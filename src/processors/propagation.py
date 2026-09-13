"""信息传播去重（**1+99 构造**）——"绝不把转载当成独立观点"。

背景（`docs/05` Phase 2、`docs/08 §5`）：

    同一条内容被 100 个账号转发时，若不去重，作者独立性会被严重高估、
    权重会被重复计算。因此必须先建立**传播关系**（`propagation_edges`），
    再在"作者 × 观点"层面统计独立观点数。

算法（确定性、零第三方依赖；`src/processors/similarity.py` 提供相似度）：

1. 按 ``group_key``（默认 = 同一标的 / 主题）**分桶**比较，避免全量 O(n²) 失控；
2. 桶内用 TF-IDF + 余弦相似度比较（同一桶一次性 fit，IDF 局部统计）；
3. 相似度 >= ``threshold``（默认 0.85）→ 建边，方向 **早 → 晚**
   （`effective_at` 升序；平局用 `item_id` 保证确定性）；
4. 关系类型判定优先级：
   - 归一化文本完全相同 → ``REPOST``（原样转发，最强证据）；
   - 同一 `source_id` → ``SAME_SOURCE``（同源账号互转）；
   - 其余 → ``SEMANTIC_SIMILAR``；
5. 禁止自环；同一自然键 `(from_item_id, to_item_id, relation_type, model_version)`
   只产生一条边（与 `propagation_edges` 的唯一约束一致）；
6. 连通分量（union-find）→ 每个分量的**最早节点**视为该传播簇的源头，
   ``independent_opinion_count = 分量数``（1+99 → **1**）。

已知限制（显式登记，见 `TECH_DEBT.md`）：

- 连通分量可能"链式合并"（A≈B、B≈C 但 A≉C）；缓解手段是阈值 + 分桶，
  更严格的簇内一致性检验属 Phase 3；
- 桶内两两比较为 O(n²)（100 条 = 4950 对，秒级）；大规模需改用 SimHash / 倒排索引 /
  向量检索（`max_pairs` 已做保护，超限直接报错而不是静默截断）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import PropagationEdge
from database.models.enums import PropagationRelation
from src.common.hashing import normalize_for_hash
from src.processors.similarity import DEFAULT_NGRAM, TfidfCosineModel

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_MAX_PAIRS",
    "PropagationCluster",
    "PropagationDetector",
    "PropagationDocument",
    "PropagationEdgeCandidate",
    "PropagationResult",
    "store_propagation_edges",
]

#: 相似度阈值：同一传播簇的判定门槛。
#: 取值依据（2026-09 实测，`mock` 语料，模型 `tfidf-cjk-ngram-v1-n2`）：
#:   - 完全同文                    → 1.000（必须判为传播）
#:   - 同文带"转发：… @账号"噪声    → 0.820（必须判为传播）
#:   - 同句式但方向相反（做多/做空）→ 0.603（绝不能判为传播）
#: 因此 0.80 在"噪声可容忍"与"反向观点不误合"之间留有两侧余量。
#: 阈值属于模型参数：**调整必须改 model_version 并整体重算**（旧边不覆盖）。
DEFAULT_THRESHOLD = 0.80
#: 单次运行的两两比较上限（防止误用导致 O(n²) 爆炸；超限**报错**而非静默截断）
DEFAULT_MAX_PAIRS = 200_000


@dataclass(frozen=True, slots=True)
class PropagationDocument:
    """参与传播判定的一条内容（必须来自 `raw_items`，保证可回溯）。"""

    item_id: uuid.UUID
    text: str
    effective_at: datetime
    source_id: uuid.UUID | None = None
    #: 分桶键（默认 None = 全部同桶；实践中建议用标的代码或主题）
    group_key: str | None = None


@dataclass(frozen=True, slots=True)
class PropagationEdgeCandidate:
    """一条待落库的传播边（对应 `propagation_edges` 一行）。"""

    from_item_id: uuid.UUID
    to_item_id: uuid.UUID
    relation_type: PropagationRelation
    similarity: Decimal
    detected_at: datetime
    model_version: str


@dataclass(frozen=True, slots=True)
class PropagationCluster:
    """一个传播簇（源头 + 其传播者）。"""

    root_item_id: uuid.UUID
    item_ids: tuple[uuid.UUID, ...]

    @property
    def size(self) -> int:
        return len(self.item_ids)

    @property
    def duplicate_count(self) -> int:
        """重复条目数（源头本身不计）。"""
        return max(len(self.item_ids) - 1, 0)


@dataclass(frozen=True, slots=True)
class PropagationResult:
    """一次传播判定的结果（可打印、可测试、可写审计）。"""

    edges: tuple[PropagationEdgeCandidate, ...]
    clusters: tuple[PropagationCluster, ...]
    compared_pairs: int
    model_version: str
    threshold: float

    @property
    def item_count(self) -> int:
        return sum(cluster.size for cluster in self.clusters)

    @property
    def independent_opinion_count(self) -> int:
        """独立观点数 = 传播簇数量（1+99 → 1）。"""
        return len(self.clusters)

    @property
    def duplicate_ratio(self) -> float:
        """重复率 = 1 - 独立观点数 / 条目数（无条目时 0.0）。"""
        if self.item_count == 0:
            return 0.0
        return round(1.0 - self.independent_opinion_count / self.item_count, 6)

    def summary(self) -> str:
        return (
            f"items={self.item_count} edges={len(self.edges)} "
            f"clusters={self.independent_opinion_count} "
            f"duplicate_ratio={self.duplicate_ratio} pairs={self.compared_pairs} "
            f"model={self.model_version}"
        )


class _UnionFind:
    """并查集（连通分量）：用于把相似边聚成传播簇。"""

    def __init__(self, keys: Iterable[uuid.UUID]) -> None:
        self._parent = {key: key for key in keys}

    def find(self, key: uuid.UUID) -> uuid.UUID:
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # 路径压缩
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, left: uuid.UUID, right: uuid.UUID) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


class PropagationDetector:
    """基于 TF-IDF 余弦相似度的传播关系检测器（纯计算，不触碰数据库）。"""

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        ngram: int = DEFAULT_NGRAM,
        use_jieba: bool = False,
        max_pairs: int = DEFAULT_MAX_PAIRS,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold 必须在 (0, 1] 区间内")
        if max_pairs < 1:
            raise ValueError("max_pairs 必须 >= 1")
        self._threshold = threshold
        self._max_pairs = max_pairs
        self._model = TfidfCosineModel(ngram=ngram, use_jieba=use_jieba)

    @property
    def model_version(self) -> str:
        """模型版本 = 切分策略 + n-gram + 阈值（阈值变化必须重算，故纳入版本号）。"""
        return f"{self._model.model_version}-thr{self._threshold:.2f}"

    @property
    def threshold(self) -> float:
        return self._threshold

    def detect(
        self,
        documents: Sequence[PropagationDocument],
        *,
        detected_at: datetime | None = None,
    ) -> PropagationResult:
        """计算传播边与传播簇。

        Args:
            documents: 参与判定的内容（`item_id` 必须唯一）。
            detected_at: 检测时刻（写入 `propagation_edges.detected_at`，默认当前 UTC）。

        Returns:
            :class:`PropagationResult`。

        Raises:
            ValueError: `item_id` 重复，或两两比较对数超过 `max_pairs`。
        """
        self._assert_unique(documents)
        buckets = self._bucketize(documents)
        if sum(self._pair_count(len(bucket)) for bucket in buckets.values()) > self._max_pairs:
            raise ValueError(
                f"两两比较对数超过 max_pairs={self._max_pairs}：请按 group_key 分桶分批处理"
                "（大语料需改用 SimHash / 向量检索，见 TECH_DEBT）"
            )

        moment = detected_at or datetime.now(UTC)
        edges: list[PropagationEdgeCandidate] = []
        compared_pairs = 0
        union_find = _UnionFind(document.item_id for document in documents)

        for _group, bucket in sorted(buckets.items()):
            ordered = sorted(bucket, key=lambda doc: (doc.effective_at, str(doc.item_id)))
            model = self._model.fit({str(document.item_id): document.text for document in ordered})
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    compared_pairs += 1
                    similarity = model.similarity(str(left.item_id), str(right.item_id))
                    if similarity < self._threshold:
                        continue
                    edges.append(self._build_edge(left, right, similarity, moment))
                    union_find.union(left.item_id, right.item_id)

        return PropagationResult(
            edges=tuple(edges),
            clusters=self._build_clusters(documents, union_find),
            compared_pairs=compared_pairs,
            model_version=self.model_version,
            threshold=self._threshold,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _assert_unique(documents: Sequence[PropagationDocument]) -> None:
        seen: set[uuid.UUID] = set()
        for document in documents:
            if document.item_id in seen:
                raise ValueError(
                    f"documents 中存在重复 item_id={document.item_id}（传播边必须唯一）"
                )
            seen.add(document.item_id)

    @staticmethod
    def _pair_count(size: int) -> int:
        return size * (size - 1) // 2

    def _bucketize(
        self, documents: Sequence[PropagationDocument]
    ) -> dict[str, list[PropagationDocument]]:
        """按 `group_key` 分桶（None → 统一桶 'ALL'）。"""
        buckets: dict[str, list[PropagationDocument]] = {}
        for document in documents:
            buckets.setdefault(document.group_key or "ALL", []).append(document)
        return buckets

    def _build_edge(
        self,
        left: PropagationDocument,
        right: PropagationDocument,
        similarity: float,
        detected_at: datetime,
    ) -> PropagationEdgeCandidate:
        """方向恒为 早 → 晚；关系类型按"原样转发 > 同源 > 语义相似"优先级判定。"""
        early, late = self._orient(left, right)
        return PropagationEdgeCandidate(
            from_item_id=early.item_id,
            to_item_id=late.item_id,
            relation_type=self._relation_type(early, late, similarity),
            similarity=Decimal(str(round(similarity, 6))),
            detected_at=detected_at,
            model_version=self.model_version,
        )

    @staticmethod
    def _orient(
        left: PropagationDocument, right: PropagationDocument
    ) -> tuple[PropagationDocument, PropagationDocument]:
        if (left.effective_at, str(left.item_id)) <= (right.effective_at, str(right.item_id)):
            return left, right
        return right, left

    @staticmethod
    def _relation_type(
        early: PropagationDocument, late: PropagationDocument, similarity: float
    ) -> PropagationRelation:
        """关系判定优先级：完全同文 → REPOST；同源 → SAME_SOURCE；否则语义相似。"""
        if normalize_for_hash(early.text) == normalize_for_hash(late.text):
            return PropagationRelation.REPOST
        if early.source_id is not None and early.source_id == late.source_id:
            return PropagationRelation.SAME_SOURCE
        if similarity >= 0.999:
            return PropagationRelation.REPOST
        return PropagationRelation.SEMANTIC_SIMILAR

    @staticmethod
    def _build_clusters(
        documents: Sequence[PropagationDocument], union_find: _UnionFind
    ) -> tuple[PropagationCluster, ...]:
        """按连通分量聚簇；源头 = 分量内最早（`effective_at`，平局用 item_id）的节点。"""
        members: dict[uuid.UUID, list[PropagationDocument]] = {}
        for document in documents:
            members.setdefault(union_find.find(document.item_id), []).append(document)

        clusters: list[PropagationCluster] = []
        for group in members.values():
            ordered = sorted(group, key=lambda doc: (doc.effective_at, str(doc.item_id)))
            clusters.append(
                PropagationCluster(
                    root_item_id=ordered[0].item_id,
                    item_ids=tuple(document.item_id for document in ordered),
                )
            )
        clusters.sort(key=lambda cluster: str(cluster.root_item_id))
        return tuple(clusters)


def store_propagation_edges(session: Session, edges: Iterable[PropagationEdgeCandidate]) -> int:
    """把候选传播边写入 `propagation_edges`（幂等：自然键已存在则跳过）。

    实现说明：**先一次性批量查重**（同一 `model_version` 下已有的
    `(from_item_id, to_item_id, relation_type)`），再插入缺失行。
    为什么不用逐条 SELECT：1+99 这类构造会产生数千条边，逐条查询会放大 IO。

    Args:
        session: 当前事务的 Session（本函数不 commit）。
        edges: :class:`PropagationEdgeCandidate` 序列。

    Returns:
        实际新增的行数。
    """
    candidates = list(edges)
    if not candidates:
        return 0

    model_versions = {edge.model_version for edge in candidates}
    existing_keys = {
        (row.from_item_id, row.to_item_id, row.relation_type)
        for row in session.execute(
            sa.select(
                PropagationEdge.from_item_id,
                PropagationEdge.to_item_id,
                PropagationEdge.relation_type,
            ).where(PropagationEdge.model_version.in_(model_versions))
        ).all()
    }

    created = 0
    seen: set[tuple[uuid.UUID, uuid.UUID, PropagationRelation]] = set()
    for edge in candidates:
        key = (edge.from_item_id, edge.to_item_id, edge.relation_type)
        if key in existing_keys or key in seen:
            continue
        seen.add(key)
        session.add(
            PropagationEdge(
                from_item_id=edge.from_item_id,
                to_item_id=edge.to_item_id,
                relation_type=edge.relation_type,
                similarity=edge.similarity,
                detected_at=edge.detected_at,
                model_version=edge.model_version,
            )
        )
        created += 1
    session.flush()
    return created