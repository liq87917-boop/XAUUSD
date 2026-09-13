"""采集器注册表：``collector_name`` ↔ 采集器类。

与数据源配置的关系（Phase 1 第 1 步种子的 ``sources.config_json``）：
    ``sources.config_json["collector"]`` 保存采集器名称（如 ``weibo_collector``），
    本注册表按该名称查找实现类，从而做到"新增来源只改配置、不改代码"。

⚠️ 现状说明（禁止伪完成）：Phase 1 第 2 步只交付**框架**，生产环境的注册表在
   Step 3 之前是空的；具体采集器（微博 / 新闻 / 行情 / 宏观）将在 Step 3 实现并
   通过 ``@register_collector`` 注册。测试使用测试内定义的 Mock 采集器验证机制。
"""

from __future__ import annotations

from typing import Any

from database.models import Source
from src.collectors.base import BaseCollector
from src.collectors.errors import CollectorNotRegisteredError

__all__ = [
    "available_collectors",
    "build_collector",
    "collector_for_source",
    "get_collector_class",
    "register_collector",
    "source_collector_name",
    "unregister_collector",
]

_REGISTRY: dict[str, type[BaseCollector]] = {}


def register_collector(collector_cls: type[BaseCollector]) -> type[BaseCollector]:
    """注册采集器类（可作装饰器使用）。

    Raises:
        ValueError: ``collector_name`` 为空或与已有实现冲突（防止静默覆盖）。
    """
    name = collector_cls.collector_name
    if not name:
        raise ValueError(f"{collector_cls.__name__} 未声明 collector_name")

    existing = _REGISTRY.get(name)
    if existing is not None and existing is not collector_cls:
        raise ValueError(
            f"采集器名称冲突：{name} 已由 {existing.__name__} 注册，"
            f"不能覆盖为 {collector_cls.__name__}"
        )

    _REGISTRY[name] = collector_cls
    return collector_cls


def unregister_collector(name: str) -> None:
    """注销采集器（仅供测试清理注册表使用）。"""
    _REGISTRY.pop(name, None)


def available_collectors() -> tuple[str, ...]:
    """已注册的采集器名称（Step 3 之前为空是预期状态）。"""
    return tuple(sorted(_REGISTRY))


def get_collector_class(name: str) -> type[BaseCollector]:
    """按名称取采集器类。

    Raises:
        CollectorNotRegisteredError: 名称未注册（附当前已注册清单，便于排查）。
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise CollectorNotRegisteredError(
            f"未注册的采集器：{name!r}；当前已注册：{list(available_collectors())}。"
            "具体采集器将在 Phase 1 第 3 步实现并注册",
            details={"collector_name": name, "available": list(available_collectors())},
        ) from exc


def source_collector_name(source: Source) -> str:
    """从数据源配置中解析采集器名称。

    Raises:
        CollectorNotRegisteredError: 配置缺失 ``config_json["collector"]``。
    """
    config: dict[str, Any] = dict(source.config_json or {})
    name = config.get("collector")
    if not isinstance(name, str) or not name.strip():
        raise CollectorNotRegisteredError(
            f"数据源 {source.name!r} 未配置 config_json['collector']，无法确定采集器",
            details={"source_name": source.name},
        )
    return name.strip()


def build_collector(name: str, source: Source, **kwargs: Any) -> BaseCollector:
    """构造采集器实例（``kwargs`` 透传，例如 transport / retry_policy / sleep）。"""
    return get_collector_class(name)(source, **kwargs)


def collector_for_source(source: Source, **kwargs: Any) -> BaseCollector:
    """按数据源配置构造对应采集器。"""
    return build_collector(source_collector_name(source), source, **kwargs)
