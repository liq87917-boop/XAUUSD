"""授权 Evidence Operator 工作流（GOLD-007）：把模板 / preflight / 隔离摘要 /
显式 intake / 资格复核串成**单入口、默认安全**的 operator workflow。

职责边界（**重要**）：

- **复用，不另造规则**：本模块不新造任何阈值或资格规则，只消费
  :mod:`src.evidence.validation`（逐行机械校验）与 :mod:`src.evidence.intake`
  （dry-run-first 导入引擎）产出的 ``EvidenceIntakeReport``；阈值与 blocker 口径仍由
  ``src.alpha.evidence_gate`` / ``src.monitoring.phase33_qualification`` 决定；
- **写入前二次验证**：显式写入（``dry_run=False``）前必须先跑一次 dry-run，
  再用 :func:`evaluate_write_gate` 汇总"哪些行绝不能落库"
  （未授权 / 合成示例 / 时间非法 / 身份冲突 / 凭据 / 坏行），
  只有 ``ACCEPTED`` 行才会被 append-only 写入；隔离行永远不进入 qualification ledger；
- **默认零网络 / 零写入**：本模块不做任何 I/O 与网络访问，全部输入由调用方提供；
- **脱敏**：只输出白名单标量（计数 / 稳定原因码 / 已脱敏来源名 / 指纹前缀 / 时间），
  **绝不**输出正文、token / API key / Authorization 或完整 source config。

单入口步骤名（稳定，供 CLI 与测试引用）：

```text
template → preflight → quarantine → intake → recheck
```
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

from src.common.redaction import safe_text
from src.evidence.contracts import ReasonCode, RowStatus
from src.evidence.intake import EvidenceIntakeReport, RowOutcome

__all__ = [
    "OPERATOR_STEPS",
    "OPERATOR_WORKFLOW_SCHEMA_VERSION",
    "WRITE_GATE_NOTE",
    "QuarantineEntry",
    "QuarantineSummary",
    "WriteGateDecision",
    "build_quarantine_summary",
    "evaluate_write_gate",
    "render_quarantine_summary",
    "render_write_gate",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
OPERATOR_WORKFLOW_SCHEMA_VERSION: Final[int] = 1
#: 单入口 operator workflow 的步骤顺序（稳定；CLI 与测试共同引用）
OPERATOR_STEPS: Final[tuple[str, ...]] = (
    "template",
    "preflight",
    "quarantine",
    "intake",
    "recheck",
)
#: 写入门禁的固定说明（每次输出都带上，防止"工具 PASS 即解除 blocker"的误读）
WRITE_GATE_NOTE: Final[str] = (
    "写入门禁只允许 `ACCEPTED` 行进入 append-only 落库；未授权 / 合成示例 / 时间非法 / "
    "身份冲突 / 凭据 / 坏行一律隔离，绝不进入 qualification ledger，也绝不覆盖历史事实。"
    "本门禁**不解除** PHASE3_3_DATA：授权法律效力与历史可用证据仍需人工核验。"
)

#: 原因码分组（只用于分类计数与展示；判定仍由 :mod:`src.evidence.validation` 完成）
_SYNTHETIC_CODES: Final[frozenset[ReasonCode]] = frozenset({ReasonCode.SYNTHETIC_EVIDENCE})
_AUTHORIZATION_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.AUTHORIZATION_MISSING, ReasonCode.AUTHORIZATION_REFERENCE_INVALID}
)
_TIME_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.PUBLISHED_AT_INVALID, ReasonCode.COLLECTED_AT_INVALID}
)
_CONTENT_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.CONTENT_EMPTY, ReasonCode.ROW_UNREADABLE}
)
_IDENTITY_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.SOURCE_IDENTITY_MISSING,
        ReasonCode.SOURCE_RECORD_ID_MISSING,
        ReasonCode.IDENTITY_CONFLICT,
        ReasonCode.SCOPE_MISMATCH,
    }
)
_SENSITIVE_CODES: Final[frozenset[ReasonCode]] = frozenset({ReasonCode.SENSITIVE_VALUE_DETECTED})
_PROVENANCE_CODES: Final[frozenset[ReasonCode]] = frozenset({ReasonCode.PROVENANCE_MISSING})
_AVAILABILITY_CODES: Final[frozenset[ReasonCode]] = frozenset({ReasonCode.AVAILABILITY_UNPROVEN})


def _safe(value: str, *, max_chars: int = 200) -> str:
    """短字段脱敏（擦除凭据 + 截断）；展示层统一入口。"""
    return safe_text(value, max_chars=max_chars)


def _fingerprint_prefix(value: str | None) -> str | None:
    """指纹只保留前 16 位（足够人工核对，且不承载正文信息）。"""
    if not value:
        return None
    return value[:16]


def _count_codes(rows: tuple[RowOutcome, ...], codes: frozenset[ReasonCode]) -> int:
    """统计**命中任意给定原因码**的行数（同一行只算一次）。"""
    return sum(1 for row in rows if any(code in codes for code in row.reason_codes))


def _reason_tally(rows: tuple[RowOutcome, ...]) -> tuple[tuple[str, int], ...]:
    """稳定排序的原因码计数（机器可读）。"""
    tally: Counter[str] = Counter()
    for row in rows:
        for code in row.reason_codes:
            tally[code.value] += 1
    return tuple(sorted(tally.items()))


@dataclass(frozen=True, slots=True)
class WriteGateDecision:
    """显式写入前的二次验证结论（来自一次 dry-run ``EvidenceIntakeReport``）。

    ``write_allowed`` 只表示"本次批次至少有一行可 append-only 写入"，
    **不表示**授权已通过人工核验、也不解除 ``PHASE3_3_DATA``。
    """

    scope: str
    dry_run_source: bool
    rows: int
    accepted: int
    quarantined: int
    duplicate: int
    conflict: int
    not_oos_eligible: int
    oos_eligible: int
    synthetic: int
    authorization_incomplete: int
    time_invalid: int
    content_invalid: int
    identity_issues: int
    sensitive_detected: int
    provenance_missing: int
    availability_unproven: int
    write_allowed: bool
    reason_code_counts: tuple[tuple[str, int], ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATOR_WORKFLOW_SCHEMA_VERSION,
            "scope": self.scope,
            "dry_run_source": self.dry_run_source,
            "rows": self.rows,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "duplicate": self.duplicate,
            "conflict": self.conflict,
            "not_oos_eligible": self.not_oos_eligible,
            "oos_eligible": self.oos_eligible,
            "synthetic": self.synthetic,
            "authorization_incomplete": self.authorization_incomplete,
            "time_invalid": self.time_invalid,
            "content_invalid": self.content_invalid,
            "identity_issues": self.identity_issues,
            "sensitive_detected": self.sensitive_detected,
            "provenance_missing": self.provenance_missing,
            "availability_unproven": self.availability_unproven,
            "write_allowed": self.write_allowed,
            "reason_code_counts": [
                {"reason_code": code, "count": count} for code, count in self.reason_code_counts
            ],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """一条隔离记录的**脱敏**摘要（不含正文，不含输入里的额外列值）。"""

    row_number: int
    status: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    source: str
    source_record_id: str
    fingerprint: str | None
    oos_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "source": self.source,
            "source_record_id": self.source_record_id,
            "fingerprint": self.fingerprint,
            "oos_eligible": self.oos_eligible,
        }


@dataclass(frozen=True, slots=True)
class QuarantineSummary:
    """一次批次的隔离摘要（供 operator 人工复核；零网络、零写入）。"""

    scope: str
    rows: int
    total_quarantined: int
    reason_code_counts: tuple[tuple[str, int], ...]
    entries: tuple[QuarantineEntry, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATOR_WORKFLOW_SCHEMA_VERSION,
            "scope": self.scope,
            "rows": self.rows,
            "total_quarantined": self.total_quarantined,
            "reason_code_counts": [
                {"reason_code": code, "count": count} for code, count in self.reason_code_counts
            ],
            "entries": [entry.to_dict() for entry in self.entries],
            "notes": list(self.notes),
        }


def evaluate_write_gate(report: EvidenceIntakeReport) -> WriteGateDecision:
    """把一次 dry-run 结论汇总为写入门禁（纯函数；不触库、不联网、不写文件）。

    Args:
        report: 由 :func:`src.evidence.intake.intake_evidence` 在 ``dry_run=True``
            下产出的批次报告（身份冲突判定同样来自该次 dry-run 的库内哈希比对）。

    Returns:
        机器可读的 :class:`WriteGateDecision`；``write_allowed`` 仅表示存在可写行。
    """
    rows = report.rows
    quarantined_rows = tuple(row for row in rows if row.status is RowStatus.QUARANTINED)
    accepted_rows = tuple(row for row in rows if row.status is RowStatus.ACCEPTED)
    notes = (
        WRITE_GATE_NOTE,
        f"本判定来自 dry-run 报告（dry_run={str(report.dry_run).lower()}）；"
        "显式写入时会再次执行完整 Evidence Gateway 校验。",
    )
    return WriteGateDecision(
        scope=report.scope.value,
        dry_run_source=report.dry_run,
        rows=report.counts.rows,
        accepted=report.counts.accepted,
        quarantined=report.counts.quarantined,
        duplicate=report.counts.duplicate,
        conflict=_count_codes(rows, frozenset({ReasonCode.IDENTITY_CONFLICT})),
        not_oos_eligible=report.counts.not_oos_eligible,
        oos_eligible=report.counts.oos_eligible,
        synthetic=_count_codes(quarantined_rows, _SYNTHETIC_CODES),
        authorization_incomplete=_count_codes(quarantined_rows, _AUTHORIZATION_CODES),
        time_invalid=_count_codes(quarantined_rows, _TIME_CODES),
        content_invalid=_count_codes(quarantined_rows, _CONTENT_CODES),
        identity_issues=_count_codes(quarantined_rows, _IDENTITY_CODES),
        sensitive_detected=_count_codes(quarantined_rows, _SENSITIVE_CODES),
        provenance_missing=_count_codes(quarantined_rows, _PROVENANCE_CODES),
        availability_unproven=_count_codes(accepted_rows, _AVAILABILITY_CODES),
        write_allowed=bool(accepted_rows),
        reason_code_counts=_reason_tally(rows),
        notes=notes,
    )


def build_quarantine_summary(report: EvidenceIntakeReport) -> QuarantineSummary:
    """把一次批次报告里的隔离行整理为**可人工复核**的脱敏摘要（纯函数）。"""
    quarantined_rows = tuple(row for row in report.rows if row.status is RowStatus.QUARANTINED)
    entries = tuple(
        QuarantineEntry(
            row_number=row.row_number,
            status=row.status.value,
            reason_codes=tuple(code.value for code in row.reason_codes),
            reasons=tuple(_safe(text, max_chars=300) for text in row.reasons),
            source=_safe(row.source, max_chars=100),
            source_record_id=_safe(row.source_record_id, max_chars=100),
            fingerprint=_fingerprint_prefix(row.fingerprint),
            oos_eligible=row.oos_eligible,
        )
        for row in quarantined_rows
    )
    notes = (
        "隔离清单不含正文与输入额外列值；原因码稳定，可直接用于人工复核与复盘。",
        "隔离行**未落库、未计入** qualification ledger；如需采纳，必须先补齐授权 / 时间 / "
        "可用性证据并按新的 (source, source_record_id) 重新提交。",
    )
    return QuarantineSummary(
        scope=report.scope.value,
        rows=report.counts.rows,
        total_quarantined=len(entries),
        reason_code_counts=_reason_tally(quarantined_rows),
        entries=entries,
        notes=notes,
    )


def render_write_gate(decision: WriteGateDecision) -> str:
    """渲染人类可读的写入门禁摘要（脱敏；不解除 blocker）。"""
    tally = (
        "、".join(f"`{code}`={count}" for code, count in decision.reason_code_counts) or "无"
    )
    return "\n".join(
        [
            f"### 写入门禁（write gate）— scope `{decision.scope}`",
            "",
            f"- 行数：{decision.rows}；可写（`ACCEPTED`）：{decision.accepted}；"
            f"隔离：{decision.quarantined}；重复：{decision.duplicate}；"
            f"身份冲突：{decision.conflict}；不可用于 OOS：{decision.not_oos_eligible}",
            f"- 合成 / 示例（`SYNTHETIC_EVIDENCE`）：{decision.synthetic}；"
            f"授权不完整：{decision.authorization_incomplete}；"
            f"时间非法：{decision.time_invalid}；"
            f"来源 / 记录身份问题：{decision.identity_issues}",
            f"- 内容非法：{decision.content_invalid}；"
            f"出处缺失：{decision.provenance_missing}；"
            f"凭据拦截：{decision.sensitive_detected}；"
            f"历史可用证据不足：{decision.availability_unproven}",
            f"- 是否至少有一行可 append-only 写入：`{str(decision.write_allowed).lower()}`"
            "（**不表示**授权已通过人工核验、也不解除 blocker）",
            f"- 原因码计数：{tally}",
            "",
            *[f"- {note}" for note in decision.notes],
            "",
        ]
    )


def render_quarantine_summary(summary: QuarantineSummary) -> str:
    """渲染人类可读的隔离摘要（脱敏；供 operator 人工复核）。"""
    tally = (
        "、".join(f"`{code}`={count}" for code, count in summary.reason_code_counts) or "无"
    )
    lines = [
        f"### 隔离摘要（quarantine）— scope `{summary.scope}`",
        "",
        f"- 批次行数：{summary.rows}；隔离行数：{summary.total_quarantined}",
        f"- 原因码计数：{tally}",
        "",
        "| 行 | 状态 | 原因码 | 来源 | 来源记录 ID | 指纹 |",
        "|---:|---|---|---|---|---|",
    ]
    if not summary.entries:
        lines.append("| — | — | 无 | — | — | — |")
    for entry in summary.entries:
        lines.append(
            f"| {entry.row_number} | {entry.status} | "
            f"{', '.join(entry.reason_codes) or '—'} | {entry.source or '—'} | "
            f"{entry.source_record_id or '—'} | {entry.fingerprint or '—'} |"
        )
    lines += ["", *[f"- {note}" for note in summary.notes], ""]
    return "\n".join(lines)
