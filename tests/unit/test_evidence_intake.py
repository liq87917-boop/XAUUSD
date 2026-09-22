"""Evidence Intake 读取与报告单元测试（GOLD-005；临时文件，零网络、零数据库）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceScope,
    ReasonCode,
    RowStatus,
)
from src.evidence.intake import (
    EvidenceIntakeReport,
    InputFile,
    InputRow,
    IntakeCounts,
    RowOutcome,
    load_input_rows,
    read_input_file,
    resolve_format,
)
from src.evidence.report import quarantine_payload, render_intake_report

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"

_ROW_KEYS = {
    "index",
    "row_number",
    "status",
    "reason_codes",
    "reasons",
    "fingerprint",
    "source",
    "source_record_id",
    "oos_eligible",
    "not_oos_eligible_reason",
    "persisted",
}
_REPORT_KEYS = {
    "schema_version",
    "report",
    "contract_version",
    "scope",
    "dry_run",
    "generated_at",
    "input",
    "counts",
    "rows",
    "notes",
}


def _outcome(
    *,
    index: int,
    row_number: int,
    status: RowStatus,
    reason_codes: tuple[ReasonCode, ...] = (),
    reasons: tuple[str, ...] = (),
    fingerprint: str | None = "a" * 64,
    oos_eligible: bool = False,
    not_oos_eligible_reason: ReasonCode | None = None,
) -> RowOutcome:
    return RowOutcome(
        index=index,
        row_number=row_number,
        status=status,
        reason_codes=reason_codes,
        reasons=reasons,
        fingerprint=fingerprint,
        source="manual-example",
        source_record_id=f"rec-{index}",
        oos_eligible=oos_eligible,
        not_oos_eligible_reason=not_oos_eligible_reason,
        persisted=status is RowStatus.ACCEPTED,
    )


def _report() -> EvidenceIntakeReport:
    rows = (
        _outcome(index=0, row_number=2, status=RowStatus.ACCEPTED, oos_eligible=True),
        _outcome(
            index=1,
            row_number=3,
            status=RowStatus.QUARANTINED,
            reason_codes=(ReasonCode.AUTHORIZATION_MISSING,),
            reasons=("authorization_status=MISSING 不是显式 APPROVED",),
            fingerprint=None,
        ),
    )
    return EvidenceIntakeReport(
        scope=EvidenceScope.AUTHOR,
        dry_run=True,
        generated_at=MOMENT,
        input_file=InputFile(path="logs/evidence/authors.jsonl", format="jsonl", sha256="b" * 64),
        rows=rows,
        counts=IntakeCounts(rows=2, accepted=1, quarantined=1, oos_eligible=1, persisted=0),
        notes=("dry-run：未写数据库",),
    )


def test_resolve_format_by_suffix_and_explicit_value(tmp_path: Path) -> None:
    assert resolve_format(tmp_path / "a.jsonl", "auto") == "jsonl"
    assert resolve_format(tmp_path / "a.ndjson", "auto") == "jsonl"
    assert resolve_format(tmp_path / "a.csv", "auto") == "csv"
    assert resolve_format(tmp_path / "a.dat", "jsonl") == "jsonl"
    with pytest.raises(ValueError, match="无法按后缀识别"):
        resolve_format(tmp_path / "a.dat", "auto")
    with pytest.raises(ValueError, match="不支持的 --format"):
        resolve_format(tmp_path / "a.csv", "xlsx")


def test_load_jsonl_isolates_unreadable_lines(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(
        '{"source": "s"}\n\nnot-json\n[1, 2, 3]\n{"source": "t"}\n',
        encoding="utf-8",
    )
    rows = load_input_rows(path, fmt="jsonl")
    assert [row.row_number for row in rows] == [1, 3, 4, 5]
    assert rows[0].readable is True
    assert rows[1].readable is False
    assert rows[1].error is not None
    assert "JSON 解析失败" in rows[1].error
    assert rows[2].readable is False
    assert rows[2].error == "JSONL 每行必须是对象（object）"
    assert rows[3].data == {"source": "t"}


def test_load_csv_numbers_rows_from_two(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text("source,content\nmanual-a,第一行\nmanual-b,第二行\n", encoding="utf-8")
    rows = load_input_rows(path, fmt="csv")
    assert [row.row_number for row in rows] == [2, 3]
    assert rows[0].data == {"source": "manual-a", "content": "第一行"}


def test_read_input_file_returns_hash_and_rejects_missing(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"source": "s"}\n', encoding="utf-8")
    info, rows = read_input_file(path)
    assert info.format == "jsonl"
    assert len(info.sha256) == 64
    assert len(rows) == 1
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    info, rows = read_input_file(empty)
    assert rows == []  # 空输入由调用方决定（CLI 退出码 3）
    assert len(info.sha256) == 64
    with pytest.raises(FileNotFoundError):
        read_input_file(tmp_path / "missing.jsonl")


def test_report_json_shape_is_stable_and_json_serializable() -> None:
    payload = _report().to_dict()
    assert set(payload) == _REPORT_KEYS
    assert payload["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert payload["scope"] == "author"
    assert payload["dry_run"] is True
    assert payload["generated_at"] == MOMENT.isoformat()
    assert payload["input"]["format"] == "jsonl"
    assert payload["counts"]["quarantined"] == 1
    for row in payload["rows"]:
        assert set(row) == _ROW_KEYS
    assert payload["rows"][1]["reason_codes"] == ["AUTHORIZATION_MISSING"]
    assert payload["rows"][1]["fingerprint"] is None
    json.dumps(payload, ensure_ascii=False)  # 必须可直接序列化


def test_quarantine_payload_covers_only_quarantined_rows() -> None:
    entries = quarantine_payload(_report())
    assert len(entries) == 1
    assert entries[0]["reason_codes"] == ["AUTHORIZATION_MISSING"]
    assert entries[0]["scope"] == "author"
    assert entries[0]["recorded_at"] == MOMENT.isoformat()
    assert entries[0]["input_sha256"] == "b" * 64


def test_render_reports_contract_counts_and_reasons() -> None:
    report = _report()
    text = render_intake_report(report)
    assert "授权证据 Evidence Intake 报告" in text
    assert "evidence-intake-v1" in text
    assert "AUTHORIZATION_MISSING" in text
    assert "NOT_OOS_ELIGIBLE" in text
    assert "不联网" in text
    assert "PHASE3_3_DATA" in text
    assert text == report.render()


def test_render_and_payload_never_contain_credentials() -> None:
    outcome = _outcome(
        index=0,
        row_number=2,
        status=RowStatus.QUARANTINED,
        reason_codes=(ReasonCode.SENSITIVE_VALUE_DETECTED,),
        reasons=("字段疑似包含凭据 ['api_key']（值不记录、不入库）",),
    )
    report = EvidenceIntakeReport(
        scope=EvidenceScope.NEWS,
        dry_run=False,
        generated_at=MOMENT,
        input_file=InputFile(path="logs/evidence/news.csv", format="csv", sha256="c" * 64),
        rows=(outcome,),
        counts=IntakeCounts(rows=1, quarantined=1),
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert SECRET not in serialized
    assert SECRET not in render_intake_report(report)
    assert "api_key" in serialized  # 只暴露列名，不暴露值


def test_input_row_readable_flag() -> None:
    assert InputRow(row_number=1, data={"a": "b"}).readable is True
    assert InputRow(row_number=1, data=None, error="坏行").readable is False

