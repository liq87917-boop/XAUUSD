"""GOLD-015 L3 人工决策包单元测试（临时目录 / Mock 证据与审计产物 / 零数据库 / 零网络）。

覆盖：

- 退出码映射稳定；缺 handoff / plan / ledger / approved list / inbox：参数或路径错误；
- handoff 严格只读视图：文档标识 / schema / 契约版本 / 安全字段不可被削弱 / ``thresholds`` 必须
  等于**当前**唯一阈值来源 / 缺口算术自洽 / 禁止证据时间键 → 任一不合法即 fail-closed；
- readiness state 绑定：state 损坏（指纹校验失败）→ fail-closed；与 handoff **不同源** →
  ``READINESS_MISMATCH``；
- 聚合 fail-closed：plan stale / fingerprint drift / review override / 执行结果缺失或 dry-run /
  recheck 缺失或早于执行或结论不一致 / handoff stale → 逐项稳定原因码（沿用 GOLD-014 口径）；
- 可选 GOLD-014 收据文件的交叉核对：``receipt_id`` 不自洽 / ``plan_id`` / 指纹 /
  review revision / recheck 摘要不一致 → 稳定原因码；
- 五个布尔互不蕴含：``data_qualification_passed`` / ``phase_transition_allowed`` 恒 false，
  ``submit_to_l3_human_gate`` 只由三个独立事实合取；
- 内容级 ``packet_id`` 幂等 / byte-stable；关键输入变化 → 新 ``packet_id``；
- 默认只读：不写数据库、不写 inbox、不移动 / 改写原始 evidence；原子写 / 锁冲突；
- 脱敏与"操作时间不是证据时间"；Markdown 摘要保留 blocker / human Gate；源码守卫与 CLI 编排。
"""

from __future__ import annotations

import ast
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common import hashing
from src.evidence import (
    DECISION_PACKET_FILE_NAME,
    DECISION_PACKET_KIND,
    DECISION_PACKET_SCHEMA_VERSION,
    EVIDENCE_CONTRACT_VERSION,
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    FORBIDDEN_EVIDENCE_FIELDS,
    HANDOFF_REPORT_NAME,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    OPERATOR_MANIFEST_REPORT,
    PACKET_EXECUTION_MODE,
    QUALIFICATION_RECHECK_REPORT,
    DecisionPacketArgumentError,
    DecisionPacketPathError,
    DecisionPacketStateError,
    DecisionPacketStatus,
    DecisionPacketVerificationError,
    DecisionPacketWriteError,
    EvidenceLedger,
    EvidenceScope,
    LockConflictError,
    LockUnavailableError,
    PacketVerificationCode,
    ReviewReasonCode,
    SingleInstanceLock,
    build_handoff_report,
    compute_packet_id,
    compute_receipt_id,
    decision_packet_exit_code_for,
    ledger_from_raw_json,
    load_handoff_document,
    load_intake_receipt_document,
    load_readiness_state_document,
    render_decision_packet_summary,
    run_decision_packet,
    run_intake_plan,
    run_intake_receipt,
    run_review,
    scan_inbox,
    verify_decision_packet,
    write_snapshot_state,
)
from src.evidence import decision_packet as packet_module
from src.evidence.human_verification_attestation import run_attestation
from src.evidence.intake_handoff import load_intake_handoff
from src.monitoring import PHASE3_3_BLOCKER_CODE, build_readiness_report

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
INTAKE_AT = MOMENT + timedelta(hours=1)
RECHECK_AT = MOMENT + timedelta(hours=2)
HANDOFF_AT = MOMENT + timedelta(hours=3)
PACKET_AT = MOMENT + timedelta(hours=4)
FAILED_AT = MOMENT + timedelta(hours=6)
#: 证据的独立历史可用时间窗口（必须早于任何审计时点）
EVIDENCE_START = datetime(2026, 5, 1, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
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


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]], *, fmt: str = "jsonl") -> None:
    """把若干行写成 ``jsonl``（UTF-8；本测试只用 jsonl 口径）。"""
    assert fmt == "jsonl"
    path.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def manifest_payload(evidence_path: Path, *, evidence_name: str = EVIDENCE_NAME) -> dict[str, Any]:
    """构造最小合规 manifest（摘要取自**实际文件内容**）。"""
    return {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
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
                "format": "jsonl",
            }
        ],
    }


def build_package(root: Path, name: str = "pkg-author-01") -> Path:
    """在 ``root`` 下建一个合规候选包（manifest 摘要取自实际内容）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / EVIDENCE_NAME
    write_rows(evidence, [author_row()])
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest_payload(evidence), ensure_ascii=False), encoding="utf-8"
    )
    return package


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """把字典写成 JSON 文件（UTF-8、稳定排序）并返回路径。"""
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def tamper(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """就地篡改一个 JSON 文档（模拟 handoff / 收据被改写）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json(path, payload)


