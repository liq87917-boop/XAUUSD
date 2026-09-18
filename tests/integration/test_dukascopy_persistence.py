from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AssetClass, Instrument, MarketBar, RawItem, Source, SourceType
from scripts.backfill_dukascopy_xauusd import SOURCE_NAME, persist_hours
from src.collectors.dukascopy import DUKASCOPY_INSTRUMENT, HourCandle
from src.processors.timeline import ensure_utc_from_database

pytestmark = pytest.mark.integration


def test_persist_hours_is_idempotent_and_keeps_provenance(session: Session) -> None:
    session.add(
        Instrument(
            symbol=DUKASCOPY_INSTRUMENT,
            name="Dukascopy spot gold",
            asset_class=AssetClass.METAL,
            quote_currency="USD",
            timezone="UTC",
        )
    )
    session.add(
        Source(
            name=SOURCE_NAME,
            source_type=SourceType.MARKET,
            base_url="https://datafeed.dukascopy.com/datafeed",
            timezone="UTC",
            enabled=False,
            config_json={"collector": "dukascopy_xauusd_backfill"},
        )
    )
    session.flush()
    start = datetime(2025, 1, 2, tzinfo=UTC)
    bar = HourCandle(
        open_time=start,
        close_time=start + timedelta(hours=1),
        open=Decimal("2600"),
        high=Decimal("2602"),
        low=Decimal("2599"),
        close=Decimal("2601"),
        volume=Decimal("42"),
        source_sha256="a" * 64,
    )
    collected_at = datetime(2026, 9, 18, tzinfo=UTC)

    assert persist_hours(session, [bar], collected_at=collected_at) == (1, 0)
    assert persist_hours(session, [bar], collected_at=collected_at) == (0, 1)
    stored = session.scalar(sa.select(MarketBar))
    raw = session.scalar(sa.select(RawItem))
    assert stored is not None and stored.source_id is not None
    assert raw is not None
    assert raw.raw_json["source_sha256"] == "a" * 64
    assert (
        ensure_utc_from_database(raw.effective_at, field_name="raw_items.effective_at")
        == collected_at
    )
