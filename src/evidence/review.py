"""Evidence Inbox 人工复核决策与审计闭环（**GOLD-012**，纯本地 / 零网络 / 零数据库）。

GOLD-011 已交付**只读发现与预检**（:mod:`src.evidence.inbox` /
``scripts/evidence_inbox.py``）：候选 evidence 放进显式 ``--inbox-dir`` 后，系统能确定性发现、
算内容 SHA-256 与候选包指纹、做 manifest 与逐行预检。但它**只把候选交给人工**：
"谁看了、看的是哪一版内容、结论是什么、凭什么" 没有留下可审计的决策记录。

本模块补齐**纯本地的人工复核决策层**：

- **显式决策、受控词表**：只允许对**显式给出的 package fingerprint**（GOLD-011 的**内容级**
  指纹）记录三类决策之一 —— :class:`ReviewDecision` 的 ``APPROVE`` / ``REJECT`` /
  ``NEEDS_CHANGES``；原因码必须落在 :data:`REASON_CODES_BY_DECISION`（与决策匹配的受控词表），
  否则 fail-closed；
- **最小审计元数据**：``reviewer``（脱敏 / 非敏感表示）、``reviewed_at``（必须带时区）、
  ``reason_code``、可选非敏感 ``note``；**凭据类内容一律拒绝记录**（绝不擦一擦就落盘）；
- **approve 前置门禁（fail-closed）**：必须 ①该 fingerprint **当前仍出现在** inbox 扫描结果中、
  ②当前预检结论为 ``PREFLIGHT_PASS``、③**不是**模板 / 示例 / Mock；内容变化 → 新指纹
  （**旧批准绝不继承到新指纹**）；候选消失 / 预检不通过 / state 损坏 / 复核元数据与当前证据
  不一致 → 拒绝记录且**零写入**；
- **追加式审计 ledger**：``kind=evidence_inbox_review_ledger``，决策只 append、**绝不静默覆盖**；
  ``decision_id`` 确定性（只由指纹 / 决策 / revision / 原因码派生）；同一 fingerprint +
  **完全相同**的决策内容重复提交**幂等**（不新增记录）；任何差异都必须**显式**给出新
  ``revision`` 与 ``override``，否则 fail-closed 并保留**全部**历史（``supersedes`` 指向被取代的
  决策）；
- **原子写与并发安全**：只有显式 ``out_path`` 才写文件，且先取**单实例锁**
  （复用 GOLD-010 的 :class:`~src.evidence.readiness_runner.SingleInstanceLock`）再读 ledger；
  落盘复用 GOLD-009 的 :func:`~src.evidence.readiness_watch.atomic_write_text`（无残留 ``.tmp``）；
- **批准清单 ≠ 资格**：:func:`build_approved_intake_list` 只生成**脱敏**的
  ``approved-for-explicit-intake`` 清单，并且必须在**当前**扫描结果上**重新验证**
  （候选仍在、仍 ``PREFLIGHT_PASS``、摘要与复核时一致），否则列入 ``invalidated``
  （绝不因为"曾经批准过"就放行）；本模块**不存在**任何 intake / commit 调用，**绝不写数据库**、
  **绝不移动 / 删除 / 改写** inbox 内任何原始 evidence；
- **诚实**：所有 artifact 恒为 ``blocker_active=true`` / ``human_gate_required=true`` /
  ``data_qualification_passed=false`` / ``phase_transition_allowed=false``（**硬编码**）；
  人工批准**只是**人工预审通过，**不等于** data qualification PASS，**绝不**因为 approve 数量
  达到任何阈值而自动改变上述字段；``PHASE3_3_DATA`` **保持 BLOCKED**；
- **绝不把复核元数据当证据**：``reviewer`` / ``note`` / 文件名 / mtime / 复核时间 / "曾被人批准"
  **都不是** ``published_at`` / ``collected_at`` / ``effective_at`` / ``availability`` 证据；
  本模块产出的 JSON **只有**上述审计字段，绝不合成任何证据时间（见
  :data:`FORBIDDEN_EVIDENCE_FIELDS`）。
- **新 APPROVE 必须绑定 GOLD-028 材料级人工核验凭证**（GOLD-029）：``APPROVE`` 只允许在
  **显式**给出 ``--attestation <GOLD-028 凭证文件>`` 时记录，并且在记录前用
  :func:`~src.evidence.human_verification_attestation.verify_attestation` 把凭证与**当前**
  候选目录逐项重新绑定核验（package fingerprint / package 内容身份 / handoff 内容身份 /
  ``scope`` / ``preflight_pass`` / ``all_required_verified``）：任一不匹配（缺失 / 部分核验 /
  fingerprint mismatch / 漂移 / 被篡改）都 **fail-closed 且零写入**。批准记录里只保存
  **最小 attestation binding**（版本 + ``attestation_id`` + 内容摘要 + 绑定的 package / handoff
  身份 + ``scope`` + 完整性布尔），**绝不**保存凭证正文、凭据或任何证据时间；
- **负向决策不被阻塞**：``REJECT`` / ``NEEDS_CHANGES`` 仍然**不需要** attestation（便于人工
  驳回 / 要求整改的审计留痕）；
- **历史记录只读兼容**：旧 ledger（含**没有** attestation binding 的旧 ``APPROVE``）仍然可读、
  可审计、**绝不**被原地迁移或重写；但**缺失 binding 的 ``APPROVE`` 永不满足新门禁**，
  :func:`build_approved_intake_list` 会以稳定原因码 ``ATTESTATION_MISSING`` 把它列入
  ``invalidated``（新门禁只对**后续 / 当前**重新验证生效，绝不自动升级历史结论）。
  演进方式刻意保持**向后兼容只读**：ledger 文档 ``schema_version`` **不变**，新字段
  ``decisions[].attestation`` 是**可选**的、且**自带版本**（``binding_version`` +
  GOLD-028 ``attestation_schema_version`` / ``attestation_contract_version``），
  老读者忽略它即可；:func:`compute_decision_id` 的派生口径**不变**，因此历史
  ``decision_id`` 仍可原样重算与对账；
- **批准清单 / 下游链重新验证**：生成批准清单时再次用**当前** inbox / handoff 重新推导
  binding 绑定的身份（绑定候选包的材料结构内容身份 / ``scope``），任一漂移 → 稳定原因码
  ``ATTESTATION_STALE``（``ATTESTATION_INCOMPLETE`` 用于声明未完整核验的 binding），
  批准失效并**绝不放行**。

入口：``scripts/evidence_review.py``（``--inbox-dir`` / ``--decision`` / ``--fingerprint`` /
``--reviewer`` / ``--reason-code`` / ``--attestation``；``--out`` 是**唯一** ledger 写开关，
``--approved-out`` 才写批准清单）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common import hashing
from src.common.redaction import redact_secrets, safe_text, safe_url
from src.evidence import readiness_runner as runner
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.human_verification_attestation import (
    AttestationError,
    load_attestation_document,
    package_content_sha256,
    verify_attestation,
)
from src.evidence.inbox import (
    CandidatePackage,
    InboxError,
    InboxPreflightReport,
    InboxStatus,
    ensure_outside_inbox,
    scan_inbox,
)
from src.evidence.intake_handoff import IntakeHandoffDocument, load_intake_handoff
from src.evidence.readiness_watch import MAX_CODE_CHARS, atomic_write_text
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "APPROVED_KIND",
    "APPROVED_NOTE",
    "APPROVAL_SCOPE",
    "ATTESTATION_BINDING_KEYS",
    "ATTESTATION_BINDING_VERSION",
    "EXIT_CONFIG_ERROR",
    "EXIT_LOCK_CONFLICT",
    "EXIT_NO_DECISION",
    "EXIT_OK",
    "EXIT_STATE_INVALID",
    "EXIT_UNUSABLE",
    "FORBIDDEN_EVIDENCE_FIELDS",
    "LEDGER_FILE_NAME",
    "LEDGER_KIND",
    "LEDGER_LOCK_SUFFIX",
    "MAX_NOTE_CHARS",
    "MAX_REVIEWER_CHARS",
    "NEXT_STEP_NOTE",
    "PACKAGE_SUMMARY_KEYS",
    "REASON_CODES_BY_DECISION",
    "REVIEW_NOTE",
    "REVIEW_REPORT_KIND",
    "REVIEW_REPORT_NAME",
    "REVIEW_SCHEMA_VERSION",
    "ApprovedEntry",
    "ApprovedIntakeList",
    "DecidedReview",
    "InvalidatedApproval",
    "InvalidationReason",
    "ReviewArgumentError",
    "ReviewAttestationError",
    "ReviewConflictError",
    "ReviewDecision",
    "ReviewError",
    "ReviewLedger",
    "ReviewLedgerStateError",
    "ReviewLedgerWriteError",
    "ReviewPathError",
    "ReviewReasonCode",
    "ReviewRecord",
    "ReviewReport",
    "ReviewTargetError",
    "build_approved_intake_list",
    "compute_decision_id",
    "exit_code_for",
    "load_review_ledger",
    "record_review_decision",
    "render_review_summary",
    "run_review",
    "write_approved_intake_list",
    "write_review_ledger",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
REVIEW_SCHEMA_VERSION: Final[int] = 1
#: 复核报告标识 / 文档标识（稳定，供上游与定时器日志解析）
REVIEW_REPORT_NAME: Final[str] = "evidence_inbox_review"
REVIEW_REPORT_KIND: Final[str] = "evidence_inbox_review"
#: 追加式决策 ledger 的文档标识
LEDGER_KIND: Final[str] = "evidence_inbox_review_ledger"
#: 批准清单（approved-for-explicit-intake）的文档标识
APPROVED_KIND: Final[str] = "evidence_inbox_approved_for_explicit_intake"
#: ledger 的默认文件名（operator 放在显式工作目录里）
LEDGER_FILE_NAME: Final[str] = "inbox_review_ledger.json"
#: ledger 的单实例锁后缀（与 ``out_path`` 同级）
LEDGER_LOCK_SUFFIX: Final[str] = ".lock"
#: 复核元数据上限（审核标识 / note 都是"最小元数据"，不做日志或正文的容器）
MAX_REVIEWER_CHARS: Final[int] = 100
MAX_NOTE_CHARS: Final[int] = 300
#: 决策含义（机器可读；防止把"人工预审通过"读成"资格通过"）
APPROVAL_SCOPE: Final[str] = "human_pre_review_only_not_data_qualification"
#: 明确禁止出现在本模块产出里的证据时间字段（复核元数据不得冒充证据）
FORBIDDEN_EVIDENCE_FIELDS: Final[tuple[str, ...]] = (
    "published_at",
    "collected_at",
    "effective_at",
    "available_at",
    "availability_provenance",
    "availability_reference",
)
#: ledger 记录里保存的候选包摘要键（**不含**任何证据时间，也不含 mtime）
PACKAGE_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "package_dir",
    "evidence_type",
    "source",
    "authorization_reference",
    "status",
    "files",
    "rows",
    "acceptable_rows",
    "reason_codes",
)
#: 批准清单重新验证时**必须一致**的候选包字段（内容级一致性；不含目录名）
PACKAGE_COMPARED_KEYS: Final[tuple[str, ...]] = (
    "status",
    "evidence_type",
    "source",
    "files",
    "rows",
    "acceptable_rows",
)
#: **最小 attestation binding** 的版本（GOLD-029；字段增删必须同步升版本 + 更新测试与 README）
ATTESTATION_BINDING_VERSION: Final[int] = 1
#: 新的 ``APPROVE`` 记录里保存的**最小 attestation binding** 键集合（白名单）。
#:
#: 这里**只**放能够稳定重算 / 比对的身份字段：**绝不**保存凭证正文、材料备注、
#: ``reviewer`` / ``reviewed_at`` 等内容，也**绝不**保存任何证据时间
#: （``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at``）。
ATTESTATION_BINDING_KEYS: Final[tuple[str, ...]] = (
    "binding_version",
    "attestation_schema_version",
    "attestation_contract_version",
    "attestation_id",
    "attestation_document_sha256",
    "package_fingerprint",
    "package_content_sha256",
    "handoff_content_sha256",
    "scope",
    "all_required_verified",
)

#: 64 位小写十六进制（指纹 / 摘要 / decision_id 统一校验）
_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")

#: 固定说明：复核层只做审计与预审，不解除 blocker、不切 Phase、不自动 intake
REVIEW_NOTE: Final[str] = (
    "本工具只做**纯本地**的人工复核决策与审计记录：不联网、不写数据库、"
    "不移动 / 删除 / 改写任何原始 evidence、也不调用任何 intake / commit 路径；"
    "`APPROVE` **只表示人工预审通过**（可进入**显式** intake 队列），"
    "**不等于** data qualification PASS；`blocker_active` / `human_gate_required` 恒为 true，"
    "`data_qualification_passed` / `phase_transition_allowed` 恒为 false，"
    "`PHASE3_3_DATA` **保持 BLOCKED**。"
)
#: 批准清单的固定说明（机器可读字段的语义注释）
APPROVED_NOTE: Final[str] = (
    "批准清单仅列出**人工预审通过且当前仍与 inbox 实际内容一致**的候选包；"
    "它**不是**资格证明，**不会**解除 blocker，**不会**自动落库；"
    "真正的落库仍须 operator **显式**执行 `scripts/evidence_operator.py workflow --no-dry-run`，"
    "并以 `handoff` / `recheck` 复核后走 L3 人工 Gate。"
)
#: 最小操作路径提示（inbox 扫描 → 人工复核 → 批准清单 → 显式 intake）
NEXT_STEP_NOTE: Final[str] = (
    "最小路径：`scripts.evidence_inbox --inbox-dir <inbox> --out <pending>` 发现与预检 → "
    "本工具 `--decision approve --fingerprint <fp> --reviewer <id> --reason-code <code> "
    "--out <ledger>` 记录人工决策 → `--approved-out <list>` 生成脱敏批准清单 → "
    "人工按清单**显式**执行 "
    "`scripts.evidence_operator workflow --no-dry-run --scope <author|news> "
    "--input <pkg 内证据文件>` → `handoff` / `recheck` 复核。"
    "任何一步都不会自动解除 `PHASE3_3_DATA`。"
)


class ReviewDecision(StrEnum):
    """人工复核决策（只有三种；``APPROVE`` 也只是**人工预审通过**）。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_CHANGES = "NEEDS_CHANGES"


