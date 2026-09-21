"""数据质量统一校验层单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.collectors.validation import validate_market_bar


def _valid_bar(**overrides) -> dict:
    base = {
        "open_time": datetime(2026, 1, 5, tzinfo=UTC),
        "close_time": datetime(2026, 1, 6, tzinfo=UTC),
        "open_": Decimal("500"),
        "high": Decimal("510"),
        "low": Decimal("490"),
        "close": Decimal("505"),
        "volume": Decimal("1000"),
    }
    base.update(overrides)
    return base


def test_valid_bar_has_no_issues() -> None:
    assert validate_market_bar(**_valid_bar()) == ()


def test_missing_ohlc_flagged() -> None:
    issues = validate_market_bar(**_valid_bar(close=None))
    assert any(issue.field == "ohlc" for issue in issues)


def test_high_below_close_flagged() -> None:
    # close < low 且 high < close（SGE 结算价低于最低成交价）
    issues = validate_market_bar(
        **_valid_bar(
            open_=Decimal("276.2"),
            high=Decimal("276.69"),
            low=Decimal("272.95"),
            close=Decimal("272.83"),
        )
    )
    assert any(issue.field == "low" for issue in issues)


def test_non_positive_price_flagged() -> None:
    issues = validate_market_bar(**_valid_bar(close=Decimal("0")))
    assert any(issue.field == "price" for issue in issues)


def test_negative_volume_flagged() -> None:
    issues = validate_market_bar(**_valid_bar(volume=Decimal("-1")))
    assert any(issue.field == "volume" for issue in issues)


def test_close_time_not_after_open_flagged() -> None:
    issues = validate_market_bar(
        **_valid_bar(
            open_time=datetime(2026, 1, 6, tzinfo=UTC), close_time=datetime(2026, 1, 5, tzinfo=UTC)
        )
    )
    assert any(issue.field == "timestamp" for issue in issues)
