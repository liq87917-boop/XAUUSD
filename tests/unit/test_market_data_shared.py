"""W0-1 共享工具与两个脚本的单元测试（**全离线**：合成 fixture + monkeypatch，不联网、不碰数据库）。

覆盖重点：

1. **取数层**：空槽必须保留（审计对象）、坏时间戳计数、**必须带 UA**（无 UA → 429）、
   候选链回退、"HTTP 200 但 0 根有效 bar"必须视为失败、不支持的标的显式报错；
2. **会话日历**：休市桶由数据标定（周末 / 每日结算间隙），不硬编码 DST/假期；
3. **缺口四分类**：`session_break` / `holiday_suspect` / `data_gap` / `missing_timestamp`，
   且**干净数据必须 0 异常**（不误报）；
4. **TD-03 4h 聚合**：UTC 4h 桶、OHLC 正确、**半桶丢弃**、桶起点校验；
5. **CLI**：`--dry-run` 不写盘、flag 冲突退 2、非法参数退 2、报告必备章节。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts._market_data import (
    CATEGORY_DATA_GAP,
    CATEGORY_HOLIDAY_SUSPECT,
    CATEGORY_MISSING_TIMESTAMP,
    CATEGORY_SESSION_BREAK,
    DEFAULT_SESSION_TZ,
    PROVIDER_SYMBOLS,
    UNSUPPORTED_SYMBOLS,
    ChartFetch,
    ChartSlot,
    SourceBar,
    aggregate_bars_to_4h,
    audit_gaps,
    bucket_start,
    build_session_profile,
    fetch_chart,
    parse_chart_slots,
)
from scripts.audit_market_gaps import (
    audit_rows,
    finding_rows,
    select_items,
)
from scripts.audit_market_gaps import (
    main as audit_main,
)
from scripts.audit_market_gaps import (
    render_report as audit_render_report,
)
from scripts.backfill_market_bars import (
    main as backfill_main,
)
from scripts.backfill_market_bars import (
    mask_db_url,
    parse_csv_list,
    plan_windows,
)
from src.collectors.transport import HttpRequest

pytestmark = pytest.mark.unit

#: 2026-01-05 是**周一**（构造会话日历用）
MONDAY: datetime = datetime(2026, 1, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 夹具：合成槽（保留空槽，用于审计）
# ---------------------------------------------------------------------------
def _null_slot(moment: datetime) -> ChartSlot:
    return ChartSlot(open_time=moment)


def _filled_slot(moment: datetime) -> ChartSlot:
    return ChartSlot(moment, open=1800.0, high=1801.0, low=1799.0, close=1800.5, volume=10.0)


def _chart_payload(timestamps: list[object], closes: list[float | None]) -> dict[str, object]:
    """Yahoo chart JSON（与真实结构一致的最小 payload）。"""
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [None if v is None else v - 1 for v in closes],
                                "high": [None if v is None else v + 1 for v in closes],
                                "low": [None if v is None else v - 1 for v in closes],
                                "close": closes,
                                "volume": [1 for _ in closes],
                            }
                        ]
                    },
                }
            ],
        }
    }


def _week_slots(
    *,
    weeks: int = 3,
    whole_day_null: tuple[int, int] | None = None,
    gap_day: tuple[int, int] | None = None,
    gap_hours: tuple[int, ...] = (),
    drop_day: tuple[int, int] | None = None,
    drop_hours: tuple[int, ...] = (),
) -> list[ChartSlot]:
    """构造 1h 槽：**按交易所本地时间（美东）定义会话** —— 工作日 `00:00–20:00` 有 bar，
    `21:00–23:00` 与周末为空（模拟每日结算间隙与周末），返回 UTC 时刻的槽（与 provider 一致）。

    这样构造是为了**不把 DST 假象写进夹具**：真实 provider 的 1h bar 在 UTC 小时上随 DST 漂移，
    但在美东本地时间上稳定。

    - `whole_day_null=(week, day)`：某本地工作日**整日空 bar**（疑似假期）；
    - `gap_day/gap_hours`：某本地工作日开市时段内注入**空 bar**（数据缺失）；
    - `drop_day/drop_hours`：某本地工作日开市时段内**连时间戳都不返回**（时间戳缺失）。
    """
    tz = ZoneInfo(DEFAULT_SESSION_TZ)
    base_local = datetime(2026, 1, 5, tzinfo=tz)  # 本地周一
    slots: list[ChartSlot] = []
    for week in range(weeks):
        for day in range(7):
            for hour in range(24):
                local = base_local + timedelta(days=week * 7 + day, hours=hour)
                is_open_bucket = local.weekday() < 5 and hour < 21
                if whole_day_null == (week, day):
                    is_open_bucket = False
                if is_open_bucket and drop_day == (week, day) and hour in drop_hours:
                    continue
                moment = local.astimezone(UTC)
                if is_open_bucket and gap_day == (week, day) and hour in gap_hours:
                    slots.append(_null_slot(moment))
                    continue
                slots.append(_filled_slot(moment) if is_open_bucket else _null_slot(moment))
    return slots


# ---------------------------------------------------------------------------
# 1) 解析与取数
# ---------------------------------------------------------------------------
def test_parse_chart_slots_keeps_null_slots_and_counts_bad_timestamps() -> None:
    """**空槽必须保留**（它是审计对象）；只有坏时间戳计入 `bad_timestamps`。"""
    payload = _chart_payload([1700000000, 1700003600, "bad"], [1800.0, None, None])

    slots, bad = parse_chart_slots(payload)

    assert len(slots) == 2
    assert [slot.is_null for slot in slots] == [False, True]
    assert bad == 1
    assert str(slots[0].open_time.tzinfo) == "UTC"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"chart": {"error": {"code": 1}, "result": None}}, "行情接口返回错误"),
        ({"nope": 1}, "缺少 chart 字段"),
        ({"chart": {"error": None, "result": []}}, "缺少 result"),
        ("nope", "不是 JSON 对象"),
    ],
)
def test_parse_chart_slots_rejects_broken_payload(payload: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chart_slots(payload)  # type: ignore[arg-type]


def test_fetch_chart_sends_user_agent_and_falls_through_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """必须带 UA（无 UA → 429）；候选链中途失败要自动落到下一个 ticker。"""
    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, status: int, payload: dict[str, object] | None = None) -> None:
            self.status_code = status
            self._payload = payload or {}

        def json(self) -> dict[str, object]:
            return self._payload

    def _fake_get(
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        calls.append({"url": url, "headers": dict(headers or {})})
        if "GC=F" in url:
            return _Response(429)  # 模拟首个候选被限流
        return _Response(200, _chart_payload([1700000000, 1700003600], [1800.0, 1801.0]))

    monkeypatch.setattr("httpx.get", _fake_get)

    fetch = fetch_chart("XAUUSD", interval="1h", lookback="2y")

    assert fetch.provider_symbol == PROVIDER_SYMBOLS["XAUUSD"][1]  # 回退到第二个候选
    assert fetch.total == 2
    assert len(fetch.valid_slots) == 2
    assert len(calls) == 2
    assert all(str(call["headers"].get("User-Agent", "")) for call in calls)


def test_fetch_chart_treats_http_200_with_zero_valid_bars_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实测教训：裸 `DXY`/`TNX` 返回 200 但 0 根 bar → **必须视为失败**，不能当成功。"""

    class _Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return _chart_payload([1700000000, 1700003600], [None, None])

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="0 根有效 K 线"):
        fetch_chart("DXY", interval="1d", lookback="1mo")


