"""GOLD-013 Intake Plan 单元测试（临时目录 / Mock 证据包 / 零数据库 / 零网络）。

覆盖：

- 退出码映射稳定；缺输入 / 时区缺失 / 缺 inbox 目录：参数或路径错误；
- 计划绑定：必须在**当前** inbox + review ledger 上重新核验批准清单；
- fail-closed：stale 指纹、候选消失、preflight 回退、review override、approved-list tamper、
  合成候选、损坏 / 被削弱的批准清单与 ledger；
- 幂等 / 内容级 ``plan_id``：同一输入重复生成幂等，输入或 review revision 变化产生新计划；
- 默认只读：不写数据库、不写 inbox、不自动 intake、不改动原始 evidence；
- 显式衔接：handoff 必带 ``--no-dry-run`` 与显式 ``--input``，且本工具绝不执行；
- 安全字段与"计划 ≠ 资格"：四个安全字段恒为 blocker/human gate、``data_qualification_passed``
  恒为 false、``data_qualification_passed_count`` 恒为 0；
- 脱敏与"复核 / 计划时间不是证据"；原子写与并发；源码守卫。
"""

from __future__ import annotations

import ast
import csv
import io
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence import (
    APPROVAL_SCOPE,
    APPROVED_KIND,
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    FORBIDDEN_EVIDENCE_FIELDS,
    INBOX_SCHEMA_VERSION,
    INTAKE_PLAN_KIND,
    INTAKE_PLAN_SCHEMA_VERSION,
    LEDGER_KIND,
    MANIFEST_FILE_NAME,
    OPERATOR_EXPLICIT_FLAG,
    PLAN_EXECUTION_MODE,
    REVIEW_SCHEMA_VERSION,
    IntakePlanArgumentError,
    IntakePlanInconsistentError,
    IntakePlanPathError,
    IntakePlanStateError,
    IntakePlanStatus,
    IntakePlanWriteError,
    LockConflictError,
    PlanVerificationCode,
    ReviewReasonCode,
    compute_decision_id,
    intake_plan_exit_code_for,
    render_intake_plan_summary,
    run_intake_plan,
    run_review,
    scan_inbox,
)
from src.evidence import intake_plan as intake_plan_module
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=6)
#: 凭证类字符串（必须被脱敏，绝不进入任何 artifact）
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
REJECT_CODE = ReviewReasonCode.REJECTED_AUTHORIZATION_INSUFFICIENT.value


