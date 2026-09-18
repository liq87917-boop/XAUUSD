"""可解释、严格因果的 Phase 3.1 Market Regime 引擎。"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import (
    DataVersion,
    FeatureSet,
    FeatureSetKind,
    FeatureSnapshot,
    FeatureValue,
    Instrument,
    MacroEvent,
    MarketBar,
    MarketRegime,
    NewsEvent,
    Regime,
    Timeframe,
)
from src.common.time import utc_now
from src.common.uuid7 import uuid7

REGIME_MODEL_VERSION: Final[str] = "regime-rules-stat-v1"
REGIME_FEATURE_SET_NAME: Final[str] = "regime-core"
REGIME_FEATURE_SET_VERSION: Final[str] = "1.0.0"
SESSION_TZ: Final[ZoneInfo] = ZoneInfo("America/New_York")
MIN_HOLD_BARS: Final[int] = 12
CONFIRM_BARS: Final[int] = 3
EVENT_WINDOW_HOURS: Final[int] = 4
NEWS_COUNT_THRESHOLD: Final[int] = 3
MACRO_COUNT_THRESHOLD: Final[int] = 1

REGIME_FEATURE_DEFINITION: Final[dict[str, Any]] = {
    "model_version": REGIME_MODEL_VERSION,
    "timeframe": "1h",
    "features": [
        "ema_20",
        "ema_60",
        "ema_20_slope_4",
        "ema_60_slope_4",
        "adx_14",
        "atr_14_pct",
        "atr_pct_rank_60",
        "news_count_4h",
        "macro_count_4h",
    ],
    "thresholds": {
        "adx_trend": 25.0,
        "adx_range": 20.0,
        "vol_high_rank": 0.8,
        "vol_low_rank": 0.2,
        "news_count_4h": NEWS_COUNT_THRESHOLD,
        "macro_count_4h": MACRO_COUNT_THRESHOLD,
        "min_hold_bars": MIN_HOLD_BARS,
        "confirm_bars": CONFIRM_BARS,
    },
    "priority": [
        "UNKNOWN",
        "NEWS_DRIVEN",
        "HIGH_VOLATILITY",
        "TREND_UP/TREND_DOWN",
        "LOW_VOLATILITY/RANGE",
    ],
    "gap_policy": "reset_on_unscheduled_gap; preserve_CME_weekend_and_daily_settlement",
    "statistics": "causal rolling/expanding only; no full-sample normalization",
}


@dataclass(frozen=True, slots=True)
class EventMark:
    occurred_at: datetime
    effective_at: datetime
    kind: str


@dataclass(frozen=True, slots=True)
class RegimePoint:
    bar_id: uuid.UUID
    instrument_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    as_of: datetime
    max_effective_at: datetime
    input_start_at: datetime
    input_count: int
    input_hash: str
    values: dict[str, float | int | str]
    raw_regime: Regime
    statistical_regime: Regime
    regime: Regime
    confidence: float


@dataclass(frozen=True, slots=True)
class RegimeRun:
    instrument_id: uuid.UUID
    instrument_symbol: str
    timeframe: Timeframe
    points: tuple[RegimePoint, ...]

    @property
    def mature_points(self) -> tuple[RegimePoint, ...]:
        return tuple(point for point in self.points if point.regime is not Regime.UNKNOWN)


@dataclass(frozen=True, slots=True)
class RegimeWriteResult:
    inserted_snapshots: int
    inserted_regimes: int
    existing_regimes: int


def regime_metrics(run: RegimeRun) -> dict[str, Any]:
    """计算 Phase 3.1 机器验收指标（不含人工盲评）。"""
    points = run.points
    total = len(points)
    unknown = sum(point.regime is Regime.UNKNOWN for point in points)
    raw_mature = [point for point in points if point.raw_regime is not Regime.UNKNOWN]
    agreement = (
        sum(point.raw_regime is point.statistical_regime for point in raw_mature) / len(raw_mature)
        if raw_mature
        else 0.0
    )

    durations: list[int] = []
    switches_by_day: dict[str, int] = {}
    active: Regime | None = None
    active_segment: int | None = None
    duration = 0
    for point in points:
        segment_id = int(point.values["segment_id"])
        if point.regime is Regime.UNKNOWN:
            if duration:
                durations.append(duration)
            active, active_segment, duration = None, None, 0
            continue
        if point.regime is active and segment_id == active_segment:
            duration += 1
            continue
        if duration:
            durations.append(duration)
            day = point.start_at.date().isoformat()
            switches_by_day[day] = switches_by_day.get(day, 0) + 1
        active, active_segment, duration = point.regime, segment_id, 1
    if duration:
        durations.append(duration)

    unknown_ratio = unknown / total if total else 1.0
    average_duration = sum(durations) / len(durations) if durations else 0.0
    max_daily_switches = max(switches_by_day.values(), default=0)
    causal_violations = sum(point.max_effective_at > point.as_of for point in points)
    gates = {
        "unknown_lte_1pct": unknown_ratio <= 0.01,
        "average_duration_gte_12": average_duration >= 12.0,
        "daily_switches_lte_6": max_daily_switches <= 6,
        "causal_violations_zero": causal_violations == 0,
        "rule_stat_agreement_gte_80pct": agreement >= 0.8,
    }
    distribution: dict[str, int] = {item.value: 0 for item in Regime}
    for point in points:
        distribution[point.regime.value] += 1
    return {
        "bars": total,
        "distribution": distribution,
        "unknown_ratio": unknown_ratio,
        "average_duration_bars": average_duration,
        "max_daily_switches": max_daily_switches,
        "rule_stat_agreement": agreement,
        "causal_violations": causal_violations,
        "segments": len({int(point.values["segment_id"]) for point in points}),
        "gates": gates,
        "machine_pass": all(gates.values()),
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scheduled_closed(moment: datetime) -> bool:
    """CME 贵金属常规休市：周末 + 每日 17:00–18:00 美东结算间隙。"""
    local = _utc(moment).astimezone(SESSION_TZ)
    weekday, hour = local.weekday(), local.hour
    if _holiday_closed(local):
        return True
    if weekday == 5:
        return True
    if weekday == 4 and hour >= 17:
        return True
    if weekday == 6 and hour < 18:
        return True
    return weekday <= 3 and hour == 17


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    """Meeus/Jones/Butcher Gregorian Easter algorithm (stdlib-only)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _holiday_closed(local: datetime) -> bool:
    """黄金现货/COMEX 共同的已知美国节假日休市窗口（美东时间）。"""
    current, hour, year = local.date(), local.hour, local.year
    good_friday = _easter_sunday(year) - timedelta(days=2)
    if current == good_friday - timedelta(days=1) and hour >= 17:
        return True
    if current == good_friday:
        return True

    if (current == date(year, 12, 24) and hour >= 14) or (
        current == date(year, 12, 25) and hour < 18
    ):
        return True
    if (current == date(year, 12, 31) and hour >= 17) or (
        current == date(year, 1, 1) and hour < 18
    ):
        return True

    monday_holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _nth_weekday(year, 9, 0, 1),
    }
    if current in monday_holidays and 15 <= hour < 18:
        return True

    special = {_observed(date(year, 6, 19)), _observed(date(year, 7, 4))}
    if current in special and (hour >= 13 if current.weekday() == 4 else 15 <= hour < 18):
        return True

    thanksgiving = _nth_weekday(year, 11, 3, 4)
    if current == thanksgiving and 15 <= hour < 18:
        return True
    return current == thanksgiving + timedelta(days=1) and hour >= 15


