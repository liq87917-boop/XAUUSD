"""GOLD-009 Evidence readiness 状态变更通知单元测试（纯函数，零数据库、零网络）。

覆盖：
- 首次快照事件诚实（blocker_active / human_gate_required / phase_transition_allowed）；
- 完全相同状态重复运行 → **0 事件**（指纹幂等，时间戳不参与指纹）；
- BLOCKED 缺口变化 → BLOCKER_GAP_CHANGED；
- 原因码集合变化 → REASON_CODES_CHANGED；
- ``ready_for_human_review`` 双向变化 → ENABLED / REVOKED（仍要求 L3 人工 Gate）；
- 敏感字段（token / Authorization）不进入快照、事件与 Markdown；
- 损坏 state 文件安全失败；
- 显式写开关 + 原子替换（无残留临时文件）；
- 白名单字段（不含来源名 / notes / checklist）与确定性排序。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import evidence_readiness_watch as watch_cli
from src.alpha.evidence_gate import (
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.evidence import (
    BLOCKED_STATUS,
    EVIDENCE_CONTRACT_VERSION,
    PENDING_HUMAN_REVIEW_STATUS,
    SNAPSHOT_KIND,
    WATCH_REPORT_NAME,
    WATCH_SCHEMA_VERSION,
    EvidenceScope,
    ReadinessSnapshot,
    SnapshotStateError,
    WatchEventType,
    build_handoff_report,
    build_snapshot,
    detect_changes,
    load_snapshot_state,
    render_watch_summary,
    write_snapshot_state,
)
from src.evidence.ledger import ledger_from_raw_json
from src.monitoring import PHASE3_3_BLOCKER_CODE, build_readiness_report

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=6)
SECRET = "sk-livesecret0123456789"
NEWS_START = datetime(2026, 6, 1, tzinfo=UTC)

SNAPSHOT_KEYS = {
    "kind",
    "schema_version",
    "contract_version",
    "generated_at",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "status",
    "ready_for_human_review",
    "total_remaining_gap_count",
    "scopes",
    "reason_codes",
    "fingerprint",
}
SCOPE_KEYS = {
    "scope",
    "status",
    "eligible",
    "required",
    "remaining",
    "remaining_checks",
    "coverage_applicable",
    "coverage_days",
    "coverage_required",
    "coverage_remaining",
    "source_share_applicable",
    "source_share_evaluable",
    "source_share_remaining",
}
EVENT_KEYS = {
    "report",
    "event",
    "detected_at",
    "fingerprint",
    "previous_fingerprint",
    "blocker_code",
    "blocker_active",
    "human_gate_required",
    "data_qualification_passed",
    "phase_transition_allowed",
    "status",
    "previous_status",
    "ready_for_human_review",
    "previous_ready_for_human_review",
    "total_remaining_gap_count",
    "reason_codes",
    "added_reason_codes",
    "removed_reason_codes",
    "scope_changes",
    "message",
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


def handoff_for(
    entries: list[tuple[dict[str, Any], None]],
    *,
    quarantine_reason_counts: tuple[tuple[str, int], ...] | None = None,
) -> Any:
    readiness = build_readiness_report(ledger_from_raw_json(entries), as_of=MOMENT)
    return build_handoff_report(readiness, quarantine_reason_counts=quarantine_reason_counts)


def snapshot_for(
    entries: list[tuple[dict[str, Any], None]],
    *,
    at: datetime = MOMENT,
    quarantine_reason_counts: tuple[tuple[str, int], ...] | None = None,
) -> ReadinessSnapshot:
    return build_snapshot(
        handoff_for(entries, quarantine_reason_counts=quarantine_reason_counts),
        generated_at=at,
    )
def test_first_snapshot_event_is_honest() -> None:
    snapshot = snapshot_for([])
    assert snapshot.kind == SNAPSHOT_KIND
    assert snapshot.schema_version == WATCH_SCHEMA_VERSION
    assert snapshot.blocker_code == PHASE3_3_BLOCKER_CODE
    assert snapshot.blocker_active is True
    assert snapshot.human_gate_required is True
    assert snapshot.status == BLOCKED_STATUS
    assert snapshot.ready_for_human_review is False
    assert snapshot.scopes and [item.scope for item in snapshot.scopes] == ["author", "news"]
    assert list(snapshot.reason_codes) == sorted(snapshot.reason_codes)

    events = detect_changes(None, snapshot)
    assert len(events) == 1
    event = events[0]
    payload = event.to_dict()
    assert set(payload) == EVENT_KEYS
    assert payload["event"] == WatchEventType.FIRST_SNAPSHOT.value
    assert payload["report"] == WATCH_REPORT_NAME
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["previous_fingerprint"] is None
    assert payload["previous_ready_for_human_review"] is None
    assert payload["ready_for_human_review"] is False
    assert payload["reason_codes"] == list(snapshot.reason_codes)
    assert payload["added_reason_codes"] == list(snapshot.reason_codes)
    assert "首次" in payload["message"]


def test_identical_state_produces_no_events() -> None:
    first = snapshot_for(author_entries(10) + news_entries(5, days=3))
    second = snapshot_for(author_entries(10) + news_entries(5, days=3), at=LATER)
    # 时间戳变化不算状态变化：指纹必须完全一致
    assert first.fingerprint == second.fingerprint
    assert detect_changes(None, first) != ()
    assert detect_changes(first, second) == ()
    assert detect_changes(second, first) == ()
    # 只有 generated_at 不同
    assert first.to_dict() != second.to_dict()
    assert first.state() == second.state()


def test_gap_change_emits_blocker_gap_event() -> None:
    before = snapshot_for(author_entries(10) + news_entries(5, days=3))
    after = snapshot_for(author_entries(12) + news_entries(5, days=3), at=LATER)
    events = detect_changes(before, after, detected_at=LATER)
    assert [event.event for event in events] == [WatchEventType.BLOCKER_GAP_CHANGED]
    event = events[0]
    assert event.previous_ready_for_human_review is False
    assert event.ready_for_human_review is False
    assert (event.previous_fingerprint, event.fingerprint) == (
        before.fingerprint,
        after.fingerprint,
    )
    changes = {change.field for change in event.scope_changes}
    assert "eligible" in changes
    assert "remaining" in changes
    assert event.to_dict()["blocker_active"] is True
    assert event.to_dict()["phase_transition_allowed"] is False


def test_reason_code_change_emits_event() -> None:
    before = snapshot_for([])
    after = snapshot_for(
        [],
        at=LATER,
        quarantine_reason_counts=(("SYNTHETIC_EVIDENCE", 2), ("AUTHORIZATION_MISSING", 1)),
    )
    events = detect_changes(before, after, detected_at=LATER)
    assert WatchEventType.REASON_CODES_CHANGED in {event.event for event in events}
    reason_event = next(
        event for event in events if event.event is WatchEventType.REASON_CODES_CHANGED
    )
    assert "SYNTHETIC_EVIDENCE" in reason_event.added_reason_codes
    assert "AUTHORIZATION_MISSING" in reason_event.added_reason_codes
    assert reason_event.removed_reason_codes == ()
    # 反向：原因码消失也应产生事件
    back = detect_changes(after, before, detected_at=LATER)
    revoked = next(event for event in back if event.event is WatchEventType.REASON_CODES_CHANGED)
    assert "SYNTHETIC_EVIDENCE" in revoked.removed_reason_codes
def test_ready_flip_emits_events_both_directions_and_still_requires_human_gate() -> None:
    blocked = snapshot_for([])
    ready = snapshot_for(author_entries(MIN_AUTHOR_SAMPLES) + news_entries(), at=LATER)
    assert ready.ready_for_human_review is True
    assert ready.status == PENDING_HUMAN_REVIEW_STATUS
    assert ready.blocker_active is True
    assert ready.human_gate_required is True

    up = detect_changes(blocked, ready, detected_at=LATER)
    assert up[0].event is WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED
    payload = up[0].to_dict()
    assert payload["ready_for_human_review"] is True
    assert payload["previous_ready_for_human_review"] is False
    # 达标也只能进入人工复核：不解除 blocker、不允许自动切 Phase
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert "L3 人工确认" in payload["message"]

    down = detect_changes(ready, blocked, detected_at=LATER)
    revoked = next(
        event for event in down if event.event is WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED
    )
    assert revoked.to_dict()["phase_transition_allowed"] is False
    assert revoked.to_dict()["human_gate_required"] is True


def test_event_order_is_deterministic() -> None:
    blocked = snapshot_for([])
    ready = snapshot_for(author_entries(MIN_AUTHOR_SAMPLES) + news_entries(), at=LATER)
    events = detect_changes(blocked, ready, detected_at=LATER)
    assert [event.event for event in events] == [
        WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED,
        WatchEventType.BLOCKER_GAP_CHANGED,
        WatchEventType.REASON_CODES_CHANGED,
    ]
    again = detect_changes(blocked, ready, detected_at=LATER)
    assert [event.to_dict() for event in events] == [event.to_dict() for event in again]


def test_sensitive_values_are_redacted_everywhere() -> None:
    snapshot = snapshot_for(
        author_entries(1) + news_entries(2, days=0),
        quarantine_reason_counts=(
            (f"token={SECRET}", 1),
            (f"Authorization: Bearer {SECRET}", 1),
        ),
    )
    events = detect_changes(None, snapshot)
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    event_payload = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
    text = render_watch_summary(snapshot, events)
    for blob in (payload, event_payload, text):
        assert SECRET not in blob
    assert "token=***" in payload
    assert "token=***" in event_payload
    assert "token=***" in text
    # 授权头同样必须被擦除（token 绝不出现在快照 / 事件 / Markdown）
    assert "Authorization=***" in event_payload


def test_snapshot_only_contains_whitelist_fields() -> None:
    snapshot = snapshot_for(author_entries(3) + news_entries(4, days=2))
    payload = snapshot.to_dict()
    assert set(payload) == SNAPSHOT_KEYS
    assert set(payload["scopes"]) == {"author", "news"}
    for item in payload["scopes"].values():
        assert set(item) == SCOPE_KEYS
    # 绝不透传正文 / 来源配置 / notes / checklist 等无关字段
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("notes", "checklist", "excluded_evidence", "batch", "src-a", "manual"):
        assert forbidden not in blob


def test_serialization_is_deterministic() -> None:
    entries = author_entries(7) + news_entries(6, days=4)
    first = snapshot_for(entries)
    second = snapshot_for(entries, at=LATER)

    def dump(snapshot: ReadinessSnapshot) -> str:
        return json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)

    assert dump(first).replace(first.generated_at.isoformat(), "T") == dump(second).replace(
        second.generated_at.isoformat(), "T"
    )
    assert first.state() == second.state()
    assert first.fingerprint == second.fingerprint


def _state_file(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_state_file_means_first_snapshot(tmp_path: Path) -> None:
    assert load_snapshot_state(tmp_path / "missing.json") is None


def test_corrupt_state_file_fails_safely(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SnapshotStateError):
        load_snapshot_state(broken)
    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(SnapshotStateError):
        load_snapshot_state(empty)
    with pytest.raises(SnapshotStateError):  # 顶层不是对象
        load_snapshot_state(_state_file(tmp_path, [1, 2, 3]))

    wrong_kind = snapshot_for([]).to_dict()
    wrong_kind["kind"] = "something_else"
    with pytest.raises(SnapshotStateError):
        load_snapshot_state(_state_file(tmp_path, wrong_kind))

    wrong_version = snapshot_for([]).to_dict()
    wrong_version["schema_version"] = 99
    with pytest.raises(SnapshotStateError):
        load_snapshot_state(_state_file(tmp_path, wrong_version))

    tampered = snapshot_for([]).to_dict()
    tampered["status"] = "PASS_FAKE"
    with pytest.raises(SnapshotStateError):  # 指纹校验必须失败
        load_snapshot_state(_state_file(tmp_path, tampered))

    unlocked = snapshot_for([]).to_dict()
    unlocked["blocker_active"] = False
    with pytest.raises(SnapshotStateError):  # 声称 blocker 已解除 → 拒绝加载
        load_snapshot_state(_state_file(tmp_path, unlocked))


def test_state_roundtrip_and_atomic_replace(tmp_path: Path) -> None:
    snapshot = snapshot_for(author_entries(4) + news_entries(3, days=2))
    target = tmp_path / "nested" / "state.json"
    write_snapshot_state(target, snapshot)
    assert target.exists()
    assert target.read_text(encoding="utf-8").endswith("\n")
    loaded = load_snapshot_state(target)
    assert loaded is not None
    assert loaded.fingerprint == snapshot.fingerprint
    assert loaded.state() == snapshot.state()
    assert detect_changes(loaded, snapshot) == ()
    assert [item.name for item in target.parent.iterdir() if item.name.endswith(".tmp")] == []
    # --state 与 --out 同路径的"更新状态"模式：重复写仍原子、无残留
    write_snapshot_state(target, snapshot)
    assert json.loads(target.read_text(encoding="utf-8"))["fingerprint"] == snapshot.fingerprint
    assert [item.name for item in target.parent.iterdir() if item.name.endswith(".tmp")] == []


def test_cli_exit_codes_are_defined_before_main_guard() -> None:
    """回归：命令行入口只引用模块级退出码，禁止把常量定义放在 ``__main__`` 守卫之后。

    2026-09-22 实测发现：若 ``EXIT_BLOCKED`` 被放在 ``if __name__ == "__main__"`` 之后，
    直接运行 CLI 会在打印报告后抛 ``NameError``（退出码 1），而单测因先 import 模块而看不出。
    """
    source = Path(watch_cli.__file__).read_text(encoding="utf-8")
    guard = source.index('if __name__ == "__main__"')
    for name in (
        "EXIT_OK",
        "EXIT_INPUT_ERROR",
        "EXIT_NO_ROWS",
        "EXIT_STATE_INVALID",
        "EXIT_BLOCKED",
    ):
        assert f"{name} = " in source[:guard], name



