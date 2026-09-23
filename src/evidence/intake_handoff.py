"""Phase 3.3 人工证据 Intake Handoff 契约与**纯本地只读预检**（GOLD-027）。

本模块把 GOLD-026 的 ``PHASE3_3_DATA`` **只读缺口诊断**转换为一份**版本化的人工证据提交 /
预检 handoff**：业务人员按**单一契约**准备真实授权 Author / News 材料，预检只回答
"材料是否齐全、字段是否可解析、来源 / 时间声明是否带独立证据引用"，**绝不**回答"是否合格"。

职责边界（**重要**，与 ``.clinerules`` 与 ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）：

- **只读 / 零网络 / 零写入**：不抓取站点、不绕过 robots / 条款 / 证书、不申请证书豁免、
  不写数据库、不做模型训练、不触碰交易；只消费**显式给出的本地候选目录**（``--inbox-dir``）
  与既有只读预检（:func:`src.evidence.inbox.scan_inbox`）；
- **复用而不复制**：字段 / 原因码 / 目录契约完全复用 ``evidence-intake-v1``
  （:mod:`src.evidence.contracts`）与 GOLD-011 inbox 预检（:mod:`src.evidence.inbox`）；
  manifest 字段级事实与缺口分类复用 GOLD-026
  （:mod:`src.monitoring.evidence_gap_diagnostic`）；阈值只**引用**
  ``src.alpha.evidence_gate``（经 :func:`src.evidence.handoff.thresholds`）。
  本模块**不新增、不降低任何资格阈值**，也不重新解释任何契约字段；
- **五态状态**（机器可读、稳定字符串）：每个提交材料落在 :class:`IntakeStatus` 之一 ——
  ``MISSING``（缺失，或已提交但机械校验未通过：不可用于人工核验）/
  ``PRESENT_UNVERIFIED``（已提交，但独立证据引用 / 机械校验结论不足，尚不能进入人工核验队列）/
  ``HUMAN_VERIFICATION_REQUIRED``（结构完整，法律效力 / 出处 / 时间语义**只能**人工核验）/
  ``NON_QUALIFYING``（Mock / 模板 / 示例 / 合成材料：**永远**不计资格）/
  ``GATE_BLOCKED``（``PHASE3_3_DATA`` 与 L3 / L4 人工 Gate：工具永不具备解除能力）；
- **结构完整 ≠ 资格通过**：``preflight_pass=true`` **只**表示 intake package 结构完整
  （材料齐全、字段可解析、来源 / 时间声明带独立证据引用），**绝不**等于
  ``evidence_qualified``、**不解除** ``PHASE3_3_DATA``、也**不**代表 L3 / L4 通过；
- **不伪造时间事实**：``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at``
  / OOS 证据缺失时**只报告事实与人工下一步**，**绝不**用当前时间、文件 mtime、抓取时间或
  任何推断值填补；
- **fail-closed**：``evidence_qualified`` / ``data_qualification_passed`` /
  ``phase_transition_allowed`` / ``advance_allowed`` / ``l3_l4_auto_advance_allowed``
  **恒为 false**；``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` **恒为 true**
  （**硬编码**，不被输入或上游报告透传）；
- **脱敏**：只输出白名单标量（字段名 / 计数 / 稳定原因码 / 已脱敏的声明文本与引用 / 时间），
  **绝不**输出候选文件正文、token / API key / Authorization 或完整 source config。

入口：``scripts/evidence_intake_handoff.py``（``--schema`` 打印单一 handoff 契约；
默认只打印 stdout，唯一写开关是显式 ``--out``）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common.redaction import safe_text
from src.evidence.contracts import (
    AUTHOR_ONLY_FIELDS,
    EVIDENCE_CONTRACT_VERSION,
    PERMISSION_FIELDS,
    EvidenceScope,
    canonical_field_names,
    required_field_names,
)
from src.evidence.handoff import thresholds
from src.evidence.inbox import (
    FILE_ENTRY_REQUIRED_FIELDS,
    MANIFEST_FILE_NAME,
    MANIFEST_REQUIRED_FIELDS,
    SUPPORTED_FILE_FORMATS,
    InboxPreflightReport,
    InboxStatus,
    scan_inbox,
)
from src.monitoring.evidence_gap_diagnostic import (
    GAP_CATEGORIES,
    ManifestDiagnostic,
    ManifestFact,
    build_manifest_diagnostic,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "INTAKE_HANDOFF_CONTRACT_VERSION",
    "INTAKE_HANDOFF_KIND",
    "INTAKE_HANDOFF_LAYOUT_NOTE",
    "INTAKE_HANDOFF_NOTE",
    "INTAKE_HANDOFF_SCHEMA_VERSION",
    "INTAKE_STATUS_ORDER",
    "MATERIAL_SPECS",
    "PREFLIGHT_PASS_SEMANTICS",
    "STATUS_MEANINGS",
    "TIME_SEMANTICS_REQUIREMENTS",
    "HandoffMaterial",
    "IntakeHandoffDocument",
    "IntakeStatus",
    "MaterialSpec",
    "PackageHandoff",
    "build_intake_handoff",
    "intake_handoff_schema",
    "load_intake_handoff",
    "render_intake_handoff_markdown",
    "unknown_contract_fields",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新 README / 测试
INTAKE_HANDOFF_SCHEMA_VERSION: Final[int] = 1
#: 文档标识（稳定，供人工与上游日志解析）
INTAKE_HANDOFF_KIND: Final[str] = "phase33_evidence_intake_handoff"
#: 输入契约版本：**只引用** ``evidence-intake-v1``，本模块不另造第二套契约
INTAKE_HANDOFF_CONTRACT_VERSION: Final[str] = EVIDENCE_CONTRACT_VERSION

#: 固定说明：预检结构完整与资格判定、Gate 决策完全独立
INTAKE_HANDOFF_NOTE: Final[str] = (
    "本 handoff 只做**纯本地只读预检**：不联网、不抓取、不绕过 robots / 证书、不写数据库、"
    "不训练模型、不触碰交易；只检查提交材料是否齐全、字段是否可解析、来源 / 时间声明是否带"
    "独立证据引用。资格判定（`evidence_qualified` / `data_qualification_passed` / "
    "`phase_transition_allowed`）与 L3 / L4 Gate **完全独立**，本工具**永不**参与，"
    f"`{PHASE3_3_BLOCKER_CODE}` 保持 BLOCKED。"
)
#: `preflight_pass` 的诚实语义（文档与机器字段都必须明确这一点）
PREFLIGHT_PASS_SEMANTICS: Final[str] = (
    "`preflight_pass=true` **只**表示 intake package **结构完整**"
    "（材料齐全、字段可解析、来源 / 时间声明带独立证据引用），"
    "**绝不**等于 `evidence_qualified=true`、**不解除** "
    f"`{PHASE3_3_BLOCKER_CODE}`、也**不**代表 L3 / L4 通过；"
    "结构完整只是**进入人工核验队列**的前提。"
)
#: 目录契约说明（只沿用 GOLD-011 inbox 布局，不另造第二套目录约定）
INTAKE_HANDOFF_LAYOUT_NOTE: Final[str] = (
    "handoff 包与 GOLD-011 inbox 候选包**同一布局**：显式 `--inbox-dir` 下的直接子目录"
    f"（或单文件），内含 `{MANIFEST_FILE_NAME}` 显式关联证据文件；本模块不移动 / 不删除 / "
    "不改写任何原始证据，也不调用任何 intake / commit 路径。"
)

#: 独立时间语义要求（**引用**既有契约语义，不新增 / 不放宽任何阈值）
TIME_SEMANTICS_REQUIREMENTS: Final[tuple[str, ...]] = (
    "`published_at` / `collected_at` 必须来自提交数据本身（ISO8601 且带时区）；"
    "程序**绝不**用当前时间、文件 mtime、抓取时间或推断值填补。",
    "`effective_at = max(published_at, collected_at)` 由契约**派生**，不需要人工提供，"
    "但两个来源字段必须真实可信。",
    "`available_at` 必须由**独立**可用性证据支撑（`availability_provenance` + "
    "`availability_reference`）；缺失即 `NOT_OOS_ELIGIBLE`（`AVAILABILITY_UNPROVEN`）。",
    "manifest 必须显式声明 `time_semantics` 与 `availability_semantics`（来源导出口径）；"
    "声明本身**不构成**证据，只用于人工核验。",
    "历史 OOS 资格独立于 `published_at < collected_at`：没有独立 `available_at` 证据一律不计 OOS。",
    "Mock / 模板 / 示例 / 合成材料（`is_mock` / `record_kind=example` / 名称命中示例词表）"
    "**永远** `NON_QUALIFYING`，不得用于达标。",
)


class IntakeStatus(StrEnum):
    """提交材料的五态状态（稳定字符串；只有人工 Gate 能改变最终资格结论）。"""

    #: 材料缺失，或已提交但机械校验未通过（不可用于人工核验）
    MISSING = "MISSING"
    #: 材料已提交，但独立证据引用 / 机械校验结论不足，尚不能进入人工核验队列
    PRESENT_UNVERIFIED = "PRESENT_UNVERIFIED"
    #: 材料结构完整、可进入人工核验队列：法律效力 / 出处 / 时间语义只能人工判定
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    #: Mock / 模板 / 示例 / 合成材料：**永远**不计资格
    NON_QUALIFYING = "NON_QUALIFYING"
    #: ``PHASE3_3_DATA`` 与 L3 / L4 人工 Gate：工具永不具备解除或推进能力
    GATE_BLOCKED = "GATE_BLOCKED"


#: 状态的固定输出顺序（计数与渲染共用，保证确定性）
INTAKE_STATUS_ORDER: Final[tuple[IntakeStatus, ...]] = (
    IntakeStatus.MISSING,
    IntakeStatus.PRESENT_UNVERIFIED,
    IntakeStatus.HUMAN_VERIFICATION_REQUIRED,
    IntakeStatus.NON_QUALIFYING,
    IntakeStatus.GATE_BLOCKED,
)

#: 状态的机器可读释义（与文档同源，供人工与上游解析）
STATUS_MEANINGS: Final[dict[str, str]] = {
    IntakeStatus.MISSING.value: (
        "提交材料缺失，或已提交但机械校验未通过（不可用于人工核验）—— fail-closed；"
        "不得用当前时间 / 文件 mtime / 推断值补齐。"
    ),
    IntakeStatus.PRESENT_UNVERIFIED.value: (
        "材料已提交，但独立证据引用 / 机械校验结论不足，尚不能进入人工核验队列。"
    ),
    IntakeStatus.HUMAN_VERIFICATION_REQUIRED.value: (
        "材料结构完整，已进入人工核验队列：法律效力 / 出处 / 时间语义只能人工判定。"
    ),
    IntakeStatus.NON_QUALIFYING.value: "Mock / 模板 / 示例 / 合成材料：**永远**不计入真实资格。",
    IntakeStatus.GATE_BLOCKED.value: (
        f"`{PHASE3_3_BLOCKER_CODE}` 与 L3 / L4 人工 Gate：本预检永不具备解除或推进能力。"
    ),
}


@dataclass(frozen=True, slots=True)
class MaterialSpec:
    """一条 handoff 材料要求（**只引用**既有契约字段名，不新增阈值、不新增列）。"""

    key: str
    category: str
    scope: str
    requirement: str
    contract_fields: tuple[str, ...]
    reference_fields: tuple[str, ...]
    declaration_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "requirement": self.requirement,
            "contract_fields": list(self.contract_fields),
            "reference_fields": list(self.reference_fields),
            "declaration_fields": list(self.declaration_fields),
        }


#: handoff 材料契约（顺序即输出顺序；`category` 与 GOLD-026 缺口分类**同一词表**）
MATERIAL_SPECS: Final[tuple[MaterialSpec, ...]] = (
    MaterialSpec(
        key="evidence_records",
        category="source",
        scope="both",
        requirement=(
            "候选包内必须有 ≥1 条可解析、SHA-256 摘要核对通过、逐行机械校验通过的真实数据行"
            "（csv / jsonl）；空文件、不可读文件与全部被隔离的包一律 fail-closed。"
        ),
        contract_fields=("source", "source_record_id", "content", "content_ref"),
        reference_fields=(),
        declaration_fields=("evidence_type", "files"),
    ),
    MaterialSpec(
        key="source_identity",
        category="source",
        scope="both",
        requirement=(
            "来源身份与出处：manifest 声明 `source`（平台 / 供应商名称，不得用模型名冒充），"
            "每行提供 `source` / `source_record_id` 与可复核的 `provenance_reference`"
            "（+ 可选 `url`）。"
        ),
        contract_fields=("source", "source_record_id", "provenance_reference", "url"),
        reference_fields=("provenance_reference", "url"),
        declaration_fields=("source",),
    ),
    MaterialSpec(
        key="authorization_declaration",
        category="authorization",
        scope="both",
        requirement=(
            "授权证明：`authorization_status=APPROVED`、`authorization_basis` ∈ 白名单、"
            "`authorization_reference` 为可复核引用（https 或 docs/legal/ 路径）、"
            "`authorization_reviewed_by` 为实际核验的人工、`authorization_reviewed_at` 带时区，"
            "且三项 `permits_*` 均为 true。"
        ),
        contract_fields=(
            "authorization_status",
            "authorization_basis",
            "authorization_reference",
            "authorization_reviewed_by",
            "authorization_reviewed_at",
            "authorization_valid_from",
            "authorization_expires_at",
            *PERMISSION_FIELDS,
        ),
        reference_fields=("authorization_reference",),
        declaration_fields=("authorization_reference",),
    ),
    MaterialSpec(
        key="time_semantics",
        category="time_semantics",
        scope="both",
        requirement=(
            "独立时间语义声明：manifest `time_semantics` 必须说明 `published_at` / "
            "`collected_at` 的时区口径与导出方式（不接受「用采集当天时间回填」）。"
        ),
        contract_fields=(),
        reference_fields=(),
        declaration_fields=("time_semantics",),
    ),
    MaterialSpec(
        key="published_at",
        category="published_at",
        scope="both",
        requirement=(
            "`published_at`：每行必须为**原始发布时间**（ISO8601 且带时区、不得为未来时间）；"
            "缺失 / naive / 非法一律 fail-closed。"
        ),
        contract_fields=("published_at",),
        reference_fields=(),
        declaration_fields=("time_semantics",),
    ),
    MaterialSpec(
        key="collected_at",
        category="collected_at",
        scope="both",
        requirement=(
            "`collected_at`：每行为**独立采集 / 导出时间**（带时区、不早于 `published_at`），"
            "且必须能追溯到导出记录。"
        ),
        contract_fields=("collected_at",),
        reference_fields=(),
        declaration_fields=("time_semantics",),
    ),
    MaterialSpec(
        key="effective_at",
        category="effective_at",
        scope="both",
        requirement=(
            "`effective_at`：由契约 `max(published_at, collected_at)` 派生"
            "（不需要人工提供，但两个来源字段必须真实可信）。"
        ),
        contract_fields=("published_at", "collected_at"),
        reference_fields=(),
        declaration_fields=("time_semantics",),
    ),
    MaterialSpec(
        key="availability_oos",
        category="availability_oos",
        scope="both",
        requirement=(
            "独立历史可用证据（OOS）：每行 `available_at`（带时区）+ "
            "`availability_provenance` + `availability_reference`，且 manifest 声明 "
            "`availability_semantics` 与 `historical_oos_applicable=true`；"
            "缺失即 `NOT_OOS_ELIGIBLE`。"
        ),
        contract_fields=("available_at", "availability_provenance", "availability_reference"),
        reference_fields=("availability_reference",),
        declaration_fields=("availability_semantics", "historical_oos_applicable"),
    ),
    MaterialSpec(
        key="author_identity",
        category="source",
        scope="author",
        requirement=(
            "Author 证据必须提供 `author_name` 与 `external_account_id`"
            "（平台账号稳定 ID，不得填昵称副本）。"
        ),
        contract_fields=tuple(AUTHOR_ONLY_FIELDS),
        reference_fields=(),
        declaration_fields=(),
    ),
    MaterialSpec(
        key="phase33_data_gate",
        category="gate",
        scope="both",
        requirement=(
            f"`{PHASE3_3_BLOCKER_CODE}` 与 L3 / L4 人工 Gate：预检工具**不具备**解除 blocker、"
            "签发 review 或推进 Phase 的能力。"
        ),
        contract_fields=(),
        reference_fields=(),
        declaration_fields=(),
    ),
)

#: Gate 材料 key（机器判定恒为 ``GATE_BLOCKED``）
_GATE_KEY: Final[str] = "phase33_data_gate"
_SPEC_BY_KEY: Final[dict[str, MaterialSpec]] = {spec.key: spec for spec in MATERIAL_SPECS}


# ---- 判定输入：把 manifest 字段级事实翻译为材料级判定（只读、零 I/O）----------


def _inline(values: tuple[str, ...]) -> str:
    """把字段名 / 引用渲染为脱敏内联引用（空集合显示 ``—``）。"""
    return " / ".join(f"`{value}`" for value in values) if values else "—"


@dataclass(frozen=True, slots=True)
class _FactView:
    """一个候选包 manifest 事实的**只读判定视图**（零 I/O、零网络、零数据库）。"""

    fact: ManifestFact

    @property
    def declared(self) -> frozenset[str]:
        """manifest **已**声明的必填字段（字段级事实，来自 GOLD-026）。"""
        return frozenset(self.fact.declared_fields)

    @property
    def missing(self) -> frozenset[str]:
        """manifest **未**声明的必填字段（绝不推断、绝不用默认值补齐）。"""
        return frozenset(self.fact.missing_fields)

    @property
    def rows_observed(self) -> bool:
        """是否真的解析到过数据行（未被摘要 / 文件层面校验短路）。"""
        return self.fact.acceptable_rows + self.fact.quarantined_rows > 0

    @property
    def rows_clean(self) -> bool:
        """是否**没有**任何被隔离的行（行级机械校验整体通过）。"""
        return self.fact.quarantined_rows == 0

    @property
    def rows_usable(self) -> bool:
        """既有可核对行、又没有隔离行（进入人工核验的最小前提）。"""
        return self.rows_observed and self.rows_clean

    @property
    def oos_applicable(self) -> bool:
        """manifest 是否显式声明该包适用于历史 OOS（``false`` / 缺失一律不算）。"""
        return self.fact.historical_oos_applicable is True

    @property
    def preflight_pass(self) -> bool:
        """GOLD-011 inbox 预检是否**零**原因码（``PREFLIGHT_PASS`` 也只进人工队列）。"""
        return self.fact.status == InboxStatus.PREFLIGHT_PASS.value

    @property
    def reason_text(self) -> str:
        """稳定原因码的内联呈现（无原因码显示 ``—``）。"""
        if not self.fact.reason_codes:
            return "—"
        return " / ".join(f"`{code}`" for code in self.fact.reason_codes)


@dataclass(frozen=True, slots=True)
class _MaterialFacts:
    """单个材料的**机械事实**（``present`` / ``proven`` 与字段级明细；零推断值）。"""

    present: bool
    proven: bool
    present_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    references: tuple[str, ...]
    machine_fact: str


def _records_facts(view: _FactView) -> _MaterialFacts:
    """可核对数据行是否已提交（摘要 + 逐行机械校验通过）。"""
    names = ("files",)
    acceptable = view.fact.acceptable_rows
    usable = acceptable > 0 and view.rows_clean
    return _MaterialFacts(
        present=view.rows_observed,
        proven=usable,
        present_fields=names if usable else (),
        missing_fields=() if usable else names,
        references=(f"acceptable_rows={acceptable}",),
        machine_fact=(
            f"可核对行 {acceptable} 条 / 被隔离行 {view.fact.quarantined_rows} 条；"
            f"manifest 已声明={_inline(tuple(sorted(view.declared)))}；"
            f"缺声明={_inline(tuple(sorted(view.missing)))}"
        ),
    )


def _source_identity_facts(view: _FactView) -> _MaterialFacts:
    """来源身份与出处（含可复核引用）。"""
    #: 行级**必填**字段（``url`` 是可选字段，不在此列，也绝不报告为"缺失"）
    row_names = ("source_record_id", "provenance_reference")
    declared = "source" in view.declared
    present_fields: list[str] = []
    if declared:
        present_fields.append("source")
        if view.rows_usable:
            present_fields.extend(row_names)
    missing_fields: list[str] = []
    if "source" in view.missing:
        missing_fields.append("source")
    if not view.rows_usable:
        missing_fields.extend(row_names)
    return _MaterialFacts(
        present=declared,
        proven=declared and view.rows_usable,
        present_fields=tuple(present_fields),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        references=(),
        machine_fact=(
            f"manifest source 声明={str(declared).lower()}；"
            f"行级机械校验通过={str(view.rows_usable).lower()}；"
            "`url` 为可选字段（缺失不算缺口）"
        ),
    )



def _authorization_facts(view: _FactView) -> _MaterialFacts:
    """授权证明（授权声明 + 人工核验人与核验时间 + 三项用途许可）。"""
    row_names = (
        "authorization_status",
        "authorization_basis",
        "authorization_reviewed_by",
        "authorization_reviewed_at",
        *PERMISSION_FIELDS,
    )
    declared = "authorization_reference" in view.declared
    present_fields: list[str] = []
    if declared:
        present_fields.append("authorization_reference")
        if view.rows_usable:
            present_fields.extend(row_names)
    missing_fields: list[str] = []
    if "authorization_reference" in view.missing:
        missing_fields.append("authorization_reference")
    if not view.rows_usable:
        missing_fields.extend(row_names)
    return _MaterialFacts(
        present=declared,
        proven=declared and view.rows_usable,
        present_fields=tuple(present_fields),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        references=(),
        machine_fact=(
            f"manifest authorization_reference 声明={str(declared).lower()}；"
            f"行级机械校验通过={str(view.rows_usable).lower()}；"
            "授权法律效力与许可范围只能人工核验"
        ),
    )


def _time_semantics_facts(view: _FactView) -> _MaterialFacts:
    """独立时间语义声明（manifest ``time_semantics``）。"""
    declared = "time_semantics" in view.declared
    return _MaterialFacts(
        present=declared,
        proven=declared,
        present_fields=("time_semantics",) if declared else (),
        missing_fields=() if declared else ("time_semantics",),
        references=(),
        machine_fact=(
            f"manifest time_semantics={'已声明' if declared else '未声明'}；"
            "声明内容只作人工核验输入，程序不据此放行"
        ),
    )


def _row_time_facts(view: _FactView, *, field: str) -> _MaterialFacts:
    """行级时间字段（``published_at`` / ``collected_at``）：只报告事实，绝不填补。"""
    present = view.rows_observed
    usable = view.rows_usable
    return _MaterialFacts(
        present=present,
        proven=usable,
        present_fields=(field,) if usable else (),
        missing_fields=() if present else (field,),
        references=(),
        machine_fact=(
            f"{field}：{'已解析到数据行' if present else '未解析到任何数据行'}；"
            f"被隔离行 {view.fact.quarantined_rows} 条；"
            "程序**不**使用当前时间 / 文件 mtime / 抓取时间 / 推断值填补"
        ),
    )


def _effective_facts(view: _FactView) -> _MaterialFacts:
    """``effective_at``：契约派生值（``max(published_at, collected_at)``）。"""
    present = view.rows_observed
    usable = view.rows_usable
    return _MaterialFacts(
        present=present,
        proven=usable,
        present_fields=("published_at", "collected_at") if usable else (),
        missing_fields=() if present else ("published_at", "collected_at"),
        references=(),
        machine_fact=(
            "effective_at = max(published_at, collected_at)（契约派生）；"
            f"{'已解析到数据行' if present else '未解析到任何数据行'}"
        ),
    )



def _availability_facts(view: _FactView) -> _MaterialFacts:
    """独立历史可用证据（OOS）：``available_at`` + provenance + reference。"""
    semantics_declared = "availability_semantics" in view.declared
    row_names = ("available_at", "availability_provenance", "availability_reference")
    # 材料 = **声明**（availability_semantics）+ 逐行独立可用证据：两者缺一即视为缺失
    present = semantics_declared and view.rows_observed
    usable_rows = view.rows_usable and view.fact.not_oos_eligible_rows == 0
    proven = semantics_declared and view.oos_applicable and usable_rows
    present_fields: list[str] = []
    if semantics_declared:
        present_fields.append("availability_semantics")
    if usable_rows:
        present_fields.extend(row_names)
    missing_fields: list[str] = []
    if "availability_semantics" in view.missing:
        missing_fields.append("availability_semantics")
    if not view.oos_applicable:
        missing_fields.append("historical_oos_applicable")
    if not usable_rows and view.fact.not_oos_eligible_rows > 0:
        # 只报告**真正缺的独立证据**：独立可用时间（provenance / reference 由要求文本约束）
        missing_fields.append("available_at")
    return _MaterialFacts(
        present=present,
        proven=proven,
        present_fields=tuple(present_fields),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        references=(),
        machine_fact=(
            f"availability_semantics={'已声明' if semantics_declared else '未声明'}；"
            f"historical_oos_applicable={view.fact.historical_oos_applicable}；"
            f"缺独立可用时间的行 {view.fact.not_oos_eligible_rows} 条；"
            f"声明口径（脱敏）：{safe_text(view.fact.availability_semantics, max_chars=120) or '—'}"
        ),
    )


def _author_identity_facts(view: _FactView) -> _MaterialFacts:
    """Author 专有：作者显示名 + 平台账号稳定 ID。"""
    names = tuple(AUTHOR_ONLY_FIELDS)
    present = view.rows_observed
    usable = view.rows_usable
    return _MaterialFacts(
        present=present,
        proven=usable,
        present_fields=names if usable else (),
        missing_fields=() if present else names,
        references=(),
        machine_fact=(
            f"Author 身份字段 {_inline(names)}："
            f"{'行级机械校验通过' if usable else '未通过 / 未解析到数据行'}"
        ),
    )


#: ``材料 key -> 机械判定器``（Gate 材料单独处理；覆盖性在运行期 fail-closed 校验）
_MATERIAL_EVALUATORS: Final[dict[str, Callable[[_FactView], _MaterialFacts]]] = {
    "evidence_records": _records_facts,
    "source_identity": _source_identity_facts,
    "authorization_declaration": _authorization_facts,
    "time_semantics": _time_semantics_facts,
    "published_at": lambda view: _row_time_facts(view, field="published_at"),
    "collected_at": lambda view: _row_time_facts(view, field="collected_at"),
    "effective_at": _effective_facts,
    "availability_oos": _availability_facts,
    "author_identity": _author_identity_facts,
}


def _check_spec_coverage() -> None:
    """契约漂移守卫：每个材料 spec 必须恰好有判定器（Gate 材料除外）。

    Raises:
        ValueError: 新增材料 spec 却忘记登记判定器（绝不静默按"已通过"处理）。
    """
    covered = set(_MATERIAL_EVALUATORS) | {_GATE_KEY}
    missing = sorted(set(_SPEC_BY_KEY) - covered)
    if missing:
        raise ValueError(f"handoff 材料契约漂移（缺少判定器）：{missing}")


def unknown_contract_fields() -> tuple[str, ...]:
    """返回 handoff schema 中**不在** ``evidence-intake-v1`` 契约里的字段名。

    健康树恒为空：这是"只引用既有契约字段、不偷偷加列"的机器可检查断言；
    任何新增字段都必须先在 :mod:`src.evidence.contracts` 完成（并升契约版本）。
    """
    known = set(canonical_field_names())
    unknown: set[str] = set()
    for spec in MATERIAL_SPECS:
        for name in spec.contract_fields:
            if name not in known:
                unknown.add(name)
    return tuple(sorted(unknown))



# ---- 结论对象：材料 / 包 / 文档（同一事实来源，全部脱敏）---------------------


@dataclass(frozen=True, slots=True)
class HandoffMaterial:
    """一条材料的预检结论（``counts_toward_eligibility`` 恒为 false）。"""

    key: str
    category: str
    scope: str
    status: IntakeStatus
    requirement: str
    contract_fields: tuple[str, ...]
    reference_fields: tuple[str, ...]
    declaration_fields: tuple[str, ...]
    present_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    machine_fact: str
    human_next_step: str

    @property
    def human_verification_required(self) -> bool:
        """是否已进入**人工核验**队列（``preflight_pass`` 的唯一合法前提之一）。"""
        return self.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "status": self.status.value,
            "requirement": self.requirement,
            "contract_fields": list(self.contract_fields),
            "reference_fields": list(self.reference_fields),
            "declaration_fields": list(self.declaration_fields),
            "present_fields": list(self.present_fields),
            "missing_fields": list(self.missing_fields),
            "machine_fact": self.machine_fact,
            "human_next_step": self.human_next_step,
            # Mock / 模板 / 示例 / 结构完整都**不计**资格：恒为 false（硬编码）
            "counts_toward_eligibility": False,
        }


def _human_next_step(spec: MaterialSpec, status: IntakeStatus, facts: _MaterialFacts) -> str:
    """给出该材料状态下的**人工下一步**（只陈述动作，不做资格判定）。"""
    if status is IntakeStatus.GATE_BLOCKED:
        return (
            f"`{PHASE3_3_BLOCKER_CODE}` 与 L3 / L4 只能由人工 Gate 推进："
            "本预检不解除 blocker、不签发 review、不推进 Phase。"
        )
    if status is IntakeStatus.NON_QUALIFYING:
        return "该材料为 Mock / 模板 / 示例 / 合成：**永远**不计资格，请改用真实授权证据重新提交。"
    if status is IntakeStatus.MISSING:
        missing = _inline(facts.missing_fields) if facts.missing_fields else spec.requirement
        return f"按 handoff 契约补齐 {missing} 后重新提交；Mock / 模板 / 推断值一律不接受。"
    if status is IntakeStatus.PRESENT_UNVERIFIED:
        return (
            "独立证据引用 / 机械校验仍不足（见 machine_fact）：补齐可核对引用与证据后重跑预检；"
            "本预检不因此解除 blocker。"
        )
    return (
        f"材料结构完整：请人工核验 `{spec.key}` 的真实性 / 法律效力 / 出处与时间语义"
        "（程序不判定，Phase 切换仍须 L3 人工 Gate）。"
    )


def _material(spec: MaterialSpec, view: _FactView) -> HandoffMaterial:
    """由材料契约 + 只读事实判定单个材料（Gate 材料恒为 ``GATE_BLOCKED``）。"""
    if spec.key == _GATE_KEY:
        facts = _MaterialFacts(
            present=True,
            proven=False,
            present_fields=(),
            missing_fields=(),
            references=(PHASE3_3_BLOCKER_CODE,),
            machine_fact=(
                f"`{PHASE3_3_BLOCKER_CODE}` 保持 BLOCKED；本预检**没有**资格判定、"
                "签发 review 或推进 Phase 的能力"
            ),
        )
        status = IntakeStatus.GATE_BLOCKED
    else:
        facts = _MATERIAL_EVALUATORS[spec.key](view)
        if view.fact.synthetic:
            status = IntakeStatus.NON_QUALIFYING
        elif not facts.present:
            status = IntakeStatus.MISSING
        elif not facts.proven:
            status = IntakeStatus.PRESENT_UNVERIFIED
        else:
            status = IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    return HandoffMaterial(
        key=spec.key,
        category=spec.category,
        scope=spec.scope,
        status=status,
        requirement=spec.requirement,
        contract_fields=spec.contract_fields,
        reference_fields=spec.reference_fields,
        declaration_fields=spec.declaration_fields,
        present_fields=facts.present_fields,
        # Mock / 模板 / 示例不是"缺失"，而是**不计资格**：绝不报告为缺失字段
        missing_fields=() if status is IntakeStatus.NON_QUALIFYING else facts.missing_fields,
        machine_fact=safe_text(facts.machine_fact, max_chars=400),
        human_next_step=safe_text(_human_next_step(spec, status, facts), max_chars=400),
    )



@dataclass(frozen=True, slots=True)
class PackageHandoff:
    """单个候选包的 handoff 预检结论（``preflight_pass`` 只表示**结构完整**）。"""

    package_dir: str
    fingerprint: str
    scope: str
    inbox_status: str
    synthetic: bool
    preflight_pass: bool
    evidence_qualified: bool
    gate_blocked: bool
    status_counts: tuple[tuple[str, int], ...]
    materials: tuple[HandoffMaterial, ...]
    notes: tuple[str, ...]

    def material(self, key: str) -> HandoffMaterial:
        """按 key 取材料结论；缺失即显式失败（防止口径漂移被静默忽略）。"""
        for item in self.materials:
            if item.key == key:
                return item
        raise KeyError(key)

    def status_of(self, key: str) -> IntakeStatus:
        """按 key 取材料状态（缺失 → ``KeyError``）。"""
        return self.material(key).status

    def count(self, status: IntakeStatus) -> int:
        """指定状态的材料数量。"""
        return sum(1 for item in self.materials if item.status is status)

    def keys_with(self, *statuses: IntakeStatus) -> tuple[str, ...]:
        """指定状态的材料 key（保持确定性顺序）。"""
        wanted = set(statuses)
        return tuple(item.key for item in self.materials if item.status in wanted)

    @property
    def missing_materials(self) -> tuple[str, ...]:
        """仍 ``MISSING`` 的材料（结构缺口）。"""
        return self.keys_with(IntakeStatus.MISSING)

    @property
    def present_unverified_materials(self) -> tuple[str, ...]:
        """``PRESENT_UNVERIFIED`` 的材料（已提交但独立证据引用不足）。"""
        return self.keys_with(IntakeStatus.PRESENT_UNVERIFIED)

    @property
    def human_verification_materials(self) -> tuple[str, ...]:
        """``HUMAN_VERIFICATION_REQUIRED`` 的材料（只能人工核验）。"""
        return self.keys_with(IntakeStatus.HUMAN_VERIFICATION_REQUIRED)

    @property
    def non_qualifying_materials(self) -> tuple[str, ...]:
        """``NON_QUALIFYING`` 的材料（Mock / 模板 / 示例）。"""
        return self.keys_with(IntakeStatus.NON_QUALIFYING)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**：不含正文、不含原始行值）。"""
        return {
            "package_dir": self.package_dir,
            "fingerprint": self.fingerprint,
            "scope": self.scope,
            "inbox_status": self.inbox_status,
            "synthetic": self.synthetic,
            "preflight_pass": self.preflight_pass,
            "preflight_pass_meaning": PREFLIGHT_PASS_SEMANTICS,
            "evidence_qualified": self.evidence_qualified,
            "gate_blocked": self.gate_blocked,
            "status_counts": {name: count for name, count in self.status_counts},
            "missing_materials": list(self.missing_materials),
            "present_unverified_materials": list(self.present_unverified_materials),
            "human_verification_materials": list(self.human_verification_materials),
            "non_qualifying_materials": list(self.non_qualifying_materials),
            "materials": [item.to_dict() for item in self.materials],
            "notes": list(self.notes),
        }


