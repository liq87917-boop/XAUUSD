"""GOLD-010 Evidence Readiness 单次本地 tick runner 单元测试（临时文件 / 零数据库 / 零网络）。

覆盖：
- 正常 tick：state / 事件日志 / status 三类 artifact + 原子写无残留临时文件；
- 连续无变化的 tick **零重复事件**（幂等，不重写既有日志）；
- 有意义变化只追加新事件、事件日志有界保留且**不丢本次新事件**；
- 并发锁冲突：fail-closed、零写入、**绝不删除 / 改写活动锁**；
- 陈旧锁（崩溃留痕 / 内容不可解析）安全接管且可审计；正常退出释放锁；
- 损坏 state / 被篡改 state / 损坏事件日志 / 篡改安全字段一律 fail-closed 且保留旧 state；
- 资格计算失败 fail-closed 且**异常正文不落入错误输出**（防连接串 / 凭据泄露）；
- 退出码映射稳定；artifact 写入失败 fail-closed；status 安全字段恒定；敏感值全面脱敏；
- 源码守卫：runner 无 `while True` 常驻循环、无第三方 scheduler 依赖。
"""

from __future__ import annotations

import ast
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    ArtifactWriteError,
    EvidenceScope,
    LockConflictError,
    QualificationError,
    SingleInstanceLock,
    TickPaths,
    TickStateError,
    WatchEventType,
    WorkDirError,
    build_handoff_report,
    build_snapshot,
    exit_code_for,
    ledger_from_raw_json,
    load_event_journal,
    load_snapshot_state,
    render_tick_summary,
    run_tick,
    write_event_journal,
)
from src.evidence import readiness_runner as runner_module
from src.monitoring import PHASE3_3_BLOCKER_CODE, build_readiness_report

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=1)
SECRET = "sk-livesecret0123456789"
NEWS_START = datetime(2026, 6, 1, tzinfo=UTC)

STATUS_KEYS = {
    "kind",
    "report",
    "schema_version",
    "generated_at",
    "work_dir",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "data_qualification_passed",
    "phase_transition_allowed",
    "ready_for_human_review",
    "status",
    "fingerprint",
    "previous_fingerprint",
    "changed",
    "event_count",
    "events",
    "gaps",
    "lock",
    "journal",
    "tick_completed",
    "notes",
}


def evidence_entry(
    scope: str, *, source: str, available_at: str, oos_eligible: bool = True
) -> tuple[dict[str, Any], None]:
    return (
        {
            "evidence": {
                "contract_version": EVIDENCE_CONTRACT_VERSION,
                "scope": scope,
                "source": source,
                "oos_eligible": oos_eligible,
                "available_at": available_at,
            }
        },
        None,
    )


def author_entries(count: int) -> list[tuple[dict[str, Any], None]]:
    return [
        evidence_entry(
            EvidenceScope.AUTHOR.value,
            source="manual-evidence-author",
            available_at=(MOMENT - timedelta(days=5, hours=index)).isoformat(),
        )
        for index in range(count)
    ]


def news_entries(
    count: int = MIN_NEWS_EVENTS,
    *,
    days: int = MIN_NEWS_HISTORY_DAYS,
    sources: tuple[str, ...] = ("src-a", "src-b", "src-c"),
) -> list[tuple[dict[str, Any], None]]:
    span = timedelta(days=days)
    denominator = max(count - 1, 1)
    return [
        evidence_entry(
            EvidenceScope.NEWS.value,
            source=sources[index % len(sources)],
            available_at=(NEWS_START + span * (index / denominator)).isoformat(),
        )
        for index in range(count)
    ]


def snapshot_for(
    entries: list[tuple[dict[str, Any], None]],
    *,
    at: datetime = MOMENT,
    quarantine_reason_counts: tuple[tuple[str, int], ...] | None = None,
) -> Any:
    """由 Mock 台账构造真实快照（复用 GOLD-009 口径，不复制任何阈值算法）。"""
    readiness = build_readiness_report(ledger_from_raw_json(entries), as_of=at)
    handoff = build_handoff_report(readiness, quarantine_reason_counts=quarantine_reason_counts)
    return build_snapshot(handoff, generated_at=at)


