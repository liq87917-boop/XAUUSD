"""基础数据（种子）质量测试 —— 03 §17 数据质量规则的落地检查。

这些检查针对"数据本身"而不是代码路径：必填字段非空、枚举合法、时区可解析、
URL 规范、跨表引用一致（行情来源声明的 symbols 必须都在 instruments 中）。
"""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa

from database.models import Instrument, Source
from database.seeds import seed_instruments, seed_sources
from database.seeds.instruments import REQUIRED_PHASE1_SYMBOLS
from src.common.time import resolve_timezone

pytestmark = pytest.mark.data_quality

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]+$")


@pytest.fixture()
def seeded(session: sa.orm.Session) -> sa.orm.Session:
    seed_instruments(session)
    seed_sources(session)
    session.flush()
    return session


def test_required_fields_are_not_null_or_blank(seeded: sa.orm.Session) -> None:
    for instrument in seeded.scalars(sa.select(Instrument)).all():
        assert instrument.symbol.strip()
        assert instrument.asset_class is not None
        assert instrument.timezone.strip()

    for source in seeded.scalars(sa.select(Source)).all():
        assert source.name.strip()
        assert source.source_type is not None
        assert (source.base_url or "").strip()
        assert source.timezone.strip()


def test_symbols_follow_naming_convention(seeded: sa.orm.Session) -> None:
    for symbol in seeded.scalars(sa.select(Instrument.symbol)).all():
        assert SYMBOL_PATTERN.match(symbol), f"symbol 命名不规范：{symbol}"


def test_timezones_are_valid_iana_names(seeded: sa.orm.Session) -> None:
    for timezone_name in seeded.scalars(sa.select(Instrument.timezone)).all():
        resolve_timezone(timezone_name)
    for timezone_name in seeded.scalars(sa.select(Source.timezone)).all():
        resolve_timezone(timezone_name)


def test_base_urls_use_https(seeded: sa.orm.Session) -> None:
    for base_url in seeded.scalars(sa.select(Source.base_url)).all():
        assert base_url.startswith("https://"), f"来源未使用 https：{base_url}"


def test_market_sources_reference_seeded_instruments(seeded: sa.orm.Session) -> None:
    """跨表一致性：行情来源 config_json 里声明的 symbols 必须存在于 instruments。"""
    known = set(seeded.scalars(sa.select(Instrument.symbol)).all())
    market_sources = seeded.scalars(
        sa.select(Source).where(Source.source_type == "MARKET")
    ).all()
    assert market_sources, "Phase 1 必须至少有一个 MARKET 来源"

    for source in market_sources:
        declared = (source.config_json or {}).get("symbols", [])
        unknown = sorted(set(declared) - known)
        assert unknown == [], f"{source.name} 声明了未登记的标的：{unknown}"


def test_phase1_required_instruments_present(seeded: sa.orm.Session) -> None:
    symbols = set(seeded.scalars(sa.select(Instrument.symbol)).all())
    assert set(REQUIRED_PHASE1_SYMBOLS) <= symbols


def test_no_duplicate_natural_keys(seeded: sa.orm.Session) -> None:
    symbols = seeded.scalars(sa.select(Instrument.symbol)).all()
    names = seeded.scalars(sa.select(Source.name)).all()
    assert len(symbols) == len(set(symbols))
    assert len(names) == len(set(names))


def test_enabled_sources_declare_collector_config(seeded: sa.orm.Session) -> None:
    for source in seeded.scalars(sa.select(Source).where(Source.enabled.is_(True))).all():
        config = source.config_json or {}
        assert config.get("collector"), f"启用的来源必须声明采集器：{source.name}"
