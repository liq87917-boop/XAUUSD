"""三模型标注对比脚本的单元测试（零新依赖：xlsx 由测试自己用 zip+XML 造出来）。

覆盖重点：
1. **xlsx 标准库读取器**（共享字符串 / 内联字符串 / 数字 / 布尔 /
   **稀疏单元格** / 空行 / 多表选择）；
2. **列名匹配**（大小写 / 下划线不敏感，覆盖真实文件里的 `stop_loss_DB` 混用）；
3. **归一化口径**（同义不同写不算分歧；空值与占位符算"未给出"）；
4. **一致性统计**（一致 / 三方都留空 / 2:1 / 三方各异；两两一致率只在双方都给出值时计入）；
5. **导出契约**（`final_gold_standard` 必须留空——金标准只能人工填；共识文件带 provenance）；
6. **红线**：脚本不得触碰数据库（源码级断言）。
"""

from __future__ import annotations

import csv
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.compare_model_annotations import (
    CONSENSUS_COLUMNS,
    CONSENSUS_PROVENANCE,
    MODELS,
    PENDING_COLUMNS,
    ComparisonReport,
    FieldOutcome,
    ModelAnnotation,
    SheetTable,
    _column_index,
    _plain_number,
    build_annotations,
    compare,
    compare_field,
    detect_extra_fields,
    find_field_columns,
    main,
    normalize_number,
    normalize_value,
    read_csv_table,
    read_table,
    read_xlsx,
    render_report,
    write_consensus,
    write_pending_review,
)

pytestmark = pytest.mark.unit

FIELDS = ("stance", "horizon", "stop_loss", "take_profit", "information_type")
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
)
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<workbook xmlns="{_MAIN}" xmlns:r="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships"><sheets><sheet name="annotation_sample" sheetId="1" '
    'r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
    'Target="sharedStrings.xml"/></Relationships>'
)


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _write_min_xlsx(path: Path, rows: list[list[tuple[str, str]]]) -> Path:
    """写一个最小可用 xlsx（专供读取器测试）。

    每行是 ``[(kind, value), ...]``；``kind`` ∈ ``s``(共享字符串) / ``n``(数字) /
    ``b``(布尔) / ``i``(内联字符串) / ``skip``(该单元格完全不存在，用于测稀疏行)。
    """
    shared: list[str] = []
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column, (kind, value) in enumerate(row):
            if kind == "skip":
                continue
            ref = f"{_col_letter(column)}{row_index}"
            if kind == "s":
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
            elif kind == "b":
                cells.append(f'<c r="{ref}" t="b"><v>{value}</v></c>')
            elif kind == "i":
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    string_items = "".join(f"<si><t>{item}</t></si>" for item in shared)
    sheet_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_MAIN}">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{_MAIN}" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">{string_items}</sst>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return path


# ---------------------------------------------------------------------------
# 1) 读取器
# ---------------------------------------------------------------------------
def test_column_index_handles_multi_letter_refs() -> None:
    assert _column_index("A1") == 0
    assert _column_index("B2") == 1
    assert _column_index("Z10") == 25
    assert _column_index("AA1") == 26
    assert _column_index("AB12") == 27
    assert _column_index("AE2") == 30


def test_column_index_rejects_malformed_ref() -> None:
    with pytest.raises(ValueError, match="非法单元格引用"):
        _column_index("1A")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2450", "2450"), ("2450.0", "2450"), ("0.9", "0.9"), ("2450.5", "2450.5"), ("-3", "-3")],
)
def test_plain_number_trims_only_zero_tails(value: str, expected: str) -> None:
    assert _plain_number(value) == expected


