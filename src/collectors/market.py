"""行情采集器：``market_collector``（Phase 1 第 3 步第 1 个具体采集器）。

数据来源（团队批准使用免费接口）：
    使用 **Yahoo Finance chart JSON 端点**（``/v8/finance/chart/{symbol}``）——这正是
    ``yfinance`` 库内部调用的同一接口。选它而不是 yfinance 的原因：
    1. 不引入 pandas 等重型依赖，采集器保持轻量；
    2. 完全走本项目的 Transport 层（可重试 / 可超时 / 可 Mock / 可断点），便于测试；
    3. 无需 API Key（Alpha Vantage 需要 Key，禁止硬编码；待提供 Key 后再新增 provider）。
    主机名从数据源配置 ``sources.base_url`` 读取，**不在代码里硬编码站点地址**。

落库目标（04 §13 + 01 §4.3）：
    - ``market_bars``：结构化行情（instrument_id / timeframe / open_time / close_time /
      open / high / low / close / volume / source_id / collected_at / effective_at）；
    - ``raw_items``（item_type=QUOTE）：逐根保留原始 JSON 切片，保证"任何研究输入可追溯"。

时间语义（08 §4 Market Bar 验收项）：
    - ``open_time`` 为 **UTC 分钟精度**（provider 返回 epoch 秒，直接换算）；
    - ``close_time = open_time + 周期``；
    - ``published_at = close_time``（K 线收盘后才可见），
      ``effective_at = max(published_at, collected_at)`` —— 回补历史数据时以"系统真正
      采集到的时间"为准，防止未来数据泄漏。

数据质量（用户明确要求）：
    - 单轮获得条数低于期望（窗口时长 ÷ 周期）→ WARNING 日志 + ``last_warnings``；
    - 相邻 K 线出现缺口（例如缺了 1 分钟）→ WARNING 日志，附缺失根数与示例时间。

⚠️ 生产 / 研究环境必须使用 PostgreSQL；SQLite 仅用于本地开发与自动化测试。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import Instrument, MarketBar, Source
from database.models.enums import TIMEFRAME_SECONDS, RawItemType, SourceType
from src.collectors.base import PERSIST_DUPLICATE, PERSIST_INSERTED, BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.transport import HttpRequest
from src.collectors.types import CollectOutcome, CollectWindow, FetchPage, RawItemPayload
from src.common.time import parse_iso8601

__all__ = [
    "DEFAULT_TIMEFRAMES",
    "YAHOO_INTERVAL_BY_TIMEFRAME",
    "MarketBarPoint",
    "MarketCollector",
    "ParsedChart",
    "detect_bar_gaps",
    "parse_yahoo_chart",
    "to_decimal",
]

_log = get_logger("collectors.market")

#: Yahoo chart 端点路径（主机名来自 sources.base_url，代码中不写死站点地址）
YAHOO_CHART_PATH: Final[str] = "/v8/finance/chart/{symbol}"

#: 项目周期 → Yahoo interval 参数（Yahoo 无 4h，需要聚合，故此处不含 "4h"）
YAHOO_INTERVAL_BY_TIMEFRAME: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
}

DEFAULT_TIMEFRAMES: Final[tuple[str, ...]] = ("1m",)

#: 价格 / 成交量量化精度（03 §14：NUMERIC(20,8)，禁止 binary float）
PRICE_QUANT: Final[Decimal] = Decimal("0.00000001")

MAX_GAP_EXAMPLES: Final[int] = 3
MAX_GAPS_REPORTED: Final[int] = 500


@dataclass(frozen=True, slots=True)
class MarketBarPoint:
    """一根解析后的 K 线（UTC，分钟精度）。"""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    @property
    def epoch_seconds(self) -> int:
        return int(self.open_time.timestamp())

    def record_key(self, symbol: str, timeframe: str) -> str:
        """raw_items 幂等键：``symbol:timeframe:open_time_epoch``（与采集次数无关，可复现）。"""
        return f"{symbol}:{timeframe}:{self.epoch_seconds}"


@dataclass(frozen=True, slots=True)
class ParsedChart:
    """一次解析的结果：有效 K 线 + 被跳过的无效 bar 统计（数据质量用）。"""

    points: tuple[MarketBarPoint, ...] = ()
    skipped: int = 0
    skipped_examples: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        """provider 返回的 bar 总数（含被跳过的空 bar）。"""
        return len(self.points) + self.skipped


def to_decimal(value: object) -> Decimal | None:
    """把 provider 返回值安全转成 Decimal（无法解析返回 None，由调用方判定为无效 bar）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _value_at(values: Sequence[Any] | None, index: int) -> Any:
    if not values or index >= len(values):
        return None
    return values[index]


