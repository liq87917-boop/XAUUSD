"""GOLD-014 Intake Receipt 单元测试（临时目录 / Mock 证据包与审计产物 / 零数据库 / 零网络）。

覆盖：

- 退出码映射稳定；缺 plan / ledger / approved list / inbox：参数或路径错误；
- plan 文档严格只读视图：文档标识 / schema / 契约版本 / 安全字段不可被削弱 / 条目白名单键 /
  计数自洽 / 禁止证据时间键 → 任一不合法即 fail-closed；
- 执行后重新绑定：plan stale（内容变化）/ fingerprint drift / review override / 条目摘要变化；
- 人工显式执行结果：缺失 / dry-run / 落库 0 行 / 计数自相矛盾 / 输入内容不匹配 / scope 不匹配 /
  覆盖不全 / 用了**未被批准**的输入 → 逐项 fail-closed；
- qualification recheck：缺失 / 早于执行 / blocker 被"解除" → fail-closed；
- 明确区分四个布尔：``intake_executed`` / ``receipt_verified`` **绝不**蕴含
  ``data_qualification_passed`` / ``phase_transition_allowed``（后两者恒 false）；
- 内容级 ``receipt_id`` 幂等 / byte-stable；plan / 执行结果 / 复核任一变化 → 新 ``receipt_id``；
- 默认只读：不写数据库、不写 inbox、不移动 / 改写原始 evidence；原子写与并发；锁冲突；
- 脱敏与"操作时间不是证据时间"；Markdown 摘要保留 blocker / human gate；源码守卫与 CLI 编排。
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
from types import SimpleNamespace
from typing import Any

import pytest

from src.common import hashing
from src.evidence import (
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    FORBIDDEN_EVIDENCE_FIELDS,
    INBOX_SCHEMA_VERSION,
    INTAKE_RECEIPT_KIND,
    INTAKE_RECEIPT_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    OPERATOR_MANIFEST_REPORT,
    QUALIFICATION_RECHECK_REPORT,
    RECEIPT_EXECUTION_MODE,
    IntakeReceiptArgumentError,
    IntakeReceiptPathError,
    IntakeReceiptStateError,
    IntakeReceiptStatus,
    IntakeReceiptVerificationError,
    IntakeReceiptWriteError,
    LockConflictError,
    ReceiptVerificationCode,
    ReviewReasonCode,
    intake_receipt_exit_code_for,
    load_intake_plan_document,
    load_operator_result,
    load_qualification_recheck,
    render_intake_receipt_summary,
    run_intake_plan,
    run_intake_receipt,
    run_review,
    scan_inbox,
    verify_intake_receipt,
)
from src.evidence import intake_receipt as receipt_module
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
INTAKE_AT = MOMENT + timedelta(hours=1)
RECHECK_AT = MOMENT + timedelta(hours=2)
RECEIPT_AT = MOMENT + timedelta(hours=3)
LATER = MOMENT + timedelta(hours=6)
#: 当 review / plan 在 ``LATER`` 被重新生成时，收据核验时点必须**不早于**它
LATER_AT = LATER + timedelta(hours=1)
LATER_RECHECK_AT = LATER + timedelta(hours=2)
#: 凭证类字符串（必须被脱敏，绝不进入任何 artifact）
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
REJECT_CODE = ReviewReasonCode.REJECTED_AUTHORIZATION_INSUFFICIENT.value
#: 用于「显式传入 / 显式不传」的哨兵
OMIT = object()


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
) -> Path:
    """在 ``root`` 下建一个合规候选包目录（manifest 摘要取自实际内容）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    payload_rows = list(rows if rows is not None else [author_row()])
    evidence = package / evidence_name
    write_rows(evidence, payload_rows, fmt=fmt)
    document = manifest_payload(evidence, evidence_name=evidence_name)
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return package


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """把字典写成 JSON 文件（UTF-8）并返回路径。"""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def tamper(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """就地篡改一个 JSON 文档（模拟 plan / manifest / recheck 被改写）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json(path, payload)


def codes_of(failure: pytest.ExceptionInfo[IntakeReceiptVerificationError]) -> list[str]:
    """取失败里的稳定原因码（排序；用于断言 fail-closed 的具体口径）。"""
    return sorted(item.code for item in failure.value.violations)


def operator_manifest(
    evidence: Path,
    *,
    scope: str = "author",
    dry_run: bool = False,
    persisted: int = 1,
    rows_count: int = 1,
    accepted: int | None = None,
    quarantined: int = 0,
    generated_at: datetime = INTAKE_AT,
    input_sha256: str | None = None,
    input_path: str | None = None,
    counts_overrides: Mapping[str, int] | None = None,
    row_entries: int | None = None,
) -> dict[str, Any]:
    """构造**格式合法**的 Evidence Operator 执行结果（manifest；与 GOLD-005 结构一致）。"""
    counts: dict[str, int] = {
        "rows": rows_count,
        "accepted": accepted if accepted is not None else persisted + quarantined,
        "quarantined": quarantined,
        "duplicate": 0,
        "oos_eligible": persisted,
        "not_oos_eligible": 0,
        "persisted": persisted,
        "sources_created": 1 if persisted else 0,
        "processed_success": persisted,
    }
    counts.update(dict(counts_overrides or {}))
    entries = row_entries if row_entries is not None else counts["rows"]
    return {
        "schema_version": 1,
        "report": OPERATOR_MANIFEST_REPORT,
        "contract_version": "evidence-intake-v1",
        "scope": scope,
        "dry_run": dry_run,
        "generated_at": generated_at.isoformat(),
        "input": {
            "path": input_path if input_path is not None else str(evidence),
            "format": "jsonl" if evidence.name.endswith(".jsonl") else "csv",
            "sha256": (
                input_sha256
                if input_sha256 is not None
                else hashing.sha256_bytes(evidence.read_bytes())
            ),
        },
        "counts": counts,
        "rows": [
            {
                "index": index,
                "row_number": index + 1,
                "status": "ACCEPTED",
                "reason_codes": [],
                "reasons": [],
                "fingerprint": f"{index:064x}"[:64],
                "source": "vendor-author",
                "source_record_id": f"a-{index:04d}",
                "oos_eligible": True,
                "not_oos_eligible_reason": None,
                "persisted": True,
            }
            for index in range(entries)
        ],
        "notes": [],
    }


def recheck_payload(
    *,
    as_of: datetime = RECHECK_AT,
    ready: bool = False,
    blocker_code: str = PHASE3_3_BLOCKER_CODE,
    blocker_active: bool = True,
    human_gate_required: bool = True,
    gate_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造**格式合法**的 qualification recheck 产物（``phase33_qualification_recheck``）。"""
    gate: dict[str, Any] = {
        "qualification_ready": False,
        "qualification_pass_count": 3,
        "qualification_blocked_count": 5,
        "readiness_ready": False,
        "readiness_blocked_scope_count": 2,
    }
    gate.update(dict(gate_overrides or {}))
    return {
        "schema_version": 1,
        "report": QUALIFICATION_RECHECK_REPORT,
        "contract_version": "evidence-intake-v1",
        "as_of": as_of.isoformat(),
        "blocker_code": blocker_code,
        "blocker_active": blocker_active,
        "human_gate_required": human_gate_required,
        "ready": ready,
        "gate": gate,
        "readiness": {"scopes": []},
        "qualification": {"checks": []},
        "notes": [],
    }


def approve_package(
    tmp_path: Path,
    inbox: Path,
    *,
    package_dir_name: str = "pkg-author-01",
    moment: datetime = MOMENT,
) -> tuple[Path, Path]:
    """按 GOLD-012 口径对指定候选包 approve，并落盘 ledger + 批准清单。"""
    report = scan_inbox(inbox, moment=moment)
    target = next(item for item in report.packages if item.package_dir == package_dir_name)
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
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


def write_plan(
    tmp_path: Path,
    inbox: Path,
    ledger_path: Path,
    approved_path: Path,
    *,
    moment: datetime = MOMENT,
    name: str = "evidence_intake_plan.json",
) -> Path:
    """用 GOLD-013 真实入口生成 plan 文件（收据核验的对象）。"""
    out = tmp_path / name
    run_intake_plan(
        inbox,
        moment=moment,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
        out_path=out,
    )
    return out


def ready_setup(tmp_path: Path) -> SimpleNamespace:
    """构造一条完整的、可核验的路径：inbox → review → plan → 显式执行结果 → recheck。"""
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)
    evidence = package_dir / EVIDENCE_NAME
    manifest_path = write_json(tmp_path / "operator_manifest.json", operator_manifest(evidence))
    recheck_path = write_json(tmp_path / "recheck.json", recheck_payload())
    return SimpleNamespace(
        inbox=inbox,
        package_dir=package_dir,
        evidence=evidence,
        ledger=ledger_path,
        approved=approved_path,
        plan=plan_path,
        manifest=manifest_path,
        recheck=recheck_path,
    )