def test_fetch_chart_reports_all_failures_and_unsupported_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        status_code = 404

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="所有候选 ticker 均取数失败"):
        fetch_chart("USDCNY", interval="1d", lookback="1mo")

    with pytest.raises(RuntimeError, match="无可用行情源"):
        fetch_chart("US10Y_REAL", interval="1d", lookback="1mo")

    assert "US10Y_REAL" in UNSUPPORTED_SYMBOLS


def test_httpx_transport_maps_request_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归测试（实测 Yahoo 对 aiohttp 403）**：httpx 传输层必须带 UA 并正确映射响应。"""
    import asyncio

    from scripts._market_data import HttpxTransport

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"chart": {}}'

        def json(self) -> dict[str, object]:
            return {"chart": {}}

    def _fake_request(method: str, url: str, **kwargs: object) -> _Response:
        captured.update({"method": method, "url": url, **kwargs})
        return _Response()

    monkeypatch.setattr("httpx.request", _fake_request)

    transport = HttpxTransport(default_timeout_seconds=12.5)
    response = asyncio.run(
        transport.send(
            HttpRequest(url="https://example.invalid/chart/GC=F", params={"interval": "1d"})
        )
    )

    assert response.status == 200
    assert response.ok
    assert response.json_body == {"chart": {}}
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"] == "gold-ai-collector/0.2"
    assert captured["timeout"] == 12.5


def test_httpx_transport_maps_failures_to_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络异常必须映射为项目统一的 `TransportError`（交给采集器重试策略）。"""
    import asyncio

    from scripts._market_data import HttpxTransport
    from src.collectors.errors import TransportError

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("connection reset")

    monkeypatch.setattr("httpx.request", _boom)

    with pytest.raises(TransportError, match="httpx 请求失败"):
        asyncio.run(HttpxTransport().send(HttpRequest(url="https://example.invalid/x")))


