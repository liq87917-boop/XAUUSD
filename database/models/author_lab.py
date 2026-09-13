"""Phase 2（Author Lab）研究事实表（04 §10 author_opinions / §11 propagation_edges /
§19 author_skill_snapshots / §20 author_weight_snapshots）。

对应文档：
- 05_分阶段开发路线图 Phase 2：把"作者说了什么"转成结构化、可验证的预测事件；
- 07_每个阶段的Cline执行Prompt Phase 2：必须建立这 4 张表；所有 LLM/解析器输出
  必须记录 parser/model version；所有测试必须避免 future leakage；
- 03_数据库完整设计 §8（索引建议）、§10（不可覆盖原则）、§14（数值类型）。

设计决策（全部显式记录，避免"隐含约定"）：

1. **append-only**：4 张表全部注册"不可覆盖"守卫（``database/protection.py``）——
   03 §10 明确列出 author_opinions / author_skill_snapshots / author_weight_snapshots；
   propagation_edges 同为派生事实，一并保护。修正方式 = 新 ``parser_version`` /
   ``model_version`` 产生新行，历史行永不改写。
2. **时间因果下沉到数据库层（可强制部分）**：
   - ``created_at >= effective_at``（观点）、``created_at >= detected_at``（传播边）、
     ``created_at >= as_of``（技能 / 权重快照）：禁止写入"自称在未来才可用"的记录；
   - ``effective_at >= 上游 author_posts.effective_at`` 是**跨表**约束，单表 CHECK 无法
     表达，沿用 Phase 1 ``processed_items`` 的同一口径：由提取器层保证 + leakage 测试强制。
3. **NULL 语义**：分值 / 价格 / 置信度一律可空，NULL = "未计算 / 未给出"，
   **禁止用 0 冒充**（与 raw_items.published_at 允许 NULL 的口径一致）。
4. **枚举统一 VARCHAR + CHECK**（``database.types.enum_type``）；``horizon`` 只允许
   Phase 2 的五个评价窗口（15m/30m/1h/4h/1d），不含 1m / 5m。
5. **幂等自然键**：
   - propagation_edges: ``(from_item_id, to_item_id, relation_type, model_version)``；
   - author_skill_snapshots: ``(author_id, as_of)``；
   - author_weight_snapshots: ``(author_id, regime_type, horizon, information_type, as_of)``
     （维度缺省用哨兵 ``'ANY'``，因为 SQL 唯一约束对 NULL 不去重）；
   - author_opinions 的幂等由上游 ``processed_items(raw_item_id, processor_name,
     processor_version)`` 唯一键 + 提取器前置检查保证（一条帖子可产生多个标的/方向的
     观点，故自身不设唯一键）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.models.enums import (
    WEIGHT_CONTEXT_ANY,
    InformationType,
    OpinionHorizon,
    OpinionStance,
    PropagationRelation,
)
from database.types import (
    JSONB,
    PRICE,
    PROBABILITY,
    RETURN_RATE,
    TIMESTAMP,
    UUID,
    enum_type,
)

__all__ = [
    "AuthorOpinion",
    "AuthorSkillSnapshot",
    "AuthorWeightSnapshot",
    "PropagationEdge",
]


class AuthorOpinion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """作者观点（从 ``author_posts`` 文本抽取的结构化预测事件，04 §10）。"""

    __tablename__ = "author_opinions"
    __table_args__ = (
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
        # 03 §8：author_opinions 索引建议 (author_id, effective_at) 与 (stance, horizon)
        sa.Index("ix_author_opinions_author_effective_at", "author_id", "effective_at"),
        sa.Index("ix_author_opinions_stance_horizon", "stance", "horizon"),
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("authors.id", ondelete="RESTRICT"), nullable=False
    )
    author_post_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("author_posts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    stance: Mapped[OpinionStance] = mapped_column(
        enum_type(OpinionStance, name="opinion_stance", length=20), nullable=False
    )
    # 观点可能不指向具体标的（纯宏观 / 情绪判断）→ 允许 NULL，禁止随便填 XAUUSD
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # 未给出周期时置空（评价阶段按 Phase 2 的五个窗口分别打分，此处不猜）
    horizon: Mapped[OpinionHorizon | None] = mapped_column(
        enum_type(OpinionHorizon, name="opinion_horizon", length=20), nullable=True
    )
    confidence: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    entry_low: Mapped[sa.Numeric | None] = mapped_column(PRICE, nullable=True)
    entry_high: Mapped[sa.Numeric | None] = mapped_column(PRICE, nullable=True)
    stop_loss: Mapped[sa.Numeric | None] = mapped_column(PRICE, nullable=True)
    take_profit: Mapped[sa.Numeric | None] = mapped_column(PRICE, nullable=True)
    information_type: Mapped[InformationType | None] = mapped_column(
        enum_type(InformationType, name="information_type", length=50), nullable=True
    )
    # 理由：必须留下**可复核的原文片段或模型理由**，不得只存结论
    rationale: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # 07 Phase 2 强制：所有 LLM/解析器输出必须记录 parser / model version
    parser_version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    # 观点可用时间：必须 >= 上游 author_posts.effective_at（提取器保证 + leakage 测试）
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)


class PropagationEdge(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """信息传播关系（04 §11）：转载 / 引用 / 语义相似 / 同源。"""

    __tablename__ = "propagation_edges"
    __table_args__ = (
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

    # 节点统一绑定 raw_items：传播关系是"内容之间"的关系，必须可回溯到原始层
    from_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    to_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    relation_type: Mapped[PropagationRelation] = mapped_column(
        enum_type(PropagationRelation, name="propagation_relation", length=50), nullable=False
    )
    similarity: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    confidence: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.String(50), nullable=False)


class AuthorSkillSnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """作者技能快照（04 §19）：某一时点的方向 / 时机 / 入场 / 退出 / 独立性 / 校准。"""

    __tablename__ = "author_skill_snapshots"
    __table_args__ = (
        # 幂等自然键：同一作者同一时点只允许一份快照（重算 = 新 as_of，历史快照保留）
        sa.UniqueConstraint("author_id", "as_of", name="uq_author_skill_snapshots_author_as_of"),
        sa.CheckConstraint("created_at >= as_of", name="created_at_ge_as_of"),
        sa.CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
        # 六项 0~1 分值统一校验（NULL = 未计算，禁止用 0 冒充）
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

    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("authors.id", ondelete="RESTRICT"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    direction_skill: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    timing_skill: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    entry_skill: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    exit_skill: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    independence_score: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    calibration_score: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    # 边际 Alpha 可为负（移除该作者后组合表现变好）→ 用 RETURN_RATE 而非 0~1 的 PROBABILITY
    marginal_alpha: Mapped[sa.Numeric | None] = mapped_column(RETURN_RATE, nullable=True)
    # 样本量：05 Phase 2 要求"防止少量样本高分"，样本量必须与分值一起持久化
    sample_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # 分周期 / Regime 等扩展维度（04 §19 明确放 JSONB，不在本表加列）
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)


class AuthorWeightSnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """作者动态权重快照（04 §20）：Weight(author | regime, horizon, information_type)。"""

    __tablename__ = "author_weight_snapshots"
    __table_args__ = (
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

    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("authors.id", ondelete="RESTRICT"), nullable=False
    )
    # 维度缺省用哨兵 'ANY'（见 database/models/enums.py::WEIGHT_CONTEXT_ANY）：
    # 唯一约束对 NULL 不去重，用 NULL 会让幂等失效。
    regime_type: Mapped[str] = mapped_column(
        sa.String(40), nullable=False, default=WEIGHT_CONTEXT_ANY
    )
    horizon: Mapped[str] = mapped_column(sa.String(20), nullable=False, default=WEIGHT_CONTEXT_ANY)
    information_type: Mapped[str] = mapped_column(
        sa.String(50), nullable=False, default=WEIGHT_CONTEXT_ANY
    )
    weight: Mapped[sa.Numeric] = mapped_column(PROBABILITY, nullable=False)
    # 探索权重（Contextual Bandit / Thompson Sampling 的 exploration 部分）
    exploration_weight: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    reason_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
