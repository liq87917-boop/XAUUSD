"""统一时间语义工具（UTC 唯一标准）。

对应文档：
- 06_Cline开发规则 第 7 条：系统内部统一 UTC，数据库 TIMESTAMPTZ，严禁 naive datetime。
- 02_系统详细设计 5.4 Timeline：published_at / collected_at / effective_at / event_at。
- 01_系统总体架构设计 4.3：feature.effective_at <= prediction_at。

字段语义（Phase 1 唯一解释，禁止在别处重新定义）：
- published_at：外部世界首次公开时间（可能未知，允许 NULL，但必须说明原因）。
- collected_at：系统实际采集到该数据的时间。
- effective_at：系统允许使用该数据的最早时间，必须 >= max(published_at, collected_at)，
  否则就构成未来数据泄漏。
- event_at：事件本身发生时间（宏观数据公布时间、新闻事件时间）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.common.exceptions import TimeSemanticsError

UTC_TZ = UTC


def utc_now() -> datetime:
    """返回当前 UTC 时间（timezone-aware，微秒精度）。"""
    return datetime.now(UTC_TZ)


def require_aware(value: datetime, *, field_name: str = "datetime") -> datetime:
    """断言 datetime 为 timezone-aware，否则抛 :class:`TimeSemanticsError`。"""
    if not isinstance(value, datetime):
        raise TimeSemanticsError(f"{field_name} 必须为 datetime，实际为 {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeSemanticsError(f"{field_name} 为 naive datetime，禁止进入系统（必须带时区）")
    return value


def to_utc(
    value: datetime, *, assume_tz: timezone | None = UTC_TZ, field_name: str = "datetime"
) -> datetime:
    """把 datetime 转换为 UTC。

    Args:
        value: 待转换时间。
        assume_tz: 当 ``value`` 为 naive 时按其解释的时区；默认 UTC。
            只有在解析外部数据且已明确来源时区时才可使用，且必须在调用处显式声明。
        field_name: 错误信息中的字段名。

    Raises:
        TimeSemanticsError: ``value`` 为 naive 且 ``assume_tz`` 为 None。
    """
    if not isinstance(value, datetime):
        raise TimeSemanticsError(f"{field_name} 必须为 datetime，实际为 {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        if assume_tz is None:
            raise TimeSemanticsError(f"{field_name} 为 naive datetime 且未声明时区，拒绝隐式转换")
        value = value.replace(tzinfo=assume_tz)
    return value.astimezone(UTC_TZ)


def parse_iso8601(value: str, *, field_name: str = "published_at") -> datetime:
    """解析 ISO8601 字符串为 UTC datetime。

    - 支持 ``Z`` 后缀与 ``+08:00`` 偏移。
    - naive 字符串按 UTC 解释（外部接口未带时区时的唯一约定，并在
      :func:`resolve_effective_at` 中以 collected_at 兜底，避免泄漏）。
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeSemanticsError(f"{field_name} 不能为空字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - 由测试覆盖异常分支
        raise TimeSemanticsError(f"{field_name} 无法解析为 ISO8601：{value!r}") from exc
    return to_utc(parsed, assume_tz=UTC_TZ, field_name=field_name)


def resolve_timezone(name: str) -> ZoneInfo:
    """把 IANA 时区名解析为 :class:`zoneinfo.ZoneInfo`。

    为什么需要这个函数而不是直接 ``ZoneInfo(name)``：
    - 数据源 / 标的的时区名来自配置与数据库字符串，写错时必须在**入口处**报错，
      而不是在后续时间对齐时留下隐性偏差（02 §5.4 时间语义）。
    - Windows 没有系统 tz 数据库，``zoneinfo`` 依赖 ``tzdata`` 包（已在
      pyproject.toml 声明），保证本地开发与 CI 行为一致。

    Raises:
        TimeSemanticsError: 时区名为空或不是合法的 IANA 名称。
    """
    if not isinstance(name, str) or not name.strip():
        raise TimeSemanticsError("时区名不能为空")
    candidate = name.strip()
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeSemanticsError(
            f"未知时区名：{name!r}；必须为 IANA 名称（如 UTC / Asia/Shanghai / America/New_York）"
        ) from exc


def resolve_effective_at(
    *,
    published_at: datetime | None,
    collected_at: datetime,
    field_name: str = "effective_at",
) -> datetime:
    """计算 ``effective_at`` = 系统最早可用时间。

    规则（防泄漏核心）：
    - 数据只有"采集到时"才真正可用，故 ``effective_at >= collected_at`` 恒成立。
    - 若外部发布时间晚于采集时间（异常/跨时区错误），取较晚者，宁可保守。
    - ``published_at`` 为空时以 ``collected_at`` 为准（禁止用"现在"兜底，
      否则重放历史批次会漂移）。

    Args:
        published_at: 外部发布时间，可为 None。
        collected_at: 采集时间（必须 timezone-aware）。
        field_name: 错误信息字段名。

    Returns:
        timezone-aware UTC datetime。
    """
    collected_utc = to_utc(collected_at, assume_tz=None, field_name="collected_at")
    if published_at is None:
        return collected_utc

    published_utc = to_utc(published_at, assume_tz=None, field_name="published_at")
    effective = max(published_utc, collected_utc)
    if effective.tzinfo is None:  # pragma: no cover - 防御性分支
        raise TimeSemanticsError(f"{field_name} 计算结果为 naive datetime")
    return effective


def is_future(
    value: datetime,
    *,
    now: datetime | None = None,
    tolerance: timedelta = timedelta(minutes=5),
) -> bool:
    """判断时间是否落在未来（超出容差）。

    用于数据质量检查：采集时间 / 发布时间不允许显著超过当前时间。
    """
    reference = to_utc(now, assume_tz=None, field_name="now") if now else utc_now()
    return to_utc(value, assume_tz=None, field_name="value") > reference + tolerance


def to_naive_utc(value: datetime) -> datetime:
    """转换为 naive UTC。

    仅用于与不支持时区的第三方库（如部分 SQLite 驱动 / pandas 旧接口）交互，
    严禁用于数据库写入或研究事实表。
    """
    return to_utc(value, assume_tz=None, field_name="value").replace(tzinfo=None)
