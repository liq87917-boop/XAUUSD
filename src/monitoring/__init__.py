"""只读运行健康度与数据资格观测层（GOLD-004）。

本包**只读**：
- :mod:`src.monitoring.collector_health`：从 ``collector_runs`` / ``raw_items`` /
  ``processed_items`` / ``job_runs`` / ``sources`` 汇总 source 级运行健康度；
- :mod:`src.monitoring.phase33_qualification`：复用 ``src.alpha.evidence_gate`` 的既有阈值，
  输出机器可读的 Phase 3.3 Author / News 资格缺口（blocker 保持 BLOCKED）；
- :mod:`src.monitoring.evidence_readiness`：证据就绪度 / preflight（GOLD-006），
  量化证据入口台账与阈值之间的 remaining gap，并支持一键资格复核（blocker 恒为 active）；
- :mod:`src.monitoring.report`：组合报告（Markdown / 稳定 JSON）。

调用入口见 ``scripts/report_collector_health.py`` 与 ``scripts/evidence_readiness.py``。
"""

from __future__ import annotations

from src.monitoring.collector_health import (
    DEFAULT_STALE_AFTER,
    DEFAULT_WINDOW_HOURS,
    FAILED_STREAK_THRESHOLD,
    HealthReport,
    HealthState,
    ProcessorCounts,
    ProcessorState,
    ProcessorSummary,
    SchedulerSummary,
    SourceHealth,
    classify_source_state,
    load_health_report,
    render_health_report,
)
from src.monitoring.evidence_readiness import (
    READINESS_NOTE,
    READINESS_REPORT_TITLE,
    READINESS_SCHEMA_VERSION,
    BatchQuantification,
    EvidenceReadinessReport,
    ReadinessCheck,
    ScopeReadiness,
    build_readiness_report,
    render_readiness_report,
    summarize_batch,
)
from src.monitoring.phase33_qualification import (
    EVIDENCE_INTAKE_NOTE,
    EVIDENCE_INTAKE_SCHEMA_VERSION,
    PHASE3_3_BLOCKER_CODE,
    AuthorGap,
    CheckStatus,
    EvidenceIntakeSection,
    QualificationGap,
    QualificationReport,
    build_evidence_intake_section,
    load_qualification_report,
    render_qualification_report,
)
from src.monitoring.report import (
    MONITORING_SCHEMA_VERSION,
    REPORT_TITLE,
    MonitoringReport,
    load_monitoring_report,
    render_monitoring_report,
)

__all__ = [
    "DEFAULT_STALE_AFTER",
    "DEFAULT_WINDOW_HOURS",
    "EVIDENCE_INTAKE_NOTE",
    "EVIDENCE_INTAKE_SCHEMA_VERSION",
    "FAILED_STREAK_THRESHOLD",
    "MONITORING_SCHEMA_VERSION",
    "PHASE3_3_BLOCKER_CODE",
    "READINESS_NOTE",
    "READINESS_REPORT_TITLE",
    "READINESS_SCHEMA_VERSION",
    "REPORT_TITLE",
    "AuthorGap",
    "BatchQuantification",
    "CheckStatus",
    "EvidenceIntakeSection",
    "EvidenceReadinessReport",
    "HealthReport",
    "HealthState",
    "MonitoringReport",
    "ProcessorCounts",
    "ProcessorState",
    "ProcessorSummary",
    "QualificationGap",
    "QualificationReport",
    "ReadinessCheck",
    "SchedulerSummary",
    "ScopeReadiness",
    "SourceHealth",
    "build_evidence_intake_section",
    "build_readiness_report",
    "classify_source_state",
    "load_health_report",
    "load_monitoring_report",
    "load_qualification_report",
    "render_health_report",
    "render_monitoring_report",
    "render_qualification_report",
    "render_readiness_report",
    "summarize_batch",
]
