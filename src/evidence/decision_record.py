"""Evidence Qualification **L3 人工决策记录**（**GOLD-016**，纯本地 / 显式人工输入 / fail-closed）。

GOLD-015 已把 readiness / handoff、批准清单与 GOLD-013 plan、GOLD-014 verified receipt 与
qualification recheck 聚合成**确定性、脱敏、内容寻址**（``packet_id``）的 L3 人工决策包，
并严格区分 ``evidence_ready_for_human_review`` / ``receipt_verified`` /
``qualification_recheck_ready`` 三个独立事实与恒为 false 的
``data_qualification_passed`` / ``phase_transition_allowed``。

但"**人到底作出过什么决策**"这件事在仓库里**没有任何可核验记录**：packet 只说明
"可以提交人工 Gate"，无法证明某个具体 packet 收到过明确的人工 ``approve`` / ``reject`` /
``needs_changes``；口头 / 聊天记录既不可审计也无法防伪。

本模块补上**审计闭环的最后一段**：把**人工显式**给出的决策与**具体的** GOLD-015 packet
（``packet_id`` + 内容摘要 + 产物摘要）绑成**确定性、脱敏、内容寻址**（``record_id``）的
决策记录。

- **纯本地 / 显式人工输入 / 零网络 / 零数据库**：只**读**显式给出的 GOLD-015 packet 文件
  （可选再读一份既有决策记录做一致性核验）；``decision`` 与 ``reviewer`` **必须**由人工
  **显式**提供，工具**绝不**自行生成批准、**绝不**推断人工意图；默认**零写入**，只有显式
  ``--out`` 才**原子**落盘记录本身；**绝不**修改 ``PROJECT_STATE``、**绝不**解除
  ``PHASE3_3_DATA``、**绝不**切换 Phase；**没有**任何 intake / 数据库 / 网络调用；
- **复用而非复制 GOLD-015 契约**：``packet_id`` / 原因码 / 脱敏 / 原子写 / 单实例锁全部
  **复用**（``PacketVerificationCode`` 中同义的稳定原因码直接沿用，例如
  ``EVIDENCE_TIME_SUBSTITUTION`` / ``FUTURE_TIMESTAMP`` / ``READINESS_MISMATCH``）；
  packet 的资格规则**不被复制、不被降低**——本模块只**重新校验 packet 文档自身是否自洽**
  （``kind`` / 报告名 / schema / 契约版本 / 安全字段未被削弱 / 缺口与检查的**内部算术** /
  三个布尔与 ``submit_to_l3_human_gate`` 的**合取关系** / 收据状态与 ``receipt_verified`` /
  readiness 快照**指纹同源性** / ``thresholds`` 必须等于**当前**唯一阈值来源 /
  ``unmet_check_keys`` 可重算 / 计数与摘要**长度自洽** / **禁止任何证据时间键** /
  **不得出现未来时间**）；
- **approve 只能落在可提交的 packet 上**：只有 packet **本身** ``submit_to_l3_human_gate``
  为 true **且**上述完整性核验通过时才允许记录 ``approve``；packet ``BLOCKED`` →
  ``PACKET_NOT_SUBMITTABLE``；缺失 / 损坏 / 篡改 / 内容或 ``packet_id`` 不一致 / 未来时间 →
  逐项**稳定原因码**且**零写入**（fail-closed）。``reject`` / ``needs_changes`` **可以**被记录
  （即使 packet 仍 BLOCKED），但**绝不**改变任何资格状态；
- **记录语义显式且互不蕴含**：``human_decision_recorded``（记录存在）/ ``human_decision``
  （人工决策原文）/ ``packet_verified``（packet 完整性核验通过）三个事实独立；
  ``data_qualification_passed`` / ``phase_transition_allowed`` / ``phase_transition_executed``
  **恒为** ``false``（**硬编码**），``blocker_active`` / ``human_gate_required`` 恒为 true、
  ``human_gate_level`` 恒为 ``L3``；本工具**不具备**把后两项置为 true 的能力；
- **人工身份只收显式、非敏感的 operator / reviewer label**：不采集任何凭据 / 口令 / token，
  拒绝空值、超长、非法字符与**看起来像凭据**的取值；``reason_code`` / ``note`` 一律走
  ``src.common.redaction`` **脱敏**并**限制长度**（超长 → fail-closed，不静默截断成假事实）；
- **绝不把操作时间当证据时间**：``decision_at`` / ``generated_at`` / packet 的
  ``generated_at`` / 文件 mtime **都只是审计操作时间**，记录里的
  ``evidence_time_semantics.contains_evidence_times`` **恒为** false，且加载 packet 与既有记录
  时都会**递归拒绝**任何证据时间键（``published_at`` / ``collected_at`` / ``effective_at`` /
  ``available_at`` / ``availability_*``）；
- **幂等 / 内容寻址 / 不静默改写历史**：``record_id`` 只由（策略块 + ``decision`` +
  ``reviewer`` + 受约束的 ``note`` / ``reason_code`` + ``revision`` / ``supersedes`` +
  packet 绑定摘要）派生，**不含**任何审计时间；同一 packet + 同一人工决策 + 同一审计时点
  重复生成得到**逐字节稳定**的文档与**同一** ``record_id``；``packet_id`` / ``decision`` /
  ``reviewer`` / ``note`` / ``revision`` / ``supersedes`` 或 packet 内容任一变化必然产生
  **新** ``record_id``；写下的记录**绝不**被静默覆盖——冲突必须**显式** ``revision`` +
  ``supersedes`` 且必须指向**当前**记录，否则 fail-closed（零写入）；
- **防伪核验**：``verify_decision_record(...)`` 可从记录文档**重新推导** ``record_id``、
  把记录绑定与**当前** packet 逐项比对（``PACKET_ID_MISMATCH`` /
  ``PACKET_CONTENT_MISMATCH`` / ``PACKET_STALE`` / ``RECORD_ID_MISMATCH`` /
  ``RECORD_TAMPERED``），并可判定"当初的 ``approve`` 是否**仍然**成立"。

入口：``scripts/evidence_decision_record.py``（``--packet`` + ``--decision`` + ``--reviewer``
必填；``--note`` / ``--reason-code`` / ``--revision`` / ``--supersedes`` 可选；``--out`` 是
**唯一**写开关；``--verify-record`` 为纯只读防伪核验模式；**没有**任何 intake / 写库参数）。
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
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.decision_packet import (
    DECISION_PACKET_KIND,
    DECISION_PACKET_REPORT_NAME,
    DECISION_PACKET_SCHEMA_VERSION,
    PACKET_EXECUTION_MODE,
    DecisionPacketStatus,
    PacketVerificationCode,
)
from src.evidence.handoff import (
    BLOCKED_STATUS,
    HANDOFF_REPORT_NAME,
    HANDOFF_SCHEMA_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
)
from src.evidence.handoff import thresholds as handoff_thresholds
from src.evidence.intake_plan import OPERATOR_EXPLICIT_FLAG, IntakePlanStatus
from src.evidence.intake_receipt import (
    OPERATOR_MANIFEST_REPORT,
    QUALIFICATION_RECHECK_REPORT,
    IntakeReceiptStatus,
)
from src.evidence.readiness_watch import (
    MAX_CODE_CHARS,
    SNAPSHOT_KIND,
    SNAPSHOT_SCHEMA_VERSION,
    atomic_write_text,
)
from src.evidence.review import FORBIDDEN_EVIDENCE_FIELDS
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE, CheckStatus

__all__ = [
    "DECISION_RECORD_ACTIONS",
    "DECISION_RECORD_EXECUTION_MODE",
    "DECISION_RECORD_FILE_NAME",
    "DECISION_RECORD_KIND",
    "DECISION_RECORD_LOCK_SUFFIX",
    "DECISION_RECORD_NOTE",
    "DECISION_RECORD_REPORT_NAME",
    "DECISION_RECORD_SCHEMA_VERSION",
    "DECISION_RECORD_TIME_SEMANTICS_NOTE",
    "DECISION_SCOPE",
    "MAX_RECORD_NOTE_CHARS",
    "MAX_RECORD_NOTE_INPUT_CHARS",
    "MAX_RECORD_REASON_CODE_CHARS",
    "MAX_RECORD_REVIEWER_CHARS",
    "MAX_RECORD_REVISION",
    "PACKET_CHECKS",
    "REVIEWER_KIND",
    "DecisionRecordArgumentError",
    "DecisionRecordCode",
    "DecisionRecordError",
    "DecisionRecordNotSubmittableError",
    "DecisionRecordPathError",
    "DecisionRecordStateError",
    "DecisionRecordVerification",
    "DecisionRecordVerificationError",
    "DecisionRecordWriteError",
    "HumanDecision",
    "HumanDecisionRecord",
    "PacketBinding",
    "build_decision_record",
    "compute_record_id",
    "decision_record_exit_code_for",
    "load_decision_packet",
    "main_verification_exit_code",
    "render_decision_record_summary",
    "run_decision_record",
    "verify_decision_record",
]


#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
DECISION_RECORD_SCHEMA_VERSION: Final[int] = 1
#: 记录文档标识 / 报告标识（稳定，供审计脚本解析）
DECISION_RECORD_KIND: Final[str] = "evidence_qualification_l3_human_decision_record"
DECISION_RECORD_REPORT_NAME: Final[str] = DECISION_RECORD_KIND
#: 记录文件名建议（只有显式 ``--out`` 才写盘）
DECISION_RECORD_FILE_NAME: Final[str] = "evidence_l3_human_decision_record.json"
#: 单实例锁后缀（与 GOLD-010 / 015 同一约定）
DECISION_RECORD_LOCK_SUFFIX: Final[str] = ".lock"
#: 执行模式（**只读 + 显式人工输入**；硬编码）
DECISION_RECORD_EXECUTION_MODE: Final[str] = "LOCAL_L3_HUMAN_DECISION_RECORD"
#: 决策作用域（防止把"人工 L3 决策记录"读成"数据资格通过"）
DECISION_SCOPE: Final[str] = "l3_human_gate_decision_only_not_data_qualification"
#: 人工身份口径（**只**接受显式 label；不采集凭据）
REVIEWER_KIND: Final[str] = "explicit_operator_label_no_credentials"
#: 允许的人工决策（**只有**这三种；工具绝不自行生成 approve）
DECISION_RECORD_ACTIONS: Final[tuple[str, ...]] = ("approve", "reject", "needs_changes")
#: reviewer label 长度上限
MAX_RECORD_REVIEWER_CHARS: Final[int] = 64
#: note 长度上限（脱敏后写入记录的长度）
MAX_RECORD_NOTE_CHARS: Final[int] = 400
#: note 输入长度上限（超出 → fail-closed，绝不静默截断成假事实）
MAX_RECORD_NOTE_INPUT_CHARS: Final[int] = 4000
#: reason_code 长度上限
MAX_RECORD_REASON_CODE_CHARS: Final[int] = 64
#: revision 上限（防止异常输入撑爆记录）
MAX_RECORD_REVISION: Final[int] = 1_000_000
#: record_id 的十六进制形态（与 packet_id / plan_id / receipt_id 同一口径）
_HEX_DIGITS: Final[str] = "0123456789abcdef"

#: packet 完整性核验通过后写入记录的不变量清单（**全部**为硬性检查，任一失败即 fail-closed）
PACKET_CHECKS: Final[tuple[str, ...]] = (
    "DOCUMENT_IDENTITY",  # kind / report / schema_version / contract_version / execution_mode
    "SAFETY_FIELDS_HARDCODED",  # blocker_active / human_gate_required / data_qualification_passed …
    "PACKET_ID_FORMAT",  # packet_id 必须是内容寻址的 64 位小写十六进制
    "HANDOFF_STRUCTURE",  # handoff 报告标识 / schema / 安全字段 / thresholds / 状态一致
    "GAP_ARITHMETIC",  # 缺口与检查的内部算术自洽（remaining / checks / coverage）
    "UNMET_CHECK_RECOMPUTABLE",  # unmet_check_keys 可由各 scope 检查状态重新推导
    "FLAG_CONJUNCTION",  # submit_to_l3_human_gate == 三个独立事实的合取
    "READINESS_SAME_SOURCE",  # readiness 快照与 handoff 同源（指纹 / ready / 缺口 / 原因码）
    "RECEIPT_BINDING",  # 收据状态与 receipt_verified 一致；文件摘要往返一致
    "RECHECK_BINDING",  # recheck 存在 / ready / receipt_verified 的合取一致
    "APPROVED_LEDGER_CONSISTENCY",  # 批准条数 / ledger 指纹数与摘要长度自洽
    "OPERATOR_RESULT_CONSISTENCY",  # 执行结果 report / scope / dry_run / 落库计数自洽
    "VERIFICATION_BLOCK_CLEAN",  # violations 为空且 revalidated_at 与 generated_at 一致
    "EVIDENCE_TIME_FREE",  # 递归禁止任何证据时间键
    "NO_FUTURE_TIMESTAMPS",  # 审计时点不得晚于本次人工决策时点
)

#: reviewer label：只允许可审计、非敏感的显式标签（不含空格 / 引号 / 路径分隔符）
_REVIEWER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")
#: reason_code：稳定大写原因码形态（与 GOLD-012/013/014 同一口径；不做静默转换）
_REASON_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: "看起来像凭据"的取值（reviewer / reason / note 一律拒绝或擦除）
_SECRET_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(passwo?r?d|pwd|secret|token|api[_-]?key|apikey|bearer|credential|"
    r"authorization|private[_-]?key|session[_-]?id)"
)
#: 长十六进制 / base64 形态的疑似密钥串（reviewer 一律拒绝）
_BLOB_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")
#: GOLD-015 packet 里单个 scope 缺口**视图**的键（与 ``_gap_view`` 的输出形态一致）
_GAP_VIEW_KEYS: Final[tuple[str, ...]] = (
    "scope",
    "status",
    "ready",
    "eligible",
    "required",
    "remaining",
    "certified",
    "not_oos_eligible",
    "remaining_checks",
    "coverage_applicable",
    "coverage_days",
    "coverage_required",
    "coverage_remaining",
    "source_share_applicable",
    "source_share_limit",
    "source_share_evaluable",
    "source_share_remaining",
    "checks",
)
#: GOLD-015 packet 里单条就绪度**视图**的键（``_check_view`` 的输出形态）
_CHECK_VIEW_KEYS: Final[tuple[str, ...]] = (
    "key",
    "scope",
    "metric",
    "current",
    "required",
    "comparator",
    "status",
    "evaluable",
    "remaining",
)
#: record_id 绑定用的 packet 字段（**不含**任何审计时间以外的自由文本）
_PACKET_BINDING_KEYS: Final[tuple[str, ...]] = (
    "packet_id",
    "content_sha256",
    "artifact_sha256",
    "generated_at",
    "status",
    "submit_to_l3_human_gate",
    "evidence_ready_for_human_review",
    "receipt_verified",
    "qualification_recheck_ready",
    "blocker_code",
    "handoff_artifact_sha256",
    "plan_id",
    "receipt_id",
    "readiness_binding",
)

#: 固定说明：本工具是**人工决策审计层**，不是 Phase transition executor
DECISION_RECORD_NOTE: Final[str] = (
    "本工具只记录**人工显式**给出的 L3 Gate 决策（``approve`` / ``reject`` / "
    "``needs_changes``）并把它绑定到**具体**的 GOLD-015 packet（``packet_id`` + 内容摘要 + "
    "产物摘要）。它**不生成**批准、**不判断**数据资格、**不写**数据库、**不调用**任何 "
    "intake / commit、**不修改** `PROJECT_STATE`、**不解除** `PHASE3_3_DATA`、**绝不**切换 "
    "Phase：``data_qualification_passed`` / ``phase_transition_allowed`` / "
    "``phase_transition_executed`` 恒为 false。**记录一份人工决策 ≠ 资格通过**。"
)
#: 固定说明：记录里的时间**只是审计操作时间**，绝不当证据时间
DECISION_RECORD_TIME_SEMANTICS_NOTE: Final[str] = (
    "记录只保存审计操作时间（decision_at / generated_at / packet.generated_at / "
    "packet_verification.verified_at）；它们**不是**证据时间，绝不用于冒充 published_at、"
    "collected_at、effective_at 或 availability / OOS 证据，也不会由它们推导任何证据时间；"
    "文件 mtime 同样不被采信。"
)
#: 固定说明：下一步仍是人工 / 外部动作
DECISION_RECORD_NEXT_STEP_NOTE: Final[str] = (
    "本记录只证明「某个具体 packet_id 收到过**明确**的人工决策」；它**不是**资格通过，也"
    "**不是** Phase 切换授权。真实数据资格仍需独立复核（授权 / 独立历史可用性 / OOS 证据），"
    "Phase 切换仍需 `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**并显式更新 "
    "`PROJECT_STATE`（由人工 / Orchestrator 完成，**不由本工具完成**）。"
)


class HumanDecision(StrEnum):
    """允许的人工决策（**必须**由人工显式给出；工具绝不自行生成）。"""

    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_CHANGES = "needs_changes"


class DecisionRecordCode(StrEnum):
    """决策记录的稳定原因码（任何一条都意味着 **fail-closed**，绝不产出 / 覆盖记录）。

    note:
        与 GOLD-015 / GOLD-014 同义的原因码**直接沿用**其字符串（``READINESS_MISMATCH`` /
        ``EVIDENCE_TIME_SUBSTITUTION`` / ``FUTURE_TIMESTAMP``），不另造第二套词表。
    """

    # ---- packet 文档（GOLD-015 产物）--------------------------------------
    PACKET_NOT_FOUND = "PACKET_NOT_FOUND"
    PACKET_UNREADABLE = "PACKET_UNREADABLE"
    PACKET_TAMPERED = "PACKET_TAMPERED"
    PACKET_ID_INVALID = "PACKET_ID_INVALID"
    PACKET_NOT_SUBMITTABLE = "PACKET_NOT_SUBMITTABLE"
    PACKET_OVERWRITE_REFUSED = "PACKET_OVERWRITE_REFUSED"
    # ---- 记录文档（本工具产物）------------------------------------------
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    RECORD_UNREADABLE = "RECORD_UNREADABLE"
    RECORD_TAMPERED = "RECORD_TAMPERED"
    RECORD_ID_MISMATCH = "RECORD_ID_MISMATCH"
    RECORD_CONFLICT = "RECORD_CONFLICT"
    # ---- 防伪核验：记录绑定 vs **当前** packet ---------------------------
    PACKET_ID_MISMATCH = "PACKET_ID_MISMATCH"
    PACKET_CONTENT_MISMATCH = "PACKET_CONTENT_MISMATCH"
    PACKET_STALE = "PACKET_STALE"
    # ---- 人工输入 --------------------------------------------------------
    DECISION_INVALID = "DECISION_INVALID"
    REVIEWER_INVALID = "REVIEWER_INVALID"
    NOTE_TOO_LONG = "NOTE_TOO_LONG"
    REASON_CODE_INVALID = "REASON_CODE_INVALID"
    SUPERSEDES_MISMATCH = "SUPERSEDES_MISMATCH"
    REVISION_INVALID = "REVISION_INVALID"
    # ---- 与 GOLD-015 同义的稳定原因码（**沿用**，不另造词）----------------
    READINESS_MISMATCH = PacketVerificationCode.READINESS_MISMATCH.value
    EVIDENCE_TIME_SUBSTITUTION = PacketVerificationCode.EVIDENCE_TIME_SUBSTITUTION.value
    FUTURE_TIMESTAMP = PacketVerificationCode.FUTURE_TIMESTAMP.value


class DecisionRecordError(RuntimeError):
    """决策记录层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class DecisionRecordArgumentError(DecisionRecordError):
    """参数错误（缺 ``--packet`` / ``--decision`` / ``--reviewer``，时点缺时区等）。"""


