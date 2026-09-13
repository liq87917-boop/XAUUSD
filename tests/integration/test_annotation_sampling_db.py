"""抽样脚本的数据库读取路径（集成测试；只读 Phase 1 既有表，不改动 schema）。

验证 `load_candidates_from_db` 的 JOIN 正确性与"读到就能抽样"的端到端可用性：
作者库导入（TECH_DEBT TD-16）完成后，`author_posts` 一有数据本路径即可用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorPost
from scripts.sample_annotation_set import load_candidates_from_db, sample_candidates

pytestmark = pytest.mark.integration

COLLECTED_AT = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)


def _make_post(
    session: Session,
    make_author_account,
    make_raw_item,
    *,
    text: str,
    has_media: bool = False,
    offset_minutes: int = 0,
) -> tuple[Any, Any]:
    collected_at = COLLECTED_AT + timedelta(minutes=offset_minutes)
    account = make_author_account()
    raw = make_raw_item(content_text=text, collected_at=collected_at, published_at=None)
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=raw.published_at,
        collected_at=raw.collected_at,
        effective_at=raw.effective_at,
        text_content=text,
        has_media=has_media,
    )
    session.add(post)
    session.flush()
    return post, account


def test_load_candidates_from_db_returns_joined_fields(
    session: Session, engine: sa.Engine, make_author_account, make_raw_item
) -> None:
    text = "黄金 2380 附近做多，止损 2365，目标 2450"
    post, account = _make_post(
        session, make_author_account, make_raw_item, text=text, has_media=True
    )
    session.commit()

    candidates = load_candidates_from_db(engine)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.post_id == str(post.id)
    assert candidate.raw_item_id == str(post.raw_item_id)
    assert candidate.author_id == str(post.author_id)
    assert candidate.author_name == account.author.display_name
    assert candidate.source_name == account.source.name
    assert candidate.text_content == text
    assert candidate.has_media is True
    assert candidate.effective_at.tzinfo is not None
    assert len(candidate.content_hash) == 64  # 来自 raw_items.content_hash
    assert candidate.time_precision == "collected_only"  # published_at 为空


def test_load_candidates_from_db_is_empty_without_posts(engine: sa.Engine) -> None:
    assert load_candidates_from_db(engine) == []


def test_db_candidates_can_be_sampled_end_to_end(
    session: Session, engine: sa.Engine, make_author_account, make_raw_item
) -> None:
    for index in range(6):
        _make_post(
            session,
            make_author_account,
            make_raw_item,
            text=f"黄金看多 {index}，目标 2450" + "补" * (index * 60),
            offset_minutes=index,
        )
    session.commit()

    candidates = load_candidates_from_db(engine)
    report = sample_candidates(candidates, limit=3, seed=20260912, media_quota=0)

    assert len(candidates) == 6
    assert report.selected_count == 3
    assert report.candidates_total == 6
    assert {item.post_id for item in report.selected} <= {item.post_id for item in candidates}
