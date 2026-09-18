"""Phase 3 当前检查点的范围边界审计。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

EXPECTED_REVISION: Final[str] = "0007_phase3_feature_tables"
FORBIDDEN_FACT_TABLES: Final[frozenset[str]] = frozenset(
    {
        "alpha_models",
        "alpha_signals",
        "calibration_models",
        "predictions",
        "author_regime_skills",
        "author_horizon_skills",
        "author_information_type_skills",
        "ensemble_runs",
        "ensemble_components",
        "strategies",
        "strategy_versions",
        "backtest_runs",
        "orders",
        "fills",
        "positions",
    }
)


@dataclass(frozen=True, slots=True)
class Phase3BoundaryAudit:
    revision: str
    unexpected_fact_tables: tuple[str, ...]
    author_skill_rows: int
    author_weight_rows: int
    live_trading: bool
    external_order_submission: bool
    passed: bool


def assess_phase3_boundaries(
    *,
    revision: str,
    table_names: set[str],
    author_skill_rows: int,
    author_weight_rows: int,
    live_trading: bool,
    external_order_submission: bool,
) -> Phase3BoundaryAudit:
    """检查失败 Alpha 未越权落表、Phase 3.3 阻塞时未写技能/权重、实盘仍关闭。"""
    unexpected = tuple(sorted(table_names & FORBIDDEN_FACT_TABLES))
    passed = bool(
        revision == EXPECTED_REVISION
        and not unexpected
        and author_skill_rows == 0
        and author_weight_rows == 0
        and not live_trading
        and not external_order_submission
    )
    return Phase3BoundaryAudit(
        revision=revision,
        unexpected_fact_tables=unexpected,
        author_skill_rows=author_skill_rows,
        author_weight_rows=author_weight_rows,
        live_trading=live_trading,
        external_order_submission=external_order_submission,
        passed=passed,
    )
