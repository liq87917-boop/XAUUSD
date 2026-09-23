"""GOLD-016 L3 人工决策记录单元测试（临时目录 / Mock 审计产物 / 零数据库 / 零网络）。

覆盖：

- 退出码映射稳定；人工输入（``decision`` / ``reviewer`` / ``note`` / ``reason_code`` /
  ``revision`` / ``supersedes``）逐项校验与脱敏、限长；
- GOLD-015 packet 文档的**逐项**完整性核验：文档身份 / 安全字段被削弱 / ``packet_id`` 形态 /
  handoff 结构 / 缺口与检查算术 / ``unmet_check_keys`` 可重算 / 三个布尔合取 / readiness 同源 /
  plan 与收据绑定 / recheck 合取 / 批准与执行计数 / verification 干净 / **禁止证据时间键** /
  **禁止未来时间** → 任一不合法即 fail-closed 且**零写入**；
- ``approve`` 门禁：只在 packet 本身可提交时允许；BLOCKED → 拒绝（零写入）；
  ``reject`` / ``needs_changes`` 可记录但**绝不**改变资格状态；
- 记录的四个语义布尔 + 安全字段恒定；``record_id`` 幂等 / 内容寻址 / byte-stable；
- 既有记录**绝不**被静默覆盖（显式 revision + supersedes）；既有记录被改写 → fail-closed；
- ``verify_decision_record`` 防伪核验（重新推导 ``record_id``、与当前 packet 比对）；
- CLI 编排（缺参数 / naive 时点 / 互斥参数 / 退出码 / stdout 纯净 / Markdown 保留 blocker）；
- 源码守卫：本层**绝不**联网 / 写库 / 调用 intake / 触碰 PROJECT_STATE 或文件 mtime。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.evidence_decision_record import main as cli_main
from src.common import hashing
from src.evidence import (
    DECISION_PACKET_KIND,
    DECISION_PACKET_SCHEMA_VERSION,
    DECISION_RECORD_ACTIONS,
    DECISION_RECORD_KIND,
    DECISION_SCOPE,
    EVIDENCE_CONTRACT_VERSION,
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    FORBIDDEN_EVIDENCE_FIELDS,
    HANDOFF_REPORT_NAME,
    OPERATOR_EXPLICIT_FLAG,
    OPERATOR_MANIFEST_REPORT,
    PACKET_CHECKS,
    PACKET_EXECUTION_MODE,
    QUALIFICATION_RECHECK_REPORT,
    REVIEWER_KIND,
    DecisionRecordArgumentError,
    DecisionRecordCode,
    DecisionRecordNotSubmittableError,
    DecisionRecordPathError,
    DecisionRecordStateError,
    DecisionRecordVerificationError,
    DecisionRecordWriteError,
    HumanDecision,
    LockConflictError,
    SingleInstanceLock,
    build_decision_record,
    compute_record_id,
    decision_record_exit_code_for,
    load_decision_packet,
    main_verification_exit_code,
    render_decision_record_summary,
    run_decision_record,
    verify_decision_record,
)
from src.evidence import decision_record as record_module
from src.evidence.handoff import thresholds as handoff_thresholds
from src.evidence.intake_receipt import IntakeReceiptStatus
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
PACKET_AT = MOMENT + timedelta(hours=4)
DECISION_AT = MOMENT + timedelta(hours=5)
REVIEWER = "operator-l3-li"
REASON = "APPROVED_AFTER_L3_REVIEW"
SECRET = "sk-livesecret0123456789"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


# ---------------------------------------------------------------------------
# 夹具：**schema 准确**的 GOLD-015 packet 文档（Mock 仅用于控制流）
# ---------------------------------------------------------------------------
def check_view(
    key: str,
    *,
    comparator: str,
    current: int | float,
    required: int | float,
    scope: str = "author",
    evaluable: bool = True,
) -> dict[str, Any]:
    """单条就绪度检查视图（``status`` / ``remaining`` 与阈值口径自洽）。"""
    if comparator == ">=":
        status = "PASS" if current >= required else "BLOCKED"
        remaining: int | float = max(0, required - current)
    else:
        status = "PASS" if (evaluable and current <= required) else "BLOCKED"
        remaining = round(max(0.0, current - required), 6) if evaluable else 0.0
    return {
        "key": key,
        "scope": scope,
        "metric": f"metric-{key}",
        "current": current,
        "required": required,
        "comparator": comparator,
        "status": status,
        "evaluable": evaluable,
        "remaining": remaining,
    }


def scope_gap(
    scope: str, *, checks: list[dict[str, Any]], eligible: int, required: int
) -> dict[str, Any]:
    """单个 scope 的缺口视图（算术与检查结论自洽）。"""
    blocked = [item["key"] for item in checks if item["status"] != "PASS"]
    news = scope == "news"
    return {
        "scope": scope,
        "status": "PASS" if not blocked else "BLOCKED",
        "ready": not blocked,
        "eligible": eligible,
        "required": required,
        "remaining": max(0, required - eligible),
        "certified": eligible,
        "not_oos_eligible": 0,
        "coverage_applicable": news,
        "coverage_days": 240 if news else 0,
        "coverage_required": 200 if news else 0,
        "coverage_remaining": 0,
        "source_share_applicable": news,
        "source_share_limit": 0.4 if news else 0.0,
        "source_share_evaluable": news,
        "source_share_remaining": 0.0,
        "remaining_checks": len(blocked),
        "checks": checks,
    }


def author_and_news(evidence_ready: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造 author / news 两个 scope 的缺口（``evidence_ready`` 决定 author 是否达标）。"""
    author_checks = [
        check_view(
            "author_eligible", comparator=">=", current=30 if evidence_ready else 3, required=30
        ),
        check_view("author_dedup", comparator="<=", current=0.05, required=0.1, evaluable=True),
    ]
    news_checks = [
        check_view("news_events", comparator=">=", current=210, required=200, scope="news"),
        check_view("news_coverage_days", comparator=">=", current=240, required=200, scope="news"),
        check_view(
            "news_source_share",
            comparator="<=",
            current=0.33,
            required=0.4,
            scope="news",
            evaluable=True,
        ),
    ]
    return (
        scope_gap(
            "author", checks=author_checks, eligible=30 if evidence_ready else 3, required=30
        ),
        scope_gap("news", checks=news_checks, eligible=210, required=200),
    )


def unmet_keys(author: dict[str, Any], news: dict[str, Any]) -> list[str]:
    """由两个 scope 的检查状态重新推导"仍未 PASS 的检查键"。"""
    return sorted(
        {
            item["key"]
            for gap in (author, news)
            for item in gap["checks"]
            if item["status"] != "PASS"
        }
    )


