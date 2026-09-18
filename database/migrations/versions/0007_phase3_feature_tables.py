"""Phase 3.0 W0-5：特征定义、快照、规范化值与 Regime 追溯表。

Revision ID: 0007_phase3_feature_tables
Revises: 0006_macro_event_vintages
Create Date: 2026-09-18

本 revision 只建立数据契约。Regime 识别逻辑不属于 W0-5。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from database.types import JSONB, PROBABILITY, TIMESTAMP, UUID

revision: str = "0007_phase3_feature_tables"
down_revision: str | None = "0006_macro_event_vintages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str, length: int) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
    )


def upgrade() -> None:
    op.create_table(
        "feature_sets",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column(
            "kind",
            _enum(
                "TECHNICAL",
                "MACRO",
                "NEWS",
                "AUTHOR",
                "MIXED",
                name="feature_set_kind",
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition_json", JSONB, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_feature_sets_name_version"),
    )

    op.create_table(
        "feature_snapshots",
        sa.Column("feature_set_id", UUID, nullable=False),
        sa.Column("instrument_id", UUID, nullable=False),
        sa.Column("as_of", TIMESTAMP, nullable=False),
        sa.Column("max_effective_at", TIMESTAMP, nullable=False),
        sa.Column("values_json", JSONB, nullable=False),
        sa.Column("data_version_id", UUID, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["feature_set_id"], ["feature_sets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["data_version_id"], ["data_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "feature_set_id",
            "instrument_id",
            "as_of",
            "data_version_id",
            name="uq_feature_snapshots_identity",
        ),
        sa.CheckConstraint("max_effective_at <= as_of", name="max_effective_at_le_as_of"),
        sa.CheckConstraint("created_at >= as_of", name="created_at_ge_as_of"),
    )

    op.create_table(
        "feature_values",
        sa.Column("feature_snapshot_id", UUID, nullable=False),
        sa.Column("feature_name", sa.String(length=100), nullable=False),
        sa.Column("value_numeric", sa.Numeric(precision=28, scale=12), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"], ["feature_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "feature_snapshot_id", "feature_name", name="uq_feature_values_snapshot_name"
        ),
        sa.CheckConstraint(
            "(CASE WHEN value_numeric IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_bool IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="exactly_one_typed_value",
        ),
    )

    op.create_table(
        "market_regimes",
        sa.Column("instrument_id", UUID, nullable=False),
        sa.Column(
            "regime_type",
            _enum(
                "TREND_UP",
                "TREND_DOWN",
                "RANGE",
                "HIGH_VOLATILITY",
                "LOW_VOLATILITY",
                "NEWS_DRIVEN",
                "UNKNOWN",
                name="regime",
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("confidence", PROBABILITY, nullable=False),
        sa.Column("start_at", TIMESTAMP, nullable=False),
        sa.Column("end_at", TIMESTAMP, nullable=True),
        sa.Column("detected_at", TIMESTAMP, nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=False),
        sa.Column("feature_snapshot_id", UUID, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"], ["feature_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "instrument_id", "start_at", "model_version", name="uq_market_regimes_identity"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_unit_range"),
        sa.CheckConstraint("end_at IS NULL OR end_at > start_at", name="end_after_start"),
        sa.CheckConstraint("detected_at >= start_at", name="detected_at_ge_start_at"),
        sa.CheckConstraint("created_at >= detected_at", name="created_at_ge_detected_at"),
    )

    op.create_index(
        "ix_feature_snapshots_instrument_as_of",
        "feature_snapshots",
        ["instrument_id", "as_of"],
    )
    op.create_index("ix_feature_values_feature_name", "feature_values", ["feature_name"])
    op.create_index(
        "ix_market_regimes_instrument_start_at", "market_regimes", ["instrument_id", "start_at"]
    )
    op.create_index("ix_market_regimes_detected_at", "market_regimes", ["detected_at"])


def downgrade() -> None:
    op.drop_index("ix_market_regimes_detected_at", table_name="market_regimes")
    op.drop_index("ix_market_regimes_instrument_start_at", table_name="market_regimes")
    op.drop_index("ix_feature_values_feature_name", table_name="feature_values")
    op.drop_index("ix_feature_snapshots_instrument_as_of", table_name="feature_snapshots")
    op.drop_table("market_regimes")
    op.drop_table("feature_values")
    op.drop_table("feature_snapshots")
    op.drop_table("feature_sets")