#: 合成 / 示例 / 模板候选包的固定说明（醒目：永远不计资格）
_SYNTHETIC_PACKAGE_NOTE: Final[str] = (
    "候选包带显式示例 / 合成 / Mock 标记（或名称命中示例词表）：所有材料恒 "
    "`NON_QUALIFYING`，**永远**不得用于达标。"
)


def _specs_for_scope(scope: str) -> tuple[MaterialSpec, ...]:
    """按候选包 scope 取材料契约（Author 专有材料只对 ``author`` 评估）。"""
    return tuple(
        spec for spec in MATERIAL_SPECS if spec.scope == "both" or spec.scope == scope
    )


def _package_handoff(fact: ManifestFact) -> PackageHandoff:
    """把一个 manifest 事实判定为一个 handoff 结论（纯函数）。"""
    view = _FactView(fact)
    materials = tuple(_material(spec, view) for spec in _specs_for_scope(fact.scope))
    status_counts = tuple(
        (status.value, sum(1 for item in materials if item.status is status))
        for status in INTAKE_STATUS_ORDER
    )
    preflight_pass = (
        view.preflight_pass
        and not view.fact.synthetic
        and all(
            item.status
            in {IntakeStatus.HUMAN_VERIFICATION_REQUIRED, IntakeStatus.GATE_BLOCKED}
            for item in materials
        )
    )
    notes: list[str] = [PREFLIGHT_PASS_SEMANTICS]
    if view.fact.synthetic:
        notes.append(_SYNTHETIC_PACKAGE_NOTE)
    if not view.preflight_pass:
        notes.append(f"GOLD-011 inbox 预检未通过（fail-closed）：原因码 {view.reason_text}。")
    return PackageHandoff(
        package_dir=view.fact.package_dir,
        fingerprint=view.fact.fingerprint,
        scope=view.fact.scope,
        inbox_status=view.fact.status,
        synthetic=view.fact.synthetic,
        preflight_pass=preflight_pass,
        # 结构完整 ≠ 资格通过：恒为 false（硬编码，不被任何输入透传）
        evidence_qualified=False,
        # Gate 恒为 blocked：工具永不具备解除能力
        gate_blocked=True,
        status_counts=status_counts,
        materials=materials,
        notes=tuple(notes),
    )