def receipt_kwargs(setup: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    """构造 ``run_intake_receipt`` 的关键字参数（可覆盖；用于参数级失败测试）。"""
    kwargs: dict[str, Any] = {
        "moment": RECEIPT_AT,
        "plan_path": setup.plan,
        "ledger_path": setup.ledger,
        "approved_list_path": setup.approved,
        "operator_result_paths": (setup.manifest,),
        "recheck_path": setup.recheck,
    }
    kwargs.update(overrides)
    return kwargs


def build_receipt(
    setup: SimpleNamespace,
    *,
    moment: datetime = RECEIPT_AT,
    operator_results: Any = OMIT,
    recheck: Any = OMIT,
    out_path: Path | None = None,
    lock_path: Path | None = None,
) -> Any:
    """按 setup 生成一次收据（默认带上 setup 里的执行结果与 recheck）。"""
    return run_intake_receipt(
        setup.inbox,
        moment=moment,
        plan_path=setup.plan,
        ledger_path=setup.ledger,
        approved_list_path=setup.approved,
        operator_result_paths=(
            (setup.manifest,) if operator_results is OMIT else tuple(operator_results)
        ),
        recheck_path=setup.recheck if recheck is OMIT else recheck,
        out_path=out_path,
        lock_path=lock_path,
    )


# ---------------------------------------------------------------------------
# 退出码 / 参数
# ---------------------------------------------------------------------------
def test_exit_code_mapping_is_stable() -> None:
    assert intake_receipt_exit_code_for(IntakeReceiptArgumentError("x")) == EXIT_CONFIG_ERROR
    assert intake_receipt_exit_code_for(IntakeReceiptStateError("x")) == EXIT_STATE_INVALID
    assert intake_receipt_exit_code_for(IntakeReceiptVerificationError([])) == EXIT_STATE_INVALID
    assert intake_receipt_exit_code_for(IntakeReceiptPathError("x")) == EXIT_UNUSABLE
    assert intake_receipt_exit_code_for(IntakeReceiptWriteError("x")) == EXIT_UNUSABLE
    assert intake_receipt_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert intake_receipt_exit_code_for(RuntimeError("x")) == EXIT_STATE_INVALID
    assert EXIT_OK == 0 and EXIT_BLOCKED == 5


def test_missing_required_inputs_and_naive_moment_are_rejected(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    args: dict[str, Any] = {
        "moment": RECEIPT_AT,
        "plan_path": setup.plan,
        "ledger_path": setup.ledger,
        "approved_list_path": setup.approved,
    }
    for missing in ("plan_path", "ledger_path", "approved_list_path"):
        broken = dict(args)
        broken[missing] = None
        with pytest.raises(IntakeReceiptArgumentError):
            run_intake_receipt(setup.inbox, **broken)
    with pytest.raises(IntakeReceiptArgumentError):
        run_intake_receipt(setup.inbox, **{**args, "moment": datetime(2026, 9, 23)})


def test_missing_inbox_dir_is_a_path_error(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptPathError):
        run_intake_receipt(
            tmp_path / "absent-inbox",
            moment=RECEIPT_AT,
            plan_path=setup.plan,
            ledger_path=setup.ledger,
            approved_list_path=setup.approved,
        )


# ---------------------------------------------------------------------------
# 正常路径：plan + 当前状态 + 显式执行结果 + recheck 全部绑定
# ---------------------------------------------------------------------------
def test_verified_receipt_binds_plan_execution_and_recheck(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    plan_document = load_intake_plan_document(setup.plan)

    receipt = build_receipt(setup)

    assert receipt.status is IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED
    assert receipt.receipt_verified is True
    assert receipt.intake_executed is True
    assert receipt.plan_id == plan_document.plan_id
    assert len(receipt.receipt_id) == 64
    assert receipt.written_path is None  # 默认只读：不写任何文件
    assert len(receipt.entries) == 1
    entry = receipt.entries[0]
    assert entry.revision == 1 and entry.operator_scope == "author" and entry.executed is True
    assert entry.content_sha256 == (hashing.sha256_bytes(setup.evidence.read_bytes()),)
    assert receipt.operator_results[0].scope == "author"
    assert receipt.operator_results[0].dry_run is False
    assert receipt.operator_results[0].persisted == 1
    assert receipt.recheck is not None and receipt.recheck.report == QUALIFICATION_RECHECK_REPORT
    payload = receipt.to_dict()
    assert payload["kind"] == INTAKE_RECEIPT_KIND
    assert payload["schema_version"] == INTAKE_RECEIPT_SCHEMA_VERSION
    assert payload["report"] == INTAKE_RECEIPT_KIND
    assert payload["execution_mode"] == RECEIPT_EXECUTION_MODE
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["requires_explicit_operator_action"] is True
    assert payload["intake_executed"] is True
    assert payload["receipt_verified"] is True
    assert payload["landed_rows"] == 1
    assert payload["executed_entry_count"] == 1
    assert payload["verification"]["violations"] == []
    assert payload["plan"]["plan_id"] == plan_document.plan_id
    assert payload["evidence_time_semantics"]["contains_evidence_times"] is False
    assert payload["written_path"] is None


def test_blocked_receipt_when_nothing_is_approved(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    run_review(inbox, moment=MOMENT, out_path=ledger_path, approved_out_path=approved_path)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)

    receipt = run_intake_receipt(
        inbox,
        moment=RECEIPT_AT,
        plan_path=plan_path,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
    )

    assert receipt.status is IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE
    assert receipt.intake_executed is False
    assert receipt.receipt_verified is False
    assert receipt.entries == () and receipt.operator_results == () and receipt.recheck is None
    payload = receipt.to_dict()
    assert payload["blocker_active"] is True and payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["qualification_recheck"] is None


def test_blocked_plan_with_execution_result_is_fail_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    ledger_path = tmp_path / "empty_ledger.json"
    approved_path = tmp_path / "empty_approved.json"
    run_review(inbox, moment=MOMENT, out_path=ledger_path, approved_out_path=approved_path)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)
    manifest = write_json(
        tmp_path / "manifest.json", operator_manifest(package_dir / EVIDENCE_NAME)
    )
    recheck = write_json(tmp_path / "recheck.json", recheck_payload())

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        run_intake_receipt(
            inbox,
            moment=RECEIPT_AT,
            plan_path=plan_path,
            ledger_path=ledger_path,
            approved_list_path=approved_path,
            operator_result_paths=(manifest,),
            recheck_path=recheck,
        )
    codes = codes_of(failure)
    assert ReceiptVerificationCode.OPERATOR_UNBOUND.value in codes
    assert ReceiptVerificationCode.RECHECK_UNBOUND.value in codes


# ---------------------------------------------------------------------------
# plan 严格只读视图：结构篡改一律 fail-closed
# ---------------------------------------------------------------------------
def _plan_mutations() -> list[tuple[str, Callable[[dict[str, Any]], None]]]:
    """plan 文档的篡改口径（每一项都必须 fail-closed）。"""

    def set_field(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(payload: dict[str, Any]) -> None:
            payload[field] = value

        return mutate

    def entry_field(field: str, value: Any, *, action: str = "set") -> Callable[..., None]:
        def mutate(payload: dict[str, Any]) -> None:
            entry = payload["approved_for_explicit_intake"][0]
            if action == "del":
                entry.pop(field)
            else:
                entry[field] = value

        return mutate

    return [
        ("kind", set_field("kind", "evidence_inbox_approved_for_explicit_intake")),
        ("schema_version", set_field("schema_version", 999)),
        ("contract_version", set_field("contract_version", "evidence-intake-v9")),
        ("blocker_active", set_field("blocker_active", False)),
        ("human_gate_required", set_field("human_gate_required", False)),
        ("data_qualification_passed", set_field("data_qualification_passed", True)),
        ("phase_transition_allowed", set_field("phase_transition_allowed", True)),
        ("auto_intake_allowed", set_field("auto_intake_allowed", True)),
        ("writes_database", set_field("writes_database", True)),
        (
            "requires_explicit_operator_action",
            set_field("requires_explicit_operator_action", False),
        ),
        ("plan_id", set_field("plan_id", "not-a-hash")),
        ("count", set_field("approved_for_explicit_intake_count", 7)),
        ("qualification_count", set_field("data_qualification_passed_count", 1)),
        ("generated_at_naive", set_field("generated_at", "2026-09-23T00:00:00")),
        ("evidence_time_key", set_field("published_at", "2026-06-01T00:00:00+00:00")),
        ("entry_missing_key", entry_field("revision", None, action="del")),
        ("entry_extra_key", entry_field("note", "hacked")),
        ("entry_not_approved", entry_field("approved_for_explicit_intake", False)),
        ("entry_qualification_true", entry_field("data_qualification_passed", True)),
        ("entry_explicit_false", entry_field("requires_explicit_operator_action", False)),
        ("entry_scope", entry_field("operator_scope", "twitter")),
        ("entry_fingerprint", entry_field("fingerprint", "zz")),
        ("entry_files_empty", entry_field("evidence_files", [])),
        ("entry_rows_negative", entry_field("rows", -1)),
    ]


@pytest.mark.parametrize(
    "mutator", [item[1] for item in _plan_mutations()], ids=[item[0] for item in _plan_mutations()]
)
def test_tampered_plan_document_is_fail_closed(
    tmp_path: Path, mutator: Callable[[dict[str, Any]], None]
) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.plan, mutator)

    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)


def test_missing_or_corrupt_plan_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptStateError):
        run_intake_receipt(
            setup.inbox, **receipt_kwargs(setup, plan_path=tmp_path / "absent_plan.json")
        )
    broken = tmp_path / "broken_plan.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(IntakeReceiptStateError):
        run_intake_receipt(setup.inbox, **receipt_kwargs(setup, plan_path=broken))
    listed = tmp_path / "list_plan.json"
    listed.write_text("[]", encoding="utf-8")
    with pytest.raises(IntakeReceiptStateError):
        run_intake_receipt(setup.inbox, **receipt_kwargs(setup, plan_path=listed))