class DecisionRecordStateError(DecisionRecordError):
    """packet / 记录文档缺失、损坏、被篡改、stale 或与内核验不一致（**零写入**）。"""


class DecisionRecordPathError(DecisionRecordError):
    """输入 / 输出路径不可用（含把记录写到 packet 文件上的拒绝）。"""


class DecisionRecordVerificationError(DecisionRecordError):
    """防伪核验不通过（记录与当前 packet 不一致 / 记录自身被改写；**零写入**）。

    Attributes:
        codes: 稳定原因码（去重排序），便于脚本与人工稳定解析。
        details: 已脱敏的逐项说明。
    """

    def __init__(self, codes: Sequence[str], details: Sequence[str] = ()) -> None:
        self.codes: tuple[str, ...] = tuple(sorted({str(code) for code in codes}))
        self.details: tuple[str, ...] = tuple(str(item) for item in details)
        summary = "、".join(self.codes[:8]) if self.codes else "UNKNOWN"
        super().__init__(
            "决策记录防伪核验不通过（fail-closed；零写入、绝不把操作时间当证据、"
            f"绝不改变资格状态）：{len(self.codes)} 类不一致；原因码：{summary}"
        )


class DecisionRecordNotSubmittableError(DecisionRecordStateError):
    """``approve`` 被拒绝：packet 本身不满足 ``submit_to_l3_human_gate``（**预期 BLOCKED**）。"""


class DecisionRecordWriteError(DecisionRecordError):
    """记录原子写失败（**fail-closed**）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 记录已生成（``--out`` 时已原子落盘）；仍是**人工决策审计**，不是资格通过
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数错误（缺 packet / decision / reviewer，时点缺时区，互斥参数冲突等）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: 路径 / 输出不可用（含把记录写到 packet 文件上的拒绝、原子写失败）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: packet / 记录缺失、损坏、篡改、stale、冲突或防伪核验不通过（全部 fail-closed）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: ``approve`` 被拒绝：packet 不可提交（证据未达标 / 复核未完成；**预期 BLOCKED**）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一处记录写入正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

_RECORD_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (DecisionRecordNotSubmittableError, EXIT_BLOCKED),
    (DecisionRecordArgumentError, EXIT_CONFIG_ERROR),
    (DecisionRecordPathError, EXIT_UNUSABLE),
    (DecisionRecordWriteError, EXIT_UNUSABLE),
    (DecisionRecordStateError, EXIT_STATE_INVALID),
    (DecisionRecordVerificationError, EXIT_STATE_INVALID),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
)


def decision_record_exit_code_for(error: BaseException) -> int:
    """把异常映射为**稳定**退出码（未知异常一律 ``4``：fail-closed、不得假设已写入）。"""
    for error_type, code in _RECORD_EXIT_CODES:
        if isinstance(error, error_type):
            return code
    return EXIT_STATE_INVALID


def main_verification_exit_code(verified: bool) -> int:
    """防伪核验结论 → 退出码（通过 ``0``，不通过 ``4``）。"""
    return EXIT_OK if verified else EXIT_STATE_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（写盘 / 打印前的**唯一**入口）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径的脱敏 + 截断表示（审计用；不做任何真实 IO）。"""
    return safe_text(str(value), max_chars=max_chars)


def _is_hex64(value: object) -> bool:
    """是否是 64 位小写十六进制摘要（内容寻址字段的**唯一**合法形态）。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX_DIGITS for char in value)
    )


def _same_path(left: Path, right: Path) -> bool:
    """两个路径是否指向同一处（Windows 大小写不敏感；不做任何真实 IO 探测）。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - 极端路径解析失败时退化为字符串比较
        return str(left) == str(right)