def operator_manifest(
    evidence: Path,
    *,
    scope: str = "author",
    dry_run: bool = False,
    persisted: int = 1,
    generated_at: datetime = INTAKE_AT,
) -> dict[str, Any]:
    """构造**格式合法**的 Evidence Operator 执行结果（manifest；与 GOLD-005 结构一致）。"""
    return {
        "schema_version": 1,
        "report": OPERATOR_MANIFEST_REPORT,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "scope": scope,
        "dry_run": dry_run,
        "generated_at": generated_at.isoformat(),
        "input": {
            "path": str(evidence),
            "format": "jsonl",
            "sha256": hashing.sha256_bytes(evidence.read_bytes()),
        },
        "counts": {
            "rows": 1,
            "accepted": persisted,
            "quarantined": 0,
            "duplicate": 0,
            "oos_eligible": persisted,
            "not_oos_eligible": 0,
            "persisted": persisted,
            "sources_created": 1 if persisted else 0,
            "processed_success": persisted,
        },
        "rows": [
            {
                "index": 0,
                "row_number": 1,
                "status": "ACCEPTED",
                "reason_codes": [],
                "reasons": [],
                "fingerprint": f"{0:064x}",
                "source": "vendor-author",
                "source_record_id": "a-0001",
                "oos_eligible": True,
                "not_oos_eligible_reason": None,
                "persisted": True,
            }
        ],
        "notes": [],
    }


def recheck_payload(
    *,
    as_of: datetime = RECHECK_AT,
    ready: bool = False,
    readiness_ready: bool = False,
    qualification_ready: bool = False,
    blocker_code: str = PHASE3_3_BLOCKER_CODE,
    blocker_active: bool = True,
    human_gate_required: bool = True,
) -> dict[str, Any]:
    """构造**格式合法**的 qualification recheck 产物（``phase33_qualification_recheck``）。"""
    return {
        "schema_version": 1,
        "report": QUALIFICATION_RECHECK_REPORT,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "as_of": as_of.isoformat(),
        "blocker_code": blocker_code,
        "blocker_active": blocker_active,
        "human_gate_required": human_gate_required,
        "ready": ready,
        "gate": {
            "qualification_ready": qualification_ready,
            "qualification_pass_count": 3 if qualification_ready else 0,
            "qualification_blocked_count": 0 if qualification_ready else 5,
            "readiness_ready": readiness_ready,
            "readiness_blocked_scope_count": 0 if readiness_ready else 2,
        },
        "readiness": {"scopes": []},
        "qualification": {"checks": []},
        "notes": [],
    }


# ---------------------------------------------------------------------------
# handoff / readiness state：用**真实生产函数**生成（Mock 仅用于测试控制流）
# ---------------------------------------------------------------------------
def evidence_entry(scope: str, source: str, available: datetime) -> tuple[dict[str, Any], None]:
    """一条「经证据入口认证且具备独立历史可用证据」的台账证据块（纯合成，仅供测试）。"""
    return (
        {
            "import_kind": "authorized_evidence_intake",
            "evidence": {
                "contract_version": EVIDENCE_CONTRACT_VERSION,
                "scope": scope,
                "source": source,
                "oos_eligible": True,
                "available_at": available.isoformat(),
            },
        },
        None,
    )


def ready_ledger() -> EvidenceLedger:
    """构造**恰好达标**的合成台账（Author 30 条 / News 210 条 / 覆盖 >90 天 / 单源 ~33%）。"""
    author_entries = [
        evidence_entry("author", "vendor-author", EVIDENCE_START + timedelta(days=index))
        for index in range(MIN_AUTHOR_SAMPLES)
    ]
    news_sources = ("news-src-a", "news-src-b", "news-src-c")
    news_count = MIN_NEWS_EVENTS + 10
    span_days = MIN_NEWS_HISTORY_DAYS + 110
    news_entries = [
        evidence_entry(
            "news",
            news_sources[index % len(news_sources)],
            EVIDENCE_START + timedelta(days=span_days * index / (news_count - 1)),
        )
        for index in range(news_count)
    ]
    return ledger_from_raw_json([*author_entries, *news_entries])


def handoff_payload(*, as_of: datetime = HANDOFF_AT, ready: bool = False) -> dict[str, Any]:
    """用**真实** producer（readiness → handoff）生成 handoff 报告载荷。"""
    ledger = ready_ledger() if ready else EvidenceLedger.empty()
    readiness = build_readiness_report(
        ledger, as_of=as_of, batches=(), scopes=(EvidenceScope.AUTHOR, EvidenceScope.NEWS)
    )
    handoff = build_handoff_report(readiness)
    assert handoff.ready_for_human_review is ready, "合成台账的 readiness 结论与预期不符"
    assert handoff.blocker_active is True
    assert handoff.human_gate_required is True
    assert handoff.data_qualification_passed is False
    assert handoff.phase_transition_allowed is False
    return handoff.to_dict()


def write_readiness_state(path: Path, handoff_path: Path) -> Path:
    """把「由该 handoff **重新推导**」的快照写成 GOLD-010 口径 state 文件（同源、含指纹）。"""
    document = load_handoff_document(handoff_path)
    write_snapshot_state(path, document.source)
    return path


