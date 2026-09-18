from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.alpha.evidence_gate import assess_news_counts


def test_news_gate_requires_volume_history_and_diversity() -> None:
    first = datetime(2025, 1, 1, tzinfo=UTC)
    ready = assess_news_counts(
        {"source_a": 100, "source_b": 100, "source_c": 100},
        first,
        first + timedelta(days=120),
    )
    assert ready.ready

    too_small = assess_news_counts(
        {"source_a": 90, "source_b": 90}, first, first + timedelta(days=120)
    )
    assert not too_small.ready

    concentrated = assess_news_counts(
        {"source_a": 160, "source_b": 40}, first, first + timedelta(days=120)
    )
    assert not concentrated.ready


def test_news_gate_handles_empty_input() -> None:
    result = assess_news_counts({}, None, None)
    assert result.events == 0
    assert result.history_days == 0
    assert result.largest_source_share == 0
    assert not result.ready
