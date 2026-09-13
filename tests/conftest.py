"""pytest 全局夹具（08_测试与验收标准）。

设计原则：
1. 单元测试（tests/unit）完全不依赖数据库。
2. 集成测试使用"每个测试独立的一次性 SQLite 文件库"，互不污染；
   生产/研究环境仍是 PostgreSQL（同一份 ORM 元数据 + 同一份 Alembic 迁移）。
3. 事实数据工厂统一走 ``resolve_effective_at``，保证测试数据本身不违反时间因果。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from src.collectors.transport import HttpRequest, HttpResponse

if TYPE_CHECKING:  # 仅类型标注使用，避免运行时导入 alembic
    from alembic.config import Config

import database.models  # noqa: F401  导入以注册全部模型元数据与不可覆盖守卫
from database.base import Base
from database.models import (
    Author,
    AuthorAccount,
    RawItem,
    RawItemType,
    Source,
    SourceType,
)
from database.session import build_engine, build_session_factory
from src.common.hashing import content_hash
from src.common.time import utc_now
from src.common.uuid7 import uuid7

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 数据库夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def sqlite_url(tmp_path: Path) -> str:
    """一次性 SQLite 文件库 URL。

    ⚠️ 仅用于本地/CI 测试：SQLite 不保存时区偏移、无 JSONB 与原生 UUID。
       生产与研究环境必须使用 PostgreSQL（见 database/session.py 顶部说明），
       因此本项目的测试只验证"可移植的约束与语义"，真实类型由 PG 上的迁移保证。
    """
    return f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture()
def engine(sqlite_url: str) -> Iterator[sa.Engine]:
    """按 ORM 元数据建表（不经过 Alembic）的引擎。"""
    created = build_engine(sqlite_url)
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture()
def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """测试用 Session：结束时不自动提交，避免掩盖事务问题。"""
    db = session_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def alembic_config_factory() -> Callable[[str], Config]:
    """返回「给定数据库 URL → 已配置的 Alembic Config」工厂（用于迁移行为测试）。"""
    from alembic.config import Config

    def _factory(database_url: str) -> Config:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
        config.set_main_option("sqlalchemy.url", database_url)
        return config

    return _factory


@pytest.fixture()
def migrated_engine(
    tmp_path: Path, alembic_config_factory: Callable[[str], Config]
) -> Iterator[sa.Engine]:
    """通过 Alembic ``upgrade head`` 建库的引擎（用于迁移/元数据漂移校验）。"""
    from alembic import command

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    command.upgrade(alembic_config_factory(database_url), "head")

    created = build_engine(database_url)
    try:
        yield created
    finally:
        created.dispose()


# ---------------------------------------------------------------------------
# 数据工厂（避免测试之间复制粘贴，统一时间语义）
# ---------------------------------------------------------------------------
@pytest.fixture()
def make_source(session: Session) -> Callable[..., Source]:
    def _make_source(**overrides: object) -> Source:
        defaults: dict[str, object] = {
            "name": f"source-{uuid.uuid4().hex[:8]}",
            "source_type": SourceType.WEIBO,
        }
        defaults.update(overrides)
        source = Source(**defaults)  # type: ignore[arg-type]
        session.add(source)
        session.flush()
        return source

    return _make_source


@pytest.fixture()
def make_author_account(
    session: Session, make_source: Callable[..., Source]
) -> Callable[..., AuthorAccount]:
    def _make_author_account(**overrides: object) -> AuthorAccount:
        source = overrides.pop("source", None) or make_source()
        author = overrides.pop("author", None)
        if author is None:
            author = Author(display_name=f"author-{uuid.uuid4().hex[:8]}")
            session.add(author)
            session.flush()
        defaults: dict[str, object] = {
            "author": author,
            "source": source,
            "external_account_id": uuid.uuid4().hex,
            "account_name": "test-account",
        }
        defaults.update(overrides)
        account = AuthorAccount(**defaults)  # type: ignore[arg-type]
        session.add(account)
        session.flush()
        return account

    return _make_author_account


@pytest.fixture()
def make_raw_item(
    session: Session, make_source: Callable[..., Source]
) -> Callable[..., RawItem]:
    """构造符合时间语义的 raw_items 记录（effective_at = max(published_at, collected_at)）。"""

    def _make_raw_item(**overrides: object) -> RawItem:
        source = overrides.pop("source", None)
        source_id = overrides.pop("source_id", None)
        if source_id is None:
            source_id = (source or make_source()).id  # type: ignore[union-attr]
        collected_at = overrides.pop("collected_at", None) or utc_now()
        assert isinstance(collected_at, datetime)
        published_at = overrides.pop("published_at", collected_at - timedelta(minutes=5))
        if published_at is None:
            effective_at = collected_at
        else:
            assert isinstance(published_at, datetime)
            effective_at = max(published_at, collected_at)

        title = overrides.pop("title", "gold outlook")
        content_text = overrides.pop("content_text", "XAUUSD 短线偏多")
        defaults: dict[str, object] = {
            "source_id": source_id,
            "source_record_id": uuid.uuid4().hex,
            "item_type": RawItemType.POST,
            "title": title,
            "content_text": content_text,
            "raw_json": {"id": uuid.uuid4().hex, "text": content_text},
            "source_url": "https://example.invalid/post/1",
            "content_hash": content_hash(title, content_text),
            "published_at": published_at,
            "collected_at": collected_at,
            "effective_at": effective_at,
        }
        defaults.update(overrides)
        item = RawItem(**defaults)  # type: ignore[arg-type]
        session.add(item)
        session.flush()
        return item

    return _make_raw_item


# ---------------------------------------------------------------------------
# 采集器测试替身（★ 保证测试完全不访问外部网络 ★）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ★ 全局守卫：测试套件禁止访问外部网络（强制要求）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """拦截测试期间的一切真实外部网络（**两层防线**）。

    背景：采集器与 LLM 测试必须使用 Mock 传输层，绝不真实抓取外部站点
    （避免被封 IP、拖慢测试、让 CI 依赖外网，以及**把付费 API 的 token 烧掉**）。

    第一层（socket）：拦截对非 127.0.0.1 / ::1 / localhost 的连接
    —— 覆盖 aiohttp 采集器与裸 socket。

    第二层（httpcore / httpx）：直接让真实建连的后端抛错。
    **为什么必须加第二层**：Windows 上常配置本机代理（实测 127.0.0.1:7892），
    此时 socket 的目的地址是 127.0.0.1，第一层会放行 → httpx 仍能经代理打到外网
    （2026-09-13 实测：未注入 MockTransport 的 httpx 请求真的到达了 api.deepseek.com）。
    注意：`httpx.MockTransport` 不经过后端 → 不受影响。
    """
    import importlib
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(f"测试套件禁止访问外部网络：{address!r}")
        return real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    def blocked_backend_call(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "测试套件禁止真实 HTTP 建连（httpx/httpcore）：请注入 MockTransport"
        )

    for module_name, class_name in (
        ("httpcore._backends.sync", "SyncBackend"),
        ("httpcore._backends.anyio", "AnyIOBackend"),
    ):
        backend = getattr(importlib.import_module(module_name), class_name, None)
        if backend is None:  # pragma: no cover - 依赖升级时的兜底
            continue
        for method in ("connect_tcp", "connect_unix_socket"):
            if hasattr(backend, method):
                monkeypatch.setattr(backend, method, blocked_backend_call)


class MockTransport:
    """可脚本化的 Mock 传输层。

    脚本项为 :class:`~src.collectors.transport.HttpResponse`，或"要抛出的异常实例"
    （例如 :class:`~src.collectors.errors.TransportTimeoutError`、``asyncio.TimeoutError``、
    ``aiohttp.ClientError`` 子类），按调用顺序逐个消费。

    用法::

        transport = MockTransport([HttpResponse(429), HttpResponse(200, json_body={...})])
        ...
        transport.push(HttpResponse(500))   # 追加后续响应（多轮采集场景）
    """

    def __init__(self, script: Sequence[Any]) -> None:
        self._script: list[Any] = list(script)
        self.requests: list[HttpRequest] = []

    @property
    def calls(self) -> int:
        """已发生的请求次数（用于断言重试次数）。"""
        return len(self.requests)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def push(self, *items: Any) -> None:
        """追加脚本项（用于"中断后续采"等多轮场景）。"""
        self._script.extend(items)

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("MockTransport 脚本已耗尽：请检查测试脚本长度")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingSleep:
    """记录退避时长的假 sleep：不真正等待，便于断言重试退避是否符合预期。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture()
def mock_transport() -> Callable[[Sequence[Any]], MockTransport]:
    """返回构造 :class:`MockTransport` 的工厂（脚本可在测试中追加）。"""

    def _factory(script: Sequence[Any]) -> MockTransport:
        return MockTransport(script)

    return _factory


@pytest.fixture()
def recording_sleep() -> RecordingSleep:
    """返回记录退避时长的假 sleep。"""
    return RecordingSleep()


@pytest.fixture()
def fixed_uuid() -> Callable[[int], uuid.UUID]:
    """生成基于指定毫秒时间戳的 UUID v7（便于断言时间有序性）。"""
    return uuid7