# ---------------------------------------------------------------------------
# 与**当前**状态重新绑定：plan stale / fingerprint drift / review override
# ---------------------------------------------------------------------------
def test_stale_plan_after_content_change_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    write_rows(setup.evidence, [author_row("a-0002")], fmt="jsonl")  # 内容变化 → 新指纹

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    codes = codes_of(failure)
    assert ReceiptVerificationCode.PLAN_STALE.value in codes
    assert ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value in codes


def test_rewritten_plan_id_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.plan, lambda payload: payload.__setitem__("plan_id", "a" * 64))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.PLAN_STALE.value in codes_of(failure)


def test_review_override_invalidates_previous_receipt(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    fingerprint = load_intake_plan_document(setup.plan).entries[0].fingerprint

    run_review(
        setup.inbox,
        moment=LATER,
        decision="reject",
        fingerprint=fingerprint,
        reviewer="operator-li",
        reason_code=REJECT_CODE,
        ledger_path=setup.ledger,
        out_path=setup.ledger,
        approved_out_path=setup.approved,
        revision=2,
        override=True,
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup, moment=LATER_AT)
    codes = codes_of(failure)
    assert ReceiptVerificationCode.PLAN_STALE.value in codes
    assert ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value in codes

    # 用**当前**事实重新生成 plan 后，得到的是「没有任何仍成立批准」的 BLOCKED 收据
    fresh_plan = write_plan(
        tmp_path, setup.inbox, setup.ledger, setup.approved, moment=LATER, name="plan2.json"
    )
    blocked = run_intake_receipt(
        setup.inbox,
        **receipt_kwargs(
            setup,
            plan_path=fresh_plan,
            moment=LATER_AT,
            operator_result_paths=(),
            recheck_path=None,
        ),
    )
    assert blocked.status is IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE
    assert blocked.receipt_verified is False


# ---------------------------------------------------------------------------
# 人工显式执行结果：缺失 / 失败 / 不匹配 一律 fail-closed
# ---------------------------------------------------------------------------
def test_operator_result_missing_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup, operator_results=())
    assert ReceiptVerificationCode.OPERATOR_RESULT_MISSING.value in codes_of(failure)


