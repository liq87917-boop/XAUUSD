"""Phase 3.3 证据链**端到端只读审计 / 纯本地演练**（**GOLD-030**）。

GOLD-027 → GOLD-028 → GOLD-029 → intake plan → receipt/recheck → decision packet →
decision record 各自都有严格的只读核验入口，但**没有任何一个入口**把整条链串起来逐段核对
artifact identity / schema / content binding。真实授权证据到来时才暴露"跨模块契约漂移"
（例如凭证绑定的 package 与 plan/receipt/packet/record 引用的不是同一份内容），代价极高。

本模块补齐这一层：

- **只读 / 零网络 / 零数据库 / 零写入**：只读取**显式给出**的本地 artifact 路径与 ``--inbox-dir``；
  不采集、不联网、不写数据库、不执行任何 intake / commit、**绝不**移动 / 删除 / 改写任何原始
  evidence 或历史 artifact；唯一写开关是显式 ``--out``（只写**审计报告本身**，原子写）；
- **复用而不复制**：每个阶段**直接调用**既有模块的核验函数（``load_intake_handoff`` /
  ``verify_attestation`` / ``build_approved_intake_list`` / ``verify_intake_receipt`` /
  ``verify_decision_packet`` / ``verify_decision_record`` …），本模块**不新增、不降低**任何
  资格阈值，也不重新解释任何契约字段；它只做"同一份内容身份是否前后一致"的绑定核对；
- **逐段 fail-closed + 最早失败阶段**：任一阶段缺失 / 损坏 / 被篡改 / 漂移 → 该阶段
  ``status=FAIL``（或 ``MISSING``）并给出**稳定原因码**，后续阶段标为 ``NOT_EVALUATED``，
  ``earliest_failure_stage`` 明确指向**最早**出问题的那一段；
- **内容身份**：输出确定性的 ``facts_digest``（各阶段身份事实的内容摘要）与 ``chain_id``
  （策略块 + 阶段 / 状态 / 原因码 + ``facts_digest``）；任一 artifact 漂移必然改变 ``chain_id``；
- **四个独立结论**：``engineering_chain_ready``（工程链契约是否全部一致）/
  ``real_evidence_missing`` / ``human_verification_missing`` / ``l3_human_gate_pending``；
  **即使工程链全 PASS**，``data_qualification_passed`` / ``phase_transition_allowed`` **恒为
  false**、``blocker_active`` / ``human_gate_required`` **恒为 true**（**硬编码**），
  ``PHASE3_3_DATA`` 保持 BLOCKED，L3 / L4 只能人工决策；
- **演练只证明工具链健康**：``evidence_source=rehearsal_fixture`` 的链路完全由 fixture /
  Mock operator result 构成，``real_evidence_missing`` / ``human_verification_missing`` /
  ``l3_human_gate_pending`` **恒为 true**：**绝不**把 fixture / Mock 当作真实资格证据，
  **绝不**解除 blocker，**绝不**产生真实 L3 批准。

入口：``scripts/evidence_chain_audit.py``（``--audit`` 只读审计既有 artifact；
``--rehearsal --work-dir <dir>`` 在显式工作目录内生成 fixture 链并审计；默认只打印 stdout）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common import hashing
from src.common.redaction import safe_text
from src.evidence import readiness_runner as runner
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.decision_packet import (
    DecisionPacketError,
    HandoffDocument,
    PacketVerificationCode,
    ReadinessStateDocument,
    ReceiptDocument,
    build_decision_packet,
    load_handoff_document,
    load_intake_receipt_document,
    load_readiness_state_document,
    verify_decision_packet,
)
from src.evidence.decision_record import (
    DecisionRecordCode,
    DecisionRecordError,
    PacketBinding,
    load_decision_packet,
    verify_decision_record,
)
from src.evidence.human_verification_attestation import (
    AttestationError,
    AttestationVerification,
    handoff_content_sha256,
    verify_attestation,
)
from src.evidence.inbox import (
    InboxError,
    InboxPreflightReport,
    ensure_outside_inbox,
    scan_inbox,
)
from src.evidence.intake_handoff import (
    INTAKE_HANDOFF_CONTRACT_VERSION,
    INTAKE_HANDOFF_SCHEMA_VERSION,
    IntakeHandoffDocument,
    load_intake_handoff,
)
from src.evidence.intake_plan import (
    ApprovedListDocument,
    IntakePlan,
    IntakePlanError,
    IntakePlanInconsistentError,
    build_intake_plan,
    load_approved_intake_list,
)
from src.evidence.intake_receipt import (
    IntakePlanDocument,
    IntakeReceiptError,
    IntakeReceiptStateError,
    OperatorResult,
    QualificationRecheck,
    ReceiptVerificationCode,
    build_intake_receipt,
    load_intake_plan_document,
    load_operator_result,
    load_qualification_recheck,
    verify_intake_receipt,
)
from src.evidence.readiness_watch import atomic_write_text
from src.evidence.review import (
    InvalidationReason,
    ReviewError,
    ReviewLedger,
    build_approved_intake_list,
    load_review_ledger,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "CHAIN_AUDIT_CONTRACT_VERSION",
    "CHAIN_AUDIT_EXECUTION_MODE",
    "CHAIN_AUDIT_FILE_NAME",
    "CHAIN_AUDIT_KIND",
    "CHAIN_AUDIT_LOCK_SUFFIX",
    "CHAIN_AUDIT_NOTE",
    "CHAIN_AUDIT_REPORT_NAME",
    "CHAIN_AUDIT_SCHEMA_VERSION",
    "CHAIN_AUDIT_SEMANTICS_NOTE",
    "CHAIN_REHEARSAL_SEMANTICS_NOTE",
    "CHAIN_STAGE_ORDER",
    "EVIDENCE_SOURCES",
    "EVIDENCE_SOURCE_OPERATOR_PROVIDED",
    "EVIDENCE_SOURCE_REHEARSAL_FIXTURE",
    "EXIT_BLOCKED",
    "EXIT_CONFIG_ERROR",
    "EXIT_LOCK_CONFLICT",
    "EXIT_OK",
    "EXIT_STATE_INVALID",
    "EXIT_UNUSABLE",
    "ChainAuditArgumentError",
    "ChainAuditCode",
    "ChainAuditError",
    "ChainAuditInputs",
    "ChainAuditPathError",
    "ChainAuditWriteError",
    "ChainStage",
    "ChainStageResult",
    "ChainStageStatus",
    "EvidenceChainAudit",
    "audit_evidence_chain",
    "chain_audit_exit_code_for",
    "compute_chain_facts_digest",
    "compute_chain_id",
    "main_audit_exit_code",
    "render_chain_audit_summary",
    "run_chain_audit",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
CHAIN_AUDIT_SCHEMA_VERSION: Final[int] = 1
#: 文档 / 报告标识（稳定，供 operator 与上游日志解析）
CHAIN_AUDIT_KIND: Final[str] = "phase33_evidence_chain_audit"
CHAIN_AUDIT_REPORT_NAME: Final[str] = "phase33_evidence_chain_audit"
#: 输入契约版本（**只引用** ``evidence-intake-v1``，不另造第二套契约）
CHAIN_AUDIT_CONTRACT_VERSION: Final[str] = EVIDENCE_CONTRACT_VERSION
#: 执行模式（**恒为只读**：本工具不执行任何 intake / 写库 / 交易动作）
CHAIN_AUDIT_EXECUTION_MODE: Final[str] = "READ_ONLY_CHAIN_AUDIT"
#: 审计报告（``--out`` 时才写）的默认文件名
CHAIN_AUDIT_FILE_NAME: Final[str] = "phase33_evidence_chain_audit.json"
#: 审计报告的单实例锁后缀（与 ``out_path`` 同级）
CHAIN_AUDIT_LOCK_SUFFIX: Final[str] = ".lock"
#: 证据来源：纯本地演练 fixture（**绝不**是真实资格证据）
EVIDENCE_SOURCE_REHEARSAL_FIXTURE: Final[str] = "rehearsal_fixture"
#: 证据来源：operator 显式提供的既有 artifact（本工具仍不判断真实性 / 法律效力）
EVIDENCE_SOURCE_OPERATOR_PROVIDED: Final[str] = "operator_provided"
#: 受控词表：允许的 ``evidence_source``
EVIDENCE_SOURCES: Final[tuple[str, ...]] = (
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    EVIDENCE_SOURCE_OPERATOR_PROVIDED,
)

#: 固定说明：本工具的职责边界（只读审计 / 演练，不是资格判定器）
CHAIN_AUDIT_NOTE: Final[str] = (
    "本工具只做**端到端只读审计 / 演练**：逐段核验 Phase 3.3 证据链（GOLD-027 intake handoff → "
    "GOLD-028 材料级人工核验凭证 → GOLD-012 / GOLD-029 review 批准 → GOLD-013 plan → 人工**显式**"
    "执行结果 → GOLD-014 receipt / recheck → GOLD-015 packet → GOLD-016 L3 决策记录）的 "
    "artifact identity / schema / content binding；它**不采集**、**不联网**、**不写数据库**、"
    "**不执行 intake**、**不移动 / 删除 / 改写**任何原始 evidence 或历史 artifact。"
)
#: 固定说明：四个结论字段的诚实语义
CHAIN_AUDIT_SEMANTICS_NOTE: Final[str] = (
    "`engineering_chain_ready=true` **只**表示工程链各段契约一致（工具链健康）；它**不是**"
    "数据资格通过。`real_evidence_missing` 只描述\"工程审计未发现结构性证据缺口\"，真实性 / "
    "法律效力 / 出处仍**只能**由 GOLD-028 材料级人工核验与 **L3 人工 Gate** 判定；"
    "`data_qualification_passed` / `phase_transition_allowed` **恒为 false**，"
    f"`blocker_active` / `human_gate_required` 恒为 true，`{PHASE3_3_BLOCKER_CODE}` 保持 BLOCKED。"
)
#: 固定说明：演练（fixture）语义
CHAIN_REHEARSAL_SEMANTICS_NOTE: Final[str] = (
    "`evidence_source=rehearsal_fixture` 表示本次链路**完全**由 fixture / Mock operator result "
    "构成：`real_evidence_missing` / `human_verification_missing` / `l3_human_gate_pending` "
    "**恒为 true**；演练**绝不**代表真实授权证据、**绝不**解除 blocker、**绝不**构成 L3 批准，"
    "只证明工程链契约一致、工具链健康。"
)


class ChainStage(StrEnum):
    """证据链的**逐段**标识（顺序即审计顺序；稳定字符串，供脚本解析）。"""

    INTAKE_HANDOFF = "intake_handoff"
    ATTESTATION = "attestation"
    REVIEW_APPROVAL = "review_approval"
    APPROVED_LIST_AND_PLAN = "approved_list_and_plan"
    OPERATOR_RESULT = "operator_result"
    INTAKE_RECEIPT = "intake_receipt"
    DECISION_PACKET = "decision_packet"
    DECISION_RECORD = "decision_record"


#: 阶段固定顺序（审计按此顺序逐段核验；任一失败即短路后续阶段）
CHAIN_STAGE_ORDER: Final[tuple[ChainStage, ...]] = tuple(ChainStage)


class ChainStageStatus(StrEnum):
    """单个阶段的核验状态（``PASS`` 才表示该段绑定一致；其余一律 fail-closed）。"""

    PASS = "PASS"
    #: 该段 artifact 缺失（**预期 BLOCKED**：真实链路尚未走完）
    MISSING = "MISSING"
    #: 该段核验不通过（缺失内容 / 损坏 / 被篡改 / 漂移）
    FAIL = "FAIL"
    #: 该段因**更早**阶段失败而未评估（短路；原因码恒为 ``STAGE_NOT_EVALUATED``）
    NOT_EVALUATED = "NOT_EVALUATED"


class ChainAuditCode(StrEnum):
    """本层自有的**稳定原因码**（与既有模块同义的原因码**直接沿用**其字符串，不另造词）。"""

    #: 该段核验通过
    STAGE_OK = "STAGE_OK"
    #: 该段因更早阶段失败而未评估
    STAGE_NOT_EVALUATED = "STAGE_NOT_EVALUATED"
    #: 该段必需的 artifact 未提供 / 不存在
    ARTIFACT_MISSING = "CHAIN_ARTIFACT_MISSING"
    #: artifact 不可读 / 非法 JSON / 结构非法
    ARTIFACT_UNREADABLE = "CHAIN_ARTIFACT_UNREADABLE"
    #: artifact 被篡改 / 内部自洽失败 / 安全字段被削弱
    ARTIFACT_TAMPERED = "CHAIN_ARTIFACT_TAMPERED"
    #: 相邻 artifact 的内容身份 / 绑定不一致（契约漂移）
    BINDING_MISMATCH = "CHAIN_BINDING_MISMATCH"
    #: 版本 / schema 漂移
    SCHEMA_DRIFT = "CHAIN_SCHEMA_DRIFT"
    #: inbox 目录不可用（不存在 / 不是目录 / 不可读）
    INBOX_UNUSABLE = "CHAIN_INBOX_UNUSABLE"
    #: 没有任何仍成立的批准（链路停在人工复核之前）
    NO_APPROVED_EVIDENCE = "CHAIN_NO_APPROVED_EVIDENCE"
    #: 候选包当前未通过 GOLD-011 结构预检（提交材料本身已不一致 / 损坏）
    INVALID_CANDIDATE = "CHAIN_INVALID_CANDIDATE"


class ChainAuditError(RuntimeError):
    """审计层失败（**fail-closed**：调用方按退出码处理，不得假设已写入任何 artifact）。"""


class ChainAuditArgumentError(ChainAuditError):
    """参数 / 词表 / 时区错误。"""


class ChainAuditPathError(ChainAuditError):
    """inbox 目录或输出 artifact 不可用（含把输出写进 inbox 的拒绝）。"""


class ChainAuditWriteError(ChainAuditError):
    """审计报告原子写失败（**fail-closed**）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 工程链全部 PASS（仍**不是**资格通过）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数 / 词表 / 时区错误
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: artifact 损坏 / 被篡改 / 漂移（fail-closed，零写入）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 证据链未走通 / 任一段核验不通过（**预期 BLOCKED**，不是故障）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一处审计写入正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_CHAIN_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (ChainAuditArgumentError, EXIT_CONFIG_ERROR),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (ChainAuditPathError, EXIT_UNUSABLE),
    (ChainAuditWriteError, EXIT_UNUSABLE),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
    (ChainAuditError, EXIT_STATE_INVALID),
)


