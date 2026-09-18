"""Phase 3.1 Regime 规则、因果性与缺口处理。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from database.models import AssetClass, Instrument, MarketBar, Regime, Timeframe
from src.alpha.regime import EventMark, _gap_is_expected, build_regime_run

pytestmark = [pytest.mark.unit, pytest.mark.leakage]


def _instrument() -> Instrument:
    return Instrument(
        id=uuid.uuid4(),
        symbol="XAUUSD",
        asset_class=AssetClass.METAL,
        timezone="UTC",
    )


def _bars(
    instrument: Instrument,
    *,
    count: int = 180,
    start: datetime = datetime(2026, 1, 5, tzinfo=UTC),
) -> list[MarketBar]:
    result: list[MarketBar] = []
    for index in range(count):
        open_time = start + timedelta(hours=index)
        close = Decimal("2000") + Decimal(index) / Decimal("2")
        result.append(
            MarketBar(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                timeframe=Timeframe.H1,
                open_time=open_time,
                close_time=open_time + timedelta(hours=1),
                open=close - Decimal("0.2"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("10"),
                collected_at=open_time + timedelta(hours=1),
                effective_at=open_time + timedelta(hours=1),
            )
        )
    return result


def test_trending_series_becomes_trend_up_after_causal_warmup() -> None:
    instrument = _instrument()
    run = build_regime_run(
        instrument=instrument,
        timeframe=Timeframe.H1,
        bars=_bars(instrument),
    )

    assert run.points[0].regime is Regime.UNKNOWN
    assert run.points[-1].regime is Regime.TREND_UP
    assert run.points[-1].max_effective_at <= run.points[-1].as_of


def test_future_bar_does_not_change_prior_regimes() -> None:
    instrument = _instrument()
    original = _bars(instrument)
    baseline = build_regime_run(instrument=instrument, timeframe=Timeframe.H1, bars=original)
    future = _bars(
        instrument,
        count=1,
        start=original[-1].close_time,
    )[0]
    future.close = Decimal("9999")
    future.high = Decimal("10000")
    injected = build_regime_run(
        instrument=instrument,
        timeframe=Timeframe.H1,
        bars=[*original, future],
    )

    assert [point.regime for point in injected.points[:-1]] == [
        point.regime for point in baseline.points
    ]
    assert [point.values for point in injected.points[:-1]] == [
        point.values for point in baseline.points
    ]


def test_unscheduled_gap_resets_indicator_warmup() -> None:
    instrument = _instrument()
    bars = _bars(instrument, count=100)
    later = _bars(
        instrument,
        count=5,
        start=bars[-1].close_time + timedelta(hours=2),
    )
    run = build_regime_run(
        instrument=instrument,
        timeframe=Timeframe.H1,
        bars=[*bars, *later],
    )

    assert run.points[99].regime is not Regime.UNKNOWN
    assert all(point.regime is Regime.UNKNOWN for point in run.points[100:])
    assert run.points[100].values["segment_id"] == 1


@pytest.mark.parametrize(
    ("previous_close", "current_open"),
    [
        (datetime(2025, 11, 27, 20, tzinfo=UTC), datetime(2025, 11, 27, 23, tzinfo=UTC)),
        (datetime(2025, 12, 24, 19, tzinfo=UTC), datetime(2025, 12, 25, 23, tzinfo=UTC)),
        (datetime(2026, 4, 2, 21, tzinfo=UTC), datetime(2026, 4, 5, 22, tzinfo=UTC)),
        (datetime(2026, 6, 19, 17, tzinfo=UTC), datetime(2026, 6, 21, 22, tzinfo=UTC)),
    ],
)
def test_known_gold_market_holiday_gaps_are_expected(
    previous_close: datetime, current_open: datetime
) -> None:
    assert _gap_is_expected(previous_close, current_open)


def test_nonholiday_multi_hour_gap_remains_unexpected() -> None:
    assert not _gap_is_expected(
        datetime(2026, 3, 10, 20, tzinfo=UTC),
        datetime(2026, 3, 10, 23, tzinfo=UTC),
    )


def test_news_override_uses_only_events_effective_by_as_of() -> None:
    instrument = _instrument()
    bars = _bars(instrument, count=120)
    end_at = bars[-1].close_time
    visible = [EventMark(end_at - timedelta(hours=index + 1), end_at, "news") for index in range(3)]
    hidden = EventMark(end_at, end_at + timedelta(minutes=1), "news")
    run = build_regime_run(
        instrument=instrument,
        timeframe=Timeframe.H1,
        bars=bars,
        events=[*visible, hidden],
    )

    assert run.points[-1].raw_regime is Regime.NEWS_DRIVEN
    assert run.points[-1].values["news_count_4h"] == 3


def test_cli_refuses_database_write_when_machine_gate_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.build_regimes import main

    instrument = _instrument()
    bars = _bars(instrument, count=85)

    @contextmanager
    def _session_scope():
        yield object()

    monkeypatch.setattr("scripts.build_regimes.session_scope", _session_scope)
    monkeypatch.setattr(
        "scripts.build_regimes.load_regime_inputs",
        lambda *_args, **_kwargs: (instrument, bars, []),
    )
    monkeypatch.setattr(
        "scripts.build_regimes.persist_regime_run",
        lambda *_args, **_kwargs: pytest.fail("机器门禁失败时不得写库"),
    )

    assert main(["--no-dry-run"]) == 1
    assert "REFUSED" in capsys.readouterr().err