def builder_for(
    entries: list[tuple[dict[str, Any], None]],
    *,
    quarantine_reason_counts: tuple[tuple[str, int], ...] | None = None,
) -> runner_module.SnapshotBuilder:
    """固定内容的快照构建器（时间戳变化不改变指纹）。"""

    def _build(moment: datetime) -> Any:
        return snapshot_for(entries, at=moment, quarantine_reason_counts=quarantine_reason_counts)

    return _build


def work_paths(tmp_path: Path) -> TickPaths:
    return TickPaths.in_work_dir(tmp_path / "runner")


def read_status(paths: TickPaths) -> dict[str, Any]:
    return json.loads(paths.status.read_text(encoding="utf-8"))


def journal_lines(paths: TickPaths) -> list[str]:
    return [line for line in paths.events.read_text(encoding="utf-8").splitlines() if line.strip()]


def leftover_tmp_files(paths: TickPaths) -> list[str]:
    return sorted(item.name for item in paths.root.glob("*.tmp"))


def test_exit_code_mapping_is_stable() -> None:
    assert exit_code_for(TickStateError("x")) == runner_module.EXIT_STATE_INVALID
    assert exit_code_for(LockConflictError("x")) == runner_module.EXIT_LOCK_CONFLICT
    assert exit_code_for(WorkDirError("x")) == runner_module.EXIT_WORKDIR_UNUSABLE
    assert exit_code_for(ArtifactWriteError("x")) == runner_module.EXIT_WORKDIR_UNUSABLE
    assert exit_code_for(QualificationError("x")) == runner_module.EXIT_QUALIFICATION_FAILED
    assert runner_module.EXIT_OK == 0
    assert runner_module.EXIT_CONFIG_ERROR == 2
    assert runner_module.EXIT_BLOCKED == 5


def test_tick_paths_stay_inside_explicit_work_dir(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    for item in (paths.state, paths.events, paths.status, paths.lock):
        assert item.parent == paths.root
    assert paths.root == tmp_path / "runner"
    assert not paths.root.exists()  # 只派生路径，不创建任何东西


def test_first_tick_writes_state_journal_and_status(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    report = run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))

    assert report.changed is True
    assert [event.event for event in report.events] == [WatchEventType.FIRST_SNAPSHOT]
    assert report.journal_added == 1
    assert report.journal_entries == 1
    assert report.previous_fingerprint is None
    assert report.lock.recovered_stale is False
    assert report.lock.pid > 0

    # 三类 artifact 都在**显式工作目录**内，且都是原子写（无残留 .tmp）
    assert paths.state.exists() and paths.events.exists() and paths.status.exists()
    assert leftover_tmp_files(paths) == []
    loaded = load_snapshot_state(paths.state)
    assert loaded is not None and loaded.fingerprint == report.snapshot.fingerprint
    assert len(journal_lines(paths)) == 1

    payload = read_status(paths)
    assert set(payload) == STATUS_KEYS
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["ready_for_human_review"] is False
    assert payload["tick_completed"] is True
    assert payload["journal"]["added"] == 1
    assert payload["lock"]["owner"] == report.lock.owner
    assert json.loads(paths.lock.read_text(encoding="utf-8"))["owner"] == report.lock.owner


def test_tick_releases_lock_when_done(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))
    # 正常退出必须释放锁：同一锁文件可被立刻重新获取（文件保留为审计留痕）
    assert paths.lock.exists()
    again = SingleInstanceLock(paths.lock, owner="second-tick")
    info = again.acquire()
    assert info.owner == "second-tick"
    assert info.recovered_stale is True  # 上一位 owner 留痕被审计记录
    again.release()
    assert not again.held
    assert again.owner_record() is None


