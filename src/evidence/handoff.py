"""Phase 3.3 Evidence 人工交接包（GOLD-008，只读 / 默认 dry-run）。

把 GOLD-007 的 Evidence Operator 输出整理为**确定性、可复核的人工交接包**，交给业务方 /
operator 去补齐真实授权证据：

- **复用唯一资格口径**：本模块只消费
  :class:`src.monitoring.evidence_readiness.EvidenceReadinessReport`
  （其阈值全部来自 ``src.alpha.evidence_gate``），**绝不新造第二套阈值算法**；
- **机器可读 + 人类可读**：稳定 JSON 与 Markdown 摘要同时给出 Author / News 的
  ``eligible`` / ``required`` / ``remaining``、coverage gap、source-share 可评估性、
  主要 quarantine / reason-code 计数；
- **人工证据 checklist**：区分 authorization / provenance / published_at / collected_at /
  availability-OOS / identity 六类，并**醒目标记**模板 / Mock / 示例**不计资格**；
- **默认只读 / 零网络 / 零写入**：本模块自身不做任何 I/O 与网络访问；CLI 默认只打印
  stdout，任何写文件都必须显式给出 ``--out``；
- **不解除 blocker**：``blocker_active`` / ``human_gate_required`` 恒为 ``True``，
  ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 ``False``；
  量化门槛达标时最多 ``ready_for_human_review=True``，Phase 切换仍需
  ``.ai/DEVELOPMENT_PROTOCOL.md`` 的 **L3 人工确认**；
- **脱敏**：只输出白名单标量（计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名 / 时间），
  **绝不**输出正文、token / API key / Authorization 或完整 source config。

本工具**只减少人工交接摩擦**，不改变 ``PHASE3_3_DATA`` 的解除条件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.redaction import safe_text
from src.evidence.contracts import ALLOWED_AUTHORIZATION_BASES, EvidenceScope, ReasonCode
from src.monitoring.evidence_readiness import (
    BatchQuantification,
    EvidenceReadinessReport,
    ReadinessCheck,
    ScopeReadiness,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "BLOCKED_STATUS",
    "CHECK_CATEGORIES",
    "HANDOFF_NOTE",
    "HANDOFF_REPORT_NAME",
    "HANDOFF_SCHEMA_VERSION",
    "PENDING_HUMAN_REVIEW_STATUS",
    "ChecklistItem",
    "EvidenceHandoffReport",
    "ExcludedEvidence",
    "ScopeGap",
    "build_evidence_checklist",
    "build_excluded_evidence",
    "build_handoff_report",
    "render_handoff_markdown",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
HANDOFF_SCHEMA_VERSION: Final[int] = 1
#: 报告标识（稳定，供上游解析）
HANDOFF_REPORT_NAME: Final[str] = "evidence_handoff_report"
#: 未达标时的诚实状态（**不解除** ``PHASE3_3_DATA``）
BLOCKED_STATUS: Final[str] = "BLOCKED"
#: 量化门槛达标、但人工 Gate 尚未完成时的状态（仍属 blocked）
PENDING_HUMAN_REVIEW_STATUS: Final[str] = "BLOCKED_PENDING_HUMAN_REVIEW"
#: 人工证据 checklist 的固定类别顺序（稳定；CLI / Markdown / 测试共同引用）
CHECK_CATEGORIES: Final[tuple[str, ...]] = (
    "authorization",
    "provenance",
    "published_at",
    "collected_at",
    "availability",
    "identity",
)
#: 固定说明：交接包不具备解除 blocker / 切换 Phase 的能力
HANDOFF_NOTE: Final[str] = (
    "本交接包只把当前资格状态与人工证据口径交给 operator，**不解除** "
    f"{PHASE3_3_BLOCKER_CODE}：`blocker_active` / `human_gate_required` 恒为 true，"
    "`data_qualification_passed` / `phase_transition_allowed` 恒为 false。"
    "即使量化门槛达标（`ready_for_human_review=true`），Phase 切换仍须按 "
    ".ai/DEVELOPMENT_PROTOCOL.md 的 L3 人工确认。"
)

#: 与 ``src.monitoring.evidence_readiness`` 共享的检查键（漂移即测试失败，绝不静默）
_AUTHOR_ELIGIBLE_CHECK: Final[str] = "author.evidence_intake_oos_eligible"
_NEWS_ELIGIBLE_CHECK: Final[str] = "news.evidence_intake_oos_eligible"
_NEWS_COVERAGE_CHECK: Final[str] = "news.evidence_intake_coverage_days"
_NEWS_SHARE_CHECK: Final[str] = "news.evidence_intake_max_source_share"


def _check_dict(check: ReadinessCheck) -> dict[str, Any]:
    """把就绪度检查序列化为稳定 JSON（原因文本再过一次脱敏）。"""
    data = check.to_dict()
    data["reason"] = safe_text(str(data.get("reason") or ""), max_chars=300)
    return data


def _find_check(item: ScopeReadiness, key: str) -> ReadinessCheck:
    """按检查键取就绪度检查；缺失即显式失败（防止口径漂移被静默忽略）。"""
    for check in item.checks:
        if check.key == key:
            return check
    raise ValueError(f"就绪度报告缺少检查键：{key}")


def _scope_status(item: ScopeReadiness) -> str:
    return "PASS" if item.ready else "BLOCKED"


@dataclass(frozen=True, slots=True)
class ScopeGap:
    """单个 scope 的可量化缺口（Author = 可信帖子数；News = 条数 / 覆盖 / 单源占比）。"""

    scope: str
    status: str
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
    max_source_share: float
    source_share_limit: float
    source_share_evaluable: bool
    source_share_remaining: float
    remaining_checks: int
    checks: tuple[ReadinessCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "status": self.status,
            "eligible": self.eligible,
            "required": self.required,
            "remaining": self.remaining,
            "certified": self.certified,
            "not_oos_eligible": self.not_oos_eligible,
            "coverage_applicable": self.coverage_applicable,
            "coverage_days": self.coverage_days,
            "coverage_required": self.coverage_required,
            "coverage_remaining": self.coverage_remaining,
            "source_share_applicable": self.source_share_applicable,
            "max_source_share": self.max_source_share,
            "source_share_limit": self.source_share_limit,
            "source_share_evaluable": self.source_share_evaluable,
            "source_share_remaining": self.source_share_remaining,
            "remaining_checks": self.remaining_checks,
            "checks": [_check_dict(check) for check in self.checks],
        }


def _scope_gap(item: ScopeReadiness) -> ScopeGap:
    """把单个 scope 的就绪度重排为交接包需要的缺口视图（纯函数，零 I/O）。"""
    if item.scope == EvidenceScope.AUTHOR.value:
        eligible = _find_check(item, _AUTHOR_ELIGIBLE_CHECK)
        return ScopeGap(
            scope=item.scope,
            status=_scope_status(item),
            eligible=item.eligible_count,
            required=int(eligible.required),
            remaining=int(eligible.remaining),
            certified=item.certified_count,
            not_oos_eligible=item.not_oos_eligible_count,
            coverage_applicable=False,
            coverage_days=0,
            coverage_required=0,
            coverage_remaining=0,
            source_share_applicable=False,
            max_source_share=0.0,
            source_share_limit=0.0,
            source_share_evaluable=False,
            source_share_remaining=0.0,
            remaining_checks=item.remaining_gap_count,
            checks=item.checks,
        )
    eligible = _find_check(item, _NEWS_ELIGIBLE_CHECK)
    coverage = _find_check(item, _NEWS_COVERAGE_CHECK)
    share = _find_check(item, _NEWS_SHARE_CHECK)
    return ScopeGap(
        scope=item.scope,
        status=_scope_status(item),
        eligible=item.eligible_count,
        required=int(eligible.required),
        remaining=int(eligible.remaining),
        certified=item.certified_count,
        not_oos_eligible=item.not_oos_eligible_count,
        coverage_applicable=True,
        coverage_days=item.coverage_days,
        coverage_required=int(coverage.required),
        coverage_remaining=int(coverage.remaining),
        source_share_applicable=True,
        max_source_share=round(float(item.max_source_share), 6),
        source_share_limit=float(share.required),
        source_share_evaluable=bool(share.evaluable),
        source_share_remaining=round(float(share.remaining), 6),
        remaining_checks=item.remaining_gap_count,
        checks=item.checks,
    )


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """一条人工证据 checklist 项（业务方需要提供 / 复核的具体字段）。"""

    key: str
    category: str
    scope: str
    requirement: str
    contract_fields: tuple[str, ...]
    machine_check: str
    human_review_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "scope": self.scope,
            "requirement": self.requirement,
            "contract_fields": list(self.contract_fields),
            "machine_check": self.machine_check,
            "human_review_required": self.human_review_required,
        }


@dataclass(frozen=True, slots=True)
class ExcludedEvidence:
    """**明确不计资格**的证据形态（模板 / Mock / 历史 CSV / 缺独立可用时间）。"""

    kind: str
    reason_code: str
    counts_toward_eligibility: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "counts_toward_eligibility": self.counts_toward_eligibility,
            "detail": self.detail,
        }


def build_evidence_checklist() -> tuple[ChecklistItem, ...]:
    """返回人工证据 checklist（固定顺序；区分六类证据口径，纯函数、零 I/O）。

    说明：``machine_check`` 只描述证据入口能机械验证的部分；**任何一项都不能替代人工
    对授权法律效力与历史可用性的核验**，因此 ``human_review_required`` 恒为 ``True``。
    """
    bases = "、".join(sorted(ALLOWED_AUTHORIZATION_BASES))
    return (
        ChecklistItem(
            key="authorization.status",
            category="authorization",
            scope="both",
            requirement=(
                "authorization_status 必须显式为 APPROVED（缺失 / UNKNOWN / DENIED 一律隔离）"
            ),
            contract_fields=("authorization_status",),
            machine_check="证据入口逐行校验取值，非 APPROVED 判 AUTHORIZATION_MISSING 隔离",
            human_review_required=True,
        ),
        ChecklistItem(
            key="authorization.basis",
            category="authorization",
            scope="both",
            requirement=f"authorization_basis 必须是白名单依据之一（{bases}）",
            contract_fields=("authorization_basis",),
            machine_check="证据入口按白名单校验，非白名单判 AUTHORIZATION_MISSING 隔离",
            human_review_required=True,
        ),
        ChecklistItem(
            key="authorization.reference",
            category="authorization",
            scope="both",
            requirement=(
                "提供可复核的授权引用：https 条款 URL 或 docs/legal/ 内**现存**许可文件路径，"
                "且引用内容真实覆盖本次数据用途"
            ),
            contract_fields=("authorization_reference",),
            machine_check="证据入口校验引用形态与文件存在性（不判断法律效力）",
            human_review_required=True,
        ),
        ChecklistItem(
            key="authorization.reviewer",
            category="authorization",
            scope="both",
            requirement=(
                "authorization_reviewed_by 必须是**实际核验授权的人**（不得填模型名），"
                "authorization_reviewed_at 必须带时区且不得为未来时间"
            ),
            contract_fields=("authorization_reviewed_by", "authorization_reviewed_at"),
            machine_check="证据入口校验字段齐全与非未来时间（不判断签认人是否真实）",
            human_review_required=True,
        ),
        ChecklistItem(
            key="authorization.window",
            category="authorization",
            scope="both",
            requirement=(
                "collected_at 必须落在 [authorization_valid_from, authorization_expires_at] "
                "授权窗口内"
            ),
            contract_fields=("authorization_valid_from", "authorization_expires_at"),
            machine_check="证据入口在提供窗口时校验 collected_at 的区间包含关系",
            human_review_required=True,
        ),
        ChecklistItem(
            key="authorization.permits",
            category="authorization",
            scope="both",
            requirement=(
                "permits_automated_collection / permits_local_storage / permits_research_use "
                "三项必须均为 true（缺任一即为后续模型用途未获授权）"
            ),
            contract_fields=(
                "permits_automated_collection",
                "permits_local_storage",
                "permits_research_use",
            ),
            machine_check="证据入口对三项用途许可做布尔校验，缺项判 AUTHORIZATION_MISSING 隔离",
            human_review_required=True,
        ),
        ChecklistItem(
            key="provenance.reference",
            category="provenance",
            scope="both",
            requirement="provenance_reference 必须是 https URL 或 docs/legal/ 内相对路径",
            contract_fields=("provenance_reference",),
            machine_check="证据入口校验引用形态，不合法判 PROVENANCE_MISSING 隔离",
            human_review_required=True,
        ),
        ChecklistItem(
            key="provenance.url",
            category="provenance",
            scope="both",
            requirement="url 指向原始页面 / 导出文件的稳定地址（可用于人工回查）",
            contract_fields=("url",),
            machine_check="证据入口保留脱敏后的 URL 形态（去掉查询串与 userinfo）",
            human_review_required=True,
        ),
        ChecklistItem(
            key="published_at.value",
            category="published_at",
            scope="both",
            requirement=(
                "published_at 必须是**原始发布时间**：合法 ISO8601 且带时区，不得为未来时间，"
                "也不得用采集当天时间回填历史标题"
            ),
            contract_fields=("published_at",),
            machine_check="证据入口校验 ISO8601 + 时区 + 非未来；非法判 PUBLISHED_AT_INVALID 隔离",
            human_review_required=True,
        ),
        ChecklistItem(
            key="collected_at.value",
            category="collected_at",
            scope="both",
            requirement=(
                "collected_at 必须是**独立采集 / 导出的时间**（带时区、不早于 published_at、"
                "不晚于审计时点），且能追溯到导出记录"
            ),
            contract_fields=("collected_at",),
            machine_check=(
                "证据入口校验 ISO8601 + 时区 + 与 published_at 的先后；"
                "非法判 COLLECTED_AT_INVALID 隔离"
            ),
            human_review_required=True,
        ),
        ChecklistItem(
            key="availability.available_at",
            category="availability",
            scope="both",
            requirement=(
                "available_at 是**独立历史可用证据**：带时区且落在 [published_at, collected_at] "
                "区间；只有它才能证明该记录在历史时点即可用（缺失即 NOT_OOS_ELIGIBLE）"
            ),
            contract_fields=("available_at",),
            machine_check=(
                "证据入口校验区间与可解析性；缺失 / 非法判 AVAILABILITY_UNPROVEN（不计 OOS）"
            ),
            human_review_required=True,
        ),
        ChecklistItem(
            key="availability.provenance",
            category="availability",
            scope="both",
            requirement=(
                "availability_provenance 说明证据形式（如授权方导出 / 归档接口），"
                "availability_reference 提供可复核的 https 引用或 docs/legal/ 路径"
            ),
            contract_fields=("availability_provenance", "availability_reference"),
            machine_check="证据入口校验形式字段与引用形态；缺失判 AVAILABILITY_UNPROVEN",
            human_review_required=True,
        ),
        ChecklistItem(
            key="identity.record",
            category="identity",
            scope="both",
            requirement=(
                "source + source_record_id 构成稳定身份；同一身份内容不一致不得覆盖历史"
                "（判 IDENTITY_CONFLICT）"
            ),
            contract_fields=("source", "source_record_id"),
            machine_check="证据入口做身份唯一性与内容冲突判定，冲突行隔离且不 UPDATE",
            human_review_required=True,
        ),
        ChecklistItem(
            key="identity.author",
            category="identity",
            scope="author",
            requirement=(
                "Author 证据必须提供 author_name 与 external_account_id（平台账号稳定 ID，"
                "不得填昵称副本）；账号不得已归属其他作者"
            ),
            contract_fields=("author_name", "external_account_id"),
            machine_check="证据入口校验作者身份字段齐全；作者归属链跳过身份冲突且不覆盖",
            human_review_required=True,
        ),
    )


def build_excluded_evidence() -> tuple[ExcludedEvidence, ...]:
    """返回**明确不计资格**的证据形态（醒目标记，防止把模板 / Mock 当真实证据）。"""
    return (
        ExcludedEvidence(
            kind="template_or_example",
            reason_code=ReasonCode.SYNTHETIC_EVIDENCE.value,
            counts_toward_eligibility=False,
            detail=(
                "模板 / 示例行带 `record_kind=example` / `is_mock=true`，导入即判 "
                "SYNTHETIC_EVIDENCE 隔离，**不写库、不计入** qualification ledger。"
            ),
        ),
        ExcludedEvidence(
            kind="mock_or_synthetic_corpus",
            reason_code=ReasonCode.SYNTHETIC_EVIDENCE.value,
            counts_toward_eligibility=False,
            detail="Mock / 演练语料（`is_mock=true`）只用于跑通流程，**不得**用于达标。",
        ),
        ExcludedEvidence(
            kind="historical_plain_csv",
            reason_code="NOT_CERTIFIED",
            counts_toward_eligibility=False,
            detail=(
                "scripts/import_real_posts.py 的普通 CSV 载荷没有 `evidence-intake-v1` 证据块，"
                "既不计入 qualification ledger，也无法进入 gateway-only 作者链。"
            ),
        ),
        ExcludedEvidence(
            kind="certified_without_available_at",
            reason_code=ReasonCode.AVAILABILITY_UNPROVEN.value,
            counts_toward_eligibility=False,
            detail=(
                "经入口认证但缺独立 `available_at` 证据的记录只算 certified，"
                "**不计入** OOS eligible（NOT_OOS_ELIGIBLE）。"
            ),
        ),
    )


def thresholds() -> dict[str, float]:
    """回显既有资格门槛（**唯一来源** ``src.alpha.evidence_gate``，绝不复制第二套）。"""
    return {
        "author_min_eligible_records": MIN_AUTHOR_SAMPLES,
        "news_min_eligible_records": MIN_NEWS_EVENTS,
        "news_min_history_days": MIN_NEWS_HISTORY_DAYS,
        "news_max_source_share": MAX_NEWS_SOURCE_SHARE,
    }


@dataclass(frozen=True, slots=True)
class EvidenceHandoffReport:
    """Phase 3.3 Evidence 人工交接包（稳定 JSON + 可读 Markdown 的同一事实来源）。"""

    schema_version: int
    report: str
    contract_version: str
    as_of: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    status: str
    quantified_thresholds_met: bool
    ready_for_human_review: bool
    data_qualification_passed: bool
    phase_transition_allowed: bool
    author: ScopeGap
    news: ScopeGap
    total_remaining_gap_count: int
    checklist: tuple[ChecklistItem, ...]
    excluded_evidence: tuple[ExcludedEvidence, ...]
    quarantine_reason_counts: tuple[tuple[str, int], ...]
    batch: BatchQuantification | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report": self.report,
            "contract_version": self.contract_version,
            "as_of": self.as_of.isoformat(),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "status": self.status,
            "quantified_thresholds_met": self.quantified_thresholds_met,
            "ready_for_human_review": self.ready_for_human_review,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "thresholds": thresholds(),
            "author": self.author.to_dict(),
            "news": self.news.to_dict(),
            "total_remaining_gap_count": self.total_remaining_gap_count,
            "checklist": [item.to_dict() for item in self.checklist],
            "excluded_evidence": [item.to_dict() for item in self.excluded_evidence],
            "quarantine_reason_counts": [
                {"reason_code": code, "count": count}
                for code, count in self.quarantine_reason_counts
            ],
            "batch": self.batch.to_dict() if self.batch is not None else None,
            "notes": list(self.notes),
        }


def build_handoff_report(
    readiness: EvidenceReadinessReport,
    *,
    batch: BatchQuantification | None = None,
    quarantine_reason_counts: tuple[tuple[str, int], ...] | None = None,
) -> EvidenceHandoffReport:
    """把只读就绪度报告整理为人工交接包（纯函数；不触库、不联网、不写文件）。

    Args:
        readiness: :func:`src.monitoring.evidence_readiness.build_readiness_report` 的结果
            （阈值已由 ``src.alpha.evidence_gate`` 决定）。
        batch: 可选的候选文件 dry-run 量化结论（来自 preflight，零写入）。
        quarantine_reason_counts: 可选的隔离原因码计数；缺省时取 ``batch.reason_code_counts``。

    Returns:
        机器可读的 :class:`EvidenceHandoffReport`；``data_qualification_passed`` /
        ``phase_transition_allowed`` 恒为 ``False``，量化达标时最多
        ``ready_for_human_review=True``。
    """
    met = bool(readiness.ready)
    if quarantine_reason_counts is None:
        counts = batch.reason_code_counts if batch is not None else ()
    else:
        counts = quarantine_reason_counts
    stable_counts = tuple(sorted((str(code), int(count)) for code, count in counts))
    author = _scope_gap(readiness.scope(EvidenceScope.AUTHOR))
    news = _scope_gap(readiness.scope(EvidenceScope.NEWS))
    notes = (
        HANDOFF_NOTE,
        "Author / News 的 eligible / required / remaining 与 coverage / source-share 口径"
        "全部复用现有 Evidence Readiness 报告（同源 src.alpha.evidence_gate 阈值），"
        "本交接包不修改任何阈值、授权 / 时间 / availability 规则。",
        "无候选输入（--input）时 quarantine_reason_counts 为空；"
        "请用 --input 传入候选文件做 dry-run 获取隔离原因计数（零写入）。",
        "本报告不含正文、token / API key / Authorization 或完整 source config。",
    )
    return EvidenceHandoffReport(
        schema_version=HANDOFF_SCHEMA_VERSION,
        report=HANDOFF_REPORT_NAME,
        contract_version=readiness.contract_version,
        as_of=readiness.as_of,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=True,
        human_gate_required=True,
        status=PENDING_HUMAN_REVIEW_STATUS if met else BLOCKED_STATUS,
        quantified_thresholds_met=met,
        ready_for_human_review=met,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        author=author,
        news=news,
        total_remaining_gap_count=author.remaining_checks + news.remaining_checks,
        checklist=build_evidence_checklist(),
        excluded_evidence=build_excluded_evidence(),
        quarantine_reason_counts=stable_counts,
        batch=batch,
        notes=notes,
    )


def _gap_rows(gap: ScopeGap) -> list[str]:
    """渲染单个 scope 的检查表（当前值 / 阈值 / 状态 / 缺口 / 证据范围）。"""
    lines = [
        f"### {gap.scope}",
        "",
        f"- eligible：{gap.eligible}；required：{gap.required}；remaining：{gap.remaining}；"
        f"certified：{gap.certified}；not_oos_eligible：{gap.not_oos_eligible}",
        f"- status：`{gap.status}`；仍未达标的检查条数：{gap.remaining_checks}",
        "",
        "| 检查 | 指标 | 当前 | 阈值 | 比较 | 状态 | 可评估 | 缺口 | 证据范围 |",
        "|---|---|---:|---:|---|---|---|---:|---|",
    ]
    for check in gap.checks:
        lines.append(
            f"| {check.key} | {check.metric} | {check.current} | {check.required} | "
            f"{check.comparator} | {check.status.value} | {str(check.evaluable).lower()} | "
            f"{check.remaining} | {check.evidence_start or '—'} → {check.evidence_end or '—'} |"
        )
    if gap.coverage_applicable:
        lines.append(
            f"| coverage | OOS eligible 覆盖天数 | {gap.coverage_days} | "
            f"{gap.coverage_required} | >= | — | true | {gap.coverage_remaining} | — |"
        )
    if gap.source_share_applicable:
        lines.append(
            f"| source-share | 单一来源最大占比 | {gap.max_source_share} | "
            f"{gap.source_share_limit} | <= | — | "
            f"{str(gap.source_share_evaluable).lower()} | {gap.source_share_remaining} | — |"
        )
    lines.append("")
    lines.append("- 检查说明（文本已脱敏）：")
    lines.extend(
        f"  - `{check.key}`：{safe_text(check.reason, max_chars=300)}" for check in gap.checks
    )
    lines.append("")
    return lines


def render_handoff_markdown(report: EvidenceHandoffReport) -> str:
    """渲染人类可读的交接摘要（脱敏；**不解除** blocker）。"""
    lines: list[str] = [
        "# Evidence 人工交接包（Evidence Handoff Report）",
        "",
        f"> blocker `{report.blocker_code}`：active={str(report.blocker_active).lower()}；"
        f"human_gate_required={str(report.human_gate_required).lower()}；"
        "本报告只减少人工交接摩擦，**不解除** blocker，也不切换 Phase。",
        "",
        "## 1. 状态",
        "",
        f"- status：`{report.status}`",
        f"- quantified_thresholds_met：{str(report.quantified_thresholds_met).lower()}；"
        f"ready_for_human_review：{str(report.ready_for_human_review).lower()}",
        f"- data_qualification_passed：{str(report.data_qualification_passed).lower()}；"
        f"phase_transition_allowed：{str(report.phase_transition_allowed).lower()}",
        f"- 契约版本：`{report.contract_version}`；审计时点（UTC）：{report.as_of.isoformat()}",
        f"- 全部未达标检查条数：{report.total_remaining_gap_count}",
        "",
        f"- 门槛（来自 src.alpha.evidence_gate）：{thresholds()}",
        "",
        "## 2. 缺口明细",
        "",
    ]
    lines += _gap_rows(report.author)
    lines += _gap_rows(report.news)
    lines += [
        "## 3. 隔离 / 原因码计数",
        "",
        "| 原因码 | 计数 |",
        "|---|---:|",
    ]
    if not report.quarantine_reason_counts:
        lines.append("| — | 0 |")
    for code, count in report.quarantine_reason_counts:
        lines.append(f"| {code} | {count} |")
    lines += ["", "## 4. 人工证据 checklist", ""]
    for category in CHECK_CATEGORIES:
        items = [item for item in report.checklist if item.category == category]
        if not items:
            continue
        lines += [
            f"### {category}",
            "",
            "| 项 | 适用 | 要求 | 契约字段 | 机械校验 | 需人工复核 |",
            "|---|---|---|---|---|---|",
        ]
        for item in items:
            fields = ", ".join(f"`{name}`" for name in item.contract_fields) or "—"
            lines.append(
                f"| {item.key} | {item.scope} | {item.requirement} | {fields} | "
                f"{item.machine_check} | {str(item.human_review_required).lower()} |"
            )
        lines.append("")
    lines += [
        "## 5. 明确不计资格的证据（**不得**用于达标）",
        "",
        "| 形态 | 原因码 | 计入资格 | 说明 |",
        "|---|---|---|---|",
    ]
    for excluded in report.excluded_evidence:
        lines.append(
            f"| {excluded.kind} | `{excluded.reason_code}` | "
            f"{str(excluded.counts_toward_eligibility).lower()} | {excluded.detail} |"
        )
    lines += ["", "## 6. 口径与边界", ""]
    lines.extend(f"- {note}" for note in report.notes)
    lines.append("")
    return "\n".join(lines)


