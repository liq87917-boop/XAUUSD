"""GOLD-030 Phase 3.3 证据链端到端只读审计 / 演练单元测试（零网络 / 零数据库 / tmp_path）。

覆盖：

- 演练 happy path（``ready``）与诚实 BLOCKED（``blocked``）场景：8 段全 PASS；
- ``chain_id`` / ``facts_digest`` 的确定性与"漂移必变"；
- 每一关键绑定点的 tamper / stale / missing 场景 → **稳定原因码 + 最早失败阶段**；
- 四态结论：``engineering_chain_ready`` / ``real_evidence_missing`` /
  ``human_verification_missing`` / ``l3_human_gate_pending`` 的诚实语义；
- 安全字段**硬编码**（工程链全绿也不解除 blocker / 不通过资格）；
- 审计对输入 artifact **零改写**、``--out`` 只写报告本身、写进 inbox 被拒；
- 演练工作目录**不得**污染仓库；源码级守卫：审计 / 演练模块不触碰数据库与网络。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence.chain_audit import (
    CHAIN_AUDIT_KIND,
    CHAIN_AUDIT_SCHEMA_VERSION,
    CHAIN_STAGE_ORDER,
    EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    EXIT_BLOCKED,
    EXIT_OK,
    ChainAuditArgumentError,
    ChainAuditCode,
    ChainAuditInputs,
    ChainAuditPathError,
    ChainStage,
    ChainStageStatus,
    EvidenceChainAudit,
    audit_evidence_chain,
    compute_chain_facts_digest,
    compute_chain_id,
    main_audit_exit_code,
    render_chain_audit_summary,
    run_chain_audit,
)
from src.evidence.rehearsal import (
    REHEARSAL_SCENARIOS,
    RehearsalArgumentError,
    RehearsalChain,
    RehearsalPathError,
    RehearsalScenario,
    build_rehearsal_chain,
    ensure_rehearsal_work_dir,
)

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SOURCE_MODULES = (
    REPO_ROOT / "src" / "evidence" / "chain_audit.py",
    REPO_ROOT / "src" / "evidence" / "rehearsal.py",
)


def build_chain(tmp_path: Path, *, scenario: str = "ready") -> RehearsalChain:
    """在 ``tmp_path`` 内搭起演练链 fixture（零网络 / 零数据库）。"""
    return build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT, scenario=scenario)


def audit_chain(chain: RehearsalChain) -> EvidenceChainAudit:
    """对演练链做一次只读审计（``evidence_source=rehearsal_fixture``）。"""
    return audit_evidence_chain(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
    )


def codes_of(audit: EvidenceChainAudit, stage: ChainStage) -> tuple[str, ...]:
    """取某段的原因码（未收录返回空）。"""
    item = audit.stage(stage)
    return () if item is None else item.reason_codes


def status_of(audit: EvidenceChainAudit, stage: ChainStage) -> ChainStageStatus | None:
    """取某段的状态。"""
    return audit.stage_status(stage)


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内全部文件的相对路径 → 字节摘要（用于断言"什么都没被改写"）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def edit_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """原地改写一个 JSON 文档（仅用于测试篡改场景）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_stage_order_is_locked() -> None:
    """阶段顺序 / 数量是**契约**：新增或重排必须显式改测试（防静默漂移）。"""
    assert [stage.value for stage in CHAIN_STAGE_ORDER] == [
        "intake_handoff",
        "attestation",
        "review_approval",
        "approved_list_and_plan",
        "operator_result",
        "intake_receipt",
        "decision_packet",
        "decision_record",
    ]
    assert tuple(ChainStage) == CHAIN_STAGE_ORDER
    assert REHEARSAL_SCENARIOS == ("ready", "blocked")