def author_row(record_id: str = "a-0001", **overrides: Any) -> dict[str, Any]:
    """一条**完全合规**的 Author 证据行（时间自洽、带时区、OOS 可用；与 GOLD-012 同口径）。"""
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
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    """在 ``root`` 下建一个候选包目录（默认合规；可用 ``manifest`` 覆盖）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    payload_rows = list(rows if rows is not None else [author_row()])
    evidence = package / evidence_name
    write_rows(evidence, payload_rows, fmt=fmt)
    document = (
        manifest_payload(evidence, evidence_name=evidence_name)
        if manifest is None
        else dict(manifest)
    )
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return package


def only_package(report: Any) -> Any:
    """取扫描报告中唯一的候选包（测试断言用）。"""
    assert len(report.packages) == 1, report.to_dict()
    return report.packages[0]


def _inbox_with_package(root: Path) -> tuple[Path, Path]:
    """在 ``root`` 下建 inbox + 一个合规候选包，返回 (inbox, package_dir)。"""
    inbox = root / "inbox"
    package = build_package(inbox)
    return inbox, package



def approve_package(
    tmp_path: Path,
    inbox: Path,
    *,
    package_dir_name: str = "pkg-author-01",
    moment: datetime = MOMENT,
    ledger_name: str = "review_ledger.json",
    approved_name: str = "approved_for_intake.json",
) -> tuple[Path, Path]:
    """按 GOLD-012 口径对指定候选包 approve，并落盘 ledger + 批准清单。"""
    report = scan_inbox(inbox, moment=moment)
    target = next(item for item in report.packages if item.package_dir == package_dir_name)
    ledger_path = tmp_path / ledger_name
    approved_path = tmp_path / approved_name
    run_review(
        inbox,
        moment=moment,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
        approved_out_path=approved_path,
    )
    return ledger_path, approved_path


def build_plan(
    inbox: Path,
    ledger_path: Path,
    approved_path: Path,
    *,
    moment: datetime = MOMENT,
    out_path: Path | None = None,
) -> Any:
    """生成一次 intake plan（默认只读；给出 ``out_path`` 才写计划文件）。"""
    return run_intake_plan(
        inbox,
        moment=moment,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
        out_path=out_path,
    )


def crafted_record(
    fingerprint: str,
    *,
    decision: str = "APPROVE",
    revision: int = 1,
    reason_code: str = APPROVE_CODE,
    package: Mapping[str, Any] | None = None,
    supersedes: str | None = None,
    override: bool = False,
) -> dict[str, Any]:
    """手工构造一条**格式合法**的 ledger 决策（用于验证 GOLD-013 的门禁本身）。"""
    return {
        "decision_id": compute_decision_id(fingerprint, decision, revision, reason_code),
        "fingerprint": fingerprint,
        "decision": decision,
        "revision": revision,
        "supersedes": supersedes,
        "override": override,
        "reviewer": "operator-li",
        "reviewed_at": MOMENT.isoformat(),
        "reason_code": reason_code,
        "note": "",
        "approval_scope": APPROVAL_SCOPE,
        "package": dict(package or {}),
    }


def crafted_ledger(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """手工构造 review ledger 文档（供"曾被批准"这类历史状态复现）。"""
    return {
        "kind": LEDGER_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "generated_at": MOMENT.isoformat(),
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "append_only": True,
        "approval_scope": APPROVAL_SCOPE,
        "decision_count": len(records),
        "decisions": [dict(item) for item in records],
        "notes": [],
    }


def crafted_entry(fingerprint: str, decision_id: str, **overrides: Any) -> dict[str, Any]:
    """手工构造一条批准清单条目。"""
    entry: dict[str, Any] = {
        "fingerprint": fingerprint,
        "decision_id": decision_id,
        "approved_at": MOMENT.isoformat(),
        "reviewer": "operator-li",
        "reason_code": APPROVE_CODE,
        "package_dir": "pkg-author-01",
        "evidence_type": "author",
        "source": "vendor-author",
        "evidence_files": [EVIDENCE_NAME],
        "rows": 1,
        "acceptable_rows": 1,
        "approval_scope": APPROVAL_SCOPE,
        "requires_explicit_intake": True,
    }
    entry.update(overrides)
    return entry


def crafted_approved_list(
    entries: Sequence[Mapping[str, Any]],
    *,
    invalidated: Sequence[Mapping[str, Any]] = (),
    decision_counts: Mapping[str, int] | None = None,
    generated_at: datetime = MOMENT,
) -> dict[str, Any]:
    """手工构造批准清单文档（结构自洽；供 tamper 测试用）。"""
    counts = dict(decision_counts) if decision_counts is not None else {"APPROVE": len(entries)}
    return {
        "kind": APPROVED_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "generated_at": generated_at.isoformat(),
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "approval_scope": APPROVAL_SCOPE,
        "approved_count": len(entries),
        "invalidated_count": len(invalidated),
        "approved": [dict(item) for item in entries],
        "invalidated": [dict(item) for item in invalidated],
        "decision_counts": counts,
        "next_step": "",
        "notes": [],
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """把字典写成 JSON 文件（UTF-8）并返回路径。"""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def tamper(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """就地篡改一个 JSON 文档（模拟 approved-list / ledger 被改写）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json(path, payload)


def codes_of(failure: pytest.ExceptionInfo[IntakePlanInconsistentError]) -> list[str]:
    """取失败里的稳定原因码（排序；用于断言 fail-closed 的具体口径）。"""
    return sorted(item.code for item in failure.value.violations)