def chain_audit_exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _CHAIN_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def main_audit_exit_code(audit: EvidenceChainAudit) -> int:
    """审计结论 → 进程退出码：工程链全绿 ``0``，否则 ``5``（**预期 BLOCKED**）。"""
    return EXIT_OK if audit.engineering_chain_ready else EXIT_BLOCKED




# ---------------------------------------------------------------------------
# 通用工具（脱敏 / 规范化 / 时区 / 原因码读回）
# ---------------------------------------------------------------------------
def _safe(value: object, *, max_chars: int = 200) -> str:
    """统一脱敏 + 截断（原因文本 / 详情共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径类展示：脱敏 + 截断（绝不回显凭据）。"""
    return _safe(value, max_chars=max_chars)


def _require_aware(moment: object, *, field_name: str) -> datetime:
    """要求带时区的 ``datetime``（naive → 参数错误，绝不默认 UTC 猜测）。"""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ChainAuditArgumentError(f"{field_name} 必须是带时区的 datetime")
    return moment.astimezone(UTC)


def _canonical_json(payload: object) -> str:
    """规范化 JSON（确定性：键排序 + 紧凑分隔符 + 不转义非 ASCII）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _policy_block() -> dict[str, Any]:
    """内容身份的策略块（**硬编码**安全字段；任何输入都无法把它们改成"通过"）。"""
    return {
        "kind": CHAIN_AUDIT_KIND,
        "schema_version": CHAIN_AUDIT_SCHEMA_VERSION,
        "contract_version": CHAIN_AUDIT_CONTRACT_VERSION,
        "execution_mode": CHAIN_AUDIT_EXECUTION_MODE,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "l3_l4_auto_advance_allowed": False,
        "advance_allowed": False,
        "evidence_qualified": False,
        "gate_blocked": True,
        "stages": [stage.value for stage in CHAIN_STAGE_ORDER],
    }


def _code_from_error(error: BaseException, *, fallback: str) -> str:
    """从既有模块的异常文本里**读回**稳定原因码（各模块均以原因码开头；找不到用兜底码）。

    note:
        这里**只**把上游模块已经给出的原因码读回来，不新增第二套判定逻辑；上游原因码词表
        变化会自然反映到审计输出，并被回归测试钉住。
    """
    for token in re.split(r"[^A-Za-z0-9_]+", str(error)):
        if len(token) >= 4 and token.isupper() and any(char.isalpha() for char in token):
            return token
    return fallback


def _optional_path(value: str | None) -> str | None:
    """可选路径：``None`` 保留，其余脱敏 + 截断。"""
    return None if value is None else _safe_path(value)


# ---------------------------------------------------------------------------
# 输入：显式给出的 artifact 路径（本模块**绝不**猜路径、绝不自动生成）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChainAuditInputs:
    """一次审计的**全部**显式输入（缺省即"该段未提供" → 相应阶段 ``MISSING``）。"""

    #: 显式本地候选目录（复用 GOLD-011 / GOLD-027 只读预检）
    inbox_dir: str
    #: GOLD-008 evidence handoff 报告（decision packet 的必需输入）
    handoff_report: str | None = None
    #: GOLD-028 材料级人工核验凭证
    attestation: str | None = None
    #: GOLD-012 review ledger（追加式人工决策审计）
    ledger: str | None = None
    #: GOLD-012 approved-for-explicit-intake 清单
    approved_list: str | None = None
    #: GOLD-013 intake plan
    plan: str | None = None
    #: 人工**显式** Evidence Operator 执行结果（可能多个；演练时由 Mock 生成）
    operator_results: tuple[str, ...] = ()
    #: qualification recheck 结果
    recheck: str | None = None
    #: GOLD-010 readiness state 快照（可选；与 handoff 必须同源）
    readiness: str | None = None
    #: GOLD-014 intake receipt
    receipt: str | None = None
    #: GOLD-015 L3 人工决策包
    packet: str | None = None
    #: GOLD-016 L3 人工决策记录
    record: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；只暴露是否提供与脱敏路径）。"""
        return {
            "inbox_dir": _safe_path(self.inbox_dir),
            "handoff_report": _optional_path(self.handoff_report),
            "attestation": _optional_path(self.attestation),
            "ledger": _optional_path(self.ledger),
            "approved_list": _optional_path(self.approved_list),
            "plan": _optional_path(self.plan),
            "operator_results": [_safe_path(item) for item in self.operator_results],
            "operator_result_count": len(self.operator_results),
            "recheck": _optional_path(self.recheck),
            "readiness": _optional_path(self.readiness),
            "receipt": _optional_path(self.receipt),
            "packet": _optional_path(self.packet),
            "record": _optional_path(self.record),
        }


