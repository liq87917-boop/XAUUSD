"""PHASE3_3_DATA 人工证据缺口的只读 Readiness Diagnostic（GOLD-026）。

目的：在**不采集、不伪造、不放宽资格门槛**的前提下，把 ``PHASE3_3_DATA`` 当前缺失的
真实授权 Author / News 证据整理成**单一、确定、只读、可操作**的缺口诊断，供 GPT 与人工
判断"还缺什么、必须由谁完成"。

职责边界（**重要**，与 ``.clinerules`` 与 ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）：

- **只读 / 零网络 / 零写入**：不抓取站点、不绕过 robots / 条款 / 证书、不申请证书豁免、
  不写数据库、不做模型训练、不触碰交易；测试与运行都只消费**本地**台账、既有只读报告与
  **显式给出的本地候选目录**；
- **复用而不复制**：缺口与阈值完全复用既有能力 ——
  :func:`src.monitoring.evidence_readiness.build_readiness_report`（阈值来自
  ``src.alpha.evidence_gate``）+ :func:`src.evidence.handoff.build_handoff_report`
  （缺口 / checklist / 不计资格证据）+ :func:`src.evidence.inbox.scan_inbox`
  （manifest 级只读事实）+ :mod:`src.evidence.contracts`（契约字段名 / 原因码词表）。
  本模块**不新增、不降低任何资格阈值**，也不重新解释任何契约字段；
- **四态分类**（机器可读，稳定字符串）：每个诊断项落在
  :class:`ReadinessClass` 之一 —— ``CODE_READY``（代码 / 契约侧已就绪）/
  ``EVIDENCE_MISSING``（真实证据缺失或未 ingest）/
  ``HUMAN_VERIFICATION_REQUIRED``（证据已在，但法律效力 / 出处 / 时间语义只能人工核验）/
  ``GATE_BLOCKED``（资格门禁与 L3/L4 人工 Gate，工具永不具备解除能力）；
- **Mock / 模板 / 示例永不 qualified**：所有非真实证据都以
  ``counts_toward_eligibility=false`` 显式列出，任何分类都不会因合成语料而变绿；
- **不伪造缺失字段**：``published_at`` / ``collected_at`` / ``effective_at`` /
  ``available_at`` / OOS 证据缺失时，本模块**只报告事实与人工下一步**，
  **绝不**用当前时间、文件 mtime、抓取时间或任何推断值填补；
- **fail-closed**：证据不足时顶层结论恒为 ``GATE_BLOCKED``，
  ``advance_allowed=false``、``l3_l4_auto_advance_allowed=false``，
  且 ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 ``False``；
- **脱敏**：只输出白名单标量（计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名与声明文本 /
  时间），**绝不**输出正文、token / API key / Authorization 或完整 source config。

顶层结论的诚实语义：``readiness`` 恒为 ``GATE_BLOCKED`` —— 即使量化门槛全部达标，
``PHASE3_3_DATA`` 的解除条件仍是"真实授权证据 + 人工 Gate"，代码完成或本诊断运行成功
**都不算**资格通过；Phase 切换仍是 ``.ai/DEVELOPMENT_PROTOCOL.md`` 的 **L3 人工确认**。

入口：``scripts/evidence_gap_diagnostic.py``（默认只打印 stdout，唯一写开关是显式 ``--out``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.redaction import safe_text
from src.evidence.contracts import (
    ELIGIBILITY_REASON_CODES,
    EVIDENCE_CONTRACT_VERSION,
    PERMISSION_FIELDS,
    QUARANTINE_REASON_CODES,
    EvidenceScope,
    ReasonCode,
    required_field_names,
)
from src.evidence.handoff import (
    HANDOFF_NOTE,
    EvidenceHandoffReport,
    ScopeGap,
    build_handoff_report,
    thresholds,
)
from src.evidence.inbox import (
    MANIFEST_REQUIRED_FIELDS,
    CandidatePackage,
    InboxPreflightReport,
    InboxStatus,
    scan_inbox,
)
from src.evidence.ledger import load_evidence_ledger
from src.monitoring.evidence_readiness import (
    READINESS_NOTE,
    EvidenceReadinessReport,
    ReadinessCheck,
    build_readiness_report,
)
from src.monitoring.phase33_qualification import (
    EVIDENCE_INTAKE_NOTE,
    PHASE3_3_BLOCKER_CODE,
)

__all__ = [
    "AUTHORIZATION_CONTRACT_FIELDS",
    "AVAILABILITY_CONTRACT_FIELDS",
    "DIAGNOSTIC_KIND",
    "DIAGNOSTIC_NOTE",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "GAP_CATEGORIES",
    "MANDATORY_HUMAN_STEPS",
    "TIME_CONTRACT_FIELDS",
    "DiagnosticItem",
    "EvidenceGapDiagnostic",
    "HumanStep",
    "HumanStepSpec",
    "ManifestDiagnostic",
    "ManifestFact",
    "NonQualifyingEvidenceItem",
    "ReadinessClass",
    "StepVerification",
    "build_gap_diagnostic",
    "build_manifest_diagnostic",
    "load_gap_diagnostic",
    "render_gap_diagnostic_markdown",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
DIAGNOSTIC_SCHEMA_VERSION: Final[int] = 1
#: 报告 / 文档标识（稳定，供上游与人工日志解析）
DIAGNOSTIC_KIND: Final[str] = "phase33_evidence_gap_diagnostic"
#: 固定说明：诊断本身不具备解除 blocker / 推进 Phase 的能力
DIAGNOSTIC_NOTE: Final[str] = (
    "本诊断**只读**聚合既有 readiness / handoff / manifest 能力：不联网、不抓取、"
    "不绕过 robots / 证书、不写数据库、不训练模型、不触碰交易；"
    f"证据不足时 `{PHASE3_3_BLOCKER_CODE}` **保持 BLOCKED**，"
    "`advance_allowed` / `l3_l4_auto_advance_allowed` 恒为 false，"
    "Phase 切换只能由 L3 人工按 `.ai/DEVELOPMENT_PROTOCOL.md` 决定。"
    "缺失字段只报告事实与人工下一步：**绝不**用当前时间 / 文件 mtime / 抓取时间 / "
    "推断值填补 `published_at` / `collected_at` / `effective_at` / `available_at` / OOS 证据。"
)

#: 诊断分类（顺序即输出顺序；对齐既有契约分组，不新增业务类别）
GAP_CATEGORIES: Final[tuple[str, ...]] = (
    "contract",
    "gate",
    "authorization",
    "source",
    "published_at",
    "collected_at",
    "effective_at",
    "availability_oos",
    "time_semantics",
    "sample_size",
    "coverage",
)

#: 现有契约（``evidence-intake-v1``）中与授权声明相关的字段名（**只引用**，不新增字段）
AUTHORIZATION_CONTRACT_FIELDS: Final[tuple[str, ...]] = (
    "authorization_status",
    "authorization_basis",
    "authorization_reference",
    "authorization_reviewed_by",
    "authorization_reviewed_at",
    "authorization_valid_from",
    "authorization_expires_at",
    *PERMISSION_FIELDS,
)
#: 现有契约中与"独立历史可用证据"相关的字段名（**只引用**，不新增字段）
AVAILABILITY_CONTRACT_FIELDS: Final[tuple[str, ...]] = (
    "available_at",
    "availability_provenance",
    "availability_reference",
)
#: 现有契约中的时间字段（``effective_at`` 由契约 ``max(published_at, collected_at)`` 派生）
TIME_CONTRACT_FIELDS: Final[tuple[str, ...]] = ("published_at", "collected_at", "effective_at")
#: manifest 中与独立时间语义相关的**声明**字段（来自 ``src.evidence.inbox``，不另造）
TIME_SEMANTICS_DECLARATION_FIELDS: Final[tuple[str, ...]] = (
    "time_semantics",
    "availability_semantics",
)

class ReadinessClass(StrEnum):
    """诊断项的四态分类（稳定字符串；只有 ``GATE_BLOCKED`` 是顶层结论）。"""

    #: 代码 / 契约侧已就绪（绝不等于数据资格通过）
    CODE_READY = "CODE_READY"
    #: 真实证据缺失、未 ingest 或未达标（fail-closed 的缺口）
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    #: 证据已在，但法律效力 / 出处 / 时间语义只能人工核验
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    #: 资格门禁 / L3-L4 人工 Gate：工具永不具备解除或推进能力
    GATE_BLOCKED = "GATE_BLOCKED"


#: 分类的固定输出顺序（计数与渲染共用，保证确定性）
READINESS_CLASS_ORDER: Final[tuple[ReadinessClass, ...]] = (
    ReadinessClass.CODE_READY,
    ReadinessClass.EVIDENCE_MISSING,
    ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
    ReadinessClass.GATE_BLOCKED,
)


class StepVerification(StrEnum):
    """人工步骤的机器可核验程度（最保守优先）。

    - ``MACHINE_CONFIRMED``：机器**可确认**该前置条件已满足（仍不解除 blocker）；
    - ``MACHINE_UNSATISFIED``：机器**可确认**该前置条件尚未满足（fail-closed）；
    - ``HUMAN_ONLY``：程序无法确认，必须由人工完成 / 核验（默认、最保守）。
    """

    MACHINE_CONFIRMED = "MACHINE_CONFIRMED"
    MACHINE_UNSATISFIED = "MACHINE_UNSATISFIED"
    HUMAN_ONLY = "HUMAN_ONLY"


@dataclass(frozen=True, slots=True)
class HumanStepSpec:
    """必须由人工完成的步骤的**固定声明**（不含运行期事实）。"""

    step: str
    category: str
    scope: str
    requirement: str
    evidence_reference: str


#: 必须人工完成的步骤（固定顺序；阈值文本由既有常量格式化，绝不另造数字）
MANDATORY_HUMAN_STEPS: Final[tuple[HumanStepSpec, ...]] = (
    HumanStepSpec(
        step="provide_authorized_evidence",
        category="authorization",
        scope="both",
        requirement=(
            "提供真实授权的 Author / News 原始证据（含授权声明与三项用途许可），"
            "由人工按 `evidence-intake-v1` 显式 intake；普通 CSV / Mock / 模板不计资格"
        ),
        evidence_reference=(
            "scripts/evidence_readiness.py preflight → scripts/intake_evidence"
            "（--no-dry-run）→ scripts/evidence_readiness.py recheck"
        ),
    ),
    HumanStepSpec(
        step="verify_authorization_validity",
        category="authorization",
        scope="both",
        requirement=(
            "人工核验授权的法律效力与许可范围（`authorization_status` / `basis` / "
            "`reference` / `reviewed_by`（须为真人）/ `reviewed_at` + "
            "`permits_automated_collection` / `permits_local_storage` / `permits_research_use`）"
        ),
        evidence_reference="evidence-intake-v1 authorization_* 字段 + docs/legal/ 内的许可文件",
    ),
    HumanStepSpec(
        step="verify_independent_availability",
        category="availability_oos",
        scope="both",
        requirement=(
            "为每条记录提供独立历史可用证据并人工核验其出处"
            "（`available_at` + `availability_provenance` + `availability_reference`）；"
            "**不得**用 `published_at` / `collected_at` / 今天采集时间 / 文件 mtime 代替"
        ),
        evidence_reference="evidence-intake-v1 availability 字段（AVAILABILITY_UNPROVEN 不计 OOS）",
    ),
    HumanStepSpec(
        step="reach_quantified_thresholds",
        category="sample_size",
        scope="both",
        requirement=(
            f"达到既有量化门槛：Author 可信样本 ≥ {MIN_AUTHOR_SAMPLES} 条、"
            f"News 事件 ≥ {MIN_NEWS_EVENTS} 条、News 独立可用时间覆盖 ≥ "
            f"{MIN_NEWS_HISTORY_DAYS} 天、单一来源占比 ≤ {MAX_NEWS_SOURCE_SHARE:.0%}"
        ),
        evidence_reference="src/alpha/evidence_gate.py（本诊断只展示，不新增 / 不降低阈值）",
    ),
    HumanStepSpec(
        step="exclude_synthetic_evidence",
        category="contract",
        scope="both",
        requirement=(
            "确认候选语料不含 Mock / 模板 / 示例行（`record_kind=example` / `is_mock=true` / "
            "`synthetic=true` 一律判 SYNTHETIC_EVIDENCE 且永不计资格）"
        ),
        evidence_reference="src/evidence/contracts.py（EXAMPLE_MARKER_* / SYNTHETIC_EVIDENCE）",
    ),
    HumanStepSpec(
        step="l3_human_gate_decision",
        category="gate",
        scope="both",
        requirement=(
            f"`{PHASE3_3_BLOCKER_CODE}` 的解除与后续 Phase 切换只能由 L3 人工按开发协议决策；"
            "任何工具（含本诊断）都不会自动推进"
        ),
        evidence_reference=".ai/DEVELOPMENT_PROTOCOL.md（L3 / L4 人工 Gate）",
    ),
)


@dataclass(frozen=True, slots=True)
class HumanStep:
    """一条必须人工完成的步骤（含运行期核验状态；``blocking`` 恒为 True）。"""

    step: str
    category: str
    scope: str
    requirement: str
    evidence_reference: str
    verification: StepVerification
    blocking: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "category": self.category,
            "scope": self.scope,
            "requirement": self.requirement,
            "evidence_reference": self.evidence_reference,
            "verification": self.verification.value,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class NonQualifyingEvidenceItem:
    """明确**不计资格**的证据形态（``counts_toward_eligibility`` 恒为 False）。"""

    kind: str
    reason_code: str
    detail: str
    detected_count: int
    counts_toward_eligibility: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "detected_count": self.detected_count,
            "counts_toward_eligibility": False,
        }


@dataclass(frozen=True, slots=True)
class ManifestFact:
    """单个本地候选包的 manifest **字段级事实**（只读 / 脱敏；不含正文与原始值）。"""

    package_dir: str
    fingerprint: str
    scope: str
    status: str
    synthetic: bool
    declared_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    time_semantics: str
    availability_semantics: str
    historical_oos_applicable: bool | None
    reason_codes: tuple[str, ...]
    acceptable_rows: int
    quarantined_rows: int
    not_oos_eligible_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_dir": self.package_dir,
            "fingerprint": self.fingerprint,
            "scope": self.scope,
            "status": self.status,
            "synthetic": self.synthetic,
            "declared_fields": list(self.declared_fields),
            "missing_fields": list(self.missing_fields),
            "time_semantics": self.time_semantics,
            "availability_semantics": self.availability_semantics,
            "historical_oos_applicable": self.historical_oos_applicable,
            "reason_codes": list(self.reason_codes),
            "acceptable_rows": self.acceptable_rows,
            "quarantined_rows": self.quarantined_rows,
            "not_oos_eligible_rows": self.not_oos_eligible_rows,
            # 候选 ≠ 授权已核验 ≠ 资格通过：恒为 false（硬编码，不被输入透传影响）
            "qualifies": False,
        }


@dataclass(frozen=True, slots=True)
class ManifestDiagnostic:
    """本地候选目录（manifest）的只读诊断段（``scanned=false`` 表示未提供目录）。"""

    inbox_dir: str | None
    scanned: bool
    facts: tuple[ManifestFact, ...]
    notes: tuple[str, ...]

    @property
    def preflight_pass_count(self) -> int:
        return sum(1 for fact in self.facts if fact.status == InboxStatus.PREFLIGHT_PASS.value)

    @property
    def quarantined_count(self) -> int:
        return sum(1 for fact in self.facts if fact.status == InboxStatus.QUARANTINED.value)

    @property
    def synthetic_count(self) -> int:
        return sum(1 for fact in self.facts if fact.synthetic)

    def for_scope(self, scope: EvidenceScope | str) -> tuple[ManifestFact, ...]:
        name = scope.value if isinstance(scope, EvidenceScope) else str(scope)
        return tuple(fact for fact in self.facts if fact.scope == name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inbox_dir": self.inbox_dir,
            "scanned": self.scanned,
            "package_count": len(self.facts),
            "preflight_pass_count": self.preflight_pass_count,
            "quarantined_count": self.quarantined_count,
            "synthetic_count": self.synthetic_count,
            "packages": [fact.to_dict() for fact in self.facts],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    """一条机器可读的缺口 / 就绪诊断项（只陈述事实与人工下一步，绝不填补缺失值）。"""

    key: str
    category: str
    scope: str
    readiness: ReadinessClass
    machine_fact: str
    human_next_step: str
    current: int | float | str | bool | None
    required: int | float | str | None
    missing_fields: tuple[str, ...]
    missing_count: int | None
    references: tuple[str, ...]
    evidence_start: str | None = None
    evidence_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "readiness": self.readiness.value,
            "machine_fact": self.machine_fact,
            "human_next_step": self.human_next_step,
            "current": self.current,
            "required": self.required,
            "missing_fields": list(self.missing_fields),
            "missing_count": self.missing_count,
            "references": list(self.references),
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
            # Mock / 模板 / 示例永不计资格：恒为 false（硬编码，不被输入透传影响）
            "mock_or_template_qualifies": False,
        }


@dataclass(frozen=True, slots=True)
class EvidenceGapDiagnostic:
    """``PHASE3_3_DATA`` 的只读缺口诊断（稳定 JSON + 可读 Markdown 的同一事实来源）。"""

    schema_version: int
    kind: str
    report: str
    contract_version: str
    as_of: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    data_qualification_passed: bool
    phase_transition_allowed: bool
    gate_blocked: bool
    advance_allowed: bool
    l3_l4_auto_advance_allowed: bool
    readiness: ReadinessClass
    class_counts: tuple[tuple[str, int], ...]
    items: tuple[DiagnosticItem, ...]
    human_steps: tuple[HumanStep, ...]
    non_qualifying: tuple[NonQualifyingEvidenceItem, ...]
    manifest: ManifestDiagnostic
    thresholds: tuple[tuple[str, float], ...]
    notes: tuple[str, ...]

    @property
    def open_evidence_gap_count(self) -> int:
        """仍为 ``EVIDENCE_MISSING`` 的诊断项数量（> 0 表示真实证据仍有缺口）。"""
        return self.count(ReadinessClass.EVIDENCE_MISSING)

    def count(self, readiness: ReadinessClass) -> int:
        """指定分类的诊断项数量。"""
        return sum(1 for item in self.items if item.readiness is readiness)

    def items_for(self, readiness: ReadinessClass) -> tuple[DiagnosticItem, ...]:
        """指定分类的诊断项（保持确定性顺序）。"""
        return tuple(item for item in self.items if item.readiness is readiness)

    def item(self, key: str) -> DiagnosticItem:
        """按 key 取诊断项；缺失即显式失败（防止口径漂移被静默忽略）。"""
        for item in self.items:
            if item.key == key:
                return item
        raise KeyError(key)

    def step(self, step: str) -> HumanStep:
        """按 key 取人工步骤；缺失即显式失败。"""
        for item in self.human_steps:
            if item.step == step:
                return item
        raise KeyError(step)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "report": self.report,
            "contract_version": self.contract_version,
            "as_of": self.as_of.isoformat(),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "gate_blocked": self.gate_blocked,
            "advance_allowed": self.advance_allowed,
            "l3_l4_auto_advance_allowed": self.l3_l4_auto_advance_allowed,
            "readiness": self.readiness.value,
            "class_counts": {name: count for name, count in self.class_counts},
            "open_evidence_gap_count": self.open_evidence_gap_count,
            "thresholds": {name: value for name, value in self.thresholds},
            "items": [item.to_dict() for item in self.items],
            "human_steps": [step.to_dict() for step in self.human_steps],
            "non_qualifying_evidence": [item.to_dict() for item in self.non_qualifying],
            "manifest": self.manifest.to_dict(),
            "notes": list(self.notes),
        }


# ---- 内部常量（只做引用与排序，不复制业务口径）------------------------------
_SCOPE_ORDER: Final[tuple[EvidenceScope, ...]] = (EvidenceScope.AUTHOR, EvidenceScope.NEWS)
_SCOPE_LABELS: Final[dict[str, str]] = {"author": "Author", "news": "News", "both": "Author+News"}
_SCOPE_RANK: Final[dict[str, int]] = {"both": 0, "author": 1, "news": 2}
_CATEGORY_RANK: Final[dict[str, int]] = {
    name: index for index, name in enumerate(GAP_CATEGORIES)
}
#: 量化门槛类分类（用于"量化门槛是否全部达标"这一步的机器判定）
_QUANTIFIED_CATEGORIES: Final[frozenset[str]] = frozenset({"sample_size", "coverage"})
#: 未知 ``evidence_type``（无法归属 Author / News）的候选包 scope 标记
_UNKNOWN_SCOPE: Final[str] = "unknown"
#: 证据入口必须存在的最低可观测信息（缺失即 fail-closed，绝不静默）
_MINIMUM_MANIFEST_FIELDS: Final[tuple[str, ...]] = MANIFEST_REQUIRED_FIELDS

MANIFEST_SCANNED_NOTE: Final[str] = (
    "manifest 段只复用 GOLD-011 的**只读**预检事实（`src.evidence.inbox.scan_inbox`）："
    "候选 ≠ 授权已核验 ≠ 资格通过，`qualifies` 恒为 false；模板 / Mock / 示例永不达标。"
)
MANIFEST_NOT_SCANNED_NOTE: Final[str] = (
    "未提供本地候选目录（`--inbox-dir`）：manifest 级时间语义 / 授权引用 / 合成标记未纳入本次诊断；"
    "缺失字段一律按「无证据」报告，**不**推断、**不**用当前时间或文件 mtime 填补。"
)


def _safe(value: object, *, max_chars: int = 200) -> str:
    """字符串统一脱敏 + 截断（绝不回显正文 / 凭据）。"""
    return safe_text(str(value), max_chars=max_chars)


def _label(scope: str) -> str:
    return _SCOPE_LABELS.get(scope, scope)


def _is_declared(value: object) -> bool:
    """manifest 声明字段是否**已声明**（``False`` 也是显式声明，``None`` / 空串不是）。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        return bool(value)
    return True