# ---------------------------------------------------------------------------
# 退出码 / 参数
# ---------------------------------------------------------------------------
def test_exit_code_mapping_is_stable() -> None:
    assert intake_plan_exit_code_for(IntakePlanArgumentError("x")) == EXIT_CONFIG_ERROR
    assert intake_plan_exit_code_for(IntakePlanStateError("x")) == EXIT_STATE_INVALID
    assert intake_plan_exit_code_for(IntakePlanInconsistentError([])) == EXIT_STATE_INVALID
    assert intake_plan_exit_code_for(IntakePlanPathError("x")) == EXIT_UNUSABLE
    assert intake_plan_exit_code_for(IntakePlanWriteError("x")) == EXIT_UNUSABLE
    assert intake_plan_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert intake_plan_exit_code_for(RuntimeError("未知")) == EXIT_STATE_INVALID  # fail-closed
    assert EXIT_OK == 0 and EXIT_CONFIG_ERROR == 2 and EXIT_BLOCKED == 5


def test_missing_inputs_and_naive_moment_are_rejected(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    with pytest.raises(IntakePlanArgumentError):
        run_intake_plan(
            inbox,
            moment=datetime(2026, 9, 23, 0, 0),
            ledger_path=ledger_path,
            approved_list_path=approved_path,
        )
    with pytest.raises(IntakePlanArgumentError):
        run_intake_plan(inbox, moment=MOMENT, ledger_path=ledger_path)
    with pytest.raises(IntakePlanArgumentError):
        run_intake_plan(inbox, moment=MOMENT, approved_list_path=approved_path)
    with pytest.raises(IntakePlanPathError):
        run_intake_plan(
            tmp_path / "nope",
            moment=MOMENT,
            ledger_path=ledger_path,
            approved_list_path=approved_path,
        )


# ---------------------------------------------------------------------------
# 正常路径：批准清单必须与**当前** inbox + ledger 一致
# ---------------------------------------------------------------------------
def test_ready_plan_binds_approved_list_to_current_inbox(tmp_path: Path) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    evidence = package_dir / EVIDENCE_NAME
    evidence_bytes = evidence.read_bytes()
    package_files = sorted(item.name for item in package_dir.iterdir())

    plan = build_plan(inbox, ledger_path, approved_path)

    assert plan.status is IntakePlanStatus.READY_FOR_EXPLICIT_INTAKE
    assert plan.has_approved is True
    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.revision == 1 and entry.scope == "author"
    assert len(entry.fingerprint) == 64 and len(plan.plan_id) == 64
    payload = plan.to_dict()
    assert payload["kind"] == INTAKE_PLAN_KIND
    assert payload["schema_version"] == INTAKE_PLAN_SCHEMA_VERSION
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["execution_mode"] == PLAN_EXECUTION_MODE
    assert payload["approved_for_explicit_intake_count"] == 1
    assert payload["data_qualification_passed_count"] == 0
    assert payload["verification"]["violations"] == []
    assert payload["verification"]["preflight_pass"] == 1
    assert plan.written_path is None  # 默认只读：不写任何文件
    # 原始 evidence 从未被移动 / 改写，包内也没有新增文件
    assert evidence.read_bytes() == evidence_bytes
    assert sorted(item.name for item in package_dir.iterdir()) == package_files
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []



def test_blocked_plan_when_nothing_is_approved(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    run_review(inbox, moment=MOMENT, out_path=ledger_path, approved_out_path=approved_path)

    plan = build_plan(inbox, ledger_path, approved_path)

    assert plan.status is IntakePlanStatus.BLOCKED_NO_APPROVED_EVIDENCE
    assert plan.has_approved is False
    assert plan.entries == () and plan.handoff == ()
    payload = plan.to_dict()
    assert payload["approved_for_explicit_intake"] == []
    assert payload["data_qualification_passed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["blocker_active"] is True and payload["human_gate_required"] is True
    assert payload["auto_intake_allowed"] is False and payload["writes_database"] is False


def test_missing_ledger_is_empty_history_not_corruption(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    approved_path = write_json(
        tmp_path / "approved.json", crafted_approved_list([], decision_counts={})
    )

    plan = build_plan(inbox, tmp_path / "absent_ledger.json", approved_path)
    assert plan.status is IntakePlanStatus.BLOCKED_NO_APPROVED_EVIDENCE

    # 但"没有 ledger"绝不能放行任何批准
    fingerprint = "a" * 64
    stale = write_json(
        tmp_path / "stale_approved.json",
        crafted_approved_list([crafted_entry(fingerprint, "b" * 64)]),
    )
    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, tmp_path / "absent_ledger.json", stale)
    assert PlanVerificationCode.LEDGER_MISSING_APPROVAL.value in codes_of(failure)


# ---------------------------------------------------------------------------
# 内容级 plan_id / 幂等
# ---------------------------------------------------------------------------
def test_plan_id_is_content_level_and_independent_of_audit_time(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)

    first = build_plan(inbox, ledger_path, approved_path)
    later = build_plan(inbox, ledger_path, approved_path, moment=LATER)

    assert first.plan_id == later.plan_id  # 内容级：与 generated_at 无关
    assert first.to_dict()["generated_at"] != later.to_dict()["generated_at"]


def test_new_plan_when_ledger_decisions_change(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    build_package(inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    first = build_plan(inbox, ledger_path, approved_path)

    report = scan_inbox(inbox, moment=MOMENT)
    second = next(item for item in report.packages if item.package_dir == "pkg-author-02")
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=second.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        ledger_path=ledger_path,
        out_path=ledger_path,
        approved_out_path=approved_path,
    )

    updated = build_plan(inbox, ledger_path, approved_path)
    assert updated.plan_id != first.plan_id  # 输入（ledger 决策集）变化 → 新计划
    assert len(updated.entries) == 2

    # 旧清单（只含一条批准）不再成立 → fail-closed（旧批准不继承）
    stale_entry = crafted_entry(
        first.entries[0].fingerprint, first.entries[0].decision_id
    )
    stale = write_json(
        tmp_path / "old_approved.json", crafted_approved_list([stale_entry])
    )
    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, stale)
    assert PlanVerificationCode.APPROVED_LIST_STALE.value in codes_of(failure)


def test_repeated_identical_plan_is_byte_stable_when_written(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    out = tmp_path / "plan.json"

    first = build_plan(inbox, ledger_path, approved_path, out_path=out)
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["plan_id"] == first.plan_id
    assert document["kind"] == INTAKE_PLAN_KIND
    assert document["written_path"] is None  # 文件内容里不带"写开关"的自述

    again = build_plan(inbox, ledger_path, approved_path, out_path=out)
    assert again.plan_id == first.plan_id
    assert out.read_text(encoding="utf-8") == text  # 幂等：逐字节稳定
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []



# ---------------------------------------------------------------------------
# 最终写入前核验：逐项 fail-closed
# ---------------------------------------------------------------------------
def test_stale_fingerprint_is_fail_closed(tmp_path: Path) -> None:
    """内容变化 → 新指纹：旧批准绝不继承，且不产出"可落库"的计划。"""
    inbox, package_dir = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)

    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row("a-0001"), author_row("a-0002")], fmt="jsonl")
    manifest_path = package_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(evidence.read_bytes())
    write_json(manifest_path, manifest)

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path)
    assert PlanVerificationCode.FINGERPRINT_MISSING.value in codes_of(failure)
    # 历史仍保留（ledger 未被改写）
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["decision_count"] == 1


