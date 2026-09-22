"""GOLD-012 Evidence Inbox 人工复核决策与审计单元测试（临时目录 / 零数据库 / 零网络）。

覆盖：

- 退出码映射稳定；空 ledger / 只读运行零写入；
- ``approve`` 门禁：必须当前指纹一致 + ``PREFLIGHT_PASS`` + 非模板 / 示例 / Mock；
- ``reject`` / ``needs_changes`` 可记录（含对已隔离候选）；
- 受控词表：未知决策 / 原因码与决策不匹配 / 指纹非法 / 元数据含凭据 / 时区缺失：fail-closed；
- 幂等：同一指纹 + 完全相同决策重复提交不新增记录（ledger 字节级不变）；
- 冲突：差异决策必须显式 ``revision`` + ``override``（跳号 / 回退 / 缺 override 一律拒绝，
  历史全保留）；
- 内容变化 → 新指纹（旧批准不继承）；候选消失 / 预检回退 → 批准失效且**不静默放行**；
- ledger 严格校验（损坏 / 篡改 ``decision_id`` / revision 断链 / supersedes 断链 / 削弱安全字段）；
- 原子写（无残留 ``.tmp``）、锁冲突零写入、并发写不产生半写文档；
- 脱敏（reviewer / note 含凭据一律拒绝记录；URL 引用去 query）、安全字段恒为 blocker + human gate；
- 复核元数据**绝不**冒充 ``published_at`` / ``collected_at`` / ``effective_at`` /
  ``availability`` 证据；
- 源码守卫（无网络 / 无数据库 / 无 intake / 不删移原始 evidence / 无常驻循环）。
"""

from __future__ import annotations

import ast
import csv
import io
import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence import (
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_NO_DECISION,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    FORBIDDEN_EVIDENCE_FIELDS,
    INBOX_SCHEMA_VERSION,
    LEDGER_KIND,
    MANIFEST_FILE_NAME,
    PACKAGE_SUMMARY_KEYS,
    REASON_CODES_BY_DECISION,
    REVIEW_SCHEMA_VERSION,
    InboxStatus,
    LockConflictError,
    ReviewArgumentError,
    ReviewConflictError,
    ReviewDecision,
    ReviewLedger,
    ReviewLedgerStateError,
    ReviewPathError,
    ReviewReasonCode,
    ReviewTargetError,
    SingleInstanceLock,
    build_approved_intake_list,
    compute_decision_id,
    load_review_ledger,
    record_review_decision,
    render_review_summary,
    review_exit_code_for,
    run_review,
    scan_inbox,
)
from src.evidence import review as review_module
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=6)
#: 凭证类字符串（必须命中 ``redact_secrets``，用于脱敏断言）
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
REJECT_CODE = ReviewReasonCode.REJECTED_AUTHORIZATION_INSUFFICIENT.value
NEEDS_CODE = ReviewReasonCode.NEEDS_AUTHORIZATION_FIX.value


def author_row(record_id: str = "a-0001", **overrides: Any) -> dict[str, Any]:
    """一条**完全合规**的 Author 证据行（时间自洽、带时区、OOS 可用）。"""
    row: dict[str, Any] = {
        "source": "vendor-author",
        "source_record_id": record_id,
        "author_name": "张三",
        "external_account_id": "acct-0001",
        "content": "黄金短线看多",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-06-02T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]], *, fmt: str) -> None:
    """把若干行写成 ``jsonl`` 或 ``csv``（UTF-8；CSV 表头取自首行键集合）。"""
    if fmt == "jsonl":
        path.write_text(
            "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    path.write_text(buffer.getvalue(), encoding="utf-8")


def manifest_payload(
    evidence_path: Path, *, evidence_name: str = EVIDENCE_NAME, **overrides: Any
) -> dict[str, Any]:
    """构造最小合规 manifest（摘要取自**实际文件内容**）。"""
    payload: dict[str, Any] = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "evidence_type": "author",
        "source": "vendor-author",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": evidence_name,
                "sha256": hashing.sha256_bytes(evidence_path.read_bytes()),
                "format": "jsonl" if evidence_name.endswith(".jsonl") else "csv",
            }
        ],
    }
    payload.update(overrides)
    return payload


