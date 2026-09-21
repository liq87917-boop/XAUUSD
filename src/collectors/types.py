"""采集器数据契约（与 04_数据表结构及字段定义 对齐）。

本模块只描述"数据长什么样"，不含任何站点逻辑与数据库访问：
- :class:`RawItemPayload` ↔ 04 §5 ``raw_items``（id / created_at / effective_at 由框架补齐）
- :class:`MediaPayload` ↔ 04 §6 ``raw_media``
- :class:`FetchPage` 是 ``_do_fetch()`` 的返回值（一页数据 + 下一页游标）
- :class:`CollectOutcome` ↔ 04 §7 ``collector_runs`` 的统计字段

所有时间字段强制 timezone-aware（06_Cline开发规则 第 7 条）：传入 naive datetime
会立刻抛 :class:`TimeSemanticsError`，不允许"到落库时才发现"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from database.models.enums import CollectorRunStatus, MediaType, RawItemType
from src.common.exceptions import TimeSemanticsError
from src.common.hashing import content_hash
from src.common.time import resolve_effective_at, to_utc, utc_now

__all__ = [
    "CollectOutcome",
    "CollectWindow",
    "CollectorHealth",
    "FetchPage",
    "MediaPayload",
    "RawItemPayload",
    "new_cursor",
]


@dataclass(frozen=True, slots=True)
class MediaPayload:
    """待入库的媒体记录（04 §6 raw_media）。"""

    media_type: MediaType
    original_url: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    storage_uri: str | None = None


@dataclass(frozen=True, slots=True)
class RawItemPayload:
    """待入库的原始记录（04 §5 raw_items）。

    ``source_record_id`` 是幂等键的一部分，必须由采集器提供（不允许框架生成随机值，
    否则重复采集会产生重复记录 —— Phase 1 验收门槛"无大规模重复"）。
    """

    source_record_id: str
    item_type: RawItemType
    title: str | None = None
    content_text: str | None = None
    raw_json: Mapping[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    published_at: datetime | None = None
    media: tuple[MediaPayload, ...] = ()
    #: 可选：采集器自定义哈希（例如需要把媒体 URL 一并纳入）；默认由框架计算
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_record_id, str) or not self.source_record_id.strip():
            raise ValueError("source_record_id 不能为空：它是 raw_items 的幂等键（04 §5）")
        if self.published_at is not None:
            to_utc(self.published_at, assume_tz=None, field_name="published_at")
        if self.content_hash is not None and (
            len(self.content_hash) != 64 or self.content_hash != self.content_hash.lower()
        ):
            raise ValueError("content_hash 必须是 64 位小写十六进制字符串")

    def resolved_content_hash(self) -> str:
        """返回最终用于去重的 SHA256（缺省按标题 + 正文计算）。"""
        if self.content_hash:
            return self.content_hash
        return content_hash(self.title, self.content_text)

    def resolve_effective_at(self, collected_at: datetime) -> datetime:
        """按 02 §5.4 计算 effective_at = max(published_at, collected_at)。"""
        return resolve_effective_at(
            published_at=self.published_at,
            collected_at=collected_at,
            field_name="effective_at",
        )


@dataclass(frozen=True, slots=True)
class CollectWindow:
    """采集时间窗口（必须 timezone-aware；end 不得早于 start）。"""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = to_utc(self.start_at, assume_tz=None, field_name="start_at")
        end = to_utc(self.end_at, assume_tz=None, field_name="end_at")
        if end < start:
            raise TimeSemanticsError(
                f"采集窗口非法：end_at({end.isoformat()}) 早于 start_at({start.isoformat()})"
            )

    @property
    def start_utc(self) -> datetime:
        return to_utc(self.start_at, assume_tz=None, field_name="start_at")

    @property
    def end_utc(self) -> datetime:
        return to_utc(self.end_at, assume_tz=None, field_name="end_at")


@dataclass(frozen=True, slots=True)
class FetchPage:
    """``_do_fetch()`` 的返回：一页数据 + 下一页游标。

    ``next_cursor=None`` 表示已到达窗口末尾（本轮采集完成）。
    ``raw_count`` 用于统计"来源返回的条数"（默认等于 payloads 数量），
    采集器内部过滤数据时可用它保留真实 fetched 口径。
    """

    payloads: tuple[RawItemPayload, ...] = ()
    next_cursor: dict[str, Any] | None = None
    raw_count: int | None = None

    @property
    def fetched(self) -> int:
        return len(self.payloads) if self.raw_count is None else self.raw_count


@dataclass(frozen=True, slots=True)
class CollectorHealth:
    """采集器健康检查结果（06_Cline开发规则 第 12 条：必须可 health check）。"""

    collector_name: str
    healthy: bool
    checked_at: datetime
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectOutcome:
    """一轮采集结果，字段与 04 §7 ``collector_runs`` 统计列一一对应。"""

    collector_name: str
    source_id: uuid.UUID | None
    status: CollectorRunStatus
    started_at: datetime
    finished_at: datetime
    fetched_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0
    failed_count: int = 0
    pages_fetched: int = 0
    retry_count: int = 0
    cursor: dict[str, Any] | None = None
    resumed_from_cursor: bool = False
    error_message: str | None = None
    #: 跳过的记录数（数据质量校验拒绝，未入库；区别于 failed——failed 是入库异常）
    skipped_count: int = 0
    #: 实际使用的传输通道（library / rest / cache），供统一结果对象与监控使用
    transport: str | None = None
    #: 错误类型（异常类名，如 TransportError / CollectorError；成功为 None）
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.finished_at < self.started_at:
            raise TimeSemanticsError("finished_at 不得早于 started_at（collector_runs 约束）")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> str:
        """人读摘要（日志 / CLI 输出）。"""
        return (
            f"{self.collector_name}: {self.status.value} | "
            f"fetched={self.fetched_count} inserted={self.inserted_count} "
            f"duplicate={self.duplicate_count} failed={self.failed_count} "
            f"pages={self.pages_fetched} retries={self.retry_count} "
            f"耗时={self.duration_seconds:.2f}s"
        )


def new_cursor(page_token: str | None = None, **extra: Any) -> dict[str, Any]:
    """构造带上更新时间的游标（供子类复用，保证游标结构一致、可断点续传）。"""
    cursor: dict[str, Any] = {"updated_at": utc_now().isoformat()}
    if page_token is not None:
        cursor["page_token"] = page_token
    cursor.update(extra)
    return cursor

