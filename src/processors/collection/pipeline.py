"""采集后处理流水线（TD-11：把 normalize / dedup / timezone 从采集器抽离出来）。

流水线（确定性，同一输入必得同一输出）：

```text
ProcessorInput
  → ① normalize              （Unicode NFKC / 去零宽 / 折叠空白；空文本判坏数据）
  → ② timezone/effective_at  （统一 UTC；effective_at = max(published_at, collected_at, 上游下界)）
  → ③ identity/dedup         （内容指纹 + 幂等键；批次内 / 跨批重复判 DUPLICATE）
  → ④ validation/audit       （数据质量校验；元数据脱敏为白名单审计摘要）
  → ProcessedRecord（append-only）→ processed_items
```

红线（逐条对齐 `.clinerules` 与 `.ai/DEVELOPMENT_PROTOCOL.md`）：

1. **不联网**：本模块不导入 HTTP transport / aiohttp / registry，不读 robots、不做 provider
   授权、不解析站点、不调度（调度在 ``src/scheduler``）；
2. **不伪造时间**：事实时间只来自 ``published_at`` / ``collected_at`` / 上游 ``effective_at``；
   三者都不可用时判 ``REJECTED`` 且**不落库**（``persistable=False``），绝不用"现在"冒充；
3. **append-only**：只写 ``processed_items``（``(raw_item_id, processor_name, processor_version)``
   唯一约束 + 落库前存在性检查 → 重复运行不产生重复结果）；绝不 UPDATE 原始层；
4. **单条失败隔离**：一条坏数据只影响自己（``REJECTED`` / ``FAILED``），
   批次状态诚实区分 ``SUCCESS`` / ``PARTIAL_FAILED`` / ``FAILED``；
5. **不泄露凭据**：审计摘要经 :func:`src.common.redaction.sanitize_mapping` 裁剪，
   丢弃凭据键与嵌套结构，URL 去掉 query/userinfo。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import ProcessedItem
from src.common.exceptions import TimeSemanticsError
from src.common.redaction import safe_text, sanitize_mapping
from src.common.time import UTC_TZ, resolve_effective_at, to_utc, utc_now
from src.processors.collection.contracts import (
    DEFAULT_FUTURE_TOLERANCE,
    DEFAULT_MAX_TEXT_CHARS,
    MAX_SUMMARY_WARNINGS,
    PROCESSOR_NAME,
    PROCESSOR_VERSION,
    BatchStatus,
    ProcessedRecord,
    ProcessingReport,
    ProcessorInput,
    RawItemLike,
    RecordOutcome,
)
from src.processors.collection.identity import DedupIndex, content_fingerprint, identity_key
from src.processors.collection.normalize import normalize_text, truncate_text
from src.processors.collection.validate import format_issues, validate_normalized_record

__all__ = [
    "REASON_DUPLICATE",
    "REASON_EMPTY_CONTENT",
    "REASON_INVALID_TIMESTAMP",
    "REASON_MISSING_SOURCE_RECORD_ID",
    "REASON_PUBLISHED_AT_IN_FUTURE",
    "CollectionProcessor",
    "batch_status_for",
]

_log = get_logger("processors.collection.pipeline")

ClockFn = Callable[[], datetime]

#: 拒绝 / 失败原因（机器可读；写进 ``processed_items.structured_json.reason``）
REASON_MISSING_SOURCE_RECORD_ID = "missing_source_record_id"
REASON_INVALID_TIMESTAMP = "invalid_timestamp"
REASON_EMPTY_CONTENT = "empty_content"
REASON_PUBLISHED_AT_IN_FUTURE = "published_at_in_future"
REASON_DUPLICATE = "duplicate_identity"

#: 标题最大长度（超长标题同样截断并留 warning）
MAX_TITLE_CHARS = 2_000


def batch_status_for(
    *,
    input_count: int,
    output_count: int,
    unusable_count: int,
    error: str | None = None,
) -> BatchStatus:
    """按"诚实统计"推导批次状态。

    - 批次级异常（``error`` 非空）→ ``FAILED``；
    - 空批次 / 全部可用（含"全是重复"，即幂等重放）→ ``SUCCESS``；
    - 全部不可用且没有有效产出 → ``FAILED``；其余部分失败 → ``PARTIAL_FAILED``。
    """
    if error is not None:
        return BatchStatus.FAILED
    if input_count <= 0 or unusable_count <= 0:
        return BatchStatus.SUCCESS
    if output_count <= 0 and unusable_count >= input_count:
        return BatchStatus.FAILED
    return BatchStatus.PARTIAL_FAILED


class CollectionProcessor:
    """采集后处理流水线的默认实现（本类实现 :class:`PersistedItemProcessor` 协议）。

    Args:
        clock: 时间源。**只用于报告时间戳与"未来时间"容差判定的参照**，
            绝不用于推断事实时间（测试可注入固定时钟，保证断言稳定）。
        max_text_chars: 归一化文本的最大长度（超出截断并留 warning）。
        future_tolerance: ``published_at`` 允许超出参照时间的容差。
        known_identity_keys: 已知（例如上一次运行已落库）的 identity key，
            用于**跨批去重**：重复输入直接判 ``DUPLICATE``，不产生重复加工结果。
        persist: 是否允许写 ``processed_items``（默认允许；只读复算时可关闭）。
    """

    def __init__(
        self,
        *,
        clock: ClockFn = utc_now,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        future_tolerance: timedelta = DEFAULT_FUTURE_TOLERANCE,
        known_identity_keys: Iterable[str] = (),
        persist: bool = True,
    ) -> None:
        if max_text_chars <= 0:
            raise ValueError("max_text_chars 必须为正整数")
        self._clock = clock
        self.max_text_chars = max_text_chars
        self.future_tolerance = future_tolerance
        self._index = DedupIndex(known_identity_keys)
        self._persist_enabled = persist

    # ------------------------------------------------------------------
    # 协议属性 / 只读状态（供采集层与测试观察）
    # ------------------------------------------------------------------
    @property
    def processor_name(self) -> str:
        return PROCESSOR_NAME

    @property
    def processor_version(self) -> str:
        return PROCESSOR_VERSION

    @property
    def identity_index(self) -> DedupIndex:
        """已登记的 identity key 索引（只读观察用）。"""
        return self._index

    @property
    def persist_enabled(self) -> bool:
        return self._persist_enabled

    # ------------------------------------------------------------------
    # 主入口（批次）
    # ------------------------------------------------------------------
    def process(self, records: Sequence[ProcessorInput]) -> ProcessingReport:
        """加工一批输入，返回可审计报告（**不写数据库**）。"""
        started_at = self._reference()
        results = tuple(self.process_one(record, now=started_at) for record in records)
        finished_at = self._reference()

        unusable = sum(
            1
            for record in results
            if not record.accepted and record.outcome is not RecordOutcome.DUPLICATE
        )
        warnings: list[str] = []
        for record in results:
            for warning in record.warnings:
                if warning not in warnings:
                    warnings.append(warning)
                if len(warnings) >= MAX_SUMMARY_WARNINGS:
                    break
            if len(warnings) >= MAX_SUMMARY_WARNINGS:
                break

        report = ProcessingReport(
            processor_name=self.processor_name,
            processor_version=self.processor_version,
            status=batch_status_for(
                input_count=len(results),
                output_count=sum(1 for record in results if record.accepted),
                unusable_count=unusable,
            ),
            started_at=started_at,
            finished_at=finished_at,
            records=results,
            warnings=tuple(warnings),
        )
        _log.info("采集后处理完成 | %s", report.summary())
        for warning in report.warnings:
            _log.warning("%s | %s", self.processor_name, warning)
        return report

    def process_and_persist(
        self, session: Session, records: Sequence[ProcessorInput]
    ) -> tuple[ProcessingReport, int]:
        """加工 + 落库（``processed_items``），返回 ``(报告, 新增行数)``。"""
        report = self.process(records)
        written = self.persist_records(session, report.records)
        return report, written

    # ------------------------------------------------------------------
    # 单条加工（**绝不抛异常**：单条失败隔离）
    # ------------------------------------------------------------------
    def process_one(
        self, record: ProcessorInput, *, now: datetime | None = None
    ) -> ProcessedRecord:
        """加工单条记录；任何异常都转成 ``FAILED`` 记录，不打断整批。"""
        reference = self._reference(now)
        try:
            return self._process_record(record, reference=reference)
        except TimeSemanticsError as exc:
            return self._non_success_record(
                record,
                reference=reference,
                outcome=RecordOutcome.REJECTED,
                reason=f"{REASON_INVALID_TIMESTAMP}: {safe_text(str(exc))}",
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败隔离：绝不静默丢失整批
            detail = safe_text(f"{type(exc).__name__}: {exc}")
            warning = f"记录 {safe_text(str(record.source_record_id))} 加工异常：{detail}"
            _log.warning("%s | %s", self.processor_name, warning)
            return self._non_success_record(
                record,
                reference=reference,
                outcome=RecordOutcome.FAILED,
                reason=f"unexpected_error: {detail}",
                warnings=(warning,),
            )

    def _process_record(self, record: ProcessorInput, *, reference: datetime) -> ProcessedRecord:
        """四个阶段的确定性流水线（顺序与模块 docstring 一致）。"""
        # ---- ① normalize -------------------------------------------------
        normalized_title, title_truncated = self._normalize_field(
            record.title, max_chars=MAX_TITLE_CHARS
        )
        normalized_text, text_truncated = self._normalize_field(
            record.content_text, max_chars=self.max_text_chars
        )
        warnings: list[str] = []
        if title_truncated:
            warnings.append(f"标题超长已截断（>{MAX_TITLE_CHARS} 字符）")
        if text_truncated:
            warnings.append(f"正文超长已截断（>{self.max_text_chars} 字符）")
        if not (normalized_title or normalized_text):
            return self._non_success_record(
                record,
                reference=reference,
                outcome=RecordOutcome.REJECTED,
                reason=REASON_EMPTY_CONTENT,
                warnings=tuple(warnings),
            )

        # ---- ② timezone / effective_at -----------------------------------
        collected_at = self._coerce_utc(record.collected_at, field_name="collected_at")
        published_at = (
            None
            if record.published_at is None
            else self._coerce_utc(record.published_at, field_name="published_at")
        )
        effective_at = resolve_effective_at(published_at=published_at, collected_at=collected_at)
        if record.effective_at_floor is not None:
            floor = self._coerce_utc(record.effective_at_floor, field_name="effective_at_floor")
            if floor > effective_at:
                effective_at = floor
                warnings.append("effective_at 取上游事实下界（防未来数据泄漏）")

        # ---- ③ identity / dedup -----------------------------------------
        if not str(record.source_record_id or "").strip():
            return self._non_success_record(
                record,
                reference=reference,
                outcome=RecordOutcome.REJECTED,
                reason=REASON_MISSING_SOURCE_RECORD_ID,
                warnings=tuple(warnings),
            )
        content_hash = content_fingerprint(title=normalized_title, content_text=normalized_text)
        key = identity_key(
            source_id=record.source_id,
            source_record_id=record.source_record_id,
            content_hash=content_hash,
        )
        is_duplicate = not self._index.add(key)

        # ---- ④ validation / audit ---------------------------------------
        issues = validate_normalized_record(
            source_record_id=record.source_record_id,
            normalized_title=normalized_title,
            normalized_text=normalized_text,
            collected_at=collected_at,
            published_at=published_at,
            reference=reference,
            future_tolerance=self.future_tolerance,
        )
        if issues:
            reason = format_issues(issues)
            if any(issue.field == "published_at" for issue in issues):
                reason = REASON_PUBLISHED_AT_IN_FUTURE
            return self._non_success_record(
                record,
                reference=reference,
                outcome=RecordOutcome.REJECTED,
                reason=reason,
                warnings=tuple(warnings),
            )

        metadata = sanitize_mapping(record.metadata)
        language = metadata.get("language")
        return ProcessedRecord(
            identity_key=key,
            outcome=RecordOutcome.DUPLICATE if is_duplicate else RecordOutcome.SUCCESS,
            source_record_id=str(record.source_record_id).strip(),
            source_id=record.source_id,
            raw_item_id=record.raw_item_id,
            item_type=record.item_type,
            normalized_title=normalized_title,
            normalized_text=normalized_text,
            language=language if isinstance(language, str) else None,
            published_at=published_at,
            collected_at=collected_at,
            effective_at=effective_at,
            content_hash=content_hash,
            reason=REASON_DUPLICATE if is_duplicate else None,
            warnings=tuple(warnings),
            metadata=metadata,
        )

    def _non_success_record(
        self,
        record: ProcessorInput,
        *,
        reference: datetime,
        outcome: RecordOutcome,
        reason: str,
        warnings: tuple[str, ...] = (),
    ) -> ProcessedRecord:
        """构造 ``REJECTED`` / ``FAILED`` 记录（时间不可用时**不允许落库**）。"""
        collected_at, published_at, effective_at, persistable = self._best_effort_times(
            record, reference=reference
        )
        if not persistable:
            warnings = (
                *warnings,
                "时间字段不可用：结果仅作内存留痕，不写 processed_items（禁止伪造时间）",
            )
        content_hash = content_fingerprint(
            title=normalize_text(record.title), content_text=normalize_text(record.content_text)
        )
        source_record_id = str(record.source_record_id or "").strip()
        try:
            key = identity_key(
                source_id=record.source_id,
                source_record_id=source_record_id or "unknown",
                content_hash=content_hash,
            )
        except ValueError:  # pragma: no cover - 仅当 identity key 无法构造时
            key = ""
        return ProcessedRecord(
            identity_key=key,
            outcome=outcome,
            source_record_id=source_record_id,
            source_id=record.source_id,
            raw_item_id=record.raw_item_id,
            item_type=record.item_type,
            normalized_title=normalize_text(record.title),
            normalized_text=normalize_text(record.content_text),
            published_at=published_at,
            collected_at=collected_at,
            effective_at=effective_at,
            content_hash=content_hash,
            reason=safe_text(reason, max_chars=500),
            warnings=warnings,
            metadata={},
            persistable=persistable,
        )

    # ------------------------------------------------------------------
    # 时间与文本工具（纯函数式，便于单测）
    # ------------------------------------------------------------------
    def _reference(self, now: datetime | None = None) -> datetime:
        """参照时间（报告时间戳 / 未来容差比较）；**不参与事实时间计算**。"""
        if now is not None:
            return to_utc(now, assume_tz=UTC_TZ, field_name="reference")
        return to_utc(self._clock(), assume_tz=UTC_TZ, field_name="reference")

    @staticmethod
    def _coerce_utc(value: object, *, field_name: str) -> datetime:
        """把时间统一为 UTC-aware（naive 按 UTC 解释，非 datetime 直接报错）。

        naive 值按 UTC 解释是项目既定约定：所有时间列都以 UTC 写入；
        SQLite（本地/测试库）不保存偏移，PostgreSQL ``TIMESTAMPTZ`` 读回即 aware
        （见 :func:`src.processors.timeline.ensure_utc_from_database`）。
        """
        if not isinstance(value, datetime):
            raise TimeSemanticsError(
                f"{field_name} 必须为 datetime，实际为 {type(value).__name__}"
            )
        return to_utc(value, assume_tz=UTC_TZ, field_name=field_name)

    @staticmethod
    def _try_coerce(value: object) -> datetime | None:
        """尽力转换（失败返回 ``None``）；用于坏数据留痕，不抛异常。"""
        try:
            return CollectionProcessor._coerce_utc(value, field_name="timestamp")
        except TimeSemanticsError:
            return None

    def _best_effort_times(
        self, record: ProcessorInput, *, reference: datetime
    ) -> tuple[datetime, datetime | None, datetime, bool]:
        """坏数据路径的时间兜底：返回 ``(collected_at, published_at, effective_at, persistable)``。

        ``persistable=False`` 表示时间字段不可信 → 该结果**不得**写入 ``processed_items``
        （宁可不落库，也不用"现在"伪造事实时间）。
        """
        collected_at = self._try_coerce(record.collected_at)
        published_at = (
            None if record.published_at is None else self._try_coerce(record.published_at)
        )
        persistable = collected_at is not None
        if collected_at is None:
            collected_at = reference
            published_at = None
        effective_at = resolve_effective_at(published_at=published_at, collected_at=collected_at)
        floor = (
            None
            if record.effective_at_floor is None
            else self._try_coerce(record.effective_at_floor)
        )
        if floor is not None and floor > effective_at:
            effective_at = floor
        return collected_at, published_at, effective_at, persistable

    def _normalize_field(self, value: str | None, *, max_chars: int) -> tuple[str | None, bool]:
        """归一化 + 截断单个文本字段；返回 ``(文本, 是否被截断)``。"""
        normalized = normalize_text(value)
        if normalized is None:
            return None, False
        return truncate_text(normalized, max_chars=max_chars)

    # ------------------------------------------------------------------
    # 落库（append-only + 幂等）
    # ------------------------------------------------------------------
    def persist_records(
        self, session: Session, records: Iterable[ProcessedRecord]
    ) -> int:
        """把加工结果写入 ``processed_items``；返回**新增行数**（重复运行恒为 0）。"""
        if not self._persist_enabled:
            return 0

        written = 0
        seen_in_batch: set[uuid.UUID] = set()
        for record in records:
            if record.raw_item_id is None or not record.persistable:
                continue  # 未落库的原始记录 / 时间不可信：不写加工表
            if record.outcome is RecordOutcome.DUPLICATE:
                continue  # 同一原始事实已有加工结果：不重复写入
            if record.raw_item_id in seen_in_batch:
                continue
            if self._already_processed(session, record.raw_item_id):
                continue

            item = ProcessedItem(
                raw_item_id=record.raw_item_id,
                processor_name=self.processor_name,
                processor_version=self.processor_version,
                normalized_text=record.normalized_text,
                language=record.language,
                structured_json=record.to_structured_json(),
                status=record.process_status,
                effective_at=record.effective_at,
            )
            try:
                with session.begin_nested():
                    session.add(item)
            except IntegrityError:  # pragma: no cover - 并发下的唯一约束兜底
                _log.info(
                    "processed_items 已存在（并发写入）：raw_item_id=%s version=%s",
                    record.raw_item_id,
                    self.processor_version,
                )
                continue
            seen_in_batch.add(record.raw_item_id)
            written += 1

        if written:
            session.flush()
        return written

    def _already_processed(self, session: Session, raw_item_id: uuid.UUID) -> bool:
        """``(raw_item_id, processor_name, processor_version)`` 是否已有加工结果。"""
        exists = session.scalar(
            sa.select(ProcessedItem.id)
            .where(ProcessedItem.raw_item_id == raw_item_id)
            .where(ProcessedItem.processor_name == self.processor_name)
            .where(ProcessedItem.processor_version == self.processor_version)
            .limit(1)
        )
        return exists is not None

    # ------------------------------------------------------------------
    # 采集器接线点（实现 PersistedItemProcessor 协议）
    # ------------------------------------------------------------------
    def process_persisted(self, session: Session, raw_item: RawItemLike) -> ProcessedRecord:
        """采集器钩子：对刚落库的一条原始记录执行加工并幂等落库。

        只依赖 ``raw_item`` 的字段（结构协议 :class:`RawItemLike`），
        因此采集层无需了解流水线内部实现。
        """
        record = self.process_one(ProcessorInput.from_raw_item(raw_item))
        self.persist_records(session, (record,))
        return record