def test_read_xlsx_supports_sparse_rows_and_cell_types(tmp_path: Path) -> None:
    """缺列（不写 `<c>`）、内联字符串、布尔、数字混合——都要落到正确的列。"""
    path = _write_min_xlsx(
        tmp_path / "sample.xlsx",
        [
            [("s", "post_id"), ("s", "text_content"), ("s", "has_media"), ("s", "stop_loss_db")],
            [("s", "mock-post-0001"), ("s", "黄金做多，目标 2450"), ("b", "1"), ("n", "2420.0")],
            [
                ("s", "mock-post-0002"),
                ("i", "内联字符串不是共享字符串"),
                ("skip", ""),
                ("n", "2380"),
            ],
            [("skip", "")],
            [("s", "mock-post-0003"), ("s", "观望"), ("b", "0"), ("n", "无")],
        ],
    )

    table = read_xlsx(path, sheet="annotation_sample")

    assert table.headers == ("post_id", "text_content", "has_media", "stop_loss_db")
    assert table.sheet_name == "annotation_sample"
    assert table.sheet_names == ("annotation_sample",)
    assert len(table.rows) == 3  # 全空行被丢弃
    assert table.rows[0] == {
        "post_id": "mock-post-0001",
        "text_content": "黄金做多，目标 2450",
        "has_media": "true",
        "stop_loss_db": "2420",  # 2420.0 → 2420：去掉无意义的 .0 尾巴
    }
    assert table.rows[1]["has_media"] == ""  # 稀疏单元格补空串
    assert table.rows[1]["text_content"] == "内联字符串不是共享字符串"
    assert table.rows[2]["has_media"] == "false"


def test_read_xlsx_unknown_sheet_raises(tmp_path: Path) -> None:
    path = _write_min_xlsx(tmp_path / "sample.xlsx", [[("s", "post_id")]])

    with pytest.raises(LookupError, match="工作表不存在"):
        read_xlsx(path, sheet="nope")


def test_read_table_dispatches_by_suffix(tmp_path: Path) -> None:
    xlsx = _write_min_xlsx(tmp_path / "a.xlsx", [[("s", "post_id")], [("s", "p1")]])
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("post_id\np1\n", encoding="utf-8")

    assert read_table(xlsx).rows == ({"post_id": "p1"},)
    assert read_table(csv_path).rows == ({"post_id": "p1"},)


def test_read_table_rejects_unknown_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不支持的输入格式"):
        read_table(tmp_path / "a.docx")


def test_read_csv_table_handles_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_text("post_id,text_content\np1,黄金做多\n", encoding="utf-8-sig")

    table = read_csv_table(path)

    assert table.headers == ("post_id", "text_content")
    assert table.rows[0]["text_content"] == "黄金做多"


# ---------------------------------------------------------------------------
# 2) 列名匹配（大小写 / 下划线不敏感）
# ---------------------------------------------------------------------------
def test_find_field_columns_is_case_and_separator_insensitive() -> None:
    """真实文件里就是 `stop_loss_DB` / `stop_loss_QW` / `stop_loss_bd` 这种混用写法。"""
    headers = (
        "post_id",
        "Stance_DB",
        "stance_qw",
        "stance-BD",
        "horizon_db",
        "horizon_qw",
        "horizon_bd",
        "stop_loss_DB",
        "stop_loss_QW",
        "stop_loss_bd",
        "take_profit_db",
        "take_profit_qw",
        "take_profit_bd",
        "information_type_db",
        "information_type_qw",
        "information_type_bd",
    )

    mapping, problems = find_field_columns(headers, FIELDS)

    assert problems == []
    assert mapping["stance"] == {"db": "Stance_DB", "qw": "stance_qw", "bd": "stance-BD"}
    assert mapping["stop_loss"] == {
        "db": "stop_loss_DB",
        "qw": "stop_loss_QW",
        "bd": "stop_loss_bd",
    }


def test_find_field_columns_reports_missing_combinations() -> None:
    mapping, problems = find_field_columns(("post_id", "stance_db"), ("stance",))

    assert mapping["stance"] == {"db": "stance_db"}
    assert len(problems) == 2
    assert all("stance_qw" in problem or "stance_bd" in problem for problem in problems)


def test_detect_extra_fields_lists_unused_triplets() -> None:
    headers = (
        "post_id",
        "stance_db",
        "stance_qw",
        "stance_bd",
        "instrument_db",
        "instrument_qw",
        "instrument_bd",
        "confidence_db",
        "confidence_qw",
    )

    assert detect_extra_fields(headers, ("stance",)) == ("instrument",)


