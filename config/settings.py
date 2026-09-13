"""GOLD-AI 全局配置。

对应文档：
- 06_Cline开发规则 第 7 条（时间统一 UTC）、第 18 条（实盘安全规则）
- 02_系统详细设计 3.1（后端技术栈）

设计要点：
1. 所有配置集中在此，禁止在业务代码里散落 ``os.environ`` 读取。
2. ``LIVE_TRADING`` / ``ALLOW_EXTERNAL_ORDER_SUBMISSION`` 为硬门禁：
   只要被设置为 true，配置加载阶段立即抛错，系统无法启动。
3. 系统内部统一 UTC，数据库使用 TIMESTAMPTZ。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.common.exceptions import ConfigSecurityError

DEFAULT_DATABASE_URL = "postgresql+psycopg://gold_ai:gold_ai@localhost:5432/gold_ai"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class Settings(BaseSettings):
    """GOLD-AI 运行配置（环境变量 / .env）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(default="GOLD-AI")
    app_env: str = Field(default="development")

    # 主数据库：**生产 / 研究环境必须使用 PostgreSQL**（03_数据库完整设计 第 1 节）。
    # SQLite（``sqlite+pysqlite:///...``）仅允许用于本地开发与自动化测试：
    #   - 缺少 JSONB / 原生 UUID / TIMESTAMPTZ 语义（不保存时区偏移）；
    #   - 缺少并发写入与后续按时间分区的能力；
    # 绝不可用于采集数据与研究事实数据的长期存储。
    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    redis_url: str = Field(default=DEFAULT_REDIS_URL)
    log_level: str = Field(default="INFO")

    # 时间规范：系统内部统一 UTC
    default_timezone: str = Field(default="UTC")

    # 实盘安全门禁：MVP 阶段必须保持 false
    live_trading: bool = Field(default=False)
    allow_external_order_submission: bool = Field(default=False)

    # ------------------------------------------------------------------
    # DeepSeek（Phase 2 的 LLM 观点抽取器）
    # 用 SecretStr：`repr()` / 日志里显示为 **********，从源头避免密钥泄漏；
    # 密钥只允许来自环境变量或本地 .env（`.gitignore` 已覆盖），**禁止硬编码**。
    # ------------------------------------------------------------------
    deepseek_api_key: SecretStr | None = Field(default=None)
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    # 2026-09-13 实测：`GET /models` 只返回 deepseek-flash / deepseek-v4-pro，
    # 旧名 `deepseek-chat` 会被静默路由到 flash（模型名是缓存键的一部分，故写实际模型名）。
    deepseek_model: str = Field(default="deepseek-flash")
    # 抽取任务不需要思维链：新一代模型**默认开启**思考模式，显式关掉更便宜也更稳。
    deepseek_thinking_mode: str = Field(default="disabled")

    @field_validator("deepseek_thinking_mode")
    @classmethod
    def _validate_thinking_mode(cls, value: str) -> str:
        """只允许 DeepSeek 文档里的两个取值，避免拼错后静默走默认（= 开启）。"""
        allowed = {"disabled", "enabled"}
        cleaned = value.strip().lower()
        if cleaned not in allowed:
            raise ValueError(
                f"DEEPSEEK_THINKING_MODE 必须是 {sorted(allowed)} 之一，得到 {value!r}"
            )
        return cleaned

    @property
    def deepseek_configured(self) -> bool:
        """是否已配置可用的 DeepSeek 密钥（空串视为未配置）。"""
        if self.deepseek_api_key is None:
            return False
        return bool(self.deepseek_api_key.get_secret_value().strip())

    @field_validator("live_trading", "allow_external_order_submission")
    @classmethod
    def _reject_enabled_trading_flags(cls, value: bool, info: object) -> bool:
        """硬门禁：禁止把实盘相关开关打开。"""
        if value:
            field_name = getattr(info, "field_name", "unknown")
            raise ConfigSecurityError(
                f"{field_name} 必须为 false：MVP 阶段禁止实盘交易与外部下单 "
                "（.clinerules 第二条 / 06_Cline开发规则 第 18 条）。"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def _require_sqlalchemy_url(cls, value: str) -> str:
        """校验数据库 URL 形态，避免误填裸 DSN 导致连接层报错。"""
        value = value.strip()
        if "+" not in value.split("://", 1)[0]:
            raise ValueError(
                "DATABASE_URL 必须使用 SQLAlchemy 驱动前缀，"
                "例如 postgresql+psycopg://user:pwd@host:5432/dbname 或 sqlite+pysqlite:///./x.db"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。"""
    return Settings()


def reset_settings_cache() -> None:
    """清空配置缓存（测试用）。"""
    get_settings.cache_clear()


def assert_live_trading_disabled(settings: Settings | None = None) -> None:
    """运行期二次门禁：任何下单/执行链路调用前必须通过。"""
    current = settings or get_settings()
    if current.live_trading or current.allow_external_order_submission:
        raise ConfigSecurityError(
            "检测到实盘开关被打开，禁止执行任何真实交易动作"
            "（LIVE_TRADING / ALLOW_EXTERNAL_ORDER_SUBMISSION 必须为 false）。"
        )
