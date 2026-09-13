"""LLM 缓存模块的单元测试（零网络：只碰 `tmp_path`）。

覆盖重点：
1. **键的确定性**：同输入同键；正文 / `has_media` / **prompt 版本** / 模型任一变化即换键
   （改 Prompt 必须自动失效，绝不能读到旧答案）；
2. **四种模式**：`auto` 读写、`readonly` 只读且未命中报错、`refresh` 忽略旧值、
   `off` 彻底不碰磁盘；
3. **原子写入**：落盘后目录里只留正式文件（没有 `.tmp` 残留）；
4. **健壮性**：文件损坏 / 缺字段 → 视为未命中而不是崩溃；
5. **失败也缓存**：错误条目能被读回（避免同一坏输入反复烧钱）；
6. **无密钥**：缓存文件里不允许出现任何 `sk-` 形态的串。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.processors.llm_cache import (
    CACHE_MODES,
    STATUS_ERROR,
    STATUS_OK,
    LLMCache,
    LLMCacheEntry,
    LLMCacheMissError,
    cache_key,
    text_digest,
)

pytestmark = pytest.mark.unit

PROMPT_VERSION = "opinion-prompt-v1"
MODEL = "deepseek-chat"
TEXT = "黄金 2380 做多，止损 2365，目标 2450"


def _key(
    text: str = TEXT,
    *,
    prompt_version: str = PROMPT_VERSION,
    model: str = MODEL,
    has_media: bool = False,
) -> str:
    return cache_key(
        prompt_version=prompt_version, model=model, text=text, has_media=has_media
    )


def _entry(
    key: str,
    *,
    status: str = STATUS_OK,
    raw: str = '{"opinions": []}',
    error: str = "",
) -> LLMCacheEntry:
    return LLMCacheEntry(
        key=key,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        text_sha256=text_digest(TEXT),
        has_media=False,
        created_at=LLMCache.now_iso(),
        status=status,
        raw_content=raw,
        error=error,
        usage={"prompt_tokens": 100, "completion_tokens": 20},
        latency_ms=1234,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------
def test_cache_key_is_deterministic() -> None:
    assert _key() == _key()
    assert len(_key()) == 64  # sha256 hex


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": TEXT + "。"},
        {"has_media": True},
        {"prompt_version": "opinion-prompt-v2"},
        {"model": "deepseek-reasoner"},
    ],
)
def test_cache_key_changes_when_any_input_changes(kwargs: dict[str, object]) -> None:
    assert _key(**kwargs) != _key()  # type: ignore[arg-type]


def test_cache_key_does_not_contain_plaintext() -> None:
    """键里不能出现正文（缓存文件名即键，避免把语料写进路径）。"""
    assert "2380" not in _key()


def test_text_digest_is_short_and_stable() -> None:
    assert text_digest(TEXT) == text_digest(TEXT)
    assert len(text_digest(TEXT)) == 16


# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------
def test_modes_are_frozen_tuple() -> None:
    assert CACHE_MODES == ("auto", "readonly", "refresh", "off")


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LLMCache(tmp_path, mode="wild")


def test_auto_mode_roundtrip(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, mode="auto")
    key = _key()

    assert cache.load(key) is None  # 首次未命中
    cache.store(_entry(key))
    entry = cache.load(key)

    assert entry is not None
    assert entry.ok is True
    assert entry.raw_content == '{"opinions": []}'
    assert entry.usage == {"prompt_tokens": 100, "completion_tokens": 20}
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1
    assert cache.stats()["writes"] == 1
    assert cache.stats()["hit_rate"] == 0.5


def test_readonly_mode_never_writes_and_raises_on_miss(tmp_path: Path) -> None:
    seed = LLMCache(tmp_path, mode="auto")
    seed.store(_entry(_key()))

    cache = LLMCache(tmp_path, mode="readonly")

    assert cache.load(_key()) is not None  # 命中可读
    assert cache.store(_entry("deadbeef" * 8)) is None  # 不写
    with pytest.raises(LLMCacheMissError):
        cache.require(_key(text="这条没跑过"))
    assert cache.stats()["writes"] == 0


def test_refresh_mode_ignores_existing_entry(tmp_path: Path) -> None:
    seed = LLMCache(tmp_path, mode="auto")
    seed.store(_entry(_key()))

    cache = LLMCache(tmp_path, mode="refresh")

    assert cache.load(_key()) is None  # 忽略旧值 → 会重新调 API
    stored = cache.store(_entry(_key(), raw='{"opinions": [{"stance": "LONG"}]}'))
    assert stored is not None
    reread = LLMCache(tmp_path).load(_key())
    assert reread is not None
    assert reread.raw_content.startswith('{"opinions": [{"stance"')


def test_off_mode_does_not_touch_disk(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, mode="off")

    assert cache.load(_key()) is None
    assert cache.store(_entry(_key())) is None
    assert list(tmp_path.rglob("*.json")) == []


# ---------------------------------------------------------------------------
# 健壮性
# ---------------------------------------------------------------------------
def test_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, mode="auto")

    cache.store(_entry(_key()))

    files = [item for item in tmp_path.rglob("*") if item.is_file()]
    assert [item.name for item in files] == [f"{_key()}.json"]
    assert not list(tmp_path.rglob("*.tmp"))


def test_corrupted_cache_file_is_treated_as_miss(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, mode="auto")
    path = cache.path_for(_key())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert cache.load(_key()) is None
    assert cache.stats()["misses"] == 1


def test_non_object_payload_is_treated_as_miss(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, mode="auto")
    path = cache.path_for(_key())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert cache.load(_key()) is None


def test_entry_payload_roundtrip_tolerates_missing_fields() -> None:
    entry = LLMCacheEntry.from_payload({"key": "abc", "status": STATUS_OK})

    assert entry.model == ""
    assert entry.parsed is None
    assert entry.latency_ms == 0
    assert entry.to_payload()["key"] == "abc"


def test_error_entry_is_cached_and_readable(tmp_path: Path) -> None:
    """★ 失败也缓存：同一坏输入不应被反复重试（反复烧钱）。"""
    cache = LLMCache(tmp_path, mode="auto")
    key = _key()

    cache.store(_entry(key, status=STATUS_ERROR, raw="", error="HTTP 500"))

    entry = cache.load(key)
    assert entry is not None
    assert entry.ok is False
    assert entry.error == "HTTP 500"


def test_cache_files_contain_no_api_key(tmp_path: Path) -> None:
    """红线：缓存只存请求 / 响应，绝不存密钥。"""
    cache = LLMCache(tmp_path, mode="auto")
    cache.store(_entry(_key()))

    for path in tmp_path.rglob("*.json"):
        content = path.read_text(encoding="utf-8")
        assert "sk-" not in content
        assert json.loads(content)["key"] == _key()