def _quote(values: tuple[str, ...]) -> str:
    """把声明文本渲染为脱敏的内联引用（空集合显示 ``—``）。"""
    return " / ".join(f"`{value}`" for value in values) if values else "—"


def _item(
    *,
    key: str,
    category: str,
    scope: str,
    readiness: ReadinessClass,
    machine_fact: str,
    human_next_step: str,
    current: int | float | str | bool | None,
    required: int | float | str | None,
    missing_fields: tuple[str, ...] = (),
    missing_count: int | None = None,
    references: tuple[str, ...] = (),
    evidence_start: str | None = None,
    evidence_end: str | None = None,
) -> DiagnosticItem:
    """构造诊断项（统一脱敏；``mock_or_template_qualifies`` 恒为 false）。"""
    return DiagnosticItem(
        key=key,
        category=category,
        scope=scope,
        readiness=readiness,
        machine_fact=safe_text(machine_fact, max_chars=600),
        human_next_step=safe_text(human_next_step, max_chars=400),
        current=current,
        required=required,
        missing_fields=missing_fields,
        missing_count=missing_count,
        references=references,
        evidence_start=evidence_start,
        evidence_end=evidence_end,
    )


def _manifest_fact(package: CandidatePackage) -> ManifestFact:
    """把一个候选包映射为 **字段级** manifest 事实（只读；绝不读取候选文件正文）。"""
    raw: dict[str, object] = {
        "evidence_type": package.evidence_type,
        "source": package.source,
        "authorization_reference": package.authorization_reference,
        "time_semantics": package.time_semantics,
        "availability_semantics": package.availability_semantics,
        "historical_oos_applicable": package.historical_oos_applicable,
        "files": package.files,
    }
    drifted = sorted(set(_MINIMUM_MANIFEST_FIELDS) - set(raw))
    if drifted:  # 契约漂移必须显式失败：绝不静默忽略新必填声明
        raise ValueError(f"manifest 契约字段漂移（inbox 已新增必填项）：{drifted}")
    declared = tuple(name for name in _MINIMUM_MANIFEST_FIELDS if _is_declared(raw[name]))
    missing = tuple(name for name in _MINIMUM_MANIFEST_FIELDS if not _is_declared(raw[name]))
    scope_name = str(package.evidence_type).strip().lower()
    scope = (
        scope_name
        if scope_name in {item.value for item in EvidenceScope}
        else _UNKNOWN_SCOPE
    )
    return ManifestFact(
        package_dir=_safe(package.package_dir),
        fingerprint=str(package.fingerprint),
        scope=scope,
        status=package.status.value,
        synthetic=bool(package.synthetic),
        declared_fields=declared,
        missing_fields=missing,
        time_semantics=_safe(package.time_semantics),
        availability_semantics=_safe(package.availability_semantics),
        historical_oos_applicable=package.historical_oos_applicable,
        reason_codes=tuple(sorted(str(code) for code in package.reason_codes)),
        acceptable_rows=int(package.acceptable_rows),
        quarantined_rows=int(package.quarantined_rows),
        not_oos_eligible_rows=int(package.not_oos_eligible_rows),
    )


