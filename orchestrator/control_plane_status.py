"""控制面状态派生与一致性校验（GOLD-057）。

以 PROJECT_STATE 的 ``status`` / ``blockers`` / ``history_blockers`` / ``human_gates`` /
``queue_status`` 为输入，确定性派生 ``ACTIVE`` / ``HOLD`` / ``BLOCKED``，并对非法组合
fail-closed（输出稳定 reason code）。本模块**只读、纯函数**：不依赖 ``cwd`` / 平台路径 /
目录扫描顺序，绝不写文件、绝不改变 ``PHASE3_3_DATA``、绝不弱化交易安全不变量。

与 ``orchestrator/control_plane_invariants.py``（GOLD-049）的关系：后者只校验
``queue_status`` 与 ``status``/``blockers`` 的一致性；本模块在其之上补充：

- ``PHASE3_3_DATA`` 两段语义（冻结期必须位于 ``history_blockers``，且不得位于 ``blockers``）；
- ``HOLD`` / ``BLOCKED`` 需有显式依据的 fail-closed 规则；
- ``derive_queue_status`` 确定性派生（供回归测试统一从单一事实源读取，不再硬编码快照值）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: ``queue_status`` 唯一允许的取值域。
QUEUE_STATUS_DOMAIN: frozenset[str] = frozenset({"ACTIVE", "HOLD", "BLOCKED"})

#: 稳定 reason code（fail-closed，绝不本地化 / 绝不改写历史语义）。
QUEUE_STATUS_MISSING = "QUEUE_STATUS_MISSING"
QUEUE_STATUS_NOT_A_STRING = "QUEUE_STATUS_NOT_A_STRING"
QUEUE_STATUS_OUT_OF_DOMAIN = "QUEUE_STATUS_OUT_OF_DOMAIN"
QUEUE_STATUS_ACTIVE_WHILE_BLOCKED = "QUEUE_STATUS_ACTIVE_WHILE_BLOCKED"
QUEUE_STATUS_BLOCKED_WHILE_ACTIVE = "QUEUE_STATUS_BLOCKED_WHILE_ACTIVE"
QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS = "QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS"
QUEUE_STATUS_HOLD_WITHOUT_REASON = "QUEUE_STATUS_HOLD_WITHOUT_REASON"
QUEUE_STATUS_BLOCKED_WITHOUT_CAUSE = "QUEUE_STATUS_BLOCKED_WITHOUT_CAUSE"
PHASE33_DATA_ACTIVE_WHILE_FROZEN = "PHASE33_DATA_ACTIVE_WHILE_FROZEN"

#: 业务门禁 blocker（冻结期必须位于 history_blockers，不得位于 active blockers）。
PHASE33_DATA = "PHASE3_3_DATA"


def _codes(entries: Any) -> frozenset[str]:
    """把 blocker 列表归一化为 code 集合（容忍缺失 / 畸形结构）。"""
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        entry["code"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("code"), str)
    )


def active_blocker_codes(state: Mapping[str, Any]) -> frozenset[str]:
    """当前未解除（``blockers``）blocker 的 code 集合，只读。"""
    return _codes(state.get("blockers"))


def history_blocker_codes(state: Mapping[str, Any]) -> frozenset[str]:
    """历史冻结（``history_blockers``）blocker 的 code 集合，只读。"""
    return _codes(state.get("history_blockers"))


def phase33_frozen(state: Mapping[str, Any]) -> bool:
    """``PHASE3_3_DATA`` 是否已冻结（位于 history_blockers）。"""
    return PHASE33_DATA in history_blocker_codes(state)


def phase33_active(state: Mapping[str, Any]) -> bool:
    """``PHASE3_3_DATA`` 是否仍是 active blocker（位于 blockers）。"""
    return PHASE33_DATA in active_blocker_codes(state)


def human_gate_active(state: Mapping[str, Any]) -> bool:
    """是否存在 active human gate（显式人工门禁，阻断队列）。"""
    gates = state.get("human_gates")
    if isinstance(gates, dict):
        return any(
            str(value).strip().lower() in ("required", "pending", "active")
            for value in gates.values()
        )
    if isinstance(gates, list):
        return any(
            isinstance(gate, dict)
            and str(gate.get("status", "")).strip().lower() in ("required", "pending", "active")
            for gate in gates
        )
    return False


def derive_queue_status(state: Mapping[str, Any]) -> str:
    """确定性派生 ``queue_status``。

    * ``status == "BLOCKED"`` → ``"BLOCKED"``；
    * 存在 active blocker → ``"HOLD"``；
    * 否则 → ``"ACTIVE"``。
    """
    if state.get("status") == "BLOCKED":
        return "BLOCKED"
    if active_blocker_codes(state):
        return "HOLD"
    return "ACTIVE"


def phase33_violations(state: Mapping[str, Any]) -> list[str]:
    """``PHASE3_3_DATA`` 两段语义校验。

    冻结期（``history_blockers`` 含 ``PHASE3_3_DATA``）必须**同时**不在 active blockers；
    否则返回 ``PHASE33_DATA_ACTIVE_WHILE_FROZEN``。
    """
    if phase33_frozen(state) and phase33_active(state):
        return [PHASE33_DATA_ACTIVE_WHILE_FROZEN]
    return []


def status_violations(state: Mapping[str, Any]) -> list[str]:
    """校验 queue_status 与 status/blockers/human_gates 的一致性（fail-closed）。

    返回稳定 reason code 列表；空列表表示自洽。规则：

    * ``queue_status`` 必须在 ``{ACTIVE, HOLD, BLOCKED}`` 内；
    * ``status == "BLOCKED"`` 时 ``queue_status`` 不得为 ``ACTIVE``；
    * ``status == "ACTIVE"`` 时 ``queue_status`` 不得为 ``BLOCKED``；
    * 存在 active blocker 时 ``queue_status`` 不得为 ``ACTIVE``；
    * ``queue_status == "HOLD"`` 必须有显式依据（BLOCKED / blocker / human gate）；
    * ``queue_status == "BLOCKED"`` 必须有显式阻断依据（BLOCKED / blocker / human gate）。
    """
    violations: list[str] = []

    queue_status = state.get("queue_status")
    if queue_status is None:
        return [QUEUE_STATUS_MISSING]
    if not isinstance(queue_status, str):
        return [QUEUE_STATUS_NOT_A_STRING]
    if queue_status not in QUEUE_STATUS_DOMAIN:
        violations.append(QUEUE_STATUS_OUT_OF_DOMAIN)

    status = state.get("status")
    has_active_blocker = bool(active_blocker_codes(state))
    has_gate = human_gate_active(state)

    if status == "BLOCKED" and queue_status == "ACTIVE":
        violations.append(QUEUE_STATUS_ACTIVE_WHILE_BLOCKED)
    if status == "ACTIVE" and queue_status == "BLOCKED":
        violations.append(QUEUE_STATUS_BLOCKED_WHILE_ACTIVE)
    if has_active_blocker and queue_status == "ACTIVE":
        violations.append(QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS)

    has_hold_reason = (status == "BLOCKED") or has_active_blocker or has_gate
    if queue_status == "HOLD" and not has_hold_reason:
        violations.append(QUEUE_STATUS_HOLD_WITHOUT_REASON)

    has_blocked_cause = (status == "BLOCKED") or has_active_blocker or has_gate
    if queue_status == "BLOCKED" and not has_blocked_cause:
        violations.append(QUEUE_STATUS_BLOCKED_WITHOUT_CAUSE)

    return violations
