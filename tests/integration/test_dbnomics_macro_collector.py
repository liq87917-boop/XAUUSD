"""DbnomicsMacroCollector 集成测试（SQLite，100% Mock）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import MacroEvent, Source
from database.models.enums import SourceType
from src.collectors.base import PERSIST_DUPLICATE, PERSIST_INSERTED
from src.collectors.dbnomics_macro import (
    UNTRUSTED_RELEASE_TIME,
    DbnomicsMacroCollector,
    parse_dbnomics_series,
)

pytestmark = pytest.mark.integration

FIXED_CLOCK = datetime(2026, 9, 20, tzinfo=UTC)


def _rows() -> list[dict]:
    return [{"period": "2026-01-01", "value": 2600.5}]


@pytest.fixture()
def dbnomics_source(session: Session, make_source) -> Source:
    return make_source(name="dbnomics_macro", source_type=SourceType.MACRO)


def _collector(source: Source) -> DbnomicsMacroCollector:
    return DbnomicsMacroCollector(
        source, fetch_series=lambda provider, series: _rows(), clock=lambda: FIXED_CLOCK
    )


def test_persist_is_idempotent(session: Session, dbnomics_source) -> None:
    collector = _collector(dbnomics_source)
    payload = collector._to_payload(parse_dbnomics_series(_rows())[0])

    assert collector._persist_payload(session, payload) == PERSIST_INSERTED
    session.flush()
    assert collector._persist_payload(session, payload) == PERSIST_DUPLICATE
    session.flush()
    assert session.scalar(sa.select(sa.func.count()).select_from(MacroEvent)) == 1


def test_persist_marks_untrusted_release_time(session: Session, dbnomics_source) -> None:
    collector = _collector(dbnomics_source)
    payload = collector._to_payload(parse_dbnomics_series(_rows())[0])
    # R3 修复：released_at 无法确定，必须标注 UNTRUSTED，禁止用于 Macro Alpha 训练
    assert payload.raw_json["release_time_provenance"] == UNTRUSTED_RELEASE_TIME
    collector._persist_payload(session, payload)
    session.flush()
    event = session.scalar(sa.select(MacroEvent))
    assert event is not None
    assert event.released_at == datetime(2026, 1, 1)  # SQLite 读回 naive