def receipt_entries() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """一条已批准 + 已**显式**执行的执行结果与 ledger 摘要（脱敏、无证据时间）。"""
    entry = {
        "fingerprint": DIGEST_A,
        "decision_id": DIGEST_B,
        "revision": 1,
        "operator_scope": "author",
        "package_dir": "pkg-author-01",
        "evidence_files": ["author.jsonl"],
        "content_sha256": [DIGEST_B],
        "rows": 1,
        "acceptable_rows": 1,
        "intake_executed": True,
        "approved_for_explicit_intake": True,
        "data_qualification_passed": False,
        "requires_explicit_operator_action": True,
    }
    operator = {
        "path": "author_manifest.json",
        "artifact_sha256": DIGEST_A,
        "report": OPERATOR_MANIFEST_REPORT,
        "scope": "author",
        "dry_run": False,
        "generated_at": (PACKET_AT - timedelta(hours=1)).isoformat(),
        "input_path": "author.jsonl",
        "input_sha256": DIGEST_B,
        "counts": {"persisted": 1, "quarantined": 0},
        "row_count": 1,
        "explicit_execution": True,
        "landed_rows": 1,
    }
    ledger = [{"fingerprint": DIGEST_A, "revision": 1, "decision_id": DIGEST_B}]
    return [entry], [operator], ledger


def packet_handoff_block(
    author: dict[str, Any],
    news: dict[str, Any],
    unmet: list[str],
    reason_codes: list[str],
    evidence_ready: bool,
) -> dict[str, Any]:
    """packet 文档里的 handoff 块（与 gaps 同源、算术自洽、无证据时间）。"""
    return {
        "handoff": {
            "path": "handoff.json",
            "artifact_sha256": DIGEST_B,
            "report": HANDOFF_REPORT_NAME,
            "schema_version": 1,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "as_of": (PACKET_AT - timedelta(minutes=10)).isoformat(),
            "status": "BLOCKED_PENDING_HUMAN_REVIEW" if evidence_ready else "BLOCKED",
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "quantified_thresholds_met": evidence_ready,
            "ready_for_human_review": evidence_ready,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "total_remaining_gap_count": (
                author["remaining_checks"] + news["remaining_checks"]
            ),
            "readiness_fingerprint": DIGEST_A,
            "reason_codes": reason_codes,
            "unmet_check_keys": unmet,
            "thresholds": {key: float(value) for key, value in handoff_thresholds().items()},
        }
    }


def packet_document(
    *,
    evidence_ready: bool = False,
    receipt_verified: bool = True,
    recheck_present: bool = True,
    recheck_ready: bool = False,
    with_state: bool = True,
    receipt_file_provided: bool = True,
    generated_at: datetime = PACKET_AT,
) -> dict[str, Any]:
    """构造**与 GOLD-015 契约一致**的 packet 文档（Mock 仅用于控制流，不代表任何资格）。"""
    author, news = author_and_news(evidence_ready)
    unmet = unmet_keys(author, news)
    reason_codes = sorted({f"{item}:pending" for item in unmet})
    total_remaining = author["remaining_checks"] + news["remaining_checks"]
    recheck_ready_value = recheck_ready and receipt_verified
    submit = evidence_ready and receipt_verified and recheck_ready_value
    receipt_status = (
        IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED.value
        if receipt_verified
        else IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE.value
    )
    entries: list[dict[str, Any]] = []
    operators: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    if receipt_verified:
        entries, operators, ledger = receipt_entries()
    state = None
    if with_state:
        state = {
            "path": "readiness_state.json",
            "artifact_sha256": DIGEST_B,
            "kind": "evidence_readiness_snapshot",
            "schema_version": 1,
            "fingerprint": DIGEST_A,
            "generated_at": (PACKET_AT - timedelta(minutes=30)).isoformat(),
            "status": "PASS" if evidence_ready else "BLOCKED",
            "ready_for_human_review": evidence_ready,
            "total_remaining_gap_count": total_remaining,
            "reason_codes": reason_codes,
            "scopes": {"author": {"eligible": author["eligible"], "required": 30}},
        }
    recheck = None
    if recheck_present:
        recheck = {
            "path": "phase33_recheck.json",
            "artifact_sha256": DIGEST_A,
            "report": QUALIFICATION_RECHECK_REPORT,
            "as_of": (PACKET_AT - timedelta(hours=2)).isoformat(),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "ready": recheck_ready,
            "gate": {
                "qualification_ready": recheck_ready,
                "qualification_pass_count": 7 if recheck_ready else 0,
                "qualification_blocked_count": 0 if recheck_ready else 2,
                "readiness_ready": evidence_ready,
                "readiness_blocked_scope_count": 0 if evidence_ready else 2,
            },
            "is_qualification_decision": False,
            "human_gate_required_for_phase_change": True,
        }
    document: dict[str, Any] = {
        "kind": DECISION_PACKET_KIND,
        "report": DECISION_PACKET_KIND,
        "schema_version": DECISION_PACKET_SCHEMA_VERSION,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "generated_at": generated_at.isoformat(),
        "packet_id": DIGEST_A,
        "status": "READY_FOR_L3_HUMAN_GATE" if submit else "BLOCKED_PENDING_EVIDENCE",
        "execution_mode": PACKET_EXECUTION_MODE,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "human_gate_level": "L3",
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "data_qualification_passed_count": 0,
        "auto_intake_allowed": False,
        "writes_database": False,
        "requires_explicit_operator_action": True,
        "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
        "evidence_ready_for_human_review": evidence_ready,
        "receipt_verified": receipt_verified,
        "qualification_recheck_ready": recheck_ready_value,
        "submit_to_l3_human_gate": submit,
    }
    document.update(packet_handoff_block(author, news, unmet, reason_codes, evidence_ready))
    document.update(
        {
            "readiness_state": state,
            "readiness_binding": "STATE_FILE_MATCHED" if with_state else "NOT_PROVIDED",
            "plan": {
                "path": "evidence_intake_plan.json",
                "plan_id": DIGEST_B,
                "status": "READY_FOR_EXPLICIT_INTAKE",
                "ledger_path": "inbox_review_ledger.json",
                "approved_list_path": "approved_for_intake.json",
            },
            "receipt": {
                "path": "evidence_intake_receipt.json" if receipt_file_provided else None,
                "artifact_sha256": DIGEST_A if receipt_file_provided else None,
                "receipt_id": DIGEST_B,
                "status": receipt_status,
                "receipt_file_provided": receipt_file_provided,
                "receipt_file_matched": True if receipt_file_provided else None,
            },
            "approved_entries": entries,
            "approved_entry_count": len(entries),
            "landed_rows": 1 if receipt_verified else 0,
            "operator_results": operators,
            "qualification_recheck": recheck,
            "ledger_revision_digest": ledger,
            "gaps": {
                "author": author,
                "news": news,
                "total_remaining_gap_count": total_remaining,
                "reason_codes": reason_codes,
                "unmet_check_keys": unmet,
            },
            "verification": {
                "violations": [],
                "revalidated_at": generated_at.isoformat(),
                "inbox_packages": 1,
                "preflight_pass": 1 if receipt_verified else 0,
                "ledger_fingerprints": len(ledger),
            },
            "evidence_time_semantics": {
                "contains_evidence_times": False,
                "audit_times_only": ["generated_at", "handoff.as_of"],
                "note": "审计时点，不是证据时间。",
            },
            "written_path": None,
            "next_step": "仍须 L3 人工 Gate。",
            "notes": ["测试夹具：仅用于控制流，不代表任何真实数据资格。"],
        }
    )
    return document


