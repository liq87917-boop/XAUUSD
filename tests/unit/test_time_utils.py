"""时间语义工具测试（06_Cline开发规则 第 7 条 / 02 §5.4）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.common.exceptions import TimeSemanticsError
from src.common.time import (
    is_future,
    parse_iso8601,
    require_aware,
    resolve_effective_at,
    to_naive_utc,
    to_utc,
    utc_now,
)

pytestmark = pytest.mark.unit

SHANGHAI = timezone(timedelta(hours=8))


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_require_aware_rejects_naive() -> None:
    with pytest.raises(TimeSemanticsError):
        require_aware(datetime(2026, 1, 1))
    assert require_aware(datetime(2026, 1, 1, tzinfo=UTC)).tzinfo is UTC


def test_require_aware_rejects_non_datetime() -> None:
    with pytest.raises(TimeSemanticsError):
        require_aware("2026-01-01")  # type: ignore[arg-type]


def test_to_utc_converts_offset_to_utc() -> None:
    moment = datetime(2026, 9, 12, 16, 0, tzinfo=SHANGHAI)
    assert to_utc(moment) == datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


def test_to_utc_naive_requires_explicit_assumption() -> None:
    with pytest.raises(TimeSemanticsError):
        to_utc(datetime(2026, 1, 1), assume_tz=None)


def test_parse_iso8601_supports_z_and_offset() -> None:
    expected = datetime(2026, 9, 12, 8, 30, tzinfo=UTC)
    assert parse_iso8601("2026-09-12T08:30:00Z") == expected
    assert parse_iso8601("2026-09-12T16:30:00+08:00") == expected


def test_parse_iso8601_rejects_empty_and_invalid() -> None:
    with pytest.raises(TimeSemanticsError):
        parse_iso8601("   ")
    with pytest.raises(TimeSemanticsError):
        parse_iso8601("2026-13-45T99:99:99Z")


def test_resolve_effective_at_falls_back_to_collected_at() -> None:
    """发布时间未知时，最早可用时间只能是采集时间（不得用"现在"兜底）。"""
    collected = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    assert resolve_effective_at(published_at=None, collected_at=collected) == collected


def test_resolve_effective_at_never_before_collected_at() -> None:
    """发布早于采集：系统实际在采集时才拿到数据。"""
    published = datetime(2026, 9, 12, 7, 0, tzinfo=UTC)
    collected = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    assert resolve_effective_at(published_at=published, collected_at=collected) == collected


def test_resolve_effective_at_takes_later_published_at() -> None:
    """发布时间晚于采集时间（跨时区错误/预发布）：保守取较晚者，宁可推迟可用。"""
    published = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    collected = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    assert resolve_effective_at(published_at=published, collected_at=collected) == published


def test_resolve_effective_at_requires_aware_inputs() -> None:
    with pytest.raises(TimeSemanticsError):
        resolve_effective_at(published_at=None, collected_at=datetime(2026, 1, 1))
    with pytest.raises(TimeSemanticsError):
        resolve_effective_at(
            published_at=datetime(2026, 1, 1), collected_at=datetime(2026, 1, 2, tzinfo=UTC)
        )


def test_is_future_with_tolerance() -> None:
    now = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    assert is_future(now + timedelta(minutes=1), now=now) is False
    assert is_future(now + timedelta(minutes=30), now=now) is True


def test_to_naive_utc_drops_timezone_after_conversion() -> None:
    assert to_naive_utc(datetime(2026, 9, 12, 16, 0, tzinfo=SHANGHAI)) == datetime(
        2026, 9, 12, 8, 0
    )
