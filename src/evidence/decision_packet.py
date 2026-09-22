"""Evidence Qualification **人工决策包**（**GOLD-015**，纯本地只读 / fail-closed）。

GOLD-005 ~ GOLD-014 已经把**证据链**逐段交付齐了：合规授权证据接收（GOLD-005 / 007）、
就绪度与一键复核（GOLD-006）、人工交接包（GOLD-008）、readiness 变更通知与本地 tick runner
（GOLD-009 / 010）、本地 inbox 发现与预检（GOLD-011）、人工复核决策与批准清单（GOLD-012）、
最终写入前 intake plan 门禁（GOLD-013）与执行后的 Intake Receipt 审计（GOLD-014）。
但"到底能不能把这件事提交 **L3 人工 Gate**"过去仍要人工在多个 JSON 之间来回比对：
readiness / handoff 说够不够、批准清单与 plan 是不是**当前**事实、人工显式落库有没有可核验
收据、recheck 有没有跑过、这些 artifact 是不是**同一批**内容。

本模块补上**最后一张只读聚合视图**：把 ① 最新 readiness / handoff、② 批准清单与 GOLD-013
intake plan、③ GOLD-014 verified receipt（可选的 GOLD-014 收据文件再做一次交叉核对）与
④ qualification recheck 聚合为**确定性、脱敏、内容寻址**（``packet_id``）的 **L3 人工决策包**。

- **纯本地 / 只读 / 零网络 / 零数据库**：只**读**显式给出的 handoff / readiness state /
  inbox / ledger / 批准清单 / plan / 收据 / 执行结果 / recheck 文件；**复用** GOLD-013 的
  :func:`~src.evidence.intake_plan.build_intake_plan` 与 GOLD-014 的
  :func:`~src.evidence.intake_receipt.build_intake_receipt` /
  :func:`~src.evidence.intake_receipt.compute_receipt_id` 做重新绑定核验，**不复制、不降低**
  任何资格规则；默认**零写入**，只有显式 ``--out`` 才**原子**落盘 packet 本身；本模块
  **没有**任何 intake / commit 调用，**绝不**写研究数据库，**绝不**移动 / 删除 / 改写 inbox
  内任何原始 evidence，**绝不**修改 ``.ai/PROJECT_STATE.json``、**绝不**解除 blocker；
- **聚合（逐项 fail-closed）**：①handoff 必须**结构自洽**（文档标识 / schema / 契约版本 /
  四个安全字段不可被削弱 / ``thresholds`` 必须等于**当前**代码里的唯一阈值来源 / 内部算术自洽
  （``status`` / ``remaining`` / ``remaining_checks`` 必须与 ``current`` / ``required`` /
  ``comparator`` / ``evaluable`` 及 ``ready_for_human_review`` 一致）/ **不得**出现证据时间键）；
  ②给出 ``--readiness`` 时该快照必须与 handoff **同源**（用 ``readiness_watch.build_snapshot``
  从 handoff **重新推导**的快照 state 与**指纹**必须逐字段一致）；③批准清单 / plan / 人工显式
  执行结果 / recheck 的核验**沿用 GOLD-014 的稳定原因码**（plan stale / fingerprint drift /
  review override / 执行结果缺失或 dry-run 或零落库或计数矛盾或输入内容不符 / recheck 缺失或
  早于执行或被改写 一律 fail-closed）；④handoff 不得早于最近一次人工显式执行（否则视为
  ``HANDOFF_STALE``：readiness 没有覆盖新落库的证据）；⑤显式给出 GOLD-014 收据文件时，其
  ``receipt_id`` 必须**内容寻址自洽**，且 ``plan_id`` / 指纹 / review revision / 执行摘要 /
  recheck 摘要必须与**当前**重新绑定结果一致（否则 ``RECEIPT_ID_MISMATCH`` /
  ``PLAN_ID_MISMATCH`` / ``FINGERPRINT_MISMATCH`` / ``REVIEW_REVISION_MISMATCH`` /
  ``RECHECK_MISMATCH``）；
- **五个布尔显式且互不蕴含**：``evidence_ready_for_human_review``（只由 handoff + 可选
  readiness 快照推导）、``receipt_verified``（GOLD-014 重新绑定核验是否通过）、
  ``qualification_recheck_ready``（recheck 存在、绑定成功且自报 ``ready=true``）是三个**独立**
  事实；``data_qualification_passed`` / ``phase_transition_allowed`` **恒为** ``false``
  （**硬编码**），``blocker_active`` / ``human_gate_required`` 恒为 ``true``（**硬编码**）、
  ``human_gate_level`` 恒为 ``L3``；本工具**只能**给出
  ``submit_to_l3_human_gate``（= 三个事实**同时**成立）与缺口 / 稳定原因码；
  **绝不**把后两项置为 true，**绝不**自动切换 Phase；
- **绝不把操作时间当证据**：``generated_at`` / ``handoff.as_of`` / ``plan.generated_at`` /
  ``recheck.as_of`` / ``receipt_at`` / 文件 mtime **都只是审计操作时间**，packet 产出里
  **没有** ``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at`` /
  ``availability_provenance`` 这些证据时间键（加载输入时也会拒绝含这些键的文档），也**不会**
  由操作时间推导任何证据时间；
- **幂等 / 内容寻址**：``packet_id`` 只由（策略块 + handoff state / 产物摘要 + 可选 readiness
  摘要 + ``plan_id`` + 收据摘要 + recheck 摘要 + 五个布尔 + 状态）派生，**不含**
  ``generated_at``；同一输入**重复生成**得到同一 ``packet_id``（同一审计时点下文档逐字节稳定），
  任一关键输入变化必然产生新 ``packet_id`` 或直接 fail-closed（旧 packet **绝不静默继承**）。

入口：``scripts/evidence_decision_packet.py``（``--handoff`` / ``--inbox-dir`` / ``--ledger`` /
``--approved-list`` / ``--plan`` 必填；``--readiness`` / ``--receipt`` / ``--operator-result`` /
``--recheck`` 可选；``--out`` 是**唯一**写开关；**没有**任何 intake / ``--no-dry-run`` 参数）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common import hashing
from src.common.redaction import safe_text
from src.evidence import readiness_runner as runner
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.handoff import (
    BLOCKED_STATUS,
    HANDOFF_REPORT_NAME,
    HANDOFF_SCHEMA_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
    EvidenceHandoffReport,
    ScopeGap,
    build_evidence_checklist,
    build_excluded_evidence,
)
from src.evidence.handoff import thresholds as handoff_thresholds
from src.evidence.inbox import (
    InboxError,
    InboxPreflightReport,
    ensure_outside_inbox,
    scan_inbox,
)
from src.evidence.intake_plan import (
    OPERATOR_EXPLICIT_FLAG,
    ApprovedListDocument,
    IntakePlanStateError,
    load_approved_intake_list,
)
from src.evidence.intake_receipt import (
    INTAKE_RECEIPT_KIND,
    INTAKE_RECEIPT_REPORT_NAME,
    INTAKE_RECEIPT_SCHEMA_VERSION,
    OPERATOR_MANIFEST_REPORT,
    IntakePlanDocument,
    IntakeReceipt,
    IntakeReceiptStateError,
    IntakeReceiptStatus,
    IntakeReceiptVerificationError,
    OperatorResult,
    QualificationRecheck,
    ReceiptEntry,
    build_intake_receipt,
    compute_receipt_id,
    load_intake_plan_document,
    load_operator_result,
    load_qualification_recheck,
)
from src.evidence.readiness_watch import (
    MAX_CODE_CHARS,
    SNAPSHOT_KIND,
    SNAPSHOT_SCHEMA_VERSION,
    ReadinessSnapshot,
    SnapshotStateError,
    atomic_write_text,
    build_snapshot,
    load_snapshot_state,
)
from src.evidence.review import (
    FORBIDDEN_EVIDENCE_FIELDS,
    ReviewLedger,
    ReviewLedgerStateError,
    load_review_ledger,
)
from src.monitoring.evidence_readiness import ReadinessCheck
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE, CheckStatus

__all__ = [
    "DECISION_PACKET_FILE_NAME",
    "DECISION_PACKET_KIND",
    "DECISION_PACKET_LOCK_SUFFIX",
    "DECISION_PACKET_NOTE",
    "DECISION_PACKET_REPORT_NAME",
    "DECISION_PACKET_SCHEMA_VERSION",
    "PACKET_EXECUTION_MODE",
    "PACKET_NEXT_STEP_NOTE",
    "PACKET_TIME_SEMANTICS_NOTE",
    "READINESS_CHECK_KEYS",
    "RECEIPT_ENTRY_KEYS",
    "SCOPE_GAP_KEYS",
    "DecisionPacket",
    "DecisionPacketArgumentError",
    "DecisionPacketError",
    "DecisionPacketPathError",
    "DecisionPacketStateError",
    "DecisionPacketStatus",
    "DecisionPacketVerificationError",
    "DecisionPacketWriteError",
    "HandoffDocument",
    "PacketVerificationCode",
    "PacketViolation",
    "ReadinessStateDocument",
    "ReceiptDocument",
    "build_decision_packet",
    "compute_packet_id",
    "exit_code_for",
    "load_handoff_document",
    "load_intake_receipt_document",
    "load_readiness_state_document",
    "render_decision_packet_summary",
    "run_decision_packet",
    "verify_decision_packet",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
DECISION_PACKET_SCHEMA_VERSION: Final[int] = 1
#: packet 文档标识 / 报告标识（稳定，供上游与人工日志解析）
DECISION_PACKET_KIND: Final[str] = "evidence_qualification_decision_packet"
DECISION_PACKET_REPORT_NAME: Final[str] = "evidence_qualification_decision_packet"
#: packet 的默认文件名（operator 放在显式工作目录里；**默认不写**）
DECISION_PACKET_FILE_NAME: Final[str] = "evidence_qualification_decision_packet.json"
#: packet 的单实例锁后缀（与 ``out_path`` 同级）
DECISION_PACKET_LOCK_SUFFIX: Final[str] = ".lock"
#: 执行模式（**恒为只读聚合**：本工具不执行 intake、不写数据库、不切 Phase）
PACKET_EXECUTION_MODE: Final[str] = "READ_ONLY_L3_DECISION_PACKET"

#: handoff 报告里必须出现的字段（GOLD-008 ``EvidenceHandoffReport.to_dict()`` 的可核验子集）
HANDOFF_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "report",
    "contract_version",
    "as_of",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "status",
    "quantified_thresholds_met",
    "ready_for_human_review",
    "data_qualification_passed",
    "phase_transition_allowed",
    "thresholds",
    "author",
    "news",
    "total_remaining_gap_count",
    "quarantine_reason_counts",
)
#: handoff 单个 scope 缺口视图必须包含的字段（GOLD-008 ``ScopeGap.to_dict()`` 全量键）
SCOPE_GAP_KEYS: Final[tuple[str, ...]] = (
    "scope",
    "status",
    "eligible",
    "required",
    "remaining",
    "certified",
    "not_oos_eligible",
    "coverage_applicable",
    "coverage_days",
    "coverage_required",
    "coverage_remaining",
    "source_share_applicable",
    "max_source_share",
    "source_share_limit",
    "source_share_evaluable",
    "source_share_remaining",
    "remaining_checks",
    "checks",
)
#: handoff 单条就绪度检查必须包含的字段（``ReadinessCheck.to_dict()`` 全量键）
READINESS_CHECK_KEYS: Final[tuple[str, ...]] = (
    "key",
    "scope",
    "metric",
    "current",
    "required",
    "comparator",
    "status",
    "evaluable",
    "remaining",
    "reason",
    "evidence_start",
    "evidence_end",
)
#: GOLD-014 收据里单条已绑定批准必须包含的字段（``ReceiptEntry.to_dict()`` 的可核验子集）
RECEIPT_ENTRY_KEYS: Final[tuple[str, ...]] = (
    "fingerprint",
    "decision_id",
    "revision",
    "operator_scope",
    "package_dir",
    "evidence_files",
    "content_sha256",
    "rows",
    "acceptable_rows",
    "intake_executed",
)
#: 证据时间键（与 GOLD-012 / GOLD-014 同一常量来源；packet 里**绝不**允许出现这些键）
_FORBIDDEN_KEYS: Final[tuple[str, ...]] = FORBIDDEN_EVIDENCE_FIELDS
#: 64 位小写十六进制（``plan_id`` / ``receipt_id`` / ``packet_id`` / 指纹 / 摘要统一校验）
_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
#: 允许的比较符（就绪度检查只有这两种口径；出现其它值即视为被篡改）
_COMPARATORS: Final[frozenset[str]] = frozenset({">=", "<="})
#: 支持的证据类别（直接取自 ``evidence-intake-v1`` 契约，**不另造词表**）
_EVIDENCE_SCOPES: Final[frozenset[str]] = frozenset(member.value for member in EvidenceScope)

#: 固定说明：decision packet 只是**人工 Gate 前的只读汇总**，不具备解除 blocker 的能力
DECISION_PACKET_NOTE: Final[str] = (
    "本工具只做**L3 人工 Gate 之前**的**只读**聚合：把 readiness / handoff、批准清单与 "
    "GOLD-013 plan、GOLD-014 收据与 qualification recheck 绑成一份可复核的决策包；它"
    "**不写数据库**、**不调用任何 intake / commit**、**不移动 / 删除 / 改写**任何原始 evidence，"
    "也**不会**解除 `PHASE3_3_DATA`。`evidence_ready_for_human_review` / `receipt_verified` / "
    "`qualification_recheck_ready` 只说明「量化门槛是否达标」「显式落库是否可核验」「复核是否已"
    "完成」，**绝不等于** data qualification PASS；`data_qualification_passed` / "
    "`phase_transition_allowed` 恒为 false；Phase 切换仍是 **L3 人工 Gate**。"
)
#: 固定说明：packet 里的时间**只是审计操作时间**，绝不当证据时间
PACKET_TIME_SEMANTICS_NOTE: Final[str] = (
    "packet 只记录审计操作时间（generated_at / handoff.as_of / plan.generated_at / "
    "operator_results[].generated_at / qualification_recheck.as_of / receipt_at）；它们**不是**"
    "证据时间，绝不用于冒充 published_at、collected_at、effective_at 或 availability / OOS 证据，"
    "也不会由它们推导任何证据时间；文件 mtime 同样不被采信。"
)
#: 固定说明：下一步仍是人工动作
PACKET_NEXT_STEP_NOTE: Final[str] = (
    "`submit_to_l3_human_gate=true` 只表示「量化门槛达标 + 显式落库可核验 + 复核已完成」三件事"
    "**同时成立**，可提交 **L3 人工 Gate** 复核真实授权与历史可用性证据；它**不是**资格通过，"
    "**不会**自动解除 `PHASE3_3_DATA`、**不会**自动切换 Phase，也不代表任何数据已被允许用于训练。"
    "真实证据不足时必须保持 BLOCKED。"
)


class DecisionPacketStatus(StrEnum):
    """packet 状态（``READY_FOR_L3_HUMAN_GATE`` 只表示"可提交人工 Gate"，**不是**资格通过）。"""

    READY_FOR_L3_HUMAN_GATE = "READY_FOR_L3_HUMAN_GATE"
    BLOCKED_PENDING_EVIDENCE = "BLOCKED_PENDING_EVIDENCE"


class PacketVerificationCode(StrEnum):
    """决策包聚合的稳定原因码（任何一条都意味着 **fail-closed**，绝不产出 packet）。

    note:
        与 GOLD-014 同义的核验失败**直接沿用**其稳定原因码字符串（不另造词），例如
        ``PLAN_STALE`` / ``PLAN_ENTRY_MISMATCH`` / ``PLAN_TAMPERED`` / ``OPERATOR_*`` /
        ``RECHECK_*`` / ``FUTURE_TIMESTAMP``；结构性错误（缺失 / 损坏 / 被篡改）的异常消息也以
        本枚举的编码开头，便于人工与脚本稳定解析。
    """

    # ---- handoff / readiness（GOLD-008 / 010 产物）------------------------
    HANDOFF_TAMPERED = "HANDOFF_TAMPERED"
    HANDOFF_STALE = "HANDOFF_STALE"
    READINESS_TAMPERED = "READINESS_TAMPERED"
    READINESS_MISMATCH = "READINESS_MISMATCH"
    # ---- GOLD-014 收据文件的交叉核对 --------------------------------------
    RECEIPT_TAMPERED = "RECEIPT_TAMPERED"
    RECEIPT_ID_MISMATCH = "RECEIPT_ID_MISMATCH"
    RECEIPT_MISMATCH = "RECEIPT_MISMATCH"
    PLAN_ID_MISMATCH = "PLAN_ID_MISMATCH"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    REVIEW_REVISION_MISMATCH = "REVIEW_REVISION_MISMATCH"
    RECHECK_MISMATCH = "RECHECK_MISMATCH"
    # ---- 时间语义（操作时间不得越界，也不得冒充证据时间）-------------------
    EVIDENCE_TIME_SUBSTITUTION = "EVIDENCE_TIME_SUBSTITUTION"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"


class DecisionPacketError(RuntimeError):
    """packet 层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class DecisionPacketArgumentError(DecisionPacketError):
    """参数错误（缺 handoff / plan / inbox / ledger / approved list，时区缺失等）。"""