# ---------------------------------------------------------------------------
# 逐段结果（稳定 stage / status / reason code + 内容身份事实）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChainStageResult:
    """**一段**审计结论（状态 + 稳定原因码 + 脱敏详情 + 内容身份事实）。"""

    stage: ChainStage
    status: ChainStageStatus
    reason_codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    #: 内容身份事实（键 → 标量字符串；**只放 id / 摘要 / 计数 / 布尔**，不含路径与时间）
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def passed(self) -> bool:
        """该段是否核验通过。"""
        return self.status is ChainStageStatus.PASS

    def fact(self, key: str) -> str | None:
        """按 key 取内容身份事实（不存在返回 ``None``）。"""
        for name, value in self.facts:
            if name == key:
                return value
        return None

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；原因码 / 详情恒为字符串数组）。"""
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "details": [_safe(item, max_chars=300) for item in self.details],
            "facts": {key: value for key, value in self.facts},
        }

# ---------------------------------------------------------------------------
# 审计结论（确定性 / 脱敏 / 内容寻址；安全字段**硬编码**）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EvidenceChainAudit:
    """一次端到端审计的结论（**确定性脱敏**；默认零写入，``written_path`` 才落盘报告本身）。"""

    generated_at: datetime
    inbox_dir: str
    #: ``rehearsal_fixture`` / ``operator_provided``（见 :data:`EVIDENCE_SOURCES`）
    evidence_source: str
    #: 演练场景（非演练为 ``None``）
    scenario: str | None
    stages: tuple[ChainStageResult, ...]
    #: 各阶段内容身份事实的内容摘要（确定性；不含路径与时间）
    facts_digest: str
    #: 内容级链路身份（策略块 + 阶段 / 状态 / 原因码 + ``facts_digest``）
    chain_id: str
    #: 工程审计是否未发现结构性证据缺口（**不是**资格结论；演练恒为 True）
    real_evidence_missing: bool = True
    #: 材料级人工核验是否缺失（演练恒为 True：fixture 凭证**不是**真实人工核验）
    human_verification_missing: bool = True
    inputs: ChainAuditInputs | None = None
    written_path: str | None = None
    notes: tuple[str, ...] = (CHAIN_AUDIT_NOTE, CHAIN_AUDIT_SEMANTICS_NOTE)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return CHAIN_AUDIT_KIND

    @property
    def rehearsal(self) -> bool:
        """本次链路是否由 fixture / Mock 构成。"""
        return self.evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE

    @property
    def engineering_chain_ready(self) -> bool:
        """工程链是否**逐段**全部通过（**只**说明工具链契约一致）。"""
        return bool(self.stages) and all(stage.passed for stage in self.stages)

    @property
    def earliest_failure_stage(self) -> ChainStage | None:
        """**最早**未通过的阶段（全绿返回 ``None``）。"""
        for stage in self.stages:
            if not stage.passed:
                return stage.stage
        return None

    def stage_status(self, stage: ChainStage) -> ChainStageStatus | None:
        """取某一段的核验状态（未收录返回 ``None``）。"""
        for item in self.stages:
            if item.stage is stage:
                return item.status
        return None

    def stage(self, stage: ChainStage) -> ChainStageResult | None:
        """取某一段的完整结果（未收录返回 ``None``）。"""
        for item in self.stages:
            if item.stage is stage:
                return item
        return None

    @property
    def passed_stage_count(self) -> int:
        """通过的阶段数。"""
        return sum(1 for item in self.stages if item.passed)

    @property
    def failed_stage_count(self) -> int:
        """未通过（``FAIL`` / ``MISSING`` / ``NOT_EVALUATED``）的阶段数。"""
        return sum(1 for item in self.stages if not item.passed)



    # ---- 恒定的安全语义（**硬编码**：任何输入都无法把它们改成"通过"）--------
    @property
    def l3_human_gate_pending(self) -> bool:
        """恒为 ``True``：L3 / L4 只能人工决策，本工具永不自动推进。"""
        return True

    @property
    def data_qualification_passed(self) -> bool:
        """恒为 ``False``：本工具**没有**资格判定能力。"""
        return False

    @property
    def phase_transition_allowed(self) -> bool:
        """恒为 ``False``：Phase 切换只能由 L3 人工 Gate 决定。"""
        return False

    @property
    def l3_l4_auto_advance_allowed(self) -> bool:
        """恒为 ``False``。"""
        return False

    @property
    def advance_allowed(self) -> bool:
        """恒为 ``False``。"""
        return False

    @property
    def evidence_qualified(self) -> bool:
        """恒为 ``False``。"""
        return False

    @property
    def blocker_active(self) -> bool:
        """恒为 ``True``（``PHASE3_3_DATA`` 保持 BLOCKED）。"""
        return True

    @property
    def human_gate_required(self) -> bool:
        """恒为 ``True``。"""
        return True

    @property
    def gate_blocked(self) -> bool:
        """恒为 ``True``。"""
        return True

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段与四个结论**显式且恒定**）。"""
        return {
            "kind": CHAIN_AUDIT_KIND,
            "report": CHAIN_AUDIT_REPORT_NAME,
            "schema_version": CHAIN_AUDIT_SCHEMA_VERSION,
            "contract_version": CHAIN_AUDIT_CONTRACT_VERSION,
            "execution_mode": CHAIN_AUDIT_EXECUTION_MODE,
            "generated_at": self.generated_at.isoformat(),
            "inbox_dir": _safe_path(self.inbox_dir),
            "evidence_source": self.evidence_source,
            "scenario": self.scenario,
            "rehearsal": self.rehearsal,
            "chain_id": self.chain_id,
            "facts_digest": self.facts_digest,
            "engineering_chain_ready": self.engineering_chain_ready,
            "real_evidence_missing": self.real_evidence_missing,
            "human_verification_missing": self.human_verification_missing,
            "l3_human_gate_pending": self.l3_human_gate_pending,
            "earliest_failure_stage": (
                None if self.earliest_failure_stage is None else self.earliest_failure_stage.value
            ),
            "stage_count": len(self.stages),
            "passed_stage_count": self.passed_stage_count,
            "failed_stage_count": self.failed_stage_count,
            "stage_order": [stage.value for stage in CHAIN_STAGE_ORDER],
            "stages": [item.to_dict() for item in self.stages],
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "gate_blocked": self.gate_blocked,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "l3_l4_auto_advance_allowed": self.l3_l4_auto_advance_allowed,
            "advance_allowed": self.advance_allowed,
            "evidence_qualified": self.evidence_qualified,
            "writes_database": False,
            "reads_network": False,
            "executes_intake": False,
            "inputs": None if self.inputs is None else self.inputs.to_dict(),
            "written_path": None if self.written_path is None else _safe_path(self.written_path),
            "notes": list(self.notes),
        }

