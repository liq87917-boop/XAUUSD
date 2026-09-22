"""只读运行健康度与数据资格观测层（GOLD-004）。

本包**只读**：
- :mod:`src.monitoring.collector_health`：从 ``collector_runs`` / ``raw_items`` /
  ``processed_items`` / ``job_runs`` / ``sources`` 汇总 source 级运行健康度；
- :mod:`src.monitoring.phase33_qualification`：复用 ``src.alpha.evidence_gate`` 的既有阈值，
  输出机器可读的 Phase 3.3 Author / News 资格缺口（blocker 保持 BLOCKED）；
- :mod:`src.monitoring.report`：组合报告（Markdown / 稳定 JSON）。

调用入口见 ``scripts/report_collector_health.py``。
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
from src.monitoring.phase33_qualification import (
    PHASE3_3_BLOCKER_CODE,
    AuthorGap,
    CheckStatus,
    QualificationGap,
    QualificationReport,
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
    "FAILED_STREAK_THRESHOLD",
    "MONITORING_SCHEMA_VERSION",
    "PHASE3_3_BLOCKER_CODE",
    "REPORT_TITLE",
    "AuthorGap",
    "CheckStatus",
    "HealthReport",
    "HealthState",
    "MonitoringReport",
    "ProcessorCounts",
    "ProcessorState",
    "ProcessorSummary",
    "QualificationGap",
    "QualificationReport",
    "SchedulerSummary",
    "SourceHealth",
    "classify_source_state",
    "load_health_report",
    "load_monitoring_report",
    "load_qualification_report",
    "render_health_report",
    "render_monitoring_report",
    "render_qualification_report",
]
