"""采集健康度聚合层单元测试（GOLD-004；纯函数，不触库、不联网）。

覆盖：
- 状态判定优先级（DISABLED / NEVER_RUN / FAILED 连败 / NEVER_SUCCEEDED / STALE /
  DEGRADED / UNKNOWN / HEALTHY）；
- 连败口径（按时间由新到旧、部分失败打断连败）；
- 加工观测状态（OBSERVED / NOT_OBSERVED / NO_INPUT）；
- 脱敏（错误文本擦除凭据、URL 丢弃 query）；
- 稳定 JSON schema 与"unknown 绝不冒充 healthy"。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from database.models.enums import CollectorRunStatus
from src.monitoring.collector_health import (
    FAILED_STREAK_THRESHOLD,
    HEALTH_SCHEMA_VERSION,
    HealthState,
    ProcessorCounts,
    ProcessorState,
    ProcessorSummary,
    SchedulerSummary,
    SourceHealth,
    build_health_report,
    build_source_health,
    classify_processor_state,
    classify_source_state,
    consecutive_failures,
    render_health_report,
)

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=90)
SECRET = "SECRETVALUE-9f1c"

_TOP_LEVEL_KEYS = {
    "schema_version",
    "as_of",
    "window",
    "state",
    "healthy",
    "reason",
    "sources",
    "scheduler",
    "processor",
    "totals",
    "unattributed_runs",
}


def _source_health(
    name: str = "src",
    *,
    statuses: Sequence[CollectorRunStatus] = (),
    last_success_at: datetime | None = MOMENT - timedelta(minutes=10),
    enabled: bool = True,
    inserted: int = 0,
    duplicate: int = 0,
    raw_items: int = 0,
    processed: ProcessorCounts | None = None,
    error: str | None = None,
) -> SourceHealth:
    return build_source_health(
        source_name=name,
        source_id="source-id",
        source_type="NEWS",
        enabled=enabled,
        collector="probe_collector",
        base_url=f"https://feed.invalid/rss.xml?api_key={SECRET}",
        statuses=list(statuses),
        last_success_at=last_success_at,
        inserted_reported=inserted,
        duplicate_reported=duplicate,
        raw_items_observed=raw_items,
        processed=processed or ProcessorCounts(),
        last_run_at=MOMENT - timedelta(minutes=5),
        last_error=error,
        as_of=MOMENT,
        stale_after=STALE_AFTER,
    )


def _report(sources: Sequence[SourceHealth], processed: ProcessorCounts | None = None):
    counts = processed or ProcessorCounts()
    state, reason = classify_processor_state(
        raw_items_observed=sum(item.raw_items_observed for item in sources), counts=counts
    )
    return build_health_report(
        as_of=MOMENT,
        window_hours=24,
        stale_after=STALE_AFTER,
        sources=list(sources),
        scheduler=SchedulerSummary(
            slots=0,
            success=0,
            partial_failed=0,
            failed=0,
            in_flight=0,
            other=0,
            retries=0,
            per_source_entries=0,
            slots_without_output=0,
            last_slot_at=None,
        ),
        processor=ProcessorSummary(state=state, counts=counts, reason=reason),
        unattributed_runs=0,
    )


def test_consecutive_failures_counts_only_leading_streak() -> None:
    assert consecutive_failures([CollectorRunStatus.FAILED] * 4) == 4
    assert consecutive_failures([]) == 0
    # 最新的成功会打断连败（无论历史失败多少）
    assert (
        consecutive_failures(
            [CollectorRunStatus.SUCCESS, CollectorRunStatus.FAILED, CollectorRunStatus.FAILED]
        )
        == 0
    )
    # 部分失败不是硬失败，同样打断连败
    assert (
        consecutive_failures(
            [CollectorRunStatus.PARTIAL_FAILED, CollectorRunStatus.FAILED]
        )
        == 0
    )
    assert (
        consecutive_failures(
            [CollectorRunStatus.FAILED, CollectorRunStatus.FAILED, CollectorRunStatus.SUCCESS]
        )
        == 2
    )


def test_classify_source_state_branches() -> None:
    def state(**overrides: object) -> tuple[HealthState, str]:
        params: dict[str, object] = {
            "enabled": True,
            "runs": 0,
            "succeeded": 0,
            "partial": 0,
            "failed": 0,
            "in_flight": 0,
            "failure_streak": 0,
            "last_success_at": None,
            "as_of": MOMENT,
            "stale_after": STALE_AFTER,
        }
        params.update(overrides)
        return classify_source_state(**params)  # type: ignore[arg-type]

    assert state(enabled=False)[0] is HealthState.DISABLED
    assert state()[0] is HealthState.NEVER_RUN
    assert (
        state(failure_streak=FAILED_STREAK_THRESHOLD, runs=3, failed=3)[0] is HealthState.FAILED
    )
    assert state(runs=1, failed=1)[0] is HealthState.FAILED
    assert state(runs=1, partial=1)[0] is HealthState.NEVER_SUCCEEDED
    assert state(runs=1, in_flight=1)[0] is HealthState.NEVER_SUCCEEDED
    assert (
        state(
            runs=1,
            succeeded=1,
            last_success_at=MOMENT - timedelta(days=5),
        )[0]
        is HealthState.STALE
    )
    assert (
        state(
            runs=2,
            succeeded=1,
            failed=1,
            last_success_at=MOMENT - timedelta(minutes=10),
        )[0]
        is HealthState.DEGRADED
    )
    assert (
        state(
            runs=1,
            succeeded=1,
            last_success_at=MOMENT - timedelta(minutes=10),
        )[0]
        is HealthState.HEALTHY
    )
    unknown, reason = state(succeeded=1, last_success_at=MOMENT - timedelta(minutes=10))
    assert unknown is HealthState.UNKNOWN
    assert "窗口内无运行记录" in reason


def test_classify_processor_state_never_claims_observed_without_facts() -> None:
    assert classify_processor_state(
        raw_items_observed=0, counts=ProcessorCounts()
    )[0] is ProcessorState.NO_INPUT
    observed, reason = classify_processor_state(
        raw_items_observed=5, counts=ProcessorCounts()
    )
    assert observed is ProcessorState.NOT_OBSERVED
    assert "Processor 未启用或未运行" in reason
    assert classify_processor_state(
        raw_items_observed=0, counts=ProcessorCounts(success=1)
    )[0] is ProcessorState.OBSERVED


def test_source_health_redacts_credentials_and_url_query() -> None:
    health = _source_health(error=f"RuntimeError: api_key={SECRET}")
    payload = health.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert SECRET not in text
    assert "***" in text
    assert payload["base_url"] == "https://feed.invalid/rss.xml"
    assert payload["last_error"] is not None
    assert "api_key=***" in payload["last_error"]


def test_empty_sources_report_is_not_healthy_and_schema_is_stable() -> None:
    report = _report([])
    payload = report.to_dict()
    assert set(payload) == _TOP_LEVEL_KEYS
    assert payload["schema_version"] == HEALTH_SCHEMA_VERSION
    assert payload["state"] == HealthState.NO_SOURCES.value
    assert payload["healthy"] is False
    assert payload["sources"] == []
    assert payload["totals"]["sources"] == 0
    assert payload["processor"]["state"] == ProcessorState.NO_INPUT.value
    assert payload["unattributed_runs"] == 0


def test_healthy_source_requires_observed_processor() -> None:
    healthy = _source_health(
        statuses=[CollectorRunStatus.SUCCESS],
        raw_items=0,
    )
    # 无原始数据 → 加工 NO_INPUT：整体仍可为 HEALTHY（但会显式标注 NO_INPUT）
    report = _report([healthy])
    assert report.state is HealthState.HEALTHY
    assert report.healthy is True

    # 有原始数据但无加工结果 → 整体必须降级，绝不报 healthy
    not_observed = _source_health(
        statuses=[CollectorRunStatus.SUCCESS],
        raw_items=3,
    )
    degraded = _report([not_observed])
    assert degraded.state is HealthState.DEGRADED
    assert degraded.healthy is False
    assert "加工结果" in degraded.reason
    assert degraded.sources[0].processor_state is ProcessorState.NOT_OBSERVED


def test_any_failed_source_makes_overall_failed() -> None:
    good = _source_health("good", statuses=[CollectorRunStatus.SUCCESS])
    bad = _source_health(
        "bad",
        statuses=[CollectorRunStatus.FAILED] * FAILED_STREAK_THRESHOLD,
        last_success_at=None,
    )
    report = _report([good, bad])
    assert report.state is HealthState.FAILED
    assert report.healthy is False
    assert "bad" in report.reason
    assert report.totals["sources_non_healthy"] == 1


def test_duplicate_only_success_is_healthy_but_counters_are_honest() -> None:
    source = _source_health(
        statuses=[CollectorRunStatus.SUCCESS],
        inserted=0,
        duplicate=5,
    )
    report = _report([source])
    assert source.state is HealthState.HEALTHY
    assert source.inserted_reported == 0
    assert source.duplicate_reported == 5
    assert report.totals["inserted_reported"] == 0
    assert report.totals["duplicate_reported"] == 5


def test_render_never_leaks_secret_and_marks_states() -> None:
    source = _source_health(
        statuses=[CollectorRunStatus.FAILED],
        last_success_at=None,
        error=f"RuntimeError: token={SECRET}",
    )
    report = _report([source])
    text = render_health_report(report)
    # 渲染层不输出错误原文（只输出状态与计数），更不可能泄露凭据
    assert SECRET not in text
    assert "token=" not in text
    assert "FAILED" in text
    assert "Processor" in text
