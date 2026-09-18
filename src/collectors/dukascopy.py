"""Dukascopy XAUUSD BI5 历史分钟 K 线解析与严格小时聚合。"""

from __future__ import annotations

import hashlib
import lzma
import struct
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

DUKASCOPY_BASE_URL: Final[str] = "https://datafeed.dukascopy.com/datafeed"
DUKASCOPY_INSTRUMENT: Final[str] = "XAUUSD_DUKASCOPY"
DUKASCOPY_PROVIDER_SYMBOL: Final[str] = "XAUUSD"
PRICE_SCALE: Final[Decimal] = Decimal("1000")
_RECORD = struct.Struct(">5if")


@dataclass(frozen=True, slots=True)
class MinuteCandle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class HourCandle:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_sha256: str


def day_url(day: date) -> str:
    """返回官方日文件 URL；Dukascopy 的月份目录从 00 开始。"""
    return (
        f"{DUKASCOPY_BASE_URL}/{DUKASCOPY_PROVIDER_SYMBOL}/{day.year}/"
        f"{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"
    )


def parse_minute_candles(payload: bytes, *, day: date) -> tuple[MinuteCandle, ...]:
    """解压并解析一天的 BI5；格式异常立即失败，不吞掉坏记录。"""
    if not payload:
        return ()
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise ValueError(f"Dukascopy BI5 解压失败：{day.isoformat()}") from exc
    if len(raw) % _RECORD.size:
        raise ValueError(f"Dukascopy BI5 记录长度异常：{day.isoformat()} size={len(raw)}")

    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    output: list[MinuteCandle] = []
    previous_second = -1
    for offset in range(0, len(raw), _RECORD.size):
        second, open_i, close_i, low_i, high_i, volume_f = _RECORD.unpack_from(raw, offset)
        if second % 60 or not 0 <= second < 86_400 or second <= previous_second:
            raise ValueError(f"Dukascopy 分钟时间异常：{day.isoformat()} second={second}")
        if min(open_i, close_i, low_i, high_i) <= 0:
            raise ValueError(f"Dukascopy 非正价格：{day.isoformat()} second={second}")
        if high_i < max(open_i, close_i) or low_i > min(open_i, close_i):
            raise ValueError(f"Dukascopy OHLC 边界异常：{day.isoformat()} second={second}")
        previous_second = second
        output.append(
            MinuteCandle(
                open_time=midnight + timedelta(seconds=second),
                open=Decimal(open_i) / PRICE_SCALE,
                high=Decimal(high_i) / PRICE_SCALE,
                low=Decimal(low_i) / PRICE_SCALE,
                close=Decimal(close_i) / PRICE_SCALE,
                volume=Decimal(str(volume_f)),
            )
        )
    return tuple(output)


def aggregate_complete_hours(
    minutes: tuple[MinuteCandle, ...], *, source_sha256: str
) -> tuple[HourCandle, ...]:
    """只输出恰好含 60 个连续分钟的小时桶，禁止插值和部分桶。"""
    buckets: dict[datetime, list[MinuteCandle]] = {}
    for item in minutes:
        hour = item.open_time.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(item)

    output: list[HourCandle] = []
    for hour, rows in sorted(buckets.items()):
        expected = [hour + timedelta(minutes=index) for index in range(60)]
        if len(rows) != 60 or [item.open_time for item in rows] != expected:
            continue
        volume = sum((item.volume for item in rows), start=Decimal("0"))
        # Dukascopy 在休市时仍可能返回全天平价、零成交/报价量的占位分钟线。
        # 这不是可交易行情；若保留会把周末伪造成连续市场。
        if volume <= 0:
            continue
        output.append(
            HourCandle(
                open_time=hour,
                close_time=hour + timedelta(hours=1),
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
                volume=volume,
                source_sha256=source_sha256,
            )
        )
    return tuple(output)


def parse_and_aggregate(payload: bytes, *, day: date) -> tuple[HourCandle, ...]:
    digest = hashlib.sha256(payload).hexdigest()
    return aggregate_complete_hours(parse_minute_candles(payload, day=day), source_sha256=digest)