class DecisionPacketStateError(DecisionPacketError):
    """handoff / readiness / plan / 收据 / 执行结果 / 复核结果损坏、被篡改或被削弱安全字段。"""


class DecisionPacketPathError(DecisionPacketError):
    """inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。"""


class DecisionPacketVerificationError(DecisionPacketError):
    """聚合核验不通过（**fail-closed**：绝不产出 packet，绝不把"工具跑完"当成资格）。"""

    def __init__(self, violations: Sequence[PacketViolation]) -> None:
        self.violations: tuple[PacketViolation, ...] = tuple(violations)
        codes = sorted({item.code for item in self.violations})
        detail = "、".join(codes[:8]) if codes else "UNKNOWN"
        super().__init__(
            "decision packet 聚合核验不通过（fail-closed；零写入、绝不把操作时间当证据、"
            f"绝不解除 blocker）：{len(self.violations)} 项不一致；原因码：{detail}"
        )


class DecisionPacketWriteError(DecisionPacketError):
    """packet 原子写失败（**fail-closed**）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: packet 已生成且**三个事实同时成立**（可提交 L3 人工 Gate；仍**不是**资格通过）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数错误（缺 handoff / plan / inbox / ledger / approved list，时区缺失等）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输入 / 输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: 任一输入缺失 / 损坏 / stale / 篡改 / 核验不通过（全部 fail-closed）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: packet 已生成但**不可提交**（证据未达标 / 无收据 / 复核未完成；**预期 BLOCKED**）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一个 packet / 计划 / 复核 / 重扫正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_PACKET_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (DecisionPacketArgumentError, EXIT_CONFIG_ERROR),
    (DecisionPacketStateError, EXIT_STATE_INVALID),
    (DecisionPacketVerificationError, EXIT_STATE_INVALID),
    (DecisionPacketPathError, EXIT_UNUSABLE),
    (DecisionPacketWriteError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _PACKET_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（原因文本 / 来源名 / 路径共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径脱敏 + 截断（只保留可人工核对的形式，绝不回显凭据）。"""
    return safe_text(str(value), max_chars=max_chars)


def _require_aware(moment: datetime, *, field_name: str) -> datetime:
    """要求时间戳带时区（naive 时间一律拒绝）。"""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DecisionPacketArgumentError(f"{field_name} 必须带时区")
    return moment


def _parse_aware_moment(raw: object, *, field_name: str) -> datetime:
    """解析 ISO8601 时间戳并要求带时区（state 文件里的时间一律显式校验）。"""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise DecisionPacketStateError(f"{field_name} 无法解析为 ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionPacketStateError(f"{field_name} 必须带时区")
    return parsed


def _require_bool(raw: object, *, field_name: str) -> bool:
    """严格布尔（整数 / 字符串一律拒绝：避免把 0/1 或 "false" 当布尔静默采信）。"""
    if not isinstance(raw, bool):
        raise DecisionPacketStateError(f"{field_name} 必须是布尔值")
    return raw


def _require_int(raw: object, *, field_name: str) -> int:
    """严格整数（布尔 / 浮点 / 字符串一律拒绝）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DecisionPacketStateError(f"{field_name} 必须是整数")
    return raw


def _require_number(raw: object, *, field_name: str) -> int | float:
    """严格数值（布尔 / 字符串 / ``None`` 一律拒绝；int 与 float 都接受）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DecisionPacketStateError(f"{field_name} 必须是数值")
    return raw


def _require_hex64(raw: object, *, field_name: str) -> str:
    """要求 64 位小写十六进制（内容级摘要 / 指纹统一口径）。"""
    text = str(raw or "")
    if not _HEX64.match(text):
        raise DecisionPacketStateError(f"{field_name} 必须是 64 位小写十六进制摘要")
    return text


def _reject_evidence_time_fields(payload: object, *, what: str) -> None:
    """任何输入文档里**绝不**允许出现证据时间键（操作时间一律不得当作证据时间）。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key) in _FORBIDDEN_KEYS:
                raise DecisionPacketStateError(
                    f"{PacketVerificationCode.EVIDENCE_TIME_SUBSTITUTION.value}："
                    f"{what} 含禁止的证据时间字段 {_safe(key, max_chars=40)}：fail-closed"
                    "（审计操作时间 / 复核时间一律不得当作证据时间）"
                )
            _reject_evidence_time_fields(value, what=what)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            _reject_evidence_time_fields(item, what=what)


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    """严格读取一个本地 JSON 对象（不存在 / 不可读 / 非对象 → fail-closed）。"""
    if not path.exists():
        raise DecisionPacketStateError(f"{what} 不存在：{_safe_path(path)}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecisionPacketStateError(f"{what} 不可读：{_safe_path(path)}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecisionPacketStateError(
            f"{what} 不是合法 JSON：{_safe(exc.msg, max_chars=120)}"
        ) from exc
    if not isinstance(payload, dict):
        raise DecisionPacketStateError(f"{what} 顶层必须是 JSON 对象")
    return payload


def _read_state_object(path: Path, *, what: str, code: str) -> dict[str, Any]:
    """读取输入文档，并在"缺失 / 不可读 / 非 JSON / 非对象"时带**稳定原因码** fail-closed。"""
    try:
        return _read_json_object(path, what=what)
    except DecisionPacketStateError as exc:
        if str(exc).startswith(code):
            raise
        raise DecisionPacketStateError(f"{code}：{exc}") from exc


def _policy_block() -> dict[str, Any]:
    """策略块（五个安全字段 + 只读执行模式；**硬编码**，与任何计数无关）。"""
    return {
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "packet_execution_mode": PACKET_EXECUTION_MODE,
        "auto_intake_allowed": False,
        "writes_database": False,
        "requires_explicit_operator_action": True,
        "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
        "human_gate_level": "L3",
    }


# ---------------------------------------------------------------------------
# 核验不一致（稳定原因码）+ 脱敏缺口视图
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PacketViolation:
    """一条聚合核验不一致（**稳定原因码** + 已脱敏说明；任一条都让 packet fail-closed）。"""

    code: str
    fingerprint: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**）。"""
        return {
            "code": self.code,
            "fingerprint": self.fingerprint,
            "detail": _safe(self.detail, max_chars=300),
        }


def _violation(code: str, fingerprint: str, detail: str) -> PacketViolation:
    """构造一条核验不一致（统一脱敏）。"""
    return PacketViolation(
        code=str(code), fingerprint=str(fingerprint), detail=_safe(detail, max_chars=300)
    )


def _check_view(check: ReadinessCheck) -> dict[str, Any]:
    """单条就绪度检查的**脱敏**视图（不含正文 / 来源配置；参数只保留可核对标量）。"""
    return {
        "key": _safe(check.key, max_chars=80),
        "scope": _safe(check.scope, max_chars=20),
        "metric": _safe(check.metric, max_chars=120),
        "current": check.current,
        "required": check.required,
        "comparator": check.comparator,
        "status": check.status.value,
        "evaluable": check.evaluable,
        "remaining": check.remaining,
    }


def _gap_view(gap: ScopeGap) -> dict[str, Any]:
    """单个 scope 缺口的**脱敏**视图（口径与 GOLD-008 交接包一致；不含正文）。"""
    return {
        "scope": _safe(gap.scope, max_chars=20),
        "status": _safe(gap.status, max_chars=40),
        "ready": gap.status == "PASS",
        "eligible": gap.eligible,
        "required": gap.required,
        "remaining": gap.remaining,
        "certified": gap.certified,
        "not_oos_eligible": gap.not_oos_eligible,
        "remaining_checks": gap.remaining_checks,
        "coverage_applicable": gap.coverage_applicable,
        "coverage_days": gap.coverage_days,
        "coverage_required": gap.coverage_required,
        "coverage_remaining": gap.coverage_remaining,
        "source_share_applicable": gap.source_share_applicable,
        "source_share_limit": gap.source_share_limit,
        "source_share_evaluable": gap.source_share_evaluable,
        "source_share_remaining": gap.source_share_remaining,
        "checks": [_check_view(check) for check in gap.checks],
    }


def _gap_digest(gap: ScopeGap) -> list[Any]:
    """缺口的内容级摘要（只取**会改变结论**的标量；不含任何时间与自由文本）。"""
    return [
        gap.scope,
        gap.status,
        gap.eligible,
        gap.required,
        gap.remaining,
        gap.certified,
        gap.not_oos_eligible,
        gap.remaining_checks,
        gap.coverage_applicable,
        gap.coverage_days,
        gap.coverage_required,
        gap.coverage_remaining,
        gap.source_share_applicable,
        gap.source_share_limit,
        gap.source_share_evaluable,
        gap.source_share_remaining,
        [
            [
                check.key,
                check.comparator,
                check.status.value,
                check.evaluable,
                check.current,
                check.required,
                check.remaining,
            ]
            for check in gap.checks
        ],
    ]


# ---------------------------------------------------------------------------
# handoff 报告（GOLD-008 产物）的严格只读视图（**复用**同一口径重新推导 readiness）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HandoffDocument:
    """已严格校验的 evidence handoff 报告（**只读视图**；≠ 任何资格结论 / 放行）。"""

    path: str
    artifact_sha256: str
    schema_version: int
    contract_version: str
    as_of: datetime
    status: str
    blocker_code: str
    ready_for_human_review: bool
    quantified_thresholds_met: bool
    total_remaining_gap_count: int
    report: EvidenceHandoffReport
    source: ReadinessSnapshot

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """仍未达标的稳定原因码集合（**直接复用** GOLD-009 快照的推导口径）。"""
        return self.source.reason_codes

    @property
    def unmet_check_keys(self) -> tuple[str, ...]:
        """仍未 PASS 的检查键（用于人工定位缺口；只是检查键，不含自由文本）。"""
        keys = {
            check.key
            for gap in (self.report.author, self.report.news)
            for check in gap.checks
            if check.status is not CheckStatus.PASS
        }
        return tuple(sorted(keys))

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "path": _safe_path(self.path),
            "artifact_sha256": self.artifact_sha256,
            "report": HANDOFF_REPORT_NAME,
            "schema_version": self.schema_version,
            "contract_version": _safe(self.contract_version, max_chars=60),
            "as_of": self.as_of.isoformat(),
            "status": _safe(self.status, max_chars=40),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "quantified_thresholds_met": self.quantified_thresholds_met,
            "ready_for_human_review": self.ready_for_human_review,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "total_remaining_gap_count": self.total_remaining_gap_count,
            "readiness_fingerprint": self.source.fingerprint,
            "reason_codes": list(self.reason_codes),
            "unmet_check_keys": list(self.unmet_check_keys),
            "thresholds": {
                key: float(value) for key, value in self.thresholds().items()
            },
        }

    def thresholds(self) -> dict[str, float]:
        """回显**当前**代码里的唯一阈值来源（GOLD-008 的 ``thresholds()``；绝不复制第二套）。"""
        return {key: float(value) for key, value in handoff_thresholds().items()}


