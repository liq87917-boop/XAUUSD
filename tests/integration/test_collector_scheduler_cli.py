"""``run_collector_scheduler`` CLI 集成测试（SQLite，100% Mock，零网络）。

验证 CLI 单轮入口与安全循环入口的职责边界：
- ``--once`` 只执行当前槽一次；全源失败时返回非 0；
- 常驻循环按槽推进，收到 ``KeyboardInterrupt``（Ctrl+C）安全退出且不重复执行同槽。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from database.models import JobRun, Source
from database.models.enums import JobStatus, SourceType
from database.session import session_scope
from scripts.run_collector_scheduler import main
from src.collectors.base import BaseCollector
from src.collectors.types import FetchPage

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 7, 30, tzinfo=UTC)
LATE_MOMENT = datetime(2026, 9, 22, 12, 59, 0, tzinfo=UTC)


class _Clock:
    """可推进的假时钟（CLI 循环测试用）。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class _CliCollector(BaseCollector):
    collector_name = "cli_counting"
    source_type = SourceType.NEWS

    def __init__(self, source: Source, *, calls: list[str], fail: bool = False, **kwargs: object):
        super().__init__(source, **kwargs)  # type: ignore[arg-type]
        self._calls = calls
        self._fail = fail

    async def _do_fetch(self, cursor: dict | None, window) -> FetchPage:
        self._calls.append(self.source.name)
        if self._fail:
            raise RuntimeError("provider down")
        return FetchPage(next_cursor=None)


def _seed_source(factory: sessionmaker, name: str, *, enabled: bool = True) -> None:
    with session_scope(factory) as session:
        session.add(
            Source(
                name=name,
                source_type=SourceType.NEWS,
                enabled=enabled,
                config_json={"collector": "cli_counting"},
            )
        )


def _job_runs(factory: sessionmaker) -> list[JobRun]:
    with factory() as session:
        return list(session.scalars(sa.select(JobRun).order_by(JobRun.scheduled_at)))


def test_once_executes_single_round(session_factory: sessionmaker) -> None:
    _seed_source(session_factory, "cli-a")
    calls: list[str] = []

    def factory(source):
        return _CliCollector(source, calls=calls)

    code = main(
        ["--once"],
        session_factory=session_factory,
        clock=_Clock(MOMENT),
        collector_factory=factory,
    )

    assert code == 0
    assert calls == ["cli-a"]
    runs = _job_runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status is JobStatus.SUCCESS
    assert runs[0].idempotency_key == "collector_scheduler:30m:20260922T120000Z"


def test_once_returns_nonzero_when_all_sources_fail(session_factory: sessionmaker) -> None:
    _seed_source(session_factory, "cli-bad")
    calls: list[str] = []

    def factory(source):
        return _CliCollector(source, calls=calls, fail=True)

    code = main(
        ["--once"],
        session_factory=session_factory,
        clock=_Clock(MOMENT),
        collector_factory=factory,
    )

    assert code == 1
    runs = _job_runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status is JobStatus.FAILED


def test_once_is_idempotent_within_same_slot(session_factory: sessionmaker) -> None:
    _seed_source(session_factory, "cli-a")
    calls: list[str] = []

    def factory(source):
        return _CliCollector(source, calls=calls)

    clock = _Clock(MOMENT)
    first = main(
        ["--once"], session_factory=session_factory, clock=clock, collector_factory=factory
    )
    second = main(
        ["--once"], session_factory=session_factory, clock=clock, collector_factory=factory
    )

    assert first == 0
    assert second == 0
    assert calls == ["cli-a"]
    assert len(_job_runs(session_factory)) == 1


def test_interval_minutes_controls_slot_grid(session_factory: sessionmaker) -> None:
    _seed_source(session_factory, "cli-a")
    calls: list[str] = []

    def factory(source):
        return _CliCollector(source, calls=calls)

    code = main(
        ["--once", "--interval-minutes", "60"],
        session_factory=session_factory,
        clock=_Clock(LATE_MOMENT),
        collector_factory=factory,
    )

    assert code == 0
    runs = _job_runs(session_factory)
    assert len(runs) == 1
    assert runs[0].idempotency_key == "collector_scheduler:60m:20260922T120000Z"


def test_loop_advances_slots_and_exits_on_keyboard_interrupt(
    session_factory: sessionmaker,
) -> None:
    _seed_source(session_factory, "cli-a")
    calls: list[str] = []

    def factory(source):
        return _CliCollector(source, calls=calls)

    clock = _Clock(MOMENT)
    sleeps: list[float] = []

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:  # 第二轮睡眠时模拟用户 Ctrl+C
            raise KeyboardInterrupt
        clock.moment = clock.moment + timedelta(seconds=delay)

    code = main(
        [],
        session_factory=session_factory,
        clock=clock,
        sleeper=sleeper,
        collector_factory=factory,
    )

    assert code == 0
    assert len(sleeps) == 2
    assert calls == ["cli-a", "cli-a"]
    runs = _job_runs(session_factory)
    assert len(runs) == 2
    assert {run.idempotency_key for run in runs} == {
        "collector_scheduler:30m:20260922T120000Z",
        "collector_scheduler:30m:20260922T123000Z",
    }