# ---------------------------------------------------------------------------
# happy path（ready / blocked）
# ---------------------------------------------------------------------------
def test_rehearsal_ready_happy_path_all_stages_pass(tmp_path: Path) -> None:
    """happy path：整条链逐段一致，但**绝不**代表资格通过或 L3 批准。"""
    chain = build_chain(tmp_path, scenario=RehearsalScenario.READY.value)
    audit = audit_chain(chain)

    assert audit.kind == CHAIN_AUDIT_KIND
    assert audit.evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert audit.scenario == "ready"
    assert audit.rehearsal is True
    assert audit.engineering_chain_ready is True
    assert audit.earliest_failure_stage is None
    assert audit.passed_stage_count == len(CHAIN_STAGE_ORDER)
    assert audit.failed_stage_count == 0
    assert [item.status for item in audit.stages] == [ChainStageStatus.PASS] * 8
    assert all(item.reason_codes == (ChainAuditCode.STAGE_OK.value,) for item in audit.stages)
    # 四个结论：工程链健康，但真实证据 / 人工核验 / L3 Gate 都还没完成
    assert audit.real_evidence_missing is True
    assert audit.human_verification_missing is True
    assert audit.l3_human_gate_pending is True
    # 安全字段硬编码：工程链全绿也不放行
    assert audit.data_qualification_passed is False
    assert audit.phase_transition_allowed is False
    assert audit.blocker_active is True
    assert audit.human_gate_required is True
    assert audit.gate_blocked is True
    assert audit.advance_allowed is False
    assert audit.evidence_qualified is False
    assert audit.l3_l4_auto_advance_allowed is False

    packet = audit.stage(ChainStage.DECISION_PACKET)
    assert packet is not None
    assert packet.fact("submit_to_l3_human_gate") == "true"
    assert packet.fact("packet_status") == "READY_FOR_L3_HUMAN_GATE"
    record = audit.stage(ChainStage.DECISION_RECORD)
    assert record is not None
    assert record.fact("decision") == "approve"
    assert record.fact("approve_still_valid") == "true"

    payload = audit.to_dict()
    assert payload["schema_version"] == CHAIN_AUDIT_SCHEMA_VERSION
    assert payload["evidence_source"] == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert payload["writes_database"] is False
    assert payload["reads_network"] is False
    assert payload["executes_intake"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_human_gate_pending"] is True
    assert payload["earliest_failure_stage"] is None
    assert len(payload["stages"]) == 8
    assert main_audit_exit_code(audit) == EXIT_OK


def test_rehearsal_blocked_scenario_stays_consistent(tmp_path: Path) -> None:
    """blocked 场景：packet 不可提交 + ``reject`` 记录，工程链仍逐段一致。"""
    chain = build_chain(tmp_path, scenario=RehearsalScenario.BLOCKED.value)
    assert chain.record_decision == "reject"
    audit = audit_chain(chain)

    assert audit.engineering_chain_ready is True
    assert audit.earliest_failure_stage is None
    packet = audit.stage(ChainStage.DECISION_PACKET)
    assert packet is not None
    assert packet.fact("packet_status") == "BLOCKED_PENDING_EVIDENCE"
    assert packet.fact("submit_to_l3_human_gate") == "false"
    assert packet.fact("evidence_ready_for_human_review") == "false"
    record = audit.stage(ChainStage.DECISION_RECORD)
    assert record is not None
    assert record.fact("decision") == "reject"
    assert record.fact("approve_still_valid") == "none"
    assert audit.data_qualification_passed is False
    assert audit.real_evidence_missing is True


def test_chain_id_and_facts_digest_are_deterministic(tmp_path: Path) -> None:
    """同一 artifact 内容重复审计 → 同一 ``chain_id`` / ``facts_digest``。"""
    chain = build_chain(tmp_path)
    first = audit_chain(chain)
    second = audit_chain(chain)

    assert first.chain_id == second.chain_id
    assert first.facts_digest == second.facts_digest
    assert len(first.chain_id) == 64
    assert len(first.facts_digest) == 64
    assert first.chain_id == compute_chain_id(
        stages=first.stages,
        facts_digest=first.facts_digest,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario="ready",
    )
    assert first.facts_digest == compute_chain_facts_digest(first.stages)
    assert first.chain_id != compute_chain_id(
        stages=first.stages,
        facts_digest=first.facts_digest,
        evidence_source=EVIDENCE_SOURCE_OPERATOR_PROVIDED,
        scenario="ready",
    )


def test_every_stage_carries_content_identity_facts(tmp_path: Path) -> None:
    """每段都必须给出可核对的内容身份事实（指纹 / 摘要 / id / 计数）。"""
    audit = audit_chain(build_chain(tmp_path))

    assert audit.stage(ChainStage.INTAKE_HANDOFF).fact("handoff_content_sha256")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.ATTESTATION).fact("attestation_id")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.REVIEW_APPROVAL).fact("approved[1].decision_id")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.APPROVED_LIST_AND_PLAN).fact("plan_id")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.OPERATOR_RESULT).fact("operator[1].input_sha256")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.INTAKE_RECEIPT).fact("receipt_id")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.DECISION_PACKET).fact("packet_id")  # type: ignore[union-attr]
    assert audit.stage(ChainStage.DECISION_RECORD).fact("record_id")  # type: ignore[union-attr]



