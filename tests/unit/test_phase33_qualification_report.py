"""Phase 3.3 数据资格缺口单元测试（GOLD-004；纯函数，不触库、不联网）。

覆盖：
- 阈值必须与 ``src.alpha.evidence_gate`` 完全一致（不得另造）；
- 库内数量门槛 PASS **不等于** Alpha 放行：授权 / 历史可用证据始终 BLOCKED；
- 缺失证据（无作者、无事件）绝不判 PASS；
- 机器可读结构（当前值 / 要求值 / 比较方式 / 状态 / 原因 / 证据时间范围）稳定。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
    AuthorReadiness,
    NewsReadiness,
    Phase33Readiness,
)
from src.monitoring.phase33_qualification import (
    HUMAN_GATE_COMPARATOR,
    PHASE3_3_BLOCKER_CODE,
    QUALIFICATION_SCHEMA_VERSION,
    CheckStatus,
    build_qualification_report,
)

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

_CHECK_KEYS = {
    "key",
    "scope",
    "metric",
    "current",
    "required",
    "comparator",
    "status",
    "reason",
    "evidence_start",
    "evidence_end",
}


def _ready_news() -> NewsReadiness:
    sources = (("source_a", 100), ("source_b", 100), ("source_c", 100))
    return NewsReadiness(
        events=300,
        history_days=120,
        largest_source_share=100 / 300,
        source_counts=sources,
        ready=True,
    )


def _empty_news() -> NewsReadiness:
    return NewsReadiness(
        events=0,
        history_days=0,
        largest_source_share=0.0,
        source_counts=(),
        ready=False,
    )


def _readiness(
    *,
    authors: tuple[AuthorReadiness, ...] = (),
    news: NewsReadiness | None = None,
    author_ready: bool = False,
) -> Phase33Readiness:
    resolved_news = news or _empty_news()
    return Phase33Readiness(
        authors=authors,
        label_status_counts=(("LABELED", 30),) if authors else (),
        news=resolved_news,
        hf_weak_supervision_rows=150,
        author_ready=author_ready,
        news_ready=resolved_news.ready,
        as_of=MOMENT,
    )


def test_quantitative_pass_still_blocked_by_human_gates() -> None:
    author = AuthorReadiness(
        author_id="author-1",
        display_name="作者甲",
        opinions=MIN_AUTHOR_SAMPLES,
        trusted_posts=MIN_AUTHOR_SAMPLES,
        ready=True,
        account_counts=(("source_a/account-1", MIN_AUTHOR_SAMPLES),),
    )
    report = build_qualification_report(
        _readiness(authors=(author,), news=_ready_news(), author_ready=True),
        hf_weak_supervision_rows=150,
    )
    by_key = {item.key: item for item in report.checks}
    assert by_key["author.trusted_posts_per_account"].status is CheckStatus.PASS
    assert by_key["author.identity_consistency"].status is CheckStatus.PASS
    assert by_key["news.events"].status is CheckStatus.PASS
    assert by_key["news.history_days"].status is CheckStatus.PASS
    assert by_key["news.max_source_share"].status is CheckStatus.PASS
    # 人工 Gate 项必须保持 BLOCKED
    assert by_key["author.source_authorization"].status is CheckStatus.BLOCKED
    assert by_key["news.available_history_evidence"].status is CheckStatus.BLOCKED
    assert by_key["author.source_authorization"].comparator == HUMAN_GATE_COMPARATOR
    assert report.blocker_code == PHASE3_3_BLOCKER_CODE
    assert report.blocker_active is True
    assert report.ready is False
    assert report.pass_count == 5
    assert report.blocked_count == 2
    assert report.hf_weak_supervision_rows == 150


def test_missing_evidence_never_passes() -> None:
    report = build_qualification_report(_readiness())
    assert report.blocked_count == len(report.checks) == 7
    assert report.pass_count == 0
    assert report.ready is False
    by_key = {item.key: item for item in report.checks}
    assert by_key["news.events"].current == 0
    assert "缺失数据" in by_key["news.events"].reason
    assert by_key["news.max_source_share"].status is CheckStatus.BLOCKED
    assert "无事件证据" in by_key["news.max_source_share"].reason
    assert by_key["author.trusted_posts_per_account"].current == 0
    assert "无证据" in by_key["author.trusted_posts_per_account"].reason


def test_thresholds_are_reused_from_evidence_gate() -> None:
    report = build_qualification_report(_readiness())
    required = {item.key: item.required for item in report.checks}
    assert required["author.trusted_posts_per_account"] == MIN_AUTHOR_SAMPLES
    assert required["news.events"] == MIN_NEWS_EVENTS
    assert required["news.history_days"] == MIN_NEWS_HISTORY_DAYS
    assert required["news.max_source_share"] == MAX_NEWS_SOURCE_SHARE


def test_concentrated_news_source_blocks_share_check() -> None:
    news = NewsReadiness(
        events=300,
        history_days=120,
        largest_source_share=0.6,
        source_counts=(("source_a", 180), ("source_b", 120)),
        ready=False,
    )
    report = build_qualification_report(_readiness(news=news))
    by_key = {item.key: item for item in report.checks}
    assert by_key["news.events"].status is CheckStatus.PASS
    assert by_key["news.history_days"].status is CheckStatus.PASS
    assert by_key["news.max_source_share"].status is CheckStatus.BLOCKED
    assert "60.00%" in by_key["news.max_source_share"].reason


def test_evidence_windows_are_reported_per_scope() -> None:
    author_start = MOMENT - timedelta(days=30)
    news_start = MOMENT - timedelta(days=90)
    author = AuthorReadiness("author-1", "作者甲", 5, 0, False, ())
    report = build_qualification_report(
        _readiness(authors=(author,), news=_ready_news()),
        author_evidence=(author_start, MOMENT - timedelta(days=1)),
        news_evidence=(news_start, MOMENT - timedelta(days=2)),
    )
    by_key = {item.key: item for item in report.checks}
    assert by_key["author.trusted_posts_per_account"].evidence_start == author_start.isoformat()
    assert by_key["news.events"].evidence_start == news_start.isoformat()
    # 授权 / 历史可用证据检查没有可用证据区间（诚实留空）
    assert by_key["author.source_authorization"].evidence_start is None
    assert by_key["news.available_history_evidence"].evidence_end is None
    payload = report.to_dict()
    assert payload["evidence_windows"]["author"]["start_at"] == author_start.isoformat()
    assert payload["evidence_windows"]["news"]["start_at"] == news_start.isoformat()


def test_machine_readable_payload_is_stable_and_json_serializable() -> None:
    import json

    report = build_qualification_report(
        _readiness(
            authors=(AuthorReadiness("a-1", "作者甲", 9, 0, False, (("src/acct", 0),)),),
            news=_empty_news(),
        ),
        hf_weak_supervision_rows=150,
    )
    payload = report.to_dict()
    assert set(payload) == {
        "schema_version",
        "blocker_code",
        "blocker_active",
        "ready",
        "as_of",
        "evidence_windows",
        "checks",
        "authors",
        "pass_count",
        "blocked_count",
        "hf_weak_supervision_rows",
    }
    assert payload["schema_version"] == QUALIFICATION_SCHEMA_VERSION
    assert payload["as_of"] == MOMENT.isoformat()
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["ready"] is False
    for check in payload["checks"]:
        assert set(check) == _CHECK_KEYS
    assert payload["authors"][0]["accounts"] == [{"account": "src/acct", "trusted_posts": 0}]
    json.dumps(payload, ensure_ascii=False)  # 必须可直接序列化


def test_render_keeps_blocker_visible() -> None:
    from src.monitoring.phase33_qualification import render_qualification_report

    text = render_qualification_report(build_qualification_report(_readiness()))
    assert PHASE3_3_BLOCKER_CODE in text
    assert "blocker" in text
    assert "BLOCKED" in text
    assert "PASS=0" in text
