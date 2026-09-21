"""上海黄金交易所（SGE）历史日线行情采集器：``akshare_gold``。

数据源：akshare ``ak.spot_hist_sge(symbol='Au99.99')``（上海黄金交易所 Au99.99 历史日线）。
依赖：``akshare``（已批准新增，见 pyproject.toml）；代码**延迟 import**，测试 100% Mock，
不发起真实网络请求。

落库（严格按 04 §13 ``market_bars`` + 01 §4.3 可追溯）：
- ``instrument_id`` 映射为 ``SGE_AU9999``；
- ``timeframe='1d'``；``open/high/low/close`` + ``volume``（如有）；
- 同时逐根写 ``raw_items(item_type=QUOTE)`` 原始切片。

时间语义（防未来数据泄漏）：
- ``open_time`` = provider 返回的 date（UTC 00:00）；
- ``close_time = open_time + 1 day``；
- ``published_at = close_time``（K 线收盘后才可见）；
- ``effective_at = max(close_time, collected_at)``（回补历史时以系统真正采集时刻为准）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import Instrument, MarketBar, Source
from database.models.enums import RawItemType, SourceType, Timeframe
from src.collectors.base import PERSIST_DUPLICATE, PERSIST_INSERTED, BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload
from src.common.time import parse_iso8601

_log = get_logger("collectors.akshare_gold")

#: SGE Au99.99 对应的项目标的（instruments.symbol，已注册于 seeds）
SGE_INSTRUMENT_SYMBOL: Final[str] = "SGE_AU9999"
#: akshare 的 SGE 品种符号
SGE_PROVIDER_SYMBOL: Final[str] = "Au99.99"
#: 日线周期
SGE_TIMEFRAME: Final[str] = Timeframe.D1.value

#: 价格 / 成交量量化精度（03 §14：NUMERIC(20,8)）
PRICE_QUANT: Final[Decimal] = Decimal("0.00000001")

#: akshare 中文列名 → 规范英文列名的映射（真实库不同版本列名可能不同，在 fetch 层归一）
SGE_COLUMN_ALIASES: Final[dict[str, str]] = {
    "日期": "date",
    "交易日期": "date",
    "开盘价": "open",
    "开盘": "open",
    "最高价": "high",
    "最高": "high",
    "最低价": "low",
    "最低": "low",
    "收盘价": "close",
    "收盘": "close",
    "成交量": "volume",
    "成交": "volume",
}

REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("date", "open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class SgeBarPoint:
    """一根解析后的 SGE 日线 K 线（UTC）。"""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    def record_key(self) -> str:
        """raw_items 幂等键（与采集次数无关，可复现）。"""
        return f"{SGE_INSTRUMENT_SYMBOL}:{SGE_TIMEFRAME}:{int(self.open_time.timestamp())}"


def to_decimal(value: object) -> Decimal | None:
    """把 provider 返回值安全转成 Decimal（无法解析返回 None）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_open_time(value: object, *, field: str = "date") -> datetime:
    """把 provider 返回的日期解析为 UTC 00:00（支持 str / date / datetime / Timestamp）。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        # akshare spot_hist_sge 返回的 date 列是 datetime.date（纯日期）
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CollectorError(
                f"SGE 行情 {field} 无法解析：{value!r}", details={"field": field}
            ) from exc
    elif hasattr(value, "to_pydatetime"):
        parsed = value.to_pydatetime()
    else:
        raise CollectorError(
            f"SGE 行情 {field} 类型非法：{type(value).__name__}", details={"field": field}
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def parse_sge_bars(rows: Sequence[Mapping[str, Any]]) -> tuple[SgeBarPoint, ...]:
    """把 ``ak.spot_hist_sge`` 的返回（记录列表）解析为 SGE 日线 K 线（纯函数）。

    ``rows`` 每行需含 ``date / open / high / low / close``（volume 可选）。
    任何一行缺 OHLC 或数值非法 → 抛 ``CollectorError``（不静默写脏数据）；
    违反 OHLC 关系（``high < max(open, close)`` 或 ``low > min(open, close)``）的
    异常 bar 直接跳过（SGE 结算价可能低于当日最低成交价，见冒烟实测）。
    """
    points: list[SgeBarPoint] = []
    for index, row in enumerate(rows):
        missing = [column for column in REQUIRED_COLUMNS if column not in row]
        if missing:
            raise CollectorError(
                f"SGE 行情第 {index} 行缺少字段：{missing}",
                details={"row_index": index, "missing": missing},
            )
        open_time = _parse_open_time(row["date"])
        close_time = open_time + timedelta(days=1)
        open_price = to_decimal(row["open"])
        high_price = to_decimal(row["high"])
        low_price = to_decimal(row["low"])
        close_price = to_decimal(row["close"])
        if None in (open_price, high_price, low_price, close_price):
            raise CollectorError(
                f"SGE 行情第 {index} 行 OHLC 无法解析为数值", details={"row_index": index}
            )
        assert open_price is not None
        assert high_price is not None
        assert low_price is not None
        assert close_price is not None
        # OHLC 关系校验（market_bars CHECK：high >= max(open, close) 且 low <= min(open, close)）
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
            continue
        volume = to_decimal(row.get("volume"))
        points.append(
            SgeBarPoint(
                open_time=open_time,
                close_time=close_time,
                open=open_price,  # type: ignore[arg-type]
                high=high_price,  # type: ignore[arg-type]
                low=low_price,  # type: ignore[arg-type]
                close=close_price,  # type: ignore[arg-type]
                volume=volume,
            )
        )
    return tuple(points)


@register_collector
class AkshareGoldCollector(BaseCollector):
    """SGE 日线行情采集器（akshare 同步库，100% Mock 测试）。"""

    collector_name: ClassVar[str] = "akshare_gold"
    source_type: ClassVar[SourceType] = SourceType.MARKET
    #: SGE 历史日线一次性返回，无需分页
    max_pages_per_run: ClassVar[int] = 1

    def __init__(
        self,
        source: Source,
        *,
        fetch_sge: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        #: 测试注入 Mock（返回 DataFrame 或记录列表），生产 lazy import akshare
        self._fetch_sge = fetch_sge

    def _fetch_frame(self) -> Any:
        """调用 akshare 获取 SGE 历史日线（延迟 import，测试不触发真实库）。"""
        if self._fetch_sge is not None:
            return self._fetch_sge(SGE_PROVIDER_SYMBOL)
        import akshare as ak  # 延迟 import：模块导入不强制安装 akshare

        return ak.spot_hist_sge(symbol=SGE_PROVIDER_SYMBOL)

    def _rows_from_frame(self, frame: Any) -> Sequence[Mapping[str, Any]]:
        """把 DataFrame / 记录列表归一为英文列名的记录列表。"""
        records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
        renamed: list[dict[str, Any]] = []
        for record in records:
            row = {SGE_COLUMN_ALIASES.get(str(key), key): value for key, value in record.items()}
            renamed.append(row)
        return renamed

    async def _do_fetch(self, cursor: dict[str, Any] | None, window: CollectWindow) -> FetchPage:
        # SGE 历史日线一次拉全量，无分页；cursor 仅用于断点语义，本实现忽略。
        frame = self._fetch_frame()
        rows = self._rows_from_frame(frame)
        points = parse_sge_bars(rows)
        payloads = tuple(self._to_payload(point) for point in points)
        return FetchPage(payloads=payloads, next_cursor=None, raw_count=len(points))

    def _to_payload(self, point: SgeBarPoint) -> RawItemPayload:
        return RawItemPayload(
            source_record_id=point.record_key(),
            item_type=RawItemType.QUOTE,
            title=f"{SGE_INSTRUMENT_SYMBOL} {SGE_TIMEFRAME} {point.open_time.isoformat()}",
            content_text=None,
            raw_json={
                "provider": "akshare_sge",
                "symbol": SGE_INSTRUMENT_SYMBOL,
                "timeframe": SGE_TIMEFRAME,
                "open_time": point.open_time.isoformat(),
                "close_time": point.close_time.isoformat(),
                "open": str(point.open),
                "high": str(point.high),
                "low": str(point.low),
                "close": str(point.close),
                "volume": None if point.volume is None else str(point.volume),
            },
            source_url=None,
            published_at=point.close_time,
        )

    # ------------------------------------------------------------------
    # 落库：market_bars（结构化，判重幂等）+ raw_items（原始切片）
    # ------------------------------------------------------------------
    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
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
                volume=None
                if volume_raw is None
                else _required_decimal(volume_raw, field="volume"),
                source_id=self.source.id,
                collected_at=collected_at,
                effective_at=payload.resolve_effective_at(collected_at),
            )
        )
        super()._persist_payload(session, payload)
        session.flush()
        return PERSIST_INSERTED

    def _instrument_for(self, session: Session, symbol: str) -> Instrument:
        instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == symbol))
        if instrument is None:
            raise CollectorError(
                f"标的 {symbol!r} 不在 instruments 白名单"
                f"（请先执行 python -m database.seeds --scope instruments）",
                details={"symbol": symbol},
            )
        return instrument


def _required_decimal(value: object, *, field: str) -> Decimal:
    """K 线字段必须可解析为 Decimal（否则抛领域异常，不静默写脏数据）。"""
    parsed = to_decimal(value)
    if parsed is None:
        raise CollectorError(
            f"SGE K 线字段 {field} 无法解析为数值：{value!r}", details={"field": field}
        )
    return parsed
