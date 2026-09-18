"""W0-5 市场特征快照生成器。

所有输入同时满足 ``close_time <= as_of`` 与 ``effective_at <= as_of``；连续窗口
不足时明确失败，不插值、不跨缺口。持久化时绑定输入行哈希与 DataVersion，重复运行
复用同一快照，回放则按冻结的行 ID 重算并逐值核对。
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import (
    DataVersion,
    FeatureSet,
    FeatureSetKind,
    FeatureSnapshot,
    FeatureValue,
    Instrument,
    MarketBar,
    Timeframe,
)
from database.models.enums import TIMEFRAME_SECONDS
from src.common.time import utc_now

FEATURE_SET_NAME: Final[str] = "market-core"
FEATURE_SET_VERSION: Final[str] = "1.0.0"
INPUT_BARS: Final[int] = 5
RETURN_WINDOW: Final[int] = INPUT_BARS - 1

FEATURE_DEFINITION: Final[dict[str, Any]] = {
    "input": "market_bars",
    "input_bars": INPUT_BARS,
    "gap_policy": "reject_window",
    "visibility": ["close_time <= as_of", "effective_at <= as_of"],
    "features": {
        "close": "latest close",
        "log_return_1": "ln(close_t / close_t-1)",
        "momentum_4": "ln(close_t / close_t-4)",
        "sma_4_ratio": "close_t / mean(close_t-3..close_t) - 1",
        "realized_vol_4": "population stddev of last four one-bar log returns",
        "range_pct": "(high_t - low_t) / close_t",
    },
}


class FeatureWindowGapError(ValueError):
    """在可见数据中找不到足够长的连续窗口。"""


class FeatureReplayError(RuntimeError):
    """冻结输入或重算结果与已持久化快照不一致。"""


@dataclass(frozen=True, slots=True)
class FeatureCandidate:
    instrument_id: uuid.UUID
    instrument_symbol: str
    timeframe: Timeframe
    as_of: datetime
    input_bar_ids: tuple[uuid.UUID, ...]
    input_start_at: datetime
    input_end_at: datetime
    max_effective_at: datetime
    data_hash: str
    values: dict[str, float]


@dataclass(frozen=True, slots=True)
class SnapshotWriteResult:
    snapshot_id: uuid.UUID
    data_version_id: uuid.UUID
    inserted: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _rounded(value: float) -> float:
    return round(value, 12)


def _bar_payload(bar: MarketBar) -> dict[str, Any]:
    return {
        "id": str(bar.id),
        "instrument_id": str(bar.instrument_id),
        "timeframe": bar.timeframe.value,
        "open_time": _utc(bar.open_time).isoformat(timespec="microseconds"),
        "close_time": _utc(bar.close_time).isoformat(timespec="microseconds"),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": None if bar.volume is None else str(bar.volume),
        "source_id": None if bar.source_id is None else str(bar.source_id),
        "collected_at": _utc(bar.collected_at).isoformat(timespec="microseconds"),
        "effective_at": _utc(bar.effective_at).isoformat(timespec="microseconds"),
    }


def _bars_hash(bars: list[MarketBar]) -> str:
    payload = json.dumps(
        [_bar_payload(bar) for bar in bars],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_contiguous(bars: list[MarketBar], timeframe: Timeframe) -> bool:
    duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe.value])
    for bar in bars:
        if _utc(bar.close_time) - _utc(bar.open_time) != duration:
            return False
    return all(
        _utc(current.open_time) == _utc(previous.close_time)
        for previous, current in zip(bars, bars[1:], strict=False)
    )


def _latest_contiguous_window(bars: list[MarketBar], timeframe: Timeframe) -> list[MarketBar]:
    for end in range(len(bars), INPUT_BARS - 1, -1):
        window = bars[end - INPUT_BARS : end]
        if _is_contiguous(window, timeframe):
            return window
    raise FeatureWindowGapError(
        f"找不到 {INPUT_BARS} 根连续 {timeframe.value} K 线；W0-5 禁止跨缺口计算或插值"
    )


def _calculate_values(bars: list[MarketBar]) -> dict[str, float]:
    closes = [float(str(bar.close)) for bar in bars]
    latest = bars[-1]
    returns = [
        math.log(current / previous)
        for previous, current in zip(closes[:-1], closes[1:], strict=True)
    ]
    mean_return = sum(returns) / len(returns)
    realized_vol = math.sqrt(sum((item - mean_return) ** 2 for item in returns) / len(returns))
    sma = sum(closes[-RETURN_WINDOW:]) / RETURN_WINDOW
    return {
        "close": _rounded(closes[-1]),
        "log_return_1": _rounded(returns[-1]),
        "momentum_4": _rounded(math.log(closes[-1] / closes[0])),
        "sma_4_ratio": _rounded(closes[-1] / sma - 1.0),
        "realized_vol_4": _rounded(realized_vol),
        "range_pct": _rounded((float(str(latest.high)) - float(str(latest.low))) / closes[-1]),
    }


def _candidate_from_bars(
    *, instrument: Instrument, timeframe: Timeframe, as_of: datetime, bars: list[MarketBar]
) -> FeatureCandidate:
    if len(bars) != INPUT_BARS or not _is_contiguous(bars, timeframe):
        raise FeatureWindowGapError("冻结输入不是完整连续窗口")
    normal_as_of = _utc(as_of)
    max_effective_at = max(_utc(bar.effective_at) for bar in bars)
    if any(_utc(bar.close_time) > normal_as_of for bar in bars) or max_effective_at > normal_as_of:
        raise FeatureReplayError("输入含 as_of 时点尚不可见的数据")
    return FeatureCandidate(
        instrument_id=instrument.id,
        instrument_symbol=instrument.symbol,
        timeframe=timeframe,
        as_of=normal_as_of,
        input_bar_ids=tuple(bar.id for bar in bars),
        input_start_at=_utc(bars[0].open_time),
        input_end_at=_utc(bars[-1].close_time),
        max_effective_at=max_effective_at,
        data_hash=_bars_hash(bars),
        values=_calculate_values(bars),
    )


def compute_market_feature_candidate(
    session: Session,
    *,
    instrument_symbol: str,
    timeframe: Timeframe = Timeframe.H4,
    as_of: datetime,
) -> FeatureCandidate:
    """从 ``as_of`` 时点可见的数据中选取最新的连续窗口并计算特征。"""
    normal_as_of = _utc(as_of)
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == instrument_symbol))
    if instrument is None:
        raise ValueError(f"未知标的：{instrument_symbol}")
    statement = (
        sa.select(MarketBar)
        .where(
            MarketBar.instrument_id == instrument.id,
            MarketBar.timeframe == timeframe,
            MarketBar.close_time <= normal_as_of,
            MarketBar.effective_at <= normal_as_of,
        )
        .order_by(MarketBar.close_time.desc())
        .limit(512)
    )
    visible = list(reversed(session.scalars(statement).all()))
    window = _latest_contiguous_window(visible, timeframe)
    return _candidate_from_bars(
        instrument=instrument, timeframe=timeframe, as_of=normal_as_of, bars=window
    )


def _get_or_create_feature_set(session: Session) -> FeatureSet:
    feature_set = session.scalar(
        sa.select(FeatureSet).where(
            FeatureSet.name == FEATURE_SET_NAME, FeatureSet.version == FEATURE_SET_VERSION
        )
    )
    if feature_set is not None:
        if feature_set.definition_json != FEATURE_DEFINITION:
            raise FeatureReplayError("已存在的特征集版本与代码定义不一致；请提升版本号")
        return feature_set
    feature_set = FeatureSet(
        name=FEATURE_SET_NAME,
        version=FEATURE_SET_VERSION,
        kind=FeatureSetKind.TECHNICAL,
        description="W0-5 最小市场特征集；仅验证时点可见性与可回放基础设施。",
        definition_json=FEATURE_DEFINITION,
    )
    session.add(feature_set)
    session.flush()
    return feature_set


def _get_or_create_data_version(session: Session, candidate: FeatureCandidate) -> DataVersion:
    dataset_name = (
        f"feature-input:{FEATURE_SET_NAME}:"
        f"{candidate.instrument_symbol}:{candidate.timeframe.value}"
    )
    version = f"window-v1-{candidate.data_hash[:16]}"
    data_version = session.scalar(
        sa.select(DataVersion).where(
            DataVersion.dataset_name == dataset_name, DataVersion.version == version
        )
    )
    if data_version is not None:
        if data_version.data_hash != candidate.data_hash:
            raise FeatureReplayError("DataVersion 名称碰撞且完整哈希不同")
        return data_version
    data_version = DataVersion(
        dataset_name=dataset_name,
        version=version,
        start_at=candidate.input_start_at,
        end_at=candidate.input_end_at,
        data_hash=candidate.data_hash,
        source_scope_json={"market_bar_ids": [str(item) for item in candidate.input_bar_ids]},
        filter_json={
            "instrument_symbol": candidate.instrument_symbol,
            "timeframe": candidate.timeframe.value,
            "as_of": candidate.as_of.isoformat(),
            "close_time_lte_as_of": True,
            "effective_at_lte_as_of": True,
            "gap_policy": "reject_window",
        },
        row_count=len(candidate.input_bar_ids),
        generated_at=utc_now(),
        notes="W0-5 feature snapshot frozen input window",
    )
    session.add(data_version)
    session.flush()
    return data_version


def persist_market_feature_candidate(
    session: Session, candidate: FeatureCandidate
) -> SnapshotWriteResult:
    """幂等持久化候选；相同输入与时点只能得到同一快照。"""
    if candidate.max_effective_at > candidate.as_of:
        raise FeatureReplayError("max_effective_at 晚于 as_of")
    feature_set = _get_or_create_feature_set(session)
    data_version = _get_or_create_data_version(session, candidate)
    existing = session.scalar(
        sa.select(FeatureSnapshot).where(
            FeatureSnapshot.feature_set_id == feature_set.id,
            FeatureSnapshot.instrument_id == candidate.instrument_id,
            FeatureSnapshot.as_of == candidate.as_of,
            FeatureSnapshot.data_version_id == data_version.id,
        )
    )
    if existing is not None:
        if existing.values_json != candidate.values:
            raise FeatureReplayError("相同快照身份重算值不一致")
        return SnapshotWriteResult(existing.id, data_version.id, False)

    snapshot = FeatureSnapshot(
        feature_set_id=feature_set.id,
        instrument_id=candidate.instrument_id,
        as_of=candidate.as_of,
        max_effective_at=candidate.max_effective_at,
        values_json=candidate.values,
        data_version_id=data_version.id,
    )
    session.add(snapshot)
    session.flush()
    for name, value in sorted(candidate.values.items()):
        session.add(
            FeatureValue(
                feature_snapshot_id=snapshot.id,
                feature_name=name,
                value_numeric=Decimal(str(value)),
                metadata_json={"feature_set_version": FEATURE_SET_VERSION},
            )
        )
    session.flush()
    return SnapshotWriteResult(snapshot.id, data_version.id, True)


def replay_market_feature_snapshot(session: Session, snapshot_id: uuid.UUID) -> FeatureCandidate:
    """按 DataVersion 冻结的原始行重算并验证快照。"""
    snapshot = session.get(FeatureSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError(f"特征快照不存在：{snapshot_id}")
    data_version = session.get(DataVersion, snapshot.data_version_id)
    instrument = session.get(Instrument, snapshot.instrument_id)
    if data_version is None or instrument is None:
        raise FeatureReplayError("快照的追溯引用不完整")
    scope = data_version.source_scope_json or {}
    raw_ids = scope.get("market_bar_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) != INPUT_BARS:
        raise FeatureReplayError("DataVersion 未保存完整 market_bar_ids")
    bar_ids = [uuid.UUID(item) for item in raw_ids]
    bars_by_id = {
        bar.id: bar
        for bar in session.scalars(sa.select(MarketBar).where(MarketBar.id.in_(bar_ids))).all()
    }
    if len(bars_by_id) != INPUT_BARS:
        raise FeatureReplayError("冻结的行情输入已缺失")
    bars = [bars_by_id[item] for item in bar_ids]
    candidate = _candidate_from_bars(
        instrument=instrument,
        timeframe=bars[0].timeframe,
        as_of=snapshot.as_of,
        bars=bars,
    )
    if candidate.data_hash != data_version.data_hash or candidate.values != snapshot.values_json:
        raise FeatureReplayError("回放结果与冻结快照不一致")
    return candidate
