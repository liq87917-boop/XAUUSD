"""``MacroCollector`` 解析层单元测试（纯函数，不触数据库、不触网络）。

覆盖团队批复的关键点：
- FRED 观测解析：正常值、缺失值（``.``）、缺失/非法日期、未来日期、重复日期；
- **时间对齐**：``event_at`` 为 UTC 当日 00:00，且保持 provider 原始粒度（日/月/季不拉平）；
- 配置校验：series 归一化、provider 白名单、**API Key 只从环境变量读取**且 URL 脱敏；
- CSV 兜底解析；
- 回看窗口计算（宏观观测频率低 → 复采近期观测，靠幂等键去重）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from database.models import Source
from database.models.enums import SourceType
from src.collectors.errors import CollectorError
from src.collectors.macro import (
    MacroCollector,
    MacroObservation,
    MacroSeriesSpec,
    parse_fred_observations,
    parse_macro_csv,
    parse_observation_date,
    parse_realtime_boundary,
    to_value_decimal,
)
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.unit

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
    end_at=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
)
SPEC = MacroSeriesSpec(series_id="CPIAUCSL", country="US", unit="index")


def _obs(day: str, value: object, **extra: Any) -> dict[str, Any]:
    return {
        "realtime_start": "2026-09-12",
        "realtime_end": "9999-12-31",
        "date": day,
        "value": value,
        **extra,
    }


def _fred_payload(observations: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "realtime_start": "2026-09-12",
        "realtime_end": "2026-09-12",
        "units": "Index 2017=100",
        "observations": observations,
        **extra,
    }


def _source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "name": "macro-unit",
        "source_type": SourceType.MACRO,
        "base_url": "https://api.stlouisfed.org",
        "enabled": True,
        "config_json": {
            "provider": "fred",
            "series": [{"series_id": "CPIAUCSL", "country": "US", "unit": "index"}],
            "api_key_env": "GOLD_AI_TEST_FRED_KEY",
            "lookback_days": 10,
        },
    }
    defaults.update(overrides)
    return Source(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 值 / 日期解析
# ---------------------------------------------------------------------------
def test_to_value_decimal_quantizes_and_rejects_missing_marker() -> None:
    assert to_value_decimal("319.6") == Decimal("319.6000000000")
    assert to_value_decimal(3) == Decimal("3.0000000000")
    assert to_value_decimal(".") is None  # FRED 缺失标记，绝不能当作 0
    assert to_value_decimal("") is None
    assert to_value_decimal(None) is None
    assert to_value_decimal("abc") is None


def test_parse_observation_date_returns_utc_midnight() -> None:
    parsed = parse_observation_date("2026-07-01")
    assert parsed == datetime(2026, 7, 1, tzinfo=UTC)
    assert parsed is not None
    assert parsed.hour == 0
    assert parse_observation_date("2026-13-45") is None
    assert parse_observation_date("") is None
    assert parse_observation_date(None) is None


def test_realtime_boundary_uses_conservative_next_day() -> None:
    assert parse_realtime_boundary("2026-07-15") == datetime(2026, 7, 16, tzinfo=UTC)
    assert parse_realtime_boundary("9999-12-31", open_ended=True) is None


def test_observation_record_id_is_stable() -> None:
    observation = MacroObservation(
        series_id="CPIAUCSL",
        event_at=datetime(2026, 7, 1, tzinfo=UTC),
        released_at=datetime(2026, 8, 13, tzinfo=UTC),
        vintage_end_at=None,
        value=Decimal("319.6"),
    )
    assert observation.record_id == "CPIAUCSL:2026-07-01:2026-08-13"


# ---------------------------------------------------------------------------
# FRED 解析
# ---------------------------------------------------------------------------
def test_parse_fred_observations_normal_case() -> None:
    payload = _fred_payload([_obs("2026-07-01", "319.6"), _obs("2026-08-01", "320.9")])

    parsed = parse_fred_observations(payload, spec=SPEC, window_end=WINDOW.end_utc)

    assert parsed.skipped == 0
    assert parsed.total == 2
    assert parsed.points[0].event_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert parsed.points[0].value == Decimal("319.6000000000")
    assert parsed.points[0].unit == "Index 2017=100"  # 优先取响应中的 units
    assert [point.event_at.month for point in parsed.points] == [7, 8]


def test_parse_fred_observations_keeps_monthly_granularity() -> None:
    """月度序列保持原粒度：日期仍是 provider 给出的月首日，不重采样、不拉平。"""
    payload = _fred_payload([_obs("2026-06-01", "1.0"), _obs("2026-07-01", "2.0")])

    parsed = parse_fred_observations(payload, spec=SPEC)

    assert parsed.points[0].event_at.date() == date(2026, 6, 1)
    assert parsed.points[1].event_at.date() == date(2026, 7, 1)


def test_parse_fred_observations_skips_missing_and_invalid() -> None:
    payload = _fred_payload(
        [
            _obs("2026-07-01", "319.6"),
            _obs("2026-08-01", "."),  # 缺失值
            _obs("not-a-date", "1.0"),  # 非法日期
            _obs("2026-09-01", "abc"),  # 非数值
            "not-a-mapping",  # 结构异常
        ]
    )

    parsed = parse_fred_observations(payload, spec=SPEC)

    assert len(parsed.points) == 1
    assert parsed.skipped == 4
    assert set(parsed.skip_reasons) >= {
        "missing_value",
        "missing_or_invalid_date",
        "malformed_entry",
    }


def test_parse_fred_observations_rejects_future_dates() -> None:
    payload = _fred_payload([_obs("2026-07-01", "1.0"), _obs("2027-01-01", "9.9")])

    parsed = parse_fred_observations(payload, spec=SPEC, window_end=WINDOW.end_utc)

    assert len(parsed.points) == 1
    assert parsed.skipped == 1
    assert "future_date" in parsed.skip_reasons


def test_parse_fred_observations_deduplicates_dates() -> None:
    payload = _fred_payload([_obs("2026-07-01", "1.0"), _obs("2026-07-01", "2.0")])

    parsed = parse_fred_observations(payload, spec=SPEC)

    assert len(parsed.points) == 1
    assert parsed.skipped == 1
    assert "duplicate_vintage" in parsed.skip_reasons


def test_parse_fred_observations_requires_release_boundary() -> None:
    payload = _fred_payload([_obs("2026-07-01", "1.0", realtime_start="")])

    with pytest.raises(CollectorError, match="realtime_start"):
        parse_fred_observations(payload, spec=SPEC)


def test_initial_release_response_does_not_treat_query_end_as_vintage_end() -> None:
    payload = _fred_payload(
        [_obs("2026-07-01", "1.0", realtime_end="2026-09-16")], output_type=4
    )

    parsed = parse_fred_observations(payload, spec=SPEC)

    assert parsed.points[0].released_at == datetime(2026, 9, 13, tzinfo=UTC)
    assert parsed.points[0].vintage_end_at is None


def test_parse_fred_error_message_raises() -> None:
    payload = {"error_code": 400, "error_message": "Bad Request. invalid api_key"}

    with pytest.raises(CollectorError) as excinfo:
        parse_fred_observations(payload, spec=SPEC)

    assert "FRED 返回错误" in str(excinfo.value)
    assert excinfo.value.details["error_code"] == 400


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"observations": "not-a-list"}, "not-a-mapping"],
)
def test_parse_fred_rejects_malformed_payload(payload: object) -> None:
    with pytest.raises(CollectorError):
        parse_fred_observations(payload, spec=SPEC)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CSV 兜底解析
# ---------------------------------------------------------------------------
def test_parse_macro_csv_reads_required_columns() -> None:
    csv_text = (
        "series_id,date,released_at,value,unit\n"
        "CPIAUCSL,2026-06-01,2026-07-15,318.1,index\n"
        "DFF,2026-07-01,2026-07-02,4.33,percent\n"
    )

    parsed = parse_macro_csv(csv_text)

    assert parsed.skipped == 0
    assert [(point.series_id, point.event_at.date().isoformat()) for point in parsed.points] == [
        ("CPIAUCSL", "2026-06-01"),
        ("DFF", "2026-07-01"),
    ]
    assert [point.released_at.date().isoformat() for point in parsed.points] == [
        "2026-07-15",
        "2026-07-02",
    ]
    assert parsed.points[0].value == Decimal("318.1000000000")
    assert parsed.points[0].unit == "index"


def test_parse_macro_csv_skips_incomplete_rows() -> None:
    csv_text = (
        "series_id,date,released_at,value\n"
        "DFF,2026-07-01,2026-07-02,.\n"
        "DFF,2026-07-02,2026-07-03,4.33\n"
    )

    parsed = parse_macro_csv(csv_text)

    assert len(parsed.points) == 1
    assert parsed.skipped == 1
    assert "incomplete_row" in parsed.skip_reasons


def test_parse_macro_csv_rejects_missing_release_value() -> None:
    csv_text = "series_id,date,released_at,value\nDFF,2026-07-01,,4.33\n"

    with pytest.raises(CollectorError, match="released_at"):
        parse_macro_csv(csv_text)


@pytest.mark.parametrize(
    ("csv_text", "expected"),
    [
        ("", "为空"),
        ("date,value\n2026-07-01,1.0\n", "缺少必需列"),
        ("series_id,date\nDFF,2026-07-01\n", "缺少必需列"),
    ],
)
def test_parse_macro_csv_rejects_invalid_input(csv_text: str, expected: str) -> None:
    with pytest.raises(CollectorError, match=expected):
        parse_macro_csv(csv_text)


# ---------------------------------------------------------------------------
# 采集器配置 / 密钥 / 回看窗口
# ---------------------------------------------------------------------------
def test_collector_requires_series_or_csv_path() -> None:
    with pytest.raises(CollectorError, match="未配置序列"):
        MacroCollector(_source(config_json={"provider": "fred"}))


def test_collector_rejects_unknown_provider() -> None:
    with pytest.raises(CollectorError, match="provider"):
        MacroCollector(_source(config_json={"provider": "trading-economics"}))


def test_collector_rejects_custom_key_environment_name(monkeypatch) -> None:
    """密钥只能通过集中配置的 FRED_API_KEY 读取，禁止业务代码散读环境变量。"""
    monkeypatch.delenv("GOLD_AI_TEST_FRED_KEY", raising=False)

    with pytest.raises(CollectorError) as excinfo:
        MacroCollector(_source())

    message = str(excinfo.value)
    assert "GOLD_AI_TEST_FRED_KEY" in message
    assert "集中配置" in message
    assert "FRED_API_KEY" in message


def test_collector_reads_api_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GOLD_AI_TEST_FRED_KEY", "secret-key-value")

    with pytest.raises(CollectorError, match="集中配置"):
        MacroCollector(_source())


def test_collector_reads_api_key_from_central_settings(monkeypatch) -> None:
    from config.settings import reset_settings_cache

    monkeypatch.setenv("FRED_API_KEY", "secret-key-value")
    reset_settings_cache()
    source = _source(
        config_json={
            "provider": "fred",
            "series": ["CPIAUCSL"],
            "api_key_env": "FRED_API_KEY",
        }
    )
    collector = MacroCollector(source)

    assert collector._api_key == "secret-key-value"  # noqa: SLF001 - 断言注入来源
    reset_settings_cache()


def test_collector_normalizes_series_configurations() -> None:
    collector = MacroCollector(
        _source(
            config_json={
                "series": [
                    "CPIAUCSL",
                    {"series_id": "DFF", "country": "US", "unit": "percent"},
                    "CPIAUCSL",  # 重复项应被去重
                ]
            }
        ),
        api_key="k",
    )

    assert [spec.series_id for spec in collector._specs] == ["CPIAUCSL", "DFF"]  # noqa: SLF001
    assert collector._specs[1].unit == "percent"  # noqa: SLF001


def test_collector_rejects_series_entry_without_id() -> None:
    with pytest.raises(CollectorError, match="series_id"):
        MacroCollector(_source(config_json={"series": [{"country": "US"}]}), api_key="k")


def test_collector_rejects_invalid_lookback() -> None:
    with pytest.raises(CollectorError, match="lookback_days"):
        MacroCollector(_source(), api_key="k", lookback_days=0)


def test_masked_url_never_contains_api_key() -> None:
    collector = MacroCollector(_source(), api_key="super-secret")

    request = collector._build_request(SPEC, WINDOW)  # noqa: SLF001
    masked = collector._masked_url(request)  # noqa: SLF001

    assert request.params["api_key"] == "super-secret"  # 真实请求必须带 key
    assert "super-secret" not in masked
    assert "api_key=***" in masked
    assert request.params["observation_start"] == "2026-09-02"  # 回看 10 天
    assert request.params["output_type"] == 4
    assert request.params["realtime_start"] == "2026-09-11"
    assert request.params["realtime_end"] == "2026-09-11"


def test_observation_range_uses_lookback_window() -> None:
    collector = MacroCollector(_source(), api_key="k", lookback_days=45)

    start, end = collector._observation_range(WINDOW)  # noqa: SLF001

    assert end == date(2026, 9, 12)
    assert start == date(2026, 7, 29)  # 9/12 - 45 天


def test_csv_mode_takes_precedence_and_needs_no_api_key() -> None:
    collector = MacroCollector(_source(config_json={"csv_path": "data/raw/macro.csv"}))

    assert collector._mode == "csv"  # noqa: SLF001
    assert collector._api_key is None  # noqa: SLF001


async def test_health_check_fred_mode_reports_healthy() -> None:
    collector = MacroCollector(_source(), api_key="k")

    report = await collector.health_check()

    assert report.healthy is True
    assert report.details["base_url"] == "https://api.stlouisfed.org"


async def test_health_check_csv_mode_detects_missing_file(tmp_path: Path) -> None:
    collector = MacroCollector(
        _source(config_json={"csv_path": str(tmp_path / "missing.csv")})
    )

    report = await collector.health_check()

    assert report.healthy is False
    assert "不存在" in (report.message or "")


async def test_health_check_csv_mode_reports_healthy(tmp_path: Path) -> None:
    csv_file = tmp_path / "macro_fixture.csv"
    csv_file.write_text("series_id,date,value\nDFF,2026-07-01,4.33\n", encoding="utf-8")
    collector = MacroCollector(_source(config_json={"csv_path": str(csv_file)}))

    report = await collector.health_check()

    assert report.healthy is True
    assert report.details["mode"] == "csv"


