"""Regime 快照写入、幂等与时间链集成测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import (
    AssetClass,
    FeatureSnapshot,
    Instrument,
    MarketBar,
    MarketRegime,
    Timeframe,
)
from src.alpha.regime import build_regime_run, persist_regime_run

pytestmark = [pytest.mark.integration, pytest.mark.leakage]


def test_regime_run_persists_idempotently_with_causal_snapshot(session: Session) -> None:
    instrument = Instrument(
        symbol="XAUUSD",
        asset_class=AssetClass.METAL,
        timezone="UTC",
    )
    session.add(instrument)
    session.flush()
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bars: list[MarketBar] = []
    for index in range(85):
        open_time = start + timedelta(hours=index)
        close = Decimal("2000") + Decimal(index) / Decimal("2")
        bar = MarketBar(
            instrument_id=instrument.id,
            timeframe=Timeframe.H1,
            open_time=open_time,
            close_time=open_time + timedelta(hours=1),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=10,
            collected_at=open_time + timedelta(hours=1),
            effective_at=open_time + timedelta(hours=1),
        )
        bars.append(bar)
        session.add(bar)
    session.flush()
    run = build_regime_run(instrument=instrument, timeframe=Timeframe.H1, bars=bars)

    first = persist_regime_run(session, run)
    second = persist_regime_run(session, run)

    assert first.inserted_regimes == 85
    assert second.inserted_regimes == 0
    assert second.existing_regimes == 85
    violations = session.scalar(
        sa.select(sa.func.count())
        .select_from(MarketRegime)
        .join(FeatureSnapshot, FeatureSnapshot.id == MarketRegime.feature_snapshot_id)
        .where(FeatureSnapshot.as_of > MarketRegime.detected_at)
    )
    assert violations == 0
