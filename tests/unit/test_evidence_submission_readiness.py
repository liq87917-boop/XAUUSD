"""GOLD-033 人工证据提交就绪包单元测试（纯函数 / 零数据库 / 零网络 / tmp_path）。

覆盖：

- 材料缺失原因码与 GOLD-027 材料契约的一致性，以及版本化 schema 的只读语义；
- 空输入 / 本地候选包 / 合成包 / 部分缺失 / 真实材料结构完整 五种情形的诚实结论；
- ``engineering_ready`` / ``submission_materials_complete`` / ``human_verification_complete``
  与 ``data_qualification_passed`` / ``phase_transition_allowed`` / ``l3_gate_pending``
  **互相独立**（工程链全绿、材料齐全、人工核验完成都**不**解除 blocker）；
- rehearsal / fixture 输入恒保持 blocker，真实材料缺失**不会**被工程链全绿掩盖；
- 内容身份（``pack_id`` / ``facts_digest``）确定性且漂移必变；
- 绝不生成或推断证据时间（载荷中**没有**任何证据时间键）、默认零写入、``--out`` 只写本包本身、
  写进 inbox 被拒、源码级守卫（无网络 / 无写库）。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.alpha.evidence_gate import MIN_AUTHOR_SAMPLES, MIN_NEWS_EVENTS, MIN_NEWS_HISTORY_DAYS
from src.common import hashing
from src.evidence.chain_audit import (
    CHAIN_AUDIT_KIND,
    EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    audit_evidence_chain,
    run_chain_audit,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.handoff import thresholds
from src.evidence.human_verification_attestation import (
    attestable_material_keys,
    handoff_content_sha256,
    run_attestation,
    verify_attestation,
)
from src.evidence.inbox import MANIFEST_FILE_NAME, InboxDirError
from src.evidence.intake_handoff import IntakeStatus, load_intake_handoff
from src.evidence.ledger import EvidenceLedger, ledger_from_raw_json
from src.evidence.rehearsal import build_rehearsal_chain
from src.evidence.submission_readiness import (
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    MATERIAL_MISSING_REASON_CODES,
    SUBMISSION_PACK_KIND,
    SUBMISSION_PACK_SCHEMA_VERSION,
    SubmissionCode,
    SubmissionPackArgumentError,
    SubmissionPackError,
    SubmissionPackPathError,
    SubmissionPackStateError,
    SubmissionPackWriteError,
    build_submission_pack,
    chain_binding_from_audit,
    load_chain_binding,
    load_submission_pack,
    main_pack_exit_code,
    render_submission_pack_markdown,
    run_submission_pack,
    submission_pack_exit_code_for,
    submission_pack_schema,
)
from src.monitoring.evidence_readiness import build_readiness_report
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "evidence" / "submission_readiness.py"
CLI_PATH = REPO_ROOT / "scripts" / "evidence_submission_readiness.py"
#: 证据时间键：本包**绝不**生成 / 推断 / 填补（连键名都不出现）
FORBIDDEN_EVIDENCE_KEYS = ("published_at", "collected_at", "effective_at", "available_at")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")
#: 只读模块 / CLI **禁止** import 的根模块（网络 / 子进程 / 平台文件时间）
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {"aiohttp", "httpx", "requests", "socket", "urllib", "subprocess", "os", "shutil", "telnetlib"}
)
#: 只读模块 / CLI **禁止**出现的调用属性名（网络 / 文件时间 / 数据库写入 / 进程）
FORBIDDEN_CALL_ATTRS = frozenset(
    {
        "connect",
        "urlopen",
        "getmtime",
        "utime",
        "system",
        "popen",
        "commit",
        "flush",
        "add",
        "delete",
        "execute",
    }
)


# ---------------------------------------------------------------------------
# 纯本地 fixture：证据行 / 候选包 / readiness / 演练链
# ---------------------------------------------------------------------------
def evidence_block(
    scope: str,
    *,
    record_id: str,
    source: str = "evidence-source-a",
    available_at: str | None = None,
    oos: bool = True,
) -> dict[str, Any]:
    """构造一条 ``evidence-intake-v1`` 台账条目（纯内存，零数据库）。"""
    return {
        "evidence": {
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "scope": scope,
            "oos_eligible": oos,
            "available_at": available_at,
            "source": source,
            "source_record_id": record_id,
        }
    }


def empty_readiness() -> Any:
    """库内**无任何**经入口认证的证据（空台账；零数据库）。"""
    return build_readiness_report(EvidenceLedger.empty(), as_of=MOMENT)


def ready_readiness() -> Any:
    """库内量化门槛全部达标的只读 readiness（阈值仍来自既有 evidence_gate）。"""
    entries = [
        evidence_block(
            "author",
            record_id=f"a-{index:04d}",
            available_at=(WINDOW_START + timedelta(days=index)).isoformat(),
        )
        for index in range(MIN_AUTHOR_SAMPLES)
    ]
    entries += [
        evidence_block(
            "news",
            record_id=f"n-{index:04d}",
            source=f"evidence-source-{index % 3}",
            available_at=(
                WINDOW_START + timedelta(days=index % (MIN_NEWS_HISTORY_DAYS + 1))
            ).isoformat(),
        )
        for index in range(MIN_NEWS_EVENTS)
    ]
    return build_readiness_report(
        ledger_from_raw_json((entry, None) for entry in entries), as_of=MOMENT
    )


def news_row(
    record_id: str = "n-0001",
    *,
    synthetic: bool = False,
    availability_reference: str | None = "https://vendor.example/archive/2026-06-01",
) -> dict[str, Any]:
    """一条**完全合规**的本地 News 证据行（可切换为示例 / Mock 行）。"""
    row: dict[str, Any] = {
        "source": "evidence-source-a",
        "source_record_id": record_id,
        "title": "金价短线回落",
        "content": "亚洲盘金价自 2400 回落至 2385。",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    if availability_reference is not None:
        row["availability_reference"] = availability_reference
    if synthetic:
        row["record_kind"] = "example"
        row["is_mock"] = "true"
    return row


def author_row(record_id: str = "a-0001", *, synthetic: bool = False) -> dict[str, Any]:
    """一条**完全合规**的本地 Author 证据行（可切换为示例 / Mock 行）。"""
    row: dict[str, Any] = {
        "source": "evidence-source-author",
        "source_record_id": record_id,
        "author_name": "作者甲",
        "external_account_id": "acct-0001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350。",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "url": "https://vendor.example/posts/a-0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "authorization_valid_from": "2026-04-01T00:00:00+00:00",
        "authorization_expires_at": "2027-04-01T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    if synthetic:
        row["record_kind"] = "example"
        row["is_mock"] = "true"
    return row


def write_json(path: Path, payload: Any) -> Path:
    """写入确定性 JSON（仅测试夹具使用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def build_package(
    root: Path,
    name: str,
    *,
    evidence_type: str,
    rows: list[dict[str, Any]],
    file_name: str = "evidence.jsonl",
) -> Path:
    """在 ``root`` 下建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / file_name
    evidence.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": evidence_type,
        "source": "evidence-source-a",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": file_name,
                "sha256": hashing.sha256_bytes(evidence.read_bytes()),
                "format": "jsonl",
            }
        ],
    }
    write_json(package / MANIFEST_FILE_NAME, manifest)
    return package


def compliant_inbox(root: Path) -> Path:
    """建一个 Author + News 都**结构完整**的本地候选目录。"""
    inbox = root / "inbox"
    build_package(inbox, "pkg-author-0001", evidence_type="author", rows=[author_row()])
    build_package(inbox, "pkg-news-0001", evidence_type="news", rows=[news_row()])
    return inbox


def handoff_for(inbox: Path) -> Any:
    """GOLD-027 只读预检结论（当前候选目录）。"""
    return load_intake_handoff(inbox, as_of=MOMENT)


def rehearsal_binding(tmp_path: Path, *, scenario: str = "ready") -> tuple[Any, dict[str, Any]]:
    """在 ``tmp_path`` 内搭 fixture 链并返回（chain binding, 审计报告载荷）。"""
    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT, scenario=scenario)
    audit = audit_evidence_chain(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
    )
    return chain_binding_from_audit(audit), audit.to_dict()


def all_keys(payload: Any) -> set[str]:
    """递归收集所有 JSON 键名（用于"绝不出现证据时间键"断言）。"""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            found |= all_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= all_keys(item)
    return found


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内全部文件的相对路径 → 字节摘要（用于断言"什么都没被改写"）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _EmptyResult:
    """只读假结果集（永远为空）。"""

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    """只读假会话：不连数据库、不写任何东西。"""

    def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    def rollback(self) -> None:  # pragma: no cover - 仅用于对齐真实 Session 接口
        return None


# ---------------------------------------------------------------------------
# 契约 / 版本化 schema / 源码级只读守卫
# ---------------------------------------------------------------------------
def test_material_missing_reason_codes_track_gold027_contract() -> None:
    """材料缺失原因码与 GOLD-027 材料契约**完全对齐**（漂移即显式失败，绝不静默）。"""
    keys = attestable_material_keys()

    assert set(MATERIAL_MISSING_REASON_CODES) == set(keys)
    assert len(set(MATERIAL_MISSING_REASON_CODES.values())) == len(keys)
    assert all(code.startswith("SUBMISSION_") for code in MATERIAL_MISSING_REASON_CODES.values())


def test_schema_is_versioned_and_declares_read_only_semantics() -> None:
    """版本化提交契约：材料 / 原因码 / 阈值 / 语义全部**只引用**既有事实。"""
    schema = submission_pack_schema()

    assert schema["kind"] == SUBMISSION_PACK_KIND
    assert schema["schema_version"] == SUBMISSION_PACK_SCHEMA_VERSION
    assert schema["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert schema["thresholds"] == dict(sorted(thresholds().items()))
    assert set(schema["material_missing_reason_codes"]) == set(attestable_material_keys())
    assert [
        spec["key"] for spec in schema["materials"] if spec["category"] != "gate"
    ] == list(attestable_material_keys())
    assert set(schema["conclusions"]) == {
        "engineering_ready",
        "submission_materials_complete",
        "human_verification_complete",
        "data_qualification_passed",
        "phase_transition_allowed",
        "l3_gate_pending",
    }
    assert {"provide_real_materials", "complete_material_human_verification"} <= set(
        schema["human_action_keys"]
    )
    assert "l3_human_gate_decision" in schema["human_action_keys"]
    assert schema["time_semantics_requirements"]
    safety = schema["safety"]
    assert safety["data_qualification_passed"] is False
    assert safety["phase_transition_allowed"] is False
    assert safety["l3_l4_auto_advance_allowed"] is False
    assert safety["blocker_active"] is True
    assert safety["human_gate_required"] is True
    assert safety["writes_database"] is False
    assert safety["reads_network"] is False
    assert safety["executes_intake"] is False
    assert safety["generates_evidence_times"] is False


@pytest.mark.parametrize("path", [MODULE_PATH, CLI_PATH], ids=["module", "cli"])
def test_read_only_sources_have_no_network_or_db_write_calls(path: Path) -> None:
    """源码级守卫：模块与 CLI 都**没有**网络 / 文件时间 / 数据库写入调用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, (path.name, alias)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, (path.name, node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_CALL_ATTRS, (path.name, node.func.attr)


# ---------------------------------------------------------------------------
# 空输入：缺什么就报什么，且**绝不**生成证据时间
# ---------------------------------------------------------------------------
def test_empty_input_reports_every_material_missing_and_keeps_blocker() -> None:
    pack = build_submission_pack(empty_readiness(), moment=MOMENT)

    assert pack.kind == SUBMISSION_PACK_KIND
    assert pack.report == SUBMISSION_PACK_KIND
    assert pack.evidence_source == EVIDENCE_SOURCE_OPERATOR_PROVIDED
    assert pack.rehearsal is False
    assert [item.key for item in pack.materials] == list(attestable_material_keys())
    assert all(item.status is IntakeStatus.MISSING for item in pack.materials)
    assert pack.scope_section("author").status is IntakeStatus.MISSING
    assert pack.scope_section("news").status is IntakeStatus.MISSING
    assert pack.engineering_ready is False
    assert pack.submission_materials_complete is False
    assert pack.human_verification_complete is False
    assert pack.quantified_ready is False
    assert pack.nothing_open is False
    # 恒定安全字段：任何输入都无法把它们改成"通过"
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True
    assert pack.blocker_active is True
    assert pack.human_gate_required is True
    assert pack.gate_blocked is True
    assert pack.advance_allowed is False
    assert pack.l3_l4_auto_advance_allowed is False
    assert pack.evidence_qualified is False
    codes = set(pack.missing_reason_codes)
    assert {
        SubmissionCode.REAL_MATERIAL_MISSING.value,
        SubmissionCode.QUANTIFIED_GAP_OPEN.value,
        SubmissionCode.HUMAN_ATTESTATION_MISSING.value,
        SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value,
    } <= codes
    assert set(MATERIAL_MISSING_REASON_CODES.values()) <= codes
    assert SubmissionCode.REHEARSAL_INPUT_NON_QUALIFYING.value not in codes
    assert main_pack_exit_code(pack) == EXIT_BLOCKED
    payload = pack.to_dict()
    assert payload["schema_version"] == SUBMISSION_PACK_SCHEMA_VERSION
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["mock_or_fixture_counts_as_real_material"] is False
    assert payload["generates_evidence_times"] is False
    assert payload["generates_qualification"] is False
    assert payload["writes_database"] is False
    assert payload["reads_network"] is False
    assert payload["executes_intake"] is False


def test_human_actions_cover_materials_and_fixed_gate_steps() -> None:
    pack = build_submission_pack(empty_readiness(), moment=MOMENT)
    keys = [item.key for item in pack.human_actions]

    assert all(item.blocking is True for item in pack.human_actions)
    assert "provide_real_materials" in keys
    assert "complete_material_human_verification" in keys
    assert "verify_tool_chain_health" in keys
    assert keys[-1] == "l3_human_gate_decision"
    for material in pack.materials:
        assert f"provide_material:{material.key}" in keys
    assert pack.action("l3_human_gate_decision").evidence_reference.startswith(
        ".ai/DEVELOPMENT_PROTOCOL.md"
    )
    assert pack.l3_pending
    assert pack.thresholds
    assert pack.time_semantics_requirements


def test_payload_never_contains_evidence_time_keys(tmp_path: Path) -> None:
    """载荷里**没有**任何证据时间键；唯一时间戳是审计操作时间 ``generated_at``。"""
    pack = build_submission_pack(
        ready_readiness(), moment=MOMENT, handoff=handoff_for(compliant_inbox(tmp_path))
    )
    payload = pack.to_dict()

    assert all_keys(payload).isdisjoint(FORBIDDEN_EVIDENCE_KEYS)
    text = json.dumps(payload, ensure_ascii=False)
    assert set(_TIMESTAMP_RE.findall(text)) == {MOMENT.isoformat()}


def test_render_markdown_states_blocker_and_gaps() -> None:
    pack = build_submission_pack(empty_readiness(), moment=MOMENT)
    text = render_submission_pack_markdown(pack)

    assert "提交就绪包" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "data_qualification_passed" in text
    assert "l3_gate_pending" in text
    assert SubmissionCode.REAL_MATERIAL_MISSING.value in text
    assert "l3_human_gate_decision" in text


# ---------------------------------------------------------------------------
# rehearsal / fixture：工程链全绿也**绝不**解除 blocker、**绝不**掩盖真实材料缺失
# ---------------------------------------------------------------------------
def test_rehearsal_chain_green_keeps_everything_blocked(tmp_path: Path) -> None:
    binding, report = rehearsal_binding(tmp_path)
    pack = build_submission_pack(
        ready_readiness(),
        moment=MOMENT,
        chain=binding,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    )
    codes = set(pack.missing_reason_codes)

    # 工程链全绿**只**说明工具链契约一致
    assert report["kind"] == CHAIN_AUDIT_KIND
    assert binding.engineering_chain_ready is True
    assert pack.engineering_ready is True
    assert pack.quantified_ready is True
    # 但真实材料 / 人工核验**仍是空的**，数据资格与 Phase 切换**不变**
    assert pack.rehearsal is True
    assert pack.evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert pack.submission_materials_complete is False
    assert pack.human_verification_complete is False
    assert pack.nothing_open is False
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True
    assert pack.blocker_active is True
    assert pack.advance_allowed is False
    assert SubmissionCode.REHEARSAL_INPUT_NON_QUALIFYING.value in codes
    assert SubmissionCode.REAL_MATERIAL_MISSING.value in codes
    assert SubmissionCode.HUMAN_ATTESTATION_MISSING.value in codes
    assert SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value not in codes
    assert main_pack_exit_code(pack) == EXIT_BLOCKED
    assert "fixture" in render_submission_pack_markdown(pack)


def test_rehearsal_rejects_real_inputs(tmp_path: Path) -> None:
    binding, _ = rehearsal_binding(tmp_path)

    with pytest.raises(SubmissionPackArgumentError):
        build_submission_pack(
            empty_readiness(),
            moment=MOMENT,
            handoff=handoff_for(compliant_inbox(tmp_path)),
            chain=binding,
            evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        )
    with pytest.raises(SubmissionPackArgumentError):
        load_submission_pack(
            _EmptySession(),
            moment=MOMENT,
            inbox_dir=tmp_path,
            evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        )


def test_build_submission_pack_validates_arguments() -> None:
    with pytest.raises(SubmissionPackArgumentError):
        build_submission_pack(empty_readiness(), moment=datetime(2026, 9, 23))
    with pytest.raises(SubmissionPackArgumentError):
        build_submission_pack(empty_readiness(), moment=MOMENT, evidence_source="not_a_source")
    with pytest.raises(SubmissionPackArgumentError):
        load_submission_pack(_EmptySession(), moment=MOMENT, attestation_path=Path("missing.json"))


# ---------------------------------------------------------------------------
# GOLD-030 链绑定：内容身份一致即可复现，任何漂移 / 非法结构 fail-closed
# ---------------------------------------------------------------------------
def test_chain_binding_roundtrip_matches_audit(tmp_path: Path) -> None:
    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT)
    report = tmp_path / "phase33_evidence_chain_audit.json"
    audit = run_chain_audit(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
        out_path=report,
    )
    loaded = load_chain_binding(report)

    assert loaded.report_kind == CHAIN_AUDIT_KIND
    assert loaded.schema_version == 1
    assert loaded.chain_id == audit.chain_id
    assert loaded.facts_digest == audit.facts_digest
    assert loaded.engineering_chain_ready is True
    assert loaded.earliest_failure_stage is None
    assert loaded.rehearsal is True
    assert loaded.stage_statuses == chain_binding_from_audit(audit).stage_statuses
    assert len(loaded.stage_statuses) == 8
    assert loaded.passed_stage_count == 8
    assert loaded.failed_stage_count == 0
    assert loaded.to_dict()["evidence_source"] == EVIDENCE_SOURCE_REHEARSAL_FIXTURE


