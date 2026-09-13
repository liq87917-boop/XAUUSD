"""UUID v7 主键工具测试（05_分阶段开发路线图 Phase 0 交付项）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module

import pytest

from src.common.exceptions import TimeSemanticsError
from src.common.uuid7 import uuid7, uuid7_from_datetime, uuid7_timestamp_ms

# 必须用 importlib 取真正的模块对象：``src.common.__init__`` 用 ``from ... import uuid7``
# 重新导出了同名**函数**，因此 ``import src.common.uuid7 as m`` 拿到的是函数而不是模块
# （monkeypatch 需要模块对象才能重置进程内的单调状态）。
uuid7_module = import_module("src.common.uuid7")

pytestmark = pytest.mark.unit

FIXED_MS = 1_726_000_000_000


def test_version_and_variant_bits() -> None:
    """版本位必须为 7，variant 必须为 RFC 4122（10xx）。"""
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_monotonic_within_same_millisecond() -> None:
    """同一毫秒内的连续调用必须严格递增且不重复（主键顺序即插入顺序）。"""
    values = [uuid7(FIXED_MS) for _ in range(500)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_monotonic_without_explicit_timestamp() -> None:
    """高并发下默认调用同样保持严格递增。"""
    values = [uuid7() for _ in range(2000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_timestamp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归测试：roundtrip 断言必须与运行时刻无关。

    背景（真实缺陷，2026-09-12 触发）：``uuid7()`` 为严格单调，当请求时间戳
    ``<= 已颁发的最大时间戳`` 时会把时间戳**钳到该最大值**（``src/common/uuid7.py``）。
    本测试原先使用硬编码时刻 ``2026-09-12 08:30:15.123 UTC``，而同文件前一个测试
    已按"当前时间"颁发过 2000 个 UUID —— 于是该断言实际上依赖"本套件在
    2026-09-12 08:30:15 UTC 之前运行"：早于该毫秒通过，晚于该毫秒必然失败（时间炸弹）。
    修复方式：显式重置进程内的单调状态，让 roundtrip 的语义可判定、与墙上时钟解耦。
    """
    monkeypatch.setattr(uuid7_module, "_state", (-1, 0, 0))

    moment = datetime(2026, 9, 12, 8, 30, 15, 123000, tzinfo=UTC)
    value = uuid7_from_datetime(moment)
    assert uuid7_timestamp_ms(value) == int(moment.timestamp() * 1000)


def test_past_timestamp_is_clamped_to_keep_keys_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单调优先是**有意行为**：颁发过更晚时间戳后，历史时间请求被钳位且主键仍递增。

    这条测试锁住语义，避免有人为了"让 roundtrip 通过"而删掉钳位逻辑，
    从而破坏主键单调性（研究系统的分页游标与插入顺序依赖它）。
    """
    monkeypatch.setattr(uuid7_module, "_state", (-1, 0, 0))

    later = uuid7(FIXED_MS + 10_000)
    historical = uuid7_from_datetime(datetime(2020, 1, 1, tzinfo=UTC))

    assert uuid7_timestamp_ms(historical) == uuid7_timestamp_ms(later)  # 被钳位
    assert str(later) < str(historical)  # 仍然严格递增（随机位继续递增）


def test_uuid7_from_datetime_rejects_naive_datetime() -> None:
    with pytest.raises(TimeSemanticsError):
        uuid7_from_datetime(datetime(2026, 1, 1, 0, 0, 0))


def test_uuid7_from_datetime_converts_timezone() -> None:
    from datetime import timedelta, timezone

    shanghai = datetime(2026, 9, 12, 16, 30, tzinfo=timezone(timedelta(hours=8)))
    assert uuid7_timestamp_ms(uuid7_from_datetime(shanghai)) == uuid7_timestamp_ms(
        uuid7_from_datetime(datetime(2026, 9, 12, 8, 30, tzinfo=UTC))
    )


def test_timestamp_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        uuid7(-1)
    with pytest.raises(ValueError):
        uuid7(1 << 48)


def test_uuid7_timestamp_ms_rejects_non_v7() -> None:
    with pytest.raises(ValueError):
        uuid7_timestamp_ms(uuid.uuid4())
    with pytest.raises(ValueError):
        uuid7_timestamp_ms("not-a-uuid")  # type: ignore[arg-type]


def test_string_order_follows_time_order() -> None:
    """UUID v7 的字符串排序与时间排序一致（便于游标分页与索引局部性）。"""
    earlier = uuid7(FIXED_MS)
    later = uuid7(FIXED_MS + 1)
    assert str(earlier) < str(later)
