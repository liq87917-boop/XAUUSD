"""传播去重的落库集成测试（1+99 构造端到端）。

验证：
- 1 条原创 + 99 条转载 → `propagation_edges` 落库、`独立观点数 = 1`（而不是 100）；
- 幂等：重跑不新增（自然键 `(from, to, relation, model_version)` 生效）；
- **原始层不被修改**（只读 `raw_items`）；
- 数据库层约束（`created_at >= detected_at`、similarity 0~1）在真实表结构下成立。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import PropagationEdge, RawItem
from database.models.enums import PropagationRelation
from src.processors.propagation import (
    PropagationDetector,
    PropagationDocument,
    store_propagation_edges,
)

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
DETECTED_AT = BASE_TIME + timedelta(hours=2)
ORIGINAL_TEXT = "黄金 2380 附近做多，止损 2365，目标 2450，日内短线。"
NOISY_TEXT = "转发：黄金 2380 附近做多，止损 2365，目标 2450，日内短线。@某某黄金"


def _seed_one_plus_99(session: Session, make_source, make_raw_item):
    """在 raw_items 中构造 1 条原创 + 99 条转载（**不同来源**、时间递增）。"""
    origin_source = make_source(name="propagation-origin")
    copy_source = make_source(name="propagation-copiers")
    origin_raw = make_raw_item(
        source=origin_source,
        content_text=ORIGINAL_TEXT,
        collected_at=BASE_TIME,
        published_at=None,
    )
    copies = [
        make_raw_item(
            source=copy_source,
            content_text=NOISY_TEXT,
            collected_at=BASE_TIME + timedelta(seconds=index + 1),
            published_at=None,
        )
        for index in range(99)
    ]
    documents = [
        PropagationDocument(
            item_id=origin_raw.id,
            text=ORIGINAL_TEXT,
            effective_at=BASE_TIME,
            source_id=origin_source.id,
            group_key="XAUUSD",
        ),
        *(
            PropagationDocument(
                item_id=raw.id,
                text=NOISY_TEXT,
                effective_at=BASE_TIME + timedelta(seconds=index + 1),
                source_id=copy_source.id,
                group_key="XAUUSD",
            )
            for index, raw in enumerate(copies)
        ),
    ]
    return origin_raw, copies, documents


def _count(session: Session, model: type) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 不保存时区偏移（读回 naive）：统一补齐 UTC 后再比较（仅测试辅助）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def test_one_plus_99_edges_are_persisted_and_independence_is_one(
    session: Session, make_source, make_raw_item
) -> None:
    origin_raw, copies, documents = _seed_one_plus_99(session, make_source, make_raw_item)

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)
    created = store_propagation_edges(session, result.edges)

    assert result.independent_opinion_count == 1, "★ 100 条内容相同的帖子只能算 1 个独立观点"
    assert result.duplicate_ratio == pytest.approx(0.99)
    assert created == len(result.edges) == 4950
    assert _count(session, PropagationEdge) == 4950

    edge = session.scalar(
        sa.select(PropagationEdge).where(PropagationEdge.from_item_id == origin_raw.id)
    )
    assert edge is not None
    assert edge.to_item_id in {raw.id for raw in copies}
    assert edge.model_version == result.model_version
    assert edge.similarity == Decimal("0.883067")
    assert edge.relation_type is PropagationRelation.SEMANTIC_SIMILAR
    assert _as_utc(edge.detected_at) == DETECTED_AT
    assert _as_utc(edge.created_at) >= _as_utc(edge.detected_at)  # migration 0005 的 CHECK


def test_edge_storage_is_idempotent(session: Session, make_source, make_raw_item) -> None:
    _origin_raw, _copies, documents = _seed_one_plus_99(session, make_source, make_raw_item)
    detector = PropagationDetector()
    result = detector.detect(documents, detected_at=DETECTED_AT)
    store_propagation_edges(session, result.edges)

    second = store_propagation_edges(session, result.edges)

    assert second == 0
    assert _count(session, PropagationEdge) == 4950


def test_edge_storage_deduplicates_within_batch(
    session: Session, make_source, make_raw_item
) -> None:
    """同一批次内的重复候选（同自然键）只落一行。"""
    _origin_raw, _copies, documents = _seed_one_plus_99(session, make_source, make_raw_item)
    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    created = store_propagation_edges(session, [*result.edges, *result.edges])

    assert created == 4950
    assert _count(session, PropagationEdge) == 4950


def test_propagation_does_not_touch_raw_layer(
    session: Session, make_source, make_raw_item
) -> None:
    """传播去重只读原始层：raw_items 行数与内容都不变。"""
    origin_raw, _copies, documents = _seed_one_plus_99(session, make_source, make_raw_item)
    snapshot = (origin_raw.content_text, origin_raw.content_hash, _count(session, RawItem))

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)
    store_propagation_edges(session, result.edges)
    session.refresh(origin_raw)

    assert snapshot == (origin_raw.content_text, origin_raw.content_hash, _count(session, RawItem))
    assert _count(session, RawItem) == 100


def test_edges_accept_new_model_version_without_overwriting(
    session: Session, make_source, make_raw_item
) -> None:
    """换模型版本（例如换阈值）→ 允许新增同对节点的边，旧版本行保留（append-only）。"""
    _origin_raw, _copies, documents = _seed_one_plus_99(session, make_source, make_raw_item)
    baseline = PropagationDetector(threshold=0.80).detect(documents, detected_at=DETECTED_AT)
    store_propagation_edges(session, baseline.edges)

    stricter = PropagationDetector(threshold=0.95).detect(documents, detected_at=DETECTED_AT)
    created = store_propagation_edges(session, stricter.edges)

    assert stricter.model_version != baseline.model_version
    versions = set(session.scalars(sa.select(PropagationEdge.model_version)).all())
    assert versions == {baseline.model_version, stricter.model_version}
    assert created == len(stricter.edges)
    assert _count(session, PropagationEdge) == len(baseline.edges) + created


def test_edge_requires_existing_raw_items(session: Session) -> None:
    """外键兜底：传播边必须指向真实存在的 raw_items（禁止凭空造节点）。"""
    session.add(
        PropagationEdge(
            from_item_id=uuid.uuid4(),
            to_item_id=uuid.uuid4(),
            relation_type=PropagationRelation.REPOST,
            detected_at=DETECTED_AT,
            model_version="unit-test-v1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_stored_similarity_respects_unit_range_check(
    session: Session, make_source, make_raw_item
) -> None:
    """数据库层校验：similarity 必须在 [0, 1]（越界写入必须被拒绝）。"""
    _origin_raw, copies, _documents = _seed_one_plus_99(session, make_source, make_raw_item)

    session.add(
        PropagationEdge(
            from_item_id=copies[0].id,
            to_item_id=copies[1].id,
            relation_type=PropagationRelation.SEMANTIC_SIMILAR,
            similarity=Decimal("1.5"),
            detected_at=DETECTED_AT,
            model_version="unit-test-v1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()