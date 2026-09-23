"""Phase 3.3 人工证据**提交就绪包**（**GOLD-033**，默认只读 / 零网络 / 默认零写入）。

为什么需要这一层：GOLD-026 ~ GOLD-030 已分别提供只读缺口诊断、intake handoff 契约、
材料级人工核验凭证与端到端链审计，但**业务方**在看到这些工具时仍需自己拼出
"我现在到底缺哪些**真实材料**、哪些只能人工做、哪些是工程侧已就绪"。本模块把这些
**客观缺口**汇总成**单一、确定、可操作**的提交就绪包（稳定 JSON + 人类可读 Markdown）。

职责边界（**重要**，与 ``.clinerules`` 与 ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）：

- **只读聚合，绝不产生资格**：只读取既有只读结论（GOLD-026 缺口诊断 / GOLD-008 handoff /
  GOLD-027 intake handoff 契约 / GOLD-028 材料级人工核验 / GOLD-030 证据链审计），
  **不新增、不降低**任何资格阈值，也**不重新解释**任何契约字段；本包**永远**不会把
  真实证据判定为合格，更不会解除 ``PHASE3_3_DATA``；
- **五个结论各自独立**（机器可读、稳定字段）：``engineering_ready``（工程链 / 契约一致，
  **只**说明工具链健康）/ ``submission_materials_complete``（业务方提交的真实材料**结构完整**，
  仍待人工核验）/ ``human_verification_complete``（GOLD-028 材料级人工核验已完成）/
  ``data_qualification_passed`` / ``phase_transition_allowed``；后两者在本层**恒为 false**，
  ``l3_gate_pending`` / ``blocker_active`` / ``human_gate_required`` **恒为 true**（**硬编码**）；
- **缺材料时只给事实与人工动作**：输出**稳定 missing reason codes** 与人工动作清单
  （每条动作**直接引用**既有契约字段 / 既有阈值 / 既有命令），并列出每个材料的
  ``IntakeStatus``；**绝不生成、绝不推断** ``published_at`` / ``collected_at`` /
  ``effective_at`` / ``available_at`` / OOS 值（本包只输出**字段名**，不输出任何证据时间值）；
- **Mock / fixture / 模板永不计数**：``evidence_source=rehearsal_fixture`` 时
  ``submission_materials_complete`` / ``human_verification_complete`` **恒为 false**，
  且**不允许**同时传入真实候选目录 / 凭证（fail-closed，防止 fixture 混入真实材料）；
  仅带合成标记的候选包一律 ``NON_QUALIFYING``；
- **默认零写入**：唯一写开关是显式 ``out_path`` / ``--out``（只写**本包本身**，先取单实例锁
  再原子落盘）；本层**没有**任何 intake / commit / 建 source / 写数据库 / 网络 / 模型 / 交易
  调用，也**绝不**修改 inbox、历史 evidence artifact、``Review Ledger``、
  ``PROJECT_STATE`` 或 ``.ai/tasks`` / ``.ai/results``；
- **脱敏**：只输出白名单标量（状态 / 计数 / 阈值 / 缺口 / 稳定原因码 / 字段名 / 已脱敏路径），
  **绝不**输出正文、token / API key / Authorization 或完整 source config。

入口：``scripts/evidence_submission_readiness.py``（默认只打印 stdout；唯一写开关是显式
``--out``；``--rehearsal --work-dir`` 只验证工具链健康，绝不代表真实证据）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from src.common import hashing
from src.common.redaction import safe_text
from src.evidence import readiness_runner as runner
from src.evidence.chain_audit import (
    CHAIN_AUDIT_KIND,
    CHAIN_AUDIT_SCHEMA_VERSION,
    EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    EVIDENCE_SOURCES,
    EvidenceChainAudit,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.handoff import (
    EvidenceHandoffReport,
    ScopeGap,
    build_handoff_report,
    thresholds,
)
from src.evidence.human_verification_attestation import (
    REQUIRED_VERIFICATION_CATEGORIES,
    AttestationVerification,
    attestable_material_keys,
    verify_attestation,
)
from src.evidence.inbox import (
    MANIFEST_REQUIRED_FIELDS,
    InboxError,
    ensure_outside_inbox,
)
from src.evidence.intake_handoff import (
    MATERIAL_SPECS,
    TIME_SEMANTICS_REQUIREMENTS,
    IntakeHandoffDocument,
    IntakeStatus,
    MaterialSpec,
    PackageHandoff,
    load_intake_handoff,
)
from src.evidence.ledger import load_evidence_ledger
from src.evidence.readiness_watch import atomic_write_text
from src.monitoring.evidence_gap_diagnostic import (
    EvidenceGapDiagnostic,
    ReadinessClass,
    build_gap_diagnostic,
)
from src.monitoring.evidence_readiness import EvidenceReadinessReport, build_readiness_report
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "MATERIAL_MISSING_REASON_CODES",
    "SUBMISSION_PACK_FILE_NAME",
    "SUBMISSION_PACK_KIND",
    "SUBMISSION_PACK_LOCK_SUFFIX",
    "SUBMISSION_PACK_NOTE",
    "SUBMISSION_PACK_REHEARSAL_NOTE",
    "SUBMISSION_PACK_SCHEMA_VERSION",
    "SUBMISSION_PACK_SEMANTICS_NOTE",
    "SUBMISSION_PACK_TIME_SEMANTICS_NOTE",
    "ChainBinding",
    "HumanAction",
    "MaterialRequirement",
    "ScopeSubmission",
    "SubmissionCode",
    "SubmissionPackArgumentError",
    "SubmissionPackError",
    "SubmissionPackPathError",
    "SubmissionPackStateError",
    "SubmissionPackWriteError",
    "SubmissionReadinessPack",
    "build_submission_pack",
    "chain_binding_from_audit",
    "compute_pack_facts_digest",
    "compute_pack_id",
    "load_chain_binding",
    "load_submission_pack",
    "main_pack_exit_code",
    "render_submission_pack_markdown",
    "run_submission_pack",
    "submission_pack_exit_code_for",
    "submission_pack_schema",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
SUBMISSION_PACK_SCHEMA_VERSION: Final[int] = 1
#: 文档 / 报告标识（稳定，供业务方与上游日志解析）
SUBMISSION_PACK_KIND: Final[str] = "phase33_human_evidence_submission_pack"
SUBMISSION_PACK_REPORT_NAME: Final[str] = SUBMISSION_PACK_KIND
#: 输入契约版本（**只引用** ``evidence-intake-v1``，不另造第二套契约）
SUBMISSION_PACK_CONTRACT_VERSION: Final[str] = EVIDENCE_CONTRACT_VERSION
#: 执行模式（**恒为只读聚合**：本层不采集、不 intake、不写库、不交易）
SUBMISSION_PACK_EXECUTION_MODE: Final[str] = "READ_ONLY_SUBMISSION_READINESS_PACK"
#: ``--out`` 时的默认文件名
SUBMISSION_PACK_FILE_NAME: Final[str] = "phase33_human_evidence_submission_pack.json"
#: 输出 artifact 的单实例锁后缀（与 ``out_path`` 同级）
SUBMISSION_PACK_LOCK_SUFFIX: Final[str] = ".lock"

#: 固定说明：本包**不**产生资格，也不具备任何推进能力
SUBMISSION_PACK_NOTE: Final[str] = (
    "本包只**只读聚合**既有 Phase 3.3 能力（GOLD-026 缺口诊断 / GOLD-008 handoff / "
    "GOLD-027 intake handoff 契约 / GOLD-028 材料级人工核验 / GOLD-030 证据链审计）："
    "不联网、不抓取、不绕过 robots / 证书、不写数据库、不训练模型、不触碰交易；"
    "它**不产生**也**不推断**任何真实证据资格，"
    f"`{PHASE3_3_BLOCKER_CODE}` 保持 BLOCKED，L3 / L4 只能人工决策。"
)
#: 固定说明：五个结论字段各自的诚实语义（**互不替代**）
SUBMISSION_PACK_SEMANTICS_NOTE: Final[str] = (
    "`engineering_ready=true` **只**表示工程链 / 契约一致（工具链健康）；"
    "`submission_materials_complete=true` **只**表示提交的真实材料**结构完整**"
    "（仍必须由人工核验法律效力 / 出处 / 时间语义）；"
    "`human_verification_complete=true` **只**表示 GOLD-028 材料级人工核验已完成；"
    "三者**都不等于** `data_qualification_passed`。"
    "`data_qualification_passed` / `phase_transition_allowed` **恒为 false**、"
    "`l3_gate_pending` / `blocker_active` / `human_gate_required` **恒为 true**："
    f"`{PHASE3_3_BLOCKER_CODE}` 只能由真实授权证据 + L3 人工 Gate 独立解除。"
)
#: 固定说明：证据时间**只**来自提交数据，本包不生成 / 不推断 / 不填补
SUBMISSION_PACK_TIME_SEMANTICS_NOTE: Final[str] = (
    "本包**不生成、不推断、不填补** `published_at` / `collected_at` / `effective_at` / "
    "`available_at` / OOS 证据：缺材料时只输出**字段名**与人工下一步；"
    "`generated_at` 是**审计操作时间**，绝不冒充证据时间；"
    "文件 mtime / 抓取时间 / 当前时间同样不被采信。"
)
#: 固定说明：演练（fixture / Mock）输入的诚实语义
SUBMISSION_PACK_REHEARSAL_NOTE: Final[str] = (
    f"`evidence_source={EVIDENCE_SOURCE_REHEARSAL_FIXTURE}` 表示本次输入**完全**由 "
    "fixture / Mock 构成：`submission_materials_complete` / `human_verification_complete` "
    "**恒为 false**，真实材料缺失类原因码恒被输出；演练**绝不**代表真实授权证据、"
    f"**绝不**解除 `{PHASE3_3_BLOCKER_CODE}`、**绝不**构成 L3 批准，只证明工具链健康。"
)


class SubmissionCode(StrEnum):
    """本层**稳定**原因码（与既有模块同义的原因码**直接沿用**其字符串，不另造词）。

    这些码是**给业务方与人工**看的"缺什么"清单：每条都能落到既有契约字段 / 既有命令上，
    且**只**描述客观缺口，绝不表示资格结论。
    """

    #: 契约 / 材料清单漂移（fail-closed：本层绝不静默按"无缺口"处理）
    CONTRACT_DRIFT = "SUBMISSION_CONTRACT_DRIFT"
    #: 缺少**真实**（非 Mock / 模板 / 示例）授权材料
    REAL_MATERIAL_MISSING = "SUBMISSION_REAL_MATERIAL_MISSING"
    #: 材料已提交但独立证据引用 / 机械校验不足（尚不能进入人工核验队列）
    MATERIAL_PRESENT_UNVERIFIED = "SUBMISSION_MATERIAL_PRESENT_UNVERIFIED"
    #: 只有 Mock / 模板 / 示例 / 合成候选包：**永远**不计资格
    SYNTHETIC_MATERIAL_ONLY = "SUBMISSION_SYNTHETIC_MATERIAL_ONLY"
    #: 逐材料缺失原因码（key 与 GOLD-027 ``MATERIAL_SPECS`` 一致）
    EVIDENCE_RECORDS_MISSING = "SUBMISSION_EVIDENCE_RECORDS_MISSING"
    SOURCE_IDENTITY_MISSING = "SUBMISSION_SOURCE_IDENTITY_MISSING"
    AUTHORIZATION_MISSING = "SUBMISSION_AUTHORIZATION_MISSING"
    TIME_SEMANTICS_MISSING = "SUBMISSION_TIME_SEMANTICS_MISSING"
    PUBLISHED_AT_MISSING = "SUBMISSION_PUBLISHED_AT_MISSING"
    COLLECTED_AT_MISSING = "SUBMISSION_COLLECTED_AT_MISSING"
    EFFECTIVE_AT_MISSING = "SUBMISSION_EFFECTIVE_AT_MISSING"
    AVAILABILITY_OOS_MISSING = "SUBMISSION_AVAILABILITY_OOS_MISSING"
    AUTHOR_IDENTITY_MISSING = "SUBMISSION_AUTHOR_IDENTITY_MISSING"
    #: 库内 / 候选量化门槛仍未达标（阈值**只引用** ``src/alpha/evidence_gate.py``）
    QUANTIFIED_GAP_OPEN = "SUBMISSION_QUANTIFIED_GAP_OPEN"
    #: 未给出 DB 台账（本次未读取 readiness：缺什么**不可知**，fail-closed）
    LEDGER_UNINSPECTED = "SUBMISSION_LEDGER_UNINSPECTED"
    #: 没有提供 GOLD-028 材料级人工核验凭证
    HUMAN_ATTESTATION_MISSING = "SUBMISSION_HUMAN_ATTESTATION_MISSING"
    #: 凭证存在但材料级人工核验未全部完成
    HUMAN_VERIFICATION_INCOMPLETE = "SUBMISSION_HUMAN_VERIFICATION_INCOMPLETE"
    #: 凭证与**当前**候选包 / handoff 内容身份不再一致（漂移 / 篡改：旧凭证失效）
    ATTESTATION_STALE = "SUBMISSION_ATTESTATION_STALE"
    #: 未提供 GOLD-030 链审计结论（工程链是否一致**不可知**，fail-closed）
    ENGINEERING_CHAIN_NOT_AUDITED = "SUBMISSION_ENGINEERING_CHAIN_NOT_AUDITED"
    #: 工程链审计未全部通过（工具链自身未就绪：给出最早失败阶段）
    ENGINEERING_CHAIN_INCOMPLETE = "SUBMISSION_ENGINEERING_CHAIN_INCOMPLETE"
    #: 本次输入是 fixture / Mock（演练）：**绝不**计为真实材料
    REHEARSAL_INPUT_NON_QUALIFYING = "SUBMISSION_REHEARSAL_INPUT_NON_QUALIFYING"
    #: L3 人工 Gate 待办（恒为待办：任何工具都无权推进）
    L3_HUMAN_GATE_PENDING = "SUBMISSION_L3_HUMAN_GATE_PENDING"


#: GOLD-027 材料 key → 该材料**缺失**时的稳定原因码（漂移由 :func:`_verify_material_contract` 守卫）
MATERIAL_MISSING_REASON_CODES: Final[dict[str, str]] = {
    "evidence_records": SubmissionCode.EVIDENCE_RECORDS_MISSING.value,
    "source_identity": SubmissionCode.SOURCE_IDENTITY_MISSING.value,
    "authorization_declaration": SubmissionCode.AUTHORIZATION_MISSING.value,
    "time_semantics": SubmissionCode.TIME_SEMANTICS_MISSING.value,
    "published_at": SubmissionCode.PUBLISHED_AT_MISSING.value,
    "collected_at": SubmissionCode.COLLECTED_AT_MISSING.value,
    "effective_at": SubmissionCode.EFFECTIVE_AT_MISSING.value,
    "availability_oos": SubmissionCode.AVAILABILITY_OOS_MISSING.value,
    "author_identity": SubmissionCode.AUTHOR_IDENTITY_MISSING.value,
}


class SubmissionPackError(RuntimeError):
    """本层失败（**fail-closed**：调用方按退出码处理，不得假设已写入任何 artifact）。"""


class SubmissionPackArgumentError(SubmissionPackError):
    """参数 / 词表 / 时区错误。"""


class SubmissionPackPathError(SubmissionPackError):
    """inbox 目录或输出 artifact 不可用（含把输出写进 inbox 的拒绝）。"""


class SubmissionPackStateError(SubmissionPackError):
    """输入 artifact 缺失 / 不可读 / 结构非法（fail-closed，零写入）。"""


class SubmissionPackWriteError(SubmissionPackError):
    """输出 artifact 原子写失败（**fail-closed**）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 无待办缺口（**仍不是**资格通过：blocker 恒为 active）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数 / 词表 / 时区错误
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输出 artifact 不可用（含把输出写进 inbox 的拒绝）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: 输入 artifact 缺失 / 不可读 / 结构非法（fail-closed，零写入）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 仍有待办缺口（**预期 BLOCKED**：真实材料 / 人工核验 / 工程链至少缺一项）
EXIT_BLOCKED: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一处写入正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_PACK_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (SubmissionPackArgumentError, EXIT_CONFIG_ERROR),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (SubmissionPackPathError, EXIT_UNUSABLE),
    (SubmissionPackWriteError, EXIT_UNUSABLE),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
    (SubmissionPackError, EXIT_STATE_INVALID),
)


