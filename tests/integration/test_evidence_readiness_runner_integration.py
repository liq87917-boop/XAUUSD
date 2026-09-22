"""GOLD-010 Evidence Readiness 单次本地 tick runner 端到端测试。

全部使用**临时 SQLite + 临时文件 + Mock 证据块**，零网络：

- 正常 tick：真实 CLI 只读台账、写三类 artifact、退出码诚实（BLOCKED=5）；
- 连续无变化 tick：0 重复事件、指纹稳定、数据库零写入；
- 达标 tick：退出码 0 且安全字段（blocker / human gate / 数据资格 / Phase 切换）不变；
- 锁冲突：另一个进程（本测试用同进程锁模拟）持锁 → 退出码 6 且**零写入**、锁文件不被删除；
- 陈旧锁：崩溃留痕被安全接管且可审计（``previous_owner``）；
- 损坏 state：退出码 4、原文件保留、不写任何新 artifact；
- 资格计算失败：退出码 7、零写入、**错误输出不含连接串 / 凭据**；
- 参数错误：退出码 2。
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
from scripts.evidence_readiness_runner import main
from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common.hashing import content_hash
from src.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EXIT_BLOCKED,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_QUALIFICATION_FAILED,
    EXIT_STATE_INVALID,
    RUNNER_REPORT_NAME,
    STATUS_KIND,
    SingleInstanceLock,
    TickPaths,
    WatchEventType,
)
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
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
                content_text="黄金观点",
                raw_json=evidence_raw_json(scope, source_name, available),
                source_url="https://example.com/evidence",
                content_hash=content_hash(f"{source_name}-{index}", "黄金观点"),
                published_at=available,
                collected_at=available,
                effective_at=available,
            )
        )
    session.flush()


def seed_balanced_news(session: Session) -> None:
    """满足 News 条数 / 90 天覆盖 / 单源占比的均衡 Mock 语料（仅验证事件语义）。"""
    names = ("news-src-a", "news-src-b", "news-src-c")
    span = timedelta(days=MIN_NEWS_HISTORY_DAYS)
    denominator = max(MIN_NEWS_EVENTS - 1, 1)
    for index in range(MIN_NEWS_EVENTS):
        name = names[index % len(names)]
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


def work_paths(tmp_path: Path) -> TickPaths:
    return TickPaths.in_work_dir(tmp_path / "runner")


def json_stdout(capsys: Any) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def journal_lines(paths: TickPaths) -> list[str]:
    return [line for line in paths.events.read_text(encoding="utf-8").splitlines() if line.strip()]


def leftovers(paths: TickPaths) -> list[str]:
    return sorted(item.name for item in paths.root.iterdir() if item.name.endswith(".tmp"))


def test_tick_cli_is_single_run_read_only_and_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)
    args = ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()]

    assert main(args, session_factory=session_factory) == EXIT_BLOCKED
    first = json_stdout(capsys)
    assert first["report"] == RUNNER_REPORT_NAME
    assert first["kind"] == STATUS_KIND
    assert first["tick_completed"] is True
    assert first["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert first["blocker_active"] is True
    assert first["human_gate_required"] is True
    assert first["data_qualification_passed"] is False
    assert first["phase_transition_allowed"] is False
    assert first["ready_for_human_review"] is False
    assert first["changed"] is True
    assert first["events"][0]["event"] == WatchEventType.FIRST_SNAPSHOT.value
    assert first["gaps"]["author"]["eligible"] == 0
    assert first["journal"]["added"] == 1

    # 三类 artifact 落盘，且都是原子写（无残留 .tmp）
    assert paths.state.exists() and paths.events.exists() and paths.status.exists()
    assert leftovers(paths) == []
    assert json.loads(paths.status.read_text(encoding="utf-8")) == first
    assert journal_lines(paths) == [
        json.dumps(json.loads(journal_lines(paths)[0]), ensure_ascii=False, sort_keys=True)
    ]

    # 第二次（不同审计时点，但状态完全相同）：0 事件、指纹稳定
    assert (
        main(
            ["--work-dir", str(paths.root), "--json", "--as-of", LATER.isoformat()],
            session_factory=session_factory,
        )
        == EXIT_BLOCKED
    )
    second = json_stdout(capsys)
    assert second["changed"] is False
    assert second["event_count"] == 0
    assert second["events"] == []
    assert second["fingerprint"] == first["fingerprint"]
    assert second["previous_fingerprint"] == first["fingerprint"]
    assert second["journal"]["added"] == 0
    assert len(journal_lines(paths)) == 1
    # 锁在 tick 结束后释放（可立即重新获取），文件保留为审计留痕
    assert not SingleInstanceLock(paths.lock).held
    assert row_counts(session_factory) == (0, 0)  # 只读：零数据库写入


def test_tick_cli_reports_ready_event_without_changing_safety_fields(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)
    args = ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()]
    assert main(args, session_factory=session_factory) == EXIT_BLOCKED
    blocked = json_stdout(capsys)

    with session_factory() as session:
        insert_evidence(
            session,
            scope="author",
            source_name="runner-author-full",
            count=MIN_AUTHOR_SAMPLES,
            span_days=0,
        )
        seed_balanced_news(session)
        session.commit()

    assert main(args, session_factory=session_factory) == EXIT_OK
    ready = json_stdout(capsys)
    assert ready["fingerprint"] != blocked["fingerprint"]
    assert ready["previous_fingerprint"] == blocked["fingerprint"]
    assert ready["ready_for_human_review"] is True
    assert ready["status"] == "BLOCKED_PENDING_HUMAN_REVIEW"
    assert ready["gaps"]["author"]["eligible"] == MIN_AUTHOR_SAMPLES
    assert ready["gaps"]["news"]["eligible"] == MIN_NEWS_EVENTS
    types = [event["event"] for event in ready["events"]]
    assert WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED.value in types
    ready_event = ready["events"][types.index(WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED.value)]
    # 达标**只到人工复核**：安全字段一字不改，Phase 切换仍须 L3 人工确认
    assert ready_event["blocker_active"] is True
    assert ready_event["human_gate_required"] is True
    assert ready_event["data_qualification_passed"] is False
    assert ready_event["phase_transition_allowed"] is False
    assert "L3 人工确认" in ready_event["message"]
    # status 文档层面的四个安全字段同样不变
    assert ready["blocker_active"] is True and ready["human_gate_required"] is True
    assert ready["data_qualification_passed"] is False
    assert ready["phase_transition_allowed"] is False
    assert len(journal_lines(paths)) == 1 + ready["journal"]["added"]
    assert row_counts(session_factory)[0] == MIN_AUTHOR_SAMPLES + MIN_NEWS_EVENTS  # 只读台账


def test_tick_cli_lock_conflict_exits_6_without_writes(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)
    args = ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()]
    assert main(args, session_factory=session_factory) == EXIT_BLOCKED
    capsys.readouterr()
    state_before = paths.state.read_text(encoding="utf-8")
    journal_before = paths.events.read_text(encoding="utf-8")

    holder = SingleInstanceLock(paths.lock, owner="scheduler-holder")
    holder.acquire()
    try:
        assert main(args, session_factory=session_factory) == EXIT_LOCK_CONFLICT
        captured = capsys.readouterr()
        assert captured.out == ""  # fail-closed：stdout 不产生任何伪报告
        assert "锁冲突" in captured.err
        assert "scheduler-holder" in (holder.owner_record() or "")
    finally:
        holder.release()
    assert paths.lock.exists()  # 活动锁绝不被删除
    assert paths.state.read_text(encoding="utf-8") == state_before
    assert paths.events.read_text(encoding="utf-8") == journal_before


def test_tick_cli_recovers_stale_lock_with_audit_trail(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.lock.write_text(
        json.dumps(
            {
                "kind": "evidence_readiness_tick_lock",
                "owner": "crashed-scheduler-run",
                "pid": 999999,
                "acquired_at": MOMENT.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    code = main(
        ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()],
        session_factory=session_factory,
    )
    assert code == EXIT_BLOCKED
    payload = json_stdout(capsys)
    assert payload["lock"]["recovered_stale"] is True
    assert "crashed-scheduler-run" in payload["lock"]["previous_owner"]
    record = json.loads(paths.lock.read_text(encoding="utf-8"))
    assert record["recovered_stale"] is True and record["owner"].startswith("tick-")


def test_tick_cli_corrupt_state_exits_4_and_preserves_state(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.state.write_text("{ not json", encoding="utf-8")
    code = main(
        ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()],
        session_factory=session_factory,
    )
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "state 不可用" in captured.err
    # 安全失败：旧 state 原样保留，且不写事件日志 / status；数据库零写入
    assert paths.state.read_text(encoding="utf-8") == "{ not json"
    assert not paths.events.exists() and not paths.status.exists()
    assert row_counts(session_factory) == (0, 0)


def test_tick_cli_qualification_failure_exits_7_without_leaking(
    tmp_path: Path, capsys: Any
) -> None:
    paths = work_paths(tmp_path)

    def exploding_factory() -> Session:
        raise RuntimeError(f"could not connect: postgresql://user:{SECRET}@127.0.0.1/gold_ai")

    code = main(
        ["--work-dir", str(paths.root), "--json", "--as-of", MOMENT.isoformat()],
        session_factory=exploding_factory,  # type: ignore[arg-type]
    )
    assert code == EXIT_QUALIFICATION_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fail-closed" in captured.err
    assert "RuntimeError" in captured.err
    assert SECRET not in captured.err and SECRET not in captured.out
    assert not paths.state.exists() and not paths.events.exists() and not paths.status.exists()


def test_tick_cli_argument_errors(tmp_path: Path, capsys: Any) -> None:
    paths = work_paths(tmp_path)
    with pytest.raises(SystemExit) as missing_work_dir:
        main(["--json"])
    assert missing_work_dir.value.code == 2  # --work-dir 必填：唯一写入口必须显式给出
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive_as_of:
        main(["--work-dir", str(paths.root), "--as-of", "2026-09-23T00:00:00"])
    assert naive_as_of.value.code == 2  # 禁止隐式时区
    capsys.readouterr()
    assert not paths.root.exists()
