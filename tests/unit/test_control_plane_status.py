"""GOLD-057：控制面状态派生与一致性校验的单元测试。

覆盖 PHASE3_3_DATA 两段分离断言、queue_status 派生回归、非法组合 fail-closed 回归。
"""

from __future__ import annotations

from orchestrator import control_plane_status as cps


def _state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "status": "BLOCKED",
        "queue_status": "HOLD",
        "blockers": [{"code": "GPT_FAIL_WITHOUT_WRITES"}],
        "history_blockers": [{"code": "PHASE3_3_DATA", "frozen_reason": "frozen"}],
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# queue_status 派生回归
# ---------------------------------------------------------------------------


def test_derive_blocked_when_status_blocked() -> None:
    assert cps.derive_queue_status(_state(status="BLOCKED", blockers=[])) == "BLOCKED"


def test_derive_hold_when_active_blockers() -> None:
    assert cps.derive_queue_status(_state(status="ACTIVE", blockers=[{"code": "X"}])) == "HOLD"


def test_derive_active_when_clear() -> None:
    assert cps.derive_queue_status(_state(status="ACTIVE", blockers=[])) == "ACTIVE"


# ---------------------------------------------------------------------------
# PHASE3_3_DATA 两段分离断言
# ---------------------------------------------------------------------------


def test_phase33_frozen_in_history_and_not_active_is_consistent() -> None:
    state = _state(
        blockers=[{"code": "GPT_FAIL_WITHOUT_WRITES"}],
        history_blockers=[{"code": "PHASE3_3_DATA"}],
    )
    assert cps.phase33_frozen(state)
    assert not cps.phase33_active(state)
    assert cps.phase33_violations(state) == []


def test_phase33_frozen_and_active_fails_closed() -> None:
    state = _state(
        blockers=[{"code": "PHASE3_3_DATA"}],
        history_blockers=[{"code": "PHASE3_3_DATA"}],
    )
    assert cps.phase33_frozen(state)
    assert cps.phase33_active(state)
    assert cps.PHASE33_DATA_ACTIVE_WHILE_FROZEN in cps.phase33_violations(state)


# ---------------------------------------------------------------------------
# 非法组合 fail-closed 回归
# ---------------------------------------------------------------------------


def test_active_queue_with_blocked_status_fails_closed() -> None:
    state = _state(status="BLOCKED", queue_status="ACTIVE", blockers=[])
    assert cps.QUEUE_STATUS_ACTIVE_WHILE_BLOCKED in cps.status_violations(state)


def test_active_queue_with_active_phase33_blocker_fails_closed() -> None:
    state = _state(
        status="ACTIVE",
        queue_status="ACTIVE",
        blockers=[{"code": "PHASE3_3_DATA"}],
        history_blockers=[],
    )
    assert cps.QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS in cps.status_violations(state)


def test_hold_without_reason_fails_closed() -> None:
    state = _state(status="ACTIVE", queue_status="HOLD", blockers=[], history_blockers=[])
    assert cps.QUEUE_STATUS_HOLD_WITHOUT_REASON in cps.status_violations(state)


def test_blocked_without_cause_fails_closed() -> None:
    state = _state(status="ACTIVE", queue_status="BLOCKED", blockers=[], history_blockers=[])
    violations = cps.status_violations(state)
    assert cps.QUEUE_STATUS_BLOCKED_WHILE_ACTIVE in violations
    assert cps.QUEUE_STATUS_BLOCKED_WITHOUT_CAUSE in violations


# ---------------------------------------------------------------------------
# 合法组合（必须通过）
# ---------------------------------------------------------------------------


def test_valid_blocked() -> None:
    state = _state(status="BLOCKED", queue_status="BLOCKED", blockers=[{"code": "X"}])
    assert cps.status_violations(state) == []


def test_valid_hold() -> None:
    state = _state(status="BLOCKED", queue_status="HOLD", blockers=[{"code": "X"}])
    assert cps.status_violations(state) == []


def test_valid_active() -> None:
    state = _state(status="ACTIVE", queue_status="ACTIVE", blockers=[])
    assert cps.status_violations(state) == []


def test_missing_queue_status_fails_closed() -> None:
    assert cps.status_violations(_state(queue_status=None)) == [cps.QUEUE_STATUS_MISSING]


def test_out_of_domain_fails_closed() -> None:
    assert cps.QUEUE_STATUS_OUT_OF_DOMAIN in cps.status_violations(_state(queue_status="IDLE"))
