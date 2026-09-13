"""Alembic 迁移环境。

关键设计：
1. 数据库 URL 不写死在 alembic.ini 中，解析优先级：
   ``-x db_url=...``（测试/CI） > ``alembic.ini`` > 环境变量 ``DATABASE_URL``。
2. ``target_metadata = Base.metadata``：Alembic 会自动继承元数据的命名约定
   （见 alembic/operations/schemaobj.py），因此 migration 与 ORM 的约束名一致，
   "迁移结果 == ORM 元数据"可由测试严格比对（tests/integration）。
3. ``compare_type=True``：字段类型变更可被 autogenerate 检出，避免 SQLite/PG 漂移。
4. SQLite 下开启 ``render_as_batch``（仅本地测试库使用）。
"""

from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

import database.models  # noqa: F401  必须导入：注册全部 ORM 模型到 Base.metadata
from config.settings import get_settings
from database.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False 是必须的：
    # stdlib fileConfig 默认会把"已存在但未在 alembic.ini 中声明"的 logger 全部禁用，
    # 而迁移经常在**已配置日志的进程内**执行（Scheduler / API / 测试套件），
    # 使用默认值会导致 gold_ai.* 日志静默消失（曾真实发生，见回归测试）。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """解析本次迁移使用的数据库 URL。"""
    x_args = context.get_x_argument(as_dictionary=True)
    override = x_args.get("db_url")
    if override:
        return override

    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured:
        return configured

    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    url = _database_url()
    connectable = sa.create_engine(url, poolclass=sa.pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=url.startswith("sqlite"),
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
