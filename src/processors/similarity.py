"""文本相似度基线（Phase 2 传播去重的**零依赖**实现）。

为什么自己实现而不用 `jieba` / `scikit-learn`：

- 两者已在 `pyproject.toml` 中获批，但当前环境网络受限尚未安装；
- 传播去重是"防止把 100 条转载当成 100 个独立观点"的**核心防重复计数逻辑**，
  必须有可运行、可复现、可回归的基线，不能依赖"某台机器上是否装好某个包"；
- 因此默认使用确定性的「CJK 字符 n-gram + ASCII 词」切分；
  分词升级路径（jieba）作为**显式开关**（`use_jieba=True`），
  切换即意味着 `model_version` 变化 → 相似度必须整体重算（与 parser_version 同一口径）。

实现口径：

- 归一化复用 :func:`src.common.hashing.normalize_for_hash`
  （NFKC / 去零宽 / 折叠空白 / casefold），与 `raw_items.content_hash` 保持一致，
  避免"同一文本两种口径"；
- TF-IDF 与余弦相似度用纯 Python dict 向量实现（**不使用 numpy**）：
  对 n≈100 的传播簇完全够用；大规模语料的向量化检索属于后续优化（见 `TECH_DEBT.md`）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping

from src.common.hashing import normalize_for_hash

__all__ = [
    "DEFAULT_NGRAM",
    "TOKENIZER_JIEBA",
    "TOKENIZER_STDLIB",
    "TfidfCosineModel",
    "active_tokenizer_version",
    "cosine_similarity",
    "tokenize",
]

#: 默认字符 n-gram 长度（中文二字组合足以区分"看多/看空"这类近义短句）
DEFAULT_NGRAM = 2
#: 零依赖切分策略版本（默认）
TOKENIZER_STDLIB = "cjk-ngram-v1"
#: jieba 分词策略版本（需显式开启）
TOKENIZER_JIEBA = "jieba-v1"

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _load_jieba() -> object | None:
    """按需导入 jieba（未安装时返回 ``None``，不做任何网络/环境假设）。"""
    try:  # pragma: no cover - 取决于环境是否安装（团队已批准依赖，但本环境未装）
        import jieba  # type: ignore[import-not-found]
    except ImportError:
        return None
    return jieba


def active_tokenizer_version(*, use_jieba: bool = False) -> str:
    """返回实际生效的切分策略版本（用于 `model_version` 与审计）。"""
    if use_jieba:
        if _load_jieba() is None:
            raise RuntimeError(
                "use_jieba=True 但当前环境未安装 jieba（团队批准依赖，尚未安装）："
                "请先安装，或使用默认的 cjk-ngram-v1 切分"
            )
        return TOKENIZER_JIEBA
    return TOKENIZER_STDLIB


def _is_punctuation(token: str) -> bool:
    return all(unicodedata.category(char).startswith(("P", "Z", "S")) for char in token)


def tokenize(text: str, *, ngram: int = DEFAULT_NGRAM, use_jieba: bool = False) -> list[str]:
    """把文本切成 token 序列（确定性、无词典、无网络）。

    规则（``cjk-ngram-v1``）：

    - 先做与 `content_hash` 相同的归一化（NFKC / 去零宽 / 折叠空白 / casefold）；
    - CJK 连续片段 → 字符 n-gram（片段短于 ``ngram`` 时保留整段）；
    - ASCII 字母数字 → 整词；
    - 标点 / 其它符号 → 丢弃。

    Args:
        text: 原始文本。
        ngram: 字符 n-gram 长度（必须 >= 1）。
        use_jieba: 是否改用 jieba 分词（未安装则抛 ``RuntimeError``）。

    Returns:
        token 列表（可为空，例如纯标点文本）。
    """
    if ngram < 1:
        raise ValueError("ngram 必须 >= 1")

    normalized = normalize_for_hash(text)
    if not normalized:
        return []

    if use_jieba:
        jieba_module = _load_jieba()
        if jieba_module is None:  # pragma: no cover - 未安装环境下的防御
            raise RuntimeError("use_jieba=True 但未安装 jieba")
        cut = jieba_module.lcut  # type: ignore[attr-defined]
        return [
            token
            for token in (piece.strip() for piece in cut(normalized))
            if token and not _is_punctuation(token)
        ]

    tokens: list[str] = []
    for match in _CJK_RUN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) < ngram:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + ngram] for index in range(len(run) - ngram + 1))
    tokens.extend(_ASCII_WORD_RE.findall(normalized))
    return tokens


def cosine_similarity(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    """两个稀疏向量的余弦相似度（无向量的模长为 0 时返回 0.0）。"""
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot = sum(value * right.get(term, 0.0) for term, value in left.items())
    if dot == 0.0:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class TfidfCosineModel:
    """纯 Python 的 TF-IDF + 余弦相似度模型（先 `fit` 语料，再两两比较）。

    用法：把**同一传播桶**（同一标的 / 同一时段）的文本一次性喂给 :meth:`fit`，
    IDF 在桶内统计（局部语料），随后用 :meth:`similarity` 取任意两篇的相似度。
    """

    def __init__(self, *, ngram: int = DEFAULT_NGRAM, use_jieba: bool = False) -> None:
        self._ngram = ngram
        self._use_jieba = use_jieba
        self._idf: dict[str, float] = {}
        self._vectors: dict[str, dict[str, float]] = {}
        self._tokenizer_version = active_tokenizer_version(use_jieba=use_jieba)

    @property
    def model_version(self) -> str:
        """模型版本（写入 `propagation_edges.model_version`，保证可追溯）。"""
        return f"tfidf-{self._tokenizer_version}-n{self._ngram}"

    @property
    def tokenizer_version(self) -> str:
        return self._tokenizer_version

    @property
    def is_fitted(self) -> bool:
        return bool(self._vectors)

    @property
    def document_count(self) -> int:
        return len(self._vectors)

    def tokens(self, text: str) -> list[str]:
        return tokenize(text, ngram=self._ngram, use_jieba=self._use_jieba)

    def fit(self, documents: Mapping[str, str]) -> TfidfCosineModel:
        """在给定语料上统计 IDF 并生成 L2 归一化后的 TF-IDF 向量。"""
        counts_by_key = {key: Counter(self.tokens(text)) for key, text in documents.items()}
        document_frequency: Counter[str] = Counter()
        for counts in counts_by_key.values():
            document_frequency.update(counts.keys())

        total_documents = max(len(counts_by_key), 1)
        self._idf = {
            term: math.log((1 + total_documents) / (1 + frequency)) + 1.0
            for term, frequency in document_frequency.items()
        }
        self._vectors = {key: self._vectorize(counts) for key, counts in counts_by_key.items()}
        return self

    def _vectorize(self, counts: Counter[str]) -> dict[str, float]:
        total_terms = sum(counts.values())
        if total_terms == 0:
            return {}
        weighted = {
            term: (count / total_terms) * self._idf.get(term, 1.0)
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        if norm == 0.0:
            return {}
        return {term: value / norm for term, value in weighted.items()}

    def vector(self, key: str) -> dict[str, float]:
        """取某篇文档的归一化 TF-IDF 向量（未 fit 或键不存在时抛 ``KeyError``）。"""
        try:
            return self._vectors[key]
        except KeyError as exc:  # pragma: no cover - 防御式分支
            raise KeyError(f"文档 {key!r} 不在已 fit 的语料中（先调用 fit()）") from exc

    def similarity(self, left_key: str, right_key: str) -> float:
        """两篇文档的余弦相似度（0.0 ~ 1.0；任一篇无 token 时为 0.0）。"""
        return round(cosine_similarity(self.vector(left_key), self.vector(right_key)), 6)

    def idf(self, term: str) -> float:
        """取词条 IDF（未出现过的词条返回 ``KeyError``，避免静默 0）。"""
        if term not in self._idf:
            raise KeyError(f"词条 {term!r} 不在 IDF 表中")
        return self._idf[term]