# ---------------------------------------------------------------------------
# 每一关键绑定点的 tamper / stale / missing
# ---------------------------------------------------------------------------
def _assert_single_failure(
    audit: EvidenceChainAudit, stage: ChainStage, *, must_include: str = ""
) -> tuple[str, ...]:
    """断言：**该段**是唯一失败（此前全 PASS），其后全部 ``NOT_EVALUATED``。"""
    order = list(CHAIN_STAGE_ORDER)
    position = order.index(stage)
    for earlier in order[:position]:
        assert status_of(audit, earlier) is ChainStageStatus.PASS, earlier
    assert status_of(audit, stage) is not ChainStageStatus.PASS
    assert audit.earliest_failure_stage is stage
    for later in order[position + 1 :]:
        assert status_of(audit, later) is ChainStageStatus.NOT_EVALUATED, later
        assert codes_of(audit, later) == (ChainAuditCode.STAGE_NOT_EVALUATED.value,)
    assert audit.engineering_chain_ready is False
    assert main_audit_exit_code(audit) == EXIT_BLOCKED
    codes = codes_of(audit, stage)
    if must_include:
        assert must_include in codes, codes
    return codes


def test_missing_artifacts_are_reported_as_missing_until_the_first_gap(tmp_path: Path) -> None:
    """只给出 inbox：第 ① 段 PASS，第 ② 段 MISSING（真实链路尚未走完）。"""
    chain = build_chain(tmp_path)
    audit = audit_evidence_chain(
        ChainAuditInputs(inbox_dir=str(chain.inbox_dir)), moment=MOMENT
    )

    assert status_of(audit, ChainStage.INTAKE_HANDOFF) is ChainStageStatus.PASS
    assert status_of(audit, ChainStage.ATTESTATION) is ChainStageStatus.MISSING
    assert audit.earliest_failure_stage is ChainStage.ATTESTATION
    assert codes_of(audit, ChainStage.ATTESTATION) == (
        ChainAuditCode.ARTIFACT_MISSING.value,
    )
    # 没有任何凭证 → 人工核验缺失 + 真实证据缺失（诚实 BLOCKED）
    assert audit.human_verification_missing is True
    assert audit.real_evidence_missing is True
    assert audit.blocker_active is True
    assert audit.data_qualification_passed is False


def test_tampered_attestation_fails_at_attestation_stage(tmp_path: Path) -> None:
    """凭证被改写（伪造 ``all_required_verified``）→ 凭证段 fail-closed。"""
    chain = build_chain(tmp_path)
    edit_json(
        chain.attestation_path,
        lambda payload: payload.update({"all_required_verified": True}),
    )
    edit_json(
        chain.attestation_path,
        lambda payload: payload.update({"verified_material_count": 0}),
    )
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.ATTESTATION)
    assert any(code.startswith("ATTESTATION_") for code in codes), codes


def test_stale_attestation_after_evidence_drift_fails_early(tmp_path: Path) -> None:
    """提交材料内容漂移 → 最早失败落在**提交材料**那一段（旧凭证必然失效）。"""
    chain = build_chain(tmp_path)
    chain.evidence_path.write_text(
        chain.evidence_path.read_text(encoding="utf-8").replace("2400", "2500"),
        encoding="utf-8",
    )
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.INTAKE_HANDOFF)
    assert ChainAuditCode.INVALID_CANDIDATE.value in codes


def test_candidate_removal_makes_attestation_stale(tmp_path: Path) -> None:
    """候选包被移除（inbox 清空）→ 凭证与批准全部 stale（fail-closed）。"""
    import shutil

    chain = build_chain(tmp_path)
    shutil.rmtree(chain.inbox_dir)
    chain.inbox_dir.mkdir()
    audit = audit_chain(chain)

    assert status_of(audit, ChainStage.INTAKE_HANDOFF) is ChainStageStatus.PASS
    assert audit.earliest_failure_stage is ChainStage.ATTESTATION
    assert "PACKAGE_FINGERPRINT_DRIFT" in codes_of(audit, ChainStage.ATTESTATION)


