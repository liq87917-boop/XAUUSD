import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = [
    "id",
    "source",
    "author_name",
    "external_account_id",
    "content",
    "published_at",
    "collected_at",
    "effective_at",
    "url",
    "has_media",
    "source_type",
    "collection_time_provenance",
]


def test_author_template_is_header_only_and_requires_independent_collection_time() -> None:
    path = ROOT / "templates" / "phase3_3" / "author_posts_template.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [EXPECTED_COLUMNS]


def test_handoff_forbids_historical_news_backfill_leakage() -> None:
    guide = (
        ROOT / "docs" / "operations" / "Phase3_3_用户数据交接说明.md"
    ).read_text(encoding="utf-8")
    assert "不能倒填成历史可见" in guide
    assert "effective_at >= collected_at" in guide
    assert "不得把 `collected_at` 从 `published_at` 复制" in guide