def test_consecutive_ticks_without_change_are_idempotent(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    builder = builder_for(author_entries(5) + news_entries(4, days=2))
    first = run_tick(paths.root, moment=MOMENT, snapshot_builder=builder)
    journal_after_first = paths.events.read_text(encoding="utf-8")
    assert first.changed is True

    second = run_tick(paths.root, moment=LATER, snapshot_builder=builder)
    third = run_tick(paths.root, moment=LATER + timedelta(hours=1), snapshot_builder=builder)

    assert second.changed is False and third.changed is False
    assert second.events == () and third.events == ()
    assert second.journal_added == 0 and third.journal_added == 0
    assert second.journal_dropped == 0
    # 完全相同状态：既不追加重复事件，也不重写既有日志（逐字节一致）
    assert paths.events.read_text(encoding="utf-8") == journal_after_first
    assert len(journal_lines(paths)) == 1
    assert read_status(paths)["changed"] is False
    assert read_status(paths)["event_count"] == 0
    # 指纹稳定，且 status 记录了前次指纹（时间戳变化不算状态变化）
    assert third.previous_fingerprint == first.snapshot.fingerprint
    assert read_status(paths)["previous_fingerprint"] == first.snapshot.fingerprint


def test_changed_tick_appends_only_new_events(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(1)))
    before = journal_lines(paths)
    report = run_tick(
        paths.root,
        moment=LATER,
        snapshot_builder=builder_for(author_entries(6) + news_entries(3, days=1)),
    )
    after = journal_lines(paths)
    assert report.changed is True
    assert report.journal_added == len(report.events) >= 1
    assert after[: len(before)] == before  # 既有留痕不被改写
    assert len(after) == len(before) + report.journal_added
    newest = json.loads(after[-1])
    assert newest["fingerprint"] == report.snapshot.fingerprint
    assert newest["phase_transition_allowed"] is False


def test_active_lock_conflict_is_rejected_without_writes(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(2)))
    state_before = paths.state.read_text(encoding="utf-8")
    journal_before = paths.events.read_text(encoding="utf-8")

    holder = SingleInstanceLock(paths.lock, owner="holder-tick")
    info = holder.acquire()
    try:
        with pytest.raises(LockConflictError):
            run_tick(
                paths.root,
                moment=LATER,
                snapshot_builder=builder_for(author_entries(6) + news_entries(3, days=1)),
                lock_owner="loser-tick",
            )
        # 活动锁**绝不被删除 / 改写**：holder 仍可读到自己的 owner 记录
        assert paths.lock.exists()
        assert holder.info is not None and holder.info.owner == "holder-tick"
        record = holder.owner_record()
        assert record is not None and "holder-tick" in record and "loser-tick" not in record
    finally:
        holder.release()
    assert info.pid > 0
    # fail-closed：冲突路径零写入（旧 state / 事件日志逐字节不变）
    assert paths.state.read_text(encoding="utf-8") == state_before
    assert paths.events.read_text(encoding="utf-8") == journal_before


def test_stale_lock_is_recovered_safely(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    paths.root.mkdir(parents=True)
    stale_owner = "crashed-tick"
    paths.lock.write_text(
        json.dumps(
            {
                "kind": "evidence_readiness_tick_lock",
                "owner": stale_owner,
                "pid": 424242,
                "acquired_at": MOMENT.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))
    # 陈旧锁（无活动 OS 锁）被安全接管，且旧 owner 留痕可审计
    assert report.lock.recovered_stale is True
    assert report.lock.previous_owner is not None
    assert stale_owner in report.lock.previous_owner
    record = json.loads(paths.lock.read_text(encoding="utf-8"))
    assert record["recovered_stale"] is True
    assert stale_owner in record["previous_owner"]
    assert record["owner"].startswith("tick-")
    assert read_status(paths)["lock"]["recovered_stale"] is True
    assert paths.state.exists()  # 接管后 tick 正常完成


def test_unparseable_lock_file_is_recovered_without_crash(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.lock.write_text("not-json \x00 garbage", encoding="utf-8")
    lock = SingleInstanceLock(paths.lock, owner="recoverer")
    info = lock.acquire()
    assert info.recovered_stale is True
    assert info.previous_owner is not None and "不可解析" in info.previous_owner
    record = lock.owner_record()
    assert record is not None and "recoverer" in record
    lock.release()


def test_lock_acquire_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "runner" / "readiness_tick.lock", owner="once")
    first = lock.acquire()
    second = lock.acquire()
    assert first is second
    assert lock.held is True
    lock.release()
    lock.release()  # 幂等释放
    assert lock.held is False


def test_corrupt_state_fails_closed_and_preserves_old_state(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(3)))
    journal_before = paths.events.read_text(encoding="utf-8")
    status_before = paths.status.read_text(encoding="utf-8")
    paths.state.write_text("{ not json", encoding="utf-8")
    with pytest.raises(TickStateError):
        run_tick(paths.root, moment=LATER, snapshot_builder=builder_for(author_entries(9)))
    # 安全失败：损坏 state 保留原样（不覆盖 / 不清除），事件日志与 status 不被改写
    assert paths.state.read_text(encoding="utf-8") == "{ not json"
    assert paths.events.read_text(encoding="utf-8") == journal_before
    assert paths.status.read_text(encoding="utf-8") == status_before


def test_state_claiming_unblocked_is_rejected(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))
    tampered = json.loads(paths.state.read_text(encoding="utf-8"))
    tampered["blocker_active"] = False
    paths.state.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TickStateError):
        run_tick(paths.root, moment=LATER, snapshot_builder=builder_for(author_entries(1)))


