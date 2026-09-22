"""证据就绪度（readiness / preflight）单元测试（GOLD-006；纯函数，零数据库、零网络）。

覆盖：
- 阈值必须与 ``src.alpha.evidence_gate`` 完全一致（不得另造）；
- 空库 / 部分达标 / 授权或可用性不达标 / 单源集中度超限 / 覆盖天数不足的 remaining gap；
- News 的 ``eligible_count`` / ``coverage_days`` / ``max_source_share`` 与 Author 可信 eligible 数；
- 批次量化（accepted / quarantined / duplicate / conflict / not_oos_eligible）；
- 稳定 JSON 结构、脱敏（来源名里的凭据不进入报告）、无法评估时不得判 PASS。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope, ReasonCode, RowStatus
from src.evidence.intake import EvidenceIntakeReport, InputFile, IntakeCounts, RowOutcome
from src.evidence.ledger import EvidenceLedger, ScopeLedger, ledger_from_raw_json
from src.monitoring import CheckStatus
from src.monitoring.evidence_readiness import (
    READINESS_SCHEMA_VERSION,
    EvidenceReadinessReport,
    ReadinessCheck,
    build_readiness_report,
    render_readiness_report,
    summarize_batch,
)

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
START = datetime(2026, 6, 1, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"

CHECK_KEYS = {
    "key",
    "scope",
    "metric",
    "current",
    "required",
    "comparator",
    "status",
    "evaluable",
    "remaining",
    "reason",
    "evidence_start",
    "evidence_end",
}
REPORT_KEYS = {
    "schema_version",
    "contract_version",
    "as_of",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "ready",
    "scopes",
    "batches",
    "notes",
}
SCOPE_KEYS = {
    "scope",
    "eligible_count",
    "certified_count",
    "not_oos_eligible_count",
    "coverage_days",
    "max_source_share",
    "source_counts",
    "ready",
    "remaining_gap_count",
    "checks",
}
BATCH_KEYS = {
    "scope",
    "input_path",
    "input_sha256",
    "rows",
    "accepted",
    "quarantined",
    "duplicate",
    "conflict",
    "not_oos_eligible",
    "oos_eligible",
    "reason_code_counts",
}


def _entry(
    scope: EvidenceScope, *, source: str, available_at: str | None = None, oos_eligible: bool = True
) -> tuple[dict[str, dict[str, object]], None]:
    return (
        {
            "evidence": {
                "contract_version": EVIDENCE_CONTRACT_VERSION,
                "scope": scope.value,
                "source": source,
                "oos_eligible": oos_eligible,
                "available_at": available_at,
            }
        },
        None,
    )


def _spread(
    scope: EvidenceScope,
    source: str,
    count: int,
    *,
    days: int,
    oos_eligible: bool = True,
) -> list[tuple[dict[str, dict[str, object]], None]]:
    """均匀铺满 ``days`` 天的独立可用证据窗口（用于精确断言 coverage_days）。"""
    span = timedelta(days=days)
    denominator = max(count - 1, 1)
    return [
        _entry(
            scope,
            source=source,
            available_at=(START + span * (index / denominator)).isoformat(),
            oos_eligible=oos_eligible,
        )
        for index in range(count)
    ]


def _report(
    entries: list[tuple[dict[str, dict[str, object]], None]],
    *,
    scopes: tuple[EvidenceScope, ...] = (EvidenceScope.AUTHOR, EvidenceScope.NEWS),
) -> EvidenceReadinessReport:
    return build_readiness_report(ledger_from_raw_json(entries), as_of=MOMENT, scopes=scopes)


def _checks(report: EvidenceReadinessReport) -> dict[str, ReadinessCheck]:
    return {check.key: check for item in report.scopes for check in item.checks}


def test_thresholds_and_comparators_reuse_evidence_gate() -> None:
    checks = _checks(_report([]))
    author = checks["author.evidence_intake_oos_eligible"]
    assert author.required == MIN_AUTHOR_SAMPLES
    assert author.comparator == ">="
    news = checks["news.evidence_intake_oos_eligible"]
    assert news.required == MIN_NEWS_EVENTS and news.comparator == ">="
    coverage = checks["news.evidence_intake_coverage_days"]
    assert coverage.required == MIN_NEWS_HISTORY_DAYS and coverage.comparator == ">="
    share = checks["news.evidence_intake_max_source_share"]
    assert share.required == MAX_NEWS_SOURCE_SHARE and share.comparator == "<="
    # 无证据不得判 PASS（与 news.max_source_share 的既有口径一致）
    assert share.evaluable is False and share.status is CheckStatus.BLOCKED


def test_empty_ledger_is_blocked_with_full_gaps() -> None:
    report = _report([])
    assert report.blocker_code == "PHASE3_3_DATA"
    assert report.blocker_active is True
    assert report.human_gate_required is True
    assert report.ready is False
    author = report.scope("author")
    assert author.eligible_count == 0 and author.remaining_gap_count == 1
    assert author.checks[0].remaining == MIN_AUTHOR_SAMPLES
    news = report.scope("news")
    assert news.remaining_gap_count == 3
    assert news.coverage_days == 0
    assert news.max_source_share == 0.0
    gaps = {check.key: check.remaining for check in news.checks}
    assert gaps["news.evidence_intake_oos_eligible"] == MIN_NEWS_EVENTS
    assert gaps["news.evidence_intake_coverage_days"] == MIN_NEWS_HISTORY_DAYS
    assert gaps["news.evidence_intake_max_source_share"] == 0.0


def test_author_gap_equals_remaining_trusted_posts() -> None:
    partial = _report(_spread(EvidenceScope.AUTHOR, "manual-author", 10, days=5))
    author = partial.scope("author")
    assert author.eligible_count == 10
    assert author.checks[0].status is CheckStatus.BLOCKED
    assert author.checks[0].remaining == MIN_AUTHOR_SAMPLES - 10
    full = _report(
        _spread(EvidenceScope.AUTHOR, "manual-author", MIN_AUTHOR_SAMPLES, days=5)
    ).scope("author")
    assert full.ready is True
    assert full.checks[0].status is CheckStatus.PASS
    assert full.checks[0].remaining == 0


def test_records_without_independent_availability_do_not_count() -> None:
    """认证但缺历史可用证据（NOT_OOS_ELIGIBLE）不得计入 eligible 数。"""
    entries = _spread(EvidenceScope.AUTHOR, "manual-author", 40, days=5, oos_eligible=False)
    author = _report(entries).scope("author")
    assert author.certified_count == 40
    assert author.eligible_count == 0
    assert author.not_oos_eligible_count == 40
    assert author.ready is False
    assert "NOT_OOS_ELIGIBLE" in author.checks[0].reason


def test_news_count_gap_and_coverage_pass() -> None:
    entries = _spread(
        EvidenceScope.NEWS, "manual-news", MIN_NEWS_EVENTS - 50, days=MIN_NEWS_HISTORY_DAYS
    )
    news = _report(entries).scope("news")
    checks = {check.key: check for check in news.checks}
    assert news.eligible_count == MIN_NEWS_EVENTS - 50
    assert checks["news.evidence_intake_oos_eligible"].remaining == 50
    assert checks["news.evidence_intake_oos_eligible"].status is CheckStatus.BLOCKED
    assert checks["news.evidence_intake_coverage_days"].status is CheckStatus.PASS
    assert checks["news.evidence_intake_coverage_days"].current == MIN_NEWS_HISTORY_DAYS


def test_news_coverage_days_gap_blocks_even_when_count_is_enough() -> None:
    entries = _spread(EvidenceScope.NEWS, "manual-news", MIN_NEWS_EVENTS, days=10)
    news = _report(entries).scope("news")
    checks = {check.key: check for check in news.checks}
    assert news.eligible_count == MIN_NEWS_EVENTS
    assert checks["news.evidence_intake_oos_eligible"].status is CheckStatus.PASS
    coverage = checks["news.evidence_intake_coverage_days"]
    assert coverage.current == 10
    assert coverage.status is CheckStatus.BLOCKED
    assert coverage.remaining == MIN_NEWS_HISTORY_DAYS - 10
    assert news.ready is False


def test_news_single_source_share_over_limit_reports_remaining() -> None:
    entries = (
        _spread(EvidenceScope.NEWS, "source-a", 200, days=MIN_NEWS_HISTORY_DAYS)
        + _spread(EvidenceScope.NEWS, "source-b", 50, days=MIN_NEWS_HISTORY_DAYS)
        + _spread(EvidenceScope.NEWS, "source-c", 50, days=MIN_NEWS_HISTORY_DAYS)
    )
    news = _report(entries).scope("news")
    checks = {check.key: check for check in news.checks}
    share = checks["news.evidence_intake_max_source_share"]
    assert news.eligible_count == 300
    assert news.max_source_share == round(200 / 300, 6)
    assert share.status is CheckStatus.BLOCKED
    assert share.evaluable is True
    assert share.remaining == round(200 / 300 - MAX_NEWS_SOURCE_SHARE, 6)
    assert checks["news.evidence_intake_oos_eligible"].status is CheckStatus.PASS
    assert news.ready is False


def test_news_balanced_sources_pass_quantitatively_but_never_unblock() -> None:
    entries = (
        _spread(EvidenceScope.NEWS, "source-a", 100, days=MIN_NEWS_HISTORY_DAYS)
        + _spread(EvidenceScope.NEWS, "source-b", 100, days=MIN_NEWS_HISTORY_DAYS)
        + _spread(EvidenceScope.NEWS, "source-c", 100, days=MIN_NEWS_HISTORY_DAYS)
    )
    report = _report(entries)
    news = report.scope("news")
    assert news.ready is True and news.remaining_gap_count == 0
    assert news.coverage_days == MIN_NEWS_HISTORY_DAYS
    # Author 侧仍无证据 → 顶层 ready 仍为 False，blocker 仍 active
    assert report.ready is False
    assert report.blocker_active is True


def test_scope_ledger_properties() -> None:
    ledger = ScopeLedger(
        scope="news",
        certified_records=4,
        oos_eligible_records=4,
        not_oos_eligible_records=0,
        evidence_start=START,
        evidence_end=START + timedelta(days=90),
        source_counts=(("a", 3), ("b", 1)),
    )
    assert ledger.coverage_days == 90
    assert ledger.max_source_share == 0.75
    empty = ScopeLedger("author", 0, 0, 0)
    assert empty.coverage_days == 0 and empty.max_source_share == 0.0
    payload = ledger.to_dict()
    assert payload["coverage_days"] == 90
    assert payload["source_counts"] == [{"source": "a", "count": 3}, {"source": "b", "count": 1}]


def _row(
    index: int,
    status: RowStatus,
    codes: tuple[ReasonCode, ...],
    *,
    oos_eligible: bool = False,
) -> RowOutcome:
    return RowOutcome(
        index=index,
        row_number=index + 2,
        status=status,
        reason_codes=codes,
        reasons=(),
        fingerprint="f" * 16,
        source="manual-news",
        source_record_id=f"rec-{index}",
        oos_eligible=oos_eligible,
        not_oos_eligible_reason=None,
        persisted=status is RowStatus.ACCEPTED,
    )


def test_summarize_batch_quantifies_every_outcome() -> None:
    rows = (
        _row(0, RowStatus.ACCEPTED, (), oos_eligible=True),
        _row(1, RowStatus.ACCEPTED, (ReasonCode.AVAILABILITY_UNPROVEN,)),
        _row(2, RowStatus.QUARANTINED, (ReasonCode.AUTHORIZATION_MISSING,)),
        _row(3, RowStatus.QUARANTINED, (ReasonCode.IDENTITY_CONFLICT,)),
        _row(4, RowStatus.DUPLICATE, (ReasonCode.DUPLICATE,)),
    )
    report = EvidenceIntakeReport(
        scope=EvidenceScope.NEWS,
        dry_run=True,
        generated_at=MOMENT,
        input_file=InputFile(path="logs/evidence/news.csv", format="csv", sha256="a" * 64),
        rows=rows,
        counts=IntakeCounts(
            rows=5, accepted=2, quarantined=2, duplicate=1, oos_eligible=1, not_oos_eligible=1
        ),
    )
    batch = summarize_batch(report)
    assert (
        batch.rows,
        batch.accepted,
        batch.quarantined,
        batch.duplicate,
        batch.conflict,
        batch.not_oos_eligible,
        batch.oos_eligible,
    ) == (5, 2, 2, 1, 1, 1, 1)
    tally = dict(batch.reason_code_counts)
    assert tally["IDENTITY_CONFLICT"] == 1
    assert tally["AUTHORIZATION_MISSING"] == 1
    assert tally["DUPLICATE"] == 1
    assert tally["AVAILABILITY_UNPROVEN"] == 1
    assert set(batch.to_dict()) == BATCH_KEYS


def test_report_json_shape_is_stable_and_serializable() -> None:
    report = _report(_spread(EvidenceScope.NEWS, "manual-news", 5, days=2))
    payload = report.to_dict()
    assert set(payload) == REPORT_KEYS
    assert payload["schema_version"] == READINESS_SCHEMA_VERSION
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["contract_version"] == EVIDENCE_CONTRACT_VERSION
    for scope in payload["scopes"]:
        assert set(scope) == SCOPE_KEYS
        for check in scope["checks"]:
            assert set(check) == CHECK_KEYS
    json.dumps(payload, ensure_ascii=False)  # 必须可直接序列化


def test_report_never_leaks_credentials_from_source_names() -> None:
    entries = _spread(EvidenceScope.NEWS, f"manual-news token={SECRET}", 3, days=1)
    report = _report(entries)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    text = render_readiness_report(report)
    assert SECRET not in payload
    assert SECRET not in text
    assert "token=***" in payload
    assert SECRET not in json.dumps(
        [check.to_dict() for item in report.scopes for check in item.checks], ensure_ascii=False
    )


def test_build_readiness_report_rejects_naive_as_of() -> None:
    with pytest.raises(ValueError, match="时区"):
        build_readiness_report(EvidenceLedger.empty(), as_of=datetime(2026, 9, 22, 12, 0))


def test_render_shows_blocker_thresholds_and_gaps() -> None:
    text = render_readiness_report(_report([]))
    assert "PHASE3_3_DATA" in text
    assert "active=true" in text
    assert "human_gate_required=true" in text
    assert "不解除" in text
    assert str(MIN_NEWS_EVENTS) in text
    assert "缺口" in text
    assert "author.evidence_intake_oos_eligible" in text
    assert "news.evidence_intake_max_source_share" in text


def test_scope_filter_limits_output() -> None:
    report = _report([], scopes=(EvidenceScope.NEWS,))
    assert [item.scope for item in report.scopes] == ["news"]
    with pytest.raises(KeyError):
        report.scope("author")