# ---------------------------------------------------------------------------
# 链路夹具与调用助手
# ---------------------------------------------------------------------------
def write_attestation(
    inbox: Path,
    fingerprint: str,
    *,
    moment: datetime = MOMENT,
    suffix: str = "",
) -> Path:
    """为一个候选包生成合法 GOLD-028 材料级人工核验凭证（测试辅助；零网络）。"""
    handoff = load_intake_handoff(inbox, as_of=moment)
    package = next(item for item in handoff.packages if item.fingerprint == fingerprint)
    materials = [
        {
            "material": item.key,
            "decision": "VERIFIED",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": "operator-li",
            "reviewed_at": (moment - timedelta(hours=1)).isoformat(),
            "evidence_reference": "https://vendor.example/terms",
        }
        for item in package.materials
        if item.category != "gate"
    ]
    document = {
        "schema_version": 1,
        "package_fingerprint": package.fingerprint,
        "scope": package.scope,
        "reviewer": "operator-li",
        "materials": materials,
    }
    root = Path(inbox).parent
    verification = root / f"verification{suffix}.json"
    verification.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    out = root / f"phase33_human_verification_attestation{suffix}.json"
    run_attestation(
        inbox,
        verification_path=verification,
        moment=moment,
        package=package.fingerprint,
        out_path=out,
    )
    return out


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
        attestation_path=write_attestation(
            inbox, target.fingerprint, moment=moment, suffix=f"-{target.fingerprint[:8]}"
        ),
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
    """用 GOLD-013 真实入口生成 plan 文件（决策包核验的对象）。"""
    out = tmp_path / name
    run_intake_plan(
        inbox,
        moment=moment,
        ledger_path=ledger_path,
        approved_list_path=approved_path,
        out_path=out,
    )
    return out


def blocked_setup(tmp_path: Path, *, state_file: bool = False) -> SimpleNamespace:
    """没有任何批准的链路（inbox 有候选但 ledger / 清单为空；预期 BLOCKED）。"""
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger_path = tmp_path / "review_ledger.json"
    approved_path = tmp_path / "approved_for_intake.json"
    run_review(inbox, moment=MOMENT, out_path=ledger_path, approved_out_path=approved_path)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)
    handoff_path = write_json(tmp_path / "handoff.json", handoff_payload(ready=False))
    readiness_path = (
        write_readiness_state(tmp_path / "readiness_state.json", handoff_path)
        if state_file
        else None
    )
    return SimpleNamespace(
        inbox=inbox,
        package_dir=inbox / "pkg-author-01",
        evidence=inbox / "pkg-author-01" / EVIDENCE_NAME,
        ledger=ledger_path,
        approved=approved_path,
        plan=plan_path,
        handoff=handoff_path,
        readiness=readiness_path,
        manifest=None,
        recheck=None,
        receipt=None,
    )


def ready_setup(
    tmp_path: Path,
    *,
    ready: bool = True,
    state_file: bool = True,
    write_receipt: bool = False,
    manifest_overrides: Mapping[str, Any] | None = None,
) -> SimpleNamespace:
    """构造完整可核验链路：inbox → review → plan → 显式执行 → recheck → handoff（+ state）。"""
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    ledger_path, approved_path = approve_package(tmp_path, inbox)
    plan_path = write_plan(tmp_path, inbox, ledger_path, approved_path)
    evidence = package_dir / EVIDENCE_NAME
    payload = operator_manifest(evidence)
    payload.update(dict(manifest_overrides or {}))
    manifest_path = write_json(tmp_path / "operator_manifest.json", payload)
    recheck_path = write_json(
        tmp_path / "recheck.json",
        recheck_payload(ready=ready, readiness_ready=ready, qualification_ready=ready),
    )
    handoff_path = write_json(tmp_path / "handoff.json", handoff_payload(ready=ready))
    readiness_path = (
        write_readiness_state(tmp_path / "readiness_state.json", handoff_path)
        if state_file
        else None
    )
    receipt_path: Path | None = None
    if write_receipt:
        receipt_path = tmp_path / "evidence_intake_receipt.json"
        run_intake_receipt(
            inbox,
            moment=PACKET_AT,
            plan_path=plan_path,
            ledger_path=ledger_path,
            approved_list_path=approved_path,
            operator_result_paths=(manifest_path,),
            recheck_path=recheck_path,
            out_path=receipt_path,
        )
    return SimpleNamespace(
        inbox=inbox,
        package_dir=package_dir,
        evidence=evidence,
        ledger=ledger_path,
        approved=approved_path,
        plan=plan_path,
        manifest=manifest_path,
        recheck=recheck_path,
        handoff=handoff_path,
        readiness=readiness_path,
        receipt=receipt_path,
    )


