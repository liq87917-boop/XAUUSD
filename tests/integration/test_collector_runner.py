"""采集器运行编排集成测试（覆盖四类强制场景）。

★ 全部使用 Mock 传输层：**不访问任何外部网络**（不爬微博、不依赖外网可用性）★

强制场景：
1. 失败重试：网络超时 / 429 限流 / 500 服务端错误 → 3 次重试机制正确触发；
2. 幂等：同一条帖子采集两次 → ``content_hash`` 判重，只落库 1 次；
3. 断点续采：采集中断后，下次运行从 ``collector_runs.cursor_json`` 继续；
4. 失败隔离：单个采集器失败不影响其他采集器，且每轮都有 ``collector_runs`` 记录。

数据库：本地 / CI 使用 SQLite（仅测试）；生产 / 研究环境必须 PostgreSQL。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import CollectorRun, RawItem, RawMedia
from database.models.enums import CollectorRunStatus, MediaType, RawItemType, SourceType
from src.collectors import (
    BaseCollector,
    CollectWindow,
    FetchPage,
    HttpRequest,
    HttpResponse,
    MediaPayload,
    RawItemPayload,
    TransportTimeoutError,
    collector_for_source,
    run_collector,
    run_collectors,
)
from src.collectors.errors import CollectorNotRegisteredError
from src.common.time import parse_iso8601, utc_now

pytestmark = pytest.mark.integration

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 12, 0, 0, tzinfo=UTC),
    end_at=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
)


def _feed(items: list[dict[str, Any]], next_cursor: dict[str, Any] | None = None) -> HttpResponse:
    """构造采集器期望的 JSON 响应（Mock 传输层使用，不涉及真实接口）。"""
    return HttpResponse(status=200, json_body={"items": items, "next_cursor": next_cursor})


def _post(record_id: str, *, text: str = "黄金短线偏多", minutes_ago: int = 30) -> dict[str, Any]:
    published = WINDOW.end_utc - timedelta(minutes=minutes_ago)
    return {
        "id": record_id,
        "title": "gold note",
        "text": text,
        "published_at": published.isoformat(),
        "url": f"https://example.invalid/{record_id}",
    }


def _payload(record_id: str, *, text: str | None = None) -> RawItemPayload:
    """构造 payload。

    默认正文随 ``record_id`` 变化，避免不同记录之间"意外同内容判重"；
    需要验证 content_hash 判重的用例请显式传入相同的 ``text``。
    """
    body = text if text is not None else f"内容-{record_id}"
    return RawItemPayload(
        source_record_id=record_id,
        item_type=RawItemType.NEWS,
        title="gold note",
        content_text=body,
        published_at=WINDOW.end_utc - timedelta(minutes=30),
        raw_json={"id": record_id, "text": body},
    )


def _raw_item_count(session: Session) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(RawItem)) or 0


class ScriptedCollector(BaseCollector):
    """测试替身：``_do_fetch`` 按脚本返回页面或抛出异常（框架行为验证）。"""

    collector_name = "scripted_collector"
    source_type = SourceType.NEWS

    def __init__(self, source, *, script, seen_cursors=None, **kwargs) -> None:  # noqa: ANN001
        super().__init__(source, **kwargs)
        self._script: list[Any] = list(script)
        self.seen_cursors: list[dict[str, Any] | None] = (
            seen_cursors if seen_cursors is not None else []
        )

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        self.seen_cursors.append(cursor)
        if not self._script:
            raise AssertionError("ScriptedCollector 脚本已耗尽")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class HttpCollector(BaseCollector):
    """通过 ``self._request()`` 走真实重试链路的采集器（传输层注入 Mock）。"""

    collector_name = "http_collector"
    source_type = SourceType.NEWS

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        token = (cursor or {}).get("page_token")
        response = await self._request(
            HttpRequest(url=f"{self.source.base_url}/feed", params={"token": token})
        )
        body = response.json_body if isinstance(response.json_body, dict) else {}
        payloads = tuple(
            RawItemPayload(
                source_record_id=str(item["id"]),
                item_type=RawItemType.NEWS,
                title=item.get("title"),
                content_text=item.get("text"),
                raw_json=item,
                source_url=item.get("url"),
                published_at=parse_iso8601(item["published_at"]),
            )
            for item in body.get("items", [])
        )
        return FetchPage(payloads=payloads, next_cursor=body.get("next_cursor"))


@pytest.fixture()
def news_source(make_source):
    return make_source(
        name="news-source",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        enabled=True,
    )


# ---------------------------------------------------------------------------
# 强制场景 1：失败重试（网络超时 / 429 限流 / 500 服务端错误）
# ---------------------------------------------------------------------------
async def test_network_timeout_retries_three_times_then_failed(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    """网络超时：必须正好重试 3 次（含首次），退避 1s/2s，最终 FAILED 并留记录。"""
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)
    collector = HttpCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert transport.calls == 3
    assert [round(delay, 2) for delay in recording_sleep.delays] == [1.0, 2.0]
    assert "超时" in (result.error_message or "")

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.status is CollectorRunStatus.FAILED
    assert run.fetched_count == 0
    assert run.inserted_count == 0
    assert run.error_message is not None
    assert _raw_item_count(session) == 0


async def test_rate_limit_429_is_retried_then_run_succeeds(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    """429 限流：重试后成功；第一次遵守 Retry-After，其后走指数退避。"""
    transport = mock_transport(
        [
            HttpResponse(status=429, headers={"Retry-After": "2"}),
            HttpResponse(status=429),
            _feed([_post("p-1")]),
        ]
    )
    collector = HttpCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 2
    assert transport.calls == 3
    assert recording_sleep.delays == [2.0, 2.0]
    assert result.outcome is not None
    assert result.outcome.inserted_count == 1
    assert _raw_item_count(session) == 1


async def test_rate_limit_exhausted_is_marked_failed(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([HttpResponse(status=429)] * 3)
    collector = HttpCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert transport.calls == 3
    assert "429" in (result.error_message or "")


async def test_server_error_500_retries_three_times_then_failed(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    """500 服务端错误：同样触发 3 次重试并最终 FAILED。"""
    transport = mock_transport([HttpResponse(status=500)] * 3)
    collector = HttpCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert transport.calls == 3
    assert "500" in (result.error_message or "")

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.status is CollectorRunStatus.FAILED


# ---------------------------------------------------------------------------
# 强制场景 2：幂等（同一条帖子采集两次只落库 1 次）
# ---------------------------------------------------------------------------
async def test_same_post_collected_twice_is_stored_once(
    session: Session, news_source, mock_transport
) -> None:
    """同一帖子（相同 source_record_id）被采集两次 → 只落库 1 条。"""
    transport = mock_transport([_feed([_post("p-1")]), _feed([_post("p-1")])])

    first = await run_collector(
        session, HttpCollector(news_source, transport=transport), window=WINDOW
    )
    second = await run_collector(
        session, HttpCollector(news_source, transport=transport), window=WINDOW
    )

    assert first.outcome is not None
    assert first.outcome.inserted_count == 1
    assert first.outcome.duplicate_count == 0

    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 1

    assert _raw_item_count(session) == 1

    run = session.get(CollectorRun, second.run_id)
    assert run is not None
    assert run.duplicate_count == 1


async def test_same_content_with_new_platform_id_is_deduped_by_content_hash(
    session: Session, news_source, mock_transport
) -> None:
    """★ content_hash 判重：平台重新分配记录 ID、正文只差空白 → 仍然只落库 1 条。"""
    transport = mock_transport(
        [
            _feed([_post("p-1", text="黄金 2400 上方偏多")]),
            _feed([_post("p-1-republished", text="  黄金   2400 上方偏多 ")]),
        ]
    )

    first = await run_collector(
        session, HttpCollector(news_source, transport=transport), window=WINDOW
    )
    second = await run_collector(
        session, HttpCollector(news_source, transport=transport), window=WINDOW
    )

    assert first.outcome is not None
    assert first.outcome.inserted_count == 1
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 1
    assert _raw_item_count(session) == 1

    hashes = set(session.scalars(sa.select(RawItem.content_hash)).all())
    assert len(hashes) == 1


async def test_same_content_from_different_sources_is_not_deduped(
    session: Session, make_source
) -> None:
    """去重只在**同一数据源**内生效：不同来源的相同文本是两条独立记录。"""
    source_a = make_source(
        name="src-a", source_type=SourceType.NEWS, base_url="https://a.invalid"
    )
    source_b = make_source(
        name="src-b", source_type=SourceType.NEWS, base_url="https://b.invalid"
    )
    payload = _payload("same-text", text="完全相同的正文")

    results = await run_collectors(
        session,
        [
            ScriptedCollector(source_a, script=[FetchPage(payloads=(payload,))]),
            ScriptedCollector(source_b, script=[FetchPage(payloads=(payload,))]),
        ],
        window=WINDOW,
    )

    assert [result.status for result in results] == [
        CollectorRunStatus.SUCCESS,
        CollectorRunStatus.SUCCESS,
    ]
    assert _raw_item_count(session) == 2


# ---------------------------------------------------------------------------
# 统计与 collector_runs 落库
# ---------------------------------------------------------------------------
async def test_run_row_records_stats_and_timestamps(session: Session, news_source) -> None:
    """统计字段与时间必须落库到 collector_runs（04 §7）。"""
    page = FetchPage(
        payloads=(_payload("p-1", text="同一条内容"), _payload("p-2", text="同一条内容")),
        next_cursor=None,
    )
    collector = ScriptedCollector(news_source, script=[page])

    result = await run_collector(session, collector, window=WINDOW)

    outcome = result.outcome
    assert outcome is not None
    assert outcome.fetched_count == 2
    assert outcome.inserted_count == 1
    assert outcome.duplicate_count == 1
    assert outcome.failed_count == 0
    assert outcome.pages_fetched == 1
    assert outcome.status is CollectorRunStatus.SUCCESS

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.fetched_count == 2
    assert run.inserted_count == 1
    assert run.duplicate_count == 1
    assert run.failed_count == 0
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert run.cursor_json is None  # 采集到底，无待续游标
    assert _raw_item_count(session) == 1


async def test_media_payloads_are_persisted_with_deduplication(
    session: Session, news_source
) -> None:
    """媒体元数据写入 raw_media，并按 sha256 去重（04 §6）。"""
    digest = "a" * 64
    payload = RawItemPayload(
        source_record_id="p-media",
        item_type=RawItemType.NEWS,
        title="chart",
        content_text="K 线截图",
        published_at=WINDOW.end_utc - timedelta(minutes=10),
        raw_json={"id": "p-media"},
        media=(
            MediaPayload(
                MediaType.IMAGE, original_url="https://img.invalid/a.png", sha256=digest
            ),
            MediaPayload(
                MediaType.IMAGE, original_url="https://img.invalid/a-copy.png", sha256=digest
            ),
        ),
    )
    collector = ScriptedCollector(news_source, script=[FetchPage(payloads=(payload,))])

    await run_collector(session, collector, window=WINDOW)

    media_count = session.scalar(sa.select(sa.func.count()).select_from(RawMedia)) or 0
    assert media_count == 1


# ---------------------------------------------------------------------------
# 强制场景 3：游标状态断点续采
# ---------------------------------------------------------------------------
async def test_cursor_resume_continues_after_interruption(session: Session, news_source) -> None:
    """采集中断后，下一轮必须从上次的 ``cursor_json`` 继续（不重复、不遗漏）。"""
    first_seen: list[dict | None] = []
    first_collector = ScriptedCollector(
        news_source,
        script=[
            FetchPage(payloads=(_payload("p-1"),), next_cursor={"page_token": "t2"}),
            TransportTimeoutError("第二页超时"),
        ],
        seen_cursors=first_seen,
    )

    first = await run_collector(session, first_collector, window=WINDOW)

    assert first.status is CollectorRunStatus.PARTIAL_FAILED
    assert first.outcome is not None
    assert first.outcome.cursor == {"page_token": "t2"}
    assert first.outcome.pages_fetched == 1

    run_one = session.get(CollectorRun, first.run_id)
    assert run_one is not None
    assert run_one.cursor_json == {"page_token": "t2"}
    assert _raw_item_count(session) == 1

    resumed_seen: list[dict | None] = []
    resumed = await run_collector(
        session,
        ScriptedCollector(
            news_source,
            script=[FetchPage(payloads=(_payload("p-2"),), next_cursor=None)],
            seen_cursors=resumed_seen,
        ),
        window=WINDOW,
    )

    assert resumed.status is CollectorRunStatus.SUCCESS
    assert resumed.resumed_from_cursor is True
    assert resumed_seen[0] == {"page_token": "t2"}  # ★ 从断点开始，而不是从头
    assert _raw_item_count(session) == 2


async def test_resume_disabled_starts_from_scratch(session: Session, news_source) -> None:
    seen: list[dict | None] = []
    collector = ScriptedCollector(news_source, script=[FetchPage()], seen_cursors=seen)

    await run_collector(session, collector, window=WINDOW, resume=False)

    assert seen == [None]


async def test_multi_page_run_aggregates_and_clears_cursor(session: Session, news_source) -> None:
    script = [
        FetchPage(payloads=(_payload("p-1"),), next_cursor={"page_token": "t2"}),
        FetchPage(payloads=(_payload("p-2"),), next_cursor=None),
    ]
    result = await run_collector(
        session, ScriptedCollector(news_source, script=script), window=WINDOW
    )

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.pages_fetched == 2
    assert result.outcome.inserted_count == 2
    assert result.outcome.cursor is None

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.cursor_json is None
    assert _raw_item_count(session) == 2


async def test_max_pages_guard_saves_cursor_and_marks_partial(
    session: Session, news_source
) -> None:
    """单轮页数上限：达到上限按 PARTIAL_FAILED 处理并保留游标，防止无限翻页。"""

    class TwoPagesCollector(ScriptedCollector):
        collector_name = "two_pages_collector"
        max_pages_per_run = 2

    script = [
        FetchPage(payloads=(_payload(f"p-{index}"),), next_cursor={"page_token": f"t{index + 1}"})
        for index in range(5)
    ]
    result = await run_collector(
        session, TwoPagesCollector(news_source, script=script), window=WINDOW
    )

    assert result.status is CollectorRunStatus.PARTIAL_FAILED
    assert result.outcome is not None
    assert result.outcome.pages_fetched == 2
    assert result.outcome.cursor == {"page_token": "t2"}
    assert "最大页数" in (result.error_message or "")


# ---------------------------------------------------------------------------
# 强制场景 4：单个采集器失败不影响其他采集器
# ---------------------------------------------------------------------------
async def test_one_failing_collector_does_not_block_others(session: Session, make_source) -> None:
    failing_source = make_source(
        name="src-failing", source_type=SourceType.NEWS, base_url="https://bad.invalid"
    )
    healthy_source = make_source(
        name="src-healthy", source_type=SourceType.NEWS, base_url="https://ok.invalid"
    )
    failing = ScriptedCollector(failing_source, script=[TransportTimeoutError("超时")])
    healthy = ScriptedCollector(
        healthy_source, script=[FetchPage(payloads=(_payload("p-ok"),))]
    )

    results = await run_collectors(session, [failing, healthy], window=WINDOW)

    assert [result.status for result in results] == [
        CollectorRunStatus.FAILED,
        CollectorRunStatus.SUCCESS,
    ]
    assert results[1].outcome is not None
    assert results[1].outcome.inserted_count == 1
    assert _raw_item_count(session) == 1

    runs = session.scalars(sa.select(CollectorRun).order_by(CollectorRun.started_at)).all()
    assert len(runs) == 2
    assert {run.status for run in runs} == {
        CollectorRunStatus.FAILED,
        CollectorRunStatus.SUCCESS,
    }
    assert all(run.finished_at is not None for run in runs)


def test_unregistered_collector_reports_actionable_error(make_source) -> None:
    """未实现/未注册的采集器必须给出明确指引，而不是静默跳过。"""
    source = make_source(
        name="weibo-unregistered",
        source_type=SourceType.WEIBO,
        base_url="https://weibo.invalid",
        config_json={"collector": "weibo_collector"},
    )

    with pytest.raises(CollectorNotRegisteredError) as excinfo:
        collector_for_source(source)

    message = str(excinfo.value)
    assert "weibo_collector" in message
    assert "第 3 步" in message


def test_registry_contains_implemented_collectors() -> None:
    """已实现：market / news / macro；未实现：econ_calendar、weibo（禁止伪完成）。"""
    import src.collectors as collectors_package

    registered = collectors_package.available_collectors()
    assert "market_collector" in registered
    assert "news_collector" in registered
    assert "macro_collector" in registered
    assert "econ_calendar_collector" not in registered
    assert "weibo_collector" not in registered


# ---------------------------------------------------------------------------
# retry_count 持久化（团队批复：migration 0002）
# ---------------------------------------------------------------------------
async def test_retry_count_is_persisted_for_successful_retry_run(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    """先 429 限流两次再成功 → collector_runs.retry_count 必须为 2。"""
    transport = mock_transport(
        [HttpResponse(status=429), HttpResponse(status=429), _feed([_post("p-1")])]
    )
    result = await run_collector(
        session,
        HttpCollector(news_source, transport=transport, sleep=recording_sleep),
        window=WINDOW,
    )

    assert result.status is CollectorRunStatus.SUCCESS
    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 2
    assert run.inserted_count == 1


async def test_retry_count_is_persisted_for_failed_retry_run(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    """超时耗尽 3 次尝试 → 即使失败也必须把 retry_count=3 落库。"""
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)
    result = await run_collector(
        session,
        HttpCollector(news_source, transport=transport, sleep=recording_sleep),
        window=WINDOW,
    )

    assert result.status is CollectorRunStatus.FAILED
    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 3
    assert run.error_message is not None


def test_retry_count_defaults_to_zero(session: Session) -> None:
    run = CollectorRun(
        collector_name="no-retry-run",
        started_at=utc_now(),
        status=CollectorRunStatus.RUNNING,
    )
    session.add(run)
    session.flush()

    assert run.retry_count == 0


def test_negative_retry_count_is_rejected_by_database(session: Session) -> None:
    """数据库 CHECK 兜底：retry_count 不允许为负。"""
    run = CollectorRun(
        collector_name="negative-retry", started_at=utc_now(), status=CollectorRunStatus.RUNNING
    )
    session.add(run)
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE collector_runs SET retry_count = -1"))
    session.rollback()


# ---------------------------------------------------------------------------
# 数据质量告警（用户要求：少于特定条数必须打印 WARNING 日志）
# ---------------------------------------------------------------------------
async def test_low_record_count_raises_warning(
    session: Session, news_source, caplog
) -> None:
    class ExpectsFiveCollector(ScriptedCollector):
        collector_name = "expects_five_collector"
        min_records_per_run = 5

    collector = ExpectsFiveCollector(
        news_source, script=[FetchPage(payloads=(_payload("only-one"),))]
    )

    with caplog.at_level(logging.WARNING):
        result = await run_collector(session, collector, window=WINDOW)

    assert result.warnings, "少于预期条数必须产生数据质量告警"
    assert "低于预期下限 5" in result.warnings[0]
    assert any(
        record.levelno == logging.WARNING and "低于预期下限" in record.getMessage()
        for record in caplog.records
    )


async def test_sufficient_records_do_not_warn(session: Session, news_source, caplog) -> None:
    class ExpectsOneCollector(ScriptedCollector):
        collector_name = "expects_one_collector"
        min_records_per_run = 1

    collector = ExpectsOneCollector(
        news_source, script=[FetchPage(payloads=(_payload("p-1"),))]
    )

    with caplog.at_level(logging.WARNING):
        result = await run_collector(session, collector, window=WINDOW)

    assert result.warnings == ()
    assert not [r for r in caplog.records if "低于预期下限" in r.getMessage()]


async def test_source_config_min_records_threshold_is_applied(
    session: Session, make_source
) -> None:
    """团队批复：阈值来自 ``sources.config_json['min_records_per_run']``。"""
    source = make_source(
        name="threshold-from-config",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        config_json={"min_records_per_run": 3},
    )
    collector = ScriptedCollector(source, script=[FetchPage(payloads=(_payload("only-1"),))])

    result = await run_collector(session, collector, window=WINDOW)

    assert any("低于预期下限 3" in warning for warning in result.warnings), result.warnings


async def test_low_record_warning_is_persisted_in_collector_runs(
    session: Session, make_source
) -> None:
    """★ 低于阈值 → WARNING 日志 + ``collector_runs.warnings_json`` 落库（可查询）。"""
    source = make_source(
        name="warnings-persisted",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        config_json={"min_records_per_run": 5},
    )
    collector = ScriptedCollector(source, script=[FetchPage(payloads=(_payload("only-1"),))])

    result = await run_collector(session, collector, window=WINDOW)

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.warnings_json is not None, "低于阈值必须写入 warnings_json（WARNING 级运行）"
    assert "低于预期下限 5" in run.warnings_json["warnings"][0]
    assert run.status is CollectorRunStatus.SUCCESS  # 状态语义不变，仅标记告警


async def test_run_without_warnings_persists_null(session: Session, news_source) -> None:
    collector = ScriptedCollector(
        news_source, script=[FetchPage(payloads=(_payload("p-1"),))]
    )

    result = await run_collector(session, collector, window=WINDOW)

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.warnings_json is None