def _gap_is_expected(previous_close: datetime, current_open: datetime) -> bool:
    cursor = _utc(previous_close)
    end = _utc(current_open)
    if cursor == end:
        return True
    if cursor > end:
        return False
    while cursor < end:
        if not _scheduled_closed(cursor):
            return False
        cursor += timedelta(hours=1)
    return True


def _bar_token(bar: MarketBar) -> str:
    payload = {
        "id": str(bar.id),
        "open_time": _utc(bar.open_time).isoformat(timespec="microseconds"),
        "close_time": _utc(bar.close_time).isoformat(timespec="microseconds"),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": None if bar.volume is None else str(bar.volume),
        "effective_at": _utc(bar.effective_at).isoformat(timespec="microseconds"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _indicator_frame(bars: list[MarketBar]) -> pd.DataFrame:
    data = pd.DataFrame(
        {
            "high": [float(str(bar.high)) for bar in bars],
            "low": [float(str(bar.low)) for bar in bars],
            "close": [float(str(bar.close)) for bar in bars],
        }
    )
    close = data["close"]
    data["ema_20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    data["ema_60"] = close.ewm(span=60, adjust=False, min_periods=60).mean()
    data["ema_20_slope_4"] = data["ema_20"] / data["ema_20"].shift(4) - 1.0
    data["ema_60_slope_4"] = data["ema_60"] / data["ema_60"].shift(4) - 1.0

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    data["atr_14_pct"] = atr / close

    up_move = data["high"].diff()
    down_move = -data["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0))
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0))
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean() / atr
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator.where(denominator != 0)
    data["adx_14"] = dx.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()

    def _rank_last(window: np.ndarray) -> float:
        return float(np.count_nonzero(window <= window[-1]) / len(window))

    data["atr_pct_rank_60"] = data["atr_14_pct"].rolling(60).apply(_rank_last, raw=True)
    return data


def _event_counts(
    marks: list[EventMark],
    occurred_times: list[datetime],
    *,
    end_at: datetime,
    as_of: datetime,
) -> tuple[int, int, datetime | None]:
    start_at = _utc(end_at) - timedelta(hours=EVENT_WINDOW_HOURS)
    left = bisect_right(occurred_times, start_at)
    right = bisect_right(occurred_times, _utc(end_at))
    visible = [mark for mark in marks[left:right] if _utc(mark.effective_at) <= _utc(as_of)]
    return (
        sum(mark.kind == "news" for mark in visible),
        sum(mark.kind == "macro" for mark in visible),
        max((_utc(mark.effective_at) for mark in visible), default=None),
    )


def _raw_classification(values: dict[str, float | int | str]) -> tuple[Regime, float]:
    required = (
        "ema_20",
        "ema_60",
        "ema_20_slope_4",
        "ema_60_slope_4",
        "adx_14",
        "atr_14_pct",
        "atr_pct_rank_60",
    )
    if any(not math.isfinite(float(values.get(name, math.nan))) for name in required):
        return Regime.UNKNOWN, 0.0
    news_count = int(values["news_count_4h"])
    macro_count = int(values["macro_count_4h"])
    if news_count >= NEWS_COUNT_THRESHOLD or macro_count >= MACRO_COUNT_THRESHOLD:
        return Regime.NEWS_DRIVEN, min(1.0, 0.7 + 0.1 * news_count + 0.2 * macro_count)
    vol_rank = float(values["atr_pct_rank_60"])
    if vol_rank >= 0.8:
        return Regime.HIGH_VOLATILITY, vol_rank
    adx = float(values["adx_14"])
    ema20 = float(values["ema_20"])
    ema60 = float(values["ema_60"])
    slope20 = float(values["ema_20_slope_4"])
    slope60 = float(values["ema_60_slope_4"])
    if adx >= 25.0 and ema20 > ema60 and slope20 > 0 and slope60 > 0:
        return Regime.TREND_UP, min(1.0, adx / 50.0)
    if adx >= 25.0 and ema20 < ema60 and slope20 < 0 and slope60 < 0:
        return Regime.TREND_DOWN, min(1.0, adx / 50.0)
    if adx < 20.0:
        return Regime.RANGE, max(0.5, 1.0 - adx / 40.0)
    if vol_rank <= 0.2:
        return Regime.LOW_VOLATILITY, 1.0 - vol_rank
    return Regime.RANGE, 0.55


def _statistical_classification(values: dict[str, float | int | str]) -> Regime:
    if not math.isfinite(float(values.get("atr_pct_rank_60", math.nan))):
        return Regime.UNKNOWN
    if (
        int(values["news_count_4h"]) >= NEWS_COUNT_THRESHOLD
        or int(values["macro_count_4h"]) >= MACRO_COUNT_THRESHOLD
    ):
        return Regime.NEWS_DRIVEN
    rank = float(values["atr_pct_rank_60"])
    if rank >= 0.8:
        return Regime.HIGH_VOLATILITY
    atr = float(values["atr_14_pct"])
    adx = float(values["adx_14"])
    trend_score = (float(values["ema_20"]) - float(values["ema_60"])) / (
        float(values["close"]) * atr
    )
    if adx >= 22.0 and trend_score >= 0.5 and float(values["ema_20_slope_4"]) > 0:
        return Regime.TREND_UP
    if adx >= 22.0 and trend_score <= -0.5 and float(values["ema_20_slope_4"]) < 0:
        return Regime.TREND_DOWN
    if adx < 20.0:
        return Regime.RANGE
    if rank <= 0.2:
        return Regime.LOW_VOLATILITY
    return Regime.RANGE


def _smooth(raw: list[Regime], segment_ids: list[int]) -> list[Regime]:
    output: list[Regime] = []
    active = Regime.UNKNOWN
    held = 0
    pending = Regime.UNKNOWN
    pending_count = 0
    prior_segment = -1
    for candidate, segment_id in zip(raw, segment_ids, strict=True):
        if segment_id != prior_segment:
            active, held, pending, pending_count = Regime.UNKNOWN, 0, Regime.UNKNOWN, 0
            prior_segment = segment_id
        if candidate is Regime.UNKNOWN:
            active, held, pending, pending_count = Regime.UNKNOWN, 0, Regime.UNKNOWN, 0
            output.append(active)
            continue
        if active is Regime.UNKNOWN:
            active, held = candidate, 1
            output.append(active)
            continue
        if candidate is active or held < MIN_HOLD_BARS:
            held += 1
            pending, pending_count = Regime.UNKNOWN, 0
            output.append(active)
            continue
        if candidate is pending:
            pending_count += 1
        else:
            pending, pending_count = candidate, 1
        if pending_count >= CONFIRM_BARS:
            active, held = candidate, 1
            pending, pending_count = Regime.UNKNOWN, 0
        else:
            held += 1
        output.append(active)
    return output


def build_regime_run(
    *,
    instrument: Instrument,
    timeframe: Timeframe,
    bars: list[MarketBar],
    events: list[EventMark] | None = None,
) -> RegimeRun:
    """计算全部逐 bar Regime；每个点只依赖该点及其之前可见的信息。"""
    if timeframe is not Timeframe.H1:
        raise ValueError("Phase 3.1 首版只允许 1h，避免混用未经标定的周期阈值")
    ordered = sorted(bars, key=lambda item: _utc(item.open_time))
    if not ordered:
        return RegimeRun(instrument.id, instrument.symbol, timeframe, ())
    marks = sorted(events or [], key=lambda item: _utc(item.occurred_at))
    occurred_times = [_utc(mark.occurred_at) for mark in marks]
    points: list[RegimePoint] = []
    raw_labels: list[Regime] = []
    segment_ids = [0]
    for index in range(1, len(ordered)):
        increment = int(
            not _gap_is_expected(ordered[index - 1].close_time, ordered[index].open_time)
        )
        segment_ids.append(segment_ids[-1] + increment)

    indicator_rows: list[pd.Series[Any]] = []
    segment_starts: dict[int, int] = {}
    for segment_id in range(segment_ids[-1] + 1):
        positions = [index for index, item in enumerate(segment_ids) if item == segment_id]
        start, end = positions[0], positions[-1] + 1
        segment_starts[segment_id] = start
        frame = _indicator_frame(ordered[start:end])
        indicator_rows.extend(frame.iloc[index] for index in range(len(frame)))

    chain = ""
    current_segment = -1
    bar_max_effective = datetime.min.replace(tzinfo=UTC)

    for index, bar in enumerate(ordered):
        segment_id = segment_ids[index]
        segment_start = segment_starts[segment_id]
        if segment_id != current_segment:
            chain = ""
            bar_max_effective = datetime.min.replace(tzinfo=UTC)
            current_segment = segment_id
        latest = indicator_rows[index]
        bar_max_effective = max(bar_max_effective, _utc(bar.effective_at))
        as_of = max(_utc(bar.close_time), bar_max_effective)
        news_count, macro_count, event_max_effective = _event_counts(
            marks, occurred_times, end_at=bar.close_time, as_of=as_of
        )
        max_effective = max(
            bar_max_effective,
            event_max_effective or bar_max_effective,
        )
        values: dict[str, float | int | str] = {
            "bar_id": str(bar.id),
            "close": round(float(str(bar.close)), 12),
            "news_count_4h": news_count,
            "macro_count_4h": macro_count,
            "segment_id": segment_id,
        }
        for name in (
            "ema_20",
            "ema_60",
            "ema_20_slope_4",
            "ema_60_slope_4",
            "adx_14",
            "atr_14_pct",
            "atr_pct_rank_60",
        ):
            value = float(latest[name])
            if math.isfinite(value):
                values[name] = round(value, 12)
        raw_regime, confidence = _raw_classification(values)
        statistical = _statistical_classification(values)
        token = _bar_token(bar)
        chain = hashlib.sha256(f"{chain}\n{token}".encode()).hexdigest()
        point = RegimePoint(
            bar_id=bar.id,
            instrument_id=instrument.id,
            start_at=_utc(bar.open_time),
            end_at=_utc(bar.close_time),
            as_of=as_of,
            max_effective_at=max_effective,
            input_start_at=_utc(ordered[segment_start].open_time),
            input_count=index - segment_start + 1,
            input_hash=chain,
            values=values,
            raw_regime=raw_regime,
            statistical_regime=statistical,
            regime=raw_regime,
            confidence=confidence,
        )
        points.append(point)
        raw_labels.append(raw_regime)

    smoothed = _smooth(raw_labels, segment_ids)
    final = tuple(
        RegimePoint(
            bar_id=point.bar_id,
            instrument_id=point.instrument_id,
            start_at=point.start_at,
            end_at=point.end_at,
            as_of=point.as_of,
            max_effective_at=point.max_effective_at,
            input_start_at=point.input_start_at,
            input_count=point.input_count,
            input_hash=point.input_hash,
            values={
                **point.values,
                "raw_regime": point.raw_regime.value,
                "statistical_regime": point.statistical_regime.value,
                "final_regime": label.value,
            },
            raw_regime=point.raw_regime,
            statistical_regime=point.statistical_regime,
            regime=label,
            confidence=point.confidence if label is point.raw_regime else 0.5,
        )
        for point, label in zip(points, smoothed, strict=True)
    )
    return RegimeRun(instrument.id, instrument.symbol, timeframe, final)


def load_regime_inputs(
    session: Session, *, instrument_symbol: str, timeframe: Timeframe = Timeframe.H1
) -> tuple[Instrument, list[MarketBar], list[EventMark]]:
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == instrument_symbol))
    if instrument is None:
        raise ValueError(f"未知标的：{instrument_symbol}")
    bars = list(
        session.scalars(
            sa.select(MarketBar)
            .where(MarketBar.instrument_id == instrument.id, MarketBar.timeframe == timeframe)
            .order_by(MarketBar.open_time)
        ).all()
    )
    news = session.execute(
        sa.select(NewsEvent.published_at, NewsEvent.effective_at).order_by(NewsEvent.published_at)
    ).all()
    macro = session.execute(
        sa.select(MacroEvent.released_at, MacroEvent.effective_at).order_by(MacroEvent.released_at)
    ).all()
    marks = [EventMark(_utc(row[0]), _utc(row[1]), "news") for row in news]
    marks.extend(EventMark(_utc(row[0]), _utc(row[1]), "macro") for row in macro)
    return instrument, bars, marks