def decision_kwargs(setup: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    """构造 ``run_decision_packet`` 的关键字参数（可覆盖；用于参数级失败测试）。"""
    kwargs: dict[str, Any] = {
        "moment": PACKET_AT,
        "handoff_path": setup.handoff,
        "plan_path": setup.plan,
        "ledger_path": setup.ledger,
        "approved_list_path": setup.approved,
        "readiness_path": setup.readiness,
        "operator_result_paths": (setup.manifest,) if setup.manifest is not None else (),
        "recheck_path": setup.recheck,
        "receipt_path": setup.receipt,
    }
    kwargs.update(overrides)
    return kwargs


def build_packet(setup: SimpleNamespace, **overrides: Any) -> Any:
    """按 setup 生成一次决策包（默认带上 setup 里的全部输入）。"""
    return run_decision_packet(setup.inbox, **decision_kwargs(setup, **overrides))


def codes_of(error: DecisionPacketVerificationError) -> list[str]:
    """取失败里的稳定原因码（排序；用于断言 fail-closed 的具体口径）。"""
    return sorted(item.code for item in error.violations)


def snapshot(root: Path) -> dict[str, str]:
    """记录目录内所有文件的相对路径与内容摘要（"什么都没被改写" 的断言用）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# 退出码 / 参数 / 路径
# ---------------------------------------------------------------------------
def test_exit_code_mapping_is_stable() -> None:
    assert decision_packet_exit_code_for(DecisionPacketArgumentError("x")) == EXIT_CONFIG_ERROR
    assert decision_packet_exit_code_for(DecisionPacketStateError("x")) == EXIT_STATE_INVALID
    assert (
        decision_packet_exit_code_for(DecisionPacketVerificationError(())) == EXIT_STATE_INVALID
    )
    assert decision_packet_exit_code_for(DecisionPacketPathError("x")) == EXIT_UNUSABLE
    assert decision_packet_exit_code_for(DecisionPacketWriteError("x")) == EXIT_UNUSABLE
    assert decision_packet_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert decision_packet_exit_code_for(LockUnavailableError("x")) == EXIT_UNUSABLE
    assert decision_packet_exit_code_for(RuntimeError("unknown")) == EXIT_STATE_INVALID


def test_missing_required_inputs_and_naive_moment_are_rejected(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    for key in ("handoff_path", "plan_path", "ledger_path", "approved_list_path"):
        with pytest.raises(DecisionPacketArgumentError):
            run_decision_packet(setup.inbox, **decision_kwargs(setup, **{key: None}))
    with pytest.raises(DecisionPacketArgumentError):
        run_decision_packet(setup.inbox, **decision_kwargs(setup, moment=datetime(2026, 9, 23)))


def test_missing_inbox_dir_is_a_path_error(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(DecisionPacketPathError):
        run_decision_packet(tmp_path / "absent-inbox", **decision_kwargs(setup))


def test_output_and_inputs_inside_inbox_are_rejected(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    with pytest.raises(DecisionPacketPathError):
        build_packet(setup, out_path=setup.inbox / "packet.json")
    with pytest.raises(DecisionPacketPathError):
        build_packet(setup, handoff_path=setup.inbox / "handoff.json")


# ---------------------------------------------------------------------------
# handoff 报告：严格只读视图
# ---------------------------------------------------------------------------
def handoff_mutations() -> list[tuple[str, Callable[[dict[str, Any]], None]]]:
    """handoff 的结构 / 安全字段 / 算术 / 阈值篡改方式（每一种都必须 fail-closed）。"""
    return [
        ("report", lambda payload: payload.__setitem__("report", "not_the_handoff")),
        ("schema_version", lambda payload: payload.__setitem__("schema_version", 99)),
        (
            "contract_version",
            lambda payload: payload.__setitem__("contract_version", "evidence-intake-v0"),
        ),
        ("blocker_released", lambda payload: payload.__setitem__("blocker_active", False)),
        (
            "human_gate_not_required",
            lambda payload: payload.__setitem__("human_gate_required", False),
        ),
        (
            "qualification_claimed",
            lambda payload: payload.__setitem__("data_qualification_passed", True),
        ),
        (
            "phase_transition_claimed",
            lambda payload: payload.__setitem__("phase_transition_allowed", True),
        ),
        ("blocker_code", lambda payload: payload.__setitem__("blocker_code", "OTHER_BLOCKER")),
        (
            "thresholds_relaxed",
            lambda payload: payload["thresholds"].__setitem__("author_min_eligible_records", 1.0),
        ),
        (
            "thresholds_missing_key",
            lambda payload: payload["thresholds"].pop("news_max_source_share"),
        ),
        (
            "ready_flag_inconsistent",
            lambda payload: payload.__setitem__("ready_for_human_review", True),
        ),
        ("status_inconsistent", lambda payload: payload.__setitem__("status", "PASS")),
        ("gap_missing_key", lambda payload: payload["author"].pop("remaining")),
        ("gap_arithmetic", lambda payload: payload["author"].__setitem__("remaining", 0)),
        ("gap_status", lambda payload: payload["author"].__setitem__("status", "PASS")),
        (
            "check_status",
            lambda payload: payload["author"]["checks"][0].__setitem__("status", "PASS"),
        ),
        (
            "check_remaining",
            lambda payload: payload["author"]["checks"][0].__setitem__("remaining", 0),
        ),
        (
            "check_comparator",
            lambda payload: payload["author"]["checks"][0].__setitem__("comparator", "=="),
        ),
        ("checks_not_array", lambda payload: payload["author"].__setitem__("checks", {})),
        (
            "quarantine_counts_not_array",
            lambda payload: payload.__setitem__("quarantine_reason_counts", {}),
        ),
        (
            "total_remaining_inconsistent",
            lambda payload: payload.__setitem__("total_remaining_gap_count", 0),
        ),
        ("missing_key", lambda payload: payload.pop("author")),
    ]


@pytest.mark.parametrize(
    ("label", "mutate"),
    handoff_mutations(),
    ids=[label for label, _ in handoff_mutations()],
)
def test_tampered_handoff_is_fail_closed(
    tmp_path: Path, label: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    setup = blocked_setup(tmp_path)
    tamper(setup.handoff, mutate)

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.HANDOFF_TAMPERED.value in str(failure.value), label


def test_handoff_with_evidence_time_key_is_fail_closed(tmp_path: Path) -> None:
    setup = blocked_setup(tmp_path)
    tamper(
        setup.handoff,
        lambda payload: payload.__setitem__("published_at", "2026-06-01T00:00:00+00:00"),
    )

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.EVIDENCE_TIME_SUBSTITUTION.value in str(failure.value)


def test_corrupt_or_absent_handoff_is_fail_closed(tmp_path: Path) -> None:
    setup = blocked_setup(tmp_path)
    setup.handoff.write_text("{broken", encoding="utf-8")

    with pytest.raises(DecisionPacketStateError):
        build_packet(setup)
    assert setup.handoff.read_text(encoding="utf-8") == "{broken"

    with pytest.raises(DecisionPacketStateError):
        run_decision_packet(
            setup.inbox, **decision_kwargs(setup, handoff_path=tmp_path / "absent-handoff.json")
        )


def test_load_handoff_document_exposes_readiness_source(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    document = load_handoff_document(setup.handoff)

    assert document.report.ready_for_human_review is True
    assert document.source.ready_for_human_review is True
    assert document.reason_codes == ()
    assert document.unmet_check_keys == ()
    assert len(document.artifact_sha256) == 64
    assert len(document.source.fingerprint) == 64
    payload = document.to_dict()
    assert payload["report"] == HANDOFF_REPORT_NAME
    assert payload["blocker_active"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert set(payload["thresholds"]) == {
        "author_min_eligible_records",
        "news_min_eligible_records",
        "news_min_history_days",
        "news_max_source_share",
    }


# ---------------------------------------------------------------------------
# readiness state 绑定（同源 / 指纹）
# ---------------------------------------------------------------------------
def test_readiness_state_must_be_same_source(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    other = write_json(tmp_path / "other_handoff.json", handoff_payload(ready=False))
    write_readiness_state(setup.readiness, other)  # 合法 state，但来自**另一批**内容

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert codes_of(failure.value) == [PacketVerificationCode.READINESS_MISMATCH.value]


def test_corrupt_readiness_state_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    setup.readiness.write_text("{broken", encoding="utf-8")

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.READINESS_TAMPERED.value in str(failure.value)
    assert setup.readiness.read_text(encoding="utf-8") == "{broken"


def test_missing_readiness_state_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    setup.readiness.unlink()

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.READINESS_TAMPERED.value in str(failure.value)


def test_readiness_state_is_optional_but_marked_when_absent(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    payload = build_packet(setup, readiness_path=None).to_dict()

    assert payload["readiness_binding"] == "NOT_PROVIDED"
    assert payload["readiness_state"] is None


# ---------------------------------------------------------------------------
# 完整链路：绑定成功 / 预期 BLOCKED
# ---------------------------------------------------------------------------
def test_verified_packet_binds_the_whole_chain(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    before = snapshot(tmp_path)

    packet = build_packet(setup)
    payload = packet.to_dict()

    assert packet.kind == DECISION_PACKET_KIND
    assert packet.status is DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE
    assert packet.submit_to_l3_human_gate is True
    assert packet.evidence_ready_for_human_review is True
    assert packet.receipt_verified is True
    assert packet.qualification_recheck_ready is True
    assert compute_packet_id(packet) == packet.packet_id
    assert len(packet.packet_id) == 64
    assert payload["schema_version"] == DECISION_PACKET_SCHEMA_VERSION
    assert payload["execution_mode"] == PACKET_EXECUTION_MODE
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["data_qualification_passed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["phase_transition_allowed"] is False
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["requires_explicit_operator_action"] is True
    assert payload["approved_entry_count"] == 1
    assert payload["landed_rows"] == 1
    assert payload["readiness_binding"] == "STATE_FILE_MATCHED"
    assert payload["receipt"]["receipt_file_provided"] is False
    assert payload["verification"]["violations"] == []
    assert payload["handoff"]["report"] == HANDOFF_REPORT_NAME
    assert payload["gaps"]["author"]["status"] == "PASS"
    assert payload["gaps"]["news"]["status"] == "PASS"
    assert payload["qualification_recheck"]["report"] == QUALIFICATION_RECHECK_REPORT
    assert payload["qualification_recheck"]["is_qualification_decision"] is False
    assert snapshot(tmp_path) == before  # 只读：什么都没写


def test_blocked_packet_reports_gaps_and_keeps_blocker(tmp_path: Path) -> None:
    setup = blocked_setup(tmp_path, state_file=True)
    before = snapshot(tmp_path)

    packet = build_packet(setup)
    payload = packet.to_dict()

    assert packet.status is DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE
    assert packet.submit_to_l3_human_gate is False
    assert packet.evidence_ready_for_human_review is False
    assert packet.receipt_verified is False
    assert packet.qualification_recheck_ready is False
    assert payload["approved_entry_count"] == 0
    assert len(payload["receipt"]["receipt_id"]) == 64  # 仍是内容寻址的「无批准」收据
    assert payload["receipt"]["status"] == "BLOCKED_NO_APPROVED_EVIDENCE"
    assert payload["gaps"]["author"]["remaining"] > 0
    assert payload["gaps"]["reason_codes"] != []
    assert payload["qualification_recheck"] is None
    assert payload["blocker_active"] is True
    assert payload["data_qualification_passed"] is False
    assert snapshot(tmp_path) == before


def test_three_facts_are_independent_and_never_imply_qualification(tmp_path: Path) -> None:
    """证据达标 + 收据可核验，但复核缺失 → 仍不可提交（三个独立事实互不蕴含）。"""
    setup = ready_setup(tmp_path)

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup, recheck_path=None, operator_result_paths=())

    codes = codes_of(failure.value)
    assert codes == ["OPERATOR_COVERAGE_INCOMPLETE", "OPERATOR_RESULT_MISSING", "RECHECK_MISSING"]
    assert "RECEIPT_TAMPERED" not in codes


# ---------------------------------------------------------------------------
# 证据链聚合失败（沿用 GOLD-014 的稳定原因码）
# ---------------------------------------------------------------------------
def test_dry_run_execution_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path, manifest_overrides={"dry_run": True})

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert "OPERATOR_NOT_EXECUTED" in codes_of(failure.value)


def test_zero_persisted_execution_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.manifest, lambda payload: payload["counts"].__setitem__("persisted", 0))

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert "OPERATOR_FAILED" in codes_of(failure.value)


def test_plan_stale_after_content_change_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_rows(setup.evidence, [author_row("a-0002")])

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert set(codes_of(failure.value)) & {"PLAN_STALE", "PLAN_ENTRY_MISMATCH"}


def test_tampered_plan_id_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    tamper(setup.plan, lambda payload: payload.__setitem__("plan_id", "0" * 64))

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert "PLAN_STALE" in codes_of(failure.value)


def test_missing_or_corrupt_plan_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    with pytest.raises(DecisionPacketStateError):
        build_packet(setup, plan_path=tmp_path / "absent-plan.json")

    setup.plan.write_text("{broken", encoding="utf-8")
    with pytest.raises(DecisionPacketStateError):
        build_packet(setup)
    assert setup.plan.read_text(encoding="utf-8") == "{broken"


def test_corrupt_ledger_and_approved_list_are_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    setup.ledger.write_text("{broken", encoding="utf-8")

    with pytest.raises(DecisionPacketStateError):
        build_packet(setup)

    other = ready_setup(tmp_path / "second")
    other.approved.write_text("{broken", encoding="utf-8")
    with pytest.raises(DecisionPacketStateError):
        build_packet(other)


def test_corrupt_operator_result_and_recheck_are_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    setup.manifest.write_text("{broken", encoding="utf-8")

    with pytest.raises(DecisionPacketStateError):
        build_packet(setup)

    other = ready_setup(tmp_path / "second")
    other.recheck.write_text("{broken", encoding="utf-8")
    with pytest.raises(DecisionPacketStateError):
        build_packet(other)


def test_recheck_before_explicit_intake_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(
        setup.recheck,
        recheck_payload(as_of=MOMENT, ready=True, readiness_ready=True, qualification_ready=True),
    )

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    codes = codes_of(failure.value)
    assert "RECHECK_BEFORE_INTAKE" in codes
    assert PacketVerificationCode.RECHECK_MISMATCH.value not in codes


def test_recheck_readiness_disagreement_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.recheck, recheck_payload(ready=False, readiness_ready=False))

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert codes_of(failure.value) == [PacketVerificationCode.RECHECK_MISMATCH.value]


def test_handoff_stale_after_explicit_intake_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    write_json(setup.handoff, handoff_payload(as_of=MOMENT, ready=True))

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert codes_of(failure.value) == [PacketVerificationCode.HANDOFF_STALE.value]


def test_future_timestamps_are_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup, moment=INTAKE_AT)

    assert "FUTURE_TIMESTAMP" in codes_of(failure.value)


def test_verify_decision_packet_returns_codes_without_raising(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    document = packet_module.load_intake_plan_document(setup.plan)
    ledger = packet_module.load_review_ledger(setup.ledger)
    assert ledger is not None
    approved = packet_module.load_approved_intake_list(setup.approved)
    scan = scan_inbox(setup.inbox, moment=PACKET_AT)
    manifest = packet_module.load_operator_result(setup.manifest)
    recheck = packet_module.load_qualification_recheck(setup.recheck)
    handoff = load_handoff_document(setup.handoff)
    readiness = load_readiness_state_document(setup.readiness)

    assert (
        verify_decision_packet(
            handoff,
            document,
            scan,
            ledger,
            approved,
            moment=PACKET_AT,
            readiness=readiness,
            operator_results=(manifest,),
            recheck=recheck,
        )
        == ()
    )
    violations = verify_decision_packet(
        handoff, document, scan, ledger, approved, moment=PACKET_AT, readiness=readiness
    )
    codes = {item.code for item in violations}
    assert codes == {"OPERATOR_RESULT_MISSING", "OPERATOR_COVERAGE_INCOMPLETE", "RECHECK_MISSING"}
    assert all(item.to_dict()["code"] == item.code for item in violations)


# ---------------------------------------------------------------------------
# 可选 GOLD-014 收据文件：内容寻址交叉核对
# ---------------------------------------------------------------------------
def resign_receipt(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """按**真实**内容寻址口径重算 ``receipt_id``（模拟"被改写并重签"的旧收据）。"""
    tamper(path, mutate)
    document = load_intake_receipt_document(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["receipt_id"] = compute_receipt_id(document.receipt)
    write_json(path, payload)


def test_binding_receipt_file_is_cross_checked(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path, write_receipt=True)

    packet = build_packet(setup)
    payload = packet.to_dict()
    declared = json.loads(setup.receipt.read_text(encoding="utf-8"))

    assert payload["receipt"]["receipt_file_provided"] is True
    assert payload["receipt"]["receipt_file_matched"] is True
    assert payload["receipt"]["receipt_id"] == declared["receipt_id"]
    assert payload["receipt"]["artifact_sha256"] == hashing.sha256_bytes(
        setup.receipt.read_bytes()
    )
    assert packet.receipt_verified is True
    assert packet.status is DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE


def receipt_mutations() -> list[tuple[str, str, Callable[[dict[str, Any]], None]]]:
    """(label, 期望原因码, 改写方式) —— 重签 ``receipt_id`` 之后仍必须 fail-closed。"""
    other_as_of = (MOMENT + timedelta(minutes=90)).isoformat()
    return [
        (
            "content_sha256",
            PacketVerificationCode.FINGERPRINT_MISMATCH.value,
            lambda payload: payload["executed_entries"][0].__setitem__(
                "content_sha256", ["0" * 64]
            ),
        ),
        (
            "review_revision",
            PacketVerificationCode.REVIEW_REVISION_MISMATCH.value,
            lambda payload: payload["executed_entries"][0].__setitem__("revision", 99),
        ),
        (
            "plan_id",
            PacketVerificationCode.PLAN_ID_MISMATCH.value,
            lambda payload: (
                payload.__setitem__("plan_id", "0" * 64),
                payload["plan"].__setitem__("plan_id", "0" * 64),
            ),
        ),
        (
            "operator_counts",
            PacketVerificationCode.RECEIPT_MISMATCH.value,
            lambda payload: payload["operator_results"][0]["counts"].__setitem__("persisted", 2),
        ),
        (
            "recheck_as_of",
            PacketVerificationCode.RECHECK_MISMATCH.value,
            lambda payload: payload["qualification_recheck"].__setitem__("as_of", other_as_of),
        ),
    ]


@pytest.mark.parametrize(
    ("label", "expected", "mutate"),
    receipt_mutations(),
    ids=[label for label, _, _ in receipt_mutations()],
)
def test_modified_receipt_file_is_fail_closed(
    tmp_path: Path, label: str, expected: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    setup = ready_setup(tmp_path, write_receipt=True)
    resign_receipt(setup.receipt, mutate)

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert codes_of(failure.value) == [expected], label


def test_receipt_id_tamper_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path, write_receipt=True)
    tamper(setup.receipt, lambda payload: payload.__setitem__("receipt_id", "0" * 64))

    with pytest.raises(DecisionPacketVerificationError) as failure:
        build_packet(setup)

    assert codes_of(failure.value) == [PacketVerificationCode.RECEIPT_ID_MISMATCH.value]


def test_corrupt_receipt_file_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path, write_receipt=True)
    setup.receipt.write_text("{broken", encoding="utf-8")

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.RECEIPT_TAMPERED.value in str(failure.value)
    assert setup.receipt.read_text(encoding="utf-8") == "{broken"


def test_receipt_with_weakened_safety_fields_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path, write_receipt=True)
    tamper(
        setup.receipt,
        lambda payload: payload.__setitem__("data_qualification_passed", True),
    )

    with pytest.raises(DecisionPacketStateError) as failure:
        build_packet(setup)

    assert PacketVerificationCode.RECEIPT_TAMPERED.value in str(failure.value)


# ---------------------------------------------------------------------------
# 幂等 / 原子写 / 锁 / 只读 / CLI 退出码
# ---------------------------------------------------------------------------
def cli_argv(setup: SimpleNamespace, *extra: str) -> list[str]:
    """把 setup 转成 CLI 参数（与 ``run_decision_packet`` 同口径）。"""
    argv = [
        "--handoff",
        str(setup.handoff),
        "--inbox-dir",
        str(setup.inbox),
        "--ledger",
        str(setup.ledger),
        "--approved-list",
        str(setup.approved),
        "--plan",
        str(setup.plan),
    ]
    if setup.readiness is not None:
        argv += ["--readiness", str(setup.readiness)]
    if setup.receipt is not None:
        argv += ["--receipt", str(setup.receipt)]
    if setup.manifest is not None:
        argv += ["--operator-result", str(setup.manifest)]
    if setup.recheck is not None:
        argv += ["--recheck", str(setup.recheck)]
    argv += list(extra)
    return argv


def test_packet_id_is_content_level_and_independent_of_audit_time(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    first = build_packet(setup)
    second = build_packet(setup, moment=PACKET_AT + timedelta(hours=1))

    assert first.packet_id == second.packet_id
    assert first.generated_at != second.generated_at


def test_repeated_identical_packet_is_byte_stable_when_written(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    out = tmp_path / DECISION_PACKET_FILE_NAME

    packet = build_packet(setup, out_path=out)
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)

    assert document["kind"] == DECISION_PACKET_KIND
    assert document["packet_id"] == packet.packet_id
    assert document["written_path"] is None

    build_packet(setup, out_path=out)
    assert out.read_text(encoding="utf-8") == text
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


def test_changed_inputs_produce_a_new_packet_id(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    original = build_packet(setup).packet_id

    tamper(
        setup.manifest,
        lambda payload: payload.__setitem__(
            "generated_at", (INTAKE_AT + timedelta(minutes=30)).isoformat()
        ),
    )
    changed = build_packet(setup)

    assert changed.packet_id != original
    assert changed.receipt_verified is True
    assert changed.qualification_recheck_ready is True


def test_read_only_run_writes_nothing(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    before = snapshot(tmp_path)
    evidence = setup.evidence.read_bytes()

    packet = build_packet(setup)

    assert packet.written_path is None
    assert snapshot(tmp_path) == before
    assert setup.evidence.read_bytes() == evidence  # 原始 evidence 未被移动 / 改写


def test_write_failure_is_fail_closed(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(DecisionPacketWriteError):
        build_packet(
            setup,
            out_path=blocker / "decision_packet.json",
            lock_path=tmp_path / "packet.lock",
        )


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    out = tmp_path / "decision_packet.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")

    with held, pytest.raises(LockConflictError):
        build_packet(setup, out_path=out)

    assert not out.exists()


def test_concurrent_writes_never_produce_partial_documents(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    out = tmp_path / "decision_packet.json"
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            build_packet(setup, out_path=out)
        except LockConflictError:  # 并发时另一线程持锁 → fail-closed（可接受）
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
    assert document["kind"] == DECISION_PACKET_KIND
    assert document["packet_id"] == build_packet(setup).packet_id
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


def test_cli_run_returns_expected_exit_codes(tmp_path: Path) -> None:
    from scripts.evidence_decision_packet import main as cli_main

    setup = ready_setup(tmp_path)
    assert cli_main([*cli_argv(setup, "--json")], moment=PACKET_AT) == EXIT_OK

    blocked = blocked_setup(tmp_path / "blocked")
    assert cli_main([*cli_argv(blocked, "--json")], moment=PACKET_AT) == EXIT_BLOCKED


# ---------------------------------------------------------------------------
# 脱敏 / 时间语义 / 摘要
# ---------------------------------------------------------------------------
def all_keys(payload: object) -> set[str]:
    """递归收集 JSON 结构里的所有键（用于断言"没有证据时间键"）。"""
    keys: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            keys.add(str(key))
            keys |= all_keys(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            keys |= all_keys(item)
    return keys


def test_packet_never_carries_evidence_time_fields(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    payload = build_packet(setup).to_dict()

    keys = all_keys(payload)
    for field_name in FORBIDDEN_EVIDENCE_FIELDS:
        assert field_name not in keys
    semantics = payload["evidence_time_semantics"]
    assert semantics["contains_evidence_times"] is False
    assert "generated_at" in semantics["audit_times_only"]
    assert "handoff.as_of" in semantics["audit_times_only"]


def test_packet_artifacts_are_redacted(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)
    secret_handoff = tmp_path / f"{SECRET}.json"
    secret_handoff.write_text(setup.handoff.read_text(encoding="utf-8"), encoding="utf-8")

    payload = build_packet(setup, handoff_path=secret_handoff).to_dict()
    text = json.dumps(payload, ensure_ascii=False)

    assert SECRET not in text
    assert "***" in text


def test_render_summary_keeps_blocker_and_human_gate_visible(tmp_path: Path) -> None:
    setup = ready_setup(tmp_path)

    text = render_decision_packet_summary(build_packet(setup))

    assert "blocker_active" in text
    assert "human_gate_required" in text
    assert "data_qualification_passed" in text
    assert "phase_transition_allowed" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "L3" in text
    assert DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE.value in text
    assert "submit_to_l3_human_gate" in text


def test_render_summary_for_blocked_packet_keeps_blocker_visible(tmp_path: Path) -> None:
    setup = blocked_setup(tmp_path)

    text = render_decision_packet_summary(build_packet(setup))

    assert DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE.value in text
    assert "blocker_active" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "BLOCKED" in text


# ---------------------------------------------------------------------------
# 源码守卫 / CLI 编排
# ---------------------------------------------------------------------------
def test_module_has_no_network_database_or_intake_behaviour() -> None:
    source = Path(packet_module.__file__).read_text(encoding="utf-8")
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
    # 安全字段与执行模式硬编码
    assert PACKET_EXECUTION_MODE in source
    assert '"human_gate_level": "L3"' in source


def test_cli_module_exposes_no_intake_flag_and_requires_five_inputs() -> None:
    from scripts.evidence_decision_packet import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as missing_plan:
        parser.parse_args(
            ["--handoff", "h", "--inbox-dir", "i", "--ledger", "l", "--approved-list", "a"]
        )
    assert missing_plan.value.code == 2
    with pytest.raises(SystemExit) as missing_handoff:
        parser.parse_args(
            ["--inbox-dir", "i", "--ledger", "l", "--approved-list", "a", "--plan", "p"]
        )
    assert missing_handoff.value.code == 2
    with pytest.raises(SystemExit) as intake_flag:
        parser.parse_args(
            [
                "--handoff",
                "h",
                "--inbox-dir",
                "i",
                "--ledger",
                "l",
                "--approved-list",
                "a",
                "--plan",
                "p",
                "--no-dry-run",
            ]
        )
    assert intake_flag.value.code == 2
    parsed = parser.parse_args(
        [
            "--handoff",
            "h",
            "--inbox-dir",
            "i",
            "--ledger",
            "l",
            "--approved-list",
            "a",
            "--plan",
            "p",
            "--readiness",
            "s",
            "--receipt",
            "c",
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
    assert parsed.out is None
    assert str(parsed.readiness) == "s"
    assert str(parsed.receipt) == "c"