def test_load_chain_binding_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SubmissionPackPathError):
        load_chain_binding(tmp_path / "absent.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SubmissionPackStateError):
        load_chain_binding(broken)

    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT)
    audit = audit_evidence_chain(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
    )
    good = audit.to_dict()
    cases: dict[str, dict[str, Any]] = {
        "kind": {**good, "kind": "not_the_chain_audit"},
        "schema": {**good, "schema_version": 999},
        "source": {**good, "evidence_source": "made_up"},
        "identity": {**good, "chain_id": ""},
        "stages_missing": {**good, "stages": []},
        "stage_entry": {**good, "stages": [{"stage": "intake_handoff"}]},
    }
    for name, payload in cases.items():
        path = write_json(tmp_path / f"bad_{name}.json", payload)
        with pytest.raises(SubmissionPackStateError):
            load_chain_binding(path)


def test_chain_binding_failed_stage_is_reported(tmp_path: Path) -> None:
    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT)
    audit = audit_evidence_chain(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
    )
    payload = audit.to_dict()
    payload["engineering_chain_ready"] = False
    payload["earliest_failure_stage"] = "attestation"
    payload["stages"][1] = {**payload["stages"][1], "status": "FAIL", "passed": False}
    binding = load_chain_binding(write_json(tmp_path / "failed_chain.json", payload))

    pack = build_submission_pack(
        empty_readiness(), moment=MOMENT, chain=binding,
        evidence_source=EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    )

    assert pack.engineering_ready is False
    assert SubmissionCode.ENGINEERING_CHAIN_INCOMPLETE.value in set(pack.missing_reason_codes)
    assert SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value not in set(pack.missing_reason_codes)
    action = pack.action("verify_tool_chain_health")
    assert action.reason_code == SubmissionCode.ENGINEERING_CHAIN_INCOMPLETE.value
    assert "attestation" in action.requirement


