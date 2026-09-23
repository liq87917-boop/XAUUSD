"""Phase 3.3 证据链**纯本地演练** fixture 构造（**GOLD-030**）。

本模块把"从 GOLD-027 handoff 到 GOLD-016 L3 决策记录"的**整条**链路用**纯本地** fixture
搭起来，供 :mod:`src.evidence.chain_audit` 做端到端演练（rehearsal）：

- **零网络 / 零数据库 / 零交易**：**绝不**调用 ``scripts.evidence_operator``（真实落库）、
  **绝不**建数据库连接、**绝不**采集：人工显式执行结果用**显式标注的 Mock manifest** 代替
  （只校验格式与内容绑定，不产生任何真实落库事实）；
- **复用既有模块**：GOLD-028 ``run_attestation`` / GOLD-012 ``run_review`` /
  GOLD-013 ``run_intake_plan`` / GOLD-014 ``run_intake_receipt`` / GOLD-015
  ``run_decision_packet`` / GOLD-016 ``run_decision_record`` 全部**原样调用**，
  不复制任何资格规则；GOLD-008 handoff 与 qualification recheck 用**显式标注的 fixture**
  产物（``fixture=true`` 由审计输出声明）构造；
- **只写显式工作目录**：所有 fixture 只写入 ``--work-dir``（``logs/`` 之外**拒绝**写入仓库），
  且**绝不**覆盖仓库内任何既有文件；
- **绝不冒充真实资格**：本模块产出的一切都**不是**真实授权证据；演练结论由
  :mod:`src.evidence.chain_audit` 明确标为 ``rehearsal_fixture``，``real_evidence_missing`` /
  ``human_verification_missing`` / ``l3_human_gate_pending`` 恒为 true，
  ``PHASE3_3_DATA`` 保持 BLOCKED。

入口：``scripts/evidence_chain_audit.py --rehearsal --work-dir <dir> [--scenario ready|blocked]``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common import hashing
from src.evidence.chain_audit import (
    ChainAuditArgumentError,
    ChainAuditError,
    ChainAuditInputs,
    ChainAuditPathError,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.decision_packet import load_handoff_document, run_decision_packet
from src.evidence.decision_record import run_decision_record
from src.evidence.handoff import (
    BLOCKED_STATUS,
    HANDOFF_NOTE,
    HANDOFF_REPORT_NAME,
    HANDOFF_SCHEMA_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
    EvidenceHandoffReport,
    ScopeGap,
    build_evidence_checklist,
    build_excluded_evidence,
)
from src.evidence.human_verification_attestation import (
    handoff_content_sha256,
    run_attestation,
)
from src.evidence.inbox import INBOX_SCHEMA_VERSION, MANIFEST_FILE_NAME, scan_inbox
from src.evidence.intake_handoff import IntakeHandoffDocument, load_intake_handoff
from src.evidence.intake_plan import run_intake_plan
from src.evidence.intake_receipt import OPERATOR_MANIFEST_REPORT, run_intake_receipt
from src.evidence.readiness_watch import write_snapshot_state
from src.evidence.review import run_review
from src.monitoring.evidence_readiness import ReadinessCheck
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE, CheckStatus

__all__ = [
    "ALLOWED_REPO_SUBDIRS",
    "EVIDENCE_FILE_NAME",
    "PACKAGE_DIR_NAME",
    "REHEARSAL_APPROVE_REASON",
    "REHEARSAL_FIXTURE_NOTE",
    "REHEARSAL_MATERIAL_REASON",
    "REHEARSAL_OPERATOR_LABEL",
    "REHEARSAL_PACKAGE_SOURCE",
    "REHEARSAL_RECHECK_REPORT",
    "REHEARSAL_SCENARIOS",
    "REHEARSAL_SCOPE",
    "RehearsalArgumentError",
    "RehearsalChain",
    "RehearsalError",
    "RehearsalPathError",
    "RehearsalScenario",
    "build_rehearsal_chain",
    "ensure_rehearsal_work_dir",
]

#: 仓库内**允许**写入的相对顶层目录（除此之外的仓库路径一律拒绝，防止污染仓库）
ALLOWED_REPO_SUBDIRS: Final[tuple[str, ...]] = ("logs",)
#: 演练候选包目录名 / 证据文件名（固定，保证演练可复现）
PACKAGE_DIR_NAME: Final[str] = "pkg-author-rehearsal-01"
EVIDENCE_FILE_NAME: Final[str] = "author.jsonl"
#: 演练用的人工标签（**不是**真实审批人；非敏感 label）
REHEARSAL_OPERATOR_LABEL: Final[str] = "rehearsal-operator"
#: 演练用受控原因码（取自既有词表，**不新增**词表）
REHEARSAL_APPROVE_REASON: Final[str] = "APPROVED_FOR_EXPLICIT_INTAKE"
REHEARSAL_MATERIAL_REASON: Final[str] = "HUMAN_REVIEWED"
#: 演练候选包的 ``source`` / ``scope``（固定，便于人工核对）
REHEARSAL_PACKAGE_SOURCE: Final[str] = "manual-evidence-rehearsal"
REHEARSAL_SCOPE: Final[str] = EvidenceScope.AUTHOR.value
#: 演练 recheck 产物的 ``report`` 标识（与 GOLD-014 期望的既有载荷标识一致）
REHEARSAL_RECHECK_REPORT: Final[str] = "phase33_qualification_recheck"
#: 固定说明：演练 fixture 的诚实语义
REHEARSAL_FIXTURE_NOTE: Final[str] = (
    "本演练链路**完全**由 fixture / Mock operator result 构成（GOLD-008 handoff 与 "
    "qualification recheck 都是**显式** fixture 产物）：它**绝不**代表真实授权证据、"
    f"**绝不**解除 `{PHASE3_3_BLOCKER_CODE}`、**绝不**构成 L3 批准；"
    "演练只证明工程链各段契约一致（工具链健康）。"
)


class RehearsalScenario(StrEnum):
    """演练场景：``READY`` = 量化门槛达标（fixture 控制流）；``BLOCKED`` = 诚实 BLOCKED。"""

    READY = "ready"
    BLOCKED = "blocked"


#: 受控词表：允许的演练场景
REHEARSAL_SCENARIOS: Final[tuple[str, ...]] = tuple(item.value for item in RehearsalScenario)


class RehearsalError(ChainAuditError):
    """演练层失败（**fail-closed**；退出码语义与审计层一致）。"""


class RehearsalArgumentError(RehearsalError, ChainAuditArgumentError):
    """演练参数错误（未知场景 / 时点缺时区等）。"""


class RehearsalPathError(RehearsalError, ChainAuditPathError):
    """演练工作目录不可用（含"会污染仓库"的拒绝）。"""



@dataclass(frozen=True, slots=True)
class RehearsalChain:
    """一次演练产出的**全部** fixture artifact 路径（供审计逐段核验；**不是**真实证据）。"""

    work_dir: Path
    scenario: str
    moment: datetime
    inbox_dir: Path
    package_dir: Path
    evidence_path: Path
    manifest_path: Path
    verification_path: Path
    attestation_path: Path
    ledger_path: Path
    approved_list_path: Path
    plan_path: Path
    operator_result_path: Path
    recheck_path: Path
    handoff_report_path: Path
    readiness_path: Path
    receipt_path: Path
    packet_path: Path
    record_path: Path
    record_decision: str
    fixture: bool = True

    def to_chain_inputs(self) -> ChainAuditInputs:
        """转换为审计输入（**显式**路径；审计层绝不猜路径）。"""
        return ChainAuditInputs(
            inbox_dir=str(self.inbox_dir),
            handoff_report=str(self.handoff_report_path),
            attestation=str(self.attestation_path),
            ledger=str(self.ledger_path),
            approved_list=str(self.approved_list_path),
            plan=str(self.plan_path),
            operator_results=(str(self.operator_result_path),),
            recheck=str(self.recheck_path),
            readiness=str(self.readiness_path),
            receipt=str(self.receipt_path),
            packet=str(self.packet_path),
            record=str(self.record_path),
        )

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；只列文件名与场景）。"""
        return {
            "work_dir": str(self.work_dir),
            "scenario": self.scenario,
            "moment": self.moment.isoformat(),
            "fixture": self.fixture,
            "record_decision": self.record_decision,
            "inbox_dir": str(self.inbox_dir),
            "files": {
                key: value.name
                for key, value in (
                    ("evidence", self.evidence_path),
                    ("manifest", self.manifest_path),
                    ("verification", self.verification_path),
                    ("attestation", self.attestation_path),
                    ("ledger", self.ledger_path),
                    ("approved_list", self.approved_list_path),
                    ("plan", self.plan_path),
                    ("operator_result", self.operator_result_path),
                    ("recheck", self.recheck_path),
                    ("handoff_report", self.handoff_report_path),
                    ("readiness", self.readiness_path),
                    ("receipt", self.receipt_path),
                    ("packet", self.packet_path),
                    ("record", self.record_path),
                )
            },
        }


