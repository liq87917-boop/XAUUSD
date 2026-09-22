"""确定性 UTC 调度槽（TD-09：30 分钟采集框架的时间基座）。

设计约束（与 06_Cline开发规则 / 08 §12 对齐）：
- 纯计算模块：不访问数据库、不访问网络、不含任何 provider 特例；
- 所有时间必须 timezone-aware（naive 直接抛 :class:`TimeSemanticsError`）；
- 槽边界按 UTC epoch 对齐，同一时刻永远映射到同一槽 → ``idempotency_key`` 稳定，
  "重复触发只执行一次"由 ``job_runs.idempotency_key`` 唯一约束兜底（不新增 schema）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.common.exceptions import TimeSemanticsError
from src.common.time import to_utc

__all__ = [
    "DEFAULT_INTERVAL_MINUTES",
    "ScheduleSlot",
    "floor_to_interval",
    "resolve_slot",
    "seconds_until_next_slot",
]

#: 默认调度间隔（分钟）——TD-09 要求 30 分钟
DEFAULT_INTERVAL_MINUTES = 30

#: 24 小时秒数：用于校验"间隔必须整除一天"，保证槽边界每天稳定对齐、不漂移
_SECONDS_PER_DAY = 86_400


def _validated_interval(interval_minutes: int) -> int:
    """校验调度间隔并返回其值（错误一律 fail-fast，不做隐式纠正）。

    Raises:
        TimeSemanticsError: 非整数 / 越界 / 不能整除 24 小时。
    """
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int):
        raise TimeSemanticsError(
            f"调度间隔必须为整数分钟，实际为 {type(interval_minutes).__name__}"
        )
    if interval_minutes < 1 or interval_minutes > 1440:
        raise TimeSemanticsError(f"调度间隔必须在 1~1440 分钟之间，得到 {interval_minutes}")
    if _SECONDS_PER_DAY % (interval_minutes * 60) != 0:
        raise TimeSemanticsError(
            "调度间隔必须能整除 24 小时（86400 秒），否则 UTC 槽边界会每天漂移："
            f"{interval_minutes} 分钟（可用 15 / 30 / 60 等）"
        )
    return interval_minutes


def floor_to_interval(
    moment: datetime, interval_minutes: int = DEFAULT_INTERVAL_MINUTES
) -> datetime:
    """把时刻向下取整到 UTC 对齐的槽边界（返回 timezone-aware UTC）。

    Args:
        moment: 任意 timezone-aware 时刻（naive 会抛 :class:`TimeSemanticsError`）。
        interval_minutes: 调度间隔（分钟）。

    Returns:
        落在 ``interval_minutes`` 网格上的 UTC 时刻。
    """
    interval = _validated_interval(interval_minutes)
    aware = to_utc(moment, assume_tz=None, field_name="moment")
    seconds = interval * 60
    epoch_seconds = int(aware.timestamp())
    aligned = epoch_seconds - (epoch_seconds % seconds)
    return datetime.fromtimestamp(aligned, tz=UTC)


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    """一个确定性的 UTC 调度槽（区间 ``[start_at, end_at)``）。"""

    interval_minutes: int
    start_at: datetime

    def __post_init__(self) -> None:
        interval = _validated_interval(self.interval_minutes)
        start = to_utc(self.start_at, assume_tz=None, field_name="start_at")
        if floor_to_interval(start, interval) != start:
            raise TimeSemanticsError(
                f"调度槽起点必须落在 UTC 槽边界上：start_at={start.isoformat()} "
                f"不在 {interval} 分钟网格上"
            )
        object.__setattr__(self, "interval_minutes", interval)
        object.__setattr__(self, "start_at", start)

    @property
    def end_at(self) -> datetime:
        """槽结束时刻（**右开**：``[start_at, end_at)``）。"""
        return self.start_at + timedelta(minutes=self.interval_minutes)

    @property
    def idempotency_key(self) -> str:
        """稳定幂等键：同一 UTC 槽永远得到同一字符串。"""
        stamp = self.start_at.strftime("%Y%m%dT%H%M%SZ")
        return f"collector_scheduler:{self.interval_minutes}m:{stamp}"

    def to_dict(self) -> dict[str, object]:
        """序列化摘要（不含任何敏感信息）。"""
        return {
            "interval_minutes": self.interval_minutes,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "idempotency_key": self.idempotency_key,
        }


def resolve_slot(
    moment: datetime, interval_minutes: int = DEFAULT_INTERVAL_MINUTES
) -> ScheduleSlot:
    """把时刻映射到"当前所属"的确定性调度槽。"""
    interval = _validated_interval(interval_minutes)
    return ScheduleSlot(
        interval_minutes=interval, start_at=floor_to_interval(moment, interval)
    )


def seconds_until_next_slot(
    moment: datetime, interval_minutes: int = DEFAULT_INTERVAL_MINUTES
) -> float:
    """距离下一个槽边界还有多少秒（安全循环用；恒 > 0，避免同槽自旋）。"""
    interval = _validated_interval(interval_minutes)
    aware = to_utc(moment, assume_tz=None, field_name="moment")
    next_boundary = floor_to_interval(aware, interval) + timedelta(minutes=interval)
    delay = (next_boundary - aware).total_seconds()
    if delay <= 0:  # pragma: no cover - 防御性分支（floor 语义下不会发生）
        return float(interval * 60)
    return delay