# ---------------------------------------------------------------------------
# 2) 会话日历（数据驱动）与缺口四分类
# ---------------------------------------------------------------------------
def test_session_profile_marks_weekend_and_settlement_break_as_closed() -> None:
    profile = build_session_profile(_week_slots())

    assert profile.is_closed((5, 10))  # 周六
    assert profile.is_closed((6, 10))  # 周日
    assert profile.is_closed((0, 22))  # 周一 22:00（每日结算间隙）
    assert profile.is_open((0, 10))  # 周一 10:00（常规开市）
    assert profile.threshold == 0.02


def _fetch(slots: list[ChartSlot]) -> object:
    return ChartFetch(
        symbol="XAUUSD", interval="1h", lookback="2y", provider_symbol="GC=F", slots=tuple(slots)
    )


def test_audit_clean_data_has_zero_anomalies() -> None:
    """**不误报**：规律数据里周末/结算间隙算休市，异常必须是 0。"""
    audit = audit_gaps(_fetch(_week_slots()))  # type: ignore[arg-type]

    assert audit.counts.get(CATEGORY_SESSION_BREAK, 0) > 0
    assert audit.anomaly_count == 0
    assert audit.counts.get(CATEGORY_DATA_GAP, 0) == 0
    assert audit.missing_timestamps == 0
    summary = audit.summary_lines()[0]
    assert "常规休市（预期内）" in summary
    assert "ticker=GC=F" in summary


def test_audit_classifies_data_gap_on_open_hours() -> None:
    """常规开市时段内的空 bar = **数据缺失（异常）**。"""
    slots = _week_slots(gap_day=(1, 1), gap_hours=(10, 11))

    audit = audit_gaps(_fetch(slots))  # type: ignore[arg-type]

    assert audit.counts.get(CATEGORY_DATA_GAP, 0) == 2
    assert audit.longest_run[CATEGORY_DATA_GAP] == 2
    assert audit.anomaly_count == 2
    assert [f.category for f in audit.findings] == [CATEGORY_DATA_GAP, CATEGORY_DATA_GAP]


def test_audit_classifies_whole_weekday_as_holiday_suspect() -> None:
    """工作日整段无 bar = **疑似假期**（不是数据缺失，需人工确认）。"""
    slots = _week_slots(whole_day_null=(1, 2))  # 第二周的周三整日空

    audit = audit_gaps(_fetch(slots))  # type: ignore[arg-type]

    assert audit.counts.get(CATEGORY_HOLIDAY_SUSPECT, 0) == 21  # 本地 00:00–20:00 共 21 槽
    assert audit.counts.get(CATEGORY_DATA_GAP, 0) == 0
    # 判据写在 note 里（本地日期 + "疑似交易所假期"）；时间戳是 UTC，会跨两个 UTC 日期
    assert any(
        finding.category == CATEGORY_HOLIDAY_SUSPECT
        and "2026-01-14" in finding.note
        and "疑似交易所假期" in finding.note
        for finding in audit.findings
    )


def test_audit_counts_missing_timestamps() -> None:
    """provider **连时间戳都没返回**的"应有槽"→ 时间戳缺失（异常）。"""
    slots = _week_slots(drop_day=(1, 3), drop_hours=(9, 10, 11))

    audit = audit_gaps(_fetch(slots))  # type: ignore[arg-type]

    assert audit.missing_timestamps == 3
    assert audit.counts.get(CATEGORY_MISSING_TIMESTAMP, 0) == 3
    assert audit.anomaly_count == 3
    assert any("缺 3 个时间戳" in finding.note for finding in audit.findings)


