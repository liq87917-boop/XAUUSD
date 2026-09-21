"""采集器故障隔离 + 健康摘要测试（SQLite，100% Mock）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from database.models.enums import SourceType
from src.collectors.base import BaseCollector
from src.collectors.runner import render_run_summary, run_collectors
from src.collectors.types import CollectWindow, FetchPage

pytestmark = pytest.mark.integration

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 2, tzinfo=UTC)
)


class _CrashingCollector(BaseCollector):
    collector_name = "crashing"
    source_type = SourceType.NEWS

    async def _do_fetch(self, cursor, window):
        raise RuntimeError("boom")


class _OkCollector(BaseCollector):
    collector_name = "ok"
    source_type = SourceType.NEWS

    async def _do_fetch(self, cursor, window):
        return FetchPage(next_cursor=None)


def test_single_collector_crash_does_not_block_others(session: Session, make_source) -> None:
    """provider A crash 不影响 provider B。"""
    crashing_source = make_source(name="crashing", source_type=SourceType.NEWS)
    ok_source = make_source(name="ok", source_type=SourceType.NEWS)
    results = asyncio.run(
        run_collectors(
            session,
            [_CrashingCollector(crashing_source), _OkCollector(ok_source)],
            window=WINDOW,
            resume=False,
        )
    )
    assert len(results) == 2
    assert results[0].status.value == "FAILED"
    assert results[0].error_message is not None
    assert results[1].status.value == "SUCCESS"


def test_render_run_summary_lists_each_provider(session: Session, make_source) -> None:
    crashing_source = make_source(name="crashing", source_type=SourceType.NEWS)
    ok_source = make_source(name="ok", source_type=SourceType.NEWS)
    results = asyncio.run(
        run_collectors(
            session,
            [_CrashingCollector(crashing_source), _OkCollector(ok_source)],
            window=WINDOW,
            resume=False,
        )
    )
    summary = render_run_summary(results)
    assert "Collector Run" in summary
    assert "crashing" in summary and "FAILED" in summary
    assert "ok" in summary and "SUCCESS" in summary
    assert "Total:" in summary and "failed" in summary