def build_manifest_diagnostic(report: InboxPreflightReport | None) -> ManifestDiagnostic:
    """把一次**已完成的**只读 inbox 预检映射为诊断段（纯函数，零 I/O、零网络）。"""
    if report is None:
        return ManifestDiagnostic(
            inbox_dir=None, scanned=False, facts=(), notes=(MANIFEST_NOT_SCANNED_NOTE,)
        )
    facts = tuple(
        sorted(
            (_manifest_fact(package) for package in report.packages),
            key=lambda fact: (fact.fingerprint, fact.package_dir),
        )
    )
    return ManifestDiagnostic(
        inbox_dir=_safe(report.inbox_dir),
        scanned=True,
        facts=facts,
        notes=(MANIFEST_SCANNED_NOTE,),
    )


@dataclass(frozen=True, slots=True)
class _ScopeFacts:
    """单个 scope 的缺口事实（全部来自既有 readiness / handoff 口径，不重算阈值）。"""

    scope: str
    eligible: int
    required: int
    remaining: int
    certified: int
    not_oos_eligible: int
    coverage_applicable: bool
    coverage_days: int
    coverage_required: int
    coverage_remaining: int
    source_share_applicable: bool
    source_share_evaluable: bool
    max_source_share: float
    source_share_limit: float
    source_share_remaining: float
    evidence_start: str | None
    evidence_end: str | None
    candidates: tuple[ManifestFact, ...]
    manifest_scanned: bool

    @property
    def has_records(self) -> bool:
        """库内是否已有**经证据入口认证**的记录（候选不算）。"""
        return self.certified > 0 or self.eligible > 0

    @property
    def candidate_fact(self) -> str:
        """本地候选包的事实描述（区分"未扫描"与"该 scope 无候选"）。"""
        if self.candidates:
            passed = sum(
                1 for item in self.candidates if item.status == InboxStatus.PREFLIGHT_PASS.value
            )
            quarantined = len(self.candidates) - passed
            synthetic = sum(1 for item in self.candidates if item.synthetic)
            return (
                f"本地候选包 {len(self.candidates)} 个（preflight PASS {passed} / QUARANTINED "
                f"{quarantined}；带示例 / 合成标记 {synthetic} 个）尚未经人工确认 + 显式 intake。"
            )
        if self.manifest_scanned:
            return f"本地候选目录中没有 {_label(self.scope)} 类候选包。"
        return "本地未提供候选目录（`--inbox-dir` 缺省）。"

    def declares(self, name: str) -> bool:
        """是否有候选包显式声明了该 manifest 字段。"""
        return any(name in item.declared_fields for item in self.candidates)


