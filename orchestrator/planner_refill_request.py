"""GPT Planner 队列补给请求事实包（GOLD-034）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2.10（GPT Rolling Queue Autopilot）要求：GPT Planner
在「当前正在执行 / 即将执行的 queue head」之外，默认维持 **3 个**已批准 follow-on task
（``planner_lookahead_size=3``）；一旦不足，就应在队列**耗尽之前**被明确提醒去补队列，
而不是等到 ``No runnable task`` 才发现断粮。但项目红线同样清楚：

- **只有 GPT 可以规划**：创建 / 拆分 / 追加 task、补 rolling queue、更新
  ``.ai/PROJECT_STATE.json``、签发 review verdict 全部是 GPT 独占权限；
- Cline / DeepSeek 只是 Executor，**绝不允许**生成 follow-on task、补队列或决定 Phase。

因此仓库侧唯一合法的职责，是把「当前任务之外还有几个已批准任务 / 缺几个」变成
**确定性、只读、可审计**的事实交给 GPT。本模块就是这份事实包（refill request）。

本模块提供
----------
1. :func:`build_planner_refill_request`：**复用** :mod:`orchestrator.planner_snapshot`
   的只读快照事实（queue / pointer / gates / results / tasks），派生出 ``queue_head`` /
   ``follow_on_count`` / ``lookahead_target`` / ``deficit`` / ``refill_required`` /
   ``latest_completed`` / ``completed_but_unreviewed`` / ``blockers`` / ``human_gates`` /
   ``safety_invariants`` 与稳定 ``reason_codes``。readiness / 依赖 / 终态 / 指针漂移的
   状态判断**只有一个来源**（planner snapshot），本模块绝不复制成第二套容易漂移的实现；
2. 确定性契约：``facts_digest`` 只覆盖状态事实，``generated_at`` 是 wall-clock 审计字段
   并被显式排除在 facts 之外——相同仓库事实必然得到相同 digest，资格判断不依赖 wall-clock；
3. 职责边界：``refill_required`` 只表达「当前任务之外已批准的 follow-on 少于
   ``lookahead_target``」这一**纯计数事实**，等价于 ``deficit > 0``。它不是任务内容、
   不是下一任务、不是 Phase 决定；``hard_gate_tail_allowed=true`` 时 GPT 可以合法地停在
   human-only 的 gated tail 上而不补任务（GOLD-036 会把该例外固化成契约）。
   本模块输出的 JSON 里**不存在**任何具体后续任务内容 / 下一任务标题 / Phase 决定字段；
4. 受控输出：``--output`` **复用** :mod:`orchestrator.planner_snapshot_output` 的
   fail-closed 路径守卫（只允许 ``<root>/.ai/runtime/**`` 或系统临时目录），
   其余位置一律拒绝且不写任何文件。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：绝不写 ``.ai/tasks/**``、``.ai/results/**``、``.ai/PROJECT_STATE.json``、
  ``.ai/GPT_REVIEW_LEDGER.json``，也不写任何 tracked planner / task / state 文件
  （由源码守卫测试锁定）；唯一写操作是显式 ``--output`` 到受控 runtime/临时路径；
- 绝不调用 LLM / 网络 / 数据库，绝不生成任务、绝不补队列、绝不决定 Phase、
  绝不跨越 L3/L4 Human Gate；
- ``PHASE3_3_DATA``、``LIVE_TRADING=false``、``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``
  全部原样只读呈现，绝不被本工具改变。

用法
----
.. code-block:: text

    python -m orchestrator.planner_refill_request                # 只读事实包写 stdout（JSON）
    python -m orchestrator.planner_refill_request --root .       # 指定仓库根
    python -m orchestrator.planner_refill_request --output <受控 runtime/临时路径>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放人类
可读的 issue 摘要与一行 refill 提示（不参与机器解析）。给出 ``--output`` 时 JSON 只写受控
文件、``stdout`` 保持为空。

退出码：``0`` 队列足量且无漂移 / ``2`` 需要 GPT 规划（``refill_required``）或检出漂移 /
``3`` PROJECT_STATE 不可读 / ``4`` ``--output`` 目标被 fail-closed 拒绝（此时绝不写文件）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output

ROOT = Path(__file__).resolve().parent.parent


# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
PLANNER_REFILL_REQUEST_SCHEMA = "gold-ai/planner-refill-request/v1"

PLANNER_REFILL_REQUEST_SCHEMA_VERSION = 1

PLANNER_REFILL_AUTHORITY_SCHEMA = "gold-ai/planner-refill-authority/v1"

# 默认前瞻目标：当前任务之外应保留的已批准 follow-on 数量（§2.10）。
LOOKAHEAD_TARGET = 3

# PROJECT_STATE 中可覆盖默认前瞻目标的字段（``planner_lookahead_size``）。
PLANNER_LOOKAHEAD_STATE_KEY = "planner_lookahead_size"

# 必须与 ``planner.REQUIRED_SAFETY_INVARIANTS`` 保持一致（由测试锁定）。
REQUIRED_LIVE_TRADING_INVARIANT = "LIVE_TRADING=false"

REQUIRED_EXTERNAL_ORDER_INVARIANT = "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计元数据，绝不参与资格判断；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# 前瞻目标来源（机器可读）。
LOOKAHEAD_SOURCE_PROJECT_STATE = "project_state"

LOOKAHEAD_SOURCE_DEFAULT = "default"

LOOKAHEAD_SOURCE_DEFAULTED_INVALID = "defaulted_invalid"

LOOKAHEAD_SOURCE_EXPLICIT = "explicit"

# ---------- 稳定 reason code 词表（供 GPT / 人工 grep 与测试断言） ----------

REASON_REFILL_REQUIRED = "REFILL_REQUIRED"

REASON_QUEUE_AT_TARGET = "QUEUE_AT_TARGET"

REASON_QUEUE_EMPTY = "QUEUE_EMPTY"

REASON_QUEUE_HEAD_RUNNABLE = "QUEUE_HEAD_RUNNABLE"

REASON_QUEUE_HEAD_NOT_RUNNABLE = "QUEUE_HEAD_NOT_RUNNABLE"

REASON_BLOCKING_HUMAN_GATE_AT_HEAD = "BLOCKING_HUMAN_GATE_AT_HEAD"

REASON_QUEUE_TASKS_WAITING_ON_HUMAN_GATE = "QUEUE_TASKS_WAITING_ON_HUMAN_GATE"

REASON_HUMAN_ONLY_HARD_GATE = "HUMAN_ONLY_HARD_GATE"

REASON_COMPLETED_BUT_UNREVIEWED = "COMPLETED_BUT_UNREVIEWED"

REASON_STATE_RESULT_DRIFT = "STATE_RESULT_DRIFT"

REASON_ACTIVE_BLOCKERS = "ACTIVE_BLOCKERS_PRESENT"

REASON_SAFETY_INVARIANT_MISSING = "SAFETY_INVARIANT_MISSING"

REASON_PLANNER_SNAPSHOT_DRIFT = "PLANNER_SNAPSHOT_DRIFT"

REASON_LOOKAHEAD_TARGET_DEFAULTED = "LOOKAHEAD_TARGET_INVALID_DEFAULTED"

REASON_CODES = (
    REASON_REFILL_REQUIRED,
    REASON_QUEUE_AT_TARGET,
    REASON_QUEUE_EMPTY,
    REASON_QUEUE_HEAD_RUNNABLE,
    REASON_QUEUE_HEAD_NOT_RUNNABLE,
    REASON_BLOCKING_HUMAN_GATE_AT_HEAD,
    REASON_QUEUE_TASKS_WAITING_ON_HUMAN_GATE,
    REASON_HUMAN_ONLY_HARD_GATE,
    REASON_COMPLETED_BUT_UNREVIEWED,
    REASON_STATE_RESULT_DRIFT,
    REASON_ACTIVE_BLOCKERS,
    REASON_SAFETY_INVARIANT_MISSING,
    REASON_PLANNER_SNAPSHOT_DRIFT,
    REASON_LOOKAHEAD_TARGET_DEFAULTED,
)

# 本模块自定义的 issue code（其余 issue 全部来自 planner snapshot，单一来源）。
ISSUE_LOOKAHEAD_TARGET_INVALID = "LOOKAHEAD_TARGET_INVALID"

# 来自 planner snapshot 的「项目状态 vs results 漂移」issue code（只报告，绝不修复）。
DRIFT_ISSUE_CODES = (
    planner.ISSUE_POINTER_BEHIND_RESULTS,
    planner.ISSUE_POINTER_AHEAD_OF_RESULTS,
    planner.ISSUE_POINTER_MISSING,
    planner.ISSUE_POINTER_UNKNOWN_TASK,
    planner.ISSUE_QUEUE_DECLARATION_MISMATCH,
    planner.ISSUE_QUEUE_TASK_MISSING,
)

# 事实包里**绝不允许**出现的键：本工具只产事实，不产任务内容 / Phase 决定 / Review 结论。
FORBIDDEN_REQUEST_KEYS = (
    "next_task",
    "next_task_id",
    "next_task_title",
    "task_title",
    "task_body",
    "planned_task",
    "planned_tasks",
    "follow_on_task_content",
    "phase_decision",
    "phase_transition_decision",
    "verdict",
    "reviewed_at",
    "reviewer",
)

EXIT_OK = 0

# 需要 GPT 规划（``refill_required``）或检出状态漂移时的退出码（只报告，不阻塞）。
EXIT_REFILL_REQUIRED = 2

EXIT_STATE_UNREADABLE = 3

# 确定性排序契约（机器可读，供 GPT 与测试断言；只描述规则，不含事实）。
REFILL_REQUEST_ORDERING: dict[str, str] = {
    "queue.pending": "planner_snapshot queue order (task_id_sort_key)",
    "queue.follow_on_task_ids": "queue order after the head",
    "blockers": "PROJECT_STATE declaration order (sorted unique codes)",
    "human_gates.state_codes": "sorted unique",
    "human_gates.blocking_task_ids": "task_id_sort_key",
    "human_gates.pending_task_gates": "queue order",
    "safety_invariants.present": "planner.REQUIRED_SAFETY_INVARIANTS order",
    "completed_but_unreviewed.task_ids": "task_rank order",
    "reason_codes": "sorted unique",
    "issues": "sorted by (code, detail)",
}


# ============================================================
# 只读事实派生（全部来自 planner_snapshot，绝不复制第二套状态判断）
# ============================================================


def mapping_facts(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    """只读取出 snapshot 的某个映射事实（缺失 / 类型非法 ⇒ 空映射）。"""

    value = snapshot.get(key)

    return value if isinstance(value, dict) else {}


def queue_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只读取出 snapshot 的 queue 事实。"""

    return mapping_facts(snapshot, "queue")