def compute_chain_facts_digest(stages: tuple[ChainStageResult, ...]) -> str:
    """由各阶段（状态 / 原因码 / 内容身份事实）派生 ``facts_digest``（确定性）。

    ⚠️ 刻意**不含**路径与审计时点：同一份 artifact 内容无论在什么目录、什么时点核验，
    都得到**同一**摘要；内容漂移必然改变摘要。
    """
    payload = [
        {
            "stage": item.stage.value,
            "status": item.status.value,
            "reason_codes": list(item.reason_codes),
            "facts": [[key, value] for key, value in item.facts],
        }
        for item in stages
    ]
    return hashing.sha256_text(_canonical_json(payload))


def compute_chain_id(
    *,
    stages: tuple[ChainStageResult, ...],
    facts_digest: str,
    evidence_source: str,
    scenario: str | None,
) -> str:
    """内容级 ``chain_id``：策略块 + 证据来源 + 阶段 / 状态 / 原因码 + ``facts_digest``。

    ⚠️ 刻意**不含**审计时点与路径：同一链路重复审计得到**同一** ``chain_id``；任一 artifact
    漂移（含某段从 ``PASS`` 变为 ``FAIL``）必然产生**新** ``chain_id``（旧结论绝不静默继承）。
    """
    payload = {
        "policy": _policy_block(),
        "evidence_source": evidence_source,
        "scenario": scenario,
        "facts_digest": facts_digest,
        "stages": [
            [item.stage.value, item.status.value, list(item.reason_codes)] for item in stages
        ],
    }
    return hashing.sha256_text(_canonical_json(payload))


# ---------------------------------------------------------------------------
# 阶段构造与短路
# ---------------------------------------------------------------------------
def _result(
    stage: ChainStage,
    status: ChainStageStatus,
    reason_codes: tuple[str, ...] = (),
    details: tuple[str, ...] = (),
    facts: tuple[tuple[str, str], ...] = (),
) -> ChainStageResult:
    """构造一段结论（原因码去重保序 + 脱敏截断；事实只保留字符串）。"""
    seen: dict[str, None] = {}
    for code in reason_codes:
        seen.setdefault(_safe(code, max_chars=80), None)
    return ChainStageResult(
        stage=stage,
        status=status,
        reason_codes=tuple(seen),
        details=tuple(_safe(item, max_chars=300) for item in details),
        facts=tuple(
            (_safe(key, max_chars=80), _safe(value, max_chars=200)) for key, value in facts
        ),
    )


def _pass(
    stage: ChainStage, facts: tuple[tuple[str, str], ...] = (), details: tuple[str, ...] = ()
) -> ChainStageResult:
    """该段通过（原因码恒为 ``STAGE_OK``）。"""
    return _result(
        stage,
        ChainStageStatus.PASS,
        (ChainAuditCode.STAGE_OK.value,),
        details,
        facts,
    )


def _missing(stage: ChainStage, what: str) -> ChainStageResult:
    """该段必需 artifact 缺失（**预期 BLOCKED**，不是故障）。"""
    return _result(
        stage,
        ChainStageStatus.MISSING,
        (ChainAuditCode.ARTIFACT_MISSING.value,),
        (f"{what} 未提供或不存在：无法核验本段绑定",),
    )


#: 审计过程中的**只读**中间事实（跨阶段交叉核对用；绝不写入任何 artifact）
@dataclass(slots=True)
class _Context:
    """一次审计的内部上下文（只持有既有模块返回的只读对象）。"""

    inputs: ChainAuditInputs
    moment: datetime
    handoff: IntakeHandoffDocument | None = None
    handoff_digest: str = ""
    scan: InboxPreflightReport | None = None
    ledger: ReviewLedger | None = None
    approved_doc: ApprovedListDocument | None = None
    plan_doc: IntakePlanDocument | None = None
    operator_results: tuple[OperatorResult, ...] = ()
    recheck: QualificationRecheck | None = None
    handoff_doc: HandoffDocument | None = None
    readiness: ReadinessStateDocument | None = None
    receipt_doc: ReceiptDocument | None = None
    packet_binding: PacketBinding | None = None
    attestation: AttestationVerification | None = None
    #: GOLD-027 预检事实（用于 ``real_evidence_missing`` 的诚实推导）
    preflight_pass: int = 0
    synthetic: int = 0

    @property
    def attestation_id(self) -> str:
        """已核验凭证的 ``attestation_id``（未核验返回空串）。"""
        return "" if self.attestation is None else self.attestation.attestation_id

    @property
    def attestation_complete(self) -> bool:
        """凭证是否**当前**全部必核验材料均已核验且未漂移。"""
        if self.attestation is None:
            return False
        return bool(self.attestation.verified and self.attestation.all_required_verified)

def _handoff_problems(handoff: IntakeHandoffDocument) -> list[tuple[str, str]]:
    """GOLD-027 handoff 的 schema / 契约版本 / 安全字段不变量（漂移即 fail-closed）。"""
    found: list[tuple[str, str]] = []
    if handoff.schema_version != INTAKE_HANDOFF_SCHEMA_VERSION:
        found.append(
            (
                ChainAuditCode.SCHEMA_DRIFT.value,
                f"handoff schema_version={handoff.schema_version!r} 与当前 "
                f"{INTAKE_HANDOFF_SCHEMA_VERSION} 不一致",
            )
        )
    if handoff.contract_version != INTAKE_HANDOFF_CONTRACT_VERSION:
        found.append(
            (
                ChainAuditCode.SCHEMA_DRIFT.value,
                f"handoff contract_version={handoff.contract_version!r} 与 "
                f"{INTAKE_HANDOFF_CONTRACT_VERSION} 不一致",
            )
        )
    if handoff.blocker_code != PHASE3_3_BLOCKER_CODE:
        found.append(
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                f"handoff blocker_code={handoff.blocker_code!r} 不是 {PHASE3_3_BLOCKER_CODE}",
            )
        )
    for field_name, expected in (
        ("blocker_active", True),
        ("human_gate_required", True),
        ("gate_blocked", True),
        ("data_qualification_passed", False),
        ("phase_transition_allowed", False),
        ("evidence_qualified", False),
        ("advance_allowed", False),
        ("l3_l4_auto_advance_allowed", False),
    ):
        if getattr(handoff, field_name) is not expected:
            found.append(
                (
                    ChainAuditCode.ARTIFACT_TAMPERED.value,
                    f"handoff 声称 {field_name}={getattr(handoff, field_name)!r}："
                    "安全字段不可被改写（fail-closed）",
                )
            )
    return found


def _handoff_facts(ctx: _Context, handoff: IntakeHandoffDocument) -> tuple[tuple[str, str], ...]:
    """handoff 阶段的内容身份事实（id / 摘要 / 计数，不含路径与时间）。"""
    return (
        ("handoff_kind", handoff.kind),
        ("handoff_schema_version", str(handoff.schema_version)),
        ("handoff_contract_version", handoff.contract_version),
        ("handoff_content_sha256", ctx.handoff_digest),
        ("inbox_packages", str(len(handoff.packages))),
        ("preflight_pass", str(handoff.preflight_pass_count)),
        ("synthetic", str(handoff.synthetic_count)),
        ("scanned", str(handoff.scanned).lower()),
    )


