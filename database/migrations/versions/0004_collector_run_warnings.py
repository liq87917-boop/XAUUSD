"""collector_runs.warnings_json（团队批复：数据质量告警持久化）

Revision ID: 0004_collector_run_warnings
Revises: 0003_market_bars_source_unique
Create Date: 2026-09-12

背景：
    ``min_records_per_run`` 阈值（按来源配置在 ``sources.config_json``）触发时必须留下痕迹。
    仅打 WARNING 日志不足以支撑"每轮采集完成后可复核数据质量"的要求：
    Dashboard / 数据质量巡检需要能直接查询"本轮是否有告警"。

设计：
    - 新增可空 JSONB 列 ``warnings_json``，结构 ``{"warnings": ["..."]}``；
    - **非 NULL 即表示该轮为 WARNING 级运行**（status 仍保持 SUCCESS / PARTIAL_FAILED，
      不新增 status 枚举值，避免破坏 04 §7 的既有状态语义）；
    - 无告警时写 NULL，保持"无告警"与"空告警"可区分（NULL 表示未评估/无告警）。

方言说明：仅新增可空列，PostgreSQL 与 SQLite 都支持直接 ``ALTER TABLE ADD COLUMN``，
          无需 batch 模式。

不可回滚说明：无（downgrade 删除该列；已写入的告警信息会丢失，但不影响原始数据）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from database.types import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004_collector_run_warnings"
down_revision: str | None = "0003_market_bars_source_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 复用 database.types.JSONB（PG 原生 JSONB + SQLite 测试变体），保证与 ORM 完全一致
    op.add_column(
        "collector_runs",
        sa.Column("warnings_json", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collector_runs", "warnings_json")