def test_corrupt_event_journal_fails_closed(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))
    paths.events.write_text("{ not json\n", encoding="utf-8")
    with pytest.raises(TickStateError):
        run_tick(paths.root, moment=LATER, snapshot_builder=builder_for(author_entries(1)))
    assert paths.events.read_text(encoding="utf-8") == "{ not json\n"


def test_event_journal_tampered_safety_flags_are_rejected(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    paths.root.mkdir(parents=True)
    fake = {
        "kind": "evidence_readiness_watch_events",
        "event": "FIRST_SNAPSHOT",
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": True,
        "phase_transition_allowed": False,
    }
    write_event_journal(paths.events, [fake])
    with pytest.raises(TickStateError):
        load_event_journal(paths.events)
    with pytest.raises(TickStateError):
        run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for([]))
    assert not paths.state.exists()


def test_missing_journal_is_empty_and_tick_creates_it(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    assert load_event_journal(paths.events) == ()
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(1)))
    assert len(load_event_journal(paths.events)) == 1


def test_qualification_failure_is_fail_closed_and_redacted(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(2)))
    state_before = paths.state.read_text(encoding="utf-8")
    journal_before = paths.events.read_text(encoding="utf-8")

    def boom(moment: datetime) -> Any:
        raise RuntimeError(f"dsn postgresql://user:{SECRET}@127.0.0.1:5432/gold_ai refused")

    with pytest.raises(QualificationError) as failure:
        run_tick(paths.root, moment=LATER, snapshot_builder=boom)
    message = str(failure.value)
    assert SECRET not in message  # 异常正文（可能含连接串 / 凭据）绝不进入输出
    assert "RuntimeError" in message
    assert "fail-closed" in message
    # 保留旧 state / 事件日志，且不产生 status 覆盖
    assert paths.state.read_text(encoding="utf-8") == state_before
    assert paths.events.read_text(encoding="utf-8") == journal_before