def _find_check(gap: ScopeGap, key: str) -> ReadinessCheck:
    """按检查键取既有就绪度检查；缺失即显式失败（防止口径漂移被静默忽略）。"""
    for check in gap.checks:
        if check.key == key:
            return check
    raise ValueError(f"就绪度报告缺少检查键：{key}")


def _scope_facts(
    scope: EvidenceScope,
    gap: ScopeGap,
    candidates: tuple[ManifestFact, ...],
    *,
    manifest_scanned: bool,
) -> _ScopeFacts:
    """把既有 ScopeGap 转成缺口事实（只搬运既有口径，不新增任何阈值）。"""
    eligible = _find_check(gap, f"{scope.value}.evidence_intake_oos_eligible")
    return _ScopeFacts(
        scope=scope.value,
        eligible=gap.eligible,
        required=gap.required,
        remaining=gap.remaining,
        certified=gap.certified,
        not_oos_eligible=gap.not_oos_eligible,
        coverage_applicable=gap.coverage_applicable,
        coverage_days=gap.coverage_days,
        coverage_required=gap.coverage_required,
        coverage_remaining=gap.coverage_remaining,
        source_share_applicable=gap.source_share_applicable,
        source_share_evaluable=gap.source_share_evaluable,
        max_source_share=gap.max_source_share,
        source_share_limit=gap.source_share_limit,
        source_share_remaining=gap.source_share_remaining,
        evidence_start=eligible.evidence_start,
        evidence_end=eligible.evidence_end,
        candidates=candidates,
        manifest_scanned=manifest_scanned,
    )


def _time_field_item(facts: _ScopeFacts, field: str, *, derived: bool) -> DiagnosticItem:
    """时间字段（``published_at`` / ``collected_at`` / ``effective_at``）的缺口 / 就绪项。"""
    label = _label(facts.scope)
    key = f"{facts.scope}.{field}"
    if derived:
        origin = "由既有契约派生（`effective_at = max(published_at, collected_at)`）"
        next_step = (
            f"`{field}` 是派生字段（无需单独提供）：补齐含时区的 `published_at` / "
            "`collected_at` 后由既有契约派生；不得由本工具或人工用当前时间推断。"
        )
    else:
        origin = "由输入声明（入口机械校验：合法 ISO8601 + 必须带时区）"
        next_step = (
            f"提供含 `{field}` 真实值的授权证据并显式 intake；"
            "不得用当前时间 / 文件 mtime / 抓取时间替代。"
        )
    if facts.has_records:
        return _item(
            key=key,
            category=field,
            scope=facts.scope,
            readiness=ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
            machine_fact=(
                f"{label}：库内已有 {facts.certified} 条经入口认证记录，`{field}` {origin}；"
                "程序只能确认字段**存在且形态合法**，无法证明其真实性与原始出处 → 必须人工核验。"
            ),
            human_next_step=(
                f"人工核验 `{field}`（{label}）的原始出处与真实性；本诊断不填补、不推断。"
            ),
            current=facts.certified,
            required=None,
            missing_fields=(),
            missing_count=0,
            references=(field,),
            evidence_start=facts.evidence_start,
            evidence_end=facts.evidence_end,
        )
    return _item(
        key=key,
        category=field,
        scope=facts.scope,
        readiness=ReadinessClass.EVIDENCE_MISSING,
        machine_fact=(
            f"{label}：库内 0 条经入口认证记录，`{field}` {origin} 但**无任何可用证据**；"
            f"{facts.candidate_fact}"
            f"本诊断只报告缺失事实：**不**用当前时间 / 文件 mtime / 抓取时间 / "
            f"推断值填补 `{field}`。"
        ),
        human_next_step=next_step,
        current=0,
        required=None,
        missing_fields=(field,),
        missing_count=None,
        references=(field,),
        evidence_start=facts.evidence_start,
        evidence_end=facts.evidence_end,
    )


def _availability_item(facts: _ScopeFacts) -> DiagnosticItem:
    """独立历史可用证据 / OOS 资格项（**本诊断的核心缺口来源**）。"""
    label = _label(facts.scope)
    fields = AVAILABILITY_CONTRACT_FIELDS
    window = f"{facts.evidence_start or '—'} → {facts.evidence_end or '—'}"
    if facts.not_oos_eligible > 0:
        return _item(
            key=f"{facts.scope}.availability_oos",
            category="availability_oos",
            scope=facts.scope,
            readiness=ReadinessClass.EVIDENCE_MISSING,
            machine_fact=(
                f"{label}：经入口认证 {facts.certified} 条，其中 {facts.not_oos_eligible} 条"
                "缺独立历史可用证据（NOT_OOS_ELIGIBLE / AVAILABILITY_UNPROVEN）；"
                "只有独立 `available_at` + 出处证据才计 OOS，"
                "`published_at` 早于 `collected_at` **本身不等于**历史可用。"
            ),
            human_next_step=(
                "为这些记录补齐独立 `available_at` + `availability_provenance` + "
                "`availability_reference` 并由人工核验出处（不得用 published_at / 抓取时间代替）。"
            ),
            current=facts.not_oos_eligible,
            required=0,
            missing_fields=fields,
            missing_count=facts.not_oos_eligible,
            references=fields,
            evidence_start=facts.evidence_start,
            evidence_end=facts.evidence_end,
        )
    if facts.eligible > 0:
        return _item(
            key=f"{facts.scope}.availability_oos",
            category="availability_oos",
            scope=facts.scope,
            readiness=ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
            machine_fact=(
                f"{label}：OOS eligible {facts.eligible} 条，独立可用时间窗口 {window}"
                "（来自 `available_at`，**不是**抓取时间）；"
                "出处（provenance / reference）与授权范围仍需人工核验。"
            ),
            human_next_step=(
                "人工核验 availability 出处是否可复核、属授权范围，且与论文 / 研究口径一致。"
            ),
            current=facts.eligible,
            required=facts.required,
            missing_fields=(),
            missing_count=0,
            references=fields,
            evidence_start=facts.evidence_start,
            evidence_end=facts.evidence_end,
        )
    return _item(
        key=f"{facts.scope}.availability_oos",
        category="availability_oos",
        scope=facts.scope,
        readiness=ReadinessClass.EVIDENCE_MISSING,
        machine_fact=(
            f"{label}：OOS eligible 0 条（入口认证 {facts.certified} 条）；"
            "独立历史可用证据完全缺失，"
            f"{facts.candidate_fact}"
            "缺失字段只报告事实：不计算、不推断、不用当前时间或抓取时间推断历史可用性。"
        ),
        human_next_step=(
            "提供逐条独立 `available_at` + 出处证据的授权数据并显式 intake；"
            "仅有 `published_at` / `collected_at` 的记录不具备 OOS 资格。"
        ),
        current=0,
        required=facts.required,
        missing_fields=fields,
        missing_count=None,
        references=fields,
        evidence_start=facts.evidence_start,
        evidence_end=facts.evidence_end,
    )


