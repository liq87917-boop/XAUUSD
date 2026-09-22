"""GOLD-007 单入口 Evidence Operator 工作流端到端测试。

全部使用**临时 SQLite + 临时文件 + Mock 输入**，零网络：
- 单入口 workflow 默认 dry-run：零写入、串联 preflight / quarantine / recheck，blocker 恒 active；
- 模板示例经写入门禁与隔离摘要判 ``SYNTHETIC_EVIDENCE``，永不落库、永不进台账；
- 未授权 / 时间不足 / 内容冲突只能隔离，绝不覆盖历史事实；
- 显式 intake 幂等（重复导入只产 ``DUPLICATE``），台账只计 ``ACCEPTED`` + OOS eligible；
- 作者归属链 gateway-only：普通 CSV（``import_real_posts.py`` 口径）不是候选；
  只有 gateway 证据才能进作者链且幂等，身份冲突跳过且不覆盖；
- News coverage / source-share 缺口可量化；报告不回显凭据 / source config / 正文。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import Author, AuthorAccount, AuthorPost, RawItem, Source
from database.models.enums import RawItemType, SourceType
from scripts.evidence_operator import (
    EXIT_INPUT_ERROR,
    EXIT_NO_ROWS,
    EXIT_OK,
    EXIT_QUARANTINED,
    WORKFLOW_REPORT_NAME,
    main,
)
from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.hashing import content_hash
from src.evidence import EVIDENCE_CONTRACT_VERSION, OPERATOR_STEPS, load_evidence_ledger
from src.evidence.ledger import EvidenceLedger
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"
REPO_ROOT = Path(__file__).resolve().parents[2]


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def template_row(scope: str = "author") -> dict[str, str]:
    """读取仓库内模板的示例行（带 `record_kind=example` / `is_mock=true` 标记）。"""
    path = REPO_ROOT / "examples" / "evidence" / f"{scope}_evidence_template.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return dict(next(csv.DictReader(handle)))


def row_counts(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as session:
        raw = session.scalar(sa.select(sa.func.count()).select_from(RawItem))
        sources = session.scalar(sa.select(sa.func.count()).select_from(Source))
    return int(raw or 0), int(sources or 0)


def ensure_source(session: Session, name: str) -> Source:
    existing = session.scalar(sa.select(Source).where(Source.name == name))
    if existing is not None:
        return existing
    source = Source(name=name, source_type=SourceType.NEWS, timezone="UTC", enabled=False)
    session.add(source)
    session.flush()
    return source


def ledger(factory: sessionmaker[Session]) -> EvidenceLedger:
    with factory() as session:
        return load_evidence_ledger(session)


def author_post_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.scalar(sa.select(sa.func.count()).select_from(AuthorPost)) or 0)


def raw_content_hash(factory: sessionmaker[Session], source_name: str, record_id: str) -> str:
    with factory() as session:
        value = session.scalar(
            sa.select(RawItem.content_hash)
            .join(Source, Source.id == RawItem.source_id)
            .where(Source.name == source_name, RawItem.source_record_id == record_id)
        )
    assert value is not None
    return str(value)


def insert_plain_csv_post(session: Session, *, source_name: str, record_id: str) -> None:
    """模拟 ``scripts/import_real_posts.py`` 的普通 CSV 落库（无证据块）。"""
    source = ensure_source(session, source_name)
    moment = MOMENT - timedelta(days=1)
    session.add(
        RawItem(
            source_id=source.id,
            source_record_id=record_id,
            item_type=RawItemType.POST,
            content_text="普通 CSV 导入的正文（无证据块）",
            raw_json={
                "import_kind": "manual_real_corpus",
                "author_name": "作者甲",
                "language": "zh",
                "collected_at_provenance": "input",
            },
            source_url="https://example.com/posts/plain",
            content_hash=content_hash("plain", record_id),
            published_at=moment,
            collected_at=moment,
            effective_at=moment,
        )
    )
    session.flush()


def insert_news_evidence(session: Session, *, source_name: str, count: int, days: int) -> None:
    """直接写 ``raw_items``（Mock 证据块），构造 News 覆盖天数 / 单源占比场景。"""
    source = ensure_source(session, source_name)
    span = timedelta(days=days)
    denominator = max(count - 1, 1)
    for index in range(count):
        available = WINDOW_START + span * (index / denominator)
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id=f"{source_name}-{index:04d}",
                item_type=RawItemType.NEWS,
                title="gold news evidence",
                content_text="黄金快讯",
                raw_json={
                    "import_kind": "authorized_evidence_intake",
                    "evidence": {
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                        "scope": "news",
                        "source": source_name,
                        "oos_eligible": True,
                        "available_at": available.isoformat(),
                    },
                },
                source_url="https://example.com/news",
                content_hash=content_hash(f"{source_name}-{index}", "黄金快讯"),
                published_at=available,
                collected_at=available,
                effective_at=available,
            )
        )
    session.flush()


def test_workflow_default_dry_run_reports_blocker_and_writes_nothing(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    assert (
        main(
            ["workflow", "--json", "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == WORKFLOW_REPORT_NAME
    assert payload["schema_version"] == 1
    assert payload["steps"] == list(OPERATOR_STEPS)
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["ready"] is False
    assert payload["dry_run"] is True
    assert payload["write_gate"] is None
    assert payload["intake"] is None
    assert payload["author_chain"] is None
    assert payload["qualification"]["blocker_active"] is True
    scopes = {item["scope"]: item for item in payload["preflight"]["readiness"]["scopes"]}
    assert set(scopes) == {"author", "news"}
    assert scopes["author"]["eligible_count"] == 0
    templates = {item["scope"]: item for item in payload["template"]["files"]}
    assert templates["author"]["exists"] is True
    assert templates["news"]["exists"] is True
    assert row_counts(session_factory) == (0, 0)


def test_workflow_report_is_dry_run_by_default(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    report = tmp_path / "workflow.json"
    assert (
        main(
            ["workflow", "--json", "--report", str(report), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert not report.exists()  # 默认 dry-run：零写入
    assert (
        main(
            [
                "workflow",
                "--json",
                "--report",
                str(report),
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["blocker_active"] is True


def test_workflow_template_file_gate_and_quarantine_never_count(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    template = REPO_ROOT / "examples" / "evidence" / "author_evidence_template.csv"
    assert (
        main(
            [
                "workflow",
                "--scope",
                "author",
                "--input",
                str(template),
                "--json",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["write_gate"]
    assert (gate["rows"], gate["accepted"], gate["quarantined"]) == (2, 0, 2)
    assert gate["synthetic"] == 2
    assert gate["write_allowed"] is False
    quarantine = payload["quarantine"]
    assert quarantine["total_quarantined"] == 2
    codes = {item["reason_code"]: item["count"] for item in quarantine["reason_code_counts"]}
    assert codes["SYNTHETIC_EVIDENCE"] == 2
    scopes = {item["scope"]: item for item in payload["preflight"]["readiness"]["scopes"]}
    assert scopes["author"]["eligible_count"] == 0
    assert scopes["author"]["certified_count"] == 0
    assert row_counts(session_factory) == (0, 0)


def test_intake_dry_run_zero_write_and_gate_flags(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(
        tmp_path / "author.jsonl",
        [
            author_row(source_record_id="post-0001", authorization_status="PENDING"),
            author_row(source_record_id="post-0002", published_at="2026-09-18 08:30:00"),
        ],
    )
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--json",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_QUARANTINED
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == "evidence_operator_intake"
    assert payload["dry_run"] is True
    gate = payload["write_gate"]
    assert gate["accepted"] == 0
    assert gate["authorization_incomplete"] == 1
    assert gate["time_invalid"] == 1
    assert gate["write_allowed"] is False
    assert payload["intake"]["counts"]["persisted"] == 0
    assert row_counts(session_factory) == (0, 0)


def test_intake_explicit_commit_gate_ledger_and_idempotency(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    valid = [author_row(source_record_id=f"post-{index:04d}") for index in range(1, 4)]
    unauthorized = author_row(source_record_id="post-9000", authorization_status="DENIED")
    mixed = write_jsonl(tmp_path / "mixed.jsonl", [*valid, template_row("author"), unauthorized])
    manifest = tmp_path / "manifest.json"
    quarantine_file = tmp_path / "quarantine.jsonl"
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(mixed),
                "--json",
                "--no-dry-run",
                "--manifest",
                str(manifest),
                "--quarantine",
                str(quarantine_file),
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_QUARANTINED
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    gate = payload["write_gate"]
    assert (gate["accepted"], gate["quarantined"]) == (3, 2)
    assert gate["synthetic"] == 1
    # 模板示例行同时命中 AUTHORIZATION_MISSING；未授权行也是 AUTHORIZATION_MISSING
    assert gate["authorization_incomplete"] == 2
    assert payload["intake"]["counts"]["persisted"] == 3
    assert row_counts(session_factory)[0] == 3
    assert manifest.exists()
    entries = quarantine_file.read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    journal = ledger(session_factory)
    assert journal.scope("author").certified_records == 3
    assert journal.scope("author").oos_eligible_records == 3
    assert journal.scope("news").certified_records == 0

    # 幂等：同一批次重复显式导入只产 DUPLICATE，原始层与台账不增长
    only_valid = write_jsonl(tmp_path / "valid.jsonl", valid)
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(only_valid),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    again = json.loads(capsys.readouterr().out)
    assert again["write_gate"]["duplicate"] == 3
    assert again["write_gate"]["accepted"] == 0
    assert again["intake"]["counts"]["persisted"] == 0
    assert row_counts(session_factory)[0] == 3
    assert ledger(session_factory).scope("author").certified_records == 3


def test_intake_identity_conflict_is_quarantined_without_overwrite(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    seed = write_jsonl(tmp_path / "seed.jsonl", [author_row(source_record_id="post-0001")])
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(seed),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    original = raw_content_hash(session_factory, "manual-evidence-author", "post-0001")
    conflict = write_jsonl(
        tmp_path / "conflict.jsonl",
        [author_row(source_record_id="post-0001", content="完全不同的改写内容（触发冲突）")],
    )
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(conflict),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_QUARANTINED
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["write_gate"]["conflict"] == 1
    assert payload["write_gate"]["accepted"] == 0
    assert {item["reason_code"] for item in payload["quarantine"]["reason_code_counts"]} == {
        "IDENTITY_CONFLICT"
    }
    assert row_counts(session_factory)[0] == 1  # 未覆盖、未新增
    assert raw_content_hash(session_factory, "manual-evidence-author", "post-0001") == original


def test_author_chain_ignores_plain_csv_imports(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    with session_factory() as session:
        insert_plain_csv_post(session, source_name="manual-real-corpus", record_id="plain-1")
        session.commit()
    assert (
        main(
            ["author-chain", "--json", "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == 0
    assert payload["attributed"] == 0
    assert payload["notes"]
    assert author_post_count(session_factory) == 0


def test_author_chain_consumes_gateway_evidence_and_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    seed = write_jsonl(tmp_path / "authors.jsonl", [author_row(source_record_id="post-0001")])
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(seed),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    # 默认 dry-run：零写入
    assert (
        main(
            ["author-chain", "--json", "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["candidates"] == 1
    assert dry["attributed"] == 1
    assert author_post_count(session_factory) == 0
    # 显式提交
    assert (
        main(
            [
                "author-chain",
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    committed = json.loads(capsys.readouterr().out)
    assert committed["dry_run"] is False
    assert committed["candidates"] == 1
    assert committed["attributed"] == 1
    assert committed["authors_created"] == 1
    assert committed["accounts_created"] == 1
    assert author_post_count(session_factory) == 1
    with session_factory() as session:
        account = session.scalar(sa.select(AuthorAccount))
        assert account is not None
        assert account.enabled is False  # 只建立归属，不启用采集
        assert account.verified is False
    # 幂等：重复运行不新增归属
    assert (
        main(
            [
                "author-chain",
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    again = json.loads(capsys.readouterr().out)
    assert again["attributed"] == 0
    assert again["duplicate"] == 1
    assert author_post_count(session_factory) == 1


def test_author_chain_identity_conflict_is_skipped_not_overwritten(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    seed = write_jsonl(tmp_path / "authors.jsonl", [author_row(source_record_id="post-0001")])
    assert (
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(seed),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    with session_factory() as session:
        source = session.scalar(
            sa.select(Source).where(Source.name == "manual-evidence-author")
        )
        assert source is not None
        other = Author(display_name="另一个作者", canonical_name="other-author")
        session.add(other)
        session.flush()
        session.add(
            AuthorAccount(
                author_id=other.id,
                source_id=source.id,
                external_account_id="acct-001",
                account_name="另一个作者",
                enabled=False,
            )
        )
        session.commit()
    assert (
        main(
            [
                "author-chain",
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["identity_conflicts"] == 1
    assert payload["skipped"] == 1
    assert payload["attributed"] == 0
    assert payload["authors_created"] == 0
    assert author_post_count(session_factory) == 0
    with session_factory() as session:
        account = session.scalar(sa.select(AuthorAccount))
        assert account is not None
        owner = session.get(Author, account.author_id)
        assert owner is not None
        assert owner.display_name == "另一个作者"  # 历史归属未被覆盖


def test_workflow_author_chain_step_only_uses_gateway_evidence(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    with session_factory() as session:
        insert_plain_csv_post(session, source_name="manual-real-corpus", record_id="plain-1")
        session.commit()
    seed = write_jsonl(tmp_path / "authors.jsonl", [author_row(source_record_id="post-0001")])
    assert (
        main(
            [
                "workflow",
                "--scope",
                "author",
                "--input",
                str(seed),
                "--json",
                "--no-dry-run",
                "--author-chain",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["intake"]["counts"]["persisted"] == 1
    assert payload["author_chain"]["candidates"] == 1  # 普通 CSV 载荷不是候选
    assert payload["author_chain"]["attributed"] == 1
    assert payload["author_chain"]["dry_run"] is False
    assert author_post_count(session_factory) == 1


def test_workflow_news_coverage_and_source_share_gap(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    with session_factory() as session:
        for name in ("source-a", "source-b", "source-c"):
            insert_news_evidence(
                session, source_name=name, count=100, days=MIN_NEWS_HISTORY_DAYS
            )
        session.commit()
    assert (
        main(
            ["workflow", "--json", "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    scopes = {item["scope"]: item for item in payload["preflight"]["readiness"]["scopes"]}
    news = scopes["news"]
    assert news["eligible_count"] == 300
    assert news["coverage_days"] == MIN_NEWS_HISTORY_DAYS
    assert news["max_source_share"] <= MAX_NEWS_SOURCE_SHARE
    assert news["remaining_gap_count"] == 0
    assert news["source_counts"]
    # 量化门槛达标也不解除 blocker
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["ready"] is False
    assert payload["qualification"]["ready"] is False
    author = scopes["author"]
    checks = {check["key"]: check for check in author["checks"]}
    assert checks["author.evidence_intake_oos_eligible"]["remaining"] == MIN_AUTHOR_SAMPLES


def test_operator_never_leaks_credentials(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    row = author_row(
        source=f"manual-evidence-author token={SECRET}",
        authorization_reference=f"https://example.com/terms?api_key={SECRET}",
    )
    candidate = write_jsonl(tmp_path / "author.jsonl", [row])
    assert (
        main(
            [
                "workflow",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--json",
                "--no-dry-run",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    payload = json.loads(captured.out)
    assert payload["write_gate"]["sensitive_detected"] == 1
    assert payload["write_gate"]["accepted"] == 0
    assert row_counts(session_factory) == (0, 0)
    # 人类可读模式同样脱敏
    assert (
        main(
            [
                "workflow",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    text = capsys.readouterr().out
    assert SECRET not in text
    assert "token=***" in text


def test_single_step_commands_share_the_same_hardening(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(
        tmp_path / "author.jsonl", [template_row("author"), author_row()]
    )
    # quarantine：只输出隔离摘要（零写入）
    assert (
        main(
            [
                "quarantine",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--json",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["total_quarantined"] == 1
    assert summary["reason_code_counts"]
    assert row_counts(session_factory) == (0, 0)
    # preflight：写入门禁 + 只读就绪度（零写入）
    assert (
        main(
            [
                "preflight",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--json",
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["report"] == "evidence_operator_preflight"
    assert preflight["write_gate"]["accepted"] == 1
    assert preflight["write_gate"]["quarantined"] == 1
    assert preflight["blocker_active"] is True
    assert row_counts(session_factory) == (0, 0)
    # recheck：复用 evidence_readiness 的一键资格复核
    assert (
        main(
            ["recheck", "--json", "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    recheck = json.loads(capsys.readouterr().out)
    assert recheck["report"] == "phase33_qualification_recheck"
    assert recheck["blocker_active"] is True
    assert recheck["ready"] is False


def test_operator_argument_and_input_errors(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(tmp_path / "author.jsonl", [author_row()])
    with pytest.raises(SystemExit) as missing_scope:
        main(["preflight", "--input", str(candidate)], session_factory=session_factory)
    assert missing_scope.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as missing_input:
        main(["intake", "--scope", "author"], session_factory=session_factory)
    assert missing_input.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive:
        main(["workflow", "--as-of", "2026-09-22T12:00:00"], session_factory=session_factory)
    assert naive.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as same_file:
        main(
            [
                "intake",
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--manifest",
                str(candidate),
            ],
            session_factory=session_factory,
        )
    assert same_file.value.code == 2
    capsys.readouterr()
    unknown = tmp_path / "author.dat"
    unknown.write_text('{"source": "s"}\n', encoding="utf-8")
    assert (
        main(
            ["preflight", "--scope", "author", "--input", str(unknown)],
            session_factory=session_factory,
        )
        == EXIT_INPUT_ERROR
    )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert (
        main(
            ["preflight", "--scope", "author", "--input", str(empty)],
            session_factory=session_factory,
        )
        == EXIT_NO_ROWS
    )
    capsys.readouterr()