def _optional_text(raw: object, *, max_chars: int) -> str | None:
    """可选文本（``None`` 保留，其余一律脱敏 + 截断）。"""
    if raw is None:
        return None
    return _safe(raw, max_chars=max_chars)


def _readiness_check(raw: object, *, index: str) -> ReadinessCheck:
    """把 handoff 一条就绪度检查解析为 :class:`ReadinessCheck`（缺字段 / 非法 → fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 检查[{index}] 必须是对象"
        )
    missing = [key for key in READINESS_CHECK_KEYS if key not in raw]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 检查[{index}] 缺字段："
            f"{'、'.join(sorted(missing))}"
        )
    comparator = str(raw["comparator"])
    if comparator not in _COMPARATORS:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 检查[{index}].comparator "
            f"非法：{_safe(comparator, max_chars=10)}"
        )
    try:
        status = CheckStatus(str(raw["status"]))
    except ValueError as exc:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 检查[{index}].status 非法："
            f"{_safe(raw['status'], max_chars=20)}"
        ) from exc
    return ReadinessCheck(
        key=_safe(raw["key"], max_chars=80),
        scope=_safe(raw["scope"], max_chars=20),
        metric=_safe(raw["metric"], max_chars=120),
        current=_require_number(raw["current"], field_name=f"handoff 检查[{index}].current"),
        required=_require_number(raw["required"], field_name=f"handoff 检查[{index}].required"),
        comparator=comparator,
        status=status,
        evaluable=_require_bool(raw["evaluable"], field_name=f"handoff 检查[{index}].evaluable"),
        remaining=_require_number(raw["remaining"], field_name=f"handoff 检查[{index}].remaining"),
        reason=_safe(raw["reason"], max_chars=300),
        evidence_start=_optional_text(raw["evidence_start"], max_chars=60),
        evidence_end=_optional_text(raw["evidence_end"], max_chars=60),
    )


def _scope_gap(raw: object, *, index: str) -> ScopeGap:
    """把 handoff 里的一个 scope 缺口解析为 :class:`ScopeGap`（缺字段 / 非法 → fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index} 必须是对象"
        )
    missing = [key for key in SCOPE_GAP_KEYS if key not in raw]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index} 缺字段："
            f"{'、'.join(sorted(missing))}"
        )
    raw_checks = raw["checks"]
    if isinstance(raw_checks, str) or not isinstance(raw_checks, Sequence) or not raw_checks:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index}.checks "
            "必须是非空数组"
        )
    checks = tuple(
        _readiness_check(item, index=f"{index}.checks[{position}]")
        for position, item in enumerate(raw_checks, start=1)
    )
    scope = _safe(raw["scope"], max_chars=20)
    if scope not in _EVIDENCE_SCOPES:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index}.scope 非法："
            f"{_safe(scope, max_chars=40)}"
        )
    prefix = f"handoff {index}"
    return ScopeGap(
        scope=scope,
        status=_safe(raw["status"], max_chars=40),
        eligible=_require_int(raw["eligible"], field_name=f"{prefix}.eligible"),
        required=_require_int(raw["required"], field_name=f"{prefix}.required"),
        remaining=_require_int(raw["remaining"], field_name=f"{prefix}.remaining"),
        certified=_require_int(raw["certified"], field_name=f"{prefix}.certified"),
        not_oos_eligible=_require_int(
            raw["not_oos_eligible"], field_name=f"{prefix}.not_oos_eligible"
        ),
        coverage_applicable=_require_bool(
            raw["coverage_applicable"], field_name=f"{prefix}.coverage_applicable"
        ),
        coverage_days=_require_int(raw["coverage_days"], field_name=f"{prefix}.coverage_days"),
        coverage_required=_require_int(
            raw["coverage_required"], field_name=f"{prefix}.coverage_required"
        ),
        coverage_remaining=_require_int(
            raw["coverage_remaining"], field_name=f"{prefix}.coverage_remaining"
        ),
        source_share_applicable=_require_bool(
            raw["source_share_applicable"], field_name=f"{prefix}.source_share_applicable"
        ),
        max_source_share=float(
            _require_number(raw["max_source_share"], field_name=f"{prefix}.max_source_share")
        ),
        source_share_limit=float(
            _require_number(raw["source_share_limit"], field_name=f"{prefix}.source_share_limit")
        ),
        source_share_evaluable=_require_bool(
            raw["source_share_evaluable"], field_name=f"{prefix}.source_share_evaluable"
        ),
        source_share_remaining=float(
            _require_number(
                raw["source_share_remaining"], field_name=f"{prefix}.source_share_remaining"
            )
        ),
        remaining_checks=_require_int(
            raw["remaining_checks"], field_name=f"{prefix}.remaining_checks"
        ),
        checks=checks,
    )


def _quarantine_counts(raw: object) -> tuple[tuple[str, int], ...]:
    """解析 handoff 的隔离原因码计数（结构非法 → fail-closed；只保留稳定代码与计数）。"""
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff quarantine_reason_counts "
            "必须是数组"
        )
    counts: list[tuple[str, int]] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping) or "reason_code" not in item or "count" not in item:
            raise DecisionPacketStateError(
                f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff "
                f"quarantine_reason_counts[{position}] 结构非法"
            )
        counts.append(
            (
                _safe(item["reason_code"], max_chars=60),
                _require_int(
                    item["count"], field_name=f"handoff quarantine_reason_counts[{position}].count"
                ),
            )
        )
    return tuple(sorted(counts))


def _verify_check_arithmetic(check: ReadinessCheck, *, where: str) -> None:
    """单条就绪度检查的**算术自洽**：``status`` / ``remaining`` 必须与阈值口径一致。"""
    if check.comparator == ">=":
        expected_status = (
            CheckStatus.PASS if check.current >= check.required else CheckStatus.BLOCKED
        )
        expected_remaining: int | float = max(0, check.required - check.current)
    else:
        expected_status = (
            CheckStatus.PASS
            if check.evaluable and check.current <= check.required
            else CheckStatus.BLOCKED
        )
        expected_remaining = (
            round(max(0.0, check.current - check.required), 6) if check.evaluable else 0.0
        )
    if check.status is not expected_status:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：{where}.status"
            f"（{check.status.value}）与 current / required / comparator / evaluable 不一致"
        )
    if float(check.remaining) != float(expected_remaining):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：{where}.remaining"
            f"（{check.remaining}）与 current / required 不一致（应为 {expected_remaining}）"
        )


def _verify_scope_arithmetic(gap: ScopeGap, *, index: str) -> None:
    """单个 scope 缺口的**算术自洽**（status / remaining / remaining_checks 必须自洽）。"""
    for position, check in enumerate(gap.checks, start=1):
        _verify_check_arithmetic(check, where=f"handoff {index}.checks[{position}]")
    blocked = sum(1 for check in gap.checks if check.status is not CheckStatus.PASS)
    if gap.remaining_checks != blocked:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index}.remaining_checks"
            f"（{gap.remaining_checks}）与仍未 PASS 的检查条数（{blocked}）不一致"
        )
    expected_remaining = max(0, gap.required - gap.eligible)
    if gap.remaining != expected_remaining:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index}.remaining"
            f"（{gap.remaining}）与 required / eligible 不一致（应为 {expected_remaining}）"
        )
    expected_status = "PASS" if blocked == 0 else "BLOCKED"
    if gap.status != expected_status:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff {index}.status"
            f"（{gap.status}）与检查结论不一致（应为 {expected_status}）"
        )