def _feature_set(session: Session) -> FeatureSet:
    item = session.scalar(
        sa.select(FeatureSet).where(
            FeatureSet.name == REGIME_FEATURE_SET_NAME,
            FeatureSet.version == REGIME_FEATURE_SET_VERSION,
        )
    )
    if item is not None:
        if item.definition_json != REGIME_FEATURE_DEFINITION:
            raise RuntimeError("regime-core 版本与代码定义不一致；必须提升版本")
        return item
    item = FeatureSet(
        id=uuid7(),
        name=REGIME_FEATURE_SET_NAME,
        version=REGIME_FEATURE_SET_VERSION,
        kind=FeatureSetKind.MIXED,
        description="Phase 3.1 可解释 Regime 特征（规则 + 因果滚动统计）。",
        definition_json=REGIME_FEATURE_DEFINITION,
    )
    session.add(item)
    session.flush()
    return item


def _typed_feature_value(
    snapshot_id: uuid.UUID, name: str, value: float | int | str
) -> FeatureValue:
    kwargs: dict[str, Any] = {
        "id": uuid7(),
        "feature_snapshot_id": snapshot_id,
        "feature_name": name,
        "metadata_json": {"model_version": REGIME_MODEL_VERSION},
    }
    if isinstance(value, str):
        kwargs["value_text"] = value
    else:
        kwargs["value_numeric"] = Decimal(str(value))
    return FeatureValue(**kwargs)


