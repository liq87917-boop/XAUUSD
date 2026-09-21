"""AkshareGoldCollector 集成测试（SQLite，100% Mock，不访问外部网络）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import Instrument, MarketBar, Source
from database.models.enums import SourceType
from database.seeds import seed_instruments
from src.collectors.akshare_gold import AkshareGoldCollector, parse_sge_bars
from src.collectors.base import PERSIST_DUPLICATE, PERSIST_INSERTED
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.integration

FIXED_CLOCK = datetime(2026, 1, 10, tzinfo=UTC)


def _rows() -> list[dict]:
    return [
        {"date": "2026-01-05", "open": 500, "high": 510, "low": 490, "close": 505, "volume": 1000}
    ]


@pytest.fixture()
def sge_source(session: Session, make_source) -> Source:
    return make_source(name="akshare_sge", source_type=SourceType.MARKET)


@pytest.fixture()
def sge_instrument(session: Session) -> Instrument:
    seed_instruments(session)
    session.flush()
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == "SGE_AU9999"))
    assert instrument is not None
    return instrument


def _collector(source: Source) -> AkshareGoldCollector:
    return AkshareGoldCollector(source, fetch_sge=lambda symbol: _rows(), clock=lambda: FIXED_CLOCK)


def test_persist_is_idempotent(session: Session, sge_source, sge_instrument) -> None:
    collector = _collector(sge_source)
    payload = collector._to_payload(parse_sge_bars(_rows())[0])

    assert collector._persist_payload(session, payload) == PERSIST_INSERTED
    session.flush()
    # 同一 (instrument_id, timeframe, open_time) 重复插入不重复
    assert collector._persist_payload(session, payload) == PERSIST_DUPLICATE
    session.flush()
    assert session.scalar(sa.select(sa.func.count()).select_from(MarketBar)) == 1


def test_persist_effective_at_uses_max_of_close_and_collected(
    session: Session, sge_source, sge_instrument
) -> None:
    collector = _collector(sge_source)
    payload = collector._to_payload(parse_sge_bars(_rows())[0])
    collector._persist_payload(session, payload)
    session.flush()
    bar = session.scalar(sa.select(MarketBar))
    assert bar is not None
    # close_time = 2026-01-06，collected_at = 2026-01-10 → effective_at = 2026-01-10
    # SQLite 读回 naive，故与 FIXED_CLOCK 的 naive 表示比较
    assert bar.effective_at == FIXED_CLOCK.replace(tzinfo=None)


def test_do_fetch_returns_page(sge_source, sge_instrument) -> None:
    collector = _collector(sge_source)
    window = CollectWindow(
        start_at=datetime(2026, 1, 1, tzinfo=UTC), end_at=datetime(2026, 1, 10, tzinfo=UTC)
    )
    page = asyncio.run(collector._do_fetch(None, window))
    assert page.raw_count == 1
    assert len(page.payloads) == 1
    assert page.next_cursor is None
