"""证据就绪度（preflight / readiness）与一键资格复核（GOLD-006）。

职责边界（**重要**）：

- **只读 / 零网络 / 零写入**：本模块只聚合 GOLD-005 的只读台账与（可选）本地候选文件的
  dry-run 结论，不抓取站点、不写库、不改写任何历史事实；
- **复用口径，不另造阈值**：阈值全部来自 ``src.alpha.evidence_gate.py``
  （``MIN_AUTHOR_SAMPLES`` / ``MIN_NEWS_EVENTS`` / ``MIN_NEWS_HISTORY_DAYS`` /
  ``MAX_NEWS_SOURCE_SHARE``），本模块只**展示**当前值、阈值与 remaining gap；
- **诚实**：``blocker_active`` 恒为 True、``human_gate_required`` 恒为 True。
  就绪度 PASS **不等于** 解除 ``PHASE3_3_DATA``：授权法律效力、许可范围与历史可用时间
  证据必须由人工 Gate 核验，任何 Mock / 模板 / 代码完成都不能替代；
- **脱敏**：只输出白名单标量（计数、阈值、缺口、稳定原因码、已脱敏来源名、时间），
  **绝不**回显 token / API key / Authorization / 完整 source config；不读取
  ``sources.config_json``，也不读取记录正文。

量化口径：

- 台账侧（就绪度）：只统计经 ``evidence-intake-v1`` 入口认证、且具备独立历史可用证据
  （``available_at``）的记录；普通导入 / 历史 CSV / Mock / 模板示例一律不计入；
- 批次侧（preflight）：对本地候选文件做 dry-run，量化
  ``accepted`` / ``quarantined`` / ``duplicate`` / ``conflict`` / ``not_oos_eligible``。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope, ReasonCode
from src.evidence.intake import EvidenceIntakeReport
from src.evidence.ledger import EvidenceLedger, ScopeLedger
from src.monitoring.phase33_qualification import (
    EVIDENCE_INTAKE_NOTE,
    PHASE3_3_BLOCKER_CODE,
    CheckStatus,
)

__all__ = [
    "READINESS_NOTE",
    "READINESS_REPORT_TITLE",
    "READINESS_SCHEMA_VERSION",
    "BatchQuantification",
    "EvidenceReadinessReport",
    "ReadinessCheck",
    "ScopeReadiness",
    "build_readiness_report",
    "render_readiness_report",
    "summarize_batch",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
READINESS_SCHEMA_VERSION: Final[int] = 1
#: 报告标题（终端与落盘 Markdown 共用）
READINESS_REPORT_TITLE: Final[str] = "证据就绪度（Evidence Readiness）报告"
#: 固定说明：就绪度工具不具备解除 blocker 的能力
READINESS_NOTE: Final[str] = (
    "本报告只量化\"证据是否足够\"，不判断授权法律效力；就绪度 PASS **不解除** "
    f"{PHASE3_3_BLOCKER_CODE}，最终放行必须由人工 Gate 核验真实授权与历史可用证据。"
)

@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """一条机器可读的就绪度检查（当前值 / 阈值 / 比较方式 / 状态 / remaining gap）。"""

    key: str
    scope: str
    metric: str
    current: int | float
    required: int | float
    comparator: str
    status: CheckStatus
    evaluable: bool
    remaining: int | float
    reason: str
    evidence_start: str | None = None
    evidence_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "scope": self.scope,
            "metric": self.metric,
            "current": self.current,
            "required": self.required,
            "comparator": self.comparator,
            "status": self.status.value,
            "evaluable": self.evaluable,
            "remaining": self.remaining,
            "reason": self.reason,
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
        }


@dataclass(frozen=True, slots=True)
class ScopeReadiness:
    """单个 scope 的就绪度（Author = 可信 eligible 帖子数；News = 条数/覆盖/单源占比）。"""

    scope: str
    eligible_count: int
    certified_count: int
    not_oos_eligible_count: int
    coverage_days: int
    max_source_share: float
    source_counts: tuple[tuple[str, int], ...]
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(
            check.status is CheckStatus.PASS for check in self.checks
        )

    @property
    def remaining_gap_count(self) -> int:
        """仍处于 BLOCKED 的检查条数（0 表示该 scope 的量化门槛全部达标）。"""
        return sum(1 for check in self.checks if check.status is not CheckStatus.PASS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "eligible_count": self.eligible_count,
            "certified_count": self.certified_count,
            "not_oos_eligible_count": self.not_oos_eligible_count,
            "coverage_days": self.coverage_days,
            "max_source_share": self.max_source_share,
            "source_counts": [
                {"source": source, "count": count} for source, count in self.source_counts
            ],
            "ready": self.ready,
            "remaining_gap_count": self.remaining_gap_count,
            "checks": [check.to_dict() for check in self.checks],
        }

@dataclass(frozen=True, slots=True)
class BatchQuantification:
    """候选输入文件（dry-run）的量化结论（零写入）。"""

    scope: str
    input_path: str
    input_sha256: str
    rows: int
    accepted: int
    quarantined: int
    duplicate: int
    conflict: int
    not_oos_eligible: int
    oos_eligible: int
    reason_code_counts: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "input_path": self.input_path,
            "input_sha256": self.input_sha256,
            "rows": self.rows,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "duplicate": self.duplicate,
            "conflict": self.conflict,
            "not_oos_eligible": self.not_oos_eligible,
            "oos_eligible": self.oos_eligible,
            "reason_code_counts": [
                {"reason_code": code, "count": count} for code, count in self.reason_code_counts
            ],
        }


@dataclass(frozen=True, slots=True)
class EvidenceReadinessReport:
    """证据就绪度报告（只读；blocker 与人工 Gate 保持显式）。"""

    schema_version: int
    contract_version: str
    as_of: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    ready: bool
    scopes: tuple[ScopeReadiness, ...]
    batches: tuple[BatchQuantification, ...]
    notes: tuple[str, ...]

    def scope(self, scope: EvidenceScope | str) -> ScopeReadiness:
        name = scope.value if isinstance(scope, EvidenceScope) else str(scope)
        for item in self.scopes:
            if item.scope == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "as_of": self.as_of.isoformat(),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "ready": self.ready,
            "scopes": [item.to_dict() for item in self.scopes],
            "batches": [item.to_dict() for item in self.batches],
            "notes": list(self.notes),
        }

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _count_check(
    *,
    scope: str,
    key: str,
    metric: str,
    current: int,
    required: int,
    reason: str,
    evidence_start: str | None,
    evidence_end: str | None,
) -> ReadinessCheck:
    """``>=`` 类检查（数量 / 覆盖天数）：``remaining`` = 还差多少。"""
    return ReadinessCheck(
        key=key,
        scope=scope,
        metric=metric,
        current=current,
        required=required,
        comparator=">=",
        status=CheckStatus.PASS if current >= required else CheckStatus.BLOCKED,
        evaluable=True,
        remaining=max(0, required - current),
        reason=reason,
        evidence_start=evidence_start,
        evidence_end=evidence_end,
    )


def _share_check(
    *,
    scope: str,
    key: str,
    metric: str,
    current: float,
    required: float,
    evaluable: bool,
    reason: str,
    evidence_start: str | None,
    evidence_end: str | None,
) -> ReadinessCheck:
    """``<=`` 类检查（集中度）：``remaining`` = 还需要下降多少占比。

    无证据（``evaluable=False``）时**不得判 PASS**（与 ``news.max_source_share`` 的既有
    口径一致：\"无事件证据时不得判 PASS\"），此时 ``remaining`` 记为 0.0 并由 ``status`` /
    ``evaluable`` 表达真实状态。
    """
    return ReadinessCheck(
        key=key,
        scope=scope,
        metric=metric,
        current=current,
        required=required,
        comparator="<=",
        status=(
            CheckStatus.PASS
            if evaluable and current <= required
            else CheckStatus.BLOCKED
        ),
        evaluable=evaluable,
        remaining=round(max(0.0, current - required), 6) if evaluable else 0.0,
        reason=reason,
        evidence_start=evidence_start,
        evidence_end=evidence_end,
    )


def _ledger_reason(ledger: ScopeLedger) -> str:
    return (
        f"证据入口认证 {ledger.certified_records} 条，其中具备独立历史可用证据 "
        f"{ledger.oos_eligible_records} 条；仅认证但缺历史可用证据 "
        f"{ledger.not_oos_eligible_records} 条（NOT_OOS_ELIGIBLE）；"
        "普通导入 / 历史 CSV / Mock / 模板示例不计入。"
    )


def _author_readiness(ledger: ScopeLedger) -> ScopeReadiness:
    check = _count_check(
        scope=EvidenceScope.AUTHOR.value,
        key="author.evidence_intake_oos_eligible",
        metric="经证据入口认证且具备独立历史可用证据的 Author 可信帖子数",
        current=ledger.oos_eligible_records,
        required=MIN_AUTHOR_SAMPLES,
        reason=_ledger_reason(ledger),
        evidence_start=_iso(ledger.evidence_start),
        evidence_end=_iso(ledger.evidence_end),
    )
    return ScopeReadiness(
        scope=EvidenceScope.AUTHOR.value,
        eligible_count=ledger.oos_eligible_records,
        certified_count=ledger.certified_records,
        not_oos_eligible_count=ledger.not_oos_eligible_records,
        coverage_days=ledger.coverage_days,
        max_source_share=round(ledger.max_source_share, 6),
        source_counts=ledger.source_counts,
        checks=(check,),
    )


def _news_readiness(ledger: ScopeLedger) -> ScopeReadiness:
    has_evidence = ledger.oos_eligible_records > 0
    if ledger.source_counts:
        top_source, top_count = max(ledger.source_counts, key=lambda item: (item[1], item[0]))
        share_reason = (
            f"OOS eligible {ledger.oos_eligible_records} 条，最大来源 {top_source} 计 "
            f"{top_count} 条（占比 {ledger.max_source_share:.2%}）；单源集中度超过 "
            f"{MAX_NEWS_SOURCE_SHARE:.0%} 时即使总条数达标也不得放行。"
        )
    else:
        share_reason = "无 OOS eligible 记录：来源分布无证据（不得判 PASS）。"
    checks = (
        _count_check(
            scope=EvidenceScope.NEWS.value,
            key="news.evidence_intake_oos_eligible",
            metric="经证据入口认证且具备独立历史可用证据的 News 记录数",
            current=ledger.oos_eligible_records,
            required=MIN_NEWS_EVENTS,
            reason=_ledger_reason(ledger),
            evidence_start=_iso(ledger.evidence_start),
            evidence_end=_iso(ledger.evidence_end),
        ),
        _count_check(
            scope=EvidenceScope.NEWS.value,
            key="news.evidence_intake_coverage_days",
            metric="OOS eligible 记录的独立可用时间覆盖天数",
            current=ledger.coverage_days,
            required=MIN_NEWS_HISTORY_DAYS,
            reason=(
                "覆盖天数按独立历史可用证据（available_at）窗口计算："
                f"{_iso(ledger.evidence_start) or '—'} → {_iso(ledger.evidence_end) or '—'}；"
                "不能用今天采集的旧标题回填历史。"
            ),
            evidence_start=_iso(ledger.evidence_start),
            evidence_end=_iso(ledger.evidence_end),
        ),
        _share_check(
            scope=EvidenceScope.NEWS.value,
            key="news.evidence_intake_max_source_share",
            metric="单一来源在 OOS eligible 记录中的最大占比",
            current=round(ledger.max_source_share, 6),
            required=MAX_NEWS_SOURCE_SHARE,
            evaluable=has_evidence,
            reason=share_reason,
            evidence_start=_iso(ledger.evidence_start),
            evidence_end=_iso(ledger.evidence_end),
        ),
    )
    return ScopeReadiness(
        scope=EvidenceScope.NEWS.value,
        eligible_count=ledger.oos_eligible_records,
        certified_count=ledger.certified_records,
        not_oos_eligible_count=ledger.not_oos_eligible_records,
        coverage_days=ledger.coverage_days,
        max_source_share=round(ledger.max_source_share, 6),
        source_counts=ledger.source_counts,
        checks=checks,
    )

def summarize_batch(report: EvidenceIntakeReport) -> BatchQuantification:
    """把一次 dry-run 的导入报告量化成稳定的批次结论（零写入、无可辨识内容）。"""
    counts = report.counts
    conflict = sum(1 for row in report.rows if ReasonCode.IDENTITY_CONFLICT in row.reason_codes)
    tally: Counter[str] = Counter()
    for row in report.rows:
        for code in row.reason_codes:
            tally[code.value] += 1
    return BatchQuantification(
        scope=report.scope.value,
        input_path=report.input_file.path,
        input_sha256=report.input_file.sha256,
        rows=counts.rows,
        accepted=counts.accepted,
        quarantined=counts.quarantined,
        duplicate=counts.duplicate,
        conflict=conflict,
        not_oos_eligible=counts.not_oos_eligible,
        oos_eligible=counts.oos_eligible,
        reason_code_counts=tuple(sorted(tally.items())),
    )


def build_readiness_report(
    ledger: EvidenceLedger,
    *,
    as_of: datetime,
    batches: tuple[BatchQuantification, ...] = (),
    scopes: tuple[EvidenceScope, ...] = (EvidenceScope.AUTHOR, EvidenceScope.NEWS),
) -> EvidenceReadinessReport:
    """构造就绪度报告（纯函数；不触库、不联网、不写文件）。

    Args:
        ledger: 只读证据台账（``load_evidence_ledger`` 的结果）。
        as_of: 审计时点（必须带时区，保证可复现）。
        batches: 可选的候选批次量化结论（来自 dry-run preflight）。
        scopes: 需要输出的 scope（默认 Author + News 全量）。

    Raises:
        ValueError: ``as_of`` 未带时区。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    rows: list[ScopeReadiness] = []
    for scope in scopes:
        item = ledger.scope(scope)
        if scope is EvidenceScope.AUTHOR:
            rows.append(_author_readiness(item))
        else:
            rows.append(_news_readiness(item))
    ready = bool(rows) and all(row.ready for row in rows)
    notes = (
        READINESS_NOTE,
        EVIDENCE_INTAKE_NOTE,
        "默认只读 / 零网络：本报告不抓取站点、不写库、不修改任何历史事实。",
        "模板 / 示例行带显式 synthetic/example 标记，导入时判 SYNTHETIC_EVIDENCE 隔离，"
        "不会计入 qualification ledger。",
    )
    return EvidenceReadinessReport(
        schema_version=READINESS_SCHEMA_VERSION,
        contract_version=ledger.contract_version or EVIDENCE_CONTRACT_VERSION,
        as_of=as_of.astimezone(UTC),
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=True,  # 就绪度工具不具备解除 blocker 的能力
        human_gate_required=True,
        ready=ready,
        scopes=tuple(rows),
        batches=batches,
        notes=notes,
    )

