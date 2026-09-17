"""Phase 3.0 W0-2：宏观 vintage 的未来数据泄漏守卫。"""

# ruff: noqa: I001 -- ruff 0.16.7 在此文件反复建议同一无变化的 import 排序结果。

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import MacroEvent
from src.common.exceptions import ImmutableRecordError
from src.processors.macro_vintages import macro_events_as_of

pytestmark = pytest.mark.leakage

OBSERVATION_AT = datetime(2026, 6, 1, tzinfo=UTC)
FIRST_RELEASE = datetime(2026, 7, 16, tzinfo=UTC)
REVISION_RELEASE = datetime(2026, 8, 16, tzinfo=UTC)


def _event(
    source_id: object,
    *,
    released_at: datetime,
    vintage_end_at: datetime | None,
    value: str,
) -> MacroEvent:
    return MacroEvent(
        source_id=source_id,
        event_code="CPIAUCSL",
        country="US",
        event_at=OBSERVATION_AT,
        released_at=released_at,
        vintage_end_at=vintage_end_at,
        actual_value=Decimal(value),
        unit="index",
        collected_at=released_at,
        effective_at=released_at,
    )


def test_release_is_invisible_before_boundary_and_visible_after(
    session: Session, make_source
) -> None:
    source = make_source()
    session.add(
        _event(
            source.id,
            released_at=FIRST_RELEASE,
            vintage_end_at=None,
            value="318.1",
        )
    )
    session.flush()

    assert macro_events_as_of(session, as_of=FIRST_RELEASE - timedelta(seconds=1)) == []
    visible = macro_events_as_of(session, as_of=FIRST_RELEASE)
    assert len(visible) == 1
    assert visible[0].actual_value == Decimal("318.1000000000")


def test_as_of_switches_to_revision_without_overwriting_history(
    session: Session, make_source
) -> None:
    source = make_source()
    first = _event(
        source.id,
        released_at=FIRST_RELEASE,
        vintage_end_at=REVISION_RELEASE,
        value="318.1",
    )
    revision = _event(
        source.id,
        released_at=REVISION_RELEASE,
        vintage_end_at=None,
        value="318.4",
    )
    session.add_all([first, revision])
    session.flush()

    before = macro_events_as_of(session, as_of=REVISION_RELEASE - timedelta(seconds=1))
    after = macro_events_as_of(session, as_of=REVISION_RELEASE)
    assert [event.id for event in before] == [first.id]
    assert [event.id for event in after] == [revision.id]

    first.actual_value = Decimal("999")
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_database_rejects_invalid_release_window(session: Session, make_source) -> None:
    source = make_source()
    session.add(
        _event(
            source.id,
            released_at=FIRST_RELEASE,
            vintage_end_at=FIRST_RELEASE,
            value="318.1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_database_rejects_effective_time_before_release(session: Session, make_source) -> None:
    source = make_source()
    event = _event(
        source.id,
        released_at=FIRST_RELEASE,
        vintage_end_at=None,
        value="318.1",
    )
    event.effective_at = FIRST_RELEASE - timedelta(seconds=1)
    session.add(event)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()