class ReviewReasonCode(StrEnum):
    """复核原因码（受控词表；必须与决策匹配，否则 fail-closed）。"""

    # ---- approve -----------------------------------------------------------
    APPROVED_FOR_EXPLICIT_INTAKE = "APPROVED_FOR_EXPLICIT_INTAKE"
    APPROVED_AUTHORIZATION_REVIEWED = "APPROVED_AUTHORIZATION_REVIEWED"
    APPROVED_TIME_SEMANTICS_REVIEWED = "APPROVED_TIME_SEMANTICS_REVIEWED"
    APPROVED_AVAILABILITY_REVIEWED = "APPROVED_AVAILABILITY_REVIEWED"
    # ---- reject ------------------------------------------------------------
    REJECTED_SYNTHETIC_OR_EXAMPLE = "REJECTED_SYNTHETIC_OR_EXAMPLE"
    REJECTED_AUTHORIZATION_INSUFFICIENT = "REJECTED_AUTHORIZATION_INSUFFICIENT"
    REJECTED_TIME_SEMANTICS_INSUFFICIENT = "REJECTED_TIME_SEMANTICS_INSUFFICIENT"
    REJECTED_AVAILABILITY_INSUFFICIENT = "REJECTED_AVAILABILITY_INSUFFICIENT"
    REJECTED_INTEGRITY_FAILED = "REJECTED_INTEGRITY_FAILED"
    REJECTED_SCOPE_UNSUPPORTED = "REJECTED_SCOPE_UNSUPPORTED"
    REJECTED_OTHER = "REJECTED_OTHER"
    # ---- needs_changes -----------------------------------------------------
    NEEDS_AUTHORIZATION_FIX = "NEEDS_AUTHORIZATION_FIX"
    NEEDS_TIME_SEMANTICS_FIX = "NEEDS_TIME_SEMANTICS_FIX"
    NEEDS_AVAILABILITY_FIX = "NEEDS_AVAILABILITY_FIX"
    NEEDS_MANIFEST_OR_FILES_FIX = "NEEDS_MANIFEST_OR_FILES_FIX"
    NEEDS_OTHER = "NEEDS_OTHER"


#: 每个决策**只允许**使用的原因码（跨决策使用 → 参数错误，fail-closed）
REASON_CODES_BY_DECISION: Final[Mapping[ReviewDecision, frozenset[str]]] = {
    ReviewDecision.APPROVE: frozenset(
        {
            ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value,
            ReviewReasonCode.APPROVED_AUTHORIZATION_REVIEWED.value,
            ReviewReasonCode.APPROVED_TIME_SEMANTICS_REVIEWED.value,
            ReviewReasonCode.APPROVED_AVAILABILITY_REVIEWED.value,
        }
    ),
    ReviewDecision.REJECT: frozenset(
        {
            ReviewReasonCode.REJECTED_SYNTHETIC_OR_EXAMPLE.value,
            ReviewReasonCode.REJECTED_AUTHORIZATION_INSUFFICIENT.value,
            ReviewReasonCode.REJECTED_TIME_SEMANTICS_INSUFFICIENT.value,
            ReviewReasonCode.REJECTED_AVAILABILITY_INSUFFICIENT.value,
            ReviewReasonCode.REJECTED_INTEGRITY_FAILED.value,
            ReviewReasonCode.REJECTED_SCOPE_UNSUPPORTED.value,
            ReviewReasonCode.REJECTED_OTHER.value,
        }
    ),
    ReviewDecision.NEEDS_CHANGES: frozenset(
        {
            ReviewReasonCode.NEEDS_AUTHORIZATION_FIX.value,
            ReviewReasonCode.NEEDS_TIME_SEMANTICS_FIX.value,
            ReviewReasonCode.NEEDS_AVAILABILITY_FIX.value,
            ReviewReasonCode.NEEDS_MANIFEST_OR_FILES_FIX.value,
            ReviewReasonCode.NEEDS_OTHER.value,
        }
    ),
}


class InvalidationReason(StrEnum):
    """批准失效原因（生成批准清单时**重新验证**得出的稳定原因码）。"""

    CANDIDATE_MISSING = "CANDIDATE_MISSING"
    PREFLIGHT_NOT_PASSING = "PREFLIGHT_NOT_PASSING"
    SYNTHETIC_EVIDENCE = "SYNTHETIC_EVIDENCE"
    EVIDENCE_INCONSISTENT = "EVIDENCE_INCONSISTENT"
    # ---- GOLD-029：材料级人工核验 attestation 门禁（任一即批准失效）----------
    #: 该 ``APPROVE`` **没有** attestation binding（历史 / 旧口径），永不满足新门禁
    ATTESTATION_MISSING = "ATTESTATION_MISSING"
    #: binding 声明的材料级核验**不完整**（``all_required_verified=false``）
    ATTESTATION_INCOMPLETE = "ATTESTATION_INCOMPLETE"
    #: binding 绑定的 package / handoff / scope 身份与**当前** inbox 不一致（漂移）
    ATTESTATION_STALE = "ATTESTATION_STALE"
    #: binding 结构被改写 / 与记录本身不一致（篡改）
    ATTESTATION_TAMPERED = "ATTESTATION_TAMPERED"


