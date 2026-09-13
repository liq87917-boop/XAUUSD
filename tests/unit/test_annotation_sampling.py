"""标注抽样脚本单元测试（纯逻辑 + CSV 模式；不依赖数据库、不联网）。

覆盖团队要求：**随机种子**（可复现）、**分层抽样**（长度分档 / 来源上限 / 媒体配额）、
**去重**、**导出 CSV**（判定列必须留空）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.generate_mock_posts import MOCK_BATCH, generate_mock_posts, write_posts_csv
from scripts.sample_annotation_set import (
    CSV_COLUMNS,
    PREFILLED_COLUMNS,
    Candidate,
    deduplicate,
    detect_mock_provenance,
    load_candidates_from_csv,
    main,
    sample_candidates,
    write_annotation_csv,
    write_metadata,
)
from src.common.hashing import content_hash

pytestmark = pytest.mark.unit


def _candidate(
    index: int,
    *,
    source: str = "src-a",
    text_length: int = 30,
    media: bool = False,
    day: int = 2,
    published: bool = True,
    text: str | None = None,
) -> Candidate:
    effective_at = datetime(2024, 1, day, 8, 0, tzinfo=UTC) + timedelta(minutes=index)
    body = text if text is not None else f"样本{index:04d}" + "字" * text_length
    return Candidate(
        post_id=f"post-{index:04d}",
        raw_item_id=f"raw-{index:04d}",
        author_id="author-1",
        author_name="作者甲",
        source_name=source,
        published_at=effective_at if published else None,
        effective_at=effective_at,
        text_content=body,
        has_media=media,
        content_hash=content_hash(body),
    )


# ---------------------------------------------------------------------------
# 1) 候选样本的派生属性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("length", "expected"),
    [(1, "short"), (50, "short"), (51, "medium"), (200, "medium"), (201, "long"), (900, "long")],
)
def test_length_bucket_boundaries(length: int, expected: str) -> None:
    assert _candidate(1, text="字" * length).length_bucket == expected


def test_time_precision_reflects_published_at() -> None:
    assert _candidate(1, published=True).time_precision == "published"
    assert _candidate(2, published=False).time_precision == "collected_only"


def test_trading_day_uses_effective_at() -> None:
    assert _candidate(1, day=5).trading_day == "2024-01-05"


# ---------------------------------------------------------------------------
# 2) 去重
# ---------------------------------------------------------------------------
def test_dedup_keeps_earliest_and_counts_removed() -> None:
    earliest = _candidate(1, text="黄金看多，目标 2450")
    later = _candidate(2, text="黄金看多，目标 2450")
    latest = _candidate(3, text="黄金看多，目标 2450")

    kept, duplicates_removed, empty_removed = deduplicate([latest, earliest, later])

    assert [item.post_id for item in kept] == [earliest.post_id]
    assert duplicates_removed == 2
    assert empty_removed == 0


def test_dedup_drops_empty_text() -> None:
    kept, duplicates_removed, empty_removed = deduplicate(
        [_candidate(1, text="   "), _candidate(2, text="黄金看空")]
    )

    assert len(kept) == 1
    assert duplicates_removed == 0
    assert empty_removed == 1


def test_dedup_uses_normalized_text_when_hash_absent() -> None:
    """哈希列缺失时用规范化文本兜底（大小写 / 首尾空白差异视为同一条）。"""
    plain = replace(_candidate(1, text="Gold long"), content_hash="")
    spaced = replace(_candidate(2, text="  gold LONG "), content_hash="")

    kept, duplicates_removed, _empty = deduplicate([plain, spaced])

    assert len(kept) == 1
    assert duplicates_removed == 1


# ---------------------------------------------------------------------------
# 3) 分层抽样
# ---------------------------------------------------------------------------
def test_sampling_is_deterministic_for_same_seed() -> None:
    pool = [
        _candidate(index, source=f"src-{index % 3}", day=(index % 6) + 1) for index in range(60)
    ]

    first = sample_candidates(pool, limit=20, seed=1234)
    second = sample_candidates(pool, limit=20, seed=1234)

    assert [item.post_id for item in first.selected] == [item.post_id for item in second.selected]


def test_different_seed_changes_selection() -> None:
    pool = [
        _candidate(index, source=f"src-{index % 4}", day=(index % 6) + 1) for index in range(80)
    ]

    first = sample_candidates(pool, limit=20, seed=1)
    second = sample_candidates(pool, limit=20, seed=2)

    assert [item.post_id for item in first.selected] != [item.post_id for item in second.selected]


def test_sampling_respects_length_bucket_targets() -> None:
    pool = (
        [_candidate(index, text_length=20, day=2) for index in range(30)]
        + [_candidate(100 + index, text_length=100, day=3) for index in range(30)]
        + [_candidate(200 + index, text_length=300, day=4) for index in range(30)]
    )

    report = sample_candidates(pool, limit=30, seed=7)

    assert report.selected_count == 30
    assert report.bucket_counts == {"short": 10, "medium": 10, "long": 10}
    assert report.bucket_targets == {"short": 10, "medium": 10, "long": 10}


def test_sampling_respects_source_cap() -> None:
    pool = [
        _candidate(index, source=f"src-{index % 5}", day=(index % 5) + 1) for index in range(100)
    ]

    report = sample_candidates(pool, limit=50, seed=11)

    assert report.selected_count == 50
    assert report.source_cap_respected is True
    assert max(report.source_counts.values()) <= report.source_cap


def test_sampling_fills_media_quota_when_available() -> None:
    pool = [_candidate(index, media=index < 12, day=(index % 5) + 1) for index in range(60)]

    report = sample_candidates(pool, limit=30, seed=3, media_quota=10)

    assert report.media_selected == 10
    assert report.media_quota_met is True


def test_sampling_relaxes_source_cap_and_records_note() -> None:
    """单一来源的样本池无法满足 40% 上限时必须显式记录，而不是静默降级。"""
    pool = [_candidate(index, source="only-source", day=(index % 5) + 1) for index in range(40)]

    report = sample_candidates(pool, limit=20, seed=5)

    assert report.selected_count == 20
    assert report.source_cap_respected is False
    assert any("放宽单一来源" in note for note in report.notes)


def test_sampling_reports_shortfall() -> None:
    report = sample_candidates([_candidate(index) for index in range(5)], limit=50, seed=5)

    assert report.selected_count == 5
    assert any("候选样本不足" in note for note in report.notes)


def test_sampling_reports_day_coverage() -> None:
    pool = [_candidate(index, day=(index % 3) + 1) for index in range(30)]

    report = sample_candidates(pool, limit=30, seed=9)

    assert report.distinct_days == 3
    assert any("交易日" in note for note in report.notes)


def test_sampling_reports_dedup_and_empty_stats() -> None:
    pool = [
        _candidate(1, text="黄金看多"),
        _candidate(2, text="黄金看多"),
        _candidate(3, text="   "),
        _candidate(4, text="黄金看空"),
    ]

    report = sample_candidates(pool, limit=10, seed=1)

    assert report.candidates_total == 4
    assert report.duplicates_removed == 1
    assert report.empty_text_removed == 1
    assert report.selected_count == 2


# ---------------------------------------------------------------------------
# 4) 导出（列定义与"标注列留空"的硬约束）
# ---------------------------------------------------------------------------
def test_csv_columns_match_documented_spec() -> None:
    """列定义对齐 docs/10 附录 A（另有 author_name / spec_version 两个补充列）。"""
    assert CSV_COLUMNS == (
        "annotation_id",
        "post_id",
        "raw_item_id",
        "author_id",
        "author_name",
        "source_name",
        "published_at",
        "effective_at",
        "text_content",
        "has_media",
        "no_opinion",
        "opinion_index",
        "stance",
        "instrument",
        "horizon",
        "confidence",
        "entry_low",
        "entry_high",
        "stop_loss",
        "take_profit",
        "information_type",
        "rationale",
        "leakage_suspect",
        "time_precision",
        "annotation_version",
        "annotated_by",
        "annotated_at",
        "notes",
        "spec_version",
    )


def test_annotation_row_leaves_judgement_columns_empty() -> None:
    """★ 绝不代替人工标注：所有判定列必须是空字符串。"""
    from scripts.sample_annotation_set import annotation_row

    row = annotation_row(_candidate(1), spec_version="spec-v1.0")

    for column in CSV_COLUMNS:
        if column in PREFILLED_COLUMNS:
            assert row[column] != "" or column in {"published_at"}, column
        else:
            assert row[column] == "", column


def test_annotation_row_prefills_objective_columns() -> None:
    from scripts.sample_annotation_set import annotation_row

    candidate = _candidate(1, source="jin10_flash", day=3)
    row = annotation_row(candidate, spec_version="spec-v1.0")

    assert row["source_name"] == "jin10_flash"
    assert row["effective_at"].startswith("2024-01-03")
    assert row["text_content"] == candidate.text_content
    assert row["has_media"] == "false"
    assert row["opinion_index"] == "1"
    assert row["time_precision"] == "published"
    assert row["spec_version"] == "spec-v1.0"


def test_write_annotation_csv_writes_header_and_rows(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"

    written = write_annotation_csv(path, [_candidate(1), _candidate(2)], spec_version="spec-v1.0")

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM：Excel 双击中文不乱码
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    assert written == 2
    assert reader.fieldnames == list(CSV_COLUMNS)
    assert len(rows) == 2
    assert all(row["stance"] == "" and row["horizon"] == "" for row in rows)


def test_metadata_json_is_written_and_parseable(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"

    write_metadata(path, {"seed": 20260912, "rows_written": 3})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"rows_written": 3, "seed": 20260912}


# ---------------------------------------------------------------------------
# 5) CSV 模式（作者库未导入时的演练路径）
# ---------------------------------------------------------------------------
POST_COLUMNS = (
    "post_id",
    "author_name",
    "source_name",
    "published_at",
    "effective_at",
    "text_content",
    "has_media",
    "content_hash",
)


def _write_posts_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(POST_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _post_row(
    index: int, *, text: str | None = None, published: str = "2024-01-02T08:00:00+00:00"
) -> dict[str, str]:
    return {
        "post_id": f"csv-post-{index:03d}",
        "author_name": f"机构{index % 3}",
        "source_name": f"src-{index % 3}",
        "published_at": published,
        "effective_at": published,
        "text_content": text if text is not None else f"黄金看多 {index}，目标 2450",
        "has_media": "false",
        "content_hash": "",
    }


def test_load_candidates_from_csv_parses_rows(tmp_path: Path) -> None:
    path = _write_posts_csv(tmp_path / "posts.csv", [_post_row(1), _post_row(2)])

    candidates, problems = load_candidates_from_csv(path)

    assert problems == []
    assert len(candidates) == 2
    assert candidates[0].source_name == "src-1"
    assert candidates[0].effective_at.tzinfo is not None


def test_load_candidates_from_csv_reports_problems(tmp_path: Path) -> None:
    rows = [
        _post_row(1),
        {**_post_row(2), "text_content": "   "},
        {**_post_row(3), "effective_at": "不是时间", "published_at": ""},
    ]
    path = _write_posts_csv(tmp_path / "posts.csv", rows)

    candidates, problems = load_candidates_from_csv(path)

    assert len(candidates) == 1
    assert len(problems) == 2
    assert any("为空" in problem for problem in problems)
    assert any("时间无法解析" in problem for problem in problems)


def test_load_candidates_from_csv_requires_text_column(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="text_content"):
        load_candidates_from_csv(path)


def test_load_candidates_from_csv_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_candidates_from_csv(tmp_path / "nope.csv")


def test_csv_mode_generates_deterministic_ids(tmp_path: Path) -> None:
    """重复读取同一文件必须得到相同 id（否则标注结果无法与数据对齐）。"""
    path = _write_posts_csv(tmp_path / "posts.csv", [_post_row(1), _post_row(2)])

    first, _ = load_candidates_from_csv(path)
    second, _ = load_candidates_from_csv(path)

    assert [item.post_id for item in first] == [item.post_id for item in second]
    assert [item.raw_item_id for item in first] == [item.raw_item_id for item in second]
    assert [item.author_id for item in first] == [item.author_id for item in second]


def test_cli_csv_mode_end_to_end(tmp_path: Path, capsys) -> None:
    source = _write_posts_csv(
        tmp_path / "posts.csv", [_post_row(index) for index in range(1, 21)]
    )
    out = tmp_path / "sample.csv"

    exit_code = main(
        ["--input-csv", str(source), "--out", str(out), "--limit", "8", "--media-quota", "0"]
    )

    assert exit_code == 0
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))

    assert len(rows) == 8
    assert meta["seed"] == 20260912
    assert meta["rows_written"] == 8
    assert meta["source"] == f"csv:{source}"
    assert "stance" in meta["annotation_columns_left_empty"]
    assert capsys.readouterr().out  # 控制台给出人读摘要


def test_cli_require_full_returns_3(tmp_path: Path) -> None:
    source = _write_posts_csv(tmp_path / "posts.csv", [_post_row(index) for index in range(1, 4)])

    exit_code = main(
        [
            "--input-csv",
            str(source),
            "--out",
            str(tmp_path / "sample.csv"),
            "--limit",
            "10",
            "--require-full",
        ]
    )

    assert exit_code == 3


def test_cli_returns_2_when_no_candidates(tmp_path: Path) -> None:
    source = _write_posts_csv(tmp_path / "empty.csv", [])

    exit_code = main(["--input-csv", str(source), "--out", str(tmp_path / "sample.csv")])

    assert exit_code == 2


# ---------------------------------------------------------------------------
# 6) 列别名 + "输入缺失自动生成 Mock"（`logs/posts.csv` 演练路径）
# ---------------------------------------------------------------------------
ALIAS_COLUMNS = ("id", "source", "content", "published_at", "effective_at")


def _write_alias_posts_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALIAS_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _alias_row(index: int) -> dict[str, str]:
    return {
        "id": f"post-{index}",
        "source": "jin10_flash",
        "content": f"黄金做多，目标 {2400 + index}，止损 2380",
        "published_at": "2024-01-02T08:00:00+00:00",
        "effective_at": f"2024-01-02T08:{index:02d}:00+00:00",
    }


def test_load_candidates_from_csv_accepts_minimal_alias_columns(tmp_path: Path) -> None:
    """最小可用输入 = `id,source,content,published_at,effective_at`（人工手搓也能用）。"""
    path = _write_alias_posts_csv(tmp_path / "min.csv", [_alias_row(1)])

    candidates, problems = load_candidates_from_csv(path)

    assert problems == []
    assert len(candidates) == 1
    assert candidates[0].post_id == "post-1"
    assert candidates[0].source_name == "jin10_flash"
    assert candidates[0].text_content == "黄金做多，目标 2401，止损 2380"
    assert candidates[0].time_precision == "published"


def test_load_candidates_from_csv_is_case_insensitive(tmp_path: Path) -> None:
    """Excel 手工导出的列名常见 `Content` / `Source`，必须照样能读。"""
    path = tmp_path / "upper.csv"
    path.write_text(
        "Id,Source,Content,Published_At,Effective_At\n"
        "p1,jin10_flash,黄金做空，目标 2300，止损 2320,"
        "2024-01-02T08:00:00+00:00,2024-01-02T08:01:00+00:00\n",
        encoding="utf-8",
    )

    candidates, problems = load_candidates_from_csv(path)

    assert problems == []
    assert candidates[0].post_id == "p1"
    assert candidates[0].source_name == "jin10_flash"


def test_load_candidates_from_csv_prefers_canonical_over_alias(tmp_path: Path) -> None:
    """同时出现 `content` 与 `text_content` 时，以规范列 `text_content` 为准。"""
    path = tmp_path / "both.csv"
    path.write_text(
        "id,content,text_content,effective_at\n"
        "p1,别名文本,规范文本,2024-01-02T08:00:00+00:00\n",
        encoding="utf-8",
    )

    candidates, problems = load_candidates_from_csv(path)

    assert problems == []
    assert candidates[0].text_content == "规范文本"


def test_cli_generates_mock_posts_when_input_missing(tmp_path: Path, capsys) -> None:
    """`--input-csv` 指向的文件不存在 → 自动生成 Mock 演练数据，并醒目警告 + 元数据留痕。"""
    source = tmp_path / "logs" / "posts.csv"
    out = tmp_path / "sample.csv"

    exit_code = main(
        [
            "--input-csv",
            str(source),
            "--out",
            str(out),
            "--limit",
            "40",
            "--mock-fill",
            "40",
            "--media-quota",
            "0",
            "--require-full",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert source.exists()
    assert "合成 Mock 演练数据" in captured.err
    assert MOCK_BATCH in captured.err
    assert "不能作为研究或验收结论" in captured.out
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert len(rows) == 40
    assert meta["mock_filled"] is True
    assert meta["mock_batch"] == MOCK_BATCH
    assert meta["mock_rows_written"] == 40
    assert meta["input_csv"] == str(source)
    assert meta["source"] == f"csv:{source}"
    # 红线：无论数据从哪来，判定列一律留空（脚本绝不代替人工标注）
    judgement_columns = set(CSV_COLUMNS) - PREFILLED_COLUMNS
    assert judgement_columns  # 判定列集合非空
    for row in rows:
        assert all(row[column] == "" for column in judgement_columns)


def test_cli_marks_mock_fill_off_by_default_db_metadata(tmp_path: Path) -> None:
    """真实输入（文件已存在）时不得留下 `mock_filled` 的假痕迹。"""
    source = _write_alias_posts_csv(
        tmp_path / "posts.csv", [_alias_row(index) for index in range(1, 5)]
    )
    out = tmp_path / "sample.csv"

    exit_code = main(
        ["--input-csv", str(source), "--out", str(out), "--limit", "4", "--media-quota", "0"]
    )

    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert meta["mock_filled"] is False
    assert meta["mock_batch"] is None
    assert meta["mock_rows_written"] == 0


def test_cli_never_overwrites_existing_input_csv(tmp_path: Path) -> None:
    """已存在的输入 CSV 绝不被覆盖（它可能已经是真实导出数据）。"""
    source = _write_alias_posts_csv(
        tmp_path / "posts.csv", [_alias_row(index) for index in range(1, 4)]
    )
    before = source.read_text(encoding="utf-8")

    exit_code = main(
        [
            "--input-csv",
            str(source),
            "--out",
            str(tmp_path / "sample.csv"),
            "--limit",
            "3",
            "--media-quota",
            "0",
        ]
    )

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == before


def test_cli_without_mock_fill_raises_when_input_missing(tmp_path: Path) -> None:
    """`--no-mock-fill`：保持"缺输入即报错"的严格行为（不偷偷造数据）。"""
    with pytest.raises(FileNotFoundError):
        main(
            [
                "--input-csv",
                str(tmp_path / "missing.csv"),
                "--out",
                str(tmp_path / "sample.csv"),
                "--no-mock-fill",
            ]
        )


# ---------------------------------------------------------------------------
# 7) 输入来源体检（合成数据不得静默进入标注/验收）
# ---------------------------------------------------------------------------
def test_detect_mock_provenance_reports_mock_batch(tmp_path: Path) -> None:
    path = tmp_path / "posts.csv"
    write_posts_csv(path, generate_mock_posts(count=20))

    provenance = detect_mock_provenance(path)
    metadata = provenance.to_metadata()

    assert provenance.is_mock is True
    assert provenance.mock_rows == 20
    assert provenance.rows_scanned == 20
    assert provenance.mock_batch == MOCK_BATCH
    assert metadata == {
        "input_rows_scanned": 20,
        "input_is_mock": True,
        "input_mock_rows": 20,
        "input_mock_batch": MOCK_BATCH,
    }


def test_detect_mock_provenance_without_marker_columns(tmp_path: Path) -> None:
    """真实数据（没有 `is_mock` / `mock_batch` 列）→ 干净返回，不做任何猜测。"""
    path = _write_alias_posts_csv(tmp_path / "plain.csv", [_alias_row(1), _alias_row(2)])

    provenance = detect_mock_provenance(path)

    assert provenance.is_mock is False
    assert provenance.rows_scanned == 0
    assert provenance.to_metadata()["input_mock_batch"] is None


def test_detect_mock_provenance_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        detect_mock_provenance(tmp_path / "nope.csv")


def test_cli_warns_when_existing_input_is_mock(tmp_path: Path, capsys) -> None:
    """输入 CSV **已存在**但带合成标记 → 仍必须警告并把来源写进元数据（不静默放行）。"""
    source = tmp_path / "posts.csv"
    write_posts_csv(source, generate_mock_posts(count=40))
    out = tmp_path / "sample.csv"

    exit_code = main(
        [
            "--input-csv",
            str(source),
            "--out",
            str(out),
            "--limit",
            "40",
            "--media-quota",
            "0",
            "--require-full",
        ]
    )

    captured = capsys.readouterr()
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "自带合成标记" in captured.out
    assert meta["mock_filled"] is False  # 本次没有生成，输入本来就存在
    assert meta["input_is_mock"] is True
    assert meta["input_mock_rows"] == 40
    assert meta["input_mock_batch"] == MOCK_BATCH