def test_candidate_disappearance_is_fail_closed(tmp_path: Path) -> None:
    inbox, package_dir = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)

    for item in package_dir.iterdir():
        item.unlink()
    package_dir.rmdir()

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path)
    assert PlanVerificationCode.FINGERPRINT_MISSING.value in codes_of(failure)


def test_preflight_regression_is_fail_closed(tmp_path: Path) -> None:
    """同一内容在不同审计时点可能"过期"：计划必须按**当前**证据重新核验。"""
    inbox = tmp_path / "inbox"
    early = datetime(2026, 6, 3, tzinfo=UTC)
    build_package(
        inbox,
        rows=[author_row("late-review", authorization_reviewed_at="2026-06-05T00:00:00+00:00")],
    )
    ledger_path, approved_path = approve_package(tmp_path, inbox)  # 在 MOMENT 通过并批准

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path, moment=early)
    assert PlanVerificationCode.PREFLIGHT_NOT_PASSING.value in codes_of(failure)


def test_review_override_supersedes_old_approval(tmp_path: Path) -> None:
    """显式 revision + override 推翻批准后：旧批准与旧计划不得静默继承。"""
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    fingerprint = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint

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

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path, moment=LATER)
    assert PlanVerificationCode.LEDGER_DECISION_SUPERSEDED.value in codes_of(failure)

    # 重新生成清单（按当前 truth）后：计划如实变成 BLOCKED，而不是"看起来可以落库"
    run_review(
        inbox,
        moment=LATER,
        ledger_path=ledger_path,
        out_path=ledger_path,
        approved_out_path=approved_path,
    )
    plan = build_plan(inbox, ledger_path, approved_path, moment=LATER)
    assert plan.status is IntakePlanStatus.BLOCKED_NO_APPROVED_EVIDENCE
    assert plan.entries == ()



