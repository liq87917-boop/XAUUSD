"""信息源与作者（04 §2 sources / §3 authors / §4 author_accounts）。

Phase 1 目标：把"稳定数据采集"的上游主体建立起来——首期支持配置 50~100 位作者。
设计决策：
- ``sources.name`` / ``authors.canonical_name`` 建唯一约束：没有自然键就无法
  幂等地初始化数据源与作者（重复执行不得产生重复行）。
- ``authors.status`` 使用 02 §5.6 的四态（ACTIVE/EXPLORATION/PROBATION/DISABLED），
  低权重作者进入 PROBATION 而不是永久删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import (
    Base,
    CreatedAtMixin,
    SoftDeleteMixin,
    UpdatedAtMixin,
    UUIDPrimaryKeyMixin,
)
from database.models.enums import AuthorStatus, SourceType
from database.types import JSONB, TIMESTAMP, UUID, enum_type

__all__ = ["Author", "AuthorAccount", "Source"]


class Source(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, SoftDeleteMixin, Base):
    """数据来源：微博 / 新闻 / 行情 / 宏观。"""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, name="source_type", length=50), nullable=False
    )
    base_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    timezone: Mapped[str] = mapped_column(sa.String(50), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)

    author_accounts: Mapped[list[AuthorAccount]] = relationship(
        back_populates="source", lazy="select"
    )


class Author(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, SoftDeleteMixin, Base):
    """财经作者（信息源主体）。Phase 1 阶段不做任何打分，只维护身份与状态。"""

    __tablename__ = "authors"

    display_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    canonical_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True, unique=True)
    status: Mapped[AuthorStatus] = mapped_column(
        enum_type(AuthorStatus, name="author_status", length=30),
        nullable=False,
        default=AuthorStatus.ACTIVE,
    )
    style_embedding_ref: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)

    accounts: Mapped[list[AuthorAccount]] = relationship(
        back_populates="author", lazy="select"
    )


class AuthorAccount(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, SoftDeleteMixin, Base):
    """作者在某平台的具体账号（同一作者可有多个平台账号）。"""

    __tablename__ = "author_accounts"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_id",
            "external_account_id",
            name="uq_author_accounts_platform_account",
        ),
    )

    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("authors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_account_id: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    account_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    follower_count: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    last_collected_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)

    author: Mapped[Author] = relationship(back_populates="accounts", lazy="select")
    source: Mapped[Source] = relationship(back_populates="author_accounts", lazy="select")