def test_ledger_without_attestation_binding_fails_review_stage(tmp_path: Path) -> None:
    """历史口径 ``APPROVE``（无 attestation binding）不再满足新门禁 → 复核段 fail-closed。"""
    chain = build_chain(tmp_path)

    def drop_binding(payload: dict[str, Any]) -> None:
        for record in payload["decisions"]:
            record.pop("attestation", None)

    edit_json(chain.ledger_path, drop_binding)
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.REVIEW_APPROVAL)
    assert "ATTESTATION_MISSING" in codes


def test_review_bound_to_another_attestation_fails_review_stage(tmp_path: Path) -> None:
    """ledger 绑定的凭证身份与本次核验的凭证不一致 → 复核段 fail-closed。"""
    chain = build_chain(tmp_path)

    def swap_binding(payload: dict[str, Any]) -> None:
        for record in payload["decisions"]:
            binding = record.get("attestation")
            if binding is not None:
                binding["attestation_id"] = "0" * 64

    edit_json(chain.ledger_path, swap_binding)
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.REVIEW_APPROVAL)
    assert "ATTESTATION_TAMPERED" in codes



def test_tampered_approved_list_fails_plan_stage(tmp_path: Path) -> None:
    """批准清单被改写 → 「批准清单 / plan」段 fail-closed。"""
    chain = build_chain(tmp_path)

    def bump_rows(payload: dict[str, Any]) -> None:
        payload["approved"][0]["rows"] = payload["approved"][0]["rows"] + 1

    edit_json(chain.approved_list_path, bump_rows)
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.APPROVED_LIST_AND_PLAN)
    assert codes  # 必须给出稳定原因码（不是空集合）
    assert "APPROVED_LIST_TAMPERED" in codes


def test_tampered_plan_id_fails_plan_stage(tmp_path: Path) -> None:
    """``plan_id`` 被改写 → 计划段给出稳定原因码（``PLAN_STALE``）。"""
    chain = build_chain(tmp_path)
    edit_json(chain.plan_path, lambda payload: payload.update({"plan_id": "f" * 64}))
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.APPROVED_LIST_AND_PLAN)
    assert "PLAN_STALE" in codes


def test_dry_run_operator_result_fails_operator_stage(tmp_path: Path) -> None:
    """执行结果仍是 dry-run / 没有落库行 → 执行段 fail-closed（绝不把 dry-run 当执行）。"""
    chain = build_chain(tmp_path)

    def to_dry_run(payload: dict[str, Any]) -> None:
        payload["dry_run"] = True
        payload["counts"]["persisted"] = 0

    edit_json(chain.operator_result_path, to_dry_run)
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.OPERATOR_RESULT)
    assert "OPERATOR_NOT_EXECUTED" in codes


def test_tampered_receipt_id_fails_receipt_stage(tmp_path: Path) -> None:
    """收据 ``receipt_id`` 被改写 → 收据段 fail-closed（``RECEIPT_ID_MISMATCH``）。"""
    chain = build_chain(tmp_path)
    edit_json(chain.receipt_path, lambda payload: payload.update({"receipt_id": "0" * 64}))
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.INTAKE_RECEIPT)
    assert "RECEIPT_ID_MISMATCH" in codes


def test_tampered_recheck_fails_receipt_stage(tmp_path: Path) -> None:
    """recheck 被改写 → 收据内绑定的 recheck 摘要不一致（``RECEIPT_ID_MISMATCH``）。"""
    chain = build_chain(tmp_path)
    edit_json(
        chain.recheck_path,
        lambda payload: payload["gate"].update({"readiness_ready": False}),
    )
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.INTAKE_RECEIPT)
    assert "RECEIPT_ID_MISMATCH" in codes


def test_tampered_readiness_state_fails_packet_stage(tmp_path: Path) -> None:
    """readiness 快照被改写（指纹校验失败）→ packet 段 fail-closed。"""
    chain = build_chain(tmp_path)
    edit_json(
        chain.readiness_path,
        lambda payload: payload.update(
            {"ready_for_human_review": not payload["ready_for_human_review"]}
        ),
    )
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.DECISION_PACKET)
    assert "READINESS_TAMPERED" in codes


