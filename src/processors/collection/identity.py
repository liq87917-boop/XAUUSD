"""身份与去重键（Processor 第三阶段：``identity/dedup``）。

设计要点（幂等是红线：重复采集 / 重复运行不得产生重复加工结果）：
1. :func:`content_fingerprint` 复用 :mod:`src.common.hashing`（NFKC / 零宽字符 / 空白折叠 /
   casefold 已在其中统一实现），输出 64 位小写 SHA256；
2. :func:`identity_key` = ``sha256(source_id + source_record_id + content_hash)``——
   既包含来源内 ID（平台重新分配 ID 也能靠内容指纹兜底），又包含来源命名空间
   （不同来源的相同文本是两条独立事实）；
3. :class:`DedupIndex` 只依赖内存状态，**不使用时间 / 随机数**，因此同一输入序列
   必得同一去重结论（可复现、可测试）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from src.common import hashing

__all__ = [
    "CONTENT_NAMESPACE",
    "IDENTITY_NAMESPACE",
    "DedupIndex",
    "content_fingerprint",
    "identity_key",
    "is_sha256_hex",
]

#: 内容指纹命名空间（与其它哈希空间隔离，防止跨用途碰撞）
CONTENT_NAMESPACE: Final[str] = "collection.processor.content"
#: 幂等键命名空间
IDENTITY_NAMESPACE: Final[str] = "collection.processor.identity"


def content_fingerprint(*, title: str | None, content_text: str | None) -> str:
    """内容指纹（归一化文本的 SHA256，64 位小写十六进制）。"""
    return hashing.content_hash(title, content_text, namespace=CONTENT_NAMESPACE)


def identity_key(*, source_id: str | None, source_record_id: str, content_hash: str) -> str:
    """稳定幂等键：同一原始事实重复进入 Processor 必得同一 key。

    Raises:
        ValueError: ``source_record_id`` 为空（幂等键必须可追溯，禁止用随机值兜底）。
    """
    record_id = str(source_record_id).strip()
    if not record_id:
        raise ValueError(
            "source_record_id 为空：identity key 必须由来源记录 ID + 内容指纹构成，"
            "禁止用随机值兜底（否则重复采集会产生重复加工结果）"
        )
    return hashing.content_hash(
        source_id, record_id, content_hash, namespace=IDENTITY_NAMESPACE
    )


class DedupIndex:
    """批次内 + 跨批的 identity 去重索引（纯内存、确定性、无时间依赖）。

    用法::

        index = DedupIndex(known_keys)      # 已知（例如上次已落库）的 key
        index.add(key)   # True = 首次出现；False = 重复
    """

    __slots__ = ("_seen",)

    def __init__(self, initial: Iterable[str] = ()) -> None:
        self._seen: dict[str, None] = {}
        for key in initial:
            self.add(key)

    def add(self, key: str) -> bool:
        """登记 key；返回 ``True`` 表示首次出现，``False`` 表示重复。"""
        text = str(key).strip()
        if not text:
            raise ValueError("identity key 不能为空")
        if text in self._seen:
            return False
        self._seen[text] = None
        return True

    def __contains__(self, key: object) -> bool:
        return str(key) in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def keys(self) -> tuple[str, ...]:
        """已登记 key（插入顺序稳定，供审计摘要使用）。"""
        return tuple(self._seen)


def is_sha256_hex(value: str) -> bool:
    """校验 64 位小写十六进制 SHA256（契约自检用）。"""
    text = str(value).strip()
    if len(text) != 64:
        return False
    return all(char in "0123456789abcdef" for char in text)
