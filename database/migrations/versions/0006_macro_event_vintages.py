"""Phase 3.0 W0-2：macro_events 增加 ALFRED vintage 时间语义。

Revision ID: 0006_macro_event_vintages
Revises: 0005_phase2_author_lab_tables
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from database.types import TIMESTAMP

revision: str = "0006_macro_event_vintages"
down_revision: str | None = "0005_phase2_author_lab_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加 vintage 字段；旧行用既有 effective_at 作保守发布时间，绝不提前可见。"""
    with op.batch_alter_table("macro_events") as batch:
        batch.add_column(sa.Column("released_at", TIMESTAMP, nullable=True))
        batch.add_column(sa.Column("vintage_end_at", TIMESTAMP, nullable=True))
    op.execute("UPDATE macro_events SET released_at = effective_at WHERE released_at IS NULL")
    with op.batch_alter_table("macro_events") as batch:
        batch.alter_column("released_at", existing_type=TIMESTAMP, nullable=False)
        batch.drop_constraint("uq_macro_events_source_code_country_event_at", type_="unique")
        batch.drop_constraint("event_at_le_effective_at", type_="check")
        batch.create_unique_constraint(
            "uq_macro_events_source_code_country_event_release",
            ["source_id", "event_code", "country", "event_at", "released_at"],
        )
        batch.create_check_constraint(
            "released_at_le_effective_at", "released_at <= effective_at"
        )
        batch.create_check_constraint(
            "vintage_end_after_release",
            "vintage_end_at IS NULL OR vintage_end_at > released_at",
        )
        batch.create_index(
            "ix_macro_events_release_window", ["released_at", "vintage_end_at"], unique=False
        )


def downgrade() -> None:
    """回滚字段；多 vintage 时确定性保留最早 release，以满足旧唯一键。"""
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT source_id, event_code, country, event_at, MIN(released_at) AS keep_release "
            "FROM macro_events GROUP BY source_id, event_code, country, event_at "
            "HAVING COUNT(*) > 1"
        )
    ).mappings()
    for row in duplicates:
        connection.execute(
            sa.text(
                "DELETE FROM macro_events WHERE source_id = :source_id "
                "AND event_code = :event_code AND country = :country "
                "AND event_at = :event_at AND released_at <> :keep_release"
            ),
            dict(row),
        )
    with op.batch_alter_table("macro_events") as batch:
        batch.drop_index("ix_macro_events_release_window")
        batch.drop_constraint("vintage_end_after_release", type_="check")
        batch.drop_constraint("released_at_le_effective_at", type_="check")
        batch.drop_constraint("uq_macro_events_source_code_country_event_release", type_="unique")
        batch.create_unique_constraint(
            "uq_macro_events_source_code_country_event_at",
            ["source_id", "event_code", "country", "event_at"],
        )
        batch.create_check_constraint("event_at_le_effective_at", "event_at <= effective_at")
        batch.drop_column("vintage_end_at")
        batch.drop_column("released_at")