def _stage_intake_handoff(ctx: _Context) -> ChainStageResult:
    """① GOLD-027 intake handoff：只读预检**当前**候选目录并取内容身份。"""
    stage = ChainStage.INTAKE_HANDOFF
    root = Path(ctx.inputs.inbox_dir)
    if not root.is_dir():
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (ChainAuditCode.INBOX_UNUSABLE.value,),
            (f"inbox 目录不存在或不是目录：{_safe_path(root)}",),
        )
    try:
        handoff = load_intake_handoff(root, as_of=ctx.moment)
    except InboxError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (ChainAuditCode.INBOX_UNUSABLE.value,),
            (f"候选目录不可用：{_safe(str(exc))}",),
        )
    except ValueError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (ChainAuditCode.ARTIFACT_UNREADABLE.value,),
            (f"handoff 预检失败：{_safe(str(exc))}",),
        )
    ctx.handoff = handoff
    ctx.handoff_digest = handoff_content_sha256(handoff)
    ctx.preflight_pass = handoff.preflight_pass_count
    ctx.synthetic = handoff.synthetic_count
    facts = _handoff_facts(ctx, handoff)
    problems = list(_handoff_problems(handoff))
    # 候选包**当前**未通过 GOLD-011 预检（含 manifest / 文件摘要不一致）→ 该段即 fail-closed：
    # 结构已变（漂移）时，下游凭证 / 批准必然 stale，最早失败应归到"提交材料本身"。
    for package in handoff.packages:
        if package.preflight_pass or package.synthetic:
            continue
        problems.append(
            (
                ChainAuditCode.INVALID_CANDIDATE.value,
                f"候选包 `{package.package_dir}` 当前未通过 GOLD-011 预检"
                f"（inbox_status={package.inbox_status}）：提交材料结构 / 内容摘要不一致，"
                "必须先修正提交材料再重新核验",
            )
        )
    if problems:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            tuple(code for code, _ in problems),
            tuple(detail for _, detail in problems),
            facts,
        )
    return _pass(stage, facts)


def _stage_attestation(ctx: _Context) -> ChainStageResult:
    """② GOLD-028 材料级人工核验凭证：与**当前**候选包逐项重新绑定核验。"""
    stage = ChainStage.ATTESTATION
    path = ctx.inputs.attestation
    if path is None:
        return _missing(stage, "GOLD-028 材料级人工核验凭证（--attestation）")
    try:
        verification = verify_attestation(path, ctx.inputs.inbox_dir, moment=ctx.moment)
    except AttestationError as exc:
        code = _code_from_error(exc, fallback=ChainAuditCode.ARTIFACT_UNREADABLE.value)
        status = (
            ChainStageStatus.MISSING if code.endswith("NOT_FOUND") else ChainStageStatus.FAIL
        )
        return _result(stage, status, (code,), (f"凭证核验未通过：{_safe(str(exc))}",))
    ctx.attestation = verification
    facts: tuple[tuple[str, str], ...] = (
        ("attestation_id", verification.attestation_id),
        ("recomputed_attestation_id", verification.recomputed_attestation_id),
        ("attestation_id_matches", str(verification.attestation_id_matches).lower()),
        ("package_fingerprint", verification.package_fingerprint),
        ("package_content_sha256", verification.package_content_sha256),
        ("handoff_content_sha256", verification.handoff_content_sha256),
        ("all_required_verified", str(verification.all_required_verified).lower()),
        ("preflight_pass_matches", str(verification.preflight_pass_matches).lower()),
        ("scope_matches", str(verification.scope_matches).lower()),
    )
    codes = list(verification.codes)
    details = list(verification.details)
    if not verification.all_required_verified:
        codes.append(InvalidationReason.ATTESTATION_INCOMPLETE.value)
        details.append(
            "凭证未声明全部必核验材料均已人工核验（all_required_verified=false）："
            "材料级人工核验尚未完成（fail-closed）"
        )
    if ctx.handoff_digest and verification.handoff_content_sha256 != ctx.handoff_digest:
        codes.append(ChainAuditCode.BINDING_MISMATCH.value)
        details.append("凭证绑定的 intake handoff 内容身份与本次审计推导的不一致（漂移）")
    if codes:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            tuple(codes) or (ChainAuditCode.BINDING_MISMATCH.value,),
            tuple(details),
            facts,
        )
    return _pass(stage, facts)



def _stage_review_approval(ctx: _Context) -> ChainStageResult:
    """③ GOLD-012 / GOLD-029 review 批准：最新决策必须是带**当前凭证勾稽**的 APPROVE。"""
    stage = ChainStage.REVIEW_APPROVAL
    if ctx.inputs.ledger is None:
        return _missing(stage, "GOLD-012 review ledger（--ledger）")
    try:
        ledger = load_review_ledger(ctx.inputs.ledger)
    except ReviewError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=ChainAuditCode.ARTIFACT_UNREADABLE.value),
            ),
            (f"review ledger 核验未通过：{_safe(str(exc))}",),
        )
    if ledger is None:
        return _missing(stage, "GOLD-012 review ledger（--ledger）")
    try:
        scan = scan_inbox(Path(ctx.inputs.inbox_dir), moment=ctx.moment)
    except InboxError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (ChainAuditCode.INBOX_UNUSABLE.value,),
            (f"候选目录不可用：{_safe(str(exc))}",),
        )
    ctx.ledger = ledger
    ctx.scan = scan
    built = build_approved_intake_list(scan, ledger, moment=ctx.moment)
    facts: list[tuple[str, str]] = [
        ("approve_decisions", str(len(built.approved))),
        ("invalidated_approvals", str(len(built.invalidated))),
        ("attestation_id", ctx.attestation_id),
    ]
    codes: list[str] = []
    details: list[str] = []
    if built.invalidated:
        codes.extend(item.reason_code for item in built.invalidated)
        details.extend(f"{item.reason_code}：{item.detail}" for item in built.invalidated)
    if not built.approved and not codes:
        codes.append(ChainAuditCode.NO_APPROVED_EVIDENCE.value)
        details.append("ledger 中没有任何**当前仍然成立**的 APPROVE 决策（链路停在人工复核之前）")
    for position, entry in enumerate(built.approved, start=1):
        record = ledger.latest_for(entry.fingerprint)
        bound = None if record is None else record.attestation_id
        prefix = f"approved[{position}]"
        facts.extend(
            (
                (f"{prefix}.fingerprint", entry.fingerprint),
                (f"{prefix}.decision_id", entry.decision_id),
                (f"{prefix}.attestation_id", bound or ""),
                (f"{prefix}.revision", str(0 if record is None else record.revision)),
                (f"{prefix}.reason_code", entry.reason_code),
            )
        )
        if bound is None:
            codes.append(InvalidationReason.ATTESTATION_MISSING.value)
            details.append(
                f"{prefix}（指纹 {entry.fingerprint[:16]}…）的 APPROVE 没有 attestation binding："
                "旧口径批准不满足 GOLD-029 门禁（必须在**当前**核验材料上重新批准）"
            )
            continue
        if ctx.attestation_id and bound != ctx.attestation_id:
            codes.append(InvalidationReason.ATTESTATION_TAMPERED.value)
            details.append(
                f"{prefix} 绑定的 attestation_id 与本次核验的 GOLD-028 凭证不一致"
                "（批准 / 凭证不是同一份材料级核验结论）"
            )
    if codes or not built.approved:
        if not codes:
            codes.append(ChainAuditCode.NO_APPROVED_EVIDENCE.value)
        return _result(stage, ChainStageStatus.FAIL, tuple(codes), tuple(details), tuple(facts))
    return _pass(stage, tuple(facts))


def _plan_entry_digests(plan: IntakePlan) -> tuple[tuple[str, str, int, str, tuple[str, ...]], ...]:
    """canonical plan 的条目摘要（与 GOLD-014 ``PlanEntryDocument.digest()`` 同结构 / 同排序口径）。

    note:
        这里**只**把既有 plan 条目的身份字段重新排序比对（不新增任何资格规则）；
        深度绑定核验仍由 GOLD-014 ``verify_intake_receipt`` 负责。
    """
    return tuple(
        sorted(
            (
                entry.fingerprint,
                entry.decision_id,
                entry.revision,
                entry.scope,
                tuple(entry.evidence_files),
            )
            for entry in plan.entries
        )
    )