def _verify_thresholds(raw: object) -> None:
    """``thresholds`` 必须等于**当前**代码里的唯一阈值来源（GOLD-008 ``thresholds()``）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff thresholds 必须是对象"
        )
    declared: dict[str, float] = {}
    for key, value in raw.items():
        declared[str(key)] = float(
            _require_number(value, field_name=f"handoff thresholds.{_safe(key, max_chars=40)}")
        )
    current = {str(key): float(value) for key, value in handoff_thresholds().items()}
    if declared != current:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff thresholds 与当前代码里的"
            "唯一阈值来源不一致（拒绝被放宽 / 篡改的阈值）"
        )


@dataclass(frozen=True, slots=True)
class _HandoffHeader:
    """handoff 报告的**标量**校验结果（内部使用；只在通过全部校验后构造）。"""

    as_of: datetime
    status: str
    contract_version: str
    ready_for_human_review: bool
    quantified_thresholds_met: bool
    total_remaining_gap_count: int
    notes: tuple[str, ...]


def _safe_notes(raw: object) -> tuple[str, ...]:
    """handoff 的 notes（可缺省；必须是字符串数组，逐条脱敏）。"""
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff notes 必须是数组"
        )
    return tuple(_safe(item, max_chars=300) for item in raw)


def _validate_handoff_payload(payload: Mapping[str, Any]) -> _HandoffHeader:
    """校验 handoff 的标识 / 安全字段 / 阈值 / 标量自洽（任一不合法 → fail-closed）。"""
    missing = [key for key in HANDOFF_REQUIRED_KEYS if key not in payload]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 报告缺字段："
            f"{'、'.join(sorted(missing))}"
        )
    if str(payload.get("report") or "") != HANDOFF_REPORT_NAME:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：文件不是 GOLD-008 "
            "evidence handoff 报告"
        )
    if payload.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff schema_version 不受支持："
            f"{payload.get('schema_version')!r}"
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff contract_version 与 "
            f"{EVIDENCE_CONTRACT_VERSION} 不一致"
        )
    if str(payload.get("blocker_code") or "") != PHASE3_3_BLOCKER_CODE:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff blocker_code 必须是 "
            f"{PHASE3_3_BLOCKER_CODE}"
        )
    if payload.get("blocker_active") is not True or payload.get("human_gate_required") is not True:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 声称 blocker 已解除或无需"
            "人工 Gate：安全字段不可被改写（fail-closed）"
        )
    if (
        payload.get("data_qualification_passed") is not False
        or payload.get("phase_transition_allowed") is not False
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 声称资格已通过或已允许 "
            "Phase 切换：安全字段不可被改写（fail-closed）"
        )
    _reject_evidence_time_fields(payload, what="evidence handoff 报告")
    _verify_thresholds(payload["thresholds"])
    ready = _require_bool(
        payload["ready_for_human_review"], field_name="handoff ready_for_human_review"
    )
    quantified = _require_bool(
        payload["quantified_thresholds_met"], field_name="handoff quantified_thresholds_met"
    )
    if quantified != ready:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff quantified_thresholds_met"
            " 与 ready_for_human_review 必须一致"
        )
    status = _safe(payload["status"], max_chars=40)
    expected_status = PENDING_HUMAN_REVIEW_STATUS if ready else BLOCKED_STATUS
    if status != expected_status:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff status（{status}）与 "
            f"ready_for_human_review={str(ready).lower()} 不一致（应为 {expected_status}）"
        )
    total_remaining = _require_int(
        payload["total_remaining_gap_count"], field_name="handoff total_remaining_gap_count"
    )
    return _HandoffHeader(
        as_of=_parse_aware_moment(payload["as_of"], field_name="handoff as_of"),
        status=status,
        contract_version=str(contract or EVIDENCE_CONTRACT_VERSION),
        ready_for_human_review=ready,
        quantified_thresholds_met=quantified,
        total_remaining_gap_count=total_remaining,
        notes=_safe_notes(payload.get("notes")),
    )


def load_handoff_document(path: Path | str) -> HandoffDocument:
    """读取 GOLD-008 evidence handoff 报告并做**严格**校验（损坏 / 篡改 → fail-closed）。

    Raises:
        DecisionPacketStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 文档标识 / schema /
            契约版本 / 安全字段 / 阈值 / 缺口算术自洽 / 证据时间键 任一不合法。
    """
    target = Path(path)
    payload = _read_state_object(
        target,
        what="evidence handoff 报告",
        code=PacketVerificationCode.HANDOFF_TAMPERED.value,
    )
    header = _validate_handoff_payload(payload)
    author = _scope_gap(payload["author"], index="author")
    news = _scope_gap(payload["news"], index="news")
    if author.scope != EvidenceScope.AUTHOR.value or news.scope != EvidenceScope.NEWS.value:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff 的 author / news scope "
            "口径不一致"
        )
    _verify_scope_arithmetic(author, index="author")
    _verify_scope_arithmetic(news, index="news")
    if author.coverage_applicable or not news.coverage_applicable:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff coverage 口径不一致"
            "（author 不适用覆盖天数、news 必须适用）"
        )
    if header.total_remaining_gap_count != author.remaining_checks + news.remaining_checks:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.HANDOFF_TAMPERED.value}：handoff total_remaining_gap_count "
            "与各 scope 的 remaining_checks 之和不一致"
        )
    report = EvidenceHandoffReport(
        schema_version=HANDOFF_SCHEMA_VERSION,
        report=HANDOFF_REPORT_NAME,
        contract_version=header.contract_version,
        as_of=header.as_of,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=True,
        human_gate_required=True,
        status=header.status,
        quantified_thresholds_met=header.quantified_thresholds_met,
        ready_for_human_review=header.ready_for_human_review,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        author=author,
        news=news,
        total_remaining_gap_count=header.total_remaining_gap_count,
        checklist=build_evidence_checklist(),
        excluded_evidence=build_excluded_evidence(),
        quarantine_reason_counts=_quarantine_counts(payload["quarantine_reason_counts"]),
        batch=None,
        notes=header.notes,
    )
    return HandoffDocument(
        path=_safe_path(target),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        schema_version=HANDOFF_SCHEMA_VERSION,
        contract_version=header.contract_version,
        as_of=header.as_of,
        status=header.status,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        ready_for_human_review=header.ready_for_human_review,
        quantified_thresholds_met=header.quantified_thresholds_met,
        total_remaining_gap_count=header.total_remaining_gap_count,
        report=report,
        source=build_snapshot(report, generated_at=header.as_of),
    )


# ---------------------------------------------------------------------------
# readiness state（GOLD-010 tick 落盘产物）的严格只读视图
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReadinessStateDocument:
    """已严格校验的 readiness 状态快照（GOLD-010 ``readiness_state.json``；**只读视图**）。"""

    path: str
    artifact_sha256: str
    snapshot: ReadinessSnapshot

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**脱敏**；不含任何证据时间 / 正文 / 来源配置）。"""
        return {
            "path": _safe_path(self.path),
            "artifact_sha256": self.artifact_sha256,
            "kind": SNAPSHOT_KIND,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "fingerprint": self.snapshot.fingerprint,
            "generated_at": self.snapshot.generated_at.isoformat(),
            "status": _safe(self.snapshot.status, max_chars=40),
            "ready_for_human_review": self.snapshot.ready_for_human_review,
            "total_remaining_gap_count": self.snapshot.total_remaining_gap_count,
            "reason_codes": list(self.snapshot.reason_codes),
            "scopes": {
                item.scope: {
                    "eligible": item.eligible,
                    "required": item.required,
                    "remaining": item.remaining,
                    "status": _safe(item.status, max_chars=40),
                }
                for item in self.snapshot.scopes
            },
        }


def load_readiness_state_document(path: Path | str) -> ReadinessStateDocument:
    """读取 GOLD-010 落盘的 readiness 快照（损坏 / 指纹校验失败 → fail-closed）。

    Raises:
        DecisionPacketStateError: 文件不存在 / 不可读 / 结构非法 / 指纹校验失败 /
            声称 blocker 已解除 / 含证据时间键。
    """
    target = Path(path)
    try:
        snapshot = load_snapshot_state(target)
    except SnapshotStateError as exc:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.READINESS_TAMPERED.value}：readiness state 损坏或指纹校验"
            f"失败：{_safe(str(exc), max_chars=300)}"
        ) from exc
    if snapshot is None:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.READINESS_TAMPERED.value}：readiness state 不存在："
            f"{_safe_path(target)}"
        )
    if snapshot.blocker_code != PHASE3_3_BLOCKER_CODE:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.READINESS_TAMPERED.value}：readiness state 的 blocker_code "
            f"不是 {PHASE3_3_BLOCKER_CODE}"
        )
    _reject_evidence_time_fields(
        _read_state_object(
            target,
            what="readiness state",
            code=PacketVerificationCode.READINESS_TAMPERED.value,
        ),
        what="readiness state",
    )
    return ReadinessStateDocument(
        path=_safe_path(target),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# GOLD-014 intake receipt 的严格只读视图（**复用**其内容寻址口径，不复制规则）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReceiptDocument:
    """已严格校验的 GOLD-014 收据文件（**只读视图**；≠ 任何资格结论）。"""

    path: str
    artifact_sha256: str
    receipt: IntakeReceipt

    @property
    def receipt_id(self) -> str:
        """收据声明的**内容级** ``receipt_id``。"""
        return self.receipt.receipt_id

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**脱敏**；不含正文与任何证据时间）。"""
        return {
            "path": _safe_path(self.path),
            "artifact_sha256": self.artifact_sha256,
            "kind": INTAKE_RECEIPT_KIND,
            "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
            "receipt_id": self.receipt.receipt_id,
            "status": self.receipt.status.value,
            "plan_id": self.receipt.plan_id,
            "intake_executed": self.receipt.intake_executed,
            "receipt_verified": self.receipt.receipt_verified,
            "entry_count": len(self.receipt.entries),
            "landed_rows": self.receipt.landed_rows,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
        }


def _optional_path(raw: object) -> str | None:
    """可选路径字段（``None`` 保留；其余脱敏）。"""
    if raw is None:
        return None
    return _safe_path(raw)


def _sequence_field(raw: object, *, what: str) -> Sequence[Any]:
    """要求一个数组字段（字符串 / 对象 / ``None`` → fail-closed）。"""
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：{what} 必须是数组"
        )
    return raw


def _receipt_entry(raw: object, *, index: int) -> ReceiptEntry:
    """解析收据里的一条已绑定批准（缺字段 / 非法 → fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.executed_entries[{index}] "
            "必须是对象"
        )
    missing = [key for key in RECEIPT_ENTRY_KEYS if key not in raw]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.executed_entries[{index}] "
            f"缺字段：{'、'.join(sorted(missing))}"
        )
    raw_hashes = _sequence_field(
        raw["content_sha256"], what=f"receipt.executed_entries[{index}].content_sha256"
    )
    raw_files = _sequence_field(
        raw["evidence_files"], what=f"receipt.executed_entries[{index}].evidence_files"
    )
    return ReceiptEntry(
        fingerprint=_require_hex64(
            raw["fingerprint"], field_name=f"receipt.executed_entries[{index}].fingerprint"
        ),
        decision_id=_require_hex64(
            raw["decision_id"], field_name=f"receipt.executed_entries[{index}].decision_id"
        ),
        revision=_require_int(
            raw["revision"], field_name=f"receipt.executed_entries[{index}].revision"
        ),
        operator_scope=_safe(raw["operator_scope"], max_chars=40),
        package_dir=_safe_path(raw["package_dir"]),
        evidence_files=tuple(_safe_path(name) for name in raw_files),
        content_sha256=tuple(
            _require_hex64(value, field_name=f"receipt.executed_entries[{index}].content_sha256")
            for value in raw_hashes
        ),
        rows=_require_int(raw["rows"], field_name=f"receipt.executed_entries[{index}].rows"),
        acceptable_rows=_require_int(
            raw["acceptable_rows"], field_name=f"receipt.executed_entries[{index}].acceptable_rows"
        ),
        executed=_require_bool(
            raw["intake_executed"], field_name=f"receipt.executed_entries[{index}].intake_executed"
        ),
    )


_OPERATOR_RECEIPT_KEYS: Final[tuple[str, ...]] = (
    "path",
    "artifact_sha256",
    "report",
    "scope",
    "dry_run",
    "generated_at",
    "input_path",
    "input_sha256",
    "counts",
    "row_count",
)


