"""采集后处理落库集成测试（``processed_items``：append-only + 幂等 + 时间因果）。

★ 全程不联网、不访问真实数据源 ★：只使用 SQLite 测试库与本地构造的原始记录。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import ProcessedItem, RawItem
from database.models.enums import ProcessStatus
from src.processors.collection.contracts import (
    PROCESSOR_NAME,
    PROCESSOR_VERSION,
    ProcessorInput,
    RecordOutcome,
)
from src.processors.collection.pipeline import CollectionProcessor

pytestmark = pytest.mark.integration

FIXED_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
COLLECTED_AT = datetime(2026, 9, 22, 11, 0, tzinfo=UTC)
SECRET_VALUE = "SECRETVALUE-4d2f"


def _clock() -> datetime:
    return FIXED_NOW


def _processor(**overrides: object) -> CollectionProcessor:
    overrides.setdefault("clock", _clock)
    return CollectionProcessor(**overrides)  # type: ignore[arg-type]


def _rows(session: Session) -> list[ProcessedItem]:
    return list(session.scalars(sa.select(ProcessedItem)).all())


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 不保存时区偏移（读回 naive）：统一补齐 UTC 后再比较（仅测试辅助）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _raw(make_raw_item, **overrides: object) -> RawItem:
    payload: dict[str, object] = {
        "collected_at": COLLECTED_AT,
        "published_at": COLLECTED_AT - timedelta(minutes=5),
        "content_text": "3400 上方减仓后继续持有。",
        "title": "黄金 3400 上方继续看多",
        "raw_json": {"language": "zh"},
    }
    payload.update(overrides)
    return make_raw_item(**payload)


# ---------------------------------------------------------------------------
# 正常路径与字段落库
# ---------------------------------------------------------------------------
def test_process_and_persist_writes_processed_item(session: Session, make_raw_item) -> None:
    raw = _raw(make_raw_item)
    processor = _processor()

    report, written = processor.process_and_persist(
        session, [ProcessorInput.from_raw_item(raw)]
    )

    assert written == 1
    assert report.status.value == "SUCCESS"
    assert report.output_count == 1

    rows = _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.raw_item_id == raw.id
    assert row.processor_name == PROCESSOR_NAME
    assert row.processor_version == PROCESSOR_VERSION
    assert row.status is ProcessStatus.SUCCESS
    assert row.normalized_text == "3400 上方减仓后继续持有。"
    assert row.language == "zh"
    assert _as_utc(row.effective_at) >= _as_utc(raw.effective_at)
    assert row.structured_json is not None
    assert row.structured_json["outcome"] == RecordOutcome.SUCCESS.value
    assert row.structured_json["stages"] == [
        "normalize",
        "timezone/effective_at",
        "identity/dedup",
        "validation/audit",
    ]


def test_processed_effective_at_never_precedes_raw_effective_at(
    session: Session, make_raw_item
) -> None:
    processor = _processor()
    # ① 发布时间晚于采集时间：raw.effective_at = published_at
    late_publish = _raw(
        make_raw_item,
        published_at=COLLECTED_AT + timedelta(hours=2),
        source_record_id="late-publish",
    )
    # ② 无发布时间：raw.effective_at = collected_at
    no_publish = _raw(make_raw_item, published_at=None, source_record_id="no-publish")

    processor.process_and_persist(
        session,
        [ProcessorInput.from_raw_item(late_publish), ProcessorInput.from_raw_item(no_publish)],
    )

    stored = {row.raw_item_id: row for row in _rows(session)}
    assert len(stored) == 2
    assert _as_utc(stored[late_publish.id].effective_at) >= _as_utc(late_publish.effective_at)
    assert _as_utc(stored[no_publish.id].effective_at) == _as_utc(no_publish.effective_at)


# ---------------------------------------------------------------------------
# 幂等：重复输入 / 重复运行
# ---------------------------------------------------------------------------
def test_repeat_run_does_not_create_duplicate_processed_rows(
    session: Session, make_raw_item
) -> None:
    raw = _raw(make_raw_item)

    first_processor = _processor()
    first_record = first_processor.process_persisted(session, raw)
    assert first_record.outcome is RecordOutcome.SUCCESS
    assert len(_rows(session)) == 1

    # 重新运行（新实例、索引为空）：落库层按 (raw_item_id, processor, version) 幂等
    second_processor = _processor()
    _, written = second_processor.process_and_persist(
        session, [ProcessorInput.from_raw_item(raw)]
    )

    assert written == 0
    assert len(_rows(session)) == 1  # 没有产生重复 processed 结果


def test_known_identity_key_marks_rerun_as_duplicate(session: Session, make_raw_item) -> None:
    raw = _raw(make_raw_item)
    first = _processor().process_persisted(session, raw)

    replay = _processor(known_identity_keys=[first.identity_key])
    report, written = replay.process_and_persist(session, [ProcessorInput.from_raw_item(raw)])

    assert written == 0
    assert report.duplicate_count == 1
    assert report.status.value == "SUCCESS"  # 幂等重放不得谎报失败
    assert len(_rows(session)) == 1


# ---------------------------------------------------------------------------
# 坏数据隔离与审计
# ---------------------------------------------------------------------------
def test_rejected_record_is_recorded_as_skipped(session: Session, make_raw_item) -> None:
    raw = _raw(make_raw_item, title=None, content_text="   ", source_record_id="empty")

    record = _processor().process_persisted(session, raw)

    assert record.outcome is RecordOutcome.REJECTED
    rows = _rows(session)
    assert len(rows) == 1
    assert rows[0].status is ProcessStatus.SKIPPED  # 留痕但不冒充加工成功
    assert rows[0].normalized_text is None
    assert rows[0].structured_json is not None
    assert rows[0].structured_json["reason"] == "empty_content"


def test_processor_exception_is_isolated_at_collector_level(
    session: Session, make_raw_item
) -> None:
    """Processor 内部异常必须转成 FAILED 记录（并留痕），而不是打断整批。"""
    raw = _raw(make_raw_item, title="正常标题", content_text="正常正文")

    processor = _processor()
    original = CollectionProcessor._process_record

    def _explode(self: CollectionProcessor, record: ProcessorInput, *, reference: object):
        raise RuntimeError("processor-internal-boom")

    CollectionProcessor._process_record = _explode  # type: ignore[method-assign]
    try:
        record = processor.process_persisted(session, raw)
    finally:
        CollectionProcessor._process_record = original  # type: ignore[method-assign]

    assert record.outcome is RecordOutcome.FAILED
    rows = _rows(session)
    assert len(rows) == 1
    assert rows[0].status is ProcessStatus.FAILED
    assert rows[0].structured_json is not None
    assert "processor-internal-boom" in str(rows[0].structured_json)


def test_unpersisted_input_is_not_written(session: Session) -> None:
    processor = _processor()
    record = processor.process_one(
        ProcessorInput(
            collected_at=COLLECTED_AT,
            source_record_id="not-stored",
            content_text="没有 raw_items 记录的事实",
        )
    )

    written = processor.persist_records(session, (record,))

    assert written == 0
    assert _rows(session) == []


def test_structured_json_contains_no_credentials(session: Session, make_raw_item) -> None:
    raw = _raw(
        make_raw_item,
        source_record_id="secret-case",
        raw_json={
            "language": "zh",
            "api_key": SECRET_VALUE,
            "authorization": f"Bearer {SECRET_VALUE}",
            "feed_url": f"https://feeds.example.invalid/rss.xml?api_key={SECRET_VALUE}",
            "headers": {"Authorization": f"Bearer {SECRET_VALUE}"},
            "source_name": "金十数据",
        },
    )

    record = _processor().process_persisted(session, raw)

    rows = _rows(session)
    assert len(rows) == 1
    serialized = str(rows[0].structured_json)
    assert SECRET_VALUE not in serialized
    assert "api_key" not in (rows[0].structured_json or {})
    assert rows[0].structured_json is not None
    assert rows[0].structured_json["metadata"]["feed_url"] == (
        "https://feeds.example.invalid/rss.xml"
    )
    assert rows[0].structured_json["metadata"]["source_name"] == "金十数据"
    assert SECRET_VALUE not in str(record.metadata)
