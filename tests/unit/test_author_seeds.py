"""作者种子与 CSV 解析的单元测试（不依赖数据库，秒级反馈）。

覆盖：种子定义合法性（含"不采集微博"的团队裁决）、CSV 解析的成功与各类问题行。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from database.models.enums import AuthorStatus, SourceType
from database.seeds.authors import (
    AUTHOR_SEEDS,
    CSV_COLUMNS,
    REQUIRED_COLUMNS,
    AuthorSeed,
    load_author_seeds_from_csv,
)
from database.seeds.sources import SOURCE_SEEDS

pytestmark = pytest.mark.unit

SOURCE_TYPES = {seed.name: seed.source_type for seed in SOURCE_SEEDS}


# ---------------------------------------------------------------------------
# 1) 内置种子定义
# ---------------------------------------------------------------------------
def test_author_seeds_are_not_empty() -> None:
    assert len(AUTHOR_SEEDS) >= 1, "至少需要一条内置作者定义用于本地演练"


def test_author_canonical_names_are_unique() -> None:
    names = [seed.canonical_name for seed in AUTHOR_SEEDS if seed.canonical_name]
    assert len(names) == len(set(names)), "authors.canonical_name 是自然键，不能重复"


def test_author_accounts_are_unique_per_source() -> None:
    keys = [(seed.source_name, seed.external_account_id) for seed in AUTHOR_SEEDS]
    assert len(keys) == len(set(keys)), (
        "author_accounts 自然键 (source_id, external_account_id) 重复"
    )


@pytest.mark.parametrize("seed", AUTHOR_SEEDS, ids=lambda s: s.canonical_name or s.display_name)
def test_author_seed_fields_are_valid(seed: AuthorSeed) -> None:
    """04 §3/§4：显示名 / 平台 / 外部账号 ID 必填，状态必须是文档枚举。"""
    assert seed.display_name.strip()
    assert seed.external_account_id.strip()
    assert isinstance(seed.status, AuthorStatus)
    assert seed.source_name in SOURCE_TYPES, f"来源 {seed.source_name!r} 未在 SOURCE_SEEDS 中定义"

def test_author_seeds_never_target_weibo() -> None:
    """★ 团队裁决：先不采集微博；内置作者只能挂在非 WEIBO 来源上。"""
    offending = [
        seed.source_name
        for seed in AUTHOR_SEEDS
        if SOURCE_TYPES.get(seed.source_name) is SourceType.WEIBO
    ]
    assert offending == []


# ---------------------------------------------------------------------------
# 2) CSV 解析（纯函数）
# ---------------------------------------------------------------------------
def _write_csv(
    path: Path, rows: list[dict[str, str]], *, columns: tuple[str, ...] = CSV_COLUMNS
) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _row(**overrides: str) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    row.update(
        {
            "display_name": "某机构首席分析师",
            "canonical_name": "analyst-x",
            "status": "ACTIVE",
            "source_name": "jin10_flash",
            "external_account_id": "analyst-x-rss",
            "account_name": "机构首席",
            "enabled": "true",
            "verified": "false",
            "follower_count": "12000",
            "profile_url": "https://example.invalid/analyst-x",
            "metadata_json": '{"kind": "analyst"}',
        }
    )
    row.update(overrides)
    return row


def test_required_columns_are_documented() -> None:
    assert set(REQUIRED_COLUMNS) <= set(CSV_COLUMNS)


def test_csv_parses_valid_row(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "authors.csv", [_row()])

    seeds, problems = load_author_seeds_from_csv(path)

    assert problems == []
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.display_name == "某机构首席分析师"
    assert seed.canonical_name == "analyst-x"
    assert seed.status is AuthorStatus.ACTIVE
    assert seed.enabled is True
    assert seed.verified is False
    assert seed.follower_count == 12000
    assert seed.metadata_json == {"kind": "analyst"}


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "Y"])
def test_csv_truthy_variants(tmp_path: Path, raw: str) -> None:
    path = _write_csv(tmp_path / "authors.csv", [_row(enabled=raw)])

    seeds, _problems = load_author_seeds_from_csv(path)

    assert seeds[0].enabled is True


def test_csv_defaults_when_optional_fields_are_empty(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "authors.csv",
        [
            _row(
                canonical_name="",
                status="",
                enabled="",
                verified="",
                follower_count="",
                metadata_json="",
            )
        ],
    )

    seeds, problems = load_author_seeds_from_csv(path)

    assert problems == []
    seed = seeds[0]
    assert seed.canonical_name is None
    assert seed.status is AuthorStatus.ACTIVE
    assert seed.enabled is True
    assert seed.verified is False
    assert seed.follower_count is None
    assert seed.metadata_json == {}


def test_csv_rejects_missing_required_column(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "authors.csv",
        [{"display_name": "x", "source_name": "jin10_flash"}],
        columns=("display_name", "source_name"),
    )

    with pytest.raises(ValueError, match="external_account_id"):
        load_author_seeds_from_csv(path)


def test_csv_reports_row_level_problems(tmp_path: Path) -> None:
    rows = [
        _row(),
        _row(display_name=""),
        _row(status="VIP"),
        _row(follower_count="一万"),
        _row(metadata_json="[1,2,3]"),
    ]
    path = _write_csv(tmp_path / "authors.csv", rows)

    seeds, problems = load_author_seeds_from_csv(path)

    assert len(seeds) == 1
    assert len(problems) == 4
    assert any("不能为空" in item for item in problems)
    assert any("status" in item for item in problems)
    assert any("字段解析失败" in item for item in problems)


def test_csv_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_author_seeds_from_csv(tmp_path / "nope.csv")


def test_csv_parsing_is_deterministic(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "authors.csv", [_row(), _row(display_name="第二个", external_account_id="b")]
    )

    first, _ = load_author_seeds_from_csv(path)
    second, _ = load_author_seeds_from_csv(path)

    assert [item.display_name for item in first] == [item.display_name for item in second]
    assert first == second


def test_csv_metadata_json_is_parsed_as_object(tmp_path: Path) -> None:
    payload = {"kind": "institution", "lang": "zh"}
    path = _write_csv(tmp_path / "authors.csv", [_row(metadata_json=json.dumps(payload))])

    seeds, _problems = load_author_seeds_from_csv(path)

    assert seeds[0].metadata_json == payload