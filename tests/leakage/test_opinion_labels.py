from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from database.models import (
    Author,
    AuthorAccount,
    AuthorOpinion,
    AuthorPost,
    Instrument,
    MarketBar,
    RawItem,
)
from database.models.enums import (
    AssetClass,
    OpinionHorizon,
    OpinionStance,
    RawItemType,
    SourceType,
    Timeframe,
)
from src.common.hashing import content_hash
from src.processors.opinion_labels import build_opinion_label

pytestmark = pytest.mark.leakage


def _opinion_graph(
    session: Session,
    make_source,
    *,
    effective_at: datetime,
    collected_at_provenance: str | None = None,
) -> AuthorOpinion:
    source = make_source(name="manual-label", source_type=SourceType.NEWS)
    author = Author(display_name="测试作者", canonical_name="label-author")
    session.add(author)
    session.flush()
    account = AuthorAccount(
        author_id=author.id,
        source_id=source.id,
        external_account_id="label-account",
        enabled=True,
        verified=True,
    )
    instrument = Instrument(
        symbol="LABEL_XAUUSD", asset_class=AssetClass.COMMODITY, timezone="UTC", enabled=True
    )
    session.add_all([account, instrument])
    session.flush()
    raw = RawItem(
        source_id=source.id,
        source_record_id="label-post",
        item_type=RawItemType.POST,
        title="看多黄金",
        content_text="下一小时看多黄金",
        raw_json=(
            {}
            if collected_at_provenance is None
            else {"collected_at_provenance": collected_at_provenance}
        ),
        content_hash=content_hash("下一小时看多黄金"),
        published_at=effective_at,
        collected_at=effective_at,
        effective_at=effective_at,
    )
    session.add(raw)
    session.flush()
    post = AuthorPost(
        author_id=author.id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=effective_at,
        collected_at=effective_at,
        effective_at=effective_at,
        text_content=raw.content_text,
        has_media=False,
    )
    session.add(post)
    session.flush()
    opinion = AuthorOpinion(
        author_id=author.id,
        author_post_id=post.id,
        stance=OpinionStance.LONG,
        instrument_id=instrument.id,
        horizon=OpinionHorizon.H1,
        parser_version="test-v1",
        effective_at=effective_at,
    )
    session.add(opinion)
    session.flush()
    return opinion


def _bar(
    session: Session, make_source, opinion: AuthorOpinion, *, open_at: datetime, price: int
) -> None:
    source = make_source(source_type=SourceType.MARKET)
    close_at = open_at + timedelta(hours=1)
    session.add(
        MarketBar(
            instrument_id=opinion.instrument_id,
            timeframe=Timeframe.H1,
            open_time=open_at,
            close_time=close_at,
            open=Decimal(price),
            high=Decimal(price + 11),
            low=Decimal(price - 1),
            close=Decimal(price + 10),
            source_id=source.id,
            collected_at=close_at,
            effective_at=close_at,
        )
    )
    session.flush()


def test_entry_bar_must_be_strictly_after_signal(session: Session, make_source) -> None:
    signal_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    opinion = _opinion_graph(session, make_source, effective_at=signal_at)
    _bar(session, make_source, opinion, open_at=signal_at, price=100)  # 同时刻，必须忽略
    _bar(session, make_source, opinion, open_at=signal_at + timedelta(hours=1), price=200)

    label = build_opinion_label(session, opinion)

    assert label.status == "LABELED"
    assert label.entry_at == (signal_at + timedelta(hours=1)).isoformat()
    assert label.entry_open == 200.0
    assert label.exit_close == 210.0
    assert label.direction_hit is True


def test_missing_horizon_is_counted_not_guessed(session: Session, make_source) -> None:
    opinion = _opinion_graph(
        session, make_source, effective_at=datetime(2026, 9, 1, 8, tzinfo=UTC)
    )
    opinion.horizon = None
    label = build_opinion_label(session, opinion)
    assert label.status == "MISSING_HORIZON"
    assert label.entry_at is None and label.log_return is None


def test_fallback_collection_time_is_pipeline_only(session: Session, make_source) -> None:
    signal_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    opinion = _opinion_graph(
        session,
        make_source,
        effective_at=signal_at,
        collected_at_provenance="input_effective_at_fallback",
    )
    _bar(session, make_source, opinion, open_at=signal_at + timedelta(hours=1), price=100)

    label = build_opinion_label(session, opinion)

    assert label.status == "UNTRUSTED_COLLECTION_TIME"
    assert label.collection_time_provenance == "input_effective_at_fallback"
    assert label.entry_at is None and label.log_return is None


def test_entry_lag_cannot_exceed_evaluated_horizon(session: Session, make_source) -> None:
    signal_at = datetime(2026, 9, 5, 20, tzinfo=UTC)  # 周末信号
    opinion = _opinion_graph(session, make_source, effective_at=signal_at)
    monday_open = datetime(2026, 9, 7, 1, tzinfo=UTC)
    _bar(session, make_source, opinion, open_at=monday_open, price=100)

    label = build_opinion_label(session, opinion)

    assert label.status == "ENTRY_LAG_EXCEEDED"
    assert label.entry_at == monday_open.isoformat()
    assert label.entry_lag_seconds == 29 * 60 * 60
    assert label.log_return is None
