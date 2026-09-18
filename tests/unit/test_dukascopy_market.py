from __future__ import annotations

import lzma
import struct
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src.collectors.dukascopy import day_url, parse_and_aggregate, parse_minute_candles

REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(*, count: int = 60, skip: int | None = None, volume: float = 1.5) -> bytes:
    rows = []
    for index in range(count):
        if index == skip:
            continue
        base = 2_600_000 + index
        rows.append(struct.pack(">5if", index * 60, base, base + 2, base - 3, base + 5, volume))
    return lzma.compress(b"".join(rows))


def test_day_url_uses_zero_based_month() -> None:
    assert day_url(date(2025, 1, 2)).endswith("/XAUUSD/2025/00/02/BID_candles_min_1.bi5")


def test_parse_and_aggregate_complete_hour() -> None:
    result = parse_and_aggregate(_payload(), day=date(2025, 1, 2))
    assert len(result) == 1
    assert result[0].open_time == datetime(2025, 1, 2, tzinfo=UTC)
    assert result[0].close_time == datetime(2025, 1, 2, 1, tzinfo=UTC)
    assert str(result[0].open) == "2600"
    assert result[0].high > result[0].close > result[0].low
    assert result[0].volume == 90
    assert len(result[0].source_sha256) == 64


def test_incomplete_hour_is_rejected_not_interpolated() -> None:
    assert parse_and_aggregate(_payload(skip=10), day=date(2025, 1, 2)) == ()


def test_zero_volume_closed_market_placeholder_is_rejected() -> None:
    assert parse_and_aggregate(_payload(volume=0), day=date(2025, 1, 4)) == ()


def test_parser_rejects_non_minute_timestamp() -> None:
    payload = lzma.compress(struct.pack(">5if", 61, 1, 1, 1, 1, 0.0))
    with pytest.raises(ValueError, match="分钟时间异常"):
        parse_minute_candles(payload, day=date(2025, 1, 2))


def test_parser_preserves_day_boundary() -> None:
    minutes = parse_minute_candles(_payload(count=2), day=date(2025, 1, 2))
    assert minutes[1].open_time - minutes[0].open_time == timedelta(minutes=1)


def test_backfill_cli_help_starts() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "backfill_dukascopy_xauusd.py"), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Dukascopy XAUUSD 1h" in result.stdout