def _receipt_operator_result(raw: object, *, index: int) -> OperatorResult:
    """解析收据里的一条人工显式执行结果（缺字段 / 非法 → fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}] "
            "必须是对象"
        )
    missing = [key for key in _OPERATOR_RECEIPT_KEYS if key not in raw]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}] "
            f"缺字段：{'、'.join(sorted(missing))}"
        )
    if str(raw["report"]) != OPERATOR_MANIFEST_REPORT:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}]"
            ".report 不是 Evidence Operator manifest 标识"
        )
    scope = _safe(raw["scope"], max_chars=40)
    if scope not in _EVIDENCE_SCOPES:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}]"
            f".scope 非法：{scope}"
        )
    raw_counts = raw["counts"]
    if not isinstance(raw_counts, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}]"
            ".counts 必须是对象"
        )
    counts = {
        str(key): _require_int(
            value, field_name=f"receipt.operator_results[{index}].counts.{_safe(key, max_chars=40)}"
        )
        for key, value in raw_counts.items()
    }
    if "persisted" not in counts:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.operator_results[{index}]"
            ".counts 缺 persisted"
        )
    return OperatorResult(
        path=_safe_path(raw["path"]),
        artifact_sha256=_require_hex64(
            raw["artifact_sha256"], field_name=f"receipt.operator_results[{index}].artifact_sha256"
        ),
        report=str(raw["report"]),
        scope=scope,
        dry_run=_require_bool(
            raw["dry_run"], field_name=f"receipt.operator_results[{index}].dry_run"
        ),
        generated_at=_parse_aware_moment(
            raw["generated_at"], field_name=f"receipt.operator_results[{index}].generated_at"
        ),
        input_path=_safe_path(raw["input_path"]),
        input_sha256=_require_hex64(
            raw["input_sha256"], field_name=f"receipt.operator_results[{index}].input_sha256"
        ),
        counts=counts,
        row_count=_require_int(
            raw["row_count"], field_name=f"receipt.operator_results[{index}].row_count"
        ),
    )


def _receipt_recheck(raw: object) -> QualificationRecheck:
    """解析收据里绑定的 qualification recheck（缺字段 / 非法 → fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.qualification_recheck "
            "必须是对象"
        )
    missing = [
        key
        for key in (
            "path",
            "artifact_sha256",
            "report",
            "as_of",
            "blocker_code",
            "blocker_active",
            "human_gate_required",
            "ready",
            "gate",
        )
        if key not in raw
    ]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.qualification_recheck "
            f"缺字段：{'、'.join(sorted(missing))}"
        )
    gate = raw["gate"]
    if not isinstance(gate, Mapping):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.qualification_recheck.gate "
            "必须是对象"
        )
    if str(raw["report"]) != "phase33_qualification_recheck":
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.qualification_recheck"
            ".report 不是 phase33 qualification recheck 产物"
        )
    return QualificationRecheck(
        path=_safe_path(raw["path"]),
        artifact_sha256=_require_hex64(
            raw["artifact_sha256"], field_name="receipt.qualification_recheck.artifact_sha256"
        ),
        report=str(raw["report"]),
        as_of=_parse_aware_moment(raw["as_of"], field_name="receipt.qualification_recheck.as_of"),
        blocker_code=_safe(raw["blocker_code"], max_chars=40),
        blocker_active=_require_bool(
            raw["blocker_active"], field_name="receipt.qualification_recheck.blocker_active"
        ),
        human_gate_required=_require_bool(
            raw["human_gate_required"],
            field_name="receipt.qualification_recheck.human_gate_required",
        ),
        ready=_require_bool(raw["ready"], field_name="receipt.qualification_recheck.ready"),
        qualification_ready=bool(gate.get("qualification_ready")),
        readiness_ready=bool(gate.get("readiness_ready")),
        qualification_pass_count=_require_int(
            gate.get("qualification_pass_count"),
            field_name="receipt.qualification_recheck.gate.qualification_pass_count",
        ),
        qualification_blocked_count=_require_int(
            gate.get("qualification_blocked_count"),
            field_name="receipt.qualification_recheck.gate.qualification_blocked_count",
        ),
        readiness_blocked_scope_count=_require_int(
            gate.get("readiness_blocked_scope_count"),
            field_name="receipt.qualification_recheck.gate.readiness_blocked_scope_count",
        ),
    )


def _receipt_ledger_item(raw: object, *, index: int) -> tuple[str, int, str]:
    """解析收据里的一条 ledger 最新决策摘要（缺字段 / 非法 → fail-closed）。"""
    if (
        not isinstance(raw, Mapping)
        or "fingerprint" not in raw
        or "revision" not in raw
        or "decision_id" not in raw
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.ledger_revision_digest"
            f"[{index}] 结构非法"
        )
    return (
        _require_hex64(
            raw["fingerprint"], field_name=f"receipt.ledger_revision_digest[{index}].fingerprint"
        ),
        _require_int(
            raw["revision"], field_name=f"receipt.ledger_revision_digest[{index}].revision"
        ),
        _require_hex64(
            raw["decision_id"], field_name=f"receipt.ledger_revision_digest[{index}].decision_id"
        ),
    )


#: GOLD-014 收据里必须出现的顶层字段（可核验子集）
_RECEIPT_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "kind",
    "schema_version",
    "report",
    "receipt_at",
    "receipt_id",
    "status",
    "inbox_dir",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "data_qualification_passed",
    "phase_transition_allowed",
    "intake_executed",
    "receipt_verified",
    "plan",
    "executed_entries",
    "ledger_revision_digest",
    "operator_results",
    "verification",
)


@dataclass(frozen=True, slots=True)
class _ReceiptParts:
    """GOLD-014 收据里用于**重新绑定 / 交叉核对**的字段（内部使用）。"""

    receipt_at: datetime
    receipt_id: str
    status: IntakeReceiptStatus
    plan_id: str
    plan_status: str
    plan_generated_at: datetime
    approved_list_generated_at: str | None
    inbox_dir: str
    plan_path: str | None
    ledger_path: str | None
    approved_list_path: str | None
    entries: tuple[ReceiptEntry, ...]
    ledger: tuple[tuple[str, int, str], ...]
    operators: tuple[OperatorResult, ...]
    recheck: QualificationRecheck | None
    inbox_packages: int
    preflight_pass: int
    ledger_fingerprints: int


def _validate_receipt_document(payload: Mapping[str, Any]) -> None:
    """校验 GOLD-014 收据的标识 / 安全字段 / 结构自洽（任一不合法 → fail-closed）。"""
    missing = [key for key in _RECEIPT_REQUIRED_KEYS if key not in payload]
    if missing:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt 缺字段："
            f"{'、'.join(sorted(missing))}"
        )
    if str(payload.get("kind") or "") != INTAKE_RECEIPT_KIND:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：文件不是 GOLD-014 "
            "evidence intake receipt"
        )
    if payload.get("schema_version") != INTAKE_RECEIPT_SCHEMA_VERSION:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt schema_version 不受支持："
            f"{payload.get('schema_version')!r}"
        )
    if str(payload.get("report") or "") != INTAKE_RECEIPT_REPORT_NAME:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt report 标识不匹配"
        )
    if str(payload.get("blocker_code") or "") != PHASE3_3_BLOCKER_CODE:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt blocker_code 必须是 "
            f"{PHASE3_3_BLOCKER_CODE}"
        )
    if payload.get("blocker_active") is not True or payload.get("human_gate_required") is not True:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt 声称 blocker 已解除或无需"
            "人工 Gate：安全字段不可被改写（fail-closed）"
        )
    if (
        payload.get("data_qualification_passed") is not False
        or payload.get("phase_transition_allowed") is not False
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt 声称资格已通过或已允许 "
            "Phase 切换：安全字段不可被改写（fail-closed）"
        )
    _reject_evidence_time_fields(payload, what="GOLD-014 收据")
    plan_block = payload["plan"]
    if not isinstance(plan_block, Mapping) or any(
        key not in plan_block for key in ("plan_id", "status", "generated_at")
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.plan 结构非法"
        )
    verification = payload["verification"]
    if not isinstance(verification, Mapping) or any(
        key not in verification
        for key in ("inbox_packages", "preflight_pass", "ledger_fingerprints")
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.verification 结构非法"
        )
    _require_hex64(plan_block["plan_id"], field_name="receipt.plan.plan_id")
    try:
        IntakeReceiptStatus(str(payload["status"]))
    except ValueError as exc:
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.status 非法："
            f"{_safe(payload['status'], max_chars=60)}"
        ) from exc
    _require_hex64(payload["receipt_id"], field_name="receipt.receipt_id")
    _parse_aware_moment(payload["receipt_at"], field_name="receipt.receipt_at")
    _parse_aware_moment(plan_block["generated_at"], field_name="receipt.plan.generated_at")


def _receipt_parts(payload: Mapping[str, Any]) -> _ReceiptParts:
    """把已通过结构校验的收据解析为可核验字段（内部使用；不再重复安全校验）。"""
    plan_block = payload["plan"]
    verification = payload["verification"]
    entries = tuple(
        _receipt_entry(item, index=index)
        for index, item in enumerate(
            _sequence_field(payload["executed_entries"], what="receipt.executed_entries"), start=1
        )
    )
    ledger = tuple(
        _receipt_ledger_item(item, index=index)
        for index, item in enumerate(
            _sequence_field(
                payload["ledger_revision_digest"], what="receipt.ledger_revision_digest"
            ),
            start=1,
        )
    )
    operators = tuple(
        _receipt_operator_result(item, index=index)
        for index, item in enumerate(
            _sequence_field(payload["operator_results"], what="receipt.operator_results"), start=1
        )
    )
    recheck_raw = payload.get("qualification_recheck")
    return _ReceiptParts(
        receipt_at=_parse_aware_moment(payload["receipt_at"], field_name="receipt.receipt_at"),
        receipt_id=_require_hex64(payload["receipt_id"], field_name="receipt.receipt_id"),
        status=IntakeReceiptStatus(str(payload["status"])),
        plan_id=_require_hex64(plan_block["plan_id"], field_name="receipt.plan.plan_id"),
        plan_status=_safe(plan_block["status"], max_chars=40),
        plan_generated_at=_parse_aware_moment(
            plan_block["generated_at"], field_name="receipt.plan.generated_at"
        ),
        approved_list_generated_at=_optional_path(plan_block.get("approved_list_generated_at")),
        inbox_dir=_safe_path(payload["inbox_dir"]),
        plan_path=_optional_path(plan_block.get("path")),
        ledger_path=_optional_path(plan_block.get("ledger_path")),
        approved_list_path=_optional_path(plan_block.get("approved_list_path")),
        entries=entries,
        ledger=ledger,
        operators=operators,
        recheck=None if recheck_raw is None else _receipt_recheck(recheck_raw),
        inbox_packages=_require_int(
            verification["inbox_packages"], field_name="receipt.verification.inbox_packages"
        ),
        preflight_pass=_require_int(
            verification["preflight_pass"], field_name="receipt.verification.preflight_pass"
        ),
        ledger_fingerprints=_require_int(
            verification["ledger_fingerprints"],
            field_name="receipt.verification.ledger_fingerprints",
        ),
    )