def write_packet(path: Path, document: dict[str, Any]) -> Path:
    """把 packet 文档写成 JSON 文件（UTF-8、稳定排序）并返回路径。"""
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def blocked_packet(tmp_path: Path) -> Path:
    """仍 BLOCKED（不可提交）的合法 packet 文件。"""
    return write_packet(tmp_path / "packet_blocked.json", packet_document())


def submittable_packet(tmp_path: Path) -> Path:
    """可提交 L3 人工 Gate 的合法 packet 文件。"""
    return write_packet(
        tmp_path / "packet_ready.json", packet_document(evidence_ready=True, recheck_ready=True)
    )


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内所有文件的相对路径与内容摘要（"什么都没被改写" 的断言用）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }




# ---------------------------------------------------------------------------
# 退出码与人工输入
# ---------------------------------------------------------------------------
def test_exit_code_mapping_is_stable() -> None:
    assert decision_record_exit_code_for(DecisionRecordArgumentError("x")) == EXIT_CONFIG_ERROR
    assert decision_record_exit_code_for(DecisionRecordPathError("x")) == EXIT_UNUSABLE
    assert decision_record_exit_code_for(DecisionRecordWriteError("x")) == EXIT_UNUSABLE
    assert decision_record_exit_code_for(DecisionRecordStateError("x")) == EXIT_STATE_INVALID
    assert (
        decision_record_exit_code_for(DecisionRecordVerificationError([], []))
        == EXIT_STATE_INVALID
    )
    assert decision_record_exit_code_for(DecisionRecordNotSubmittableError("x")) == EXIT_BLOCKED
    assert decision_record_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert decision_record_exit_code_for(ValueError("x")) == EXIT_STATE_INVALID
    assert main_verification_exit_code(True) == EXIT_OK
    assert main_verification_exit_code(False) == EXIT_STATE_INVALID


@pytest.mark.parametrize("decision", DECISION_RECORD_ACTIONS)
def test_build_accepts_the_three_explicit_human_decisions(
    tmp_path: Path, decision: str
) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(
        packet, decision=decision, reviewer=REVIEWER, moment=DECISION_AT
    )
    payload = record.to_dict()
    assert payload["kind"] == DECISION_RECORD_KIND
    assert payload["decision"] == decision
    assert payload["human_decision"] == decision
    assert payload["human_decision_recorded"] is True
    assert payload["packet_verified"] is True


def test_build_approve_on_blocked_packet_is_refused(tmp_path: Path) -> None:
    packet = load_decision_packet(blocked_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordNotSubmittableError) as error:
        build_decision_record(packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT)
    assert DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value in str(error.value)


