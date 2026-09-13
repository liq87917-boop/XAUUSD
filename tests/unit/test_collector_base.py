"""``BaseCollector`` 单元测试（不触数据库、不触网络）。

覆盖：
- 抽象约束（必须声明 ``collector_name`` / ``source_type`` / 实现 ``_do_fetch``）；
- Transport 注入要求（缺失时给出可执行提示，绝不静默失败）；
- 数据契约校验（payload / window 的时间语义与幂等键）；
- 健康检查（不发网络请求、异常转"不健康"而不抛出）；
- 重试计数（策略默认 3 次 + 每次失败尝试计数）；
- ★ 硬性要求：``base.py`` 不得写死任何站点 URL（collect() 抽象层次必须高）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from inspect import getsourcefile
from pathlib import Path

import pytest

from database.models import Source
from database.models.enums import RawItemType, SourceType
from src.collectors.base import BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.transport import HttpRequest, HttpResponse, RetryPolicy
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload
from src.common.exceptions import TimeSemanticsError

pytestmark = pytest.mark.unit

BASE_SOURCE_FILE = Path(getsourcefile(BaseCollector) or "")
UTC_NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


class DummyCollector(BaseCollector):
    """最小可用子类：``_do_fetch`` 返回空页（仅用于测试框架能力）。"""

    collector_name = "dummy"
    source_type = SourceType.NEWS

    def __init__(self, source: Source, **kwargs: object) -> None:
        super().__init__(source, **kwargs)  # type: ignore[arg-type]
        self.fetch_calls: list[dict | None] = []

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        self.fetch_calls.append(cursor)
        return FetchPage(payloads=(), next_cursor=None)


def _source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "name": "unit-source",
        "source_type": SourceType.NEWS,
        "base_url": "https://unit.invalid",
        "enabled": True,
    }
    defaults.update(overrides)
    return Source(**defaults)  # type: ignore[arg-type]


def _window() -> CollectWindow:
    """默认采集窗口（当前测试用例按需自建窗口，保留以便后续用例复用）。"""
    return CollectWindow(start_at=UTC_NOW - timedelta(hours=1), end_at=UTC_NOW)


def test_default_window_bounds_are_ordered() -> None:
    """窗口工具函数的正例：end 必须晚于 start，且两者都是 timezone-aware。"""
    window = _window()
    assert window.end_utc > window.start_utc
    assert window.start_utc.tzinfo is not None
    assert window.end_utc.tzinfo is not None


# ---------------------------------------------------------------------------
# 硬性要求：抽象层次
# ---------------------------------------------------------------------------
def test_base_collector_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseCollector(_source())  # type: ignore[abstract]
    assert "_do_fetch" in BaseCollector.__abstractmethods__


def test_subclass_without_collector_name_is_rejected() -> None:
    with pytest.raises(TypeError, match="collector_name"):

        class Nameless(BaseCollector):
            source_type = SourceType.NEWS

            async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
                return FetchPage()


def test_subclass_without_source_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="source_type"):

        class Typeless(BaseCollector):
            collector_name = "typeless"

            async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
                return FetchPage()


def test_base_module_contains_no_hardcoded_site_urls() -> None:
    """★ collect() 必须与站点解耦：base.py 中不得出现任何具体站点 URL / 域名。"""
    source = BASE_SOURCE_FILE.read_text(encoding="utf-8")
    assert not re.search(r"https?://", source), "base.py 不得包含任何 URL"
    assert not re.search(r"\b[\w-]+\.(com|cn|org|net|io)\b", source), "base.py 不得包含站点域名"


# ---------------------------------------------------------------------------
# Transport 注入与重试计数
# ---------------------------------------------------------------------------
async def test_request_without_transport_raises_actionable_error() -> None:
    collector = DummyCollector(_source())

    with pytest.raises(CollectorError) as excinfo:
        await collector._request(HttpRequest(url="https://unit.invalid/feed"))

    message = str(excinfo.value)
    assert "Transport" in message
    assert "MockTransport" in message


async def test_request_counts_failed_attempts_as_retries(
    mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [HttpResponse(status=500), HttpResponse(status=500), HttpResponse(status=200)]
    )
    collector = DummyCollector(_source(), transport=transport, sleep=recording_sleep)

    response = await collector._request(HttpRequest(url="https://unit.invalid/feed"))

    assert response.status == 200
    assert collector.retry_count == 2
    assert len(collector.attempts) == 3
    assert recording_sleep.delays == [1.0, 2.0]


def test_default_retry_policy_is_three_attempts() -> None:
    collector = DummyCollector(_source())
    assert collector.retry_policy == RetryPolicy()
    assert collector.retry_policy.max_attempts == 3


def test_custom_retry_policy_is_honoured() -> None:
    policy = RetryPolicy(max_attempts=1, base_delay_seconds=0.5)
    collector = DummyCollector(_source(), retry_policy=policy)
    assert collector.retry_policy.max_attempts == 1


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
async def test_health_check_healthy_when_configured() -> None:
    collector = DummyCollector(_source())

    report = await collector.health_check()

    assert report.healthy is True
    assert report.collector_name == "dummy"
    assert report.details["base_url"] == "https://unit.invalid"


async def test_health_check_unhealthy_without_base_url() -> None:
    collector = DummyCollector(_source(base_url=None))

    report = await collector.health_check()

    assert report.healthy is False
    assert "base_url" in (report.message or "")


async def test_health_check_never_raises() -> None:
    class BrokenHealth(DummyCollector):
        collector_name = "broken-health"

        async def _do_health_check(self):  # noqa: ANN202
            raise RuntimeError("探针爆炸")

    collector = BrokenHealth(_source())
    report = await collector.health_check()

    assert report.healthy is False
    assert "RuntimeError" in (report.message or "")


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------
def test_payload_requires_source_record_id() -> None:
    with pytest.raises(ValueError, match="source_record_id"):
        RawItemPayload(source_record_id="   ", item_type=RawItemType.NEWS)


def test_payload_rejects_naive_published_at() -> None:
    with pytest.raises(TimeSemanticsError):
        RawItemPayload(
            source_record_id="p-1",
            item_type=RawItemType.NEWS,
            published_at=datetime(2026, 9, 12, 8, 0),
        )


def test_payload_rejects_malformed_content_hash() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        RawItemPayload(
            source_record_id="p-1", item_type=RawItemType.NEWS, content_hash="NOT-A-HASH"
        )


def test_payload_content_hash_ignores_whitespace_and_case() -> None:
    """幂等基础：内容等价的文本必须得到同一哈希。"""
    first = RawItemPayload(
        source_record_id="p-1",
        item_type=RawItemType.NEWS,
        title="Gold Outlook",
        content_text="做多",
    )
    second = RawItemPayload(
        source_record_id="p-2",
        item_type=RawItemType.NEWS,
        title="  gold   outlook ",
        content_text="做多",
    )
    assert first.resolved_content_hash() == second.resolved_content_hash()


def test_payload_effective_at_is_max_of_published_and_collected() -> None:
    collected = UTC_NOW
    published = UTC_NOW - timedelta(minutes=30)
    payload = RawItemPayload(
        source_record_id="p-1", item_type=RawItemType.NEWS, published_at=published
    )
    assert payload.resolve_effective_at(collected) == collected

    future_published = UTC_NOW + timedelta(minutes=5)
    later = RawItemPayload(
        source_record_id="p-2", item_type=RawItemType.NEWS, published_at=future_published
    )
    assert later.resolve_effective_at(collected) == future_published


def test_payload_without_published_at_falls_back_to_collected() -> None:
    payload = RawItemPayload(source_record_id="p-1", item_type=RawItemType.NEWS)
    assert payload.resolve_effective_at(UTC_NOW) == UTC_NOW


def test_window_requires_timezone_aware_bounds() -> None:
    with pytest.raises(TimeSemanticsError):
        CollectWindow(start_at=datetime(2026, 9, 12, 8, 0), end_at=UTC_NOW)


def test_window_rejects_reversed_range() -> None:
    with pytest.raises(TimeSemanticsError, match="采集窗口非法"):
        CollectWindow(start_at=UTC_NOW, end_at=UTC_NOW - timedelta(seconds=1))


def test_fetch_page_fetched_defaults_to_payload_count() -> None:
    payload = RawItemPayload(source_record_id="p-1", item_type=RawItemType.NEWS)
    assert FetchPage(payloads=(payload,)).fetched == 1
    assert FetchPage(payloads=(payload,), raw_count=7).fetched == 7

