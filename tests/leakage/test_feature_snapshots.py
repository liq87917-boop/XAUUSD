"""W0-5 特征快照的时间因果与回放验收。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AssetClass, FeatureSnapshot, Instrument, MarketBar, Timeframe
from src.common.exceptions import ImmutableRecordError
from src.features.market import (
    FeatureWindowGapError,
    compute_market_feature_candidate,
    persist_market_feature_candidate,
    replay_market_feature_snapshot,
)

pytestmark = [pytest.mark.integration, pytest.mark.leakage]


def _instrument(session: Session) -> Instrument:
    item = Instrument(
        symbol="XAUUSD",
        name="Gold",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    )
    session.add(item)
    session.flush()
    return item


def _bar(
    session: Session,
    instrument: Instrument,
    *,
    open_time: datetime,
    close: int,
    effective_at: datetime | None = None,
) -> MarketBar:
    close_time = open_time + timedelta(hours=4)
    item = MarketBar(
        instrument_id=instrument.id,
        timeframe=Timeframe.H4,
        open_time=open_time,
        close_time=close_time,
        open=Decimal(close - 1),
        high=Decimal(close + 2),
        low=Decimal(close - 2),
        close=Decimal(close),
        volume=Decimal("10"),
        collected_at=effective_at or close_time,
        effective_at=effective_at or close_time,
    )
    session.add(item)
    session.flush()
    return item


def test_future_effective_bar_is_excluded_and_snapshot_replays(session: Session) -> None:
    instrument = _instrument(session)
    start = datetime(2026, 9, 15, tzinfo=UTC)
    visible = [
        _bar(session, instrument, open_time=start + timedelta(hours=4 * index), close=100 + index)
        for index in range(5)
    ]
    as_of = datetime(2026, 9, 18, tzinfo=UTC)
    future = _bar(
        session,
        instrument,
        open_time=start + timedelta(hours=20),
        close=999,
        effective_at=as_of + timedelta(seconds=1),
    )

    candidate = compute_market_feature_candidate(
        session, instrument_symbol="XAUUSD", timeframe=Timeframe.H4, as_of=as_of
    )

    assert candidate.input_bar_ids == tuple(item.id for item in visible)
    assert future.id not in candidate.input_bar_ids
    assert candidate.max_effective_at <= candidate.as_of
    assert candidate.values["close"] == 104.0

    first = persist_market_feature_candidate(session, candidate)
    second = persist_market_feature_candidate(session, candidate)
    replayed = replay_market_feature_snapshot(session, first.snapshot_id)

    assert first.inserted is True
    assert second.inserted is False
    assert second.snapshot_id == first.snapshot_id
    assert replayed.data_hash == candidate.data_hash
    assert replayed.values == candidate.values


def test_gap_window_is_rejected_instead_of_interpolated(session: Session) -> None:
    instrument = _instrument(session)
    start = datetime(2026, 9, 15, tzinfo=UTC)
    for index in range(5):
        offset = index * 4 + (4 if index >= 2 else 0)
        _bar(session, instrument, open_time=start + timedelta(hours=offset), close=100 + index)

    with pytest.raises(FeatureWindowGapError, match="禁止跨缺口"):
        compute_market_feature_candidate(
            session,
            instrument_symbol="XAUUSD",
            timeframe=Timeframe.H4,
            as_of=datetime(2026, 9, 18, tzinfo=UTC),
        )


def test_feature_snapshot_is_append_only(session: Session) -> None:
    instrument = _instrument(session)
    start = datetime(2026, 9, 15, tzinfo=UTC)
    for index in range(5):
        _bar(session, instrument, open_time=start + timedelta(hours=4 * index), close=100 + index)
    candidate = compute_market_feature_candidate(
        session,
        instrument_symbol="XAUUSD",
        timeframe=Timeframe.H4,
        as_of=datetime(2026, 9, 18, tzinfo=UTC),
    )
    result = persist_market_feature_candidate(session, candidate)
    snapshot = session.get(FeatureSnapshot, result.snapshot_id)
    assert snapshot is not None
    snapshot.values_json = {"close": 1.0}

    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_database_rejects_max_effective_after_as_of(session: Session) -> None:
    instrument = _instrument(session)
    start = datetime(2026, 9, 15, tzinfo=UTC)
    for index in range(5):
        _bar(session, instrument, open_time=start + timedelta(hours=4 * index), close=100 + index)
    candidate = compute_market_feature_candidate(
        session,
        instrument_symbol="XAUUSD",
        timeframe=Timeframe.H4,
        as_of=datetime(2026, 9, 18, tzinfo=UTC),
    )
    result = persist_market_feature_candidate(session, candidate)
    source = session.get(FeatureSnapshot, result.snapshot_id)
    assert source is not None
    session.expunge(source)
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.update(FeatureSnapshot)
            .where(FeatureSnapshot.id == result.snapshot_id)
            .values(max_effective_at=candidate.as_of + timedelta(seconds=1))
        )
