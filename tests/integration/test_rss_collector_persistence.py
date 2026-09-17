"""``RssCollector`` 集成测试（落库 / 幂等 / 断点续采 / 单源失败隔离 / 全失败 → FAILED）。

★ 全程不访问外部网络 ★：HTTP 由 ``conftest.MockTransport`` 脚本化提供，
   ``readonly`` 重放用例甚至不提供任何响应（一旦发请求就会失败）。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import CollectorRun, NewsEvent, RawItem
from database.models.enums import CollectorRunStatus, RawItemType, SourceType
from scripts import collect_rss
from src.collectors import CollectWindow, HttpResponse, run_collector
from src.collectors.rss_collector import RSS_PARSER_VERSION, RssCollector, RssSourceSpec

pytestmark = pytest.mark.integration

FEED_A = "https://feed-a.invalid/rss.xml"
FEED_B = "https://feed-b.invalid/rss.xml"
WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 30, tzinfo=UTC)
)


def _spec(source_id: str, url: str, *, source_name: str | None = None) -> RssSourceSpec:
    return RssSourceSpec(
        id=source_id,
        name=source_id,
        url=url,
        source_name=source_name or source_id,
        author_name="金十数据",
        enabled=True,
        verified=True,
    )


def _rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>Gold</title>" + "".join(items) + "</channel></rss>"
    )


def _item(
    guid: str,
    *,
    title: str = "黄金 3400 上方继续看多",
    pub_date: str | None = "Mon, 14 Sep 2026 08:30:00 GMT",
) -> str:
    time_tag = f"<pubDate>{pub_date}</pubDate>" if pub_date else ""
    return (
        "<item>"
        f"<title><![CDATA[{title}]]></title>"
        f"<link>https://feed.invalid/{guid}</link>"
        f"<description><![CDATA[<p>3400 上方减仓后继续持有。</p>]]></description>"
        f'<guid isPermaLink="false">{guid}</guid>{time_tag}'
        "</item>"
    )


def _feed(status: int = 200, *, items: tuple[str, ...] = ()) -> HttpResponse:
    return HttpResponse(status=status, text=_rss(*items) if items else None)


def _raws(session: Session) -> list[RawItem]:
    return list(
        session.scalars(sa.select(RawItem).where(RawItem.item_type == RawItemType.NEWS)).all()
    )


def _runs(session: Session) -> list[CollectorRun]:
    return list(session.scalars(sa.select(CollectorRun)).all())


def _events(session: Session) -> list[NewsEvent]:
    return list(session.scalars(sa.select(NewsEvent)).all())


@pytest.fixture()
def rss_source(make_source):
    return make_source(
        name="rss-source",
        source_type=SourceType.NEWS,
        base_url=FEED_A,
        enabled=True,
        config_json={"min_records_per_run": 0},
    )


async def test_run_persists_raw_items_aligned_with_docs11_contract(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    transport = mock_transport(
        [
            HttpResponse(404),
            _feed(items=(_item("g-1"), _item("g-2", title="Gold nears 3450", pub_date=None))),
        ]
    )
    collector = RssCollector(
        rss_source, specs=[_spec("feed_a", FEED_A)], transport=transport, cache_dir=tmp_path
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.inserted_count == 2
    raws = {item.source_record_id: item for item in _raws(session)}
    assert set(raws) == {"g-1", "g-2"}

    first = raws["g-1"]
    assert first.title == "黄金 3400 上方继续看多"
    assert first.content_text == "3400 上方减仓后继续持有。"
    assert first.source_url == "https://feed.invalid/g-1"
    assert first.raw_json["feed_id"] == "feed_a"
    assert first.raw_json["feed_url"] == FEED_A
    assert first.raw_json["source_name"] == "feed_a"
    assert first.raw_json["author_name"] == "金十数据"
    assert first.raw_json["parser_version"] == RSS_PARSER_VERSION
    assert first.published_at is not None
    assert first.published_at.replace(tzinfo=UTC) == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert first.effective_at >= first.collected_at

    # ★ 无发布时间 → published_at 留空，effective_at 回落到 collected_at（时间因果不猜）
    second = raws["g-2"]
    assert second.published_at is None
    assert second.raw_json["published_tz_ambiguous"] is False
    assert second.raw_json["published_reason"] == "empty"
    assert second.effective_at == second.collected_at
    events = _events(session)
    assert len(events) == 1  # 无发布时间的 g-2 只能留在原始层，不能冒充结构化新闻
    assert events[0].raw_item_id == first.id
    assert events[0].parser_version == RSS_PARSER_VERSION
    assert events[0].published_at.replace(tzinfo=UTC) == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)


async def test_second_run_is_idempotent(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport(
            [
                HttpResponse(404),
                _feed(items=(_item("g-1"), _item("g-2", title="Gold nears 3450"))),
            ]
        ),
        cache_dir=tmp_path,
    )
    first = await run_collector(session, collector, window=WINDOW)
    assert first.outcome is not None and first.outcome.inserted_count == 2

    # 第二轮：缓存无 ETag/Last-Modified → 直接复用缓存（零请求），payload 相同 → 全部去重
    second_collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport([]),
        cache_dir=tmp_path,
    )
    second = await run_collector(session, second_collector, window=WINDOW)
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 2
    assert second_collector.cache.stats()["hits"] >= 1
    assert len(_raws(session)) == 2  # 没有产生重复行
    assert len(_events(session)) == 2  # 结构化层同样幂等


async def test_single_source_failure_does_not_abort_other_sources(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    transport = mock_transport(
        [
            HttpResponse(404),  # feed_a：robots 允许
            _feed(items=(_item("g-1"),)),  # feed_a：正常
            HttpResponse(403),  # feed_b：robots 取不到 → 技术性失败（不静默）
        ]
    )
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A), _spec("feed_b", FEED_B)],
        transport=transport,
        cache_dir=tmp_path,
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS  # 好源的数据必须保住
    assert result.outcome is not None and result.outcome.inserted_count == 1
    assert any("feed_b" in w for w in result.warnings)
    assert len(_raws(session)) == 1




async def test_all_sources_failed_marks_run_failed(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    spec = RssSourceSpec(
        id="feed_a",
        name="feed_a",
        url=FEED_A,
        source_name="feed_a",
        enabled=True,
        verified=True,
        robots_check=False,
    )
    collector = RssCollector(
        rss_source,
        specs=[spec],
        transport=mock_transport([HttpResponse(503), HttpResponse(503), HttpResponse(503)]),
        cache_dir=tmp_path,
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.outcome is not None
    assert "所有 RSS 源均抓取失败" in (result.outcome.error_message or "")
    runs = _runs(session)
    assert runs and runs[-1].status is CollectorRunStatus.FAILED
    assert _raws(session) == []


async def test_cursor_resumes_from_next_source(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A), _spec("feed_b", FEED_B)],
        transport=mock_transport([HttpResponse(404), _feed(items=(_item("g-1"),))]),
        cache_dir=tmp_path,
    )
    collector.max_pages_per_run = 1  # 强制一页/轮：验证游标续采

    first = await run_collector(session, collector, window=WINDOW)
    assert first.outcome is not None
    assert first.outcome.cursor == {"feed_index": 1}
    assert first.outcome.resumed_from_cursor is False
    assert first.status is CollectorRunStatus.PARTIAL_FAILED  # 未跑完 → 必须可观测

    resumed = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A), _spec("feed_b", FEED_B)],
        transport=mock_transport(
            [HttpResponse(404), _feed(items=(_item("g-9", title="黄金回踩 3360 后再度上攻"),))]
        ),
        cache_dir=tmp_path,
    )
    resumed.max_pages_per_run = 1
    second = await run_collector(session, resumed, window=WINDOW, resume=True)
    assert second.outcome is not None
    assert second.outcome.resumed_from_cursor is True
    assert second.outcome.inserted_count == 1
    assert {item.source_record_id for item in _raws(session)} == {"g-1", "g-9"}


async def test_readonly_cache_run_replays_without_any_request(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    warm = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport([HttpResponse(404), _feed(items=(_item("g-1"),))]),
        cache_dir=tmp_path,
    )
    await run_collector(session, warm, window=WINDOW)

    offline = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport([]),  # 任何请求都会让 MockTransport 报错 → 证明零请求
        cache_dir=tmp_path,
        cache_mode="readonly",
    )
    second = await run_collector(session, offline, window=WINDOW)
    assert second.outcome is not None
    assert second.outcome.duplicate_count == 1  # 同一批内容重放 → 去重
    assert offline.cache.stats()["mode"] == "readonly"


async def test_cli_database_path_persists_raw_event_and_run(
    session: Session,
    engine: sa.Engine,
    make_source,
    mock_transport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--to-db`` 的实际驱动路径必须完成三层落库，不能再是未接线占位。"""
    make_source(
        name="feed_a",
        source_type=SourceType.NEWS,
        base_url=FEED_A,
        enabled=True,
        config_json={"min_records_per_run": 0},
    )
    session.commit()  # _drive_to_db 使用独立事务，需要先让来源可见
    transport = mock_transport([HttpResponse(404), _feed(items=(_item("cli-1"),))])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr("database.session.build_engine", lambda: engine)

    args = argparse.Namespace(
        lookback_days=90,
        min_interval=1.0,
        cache_dir=str(tmp_path / "cache"),
        cache_mode="auto",
        per_feed_limit=None,
        json_out=str(tmp_path / "stats.json"),
    )
    code = await collect_rss._drive_to_db(args, [_spec("feed_a", FEED_A)])  # noqa: SLF001

    assert code == 0
    session.expire_all()
    assert len(_raws(session)) == 1
    assert len(_events(session)) == 1
    runs = _runs(session)
    assert len(runs) == 1 and runs[0].status is CollectorRunStatus.SUCCESS
    stats = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert stats["source_distribution"]["feed_a"] == {"count": 1, "share": 1.0}
    assert stats["concentration_warnings"] == ["feed_a 单源占比 100.0% > 40%"]
