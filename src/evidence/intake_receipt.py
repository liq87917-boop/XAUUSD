"""Evidence 显式 Intake **Receipt** 与资格复核审计闭环（**GOLD-014**，纯本地只读核验）。

GOLD-013 交付了**最终写入前**的只读 intake plan 门禁：它把 GOLD-012 的批准清单与**当前**
inbox / review ledger 重新绑定，给出内容级 ``plan_id`` 与**字符串**形式的显式 operator 命令模板。
但"人工到底有没有**显式**执行过 intake、执行的是不是**被批准的那一版内容**、执行之后有没有做过
qualification recheck"过去只存在于人的记忆与聊天记录里：没有任何机器可核验的收据。

本模块补上**执行之后**的确定性审计层：把 ①GOLD-013 plan、②**当前** inbox / review 状态、
③人工**显式** Evidence Operator 的结构化执行结果（``--no-dry-run --manifest`` 产出的
``EvidenceIntakeReport.to_dict()``）、④随后的人工 / 脚本 qualification recheck 结果
（``phase33_qualification_recheck``）绑定成**确定性、脱敏、内容寻址**（``receipt_id``）的
**只读** receipt。

- **纯本地 / 只读**：只**读**显式给出的 plan / inbox / ledger / approved list / 执行结果 /
  复核结果；只**复用** GOLD-013 的 plan 重新绑定口径
  （:func:`~src.evidence.intake_plan.build_intake_plan`）与 GOLD-012 的 ledger 加载口径
  （``load_review_ledger``），**不复制、不降低**任何资格规则；默认**零写入**，只有显式
  ``--out`` 才**原子**落盘 receipt 本身；本模块**没有**任何 intake / commit 调用，**绝不**写
  研究数据库，**绝不**移动 / 删除 / 改写 inbox 内任何原始 evidence，**绝不**联网，**绝不**修改
  ``.ai/PROJECT_STATE.json`` 或解除 blocker；
- **重新绑定（逐项 fail-closed）**：①plan 文档必须**结构自洽**（文档标识 / schema / 契约版本 /
  四个安全字段与 ``auto_intake_allowed`` / ``writes_database`` 不可被 state 削弱 / 条目白名单键 /
  **不得**出现任何证据时间键 / ``plan_id`` 为 64 位小写十六进制）；②plan 必须**当前仍然成立**
  （用**当前** inbox + ledger + approved list 重新算出的 ``plan_id`` 必须与文档一致：plan stale /
  fingerprint drift / 候选消失 / preflight 回退 / **review override** 一律
  :class:`IntakeReceiptVerificationError`，**零写入**）；③人工显式执行结果必须与**被批准的内容**
  按**内容 SHA-256** 绑定（不是文件名 / mtime）、scope 必须与批准条目一致，且**必须真的执行过**
  （``dry_run=false``、``persisted>=1``；dry-run / 空落库 / 计数自相矛盾 → fail-closed）；
  ④qualification recheck 必须存在且**晚于**显式执行（早于执行 / 缺失 / 被篡改 → fail-closed）；
- **绝不把操作时间当证据**：``receipt_at`` / ``plan.generated_at`` /
  ``operator_results[].generated_at`` / ``qualification_recheck.as_of`` **都只是审计操作时间**，
  本模块产出里**根本没有** ``published_at`` / ``collected_at`` / ``effective_at`` /
  ``available_at`` / ``availability_provenance`` / ``availability_reference`` 这些证据时间键
  （加载输入时也会拒绝含这些键的文档），并且**不会**由操作时间推导任何证据时间；
- **明确区分四个布尔**：``intake_executed``（人工显式落库确实发生）与 ``receipt_verified``
  （本 receipt 的绑定核验通过）是两个**独立**字段；它们为 true **绝不蕴含**
  ``data_qualification_passed``（**恒为** ``false``）或 ``phase_transition_allowed``
  （**恒为** ``false``）。``blocker_active`` / ``human_gate_required`` 恒为 ``true``（**硬编码**），
  ``PHASE3_3_DATA`` **保持 BLOCKED**，Phase 切换仍须 ``.ai/DEVELOPMENT_PROTOCOL.md`` 的
  **L3 人工 Gate**；``qualification_recheck`` 只是"复核发生过"的事实记录（含 ``ready`` / gate
  计数），**不是**放行结论；
- **内容级 ``receipt_id`` / 幂等**：``receipt_id`` 只由（策略块 + ``plan_id`` + 批准条目摘要 +
  执行结果摘要 + 复核结果摘要 + 两个布尔 + 状态）派生，**不含** ``receipt_at``；同一输入重复生成
  得到同一 ``receipt_id``（文档在**同一审计时点**下逐字节稳定）；plan / review revision / 指纹 /
  执行结果 / 复核结果**任一变化**必然产生新 ``receipt_id`` 或直接 fail-closed，旧 receipt
  **绝不静默继承**。

入口：``scripts/evidence_intake_receipt.py``（``--inbox-dir`` / ``--ledger`` / ``--approved-list``
/ ``--plan`` 必填；``--operator-result`` 可重复、``--recheck`` 可选；``--out`` 是**唯一**写开关；
**没有**任何 intake / ``--no-dry-run`` 参数）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common import hashing
from src.common.redaction import safe_text
from src.evidence import readiness_runner as runner
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.inbox import (
    CandidatePackage,
    InboxError,
    InboxPreflightReport,
    ensure_outside_inbox,
    scan_inbox,
)
from src.evidence.intake_plan import (
    INTAKE_PLAN_KIND,
    INTAKE_PLAN_SCHEMA_VERSION,
    OPERATOR_EXPLICIT_FLAG,
    ApprovedListDocument,
    IntakePlan,
    IntakePlanInconsistentError,
    IntakePlanStateError,
    build_intake_plan,
    load_approved_intake_list,
)
from src.evidence.readiness_watch import MAX_CODE_CHARS, atomic_write_text
from src.evidence.report import REPORT_TITLE as OPERATOR_MANIFEST_REPORT
from src.evidence.review import (
    FORBIDDEN_EVIDENCE_FIELDS,
    ReviewLedger,
    ReviewLedgerStateError,
    load_review_ledger,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "INTAKE_RECEIPT_FILE_NAME",
    "INTAKE_RECEIPT_KIND",
    "INTAKE_RECEIPT_LOCK_SUFFIX",
    "INTAKE_RECEIPT_REPORT_NAME",
    "INTAKE_RECEIPT_SCHEMA_VERSION",
    "OPERATOR_MANIFEST_REPORT",
    "PLAN_ENTRY_DOCUMENT_KEYS",
    "QUALIFICATION_RECHECK_REPORT",
    "RECEIPT_EXECUTION_MODE",
    "IntakePlanDocument",
    "IntakeReceipt",
    "IntakeReceiptArgumentError",
    "IntakeReceiptError",
    "IntakeReceiptPathError",
    "IntakeReceiptStateError",
    "IntakeReceiptStatus",
    "IntakeReceiptVerificationError",
    "IntakeReceiptWriteError",
    "OperatorResult",
    "PlanEntryDocument",
    "QualificationRecheck",
    "ReceiptEntry",
    "ReceiptVerificationCode",
    "ReceiptViolation",
    "build_intake_receipt",
    "compute_receipt_id",
    "exit_code_for",
    "load_intake_plan_document",
    "load_operator_result",
    "load_qualification_recheck",
    "render_intake_receipt_summary",
    "run_intake_receipt",
    "verify_intake_receipt",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
INTAKE_RECEIPT_SCHEMA_VERSION: Final[int] = 1
#: receipt 文档标识 / 报告标识（稳定，供上游与人工日志解析）
INTAKE_RECEIPT_KIND: Final[str] = "evidence_intake_receipt"
INTAKE_RECEIPT_REPORT_NAME: Final[str] = "evidence_intake_receipt"
#: receipt 的默认文件名（operator 放在显式工作目录里；**默认不写**）
INTAKE_RECEIPT_FILE_NAME: Final[str] = "evidence_intake_receipt.json"
#: receipt 的单实例锁后缀（与 ``out_path`` 同级）
INTAKE_RECEIPT_LOCK_SUFFIX: Final[str] = ".lock"
#: 执行模式（**恒为只读核验**：本工具不执行任何 intake）
RECEIPT_EXECUTION_MODE: Final[str] = "READ_ONLY_RECEIPT_VERIFICATION"
#: qualification recheck 产物的 ``report`` 标识（**复用** GOLD-006/007 一键复核的既有载荷标识，
#: 见 ``scripts/evidence_readiness.py`` 的 ``_recheck_payload``，不另造一份产物契约）
QUALIFICATION_RECHECK_REPORT: Final[str] = "phase33_qualification_recheck"
#: 证据时间键（与 GOLD-012 同一常量来源；receipt 里**绝不**允许出现这些键）
_FORBIDDEN_KEYS: Final[tuple[str, ...]] = FORBIDDEN_EVIDENCE_FIELDS
#: plan 条目必须包含的字段（GOLD-013 ``PlanEntry.to_dict()`` 的**全量**键集合）
PLAN_ENTRY_DOCUMENT_KEYS: Final[tuple[str, ...]] = (
    "fingerprint",
    "decision_id",
    "revision",
    "reviewer",
    "reviewed_at",
    "reason_code",
    "package_dir",
    "evidence_type",
    "source",
    "operator_scope",
    "evidence_files",
    "rows",
    "acceptable_rows",
    "approved_for_explicit_intake",
    "data_qualification_passed",
    "requires_explicit_operator_action",
)
#: Evidence Operator 执行结果（manifest）里必须出现的计数键（GOLD-005 ``IntakeCounts.to_dict()``）
_OPERATOR_COUNT_KEYS: Final[tuple[str, ...]] = (
    "rows",
    "accepted",
    "quarantined",
    "duplicate",
    "oos_eligible",
    "not_oos_eligible",
    "persisted",
    "sources_created",
    "processed_success",
)
#: 64 位小写十六进制（``plan_id`` / 指纹 / ``decision_id`` / 内容摘要统一校验）
_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
#: 支持的证据类别（直接取自 ``evidence-intake-v1`` 契约，**不另造词表**）
_EVIDENCE_SCOPES: Final[frozenset[str]] = frozenset(member.value for member in EvidenceScope)

#: 固定说明：receipt 只做**执行之后**的只读审计，不解除 blocker、不切 Phase、不自动 intake
INTAKE_RECEIPT_NOTE: Final[str] = (
    "本工具只做**执行之后**的**只读**审计：把 GOLD-013 plan、**当前** inbox / review 状态、"
    "人工**显式** Evidence Operator 执行结果与随后的 qualification recheck 绑定成内容寻址的 "
    "receipt；它**不写数据库**、**不调用任何 intake / commit**、**不移动 / 删除 / 改写**任何原始 "
    "evidence，也**不会**解除 `PHASE3_3_DATA`。`intake_executed` / `receipt_verified` 只说明"
    "「人工显式落库发生过」与「本 receipt 的绑定核验通过」，**绝不等于** data qualification PASS；"
    "`data_qualification_passed` / `phase_transition_allowed` 恒为 false；"
    "Phase 切换仍是 **L3 人工 Gate**。"
)
#: 固定说明：receipt 里的时间**只是审计操作时间**，绝不当证据时间
EVIDENCE_TIME_SEMANTICS_NOTE: Final[str] = (
    "receipt 只记录审计操作时间（receipt_at / plan.generated_at / "
    "operator_results[].generated_at / qualification_recheck.as_of）；它们**不是**证据时间，"
    "绝不用于冒充 published_at、collected_at、effective_at 或 availability / OOS 证据，"
    "也不会由它们推导任何证据时间。"
)
#: 固定说明：下一步仍是人工动作
NEXT_STEP_NOTE: Final[str] = (
    "本 receipt 只是**审计收据**：真实落库必须由人工**显式**执行 "
    "`scripts.evidence_operator.py workflow --no-dry-run`（执行前补显式 `--manifest`）；"
    "若要放宽任何数据资格，必须由业务方提供经人工核验的真实授权证据并完成 **L3 人工 Gate**，"
    "本工具不具备解除 `PHASE3_3_DATA` 的能力。"
)


class IntakeReceiptStatus(StrEnum):
    """receipt 状态（``VERIFIED_*`` 也只表示"执行与复核都已绑定成功"，**不是**资格通过）。"""

    VERIFIED_EXECUTION_RECORDED = "VERIFIED_EXECUTION_RECORDED"
    BLOCKED_NO_APPROVED_EVIDENCE = "BLOCKED_NO_APPROVED_EVIDENCE"


class ReceiptVerificationCode(StrEnum):
    """执行后核验的稳定原因码（任何一条都意味着 **fail-closed**，绝不产出 receipt）。"""

    # ---- plan 侧（结构 / 当前一致性）-------------------------------------
    PLAN_TAMPERED = "PLAN_TAMPERED"
    PLAN_STALE = "PLAN_STALE"
    PLAN_ENTRY_MISMATCH = "PLAN_ENTRY_MISMATCH"
    # ---- 时点语义（操作时间不得越界，也不得冒充证据时间）-------------------
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    INTAKE_BEFORE_PLAN = "INTAKE_BEFORE_PLAN"
    # ---- 人工显式执行结果（manifest）------------------------------------
    OPERATOR_RESULT_MISSING = "OPERATOR_RESULT_MISSING"
    OPERATOR_UNBOUND = "OPERATOR_UNBOUND"
    OPERATOR_NOT_EXECUTED = "OPERATOR_NOT_EXECUTED"
    OPERATOR_FAILED = "OPERATOR_FAILED"
    OPERATOR_TAMPERED = "OPERATOR_TAMPERED"
    OPERATOR_SCOPE_MISMATCH = "OPERATOR_SCOPE_MISMATCH"
    OPERATOR_INPUT_MISMATCH = "OPERATOR_INPUT_MISMATCH"
    OPERATOR_COVERAGE_INCOMPLETE = "OPERATOR_COVERAGE_INCOMPLETE"
    # ---- qualification recheck 侧 --------------------------------------
    RECHECK_MISSING = "RECHECK_MISSING"
    RECHECK_UNBOUND = "RECHECK_UNBOUND"
    RECHECK_TAMPERED = "RECHECK_TAMPERED"
    RECHECK_BEFORE_INTAKE = "RECHECK_BEFORE_INTAKE"


#: plan 核验原因码（GOLD-013）→ receipt 侧稳定原因码（**同一含义不另造词**）
_PLAN_CODE_MAP: Final[Mapping[str, str]] = {
    "APPROVED_LIST_TAMPERED": ReceiptVerificationCode.PLAN_TAMPERED.value,
    "APPROVED_LIST_STALE": ReceiptVerificationCode.PLAN_STALE.value,
    "INVALIDATED_MISMATCH": ReceiptVerificationCode.PLAN_STALE.value,
    "COUNT_MISMATCH": ReceiptVerificationCode.PLAN_STALE.value,
    "PLAN_TIME_BEFORE_APPROVAL": ReceiptVerificationCode.PLAN_STALE.value,
    "LEDGER_MISSING_APPROVAL": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "LEDGER_DECISION_SUPERSEDED": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "LEDGER_REVISION_SUPERSEDED": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "FINGERPRINT_MISSING": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "PREFLIGHT_NOT_PASSING": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "SYNTHETIC_EVIDENCE": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
    "EVIDENCE_INCONSISTENT": ReceiptVerificationCode.PLAN_ENTRY_MISMATCH.value,
}


class IntakeReceiptError(RuntimeError):
    """receipt 层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class IntakeReceiptArgumentError(IntakeReceiptError):
    """参数错误（缺 plan / inbox / ledger / approved list，时区缺失等）。"""


class IntakeReceiptStateError(IntakeReceiptError):
    """plan / 执行结果 / 复核结果 / ledger 损坏、被篡改或被削弱安全字段（fail-closed，零写入）。"""


class IntakeReceiptPathError(IntakeReceiptError):
    """inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。"""


class IntakeReceiptVerificationError(IntakeReceiptError):
    """执行后核验不通过（**fail-closed**：绝不产出 receipt，绝不把执行结果当成资格）。"""

    def __init__(self, violations: Sequence[ReceiptViolation]) -> None:
        self.violations: tuple[ReceiptViolation, ...] = tuple(violations)
        codes = sorted({item.code for item in self.violations})
        detail = "、".join(codes[:8]) if codes else "UNKNOWN"
        super().__init__(
            "intake receipt 核验不通过（fail-closed；零写入、绝不把执行当成资格）："
            f"{len(self.violations)} 项不一致；原因码：{detail}"
        )


class IntakeReceiptWriteError(IntakeReceiptError):
    """receipt 原子写失败（**fail-closed**）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: receipt 已生成且**执行与复核绑定成功**（仍**不是**资格通过）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数错误（缺 plan / inbox / ledger / approved list，时区缺失等）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输入 / 输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: plan / 执行结果 / 复核结果 / ledger 损坏被篡改或核验不通过（全部 fail-closed）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: receipt 已生成但**当前没有任何**仍成立的批准（**预期 BLOCKED**，不是故障）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一个 receipt 生成 / 计划 / 复核 / 重扫正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_RECEIPT_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (IntakeReceiptArgumentError, EXIT_CONFIG_ERROR),
    (IntakeReceiptStateError, EXIT_STATE_INVALID),
    (IntakeReceiptVerificationError, EXIT_STATE_INVALID),
    (IntakeReceiptPathError, EXIT_UNUSABLE),
    (IntakeReceiptWriteError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _RECEIPT_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（原因文本 / 来源名 / 路径共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径类展示：脱敏 + 截断（绝不回显凭据）。"""
    return _safe(value, max_chars=max_chars)


def _require_aware(moment: datetime, *, field_name: str) -> datetime:
    """要求带时区并统一到 UTC（禁止隐式时区）。"""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise IntakeReceiptArgumentError(f"{field_name} 必须包含时区（禁止隐式时区）")
    return moment.astimezone(UTC)


def _parse_aware_moment(raw: object, *, field_name: str) -> datetime:
    """解析 ISO8601 时间戳并要求带时区（state 文件里的时间一律显式校验）。"""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise IntakeReceiptStateError(f"{field_name} 无法解析为 ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntakeReceiptStateError(f"{field_name} 必须带时区")
    return parsed


def _require_hex64(raw: object, *, field_name: str) -> str:
    """要求 64 位小写十六进制（指纹 / ``plan_id`` / ``decision_id`` / 内容摘要）。"""
    text = str(raw).strip().lower()
    if not _HEX64.match(text):
        raise IntakeReceiptStateError(
            f"{field_name} 必须是 64 位小写十六进制（文件名 / mtime / 操作时间都不是指纹）"
        )
    return text


def _require_int(raw: object, *, field_name: str) -> int:
    """要求非负整数（排除 ``bool``，避免 ``True`` 被当成 1）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise IntakeReceiptStateError(f"{field_name} 必须是整数")
    if raw < 0:
        raise IntakeReceiptStateError(f"{field_name} 不能为负数")
    return raw


def _reject_evidence_time_fields(payload: object) -> None:
    """**绝不**允许证据时间键出现在 plan / 执行结果里（复核 / 执行时间都不是证据时间）。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key) in _FORBIDDEN_KEYS:
                raise IntakeReceiptStateError(
                    f"文档含禁止的证据时间字段 {_safe(key, max_chars=40)}："
                    "fail-closed（操作时间一律不得当作证据时间）"
                )
            _reject_evidence_time_fields(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            _reject_evidence_time_fields(item)


def _policy_block() -> dict[str, Any]:
    """策略块（四个安全字段 + 只读执行模式；**硬编码**，与批准 / 执行数量无关）。"""
    return {
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "receipt_execution_mode": RECEIPT_EXECUTION_MODE,
        "auto_intake_allowed": False,
        "writes_database": False,
        "requires_explicit_operator_action": True,
        "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
        "human_gate_level": "L3",
    }


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise IntakeReceiptWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


# ---------------------------------------------------------------------------
# GOLD-013 intake plan 的严格只读视图（**不复制**其生成 / 核验逻辑）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlanEntryDocument:
    """plan 文档里**一条**已核验的批准（只保留白名单键；≠ 资格）。"""

    fingerprint: str
    decision_id: str
    revision: int
    operator_scope: str
    evidence_files: tuple[str, ...]
    rows: int
    acceptable_rows: int
    package_dir: str = ""

    def digest(self) -> tuple[str, str, int, str, tuple[str, ...]]:
        """内容级绑定摘要（指纹 / 决策 / review revision / scope / 文件清单）。"""
        return (
            self.fingerprint,
            self.decision_id,
            self.revision,
            self.operator_scope,
            tuple(self.evidence_files),
        )

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "revision": self.revision,
            "operator_scope": self.operator_scope,
            "package_dir": _safe_path(self.package_dir),
            "evidence_files": [_safe_path(name) for name in self.evidence_files],
            "rows": self.rows,
            "acceptable_rows": self.acceptable_rows,
            "approved_for_explicit_intake": True,
            "data_qualification_passed": False,
            "requires_explicit_operator_action": True,
        }


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:

    """严格读取一个本地 JSON 对象（不存在 / 不可读 / 非对象 → fail-closed）。"""
    if not path.exists():
        raise IntakeReceiptStateError(f"{what} 不存在：{_safe_path(path)}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeReceiptStateError(f"{what} 不可读：{_safe_path(path)}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntakeReceiptStateError(f"{what} 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise IntakeReceiptStateError(f"{what} 顶层必须是 JSON 对象")
    return payload


@dataclass(frozen=True, slots=True)
class IntakePlanDocument:
    """已严格校验的 GOLD-013 intake plan（**只读视图**；结构自洽 + 未削弱安全字段）。"""

    generated_at: datetime
    plan_id: str
    status: str
    inbox_dir: str
    approved_list_generated_at: str | None
    entries: tuple[PlanEntryDocument, ...] = ()

    @property
    def entry_digests(self) -> tuple[tuple[str, str, int, str, tuple[str, ...]], ...]:
        """按指纹排序的条目摘要（确定性；用于与**当前**重新算出的 plan 比对）。"""
        return tuple(sorted(entry.digest() for entry in self.entries))

    @property
    def has_approved(self) -> bool:
        """plan 声明的、**当前仍成立**的批准条数是否大于 0。"""
        return bool(self.entries)


def _plan_safety_checks(payload: Mapping[str, Any]) -> None:
    """plan 文档的安全字段**不可被 state 削弱**（fail-closed）。"""
    for field_name, expected in (
        ("blocker_active", True),
        ("human_gate_required", True),
        ("data_qualification_passed", False),
        ("phase_transition_allowed", False),
        ("auto_intake_allowed", False),
        ("writes_database", False),
        ("requires_explicit_operator_action", True),
    ):
        if field_name in payload and payload[field_name] is not expected:
            raise IntakeReceiptStateError(
                f"plan 声称 {field_name}={payload[field_name]!r}："
                "安全字段不可被 state 改写（fail-closed）"
            )


def _plan_entry_view(item: Mapping[str, Any], *, index: int) -> PlanEntryDocument:
    """把 plan 里的一条批准规范化为**白名单**视图（缺字段 / 多余键 / 类型非法 → fail-closed）。"""
    missing = [key for key in PLAN_ENTRY_DOCUMENT_KEYS if key not in item]
    if missing:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] 缺字段：{'、'.join(sorted(missing))}"
        )
    extra = sorted(set(item) - set(PLAN_ENTRY_DOCUMENT_KEYS))
    if extra:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] 含未授权字段："
            f"{_safe('、'.join(extra), max_chars=120)}（fail-closed）"
        )
    if item["approved_for_explicit_intake"] is not True:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] 未标记为已批准：fail-closed"
        )
    if item["data_qualification_passed"] is not False:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] 声称 data_qualification_passed="
            f"{item['data_qualification_passed']!r}：批准**不等于**资格（fail-closed）"
        )
    if item["requires_explicit_operator_action"] is not True:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] requires_explicit_operator_action "
            "被改写为 false：fail-closed（绝不自动 intake）"
        )
    scope = str(item["operator_scope"]).strip().lower()
    if scope not in _EVIDENCE_SCOPES:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] operator_scope 非法："
            f"{_safe(scope, max_chars=40)}"
        )
    raw_files = item["evidence_files"]
    if isinstance(raw_files, str) or not isinstance(raw_files, Sequence) or not raw_files:
        raise IntakeReceiptStateError(
            f"plan 条目 approved_for_explicit_intake[{index}] evidence_files 必须是非空数组"
        )
    return PlanEntryDocument(
        fingerprint=_require_hex64(
            item["fingerprint"], field_name=f"plan 条目[{index}] fingerprint"
        ),
        decision_id=_require_hex64(
            item["decision_id"], field_name=f"plan 条目[{index}] decision_id"
        ),
        revision=_require_int(item["revision"], field_name=f"plan 条目[{index}] revision"),
        operator_scope=scope,
        evidence_files=tuple(_safe_path(name) for name in raw_files),
        rows=_require_int(item["rows"], field_name=f"plan 条目[{index}] rows"),
        acceptable_rows=_require_int(
            item["acceptable_rows"], field_name=f"plan 条目[{index}] acceptable_rows"
        ),
        package_dir=_safe_path(item["package_dir"]),
    )


