"""实盘安全门禁测试（.clinerules 第二条 / 06_Cline开发规则 第 18 条）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings, assert_live_trading_disabled
from scripts.phase1_pipeline_report import _mask_url
from src.common.exceptions import ConfigSecurityError

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主环境变量，使测试只验证**代码默认值**。

    回归背景（GOLD-003 修复 GOLD-002-R1 遗留问题）：``Settings(_env_file=None)`` 只关闭
    ``.env`` 文件读取，pydantic-settings **仍会读取进程环境变量**。开发 / CI 机器上只要导出过
    ``FRED_API_KEY``，``test_defaults_are_safe`` 就会假失败（实测本机 shell 已导出该变量，
    失败信息为 ``assert True is False``）。

    这里删除相关宿主变量而不是改生产代码——生产配置读取环境变量的能力必须保留，
    由 :func:`test_environment_variables_are_still_honored_for_secrets` 明确锁定。
    """
    for name in (
        "FRED_API_KEY",
        "DEEPSEEK_API_KEY",
        "LIVE_TRADING",
        "ALLOW_EXTERNAL_ORDER_SUBMISSION",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_safe(isolated_host_env: None) -> None:
    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.allow_external_order_submission is False
    assert settings.fred_configured is False


def test_environment_variables_are_still_honored_for_secrets(
    isolated_host_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生产配置**读取环境变量**的能力不得因测试隔离而被削弱。"""
    monkeypatch.setenv("FRED_API_KEY", "env-secret-value")

    settings = Settings(_env_file=None)

    assert settings.fred_configured is True
    assert "env-secret-value" not in repr(settings)


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


def test_pipeline_report_masks_database_password() -> None:
    url = "postgresql+psycopg://gold_ai_app:do-not-print@127.0.0.1:5432/gold_ai"

    masked = _mask_url(url)

    assert masked == "postgresql+psycopg://gold_ai_app:***@127.0.0.1:5432/gold_ai"
    assert "do-not-print" not in masked
