"""文本归一化（Processor 第一阶段：``normalize``）。

职责与边界：
- 只做**确定性文本规范化**：Unicode NFKC → 去零宽字符 → 折叠空白 → 去首尾；
- 不改写原始层（``raw_items`` 只追加），加工结果写 ``processed_items``；
- 不包含任何站点 / HTML / 编码探测逻辑（时间与解析属采集器的职责边界）。

为什么保留大小写：`src.common.hashing.normalize_for_hash` 面向哈希（casefold），
而这里的输出会写进 ``processed_items.normalized_text`` 供人阅读与检索，
大小写属于原语义，不应被抹掉。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

__all__ = ["ZERO_WIDTH_CHARS", "normalize_text", "truncate_text"]

#: 零宽字符（网页复制 / 平台贴文常见：肉眼不可见，却会破坏检索与去重）
ZERO_WIDTH_CHARS: Final[tuple[str, ...]] = ("\u200b", "\u200c", "\u200d", "\ufeff")

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_text(value: str | None) -> str | None:
    """归一化文本；返回 ``None`` 表示"归一化后为空"。

    空结果**不**被悄悄写成空串：调用方必须按"坏数据"处理（``REJECTED``），
    避免用空文本冒充有效事实。
    """
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    for char in ZERO_WIDTH_CHARS:
        normalized = normalized.replace(char, "")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized or None


def truncate_text(value: str, *, max_chars: int) -> tuple[str, bool]:
    """截断超长文本；返回 ``(文本, 是否被截断)``（截断必须留下 warning）。"""
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True