def load_intake_plan_document(path: Path | str) -> IntakePlanDocument:
    """读取 GOLD-013 intake plan 并做**严格**校验（损坏 / 篡改 → fail-closed）。

    Raises:
        IntakeReceiptStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 文档标识 / schema /
            契约版本 / 安全字段 / ``plan_id`` / 条目结构 / 证据时间键 任一不合法。
    """
    target = Path(path)
    payload = _read_json_object(target, what="GOLD-013 intake plan")
    if payload.get("kind") != INTAKE_PLAN_KIND:
        raise IntakeReceiptStateError("文件不是 GOLD-013 evidence intake plan")
    if payload.get("schema_version") != INTAKE_PLAN_SCHEMA_VERSION:
        raise IntakeReceiptStateError(
            f"plan schema_version 不受支持：{payload.get('schema_version')!r}"
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
        raise IntakeReceiptStateError(
            f"plan contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
        )
    _plan_safety_checks(payload)
    _reject_evidence_time_fields(payload)
    plan_id = _require_hex64(payload.get("plan_id"), field_name="plan plan_id")
    status = str(payload.get("status") or "")
    raw_entries = payload.get("approved_for_explicit_intake")
    if not isinstance(raw_entries, list):
        raise IntakeReceiptStateError("plan approved_for_explicit_intake 必须是数组")
    entries: list[PlanEntryDocument] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_entries, start=1):
        if not isinstance(item, Mapping):
            raise IntakeReceiptStateError(
                f"plan 条目 approved_for_explicit_intake[{index}] 必须是对象"
            )
        view = _plan_entry_view(item, index=index)
        if view.fingerprint in seen:
            raise IntakeReceiptStateError(
                f"plan 条目 approved_for_explicit_intake[{index}] 指纹重复"
            )
        seen.add(view.fingerprint)
        entries.append(view)
    declared = payload.get("approved_for_explicit_intake_count")
    if isinstance(declared, bool) or not isinstance(declared, int) or declared != len(entries):
        raise IntakeReceiptStateError(
            f"plan approved_for_explicit_intake_count={declared!r} 与列表长度 "
            f"{len(entries)} 不一致：结构不自洽（fail-closed）"
        )
    if payload.get("data_qualification_passed_count") not in (0, None):
        raise IntakeReceiptStateError(
            "plan data_qualification_passed_count 不为 0：计划**不是**资格判定（fail-closed）"
        )
    approved_at = payload.get("approved_list_generated_at")
    return IntakePlanDocument(
        generated_at=_parse_aware_moment(
            payload.get("generated_at"), field_name="plan generated_at"
        ),
        plan_id=plan_id,
        status=status,
        inbox_dir=_safe_path(payload.get("inbox_dir")),
        approved_list_generated_at=(str(approved_at) if approved_at is not None else None),
        entries=tuple(sorted(entries, key=lambda item: item.fingerprint)),
    )


# ---------------------------------------------------------------------------
# 人工**显式** Evidence Operator 执行结果（GOLD-005/007 的 manifest 产物）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OperatorResult:
    """一次人工**显式** intake 执行结果（GOLD-005 ``EvidenceIntakeReport`` manifest 只读视图）。"""

    path: str
    artifact_sha256: str
    report: str
    scope: str
    dry_run: bool
    generated_at: datetime
    input_path: str
    input_sha256: str
    counts: Mapping[str, int]
    row_count: int

    @property
    def persisted(self) -> int:
        """落到 ``raw_items`` 的行数（``>=1`` 才说明**真的执行过**）。"""
        return int(self.counts["persisted"])

    @property
    def quarantined(self) -> int:
        """被隔离的行数（隔离行**绝不**计入可信证据）。"""
        return int(self.counts["quarantined"])

    def digest(self) -> tuple[str, str, bool, str, int, str]:
        """内容级绑定摘要（scope / 输入内容摘要 / 执行模式 / 落库数 / 产物摘要）。"""
        return (
            self.scope,
            self.input_sha256,
            self.dry_run,
            self.generated_at.isoformat(),
            self.persisted,
            self.artifact_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含任何证据时间）。"""
        return {
            "path": _safe_path(self.path),
            "artifact_sha256": self.artifact_sha256,
            "report": self.report,
            "scope": self.scope,
            "dry_run": self.dry_run,
            "generated_at": self.generated_at.isoformat(),
            "input_path": _safe_path(self.input_path),
            "input_sha256": self.input_sha256,
            "counts": {key: int(value) for key, value in sorted(self.counts.items())},
            "row_count": self.row_count,
            "explicit_execution": not self.dry_run,
            "landed_rows": self.persisted,
        }


def load_operator_result(path: Path | str) -> OperatorResult:
    """读取人工显式 Evidence Operator 的执行结果并做**严格**校验（损坏 → fail-closed）。

    Raises:
        IntakeReceiptStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 报告标识 / scope /
            时间戳 / 输入摘要 / 计数结构 任一不合法。
    """
    target = Path(path)
    payload = _read_json_object(target, what="Evidence Operator 执行结果（manifest）")
    if str(payload.get("report") or "") != OPERATOR_MANIFEST_REPORT:
        raise IntakeReceiptStateError(
            "执行结果不是 Evidence Operator 的 `--no-dry-run --manifest` 产物"
            f"（report={_safe(payload.get('report'), max_chars=60)!r}）"
        )
    _reject_evidence_time_fields(payload)
    dry_run = payload.get("dry_run")
    if not isinstance(dry_run, bool):
        raise IntakeReceiptStateError("执行结果 dry_run 必须是布尔值")
    scope = str(payload.get("scope") or "").strip().lower()
    if scope not in _EVIDENCE_SCOPES:
        raise IntakeReceiptStateError(
            f"执行结果 scope 非法：{_safe(scope, max_chars=40)}"
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
        raise IntakeReceiptStateError(
            f"执行结果 contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
        )
    input_block = payload.get("input")
    if not isinstance(input_block, Mapping):
        raise IntakeReceiptStateError("执行结果 input 必须是对象")
    counts_block = payload.get("counts")
    if not isinstance(counts_block, Mapping):
        raise IntakeReceiptStateError("执行结果 counts 必须是对象")
    counts: dict[str, int] = {}
    for key in _OPERATOR_COUNT_KEYS:
        if key not in counts_block:
            raise IntakeReceiptStateError(f"执行结果 counts 缺字段：{key}")
        counts[key] = _require_int(counts_block[key], field_name=f"执行结果 counts.{key}")
    rows_block = payload.get("rows")
    if not isinstance(rows_block, list):
        raise IntakeReceiptStateError("执行结果 rows 必须是数组")
    return OperatorResult(
        path=_safe_path(target),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        report=str(payload["report"]),
        scope=scope,
        dry_run=dry_run,
        generated_at=_parse_aware_moment(
            payload.get("generated_at"), field_name="执行结果 generated_at"
        ),
        input_path=_safe_path(input_block.get("path")),
        input_sha256=_require_hex64(input_block.get("sha256"), field_name="执行结果 input.sha256"),
        counts=counts,
        row_count=len(rows_block),
    )


# ---------------------------------------------------------------------------
# qualification recheck 结果（GOLD-006/007 一键复核的只读视图）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QualificationRecheck:
    """一次 qualification recheck 的只读事实（``ready`` 只是复核结论，**不是**放行）。"""

    path: str
    artifact_sha256: str
    report: str
    as_of: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    ready: bool
    qualification_ready: bool
    readiness_ready: bool
    qualification_pass_count: int
    qualification_blocked_count: int
    readiness_blocked_scope_count: int

    def digest(self) -> tuple[str, str, bool, str]:
        """内容级绑定摘要（报告标识 / 复核时点 / ``ready`` / 产物摘要）。"""
        return (self.report, self.as_of.isoformat(), self.ready, self.artifact_sha256)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**脱敏**；不含 evidence 窗口与任何证据时间）。"""
        return {
            "path": _safe_path(self.path),
            "artifact_sha256": self.artifact_sha256,
            "report": self.report,
            "as_of": self.as_of.isoformat(),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "ready": self.ready,
            "gate": {
                "qualification_ready": self.qualification_ready,
                "qualification_pass_count": self.qualification_pass_count,
                "qualification_blocked_count": self.qualification_blocked_count,
                "readiness_ready": self.readiness_ready,
                "readiness_blocked_scope_count": self.readiness_blocked_scope_count,
            },
            "is_qualification_decision": False,
            "human_gate_required_for_phase_change": True,
        }


def load_qualification_recheck(path: Path | str) -> QualificationRecheck:
    """读取 qualification recheck 结果并做**严格**校验（损坏 / 被篡改 → fail-closed）。

    Raises:
        IntakeReceiptStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 报告标识 / 契约版本 /
            时点 / 安全字段 / gate 结构 任一不合法。
    """
    target = Path(path)
    payload = _read_json_object(target, what="qualification recheck 结果")
    if str(payload.get("report") or "") != QUALIFICATION_RECHECK_REPORT:
        raise IntakeReceiptStateError(
            "复核结果不是 phase33 qualification recheck 产物"
            f"（report={_safe(payload.get('report'), max_chars=60)!r}）"
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
        raise IntakeReceiptStateError(
            f"复核结果 contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
        )
    blocker_code = str(payload.get("blocker_code") or "")
    blocker_active = payload.get("blocker_active")
    human_gate_required = payload.get("human_gate_required")
    if not isinstance(blocker_active, bool) or not isinstance(human_gate_required, bool):
        raise IntakeReceiptStateError("复核结果 blocker_active / human_gate_required 必须是布尔值")
    ready = payload.get("ready")
    if not isinstance(ready, bool):
        raise IntakeReceiptStateError("复核结果 ready 必须是布尔值")
    gate = payload.get("gate")
    if not isinstance(gate, Mapping):
        raise IntakeReceiptStateError("复核结果 gate 必须是对象")
    return QualificationRecheck(
        path=_safe_path(target),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        report=str(payload["report"]),
        as_of=_parse_aware_moment(payload.get("as_of"), field_name="复核结果 as_of"),
        blocker_code=blocker_code,
        blocker_active=blocker_active,
        human_gate_required=human_gate_required,
        ready=ready,
        qualification_ready=bool(gate.get("qualification_ready")),
        readiness_ready=bool(gate.get("readiness_ready")),
        qualification_pass_count=_require_int(
            gate.get("qualification_pass_count"),
            field_name="复核结果 gate.qualification_pass_count",
        ),
        qualification_blocked_count=_require_int(
            gate.get("qualification_blocked_count"),
            field_name="复核结果 gate.qualification_blocked_count",
        ),
        readiness_blocked_scope_count=_require_int(
            gate.get("readiness_blocked_scope_count"),
            field_name="复核结果 gate.readiness_blocked_scope_count",
        ),
    )


# ---------------------------------------------------------------------------
# receipt 文档（确定性 / 脱敏 / 内容寻址）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReceiptViolation:
    """一条核验不一致（**稳定原因码** + 已脱敏说明；任一条都让 receipt fail-closed）。"""

    code: str
    fingerprint: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "fingerprint": self.fingerprint,
            "detail": _safe(self.detail, max_chars=300),
        }


def _violation(
    code: ReceiptVerificationCode, fingerprint: str, detail: str
) -> ReceiptViolation:
    """构造一条核验不一致（统一脱敏）。"""
    return ReceiptViolation(
        code=code.value, fingerprint=fingerprint, detail=_safe(detail, max_chars=300)
    )


@dataclass(frozen=True, slots=True)
class ReceiptEntry:
    """已**按内容**绑定到人工显式执行结果的一条批准（≠ 资格）。"""

    fingerprint: str
    decision_id: str
    revision: int
    operator_scope: str
    package_dir: str
    evidence_files: tuple[str, ...]
    content_sha256: tuple[str, ...]
    rows: int
    acceptable_rows: int
    executed: bool

    def digest(self) -> tuple[str, str, int, str, tuple[str, ...], bool]:
        """内容级绑定摘要（指纹 / 决策 / review revision / scope / 文件摘要 / 是否已执行）。"""
        return (
            self.fingerprint,
            self.decision_id,
            self.revision,
            self.operator_scope,
            tuple(self.content_sha256),
            self.executed,
        )

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "revision": self.revision,
            "operator_scope": self.operator_scope,
            "package_dir": _safe_path(self.package_dir),
            "evidence_files": [_safe_path(name) for name in self.evidence_files],
            "content_sha256": list(self.content_sha256),
            "rows": self.rows,
            "acceptable_rows": self.acceptable_rows,
            "intake_executed": self.executed,
            "approved_for_explicit_intake": True,
            "data_qualification_passed": False,
            "requires_explicit_operator_action": True,
        }


