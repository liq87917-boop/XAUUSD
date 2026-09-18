"""Phase 3.0 特征快照与市场状态表（W0-5）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.models.enums import FeatureSetKind, Regime
from database.types import JSONB, PROBABILITY, TIMESTAMP, UUID, enum_type

__all__ = ["FeatureSet", "FeatureSnapshot", "FeatureValue", "MarketRegime"]


class FeatureSet(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """有版本的特征定义；定义变化必须新增版本。"""

    __tablename__ = "feature_sets"
    __table_args__ = (sa.UniqueConstraint("name", "version", name="uq_feature_sets_name_version"),)

    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    kind: Mapped[FeatureSetKind] = mapped_column(
        enum_type(FeatureSetKind, name="feature_set_kind", length=30), nullable=False
    )
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    snapshots: Mapped[list[FeatureSnapshot]] = relationship(back_populates="feature_set")


class FeatureSnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """某个 ``as_of`` 时点可见的不可变特征快照。"""

    __tablename__ = "feature_snapshots"
    __table_args__ = (
        sa.UniqueConstraint(
            "feature_set_id",
            "instrument_id",
            "as_of",
            "data_version_id",
            name="uq_feature_snapshots_identity",
        ),
        sa.CheckConstraint("max_effective_at <= as_of", name="max_effective_at_le_as_of"),
        sa.CheckConstraint("created_at >= as_of", name="created_at_ge_as_of"),
        sa.Index("ix_feature_snapshots_instrument_as_of", "instrument_id", "as_of"),
    )

    feature_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("feature_sets.id", ondelete="RESTRICT"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    max_effective_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    values_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("data_versions.id", ondelete="RESTRICT"), nullable=False
    )

    feature_set: Mapped[FeatureSet] = relationship(back_populates="snapshots")
    values: Mapped[list[FeatureValue]] = relationship(back_populates="snapshot")


class FeatureValue(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """特征快照的规范化、带类型单值。不可用值不写入。"""

    __tablename__ = "feature_values"
    __table_args__ = (
        sa.UniqueConstraint(
            "feature_snapshot_id", "feature_name", name="uq_feature_values_snapshot_name"
        ),
        sa.CheckConstraint(
            "(CASE WHEN value_numeric IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_bool IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="exactly_one_typed_value",
        ),
        sa.Index("ix_feature_values_feature_name", "feature_name"),
    )

    feature_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("feature_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    feature_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    value_numeric: Mapped[sa.Numeric | None] = mapped_column(sa.Numeric(28, 12), nullable=True)
    value_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    value_bool: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    snapshot: Mapped[FeatureSnapshot] = relationship(back_populates="values")


class MarketRegime(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """市场状态识别结果；W0-5 仅提供可追溯结构。"""

    __tablename__ = "market_regimes"
    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_id", "start_at", "model_version", name="uq_market_regimes_identity"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_unit_range"),
        sa.CheckConstraint("end_at IS NULL OR end_at > start_at", name="end_after_start"),
        sa.CheckConstraint("detected_at >= start_at", name="detected_at_ge_start_at"),
        sa.CheckConstraint("created_at >= detected_at", name="created_at_ge_detected_at"),
        sa.Index("ix_market_regimes_instrument_start_at", "instrument_id", "start_at"),
        sa.Index("ix_market_regimes_detected_at", "detected_at"),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    regime_type: Mapped[Regime] = mapped_column(
        enum_type(Regime, name="regime", length=40), nullable=False
    )
    confidence: Mapped[sa.Numeric] = mapped_column(PROBABILITY, nullable=False)
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    feature_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("feature_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
