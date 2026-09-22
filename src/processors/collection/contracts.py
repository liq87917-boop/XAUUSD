"""采集后处理（Collection Processor）的稳定契约 —— TD-11。

职责边界（对齐 `docs/02 §5.2` 的 Processor 链与 `.clinerules`）：
- 本层只做**确定性的数据加工**：
  ``normalize → timezone/effective_at → identity/dedup → validation/audit summary``；
- **不联网**：不导入 HTTP transport / aiohttp，不读 robots，不解析站点，不做 provider 授权，
  不读环境变量里的密钥，不做调度（调度属 ``src/scheduler``）；
- **不臆造时间**：事实时间只来自入参 ``published_at`` / ``collected_at``（以及
  ``effective_at_floor``），绝不使用"当前时间"推断事实；
- **不覆盖原始层**：输出只写 ``processed_items``（append-only）。

为什么把契约与实现分开：
    采集层（`src/collectors`）只需要依赖本模块的 :class:`PersistedItemProcessor` 协议，
    不依赖 :class:`~src.processors.collection.pipeline.CollectionProcessor` 的实现细节，
    这样"接线点"最小、可替换、可 Mock。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol

from database.models.enums import ProcessStatus

__all__ = [
    "AUDIT_PIPELINE_STAGES",
    "DEFAULT_FUTURE_TOLERANCE",
    "DEFAULT_MAX_TEXT_CHARS",
    "MAX_SUMMARY_WARNINGS",
    "PROCESSOR_NAME",
    "PROCESSOR_VERSION",
    "BatchStatus",
    "PersistedItemProcessor",
    "ProcessedRecord",
    "ProcessingReport",
    "ProcessorInput",
    "RawItemLike",
    "RecordOutcome",
]

#: Processor 名称（写入 ``processed_items.processor_name``）
PROCESSOR_NAME: Final[str] = "collection_normalizer"
#: Processor 版本：升级算法必须改版本号（append-only，历史结果永不覆盖）
PROCESSOR_VERSION: Final[str] = "collection-normalizer-v1"

#: 流水线阶段（写入审计摘要，说明本结果由哪几段加工产生）
AUDIT_PIPELINE_STAGES: Final[tuple[str, ...]] = (
    "normalize",
    "timezone/effective_at",
    "identity/dedup",
    "validation/audit",
)

#: 归一化后文本的最大长度（超出截断并留下 warning；防止异常长文本撑爆列）
DEFAULT_MAX_TEXT_CHARS: Final[int] = 200_000
#: 发布时间允许超出"当前时间"的容差（超过即判为坏数据，不静默接受）
DEFAULT_FUTURE_TOLERANCE: Final[timedelta] = timedelta(minutes=5)
#: 审计摘要里保留的最大 warning 条数（其余只计数）
MAX_SUMMARY_WARNINGS: Final[int] = 20


class RecordOutcome(StrEnum):
    """单条记录的加工结论。

    与 ``ProcessStatus``（数据库枚举，禁止擅自扩展）的映射见 :attr:`process_status`：
    ``DUPLICATE`` / ``REJECTED`` 都是"没有产出新事实"，落库为 ``SKIPPED``（留痕但不冒充加工成功）。
    """

    SUCCESS = "SUCCESS"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    @property
    def process_status(self) -> ProcessStatus:
        """映射到 ``processed_items.status``（复用现有枚举，不新增 schema）。"""
        return _PROCESS_STATUS[self]

    @property
    def produced_fact(self) -> bool:
        """该结论是否产出了新的加工事实（只有 SUCCESS 才算）。"""
        return self is RecordOutcome.SUCCESS


#: 结论 → 数据库状态（唯一映射，避免各处自行解释）
_PROCESS_STATUS: Final[dict[RecordOutcome, ProcessStatus]] = {
    RecordOutcome.SUCCESS: ProcessStatus.SUCCESS,
    RecordOutcome.DUPLICATE: ProcessStatus.SKIPPED,
    RecordOutcome.REJECTED: ProcessStatus.SKIPPED,
    RecordOutcome.FAILED: ProcessStatus.FAILED,
}


class BatchStatus(StrEnum):
    """批次状态。

    取值与 ``CollectorRunStatus`` / ``JobStatus`` **同一词汇表**（SUCCESS / PARTIAL_FAILED /
    FAILED），这样 Scheduler 与 Processor 的摘要可以并排比较，且不需要新增数据库枚举。
    """

    SUCCESS = "SUCCESS"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"


class RawItemLike(Protocol):
    """``database.models.RawItem`` 的**结构化视图**（契约层不依赖 ORM 具体类型）。

    属性类型刻意写得宽松（``Any`` / 可空）：契约只要求"这些字段取得到"，
    真正的类型检查由 :func:`CollectionProcessor._coerce_utc` 等运行时守卫完成。
    """

    id: Any
    source_id: Any
    source_record_id: str
    item_type: Any
    title: Any
    content_text: Any
    published_at: Any
    collected_at: Any
    effective_at: Any
    raw_json: Any


class PersistedItemProcessor(Protocol):
    """采集器可注入的后处理钩子（采集层只依赖本协议）。

    约定（`:class:`~src.processors.collection.pipeline.CollectionProcessor`` 已实现）：
    - 只读 ``raw_item``、只写 ``processed_items``（append-only），**绝不修改原始层**；
    - 单条失败必须转成状态记录（``FAILED`` / ``REJECTED``），不得让整批静默丢失；
    - 摘要不得包含 token / API key / Authorization / 完整 source config。
    """

    processor_name: str
    processor_version: str

    def process_persisted(self, session: Any, raw_item: RawItemLike) -> ProcessedRecord: ...


@dataclass(frozen=True, slots=True)
class ProcessorInput:
    """Processor 的输入契约（**只含归属与时间**，不含任何站点 / 传输 / 授权信息）。

    Attributes:
        collected_at: 系统采集到该事实的时间（事实时间的唯一来源之一，必须存在）。
            数据库读回的时间可能为 naive（SQLite 不保存偏移），按项目既定约定
            解释为 UTC（见 :func:`src.processors.timeline.ensure_utc_from_database`）；
            带偏移的时间（如 ``+08:00``）会被正确转换为 UTC。
        source_record_id: 来源内唯一 ID（幂等键的一部分）。
        source_id: 来源 UUID 的字符串形式（可选；参与 identity key，做跨源隔离）。
        raw_item_id: ``raw_items.id``（存在时才能把加工结果写入 ``processed_items``）。
        item_type: 原始类型（POST / NEWS / MACRO / QUOTE）。
        title / content_text: 原始文本（本层只归一化，不改写原始层）。
        published_at: 外部发布时间（未知则为 ``None``，**不允许猜**）。
        effective_at_floor: 上游事实的 ``effective_at``（加工结果不得早于它，防泄漏下界）。
        metadata: 采集器附带的元数据（写入审计摘要前会被裁剪为白名单形态）。
    """

    collected_at: datetime
    source_record_id: str
    source_id: str | None = None
    raw_item_id: uuid.UUID | None = None
    item_type: str | None = None
    title: str | None = None
    content_text: str | None = None
    published_at: datetime | None = None
    effective_at_floor: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw_item(cls, raw_item: RawItemLike) -> ProcessorInput:
        """从已落库的原始记录构造输入（``collected_at`` 取自数据库，不用"现在"）。"""
        raw_id = getattr(raw_item, "id", None)
        published_at = getattr(raw_item, "published_at", None)
        floor = getattr(raw_item, "effective_at", None)
        metadata = getattr(raw_item, "raw_json", None) or {}
        return cls(
            collected_at=raw_item.collected_at,
            source_record_id=str(getattr(raw_item, "source_record_id", "") or ""),
            source_id=_stringify_uuid(getattr(raw_item, "source_id", None)),
            raw_item_id=raw_id if isinstance(raw_id, uuid.UUID) else None,
            item_type=_enum_value(getattr(raw_item, "item_type", None)),
            title=getattr(raw_item, "title", None),
            content_text=getattr(raw_item, "content_text", None),
            published_at=published_at,
            effective_at_floor=floor,
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )


def _stringify_uuid(value: Any) -> str | None:
    """UUID → 字符串（其它类型原样字符串化，空值返回 ``None``）。"""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    text = str(value).strip()
    return text or None


def _enum_value(value: Any) -> str | None:
    """枚举 / 字符串统一成字符串取值（``RawItemType.NEWS`` → ``"NEWS"``）。"""
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class ProcessedRecord:
    """单条记录的加工结果（append-only 事实；字段即审计白名单）。

    ``persistable=False`` 表示事实时间（``collected_at`` / ``published_at``）不可用：
    该结果只作内存留痕与审计计数，**不允许写入** ``processed_items``——
    宁可不落库，也不用"当前时间"伪造事实时间（`.clinerules` 红线）。
    """

    identity_key: str
    outcome: RecordOutcome
    source_record_id: str
    collected_at: datetime
    effective_at: datetime
    content_hash: str
    source_id: str | None = None
    raw_item_id: uuid.UUID | None = None
    item_type: str | None = None
    normalized_title: str | None = None
    normalized_text: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    persistable: bool = True

    @property
    def accepted(self) -> bool:
        """是否产出新的加工事实（``SUCCESS``）。"""
        return self.outcome.produced_fact

    @property
    def process_status(self) -> ProcessStatus:
        """落库状态（复用 ``ProcessStatus``，不新增 schema）。"""
        return self.outcome.process_status

    def to_structured_json(self) -> dict[str, Any]:
        """写入 ``processed_items.structured_json`` 的白名单摘要（**不含凭据**）。"""
        return {
            "processor": PROCESSOR_NAME,
            "processor_version": PROCESSOR_VERSION,
            "outcome": self.outcome.value,
            "stages": list(AUDIT_PIPELINE_STAGES),
            "identity_key": self.identity_key,
            "content_hash": self.content_hash,
            "source_record_id": self.source_record_id,
            "item_type": self.item_type,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProcessingReport:
    """一批记录的加工报告（可审计摘要 + 逐条结果）。"""

    processor_name: str
    processor_version: str
    status: BatchStatus
    started_at: datetime
    finished_at: datetime
    records: tuple[ProcessedRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def _count(self, outcome: RecordOutcome) -> int:
        return sum(1 for record in self.records if record.outcome is outcome)

    @property
    def input_count(self) -> int:
        return len(self.records)

    @property
    def output_count(self) -> int:
        """产出新加工事实的条数（= 成功条数）。"""
        return self._count(RecordOutcome.SUCCESS)

    @property
    def processed_count(self) -> int:
        return self.output_count

    @property
    def duplicate_count(self) -> int:
        return self._count(RecordOutcome.DUPLICATE)

    @property
    def rejected_count(self) -> int:
        return self._count(RecordOutcome.REJECTED)

    @property
    def failed_count(self) -> int:
        return self._count(RecordOutcome.FAILED)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> str:
        """人读单行摘要（日志用；不含凭据）。"""
        return (
            f"{self.processor_name}@{self.processor_version} {self.status.value} | "
            f"input={self.input_count} output={self.output_count} "
            f"duplicate={self.duplicate_count} rejected={self.rejected_count} "
            f"failed={self.failed_count} 耗时={self.duration_seconds:.3f}s"
        )

    def to_summary_dict(self) -> dict[str, Any]:
        """白名单审计摘要（可直接写入 ``job_runs.output_json`` / 日志 / CLI）。"""
        payload: dict[str, Any] = {
            "processor": self.processor_name,
            "processor_version": self.processor_version,
            "status": self.status.value,
            "stages": list(AUDIT_PIPELINE_STAGES),
            "input_count": self.input_count,
            "output_count": self.output_count,
            "duplicate_count": self.duplicate_count,
            "rejected_count": self.rejected_count,
            "failed_count": self.failed_count,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "warnings": list(self.warnings[:MAX_SUMMARY_WARNINGS]),
        }
        if self.error:
            payload["error"] = self.error
        return payload