def results_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只读取出 snapshot 的 results 事实。"""

    return mapping_facts(snapshot, "results")


def gates_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只读取出 snapshot 的 gates 事实。"""

    return mapping_facts(snapshot, "gates")


def pointer_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只读取出 snapshot 的 pointer 事实。"""

    return mapping_facts(snapshot, "pointer")


def task_views(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """非终态 task 视图：``task_id -> view``（顺序即 snapshot 的确定性 task 序）。"""

    views: dict[str, dict[str, Any]] = {}

    for view in snapshot.get("tasks") or []:
        if not isinstance(view, dict):
            continue

        task_id = view.get("task_id")

        if isinstance(task_id, str) and task_id.strip():
            views[task_id.strip()] = view

    return views


def resolve_lookahead_target(
    state: dict[str, Any],
    explicit: int | None = None,
) -> tuple[int, str]:
    """决定 ``lookahead_target``（默认 3，可由 PROJECT_STATE / 显式参数覆盖）。

    fail-safe：任何非法值（``0`` / 负数 / 字符串 / bool / 非法类型）都**绝不**被采纳，
    一律回落到默认 3（对应 GOLD-036 的「不得把 follow-on target 降为 0」契约）。
    返回值是 ``(target, source)``，``source`` 说明这个数字来自哪里。
    """

    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit < 1:
            return LOOKAHEAD_TARGET, LOOKAHEAD_SOURCE_DEFAULTED_INVALID

        return explicit, LOOKAHEAD_SOURCE_EXPLICIT

    raw = state.get(PLANNER_LOOKAHEAD_STATE_KEY)

    if raw is None:
        return LOOKAHEAD_TARGET, LOOKAHEAD_SOURCE_DEFAULT

    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return LOOKAHEAD_TARGET, LOOKAHEAD_SOURCE_DEFAULTED_INVALID

    return raw, LOOKAHEAD_SOURCE_PROJECT_STATE


def completed_but_unreviewed(
    completed_ids: Sequence[str],
    last_reviewed: str | None,
) -> dict[str, Any]:
    """终态 completed 中**还没有被 Review 覆盖**的任务（GPT-only Review 事实）。

    判定只使用 PROJECT_STATE 的 ``last_reviewed_task`` 指针与 results 终态，
    与 planner snapshot 的 ``is_newer`` 同源，绝不回写任何指针。
    """

    if last_reviewed is None:
        unreviewed = list(completed_ids)
        pointer_missing = bool(completed_ids)
    else:
        unreviewed = [
            task_id for task_id in completed_ids if planner.is_newer(task_id, last_reviewed)
        ]
        pointer_missing = False

    return {
        "task_ids": unreviewed,
        "count": len(unreviewed),
        "latest": unreviewed[-1] if unreviewed else None,
        "last_reviewed_pointer": last_reviewed,
        "reviewed_pointer_missing": pointer_missing,
        "review_authority": "gpt_only",
    }


def human_only_hard_gate(
    queue: dict[str, Any],
    views: dict[str, dict[str, Any]],
) -> bool:
    """是否「已完全不存在合法并行工作」：没有可运行任务，且每个非终态任务都在等人工。"""

    pending = [str(task_id) for task_id in (queue.get("pending") or [])]

    if not pending or queue.get("runnable_task") is not None:
        return False

    for task_id in pending:
        view = views.get(task_id) or {}

        if view.get("readiness") is True:
            return False

        blocked_by_human = (
            view.get("human_gate") in orch.BLOCKING_HUMAN_GATES
            or view.get("auto_start") is False
            or view.get("requires_human_approval") is True
        )

        if not blocked_by_human:
            return False

    return True


def blockers_section(state: dict[str, Any]) -> list[dict[str, Any]]:
    """PROJECT_STATE 声明的 blocker（只读回显 code / detail / retryable）。"""

    entries = state.get("blockers")

    details: dict[str, dict[str, Any]] = {}

    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            code = entry.get("code")

            if isinstance(code, str) and code.strip():
                details.setdefault(code.strip(), entry)

    return [
        {
            "code": code,
            "detail": details.get(code, {}).get("detail"),
            "retryable": details.get(code, {}).get("retryable"),
        }
        for code in planner.gate_codes(entries)
    ]


def human_gates_section(
    snapshot: dict[str, Any],
    views: dict[str, dict[str, Any]],
    pending: Sequence[str],
) -> dict[str, Any]:
    """Human Gate 事实（state 声明 + 队列里等待 Gate 的任务），绝不改变 Gate 状态。"""

    gates = gates_facts(snapshot)
    queue = queue_facts(snapshot)

    blocking_levels = gates.get("blocking_human_gate_levels")

    if not isinstance(blocking_levels, list):
        blocking_levels = sorted(orch.BLOCKING_HUMAN_GATES)

    pending_task_gates: list[dict[str, Any]] = []

    for task_id in pending:
        view = views.get(task_id) or {}

        pending_task_gates.append(
            {
                "task_id": task_id,
                "human_gate": view.get("human_gate"),
                "auto_start": view.get("auto_start"),
                "requires_human_approval": view.get("requires_human_approval"),
                "readiness": view.get("readiness"),
                "reason": view.get("reason"),
            }
        )

    return {
        "state_codes": list(gates.get("state_human_gate_codes") or []),
        "blocking_levels": list(blocking_levels),
        "blocking_task_ids": list(gates.get("queue_tasks_waiting_on_human_gate") or []),
        "queue_head_blocking_gate": queue.get("blocking_gate_at_head"),
        "pending_task_gates": pending_task_gates,
        "human_only_hard_gate": human_only_hard_gate(queue, views),
        "phase_transition_requires_human_gate": True,
        "executor_may_cross_human_gate": False,
    }


def safety_invariants_section(snapshot: dict[str, Any]) -> dict[str, Any]:
    """交易 / 数据安全不变量事实（只读呈现，绝不修改）。"""

    raw = gates_facts(snapshot).get("safety_invariants")

    payload = raw if isinstance(raw, dict) else {}

    required = payload.get("required")

    if not isinstance(required, list):
        required = list(planner.REQUIRED_SAFETY_INVARIANTS)

    present = [str(item) for item in (payload.get("present") or [])]

    missing = [str(item) for item in (payload.get("missing") or [])]

    return {
        "required": [str(item) for item in required],
        "present": present,
        "missing": missing,
        "all_present": not missing,
        "live_trading_disabled": REQUIRED_LIVE_TRADING_INVARIANT in present,
        "external_order_submission_disabled": REQUIRED_EXTERNAL_ORDER_INVARIANT in present,
    }


def authority_section() -> dict[str, Any]:
    """本工具的职责边界（机器可读：只请求 GPT 规划，绝无任何规划 / 写状态能力）。"""

    contract = planner.role_contract()

    return {
        "schema": PLANNER_REFILL_AUTHORITY_SCHEMA,
        "read_only": True,
        "request_only": True,
        "planning_authority": "gpt_only",
        "planner_agents": list(contract["planner_agents"]),
        "executor_agents": list(contract["executor_agents"]),
        "executor_can_refill": False,
        "executor_can_plan": False,
        "executor_can_modify_project_state": False,
        "executor_can_decide_phase": False,
        "executor_can_cross_human_gate": False,
        "tool_can_write_tasks": False,
        "tool_can_write_results": False,
        "tool_can_write_project_state": False,
        "tool_can_write_review_ledger": False,
        "tool_can_qualify_data": False,
        "tool_can_transition_phase": False,
        "next_task_content_included": False,
        "refill_request_semantics": "只请求 GPT 规划：不含任何具体后续任务内容，也不决定 Phase",
        "executor_forbidden_capabilities": list(contract["executor_forbidden_capabilities"]),
        "blocking_human_gates": list(contract["blocking_human_gates"]),
        "phase_transition_requires_human_gate": bool(
            contract["phase_transition_requires_human_gate"]
        ),
    }


def drift_section(snapshot: dict[str, Any]) -> dict[str, Any]:
    """PROJECT_STATE 指针 / 队列声明 vs 真实 results 的漂移事实（只报告，绝不修复）。"""

    drift_issues = [
        issue
        for issue in (snapshot.get("issues") or [])
        if str(issue.get("code")) in DRIFT_ISSUE_CODES
    ]

    pointer = pointer_facts(snapshot)

    return {
        "detected": bool(drift_issues),
        "codes": sorted({str(issue.get("code")) for issue in drift_issues}),
        "details": [str(issue.get("detail")) for issue in drift_issues],
        "current_task_pointer": pointer.get("current_task"),
        "last_completed_task_pointer": pointer.get("last_completed_task"),
        "last_reviewed_task_pointer": pointer.get("last_reviewed_task"),
        "latest_terminal_result": pointer.get("latest_terminal_result"),
        "latest_terminal_status": pointer.get("latest_terminal_status"),
    }


def local_issues(target_source: str) -> list[dict[str, str]]:
    """本模块自有的 issue（只有前瞻目标回落这一类，warning 级，不阻塞）。"""

    if target_source != LOOKAHEAD_SOURCE_DEFAULTED_INVALID:
        return []

    return [
        {
            "code": ISSUE_LOOKAHEAD_TARGET_INVALID,
            "severity": planner.SEVERITY_WARNING,
            "detail": (
                f"{PLANNER_LOOKAHEAD_STATE_KEY} 非法（必须为 >= 1 的整数）："
                f"已 fail-safe 回落到默认前瞻目标 {LOOKAHEAD_TARGET}"
            ),
        }
    ]


# ============================================================
# 只读事实包构建
# ============================================================


def refill_exit_code(*, refill_required: bool, issues: Sequence[dict[str, str]]) -> int:
    """退出码：``0`` 足量无漂移 / ``2`` 需要 GPT 规划或检出漂移 / ``3`` state 不可读。"""

    codes = {str(issue.get("code")) for issue in issues}

    if planner.ISSUE_PROJECT_STATE_UNREADABLE in codes:
        return EXIT_STATE_UNREADABLE

    has_error = any(issue.get("severity") == planner.SEVERITY_ERROR for issue in issues)

    if refill_required or has_error:
        return EXIT_REFILL_REQUIRED

    return EXIT_OK


def build_reason_codes(
    *,
    target_source: str,
    deficit: int,
    pending: Sequence[str],
    queue: dict[str, Any],
    blocking_task_ids: Sequence[str],
    hard_gate: bool,
    unreviewed: dict[str, Any],
    drift: dict[str, Any],
    blockers: Sequence[dict[str, Any]],
    safety: dict[str, Any],
    error_count: int,
) -> list[str]:
    """稳定 reason code 汇总（排序去重；只描述事实，不含任何建议 / 决定）。"""

    codes: set[str] = set()

    if target_source == LOOKAHEAD_SOURCE_DEFAULTED_INVALID:
        codes.add(REASON_LOOKAHEAD_TARGET_DEFAULTED)

    if deficit > 0:
        codes.add(REASON_REFILL_REQUIRED)
    else:
        codes.add(REASON_QUEUE_AT_TARGET)

    if not pending:
        codes.add(REASON_QUEUE_EMPTY)
    elif queue.get("runnable_task") is None:
        codes.add(REASON_QUEUE_HEAD_NOT_RUNNABLE)
    else:
        codes.add(REASON_QUEUE_HEAD_RUNNABLE)

    if queue.get("blocking_gate_at_head"):
        codes.add(REASON_BLOCKING_HUMAN_GATE_AT_HEAD)

    if blocking_task_ids:
        codes.add(REASON_QUEUE_TASKS_WAITING_ON_HUMAN_GATE)

    if hard_gate:
        codes.add(REASON_HUMAN_ONLY_HARD_GATE)

    if int(unreviewed.get("count") or 0) > 0:
        codes.add(REASON_COMPLETED_BUT_UNREVIEWED)

    if drift.get("detected"):
        codes.add(REASON_STATE_RESULT_DRIFT)

    if blockers:
        codes.add(REASON_ACTIVE_BLOCKERS)

    if safety.get("missing"):
        codes.add(REASON_SAFETY_INVARIANT_MISSING)

    if error_count > 0:
        codes.add(REASON_PLANNER_SNAPSHOT_DRIFT)

    return sorted(codes)


def build_planner_refill_request(
    *,
    root: Path | None = None,
    state_path: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    generated_at: str | None = None,
    lookahead_target: int | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建只读 Planner refill request。**绝不修改任何项目状态、绝不生成任务**。

    ``snapshot`` 允许注入已经构建好的 planner snapshot（便于测试与复用），
    否则按 ``root`` / ``state_path`` / ``tasks_dir`` / ``results_dir`` 只读构建一份。
    """

    resolved_root = Path(root) if root is not None else planner.ROOT

    resolved_state = Path(state_path) if state_path is not None else planner.PROJECT_STATE_PATH

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else orch.TASK_DIR

    resolved_results = Path(results_dir) if results_dir is not None else orch.RESULT_DIR

    if snapshot is None:
        snapshot = planner.build_planner_snapshot(
            root=resolved_root,
            state_path=resolved_state,
            tasks_dir=resolved_tasks,
            results_dir=resolved_results,
            generated_at=generated_at,
        )

    state = snapshot.get("project_state") or {}

    if not isinstance(state, dict):
        state = {}

    queue = queue_facts(snapshot)
    gates = gates_facts(snapshot)

    views = task_views(snapshot)

    pending = [str(task_id) for task_id in (queue.get("pending") or [])]

    queue_head = pending[0] if pending else None

    follow_on_task_ids = pending[1:]

    follow_on_count = len(follow_on_task_ids)

    target, target_source = resolve_lookahead_target(state, lookahead_target)

    deficit = max(0, target - follow_on_count)

    refill_required = deficit > 0

    terminal = results_facts(snapshot).get("terminal") or {}

    terminal_order = [str(task_id) for task_id in terminal]

    completed_ids = [task_id for task_id in terminal_order if terminal[task_id] == "completed"]

    latest_terminal = terminal_order[-1] if terminal_order else None

    latest_completed_id = completed_ids[-1] if completed_ids else None

    last_completed_pointer = planner.pointer_value(state, "last_completed_task")

    unreviewed = completed_but_unreviewed(
        completed_ids,
        planner.pointer_value(state, "last_reviewed_task"),
    )

    drift = drift_section(snapshot)

    hard_gate = human_only_hard_gate(queue, views)

    blocker_entries = blockers_section(state)

    safety = safety_invariants_section(snapshot)

    issue_candidates: list[dict[str, str]] = [
        dict(issue) for issue in (snapshot.get("issues") or [])
    ]

    issue_candidates.extend(local_issues(target_source))

    deduped = {
        (str(issue.get("code")), str(issue.get("severity")), str(issue.get("detail"))): issue
        for issue in issue_candidates
    }

    issues = sorted(
        deduped.values(),
        key=lambda issue: (str(issue.get("code")), str(issue.get("detail"))),
    )

    error_count = sum(1 for issue in issues if issue.get("severity") == planner.SEVERITY_ERROR)

    payload: dict[str, Any] = {
        "schema": PLANNER_REFILL_REQUEST_SCHEMA,
        "schema_version": PLANNER_REFILL_REQUEST_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "request_kind": "planner_refill_request",
        "queue_head": queue_head,
        "queue_head_runnable": queue_head is not None and queue.get("runnable_task") == queue_head,
        "queue": {
            "pending": pending,
            "pending_count": len(pending),
            "follow_on_task_ids": follow_on_task_ids,
            "queue_status": queue.get("queue_status"),
            "declared": list(queue.get("declared") or []),
            "declared_queue_underfilled": bool(queue.get("underfilled")),
            "queue_target_size": state.get("queue_target_size"),
            "first_stop_task": queue.get("first_stop_task"),
            "stop_reason": queue.get("first_stop_reason"),
            "blocking_gate_at_head": queue.get("blocking_gate_at_head"),
        },
        "lookahead_target": target,
        "lookahead_target_source": target_source,
        "follow_on_count": follow_on_count,
        "deficit": deficit,
        "refill_required": refill_required,
        "refill_required_basis": (
            "deficit > 0（纯计数事实：当前 queue head 之外的已批准 follow-on 少于 "
            "lookahead_target；hard_gate_tail_allowed=true 时 GPT 可停在 human-only gated tail）"
        ),
        "hard_gate_tail_allowed": hard_gate,
        "latest_completed": {
            "task_id": latest_completed_id,
            "status": "completed" if latest_completed_id is not None else None,
            "latest_terminal_result": latest_terminal,
            "latest_terminal_status": terminal.get(latest_terminal) if latest_terminal else None,
            "project_state_last_completed_task": last_completed_pointer,
            "pointer_matches_latest_completed": last_completed_pointer == latest_completed_id,
            "source": "orchestrator.planner_snapshot.results.terminal",
        },
        "completed_but_unreviewed": unreviewed,
        "state_result_drift": drift,
        "blockers": blocker_entries,
        "human_gates": human_gates_section(snapshot, views, pending),
        "safety_invariants": safety,
        "phase": {
            "current": state.get("phase"),
            "status": state.get("status"),
            "transition_allowed": False,
            "transition_requires_human_gate": True,
            "executor_may_transition_phase": False,
        },
        "planner_authority": authority_section(),
        "reason_codes": build_reason_codes(
            target_source=target_source,
            deficit=deficit,
            pending=pending,
            queue=queue,
            blocking_task_ids=list(gates.get("queue_tasks_waiting_on_human_gate") or []),
            hard_gate=hard_gate,
            unreviewed=unreviewed,
            drift=drift,
            blockers=blocker_entries,
            safety=safety,
            error_count=error_count,
        ),
        "snapshot_ref": {
            "schema": snapshot.get("schema"),
            "schema_version": snapshot.get("schema_version"),
            "facts_digest": snapshot.get("facts_digest"),
            "issue_codes": sorted({str(issue.get("code")) for issue in issues}),
            "error_count": error_count,
            "has_drift": bool(issues),
        },
        "issues": issues,
    }

    payload["summary"] = {
        "queue_head": queue_head,
        "follow_on_count": follow_on_count,
        "lookahead_target": target,
        "deficit": deficit,
        "refill_required": refill_required,
        "hard_gate_tail_allowed": hard_gate,
        "completed_but_unreviewed_count": int(unreviewed["count"]),
        "drift_detected": bool(drift["detected"]),
        "blocker_count": len(blocker_entries),
        "issue_count": len(issues),
        "error_count": error_count,
        "warning_count": len(issues) - error_count,
        "exit_code": refill_exit_code(refill_required=refill_required, issues=issues),
    }

    # digest / determinism 由 facts 派生，且自身被排除在 facts 之外（幂等、无自引用）。
    payload["facts_digest"] = request_facts_digest(payload)

    payload["determinism"] = determinism_section()

    return payload