def load_intake_receipt_document(path: Path | str) -> ReceiptDocument:
    """读取 GOLD-014 收据并做**严格**结构 / 安全字段 / 内部自洽校验（损坏 → fail-closed）。

    note:
        本函数只做**结构**校验与内部自洽（``plan.plan_id``、``intake_executed`` /
        ``receipt_verified`` 必须与收据内容一致）；``receipt_id`` 的**内容寻址自洽**与**当前
        状态**的一致性在 :func:`verify_decision_packet` 里核对（稳定原因码
        ``RECEIPT_ID_MISMATCH`` / ``PLAN_ID_MISMATCH`` / ``FINGERPRINT_MISMATCH`` /
        ``REVIEW_REVISION_MISMATCH`` / ``RECHECK_MISMATCH``）。

    Raises:
        DecisionPacketStateError: 文件不存在 / 不可读 / 非 JSON 对象 / 文档标识 / schema /
            安全字段 / 结构 / 内部自洽 / 证据时间键 任一不合法。
    """
    target = Path(path)
    payload = _read_state_object(
        target,
        what="GOLD-014 收据",
        code=PacketVerificationCode.RECEIPT_TAMPERED.value,
    )
    _validate_receipt_document(payload)
    parts = _receipt_parts(payload)
    receipt = IntakeReceipt(
        receipt_at=parts.receipt_at,
        status=parts.status,
        plan_id=parts.plan_id,
        plan_status=parts.plan_status,
        plan_generated_at=parts.plan_generated_at,
        approved_list_generated_at=parts.approved_list_generated_at,
        inbox_dir=parts.inbox_dir,
        plan_path=parts.plan_path,
        ledger_path=parts.ledger_path,
        approved_list_path=parts.approved_list_path,
        entries=parts.entries,
        ledger_digest=parts.ledger,
        operator_results=parts.operators,
        recheck=parts.recheck,
        inbox_packages=parts.inbox_packages,
        preflight_pass=parts.preflight_pass,
        ledger_fingerprints=parts.ledger_fingerprints,
        receipt_id=parts.receipt_id,
        written_path=_optional_path(payload.get("written_path")),
    )
    if (
        _require_bool(payload["intake_executed"], field_name="receipt.intake_executed")
        != receipt.intake_executed
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.intake_executed 与收据内容"
            "不一致（fail-closed）"
        )
    if (
        _require_bool(payload["receipt_verified"], field_name="receipt.receipt_verified")
        != receipt.receipt_verified
    ):
        raise DecisionPacketStateError(
            f"{PacketVerificationCode.RECEIPT_TAMPERED.value}：receipt.receipt_verified 与收据内容"
            "不一致（fail-closed）"
        )
    return ReceiptDocument(
        path=_safe_path(target),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        receipt=receipt,
    )


# ---------------------------------------------------------------------------
# 聚合核验（fail-closed；**复用** GOLD-014 的重新绑定口径与稳定原因码）
# ---------------------------------------------------------------------------
def _receipt_violations(error: IntakeReceiptVerificationError) -> tuple[PacketViolation, ...]:
    """把 GOLD-014 的核验不一致映射为 packet 侧原因码（**同一含义不另造词**）。"""
    mapped = tuple(
        PacketViolation(code=str(item.code), fingerprint=item.fingerprint, detail=item.detail)
        for item in error.violations
    )
    if mapped:
        return mapped
    return (
        _violation(
            PacketVerificationCode.RECEIPT_MISMATCH,
            "",
            "证据链重新绑定未能完成（GOLD-014 未给出具体原因码）",
        ),
    )


def _verify_handoff(
    handoff: HandoffDocument,
    readiness: ReadinessStateDocument | None,
    *,
    moment: datetime,
) -> list[PacketViolation]:
    """handoff 时点与 readiness 快照**同源性**的核验（任一不一致 → fail-closed）。"""
    violations: list[PacketViolation] = []
    if handoff.as_of > moment:
        violations.append(
            _violation(
                PacketVerificationCode.FUTURE_TIMESTAMP,
                "",
                "handoff 审计时点晚于本次 packet 审计时点：拒绝未来时间",
            )
        )
    if readiness is None:
        return violations
    snapshot = readiness.snapshot
    if snapshot.generated_at > moment:
        violations.append(
            _violation(
                PacketVerificationCode.FUTURE_TIMESTAMP,
                "",
                "readiness state 生成时点晚于本次 packet 审计时点：拒绝未来时间",
            )
        )
    if snapshot.state() != handoff.source.state():
        violations.append(
            _violation(
                PacketVerificationCode.READINESS_MISMATCH,
                "",
                "readiness state 与由 handoff 重新推导的快照逐字段不一致："
                "handoff / readiness 不是同一批内容（fail-closed）",
            )
        )
    elif snapshot.fingerprint != handoff.source.fingerprint:
        violations.append(
            _violation(
                PacketVerificationCode.READINESS_MISMATCH,
                "",
                "readiness state 指纹与由 handoff 重新推导的指纹不一致（快照已 stale / 被改写）",
            )
        )
    return violations


def _verify_handoff_staleness(
    handoff: HandoffDocument, receipt: IntakeReceipt
) -> list[PacketViolation]:
    """handoff 必须**不早于**最近一次人工显式执行（否则 readiness 未覆盖新落库证据）。"""
    latest = max((item.generated_at for item in receipt.operator_results), default=None)
    if latest is None or handoff.as_of >= latest:
        return []
    return [
        _violation(
            PacketVerificationCode.HANDOFF_STALE,
            "",
            "handoff 审计时点早于最近一次人工显式落库：readiness / handoff 已 stale，"
            "必须在落库后重新生成 handoff（不得静默继承旧状态）",
        )
    ]


def _verify_recheck_agreement(
    recheck: QualificationRecheck, handoff: HandoffDocument
) -> list[PacketViolation]:
    """recheck 的 readiness / blocker 结论必须与 handoff 一致（否则复核没覆盖最新状态）。"""
    violations: list[PacketViolation] = []
    if recheck.readiness_ready != handoff.source.ready_for_human_review:
        violations.append(
            _violation(
                PacketVerificationCode.RECHECK_MISMATCH,
                "",
                "recheck 的 readiness 结论与 handoff 不一致：复核早于最新 readiness 变化"
                "（必须重新复核，fail-closed）",
            )
        )
    if (
        recheck.blocker_code != PHASE3_3_BLOCKER_CODE
        or not recheck.blocker_active
        or not recheck.human_gate_required
    ):
        violations.append(
            _violation(
                PacketVerificationCode.RECHECK_MISMATCH,
                "",
                "recheck 的 blocker 安全字段被改写（拒绝解除 blocker / 跳过人工 Gate）",
            )
        )
    return violations


def _operator_digests(results: Sequence[OperatorResult]) -> list[list[Any]]:
    """执行结果的内容级摘要集合（排序后比较：**顺序不参与判定**）。"""
    return sorted(
        [
            item.scope,
            item.input_sha256,
            item.dry_run,
            item.generated_at.isoformat(),
            item.persisted,
            item.artifact_sha256,
        ]
        for item in results
    )


def _receipt_differences(
    loaded: ReceiptDocument, rebuilt: IntakeReceipt
) -> list[PacketViolation]:
    """把显式给出的 GOLD-014 收据与**当前**重新绑定结果逐项比对（任一不一致 → fail-closed）。"""
    violations: list[PacketViolation] = []
    declared = loaded.receipt
    if declared.receipt_id != compute_receipt_id(declared):
        violations.append(
            _violation(
                PacketVerificationCode.RECEIPT_ID_MISMATCH,
                "",
                "收据的 receipt_id 与其内容不自洽（内容寻址失败：文件被改写，或不是本工具的"
                "收据口径）",
            )
        )
    if declared.plan_id != rebuilt.plan_id:
        violations.append(
            _violation(
                PacketVerificationCode.PLAN_ID_MISMATCH,
                "",
                "收据绑定的 plan_id 与**当前** plan 不一致（plan / 批准清单已变化）",
            )
        )
    if declared.status is not rebuilt.status:
        violations.append(
            _violation(
                PacketVerificationCode.RECEIPT_MISMATCH,
                "",
                f"收据状态（{declared.status.value}）与当前重新绑定结果"
                f"（{rebuilt.status.value}）不一致",
            )
        )
    loaded_entries = {item.fingerprint: item for item in declared.entries}
    rebuilt_entries = {item.fingerprint: item for item in rebuilt.entries}
    if set(loaded_entries) != set(rebuilt_entries):
        violations.append(
            _violation(
                PacketVerificationCode.FINGERPRINT_MISMATCH,
                "",
                "收据绑定的批准指纹集合与当前重新绑定结果不一致（批准被新增 / 移除）",
            )
        )
    for fingerprint in sorted(set(loaded_entries) & set(rebuilt_entries)):
        old = loaded_entries[fingerprint]
        new = rebuilt_entries[fingerprint]
        if old.content_sha256 != new.content_sha256:
            violations.append(
                _violation(
                    PacketVerificationCode.FINGERPRINT_MISMATCH,
                    fingerprint,
                    "证据文件内容摘要与收据不一致（内容已变化 → 新指纹，旧收据不得继承）",
                )
            )
        if (old.revision, old.decision_id) != (new.revision, new.decision_id):
            violations.append(
                _violation(
                    PacketVerificationCode.REVIEW_REVISION_MISMATCH,
                    fingerprint,
                    "review revision / decision_id 与收据不一致"
                    "（review override 后旧批准与旧收据均失效）",
                )
            )
        if (old.operator_scope, old.evidence_files) != (new.operator_scope, new.evidence_files):
            violations.append(
                _violation(
                    PacketVerificationCode.PLAN_ID_MISMATCH,
                    fingerprint,
                    "收据条目的 scope / 文件清单与当前 plan 不一致",
                )
            )
        if (old.rows, old.acceptable_rows, old.executed) != (
            new.rows,
            new.acceptable_rows,
            new.executed,
        ):
            violations.append(
                _violation(
                    PacketVerificationCode.RECEIPT_MISMATCH,
                    fingerprint,
                    "收据条目的行数 / 执行标记与当前重新绑定结果不一致",
                )
            )
    if tuple(declared.ledger_digest) != tuple(rebuilt.ledger_digest):
        violations.append(
            _violation(
                PacketVerificationCode.REVIEW_REVISION_MISMATCH,
                "",
                "ledger 最新决策摘要（指纹 / revision / decision_id）与收据不一致",
            )
        )
    if _operator_digests(declared.operator_results) != _operator_digests(rebuilt.operator_results):
        violations.append(
            _violation(
                PacketVerificationCode.RECEIPT_MISMATCH,
                "",
                "人工显式执行结果摘要与收据不一致",
            )
        )
    if (declared.recheck is None) != (rebuilt.recheck is None):
        violations.append(
            _violation(
                PacketVerificationCode.RECHECK_MISMATCH,
                "",
                "收据是否绑定 qualification recheck 与当前重新绑定结果不一致",
            )
        )
    elif (
        declared.recheck is not None
        and rebuilt.recheck is not None
        and declared.recheck.digest() != rebuilt.recheck.digest()
    ):
        violations.append(
            _violation(
                PacketVerificationCode.RECHECK_MISMATCH,
                "",
                "qualification recheck 摘要与收据不一致（复核结果已变化）",
            )
        )
    if (declared.intake_executed, declared.receipt_verified) != (
        rebuilt.intake_executed,
        rebuilt.receipt_verified,
    ):
        violations.append(
            _violation(
                PacketVerificationCode.RECEIPT_MISMATCH,
                "",
                "收据声明的 intake_executed / receipt_verified 与当前重新绑定结果不一致",
            )
        )
    return violations


def _evaluate(
    handoff: HandoffDocument,
    document: IntakePlanDocument,
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    readiness: ReadinessStateDocument | None = None,
    receipt: ReceiptDocument | None = None,
    operator_results: Sequence[OperatorResult] = (),
    recheck: QualificationRecheck | None = None,
) -> tuple[IntakeReceipt | None, tuple[PacketViolation, ...]]:
    """聚合核验的**唯一**事实来源（复用 GOLD-014 重新绑定 + handoff / readiness / 收据核对）。"""
    violations: list[PacketViolation] = list(_verify_handoff(handoff, readiness, moment=moment))
    rebuilt: IntakeReceipt | None = None
    try:
        rebuilt = build_intake_receipt(
            document,
            scan,
            ledger,
            approved_list,
            moment=moment,
            operator_results=operator_results,
            recheck=recheck,
        )
    except IntakeReceiptVerificationError as exc:
        violations.extend(_receipt_violations(exc))
    if rebuilt is not None:
        violations.extend(_verify_handoff_staleness(handoff, rebuilt))
        if receipt is not None:
            violations.extend(_receipt_differences(receipt, rebuilt))
    if recheck is not None:
        violations.extend(_verify_recheck_agreement(recheck, handoff))
    return rebuilt, tuple(violations)