def test_recheck_missing_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup, recheck=None)
    assert ReceiptVerificationCode.RECHECK_MISSING.value in codes_of(failure)


def test_dry_run_operator_result_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.manifest,
        operator_manifest(setup.evidence, dry_run=True, persisted=0, accepted=0),
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    codes = codes_of(failure)
    # 单条结果本身未执行 + 覆盖度检查也判定"没有非 dry-run 的执行" → 同一稳定原因码两次
    assert codes == [ReceiptVerificationCode.OPERATOR_NOT_EXECUTED.value] * 2


def test_operator_zero_persisted_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.manifest,
        operator_manifest(setup.evidence, persisted=0, accepted=1, quarantined=1),
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.OPERATOR_FAILED.value in codes_of(failure)


def test_operator_input_digest_mismatch_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.manifest, operator_manifest(setup.evidence, input_sha256="a" * 64))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    codes = codes_of(failure)
    assert ReceiptVerificationCode.OPERATOR_COVERAGE_INCOMPLETE.value in codes
    assert ReceiptVerificationCode.OPERATOR_INPUT_MISMATCH.value in codes


def test_operator_scope_mismatch_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.manifest, operator_manifest(setup.evidence, scope="news"))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.OPERATOR_SCOPE_MISMATCH.value in codes_of(failure)


