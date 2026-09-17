"""W0-1 回填与 4h 聚合的集成测试（用 conftest 的 SQLite 库；**不联网**）。

覆盖：`preflight` 种子预检、`aggregate_and_insert_4h` 的**幂等与半桶丢弃**、
回填 CLI 的 `--dry-run` 零落盘 / `--no-dry-run` 产出四件套。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import DataVersion, Instrument, MarketBar, Source
from database.models.enums import SourceType, Timeframe
from database.seeds import seed_instruments
from scripts._market_data import GapAudit
from scripts.backfill_market_bars import (
    BackfillOutcome,
    aggregate_and_insert_4h,
    main,
    preflight,
)

pytestmark = pytest.mark.integration

MONDAY: datetime = datetime(2026, 1, 5, tzinfo=UTC)


def _market_source(session: Session, make_source: Any) -> Source:
    """行情源（名字必须与 `scripts/backfill_market_bars.DEFAULT_MARKET_SOURCE` 一致）。"""
    return make_source(
        name="market_yahoo",
        source_type=SourceType.MARKET,
        base_url="https://query1.finance.yahoo.com",
        enabled=True,
        config_json={"symbols": ["XAUUSD"], "timeframes": ["1h", "1d"]},
    )


def _hourly_bar(
    instrument_id: Any, hour: int, *, source_id: Any = None, base: float = 1800.0
) -> MarketBar:
    open_time = MONDAY + timedelta(hours=hour)
    close_time = open_time + timedelta(hours=1)
    return MarketBar(
        instrument_id=instrument_id,
        timeframe=Timeframe.H1,
        open_time=open_time,
        close_time=close_time,
        open=Decimal(str(base + hour)),
        high=Decimal(str(base + hour + 2)),
        low=Decimal(str(base + hour - 1)),
        close=Decimal(str(base + hour + 1)),
        volume=Decimal("5"),
        source_id=source_id,
        collected_at=close_time,
        effective_at=close_time,
    )


def _canned_audit() -> GapAudit:
    return GapAudit(
        symbol="XAUUSD",
        interval="1d",
        lookback="10y",
        provider_symbol="GC=F",
        provider_digest="deadbeefdeadbeef",
        total_slots=2500,
        valid_slots=2500,
    )


def test_preflight_flags_missing_market_source(session: Session, make_source: Any) -> None:
    """预检必须能指出「缺行情源 / 缺标的」（脚本据此退 2 并打印修复指引）。"""
    seed_instruments(session)

    problems = preflight(session, symbols=["XAUUSD"])
    assert any("market_yahoo" in problem for problem in problems)

    _market_source(session, make_source)
    session.flush()

    assert preflight(session, symbols=["XAUUSD"]) == []
    assert preflight(session, symbols=["NOPE"]) != []


def test_aggregate_and_insert_4h_is_idempotent_and_skips_partial(
    session: Session, make_source: Any
) -> None:
    """4h 聚合：只写满桶、复跑幂等、`effective_at` 保持防泄漏语义（TD-03）。"""
    seed_instruments(session)
    source = _market_source(session, make_source)
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == "XAUUSD"))
    assert instrument is not None
    for hour in range(7):  # 桶 0–3 满；桶 4–7 只有 3 根 → 半桶
        session.add(_hourly_bar(instrument.id, hour, source_id=source.id))
    session.commit()

    assert aggregate_and_insert_4h(session, symbol="XAUUSD") == (1, 1, 0, 1)
    # 幂等：复跑不重复插入
    assert aggregate_and_insert_4h(session, symbol="XAUUSD") == (1, 0, 1, 1)

    bar = session.scalar(sa.select(MarketBar).where(MarketBar.timeframe == Timeframe.H4))
    assert bar is not None
    # SQLite 读回 naive（项目既有约定：测试里显式补 tz 再断言）
    assert bar.open_time.replace(tzinfo=UTC) == MONDAY
    assert bar.close == Decimal("1804")  # 第四根（h=3）close = 1800+3+1
    assert bar.collected_at <= bar.effective_at  # 采集器同款防泄漏约束
    assert bar.effective_at >= bar.close_time

    versions = list(session.scalars(sa.select(DataVersion)).all())
    assert len(versions) == 1
    assert versions[0].dataset_name == "market_bars:XAUUSD:4h"
    assert versions[0].version.startswith("4h-v1-")
    assert versions[0].row_count == 1
    assert versions[0].data_hash is not None and len(versions[0].data_hash) == 64
    assert versions[0].source_scope_json == {
        "instrument_id": str(instrument.id),
        "symbol": "XAUUSD",
        "input_timeframe": "1h",
        "output_timeframe": "4h",
        "processor": "aggregate_bars_to_4h",
        "processor_version": "4h-v1",
    }

    with pytest.raises(ValueError, match="不存在标的"):
        aggregate_and_insert_4h(session, symbol="NOPE")


def test_backfill_cli_dry_run_writes_nothing(
    tmp_path: Path, session: Session, make_source: Any, sqlite_url: str, monkeypatch: Any
) -> None:
    """`--dry-run`：只做只读取数 + 缺口复审，**不写库、不写文件**。"""
    seed_instruments(session)
    _market_source(session, make_source)
    session.commit()
    calls: list[str] = []

    def _fake_fetch(*args: Any, **kwargs: Any) -> object:
        calls.append("fetch")
        return object()

    monkeypatch.setattr("scripts.backfill_market_bars.fetch_chart", _fake_fetch)
    monkeypatch.setattr("scripts.backfill_market_bars.audit_gaps", lambda *a, **k: _canned_audit())
    out_dir = tmp_path / "out"
    report = tmp_path / "report.md"

    code = main(
        [
            "--symbols",
            "XAUUSD",
            "--timeframes",
            "1h",
            "--db-url",
            sqlite_url,
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
        ]
    )

    assert code == 0
    assert calls == ["fetch"]  # 只取了数，没有回填
    assert not out_dir.exists()
    assert not report.exists()


def test_backfill_cli_no_dry_run_writes_outputs(
    tmp_path: Path, session: Session, make_source: Any, sqlite_url: str, monkeypatch: Any
) -> None:
    """`--no-dry-run`：产出 outcomes CSV + meta + 报告（回填本体被替身替换，不联网）。"""
    seed_instruments(session)
    _market_source(session, make_source)
    session.commit()

    async def _fake_backfill(
        session_: Session,
        *,
        source: Source,
        plan: Any,
        timeout: float = 60.0,
        transport_name: str = "httpx",
    ) -> list[BackfillOutcome]:
        return [
            BackfillOutcome(
                symbol=symbol,
                timeframe=timeframe,
                window_start=window.start_utc.isoformat(),
                window_end=window.end_utc.isoformat(),
                status="SUCCESS",
                fetched=10,
                inserted=10,
            )
            for symbol, timeframe, window in plan
        ]

    monkeypatch.setattr("scripts.backfill_market_bars.run_backfill", _fake_backfill)
    monkeypatch.setattr("scripts.backfill_market_bars.fetch_chart", lambda *a, **k: object())
    monkeypatch.setattr("scripts.backfill_market_bars.audit_gaps", lambda *a, **k: _canned_audit())
    out_dir = tmp_path / "out"
    report = tmp_path / "report.md"

    code = main(
        [
            "--symbols",
            "XAUUSD",
            "--timeframes",
            "1d",
            "--db-url",
            sqlite_url,
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
            "--no-dry-run",
        ]
    )

    assert code == 0
    rows = list(
        csv.DictReader((out_dir / "backfill_outcomes.csv").open(encoding="utf-8-sig", newline=""))
    )
    assert rows and rows[0]["symbol"] == "XAUUSD"
    assert rows[0]["inserted"] == "10"
    meta = json.loads((out_dir / "backfill_meta.json").read_text(encoding="utf-8"))
    assert meta["outcomes"][0]["status"] == "SUCCESS"
    assert "test.db" in meta["db_url"]  # 口令/主机不泄露（本用例是 sqlite 路径）
    text = report.read_text(encoding="utf-8")
    assert "# Phase 3.0 W0-1 行情回填报告" in text
    assert "缺口复审" in text
    assert "幂等性验证" in text
    # §6 实测约束必须随报告再生成（防"人工补充被冲掉"）
    assert "## 6. 实测约束与已修缺陷" in text
    assert "US10Y_REAL" in text and "HTTP 403" in text and "进行中" in text
