"""合规授权数据 Evidence Intake Gateway（GOLD-005）。

本包提供**独立于** Collector / Scheduler / Alpha 的证据接收与审计入口：

- :mod:`src.evidence.contracts`：版本化输入契约（``evidence-intake-v1``，Author / News）；
- :mod:`src.evidence.validation`：逐行机械校验（授权 / 来源身份 / 时间语义 / 内容完整性）；
- :mod:`src.evidence.intake`：validate-first / dry-run-first 的导入引擎
  （幂等、坏行隔离、append-only 落 ``raw_items`` + ``processed_items``）；
- :mod:`src.evidence.report`：Markdown 报告 + quarantine JSONL 载荷（全部脱敏）；
- :mod:`src.evidence.templates`：Author / News 的 operator-ready 输入模板（GOLD-006），
  模板行带显式 synthetic/example 标记，导入时判 ``SYNTHETIC_EVIDENCE`` 隔离、绝不计入；
- :mod:`src.evidence.ledger`：只读台账，供 GOLD-004 资格观测层统计
  "经证据入口认证且具备独立历史可用证据"的记录；
- :mod:`src.evidence.workflow`：operator 工作流的写入门禁与隔离摘要（GOLD-007），
  显式写入前二次验证；只允许 ``ACCEPTED`` 行 append-only 落库；
- :mod:`src.evidence.author_chain`：**gateway-only** 作者归属链（GOLD-007），
  只消费已通过证据入口的 Author 证据，普通 CSV / 历史样本无法绕过。

红线（与 `.clinerules` 一致）：

- 不联网、不抓取、不绕过 robots / 条款 / 证书；不把"公开可访问"当作采集或训练授权；
- 不伪造 ``published_at`` / ``collected_at`` / ``effective_at`` / 历史可用时间；
- 不覆盖历史事实（只 append-only；内容冲突判 ``IDENTITY_CONFLICT`` 并隔离）；
- 不因代码完成或 Mock 测试解除 ``PHASE3_3_DATA``。

入口：``scripts/intake_evidence.py``（单步 intake）与 ``scripts/evidence_operator.py``
（GOLD-007 单入口 operator workflow；两者均默认 dry-run、默认零写入、默认零网络）。
"""

from __future__ import annotations

from src.evidence.author_chain import (
    AUTHOR_CHAIN_NOTE,
    AUTHOR_CHAIN_SCHEMA_VERSION,
    AuthorChainReport,
    GatewayAuthorEvidence,
    attribute_gateway_author_evidence,
    gateway_author_evidence,
    load_gateway_author_evidence,
)
from src.evidence.contracts import (
    ALLOWED_AUTHORIZATION_BASES,
    COMMON_REQUIRED_FIELDS,
    EVIDENCE_CONTRACT_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EXAMPLE_MARKER_FLAG_FIELDS,
    EXAMPLE_MARKER_KIND_FIELDS,
    EXAMPLE_MARKER_VALUES,
    QUARANTINE_REASON_CODES,
    AuthorizationDeclaration,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    required_field_names,
    synthetic_marker_fields,
)
from src.evidence.handoff import (
    BLOCKED_STATUS,
    CHECK_CATEGORIES,
    HANDOFF_NOTE,
    HANDOFF_REPORT_NAME,
    HANDOFF_SCHEMA_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
    ChecklistItem,
    EvidenceHandoffReport,
    ExcludedEvidence,
    ScopeGap,
    build_evidence_checklist,
    build_excluded_evidence,
    build_handoff_report,
    render_handoff_markdown,
)
from src.evidence.intake import (
    EvidenceIntakeReport,
    InputFile,
    InputRow,
    IntakeCounts,
    RowOutcome,
    intake_evidence,
    load_input_rows,
    read_input_file,
    resolve_format,
)
from src.evidence.ledger import (
    EvidenceLedger,
    ScopeLedger,
    ledger_from_raw_json,
    load_evidence_ledger,
)
from src.evidence.report import quarantine_payload, render_intake_report
from src.evidence.templates import (
    EXAMPLE_MARKER_COLUMNS,
    TEMPLATE_FORMATS,
    TEMPLATE_ROOT,
    TEMPLATE_SCHEMA_VERSION,
    example_rows,
    render_template,
    template_columns,
    template_output_name,
    write_template,
)
from src.evidence.validation import (
    EvidenceRecord,
    NormalizedRow,
    RowAssessment,
    assess_row,
    normalize_input_row,
)
from src.evidence.workflow import (
    OPERATOR_STEPS,
    OPERATOR_WORKFLOW_SCHEMA_VERSION,
    WRITE_GATE_NOTE,
    QuarantineEntry,
    QuarantineSummary,
    WriteGateDecision,
    build_quarantine_summary,
    evaluate_write_gate,
    render_quarantine_summary,
    render_write_gate,
)

__all__ = [
    "ALLOWED_AUTHORIZATION_BASES",
    "AUTHOR_CHAIN_NOTE",
    "AUTHOR_CHAIN_SCHEMA_VERSION",
    "BLOCKED_STATUS",
    "CHECK_CATEGORIES",
    "COMMON_REQUIRED_FIELDS",
    "EVIDENCE_CONTRACT_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "EXAMPLE_MARKER_COLUMNS",
    "EXAMPLE_MARKER_FLAG_FIELDS",
    "EXAMPLE_MARKER_KIND_FIELDS",
    "EXAMPLE_MARKER_VALUES",
    "HANDOFF_NOTE",
    "HANDOFF_REPORT_NAME",
    "HANDOFF_SCHEMA_VERSION",
    "OPERATOR_STEPS",
    "OPERATOR_WORKFLOW_SCHEMA_VERSION",
    "PENDING_HUMAN_REVIEW_STATUS",
    "QUARANTINE_REASON_CODES",
    "TEMPLATE_FORMATS",
    "TEMPLATE_ROOT",
    "TEMPLATE_SCHEMA_VERSION",
    "WRITE_GATE_NOTE",
    "AuthorizationDeclaration",
    "AuthorChainReport",
    "ChecklistItem",
    "EvidenceHandoffReport",
    "EvidenceIntakeReport",
    "EvidenceLedger",
    "EvidenceRecord",
    "EvidenceScope",
    "ExcludedEvidence",
    "GatewayAuthorEvidence",
    "InputFile",
    "InputRow",
    "IntakeCounts",
    "NormalizedRow",
    "QuarantineEntry",
    "QuarantineSummary",
    "ReasonCode",
    "RowAssessment",
    "RowOutcome",
    "RowStatus",
    "ScopeGap",
    "ScopeLedger",
    "WriteGateDecision",
    "assess_row",
    "attribute_gateway_author_evidence",
    "build_evidence_checklist",
    "build_excluded_evidence",
    "build_handoff_report",
    "build_quarantine_summary",
    "evaluate_write_gate",
    "example_rows",
    "gateway_author_evidence",
    "intake_evidence",
    "ledger_from_raw_json",
    "load_evidence_ledger",
    "load_gateway_author_evidence",
    "load_input_rows",
    "normalize_input_row",
    "quarantine_payload",
    "read_input_file",
    "render_handoff_markdown",
    "render_intake_report",
    "render_quarantine_summary",
    "render_template",
    "render_write_gate",
    "required_field_names",
    "resolve_format",
    "synthetic_marker_fields",
    "template_columns",
    "template_output_name",
    "write_template",
]
