"""GOLD-007 写入门禁 / 隔离摘要单元测试（纯函数，零数据库、零网络）。

覆盖：
- 稳定步骤名与 schema 版本；
- 写入门禁对 accepted / quarantined / duplicate / conflict / not_oos_eligible 的分类计数；
- 合成示例、授权缺失、时间非法、身份冲突、凭据拦截、出处缺失、availability 各自可区分；
- 隔离摘要脱敏（来源名 / 原因文本里的凭据一律 `***`）、指纹只留前缀；
- 稳定 JSON 结构与人类可读渲染。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.evidence.contracts import EvidenceScope, ReasonCode, RowStatus
from src.evidence.intake import EvidenceIntakeReport, InputFile, IntakeCounts, RowOutcome
from src.evidence.workflow import (
    OPERATOR_STEPS,
    OPERATOR_WORKFLOW_SCHEMA_VERSION,
    WRITE_GATE_NOTE,
    build_quarantine_summary,
    evaluate_write_gate,
    render_quarantine_summary,
    render_write_gate,
)

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"

GATE_KEYS = {
    "schema_version",
    "scope",
    "dry_run_source",
    "rows",
    "accepted",
    "quarantined",
    "duplicate",
    "conflict",
    "not_oos_eligible",
    "oos_eligible",
    "synthetic",
    "authorization_incomplete",
    "time_invalid",
    "content_invalid",
    "identity_issues",
    "sensitive_detected",
    "provenance_missing",
    "availability_unproven",
    "write_allowed",
    "reason_code_counts",
    "notes",
}
QUARANTINE_KEYS = {
    "schema_version",
    "scope",
    "rows",
    "total_quarantined",
    "reason_code_counts",
    "entries",
    "notes",
}
ENTRY_KEYS = {
    "row_number",
    "status",
    "reason_codes",
    "reasons",
    "source",
    "source_record_id",
    "fingerprint",
    "oos_eligible",
}


def _row(
    index: int,
    status: RowStatus,
    codes: tuple[ReasonCode, ...],
    *,
    oos_eligible: bool = True,
    source: str = "manual-evidence-author",
    record_id: str | None = None,
    reasons: tuple[str, ...] = (),
) -> RowOutcome:
    return RowOutcome(
        index=index,
        row_number=index + 1,
        status=status,
        reason_codes=codes,
        reasons=reasons,
        fingerprint="f" * 64,
        source=source,
        source_record_id=record_id if record_id is not None else f"rec-{index:04d}",
        oos_eligible=oos_eligible,
        not_oos_eligible_reason=(
            None if oos_eligible else ReasonCode.AVAILABILITY_UNPROVEN
        ),
        persisted=False,
    )


def _report(
    rows: tuple[RowOutcome, ...],
    *,
    scope: EvidenceScope = EvidenceScope.AUTHOR,
    dry_run: bool = True,
) -> EvidenceIntakeReport:
    accepted = sum(1 for row in rows if row.status is RowStatus.ACCEPTED)
    quarantined = sum(1 for row in rows if row.status is RowStatus.QUARANTINED)
    duplicate = sum(1 for row in rows if row.status is RowStatus.DUPLICATE)
    oos = sum(1 for row in rows if row.status is RowStatus.ACCEPTED and row.oos_eligible)
    return EvidenceIntakeReport(
        scope=scope,
        dry_run=dry_run,
        generated_at=MOMENT,
        input_file=InputFile(path="logs/evidence/author.jsonl", format="jsonl", sha256="a" * 64),
        rows=rows,
        counts=IntakeCounts(
            rows=len(rows),
            accepted=accepted,
            quarantined=quarantined,
            duplicate=duplicate,
            oos_eligible=oos,
            not_oos_eligible=accepted - oos,
        ),
    )


def test_operator_steps_and_schema_version_are_stable() -> None:
    assert OPERATOR_STEPS == ("template", "preflight", "quarantine", "intake", "recheck")
    assert OPERATOR_WORKFLOW_SCHEMA_VERSION == 1


def test_gate_classifies_every_outcome() -> None:
    rows = (
        _row(0, RowStatus.ACCEPTED, ()),
        _row(1, RowStatus.ACCEPTED, (ReasonCode.AVAILABILITY_UNPROVEN,), oos_eligible=False),
        _row(2, RowStatus.QUARANTINED, (ReasonCode.SYNTHETIC_EVIDENCE,)),
        _row(
            3,
            RowStatus.QUARANTINED,
            (ReasonCode.AUTHORIZATION_MISSING, ReasonCode.PUBLISHED_AT_INVALID),
        ),
        _row(4, RowStatus.QUARANTINED, (ReasonCode.IDENTITY_CONFLICT,)),
        _row(5, RowStatus.QUARANTINED, (ReasonCode.SENSITIVE_VALUE_DETECTED,)),
        _row(6, RowStatus.DUPLICATE, (ReasonCode.DUPLICATE,)),
    )
    decision = evaluate_write_gate(_report(rows))
    assert (decision.rows, decision.accepted, decision.quarantined, decision.duplicate) == (
        7,
        2,
        4,
        1,
    )
    assert decision.conflict == 1
    assert decision.oos_eligible == 1
    assert decision.not_oos_eligible == 1
    assert decision.availability_unproven == 1
    assert decision.synthetic == 1
    assert decision.authorization_incomplete == 1
    assert decision.time_invalid == 1
    assert decision.identity_issues == 1
    assert decision.sensitive_detected == 1
    assert decision.provenance_missing == 0
    assert decision.content_invalid == 0
    assert decision.write_allowed is True
    assert decision.scope == "author"
    assert decision.dry_run_source is True
    assert set(decision.to_dict()) == GATE_KEYS
    assert decision.to_dict()["schema_version"] == OPERATOR_WORKFLOW_SCHEMA_VERSION
    payload = decision.to_dict()
    codes = {item["reason_code"]: item["count"] for item in payload["reason_code_counts"]}
    assert codes["SYNTHETIC_EVIDENCE"] == 1
    assert codes["IDENTITY_CONFLICT"] == 1
    assert codes["DUPLICATE"] == 1
    json.dumps(payload, ensure_ascii=False)


def test_gate_blocks_when_nothing_is_acceptable() -> None:
    decision = evaluate_write_gate(
        _report((_row(0, RowStatus.QUARANTINED, (ReasonCode.SYNTHETIC_EVIDENCE,)),))
    )
    assert decision.write_allowed is False
    assert decision.accepted == 0
    assert WRITE_GATE_NOTE in decision.notes
    text = render_write_gate(decision)
    assert "写入门禁" in text
    assert "SYNTHETIC_EVIDENCE" in text
    assert "不解除" in text


def test_quarantine_summary_is_sanitized_and_reviewable() -> None:
    rows = (
        _row(0, RowStatus.ACCEPTED, ()),
        _row(
            1,
            RowStatus.QUARANTINED,
            (ReasonCode.AUTHORIZATION_MISSING,),
            source=f"manual token={SECRET}",
            record_id=f"rec-{SECRET}",
            reasons=(f"bearer {SECRET}",),
        ),
        _row(2, RowStatus.DUPLICATE, (ReasonCode.DUPLICATE,)),
    )
    summary = build_quarantine_summary(_report(rows))
    assert summary.total_quarantined == 1
    assert summary.rows == 3
    assert summary.reason_code_counts == (("AUTHORIZATION_MISSING", 1),)
    assert set(summary.to_dict()) == QUARANTINE_KEYS
    assert set(summary.entries[0].to_dict()) == ENTRY_KEYS
    assert summary.entries[0].fingerprint == "f" * 16
    payload = json.dumps(summary.to_dict(), ensure_ascii=False)
    text = render_quarantine_summary(summary)
    assert SECRET not in payload
    assert SECRET not in text
    assert "token=***" in payload
    assert "bearer ***" in payload


def test_quarantine_summary_handles_empty_batch() -> None:
    summary = build_quarantine_summary(_report((_row(0, RowStatus.ACCEPTED, ()),)))
    assert summary.total_quarantined == 0
    assert summary.reason_code_counts == ()
    text = render_quarantine_summary(summary)
    assert "无" in text
