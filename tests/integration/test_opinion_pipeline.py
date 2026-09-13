"""Post → Opinion 管道集成测试（纯 Mock：无网络、无真实 LLM）。

验证：两层落库（`processed_items` + `author_opinions`）、幂等、时间因果、
无观点/空文本/未知标的/失败隔离、`limit` 分批、以及**原始层完全不被修改**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorOpinion, AuthorPost, Instrument, ProcessedItem, RawItem
from database.models.enums import (
    InformationType,
    OpinionHorizon,
    OpinionStance,
    ProcessStatus,
)
from database.seeds import seed_instruments
from src.processors.opinion_extractor import OpinionExtractionResult
from src.processors.opinion_pipeline import PROCESSOR_NAME, OpinionPipeline
from src.processors.regex_extractor import RegexOpinionExtractor

pytestmark = pytest.mark.integration

COLLECTED_AT = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
LONG_TEXT = "黄金 2380 附近做多，止损 2365，目标 2450，日内短线。"
NO_OPINION_TEXT = "美国 8 月 CPI 同比 3.2%，高于预期 3.0%。"
CONDITIONAL_TEXT = "如果今天黄金收盘跌破 2380，那我会转空看 2330。"


def _post(
    session: Session,
    make_author_account,
    make_raw_item,
    *,
    text: str,
    has_media: bool = False,
    offset_minutes: int = 0,
) -> AuthorPost:
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
    return post


def _count(session: Session, model: type) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 不保存时区偏移（读回 naive）：统一补齐 UTC 后再比较（仅测试辅助）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1) 正常路径：两层落库
# ---------------------------------------------------------------------------
def test_pipeline_writes_processed_item_and_opinion(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    post = _post(session, make_author_account, make_raw_item, text=LONG_TEXT)

    report = OpinionPipeline(session).run()

    assert report.posts_scanned == 1
    assert report.posts_processed == 1
    assert report.opinions_created == 1
    assert report.posts_failed == 0

    item = session.scalar(sa.select(ProcessedItem))
    assert item is not None
    assert item.processor_name == PROCESSOR_NAME
    assert item.processor_version == "mock-regex-v1"
    assert item.status is ProcessStatus.SUCCESS
    assert _as_utc(item.effective_at) == _as_utc(post.effective_at)
    assert item.structured_json is not None
    assert item.structured_json["no_opinion"] is False
    assert item.structured_json["drafts"][0]["stance"] == "LONG"

    opinion = session.scalar(sa.select(AuthorOpinion))
    assert opinion is not None
    assert opinion.author_post_id == post.id
    assert opinion.author_id == post.author_id
    assert opinion.stance is OpinionStance.LONG
    assert opinion.horizon is OpinionHorizon.H1
    assert opinion.confidence == Decimal("0.9")
    assert opinion.entry_low == Decimal("2380")
    assert opinion.stop_loss == Decimal("2365")
    assert opinion.take_profit == Decimal("2450")
    assert opinion.information_type is InformationType.TECHNICAL
    assert opinion.parser_version == "mock-regex-v1"
    assert _as_utc(opinion.effective_at) == _as_utc(post.effective_at)


def test_pipeline_resolves_instrument_from_whitelist(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=LONG_TEXT)

    OpinionPipeline(session).run()

    opinion = session.scalar(sa.select(AuthorOpinion))
    assert opinion is not None
    xauusd_id = session.scalar(sa.select(Instrument.id).where(Instrument.symbol == "XAUUSD"))
    assert opinion.instrument_id == xauusd_id


# ---------------------------------------------------------------------------
# 2) 幂等与分批
# ---------------------------------------------------------------------------
def test_pipeline_is_idempotent(session: Session, make_author_account, make_raw_item) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=LONG_TEXT)
    pipeline = OpinionPipeline(session)

    first = pipeline.run()
    items_after_first = _count(session, ProcessedItem)
    opinions_after_first = _count(session, AuthorOpinion)

    second = pipeline.run()

    assert first.opinions_created == 1
    assert second.posts_scanned == 0
    assert second.posts_processed == 0
    assert second.opinions_created == 0
    assert _count(session, ProcessedItem) == items_after_first
    assert _count(session, AuthorOpinion) == opinions_after_first


def test_pipeline_limit_processes_only_requested_posts(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    for index in range(3):
        _post(session, make_author_account, make_raw_item, text=LONG_TEXT, offset_minutes=index)
    pipeline = OpinionPipeline(session)

    first_batch = pipeline.run(limit=1)
    second_batch = pipeline.run()

    assert first_batch.posts_processed == 1
    assert second_batch.posts_scanned == 2
    assert _count(session, ProcessedItem) == 3


# ---------------------------------------------------------------------------
# 3) 无观点 / 空文本（也必须留痕，避免重复解析）
# ---------------------------------------------------------------------------
def test_pipeline_records_no_opinion_post(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=NO_OPINION_TEXT)

    report = OpinionPipeline(session).run()

    assert report.posts_no_opinion == 1
    assert report.opinions_created == 0
    item = session.scalar(sa.select(ProcessedItem))
    assert item is not None
    assert item.status is ProcessStatus.SUCCESS
    assert item.structured_json is not None
    assert item.structured_json["no_opinion"] is True
    assert _count(session, AuthorOpinion) == 0


def test_pipeline_skips_empty_text_with_reason(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text="   ")

    report = OpinionPipeline(session).run()

    assert report.posts_skipped_empty_text == 1
    assert report.posts_processed == 0
    item = session.scalar(sa.select(ProcessedItem))
    assert item is not None
    assert item.status is ProcessStatus.SKIPPED
    assert item.structured_json == {"status_reason": "empty_text"}
    assert _count(session, AuthorOpinion) == 0


# ---------------------------------------------------------------------------
# 4) 时间因果（防未来数据泄漏）
# ---------------------------------------------------------------------------
def test_pipeline_opinion_effective_at_follows_post(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    earlier = _post(session, make_author_account, make_raw_item, text=LONG_TEXT)
    later = _post(session, make_author_account, make_raw_item, text=LONG_TEXT, offset_minutes=30)

    OpinionPipeline(session).run()

    rows = session.execute(
        sa.select(AuthorOpinion, AuthorPost)
        .join(AuthorPost, AuthorPost.id == AuthorOpinion.author_post_id)
        .order_by(AuthorPost.effective_at)
    ).all()
    assert len(rows) == 2
    for opinion, post in rows:
        assert _as_utc(opinion.effective_at) == _as_utc(post.effective_at)
        assert _as_utc(opinion.effective_at) >= _as_utc(post.effective_at)
        assert _as_utc(opinion.created_at) >= _as_utc(opinion.effective_at)  # 0005 的 CHECK
    assert rows[0][1].id == earlier.id
    assert rows[1][1].id == later.id


def test_pipeline_never_uses_now_for_effective_at(
    session: Session, make_author_account, make_raw_item
) -> None:
    """`effective_at` 必须来自上游帖子，而不是"现在"（否则重放历史会漂移）。"""
    seed_instruments(session)
    post = _post(session, make_author_account, make_raw_item, text=LONG_TEXT)

    OpinionPipeline(session).run()

    opinion = session.scalar(sa.select(AuthorOpinion))
    assert opinion is not None
    assert _as_utc(opinion.effective_at) == _as_utc(post.effective_at)
    assert _as_utc(opinion.effective_at) < datetime.now(UTC)


# ---------------------------------------------------------------------------
# 5) 未知标的 / 失败隔离 / 诊断计数
# ---------------------------------------------------------------------------
def test_pipeline_skips_unknown_instrument_with_counter(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text="XPTUSD 日内看多，目标 1100。")

    report = OpinionPipeline(session).run()

    assert report.posts_processed == 1
    assert report.opinions_created == 0
    assert report.opinions_skipped_unknown_instrument == 1
    assert report.diagnostics == {"unknown_instrument": 1}
    assert _count(session, AuthorOpinion) == 0
    # 加工结果仍然留痕（该帖子已被处理过，不会每轮重复解析）
    assert _count(session, ProcessedItem) == 1


class _FlakyExtractor:
    """测试替身：遇到 "boom" 抛错，其余委托真实 Mock 抽取器（用于失败隔离测试）。"""

    parser_version = "mock-flaky-v1"

    def __init__(self) -> None:
        self._delegate = RegexOpinionExtractor()

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        if "boom" in text:
            raise RuntimeError("boom")
        return self._delegate.extract(text, has_media=has_media)


def test_pipeline_isolates_failed_post(
    session: Session, make_author_account, make_raw_item
) -> None:
    """单个帖子抽取异常 → 该帖 FAILED，其余照常处理（与采集器失败隔离同一原则）。"""
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text="boom 黄金看多", offset_minutes=0)
    _post(session, make_author_account, make_raw_item, text=LONG_TEXT, offset_minutes=1)

    report = OpinionPipeline(session, extractor=_FlakyExtractor()).run()

    assert report.posts_failed == 1
    assert report.posts_processed == 1
    assert report.opinions_created == 1

    failed = session.scalar(
        sa.select(ProcessedItem).where(ProcessedItem.status == ProcessStatus.FAILED)
    )
    assert failed is not None
    assert failed.processor_version == "mock-flaky-v1"
    assert failed.structured_json is not None
    assert failed.structured_json["exception"] == "RuntimeError"
    # processed_items 无 error_message 列（schema 冻结）→ 失败详情写进 structured_json
    assert "RuntimeError: boom" in str(failed.structured_json["error_message"])


def test_pipeline_counts_extractor_diagnostics(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=CONDITIONAL_TEXT)

    report = OpinionPipeline(session).run()

    assert report.diagnostics.get("conditional") == 1
    opinion = session.scalar(sa.select(AuthorOpinion))
    assert opinion is not None
    assert opinion.stance is OpinionStance.UNKNOWN  # 条件未满足，绝不猜方向
    assert opinion.horizon is OpinionHorizon.D1


# ---------------------------------------------------------------------------
# 6) 原始层绝对不被修改（append-only 红线）
# ---------------------------------------------------------------------------
def test_pipeline_does_not_modify_raw_layer(
    session: Session, make_author_account, make_raw_item
) -> None:
    seed_instruments(session)
    post = _post(session, make_author_account, make_raw_item, text=LONG_TEXT)
    raw = session.scalar(sa.select(RawItem).where(RawItem.id == post.raw_item_id))
    assert raw is not None
    snapshot: tuple[Any, ...] = (
        raw.content_text,
        raw.content_hash,
        _as_utc(raw.collected_at),
        _as_utc(raw.effective_at),
        post.text_content,
        _as_utc(post.effective_at),
        _count(session, RawItem),
        _count(session, AuthorPost),
    )

    OpinionPipeline(session).run()
    session.refresh(raw)
    session.refresh(post)

    assert snapshot == (
        raw.content_text,
        raw.content_hash,
        _as_utc(raw.collected_at),
        _as_utc(raw.effective_at),
        post.text_content,
        _as_utc(post.effective_at),
        _count(session, RawItem),
        _count(session, AuthorPost),
    )
    assert _count(session, ProcessedItem) == 1
    assert _count(session, AuthorOpinion) == 1


def test_pipeline_processed_items_are_append_only(
    session: Session, make_author_account, make_raw_item
) -> None:
    """加工结果是事实表：管道不得改写历史行（由 ORM 守卫在 flush 期拦截）。"""
    from src.common.exceptions import ImmutableRecordError

    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=LONG_TEXT)
    OpinionPipeline(session).run()

    item = session.scalar(sa.select(ProcessedItem))
    assert item is not None
    item.status = ProcessStatus.FAILED
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_new_processor_version_reprocesses_without_overwriting(
    session: Session, make_author_account, make_raw_item
) -> None:
    """换处理器版本 → 同一帖子可被新版本重新处理，旧行保留（append-only，不覆盖）。"""
    seed_instruments(session)
    _post(session, make_author_account, make_raw_item, text=LONG_TEXT)

    OpinionPipeline(session).run()
    second = OpinionPipeline(session, extractor=_FlakyExtractor()).run()

    assert second.posts_scanned == 1  # 新版本尚未处理过该帖子
    assert second.opinions_created == 1
    versions = set(session.scalars(sa.select(ProcessedItem.processor_version)).all())
    assert versions == {"mock-regex-v1", "mock-flaky-v1"}
    assert _count(session, AuthorOpinion) == 2