# ---------------------------------------------------------------------------
# 3) 归一化（只影响比较，不影响展示）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("stance", "做多", "LONG"),
        ("stance", "LONG", "LONG"),
        ("stance", " 看多 ", "LONG"),
        ("stance", "bullish", "LONG"),
        ("stance", "做空", "SHORT"),
        ("stance", "SHORT ", "SHORT"),
        ("stance", "观望", "FLAT"),
        ("stance", "区间", "FLAT"),
        ("stance", "未知", "UNKNOWN"),
        ("stance", "-", ""),
        ("stance", "None", ""),
        ("stance", "无", ""),
        ("horizon", "15m", "15M"),
        ("horizon", "15 分钟", "15M"),
        ("horizon", "1小时", "1H"),
        ("horizon", "日内", "1H"),
        ("horizon", "4小时", "4H"),
        ("horizon", "日线", "1D"),
        ("horizon", "本周", "1D"),
        ("information_type", "宏观", "MACRO"),
        ("information_type", "TECHNICAL", "TECHNICAL"),
        ("information_type", "技术面", "TECHNICAL"),
        ("information_type", "消息", "NEWS"),
        ("information_type", "持仓", "POSITIONING"),
        ("information_type", "情绪", "SENTIMENT"),
        ("information_type", "其他", "OTHER"),
        ("stop_loss", "2420.0", "2420"),
        ("stop_loss", "2,450", "2450"),
        ("stop_loss", "≈2340", "2340"),
        ("stop_loss", "2340-2350", "2340"),
        ("stop_loss", "2340 美元", "2340"),
        ("stop_loss", "N/A", ""),
        ("take_profit", "2292", "2292"),
    ],
)
def test_normalize_value(field: str, raw: str, expected: str) -> None:
    assert normalize_value(field, raw) == expected


def test_normalize_number_without_digits_falls_back_to_text() -> None:
    assert normalize_number("不适用") == "不适用"
    assert normalize_number("  ") == ""


# ---------------------------------------------------------------------------
# 4) 单字段对比
# ---------------------------------------------------------------------------
def _outcome_pattern(
    normalized: tuple[str, str, str], raw: tuple[str, str, str] | None = None
) -> FieldOutcome:
    raw_values = raw or normalized
    return compare_field(
        "p1",
        "stance",
        dict(zip(MODELS, raw_values, strict=True)),
        dict(zip(MODELS, normalized, strict=True)),
    )


def test_compare_field_unanimous() -> None:
    outcome = _outcome_pattern(("LONG", "LONG", "LONG"))

    assert outcome.pattern == "unanimous"
    assert outcome.is_unanimous is True
    assert outcome.agreeing_models == MODELS
    assert outcome.disagreeing_models == ()
    assert outcome.blank_models == ()


def test_compare_field_unanimous_blank_is_separate_pattern() -> None:
    outcome = _outcome_pattern(("", "", ""))

    assert outcome.pattern == "unanimous_blank"
    assert outcome.is_unanimous is True  # 算一致，但单独统计覆盖度
    assert outcome.blank_models == MODELS


def test_compare_field_two_vs_one() -> None:
    outcome = _outcome_pattern(("SHORT", "SHORT", "LONG"), ("做空", "空单", "看多"))

    assert outcome.pattern == "two_vs_one"
    assert outcome.agreeing_models == ("db", "qw")
    assert outcome.disagreeing_models == ("bd",)
    assert outcome.raw["bd"] == "看多"  # 展示始终用原文


def test_compare_field_three_way() -> None:
    outcome = _outcome_pattern(("LONG", "SHORT", "FLAT"))

    assert outcome.pattern == "three_way"
    assert outcome.agreeing_models == ()
    assert outcome.disagreeing_models == MODELS


def test_compare_field_blank_vs_value_counts_as_disagreement() -> None:
    outcome = _outcome_pattern(("", "2420", "2420"))

    assert outcome.pattern == "two_vs_one"
    assert outcome.blank_models == ("db",)
    assert outcome.is_unanimous is False


# ---------------------------------------------------------------------------
# 5) 整批对比
# ---------------------------------------------------------------------------
def _row(post_id: str, text: str, **values: str) -> dict[str, str]:
    row = {
        "post_id": post_id,
        "text_content": text,
        "source_name": "jin10_flash",
        "published_at": "2026-08-24T00:00:00+00:00",
        "effective_at": "2026-08-24T01:00:00+00:00",
    }
    row.update(values)
    return row


