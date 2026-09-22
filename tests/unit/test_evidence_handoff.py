"""GOLD-008 Evidence 人工交接包单元测试（纯函数，零数据库、零网络）。

覆盖：
- 空库诚实保持 BLOCKED（blocker_active / human_gate_required / data_qualification_passed）；
- 部分达标（News 达标、Author 不达标）仍 BLOCKED；
- 达到量化门槛时最多 ``ready_for_human_review=true``，仍需人工 Gate；
- Author / News eligible / required / remaining、coverage gap、source-share 可评估性量化；
- 人工证据 checklist 六类齐全且契约字段不漂移；模板 / Mock / 示例醒目标记为不计资格；
- 稳定排序 / 确定性 JSON；
- 敏感字段（token / API key / Authorization）不进入 JSON 与 Markdown。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope, field_by_name
from src.evidence.handoff import (
    BLOCKED_STATUS,
    CHECK_CATEGORIES,
    HANDOFF_NOTE,
    HANDOFF_REPORT_NAME,
    HANDOFF_SCHEMA_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
    build_evidence_checklist,
    build_excluded_evidence,
    build_handoff_report,
    render_handoff_markdown,
    thresholds,
)
from src.evidence.ledger import ledger_from_raw_json
from src.monitoring import PHASE3_3_BLOCKER_CODE, build_readiness_report

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"
NEWS_START = datetime(2026, 6, 1, tzinfo=UTC)

REPORT_KEYS = {
    "schema_version",
    "report",
    "contract_version",
    "as_of",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "status",
    "quantified_thresholds_met",
    "ready_for_human_review",
    "data_qualification_passed",
    "phase_transition_allowed",
    "thresholds",
    "author",
    "news",
    "total_remaining_gap_count",
    "checklist",
    "excluded_evidence",
    "quarantine_reason_counts",
    "batch",
    "notes",
}
GAP_KEYS = {
    "scope",
    "status",
    "eligible",
    "required",
    "remaining",
    "certified",
    "not_oos_eligible",
    "coverage_applicable",
    "coverage_days",
    "coverage_required",
    "coverage_remaining",
    "source_share_applicable",
    "max_source_share",
    "source_share_limit",
    "source_share_evaluable",
    "source_share_remaining",
    "remaining_checks",
    "checks",
}
CHECKLIST_KEYS = {
    "key",
    "category",
    "scope",
    "requirement",
    "contract_fields",
    "machine_check",
    "human_review_required",
}


def evidence_entry(
    scope: str,
    *,
    source: str,
    available_at: str,
    oos_eligible: bool = True,
) -> tuple[dict[str, Any], None]:
    return (
        {
            "evidence": {
                "contract_version": EVIDENCE_CONTRACT_VERSION,
                "scope": scope,
                "source": source,
                "oos_eligible": oos_eligible,
                "available_at": available_at,
            }
        },
        None,
    )


def author_entries(count: int) -> list[tuple[dict[str, Any], None]]:
    return [
        evidence_entry(
            "author",
            source="manual-evidence-author",
            available_at=(MOMENT - timedelta(days=5, hours=index)).isoformat(),
        )
        for index in range(count)
    ]


def news_entries(
    count: int = MIN_NEWS_EVENTS,
    *,
    days: int = MIN_NEWS_HISTORY_DAYS,
    sources: tuple[str, ...] = ("src-a", "src-b", "src-c"),
) -> list[tuple[dict[str, Any], None]]:
    span = timedelta(days=days)
    denominator = max(count - 1, 1)
    return [
        evidence_entry(
            "news",
            source=sources[index % len(sources)],
            available_at=(NEWS_START + span * (index / denominator)).isoformat(),
        )
        for index in range(count)
    ]


def report_for(entries: list[tuple[dict[str, Any], None]]) -> Any:
    readiness = build_readiness_report(ledger_from_raw_json(entries), as_of=MOMENT)
    return build_handoff_report(readiness)


def test_empty_ledger_is_blocked_and_honest() -> None:
    report = report_for([])
    payload = report.to_dict()
    assert set(payload) == REPORT_KEYS
    assert payload["report"] == HANDOFF_REPORT_NAME
    assert payload["schema_version"] == HANDOFF_SCHEMA_VERSION
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["status"] == BLOCKED_STATUS
    assert payload["quantified_thresholds_met"] is False
    assert payload["ready_for_human_review"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["batch"] is None
    assert payload["quarantine_reason_counts"] == []

    author = payload["author"]
    news = payload["news"]
    assert set(author) == GAP_KEYS
    assert set(news) == GAP_KEYS
    assert (author["eligible"], author["required"], author["remaining"]) == (
        0,
        MIN_AUTHOR_SAMPLES,
        MIN_AUTHOR_SAMPLES,
    )
    assert (news["eligible"], news["required"], news["remaining"]) == (
        0,
        MIN_NEWS_EVENTS,
        MIN_NEWS_EVENTS,
    )
    assert news["coverage_remaining"] == MIN_NEWS_HISTORY_DAYS
    assert news["source_share_evaluable"] is False
    assert author["source_share_applicable"] is False
    assert author["coverage_applicable"] is False
    assert payload["total_remaining_gap_count"] > 0
    assert HANDOFF_NOTE in payload["notes"]


def test_partial_pass_stays_blocked() -> None:
    # News 三项门槛全达标，但 Author 只有 10 条 → 整体仍未达标
    report = report_for(author_entries(10) + news_entries())
    payload = report.to_dict()
    assert payload["author"]["status"] == "BLOCKED"
    assert payload["author"]["remaining"] == MIN_AUTHOR_SAMPLES - 10
    assert payload["news"]["status"] == "PASS"
    assert payload["news"]["coverage_remaining"] == 0
    assert payload["news"]["source_share_evaluable"] is True
    assert payload["news"]["max_source_share"] <= MAX_NEWS_SOURCE_SHARE
    assert payload["quantified_thresholds_met"] is False
    assert payload["status"] == BLOCKED_STATUS
    assert payload["ready_for_human_review"] is False


def test_thresholds_met_still_requires_human_review() -> None:
    report = report_for(author_entries(MIN_AUTHOR_SAMPLES) + news_entries())
    payload = report.to_dict()
    assert payload["author"]["status"] == "PASS"
    assert payload["news"]["status"] == "PASS"
    assert payload["author"]["remaining"] == 0
    assert payload["quantified_thresholds_met"] is True
    assert payload["ready_for_human_review"] is True
    assert payload["status"] == PENDING_HUMAN_REVIEW_STATUS
    # 工具完成 ≠ 数据资格通过：仍不解除 blocker、不允许自动切 Phase
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    text = render_handoff_markdown(report)
    assert "人工" in text
    assert "不解除" in text
    assert PENDING_HUMAN_REVIEW_STATUS in text


def test_thresholds_echo_evidence_gate_single_source() -> None:
    assert thresholds() == {
        "author_min_eligible_records": MIN_AUTHOR_SAMPLES,
        "news_min_eligible_records": MIN_NEWS_EVENTS,
        "news_min_history_days": MIN_NEWS_HISTORY_DAYS,
        "news_max_source_share": MAX_NEWS_SOURCE_SHARE,
    }


def test_checklist_covers_categories_and_real_contract_fields() -> None:
    checklist = build_evidence_checklist()
    assert {item.category for item in checklist} == set(CHECK_CATEGORIES)
    for item in checklist:
        assert set(item.to_dict()) == CHECKLIST_KEYS
        assert item.human_review_required is True
        assert item.scope in {EvidenceScope.AUTHOR.value, EvidenceScope.NEWS.value, "both"}
        assert item.contract_fields, item.key
        for name in item.contract_fields:
            assert field_by_name(name) is not None, name


def test_excluded_evidence_is_never_counted() -> None:
    excluded = build_excluded_evidence()
    kinds = {item.kind for item in excluded}
    assert "template_or_example" in kinds
    assert "historical_plain_csv" in kinds
    for item in excluded:
        assert item.counts_toward_eligibility is False
        assert item.reason_code
    report = report_for([])
    payload = report.to_dict()
    assert payload["excluded_evidence"]
    assert all(
        entry["counts_toward_eligibility"] is False
        for entry in payload["excluded_evidence"]
    )


def test_json_is_stable_and_deterministic() -> None:
    entries = author_entries(10) + news_entries()
    first = report_for(entries).to_dict()
    second = report_for(entries).to_dict()
    dumped = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert dumped == json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert [item["key"] for item in first["checklist"]] == [
        item.key for item in build_evidence_checklist()
    ]


def test_quarantine_reason_counts_are_sorted_and_stable() -> None:
    readiness = build_readiness_report(ledger_from_raw_json([]), as_of=MOMENT)
    report = build_handoff_report(
        readiness,
        quarantine_reason_counts=(("SYNTHETIC_EVIDENCE", 2), ("AUTHORIZATION_MISSING", 1)),
    )
    assert report.to_dict()["quarantine_reason_counts"] == [
        {"reason_code": "AUTHORIZATION_MISSING", "count": 1},
        {"reason_code": "SYNTHETIC_EVIDENCE", "count": 2},
    ]


def test_sensitive_values_are_redacted_in_json_and_markdown() -> None:
    entries = author_entries(1) + news_entries(
        2, days=0, sources=(f"manual token={SECRET}",)
    )
    report = report_for(entries)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    text = render_handoff_markdown(report)
    assert SECRET not in payload
    assert SECRET not in text
    assert "token=***" in payload
    assert "token=***" in text


def test_render_markdown_has_required_sections() -> None:
    text = render_handoff_markdown(report_for([]))
    for heading in (
        "Evidence 人工交接包",
        "## 1. 状态",
        "## 2. 缺口明细",
        "## 3. 隔离 / 原因码计数",
        "## 4. 人工证据 checklist",
        "## 5. 明确不计资格的证据",
        "## 6. 口径与边界",
    ):
        assert heading in text
    for category in CHECK_CATEGORIES:
        assert f"### {category}" in text
    assert "false" in text  # counts_toward_eligibility 一律 false（模板 / Mock 不计资格）
    assert BLOCKED_STATUS in text