def submission_pack_exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为"state 非法"）。"""
    for kind, code in _PACK_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def main_pack_exit_code(pack: SubmissionReadinessPack) -> int:
    """包结论 → 进程退出码：无待办缺口 ``0``，否则 ``5``（**预期 BLOCKED**）。"""
    return EXIT_OK if pack.nothing_open else EXIT_BLOCKED


# ---------------------------------------------------------------------------
# 通用工具（脱敏 / 规范化 / 时区 / 内容身份）
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
        raise SubmissionPackArgumentError(f"{field_name} 必须是带时区的 datetime")
    return moment.astimezone(UTC)


def _canonical_json(payload: object) -> str:
    """规范化 JSON（确定性：键排序 + 紧凑分隔符 + 不转义非 ASCII）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_path(value: str | None) -> str | None:
    """可选路径：``None`` 保持 ``None``（缺省 = 未提供），否则脱敏。"""
    return None if value is None else _safe_path(value)


def _inline(values: tuple[str, ...]) -> str:
    """把字段名 / 引用渲染为脱敏内联引用（空集合显示 ``—``）。"""
    return ", ".join(f"`{_safe(name, max_chars=80)}`" for name in values) if values else "—"


def _policy_block() -> dict[str, Any]:
    """硬编码安全策略块（任何输入都无法把它改成"通过"）。"""
    return {
        "execution_mode": SUBMISSION_PACK_EXECUTION_MODE,
        "contract_version": SUBMISSION_PACK_CONTRACT_VERSION,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "gate_blocked": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "l3_l4_auto_advance_allowed": False,
        "evidence_qualified": False,
        "advance_allowed": False,
        "generates_evidence_times": False,
        "writes_database": False,
        "reads_network": False,
        "executes_intake": False,
    }


def _material_specs() -> dict[str, MaterialSpec]:
    """材料契约索引（**只引用** GOLD-027 ``MATERIAL_SPECS``，不复制）。"""
    return {spec.key: spec for spec in MATERIAL_SPECS}


