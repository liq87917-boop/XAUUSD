"""证据就绪度 / preflight / 一键资格复核端到端测试（GOLD-006）。

全部使用**临时 SQLite + 临时文件 + Mock 输入**，零网络：
- 空库 → 就绪度全 BLOCKED 且 remaining gap 准确；
- 模板示例文件经 preflight 全被 ``SYNTHETIC_EVIDENCE`` 隔离、台账与 DB 均为 0；
- 授权缺失 / 重复 / 内容冲突在批次量化里可区分；
- preflight 默认零写入（不建 source、不写 raw_items）；
- 真实达标记录也只让 **量化门槛** PASS，``blocker_active`` 恒为 True；
- 报告不回显 token / API key / Authorization / source config / 正文。
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
from scripts.evidence_readiness import EXIT_INPUT_ERROR, EXIT_NO_ROWS, EXIT_OK, main
from scripts.intake_evidence import main as intake_main
from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.hashing import content_hash
from src.evidence import EVIDENCE_CONTRACT_VERSION
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"


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
        "authorization_reviewed_at": "2026-09-17T10:00:00+00:00",
        "authorization_valid_from": "2026-09-16T00:00:00+00:00",
        "authorization_expires_at": "2027-09-16T00:00:00+00:00",
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


def insert_news_evidence(
    session: Session,
    *,
    source_name: str,
    count: int,
    days: int,
    oos_eligible: bool = True,
) -> None:
    """直接写 ``raw_items``（Mock 证据块），用于构造台账侧的覆盖率 / 集中度场景。"""
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
                        "oos_eligible": oos_eligible,
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


def test_preflight_empty_db_is_blocked_and_writes_nothing(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    code = main(
        ["preflight", "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["ready"] is False
    assert payload["batches"] == []
    scopes = {item["scope"]: item for item in payload["scopes"]}
    assert set(scopes) == {"author", "news"}
    assert scopes["author"]["eligible_count"] == 0
    assert scopes["news"]["eligible_count"] == 0
    assert scopes["news"]["coverage_days"] == 0
    assert scopes["news"]["max_source_share"] == 0.0
    gaps = {check["key"]: check["remaining"] for check in scopes["news"]["checks"]}
    assert gaps["news.evidence_intake_oos_eligible"] == MIN_NEWS_EVENTS
    assert gaps["news.evidence_intake_coverage_days"] == MIN_NEWS_HISTORY_DAYS
    assert row_counts(session_factory) == (0, 0)


def test_recheck_empty_db_json_exposes_gate_and_blocker(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    code = main(
        ["recheck", "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == "phase33_qualification_recheck"
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["ready"] is False
    assert payload["gate"]["qualification_ready"] is False
    assert payload["gate"]["qualification_blocked_count"] >= 1
    assert payload["gate"]["readiness_ready"] is False
    qualification = payload["qualification"]
    keys = {check["key"] for check in qualification["checks"]}
    assert "author.source_authorization" in keys
    assert "news.available_history_evidence" in keys
    # 串联现有 qualification 报告 + 证据入口子报告
    assert qualification["evidence_intake"]["blocker_active"] is True



def test_preflight_template_rows_are_quarantined_and_never_counted(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    template = repo_root / "examples" / "evidence" / "author_evidence_template.csv"
    code = main(
        [
            "preflight",
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
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    batch = payload["batches"][0]
    assert batch["rows"] == 2
    assert batch["accepted"] == 0
    assert batch["quarantined"] == 2
    assert batch["oos_eligible"] == 0
    codes = {item["reason_code"]: item["count"] for item in batch["reason_code_counts"]}
    assert codes["SYNTHETIC_EVIDENCE"] == 2
    scopes = {item["scope"]: item for item in payload["scopes"]}
    assert scopes["author"]["eligible_count"] == 0
    assert scopes["author"]["certified_count"] == 0
    assert row_counts(session_factory) == (0, 0)  # dry-run 零写入


def test_preflight_reports_duplicate_conflict_and_authorization_gap(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    seeded = write_jsonl(
        tmp_path / "seed.jsonl",
        [news_row(), news_row(source_record_id="news-0003")],
    )
    assert (
        intake_main(
            [
                "--scope",
                "news",
                "--input",
                str(seeded),
                "--as-of",
                MOMENT.isoformat(),
                "--no-dry-run",
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    candidate = write_jsonl(
        tmp_path / "candidate.jsonl",
        [
            # 库内同 ID 但内容不同 → IDENTITY_CONFLICT（拒绝覆盖历史事实）
            news_row(content="金价完全不同的改写内容，用于触发内容冲突。"),
            # 库内同 ID 且内容一致 → DUPLICATE（幂等重放）
            news_row(source_record_id="news-0003"),
            # 授权未核验 → AUTHORIZATION_MISSING 隔离
            news_row(source_record_id="news-0002", authorization_status="PENDING"),
        ],
    )
    code = main(
        [
            "preflight",
            "--scope",
            "news",
            "--input",
            str(candidate),
            "--json",
            "--as-of",
            MOMENT.isoformat(),
        ],
        session_factory=session_factory,
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    batch = payload["batches"][0]
    assert batch["rows"] == 3
    assert batch["duplicate"] == 1
    assert batch["conflict"] == 1
    assert batch["accepted"] == 0
    codes = {item["reason_code"]: item["count"] for item in batch["reason_code_counts"]}
    assert codes["DUPLICATE"] == 1
    assert codes["IDENTITY_CONFLICT"] == 1
    assert codes["AUTHORIZATION_MISSING"] == 1
    assert row_counts(session_factory)[0] == 2  # 预置的 2 条，preflight 未新增


def test_preflight_valid_input_is_dry_run_and_zero_write(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(tmp_path / "candidate.jsonl", [author_row()])
    code = main(
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
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    batch = payload["batches"][0]
    assert batch["accepted"] == 1
    assert batch["oos_eligible"] == 1
    assert row_counts(session_factory) == (0, 0)
    scopes = {item["scope"]: item for item in payload["scopes"]}
    assert scopes["author"]["eligible_count"] == 0  # 未提交 → 台账仍为 0



def test_recheck_after_authorized_intake_counts_but_stays_blocked(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    committed = 3
    rows = [
        author_row(source_record_id=f"post-{index:04d}")
        for index in range(1, committed + 1)
    ]
    seeded = write_jsonl(tmp_path / "authors.jsonl", rows)
    assert (
        intake_main(
            [
                "--scope",
                "author",
                "--input",
                str(seeded),
                "--as-of",
                MOMENT.isoformat(),
                "--no-dry-run",
            ],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    code = main(
        ["recheck", "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    scopes = {item["scope"]: item for item in payload["readiness"]["scopes"]}
    author = scopes["author"]
    assert author["eligible_count"] == committed
    assert author["ready"] is False
    check = next(
        item for item in author["checks"] if item["key"] == "author.evidence_intake_oos_eligible"
    )
    assert check["current"] == committed
    assert check["required"] == MIN_AUTHOR_SAMPLES
    assert check["remaining"] == MIN_AUTHOR_SAMPLES - committed
    assert check["status"] == "BLOCKED"
    # 就绪度未达标 + 人工 Gate 恒 BLOCKED
    assert payload["blocker_active"] is True
    assert payload["ready"] is False
    evidence_intake = {
        item["key"]: item for item in payload["qualification"]["evidence_intake"]["checks"]
    }
    assert evidence_intake["author.evidence_intake_oos_eligible"]["current"] == committed


def test_recheck_news_quantitative_pass_never_unblocks(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    with session_factory() as session:
        for name in ("source-a", "source-b", "source-c"):
            insert_news_evidence(
                session, source_name=name, count=100, days=MIN_NEWS_HISTORY_DAYS
            )
        session.commit()
    code = main(
        ["recheck", "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    news = {item["scope"]: item for item in payload["readiness"]["scopes"]}["news"]
    assert news["eligible_count"] == 300
    assert news["coverage_days"] == MIN_NEWS_HISTORY_DAYS
    assert news["max_source_share"] <= MAX_NEWS_SOURCE_SHARE
    assert news["ready"] is True
    assert news["remaining_gap_count"] == 0
    # 量化门槛 PASS 仍不解除 blocker（人工 Gate 恒 BLOCKED）
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["qualification"]["ready"] is False
    assert payload["ready"] is False


def test_recheck_never_leaks_credentials_or_source_config(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    with session_factory() as session:
        source = ensure_source(session, "manual-secret-source")
        source.config_json = {"api_key": SECRET, "Authorization": f"Bearer {SECRET}"}
        session.add(
            RawItem(
                source_id=source.id,
                source_record_id="news-secret-0001",
                item_type=RawItemType.NEWS,
                title="gold",
                content_text=f"正文不应出现在报告里 {SECRET}",
                raw_json={
                    "import_kind": "authorized_evidence_intake",
                    "evidence": {
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                        "scope": "news",
                        "source": f"manual-secret-source token={SECRET}",
                        "oos_eligible": True,
                        "available_at": WINDOW_START.isoformat(),
                    },
                },
                source_url=f"https://example.com/news?api_key={SECRET}",
                content_hash=content_hash("news-secret-0001", "gold"),
                published_at=WINDOW_START,
                collected_at=WINDOW_START,
                effective_at=WINDOW_START,
            )
        )
        session.commit()
    code = main(
        ["recheck", "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "Bearer " + SECRET not in out
    payload = json.loads(out)
    sources = {
        item["source"]
        for scope in payload["readiness"]["scopes"]
        for item in scope["source_counts"]
    }
    assert sources == {"manual-secret-source token=***"}  # 只出现脱敏后的来源名
    assert payload["readiness"]["scopes"][1]["eligible_count"] == 1


def test_recheck_report_is_written_only_with_no_dry_run(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    report = tmp_path / "recheck.json"
    assert (
        main(
            ["recheck", "--json", "--report", str(report), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert not report.exists()  # 默认 dry-run：零写入
    assert (
        main(
            [
                "recheck",
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



def test_cli_argument_and_input_errors(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    path = write_jsonl(tmp_path / "authors.jsonl", [author_row()])
    with pytest.raises(SystemExit) as missing_scope:
        main(["preflight", "--input", str(path)], session_factory=session_factory)
    assert missing_scope.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive:
        main(
            ["preflight", "--scope", "author", "--as-of", "2026-09-22T12:00:00"],
            session_factory=session_factory,
        )
    assert naive.value.code == 2
    capsys.readouterr()
    unknown = tmp_path / "authors.dat"
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


def test_template_command_prints_and_writes_without_overwrite(
    tmp_path: Path, capsys: Any
) -> None:
    assert main(["template", "--scope", "author"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "record_kind" in captured.out
    assert "is_mock" in captured.out
    target = tmp_path / "author_template.csv"
    assert main(["template", "--scope", "author", "--out", str(target)]) == EXIT_OK
    capsys.readouterr()
    content = target.read_text(encoding="utf-8")
    assert "record_kind,is_mock" in content.splitlines()[0]
    with pytest.raises(SystemExit) as refused:
        main(["template", "--scope", "author", "--out", str(target)])
    assert refused.value.code == 2
    capsys.readouterr()
    assert main(["template", "--scope", "author", "--out", str(target), "--overwrite"]) == EXIT_OK
    capsys.readouterr()


def test_preflight_markdown_report_is_human_readable(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    assert (
        main(["preflight", "--as-of", MOMENT.isoformat()], session_factory=session_factory)
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "证据就绪度（Evidence Readiness）报告" in out
    assert PHASE3_3_BLOCKER_CODE in out
    assert "human_gate_required=true" in out
    assert "缺口" in out
    assert "不解除" in out

