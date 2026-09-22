"""采集调度包（TD-09：每 30 分钟任务框架）。

分层（严禁耦合）：
- :mod:`src.scheduler.slots`：纯时间基座——UTC 对齐的确定性调度槽与幂等键；
- :mod:`src.scheduler.core`：调度核心——复用 ``job_runs`` 幂等 + 复用 ``run_collectors``
  做单源故障隔离，只写白名单摘要（不含密钥 / 完整 source 配置）。

采集器的注册与构造（provider 相关）**不在本包**，见 :mod:`src.collectors.bootstrap`。
"""

from src.scheduler.core import (
    JOB_TYPE,
    SchedulerRunResult,
    SourceRunSummary,
    aggregate_status,
    collector_name_for,
    default_stale_after,
    enabled_collector_sources,
    redact_secrets,
    run_scheduled_collection_once,
)
from src.scheduler.slots import (
    DEFAULT_INTERVAL_MINUTES,
    ScheduleSlot,
    floor_to_interval,
    resolve_slot,
    seconds_until_next_slot,
)

__all__ = [
    "DEFAULT_INTERVAL_MINUTES",
    "JOB_TYPE",
    "ScheduleSlot",
    "SchedulerRunResult",
    "SourceRunSummary",
    "aggregate_status",
    "collector_name_for",
    "default_stale_after",
    "enabled_collector_sources",
    "floor_to_interval",
    "redact_secrets",
    "resolve_slot",
    "run_scheduled_collection_once",
    "seconds_until_next_slot",
]