def persist_regime_run(session: Session, run: RegimeRun) -> RegimeWriteResult:
    """幂等写入逐 bar 特征快照和 Regime；已有行必须逐值一致。"""
    feature_set = _feature_set(session)
    existing_regimes = {
        _utc(item.start_at): item
        for item in session.scalars(
            sa.select(MarketRegime).where(
                MarketRegime.instrument_id == run.instrument_id,
                MarketRegime.model_version == REGIME_MODEL_VERSION,
            )
        ).all()
    }
    inserted_snapshots = 0
    inserted_regimes = 0
    reused = 0
    dataset_name = f"regime-input:{run.instrument_symbol}:{run.timeframe.value}"
    versions = {
        item.version: item
        for item in session.scalars(
            sa.select(DataVersion).where(DataVersion.dataset_name == dataset_name)
        ).all()
    }
    data_versions: dict[uuid.UUID, DataVersion] = {}
    for point in run.points:
        existing_regime = existing_regimes.get(point.start_at)
        if existing_regime is not None:
            if (
                existing_regime.regime_type != point.regime
                or _utc(existing_regime.end_at) != point.end_at  # type: ignore[arg-type]
            ):
                raise RuntimeError(f"Regime 幂等冲突：{point.start_at.isoformat()}")
            reused += 1
            continue

        version = f"bar-{point.bar_id.hex[:32]}"
        data_version = versions.get(version)
        if data_version is None:
            data_version = DataVersion(
                id=uuid7(),
                dataset_name=dataset_name,
                version=version,
                start_at=point.input_start_at,
                end_at=point.end_at,
                data_hash=point.input_hash,
                source_scope_json={
                    "last_market_bar_id": str(point.bar_id),
                    "hash_algorithm": "sha256-chain-v1",
                },
                filter_json={
                    "instrument": run.instrument_symbol,
                    "timeframe": run.timeframe.value,
                    "as_of": point.as_of.isoformat(),
                },
                row_count=point.input_count,
                generated_at=utc_now(),
                notes="Phase 3.1 causal Regime input prefix",
            )
            session.add(data_version)
            versions[version] = data_version
        elif data_version.data_hash != point.input_hash:
            raise RuntimeError(f"Regime DataVersion 哈希冲突：{version}")
        data_versions[point.bar_id] = data_version

    # FeatureSnapshot 的 FK 指向 DataVersion；先统一 flush，避免仅靠 UUID 裸值时
    # SQLAlchemy 无对象关系可用于推导同一 flush 内的插入顺序。
    session.flush()

    for point in run.points:
        if point.start_at in existing_regimes:
            continue
        data_version = data_versions[point.bar_id]

        snapshot = FeatureSnapshot(
            id=uuid7(),
            feature_set_id=feature_set.id,
            instrument_id=run.instrument_id,
            as_of=point.as_of,
            max_effective_at=point.max_effective_at,
            values_json=point.values,
            data_version_id=data_version.id,
        )
        session.add(snapshot)
        session.add_all(
            _typed_feature_value(snapshot.id, name, value)
            for name, value in sorted(point.values.items())
        )
        session.add(
            MarketRegime(
                id=uuid7(),
                instrument_id=run.instrument_id,
                regime_type=point.regime,
                confidence=Decimal(str(round(point.confidence, 10))),
                start_at=point.start_at,
                end_at=point.end_at,
                detected_at=point.as_of,
                model_version=REGIME_MODEL_VERSION,
                feature_snapshot_id=snapshot.id,
            )
        )
        inserted_snapshots += 1
        inserted_regimes += 1
        if inserted_regimes % 500 == 0:
            session.flush()
    session.flush()
    return RegimeWriteResult(inserted_snapshots, inserted_regimes, reused)
