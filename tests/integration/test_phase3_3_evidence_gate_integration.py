from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pytest import MonkeyPatch
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
from src.processors.opinion_labels import OpinionLabel


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
    assert result.authors[0].trusted_posts == 0
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


def test_news_parser_versions_do_not_inflate_independent_event_count(
    session: Session,
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
) -> None:
    source = make_source(name="versioned-source", source_type=SourceType.NEWS)
    published = datetime(2026, 1, 1, tzinfo=UTC)
    raw = make_raw_item(
        source=source,
        item_type=RawItemType.NEWS,
        published_at=published,
        collected_at=published + timedelta(minutes=1),
    )
    for version, offset in (("test-v1", 1), ("test-v2", 2)):
        session.add(
            NewsEvent(
                raw_item_id=raw.id,
                headline="same underlying event",
                published_at=published,
                effective_at=published + timedelta(minutes=offset),
                parser_version=version,
            )
        )
    session.flush()

    result = load_phase33_readiness(session)
    assert result.news.events == 1
    assert result.news.source_counts == (("versioned-source", 1),)
    assert result.news.history_days == 0


def test_news_event_linked_to_post_raw_item_is_not_qualified_news(
    session: Session,
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
) -> None:
    source = make_source(name="wrong-raw-type", source_type=SourceType.NEWS)
    published = datetime(2026, 1, 1, tzinfo=UTC)
    raw = make_raw_item(
        source=source,
        item_type=RawItemType.POST,
        published_at=published,
        collected_at=published,
    )
    session.add(
        NewsEvent(
            raw_item_id=raw.id,
            headline="not a news raw item",
            published_at=published,
            effective_at=published,
            parser_version="test-v1",
        )
    )
    session.flush()

    result = load_phase33_readiness(session)
    assert result.news.events == 0
    assert not result.news_ready


def test_future_news_is_excluded_at_audited_time(
    session: Session,
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
) -> None:
    source = make_source(name="asof-news", source_type=SourceType.NEWS)
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    for published in (as_of - timedelta(days=1), as_of + timedelta(days=1)):
        raw = make_raw_item(
            source=source,
            item_type=RawItemType.NEWS,
            published_at=published,
            collected_at=published,
        )
        session.add(
            NewsEvent(
                raw_item_id=raw.id,
                headline="time-gated news",
                published_at=published,
                effective_at=published,
                parser_version="test-v1",
            )
        )
    session.flush()

    result = load_phase33_readiness(session, as_of=as_of)
    assert result.news.events == 1
    assert result.news.future_rows_excluded == 1
    assert result.news.history_days == 0
    assert result.as_of == as_of


def test_readiness_requires_timezone_aware_as_of(session: Session) -> None:
    with pytest.raises(ValueError, match="as_of 必须包含时区"):
        load_phase33_readiness(session, as_of=datetime(2026, 1, 2))


def test_multiple_opinions_and_horizon_labels_from_one_post_count_once(
    session: Session,
    make_author_account: Callable[..., AuthorAccount],
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
    monkeypatch: MonkeyPatch,
) -> None:
    published = datetime(2025, 1, 1, tzinfo=UTC)
    collected = published + timedelta(minutes=2)
    source = make_source(name="multi-label-author", source_type=SourceType.NEWS)
    account = make_author_account(source=source)
    raw = make_raw_item(
        source=source,
        published_at=published,
        collected_at=collected,
        raw_json={"collected_at_provenance": "independent_observation"},
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=published,
        collected_at=collected,
        effective_at=collected,
        text_content="黄金观点的同一原始帖子",
        has_media=False,
    )
    session.add(post)
    session.flush()
    opinions = [
        AuthorOpinion(
            author_id=account.author_id,
            author_post_id=post.id,
            stance=OpinionStance.LONG,
            instrument_id=None,
            horizon=None,
            confidence=Decimal("0.7"),
            information_type=InformationType.TECHNICAL,
            rationale="同一帖子多次解析",
            parser_version=f"test-v{index}",
            effective_at=collected,
        )
        for index in range(10)
    ]
    session.add_all(opinions)
    session.flush()
    labels = [
        OpinionLabel(
            opinion_id=str(opinion.id),
            effective_at=collected.isoformat(),
            stance="LONG",
            horizon=horizon,
            horizon_source="evaluation_grid",
            status="LABELED",
            exit_at=(collected + timedelta(hours=1)).isoformat(),
        )
        for opinion in opinions
        for horizon in ("H1", "H4", "D1")
    ]
    monkeypatch.setattr("src.alpha.evidence_gate.build_opinion_labels", lambda _session: labels)

    result = load_phase33_readiness(session)
    assert result.label_status_counts == (("LABELED", 30),)
    assert result.authors[0].opinions == 10
    assert result.authors[0].trusted_posts == 1
    assert result.authors[0].account_counts[0][1] == 1
    assert not result.author_ready