def _verify_material_contract() -> tuple[str, ...]:
    """契约漂移守卫：必核验材料清单 / 原因码映射 / 必核验类别必须完全对齐。

    Returns:
        可由人工核验的材料 key（顺序即 GOLD-027 的输出顺序）。

    Raises:
        SubmissionPackError: 材料契约漂移（缺材料 / 多材料 / 缺必核验类别 / 原因码映射过期）。
            绝不静默按"无缺口"处理（fail-closed）。
    """
    keys = attestable_material_keys()
    missing_codes = sorted(set(keys) - set(MATERIAL_MISSING_REASON_CODES))
    stale_codes = sorted(set(MATERIAL_MISSING_REASON_CODES) - set(keys))
    if missing_codes or stale_codes:
        raise SubmissionPackError(
            f"{SubmissionCode.CONTRACT_DRIFT.value}：缺失原因码映射与 GOLD-027 材料契约不一致"
            f"（缺映射 {missing_codes}；多余映射 {stale_codes}）"
        )
    specs = _material_specs()
    categories = {specs[key].category for key in keys}
    uncovered = sorted(set(REQUIRED_VERIFICATION_CATEGORIES) - categories)
    if uncovered:
        raise SubmissionPackError(
            f"{SubmissionCode.CONTRACT_DRIFT.value}：必核验材料类别覆盖不足（缺少 {uncovered}）"
        )
    return keys