def parse_yahoo_chart(
    payload: Mapping[str, Any] | None,
    *,
    symbol: str,
    timeframe: str,
) -> ParsedChart:
    """把 Yahoo chart JSON 解析为 K 线（纯函数，便于单元测试）。

    Raises:
        CollectorError: 响应结构异常、provider 报错、缺少 result 或周期非法。
    """
    if timeframe not in YAHOO_INTERVAL_BY_TIMEFRAME:
        raise CollectorError(
            f"provider 不支持的周期：{timeframe!r}"
            f"（{YAHOO_INTERVAL_BY_TIMEFRAME} 允许：{sorted(YAHOO_INTERVAL_BY_TIMEFRAME)}）"
        )
    if not isinstance(payload, Mapping):
        raise CollectorError(
            f"行情响应不是 JSON 对象：symbol={symbol} timeframe={timeframe}",
            details={"symbol": symbol, "timeframe": timeframe},
        )

    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise CollectorError(
            f"行情响应缺少 chart 字段：symbol={symbol} timeframe={timeframe}",
            details={"symbol": symbol, "timeframe": timeframe},
        )

    error = chart.get("error")
    if error:
        raise CollectorError(
            f"行情接口返回错误：{error!r}",
            details={"symbol": symbol, "timeframe": timeframe, "provider_error": error},
        )

    results = chart.get("result")
    if not isinstance(results, list) or not results:
        raise CollectorError(
            f"行情响应缺少 result：symbol={symbol} timeframe={timeframe}",
            details={"symbol": symbol, "timeframe": timeframe},
        )

    result = results[0] if isinstance(results[0], Mapping) else {}
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    quote = quotes[0] if quotes and isinstance(quotes[0], Mapping) else {}

    opens = quote.get("open")
    highs = quote.get("high")
    lows = quote.get("low")
    closes = quote.get("close")
    volumes = quote.get("volume")

    bar_seconds = TIMEFRAME_SECONDS[timeframe]
    seen: set[int] = set()
    points: list[MarketBarPoint] = []
    skipped = 0
    skipped_examples: list[int] = []

    for index, raw_timestamp in enumerate(timestamps):
        try:
            epoch = int(raw_timestamp)
        except (TypeError, ValueError):
            skipped += 1
            continue

        open_price = to_decimal(_value_at(opens, index))
        high_price = to_decimal(_value_at(highs, index))
        low_price = to_decimal(_value_at(lows, index))
        close_price = to_decimal(_value_at(closes, index))

        # provider 对空档（停牌 / 无成交）返回 None，这类 bar 不得进入研究数据
        if None in (open_price, high_price, low_price, close_price):
            skipped += 1
            if len(skipped_examples) < MAX_GAP_EXAMPLES:
                skipped_examples.append(epoch)
            continue

        assert open_price is not None  # 类型收窄（上一行已排除 None）
        assert high_price is not None
        assert low_price is not None
        assert close_price is not None

        # 把 04 §13 的数据库 CHECK 前置到解析层，避免脏数据打库失败
        if min(open_price, high_price, low_price, close_price) <= 0:
            skipped += 1
            continue
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
            skipped += 1
            continue

        if epoch in seen:  # provider 偶发重复时间戳
            continue
        seen.add(epoch)

        open_time = datetime.fromtimestamp(epoch, tz=UTC).replace(microsecond=0)
        points.append(
            MarketBarPoint(
                open_time=open_time,
                close_time=open_time + timedelta(seconds=bar_seconds),
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=to_decimal(_value_at(volumes, index)),
            )
        )

    points.sort(key=lambda point: point.open_time)
    return ParsedChart(
        points=tuple(points), skipped=skipped, skipped_examples=tuple(skipped_examples)
    )