def _time_semantics_item(facts: _ScopeFacts) -> DiagnosticItem:
    """独立时间语义项（声明 vs 证据分离；程序不推断"历史当时可用"）。"""
    label = _label(facts.scope)
    semantics_values = {candidate.time_semantics for candidate in facts.candidates}
    declared = tuple(sorted(text for text in semantics_values if text))
    availability_values = {candidate.availability_semantics for candidate in facts.candidates}
    declared_availability = tuple(sorted(text for text in availability_values if text))
    declared_by_field = {
        "time_semantics": declared,
        "availability_semantics": declared_availability,
    }
    missing = tuple(
        name for name in TIME_SEMANTICS_DECLARATION_FIELDS if not declared_by_field[name]
    )
    if facts.has_records:
        return _item(
            key=f"{facts.scope}.time_semantics",
            category="time_semantics",
            scope=facts.scope,
            readiness=ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
            machine_fact=(
                f"{label}：契约要求时间语义**独立** —— `published_at` / `collected_at` "
                "来自输入声明、`effective_at` 由 `max(published_at, collected_at)` 派生、"
                "历史可用性必须由独立 `available_at` + 出处证据证明；"
                "入口只做机械校验，「历史当时是否真的可用」必须人工核验；"
                "本诊断**不**用当前时间 / 文件 mtime / 抓取时间推断任何时间语义。"
            ),
            human_next_step=(
                "人工核验时间语义声明与独立可用证据的一致性"
                "（manifest 的 `time_semantics` / `availability_semantics`"
                " + 逐条 available_at 出处）。"
            ),
            current=facts.certified,
            required=None,
            missing_fields=(),
            missing_count=0,
            references=TIME_SEMANTICS_DECLARATION_FIELDS,
            evidence_start=facts.evidence_start,
            evidence_end=facts.evidence_end,
        )
    if facts.candidates:
        return _item(
            key=f"{facts.scope}.time_semantics",
            category="time_semantics",
            scope=facts.scope,
            readiness=ReadinessClass.EVIDENCE_MISSING,
            machine_fact=(
                f"{label}：库内 0 条经入口认证记录；候选 manifest 声明 `time_semantics`="
                f"{_quote(declared)}、`availability_semantics`={_quote(declared_availability)}"
                "（声明 ≠ 证据，候选尚未 ingest）；独立时间语义仍需人工核验。"
            ),
            human_next_step=(
                "人工核验候选的时间语义声明并显式 intake"
                "（声明不得替代 available_at 证据）。"
            ),
            current=len(facts.candidates),
            required=None,
            missing_fields=missing,
            missing_count=None,
            references=TIME_SEMANTICS_DECLARATION_FIELDS,
        )
    return _item(
        key=f"{facts.scope}.time_semantics",
        category="time_semantics",
        scope=facts.scope,
        readiness=ReadinessClass.EVIDENCE_MISSING,
        machine_fact=(
            f"{label}：既无认证记录也无本地候选 manifest，独立时间语义**无任何证据**"
            f"（缺失声明：{_quote(TIME_SEMANTICS_DECLARATION_FIELDS)}）。"
        ),
        human_next_step=(
            "提供含独立时间语义声明的真实授权证据（manifest + 证据行）并由人工核验后显式 intake。"
        ),
        current=0,
        required=None,
        missing_fields=TIME_SEMANTICS_DECLARATION_FIELDS,
        missing_count=None,
        references=TIME_SEMANTICS_DECLARATION_FIELDS,
    )


def _authorization_item(facts: _ScopeFacts) -> DiagnosticItem:
    """授权声明项（程序只做机械校验，法律效力只能人工核验）。"""
    label = _label(facts.scope)
    fields = AUTHORIZATION_CONTRACT_FIELDS
    if facts.certified > 0:
        origin = (
            f"库内 {facts.certified} 条经入口认证记录已通过授权字段的机械校验"
            "（状态 / 依据 / 引用 / 复核人 / 复核时间 + 三项用途许可）"
        )
        declared = True
    elif facts.declares("authorization_reference"):
        origin = "本地候选 manifest 声明了 `authorization_reference`"
        declared = True
    else:
        origin = (
            f"认证记录 0 条、候选 manifest {len(facts.candidates)} 个均未声明 "
            "`authorization_reference`"
        )
        declared = False
    if declared:
        return _item(
            key=f"{facts.scope}.authorization",
            category="authorization",
            scope=facts.scope,
            readiness=ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
            machine_fact=(
                f"{label}：{origin}；程序只做机械校验，**不证明**授权法律效力 / "
                "许可范围 / 签认人身份 → 必须人工核验"
                "（`reviewed_by` 必须是真人，不得填模型名）。"
            ),
            human_next_step=(
                "人工核验授权声明与许可文件（docs/legal/ 内可复核引用），"
                "确认三项用途许可均在授权范围内。"
            ),
            current=facts.certified,
            required=None,
            missing_fields=(),
            missing_count=0,
            references=fields,
        )
    return _item(
        key=f"{facts.scope}.authorization",
        category="authorization",
        scope=facts.scope,
        readiness=ReadinessClass.EVIDENCE_MISSING,
        machine_fact=(
            f"{label}：{origin}；缺失字段：{_quote(fields)}。{facts.candidate_fact}"
            "本诊断只报告缺失事实，**不**推断授权状态、**不**把公开可访问当成授权。"
        ),
        human_next_step=(
            "由业务方提供授权声明（status / basis / reference / reviewed_by / reviewed_at + "
            "permits_automated_collection / permits_local_storage / "
            "permits_research_use）并人工核验。"
        ),
        current=0,
        required=None,
        missing_fields=fields,
        missing_count=None,
        references=fields,
    )


def _source_item(facts: _ScopeFacts) -> DiagnosticItem:
    """来源身份项（是否为**已授权来源**只能人工核验）。"""
    label = _label(facts.scope)
    fields: tuple[str, ...] = ("source", "source_record_id")
    if facts.certified > 0:
        origin = f"库内 {facts.certified} 条经入口认证记录已声明 `source` / `source_record_id`"
        declared = True
    elif facts.declares("source"):
        origin = "本地候选 manifest 已声明来源字段"
        declared = True
    else:
        origin = "无认证记录且候选 manifest 未声明来源字段"
        declared = False
    if declared:
        return _item(
            key=f"{facts.scope}.source",
            category="source",
            scope=facts.scope,
            readiness=ReadinessClass.HUMAN_VERIFICATION_REQUIRED,
            machine_fact=(
                f"{label}：{origin}；来源是否为**已授权来源**、账号身份是否稳定"
                "（同一身份内容不一致判 IDENTITY_CONFLICT 且不覆盖历史）"
                "必须人工核验。"
            ),
            human_next_step=(
                "人工核验来源授权范围与账号身份稳定性（不得用模型名 / 昵称副本冒充来源或账号 ID）。"
            ),
            current=facts.certified,
            required=None,
            missing_fields=(),
            missing_count=0,
            references=fields,
        )
    return _item(
        key=f"{facts.scope}.source",
        category="source",
        scope=facts.scope,
        readiness=ReadinessClass.EVIDENCE_MISSING,
        machine_fact=f"{label}：{origin}；缺失字段：{_quote(fields)}。{facts.candidate_fact}",
        human_next_step=(
            "在授权证据中提供稳定的 `source` 与 `source_record_id`（身份唯一键），"
            "再由人工核验来源授权。"
        ),
        current=0,
        required=None,
        missing_fields=fields,
        missing_count=None,
        references=fields,
    )