def test_packet_safety_field_weakened_fails_packet_stage(tmp_path: Path) -> None:
    """packet 声称资格已通过 / blocker 已解除 → packet 段 fail-closed。"""
    chain = build_chain(tmp_path)
    edit_json(
        chain.packet_path,
        lambda payload: payload.update({"data_qualification_passed": True}),
    )
    audit = audit_chain(chain)

    _assert_single_failure(audit, ChainStage.DECISION_PACKET)


def test_tampered_decision_record_fails_record_stage(tmp_path: Path) -> None:
    """决策记录被改写（``reviewer`` 变化 → ``record_id`` 不自洽）→ 记录段 fail-closed。"""
    chain = build_chain(tmp_path)
    edit_json(chain.record_path, lambda payload: payload.update({"reviewer": "someone-else"}))
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.DECISION_RECORD)
    assert "RECORD_ID_MISMATCH" in codes


def test_record_bound_packet_content_drift_fails_record_stage(tmp_path: Path) -> None:
    """记录绑定的 packet 内容摘要漂移 → 记录段 fail-closed（``approve`` 不再成立）。"""
    chain = build_chain(tmp_path)
    edit_json(
        chain.record_path,
        lambda payload: payload["packet"].update({"content_sha256": "0" * 64}),
    )
    audit = audit_chain(chain)

    codes = _assert_single_failure(audit, ChainStage.DECISION_RECORD)
    assert "PACKET_CONTENT_MISMATCH" in codes
    assert "RECORD_ID_MISMATCH" in codes



# ---------------------------------------------------------------------------
# 工程链全绿也不等于资格通过 / 零副作用 / 参数守卫
# ---------------------------------------------------------------------------
def test_green_engineering_chain_never_qualifies_data_or_advances_phase(
    tmp_path: Path,
) -> None:
    """即使工程链全绿（且声明为 operator-provided），资格 / Phase / blocker 语义都不变。"""
    chain = build_chain(tmp_path)
    audit = audit_evidence_chain(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_OPERATOR_PROVIDED,
        scenario=None,
    )

    assert audit.engineering_chain_ready is True
    # 工程审计未发现结构性缺口 ≠ 资格通过：真实性与 Gate 仍归人工
    assert audit.real_evidence_missing is False
    assert audit.human_verification_missing is False
    assert audit.l3_human_gate_pending is True
    assert audit.data_qualification_passed is False
    assert audit.phase_transition_allowed is False
    assert audit.blocker_active is True
    assert audit.human_gate_required is True
    assert audit.gate_blocked is True
    assert audit.evidence_qualified is False
    assert audit.advance_allowed is False
    assert audit.l3_l4_auto_advance_allowed is False


def test_rehearsal_flags_stay_honest_even_when_chain_is_green(tmp_path: Path) -> None:
    """演练：即使 8 段全 PASS，三个"缺失 / 待人工"结论**恒为 true**。"""
    audit = audit_chain(build_chain(tmp_path))
    assert audit.engineering_chain_ready is True
    assert (audit.real_evidence_missing, audit.human_verification_missing) == (True, True)
    assert audit.l3_human_gate_pending is True


def test_audit_never_modifies_artifacts(tmp_path: Path) -> None:
    """审计前后**全部** fixture artifact（含原始 evidence）逐字节不变。"""
    chain = build_chain(tmp_path)
    root = chain.work_dir
    before = file_snapshot(root)

    audit_chain(chain)
    run_chain_audit(chain.to_chain_inputs(), moment=MOMENT)
    audit_chain(chain)

    assert file_snapshot(root) == before
    assert chain.evidence_path.read_bytes()


def test_run_chain_audit_writes_only_the_report(tmp_path: Path) -> None:
    """``out_path`` 是唯一写开关：只新增报告本身（原子写、无 ``.tmp`` 残留）。"""
    chain = build_chain(tmp_path)
    root = chain.work_dir
    before = file_snapshot(root)
    report = root / "audit_report.json"

    audit = run_chain_audit(chain.to_chain_inputs(), moment=MOMENT, out_path=report)

    after = file_snapshot(root)
    added = set(after) - set(before)
    assert added <= {"audit_report.json", "audit_report.json.lock"}, added
    assert "audit_report.json" in added
    # 单实例锁文件是同目录并发留痕（既有 runner 口径）；其余历史 artifact 逐字节不变
    for relative, digest in before.items():
        assert after[relative] == digest
    assert audit.written_path is not None
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["chain_id"] == audit.chain_id
    assert payload["engineering_chain_ready"] is True
    assert payload["blocker_active"] is True
    assert not list(root.glob("*.tmp"))


