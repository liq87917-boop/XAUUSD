"""`RssCache` 单元测试（**零网络**：只碰 tmp_path 下的文件）。

覆盖：键确定性、四种模式语义、损坏文件 → miss、失败条目也缓存、原子写（不留 .tmp）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.rss_cache import CACHE_MODES, RssCache, RssCacheEntry

pytestmark = pytest.mark.unit


def _entry(url: str = "https://feed.invalid/rss.xml", *, body: str = "<rss/>") -> RssCacheEntry:
    return RssCacheEntry(
        url=url,
        status=200,
        fetched_at="2026-09-14T00:00:00+00:00",
        body=body,
        etag='W/"abc"',
        last_modified="Mon, 14 Sep 2026 00:00:00 GMT",
    )


def test_modes_are_frozen() -> None:
    assert CACHE_MODES == ("auto", "readonly", "refresh", "off")


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知缓存模式"):
        RssCache(tmp_path, mode="fast")


def test_key_is_deterministic_and_url_only(tmp_path: Path) -> None:
    first = "https://a.invalid/f.xml"
    assert RssCache.key_for(first) == RssCache.key_for(f" {first} ")
    assert RssCache.key_for(first) != RssCache.key_for("https://b.invalid/f.xml")
    assert len(RssCache.key_for(first)) == 64


def test_auto_roundtrip_and_path_layout(tmp_path: Path) -> None:
    cache = RssCache(tmp_path, mode="auto")
    entry = _entry()
    written = cache.store(entry)
    assert written is not None
    assert written.parent.name == RssCache.key_for(entry.url)[:2]
    assert not list(tmp_path.rglob("*.tmp"))  # 原子写不留临时文件

    loaded = cache.load(RssCache.key_for(entry.url))
    assert loaded == entry
    assert loaded is not None and loaded.ok
    assert cache.stats() == {
        "mode": "auto",
        "directory": str(tmp_path),
        "hits": 1,
        "misses": 0,
        "writes": 1,
        "hit_rate": 1.0,
    }


def test_readonly_never_writes_and_replays(tmp_path: Path) -> None:
    writer = RssCache(tmp_path, mode="auto")
    writer.store(_entry())
    reader = RssCache(tmp_path, mode="readonly")
    assert reader.load(RssCache.key_for("https://feed.invalid/rss.xml")) is not None
    assert reader.store(_entry()) is None  # 只读：不写
    assert reader.writes == 0
    assert writer.stats()["writes"] == 1


def test_readonly_miss_returns_none(tmp_path: Path) -> None:
    cache = RssCache(tmp_path, mode="readonly")
    assert cache.load(RssCache.key_for("https://feed.invalid/rss.xml")) is None
    assert cache.misses == 1


def test_refresh_and_off_ignore_existing_cache(tmp_path: Path) -> None:
    RssCache(tmp_path, mode="auto").store(_entry())
    key = RssCache.key_for("https://feed.invalid/rss.xml")
    assert RssCache(tmp_path, mode="refresh").load(key) is None
    assert RssCache(tmp_path, mode="off").load(key) is None
    assert RssCache(tmp_path, mode="off").store(_entry()) is None


def test_corrupted_file_is_a_miss_not_an_error(tmp_path: Path) -> None:
    cache = RssCache(tmp_path, mode="auto")
    path = cache.path_for(cache.key_for("https://feed.invalid/rss.xml"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cache.load(RssCache.key_for("https://feed.invalid/rss.xml")) is None
    path.write_text(json.dumps(["list", "not", "object"]), encoding="utf-8")
    assert cache.load(cache.key_for("https://feed.invalid/rss.xml")) is None
    assert cache.hits == 0


def test_failure_entries_are_cached_too(tmp_path: Path) -> None:
    cache = RssCache(tmp_path, mode="auto")
    failed = RssCacheEntry(
        url="https://feed.invalid/rss.xml",
        status=503,
        fetched_at=RssCache.now_iso(),
        error="HTTP 503（响应非 2xx）",
    )
    cache.store(failed)
    loaded = cache.load(RssCache.key_for(failed.url))
    assert loaded is not None and not loaded.ok
    assert loaded.error.startswith("HTTP 503")


def test_payload_roundtrip_tolerates_missing_fields() -> None:
    entry = RssCacheEntry.from_payload({"url": "https://a.invalid/f.xml"})
    assert entry.status == 0 and entry.body == "" and not entry.ok
    assert entry.to_payload()["url"] == "https://a.invalid/f.xml"