# ---------------------------------------------------------------------------
# 材料级事实：Mock / 模板永不计资格；真实材料结构完整也**不**解除 blocker
# ---------------------------------------------------------------------------
def test_synthetic_only_candidates_never_count_as_real_material(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(
        inbox, "pkg-author-mock", evidence_type="author", rows=[author_row(synthetic=True)]
    )

    pack = build_submission_pack(ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox))
    author = pack.scope_section("author")
    material = pack.material("authorization_declaration")
    codes = set(pack.missing_reason_codes)

    assert pack.submission_materials_complete is False
    assert author.status is IntakeStatus.NON_QUALIFYING
    assert author.real_package_count == 0
    assert author.synthetic_package_count == 1
    assert material.status is IntakeStatus.NON_QUALIFYING
    assert material.reason_code == SubmissionCode.SYNTHETIC_MATERIAL_ONLY.value
    assert material.real_package_count == 0
    assert material.synthetic_package_count == 1
    assert material.to_dict()["counts_toward_eligibility"] is False
    assert SubmissionCode.SYNTHETIC_MATERIAL_ONLY.value in codes
    assert SubmissionCode.REAL_MATERIAL_MISSING.value in codes
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False


def test_complete_real_materials_still_keep_gate_blocked(tmp_path: Path) -> None:
    inbox = compliant_inbox(tmp_path)

    pack = build_submission_pack(ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox))
    codes = set(pack.missing_reason_codes)

    assert pack.submission_materials_complete is True
    assert pack.quantified_ready is True
    assert all(item.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED for item in pack.materials)
    assert [item.scope for item in pack.scopes] == ["author", "news"]
    assert SubmissionCode.REAL_MATERIAL_MISSING.value not in codes
    # 工程链 / 人工核验仍未就绪 → **绝不**放行
    assert pack.engineering_ready is False
    assert pack.human_verification_complete is False
    assert pack.nothing_open is False
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True
    assert pack.blocker_active is True
    assert SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value in codes
    assert SubmissionCode.HUMAN_ATTESTATION_MISSING.value in codes
    assert main_pack_exit_code(pack) == EXIT_BLOCKED


