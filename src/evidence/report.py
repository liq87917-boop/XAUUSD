"""Evidence Intake 报告渲染（Markdown + quarantine JSONL 载荷）。

- 渲染与 JSON **同一事实来源**（``EvidenceIntakeReport``），不引入额外业务判断；
- 报告只包含脱敏字段（来源 / 记录 ID / 指纹 / 原因码 / 时间），
  **绝不输出正文、凭据或输入里的额外列值**；
- 报告不解除 ``PHASE3_3_DATA``：它只说明"哪些记录经证据入口认证"，资格由
  ``src/monitoring/phase33_qualification.py`` 与人工 Gate 决定。
"""

from __future__ import annotations

from typing import Any, Final

from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, RowStatus
from src.evidence.intake import EvidenceIntakeReport, RowOutcome

__all__ = [
    "REPORT_TITLE",
    "quarantine_payload",
    "render_intake_report",
    "render_row_table",
]

#: 报告标题（终端与落盘 Markdown 共用）
REPORT_TITLE: Final[str] = "授权证据 Evidence Intake 报告"


def render_row_table(rows: tuple[RowOutcome, ...]) -> str:
    """把逐行结论渲染为 Markdown 表格。"""
    lines = [
        "| 行 | 状态 | 原因码 | OOS 可用 | 来源 | 来源记录 ID | 指纹 |",
        "|---:|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| — | — | — | — | — | — | — |")
    for row in rows:
        codes = ", ".join(code.value for code in row.reason_codes) or "—"
        fingerprint = (row.fingerprint or "—")[:16]
        lines.append(
            f"| {row.row_number} | {row.status.value} | {codes} | "
            f"{'yes' if row.oos_eligible else 'no'} | {row.source or '—'} | "
            f"{row.source_record_id or '—'} | {fingerprint} |"
        )
    return "\n".join(lines)


def quarantine_payload(report: EvidenceIntakeReport) -> list[dict[str, Any]]:
    """隔离清单的 JSONL 载荷（只含脱敏字段与稳定原因码）。"""
    payload: list[dict[str, Any]] = []
    for row in report.quarantined:
        entry = row.to_dict()
        entry.update(
            {
                "contract_version": EVIDENCE_CONTRACT_VERSION,
                "scope": report.scope.value,
                "recorded_at": report.generated_at.isoformat(),
                "input_sha256": report.input_file.sha256,
            }
        )
        payload.append(entry)
    return payload


def render_intake_report(report: EvidenceIntakeReport) -> str:
    """渲染人类可读 Markdown（控制台与落盘共用）。"""
    counts = report.counts
    mode = "dry-run（未写库）" if report.dry_run else "提交（append-only 写入 raw/processed）"
    lines = [
        f"# {REPORT_TITLE}",
        "",
        "> 本命令**不联网**：只读取本地输入文件，不抓取站点、不绕过 robots / 条款 / 证书限制；",
        "> 默认 dry-run，只有显式 `--no-dry-run` 才 append-only 写入 `raw_items` + "
        "`processed_items`；",
        "> 仅凭本命令或 Mock 测试**不能**解除 `PHASE3_3_DATA`（授权法律效力与历史可用性"
        "仍需人工核验）。",
        "",
        f"- 契约版本：`{report.contract_version}`",
        f"- 证据类别：`{report.scope.value}`（{report.scope.label}）",
        f"- 模式：{mode}",
        f"- 审计时点（UTC）：{report.generated_at.isoformat()}",
        f"- 输入：`{report.input_file.path}`（格式 `{report.input_file.format}`，"
        f"SHA-256 `{report.input_file.sha256}`）",
        f"- 行数：{counts.rows}；通过 {counts.accepted}；隔离 {counts.quarantined}；"
        f"重复 {counts.duplicate}；落库 {counts.persisted}；新建来源 {counts.sources_created}；"
        f"加工成功 {counts.processed_success}",
        f"- OOS 可用 {counts.oos_eligible}；不可用于 OOS {counts.not_oos_eligible}"
        f"（`AVAILABILITY_UNPROVEN` / `NOT_OOS_ELIGIBLE`）",
        "",
        "## 逐行结论",
        "",
        render_row_table(report.rows),
        "",
        "## 隔离清单（原因码稳定，文本已脱敏）",
        "",
        "| 行 | 原因码 | 原因 |",
        "|---:|---|---|",
    ]
    if not report.quarantined:
        lines.append("| — | — | 无 |")
    for row in report.quarantined:
        codes = ", ".join(code.value for code in row.reason_codes)
        lines.append(f"| {row.row_number} | {codes} | {'；'.join(row.reasons) or '—'} |")
    lines += [
        "",
        "## 口径与边界",
        "",
    ]
    lines.extend(f"- {note}" for note in report.notes)
    lines += [
        "",
        f"- 通过（`{RowStatus.ACCEPTED.value}`）只表示**契约字段齐全**，"
        "不表示授权在法律上成立；",
        "- `published_at` 早于 `collected_at` 本身不等于历史可用；",
        "  只有独立 `available_at` 证据才具备 OOS 资格。",
        "",
    ]
    return "\n".join(lines)
