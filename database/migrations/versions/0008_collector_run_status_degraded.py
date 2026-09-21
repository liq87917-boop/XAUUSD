"""collector_runs.status 增加 DEGRADED（provider 降级状态）。

Revision ID: 0008_collector_run_status_degraded
Revises: 0007_phase3_feature_tables
Create Date: 2026-09-21

背景：
    Phase 2 Collector Infrastructure Hardening 引入「provider 降级」语义：
    provider 故障但已通过 fallback（如 DBnomics library → REST）或部分数据可用时，
    状态应为 DEGRADED，而非 FAILED。``provider FAILED != pipeline FAILED``。

设计：
    改 ``collector_runs.status`` 的 CHECK 约束（VARCHAR + CHECK，见 enum_type），
    新增 'DEGRADED' 取值。PostgreSQL 与 SQLite 都支持 DROP/ADD CHECK 约束。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_collector_run_status_degraded"
down_revision: str | None = "0007_phase3_feature_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES_WITH_DEGRADED = (
    "'PENDING','RUNNING','SUCCESS','PARTIAL_FAILED','DEGRADED','FAILED'"
)
_STATUSES_WITHOUT_DEGRADED = "'PENDING','RUNNING','SUCCESS','PARTIAL_FAILED','FAILED'"


def upgrade() -> None:
    op.drop_constraint(
        "ck_collector_runs_collector_run_status", "collector_runs", type_="check"
    )
    op.create_check_constraint(
        "collector_run_status", "collector_runs", f"status IN ({_STATUSES_WITH_DEGRADED})"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_collector_runs_collector_run_status", "collector_runs", type_="check"
    )
    op.create_check_constraint(
        "collector_run_status", "collector_runs", f"status IN ({_STATUSES_WITHOUT_DEGRADED})"
    )