def test_audit_handles_empty_payload() -> None:
    audit = audit_gaps(_fetch([]))  # type: ignore[arg-type]

    assert audit.total_slots == 0
    assert audit.counts == {}
    assert audit.anomaly_count == 0


# ---------------------------------------------------------------------------
# 3) TD-03：4h 聚合
# ---------------------------------------------------------------------------
def test_bucket_start_floors_to_4h_and_validates() -> None:
    moment = datetime(2026, 1, 5, 13, 45, tzinfo=UTC)

    assert bucket_start(moment) == datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    assert bucket_start(datetime(2026, 1, 5, 20, 0, tzinfo=UTC)).hour == 20
    with pytest.raises(ValueError, match="1440 的因子"):
        bucket_start(moment, bucket_minutes=0)
    with pytest.raises(ValueError, match="1440 的因子"):
        bucket_start(moment, bucket_minutes=7)


def _hourly_bars(hours: range, *, base: float = 1800.0) -> list[SourceBar]:
    return [
        SourceBar(
            open_time=MONDAY + timedelta(hours=hour),
            open=base + hour,
            high=base + hour + 2,
            low=base + hour - 1,
            close=base + hour + 1,
            volume=5.0,
        )
        for hour in hours
    ]


def test_aggregate_bars_to_4h_ohlc_and_skips_partial_buckets() -> None:
    """满桶聚合正确；**半桶必须丢弃**（半桶 close 不是该周期收盘价）。"""
    aggregated, skipped = aggregate_bars_to_4h(_hourly_bars(range(6)))

    assert len(aggregated) == 1
    assert len(skipped) == 1
    bar = aggregated[0]
    assert bar.open_time == MONDAY
    assert bar.close_time == MONDAY + timedelta(hours=4)
    assert bar.bars == 4
    assert bar.open == 1800.0  # 第一根 open
    assert bar.close == 1804.0  # 第四根（h=3）close = 1800+3+1
    assert bar.high == 1805.0  # max(1800+h+2, h=0..3)
    assert bar.low == 1799.0  # min(1800+h-1, h=0..3)
    assert bar.volume == 20.0
    assert skipped[0] == MONDAY + timedelta(hours=4)


def test_aggregate_bars_to_4h_volume_none_when_all_missing() -> None:
    bars = [
        SourceBar(MONDAY + timedelta(hours=hour), 1800.0, 1801.0, 1799.0, 1800.0)
        for hour in range(4)
    ]

    aggregated, _ = aggregate_bars_to_4h(bars)

    assert aggregated[0].volume is None


# ---------------------------------------------------------------------------
# 4) 计划、报告与 CLI（不联网、不碰数据库）
# ---------------------------------------------------------------------------
def test_plan_windows_skips_unsupported_symbols_and_4h() -> None:
    plan, notes = plan_windows(["XAUUSD", "US10Y_REAL", "UNKNOWN"], ["1d", "1h", "4h"])

    assert [(symbol, timeframe) for symbol, timeframe, _ in plan] == [
        ("XAUUSD", "1d"),
        ("XAUUSD", "1h"),
    ]
    joined = "；".join(notes)
    assert "US10Y_REAL" in joined and "4h" in joined and "UNKNOWN" in joined
    for _symbol, timeframe, window in plan:
        # 1d: 10 年；1h: **720 天**（provider 只允许 730 天，留 10 天边际防 HTTP 422）
        expected_days = 3650 if timeframe == "1d" else 720
        assert (window.end_utc - window.start_utc).days == expected_days


def test_parse_csv_list_and_mask_db_url() -> None:
    assert parse_csv_list(" XAUUSD , DXY ") == ("XAUUSD", "DXY")
    with pytest.raises(ValueError, match="不能为空"):
        parse_csv_list(" , ")

    masked = mask_db_url("postgresql+psycopg://gold:secret@localhost:5432/gold")
    assert masked == "postgresql+psycopg://gold:***@localhost:5432/gold"
    assert "secret" not in masked
    assert mask_db_url("sqlite+pysqlite:///./database/gold_ai.db").endswith("gold_ai.db")


