"""采集后处理流水线单元测试（TD-11）。

覆盖：normalize / identity / validation / audit 四个阶段的确定性、
时区边界、``effective_at`` 规则、重复输入去重、坏数据隔离、敏感字段过滤、
空批次与批次状态映射。**纯 Mock：不联网、不依赖数据库。**
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from src.processors.collection.contracts import (
    AUDIT_PIPELINE_STAGES,
    BatchStatus,
    ProcessorInput,
    RecordOutcome,
)
from src.processors.collection.identity import (
    DedupIndex,
    content_fingerprint,
    identity_key,
    is_sha256_hex,
)
from src.processors.collection.normalize import normalize_text, truncate_text
from src.processors.collection.pipeline import (
    REASON_DUPLICATE,
    REASON_EMPTY_CONTENT,
    REASON_INVALID_TIMESTAMP,
    REASON_MISSING_SOURCE_RECORD_ID,
    REASON_PUBLISHED_AT_IN_FUTURE,
    CollectionProcessor,
    batch_status_for,
)
from src.processors.collection.validate import validate_normalized_record

pytestmark = pytest.mark.unit

FIXED_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
COLLECTED_AT = datetime(2026, 9, 22, 11, 30, tzinfo=UTC)
SHANGHAI = timezone(timedelta(hours=8))


def _clock() -> datetime:
    return FIXED_NOW


def _input(**overrides: Any) -> ProcessorInput:
    payload: dict[str, Any] = {
        "collected_at": COLLECTED_AT,
        "source_record_id": "rec-1",
        "source_id": "source-a",
        "item_type": "NEWS",
        "title": "黄金 3400 上方继续看多",
        "content_text": "3400 上方减仓后继续持有。",
        "published_at": COLLECTED_AT - timedelta(minutes=10),
    }
    payload.update(overrides)
    return ProcessorInput(**payload)


def _processor(**overrides: Any) -> CollectionProcessor:
    overrides.setdefault("clock", _clock)
    return CollectionProcessor(**overrides)


class _RawStub:
    """``RawItemLike`` 的最小结构化替身（不需要数据库）。"""

    def __init__(self, **overrides: Any) -> None:
        self.id = overrides.get("id", uuid.uuid4())
        self.source_id = overrides.get("source_id", uuid.uuid4())
        self.source_record_id = overrides.get("source_record_id", "stub-1")
        self.item_type = overrides.get("item_type", "NEWS")
        self.title = overrides.get("title", "gold")
        self.content_text = overrides.get("content_text", "body")
        self.published_at = overrides.get("published_at")
        self.collected_at = overrides.get("collected_at", COLLECTED_AT)
        self.effective_at = overrides.get("effective_at", COLLECTED_AT)
        self.raw_json = overrides.get("raw_json", {})


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------
def test_pipeline_stages_contract() -> None:
    assert AUDIT_PIPELINE_STAGES == (
        "normalize",
        "timezone/effective_at",
        "identity/dedup",
        "validation/audit",
    )


def test_processor_exposes_hook_protocol_fields() -> None:
    processor = _processor()
    assert processor.processor_name == "collection_normalizer"
    assert processor.processor_version.startswith("collection-normalizer-v")
    assert callable(processor.process_persisted)


def test_process_status_mapping_reuses_existing_enum() -> None:
    assert RecordOutcome.SUCCESS.process_status.value == "SUCCESS"
    assert RecordOutcome.FAILED.process_status.value == "FAILED"
    assert RecordOutcome.REJECTED.process_status.value == "SKIPPED"
    assert RecordOutcome.DUPLICATE.process_status.value == "SKIPPED"


def test_processor_input_from_raw_item_maps_fields() -> None:
    raw = _RawStub(
        source_record_id="r-9",
        title="黄金走强",
        content_text="逢低做多",
        raw_json={"language": "zh", "api_key": "SECRETVALUE"},
        published_at=COLLECTED_AT - timedelta(minutes=1),
    )

    record = ProcessorInput.from_raw_item(raw)

    assert record.source_record_id == "r-9"
    assert record.source_id == str(raw.source_id)
    assert record.raw_item_id == raw.id
    assert record.item_type == "NEWS"
    assert record.metadata["language"] == "zh"


# ---------------------------------------------------------------------------
# ① normalize
# ---------------------------------------------------------------------------
def test_normalize_text_nfkc_zero_width_and_whitespace() -> None:
    raw = "　黄金\u200b  3400\xa0上方\n继续看多　"
    assert normalize_text(raw) == "黄金 3400 上方 继续看多"


def test_normalize_text_returns_none_for_empty() -> None:
    assert normalize_text(None) is None
    assert normalize_text("") is None
    assert normalize_text("   \u200b ") is None


def test_truncate_text_flags_truncation() -> None:
    assert truncate_text("abc", max_chars=3) == ("abc", False)
    assert truncate_text("abcd", max_chars=3) == ("abc", True)
    with pytest.raises(ValueError, match="max_chars"):
        truncate_text("abc", max_chars=0)


def test_oversized_text_is_truncated_with_warning() -> None:
    processor = _processor(max_text_chars=5)
    record = processor.process_one(_input(content_text="0123456789"))

    assert record.normalized_text == "01234"
    assert any("截断" in warning for warning in record.warnings)


# ---------------------------------------------------------------------------
# ② timezone / effective_at
# ---------------------------------------------------------------------------
def test_timezone_offset_is_converted_to_utc() -> None:
    processor = _processor()
    record = processor.process_one(
        _input(
            collected_at=datetime(2026, 9, 22, 19, 30, tzinfo=SHANGHAI),
            published_at=datetime(2026, 9, 22, 19, 0, tzinfo=SHANGHAI),
        )
    )

    assert record.collected_at == COLLECTED_AT
    assert record.published_at == COLLECTED_AT - timedelta(minutes=30)
    assert record.effective_at == COLLECTED_AT


def test_naive_database_time_is_interpreted_as_utc() -> None:
    processor = _processor()
    naive = datetime(2026, 9, 22, 11, 30)  # SQLite 读回形态

    record = processor.process_one(_input(collected_at=naive, published_at=None))

    assert record.collected_at == COLLECTED_AT
    assert record.effective_at == COLLECTED_AT


def test_effective_at_is_max_of_published_and_collected() -> None:
    processor = _processor()
    future_publish = COLLECTED_AT + timedelta(minutes=1)

    record = processor.process_one(_input(published_at=future_publish))

    assert record.effective_at == future_publish  # 宁可保守，也不提前可用


def test_effective_at_uses_upstream_floor() -> None:
    processor = _processor()
    floor = COLLECTED_AT + timedelta(hours=1)

    record = processor.process_one(_input(effective_at_floor=floor))

    assert record.effective_at == floor
    assert any("下界" in warning for warning in record.warnings)


def test_effective_at_falls_back_to_collected_at_when_published_missing() -> None:
    processor = _processor()
    record = processor.process_one(_input(published_at=None))

    assert record.published_at is None
    assert record.effective_at == COLLECTED_AT


# ---------------------------------------------------------------------------
# ③ identity / dedup
# ---------------------------------------------------------------------------
def test_identity_key_is_stable_and_source_scoped() -> None:
    fingerprint = content_fingerprint(title="gold", content_text="body")
    assert is_sha256_hex(fingerprint)

    first = identity_key(source_id="s1", source_record_id="r1", content_hash=fingerprint)
    second = identity_key(source_id="s1", source_record_id="r1", content_hash=fingerprint)
    other_source = identity_key(source_id="s2", source_record_id="r1", content_hash=fingerprint)

    assert first == second
    assert first != other_source
    assert is_sha256_hex(first)


def test_identity_key_rejects_empty_source_record_id() -> None:
    fingerprint = content_fingerprint(title="gold", content_text="body")
    with pytest.raises(ValueError, match="source_record_id"):
        identity_key(source_id="s1", source_record_id="  ", content_hash=fingerprint)


def test_dedup_index_marks_first_and_duplicates() -> None:
    index = DedupIndex(["known"])
    assert index.add("known") is False
    assert index.add("fresh") is True
    assert index.add("fresh") is False
    assert len(index) == 2
    assert "fresh" in index
    with pytest.raises(ValueError, match="不能为空"):
        index.add("  ")


def test_duplicate_inputs_within_batch_are_deduplicated() -> None:
    processor = _processor()
    report = processor.process([_input(), _input(), _input(source_record_id="rec-2")])

    assert report.status is BatchStatus.SUCCESS
    assert report.input_count == 3
    assert report.output_count == 2
    assert report.duplicate_count == 1
    duplicate = [r for r in report.records if r.outcome is RecordOutcome.DUPLICATE][0]
    assert duplicate.reason == REASON_DUPLICATE
    assert duplicate.accepted is False


def test_known_identity_keys_dedup_across_runs() -> None:
    fingerprint = content_fingerprint(
        title="黄金 3400 上方继续看多", content_text="3400 上方减仓后继续持有。"
    )
    known = identity_key(source_id="source-a", source_record_id="rec-1", content_hash=fingerprint)

    processor = _processor(known_identity_keys=[known])
    report = processor.process([_input()])

    assert report.duplicate_count == 1
    assert report.output_count == 0
    assert report.status is BatchStatus.SUCCESS  # 幂等重放不算失败


def test_same_content_different_source_is_not_a_duplicate() -> None:
    processor = _processor()
    report = processor.process([_input(), _input(source_id="source-b")])

    assert report.output_count == 2
    assert report.duplicate_count == 0


# ---------------------------------------------------------------------------
# ④ validation / audit
# ---------------------------------------------------------------------------
def test_validate_normalized_record_rules() -> None:
    ok = validate_normalized_record(
        source_record_id="r1",
        normalized_title="t",
        normalized_text="b",
        collected_at=COLLECTED_AT,
        published_at=None,
        reference=FIXED_NOW,
        future_tolerance=timedelta(minutes=5),
    )
    assert ok == ()

    issues = validate_normalized_record(
        source_record_id="",
        normalized_title=None,
        normalized_text=None,
        collected_at=None,
        published_at=FIXED_NOW + timedelta(hours=1),
        reference=FIXED_NOW,
        future_tolerance=timedelta(minutes=5),
    )
    assert {(issue.field, issue.reason) for issue in issues} == {
        ("source_record_id", "missing"),
        ("collected_at", "missing"),
        ("content", "empty"),
        ("published_at", "in_future"),
    }


def test_empty_content_is_rejected() -> None:
    processor = _processor()
    record = processor.process_one(_input(title=None, content_text="   "))

    assert record.outcome is RecordOutcome.REJECTED
    assert record.reason == REASON_EMPTY_CONTENT
    assert record.persistable is True  # 时间有效：可留痕（落库为 SKIPPED）
    assert record.normalized_text is None


def test_missing_source_record_id_is_rejected() -> None:
    processor = _processor()
    record = processor.process_one(_input(source_record_id="   "))

    assert record.outcome is RecordOutcome.REJECTED
    assert record.reason == REASON_MISSING_SOURCE_RECORD_ID


def test_future_published_at_is_rejected() -> None:
    processor = _processor()
    record = processor.process_one(_input(published_at=FIXED_NOW + timedelta(hours=2)))

    assert record.outcome is RecordOutcome.REJECTED
    assert record.reason == REASON_PUBLISHED_AT_IN_FUTURE


def test_invalid_timestamp_type_is_rejected_and_not_persistable() -> None:
    processor = _processor()
    record = processor.process_one(
        _input(collected_at="2026-09-22T11:30:00Z")  # type: ignore[arg-type]
    )

    assert record.outcome is RecordOutcome.REJECTED
    assert record.reason is not None and record.reason.startswith(REASON_INVALID_TIMESTAMP)
    assert record.persistable is False  # 时间不可信：禁止落库（不用"现在"伪造）
    assert any("禁止伪造时间" in warning for warning in record.warnings)


def test_metadata_is_sanitized_in_audit_summary() -> None:
    processor = _processor()
    record = processor.process_one(
        _input(
            metadata={
                "language": "zh",
                "feed_url": "https://feeds.example.invalid/rss.xml?api_key=SECRETVALUE",
                "api_key": "SECRETVALUE",
                "nested": {"config": {"token": "SECRETVALUE"}},
                "note": "Authorization: Bearer SECRETVALUE",
            }
        )
    )

    assert record.outcome is RecordOutcome.SUCCESS
    assert record.language == "zh"
    assert record.metadata["feed_url"] == "https://feeds.example.invalid/rss.xml"
    assert "api_key" not in record.metadata
    assert "nested" not in record.metadata
    assert "SECRETVALUE" not in str(record.metadata)

    structured = record.to_structured_json()
    assert "SECRETVALUE" not in str(structured)
    assert structured["identity_key"] == record.identity_key
    assert structured["stages"] == list(AUDIT_PIPELINE_STAGES)


# ---------------------------------------------------------------------------
# 确定性与批次状态
# ---------------------------------------------------------------------------
def test_processor_is_deterministic_for_same_input() -> None:
    first = _processor().process([_input(), _input(source_record_id="rec-2")])
    second = _processor().process([_input(), _input(source_record_id="rec-2")])

    assert first.status is second.status
    assert [r.identity_key for r in first.records] == [r.identity_key for r in second.records]
    assert [r.effective_at for r in first.records] == [r.effective_at for r in second.records]
    assert [r.normalized_text for r in first.records] == [
        r.normalized_text for r in second.records
    ]
    assert first.to_summary_dict() == second.to_summary_dict()


def test_summary_dict_only_exposes_whitelist_fields() -> None:
    report = _processor().process([_input(metadata={"api_key": "SECRETVALUE"})])
    summary = report.to_summary_dict()

    assert set(summary) <= {
        "processor",
        "processor_version",
        "status",
        "stages",
        "input_count",
        "output_count",
        "duplicate_count",
        "rejected_count",
        "failed_count",
        "started_at",
        "finished_at",
        "warnings",
        "error",
    }
    assert "SECRETVALUE" not in str(summary)
    assert summary["output_count"] == 1
    assert summary["status"] == "SUCCESS"
    assert "input=1" in report.summary()


def test_empty_batch_is_success_with_zero_counts() -> None:
    report = _processor().process([])

    assert report.status is BatchStatus.SUCCESS
    assert (report.input_count, report.output_count, report.rejected_count) == (0, 0, 0)
    assert report.warnings == ()


def test_single_record_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    processor = _processor()
    original = CollectionProcessor._process_record

    def _flaky(self: CollectionProcessor, record: ProcessorInput, *, reference: datetime) -> Any:
        if record.source_record_id == "boom":
            raise RuntimeError("boom-internal")
        return original(self, record, reference=reference)

    monkeypatch.setattr(CollectionProcessor, "_process_record", _flaky)

    report = processor.process(
        [_input(source_record_id="ok-1"), _input(source_record_id="boom")]
    )

    assert report.status is BatchStatus.PARTIAL_FAILED
    assert report.output_count == 1
    assert report.failed_count == 1
    failed = [r for r in report.records if r.outcome is RecordOutcome.FAILED][0]
    assert failed.reason is not None and failed.reason.startswith("unexpected_error")
    assert any("boom-internal" in warning for warning in report.warnings)
    assert report.records[0].outcome is RecordOutcome.SUCCESS  # 好数据仍被处理


def test_all_records_unusable_marks_batch_failed() -> None:
    processor = _processor()
    report = processor.process(
        [_input(title=None, content_text=None), _input(source_record_id=" ")]
    )

    assert report.status is BatchStatus.FAILED
    assert report.rejected_count == 2
    assert report.output_count == 0


@pytest.mark.parametrize(
    ("input_count", "output_count", "unusable_count", "error", "expected"),
    [
        (0, 0, 0, None, BatchStatus.SUCCESS),
        (3, 3, 0, None, BatchStatus.SUCCESS),
        (3, 1, 1, None, BatchStatus.PARTIAL_FAILED),
        (3, 0, 3, None, BatchStatus.FAILED),
        (3, 3, 0, "batch-error", BatchStatus.FAILED),
    ],
)
def test_batch_status_for(
    input_count: int,
    output_count: int,
    unusable_count: int,
    error: str | None,
    expected: BatchStatus,
) -> None:
    assert (
        batch_status_for(
            input_count=input_count,
            output_count=output_count,
            unusable_count=unusable_count,
            error=error,
        )
        is expected
    )