# ---------------------------------------------------------------------------
# approved-list tamper（内容 / 计数 / 时间）
# ---------------------------------------------------------------------------
def _ready(tmp_path: Path) -> tuple[Path, Path, Path]:
    """建一个"已批准且当前仍一致"的 inbox + ledger + 批准清单。"""
    inbox, _package = _inbox_with_package(tmp_path)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    return inbox, ledger_path, approved_path


def test_tampered_approved_entry_fields_are_fail_closed(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)

    cases: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
        (
            "reviewer",
            lambda payload: payload["approved"][0].__setitem__("reviewer", "someone-else"),
            PlanVerificationCode.APPROVED_LIST_TAMPERED.value,
        ),
        (
            "rows",
            lambda payload: payload["approved"][0].__setitem__("rows", 999),
            PlanVerificationCode.APPROVED_LIST_TAMPERED.value,
        ),
        (
            "package_dir",
            lambda payload: payload["approved"][0].__setitem__("package_dir", "/tmp/other"),
            PlanVerificationCode.APPROVED_LIST_TAMPERED.value,
        ),
        (
            "extra-key",
            lambda payload: payload["approved"][0].__setitem__("note", "偷偷加的字段"),
            PlanVerificationCode.APPROVED_LIST_TAMPERED.value,
        ),
        (
            "decision-id",
            lambda payload: payload["approved"][0].__setitem__("decision_id", "b" * 64),
            PlanVerificationCode.LEDGER_REVISION_SUPERSEDED.value,
        ),
        (
            "decision-counts",
            lambda payload: payload.__setitem__("decision_counts", {"REJECT": 1}),
            PlanVerificationCode.COUNT_MISMATCH.value,
        ),
        (
            "invalidated",
            lambda payload: (
                payload.__setitem__(
                    "invalidated",
                    [
                        {
                            "fingerprint": "c" * 64,
                            "decision_id": "d" * 64,
                            "reason_code": "CANDIDATE_MISSING",
                            "detail": "x",
                        }
                    ],
                ),
                payload.__setitem__("invalidated_count", 1),
            ),
            PlanVerificationCode.INVALIDATED_MISMATCH.value,
        ),
        (
            "future-generated-at",
            lambda payload: payload.__setitem__(
                "generated_at", (MOMENT + timedelta(days=1)).isoformat()
            ),
            PlanVerificationCode.PLAN_TIME_BEFORE_APPROVAL.value,
        ),
    ]
    for name, mutate, expected in cases:
        tamper(approved_path, mutate)
        with pytest.raises(IntakePlanInconsistentError) as failure:
            build_plan(inbox, ledger_path, approved_path)
        assert expected in codes_of(failure), f"{name} 未 fail-closed：{codes_of(failure)}"
        # 恢复原始清单（每个 case 独立）
        ledger_path, approved_path = approve_package(tmp_path, inbox)
        assert ledger_path is not None



