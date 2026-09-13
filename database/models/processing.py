"""结构化加工结果（04 §8 processed_items / §9 author_posts）。

对应文档：
- 02 §5.2：Processor 输出不得覆盖原始数据，必须记录 processor_name / processor_version。
- 06_Cline开发规则 第 13 条：处理结果必须可追溯（processor_name + version + status 等）。

幂等设计：
    processed_items 以 (raw_item_id, processor_name, processor_version) 唯一。
    重新运行同一处理器版本不会产生重复行；处理器升级（version 变化）则生成新行，
    历史结果永久保留，保证"数据可追溯、结果可复现"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.models.enums import ProcessStatus
from database.types import JSONB, TIMESTAMP, UUID, enum_type

__all__ = ["AuthorPost", "ProcessedItem"]


class ProcessedItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """清洗 / 解析后的加工结果（append-only，新版本不覆盖旧版本）。"""

    __tablename__ = "processed_items"
    __table_args__ = (
        sa.UniqueConstraint(
            "raw_item_id",
            "processor_name",
            "processor_version",
            name="uq_processed_items_processor_version",
        ),
        sa.Index("ix_processed_items_status_created_at", "status", "created_at"),
    )

    raw_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    processor_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    processor_version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    language: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    structured_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ProcessStatus] = mapped_column(
        enum_type(ProcessStatus, name="process_status", length=30),
        nullable=False,
        default=ProcessStatus.PENDING,
    )
    # 加工结果的最早可用时间，必须 >= 对应 raw_item.effective_at（由处理器层保证并测试）
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)


class AuthorPost(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """作者帖子（RawItem → 作者归属的结构化结果）。

    说明：
    - ``raw_item_id`` 唯一：一条原始记录只能归属到一个作者帖子，保证重复采集幂等。
    - Phase 1 只做归属与文本/媒体标记，观点抽取（author_opinions）属于 Phase 2。
    """

    __tablename__ = "author_posts"
    __table_args__ = (
        sa.UniqueConstraint("raw_item_id", name="uq_author_posts_raw_item"),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at <= effective_at",
            name="published_at_le_effective_at",
        ),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
        sa.Index("ix_author_posts_author_published_at", "author_id", "published_at"),
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("authors.id", ondelete="RESTRICT"), nullable=False
    )
    author_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("author_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    raw_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    text_content: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    has_media: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
