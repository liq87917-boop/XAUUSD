"""GOLD-008 Evidence 人工交接包端到端测试。

全部使用**临时 SQLite + 临时文件 + Mock 输入**，零网络：
- 空库：退出码诚实 BLOCKED、JSON 保持 blocker_active / ready_for_human_review=false、零写入；
- 候选文件只做 dry-run：隔离原因码计数可见、数据库零写入、禁止自动 intake；
- `--out` 是唯一显式写文件开关，默认只打印 stdout；
- 达标（Mock 证据块直接入库）时最多 ``ready_for_human_review=true``，仍要求人工 Gate；
- 参数 / 输入错误退出码，且敏感字段（token / 正文）不进入交接包输出。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import RawItem, Source
from database.models.enums import RawItemType, SourceType
from scripts.evidence_handoff import (
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_NO_ROWS,
    EXIT_OK,
    main,
)
from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.hashing import content_hash
from src.evidence import EVIDENCE_CONTRACT_VERSION, HANDOFF_REPORT_NAME
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
NEWS_START = datetime(2026, 6, 1, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def insert_evidence(
    session: Session, *, scope: str, source_name: str, count: int, span_days: int
) -> None:
    """直接写 ``raw_items``（Mock 证据块），构造 Author / News 达标场景。"""
    source = ensure_source(session, source_name)
    item_type = RawItemType.POST if scope == "author" else RawItemType.NEWS
    span = timedelta(days=span_days)
    denominator = max(count - 1, 1)
    for index in range(count):
        available = NEWS_START + span * (index / denominator)
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id=f"{source_name}-{index:04d}",
                item_type=item_type,
                title="gold evidence",
                content_text="黄金证据",
                raw_json={
                    "import_kind": "authorized_evidence_intake",
                    "evidence": {
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                        "scope": scope,
                        "source": source_name,
                        "oos_eligible": True,
                        "available_at": available.isoformat(),
                    },
                },
                source_url="https://example.com/evidence",
                content_hash=content_hash(f"{source_name}-{index}", "黄金证据"),
                published_at=available,
                collected_at=available,
                effective_at=available,
            )
        )
    session.flush()


def seed_balanced_news(session: Session, *, count: int = MIN_NEWS_EVENTS) -> None:
    """三条来源均衡分布（单源占比 ~33%，满足 <=40%）。"""
    sources = ("news-src-a", "news-src-b", "news-src-c")
    for name in sources:
        ensure_source(session, name)
    span = timedelta(days=MIN_NEWS_HISTORY_DAYS)
    denominator = max(count - 1, 1)
    for index in range(count):
        name = sources[index % len(sources)]
        source = ensure_source(session, name)
        available = NEWS_START + span * (index / denominator)
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id=f"{name}-{index:04d}",
                item_type=RawItemType.NEWS,
                title="gold news evidence",
                content_text="黄金快讯",
                raw_json={
                    "import_kind": "authorized_evidence_intake",
                    "evidence": {
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                        "scope": "news",
                        "source": name,
                        "oos_eligible": True,
                        "available_at": available.isoformat(),
                    },
                },
                source_url="https://example.com/news",
                content_hash=content_hash(f"{name}-{index}", "黄金快讯"),
                published_at=available,
                collected_at=available,
                effective_at=available,
            )
        )
    session.flush()


def author_row(**overrides: Any) -> dict[str, Any]:
    """完全合规的 Author 证据行（含独立历史可用证据）。"""
    row: dict[str, Any] = {
        "source": "manual-evidence-author",
        "source_record_id": "handoff-post-0001",
        "author_name": "作者甲",
        "external_account_id": "handoff-acct-001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350，本周维持逢高做空思路。",
        "published_at": "2026-09-18T08:30:00+08:00",
        "collected_at": "2026-09-18T09:10:00+08:00",
        "available_at": "2026-09-18T09:05:00+08:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://example.com/archive/2026-09-18",
        "provenance_reference": "https://example.com/posts/handoff-post-0001",
        "url": "https://example.com/posts/handoff-post-0001",
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


def test_empty_db_is_blocked_and_zero_writes(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    assert (
        main(["--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory)
        == EXIT_BLOCKED
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == HANDOFF_REPORT_NAME
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["status"] == "BLOCKED"
    assert payload["quantified_thresholds_met"] is False
    assert payload["ready_for_human_review"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["author"]["eligible"] == 0
    assert payload["news"]["source_share_evaluable"] is False
    assert payload["checklist"]
    assert payload["excluded_evidence"]
    assert row_counts(session_factory) == (0, 0)


def test_out_is_the_only_explicit_write_switch(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    target = tmp_path / "handoff.json"
    assert (
        main(["--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory)
        == EXIT_BLOCKED
    )
    capsys.readouterr()
    assert not target.exists()  # 默认只打印 stdout：零写入
    assert (
        main(
            ["--json", "--out", str(target), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_BLOCKED
    )
    captured = capsys.readouterr()
    assert target.exists()
    assert target.read_text(encoding="utf-8") == captured.out
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "BLOCKED"


def test_candidate_dry_run_reports_quarantine_reasons_without_writes(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(
        tmp_path / "author.jsonl",
        [
            author_row(),
            {"source": "template-src", "source_record_id": "t-1", "record_kind": "example"},
            {"source": "noauth-src", "source_record_id": "n-1", "content": "黄金看多"},
        ],
    )
    assert (
        main(
            [
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
        == EXIT_BLOCKED
    )
    payload = json.loads(capsys.readouterr().out)
    codes = {item["reason_code"] for item in payload["quarantine_reason_counts"]}
    assert "SYNTHETIC_EVIDENCE" in codes
    assert "AUTHORIZATION_MISSING" in codes
    assert payload["batch"] is not None
    assert payload["batch"]["quarantined"] == 2
    assert payload["batch"]["accepted"] == 1
    # dry-run：绝不落库、绝不建立来源
    assert row_counts(session_factory) == (0, 0)


def test_thresholds_met_requires_human_review(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    with session_factory() as session:
        insert_evidence(
            session,
            scope="author",
            source_name="author-evidence-src",
            count=MIN_AUTHOR_SAMPLES,
            span_days=0,
        )
        seed_balanced_news(session)
        session.commit()
    before = row_counts(session_factory)
    assert (
        main(["--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory)
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["author"]["eligible"] == MIN_AUTHOR_SAMPLES
    assert payload["news"]["eligible"] == MIN_NEWS_EVENTS
    assert payload["news"]["coverage_days"] >= MIN_NEWS_HISTORY_DAYS
    assert payload["quantified_thresholds_met"] is True
    assert payload["ready_for_human_review"] is True
    assert payload["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    # 达标也只到人工复核：仍不解除 blocker、不自动切 Phase
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert row_counts(session_factory) == before


def test_sensitive_values_never_leak(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(
        tmp_path / "author.jsonl",
        [
            author_row(
                source_record_id="handoff-secret-0001",
                content=f"黄金看多，附带 Authorization: Bearer {SECRET}",
                note=f"api_key={SECRET}",
            )
        ],
    )
    assert (
        main(
            [
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
        == EXIT_BLOCKED
    )
    out = capsys.readouterr().out
    assert SECRET not in out
    assert row_counts(session_factory) == (0, 0)


def test_argument_and_input_errors(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(tmp_path / "author.jsonl", [author_row()])
    with pytest.raises(SystemExit) as scope_only:
        main(["--scope", "author"], session_factory=session_factory)
    assert scope_only.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as input_only:
        main(["--input", str(candidate)], session_factory=session_factory)
    assert input_only.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as same_file:
        main(
            ["--scope", "author", "--input", str(candidate), "--out", str(candidate)],
            session_factory=session_factory,
        )
    assert same_file.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive:
        main(["--as-of", "2026-09-22T12:00:00"], session_factory=session_factory)
    assert naive.value.code == 2
    capsys.readouterr()
    unknown = tmp_path / "author.dat"
    unknown.write_text('{"source": "s"}\n', encoding="utf-8")
    assert (
        main(
            ["--scope", "author", "--input", str(unknown)],
            session_factory=session_factory,
        )
        == EXIT_INPUT_ERROR
    )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert (
        main(
            ["--scope", "author", "--input", str(empty)],
            session_factory=session_factory,
        )
        == EXIT_NO_ROWS
    )
    capsys.readouterr()