def test_audit_select_items_validates_timeframes() -> None:
    assert select_items("XAUUSD", "1h,1d") == (("XAUUSD", "1h"), ("XAUUSD", "1d"))
    with pytest.raises(ValueError, match="4h"):
        select_items("XAUUSD", "4h")
    with pytest.raises(ValueError, match="未知周期"):
        select_items("XAUUSD", "7m")
    with pytest.raises(ValueError, match="不能为空"):
        select_items("", "1h")


def test_audit_report_and_rows_contain_expected_content() -> None:
    audit = audit_gaps(_fetch(_week_slots(gap_day=(1, 1), gap_hours=(10, 11))))  # type: ignore[arg-type]

    report = audit_render_report(
        [audit],
        failures=["DXY 1d：RuntimeError: 取数失败"],
        closed_threshold=0.02,
        generated_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
        repro_command="python scripts/audit_market_gaps.py --dry-run",
    )

    for section in (
        "# Phase 3.0 W0-1 行情缺口审计报告",
        "## 0. 结论摘要",
        "## 1. 逐项明细（含数据指纹）",
        "## 2. 异常明细",
        "## 3. 疑似假期清单",
        "## 4. 覆盖不足的日子",
        "## 5. 方法学与局限",
        "## 附录 A. 复现命令",
    ):
        assert section in report
    assert "真正的数据质量问题 = 2 个槽" in report
    assert "DXY 1d" in report

    rows = audit_rows([audit])
    assert rows[0]["data_gap"] == "2"
    assert rows[0]["provider_symbol"] == "GC=F"
    assert all(row["category"] == CATEGORY_DATA_GAP for row in finding_rows([audit]))


def test_audit_main_dry_run_prints_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = audit_gaps(_fetch(_week_slots()))  # type: ignore[arg-type]
    monkeypatch.setattr("scripts.audit_market_gaps.audit_one", lambda *a, **k: audit)
    out_dir = tmp_path / "out"
    report = tmp_path / "report.md"

    code = audit_main(
        [
            "--symbols",
            "XAUUSD",
            "--timeframes",
            "1h",
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "# Phase 3.0 W0-1 行情缺口审计报告" in out
    assert "未写任何文件" in out
    assert not out_dir.exists()
    assert not report.exists()


def test_audit_main_no_dry_run_writes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = audit_gaps(_fetch(_week_slots(gap_day=(1, 1), gap_hours=(10, 11))))  # type: ignore[arg-type]
    monkeypatch.setattr("scripts.audit_market_gaps.audit_one", lambda *a, **k: audit)
    out_dir = tmp_path / "out"
    report = tmp_path / "report.md"

    code = audit_main(
        [
            "--symbols",
            "XAUUSD",
            "--timeframes",
            "1h",
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
            "--no-dry-run",
        ]
    )

    assert code == 0
    assert (out_dir / "gap_audit_summary.csv").exists()
    assert (out_dir / "gap_findings.csv").exists()
    meta = json.loads((out_dir / "gap_audit_meta.json").read_text(encoding="utf-8"))
    assert meta["items"] == ["XAUUSD/1h"]
    assert meta["audits"][0]["data_gap"] == "2"
    assert report.exists()


def test_audit_main_error_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert audit_main(["--dry-run", "--no-dry-run"]) == 2

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("所有候选 ticker 均取数失败：GC=F: HTTP 429")

    monkeypatch.setattr("scripts.audit_market_gaps.audit_one", _boom)

    assert audit_main(["--symbols", "XAUUSD", "--timeframes", "1h"]) == 2
    assert "FAILED" in capsys.readouterr().err


def test_backfill_main_rejects_empty_plan_and_conflicting_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """只有 4h / 不支持的标的 → 计划为空 → 退 2（且在触碰数据库**之前**返回）。"""
    assert backfill_main(["--symbols", "XAUUSD", "--timeframes", "4h"]) == 2
    assert "没有可执行的采集计划" in capsys.readouterr().err

    assert backfill_main(["--symbols", "US10Y_REAL", "--timeframes", "1d"]) == 2
    assert backfill_main(["--dry-run", "--no-dry-run"]) == 2
