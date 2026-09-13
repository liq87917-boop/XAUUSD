"""市场行情（04 §12 instruments / §13 market_bars）。

对应文档：
- 05_分阶段开发路线图 Phase 1.5：首期至少 XAUUSD / DXY / 美债收益率 / USD-CNY，
  可追加 COMEX Gold / Silver / Oil / BTC —— 全部通过 ``instruments`` 字典表扩展，
  数据结构不因新增标的变化。
- 08_测试与验收标准 4 Market Bar：OHLC 关系、price > 0、K 线时间不重复、
  timeframe 正确、时区正确 —— 本文件用 DB CHECK 约束在写入层强制。

时区：``open_time`` / ``close_time`` / ``collected_at`` / ``effective_at`` 全部为
TIMESTAMPTZ（UTC 存储），本地市场时区只保存在 ``instruments.timezone`` 供展示与对齐使用。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import (
    Base,
    CreatedAtMixin,
    SoftDeleteMixin,
    UpdatedAtMixin,
    UUIDPrimaryKeyMixin,
)
from database.models.enums import AssetClass, Timeframe
from database.types import PRICE, TIMESTAMP, UUID, VOLUME, enum_type

__all__ = ["Instrument", "MarketBar"]


class Instrument(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, SoftDeleteMixin, Base):
    """可交易/可研究标的字典表（XAUUSD、DXY、US10Y、USDCNY、COMEX_GOLD ...）。"""

    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(sa.String(50), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    asset_class: Mapped[AssetClass] = mapped_column(
        enum_type(AssetClass, name="asset_class", length=50), nullable=False
    )
    quote_currency: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    timezone: Mapped[str] = mapped_column(sa.String(50), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    bars: Mapped[list[MarketBar]] = relationship(back_populates="instrument", lazy="select")


class MarketBar(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """K 线数据（append-only，禁止覆盖历史行情）。"""

    __tablename__ = "market_bars"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "open_time",
            name="uq_market_bars_instrument_timeframe_open",
        ),
        # 团队批复新增（migration 0003）：按来源的行情唯一索引。
        # 说明：market_bars 没有 symbol 列（04 §13 用 instrument_id 关联 instruments，
        # 而 instruments.symbol 本身唯一 → symbol 与 instrument_id 一一对应），因此这里用
        # instrument_id 表达 symbol。该索引在当前"更严格的"约束下是被隐含的，
        # 它的价值是显式表达"同一来源内同一根 K 线绝不重复"的业务语义，并为未来
        # 支持"多来源行情共存"预留保护（届时只需放宽严格约束，无需再补索引）。
        sa.Index(
            "uq_market_bars_source_instrument_timeframe_open",
            "source_id",
            "instrument_id",
            "timeframe",
            "open_time",
            unique=True,
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc_bounds_valid",
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="prices_positive",
        ),
        sa.CheckConstraint("close_time > open_time", name="bar_time_order"),
        # 行情也只有"采集到"之后才允许使用，防止用未来 K 线训练/回测
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
        sa.CheckConstraint("volume IS NULL OR volume >= 0", name="volume_non_negative"),
        sa.Index(
            "ix_market_bars_instrument_timeframe_close_time",
            "instrument_id",
            "timeframe",
            "close_time",
        ),
        sa.Index("ix_market_bars_open_time", "open_time"),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        enum_type(Timeframe, name="timeframe", length=10), nullable=False
    )
    open_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    close_time: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    open: Mapped[sa.Numeric] = mapped_column("open", PRICE, nullable=False)
    high: Mapped[sa.Numeric] = mapped_column("high", PRICE, nullable=False)
    low: Mapped[sa.Numeric] = mapped_column("low", PRICE, nullable=False)
    close: Mapped[sa.Numeric] = mapped_column("close", PRICE, nullable=False)
    volume: Mapped[sa.Numeric | None] = mapped_column(VOLUME, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=True
    )
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    instrument: Mapped[Instrument] = relationship(back_populates="bars", lazy="select")