def _canonical_json(payload: object) -> str:
    """确定性 JSON（排序键 + 紧凑分隔符 + 保留中文），内容摘要的**唯一**口径。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_aware(moment: object, *, field_name: str) -> datetime:
    """要求带时区的 ``datetime``（naive → 参数错误，绝不默认 UTC 猜测）。"""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise DecisionRecordArgumentError(f"{field_name} 必须是带时区的 datetime")
    return moment


def _require_bool(raw: object, *, field_name: str) -> bool:
    """严格布尔（缺字段 / 非布尔 → fail-closed）。"""
    if not isinstance(raw, bool):
        raise DecisionRecordStateError(f"{field_name} 必须是布尔值")
    return raw


def _parse_aware_moment(raw: object, *, field_name: str) -> datetime:
    """解析 ISO8601 且**必须带时区**（缺时区 / 非法 → fail-closed）。"""
    if isinstance(raw, datetime):
        return _require_aware(raw, field_name=field_name)
    if not isinstance(raw, str) or not raw.strip():
        raise DecisionRecordStateError(f"{field_name} 缺失或不是字符串")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise DecisionRecordStateError(
            f"{field_name} 不是合法 ISO8601：{_safe(raw, max_chars=60)}"
        ) from exc
    return _require_aware(parsed, field_name=field_name)


def _as_moment(raw: object) -> datetime | None:
    """宽松解析（核验路径用）：不合法时返回 ``None``，由调用方记一条原因码。"""
    try:
        return _parse_aware_moment(raw, field_name="timestamp")
    except DecisionRecordError:
        return None


def _forbidden_key_paths(payload: object, *, prefix: str = "") -> list[str]:
    """递归找出所有**禁止的证据时间键**路径（操作时间绝不允许出现在文档里）。"""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key)
            location = f"{prefix}.{name}" if prefix else name
            if name in FORBIDDEN_EVIDENCE_FIELDS:
                found.append(location)
            found.extend(_forbidden_key_paths(value, prefix=location))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(_forbidden_key_paths(item, prefix=f"{prefix}[{index}]"))
    return found


def _format_violations(violations: Sequence[tuple[str, str]], *, what: str) -> str:
    """把（稳定原因码, 说明）列表汇总成**可稳定解析**的一句话（原因码在最前）。"""
    codes = sorted({code for code, _ in violations})
    details = "；".join(detail for _, detail in violations[:4])
    return (
        f"{'、'.join(codes)}：{what}未通过（{len(violations)} 项，fail-closed、零写入）：{details}"
    )


# ---------------------------------------------------------------------------
# GOLD-015 packet 文档的**严格只读**校验（复用其契约，不复制 / 不降低资格规则）
# ---------------------------------------------------------------------------
def _read_json_object(
    path: Path, *, missing_code: str, unreadable_code: str, what: str
) -> dict[str, Any]:
    """读取一个 JSON 对象（不存在 / 不可读 / 非法 / 非对象 → 稳定原因码 fail-closed）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DecisionRecordPathError(f"{missing_code}：{what}不存在：{_safe_path(path)}") from exc
    except OSError as exc:
        raise DecisionRecordStateError(
            f"{unreadable_code}：{what}不可读（{type(exc).__name__}）：{_safe_path(path)}"
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DecisionRecordStateError(
            f"{unreadable_code}：{what}不是合法 JSON：{_safe_path(path)}"
        ) from exc
    if not isinstance(payload, dict):
        raise DecisionRecordStateError(
            f"{unreadable_code}：{what}的 JSON 顶层必须是对象：{_safe_path(path)}"
        )
    return payload


def _add(found: list[tuple[str, str]], code: DecisionRecordCode, detail: str) -> None:
    """记一条（稳定原因码, 已脱敏说明）。"""
    found.append((code.value, _safe(detail, max_chars=200)))


def _verify_document_identity(payload: Mapping[str, Any], *, found: list[tuple[str, str]]) -> None:
    """文档身份：``kind`` / 报告名 / schema / 契约版本 / 执行模式（任一不符 → fail-closed）。"""
    for field, expected in (
        ("kind", DECISION_PACKET_KIND),
        ("report", DECISION_PACKET_REPORT_NAME),
        ("schema_version", DECISION_PACKET_SCHEMA_VERSION),
        ("contract_version", EVIDENCE_CONTRACT_VERSION),
        ("execution_mode", PACKET_EXECUTION_MODE),
    ):
        if payload.get(field) != expected or type(payload.get(field)) is not type(expected):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.{field} 不是 {expected!r}（文档身份不符）",
            )
    if not _is_hex64(payload.get("packet_id")):
        _add(
            found,
            DecisionRecordCode.PACKET_ID_INVALID,
            "packet.packet_id 必须是内容寻址的 64 位小写十六进制摘要",
        )
    if _as_moment(payload.get("generated_at")) is None:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.generated_at 必须是带时区的 ISO8601（审计时点）",
        )


def _verify_safety_fields(payload: Mapping[str, Any], *, found: list[tuple[str, str]]) -> None:
    """安全字段**必须**是硬编码常量（任一被削弱 → fail-closed，绝不透传上游）。"""
    for field, expected in (
        ("blocker_code", PHASE3_3_BLOCKER_CODE),
        ("blocker_active", True),
        ("human_gate_required", True),
        ("human_gate_level", "L3"),
        ("data_qualification_passed", False),
        ("phase_transition_allowed", False),
        ("data_qualification_passed_count", 0),
        ("auto_intake_allowed", False),
        ("writes_database", False),
        ("requires_explicit_operator_action", True),
        ("operator_explicit_flag", OPERATOR_EXPLICIT_FLAG),
    ):
        actual = payload.get(field)
        if actual != expected or type(actual) is not type(expected):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.{field} 被削弱：应为 {expected!r}，实际 {_safe(actual, max_chars=40)!r}",
            )
    if payload.get("evidence_time_semantics") is None:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet 缺 evidence_time_semantics 说明块")
    forbidden = _forbidden_key_paths(payload)
    if forbidden:
        _add(
            found,
            DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION,
            "packet 不允许出现证据时间字段：" + "、".join(forbidden[:8]),
        )


def _verify_code_list(raw: object, *, field: str, found: list[tuple[str, str]]) -> None:
    """原因码 / 检查键列表：必须是**已排序去重**的字符串数组（确定性）。"""
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{field} 必须是字符串数组")
        return
    if list(raw) != sorted(set(raw)):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{field} 必须是已排序去重的数组")


def _verify_handoff_block(
    payload: Mapping[str, Any], *, found: list[tuple[str, str]]
) -> Mapping[str, Any]:
    """handoff 块：报告标识 / schema / 安全字段 / ``thresholds`` / 状态与就绪一致性。"""
    handoff = payload.get("handoff")
    if not isinstance(handoff, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.handoff 必须是对象")
        return {}
    if handoff.get("report") != HANDOFF_REPORT_NAME:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.handoff.report 不是 GOLD-008 报告")
    if handoff.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.handoff.schema_version 不符")
    if handoff.get("contract_version") != EVIDENCE_CONTRACT_VERSION:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.handoff.contract_version 不符")
    if handoff.get("blocker_code") != PHASE3_3_BLOCKER_CODE:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.handoff.blocker_code 被改写")
    for field in ("blocker_active", "human_gate_required"):
        if handoff.get(field) is not True:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"packet.handoff.{field} 必须恒为 true")
    for field in ("data_qualification_passed", "phase_transition_allowed"):
        if handoff.get(field) is not False:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.handoff.{field} 必须恒为 false",
            )
    if handoff.get("thresholds") != handoff_thresholds():
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.handoff.thresholds 必须等于当前代码里的唯一阈值来源",
        )
    for field in ("artifact_sha256", "readiness_fingerprint"):
        if not _is_hex64(handoff.get(field)):
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"packet.handoff.{field} 必须是摘要")
    ready = handoff.get("ready_for_human_review")
    if not isinstance(ready, bool):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.handoff.ready_for_human_review 必须是布尔",
        )
    elif handoff.get("quantified_thresholds_met") is not ready:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.handoff.quantified_thresholds_met 与 ready_for_human_review 必须一致",
        )
    expected_status = PENDING_HUMAN_REVIEW_STATUS if ready is True else BLOCKED_STATUS
    if handoff.get("status") != expected_status:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.handoff.status 与 ready_for_human_review 不一致（应为 {expected_status}）",
        )
    _verify_code_list(handoff.get("reason_codes"), field="packet.handoff.reason_codes", found=found)
    _verify_code_list(
        handoff.get("unmet_check_keys"), field="packet.handoff.unmet_check_keys", found=found
    )
    return handoff


def _verify_check_view(
    raw: object, *, where: str, found: list[tuple[str, str]]
) -> tuple[str | None, bool]:
    """单条检查视图的算术自洽（**复用** GOLD-015 ``_verify_check_arithmetic`` 的口径）。

    Returns:
        ``(检查键, 是否 PASS)``；结构非法时返回 ``(None, False)``。
    """
    if not isinstance(raw, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where} 必须是对象")
        return None, False
    missing = [key for key in _CHECK_VIEW_KEYS if key not in raw]
    if missing:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"{where} 缺字段：{'、'.join(sorted(missing))}",
        )
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.key 必须是非空字符串")
        key = None
    comparator = raw.get("comparator")
    if comparator not in {">=", "<="}:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.comparator 必须是 >= / <=")
        return key, False
    current = raw.get("current")
    required = raw.get("required")
    evaluable = raw.get("evaluable")
    if (
        not isinstance(current, (int, float))
        or isinstance(current, bool)
        or not isinstance(required, (int, float))
        or isinstance(required, bool)
        or not isinstance(evaluable, bool)
    ):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"{where} 的 current / required / evaluable 类型非法",
        )
        return key, False
    if comparator == ">=":
        expected_status = CheckStatus.PASS if current >= required else CheckStatus.BLOCKED
        expected_remaining: int | float = max(0, required - current)
    else:
        expected_status = (
            CheckStatus.PASS if evaluable and current <= required else CheckStatus.BLOCKED
        )
        expected_remaining = round(max(0.0, current - required), 6) if evaluable else 0.0
    if raw.get("status") != expected_status.value:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"{where}.status 与 current / required / comparator / evaluable 不一致",
        )
    remaining = raw.get("remaining")
    if (
        not isinstance(remaining, (int, float))
        or isinstance(remaining, bool)
        or float(remaining) != float(expected_remaining)
    ):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.remaining 与检查阈值不一致")
    return key, raw.get("status") == CheckStatus.PASS.value


def _verify_scope_gap(
    raw: object, *, scope: str, coverage_applicable: bool, found: list[tuple[str, str]]
) -> tuple[Mapping[str, Any], list[str]]:
    """单个 scope 缺口：结构 / 算术 / 检查结论自洽（返回仍未 PASS 的检查键）。"""
    if not isinstance(raw, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"packet.gaps.{scope} 必须是对象")
        return {}, []
    missing = [key for key in _GAP_VIEW_KEYS if key not in raw]
    if missing:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope} 缺字段：{'、'.join(sorted(missing))}",
        )
    if raw.get("scope") != scope:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.scope 必须等于 {scope}",
        )
    unmet: list[str] = []
    checks = raw.get("checks")
    if not isinstance(checks, list):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, f"packet.gaps.{scope}.checks 必须是数组")
    else:
        for position, check in enumerate(checks, start=1):
            check_key, passed = _verify_check_view(
                check, where=f"packet.gaps.{scope}.checks[{position}]", found=found
            )
            if check_key is not None and not passed:
                unmet.append(check_key)
    blocked = len(unmet)
    if raw.get("remaining_checks") != blocked:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.remaining_checks 与仍未 PASS 的检查条数不一致",
        )
    expected_status = "PASS" if blocked == 0 else "BLOCKED"
    if raw.get("status") != expected_status:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.status 与检查结论不一致（应为 {expected_status}）",
        )
    if raw.get("ready") is not (raw.get("status") == "PASS"):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.ready 必须与 status 一致",
        )
    eligible = raw.get("eligible")
    required = raw.get("required")
    if (
        isinstance(eligible, int)
        and not isinstance(eligible, bool)
        and isinstance(required, int)
        and not isinstance(required, bool)
    ):
        if raw.get("remaining") != max(0, required - eligible):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.gaps.{scope}.remaining 与 required / eligible 不一致",
            )
    else:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.eligible / required 必须是整数",
        )
    if raw.get("coverage_applicable") is not coverage_applicable:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            f"packet.gaps.{scope}.coverage_applicable 必须为 {coverage_applicable}",
        )
    return raw, sorted(set(unmet))


