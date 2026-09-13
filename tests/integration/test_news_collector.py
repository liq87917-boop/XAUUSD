"""``NewsCollector`` 集成测试（RSS 落库 / 时区对齐 / 幂等 / 重试 / 断点续采）。

★ 全程不访问外部网络 ★：HTTP 一律由 ``conftest.MockTransport`` 脚本化提供，
   conftest 的 autouse 守卫还会拦截任何非本机 socket 连接。

覆盖团队批复的强制要求：
    1. 正常 RSS 解析并落库（严格按 04 §14 ``news_events`` 字段）；
    2. XML 格式异常 → 可读错误、不写脏数据；
    3. 超时重试（3 次尝试 → FAILED，retry_count 落库）与 429 限流重试；
    4. **UTC 时区转换正确性**（带偏移的时间必须换算成 UTC）；
    5. 时区不明确 → WARNING + ``published_at`` 置空 + ``effective_at = collected_at``，
       且不生成 ``news_events``；
    6. 跨源 content_hash 去重（同一篇新闻被多个 RSS 源转载只落库一次）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import CollectorRun, NewsEvent, RawItem
from database.models.enums import CollectorRunStatus, RawItemType, SourceType
from src.collectors import CollectWindow, HttpResponse, TransportTimeoutError, run_collector
from src.collectors.news import NEWS_PARSER_VERSION, NewsCollector

pytestmark = pytest.mark.integration

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 7, 0, 0, tzinfo=UTC),
    end_at=datetime(2026, 9, 8, 0, 0, tzinfo=UTC),
)


def _rss_item(
    *,
    guid: str,
    title: str = "Gold rises on Fed bets",
    description: str = "<p>Gold <b>rises</b> 1%.</p>",
    pub_date: str = "Mon, 07 Sep 2026 12:00:00 GMT",
    category: str = "Gold",
) -> str:
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<link>https://feed.invalid/{guid}</link>"
        f"<description>{description}</description>"
        f'<guid isPermaLink="false">{guid}</guid>'
        f"<pubDate>{pub_date}</pubDate>"
        f"<category>{category}</category>"
        "</item>"
    )


def _rss_document(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>' + "".join(items) + "</channel></rss>"
    )


def _feed_response(*items: str, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, text=_rss_document(*items) if items else None)


@pytest.fixture()
def news_source(make_source):
    return make_source(
        name="news-source",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        enabled=True,
        config_json={"feeds": ["https://feed.invalid/rss"]},
    )


def _raw_news(session: Session) -> list[RawItem]:
    return list(
        session.scalars(sa.select(RawItem).where(RawItem.item_type == RawItemType.NEWS)).all()
    )


def _events(session: Session) -> list[NewsEvent]:
    return list(session.scalars(sa.select(NewsEvent)).all())


class _RecordingHandler(logging.Handler):
    """直接挂在目标 logger 上的记录器（不依赖 root 传播，避免测试间顺序耦合）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# 1) 正常 RSS 解析并落库 + UTC 时区转换正确性
