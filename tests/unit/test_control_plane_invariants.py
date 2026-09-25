"""GOLD-049：控制面 ``queue_status`` 不变量单一事实源的单元测试。

覆盖 ``queue_status`` 与 ``status``/``blockers`` 的语义一致性：
合法组合（``ACTIVE``/``HOLD``/``BLOCKED`` 与对应 status/blockers）必须通过；
不合法组合必须 fail-closed（返回稳定 reason code）。
"""

from __future__ import annotations

from orchestrator import control_plane_invariants as cpi


def _state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "status": "BLOCKED",
        "queue_status": "HOLD",
        "blockers": [{"code": "PHASE3_3_DATA"}],
    }
    state.update(overrides)
    return state


def test_domain_constant_is_the_only_authoritative_value_set() -> None:
    assert {"ACTIVE", "HOLD", "BLOCKED"} == cpi.QUEUE_STATUS_DOMAIN


def test_active_blocker_codes_reads_current_blockers_only() -> None:
    state = _state(
        blockers=[{"code": "PHASE3_3_DATA"}, {"code": "OTHER_BLOCKER"}],
        history_blockers=[{"code": "RESOLVED_BLOCKER"}],
    )
    assert cpi.active_blocker_codes(state) == {"PHASE3_3_DATA", "OTHER_BLOCKER"}


def test_active_blocker_codes_tolerates_missing_or_malformed_blockers() -> None:
    assert cpi.active_blocker_codes(_state(blockers=None)) == frozenset()
    assert cpi.active_blocker_codes(_state(blockers="not-a-list")) == frozenset()
    assert cpi.active_blocker_codes(_state(blockers=[{"no_code": 1}])) == frozenset()


# ---------------------------------------------------------------------------
# 合法组合（必须通过，即 violations == []）
# ---------------------------------------------------------------------------


def test_blocked_with_blocked_queue_and_active_blockers_is_consistent() -> None:
    state = _state(status="BLOCKED", queue_status="BLOCKED", blockers=[{"code": "X"}])
    assert cpi.queue_status_consistency_violations(state) == []


def test_blocked_with_hold_queue_and_active_blockers_is_consistent() -> None:
    state = _state(status="BLOCKED", queue_status="HOLD", blockers=[{"code": "X"}])
    assert cpi.queue_status_consistency_violations(state) == []


def test_blocked_with_blocked_queue_without_blockers_is_consistent() -> None:
    state = _state(status="BLOCKED", queue_status="BLOCKED", blockers=[])
    assert cpi.queue_status_consistency_violations(state) == []


def test_active_with_active_queue_without_blockers_is_consistent() -> None:
    state = _state(status="ACTIVE", queue_status="ACTIVE", blockers=[])
    assert cpi.queue_status_consistency_violations(state) == []


def test_active_with_hold_queue_without_blockers_is_consistent() -> None:
    state = _state(status="ACTIVE", queue_status="HOLD", blockers=[])
    assert cpi.queue_status_consistency_violations(state) == []


# ---------------------------------------------------------------------------
# 不合法组合（必须 fail-closed，返回对应 reason code）
# ---------------------------------------------------------------------------


def test_blocked_project_must_not_have_active_queue() -> None:
    state = _state(status="BLOCKED", queue_status="ACTIVE", blockers=[])
    violations = cpi.queue_status_consistency_violations(state)
    assert cpi.QUEUE_STATUS_ACTIVE_WHILE_BLOCKED in violations


def test_blocked_project_with_active_blockers_and_active_queue_fails_closed() -> None:
    state = _state(status="BLOCKED", queue_status="ACTIVE", blockers=[{"code": "X"}])
    violations = cpi.queue_status_consistency_violations(state)
    assert cpi.QUEUE_STATUS_ACTIVE_WHILE_BLOCKED in violations
    assert cpi.QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS in violations


def test_active_project_must_not_have_blocked_queue() -> None:
    state = _state(status="ACTIVE", queue_status="BLOCKED", blockers=[])
    violations = cpi.queue_status_consistency_violations(state)
    assert cpi.QUEUE_STATUS_BLOCKED_WHILE_ACTIVE in violations


def test_active_queue_with_active_blockers_fails_closed() -> None:
    state = _state(status="ACTIVE", queue_status="ACTIVE", blockers=[{"code": "X"}])
    violations = cpi.queue_status_consistency_violations(state)
    assert cpi.QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS in violations


def test_queue_status_out_of_domain_fails_closed() -> None:
    state = _state(status="BLOCKED", queue_status="IDLE")
    violations = cpi.queue_status_consistency_violations(state)
    assert cpi.QUEUE_STATUS_OUT_OF_DOMAIN in violations


def test_missing_queue_status_fails_closed() -> None:
    violations = cpi.queue_status_consistency_violations(_state(queue_status=None))
    assert violations == [cpi.QUEUE_STATUS_MISSING]


def test_non_string_queue_status_fails_closed() -> None:
    violations = cpi.queue_status_consistency_violations(_state(queue_status=123))
    assert violations == [cpi.QUEUE_STATUS_NOT_A_STRING]
