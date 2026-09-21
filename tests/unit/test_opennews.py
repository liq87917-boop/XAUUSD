"""OpenNewsCollector 解析纯函数单元测试（无数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.collectors.errors import CollectorError
from src.collectors.opennews import _parse_published_at, parse_news_search


def _entry(**overrides) -> dict:
    base = {
        "text": "Gold rallies on Fed outlook",
        "description": "Gold prices rose...",
        "ts": "2026-09-15T10:00:00Z",
        "source": "test-feed",
        "link": "https://example.invalid/gold",
        "engineType": "news",
    }
    base.update(overrides)
    return base


def test_parse_news_search_normal() -> None:
    items = parse_news_search({"data": [_entry()]})
    assert len(items) == 1
    assert items[0].title == "Gold rallies on Fed outlook"
    assert items[0].published_at == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert items[0].source_name == "test-feed"
    assert items[0].url == "https://example.invalid/gold"


def test_parse_news_search_empty_data() -> None:
    assert parse_news_search({"data": []}) == ()


def test_parse_news_search_missing_data_raises() -> None:
    with pytest.raises(CollectorError, match="缺少 data"):
        parse_news_search({"error": "no data"})


def test_parse_news_search_skips_empty_title() -> None:
    assert parse_news_search({"data": [_entry(text="")]}) == ()


def test_parse_news_search_filters_non_news_engine_types() -> None:
    """market / onchain 等类型不进入 news_events。"""
    items = parse_news_search(
        {
            "data": [
                _entry(),
                _entry(engineType="market", text="BTC funding rate"),
                _entry(engineType="onchain"),
            ]
        }
    )
    assert len(items) == 1
    assert items[0].title == "Gold rallies on Fed outlook"


def test_parse_published_at_variants() -> None:
    assert _parse_published_at(None) is None
    assert _parse_published_at("") is None
    assert _parse_published_at("2026-09-15T10:00:00+00:00") == datetime(
        2026, 9, 15, 10, 0, tzinfo=UTC
    )
    epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=UTC).timestamp())
    assert _parse_published_at(epoch) == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert _parse_published_at("garbage") is None