class ReviewError(RuntimeError):
    """复核层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class ReviewArgumentError(ReviewError):
    """参数 / 词表错误（决策、原因码、reviewer、note、时区、revision 组合等）。"""


class ReviewTargetError(ReviewError):
    """目标候选不满足记录条件（不存在 / 内容已变化 / 预检未通过 / 模板示例）。"""


class ReviewAttestationError(ReviewTargetError):
    """新 ``APPROVE`` 的 GOLD-028 材料级人工核验凭证缺失 / 漂移 / 被篡改。

    **fail-closed、零写入**；继承 :class:`ReviewTargetError` 以保证退出码语义不变
    （``4``：state 非法，绝不假设已落盘）。
    """

class ReviewConflictError(ReviewError):
    """与既有决策冲突且未显式给出新 ``revision`` + ``override``（**绝不静默覆盖**）。"""


class ReviewLedgerStateError(ReviewError):
    """ledger 损坏 / 被篡改 / 声称已解除 blocker（**fail-closed**，保留旧文件，零写入）。"""


class ReviewLedgerWriteError(ReviewError):
    """ledger 或批准清单原子写失败（**fail-closed**）。"""


class ReviewPathError(ReviewError):
    """inbox 目录不可用，或输出 artifact 与 inbox 目录冲突。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 决策已记录（或幂等重复）/ 批准清单非空
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数 / 词表 / 时区错误
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: ledger 损坏 / 决策冲突 / 目标候选不满足门禁（全部 fail-closed，零写入）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 只读运行且**没有任何**可进入显式 intake 的批准（预期 BLOCKED，不是故障）
EXIT_NO_DECISION: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一个复核 / 重扫正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_REVIEW_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (ReviewArgumentError, EXIT_CONFIG_ERROR),
    (ReviewLedgerStateError, EXIT_STATE_INVALID),
    (ReviewTargetError, EXIT_STATE_INVALID),
    (ReviewConflictError, EXIT_STATE_INVALID),
    (ReviewPathError, EXIT_UNUSABLE),
    (ReviewLedgerWriteError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _REVIEW_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（原因文本 / 来源名 / 引用共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径类展示：脱敏 + 截断（绝不回显凭据）。"""
    return _safe(value, max_chars=max_chars)


def _safe_reference(value: str, *, max_chars: int = 200) -> str:
    """引用类展示：URL 去掉 userinfo / query / fragment，再脱敏 + 截断。"""
    return safe_text(safe_url(value, max_chars=max_chars), max_chars=max_chars)


def _clean_review_text(raw: str, *, field_name: str, max_chars: int, required: bool) -> str:
    """校验并规范化复核元数据文本；**凭据类内容一律拒绝记录**（fail-closed）。

    这里**不**做"擦一擦再落盘"：复核元数据是最小审计字段，一旦疑似含凭据，
    正确做法是拒绝本次记录并让人工改掉，而不是把 ``***`` 写进审计历史。
    """
    text = str(raw).strip()
    if required and not text:
        raise ReviewArgumentError(f"{field_name} 不能为空（必须给出非敏感的 {field_name}）")
    if len(text) > max_chars:
        raise ReviewArgumentError(
            f"{field_name} 超过 {max_chars} 字符上限：复核元数据只放最小审计字段"
            "（不要粘贴日志、正文或大段说明）"
        )
    if text and redact_secrets(text) != text:
        raise ReviewArgumentError(
            f"{field_name} 疑似包含凭据：**拒绝记录**（fail-closed，绝不写进 ledger）"
        )
    return text


def _require_aware(moment: datetime, *, field_name: str) -> datetime:
    """要求带时区并统一到 UTC（禁止隐式时区）。"""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ReviewArgumentError(f"{field_name} 必须包含时区（禁止隐式时区）")
    return moment.astimezone(UTC)


def compute_decision_id(
    fingerprint: str,
    decision: ReviewDecision | str,
    revision: int,
    reason_code: str,
) -> str:
    """确定性 ``decision_id``：只由指纹 / 决策 / revision / 原因码派生。

    ⚠️ 刻意**不含** ``reviewed_at`` / ``reviewer`` / note：同一内容的同一决策无论何时由谁提交，
    id 都相同（便于幂等判定与外部对账）；任何差异都必须走**新 revision**（id 随之变化）。
    """
    payload = json.dumps(
        {
            "fingerprint": str(fingerprint),
            "decision": str(decision),
            "revision": int(revision),
            "reason_code": str(reason_code),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashing.sha256_text(payload)


def _package_summary(package: CandidatePackage) -> dict[str, Any]:
    """候选包的**最小脱敏摘要**（复核记录里保存它；**不含**任何证据时间 / mtime / 正文）。"""
    return {
        "package_dir": _safe_path(package.package_dir),
        "evidence_type": _safe(package.evidence_type, max_chars=40),
        "source": _safe(package.source, max_chars=100),
        "authorization_reference": _safe_reference(package.authorization_reference),
        "status": package.status.value,
        "files": len(package.files),
        "rows": package.rows,
        "acceptable_rows": package.acceptable_rows,
        "reason_codes": sorted({_safe(code, max_chars=80) for code in package.reason_codes}),
    }


def _sanitize_package_summary(raw: object) -> dict[str, Any]:
    """把 ledger 里的候选包摘要**规范化**为白名单形态（未知键一律丢弃）。"""
    summary: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return summary
    for key in PACKAGE_SUMMARY_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in {"files", "rows", "acceptable_rows"}:
            try:
                summary[key] = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        elif key == "reason_codes":
            if isinstance(value, Sequence) and not isinstance(value, str):
                summary[key] = sorted({_safe(item, max_chars=80) for item in value})
        elif key == "authorization_reference":
            summary[key] = _safe_reference(str(value))
        else:
            summary[key] = _safe(value, max_chars=100)
    return summary


def _package_matches_review(record_package: Mapping[str, Any], package: CandidatePackage) -> bool:
    """复核时记录的摘要与**当前**候选包是否一致（内容 / 状态不一致 → 批准失效）。"""
    current = _package_summary(package)
    return all(
        record_package.get(key) == current[key] for key in PACKAGE_COMPARED_KEYS
    )


def _known_scopes() -> frozenset[str]:
    """受控 scope 词表（**直接取自** ``evidence-intake-v1`` 契约，不另造词表）。"""
    return frozenset(member.value for member in EvidenceScope)


def _sanitize_attestation_binding(raw: object, *, index: int) -> dict[str, Any] | None:
    """把 ledger 里的 attestation binding **规范化**为白名单形态（被改写 → fail-closed）。

    - 键缺失 / ``None`` → 没有 binding（旧口径记录，仍可读，但**永不**满足新门禁）；
    - 出现**任何**未授权键 / 类型非法 / 摘要非法 / scope 不在受控词表 →
      :class:`ReviewLedgerStateError`（绝不"擦一擦"继续读，也绝不猜测缺失字段）。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ReviewLedgerStateError("review ledger attestation binding 必须是对象或 null")
    unknown = sorted(set(str(key) for key in raw) - set(ATTESTATION_BINDING_KEYS))
    if unknown:
        raise ReviewLedgerStateError(
            "review ledger attestation binding 含未授权字段："
            + "、".join(_safe(item, max_chars=40) for item in unknown)
        )
    missing = [key for key in ATTESTATION_BINDING_KEYS if key not in raw]
    if missing:
        raise ReviewLedgerStateError(
            "review ledger attestation binding 缺字段："
            + "、".join(sorted(missing))
        )
    binding_version = raw.get("binding_version")
    if binding_version != ATTESTATION_BINDING_VERSION or isinstance(binding_version, bool):
        raise ReviewLedgerStateError(
            "review ledger attestation binding.binding_version 不受支持："
            f"{binding_version!r}"
        )
    schema_version = raw.get("attestation_schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ReviewLedgerStateError(
            "review ledger attestation binding.attestation_schema_version 必须是整数"
        )
    contract_version = raw.get("attestation_contract_version")
    if not isinstance(contract_version, str) or not contract_version:
        raise ReviewLedgerStateError(
            "review ledger attestation binding.attestation_contract_version 必须是非空字符串"
        )
    digest_fields = (
        "attestation_id",
        "attestation_document_sha256",
        "package_fingerprint",
        "package_content_sha256",
        "handoff_content_sha256",
    )
    for field_name in digest_fields:
        if not _HEX64.match(str(raw.get(field_name) or "")):
            raise ReviewLedgerStateError(
                f"review ledger attestation binding.{field_name} 必须是 64 位小写十六进制"
            )
    scope = raw.get("scope")
    if not isinstance(scope, str) or scope not in _known_scopes():
        raise ReviewLedgerStateError(
            "review ledger attestation binding.scope 不在受控词表内"
        )
    complete = raw.get("all_required_verified")
    if not isinstance(complete, bool):
        raise ReviewLedgerStateError(
            "review ledger attestation binding.all_required_verified 必须是布尔"
        )
    return {
        "binding_version": ATTESTATION_BINDING_VERSION,
        "attestation_schema_version": schema_version,
        "attestation_contract_version": contract_version,
        "attestation_id": str(raw["attestation_id"]).lower(),
        "attestation_document_sha256": str(raw["attestation_document_sha256"]).lower(),
        "package_fingerprint": str(raw["package_fingerprint"]).lower(),
        "package_content_sha256": str(raw["package_content_sha256"]).lower(),
        "handoff_content_sha256": str(raw["handoff_content_sha256"]).lower(),
        "scope": scope,
        "all_required_verified": complete,
    }


# ---------------------------------------------------------------------------
# 决策记录 / 追加式 ledger
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """**一条**人工复核决策（追加式审计的最小单位；全部字段脱敏或受控）。"""

    fingerprint: str
    decision: str
    revision: int
    decision_id: str
    reviewer: str
    reviewed_at: str
    reason_code: str
    note: str = ""
    supersedes: str | None = None
    override: bool = False
    package: Mapping[str, Any] = field(default_factory=dict)
    #: GOLD-029 **最小 attestation binding**（只有 ``APPROVE`` 才可能非空；旧记录恒为 ``None``）
    attestation: Mapping[str, Any] | None = None

    @property
    def approval_scope(self) -> str:
        """决策含义（机器可读；防止把人工预审读成资格）。"""
        return APPROVAL_SCOPE

    @property
    def attestation_id(self) -> str | None:
        """binding 绑定的 GOLD-028 ``attestation_id``（没有 binding → ``None``）。"""
        if self.attestation is None:
            return None
        value = self.attestation.get("attestation_id")
        return None if value is None else str(value)

    def payload(self) -> tuple[str, str, str, str, str | None]:
        """幂等判定用的"决策内容"（决策 / 原因码 / note / reviewer / attestation 身份）。

        ⚠️ attestation 身份刻意只用**内容级** ``attestation_id``（不含审计时间）：同一 package
        派生出的同一凭证无论何时重新生成，id 都相同 → 重复提交仍然是**幂等**；
        任何**内容**差异（含换用另一份凭证 / 另一个 package）都必须走**新 revision**。
        """
        return (self.decision, self.reason_code, self.note, self.reviewer, self.attestation_id)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含任何证据时间字段）。"""
        return {
            "decision_id": self.decision_id,
            "fingerprint": self.fingerprint,
            "decision": self.decision,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "override": self.override,
            "reviewer": _safe(self.reviewer, max_chars=MAX_REVIEWER_CHARS),
            "reviewed_at": self.reviewed_at,
            "reason_code": self.reason_code,
            "note": _safe(self.note, max_chars=MAX_NOTE_CHARS),
            "approval_scope": APPROVAL_SCOPE,
            "package": dict(self.package),
            "attestation": None if self.attestation is None else dict(self.attestation),
        }


def _record_from_state(item: object, index: int) -> ReviewRecord:
    """从 ledger 条目恢复**一条**记录（逐字段严格校验；绝不猜测缺失字段）。"""
    if not isinstance(item, Mapping):
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] 必须是对象")
    fingerprint = str(item.get("fingerprint") or "")
    if not _HEX64.match(fingerprint):
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] fingerprint 非法")
    decision = str(item.get("decision") or "")
    if decision not in {member.value for member in ReviewDecision}:
        raise ReviewLedgerStateError(
            f"review ledger decisions[{index}] decision 非法：{decision!r}"
        )
    reason_code = str(item.get("reason_code") or "")
    if reason_code not in REASON_CODES_BY_DECISION[ReviewDecision(decision)]:
        raise ReviewLedgerStateError(
            f"review ledger decisions[{index}] reason_code 与决策不匹配：{reason_code!r}"
        )
    try:
        revision = int(item.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] revision 非法") from exc
    if revision < 1:
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] revision 必须 >= 1")
    reviewer = _safe(item.get("reviewer") or "", max_chars=MAX_REVIEWER_CHARS)
    if not reviewer:
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] reviewer 不能为空")
    reviewed_at = str(item.get("reviewed_at") or "")
    try:
        parsed_at = datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise ReviewLedgerStateError(
            f"review ledger decisions[{index}] reviewed_at 无法解析"
        ) from exc
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise ReviewLedgerStateError(
            f"review ledger decisions[{index}] reviewed_at 必须带时区"
        )
    raw_supersedes = item.get("supersedes")
    supersedes = None if raw_supersedes is None else str(raw_supersedes)
    if supersedes is not None and not _HEX64.match(supersedes):
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] supersedes 非法")
    override = bool(item.get("override", False))
    decision_id = str(item.get("decision_id") or "")
    if not _HEX64.match(decision_id):
        raise ReviewLedgerStateError(f"review ledger decisions[{index}] decision_id 非法")
    if decision_id != compute_decision_id(fingerprint, decision, revision, reason_code):
        raise ReviewLedgerStateError(
            f"review ledger decisions[{index}] decision_id 与记录内容不一致（疑似被篡改）"
        )
    attestation = _sanitize_attestation_binding(item.get("attestation"), index=index)
    if attestation is not None:
        if decision != ReviewDecision.APPROVE.value:
            raise ReviewLedgerStateError(
                f"review ledger decisions[{index}] 只有 APPROVE 才允许携带 attestation binding"
            )
        if attestation["package_fingerprint"] != fingerprint:
            raise ReviewLedgerStateError(
                f"review ledger decisions[{index}] attestation binding 绑定的 package 指纹"
                "与记录指纹不一致（疑似被篡改）"
            )
    return ReviewRecord(
        fingerprint=fingerprint,
        decision=decision,
        revision=revision,
        decision_id=decision_id,
        reviewer=reviewer,
        reviewed_at=parsed_at.astimezone(UTC).isoformat(),
        reason_code=reason_code,
        note=_safe(item.get("note") or "", max_chars=MAX_NOTE_CHARS),
        supersedes=supersedes,
        override=override,
        package=_sanitize_package_summary(item.get("package")),
        attestation=attestation,
    )




@dataclass(frozen=True, slots=True)
class ReviewLedger:
    """**追加式**人工复核决策 ledger（只 append、绝不覆盖；``out_path`` 才原子落盘）。"""

    generated_at: datetime
    records: tuple[ReviewRecord, ...] = ()

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return LEDGER_KIND

    def records_for(self, fingerprint: str) -> tuple[ReviewRecord, ...]:
        """某指纹的**全部**历史决策（按 append 顺序；审计留痕）。"""
        return tuple(item for item in self.records if item.fingerprint == fingerprint)

    def latest_for(self, fingerprint: str) -> ReviewRecord | None:
        """某指纹的**最新**决策（不存在返回 ``None``）。"""
        found = self.records_for(fingerprint)
        return max(found, key=lambda item: item.revision) if found else None

    def latest_decisions(self) -> tuple[ReviewRecord, ...]:
        """每个指纹的最新决策（按指纹排序：确定性输出）。"""
        latest: dict[str, ReviewRecord] = {}
        for record in self.records:
            current = latest.get(record.fingerprint)
            if current is None or record.revision > current.revision:
                latest[record.fingerprint] = record
        return tuple(latest[key] for key in sorted(latest))

    def with_record(self, record: ReviewRecord, *, moment: datetime) -> ReviewLedger:
        """append 一条新决策（返回**新** ledger；原对象不变）。"""
        return ReviewLedger(generated_at=moment, records=(*self.records, record))

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（四个安全字段**硬编码**）。"""
        return {
            "kind": LEDGER_KIND,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "append_only": True,
            "approval_scope": APPROVAL_SCOPE,
            "decision_count": len(self.records),
            "decisions": [record.to_dict() for record in self.records],
            "notes": [REVIEW_NOTE],
        }


    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReviewLedger:
        """从落盘字典恢复（**严格校验**；任何异常都 fail-closed）。

        Raises:
            ReviewLedgerStateError: 文档标识 / schema / 契约版本 / 安全字段 / 记录 /
                ``supersedes`` 链 / ``decision_id`` 任一不合法。
        """
        if payload.get("kind") != LEDGER_KIND:
            raise ReviewLedgerStateError("state 文件不是 evidence inbox review ledger")
        if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ReviewLedgerStateError(
                f"review ledger schema_version 不受支持：{payload.get('schema_version')!r}"
            )
        contract = payload.get("contract_version")
        if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
            raise ReviewLedgerStateError(
                f"review ledger contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
            )
        for field_name, expected in (
            ("blocker_active", True),
            ("human_gate_required", True),
            ("data_qualification_passed", False),
            ("phase_transition_allowed", False),
        ):
            if field_name in payload and payload[field_name] is not expected:
                raise ReviewLedgerStateError(
                    f"review ledger 声称 {field_name}={payload[field_name]!r}："
                    "安全字段不可被 state 改写（fail-closed）"
                )
        raw_records = payload.get("decisions")
        if not isinstance(raw_records, list):
            raise ReviewLedgerStateError("review ledger decisions 必须是数组")
        records: list[ReviewRecord] = []
        seen_ids: set[str] = set()
        by_fingerprint: dict[str, list[ReviewRecord]] = {}
        for index, item in enumerate(raw_records, start=1):
            record = _record_from_state(item, index)
            if record.decision_id in seen_ids:
                raise ReviewLedgerStateError(f"review ledger decisions[{index}] decision_id 重复")
            seen_ids.add(record.decision_id)
            by_fingerprint.setdefault(record.fingerprint, []).append(record)
            records.append(record)
        for fingerprint, group in by_fingerprint.items():
            previous: ReviewRecord | None = None
            for position, record in enumerate(group, start=1):
                if record.revision != position:
                    raise ReviewLedgerStateError(
                        f"review ledger 指纹 {fingerprint[:16]} 的 revision 必须从 1 连续递增"
                    )
                expected_supersedes = previous.decision_id if previous is not None else None
                if record.supersedes != expected_supersedes:
                    raise ReviewLedgerStateError(
                        f"review ledger 指纹 {fingerprint[:16]} 的 supersedes 链断裂"
                    )
                if record.override is not (record.supersedes is not None):
                    raise ReviewLedgerStateError(
                        f"review ledger 指纹 {fingerprint[:16]} 的 override 与 supersedes 不一致"
                    )
                previous = record
        try:
            generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        except (KeyError, ValueError) as exc:
            raise ReviewLedgerStateError("review ledger generated_at 无法解析") from exc
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ReviewLedgerStateError("review ledger generated_at 必须带时区")
        return cls(generated_at=generated_at, records=tuple(records))


# ---------------------------------------------------------------------------
# 决策记录（append-only / 幂等 / 冲突必须显式 revision）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecidedReview:
    """一次决策提交的结果（``idempotent=True`` 表示既有记录**完全相同**、未新增）。"""

    record: ReviewRecord
    ledger: ReviewLedger
    idempotent: bool

    @property
    def created(self) -> bool:
        """是否**新增**了一条审计记录。"""
        return not self.idempotent

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "created": self.created,
            "idempotent": self.idempotent,
            "ledger_decision_count": len(self.ledger.records),
        }


