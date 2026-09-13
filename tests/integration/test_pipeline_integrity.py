"""端到端数据管道完整性复核（Phase 1 Step 3 收口，团队批复"复核整体管道"）。

复核链路：::

    seeds（instruments / sources）
        → 采集器（market / news / macro）
        → 原始层 raw_items（QUOTE / NEWS / MACRO，可追溯）
        → 结构化层 market_bars / news_events / macro_events
        → 运行元数据 collector_runs（统计 / 重试 / 告警 / 游标）

复核目标：
    1. 三个采集器可在一轮调度中全部运行，每个来源各自留下 ``collector_runs``；
    2. 原始层与结构化层数据一致（条数、来源、可追溯）；
    3. **时间因果不变式**（防未来数据泄漏）在所有表上成立：
       ``event_at/open_time <= effective_at``、``collected_at <= effective_at``、
       ``effective_at <= 本轮结束时间``；
    4. 重跑幂等（不重复落库）；
    5. 单源失败被隔离：一个来源失败，其他来源照常采集（06 规则第 12 条）。

★ 全程不访问外部网络 ★：所有 HTTP 由 :class:`_RoutingTransport` 按 URL 路由返回
   Mock 数据；conftest 的 autouse 守卫会拦截任何非本机 socket 连接。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import CollectorRun, MacroEvent, MarketBar, NewsEvent, RawItem
from database.models.enums import CollectorRunStatus, RawItemType, SourceType
from database.seeds import seed_instruments
from src.collectors import (
    CollectWindow,
    HttpRequest,
    HttpResponse,
    NewsCollector,
    run_collectors,
)
from src.collectors.macro import MacroCollector
from src.collectors.market import MarketCollector

pytestmark = pytest.mark.integration

#: 使用"过去的"窗口，保证 event_at < collected_at（真实系统时间），与 CI 运行时刻无关
WINDOW_START = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
WINDOW = CollectWindow(start_at=WINDOW_START, end_at=WINDOW_START + timedelta(minutes=10))
API_KEY = "pipeline-fred-key"

MARKET_PREFIX = "/v8/finance/chart/"
NEWS_HOST = "feed.invalid"
MACRO_HOST = "api.stlouisfed.org"


def _as_utc(value: datetime) -> datetime:
    """SQLite 返回 naive datetime：统一补齐 UTC 后再比较（仅测试辅助）。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mock 数据构造
# ---------------------------------------------------------------------------
def _chart_response(bars: int = 10) -> HttpResponse:
    """Yahoo chart JSON：窗口内每分钟 1 根 K 线（满足行情采集器的条数期望）。"""
    timestamps = [
        int((WINDOW_START + timedelta(minutes=offset)).timestamp()) for offset in range(bars)
    ]
    base = [2400.0 + offset for offset in range(bars)]
    return HttpResponse(
        status=200,
        json_body={
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "XAUUSD", "currency": "USD"},
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": base,
                                    "high": [value + 5 for value in base],
                                    "low": [value - 5 for value in base],
                                    "close": [value + 1 for value in base],
                                    "volume": [120.0] * bars,
                                }
                            ]
                        },
                    }
                ],
            }
        },
    )


def _rss_response() -> HttpResponse:
    """美联储风格 RSS：2 条窗口内新闻（时间均为 UTC 可解析格式）。"""

    def item(guid: str, title: str, pub_date: str) -> str:
        return (
            "<item>"
            f"<title>{title}</title>"
            f"<link>https://{NEWS_HOST}/{guid}</link>"
            "<description><p>Macro digest.</p></description>"
            f'<guid isPermaLink="false">{guid}</guid>'
            f"<pubDate>{pub_date}</pubDate>"
            "<category>Monetary Policy</category>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Fed</title>'
        + item("p-1", "Fed holds rates steady", "Tue, 02 Jan 2024 08:05:00 GMT")
        + item("p-2", "Fed minutes published", "Tue, 02 Jan 2024 16:05:00 +0800")
        + "</channel></rss>"
    )
    return HttpResponse(status=200, text=xml)


def _fred_response(request: HttpRequest) -> HttpResponse:
    """FRED observations：每个 series 返回 1 条过去日期的观测（保持日粒度）。"""
    series_id = str(request.params.get("series_id", ""))
    unit = "Percent" if series_id == "DFF" else "Index 2017=100"
    value = "4.33" if series_id == "DFF" else "319.6"
    return HttpResponse(
        status=200,
        json_body={
            "units": unit,
            "observations": [
                {
                    "realtime_start": "2024-01-02",
                    "realtime_end": "2024-01-02",
                    "date": "2023-12-01",
                    "value": value,
                }
            ],
        },
    )


