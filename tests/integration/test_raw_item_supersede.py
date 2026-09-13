"""raw_items 修正路径 ①（先插新记录，再回填取代指针）测试。

对应决策：``database/models/raw.py`` 明确的两条合法修正路径中，**优先实现路径 ①**。
本文件同时验证"取代链不可改写"与"被取代记录的内容永不变化"。
"""

from __future__ import annotations

import pytest

from database.models import RawItem
from database.repositories import supersede_raw_item
from src.common.exceptions import ImmutableRecordError

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def test_supersede_links_old_record_to_new_one(session, make_raw_item) -> None:
    original = make_raw_item(content_text="原始正文（第一版）")
    replacement = make_raw_item(source_id=original.source_id, content_text="来源修订后的正文")

    supersede_raw_item(session, original=original, replacement=replacement)

    assert original.superseded_by_id == replacement.id
    assert replacement.superseded_by_id is None


def test_supersede_does_not_change_original_content(session, make_raw_item) -> None:
    """取代只是加指针：被取代记录的原始内容必须逐字保留（append-only）。"""
    original = make_raw_item(content_text="不可篡改的原始内容", title="原始标题")
    original_id = original.id
    original_hash = original.content_hash

    replacement = make_raw_item(source_id=original.source_id, content_text="新版正文")
    supersede_raw_item(session, original=original, replacement=replacement)
    session.commit()

    reloaded = session.get(RawItem, original_id)
    assert reloaded is not None
    assert reloaded.content_text == "不可篡改的原始内容"
    assert reloaded.title == "原始标题"
    assert reloaded.content_hash == original_hash
    assert reloaded.superseded_by_id == replacement.id


def test_supersede_is_idempotent(session, make_raw_item) -> None:
    original = make_raw_item()
    replacement = make_raw_item(source_id=original.source_id)

    supersede_raw_item(session, original=original, replacement=replacement)
    supersede_raw_item(session, original=original, replacement=replacement)

    assert original.superseded_by_id == replacement.id


def test_replacement_must_be_persisted_first(session, make_raw_item) -> None:
    """修正路径 ① 的顺序强制：新记录必须先 flush，否则外键必然失败。"""
    original = make_raw_item()
    pending_replacement = RawItem(
        source_id=original.source_id,
        source_record_id="platform-id-rev2",
        item_type=original.item_type,
        raw_json={"rev": 2},
        content_hash="0" * 64,
        published_at=original.published_at,
        collected_at=original.collected_at,
        effective_at=original.effective_at,
    )
    session.add(pending_replacement)  # 注意：尚未 flush

    with pytest.raises(ValueError, match="先入库"):
        supersede_raw_item(session, original=original, replacement=pending_replacement)

    session.rollback()


def test_same_record_is_rejected(session, make_raw_item) -> None:
    item = make_raw_item()
    with pytest.raises(ValueError, match="不同的 raw_items 记录"):
        supersede_raw_item(session, original=item, replacement=item)


def test_replacement_must_not_be_superseded_itself(session, make_raw_item) -> None:
    first = make_raw_item()
    second = make_raw_item(source_id=first.source_id)
    third = make_raw_item(source_id=first.source_id)
    supersede_raw_item(session, original=second, replacement=third)

    with pytest.raises(ValueError, match="自身已被取代"):
        supersede_raw_item(session, original=first, replacement=second)


def test_cross_source_replacement_is_rejected(session, make_raw_item, make_source) -> None:
    original = make_raw_item()
    other_source = make_source(name="another-source")
    replacement = make_raw_item(source_id=other_source.id)

    with pytest.raises(ValueError, match="同一 source_id"):
        supersede_raw_item(session, original=original, replacement=replacement)


def test_chain_rewrite_is_blocked(session, make_raw_item) -> None:
    """已被取代的记录不得改指向别的记录（取代链不可改写）。"""
    original = make_raw_item()
    first_replacement = make_raw_item(source_id=original.source_id)
    second_replacement = make_raw_item(source_id=original.source_id)

    supersede_raw_item(session, original=original, replacement=first_replacement)

    with pytest.raises(ImmutableRecordError) as excinfo:
        supersede_raw_item(session, original=original, replacement=second_replacement)

    assert excinfo.value.code == "IMMUTABLE_RECORD_VIOLATION"
    assert original.superseded_by_id == first_replacement.id
