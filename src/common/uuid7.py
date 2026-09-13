"""UUID v7（RFC 9562）主键工具。

对应文档：
- 05_分阶段开发路线图 Phase 0：UUID v7 主键工具。
- 03_数据库完整设计：所有业务表主键统一 UUID。

为什么使用 UUID v7：
1. 48 bit 毫秒时间戳在最前，整体时间有序，B-Tree 索引写入局部性远好于 UUID v4，
   避免随机主键导致的页分裂写放大。
2. 单进程内严格单调递增，便于按主键做稳定的插入顺序与分页游标。
3. 不暴露业务自增信息，适合研究事实表的分布式写入。

⚠️ 单调优先于"时间戳精确回放"（设计取舍，已在 ``test_uuid7.py`` 中测试）：
   当请求的 ``timestamp_ms <= 已颁发的最大时间戳``（历史时间、系统时钟回拨、
   同一毫秒内连续调用）时，生成结果的时间戳会被**钳到已颁发的最大时间戳**，
   只在随机位上递增。因此 ``uuid7_from_datetime(过去时刻)`` 不保证时间戳等于该时刻
   —— 保证的是"主键永不回退"。需要精确回放时间戳的场景请直接用
   ``uuid7_timestamp_ms()`` 读取，或在新进程 / 重置状态后生成。

位布局（128 bit）：
``[48 bit 毫秒时间戳][4 bit 版本=7][12 bit rand_a][2 bit variant=10][62 bit rand_b]``
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime

from src.common.time import to_utc

__all__ = ["uuid7", "uuid7_from_datetime", "uuid7_timestamp_ms"]

_TIMESTAMP_BITS = 48
_RAND_A_BITS = 12
_RAND_B_BITS = 62

_MAX_TIMESTAMP_MS = (1 << _TIMESTAMP_BITS) - 1
_MAX_RAND_A = (1 << _RAND_A_BITS) - 1
_MAX_RAND_B = (1 << _RAND_B_BITS) - 1

_VERSION_7_SHIFT = 76
_VARIANT_SHIFT = 62
_RAND_A_SHIFT = 64
_TIMESTAMP_SHIFT = 80

_lock = threading.Lock()
_state: tuple[int, int, int] = (-1, 0, 0)  # (timestamp_ms, rand_a, rand_b)


def _current_timestamp_ms() -> int:
    """当前毫秒时间戳（裁剪到 UUID v7 可表示范围）。"""
    return max(0, min(int(time.time() * 1000), _MAX_TIMESTAMP_MS))


def _random_rand_a() -> int:
    return int.from_bytes(os.urandom(2), "big") & _MAX_RAND_A


def _random_rand_b() -> int:
    return int.from_bytes(os.urandom(8), "big") & _MAX_RAND_B


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """生成 UUID v7（同一进程内严格单调递增）。

    Args:
        timestamp_ms: 可选的毫秒级 Unix 时间戳，主要用于测试与历史回放；
            为 None 时取系统当前时间。

    Returns:
        ``uuid.UUID``，``version == 7``。

    Raises:
        ValueError: 传入的时间戳超出 48 bit 可表示范围。
        OverflowError: 极端情况下同毫秒随机位耗尽且时间戳溢出。
    """
    global _state

    with _lock:
        last_ts, last_rand_a, last_rand_b = _state
        ts = _current_timestamp_ms() if timestamp_ms is None else int(timestamp_ms)
        if ts < 0 or ts > _MAX_TIMESTAMP_MS:
            raise ValueError(f"timestamp_ms 超出 UUID v7 可表示范围: {ts}")

        if ts > last_ts:
            rand_a = _random_rand_a()
            rand_b = _random_rand_b()
        else:
            # 同一毫秒内的连续调用（或系统时钟回拨）：在随机位上递增，保持单调
            ts = last_ts
            rand_a = last_rand_a
            rand_b = last_rand_b + 1
            if rand_b > _MAX_RAND_B:
                rand_b = 0
                rand_a = last_rand_a + 1
                if rand_a > _MAX_RAND_A:
                    rand_a = 0
                    ts = last_ts + 1
                    if ts > _MAX_TIMESTAMP_MS:
                        raise OverflowError("UUID v7 时间戳位溢出")
        _state = (ts, rand_a, rand_b)

    value = ((ts & _MAX_TIMESTAMP_MS) << _TIMESTAMP_SHIFT) | (0x7 << _VERSION_7_SHIFT)
    value |= (rand_a & _MAX_RAND_A) << _RAND_A_SHIFT
    value |= 0b10 << _VARIANT_SHIFT
    value |= rand_b & _MAX_RAND_B
    return uuid.UUID(int=value)


def uuid7_from_datetime(moment: datetime) -> uuid.UUID:
    """按给定时间生成 UUID v7（时间必须为 timezone-aware）。"""
    utc_moment = to_utc(moment, assume_tz=None, field_name="moment")
    return uuid7(int(utc_moment.timestamp() * 1000))


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """提取 UUID v7 中的毫秒时间戳。

    Raises:
        ValueError: 传入的不是 UUID v7。
    """
    if not isinstance(value, uuid.UUID):
        raise ValueError(f"需要 uuid.UUID，实际为 {type(value).__name__}")
    if value.version != 7:
        raise ValueError(f"需要 UUID v7，实际版本为 {value.version}")
    return value.int >> _TIMESTAMP_SHIFT