def _sheet(rows: list[dict[str, str]]) -> SheetTable:
    headers = tuple(dict.fromkeys(key for row in rows for key in row))
    return SheetTable(source="test:memory", headers=headers, rows=tuple(rows), sheet_name="test")


def _sample_rows() -> tuple[list[dict[str, str]], list[ModelAnnotation]]:
    """三行样本，分别代表：完全一致（raw 不同）/ 2:1 分歧 / 三方各异 + 一行全员留空。"""
    rows = [
        _row(
            "mock-post-0001",
            "黄金 做多，目标 2450，止损 2420",
            stance_db="做多",
            stance_qw="LONG",
            stance_bd="看多",
            horizon_db="15m",
            horizon_qw="15m",
            horizon_bd="15m",
            stop_loss_db="2420",
            stop_loss_qw="2420",
            stop_loss_bd="2420",
            take_profit_db="2450",
            take_profit_qw="2450",
            take_profit_bd="2450",
            information_type_db="宏观",
            information_type_qw="MACRO",
            information_type_bd="宏观",
        ),
        _row(
            "mock-post-0002",
            "黄金 做空，目标 2292",
            stance_db="做空",
            stance_qw="做空",
            stance_bd="观望",
            horizon_db="1d",
            horizon_qw="1d",
            horizon_bd="1d",
            stop_loss_db="",
            stop_loss_qw="",
            stop_loss_bd="2420",
            take_profit_db="2292",
            take_profit_qw="2292",
            take_profit_bd="2292",
            information_type_db="NEWS",
            information_type_qw="NEWS",
            information_type_bd="NEWS",
        ),
        _row(
            "mock-post-0003",
            "看图操作，等价格走出来",
            stance_db="LONG",
            stance_qw="LONG",
            stance_bd="LONG",
            horizon_db="",
            horizon_qw="",
            horizon_bd="",
            stop_loss_db="2400",
            stop_loss_qw="2400",
            stop_loss_bd="2400",
            take_profit_db="2450",
            take_profit_qw="2450",
            take_profit_bd="2450",
            information_type_db="TECHNICAL",
            information_type_qw="POSITIONING",
            information_type_bd="NEWS",
        ),
    ]
    table = _sheet(rows)
    annotations, _mapping, problems = build_annotations(table, FIELDS)
    assert problems == []
    return rows, annotations


def test_build_annotations_keeps_raw_and_normalized() -> None:
    _rows, annotations = _sample_rows()

    first = annotations[0]

    assert first.post_id == "mock-post-0001"
    assert first.raw["stance"]["db"] == "做多"  # 原文保留
    assert first.normalized["stance"]["db"] == "LONG"  # 归一化用于比较
    assert first.normalized["information_type"]["db"] == "MACRO"
    assert first.text_content == "黄金 做多，目标 2450，止损 2420"


def test_compare_counts_row_level_agreement() -> None:
    _rows, annotations = _sample_rows()

    report = compare(annotations, FIELDS, source="test:memory", sheet_name="test")

    assert report.rows_total == 3
    assert report.rows_full_agreement == 1  # 只有第一行五字段全一致
    assert report.rows_full_agreement_strict == 1  # 第一行三模型都给了值
    assert report.rows_full_agreement_raw == 0  # 第一行原文不同（做多 / LONG / 看多）


def test_compare_field_stats_and_pairwise() -> None:
    _rows, annotations = _sample_rows()

    report = compare(annotations, FIELDS)
    stance = report.field_stats["stance"]
    horizon = report.field_stats["horizon"]
    info_type = report.field_stats["information_type"]

    assert (stance.unanimous, stance.two_vs_one, stance.three_way) == (2, 1, 0)
    assert stance.disagreement_rate == pytest.approx(1 / 3)
    assert horizon.unanimous_blank == 1  # 第三行三家都留空
    assert horizon.disagreement_rate == 0.0
    assert (info_type.unanimous, info_type.three_way) == (2, 1)
    # 两两一致只在"双方都给出值"时计入：db|qw 三行都一致，db|bd 只有 2 行
    assert stance.pairwise_agree["db|qw"] == 3
    assert stance.pairwise_agree["db|bd"] == 2
    assert report.field_stats["stop_loss"].blank_counts == {"db": 1, "qw": 1}


