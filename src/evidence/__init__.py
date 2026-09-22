"""合规授权数据 Evidence Intake Gateway（GOLD-005）。

本包提供**独立于** Collector / Scheduler / Alpha 的证据接收与审计入口：

- :mod:`src.evidence.contracts`：版本化输入契约（``evidence-intake-v1``，Author / News）；
- :mod:`src.evidence.validation`：逐行机械校验（授权 / 来源身份 / 时间语义 / 内容完整性）；
- :mod:`src.evidence.intake`：validate-first / dry-run-first 的导入引擎
  （幂等、坏行隔离、append-only 落 ``raw_items`` + ``processed_items``）；
- :mod:`src.evidence.report`：Markdown 报告 + quarantine JSONL 载荷（全部脱敏）；
- :mod:`src.evidence.ledger`：只读台账，供 GOLD-004 资格观测层统计
  "经证据入口认证且具备独立历史可用证据"的记录。

红线（与 `.clinerules` 一致）：

- 不联网、不抓取、不绕过 robots / 条款 / 证书；不把"公开可访问"当作采集或训练授权；
- 不伪造 ``published_at`` / ``collected_at`` / ``effective_at`` / 历史可用时间；
- 不覆盖历史事实（只 append-only；内容冲突判 ``IDENTITY_CONFLICT`` 并隔离）；
- 不因代码完成或 Mock 测试解除 ``PHASE3_3_DATA``。

入口：``scripts/intake_evidence.py``（默认 dry-run、默认零写入、默认零网络）。
"""

from __future__ import annotations

from src.evidence.contracts import (
    ALLOWED_AUTHORIZATION_BASES,
    COMMON_REQUIRED_FIELDS,
    EVIDENCE_CONTRACT_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    QUARANTINE_REASON_CODES,
    AuthorizationDeclaration,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    required_field_names,
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
from src.evidence.validation import (
    EvidenceRecord,
    NormalizedRow,
    RowAssessment,
    assess_row,
    normalize_input_row,
)

__all__ = [
    "ALLOWED_AUTHORIZATION_BASES",
    "COMMON_REQUIRED_FIELDS",
    "EVIDENCE_CONTRACT_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "QUARANTINE_REASON_CODES",
    "AuthorizationDeclaration",
    "EvidenceIntakeReport",
    "EvidenceLedger",
    "EvidenceRecord",
    "EvidenceScope",
    "InputFile",
    "InputRow",
    "IntakeCounts",
    "NormalizedRow",
    "ReasonCode",
    "RowAssessment",
    "RowOutcome",
    "RowStatus",
    "ScopeLedger",
    "assess_row",
    "intake_evidence",
    "ledger_from_raw_json",
    "load_evidence_ledger",
    "load_input_rows",
    "normalize_input_row",
    "quarantine_payload",
    "read_input_file",
    "render_intake_report",
    "required_field_names",
    "resolve_format",
]