def _stage_approved_list_and_plan(ctx: _Context) -> ChainStageResult:
    """④ GOLD-012 批准清单 + GOLD-013 plan：与**当前** inbox / ledger 重新核验一致。"""
    stage = ChainStage.APPROVED_LIST_AND_PLAN
    if ctx.inputs.approved_list is None:
        return _missing(stage, "GOLD-012 approved-for-explicit-intake 清单（--approved-list）")
    if ctx.inputs.plan is None:
        return _missing(stage, "GOLD-013 intake plan（--plan）")
    try:
        approved = load_approved_intake_list(ctx.inputs.approved_list)
    except IntakePlanError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=ChainAuditCode.ARTIFACT_UNREADABLE.value),
            ),
            (f"批准清单核验未通过：{_safe(str(exc))}",),
        )
    try:
        plan_doc = load_intake_plan_document(ctx.inputs.plan)
    except IntakeReceiptError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=ChainAuditCode.ARTIFACT_UNREADABLE.value),
            ),
            (f"intake plan 核验未通过：{_safe(str(exc))}",),
        )
    ctx.approved_doc = approved
    ctx.plan_doc = plan_doc
    if ctx.scan is None or ctx.ledger is None:  # pragma: no cover - 前置阶段失败即短路
        raise ChainAuditError("内部状态缺失：批准清单 / plan 阶段缺少前置阶段的只读结论")
    facts: tuple[tuple[str, str], ...] = (
        ("plan_id", plan_doc.plan_id),
        ("plan_status", plan_doc.status),
        ("plan_entries", str(len(plan_doc.entries))),
        ("approved_list_approved_count", str(len(approved.approved))),
        ("approved_list_invalidated_count", str(len(approved.invalidated))),
        ("approved_list_generated_at", approved.generated_at.isoformat()),
    )
    codes: list[str] = []
    details: list[str] = []
    try:
        canonical = build_intake_plan(ctx.scan, ctx.ledger, approved, moment=ctx.moment)
    except IntakePlanInconsistentError as exc:
        codes.extend(item.code for item in exc.violations)
        details.extend(f"{item.code}：{item.detail}" for item in exc.violations)
    else:
        if plan_doc.plan_id != canonical.plan_id:
            codes.append(ReceiptVerificationCode.PLAN_STALE.value)
            details.append(
                "plan_id 与用**当前** inbox / ledger / 批准清单重新算出的不一致："
                "plan 已过期或被改写"
            )
        if plan_doc.entry_digests != _plan_entry_digests(canonical):
            codes.append(ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value)
            details.append(
                "plan 的批准条目（指纹 / decision_id / review revision / scope / 文件清单）"
                "与当前重新核验结果不一致（review override 或内容变化 → 旧批准不继承）"
            )
        if plan_doc.status != canonical.status.value:
            codes.append(ReceiptVerificationCode.PLAN_STALE.value)
            details.append(
                f"plan 的 status（{plan_doc.status}）与当前重新算出的"
                f"（{canonical.status.value}）不一致"
            )
    if plan_doc.generated_at > ctx.moment:
        codes.append(ReceiptVerificationCode.FUTURE_TIMESTAMP.value)
        details.append("plan 审计时点晚于本次审计时点：拒绝未来时间")
    if codes:
        return _result(stage, ChainStageStatus.FAIL, tuple(codes), tuple(details), facts)
    return _pass(stage, facts)



def _stage_operator_result(ctx: _Context) -> ChainStageResult:
    """⑤ 人工**显式**执行结果（演练时由 Mock manifest 提供；绝不自动执行）。"""
    stage = ChainStage.OPERATOR_RESULT
    if not ctx.inputs.operator_results:
        return _missing(stage, "人工显式 Evidence Operator 执行结果（--operator-result）")
    results: list[OperatorResult] = []
    codes: list[str] = []
    details: list[str] = []
    facts: list[tuple[str, str]] = [
        ("operator_result_count", str(len(ctx.inputs.operator_results)))
    ]
    for position, raw in enumerate(ctx.inputs.operator_results, start=1):
        prefix = f"operator[{position}]"
        try:
            result = load_operator_result(raw)
        except IntakeReceiptStateError as exc:
            codes.append(ChainAuditCode.ARTIFACT_TAMPERED.value)
            codes.append(
                _code_from_error(exc, fallback=ReceiptVerificationCode.OPERATOR_TAMPERED.value)
            )
            details.append(f"{prefix} 核验未通过：{_safe(str(exc))}")
            continue
        results.append(result)
        facts.extend(
            (
                (f"{prefix}.scope", result.scope),
                (f"{prefix}.dry_run", str(result.dry_run).lower()),
                (f"{prefix}.persisted", str(result.persisted)),
                (f"{prefix}.input_sha256", result.input_sha256),
                (f"{prefix}.artifact_sha256", result.artifact_sha256),
                (f"{prefix}.generated_at", result.generated_at.isoformat()),
            )
        )
        if result.dry_run:
            codes.append(ReceiptVerificationCode.OPERATOR_NOT_EXECUTED.value)
            details.append(f"{prefix} 仍是 dry-run（人工未**显式**落库）")
        elif result.persisted < 1:
            codes.append(ReceiptVerificationCode.OPERATOR_FAILED.value)
            details.append(f"{prefix} 没有任何落库行（persisted=0）")
    ctx.operator_results = tuple(results)
    if codes or not results:
        return _result(stage, ChainStageStatus.FAIL, tuple(codes), tuple(details), tuple(facts))
    return _pass(stage, tuple(facts))



def _stage_intake_receipt(ctx: _Context) -> ChainStageResult:
    """⑥ GOLD-014 receipt + qualification recheck：执行结果与复核必须与 plan 绑定。"""
    stage = ChainStage.INTAKE_RECEIPT
    if ctx.inputs.receipt is None:
        return _missing(stage, "GOLD-014 intake receipt（--receipt）")
    if ctx.inputs.recheck is None:
        return _missing(stage, "qualification recheck 结果（--recheck）")
    if ctx.plan_doc is None or ctx.scan is None or ctx.ledger is None or ctx.approved_doc is None:
        raise ChainAuditError("内部状态缺失：receipt 阶段缺少前置阶段的只读结论")
    try:
        recheck = load_qualification_recheck(ctx.inputs.recheck)
    except IntakeReceiptStateError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=ReceiptVerificationCode.RECHECK_TAMPERED.value),
            ),
            (f"qualification recheck 核验未通过：{_safe(str(exc))}",),
        )
    ctx.recheck = recheck
    violations = verify_intake_receipt(
        ctx.plan_doc,
        ctx.scan,
        ctx.ledger,
        ctx.approved_doc,
        moment=ctx.moment,
        operator_results=ctx.operator_results,
        recheck=recheck,
    )
    if violations:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            tuple(item.code for item in violations),
            tuple(f"{item.code}：{item.detail}" for item in violations),
        )
    try:
        receipt_doc = load_intake_receipt_document(ctx.inputs.receipt)
    except DecisionPacketError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=PacketVerificationCode.RECEIPT_TAMPERED.value),
            ),
            (f"receipt 文档核验未通过：{_safe(str(exc))}",),
        )
    ctx.receipt_doc = receipt_doc
    rebuilt = build_intake_receipt(
        ctx.plan_doc,
        ctx.scan,
        ctx.ledger,
        ctx.approved_doc,
        moment=ctx.moment,
        operator_results=ctx.operator_results,
        recheck=recheck,
    )
    facts: tuple[tuple[str, str], ...] = (
        ("receipt_id", receipt_doc.receipt.receipt_id),
        ("recomputed_receipt_id", rebuilt.receipt_id),
        ("receipt_status", receipt_doc.receipt.status.value),
        ("plan_id", receipt_doc.receipt.plan_id),
        ("intake_executed", str(receipt_doc.receipt.intake_executed).lower()),
        ("receipt_verified", str(receipt_doc.receipt.receipt_verified).lower()),
        ("landed_rows", str(receipt_doc.receipt.landed_rows)),
        ("recheck_ready", str(recheck.ready).lower()),
    )
    if receipt_doc.receipt.receipt_id != rebuilt.receipt_id:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (PacketVerificationCode.RECEIPT_ID_MISMATCH.value,),
            (
                "receipt_id 与用**当前** plan / 执行结果 / 复核结果重新算出的不一致："
                "收据已被改写或已过期（fail-closed）",
            ),
            facts,
        )
    return _pass(stage, facts)

