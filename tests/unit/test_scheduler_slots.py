"""确定性 UTC 调度槽测试（纯函数；零数据库、零网络）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.common.exceptions import TimeSemanticsError
from src.scheduler.slots import (
    DEFAULT_INTERVAL_MINUTES,
    ScheduleSlot,
    floor_to_interval,
    resolve_slot,
    seconds_until_next_slot,
)

pytestmark = pytest.mark.unit


def test_default_interval_is_thirty_minutes() -> None:
    assert DEFAULT_INTERVAL_MINUTES == 30


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 22, 12, 0, tzinfo=UTC), datetime(2026, 9, 22, 12, 0, tzinfo=UTC)),
        (datetime(2026, 9, 22, 12, 7, 30, tzinfo=UTC), datetime(2026, 9, 22, 12, 0, tzinfo=UTC)),
        (
            datetime(2026, 9, 22, 12, 29, 59, 999999, tzinfo=UTC),
            datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        ),
        (datetime(2026, 9, 22, 12, 30, tzinfo=UTC), datetime(2026, 9, 22, 12, 30, tzinfo=UTC)),
        (datetime(2026, 9, 22, 23, 59, 59, tzinfo=UTC), datetime(2026, 9, 22, 23, 30, tzinfo=UTC)),
        (datetime(2026, 9, 23, 0, 0, tzinfo=UTC), datetime(2026, 9, 23, 0, 0, tzinfo=UTC)),
    ],
)
def test_floor_to_interval_aligns_to_utc_grid(moment: datetime, expected: datetime) -> None:
    assert floor_to_interval(moment, 30) == expected


def test_floor_to_interval_normalises_other_timezones() -> None:
    shanghai_1210_utc = datetime(2026, 9, 22, 20, 10, tzinfo=timezone(timedelta(hours=8)))
    assert floor_to_interval(shanghai_1210_utc, 30) == datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def test_floor_to_interval_rejects_naive_datetime() -> None:
    with pytest.raises(TimeSemanticsError):
        floor_to_interval(datetime(2026, 9, 22, 12, 10))


@pytest.mark.parametrize("interval", [0, -30, 7, 1441])
def test_invalid_interval_is_rejected(interval: int) -> None:
    with pytest.raises(TimeSemanticsError):
        floor_to_interval(datetime(2026, 9, 22, 12, 0, tzinfo=UTC), interval)


def test_resolve_slot_is_stable_within_slot() -> None:
    early = resolve_slot(datetime(2026, 9, 22, 12, 0, 1, tzinfo=UTC), 30)
    late = resolve_slot(datetime(2026, 9, 22, 12, 29, 59, tzinfo=UTC), 30)

    assert early == late
    assert early.start_at == datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    assert early.end_at == datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
    assert early.idempotency_key == "collector_scheduler:30m:20260922T120000Z"
    assert late.idempotency_key == early.idempotency_key


def test_idempotency_key_differs_across_slots() -> None:
    first = resolve_slot(datetime(2026, 9, 22, 12, 29, 59, tzinfo=UTC), 30)
    second = resolve_slot(datetime(2026, 9, 22, 12, 30, tzinfo=UTC), 30)
    assert first.idempotency_key != second.idempotency_key


def test_schedule_slot_rejects_unaligned_start() -> None:
    with pytest.raises(TimeSemanticsError):
        ScheduleSlot(interval_minutes=30, start_at=datetime(2026, 9, 22, 12, 5, tzinfo=UTC))


def test_schedule_slot_rejects_naive_start() -> None:
    with pytest.raises(TimeSemanticsError):
        ScheduleSlot(interval_minutes=30, start_at=datetime(2026, 9, 22, 12, 0))


def test_schedule_slot_to_dict_exposes_only_whitelist_fields() -> None:
    slot = resolve_slot(datetime(2026, 9, 22, 12, 0, tzinfo=UTC), 30)
    payload = slot.to_dict()
    assert set(payload) == {"interval_minutes", "start_at", "end_at", "idempotency_key"}
    assert payload["interval_minutes"] == 30
    assert payload["idempotency_key"] == slot.idempotency_key


def test_seconds_until_next_slot() -> None:
    assert seconds_until_next_slot(datetime(2026, 9, 22, 12, 0, tzinfo=UTC), 30) == 1800.0
    assert seconds_until_next_slot(datetime(2026, 9, 22, 12, 7, 30, tzinfo=UTC), 30) == 1350.0
    assert seconds_until_next_slot(datetime(2026, 9, 22, 12, 29, 59, tzinfo=UTC), 30) == 1.0
