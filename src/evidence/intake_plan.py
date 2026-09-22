"""Evidence approved-for-explicit-intake **Intake Plan**（**GOLD-013**，纯本地只读）。

GOLD-012 交付了人工复核决策与**脱敏**批准清单（``approved-for-explicit-intake``），
GOLD-007 / GOLD-005 交付了**显式** Evidence Operator intake 路径。两者之间过去只靠人工
"看着清单照抄命令"衔接：清单是否仍是**当前** inbox / review ledger 的真实结论、有没有被改过、
批准的是**哪一版内容**、真正落库前还缺哪一步，都没有机械化的最后一道门禁。

本模块补上**最终写入前的只读门禁**：把批准清单与**当前** inbox / review ledger **重新绑定核验**，
生成**确定性、脱敏、只读**的 intake plan（``plan_id`` 为**内容级**哈希），供人工在**显式**执行
``scripts/evidence_operator.py workflow --no-dry-run`` 之前复核。

- **纯本地 / 只读**：只**读**显式 ``--inbox-dir`` / ``--ledger`` / ``--approved-list``，只**复用**
  GOLD-011 的 inbox 预检与 GOLD-012 的批准清单重新验证口径
  （:func:`~src.evidence.review.build_approved_intake_list`），**不复制、不降低**任何资格规则；
  默认**零写入**，只有显式 ``--out`` 才**原子**落盘计划本身；本模块**没有**任何 intake / commit
  调用，**绝不**写研究数据库，**绝不**移动 / 删除 / 改写 inbox 内任何原始 evidence；
- **最终写入前重新核验（逐项 fail-closed）**：①批准清单必须**结构自洽**（文档标识 / schema /
  契约版本 / ``approved_count`` / ``invalidated_count`` 与列表长度一致 / 四个安全字段与
  ``approval_scope`` 不可被 state 削弱 / **不得**出现任何证据时间字段）；②每条批准必须**当前仍是**
  ledger 上该指纹的**最新有效**决策（``revision`` 与 ``decision_id`` 完全一致；
  ``review override`` 之后的旧批准一律失效）；③该指纹必须**当前仍在** inbox 扫描结果中且仍
  ``PREFLIGHT_PASS``、**不是**模板 / 示例 / Mock、候选包摘要与复核时一致；④清单必须与"用
  **当前** inbox + ledger 重新算出的批准集合"**完全一致**（条目 / 失效项 / 计数 / 决策计数），
  否则视为 **approved-list tamper**；任一不一致 → :class:`IntakePlanInconsistentError`
  （**fail-closed**，零写入，绝不产出"看起来可以落库"的计划）；
- **内容级 ``plan_id`` / 幂等**：``plan_id`` 只由（策略块 + 批准条目 + ledger 最新决策摘要）派生，
  **不含** ``generated_at``；同一输入重复生成得到同一 ``plan_id``（文档在**同一审计时点**下逐字节
  稳定），**输入内容或 review revision 变化**必然产生新 ``plan_id``（旧计划 / 旧批准
  **绝不静默继承**）；
- **明确区分 ≠ 资格**：``approved_for_explicit_intake`` 与 ``data_qualification_passed`` 是两个
  **独立**字段，后者**恒为** ``false``、``data_qualification_passed_count`` **恒为** ``0``：
  批准数量（含全部候选被批准）**不会**自动改变它；``blocker_active`` / ``human_gate_required``
  恒为 ``true``、``phase_transition_allowed`` 恒为 ``false``（**硬编码**），``PHASE3_3_DATA``
  **保持 BLOCKED**；
- **显式衔接，绝不自动执行**：``handoff`` 只给出**字符串形式**的显式 operator 命令
  （必带 ``--no-dry-run`` 与显式 ``--input``），仅供人工复制执行；``auto_intake_allowed``
  恒为 ``false``、``writes_database`` 恒为 ``false``、``requires_explicit_operator_action``
  恒为 ``true``；默认路径**不**写研究数据库，也**不**解除 blocker；
- **绝不把复核 / 计划时间当证据**：``reviewer`` / ``note`` / 文件名 / mtime / ``reviewed_at`` /
  ``generated_at`` / "曾被人批准" **都不是**证据时间；本模块产出**只有**上述审计字段。

入口：``scripts/evidence_intake_plan.py``（``--inbox-dir`` / ``--ledger`` / ``--approved-list``
必填；``--out`` 是**唯一**写开关，默认只读；**没有**任何 intake / ``--no-dry-run`` 参数）。
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
    InboxError,
    InboxPreflightReport,
    ensure_outside_inbox,
    scan_inbox,
)
from src.evidence.readiness_watch import MAX_CODE_CHARS, atomic_write_text
from src.evidence.review import (
    APPROVAL_SCOPE,
    APPROVED_KIND,
    FORBIDDEN_EVIDENCE_FIELDS,
    REVIEW_SCHEMA_VERSION,
    ApprovedEntry,
    InvalidationReason,
    ReviewDecision,
    ReviewLedger,
    ReviewLedgerStateError,
    build_approved_intake_list,
    load_review_ledger,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "APPROVED_ENTRY_KEYS",
    "EXIT_BLOCKED",
    "EXIT_CONFIG_ERROR",
    "EXIT_LOCK_CONFLICT",
    "EXIT_OK",
    "EXIT_STATE_INVALID",
    "EXIT_UNUSABLE",
    "INTAKE_PLAN_FILE_NAME",
    "INTAKE_PLAN_HANDOFF_NOTE",
    "INTAKE_PLAN_KIND",
    "INTAKE_PLAN_LOCK_SUFFIX",
    "INTAKE_PLAN_NOTE",
    "INTAKE_PLAN_REPORT_NAME",
    "INTAKE_PLAN_SCHEMA_VERSION",
    "OPERATOR_EXPLICIT_FLAG",
    "PLAN_EXECUTION_MODE",
    "ApprovedListDocument",
    "IntakePlan",
    "IntakePlanArgumentError",
    "IntakePlanError",
    "IntakePlanInconsistentError",
    "IntakePlanPathError",
    "IntakePlanStateError",
    "IntakePlanStatus",
    "IntakePlanWriteError",
    "OperatorHandoffStep",
    "PlanEntry",
    "PlanVerificationCode",
    "PlanViolation",
    "build_intake_plan",
    "compute_plan_id",
    "exit_code_for",
    "intake_plan_handoff",
    "load_approved_intake_list",
    "render_intake_plan_summary",
    "run_intake_plan",
    "verify_intake_plan_inputs",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
INTAKE_PLAN_SCHEMA_VERSION: Final[int] = 1
#: 计划文档标识 / 报告标识（稳定，供上游与人工日志解析）
INTAKE_PLAN_KIND: Final[str] = "evidence_intake_plan"
INTAKE_PLAN_REPORT_NAME: Final[str] = "evidence_intake_plan"
#: 计划的默认文件名（operator 放在显式工作目录里；**默认不写**）
INTAKE_PLAN_FILE_NAME: Final[str] = "evidence_intake_plan.json"
#: 计划的单实例锁后缀（与 ``out_path`` 同级）
INTAKE_PLAN_LOCK_SUFFIX: Final[str] = ".lock"
#: 执行模式（**恒为只读**：本工具不执行任何 intake）
PLAN_EXECUTION_MODE: Final[str] = "DRY_RUN_READ_ONLY"
#: 后续真实 intake **必须**显式给出的 operator 开关（缺它就只是 dry-run）
OPERATOR_EXPLICIT_FLAG: Final[str] = "--no-dry-run"
#: 批准清单条目必须包含的字段（GOLD-012 ``ApprovedEntry.to_dict()`` 的**全量**键集合）
APPROVED_ENTRY_KEYS: Final[tuple[str, ...]] = (
    "fingerprint",
    "decision_id",
    "approved_at",
    "reviewer",
    "reason_code",
    "package_dir",
    "evidence_type",
    "source",
    "evidence_files",
    "rows",
    "acceptable_rows",
    "approval_scope",
    "requires_explicit_intake",
)
#: 64 位小写十六进制（指纹 / ``decision_id`` 统一校验）
_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
#: 支持的证据类别（直接取自 ``evidence-intake-v1`` 契约，**不另造词表**）
_EVIDENCE_SCOPES: Final[frozenset[str]] = frozenset(member.value for member in EvidenceScope)
#: 批准清单里的四个安全字段（state 不得改写它们）
_SAFETY_FIELDS: Final[tuple[tuple[str, bool], ...]] = (
    ("blocker_active", True),
    ("human_gate_required", True),
    ("data_qualification_passed", False),
    ("phase_transition_allowed", False),
)

#: 固定说明：计划只做**最终写入前**的只读核验，不解除 blocker、不自动 intake
INTAKE_PLAN_NOTE: Final[str] = (
    "本工具只做**最终写入前**的**只读**核验：把 GOLD-012 的批准清单与**当前** inbox / review "
    "ledger 重新绑定核验，并给出确定性脱敏计划；它**不写数据库**、**不调用任何 intake / commit**、"
    "**不移动 / 删除 / 改写**任何原始 evidence，也**不会**解除 `PHASE3_3_DATA`。"
    "计划里 `approved_for_explicit_intake` 只表示**人工预审通过且当前仍一致**，"
    "`data_qualification_passed` **恒为 false**；真正落库必须由人工**显式**执行 "
    "`scripts.evidence_operator.py workflow --no-dry-run`，随后以 `handoff` / `recheck` 复核，"
    "Phase 切换仍是 **L3 人工 Gate**。"
)
#: 固定说明：handoff 命令只是**字符串**，本工具绝不执行
INTAKE_PLAN_HANDOFF_NOTE: Final[str] = (
    "handoff 命令只是**字符串模板**（必带 `--no-dry-run` 与显式 `--input`），"
    "用于人工**显式**执行；本工具绝不执行任何命令、绝不联网、绝不写数据库；"
    "执行前请自行按 GOLD-007 提供显式 `--manifest` 路径，并在执行后跑 `handoff` / `recheck`。"
)


class IntakePlanStatus(StrEnum):
    """计划状态（``READY_*`` 也只表示"可以交给人工显式执行"，**不是**资格通过）。"""

    READY_FOR_EXPLICIT_INTAKE = "READY_FOR_EXPLICIT_INTAKE"
    BLOCKED_NO_APPROVED_EVIDENCE = "BLOCKED_NO_APPROVED_EVIDENCE"


class PlanVerificationCode(StrEnum):
    """最终写入前核验的稳定原因码（任何一条都意味着 **fail-closed**）。"""

    # ---- 批准清单本身（结构 / 内容被改写）---------------------------------
    APPROVED_LIST_TAMPERED = "APPROVED_LIST_TAMPERED"
    APPROVED_LIST_STALE = "APPROVED_LIST_STALE"
    INVALIDATED_MISMATCH = "INVALIDATED_MISMATCH"
    COUNT_MISMATCH = "COUNT_MISMATCH"
    PLAN_TIME_BEFORE_APPROVAL = "PLAN_TIME_BEFORE_APPROVAL"
    # ---- review ledger 侧（最新有效决策）---------------------------------
    LEDGER_MISSING_APPROVAL = "LEDGER_MISSING_APPROVAL"
    LEDGER_DECISION_SUPERSEDED = "LEDGER_DECISION_SUPERSEDED"
    LEDGER_REVISION_SUPERSEDED = "LEDGER_REVISION_SUPERSEDED"
    # ---- 当前 inbox 侧（内容 / 预检 / 合成）------------------------------
    FINGERPRINT_MISSING = "FINGERPRINT_MISSING"
    PREFLIGHT_NOT_PASSING = "PREFLIGHT_NOT_PASSING"
    SYNTHETIC_EVIDENCE = "SYNTHETIC_EVIDENCE"
    EVIDENCE_INCONSISTENT = "EVIDENCE_INCONSISTENT"


#: 失效批准原因（GOLD-012 口径）→ 计划核验原因码（**同一含义不另造词**）
_INVALIDATION_CODES: Final[Mapping[str, str]] = {
    InvalidationReason.CANDIDATE_MISSING.value: PlanVerificationCode.FINGERPRINT_MISSING.value,
    InvalidationReason.PREFLIGHT_NOT_PASSING.value: (
        PlanVerificationCode.PREFLIGHT_NOT_PASSING.value
    ),
    InvalidationReason.SYNTHETIC_EVIDENCE.value: PlanVerificationCode.SYNTHETIC_EVIDENCE.value,
    InvalidationReason.EVIDENCE_INCONSISTENT.value: (
        PlanVerificationCode.EVIDENCE_INCONSISTENT.value
    ),
}


class IntakePlanError(RuntimeError):
    """计划层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class IntakePlanArgumentError(IntakePlanError):
    """参数错误（缺 inbox / ledger / approved list，时区缺失等）。"""


