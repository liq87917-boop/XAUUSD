"""实盘安全门禁测试（.clinerules 第二条 / 06_Cline开发规则 第 18 条）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings, assert_live_trading_disabled
from src.common.exceptions import ConfigSecurityError

pytestmark = pytest.mark.unit


def test_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.allow_external_order_submission is False
    assert settings.fred_configured is False


def test_fred_key_is_secret_and_reports_only_configuration_state() -> None:
    settings = Settings(_env_file=None, fred_api_key="fred-secret-value")

    assert settings.fred_configured is True
    assert "fred-secret-value" not in repr(settings)


def test_enabling_live_trading_is_rejected_at_config_load() -> None:
    """任何把 LIVE_TRADING 设为 true 的行为都必须导致系统无法启动。"""
    with pytest.raises(ConfigSecurityError):
        Settings(_env_file=None, live_trading=True)


def test_enabling_external_order_submission_is_rejected() -> None:
    with pytest.raises(ConfigSecurityError):
        Settings(_env_file=None, allow_external_order_submission=True)


def test_runtime_gate_blocks_after_mutation() -> None:
    """运行期二次门禁：即使配置对象被改坏，下单链路调用前仍会被拦截。"""
    settings = Settings(_env_file=None)
    assert_live_trading_disabled(settings)

    settings.live_trading = True
    with pytest.raises(ConfigSecurityError):
        assert_live_trading_disabled(settings)


def test_database_url_requires_sqlalchemy_driver_prefix() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://gold_ai@localhost:5432/gold_ai")


def test_database_url_accepts_postgresql_and_sqlite_drivers() -> None:
    postgres = Settings(
        _env_file=None, database_url="postgresql+psycopg://gold_ai:gold_ai@localhost:5432/gold_ai"
    )
    assert postgres.database_url.startswith("postgresql+psycopg://")

    sqlite = Settings(_env_file=None, database_url="sqlite+pysqlite:///./local.db")
    assert sqlite.database_url.startswith("sqlite+pysqlite:///")
