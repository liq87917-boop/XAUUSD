"""内容哈希与文本规范化测试（08 §4 去重验收项）。"""

from __future__ import annotations

import re

import pytest

from src.common.hashing import content_hash, normalize_for_hash, sha256_bytes, sha256_text

pytestmark = pytest.mark.unit

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_content_hash_is_sha256_hex() -> None:
    assert HEX64.match(content_hash("gold", "看多"))


def test_content_hash_is_stable_and_repeatable() -> None:
    first = content_hash("标题", "正文内容", namespace="weibo")
    second = content_hash("标题", "正文内容", namespace="weibo")
    assert first == second


def test_content_hash_ignores_whitespace_and_case() -> None:
    """同一内容因空白/大小写差异不得被当成新数据。"""
    assert content_hash("XAUUSD   Bullish") == content_hash("xauusd bullish")
    assert content_hash("多\n\n   头  ") == content_hash("多 头")


def test_content_hash_removes_zero_width_characters() -> None:
    assert content_hash("黄金\u200b看多") == content_hash("黄金看多")


def test_content_hash_normalizes_full_width_characters() -> None:
    assert content_hash("ＡＢＣ１２３") == content_hash("abc123")


def test_content_hash_prevents_field_boundary_collision() -> None:
    assert content_hash("ab", "c") != content_hash("a", "bc")


def test_content_hash_namespace_isolates_spaces() -> None:
    assert content_hash("same", namespace="weibo") != content_hash("same", namespace="news")


def test_content_hash_handles_media_only_post() -> None:
    """纯图片帖子（正文为空）仍需得到稳定哈希，不得抛错。"""
    assert HEX64.match(content_hash(None, ""))
    assert content_hash(None, "") == content_hash(None, "")


def test_normalize_for_hash_on_empty_input() -> None:
    assert normalize_for_hash(None) == ""
    assert normalize_for_hash("   ") == ""


def test_sha256_text_does_not_normalize() -> None:
    assert sha256_text("A B") != sha256_text("a b")


def test_sha256_bytes_requires_bytes() -> None:
    assert HEX64.match(sha256_bytes(b"binary"))
    with pytest.raises(TypeError):
        sha256_bytes("not-bytes")  # type: ignore[arg-type]
