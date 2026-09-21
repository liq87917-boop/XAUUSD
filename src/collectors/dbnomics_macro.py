"""DBnomics 宏观序列采集器：``dbnomics_macro``。

数据源：dbnomics ``fetch_series('WB', 'GOLD')``（世界银行黄金价格序列，或等效 IMF/FRED）。
依赖：``dbnomics``（已批准新增，见 pyproject.toml）；代码**延迟 import**，测试 100% Mock。

落库（严格按 04 §15 ``macro_events``）：
- ``event_code = series_id``（如 ``WB/GOLD``）；``event_at`` = 观测期（UTC 00:00）；
- ``actual_value`` = 观测值；``source_id`` = 当前数据源；
- 逐条写 ``raw_items(item_type=MACRO)`` 原始切片。

R3 修复（P0 红线，最高优先级）：
    DBnomics 观测值有发布延迟，且 API 无法给出准确的 ``released_at``。因此：
    - ``released_at`` 保守取 ``date``（观测期），并写
      ``raw_json.release_time_provenance = UNTRUSTED_RELEASE_TIME``；
    - ``effective_at = max(released_at, collected_at)`` —— 实际以系统获取时刻为准；
    - **带 UNTRUSTED_RELEASE_TIME 的记录不得用于 Macro Alpha 训练**（由下游 gate 强制）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import MacroEvent, Source
from database.models.enums import RawItemType, SourceType
from src.collectors.base import PERSIST_DUPLICATE, PERSIST_INSERTED, BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload
from src.common.time import parse_iso8601

_log = get_logger("collectors.dbnomics_macro")

#: 默认序列：世界银行黄金价格（provider, series）
DBNOMICS_PROVIDER: Final[str] = "WB"
DBNOMICS_SERIES: Final[str] = "GOLD"
#: series_id = provider/series（event_code 落库值）
DBNOMICS_SERIES_ID: Final[str] = f"{DBNOMICS_PROVIDER}/{DBNOMICS_SERIES}"
#: 无法确定 released_at 的标注（R3 修复：禁止用于 Macro Alpha 训练）
UNTRUSTED_RELEASE_TIME: Final[str] = "UNTRUSTED_RELEASE_TIME"

#: 数值精度（03 §14：宏观值 NUMERIC(28,10)）
VALUE_QUANT: Final[Decimal] = Decimal("0.0000000001")

REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("period", "value")


@dataclass(frozen=True, slots=True)
class DbnomicsObservation:
    """一条 DBnomics 宏观观测（保持 provider 原始粒度）。"""

    series_id: str
    event_at: datetime
    released_at: datetime
    value: Decimal
    unit: str | None = None

    def record_key(self) -> str:
        return f"{self.series_id}:{self.event_at.date().isoformat()}"


def to_decimal(value: object) -> Decimal | None:
    """把 provider 返回值安全转成 Decimal（无法解析返回 None）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(VALUE_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_dbnomics_series(
    rows: Sequence[Mapping[str, Any]], *, series_id: str = DBNOMICS_SERIES_ID
) -> tuple[DbnomicsObservation, ...]:
    """把 ``dbnomics.fetch_series`` 的返回（记录列表）解析为宏观观测（纯函数）。

    ``rows`` 每行需含 ``period``（观测期）与 ``value``（观测值）。
    缺字段 / 时间无法解析 → 抛 ``CollectorError``（不静默丢数据）。
    """
    observations: list[DbnomicsObservation] = []
    for index, row in enumerate(rows):
        missing = [column for column in REQUIRED_COLUMNS if column not in row]
        if missing:
            raise CollectorError(
                f"DBnomics 序列 {series_id} 第 {index} 行缺少字段：{missing}",
                details={"series_id": series_id, "row_index": index, "missing": missing},
            )
        event_at = _parse_period(row["period"])
        value = to_decimal(row.get("value"))
        if value is None:
            raise CollectorError(
                f"DBnomics 序列 {series_id} 第 {index} 行 value 无法解析为数值",
                details={"series_id": series_id, "row_index": index},
            )
        # R3 修复：released_at 无法确定，保守取观测期（观测期 <= 真实发布时间）。
        released_at = event_at
        observations.append(
            DbnomicsObservation(
                series_id=series_id,
                event_at=event_at,
                released_at=released_at,
                value=value,
                unit=row.get("unit"),
            )
        )
    return tuple(observations)


def _parse_period(value: object) -> datetime:
    """把观测期解析为 UTC 00:00（支持 str / date / datetime / Timestamp）。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CollectorError(f"DBnomics 观测期无法解析：{value!r}") from exc
    elif hasattr(value, "to_pydatetime"):
        parsed = value.to_pydatetime()
    else:
        raise CollectorError(f"DBnomics 观测期类型非法：{type(value).__name__}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@register_collector
class DbnomicsMacroCollector(BaseCollector):
    """DBnomics 宏观序列采集器（同步库，100% Mock 测试）。"""

    collector_name: ClassVar[str] = "dbnomics_macro"
    source_type: ClassVar[SourceType] = SourceType.MACRO
    max_pages_per_run: ClassVar[int] = 1

    def __init__(
        self,
        source: Source,
        *,
        fetch_series: Callable[[str, str], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})
        self.provider = str(config.get("provider") or DBNOMICS_PROVIDER)
        self.series = str(config.get("series") or DBNOMICS_SERIES)
        self.series_id = f"{self.provider}/{self.series}"
        self.country = str(config.get("country") or "US")
        self.unit = config.get("unit")
        #: 测试注入 Mock，生产 lazy import dbnomics
        self._fetch_series = fetch_series

    def _fetch_frame(self) -> Any:
        if self._fetch_series is not None:
            return self._fetch_series(self.provider, self.series)
        import dbnomics  # 延迟 import：模块导入不强制安装 dbnomics

        return dbnomics.fetch_series(self.provider, self.series)

    def _rows_from_frame(self, frame: Any) -> Sequence[Mapping[str, Any]]:
        records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
        return records

    async def _do_fetch(self, cursor: dict[str, Any] | None, window: CollectWindow) -> FetchPage:
        frame = self._fetch_frame()
        rows = self._rows_from_frame(frame)
        observations = parse_dbnomics_series(rows, series_id=self.series_id)
        payloads = tuple(self._to_payload(obs) for obs in observations)
        return FetchPage(payloads=payloads, next_cursor=None, raw_count=len(observations))

    def _to_payload(self, observation: DbnomicsObservation) -> RawItemPayload:
        return RawItemPayload(
            source_record_id=observation.record_key(),
            item_type=RawItemType.MACRO,
            title=f"{observation.series_id} {observation.event_at.date().isoformat()}",
            content_text=None,
            raw_json={
                "provider": "dbnomics",
                "series_id": observation.series_id,
                "event_at": observation.event_at.isoformat(),
                "released_at": observation.released_at.isoformat(),
                "value": str(observation.value),
                "unit": observation.unit,
                # R3 修复：released_at 无法确定，标注 UNTRUSTED，禁止用于 Macro Alpha 训练
                "release_time_provenance": UNTRUSTED_RELEASE_TIME,
            },
            source_url=None,
            published_at=observation.released_at,
        )

    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
        meta = payload.raw_json
        series_id = str(meta["series_id"])
        event_at = parse_iso8601(str(meta["event_at"]), field_name="event_at")
        released_at = parse_iso8601(str(meta["released_at"]), field_name="released_at")

        exists = session.scalar(
            sa.select(MacroEvent.id)
            .where(
                MacroEvent.source_id == self.source.id,
                MacroEvent.event_code == series_id,
                MacroEvent.event_at == event_at,
            )
            .limit(1)
        )
        if exists is not None:
            return PERSIST_DUPLICATE

        collected_at = self._clock()
        session.add(
            MacroEvent(
                event_code=series_id,
                country=self.country,
                event_at=event_at,
                released_at=released_at,
                vintage_end_at=None,
                actual_value=_required_decimal(meta["value"], field="value"),
                forecast_value=None,
                previous_value=None,
                unit=self.unit,
                source_id=self.source.id,
                collected_at=collected_at,
                effective_at=payload.resolve_effective_at(collected_at),
            )
        )
        super()._persist_payload(session, payload)
        session.flush()
        return PERSIST_INSERTED


def _required_decimal(value: object, *, field: str) -> Decimal:
    parsed = to_decimal(value)
    if parsed is None:
        raise CollectorError(
            f"DBnomics 字段 {field} 无法解析为数值：{value!r}", details={"field": field}
        )
    return parsed