def test_approved_list_without_an_still_valid_entry_is_stale(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    payload = json.loads(approved_path.read_text(encoding="utf-8"))
    payload["approved"] = []
    payload["approved_count"] = 0
    write_json(approved_path, payload)

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path)
    assert PlanVerificationCode.APPROVED_LIST_STALE.value in codes_of(failure)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("kind", "not_an_approved_list"),
        lambda payload: payload.__setitem__("schema_version", 99),
        lambda payload: payload.__setitem__("contract_version", "evidence-intake-v0"),
        lambda payload: payload.__setitem__("blocker_active", False),
        lambda payload: payload.__setitem__("human_gate_required", False),
        lambda payload: payload.__setitem__("data_qualification_passed", True),
        lambda payload: payload.__setitem__("phase_transition_allowed", True),
        lambda payload: payload.__setitem__("approval_scope", "data_qualification"),
        lambda payload: payload.__setitem__("approved_count", 5),
        lambda payload: payload.__setitem__("invalidated_count", 3),
        lambda payload: payload["approved"][0].pop("reason_code"),
        lambda payload: payload["approved"][0].__setitem__("requires_explicit_intake", False),
        lambda payload: payload["approved"][0].__setitem__("approval_scope", "qualified"),
        lambda payload: payload["approved"][0].__setitem__("fingerprint", "not-a-fingerprint"),
        lambda payload: payload["approved"][0].__setitem__("decision_id", "xyz"),
        lambda payload: payload["approved"][0].__setitem__("published_at", "2026-06-01T00:00:00Z"),
        lambda payload: payload["approved"][0].__setitem__("evidence_files", "author.jsonl"),
        lambda payload: payload.__setitem__("generated_at", "2026-09-23T00:00:00"),
    ],
    ids=[
        "kind",
        "schema",
        "contract",
        "blocker",
        "human-gate",
        "qualification",
        "phase-transition",
        "approval-scope",
        "approved-count",
        "invalidated-count",
        "missing-field",
        "explicit-intake",
        "entry-scope",
        "fingerprint",
        "decision-id",
        "forbidden-evidence-field",
        "files-type",
        "naive-generated-at",
    ],
)
def test_tampered_approved_list_state_is_fail_closed(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    tamper(approved_path, mutate)

    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)


def test_unreadable_or_missing_state_is_fail_closed(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    assert build_plan(inbox, ledger_path, approved_path).has_approved is True

    approved_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)

    approved_path.write_text("[]", encoding="utf-8")
    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)

    approved_path.unlink()
    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)


def test_corrupt_ledger_is_fail_closed(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    ledger_path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)
    # 旧文件原样保留（fail-closed 不等于"清空历史"）
    assert ledger_path.read_text(encoding="utf-8") == "{ not json"