def _coerce_decision(decision: ReviewDecision | str) -> ReviewDecision:
    """把入参规范化为 :class:`ReviewDecision`（未知值 → 参数错误）。"""
    if isinstance(decision, ReviewDecision):
        return decision
    try:
        return ReviewDecision(str(decision).strip().upper())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in ReviewDecision)
        raise ReviewArgumentError(f"decision 必须是 {allowed} 之一") from exc


def _require_fingerprint(fingerprint: str) -> str:
    """指纹必须是 GOLD-011 的 64 位小写十六进制**内容级**指纹（不接受文件名等替代物）。"""
    text = str(fingerprint).strip().lower()
    if not _HEX64.match(text):
        raise ReviewArgumentError(
            "fingerprint 必须是 64 位小写十六进制（GOLD-011 的**内容级**候选包指纹；"
            "文件名 / mtime / 扫描时间都不是指纹）"
        )
    return text


def _package_for(report: InboxPreflightReport, fingerprint: str) -> CandidatePackage | None:
    """在当前扫描结果中按指纹取候选包（不存在返回 ``None``）。"""
    for package in report.packages:
        if package.fingerprint == fingerprint:
            return package
    return None

def _attestation_document_sha256(payload: Mapping[str, Any]) -> str:
    """凭证**文档**的内容摘要（canonical JSON；把"这一份"凭证稳定绑定进 ledger）。"""
    return hashing.sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def build_attestation_binding(
    attestation_path: Path | str,
    *,
    fingerprint: str,
    inbox_dir: Path | str,
    moment: datetime,
) -> dict[str, Any]:
    """校验 GOLD-028 材料级人工核验凭证并压缩为**最小 binding**（纯只读）。

    校验（**全部 fail-closed**）：

    1. 凭证文档自身完整性（重新推导 ``attestation_id``、安全字段未被削弱、计数 / 合取自洽、
       无证据时间键）—— 被改写 → :class:`ReviewAttestationError`（``ATTESTATION_TAMPERED``）；
    2. 凭证与**当前**候选目录逐项重新绑定（fingerprint / package 内容身份 / handoff 内容身份 /
       ``scope`` / ``preflight_pass``）—— 任一不一致 → ``ATTESTATION_STALE``；
    3. 凭证绑定的 package 必须**正是本次 APPROVE 的目标**（绑错 package → ``ATTESTATION_STALE``）；
    4. ``all_required_verified`` 必须为 ``true``（部分核验 → ``ATTESTATION_INCOMPLETE``）。

    Raises:
        ReviewAttestationError: 缺失 / 不可读 / 被篡改 / 漂移 / 部分核验 / 绑错 package。
    """
    moment = _require_aware(moment, field_name="moment")
    target = _require_fingerprint(fingerprint)
    try:
        payload = load_attestation_document(attestation_path)
    except AttestationError as exc:
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_TAMPERED.value}：材料级人工核验凭证缺失 / 不可读 / "
            f"被改写（APPROVE fail-closed、零写入）：{_safe(str(exc), max_chars=300)}"
        ) from exc
    try:
        verification = verify_attestation(attestation_path, inbox_dir, moment=moment)
    except AttestationError as exc:
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_TAMPERED.value}：材料级人工核验凭证无法与当前候选"
            f"目录重新绑定（APPROVE fail-closed、零写入）：{_safe(str(exc), max_chars=300)}"
        ) from exc
    if not verification.verified:
        codes = "、".join(verification.codes) or "UNKNOWN"
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_STALE.value}：凭证与**当前**候选包不再一致"
            f"（稳定原因码：{codes}）；陈旧 / 漂移的凭证**永远**不能支撑 APPROVE（fail-closed）"
        )
    if verification.package_fingerprint != target:
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_STALE.value}：凭证绑定的 package fingerprint"
            f"（{verification.package_fingerprint[:16]}…）不是本次 APPROVE 的目标"
            f"（{target[:16]}…）：绑错 package 一律 fail-closed、零写入"
        )
    if not verification.all_required_verified:
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_INCOMPLETE.value}：凭证的 "
            "all_required_verified=false（材料级人工核验未完成 / 部分核验）："
            "不完整的人工核验**永不**满足 APPROVE 门禁（fail-closed）"
        )
    package_block = payload.get("package")
    schema_version = payload.get("schema_version")
    contract_version = payload.get("contract_version")
    if (
        not isinstance(package_block, Mapping)
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or not isinstance(contract_version, str)
        or not contract_version
    ):
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_TAMPERED.value}：凭证缺少可绑定的版本 / package 段"
            "（fail-closed、零写入）"
        )
    binding: dict[str, Any] = {
        "binding_version": ATTESTATION_BINDING_VERSION,
        "attestation_schema_version": schema_version,
        "attestation_contract_version": contract_version,
        "attestation_id": str(payload["attestation_id"]).lower(),
        "attestation_document_sha256": _attestation_document_sha256(payload),
        "package_fingerprint": str(package_block["fingerprint"]).lower(),
        "package_content_sha256": str(package_block["package_content_sha256"]).lower(),
        "handoff_content_sha256": str(package_block["handoff_content_sha256"]).lower(),
        "scope": str(package_block["scope"]),
        "all_required_verified": True,
    }
    if binding["package_fingerprint"] != target or binding["scope"] not in _known_scopes():
        raise ReviewAttestationError(
            f"{InvalidationReason.ATTESTATION_TAMPERED.value}：凭证 binding 与本次 APPROVE 目标"
            "不自洽（fail-closed、零写入）"
        )
    return binding