def verify_decision_packet(
    handoff: HandoffDocument,
    document: IntakePlanDocument,
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    readiness: ReadinessStateDocument | None = None,
    receipt: ReceiptDocument | None = None,
    operator_results: Sequence[OperatorResult] = (),
    recheck: QualificationRecheck | None = None,
) -> tuple[PacketViolation, ...]:
    """**只读**核验：聚合是否齐备 / 自洽（空列表 = 可以生成 packet）。

    Returns:
        核验不一致列表（空 = 可以生成 packet）。任何一条都意味着调用方必须 **fail-closed**。
    """
    moment = _require_aware(moment, field_name="moment")
    return _evaluate(
        handoff,
        document,
        scan,
        ledger,
        approved_list,
        moment=moment,
        readiness=readiness,
        receipt=receipt,
        operator_results=operator_results,
        recheck=recheck,
    )[1]


# ---------------------------------------------------------------------------
# L3 人工决策包（确定性 / 脱敏 / 只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionPacket:
    """一次 L3 人工决策包生成的结果（**确定性脱敏**；默认零写入，``written_path`` 才落盘）。"""

    generated_at: datetime
    status: DecisionPacketStatus
    handoff: HandoffDocument
    plan_id: str
    plan_status: str
    receipt_id: str
    receipt_status: str
    readiness: ReadinessStateDocument | None = None
    receipt_path: str | None = None
    receipt_artifact_sha256: str | None = None
    receipt_file_matched: bool | None = None
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
    packet_id: str = ""
    written_path: str | None = None
    notes: tuple[str, ...] = (DECISION_PACKET_NOTE,)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return DECISION_PACKET_KIND

    @property
    def evidence_ready_for_human_review(self) -> bool:
        """量化门槛是否达标（**只**由 handoff + 可选 readiness 快照推导；仍须 L3 人工 Gate）。"""
        if self.readiness is not None and not self.readiness.snapshot.ready_for_human_review:
            return False
        return self.handoff.source.ready_for_human_review

    @property
    def receipt_verified(self) -> bool:
        """GOLD-014 的重新绑定核验是否通过（**绝不**蕴含资格通过或 Phase 切换）。"""
        return self.receipt_status == IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED.value

    @property
    def qualification_recheck_ready(self) -> bool:
        """qualification recheck 是否已完成**且**已绑定**且**自报 ``ready=true``（仍不是放行）。"""
        return self.recheck is not None and self.recheck.ready and self.receipt_verified

    @property
    def data_qualification_passed(self) -> bool:
        """恒为 ``False``：本工具**没有**资格判定能力（硬编码，不受任何输入影响）。"""
        return False

    @property
    def phase_transition_allowed(self) -> bool:
        """恒为 ``False``：Phase 切换只能由 **L3 人工 Gate** 决定。"""
        return False

    @property
    def submit_to_l3_human_gate(self) -> bool:
        """是否可以提交 **L3 人工 Gate**（三个独立事实**同时**成立；仍**不是**资格通过）。"""
        return (
            self.evidence_ready_for_human_review
            and self.receipt_verified
            and self.qualification_recheck_ready
        )

    @property
    def approved_count(self) -> int:
        """已绑定的批准条数（不是资格计数）。"""
        return len(self.entries)

    @property
    def landed_rows(self) -> int:
        """所有显式执行结果声明的落库行数合计（只是执行事实，不是资格）。"""
        return sum(result.persisted for result in self.operator_results)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；五个布尔与安全字段**显式且恒定**）。"""
        return {
            "kind": DECISION_PACKET_KIND,
            "report": DECISION_PACKET_REPORT_NAME,
            "schema_version": DECISION_PACKET_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "packet_id": self.packet_id,
            "status": self.status.value,
            "execution_mode": PACKET_EXECUTION_MODE,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "human_gate_level": "L3",
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "data_qualification_passed_count": 0,
            "auto_intake_allowed": False,
            "writes_database": False,
            "requires_explicit_operator_action": True,
            "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
            "evidence_ready_for_human_review": self.evidence_ready_for_human_review,
            "receipt_verified": self.receipt_verified,
            "qualification_recheck_ready": self.qualification_recheck_ready,
            "submit_to_l3_human_gate": self.submit_to_l3_human_gate,
            "handoff": self.handoff.to_dict(),
            "readiness_state": None if self.readiness is None else self.readiness.to_dict(),
            "readiness_binding": (
                "STATE_FILE_MATCHED" if self.readiness is not None else "NOT_PROVIDED"
            ),
            "plan": {
                "path": _safe_path(self.plan_path) if self.plan_path else None,
                "plan_id": self.plan_id,
                "status": _safe(self.plan_status, max_chars=40),
                "ledger_path": _safe_path(self.ledger_path) if self.ledger_path else None,
                "approved_list_path": (
                    _safe_path(self.approved_list_path) if self.approved_list_path else None
                ),
            },
            "receipt": {
                "path": _safe_path(self.receipt_path) if self.receipt_path else None,
                "artifact_sha256": self.receipt_artifact_sha256,
                "receipt_id": self.receipt_id,
                "status": self.receipt_status,
                "receipt_file_provided": self.receipt_path is not None,
                "receipt_file_matched": self.receipt_file_matched,
            },
            "approved_entries": [entry.to_dict() for entry in self.entries],
            "approved_entry_count": self.approved_count,
            "landed_rows": self.landed_rows,
            "operator_results": [item.to_dict() for item in self.operator_results],
            "qualification_recheck": None if self.recheck is None else self.recheck.to_dict(),
            "ledger_revision_digest": [
                {"fingerprint": fingerprint, "revision": revision, "decision_id": decision_id}
                for fingerprint, revision, decision_id in self.ledger_digest
            ],
            "gaps": {
                "author": _gap_view(self.handoff.report.author),
                "news": _gap_view(self.handoff.report.news),
                "total_remaining_gap_count": self.handoff.total_remaining_gap_count,
                "reason_codes": list(self.handoff.reason_codes),
                "unmet_check_keys": list(self.handoff.unmet_check_keys),
            },
            "verification": {
                "violations": [],
                "revalidated_at": self.generated_at.isoformat(),
                "inbox_packages": self.inbox_packages,
                "preflight_pass": self.preflight_pass,
                "ledger_fingerprints": self.ledger_fingerprints,
            },
            "evidence_time_semantics": {
                "contains_evidence_times": False,
                "audit_times_only": [
                    "generated_at",
                    "handoff.as_of",
                    "readiness_state.generated_at",
                    "operator_results[].generated_at",
                    "qualification_recheck.as_of",
                ],
                "note": PACKET_TIME_SEMANTICS_NOTE,
            },
            "written_path": self.written_path,
            "next_step": PACKET_NEXT_STEP_NOTE,
            "notes": list(self.notes),
        }


def _packet_status(
    *, submit_to_l3_human_gate: bool
) -> DecisionPacketStatus:
    """由三个独立事实的**合取**推导 packet 状态（只有同时成立才可提交 L3 人工 Gate）。"""
    if submit_to_l3_human_gate:
        return DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE
    return DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE


def compute_packet_id(packet: DecisionPacket) -> str:
    """内容级 ``packet_id``：只由（策略块 + handoff / readiness 摘要 + plan + 收据 + 布尔）派生。

    ⚠️ 刻意**不含** ``generated_at``：同一输入重复生成得到**同一** ``packet_id``；任一关键输入
    变化（handoff state / readiness 指纹 / plan / 批准 / 执行 / 复核 / 收据文件摘要）必然产生
    新 ``packet_id``（旧 packet **绝不静默继承**）。
    """
    payload = json.dumps(
        {
            "policy": _policy_block(),
            "status": packet.status.value,
            "handoff": {
                "state": packet.handoff.source.state(),
                "artifact_sha256": packet.handoff.artifact_sha256,
                "gaps": [
                    _gap_digest(packet.handoff.report.author),
                    _gap_digest(packet.handoff.report.news),
                ],
                "reason_codes": list(packet.handoff.reason_codes),
            },
            "readiness": (
                None
                if packet.readiness is None
                else {
                    "fingerprint": packet.readiness.snapshot.fingerprint,
                    "artifact_sha256": packet.readiness.artifact_sha256,
                }
            ),
            "plan_id": packet.plan_id,
            "plan_status": packet.plan_status,
            "receipt": {
                "receipt_id": packet.receipt_id,
                "status": packet.receipt_status,
                "artifact_sha256": packet.receipt_artifact_sha256,
                "file_provided": packet.receipt_path is not None,
                "file_matched": packet.receipt_file_matched,
                "entries": [
                    list(entry.digest())
                    for entry in sorted(packet.entries, key=lambda item: item.fingerprint)
                ],
                "ledger": [
                    [fingerprint, int(revision), decision_id]
                    for fingerprint, revision, decision_id in packet.ledger_digest
                ],
                "operator": _operator_digests(packet.operator_results),
                "recheck": (
                    None
                    if packet.recheck is None
                    else [
                        packet.recheck.report,
                        packet.recheck.as_of.isoformat(),
                        packet.recheck.ready,
                        packet.recheck.artifact_sha256,
                    ]
                ),
            },
            "flags": {
                "evidence_ready_for_human_review": packet.evidence_ready_for_human_review,
                "receipt_verified": packet.receipt_verified,
                "qualification_recheck_ready": packet.qualification_recheck_ready,
                "data_qualification_passed": False,
                "phase_transition_allowed": False,
                "submit_to_l3_human_gate": packet.submit_to_l3_human_gate,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashing.sha256_text(payload)


def build_decision_packet(
    handoff: HandoffDocument,
    document: IntakePlanDocument,
    scan: InboxPreflightReport,
    ledger: ReviewLedger,
    approved_list: ApprovedListDocument,
    *,
    moment: datetime,
    readiness: ReadinessStateDocument | None = None,
    receipt: ReceiptDocument | None = None,
    operator_results: Sequence[OperatorResult] = (),
    recheck: QualificationRecheck | None = None,
    plan_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    approved_list_path: Path | str | None = None,
) -> DecisionPacket:
    """核验 → 生成**确定性脱敏** packet（**零写入**；不一致 → fail-closed）。

    Raises:
        DecisionPacketArgumentError: ``moment`` 未带时区。
        DecisionPacketVerificationError: 任一聚合核验不通过（**零写入**，绝不把工具完成当资格）。
    """
    moment = _require_aware(moment, field_name="moment")
    rebuilt, violations = _evaluate(
        handoff,
        document,
        scan,
        ledger,
        approved_list,
        moment=moment,
        readiness=readiness,
        receipt=receipt,
        operator_results=operator_results,
        recheck=recheck,
    )
    if violations:
        raise DecisionPacketVerificationError(violations)
    if rebuilt is None:  # pragma: no cover - 绑定失败必然产生 violations
        raise DecisionPacketVerificationError(
            (
                _violation(
                    PacketVerificationCode.RECEIPT_MISMATCH,
                    "",
                    "证据链重新绑定未能完成（fail-closed）",
                ),
            )
        )
    provisional = DecisionPacket(
        generated_at=moment,
        status=DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE,
        handoff=handoff,
        plan_id=document.plan_id,
        plan_status=document.status,
        receipt_id=rebuilt.receipt_id,
        receipt_status=rebuilt.status.value,
        readiness=readiness,
        receipt_path=receipt.path if receipt is not None else None,
        receipt_artifact_sha256=receipt.artifact_sha256 if receipt is not None else None,
        receipt_file_matched=(
            None if receipt is None else receipt.receipt_id == rebuilt.receipt_id
        ),
        plan_path=str(plan_path) if plan_path is not None else None,
        ledger_path=str(ledger_path) if ledger_path is not None else None,
        approved_list_path=str(approved_list_path) if approved_list_path is not None else None,
        entries=tuple(rebuilt.entries),
        ledger_digest=tuple(rebuilt.ledger_digest),
        operator_results=tuple(rebuilt.operator_results),
        recheck=rebuilt.recheck,
        inbox_packages=len(scan.packages),
        preflight_pass=len(scan.preflight_pass),
        ledger_fingerprints=len(ledger.latest_decisions()),
    )
    status = _packet_status(submit_to_l3_human_gate=provisional.submit_to_l3_human_gate)
    packet = replace(provisional, status=status)
    return replace(packet, packet_id=compute_packet_id(packet))


# ---------------------------------------------------------------------------
# 只读运行入口（默认零写入；只有显式 out_path 才原子落盘 packet 本身）
# ---------------------------------------------------------------------------
def _compose_packet(
    root: Path,
    *,
    moment: datetime,
    handoff_path: Path,
    plan_path: Path,
    ledger_path: Path,
    approved_list_path: Path,
    readiness_path: Path | None,
    receipt_path: Path | None,
    operator_result_paths: Sequence[Path],
    recheck_path: Path | None,
) -> DecisionPacket:
    """读 handoff / readiness / plan / 收据（只读）→ ledger / inbox（只读）→ 核验生成 packet。"""
    handoff = load_handoff_document(handoff_path)
    readiness = (
        load_readiness_state_document(readiness_path) if readiness_path is not None else None
    )
    receipt = load_intake_receipt_document(receipt_path) if receipt_path is not None else None
    try:
        document = load_intake_plan_document(plan_path)
    except IntakeReceiptStateError as exc:
        raise DecisionPacketStateError(_safe(str(exc), max_chars=300)) from exc
    try:
        ledger: ReviewLedger | None = load_review_ledger(ledger_path)
    except ReviewLedgerStateError as exc:
        raise DecisionPacketStateError(_safe(str(exc), max_chars=300)) from exc
    if ledger is None:
        # 文件不存在 / 未给出 = 没有任何历史决策（不是"损坏"）；此时 plan 必须也没有批准条目，
        # 否则重新核验会判 fail-closed。
        ledger = ReviewLedger(generated_at=moment)
    try:
        approved_list = load_approved_intake_list(approved_list_path)
    except IntakePlanStateError as exc:
        raise DecisionPacketStateError(_safe(str(exc), max_chars=300)) from exc
    try:
        scan = scan_inbox(root, moment=moment)
    except InboxError as exc:
        raise DecisionPacketPathError(_safe(str(exc), max_chars=300)) from exc
    try:
        results = tuple(load_operator_result(path) for path in operator_result_paths)
        recheck = load_qualification_recheck(recheck_path) if recheck_path is not None else None
    except IntakeReceiptStateError as exc:
        raise DecisionPacketStateError(_safe(str(exc), max_chars=300)) from exc
    return build_decision_packet(
        handoff,
        document,
        scan,
        ledger,
        approved_list,
        moment=moment,
        readiness=readiness,
        receipt=receipt,
        operator_results=results,
        recheck=recheck,
        plan_path=plan_path,
        ledger_path=ledger_path,
        approved_list_path=approved_list_path,
    )


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise DecisionPacketWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def run_decision_packet(
    inbox_dir: Path | str,
    *,
    moment: datetime,
    handoff_path: Path | str | None = None,
    plan_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    approved_list_path: Path | str | None = None,
    readiness_path: Path | str | None = None,
    receipt_path: Path | str | None = None,
    operator_result_paths: Sequence[Path | str] = (),
    recheck_path: Path | str | None = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> DecisionPacket:
    """执行**一次**决策包聚合（默认只读；只有显式 ``out_path`` 才写 packet 文件）。

    安全语义：

    - handoff / readiness / plan / 收据 / ledger / 批准清单 / 执行结果 / 复核结果全部只读；
      损坏 / 被篡改 → :class:`DecisionPacketStateError`（零写入，旧文件原样保留）；
    - 只有 ``out_path`` 才写**packet 本身**，且先取**单实例锁**再重新核验（避免并发竞态）；
      锁冲突 → :class:`~src.evidence.readiness_runner.LockConflictError` 且**零写入**；
    - 任何聚合核验不一致 → :class:`DecisionPacketVerificationError` 且**零写入**；
    - 本函数**没有**任何 intake / commit 调用，**绝不**写数据库，**绝不**移动 / 删除 / 改写
      inbox 内原始 evidence，**绝不**联网，**绝不**解除 blocker / 切换 Phase。

    Raises:
        DecisionPacketArgumentError: ``moment`` 未带时区，或缺 ``handoff_path`` /
            ``plan_path`` / ``ledger_path`` / ``approved_list_path``。
        DecisionPacketPathError: inbox 目录不可用，或输入 / 输出 artifact 与 inbox 目录冲突。
        DecisionPacketStateError: 任一输入文档损坏 / 被篡改 / stale。
        DecisionPacketVerificationError: 聚合核验不通过（fail-closed）。
        LockConflictError: 另一个 packet / 计划 / 复核 / 重扫正持有活动锁（零写入）。
        DecisionPacketWriteError: packet 原子写失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(inbox_dir)
    if not root.is_dir():
        raise DecisionPacketPathError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    if handoff_path is None:
        raise DecisionPacketArgumentError(
            "必须显式给出 readiness / handoff 报告（--handoff）：本工具不猜、不自行生成交接包"
        )
    if plan_path is None:
        raise DecisionPacketArgumentError(
            "必须显式给出 GOLD-013 intake plan（--plan）：本工具不猜、不自动生成计划"
        )
    if ledger_path is None:
        raise DecisionPacketArgumentError(
            "必须显式给出 GOLD-012 review ledger（--ledger）：批准来源必须可核验"
        )
    if approved_list_path is None:
        raise DecisionPacketArgumentError(
            "必须显式给出 GOLD-012 批准清单（--approved-list）：批准范围必须可核验"
        )
    handoff_target = Path(handoff_path)
    plan_target = Path(plan_path)
    ledger_target = Path(ledger_path)
    approved_target = Path(approved_list_path)
    readiness_target = Path(readiness_path) if readiness_path is not None else None
    receipt_target = Path(receipt_path) if receipt_path is not None else None
    recheck_target = Path(recheck_path) if recheck_path is not None else None
    result_targets = tuple(Path(item) for item in operator_result_paths)
    out = Path(out_path) if out_path is not None else None
    for target in (
        handoff_target,
        plan_target,
        ledger_target,
        approved_target,
        readiness_target,
        receipt_target,
        recheck_target,
        *result_targets,
        out,
    ):
        if target is None:
            continue
        try:
            ensure_outside_inbox(root, target)
        except InboxError as exc:
            raise DecisionPacketPathError(_safe(str(exc), max_chars=300)) from exc

    def _compose() -> DecisionPacket:
        return _compose_packet(
            root,
            moment=moment,
            handoff_path=handoff_target,
            plan_path=plan_target,
            ledger_path=ledger_target,
            approved_list_path=approved_target,
            readiness_path=readiness_target,
            receipt_path=receipt_target,
            operator_result_paths=result_targets,
            recheck_path=recheck_target,
        )

    if out is None:
        return _compose()
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + DECISION_PACKET_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        packet = _compose()
        _atomic_write_json(out, packet.to_dict(), what="decision packet")
    return replace(packet, written_path=_safe_path(out))


