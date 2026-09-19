"""Phase 3.3 Author / News Alpha 的数据资格门禁。

资格不足时只返回可审计原因，不创建技能、权重或 Alpha 信号事实。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import (
    Author,
    AuthorAccount,
    AuthorOpinion,
    AuthorPost,
    NewsEvent,
    RawItem,
    RawItemType,
    Source,
)
from src.processors.opinion_labels import build_opinion_labels
from src.processors.timeline import ensure_utc_from_database

MIN_AUTHOR_SAMPLES: Final[int] = 30
MIN_NEWS_EVENTS: Final[int] = 200
MIN_NEWS_HISTORY_DAYS: Final[int] = 90
MAX_NEWS_SOURCE_SHARE: Final[float] = 0.40


@dataclass(frozen=True, slots=True)
class AuthorReadiness:
    author_id: str
    display_name: str
    opinions: int
    trusted_posts: int
    ready: bool
    account_counts: tuple[tuple[str, int], ...] = ()
    identity_consistent: bool = True


@dataclass(frozen=True, slots=True)
class NewsReadiness:
    events: int
    history_days: int
    largest_source_share: float
    source_counts: tuple[tuple[str, int], ...]
    ready: bool
    future_rows_excluded: int = 0


@dataclass(frozen=True, slots=True)
class Phase33Readiness:
    """仅表示库内数量/标签/时序门槛；不证明来源授权已通过。"""

    authors: tuple[AuthorReadiness, ...]
    label_status_counts: tuple[tuple[str, int], ...]
    news: NewsReadiness
    hf_weak_supervision_rows: int
    author_ready: bool
    news_ready: bool
    as_of: datetime | None = None


def assess_news_counts(
    source_counts: dict[str, int], first_at: datetime | None, last_at: datetime | None
) -> NewsReadiness:
    total = sum(source_counts.values())
    history_days = 0 if first_at is None or last_at is None else (last_at - first_at).days
    largest_share = 0.0 if total == 0 else max(source_counts.values()) / total
    ready = bool(
        total >= MIN_NEWS_EVENTS
        and history_days >= MIN_NEWS_HISTORY_DAYS
        and largest_share <= MAX_NEWS_SOURCE_SHARE
    )
    return NewsReadiness(
        events=total,
        history_days=history_days,
        largest_source_share=largest_share,
        source_counts=tuple(sorted(source_counts.items())),
        ready=ready,
    )


def load_phase33_readiness(
    session: Session, *, hf_weak_supervision_rows: int = 0, as_of: datetime | None = None
) -> Phase33Readiness:
    """从研究库读取资格证据；本函数严格只读。"""
    audit_clock = as_of if as_of is not None else datetime.now(UTC)
    if audit_clock.tzinfo is None or audit_clock.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    moment = audit_clock.astimezone(UTC)
    authors = list(session.scalars(sa.select(Author).order_by(Author.id)).all())
    opinions = list(session.scalars(sa.select(AuthorOpinion).order_by(AuthorOpinion.id)).all())
    opinions_by_id = {str(item.id): item for item in opinions}
    posts = {str(item.id): item for item in session.scalars(sa.select(AuthorPost)).all()}
    raw_items = {str(item.id): item for item in session.scalars(sa.select(RawItem)).all()}
    accounts = {str(item.id): item for item in session.scalars(sa.select(AuthorAccount)).all()}
    sources = {str(item.id): item.name for item in session.scalars(sa.select(Source)).all()}
    post_by_opinion = {str(item.id): str(item.author_post_id) for item in opinions}
    opinion_counts = Counter(str(item.author_id) for item in opinions)
    account_ids_by_author: dict[str, set[str]] = {}
    account_by_opinion: dict[str, tuple[str, str]] = {}
    invalid_identity: set[str] = set()
    for opinion in opinions:
        author_id = str(opinion.author_id)
        post = posts.get(str(opinion.author_post_id))
        account = accounts.get(str(post.author_account_id)) if post is not None else None
        if (
            post is None
            or account is None
            or str(post.author_id) != author_id
            or str(account.author_id) != author_id
        ):
            invalid_identity.add(author_id)
            continue
        account_id = str(account.id)
        account_ids_by_author.setdefault(author_id, set()).add(account_id)
        account_by_opinion[str(opinion.id)] = (author_id, account_id)
    labels = build_opinion_labels(session)
    status_counts = Counter(item.status for item in labels)
    trusted_posts_by_account: dict[tuple[str, str], set[str]] = {}
    for item in labels:
        key = account_by_opinion.get(item.opinion_id)
        if item.status == "LABELED" and key is not None:
            if item.exit_at is None:
                continue
            try:
                label_exit = datetime.fromisoformat(item.exit_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if label_exit.tzinfo is None or label_exit.utcoffset() is None:
                continue
            opinion = opinions_by_id[item.opinion_id]
            post = posts[post_by_opinion[item.opinion_id]]
            raw = raw_items.get(str(post.raw_item_id))
            if raw is None or not isinstance(raw.raw_json, dict):
                continue
            if raw.raw_json.get("collected_at_provenance") != "independent_observation":
                continue
            opinion_at = ensure_utc_from_database(opinion.effective_at, field_name="opinion_at")
            post_at = ensure_utc_from_database(post.effective_at, field_name="post_at")
            if max(opinion_at, post_at, label_exit.astimezone(UTC)) <= moment:
                trusted_posts_by_account.setdefault(key, set()).add(str(post.id))
    author_rows: list[AuthorReadiness] = []
    for author in authors:
        author_id = str(author.id)
        if not opinion_counts[author_id]:
            continue
        keys = sorted(
            account_ids_by_author.get(author_id, set()),
            key=lambda account_id: (
                sources[str(accounts[account_id].source_id)],
                accounts[account_id].external_account_id,
            ),
        )
        account_counts = tuple(
            (
                f"{sources[str(accounts[account_id].source_id)]}/"
                f"{accounts[account_id].external_account_id}",
                len(trusted_posts_by_account.get((author_id, account_id), set())),
            )
            for account_id in keys
        )
        author_rows.append(
            AuthorReadiness(
                author_id=author_id,
                display_name=author.display_name,
                opinions=opinion_counts[author_id],
                trusted_posts=sum(count for _account, count in account_counts),
                ready=(
                    bool(account_counts)
                    and author_id not in invalid_identity
                    and all(count >= MIN_AUTHOR_SAMPLES for _account, count in account_counts)
                ),
                account_counts=account_counts,
                identity_consistent=author_id not in invalid_identity,
            )
        )

    news_rows = session.execute(
        sa.select(RawItem.id, Source.name, NewsEvent.effective_at, RawItem.effective_at)
        .join(RawItem, RawItem.source_id == Source.id)
        .join(NewsEvent, NewsEvent.raw_item_id == RawItem.id)
        .where(RawItem.item_type == RawItemType.NEWS)
    ).all()
    distinct_news: dict[str, tuple[str, datetime]] = {}
    future_rows_excluded = 0
    for raw_id, code, event_at, raw_at in news_rows:
        news_key = str(raw_id)
        available = max(
            ensure_utc_from_database(event_at, field_name="news_event_at"),
            ensure_utc_from_database(raw_at, field_name="raw_item_at"),
        )
        if available > moment:
            future_rows_excluded += 1
            continue
        prior = distinct_news.get(news_key)
        # 同一原始新闻的不同解析版本不增加独立样本；取较晚可用时刻，保守防前视。
        distinct_news[news_key] = (str(code), max(prior[1], available) if prior else available)
    source_counts = Counter(code for code, _available in distinct_news.values())
    # 历史覆盖以研究时实际可用时间为准；不能用今天采集的旧标题回填历史。
    available_at = [available for _code, available in distinct_news.values()]
    first_at = min(available_at) if available_at else None
    last_at = max(available_at) if available_at else None
    news = replace(
        assess_news_counts(dict(source_counts), first_at, last_at),
        future_rows_excluded=future_rows_excluded,
    )
    return Phase33Readiness(
        authors=tuple(author_rows),
        label_status_counts=tuple(sorted(status_counts.items())),
        news=news,
        hf_weak_supervision_rows=hf_weak_supervision_rows,
        author_ready=bool(author_rows) and all(item.ready for item in author_rows),
        news_ready=news.ready,
        as_of=moment,
    )