@dataclass(frozen=True, slots=True)
class IntakeHandoffDocument:
    """``PHASE3_3_DATA`` 人工证据 Intake Handoff 预检文档（稳定 JSON + 可读 Markdown 同源）。"""

    schema_version: int
    kind: str
    contract_version: str
    as_of: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    data_qualification_passed: bool
    phase_transition_allowed: bool
    gate_blocked: bool
    evidence_qualified: bool
    advance_allowed: bool
    l3_l4_auto_advance_allowed: bool
    inbox_dir: str | None
    scanned: bool
    preflight_pass_count: int
    synthetic_count: int
    status_counts: tuple[tuple[str, int], ...]
    materials: tuple[MaterialSpec, ...]
    packages: tuple[PackageHandoff, ...]
    thresholds: tuple[tuple[str, float], ...]
    notes: tuple[str, ...]

    @property
    def package_count(self) -> int:
        """候选包数量。"""
        return len(self.packages)

    @property
    def incomplete_count(self) -> int:
        """结构**不**完整的候选包数量（缺失 / 待核验 / 合成 / 预检未通过）。"""
        return sum(1 for package in self.packages if not package.preflight_pass)

    def count(self, status: IntakeStatus) -> int:
        """指定状态的材料总数（跨候选包）。"""
        return sum(package.count(status) for package in self.packages)

    @property
    def open_material_gap_count(self) -> int:
        """结构缺口数（``MISSING`` + ``PRESENT_UNVERIFIED``；> 0 表示仍不完整）。"""
        return self.count(IntakeStatus.MISSING) + self.count(IntakeStatus.PRESENT_UNVERIFIED)

    def package(self, fingerprint: str) -> PackageHandoff:
        """按指纹取候选包结论；缺失即显式失败。"""
        for package in self.packages:
            if package.fingerprint == fingerprint:
                return package
        raise KeyError(fingerprint)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（含版本化 schema 段；**全部脱敏**）。"""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "contract_version": self.contract_version,
            "as_of": self.as_of.isoformat(),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "gate_blocked": self.gate_blocked,
            "evidence_qualified": self.evidence_qualified,
            "advance_allowed": self.advance_allowed,
            "l3_l4_auto_advance_allowed": self.l3_l4_auto_advance_allowed,
            "inbox_dir": self.inbox_dir,
            "scanned": self.scanned,
            "package_count": self.package_count,
            "preflight_pass_count": self.preflight_pass_count,
            "incomplete_count": self.incomplete_count,
            "synthetic_count": self.synthetic_count,
            "open_material_gap_count": self.open_material_gap_count,
            "status_counts": {name: count for name, count in self.status_counts},
            "thresholds": {name: value for name, value in self.thresholds},
            "schema": intake_handoff_schema(),
            "packages": [package.to_dict() for package in self.packages],
            "notes": list(self.notes),
        }


def intake_handoff_schema() -> dict[str, Any]:
    """返回**版本化** handoff 提交契约（人工据此准备材料；机器可读、可 diff）。

    这是"单一 handoff 契约"的机器可读来源：材料清单、字段引用、目录布局、状态词表与
    ``preflight_pass`` 的诚实语义都在此声明；阈值**只引用** ``src.alpha.evidence_gate``
    （经 :func:`src.evidence.handoff.thresholds`），本函数**不新增、不降低**任何资格门槛。
    """
    return {
        "kind": INTAKE_HANDOFF_KIND,
        "schema_version": INTAKE_HANDOFF_SCHEMA_VERSION,
        "contract_version": INTAKE_HANDOFF_CONTRACT_VERSION,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "package_layout": {
            "root": "显式 --inbox-dir（**本地**目录；候选包 = 其直接子目录或单文件）",
            "manifest_file_name": MANIFEST_FILE_NAME,
            "manifest_required_fields": list(MANIFEST_REQUIRED_FIELDS),
            "file_entry_required_fields": list(FILE_ENTRY_REQUIRED_FIELDS),
            "supported_file_formats": list(SUPPORTED_FILE_FORMATS),
            "layout_note": INTAKE_HANDOFF_LAYOUT_NOTE,
        },
        "scope_contract_fields": {
            scope.value: {
                "required_contract_fields": list(required_field_names(scope)),
                "author_only_contract_fields": (
                    list(AUTHOR_ONLY_FIELDS) if scope is EvidenceScope.AUTHOR else []
                ),
            }
            for scope in EvidenceScope
        },
        "materials": [spec.to_dict() for spec in MATERIAL_SPECS],
        "statuses": [status.value for status in INTAKE_STATUS_ORDER],
        "status_meanings": {name: STATUS_MEANINGS[name] for name in STATUS_MEANINGS},
        "preflight_pass_semantics": PREFLIGHT_PASS_SEMANTICS,
        "preflight_pass_does_not_imply": [
            "evidence_qualified",
            "data_qualification_passed",
            f"{PHASE3_3_BLOCKER_CODE}_unblocked",
            "l3_human_gate_passed",
            "l4_review_issued",
            "phase_transition_allowed",
        ],
        "time_semantics_requirements": list(TIME_SEMANTICS_REQUIREMENTS),
        "gap_categories": list(GAP_CATEGORIES),
        "thresholds": {name: value for name, value in sorted(thresholds().items())},
        "notes": [INTAKE_HANDOFF_NOTE, PREFLIGHT_PASS_SEMANTICS],
    }



def build_intake_handoff(
    manifest: ManifestDiagnostic, *, as_of: datetime
) -> IntakeHandoffDocument:
    """由**只读** manifest 事实构造 handoff 预检文档（纯函数；不触库、不联网、不写文件）。

    Args:
        manifest: :func:`src.monitoring.evidence_gap_diagnostic.build_manifest_diagnostic`
            的结果（``scanned=false`` 表示未提供候选目录）。
        as_of: 审计时点（必须带时区；用于可复现与行级"未来时间"判定，不做任何填补）。

    Returns:
        :class:`IntakeHandoffDocument`；``evidence_qualified`` /
        ``data_qualification_passed`` / ``phase_transition_allowed`` / ``advance_allowed`` /
        ``l3_l4_auto_advance_allowed`` 恒为 ``False``。

    Raises:
        ValueError: ``as_of`` 未带时区，或材料契约漂移（缺少判定器）。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    _check_spec_coverage()
    moment = as_of.astimezone(UTC)
    packages = tuple(_package_handoff(fact) for fact in manifest.facts)
    status_counts = tuple(
        (status.value, sum(package.count(status) for package in packages))
        for status in INTAKE_STATUS_ORDER
    )
    return IntakeHandoffDocument(
        schema_version=INTAKE_HANDOFF_SCHEMA_VERSION,
        kind=INTAKE_HANDOFF_KIND,
        contract_version=INTAKE_HANDOFF_CONTRACT_VERSION,
        as_of=moment,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        # 以下安全字段全部**硬编码**：任何输入 / 上游报告都无法把它们改成"通过"
        blocker_active=True,
        human_gate_required=True,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        gate_blocked=True,
        evidence_qualified=False,
        advance_allowed=False,
        l3_l4_auto_advance_allowed=False,
        inbox_dir=manifest.inbox_dir,
        scanned=manifest.scanned,
        preflight_pass_count=sum(1 for package in packages if package.preflight_pass),
        synthetic_count=sum(1 for package in packages if package.synthetic),
        status_counts=status_counts,
        materials=MATERIAL_SPECS,
        packages=packages,
        thresholds=tuple(sorted(thresholds().items())),
        notes=(
            INTAKE_HANDOFF_NOTE,
            PREFLIGHT_PASS_SEMANTICS,
            INTAKE_HANDOFF_LAYOUT_NOTE,
            *manifest.notes,
        ),
    )


