"""Scheduler 核心集成测试（SQLite，100% Mock，零网络）。

覆盖 TD-09 acceptance：
- 同一 30 分钟槽重复触发只执行一次、不产生第二条 JobRun；
- 下一个槽 / UTC 槽边界可执行；
- 构造失败与执行失败隔离，状态诚实（PARTIAL_FAILED / FAILED）；
- stale RUNNING/RETRYING 安全恢复并增加 retry_count；非 stale 不接管；
- 敏感字段（api_key / token / Authorization）不进入 output_json。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import JobRun, RawItem, Source
from database.models.enums import JobStatus, RawItemType, SourceType
from src.collectors.base import BaseCollector
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload
from src.scheduler.core import (
    JOB_TYPE,
    SchedulerRunResult,
    run_scheduled_collection_once,
)
from src.scheduler.slots import resolve_slot

pytestmark = pytest.mark.integration

SLOT_A_MOMENT = datetime(2026, 9, 22, 12, 7, 30, tzinfo=UTC)
SLOT_A_START = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SLOT_B_MOMENT = datetime(2026, 9, 22, 12, 40, tzinfo=UTC)


class _FixedClock:
    """可推进的假时钟（保证槽边界可断言，且不依赖真实时间）。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def set(self, moment: datetime) -> None:
        self.moment = moment


class _CountingCollector(BaseCollector):
    """成功采集器：只记录调用，不落库（用于幂等 / 隔离断言）。"""

    collector_name = "scheduler_counting"
    source_type = SourceType.NEWS

    def __init__(self, source: Source, *, calls: list[str], **kwargs: object) -> None:
        super().__init__(source, **kwargs)  # type: ignore[arg-type]
        self._calls = calls

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        self._calls.append(self.source.name)
        return FetchPage(next_cursor=None)


class _PersistingCollector(BaseCollector):
    """写入 1 条 raw_item，用于验证 output_json 的统计摘要字段。"""

    collector_name = "scheduler_persisting"
    source_type = SourceType.NEWS

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        token = uuid.uuid4().hex
        payload = RawItemPayload(
            source_record_id=token,
            item_type=RawItemType.NEWS,
            title="gold outlook",
            content_text=f"XAUUSD 短线偏多 {token}",
            published_at=window.end_utc - timedelta(minutes=5),
            raw_json={"id": token},
        )
        return FetchPage(payloads=(payload,), next_cursor=None)


class _FailingCollector(BaseCollector):
    """执行阶段失败（provider 故障），用于验证故障隔离。"""

    collector_name = "scheduler_failing"
    source_type = SourceType.NEWS

    def __init__(self, source: Source, *, error: str = "boom", **kwargs: object) -> None:
        super().__init__(source, **kwargs)  # type: ignore[arg-type]
        self._error = error

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        raise RuntimeError(self._error)


def _make_collector_source(
    make_source,
    name: str,
    *,
    collector: str,
    enabled: bool = True,
    config: dict | None = None,
):
    payload: dict = {"collector": collector}
    if config:
        payload.update(config)
    return make_source(
        name=name, source_type=SourceType.NEWS, enabled=enabled, config_json=payload
    )


def _run(
    session: Session,
    *,
    clock: _FixedClock,
    factory=None,
    interval_minutes: int = 30,
    stale_after: timedelta | None = None,
) -> SchedulerRunResult:
    return asyncio.run(
        run_scheduled_collection_once(
            session,
            interval_minutes=interval_minutes,
            clock=clock,
            stale_after=stale_after,
            collector_factory=factory or None,
        )
    )


def _job_runs(session: Session) -> list[JobRun]:
    return list(session.scalars(sa.select(JobRun).order_by(JobRun.scheduled_at)))


def _raw_item_count(session: Session) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(RawItem)) or 0)


def test_same_slot_triggered_twice_executes_once(session: Session, make_source) -> None:
    source = _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    clock = _FixedClock(SLOT_A_MOMENT)
    first = _run(session, clock=clock, factory=factory)
    second = _run(session, clock=clock, factory=factory)

    assert first.executed is True
    assert first.status is JobStatus.SUCCESS
    assert second.executed is False
    assert second.job_run_id == first.job_run_id
    assert calls == [source.name]

    runs = _job_runs(session)
    assert len(runs) == 1
    assert runs[0].idempotency_key == resolve_slot(SLOT_A_MOMENT, 30).idempotency_key


