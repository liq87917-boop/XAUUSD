"""GOLD-009 Evidence readiness 状态变更通知端到端测试。

全部使用**临时 SQLite + 临时文件 + Mock 输入**，零网络：

- 默认运行只读 / 零写入（不传 `--state` / `--out` / `--events`）；
- 只有显式 `--out` / `--events` 才原子落盘快照与事件；完全相同状态重复运行 → 0 事件；
- 缺口变化 / 原因码变化 / ready 双向变化产生脱敏事件；ready 事件仍要求 L3 人工 Gate；
- state 文件损坏 → 退出码 4 且**不写任何输出**；
- 敏感字段（token / Authorization）不进入 stdout、事件文件；
- 参数错误退出码 2，且数据库零写入。
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
from scripts.evidence_readiness_watch import (
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_NO_ROWS,
    EXIT_OK,
    EXIT_STATE_INVALID,
    main,
)
from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.hashing import content_hash
from src.evidence import EVIDENCE_CONTRACT_VERSION, WATCH_REPORT_NAME
from src.evidence.readiness_watch import WatchEventType
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=6)
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


def evidence_raw_json(scope: str, source_name: str, available: datetime) -> dict[str, Any]:
    return {
        "import_kind": "authorized_evidence_intake",
        "evidence": {
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "scope": scope,
            "source": source_name,
            "oos_eligible": True,
            "available_at": available.isoformat(),
        },
    }


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
                raw_json=evidence_raw_json(scope, source_name, available),
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
                raw_json=evidence_raw_json("news", name, available),
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
        "source_record_id": "watch-post-0001",
        "author_name": "作者甲",
        "external_account_id": "watch-acct-001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350，本周维持逢高做空思路。",
        "published_at": "2026-09-18T08:30:00+08:00",
        "collected_at": "2026-09-18T09:10:00+08:00",
        "available_at": "2026-09-18T09:05:00+08:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://example.com/archive/2026-09-18",
        "provenance_reference": "https://example.com/posts/watch-post-0001",
        "url": "https://example.com/posts/watch-post-0001",
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


def test_default_run_is_read_only_and_reports_first_snapshot(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    absent = tmp_path / "state.json"
    assert (
        main(
            ["--json", "--state", str(absent), "--as-of", MOMENT.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_BLOCKED
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == WATCH_REPORT_NAME
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["ready_for_human_review"] is False
    assert payload["changed"] is True
    assert payload["event_count"] == 1
    assert payload["events"][0]["event"] == WatchEventType.FIRST_SNAPSHOT.value
    assert payload["snapshot"]["status"] == "BLOCKED"
    assert payload["snapshot"]["scopes"]["author"]["eligible"] == 0
    # 只读：--state 指向不存在的文件时不创建、不落盘任何东西
    assert not absent.exists()
    assert row_counts(session_factory) == (0, 0)


def test_persistence_requires_explicit_flags_and_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    state = tmp_path / "state.json"
    events = tmp_path / "events.json"
    argv = [
        "--json",
        "--state",
        str(state),
        "--out",
        str(state),
        "--events",
        str(events),
        "--as-of",
        MOMENT.isoformat(),
    ]
    assert main(argv, session_factory=session_factory) == EXIT_BLOCKED
    first = json.loads(capsys.readouterr().out)
    assert first["event_count"] == 1
    assert state.exists() and events.exists()
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["fingerprint"] == first["fingerprint"]
    event_doc = json.loads(events.read_text(encoding="utf-8"))
    assert event_doc["event_count"] == 1
    assert event_doc["blocker_active"] is True
    assert event_doc["phase_transition_allowed"] is False

    # 完全相同状态重复运行：0 事件，快照指纹不变（幂等）
    assert main(argv, session_factory=session_factory) == EXIT_BLOCKED
    second = json.loads(capsys.readouterr().out)
    assert second["event_count"] == 0
    assert second["changed"] is False
    assert second["fingerprint"] == first["fingerprint"]
    assert json.loads(events.read_text(encoding="utf-8"))["event_count"] == 0
    assert json.loads(state.read_text(encoding="utf-8"))["fingerprint"] == first["fingerprint"]
    # 原子写：无残留临时文件；数据库零写入
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []
    assert row_counts(session_factory) == (0, 0)
def test_gap_change_between_runs_emits_event(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    state = tmp_path / "state.json"
    events = tmp_path / "events.json"
    with session_factory() as session:
        insert_evidence(
            session, scope="author", source_name="watch-author-a", count=10, span_days=0
        )
        session.commit()

    def argv(moment: datetime) -> list[str]:
        return [
            "--json",
            "--state",
            str(state),
            "--out",
            str(state),
            "--events",
            str(events),
            "--as-of",
            moment.isoformat(),
        ]

    assert main(argv(MOMENT), session_factory=session_factory) == EXIT_BLOCKED
    capsys.readouterr()
    with session_factory() as session:
        insert_evidence(session, scope="author", source_name="watch-author-b", count=2, span_days=0)
        session.commit()
    assert main(argv(LATER), session_factory=session_factory) == EXIT_BLOCKED
    payload = json.loads(capsys.readouterr().out)
    types = [event["event"] for event in payload["events"]]
    assert WatchEventType.BLOCKER_GAP_CHANGED.value in types
    gap_event = next(
        event
        for event in payload["events"]
        if event["event"] == WatchEventType.BLOCKER_GAP_CHANGED.value
    )
    fields = {change["field"] for change in gap_event["scope_changes"]}
    assert {"eligible", "remaining"} <= fields
    assert gap_event["blocker_active"] is True
    assert gap_event["human_gate_required"] is True
    assert gap_event["phase_transition_allowed"] is False


def test_ready_run_emits_human_gate_event(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    state = tmp_path / "state.json"
    args = [
        "--json",
        "--state",
        str(state),
        "--out",
        str(state),
        "--as-of",
        MOMENT.isoformat(),
    ]
    # 第一次运行：空库 → BLOCKED 快照落盘（此时 ready=false）
    assert main(args, session_factory=session_factory) == EXIT_BLOCKED
    first = json.loads(capsys.readouterr().out)
    assert first["ready_for_human_review"] is False
    assert first["snapshot"]["status"] == "BLOCKED"

    # 写入足量 Mock 证据（只用于验证**事件语义**，不代表真实数据资格）
    with session_factory() as session:
        insert_evidence(
            session,
            scope="author",
            source_name="watch-author-full",
            count=MIN_AUTHOR_SAMPLES,
            span_days=0,
        )
        seed_balanced_news(session)
        session.commit()

    assert main(args, session_factory=session_factory) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_for_human_review"] is True
    assert payload["snapshot"]["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    assert payload["snapshot"]["scopes"]["author"]["eligible"] == MIN_AUTHOR_SAMPLES
    assert payload["snapshot"]["scopes"]["news"]["eligible"] == MIN_NEWS_EVENTS
    assert payload["previous_fingerprint"] == first["fingerprint"]
    types = [event["event"] for event in payload["events"]]
    assert WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED.value in types
    index = types.index(WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED.value)
    ready_event = payload["events"][index]
    assert ready_event["previous_ready_for_human_review"] is False
    # 达标也**只到人工复核**：不解除 blocker、不自动切 Phase
    assert ready_event["blocker_active"] is True
    assert ready_event["human_gate_required"] is True
    assert ready_event["data_qualification_passed"] is False
    assert ready_event["phase_transition_allowed"] is False
    assert "L3 人工确认" in ready_event["message"]

    # 回落：再次清空资格证据 → true→false 事件
    with session_factory() as session:
        session.execute(sa.delete(RawItem))
        session.commit()
    assert main(args, session_factory=session_factory) == EXIT_BLOCKED
    back = json.loads(capsys.readouterr().out)
    assert back["ready_for_human_review"] is False
    back_types = [event["event"] for event in back["events"]]
    assert WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED.value in back_types
    back_index = back_types.index(WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED.value)
    revoked = back["events"][back_index]
    assert revoked["human_gate_required"] is True
    assert revoked["phase_transition_allowed"] is False
def test_corrupt_state_fails_safely_without_writes(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    state = tmp_path / "state.json"
    state.write_text("{ not json", encoding="utf-8")
    out = tmp_path / "out.json"
    events = tmp_path / "events.json"
    code = main(
        [
            "--json",
            "--state",
            str(state),
            "--out",
            str(out),
            "--events",
            str(events),
            "--as-of",
            MOMENT.isoformat(),
        ],
        session_factory=session_factory,
    )
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "state 不可用" in captured.err
    assert SECRET not in captured.err
    # 安全失败：绝不写快照 / 事件，也不落库
    assert not out.exists() and not events.exists()
    assert row_counts(session_factory) == (0, 0)

    # 被篡改（声称 blocker 已解除）的 state 同样拒绝加载
    unlocked = {
        "kind": "evidence_readiness_snapshot",
        "schema_version": 1,
        "generated_at": MOMENT.isoformat(),
        "blocker_active": False,
        "human_gate_required": True,
        "scopes": {},
    }
    state.write_text(json.dumps(unlocked, ensure_ascii=False), encoding="utf-8")
    assert (
        main(["--json", "--state", str(state), "--out", str(out), "--as-of", MOMENT.isoformat()],
             session_factory=session_factory)
        == EXIT_STATE_INVALID
    )
    capsys.readouterr()
    assert not out.exists()


def test_candidate_dry_run_is_redacted_and_zero_write(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    candidate = write_jsonl(
        tmp_path / "author.jsonl",
        [
            author_row(
                content=f"黄金看多，附带 Authorization: Bearer {SECRET}",
                note=f"api_key={SECRET}",
            ),
            {"source": "template-src", "source_record_id": "t-1", "record_kind": "example"},
            {"source": "noauth-src", "source_record_id": "n-1", "content": "黄金看多"},
        ],
    )
    events = tmp_path / "events.json"
    assert (
        main(
            [
                "--scope",
                "author",
                "--input",
                str(candidate),
                "--json",
                "--events",
                str(events),
                "--as-of",
                MOMENT.isoformat(),
            ],
            session_factory=session_factory,
        )
        == EXIT_BLOCKED
    )
    out = capsys.readouterr().out
    blob = out + events.read_text(encoding="utf-8")
    assert SECRET not in blob
    assert "AUTHORIZATION_MISSING" in blob
    assert "SYNTHETIC_EVIDENCE" in blob
    # dry-run：绝不落库、绝不建立来源
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
    with pytest.raises(SystemExit) as naive:
        main(["--as-of", "2026-09-22T12:00:00"], session_factory=session_factory)
    assert naive.value.code == 2
    capsys.readouterr()
    same = tmp_path / "same.json"
    with pytest.raises(SystemExit) as events_state:
        main(["--state", str(same), "--events", str(same)], session_factory=session_factory)
    assert events_state.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as out_events:
        main(["--out", str(same), "--events", str(same)], session_factory=session_factory)
    assert out_events.value.code == 2
    capsys.readouterr()

    unknown = tmp_path / "author.dat"
    unknown.write_text('{"source": "s"}\n', encoding="utf-8")
    assert (
        main(["--scope", "author", "--input", str(unknown)], session_factory=session_factory)
        == EXIT_INPUT_ERROR
    )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert (
        main(["--scope", "author", "--input", str(empty)], session_factory=session_factory)
        == EXIT_NO_ROWS
    )
    capsys.readouterr()
    assert row_counts(session_factory) == (0, 0)