def load_intake_handoff(
    inbox_dir: str | Path, *, as_of: datetime
) -> IntakeHandoffDocument:
    """**只读**预检一个显式给出的**本地**候选目录（零网络 / 零数据库 / 零写入）。

    Args:
        inbox_dir: 显式给出的本地目录（候选包 = 其直接子目录或单文件）。
        as_of: 审计时点（必须带时区；缺省由 CLI 提供）。

    Returns:
        :class:`IntakeHandoffDocument`（结构完整 ≠ 资格通过）。

    Raises:
        ValueError: ``as_of`` 未带时区。
        src.evidence.inbox.InboxDirError: 目录不存在 / 不是目录 / 不可读（fail-closed）。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    moment = as_of.astimezone(UTC)
    report: InboxPreflightReport = scan_inbox(Path(inbox_dir), moment=moment)
    return build_intake_handoff(build_manifest_diagnostic(report), as_of=moment)



def render_intake_handoff_markdown(document: IntakeHandoffDocument) -> str:
    """渲染人类可读的 handoff 预检摘要（脱敏；**不解除** blocker、不推进 Phase）。"""
    counts = "；".join(f"{name}={count}" for name, count in document.status_counts)
    lines: list[str] = [
        "# Phase 3.3 人工证据 Intake Handoff 预检",
        "",
        f"> blocker `{document.blocker_code}`：active={str(document.blocker_active).lower()}；"
        f"human_gate_required={str(document.human_gate_required).lower()}；"
        "**结构完整 ≠ 资格通过**。",
        "",
        f"- 契约版本：`{document.contract_version}`；审计时点（UTC）：{document.as_of.isoformat()}",
        f"- 候选包：{document.package_count}；preflight_pass（仅结构完整）："
        f"{document.preflight_pass_count}；结构不完整：{document.incomplete_count}；"
        f"合成包：{document.synthetic_count}",
        f"- 材料状态计数：{counts}",
        f"- evidence_qualified={str(document.evidence_qualified).lower()}；"
        f"data_qualification_passed={str(document.data_qualification_passed).lower()}；"
        f"phase_transition_allowed={str(document.phase_transition_allowed).lower()}；"
        f"advance_allowed={str(document.advance_allowed).lower()}；"
        f"l3_l4_auto_advance_allowed={str(document.l3_l4_auto_advance_allowed).lower()}",
        f"- {PREFLIGHT_PASS_SEMANTICS}",
        "",
        "## 1. 单一 handoff 契约（Author / News 材料）",
        "",
        "| 材料 | 分类 | 适用 | 要求 | 契约字段 | 独立引用字段 |",
        "|---|---|---|---|---|---|",
    ]
    for spec in document.materials:
        lines.append(
            f"| {spec.key} | {spec.category} | {spec.scope} | {spec.requirement} | "
            f"{_inline(spec.contract_fields)} | {_inline(spec.reference_fields)} |"
        )
    lines += ["", "## 2. 独立时间语义要求", ""]
    lines.extend(f"- {item}" for item in TIME_SEMANTICS_REQUIREMENTS)
    lines += ["", "## 3. 候选包预检明细", ""]
    if not document.packages:
        lines.append(
            "- 本次预检**没有候选包**（未提供 / 目录为空）：结构不完整，fail-closed；"
            "请按契约把真实授权证据放入显式 `--inbox-dir` 后重跑。"
        )
    for package in document.packages:
        lines += [
            f"### {package.package_dir}"
            f"（scope={package.scope}；inbox_status={package.inbox_status}）",
            "",
            f"- fingerprint：`{package.fingerprint}`；synthetic="
            f"{str(package.synthetic).lower()}；preflight_pass="
            f"{str(package.preflight_pass).lower()}；evidence_qualified="
            f"{str(package.evidence_qualified).lower()}；gate_blocked="
            f"{str(package.gate_blocked).lower()}",
            "",
            "| 材料 | 状态 | 已具备字段 | 缺失字段 | 机械事实 | 人工下一步 |",
            "|---|---|---|---|---|---|",
        ]
        for item in package.materials:
            lines.append(
                f"| {item.key} | {item.status.value} | {_inline(item.present_fields)} | "
                f"{_inline(item.missing_fields)} | {item.machine_fact} | "
                f"{item.human_next_step} |"
            )
        lines.append("")
    lines += ["## 4. 口径与边界", ""]
    lines.extend(f"- {note}" for note in document.notes)
    lines.append("")
    return "\n".join(lines)