def _sample_size_item(facts: _ScopeFacts) -> DiagnosticItem:
    """样本量项（Author = 可信帖子数；News = 事件数；阈值来源 ``evidence_gate``）。"""
    label = _label(facts.scope)
    metric = "可信帖子" if facts.scope == EvidenceScope.AUTHOR.value else "OOS eligible 事件"
    met = facts.remaining <= 0
    return _item(
        key=f"{facts.scope}.sample_size",
        category="sample_size",
        scope=facts.scope,
        readiness=(
            ReadinessClass.HUMAN_VERIFICATION_REQUIRED if met else ReadinessClass.EVIDENCE_MISSING
        ),
        machine_fact=(
            f"{label}：经入口认证且具备独立历史可用证据的{metric} {facts.eligible} 条 / 既有门槛 "
            f"{facts.required} 条（阈值来源 src.alpha.evidence_gate，本诊断**不新增 / 不降低**）；"
            f"仍缺 {facts.remaining} 条。{facts.candidate_fact}"
        ),
        human_next_step=(
            "补充真实授权证据（含独立可用时间证据）并显式 intake，直到量化门槛达标；"
            "Mock / 模板 / 示例永不计资格。"
            if not met
            else (
                "量化门槛已达标：仍须人工核验授权与独立可用证据后才能进入 L3，"
                "本诊断**不**解除 blocker。"
            )
        ),
        current=facts.eligible,
        required=facts.required,
        missing_fields=(),
        missing_count=facts.remaining if not met else 0,
        references=("src/alpha/evidence_gate.py", "evidence-intake-v1"),
    )


def _coverage_item(facts: _ScopeFacts) -> DiagnosticItem:
    """News 覆盖天数项（按独立 ``available_at`` 计算，不得用采集时间回填历史）。"""
    met = facts.coverage_remaining <= 0
    window = f"{facts.evidence_start or '—'} → {facts.evidence_end or '—'}"
    return _item(
        key=f"{facts.scope}.coverage",
        category="coverage",
        scope=facts.scope,
        readiness=(
            ReadinessClass.HUMAN_VERIFICATION_REQUIRED if met else ReadinessClass.EVIDENCE_MISSING
        ),
        machine_fact=(
            f"News：OOS eligible 记录的独立可用时间窗口 {window}，覆盖 {facts.coverage_days} 天 / "
            f"既有门槛 {facts.coverage_required} 天；覆盖天数按独立 `available_at` 计算，"
            "**不能**用今天采集的旧标题回填历史。"
        ),
        human_next_step=(
            "补充覆盖更长时间窗的真实授权历史数据（逐条独立 available_at）并显式 intake。"
            if not met
            else "覆盖天数已达标：仍需人工核验窗口内证据的出处与授权范围。"
        ),
        current=facts.coverage_days,
        required=facts.coverage_required,
        missing_fields=(),
        missing_count=facts.coverage_remaining if not met else 0,
        references=("available_at", "MIN_NEWS_HISTORY_DAYS"),
        evidence_start=facts.evidence_start,
        evidence_end=facts.evidence_end,
    )


def _source_share_item(facts: _ScopeFacts) -> DiagnosticItem:
    """News 单源集中度项（无 OOS 记录时不得判 PASS）。"""
    share = f"{facts.max_source_share:.2%}"
    limit = f"{facts.source_share_limit:.0%}"
    if not facts.source_share_evaluable:
        readiness = ReadinessClass.EVIDENCE_MISSING
        fact = "News：无 OOS eligible 记录 → 来源分布**无证据**，单源集中度不得判 PASS。"
        next_step = "先补齐 OOS eligible 的授权来源数据，再评估单源集中度。"
        current: float | None = None
        missing: int | None = None
        missing_fields: tuple[str, ...] = ("available_at",)
    elif facts.source_share_remaining > 0:
        readiness = ReadinessClass.EVIDENCE_MISSING
        missing_fields = ()
        fact = (
            f"News：单一来源最大占比 {share} 超过既有上限 {limit}"
            "（按 OOS eligible 记录的真实分布计算）。"
        )
        next_step = "增加其它**已授权**来源的合格记录以降低单源集中度（不得用 Mock / 模板凑数）。"
        current = facts.max_source_share
        missing = None
    else:
        readiness = ReadinessClass.HUMAN_VERIFICATION_REQUIRED
        missing_fields = ()
        fact = (
            f"News：单一来源最大占比 {share} ≤ 既有上限 {limit}（机器可算）；"
            "来源授权范围仍须人工核验。"
        )
        next_step = "人工核验各来源的授权范围与分布是否可接受。"
        current = facts.max_source_share
        missing = 0
    return _item(
        key=f"{facts.scope}.source_share",
        category="source",
        scope=facts.scope,
        readiness=readiness,
        machine_fact=fact,
        human_next_step=next_step,
        current=current,
        required=facts.source_share_limit,
        missing_fields=missing_fields,
        missing_count=missing,
        references=("src/alpha/evidence_gate.py", "MAX_NEWS_SOURCE_SHARE"),
    )


def _scope_items(facts: _ScopeFacts) -> list[DiagnosticItem]:
    """单个 scope 的全部诊断项（分类与顺序由既有契约分组决定）。"""
    items = [
        _authorization_item(facts),
        _source_item(facts),
        _time_field_item(facts, "published_at", derived=False),
        _time_field_item(facts, "collected_at", derived=False),
        _time_field_item(facts, "effective_at", derived=True),
        _availability_item(facts),
        _time_semantics_item(facts),
        _sample_size_item(facts),
    ]
    if facts.coverage_applicable:
        items.append(_coverage_item(facts))
    if facts.source_share_applicable:
        items.append(_source_share_item(facts))
    return items


def _contract_items() -> list[DiagnosticItem]:
    """代码 / 契约侧的就绪事实（``CODE_READY``；绝不代表数据资格通过）。"""
    author_fields = required_field_names(EvidenceScope.AUTHOR)
    news_fields = required_field_names(EvidenceScope.NEWS)
    quarantine = tuple(sorted(code.value for code in QUARANTINE_REASON_CODES))
    eligibility = tuple(sorted(code.value for code in ELIGIBILITY_REASON_CODES))
    return [
        _item(
            key="contract.intake_fields",
            category="contract",
            scope="both",
            readiness=ReadinessClass.CODE_READY,
            machine_fact=(
                f"输入契约 `{EVIDENCE_CONTRACT_VERSION}` 已声明必填字段："
                f"Author {len(author_fields)} 列 / News {len(news_fields)} 列；"
                "逐行机械校验（授权 / 来源身份 / 时间语义 / 内容完整性 / 可用性证据）"
                "已实现，本诊断**只读复用**，不新增字段、不降低任何门槛。"
            ),
            human_next_step="代码侧无需动作；真实证据仍须人工提供与核验（本项不是资格判定）。",
            current=EVIDENCE_CONTRACT_VERSION,
            required=None,
            references=tuple(dict.fromkeys((*author_fields, *news_fields))),
        ),
        _item(
            key="contract.manifest_fields",
            category="contract",
            scope="both",
            readiness=ReadinessClass.CODE_READY,
            machine_fact=(
                f"候选包 manifest 契约已声明必填声明 {len(MANIFEST_REQUIRED_FIELDS)} 项"
                "（含 `time_semantics` / `availability_semantics` / "
                "`authorization_reference` / `historical_oos_applicable`）；"
                "缺任一项 fail-closed 隔离（MANIFEST_FIELD_MISSING）。"
            ),
            human_next_step="代码侧无需动作；提供候选包时必须补齐全部 manifest 声明。",
            current=len(MANIFEST_REQUIRED_FIELDS),
            required=None,
            references=MANIFEST_REQUIRED_FIELDS,
        ),
        _item(
            key="contract.reason_codes",
            category="contract",
            scope="both",
            readiness=ReadinessClass.CODE_READY,
            machine_fact=(
                f"原因码词表已定义：隔离 {len(quarantine)} 项（含 `SYNTHETIC_EVIDENCE` / "
                f"`AUTHORIZATION_MISSING`）；资格 {len(eligibility)} 项（`AVAILABILITY_UNPROVEN` → "
                "NOT_OOS_ELIGIBLE，仍留原始事实但**不计** OOS）。"
            ),
            human_next_step="代码侧无需动作；任何 Mock / 模板 / 示例行导入即隔离，永不计资格。",
            current=len(quarantine) + len(eligibility),
            required=None,
            references=(*quarantine, *eligibility),
        ),
        _item(
            key="contract.read_only",
            category="contract",
            scope="both",
            readiness=ReadinessClass.CODE_READY,
            machine_fact=(
                "本诊断与既有 readiness / handoff / manifest 能力均为只读："
                "零网络（不抓取、不绕过 robots / 证书）、"
                "零数据库写入、零模型训练、零交易；"
                "缺失字段只报告事实与人工下一步，"
                "**不**用当前时间 / 文件 mtime / 抓取时间 / "
                "推断值填补任何时间或 OOS 证据。"
            ),
            human_next_step="代码侧无需动作；本诊断没有采集、写入、放行或推进 Phase 的任何入口。",
            current=None,
            required=None,
            references=(
                "src/monitoring/evidence_readiness.py",
                "src/evidence/handoff.py",
                "src/evidence/inbox.py",
            ),
        ),
    ]