def record_review_decision(
    report: InboxPreflightReport,
    ledger: ReviewLedger | None,
    *,
    moment: datetime,
    decision: ReviewDecision | str,
    fingerprint: str,
    reviewer: str,
    reason_code: str,
    note: str = "",
    reviewed_at: datetime | None = None,
    revision: int | None = None,
    override: bool = False,
    attestation_path: Path | str | None = None,
) -> DecidedReview:
    """记录一次人工复核决策（**纯函数**：不写文件、不联网、不碰原始 evidence）。

    门禁（全部 fail-closed，绝不"先写后判"）：

    - ``decision`` / ``reason_code`` 必须匹配受控词表；``reviewer`` 必填且不得含凭据；
    - ``fingerprint`` 必须**当前**仍出现在 ``report``（inbox 扫描结果）中；
      **内容变化 → 新指纹**，旧批准绝不继承；
    - ``APPROVE`` 额外要求该候选当前 ``PREFLIGHT_PASS`` 且**不是**模板 / 示例 / Mock；
    - ``APPROVE`` **还**必须显式给出 GOLD-028 材料级人工核验凭证（``attestation_path``）并通过
      :func:`build_attestation_binding`（当前 package fingerprint / 内容身份 / handoff 内容身份 /
      ``scope`` / ``preflight_pass`` / ``all_required_verified`` 逐项复核）—— 缺失 / 陈旧 / 漂移 /
      被篡改 / 部分核验一律 :class:`ReviewAttestationError`（**fail-closed、零写入**）；
      ``REJECT`` / ``NEEDS_CHANGES`` **不**需要凭证；
    - 与既有记录**完全相同**（决策 / 原因码 / note / reviewer / attestation 身份）→ **幂等**
      （不新增记录）；
    - 任何差异都必须显式给出 ``revision = 既有 revision + 1`` 且 ``override=True``，
      否则 :class:`ReviewConflictError`（**绝不静默覆盖**，历史全部保留）。

    Raises:
        ReviewArgumentError: 词表 / 指纹 / 元数据 / 时区 / revision 组合错误。
        ReviewTargetError: 目标候选不存在 / 预检未通过 / 是模板示例。
        ReviewAttestationError: ``APPROVE`` 缺少 / 漂移 / 被篡改 / 不完整的 attestation。
        ReviewConflictError: 与既有决策冲突且未显式 revision + override。
    """
    moment = _require_aware(moment, field_name="moment")
    target = _require_fingerprint(fingerprint)
    coerced = _coerce_decision(decision)
    code = str(reason_code).strip().upper()
    if code not in REASON_CODES_BY_DECISION[coerced]:
        allowed = ", ".join(sorted(REASON_CODES_BY_DECISION[coerced]))
        raise ReviewArgumentError(
            f"reason_code 必须与决策 {coerced.value} 匹配（受控词表）：{allowed}"
        )
    clean_reviewer = _clean_review_text(
        reviewer, field_name="reviewer", max_chars=MAX_REVIEWER_CHARS, required=True
    )
    clean_note = _clean_review_text(
        note, field_name="note", max_chars=MAX_NOTE_CHARS, required=False
    )
    reviewed = (
        moment
        if reviewed_at is None
        else _require_aware(reviewed_at, field_name="reviewed_at")
    )
    if reviewed > moment:
        raise ReviewArgumentError(
            "reviewed_at 不得晚于审计时点（复核时间必须是已经发生的时刻；"
            "它**不是**任何证据时间）"
        )
    package = _package_for(report, target)
    if package is None:
        raise ReviewTargetError(
            f"候选包指纹 {target[:16]}… 当前不在 inbox 扫描结果中："
            "内容已变化（→ 新指纹）/ 已被移除 / 或从未被发现；"
            "**旧批准绝不继承到新指纹**（fail-closed）"
        )
    if coerced is ReviewDecision.APPROVE:
        if package.synthetic:
            raise ReviewTargetError(
                f"候选包指纹 {target[:16]}… 带显式示例 / 合成 / Mock 标记："
                "**永远不能**被批准为真实资格证据（fail-closed）"
            )
        if package.status is not InboxStatus.PREFLIGHT_PASS:
            codes = ", ".join(package.reason_codes) if package.reason_codes else "未知隔离原因"
            raise ReviewTargetError(
                f"候选包指纹 {target[:16]}… 当前预检结论为 {package.status.value}"
                f"（原因码：{codes}）：不满足 approve 门禁（fail-closed）"
            )
        if attestation_path is None:
            raise ReviewAttestationError(
                f"{InvalidationReason.ATTESTATION_MISSING.value}：新的 APPROVE 必须显式提供 "
                "GOLD-028 材料级人工核验凭证（`--attestation <凭证文件>`）："
                "缺少凭证的批准一律 fail-closed、零写入（历史 APPROVE 不会被追溯升级）"
            )
        attestation = build_attestation_binding(
            attestation_path,
            fingerprint=target,
            inbox_dir=report.inbox_dir,
            moment=moment,
        )
    else:
        attestation = None
    base = ledger if ledger is not None else ReviewLedger(generated_at=moment)
    existing = base.latest_for(target)
    payload = (
        coerced.value,
        code,
        clean_note,
        clean_reviewer,
        None if attestation is None else str(attestation["attestation_id"]),
    )
    if existing is not None and existing.payload() == payload:
        # 同一指纹 + 完全相同的决策内容 → 幂等（不新增记录、不改写历史）
        return DecidedReview(record=existing, ledger=base, idempotent=True)
    if existing is None:
        if revision not in (None, 1):
            raise ReviewArgumentError("首次决策的 revision 只能是 1（或省略）")
        if override:
            raise ReviewArgumentError("首次决策不接受 override（没有可取代的历史记录）")
        new_revision = 1
        supersedes: str | None = None
    else:
        if revision is None:
            raise ReviewConflictError(
                f"指纹 {target[:16]}… 已有决策 {existing.decision}"
                f"（revision {existing.revision}，decision_id {existing.decision_id[:16]}…）："
                f"如需改变结论必须**显式**给出 `--revision {existing.revision + 1}` "
                "与 `--override`；本工具**绝不静默覆盖**任何审计历史"
            )
        if not override:
            raise ReviewConflictError(
                f"指纹 {target[:16]}… 已有决策 {existing.decision}，新的 revision "
                f"{revision} 必须同时给出 `--override`（显式承认这是一次推翻），"
                "历史记录全部保留"
            )
        if revision != existing.revision + 1:
            raise ReviewConflictError(
                f"新 revision 必须是 {existing.revision + 1}（既有 revision {existing.revision}），"
                f"收到 {revision}：拒绝跳号或回退（fail-closed）"
            )
        new_revision = revision
        supersedes = existing.decision_id
    record = ReviewRecord(
        fingerprint=target,
        decision=coerced.value,
        revision=new_revision,
        decision_id=compute_decision_id(target, coerced, new_revision, code),
        reviewer=clean_reviewer,
        reviewed_at=reviewed.isoformat(),
        reason_code=code,
        note=clean_note,
        supersedes=supersedes,
        override=supersedes is not None,
        package=_package_summary(package),
        attestation=attestation,
    )
    return DecidedReview(
        record=record, ledger=base.with_record(record, moment=moment), idempotent=False
    )