class _RoutingTransport:
    """按 URL 路由的 Mock 传输层：一次为整条管道供数（与请求顺序无关）。"""

    def __init__(
        self,
        routes: Sequence[tuple[str, Callable[[HttpRequest], HttpResponse]]],
    ) -> None:
        self._routes = routes
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for needle, responder in self._routes:
            if needle in request.url:
                return responder(request)
        raise AssertionError(f"未路由的请求：{request.url}")


def _pipeline_transport(*, break_news: bool = False) -> _RoutingTransport:
    """正常管道传输层；``break_news=True`` 时新闻源持续返回 500（用于失败隔离测试）。"""
    news_responder = (
        (lambda _request: HttpResponse(status=500))
        if break_news
        else (lambda _request: _rss_response())
    )
    return _RoutingTransport(
        [
            (MARKET_PREFIX, lambda _request: _chart_response()),
            (NEWS_HOST, news_responder),
            (MACRO_HOST, _fred_response),
        ]
    )


@pytest.fixture()
def pipeline_builder(session: Session, make_source, recording_sleep):
    """构造三源采集器（配置形状与生产种子一致，仅把网络层替换为 Mock）。

    返回 ``(build, sources)``：``build(transport)`` 生成采集器列表。
    """
    seed_instruments(session)

    market_source = make_source(
        name="pipeline-market",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        config_json={
            "collector": "market_collector",
            "provider": "yahoo_chart",
            "symbols": ["XAUUSD"],
            "timeframes": ["1m"],
            "min_records_per_run": 0,  # 行情条数由"窗口 ÷ 周期"自动推断
        },
    )
    news_source = make_source(
        name="pipeline-news",
        source_type=SourceType.NEWS,
        base_url="https://news.invalid",
        config_json={
            "collector": "news_collector",
            "feeds": [f"https://{NEWS_HOST}/rss"],
            "min_records_per_run": 1,
        },
    )
    macro_source = make_source(
        name="pipeline-macro",
        source_type=SourceType.MACRO,
        base_url="https://api.stlouisfed.org",
        config_json={
            "collector": "macro_collector",
            "provider": "fred",
            "series": ["CPIAUCSL", "DFF"],
            "lookback_days": 45,
            "min_records_per_run": 1,
        },
    )

    def _build(transport: Any) -> list[Any]:
        return [
            MarketCollector(market_source, transport=transport, sleep=recording_sleep),
            NewsCollector(news_source, transport=transport, sleep=recording_sleep),
            MacroCollector(
                macro_source, transport=transport, sleep=recording_sleep, api_key=API_KEY
            ),
        ]

    return _build, (market_source, news_source, macro_source)


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# 1) 三层落库 + 时间因果不变式 + 幂等
# ---------------------------------------------------------------------------
async def test_full_pipeline_persists_all_layers(session: Session, pipeline_builder) -> None:
    build, _sources = pipeline_builder
    transport = _pipeline_transport()

    results = await run_collectors(session, build(transport), window=WINDOW)

    assert [result.collector_name for result in results] == [
        "market_collector",
        "news_collector",
        "macro_collector",
    ]
    assert all(result.status is CollectorRunStatus.SUCCESS for result in results), [
        result.summary() for result in results
    ]

    # 原始层：三类 item_type 各自留痕（可追溯）
    by_type = dict(
        session.execute(
            sa.select(RawItem.item_type, sa.func.count()).group_by(RawItem.item_type)
        ).all()
    )
    assert by_type[RawItemType.QUOTE] == 10
    assert by_type[RawItemType.NEWS] == 2
    assert by_type[RawItemType.MACRO] == 2

    # 结构化层
    assert _count(session, MarketBar) == 10
    assert _count(session, NewsEvent) == 2
    assert _count(session, MacroEvent) == 2

    # 运行元数据：三个来源各自一行，无重试
    runs = list(session.scalars(sa.select(CollectorRun)).all())
    assert len(runs) == 3
    assert all(run.retry_count == 0 for run in runs)
    assert all(run.finished_at is not None for run in runs)

    _assert_time_invariants(session)


async def test_second_round_is_idempotent(session: Session, pipeline_builder) -> None:
    """同一窗口重跑：零新增（靠内容哈希 + 唯一键幂等），表行数不变。"""
    build, _sources = pipeline_builder

    first = await run_collectors(session, build(_pipeline_transport()), window=WINDOW)
    counts_before = (
        _count(session, RawItem),
        _count(session, MarketBar),
        _count(session, NewsEvent),
        _count(session, MacroEvent),
    )

    second = await run_collectors(session, build(_pipeline_transport()), window=WINDOW)

    assert all(result.status is CollectorRunStatus.SUCCESS for result in first)
    assert all(result.status is CollectorRunStatus.SUCCESS for result in second)
    assert all(
        result.outcome is not None and result.outcome.inserted_count == 0 for result in second
    ), [result.summary() for result in second]

    counts_after = (
        _count(session, RawItem),
        _count(session, MarketBar),
        _count(session, NewsEvent),
        _count(session, MacroEvent),
    )
    assert counts_after == counts_before