def test_mock_package_beside_real_package_is_not_counted(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, "pkg-author-0001", evidence_type="author", rows=[author_row()])
    build_package(
        inbox, "pkg-author-mock", evidence_type="author", rows=[author_row(synthetic=True)]
    )
    build_package(inbox, "pkg-news-0001", evidence_type="news", rows=[news_row()])

    pack = build_submission_pack(ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox))
    author = pack.scope_section("author")

    assert author.real_package_count == 1
    assert author.synthetic_package_count == 1
    assert author.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    assert pack.submission_materials_complete is True
    assert SubmissionCode.SYNTHETIC_MATERIAL_ONLY.value not in set(pack.missing_reason_codes)


def test_unverified_availability_material_is_reported_truthfully(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(
        inbox,
        "pkg-news-0001",
        evidence_type="news",
        rows=[news_row(availability_reference=None)],
    )

    pack = build_submission_pack(empty_readiness(), moment=MOMENT, handoff=handoff_for(inbox))
    material = pack.material("availability_oos")
    codes = set(pack.missing_reason_codes)

    assert material.status is IntakeStatus.PRESENT_UNVERIFIED
    assert material.reason_code == SubmissionCode.MATERIAL_PRESENT_UNVERIFIED.value
    assert material.real_package_count == 1
    assert material.synthetic_package_count == 0
    assert pack.scope_section("news").status is IntakeStatus.PRESENT_UNVERIFIED
    assert pack.submission_materials_complete is False
    assert SubmissionCode.MATERIAL_PRESENT_UNVERIFIED.value in codes
    assert SubmissionCode.REAL_MATERIAL_MISSING.value in codes
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False


def test_load_submission_pack_missing_inbox_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InboxDirError):
        load_submission_pack(_EmptySession(), moment=MOMENT, inbox_dir=tmp_path / "absent")


