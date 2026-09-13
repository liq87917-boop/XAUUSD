"""collector_runs.retry_count（团队批复：持久化超时/限流重试次数）

Revision ID: 0002_collector_run_retry_count
Revises: 0001_phase1_core_tables
Create Date: 2026-09-12

背景：
    04 §7 的 ``collector_runs`` 原始字段清单没有 ``retry_count``。但 Phase 1 第 2 步的
    采集重试机制（最多 3 次尝试 + 指数退避）必须能被持久化，否则：
    - 无法在数据质量分析中区分"一次成功"与"重试 3 次才成功"；
    - Dashboard / Scheduler Monitor 无法展示限流与超时趋势。
    本迁移经团队批复新增该列。

实现说明：
1. 使用 ``batch_alter_table``：SQLite 不支持直接 ADD CONSTRAINT，batch 模式会以
   "重建表"方式完成加列 + 加约束（本地开发与 CI 测试库即 SQLite）；
   PostgreSQL 上 Alembic 会自动退化为普通 ``ALTER TABLE``。
2. 已有行回填 0：加列时带 ``server_default='0'``；ORM 侧保留同一个 server_default
   （见 database/models/collector.py），并由漂移测试强制"迁移结果 == ORM 元数据"。
3. 约束名与 ORM 命名约定一致：``ck_collector_runs_retry_count_non_negative``。

不可回滚说明：无（本 revision 可完整回滚：先删约束再删列，已写入的 retry_count 数据会丢失，
但该列仅用于可观测性统计，不影响原始数据与研究事实表）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_collector_run_retry_count"
down_revision: str | None = "0001_phase1_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "retry_count_non_negative"


def upgrade() -> None:
    with op.batch_alter_table("collector_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_check_constraint(CONSTRAINT_NAME, "retry_count >= 0")


def downgrade() -> None:
    with op.batch_alter_table("collector_runs", schema=None) as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="check")
        batch_op.drop_column("retry_count")