def test_unapproved_input_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    other = build_package(setup.inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    write_json(setup.manifest, operator_manifest(other / EVIDENCE_NAME))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    codes = codes_of(failure)
    assert ReceiptVerificationCode.OPERATOR_INPUT_MISMATCH.value in codes
    assert ReceiptVerificationCode.OPERATOR_COVERAGE_INCOMPLETE.value in codes


def test_incomplete_coverage_over_many_approvals_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    build_package(setup.inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    second = scan_inbox(setup.inbox, moment=MOMENT)
    target = next(item for item in second.packages if item.package_dir == "pkg-author-02")
    run_review(
        setup.inbox,
        moment=LATER,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        ledger_path=setup.ledger,
        out_path=setup.ledger,
        approved_out_path=setup.approved,
    )
    plan_path = write_plan(
        tmp_path, setup.inbox, setup.ledger, setup.approved, moment=LATER, name="plan2.json"
    )
    fresh_recheck = write_json(
        tmp_path / "recheck2.json", recheck_payload(as_of=LATER_RECHECK_AT)
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        run_intake_receipt(
            setup.inbox,
            **receipt_kwargs(
                setup, plan_path=plan_path, moment=LATER_AT, recheck_path=fresh_recheck
            ),
        )
    assert ReceiptVerificationCode.OPERATOR_COVERAGE_INCOMPLETE.value in codes_of(failure)


@pytest.mark.parametrize(
    "manifest_mutation",
    [
        pytest.param(
            lambda payload: payload.__setitem__("counts", {**payload["counts"], "rows": 99}),
            id="counts_rows_mismatch",
        ),
        pytest.param(
            lambda payload: payload["counts"].__setitem__("persisted", 5),
            id="persisted_gt_accepted",
        ),
        pytest.param(
            lambda payload: payload["counts"].__setitem__("quarantined", 99),
            id="counts_sum_exceeds_rows",
        ),
    ],
)
def test_tampered_operator_counts_are_fail_closed(
    tmp_path: Path, manifest_mutation: Callable[[dict[str, Any]], None]
) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.manifest, manifest_mutation)

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.OPERATOR_TAMPERED.value in codes_of(failure)


def test_operator_result_before_plan_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.manifest,
        operator_manifest(setup.evidence, generated_at=MOMENT - timedelta(hours=1)),
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.INTAKE_BEFORE_PLAN.value in codes_of(failure)


def test_future_timestamps_are_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.manifest,
        operator_manifest(setup.evidence, generated_at=RECEIPT_AT + timedelta(hours=1)),
    )
    write_json(setup.recheck, recheck_payload(as_of=RECEIPT_AT + timedelta(hours=2)))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.FUTURE_TIMESTAMP.value in codes_of(failure)


@pytest.mark.parametrize(
    "manifest_mutation",
    [
        pytest.param(lambda payload: payload.__setitem__("report", "other report"), id="report"),
        pytest.param(lambda payload: payload.__setitem__("scope", "twitter"), id="scope"),
        pytest.param(lambda payload: payload.__setitem__("dry_run", "false"), id="dry_run_type"),
        pytest.param(lambda payload: payload.__setitem__("contract_version", "v9"), id="contract"),
        pytest.param(lambda payload: payload.pop("input"), id="input_missing"),
        pytest.param(
            lambda payload: payload["input"].__setitem__("sha256", "zz"), id="input_bad_digest"
        ),
        pytest.param(lambda payload: payload.pop("counts"), id="counts_missing"),
        pytest.param(lambda payload: payload["counts"].pop("persisted"), id="counts_key_missing"),
        pytest.param(
            lambda payload: payload["counts"].__setitem__("persisted", "1"), id="counts_bad_type"
        ),
        pytest.param(lambda payload: payload.pop("rows"), id="rows_missing"),
        pytest.param(
            lambda payload: payload.__setitem__("published_at", "2026-06-01T00:00:00+00:00"),
            id="evidence_time_key",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("generated_at", "2026-09-23T01:00:00"),
            id="naive_timestamp",
        ),
    ],
)
def test_corrupt_operator_result_is_fail_closed(
    tmp_path: Path, manifest_mutation: Callable[[dict[str, Any]], None]
) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.manifest, manifest_mutation)

    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)


