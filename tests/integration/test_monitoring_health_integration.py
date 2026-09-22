"""Collector 健康度 + Phase 3.3 资格观测集成测试（GOLD-004，SQLite，零网络）。

覆盖：
- 空库 / 从未成功 / 连败 / 陈旧 / 只有在途 / 禁用 等边界状态；
- 窗口边界（恰好落在窗口起点计入、更早排除）；
- 幂等重复运行（inserted=0 / duplicate>0）不被误判为失败；
- Processor 未观测（有原始数据但无加工结果）必须降级；
- 调度槽汇总（含 ``output_json`` 不可解析的槽）；
- 端到端脱敏（错误文本 / 来源配置 / URL query 都不出现在 JSON 与 Markdown）；
- CLI：``--json`` 稳定结构 + ``--report`` 仅在 ``--no-dry-run`` 时落盘。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from database.models import CollectorRun, JobRun, ProcessedItem, RawItem, Source
from database.models.enums import (
    CollectorRunStatus,
    JobStatus,
    ProcessStatus,
    RawItemType,
    SourceType,
)
from database.session import session_scope
from scripts.report_collector_health import main
from src.monitoring import (
    PHASE3_3_BLOCKER_CODE,
    CheckStatus,
    HealthState,
    ProcessorState,
    load_health_report,
    load_monitoring_report,
)
from src.scheduler.core import JOB_TYPE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
WINDOW_HOURS = 24
SECRET = "SECRETVALUE-9f1c"


def _source(
    session: Session,
    name: str,
    *,
    enabled: bool = True,
    config: dict | None = None,
    base_url: str | None = None,
) -> Source:
    source = Source(
        name=name,
        source_type=SourceType.NEWS,
        enabled=enabled,
        config_json={"collector": "probe_collector", **(config or {})},
        base_url=base_url,
    )
    session.add(source)
    session.flush()
    return source


def _run(
    session: Session,
    source: Source | None,
    *,
    status: CollectorRunStatus,
    started_at: datetime,
    inserted: int = 0,
    duplicate: int = 0,
    error: str | None = None,
) -> CollectorRun:
    run = CollectorRun(
        collector_name="probe_collector",
        source_id=None if source is None else source.id,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        status=status,
        inserted_count=inserted,
        duplicate_count=duplicate,
        error_message=error,
    )
    session.add(run)
    session.flush()
    return run


def _raw(
    session: Session,
    source: Source,
    *,
    collected_at: datetime,
    item_type: RawItemType = RawItemType.NEWS,
) -> RawItem:
    record_id = uuid.uuid4().hex
    item = RawItem(
        source_id=source.id,
        source_record_id=record_id,
        item_type=item_type,
        title="gold",
        content_text="XAUUSD",
        raw_json={"id": record_id},
        content_hash=record_id.ljust(64, "0"),
        published_at=None,
        collected_at=collected_at,
        effective_at=collected_at,
    )
    session.add(item)
    session.flush()
    return item


def _processed(
    session: Session, raw: RawItem, *, status: ProcessStatus, created_at: datetime
) -> ProcessedItem:
    item = ProcessedItem(
        raw_item_id=raw.id,
        processor_name="collection_normalizer",
        processor_version="collection-normalizer-v1",
        status=status,
        effective_at=raw.effective_at,
        created_at=created_at,
    )
    session.add(item)
    session.flush()
    return item


def _job(
    session: Session,
    *,
    scheduled_at: datetime,
    status: JobStatus,
    output: dict | None = None,
    retry_count: int = 0,
) -> JobRun:
    job = JobRun(
        job_type=JOB_TYPE,
        idempotency_key=f"collector_scheduler:30m:{uuid.uuid4().hex}",
        scheduled_at=scheduled_at,
        started_at=scheduled_at,
        finished_at=scheduled_at + timedelta(minutes=1),
        status=status,
        retry_count=retry_count,
        output_json=output,
    )
    session.add(job)
    session.flush()
    return job


def _source_by_name(report, name: str):
    return next(item for item in report.sources if item.source_name == name)


def test_empty_database_is_explicitly_not_healthy(session: Session) -> None:
    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    assert report.state is HealthState.NO_SOURCES
    assert report.healthy is False
    assert report.sources == ()
    assert report.totals["sources"] == 0
    assert report.scheduler.slots == 0
    assert report.processor.state is ProcessorState.NO_INPUT
    assert report.unattributed_runs == 0


def test_window_boundary_and_counters(session: Session) -> None:
    source = _source(session, "boundary")
    inside = MOMENT - timedelta(hours=1)
    edge = MOMENT - timedelta(hours=WINDOW_HOURS)  # 恰好落在窗口起点 → 计入
    before = edge - timedelta(seconds=1)  # 窗口之外 → 排除
    _run(
        session,
        source,
        status=CollectorRunStatus.SUCCESS,
        started_at=inside,
        inserted=2,
        duplicate=3,
    )
    _run(session, source, status=CollectorRunStatus.SUCCESS, started_at=edge, inserted=1)
    _run(session, source, status=CollectorRunStatus.FAILED, started_at=before, error="boom")

    raw_success = _raw(session, source, collected_at=inside)
    raw_rejected = _raw(session, source, collected_at=inside)
    raw_failed = _raw(session, source, collected_at=inside)
    _raw(session, source, collected_at=before)  # 窗口外原始数据 → 不计入
    _processed(session, raw_success, status=ProcessStatus.SUCCESS, created_at=inside)
    _processed(session, raw_rejected, status=ProcessStatus.SKIPPED, created_at=inside)
    _processed(session, raw_failed, status=ProcessStatus.FAILED, created_at=inside)

    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    item = _source_by_name(report, "boundary")
    assert item.runs == 2
    assert item.succeeded == 2
    assert item.failed == 0  # 窗口外的失败不计入
    assert item.failure_streak == 0
    assert item.inserted_reported == 3
    assert item.duplicate_reported == 3
    assert item.raw_items_observed == 3
    assert item.processed.success == 1
    assert item.processed.rejected == 1
    assert item.processed.failed == 1
    assert item.state is HealthState.HEALTHY
    assert item.healthy is True
    assert item.last_run_at == inside
    assert item.last_success_at == inside + timedelta(minutes=1)
    assert report.processor.state is ProcessorState.OBSERVED
    assert report.totals["processed_rejected"] == 1
    assert report.totals["raw_items_observed"] == 3


def test_failed_streak_partial_only_and_in_flight_are_not_healthy(session: Session) -> None:
    streak = _source(session, "streak")
    for minutes in (10, 20, 30):
        _run(
            session,
            streak,
            status=CollectorRunStatus.FAILED,
            started_at=MOMENT - timedelta(minutes=minutes),
            error="provider down",
        )
    partial_only = _source(session, "partial-only")
    _run(
        session,
        partial_only,
        status=CollectorRunStatus.PARTIAL_FAILED,
        started_at=MOMENT - timedelta(minutes=15),
    )
    running = _source(session, "running-only")
    _run(
        session,
        running,
        status=CollectorRunStatus.RUNNING,
        started_at=MOMENT - timedelta(minutes=5),
    )

    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    streak_item = _source_by_name(report, "streak")
    assert streak_item.state is HealthState.FAILED
    assert streak_item.failure_streak == 3
    assert streak_item.healthy is False
    assert "连续硬失败" in streak_item.reason
    partial_item = _source_by_name(report, "partial-only")
    assert partial_item.state is HealthState.NEVER_SUCCEEDED
    assert partial_item.healthy is False
    running_item = _source_by_name(report, "running-only")
    assert running_item.state is HealthState.NEVER_SUCCEEDED
    assert running_item.in_flight == 1
    assert report.state is HealthState.FAILED


def test_stale_never_run_and_disabled_states(session: Session) -> None:
    stale = _source(session, "stale")
    _run(
        session,
        stale,
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(days=5),
    )
    _source(session, "never-run")
    disabled = _source(session, "disabled", enabled=False)
    _run(
        session,
        disabled,
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(minutes=30),
    )

    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    stale_item = _source_by_name(report, "stale")
    assert stale_item.state is HealthState.STALE
    assert stale_item.stale is True
    assert stale_item.healthy is False
    never_item = _source_by_name(report, "never-run")
    assert never_item.state is HealthState.NEVER_RUN
    assert never_item.runs == 0
    assert never_item.last_success_at is None
    disabled_item = _source_by_name(report, "disabled")
    assert disabled_item.state is HealthState.DISABLED
    assert disabled_item.healthy is False
    assert report.state is HealthState.DEGRADED  # 存在非健康来源 → 整体不健康


def test_idempotent_repeat_run_is_healthy_and_processor_not_observed_downgrades(
    session: Session,
) -> None:
    idempotent = _source(session, "idempotent")
    _run(
        session,
        idempotent,
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(minutes=8),
        inserted=0,
        duplicate=5,
    )
    idempotent_report = load_health_report(session, as_of=MOMENT)
    idempotent_item = _source_by_name(idempotent_report, "idempotent")
    assert idempotent_item.state is HealthState.HEALTHY
    assert idempotent_item.duplicate_reported == 5
    assert idempotent_item.inserted_reported == 0
    assert idempotent_item.processor_state is ProcessorState.NO_INPUT

    unprocessed = _source(session, "unprocessed")
    _run(
        session,
        unprocessed,
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(minutes=7),
        inserted=1,
    )
    _raw(session, unprocessed, collected_at=MOMENT - timedelta(minutes=6))

    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    unprocessed_item = _source_by_name(report, "unprocessed")
    assert unprocessed_item.processor_state is ProcessorState.NOT_OBSERVED
    assert unprocessed_item.processed.total == 0
    # ★ 有原始数据但无加工结果 → 整体不得报 healthy
    assert report.processor.state is ProcessorState.NOT_OBSERVED
    assert report.state is HealthState.DEGRADED
    assert report.healthy is False
    assert "加工" in report.reason


def test_scheduler_summary_counts_slots_and_unparsable_output(session: Session) -> None:
    good_output = {
        "sources": [
            {"source": "a", "status": "SUCCESS", "inserted": 1},
            {"source": "b", "status": "FAILED", "inserted": 0},
        ]
    }
    _job(
        session,
        scheduled_at=MOMENT - timedelta(minutes=30),
        status=JobStatus.SUCCESS,
        output=good_output,
        retry_count=2,
    )
    _job(
        session,
        scheduled_at=MOMENT - timedelta(minutes=60),
        status=JobStatus.FAILED,
        output=None,
    )
    _run(
        session,
        None,  # source_id 为空：无法归因的运行
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(minutes=5),
    )

    report = load_health_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    assert report.scheduler.slots == 2
    assert report.scheduler.success == 1
    assert report.scheduler.failed == 1
    assert report.scheduler.retries == 2
    assert report.scheduler.per_source_entries == 2
    assert report.scheduler.slots_without_output == 1
    assert report.unattributed_runs == 1
    payload = report.to_dict()
    assert payload["scheduler"]["per_source_entries"] == 2
    assert payload["scheduler"]["last_slot_at"] == (MOMENT - timedelta(minutes=30)).isoformat()


def test_report_never_leaks_credentials_or_source_config(session: Session) -> None:
    source = _source(
        session,
        "secret-source",
        config={
            "api_key": SECRET,
            "token": SECRET,
            "authorization": f"Bearer {SECRET}",
        },
        base_url=f"https://feed.invalid/rss.xml?api_key={SECRET}",
    )
    _run(
        session,
        source,
        status=CollectorRunStatus.FAILED,
        started_at=MOMENT - timedelta(minutes=10),
        error=f"RuntimeError: api_key={SECRET}",
    )
    _raw(session, source, collected_at=MOMENT - timedelta(minutes=5))

    report = load_monitoring_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    text = report.render()
    for rendered in (payload, text):
        assert SECRET not in rendered
        assert "Bearer" not in rendered  # 完整来源配置 / 凭据值从不输出
        assert "feed.invalid/rss.xml?api_key" not in rendered  # query 必须剥离
    # 错误文本经擦除后仍保留可审计信息
    item = _source_by_name(report.health, "secret-source")
    assert item.last_error is not None
    assert "api_key=***" in item.last_error
    assert item.base_url == "https://feed.invalid/rss.xml"


def test_qualification_keeps_blocker_and_reports_evidence_windows(session: Session) -> None:
    source = _source(session, "news-source")
    _raw(session, source, collected_at=MOMENT - timedelta(days=2))
    author_post_at = MOMENT - timedelta(days=1)
    _raw(session, source, collected_at=author_post_at, item_type=RawItemType.POST)
    _run(
        session,
        source,
        status=CollectorRunStatus.SUCCESS,
        started_at=MOMENT - timedelta(minutes=5),
    )

    report = load_monitoring_report(session, as_of=MOMENT, window_hours=WINDOW_HOURS)
    qualification = report.qualification
    assert qualification.blocker_code == PHASE3_3_BLOCKER_CODE
    assert qualification.blocker_active is True
    assert qualification.ready is False
    checks = {item.key: item for item in qualification.checks}
    # 有原始新闻数据但没有可审计的 news_events 解析行 → 不得判 PASS
    assert checks["news.events"].current == 0
    assert checks["news.events"].status is CheckStatus.BLOCKED
    assert checks["news.max_source_share"].status is CheckStatus.BLOCKED
    assert checks["news.available_history_evidence"].status is CheckStatus.BLOCKED
    assert checks["author.source_authorization"].status is CheckStatus.BLOCKED
    assert checks["author.trusted_posts_per_account"].status is CheckStatus.BLOCKED
    payload = report.to_dict()
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["qualification"]["evidence_windows"]["author"]["start_at"] == (
        author_post_at.isoformat()
    )
    assert payload["qualification"]["evidence_windows"]["news"]["start_at"] is None
    assert "PHASE3_3_DATA" in report.render()


def test_cli_json_dry_run_and_report_file(
    session_factory: sessionmaker[Session], tmp_path, capsys
) -> None:
    with session_scope(session_factory) as session:
        source = _source(session, "cli-source")
        _run(
            session,
            source,
            status=CollectorRunStatus.SUCCESS,
            started_at=MOMENT - timedelta(minutes=5),
            inserted=1,
        )

    code = main(
        ["--json", "--as-of", MOMENT.isoformat(), "--window-hours", str(WINDOW_HOURS)],
        session_factory=session_factory,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["health"]["sources"][0]["source"] == "cli-source"
    assert payload["health"]["sources"][0]["state"] == HealthState.HEALTHY.value

    report_path = tmp_path / "monitoring.md"
    dry_code = main(
        ["--as-of", MOMENT.isoformat(), "--report", str(report_path)],
        session_factory=session_factory,
    )
    assert dry_code == 0
    assert "dry-run" in capsys.readouterr().out
    assert not report_path.exists()  # 默认绝不落盘

    write_code = main(
        [
            "--as-of",
            MOMENT.isoformat(),
            "--report",
            str(report_path),
            "--no-dry-run",
            "--hf-weak-supervision-rows",
            "150",
        ],
        session_factory=session_factory,
    )
    assert write_code == 0
    assert report_path.exists()
    written = report_path.read_text(encoding="utf-8")
    assert PHASE3_3_BLOCKER_CODE in written
    assert "cli-source" in written
    assert "HF 标题弱监督行数" in written


def test_cli_rejects_invalid_arguments(
    session_factory: sessionmaker[Session], tmp_path, capsys
) -> None:
    with pytest.raises(SystemExit) as negative_window:
        main(["--window-hours", "0"], session_factory=session_factory)
    assert negative_window.value.code == 2

    with pytest.raises(SystemExit) as naive_as_of:
        main(["--as-of", "2026-09-22T12:00:00"], session_factory=session_factory)
    assert naive_as_of.value.code == 2

    with pytest.raises(SystemExit) as bad_stale:
        main(["--stale-after-minutes", "-1"], session_factory=session_factory)
    assert bad_stale.value.code == 2
    capsys.readouterr()


def test_loader_validates_window_and_awareness(session: Session) -> None:
    with pytest.raises(ValueError, match="window_hours"):
        load_health_report(session, window_hours=0, as_of=MOMENT)
    with pytest.raises(ValueError, match="stale_after"):
        load_health_report(
            session, as_of=MOMENT, stale_after=timedelta(0)
        )
    with pytest.raises(ValueError, match="必须包含时区"):
        load_health_report(session, as_of=datetime(2026, 9, 22, 12, 0))
    with pytest.raises(ValueError, match="必须包含时区"):
        load_monitoring_report(session, as_of=datetime(2026, 9, 22, 12, 0))