def test_compare_splits_pending_and_consensus() -> None:
    _rows, annotations = _sample_rows()

    report = compare(annotations, FIELDS)

    assert report.pending_sample_count == 2  # 第二、三行
    assert len(report.pending_outcomes) == 3  # stance + stop_loss + information_type
    assert [(item.post_id, item.field_name) for item in report.pending_outcomes] == [
        ("mock-post-0002", "stance"),
        ("mock-post-0002", "stop_loss"),
        ("mock-post-0003", "information_type"),
    ]
    assert len(report.consensus_outcomes) == len(report.outcomes) - 3 == 12


# ---------------------------------------------------------------------------
# 6) 导出：待裁决清单 / 共识 / 报告
# ---------------------------------------------------------------------------
def _report_and_annotations() -> tuple[ComparisonReport, dict[str, ModelAnnotation]]:
    _rows, annotations = _sample_rows()
    report = compare(annotations, FIELDS, source="test:memory", sheet_name="test")
    return report, {annotation.post_id: annotation for annotation in annotations}


def test_write_pending_review_keeps_gold_standard_column_empty(tmp_path: Path) -> None:
    report, by_id = _report_and_annotations()
    path = tmp_path / "pending_review.csv"

    rows = write_pending_review(path, report, by_id, fields=FIELDS)

    assert rows == 3
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        data = list(reader)
    assert reader.fieldnames == list(PENDING_COLUMNS)
    assert [row["post_id"] for row in data] == [
        "mock-post-0002",
        "mock-post-0002",
        "mock-post-0003",
    ]
    assert data[0]["field"] == "stance"
    assert data[0]["model_db"] == "做空"  # 原始判定
    assert data[0]["normalized_bd"] == "FLAT"  # 归一化结果（说明为何判为分歧）
    assert data[0]["agreeing_models"] == "db/qw"
    assert data[0]["disagreeing_models"] == "bd"
    assert data[1]["blank_models"] == "db/qw"
    assert data[0]["text_content"] == "黄金 做空，目标 2292"  # 裁决时必须看得到原文
    for row in data:  # 红线：金标准与人工字段必须留空
        assert row["final_gold_standard"] == ""
        assert row["reviewer"] == ""
        assert row["reviewed_at"] == ""
        assert row["notes"] == ""
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # Excel 友好


def test_write_consensus_marks_provenance(tmp_path: Path) -> None:
    report, by_id = _report_and_annotations()
    path = tmp_path / "model_consensus.csv"

    rows = write_consensus(path, report, by_id, fields=FIELDS)

    assert rows == 12
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        data = list(reader)
    assert reader.fieldnames == list(CONSENSUS_COLUMNS)
    assert {row["provenance"] for row in data} == {CONSENSUS_PROVENANCE}
    first = data[0]
    assert first["post_id"] == "mock-post-0001"
    assert first["value_agreed"] == "LONG"  # 规范值
    assert first["raw_values"] == "做多 | LONG | 看多"  # 三方原文可追溯


def test_render_report_contains_key_numbers_and_red_line(tmp_path: Path) -> None:
    report, _by_id = _report_and_annotations()

    text = render_report(
        report,
        input_path=tmp_path / "in.xlsx",
        digest="deadbeefdeadbeef",
        fields=FIELDS,
        generated_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        pending_path=tmp_path / "pending_review.csv",
        consensus_path=tmp_path / "model_consensus.csv",
        pending_rows=3,
        consensus_rows=12,
    )

    assert "不是金标准" in text  # 红线提示必须在报告里
    assert "1 / 3 = 33.3%" in text  # 完全一致比例
    assert "2 个样本、3 条" in text  # 待人工裁决量
    assert "pending_review.csv" in text and "model_consensus.csv" in text
    assert "deadbeefdeadbeef" in text
    assert "stop_loss_DB" in text  # 说明列名匹配规则


