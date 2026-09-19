from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import (
    AuthorAccount,
    AuthorOpinion,
    AuthorPost,
    AuthorSkillSnapshot,
    AuthorWeightSnapshot,
    InformationType,
    NewsEvent,
    OpinionHorizon,
    OpinionStance,
    RawItem,
    RawItemType,
    Source,
    SourceType,
)
from src.alpha.evidence_gate import load_phase33_readiness


def test_loader_uses_trust_gate_and_never_writes_facts(
    session: Session,
    make_author_account: Callable[..., AuthorAccount],
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
) -> None:
    moment = datetime(2025, 1, 1, tzinfo=UTC)
    author_source = make_source(name="author-source", source_type=SourceType.NEWS)
    account = make_author_account(source=author_source)
    raw = make_raw_item(
        source=author_source,
        collected_at=moment,
        published_at=moment,
        raw_json={"collected_at_provenance": "input_effective_at_fallback"},
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=moment,
        collected_at=moment,
        effective_at=moment,
        text_content="黄金短线偏多",
        has_media=False,
    )
    session.add(post)
    session.flush()
    session.add(
        AuthorOpinion(
            author_id=account.author_id,
            author_post_id=post.id,
            stance=OpinionStance.LONG,
            instrument_id=None,
            horizon=OpinionHorizon.H1,
            confidence=Decimal("0.7"),
            information_type=InformationType.TECHNICAL,
            rationale="黄金短线偏多",
            parser_version="test-v1",
            effective_at=moment,
        )
    )

    news_source = make_source(name="news-source", source_type=SourceType.NEWS)
    for offset in (0, 10):
        published = moment + timedelta(days=offset)
        news_raw = make_raw_item(
            source=news_source,
            item_type=RawItemType.NEWS,
            collected_at=published,
            published_at=published,
        )
        session.add(
            NewsEvent(
                raw_item_id=news_raw.id,
                headline="gold news",
                published_at=published,
                effective_at=published,
                parser_version="test-v1",
            )
        )
    session.flush()

    before_skills = session.scalar(sa.select(sa.func.count()).select_from(AuthorSkillSnapshot))
    before_weights = session.scalar(sa.select(sa.func.count()).select_from(AuthorWeightSnapshot))
    result = load_phase33_readiness(session, hf_weak_supervision_rows=150)
    after_skills = session.scalar(sa.select(sa.func.count()).select_from(AuthorSkillSnapshot))
    after_weights = session.scalar(sa.select(sa.func.count()).select_from(AuthorWeightSnapshot))

    assert result.authors[0].opinions == 1
    assert result.authors[0].trusted_labels == 0
    assert result.label_status_counts == (("UNTRUSTED_COLLECTION_TIME", 1),)
    assert result.news.events == 2
    assert result.news.history_days == 10
    assert result.news.largest_source_share == 1.0
    assert not result.author_ready
    assert not result.news_ready
    assert (after_skills, after_weights) == (before_skills, before_weights)


def test_historical_headlines_collected_together_do_not_create_history(
    session: Session,
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
) -> None:
    source = make_source(name="archive-source", source_type=SourceType.NEWS)
    collected = datetime(2026, 1, 1, tzinfo=UTC)
    for published in (collected - timedelta(days=120), collected - timedelta(days=1)):
        raw = make_raw_item(
            source=source,
            item_type=RawItemType.NEWS,
            published_at=published,
            collected_at=collected,
        )
        session.add(
            NewsEvent(
                raw_item_id=raw.id,
                headline="archived headline",
                published_at=published,
                effective_at=published,
                parser_version="test-v1",
            )
        )
    session.flush()

    result = load_phase33_readiness(session)
    assert result.news.events == 2
    assert result.news.history_days == 0
    assert not result.news_ready