def _verify_gaps_block(
    payload: Mapping[str, Any],
    handoff: Mapping[str, Any],
    *,
    found: list[tuple[str, str]],
) -> tuple[Mapping[str, Any], list[str]]:
    """缺口块：scope 结构 + 算术 + 未 PASS 检查键**可重算** + 与 handoff 一致。"""
    gaps = payload.get("gaps")
    if not isinstance(gaps, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.gaps 必须是对象")
        return {}, []
    author, author_unmet = _verify_scope_gap(
        gaps.get("author"), scope="author", coverage_applicable=False, found=found
    )
    news, news_unmet = _verify_scope_gap(
        gaps.get("news"), scope="news", coverage_applicable=True, found=found
    )
    unmet = sorted(set(author_unmet) | set(news_unmet))
    total = gaps.get("total_remaining_gap_count")
    expected_total = _remaining_checks(author) + _remaining_checks(news)
    if total != expected_total:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.gaps.total_remaining_gap_count 与各 scope 的 remaining_checks 之和不一致",
        )
    if total != handoff.get("total_remaining_gap_count"):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.gaps.total_remaining_gap_count 与 packet.handoff 不一致",
        )
    _verify_code_list(
        gaps.get("unmet_check_keys"), field="packet.gaps.unmet_check_keys", found=found
    )
    if list(gaps.get("unmet_check_keys") or []) != unmet:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.gaps.unmet_check_keys 无法由各 scope 的检查状态重新推导",
        )
    if list(handoff.get("unmet_check_keys") or []) != unmet:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.handoff.unmet_check_keys 与 packet.gaps 不一致",
        )
    _verify_code_list(gaps.get("reason_codes"), field="packet.gaps.reason_codes", found=found)
    if list(gaps.get("reason_codes") or []) != list(handoff.get("reason_codes") or []):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.gaps.reason_codes 与 packet.handoff.reason_codes 不一致",
        )
    return gaps, unmet


def _remaining_checks(gap: Mapping[str, Any]) -> int:
    """单个 scope 的仍未 PASS 检查条数（缺失 / 非法按 ``-1`` 计入，必然触发不一致）。"""
    value = gap.get("remaining_checks")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _verify_readiness_block(
    payload: Mapping[str, Any],
    handoff: Mapping[str, Any],
    gaps: Mapping[str, Any],
    *,
    found: list[tuple[str, str]],
) -> None:
    """readiness 快照：存在性与 handoff **同源**（指纹 / 就绪 / 缺口 / 原因码逐项一致）。"""
    state = payload.get("readiness_state")
    binding = payload.get("readiness_binding")
    if state is None:
        if binding != "NOT_PROVIDED":
            _add(
                found,
                DecisionRecordCode.READINESS_MISMATCH,
                "packet.readiness_binding 必须为 NOT_PROVIDED（没有 readiness 快照）",
            )
        return
    if not isinstance(state, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.readiness_state 必须是对象或 null")
        return
    if binding != "STATE_FILE_MATCHED":
        _add(
            found,
            DecisionRecordCode.READINESS_MISMATCH,
            "packet.readiness_binding 必须为 STATE_FILE_MATCHED",
        )
    if state.get("kind") != SNAPSHOT_KIND or state.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        _add(found, DecisionRecordCode.READINESS_MISMATCH, "readiness 快照标识 / schema 不符")
    if not _is_hex64(state.get("fingerprint")):
        _add(found, DecisionRecordCode.READINESS_MISMATCH, "readiness 快照缺内容指纹")
    if state.get("fingerprint") != handoff.get("readiness_fingerprint"):
        _add(
            found,
            DecisionRecordCode.READINESS_MISMATCH,
            "readiness 快照与 handoff 不同源（指纹不一致）",
        )
    if state.get("ready_for_human_review") is not handoff.get("ready_for_human_review"):
        _add(
            found,
            DecisionRecordCode.READINESS_MISMATCH,
            "readiness 快照与 handoff 的 ready_for_human_review 不一致",
        )
    if state.get("total_remaining_gap_count") != gaps.get("total_remaining_gap_count"):
        _add(
            found,
            DecisionRecordCode.READINESS_MISMATCH,
            "readiness 快照与 packet.gaps 的缺口计数不一致",
        )
    if list(state.get("reason_codes") or []) != list(handoff.get("reason_codes") or []):
        _add(
            found,
            DecisionRecordCode.READINESS_MISMATCH,
            "readiness 快照与 handoff 的原因码不一致",
        )


def _verify_flags_block(
    payload: Mapping[str, Any],
    handoff: Mapping[str, Any],
    *,
    found: list[tuple[str, str]],
) -> dict[str, Any]:
    """三个独立事实 + ``submit_to_l3_human_gate``：类型 / 合取关系 / 与 handoff 一致。"""
    flags: dict[str, Any] = {}
    for field in (
        "evidence_ready_for_human_review",
        "receipt_verified",
        "qualification_recheck_ready",
        "submit_to_l3_human_gate",
    ):
        value = payload.get(field)
        if isinstance(value, bool):
            flags[field] = value
        else:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"packet.{field} 必须是布尔值")
    if len(flags) == 4:
        expected_submit = (
            flags["evidence_ready_for_human_review"]
            and flags["receipt_verified"]
            and flags["qualification_recheck_ready"]
        )
        if flags["submit_to_l3_human_gate"] is not expected_submit:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.submit_to_l3_human_gate 必须是三个独立事实的合取",
            )
    ready = flags.get("evidence_ready_for_human_review")
    if (
        isinstance(ready, bool)
        and isinstance(handoff.get("ready_for_human_review"), bool)
        and ready is not handoff.get("ready_for_human_review")
    ):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.evidence_ready_for_human_review 必须等于 handoff.ready_for_human_review",
        )
    submit = flags.get("submit_to_l3_human_gate")
    if isinstance(submit, bool):
        expected_status = (
            DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE.value
            if submit
            else DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE.value
        )
        if payload.get("status") != expected_status:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.status 必须由三个独立事实的合取推导（应为 {expected_status}）",
            )
    return flags


def _verify_plan_and_receipt_block(
    payload: Mapping[str, Any], flags: Mapping[str, Any], *, found: list[tuple[str, str]]
) -> None:
    """plan 块与 receipt 块：内容寻址摘要 / 状态 / ``receipt_verified`` 往返一致。"""
    plan = payload.get("plan")
    if not isinstance(plan, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.plan 必须是对象")
    else:
        if not _is_hex64(plan.get("plan_id")):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.plan.plan_id 必须是内容寻址摘要",
            )
        if plan.get("status") not in {member.value for member in IntakePlanStatus}:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.plan.status 不是合法计划状态",
            )
    receipt = payload.get("receipt")
    if not isinstance(receipt, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.receipt 必须是对象")
        return
    verified_flag = flags.get("receipt_verified")
    receipt_status = receipt.get("status")
    if isinstance(verified_flag, bool):
        is_verified = receipt_status == IntakeReceiptStatus.VERIFIED_EXECUTION_RECORDED.value
        if verified_flag is not is_verified:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.receipt.status 与 packet.receipt_verified 不一致",
            )
    if not _is_hex64(receipt.get("receipt_id")):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.receipt.receipt_id 必须是内容寻址的 64 位小写十六进制摘要",
        )
    provided = receipt.get("receipt_file_provided")
    if not isinstance(provided, bool):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.receipt.receipt_file_provided 必须是布尔",
        )
        return
    matched = receipt.get("receipt_file_matched")
    if provided is not (receipt.get("path") is not None):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.receipt.receipt_file_provided 必须与 path 是否给出一致",
        )
    if provided:
        if not isinstance(matched, bool):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.receipt.receipt_file_matched 必须是布尔",
            )
        if not _is_hex64(receipt.get("artifact_sha256")):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.receipt.artifact_sha256 必须是摘要",
            )
    elif matched is not None:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.receipt.receipt_file_matched 必须为 null（未给出收据文件）",
        )


def _verify_recheck_block(
    payload: Mapping[str, Any], flags: Mapping[str, Any], *, found: list[tuple[str, str]]
) -> None:
    """qualification recheck 块：存在性 / ``ready`` / ``receipt_verified`` 的合取一致。"""
    recheck = payload.get("qualification_recheck")
    recheck_ready = flags.get("qualification_recheck_ready")
    receipt_verified = flags.get("receipt_verified")
    if recheck is None:
        if recheck_ready is True:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.qualification_recheck_ready 为 true 但没有 recheck 事实",
            )
        return
    if not isinstance(recheck, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.qualification_recheck 必须是对象")
        return
    if recheck.get("report") != QUALIFICATION_RECHECK_REPORT:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.report 不是复核产物",
        )
    if recheck.get("blocker_code") != PHASE3_3_BLOCKER_CODE:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.blocker_code 被改写",
        )
    for field in ("blocker_active", "human_gate_required", "human_gate_required_for_phase_change"):
        if recheck.get(field) is not True:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.qualification_recheck.{field} 必须恒为 true",
            )
    if recheck.get("is_qualification_decision") is not False:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.is_qualification_decision 必须恒为 false",
        )
    if _as_moment(recheck.get("as_of")) is None:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.as_of 必须带时区",
        )
    if not _is_hex64(recheck.get("artifact_sha256")):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck 缺产物摘要",
        )
    ready = recheck.get("ready")
    if not isinstance(ready, bool):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.ready 必须是布尔",
        )
    elif (
        isinstance(recheck_ready, bool)
        and isinstance(receipt_verified, bool)
        and recheck_ready is not (ready and receipt_verified)
    ):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck_ready 必须是 recheck.ready 与 receipt_verified 的合取",
        )
    gate = recheck.get("gate")
    if not isinstance(gate, Mapping):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.qualification_recheck.gate 必须是对象",
        )
        return
    for field in (
        "qualification_pass_count",
        "qualification_blocked_count",
        "readiness_blocked_scope_count",
    ):
        value = gate.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.qualification_recheck.gate.{field} 必须是非负整数",
            )