# ============================================================
# 确定性契约 / 渲染 / 只读 CLI
# ============================================================


def request_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in payload.items() if key not in FACTS_EXCLUDED_KEYS}


def request_facts_digest(payload: dict[str, Any]) -> str:
    """确定性事实的 sha256：相同仓库事实 ⇒ 相同 digest（幂等、无自引用）。"""

    canonical = json.dumps(
        request_facts(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "eligibility_uses_wall_clock": False,
        "facts_source": "orchestrator.planner_snapshot",
        "refill_rule": "refill_required == (deficit > 0) == (follow_on_count < lookahead_target)",
        "ordering": dict(REFILL_REQUEST_ORDERING),
    }


def render_planner_refill_request(payload: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.planner_refill_request",
        description=(
            "GPT Planner 队列补给请求事实包（GOLD-034）：只读汇总 rolling queue 低水位 / "
            "缺口数量 / 最新完成与未评审事实 / blocker / Human Gate，供 GPT 决定是否补队列；"
            "绝不生成任务、绝不决定 Phase、绝不修改任何项目状态。"
        ),
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
    )

    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="PROJECT_STATE 路径（默认 <root>/.ai/PROJECT_STATE.json）",
    )

    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help="tasks 目录（默认 <root>/.ai/tasks）",
    )

    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="results 目录（默认 <root>/.ai/results）",
    )

    parser.add_argument(
        "--generated-at",
        default=None,
        help="固定 generated_at（便于审计与字节级复现；默认取当前时间）",
    )

    parser.add_argument(
        "--lookahead-target",
        type=int,
        default=None,
        help=(
            "[只影响本事实包报告的缺口] 前瞻目标覆盖值；"
            f"非法 / 小于 1 的值一律 fail-safe 回落默认 {LOOKAHEAD_TARGET}"
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "可选：把事实包 JSON 写到用户显式指定的受控路径"
            "（只允许 <root>/.ai/runtime/** 或系统临时目录；写其它位置一律拒绝）"
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：默认写 stdout，``--output`` 时写受控文件；摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    state_path = (
        Path(args.state) if args.state is not None else root / ".ai" / "PROJECT_STATE.json"
    )

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    payload = build_planner_refill_request(
        root=root,
        state_path=state_path,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        generated_at=args.generated_at,
        lookahead_target=args.lookahead_target,
    )

    rendered = render_planner_refill_request(payload)

    if args.output is None:
        sys.stdout.write(rendered)
        sys.stdout.flush()
    else:
        target, reason = snapshot_output.resolve_output_target(root, args.output)

        if target is None:
            print(
                f"[error] {snapshot_output.ISSUE_OUTPUT_PATH_REJECTED}: {reason}",
                file=sys.stderr,
            )

            return snapshot_output.EXIT_OUTPUT_REJECTED

        snapshot_output.write_snapshot_output(target, rendered)

        print(f"[info] refill request 已写入受控路径: {target}", file=sys.stderr)

    for issue in payload["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    print(
        "[refill] head={head} follow_on={follow_on}/{target} deficit={deficit} "
        "refill_required={required} codes={codes}".format(
            head=payload["queue_head"],
            follow_on=payload["follow_on_count"],
            target=payload["lookahead_target"],
            deficit=payload["deficit"],
            required=payload["refill_required"],
            codes=",".join(payload["reason_codes"]),
        ),
        file=sys.stderr,
    )

    return int(payload["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