@pytest.mark.parametrize("provenance", [None, "input", "input_effective_at_fallback"])
def test_labeled_post_without_independent_collection_proof_is_not_trusted(
    session: Session,
    make_author_account: Callable[..., AuthorAccount],
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
    monkeypatch: MonkeyPatch,
    provenance: str | None,
) -> None:
    source = make_source(name="untrusted-provenance", source_type=SourceType.NEWS)
    account = make_author_account(source=source)
    observed = datetime(2025, 1, 1, tzinfo=UTC)
    raw = make_raw_item(
        source=source,
        published_at=observed - timedelta(minutes=1),
        collected_at=observed,
        raw_json={} if provenance is None else {"collected_at_provenance": provenance},
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=observed - timedelta(minutes=1),
        collected_at=observed,
        effective_at=observed,
        text_content="缺少独立采集时间证明的帖子",
        has_media=False,
    )
    session.add(post)
    session.flush()
    opinion = AuthorOpinion(
        author_id=account.author_id,
        author_post_id=post.id,
        stance=OpinionStance.LONG,
        instrument_id=None,
        horizon=OpinionHorizon.H1,
        confidence=Decimal("0.7"),
        information_type=InformationType.TECHNICAL,
        rationale="测试独立采集时间证明",
        parser_version="test-v1",
        effective_at=observed,
    )
    session.add(opinion)
    session.flush()
    monkeypatch.setattr(
        "src.alpha.evidence_gate.build_opinion_labels",
        lambda _session: [
            OpinionLabel(
                opinion_id=str(opinion.id),
                effective_at=observed.isoformat(),
                stance="LONG",
                horizon="H1",
                horizon_source="explicit",
                status="LABELED",
                exit_at=(observed + timedelta(hours=1)).isoformat(),
            )
        ],
    )

    result = load_phase33_readiness(session)
    assert result.label_status_counts == (("LABELED", 1),)
    assert result.authors[0].trusted_posts == 0
    assert not result.author_ready


@pytest.mark.parametrize("anomaly", ["raw_type", "raw_source", "raw_late", "opinion_early"])
def test_labeled_post_with_broken_raw_lineage_or_time_is_not_trusted(
    session: Session,
    make_author_account: Callable[..., AuthorAccount],
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
    monkeypatch: MonkeyPatch,
    anomaly: str,
) -> None:
    source = make_source(name="causal-author", source_type=SourceType.NEWS)
    account = make_author_account(source=source)
    observed = datetime(2025, 1, 1, tzinfo=UTC)
    raw_source = (
        make_source(name="other-author-source", source_type=SourceType.NEWS)
        if anomaly == "raw_source"
        else source
    )
    raw = make_raw_item(
        source=raw_source,
        item_type=RawItemType.NEWS if anomaly == "raw_type" else RawItemType.POST,
        published_at=observed - timedelta(minutes=1),
        collected_at=observed + timedelta(minutes=1) if anomaly == "raw_late" else observed,
        raw_json={"collected_at_provenance": "independent_observation"},
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=observed - timedelta(minutes=1),
        collected_at=observed,
        effective_at=observed,
        text_content="原始记录链不合格的帖子",
        has_media=False,
    )
    session.add(post)
    session.flush()
    opinion = AuthorOpinion(
        author_id=account.author_id,
        author_post_id=post.id,
        stance=OpinionStance.LONG,
        instrument_id=None,
        horizon=OpinionHorizon.H1,
        confidence=Decimal("0.7"),
        information_type=InformationType.TECHNICAL,
        rationale="测试原始记录和时间链",
        parser_version="test-v1",
        effective_at=(observed - timedelta(minutes=1) if anomaly == "opinion_early" else observed),
    )
    session.add(opinion)
    session.flush()
    monkeypatch.setattr(
        "src.alpha.evidence_gate.build_opinion_labels",
        lambda _session: [
            OpinionLabel(
                opinion_id=str(opinion.id),
                effective_at=opinion.effective_at.isoformat(),
                stance="LONG",
                horizon="H1",
                horizon_source="explicit",
                status="LABELED",
                exit_at=(observed + timedelta(hours=1)).isoformat(),
            )
        ],
    )

    result = load_phase33_readiness(session)
    assert result.authors[0].trusted_posts == 0
    assert not result.author_ready


