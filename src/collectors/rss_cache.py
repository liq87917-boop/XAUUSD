"""RSS 抓取缓存（与 `src/processors/llm_cache.py` **同一套语义**，只是换成"响应体"）。

为什么需要它（团队要求"复用缓存机制"）：
1. **复跑零网络**：`readonly` 模式直接重放上次响应 → `--dry-run` / 离线核验 / CI 都能跑；
2. **条件请求**：`ETag` / `Last-Modified` 必须跨进程持久化，否则每次都是全量下载（对源站不友好）；
3. **原始数据留档**：出问题时能逐条审计"那次 feed 到底返回了什么"（`raw` 原文保留）。

键 = `sha256(url)`（不含时间）；文件布局 ``logs/rss_cache/<key 前 2 位>/<key>.json``；
写入 ``.tmp`` + ``os.replace``（原子替换）；**失败也缓存**（避免对同一坏源反复打请求）。
**缓存里只存源站公开响应，不含任何密钥**（本采集器不发认证信息）。
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
    "RssCache",
    "RssCacheEntry",
    "STATUS_ERROR",
    "STATUS_OK",
]

#: 缓存模式（与 `llm_cache` 完全一致）：
#: - `auto`    ：命中即用，未命中抓取并写缓存（默认）
#: - `readonly`：只读；未命中返回 None（调用方据此拒绝联网，保证零费用/零请求）
#: - `refresh` ：忽略已有缓存，重新抓取并覆盖
#: - `off`     ：不读不写（纯直连）
CACHE_MODES: Final[tuple[str, ...]] = ("auto", "readonly", "refresh", "off")
STATUS_OK: Final[str] = "ok"
STATUS_ERROR: Final[str] = "error"


@dataclass(frozen=True, slots=True)
class RssCacheEntry:
    """一条缓存记录（`body` = 源站响应的原始文本）。"""

    url: str
    status: int
    fetched_at: str
    body: str = ""
    etag: str = ""
    last_modified: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """是否为可用的 200 响应（`error` 非空或状态非 200 都视为不可用）。"""
        return self.status == 200 and not self.error

    def to_payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "body": self.body,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RssCacheEntry:
        """从磁盘载入（缺字段给安全默认值，向前兼容）。"""
        return cls(
            url=str(payload.get("url", "")),
            status=int(payload.get("status", 0) or 0),
            fetched_at=str(payload.get("fetched_at", "")),
            body=str(payload.get("body", "")),
            etag=str(payload.get("etag", "")),
            last_modified=str(payload.get("last_modified", "")),
            error=str(payload.get("error", "")),
        )


class RssCache:
    """文件缓存：`mode` 决定读/写行为（见 `CACHE_MODES`）。"""

    def __init__(self, directory: Path, *, mode: str = "auto") -> None:
        if mode not in CACHE_MODES:
            raise ValueError(f"未知缓存模式：{mode!r}（可选 {CACHE_MODES}）")
        self.directory = Path(directory)
        self.mode = mode
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @staticmethod
    def key_for(url: str) -> str:
        """确定性缓存键（只依赖 URL，不含时间/密钥）。"""
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def load(self, key: str) -> RssCacheEntry | None:
        """读缓存；未命中、模式不允许或文件损坏一律返回 None（**损坏不抛异常**）。"""
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
        return RssCacheEntry.from_payload(payload)

    def store(self, entry: RssCacheEntry) -> Path | None:
        """写缓存（原子替换）；`readonly` / `off` 模式不写。"""
        if self.mode in {"readonly", "off"}:
            return None
        path = self.path_for(self.key_for(entry.url))
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