def build_package(
    root: Path,
    name: str = "pkg-author-01",
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    fmt: str = "jsonl",
    evidence_name: str = EVIDENCE_NAME,
    manifest: Mapping[str, Any] | str | None = None,
) -> Path:
    """在 ``root`` 下建一个候选包目录（默认合规；可用 ``manifest`` 覆盖 / 破坏）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    payload_rows = list(rows if rows is not None else [author_row()])
    evidence = package / evidence_name
    write_rows(evidence, payload_rows, fmt=fmt)
    if manifest is None:
        document = manifest_payload(evidence, evidence_name=evidence_name)
    elif isinstance(manifest, str):
        (package / MANIFEST_FILE_NAME).write_text(manifest, encoding="utf-8")
        return package
    else:
        document = dict(manifest)
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return package


def only_package(report: Any) -> Any:
    """取报告中唯一的候选包（测试断言用）。"""
    assert len(report.packages) == 1, report.to_dict()
    return report.packages[0]


def decide(
    report: Any,
    ledger: ReviewLedger | None,
    *,
    decision: str = ReviewDecision.APPROVE.value,
    reason_code: str = APPROVE_CODE,
    moment: datetime = MOMENT,
    **overrides: Any,
) -> Any:
    """按唯一候选包的指纹提交一次决策（测试辅助）。"""
    fingerprint = overrides.pop("fingerprint", only_package(report).fingerprint)
    reviewer = overrides.pop("reviewer", "operator-li")
    return record_review_decision(
        report,
        ledger,
        moment=moment,
        decision=decision,
        fingerprint=fingerprint,
        reviewer=reviewer,
        reason_code=reason_code,
        **overrides,
    )


# ---------------------------------------------------------------------------
# 退出码 / 决策 id / 词表
# ---------------------------------------------------------------------------
def test_exit_code_mapping_is_stable() -> None:
    assert review_exit_code_for(ReviewArgumentError("x")) == EXIT_CONFIG_ERROR
    assert review_exit_code_for(ReviewLedgerStateError("x")) == EXIT_STATE_INVALID
    assert review_exit_code_for(ReviewTargetError("x")) == EXIT_STATE_INVALID
    assert review_exit_code_for(ReviewConflictError("x")) == EXIT_STATE_INVALID
    assert review_exit_code_for(ReviewPathError("x")) == EXIT_UNUSABLE
    assert review_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert review_exit_code_for(RuntimeError("未知")) == EXIT_STATE_INVALID  # 未知 → fail-closed
    assert EXIT_OK == 0 and EXIT_CONFIG_ERROR == 2 and EXIT_NO_DECISION == 5


def test_decision_id_is_deterministic_and_revision_sensitive() -> None:
    first = compute_decision_id("a" * 64, ReviewDecision.APPROVE, 1, APPROVE_CODE)
    again = compute_decision_id("a" * 64, "APPROVE", 1, APPROVE_CODE)
    other_revision = compute_decision_id("a" * 64, ReviewDecision.APPROVE, 2, APPROVE_CODE)
    other_decision = compute_decision_id("a" * 64, ReviewDecision.REJECT, 1, REJECT_CODE)
    assert first == again and len(first) == 64
    assert first not in {other_revision, other_decision}


def test_reason_codes_are_scoped_per_decision(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    assert REJECT_CODE not in REASON_CODES_BY_DECISION[ReviewDecision.APPROVE]
    assert APPROVE_CODE not in REASON_CODES_BY_DECISION[ReviewDecision.REJECT]
    with pytest.raises(ReviewArgumentError):
        decide(report, None, reason_code=REJECT_CODE)


def test_unknown_decision_reason_and_fingerprint_are_rejected(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    with pytest.raises(ReviewArgumentError):
        decide(report, None, decision="MAYBE")
    with pytest.raises(ReviewArgumentError):
        decide(report, None, reason_code="NOT_A_REAL_CODE")
    for bad_fingerprint in ("", "not-a-fingerprint", "a" * 63, "a" * 65, "z" * 64):
        with pytest.raises(ReviewArgumentError):
            decide(report, None, fingerprint=bad_fingerprint)
    with pytest.raises(ReviewArgumentError):
        decide(report, None, reviewer="   ")
    with pytest.raises(ReviewArgumentError):
        decide(report, None, note="x" * 301)


def _inbox_with_package(root: Path) -> tuple[Path, Path]:
    """在 ``root`` 下建 inbox + 一个合规候选包，返回 (inbox, package_dir)。"""
    inbox = root / "inbox"
    package = build_package(inbox)
    return inbox, package


def test_empty_inbox_has_no_decision_and_no_approval(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    report = run_review(inbox, moment=MOMENT)
    assert report.has_decision is False
    assert report.approved_list.has_approved is False
    assert report.action == "LIST_ONLY"
    payload = report.to_dict()
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["counts"]["ledger_decisions"] == 0
    assert list(inbox.iterdir()) == []  # 只读运行绝不写 inbox


def test_missing_inbox_dir_and_naive_moment_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReviewPathError):
        run_review(tmp_path / "nope", moment=MOMENT)
    inbox, _package = _inbox_with_package(tmp_path)
    with pytest.raises(ReviewArgumentError):
        run_review(inbox, moment=datetime(2026, 9, 23, 0, 0))


def test_approve_records_decision_and_approved_list(tmp_path: Path) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    evidence = package_dir / EVIDENCE_NAME
    evidence_bytes = evidence.read_bytes()
    before = sorted(item.name for item in package_dir.iterdir())

    report = run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        note="人工核验授权与可用性证据",
        out_path=ledger_path,
        approved_out_path=approved_path,
    )
    assert report.action == "DECISION_RECORDED"
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert document["kind"] == LEDGER_KIND
    assert document["schema_version"] == REVIEW_SCHEMA_VERSION
    assert document["append_only"] is True
    assert document["decision_count"] == 1
    assert document["blocker_active"] is True
    assert document["human_gate_required"] is True
    assert document["data_qualification_passed"] is False
    assert document["phase_transition_allowed"] is False
    record = document["decisions"][0]
    assert record["decision"] == "APPROVE" and record["revision"] == 1
    assert record["supersedes"] is None and record["override"] is False
    assert record["approval_scope"] == "human_pre_review_only_not_data_qualification"
    assert set(record["package"]) == set(PACKAGE_SUMMARY_KEYS)
    approved = json.loads(approved_path.read_text(encoding="utf-8"))
    assert approved["approved_count"] == 1
    assert approved["approved"][0]["decision_id"] == record["decision_id"]
    assert approved["approved"][0]["requires_explicit_intake"] is True
    assert "evidence_operator workflow --no-dry-run" in approved["next_step"]
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []
    # 原始 evidence 从未被移动 / 改写，包内也没有新增文件
    assert evidence.read_bytes() == evidence_bytes
    assert sorted(item.name for item in package_dir.iterdir()) == before

# ---------------------------------------------------------------------------
# approve 门禁（fail-closed）
# ---------------------------------------------------------------------------
def test_approve_requires_preflight_pass(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    quarantine = build_package(
        inbox, name="pkg-pending", rows=[author_row(authorization_status="PENDING")]
    )
    report = scan_inbox(inbox, moment=MOMENT)
    target = next(item for item in report.packages if item.package_dir == quarantine.name)
    assert target.status is InboxStatus.QUARANTINED
    with pytest.raises(ReviewTargetError) as failure:
        record_review_decision(
            report,
            None,
            moment=MOMENT,
            decision="approve",
            fingerprint=target.fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
        )
    assert "QUARANTINED" in str(failure.value)
    listed = run_review(inbox, moment=MOMENT)
    assert listed.approved_list.has_approved is False
    assert listed.ledger.records == ()  # 门禁失败 → 零记录（也没有任何写入）


@pytest.mark.parametrize("marker", ["is_mock", "is_example", "synthetic", "record_kind"])
def test_approve_never_accepts_synthetic_or_template(tmp_path: Path, marker: str) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-marked"
    package_dir.mkdir(parents=True)
    row = author_row()
    row.update({marker: "example" if marker == "record_kind" else "true"})
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [row], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest[marker] = "example" if marker == "record_kind" else "true"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    report = scan_inbox(inbox, moment=MOMENT)
    package = only_package(report)
    assert package.synthetic is True
    with pytest.raises(ReviewTargetError) as failure:
        decide(report, None)
    assert "示例" in str(failure.value) or "Mock" in str(failure.value)


def test_approve_requires_current_fingerprint(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    with pytest.raises(ReviewTargetError) as failure:
        decide(report, None, fingerprint="b" * 64)
    assert "旧批准绝不继承" in str(failure.value)


def test_reject_and_needs_changes_are_recorded_even_when_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox, name="pkg-pending")
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row(authorization_status="PENDING")], fmt="jsonl")
    manifest = manifest_payload(evidence)
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    report = scan_inbox(inbox, moment=MOMENT)
    assert only_package(report).status is InboxStatus.QUARANTINED

    rejected = decide(
        report,
        None,
        decision="reject",
        reason_code=REJECT_CODE,
        note="授权引用无法核验",
    )
    assert rejected.record.decision == "REJECT" and rejected.created is True
    needs = decide(
        report,
        rejected.ledger,
        decision="needs_changes",
        reason_code=NEEDS_CODE,
        revision=2,
        override=True,
    )
    assert needs.record.decision == "NEEDS_CHANGES"
    assert needs.record.supersedes == rejected.record.decision_id
    approved = build_approved_intake_list(report, needs.ledger, moment=MOMENT)
    assert approved.approved == ()
    assert approved.to_dict()["decision_counts"] == {"NEEDS_CHANGES": 1}


def test_reviewer_and_note_must_not_contain_credentials(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    ledger_path = tmp_path / "review_ledger.json"
    with pytest.raises(ReviewArgumentError):
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=only_package(report).fingerprint,
            reviewer=f"Bearer {SECRET}",
            reason_code=APPROVE_CODE,
            out_path=ledger_path,
        )
    with pytest.raises(ReviewArgumentError):
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=only_package(report).fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            note=f"api_key={SECRET}",
            out_path=ledger_path,
        )
    assert not ledger_path.exists()  # 拒绝记录 = 零写入


def test_reviewed_at_must_be_aware_and_not_in_the_future(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    with pytest.raises(ReviewArgumentError):
        decide(report, None, reviewed_at=datetime(2026, 9, 22, 0, 0))
    with pytest.raises(ReviewArgumentError):
        decide(report, None, reviewed_at=LATER)
    decided = decide(report, None, reviewed_at=MOMENT - timedelta(hours=1))
    assert decided.record.reviewed_at == (MOMENT - timedelta(hours=1)).isoformat()
    with pytest.raises(ReviewArgumentError):
        record_review_decision(
            report,
            None,
            moment=datetime(2026, 9, 23, 0, 0),
            decision="approve",
            fingerprint=only_package(report).fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
        )


# ---------------------------------------------------------------------------
# 幂等 / 冲突 / 显式 revision
# ---------------------------------------------------------------------------
def test_repeat_identical_decision_is_idempotent_and_writes_byte_identical_ledger(
    tmp_path: Path,
) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "review_ledger.json"
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    argv = {
        "decision": "approve",
        "fingerprint": fingerprint,
        "reviewer": "operator-li",
        "reason_code": APPROVE_CODE,
        "note": "人工核验通过",
    }
    first = run_review(inbox, moment=MOMENT, out_path=ledger_path, **argv)
    assert first.action == "DECISION_RECORDED"
    original = ledger_path.read_bytes()
    first_seen = json.loads(original)["decisions"][0]["reviewed_at"]

    second = run_review(inbox, moment=LATER, out_path=ledger_path, ledger_path=ledger_path, **argv)
    assert second.action == "DECISION_IDEMPOTENT"
    assert second.idempotent is True
    assert second.ledger.records[0].reviewed_at == first_seen  # 历史原样保留
    assert ledger_path.read_bytes() == original  # 幂等：不重写历史
    assert second.to_dict()["counts"]["ledger_decisions"] == 1


def test_conflicting_decision_requires_explicit_revision_and_override(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    approved = decide(scan_inbox(inbox, moment=MOMENT), None)
    later_report = scan_inbox(inbox, moment=LATER)  # 内容未变：指纹一致

    with pytest.raises(ReviewConflictError) as missing_revision:
        decide(
            later_report,
            approved.ledger,
            decision="reject",
            reason_code=REJECT_CODE,
            moment=LATER,
        )
    assert "绝不静默覆盖" in str(missing_revision.value)

    with pytest.raises(ReviewConflictError):
        decide(
            later_report,
            approved.ledger,
            decision="reject",
            reason_code=REJECT_CODE,
            moment=LATER,
            revision=2,
        )

    with pytest.raises(ReviewConflictError):
        decide(
            later_report,
            approved.ledger,
            decision="reject",
            reason_code=REJECT_CODE,
            moment=LATER,
            revision=3,
            override=True,
        )
    assert len(approved.ledger.records) == 1  # 冲突不写入任何历史


def test_first_decision_rejects_bad_revision_and_override(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    with pytest.raises(ReviewArgumentError):
        decide(report, None, decision="reject", reason_code=REJECT_CODE, revision=5)
    with pytest.raises(ReviewArgumentError):
        decide(report, None, decision="reject", reason_code=REJECT_CODE, override=True)


def test_override_appends_new_revision_and_keeps_full_history(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "review_ledger.json"
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    first = run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    second = run_review(
        inbox,
        moment=LATER,
        decision="reject",
        fingerprint=fingerprint,
        reviewer="operator-zhang",
        reason_code=REJECT_CODE,
        note="复核发现授权引用与授权范围不符",
        revision=2,
        override=True,
        ledger_path=ledger_path,
        out_path=ledger_path,
    )
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert document["decision_count"] == 2
    history = document["decisions"]
    assert [item["revision"] for item in history] == [1, 2]
    assert [item["decision"] for item in history] == ["APPROVE", "REJECT"]
    assert history[1]["supersedes"] == history[0]["decision_id"]
    assert history[1]["override"] is True
    assert first.ledger.records[0].decision_id == history[0]["decision_id"]
    reloaded = load_review_ledger(ledger_path)
    assert reloaded is not None
    assert reloaded.records_for(fingerprint) == reloaded.records
    latest = reloaded.latest_for(fingerprint)
    assert latest is not None and latest.decision == "REJECT"
    # 推翻后不再有仍成立的批准（绝不因为"曾经批准过"就放行）
    assert second.approved_list.has_approved is False
    assert second.to_dict()["counts"]["invalidated_approvals"] == 0


def test_same_decision_with_different_reason_is_not_silently_overwritten(
    tmp_path: Path,
) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    report = scan_inbox(inbox, moment=MOMENT)
    approved = decide(report, None)
    with pytest.raises(ReviewConflictError):
        decide(
            report,
            approved.ledger,
            reason_code=ReviewReasonCode.APPROVED_AUTHORIZATION_REVIEWED.value,
        )
    assert len(approved.ledger.records) == 1  # 历史未被改写


# ---------------------------------------------------------------------------
# ledger 写盘 / 损坏 / 锁 / 只读
# ---------------------------------------------------------------------------
def test_ledger_is_written_atomically_and_is_self_describing(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "state" / "review_ledger.json"
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    report = run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    assert ledger_path.exists()
    assert report.written_ledger is not None
    assert report.to_dict()["written_ledger"].endswith("review_ledger.json")
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert document["kind"] == LEDGER_KIND
    assert document["generated_at"] == MOMENT.isoformat()
    assert report.decision is not None
    assert document["decisions"] == [report.decision.to_dict()]
    assert [item.name for item in ledger_path.parent.iterdir() if item.name.endswith(".tmp")] == []
    assert (ledger_path.parent / (ledger_path.name + ".lock")).exists()  # 锁文件留痕（不删除）


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update({"kind": "something-else"}),
        lambda document: document.update({"schema_version": 99}),
        lambda document: document.update({"contract_version": "evidence-intake-v0"}),
        lambda document: document.update({"blocker_active": False}),
        lambda document: document.update({"human_gate_required": False}),
        lambda document: document.update({"data_qualification_passed": True}),
        lambda document: document.update({"phase_transition_allowed": True}),
        lambda document: document.update({"decisions": "not-a-list"}),
        lambda document: document.update({"decisions": [{"fingerprint": "bad"}]}),
        lambda document: document.pop("generated_at", None),
    ],
)
def test_tampered_ledger_is_fail_closed(tmp_path: Path, mutate: Any) -> None:
    ledger_path = _written_ledger(tmp_path)
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    mutate(document)
    ledger_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReviewLedgerStateError):
        load_review_ledger(ledger_path)


@pytest.mark.parametrize("corrupt", ["{ not json", "[1, 2, 3]", ""])
def test_unreadable_ledger_is_fail_closed(tmp_path: Path, corrupt: str) -> None:
    path = tmp_path / "review_ledger.json"
    path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(ReviewLedgerStateError):
        load_review_ledger(path)
    assert path.read_text(encoding="utf-8") == corrupt  # 旧文件原样保留


def test_missing_ledger_is_first_run(tmp_path: Path) -> None:
    assert load_review_ledger(tmp_path / "absent.json") is None


def _written_ledger(tmp_path: Path, *, name: str = "review_ledger.json") -> Path:
    """建一个 inbox + 合规候选包，写入一条 approve 决策，返回 ledger 路径。"""
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger_path = tmp_path / name
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    return ledger_path




def test_tampered_decision_id_and_broken_history_are_detected(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "review_ledger.json"
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    run_review(
        inbox,
        moment=LATER,
        decision="reject",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=REJECT_CODE,
        revision=2,
        override=True,
        ledger_path=ledger_path,
        out_path=ledger_path,
    )
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert document["decision_count"] == 2
    loaded = ReviewLedger.from_dict(document)
    assert loaded.records[1].supersedes == loaded.records[0].decision_id

    # ① 篡改 decision_id（与内容派生值不一致）
    tampered = json.loads(json.dumps(document))
    tampered["decisions"][1]["decision_id"] = "f" * 64
    with pytest.raises(ReviewLedgerStateError):
        ReviewLedger.from_dict(tampered)

    # ② revision 断链（跳号）
    broken_revision = json.loads(json.dumps(document))
    broken_revision["decisions"][1]["revision"] = 5
    broken_revision["decisions"][1]["decision_id"] = compute_decision_id(
        fingerprint, "REJECT", 5, REJECT_CODE
    )
    with pytest.raises(ReviewLedgerStateError):
        ReviewLedger.from_dict(broken_revision)

    # ③ supersedes 链断裂
    broken_chain = json.loads(json.dumps(document))
    broken_chain["decisions"][1]["supersedes"] = None
    broken_chain["decisions"][1]["override"] = False
    with pytest.raises(ReviewLedgerStateError):
        ReviewLedger.from_dict(broken_chain)

    # ④ 原因码与决策不匹配
    broken_reason = json.loads(json.dumps(document))
    broken_reason["decisions"][0]["reason_code"] = REJECT_CODE
    with pytest.raises(ReviewLedgerStateError):
        ReviewLedger.from_dict(broken_reason)

    # ⑤ 无时区的 reviewed_at
    naive_time = json.loads(json.dumps(document))
    naive_time["decisions"][0]["reviewed_at"] = "2026-09-23T00:00:00"
    with pytest.raises(ReviewLedgerStateError):
        ReviewLedger.from_dict(naive_time)


def test_corrupt_ledger_fails_closed_without_writing(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path = tmp_path / "review_ledger.json"
    out_path = tmp_path / "out_ledger.json"
    ledger_path.write_text("{ not json", encoding="utf-8")
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    with pytest.raises(ReviewLedgerStateError):
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            ledger_path=ledger_path,
            out_path=out_path,
        )
    assert ledger_path.read_text(encoding="utf-8") == "{ not json"  # 旧 ledger 原样保留
    assert not out_path.exists()  # 零写入


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    out_path = tmp_path / "review_ledger.json"
    lock_path = out_path.with_name(out_path.name + ".lock")
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    with SingleInstanceLock(lock_path, owner="holder") as info:
        assert info.owner == "holder"
        with pytest.raises(LockConflictError):
            run_review(
                inbox,
                moment=MOMENT,
                decision="approve",
                fingerprint=fingerprint,
                reviewer="operator-li",
                reason_code=APPROVE_CODE,
                out_path=out_path,
            )
        assert not out_path.exists()  # 零写入
        assert lock_path.exists()  # 活动锁不被删除 / 改写


def test_read_only_run_never_writes_anything(tmp_path: Path) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    evidence = package_dir / EVIDENCE_NAME
    evidence_bytes = evidence.read_bytes()
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    report = run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
    )
    assert report.written_ledger is None and report.written_approved is None
    assert report.has_decision is True  # 决策只在内存中（dry-run）
    assert list(inbox.iterdir()) == [package_dir]
    assert sorted(item.name for item in package_dir.iterdir()) == [
        EVIDENCE_NAME,
        MANIFEST_FILE_NAME,
    ]
    assert evidence.read_bytes() == evidence_bytes


def test_output_inside_inbox_is_rejected_before_writing(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    with pytest.raises(ReviewPathError):
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            out_path=inbox / "review_ledger.json",
        )
    with pytest.raises(ReviewPathError):
        run_review(inbox, moment=MOMENT, approved_out_path=inbox / "approved.json")
    assert not (inbox / "review_ledger.json").exists()
    assert not (inbox / "review_ledger.json.lock").exists()
    assert sorted(item.name for item in inbox.iterdir()) == ["pkg-author-01"]

# ---------------------------------------------------------------------------
# 批准失效（内容变化 / 候选消失 / 预检回退）
# ---------------------------------------------------------------------------
def _approve_package(tmp_path: Path, inbox: Path, fingerprint: str) -> Path:
    """对给定指纹写一条 approve 决策，返回 ledger 路径。"""
    ledger_path = tmp_path / "review_ledger.json"
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    return ledger_path


def test_content_change_creates_new_fingerprint_and_never_inherits_approval(
    tmp_path: Path,
) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    old_fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    ledger_path = _approve_package(tmp_path, inbox, old_fingerprint)
    first = run_review(inbox, moment=MOMENT, ledger_path=ledger_path)
    assert [item.fingerprint for item in first.approved_list.approved] == [old_fingerprint]

    # 内容变化 + manifest 摘要同步更新
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row("a-0001"), author_row("a-0002")], fmt="jsonl")
    manifest_path = package_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(evidence.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # ① 对**旧**指纹继续 approve → fail-closed（旧批准绝不继承到新指纹）
    with pytest.raises(ReviewTargetError):
        run_review(
            inbox,
            moment=LATER,
            decision="approve",
            fingerprint=old_fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            ledger_path=ledger_path,
        )

    # ② 只读列出：旧批准**失效**，新指纹未批准
    listed = run_review(inbox, moment=LATER, ledger_path=ledger_path)
    assert listed.approved_list.has_approved is False
    invalidated = listed.approved_list.invalidated
    assert [item.fingerprint for item in invalidated] == [old_fingerprint]
    assert invalidated[0].reason_code == "CANDIDATE_MISSING"
    assert listed.ledger.records[0].fingerprint == old_fingerprint  # 历史仍保留


def test_candidate_disappearance_invalidates_approval(tmp_path: Path) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    ledger_path = _approve_package(tmp_path, inbox, fingerprint)

    for item in package_dir.iterdir():
        item.unlink()
    package_dir.rmdir()

    listed = run_review(inbox, moment=LATER, ledger_path=ledger_path)
    assert listed.approved_list.has_approved is False
    assert [item.reason_code for item in listed.approved_list.invalidated] == [
        "CANDIDATE_MISSING"
    ]


def test_preflight_regression_invalidates_approval(tmp_path: Path) -> None:
    """同一内容在不同审计时点可能"过期"：批准必须按**当前**证据重新验证。"""
    inbox = tmp_path / "inbox"
    early = datetime(2026, 6, 3, tzinfo=UTC)
    build_package(
        inbox,
        rows=[author_row("late-review", authorization_reviewed_at="2026-06-05T00:00:00+00:00")],
    )
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    assert only_package(scan_inbox(inbox, moment=MOMENT)).status is InboxStatus.PREFLIGHT_PASS
    assert only_package(scan_inbox(inbox, moment=early)).status is InboxStatus.QUARANTINED
    ledger_path = _approve_package(tmp_path, inbox, fingerprint)

    listed = run_review(inbox, moment=early, ledger_path=ledger_path)
    assert listed.approved_list.has_approved is False
    assert [item.reason_code for item in listed.approved_list.invalidated] == [
        "PREFLIGHT_NOT_PASSING"
    ]
    # 指纹未变（内容未变）→ 说明失效来自**当前**证据状态，而不是"新指纹"
    assert listed.ledger.records[0].fingerprint == fingerprint


def test_approved_list_only_contains_current_approvals(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    build_package(inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    report = scan_inbox(inbox, moment=MOMENT)
    ledger_path = tmp_path / "review_ledger.json"
    by_dir = {item.package_dir: item.fingerprint for item in report.packages}

    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=by_dir["pkg-author-01"],
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
    )
    run_review(
        inbox,
        moment=MOMENT,
        decision="reject",
        fingerprint=by_dir["pkg-author-02"],
        reviewer="operator-li",
        reason_code=REJECT_CODE,
        ledger_path=ledger_path,
        out_path=ledger_path,
    )
    final = run_review(inbox, moment=MOMENT, ledger_path=ledger_path)
    assert [item.fingerprint for item in final.approved_list.approved] == [
        by_dir["pkg-author-01"]
    ]
    payload = final.approved_list.to_dict()
    assert payload["decision_counts"] == {"APPROVE": 1, "REJECT": 1}
    assert payload["approved_count"] == 1
    assert payload["approval_scope"] == "human_pre_review_only_not_data_qualification"


# ---------------------------------------------------------------------------
# 脱敏 / 安全字段 / 复核元数据不得冒充证据
# ---------------------------------------------------------------------------
def _approve_with_reference(tmp_path: Path) -> tuple[Any, Path, Path]:
    """建一个合规候选包（引用带查询串）并 approve，返回 (report, ledger, approved)。"""
    inbox = tmp_path / "inbox"
    package_dir = inbox / "pkg-author-01"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["authorization_reference"] = "https://vendor.example/terms?v=2&operator=li"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved.json"
    report = run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        note="人工核验授权与可用性证据",
        out_path=ledger_path,
        approved_out_path=approved_path,
    )
    return report, ledger_path, approved_path


def test_review_artifacts_are_redacted_and_keep_reference_without_query(
    tmp_path: Path,
) -> None:
    report, ledger_path, approved_path = _approve_with_reference(tmp_path)
    blob = (
        json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        + render_review_summary(report)
        + ledger_path.read_text(encoding="utf-8")
        + approved_path.read_text(encoding="utf-8")
    )
    assert SECRET not in blob
    assert "authorization_reference" in blob
    assert "https://vendor.example/terms" in blob  # 引用保留（可核验）
    assert "?v=2" not in blob  # 查询串被剥离
    assert report.ledger.records[0].package["authorization_reference"] == (
        "https://vendor.example/terms"
    )


def test_review_artifacts_never_carry_evidence_time_fields(tmp_path: Path) -> None:
    report, ledger_path, approved_path = _approve_with_reference(tmp_path)
    ledger_document = json.loads(ledger_path.read_text(encoding="utf-8"))
    approved_document = json.loads(approved_path.read_text(encoding="utf-8"))
    for document in (ledger_document, approved_document, report.to_dict()):
        assert not _has_key(document, FORBIDDEN_EVIDENCE_FIELDS), document
    blob = (
        json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        + render_review_summary(report)
        + ledger_path.read_text(encoding="utf-8")
        + approved_path.read_text(encoding="utf-8")
    )
    # 证据行里的时间值**不得**被复核层复制（复核时间不是证据时间）
    assert "2026-06-01T00:00:00+00:00" not in blob
    assert "2026-06-01T00:30:00+00:00" not in blob
    assert report.ledger.records[0].reviewed_at == MOMENT.isoformat()


def _has_key(node: Any, keys: Sequence[str]) -> bool:
    """递归检查 JSON 结构中是否出现给定键（测试辅助）。"""
    if isinstance(node, Mapping):
        return any(str(key) in keys or _has_key(value, keys) for key, value in node.items())
    if isinstance(node, (list, tuple)):
        return any(_has_key(item, keys) for item in node)
    return False


def test_approvals_never_change_blocker_or_human_gate_fields(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    for index in range(3):
        build_package(inbox, name=f"pkg-{index}", rows=[author_row(f"a-{index}")])
    report = scan_inbox(inbox, moment=MOMENT)
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved.json"
    for package in report.packages:
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=package.fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            ledger_path=ledger_path,
            out_path=ledger_path,
            approved_out_path=approved_path,
        )
    ledger_document = json.loads(ledger_path.read_text(encoding="utf-8"))
    approved_document = json.loads(approved_path.read_text(encoding="utf-8"))
    assert approved_document["approved_count"] == 3
    for document in (ledger_document, approved_document):
        assert document["blocker_code"] == PHASE3_3_BLOCKER_CODE
        assert document["blocker_active"] is True
        assert document["human_gate_required"] is True
        assert document["data_qualification_passed"] is False
        assert document["phase_transition_allowed"] is False
    assert approved_document["approval_scope"] == "human_pre_review_only_not_data_qualification"


def test_render_summary_keeps_blocker_and_human_gate_visible(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    build_package(inbox, name="pkg-pending", rows=[author_row(authorization_status="PENDING")])
    report = scan_inbox(inbox, moment=MOMENT)
    by_dir = {item.package_dir: item.fingerprint for item in report.packages}
    approved = record_review_decision(
        report,
        None,
        moment=MOMENT,
        decision="approve",
        fingerprint=by_dir["pkg-author-01"],
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
    )
    rejected = record_review_decision(
        report,
        approved.ledger,
        moment=MOMENT,
        decision="reject",
        fingerprint=by_dir["pkg-pending"],
        reviewer="operator-li",
        reason_code=REJECT_CODE,
    )
    summary = render_review_summary(
        review_module.ReviewReport(
            generated_at=MOMENT,
            inbox_dir=inbox,
            ledger=rejected.ledger,
            decision=rejected.record,
            approved_list=build_approved_intake_list(report, rejected.ledger, moment=MOMENT),
        )
    )
    assert PHASE3_3_BLOCKER_CODE in summary
    assert "human_gate_required=true" in summary
    assert "data_qualification_passed=false" in summary
    assert "不自动 intake" in summary
    assert "approved-for-explicit-intake" in summary
    assert "evidence_operator workflow --no-dry-run" in summary
    assert "pkg-author-01" in summary and "pkg-pending" in summary


# ---------------------------------------------------------------------------
# 并发 / 原子写 / 源码守卫
# ---------------------------------------------------------------------------
def test_concurrent_ledger_writes_never_produce_partial_or_rewritten_history(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    for index in range(4):
        build_package(inbox, name=f"pkg-{index}", rows=[author_row(f"a-{index}")])
    report = scan_inbox(inbox, moment=MOMENT)
    fingerprints = [item.fingerprint for item in report.packages]
    assert len(set(fingerprints)) == 4
    ledger_path = tmp_path / "review_ledger.json"
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(fingerprint: str) -> None:
        try:
            run_review(
                inbox,
                moment=MOMENT,
                decision="approve",
                fingerprint=fingerprint,
                reviewer="operator-li",
                reason_code=APPROVE_CODE,
                out_path=ledger_path,
            )
            outcome = "ok"
        except LockConflictError:
            outcome = "locked"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(item,)) for item in fingerprints]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(outcomes) <= {"ok", "locked"}
    assert "ok" in outcomes  # 先拿到锁的线程必然成功
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger = ReviewLedger.from_dict(document)  # 半写文档会在这里 fail-closed
    assert ledger.records, document
    for fingerprint in {record.fingerprint for record in ledger.records}:
        revisions = [item.revision for item in ledger.records_for(fingerprint)]
        assert revisions == list(range(1, len(revisions) + 1))
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


def test_module_has_no_network_database_or_intake_behaviour() -> None:
    source = Path(review_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    for banned in (
        "aiohttp",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "sqlalchemy",
        "subprocess",
    ):
        assert all(banned not in module for module in modules), f"不得引入 {banned}：{modules}"
    # 绝不删除 / 移动 / 改名原始 evidence；绝不调用任何 intake / commit 路径
    for banned_call in ("unlink", "rmtree", "os.remove", "os.rename", "shutil.move"):
        assert banned_call not in source
    assert "intake_evidence" not in source
    assert "atomic_write_text" in source  # 唯一写路径 = 既有原子写原语
    # 无网络 / 无数据库 / 无常驻循环
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    # 复核元数据不得冒充证据时间
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert f'"{field_name}":' not in source


def test_cli_module_defers_to_core_module(tmp_path: Path) -> None:
    """CLI 只做参数 / 输出编排（真实子进程端到端在集成测试里覆盖）。"""
    from scripts.evidence_review import ALL_REASON_CODES, DECISION_CHOICES, build_parser

    assert set(DECISION_CHOICES) == {"approve", "reject", "needs_changes"}
    assert APPROVE_CODE in ALL_REASON_CODES and REJECT_CODE in ALL_REASON_CODES
    parser = build_parser()
    with pytest.raises(SystemExit) as missing_inbox_dir:
        parser.parse_args(["--json"])
    assert missing_inbox_dir.value.code == 2