@pytest.mark.parametrize("decision", ["reject", "needs_changes"])
def test_reject_and_needs_changes_are_recordable_but_change_nothing(
    tmp_path: Path, decision: str
) -> None:
    packet = load_decision_packet(blocked_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(
        packet, decision=decision, reviewer=REVIEWER, moment=DECISION_AT
    )
    payload = record.to_dict()
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["phase_transition_executed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["writes_database"] is False
    assert payload["writes_project_state"] is False
    assert payload["project_state_modified"] is False
    assert payload["approve_gate"]["applies"] is False
    assert payload["approve_gate"]["satisfied"] is None


@pytest.mark.parametrize(
    "decision", ["APPROVE", "yes", "", "approve ", "needs_changes_please", "1"]
)
def test_invalid_decision_is_rejected_without_silent_conversion(
    tmp_path: Path, decision: str
) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(packet, decision=decision, reviewer=REVIEWER, moment=DECISION_AT)
    assert DecisionRecordCode.DECISION_INVALID.value in str(error.value)


@pytest.mark.parametrize(
    "reviewer",
    [
        "",
        "   ",
        "operator li",
        "operator/li",
        "-operator",
        "x" * 65,
        "my-password-123",
        "api_key_abcdef",
        "a" * 48,
    ],
)
def test_invalid_reviewer_is_refused(tmp_path: Path, reviewer: str) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(packet, decision="reject", reviewer=reviewer, moment=DECISION_AT)
    assert DecisionRecordCode.REVIEWER_INVALID.value in str(error.value)


@pytest.mark.parametrize(
    "reviewer", ["operator-l3-li", "reviewer.zhang@example.com", "L3_Review_Bot+1"]
)
def test_valid_reviewer_labels_are_accepted(tmp_path: Path, reviewer: str) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(packet, decision="reject", reviewer=reviewer, moment=DECISION_AT)
    assert record.to_dict()["reviewer"] == reviewer
    assert record.to_dict()["reviewer_kind"] == REVIEWER_KIND


def test_note_is_redacted_and_length_bounded(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(
        packet,
        decision="reject",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        note=f"证据不足；附带的临时凭据 {SECRET} 不应进库",
    )
    note = record.to_dict()["note"]
    assert note is not None
    assert SECRET not in note
    assert "sk-***" in note

    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(
            packet,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            note="x" * (record_module.MAX_RECORD_NOTE_INPUT_CHARS + 1),
        )
    assert DecisionRecordCode.NOTE_TOO_LONG.value in str(error.value)


@pytest.mark.parametrize("reason_code", ["approved", "Approve", "has space", "x" * 65, ""])
def test_invalid_reason_code_is_refused(tmp_path: Path, reason_code: str) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(
            packet,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            reason_code=reason_code,
        )
    assert DecisionRecordCode.REASON_CODE_INVALID.value in str(error.value)


def test_reason_code_uppercase_is_preserved(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(
        packet, decision="reject", reviewer=REVIEWER, moment=DECISION_AT, reason_code=REASON
    )
    assert record.to_dict()["reason_code"] == REASON


def test_revision_and_supersedes_rules(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(
            packet, decision="reject", reviewer=REVIEWER, moment=DECISION_AT, revision=2
        )
    assert DecisionRecordCode.REVISION_INVALID.value in str(error.value)

    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(
            packet,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            revision=2,
            supersedes="not-a-record-id",
        )
    assert DecisionRecordCode.SUPERSEDES_MISMATCH.value in str(error.value)

    with pytest.raises(DecisionRecordArgumentError) as error:
        build_decision_record(
            packet,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            revision=1,
            supersedes=DIGEST_B,
        )
    assert DecisionRecordCode.REVISION_INVALID.value in str(error.value)

    record = build_decision_record(
        packet,
        decision="reject",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        revision=2,
        supersedes=DIGEST_B,
    )
    payload = record.to_dict()
    assert payload["revision"] == 2
    assert payload["supersedes"] == DIGEST_B


def test_naive_moment_is_rejected(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    with pytest.raises(DecisionRecordArgumentError):
        build_decision_record(
            packet,
            decision="reject",
            reviewer=REVIEWER,
            moment=datetime(2026, 9, 23, 5, 0),
        )



# ---------------------------------------------------------------------------
# packet 文档的逐项完整性核验（fail-closed）
# ---------------------------------------------------------------------------
def tampered(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> str:
    """改写 packet 文档并断言加载 fail-closed；返回异常消息（用于断言稳定原因码）。"""
    document = packet_document()
    mutate(document)
    path = write_packet(tmp_path / "tampered.json", document)
    with pytest.raises(DecisionRecordStateError) as error:
        load_decision_packet(path, moment=DECISION_AT)
    return str(error.value)


def test_valid_blocked_packet_binds_content_and_artifact(tmp_path: Path) -> None:
    path = blocked_packet(tmp_path)
    binding = load_decision_packet(path, moment=DECISION_AT)
    assert binding.packet_id == DIGEST_A
    assert binding.content_sha256 == hashing.sha256_text(
        record_module._canonical_json(json.loads(path.read_text(encoding="utf-8")))
    )
    assert binding.artifact_sha256 == hashing.sha256_bytes(path.read_bytes())
    assert binding.submittable is False
    assert binding.status == "BLOCKED_PENDING_EVIDENCE"
    assert binding.receipt_verified is True
    assert binding.qualification_recheck_ready is False
    assert binding.blocker_code == PHASE3_3_BLOCKER_CODE
    assert binding.checks == PACKET_CHECKS
    assert binding.written_path is None
    assert binding.written_path_matches is None


def test_valid_submittable_packet_is_accepted(tmp_path: Path) -> None:
    binding = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    assert binding.submittable is True
    assert binding.status == "READY_FOR_L3_HUMAN_GATE"
    assert binding.evidence_ready_for_human_review is True
    assert binding.qualification_recheck_ready is True


def test_missing_packet_is_a_path_error(tmp_path: Path) -> None:
    with pytest.raises(DecisionRecordPathError) as error:
        load_decision_packet(tmp_path / "absent.json", moment=DECISION_AT)
    assert DecisionRecordCode.PACKET_NOT_FOUND.value in str(error.value)


@pytest.mark.parametrize("payload", ["{not json", "[1, 2, 3]", '"text"'])
def test_unreadable_packet_is_fail_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "broken.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(DecisionRecordStateError) as error:
        load_decision_packet(path, moment=DECISION_AT)
    assert DecisionRecordCode.PACKET_UNREADABLE.value in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "something_else"),
        ("report", "another_report"),
        ("schema_version", 2),
        ("contract_version", "evidence-intake-v0"),
        ("execution_mode", "AUTO_APPROVE_EVERYTHING"),
    ],
)
def test_document_identity_is_enforced(tmp_path: Path, field: str, value: object) -> None:
    message = tampered(tmp_path, lambda doc: doc.__setitem__(field, value))
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize("packet_id", ["", "A" * 64, "z" * 64, DIGEST_A[:-1]])
def test_packet_id_must_be_content_addressed(tmp_path: Path, packet_id: str) -> None:
    message = tampered(tmp_path, lambda doc: doc.__setitem__("packet_id", packet_id))
    assert DecisionRecordCode.PACKET_ID_INVALID.value in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("blocker_active", False),
        ("human_gate_required", False),
        ("human_gate_level", "L2"),
        ("data_qualification_passed", True),
        ("phase_transition_allowed", True),
        ("data_qualification_passed_count", 7),
        ("auto_intake_allowed", True),
        ("writes_database", True),
        ("requires_explicit_operator_action", False),
        ("operator_explicit_flag", "--dry-run"),
        ("blocker_code", "SOMETHING_ELSE"),
    ],
)
def test_weakened_safety_fields_are_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    message = tampered(tmp_path, lambda doc: doc.__setitem__(field, value))
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["handoff"].__setitem__("report", "other"),
        lambda doc: doc["handoff"].__setitem__("schema_version", 99),
        lambda doc: doc["handoff"].__setitem__("contract_version", "v0"),
        lambda doc: doc["handoff"].__setitem__("blocker_code", "OTHER"),
        lambda doc: doc["handoff"].__setitem__("blocker_active", False),
        lambda doc: doc["handoff"].__setitem__("data_qualification_passed", True),
        lambda doc: doc["handoff"].__setitem__("thresholds", {"a": 1.0}),
        lambda doc: doc["handoff"].__setitem__("artifact_sha256", "short"),
        lambda doc: doc["handoff"].__setitem__("readiness_fingerprint", "short"),
        lambda doc: doc["handoff"].__setitem__("ready_for_human_review", True),
        lambda doc: doc["handoff"].__setitem__("quantified_thresholds_met", True),
        lambda doc: doc["handoff"].__setitem__("status", "PASS"),
        lambda doc: doc["handoff"].__setitem__("reason_codes", ["b", "a"]),
    ],
)
def test_handoff_block_is_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["gaps"]["author"].__setitem__("scope", "news"),
        lambda doc: doc["gaps"]["author"].__setitem__("remaining_checks", 0),
        lambda doc: doc["gaps"]["author"].__setitem__("remaining", 0),
        lambda doc: doc["gaps"]["author"].__setitem__("status", "PASS"),
        lambda doc: doc["gaps"]["author"].__setitem__("ready", True),
        lambda doc: doc["gaps"]["author"].__setitem__("coverage_applicable", True),
        lambda doc: doc["gaps"]["author"].__setitem__("eligible", "3"),
        lambda doc: doc["gaps"]["author"]["checks"][0].__setitem__("status", "PASS"),
        lambda doc: doc["gaps"]["author"]["checks"][0].__setitem__("remaining", 0),
        lambda doc: doc["gaps"]["author"]["checks"][0].__setitem__("comparator", "=="),
        lambda doc: doc["gaps"]["author"]["checks"][0].pop("metric"),
        lambda doc: doc["gaps"]["author"].__setitem__("checks", "not-a-list"),
        lambda doc: doc["gaps"].__setitem__("unmet_check_keys", []),
        lambda doc: doc["gaps"].__setitem__("total_remaining_gap_count", 0),
        lambda doc: doc["gaps"].__setitem__("reason_codes", []),
        lambda doc: doc["gaps"].__setitem__("news", {}),
        lambda doc: doc["handoff"].__setitem__("unmet_check_keys", []),
        lambda doc: doc["handoff"].__setitem__("total_remaining_gap_count", 0),
    ],
)
def test_gap_arithmetic_and_check_recomputation_are_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["readiness_state"].__setitem__("fingerprint", DIGEST_B),
        lambda doc: doc["readiness_state"].__setitem__("ready_for_human_review", True),
        lambda doc: doc["readiness_state"].__setitem__("total_remaining_gap_count", 0),
        lambda doc: doc["readiness_state"].__setitem__("reason_codes", []),
        lambda doc: doc["readiness_state"].__setitem__("kind", "other"),
        lambda doc: doc["readiness_state"].__setitem__("schema_version", 9),
        lambda doc: doc.__setitem__("readiness_binding", "NOT_PROVIDED"),
        lambda doc: doc.__setitem__("readiness_state", None),
    ],
)
def test_readiness_same_source_is_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.READINESS_MISMATCH.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.__setitem__("submit_to_l3_human_gate", True),
        lambda doc: doc.__setitem__("status", "READY_FOR_L3_HUMAN_GATE"),
        lambda doc: doc.__setitem__("evidence_ready_for_human_review", True),
        lambda doc: doc.__setitem__("evidence_ready_for_human_review", 1),
        lambda doc: doc.__setitem__("receipt_verified", "yes"),
        lambda doc: doc.__setitem__("qualification_recheck_ready", True),
    ],
)
def test_flag_conjunction_is_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