def _stage_decision_packet(ctx: _Context) -> ChainStageResult:
    """⑦ GOLD-015 decision packet：聚合核验 + packet 文件的**内容寻址**身份。"""
    stage = ChainStage.DECISION_PACKET
    if ctx.inputs.packet is None:
        return _missing(stage, "GOLD-015 decision packet（--packet）")
    if ctx.inputs.handoff_report is None:
        return _missing(stage, "GOLD-008 evidence handoff 报告（--handoff）")
    if (
        ctx.plan_doc is None
        or ctx.scan is None
        or ctx.ledger is None
        or ctx.approved_doc is None
        or ctx.receipt_doc is None
    ):
        raise ChainAuditError("内部状态缺失：decision packet 阶段缺少前置阶段的只读结论")
    try:
        handoff_doc = load_handoff_document(ctx.inputs.handoff_report)
    except DecisionPacketError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=PacketVerificationCode.HANDOFF_TAMPERED.value),
            ),
            (f"GOLD-008 handoff 核验未通过：{_safe(str(exc))}",),
        )
    ctx.handoff_doc = handoff_doc
    readiness: ReadinessStateDocument | None = None
    if ctx.inputs.readiness is not None:
        try:
            readiness = load_readiness_state_document(ctx.inputs.readiness)
        except DecisionPacketError as exc:
            return _result(
                stage,
                ChainStageStatus.FAIL,
                (
                    ChainAuditCode.ARTIFACT_TAMPERED.value,
                    _code_from_error(exc, fallback=PacketVerificationCode.READINESS_TAMPERED.value),
                ),
                (f"readiness state 核验未通过：{_safe(str(exc))}",),
            )
    ctx.readiness = readiness
    violations = verify_decision_packet(
        handoff_doc,
        ctx.plan_doc,
        ctx.scan,
        ctx.ledger,
        ctx.approved_doc,
        moment=ctx.moment,
        readiness=readiness,
        receipt=ctx.receipt_doc,
        operator_results=ctx.operator_results,
        recheck=ctx.recheck,
    )
    if violations:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            tuple(item.code for item in violations),
            tuple(f"{item.code}：{item.detail}" for item in violations),
        )
    try:
        built = build_decision_packet(
            handoff_doc,
            ctx.plan_doc,
            ctx.scan,
            ctx.ledger,
            ctx.approved_doc,
            moment=ctx.moment,
            readiness=readiness,
            receipt=ctx.receipt_doc,
            operator_results=ctx.operator_results,
            recheck=ctx.recheck,
        )
    except DecisionPacketError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=PacketVerificationCode.RECEIPT_MISMATCH.value),
            ),
            (f"decision packet 聚合核验未通过：{_safe(str(exc))}",),
        )
    try:
        binding = load_decision_packet(ctx.inputs.packet, moment=ctx.moment)
    except DecisionRecordError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=PacketVerificationCode.RECEIPT_TAMPERED.value),
            ),
            (f"packet 文件核验未通过：{_safe(str(exc))}",),
        )
    ctx.packet_binding = binding
    facts: tuple[tuple[str, str], ...] = (
        ("packet_id", binding.packet_id),
        ("recomputed_packet_id", built.packet_id),
        ("packet_content_sha256", binding.content_sha256),
        ("packet_artifact_sha256", binding.artifact_sha256),
        ("packet_status", binding.status),
        ("submit_to_l3_human_gate", str(binding.submit_to_l3_human_gate).lower()),
        (
            "evidence_ready_for_human_review",
            str(binding.evidence_ready_for_human_review).lower(),
        ),
        ("receipt_verified", str(binding.receipt_verified).lower()),
        ("qualification_recheck_ready", str(binding.qualification_recheck_ready).lower()),
        ("handoff_artifact_sha256", binding.handoff_artifact_sha256),
        ("plan_id", binding.plan_id),
        ("receipt_id", binding.receipt_id),
    )
    if binding.packet_id != built.packet_id:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (DecisionRecordCode.PACKET_ID_MISMATCH.value,),
            (
                "packet_id 与用**当前** handoff / plan / 批准 / 执行 / 复核结果重新算出的不一致："
                "packet 已被改写或已过期（fail-closed）",
            ),
            facts,
        )
    return _pass(stage, facts)


def _stage_decision_record(ctx: _Context) -> ChainStageResult:
    """⑧ GOLD-016 L3 人工决策记录：与**当前** packet 逐项防伪核验。"""
    stage = ChainStage.DECISION_RECORD
    if ctx.inputs.record is None:
        return _missing(stage, "GOLD-016 L3 人工决策记录（--record）")
    if ctx.inputs.packet is None:
        return _missing(stage, "GOLD-015 decision packet（--packet）")
    try:
        verification = verify_decision_record(
            ctx.inputs.record, ctx.inputs.packet, moment=ctx.moment
        )
    except DecisionRecordError as exc:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            (
                ChainAuditCode.ARTIFACT_TAMPERED.value,
                _code_from_error(exc, fallback=PacketVerificationCode.RECEIPT_TAMPERED.value),
            ),
            (f"决策记录防伪核验未完成：{_safe(str(exc))}",),
        )
    facts: tuple[tuple[str, str], ...] = (
        ("record_id", verification.record_id),
        ("recomputed_record_id", verification.recomputed_record_id),
        ("record_id_matches", str(verification.record_id_matches).lower()),
        ("packet_id_matches", str(verification.packet_id_matches).lower()),
        ("content_sha256_matches", str(verification.content_sha256_matches).lower()),
        ("decision", verification.decision),
        ("revision", str(verification.revision)),
        ("approve_still_valid", str(verification.approve_still_valid).lower()),
    )
    if not verification.verified:
        return _result(
            stage,
            ChainStageStatus.FAIL,
            tuple(verification.codes),
            tuple(verification.details),
            facts,
        )
    return _pass(stage, facts)


#: 阶段 → 核验函数（顺序即审计顺序；不复制任何资格规则）
_STAGE_AUDITORS: Final[tuple[tuple[ChainStage, Callable[[_Context], ChainStageResult]], ...]] = (
    (ChainStage.INTAKE_HANDOFF, _stage_intake_handoff),
    (ChainStage.ATTESTATION, _stage_attestation),
    (ChainStage.REVIEW_APPROVAL, _stage_review_approval),
    (ChainStage.APPROVED_LIST_AND_PLAN, _stage_approved_list_and_plan),
    (ChainStage.OPERATOR_RESULT, _stage_operator_result),
    (ChainStage.INTAKE_RECEIPT, _stage_intake_receipt),
    (ChainStage.DECISION_PACKET, _stage_decision_packet),
    (ChainStage.DECISION_RECORD, _stage_decision_record),
)