def _verify_entries_block(payload: Mapping[str, Any], *, found: list[tuple[str, str]]) -> int:
    """批准条目 / ledger 摘要 / 执行结果：计数、摘要形态与落库行数自洽（返回落库行数）。"""
    entries = payload.get("approved_entries")
    if not isinstance(entries, list):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.approved_entries 必须是数组")
        entries = []
    if payload.get("approved_entry_count") != len(entries):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.approved_entry_count 与 approved_entries 长度不一致",
        )
    for index, entry in enumerate(entries, start=1):
        where = f"packet.approved_entries[{index}]"
        if not isinstance(entry, Mapping):
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where} 必须是对象")
            continue
        for field, expected in (
            ("approved_for_explicit_intake", True),
            ("data_qualification_passed", False),
            ("requires_explicit_operator_action", True),
        ):
            if entry.get(field) is not expected:
                _add(
                    found,
                    DecisionRecordCode.PACKET_TAMPERED,
                    f"{where}.{field} 必须恒为 {expected}",
                )
        if not isinstance(entry.get("intake_executed"), bool):
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.intake_executed 必须是布尔")
        if not _is_hex64(entry.get("fingerprint")):
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.fingerprint 必须是摘要")
        decision_id = entry.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.decision_id 必须是非空字符串")
        revision = entry.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.revision 必须是 >= 1 的整数")
        content = entry.get("content_sha256")
        if not isinstance(content, list) or any(not _is_hex64(item) for item in content):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"{where}.content_sha256 必须是摘要数组",
            )
    ledger = payload.get("ledger_revision_digest")
    if not isinstance(ledger, list):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.ledger_revision_digest 必须是数组")
        ledger = []
    for index, item in enumerate(ledger, start=1):
        if not isinstance(item, Mapping) or not _is_hex64(item.get("fingerprint")):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"packet.ledger_revision_digest[{index}] 必须带内容指纹",
            )
    verification = payload.get("verification")
    fingerprints = (
        verification.get("ledger_fingerprints") if isinstance(verification, Mapping) else None
    )
    if fingerprints != len(ledger):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.verification.ledger_fingerprints 与 ledger_revision_digest 长度不一致",
        )
    landed = payload.get("landed_rows")
    if isinstance(landed, bool) or not isinstance(landed, int) or landed < 0:
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.landed_rows 必须是非负整数")
    operator_results = payload.get("operator_results")
    if not isinstance(operator_results, list):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.operator_results 必须是数组")
        return 0
    total = 0
    for index, item in enumerate(operator_results, start=1):
        where = f"packet.operator_results[{index}]"
        if not isinstance(item, Mapping):
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where} 必须是对象")
            continue
        if item.get("report") != OPERATOR_MANIFEST_REPORT:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"{where}.report 不是 Evidence Operator 产物",
            )
        if item.get("scope") not in {"author", "news"}:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{where}.scope 非法")
        if item.get("dry_run") is not False or item.get("explicit_execution") is not True:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"{where} 必须是显式执行（dry_run=false、explicit_execution=true）",
            )
        counts = item.get("counts")
        persisted = counts.get("persisted") if isinstance(counts, Mapping) else None
        if isinstance(persisted, bool) or not isinstance(persisted, int) or persisted < 0:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"{where}.counts.persisted 必须是非负整数",
            )
            continue
        if item.get("landed_rows") != persisted:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                f"{where}.landed_rows 与 counts.persisted 不一致",
            )
        total += persisted
    if isinstance(landed, int) and not isinstance(landed, bool) and landed != total:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.landed_rows 与各执行结果的落库行数之和不一致",
        )
    return total


def _verify_verification_block(payload: Mapping[str, Any], *, found: list[tuple[str, str]]) -> None:
    """verification 块与时间语义说明：核验必须干净、revalidated_at 与 generated_at 一致。"""
    verification = payload.get("verification")
    if not isinstance(verification, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.verification 必须是对象")
        return
    if verification.get("violations") != []:
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.verification.violations 必须为空（带不一致的 packet 不得被采信）",
        )
    inbox_packages = verification.get("inbox_packages")
    preflight = verification.get("preflight_pass")
    fingerprints = verification.get("ledger_fingerprints")
    for field, value in (
        ("packet.verification.inbox_packages", inbox_packages),
        ("packet.verification.preflight_pass", preflight),
        ("packet.verification.ledger_fingerprints", fingerprints),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _add(found, DecisionRecordCode.PACKET_TAMPERED, f"{field} 必须是非负整数")
    if (
        isinstance(inbox_packages, int)
        and not isinstance(inbox_packages, bool)
        and isinstance(preflight, int)
        and not isinstance(preflight, bool)
        and preflight > inbox_packages
    ):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.verification.preflight_pass 不得大于 inbox_packages",
        )
    if verification.get("revalidated_at") != payload.get("generated_at"):
        _add(
            found,
            DecisionRecordCode.PACKET_TAMPERED,
            "packet.verification.revalidated_at 必须等于 packet.generated_at（同一审计时点）",
        )
    semantics = payload.get("evidence_time_semantics")
    if not isinstance(semantics, Mapping):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.evidence_time_semantics 必须是对象")
    else:
        if semantics.get("contains_evidence_times") is not False:
            _add(
                found,
                DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION,
                "packet.evidence_time_semantics.contains_evidence_times 必须恒为 false",
            )
        audit_times = semantics.get("audit_times_only")
        if not isinstance(audit_times, list) or not audit_times:
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.evidence_time_semantics.audit_times_only 必须是非空数组",
            )
        elif any(not isinstance(item, str) for item in audit_times):
            _add(
                found,
                DecisionRecordCode.PACKET_TAMPERED,
                "packet.evidence_time_semantics.audit_times_only 必须是字符串数组",
            )
    written_path = payload.get("written_path")
    if written_path is not None and not isinstance(written_path, str):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.written_path 必须是字符串或 null")
    notes = payload.get("notes")
    if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
        _add(found, DecisionRecordCode.PACKET_TAMPERED, "packet.notes 必须是字符串数组")


def _verify_no_future_timestamps(
    payload: Mapping[str, Any],
    handoff: Mapping[str, Any],
    *,
    moment: datetime,
    found: list[tuple[str, str]],
) -> None:
    """审计时点不得晚于本次人工决策时点（**不得对未来的 packet 作决策**）。"""
    verification = payload.get("verification")
    recheck = payload.get("qualification_recheck")
    candidates: list[tuple[str, object]] = [
        ("packet.generated_at", payload.get("generated_at")),
        ("packet.handoff.as_of", handoff.get("as_of")),
        (
            "packet.verification.revalidated_at",
            verification.get("revalidated_at") if isinstance(verification, Mapping) else None,
        ),
        (
            "packet.qualification_recheck.as_of",
            recheck.get("as_of") if isinstance(recheck, Mapping) else None,
        ),
    ]
    for name, raw in candidates:
        parsed = _as_moment(raw)
        if parsed is not None and parsed > moment:
            _add(
                found,
                DecisionRecordCode.FUTURE_TIMESTAMP,
                f"{name} 晚于本次人工决策时点（不得对尚未产生的 packet 作决策）",
            )


def _verify_packet_payload(
    payload: Mapping[str, Any], *, moment: datetime
) -> list[tuple[str, str]]:
    """对 GOLD-015 packet 文档做**逐项**完整性核验（返回全部不一致；空列表 = 通过）。"""
    found: list[tuple[str, str]] = []
    _verify_document_identity(payload, found=found)
    _verify_safety_fields(payload, found=found)
    handoff = _verify_handoff_block(payload, found=found)
    gaps, _ = _verify_gaps_block(payload, handoff, found=found)
    _verify_readiness_block(payload, handoff, gaps, found=found)
    flags = _verify_flags_block(payload, handoff, found=found)
    _verify_plan_and_receipt_block(payload, flags, found=found)
    _verify_recheck_block(payload, flags, found=found)
    _verify_entries_block(payload, found=found)
    _verify_verification_block(payload, found=found)
    _verify_no_future_timestamps(payload, handoff, moment=moment, found=found)
    return found


@dataclass(frozen=True, slots=True)
class PacketBinding:
    """GOLD-015 packet 的**只读绑定**（内容寻址摘要 + 三个独立事实 + 安全字段）。

    Attributes:
        path: packet 文件路径（脱敏后用于审计）。
        packet_id: GOLD-015 内容级决策包标识（**原样**复用，不重算、不改造）。
        content_sha256: packet 文档**规范化 JSON** 的内容摘要（记录绑定的核心）。
        artifact_sha256: packet 文件**原始字节**摘要（任何字节改动都会改变它）。
        checks: 本次完整性核验**全部通过**的不变量清单（见 :data:`PACKET_CHECKS`）。
    """

    path: str
    packet_id: str
    content_sha256: str
    artifact_sha256: str
    generated_at: datetime
    status: str
    submit_to_l3_human_gate: bool
    evidence_ready_for_human_review: bool
    receipt_verified: bool
    qualification_recheck_ready: bool
    blocker_code: str
    handoff_artifact_sha256: str
    plan_id: str
    receipt_id: str
    readiness_binding: str
    written_path: str | None
    written_path_matches: bool | None
    checks: tuple[str, ...] = PACKET_CHECKS

    @property
    def submittable(self) -> bool:
        """packet **本身**是否满足提交 L3 人工 Gate 的条件（仍**不是**资格通过）。"""
        return self.submit_to_l3_human_gate

    def digest_block(self) -> dict[str, Any]:
        """``record_id`` 绑定块（只含必需字段 + 审计时点；不含自由文本）。"""
        return {
            "packet_id": self.packet_id,
            "content_sha256": self.content_sha256,
            "artifact_sha256": self.artifact_sha256,
            "generated_at": self.generated_at.isoformat(),
            "status": self.status,
            "submit_to_l3_human_gate": self.submit_to_l3_human_gate,
            "evidence_ready_for_human_review": self.evidence_ready_for_human_review,
            "receipt_verified": self.receipt_verified,
            "qualification_recheck_ready": self.qualification_recheck_ready,
            "blocker_code": self.blocker_code,
            "handoff_artifact_sha256": self.handoff_artifact_sha256,
            "plan_id": self.plan_id,
            "receipt_id": self.receipt_id,
            "readiness_binding": self.readiness_binding,
        }

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "path": _safe_path(self.path),
            "packet_id": self.packet_id,
            "content_sha256": self.content_sha256,
            "artifact_sha256": self.artifact_sha256,
            "generated_at": self.generated_at.isoformat(),
            "status": _safe(self.status, max_chars=40),
            "submit_to_l3_human_gate": self.submit_to_l3_human_gate,
            "evidence_ready_for_human_review": self.evidence_ready_for_human_review,
            "receipt_verified": self.receipt_verified,
            "qualification_recheck_ready": self.qualification_recheck_ready,
            "blocker_code": self.blocker_code,
            "handoff_artifact_sha256": self.handoff_artifact_sha256,
            "plan_id": _safe(self.plan_id, max_chars=80),
            "receipt_id": _safe(self.receipt_id, max_chars=80),
            "readiness_binding": _safe(self.readiness_binding, max_chars=40),
            "written_path": None if self.written_path is None else _safe_path(self.written_path),
            "written_path_matches": self.written_path_matches,
            "checks_passed": list(self.checks),
        }


def load_decision_packet(packet_path: Path | str, *, moment: datetime) -> PacketBinding:
    """读取 GOLD-015 决策包并做**逐项完整性核验**（任一不一致 → fail-closed、零写入）。

    Raises:
        DecisionRecordPathError: packet 文件不存在（``PACKET_NOT_FOUND``）。
        DecisionRecordStateError: 不是合法 JSON / 顶层非对象 / 文档身份不符 / 安全字段被削弱 /
            缺口算术或检查结论不自洽 / readiness 不同源 / 收据或 recheck 绑定矛盾 /
            出现证据时间键 / 出现未来时间（``EVIDENCE_TIME_SUBSTITUTION`` / ``FUTURE_TIMESTAMP`` /
            ``READINESS_MISMATCH`` / ``PACKET_ID_INVALID`` / ``PACKET_TAMPERED`` 等稳定原因码）。
    """
    target = Path(packet_path)
    moment = _require_aware(moment, field_name="moment")
    payload = _read_json_object(
        target,
        missing_code=DecisionRecordCode.PACKET_NOT_FOUND.value,
        unreadable_code=DecisionRecordCode.PACKET_UNREADABLE.value,
        what="GOLD-015 decision packet",
    )
    violations = _verify_packet_payload(payload, moment=moment)
    if violations:
        raise DecisionRecordStateError(_format_violations(violations, what="packet 完整性核验"))
    handoff = payload["handoff"]
    receipt = payload["receipt"]
    plan = payload["plan"]
    written_path = payload.get("written_path")
    matches: bool | None = None
    if isinstance(written_path, str) and written_path.strip():
        matches = _same_path(Path(written_path), target)
    return PacketBinding(
        path=str(target),
        packet_id=str(payload["packet_id"]),
        content_sha256=hashing.sha256_text(_canonical_json(payload)),
        artifact_sha256=hashing.sha256_bytes(target.read_bytes()),
        generated_at=_parse_aware_moment(
            payload.get("generated_at"), field_name="packet.generated_at"
        ),
        status=str(payload.get("status")),
        submit_to_l3_human_gate=bool(payload["submit_to_l3_human_gate"]),
        evidence_ready_for_human_review=bool(payload["evidence_ready_for_human_review"]),
        receipt_verified=bool(payload["receipt_verified"]),
        qualification_recheck_ready=bool(payload["qualification_recheck_ready"]),
        blocker_code=str(payload["blocker_code"]),
        handoff_artifact_sha256=str(handoff["artifact_sha256"]),
        plan_id=str(plan["plan_id"]),
        receipt_id=str(receipt["receipt_id"]),
        readiness_binding=str(payload["readiness_binding"]),
        written_path=written_path if isinstance(written_path, str) else None,
        written_path_matches=matches,
    )


