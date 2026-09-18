"""种子定义的静态校验（不依赖数据库，秒级反馈）。

目的：在写入数据库之前就把"字段缺失 / 时区非法 / 自然键重复 / 采集器未声明"这类
问题挡在 CI 里（03 §17 数据质量规则）。
"""

from __future__ import annotations

import pytest

from database.models.enums import AssetClass, SourceType
from database.seeds.instruments import INSTRUMENT_SEEDS, REQUIRED_PHASE1_SYMBOLS, InstrumentSeed
from database.seeds.sources import SOURCE_SEEDS, SourceSeed
from src.common.exceptions import TimeSemanticsError
from src.common.time import resolve_timezone

pytestmark = pytest.mark.unit


def test_instrument_symbols_are_unique() -> None:
    symbols = [seed.symbol for seed in INSTRUMENT_SEEDS]
    assert len(symbols) == len(set(symbols)), "instruments 自然键（symbol）重复"


def test_source_names_are_unique() -> None:
    names = [seed.name for seed in SOURCE_SEEDS]
    assert len(names) == len(set(names)), "sources 自然键（name）重复"


def test_required_phase1_instruments_are_seeded() -> None:
    symbols = {seed.symbol for seed in INSTRUMENT_SEEDS}
    assert set(REQUIRED_PHASE1_SYMBOLS) <= symbols


@pytest.mark.parametrize("seed", INSTRUMENT_SEEDS, ids=lambda s: s.symbol)
def test_instrument_seed_fields_follow_04(seed: InstrumentSeed) -> None:
    """04 §12：symbol / asset_class / timezone 必填，quote_currency 为币种代码。"""
    assert seed.symbol
    assert seed.symbol == seed.symbol.upper()
    assert seed.name.strip()
    assert isinstance(seed.asset_class, AssetClass)
    assert 3 <= len(seed.quote_currency) <= 20
    resolve_timezone(seed.timezone)


def test_source_type_coverage_is_complete() -> None:
    """Phase 1 必须覆盖 WEIBO / NEWS / MARKET / MACRO 四类来源。"""
    assert {seed.source_type for seed in SOURCE_SEEDS} == set(SourceType)


@pytest.mark.parametrize("seed", SOURCE_SEEDS, ids=lambda s: s.name)
def test_source_seed_fields_follow_04(seed: SourceSeed) -> None:
    """04 §2：name / source_type / base_url / timezone / enabled / config_json。"""
    assert seed.name == seed.name.strip()
    assert seed.base_url.startswith("https://"), "来源地址必须使用 https"
    resolve_timezone(seed.timezone)
    assert isinstance(seed.config_json, dict)
    assert seed.config_json.get("collector"), "必须声明采集器名称（Phase 1 第 2 步实现）"


def test_disabled_sources_are_documented() -> None:
    """默认关闭的来源必须是有意为之且写明原因（避免"以为在跑其实没跑"）。"""
    disabled = sorted(seed.name for seed in SOURCE_SEEDS if not seed.enabled)
    assert disabled == [
        "econ_calendar_investing",
        "manual-华尔街见闻",
        "manual-汇通网",
        "market_dukascopy_xauusd",
        "market_stooq_backup",
    ]

    for seed in SOURCE_SEEDS:
        assert seed.config_json.get("note"), f"来源 {seed.name} 必须写明 note 说明用途/现状"


def test_every_source_declares_min_records_threshold() -> None:
    """团队批复：每个来源都必须在 config_json 中声明 min_records_per_run（0 = 不检查）。

    该阈值由采集器在每轮结束时校验，低于阈值会打 WARNING 日志并写入
    ``collector_runs.warnings_json``（可被 Dashboard / 数据质量巡检查询）。
    """
    for seed in SOURCE_SEEDS:
        threshold = seed.config_json.get("min_records_per_run")
        assert isinstance(threshold, int), f"{seed.name} 缺少 min_records_per_run"
        assert threshold >= 0, f"{seed.name} 的 min_records_per_run 不能为负"


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(TimeSemanticsError):
        resolve_timezone("Mars/Olympus_Mons")
    with pytest.raises(TimeSemanticsError):
        resolve_timezone("   ")


def test_documented_timezones_are_resolvable() -> None:
    """UTC 与 Asia/Shanghai 必须可解析（Windows 依赖 tzdata 包）。"""
    assert str(resolve_timezone("UTC")) == "UTC"
    assert str(resolve_timezone("Asia/Shanghai")) == "Asia/Shanghai"