# ---------------------------------------------------------------------------
# GOLD-028 人工核验绑定：完成 ≠ 资格；漂移 → stale
# ---------------------------------------------------------------------------
def write_verification_input(
    inbox: Path, path: Path, *, reviewer: str = "operator-li"
) -> Path:
    """GOLD-028 人工核验输入（当前候选包 + 全部材料 VERIFIED；纯本地 fixture）。"""
    document = handoff_for(inbox)
    package = document.packages[0]
    reviewed_at = (MOMENT - timedelta(minutes=30)).isoformat()
    materials = [
        {
            "material": item.key,
            "decision": "VERIFIED",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "evidence_reference": "https://vendor.example/terms",
        }
        for item in package.materials
        if item.category != "gate"
    ]
    return write_json(
        path,
        {
            "schema_version": 1,
            "package_fingerprint": package.fingerprint,
            "scope": package.scope,
            "reviewer": reviewer,
            "handoff_content_sha256": handoff_content_sha256(document),
            "materials": materials,
        },
    )


def attest_author_package(tmp_path: Path) -> tuple[Path, Path]:
    """建一个 Author 候选包并生成 / 落盘一份 GOLD-028 凭证（返回 inbox, 凭证路径）。"""
    inbox = tmp_path / "inbox"
    build_package(inbox, "pkg-author-0001", evidence_type="author", rows=[author_row()])
    verification = write_verification_input(inbox, tmp_path / "verification.json")
    attestation_path = tmp_path / "phase33_human_verification_attestation.json"
    run_attestation(
        inbox,
        verification_path=verification,
        moment=MOMENT,
        package=handoff_for(inbox).packages[0].fingerprint,
        out_path=attestation_path,
    )
    return inbox, attestation_path


