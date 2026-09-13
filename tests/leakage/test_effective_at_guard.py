"""时间因果 / 未来数据泄漏防护测试（08 §6：最高优先级）。

原则（06_Cline开发规则 第 8 条）：
    feature.effective_at <= prediction_at
因此 effective_at 必须 >= 该数据真正可见的时间（collected_at / published_at / event_at），
本文件证明数据库层会拒绝任何违反该不等式的写入。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from database.models import AuthorPost, MacroEvent, NewsEvent
from src.common.time import is_future, resolve_effective_at, utc_now

pytestmark = pytest.mark.leakage


def test_raw_item_effective_at_cannot_precede_collected_at(session, make_raw_item) -> None:
    collected_at = utc_now()
    with pytest.raises(IntegrityError):
        make_raw_item(
            collected_at=collected_at,
            published_at=None,
            effective_at=collected_at - timedelta(seconds=1),
        )
    session.rollback()


def test_raw_item_effective_at_cannot_precede_published_at(session, make_raw_item) -> None:
    collected_at = utc_now()
    published_at = collected_at - timedelta(hours=1)
    with pytest.raises(IntegrityError):
        make_raw_item(
            collected_at=collected_at,
            published_at=published_at,
            effective_at=published_at - timedelta(minutes=1),
        )
    session.rollback()


def test_effective_at_resolver_is_leakage_safe(session, make_raw_item) -> None:
    """正常路径：effective_at = max(published_at, collected_at)，不早于任何可见时间。"""
    collected_at = utc_now()
    published_at = collected_at - timedelta(minutes=30)

    item = make_raw_item(collected_at=collected_at, published_at=published_at)
    expected = resolve_effective_at(published_at=published_at, collected_at=collected_at)

    assert item.effective_at == expected
    assert item.effective_at >= item.collected_at
    assert item.published_at is not None
    assert item.effective_at >= item.published_at


def test_future_timestamp_is_detectable() -> None:
    """数据质量检查必须能识别"未来时间戳"（时钟错误 / 脏数据）。"""
    assert is_future(utc_now() + timedelta(days=1)) is True
    assert is_future(utc_now() - timedelta(days=1)) is False


def test_macro_event_effective_at_cannot_precede_event_at(session, make_source) -> None:
    """宏观数据：绝不允许在官方公布时间之前就"知道"数据。"""
    source = make_source()
    event_at = utc_now()
    session.add(
        MacroEvent(
            source_id=source.id,
            event_code="US_CPI_YOY",
            country="US",
            event_at=event_at,
            collected_at=event_at,
            effective_at=event_at - timedelta(minutes=1),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_macro_event_valid_timing_is_accepted(session, make_source) -> None:
    source = make_source()
    event_at = utc_now()
    session.add(
        MacroEvent(
            source_id=source.id,
            event_code="US_CPI_YOY",
            country="US",
            event_at=event_at,
            collected_at=event_at,
            effective_at=event_at + timedelta(seconds=30),
        )
    )
    session.flush()


def test_news_event_published_at_cannot_exceed_effective_at(session, make_raw_item) -> None:
    item = make_raw_item()
    session.add(
        NewsEvent(
            raw_item_id=item.id,
            headline="Fed signals patience",
            published_at=item.effective_at + timedelta(minutes=10),
            effective_at=item.effective_at,
            parser_version="news_parser@0.1.0",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_author_post_effective_at_cannot_precede_collected_at(
    session, make_author_account, make_raw_item
) -> None:
    account = make_author_account()
    item = make_raw_item()
    collected_at = utc_now()
    session.add(
        AuthorPost(
            author_id=account.author_id,
            author_account_id=account.id,
            raw_item_id=item.id,
            published_at=None,
            collected_at=collected_at,
            effective_at=collected_at - timedelta(seconds=1),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_author_post_valid_timing_is_accepted(session, make_author_account, make_raw_item) -> None:
    account = make_author_account()
    item = make_raw_item()
    collected_at = item.collected_at
    session.add(
        AuthorPost(
            author_id=account.author_id,
            author_account_id=account.id,
            raw_item_id=item.id,
            published_at=item.published_at,
            collected_at=collected_at,
            effective_at=item.effective_at,
            text_content=item.content_text,
            has_media=False,
        )
    )
    session.flush()
