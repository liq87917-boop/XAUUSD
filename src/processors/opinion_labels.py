"""作者观点前瞻收益标签（W0-4，严格遵循 Phase 3 §3.4）。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorOpinion, AuthorPost, MarketBar, RawItem
from database.models.enums import OpinionHorizon, OpinionStance, Timeframe
from src.processors.timeline import ensure_utc_from_database

__all__ = ["LABEL_VERSION", "OpinionLabel", "build_opinion_label", "build_opinion_labels"]

LABEL_VERSION: Final[str] = "forward-return-v2"
PROVIDER_SYMBOL: Final[str] = "GC=F"
INSTRUMENT_PROXY: Final[str] = "COMEX_CONTINUOUS_FUTURES"

_HORIZON_MAP: Final[dict[OpinionHorizon, tuple[Timeframe, timedelta]]] = {
    OpinionHorizon.M15: (Timeframe.M15, timedelta(minutes=15)),
    OpinionHorizon.M30: (Timeframe.M30, timedelta(minutes=30)),
    OpinionHorizon.H1: (Timeframe.H1, timedelta(hours=1)),
    OpinionHorizon.H4: (Timeframe.H4, timedelta(hours=4)),
    OpinionHorizon.D1: (Timeframe.D1, timedelta(days=1)),
}
_STANCE_DIRECTION: Final[dict[OpinionStance, int]] = {
    OpinionStance.LONG: 1,
    OpinionStance.SHORT: -1,
    OpinionStance.FLAT: 0,
}
EVALUATION_GRID: Final[tuple[OpinionHorizon, ...]] = (
    OpinionHorizon.H1,
    OpinionHorizon.H4,
    OpinionHorizon.D1,
)


@dataclass(frozen=True, slots=True)
class OpinionLabel:
    opinion_id: str
    effective_at: str
    stance: str
    horizon: str | None
    horizon_source: str
    status: str
    reason: str | None = None
    collection_time_provenance: str | None = None
    entry_at: str | None = None
    entry_lag_seconds: float | None = None
    exit_at: str | None = None
    entry_open: float | None = None
    exit_close: float | None = None
    log_return: float | None = None
    market_direction: int | None = None
    stance_direction: int | None = None
    direction_hit: bool | None = None
    label_version: str = LABEL_VERSION
    provider_symbol: str = PROVIDER_SYMBOL
    instrument_proxy: str = INSTRUMENT_PROXY

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _base(
    opinion: AuthorOpinion,
    *,
    status: str,
    reason: str,
    horizon: OpinionHorizon | None = None,
    horizon_source: str = "declared",
    collection_time_provenance: str | None = None,
) -> OpinionLabel:
    effective_at = ensure_utc_from_database(
        opinion.effective_at, field_name="author_opinions.effective_at"
    )
    return OpinionLabel(
        opinion_id=str(opinion.id),
        effective_at=effective_at.isoformat(),
        stance=opinion.stance.value,
        horizon=None if horizon is None else horizon.value,
        horizon_source=horizon_source,
        status=status,
        reason=reason,
        collection_time_provenance=collection_time_provenance,
    )


def _collection_time_provenance(session: Session, opinion: AuthorOpinion) -> str | None:
    raw_json = session.scalar(
        sa.select(RawItem.raw_json)
        .join(AuthorPost, AuthorPost.raw_item_id == RawItem.id)
        .where(AuthorPost.id == opinion.author_post_id)
    )
    if not isinstance(raw_json, dict):
        return None
    value = raw_json.get("collected_at_provenance")
    return value if isinstance(value, str) else None


def build_opinion_label(
    session: Session,
    opinion: AuthorOpinion,
    *,
    evaluation_horizon: OpinionHorizon | None = None,
) -> OpinionLabel:
    """计算单条标签；任何缺失均返回显式状态，不猜 horizon、不插值。"""
    collection_time_provenance = _collection_time_provenance(session, opinion)
    if collection_time_provenance == "input_effective_at_fallback":
        return _base(
            opinion,
            status="UNTRUSTED_COLLECTION_TIME",
            reason="collected_at 由 effective_at 回填，只允许验证管道，不进入研究标签",
            horizon=opinion.horizon or evaluation_horizon,
            horizon_source=(
                "declared" if opinion.horizon is not None else "evaluation_grid"
            ),
            collection_time_provenance=collection_time_provenance,
        )
    if opinion.horizon is None and evaluation_horizon is None:
        return _base(
            opinion,
            status="MISSING_HORIZON",
            reason="观点未给出 horizon",
            collection_time_provenance=collection_time_provenance,
        )
    horizon = opinion.horizon or evaluation_horizon
    assert horizon is not None
    horizon_source = "declared" if opinion.horizon is not None else "evaluation_grid"
    if opinion.instrument_id is None:
        return _base(
            opinion,
            status="MISSING_INSTRUMENT",
            reason="观点未绑定标的",
            horizon=horizon,
            horizon_source=horizon_source,
            collection_time_provenance=collection_time_provenance,
        )
    stance_direction = _STANCE_DIRECTION.get(opinion.stance)
    if stance_direction is None:
        return _base(
            opinion,
            status="UNKNOWN_STANCE",
            reason="UNKNOWN 方向不可评价",
            horizon=horizon,
            horizon_source=horizon_source,
            collection_time_provenance=collection_time_provenance,
        )

    timeframe, duration = _HORIZON_MAP[horizon]
    effective_at = ensure_utc_from_database(
        opinion.effective_at, field_name="author_opinions.effective_at"
    )
    bar = session.scalar(
        sa.select(MarketBar)
        .where(
            MarketBar.instrument_id == opinion.instrument_id,
            MarketBar.timeframe == timeframe,
            MarketBar.open_time > effective_at,  # 严格晚于 signal.effective_at
        )
        .order_by(MarketBar.open_time)
        .limit(1)
    )
    if bar is None:
        return _base(
            opinion,
            status="NO_ENTRY_BAR",
            reason=f"没有后续 {timeframe.value} K 线",
            horizon=horizon,
            horizon_source=horizon_source,
            collection_time_provenance=collection_time_provenance,
        )

    entry_at = ensure_utc_from_database(bar.open_time, field_name="market_bars.open_time")
    entry_lag = entry_at - effective_at
    if entry_lag > duration:
        base = _base(
            opinion,
            status="ENTRY_LAG_EXCEEDED",
            reason=f"入场延迟超过 {horizon.value} 周期",
            horizon=horizon,
            horizon_source=horizon_source,
            collection_time_provenance=collection_time_provenance,
        )
        return OpinionLabel(
            **{
                **base.to_dict(),
                "entry_at": entry_at.isoformat(),
                "entry_lag_seconds": entry_lag.total_seconds(),
            }
        )
    exit_at = ensure_utc_from_database(bar.close_time, field_name="market_bars.close_time")
    expected_exit = entry_at + duration
    if exit_at != expected_exit:
        base = _base(
            opinion,
            status="GAP_IN_HORIZON",
            reason="K 线跨度与 horizon 不一致",
            horizon=horizon,
            horizon_source=horizon_source,
        )
        return OpinionLabel(
            **{
                **base.to_dict(),
                "entry_at": entry_at.isoformat(),
                "entry_lag_seconds": entry_lag.total_seconds(),
                "exit_at": exit_at.isoformat(),
            }
        )

    entry_open = float(cast(Decimal, bar.open))
    exit_close = float(cast(Decimal, bar.close))
    value = math.log(exit_close / entry_open)
    market_direction = 1 if value > 0 else (-1 if value < 0 else 0)
    return OpinionLabel(
        opinion_id=str(opinion.id),
        effective_at=effective_at.isoformat(),
        stance=opinion.stance.value,
        horizon=horizon.value,
        horizon_source=horizon_source,
        status="LABELED",
        collection_time_provenance=collection_time_provenance,
        entry_at=entry_at.isoformat(),
        entry_lag_seconds=entry_lag.total_seconds(),
        exit_at=exit_at.isoformat(),
        entry_open=entry_open,
        exit_close=exit_close,
        log_return=value,
        market_direction=market_direction,
        stance_direction=stance_direction,
        direction_hit=market_direction == stance_direction,
    )


def build_opinion_labels(session: Session) -> list[OpinionLabel]:
    opinions = list(
        session.scalars(
            sa.select(AuthorOpinion).order_by(AuthorOpinion.effective_at, AuthorOpinion.id)
        ).all()
    )
    labels: list[OpinionLabel] = []
    for opinion in opinions:
        if opinion.horizon is None:
            labels.extend(
                build_opinion_label(session, opinion, evaluation_horizon=horizon)
                for horizon in EVALUATION_GRID
            )
        else:
            labels.append(build_opinion_label(session, opinion))
    return labels