def render_readiness_report(report: EvidenceReadinessReport) -> str:
    """渲染人类可读 Markdown（**不解除** blocker）。"""
    lines: list[str] = [
        f"## {READINESS_REPORT_TITLE}",
        "",
        f"> blocker `{report.blocker_code}`：active={str(report.blocker_active).lower()}；"
        f"human_gate_required={str(report.human_gate_required).lower()}；"
        "就绪度 PASS 不等于 Alpha 放行。",
        "",
        f"- 契约版本：`{report.contract_version}`",
        f"- 审计时点（UTC）：{report.as_of.isoformat()}",
        f"- 量化门槛是否全部达标（不代表可解除 blocker）：{str(report.ready).lower()}",
        "",
    ]
    for item in report.scopes:
        lines += [
            f"### {item.scope}",
            "",
            f"- eligible_count：{item.eligible_count}；certified：{item.certified_count}；"
            f"not_oos_eligible：{item.not_oos_eligible_count}",
            f"- coverage_days：{item.coverage_days}；max_source_share："
            f"{item.max_source_share:.4f}",
            f"- 仍 BLOCKED 的检查条数：{item.remaining_gap_count}",
            "",
            "| 检查 | 指标 | 当前 | 阈值 | 比较 | 状态 | 可评估 | 缺口 | 证据范围 |",
            "|---|---|---:|---:|---|---|---|---:|---|",
        ]
        for check in item.checks:
            lines.append(
                f"| {check.key} | {check.metric} | {check.current} | {check.required} | "
                f"{check.comparator} | {check.status.value} | "
                f"{str(check.evaluable).lower()} | {check.remaining} | "
                f"{check.evidence_start or '—'} → {check.evidence_end or '—'} |"
            )
        lines.append("")
    if report.batches:
        lines += [
            "### 候选批次（dry-run，零写入）",
            "",
            "| scope | 输入 | 行数 | accepted | quarantined | duplicate | conflict | "
            "not_oos_eligible |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for batch in report.batches:
            lines.append(
                f"| {batch.scope} | {batch.input_path} | {batch.rows} | {batch.accepted} | "
                f"{batch.quarantined} | {batch.duplicate} | {batch.conflict} | "
                f"{batch.not_oos_eligible} |"
            )
        lines.append("")
    lines += ["### 口径与边界", ""]
    lines.extend(f"- {note}" for note in report.notes)
    lines.append("")
    return "\n".join(lines)
