"""传播去重（1+99 构造）单元测试 —— 团队要求"绝不把转载当成独立观点"。

核心断言：1 条原创 + 99 条复制帖子 → **独立观点数 = 1**（而不是 100）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from database.models.enums import PropagationRelation
from src.processors.propagation import (
    DEFAULT_THRESHOLD,
    PropagationDetector,
    PropagationDocument,
)

pytestmark = pytest.mark.unit

BASE_TIME = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
ORIGINAL_TEXT = "黄金 2380 附近做多，止损 2365，目标 2450，日内短线。"
NOISY_TEXT = "转发：黄金 2380 附近做多，止损 2365，目标 2450，日内短线。@某某黄金"
OPPOSITE_TEXT = "黄金 2450 附近做空，止损 2470，目标 2350，日内短线。"
UNRELATED_TEXT = "美国 8 月 CPI 同比 3.2%，高于市场预期的 3.0%。"
DETECTED_AT = BASE_TIME + timedelta(hours=1)


def _doc(
    text: str,
    *,
    seconds: int = 0,
    source_id: uuid.UUID | None = None,
    group_key: str | None = None,
    item_id: uuid.UUID | None = None,
) -> PropagationDocument:
    return PropagationDocument(
        item_id=item_id or uuid.uuid4(),
        text=text,
        effective_at=BASE_TIME + timedelta(seconds=seconds),
        source_id=source_id,
        group_key=group_key,
    )


def _one_plus_99(origin_text: str = ORIGINAL_TEXT, copy_text: str | None = None):
    """构造 1 条原创 + 99 条复制的经典场景（复制帖按时间递增、文本可带噪声）。"""
    copies_text = copy_text if copy_text is not None else origin_text
    origin = _doc(origin_text, item_id=uuid.uuid4())
    copies = [
        _doc(copies_text, seconds=index + 1, item_id=uuid.uuid4()) for index in range(99)
    ]
    return origin, copies


def _edge_keys(result) -> set[tuple[uuid.UUID, uuid.UUID]]:
    return {(edge.from_item_id, edge.to_item_id) for edge in result.edges}


# ---------------------------------------------------------------------------
# 1) 1+99 核心场景
# ---------------------------------------------------------------------------
def test_one_plus_99_collapses_into_single_independent_opinion() -> None:
    """★ 1 原创 + 99 完全同文复制 → 1 个传播簇 = **1 个独立观点**。"""
    origin, copies = _one_plus_99()
    documents = [origin, *copies]

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    assert result.item_count == 100
    assert result.independent_opinion_count == 1
    assert result.duplicate_ratio == pytest.approx(0.99)
    assert len(result.edges) == 4950  # C(100,2)：每条边都由实测相似度支撑
    assert result.compared_pairs == 4950
    assert result.clusters[0].root_item_id == origin.item_id
    assert result.clusters[0].duplicate_count == 99
    assert {edge.relation_type for edge in result.edges} == {PropagationRelation.REPOST}


def test_one_plus_99_with_forward_noise_still_collapses() -> None:
    """★ 带"转发：…"噪声的转载同样必须合并（阈值护栏在相似度层已覆盖）。"""
    origin, copies = _one_plus_99(ORIGINAL_TEXT, NOISY_TEXT)

    result = PropagationDetector().detect([origin, *copies], detected_at=DETECTED_AT)

    assert result.independent_opinion_count == 1
    assert result.clusters[0].root_item_id == origin.item_id
    relations = {edge.relation_type for edge in result.edges}
    assert PropagationRelation.SEMANTIC_SIMILAR in relations  # 源头↔噪声转载：语义相似
    assert PropagationRelation.REPOST in relations  # 噪声转载之间：原样转发


def test_unrelated_items_are_never_merged() -> None:
    documents = [
        _doc(ORIGINAL_TEXT),
        _doc(UNRELATED_TEXT, seconds=1),
        _doc(OPPOSITE_TEXT, seconds=2),
    ]

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    assert result.edges == ()
    assert result.independent_opinion_count == 3
    assert result.duplicate_ratio == 0.0
    assert result.compared_pairs == 3


def test_opposite_direction_is_not_merged() -> None:
    """同句式但方向相反 → 两个独立观点（绝不能因为句式相似而合并）。"""
    result = PropagationDetector().detect(
        [_doc(ORIGINAL_TEXT), _doc(OPPOSITE_TEXT, seconds=5)], detected_at=DETECTED_AT
    )

    assert result.independent_opinion_count == 2
    assert result.edges == ()


# ---------------------------------------------------------------------------
# 2) 边的方向与形态
# ---------------------------------------------------------------------------
def test_edges_always_point_from_earlier_to_later() -> None:
    origin, copies = _one_plus_99(ORIGINAL_TEXT, NOISY_TEXT)
    documents = [origin, *copies]
    by_id = {document.item_id: document for document in documents}

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    for edge in result.edges:
        assert by_id[edge.from_item_id].effective_at <= by_id[edge.to_item_id].effective_at
        assert edge.from_item_id != edge.to_item_id


def test_edge_similarity_is_decimal_within_unit_range() -> None:
    origin, copies = _one_plus_99(ORIGINAL_TEXT, NOISY_TEXT)

    result = PropagationDetector().detect([origin, *copies], detected_at=DETECTED_AT)

    for edge in result.edges:
        assert isinstance(edge.similarity, Decimal)
        assert Decimal("0") < edge.similarity <= Decimal("1")
        assert edge.similarity >= Decimal(str(DEFAULT_THRESHOLD))
        assert edge.detected_at == DETECTED_AT
        assert edge.model_version == result.model_version


def test_edge_key_is_unique_per_relation() -> None:
    origin, copies = _one_plus_99()

    result = PropagationDetector().detect([origin, *copies], detected_at=DETECTED_AT)

    keys = [
        (edge.from_item_id, edge.to_item_id, edge.relation_type, edge.model_version)
        for edge in result.edges
    ]
    assert len(keys) == len(set(keys)), "自然键必须唯一（与 propagation_edges 唯一约束一致）"


def test_identical_text_from_same_source_is_marked_same_source() -> None:
    source_id = uuid.uuid4()
    first = _doc(NOISY_TEXT, source_id=source_id)
    second = _doc(NOISY_TEXT + " #黄金", seconds=1, source_id=source_id)

    result = PropagationDetector().detect([first, second], detected_at=DETECTED_AT)

    assert len(result.edges) == 1
    assert result.edges[0].relation_type is PropagationRelation.SAME_SOURCE


def test_identical_text_from_different_sources_is_repost() -> None:
    first = _doc(ORIGINAL_TEXT, source_id=uuid.uuid4())
    second = _doc(ORIGINAL_TEXT, seconds=1, source_id=uuid.uuid4())

    result = PropagationDetector().detect([first, second], detected_at=DETECTED_AT)

    assert result.edges[0].relation_type is PropagationRelation.REPOST


# ---------------------------------------------------------------------------
# 3) 分桶 / 规模保护 / 输入校验
# ---------------------------------------------------------------------------
def test_group_key_prevents_cross_topic_merges() -> None:
    """不同桶（例如不同标的）即使文本完全相同也不得建边。"""
    documents = [
        _doc(ORIGINAL_TEXT, group_key="XAUUSD"),
        _doc(ORIGINAL_TEXT, seconds=1, group_key="XAGUSD"),
    ]

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    assert result.edges == ()
    assert result.independent_opinion_count == 2
    assert result.compared_pairs == 0  # 每桶只有 1 条 → 不产生比较


def test_compared_pairs_counts_within_buckets_only() -> None:
    documents = [
        _doc(ORIGINAL_TEXT, group_key="A"),
        _doc(ORIGINAL_TEXT, seconds=1, group_key="A"),
        _doc(ORIGINAL_TEXT, seconds=2, group_key="B"),
        _doc(ORIGINAL_TEXT, seconds=3, group_key="B"),
    ]

    result = PropagationDetector().detect(documents, detected_at=DETECTED_AT)

    assert result.compared_pairs == 2  # 每桶 1 对
    assert result.independent_opinion_count == 2


def test_max_pairs_guard_raises_instead_of_silently_truncating() -> None:
    documents = [_doc(ORIGINAL_TEXT, seconds=index) for index in range(4)]  # 6 对

    with pytest.raises(ValueError, match="max_pairs"):
        PropagationDetector(max_pairs=5).detect(documents, detected_at=DETECTED_AT)


def test_duplicate_item_ids_are_rejected() -> None:
    shared = uuid.uuid4()
    documents = [_doc(ORIGINAL_TEXT, item_id=shared), _doc(ORIGINAL_TEXT, item_id=shared)]

    with pytest.raises(ValueError, match="重复 item_id"):
        PropagationDetector().detect(documents, detected_at=DETECTED_AT)


@pytest.mark.parametrize("threshold", [0.0, -0.1, 1.5])
def test_invalid_threshold_is_rejected(threshold: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        PropagationDetector(threshold=threshold)


def test_invalid_max_pairs_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_pairs"):
        PropagationDetector(max_pairs=0)


# ---------------------------------------------------------------------------
# 4) 边界与确定性
# ---------------------------------------------------------------------------
def test_empty_input_yields_no_clusters() -> None:
    result = PropagationDetector().detect([], detected_at=DETECTED_AT)

    assert result.clusters == ()
    assert result.edges == ()
    assert result.independent_opinion_count == 0
    assert result.duplicate_ratio == 0.0


def test_single_document_is_one_independent_opinion() -> None:
    result = PropagationDetector().detect([_doc(ORIGINAL_TEXT)], detected_at=DETECTED_AT)

    assert result.independent_opinion_count == 1
    assert result.edges == ()
    assert result.compared_pairs == 0


def test_threshold_one_merges_only_exact_duplicates() -> None:
    origin = _doc(ORIGINAL_TEXT)
    noisy = _doc(NOISY_TEXT, seconds=1)
    exact = _doc(ORIGINAL_TEXT, seconds=2)

    result = PropagationDetector(threshold=1.0).detect(
        [origin, noisy, exact], detected_at=DETECTED_AT
    )

    assert result.independent_opinion_count == 2  # 噪声版本保持独立
    assert _edge_keys(result) == {(origin.item_id, exact.item_id)}


def test_detection_is_deterministic_under_input_order() -> None:
    origin, copies = _one_plus_99(ORIGINAL_TEXT, NOISY_TEXT)
    documents = [origin, *copies]
    detector = PropagationDetector()

    forward = detector.detect(documents, detected_at=DETECTED_AT)
    backward = detector.detect(list(reversed(documents)), detected_at=DETECTED_AT)

    assert _edge_keys(forward) == _edge_keys(backward)
    assert forward.clusters[0].root_item_id == backward.clusters[0].root_item_id
    assert [edge.similarity for edge in forward.edges] == [
        edge.similarity for edge in backward.edges
    ]


def test_cluster_members_are_time_ordered() -> None:
    later = _doc(ORIGINAL_TEXT, seconds=10)
    earlier = _doc(ORIGINAL_TEXT, seconds=1)

    result = PropagationDetector().detect([later, earlier], detected_at=DETECTED_AT)

    cluster = result.clusters[0]
    assert cluster.root_item_id == earlier.item_id
    assert cluster.item_ids == (earlier.item_id, later.item_id)


def test_model_version_records_threshold_and_tokenizer() -> None:
    detector = PropagationDetector(threshold=0.9)

    assert detector.model_version == "tfidf-cjk-ngram-v1-n2-thr0.90"
    assert detector.threshold == 0.9


def test_results_summary_is_human_readable() -> None:
    origin, copies = _one_plus_99()

    summary = PropagationDetector().detect([origin, *copies], detected_at=DETECTED_AT).summary()

    assert "items=100" in summary
    assert "clusters=1" in summary
    assert "model=tfidf-cjk-ngram-v1-n2-thr0.80" in summary


def test_chained_similarity_merges_transitively() -> None:
    """已知限制（显式锁定）：A≈B、B≈C 会因连通分量合并为同一簇（链式合并）。

    这里把当前行为写成测试，避免"悄悄改了却没人知道"；更严格的簇内一致性属 Phase 3。
    """
    a = _doc(ORIGINAL_TEXT)
    b = _doc(NOISY_TEXT, seconds=1)
    c = _doc(NOISY_TEXT + " 仅供参考", seconds=2)

    result = PropagationDetector().detect([a, b, c], detected_at=DETECTED_AT)

    assert result.independent_opinion_count == 1
    assert result.clusters[0].root_item_id == a.item_id
    assert result.clusters[0].size == 3