from datetime import UTC, datetime, timedelta

from src.alpha.author_input import validate_author_input


def _row(index: int, *, author: str = "作者A") -> dict[str, str]:
    published = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    collected = published + timedelta(minutes=2)
    return {
        "id": f"post-{index}",
        "source": "manual-source",
        "author_name": author,
        "external_account_id": "account-a",
        "content": f"第{index}条真实黄金观点，" + "正文用于验证独立样本与时间因果。" * 6,
        "published_at": published.isoformat(),
        "collected_at": collected.isoformat(),
        "effective_at": collected.isoformat(),
        "url": f"https://example.invalid/{index}",
        "has_media": "false",
        "source_type": "NEWS",
        "collection_time_provenance": "independent_observation",
    }


def test_thirty_causal_unique_rows_are_ready() -> None:
    result = validate_author_input(
        [_row(index) for index in range(30)], now=datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert result.ready
    assert result.author_counts == (("作者A", 30),)
    assert not result.errors


def test_rejects_fallback_time_duplicate_and_short_sample() -> None:
    rows = [_row(0), _row(1)]
    rows[1]["content"] = rows[0]["content"]
    rows[1]["collection_time_provenance"] = "input_effective_at_fallback"
    rows[1]["effective_at"] = rows[1]["published_at"]
    result = validate_author_input(rows, now=datetime(2026, 2, 1, tzinfo=UTC))
    assert not result.ready
    assert any("正文与前文重复" in item for item in result.errors)
    assert any("independent_observation" in item for item in result.errors)
    assert result.warnings


def test_rejects_naive_or_future_times() -> None:
    row = _row(0)
    row["published_at"] = "2026-01-01 00:00:00"
    row["collected_at"] = "2027-01-01T00:00:00Z"
    row["effective_at"] = "2027-01-01T00:00:00Z"
    result = validate_author_input([row], now=datetime(2026, 2, 1, tzinfo=UTC))
    assert any("时间不可解析" in item for item in result.errors)