class IntakePlanStateError(IntakePlanError):
    """批准清单 / ledger 损坏、被篡改，或被削弱安全字段（**fail-closed**，零写入）。"""


class IntakePlanPathError(IntakePlanError):
    """inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。"""


class IntakePlanWriteError(IntakePlanError):
    """计划原子写失败（**fail-closed**）。"""


class IntakePlanInconsistentError(IntakePlanError):
    """最终写入前核验不通过（**fail-closed**：绝不产出可落库的计划，绝不自动 intake）。"""

    def __init__(self, violations: Sequence[PlanViolation]) -> None:
        self.violations: tuple[PlanViolation, ...] = tuple(violations)
        codes = sorted({item.code for item in self.violations})
        detail = "、".join(codes[:8]) if codes else "UNKNOWN"
        super().__init__(
            "intake plan 核验不通过（fail-closed；零写入、绝不自动 intake）："
            f"{len(self.violations)} 项不一致；原因码：{detail}"
        )



# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 计划生成成功且**至少有一条**仍成立的批准（仍需人工**显式** intake）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数错误（缺 inbox / ledger / approved list，时区缺失等）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输入 / 输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: 批准清单 / ledger 损坏、被篡改，或最终写入前核验不通过（全部 fail-closed）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 计划生成成功但**没有任何**仍成立的批准（预期 BLOCKED，不是故障）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一个计划生成 / 复核 / 重扫正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_PLAN_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (IntakePlanArgumentError, EXIT_CONFIG_ERROR),
    (IntakePlanStateError, EXIT_STATE_INVALID),
    (IntakePlanInconsistentError, EXIT_STATE_INVALID),
    (IntakePlanPathError, EXIT_UNUSABLE),
    (IntakePlanWriteError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _PLAN_EXIT_CODES:
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
        raise IntakePlanArgumentError(f"{field_name} 必须包含时区（禁止隐式时区）")
    return moment.astimezone(UTC)


def _parse_aware_moment(raw: object, *, field_name: str) -> datetime:
    """解析 ISO8601 时间戳并要求带时区（state 文件里的时间一律显式校验）。"""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise IntakePlanStateError(f"{field_name} 无法解析为 ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntakePlanStateError(f"{field_name} 必须带时区")
    return parsed


def _policy_block() -> dict[str, Any]:
    """策略块（四个安全字段 + 执行模式；**硬编码**，与批准数量无关）。"""
    return {
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "execution_mode": PLAN_EXECUTION_MODE,
        "auto_intake_allowed": False,
        "writes_database": False,
        "requires_explicit_operator_action": True,
        "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
    }



# ---------------------------------------------------------------------------
# 批准清单（GOLD-012 产物）的严格只读视图
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ProvidedEntry:
    """批准清单里的**一条**条目（只保留白名单键；额外键单独留痕以便判 tamper）。"""

    fingerprint: str
    decision_id: str
    reason_code: str
    view: Mapping[str, Any]
    extra_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovedListDocument:
    """已严格校验的 GOLD-012 批准清单（**只读视图**；结构自洽 + 未削弱安全字段）。"""

    generated_at: datetime
    approved: tuple[_ProvidedEntry, ...] = ()
    invalidated: tuple[tuple[str, str, str], ...] = ()
    decision_counts: tuple[tuple[str, int], ...] = ()

    @property
    def approved_count(self) -> int:
        """清单声明的批准条数（与 ``approved`` 长度一致，加载时已校验）。"""
        return len(self.approved)

    @property
    def invalidated_count(self) -> int:
        """清单声明的失效批准条数（与 ``invalidated`` 长度一致，加载时已校验）。"""
        return len(self.invalidated)


def _reject_forbidden_fields(payload: object) -> None:
    """批准清单里**绝不**允许出现证据时间字段（复核 / 计划时间都不是证据）。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key) in FORBIDDEN_EVIDENCE_FIELDS:
                raise IntakePlanStateError(
                    f"批准清单含禁止的证据时间字段 {_safe(key, max_chars=40)}："
                    "fail-closed（复核 / 计划时间一律不得当作证据时间）"
                )
            _reject_forbidden_fields(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            _reject_forbidden_fields(item)


def _entry_view(raw: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    """把一条批准条目规范化为**白名单**视图（缺字段 / 类型非法 → fail-closed）。"""
    missing = [key for key in APPROVED_ENTRY_KEYS if key not in raw]
    if missing:
        raise IntakePlanStateError(
            f"批准清单 approved[{index}] 缺字段：{'、'.join(sorted(missing))}"
        )
    try:
        rows = int(raw["rows"])
        acceptable_rows = int(raw["acceptable_rows"])
    except (TypeError, ValueError) as exc:
        raise IntakePlanStateError(f"批准清单 approved[{index}] 行数非法") from exc
    raw_files = raw["evidence_files"]
    if isinstance(raw_files, str) or not isinstance(raw_files, Sequence):
        raise IntakePlanStateError(f"批准清单 approved[{index}] evidence_files 必须是数组")
    scope = str(raw["approval_scope"])
    if scope != APPROVAL_SCOPE:
        raise IntakePlanStateError(
            f"批准清单 approved[{index}] approval_scope 被改写（必须是 {APPROVAL_SCOPE}）："
            "fail-closed（不得把人工预审读成资格通过）"
        )
    if not bool(raw["requires_explicit_intake"]):
        raise IntakePlanStateError(
            f"批准清单 approved[{index}] requires_explicit_intake 被改写为 false："
            "fail-closed（批准绝不等于自动 intake）"
        )
    return {
        "fingerprint": str(raw["fingerprint"]),
        "decision_id": str(raw["decision_id"]),
        "approved_at": str(raw["approved_at"]),
        "reviewer": str(raw["reviewer"]),
        "reason_code": str(raw["reason_code"]),
        "package_dir": str(raw["package_dir"]),
        "evidence_type": str(raw["evidence_type"]),
        "source": str(raw["source"]),
        "evidence_files": [_safe_path(item) for item in raw_files],
        "rows": rows,
        "acceptable_rows": acceptable_rows,
        "approval_scope": scope,
        "requires_explicit_intake": True,
    }



def _validate_approved_document(payload: Mapping[str, Any]) -> ApprovedListDocument:
    """严格校验批准清单（**结构自洽**；任何异常都 fail-closed）。

    Raises:
        IntakePlanStateError: 文档标识 / schema / 契约版本 / 安全字段 / 计数自洽性 /
            条目结构 / 禁止字段 任一不合法。
    """
    if payload.get("kind") != APPROVED_KIND:
        raise IntakePlanStateError("文件不是 evidence inbox approved-for-explicit-intake 清单")
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise IntakePlanStateError(
            f"批准清单 schema_version 不受支持：{payload.get('schema_version')!r}"
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
        raise IntakePlanStateError(
            f"批准清单 contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
        )
    for field_name, expected in _SAFETY_FIELDS:
        if field_name in payload and payload[field_name] is not expected:
            raise IntakePlanStateError(
                f"批准清单声称 {field_name}={payload[field_name]!r}："
                "安全字段不可被 state 改写（fail-closed）"
            )
    if "approval_scope" in payload and str(payload["approval_scope"]) != APPROVAL_SCOPE:
        raise IntakePlanStateError("批准清单 approval_scope 被改写：fail-closed")
    _reject_forbidden_fields(payload)

    raw_approved = payload.get("approved")
    if not isinstance(raw_approved, list):
        raise IntakePlanStateError("批准清单 approved 必须是数组")
    entries: list[_ProvidedEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_approved, start=1):
        if not isinstance(item, Mapping):
            raise IntakePlanStateError(f"批准清单 approved[{index}] 必须是对象")
        view = _entry_view(item, index=index)
        fingerprint = view["fingerprint"]
        if not _HEX64.match(fingerprint):
            raise IntakePlanStateError(f"批准清单 approved[{index}] fingerprint 非法")
        if not _HEX64.match(str(view["decision_id"])):
            raise IntakePlanStateError(f"批准清单 approved[{index}] decision_id 非法")
        if fingerprint in seen:
            raise IntakePlanStateError(f"批准清单 approved[{index}] fingerprint 重复")
        seen.add(fingerprint)
        entries.append(
            _ProvidedEntry(
                fingerprint=fingerprint,
                decision_id=str(view["decision_id"]),
                reason_code=str(view["reason_code"]),
                view=view,
                extra_keys=tuple(sorted(set(item) - set(APPROVED_ENTRY_KEYS))),
            )
        )

    raw_invalidated = payload.get("invalidated")
    if not isinstance(raw_invalidated, list):
        raise IntakePlanStateError("批准清单 invalidated 必须是数组")
    invalidated: list[tuple[str, str, str]] = []
    for index, item in enumerate(raw_invalidated, start=1):
        if not isinstance(item, Mapping):
            raise IntakePlanStateError(f"批准清单 invalidated[{index}] 必须是对象")
        required = ("fingerprint", "decision_id", "reason_code")
        missing = [key for key in required if key not in item]
        if missing:
            raise IntakePlanStateError(
                f"批准清单 invalidated[{index}] 缺字段：{'、'.join(sorted(missing))}"
            )
        invalidated.append(
            (str(item["fingerprint"]), str(item["decision_id"]), str(item["reason_code"]))
        )

    raw_counts = payload.get("decision_counts")
    if not isinstance(raw_counts, Mapping):
        raise IntakePlanStateError("批准清单 decision_counts 必须是对象")
    counts: list[tuple[str, int]] = []
    for key, value in raw_counts.items():
        try:
            counts.append((_safe(key, max_chars=40), int(value)))
        except (TypeError, ValueError) as exc:
            raise IntakePlanStateError("批准清单 decision_counts 计数非法") from exc

    for field_name, actual in (
        ("approved_count", len(entries)),
        ("invalidated_count", len(invalidated)),
    ):
        declared = payload.get(field_name)
        if not isinstance(declared, int) or isinstance(declared, bool):
            raise IntakePlanStateError(f"批准清单 {field_name} 必须是整数")
        if declared != actual:
            raise IntakePlanStateError(
                f"批准清单 {field_name}={declared} 与列表长度 {actual} 不一致："
                "清单结构不自洽（fail-closed）"
            )

    generated_at = _parse_aware_moment(
        payload.get("generated_at"), field_name="批准清单 generated_at"
    )
    return ApprovedListDocument(
        generated_at=generated_at,
        approved=tuple(sorted(entries, key=lambda item: item.fingerprint)),
        invalidated=tuple(sorted(invalidated)),
        decision_counts=tuple(sorted(counts)),
    )


def load_approved_intake_list(path: Path | str) -> ApprovedListDocument:
    """读取 GOLD-012 批准清单并做**严格**校验（损坏 / 篡改 → fail-closed）。

    Raises:
        IntakePlanStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 严格校验失败
            （**绝不**当作"没有批准"，也绝不覆盖既有文件）。
    """
    target = Path(path)
    if not target.exists():
        raise IntakePlanStateError(
            "批准清单不存在（请先跑 GOLD-012 `evidence_review --approved-out`）："
            f"{_safe_path(target)}"
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakePlanStateError(f"批准清单不可读：{_safe_path(target)}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntakePlanStateError("批准清单不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise IntakePlanStateError("批准清单顶层必须是 JSON 对象")
    return _validate_approved_document(payload)



# ---------------------------------------------------------------------------
# 最终写入前核验（fail-closed）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlanViolation:
    """一条核验不一致（**稳定原因码** + 已脱敏说明；任何一条都让计划 fail-closed）。"""

    code: str
    fingerprint: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "fingerprint": self.fingerprint,
            "detail": _safe(self.detail, max_chars=300),
        }


def _violation(code: PlanVerificationCode, fingerprint: str, detail: str) -> PlanViolation:
    """构造一条核验不一致（统一脱敏）。"""
    return PlanViolation(
        code=code.value, fingerprint=fingerprint, detail=_safe(detail, max_chars=300)
    )


def _verify_against_canonical(
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    document: ApprovedListDocument,
    canonical: Sequence[ApprovedEntry],
    *,
    moment: datetime,
) -> tuple[PlanViolation, ...]:
    """把批准清单与"用**当前** inbox + ledger 重新算出的批准集合"逐项比对。"""
    violations: list[PlanViolation] = []
    packages = {item.fingerprint: item for item in scan.packages}
    canonical_by_fp: dict[str, ApprovedEntry] = {item.fingerprint: item for item in canonical}
    revalidated = build_approved_intake_list(scan, ledger, moment=moment)
    invalidated_by_key = {
        (item.fingerprint, item.decision_id): item.reason_code for item in revalidated.invalidated
    }
    provided_fps: set[str] = set()
    for entry in document.approved:
        fingerprint = entry.fingerprint
        provided_fps.add(fingerprint)
        record = ledger.latest_for(fingerprint)
        if record is None:
            violations.append(
                _violation(
                    PlanVerificationCode.LEDGER_MISSING_APPROVAL,
                    fingerprint,
                    "ledger 中不存在该指纹的任何决策：批准来源不可核验",
                )
            )
            continue
        if record.decision != ReviewDecision.APPROVE.value:
            violations.append(
                _violation(
                    PlanVerificationCode.LEDGER_DECISION_SUPERSEDED,
                    fingerprint,
                    f"ledger 最新决策为 {record.decision}（revision {record.revision}）："
                    "批准已被推翻 / 取代，必须重新人工复核",
                )
            )
            continue
        if record.decision_id != entry.decision_id:
            violations.append(
                _violation(
                    PlanVerificationCode.LEDGER_REVISION_SUPERSEDED,
                    fingerprint,
                    "清单引用的 decision_id 不是 ledger 最新有效决策"
                    "（review override / 旧 revision）：旧批准不继承",
                )
            )
            continue
        canonical_entry = canonical_by_fp.get(fingerprint)
        if canonical_entry is None:
            code = _INVALIDATION_CODES.get(
                invalidated_by_key.get((fingerprint, entry.decision_id), ""),
                PlanVerificationCode.APPROVED_LIST_TAMPERED.value,
            )
            package = packages.get(fingerprint)
            detail = (
                "批准的候选包当前不在 inbox 扫描结果中（内容已变化 → 新指纹，或已被移除）"
                if package is None
                else f"批准的候选包当前预检结论为 {package.status.value}：必须重新人工复核"
            )
            violations.append(PlanViolation(code=code, fingerprint=fingerprint, detail=detail))
            continue
        if entry.extra_keys:
            violations.append(
                _violation(
                    PlanVerificationCode.APPROVED_LIST_TAMPERED,
                    fingerprint,
                    "条目含未授权字段（除 GOLD-012 白名单键之外的键）："
                    f"{_safe('、'.join(entry.extra_keys), max_chars=120)}",
                )
            )
            continue
        if dict(entry.view) != canonical_entry.to_dict():
            violations.append(
                _violation(
                    PlanVerificationCode.APPROVED_LIST_TAMPERED,
                    fingerprint,
                    "条目字段与用当前 inbox / ledger 重新验证的结果不一致（清单被改写或已过期）",
                )
            )

    for fingerprint in sorted(canonical_by_fp):
        if fingerprint not in provided_fps:
            violations.append(
                _violation(
                    PlanVerificationCode.APPROVED_LIST_STALE,
                    fingerprint,
                    "清单缺少当前仍成立的批准（清单过期 / 不完整）：必须重新生成批准清单",
                )
            )

    provided_invalid = {(item[0], item[1]): item[2] for item in document.invalidated}
    canonical_invalid = {
        (item.fingerprint, item.decision_id): item.reason_code
        for item in revalidated.invalidated
    }
    if provided_invalid != canonical_invalid:
        violations.append(
            _violation(
                PlanVerificationCode.INVALIDATED_MISMATCH,
                "",
                "清单 invalidated 集合与重新验证得到的失效批准不一致",
            )
        )
    if document.approved_count != len(canonical):
        violations.append(
            _violation(
                PlanVerificationCode.COUNT_MISMATCH,
                "",
                "清单 approved_count 与重新验证得到的批准条数不一致",
            )
        )
    if document.decision_counts != tuple(sorted(revalidated.decision_counts)):
        violations.append(
            _violation(
                PlanVerificationCode.COUNT_MISMATCH,
                "",
                "清单 decision_counts 与重新统计的决策计数不一致",
            )
        )
    if document.generated_at > moment:
        violations.append(
            _violation(
                PlanVerificationCode.PLAN_TIME_BEFORE_APPROVAL,
                "",
                "批准清单生成时间晚于本次审计时点：拒绝（fail-closed）",
            )
        )
    return tuple(violations)



def verify_intake_plan_inputs(
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
) -> tuple[PlanViolation, ...]:
    """**只读**核验：批准清单是否仍是"当前 inbox + ledger"的真实结论（零写入）。

    Returns:
        核验不一致列表（空 = 可以生成计划）。任何一条都意味着调用方必须 **fail-closed**。
    """
    moment = _require_aware(moment, field_name="moment")
    canonical = build_approved_intake_list(scan, ledger, moment=moment)
    return _verify_against_canonical(
        scan, ledger, approved_list, canonical.approved, moment=moment
    )


# ---------------------------------------------------------------------------
# 计划条目 / 显式 operator 衔接（**只是字符串**，绝不执行）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlanEntry:
    """计划里**一条**已重新核验的批准（人工预审通过 + 当前仍一致；≠ 资格）。"""

    fingerprint: str
    decision_id: str
    revision: int
    reviewer: str
    reviewed_at: str
    reason_code: str
    package_dir: str
    evidence_type: str
    source: str
    scope: str
    evidence_files: tuple[str, ...]
    rows: int
    acceptable_rows: int

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "revision": self.revision,
            "reviewer": _safe(self.reviewer, max_chars=100),
            "reviewed_at": self.reviewed_at,
            "reason_code": self.reason_code,
            "package_dir": _safe_path(self.package_dir),
            "evidence_type": _safe(self.evidence_type, max_chars=40),
            "source": _safe(self.source, max_chars=100),
            "operator_scope": self.scope,
            "evidence_files": [_safe_path(name) for name in self.evidence_files],
            "rows": self.rows,
            "acceptable_rows": self.acceptable_rows,
            "approved_for_explicit_intake": True,
            "data_qualification_passed": False,
            "requires_explicit_operator_action": True,
        }


@dataclass(frozen=True, slots=True)
class OperatorHandoffStep:
    """后续**人工显式** intake 的一条命令模板（本工具**绝不执行**）。"""

    fingerprint: str
    decision_id: str
    scope: str
    package_dir: str
    evidence_file: str
    input_path: str
    required_flag: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "scope": self.scope,
            "package_dir": _safe_path(self.package_dir),
            "evidence_file": _safe_path(self.evidence_file),
            "input_path": _safe_path(self.input_path),
            "operator_script": "scripts.evidence_operator",
            "required_flag": self.required_flag,
            "command": _safe(self.command, max_chars=400),
            "requires_explicit_operator_action": True,
            "auto_executed": False,
            "note": INTAKE_PLAN_HANDOFF_NOTE,
        }


def _operator_scope(evidence_type: str) -> str:
    """把候选包 ``evidence_type`` 映射为 operator ``--scope``（未知取值 fail-closed）。"""
    scope = str(evidence_type).strip().lower()
    if scope not in _EVIDENCE_SCOPES:
        raise IntakePlanStateError(
            "不支持的 evidence_type（无法映射 operator --scope）："
            f"{_safe(evidence_type, max_chars=40)}"
        )
    return scope


def _handoff_command(inbox_dir: Path, entry: PlanEntry, evidence_file: str) -> str:
    """渲染一条**显式** intake 命令模板（必带显式 ``--input`` 与 ``--no-dry-run``）。"""
    target = str(Path(inbox_dir) / entry.package_dir / evidence_file)
    return (
        "python -m scripts.evidence_operator workflow "
        f"--scope {entry.scope} --input {target} {OPERATOR_EXPLICIT_FLAG}"
    )


def intake_plan_handoff(
    entries: Sequence[PlanEntry], *, inbox_dir: Path | str
) -> tuple[OperatorHandoffStep, ...]:
    """按计划条目生成**显式** operator 衔接步骤（每个证据文件一条命令模板）。"""
    root = Path(inbox_dir)
    steps: list[OperatorHandoffStep] = []
    for entry in entries:
        for evidence_file in entry.evidence_files:
            steps.append(
                OperatorHandoffStep(
                    fingerprint=entry.fingerprint,
                    decision_id=entry.decision_id,
                    scope=entry.scope,
                    package_dir=entry.package_dir,
                    evidence_file=_safe_path(evidence_file),
                    input_path=_safe_path(str(root / entry.package_dir / evidence_file)),
                    required_flag=OPERATOR_EXPLICIT_FLAG,
                    command=_handoff_command(root, entry, evidence_file),
                )
            )
    return tuple(steps)


def compute_plan_id(
    entries: Sequence[PlanEntry],
    ledger_digest: Sequence[tuple[str, int, str]],
) -> str:
    """内容级 ``plan_id``：只由（策略块 + 批准条目 + ledger 最新决策摘要）派生。

    ⚠️ 刻意**不含** ``generated_at``：同一输入（含同一 review revision）重复生成得到**同一**
    ``plan_id``；输入内容或 review revision 变化 → 必然产生新 ``plan_id``（旧计划不继承）。
    """
    payload = json.dumps(
        {
            "policy": _policy_block(),
            "approved": [entry.to_dict() for entry in entries],
            "ledger": [
                [fingerprint, int(revision), decision_id]
                for fingerprint, revision, decision_id in ledger_digest
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashing.sha256_text(payload)



# ---------------------------------------------------------------------------
# intake plan（确定性 / 脱敏 / 只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IntakePlan:
    """一次 intake plan 生成的结果（**确定性脱敏**；默认零写入，``written_path`` 才落盘）。"""

    generated_at: datetime
    inbox_dir: Path
    status: IntakePlanStatus
    entries: tuple[PlanEntry, ...] = ()
    handoff: tuple[OperatorHandoffStep, ...] = ()
    plan_id: str = ""
    ledger_digest: tuple[tuple[str, int, str], ...] = ()
    ledger_path: Path | None = None
    approved_list_path: Path | None = None
    approved_list_generated_at: str = ""
    inbox_packages: int = 0
    preflight_pass: int = 0
    ledger_fingerprints: int = 0
    written_path: str | None = None
    notes: tuple[str, ...] = (INTAKE_PLAN_NOTE,)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return INTAKE_PLAN_KIND

    @property
    def has_approved(self) -> bool:
        """是否存在**当前仍成立**的批准（仍需人工**显式**执行 operator intake）。"""
        return bool(self.entries)

    @property
    def requires_explicit_operator_action(self) -> bool:
        """恒为 ``True``：本工具绝不自动 intake、绝不写研究数据库。"""
        return True

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段**硬编码**，与批准数量无关）。"""
        return {
            "kind": INTAKE_PLAN_KIND,
            "report": INTAKE_PLAN_REPORT_NAME,
            "schema_version": INTAKE_PLAN_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "plan_id": self.plan_id,
            "status": self.status.value,
            "execution_mode": PLAN_EXECUTION_MODE,
            "inbox_dir": _safe_path(self.inbox_dir),
            "ledger_path": _safe_path(self.ledger_path) if self.ledger_path is not None else None,
            "approved_list_path": (
                _safe_path(self.approved_list_path) if self.approved_list_path is not None else None
            ),
            "approved_list_generated_at": self.approved_list_generated_at or None,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "auto_intake_allowed": False,
            "writes_database": False,
            "requires_explicit_operator_action": True,
            "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
            "approval_scope": APPROVAL_SCOPE,
            "approved_for_explicit_intake": [entry.to_dict() for entry in self.entries],
            "approved_for_explicit_intake_count": len(self.entries),
            "data_qualification_passed_count": 0,
            "verification": {
                "inbox_packages": self.inbox_packages,
                "preflight_pass": self.preflight_pass,
                "ledger_fingerprints": self.ledger_fingerprints,
                "violations": [],
                "revalidated_at": self.generated_at.isoformat(),
            },
            "ledger_revision_digest": [
                {"fingerprint": fingerprint, "revision": revision, "decision_id": decision_id}
                for fingerprint, revision, decision_id in self.ledger_digest
            ],
            "handoff": [step.to_dict() for step in self.handoff],
            "written_path": self.written_path,
            "next_step": INTAKE_PLAN_HANDOFF_NOTE,
            "notes": list(self.notes),
        }



def _plan_entry(entry: ApprovedEntry, ledger: ReviewLedger) -> PlanEntry:
    """把一条"仍成立的批准"升级为计划条目（带上 ledger 最新 revision；**只读**）。"""
    record = ledger.latest_for(entry.fingerprint)
    if record is None:
        raise IntakePlanStateError(
            f"指纹 {entry.fingerprint[:16]} 的 ledger 决策在核验后消失：fail-closed（拒绝生成计划）"
        )
    return PlanEntry(
        fingerprint=entry.fingerprint,
        decision_id=entry.decision_id,
        revision=record.revision,
        reviewer=entry.reviewer,
        reviewed_at=entry.approved_at,
        reason_code=entry.reason_code,
        package_dir=entry.package_dir,
        evidence_type=entry.evidence_type,
        source=entry.source,
        scope=_operator_scope(entry.evidence_type),
        evidence_files=tuple(entry.evidence_files),
        rows=entry.rows,
        acceptable_rows=entry.acceptable_rows,
    )


def build_intake_plan(
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    ledger_path: Path | None = None,
    approved_list_path: Path | None = None,
) -> IntakePlan:
    """核验批准清单 → 生成**确定性脱敏**计划（**零写入**；不一致 → fail-closed）。

    Raises:
        IntakePlanArgumentError: ``moment`` 未带时区。
        IntakePlanStateError: 批准条目无法映射为 operator scope。
        IntakePlanInconsistentError: 任一最终写入前核验不通过（**零写入**，绝不自动 intake）。
    """
    moment = _require_aware(moment, field_name="moment")
    canonical = build_approved_intake_list(scan, ledger, moment=moment)
    violations = _verify_against_canonical(
        scan, ledger, approved_list, canonical.approved, moment=moment
    )
    if violations:
        raise IntakePlanInconsistentError(violations)
    ledger_digest = tuple(
        (record.fingerprint, record.revision, record.decision_id)
        for record in ledger.latest_decisions()
    )
    entries = tuple(
        sorted(
            (_plan_entry(entry, ledger) for entry in canonical.approved),
            key=lambda item: item.fingerprint,
        )
    )
    status = (
        IntakePlanStatus.READY_FOR_EXPLICIT_INTAKE
        if entries
        else IntakePlanStatus.BLOCKED_NO_APPROVED_EVIDENCE
    )
    return IntakePlan(
        generated_at=moment,
        inbox_dir=Path(scan.inbox_dir),
        status=status,
        entries=entries,
        handoff=intake_plan_handoff(entries, inbox_dir=Path(scan.inbox_dir)),
        plan_id=compute_plan_id(entries, ledger_digest),
        ledger_digest=ledger_digest,
        ledger_path=ledger_path,
        approved_list_path=approved_list_path,
        approved_list_generated_at=approved_list.generated_at.isoformat(),
        inbox_packages=len(scan.packages),
        preflight_pass=len(scan.preflight_pass),
        ledger_fingerprints=len(ledger.latest_decisions()),
    )



# ---------------------------------------------------------------------------
# 只读运行入口（默认零写入；只有显式 out_path 才原子落盘计划本身）
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise IntakePlanWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def _compose_plan(
    root: Path,
    *,
    moment: datetime,
    ledger_path: Path | None,
    approved_list_path: Path,
) -> IntakePlan:
    """读 ledger（只读）→ 读批准清单（只读）→ 扫描 inbox（只读）→ 核验并生成计划（零写入）。"""
    ledger: ReviewLedger | None = None
    if ledger_path is not None:
        try:
            ledger = load_review_ledger(ledger_path)
        except ReviewLedgerStateError as exc:
            raise IntakePlanStateError(_safe(str(exc), max_chars=300)) from exc
    if ledger is None:
        # 文件不存在 / 未给出 = 没有任何历史决策（不是"损坏"）；此时批准清单必须为空，
        # 否则核验会判 LEDGER_MISSING_APPROVAL 并 fail-closed。
        ledger = ReviewLedger(generated_at=moment)
    approved_list = load_approved_intake_list(approved_list_path)
    try:
        scan = scan_inbox(root, moment=moment)
    except InboxError as exc:
        raise IntakePlanPathError(_safe(str(exc), max_chars=300)) from exc
    return build_intake_plan(
        scan,
        ledger,
        approved_list,
        moment=moment,
        ledger_path=ledger_path,
        approved_list_path=approved_list_path,
    )


def run_intake_plan(
    inbox_dir: Path | str,
    *,
    moment: datetime,
    ledger_path: Path | str | None = None,
    approved_list_path: Path | str | None = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> IntakePlan:
    """执行**一次** intake plan 生成（默认只读；只有显式 ``out_path`` 才写计划文件）。

    安全语义：

    - ``ledger_path`` / ``approved_list_path`` 只读；损坏 / 被篡改 →
      :class:`IntakePlanStateError`（零写入，旧文件原样保留）；
    - 只有 ``out_path`` 才写**计划本身**，且先取**单实例锁**再重新核验（避免并发读改写竞态）；
      锁冲突 → :class:`~src.evidence.readiness_runner.LockConflictError` 且**零写入**；
    - 任何核验不一致 → :class:`IntakePlanInconsistentError` 且**零写入**；
    - 本函数**没有**任何 intake / commit 调用，**绝不**写数据库，**绝不**移动 / 删除 / 改写
      inbox 内原始 evidence，**绝不**解除 blocker；
    - 四个安全字段恒为 true / true / false / false（硬编码），与批准数量无关。

    Raises:
        IntakePlanArgumentError: ``moment`` 未带时区，或缺 ``ledger_path`` /
            ``approved_list_path``。
        IntakePlanPathError: inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。
        IntakePlanStateError: ledger / 批准清单损坏或被篡改（fail-closed）。
        IntakePlanInconsistentError: 最终写入前核验不通过（fail-closed）。
        LockConflictError: 另一个计划生成 / 复核 / 重扫正持有活动锁（fail-closed，零写入）。
        IntakePlanWriteError: 计划原子写失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(inbox_dir)
    if not root.is_dir():
        raise IntakePlanPathError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    if approved_list_path is None:
        raise IntakePlanArgumentError(
            "必须显式给出 GOLD-012 批准清单（--approved-list）：本工具不猜、不自动生成批准"
        )
    if ledger_path is None:
        raise IntakePlanArgumentError(
            "必须显式给出 GOLD-012 review ledger（--ledger）：批准来源必须可核验"
        )
    ledger_target = Path(ledger_path)
    approved_target = Path(approved_list_path)
    out = Path(out_path) if out_path is not None else None
    for target in (ledger_target, approved_target, out):
        if target is None:
            continue
        try:
            ensure_outside_inbox(root, target)
        except InboxError as exc:
            raise IntakePlanPathError(_safe(str(exc), max_chars=300)) from exc

    def _compose() -> IntakePlan:
        return _compose_plan(
            root,
            moment=moment,
            ledger_path=ledger_target,
            approved_list_path=approved_target,
        )

    if out is None:
        return _compose()
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + INTAKE_PLAN_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        plan = _compose()
        _atomic_write_json(out, plan.to_dict(), what="intake plan")
    return replace(plan, written_path=_safe_path(out))



def render_intake_plan_summary(plan: IntakePlan) -> str:
    """渲染人类可读的计划摘要（脱敏；**不解除** blocker、**不自动** intake）。"""
    payload = plan.to_dict()
    verification = payload["verification"]
    approved_count = payload["approved_for_explicit_intake_count"]
    qualified_count = payload["data_qualification_passed_count"]
    lines: list[str] = [
        "# Evidence Intake Plan（最终写入前的只读核验）",
        "",
        f"> {INTAKE_PLAN_NOTE}",
        "",
        "## 1. 运行概览",
        "",
        f"- plan_id（内容级）：`{plan.plan_id}`；状态：`{plan.status.value}`；"
        f"执行模式：`{PLAN_EXECUTION_MODE}`",
        f"- inbox 目录：`{_safe_path(plan.inbox_dir)}`（只读发现；原始文件未被移动 / 删除）",
        f"- review ledger：`{_safe_path(plan.ledger_path)}`；批准清单："
        f"`{_safe_path(plan.approved_list_path)}`",
        f"- 当前扫描候选包：{verification['inbox_packages']}；"
        f"`PREFLIGHT_PASS`：{verification['preflight_pass']}；"
        f"ledger 已复核指纹：{verification['ledger_fingerprints']}",
        f"- 审计时点（UTC）：{plan.generated_at.isoformat()}",
        "- 核验结论：**0 项不一致**（任一不一致都会 fail-closed 且不产出计划）",
    ]
    if plan.written_path is not None:
        lines.append(f"- 计划已原子写入：`{plan.written_path}`")
    lines += ["", "## 2. approved_for_explicit_intake（**仍需人工显式 intake**）", ""]
    if not plan.entries:
        lines.append(
            "- —（当前没有仍成立的批准；`PHASE3_3_DATA` 保持 **BLOCKED**，"
            "请先按 GOLD-011/012 摆放候选并人工复核）"
        )
    for entry in plan.entries:
        lines.append(
            f"- `{entry.package_dir}`（指纹前 16 位 `{entry.fingerprint[:16]}`；"
            f"rev {entry.revision}）：{entry.evidence_type} / {entry.source}；"
            f"文件 {list(entry.evidence_files)}；行数 {entry.rows}"
            f"（可接收 {entry.acceptable_rows}）；人工批准于 {entry.reviewed_at}"
            f"（reviewer `{_safe(entry.reviewer, max_chars=100)}`）"
        )
    lines += [
        "",
        "## 3. 口径与边界（**data qualification 仍未通过**）",
        "",
        f"- `approved_for_explicit_intake_count` = {approved_count}；"
        f"`data_qualification_passed_count` = {qualified_count}"
        "（**恒为 0**：批准数量不改变资格）",
        f"- `blocker_code` = `{PHASE3_3_BLOCKER_CODE}`；`blocker_active` = true；"
        "`human_gate_required` = true",
        "- `data_qualification_passed` = false；`phase_transition_allowed` = false；"
        "`auto_intake_allowed` = false；`writes_database` = false",
        "",
        "## 4. 后续人工显式动作（handoff，**本工具绝不执行**）",
        "",
        f"- {INTAKE_PLAN_HANDOFF_NOTE}",
    ]
    if not plan.handoff:
        lines.append("- —（没有需要显式 intake 的候选）")
    for step in plan.handoff:
        lines.append(
            f"- 指纹前 16 位 `{step.fingerprint[:16]}` → `{step.command}`"
            f"（必带 `{step.required_flag}`；执行前补显式 `--manifest`，"
            "执行后 `handoff` / `recheck`）"
        )
    lines += ["", "## 5. 说明", ""]
    lines += [f"- {note}" for note in plan.notes]
    lines += [""]
    return "\n".join(lines)

