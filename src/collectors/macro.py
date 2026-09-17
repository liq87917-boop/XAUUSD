"""宏观数据采集器：``macro_collector``（Phase 1 第 3 步第 3 个具体采集器）。

数据源（团队批复：只用公开权威源，绝不爬取收费网站）：
    - **FRED（美联储经济数据）官方公开 API**（``/fred/series/observations``），
      series 列表来自 ``sources.config_json["series"]``（支持字符串或
      ``{"series_id", "country", "unit"}`` 字典）；
    - API Key 只从**环境变量**读取（``config_json["api_key_env"]``，默认 ``FRED_API_KEY``）：
      **绝不硬编码/写库**（团队批复），日志与原始记录中对 key 一律脱敏；
    - 离线兜底：``config_json["csv_path"]`` 指向本地 CSV 时走 CSV 导入
      （mock/存档数据，用于无 Key 环境与框架验证）。

时间对齐（Phase 3.0 W0-2，最高优先级）：
    - ``event_at`` = 观测所属日期（00:00 UTC），不是发布时间；
    - ALFRED ``realtime_start/realtime_end`` 表示 vintage 的已知区间。API 只有日期精度，
      ``released_at`` 保守取 ``realtime_start`` 次日 00:00 UTC，禁止假设当天零点已知；
    - ``effective_at = max(released_at, collected_at)``；缺 ``realtime_start`` 时硬失败；
    - 未来日期观测直接跳过并告警（防脏数据触发 CHECK 失败 / 泄露未来信息）。

粒度（团队批复）：
    **保持 provider 原始粒度**（日/月/季），不重采样、不拉平：月/季序列的日期就是
    provider 给出的观测日期（例如 ``2026-07-01`` 代表 7 月）。

落库（严格按 04 §15 ``macro_events``）：
    ``event_code=series_id`` / ``country`` / ``event_at`` / ``actual_value`` / ``unit`` /
    ``source_id`` / ``collected_at`` / ``effective_at``；``forecast_value`` 与
    ``previous_value`` 保持 NULL —— FRED 观测不含预期值，**不凭空推断**（Phase 2 可接日历源补全）。
    同时逐条保留 ``raw_items(item_type=MACRO)`` 原始切片，保证可追溯。

⚠️ 生产 / 研究环境必须使用 PostgreSQL；SQLite 仅用于本地开发与自动化测试。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from config.logging import get_logger
from config.settings import get_settings
from database.models import MacroEvent, RawItem
from database.models.enums import RawItemType, SourceType
from src.collectors.base import BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.transport import HttpRequest
from src.collectors.types import (
    CollectorHealth,
    CollectOutcome,
    CollectWindow,
    FetchPage,
    RawItemPayload,
)
from src.common.time import parse_iso8601

__all__ = [
    "DEFAULT_W0_2_SERIES",
    "FRED_PARSER_VERSION",
    "MACRO_CSV_PARSER_VERSION",
    "MacroCollector",
    "MacroObservation",
    "MacroSeriesSpec",
    "ParsedObservations",
    "parse_fred_observations",
    "parse_macro_csv",
    "parse_realtime_boundary",
]

_log = get_logger("collectors.macro")

FRED_PARSER_VERSION: Final[str] = "macro_alfred_vintage_parser@0.2.0"
MACRO_CSV_PARSER_VERSION: Final[str] = "macro_vintage_csv_parser@0.2.0"

#: FRED 观测端点路径（主机名来自 sources.base_url）
FRED_OBSERVATIONS_PATH: Final[str] = "/fred/series/observations"

#: FRED 用 "." 表示缺失观测（不可当作 0）
FRED_MISSING_VALUE: Final[str] = "."

#: FRED 对“该时期没有 ALFRED 历史版本”的官方错误文本片段。
ALFRED_NOT_AVAILABLE_MARKER: Final[str] = "does not exist in ALFRED"

#: 数值精度（03 §14：宏观值 NUMERIC(28,10)）
VALUE_QUANT: Final[Decimal] = Decimal("0.0000000001")

#: 未来日期的容差（provider 时区/日界差异）
FUTURE_DATE_TOLERANCE_DAYS: Final[int] = 1

#: key 在 URL / 日志中的脱敏占位符
REDACTED_KEY: Final[str] = "***"

#: Phase 3.0 W0-2 批准的宏观序列；种子与回填脚本共用，防止清单漂移。
DEFAULT_W0_2_SERIES: Final[tuple[dict[str, str], ...]] = (
    {"series_id": "CPIAUCSL", "country": "US", "unit": "index"},
    {"series_id": "PCEPI", "country": "US", "unit": "index"},
    {"series_id": "PAYEMS", "country": "US", "unit": "thousand_persons"},
    {"series_id": "DFF", "country": "US", "unit": "percent"},
    {"series_id": "DFII10", "country": "US", "unit": "percent"},
    {"series_id": "DGS10", "country": "US", "unit": "percent"},
    {"series_id": "DTWEXBGS", "country": "US", "unit": "index"},
    {"series_id": "FEDFUNDS", "country": "US", "unit": "percent"},
)


@dataclass(frozen=True, slots=True)
class MacroSeriesSpec:
    """一个宏观序列的配置（来自 ``sources.config_json["series"]``）。"""

    series_id: str
    country: str = "US"
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class MacroObservation:
    """一条宏观观测（保持 provider 原始粒度）。"""

    series_id: str
    event_at: datetime
    released_at: datetime
    vintage_end_at: datetime | None
    value: Decimal
    unit: str | None = None

    @property
    def record_id(self) -> str:
        """raw_items 幂等键包含 vintage 起点，修订值会追加而不是覆盖。"""
        return (
            f"{self.series_id}:{self.event_at.date().isoformat()}:"
            f"{self.released_at.date().isoformat()}"
        )


@dataclass(frozen=True, slots=True)
class ParsedObservations:
    """解析结果：有效观测 + 被跳过条数与原因（数据质量用）。"""

    points: tuple[MacroObservation, ...] = ()
    skipped: int = 0
    skip_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.points) + self.skipped


def to_value_decimal(value: object) -> Decimal | None:
    """把观测值安全转成 Decimal（无法解析返回 None）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == FRED_MISSING_VALUE:
        return None
    try:
        return Decimal(text).quantize(VALUE_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_observation_date(value: object) -> datetime | None:
    """把 ``YYYY-MM-DD`` 解析为 UTC 当日 00:00（保持日粒度）。"""
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def parse_realtime_boundary(value: object, *, open_ended: bool = False) -> datetime | None:
    """把 ALFRED 日期转换为保守的次日 UTC 边界。

    ``realtime_start`` 只保证数据在该自然日内变得可知，使用次日 00:00 可避免日内前视。
    ``9999-12-31`` 是开放上界哨兵，在 ``open_ended=True`` 时映射为 NULL。
    """
    if value is None:
        return None
    text = str(value).strip()
    if open_ended and text == "9999-12-31":
        return None
    parsed = parse_observation_date(text)
    return None if parsed is None else parsed + timedelta(days=1)


def parse_fred_observations(
    payload: Mapping[str, Any] | None,
    *,
    spec: MacroSeriesSpec,
    window_end: datetime | None = None,
) -> ParsedObservations:
    """解析 FRED ``/fred/series/observations`` 响应（纯函数，便于单测）。

    Args:
        payload: FRED JSON 响应。
        spec: 该序列的配置（series_id / country / unit）。
        window_end: 采集窗口结束时间；给出时会拒绝**未来日期**观测（防脏数据与未来信息）。

    Returns:
        :class:`ParsedObservations`（有效观测 + 跳过条数/原因）。

    Raises:
        CollectorError: 响应结构异常，或 FRED 返回 ``error_message``（key/series 无效等）。
    """
    if not isinstance(payload, Mapping):
        raise CollectorError(
            f"FRED 响应不是 JSON 对象：series={spec.series_id}",
            details={"series_id": spec.series_id},
        )

    error_message = payload.get("error_message")
    if error_message:
        raise CollectorError(
            f"FRED 返回错误：{error_message}",
            details={"series_id": spec.series_id, "error_code": payload.get("error_code")},
        )

    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        raise CollectorError(
            f"FRED 响应缺少 observations 字段：series={spec.series_id}",
            details={"series_id": spec.series_id},
        )

    response_unit = payload.get("units")
    unit = str(response_unit) if response_unit else spec.unit
    initial_release_only = str(payload.get("output_type") or "") == "4"
    future_limit = (
        None
        if window_end is None
        else window_end + timedelta(days=FUTURE_DATE_TOLERANCE_DAYS)
    )

    points: list[MacroObservation] = []
    skipped = 0
    reasons: list[str] = []
    seen_vintages: set[tuple[str, str]] = set()

    for raw in raw_observations:
        if not isinstance(raw, Mapping):
            skipped += 1
            reasons.append("malformed_entry")
            continue

        event_at = parse_observation_date(raw.get("date"))
        if event_at is None:
            skipped += 1
            reasons.append("missing_or_invalid_date")
            continue

        released_at = parse_realtime_boundary(raw.get("realtime_start"))
        # output_type=4 的 realtime_end 可能只是查询边界，不是该初值真正的修订失效日。
        vintage_end_at = (
            None
            if initial_release_only
            else parse_realtime_boundary(raw.get("realtime_end"), open_ended=True)
        )
        if released_at is None:
            raise CollectorError(
                f"ALFRED observation 缺少合法 realtime_start：series={spec.series_id}",
                details={
                    "series_id": spec.series_id,
                    "observation_date": raw.get("date"),
                },
            )
        if vintage_end_at is not None and vintage_end_at <= released_at:
            raise CollectorError(
                f"ALFRED observation realtime 区间非法：series={spec.series_id}",
                details={
                    "series_id": spec.series_id,
                    "realtime_start": raw.get("realtime_start"),
                    "realtime_end": raw.get("realtime_end"),
                },
            )

        value = to_value_decimal(raw.get("value"))
        if value is None:
            skipped += 1
            reasons.append("missing_value")
            continue

        if future_limit is not None and event_at > future_limit:
            skipped += 1
            reasons.append("future_date")
            continue

        vintage_key = (event_at.date().isoformat(), released_at.date().isoformat())
        if vintage_key in seen_vintages:
            skipped += 1
            reasons.append("duplicate_vintage")
            continue
        seen_vintages.add(vintage_key)

        points.append(
            MacroObservation(
                series_id=spec.series_id,
                event_at=event_at,
                released_at=released_at,
                vintage_end_at=vintage_end_at,
                value=value,
                unit=unit,
            )
        )

    points.sort(key=lambda point: point.event_at)
    return ParsedObservations(
        points=tuple(points), skipped=skipped, skip_reasons=tuple(dict.fromkeys(reasons))
    )


#: 宏观 CSV 兜底解析器的必需列；released_at 禁止缺省或猜测。
CSV_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("series_id", "date", "released_at", "value")


def parse_macro_csv(csv_text: str) -> ParsedObservations:
    """从本地 CSV 导入宏观观测（离线兜底；无 FRED Key 的环境与框架验证用）。

    必需列：``series_id`` / ``date`` / ``released_at`` / ``value``；
    可选列：``vintage_end_at`` / ``unit``。
    日期按 ``YYYY-MM-DD`` 解析为 UTC 当日 00:00（保持日粒度）。

    Raises:
        CollectorError: 内容为空或缺少必需列。
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise CollectorError("宏观 CSV 内容为空，无法解析")

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = [column for column in CSV_REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        raise CollectorError(
            f"宏观 CSV 缺少必需列：{missing}（必需：{list(CSV_REQUIRED_COLUMNS)}）",
            details={"missing_columns": missing},
        )

    points: list[MacroObservation] = []
    skipped = 0
    reasons: list[str] = []

    for row in reader:
        normalized = {
            (key or "").strip().lower(): (value or "").strip() for key, value in row.items()
        }
        series_id = normalized.get("series_id")
        event_at = parse_observation_date(normalized.get("date"))
        released_at = parse_observation_date(normalized.get("released_at"))
        vintage_end_at = parse_observation_date(normalized.get("vintage_end_at"))
        value = to_value_decimal(normalized.get("value"))
        if released_at is None:
            raise CollectorError("宏观 CSV 存在缺失/非法 released_at；禁止猜测发布时间")
        if not series_id or event_at is None or value is None:
            skipped += 1
            reasons.append("incomplete_row")
            continue
        if vintage_end_at is not None and vintage_end_at <= released_at:
            raise CollectorError("宏观 CSV 的 vintage_end_at 必须晚于 released_at")
        points.append(
            MacroObservation(
                series_id=series_id,
                event_at=event_at,
                released_at=released_at,
                vintage_end_at=vintage_end_at,
                value=value,
                unit=normalized.get("unit") or None,
            )
        )

    points.sort(key=lambda point: (point.series_id, point.event_at, point.released_at))
    return ParsedObservations(
        points=tuple(points), skipped=skipped, skip_reasons=tuple(dict.fromkeys(reasons))
    )


#: FRED API Key 的默认环境变量名（密钥只从环境变量读取，绝不入库/入代码）
DEFAULT_API_KEY_ENV: Final[str] = "FRED_API_KEY"

#: 默认回看天数（宏观观测频率低，30 分钟窗口内通常没有新观测）
DEFAULT_LOOKBACK_DAYS: Final[int] = 30


def _normalize_series(raw: object) -> tuple[MacroSeriesSpec, ...]:
    """把配置中的 ``series`` 归一化为 :class:`MacroSeriesSpec` 元组。

    支持两种写法：``["CPIAUCSL", ...]`` 或
    ``[{"series_id": "CPIAUCSL", "country": "US", "unit": "index"}, ...]``。
    """
    if raw is None:
        return ()

    if isinstance(raw, (str, Mapping)):
        entries: list[object] = [raw]
    elif isinstance(raw, Sequence):
        entries = list(raw)
    else:
        raise CollectorError(f"series 配置必须是列表或字符串：{raw!r}")

    specs: list[MacroSeriesSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            spec = MacroSeriesSpec(series_id=entry.strip())
        elif isinstance(entry, Mapping):
            series_id = str(entry.get("series_id") or "").strip()
            if not series_id:
                raise CollectorError(f"series 配置项缺少 series_id：{entry!r}")
            unit = entry.get("unit")
            spec = MacroSeriesSpec(
                series_id=series_id,
                country=str(entry.get("country") or "US"),
                unit=str(unit) if unit else None,
            )
        else:
            raise CollectorError(f"series 配置项必须是字符串或字典：{entry!r}")

        if not spec.series_id:
            raise CollectorError("series_id 不能为空")
        if spec.series_id in seen:
            continue
        seen.add(spec.series_id)
        specs.append(spec)
    return tuple(specs)


@register_collector
class MacroCollector(BaseCollector):
    """宏观数据采集器：FRED 公开 API（``/fred/series/observations``）+ CSV 兜底。

    - 每个 series 作为一"页"，游标 ``series_index`` 支持断点续采；
      单个 series 出错会终止本轮（由 runner 记录 FAILED），重试由传输层负责；
    - API Key 只从环境变量读取，且**请求 URL / 原始记录中对 key 一律脱敏**。
    """

    collector_name = "macro_collector"
    source_type = SourceType.MACRO
    provider = "fred"
    max_pages_per_run = 64

    def __init__(
        self,
        source: Any,
        *,
        series: object = None,
        api_key: str | None = None,
        lookback_days: int | None = None,
        csv_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})

        declared_provider = config.get("provider")
        if declared_provider not in (None, self.provider):
            raise CollectorError(
                f"暂不支持的宏观 provider={declared_provider!r}；当前仅实现 {self.provider!r}"
                "（经济日历 etc. 待实现，严禁爬取收费站点）",
                details={"provider": declared_provider, "supported": self.provider},
            )

        configured_csv = csv_path if csv_path is not None else config.get("csv_path")
        if configured_csv:
            self._mode = "csv"
            self._csv_path: str | None = str(configured_csv)
            self._specs: tuple[MacroSeriesSpec, ...] = ()
            self.parser_version = MACRO_CSV_PARSER_VERSION
            self._api_key: str | None = None
        else:
            specs = _normalize_series(series if series is not None else config.get("series"))
            if not specs:
                raise CollectorError(
                    "宏观采集器未配置序列：请在 sources.config_json['series'] 声明 FRED series_id"
                    "（如 CPIAUCSL / PCEPI / PAYEMS / DFF），或设置 'csv_path' 走本地 CSV 兜底"
                )
            self._mode = "fred"
            self._csv_path = None
            self._specs = specs
            self.parser_version = FRED_PARSER_VERSION
            self._api_key = self._resolve_api_key(api_key, config)

        configured_lookback = (
            lookback_days if lookback_days is not None else config.get("lookback_days")
        )
        # 注意：不能用 `or` 兜底 —— 0 是非法值（会被静默替换成默认值），必须显式报错
        self._lookback_days = (
            DEFAULT_LOOKBACK_DAYS if configured_lookback is None else int(configured_lookback)
        )
        if self._lookback_days < 1:
            raise CollectorError(f"lookback_days 必须 >= 1，收到 {self._lookback_days}")

        self._extra_warnings: list[str] = []
        self._skipped_total = 0
        self._skip_reason_counter: dict[str, int] = {}

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self._extra_warnings.clear()
        self._skipped_total = 0
        self._skip_reason_counter.clear()

    def _resolve_api_key(self, explicit: str | None, config: Mapping[str, Any]) -> str:
        """解析 FRED API Key：显式注入（测试）> 全局 Settings（环境变量 / .env）。"""
        if explicit:
            return explicit.strip()
        env_name = str(config.get("api_key_env") or DEFAULT_API_KEY_ENV)
        if env_name != DEFAULT_API_KEY_ENV:
            raise CollectorError(
                f"自定义 api_key_env={env_name!r} 不受集中配置支持；请使用 {DEFAULT_API_KEY_ENV}",
                details={"api_key_env": env_name},
            )
        secret = get_settings().fred_api_key
        key = secret.get_secret_value().strip() if secret is not None else ""
        if not key:
            raise CollectorError(
                f"缺少 FRED API Key：请设置环境变量 {env_name}"
                "（FRED 提供免费注册），或在构造时注入 api_key（测试用）。"
                "密钥绝不写入源码 / 配置 / 数据库（团队批复）",
                details={"api_key_env": env_name},
            )
        return key

    async def _do_health_check(self) -> CollectorHealth:
        """CSV 模式检查文件可读；FRED 模式沿用基类（检查 base_url 配置）。"""
        if self._mode == "csv":
            exists = Path(self._csv_path or "").exists()
            return CollectorHealth(
                collector_name=self.collector_name,
                healthy=exists,
                checked_at=self._clock(),
                message=None if exists else f"宏观 CSV 文件不存在：{self._csv_path}",
                details={"mode": "csv", "csv_path": self._csv_path},
            )
        return await super()._do_health_check()


    # ------------------------------------------------------------------
    # 站点相关：请求构造与翻页
    # ------------------------------------------------------------------
    def _observation_range(self, window: CollectWindow) -> tuple[date, date]:
        """观测日期范围 = ``[min(窗口开始, 窗口结束 - 回看天数), 窗口结束]``。

        为什么需要回看：宏观观测按日/月/季发布，30 分钟调度窗口内通常没有新观测；
        回看窗口让每轮都"复采近期观测"（靠 ``(series_id, date)`` 幂等键去重），
        同时使 ``min_records_per_run`` 阈值真正具备含义（FRED 故障会被立刻发现）。
        """
        end_dt = window.end_utc
        start_dt = min(window.start_utc, end_dt - timedelta(days=self._lookback_days))
        return start_dt.date(), end_dt.date()

    def _build_request(self, spec: MacroSeriesSpec, window: CollectWindow) -> HttpRequest:
        base_url = (self.source.base_url or "").rstrip("/")
        if not base_url:
            raise CollectorError(
                f"数据源 {self.source.name!r} 未配置 base_url，无法构造 FRED 请求"
            )
        start, end = self._observation_range(window)
        # FRED 服务端日界可能晚于本机 UTC；使用窗口结束日前一日可避免“未来 realtime_end” 400。
        realtime_end = end - timedelta(days=1)
        realtime_start = min(window.start_utc.date(), realtime_end)
        return HttpRequest(
            url=f"{base_url}{FRED_OBSERVATIONS_PATH}",
            params={
                "series_id": spec.series_id,
                "api_key": self._api_key or "",
                "file_type": "json",
                # Initial Release Only：避免把今天所见的修订终值用于历史训练。
                # output_type=1 的 realtime 字段会被查询窗口裁剪，不能代表真实首次发布。
                "output_type": 4,
                "realtime_start": realtime_start.isoformat(),
                "realtime_end": realtime_end.isoformat(),
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
            },
        )

    @staticmethod
    def _masked_url(request: HttpRequest) -> str:
        """脱敏 URL：``api_key`` 一律替换为 ``***``，绝不进入日志 / raw_json / source_url。"""
        params = {
            key: (REDACTED_KEY if key == "api_key" else value)
            for key, value in request.params.items()
        }
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"{request.url}?{query}"

    async def _do_fetch(
        self, cursor: dict[str, Any] | None, window: CollectWindow
    ) -> FetchPage:
        if self._mode == "csv":
            return self._fetch_from_csv(cursor)

        series_index = int((cursor or {}).get("series_index", 0))
        if series_index >= len(self._specs):
            return FetchPage(next_cursor=None)

        spec = self._specs[series_index]
        request = self._build_request(spec, window)
        response = await self._request(request)

        if not response.ok:
            body = response.json_body if isinstance(response.json_body, Mapping) else {}
            provider_message = str(body.get("error_message") or "")
            if response.status == 400 and ALFRED_NOT_AVAILABLE_MARKER in provider_message:
                warning = (
                    f"{spec.series_id}：该 realtime 窗口没有 ALFRED 历史版本，"
                    "保持为空（禁止退回 FRED 修订终值）"
                )
                self._extra_warnings.append(warning)
                return FetchPage(
                    next_cursor=self._next_cursor(series_index),
                    raw_count=0,
                )
            raise CollectorError(
                f"FRED 返回 HTTP {response.status}：series={spec.series_id}",
                details={"status": response.status, "series_id": spec.series_id},
            )

        parsed = parse_fred_observations(response.json_body, spec=spec, window_end=window.end_utc)
        self._record_skips(parsed, label=spec.series_id)

        payloads = tuple(
            self._to_payload(point, spec=spec, source_url=self._masked_url(request))
            for point in parsed.points
        )
        return FetchPage(
            payloads=payloads,
            next_cursor=self._next_cursor(series_index),
            raw_count=parsed.total,
        )

    def _fetch_from_csv(self, cursor: dict[str, Any] | None) -> FetchPage:
        """CSV 兜底：单页导入全部序列（离线/存档数据）。"""
        if int((cursor or {}).get("series_index", 0)) > 0:
            return FetchPage(next_cursor=None)

        csv_text = self._read_local_text(self._csv_path or "", kind="宏观 CSV")
        parsed = parse_macro_csv(csv_text)
        self._record_skips(parsed, label=Path(self._csv_path or "").name)

        payloads = tuple(
            self._to_payload(
                point,
                spec=MacroSeriesSpec(series_id=point.series_id, country="US", unit=point.unit),
                source_url=f"file:{self._csv_path}",
            )
            for point in parsed.points
        )
        return FetchPage(payloads=payloads, next_cursor=None, raw_count=parsed.total)

    def _next_cursor(self, series_index: int) -> dict[str, Any] | None:
        next_index = series_index + 1
        if next_index >= len(self._specs):
            return None
        return {"series_index": next_index, "series_id": self._specs[next_index].series_id}

    def _record_skips(self, parsed: ParsedObservations, *, label: str) -> None:
        """记录被跳过的观测（缺失字段 / 未来日期等）并产生 WARNING 告警。"""
        if not parsed.skipped:
            return
        self._skipped_total += parsed.skipped
        for reason in parsed.skip_reasons:
            self._skip_reason_counter[reason] = self._skip_reason_counter.get(reason, 0) + 1
        warning = (
            f"{label}：{parsed.skipped} 条观测被跳过（原因：{', '.join(parsed.skip_reasons)}）"
        )
        _log.warning("%s | %s", self.collector_name, warning)
        self._extra_warnings.append(warning)


    def _to_payload(
        self,
        point: MacroObservation,
        *,
        spec: MacroSeriesSpec,
        source_url: str,
    ) -> RawItemPayload:
        """把观测转成框架 payload（原始切片 + 已脱敏来源 URL）。"""
        return RawItemPayload(
            source_record_id=point.record_id,
            item_type=RawItemType.MACRO,
            # release 纳入标题/默认 content_hash，避免同观测期的新 vintage 被误判为重复。
            title=(
                f"{point.series_id} {point.event_at.date().isoformat()} "
                f"vintage {point.released_at.date().isoformat()}"
            ),
            content_text=None,
            raw_json={
                "provider": self.provider if self._mode == "fred" else "csv",
                "parser_version": self.parser_version,
                "series_id": point.series_id,
                "country": spec.country,
                "unit": point.unit or spec.unit,
                "event_at": point.event_at.isoformat(),
                "released_at": point.released_at.isoformat(),
                "vintage_end_at": (
                    point.vintage_end_at.isoformat() if point.vintage_end_at is not None else None
                ),
                "value": str(point.value),
                "frequency_note": "保持 provider 原始粒度（日/月/季），不重采样",
            },
            source_url=source_url,  # 已脱敏（api_key → ***）
            # RawItem 以保守 release 为发布时间，effective_at=max(release,collected)。
            published_at=point.released_at,
        )

    # ------------------------------------------------------------------
    # 落库：macro_events（04 §15）
    # ------------------------------------------------------------------
    def _after_persist(self, session: Session, raw_item: RawItem, payload: RawItemPayload) -> None:
        """``raw_items`` 落库后写入 ``macro_events``（字段严格对齐 04 §15）。"""
        meta = payload.raw_json
        event_at = parse_iso8601(str(meta["event_at"]), field_name="event_at")
        released_at = parse_iso8601(str(meta["released_at"]), field_name="released_at")
        vintage_end_raw = meta.get("vintage_end_at")
        vintage_end_at = (
            parse_iso8601(str(vintage_end_raw), field_name="vintage_end_at")
            if vintage_end_raw
            else None
        )
        unit = meta.get("unit")

        session.add(
            MacroEvent(
                event_code=str(meta["series_id"]),
                country=str(meta["country"]),
                event_at=event_at,
                released_at=released_at,
                vintage_end_at=vintage_end_at,
                actual_value=to_value_decimal(meta.get("value")),
                forecast_value=None,  # FRED observations 不含预期值，不凭空推断
                previous_value=None,  # 同上（Phase 2 可由日历源补全）
                unit=str(unit) if unit else None,
                source_id=self.source.id,
                collected_at=raw_item.collected_at,
                # = max(released_at, collected_at)：禁止 release 前可见。
                effective_at=raw_item.effective_at,
            )
        )
        session.flush()

    def _evaluate_run_warnings(
        self, window: CollectWindow, outcome: CollectOutcome
    ) -> tuple[str, ...]:
        """在通用"条数不足"告警之外，追加宏观专属告警（跳过的观测、未来日期等）。"""
        warnings = list(super()._evaluate_run_warnings(window, outcome))
        warnings.extend(self._extra_warnings)
        return tuple(warnings)




