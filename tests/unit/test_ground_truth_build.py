"""金标准合并脚本的单元测试（含对比脚本的 IO / 防覆盖能力）。

覆盖重点：
1. **红线条款**：人工漏填的格子**绝不回填模型值**；`notes` 有理由的留空 = 人工裁决"未给出"；
2. **推翻分类**：采纳多数派 / 采纳少数派 / 三家各异采纳其一 / 三家都错；
3. **真实世界 IO**：`pending_review.csv` 实际是 xlsx（扩展名说谎）、GBK 编码 CSV、BOM；
4. **防覆盖**：对比脚本默认拒绝覆盖已有人工填写的裁决表（退出码 4）；
5. **导出契约**：金标准列、`source` 区分人工/共识、归档副本字节一致、报告含关键数字；
6. 红线：两个脚本都不得触碰数据库。
"""

from __future__ import annotations

import csv
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import openpyxl
import pytest

from scripts.build_ground_truth import (
    CONSENSUS_NOTE,
    GROUND_TRUTH_COLUMNS,
    OVERTURN_MAJORITY,
    OVERTURN_MINORITY,
    OVERTURN_NONE,
    OVERTURN_SINGLE,
    SOURCE_CONSENSUS,
    SOURCE_HUMAN,
    archive_input,
    classify_overturn,
    merge,
    reference_grid,
    render_report,
    write_ground_truth,
)
from scripts.build_ground_truth import (
    main as truth_main,
)
from scripts.compare_model_annotations import (
    main as compare_main,
)
from scripts.compare_model_annotations import (
    pending_has_human_work,
    read_any_table,
    sniff_is_xlsx,
)

pytestmark = pytest.mark.unit

PENDING_HEADERS = (
    "review_id",
    "post_id",
    "field",
    "field_label",
    "model_db",
    "model_qw",
    "model_bd",
    "normalized_db",
    "normalized_qw",
    "normalized_bd",
    "agreeing_models",
    "disagreeing_models",
    "blank_models",
    "text_content",
    "source_name",
    "published_at",
    "effective_at",
    "final_gold_standard",
    "reviewer",
    "reviewed_at",
    "notes",
)
CONSENSUS_HEADERS = (
    "post_id",
    "field",
    "field_label",
    "value_agreed",
    "raw_values",
    "models",
    "text_content",
    "source_name",
    "published_at",
    "effective_at",
    "provenance",
)


def _pending_row(
    review_id: str,
    post_id: str,
    field: str,
    *,
    db: str = "",
    qw: str = "",
    bd: str = "",
    gold: str = "",
    notes: str = "",
    reviewer: str = "tester",
    agreeing: str = "",
    blank: str = "",
) -> dict[str, str]:
    row = {name: "" for name in PENDING_HEADERS}
    row.update(
        {
            "review_id": review_id,
            "post_id": post_id,
            "field": field,
            "model_db": db,
            "model_qw": qw,
            "model_bd": bd,
            "normalized_db": db,
            "normalized_qw": qw,
            "normalized_bd": bd,
            "agreeing_models": agreeing,
            "blank_models": blank,
            "text_content": "黄金 做多，目标 2450，止损 2420",
            "final_gold_standard": gold,
            "notes": notes,
            "reviewer": reviewer,
        }
    )
    return row


def _consensus_row(
    post_id: str, field: str, value: str = "", *, raw: str = ""
) -> dict[str, str]:
    row = {name: "" for name in CONSENSUS_HEADERS}
    row.update(
        {
            "post_id": post_id,
            "field": field,
            "value_agreed": value,
            "raw_values": raw or f"{value} | {value} | {value}",
            "models": "db/qw/bd",
            "text_content": "黄金 做多，目标 2450，止损 2420",
            "provenance": "3-model-consensus (NOT human-verified)",
        }
    )
    return row


