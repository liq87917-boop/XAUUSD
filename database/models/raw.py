"""原始数据与媒体（04 §5 raw_items / §6 raw_media）。

红线（.clinerules 第二条 / 03_数据库完整设计 第 10 节）：
    raw_items / raw_media 只追加、不覆盖；修正必须"新版本 + superseded_by_id"。
    该约束由 ``database.protection`` 在 ORM flush 阶段强制拦截。

时间语义（02 §5.4）：
    collected_at 必填；published_at 允许 NULL（但采集器必须在 raw_json 中说明原因）；
    effective_at = max(published_at, collected_at)，由 DB CHECK 兜底校验，防未来数据泄漏。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.models.enums import MediaType, RawItemType
from database.types import JSONB, TIMESTAMP, UUID, enum_type

__all__ = ["RawItem", "RawMedia"]


class RawItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """原始采集记录（不可覆盖，只追加）。

    数据修正路径（03_数据库完整设计 第 10 节）：
        1. 来源平台产生了一条新的记录标识（``source_record_id`` 不同）取代旧记录：
           先插入新记录，再把旧记录的 ``superseded_by_id`` 指向新记录并 flush。
           —— 这是唯一允许的 UPDATE 列，且顺序不能颠倒（外键必须是已存在的主键）。
        2. 加工层问题（清洗/解析错误）：不得改动 raw_items，应通过 ``processed_items``
           以新的 ``processor_version`` 生成新版本。
    注：``(source_id, source_record_id)`` 是严格唯一键（03 第 7 节）。若未来需要
    "同一外部标识的多版本共存"，必须通过新的 Alembic migration 显式引入
    （例如可延迟外键或 superseded 标记列），不得在本 revision 上悄悄修改。
    """

    __tablename__ = "raw_items"
    __table_args__ = (
        # 幂等键（03_数据库完整设计 第 7 节索引重点）：同一来源的同一外部记录只允许一条
        sa.UniqueConstraint("source_id", "source_record_id", name="uq_raw_items_source_record"),
        # 时间语义兜底：effective_at 不得早于发布时间 / 采集时间（防未来数据泄漏）
        sa.CheckConstraint(
            "published_at IS NULL OR published_at <= effective_at",
            name="published_at_le_effective_at",
        ),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
        sa.Index("ix_raw_items_published_at", "published_at"),
        sa.Index("ix_raw_items_collected_at", "collected_at"),
        sa.Index("ix_raw_items_content_hash", "content_hash"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    source_record_id: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    item_type: Mapped[RawItemType] = mapped_column(
        enum_type(RawItemType, name="raw_item_type", length=50), nullable=False
    )
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # 原始响应必须完整保存（08_测试与验收标准 4：原始 JSON 保存）
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=True
    )

    media: Mapped[list[RawMedia]] = relationship(back_populates="raw_item", lazy="select")


class RawMedia(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """原始媒体文件（图片 / 视频 / PDF），仅记录元数据与对象存储地址。"""

    __tablename__ = "raw_media"
    __table_args__ = (
        sa.UniqueConstraint("raw_item_id", "sha256", name="uq_raw_media_item_sha256"),
        sa.CheckConstraint(
            "width IS NULL OR width > 0",
            name="width_positive",
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name="height_positive",
        ),
        sa.Index("ix_raw_media_sha256", "sha256"),
    )

    raw_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    media_type: Mapped[MediaType] = mapped_column(
        enum_type(MediaType, name="media_type", length=30), nullable=False
    )
    original_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    raw_item: Mapped[RawItem] = relationship(back_populates="media", lazy="select")