# ---------------------------------------------------------------------------
# 人工输入（**显式**、非敏感、脱敏、限长；工具绝不自行生成批准）
# ---------------------------------------------------------------------------
def _clean_decision(decision: str | HumanDecision) -> HumanDecision:
    """人工决策**必须**显式给出且只有三种取值（大小写敏感，不做任何静默转换）。"""
    if isinstance(decision, HumanDecision):
        return decision
    text = decision if isinstance(decision, str) else ""
    if text not in DECISION_RECORD_ACTIONS:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.DECISION_INVALID.value}：decision 必须是 "
            f"{' / '.join(DECISION_RECORD_ACTIONS)} 之一（**必须**由人工显式给出，"
            f"工具绝不自行生成批准）；实际：{_safe(decision, max_chars=40)!r}"
        )
    return HumanDecision(text)


def _clean_reviewer(reviewer: object) -> str:
    """reviewer **必须**是显式、非敏感的 operator / reviewer label（不采集任何凭据）。"""
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVIEWER_INVALID.value}：reviewer 必须显式给出"
            "（非敏感 operator / reviewer label；本工具不采集任何凭据）"
        )
    text = reviewer.strip()
    if len(text) > MAX_RECORD_REVIEWER_CHARS:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVIEWER_INVALID.value}：reviewer 超过 "
            f"{MAX_RECORD_REVIEWER_CHARS} 字符（请使用简短非敏感标签）"
        )
    if not _REVIEWER_PATTERN.match(text):
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVIEWER_INVALID.value}：reviewer 只允许字母 / 数字 / `._@+-`，"
            "且必须以字母或数字开头（不接受空格、引号、路径分隔符）"
        )
    if _SECRET_LIKE_PATTERN.search(text) or _BLOB_LIKE_PATTERN.match(text):
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVIEWER_INVALID.value}：reviewer 看起来像凭据 / 密钥串，"
            "本工具**绝不**采集任何凭据"
        )
    return text


def _clean_reason_code(reason_code: object) -> str | None:
    """可选的稳定原因码（大写形态；脱敏 + 限长；不做静默大小写转换）。"""
    if reason_code is None:
        return None
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REASON_CODE_INVALID.value}：reason-code 必须是非空字符串"
            "（或缺省）"
        )
    text = reason_code.strip()
    if len(text) > MAX_RECORD_REASON_CODE_CHARS or not _REASON_CODE_PATTERN.match(text):
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REASON_CODE_INVALID.value}：reason-code 必须是大写稳定原因码形态"
            f"（^[A-Z][A-Z0-9_]*$，<= {MAX_RECORD_REASON_CODE_CHARS} 字符）"
        )
    return safe_text(text, max_chars=MAX_RECORD_REASON_CODE_CHARS)


def _clean_note(note: object) -> str | None:
    """可选 note：**脱敏 + 限长**；超长输入 fail-closed（不静默截断成假事实）。"""
    if note is None:
        return None
    if not isinstance(note, str):
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.NOTE_TOO_LONG.value}：note 必须是字符串（或缺省）"
        )
    text = note.strip()
    if not text:
        return None
    if len(text) > MAX_RECORD_NOTE_INPUT_CHARS:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.NOTE_TOO_LONG.value}：note 超过 "
            f"{MAX_RECORD_NOTE_INPUT_CHARS} 字符（fail-closed，绝不静默截断）"
        )
    return safe_text(text, max_chars=MAX_RECORD_NOTE_CHARS)


def _clean_revision(revision: object, supersedes: object) -> tuple[int, str | None]:
    """``revision`` / ``supersedes``：冲突必须**显式**声明，且 revision 必须严格递增。"""
    supersedes_id: str | None = None
    if supersedes is not None:
        if not isinstance(supersedes, str) or not _is_hex64(supersedes.strip().lower()):
            raise DecisionRecordArgumentError(
                f"{DecisionRecordCode.SUPERSEDES_MISMATCH.value}：supersedes 必须是 64 位小写"
                "十六进制 record_id（显式指向被取代的记录）"
            )
        supersedes_id = supersedes.strip().lower()
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVISION_INVALID.value}：revision 必须是整数"
        )
    if revision < 1 or revision > MAX_RECORD_REVISION:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVISION_INVALID.value}：revision 必须在 1 ~ "
            f"{MAX_RECORD_REVISION} 之间"
        )
    if supersedes_id is None and revision != 1:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVISION_INVALID.value}：声明 revision>1 必须同时给出 supersedes"
            "（不得静默改写历史）"
        )
    if supersedes_id is not None and revision <= 1:
        raise DecisionRecordArgumentError(
            f"{DecisionRecordCode.REVISION_INVALID.value}：supersedes 必须使用 revision >= 2"
        )
    return revision, supersedes_id


# ---------------------------------------------------------------------------
# L3 人工决策记录（确定性 / 脱敏 / 内容寻址；默认零写入）
# ---------------------------------------------------------------------------
def _policy_block() -> dict[str, Any]:
    """记录策略块（安全字段**硬编码**，与任何输入无关；绝不透传上游）。"""
    return {
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "human_gate_level": "L3",
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "phase_transition_executed": False,
        "auto_intake_allowed": False,
        "writes_database": False,
        "writes_project_state": False,
        "requires_explicit_operator_action": True,
        "record_execution_mode": DECISION_RECORD_EXECUTION_MODE,
        "decision_scope": DECISION_SCOPE,
        "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
    }


def _record_id_payload(
    *,
    decision: str,
    reviewer: str,
    note: str | None,
    reason_code: str | None,
    revision: int,
    supersedes: str | None,
    packet_block: Mapping[str, Any],
) -> dict[str, Any]:
    """``record_id`` 的输入（含策略块 + 人工决策 + packet 绑定；**不含**任何审计时间）。"""
    return {
        "policy": _policy_block(),
        "decision": decision,
        "reviewer": reviewer,
        "note": note,
        "reason_code": reason_code,
        "revision": revision,
        "supersedes": supersedes,
        "packet": {key: packet_block.get(key) for key in _PACKET_BINDING_KEYS},
    }


