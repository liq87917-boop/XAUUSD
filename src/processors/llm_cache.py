"""LLM 响应的本地缓存（避免重复消耗 Token；**只存请求/响应，不存 API Key**）。

为什么需要：LLM 抽取器会被反复评估（改 prompt、改解析、出对比报告），
每次重跑都是真金白银。把**模型原始 JSON** 落盘后：
- 复跑零成本、零网络（`readonly` 模式给 CI / 离线复现用）；
- 出了问题可逐条审计"模型当时到底返回了什么"。

键 = `sha256(prompt_version|model|text|has_media)`：
- 含 `prompt_version` → **改 prompt 自动失效**，绝不会读到旧答案；
- 不含密钥、不含时间 → 同一输入恒定命中。

文件布局：``logs/llm_cache/<key 前 2 位>/<key>.json``（二级分片，避免单目录膨胀）。
写入：先写 ``.tmp`` 再 ``os.replace``（原子替换，崩溃不会留半个文件）。
**失败也缓存**（`status=error`）：否则同一坏输入会被反复重试、反复烧钱。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CACHE_MODES",
    "STATUS_ERROR",
    "STATUS_OK",
    "LLMCache",
    "LLMCacheEntry",
    "LLMCacheMissError",
    "cache_key",
    "text_digest",
]

#: 缓存模式：
#: - `auto`    ：命中即用，未命中调 API 并写缓存（默认）
#: - `readonly`：只读；未命中直接报错（CI / 零成本复跑）
#: - `refresh` ：忽略已有缓存，重调并覆盖（换了 prompt / 模型后重跑）
#: - `off`     ：不读不写（纯直连调试）
CACHE_MODES: Final[tuple[str, ...]] = ("auto", "readonly", "refresh", "off")
STATUS_OK: Final[str] = "ok"
STATUS_ERROR: Final[str] = "error"


class LLMCacheMissError(RuntimeError):
    """`readonly` 模式下缓存未命中（说明这条输入还没跑过真实 API）。"""


def text_digest(text: str) -> str:
    """正文摘要（写进缓存便于人工比对）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cache_key(*, prompt_version: str, model: str, text: str, has_media: bool) -> str:
    """确定性缓存键（不含密钥、不含时间）。"""
    payload = json.dumps(
        {
            "prompt_version": prompt_version,
            "model": model,
            "text": text,
            "has_media": bool(has_media),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LLMCacheEntry:
    """一条缓存记录（`raw_content` = 模型原始输出，用于审计与重放）。"""

    key: str
    prompt_version: str
    model: str
    text_sha256: str
    has_media: bool
    created_at: str
    status: str
    raw_content: str = ""
    parsed: Mapping[str, Any] | None = None
    error: str = ""
    usage: Mapping[str, Any] | None = None
    latency_ms: int = 0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "text_sha256": self.text_sha256,
            "has_media": self.has_media,
            "created_at": self.created_at,
            "status": self.status,
            "raw_content": self.raw_content,
            "parsed": dict(self.parsed) if self.parsed is not None else None,
            "error": self.error,
            "usage": dict(self.usage) if self.usage is not None else None,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LLMCacheEntry:
        """从磁盘载入（缺字段给安全默认值，兼容后续新增字段）。"""
        parsed = payload.get("parsed")
        usage = payload.get("usage")
        return cls(
            key=str(payload.get("key", "")),
            prompt_version=str(payload.get("prompt_version", "")),
            model=str(payload.get("model", "")),
            text_sha256=str(payload.get("text_sha256", "")),
            has_media=bool(payload.get("has_media", False)),
            created_at=str(payload.get("created_at", "")),
            status=str(payload.get("status", STATUS_ERROR)),
            raw_content=str(payload.get("raw_content", "")),
            parsed=parsed if isinstance(parsed, Mapping) else None,
            error=str(payload.get("error", "")),
            usage=usage if isinstance(usage, Mapping) else None,
            latency_ms=int(payload.get("latency_ms", 0) or 0),
            attempts=int(payload.get("attempts", 0) or 0),
        )


class LLMCache:
    """文件缓存：`mode` 决定读/写行为（见 `CACHE_MODES`）。"""

    def __init__(self, directory: Path, *, mode: str = "auto") -> None:
        if mode not in CACHE_MODES:
            raise ValueError(f"未知缓存模式：{mode!r}（可选 {CACHE_MODES}）")
        self.directory = Path(directory)
        self.mode = mode
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def path_for(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def load(self, key: str) -> LLMCacheEntry | None:
        """读缓存；未命中或文件损坏返回 None（**损坏不抛异常**，避免评估中断）。"""
        if self.mode in {"refresh", "off"}:
            return None
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if not isinstance(payload, Mapping):
            self.misses += 1
            return None
        self.hits += 1
        return LLMCacheEntry.from_payload(payload)

    def require(self, key: str) -> LLMCacheEntry:
        """`readonly` 场景用：未命中即报错（绝不偷偷联网）。"""
        entry = self.load(key)
        if entry is None:
            raise LLMCacheMissError(f"缓存未命中（mode={self.mode}）：{self.path_for(key)}")
        return entry

    def store(self, entry: LLMCacheEntry) -> Path | None:
        """写缓存（原子替换）；`readonly` / `off` 模式不写。"""
        if self.mode in {"readonly", "off"}:
            return None
        path = self.path_for(entry.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(entry.to_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
        self.writes += 1
        return path

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "mode": self.mode,
            "directory": str(self.directory),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }