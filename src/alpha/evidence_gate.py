"""Phase 3.3 Author / News Alpha 的数据资格门禁。

资格不足时只返回可审计原因，不创建技能、权重或 Alpha 信号事实。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import Author, AuthorOpinion, NewsEvent, RawItem, Source
from src.processors.opinion_labels import build_opinion_labels

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


@dataclass(frozen=True, slots=True)
class NewsReadiness:
    events: int
    history_days: int
    largest_source_share: float
    source_counts: tuple[tuple[str, int], ...]
    ready: bool


@dataclass(frozen=True, slots=True)
class Phase33Readiness:
    """仅表示库内数量/标签/时序门槛；不证明来源授权已通过。"""

    authors: tuple[AuthorReadiness, ...]
    label_status_counts: tuple[tuple[str, int], ...]
    news: NewsReadiness
    hf_weak_supervision_rows: int
    author_ready: bool
    news_ready: bool


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
    session: Session, *, hf_weak_supervision_rows: int = 0
) -> Phase33Readiness:
    """从研究库读取资格证据；本函数严格只读。"""
    authors = list(session.scalars(sa.select(Author).order_by(Author.id)).all())
    opinions = list(session.scalars(sa.select(AuthorOpinion).order_by(AuthorOpinion.id)).all())
    author_by_opinion = {str(item.id): str(item.author_id) for item in opinions}
    post_by_opinion = {str(item.id): str(item.author_post_id) for item in opinions}
    opinion_counts = Counter(str(item.author_id) for item in opinions)
    labels = build_opinion_labels(session)
    status_counts = Counter(item.status for item in labels)
    trusted_posts: dict[str, set[str]] = {}
    for item in labels:
        if item.status == "LABELED" and item.opinion_id in author_by_opinion:
            author_id = author_by_opinion[item.opinion_id]
            trusted_posts.setdefault(author_id, set()).add(post_by_opinion[item.opinion_id])
    author_rows = tuple(
        AuthorReadiness(
            author_id=str(author.id),
            display_name=author.display_name,
            opinions=opinion_counts[str(author.id)],
            trusted_posts=len(trusted_posts.get(str(author.id), set())),
            ready=len(trusted_posts.get(str(author.id), set())) >= MIN_AUTHOR_SAMPLES,
        )
        for author in authors
        if opinion_counts[str(author.id)] > 0
    )

    news_rows = session.execute(
        sa.select(RawItem.id, Source.name, NewsEvent.effective_at, RawItem.effective_at)
        .join(RawItem, RawItem.source_id == Source.id)
        .join(NewsEvent, NewsEvent.raw_item_id == RawItem.id)
    ).all()
    distinct_news: dict[str, tuple[str, datetime]] = {}
    for raw_id, code, event_at, raw_at in news_rows:
        key = str(raw_id)
        available = max(event_at, raw_at)
        prior = distinct_news.get(key)
        # 同一原始新闻的不同解析版本不增加独立样本；取较晚可用时刻，保守防前视。
        distinct_news[key] = (str(code), max(prior[1], available) if prior else available)
    source_counts = Counter(code for code, _available in distinct_news.values())
    # 历史覆盖以研究时实际可用时间为准；不能用今天采集的旧标题回填历史。
    available_at = [available for _code, available in distinct_news.values()]
    first_at = min(available_at) if available_at else None
    last_at = max(available_at) if available_at else None
    news = assess_news_counts(dict(source_counts), first_at, last_at)
    return Phase33Readiness(
        authors=author_rows,
        label_status_counts=tuple(sorted(status_counts.items())),
        news=news,
        hf_weak_supervision_rows=hf_weak_supervision_rows,
        author_ready=bool(author_rows) and all(item.ready for item in author_rows),
        news_ready=news.ready,
    )
