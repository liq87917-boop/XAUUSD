"""组装层集成测试（SQLite）：assemble 补齐归属 / persist 幂等。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorOpinion, AuthorPost, AuthorSkillSnapshot
from database.models.enums import InformationType, OpinionHorizon, OpinionStance
from src.alpha.author_alpha import AuthorSkillResult
from src.alpha.author_skill_assembly import assemble_skill_samples, persist_skill_snapshots
from src.processors.opinion_labels import OpinionLabel

pytestmark = pytest.mark.integration

COLLECTED_AT = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)


@pytest.fixture()
def author_post(session: Session, make_author_account, make_raw_item) -> AuthorPost:
    account = make_author_account()
    raw = make_raw_item(
        collected_at=COLLECTED_AT,
        published_at=COLLECTED_AT - timedelta(minutes=5),
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=raw.published_at,
        collected_at=raw.collected_at,
        effective_at=raw.effective_at,
        text_content="XAUUSD 短线偏多",
        has_media=False,
    )
    session.add(post)
    session.flush()
    return post


def _opinion(author_post: AuthorPost, **overrides: object) -> AuthorOpinion:
    defaults: dict[str, object] = {
        "author_id": author_post.author_id,
        "author_post_id": author_post.id,
        "stance": OpinionStance.LONG,
        "instrument_id": None,
        "horizon": OpinionHorizon.H1,
        "confidence": Decimal("0.8"),
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "take_profit": None,
        "information_type": InformationType.TECHNICAL,
        "rationale": "test",
        "parser_version": "mock-v1",
        "effective_at": author_post.effective_at,
    }
    defaults.update(overrides)
    return AuthorOpinion(**defaults)


def _labeled(opinion: AuthorOpinion) -> OpinionLabel:
    return OpinionLabel(
        opinion_id=str(opinion.id),
        effective_at=opinion.effective_at.isoformat(),
        stance="LONG",
        horizon="H1",
        horizon_source="declared",
        status="LABELED",
        stance_direction=1,
        direction_hit=True,
        log_return=0.001,
    )


def test_assemble_skill_samples_fills_author_confidence_information_type(
    session: Session, author_post
) -> None:
    opinion = _opinion(author_post, confidence=Decimal("0.8"))
    session.add(opinion)
    session.flush()

    samples = assemble_skill_samples(session, [_labeled(opinion)])
    assert len(samples) == 1
    assert samples[0].author_id == str(author_post.author_id)
    assert samples[0].sample.confidence == pytest.approx(0.8)
    assert samples[0].sample.information_type == "TECHNICAL"


def test_assemble_skill_samples_skips_unlabeled(session: Session, author_post) -> None:
    opinion = _opinion(author_post)
    session.add(opinion)
    session.flush()

    label = OpinionLabel(
        opinion_id=str(opinion.id),
        effective_at=opinion.effective_at.isoformat(),
        stance="LONG",
        horizon="H1",
        horizon_source="declared",
        status="NO_ENTRY_BAR",
    )
    assert assemble_skill_samples(session, [label]) == []


def test_persist_skill_snapshots_is_idempotent(session: Session, author_post) -> None:
    as_of = datetime(2024, 1, 3, tzinfo=UTC)
    result = AuthorSkillResult(
        author_id=str(author_post.author_id),
        as_of=as_of,
        sample_size=30,
        direction_hits=20,
        direction_raw=0.666666,
        direction_skill=0.65625,
        timing_raw=0.001,
        timing_skill=0.6,
        calibration_score=None,
        entry_skill=None,
        exit_skill=None,
        independence_score=None,
        ready=True,
    )
    assert persist_skill_snapshots(session, [result]) == 1
    session.flush()
    assert persist_skill_snapshots(session, [result]) == 0  # 幂等：同一 (author_id, as_of) 不重复写
    session.flush()
    count = session.scalar(sa.select(sa.func.count()).select_from(AuthorSkillSnapshot))
    assert count == 1
