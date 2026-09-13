"""文本规范化与内容哈希（Raw 数据幂等去重基础）。

对应文档：
- 04_数据表结构及字段定义 第 5 节：``raw_items.content_hash``（SHA256）。
- 08_测试与验收标准 4：同一记录重复采集不得生成重复 RawItem。
- 03_数据库完整设计 第 10 节：原始数据不可覆盖。

设计原则：
1. 哈希只依赖"语义等价"的规范化文本（NFKC、去零宽字符、折叠空白、忽略大小写），
   避免同一内容因空格/大小写差异被当成新数据。
2. 哈希不得依赖"当前时间"等运行期状态，否则无法重放与回溯。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = ["content_hash", "normalize_for_hash", "sha256_bytes", "sha256_text"]

_FIELD_SEPARATOR = "\x1f"  # ASCII Unit Separator：避免字段拼接歧义
_NAMESPACE_SEPARATOR = "\x1e"  # ASCII Record Separator
_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff")


def normalize_for_hash(text: str | None) -> str:
    """把文本规范化为可哈希的稳定形式。

    - NFKC 兼容归一化（全角/半角、合字统一）
    - 移除零宽字符（微博等平台常见，肉眼不可见但会破坏去重）
    - 空白折叠为单个空格并去除首尾
    - casefold 忽略大小写差异
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    for char in _ZERO_WIDTH:
        normalized = normalized.replace(char, "")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.casefold()


def content_hash(*parts: str | None, namespace: str | None = None) -> str:
    """计算稳定的 SHA256 内容哈希（小写十六进制，64 字符）。

    Args:
        *parts: 参与哈希的文本片段（如标题、正文、原始ID）。
        namespace: 可选命名空间，用于隔离不同来源/不同类型的哈希空间。

    Returns:
        64 字符十六进制哈希。

    Notes:
        - 纯图片帖子（正文为空）也能得到稳定哈希，不会抛错。
        - ``parts`` 之间的分隔符保证 ``("ab", "c")`` 与 ``("a", "bc")`` 不碰撞。
    """
    payload = _FIELD_SEPARATOR.join(normalize_for_hash(part) for part in parts)
    if namespace:
        payload = f"{normalize_for_hash(namespace)}{_NAMESPACE_SEPARATOR}{payload}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    """对原始文本（不做规范化）计算 SHA256，用于逐字节完整性校验。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """对二进制内容计算 SHA256，用于 raw_media 文件哈希。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"sha256_bytes 需要 bytes，实际为 {type(data).__name__}")
    return hashlib.sha256(bytes(data)).hexdigest()
