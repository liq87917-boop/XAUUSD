"""证据模板单元测试（GOLD-006；纯函数 + 临时/仓库文件，零数据库、零网络）。

覆盖：
- 模板列来自契约（去掉系统赋值列、含可选历史可用证据列）；
- Author / News 两类模板都有可填写行，且示例行**显式标记** synthetic/example；
- 标记列能被 ``synthetic_marker_fields`` 识别 → 导入判 ``SYNTHETIC_EVIDENCE`` 隔离；
- 即使有人删掉标记列，示例行仍因授权 ``PENDING`` / 时间非法而无法通过；
- 仓库内 ``examples/evidence/*.csv`` 与生成器一致。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.evidence.contracts import (
    EXAMPLE_MARKER_FLAG_FIELDS,
    EXAMPLE_MARKER_KIND_FIELDS,
    SYSTEM_ASSIGNED_FIELDS,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    synthetic_marker_fields,
)
from src.evidence.templates import (
    EXAMPLE_MARKER_COLUMNS,
    TEMPLATE_FORMATS,
    TEMPLATE_ROOT,
    example_rows,
    render_template,
    template_columns,
    template_output_name,
    write_template,
)
from src.evidence.validation import assess_row

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_marker_columns_are_recognized_by_contract() -> None:
    """模板的每个标记列都必须属于契约可识别的标记列，否则示例会被当成真实证据。"""
    recognized = set(EXAMPLE_MARKER_KIND_FIELDS) | set(EXAMPLE_MARKER_FLAG_FIELDS)
    assert set(EXAMPLE_MARKER_COLUMNS) <= recognized
    row = {"record_kind": "example", "is_mock": "true"}
    assert synthetic_marker_fields(row) == ("is_mock", "record_kind")
    # 非标记值 / 空值 / 真实行一律不判定
    assert synthetic_marker_fields({"record_kind": "real", "is_mock": "false"}) == ()
    assert synthetic_marker_fields({"is_mock": ""}) == ()
    assert synthetic_marker_fields({"source": "manual"}) == ()


def test_template_columns_follow_contract_and_exclude_system_fields() -> None:
    for scope in EvidenceScope:
        columns = template_columns(scope)
        assert set(SYSTEM_ASSIGNED_FIELDS).isdisjoint(columns)  # ingested_at / fingerprint
        assert "published_at" in columns
        assert "collected_at" in columns
        assert "available_at" in columns  # 历史可用证据列必须可填写
        assert "authorization_status" in columns
        assert columns[-len(EXAMPLE_MARKER_COLUMNS) :] == EXAMPLE_MARKER_COLUMNS


def test_csv_template_is_fillable_and_explicitly_marked() -> None:
    rows = _read_csv(render_template(EvidenceScope.AUTHOR, "csv"))
    assert len(rows) == len(example_rows(EvidenceScope.AUTHOR)) == 2
    for row in rows:
        assert row["record_kind"] == "example"
        assert row["is_mock"] == "true"
        # 其余字段是占位值 / 明确未授权：即使删掉标记列也无法通过
        assert row["authorization_status"] == "PENDING"
        assert row["permits_automated_collection"] == "false"
        assert row["permits_local_storage"] == "false"
        assert row["permits_research_use"] == "false"
        assert row["published_at"].startswith("FILL_ME_")
        assert row["content"].startswith("FILL_ME_")


def test_jsonl_template_is_one_object_per_line_with_markers() -> None:
    lines = [line for line in render_template(EvidenceScope.NEWS, "jsonl").splitlines() if line]
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)
        assert payload["record_kind"] == "example"
        assert payload["is_mock"] == "true"
        assert payload["author_name"] == ""  # News 不需要作者身份列
        assert payload["authorization_status"] == "PENDING"


def test_render_template_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="不支持的模板格式"):
        render_template(EvidenceScope.AUTHOR, "xlsx")
    with pytest.raises(ValueError, match="不支持的模板格式"):
        template_output_name(EvidenceScope.NEWS, "xlsx")


def test_template_output_name_and_formats() -> None:
    assert TEMPLATE_FORMATS == ("csv", "jsonl")
    assert template_output_name(EvidenceScope.AUTHOR, "csv") == "author_evidence_template.csv"
    assert template_output_name(EvidenceScope.NEWS, "jsonl") == "news_evidence_template.jsonl"
    assert Path("examples") / "evidence" == TEMPLATE_ROOT


def test_write_template_refuses_overwrite_then_allows(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "author.csv"
    written = write_template(EvidenceScope.AUTHOR, "csv", target)
    assert written == target and target.exists()
    with pytest.raises(FileExistsError, match="已存在"):
        write_template(EvidenceScope.AUTHOR, "csv", target)
    target.write_text("operator 已填写内容", encoding="utf-8")
    write_template(EvidenceScope.AUTHOR, "csv", target, overwrite=True)
    assert "record_kind" in target.read_text(encoding="utf-8")


def test_repository_templates_exist_and_are_marked() -> None:
    for name in ("author_evidence_template.csv", "news_evidence_template.csv"):
        path = REPO_ROOT / "examples" / "evidence" / name
        assert path.exists(), f"缺少模板文件：{path}"
        rows = _read_csv(path.read_text(encoding="utf-8"))
        assert rows, f"模板没有示例行：{path}"
        for row in rows:
            assert synthetic_marker_fields(row)  # 标记可被机械识别
            assert row["record_kind"] == "example"


@pytest.mark.parametrize("scope", list(EvidenceScope))
def test_template_example_rows_are_quarantined_as_synthetic(scope: EvidenceScope) -> None:
    """模板示例行导入时必须判 SYNTHETIC_EVIDENCE：绝不进入可信证据台账。"""
    row = _read_csv(render_template(scope, "csv"))[0]
    assessment = assess_row(row, scope=scope, index=0, row_number=2, moment=MOMENT)
    assert assessment.status is RowStatus.QUARANTINED
    assert assessment.record is None
    assert ReasonCode.SYNTHETIC_EVIDENCE in assessment.reason_codes
    assert any("只记录列名" in text for text in assessment.reasons)


@pytest.mark.parametrize("scope", list(EvidenceScope))
def test_template_example_without_markers_still_cannot_pass(scope: EvidenceScope) -> None:
    """防御性检查：即使有人删掉标记列，示例行的占位授权 / 非法时间仍会被隔离。"""
    row = dict(_read_csv(render_template(scope, "csv"))[0])
    for column in EXAMPLE_MARKER_COLUMNS:
        row.pop(column)
    assessment = assess_row(row, scope=scope, index=0, row_number=2, moment=MOMENT)
    assert assessment.status is RowStatus.QUARANTINED
    assert ReasonCode.SYNTHETIC_EVIDENCE not in assessment.reason_codes
    assert ReasonCode.AUTHORIZATION_MISSING in assessment.reason_codes
    assert ReasonCode.PUBLISHED_AT_INVALID in assessment.reason_codes