def detect_bar_gaps(
    open_times: Sequence[datetime], *, timeframe: str
) -> tuple[datetime, ...]:
    """返回缺失的 K 线起始时间（相邻两根间隔 > 周期 → 中间缺失）。

    用途：满足"行情数据缺了 1 分钟必须告警"的数据质量要求（08 §13 Market Gap）。
    为防御异常数据（例如跨越数月的时间戳），单次最多报告 ``MAX_GAPS_REPORTED`` 个缺口。
    """
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期：{timeframe!r}")

    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    ordered = sorted({_as_utc(moment) for moment in open_times})
    gaps: list[datetime] = []

    for previous, current in zip(ordered, ordered[1:], strict=False):
        expected = previous + step
        while expected < current:
            gaps.append(expected)
            if len(gaps) >= MAX_GAPS_REPORTED:
                return tuple(gaps)
            expected += step
    return tuple(gaps)


def _as_utc(moment: datetime) -> datetime:
    """统一为 UTC（naive 时间视为错误用法，直接拒绝）。"""
    from src.common.time import to_utc

    return to_utc(moment, assume_tz=None, field_name="open_time")


@register_collector
class MarketCollector(BaseCollector):
    """行情采集器（Yahoo chart JSON provider）。

    采集计划 = ``sources.config_json["symbols"] × ["timeframes"]``（笛卡尔积）：
    每个 (symbol, timeframe) 组合作为一"页"，游标记录计划下标，天然支持断点续采；
    单个标的 404 / 单页失败不会拖垮其他标的（06_Cline开发规则 第 12 条）。
    """

    collector_name = "market_collector"
    source_type = SourceType.MARKET
    provider = "yahoo_chart"
    max_pages_per_run = 64

    def __init__(
        self,
        source: Source,
        *,
        instrument_symbols: Sequence[str] | None = None,
        timeframes: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})

        declared_provider = config.get("provider")
        if declared_provider not in (None, self.provider):
            raise CollectorError(
                f"暂不支持的行情 provider={declared_provider!r}；当前仅实现 {self.provider!r}"
                "（Alpha Vantage 需要 API Key，待提供 Key 后再新增 provider 实现）",
                details={"provider": declared_provider, "supported": self.provider},
            )

        configured_symbols = (
            instrument_symbols if instrument_symbols is not None else config.get("symbols")
        )
        symbols = tuple(str(symbol) for symbol in (configured_symbols or ()))
        if not symbols:
            raise CollectorError(
                "行情采集器未配置标的：请在 sources.config_json['symbols'] 中声明 XAUUSD 等标的"
                "（可先执行 python -m database.seeds --scope all）"
            )

        configured_timeframes = timeframes if timeframes is not None else config.get("timeframes")
        requested = tuple(str(tf) for tf in (configured_timeframes or DEFAULT_TIMEFRAMES))
        supported = tuple(tf for tf in requested if tf in YAHOO_INTERVAL_BY_TIMEFRAME)
        unsupported = tuple(tf for tf in requested if tf not in YAHOO_INTERVAL_BY_TIMEFRAME)
        if unsupported:
            _log.warning(
                "%s 跳过 provider 不支持的周期 %s（provider=%s；4h 需要客户端聚合）",
                self.collector_name,
                list(unsupported),
                self.provider,
            )
        if not supported:
            raise CollectorError(f"所有配置周期都不被 {self.provider} 支持：{list(requested)}")

        self.symbols: tuple[str, ...] = symbols
        self.timeframes: tuple[str, ...] = supported
        self._plan: tuple[tuple[str, str], ...] = tuple(
            (symbol, timeframe) for symbol in symbols for timeframe in supported
        )
        self._instruments: dict[str, Instrument] = {}
        self._bar_times: dict[tuple[str, str], list[datetime]] = {}
        self._extra_warnings: list[str] = []

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self._bar_times.clear()
        self._extra_warnings.clear()

    # ------------------------------------------------------------------
    # 站点相关：构造请求
    # ------------------------------------------------------------------
    def _build_request(self, *, symbol: str, timeframe: str, window: CollectWindow) -> HttpRequest:
        base_url = (self.source.base_url or "").rstrip("/")
        if not base_url:
            raise CollectorError(f"数据源 {self.source.name!r} 未配置 base_url，无法构造行情请求")
        return HttpRequest(
            url=f"{base_url}{YAHOO_CHART_PATH.format(symbol=symbol)}",
            params={
                "interval": YAHOO_INTERVAL_BY_TIMEFRAME[timeframe],
                "period1": int(window.start_utc.timestamp()),
                "period2": int(window.end_utc.timestamp()),
                "includePrePost": "false",
                "events": "div,splits",
            },
        )


    # ------------------------------------------------------------------
    # 站点相关：翻页与载荷构造
    # ------------------------------------------------------------------
    async def _do_fetch(
        self, cursor: dict[str, Any] | None, window: CollectWindow
    ) -> FetchPage:
        plan_index = int((cursor or {}).get("plan_index", 0))
        if plan_index >= len(self._plan):
            return FetchPage(next_cursor=None)

        symbol, timeframe = self._plan[plan_index]
        request = self._build_request(symbol=symbol, timeframe=timeframe, window=window)
        response = await self._request(request)

        if response.status == 404:
            # 单个标的不存在不应拖垮整轮采集：记录告警并继续下一个组合
            warning = f"{symbol} {timeframe}：行情接口返回 404（标的可能不存在），本轮跳过"
            _log.warning("%s | %s", self.collector_name, warning)
            self._extra_warnings.append(warning)
            return FetchPage(payloads=(), next_cursor=self._next_cursor(plan_index))

        if not response.ok:
            raise CollectorError(
                f"行情接口返回 HTTP {response.status}：symbol={symbol} timeframe={timeframe}",
                details={"status": response.status, "symbol": symbol, "timeframe": timeframe},
            )

        parsed = parse_yahoo_chart(response.json_body, symbol=symbol, timeframe=timeframe)
        if parsed.skipped:
            self._extra_warnings.append(
                f"{symbol} {timeframe}：provider 返回 {parsed.total} 根，"
                f"其中 {parsed.skipped} 根为空值/非法已被跳过"
            )

        payloads = tuple(
            self._to_payload(point, symbol=symbol, timeframe=timeframe, source_url=request.url)
            for point in parsed.points
        )
        return FetchPage(
            payloads=payloads,
            next_cursor=self._next_cursor(plan_index),
            raw_count=parsed.total,
        )

    def _next_cursor(self, plan_index: int) -> dict[str, Any] | None:
        """下一个 (symbol, timeframe) 组合；``None`` 表示本轮计划已执行完毕。"""
        next_index = plan_index + 1
        if next_index >= len(self._plan):
            return None
        symbol, timeframe = self._plan[next_index]
        return {"plan_index": next_index, "symbol": symbol, "timeframe": timeframe}

    def _to_payload(
        self,
        point: MarketBarPoint,
        *,
        symbol: str,
        timeframe: str,
        source_url: str,
    ) -> RawItemPayload:
        """把 K 线转成框架统一的 payload（原始切片完整保留，便于追溯）。"""
        return RawItemPayload(
            source_record_id=point.record_key(symbol, timeframe),
            item_type=RawItemType.QUOTE,
            title=f"{symbol} {timeframe} {point.open_time.isoformat()}",
            content_text=None,
            raw_json={
                "provider": self.provider,
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": point.open_time.isoformat(),
                "close_time": point.close_time.isoformat(),
                "open": str(point.open),
                "high": str(point.high),
                "low": str(point.low),
                "close": str(point.close),
                "volume": None if point.volume is None else str(point.volume),
            },
            source_url=source_url,
            published_at=point.close_time,
        )


    # ------------------------------------------------------------------
    # 落库：market_bars（结构化）+ raw_items（原始切片）
    # ------------------------------------------------------------------
    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
        """写入一根 K 线；已存在则判重（幂等）。

        顺序说明：先写 ``market_bars``（研究事实数据），再写 ``raw_items``（原始切片）。
        两者在同一事务内，因此不会出现"只有其一"的中间状态。
        """
        meta = payload.raw_json
        symbol = str(meta["symbol"])
        timeframe = str(meta["timeframe"])
        open_time = parse_iso8601(str(meta["open_time"]), field_name="open_time")
        close_time = parse_iso8601(str(meta["close_time"]), field_name="close_time")
        instrument = self._instrument_for(session, symbol)

        exists = session.scalar(
            sa.select(MarketBar.id)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.timeframe == timeframe,
                MarketBar.open_time == open_time,
            )
            .limit(1)
        )
        if exists is not None:
            return PERSIST_DUPLICATE

        collected_at = self._clock()
        volume_raw = meta.get("volume")
        session.add(
            MarketBar(
                instrument_id=instrument.id,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=_required_decimal(meta["open"], field="open"),
                high=_required_decimal(meta["high"], field="high"),
                low=_required_decimal(meta["low"], field="low"),
                close=_required_decimal(meta["close"], field="close"),
                volume=(
                    None if volume_raw is None else _required_decimal(volume_raw, field="volume")
                ),
                source_id=self.source.id,
                collected_at=collected_at,
                effective_at=payload.resolve_effective_at(collected_at),
            )
        )
        # 原始 JSON 切片：沿用基类的 raw_items 幂等写入（01 §4.3 可追溯要求）
        super()._persist_payload(session, payload)
        session.flush()

        self._bar_times.setdefault((symbol, timeframe), []).append(open_time)
        return PERSIST_INSERTED

    def _instrument_for(self, session: Session, symbol: str) -> Instrument:
        """按 symbol 取标的（进程内缓存）；缺失时给出可执行的修复指引。"""
        cached = self._instruments.get(symbol)
        if cached is not None:
            return cached
        instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == symbol))
        if instrument is None:
            raise CollectorError(
                f"instruments 中不存在标的 {symbol}：请先执行 "
                "python -m database.seeds --scope instruments",
                details={"symbol": symbol},
            )
        self._instruments[symbol] = instrument
        return instrument

    # ------------------------------------------------------------------
    # 数据质量：期望条数 + 缺口告警
    # ------------------------------------------------------------------
    def _expected_min_records(self, window: CollectWindow) -> int:
        """期望条数 = Σ(窗口秒数 ÷ 周期秒数)，每个组合再留 1 根边界容差。"""
        seconds = int((window.end_utc - window.start_utc).total_seconds())
        total = 0
        for _symbol, timeframe in self._plan:
            per_pair = seconds // TIMEFRAME_SECONDS[timeframe]
            total += max(1, per_pair - 1)
        return total

    def _evaluate_run_warnings(
        self, window: CollectWindow, outcome: CollectOutcome
    ) -> tuple[str, ...]:
        """在通用"条数不足"告警之外，追加行情专属告警：空值 bar、404、K 线缺口。"""
        warnings = list(super()._evaluate_run_warnings(window, outcome))
        warnings.extend(self._extra_warnings)
        for (symbol, timeframe), times in sorted(self._bar_times.items()):
            gaps = detect_bar_gaps(times, timeframe=timeframe)
            if gaps:
                examples = ", ".join(moment.isoformat() for moment in gaps[:MAX_GAP_EXAMPLES])
                warnings.append(
                    f"{symbol} {timeframe} 缺失 {len(gaps)} 根 K 线（示例：{examples}）"
                )
        return tuple(warnings)


def _required_decimal(value: object, *, field: str) -> Decimal:
    """K 线字段必须可解析为 Decimal（否则抛领域异常，而不是静默写入脏数据）。"""
    parsed = to_decimal(value)
    if parsed is None:
        raise CollectorError(
            f"K 线字段 {field} 无法解析为数值：{value!r}", details={"field": field}
        )
    return parsed