def render_decision_packet_summary(packet: DecisionPacket) -> str:
    """渲染人类可读的决策包摘要（脱敏；**不解除** blocker、**不自动** intake / 切 Phase）。"""
    payload = packet.to_dict()
    readiness_path = (
        _safe_path(packet.readiness.path) if packet.readiness is not None else "—"
    )
    lines: list[str] = [
        "# Evidence Qualification 人工决策包（L3 人工 Gate 前的只读汇总）",
        "",
        f"> {DECISION_PACKET_NOTE}",
        "",
        "## 1. 运行概览",
        "",
        f"- packet_id（内容级）：`{packet.packet_id}`；状态：`{packet.status.value}`；"
        f"执行模式：`{PACKET_EXECUTION_MODE}`",
        f"- handoff：`{_safe_path(packet.handoff.path)}`"
        f"（审计时点 {packet.handoff.as_of.isoformat()}）",
        f"- readiness state：`{readiness_path}`（绑定：{payload['readiness_binding']}）",
        f"- plan：`{_safe_path(packet.plan_path) if packet.plan_path else '—'}`；"
        f"plan_id `{packet.plan_id}`（状态 {packet.plan_status}）",
        f"- 收据：`{_safe_path(packet.receipt_path) if packet.receipt_path else '—'}`；"
        f"receipt_id `{packet.receipt_id}`（状态 {packet.receipt_status}）",
        f"- blocker：`{PHASE3_3_BLOCKER_CODE}`（`blocker_active` = true；"
        "`human_gate_required` = true；Phase 切换仍须 **L3 人工 Gate**）",
        f"- 审计时点（UTC）：{packet.generated_at.isoformat()}",
        f"- 当前扫描候选包：{payload['verification']['inbox_packages']}；"
        f"`PREFLIGHT_PASS`：{payload['verification']['preflight_pass']}；"
        f"ledger 已复核指纹：{payload['verification']['ledger_fingerprints']}",
        "- 核验结论：**0 项不一致**（任一不一致都会 fail-closed 且不产出 packet）",
    ]
    if packet.written_path is not None:
        lines.append(f"- packet 已原子写入：`{packet.written_path}`")
    lines += [
        "",
        "## 2. 五个布尔（**互不蕴含**）",
        "",
        f"- `evidence_ready_for_human_review` = "
        f"{str(packet.evidence_ready_for_human_review).lower()}（只由 readiness / handoff 推导）",
        f"- `receipt_verified` = {str(packet.receipt_verified).lower()}"
        f"（GOLD-014 重新绑定核验；已绑定批准 {packet.approved_count} 条 / 落库 "
        f"{packet.landed_rows} 行）",
        f"- `qualification_recheck_ready` = {str(packet.qualification_recheck_ready).lower()}"
        "（recheck 存在 + 绑定成功 + 自报 ready）",
        "- `data_qualification_passed` = false（**恒为 false**：本工具不是资格判定器）",
        "- `phase_transition_allowed` = false（**恒为 false**：Phase 切换仍是 **L3 人工 Gate**）",
        f"- `submit_to_l3_human_gate` = {str(packet.submit_to_l3_human_gate).lower()}"
        "（= 前三个事实**同时**成立；仍**不是**资格通过）",
        "",
        "## 3. 仍未达标的缺口（真实证据不足时必须保持 BLOCKED）",
        "",
    ]
    for scope_name in ("author", "news"):
        gap = payload["gaps"][scope_name]
        lines.append(
            f"- {scope_name}：status `{gap['status']}`；eligible {gap['eligible']} / "
            f"required {gap['required']} / remaining {gap['remaining']}；"
            f"仍未 PASS 的检查 {gap['remaining_checks']}"
        )
    lines.append(f"- 仍未达标原因码：{payload['gaps']['reason_codes'] or '—'}")
    lines += ["", "## 4. 已绑定的批准与执行（**仍不等于资格**）", ""]
    if not packet.entries:
        lines.append(
            "- —（当前没有任何仍成立的批准；`PHASE3_3_DATA` 保持 **BLOCKED**，"
            "请先按 GOLD-011/012/013/014 摆放候选、人工复核、生成计划并**显式**落库后复核）"
        )
    for entry in packet.entries:
        lines.append(
            f"- `{entry.package_dir}`（指纹前 16 位 `{entry.fingerprint[:16]}`；"
            f"rev {entry.revision}；scope `{entry.operator_scope}`）："
            f"文件 {list(entry.evidence_files)}；行数 {entry.rows}"
            f"（可接收 {entry.acceptable_rows}）；已显式执行：{str(entry.executed).lower()}"
        )
    lines += ["", "## 5. qualification recheck（只读引用；**不是**放行结论）", ""]
    if packet.recheck is None:
        lines.append("- —（没有任何 recheck 结果；`qualification_recheck_ready` = false）")
    else:
        recheck = packet.recheck
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
        f"- {PACKET_TIME_SEMANTICS_NOTE}",
        f"- {PACKET_NEXT_STEP_NOTE}",
        "",
        "## 7. 说明",
        "",
        *[f"- {note}" for note in packet.notes],
        "",
    ]
    return "\n".join(lines)
