"""研究事实表「不可覆盖」守卫测试。

对应红线：
- .clinerules 第二条：严禁覆盖原始数据
- 06_Cline开发规则 第 9 条：修正必须"新版本 + superseded_by + provenance"
- 03_数据库完整设计 第 10 节：禁止 UPDATE 覆盖主要研究事实表

守卫在 ORM flush 阶段强制执行（database/protection.py），与数据库后端无关，
因此在 SQLite 上通过即代表 PostgreSQL 上行为一致。
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from database.models import ProcessedItem, ProcessStatus
from src.common.exceptions import ImmutableRecordError
from src.common.uuid7 import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def test_protected_tables_are_registered() -> None:
    import database.models as models

    assert set(models.PROTECTED_TABLES) == {
        # Phase 1 原始 / 加工事实表
        "raw_items",
        "raw_media",
        "processed_items",
        "data_versions",
        "macro_events",
        # Phase 2（Author Lab）派生事实表：一律 append-only
        "author_opinions",
        "propagation_edges",
        "author_skill_snapshots",
        "author_weight_snapshots",
    }


def test_raw_item_content_update_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    item.content_text = "被篡改的历史内容"
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_raw_item_raw_json_update_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    item.raw_json = {"tampered": True}
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_raw_item_delete_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    session.delete(item)
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_superseded_by_id_is_the_only_allowed_update(session, make_raw_item) -> None:
    """修正流程（03_数据库完整设计 第 10 节）：①新记录入库 ②旧记录指向新记录。

    只有 superseded_by_id 允许被 UPDATE，其余任何列变更都会被守卫拒绝。
    """
    original = make_raw_item()
    corrected = make_raw_item(source_id=original.source_id, content_text="修正后的正文")

    original.superseded_by_id = corrected.id
    session.flush()

    assert original.superseded_by_id == corrected.id
    assert corrected.superseded_by_id is None


def test_supersede_pointer_requires_existing_target(session, make_raw_item) -> None:
    """外键兜底：superseded_by_id 不得指向不存在的记录（防止悬空引用）。"""
    original = make_raw_item()
    original.superseded_by_id = uuid7()
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_processed_item_update_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    processed = ProcessedItem(
        raw_item_id=item.id,
        processor_name="text_normalizer",
        processor_version="1.0.0",
        normalized_text="cleaned",
        status=ProcessStatus.SUCCESS,
        effective_at=item.effective_at,
    )
    session.add(processed)
    session.flush()

    processed.normalized_text = "changed"
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_processed_item_new_version_is_allowed(session, make_raw_item) -> None:
    item = make_raw_item()
    for version in ("1.0.0", "1.1.0"):
        session.add(
            ProcessedItem(
                raw_item_id=item.id,
                processor_name="text_normalizer",
                processor_version=version,
                normalized_text="cleaned",
                status=ProcessStatus.SUCCESS,
                effective_at=item.effective_at,
            )
        )
    session.flush()

    count = session.execute(
        sa.select(sa.func.count()).select_from(ProcessedItem)
    ).scalar_one()
    assert count == 2


def test_processed_item_same_version_twice_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    for _ in range(2):
        session.add(
            ProcessedItem(
                raw_item_id=item.id,
                processor_name="text_normalizer",
                processor_version="1.0.0",
                status=ProcessStatus.SUCCESS,
                effective_at=item.effective_at,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