def test_absent_or_corrupt_operator_result_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup, operator_results=(tmp_path / "absent_manifest.json",))
    setup.manifest.write_text("{broken", encoding="utf-8")
    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)


def test_recheck_blocker_released_claim_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.recheck,
        recheck_payload(blocker_active=False, human_gate_required=False, ready=True),
    )

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.RECHECK_TAMPERED.value in codes_of(failure)


def test_recheck_with_other_blocker_code_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.recheck, recheck_payload(blocker_code="PHASE3_4_SOMETHING"))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.RECHECK_TAMPERED.value in codes_of(failure)


def test_recheck_before_explicit_intake_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.recheck, recheck_payload(as_of=MOMENT + timedelta(minutes=30)))

    with pytest.raises(IntakeReceiptVerificationError) as failure:
        build_receipt(setup)
    assert ReceiptVerificationCode.RECHECK_BEFORE_INTAKE.value in codes_of(failure)


@pytest.mark.parametrize(
    "recheck_mutation",
    [
        pytest.param(lambda payload: payload.__setitem__("report", "other"), id="report"),
        pytest.param(lambda payload: payload.__setitem__("contract_version", "v9"), id="contract"),
        pytest.param(lambda payload: payload.__setitem__("blocker_active", "true"), id="flag_type"),
        pytest.param(lambda payload: payload.__setitem__("ready", "false"), id="ready_type"),
        pytest.param(lambda payload: payload.pop("gate"), id="gate_missing"),
        pytest.param(
            lambda payload: payload["gate"].pop("qualification_pass_count"), id="gate_key_missing"
        ),
        pytest.param(
            lambda payload: payload["gate"].__setitem__("readiness_blocked_scope_count", "x"),
            id="gate_bad_type",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("as_of", "2026-09-23T02:00:00"), id="naive_as_of"
        ),
    ],
)
def test_corrupt_recheck_is_fail_closed(
    tmp_path: Path, recheck_mutation: Callable[[dict[str, Any]], None]
) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.recheck, recheck_mutation)

    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)


def test_absent_recheck_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup, recheck=tmp_path / "absent_recheck.json")


def test_corrupt_ledger_and_approved_list_are_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    setup.ledger.write_text("{broken", encoding="utf-8")
    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)

    setup = ready_setup(tmp_path / "second")
    setup.approved.write_text("[]", encoding="utf-8")
    with pytest.raises(IntakeReceiptStateError):
        build_receipt(setup)