def _write_csv(
    path: Path, headers: tuple[str, ...], rows: list[dict[str, str]], *, encoding: str
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_xlsx(path: Path, headers: tuple[str, ...], rows: list[dict[str, str]]) -> Path:
    """用 openpyxl（dev 依赖）造真实 xlsx：与标准库读取器交叉验证。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(list(headers))
    for row in rows:
        sheet.append([row.get(name, "") for name in headers])
    workbook.save(path)
    return path


# ---------------------------------------------------------------------------
# 1) 推翻分类
# ---------------------------------------------------------------------------
def test_classify_overturn_two_vs_one() -> None:
    models = {"db": "SHORT", "qw": "SHORT", "bd": "LONG"}

    assert classify_overturn("SHORT", models, ["db", "qw"]) == OVERTURN_MAJORITY
    assert classify_overturn("LONG", models, ["db", "qw"]) == OVERTURN_MINORITY
    assert classify_overturn("FLAT", models, ["db", "qw"]) == OVERTURN_NONE


def test_classify_overturn_blank_majority() -> None:
    """两家留空 vs 一家给值：人工选"未给出"=采纳多数派；选那个值=采纳少数派。"""
    models = {"db": "", "qw": "", "bd": "2420"}

    assert classify_overturn("", models, ["db", "qw"]) == OVERTURN_MAJORITY
    assert classify_overturn("2420", models, ["db", "qw"]) == OVERTURN_MINORITY


def test_classify_overturn_three_way() -> None:
    models = {"db": "LONG", "qw": "SHORT", "bd": "FLAT"}

    assert classify_overturn("SHORT", models, []) == OVERTURN_SINGLE
    assert classify_overturn("UNKNOWN", models, []) == OVERTURN_NONE


# ---------------------------------------------------------------------------
# 2) 归档与网格
# ---------------------------------------------------------------------------
def test_archive_input_preserves_bytes(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "pending_review.csv",
        PENDING_HEADERS,
        [_pending_row("R0001", "p1", "stance")],
        encoding="utf-8-sig",
    )

    target = archive_input(source, tmp_path / "archive", stamp="20260913-120000")

    assert target.read_bytes() == source.read_bytes()
    assert target.name == "pending_review_adjudicated_20260913-120000.csv"


def test_archive_input_names_xlsx_by_content(tmp_path: Path) -> None:
    """扩展名说谎的文件（其实是 xlsx）归档时要用正确的后缀。"""
    source = _write_xlsx(
        tmp_path / "pending_review.csv", PENDING_HEADERS, [_pending_row("R0001", "p1", "stance")]
    )

    target = archive_input(source, tmp_path / "archive", stamp="20260913-120000")

    assert target.suffix == ".xlsx"
    assert zipfile.is_zipfile(target)


def test_reference_grid_expands_samples_by_fields(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "annotation_sample.csv",
        ("post_id", "text_content"),
        [{"post_id": "p1", "text_content": "a"}, {"post_id": "p2", "text_content": "b"}],
        encoding="utf-8-sig",
    )

    assert reference_grid(source, ("stance", "horizon")) == [
        ("p1", "stance"),
        ("p1", "horizon"),
        ("p2", "stance"),
        ("p2", "horizon"),
    ]


def test_reference_grid_missing_file_is_empty(tmp_path: Path) -> None:
    assert reference_grid(tmp_path / "nope.csv", ("stance",)) == []


# ---------------------------------------------------------------------------
# 3) 合并（含红线条款）
# ---------------------------------------------------------------------------
def _tables(
    tmp_path: Path,
    pending_rows: list[dict[str, str]],
    consensus_rows: list[dict[str, str]],
    *,
    pending_as_xlsx: bool = False,
) -> tuple[object, object]:
    if pending_as_xlsx:
        pending_path = _write_xlsx(tmp_path / "pending_review.csv", PENDING_HEADERS, pending_rows)
    else:
        pending_path = _write_csv(
            tmp_path / "pending_review.csv", PENDING_HEADERS, pending_rows, encoding="utf-8-sig"
        )
    consensus_path = _write_csv(
        tmp_path / "model_consensus.csv", CONSENSUS_HEADERS, consensus_rows, encoding="utf-8-sig"
    )
    return read_any_table(pending_path), read_any_table(consensus_path)


def test_merge_human_and_consensus_rows(tmp_path: Path) -> None:
    pending_rows = [
        _pending_row(
            "R0001",
            "p1",
            "stance",
            db="LONG",
            qw="UNKNOWN",
            bd="UNKNOWN",
            gold="UNKNOWN",
            notes="两家认为无法判断",
            agreeing="qw/bd",
        )
    ]
    consensus_rows = [
        _consensus_row("p1", "horizon", "1d"),
        _consensus_row("p2", "take_profit", "2450"),
    ]

    rows, stats = merge(
        *_tables(tmp_path, pending_rows, consensus_rows),
        expected=[("p1", "stance"), ("p1", "horizon"), ("p2", "take_profit")],
        fields=("stance", "horizon", "take_profit"),
    )

    assert len(rows) == 3
    human = next(row for row in rows if row.source == SOURCE_HUMAN)
    assert human.value == "UNKNOWN"
    assert human.overturn == OVERTURN_MAJORITY  # 采纳了 qw/bd 的多数派
    assert human.reviewer == "tester"
    assert "R0001" in human.provenance
    consensus = [row for row in rows if row.source == SOURCE_CONSENSUS]
    assert {row.value for row in consensus} == {"1D", "2450"}
    assert all(row.note == CONSENSUS_NOTE for row in consensus)
    assert all(row.overturn == "" for row in consensus)
    assert stats.pending_adjudicated == 1
    assert stats.consensus_with_value == 2
    assert stats.grid_missing == []


def test_merge_blank_gold_with_note_is_intentional_blank(tmp_path: Path) -> None:
    """`final_gold_standard` 空但写了理由 → 人工裁决「未给出」，且**不得**回填模型值。"""
    pending_rows = [
        _pending_row(
            "R0113",
            "p1",
            "horizon",
            db="",
            qw="",
            bd="1d",
            gold="",
            notes="文本没有提到，留空",
            agreeing="db/qw",
        )
    ]

    rows, stats = merge(*_tables(tmp_path, pending_rows, []), fields=("horizon",))

    assert len(rows) == 1
    row = rows[0]
    assert row.source == SOURCE_HUMAN
    assert row.value == ""  # 人工裁决：该字段不存在
    assert row.value_raw == ""
    assert row.note == "文本没有提到，留空"
    assert row.overturn == OVERTURN_MAJORITY  # 与两家"未给出"一致
    assert stats.pending_intentional_blank == 1
    assert stats.pending_with_value == 0
    assert stats.pending_unadjudicated == 0
    assert stats.covered_cells == 1


def test_merge_blank_gold_without_note_is_unadjudicated(tmp_path: Path) -> None:
    """空值 + 无理由 → 未裁决：不产出条目、不覆盖、单独列出（红线）。"""
    pending_rows = [
        _pending_row("R0113", "p1", "horizon", db="", qw="", bd="1d", gold="", notes="")
    ]

    rows, stats = merge(*_tables(tmp_path, pending_rows, []), fields=("horizon",))

    assert rows == []  # 绝不回填模型值（bd 说 1d）
    assert stats.pending_unadjudicated == 1
    assert stats.pending_unadjudicated_ids == ["R0113 | p1 | horizon | db/qw/bd=//1d"]
    assert stats.covered_cells == 0


def test_merge_consensus_blank_rows_are_counted_separately(tmp_path: Path) -> None:
    consensus_rows = [_consensus_row("p1", "stop_loss", "")]

    rows, stats = merge(*_tables(tmp_path, [], consensus_rows), fields=("stop_loss",))

    assert len(rows) == 1
    assert rows[0].models_agreement == "unanimous_blank"
    assert rows[0].value == ""
    assert stats.consensus_blank == 1
    assert stats.consensus_with_value == 0
    assert stats.value_cells == 0


def test_merge_detects_duplicate_cells(tmp_path: Path) -> None:
    pending_rows = [
        _pending_row(
            "R0001", "p1", "stance", db="LONG", qw="LONG", bd="SHORT", gold="LONG", agreeing="db/qw"
        )
    ]
    consensus_rows = [_consensus_row("p1", "stance", "SHORT")]

    rows, stats = merge(*_tables(tmp_path, pending_rows, consensus_rows), fields=("stance",))

    assert len(rows) == 1  # 人工优先，只保留一条
    assert rows[0].source == SOURCE_HUMAN
    assert any("格子重复" in problem for problem in stats.problems)


def test_merge_reports_grid_missing(tmp_path: Path) -> None:
    rows, stats = merge(
        *_tables(tmp_path, [], [_consensus_row("p1", "stance", "LONG")]),
        expected=[("p1", "stance"), ("p1", "horizon"), ("p2", "stance")],
        fields=("stance", "horizon"),
    )

    assert len(rows) == 1
    assert stats.expected_cells == 3
    assert stats.grid_missing == ["p1|horizon", "p2|stance"]


def test_merge_sorts_rows_by_post_then_field_order(tmp_path: Path) -> None:
    consensus_rows = [
        _consensus_row("p2", "take_profit", "2450"),
        _consensus_row("p1", "take_profit", "2450"),
        _consensus_row("p1", "stance", "LONG"),
    ]

    rows, _stats = merge(
        *_tables(tmp_path, [], consensus_rows), fields=("stance", "horizon", "take_profit")
    )

    assert [(row.post_id, row.field_name) for row in rows] == [
        ("p1", "stance"),
        ("p1", "take_profit"),
        ("p2", "take_profit"),
    ]


def test_write_ground_truth_columns_and_bom(tmp_path: Path) -> None:
    rows, _stats = merge(
        *_tables(tmp_path, [], [_consensus_row("p1", "stance", "LONG", raw="做多 | LONG | 看多")]),
        fields=("stance",),
    )
    out = tmp_path / "ground_truth_200.csv"

    written = write_ground_truth(out, rows)

    assert written == 1
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        data = list(reader)
    assert reader.fieldnames == list(GROUND_TRUTH_COLUMNS)
    assert data[0]["value"] == "LONG"
    # `value_raw` 对共识行 = 模型共识原值；三方原文在 provenance 里
    assert data[0]["value_raw"] == "LONG"
    assert "raw=做多 | LONG | 看多" in data[0]["provenance"]
    assert data[0]["source"] == SOURCE_CONSENSUS
    assert data[0]["field_label"] == "方向"


# ---------------------------------------------------------------------------
# 4) 报告
# ---------------------------------------------------------------------------
def _render(tmp_path: Path, rows: object, stats: object) -> str:
    return render_report(
        rows,
        stats,
        generated_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        pending_path=tmp_path / "pending_review.csv",
        pending_digest="deadbeefdeadbeef",
        pending_format="xlsx（ZIP 容器，文件名扩展名不代表真实格式）",
        consensus_path=tmp_path / "model_consensus.csv",
        consensus_digest="cafebabecafebabe",
        out_path=tmp_path / "ground_truth_200.csv",
        archive_path=tmp_path / "archive" / "pending_review_adjudicated_x.xlsx",
        reference_path=tmp_path / "annotation_sample.csv",
        fields=("stance", "horizon"),
    )


def _overturn_fixture(tmp_path: Path) -> tuple[list, object]:
    pending_rows = [
        # 采纳少数派（推翻多数派）：LONG/LONG/UNKNOWN → 人工选 UNKNOWN
        _pending_row(
            "R0001", "p1", "stance", db="LONG", qw="LONG", bd="UNKNOWN",
            gold="UNKNOWN", agreeing="db/qw",
        ),
        # 三家都错：LONG/SHORT/UNKNOWN → 人工给 FLAT
        _pending_row(
            "R0002", "p2", "stance", db="LONG", qw="SHORT", bd="UNKNOWN", gold="FLAT"
        ),
    ]
    consensus_rows = [_consensus_row("p1", "horizon", "1d")]
    return merge(
        *_tables(tmp_path, pending_rows, consensus_rows),
        expected=[("p1", "stance"), ("p1", "horizon"), ("p2", "stance")],
        fields=("stance", "horizon"),
    )


def test_report_contains_overturn_and_coverage(tmp_path: Path) -> None:
    rows, stats = _overturn_fixture(tmp_path)

    text = _render(tmp_path, rows, stats)

    assert stats.overturn_total == 2
    assert "人工推翻模型：2 次" in text
    assert "采纳少数派（推翻多数派）：**1** 次" in text
    assert "三家都错（人工给出新答案）：**1** 次" in text
    assert "金标准覆盖率：3 / 3 = 100.0%" in text
    assert "human-adjudicated" in text and "3-model-consensus" in text
    assert "不是金标准" in text or "未经人工确认" in text  # 红线提示必须在报告里


def test_report_lists_unadjudicated_cells(tmp_path: Path) -> None:
    pending_rows = [_pending_row("R0113", "p1", "horizon", bd="1d", gold="", notes="")]
    rows, stats = merge(*_tables(tmp_path, pending_rows, []), fields=("horizon",))

    text = _render(tmp_path, rows, stats)

    assert "未裁决清单" in text
    assert "R0113 | p1 | horizon" in text
    assert "没有回填模型值" in text


def test_report_marks_missing_grid_cells(tmp_path: Path) -> None:
    rows, stats = merge(
        *_tables(tmp_path, [], [_consensus_row("p1", "stance", "LONG")]),
        expected=[("p1", "stance"), ("p2", "stance")],
        fields=("stance", "horizon"),
    )

    text = _render(tmp_path, rows, stats)

    assert stats.grid_missing == ["p2|stance"]
    assert "网格完整性：相对抽样清单仍缺 1 格" in text


def test_merge_exposes_four_overturn_labels(tmp_path: Path) -> None:
    rows, stats = _overturn_fixture(tmp_path)

    assert stats.overturn_counts == {OVERTURN_MINORITY: 1, OVERTURN_NONE: 1}
    assert {row.overturn for row in rows if row.source == SOURCE_HUMAN} == {
        OVERTURN_MINORITY,
        OVERTURN_NONE,
    }


# ---------------------------------------------------------------------------
# 5) CLI 端到端
# ---------------------------------------------------------------------------
def _cli_inputs(
    tmp_path: Path,
    *,
    pending_as_xlsx: bool = False,
    pending_as_gbk: bool = False,
    unadjudicated: bool = False,
) -> tuple[Path, Path, Path]:
    pending_rows = [
        _pending_row(
            "R0001", "p1", "stance", db="LONG", qw="LONG", bd="UNKNOWN",
            gold="UNKNOWN", notes="两家无法判断", agreeing="db/qw",
        ),
        _pending_row(
            "R0002", "p2", "horizon", db="", qw="", bd="1d",
            gold="", notes="" if unadjudicated else "文本没有提到，留空", agreeing="db/qw",
        ),
    ]
    if pending_as_xlsx:
        pending = _write_xlsx(tmp_path / "pending_review.csv", PENDING_HEADERS, pending_rows)
    else:
        pending = _write_csv(
            tmp_path / "pending_review.csv",
            PENDING_HEADERS,
            pending_rows,
            encoding="gbk" if pending_as_gbk else "utf-8-sig",
        )
    consensus = _write_csv(
        tmp_path / "model_consensus.csv",
        CONSENSUS_HEADERS,
        [_consensus_row("p1", "take_profit", "2450")],
        encoding="utf-8-sig",
    )
    reference = _write_csv(
        tmp_path / "annotation_sample.csv",
        ("post_id", "text_content"),
        [{"post_id": "p1", "text_content": "a"}, {"post_id": "p2", "text_content": "b"}],
        encoding="utf-8-sig",
    )
    return pending, consensus, reference


def _cli_args(
    tmp_path: Path, pending: Path, consensus: Path, reference: Path, *extra: str
) -> list[str]:
    return [
        "--pending",
        str(pending),
        "--consensus",
        str(consensus),
        "--reference-csv",
        str(reference),
        "--out",
        str(tmp_path / "ground_truth_200.csv"),
        "--report",
        str(tmp_path / "report.md"),
        "--archive-dir",
        str(tmp_path / "archive"),
        "--fields",
        "stance,horizon,take_profit",
        *extra,
    ]


def test_main_writes_truth_report_and_archive(tmp_path: Path) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path)

    exit_code = truth_main(_cli_args(tmp_path, pending, consensus, reference))

    assert exit_code == 0
    out = tmp_path / "ground_truth_200.csv"
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3  # 2 条人工裁决 + 1 条共识
    assert {row["source"] for row in rows} == {SOURCE_HUMAN, SOURCE_CONSENSUS}
    assert list((tmp_path / "archive").glob("pending_review_adjudicated_*.csv"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "人工推翻模型：1 次" in report
    assert "金标准覆盖率" in report


def test_main_handles_xlsx_named_csv(tmp_path: Path) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path, pending_as_xlsx=True)

    exit_code = truth_main(_cli_args(tmp_path, pending, consensus, reference))

    assert exit_code == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "xlsx（ZIP 容器" in report
    assert list((tmp_path / "archive").glob("pending_review_adjudicated_*.xlsx"))


def test_main_handles_gbk_csv(tmp_path: Path) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path, pending_as_gbk=True)

    exit_code = truth_main(_cli_args(tmp_path, pending, consensus, reference))

    assert exit_code == 0
    assert "编码 gbk" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_main_returns_3_when_some_cells_are_unadjudicated(tmp_path: Path) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path, unadjudicated=True)

    exit_code = truth_main(_cli_args(tmp_path, pending, consensus, reference))

    assert exit_code == 3
    out = tmp_path / "ground_truth_200.csv"
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2  # 未裁决的那格没有条目（也没回填模型的 1d）
    assert not any(row["post_id"] == "p2" and row["field"] == "horizon" for row in rows)
    assert "未裁决清单" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path)

    exit_code = truth_main(_cli_args(tmp_path, pending, consensus, reference, "--dry-run"))

    assert exit_code == 0
    assert not (tmp_path / "ground_truth_200.csv").exists()
    assert not (tmp_path / "archive").exists()
    assert "--dry-run" in capsys.readouterr().out


def test_main_missing_input_returns_2(tmp_path: Path, capsys) -> None:
    exit_code = truth_main(["--pending", str(tmp_path / "nope.csv")])

    assert exit_code == 2
    assert "输入文件不存在" in capsys.readouterr().err


def test_main_empty_fields_returns_2(tmp_path: Path) -> None:
    pending, consensus, reference = _cli_inputs(tmp_path)

    assert truth_main(_cli_args(tmp_path, pending, consensus, reference, "--fields", " , ")) == 2


# ---------------------------------------------------------------------------
# 6) 对比脚本的 IO / 防覆盖能力（真实世界踩坑回归）
# ---------------------------------------------------------------------------
AI_HEADERS = (
    "post_id",
    "text_content",
    "stance_db",
    "stance_qw",
    "stance_bd",
)


def _ai_xlsx(tmp_path: Path) -> Path:
    return _write_xlsx(
        tmp_path / "ai.xlsx",
        AI_HEADERS,
        [
            {
                "post_id": "p1",
                "text_content": "黄金 做多，目标 2450",
                "stance_db": "LONG",
                "stance_qw": "LONG",
                "stance_bd": "SHORT",
            }
        ],
    )


def test_read_any_table_sniffs_content_over_suffix(tmp_path: Path) -> None:
    """扩展名说是 csv，内容其实是 xlsx —— 必须按内容读。"""
    path = _write_xlsx(tmp_path / "pending_review.csv", ("post_id",), [{"post_id": "p1"}])

    assert sniff_is_xlsx(path) is True
    assert read_any_table(path).rows == ({"post_id": "p1"},)


def test_read_any_table_handles_gbk(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "gbk.csv",
        ("post_id", "note"),
        [{"post_id": "p1", "note": "中文备注"}],
        encoding="gbk",
    )

    table = read_any_table(path)

    assert table.encoding == "gbk"
    assert table.rows[0]["note"] == "中文备注"


def test_pending_has_human_work_detects_filled_gold(tmp_path: Path) -> None:
    blank = _write_csv(
        tmp_path / "blank.csv",
        PENDING_HEADERS,
        [_pending_row("R0001", "p1", "stance")],
        encoding="utf-8-sig",
    )
    filled = _write_csv(
        tmp_path / "filled.csv",
        PENDING_HEADERS,
        [_pending_row("R0001", "p1", "stance", gold="UNKNOWN")],
        encoding="utf-8-sig",
    )

    assert pending_has_human_work(blank) is False
    assert pending_has_human_work(filled) is True
    assert pending_has_human_work(tmp_path / "missing.csv") is False


def test_compare_main_refuses_to_overwrite_human_work(tmp_path: Path, capsys) -> None:
    """保护 226 条人工裁决：默认拒绝覆盖已填写的待裁决表（退出码 4）。"""
    ai = _ai_xlsx(tmp_path)
    pending_out = _write_csv(
        tmp_path / "pending_review.csv",
        PENDING_HEADERS,
        [_pending_row("R0001", "p1", "stance", gold="UNKNOWN")],
        encoding="utf-8-sig",
    )
    before = pending_out.read_bytes()
    common = [
        "--input",
        str(ai),
        "--fields",
        "stance",
        "--sheet",
        "",
        "--pending-out",
        str(pending_out),
        "--consensus-out",
        str(tmp_path / "model_consensus.csv"),
        "--report-out",
        str(tmp_path / "report.md"),
        "--no-reference-check",
    ]

    assert compare_main(common) == 4
    assert "拒绝覆盖" in capsys.readouterr().err
    assert pending_out.read_bytes() == before  # 人工成果一字节未动

    assert compare_main([*common, "--force"]) == 0  # 显式 --force 才允许
    assert pending_out.read_bytes() != before


def test_scripts_stay_off_database() -> None:
    import scripts.build_ground_truth as truth_module
    import scripts.compare_model_annotations as compare_module

    for module in (truth_module, compare_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("sqlalchemy", "from database", "import database", "build_engine"):
            assert banned not in source, f"{module.__name__} 不得触碰数据库：{banned}"