def _gate_items() -> list[DiagnosticItem]:
    """资格门禁与 L3 / L4 人工 Gate（``GATE_BLOCKED``；工具永不推进）。"""
    return [
        _item(
            key="gate.phase3_3_data",
            category="gate",
            scope="both",
            readiness=ReadinessClass.GATE_BLOCKED,
            machine_fact=(
                f"`{PHASE3_3_BLOCKER_CODE}` 由**真实授权证据 + 人工 Gate** 决定："
                "观测 / 诊断层不具备解除能力；"
                "库内数量门槛达标也不等于 Alpha 放行。"
            ),
            human_next_step=(
                "由业务方补齐真实授权证据、人工核验后按 "
                "`.ai/DEVELOPMENT_PROTOCOL.md` 走 L3 人工决策。"
            ),
            current="BLOCKED",
            required=None,
            references=(".ai/PROJECT_STATE.json", ".ai/DEVELOPMENT_PROTOCOL.md"),
        ),
        _item(
            key="gate.l3_l4",
            category="gate",
            scope="both",
            readiness=ReadinessClass.GATE_BLOCKED,
            machine_fact=(
                "Phase 切换与 L3 / L4 Gate 只能由人工推进；"
                "本诊断**没有**任何推进 / 放行 / 写状态 API"
                "（`advance_allowed=false`、`l3_l4_auto_advance_allowed=false`）。"
            ),
            human_next_step="按 L3 人工 Gate 流程显式决策（本任务无权解除或推进）。",
            current=None,
            required=None,
            references=(".ai/DEVELOPMENT_PROTOCOL.md",),
        ),
        _item(
            key="gate.model_authority",
            category="gate",
            scope="both",
            readiness=ReadinessClass.GATE_BLOCKED,
            machine_fact=(
                "Cline / DeepSeek 仅执行已批准任务：本诊断不签发资格、不生成后续任务、"
                "不修改 `PROJECT_STATE`、不触碰交易开关（`LIVE_TRADING=false` / "
                "`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`）。"
            ),
            human_next_step="如需下一步，由 GPT / 人工规划并签发任务。",
            current=None,
            required=None,
            references=(".clinerules", ".ai/DEVELOPMENT_PROTOCOL.md"),
        ),
    ]


def _human_steps(
    facts_list: tuple[_ScopeFacts, ...],
    manifest: ManifestDiagnostic,
    items: tuple[DiagnosticItem, ...],
) -> tuple[HumanStep, ...]:
    """构造必须人工完成的步骤（含机器可确认程度；``blocking`` 恒为 True）。"""
    all_certified = all(facts.certified > 0 for facts in facts_list)
    availability_incomplete = any(
        facts.eligible <= 0 or facts.not_oos_eligible > 0 for facts in facts_list
    )
    quantified_gap = any(
        item.readiness is ReadinessClass.EVIDENCE_MISSING
        and item.category in _QUANTIFIED_CATEGORIES
        for item in items
    )
    synthetic_detected = manifest.synthetic_count > 0
    statuses: dict[str, StepVerification] = {
        "provide_authorized_evidence": (
            StepVerification.MACHINE_CONFIRMED
            if all_certified
            else StepVerification.MACHINE_UNSATISFIED
        ),
        "verify_authorization_validity": StepVerification.HUMAN_ONLY,
        "verify_independent_availability": (
            StepVerification.MACHINE_UNSATISFIED
            if availability_incomplete
            else StepVerification.HUMAN_ONLY
        ),
        "reach_quantified_thresholds": (
            StepVerification.MACHINE_UNSATISFIED
            if quantified_gap
            else StepVerification.MACHINE_CONFIRMED
        ),
        "exclude_synthetic_evidence": (
            StepVerification.MACHINE_UNSATISFIED
            if synthetic_detected
            else StepVerification.HUMAN_ONLY
        ),
        "l3_human_gate_decision": StepVerification.HUMAN_ONLY,
    }
    return tuple(
        HumanStep(
            step=spec.step,
            category=spec.category,
            scope=spec.scope,
            requirement=spec.requirement,
            evidence_reference=spec.evidence_reference,
            # 未登记的步骤一律按最保守的 HUMAN_ONLY 处理（绝不静默判为已确认）
            verification=statuses.get(spec.step, StepVerification.HUMAN_ONLY),
            blocking=True,
        )
        for spec in MANDATORY_HUMAN_STEPS
    )


def _non_qualifying(
    handoff: EvidenceHandoffReport, manifest: ManifestDiagnostic
) -> tuple[NonQualifyingEvidenceItem, ...]:
    """明确不计资格的证据形态（复用 GOLD-008 的既有清单 + 本地 manifest 合成标记）。"""
    items = [
        NonQualifyingEvidenceItem(
            kind=excluded.kind,
            reason_code=excluded.reason_code,
            detail=excluded.detail,
            detected_count=0,
            counts_toward_eligibility=False,
        )
        for excluded in handoff.excluded_evidence
    ]
    if manifest.synthetic_count > 0:
        items.append(
            NonQualifyingEvidenceItem(
                kind="local_manifest_synthetic",
                reason_code=ReasonCode.SYNTHETIC_EVIDENCE.value,
                detail=(
                    "本地候选目录中发现带示例 / 合成标记的候选包：导入即判 SYNTHETIC_EVIDENCE "
                    "隔离，**不写库、不计入** qualification ledger。"
                ),
                detected_count=manifest.synthetic_count,
                counts_toward_eligibility=False,
            )
        )
    return tuple(items)


def build_gap_diagnostic(
    readiness: EvidenceReadinessReport,
    *,
    manifest: ManifestDiagnostic | None = None,
) -> EvidenceGapDiagnostic:
    """构造只读缺口诊断（纯函数；不触库、不联网、不写文件）。

    Args:
        readiness: :func:`src.monitoring.evidence_readiness.build_readiness_report` 的结果
            （阈值已由 ``src.alpha.evidence_gate`` 决定）。
        manifest: 可选的本地候选 manifest 诊断段（缺省 = 未提供候选目录）。

    Returns:
        :class:`EvidenceGapDiagnostic`；顶层结论恒为 ``GATE_BLOCKED``，
        ``advance_allowed`` / ``l3_l4_auto_advance_allowed`` /
        ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 ``False``。

    Raises:
        ValueError: ``readiness.as_of`` 未带时区。
    """
    if readiness.as_of.tzinfo is None or readiness.as_of.utcoffset() is None:
        raise ValueError("readiness.as_of 必须包含时区")
    manifest_section = manifest if manifest is not None else build_manifest_diagnostic(None)
    handoff = build_handoff_report(readiness)
    gaps = {
        EvidenceScope.AUTHOR: handoff.author,
        EvidenceScope.NEWS: handoff.news,
    }
    facts_list = tuple(
        _scope_facts(
            scope,
            gaps[scope],
            manifest_section.for_scope(scope),
            manifest_scanned=manifest_section.scanned,
        )
        for scope in _SCOPE_ORDER
    )
    collected: list[DiagnosticItem] = [*_contract_items(), *_gate_items()]
    for facts in facts_list:
        collected.extend(_scope_items(facts))
    ordered = tuple(
        sorted(
            collected,
            key=lambda item: (
                _SCOPE_RANK.get(item.scope, len(_SCOPE_RANK)),
                _CATEGORY_RANK.get(item.category, len(_CATEGORY_RANK)),
                item.key,
            ),
        )
    )
    class_counts = tuple(
        (
            readiness_class.value,
            sum(1 for item in ordered if item.readiness is readiness_class),
        )
        for readiness_class in READINESS_CLASS_ORDER
    )
    return EvidenceGapDiagnostic(
        schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        kind=DIAGNOSTIC_KIND,
        report=DIAGNOSTIC_KIND,
        contract_version=readiness.contract_version,
        as_of=readiness.as_of.astimezone(UTC),
        blocker_code=PHASE3_3_BLOCKER_CODE,
        # 以下安全字段全部**硬编码**：任何输入 / 上游报告都无法把它们改成"通过"
        blocker_active=True,
        human_gate_required=True,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        gate_blocked=True,
        advance_allowed=False,
        l3_l4_auto_advance_allowed=False,
        readiness=ReadinessClass.GATE_BLOCKED,
        class_counts=class_counts,
        items=ordered,
        human_steps=_human_steps(facts_list, manifest_section, ordered),
        non_qualifying=_non_qualifying(handoff, manifest_section),
        manifest=manifest_section,
        thresholds=tuple(sorted(thresholds().items())),
        notes=(DIAGNOSTIC_NOTE, HANDOFF_NOTE, READINESS_NOTE, EVIDENCE_INTAKE_NOTE),
    )


