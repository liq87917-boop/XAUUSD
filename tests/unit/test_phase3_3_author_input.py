from datetime import UTC, datetime, timedelta

import pytest

from src.alpha.author_input import validate_author_input

AUTHORIZED = {("manual-source", "account-a"): (datetime(2026, 1, 1, tzinfo=UTC), None)}


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
        [_row(index) for index in range(30)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows=AUTHORIZED,
    )
    assert result.ready
    assert result.author_counts == (("作者A [manual-source/account-a]", 30),)
    assert not result.errors


def test_rejects_fallback_time_duplicate_and_short_sample() -> None:
    rows = [_row(0), _row(1)]
    rows[1]["content"] = rows[0]["content"]
    rows[1]["url"] = rows[0]["url"]
    rows[1]["collection_time_provenance"] = "input_effective_at_fallback"
    rows[1]["effective_at"] = rows[1]["published_at"]
    result = validate_author_input(
        rows, now=datetime(2026, 2, 1, tzinfo=UTC), authorization_windows=AUTHORIZED
    )
    assert not result.ready
    assert any("正文与前文重复" in item for item in result.errors)
    assert any("url 与前文重复" in item for item in result.errors)
    assert any("independent_observation" in item for item in result.errors)
    assert result.warnings


def test_rejects_naive_or_future_times() -> None:
    row = _row(0)
    row["published_at"] = "2026-01-01 00:00:00"
    row["collected_at"] = "2027-01-01T00:00:00Z"
    row["effective_at"] = "2027-01-01T00:00:00Z"
    result = validate_author_input(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), authorization_windows=AUTHORIZED
    )
    assert any("时间不可解析" in item for item in result.errors)


def test_same_display_name_cannot_merge_different_accounts() -> None:
    rows = [_row(index) for index in range(15)]
    rows.extend(
        {
            **_row(index + 15),
            "source": "another-source",
            "external_account_id": "account-b",
        }
        for index in range(15)
    )
    result = validate_author_input(
        rows,
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows={
            **AUTHORIZED,
            ("another-source", "account-b"): (datetime(2026, 1, 1, tzinfo=UTC), None),
        },
    )
    assert not result.ready
    assert result.author_counts == (
        ("作者A [another-source/account-b]", 15),
        ("作者A [manual-source/account-a]", 15),
    )
    assert len(result.warnings) == 2


def test_same_account_must_have_consistent_author_name() -> None:
    rows = [_row(index) for index in range(30)]
    rows[-1]["author_name"] = "作者A（改名）"
    result = validate_author_input(
        rows, now=datetime(2026, 2, 1, tzinfo=UTC), authorization_windows=AUTHORIZED
    )
    assert not result.ready
    assert any("作者名不一致" in item for item in result.errors)


def test_rejects_copied_collection_time_and_unverifiable_url() -> None:
    row = _row(0)
    row["collected_at"] = row["published_at"]
    row["effective_at"] = row["published_at"]
    row["url"] = "http://[malformed"
    result = validate_author_input(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), authorization_windows=AUTHORIZED
    )
    assert not result.ready
    assert any("不能复制发布时间" in item for item in result.errors)
    assert any("http/https" in item for item in result.errors)


def test_rejects_near_future_collection_time() -> None:
    row = _row(0)
    row["published_at"] = "2026-01-01T00:00:00Z"
    row["collected_at"] = "2026-01-01T00:01:00Z"
    row["effective_at"] = row["collected_at"]
    result = validate_author_input(
        [row], now=datetime(2026, 1, 1, tzinfo=UTC), authorization_windows=AUTHORIZED
    )
    assert any("未来时间" in error for error in result.errors)


def test_requires_timezone_aware_audit_clock() -> None:
    with pytest.raises(ValueError, match="now 必须包含时区"):
        validate_author_input([_row(0)], now=datetime(2026, 1, 1), authorization_windows=AUTHORIZED)


def test_rejects_structurally_valid_rows_without_account_authorization() -> None:
    result = validate_author_input(
        [_row(index) for index in range(30)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows={},
    )
    assert not result.ready
    assert any("没有当前有效" in item for item in result.errors)


def test_rejects_collection_before_authorization_began() -> None:
    windows = {("manual-source", "account-a"): (datetime(2026, 1, 2, tzinfo=UTC), None)}
    result = validate_author_input(
        [_row(index) for index in range(30)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows=windows,
    )
    assert not result.ready
    assert any("第 1 行 collected_at 不在账号" in error for error in result.errors)


def test_collection_at_authorization_start_is_allowed() -> None:
    windows = {("manual-source", "account-a"): (datetime(2026, 1, 1, 0, 2, tzinfo=UTC), None)}
    result = validate_author_input(
        [_row(index) for index in range(30)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows=windows,
    )
    assert result.ready


def test_rejects_collection_at_authorization_expiry() -> None:
    windows = {
        ("manual-source", "account-a"): (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        )
    }
    result = validate_author_input(
        [_row(index) for index in range(30)],
        now=datetime(2026, 2, 1, tzinfo=UTC),
        authorization_windows=windows,
    )
    assert not result.ready
    assert any("第 1 行 collected_at 不在账号" in error for error in result.errors)
