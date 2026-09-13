"""观点时间因果（未来数据泄漏）测试 —— 团队裁决 2 的强制落点。

规则（`docs/10 §3`、`docs/04 §44` 决策 6）：

```text
author_opinions.effective_at >= author_posts.effective_at
```

实现方式（团队裁决：**不新增** `source_effective_at` 数据库列）：
- 管道必须调用 :func:`resolve_opinion_effective_at` 计算该字段；
- 写入前必须调用 :func:`assert_opinion_available_after_post` 校验；
- 本文件证明"违规一定会抛错"，而不是靠人工 review。

同时覆盖"单表可强制部分"：`created_at >= effective_at` 由 migration 0005 的
CHECK 承担（见 `tests/integration/test_phase2_author_lab_schema.py`）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from database.models import AuthorOpinion, AuthorPost
from database.models.enums import OpinionStance
from src.common.exceptions import TimeSemanticsError
from src.processors.timeline import (
    assert_opinion_available_after_post,
    ensure_utc_from_database,
    resolve_opinion_effective_at,
)

pytestmark = pytest.mark.leakage

POST_EFFECTIVE_AT = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
PARSER_VERSION = "mock-regex-v1"


# ---------------------------------------------------------------------------
# 1) 纯函数契约（不依赖数据库）
# ---------------------------------------------------------------------------
def test_resolve_returns_upstream_availability() -> None:
    """观点可用时间 = 上游帖子可用时间（不引入新的可用性）。"""
    assert resolve_opinion_effective_at(post_effective_at=POST_EFFECTIVE_AT) == POST_EFFECTIVE_AT


def test_resolve_normalises_timezone_to_utc() -> None:
    shanghai = POST_EFFECTIVE_AT.astimezone(tz=datetime.now().astimezone().tzinfo)
    resolved = resolve_opinion_effective_at(post_effective_at=shanghai)

    assert resolved.tzinfo is not None
    assert resolved == POST_EFFECTIVE_AT


def test_resolve_rejects_naive_datetime() -> None:
    with pytest.raises(TimeSemanticsError):
        resolve_opinion_effective_at(post_effective_at=datetime(2024, 1, 2, 8, 0))


def test_assert_accepts_equal_and_later_timestamps() -> None:
    assert_opinion_available_after_post(
        opinion_effective_at=POST_EFFECTIVE_AT, post_effective_at=POST_EFFECTIVE_AT
    )
    assert_opinion_available_after_post(
        opinion_effective_at=POST_EFFECTIVE_AT + timedelta(minutes=1),
        post_effective_at=POST_EFFECTIVE_AT,
    )


def test_assert_rejects_earlier_timestamp_as_leakage() -> None:
    """★ 核心红线：观点"早于"上游可用即构成未来数据泄漏。"""
    with pytest.raises(TimeSemanticsError) as excinfo:
        assert_opinion_available_after_post(
            opinion_effective_at=POST_EFFECTIVE_AT - timedelta(seconds=1),
            post_effective_at=POST_EFFECTIVE_AT,
        )

    assert "未来数据泄漏" in str(excinfo.value)
    assert excinfo.value.details["post_effective_at"] == POST_EFFECTIVE_AT.isoformat()


# ---------------------------------------------------------------------------
# 2) 与数据库配合：合法写入通过、越界写入被拒
# ---------------------------------------------------------------------------
def test_opinion_with_upstream_effective_at_is_persisted(
    session, make_author_account, make_raw_item
) -> None:
    """正常路径：effective_at 取上游帖子可用时间 → 落库成功且关系成立。"""
    account = make_author_account()
    raw = make_raw_item(collected_at=POST_EFFECTIVE_AT, published_at=None)
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=raw.published_at,
        collected_at=raw.collected_at,
        effective_at=raw.effective_at,
        text_content="黄金看多",
        has_media=False,
    )
    session.add(post)
    session.flush()

    opinion = AuthorOpinion(
        author_id=post.author_id,
        author_post_id=post.id,
        stance=OpinionStance.LONG,
        parser_version=PARSER_VERSION,
        effective_at=resolve_opinion_effective_at(post_effective_at=post.effective_at),
    )
    session.add(opinion)
    session.flush()

    assert opinion.effective_at >= post.effective_at


def test_opinion_effective_at_before_post_is_flagged_by_assertion(
    session, make_author_account, make_raw_item
) -> None:
    """应用层必须在写入前拦截（数据库无法表达跨表约束，故此处是最后一道闸）。"""
    account = make_author_account()
    raw = make_raw_item(collected_at=POST_EFFECTIVE_AT, published_at=None)
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=raw.published_at,
        collected_at=raw.collected_at,
        effective_at=raw.effective_at,
        text_content="黄金看多",
        has_media=False,
    )
    session.add(post)
    session.flush()

    leaked_effective_at = post.effective_at - timedelta(hours=1)
    with pytest.raises(TimeSemanticsError):
        assert_opinion_available_after_post(
            opinion_effective_at=leaked_effective_at, post_effective_at=post.effective_at
        )

    # 即便绕过断言硬写，数据库的 created_at >= effective_at 仍会兜住"自称过去可用"
    session.add(
        AuthorOpinion(
            author_id=post.author_id,
            author_post_id=post.id,
            stance=OpinionStance.LONG,
            parser_version=PARSER_VERSION,
            effective_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_opinion_requires_existing_post(session, make_author_account, make_raw_item) -> None:
    """归属必须真实存在（禁止凭空生成观点）。"""
    account = make_author_account()
    raw = make_raw_item(collected_at=POST_EFFECTIVE_AT, published_at=None)
    session.add(
        AuthorOpinion(
            author_id=account.author_id,
            author_post_id=uuid.uuid4(),
            stance=OpinionStance.LONG,
            parser_version=PARSER_VERSION,
            effective_at=raw.effective_at,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------------
# 3) 管道级时间因果（团队裁决 2 的强制落点之一）
# ---------------------------------------------------------------------------
def test_ensure_utc_from_database_normalises_sqlite_naive_values() -> None:
    """数据库读回的 naive 值必须能显式归一（SQLite 不保存偏移；PG 读回即 aware）。"""
    naive = datetime(2024, 1, 2, 8, 0)

    resolved = ensure_utc_from_database(naive, field_name="author_posts.effective_at")

    assert resolved == POST_EFFECTIVE_AT
    assert resolved.tzinfo is not None


def test_ensure_utc_from_database_rejects_non_datetime() -> None:
    with pytest.raises(TimeSemanticsError):
        ensure_utc_from_database("2024-01-02T08:00:00Z", field_name="x")  # type: ignore[arg-type]


def test_pipeline_opinions_are_never_earlier_than_their_post(
    session, make_author_account, make_raw_item
) -> None:
    """★ 管道输出必须满足 `opinion.effective_at >= post.effective_at`（逐行校验）。"""
    from database.seeds import seed_instruments
    from src.processors.opinion_pipeline import OpinionPipeline

    seed_instruments(session)
    account = make_author_account()
    for index, text in enumerate(
        ("黄金 2380 附近做多，止损 2365，目标 2450", "美国 CPI 超预期，黄金看空，目标 2300")
    ):
        effective_at = POST_EFFECTIVE_AT + timedelta(minutes=index)
        raw = make_raw_item(content_text=text, collected_at=effective_at, published_at=None)
        session.add(
            AuthorPost(
                author_id=account.author_id,
                author_account_id=account.id,
                raw_item_id=raw.id,
                published_at=raw.published_at,
                collected_at=raw.collected_at,
                effective_at=raw.effective_at,
                text_content=text,
                has_media=False,
            )
        )
    session.flush()

    report = OpinionPipeline(session).run()
    assert report.opinions_created == 2

    rows = session.execute(
        sa.select(AuthorOpinion, AuthorPost).join(
            AuthorPost, AuthorPost.id == AuthorOpinion.author_post_id
        )
    ).all()
    assert len(rows) == 2
    for opinion, post in rows:
        assert _as_utc(opinion.effective_at) >= _as_utc(post.effective_at)
        assert _as_utc(opinion.effective_at) == _as_utc(post.effective_at)  # 观点不产生新可用性
        assert _as_utc(opinion.created_at) >= _as_utc(opinion.effective_at)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 返回 naive：统一补齐 UTC 后再比较（仅测试辅助）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)