def load_gap_diagnostic(
    session: Session,
    *,
    as_of: datetime,
    inbox_dir: str | Path | None = None,
) -> EvidenceGapDiagnostic:
    """读取 ``PHASE3_3_DATA`` 缺口诊断（**严格只读 / 零网络**）。

    Args:
        session: 数据库会话（只读使用；调用方负责回滚 / 关闭）。
        as_of: 审计时点（必须带时区，保证可复现）。
        inbox_dir: 可选的**本地**候选目录（交给既有 ``scan_inbox`` 只读预检；缺省不扫描）。

    Raises:
        ValueError: ``as_of`` 未带时区。
        src.evidence.inbox.InboxDirError: ``inbox_dir`` 不存在 / 不是目录 / 不可读（fail-closed）。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    moment = as_of.astimezone(UTC)
    manifest = build_manifest_diagnostic(
        scan_inbox(Path(inbox_dir), moment=moment) if inbox_dir is not None else None
    )
    ledger = load_evidence_ledger(session)
    readiness = build_readiness_report(
        ledger, as_of=moment, scopes=(EvidenceScope.AUTHOR, EvidenceScope.NEWS)
    )
    return build_gap_diagnostic(readiness, manifest=manifest)


def _item_rows(items: tuple[DiagnosticItem, ...]) -> list[str]:
    """渲染诊断项表格（缺省显示 ``—``；缺失字段只列**字段名**，绝不虚构值）。"""
    rows = [
        "| 项 | 范围 | 分类 | 当前 | 要求 | 缺口 | 缺失字段 | 机器可读事实 | 人工下一步 |",
        "|---|---|---|---:|---:|---:|---|---|---|",
    ]
    if not items:
        rows.append("| — | — | — | — | — | — | — | 无 | 无 |")
        return rows
    for item in items:
        fields = ", ".join(f"`{name}`" for name in item.missing_fields) or "—"
        missing = "—" if item.missing_count is None else str(item.missing_count)
        rows.append(
            f"| `{item.key}` | {item.scope} | {item.category} | "
            f"{item.current} | {item.required} | {missing} | {fields} | "
            f"{item.machine_fact} | {item.human_next_step} |"
        )
    return rows


def render_gap_diagnostic_markdown(diagnostic: EvidenceGapDiagnostic) -> str:
    """渲染人类可读 Markdown（**不解除** blocker、不切换 Phase）。"""
    counts = {name: count for name, count in diagnostic.class_counts}
    lines: list[str] = [
        "# PHASE3_3_DATA 证据缺口诊断（只读 Readiness Diagnostic）",
        "",
        f"> blocker `{diagnostic.blocker_code}`：active={str(diagnostic.blocker_active).lower()}；"
        f"human_gate_required={str(diagnostic.human_gate_required).lower()}；"
        f"顶层结论 `{diagnostic.readiness.value}`；"
        f"advance_allowed={str(diagnostic.advance_allowed).lower()}；"
        f"l3_l4_auto_advance_allowed={str(diagnostic.l3_l4_auto_advance_allowed).lower()}。",
        "> 本诊断**不解除** blocker、不采集、不写库、不推进 Phase。",
        "",
        "## 1. 状态",
        "",
        f"- 契约版本：`{diagnostic.contract_version}`；"
        f"审计时点（UTC）：{diagnostic.as_of.isoformat()}",
        "- 既有量化门槛（只展示，不新增 / 不降低）："
        + "、".join(f"{name}={value}" for name, value in diagnostic.thresholds),
        "- 分类计数："
        + "、".join(
            f"{name}={counts.get(name, 0)}"
            for name in (readiness_class.value for readiness_class in READINESS_CLASS_ORDER)
        ),
        f"- 仍缺实质证据的诊断项（EVIDENCE_MISSING）：{diagnostic.open_evidence_gap_count}",
        "",
        "## 2. 必须人工完成的步骤（blocking 全部为 true）",
        "",
        "| 步骤 | 分类 | 范围 | 机器可核验 | 要求 | 证据入口 |",
        "|---|---|---|---|---|---|",
    ]
    for step in diagnostic.human_steps:
        lines.append(
            f"| `{step.step}` | {step.category} | {step.scope} | {step.verification.value} | "
            f"{step.requirement} | {safe_text(step.evidence_reference, max_chars=200)} |"
        )
    lines += [
        "",
        "## 3. 实质缺口（EVIDENCE_MISSING）",
        "",
        *_item_rows(diagnostic.items_for(ReadinessClass.EVIDENCE_MISSING)),
        "",
        "## 4. 需人工核验（HUMAN_VERIFICATION_REQUIRED）",
        "",
        *_item_rows(diagnostic.items_for(ReadinessClass.HUMAN_VERIFICATION_REQUIRED)),
        "",
        "## 5. 门禁（GATE_BLOCKED）",
        "",
        *_item_rows(diagnostic.items_for(ReadinessClass.GATE_BLOCKED)),
        "",
        "## 6. 代码 / 契约就绪（CODE_READY，不等于数据资格通过）",
        "",
        *_item_rows(diagnostic.items_for(ReadinessClass.CODE_READY)),
        "",
        "## 7. 明确不计资格的证据（**不得**用于达标）",
        "",
        "| 形态 | 原因码 | 检出数 | 计入资格 | 说明 |",
        "|---|---|---:|---|---|",
    ]
    for excluded in diagnostic.non_qualifying:
        lines.append(
            f"| {excluded.kind} | `{excluded.reason_code}` | {excluded.detected_count} | "
            f"{str(excluded.counts_toward_eligibility).lower()} | {excluded.detail} |"
        )
    lines += [
        "",
        "## 8. 本地候选 manifest 事实",
        "",
    ]
    manifest = diagnostic.manifest
    if not manifest.scanned:
        lines.append("- 未提供候选目录（`--inbox-dir` 缺省）")
    else:
        lines += [
            f"- 目录：`{manifest.inbox_dir}`；候选包 {len(manifest.facts)} 个"
            f"（preflight PASS {manifest.preflight_pass_count} / QUARANTINED "
            f"{manifest.quarantined_count}；带示例 / 合成标记 {manifest.synthetic_count}）",
            "",
            "| 包 | scope | 状态 | 合成 | 已声明 | 缺失声明 | 行数 | 原因码 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for fact in manifest.facts:
            declared = ", ".join(f"`{name}`" for name in fact.declared_fields) or "—"
            missing = ", ".join(f"`{name}`" for name in fact.missing_fields) or "—"
            reasons = ", ".join(f"`{code}`" for code in fact.reason_codes) or "—"
            lines.append(
                f"| `{fact.package_dir}` | {fact.scope} | {fact.status} | "
                f"{str(fact.synthetic).lower()} | {declared} | {missing} | "
                f"{fact.acceptable_rows}/{fact.quarantined_rows}/{fact.not_oos_eligible_rows} | "
                f"{reasons} |"
            )
    lines.append("")
    lines.extend(f"- {note}" for note in manifest.notes)
    lines += ["", "## 9. 口径与边界", ""]
    lines.extend(f"- {note}" for note in diagnostic.notes)
    lines.append("")
    return "\n".join(lines)