# ---------------------------------------------------------------------------
# 批准清单（approved-for-explicit-intake）：生成前必须**重新验证**
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ApprovedEntry:
    """一条**当前仍然成立**的批准（人工预审通过 + 内容与复核时一致）。"""

    fingerprint: str
    decision_id: str
    approved_at: str
    reviewer: str
    reason_code: str
    package_dir: str
    evidence_type: str
    source: str
    evidence_files: tuple[str, ...]
    rows: int
    acceptable_rows: int

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；不含正文与任何证据时间）。"""
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "approved_at": self.approved_at,
            "reviewer": _safe(self.reviewer, max_chars=MAX_REVIEWER_CHARS),
            "reason_code": self.reason_code,
            "package_dir": _safe_path(self.package_dir),
            "evidence_type": _safe(self.evidence_type, max_chars=40),
            "source": _safe(self.source, max_chars=100),
            "evidence_files": [_safe_path(name) for name in self.evidence_files],
            "rows": self.rows,
            "acceptable_rows": self.acceptable_rows,
            "approval_scope": APPROVAL_SCOPE,
            "requires_explicit_intake": True,
        }


@dataclass(frozen=True, slots=True)
class InvalidatedApproval:
    """曾经 ``APPROVE`` 但**当前不再成立**的批准（绝不静默放行）。"""

    fingerprint: str
    decision_id: str
    reason_code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "decision_id": self.decision_id,
            "reason_code": self.reason_code,
            "detail": _safe(self.detail, max_chars=300),
        }


@dataclass(frozen=True, slots=True)
class ApprovedIntakeList:
    """**脱敏**的 approved-for-explicit-intake 清单（≠ 资格；绝不自动 intake）。"""

    generated_at: datetime
    approved: tuple[ApprovedEntry, ...] = ()
    invalidated: tuple[InvalidatedApproval, ...] = ()
    decision_counts: tuple[tuple[str, int], ...] = ()
    notes: tuple[str, ...] = (REVIEW_NOTE, APPROVED_NOTE)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return APPROVED_KIND

    @property
    def has_approved(self) -> bool:
        """是否存在可进入**显式** intake 的批准（仍需人工执行 intake）。"""
        return bool(self.approved)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（四个安全字段**硬编码**，与 approve 数量无关）。"""
        return {
            "kind": APPROVED_KIND,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "approval_scope": APPROVAL_SCOPE,
            "approved_count": len(self.approved),
            "invalidated_count": len(self.invalidated),
            "approved": [item.to_dict() for item in self.approved],
            "invalidated": [item.to_dict() for item in self.invalidated],
            "decision_counts": {code: count for code, count in self.decision_counts},
            "next_step": NEXT_STEP_NOTE,
            "notes": list(self.notes),
        }




def _invalidation_reason(package: CandidatePackage) -> InvalidationReason | None:
    """候选包**当前**是否仍满足「被批准」的前提（不满足则给出稳定失效原因）。"""
    if package.synthetic:
        return InvalidationReason.SYNTHETIC_EVIDENCE
    if package.status is not InboxStatus.PREFLIGHT_PASS:
        return InvalidationReason.PREFLIGHT_NOT_PASSING
    return None


def _attestation_binding_invalidation(
    record: ReviewRecord,
    report: InboxPreflightReport,
    *,
    moment: datetime,
) -> tuple[str, str] | None:
    """对一条 ``APPROVE`` **重新验证** GOLD-029 attestation binding（只读）。

    用**当前** inbox / intake handoff 重新推导 binding 里绑定的身份，并逐一比对：

    - 没有 binding（历史 / 旧口径 ``APPROVE``）→ ``ATTESTATION_MISSING``；
    - binding 声明未完整核验 → ``ATTESTATION_INCOMPLETE``；
    - binding 绑定的指纹与记录不一致 → ``ATTESTATION_TAMPERED``；
    - 绑定的候选包在当前 handoff 中缺失 / 其**材料结构内容身份**或 ``scope`` 与 binding
      不一致 → ``ATTESTATION_STALE``（package 漂移即批准失效）。

    说明：binding 里同时保存了 ``handoff_content_sha256``（整个 handoff 快照的内容身份），
    它由 **APPROVE 门禁**在凭证文件在场时逐项复核（:func:`build_attestation_binding`）；
    批准清单 / 后续链的复核则以**该候选包自身**的材料结构内容身份为准，避免"新增一个
    无关候选包"就静默作废既有批准（那不是被批准材料的漂移）。

    返回 ``(稳定原因码, 已脱敏说明)``；全部一致 → ``None``（批准仍然成立）。
    """
    binding = record.attestation
    if binding is None:
        return (
            InvalidationReason.ATTESTATION_MISSING.value,
            "该 APPROVE 没有 GOLD-028 材料级人工核验凭证绑定（历史 / 旧口径记录）："
            "新门禁**不承认**缺失 binding 的批准，必须重新人工核验并重新批准",
        )
    if not binding["all_required_verified"]:
        return (
            InvalidationReason.ATTESTATION_INCOMPLETE.value,
            "binding 声明的材料级人工核验不完整（all_required_verified=false）：批准失效",
        )
    if binding["package_fingerprint"] != record.fingerprint:
        return (
            InvalidationReason.ATTESTATION_TAMPERED.value,
            "binding 绑定的 package 指纹与批准记录指纹不一致（疑似被篡改）：fail-closed",
        )
    try:
        handoff: IntakeHandoffDocument = load_intake_handoff(report.inbox_dir, as_of=moment)
    except (OSError, ValueError, InboxError) as exc:
        return (
            InvalidationReason.ATTESTATION_STALE.value,
            f"无法重新推导当前 intake handoff / manifest（{type(exc).__name__}）："
            "binding 无法复核，批准失效",
        )
    current = next(
        (item for item in handoff.packages if item.fingerprint == record.fingerprint),
        None,
    )
    if current is None:
        return (
            InvalidationReason.ATTESTATION_STALE.value,
            "当前 handoff 中已不存在该 fingerprint 的候选包（package 漂移）：批准失效",
        )
    if package_content_sha256(current) != binding["package_content_sha256"]:
        return (
            InvalidationReason.ATTESTATION_STALE.value,
            "候选包材料结构内容身份与 binding 不一致（package 内容漂移）：批准失效",
        )
    if current.scope != binding["scope"]:
        return (
            InvalidationReason.ATTESTATION_STALE.value,
            "候选包 scope 与 binding 不一致：批准失效",
        )
    return None


