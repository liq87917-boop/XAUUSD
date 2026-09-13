"""日志系统（02_系统详细设计 §4：config/logging.yaml）。

设计要点：
- 日志配置集中在 ``config/logging.yaml``，由本模块用 ``logging.config.dictConfig`` 加载；
  业务代码只使用 ``get_logger(__name__)``，不各自配置 handler（避免重复输出）。
- 文件日志默认写入 ``logs/``（.gitignore 已忽略），轮转 10MB × 10 份；
  按 03_数据库完整设计 §16，普通日志保留 90~180 天，审计信息走 ``audit_logs`` 表。
- 采集统计不依赖日志：每轮采集的 fetched/inserted/duplicate/failed 必须落到
  ``collector_runs``，日志仅用于排障（Phase 1 验收要求"失败任务可重试"）。
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path
from typing import Any

import yaml

__all__ = ["configure_logging", "get_logger", "load_logging_config"]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOGGING_CONFIG = REPO_ROOT / "config" / "logging.yaml"
LOGGER_NAMESPACE = "gold_ai"

_configured = False


def load_logging_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取并返回 logging 配置字典（UTF-8）。

    Raises:
        FileNotFoundError: 配置文件缺失。
        ValueError: 配置内容不是 YAML 映射。
    """
    config_path = Path(path) if path is not None else DEFAULT_LOGGING_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(f"logging 配置不存在：{config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)

    if not isinstance(loaded, dict):
        raise ValueError(f"logging 配置必须是 YAML 映射：{config_path}")
    return loaded


def configure_logging(
    *,
    level: str | None = None,
    path: str | Path | None = None,
    force: bool = False,
) -> None:
    """应用日志配置（幂等；``force=True`` 时重新应用）。

    Args:
        level: 覆盖 ``gold_ai`` 命名空间的日志级别（来自 Settings.log_level）。
        path: 自定义配置文件路径（测试使用）。
        force: 是否强制重新配置。
    """
    global _configured
    if _configured and not force:
        return

    config = load_logging_config(path)
    if level:
        loggers = config.setdefault("loggers", {})
        namespace = loggers.setdefault(LOGGER_NAMESPACE, {})
        namespace["level"] = level.upper()

    # 文件 handler 需要目录存在（RotatingFileHandler 不会自动建目录）
    for handler in (config.get("handlers") or {}).values():
        filename = (handler or {}).get("filename")
        if filename:
            Path(filename).parent.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig(config)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取命名空间下的 logger（``gold_ai.<name>``）。"""
    if name == LOGGER_NAMESPACE or name.startswith(f"{LOGGER_NAMESPACE}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")
