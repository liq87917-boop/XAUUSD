"""文本相似度基线单元测试（零依赖）：切分、TF-IDF、余弦相似度、版本可追溯。

这些测试同时是**阈值取值的回归护栏**：完全同文 = 1.0、带"转发："噪声 ≈ 0.82、
方向相反 ≈ 0.60 —— 一旦切分策略或权重公式被改动，这里会立刻报警。
"""

from __future__ import annotations

import importlib.util

import pytest

from src.processors.propagation import DEFAULT_THRESHOLD
from src.processors.similarity import (
    TOKENIZER_STDLIB,
    TfidfCosineModel,
    active_tokenizer_version,
    cosine_similarity,
    tokenize,
)

pytestmark = pytest.mark.unit

LONG_TEXT = "黄金 2380 附近做多，止损 2365，目标 2450，日内短线。"
NOISY_COPY = "转发：黄金 2380 附近做多，止损 2365，目标 2450，日内短线。@某某黄金"
SHORT_TEXT = "黄金 2450 附近做空，止损 2470，目标 2350，日内短线。"
UNRELATED_TEXT = "美国 8 月 CPI 同比 3.2%，高于市场预期的 3.0%。"

JIEBA_INSTALLED = importlib.util.find_spec("jieba") is not None


# ---------------------------------------------------------------------------
# 1) 切分
# ---------------------------------------------------------------------------
def test_tokenize_produces_cjk_bigrams() -> None:
    tokens = tokenize("黄金看多")

    assert tokens == ["黄金", "金看", "看多"]


def test_tokenize_keeps_short_cjk_run_whole() -> None:
    assert tokenize("多") == ["多"]


def test_tokenize_extracts_ascii_words_lowercased() -> None:
    tokens = tokenize("XAUUSD 反弹 usd")

    assert set(tokens) >= {"xauusd", "usd"}


def test_tokenize_applies_nfkc_and_strips_zero_width() -> None:
    """全角数字/字母与零宽字符不得制造"看起来不同"的文本（与 content_hash 同口径）。"""
    full_width = tokenize("ＸＡＵＵＳＤ２３８０")
    half_width = tokenize("xauusd2380")

    assert full_width == half_width
    assert tokenize("黄金\u200b看多") == tokenize("黄金看多")


def test_tokenize_drops_punctuation_only_text() -> None:
    assert tokenize("！？、，。…——") == []
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_tokenize_respects_ngram_parameter() -> None:
    assert tokenize("黄金看多", ngram=1) == ["黄", "金", "看", "多"]


def test_tokenize_rejects_invalid_ngram() -> None:
    with pytest.raises(ValueError, match="ngram"):
        tokenize("黄金", ngram=0)


def test_active_tokenizer_version_defaults_to_zero_dependency() -> None:
    assert active_tokenizer_version() == TOKENIZER_STDLIB


@pytest.mark.skipif(JIEBA_INSTALLED, reason="本环境已安装 jieba，跳过未安装分支")
def test_jieba_path_requires_installation() -> None:
    """jieba 是**显式可选**路径：未安装时必须报错而不是静默换切分策略。"""
    with pytest.raises(RuntimeError, match="jieba"):
        tokenize("黄金看多", use_jieba=True)
    with pytest.raises(RuntimeError, match="jieba"):
        active_tokenizer_version(use_jieba=True)


# ---------------------------------------------------------------------------
# 2) 余弦相似度
# ---------------------------------------------------------------------------
def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert cosine_similarity({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0}) == pytest.approx(1.0)


def test_cosine_similarity_disjoint_vectors_is_zero() -> None:
    assert cosine_similarity({"a": 1.0}, {"b": 1.0}) == 0.0


def test_cosine_similarity_empty_vector_is_zero() -> None:
    assert cosine_similarity({}, {"a": 1.0}) == 0.0
    assert cosine_similarity({}, {}) == 0.0


def test_cosine_similarity_is_symmetric() -> None:
    left = {"a": 1.0, "b": 0.5}
    right = {"a": 0.2, "c": 0.9}

    assert cosine_similarity(left, right) == pytest.approx(cosine_similarity(right, left))


# ---------------------------------------------------------------------------
# 3) TF-IDF 模型
# ---------------------------------------------------------------------------
def test_tfidf_identical_documents_similarity_is_one() -> None:
    model = TfidfCosineModel().fit({"a": LONG_TEXT, "b": LONG_TEXT})

    assert model.similarity("a", "b") == 1.0
    assert model.is_fitted is True
    assert model.document_count == 2


def test_tfidf_unrelated_documents_similarity_is_low() -> None:
    model = TfidfCosineModel().fit({"a": LONG_TEXT, "b": UNRELATED_TEXT})

    assert model.similarity("a", "b") == 0.0


def test_tfidf_similarity_is_symmetric_and_bounded() -> None:
    model = TfidfCosineModel().fit({"a": LONG_TEXT, "b": NOISY_COPY, "c": SHORT_TEXT})

    for left in ("a", "b", "c"):
        for right in ("a", "b", "c"):
            value = model.similarity(left, right)
            assert 0.0 <= value <= 1.0
            assert value == model.similarity(right, left)


def test_tfidf_noisy_copy_clears_default_threshold() -> None:
    """★ 阈值护栏：带"转发：…"前缀的转载必须被判定为传播（否则漏检）。"""
    model = TfidfCosineModel().fit({"origin": LONG_TEXT, "copy": NOISY_COPY})

    assert model.similarity("origin", "copy") >= DEFAULT_THRESHOLD


def test_tfidf_opposite_direction_stays_below_threshold() -> None:
    """★ 阈值护栏：同句式但方向相反（做多/做空）绝不能合并成同一条观点。"""
    model = TfidfCosineModel().fit({"long": LONG_TEXT, "short": SHORT_TEXT})

    assert model.similarity("long", "short") < DEFAULT_THRESHOLD


def test_tfidf_rare_term_gets_higher_idf() -> None:
    model = TfidfCosineModel().fit(
        {
            "a": "黄金看多",
            "b": "黄金看多",
            "c": "白银看空",
        }
    )

    common = model.idf("黄金")
    rare = model.idf("白银")

    assert rare > common


def test_tfidf_unknown_term_raises() -> None:
    model = TfidfCosineModel().fit({"a": LONG_TEXT})

    with pytest.raises(KeyError):
        model.idf("不存在的词")


def test_tfidf_unknown_document_raises() -> None:
    model = TfidfCosineModel().fit({"a": LONG_TEXT})

    with pytest.raises(KeyError, match="不在已 fit 的语料中"):
        model.similarity("a", "missing")


def test_tfidf_model_version_records_tokenizer_and_ngram() -> None:
    model = TfidfCosineModel()

    assert model.model_version == f"tfidf-{TOKENIZER_STDLIB}-n2"
    assert TfidfCosineModel(ngram=1).model_version == f"tfidf-{TOKENIZER_STDLIB}-n1"


def test_tfidf_handles_text_without_tokens() -> None:
    """纯标点帖子没有 token：相似度必须是 0.0，而不是除零崩溃。"""
    model = TfidfCosineModel().fit({"a": LONG_TEXT, "b": "！！！？？？"})

    assert model.similarity("a", "b") == 0.0
    assert model.vector("b") == {}


def test_tfidf_is_deterministic() -> None:
    documents = {"a": LONG_TEXT, "b": NOISY_COPY, "c": SHORT_TEXT}

    first = TfidfCosineModel().fit(documents).similarity("a", "b")
    second = TfidfCosineModel().fit(documents).similarity("a", "b")

    assert first == second