def build_approved_intake_list(
    report: InboxPreflightReport, ledger: ReviewLedger, *, moment: datetime
) -> ApprovedIntakeList:
    """在当前扫描结果上**重新验证**每条 ``APPROVE``，产出脱敏批准清单。

    只有**同时**满足下列条件的指纹才进入清单：最新决策为 ``APPROVE``、
    该指纹**当前仍在**扫描结果中、当前仍 ``PREFLIGHT_PASS``、不是模板 / 示例 / Mock、
    复核时记录的候选包摘要与当前一致（状态 / 类型 / 来源 / 文件数 / 行数），
    并且 GOLD-029 attestation binding 与**当前** inbox / handoff **重新验证一致**
    （绑定的候选包材料结构内容身份 / ``scope`` 任一漂移 → 批准失效）。
    其余 ``APPROVE`` 一律列入 ``invalidated``（**绝不因为「曾经批准过」就放行**）。

    Raises:
        ReviewArgumentError: ``moment`` 未带时区。
    """
    moment = _require_aware(moment, field_name="moment")
    packages = {item.fingerprint: item for item in report.packages}
    approved: list[ApprovedEntry] = []
    invalidated: list[InvalidatedApproval] = []
    counts: Counter[str] = Counter()
    for record in ledger.latest_decisions():
        counts[record.decision] += 1
        if record.decision != ReviewDecision.APPROVE.value:
            continue
        package = packages.get(record.fingerprint)
        if package is None:
            invalidated.append(
                InvalidatedApproval(
                    fingerprint=record.fingerprint,
                    decision_id=record.decision_id,
                    reason_code=InvalidationReason.CANDIDATE_MISSING.value,
                    detail=(
                        "批准的候选包当前不在 inbox 扫描结果中（内容已变化 → 新指纹，"
                        "或已被移除）：旧批准不继承"
                    ),
                )
            )
            continue
        reason = _invalidation_reason(package)
        if reason is not None:
            invalidated.append(
                InvalidatedApproval(
                    fingerprint=record.fingerprint,
                    decision_id=record.decision_id,
                    reason_code=reason.value,
                    detail=(
                        f"当前预检结论为 {package.status.value}；批准的候选包不再满足"
                        "「非合成 + 预检通过」前提，必须重新人工复核"
                    ),
                )
            )
            continue
        if not _package_matches_review(record.package, package):
            invalidated.append(
                InvalidatedApproval(
                    fingerprint=record.fingerprint,
                    decision_id=record.decision_id,
                    reason_code=InvalidationReason.EVIDENCE_INCONSISTENT.value,
                    detail=(
                        "候选包摘要与复核时记录不一致（状态 / 类型 / 来源 / 文件数 / 行数）："
                        "必须重新人工复核"
                    ),
                )
            )
            continue
        binding_problem = _attestation_binding_invalidation(record, report, moment=moment)
        if binding_problem is not None:
            binding_code, binding_detail = binding_problem
            invalidated.append(
                InvalidatedApproval(
                    fingerprint=record.fingerprint,
                    decision_id=record.decision_id,
                    reason_code=binding_code,
                    detail=binding_detail,
                )
            )
            continue
        approved.append(
            ApprovedEntry(
                fingerprint=record.fingerprint,
                decision_id=record.decision_id,
                approved_at=record.reviewed_at,
                reviewer=record.reviewer,
                reason_code=record.reason_code,
                package_dir=_safe_path(package.package_dir),
                evidence_type=_safe(package.evidence_type, max_chars=40),
                source=_safe(package.source, max_chars=100),
                evidence_files=tuple(_safe_path(item.path) for item in package.files),
                rows=package.rows,
                acceptable_rows=package.acceptable_rows,
            )
        )
    return ApprovedIntakeList(
        generated_at=moment,
        approved=tuple(approved),
        invalidated=tuple(invalidated),
        decision_counts=tuple(sorted(counts.items())),
    )