def _assert_time_invariants(session: Session) -> None:
    """全表时间因果不变式（防未来数据泄漏的最后一道校验）。"""
    runs_by_source = {
        run.source_id: run for run in session.scalars(sa.select(CollectorRun)).all()
    }
    raw_by_id = {item.id: item for item in session.scalars(sa.select(RawItem)).all()}

    for bar in session.scalars(sa.select(MarketBar)).all():
        run = runs_by_source[bar.source_id]
        assert _as_utc(bar.open_time) <= _as_utc(bar.effective_at)
        assert _as_utc(bar.collected_at) <= _as_utc(bar.effective_at)
        assert _as_utc(bar.effective_at) <= _as_utc(run.finished_at or run.started_at)

    for news in session.scalars(sa.select(NewsEvent)).all():
        raw = raw_by_id[news.raw_item_id]  # 新闻事件通过 raw_items 关联来源
        run = runs_by_source[raw.source_id]
        assert _as_utc(news.published_at) <= _as_utc(news.effective_at)
        if news.event_at is not None:
            assert _as_utc(news.event_at) <= _as_utc(news.effective_at)
        assert _as_utc(news.effective_at) <= _as_utc(run.finished_at or run.started_at)

    for macro in session.scalars(sa.select(MacroEvent)).all():
        run = runs_by_source[macro.source_id]
        assert _as_utc(macro.event_at) <= _as_utc(macro.effective_at)
        assert _as_utc(macro.collected_at) <= _as_utc(macro.effective_at)
        assert _as_utc(macro.effective_at) <= _as_utc(run.finished_at or run.started_at)


# ---------------------------------------------------------------------------
# 2) 结构化行可追溯 / 3) 单源失败隔离
# ---------------------------------------------------------------------------
async def test_structured_rows_are_traceable_to_raw_items(
    session: Session, pipeline_builder
) -> None:
    """任何结构化数据都必须能回到原始层（01 §"任何研究输入可追溯"）。"""
    build, _sources = pipeline_builder
    await run_collectors(session, build(_pipeline_transport()), window=WINDOW)

    news_raw_ids = set(
        session.scalars(
            sa.select(RawItem.id).where(RawItem.item_type == RawItemType.NEWS)
        ).all()
    )
    news_event_raw_ids = set(session.scalars(sa.select(NewsEvent.raw_item_id)).all())
    assert news_event_raw_ids and news_event_raw_ids <= news_raw_ids

    macro_series = {
        row[0]
        for row in session.execute(
            sa.select(RawItem.raw_json["series_id"]).where(
                RawItem.item_type == RawItemType.MACRO
            )
        ).all()
    }
    macro_codes = set(session.scalars(sa.select(MacroEvent.event_code)).all())
    assert macro_codes == macro_series == {"CPIAUCSL", "DFF"}

    parser_versions = {
        row[0]
        for row in session.execute(
            sa.select(RawItem.raw_json["parser_version"]).where(
                RawItem.item_type.in_([RawItemType.MACRO, RawItemType.NEWS])
            )
        ).all()
    }
    assert parser_versions and all(parser_versions)  # 解析器版本可追溯


async def test_collector_failure_is_isolated(session: Session, pipeline_builder) -> None:
    """★ 06 规则第 12 条：一个来源失败，其他来源照常采集并各自留下记录。"""
    build, _sources = pipeline_builder

    results = await run_collectors(
        session, build(_pipeline_transport(break_news=True)), window=WINDOW
    )

    by_name = {result.collector_name: result for result in results}
    assert by_name["market_collector"].status is CollectorRunStatus.SUCCESS
    assert by_name["macro_collector"].status is CollectorRunStatus.SUCCESS

    failed = by_name["news_collector"]
    assert failed.status is CollectorRunStatus.FAILED
    assert "500" in (failed.error_message or "")
    assert failed.retry_count == 3  # 3 次尝试后放弃

    assert _count(session, CollectorRun) == 3
    # 失败来源不留半成品数据；成功来源照常落库
    assert _count(session, NewsEvent) == 0
    assert _count(session, MarketBar) == 10
    assert _count(session, MacroEvent) == 2

    run = session.get(CollectorRun, failed.run_id)
    assert run is not None
    assert run.error_message and "500" in run.error_message
    assert run.retry_count == 3


