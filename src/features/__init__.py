"""可回放、严格按 as-of 截止的特征快照。"""

from src.features.market import (
    FeatureCandidate,
    FeatureReplayError,
    FeatureWindowGapError,
    SnapshotWriteResult,
    compute_market_feature_candidate,
    persist_market_feature_candidate,
    replay_market_feature_snapshot,
)

__all__ = [
    "FeatureCandidate",
    "FeatureReplayError",
    "FeatureWindowGapError",
    "SnapshotWriteResult",
    "compute_market_feature_candidate",
    "persist_market_feature_candidate",
    "replay_market_feature_snapshot",
]
