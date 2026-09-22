"""Evidence Intake 端到端集成测试（GOLD-005；SQLite + 临时文件，零网络）。

覆盖：dry-run 零写入、提交幂等、内容冲突隔离且不覆盖、坏行隔离与稳定原因码、
自动来源禁用、敏感值不入库/不入报告、CLI 退出码与 manifest/quarantine 落盘，
以及与 GOLD-004 资格报告的集成（Mock/代码完成**不解除** ``PHASE3_3_DATA``）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import ProcessedItem, RawItem, Source
from database.models.enums import RawItemType, SourceType
from database.session import session_scope
from scripts.intake_evidence import (
    EXIT_INPUT_ERROR,
    EXIT_NO_ROWS,
    EXIT_OK,
    EXIT_QUARANTINED,
    main,
)
from src.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    intake_evidence,
    load_evidence_ledger,
    read_input_file,
)
from src.monitoring import PHASE3_3_BLOCKER_CODE, load_qualification_report

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"
COLLECTED_UTC_NAIVE = datetime(2026, 9, 18, 1, 10)


def author_row(**overrides: Any) -> dict[str, Any]:
    """完全合规的 Author 证据行（含独立历史可用证据）。"""
    row: dict[str, Any] = {
        "source": "manual-evidence-author",
        "source_record_id": "post-0001",
        "author_name": "作者甲",
        "external_account_id": "acct-001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350，本周维持逢高做空思路。",
        "published_at": "2026-09-18T08:30:00+08:00",
        "collected_at": "2026-09-18T09:10:00+08:00",
        "available_at": "2026-09-18T09:05:00+08:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://example.com/archive/2026-09-18",
        "provenance_reference": "https://example.com/posts/post-0001",
        "url": "https://example.com/posts/post-0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "authorization_reviewed_by": "合规复核人-张三",
        "authorization_reviewed_at": "2026-09-17T10:00:00+08:00",
        "authorization_valid_from": "2026-09-16T00:00:00+08:00",
        "authorization_expires_at": "2027-09-16T00:00:00+08:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def news_row(**overrides: Any) -> dict[str, Any]:
    """完全合规的 News 证据行。"""
    row: dict[str, Any] = {
        "source": "manual-evidence-news",
        "source_record_id": "news-0001",
        "title": "金价短线回落",
        "content": "亚洲盘金价自 2400 回落至 2385，市场等待美国初请数据。",
        "published_at": "2026-09-18T08:00:00+00:00",
        "collected_at": "2026-09-18T09:00:00+00:00",
        "available_at": "2026-09-18T08:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://example.com/archive/news-0001",
        "provenance_reference": "https://example.com/news/news-0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://example.com/terms/news-license",
        "authorization_reviewed_by": "合规复核人-李四",
        "authorization_reviewed_at": "2026-09-18T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def run_intake(
    session_factory: sessionmaker[Session],
    path: Path,
    *,
    scope: EvidenceScope = EvidenceScope.AUTHOR,
    dry_run: bool = True,
):
    info, rows = read_input_file(path)
    with session_scope(session_factory) as session:
        return intake_evidence(
            session, scope=scope, input_file=info, rows=rows, moment=MOMENT, dry_run=dry_run
        )


def counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with session_scope(session_factory) as session:
        return (
            int(session.scalar(sa.select(sa.func.count()).select_from(RawItem)) or 0),
            int(session.scalar(sa.select(sa.func.count()).select_from(ProcessedItem)) or 0),
            int(session.scalar(sa.select(sa.func.count()).select_from(Source)) or 0),
        )


def test_dry_run_is_zero_write(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    report = run_intake(session_factory, path, dry_run=True)
    assert report.dry_run is True
    assert report.counts.accepted == 1
    assert report.counts.persisted == 0
    assert report.counts.sources_created == 0
    assert report.accepted[0].persisted is False
    assert counts(session_factory) == (0, 0, 0)


def test_commit_persists_raw_and_processed_with_evidence_metadata(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    report = run_intake(session_factory, path, dry_run=False)
    assert report.counts.persisted == 1
    assert report.counts.processed_success == 1
    assert report.counts.sources_created == 1
    raw_count, processed_count, source_count = counts(session_factory)
    assert (raw_count, processed_count, source_count) == (1, 1, 1)
    with session_scope(session_factory) as session:
        raw = session.scalars(sa.select(RawItem)).one()
        assert raw.item_type is RawItemType.POST
        assert raw.source_record_id == "post-0001"
        assert raw.collected_at == COLLECTED_UTC_NAIVE
        assert raw.effective_at == COLLECTED_UTC_NAIVE
        evidence = raw.raw_json["evidence"]
        assert raw.raw_json["import_kind"] == "authorized_evidence_intake"
        assert evidence["contract_version"] == EVIDENCE_CONTRACT_VERSION
        assert evidence["scope"] == "author"
        assert evidence["oos_eligible"] is True
        assert evidence["not_oos_eligible_reason"] is None
        assert evidence["authorization"]["status"] == "APPROVED"
        assert evidence["ingested_at"] == MOMENT.isoformat()
        assert SECRET not in json.dumps(raw.raw_json, ensure_ascii=False)
        processed = session.scalars(sa.select(ProcessedItem)).one()
        assert processed.raw_item_id == raw.id
        assert processed.structured_json is not None
        metadata = processed.structured_json["metadata"]
        assert metadata["evidence_contract_version"] == EVIDENCE_CONTRACT_VERSION
        assert metadata["evidence_oos_eligible"] is True
        # 嵌套证据块不进入加工层元数据（sanitize_mapping 丢弃嵌套结构）
        assert "evidence" not in metadata


def test_reimport_same_bundle_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    first = run_intake(session_factory, path, dry_run=False)
    assert first.counts.persisted == 1
    second = run_intake(session_factory, path, dry_run=False)
    assert second.counts.duplicate == 1
    assert second.counts.persisted == 0
    assert second.counts.sources_created == 0
    assert second.rows[0].reason_codes == (ReasonCode.DUPLICATE,)
    assert second.rows[0].status is RowStatus.DUPLICATE
    assert counts(session_factory) == (1, 1, 1)


def test_identity_conflict_is_quarantined_without_overwriting_history(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    assert run_intake(session_factory, path, dry_run=False).counts.persisted == 1
    changed = write_jsonl(
        tmp_path / "authors_changed.jsonl",
        [author_row(content="被改写过的正文内容，绝不允许覆盖历史事实。")],
    )
    report = run_intake(session_factory, changed, dry_run=False)
    assert report.counts.quarantined == 1
    assert report.counts.persisted == 0
    assert report.rows[0].reason_codes == (ReasonCode.IDENTITY_CONFLICT,)
    assert counts(session_factory) == (1, 1, 1)
    with session_scope(session_factory) as session:
        raw = session.scalars(sa.select(RawItem)).one()
        assert "被改写过的正文" not in (raw.content_text or "")


def test_bad_rows_are_quarantined_with_stable_reason_codes(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    rows = [
        author_row(authorization_status="UNKNOWN"),
        author_row(source_record_id="post-0002", published_at="2026-09-18T08:30:00"),
        author_row(source_record_id="post-0003", provenance_reference=""),
        author_row(source_record_id="post-0004", collected_at="2026-09-18T08:30:00+08:00"),
    ]
    path = write_jsonl(tmp_path / "bad.jsonl", rows)
    report = run_intake(session_factory, path, dry_run=False)
    assert report.counts.quarantined == 4
    assert report.counts.persisted == 0
    assert counts(session_factory) == (0, 0, 0)
    codes = [set(row.reason_codes) for row in report.rows]
    assert ReasonCode.AUTHORIZATION_MISSING in codes[0]
    assert ReasonCode.PUBLISHED_AT_INVALID in codes[1]
    assert ReasonCode.PROVENANCE_MISSING in codes[2]
    assert ReasonCode.COLLECTED_AT_INVALID in codes[3]


def test_unreadable_line_is_quarantined_and_reported(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        json.dumps(author_row(), ensure_ascii=False) + "\n{ broken json\n",
        encoding="utf-8",
    )
    report = run_intake(session_factory, path, dry_run=True)
    assert report.counts.rows == 2
    assert report.counts.accepted == 1
    assert report.counts.quarantined == 1
    assert report.rows[1].reason_codes == (ReasonCode.ROW_UNREADABLE,)


def test_created_source_stays_disabled_without_collector_config(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "news.jsonl", [news_row()])
    report = run_intake(session_factory, path, scope=EvidenceScope.NEWS, dry_run=False)
    assert report.counts.sources_created == 1
    with session_scope(session_factory) as session:
        source = session.scalars(sa.select(Source)).one()
        assert source.name == "manual-evidence-news"
        assert source.source_type is SourceType.NEWS
        assert source.enabled is False  # 绝不因收到证据文件而启用采集
        assert source.config_json is None  # 不注册采集器
        assert source.base_url is None


def test_existing_source_is_reused_without_modification(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with session_scope(session_factory) as session:
        session.add(
            Source(
                name="manual-evidence-author",
                source_type=SourceType.NEWS,
                timezone="UTC",
                enabled=True,
                config_json={"collector": "probe_collector"},
            )
        )
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    report = run_intake(session_factory, path, dry_run=False)
    assert report.counts.sources_created == 0
    with session_scope(session_factory) as session:
        source = session.scalars(sa.select(Source)).one()
        assert source.enabled is True
        assert source.config_json == {"collector": "probe_collector"}


def test_credentials_are_quarantined_and_never_stored_or_reported(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(
        tmp_path / "authors.jsonl",
        [author_row(api_key=SECRET), author_row(source_record_id="post-0002")],
    )
    report = run_intake(session_factory, path, dry_run=False)
    assert report.counts.quarantined == 1
    assert report.counts.persisted == 1
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert SECRET not in serialized
    assert SECRET not in report.render()
    assert report.rows[0].reason_codes == (ReasonCode.SENSITIVE_VALUE_DETECTED,)
    with session_scope(session_factory) as session:
        for raw in session.scalars(sa.select(RawItem)).all():
            assert SECRET not in json.dumps(raw.raw_json, ensure_ascii=False)


def test_record_without_availability_evidence_is_stored_but_excluded_from_oos(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(
        tmp_path / "authors.jsonl",
        [author_row(available_at="", availability_provenance="", availability_reference="")],
    )
    report = run_intake(session_factory, path, dry_run=False)
    assert report.counts.accepted == 1
    assert report.counts.oos_eligible == 0
    assert report.counts.not_oos_eligible == 1
    assert report.rows[0].not_oos_eligible_reason is ReasonCode.AVAILABILITY_UNPROVEN
    assert counts(session_factory)[0] == 1
    with session_scope(session_factory) as session:
        ledger = load_evidence_ledger(session)
        author = ledger.scope(EvidenceScope.AUTHOR)
        assert author.certified_records == 1
        assert author.oos_eligible_records == 0
        assert author.not_oos_eligible_records == 1


def test_ledger_counts_only_evidence_certified_records(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    # 普通导入（无证据块）与其它 item_type 都不计入
    with session_scope(session_factory) as session:
        source = Source(name="manual-plain", source_type=SourceType.NEWS, timezone="UTC")
        session.add(source)
        session.flush()
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id="plain-1",
                item_type=RawItemType.POST,
                raw_json={"id": "plain-1"},
                content_hash="0" * 64,
                published_at=None,
                collected_at=MOMENT,
                effective_at=MOMENT,
            )
        )
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id="macro-1",
                item_type=RawItemType.MACRO,
                raw_json={
                    "evidence": {
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                        "scope": "news",
                    }
                },
                content_hash="1" * 64,
                published_at=None,
                collected_at=MOMENT,
                effective_at=MOMENT,
            )
        )
    with session_scope(session_factory) as session:
        assert load_evidence_ledger(session).scope(EvidenceScope.NEWS).certified_records == 0
    path = write_jsonl(tmp_path / "news.jsonl", [news_row()])
    run_intake(session_factory, path, scope=EvidenceScope.NEWS, dry_run=False)
    with session_scope(session_factory) as session:
        ledger = load_evidence_ledger(session)
        news = ledger.scope(EvidenceScope.NEWS)
        assert news.certified_records == 1
        assert news.oos_eligible_records == 1
        assert news.evidence_start == datetime(2026, 9, 18, 8, 30, tzinfo=UTC)
        assert ledger.scope(EvidenceScope.AUTHOR).certified_records == 0


def test_qualification_report_sees_evidence_intake_but_never_unblocks(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    path = write_jsonl(tmp_path / "news.jsonl", [news_row()])
    run_intake(session_factory, path, scope=EvidenceScope.NEWS, dry_run=False)
    with session_scope(session_factory) as session:
        report = load_qualification_report(session, as_of=MOMENT)
    section = report.evidence_intake
    assert section.contract_version == EVIDENCE_CONTRACT_VERSION
    assert section.blocker_code == PHASE3_3_BLOCKER_CODE
    assert section.blocker_active is True
    checks = {check.key: check for check in section.checks}
    news_check = checks["news.evidence_intake_oos_eligible"]
    assert news_check.current == 1
    assert news_check.required == 200
    assert news_check.status.value == "BLOCKED"
    assert news_check.comparator == ">="
    author_check = checks["author.evidence_intake_oos_eligible"]
    assert author_check.current == 0
    # 无论证据入口状态如何，blocker 都保持 active、报告都不 ready
    assert report.blocker_active is True
    assert report.ready is False
    assert report.to_dict()["evidence_intake"]["blocker_active"] is True
    assert PHASE3_3_BLOCKER_CODE in report.to_dict()["blocker_code"]


def test_qualification_report_without_evidence_intake_stays_blocked(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        report = load_qualification_report(session, as_of=MOMENT)
    assert report.evidence_intake.blocker_active is True
    assert all(check.status.value == "BLOCKED" for check in report.evidence_intake.checks)
    assert all(check.current == 0 for check in report.evidence_intake.checks)


def test_cli_dry_run_writes_nothing_and_commit_writes_artifacts(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    manifest = tmp_path / "manifest.json"
    quarantine = tmp_path / "quarantine.jsonl"
    args = [
        "--scope",
        "author",
        "--input",
        str(path),
        "--as-of",
        MOMENT.isoformat(),
        "--manifest",
        str(manifest),
        "--quarantine",
        str(quarantine),
    ]
    assert main(args, session_factory=session_factory) == EXIT_OK
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert not manifest.exists()
    assert not quarantine.exists()
    assert counts(session_factory) == (0, 0, 0)

    commit = [*args, "--no-dry-run", "--json"]
    assert main(commit, session_factory=session_factory) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["counts"]["persisted"] == 1
    assert manifest.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["counts"]["persisted"] == 1
    assert not quarantine.exists()  # 无隔离行时不创建文件
    assert counts(session_factory) == (1, 1, 1)


def test_cli_writes_quarantine_file_with_reason_codes(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    path = write_jsonl(
        tmp_path / "authors.jsonl",
        [author_row(), author_row(source_record_id="post-0002", authorization_status="DENIED")],
    )
    quarantine = tmp_path / "quarantine.jsonl"
    code = main(
        [
            "--scope",
            "author",
            "--input",
            str(path),
            "--as-of",
            MOMENT.isoformat(),
            "--no-dry-run",
            "--quarantine",
            str(quarantine),
        ],
        session_factory=session_factory,
    )
    assert code == EXIT_QUARANTINED
    assert quarantine.exists()
    entry = json.loads(quarantine.read_text(encoding="utf-8").strip())
    assert entry["reason_codes"] == ["AUTHORIZATION_MISSING"]
    assert entry["status"] == "QUARANTINED"
    assert entry["contract_version"] == EVIDENCE_CONTRACT_VERSION
    assert counts(session_factory)[0] == 1
    capsys.readouterr()


def test_cli_exit_codes_and_argument_validation(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert (
        main(
            ["--scope", "author", "--input", str(empty), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_NO_ROWS
    )
    missing = tmp_path / "missing.jsonl"
    assert (
        main(
            ["--scope", "author", "--input", str(missing), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_INPUT_ERROR
    )
    unknown = tmp_path / "authors.dat"
    unknown.write_text('{"source": "s"}\n', encoding="utf-8")
    assert (
        main(
            ["--scope", "author", "--input", str(unknown), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_INPUT_ERROR
    )
    capsys.readouterr()

    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    with pytest.raises(SystemExit) as conflict:
        main(
            ["--scope", "author", "--input", str(path), "--dry-run", "--no-dry-run"],
            session_factory=session_factory,
        )
    assert conflict.value.code == 2
    with pytest.raises(SystemExit) as naive:
        main(
            ["--scope", "author", "--input", str(path), "--as-of", "2026-09-22T12:00:00"],
            session_factory=session_factory,
        )
    assert naive.value.code == 2
    with pytest.raises(SystemExit) as overwrite:
        main(
            ["--scope", "author", "--input", str(path), "--manifest", str(path)],
            session_factory=session_factory,
        )
    assert overwrite.value.code == 2
    with pytest.raises(SystemExit) as same_target:
        main(
            [
                "--scope",
                "author",
                "--input",
                str(path),
                "--manifest",
                str(tmp_path / "same.json"),
                "--quarantine",
                str(tmp_path / "same.json"),
            ],
            session_factory=session_factory,
        )
    assert same_target.value.code == 2
    capsys.readouterr()


def test_cli_accepts_csv_input_with_handover_column_names(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    headers = [
        "source_name",
        "post_id",
        "content",
        "published",
        "collected",
        "available_at",
        "availability_provenance",
        "availability_reference",
        "provenance_reference",
        "authorization_status",
        "authorization_basis",
        "authorization_reference",
        "authorization_reviewed_by",
        "authorization_reviewed_at",
        "permits_automated_collection",
        "permits_local_storage",
        "permits_research_use",
    ]
    values = [
        "manual-evidence-news",
        "news-0001",
        "金价短线回落",
        "2026-09-18T08:00:00+00:00",
        "2026-09-18T09:00:00+00:00",
        "2026-09-18T08:30:00+00:00",
        "provider_archive_export",
        "https://example.com/archive/news-0001",
        "https://example.com/news/news-0001",
        "APPROVED",
        "license_agreement",
        "https://example.com/terms/news-license",
        "合规复核人-李四",
        "2026-09-18T00:00:00+00:00",
        "true",
        "true",
        "true",
    ]
    csv_path = tmp_path / "news.csv"
    csv_path.write_text(",".join(headers) + "\n" + ",".join(values) + "\n", encoding="utf-8-sig")
    code = main(
        [
            "--scope",
            "news",
            "--input",
            str(csv_path),
            "--as-of",
            MOMENT.isoformat(),
            "--no-dry-run",
        ],
        session_factory=session_factory,
    )
    assert code == EXIT_OK
    assert counts(session_factory) == (1, 1, 1)
    capsys.readouterr()





