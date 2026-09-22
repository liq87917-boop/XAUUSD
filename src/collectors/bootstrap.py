"""生产采集器注册与构造（bootstrap）——**与 Scheduler 核心解耦**。

职责：
1. :func:`ensure_collectors_registered`：导入具体采集器模块，触发 ``@register_collector``
   副作用注册（幂等：重复调用无副作用）；
2. :func:`default_collector_factory`：返回 ``Source → BaseCollector`` 的生产工厂，
   按 ``sources.config_json['collector']`` 查注册表构造采集器，并注入
   :class:`~src.collectors.transport.AiohttpTransport`。

为什么单独成模块（TD-09）：
- Scheduler 核心只做"调度 + 幂等 + 故障隔离"，**不得**出现 provider URL / 解析逻辑 /
  业务分支；采集器注册是"副作用导入"，必须留在 bootstrap/CLI 层，
  避免核心模块受导入顺序影响（也不允许通过改 smoke/dry-run 脚本来实现注册）。
"""

from __future__ import annotations

from collections.abc import Callable

from database.models import Source
from src.collectors.base import BaseCollector
from src.collectors.registry import collector_for_source
from src.collectors.transport import AiohttpTransport, Transport

__all__ = ["CollectorFactory", "default_collector_factory", "ensure_collectors_registered"]

CollectorFactory = Callable[[Source], BaseCollector]


def ensure_collectors_registered() -> None:
    """导入 ``src.collectors``（内含全部具体采集器）以完成注册。

    备注：``src/collectors/__init__.py`` 已做副作用导入；本函数只是把"需要注册"
    这一语义显式化，供 CLI / 测试调用。
    """
    import src.collectors  # noqa: F401  # 副作用导入：完成 @register_collector


def default_collector_factory(*, transport: Transport | None = None) -> CollectorFactory:
    """构造生产采集器工厂。

    Args:
        transport: HTTP 传输实现；``None`` 时惰性创建 :class:`AiohttpTransport`
            （库型采集器如 ``akshare_gold`` 不使用 HTTP，多余参数会被忽略）。
    """
    ensure_collectors_registered()
    shared_transport: Transport = transport if transport is not None else AiohttpTransport()

    def _factory(source: Source) -> BaseCollector:
        return collector_for_source(source, transport=shared_transport)

    return _factory