def ensure_rehearsal_work_dir(work_dir: str | Path) -> Path:
    """把 ``--work-dir`` 规范化为绝对路径，并**拒绝**会污染仓库的路径（fail-closed）。

    规则：仓库内的路径必须是 :data:`ALLOWED_REPO_SUBDIRS`（``logs/``）之下的子目录；
    其它仓库路径（``.ai/`` / ``docs/`` / ``src/`` / ``tests/`` / 仓库根等）一律拒绝，
    防止演练 fixture 污染仓库、或被误当成真实证据。

    Raises:
        RehearsalPathError: 路径在仓库内但不在允许的子目录下。
    """
    target = Path(work_dir).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    resolved = target.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError:
        return resolved
    head = relative.parts[0] if relative.parts else ""
    if head not in ALLOWED_REPO_SUBDIRS:
        allowed = "、".join(f"`{name}/`" for name in ALLOWED_REPO_SUBDIRS)
        raise RehearsalPathError(
            "演练只允许写入**显式**工作目录，且**不得**写进仓库（否则会污染仓库、"
            f"把 fixture 混入真实证据）；仓库内仅允许 {allowed} 之下：{resolved}"
        )
    return resolved


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    """写入确定性 JSON（UTF-8 + 缩进 + 键排序）；仅供演练 fixture 使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path

# ---------------------------------------------------------------------------
# fixture 构造（**纯本地**；只写显式工作目录，绝不触碰仓库既有文件）
# ---------------------------------------------------------------------------
def _author_row(moment: datetime) -> dict[str, Any]:
    """完全合规的 Author 证据行（**演练 fixture**；含独立历史可用证据）。"""
    published = moment - timedelta(days=30)
    collected = published + timedelta(hours=1)
    available = published + timedelta(minutes=30)
    reviewed = moment - timedelta(days=32)
    return {
        "source": REHEARSAL_PACKAGE_SOURCE,
        "source_record_id": "rehearsal-post-0001",
        "author_name": "演练作者（rehearsal fixture）",
        "external_account_id": "rehearsal-acct-001",
        "content": "演练 fixture：黄金在 2400 附近承压，若跌破 2380 看向 2350。",
        "published_at": published.isoformat(),
        "collected_at": collected.isoformat(),
        "available_at": available.isoformat(),
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/rehearsal-fixture-archive",
        "provenance_reference": "https://vendor.example/rehearsal-fixture-posts/0001",
        "url": "https://vendor.example/rehearsal-fixture-posts/0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "https://vendor.example/rehearsal-fixture-permission",
        "authorization_reviewed_by": REHEARSAL_OPERATOR_LABEL,
        "authorization_reviewed_at": reviewed.isoformat(),
        "authorization_valid_from": (reviewed - timedelta(days=1)).isoformat(),
        "authorization_expires_at": (moment + timedelta(days=365)).isoformat(),
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }


def _manifest(*, sha256: str, file_name: str) -> dict[str, Any]:
    """GOLD-011 inbox 候选包 manifest（与真实布局同一契约；**不**新增任何字段）。"""
    return {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": REHEARSAL_SCOPE,
        "source": REHEARSAL_PACKAGE_SOURCE,
        "authorization_reference": "https://vendor.example/rehearsal-fixture-permission",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [{"path": file_name, "sha256": sha256, "format": "jsonl"}],
    }


def _verification_input(handoff: IntakeHandoffDocument, *, moment: datetime) -> dict[str, Any]:
    """GOLD-028 人工核验输入（**演练 fixture**：所有必核验材料均声明 VERIFIED）。"""
    package = handoff.packages[0]
    reviewed_at = (moment - timedelta(minutes=30)).isoformat()
    materials = [
        {
            "material": item.key,
            "decision": "VERIFIED",
            "reason_code": REHEARSAL_MATERIAL_REASON,
            "reviewer": REHEARSAL_OPERATOR_LABEL,
            "reviewed_at": reviewed_at,
            "evidence_reference": "https://vendor.example/rehearsal-fixture-terms",
        }
        for item in package.materials
        if item.category != "gate"
    ]
    return {
        "schema_version": 1,
        "package_fingerprint": package.fingerprint,
        "scope": package.scope,
        "reviewer": REHEARSAL_OPERATOR_LABEL,
        "handoff_content_sha256": handoff_content_sha256(handoff),
        "materials": materials,
    }


def _mock_operator_result(evidence_path: Path, *, moment: datetime, scope: str) -> dict[str, Any]:
    """**Mock** Evidence Operator 执行结果（格式与真实 manifest 一致；**绝不**真实落库）。"""
    return {
        "schema_version": 1,
        "report": OPERATOR_MANIFEST_REPORT,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "scope": scope,
        "dry_run": False,
        "generated_at": moment.isoformat(),
        "input": {
            "path": str(evidence_path),
            "sha256": hashing.sha256_bytes(evidence_path.read_bytes()),
        },
        "counts": {
            "rows": 1,
            "accepted": 1,
            "quarantined": 0,
            "duplicate": 0,
            "oos_eligible": 1,
            "not_oos_eligible": 0,
            "persisted": 1,
            "sources_created": 1,
            "processed_success": 1,
        },
        "rows": [{"row_number": 1, "status": "ACCEPTED", "fixture": True}],
        "fixture": True,
        "notes": ["Mock operator result（演练用；**未**真实落库、**未**写数据库）"],
    }


def _fixture_recheck(*, moment: datetime, ready: bool) -> dict[str, Any]:
    """**fixture** qualification recheck（既有载荷标识；**不是**真实复核结论）。"""
    return {
        "schema_version": 1,
        "report": REHEARSAL_RECHECK_REPORT,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "as_of": moment.isoformat(),
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "ready": ready,
        "gate": {
            "qualification_ready": ready,
            "qualification_pass_count": 7 if ready else 0,
            "qualification_blocked_count": 0 if ready else 2,
            "readiness_ready": ready,
            "readiness_blocked_scope_count": 0 if ready else 2,
        },
        "readiness": {"scopes": []},
        "qualification": {"checks": []},
        "fixture": True,
        "notes": [REHEARSAL_FIXTURE_NOTE],
    }


def _check(
    *,
    key: str,
    scope: str,
    metric: str,
    current: int | float,
    required: int | float,
    comparator: str,
    evaluable: bool = True,
) -> ReadinessCheck:
    """按既有比较口径构造一条**算术自洽**的 fixture 检查（阈值只引用既有常量）。"""
    if comparator == ">=":
        status = CheckStatus.PASS if current >= required else CheckStatus.BLOCKED
        remaining: int | float = max(0, required - current)
    else:
        status = CheckStatus.PASS if (evaluable and current <= required) else CheckStatus.BLOCKED
        remaining = round(max(0.0, current - required), 6) if evaluable else 0.0
    return ReadinessCheck(
        key=key,
        scope=scope,
        metric=metric,
        current=current,
        required=required,
        comparator=comparator,
        status=status,
        evaluable=evaluable,
        remaining=remaining,
        reason=f"rehearsal fixture：{metric}",
    )


def _author_gap(*, ready: bool) -> ScopeGap:
    """Author scope 缺口（fixture；阈值**只引用** ``src.alpha.evidence_gate``）。"""
    required = MIN_AUTHOR_SAMPLES
    current = required if ready else 0
    checks = (
        _check(
            key="author.evidence_intake_oos_eligible",
            scope="author",
            metric="OOS eligible author records",
            current=current,
            required=required,
            comparator=">=",
        ),
    )
    return ScopeGap(
        scope=EvidenceScope.AUTHOR.value,
        status="PASS" if ready else "BLOCKED",
        eligible=current,
        required=required,
        remaining=max(0, required - current),
        certified=current,
        not_oos_eligible=0,
        coverage_applicable=False,
        coverage_days=0,
        coverage_required=0,
        coverage_remaining=0,
        source_share_applicable=False,
        max_source_share=0.0,
        source_share_limit=0.0,
        source_share_evaluable=False,
        source_share_remaining=0.0,
        remaining_checks=sum(1 for item in checks if item.status is not CheckStatus.PASS),
        checks=checks,
    )


def _news_gap(*, ready: bool) -> ScopeGap:
    """News scope 缺口（fixture；阈值**只引用** ``src.alpha.evidence_gate``）。"""
    required = MIN_NEWS_EVENTS
    days_required = MIN_NEWS_HISTORY_DAYS
    share_limit = MAX_NEWS_SOURCE_SHARE
    current = required + 2 if ready else 0
    days = days_required + 90 if ready else 0
    share = round(share_limit / 2, 6) if ready else 0.0
    checks = (
        _check(
            key="news.evidence_intake_oos_eligible",
            scope="news",
            metric="OOS eligible news events",
            current=current,
            required=required,
            comparator=">=",
        ),
        _check(
            key="news.evidence_intake_coverage_days",
            scope="news",
            metric="OOS eligible coverage days",
            current=days,
            required=days_required,
            comparator=">=",
        ),
        _check(
            key="news.evidence_intake_max_source_share",
            scope="news",
            metric="largest single-source share",
            current=share,
            required=share_limit,
            comparator="<=",
            evaluable=ready,
        ),
    )
    return ScopeGap(
        scope=EvidenceScope.NEWS.value,
        status="PASS" if ready else "BLOCKED",
        eligible=current,
        required=required,
        remaining=max(0, required - current),
        certified=current,
        not_oos_eligible=0,
        coverage_applicable=True,
        coverage_days=days,
        coverage_required=days_required,
        coverage_remaining=max(0, days_required - days),
        source_share_applicable=True,
        max_source_share=share,
        source_share_limit=share_limit,
        source_share_evaluable=ready,
        source_share_remaining=round(max(0.0, share - share_limit), 6) if ready else 0.0,
        remaining_checks=sum(1 for item in checks if item.status is not CheckStatus.PASS),
        checks=checks,
    )


def _fixture_handoff(*, moment: datetime, ready: bool) -> dict[str, Any]:
    """**fixture** GOLD-008 handoff 报告（算术自洽、安全字段诚实；**不是**真实量化结论）。"""
    author = _author_gap(ready=ready)
    news = _news_gap(ready=ready)
    report = EvidenceHandoffReport(
        schema_version=HANDOFF_SCHEMA_VERSION,
        report=HANDOFF_REPORT_NAME,
        contract_version=EVIDENCE_CONTRACT_VERSION,
        as_of=moment,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=True,
        human_gate_required=True,
        status=PENDING_HUMAN_REVIEW_STATUS if ready else BLOCKED_STATUS,
        quantified_thresholds_met=ready,
        ready_for_human_review=ready,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        author=author,
        news=news,
        total_remaining_gap_count=author.remaining_checks + news.remaining_checks,
        checklist=build_evidence_checklist(),
        excluded_evidence=build_excluded_evidence(),
        quarantine_reason_counts=(),
        batch=None,
        notes=(HANDOFF_NOTE, REHEARSAL_FIXTURE_NOTE),
    )
    return report.to_dict()


# ---------------------------------------------------------------------------
# 演练链路：inbox → attestation → review → plan → Mock 执行 → receipt → packet → record
# ---------------------------------------------------------------------------
def build_rehearsal_chain(
    work_dir: str | Path,
    *,
    moment: datetime,
    scenario: RehearsalScenario | str = RehearsalScenario.READY,
) -> RehearsalChain:
    """在**显式工作目录**内搭起完整 Phase 3.3 证据链 fixture 并返回其路径。

    ⚠️ 安全边界：

    - **零网络 / 零数据库 / 零交易**：不建连接、不落库、不调用任何采集器或交易代码；
      人工显式执行结果由**显式标注的 Mock manifest** 代替；
    - **只写 ``work_dir``**：仓库内仅允许 ``logs/`` 之下（见 :func:`ensure_rehearsal_work_dir`）；
    - **绝不**冒充真实证据：GOLD-008 handoff / qualification recheck 都是 fixture 产物，
      演练结论由审计层标为 ``rehearsal_fixture``。

    Args:
        work_dir: 显式工作目录（不存在则创建；**不得**写进仓库其它位置）。
        moment: 演练审计时点（必须带时区；链路内所有 artifact 都用同一时点，保证确定性）。
        scenario: ``ready``（fixture 控制流：量化达标 + ``approve`` 记录）或
            ``blocked``（诚实 BLOCKED：packet 不可提交 + ``reject`` 记录）。

    Returns:
        :class:`RehearsalChain`（含全部 fixture 路径与 ``to_chain_inputs()``）。

    Raises:
        RehearsalArgumentError: 时点缺时区 / 场景非法。
        RehearsalPathError: 工作目录不可用或会污染仓库。
        src.evidence.*: 任一既有模块的 fail-closed 异常（演练链应保持自洽，
            出现异常说明 fixture 与既有契约已漂移，必须修复）。
    """
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise RehearsalArgumentError("moment 必须是带时区的 datetime")
    now = moment.astimezone(UTC)
    try:
        chosen = RehearsalScenario(str(scenario).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(REHEARSAL_SCENARIOS)
        raise RehearsalArgumentError(f"scenario 必须是 {allowed} 之一") from exc
    ready = chosen is RehearsalScenario.READY

    root = ensure_rehearsal_work_dir(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    inbox = root / "inbox"
    package = inbox / PACKAGE_DIR_NAME
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / EVIDENCE_FILE_NAME
    evidence.write_text(json.dumps(_author_row(now), ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = _write_json(
        package / MANIFEST_FILE_NAME,
        _manifest(
            sha256=hashing.sha256_bytes(evidence.read_bytes()), file_name=EVIDENCE_FILE_NAME
        ),
    )

    # ① GOLD-027 只读预检 → ② GOLD-028 材料级人工核验凭证
    handoff = load_intake_handoff(inbox, as_of=now)
    verification = _write_json(
        root / "rehearsal_human_verification.json", _verification_input(handoff, moment=now)
    )
    attestation = root / "rehearsal_phase33_human_verification_attestation.json"
    run_attestation(
        inbox,
        verification_path=verification,
        moment=now,
        package=handoff.packages[0].fingerprint,
        out_path=attestation,
    )

    # ③ GOLD-012 review（APPROVE 必须绑定**当前**凭证）
    scan = scan_inbox(inbox, moment=now)
    target = next(item for item in scan.packages if item.package_dir == PACKAGE_DIR_NAME)
    ledger = root / "rehearsal_inbox_review_ledger.json"
    approved = root / "rehearsal_approved_for_intake.json"
    run_review(
        inbox,
        moment=now,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer=REHEARSAL_OPERATOR_LABEL,
        reason_code=REHEARSAL_APPROVE_REASON,
        note="rehearsal fixture",
        attestation_path=attestation,
        out_path=ledger,
        approved_out_path=approved,
    )


    # ④ GOLD-013 plan → ⑤ Mock 执行结果 → ⑥ GOLD-014 receipt + recheck
    plan = root / "rehearsal_evidence_intake_plan.json"
    run_intake_plan(
        inbox, moment=now, ledger_path=ledger, approved_list_path=approved, out_path=plan
    )
    operator_result = _write_json(
        root / "rehearsal_operator_result.json",
        _mock_operator_result(evidence, moment=now, scope=REHEARSAL_SCOPE),
    )
    recheck = _write_json(
        root / "rehearsal_phase33_recheck.json", _fixture_recheck(moment=now, ready=ready)
    )
    handoff_report = _write_json(
        root / "rehearsal_evidence_handoff.json", _fixture_handoff(moment=now, ready=ready)
    )
    readiness = root / "rehearsal_readiness_state.json"
    write_snapshot_state(readiness, load_handoff_document(handoff_report).source)
    receipt = root / "rehearsal_evidence_intake_receipt.json"
    run_intake_receipt(
        inbox,
        moment=now,
        plan_path=plan,
        ledger_path=ledger,
        approved_list_path=approved,
        operator_result_paths=(operator_result,),
        recheck_path=recheck,
        out_path=receipt,
    )

    # ⑦ GOLD-015 packet → ⑧ GOLD-016 L3 人工决策记录（演练用 Mock 人工输入）
    packet = root / "rehearsal_evidence_decision_packet.json"
    run_decision_packet(
        inbox,
        moment=now,
        handoff_path=handoff_report,
        plan_path=plan,
        ledger_path=ledger,
        approved_list_path=approved,
        readiness_path=readiness,
        receipt_path=receipt,
        operator_result_paths=(operator_result,),
        recheck_path=recheck,
        out_path=packet,
    )
    decision = "approve" if ready else "reject"
    record = root / "rehearsal_l3_human_decision_record.json"
    run_decision_record(
        packet,
        decision=decision,
        reviewer=REHEARSAL_OPERATOR_LABEL,
        moment=now,
        note="rehearsal fixture（非真实人工核验 / 非真实 L3 批准）",
        out_path=record,
    )
    return RehearsalChain(
        work_dir=root,
        scenario=chosen.value,
        moment=now,
        inbox_dir=inbox,
        package_dir=package,
        evidence_path=evidence,
        manifest_path=manifest,
        verification_path=verification,
        attestation_path=attestation,
        ledger_path=ledger,
        approved_list_path=approved,
        plan_path=plan,
        operator_result_path=operator_result,
        recheck_path=recheck,
        handoff_report_path=handoff_report,
        readiness_path=readiness,
        receipt_path=receipt,
        packet_path=packet,
        record_path=record,
        record_decision=decision,
    )