# ---------------------------------------------------------------------------
# GOLD-030 证据链审计的**只读绑定**（使用事实，不重算、不复制阶段语义）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChainBinding:
    """一次 GOLD-030 链审计的**客观绑定事实**（不含路径与审计时点）。"""

    report_kind: str
    schema_version: int
    #: ``rehearsal_fixture`` / ``operator_provided``（受控词表，来自 GOLD-030）
    evidence_source: str
    scenario: str | None
    chain_id: str
    facts_digest: str
    engineering_chain_ready: bool
    earliest_failure_stage: str | None
    stage_statuses: tuple[tuple[str, str], ...]

    @property
    def rehearsal(self) -> bool:
        """本次链审计是否由 fixture / Mock 构成（**绝不是**真实资格证据）。"""
        return self.evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE

    @property
    def passed_stage_count(self) -> int:
        """``PASS`` 阶段数。"""
        return sum(1 for _stage, status in self.stage_statuses if status == "PASS")

    @property
    def failed_stage_count(self) -> int:
        """非 ``PASS`` 阶段数（``MISSING`` / ``FAIL`` / ``NOT_EVALUATED``）。"""
        return sum(1 for _stage, status in self.stage_statuses if status != "PASS")

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**）。"""
        return {
            "report_kind": self.report_kind,
            "schema_version": self.schema_version,
            "evidence_source": self.evidence_source,
            "scenario": self.scenario,
            "rehearsal": self.rehearsal,
            "chain_id": self.chain_id,
            "facts_digest": self.facts_digest,
            "engineering_chain_ready": self.engineering_chain_ready,
            "earliest_failure_stage": self.earliest_failure_stage,
            "passed_stage_count": self.passed_stage_count,
            "failed_stage_count": self.failed_stage_count,
            "stage_statuses": {stage: status for stage, status in self.stage_statuses},
        }


def chain_binding_from_audit(audit: EvidenceChainAudit) -> ChainBinding:
    """把 :class:`~src.evidence.chain_audit.EvidenceChainAudit` 绑定为只读事实（纯函数）。"""
    return ChainBinding(
        report_kind=CHAIN_AUDIT_KIND,
        schema_version=CHAIN_AUDIT_SCHEMA_VERSION,
        evidence_source=audit.evidence_source,
        scenario=audit.scenario,
        chain_id=audit.chain_id,
        facts_digest=audit.facts_digest,
        engineering_chain_ready=audit.engineering_chain_ready,
        earliest_failure_stage=(
            None if audit.earliest_failure_stage is None else audit.earliest_failure_stage.value
        ),
        stage_statuses=tuple(
            (item.stage.value, item.status.value) for item in audit.stages
        ),
    )


def load_chain_binding(path: Path | str) -> ChainBinding:
    """读取一份 GOLD-030 链审计报告并绑定为事实（**纯只读 / 零写入**）。

    Raises:
        SubmissionPackPathError: 报告不存在。
        SubmissionPackStateError: 报告不可读 / 非合法 JSON / kind、schema、来源、阶段结构非法。
            绝不猜测、绝不静默按"工程链就绪"处理（fail-closed）。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SubmissionPackPathError(
            f"链审计报告不存在：{_safe_path(target)}（请先运行 "
            "`scripts/evidence_chain_audit.py` 生成）"
        ) from exc
    except OSError as exc:
        raise SubmissionPackStateError(
            f"链审计报告不可读（{type(exc).__name__}）：{_safe_path(target)}"
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SubmissionPackStateError(
            f"链审计报告不是合法 JSON：{_safe_path(target)}"
        ) from exc
    if not isinstance(payload, dict):
        raise SubmissionPackStateError(f"链审计报告顶层必须是 JSON 对象：{_safe_path(target)}")
    kind = str(payload.get("kind") or "").strip()
    if kind != CHAIN_AUDIT_KIND:
        raise SubmissionPackStateError(
            f"链审计报告 kind 不是 `{CHAIN_AUDIT_KIND}`：{_safe(kind, max_chars=80)}"
        )
    schema_version = payload.get("schema_version")
    if schema_version != CHAIN_AUDIT_SCHEMA_VERSION:
        raise SubmissionPackStateError(
            f"链审计 schema 版本不受支持（期望 {CHAIN_AUDIT_SCHEMA_VERSION}）："
            f"{_safe(schema_version, max_chars=40)}"
        )
    evidence_source = str(payload.get("evidence_source") or "").strip()
    if evidence_source not in EVIDENCE_SOURCES:
        allowed = ", ".join(EVIDENCE_SOURCES)
        raise SubmissionPackStateError(
            f"链审计报告 evidence_source 必须是 {allowed} 之一："
            f"{_safe(evidence_source, max_chars=80)}"
        )
    chain_id = str(payload.get("chain_id") or "").strip()
    facts_digest = str(payload.get("facts_digest") or "").strip()
    if not chain_id or not facts_digest:
        raise SubmissionPackStateError("链审计报告缺少 chain_id / facts_digest（内容身份不完整）")
    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages:
        raise SubmissionPackStateError("链审计报告缺少 stages（无法判断逐段绑定）")
    stage_statuses: list[tuple[str, str]] = []
    for item in stages:
        if not isinstance(item, dict) or "stage" not in item or "status" not in item:
            raise SubmissionPackStateError("链审计报告 stages 结构非法（缺 stage / status）")
        stage_statuses.append(
            (
                _safe(item["stage"], max_chars=80),
                _safe(item["status"], max_chars=40),
            )
        )
    earliest = payload.get("earliest_failure_stage")
    return ChainBinding(
        report_kind=kind,
        schema_version=int(schema_version),
        evidence_source=evidence_source,
        scenario=(
            None
            if payload.get("scenario") is None
            else _safe(payload["scenario"], max_chars=80)
        ),
        chain_id=_safe(chain_id, max_chars=64),
        facts_digest=_safe(facts_digest, max_chars=64),
        engineering_chain_ready=bool(payload.get("engineering_chain_ready")),
        earliest_failure_stage=None if earliest is None else _safe(earliest, max_chars=80),
        stage_statuses=tuple(stage_statuses),
    )


# ---------------------------------------------------------------------------
# 只读事实：材料级 / scope 级
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    """GOLD-027 单个必核验材料在**本次输入**下的只读事实（不含任何证据时间值）。"""

    key: str
    category: str
    scope: str
    requirement: str
    contract_fields: tuple[str, ...]
    reference_fields: tuple[str, ...]
    declaration_fields: tuple[str, ...]
    status: IntakeStatus
    reason_code: str
    real_package_count: int
    synthetic_package_count: int
    missing_fields: tuple[str, ...]
    human_next_step: str

    @property
    def complete(self) -> bool:
        """该材料**结构完整**（只表示可进入人工核验队列，**不是**资格通过）。"""
        return self.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**：只含字段名与状态，不含正文与时间值）。"""
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "requirement": self.requirement,
            "contract_fields": list(self.contract_fields),
            "reference_fields": list(self.reference_fields),
            "declaration_fields": list(self.declaration_fields),
            "missing_fields": list(self.missing_fields),
            "real_package_count": self.real_package_count,
            "synthetic_package_count": self.synthetic_package_count,
            "human_next_step": self.human_next_step,
            # 结构完整 / Mock 都**不计**资格：恒为 false（硬编码，不被输入透传）
            "counts_toward_eligibility": False,
            "evidence_qualified": False,
        }


@dataclass(frozen=True, slots=True)
class ScopeSubmission:
    """单个 scope（Author / News）的提交就绪事实（材料状态 + 既有量化缺口）。"""

    scope: str
    status: IntakeStatus
    package_count: int
    real_package_count: int
    preflight_pass_count: int
    synthetic_package_count: int
    material_keys: tuple[str, ...]
    missing_material_keys: tuple[str, ...]
    unverified_material_keys: tuple[str, ...]
    quantified_gap_open: bool
    eligible: int
    required: int
    remaining: int
    remaining_checks: int
    human_next_step: str

    @property
    def complete(self) -> bool:
        """该 scope 的真实材料**结构完整**（仍待人工核验；**不是**资格通过）。"""
        return self.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**）。"""
        return {
            "scope": self.scope,
            "status": self.status.value,
            "complete": self.complete,
            "package_count": self.package_count,
            "real_package_count": self.real_package_count,
            "preflight_pass_count": self.preflight_pass_count,
            "synthetic_package_count": self.synthetic_package_count,
            "material_keys": list(self.material_keys),
            "missing_material_keys": list(self.missing_material_keys),
            "unverified_material_keys": list(self.unverified_material_keys),
            "quantified_gap_open": self.quantified_gap_open,
            "eligible": self.eligible,
            "required": self.required,
            "remaining": self.remaining,
            "remaining_checks": self.remaining_checks,
            "human_next_step": self.human_next_step,
            # 结构完整 ≠ 资格通过：恒为 false（硬编码）
            "counts_toward_eligibility": False,
            "evidence_qualified": False,
        }


# ---------------------------------------------------------------------------
# 人工动作清单（每条都**直接引用**既有契约字段 / 既有阈值 / 既有命令）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HumanAction:
    """一条必须人工完成 / 提供的动作（``blocking`` 恒为 ``True``）。"""

    key: str
    category: str
    scope: str
    reason_code: str
    requirement: str
    evidence_reference: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**）。"""
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "reason_code": self.reason_code,
            "requirement": self.requirement,
            "evidence_reference": self.evidence_reference,
            "blocking": self.blocking,
        }


# ---------------------------------------------------------------------------
# 提交就绪包（确定性 / 脱敏 / 内容寻址；安全字段**硬编码**）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SubmissionReadinessPack:
    """业务方"还缺哪些真实材料 / 哪些必须人工做"的**单一**只读汇总。"""

    generated_at: datetime
    inbox_dir: str | None
    evidence_source: str
    rehearsal: bool
    pack_id: str
    facts_digest: str
    engineering_ready: bool
    submission_materials_complete: bool
    human_verification_complete: bool
    #: 库内既有量化门槛是否全部达标（``readiness`` 缺失时恒为 ``False``，fail-closed）
    quantified_ready: bool
    missing_reason_codes: tuple[str, ...]
    materials: tuple[MaterialRequirement, ...]
    scopes: tuple[ScopeSubmission, ...]
    human_actions: tuple[HumanAction, ...]
    l3_pending: tuple[str, ...]
    thresholds: tuple[tuple[str, float], ...]
    time_semantics_requirements: tuple[str, ...]
    gap: dict[str, Any] | None = None
    chain: ChainBinding | None = None
    attestation: dict[str, Any] | None = None
    notes: tuple[str, ...] = (
        SUBMISSION_PACK_NOTE,
        SUBMISSION_PACK_SEMANTICS_NOTE,
        SUBMISSION_PACK_TIME_SEMANTICS_NOTE,
    )
    written_path: str | None = None

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return SUBMISSION_PACK_KIND

    @property
    def report(self) -> str:
        """报告标识（稳定）。"""
        return SUBMISSION_PACK_REPORT_NAME

    @property
    def nothing_open(self) -> bool:
        """本包自身**是否已无待办缺口**（工程链 + 材料结构 + 人工核验 + 量化门槛全部完成）。

        ⚠️ 即使为 ``True``，``data_qualification_passed`` / ``phase_transition_allowed``
        仍**恒为 false**：资格与 Phase 切换只能由真实授权证据 + L3 人工 Gate 决定。
        """
        return (
            self.engineering_ready
            and self.submission_materials_complete
            and self.human_verification_complete
            and self.quantified_ready
        )

    def material(self, key: str) -> MaterialRequirement:
        """按 key 取材料事实；缺失即显式失败（防止口径漂移被静默忽略）。"""
        for item in self.materials:
            if item.key == key:
                return item
        raise KeyError(key)

    def scope_section(self, scope: str) -> ScopeSubmission:
        """按 scope 取事实；缺失即显式失败。"""
        for item in self.scopes:
            if item.scope == scope:
                return item
        raise KeyError(scope)

    def action(self, key: str) -> HumanAction:
        """按 key 取人工动作；缺失即显式失败。"""
        for item in self.human_actions:
            if item.key == key:
                return item
        raise KeyError(key)

    # ---- 恒定的安全语义（**硬编码**：任何输入都无法把它们改成"通过"）--------
    @property
    def data_qualification_passed(self) -> bool:
        """恒为 ``False``：本包**没有**资格判定能力。"""
        return False

    @property
    def phase_transition_allowed(self) -> bool:
        """恒为 ``False``：Phase 切换只能由 L3 人工 Gate 决定。"""
        return False

    @property
    def l3_gate_pending(self) -> bool:
        """恒为 ``True``：L3 / L4 只能人工决策，本包永不自动推进。"""
        return True

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

    @property
    def advance_allowed(self) -> bool:
        """恒为 ``False``。"""
        return False

    @property
    def l3_l4_auto_advance_allowed(self) -> bool:
        """恒为 ``False``。"""
        return False

    @property
    def evidence_qualified(self) -> bool:
        """恒为 ``False``。"""
        return False

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段与五个结论**显式且恒定**）。"""
        return {
            "kind": self.kind,
            "report": self.report,
            "schema_version": SUBMISSION_PACK_SCHEMA_VERSION,
            "contract_version": SUBMISSION_PACK_CONTRACT_VERSION,
            "execution_mode": SUBMISSION_PACK_EXECUTION_MODE,
            "generated_at": self.generated_at.isoformat(),
            "inbox_dir": _optional_path(self.inbox_dir),
            "evidence_source": self.evidence_source,
            "rehearsal": self.rehearsal,
            "pack_id": self.pack_id,
            "facts_digest": self.facts_digest,
            # ---- 五个独立结论（前三个只描述"是否就绪"，绝不等于资格）--------
            "engineering_ready": self.engineering_ready,
            "submission_materials_complete": self.submission_materials_complete,
            "human_verification_complete": self.human_verification_complete,
            "quantified_ready": self.quantified_ready,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "l3_gate_pending": self.l3_gate_pending,
            "nothing_open": self.nothing_open,
            # ---- 恒定安全字段（硬编码）------------------------------------
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "gate_blocked": self.gate_blocked,
            "advance_allowed": self.advance_allowed,
            "l3_l4_auto_advance_allowed": self.l3_l4_auto_advance_allowed,
            "evidence_qualified": self.evidence_qualified,
            # ---- 客观缺口与人工动作 ---------------------------------------
            "missing_reason_codes": list(self.missing_reason_codes),
            "human_actions": [item.to_dict() for item in self.human_actions],
            "l3_pending": list(self.l3_pending),
            "materials": [item.to_dict() for item in self.materials],
            "scopes": [item.to_dict() for item in self.scopes],
            "thresholds": {name: value for name, value in self.thresholds},
            "time_semantics_requirements": list(self.time_semantics_requirements),
            "gap_diagnostic": self.gap,
            "chain": None if self.chain is None else self.chain.to_dict(),
            "attestation": self.attestation,
            # ---- 只读保证（硬编码）----------------------------------------
            "writes_database": False,
            "reads_network": False,
            "executes_intake": False,
            "generates_evidence_times": False,
            "generates_qualification": False,
            # Mock / fixture / 模板**永远**不是真实材料（硬编码）
            "mock_or_fixture_counts_as_real_material": False,
            "written_path": _optional_path(self.written_path),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 版本化提交契约（业务方"需要提交什么"的单一机器可读来源）
# ---------------------------------------------------------------------------
def submission_pack_schema() -> dict[str, Any]:
    """返回**版本化**的提交就绪契约（人工据此准备真实材料；机器可读、可 diff）。

    这是"业务方需要提供什么"的单一来源：材料清单、缺失原因码、证据来源词表、
    五项结论的真实语义、既有阈值（**只引用** ``src.alpha.evidence_gate``）与独立时间
    语义要求都在此声明；本函数**不新增、不降低**任何资格门槛。
    """
    return {
        "kind": SUBMISSION_PACK_KIND,
        "schema_version": SUBMISSION_PACK_SCHEMA_VERSION,
        "contract_version": SUBMISSION_PACK_CONTRACT_VERSION,
        "execution_mode": SUBMISSION_PACK_EXECUTION_MODE,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "evidence_sources": list(EVIDENCE_SOURCES),
        "conclusions": {
            "engineering_ready": (
                "**只**表示工程链 / 契约一致（GOLD-030 审计；工具链健康），不是资格结论。"
            ),
            "submission_materials_complete": (
                "**只**表示业务方提交的真实材料结构完整（非 Mock / 模板 / 示例，"
                "且 GOLD-027 逐材料可达人工核验队列），仍待人工核验。"
            ),
            "human_verification_complete": (
                "**只**表示 GOLD-028 材料级人工核验凭证对**当前**候选包仍然成立"
                "（`all_required_verified=true`），不是数据资格。"
            ),
            "data_qualification_passed": "**恒为 false**：本层没有资格判定能力。",
            "phase_transition_allowed": "**恒为 false**：只能由 L3 人工 Gate 决定。",
            "l3_gate_pending": "**恒为 true**：L3 / L4 只能人工决策，任何工具都不推进。",
        },
        "materials": [spec.to_dict() for spec in MATERIAL_SPECS],
        "material_missing_reason_codes": dict(sorted(MATERIAL_MISSING_REASON_CODES.items())),
        "reason_codes": sorted(code.value for code in SubmissionCode),
        "thresholds": dict(sorted(thresholds().items())),
        "time_semantics_requirements": list(TIME_SEMANTICS_REQUIREMENTS),
        "manifest_required_fields": list(MANIFEST_REQUIRED_FIELDS),
        "human_action_keys": [item.key for item in _human_action_specs()],
        "safety": _policy_block(),
        "notes": [
            SUBMISSION_PACK_NOTE,
            SUBMISSION_PACK_SEMANTICS_NOTE,
            SUBMISSION_PACK_TIME_SEMANTICS_NOTE,
            SUBMISSION_PACK_REHEARSAL_NOTE,
        ],
    }


def _human_action_specs() -> tuple[HumanAction, ...]:
    """固定的人工动作清单（**只引用**既有契约字段 / 阈值 / 命令，不含运行期事实）。"""
    return tuple(
        HumanAction(
            key=key,
            category=category,
            scope=scope,
            reason_code=reason_code,
            requirement=requirement,
            evidence_reference=evidence_reference,
        )
        for key, category, scope, reason_code, requirement, evidence_reference in (
            (
                "provide_real_materials",
                "authorization",
                "both",
                SubmissionCode.REAL_MATERIAL_MISSING.value,
                "按 GOLD-027 intake handoff 契约提供**真实**授权 Author / News 材料"
                "（来源身份 / 授权证明 / 独立出处 / 独立时间语义 / availability-OOS），"
                "普通 CSV、Mock、模板、示例一律不计资格。",
                "scripts/evidence_intake_handoff.py（--inbox-dir）→ "
                "scripts/intake_evidence.py（--no-dry-run）→ "
                "scripts/evidence_readiness.py（recheck）",
            ),
            (
                "complete_material_human_verification",
                "human_verification",
                "both",
                SubmissionCode.HUMAN_ATTESTATION_MISSING.value,
                "对**当前**候选包逐材料记录受控人工决策"
                "（VERIFIED / REJECTED / NEEDS_CHANGES）"
                "+ 稳定 reason code + 显式 reviewer + 带时区 reviewed_at "
                "+ 独立 evidence reference，生成 GOLD-028 材料级人工核验凭证"
                "（漂移即失效，绝不静默继承）。",
                "scripts/evidence_human_verification_attestation.py（--verification --out）",
            ),
            (
                "reach_quantified_thresholds",
                "sample_size",
                "both",
                SubmissionCode.QUANTIFIED_GAP_OPEN.value,
                "达到既有量化门槛（阈值**只引用** src/alpha/evidence_gate.py，"
                "本包不新增 / 不降低）。",
                "scripts/evidence_readiness.py（就绪度 / recheck）",
            ),
            (
                "verify_tool_chain_health",
                "engine",
                "both",
                SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value,
                "提交真实材料**前**先跑一次纯本地 rehearsal 验证工具链健康"
                "（fixture / Mock 只证明工程链契约一致，绝不代表真实证据）。",
                "scripts/evidence_chain_audit.py（--rehearsal --work-dir <dir>）",
            ),
            (
                "l3_human_gate_decision",
                "gate",
                "both",
                SubmissionCode.L3_HUMAN_GATE_PENDING.value,
                f"`{PHASE3_3_BLOCKER_CODE}` 的解除与后续 Phase 切换只能由 L3 人工按开发协议决策；"
                "本包没有解除 / 放行 / 推进能力。",
                ".ai/DEVELOPMENT_PROTOCOL.md（L3 / L4 人工 Gate）",
            ),
        )
    )


def _fixed_action(key: str) -> HumanAction:
    """按 key 取固定动作模板；缺失即显式失败（防止词表漂移被静默忽略）。"""
    for item in _human_action_specs():
        if item.key == key:
            return item
    raise KeyError(key)


def _gap_summary(diagnostic: EvidenceGapDiagnostic | None) -> dict[str, Any] | None:
    """GOLD-026 缺口诊断的**紧凑**只读摘要（不复制口径；**不含任何证据时间值**）。"""
    if diagnostic is None:
        return None
    return {
        "kind": diagnostic.kind,
        "schema_version": diagnostic.schema_version,
        "readiness": diagnostic.readiness.value,
        "class_counts": {name: count for name, count in diagnostic.class_counts},
        "open_evidence_gap_count": diagnostic.open_evidence_gap_count,
        "open_gap_items": [
            {
                "key": item.key,
                "category": item.category,
                "scope": item.scope,
                "missing_fields": list(item.missing_fields),
                "missing_count": item.missing_count,
                "references": list(item.references),
                "human_next_step": item.human_next_step,
            }
            for item in diagnostic.items_for(ReadinessClass.EVIDENCE_MISSING)
        ],
        "human_steps": [step.to_dict() for step in diagnostic.human_steps],
        "non_qualifying_evidence": [item.to_dict() for item in diagnostic.non_qualifying],
    }


def _l3_pending() -> tuple[str, ...]:
    """L3 待办清单（固定声明；**只引用**既有协议与既有命令，工具永不推进）。"""
    return (
        f"`{PHASE3_3_BLOCKER_CODE}` 的解除只能由业务方提供真实授权证据并由 **L3 人工 Gate**"
        " 决策；本包没有解除 / 放行 / 推进能力。",
        "真实材料提交后按 `GOLD-027 → GOLD-028 → GOLD-029 → GOLD-030` 顺序复核："
        "结构完整、材料级人工核验、review 批准与端到端链绑定必须**同时**成立。",
        "L4 与交易安全开关（`LIVE_TRADING` / `ALLOW_EXTERNAL_ORDER_SUBMISSION`）"
        "只能由人工按 `.ai/DEVELOPMENT_PROTOCOL.md` 变更；本包不触碰任何交易路径。",
    )


def _material_next_step(status: IntakeStatus) -> str:
    """该材料状态下的人工下一步（只陈述动作，不做资格判定）。"""
    if status is IntakeStatus.MISSING:
        return "按 handoff 契约补齐该材料后重新预检；Mock / 模板 / 推断值一律不接受。"
    if status is IntakeStatus.PRESENT_UNVERIFIED:
        return "补齐可核对的独立证据引用后重跑 GOLD-027 预检（本包不会因此解除 blocker）。"
    if status is IntakeStatus.NON_QUALIFYING:
        return (
            "该材料只来自 Mock / 模板 / 示例 / 合成候选包：**永远**不计资格，"
            "请改用真实授权证据。"
        )
    return (
        "材料结构完整：请人工核验真实性 / 法律效力 / 出处与时间语义"
        "（程序不判定，Phase 切换仍须 L3 人工 Gate）。"
    )


def _material_view(
    key: str, packages: tuple[PackageHandoff, ...]
) -> tuple[IntakeStatus, int, int, tuple[str, ...]]:
    """单个材料的只读事实：状态 / 非合成候选包数 / 合成包数 / 缺失**字段名**（不含任何值）。

    - 只有**非合成**候选包才可能携带真实材料（Mock / 模板 / 示例一律不算）；
    - 只有合成候选包 → ``NON_QUALIFYING``（**绝不**当作真实材料）；无候选包 → ``MISSING``；
    - 非合成包中任一材料缺失即 ``MISSING``（最保守），任一未验证即 ``PRESENT_UNVERIFIED``；
    - 候选包是否被 GOLD-011 / GOLD-027 **接受**由 scope 级状态与 ``preflight_pass_count`` 表达：
      材料级只报告该材料自身的事实，**不**掩盖其他材料的状态。
    """
    relevant = tuple(p for p in packages if key in {item.key for item in p.materials})
    synthetic = tuple(p for p in relevant if p.synthetic)
    real = tuple(p for p in relevant if not p.synthetic)
    if not real:
        status = IntakeStatus.NON_QUALIFYING if synthetic else IntakeStatus.MISSING
        return status, 0, len(synthetic), ()
    statuses: list[IntakeStatus] = []
    missing_fields: set[str] = set()
    for package in real:
        material = package.material(key)
        statuses.append(material.status)
        missing_fields.update(material.missing_fields)
    if any(item is IntakeStatus.MISSING for item in statuses):
        status = IntakeStatus.MISSING
    elif any(item is IntakeStatus.PRESENT_UNVERIFIED for item in statuses):
        status = IntakeStatus.PRESENT_UNVERIFIED
    elif all(item is IntakeStatus.HUMAN_VERIFICATION_REQUIRED for item in statuses):
        status = IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    else:  # 非预期状态（例如意外出现的 Gate 状态）：按最保守的"待核验" fail-closed
        status = IntakeStatus.PRESENT_UNVERIFIED
    return status, len(real), len(synthetic), tuple(sorted(missing_fields))


def _materials(
    keys: tuple[str, ...],
    specs: dict[str, MaterialSpec],
    handoff: IntakeHandoffDocument | None,
) -> tuple[MaterialRequirement, ...]:
    """逐材料的只读事实（未提供候选目录 = 全部 ``MISSING``，绝不推断）。"""
    packages: tuple[PackageHandoff, ...] = () if handoff is None else handoff.packages
    rows: list[MaterialRequirement] = []
    for key in keys:
        spec = specs[key]
        status, real_count, synthetic_count, missing_fields = _material_view(key, packages)
        reason_code = MATERIAL_MISSING_REASON_CODES[key]
        if status is IntakeStatus.PRESENT_UNVERIFIED:
            reason_code = SubmissionCode.MATERIAL_PRESENT_UNVERIFIED.value
        elif status is IntakeStatus.NON_QUALIFYING:
            reason_code = SubmissionCode.SYNTHETIC_MATERIAL_ONLY.value
        rows.append(
            MaterialRequirement(
                key=key,
                category=spec.category,
                scope=spec.scope,
                requirement=spec.requirement,
                contract_fields=spec.contract_fields,
                reference_fields=spec.reference_fields,
                declaration_fields=spec.declaration_fields,
                status=status,
                reason_code=reason_code,
                real_package_count=real_count,
                synthetic_package_count=synthetic_count,
                missing_fields=missing_fields,
                human_next_step=_material_next_step(status),
            )
        )
    return tuple(rows)


def _scope_next_step(
    scope: EvidenceScope, status: IntakeStatus, quantified_gap_open: bool
) -> str:
    """该 scope 状态下的人工下一步（只陈述动作，不做资格判定）。"""
    if status is IntakeStatus.NON_QUALIFYING:
        return (
            f"{scope.label}：只发现 Mock / 模板 / 示例候选包，**永远**不计资格；"
            "请提交真实授权材料后重跑预检。"
        )
    if status is IntakeStatus.MISSING:
        return (
            f"{scope.label}：未提供（或结构不完整）真实授权材料，"
            "请按 handoff 契约补齐后重跑预检。"
        )
    if status is IntakeStatus.PRESENT_UNVERIFIED:
        return (
            f"{scope.label}：材料已提交但独立证据引用不足，"
            "补齐引用与证据后重跑 GOLD-027 预检。"
        )
    if quantified_gap_open:
        return f"{scope.label}：材料结构完整，但既有量化门槛仍未达标（阈值只引用 evidence_gate）。"
    return (
        f"{scope.label}：材料结构完整，请人工核验真实性 / 出处 / 时间语义"
        "（Phase 切换仍须 L3 人工 Gate）。"
    )


def _scope_section(
    scope: EvidenceScope,
    *,
    specs: dict[str, MaterialSpec],
    materials: tuple[MaterialRequirement, ...],
    packages: tuple[PackageHandoff, ...],
    gap: ScopeGap | None,
) -> ScopeSubmission:
    """单个 scope 的只读事实（材料状态汇总 + 既有量化缺口，**不新增阈值**）。"""
    relevant = tuple(item for item in materials if specs[item.key].scope in ("both", scope.value))
    scope_packages = tuple(p for p in packages if p.scope == scope.value)
    real_packages = tuple(p for p in scope_packages if not p.synthetic)
    accepted_packages = tuple(p for p in real_packages if p.preflight_pass)
    synthetic_packages = tuple(p for p in scope_packages if p.synthetic)
    missing_keys = tuple(item.key for item in relevant if item.status is IntakeStatus.MISSING)
    unverified_keys = tuple(
        item.key for item in relevant if item.status is IntakeStatus.PRESENT_UNVERIFIED
    )
    if not scope_packages:
        status = IntakeStatus.MISSING
    elif not real_packages:
        status = IntakeStatus.NON_QUALIFYING if synthetic_packages else IntakeStatus.MISSING
    elif missing_keys:
        status = IntakeStatus.MISSING
    elif unverified_keys or not accepted_packages:
        # 材料未验证，或尚有非合成候选包**未通过**结构预检 → 结构未完整
        status = IntakeStatus.PRESENT_UNVERIFIED
    else:
        status = IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    quantified_gap_open = bool(gap is not None and (gap.remaining > 0 or gap.remaining_checks > 0))
    return ScopeSubmission(
        scope=scope.value,
        status=status,
        package_count=len(scope_packages),
        real_package_count=len(real_packages),
        preflight_pass_count=sum(1 for item in scope_packages if item.preflight_pass),
        synthetic_package_count=len(synthetic_packages),
        material_keys=tuple(item.key for item in relevant),
        missing_material_keys=missing_keys,
        unverified_material_keys=unverified_keys,
        quantified_gap_open=quantified_gap_open,
        eligible=0 if gap is None else gap.eligible,
        required=0 if gap is None else gap.required,
        remaining=0 if gap is None else gap.remaining,
        remaining_checks=0 if gap is None else gap.remaining_checks,
        human_next_step=_scope_next_step(scope, status, quantified_gap_open),
    )


def _scopes(
    materials: tuple[MaterialRequirement, ...],
    specs: dict[str, MaterialSpec],
    handoff: IntakeHandoffDocument | None,
    handoff_report: EvidenceHandoffReport | None,
) -> tuple[ScopeSubmission, ...]:
    """Author / News 两个 scope 的只读事实（顺序固定，保证确定性）。"""
    packages: tuple[PackageHandoff, ...] = () if handoff is None else handoff.packages
    gaps: dict[EvidenceScope, ScopeGap | None] = {
        EvidenceScope.AUTHOR: None if handoff_report is None else handoff_report.author,
        EvidenceScope.NEWS: None if handoff_report is None else handoff_report.news,
    }
    return tuple(
        _scope_section(
            scope, specs=specs, materials=materials, packages=packages, gap=gaps[scope]
        )
        for scope in (EvidenceScope.AUTHOR, EvidenceScope.NEWS)
    )


def _reason_codes(
    *,
    rehearsal: bool,
    readiness: EvidenceReadinessReport | None,
    quantified_ready: bool,
    materials: tuple[MaterialRequirement, ...],
    scopes: tuple[ScopeSubmission, ...],
    attestation: AttestationVerification | None,
    chain: ChainBinding | None,
) -> tuple[str, ...]:
    """**稳定** missing reason codes（顺序确定；每条都落到既有契约 / 命令上）。"""
    codes: list[str] = []

    def add(code: str) -> None:
        if code not in codes:
            codes.append(code)

    if rehearsal:
        add(SubmissionCode.REHEARSAL_INPUT_NON_QUALIFYING.value)
    if readiness is None:
        add(SubmissionCode.LEDGER_UNINSPECTED.value)
    elif not quantified_ready:
        add(SubmissionCode.QUANTIFIED_GAP_OPEN.value)
    if any(item.status is IntakeStatus.NON_QUALIFYING for item in materials):
        add(SubmissionCode.SYNTHETIC_MATERIAL_ONLY.value)
    if any(not scope.complete for scope in scopes):
        add(SubmissionCode.REAL_MATERIAL_MISSING.value)
    for item in materials:
        if item.status is IntakeStatus.MISSING:
            add(item.reason_code)
        elif item.status is IntakeStatus.PRESENT_UNVERIFIED:
            add(SubmissionCode.MATERIAL_PRESENT_UNVERIFIED.value)
    if attestation is None:
        add(SubmissionCode.HUMAN_ATTESTATION_MISSING.value)
    elif not attestation.verified:
        add(SubmissionCode.ATTESTATION_STALE.value)
    elif not attestation.all_required_verified:
        add(SubmissionCode.HUMAN_VERIFICATION_INCOMPLETE.value)
    if chain is None:
        add(SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value)
    elif not chain.engineering_chain_ready:
        add(SubmissionCode.ENGINEERING_CHAIN_INCOMPLETE.value)
    return tuple(codes)


def _attestation_gap(attestation: AttestationVerification | None) -> tuple[str, str] | None:
    """人工核验缺口 → ``(reason_code, 已脱敏说明)``；无缺口返回 ``None``。"""
    if attestation is None:
        return (
            SubmissionCode.HUMAN_ATTESTATION_MISSING.value,
            "尚未提供 GOLD-028 材料级人工核验凭证。",
        )
    if not attestation.verified:
        codes = "、".join(attestation.codes) or "未知"
        return (
            SubmissionCode.ATTESTATION_STALE.value,
            f"凭证与**当前**候选包 / handoff 内容身份不一致（{codes}）：旧凭证失效，必须重新核验。",
        )
    if not attestation.all_required_verified:
        return (
            SubmissionCode.HUMAN_VERIFICATION_INCOMPLETE.value,
            "凭证有效，但材料级人工核验未全部完成（存在 REJECTED / NEEDS_CHANGES / 未记录决策）。",
        )
    return None


def _human_actions(
    materials: tuple[MaterialRequirement, ...],
    scopes: tuple[ScopeSubmission, ...],
    attestation: AttestationVerification | None,
    chain: ChainBinding | None,
) -> tuple[HumanAction, ...]:
    """人工动作清单（顺序确定；每条都**只引用**既有契约 / 阈值 / 命令）。"""
    actions: list[HumanAction] = []
    if not all(item.complete for item in materials):
        actions.append(
            replace(
                _fixed_action("provide_real_materials"),
                reason_code=SubmissionCode.REAL_MATERIAL_MISSING.value,
            )
        )
    for item in materials:
        if item.complete:
            continue
        actions.append(
            HumanAction(
                key=f"provide_material:{item.key}",
                category=item.category,
                scope=item.scope,
                reason_code=item.reason_code,
                requirement=(
                    f"{item.requirement} 本次状态 `{item.status.value}`：请按契约提供 / 补齐 "
                    f"{_inline(item.contract_fields)}；独立引用 "
                    f"{_inline(item.reference_fields or item.declaration_fields)}。"
                ),
                evidence_reference="evidence-intake-v1 契约字段（GOLD-005 / GOLD-027）",
            )
        )
    for scope in scopes:
        if not scope.quantified_gap_open:
            continue
        actions.append(
            HumanAction(
                key=f"reach_quantified_thresholds:{scope.scope}",
                category="sample_size",
                scope=scope.scope,
                reason_code=SubmissionCode.QUANTIFIED_GAP_OPEN.value,
                requirement=(
                    f"达到既有量化门槛（{scope.scope}）：当前 {scope.eligible}/{scope.required}，"
                    f"仍缺 {scope.remaining}；未通过的检查项 {scope.remaining_checks} 项。"
                    "阈值只引用 `src/alpha/evidence_gate.py`，本包不新增 / 不降低。"
                ),
                evidence_reference="src/alpha/evidence_gate.py（既有门槛，只展示）",
            )
        )
    gap = _attestation_gap(attestation)
    if gap is not None:
        reason_code, detail = gap
        template = _fixed_action("complete_material_human_verification")
        actions.append(
            replace(
                template,
                reason_code=reason_code,
                requirement=f"{template.requirement} {detail}",
            )
        )
    if chain is None or not chain.engineering_chain_ready:
        template = _fixed_action("verify_tool_chain_health")
        if chain is None:
            detail = "本次未提供 GOLD-030 链审计结论（无法判断工具链是否健康）。"
        else:
            earliest = chain.earliest_failure_stage or "—"
            detail = f"工程链未全部通过：最早失败阶段 `{earliest}`（修复该段后必须重跑链审计）。"
        actions.append(
            replace(
                template,
                reason_code=(
                    SubmissionCode.ENGINEERING_CHAIN_NOT_AUDITED.value
                    if chain is None
                    else SubmissionCode.ENGINEERING_CHAIN_INCOMPLETE.value
                ),
                requirement=f"{template.requirement} {detail}",
            )
        )
    actions.append(_fixed_action("l3_human_gate_decision"))
    return tuple(actions)


# ---------------------------------------------------------------------------
# 内容身份（确定性；**不含**路径与时间，漂移必变）
# ---------------------------------------------------------------------------
def compute_pack_facts_digest(
    *,
    evidence_source: str,
    rehearsal: bool,
    materials: tuple[MaterialRequirement, ...],
    scopes: tuple[ScopeSubmission, ...],
    missing_reason_codes: tuple[str, ...],
    conclusions: tuple[tuple[str, bool], ...],
    chain: ChainBinding | None,
    attestation: AttestationVerification | None,
) -> str:
    """由材料 / scope 事实与结论派生 ``facts_digest``（确定性；不含路径与审计时点）。

    ⚠️ 刻意**不含**路径与时间：同一份提交材料无论在什么目录、什么时点计算，都得到**同一**摘要；
    材料 / 结论 / 凭证 / 链路漂移必然改变摘要。
    """
    payload = {
        "evidence_source": evidence_source,
        "rehearsal": rehearsal,
        "materials": [[item.key, item.status.value] for item in materials],
        "scopes": [
            [
                item.scope,
                item.status.value,
                item.package_count,
                item.real_package_count,
                item.synthetic_package_count,
                item.eligible,
                item.required,
                item.remaining,
                item.remaining_checks,
            ]
            for item in scopes
        ],
        "missing_reason_codes": list(missing_reason_codes),
        "conclusions": [[name, value] for name, value in conclusions],
        "chain_id": None if chain is None else chain.chain_id,
        "attestation_id": None if attestation is None else attestation.attestation_id,
    }
    return hashing.sha256_text(_canonical_json(payload))


def compute_pack_id(*, facts_digest: str, evidence_source: str, rehearsal: bool) -> str:
    """内容级 ``pack_id``：策略块 + 证据来源 + ``facts_digest``（确定性）。"""
    payload = {
        "policy": _policy_block(),
        "evidence_source": evidence_source,
        "rehearsal": rehearsal,
        "facts_digest": facts_digest,
    }
    return hashing.sha256_text(_canonical_json(payload))


def build_submission_pack(
    readiness: EvidenceReadinessReport | None,
    *,
    moment: datetime,
    handoff: IntakeHandoffDocument | None = None,
    attestation: AttestationVerification | None = None,
    chain: ChainBinding | None = None,
    evidence_source: str = EVIDENCE_SOURCE_OPERATOR_PROVIDED,
) -> SubmissionReadinessPack:
    """构造人工证据提交就绪包（**纯函数**；不触库、不联网、不写文件）。

    Args:
        readiness: :func:`src.monitoring.evidence_readiness.build_readiness_report` 的结果；
            ``None`` 表示本次**未读取**库内台账（缺口不可知 → fail-closed 记
            ``SUBMISSION_LEDGER_UNINSPECTED``，绝不按"无缺口"处理）。
        moment: 审计时点（必须带时区；只作为 ``generated_at``，**不做任何证据时间填补**）。
        handoff: 可选的 GOLD-027 intake handoff 文档（**当前**候选目录的只读预检结论）。
        attestation: 可选的 GOLD-028 凭证**核验**结论（必须是对**当前**候选目录的核验）。
        chain: 可选的 GOLD-030 链审计绑定（工程链是否一致）。
        evidence_source: ``operator_provided`` 或 ``rehearsal_fixture``（受控词表）。

    Returns:
        :class:`SubmissionReadinessPack`；``data_qualification_passed`` /
        ``phase_transition_allowed`` **恒为 ``False``**，``l3_gate_pending`` /
        ``blocker_active`` / ``human_gate_required`` **恒为 ``True``**（**硬编码**）。

    Raises:
        SubmissionPackArgumentError: 时点缺时区 / ``evidence_source`` 非法 /
            演练模式下混入真实候选目录或凭证（fail-closed）。
        SubmissionPackError: 材料契约漂移（见 :func:`_verify_material_contract`）。
    """
    moment = _require_aware(moment, field_name="moment")
    if evidence_source not in EVIDENCE_SOURCES:
        allowed = ", ".join(EVIDENCE_SOURCES)
        raise SubmissionPackArgumentError(f"evidence_source 必须是 {allowed} 之一")
    rehearsal = evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    if rehearsal and (handoff is not None or attestation is not None):
        raise SubmissionPackArgumentError(
            "rehearsal_fixture 只接受 fixture / Mock 输入：不得同时给出真实候选目录或凭证"
            "（fail-closed：fixture 绝不与真实材料混算）"
        )
    keys = _verify_material_contract()
    specs = _material_specs()
    handoff_report = None if readiness is None else build_handoff_report(readiness)
    materials = _materials(keys, specs, handoff)
    scopes = _scopes(materials, specs, handoff, handoff_report)
    quantified_ready = bool(readiness is not None and readiness.ready)
    engineering_ready = bool(chain is not None and chain.engineering_chain_ready)
    submission_materials_complete = bool(
        (not rehearsal)
        and handoff is not None
        and handoff.scanned
        and all(item.complete for item in materials)
        and all(scope.complete for scope in scopes)
    )
    human_verification_complete = bool(
        (not rehearsal)
        and attestation is not None
        and attestation.verified
        and attestation.all_required_verified
    )
    missing_reason_codes = _reason_codes(
        rehearsal=rehearsal,
        readiness=readiness,
        quantified_ready=quantified_ready,
        materials=materials,
        scopes=scopes,
        attestation=attestation,
        chain=chain,
    )
    conclusions: tuple[tuple[str, bool], ...] = (
        ("engineering_ready", engineering_ready),
        ("submission_materials_complete", submission_materials_complete),
        ("human_verification_complete", human_verification_complete),
        ("quantified_ready", quantified_ready),
    )
    facts_digest = compute_pack_facts_digest(
        evidence_source=evidence_source,
        rehearsal=rehearsal,
        materials=materials,
        scopes=scopes,
        missing_reason_codes=missing_reason_codes,
        conclusions=conclusions,
        chain=chain,
        attestation=attestation,
    )
    notes: list[str] = [
        SUBMISSION_PACK_NOTE,
        SUBMISSION_PACK_SEMANTICS_NOTE,
        SUBMISSION_PACK_TIME_SEMANTICS_NOTE,
    ]
    if rehearsal:
        notes.append(SUBMISSION_PACK_REHEARSAL_NOTE)
    return SubmissionReadinessPack(
        generated_at=moment.astimezone(UTC),
        inbox_dir=None if handoff is None else handoff.inbox_dir,
        evidence_source=evidence_source,
        rehearsal=rehearsal,
        pack_id=compute_pack_id(
            facts_digest=facts_digest, evidence_source=evidence_source, rehearsal=rehearsal
        ),
        facts_digest=facts_digest,
        engineering_ready=engineering_ready,
        submission_materials_complete=submission_materials_complete,
        human_verification_complete=human_verification_complete,
        quantified_ready=quantified_ready,
        missing_reason_codes=missing_reason_codes,
        materials=materials,
        scopes=scopes,
        human_actions=_human_actions(materials, scopes, attestation, chain),
        l3_pending=_l3_pending(),
        thresholds=tuple(sorted(thresholds().items())),
        time_semantics_requirements=TIME_SEMANTICS_REQUIREMENTS,
        gap=_gap_summary(None if readiness is None else build_gap_diagnostic(readiness)),
        chain=chain,
        attestation=None if attestation is None else attestation.to_dict(),
        notes=tuple(notes),
    )


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（复用既有 ``atomic_write_text``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise SubmissionPackWriteError(
            f"{what}写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def load_submission_pack(
    session: Session,
    *,
    moment: datetime,
    inbox_dir: str | Path | None = None,
    attestation_path: str | Path | None = None,
    chain: ChainBinding | None = None,
    evidence_source: str = EVIDENCE_SOURCE_OPERATOR_PROVIDED,
) -> SubmissionReadinessPack:
    """读取只读事实并构造提交就绪包（**严格只读 / 零网络 / 零写入**）。

    数据来源（全部只读）：

    - 库内证据台账 → GOLD-006 readiness（**只 SELECT**；调用方负责回滚 / 关闭会话）；
    - ``inbox_dir`` → GOLD-027 只读预检（缺省 = 未提供候选目录，按"无材料"报告）；
    - ``attestation_path`` → GOLD-028 防伪核验（**必须**与 ``inbox_dir`` 同时给出）。

    Raises:
        SubmissionPackArgumentError: 时点缺时区 / 只给凭证不给候选目录 /
            ``evidence_source`` 非法 / 演练模式混入真实输入（fail-closed）。
        src.evidence.inbox.InboxDirError: ``inbox_dir`` 不存在 / 不是目录 / 不可读。
        src.evidence.human_verification_attestation.AttestationError: 凭证缺失 / 损坏 / 被改写。
    """
    moment = _require_aware(moment, field_name="moment")
    if evidence_source not in EVIDENCE_SOURCES:
        allowed = ", ".join(EVIDENCE_SOURCES)
        raise SubmissionPackArgumentError(f"evidence_source 必须是 {allowed} 之一")
    rehearsal = evidence_source == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    if rehearsal and (inbox_dir is not None or attestation_path is not None):
        raise SubmissionPackArgumentError(
            "rehearsal_fixture 只接受 fixture / Mock 输入："
            "不得同时给出 --inbox-dir / --attestation（fail-closed）"
        )
    handoff = None if inbox_dir is None else load_intake_handoff(Path(inbox_dir), as_of=moment)
    attestation: AttestationVerification | None = None
    if attestation_path is not None:
        if inbox_dir is None:
            raise SubmissionPackArgumentError(
                "提供 --attestation 时必须同时给出 --inbox-dir：凭证必须与**当前**候选目录比对"
            )
        attestation = verify_attestation(attestation_path, inbox_dir, moment=moment)
    readiness = build_readiness_report(
        load_evidence_ledger(session),
        as_of=moment,
        scopes=(EvidenceScope.AUTHOR, EvidenceScope.NEWS),
    )
    return build_submission_pack(
        readiness,
        moment=moment,
        handoff=handoff,
        attestation=attestation,
        chain=chain,
        evidence_source=evidence_source,
    )


def run_submission_pack(
    session: Session,
    *,
    moment: datetime,
    inbox_dir: str | Path | None = None,
    attestation_path: str | Path | None = None,
    chain: ChainBinding | None = None,
    evidence_source: str = EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> SubmissionReadinessPack:
    """执行**一次**提交就绪包生成（默认只读；只有显式 ``out_path`` 才写本包本身）。

    安全语义：

    - 所有输入 artifact 只读；本层**没有** intake / commit / 数据库写入 / 网络调用，
      **绝不**移动 / 删除 / 改写 inbox 内原始 evidence 或任何历史 artifact；
    - 只有 ``out_path`` 才写**本包本身**，且先取**单实例锁**（锁冲突 → 稳定异常，零写入）；
    - 输出落在 inbox 内会被拒绝（``SubmissionPackPathError``）；
    - **绝不**修改 ``Review Ledger`` / ``PROJECT_STATE`` / ``.ai/tasks`` / ``.ai/results``，
      **绝不**解除 ``PHASE3_3_DATA``、**绝不**切换 Phase。

    Raises:
        SubmissionPackArgumentError: 时点缺时区 / 参数非法。
        SubmissionPackPathError: 输出 artifact 与 inbox 冲突。
        SubmissionPackWriteError: 输出原子写失败（fail-closed）。
        runner.LockConflictError: 另一处写入正持有活动锁（fail-closed，零写入）。
    """
    if out_path is None:
        return load_submission_pack(
            session,
            moment=moment,
            inbox_dir=inbox_dir,
            attestation_path=attestation_path,
            chain=chain,
            evidence_source=evidence_source,
        )
    out = Path(out_path)
    if inbox_dir is not None:
        try:
            ensure_outside_inbox(Path(inbox_dir), out)
        except InboxError as exc:
            raise SubmissionPackPathError(_safe(str(exc), max_chars=300)) from exc
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + SUBMISSION_PACK_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        pack = load_submission_pack(
            session,
            moment=moment,
            inbox_dir=inbox_dir,
            attestation_path=attestation_path,
            chain=chain,
            evidence_source=evidence_source,
        )
        _atomic_write_json(out, pack.to_dict(), what="人工证据提交就绪包")
    return replace(pack, written_path=_safe_path(out))


def render_submission_pack_markdown(pack: SubmissionReadinessPack) -> str:
    """渲染人类可读摘要（脱敏；**不解除** blocker、**不**代表资格通过）。"""
    rehearsal_note = "（演练：fixture / Mock，**不是**真实证据）" if pack.rehearsal else ""
    lines: list[str] = [
        "# Phase 3.3 人工证据提交就绪包（**只读**，不是资格判定器）",
        "",
        f"- 审计时点（UTC）：{pack.generated_at.isoformat()}",
        f"- 证据来源：`{pack.evidence_source}`{rehearsal_note}",
        f"- pack_id：`{pack.pack_id}`；facts_digest：`{pack.facts_digest}`",
        f"- engineering_ready：**{str(pack.engineering_ready).lower()}**；"
        f"submission_materials_complete：**{str(pack.submission_materials_complete).lower()}**；"
        f"human_verification_complete：**{str(pack.human_verification_complete).lower()}**；"
        f"量化门槛（库内既有口径）：{str(pack.quantified_ready).lower()}",
        "- `data_qualification_passed`：**false（恒）**；"
        "`phase_transition_allowed`：**false（恒）**；"
        f"`l3_gate_pending`：**{str(pack.l3_gate_pending).lower()}（恒）**；"
        f"`{PHASE3_3_BLOCKER_CODE}` 保持 BLOCKED（本包无解除能力）。",
        "",
        "## 1. 稳定缺口原因码",
        "",
    ]
    lines += [f"- `{code}`" for code in pack.missing_reason_codes] or ["- 无"]
    lines += [
        "",
        "## 2. 人工动作清单（blocking 全部为 true）",
        "",
        "| 动作 | 范围 | 原因码 | 要求 | 证据入口 |",
        "|---|---|---|---|---|",
    ]
    for action in pack.human_actions:
        lines.append(
            f"| `{action.key}` | {action.scope} | `{action.reason_code}` | "
            f"{action.requirement} | {action.evidence_reference} |"
        )
    lines += [
        "",
        "## 3. 材料逐项状态（GOLD-027 契约，**只报事实**）",
        "",
        "| 材料 | 范围 | 状态 | 原因码 | 真实包 | 合成包 | 缺失字段 | 人工下一步 |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for material in pack.materials:
        fields = ", ".join(f"`{name}`" for name in material.missing_fields) or "—"
        lines.append(
            f"| `{material.key}` | {material.scope} | {material.status.value} | "
            f"`{material.reason_code}` | {material.real_package_count} | "
            f"{material.synthetic_package_count} | {fields} | {material.human_next_step} |"
        )
    lines += [
        "",
        "## 4. scope 汇总",
        "",
        "| 范围 | 状态 | 候选包 | 真实包 | 合成包 | eligible/required | 量化缺口 |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for scoped in pack.scopes:
        lines.append(
            f"| {scoped.scope} | {scoped.status.value} | {scoped.package_count} | "
            f"{scoped.real_package_count} | {scoped.synthetic_package_count} | "
            f"{scoped.eligible}/{scoped.required} | {str(scoped.quantified_gap_open).lower()} |"
        )
    lines += ["", "## 5. 工程链（GOLD-030）与人工核验（GOLD-028）", ""]
    if pack.chain is None:
        lines.append("- 未提供链审计结论（`--chain-audit`）：工程链状态**不可知**（fail-closed）。")
    else:
        earliest = pack.chain.earliest_failure_stage or "—"
        lines.append(
            f"- 链审计：来源 `{pack.chain.evidence_source}`；"
            f"engineering_chain_ready={str(pack.chain.engineering_chain_ready).lower()}；"
            f"最早失败阶段：`{earliest}`；chain_id=`{pack.chain.chain_id}`"
        )
    if pack.attestation is None:
        lines.append("- 未提供 GOLD-028 材料级人工核验凭证（`--attestation`）。")
    else:
        lines.append(
            "- 凭证核验："
            f"verified={str(pack.attestation.get('verified')).lower()}；"
            f"all_required_verified={str(pack.attestation.get('all_required_verified')).lower()}"
        )
    lines += ["", "## 6. L3 待办（恒为待办：工具永不推进）", ""]
    lines += [f"- {item}" for item in pack.l3_pending]
    lines += ["", "## 7. 既有阈值（只展示，不新增 / 不降低）", ""]
    lines += [f"- {name}={value}" for name, value in pack.thresholds]
    lines += ["", "## 8. 独立时间语义要求（引用既有契约）", ""]
    lines += [f"- {item}" for item in pack.time_semantics_requirements]
    lines += ["", "## 9. 口径与边界", ""]
    lines += [f"- {note}" for note in pack.notes]
    lines.append("")
    return "\n".join(lines)