# ---------------------------------------------------------------------------
# 内容级 receipt_id / 幂等 / 原子写
# ---------------------------------------------------------------------------
def test_receipt_id_is_content_level_and_independent_of_audit_time(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    first = build_receipt(setup)
    later = build_receipt(setup, moment=RECEIPT_AT + timedelta(hours=5))

    assert first.receipt_id == later.receipt_id  # 内容级：与 receipt_at 无关
    assert first.to_dict()["receipt_at"] != later.to_dict()["receipt_at"]
    assert receipt_module.compute_receipt_id(first) == first.receipt_id


def test_repeated_identical_receipt_is_byte_stable_when_written(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    out = tmp_path / "receipt.json"

    first = build_receipt(setup, out_path=out)
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["receipt_id"] == first.receipt_id
    assert document["kind"] == INTAKE_RECEIPT_KIND
    assert document["written_path"] is None  # 文件内容里不带"写开关"的自述
    assert first.written_path is not None

    again = build_receipt(setup, out_path=out)
    assert again.receipt_id == first.receipt_id
    assert out.read_text(encoding="utf-8") == text  # 幂等：逐字节稳定
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


def test_changed_operator_result_produces_a_new_receipt(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    first = build_receipt(setup)

    write_json(
        setup.manifest,
        operator_manifest(setup.evidence, generated_at=INTAKE_AT + timedelta(minutes=30)),
    )
    second = build_receipt(setup)

    assert second.receipt_id != first.receipt_id  # 执行结果变化 → 新 receipt（绝不静默继承）
    assert second.operator_results[0].artifact_sha256 != first.operator_results[0].artifact_sha256


def test_changed_recheck_produces_a_new_receipt(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    first = build_receipt(setup)

    write_json(setup.recheck, recheck_payload(as_of=RECHECK_AT + timedelta(minutes=30)))
    second = build_receipt(setup)

    assert second.receipt_id != first.receipt_id


def test_many_approvals_never_change_safety_fields(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    build_package(setup.inbox, name="pkg-author-02", rows=[author_row("b-0001")])
    report = scan_inbox(setup.inbox, moment=MOMENT)
    target = next(item for item in report.packages if item.package_dir == "pkg-author-02")
    run_review(
        setup.inbox,
        moment=LATER,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        ledger_path=setup.ledger,
        out_path=setup.ledger,
        approved_out_path=setup.approved,
    )
    plan_path = write_plan(
        tmp_path, setup.inbox, setup.ledger, setup.approved, moment=LATER, name="plan2.json"
    )
    # 顺序必须成立：plan → 两条显式执行 → recheck（都在 plan 之后、收据之前）
    intake_at = LATER + timedelta(minutes=30)
    first_manifest = write_json(
        tmp_path / "manifest1.json",
        operator_manifest(setup.evidence, generated_at=intake_at),
    )
    second_manifest = write_json(
        tmp_path / "manifest2.json",
        operator_manifest(
            setup.inbox / "pkg-author-02" / EVIDENCE_NAME, generated_at=intake_at
        ),
    )
    fresh_recheck = write_json(
        tmp_path / "recheck2.json", recheck_payload(as_of=LATER + timedelta(hours=1))
    )

    receipt = run_intake_receipt(
        setup.inbox,
        **receipt_kwargs(
            setup,
            plan_path=plan_path,
            moment=LATER + timedelta(hours=2),
            recheck_path=fresh_recheck,
            operator_result_paths=(first_manifest, second_manifest),
        ),
    )

    assert receipt.approved_count == 2
    payload = receipt.to_dict()
    assert payload["blocker_active"] is True and payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["auto_intake_allowed"] is False and payload["writes_database"] is False


def test_intake_executed_never_implies_qualification(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.recheck, recheck_payload(ready=True, gate_overrides={
        "qualification_ready": True,
        "readiness_ready": True,
        "qualification_blocked_count": 0,
        "readiness_blocked_scope_count": 0,
    }))

    receipt = build_receipt(setup)
    payload = receipt.to_dict()

    # 复核结果可以说 "ready"，但收据**绝不**因此把 data qualification / Phase 置为 PASS
    assert receipt.recheck is not None and receipt.recheck.ready is True
    assert payload["intake_executed"] is True and payload["receipt_verified"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["qualification_recheck"]["is_qualification_decision"] is False
    assert payload["qualification_recheck"]["human_gate_required_for_phase_change"] is True


# ---------------------------------------------------------------------------
# 只读 / 原子写 / 锁 / 并发
# ---------------------------------------------------------------------------
def test_read_only_run_writes_nothing(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    plan_bytes = setup.plan.read_bytes()
    ledger_bytes = setup.ledger.read_bytes()
    manifest_bytes = setup.manifest.read_bytes()
    recheck_bytes = setup.recheck.read_bytes()
    evidence_bytes = setup.evidence.read_bytes()

    build_receipt(setup)

    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert setup.plan.read_bytes() == plan_bytes
    assert setup.ledger.read_bytes() == ledger_bytes
    assert setup.manifest.read_bytes() == manifest_bytes
    assert setup.recheck.read_bytes() == recheck_bytes
    assert setup.evidence.read_bytes() == evidence_bytes


def test_output_and_inputs_inside_inbox_are_rejected(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    with pytest.raises(IntakeReceiptPathError):
        build_receipt(setup, out_path=setup.inbox / "receipt.json")
    assert not (setup.inbox / "receipt.json").exists()
    with pytest.raises(IntakeReceiptPathError):
        run_intake_receipt(
            setup.inbox, **receipt_kwargs(setup, plan_path=setup.inbox / "plan.json")
        )


def test_write_failure_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    target = tmp_path / "receipt-dir"
    target.mkdir()

    with pytest.raises(IntakeReceiptWriteError):
        build_receipt(setup, out_path=target)


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    from src.evidence import SingleInstanceLock

    setup = ready_setup(tmp_path)
    out = tmp_path / "receipt.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")

    with held, pytest.raises(LockConflictError):
        build_receipt(setup, out_path=out)
    assert not out.exists()


def test_concurrent_writes_never_produce_partial_documents(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    out = tmp_path / "receipt.json"
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            build_receipt(setup, out_path=out)
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
    assert document["kind"] == INTAKE_RECEIPT_KIND
    assert document["receipt_id"] == build_receipt(setup).receipt_id
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# 公开核验 API / 脱敏 / 时间语义 / 摘要
# ---------------------------------------------------------------------------
def test_verify_intake_receipt_returns_codes_without_raising(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    document = load_intake_plan_document(setup.plan)
    ledger = receipt_module.load_review_ledger(setup.ledger)
    assert ledger is not None
    approved_list = receipt_module.load_approved_intake_list(setup.approved)
    scan = scan_inbox(setup.inbox, moment=RECEIPT_AT)
    manifest = load_operator_result(setup.manifest)
    recheck = load_qualification_recheck(setup.recheck)

    assert (
        verify_intake_receipt(
            document,
            scan,
            ledger,
            approved_list,
            moment=RECEIPT_AT,
            operator_results=(manifest,),
            recheck=recheck,
        )
        == ()
    )
    violations = verify_intake_receipt(
        document, scan, ledger, approved_list, moment=RECEIPT_AT, operator_results=(), recheck=None
    )
    codes = {item.code for item in violations}
    assert ReceiptVerificationCode.OPERATOR_RESULT_MISSING.value in codes
    assert ReceiptVerificationCode.RECHECK_MISSING.value in codes
    assert ReceiptVerificationCode.OPERATOR_COVERAGE_INCOMPLETE.value in codes


def _all_keys(payload: object) -> set[str]:
    """递归收集 JSON 结构里的所有键（用于断言"没有证据时间键"）。"""
    keys: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            keys.add(str(key))
            keys |= _all_keys(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            keys |= _all_keys(item)
    return keys


def test_receipt_never_carries_evidence_time_fields(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    payload = build_receipt(setup).to_dict()

    keys = _all_keys(payload)
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert field_name not in keys
    assert payload["evidence_time_semantics"]["contains_evidence_times"] is False
    assert payload["evidence_time_semantics"]["audit_times_only"]


def test_receipt_artifacts_are_redacted(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    # 包名里塞入凭据样式字符串：任何 artifact 都不得把它原样带出来
    package_dir = inbox / f"pkg-author-{SECRET}"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    document = manifest_payload(evidence)
    document["authorization_reference"] = "https://vendor.example/terms?v=2&operator=li"
    write_json(package_dir / MANIFEST_FILE_NAME, document)
    report = scan_inbox(inbox, moment=MOMENT)
    target = next(item for item in report.packages if item.package_dir == package_dir.name)
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger_path,
        approved_out_path=approved_path,
    )
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)
    manifest = write_json(tmp_path / "manifest.json", operator_manifest(evidence))
    recheck = write_json(tmp_path / "recheck.json", recheck_payload())

    receipt = run_intake_receipt(
        inbox,
        moment=RECEIPT_AT,
        plan_path=plan_path,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
        operator_result_paths=(manifest,),
        recheck_path=recheck,
    )

    text = json.dumps(receipt.to_dict(), ensure_ascii=False)
    assert SECRET not in text
    assert "?v=2" not in text
    assert "authorization_reference" not in text
    assert "author_name" not in text


def test_render_summary_keeps_blocker_and_human_gate_visible(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    receipt = build_receipt(setup)
    summary = render_intake_receipt_summary(receipt)

    assert "blocker_active" in summary
    assert "human_gate_required" in summary
    assert "data_qualification_passed" in summary
    assert "phase_transition_allowed" in summary
    assert "L3" in summary
    assert PHASE3_3_BLOCKER_CODE in summary
    assert receipt.receipt_id in summary


def test_render_summary_for_blocked_receipt_keeps_blocker_visible(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    run_review(inbox, moment=MOMENT, out_path=ledger_path, approved_out_path=approved_path)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)

    receipt = run_intake_receipt(
        inbox,
        moment=RECEIPT_AT,
        plan_path=plan_path,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
    )
    summary = render_intake_receipt_summary(receipt)

    assert IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE.value in summary
    assert "blocker_active" in summary and PHASE3_3_BLOCKER_CODE in summary


# ---------------------------------------------------------------------------
# 源码守卫 / CLI 编排
# ---------------------------------------------------------------------------
def test_module_has_no_network_database_or_intake_behaviour() -> None:
    source = Path(receipt_module.__file__).read_text(encoding="utf-8")
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
    # 绝不把操作时间当证据时间
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert f'"{field_name}":' not in source


def test_cli_module_exposes_no_intake_flag_and_requires_four_inputs() -> None:
    from scripts.evidence_intake_receipt import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as missing_plan:
        parser.parse_args(["--inbox-dir", "x", "--ledger", "y", "--approved-list", "z"])
    assert missing_plan.value.code == 2
    with pytest.raises(SystemExit) as missing_approved:
        parser.parse_args(["--inbox-dir", "x", "--ledger", "y", "--plan", "p"])
    assert missing_approved.value.code == 2
    with pytest.raises(SystemExit) as intake_flag:
        parser.parse_args(
            [
                "--inbox-dir",
                "x",
                "--ledger",
                "y",
                "--approved-list",
                "z",
                "--plan",
                "p",
                "--no-dry-run",
            ]
        )
    assert intake_flag.value.code == 2
    parsed = parser.parse_args(
        [
            "--inbox-dir",
            "x",
            "--ledger",
            "y",
            "--approved-list",
            "z",
            "--plan",
            "p",
            "--operator-result",
            "m1",
            "--operator-result",
            "m2",
            "--recheck",
            "r",
            "--json",
        ]
    )
    assert [str(item) for item in parsed.operator_results] == ["m1", "m2"]
    assert parsed.json_output is True