@dataclass(frozen=True, slots=True)
class IntakeReceipt:
    """一次执行后审计的结果（**确定性脱敏**；默认零写入，``written_path`` 才落盘）。"""

    receipt_at: datetime
    status: IntakeReceiptStatus
    plan_id: str
    plan_status: str
    plan_generated_at: datetime
    approved_list_generated_at: str | None
    inbox_dir: str
    plan_path: str | None = None
    ledger_path: str | None = None
    approved_list_path: str | None = None
    entries: tuple[ReceiptEntry, ...] = ()
    ledger_digest: tuple[tuple[str, int, str], ...] = ()
    operator_results: tuple[OperatorResult, ...] = ()
    recheck: QualificationRecheck | None = None
    inbox_packages: int = 0
    preflight_pass: int = 0
    ledger_fingerprints: int = 0
    receipt_id: str = ""
    written_path: str | None = None
    notes: tuple[str, ...] = (INTAKE_RECEIPT_NOTE,)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return INTAKE_RECEIPT_KIND

    @property
    def intake_executed(self) -> bool:
        """人工**显式**落库是否确实发生（**绝不**蕴含 ``data_qualification_passed``）。"""
        return (
            self.status is IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED
            and bool(self.entries)
            and all(entry.executed for entry in self.entries)
        )

    @property
    def receipt_verified(self) -> bool:
        """本 receipt 的绑定核验是否通过（**绝不**蕴含资格通过或 Phase 切换）。"""
        return self.status is IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED

    @property
    def approved_count(self) -> int:
        """已绑定的批准条数（不是资格计数）。"""
        return len(self.entries)

    @property
    def landed_rows(self) -> int:
        """所有显式执行结果声明的落库行数合计（只是执行事实，不是资格）。"""
        return sum(result.persisted for result in self.operator_results)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段与两个布尔**显式且恒定**）。"""
        return {
            "kind": INTAKE_RECEIPT_KIND,
            "report": INTAKE_RECEIPT_REPORT_NAME,
            "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "receipt_at": self.receipt_at.isoformat(),
            "receipt_id": self.receipt_id,
            "status": self.status.value,
            "execution_mode": RECEIPT_EXECUTION_MODE,
            "inbox_dir": _safe_path(self.inbox_dir),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "human_gate_level": "L3",
            "intake_executed": self.intake_executed,
            "receipt_verified": self.receipt_verified,
            "data_qualification_passed_count": 0,
            "auto_intake_allowed": False,
            "writes_database": False,
            "requires_explicit_operator_action": True,
            "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
            "plan": {
                "path": _safe_path(self.plan_path) if self.plan_path is not None else None,
                "plan_id": self.plan_id,
                "status": self.plan_status,
                "generated_at": self.plan_generated_at.isoformat(),
                "approved_list_generated_at": self.approved_list_generated_at,
                "ledger_path": _safe_path(self.ledger_path) if self.ledger_path else None,
                "approved_list_path": (
                    _safe_path(self.approved_list_path) if self.approved_list_path else None
                ),
            },
            "executed_entries": [entry.to_dict() for entry in self.entries],
            "executed_entry_count": len(self.entries),
            "landed_rows": self.landed_rows,
            "operator_results": [result.to_dict() for result in self.operator_results],
            "qualification_recheck": (
                self.recheck.to_dict() if self.recheck is not None else None
            ),
            "ledger_revision_digest": [
                {"fingerprint": fingerprint, "revision": revision, "decision_id": decision_id}
                for fingerprint, revision, decision_id in self.ledger_digest
            ],
            "verification": {
                "violations": [],
                "revalidated_at": self.receipt_at.isoformat(),
                "inbox_packages": self.inbox_packages,
                "preflight_pass": self.preflight_pass,
                "ledger_fingerprints": self.ledger_fingerprints,
            },
            "evidence_time_semantics": {
                "contains_evidence_times": False,
                "audit_times_only": [
                    "receipt_at",
                    "plan.generated_at",
                    "operator_results[].generated_at",
                    "qualification_recheck.as_of",
                ],
                "note": EVIDENCE_TIME_SEMANTICS_NOTE,
            },
            "written_path": self.written_path,
            "next_step": NEXT_STEP_NOTE,
            "notes": list(self.notes),
        }


def _operator_sort_key(result: OperatorResult) -> tuple[str, str, str]:
    """执行结果的确定性排序键（产物摘要 → scope → 输入摘要）。"""
    return (result.artifact_sha256, result.scope, result.input_sha256)


def compute_receipt_id(receipt: IntakeReceipt) -> str:
    """内容级 ``receipt_id``：只由（策略块 + plan / 条目 / 执行 / 复核摘要 + 两个布尔）派生。

    ⚠️ 刻意**不含** ``receipt_at``：同一输入重复生成得到**同一** ``receipt_id``；plan、review
    revision、指纹、执行结果或复核结果任一变化 → 必然产生新 ``receipt_id``（旧 receipt 不继承）。
    """
    payload = json.dumps(
        {
            "policy": _policy_block(),
            "plan_id": receipt.plan_id,
            "status": receipt.status.value,
            "entries": [
                list(entry.digest())
                for entry in sorted(receipt.entries, key=lambda item: item.fingerprint)
            ],
            "ledger": [
                [fingerprint, int(revision), decision_id]
                for fingerprint, revision, decision_id in receipt.ledger_digest
            ],
            "operator": [
                list(result.digest())
                for result in sorted(receipt.operator_results, key=_operator_sort_key)
            ],
            "recheck": (
                list(receipt.recheck.digest()) if receipt.recheck is not None else None
            ),
            "intake_executed": receipt.intake_executed,
            "receipt_verified": receipt.receipt_verified,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashing.sha256_text(payload)


# ---------------------------------------------------------------------------
# 执行后核验（fail-closed；**复用** GOLD-013 的 plan 重新绑定口径）
# ---------------------------------------------------------------------------
def _canonical_plan(
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
) -> IntakePlan:
    """用**当前** inbox + ledger + 批准清单重新算出 canonical plan（直接复用 GOLD-013）。"""
    return build_intake_plan(scan, ledger, approved_list, moment=moment)


def _plan_violations(error: IntakePlanInconsistentError) -> tuple[ReceiptViolation, ...]:
    """把 GOLD-013 的 plan 核验不一致映射为 receipt 侧稳定原因码（**同一含义不另造词**）。"""
    mapped = [
        ReceiptViolation(
            code=_PLAN_CODE_MAP.get(item.code, ReceiptVerificationCode.PLAN_STALE.value),
            fingerprint=item.fingerprint,
            detail=_safe(item.detail, max_chars=300),
        )
        for item in error.violations
    ]
    if not mapped:
        mapped.append(
            _violation(
                ReceiptVerificationCode.PLAN_STALE,
                "",
                "plan 与当前 inbox / review ledger 重新核验不一致",
            )
        )
    return tuple(mapped)


def _canonical_digests(
    canonical: IntakePlan,
) -> tuple[tuple[str, str, int, str, tuple[str, ...]], ...]:
    """canonical plan 的条目摘要（与 :meth:`PlanEntryDocument.digest` 同结构，确定性排序）。"""
    return tuple(
        sorted(
            (
                entry.fingerprint,
                entry.decision_id,
                entry.revision,
                entry.scope,
                tuple(entry.evidence_files),
            )
            for entry in canonical.entries
        )
    )


def _expected_files(entry: PlanEntryDocument, package: CandidatePackage) -> dict[str, str]:
    """候选包内该批准条目声明的证据文件 → **实测**内容 SHA-256（只认已核对摘要的声明文件）。"""
    by_name: dict[str, str] = {}
    for ref in package.files:
        if not ref.digest_verified:
            continue
        by_name[ref.path] = ref.sha256
        by_name[Path(ref.path).name] = ref.sha256
    found: dict[str, str] = {}
    for name in entry.evidence_files:
        sha = by_name.get(name) or by_name.get(Path(name).name)
        if sha is not None:
            found[name] = sha
    return found


def _operator_integrity_violations(result: OperatorResult) -> list[ReceiptViolation]:
    """执行结果**内部计数自洽**检查（被改写 / 被损坏 → fail-closed）。"""
    counts = result.counts
    detail = _safe_path(result.path)
    violations: list[ReceiptViolation] = []
    if result.row_count != counts["rows"]:
        violations.append(
            _violation(
                ReceiptVerificationCode.OPERATOR_TAMPERED,
                "",
                f"执行结果 rows 条数与 counts.rows 不一致（{detail}）",
            )
        )
    if counts["accepted"] + counts["quarantined"] + counts["duplicate"] > counts["rows"]:
        violations.append(
            _violation(
                ReceiptVerificationCode.OPERATOR_TAMPERED,
                "",
                f"执行结果 counts 自相矛盾（accepted + quarantined + duplicate > rows：{detail}）",
            )
        )
    if counts["persisted"] > counts["accepted"]:
        violations.append(
            _violation(
                ReceiptVerificationCode.OPERATOR_TAMPERED,
                "",
                f"执行结果 counts 自相矛盾（persisted > accepted：{detail}）",
            )
        )
    return violations


def _verify_coverage(
    document: IntakePlanDocument,
    packages: Mapping[str, CandidatePackage],
    operator_results: Sequence[OperatorResult],
) -> list[ReceiptViolation]:
    """逐条批准按**内容 SHA-256** 核对执行结果覆盖度（缺失 / 错 scope / 未执行 → fail-closed）。"""
    violations: list[ReceiptViolation] = []
    expected_hashes: set[str] = set()
    for entry in document.entries:
        package = packages.get(entry.fingerprint)
        if package is None:
            violations.append(
                _violation(
                    ReceiptVerificationCode.PLAN_ENTRY_MISMATCH,
                    entry.fingerprint,
                    "批准的候选包当前不在 inbox 扫描结果中（内容已变化 → 新指纹，或已被移除）",
                )
            )
            continue
        expected = _expected_files(entry, package)
        if not expected:
            violations.append(
                _violation(
                    ReceiptVerificationCode.OPERATOR_INPUT_MISMATCH,
                    entry.fingerprint,
                    "批准条目声明的证据文件在当前候选包中找不到：无法按内容核对执行结果",
                )
            )
            continue
        expected_hashes.update(expected.values())
        for name, sha in sorted(expected.items()):
            same_input = [item for item in operator_results if item.input_sha256 == sha]
            if not same_input:
                violations.append(
                    _violation(
                        ReceiptVerificationCode.OPERATOR_COVERAGE_INCOMPLETE,
                        entry.fingerprint,
                        f"没有执行结果使用与批准内容一致的文件（按内容 SHA-256 绑定）：{name}",
                    )
                )
                continue
            scoped = [item for item in same_input if item.scope == entry.operator_scope]
            if not scoped:
                violations.append(
                    _violation(
                        ReceiptVerificationCode.OPERATOR_SCOPE_MISMATCH,
                        entry.fingerprint,
                        f"执行结果 scope 与批准条目（{entry.operator_scope}）不一致：{name}",
                    )
                )
                continue
            if any(not item.dry_run and item.persisted >= 1 for item in scoped):
                continue
            if all(item.dry_run for item in scoped):
                violations.append(
                    _violation(
                        ReceiptVerificationCode.OPERATOR_NOT_EXECUTED,
                        entry.fingerprint,
                        f"该证据文件只有 dry-run 执行结果（未人工显式落库）：{name}",
                    )
                )
            else:
                violations.append(
                    _violation(
                        ReceiptVerificationCode.OPERATOR_FAILED,
                        entry.fingerprint,
                        f"该证据文件的显式执行没有落库任何行：{name}",
                    )
                )
    for result in operator_results:
        if result.input_sha256 not in expected_hashes:
            violations.append(
                _violation(
                    ReceiptVerificationCode.OPERATOR_INPUT_MISMATCH,
                    "",
                    "执行结果使用的输入与任何被批准内容都不一致"
                    f"（可能落库了**未被批准**的内容）：{_safe_path(result.path)}",
                )
            )
    return violations


def _verify_recheck(
    recheck: QualificationRecheck | None,
    operator_results: Sequence[OperatorResult],
    *,
    moment: datetime,
) -> list[ReceiptViolation]:
    """qualification recheck 的时点 / 安全字段 / 顺序核验（早于执行 / 被改写 → fail-closed）。"""
    if recheck is None:
        return []
    violations: list[ReceiptViolation] = []
    if recheck.as_of > moment:
        violations.append(
            _violation(
                ReceiptVerificationCode.FUTURE_TIMESTAMP,
                "",
                "qualification recheck 时点晚于本次 receipt 审计时点：拒绝未来时间",
            )
        )
    if recheck.blocker_code != PHASE3_3_BLOCKER_CODE:
        violations.append(
            _violation(
                ReceiptVerificationCode.RECHECK_TAMPERED,
                "",
                f"复核结果 blocker_code={_safe(recheck.blocker_code, max_chars=40)!r} "
                f"与 {PHASE3_3_BLOCKER_CODE} 不一致",
            )
        )
    if not recheck.blocker_active or not recheck.human_gate_required:
        violations.append(
            _violation(
                ReceiptVerificationCode.RECHECK_TAMPERED,
                "",
                "复核结果声称 blocker 已解除或无需人工 Gate：安全字段不可被改写（fail-closed）",
            )
        )
    latest_intake = max((item.generated_at for item in operator_results), default=None)
    if latest_intake is not None and recheck.as_of < latest_intake:
        violations.append(
            _violation(
                ReceiptVerificationCode.RECHECK_BEFORE_INTAKE,
                "",
                "qualification recheck 早于显式执行：该复核没有覆盖本次落库（必须重新复核）",
            )
        )
    return violations


def _verify(
    document: IntakePlanDocument,
    canonical: IntakePlan,
    scan: InboxPreflightReport,
    *,
    moment: datetime,
    operator_results: Sequence[OperatorResult],
    recheck: QualificationRecheck | None,
) -> tuple[ReceiptViolation, ...]:
    """把 plan 文档、**当前**状态、执行结果与复核结果逐项比对（返回**全部**不一致）。"""
    violations: list[ReceiptViolation] = []
    if document.generated_at > moment:
        violations.append(
            _violation(
                ReceiptVerificationCode.FUTURE_TIMESTAMP,
                "",
                "plan 审计时点晚于本次 receipt 审计时点：拒绝（禁止未来时间）",
            )
        )
    if document.plan_id != canonical.plan_id:
        violations.append(
            _violation(
                ReceiptVerificationCode.PLAN_STALE,
                "",
                "plan_id 与用**当前** inbox / ledger / 批准清单重新算出的不一致："
                "plan 已过期或被改写（必须重新生成 plan）",
            )
        )
    if document.entry_digests != _canonical_digests(canonical):
        violations.append(
            _violation(
                ReceiptVerificationCode.PLAN_ENTRY_MISMATCH,
                "",
                "plan 的批准条目（指纹 / decision_id / review revision / scope / 文件清单）"
                "与当前重新核验结果不一致（review override 或内容变化 → 旧批准不继承）",
            )
        )
    if not canonical.entries:
        if operator_results:
            violations.append(
                _violation(
                    ReceiptVerificationCode.OPERATOR_UNBOUND,
                    "",
                    "当前没有任何仍成立的批准，却给出了执行结果：拒绝把无绑定批准的执行记成收据",
                )
            )
        if recheck is not None:
            violations.append(
                _violation(
                    ReceiptVerificationCode.RECHECK_UNBOUND,
                    "",
                    "当前没有任何仍成立的批准，却给出了 qualification recheck：拒绝无绑定复核",
                )
            )
        return tuple(violations)

    if not operator_results:
        violations.append(
            _violation(
                ReceiptVerificationCode.OPERATOR_RESULT_MISSING,
                "",
                "当前存在仍成立的批准，但没有任何人工显式 Evidence Operator 执行结果",
            )
        )
    if recheck is None:
        violations.append(
            _violation(
                ReceiptVerificationCode.RECHECK_MISSING,
                "",
                "当前存在仍成立的批准，但没有任何 qualification recheck 结果",
            )
        )
    for result in operator_results:
        if result.generated_at > moment:
            violations.append(
                _violation(
                    ReceiptVerificationCode.FUTURE_TIMESTAMP,
                    "",
                    f"执行结果审计时点晚于本次 receipt 审计时点：{_safe_path(result.path)}",
                )
            )
        if result.generated_at < document.generated_at:
            violations.append(
                _violation(
                    ReceiptVerificationCode.INTAKE_BEFORE_PLAN,
                    "",
                    "执行结果早于 plan 生成时间：顺序不成立（必须先核验计划再显式落库）",
                )
            )
        if result.dry_run:
            violations.append(
                _violation(
                    ReceiptVerificationCode.OPERATOR_NOT_EXECUTED,
                    "",
                    f"执行结果仍是 dry-run（人工未显式落库）：{_safe_path(result.path)}",
                )
            )
        elif result.persisted < 1:
            violations.append(
                _violation(
                    ReceiptVerificationCode.OPERATOR_FAILED,
                    "",
                    f"执行结果没有任何落库行（persisted=0）：{_safe_path(result.path)}",
                )
            )
        violations.extend(_operator_integrity_violations(result))

    packages = {package.fingerprint: package for package in scan.packages}
    violations.extend(_verify_coverage(document, packages, operator_results))
    violations.extend(_verify_recheck(recheck, operator_results, moment=moment))
    return tuple(violations)


def verify_intake_receipt(
    document: IntakePlanDocument,
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    operator_results: Sequence[OperatorResult] = (),
    recheck: QualificationRecheck | None = None,
) -> tuple[ReceiptViolation, ...]:
    """**只读**核验：plan 是否仍成立，且人工显式执行 + qualification recheck 是否与之绑定。

    Returns:
        核验不一致列表（空 = 可以生成 receipt）。任何一条都意味着调用方必须 **fail-closed**。
    """
    moment = _require_aware(moment, field_name="moment")
    try:
        canonical = _canonical_plan(scan, ledger, approved_list, moment=moment)
    except IntakePlanInconsistentError as exc:
        return _plan_violations(exc)
    return _verify(
        document,
        canonical,
        scan,
        moment=moment,
        operator_results=operator_results,
        recheck=recheck,
    )


def _entry_content_hashes(
    entry: PlanEntryDocument, packages: Mapping[str, CandidatePackage]
) -> tuple[str, ...]:
    """条目声明证据文件的**实测**内容摘要（排序；核验通过后调用，缺失即 fail-closed）。"""
    package = packages.get(entry.fingerprint)
    if package is None:
        raise IntakeReceiptStateError(
            f"候选包在核验后消失：{_safe(entry.fingerprint[:16], max_chars=16)}（fail-closed）"
        )
    hashes = tuple(sorted(_expected_files(entry, package).values()))
    if not hashes:
        raise IntakeReceiptStateError(
            f"条目的证据文件摘要不可核对："
            f"{_safe(entry.fingerprint[:16], max_chars=16)}（fail-closed）"
        )
    return hashes


def build_intake_receipt(
    document: IntakePlanDocument,
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    operator_results: Sequence[OperatorResult] = (),
    recheck: QualificationRecheck | None = None,
    plan_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    approved_list_path: Path | str | None = None,
) -> IntakeReceipt:
    """核验 → 生成**确定性脱敏** receipt（**零写入**；不一致 → fail-closed）。

    Raises:
        IntakeReceiptArgumentError: ``moment`` 未带时区。
        IntakeReceiptVerificationError: 任一执行后核验不通过（**零写入**，绝不把执行当资格）。
    """
    moment = _require_aware(moment, field_name="moment")
    try:
        canonical = _canonical_plan(scan, ledger, approved_list, moment=moment)
    except IntakePlanInconsistentError as exc:
        raise IntakeReceiptVerificationError(_plan_violations(exc)) from exc
    results = tuple(sorted(operator_results, key=_operator_sort_key))
    violations = _verify(
        document,
        canonical,
        scan,
        moment=moment,
        operator_results=results,
        recheck=recheck,
    )
    if violations:
        raise IntakeReceiptVerificationError(violations)
    packages = {package.fingerprint: package for package in scan.packages}
    entries = tuple(
        ReceiptEntry(
            fingerprint=entry.fingerprint,
            decision_id=entry.decision_id,
            revision=entry.revision,
            operator_scope=entry.operator_scope,
            package_dir=entry.package_dir,
            evidence_files=tuple(entry.evidence_files),
            content_sha256=_entry_content_hashes(entry, packages),
            rows=entry.rows,
            acceptable_rows=entry.acceptable_rows,
            executed=True,
        )
        for entry in document.entries
    )
    status = (
        IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED
        if entries
        else IntakeReceiptStatus.BLOCKED_NO_APPROVED_EVIDENCE
    )
    receipt = IntakeReceipt(
        receipt_at=moment,
        status=status,
        plan_id=document.plan_id,
        plan_status=document.status,
        plan_generated_at=document.generated_at,
        approved_list_generated_at=document.approved_list_generated_at,
        inbox_dir=document.inbox_dir,
        plan_path=str(plan_path) if plan_path is not None else None,
        ledger_path=str(ledger_path) if ledger_path is not None else None,
        approved_list_path=(
            str(approved_list_path) if approved_list_path is not None else None
        ),
        entries=entries,
        ledger_digest=tuple(canonical.ledger_digest),
        operator_results=results,
        recheck=recheck,
        inbox_packages=len(scan.packages),
        preflight_pass=len(scan.preflight_pass),
        ledger_fingerprints=len(ledger.latest_decisions()),
    )
    return replace(receipt, receipt_id=compute_receipt_id(receipt))


# ---------------------------------------------------------------------------
# 只读运行入口（默认零写入；只有显式 out_path 才原子落盘 receipt 本身）
# ---------------------------------------------------------------------------
def _compose_receipt(
    root: Path,
    *,
    moment: datetime,
    plan_path: Path,
    ledger_path: Path,
    approved_list_path: Path,
    operator_result_paths: Sequence[Path],
    recheck_path: Path | None,
) -> IntakeReceipt:
    """读 plan（只读）→ ledger（只读）→ 批准清单（只读）→ 扫描 inbox（只读）→ 核验生成收据。"""
    document = load_intake_plan_document(plan_path)
    try:
        ledger: ReviewLedger | None = load_review_ledger(ledger_path)
    except ReviewLedgerStateError as exc:
        raise IntakeReceiptStateError(_safe(str(exc), max_chars=300)) from exc
    if ledger is None:
        # 文件不存在 / 未给出 = 没有任何历史决策（不是"损坏"）；此时 plan 必须也没有批准条目，
        # 否则重新核验会判 fail-closed。
        ledger = ReviewLedger(generated_at=moment)
    try:
        approved_list = load_approved_intake_list(approved_list_path)
    except IntakePlanStateError as exc:
        raise IntakeReceiptStateError(_safe(str(exc), max_chars=300)) from exc
    try:
        scan = scan_inbox(root, moment=moment)
    except InboxError as exc:
        raise IntakeReceiptPathError(_safe(str(exc), max_chars=300)) from exc
    results = tuple(load_operator_result(path) for path in operator_result_paths)
    recheck = load_qualification_recheck(recheck_path) if recheck_path is not None else None
    return build_intake_receipt(
        document,
        scan,
        ledger,
        approved_list,
        moment=moment,
        operator_results=results,
        recheck=recheck,
        plan_path=plan_path,
        ledger_path=ledger_path,
        approved_list_path=approved_list_path,
    )


def run_intake_receipt(
    inbox_dir: Path | str,
    *,
    moment: datetime,
    plan_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    approved_list_path: Path | str | None = None,
    operator_result_paths: Sequence[Path | str] = (),
    recheck_path: Path | str | None = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> IntakeReceipt:
    """执行**一次**收据核验（默认只读；只有显式 ``out_path`` 才写 receipt 文件）。

    安全语义：

    - plan / ledger / 批准清单 / 执行结果 / 复核结果全部只读；损坏 / 被篡改 →
      :class:`IntakeReceiptStateError`（零写入，旧文件原样保留）；
    - 只有 ``out_path`` 才写**receipt 本身**，且先取**单实例锁**再重新核验（避免并发竞态）；
      锁冲突 → :class:`~src.evidence.readiness_runner.LockConflictError` 且**零写入**；
    - 任何核验不一致 → :class:`IntakeReceiptVerificationError` 且**零写入**；
    - 本函数**没有**任何 intake / commit 调用，**绝不**写数据库，**绝不**移动 / 删除 / 改写
      inbox 内原始 evidence，**绝不**联网，**绝不**解除 blocker。

    Raises:
        IntakeReceiptArgumentError: ``moment`` 未带时区，或缺 ``plan_path`` /
            ``ledger_path`` / ``approved_list_path``。
        IntakeReceiptPathError: inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。
        IntakeReceiptStateError: plan / ledger / 批准清单 / 执行结果 / 复核结果损坏或被篡改。
        IntakeReceiptVerificationError: 执行后核验不通过（fail-closed）。
        LockConflictError: 另一个收据生成 / 计划 / 复核 / 重扫正持有活动锁（零写入）。
        IntakeReceiptWriteError: receipt 原子写失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(inbox_dir)
    if not root.is_dir():
        raise IntakeReceiptPathError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    if plan_path is None:
        raise IntakeReceiptArgumentError(
            "必须显式给出 GOLD-013 intake plan（--plan）：本工具不猜、不自动生成计划"
        )
    if ledger_path is None:
        raise IntakeReceiptArgumentError(
            "必须显式给出 GOLD-012 review ledger（--ledger）：批准来源必须可核验"
        )
    if approved_list_path is None:
        raise IntakeReceiptArgumentError(
            "必须显式给出 GOLD-012 批准清单（--approved-list）：批准范围必须可核验"
        )
    plan_target = Path(plan_path)
    ledger_target = Path(ledger_path)
    approved_target = Path(approved_list_path)
    recheck_target = Path(recheck_path) if recheck_path is not None else None
    result_targets = tuple(Path(item) for item in operator_result_paths)
    out = Path(out_path) if out_path is not None else None
    for target in (
        plan_target,
        ledger_target,
        approved_target,
        recheck_target,
        *result_targets,
        out,
    ):
        if target is None:
            continue
        try:
            ensure_outside_inbox(root, target)
        except InboxError as exc:
            raise IntakeReceiptPathError(_safe(str(exc), max_chars=300)) from exc

    def _compose() -> IntakeReceipt:
        return _compose_receipt(
            root,
            moment=moment,
            plan_path=plan_target,
            ledger_path=ledger_target,
            approved_list_path=approved_target,
            operator_result_paths=result_targets,
            recheck_path=recheck_target,
        )

    if out is None:
        return _compose()
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + INTAKE_RECEIPT_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        receipt = _compose()
        _atomic_write_json(out, receipt.to_dict(), what="intake receipt")
    return replace(receipt, written_path=_safe_path(out))