def test_next_slot_executes_again(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    clock = _FixedClock(SLOT_A_MOMENT)
    first = _run(session, clock=clock, factory=factory)
    clock.set(SLOT_B_MOMENT)
    second = _run(session, clock=clock, factory=factory)

    assert second.executed is True
    assert second.job_run_id != first.job_run_id
    assert calls == ["src-a", "src-a"]
    assert len(_job_runs(session)) == 2


def test_utc_slot_boundary_creates_distinct_job_runs(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    clock = _FixedClock(datetime(2026, 9, 22, 12, 29, 59, tzinfo=UTC))
    _run(session, clock=clock, factory=factory)
    clock.set(datetime(2026, 9, 22, 12, 30, tzinfo=UTC))
    _run(session, clock=clock, factory=factory)

    keys = {run.idempotency_key for run in _job_runs(session)}
    assert keys == {
        "collector_scheduler:30m:20260922T120000Z",
        "collector_scheduler:30m:20260922T123000Z",
    }


def test_terminal_job_run_is_not_rerun(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    slot = resolve_slot(SLOT_A_MOMENT, 30)
    session.add(
        JobRun(
            job_type=JOB_TYPE,
            idempotency_key=slot.idempotency_key,
            scheduled_at=slot.start_at,
            started_at=slot.start_at,
            finished_at=slot.start_at,
            status=JobStatus.SUCCESS,
            retry_count=0,
        )
    )
    session.flush()
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.executed is False
    assert result.reason == "already_success"
    assert result.status is JobStatus.SUCCESS
    assert calls == []
    assert len(_job_runs(session)) == 1


def test_execution_failure_isolated_and_reported(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-ok", collector="scheduler_counting")
    _make_collector_source(make_source, "src-bad", collector="scheduler_failing")
    calls: list[str] = []

    def factory(src):
        if src.name == "src-bad":
            return _FailingCollector(src, error="provider down")
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.status is JobStatus.PARTIAL_FAILED
    assert {s.source_name: s.status for s in result.sources} == {
        "src-ok": "SUCCESS",
        "src-bad": "FAILED",
    }
    assert calls == ["src-ok"]

    job = _job_runs(session)[0]
    assert job.status is JobStatus.PARTIAL_FAILED
    assert "src-bad" in (job.error_message or "")
    assert job.output_json is not None
    entries = {item["source"]: item for item in job.output_json["sources"]}
    assert entries["src-ok"]["status"] == "SUCCESS"
    assert entries["src-bad"]["status"] == "FAILED"
    assert "provider down" in entries["src-bad"]["error"]


def test_construction_failure_isolated(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-ok", collector="scheduler_counting")
    _make_collector_source(make_source, "src-broken", collector="not_registered")
    calls: list[str] = []

    def factory(src):
        if src.name == "src-broken":
            raise RuntimeError("采集器未注册：not_registered")
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.status is JobStatus.PARTIAL_FAILED
    statuses = {s.source_name: s.status for s in result.sources}
    assert statuses == {"src-ok": "SUCCESS", "src-broken": "CONSTRUCTION_FAILED"}
    assert calls == ["src-ok"]

    job = _job_runs(session)[0]
    broken = next(
        item for item in job.output_json["sources"] if item["source"] == "src-broken"
    )
    assert broken["status"] == "CONSTRUCTION_FAILED"
    assert broken["collector"] == "not_registered"
    assert "not_registered" in broken["error"]


def test_all_sources_failing_reports_failed(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-bad-1", collector="scheduler_failing")
    _make_collector_source(make_source, "src-bad-2", collector="scheduler_failing")

    def factory(src):
        return _FailingCollector(src)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.status is JobStatus.FAILED
    assert result.is_failure is True
    assert all(s.is_hard_failure for s in result.sources)
    assert _job_runs(session)[0].status is JobStatus.FAILED


def test_stale_running_is_recovered_with_retry_count(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    slot = resolve_slot(SLOT_A_MOMENT, 30)
    stale_job = JobRun(
        job_type=JOB_TYPE,
        idempotency_key=slot.idempotency_key,
        scheduled_at=slot.start_at,
        started_at=SLOT_A_START - timedelta(hours=2),
        finished_at=None,
        status=JobStatus.RUNNING,
        retry_count=0,
    )
    session.add(stale_job)
    session.flush()
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.executed is True
    assert result.reason == "stale_recovered"
    assert result.retry_count == 1
    assert result.job_run_id == stale_job.id
    assert calls == ["src-a"]

    runs = _job_runs(session)
    assert len(runs) == 1
    assert runs[0].retry_count == 1
    assert runs[0].status is JobStatus.SUCCESS
    assert runs[0].finished_at is not None


def test_stale_retrying_is_recovered_and_increments_retry_count(
    session: Session, make_source
) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    slot = resolve_slot(SLOT_A_MOMENT, 30)
    session.add(
        JobRun(
            job_type=JOB_TYPE,
            idempotency_key=slot.idempotency_key,
            scheduled_at=slot.start_at,
            started_at=SLOT_A_START - timedelta(hours=3),
            status=JobStatus.RETRYING,
            retry_count=2,
            error_message="上一次运行崩溃",
        )
    )
    session.flush()
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.executed is True
    assert result.reason == "stale_recovered"
    assert result.retry_count == 3
    assert calls == ["src-a"]
    runs = _job_runs(session)
    assert len(runs) == 1
    assert runs[0].retry_count == 3


def test_fresh_running_is_not_taken_over(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-a", collector="scheduler_counting")
    slot = resolve_slot(SLOT_A_MOMENT, 30)
    session.add(
        JobRun(
            job_type=JOB_TYPE,
            idempotency_key=slot.idempotency_key,
            scheduled_at=slot.start_at,
            started_at=SLOT_A_MOMENT - timedelta(minutes=10),
            status=JobStatus.RUNNING,
            retry_count=0,
        )
    )
    session.flush()
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.executed is False
    assert result.reason == "in_flight"
    assert result.retry_count == 0
    assert calls == []
    runs = _job_runs(session)
    assert len(runs) == 1
    assert runs[0].status is JobStatus.RUNNING


def test_sensitive_fields_never_reach_output_json(session: Session, make_source) -> None:
    _make_collector_source(
        make_source,
        "src-secret",
        collector="scheduler_failing",
        config={
            "api_key": "SUPERSECRET-KEY",
            "token": "SUPERSECRET-TOKEN",
            "authorization": "Bearer SUPERSECRET-BEARER",
        },
    )

    def factory(src):
        return _FailingCollector(src, error="auth failed token=SUPERSECRET-TOKEN")

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.status is JobStatus.FAILED
    job = _job_runs(session)[0]
    text = json.dumps(job.output_json, ensure_ascii=False) + (job.error_message or "")
    assert "SUPERSECRET" not in text
    assert "api_key" not in text
    assert "Authorization" not in text
    assert "token=***" in text


def test_no_enabled_sources_reports_success_with_warning(session: Session) -> None:
    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT))

    assert result.executed is True
    assert result.status is JobStatus.SUCCESS
    assert result.sources == ()

    job = _job_runs(session)[0]
    assert job.output_json is not None
    assert job.output_json["totals"]["sources"] == 0
    assert job.output_json["sources"] == []
    assert any("没有已启用" in warning for warning in job.output_json["warnings"])


def test_disabled_and_unconfigured_sources_are_excluded(
    session: Session, make_source
) -> None:
    _make_collector_source(
        make_source, "src-disabled", collector="scheduler_counting", enabled=False
    )
    make_source(
        name="src-no-collector",
        source_type=SourceType.NEWS,
        config_json={"note": "缺少 collector 配置"},
    )
    _make_collector_source(make_source, "src-active", collector="scheduler_counting")
    calls: list[str] = []

    def factory(src):
        return _CountingCollector(src, calls=calls)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert [summary.source_name for summary in result.sources] == ["src-active"]
    assert calls == ["src-active"]


def test_output_json_records_per_source_counters(session: Session, make_source) -> None:
    _make_collector_source(make_source, "src-persist", collector="scheduler_persisting")

    def factory(src):
        return _PersistingCollector(src)

    result = _run(session, clock=_FixedClock(SLOT_A_MOMENT), factory=factory)

    assert result.status is JobStatus.SUCCESS
    assert _raw_item_count(session) == 1

    job = _job_runs(session)[0]
    assert job.output_json is not None
    entry = job.output_json["sources"][0]
    assert set(entry) >= {
        "source",
        "status",
        "run_id",
        "fetched",
        "inserted",
        "duplicate",
        "failed",
        "skipped",
        "retry_count",
        "warnings",
    }
    assert entry["status"] == "SUCCESS"
    assert entry["fetched"] == 1
    assert entry["inserted"] == 1
    assert entry["run_id"]
    assert job.output_json["totals"]["inserted"] == 1
    assert job.output_json["scheduler"]["interval_minutes"] == 30
    assert job.output_json["scheduler"]["slot"]["idempotency_key"] == (
        resolve_slot(SLOT_A_MOMENT, 30).idempotency_key
    )




