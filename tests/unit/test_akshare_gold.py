"""AkshareGoldCollector 解析纯函数单元测试（无数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.collectors.akshare_gold import parse_sge_bars, to_decimal
from src.collectors.errors import CollectorError


def _row(
    date: str, *, open: float = 500.0, high: float = 510.0, low: float = 490.0, close: float = 505.0
) -> dict:
    return {"date": date, "open": open, "high": high, "low": low, "close": close}


def test_parse_sge_bars_normal() -> None:
    rows = [
        _row("2026-01-05"),
        {"date": "2026-01-06", "open": 505, "high": 515, "low": 500, "close": 510},  # 无 volume
    ]
    points = parse_sge_bars(rows)
    assert len(points) == 2
    assert points[0].open_time == datetime(2026, 1, 5, tzinfo=UTC)
    assert points[0].close_time == datetime(2026, 1, 6, tzinfo=UTC)
    assert points[0].close == Decimal("505")
    assert points[1].volume is None


def test_parse_sge_bars_volume_parsed() -> None:
    points = parse_sge_bars([{**_row("2026-01-05"), "volume": 1234}])
    assert points[0].volume == Decimal("1234")


def test_parse_sge_bars_empty() -> None:
    assert parse_sge_bars([]) == ()


def test_parse_sge_bars_missing_field_raises() -> None:
    with pytest.raises(CollectorError, match="缺少字段"):
        parse_sge_bars([{"date": "2026-01-05", "open": 500}])  # 缺 high/low/close


def test_parse_sge_bars_invalid_date_raises() -> None:
    with pytest.raises(CollectorError, match="无法解析"):
        parse_sge_bars([_row("not-a-date")])


def test_parse_sge_bars_invalid_price_raises() -> None:
    with pytest.raises(CollectorError, match="无法解析为数值"):
        parse_sge_bars([_row("2026-01-05", open="abc")])


def test_to_decimal_returns_none_on_garbage() -> None:
    assert to_decimal("abc") is None
    assert to_decimal(None) is None
    assert to_decimal(505.5) == Decimal("505.50000000")


def test_parse_sge_bars_accepts_date_object() -> None:
    """akshare 真实返回的 date 列是 datetime.date（纯日期），不是 str。"""
    from datetime import date

    rows = [{"date": date(2026, 1, 5), "open": 500, "high": 510, "low": 490, "close": 505}]
    points = parse_sge_bars(rows)
    assert points[0].open_time == datetime(2026, 1, 5, tzinfo=UTC)
