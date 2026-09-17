"""``MarketCollector`` 单元测试（纯解析 / 配置校验，不触数据库、不触网络）。

覆盖：
- Yahoo chart JSON 解析（正常 / 空 bar / 非法 OHLC / 重复时间戳 / provider 报错）；
- Decimal 精度与 UTC 分钟对齐；
- K 线缺口检测（"缺了 1 分钟"必须被识别）；
- 采集器配置校验（provider 白名单、标的与周期缺失）；
- 请求参数构造（interval / period1 / period2，主机名来自数据源配置）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from database.models import Source
from database.models.enums import SourceType
from src.collectors.errors import CollectorError
from src.collectors.market import (
    DEFAULT_TIMEFRAMES,
    YAHOO_INTERVAL_BY_TIMEFRAME,
    MarketBarPoint,
    MarketCollector,
    detect_bar_gaps,
    parse_yahoo_chart,
    to_decimal,
)
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.unit

BASE_EPOCH = int(datetime(2026, 9, 12, 8, 0, tzinfo=UTC).timestamp())


def _chart_payload(
    bars: list[dict[str, Any]],
    *,
    error: Any = None,
    include_result: bool = True,
) -> dict[str, Any]:
    """构造 Yahoo chart 风格的响应体（仅供 Mock，不含任何真实请求）。"""
    chart: dict[str, Any] = {"error": error, "result": None}
    if include_result:
        chart["result"] = [
            {
                "meta": {"symbol": "XAUUSD", "currency": "USD"},
                "timestamp": [bar["ts"] for bar in bars],
                "indicators": {
                    "quote": [
                        {
                            "open": [bar.get("open") for bar in bars],
                            "high": [bar.get("high") for bar in bars],
                            "low": [bar.get("low") for bar in bars],
                            "close": [bar.get("close") for bar in bars],
                            "volume": [bar.get("volume") for bar in bars],
                        }
                    ]
                },
            }
        ]
    return {"chart": chart}


def _bar(minute_offset: int, *, price: float = 2400.0, volume: float = 100.0) -> dict[str, Any]:
    return {
        "ts": BASE_EPOCH + minute_offset * 60,
        "open": price,
        "high": price + 5,
        "low": price - 5,
        "close": price + 1,
        "volume": volume,
    }


def _source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "name": "market-unit",
        "source_type": SourceType.MARKET,
        "base_url": "https://market.invalid",
        "enabled": True,
        "config_json": {"symbols": ["XAUUSD"], "timeframes": ["1m"]},
    }
    defaults.update(overrides)
    return Source(**defaults)  # type: ignore[arg-type]


def _window(minutes: int = 10) -> CollectWindow:
    start = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    return CollectWindow(start_at=start, end_at=start + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_parse_valid_chart_returns_utc_minute_bars() -> None:
    parsed = parse_yahoo_chart(_chart_payload([_bar(0), _bar(1)]), symbol="XAUUSD", timeframe="1m")

    assert parsed.skipped == 0
    assert parsed.total == 2
    first = parsed.points[0]
    assert first.open_time == datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    assert first.open_time.second == 0
    assert first.open_time.microsecond == 0
    assert first.close_time == first.open_time + timedelta(minutes=1)
    assert first.open == Decimal("2400.00000000")
    assert first.high == Decimal("2405.00000000")
    assert first.low == Decimal("2395.00000000")
    assert first.close == Decimal("2401.00000000")
    assert first.volume == Decimal("100.00000000")


def test_parse_rejects_unclosed_bars_when_not_after_given() -> None:
    """**回归测试（实测 2026-09-15）**：只入库**已收盘** K 线。

    Yahoo 会在最后一根返回一根时间戳 = 抓取墙钟的"进行中"bar
    （`USDCNY 1d` 连续三轮分别落在 `05:04:08` / `05:12:48` / `05:19:02`，
    `XAUUSD 1h` 出现 `04:58:13` / `05:08:45`）——它永远不收盘、每轮都产生新主键，
    会让 `market_bars` 无界膨胀，并可能把"未收盘数据"喂进特征（泄漏）。
    """
    moment = datetime(2026, 9, 12, 8, 1, 30, tzinfo=UTC)
    parsed = parse_yahoo_chart(
        _chart_payload([_bar(0), _bar(1), _bar(2)]),
        symbol="XAUUSD",
        timeframe="1m",
        not_after=moment,
    )

    # 08:00 那根收盘于 08:01（已收盘，保留）；08:01/08:02 尚未收盘 → 拒绝
    assert [point.open_time.minute for point in parsed.points] == [0]
    assert parsed.unclosed == 2
    assert parsed.total == 3  # 仍计入"provider 返回总数"（数据质量统计用）


def test_parse_without_not_after_keeps_every_bar() -> None:
    """缺省 `not_after=None` → 不做未收盘过滤（保持纯解析语义，向后兼容）。"""
    parsed = parse_yahoo_chart(_chart_payload([_bar(0), _bar(1)]), symbol="XAUUSD", timeframe="1m")

    assert len(parsed.points) == 2
    assert parsed.unclosed == 0


def test_parse_rejects_wall_clock_timestamp_bar() -> None:
    """非整点（= 抓取瞬间）的时间戳同样被拒：它代表一根永不收盘的 bar。"""
    moment = datetime(2026, 9, 15, 5, 19, 2, tzinfo=UTC)
    wall_clock_bar = {
        "ts": int(moment.timestamp()),
        "open": 2400.0,
        "high": 2405.0,
        "low": 2395.0,
        "close": 2401.0,
        "volume": 100.0,
    }
    parsed = parse_yahoo_chart(
        _chart_payload([_bar(0), wall_clock_bar]),
        symbol="USDCNY",
        timeframe="1h",
        not_after=moment,
    )

    assert [point.open_time for point in parsed.points] == [datetime(2026, 9, 12, 8, 0, tzinfo=UTC)]
    assert parsed.unclosed == 1


def test_parse_sorts_bars_by_open_time() -> None:
    parsed = parse_yahoo_chart(
        _chart_payload([_bar(2), _bar(0), _bar(1)]), symbol="XAUUSD", timeframe="1m"
    )
    assert [point.open_time.minute for point in parsed.points] == [0, 1, 2]


def test_parse_skips_empty_and_invalid_bars() -> None:
    bars = [
        _bar(0),
        {
            "ts": BASE_EPOCH + 60,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
        },
        {
            "ts": BASE_EPOCH + 120,
            "open": 2400,
            "high": 2390,
            "low": 2395,
            "close": 2401,
            "volume": 1,
        },
        {"ts": BASE_EPOCH + 180, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 1},
    ]
    parsed = parse_yahoo_chart(_chart_payload(bars), symbol="XAUUSD", timeframe="1m")

    assert len(parsed.points) == 1
    assert parsed.skipped == 3
    assert parsed.total == 4
    assert parsed.skipped_examples == (BASE_EPOCH + 60,)


def test_parse_deduplicates_repeated_timestamps() -> None:
    parsed = parse_yahoo_chart(_chart_payload([_bar(0), _bar(0)]), symbol="XAUUSD", timeframe="1m")
    assert len(parsed.points) == 1


def test_parse_raises_on_provider_error() -> None:
    payload = _chart_payload([], error={"code": "Not Found", "description": "No data found"})

    with pytest.raises(CollectorError) as excinfo:
        parse_yahoo_chart(payload, symbol="NOPE", timeframe="1m")

    assert "行情接口返回错误" in str(excinfo.value)
    assert excinfo.value.details["provider_error"]["code"] == "Not Found"


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"chart": {}}, {"chart": {"error": None, "result": []}}, "not-a-mapping"],
)
def test_parse_raises_on_malformed_payload(payload: object) -> None:
    with pytest.raises(CollectorError):
        parse_yahoo_chart(payload, symbol="XAUUSD", timeframe="1m")  # type: ignore[arg-type]


def test_parse_rejects_provider_unsupported_timeframe() -> None:
    with pytest.raises(CollectorError, match="不支持的周期"):
        parse_yahoo_chart(_chart_payload([_bar(0)]), symbol="XAUUSD", timeframe="4h")


def test_to_decimal_quantizes_to_eight_places() -> None:
    assert to_decimal("2400.123456789") == Decimal("2400.12345679")
    assert to_decimal(2400) == Decimal("2400.00000000")
    assert to_decimal(None) is None
    assert to_decimal("abc") is None


def test_market_bar_point_record_key_is_deterministic() -> None:
    point = MarketBarPoint(
        open_time=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
        close_time=datetime(2026, 9, 12, 8, 1, tzinfo=UTC),
        open=Decimal("2400"),
        high=Decimal("2401"),
        low=Decimal("2399"),
        close=Decimal("2400.5"),
    )
    assert point.record_key("XAUUSD", "1m") == f"XAUUSD:1m:{BASE_EPOCH}"


# ---------------------------------------------------------------------------
# 缺口检测
# ---------------------------------------------------------------------------
def test_detect_bar_gaps_finds_missing_minute() -> None:
    times = [
        datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 12, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 12, 8, 3, tzinfo=UTC),
    ]
    gaps = detect_bar_gaps(times, timeframe="1m")
    assert gaps == (datetime(2026, 9, 12, 8, 2, tzinfo=UTC),)


def test_detect_bar_gaps_returns_empty_when_contiguous() -> None:
    times = [datetime(2026, 9, 12, 8, minute, tzinfo=UTC) for minute in range(5)]
    assert detect_bar_gaps(times, timeframe="1m") == ()


def test_detect_bar_gaps_reports_multiple_missing_bars() -> None:
    times = [
        datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 12, 8, 5, tzinfo=UTC),
    ]
    gaps = detect_bar_gaps(times, timeframe="1m")
    assert len(gaps) == 4
    assert gaps[0] == datetime(2026, 9, 12, 8, 1, tzinfo=UTC)


def test_detect_bar_gaps_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="未知周期"):
        detect_bar_gaps([], timeframe="7m")


# ---------------------------------------------------------------------------
# 采集器配置与请求构造
# ---------------------------------------------------------------------------
def test_collector_plan_is_cartesian_product() -> None:
    collector = MarketCollector(
        _source(config_json={"symbols": ["XAUUSD", "DXY"], "timeframes": ["1m", "5m"]})
    )
    assert collector.symbols == ("XAUUSD", "DXY")
    assert collector.timeframes == ("1m", "5m")
    assert collector._plan == (  # noqa: SLF001 - 计划顺序是断点续采的契约，必须断言
        ("XAUUSD", "1m"),
        ("XAUUSD", "5m"),
        ("DXY", "1m"),
        ("DXY", "5m"),
    )


def test_collector_defaults_to_one_minute_timeframe() -> None:
    collector = MarketCollector(_source(config_json={"symbols": ["XAUUSD"]}))
    assert collector.timeframes == DEFAULT_TIMEFRAMES


def test_collector_skips_provider_unsupported_timeframes() -> None:
    collector = MarketCollector(
        _source(config_json={"symbols": ["XAUUSD"], "timeframes": ["1m", "4h"]})
    )
    assert collector.timeframes == ("1m",)
    assert "4h" not in YAHOO_INTERVAL_BY_TIMEFRAME


def test_collector_rejects_unknown_provider() -> None:
    with pytest.raises(CollectorError, match="provider"):
        MarketCollector(_source(config_json={"symbols": ["XAUUSD"], "provider": "alphavantage"}))


def test_collector_requires_symbols() -> None:
    with pytest.raises(CollectorError, match="symbols"):
        MarketCollector(_source(config_json={}))


def test_collector_rejects_purely_unsupported_timeframes() -> None:
    with pytest.raises(CollectorError, match="支持"):
        MarketCollector(_source(config_json={"symbols": ["XAUUSD"], "timeframes": ["4h"]}))


def test_build_request_uses_source_base_url_and_window_bounds() -> None:
    collector = MarketCollector(_source(base_url="https://market.invalid/"))
    window = _window(60)

    request = collector._build_request(symbol="XAUUSD", timeframe="1m", window=window)  # noqa: SLF001

    assert request.url == "https://market.invalid/v8/finance/chart/XAUUSD"
    assert request.params["interval"] == "1m"
    assert request.params["period1"] == int(window.start_utc.timestamp())
    assert request.params["period2"] == int(window.end_utc.timestamp())
    assert request.params["includePrePost"] == "false"


def test_build_request_requires_base_url() -> None:
    collector = MarketCollector(_source(base_url=None))

    with pytest.raises(CollectorError, match="base_url"):
        collector._build_request(symbol="XAUUSD", timeframe="1m", window=_window())  # noqa: SLF001


def test_expected_min_records_scales_with_window_and_plan() -> None:
    collector = MarketCollector(
        _source(config_json={"symbols": ["XAUUSD", "DXY"], "timeframes": ["1m"]})
    )
    # 10 分钟窗口、2 个组合、每组合留 1 根边界容差 → (10 - 1) * 2
    assert collector._expected_min_records(_window(10)) == 18  # noqa: SLF001


def test_cursor_walks_the_plan_and_ends_with_none() -> None:
    collector = MarketCollector(
        _source(config_json={"symbols": ["XAUUSD", "DXY"], "timeframes": ["1m"]})
    )

    assert collector._next_cursor(0) == {  # noqa: SLF001
        "plan_index": 1,
        "symbol": "DXY",
        "timeframe": "1m",
    }
    assert collector._next_cursor(1) is None  # noqa: SLF001