def test_tampered_ledger_decision_id_is_fail_closed(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["decisions"][0]["decision_id"] = "e" * 64

    tamper(ledger_path, mutate)
    with pytest.raises(IntakePlanStateError):
        build_plan(inbox, ledger_path, approved_path)


def test_synthetic_candidate_can_never_be_planned(tmp_path: Path) -> None:
    """模板 / 示例 / Mock 候选：GOLD-012 绝不批准，GOLD-013 也绝不因"曾被批准"而放行。"""
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-marked"
    package_dir.mkdir(parents=True)
    row = author_row()
    row["is_mock"] = "true"
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [row], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["is_mock"] = "true"
    write_json(package_dir / MANIFEST_FILE_NAME, manifest)

    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.synthetic is True

    # 手工构造"曾被批准"的 ledger + 清单：GOLD-013 必须仍然拒绝
    record = crafted_record(package.fingerprint)
    ledger_path = write_json(tmp_path / "crafted_ledger.json", crafted_ledger([record]))
    approved_path = write_json(
        tmp_path / "crafted_approved.json",
        crafted_approved_list([crafted_entry(package.fingerprint, record["decision_id"])]),
    )

    with pytest.raises(IntakePlanInconsistentError) as failure:
        build_plan(inbox, ledger_path, approved_path)
    assert PlanVerificationCode.SYNTHETIC_EVIDENCE.value in codes_of(failure)



# ---------------------------------------------------------------------------
# 只读 / 原子写 / 输出位置
# ---------------------------------------------------------------------------
def test_read_only_run_writes_nothing(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    ledger_bytes = ledger_path.read_bytes()
    approved_bytes = approved_path.read_bytes()

    build_plan(inbox, ledger_path, approved_path)

    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert ledger_path.read_bytes() == ledger_bytes
    assert approved_path.read_bytes() == approved_bytes


def test_output_inside_inbox_is_rejected_before_writing(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    inside = inbox / "plan.json"

    with pytest.raises(IntakePlanPathError):
        build_plan(inbox, ledger_path, approved_path, out_path=inside)
    assert not inside.exists()
    # 输入 artifact 也不允许放在 inbox 内
    with pytest.raises(IntakePlanPathError):
        run_intake_plan(
            inbox,
            moment=MOMENT,
            ledger_path=inbox / "ledger.json",
            approved_list_path=approved_path,
        )


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    from src.evidence import SingleInstanceLock

    inbox, ledger_path, approved_path = _ready(tmp_path)
    out = tmp_path / "plan.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")
    with held, pytest.raises(LockConflictError):
        build_plan(inbox, ledger_path, approved_path, out_path=out)
    assert not out.exists()


def test_concurrent_plan_writes_never_produce_partial_documents(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    out = tmp_path / "plan.json"
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            build_plan(inbox, ledger_path, approved_path, out_path=out)
        except LockConflictError:  # 并发时另一个线程持锁 → fail-closed（可接受）
            pass
        except BaseException as exc:  # noqa: BLE001 - 测试需要收集任何异常
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["kind"] == INTAKE_PLAN_KIND
    assert document["plan_id"] == build_plan(inbox, ledger_path, approved_path).plan_id
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []



# ---------------------------------------------------------------------------
# 显式衔接 / 安全字段 / 脱敏 / 复核时间不是证据
# ---------------------------------------------------------------------------
def test_handoff_is_explicit_and_never_executed(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    payload = build_plan(inbox, ledger_path, approved_path).to_dict()

    assert payload["requires_explicit_operator_action"] is True
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["operator_explicit_flag"] == OPERATOR_EXPLICIT_FLAG
    assert payload["handoff"], "至少应给出一条显式 operator 命令模板"
    for step in payload["handoff"]:
        assert "--no-dry-run" in step["command"]
        assert "--input" in step["command"]
        assert step["auto_executed"] is False
        assert step["requires_explicit_operator_action"] is True
        assert step["required_flag"] == OPERATOR_EXPLICIT_FLAG


def test_many_approvals_never_change_safety_fields(tmp_path: Path) -> None:
    inbox, _package = _inbox_with_package(tmp_path)
    build_package(inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    build_package(inbox, name="pkg-author-03", rows=[author_row("c-0001")])
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    report = scan_inbox(inbox, moment=MOMENT)
    for index, package in enumerate(report.packages):
        run_review(
            inbox,
            moment=MOMENT,
            decision="approve",
            fingerprint=package.fingerprint,
            reviewer="operator-li",
            reason_code=APPROVE_CODE,
            ledger_path=None if index == 0 else ledger_path,
            out_path=ledger_path,
            approved_out_path=approved_path,
        )

    payload = build_plan(inbox, ledger_path, approved_path).to_dict()
    assert payload["approved_for_explicit_intake_count"] == 3
    assert payload["data_qualification_passed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["phase_transition_allowed"] is False
    assert payload["auto_intake_allowed"] is False


def _all_keys(payload: object) -> set[str]:
    """递归收集 JSON 结构里的**全部键名**（用于断言禁止字段不存在）。"""
    found: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            found.add(str(key))
            found |= _all_keys(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            found |= _all_keys(item)
    return found


def test_plan_never_carries_evidence_time_fields(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    payload = build_plan(inbox, ledger_path, approved_path).to_dict()
    text = json.dumps(payload, ensure_ascii=False)

    assert not (_all_keys(payload) & set(FORBIDDEN_EVIDENCE_FIELDS))
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert f'"{field_name}"' not in text
    # 行内证据时间与可用性溯源不得被复制进计划
    assert "2026-06-01T00:00:00+00:00" not in text
    assert "provider_archive_export" not in text
    # 计划里没有正文 / 引用类字段
    assert "authorization_reference" not in text
    assert "author_name" not in text


def test_plan_artifacts_are_redacted(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    # 包名里塞入凭据样式字符串：任何 artifact 都不得把它原样带出来
    package_dir = inbox / f"pkg-author-{SECRET}"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["authorization_reference"] = "https://vendor.example/terms?v=2&operator=li"
    write_json(package_dir / MANIFEST_FILE_NAME, manifest)
    ledger_path, approved_path = approve_package(
        tmp_path, inbox, package_dir_name=package_dir.name
    )

    text = json.dumps(build_plan(inbox, ledger_path, approved_path).to_dict(), ensure_ascii=False)
    assert SECRET not in text
    assert "?v=2" not in text
    assert "authorization_reference" not in text


def test_render_summary_keeps_blocker_and_human_gate_visible(tmp_path: Path) -> None:
    inbox, ledger_path, approved_path = _ready(tmp_path)
    summary = render_intake_plan_summary(build_plan(inbox, ledger_path, approved_path))

    assert "blocker_active" in summary
    assert "human_gate_required" in summary
    assert "--no-dry-run" in summary
    assert "data_qualification_passed_count" in summary
    assert PHASE3_3_BLOCKER_CODE in summary



# ---------------------------------------------------------------------------
# 源码守卫 / CLI 编排
# ---------------------------------------------------------------------------
def test_module_has_no_network_database_or_intake_behaviour() -> None:
    source = Path(intake_plan_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    for banned in ("aiohttp", "httpx", "requests", "socket", "urllib", "sqlalchemy", "subprocess"):
        assert all(banned not in module for module in modules), f"不得引入 {banned}：{modules}"
    # 绝不删除 / 移动 / 改名原始 evidence；绝不调用任何 intake / commit 路径
    for banned_call in ("unlink", "rmtree", "os.remove", "os.rename", "shutil.move"):
        assert banned_call not in source
    assert "intake_evidence" not in source
    assert "evaluate_write_gate" not in source
    assert "atomic_write_text" in source  # 唯一写路径 = 既有原子写原语
    # 无网络 / 无数据库 / 无常驻循环
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    # 绝不把复核 / 计划时间当证据
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert f'"{field_name}":' not in source


def test_cli_module_defers_to_core_module_and_exposes_no_intake_flag() -> None:
    from scripts.evidence_intake_plan import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as missing_inbox_dir:
        parser.parse_args(["--json"])
    assert missing_inbox_dir.value.code == 2
    with pytest.raises(SystemExit) as missing_ledger:
        parser.parse_args(["--inbox-dir", "x", "--approved-list", "y"])
    assert missing_ledger.value.code == 2
    with pytest.raises(SystemExit) as missing_approved:
        parser.parse_args(["--inbox-dir", "x", "--ledger", "y"])
    assert missing_approved.value.code == 2
    # 绝不暴露任何"真实落库 / 自动执行"开关
    with pytest.raises(SystemExit) as intake_flag:
        parser.parse_args(
            ["--inbox-dir", "x", "--ledger", "y", "--approved-list", "z", "--no-dry-run"]
        )
    assert intake_flag.value.code == 2

    parsed = parser.parse_args(["--inbox-dir", "x", "--ledger", "y", "--approved-list", "z"])
    assert parsed.out is None  # 默认零写入
    assert parsed.json_output is False

