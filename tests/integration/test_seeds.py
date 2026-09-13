"""种子脚本集成测试（Phase 1 幂等 / 不覆盖 / 前置校验 / CLI）。

覆盖点：
- 定义全部落地，字段值与定义一致；
- 重复执行幂等（不新增、不重复）；
- **已存在记录不被修改**（种子只补齐，不覆盖运维配置）；
- 未迁移的空库给出可执行指引（SeedError）；
- dry-run 不落库；scope 过滤正确；
- CLI（``python -m database.seeds``）退出码与输出。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from database.models import Instrument, Source
from database.models.enums import AssetClass, SourceType
from database.seeds import (
    INSTRUMENT_SEEDS,
    SOURCE_SEEDS,
    seed_database,
    seed_instruments,
    seed_sources,
)
from database.session import build_engine
from src.common.exceptions import SeedError

pytestmark = pytest.mark.integration

INSTRUMENT_SYMBOLS = tuple(seed.symbol for seed in INSTRUMENT_SEEDS)
SOURCE_NAMES = tuple(seed.name for seed in SOURCE_SEEDS)


def _count(session: sa.orm.Session, model: type) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def _migrated_url(tmp_path: Path, alembic_config_factory, name: str = "seeded.db") -> str:
    """用 Alembic 迁移建表（与生产同结构）后返回该库 URL。"""
    from alembic import command

    database_url = f"sqlite+pysqlite:///{(tmp_path / name).as_posix()}"
    command.upgrade(alembic_config_factory(database_url), "head")
    return database_url


def test_seed_instruments_creates_all_definitions(session) -> None:
    result = seed_instruments(session)

    assert result.entity == "instruments"
    assert result.created == INSTRUMENT_SYMBOLS
    assert result.existing == ()
    assert _count(session, Instrument) == len(INSTRUMENT_SEEDS)


def test_seed_instruments_is_idempotent(session) -> None:
    seed_instruments(session)
    again = seed_instruments(session)

    assert again.created == ()
    assert again.existing == INSTRUMENT_SYMBOLS
    assert _count(session, Instrument) == len(INSTRUMENT_SEEDS)


def test_seed_sources_creates_all_definitions(session) -> None:
    result = seed_sources(session)

    assert result.created == SOURCE_NAMES
    assert _count(session, Source) == len(SOURCE_SEEDS)


def test_seed_does_not_modify_existing_rows(session) -> None:
    """种子只补齐、不覆盖：人工调整过的字段必须原样保留。"""
    seed_instruments(session)
    xauusd = session.scalar(sa.select(Instrument).where(Instrument.symbol == "XAUUSD"))
    assert xauusd is not None
    xauusd.name = "人工改名-黄金现货"
    xauusd.enabled = False
    session.flush()

    seed_instruments(session)
    session.refresh(xauusd)

    assert xauusd.name == "人工改名-黄金现货"
    assert xauusd.enabled is False


def test_seeded_fields_match_04_definition(session) -> None:
    seed_instruments(session)
    seed_sources(session)

    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == "US10Y"))
    assert instrument is not None
    assert instrument.asset_class == AssetClass.BOND
    assert instrument.quote_currency == "USD"
    assert instrument.timezone == "UTC"
    assert instrument.enabled is True

    domestic = session.scalar(sa.select(Instrument).where(Instrument.symbol == "SGE_AU9999"))
    assert domestic is not None
    assert domestic.timezone == "Asia/Shanghai"

    source = session.scalar(sa.select(Source).where(Source.name == "weibo_main"))
    assert source is not None
    assert source.source_type == SourceType.WEIBO
    assert source.base_url == "https://m.weibo.cn"
    assert source.timezone == "Asia/Shanghai"
    assert source.enabled is True
    assert source.config_json is not None
    assert source.config_json["collector"] == "weibo_collector"


def test_backup_source_stays_disabled(session) -> None:
    seed_sources(session)
    seed_sources(session)

    backup = session.scalar(sa.select(Source).where(Source.name == "market_stooq_backup"))
    assert backup is not None
    assert backup.enabled is False


def test_seed_database_requires_migrated_schema(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'empty.db').as_posix()}"

    with pytest.raises(SeedError) as excinfo:
        seed_database(database_url)

    message = str(excinfo.value)
    assert "alembic upgrade head" in message
    assert "instruments" in message


def test_seed_database_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        seed_database("sqlite+pysqlite:///:memory:", scope="opinions")


def test_seed_database_dry_run_does_not_persist(tmp_path: Path, alembic_config_factory) -> None:
    database_url = _migrated_url(tmp_path, alembic_config_factory, "dry_run.db")

    results = seed_database(database_url, dry_run=True)
    assert results["instruments"].created_count == len(INSTRUMENT_SEEDS)

    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            remaining = connection.execute(sa.text("SELECT COUNT(*) FROM instruments")).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()


def test_seed_database_end_to_end_is_idempotent(tmp_path: Path, alembic_config_factory) -> None:
    database_url = _migrated_url(tmp_path, alembic_config_factory, "e2e.db")

    first = seed_database(database_url, scope="all")
    assert first["instruments"].created_count == len(INSTRUMENT_SEEDS)
    assert first["sources"].created_count == len(SOURCE_SEEDS)

    second = seed_database(database_url, scope="all")
    assert second["instruments"].created_count == 0
    assert second["instruments"].existing_count == len(INSTRUMENT_SEEDS)
    assert second["sources"].created_count == 0
    assert second["sources"].existing_count == len(SOURCE_SEEDS)


def test_seed_database_scope_instruments_only(tmp_path: Path, alembic_config_factory) -> None:
    database_url = _migrated_url(tmp_path, alembic_config_factory, "scope.db")

    results = seed_database(database_url, scope="instruments")

    assert set(results) == {"instruments"}
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            source_rows = connection.execute(sa.text("SELECT COUNT(*) FROM sources")).scalar_one()
        assert source_rows == 0
    finally:
        engine.dispose()


def test_cli_main_seeds_and_reports(tmp_path: Path, capsys, alembic_config_factory) -> None:
    from database.seeds.__main__ import main

    database_url = _migrated_url(tmp_path, alembic_config_factory, "cli.db")

    exit_code = main(["--db-url", database_url, "--scope", "instruments"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "instruments: 新增" in captured.out
    assert "XAUUSD" in captured.out
    assert "完成" in captured.out


def test_masked_url_hides_password() -> None:
    from database.seeds.__main__ import _masked_url

    masked = _masked_url("postgresql+psycopg://gold_ai:sup3r-secret@db:5432/gold_ai")

    assert "sup3r-secret" not in masked
    assert "***" in masked
    assert masked.startswith("postgresql+psycopg://gold_ai")


def test_cli_main_returns_nonzero_on_database_error(tmp_path: Path, capsys) -> None:
    """库不可达时必须返回退出码 1 并打印可读错误，而不是静默成功。"""
    from database.seeds.__main__ import main

    missing_dir_url = f"sqlite+pysqlite:///{(tmp_path / 'no-such-dir' / 'x.db').as_posix()}"

    exit_code = main(["--db-url", missing_dir_url, "--scope", "instruments"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "[seeds] 数据库错误" in captured.err

