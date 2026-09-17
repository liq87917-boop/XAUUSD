"""``MacroCollector`` 集成测试（落库 / 时间关系 / 缺失字段 / 限流重试 / 密钥安全）。

★ 全程不访问外部网络 ★：HTTP 由 ``conftest.MockTransport`` 脚本化提供（FRED 格式的
   Mock 数据即团队批复允许的"mock 数据模拟 FRED 格式"），conftest 的 autouse 守卫
   也会拦截任何非本机 socket 连接。

覆盖团队批复的强制要求：
    1. 正常解析并落库（严格按 04 §15 ``macro_events``，保持 provider 原始粒度）；
    2. 缺失字段（FRED 用 ``.`` 表示缺失）→ 跳过 + WARNING，不写脏数据；
    3. **``event_at <= effective_at`` 时间关系**：运行期满足，且数据库层面拒绝违反；
    4. Mock 层限流（429）与超时重试（3 次尝试，``collector_runs.retry_count`` 落库）；
    5. 幂等（重复采集不重复插入）、多 series 断点续采、密钥脱敏。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import CollectorRun, MacroEvent, RawItem
from database.models.enums import CollectorRunStatus, RawItemType, SourceType
from src.collectors import CollectWindow, HttpResponse, TransportTimeoutError, run_collector
from src.collectors.macro import FRED_PARSER_VERSION, MacroCollector

pytestmark = pytest.mark.integration

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
    end_at=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
)
API_KEY = "test-api-key-do-not-leak"


def _obs(day: str, value: object, **extra: object) -> dict[str, object]:
    return {
        "realtime_start": "2026-09-12",
        "realtime_end": "9999-12-31",
        "date": day,
        "value": value,
        **extra,
    }


def _fred_response(
    observations: list[object], *, status: int = 200, units: str = "Index 2017=100"
) -> HttpResponse:
    return HttpResponse(
        status=status,
        json_body={"units": units, "observations": observations, "count": len(observations)},
    )


@pytest.fixture()
def macro_source(make_source):
    return make_source(
        name="macro-source",
        source_type=SourceType.MACRO,
        base_url="https://api.stlouisfed.org",
        enabled=True,
        config_json={
            "provider": "fred",
            "series": [{"series_id": "CPIAUCSL", "country": "US", "unit": "index"}],
            "api_key_env": "GOLD_AI_TEST_FRED_KEY",
            "lookback_days": 45,
            "min_records_per_run": 1,
        },
    )


def _raw_macro(session: Session) -> list[RawItem]:
    return list(
        session.scalars(sa.select(RawItem).where(RawItem.item_type == RawItemType.MACRO)).all()
    )


def _macro_events(session: Session) -> list[MacroEvent]:
    return list(session.scalars(sa.select(MacroEvent).order_by(MacroEvent.event_at)).all())


def _as_utc(value: datetime) -> datetime:
    """SQLite 返回 naive datetime：统一补齐 UTC 后再比较（仅测试辅助）。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1) 正常解析 + 时间关系
# ---------------------------------------------------------------------------
async def test_normal_run_persists_macro_events(
    session: Session, macro_source, mock_transport
) -> None:
    transport = mock_transport(
        [_fred_response([_obs("2026-07-01", "319.6"), _obs("2026-08-01", "320.9")])]
    )
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.fetched_count == 2
    assert result.outcome.inserted_count == 2

    events = _macro_events(session)
    assert len(events) == 2
    first = events[0]
    assert first.event_code == "CPIAUCSL"
    assert first.country == "US"
    # 粒度：provider 的观测日期（月首日）保持原样，UTC 当日 00:00
    assert first.event_at.replace(tzinfo=UTC) == datetime(2026, 7, 1, tzinfo=UTC)
    assert first.actual_value == Decimal("319.6000000000")
    assert first.unit == "Index 2017=100"
    assert first.source_id == macro_source.id
    # ★ event_at 是观测期；released_at 才是外部可用边界。
    assert _as_utc(first.released_at) == datetime(2026, 9, 13, tzinfo=UTC)
    assert first.vintage_end_at is None
    assert _as_utc(first.effective_at) >= _as_utc(first.released_at)
    assert first.collected_at is not None
    assert _as_utc(first.effective_at) >= _as_utc(first.collected_at)
    # Phase 1 不凭空推断预期值/前值
    assert first.forecast_value is None
    assert first.previous_value is None

    raws = _raw_macro(session)
    assert len(raws) == 2
    assert raws[0].raw_json["parser_version"] == FRED_PARSER_VERSION
    assert raws[0].raw_json["frequency_note"].startswith("保持 provider 原始粒度")