@pytest.mark.parametrize(
    ("first_count", "second_count", "mismatched_identity", "as_of", "expected_ready"),
    [
        (15, 15, False, None, False),
        (30, 0, False, None, True),
        (30, 0, True, None, False),
        (30, 0, False, datetime(2024, 1, 1, tzinfo=UTC), False),
        (30, 0, False, datetime(2025, 1, 1, 1, tzinfo=UTC), False),
    ],
)
def test_author_sample_gate_is_per_account(
    session: Session,
    make_author_account: Callable[..., AuthorAccount],
    make_raw_item: Callable[..., RawItem],
    make_source: Callable[..., Source],
    monkeypatch: MonkeyPatch,
    first_count: int,
    second_count: int,
    mismatched_identity: bool,
    as_of: datetime | None,
    expected_ready: bool,
) -> None:
    first_source = make_source(name="account-source-a", source_type=SourceType.NEWS)
    second_source = make_source(name="account-source-b", source_type=SourceType.NEWS)
    first = make_author_account(source=first_source, external_account_id="account-a")
    second = make_author_account(
        source=second_source, author=first.author, external_account_id="account-b"
    )
    published = datetime(2025, 1, 1, tzinfo=UTC)
    opinions: list[AuthorOpinion] = []
    for account, source, count in (
        (first, first_source, first_count),
        (second, second_source, second_count),
    ):
        for index in range(count):
            collected = published + timedelta(minutes=index + 2)
            raw = make_raw_item(
                source=source,
                published_at=published,
                collected_at=collected,
                raw_json={"collected_at_provenance": "independent_observation"},
            )
            post = AuthorPost(
                author_id=first.author_id,
                author_account_id=account.id,
                raw_item_id=raw.id,
                published_at=published,
                collected_at=collected,
                effective_at=collected,
                text_content=f"第 {index} 条研究测试帖子",
                has_media=False,
            )
            session.add(post)
            session.flush()
            opinion = AuthorOpinion(
                author_id=first.author_id,
                author_post_id=post.id,
                stance=OpinionStance.LONG,
                instrument_id=None,
                horizon=OpinionHorizon.H1,
                confidence=Decimal("0.7"),
                information_type=InformationType.TECHNICAL,
                rationale="测试账号分组",
                parser_version="test-v1",
                effective_at=collected,
            )
            session.add(opinion)
            opinions.append(opinion)
    if mismatched_identity:
        foreign_account = make_author_account(
            source=second_source, external_account_id="foreign-account"
        )
        collected = published + timedelta(hours=2)
        raw = make_raw_item(source=first_source, published_at=published, collected_at=collected)
        post = AuthorPost(
            author_id=foreign_account.author_id,
            author_account_id=first.id,
            raw_item_id=raw.id,
            published_at=published,
            collected_at=collected,
            effective_at=collected,
            text_content="账号与作者归属不一致的测试行",
            has_media=False,
        )
        session.add(post)
        session.flush()
        opinion = AuthorOpinion(
            author_id=first.author_id,
            author_post_id=post.id,
            stance=OpinionStance.LONG,
            instrument_id=None,
            horizon=OpinionHorizon.H1,
            confidence=Decimal("0.7"),
            information_type=InformationType.TECHNICAL,
            rationale="测试跨表归属不一致",
            parser_version="test-v1",
            effective_at=collected,
        )
        session.add(opinion)
        opinions.append(opinion)
    session.flush()
    labels = [
        OpinionLabel(
            opinion_id=str(opinion.id),
            effective_at=opinion.effective_at.isoformat(),
            stance="LONG",
            horizon="H1",
            horizon_source="explicit",
            status="LABELED",
            exit_at=(opinion.effective_at + timedelta(hours=1)).isoformat(),
        )
        for opinion in opinions
    ]
    monkeypatch.setattr("src.alpha.evidence_gate.build_opinion_labels", lambda _session: labels)

    result = load_phase33_readiness(session, as_of=as_of)
    expected_trusted = 0 if as_of is not None else 30
    assert result.authors[0].trusted_posts == expected_trusted
    expected_counts = (("account-source-a/account-a", 0 if as_of is not None else first_count),)
    if second_count:
        expected_counts += (
            ("account-source-b/account-b", 0 if as_of is not None else second_count),
        )
    assert result.authors[0].account_counts == expected_counts
    assert result.authors[0].identity_consistent is not mismatched_identity
    assert result.authors[0].ready is expected_ready
    assert result.author_ready is expected_ready