# ---------------------------------------------------------------------------
# 落盘 / 单次运行 / 渲染
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReviewReport:
    """一次复核运行的结果（决策记录 + ledger 摘要 + 脱敏批准清单）。"""

    generated_at: datetime
    inbox_dir: Path
    ledger: ReviewLedger
    approved_list: ApprovedIntakeList
    decision: ReviewRecord | None = None
    idempotent: bool = False
    written_ledger: str | None = None
    written_approved: str | None = None
    notes: tuple[str, ...] = (REVIEW_NOTE,)

    @property
    def action(self) -> str:
        """本次动作（稳定字符串；只读运行 = ``LIST_ONLY``）。"""
        if self.decision is None:
            return "LIST_ONLY"
        return "DECISION_IDEMPOTENT" if self.idempotent else "DECISION_RECORDED"

    @property
    def has_decision(self) -> bool:
        """本次是否提交了决策（幂等重复也算提交过）。"""
        return self.decision is not None

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；四个安全字段**硬编码**）。"""
        decisions = self.ledger.latest_decisions()
        return {
            "kind": REVIEW_REPORT_KIND,
            "report": REVIEW_REPORT_NAME,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "inbox_dir": _safe_path(self.inbox_dir),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "requires_human_action": True,
            "approval_scope": APPROVAL_SCOPE,
            "action": self.action,
            "idempotent": self.idempotent,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "counts": {
                "ledger_decisions": len(self.ledger.records),
                "fingerprints_reviewed": len(decisions),
                "approved_for_explicit_intake": len(self.approved_list.approved),
                "invalidated_approvals": len(self.approved_list.invalidated),
            },
            "decision_counts": {
                code: count for code, count in self.approved_list.decision_counts
            },
            "ledger": self.ledger.to_dict(),
            "approved_list": self.approved_list.to_dict(),
            "written_ledger": self.written_ledger,
            "written_approved": self.written_approved,
            "notes": list(self.notes),
        }


def load_review_ledger(path: Path | str) -> ReviewLedger | None:
    """读取既有 review ledger（文件不存在 = 首次运行；损坏 / 被篡改 → fail-closed）。

    Raises:
        ReviewLedgerStateError: 文件不可读 / 非 JSON 对象 / 严格校验失败
            （**绝不**当作"没有历史"，也绝不覆盖既有文件）。
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewLedgerStateError(f"review ledger 不可读：{_safe_path(target)}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewLedgerStateError("review ledger 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ReviewLedgerStateError("review ledger 顶层必须是 JSON 对象")
    return ReviewLedger.from_dict(payload)


def _atomic_write_json(path: Path | str, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(Path(path), text)
    except OSError as exc:
        raise ReviewLedgerWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def write_review_ledger(path: Path | str, ledger: ReviewLedger) -> None:
    """**原子**落盘 review ledger（append-only 语义：文档内含**全部**历史决策）。"""
    _atomic_write_json(path, ledger.to_dict(), what="review ledger")


def write_approved_intake_list(path: Path | str, approved_list: ApprovedIntakeList) -> None:
    """**原子**落盘脱敏批准清单（只是清单；**绝不**触发任何 intake）。"""
    _atomic_write_json(path, approved_list.to_dict(), what="approved-for-explicit-intake 清单")



def _compose_review(
    root: Path,
    *,
    moment: datetime,
    ledger_target: Path | None,
    decision: ReviewDecision | str | None,
    fingerprint: str | None,
    reviewer: str | None,
    reason_code: str | None,
    note: str,
    reviewed_at: datetime | None,
    revision: int | None,
    override: bool,
    attestation_path: Path | None,
) -> ReviewReport:
    """读 ledger（只读）→ 扫描 inbox（只读）→ 应用决策 → 生成批准清单（**零写入**）。"""
    ledger = load_review_ledger(ledger_target) if ledger_target is not None else None
    try:
        scan = scan_inbox(root, moment=moment)
    except InboxError as exc:  # inbox 目录不可读等 → 复核层统一为 ReviewPathError（fail-closed）
        raise ReviewPathError(_safe(str(exc), max_chars=300)) from exc
    if decision is None:
        record: ReviewRecord | None = None
        idempotent = False
        final = ledger if ledger is not None else ReviewLedger(generated_at=moment)
    else:
        decided = record_review_decision(
            scan,
            ledger,
            moment=moment,
            decision=decision,
            fingerprint=fingerprint or "",
            reviewer=reviewer or "",
            reason_code=reason_code or "",
            note=note,
            reviewed_at=reviewed_at,
            revision=revision,
            override=override,
            attestation_path=attestation_path,
        )
        record = decided.record
        idempotent = decided.idempotent
        final = decided.ledger
    approved_list = build_approved_intake_list(scan, final, moment=moment)
    return ReviewReport(
        generated_at=moment,
        inbox_dir=root,
        ledger=final,
        approved_list=approved_list,
        decision=record,
        idempotent=idempotent,
    )


def run_review(
    inbox_dir: Path | str,
    *,
    moment: datetime,
    decision: ReviewDecision | str | None = None,
    fingerprint: str | None = None,
    reviewer: str | None = None,
    reason_code: str | None = None,
    note: str = "",
    reviewed_at: datetime | None = None,
    revision: int | None = None,
    override: bool = False,
    ledger_path: Path | str | None = None,
    out_path: Path | str | None = None,
    approved_out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
    attestation_path: Path | str | None = None,
) -> ReviewReport:
    """执行**一次**人工复核运行（默认只读；只有显式 ``out_path`` 才写 ledger）。

    安全语义：

    - ``ledger_path``（只读）损坏 → :class:`ReviewLedgerStateError`（零写入，旧文件原样保留）；
    - 只有 ``out_path`` 才写 ledger，且先取**单实例锁**再读 ledger（避免并发读改写竞态）；
      锁冲突 → :class:`~src.evidence.readiness_runner.LockConflictError` 且**零写入**；
    - 决策全部在内存中完成并校验（冲突 / 目标不成立 / 词表错误 → 抛错、**零写入**），
      落盘顺序为"批准清单（派生）→ ledger（唯一事实来源）"，两者都是**原子写**；
    - ``APPROVE`` 只表示人工预审通过：本函数**没有**任何 intake / commit 调用，
      也**绝不**写数据库、**绝不**移动 / 删除 / 改写 inbox 内原始 evidence；
    - ``APPROVE`` **必须**给出 ``attestation_path``（GOLD-028 材料级人工核验凭证），并在记录前
      与**当前**候选目录重新绑定核验（缺失 / 陈旧 / 漂移 / 被篡改 →
      :class:`ReviewAttestationError`，**零写入**）；
    - ``REJECT`` / ``NEEDS_CHANGES`` **不**需要凭证（负向决策审计不被阻塞）；
    - 四个安全字段恒为 true / true / false / false（硬编码），与 approve 数量无关。

    Raises:
        ReviewArgumentError: ``moment`` 未带时区或决策元数据不合法。
        ReviewPathError: inbox 目录不可用，或输出写进 inbox 目录。
        ReviewLedgerStateError: 既有 ledger 损坏 / 被篡改（fail-closed）。
        ReviewTargetError: 目标候选不存在 / 预检未通过 / 是模板示例。
        ReviewAttestationError: ``APPROVE`` 缺少 / 漂移 / 被篡改 / 不完整的 attestation。
        ReviewConflictError: 与既有决策冲突且未显式 revision + override。
        LockConflictError: 另一个复核 / 重扫正持有活动锁（fail-closed，零写入）。
        ReviewLedgerWriteError: 输出写入失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(inbox_dir)
    if not root.is_dir():
        raise ReviewPathError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    ledger_target = Path(ledger_path) if ledger_path is not None else None
    out = Path(out_path) if out_path is not None else None
    approved_out = Path(approved_out_path) if approved_out_path is not None else None
    attestation_target = Path(attestation_path) if attestation_path is not None else None
    for target in (ledger_target, out, approved_out):
        if target is not None:
            try:
                ensure_outside_inbox(root, target)
            except InboxError as exc:
                # 输出写在 inbox 内 → 复核层统一为 ReviewPathError（fail-closed，零写入）
                raise ReviewPathError(_safe(str(exc), max_chars=300)) from exc
    if out is None:
        # 只读运行（dry-run）：决策只预览，不落盘任何文件
        return _compose_review(
            root,
            moment=moment,
            ledger_target=ledger_target,
            decision=decision,
            fingerprint=fingerprint,
            reviewer=reviewer,
            reason_code=reason_code,
            note=note,
            reviewed_at=reviewed_at,
            revision=revision,
            override=override,
            attestation_path=attestation_target,
        )
    lock_file = (
        Path(lock_path) if lock_path is not None else out.with_name(out.name + LEDGER_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        report = _compose_review(
            root,
            moment=moment,
            ledger_target=ledger_target,
            decision=decision,
            fingerprint=fingerprint,
            reviewer=reviewer,
            reason_code=reason_code,
            note=note,
            reviewed_at=reviewed_at,
            revision=revision,
            override=override,
            attestation_path=attestation_target,
        )
        if approved_out is not None:
            write_approved_intake_list(approved_out, report.approved_list)
        write_review_ledger(out, report.ledger)
    return replace(
        report,
        written_ledger=_safe_path(out),
        written_approved=_safe_path(approved_out) if approved_out is not None else None,
    )



def render_review_summary(report: ReviewReport) -> str:
    """渲染人类可读的复核摘要（脱敏；**不解除** blocker、不切换 Phase）。"""
    payload = report.to_dict()
    counts = payload["counts"]
    lines: list[str] = [
        "# Evidence Inbox 人工复核决策与审计（Evidence Inbox Review）",
        "",
        f"> blocker `{PHASE3_3_BLOCKER_CODE}`：active=true；human_gate_required=true；"
        "data_qualification_passed=false；phase_transition_allowed=false；"
        "本工具只做**人工复核决策与审计留痕**，不自动 intake、不解除 blocker。",
        "",
        "## 1. 概览",
        "",
        f"- inbox 目录：`{_safe_path(report.inbox_dir)}`（只读发现；原始文件未被移动 / 删除）",
        f"- 本次动作：`{report.action}`；决策数（ledger 历史）：{counts['ledger_decisions']}；"
        f"已复核指纹：{counts['fingerprints_reviewed']}",
        f"- approved-for-explicit-intake（**仍需人工显式 intake**）："
        f"{counts['approved_for_explicit_intake']}；失效批准：{counts['invalidated_approvals']}",
        f"- 审计时点（UTC）：{report.generated_at.isoformat()}",
    ]
    if report.written_ledger is not None:
        lines.append(f"- ledger 已原子写入：`{report.written_ledger}`")
    if report.written_approved is not None:
        lines.append(f"- 批准清单已原子写入：`{report.written_approved}`")
    lines += ["", "## 2. 本次决策", ""]
    if report.decision is None:
        lines.append("- 未提交决策（只读列出 ledger 与批准清单）")
    else:
        record = report.decision
        verb = "幂等重复（既有记录完全相同，未新增）" if report.idempotent else "已记录"
        lines.append(
            f"- `{record.decision}`（revision {record.revision}，{verb}）："
            f"指纹前 16 位 `{record.fingerprint[:16]}`；原因码 `{record.reason_code}`；"
            f"reviewer `{record.reviewer}`；reviewed_at {record.reviewed_at}"
        )
        if record.note:
            lines.append(f"  - note：{_safe(record.note, max_chars=MAX_NOTE_CHARS)}")
        if record.supersedes is not None:
            lines.append(
                f"  - 本次为显式推翻：supersedes=`{record.supersedes[:16]}…`（历史全部保留）"
            )
        lines.append(
            "  - ⚠️ 人工预审通过**不等于** data qualification PASS；"
            "落库仍须显式 `scripts/evidence_operator.py workflow --no-dry-run`"
        )
    lines += [
        "",
        "## 3. ledger 最新决策（每个指纹一条）",
        "",
        "| 候选包 | 指纹（前 16 位） | 决策 | rev | 原因码 | evidence_type | source | reviewer |",
        "|---|---|---|---:|---|---|---|---|",
    ]
    latest = report.ledger.latest_decisions()
    if not latest:
        lines.append("| — | — | — | — | — | — | — | — |")
    for record in latest:
        package_dir = _safe_path(record.package.get("package_dir", ""))
        package_type = _safe(record.package.get("evidence_type", ""), max_chars=40)
        package_source = _safe(record.package.get("source", ""), max_chars=100)
        lines.append(
            f"| `{package_dir}` | `{record.fingerprint[:16]}` | {record.decision} | "
            f"{record.revision} | `{record.reason_code}` | {package_type} | {package_source} | "
            f"{_safe(record.reviewer, max_chars=MAX_REVIEWER_CHARS)} |"
        )
    lines += ["", "## 4. approved-for-explicit-intake（人工预审通过且当前仍一致）", ""]
    if not report.approved_list.approved:
        lines.append("- —（当前没有仍成立的批准；`PHASE3_3_DATA` 保持 BLOCKED）")
    for entry in report.approved_list.approved:
        lines.append(
            f"- `{entry.package_dir}`（指纹前 16 位 `{entry.fingerprint[:16]}`）："
            f"{entry.evidence_type} / {entry.source}；文件 {list(entry.evidence_files)}；"
            f"行数 {entry.rows}（可接收 {entry.acceptable_rows}）；"
            f"人工批准于 {entry.approved_at}（reviewer `{entry.reviewer}`）"
        )
    lines += ["", "## 5. 失效批准（绝不静默放行）", ""]
    if not report.approved_list.invalidated:
        lines.append("- —（没有被失效的批准）")
    for stale in report.approved_list.invalidated:
        lines.append(
            f"- 指纹前 16 位 `{stale.fingerprint[:16]}`：`{stale.reason_code}` —— {stale.detail}"
        )
    lines += ["", "## 6. 口径与边界", ""]
    lines += [f"- {note}" for note in report.notes]
    lines += [f"- {NEXT_STEP_NOTE}", ""]
    return "\n".join(lines)

