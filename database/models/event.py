"""新闻与宏观事件（04 §14 news_events / §15 macro_events）。

Phase 1 只做"采集 + 结构化落库"，不做情绪建模与 Alpha（属于 Phase 2/3）。

时间语义（宏观数据的因果关键）：
- ``macro_events.event_at``：观测所属期（observation date），不是发布时间。
- ``macro_events.released_at``：该 vintage 可被研究层使用的保守起点。
- ``macro_events.vintage_end_at``：该 vintage 被修订替代的排他边界；NULL 表示仍有效。
- ``macro_events.effective_at``：系统实际允许使用的最早时间，必须不早于
  ``released_at`` 与 ``collected_at``。
- 新闻：``published_at`` 必填，``effective_at`` >= max(published_at, collected_at)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.types import MACRO_VALUE, PROBABILITY, PROBABILITY_ANY, TIMESTAMP, UUID

__all__ = ["MacroEvent", "NewsEvent"]


class NewsEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """结构化新闻事件（由 raw_items 加工而来，append-only）。"""

    __tablename__ = "news_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "raw_item_id", "parser_version", name="uq_news_events_raw_item_parser_version"
        ),
        sa.CheckConstraint(
            "importance IS NULL OR (importance >= 0 AND importance <= 1)",
            name="importance_probability_range",
        ),
        sa.CheckConstraint(
            "sentiment IS NULL OR (sentiment >= -1 AND sentiment <= 1)",
            name="sentiment_range",
        ),
        sa.CheckConstraint("published_at <= effective_at", name="published_at_le_effective_at"),
        sa.Index("ix_news_events_published_at", "published_at"),
        sa.Index("ix_news_events_event_type", "event_type"),
    )

    raw_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("raw_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    headline: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    event_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    importance: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY, nullable=True)
    sentiment: Mapped[sa.Numeric | None] = mapped_column(PROBABILITY_ANY, nullable=True)
    event_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    published_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    # 解析器版本：任何加工结果必须可追溯到具体解析版本（06_Cline开发规则 第 13 条）
    parser_version: Mapped[str] = mapped_column(sa.String(50), nullable=False)


class MacroEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """宏观事件（CPI / PCE / 非农 / FOMC / 央行相关等）。"""

    __tablename__ = "macro_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_id",
            "event_code",
            "country",
            "event_at",
            "released_at",
            name="uq_macro_events_source_code_country_event_release",
        ),
        sa.CheckConstraint("released_at <= effective_at", name="released_at_le_effective_at"),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
        sa.CheckConstraint(
            "vintage_end_at IS NULL OR vintage_end_at > released_at",
            name="vintage_end_after_release",
        ),
        sa.Index("ix_macro_events_event_at", "event_at"),
        sa.Index("ix_macro_events_code_country", "event_code", "country"),
        sa.Index("ix_macro_events_release_window", "released_at", "vintage_end_at"),
    )

    # 事件代码保持自由文本：CPI / PCE / NFP / FOMC / ECB_RATE / BOJ_RATE ...
    event_code: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    country: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    event_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    released_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    vintage_end_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    actual_value: Mapped[sa.Numeric | None] = mapped_column(MACRO_VALUE, nullable=True)
    forecast_value: Mapped[sa.Numeric | None] = mapped_column(MACRO_VALUE, nullable=True)
    previous_value: Mapped[sa.Numeric | None] = mapped_column(MACRO_VALUE, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