def render_intake_receipt_summary(receipt: IntakeReceipt) -> str:
    """渲染人类可读的收据摘要（脱敏；**不解除** blocker、**不自动** intake）。"""
    payload = receipt.to_dict()
    lines: list[str] = [
        "# Evidence Intake Receipt（执行后的只读审计收据）",
        "",
        f"> {INTAKE_RECEIPT_NOTE}",
        "",
        "## 1. 运行概览",
        "",
        f"- receipt_id（内容级）：`{receipt.receipt_id}`；状态：`{receipt.status.value}`；"
        f"执行模式：`{RECEIPT_EXECUTION_MODE}`",
        f"- plan_id（GOLD-013）：`{receipt.plan_id}`；plan 状态：`{receipt.plan_status}`；"
        f"plan 生成于 {receipt.plan_generated_at.isoformat()}",
        f"- inbox 目录：`{_safe_path(receipt.inbox_dir)}`（只读扫描；原始文件未被移动 / 删除）",
        f"- review ledger：`{_safe_path(receipt.ledger_path)}`；批准清单："
        f"`{_safe_path(receipt.approved_list_path)}`",
        f"- 审计时点（UTC）：{receipt.receipt_at.isoformat()}",
        f"- blocker：`{PHASE3_3_BLOCKER_CODE}`（`blocker_active` = true；"
        "`human_gate_required` = true；Phase 切换仍须 **L3 人工 Gate**）",
        f"- 当前扫描候选包：{payload['verification']['inbox_packages']}；"
        f"`PREFLIGHT_PASS`：{payload['verification']['preflight_pass']}；"
        f"ledger 已复核指纹：{payload['verification']['ledger_fingerprints']}",
        "- 核验结论：**0 项不一致**（任一不一致都会 fail-closed 且不产出收据）",
    ]
    if receipt.written_path is not None:
        lines.append(f"- 收据已原子写入：`{receipt.written_path}`")
    lines += [
        "",
        "## 2. 四个布尔（**互不蕴含**）",
        "",
        f"- `intake_executed` = {str(receipt.intake_executed).lower()}"
        f"（人工**显式**落库是否确实发生；`landed_rows` = {receipt.landed_rows}）",
        f"- `receipt_verified` = {str(receipt.receipt_verified).lower()}"
        "（本收据的绑定核验是否通过）",
        "- `data_qualification_passed` = false（**恒为 false**：收据**不是**资格判定）",
        "- `phase_transition_allowed` = false（**恒为 false**：Phase 切换仍是 **L3 人工 Gate**）",
    ]
    lines += ["", "## 3. 已绑定的批准与执行（**仍不等于资格**）", ""]
    if not receipt.entries:
        lines.append(
            "- —（当前没有任何仍成立的批准；`PHASE3_3_DATA` 保持 **BLOCKED**，"
            "请先按 GOLD-011/012/013 摆放候选、人工复核并生成计划）"
        )
    for entry in receipt.entries:
        lines.append(
            f"- `{entry.package_dir}`（指纹前 16 位 `{entry.fingerprint[:16]}`；"
            f"rev {entry.revision}；scope `{entry.operator_scope}`）："
            f"文件 {list(entry.evidence_files)}；行数 {entry.rows}"
            f"（可接收 {entry.acceptable_rows}）；已显式执行：{str(entry.executed).lower()}"
        )
    lines += ["", "## 4. 人工显式执行结果（manifest，只读引用）", ""]
    if not receipt.operator_results:
        lines.append("- —（没有任何显式执行结果）")
    for result in receipt.operator_results:
        lines.append(
            f"- scope `{result.scope}`；dry_run={str(result.dry_run).lower()}；"
            f"输入内容摘要前 16 位 `{result.input_sha256[:16]}`；"
            f"行数 {result.counts['rows']}（accepted {result.counts['accepted']} / "
            f"quarantined {result.counts['quarantined']} / 落库 {result.counts['persisted']}）；"
            f"产物摘要前 16 位 `{result.artifact_sha256[:16]}`"
        )
    lines += ["", "## 5. qualification recheck（只读引用；**不是**放行结论）", ""]
    if receipt.recheck is None:
        lines.append("- —（没有任何 recheck 结果）")
    else:
        recheck = receipt.recheck
        lines.append(
            f"- `{recheck.report}`；复核时点 {recheck.as_of.isoformat()}；"
            f"`ready`={str(recheck.ready).lower()}；blocker `{recheck.blocker_code}`"
            f"（active={str(recheck.blocker_active).lower()}，"
            f"human_gate_required={str(recheck.human_gate_required).lower()}）；"
            f"qualification pass / blocked = {recheck.qualification_pass_count} / "
            f"{recheck.qualification_blocked_count}；readiness blocked scopes = "
            f"{recheck.readiness_blocked_scope_count}"
        )
    lines += [
        "",
        "## 6. 时间语义与后续人工动作",
        "",
        f"- {EVIDENCE_TIME_SEMANTICS_NOTE}",
        f"- {NEXT_STEP_NOTE}",
    ]
    lines += ["", "## 7. 说明", ""]
    lines += [f"- {note}" for note in receipt.notes]
    lines += [""]
    return "\n".join(lines)