def test_builder_returning_wrong_type_is_fail_closed(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    with pytest.raises(QualificationError):
        run_tick(paths.root, moment=MOMENT, snapshot_builder=lambda moment: object())
    assert not paths.state.exists() and not paths.events.exists() and not paths.status.exists()


def test_artifact_write_failure_is_fail_closed(tmp_path: Path, monkeypatch: Any) -> None:
    paths = work_paths(tmp_path)
    run_tick(paths.root, moment=MOMENT, snapshot_builder=builder_for(author_entries(2)))
    state_before = paths.state.read_text(encoding="utf-8")
    status_before = paths.status.read_text(encoding="utf-8")
    journal_before = journal_lines(paths)

    real_replace = runner_module.os.replace
    fail_state = {"on": True}

    def flaky_replace(src: Any, dst: Any) -> None:
        if fail_state["on"] and Path(dst) == paths.state:
            raise OSError("simulated state write failure")
        real_replace(src, dst)

    monkeypatch.setattr(runner_module.os, "replace", flaky_replace)
    changed = builder_for(author_entries(8) + news_entries(3, days=1))
    with pytest.raises(ArtifactWriteError):
        run_tick(paths.root, moment=LATER, snapshot_builder=changed)

    # 事件日志**先写**：即使随后 state 写入失败，本次变化也不会丢
    after = journal_lines(paths)
    assert len(after) > len(journal_before)
    assert after[: len(journal_before)] == journal_before
    # state / status 未被半写，原子写失败不留残留临时文件
    assert paths.state.read_text(encoding="utf-8") == state_before
    assert paths.status.read_text(encoding="utf-8") == status_before
    assert leftover_tmp_files(paths) == []

    # 恢复写入后重跑：同一次变化被**重放**（宁可重复，绝不丢失），state 随后推进
    fail_state["on"] = False
    replay = run_tick(paths.root, moment=LATER + timedelta(hours=1), snapshot_builder=changed)
    assert replay.changed is True
    assert load_snapshot_state(paths.state).fingerprint == replay.snapshot.fingerprint  # type: ignore[union-attr]


def test_work_dir_is_a_file_is_fail_closed(tmp_path: Path) -> None:
    bogus = tmp_path / "runner"
    bogus.write_text("not a directory", encoding="utf-8")
    with pytest.raises(WorkDirError):
        run_tick(bogus, moment=MOMENT, snapshot_builder=builder_for([]))
    assert bogus.read_text(encoding="utf-8") == "not a directory"


def test_ready_status_keeps_safety_fields_and_requires_human_gate(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    ready = builder_for(author_entries(MIN_AUTHOR_SAMPLES) + news_entries())
    report = run_tick(paths.root, moment=MOMENT, snapshot_builder=ready)
    assert report.ready_for_human_review is True
    payload = read_status(paths)
    assert payload["ready_for_human_review"] is True
    # 量化达标**只到人工复核**：安全字段一字不改
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    for event in payload["events"]:
        assert event["blocker_active"] is True
        assert event["human_gate_required"] is True
        assert event["data_qualification_passed"] is False
        assert event["phase_transition_allowed"] is False
    summary = render_tick_summary(report)
    assert "L3 人工确认" in summary
    assert str(paths.root) in summary
    assert "接管陈旧锁=false" in summary


def test_artifacts_and_summary_are_redacted(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    builder = builder_for(
        author_entries(1) + news_entries(2, days=0),
        quarantine_reason_counts=(
            (f"token={SECRET}", 1),
            (f"Authorization: Bearer {SECRET}", 1),
        ),
    )
    report = run_tick(paths.root, moment=MOMENT, snapshot_builder=builder)
    blob = (
        paths.status.read_text(encoding="utf-8")
        + paths.events.read_text(encoding="utf-8")
        + paths.state.read_text(encoding="utf-8")
        + render_tick_summary(report)
    )
    assert SECRET not in blob
    assert "token=***" in blob
    assert "Authorization=***" in blob


def test_event_journal_retention_is_bounded_and_keeps_current_events(tmp_path: Path) -> None:
    paths = work_paths(tmp_path)
    moments = [MOMENT + timedelta(hours=index) for index in range(5)]
    counts = [1, 4, 9, 16, 25]
    reports = [
        run_tick(
            paths.root,
            moment=moment,
            snapshot_builder=builder_for(author_entries(count)),
            journal_limit=2,
        )
        for moment, count in zip(moments, counts, strict=True)
    ]
    lines = journal_lines(paths)
    assert len(lines) <= 2  # 有界保留（确定性：保留最新 N 条）
    assert all(report.journal_limit >= 2 for report in reports)
    assert any(report.journal_dropped > 0 for report in reports)
    # 本次 tick 的新事件**永不被丢弃**：最后一行即本次快照指纹
    assert json.loads(lines[-1])["fingerprint"] == reports[-1].snapshot.fingerprint
    assert read_status(paths)["journal"]["entries"] == len(lines)


def test_runner_has_no_resident_loop_and_no_scheduler_dependency() -> None:
    module_source = Path(runner_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(module_source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # 零第三方 scheduler / 常驻依赖：不引入任何调度或子进程 / 线程库
    for forbidden in ("schedule", "apscheduler", "croniter", "win32", "subprocess", "threading"):
        assert forbidden not in imported
    # 单次 tick：模块内**没有任何循环**（不存在常驻无限循环能力）
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    # CLI 同样只是"跑一次就返回"（无 while / 无 sleep）
    cli_module = importlib.import_module("scripts.evidence_readiness_runner")
    cli_source = Path(cli_module.__file__).read_text(encoding="utf-8")
    cli_tree = ast.parse(cli_source)
    assert not any(isinstance(node, ast.While) for node in ast.walk(cli_tree))
    assert "time.sleep" not in cli_source