def test_run_chain_audit_refuses_out_inside_inbox(tmp_path: Path) -> None:
    """输出不得写进 inbox（否则改变候选集、造成自我漂移）。"""
    chain = build_chain(tmp_path)
    with pytest.raises(ChainAuditPathError):
        run_chain_audit(
            chain.to_chain_inputs(), moment=MOMENT, out_path=chain.inbox_dir / "report.json"
        )
    assert not (chain.inbox_dir / "report.json").exists()


def test_run_chain_audit_lock_conflict_writes_nothing(tmp_path: Path) -> None:
    """锁冲突：fail-closed、零写入。"""
    from src.evidence.readiness_runner import LockConflictError, SingleInstanceLock

    chain = build_chain(tmp_path)
    report = chain.work_dir / "audit_report.json"
    held = SingleInstanceLock(
        report.with_name(report.name + ".lock"), owner="held-by-test"
    )
    with held, pytest.raises(LockConflictError):
        run_chain_audit(chain.to_chain_inputs(), moment=MOMENT, out_path=report)
    assert not report.exists()


def test_unknown_evidence_source_is_rejected(tmp_path: Path) -> None:
    """``evidence_source`` 是受控词表（不得用自定义值伪造结论）。"""
    chain = build_chain(tmp_path)
    with pytest.raises(ChainAuditArgumentError):
        audit_evidence_chain(chain.to_chain_inputs(), moment=MOMENT, evidence_source="whatever")


def test_naive_moment_is_rejected(tmp_path: Path) -> None:
    """审计时点必须带时区（绝不隐式当 UTC）。"""
    chain = build_chain(tmp_path)
    with pytest.raises(ChainAuditArgumentError):
        audit_evidence_chain(
            chain.to_chain_inputs(), moment=datetime(2026, 9, 23)  # noqa: DTZ001 - 故意 naive
        )


def test_inbox_dir_must_exist_for_run_chain_audit(tmp_path: Path) -> None:
    """inbox 目录不存在 → 稳定路径错误（不是静默空审计）。"""
    with pytest.raises(ChainAuditPathError):
        run_chain_audit(ChainAuditInputs(inbox_dir=str(tmp_path / "nope")), moment=MOMENT)


# ---------------------------------------------------------------------------
# 演练目录纪律 / 源码级守卫（零数据库、零网络、零外部进程）
# ---------------------------------------------------------------------------
def test_ensure_rehearsal_work_dir_allows_tmp_and_logs(tmp_path: Path) -> None:
    """仓库外的目录与 ``logs/`` 之下都允许（不创建任何东西）。"""
    outside = tmp_path / "chain-rehearsal"
    assert ensure_rehearsal_work_dir(outside) == outside.resolve()
    under_logs = REPO_ROOT / "logs" / "_chain_audit_guard_probe"
    assert ensure_rehearsal_work_dir(under_logs) == under_logs.resolve()
    assert not under_logs.exists()
    assert not outside.exists()


@pytest.mark.parametrize(
    "target",
    ("root", "src", "docs", "tests", "database", "ai"),
)
def test_ensure_rehearsal_work_dir_refuses_repo_paths(tmp_path: Path, target: str) -> None:
    """仓库内除 ``logs/`` 之外一律拒绝（防止 fixture 污染仓库、被误当真实证据）。"""
    mapping = {
        "root": REPO_ROOT,
        "src": REPO_ROOT / "src",
        "docs": REPO_ROOT / "docs",
        "tests": REPO_ROOT / "tests",
        "database": REPO_ROOT / "database",
        "ai": REPO_ROOT / ".ai",
    }
    with pytest.raises(RehearsalPathError):
        ensure_rehearsal_work_dir(mapping[target])


