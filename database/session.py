"""数据库引擎与 Session 工厂。

环境约束（强制）：
- **生产 / 研究环境必须使用 PostgreSQL**（``postgresql+psycopg://...``）：
  JSONB / 原生 UUID / TIMESTAMPTZ、并发写入与后续分区能力都依赖它；
- SQLite（``sqlite+pysqlite:///...``）**仅允许**用于本地开发与自动化测试，
  它不保存时区偏移，不能作为采集数据与研究事实数据的长期存储。

设计说明（Phase 1）：
- 采集器的网络并发使用 ``asyncio + aiohttp``（后续步骤），数据库写入保持同步
  SQLAlchemy 2.0 ``Session``：研究脚本（pandas / scikit-learn）可直接复用同一套
  repository，且避免"事件循环中混用异步驱动"带来的隐性阻塞。
- 事务边界由 :func:`session_scope` 统一管理：成功提交、异常回滚并**重新抛出**
  （06_Cline开发规则 第 24 条：禁止捕获异常后静默忽略）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings, get_settings

__all__ = ["build_engine", "build_session_factory", "session_scope"]


def build_engine(
    database_url: str | None = None,
    *,
    echo: bool = False,
    settings: Settings | None = None,
) -> Engine:
    """创建 SQLAlchemy 引擎。

    Args:
        database_url: 显式数据库 URL；默认取配置 ``DATABASE_URL``。
            生产/研究环境应为 ``postgresql+psycopg://...``；
            ``sqlite+pysqlite:///...`` 仅用于本地开发与单元测试。
        echo: 是否打印 SQL（调试用）。
        settings: 可选配置对象（测试注入用）。

    Returns:
        已配置的 :class:`sqlalchemy.Engine`。
    """
    config = settings or get_settings()
    url = database_url or config.database_url

    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # 内存库必须使用 StaticPool，否则每个连接看到的是不同数据库
            kwargs["poolclass"] = sa.pool.StaticPool

    engine = sa.create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite 默认不强制外键约束，本地测试必须显式打开，否则 FK 形同虚设。"""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def build_session_factory(
    engine: Engine,
    *,
    expire_on_commit: bool = False,
) -> sessionmaker[Session]:
    """构造 Session 工厂。

    ``expire_on_commit=False``：提交后仍可读取对象属性，便于 API/研究脚本使用；
    数据正确性由数据库约束与显式查询保证，而非依赖对象过期。
    """
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=expire_on_commit, future=True
    )


@contextmanager
def session_scope(
    factory: sessionmaker[Session] | None = None,
    *,
    engine: Engine | None = None,
) -> Iterator[Session]:
    """事务作用域上下文管理器。

    用法::

        with session_scope() as session:
            session.add(record)

    Raises:
        Exception: 原样抛出业务/数据库异常，绝不静默吞掉。
    """
    if factory is None:
        factory = build_session_factory(engine or build_engine())

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
