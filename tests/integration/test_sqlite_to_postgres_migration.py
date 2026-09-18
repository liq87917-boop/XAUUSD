"""SQLite 研究快照迁移器的事务、时间和内容指纹测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from database.models import Base, RawItem, Source
from database.models.enums import RawItemType, SourceType
from database.session import build_engine
from scripts.migrate_sqlite_to_postgres import audit_database, migrate_database
from src.common.hashing import content_hash
from src.common.uuid7 import uuid7

pytestmark = pytest.mark.integration


def _empty_database(path: Path) -> sa.Engine:
    engine = build_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    return engine


def test_migration_preserves_rows_hashes_and_self_reference(tmp_path: Path) -> None:
    source = _empty_database(tmp_path / "source.db")
    target = _empty_database(tmp_path / "target.db")
    known_at = datetime(2026, 9, 17, 8, tzinfo=UTC)
    with source.begin() as connection:
        source_row = Source(
            id=uuid7(),
            name="migration-source",
            source_type=SourceType.NEWS,
            base_url="https://example.test",
            enabled=True,
            config_json={"nested": {"ok": True}},
        )
        connection.execute(
            Source.__table__.insert(),
            {
                "id": source_row.id,
                "name": source_row.name,
                "source_type": source_row.source_type,
                "base_url": source_row.base_url,
                "enabled": source_row.enabled,
                "config_json": source_row.config_json,
                "created_at": known_at,
                "updated_at": known_at,
                "is_deleted": False,
                "deleted_at": None,
            },
        )
        newer = RawItem(
            id=uuid7(),
            source_id=source_row.id,
            source_record_id="newer",
            item_type=RawItemType.NEWS,
            raw_json={"version": 2},
            content_hash=content_hash("newer"),
            collected_at=known_at,
            effective_at=known_at,
        )
        older = RawItem(
            id=uuid7(),
            source_id=source_row.id,
            source_record_id="older",
            item_type=RawItemType.NEWS,
            raw_json={"version": 1},
            content_hash=content_hash("older"),
            collected_at=known_at,
            effective_at=known_at,
            superseded_by_id=newer.id,
        )
        for item in (newer, older):
            connection.execute(
                RawItem.__table__.insert(),
                {
                    "id": item.id,
                    "source_id": item.source_id,
                    "source_record_id": item.source_record_id,
                    "item_type": item.item_type,
                    "title": None,
                    "content_text": None,
                    "raw_json": item.raw_json,
                    "content_hash": item.content_hash,
                    "published_at": None,
                    "collected_at": item.collected_at,
                    "effective_at": item.effective_at,
                    "superseded_by_id": item.superseded_by_id,
                    "created_at": known_at,
                },
            )

    audits = migrate_database(source, target)

    assert all(item.passed for item in audits)
    assert audit_database(source) == audit_database(target)
    with target.connect() as connection:
        migrated = connection.execute(
            sa.select(RawItem.__table__).where(RawItem.__table__.c.source_record_id == "older")
        ).mappings().one()
    assert migrated["superseded_by_id"] == newer.id
    source.dispose()
    target.dispose()


def test_migration_refuses_nonempty_target(tmp_path: Path) -> None:
    source = _empty_database(tmp_path / "source.db")
    target = _empty_database(tmp_path / "target.db")
    with target.begin() as connection:
        connection.execute(
            Source.__table__.insert(),
            {
                "name": "already-there",
                "source_type": SourceType.NEWS,
                "enabled": True,
                "config_json": {},
            },
        )

    with pytest.raises(ValueError, match="目标数据库必须为空"):
        migrate_database(source, target)

    source.dispose()
    target.dispose()