def test_build_rehearsal_chain_refuses_repo_work_dir() -> None:
    """演练入口同样 fail-closed：不得把 fixture 写进仓库非 ``logs/`` 路径。"""
    target = REPO_ROOT / "tests" / "_chain_audit_should_never_exist"
    with pytest.raises(RehearsalPathError):
        build_rehearsal_chain(target, moment=MOMENT)
    assert not target.exists()


def test_build_rehearsal_chain_rejects_bad_arguments(tmp_path: Path) -> None:
    """场景 / 时点必须合法（fail-closed，绝不猜）。"""
    with pytest.raises(RehearsalArgumentError):
        build_rehearsal_chain(tmp_path / "x", moment=MOMENT, scenario="whatever")
    with pytest.raises(RehearsalArgumentError):
        build_rehearsal_chain(tmp_path / "y", moment=datetime(2026, 9, 23))  # noqa: DTZ001


def test_rehearsal_writes_only_inside_its_work_dir(tmp_path: Path) -> None:
    """演练只在显式工作目录内写 artifact（不污染 ``tmp_path`` 的其它位置）。"""
    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT)
    assert chain.fixture is True
    assert {path.name for path in tmp_path.iterdir()} == {"rehearsal"}
    for path in (
        chain.evidence_path,
        chain.manifest_path,
        chain.verification_path,
        chain.attestation_path,
        chain.ledger_path,
        chain.approved_list_path,
        chain.plan_path,
        chain.operator_result_path,
        chain.recheck_path,
        chain.handoff_report_path,
        chain.readiness_path,
        chain.receipt_path,
        chain.packet_path,
        chain.record_path,
    ):
        assert path.is_file(), path
    assert chain.evidence_path.parent == chain.package_dir
    assert chain.package_dir.parent == chain.inbox_dir


def test_rehearsal_operator_result_is_explicitly_mock(tmp_path: Path) -> None:
    """演练的"人工显式执行结果"必须是**显式** Mock（绝不真实落库）。"""
    chain = build_chain(tmp_path)
    payload = json.loads(chain.operator_result_path.read_text(encoding="utf-8"))
    assert payload["fixture"] is True
    assert payload["dry_run"] is False
    assert payload["rows"][0]["fixture"] is True
    assert payload["counts"]["persisted"] == 1
    record = json.loads(chain.record_path.read_text(encoding="utf-8"))
    assert "rehearsal" in str(record["note"])
    assert record["data_qualification_passed"] is False
    assert record["blocker_active"] is True
    assert record["human_gate_level"] == "L3"
    assert record["phase_transition_executed"] is False


@pytest.mark.parametrize("path", AUDIT_SOURCE_MODULES, ids=lambda item: item.name)
def test_audit_modules_touch_neither_database_nor_network_nor_subprocess(path: Path) -> None:
    """源码级守卫：审计 / 演练模块不 import 数据库、网络库或子进程。"""
    source = path.read_text(encoding="utf-8")
    for token in (
        "import sqlalchemy",
        "from sqlalchemy",
        "session_factory",
        "import requests",
        "import httpx",
        "import aiohttp",
        "import socket",
        "import subprocess",
        "from scripts",
    ):
        assert token not in source, (path.name, token)


def test_audit_module_only_writes_the_report(tmp_path: Path) -> None:
    """审计模块唯一的文件写入口是审计报告（`atomic_write_text` 只出现一次）。"""
    source = (REPO_ROOT / "src" / "evidence" / "chain_audit.py").read_text(encoding="utf-8")
    assert source.count("atomic_write_text(path, text)") == 1
    rehearsal = (REPO_ROOT / "src" / "evidence" / "rehearsal.py").read_text(encoding="utf-8")
    assert "atomic_write_text" not in rehearsal


def test_render_summary_mentions_stage_and_earliest_failure(tmp_path: Path) -> None:
    """人类可读摘要必须给出逐段状态与最早失败阶段（供 operator 定位）。"""
    chain = build_chain(tmp_path)
    green = render_chain_audit_summary(audit_chain(chain))
    assert "intake_handoff" in green and "decision_record" in green
    assert "engineering_chain_ready" in green
    assert "全段 PASS" in green

    edit_json(chain.receipt_path, lambda payload: payload.update({"receipt_id": "0" * 64}))
    broken = render_chain_audit_summary(audit_chain(chain))
    assert "intake_receipt" in broken
    assert "RECEIPT_ID_MISMATCH" in broken
    assert "最早失败阶段" in broken