async def test_api_key_is_never_persisted(
    session: Session, macro_source, mock_transport
) -> None:
    """★ 密钥安全：api_key 只出现在真实请求参数中，绝不进入 source_url / raw_json。"""
    transport = mock_transport([_fred_response([_obs("2026-07-01", "319.6")])])
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    await run_collector(session, collector, window=WINDOW)

    assert transport.requests[0].params["api_key"] == API_KEY  # 真实请求带 key
    raw = _raw_macro(session)[0]
    assert "api_key=***" in (raw.source_url or "")
    assert API_KEY not in (raw.source_url or "")
    assert API_KEY not in str(raw.raw_json)


def test_database_rejects_release_after_effective_at(session: Session, macro_source) -> None:
    """★ 数据库层面兜底：``released_at`` 晚于 ``effective_at`` 必须被拒绝。

    这条 CHECK 是防未来数据泄漏的最后一道门（04 §15 / .clinerules 第二条）。
    """
    now = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    session.add(
        MacroEvent(
            event_code="CPIAUCSL",
            country="US",
            event_at=now,
            released_at=now,
            vintage_end_at=None,
            collected_at=now,
            effective_at=now - timedelta(minutes=1),
            source_id=macro_source.id,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------------
# 2) 缺失字段与未来日期（数据质量）
# ---------------------------------------------------------------------------
async def test_missing_fields_are_skipped_with_warning(
    session: Session, macro_source, mock_transport
) -> None:
    """FRED 用 ``.`` 表示缺失值；非法日期/非数值也必须跳过而不是写脏数据。"""
    transport = mock_transport(
        [
            _fred_response(
                [
                    _obs("2026-07-01", "319.6"),
                    _obs("2026-08-01", "."),  # 缺失值
                    _obs("", "1.0"),  # 缺失日期
                    _obs("2026-09-01", "abc"),  # 非数值
                ]
            )
        ]
    )
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None
    assert result.outcome.fetched_count == 4
    assert result.outcome.inserted_count == 1
    assert len(_macro_events(session)) == 1
    assert any("被跳过" in warning for warning in result.warnings), result.warnings
    assert any("missing_value" in warning for warning in result.warnings)


async def test_future_dated_observation_is_skipped(
    session: Session, macro_source, mock_transport
) -> None:
    """未来日期观测必须跳过（否则会违反 event_at <= effective_at，等于泄露未来信息）。"""
    transport = mock_transport(
        [_fred_response([_obs("2026-07-01", "1.0"), _obs("2027-01-01", "9.9")])]
    )
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    result = await run_collector(session, collector, window=WINDOW)

    assert len(_macro_events(session)) == 1
    assert any("future_date" in warning for warning in result.warnings), result.warnings


async def test_empty_observations_persist_warning_in_collector_runs(
    session: Session, macro_source, mock_transport
) -> None:
    """0 条观测（FRED 异常/返回空）→ 低于 min_records_per_run → warnings_json 落库。"""
    transport = mock_transport([_fred_response([])])
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.warnings_json is not None
    assert any(
        "低于预期下限 1" in message for message in run.warnings_json["warnings"]
    ), run.warnings_json


async def test_fred_error_payload_fails_with_message(
    session: Session, macro_source, mock_transport
) -> None:
    transport = mock_transport(
        [
            HttpResponse(
                status=200,
                json_body={"error_code": 400, "error_message": "Bad Request. invalid api_key"},
            )
        ]
    )
    collector = MacroCollector(macro_source, transport=transport, api_key=API_KEY)

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert "FRED 返回错误" in (result.error_message or "")
    assert _macro_events(session) == []


async def test_alfred_unavailable_window_is_success_with_warning(
    session: Session, macro_source, mock_transport
) -> None:
    """ALFRED 历史不存在时保持空值并留痕，绝不退回今天的修订终值。"""
    response = HttpResponse(
        status=400,
        json_body={
            "error_code": 400,
            "error_message": (
                "Bad Request. The series does not exist in ALFRED but may exist in FRED."
            ),
        },
    )
    collector = MacroCollector(
        macro_source,
        transport=mock_transport([response]),
        api_key=API_KEY,
    )

    result = await run_collector(session, collector, window=WINDOW, resume=False)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None and result.outcome.fetched_count == 0
    assert any("没有 ALFRED 历史版本" in warning for warning in result.warnings)
    assert _macro_events(session) == []


# ---------------------------------------------------------------------------
# 3) 限流与超时重试（Mock 层）
# ---------------------------------------------------------------------------
async def test_rate_limit_is_retried_then_succeeds(
    session: Session, macro_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport(
        [
            HttpResponse(status=429),
            HttpResponse(status=429),
            _fred_response([_obs("2026-07-01", "319.6")]),
        ]
    )
    collector = MacroCollector(
        macro_source, transport=transport, api_key=API_KEY, sleep=recording_sleep
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.retry_count == 2
    assert recording_sleep.delays == [1.0, 2.0]  # 指数退避
    assert len(_macro_events(session)) == 1

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 2  # ★ 重试次数持久化


async def test_rate_limit_exhausted_marks_failed(
    session: Session, macro_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([HttpResponse(status=429)] * 3)
    collector = MacroCollector(
        macro_source, transport=transport, api_key=API_KEY, sleep=recording_sleep
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert _macro_events(session) == []

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    assert run.retry_count == 3


async def test_timeout_retries_three_times_then_failed(
    session: Session, macro_source, mock_transport, recording_sleep
) -> None:
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)
    collector = MacroCollector(
        macro_source, transport=transport, api_key=API_KEY, sleep=recording_sleep
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.FAILED
    assert result.retry_count == 3
    assert "超时" in (result.error_message or "")
    assert _macro_events(session) == []


# ---------------------------------------------------------------------------
# 4) 幂等 / 断点续采 / CSV 兜底
# ---------------------------------------------------------------------------
async def test_rerun_is_idempotent(session: Session, macro_source, mock_transport) -> None:
    """同一份观测重复采集：raw_items 与 macro_events 都不重复入库。"""
    payload = _fred_response([_obs("2026-07-01", "319.6"), _obs("2026-08-01", "320.9")])
    transport = mock_transport([payload, payload])

    first = await run_collector(
        session, MacroCollector(macro_source, transport=transport, api_key=API_KEY), window=WINDOW
    )
    second = await run_collector(
        session, MacroCollector(macro_source, transport=transport, api_key=API_KEY), window=WINDOW
    )

    assert first.outcome is not None and first.outcome.inserted_count == 2
    assert second.outcome is not None
    assert second.outcome.inserted_count == 0
    assert second.outcome.duplicate_count == 2
    assert len(_raw_macro(session)) == 2
    assert len(_macro_events(session)) == 2


async def test_new_release_for_same_observation_is_appended(
    session: Session, macro_source, mock_transport
) -> None:
    """同观测期的新 release 不得被 content_hash 去重，也不得覆盖初值。"""
    first = _fred_response(
        [_obs("2026-07-01", "319.6", realtime_start="2026-08-12")]
    )
    revision = _fred_response(
        [_obs("2026-07-01", "319.8", realtime_start="2026-09-12")]
    )
    transport = mock_transport([first, revision])

    await run_collector(
        session, MacroCollector(macro_source, transport=transport, api_key=API_KEY), window=WINDOW
    )
    await run_collector(
        session,
        MacroCollector(macro_source, transport=transport, api_key=API_KEY),
        window=WINDOW,
        resume=False,
    )

    events = _macro_events(session)
    assert len(events) == 2
    assert [str(event.actual_value) for event in events] == ["319.6000000000", "319.8000000000"]
    assert len(_raw_macro(session)) == 2


async def test_multi_series_resumes_from_cursor(
    session: Session, make_source, mock_transport
) -> None:
    """多 series 采集：第二个 series 失败 → PARTIAL_FAILED，下一轮从断点续采。"""
    source = make_source(
        name="macro-two-series",
        source_type=SourceType.MACRO,
        base_url="https://api.stlouisfed.org",
        config_json={
            "provider": "fred",
            "series": ["CPIAUCSL", "DFF"],
            "min_records_per_run": 0,  # 本轮只看游标语义
        },
    )
    transport = mock_transport(
        [
            _fred_response([_obs("2026-07-01", "319.6")]),
            HttpResponse(status=200, json_body={"error_code": 400, "error_message": "series bad"}),
            _fred_response([_obs("2026-07-02", "4.33")], units="Percent"),
        ]
    )

    first = await run_collector(
        session, MacroCollector(source, transport=transport, api_key=API_KEY), window=WINDOW
    )

    assert first.status is CollectorRunStatus.PARTIAL_FAILED
    assert first.outcome is not None
    assert first.outcome.cursor == {"series_index": 1, "series_id": "DFF"}
    assert [event.event_code for event in _macro_events(session)] == ["CPIAUCSL"]

    run = session.get(CollectorRun, first.run_id)
    assert run is not None
    assert run.cursor_json == {"series_index": 1, "series_id": "DFF"}

    second = await run_collector(
        session, MacroCollector(source, transport=transport, api_key=API_KEY), window=WINDOW
    )

    assert second.status is CollectorRunStatus.SUCCESS
    assert transport.requests[-1].params["series_id"] == "DFF"  # ★ 从断点续采
    assert sorted(event.event_code for event in _macro_events(session)) == ["CPIAUCSL", "DFF"]
    assert second.outcome is not None
    assert second.outcome.cursor is None


async def test_csv_mode_imports_offline_dataset(
    session: Session, make_source, tmp_path: Path, mock_transport
) -> None:
    """CSV 兜底：无 FRED Key 的环境也能把框架跑通（团队批复允许的 mock 数据路径）。"""
    csv_file = tmp_path / "macro_fixture.csv"
    csv_file.write_text(
        "series_id,date,released_at,value,unit\n"
        "CPIAUCSL,2026-06-01,2026-07-15,318.1,index\n"
        "CPIAUCSL,2026-07-01,2026-08-15,319.6,index\n",
        encoding="utf-8",
    )
    source = make_source(
        name="macro-csv",
        source_type=SourceType.MACRO,
        base_url="https://api.stlouisfed.org",
        config_json={"csv_path": str(csv_file), "min_records_per_run": 2},
    )
    transport = mock_transport([])  # CSV 模式不应发起任何网络请求

    result = await run_collector(
        session, MacroCollector(source, transport=transport), window=WINDOW
    )

    assert result.status is CollectorRunStatus.SUCCESS
    assert transport.requests == []
    events = _macro_events(session)
    assert [event.event_at.date().isoformat() for event in events] == ["2026-06-01", "2026-07-01"]
    assert events[0].unit == "index"
    assert events[0].country == "US"
    assert all(_as_utc(event.released_at) <= _as_utc(event.effective_at) for event in events)


# ---------------------------------------------------------------------------
# 5) 防未来数据泄漏总检查
# ---------------------------------------------------------------------------
async def test_no_future_leakage_for_macro_events(
    session: Session, macro_source, mock_transport
) -> None:
    """★ 泄漏检查：所有宏观记录只在 release 与采集完成后可用。"""
    transport = mock_transport(
        [_fred_response([_obs("2026-07-01", "319.6"), _obs("2026-08-01", "320.9")])]
    )

    result = await run_collector(
        session, MacroCollector(macro_source, transport=transport, api_key=API_KEY), window=WINDOW
    )

    run = session.get(CollectorRun, result.run_id)
    assert run is not None
    for event in _macro_events(session):
        assert _as_utc(event.released_at) <= _as_utc(event.effective_at)
        assert _as_utc(event.collected_at) <= _as_utc(event.effective_at)
        assert _as_utc(event.effective_at) <= _as_utc(run.finished_at or run.started_at)