def test_readiness_state_absent_is_accepted_with_not_provided_binding(tmp_path: Path) -> None:
    path = write_packet(
        tmp_path / "no_state.json", packet_document(with_state=False)
    )
    binding = load_decision_packet(path, moment=DECISION_AT)
    assert binding.readiness_binding == "NOT_PROVIDED"

    message = tampered(
        tmp_path, lambda doc: doc.__setitem__("readiness_state", None)
    )
    assert DecisionRecordCode.READINESS_MISMATCH.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.__setitem__("plan", {}),
        lambda doc: doc["plan"].__setitem__("plan_id", "short"),
        lambda doc: doc["plan"].__setitem__("status", "MAYBE"),
        lambda doc: doc.__setitem__("receipt", {}),
        lambda doc: doc["receipt"].__setitem__("receipt_id", "short"),
        lambda doc: doc["receipt"].__setitem__(
            "status", IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE.value
        ),
        lambda doc: doc["receipt"].__setitem__("receipt_file_provided", False),
        lambda doc: doc["receipt"].__setitem__("path", None),
        lambda doc: doc["receipt"].__setitem__("artifact_sha256", "short"),
        lambda doc: doc["receipt"].__setitem__("receipt_file_matched", None),
    ],
)
def test_plan_and_receipt_binding_is_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["qualification_recheck"].__setitem__("report", "other"),
        lambda doc: doc["qualification_recheck"].__setitem__("blocker_code", "OTHER"),
        lambda doc: doc["qualification_recheck"].__setitem__("blocker_active", False),
        lambda doc: doc["qualification_recheck"].__setitem__("human_gate_required", False),
        lambda doc: doc["qualification_recheck"].__setitem__(
            "human_gate_required_for_phase_change", False
        ),
        lambda doc: doc["qualification_recheck"].__setitem__("is_qualification_decision", True),
        lambda doc: doc["qualification_recheck"].__setitem__("as_of", "no-tz"),
        lambda doc: doc["qualification_recheck"].__setitem__("artifact_sha256", "short"),
        lambda doc: doc["qualification_recheck"].__setitem__("ready", True),
        lambda doc: doc["qualification_recheck"].__setitem__("gate", {}),
        lambda doc: doc["qualification_recheck"]["gate"].__setitem__(
            "qualification_pass_count", -1
        ),
    ],
)
def test_recheck_binding_is_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["approved_entries"].append({}),
        lambda doc: doc["approved_entries"][0].__setitem__("fingerprint", "short"),
        lambda doc: doc["approved_entries"][0].__setitem__("decision_id", ""),
        lambda doc: doc["approved_entries"][0].__setitem__("revision", 0),
        lambda doc: doc["approved_entries"][0].__setitem__("content_sha256", ["short"]),
        lambda doc: doc["approved_entries"][0].__setitem__("data_qualification_passed", True),
        lambda doc: doc["approved_entries"][0].__setitem__("intake_executed", "yes"),
        lambda doc: doc["ledger_revision_digest"].append({}),
        lambda doc: doc["verification"].__setitem__("ledger_fingerprints", 0),
        lambda doc: doc["operator_results"][0].__setitem__("dry_run", True),
        lambda doc: doc["operator_results"][0].__setitem__("report", "other"),
        lambda doc: doc["operator_results"][0].__setitem__("scope", "macro"),
        lambda doc: doc["operator_results"][0]["counts"].__setitem__("persisted", 5),
        lambda doc: doc.__setitem__("landed_rows", 3),
        lambda doc: doc.__setitem__("approved_entry_count", 3),
    ],
)
def test_approved_and_operator_counts_are_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert DecisionRecordCode.PACKET_TAMPERED.value in message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["verification"].__setitem__("violations", [{"code": "X"}]),
        lambda doc: doc["verification"].__setitem__("revalidated_at", "2026-01-01T00:00:00+00:00"),
        lambda doc: doc["verification"].__setitem__("preflight_pass", 9),
        lambda doc: doc["verification"].__setitem__("inbox_packages", -1),
        lambda doc: doc.__setitem__("notes", "not-a-list"),
        lambda doc: doc.__setitem__("written_path", 12),
        lambda doc: doc["evidence_time_semantics"].__setitem__("contains_evidence_times", True),
        lambda doc: doc["evidence_time_semantics"].__setitem__("audit_times_only", []),
    ],
)
def test_verification_block_and_time_semantics_are_enforced(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    message = tampered(tmp_path, mutate)
    assert any(
        code in message
        for code in (
            DecisionRecordCode.PACKET_TAMPERED.value,
            DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION.value,
        )
    )


def test_evidence_time_keys_are_rejected_recursively(tmp_path: Path) -> None:
    message = tampered(
        tmp_path,
        lambda doc: doc["gaps"]["author"]["checks"][0].__setitem__(
            "available_at", "2026-05-01T00:00:00+00:00"
        ),
    )
    assert DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION.value in message


def test_future_packet_is_refused(tmp_path: Path) -> None:
    path = write_packet(
        tmp_path / "future.json", packet_document(generated_at=PACKET_AT + timedelta(days=1))
    )
    with pytest.raises(DecisionRecordStateError) as error:
        load_decision_packet(path, moment=DECISION_AT)
    assert DecisionRecordCode.FUTURE_TIMESTAMP.value in str(error.value)


def test_written_path_is_bound_when_present(tmp_path: Path) -> None:
    path = blocked_packet(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["written_path"] = str(path)
    write_packet(path, document)
    binding = load_decision_packet(path, moment=DECISION_AT)
    assert binding.written_path == str(path)
    assert binding.written_path_matches is True

    document["written_path"] = str(tmp_path / "somewhere-else.json")
    write_packet(path, document)
    binding = load_decision_packet(path, moment=DECISION_AT)
    assert binding.written_path_matches is False


def test_content_digest_is_formatting_independent(tmp_path: Path) -> None:
    """内容摘要只取决于**语义**，产物摘要才取决于字节（packet 被重新格式化不算篡改）。"""
    path = blocked_packet(tmp_path)
    first = load_decision_packet(path, moment=DECISION_AT)
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    second = load_decision_packet(path, moment=DECISION_AT)
    assert first.content_sha256 == second.content_sha256
    assert first.artifact_sha256 != second.artifact_sha256



# ---------------------------------------------------------------------------
# 内容寻址 / 幂等 / 语义边界
# ---------------------------------------------------------------------------
def test_record_id_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    packet = load_decision_packet(packet_path, moment=DECISION_AT)
    first = build_decision_record(
        packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT, note="证据齐备"
    )
    second = build_decision_record(
        packet,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT + timedelta(hours=3),
        note="证据齐备",
    )
    assert first.record_id == second.record_id
    assert first.record_id == compute_record_id(first)
    assert len(first.record_id) == 64
    assert first.to_dict()["record_id"] == second.to_dict()["record_id"]
    # 审计时点不同 → 文档不同（但身份相同）
    assert first.to_dict()["decision_at"] != second.to_dict()["decision_at"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda kwargs: kwargs.update({"decision": "reject"}),
        lambda kwargs: kwargs.update({"reviewer": "other-reviewer"}),
        lambda kwargs: kwargs.update({"note": "改了口径"}),
        lambda kwargs: kwargs.update({"reason_code": REASON}),
        lambda kwargs: kwargs.update({"revision": 2, "supersedes": DIGEST_B}),
    ],
)
def test_record_id_changes_with_every_constrained_input(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    base_kwargs: dict[str, Any] = {
        "decision": "approve",
        "reviewer": REVIEWER,
        "note": "证据齐备",
    }
    base = build_decision_record(packet, moment=DECISION_AT, **base_kwargs)
    mutate(base_kwargs)
    changed = build_decision_record(packet, moment=DECISION_AT, **base_kwargs)
    assert base.record_id != changed.record_id


def test_record_id_changes_when_packet_content_changes(tmp_path: Path) -> None:
    first_packet = submittable_packet(tmp_path)
    first = build_decision_record(
        load_decision_packet(first_packet, moment=DECISION_AT),
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
    )
    document = packet_document(evidence_ready=True, recheck_ready=True)
    document["verification"]["inbox_packages"] = 2
    second_packet = write_packet(tmp_path / "packet_ready_2.json", document)
    second = build_decision_record(
        load_decision_packet(second_packet, moment=DECISION_AT),
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
    )
    assert first.record_id != second.record_id
    assert first.packet.content_sha256 != second.packet.content_sha256


def test_record_document_is_byte_stable_and_free_of_evidence_times(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    first = build_decision_record(
        packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT
    )
    second = build_decision_record(
        packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT
    )
    dumped_first = json.dumps(first.to_dict(), ensure_ascii=False, sort_keys=True)
    dumped_second = json.dumps(second.to_dict(), ensure_ascii=False, sort_keys=True)
    assert dumped_first == dumped_second
    payload = first.to_dict()
    assert record_module._forbidden_key_paths(payload) == []
    assert payload["evidence_time_semantics"]["contains_evidence_times"] is False
    assert set(payload["audit_times"]) == {
        "decision_at",
        "generated_at",
        "packet_generated_at",
        "packet_verification_verified_at",
    }
    assert record_module._forbidden_key_paths(payload) == []


def test_record_document_contains_the_independent_semantic_facts(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    payload = build_decision_record(
        packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT
    ).to_dict()
    assert payload["human_decision_recorded"] is True
    assert payload["human_decision"] == "approve"
    assert payload["packet_verified"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["phase_transition_executed"] is False
    assert payload["decision_scope"] == DECISION_SCOPE
    assert payload["reviewer_kind"] == REVIEWER_KIND
    assert payload["execution_mode"] == record_module.DECISION_RECORD_EXECUTION_MODE
    assert payload["packet_verification"]["violations"] == []
    assert payload["packet_verification"]["checks_passed"] == list(PACKET_CHECKS)
    assert payload["packet_verification"]["packet_id_recomputable_from_record"] is False
    assert payload["approve_gate"]["packet_submittable"] is True
    assert payload["approve_gate"]["satisfied"] is True


def test_markdown_summary_keeps_the_human_gate_visible(tmp_path: Path) -> None:
    packet = load_decision_packet(submittable_packet(tmp_path), moment=DECISION_AT)
    record = build_decision_record(
        packet, decision="approve", reviewer=REVIEWER, moment=DECISION_AT
    )
    text = render_decision_record_summary(record)
    assert "human_decision_recorded" in text
    assert "data_qualification_passed" in text
    assert "phase_transition_allowed" in text
    assert "blocker_active" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "L3" in text
    assert record.record_id in text



# ---------------------------------------------------------------------------
# 运行入口：默认只读 / 原子写 / 锁 / 冲突（绝不静默改写历史）
# ---------------------------------------------------------------------------
def test_run_defaults_to_read_only(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    before = file_snapshot(tmp_path)
    record = run_decision_record(
        packet_path, decision="approve", reviewer=REVIEWER, moment=DECISION_AT
    )
    assert record.written_path is None
    assert record.record_id
    assert file_snapshot(tmp_path) == before


def test_run_writes_record_atomically(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "out" / "l3_record.json"
    record = run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        reason_code=REASON,
        out_path=out,
    )
    assert record.written_path is not None
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert payload["record_id"] == record.record_id
    assert payload["decision"] == "approve"
    assert payload["data_qualification_passed"] is False
    # 原子写不留 .tmp 残留（锁文件是审计留痕，允许存在）
    leftovers = [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []


def test_run_is_idempotent_for_same_record(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    first = run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    first_bytes = out.read_bytes()
    second = run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    assert first.record_id == second.record_id
    assert out.read_bytes() == first_bytes


def test_run_approve_on_blocked_packet_writes_nothing(tmp_path: Path) -> None:
    packet_path = blocked_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    before = file_snapshot(tmp_path)
    with pytest.raises(DecisionRecordNotSubmittableError):
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert not out.exists()
    assert file_snapshot(tmp_path) == before


def test_run_reject_on_blocked_packet_is_recorded(tmp_path: Path) -> None:
    packet_path = blocked_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    record = run_decision_record(
        packet_path,
        decision="needs_changes",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        note="证据不足，退回补充",
        out_path=out,
    )
    assert record.human_decision == "needs_changes"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["data_qualification_passed"] is False
    assert payload["blocker_active"] is True


def test_run_refuses_to_overwrite_the_packet_itself(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    before = packet_path.read_bytes()
    with pytest.raises(DecisionRecordPathError) as error:
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=packet_path,
        )
    assert DecisionRecordCode.PACKET_OVERWRITE_REFUSED.value in str(error.value)
    assert packet_path.read_bytes() == before


def test_run_refuses_directory_output(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    directory = tmp_path / "records"
    directory.mkdir()
    with pytest.raises(DecisionRecordPathError):
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=directory,
        )


def test_run_fails_closed_when_lock_is_held(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")
    with held, pytest.raises(LockConflictError):
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert not out.exists()


def test_run_wraps_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(record_module, "atomic_write_text", boom)
    with pytest.raises(DecisionRecordWriteError):
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert not out.exists()


def test_run_conflict_requires_explicit_revision_and_supersedes(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    first = run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    before = out.read_bytes()

    with pytest.raises(DecisionRecordStateError) as error:
        run_decision_record(
            packet_path,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert DecisionRecordCode.RECORD_CONFLICT.value in str(error.value)
    assert out.read_bytes() == before

    with pytest.raises(DecisionRecordStateError) as error:
        run_decision_record(
            packet_path,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            revision=2,
            supersedes=DIGEST_B,
            out_path=out,
        )
    assert DecisionRecordCode.SUPERSEDES_MISMATCH.value in str(error.value)
    assert out.read_bytes() == before

    with pytest.raises(DecisionRecordArgumentError) as error:
        run_decision_record(
            packet_path,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            revision=1,
            supersedes=first.record_id,
            out_path=out,
        )
    assert DecisionRecordCode.REVISION_INVALID.value in str(error.value)
    assert out.read_bytes() == before

    second = run_decision_record(
        packet_path,
        decision="reject",
        reviewer="operator-l3-zhang",
        moment=DECISION_AT,
        revision=2,
        supersedes=first.record_id,
        out_path=out,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["revision"] == 2
    assert payload["supersedes"] == first.record_id
    assert second.record_id != first.record_id
    assert payload["decision"] == "reject"


def test_run_refuses_to_overwrite_a_tampered_existing_record(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["decision"] = "reject"  # 改写内容但保留旧 record_id
    payload["human_decision"] = "reject"
    out.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tampered_bytes = out.read_bytes()

    with pytest.raises(DecisionRecordStateError) as error:
        run_decision_record(
            packet_path,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            revision=2,
            supersedes=str(payload["record_id"]),
            out_path=out,
        )
    assert any(
        code in str(error.value)
        for code in (
            DecisionRecordCode.RECORD_TAMPERED.value,
            DecisionRecordCode.RECORD_ID_MISMATCH.value,
        )
    )
    assert out.read_bytes() == tampered_bytes


def test_run_refuses_unknown_existing_document(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    out.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    before = out.read_bytes()
    with pytest.raises(DecisionRecordStateError) as error:
        run_decision_record(
            packet_path,
            decision="approve",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert DecisionRecordCode.RECORD_TAMPERED.value in str(error.value)
    assert out.read_bytes() == before


def test_run_on_tampered_packet_fails_closed_and_writes_nothing(tmp_path: Path) -> None:
    document = packet_document()
    document["blocker_active"] = False
    packet_path = write_packet(tmp_path / "tampered_packet.json", document)
    out = tmp_path / "l3_record.json"
    before = file_snapshot(tmp_path)
    with pytest.raises(DecisionRecordStateError) as error:
        run_decision_record(
            packet_path,
            decision="reject",
            reviewer=REVIEWER,
            moment=DECISION_AT,
            out_path=out,
        )
    assert DecisionRecordCode.PACKET_TAMPERED.value in str(error.value)
    assert file_snapshot(tmp_path) == before



# ---------------------------------------------------------------------------
# 防伪核验（记录 ↔ 当前 packet）
# ---------------------------------------------------------------------------
def test_verify_accepts_record_bound_to_current_packet(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    record = run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    verification = verify_decision_record(out, packet_path, moment=DECISION_AT)
    assert verification.verified is True
    assert verification.codes == ()
    assert verification.record_id == record.record_id
    assert verification.recomputed_record_id == record.record_id
    assert verification.packet_id_matches is True
    assert verification.content_sha256_matches is True
    assert verification.approve_still_valid is True
    payload = verification.to_dict()
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["human_gate_level"] == "L3"
    assert payload["blocker_active"] is True
    assert payload["packet_verified"] is True


def test_verify_detects_superseded_packet(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    # 重新生成一份**合法但更新**的 packet（例如重新聚合后 inbox 包数变化）
    document = packet_document(
        evidence_ready=True, recheck_ready=True, generated_at=PACKET_AT + timedelta(hours=1)
    )
    document["verification"]["inbox_packages"] = 2
    document["verification"]["revalidated_at"] = (PACKET_AT + timedelta(hours=1)).isoformat()
    write_packet(packet_path, document)

    verification = verify_decision_record(out, packet_path, moment=DECISION_AT + timedelta(hours=2))
    assert verification.verified is False
    assert DecisionRecordCode.PACKET_CONTENT_MISMATCH.value in verification.codes
    assert DecisionRecordCode.PACKET_STALE.value in verification.codes
    assert verification.content_sha256_matches is False
    assert verification.approve_still_valid is False


def test_verify_detects_record_tampering(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["reviewer"] = "someone-else"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    verification = verify_decision_record(out, packet_path, moment=DECISION_AT)
    assert verification.verified is False
    assert DecisionRecordCode.RECORD_ID_MISMATCH.value in verification.codes
    assert verification.record_id_matches is False


def test_verify_rejects_record_with_weakened_safety_fields(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["data_qualification_passed"] = True
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(DecisionRecordStateError) as error:
        verify_decision_record(out, packet_path, moment=DECISION_AT)
    assert DecisionRecordCode.RECORD_TAMPERED.value in str(error.value)


def test_verify_marks_approve_invalid_when_packet_is_no_longer_submittable(
    tmp_path: Path,
) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    # 同一审计时点下的合法 packet，但证据不再达标（等价于"复核结论被撤销后重新聚合"）
    write_packet(packet_path, packet_document())
    verification = verify_decision_record(out, packet_path, moment=DECISION_AT)
    assert verification.verified is False
    assert DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value in verification.codes
    assert verification.approve_still_valid is False


def test_verify_returns_none_approve_status_for_non_approve_records(tmp_path: Path) -> None:
    packet_path = blocked_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="reject",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    verification = verify_decision_record(out, packet_path, moment=DECISION_AT)
    assert verification.verified is True
    assert verification.approve_still_valid is None


def test_verify_requires_existing_record_and_packet(tmp_path: Path) -> None:
    packet_path = submittable_packet(tmp_path)
    with pytest.raises(DecisionRecordPathError) as error:
        verify_decision_record(tmp_path / "absent.json", packet_path, moment=DECISION_AT)
    assert DecisionRecordCode.RECORD_NOT_FOUND.value in str(error.value)

    out = tmp_path / "l3_record.json"
    run_decision_record(
        packet_path,
        decision="approve",
        reviewer=REVIEWER,
        moment=DECISION_AT,
        out_path=out,
    )
    with pytest.raises(DecisionRecordPathError) as error:
        verify_decision_record(out, tmp_path / "absent_packet.json", moment=DECISION_AT)
    assert DecisionRecordCode.PACKET_NOT_FOUND.value in str(error.value)



# ---------------------------------------------------------------------------
# CLI 编排
# ---------------------------------------------------------------------------
def test_cli_preflight_prints_record_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_path = submittable_packet(tmp_path)
    before = file_snapshot(tmp_path)
    code = cli_main(
        [
            "--packet",
            str(packet_path),
            "--decision",
            "approve",
            "--reviewer",
            REVIEWER,
            "--reason-code",
            REASON,
            "--json",
        ],
        moment=DECISION_AT,
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "approve"
    assert payload["data_qualification_passed"] is False
    assert file_snapshot(tmp_path) == before


def test_cli_approve_on_blocked_packet_exits_5_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_path = blocked_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    code = cli_main(
        [
            "--packet",
            str(packet_path),
            "--decision",
            "approve",
            "--reviewer",
            REVIEWER,
            "--out",
            str(out),
            "--json",
        ],
        moment=DECISION_AT,
    )
    captured = capsys.readouterr()
    assert code == EXIT_BLOCKED
    assert captured.out == ""
    assert DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value in captured.err
    assert not out.exists()


def test_cli_reject_on_blocked_packet_is_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_path = blocked_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    code = cli_main(
        [
            "--packet",
            str(packet_path),
            "--decision",
            "needs_changes",
            "--reviewer",
            REVIEWER,
            "--out",
            str(out),
            "--json",
        ],
        moment=DECISION_AT,
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_decision"] == "needs_changes"
    assert out.exists()


def test_cli_markdown_output_mentions_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_path = submittable_packet(tmp_path)
    code = cli_main(
        ["--packet", str(packet_path), "--decision", "approve", "--reviewer", REVIEWER],
        moment=DECISION_AT,
    )
    assert code == EXIT_OK
    text = capsys.readouterr().out
    assert "L3" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "data_qualification_passed" in text
    assert "phase_transition_allowed" in text


def test_cli_verify_record_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    assert (
        cli_main(
            [
                "--packet",
                str(packet_path),
                "--decision",
                "approve",
                "--reviewer",
                REVIEWER,
                "--out",
                str(out),
                "--json",
            ],
            moment=DECISION_AT,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert (
        cli_main(
            ["--packet", str(packet_path), "--verify-record", str(out), "--json"],
            moment=DECISION_AT,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert payload["codes"] == []


def test_cli_verify_record_fails_when_record_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_path = submittable_packet(tmp_path)
    out = tmp_path / "l3_record.json"
    cli_main(
        [
            "--packet",
            str(packet_path),
            "--decision",
            "approve",
            "--reviewer",
            REVIEWER,
            "--out",
            str(out),
            "--json",
        ],
        moment=DECISION_AT,
    )
    capsys.readouterr()
    write_packet(packet_path, packet_document())
    code = cli_main(
        ["--packet", str(packet_path), "--verify-record", str(out), "--json"],
        moment=DECISION_AT,
    )
    captured = capsys.readouterr()
    assert code == EXIT_STATE_INVALID
    payload = json.loads(captured.out)
    assert payload["verified"] is False
    assert DecisionRecordCode.PACKET_CONTENT_MISMATCH.value in payload["codes"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--decision", "approve", "--reviewer", REVIEWER],
        ["--packet", "x.json"],
        ["--packet", "x.json", "--decision", "approve"],
        ["--packet", "x.json", "--decision", "maybe", "--reviewer", REVIEWER],
        [
            "--packet",
            "x.json",
            "--verify-record",
            "r.json",
            "--decision",
            "approve",
            "--reviewer",
            REVIEWER,
        ],
        [
            "--packet",
            "x.json",
            "--decision",
            "approve",
            "--reviewer",
            REVIEWER,
            "--as-of",
            "2026-09-23T05:00:00",
        ],
    ],
)
def test_cli_argument_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_main(argv, moment=DECISION_AT)
    assert error.value.code == EXIT_CONFIG_ERROR


def test_cli_missing_packet_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main(
        [
            "--packet",
            str(tmp_path / "absent.json"),
            "--decision",
            "reject",
            "--reviewer",
            REVIEWER,
            "--json",
        ],
        moment=DECISION_AT,
    )
    captured = capsys.readouterr()
    assert code == EXIT_UNUSABLE
    assert captured.out == ""
    assert DecisionRecordCode.PACKET_NOT_FOUND.value in captured.err


# ---------------------------------------------------------------------------
# 源码守卫（本层**绝不**联网 / 写库 / 调 intake / 触碰 PROJECT_STATE / 采信 mtime）
# ---------------------------------------------------------------------------
def _module_source(module: Any) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_module_source_has_no_io_or_network_primitives() -> None:
    source = _module_source(record_module)
    for banned in (
        "import sqlalchemy",
        "import requests",
        "import aiohttp",
        "import httpx",
        "import subprocess",
        "import socket",
        "urlopen",
        "intake_evidence",
        "os.stat",
        "getmtime",
        "PROJECT_STATE.json",
    ):
        assert banned not in source, banned
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called  # 只通过显式 Path API 读写，绝不定点打开未知文件


def test_module_docstring_pins_the_audit_boundary() -> None:
    source = _module_source(record_module)
    assert "Phase transition executor" in source
    assert "data_qualification_passed" in source
    assert "phase_transition_executed" in source


def test_human_decision_enum_matches_the_allowed_actions() -> None:
    assert tuple(member.value for member in HumanDecision) == DECISION_RECORD_ACTIONS


def test_record_document_has_no_forbidden_evidence_time_keys(tmp_path: Path) -> None:
    for forbidden in FORBIDDEN_EVIDENCE_FIELDS:
        document = packet_document()
        document["gaps"]["author"]["checks"][0][forbidden] = "2026-05-01T00:00:00+00:00"
        packet_path = write_packet(tmp_path / f"forbidden_{forbidden}.json", document)
        with pytest.raises(DecisionRecordStateError) as error:
            load_decision_packet(packet_path, moment=DECISION_AT)
        assert DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION.value in str(error.value)

def test_recheck_missing_with_ready_flag_is_refused(tmp_path: Path) -> None:
    """``qualification_recheck_ready=true`` 却没有 recheck 事实 → fail-closed。"""
    document = packet_document(evidence_ready=True, recheck_ready=True)
    document["qualification_recheck"] = None
    document["submit_to_l3_human_gate"] = False
    document["status"] = "BLOCKED_PENDING_EVIDENCE"
    packet_path = write_packet(tmp_path / "no_recheck.json", document)
    with pytest.raises(DecisionRecordStateError) as error:
        load_decision_packet(packet_path, moment=DECISION_AT)
    assert DecisionRecordCode.PACKET_TAMPERED.value in str(error.value)