# ---------------------------------------------------------------------------
# 7) CLI 端到端（用自造 xlsx / csv 驱动）
# ---------------------------------------------------------------------------
def _e2e_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """写出与真实文件同构的最小输入（xlsx + 参考清单 CSV）。"""
    headers = ["post_id", "text_content"]
    for name in FIELDS:
        headers.extend(f"{name}_{model}" for model in MODELS)
    data_rows: list[list[tuple[str, str]]] = [[("s", header) for header in headers]]
    for index, (post_id, text, stance) in enumerate(
        [
            ("sample-0001", "黄金 做多，目标 2450，止损 2420", "LONG"),
            ("sample-0002", "黄金 做空，目标 2292，止损 2340", "SHORT"),
        ],
        start=1,
    ):
        data_rows.append(
            [
                ("s", post_id),
                ("s", text),
                ("s", stance),
                ("s", stance),
                ("s", stance),
                ("s", "1d"),
                ("s", "1d"),
                ("s", "1d"),
                ("s", "2420"),
                ("s", "2420"),
                ("s", "2420"),
                ("s", "2450"),
                ("s", "2450"),
                ("s", "2400" if index == 2 else "2450"),  # 第二行 take_profit 分歧
                ("s", "NEWS" if index == 2 else "MACRO"),
                ("s", "NEWS" if index == 2 else "MACRO"),
                ("s", "NEWS" if index == 2 else "MACRO"),
            ]
        )
    xlsx = _write_min_xlsx(tmp_path / "ai.xlsx", data_rows)
    reference = tmp_path / "annotation_sample.csv"
    reference.write_text(
        "post_id,text_content\nsample-0001,a\nsample-0002,b\n", encoding="utf-8-sig"
    )
    return xlsx, reference


def test_main_end_to_end(tmp_path: Path) -> None:
    xlsx, reference = _e2e_inputs(tmp_path)
    pending = tmp_path / "pending_review.csv"
    consensus = tmp_path / "model_consensus.csv"
    report_path = tmp_path / "annotation_model_comparison.md"

    exit_code = main(
        [
            "--input",
            str(xlsx),
            "--reference-csv",
            str(reference),
            "--pending-out",
            str(pending),
            "--consensus-out",
            str(consensus),
            "--report-out",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert pending.exists() and consensus.exists() and report_path.exists()
    with pending.open("r", encoding="utf-8-sig", newline="") as handle:
        pending_rows = list(csv.DictReader(handle))
    assert len(pending_rows) == 1  # 只有第二行的 take_profit 分歧
    assert pending_rows[0]["field"] == "take_profit"
    assert pending_rows[0]["final_gold_standard"] == ""
    assert "对齐检查：通过：2/2" in report_path.read_text(encoding="utf-8")


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    xlsx, reference = _e2e_inputs(tmp_path)
    pending = tmp_path / "pending_review.csv"

    exit_code = main(
        [
            "--input",
            str(xlsx),
            "--reference-csv",
            str(reference),
            "--pending-out",
            str(pending),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert not pending.exists()
    assert "--dry-run" in captured.out


def test_main_missing_input_returns_2(tmp_path: Path, capsys) -> None:
    exit_code = main(["--input", str(tmp_path / "nope.xlsx")])

    assert exit_code == 2
    assert "输入文件不存在" in capsys.readouterr().err


def test_main_unknown_field_returns_3(tmp_path: Path, capsys) -> None:
    xlsx, reference = _e2e_inputs(tmp_path)

    exit_code = main(
        ["--input", str(xlsx), "--reference-csv", str(reference), "--fields", "stance,bogus"]
    )

    assert exit_code == 3
    assert "关键列缺失" in capsys.readouterr().err


def test_main_empty_fields_returns_2(tmp_path: Path) -> None:
    xlsx, reference = _e2e_inputs(tmp_path)

    exit_code = main(
        ["--input", str(xlsx), "--reference-csv", str(reference), "--fields", " , "]
    )

    assert exit_code == 2


# ---------------------------------------------------------------------------
# 8) 红线：不碰数据库、不引入未被批准的依赖
# ---------------------------------------------------------------------------
def test_script_stays_off_database_and_unapproved_dependencies() -> None:
    import scripts.compare_model_annotations as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    for banned in ("sqlalchemy", "from database", "import database", "build_engine"):
        assert banned not in source, f"对比脚本不得出现 {banned}（它只读文件、不碰数据库）"
    for banned in ("import pandas", "import openpyxl", "import numpy", "import sklearn"):
        assert banned not in source, f"不得引入未批准依赖：{banned}"