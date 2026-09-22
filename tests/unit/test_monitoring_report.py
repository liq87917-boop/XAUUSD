"""组合报告单元测试（GOLD-004）：只读组合结构、HTTP/CLI 共用渲染与稳定 JSON。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from database.models.enums import CollectorRunStatus
from src.alpha.evidence_gate import NewsReadiness, Phase33Readiness
from src.monitoring.collector_health import (
    HealthState,
    ProcessorCounts,
    ProcessorState,
    ProcessorSummary,
    SchedulerSummary,
    build_health_report,
    build_source_health,
)
from src.monitoring.phase33_qualification import build_qualification_report
from src.monitoring.report import (
    MONITORING_SCHEMA_VERSION,
    REPORT_TITLE,
    MonitoringReport,
    render_monitoring_report,
)

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=90)


def _monitoring_report() -> MonitoringReport:
    source = build_source_health(
        source_name="src",
        source_id="id-1",
        source_type="NEWS",
        enabled=True,
        collector="probe",
        base_url=None,
        statuses=[CollectorRunStatus.SUCCESS],
        last_success_at=MOMENT - timedelta(minutes=5),
        inserted_reported=1,
        duplicate_reported=0,
        raw_items_observed=0,
        processed=ProcessorCounts(),
        last_run_at=MOMENT - timedelta(minutes=5),
        last_error=None,
        as_of=MOMENT,
        stale_after=STALE_AFTER,
    )
    health = build_health_report(
        as_of=MOMENT,
        window_hours=24,
        stale_after=STALE_AFTER,
        sources=[source],
        scheduler=SchedulerSummary(
            slots=1,
            success=1,
            partial_failed=0,
            failed=0,
            in_flight=0,
            other=0,
            retries=0,
            per_source_entries=1,
            slots_without_output=0,
            last_slot_at=MOMENT - timedelta(minutes=5),
        ),
        processor=ProcessorSummary(
            state=ProcessorState.NO_INPUT, counts=ProcessorCounts(), reason="窗口内没有原始数据"
        ),
        unattributed_runs=0,
    )
    readiness = Phase33Readiness(
        authors=(),
        label_status_counts=(),
        news=NewsReadiness(0, 0, 0.0, (), False),
        hf_weak_supervision_rows=0,
        author_ready=False,
        news_ready=False,
        as_of=MOMENT,
    )
    return MonitoringReport(
        schema_version=MONITORING_SCHEMA_VERSION,
        generated_at=MOMENT,
        window_hours=24,
        health=health,
        qualification=build_qualification_report(readiness),
    )


def test_to_dict_exposes_health_and_qualification() -> None:
    payload = _monitoring_report().to_dict()
    assert set(payload) == {
        "schema_version",
        "report",
        "generated_at",
        "window_hours",
        "healthy",
        "blocker_code",
        "blocker_active",
        "health",
        "qualification",
    }
    assert payload["report"] == REPORT_TITLE
    assert payload["schema_version"] == MONITORING_SCHEMA_VERSION
    assert payload["healthy"] is True
    assert payload["blocker_active"] is True
    assert payload["health"]["state"] == HealthState.HEALTHY.value
    assert payload["qualification"]["blocker_code"] == "PHASE3_3_DATA"


def test_render_contains_both_sections_and_is_deterministic() -> None:
    report = _monitoring_report()
    text = render_monitoring_report(report)
    assert text == report.render()
    assert REPORT_TITLE in text
    assert "## 1. 采集运行健康度（只读）" in text
    assert "## 2. Phase 3.3 数据资格缺口（只读）" in text
    assert "PHASE3_3_DATA" in text