def _compute_record_id(
    *,
    decision: str,
    reviewer: str,
    note: str | None,
    reason_code: str | None,
    revision: int,
    supersedes: str | None,
    packet_block: Mapping[str, Any],
) -> str:
    """由（策略块 + 人工决策 + packet 绑定）派生内容级 ``record_id``。"""
    return hashing.sha256_text(
        _canonical_json(
            _record_id_payload(
                decision=decision,
                reviewer=reviewer,
                note=note,
                reason_code=reason_code,
                revision=revision,
                supersedes=supersedes,
                packet_block=packet_block,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class HumanDecisionRecord:
    """一次 **L3 人工决策记录**（**确定性脱敏**；默认零写入，``written_path`` 才落盘）。

    Attributes:
        decision_at: 本次人工决策的**审计操作时间**（**不是**证据时间）。
        generated_at: 记录生成的审计时点（与 ``decision_at`` 同一时点）。
        packet: 被绑定的 GOLD-015 packet（已通过完整性核验）。
        record_id: 内容级记录标识（不含任何审计时间）。
    """

    decision_at: datetime
    generated_at: datetime
    decision: HumanDecision
    reviewer: str
    packet: PacketBinding
    note: str | None = None
    reason_code: str | None = None
    revision: int = 1
    supersedes: str | None = None
    record_id: str = ""
    written_path: str | None = None
    notes: tuple[str, ...] = (DECISION_RECORD_NOTE,)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return DECISION_RECORD_KIND

    @property
    def human_decision(self) -> str:
        """人工决策原文（``approve`` / ``reject`` / ``needs_changes``）。"""
        return self.decision.value

    @property
    def human_decision_recorded(self) -> bool:
        """是否真的记录了一次人工决策（**本对象存在即为 true**；与资格无关）。"""
        return True

    @property
    def packet_verified(self) -> bool:
        """packet 完整性核验是否通过（构造本对象的前置条件；仍**不是**资格通过）。"""
        return True

    @property
    def data_qualification_passed(self) -> bool:
        """恒为 ``False``：本工具**没有**资格判定能力（硬编码）。"""
        return False

    @property
    def phase_transition_allowed(self) -> bool:
        """恒为 ``False``：Phase 切换只能由 **L3 人工 Gate** 决定。"""
        return False

    @property
    def phase_transition_executed(self) -> bool:
        """恒为 ``False``：本工具**不执行**任何 Phase transition。"""
        return False

    @property
    def approve_gate_satisfied(self) -> bool | None:
        """``approve`` 的门禁是否满足（非 ``approve`` 决策返回 ``None``）。"""
        if self.decision is not HumanDecision.APPROVE:
            return None
        return self.packet.submit_to_l3_human_gate and self.packet_verified

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段与语义布尔**显式且恒定**）。"""
        generated = self.generated_at.isoformat()
        return {
            "kind": DECISION_RECORD_KIND,
            "report": DECISION_RECORD_REPORT_NAME,
            "schema_version": DECISION_RECORD_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "record_id": self.record_id,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "decision_at": self.decision_at.isoformat(),
            "generated_at": generated,
            "decision": self.decision.value,
            "human_decision": self.decision.value,
            "human_decision_recorded": True,
            "reviewer": _safe(self.reviewer, max_chars=MAX_RECORD_REVIEWER_CHARS),
            "reviewer_kind": REVIEWER_KIND,
            "reason_code": self.reason_code,
            "note": None
            if self.note is None
            else _safe(self.note, max_chars=MAX_RECORD_NOTE_CHARS),
            "decision_scope": DECISION_SCOPE,
            "execution_mode": DECISION_RECORD_EXECUTION_MODE,
            "operator_explicit_flag": OPERATOR_EXPLICIT_FLAG,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "human_gate_level": "L3",
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "phase_transition_executed": False,
            "auto_intake_allowed": False,
            "writes_database": False,
            "writes_project_state": False,
            "project_state_modified": False,
            "requires_explicit_operator_action": True,
            "packet_verified": True,
            "packet_verification": {
                "verified_at": generated,
                "checks_passed": list(self.packet.checks),
                "violations": [],
                "packet_id_recomputable_from_record": False,
                "note": (
                    "packet_id 由 GOLD-015 从其输入（handoff / readiness / plan / 收据 / recheck）"
                    "派生，本记录**不复算、不改造**它：记录改为绑定 packet **内容摘要**"
                    "（content_sha256）与**产物摘要**（artifact_sha256），任何字节 / 语义改动都会被"
                    " `verify_decision_record` 判为 PACKET_CONTENT_MISMATCH / PACKET_ID_MISMATCH。"
                ),
            },
            "packet": self.packet.to_dict(),
            "approve_gate": {
                "applies": self.decision is HumanDecision.APPROVE,
                "packet_id_valid": True,
                "verification_passed": True,
                "packet_submittable": self.packet.submit_to_l3_human_gate,
                "satisfied": self.approve_gate_satisfied,
            },
            "audit_times": {
                "decision_at": self.decision_at.isoformat(),
                "generated_at": generated,
                "packet_generated_at": self.packet.generated_at.isoformat(),
                "packet_verification_verified_at": generated,
            },
            "evidence_time_semantics": {
                "contains_evidence_times": False,
                "audit_times_only": [
                    "decision_at",
                    "generated_at",
                    "packet.generated_at",
                    "packet_verification.verified_at",
                ],
                "note": DECISION_RECORD_TIME_SEMANTICS_NOTE,
            },
            "next_step": DECISION_RECORD_NEXT_STEP_NOTE,
            "notes": list(self.notes),
            "written_path": self.written_path,
        }


def compute_record_id(record: HumanDecisionRecord) -> str:
    """由记录（策略块 + 人工决策 + packet 绑定）派生内容级 ``record_id``。

    ⚠️ 刻意**不含** ``decision_at`` / ``generated_at`` / ``written_path``：同一 packet + 同一
    人工决策 + 同一 ``revision`` / ``supersedes`` 重复记录得到**同一** ``record_id``；
    ``packet_id`` / ``decision`` / ``reviewer`` / ``note`` / ``reason_code`` / ``revision`` /
    ``supersedes`` 或 packet 内容任一变化必然产生**新** ``record_id``。
    """
    return _compute_record_id(
        decision=record.decision.value,
        reviewer=record.reviewer,
        note=record.note,
        reason_code=record.reason_code,
        revision=record.revision,
        supersedes=record.supersedes,
        packet_block=record.packet.digest_block(),
    )


def build_decision_record(
    packet: PacketBinding,
    *,
    decision: str | HumanDecision,
    reviewer: object,
    moment: datetime,
    note: object = None,
    reason_code: object = None,
    revision: object = 1,
    supersedes: object = None,
) -> HumanDecisionRecord:
    """把**人工显式**决策绑到已核验 packet 上（**零写入**；门禁不满足 → fail-closed）。

    安全语义：

    - ``decision`` / ``reviewer`` **必须**显式给出，工具**绝不**自行生成批准、**绝不**推断意图；
    - ``approve`` 要求 packet **本身** ``submit_to_l3_human_gate=true``（否则
      :class:`DecisionRecordNotSubmittableError`：**预期 BLOCKED**，零写入）；
    - ``reject`` / ``needs_changes`` **可以**被记录（即使 packet 仍 BLOCKED），但**绝不**改变
      任何资格状态（``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 false）。

    Raises:
        DecisionRecordArgumentError: ``moment`` 未带时区，或 ``decision`` / ``reviewer`` /
            ``note`` / ``reason_code`` / ``revision`` / ``supersedes`` 不合法。
        DecisionRecordNotSubmittableError: ``approve`` 落在不可提交的 packet 上。
    """
    moment = _require_aware(moment, field_name="moment")
    cleaned_decision = _clean_decision(decision)
    cleaned_reviewer = _clean_reviewer(reviewer)
    cleaned_note = _clean_note(note)
    cleaned_reason = _clean_reason_code(reason_code)
    cleaned_revision, cleaned_supersedes = _clean_revision(revision, supersedes)
    if cleaned_decision is HumanDecision.APPROVE and not packet.submit_to_l3_human_gate:
        raise DecisionRecordNotSubmittableError(
            f"{DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value}：packet（{packet.packet_id[:16]}…，"
            f"status={packet.status}）的 submit_to_l3_human_gate=false，**不得**记录 approve；"
            "请先补齐真实证据链再提交 L3 人工 Gate，或改记 reject / needs_changes"
            "（reject / needs_changes 不改变任何资格状态）"
        )
    provisional = HumanDecisionRecord(
        decision_at=moment,
        generated_at=moment,
        decision=cleaned_decision,
        reviewer=cleaned_reviewer,
        packet=packet,
        note=cleaned_note,
        reason_code=cleaned_reason,
        revision=cleaned_revision,
        supersedes=cleaned_supersedes,
    )
    return replace(provisional, record_id=compute_record_id(provisional))


def _verify_binding_block(block: object, *, found: list[tuple[str, str]]) -> None:
    """记录里的 packet 绑定块：键齐备、摘要合法、布尔为布尔（被改写 → RECORD_TAMPERED）。"""
    if not isinstance(block, Mapping):
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "记录缺 packet 绑定块")
        return
    missing = [key for key in _PACKET_BINDING_KEYS if key not in block]
    if missing:
        _add(
            found,
            DecisionRecordCode.RECORD_TAMPERED,
            "记录 packet 绑定块缺字段：" + "、".join(sorted(missing)),
        )
    for key in ("packet_id", "content_sha256", "artifact_sha256", "handoff_artifact_sha256"):
        if not _is_hex64(block.get(key)):
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"记录 packet.{key} 必须是摘要")
    for key in ("plan_id", "receipt_id"):
        if not _is_hex64(block.get(key)):
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"记录 packet.{key} 必须是内容寻址摘要")
    for key in ("status", "blocker_code", "readiness_binding"):
        if not isinstance(block.get(key), str) or not block.get(key):
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"记录 packet.{key} 必须是非空字符串")
    for key in (
        "submit_to_l3_human_gate",
        "evidence_ready_for_human_review",
        "receipt_verified",
        "qualification_recheck_ready",
    ):
        if not isinstance(block.get(key), bool):
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"记录 packet.{key} 必须是布尔")
    if _as_moment(block.get("generated_at")) is None:
        _add(
            found,
            DecisionRecordCode.RECORD_TAMPERED,
            "记录 packet.generated_at 必须是带时区的 ISO8601",
        )


def _load_record_document(record_path: Path | str) -> dict[str, Any]:
    """读取既有决策记录并做**逐项**结构核验（被改写 → ``RECORD_TAMPERED``，fail-closed）。"""
    target = Path(record_path)
    payload = _read_json_object(
        target,
        missing_code=DecisionRecordCode.RECORD_NOT_FOUND.value,
        unreadable_code=DecisionRecordCode.RECORD_UNREADABLE.value,
        what="L3 人工决策记录",
    )
    problems = _record_document_problems(payload)
    if problems:
        raise DecisionRecordStateError(
            _format_violations(problems, what="记录文档完整性核验")
        )
    return payload


def _record_id_from_document(payload: Mapping[str, Any]) -> str:
    """从记录文档**重新推导** ``record_id``（防伪核验：与文档声明值必须一致）。"""
    block = payload.get("packet")
    revision = payload.get("revision")
    return _compute_record_id(
        decision=str(payload.get("decision")),
        reviewer=str(payload.get("reviewer")),
        note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        reason_code=payload.get("reason_code")
        if isinstance(payload.get("reason_code"), str)
        else None,
        revision=revision
        if isinstance(revision, int) and not isinstance(revision, bool)
        else -1,
        supersedes=payload.get("supersedes")
        if isinstance(payload.get("supersedes"), str)
        else None,
        packet_block=block if isinstance(block, Mapping) else {},
    )


def _record_document_problems(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """既有决策记录文档的结构 / 安全字段核验（任何一处被改写 → ``RECORD_TAMPERED``）。"""
    found: list[tuple[str, str]] = []
    for field, expected in (
        ("kind", DECISION_RECORD_KIND),
        ("report", DECISION_RECORD_REPORT_NAME),
        ("schema_version", DECISION_RECORD_SCHEMA_VERSION),
        ("contract_version", EVIDENCE_CONTRACT_VERSION),
        ("execution_mode", DECISION_RECORD_EXECUTION_MODE),
        ("decision_scope", DECISION_SCOPE),
        ("operator_explicit_flag", OPERATOR_EXPLICIT_FLAG),
        ("blocker_code", PHASE3_3_BLOCKER_CODE),
        ("blocker_active", True),
        ("human_gate_required", True),
        ("human_gate_level", "L3"),
        ("data_qualification_passed", False),
        ("phase_transition_allowed", False),
        ("phase_transition_executed", False),
        ("auto_intake_allowed", False),
        ("writes_database", False),
        ("writes_project_state", False),
        ("project_state_modified", False),
        ("requires_explicit_operator_action", True),
        ("human_decision_recorded", True),
        ("packet_verified", True),
    ):
        actual = payload.get(field)
        if actual != expected or type(actual) is not type(expected):
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"记录字段 {field} 不是 {expected!r}")
    if not _is_hex64(payload.get("record_id")):
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "record_id 必须是 64 位小写十六进制")
    decision = payload.get("decision")
    if decision not in DECISION_RECORD_ACTIONS or payload.get("human_decision") != decision:
        _add(
            found,
            DecisionRecordCode.RECORD_TAMPERED,
            "decision / human_decision 必须一致且属于允许取值",
        )
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer:
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "reviewer 必须是非空字符串")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "revision 必须是 >= 1 的整数")
    supersedes = payload.get("supersedes")
    if supersedes is not None and not _is_hex64(supersedes):
        _add(
            found,
            DecisionRecordCode.RECORD_TAMPERED,
            "supersedes 必须是 64 位小写十六进制或 null",
        )
    note = payload.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_RECORD_NOTE_CHARS):
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "note 必须是字符串且不超过限长")
    reason = payload.get("reason_code")
    if reason is not None and (
        not isinstance(reason, str) or not _REASON_CODE_PATTERN.match(reason)
    ):
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "reason_code 必须是稳定原因码形态或 null")
    for field in ("decision_at", "generated_at"):
        if _as_moment(payload.get(field)) is None:
            _add(found, DecisionRecordCode.RECORD_TAMPERED, f"{field} 必须是带时区的 ISO8601")
    _verify_binding_block(payload.get("packet"), found=found)
    gate = payload.get("approve_gate")
    if not isinstance(gate, Mapping):
        _add(found, DecisionRecordCode.RECORD_TAMPERED, "记录缺 approve_gate 说明块")
    else:
        if gate.get("applies") is not (decision == HumanDecision.APPROVE.value):
            _add(
                found,
                DecisionRecordCode.RECORD_TAMPERED,
                "approve_gate.applies 与 decision 不一致",
            )
        for field in ("satisfied", "packet_submittable"):
            value = gate.get(field)
            if value is not None and not isinstance(value, bool):
                _add(
                    found,
                    DecisionRecordCode.RECORD_TAMPERED,
                    f"approve_gate.{field} 必须是布尔或 null",
                )
    semantics = payload.get("evidence_time_semantics")
    if not isinstance(semantics, Mapping) or semantics.get("contains_evidence_times") is not False:
        _add(
            found,
            DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION,
            "记录 evidence_time_semantics.contains_evidence_times 必须恒为 false",
        )
    forbidden = _forbidden_key_paths(payload)
    if forbidden:
        _add(
            found,
            DecisionRecordCode.EVIDENCE_TIME_SUBSTITUTION,
            "记录不允许出现证据时间字段：" + "、".join(forbidden[:8]),
        )
    return found


# ---------------------------------------------------------------------------
# 防伪核验（记录文档 ↔ **当前** packet；纯只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionRecordVerification:
    """一份决策记录与**当前** packet 的一致性核验结论（只读审计，**零写入**）。"""

    record_path: str
    packet_path: str
    record_id: str
    recomputed_record_id: str
    decision: str
    reviewer: str
    revision: int
    supersedes: str | None
    record_id_matches: bool
    packet_id_matches: bool
    content_sha256_matches: bool
    approve_still_valid: bool | None
    codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """是否**全部**一致（任一原因码都表示 fail-closed：不得把它当作有效决策凭据）。"""
        return not self.codes

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段恒为诚实取值）。"""
        return {
            "kind": f"{DECISION_RECORD_KIND}_verification",
            "report": DECISION_RECORD_REPORT_NAME,
            "schema_version": DECISION_RECORD_SCHEMA_VERSION,
            "record_path": _safe_path(self.record_path),
            "packet_path": _safe_path(self.packet_path),
            "record_id": self.record_id,
            "recomputed_record_id": self.recomputed_record_id,
            "record_id_matches": self.record_id_matches,
            "packet_id_matches": self.packet_id_matches,
            "content_sha256_matches": self.content_sha256_matches,
            "decision": self.decision,
            "human_decision": self.decision,
            "reviewer": _safe(self.reviewer, max_chars=MAX_RECORD_REVIEWER_CHARS),
            "revision": self.revision,
            "supersedes": self.supersedes,
            "approve_still_valid": self.approve_still_valid,
            "human_decision_recorded": True,
            "packet_verified": self.verified,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "human_gate_level": "L3",
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "verified": self.verified,
            "codes": list(self.codes),
            "details": list(self.details),
        }


def verify_decision_record(
    record_path: Path | str, packet_path: Path | str, *, moment: datetime
) -> DecisionRecordVerification:
    """把一份决策记录与其**当前** packet 逐项比对（**纯只读**；不一致 → 稳定原因码）。

    核验内容：①记录文档自身未被改写（**重新推导** ``record_id`` 与声明值一致、安全字段未被
    削弱、无证据时间键）；②记录绑定的 ``packet_id`` / 内容摘要与**当前** packet 一致；③当前
    packet 比记录更新 → ``PACKET_STALE``；④若记录是 ``approve``，判定其**是否仍然成立**
    （当前 packet 必须仍 ``submit_to_l3_human_gate=true`` 且内容未变）。

    Raises:
        DecisionRecordPathError: 记录或 packet 文件不存在。
        DecisionRecordStateError: 记录被改写 / packet 自身完整性核验不通过（含证据时间键）。
    """
    moment = _require_aware(moment, field_name="moment")
    payload = _load_record_document(record_path)
    current = load_decision_packet(packet_path, moment=moment)
    declared = str(payload["record_id"]).lower()
    recomputed = _record_id_from_document(payload)
    block = payload["packet"]
    bound_packet_id = str(block["packet_id"]).lower()
    bound_content = str(block["content_sha256"]).lower()
    codes: list[str] = []
    details: list[str] = []
    record_id_matches = declared == recomputed
    if not record_id_matches:
        codes.append(DecisionRecordCode.RECORD_ID_MISMATCH.value)
        details.append("记录文档被改写：重新推导的 record_id 与声明值不一致")
    packet_id_matches = bound_packet_id == current.packet_id
    if not packet_id_matches:
        codes.append(DecisionRecordCode.PACKET_ID_MISMATCH.value)
        details.append("记录绑定的 packet_id 与当前 packet 不一致")
    content_sha256_matches = bound_content == current.content_sha256
    if not content_sha256_matches:
        codes.append(DecisionRecordCode.PACKET_CONTENT_MISMATCH.value)
        details.append("记录绑定的 packet 内容摘要与当前 packet 不一致")
    bound_generated = _as_moment(block["generated_at"])
    if bound_generated is not None and current.generated_at > bound_generated:
        codes.append(DecisionRecordCode.PACKET_STALE.value)
        details.append("当前 packet 比记录绑定的 packet 更新（记录已 stale，需重新记录决策）")
    decision = str(payload["decision"])
    approve_still_valid: bool | None = None
    if decision == HumanDecision.APPROVE.value:
        approve_still_valid = bool(
            current.submit_to_l3_human_gate and packet_id_matches and content_sha256_matches
        )
        if not approve_still_valid:
            codes.append(DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value)
            details.append("当初的 approve 已不再成立：当前 packet 不可提交或内容已变化")
    revision = payload["revision"]
    return DecisionRecordVerification(
        record_path=str(record_path),
        packet_path=str(packet_path),
        record_id=declared,
        recomputed_record_id=recomputed,
        decision=decision,
        reviewer=str(payload["reviewer"]),
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else -1,
        supersedes=payload["supersedes"] if isinstance(payload["supersedes"], str) else None,
        record_id_matches=record_id_matches,
        packet_id_matches=packet_id_matches,
        content_sha256_matches=content_sha256_matches,
        approve_still_valid=approve_still_valid,
        codes=tuple(sorted(set(codes))),
        details=tuple(details),
    )


# ---------------------------------------------------------------------------
# 只读运行入口（默认零写入；只有显式 out_path 才原子落盘记录本身）
# ---------------------------------------------------------------------------
def _recheck_existing_record(out: Path, record: HumanDecisionRecord) -> None:
    """既有记录**绝不**被静默覆盖：同 ``record_id`` 幂等；否则必须显式 revision + supersedes。"""
    if not out.exists():
        return
    existing = _load_record_document(out)
    existing_id = str(existing["record_id"]).lower()
    if _record_id_from_document(existing) != existing_id:
        raise DecisionRecordStateError(
            f"{DecisionRecordCode.RECORD_TAMPERED.value}：既有记录 {_safe_path(out)} 的 record_id "
            "无法重新推导（文档被改写），fail-closed、零写入"
        )
    if existing_id == record.record_id:
        # 同一 packet + 同一人工决策：幂等重复记录（内容同一），允许原地重写
        return
    if record.supersedes is None:
        raise DecisionRecordStateError(
            f"{DecisionRecordCode.RECORD_CONFLICT.value}：既有记录（{existing_id[:16]}…）仍在，"
            "覆盖必须**显式**给出 --supersedes <record_id> 与更大的 --revision（绝不静默改写历史）"
        )
    if record.supersedes != existing_id:
        raise DecisionRecordStateError(
            f"{DecisionRecordCode.SUPERSEDES_MISMATCH.value}：--supersedes"
            f"（{record.supersedes[:16]}…）不是当前记录的 record_id（{existing_id[:16]}…）"
        )
    existing_revision = existing["revision"]
    if (
        isinstance(existing_revision, int)
        and not isinstance(existing_revision, bool)
        and record.revision <= existing_revision
    ):
        raise DecisionRecordStateError(
            f"{DecisionRecordCode.REVISION_INVALID.value}：revision（{record.revision}）必须**大于**"
            f"既有记录的 revision（{existing_revision}）"
        )


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise DecisionRecordWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；请检查输出目录可写性与"
            f"磁盘空间：{_safe_path(path)}"
        ) from exc


def run_decision_record(
    packet_path: Path | str,
    *,
    decision: str | HumanDecision,
    reviewer: object,
    moment: datetime,
    note: object = None,
    reason_code: object = None,
    revision: object = 1,
    supersedes: object = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> HumanDecisionRecord:
    """执行**一次**人工决策记录（默认只读；只有显式 ``out_path`` 才写记录文件）。

    安全语义：

    - packet 只读且**逐项**完整性核验（任一不一致 → :class:`DecisionRecordStateError`，零写入）；
    - ``approve`` 只在 packet 本身可提交时允许（否则 :class:`DecisionRecordNotSubmittableError`）；
    - 只有 ``out_path`` 才写**记录本身**：先取**单实例锁**，锁内**重新**读取 packet 防止
      preflight 与写入之间 packet 被改写（``PACKET_STALE``），再执行既有记录冲突检查，
      最后**原子**落盘；
    - 本函数**没有**任何 intake / commit / 数据库 / 网络调用，**绝不**修改
      ``PROJECT_STATE``、**绝不**解除 blocker、**绝不**切换 Phase。

    Raises:
        DecisionRecordArgumentError: 参数不合法（含时点缺时区、decision / reviewer 非法）。
        DecisionRecordNotSubmittableError: ``approve`` 落在不可提交的 packet 上（预期 BLOCKED）。
        DecisionRecordPathError: packet 不存在，或输出路径不可用（含指向 packet 本身的拒绝）。
        DecisionRecordStateError: packet / 既有记录损坏、被篡改、stale、冲突（fail-closed）。
        LockConflictError: 另一处记录写入正持有活动锁（**零写入**）。
        DecisionRecordWriteError: 记录原子写失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment")
    cleaned_decision = _clean_decision(decision)
    cleaned_revision, cleaned_supersedes = _clean_revision(revision, supersedes)
    packet = load_decision_packet(packet_path, moment=moment)
    record = build_decision_record(
        packet,
        decision=cleaned_decision,
        reviewer=reviewer,
        moment=moment,
        note=note,
        reason_code=reason_code,
        revision=cleaned_revision,
        supersedes=cleaned_supersedes,
    )
    if out_path is None:
        return record
    out = Path(out_path)
    if _same_path(out, Path(packet_path)):
        raise DecisionRecordPathError(
            f"{DecisionRecordCode.PACKET_OVERWRITE_REFUSED.value}：``--out`` 不得指向 packet 文件"
            f"本身（会破坏被绑定的审计产物）：{_safe_path(out)}"
        )
    if out.is_dir():
        raise DecisionRecordPathError(f"输出路径是目录、不是记录文件：{_safe_path(out)}")
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + DECISION_RECORD_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        # 锁内**重新**核验：防止 preflight 与写入之间 packet 被改写（TOCTOU）
        current = load_decision_packet(packet_path, moment=moment)
        if (
            current.content_sha256 != packet.content_sha256
            or current.packet_id != packet.packet_id
            or current.artifact_sha256 != packet.artifact_sha256
        ):
            raise DecisionRecordStateError(
                f"{DecisionRecordCode.PACKET_STALE.value}：packet 在决策记录写入前被改写"
                "（packet_id / 内容摘要 / 产物摘要不一致），fail-closed、零写入"
            )
        _recheck_existing_record(out, record)
        _atomic_write_json(out, record.to_dict(), what="L3 人工决策记录")
    return replace(record, written_path=_safe_path(out))


def render_decision_record_summary(record: HumanDecisionRecord) -> str:
    """渲染人类可读的决策记录摘要（脱敏；**不解除** blocker、**不代表**资格通过）。"""
    payload = record.to_dict()
    packet = payload["packet"]
    gate = payload["approve_gate"]
    note = record.note if record.note is not None else "—"
    lines: list[str] = [
        "# L3 人工决策记录（**人工决策审计层**，不是资格判定器，也不是 Phase 执行器）",
        "",
        f"> {DECISION_RECORD_NOTE}",
        "",
        "## 1. 人工决策",
        "",
        f"- `human_decision_recorded` = {str(payload['human_decision_recorded']).lower()}"
        f"（记录存在）；`human_decision` = `{record.human_decision}`",
        f"- reviewer：`{record.reviewer}`（{REVIEWER_KIND}；**不采集**任何凭据）",
        f"- reason_code：`{record.reason_code or '—'}`；note：{note}",
        f"- record_id（内容级）：`{record.record_id}`；revision：{record.revision}；"
        f"supersedes：`{record.supersedes or '—'}`",
        f"- 决策审计时点（**不是**证据时间）：{record.decision_at.isoformat()}",
        f"- 执行模式：`{DECISION_RECORD_EXECUTION_MODE}`；作用域：`{DECISION_SCOPE}`",
        "",
        "## 2. 被绑定的 GOLD-015 packet（已逐项核验）",
        "",
        f"- packet_id：`{packet['packet_id']}`；status：`{packet['status']}`",
        f"- 内容摘要：`{packet['content_sha256']}`；产物摘要：`{packet['artifact_sha256']}`",
        f"- `submit_to_l3_human_gate` = {str(packet['submit_to_l3_human_gate']).lower()}；"
        f"`evidence_ready_for_human_review` = "
        f"{str(packet['evidence_ready_for_human_review']).lower()}",
        f"- `receipt_verified` = {str(packet['receipt_verified']).lower()}；"
        f"`qualification_recheck_ready` = {str(packet['qualification_recheck_ready']).lower()}",
        f"- 完整性核验：**{len(payload['packet_verification']['checks_passed'])} 项全部通过**，"
        "`violations` = []",
        f"- `approve` 门禁：applies = {str(gate['applies']).lower()}；"
        f"satisfied = {gate['satisfied']}",
        "",
        "## 3. 语义（**互不蕴含**，本工具能力边界）",
        "",
        "- `human_decision_recorded` / `human_decision` / `packet_verified` 是三个**独立事实**；",
        "- `data_qualification_passed` / `phase_transition_allowed` / "
        "`phase_transition_executed` **恒为** false（**硬编码**）；",
        f"- `blocker_active` = true、`human_gate_required` = true、"
        f"`human_gate_level` = `{payload['human_gate_level']}`、"
        f"blocker = `{PHASE3_3_BLOCKER_CODE}`（**保持 BLOCKED**）；",
        "- 本记录**不具备**解除 blocker 或执行 Phase transition 的能力；",
        "",
        "## 4. 时间语义与后续人工动作",
        "",
        f"- {DECISION_RECORD_TIME_SEMANTICS_NOTE}",
        f"- {DECISION_RECORD_NEXT_STEP_NOTE}",
        "",
        "## 5. 说明",
        "",
        *[f"- {item}" for item in record.notes],
        "",
    ]
    return "\n".join(lines)