def audit_evidence_chain(
    inputs: ChainAuditInputs,
    *,
    moment: datetime,
    evidence_source: str = EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    scenario: str | None = None,
) -> EvidenceChainAudit:
    """**逐段**核验整条 Phase 3.3 证据链（**纯只读 / 零网络 / 零数据库 / 零写入**）。

    审计按 :data:`CHAIN_STAGE_ORDER` 顺序进行：任一段未通过即**短路**，其后阶段标为
    ``NOT_EVALUATED``（原因码 ``STAGE_NOT_EVALUATED``），``earliest_failure_stage`` 明确指向
    最早出问题的那一段。本函数**不写任何文件**（唯一写入口是 :func:`run_chain_audit` 的
    ``out_path``），也**绝不**修改任何历史 artifact。

    Args:
        inputs: 显式给出的 artifact 路径（缺省 = 该段未提供 → ``MISSING``）。
        moment: 审计时点（必须带时区；用于"未来时间"判定，**不做任何时间填补**）。
        evidence_source: ``rehearsal_fixture`` 或 ``operator_provided``（受控词表）。
        scenario: 演练场景标识（非演练为 ``None``）。

    Returns:
        :class:`EvidenceChainAudit`；``data_qualification_passed`` /
        ``phase_transition_allowed`` 恒为 ``False``，``blocker_active`` /
        ``human_gate_required`` 恒为 ``True``（**硬编码**）。

    Raises:
        ChainAuditArgumentError: 时点缺时区 / ``evidence_source`` 不在受控词表内。
    """
    moment = _require_aware(moment, field_name="moment")
    if evidence_source not in EVIDENCE_SOURCES:
        allowed = ", ".join(EVIDENCE_SOURCES)
        raise ChainAuditArgumentError(f"evidence_source 必须是 {allowed} 之一")
    ctx = _Context(inputs=inputs, moment=moment)
    stages: list[ChainStageResult] = []
    failed = False
    for stage, auditor in _STAGE_AUDITORS:
        if failed:
            stages.append(
                _result(
                    stage,
                    ChainStageStatus.NOT_EVALUATED,
                    (ChainAuditCode.STAGE_NOT_EVALUATED.value,),
                    ("更早阶段未通过（短路）：本段未评估",),
                )
            )
            continue
        outcome = auditor(ctx)
        stages.append(outcome)
        failed = not outcome.passed
    frozen = tuple(stages)
    facts_digest = compute_chain_facts_digest(frozen)
    notes: tuple[str, ...] = (CHAIN_AUDIT_NOTE, CHAIN_AUDIT_SEMANTICS_NOTE)
    if evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE:
        notes = (*notes, CHAIN_REHEARSAL_SEMANTICS_NOTE)
    return EvidenceChainAudit(
        generated_at=moment,
        inbox_dir=str(inputs.inbox_dir),
        evidence_source=evidence_source,
        scenario=scenario,
        stages=frozen,
        facts_digest=facts_digest,
        chain_id=compute_chain_id(
            stages=frozen,
            facts_digest=facts_digest,
            evidence_source=evidence_source,
            scenario=scenario,
        ),
        real_evidence_missing=_real_evidence_missing(ctx, evidence_source),
        human_verification_missing=_human_verification_missing(ctx, evidence_source),
        inputs=inputs,
        notes=notes,
    )


def _real_evidence_missing(ctx: _Context, evidence_source: str) -> bool:
    """工程审计是否仍存在**结构性**证据缺口（**绝不**据此宣布资格通过）。

    note:
        - 演练（fixture）**恒为** ``True``：fixture / Mock **不是**真实资格证据；
        - 非演练：仅当**存在**非合成、结构完整（``preflight_pass``）的候选包**且**本次核验的
          GOLD-028 凭证声明全部必核验材料已人工核验时才为 ``False``；真实性 / 法律效力仍
          **只能**由人工核验与 L3 人工 Gate 判定。
    """
    if evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE:
        return True
    if ctx.handoff is None or not ctx.handoff.scanned:
        return True
    if ctx.preflight_pass < 1 or ctx.synthetic > 0:
        return True
    return not ctx.attestation_complete


def _human_verification_missing(ctx: _Context, evidence_source: str) -> bool:
    """材料级人工核验是否缺失（演练 **恒为** ``True``：fixture 凭证不是真实人工核验）。"""
    if evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE:
        return True
    return not ctx.attestation_complete




# ---------------------------------------------------------------------------
# 只读运行入口（默认零写入；只有显式 out_path 才原子落盘审计报告本身）
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise ChainAuditWriteError(
            f"{what}写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def run_chain_audit(
    inputs: ChainAuditInputs,
    *,
    moment: datetime,
    evidence_source: str = EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    scenario: str | None = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> EvidenceChainAudit:
    """执行**一次**证据链审计（默认只读；只有显式 ``out_path`` 才写审计报告本身）。

    安全语义：

    - 所有输入 artifact 只读；损坏 / 被篡改 / 漂移 → 相应阶段 ``FAIL``（**零写入**）；
    - 只有 ``out_path`` 才写**审计报告本身**，且先取**单实例锁**（锁冲突 → 稳定异常，零写入）；
    - 本函数**没有**任何 intake / commit / 数据库 / 网络调用，**绝不**移动 / 删除 / 改写
      inbox 内原始 evidence 或任何历史 artifact，**绝不**修改 ``PROJECT_STATE``、
      **绝不**解除 ``PHASE3_3_DATA``、**绝不**切换 Phase。

    Raises:
        ChainAuditArgumentError: ``moment`` 未带时区 / ``evidence_source`` 非法。
        ChainAuditPathError: inbox 目录不可用，或输出 artifact 与 inbox 目录冲突。
        ChainAuditWriteError: 审计报告原子写失败（fail-closed）。
        LockConflictError: 另一处审计写入正持有活动锁（fail-closed，零写入）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(inputs.inbox_dir)
    if not root.is_dir():
        raise ChainAuditPathError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    out = Path(out_path) if out_path is not None else None
    if out is not None:
        try:
            ensure_outside_inbox(root, out)
        except InboxError as exc:
            raise ChainAuditPathError(_safe(str(exc), max_chars=300)) from exc
    if out is None:
        return audit_evidence_chain(
            inputs, moment=moment, evidence_source=evidence_source, scenario=scenario
        )
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + CHAIN_AUDIT_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        audit = audit_evidence_chain(
            inputs, moment=moment, evidence_source=evidence_source, scenario=scenario
        )
        _atomic_write_json(out, audit.to_dict(), what="证据链审计报告")
    return replace(audit, written_path=_safe_path(out))



def render_chain_audit_summary(audit: EvidenceChainAudit) -> str:
    """渲染人类可读的审计摘要（脱敏；**不解除** blocker、**不**代表资格通过）。"""
    earliest = audit.earliest_failure_stage
    lines: list[str] = [
        "# Phase 3.3 证据链端到端审计（**只读**，不是资格判定器）",
        "",
        f"- 审计时点（UTC）：{audit.generated_at.isoformat()}",
        f"- 证据来源：`{audit.evidence_source}`"
        + (f"（场景 `{audit.scenario}`）" if audit.scenario else ""),
        f"- chain_id：`{audit.chain_id}`；facts_digest：`{audit.facts_digest}`",
        f"- engineering_chain_ready：**{str(audit.engineering_chain_ready).lower()}**"
        f"（通过 {audit.passed_stage_count}/{len(audit.stages)} 段；"
        f"失败 {audit.failed_stage_count} 段）",
        f"- 最早失败阶段：`{'—' if earliest is None else earliest.value}`",
        f"- real_evidence_missing：{str(audit.real_evidence_missing).lower()}；"
        f"human_verification_missing：{str(audit.human_verification_missing).lower()}；"
        f"l3_human_gate_pending：{str(audit.l3_human_gate_pending).lower()}",
        f"- blocker `{PHASE3_3_BLOCKER_CODE}`：active={str(audit.blocker_active).lower()}；"
        f"human_gate_required={str(audit.human_gate_required).lower()}；"
        "`data_qualification_passed` / `phase_transition_allowed` 恒为 false",
        "",
        "## 逐段结论",
        "",
        "| 阶段 | 状态 | 稳定原因码 |",
        "|---|---|---|",
    ]
    for item in audit.stages:
        codes = "、".join(f"`{code}`" for code in item.reason_codes) or "—"
        lines.append(f"| {item.stage.value} | {item.status.value} | {codes} |")
    lines += ["", "## 故障定位（人工作业口径）", ""]
    if earliest is None:
        lines.append(
            "- 全段 PASS：工程链契约一致（**仍不是**资格通过；真实证据 / 人工核验 / L3 Gate "
            "仍需人工完成）。"
        )
    else:
        lines.append(
            f"- 从**最早失败阶段** `{earliest.value}` 开始排查：该段 artifact 与**当前**上游状态"
            "不一致（缺失 / 损坏 / 被篡改 / 漂移），必须先按稳定原因码修复该段，再重新审计。"
        )
    lines += [
        "- 各段稳定原因码的含义直接取自对应模块：`ATTESTATION_*`（GOLD-028/029）、"
        "`PLAN_*` / `LEDGER_*`（GOLD-013/014）、`OPERATOR_*` / `RECHECK_*`（GOLD-014）、"
        "`HANDOFF_*` / `READINESS_*` / `RECEIPT_*` / `PACKET_*`（GOLD-015/016）、"
        "`CHAIN_*`（本层：artifact 缺失 / 不可读 / 被篡改 / 绑定不一致 / schema 漂移）。",
        "- 修复后请**同时**重新生成下游 artifact（旧结论绝不静默继承），再跑一次本审计确认。",
    ]
    lines += ["", "## 口径与边界", ""]
    lines.extend(f"- {note}" for note in audit.notes)
    lines.append("")
    return "\n".join(lines)
