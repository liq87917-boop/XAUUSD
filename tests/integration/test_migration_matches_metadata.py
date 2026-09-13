"""迁移漂移测试：Alembic 迁移结果必须与 ORM 元数据完全一致。

这是防止「ORM 改了、迁移没跟上」导致生产库结构错误的核心测试
（06_Cline开发规则 第 10 条：改 ORM → 生成/编写 migration → 执行 → 跑测试）。

比较维度：表清单、列(类型/可空)、主键、唯一约束、CHECK 约束(名称+表达式)、
外键、索引(名称+是否唯一)。任何一项不一致都会让本测试失败。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

import database.models as models  # noqa: F401  确保元数据完整注册
from database.session import build_engine

pytestmark = [pytest.mark.integration, pytest.mark.regression]

ALEMBIC_VERSION_TABLE = "alembic_version"


def _normalize_expression(expression: str | None) -> str:
    """归一化 CHECK 表达式文本，避免引号/空白造成的假差异。"""
    text = expression or ""
    for char in ('"', "`", "[", "]"):
        text = text.replace(char, "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _normalize_default(value: object) -> str | None:
    """归一化列默认值（SQLite 回显常带引号，例如 ``'0'`` → ``0``）。"""
    if value is None:
        return None
    return str(value).strip().strip("'").strip('"').lower() or None


def _schema_snapshot(engine: sa.Engine) -> dict[str, dict[str, Any]]:
    inspector = inspect(engine)
    snapshot: dict[str, dict[str, Any]] = {}
    for table in sorted(inspector.get_table_names()):
        if table == ALEMBIC_VERSION_TABLE:
            continue
        snapshot[table] = {
            # 同时比较列类型、可空性与 server_default（后者能发现"只加了 ORM 默认值、
            # 迁移里没写同样默认值"这类隐性漂移）
            "columns": {
                column["name"]: (
                    str(column["type"]).upper(),
                    bool(column["nullable"]),
                    _normalize_default(column.get("default")),
                )
                for column in inspector.get_columns(table)
            },
            "primary_key": sorted(inspector.get_pk_constraint(table)["constrained_columns"]),
            "unique_constraints": sorted(
                constraint["name"] for constraint in inspector.get_unique_constraints(table)
            ),
            "check_constraints": sorted(
                (constraint["name"], _normalize_expression(constraint["sqltext"]))
                for constraint in inspector.get_check_constraints(table)
            ),
            "foreign_keys": sorted(
                fk["name"] for fk in inspector.get_foreign_keys(table) if fk["name"]
            ),
            "indexes": sorted(
                (index["name"], bool(index["unique"])) for index in inspector.get_indexes(table)
            ),
        }
    return snapshot


def test_migrated_schema_equals_orm_metadata(engine: sa.Engine, migrated_engine: sa.Engine) -> None:
    assert _schema_snapshot(migrated_engine) == _schema_snapshot(engine)


def test_migrated_schema_contains_exactly_documented_tables(migrated_engine: sa.Engine) -> None:
    """迁移建出的表必须严格等于「文档规定的当前阶段表集合」（Phase 1 + Phase 2）。"""
    tables = set(inspect(migrated_engine).get_table_names()) - {ALEMBIC_VERSION_TABLE}
    assert tables == set(models.ALL_TABLES)


def test_alembic_head_is_single_revision(
    migrated_engine: sa.Engine, alembic_config_factory
) -> None:
    """数据库版本必须等于脚本目录的唯一 head（不写死 revision 号，避免每次加迁移都要改测试）。"""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config_factory("sqlite+pysqlite:///:memory:"))
    heads = script.get_heads()
    assert len(heads) == 1, f"存在多个 head，migration 分支未合并：{heads}"

    with migrated_engine.connect() as connection:
        versions = (
            connection.execute(sa.text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}"))
            .scalars()
            .all()
        )
    assert list(versions) == list(heads)


def test_downgrade_removes_all_phase1_tables(
    tmp_path: Path, alembic_config_factory
) -> None:
    """downgrade 必须可运行（03_数据库完整设计 第 19 节：迁移可回滚）。"""
    from alembic import command

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'downgrade.db').as_posix()}"
    config = alembic_config_factory(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = build_engine(database_url)
    try:
        remaining = set(inspect(engine).get_table_names()) - {ALEMBIC_VERSION_TABLE}
        assert remaining == set()
    finally:
        engine.dispose()


def test_alembic_does_not_disable_existing_loggers(
    tmp_path: Path, alembic_config_factory
) -> None:
    """回归测试：迁移不得禁用已有 logger。

    背景：``env.py`` 中的 ``logging.config.fileConfig`` 默认 ``disable_existing_loggers=True``，
    会把未在 alembic.ini 中声明的 ``gold_ai.*`` logger 全部 ``disabled=True`` ——
    由于迁移常在已配置日志的进程内执行（Scheduler / API / 测试套件），
    这会让采集器日志静默消失（真实 bug，已修复为 ``disable_existing_loggers=False``）。
    """
    import logging

    from alembic import command

    logger = logging.getLogger("gold_ai.collectors.regression-probe")
    original_disabled = logger.disabled
    logger.disabled = False
    try:
        database_url = f"sqlite+pysqlite:///{(tmp_path / 'logging.db').as_posix()}"
        command.upgrade(alembic_config_factory(database_url), "head")

        assert logger.disabled is False, (
            "alembic 迁移禁用了已存在的 logger；env.py 必须使用 disable_existing_loggers=False"
        )
    finally:
        logger.disabled = original_disabled


def test_alembic_console_formatter_renders_log_records(
    tmp_path: Path, alembic_config_factory, capsys
) -> None:
    """回归测试：alembic.ini 的 console formatter 必须真正渲染日志内容。

    背景（真实 bug）：``logging.config.fileConfig()`` 用 ``raw=True`` 读取
    ``format`` / ``datefmt``，configparser 插值**不会**应用，因此这两个值里写 ``%%``
    会把字面量 ``%%(levelname)-5.5s [%%(name)s] %%(message)s`` 交给
    ``logging.Formatter`` —— 结果是每条迁移日志只打印格式串本身，**alembic / sqlalchemy
    的日志内容全部丢失**（``[alembic]`` 段仍必须写 ``%%``，因为 Alembic 是带插值读的）。
    """
    import logging

    from alembic import command

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        database_url = f"sqlite+pysqlite:///{(tmp_path / 'formatter.db').as_posix()}"
        # env.py 在此过程中执行 fileConfig(alembic.ini) → 配置根 logger 的 console handler
        command.upgrade(alembic_config_factory(database_url), "head")

        logging.getLogger().warning("formatter-probe")
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert "formatter-probe" in combined, (
            "alembic.ini 的 formatter 没有渲染出日志内容（检查是否误用了 %% 转义）"
        )
        assert "%(message)s" not in combined
        assert "%(levelname)" not in combined
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