# ---------------------------------------------------------------------------
async def test_normal_rss_run_persists_raw_items_and_news_events(
    session: Session, news_source, mock_transport
) -> None:
    transport = mock_transport(
        [
            _feed_response(
                _rss_item(guid="g-1", pub_date="Mon, 07 Sep 2026 12:00:00 GMT"),
                _rss_item(
                    guid="g-2",
                    title="Fed keeps rates unchanged",
                    pub_date="Mon, 07 Sep 2026 18:30:00 +0800",
                    category="Monetary Policy",
                ),
            )
        ]
    )
    collector = NewsCollector(news_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.fetched_count == 2
    assert result.outcome.inserted_count == 2

    raws = _raw_news(session)
    assert len(raws) == 2
    first = next(item for item in raws if item.source_record_id == "g-1")
    assert first.title == "Gold rises on Fed bets"
    assert first.content_text == "Gold rises 1%."
    assert first.source_url == "https://feed.invalid/g-1"
    assert first.raw_json["parser_version"] == NEWS_PARSER_VERSION
    assert first.raw_json["published_at_tz_ambiguous"] is False
    # ★ UTC 转换正确性：GMT 12:00 保持 12:00Z
    assert first.published_at is not None
    assert first.published_at.replace(tzinfo=UTC) == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert first.raw_json["published_at"] == "2026-09-07T12:00:00+00:00"
    assert first.effective_at >= first.collected_at

    events = _events(session)
    assert len(events) == 2
    event = next(item for item in events if item.raw_item_id == first.id)
    assert event.headline == "Gold rises on Fed bets"
    assert event.summary == "Gold rises 1%."
    assert event.event_type == "Gold"
    assert event.published_at.replace(tzinfo=UTC) == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert event.effective_at == first.effective_at
    assert event.parser_version == NEWS_PARSER_VERSION
    assert event.importance is None
    assert event.sentiment is None
    assert event.event_at is None

    second = next(item for item in raws if item.source_record_id == "g-2")
    assert second.published_at is not None
    # ★ +0800 的 18:30 → 10:30Z
    assert second.published_at.replace(tzinfo=UTC) == datetime(2026, 9, 7, 10, 30, tzinfo=UTC)


async def test_rfc822_with_offset_is_converted_to_utc(
    session: Session, news_source, mock_transport
) -> None:
    """★ UTC 转换正确性：-0400 的 08:00 必须落库为 12:00Z。"""
    transport = mock_transport(
        [_feed_response(_rss_item(guid="g-offset", pub_date="Mon, 07 Sep 2026 08:00:00 -0400"))]
    )

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.SUCCESS
    raw = _raw_news(session)[0]
    assert raw.published_at is not None
    assert raw.published_at.replace(tzinfo=UTC) == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert _events(session)[0].published_at.replace(tzinfo=UTC) == datetime(
        2026, 9, 7, 12, 0, tzinfo=UTC
    )


# ---------------------------------------------------------------------------
# 2) XML 格式异常（必须给出可读错误且不写脏数据）
# ---------------------------------------------------------------------------
async def test_malformed_xml_fails_without_writing_data(
    session: Session, news_source, mock_transport
) -> None:
    transport = mock_transport([HttpResponse(status=200, text="<rss><channel><item>")])

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.FAILED
    assert "XML 解析失败" in (result.error_message or "")
    assert _raw_news(session) == []
    assert _events(session) == []

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.status is CollectorRunStatus.FAILED
    assert run.error_message is not None


async def test_doctype_feed_is_rejected(session: Session, news_source, mock_transport) -> None:
    payload = (
        '<?xml version="1.0"?><!DOCTYPE rss>'
        '<rss version="2.0"><channel><item><title>x</title></item></channel></rss>'
    )
    transport = mock_transport([HttpResponse(status=200, text=payload)])

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.FAILED
    assert "DOCTYPE" in (result.error_message or "")
    assert _raw_news(session) == []


async def test_empty_feed_body_fails(session: Session, news_source, mock_transport) -> None:
    transport = mock_transport([HttpResponse(status=200, text="")])

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.FAILED
    assert "为空" in (result.error_message or "")


# ---------------------------------------------------------------------------
# 3) 超时重试与限流（含 retry_count 落库）
# ---------------------------------------------------------------------------
async def test_timeout_retries_three_times_then_failed(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)
    collector = NewsCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert transport.calls == 3
    assert recording_sleep.delays == [1.0, 2.0]  # 指数退避
    assert "超时" in (result.error_message or "")
    assert _raw_news(session) == []

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 3


async def test_timeout_then_success_stores_items_and_retry_count(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [
            TransportTimeoutError("读取超时"),
            _feed_response(_rss_item(guid="g-retry")),
        ]
    )
    collector = NewsCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 1
    assert len(_raw_news(session)) == 1
    assert len(_events(session)) == 1

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 1


async def test_rate_limit_429_is_retried_then_succeeds(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [HttpResponse(status=429), _feed_response(_rss_item(guid="g-429"))]
    )
    collector = NewsCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 1
    assert len(_raw_news(session)) == 1


async def test_http_500_exhausts_retries_and_fails(
    session: Session, news_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([HttpResponse(status=500)] * 3)
    collector = NewsCollector(news_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert "500" in (result.error_message or "")
    assert result.retry_count == 3
    assert _raw_news(session) == []


# ---------------------------------------------------------------------------
# 5) 时区不明确 → WARNING + effective_at = collected_at + 不写 news_events
# ---------------------------------------------------------------------------
async def test_ambiguous_timezone_sets_effective_at_to_collected_at(
    session: Session, news_source, mock_transport
) -> None:
    transport = mock_transport(
        [_feed_response(_rss_item(guid="g-ambiguous", pub_date="Mon, 07 Sep 2026 12:00:00"))]
    )

    # 直接挂在采集器 logger 上收集日志：不依赖 root 传播，
    # 因此本测试与其他测试（可能配置过 dictConfig）之间没有顺序耦合。
    recorder = _RecordingHandler()
    logger = logging.getLogger("gold_ai.collectors.news")
    logger.addHandler(recorder)
    try:
        result = await run_collector(
            session, NewsCollector(news_source, transport=transport), window=WINDOW
        )
    finally:
        logger.removeHandler(recorder)

    assert result.status is CollectorRunStatus.SUCCESS
    raw = _raw_news(session)[0]
    assert raw.published_at is None  # ★ 绝不猜测时区
    assert raw.raw_json["published_at_tz_ambiguous"] is True
    assert raw.raw_json["published_at_raw"] == "Mon, 07 Sep 2026 12:00:00"
    assert raw.effective_at == raw.collected_at  # ★ 团队批复要求
    assert _events(session) == []  # 不把 collected_at 冒充成发布时间
    assert any("时区不明确" in warning for warning in result.warnings), result.warnings
    assert any("未给出明确时区" in record.getMessage() for record in recorder.records), (
        "数据质量告警必须进入 WARNING 日志"
    )


async def test_unparsable_timestamp_only_stores_raw_item(
    session: Session, news_source, mock_transport
) -> None:
    transport = mock_transport(
        [_feed_response(_rss_item(guid="g-bad", pub_date="not-a-real-date"))]
    )

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.SUCCESS
    raw = _raw_news(session)[0]
    assert raw.published_at is None
    assert raw.raw_json["published_at_parse_reason"] == "unparsable"
    assert _events(session) == []
    assert any("无法解析" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# 6) 跨源 content_hash 去重（同一篇新闻被多个 RSS 源转载）
# ---------------------------------------------------------------------------
async def test_same_article_from_two_sources_is_stored_once(
    session: Session, make_source, mock_transport
) -> None:
    first_source = make_source(
        name="news-a",
        source_type=SourceType.NEWS,
        base_url="https://a.invalid",
        config_json={"feeds": ["https://a.invalid/rss"]},
    )
    second_source = make_source(
        name="news-b",
        source_type=SourceType.NEWS,
        base_url="https://b.invalid",
        config_json={"feeds": ["https://b.invalid/rss"]},
    )
    title = "Fed signals patience on rate cuts"
    body = "The committee will be patient."

    item_a = _feed_response(_rss_item(guid="a-1", title=title, description=body))
    item_b = _feed_response(_rss_item(guid="b-1", title=title, description=body))

    first = await run_collector(
        session,
        NewsCollector(first_source, transport=mock_transport([item_a])),
        window=WINDOW,
    )
    second = await run_collector(
        session,
        NewsCollector(second_source, transport=mock_transport([item_b])),
        window=WINDOW,
    )

    assert first.outcome is not None
    assert first.outcome.inserted_count == 1
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 1  # ★ 转载不重复插入
    assert len(_raw_news(session)) == 1
    assert len(_events(session)) == 1


async def test_rerun_same_feed_is_idempotent(session: Session, news_source, mock_transport) -> None:
    transport = mock_transport(
        [_feed_response(_rss_item(guid="g-1")), _feed_response(_rss_item(guid="g-1"))]
    )

    first = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )
    second = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW, resume=False
    )

    assert first.outcome is not None
    assert first.outcome.inserted_count == 1
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 1
    assert len(_raw_news(session)) == 1
    assert len(_events(session)) == 1


# ---------------------------------------------------------------------------
# 窗口过滤 / 多 feed 断点续采 / CSV 兜底 / 条数阈值告警
# ---------------------------------------------------------------------------
async def test_items_outside_window_are_skipped_with_warning(
    session: Session, news_source, mock_transport
) -> None:
    transport = mock_transport(
        [
            _feed_response(
                _rss_item(guid="in-window", pub_date="Mon, 07 Sep 2026 06:00:00 GMT"),
                _rss_item(guid="too-old", pub_date="Tue, 01 Sep 2026 06:00:00 GMT"),
            )
        ]
    )

    result = await run_collector(
        session, NewsCollector(news_source, transport=transport), window=WINDOW
    )

    assert result.outcome is not None
    assert result.outcome.fetched_count == 2
    assert result.outcome.inserted_count == 1
    assert len(_raw_news(session)) == 1
    assert any("超出采集窗口" in warning for warning in result.warnings)


async def test_cursor_resume_between_feeds(session: Session, make_source, mock_transport) -> None:
    """计划级断点续采：第一个 feed 成功后第二个 feed 超时 → 下一轮从第二个 feed 继续。"""
    source = make_source(
        name="news-multi-feed",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        config_json={"feeds": ["https://feed.invalid/a", "https://feed.invalid/b"]},
    )

    first_transport = mock_transport(
        [
            _feed_response(_rss_item(guid="g-a")),
            HttpResponse(status=500),
            HttpResponse(status=500),
            HttpResponse(status=500),
        ]
    )
    first = await run_collector(
        session, NewsCollector(source, transport=first_transport), window=WINDOW
    )

    assert first.status is CollectorRunStatus.PARTIAL_FAILED
    assert first.outcome is not None
    assert first.outcome.cursor is not None
    assert first.outcome.cursor["target_index"] == 1
    assert len(_raw_news(session)) == 1

    second_transport = mock_transport(
        [_feed_response(_rss_item(guid="g-b", title="Second feed item", description="body b"))]
    )
    second = await run_collector(
        session, NewsCollector(source, transport=second_transport), window=WINDOW
    )

    assert second.status is CollectorRunStatus.SUCCESS
    assert second.resumed_from_cursor is True
    assert second_transport.requests[0].url == "https://feed.invalid/b"  # ★ 从断点继续
    assert len(_raw_news(session)) == 2


async def test_csv_mode_imports_local_archive(
    session: Session, make_source, tmp_path: Path
) -> None:
    """CSV 兜底解析器：无可用公开 feed 时也能跑通落库（团队批复允许）。"""
    csv_path = tmp_path / "news_archive.csv"
    csv_path.write_text(
        "record_id,title,summary,published_at\n"
        "n-1,Archive headline,archive body,2026-09-07T09:00:00Z\n",
        encoding="utf-8",
    )
    source = make_source(
        name="news-csv",
        source_type=SourceType.NEWS,
        base_url=None,
        config_json={"csv_path": str(csv_path)},
    )

    result = await run_collector(session, NewsCollector(source), window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.inserted_count == 1
    raw = _raw_news(session)[0]
    assert raw.raw_json["provider"] == "csv"
    assert raw.title == "Archive headline"
    events = _events(session)
    assert len(events) == 1
    assert events[0].published_at.replace(tzinfo=UTC) == datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


async def test_csv_mode_missing_file_reports_actionable_error(
    session: Session, make_source, tmp_path: Path
) -> None:
    source = make_source(
        name="news-csv-missing",
        source_type=SourceType.NEWS,
        base_url=None,
        config_json={"csv_path": str(tmp_path / "does-not-exist.csv")},
    )

    result = await run_collector(session, NewsCollector(source), window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert "新闻 CSV 文件不存在" in (result.error_message or "")


async def test_min_records_threshold_triggers_warning(
    session: Session, make_source, mock_transport
) -> None:
    """条数低于 config_json['min_records_per_run'] → WARNING（宁可多告警）。"""
    source = make_source(
        name="news-threshold",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        config_json={"feeds": ["https://feed.invalid/rss"], "min_records_per_run": 5},
    )
    transport = mock_transport([_feed_response(_rss_item(guid="g-1"))])

    result = await run_collector(
        session, NewsCollector(source, transport=transport), window=WINDOW
    )

    assert any("低于预期下限 5" in warning for warning in result.warnings), result.warnings




