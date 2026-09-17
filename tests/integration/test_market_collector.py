"""``MarketCollector`` 集成测试（行情落库 / 限流重试 / 超时重试 / 数据质量告警）。

★ 全程不访问外部网络 ★
    1. 常规路径：``conftest.MockTransport`` 注入脚本化响应（快速）；
    2. HTTP 层路径：``unittest.mock`` 替换 ``aiohttp.ClientSession.request``，
       让真实的 :class:`AiohttpTransport` + 重试链路一起被验证（用户明确要求）；
    3. conftest 中的 autouse 守卫会拦截任何非本机 socket 连接。

覆盖的强制场景：
    - 数据正常返回（UTC 分钟精度 / OHLCV / Decimal 精度 / 原始 JSON 可追溯）；
    - API 限流（429 → 退避重试 → 成功，retry_count 落库）；
    - 超时重试（3 次尝试后 FAILED，retry_count=3 落库，无脏数据；
      单条落库失败 → PARTIAL_FAILED 且不终止整轮）；
    - 数据质量（缺 1 分钟 → WARNING 日志 + warnings；条数不足 → WARNING）。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest import mock  # noqa: F401 - 用于替换 aiohttp（见 _patch_aiohttp）

import aiohttp
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import CollectorRun, MarketBar, RawItem, Source
from database.models.enums import CollectorRunStatus, RawItemType, SourceType
from database.seeds import seed_instruments, seed_sources
from src.collectors import (
    CollectWindow,
    HttpResponse,
    TransportTimeoutError,
    collector_for_source,
    run_collector,
)
from src.collectors.market import MarketCollector
from src.collectors.transport import AiohttpTransport

pytestmark = pytest.mark.integration

WINDOW_START = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
WINDOW = CollectWindow(start_at=WINDOW_START, end_at=WINDOW_START + timedelta(minutes=10))

BarRow = tuple[int, float, float, float, float, float]


def _bar_row(minute: int, *, base: float = 2400.0, volume: float = 120.0) -> BarRow:
    epoch = int((WINDOW_START + timedelta(minutes=minute)).timestamp())
    return (epoch, base, base + 5, base - 5, base + 1, volume)


def _chart_body(bars: list[BarRow]) -> str:
    """构造 Yahoo chart JSON 文本（仅在 Mock 中使用，不含真实请求）。"""
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "XAUUSD", "currency": "USD"},
                        "timestamp": [bar[0] for bar in bars],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [bar[1] for bar in bars],
                                    "high": [bar[2] for bar in bars],
                                    "low": [bar[3] for bar in bars],
                                    "close": [bar[4] for bar in bars],
                                    "volume": [bar[5] for bar in bars],
                                }
                            ]
                        },
                    }
                ],
            }
        }
    )


def _chart_response(bars: list[BarRow]) -> HttpResponse:
    return HttpResponse(status=200, json_body=json.loads(_chart_body(bars)))


class _FakeAiohttpResponse:
    """aiohttp 响应替身（只实现传输层用到的方法）。"""

    def __init__(self, status: int, text: str, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self._text = text

    async def text(self) -> str:
        return self._text


class _FakeAiohttpRequest:
    """aiohttp 请求上下文替身（支持 ``async with``，并可模拟抛错）。"""

    def __init__(self, item: Any) -> None:
        self._item = item

    async def __aenter__(self) -> _FakeAiohttpResponse:
        if isinstance(self._item, BaseException):
            raise self._item
        status, text, headers = self._item
        return _FakeAiohttpResponse(status, text, headers)

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


def _patch_aiohttp(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> list[dict[str, Any]]:
    """★ 用 ``unittest.mock``（monkeypatch 封装）替换 HTTP 请求，绝不访问外部网络。

    Args:
        script: 依次消费的 ``(status, text, headers)`` 元组，或要抛出的异常实例。
    """
    calls: list[dict[str, Any]] = []
    queue: deque[Any] = deque(script)

    def fake_request(self: aiohttp.ClientSession, method: str, url: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "url": url, "params": kwargs.get("params")})
        if not queue:
            raise AssertionError("aiohttp mock 脚本已耗尽")
        return _FakeAiohttpRequest(queue.popleft())

    monkeypatch.setattr(aiohttp.ClientSession, "request", fake_request)
    return calls


@pytest.fixture()
def market_source(make_source, session: Session):
    """行情数据源（标的来自正式种子，保证 instrument_id 可解析）。"""
    seed_instruments(session)
    return make_source(
        name="market-source",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        enabled=True,
        config_json={"symbols": ["XAUUSD"], "timeframes": ["1m"]},
    )


def _bars(session: Session) -> list[MarketBar]:
    return list(session.scalars(sa.select(MarketBar).order_by(MarketBar.open_time)).all())


def _raw_quotes(session: Session) -> list[RawItem]:
    return list(
        session.scalars(sa.select(RawItem).where(RawItem.item_type == RawItemType.QUOTE)).all()
    )


# ---------------------------------------------------------------------------
# 场景 1：数据正常返回
# ---------------------------------------------------------------------------
async def test_normal_run_persists_utc_minute_bars(
    session: Session, market_source, mock_transport
) -> None:
    transport = mock_transport([_chart_response([_bar_row(0), _bar_row(1), _bar_row(2)])])
    collector = MarketCollector(market_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.fetched_count == 3
    assert result.outcome.inserted_count == 3
    assert result.outcome.duplicate_count == 0

    stored = _bars(session)
    assert len(stored) == 3
    first = stored[0]
    # 时间戳：UTC，精确到分钟。
    # 注：SQLite 不保存时区偏移（本地测试库限制），故按 UTC 重新标注后比较；
    # 生产 / 研究库为 PostgreSQL TIMESTAMPTZ，返回 timezone-aware 时间。
    assert first.open_time.replace(tzinfo=UTC) == WINDOW_START
    assert first.open_time.replace(tzinfo=UTC).second == 0
    assert first.open_time.replace(tzinfo=UTC).microsecond == 0
    assert first.close_time.replace(tzinfo=UTC) == WINDOW_START + timedelta(minutes=1)
    # OHLCV：Decimal 精度（03 §14 禁止 binary float）
    assert first.open == Decimal("2400.00000000")
    assert first.high == Decimal("2405.00000000")
    assert first.low == Decimal("2395.00000000")
    assert first.close == Decimal("2401.00000000")
    assert first.volume == Decimal("120.00000000")
    # 时间因果：effective_at 不早于采集时间
    assert first.effective_at >= first.collected_at
    assert first.source_id == market_source.id

    quotes = _raw_quotes(session)
    assert len(quotes) == 3
    assert {quote.raw_json["provider"] for quote in quotes} == {"yahoo_chart"}
    assert {quote.raw_json["symbol"] for quote in quotes} == {"XAUUSD"}

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.fetched_count == 3
    assert run.inserted_count == 3
    assert run.failed_count == 0
    assert run.retry_count == 0


async def test_normal_run_through_mocked_aiohttp_transport(
    session: Session, market_source, monkeypatch, recording_sleep
) -> None:
    """★ 用 unittest.mock 替换 HTTP 请求，验证真实 ``AiohttpTransport`` + 重试链路。"""
    calls = _patch_aiohttp(monkeypatch, [(200, _chart_body([_bar_row(0), _bar_row(1)]), {})])
    transport = AiohttpTransport(default_timeout_seconds=1.0)
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    try:
        result = await run_collector(session, collector, window=WINDOW)
    finally:
        await transport.close()

    assert result.status is CollectorRunStatus.SUCCESS
    assert len(calls) == 1
    assert calls[0]["url"] == "https://market.invalid/v8/finance/chart/XAUUSD"
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"]["interval"] == "1m"
    assert len(_bars(session)) == 2


async def test_retry_through_mocked_aiohttp_on_rate_limit(
    session: Session, market_source, monkeypatch, recording_sleep
) -> None:
    """HTTP 层限流：mock 连续返回 429，验证真实传输层的退避重试与最终成功。"""
    calls = _patch_aiohttp(
        monkeypatch,
        [
            (429, "", {}),
            (200, _chart_body([_bar_row(0)]), {}),
        ],
    )
    transport = AiohttpTransport(default_timeout_seconds=1.0)
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    try:
        result = await run_collector(session, collector, window=WINDOW)
    finally:
        await transport.close()

    assert result.status is CollectorRunStatus.SUCCESS
    assert len(calls) == 2
    assert recording_sleep.delays == [1.0]
    assert len(_bars(session)) == 1


# ---------------------------------------------------------------------------
# 场景 2/3：限流与超时（含 retry_count 落库）
# ---------------------------------------------------------------------------
async def test_rate_limit_is_retried_then_succeeds(
    session: Session, market_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [HttpResponse(status=429), HttpResponse(status=429), _chart_response([_bar_row(0)])]
    )
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 2
    assert transport.calls == 3
    assert recording_sleep.delays == [1.0, 2.0]  # 指数退避
    assert len(_bars(session)) == 1

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 2  # ★ 重试次数持久化（migration 0002）


async def test_rate_limit_exhausted_marks_failed_without_dirty_data(
    session: Session, market_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([HttpResponse(status=429)] * 3)
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert "429" in (result.error_message or "")
    assert _bars(session) == []

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 3


async def test_timeout_retries_three_times_then_failed(
    session: Session, market_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert transport.calls == 3
    assert recording_sleep.delays == [1.0, 2.0]
    assert "超时" in (result.error_message or "")
    assert _bars(session) == []

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.status is CollectorRunStatus.FAILED
    assert run.retry_count == 3  # ★ 超时重试次数同样持久化
    assert run.error_message is not None


async def test_timeout_then_success_persists_retry_count(
    session: Session, market_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [TransportTimeoutError("读取超时"), _chart_response([_bar_row(0), _bar_row(1)])]
    )
    collector = MarketCollector(market_source, transport=transport, sleep=recording_sleep)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 1
    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 1
    assert len(_bars(session)) == 2


# ---------------------------------------------------------------------------
# 场景 4：数据质量（缺 1 分钟必须告警）
# ---------------------------------------------------------------------------
async def test_missing_minute_emits_warning(
    session: Session, market_source, mock_transport, caplog
) -> None:
    bars = [_bar_row(0), _bar_row(1), _bar_row(3)]  # 缺第 2 分钟
    transport = mock_transport([_chart_response(bars)])
    collector = MarketCollector(market_source, transport=transport)

    with caplog.at_level(logging.WARNING):
        result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert len(_bars(session)) == 3
    assert any("缺失 1 根 K 线" in warning for warning in result.warnings), result.warnings
    # 10 分钟窗口只拿到 3 根 → 同时触发"低于预期下限"告警
    assert any("低于预期下限" in warning for warning in result.warnings), result.warnings
    assert [record for record in caplog.records if record.levelno == logging.WARNING], (
        "数据质量告警必须进入 WARNING 日志"
    )


async def test_insufficient_records_without_gap_still_warns(
    session: Session, market_source, mock_transport
) -> None:
    """窗口内只有 2 根且连续：没有缺口，但仍低于预期条数 → 必须告警。"""
    transport = mock_transport([_chart_response([_bar_row(0), _bar_row(1)])])
    collector = MarketCollector(market_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.warnings
    assert any("低于预期下限" in warning for warning in result.warnings)
    assert not any("缺失" in warning for warning in result.warnings)


async def test_incomplete_provider_response_fails_with_actionable_error(
    session: Session, market_source, mock_transport
) -> None:
    """响应缺少 result（结构不完整）→ 该组合失败并给出可读错误，不写入任何 K 线。"""
    transport = mock_transport([HttpResponse(status=200, json_body={"chart": {"result": []}})])
    collector = MarketCollector(market_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert "result" in (result.error_message or "")
    assert _bars(session) == []


async def test_zero_bars_in_window_is_success_with_warning(
    session: Session, market_source, mock_transport
) -> None:
    """响应结构完整但窗口内无 K 线（例如休市）→ SUCCESS + 0 条 + 数据质量告警。"""
    transport = mock_transport(
        [
            HttpResponse(
                status=200,
                json_body={
                    "chart": {
                        "error": None,
                        "result": [{"timestamp": [], "indicators": {"quote": [{}]}}],
                    }
                },
            )
        ]
    )
    collector = MarketCollector(market_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.inserted_count == 0
    assert _bars(session) == []
    assert any("低于预期下限" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# 幂等 / 404 跳过 / 断点续采
# ---------------------------------------------------------------------------
async def test_rerun_same_window_is_idempotent(
    session: Session, market_source, mock_transport
) -> None:
    """同一窗口重复采集：第二次必须全部判重，库中 K 线与原始切片数量不变。"""
    bars = [_bar_row(0), _bar_row(1)]
    transport = mock_transport([_chart_response(bars), _chart_response(bars)])

    first = await run_collector(
        session, MarketCollector(market_source, transport=transport), window=WINDOW
    )
    second = await run_collector(
        session, MarketCollector(market_source, transport=transport), window=WINDOW, resume=False
    )

    assert first.outcome is not None
    assert first.outcome.inserted_count == 2
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 2
    assert len(_bars(session)) == 2
    assert len(_raw_quotes(session)) == 2


async def test_symbol_404_is_skipped_with_warning(
    session: Session, market_source, mock_transport
) -> None:
    """标的 404（不存在/退市）→ 跳过该组合并告警，整轮仍算成功（06 §12）。"""
    transport = mock_transport([HttpResponse(status=404)])
    collector = MarketCollector(market_source, transport=transport)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert any("404" in warning for warning in result.warnings)
    assert _bars(session) == []


async def test_missing_instrument_yields_partial_failed_with_guidance(
    session: Session, make_source, mock_transport
) -> None:
    """未登记标的：单条落库失败 → PARTIAL_FAILED，错误信息给出可执行指引且整轮不中断。"""
    source = make_source(
        name="market-unlisted",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        config_json={"symbols": ["NOT_LISTED"], "timeframes": ["1m"]},
    )
    collector = MarketCollector(source, transport=mock_transport([_chart_response([_bar_row(0)])]))

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.PARTIAL_FAILED
    assert result.outcome is not None
    assert result.outcome.failed_count == 1
    assert "database.seeds" in (result.error_message or "")
    assert _bars(session) == []


async def test_cursor_resume_between_symbol_pairs(
    session: Session, make_source, mock_transport
) -> None:
    """计划级断点续采：XAUUSD 成功后 DXY 超时 → 下一轮从 DXY 继续（不重复抓 XAUUSD）。"""
    seed_instruments(session)
    source = make_source(
        name="market-multi",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        config_json={"symbols": ["XAUUSD", "DXY"], "timeframes": ["1m"]},
    )

    first_transport = mock_transport(
        [
            _chart_response([_bar_row(0)]),
            TransportTimeoutError("第二页超时#1"),
            TransportTimeoutError("第二页超时#2"),
            TransportTimeoutError("第二页超时#3"),
        ]
    )
    first = await run_collector(
        session, MarketCollector(source, transport=first_transport), window=WINDOW
    )

    assert first.status is CollectorRunStatus.PARTIAL_FAILED
    assert first.outcome is not None
    assert first.outcome.cursor is not None
    assert first.outcome.cursor["plan_index"] == 1
    assert len(_bars(session)) == 1
    # XAUUSD 成功 1 次 + DXY 重试 3 次
    assert len(first_transport.requests) == 4
    assert "XAUUSD" in first_transport.requests[0].url
    assert "DXY" in first_transport.requests[-1].url

    second_transport = mock_transport([_chart_response([_bar_row(5)])])
    second = await run_collector(
        session, MarketCollector(source, transport=second_transport), window=WINDOW
    )

    assert second.status is CollectorRunStatus.SUCCESS
    assert second.resumed_from_cursor is True
    assert "DXY" in second_transport.requests[0].url  # ★ 从断点继续，而不是重新抓 XAUUSD
    assert len(_bars(session)) == 2


def test_provider_symbol_mapping_is_used_only_in_request_url(
    session: Session, make_source: Any
) -> None:
    """**回归测试（实测 HTTP 403，2026-09-15）**：`provider_symbols` 映射必须生效。

    Yahoo 没有 `XAUUSD`/`DXY` 这些项目代码（`XAUUSD` → 403/404、裸 `DXY` → HTTP 200 但 0 根 bar），
    采集器把项目标的直接当 ticker 用会**全量失败**。映射**只影响请求 URL**：
    `instruments` 解析、`raw_items` 幂等键、去重仍使用项目标的。
    """
    from src.collectors.market import MarketCollector

    seed_instruments(session)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    window = CollectWindow(start_at=start, end_at=start + timedelta(days=1))

    mapped = make_source(
        name="market_mapped",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        enabled=True,
        config_json={
            "symbols": ["XAUUSD"],
            "timeframes": ["1d"],
            "provider_symbols": {"XAUUSD": "GC=F"},
        },
    )
    collector = MarketCollector(mapped, instrument_symbols=("XAUUSD",), timeframes=("1d",))

    request = collector._build_request(symbol="XAUUSD", timeframe="1d", window=window)

    assert request.url == "https://market.invalid/v8/finance/chart/GC=F"
    assert request.params is not None
    assert request.params["interval"] == "1d"

    # 未配置映射时回落到项目标的本身（向后兼容）
    plain = make_source(
        name="market_plain",
        source_type=SourceType.MARKET,
        base_url="https://market.invalid",
        enabled=True,
        config_json={"symbols": ["XAUUSD"], "timeframes": ["1d"]},
    )
    plain_collector = MarketCollector(plain, instrument_symbols=("XAUUSD",), timeframes=("1d",))

    assert plain_collector._build_request(
        symbol="XAUUSD", timeframe="1d", window=window
    ).url.endswith("/chart/XAUUSD")


# ---------------------------------------------------------------------------
# 配置 ↔ 注册表 ↔ 采集器 接线
# ---------------------------------------------------------------------------
def test_seeded_market_source_resolves_to_market_collector(
    session: Session, mock_transport
) -> None:
    """种子里的 ``market_yahoo`` 配置必须能直接解析为 ``MarketCollector``（配置与代码一致）。"""
    seed_sources(session)
    source = session.scalar(sa.select(Source).where(Source.name == "market_yahoo"))
    assert source is not None

    collector = collector_for_source(source, transport=mock_transport([]))

    assert isinstance(collector, MarketCollector)
    assert set(collector.symbols) == {"XAUUSD", "DXY", "US10Y", "USDCNY"}
    # 种子列出 4h，但 provider 不支持 → 构造时已过滤（运行期仍会告警）
    assert "4h" not in collector.timeframes
    assert "1m" in collector.timeframes
