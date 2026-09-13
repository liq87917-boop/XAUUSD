"""Phase 2（Author Lab）四张研究事实表

Revision ID: 0005_phase2_author_lab_tables
Revises: 0004_collector_run_warnings
Create Date: 2026-09-12

对应文档：
- 04_数据表结构及字段定义 §10 author_opinions / §11 propagation_edges /
  §19 author_skill_snapshots / §20 author_weight_snapshots；§42「Phase 2 再建」
- 07_每个阶段的Cline执行Prompt Phase 2：必须建立这 4 张表；所有 LLM/解析器输出必须
  记录 parser/model version；所有测试必须避免 future leakage
- 03_数据库完整设计 §8（索引建议）、§10（不可覆盖原则）、§14（金融数值类型）

实现说明（阅读后再修改本文件）：
1. **手写 DDL**，与 ORM 元数据严格一致 —— 由
   ``tests/integration/test_migration_matches_metadata.py`` 逐列/逐约束比对强制。
2. **枚举取值在本 revision 冻结**（VARCHAR(n) + CHECK）：``opinion_stance`` /
   ``opinion_horizon`` / ``information_type`` / ``propagation_relation``。
   新增取值必须写新的 migration（禁止修改已发布 revision）。
3. **时间因果 CHECK（单表可强制部分）**：
   ``created_at >= effective_at``（观点）、``created_at >= detected_at``（传播边）、
   ``created_at >= as_of``（技能 / 权重快照）——禁止任何写入路径自称"在未来才可用"。
   跨表不变式 ``author_opinions.effective_at >= author_posts.effective_at`` 无法用单表
   CHECK 表达，沿用 Phase 1 ``processed_items`` 的口径：由提取器层保证 + leakage 测试强制。
4. **幂等自然键（唯一约束）**：传播边 ``(from_item_id, to_item_id, relation_type,
   model_version)``；技能快照 ``(author_id, as_of)``；权重快照 ``(author_id, regime_type,
   horizon, information_type, as_of)``（维度缺省用哨兵 ``'ANY'``，因为唯一约束对 NULL 不去重）。
5. **4 张表全部 append-only**：``database/protection.py`` 已注册不可覆盖守卫
   （03 §10 明确列出其中三张；propagation_edges 为派生事实，一并保护）。
6. ``downgrade`` 严格逆序 drop 表与索引，可完整回滚。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from database.types import (
    JSONB,
    PRICE,
    PROBABILITY,
    RETURN_RATE,
    TIMESTAMP,
    UUID,
)

# revision identifiers, used by Alembic.
revision: str = "0005_phase2_author_lab_tables"
down_revision: str | None = "0004_collector_run_warnings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str, length: int) -> sa.Enum:
    """构造 VARCHAR(length) + CHECK 枚举类型（取值在本文件冻结）。"""
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
    )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) 作者观点（04 §10）
    # ------------------------------------------------------------------
    op.create_table(
        "author_opinions",
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("author_post_id", UUID, nullable=False),
        sa.Column(
            "stance",
            _enum("LONG", "SHORT", "FLAT", "UNKNOWN", name="opinion_stance", length=20),
            nullable=False,
        ),
        sa.Column("instrument_id", UUID, nullable=True),
        sa.Column(
            "horizon",
            _enum("15m", "30m", "1h", "4h", "1d", name="opinion_horizon", length=20),
            nullable=True,
        ),
        sa.Column("confidence", PROBABILITY, nullable=True),
        sa.Column("entry_low", PRICE, nullable=True),
        sa.Column("entry_high", PRICE, nullable=True),
        sa.Column("stop_loss", PRICE, nullable=True),
        sa.Column("take_profit", PRICE, nullable=True),
        sa.Column(
            "information_type",
            _enum(
                "MACRO",
                "TECHNICAL",
                "NEWS",
                "SENTIMENT",
                "POSITIONING",
                "OTHER",
                name="information_type",
                length=50,
            ),
            nullable=True,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(length=50), nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_post_id"], ["author_posts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("created_at >= effective_at", name="created_at_ge_effective_at"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_in_unit_range",
        ),
        sa.CheckConstraint(
            "entry_low IS NULL OR entry_high IS NULL OR entry_low <= entry_high",
            name="entry_range_ordered",
        ),
        sa.CheckConstraint(
            "(entry_low IS NULL OR entry_low > 0)"
            " AND (entry_high IS NULL OR entry_high > 0)"
            " AND (stop_loss IS NULL OR stop_loss > 0)"
            " AND (take_profit IS NULL OR take_profit > 0)",
            name="prices_positive",
        ),
    )

    # ------------------------------------------------------------------
    # 2) 信息传播关系（04 §11）
    # ------------------------------------------------------------------
    op.create_table(
        "propagation_edges",
        sa.Column("from_item_id", UUID, nullable=False),
        sa.Column("to_item_id", UUID, nullable=False),
        sa.Column(
            "relation_type",
            _enum(
                "REPOST",
                "QUOTE",
                "SEMANTIC_SIMILAR",
                "SAME_SOURCE",
                name="propagation_relation",
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("similarity", PROBABILITY, nullable=True),
        sa.Column("confidence", PROBABILITY, nullable=True),
        sa.Column("detected_at", TIMESTAMP, nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["from_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "from_item_id",
            "to_item_id",
            "relation_type",
            "model_version",
            name="uq_propagation_edges_relation_pair_model",
        ),
        sa.CheckConstraint("from_item_id <> to_item_id", name="no_self_edge"),
        sa.CheckConstraint("created_at >= detected_at", name="created_at_ge_detected_at"),
        sa.CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)",
            name="similarity_in_unit_range",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_in_unit_range",
        ),
    )

    # ------------------------------------------------------------------
    # 3) 作者技能快照（04 §19）
    # ------------------------------------------------------------------
    op.create_table(
        "author_skill_snapshots",
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("as_of", TIMESTAMP, nullable=False),
        sa.Column("direction_skill", PROBABILITY, nullable=True),
        sa.Column("timing_skill", PROBABILITY, nullable=True),
        sa.Column("entry_skill", PROBABILITY, nullable=True),
        sa.Column("exit_skill", PROBABILITY, nullable=True),
        sa.Column("independence_score", PROBABILITY, nullable=True),
        sa.Column("calibration_score", PROBABILITY, nullable=True),
        sa.Column("marginal_alpha", RETURN_RATE, nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("author_id", "as_of", name="uq_author_skill_snapshots_author_as_of"),
        sa.CheckConstraint("created_at >= as_of", name="created_at_ge_as_of"),
        sa.CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
        sa.CheckConstraint(
            "(direction_skill IS NULL OR (direction_skill >= 0 AND direction_skill <= 1))"
            " AND (timing_skill IS NULL OR (timing_skill >= 0 AND timing_skill <= 1))"
            " AND (entry_skill IS NULL OR (entry_skill >= 0 AND entry_skill <= 1))"
            " AND (exit_skill IS NULL OR (exit_skill >= 0 AND exit_skill <= 1))"
            " AND (independence_score IS NULL OR (independence_score >= 0"
            " AND independence_score <= 1))"
            " AND (calibration_score IS NULL OR (calibration_score >= 0"
            " AND calibration_score <= 1))",
            name="scores_in_unit_range",
        ),
    )

    # ------------------------------------------------------------------
    # 4) 作者动态权重快照（04 §20）
    # ------------------------------------------------------------------
    op.create_table(
        "author_weight_snapshots",
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("regime_type", sa.String(length=40), nullable=False),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("information_type", sa.String(length=50), nullable=False),
        sa.Column("weight", PROBABILITY, nullable=False),
        sa.Column("exploration_weight", PROBABILITY, nullable=True),
        sa.Column("as_of", TIMESTAMP, nullable=False),
        sa.Column("reason_json", JSONB, nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "author_id",
            "regime_type",
            "horizon",
            "information_type",
            "as_of",
            name="uq_author_weight_snapshots_context_as_of",
        ),
        sa.CheckConstraint("created_at >= as_of", name="created_at_ge_as_of"),
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="weight_in_unit_range"),
        sa.CheckConstraint(
            "exploration_weight IS NULL OR (exploration_weight >= 0 AND exploration_weight <= 1)",
            name="exploration_weight_in_unit_range",
        ),
    )

    # ------------------------------------------------------------------
    # 5) 索引（名称与 ORM 元数据一一对应，由漂移测试强制校验）
    # ------------------------------------------------------------------
    op.create_index("ix_author_opinions_author_post_id", "author_opinions", ["author_post_id"])
    op.create_index("ix_author_opinions_instrument_id", "author_opinions", ["instrument_id"])
    op.create_index(
        "ix_author_opinions_author_effective_at",
        "author_opinions",
        ["author_id", "effective_at"],
    )
    op.create_index("ix_author_opinions_stance_horizon", "author_opinions", ["stance", "horizon"])
    op.create_index("ix_propagation_edges_from_item_id", "propagation_edges", ["from_item_id"])
    op.create_index("ix_propagation_edges_to_item_id", "propagation_edges", ["to_item_id"])


def downgrade() -> None:
    """严格逆序回滚：先删索引，再按依赖倒序删表。"""
    op.drop_index("ix_propagation_edges_to_item_id", table_name="propagation_edges")
    op.drop_index("ix_propagation_edges_from_item_id", table_name="propagation_edges")
    op.drop_index("ix_author_opinions_stance_horizon", table_name="author_opinions")
    op.drop_index("ix_author_opinions_author_effective_at", table_name="author_opinions")
    op.drop_index("ix_author_opinions_instrument_id", table_name="author_opinions")
    op.drop_index("ix_author_opinions_author_post_id", table_name="author_opinions")
    op.drop_table("author_weight_snapshots")
    op.drop_table("author_skill_snapshots")
    op.drop_table("propagation_edges")
    op.drop_table("author_opinions")
