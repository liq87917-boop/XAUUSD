"""DbnomicsMacroCollector 集成测试（SQLite，100% Mock）。"""

from __future__ import annotations

import asyncio
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
from src.collectors.errors import CollectorError
from src.collectors.transport import HttpResponse
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.integration

FIXED_CLOCK = datetime(2026, 9, 20, tzinfo=UTC)
WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 21, tzinfo=UTC)
)


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


def test_rest_fallback_used_when_library_fails(dbnomics_source, mock_transport) -> None:
    """library 失败 → REST fallback 用 MockTransport 返回数据，transport 标记 rest。"""

    def _raise(provider: str, series: str):
        raise RuntimeError("library down")

    collector = DbnomicsMacroCollector(
        dbnomics_source,
        fetch_series=_raise,
        transport=mock_transport(
            [
                HttpResponse(
                    200,
                    json_body={
                        "observations": {
                            "docs": [
                                {
                                    "period": "2026-01-01",
                                    "value": 2600.5,
                                    "series_code": "A.US.PCPI_IX",
                                }
                            ]
                        }
                    },
                )
            ]
        ),
        clock=lambda: FIXED_CLOCK,
    )
    page = asyncio.run(collector._do_fetch(None, WINDOW))
    assert collector.transport_used == "rest"
    assert len(page.payloads) == 1


def test_rest_fallback_404_not_retried(dbnomics_source, mock_transport) -> None:
    """library 失败 + REST 404（permanent）→ 不重试，直接抛 CollectorError。"""

    def _raise(provider: str, series: str):
        raise RuntimeError("library down")

    collector = DbnomicsMacroCollector(
        dbnomics_source,
        fetch_series=_raise,
        transport=mock_transport([HttpResponse(404, json_body={"message": "not found"})]),
        clock=lambda: FIXED_CLOCK,
    )
    with pytest.raises(CollectorError, match="HTTP 404"):
        asyncio.run(collector._do_fetch(None, WINDOW))
