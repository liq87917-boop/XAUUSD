"""DbnomicsMacroCollector 解析纯函数单元测试（无数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.collectors.dbnomics_macro import parse_dbnomics_series
from src.collectors.errors import CollectorError


def test_parse_dbnomics_series_normal() -> None:
    rows = [
        {"period": "2026-01-01", "value": 2600.5},
        {"period": "2026-02-01", "value": 2650.0},
    ]
    observations = parse_dbnomics_series(rows, series_id="IMF/CPI")
    assert len(observations) == 2
    assert observations[0].series_id == "IMF/CPI"
    assert observations[0].event_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert observations[0].released_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert observations[0].value == Decimal("2600.5000000000")


def test_parse_dbnomics_series_empty() -> None:
    assert parse_dbnomics_series([]) == ()


def test_parse_dbnomics_series_missing_period_raises() -> None:
    """时间字段缺失必须报错（用户硬性要求）。"""
    with pytest.raises(CollectorError, match="缺少字段"):
        parse_dbnomics_series([{"value": 2600.5}])


def test_parse_dbnomics_series_invalid_period_raises() -> None:
    with pytest.raises(CollectorError, match="无法解析"):
        parse_dbnomics_series([{"period": "not-a-date", "value": 1.0}])


def test_parse_dbnomics_series_invalid_value_raises() -> None:
    with pytest.raises(CollectorError, match="无法解析为数值"):
        parse_dbnomics_series([{"period": "2026-01-01", "value": "abc"}])


def test_parse_dbnomics_series_appends_series_code() -> None:
    """多序列 dataset（如 IMF/CPI 含多国多指标）用 series_code 区分 event_code。"""
    rows = [{"period": "2026-01-01", "value": 1.5, "series_code": "A.US.PCPI_IX"}]
    observations = parse_dbnomics_series(rows, series_id="IMF/CPI")
    assert observations[0].series_id == "IMF/CPI:A.US.PCPI_IX"