def test_attestation_completion_does_not_qualify_data(tmp_path: Path) -> None:
    inbox, attestation_path = attest_author_package(tmp_path)
    verified = verify_attestation(attestation_path, inbox, moment=MOMENT)

    assert verified.verified is True
    assert verified.all_required_verified is True

    pack = build_submission_pack(
        empty_readiness(),
        moment=MOMENT,
        handoff=handoff_for(inbox),
        attestation=verified,
    )
    codes = set(pack.missing_reason_codes)

    assert pack.human_verification_complete is True
    assert pack.submission_materials_complete is False  # 只有 Author，缺 News
    assert pack.nothing_open is False
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True
    assert pack.blocker_active is True
    assert SubmissionCode.HUMAN_ATTESTATION_MISSING.value not in codes
    assert SubmissionCode.ATTESTATION_STALE.value not in codes
    assert pack.attestation is not None
    assert pack.attestation["verified"] is True
    assert pack.attestation["all_required_verified"] is True


def test_attestation_drift_is_reported_as_stale(tmp_path: Path) -> None:
    inbox, attestation_path = attest_author_package(tmp_path)
    # 候选包内容漂移（追加一行）→ 旧凭证失效（绝不静默继承）
    evidence = inbox / "pkg-author-0001" / "evidence.jsonl"
    evidence.write_text(
        evidence.read_text(encoding="utf-8")
        + json.dumps(author_row("a-0002"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    drifted = verify_attestation(attestation_path, inbox, moment=MOMENT)
    assert drifted.verified is False
    assert drifted.codes

    pack = build_submission_pack(
        empty_readiness(),
        moment=MOMENT,
        handoff=handoff_for(inbox),
        attestation=drifted,
    )

    assert pack.human_verification_complete is False
    assert SubmissionCode.ATTESTATION_STALE.value in set(pack.missing_reason_codes)
    assert SubmissionCode.HUMAN_ATTESTATION_MISSING.value not in set(pack.missing_reason_codes)
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.action("complete_material_human_verification").reason_code == (
        SubmissionCode.ATTESTATION_STALE.value
    )


# ---------------------------------------------------------------------------
# 内容身份 / 退出码 / 默认零写入
# ---------------------------------------------------------------------------
def test_pack_identity_is_deterministic_and_drift_sensitive(tmp_path: Path) -> None:
    inbox_a = compliant_inbox(tmp_path / "a")
    inbox_b = compliant_inbox(tmp_path / "b")
    first = build_submission_pack(ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox_a))
    second = build_submission_pack(ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox_b))
    later = build_submission_pack(
        ready_readiness(), moment=MOMENT + timedelta(days=1), handoff=handoff_for(inbox_a)
    )

    assert len(first.pack_id) == 64
    assert len(first.facts_digest) == 64
    assert first.pack_id == second.pack_id
    assert first.facts_digest == second.facts_digest
    assert later.pack_id == first.pack_id  # 审计时点不参与内容身份
    assert later.generated_at != first.generated_at

    build_package(inbox_a, "pkg-news-mock", evidence_type="news", rows=[news_row(synthetic=True)])
    drifted = build_submission_pack(
        ready_readiness(), moment=MOMENT, handoff=handoff_for(inbox_a)
    )

    assert drifted.scope_section("news").synthetic_package_count == 1
    assert drifted.facts_digest != first.facts_digest
    assert drifted.pack_id != first.pack_id


def test_nothing_open_never_sets_qualification_flags() -> None:
    pack = replace(
        build_submission_pack(empty_readiness(), moment=MOMENT),
        engineering_ready=True,
        submission_materials_complete=True,
        human_verification_complete=True,
        quantified_ready=True,
    )

    assert pack.nothing_open is True
    assert main_pack_exit_code(pack) == EXIT_OK
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True
    assert pack.blocker_active is True
    assert pack.human_gate_required is True
    assert pack.gate_blocked is True
    assert pack.l3_l4_auto_advance_allowed is False
    payload = pack.to_dict()
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True


def test_exit_code_mapping_is_stable() -> None:
    assert submission_pack_exit_code_for(SubmissionPackArgumentError("x")) == EXIT_CONFIG_ERROR
    assert submission_pack_exit_code_for(SubmissionPackError("x")) == EXIT_STATE_INVALID
    assert submission_pack_exit_code_for(SubmissionPackStateError("x")) == EXIT_STATE_INVALID
    assert submission_pack_exit_code_for(SubmissionPackPathError("x")) == EXIT_UNUSABLE
    assert submission_pack_exit_code_for(SubmissionPackWriteError("x")) == EXIT_UNUSABLE
    assert submission_pack_exit_code_for(RuntimeError("x")) == EXIT_STATE_INVALID


def test_run_submission_pack_is_zero_write_by_default(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    pack = run_submission_pack(_EmptySession(), moment=MOMENT)

    assert list(workdir.iterdir()) == []
    assert pack.written_path is None
    assert pack.data_qualification_passed is False
    assert pack.phase_transition_allowed is False
    assert pack.l3_gate_pending is True


def test_run_submission_pack_only_writes_the_pack_itself(tmp_path: Path) -> None:
    inbox = compliant_inbox(tmp_path)
    before = file_snapshot(inbox)
    out = tmp_path / "reports" / "phase33_human_evidence_submission_pack.json"

    pack = run_submission_pack(_EmptySession(), moment=MOMENT, inbox_dir=inbox, out_path=out)
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert pack.written_path is not None
    assert payload["kind"] == SUBMISSION_PACK_KIND
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["blocker_active"] is True
    assert payload["submission_materials_complete"] is True
    assert payload["generates_evidence_times"] is False
    # 只写 out + 锁文件（同目录）；无 .tmp 残留；inbox 逐字节未变
    assert sorted(path.name for path in out.parent.iterdir()) == sorted(
        [out.name, out.name + ".lock"]
    )
    assert not list(tmp_path.rglob("*.tmp"))
    assert file_snapshot(inbox) == before


def test_run_submission_pack_refuses_out_inside_inbox(tmp_path: Path) -> None:
    inbox = compliant_inbox(tmp_path)
    before = file_snapshot(inbox)

    with pytest.raises(SubmissionPackPathError):
        run_submission_pack(
            _EmptySession(),
            moment=MOMENT,
            inbox_dir=inbox,
            out_path=inbox / "phase33_human_evidence_submission_pack.json",
        )

    assert file_snapshot(inbox) == before
