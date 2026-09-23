"""GPT Planner 只读项目快照与职责边界契约（GOLD-023 / GOLD-024）。

为什么需要它
------------
GPT 是本项目**唯一**的 Planner / Reviewer / Architect；Cline（DeepSeek provider）
只是 Executor。历史事故（GOLD-017/018 期间）暴露的真实问题：``.ai/PROJECT_STATE.json``
的指针（``current_task`` / ``last_completed_task`` / ``last_reviewed_task``）会**落后**于
``.ai/results/**``，而 rolling queue 的 ``task_queue`` 声明也会与真实非终态任务漂移。
GPT 在做规划决策前必须先拿到**确定性、可审计、只读**的项目状态，否则规划依据不可信。

本模块提供
----------
1. :func:`build_planner_snapshot`：把 Git branch/head + ``PROJECT_STATE`` 摘要 + 非终态 task
   + 依赖 + result 终态 + 最近 reviewed/completed 指针 + 安全 Gate 汇总成**版本化** JSON
   快照（``schema`` + ``schema_version``）；
2. 一致性诊断（``issues``）：PROJECT_STATE 指针落后于 results、queue 声明与实际非终态不一致、
   未知 task / result status、损坏 task / result、依赖缺失 / 环、Gate / blocker / 安全不变量不一致
   —— **只报告，绝不自动修复、绝不静默兜底**；
3. 确定性契约：``facts_digest`` 只覆盖状态事实，wall-clock 只保留在 ``generated_at`` 审计字段
   且被明确排除在 facts 之外；相同 Git 树 + 相同输入必然得到相同 digest（见 ``determinism``）；
4. :func:`role_contract` / :func:`executor_allowed`：GPT 与 Executor 职责边界的**机器可测试契约**
   （Executor 只有白名单能力；未登记能力一律 fail-closed 拒绝）；
5. 受控输出：``--output`` 单点实现在 :mod:`orchestrator.planner_snapshot_output`，
   **只允许**写到 ``<root>/.ai/runtime/**`` 或系统临时目录，其余位置一律 fail-closed 拒绝。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：本模块绝不写 ``.ai/PROJECT_STATE.json``、``.ai/tasks/**``、``.ai/results/**``，
  源码内不存在任何文件写入路径（由源码守卫测试锁定）；
- 绝不调用 Cline / LLM，不规划下一任务，不决定 Phase，不放宽 acceptance，不跨越 L3/L4 Gate；
- 不改变 Phase 3.3 blocker、数据资格 Gate、``LIVE_TRADING`` 或外部订单开关。

用法
----
.. code-block:: text

    python -m orchestrator.planner_snapshot                 # 只读快照写 stdout（JSON）
    python -m orchestrator.planner_snapshot --root .        # 指定仓库根
    python -m orchestrator.planner_snapshot --output <受控 runtime/临时路径>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；
``stderr`` 只放人类可读的 issue 摘要（受控台编码影响，绝不参与机器解析）。
给出 ``--output`` 时 JSON 只写受控文件、``stdout`` 保持为空（机器通道二选一，不会歧义）。

退出码：``0`` 无漂移 / ``2`` 检出漂移 issue / ``3`` PROJECT_STATE 不可读 /
``4`` ``--output`` 目标被 fail-closed 拒绝（此时绝不写任何文件）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot_output as snapshot_output

ROOT = Path(__file__).resolve().parent.parent

# GPT Planner 每次规划前的只读事实来源。
PROJECT_STATE_PATH = ROOT / ".ai" / "PROJECT_STATE.json"

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
PLANNER_SNAPSHOT_SCHEMA = "gold-ai/planner-snapshot/v2"

PLANNER_SNAPSHOT_SCHEMA_VERSION = 2

ROLE_CONTRACT_SCHEMA = "gold-ai/role-contract/v1"

# 只读事实里**不允许**参与 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计元数据，绝不参与资格（readiness）或状态事实；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生，必须排除以避免自引用。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# 确定性排序契约（机器可读，供 GPT 与测试断言；只描述规则，不含事实）。
SNAPSHOT_ORDERING = {
    "issues": "sorted by (code, detail)",
    "tasks": "sorted by task_id_sort_key",
    "terminal_results": "sorted by task_rank (project prefix first, then task_id_sort_key)",
    "artifacts": "sorted by task_id_sort_key",
    "declared_queue": "PROJECT_STATE declaration order (drift reported separately)",
}

# ============================================================
# 职责边界契约（GPT = Planner / Reviewer / Architect；其余 = Executor）
# ============================================================

PLANNER_AGENTS = ("gpt",)

EXECUTOR_AGENTS = ("cline", "deepseek")

PLANNER_ROLES = ("planner", "reviewer", "architect")

EXECUTOR_ROLES = ("executor",)

# Executor 的**全部**合法能力（白名单；未登记能力一律 fail-closed 拒绝）。
EXECUTOR_ALLOWED_CAPABILITIES = (
    "consume_approved_task",
    "run_validation",
    "report_task_result",
    "recover_interrupted_worktree",
    "request_planner_decision",
)

# Executor 被明确禁止的能力（规划 / 评审 / 状态所有权）。
EXECUTOR_FORBIDDEN_CAPABILITIES = (
    "generate_follow_on_task",
    "refill_rolling_queue",
    "modify_project_state",
    "decide_phase",
    "loosen_acceptance",
    "cross_human_gate",
    "review_task_result",
    "self_promote_to_planner",
)

# Planner 独占决策：GPT 之外的任何角色都不得代表项目做这些决定。
PLANNER_ONLY_DECISIONS = (
    "generate_follow_on_task",
    "refill_rolling_queue",
    "modify_project_state",
    "decide_phase_transition",
    "loosen_acceptance_criteria",
    "cross_human_gate",
    "review_task_result",
    "change_architecture",
)

# 每条 Planner 独占决策 → 拦住它的 Executor 禁止能力（机器可测的绑定关系）。
# 说明：架构变更属于 Architect 角色（planner-only），Executor 只能通过
# 「不自我提升为 planner」这条能力被挡住。
PLANNER_DECISION_GUARDS = {
    "generate_follow_on_task": "generate_follow_on_task",
    "refill_rolling_queue": "refill_rolling_queue",
    "modify_project_state": "modify_project_state",
    "decide_phase_transition": "decide_phase",
    "loosen_acceptance_criteria": "loosen_acceptance",
    "cross_human_gate": "cross_human_gate",
    "review_task_result": "review_task_result",
    "change_architecture": "self_promote_to_planner",
}

# ============================================================
# 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言）
# ============================================================

ISSUE_PROJECT_STATE_UNREADABLE = "PROJECT_STATE_UNREADABLE"
ISSUE_GIT_INFO_UNAVAILABLE = "GIT_INFO_UNAVAILABLE"
ISSUE_POINTER_BEHIND_RESULTS = "PROJECT_STATE_POINTER_BEHIND_RESULTS"
ISSUE_POINTER_AHEAD_OF_RESULTS = "PROJECT_STATE_POINTER_AHEAD_OF_RESULTS"
ISSUE_POINTER_MISSING = "PROJECT_STATE_POINTER_MISSING"
ISSUE_POINTER_UNKNOWN_TASK = "PROJECT_STATE_POINTER_UNKNOWN_TASK"
ISSUE_QUEUE_DECLARATION_MISMATCH = "QUEUE_DECLARATION_MISMATCH"
ISSUE_QUEUE_TASK_MISSING = "QUEUE_TASK_MISSING"
ISSUE_TASK_FILE_INVALID = "TASK_FILE_INVALID"
ISSUE_TASK_METADATA_INVALID = "TASK_METADATA_INVALID"
ISSUE_UNKNOWN_RESULT_STATUS = "UNKNOWN_RESULT_STATUS"
ISSUE_DEPENDENCY_GRAPH_INVALID = "DEPENDENCY_GRAPH_INVALID"
ISSUE_GATE_INCONSISTENT = "GATE_INCONSISTENT"
ISSUE_QUEUE_GATE_STALLED = "QUEUE_GATE_STALLED"
ISSUE_BLOCKER_STATE_INCONSISTENT = "BLOCKER_STATE_INCONSISTENT"
ISSUE_SAFETY_INVARIANT_MISSING = "SAFETY_INVARIANT_MISSING"

SEVERITY_ERROR = "error"

SEVERITY_WARNING = "warning"

# 必须出现在 PROJECT_STATE.invariants 里的安全不变量。
REQUIRED_SAFETY_INVARIANTS = (
    "LIVE_TRADING=false",
    "ALLOW_EXTERNAL_ORDER_SUBMISSION=false",
)

# 任务 ID 解析（GOLD-017 / GOLD-001-R2）：用于确定性排序与「谁更新」判定。
TASK_ID_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z]+)-(?P<number>\d+)(?:-R(?P<revision>\d+))?$")

# 停线原因里 human_gate 的稳定形式（例如 `GOLD-023: human_gate=L3`）。
HUMAN_GATE_REASON_PATTERN = re.compile(r"human_gate=(L[1-4])")

EXIT_OK = 0

EXIT_DRIFT = 2

EXIT_STATE_UNREADABLE = 3


def task_id_sort_key(task_id: str) -> tuple[int, str, int, int, str]:
    """确定性任务序（非标准 ID 排在标准 ID 之后，且仍然稳定）。"""

    match = TASK_ID_PATTERN.match(task_id.strip())

    if match is None:
        return (1, "", 0, 0, task_id)

    return (
        0,
        match.group("prefix"),
        int(match.group("number")),
        int(match.group("revision") or 0),
        task_id,
    )


def is_newer(left: str, right: str) -> bool:
    """``left`` 是否比 ``right`` 更新（按 :func:`task_id_sort_key` 判定）。"""

    return task_id_sort_key(left) > task_id_sort_key(right)


def task_id_prefix(task_id: str) -> str:
    """任务 ID 前缀（``GOLD-017`` → ``GOLD``；非标准 ID → 空串）。"""

    match = TASK_ID_PATTERN.match(task_id.strip())

    if match is None:
        return ""

    return match.group("prefix").upper()


def task_rank(task_id: str, project_prefix: str | None = None) -> tuple[int, str, int, int, str]:
    """排序键（从旧到新）：项目自身前缀（例如 ``GOLD``）视为**最新**，排在最后。

    这样 ``TEST-002`` 这类非项目任务不会被误判成「最新终态 result」，
    同时保持排序完全确定。
    """

    base = task_id_sort_key(task_id)

    if not project_prefix:
        return (0, *base)

    return (0 if task_id_prefix(task_id) != project_prefix.upper() else 1, *base)


def make_issue(code: str, severity: str, detail: str) -> dict[str, str]:
    """构造一条诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": severity, "detail": detail}


def read_json_mapping(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """只读解析 JSON 对象；返回 ``(payload, error)``，绝不创建 / 修改文件。"""

    if not path.exists():
        return None, f"missing: {path}"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))

    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"unreadable: {exc}"

    if not isinstance(payload, dict):
        return None, "not a JSON object"

    return payload, None


def load_project_state(state_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """只读加载 ``.ai/PROJECT_STATE.json``（缺失 / 损坏 → ``(None, reason)``）。"""

    return read_json_mapping(state_path)


def result_statuses(results_dir: Path) -> dict[str, str]:
    """只读汇总 ``.ai/results/**`` 的 status（缺字段 / 损坏 → ``unknown``）。"""

    statuses: dict[str, str] = {}

    if not results_dir.is_dir():
        return statuses

    for path in sorted(results_dir.glob("*.json")):
        payload, error = read_json_mapping(path)

        if error is not None or payload is None:
            statuses[path.stem] = "unknown"
            continue

        status = str(payload.get("status", "")).strip().lower()

        statuses[path.stem] = status or "unknown"

    return statuses


def task_file_ids(tasks_dir: Path) -> list[str]:
    """只读列出 tasks 目录下的 task id（按确定性任务序）。"""

    if not tasks_dir.is_dir():
        return []

    return sorted(
        (path.stem for path in tasks_dir.glob("*.json")),
        key=task_id_sort_key,
    )


def result_file_ids(results_dir: Path) -> list[str]:
    """只读列出 results 目录下的 result id（按确定性任务序）。"""

    if not results_dir.is_dir():
        return []

    return sorted(
        (path.stem for path in results_dir.glob("*.json")),
        key=task_id_sort_key,
    )


@contextlib.contextmanager
def orchestrator_view(tasks_dir: Path, results_dir: Path) -> Iterator[None]:
    """把 Orchestrator 的只读视图临时指向给定目录（退出时一律还原原值）。

    这样 readiness / 依赖 / queue 语义**只有一个来源**（``ai_orchestrator``），
    不会在快照模块里被复制成第二套容易漂移的实现。
    只改本进程内的模块属性，绝不触碰任何文件。
    """

    saved_tasks = orch.TASK_DIR
    saved_results = orch.RESULT_DIR

    orch.TASK_DIR = tasks_dir
    orch.RESULT_DIR = results_dir

    try:
        yield

    finally:
        orch.TASK_DIR = saved_tasks
        orch.RESULT_DIR = saved_results


def role_contract() -> dict[str, Any]:
    """机器可读的 GPT / Executor 职责边界契约。"""

    return {
        "schema": ROLE_CONTRACT_SCHEMA,
        "read_only": True,
        "planner_agents": list(PLANNER_AGENTS),
        "planner_roles": list(PLANNER_ROLES),
        "executor_agents": list(EXECUTOR_AGENTS),
        "executor_roles": list(EXECUTOR_ROLES),
        "planner_only_decisions": list(PLANNER_ONLY_DECISIONS),
        "planner_decision_guards": dict(PLANNER_DECISION_GUARDS),
        "executor_allowed_capabilities": list(EXECUTOR_ALLOWED_CAPABILITIES),
        "executor_forbidden_capabilities": list(EXECUTOR_FORBIDDEN_CAPABILITIES),
        "blocking_human_gates": sorted(orch.BLOCKING_HUMAN_GATES),
        "executor_can_generate_follow_on_tasks": False,
        "executor_can_refill_rolling_queue": False,
        "executor_can_modify_project_state": False,
        "executor_can_decide_phase": False,
        "executor_can_loosen_acceptance": False,
        "executor_can_cross_human_gate": False,
        "executor_can_review_task_result": False,
        "executor_can_self_promote_to_planner": False,
        "phase_transition_requires_human_gate": True,
    }


def executor_capability(capability: str) -> tuple[bool, str]:
    """Executor 能力判定（fail-closed）：返回 ``(allowed, reason)``。

    只有白名单内的能力允许；明确禁止的能力与**未登记 / 未知**能力一律拒绝，
    绝不把「没写清楚」解释成「可以做」。
    """

    name = str(capability).strip()

    if name in EXECUTOR_ALLOWED_CAPABILITIES:
        return True, "allowed"

    if name in EXECUTOR_FORBIDDEN_CAPABILITIES:
        return False, f"executor forbidden capability: {name}"

    return False, f"unknown capability: {name!r} (fail-closed)"


def executor_allowed(capability: str) -> bool:
    """Executor 是否可以执行该能力（未知能力一律 ``False``）。"""

    return executor_capability(capability)[0]


def task_view(task_id: str, tasks_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """单个非终态 task 的只读视图 + 该项自身发现的 issue。"""

    issues: list[dict[str, str]] = []

    view: dict[str, Any] = {"task_id": task_id}

    try:
        task = orch.load_task(tasks_dir / f"{task_id}.json")

    except (OSError, ValueError) as exc:
        detail = f"{task_id}: task file invalid: {exc}"

        issues.append(make_issue(ISSUE_TASK_FILE_INVALID, SEVERITY_ERROR, detail))

        view.update({"load_error": str(exc), "readiness": False, "reason": detail})

        return view, issues

    for key, default in (("auto_start", True), ("requires_human_approval", False)):
        try:
            view[key] = orch.get_task_bool_flag(task, key, default)

        except orch.TaskMetadataError as exc:
            view[key] = None

            issues.append(
                make_issue(ISSUE_TASK_METADATA_INVALID, SEVERITY_ERROR, f"{task_id}: {exc}")
            )

    try:
        view["dependencies"] = orch.get_task_dependencies(task)

    except orch.TaskMetadataError as exc:
        view["dependencies"] = None

        issues.append(
            make_issue(ISSUE_TASK_METADATA_INVALID, SEVERITY_ERROR, f"{task_id}: {exc}")
        )

    try:
        view["human_gate"] = orch.get_task_human_gate(task)

    except orch.TaskMetadataError as exc:
        view["human_gate"] = None

        issues.append(make_issue(ISSUE_GATE_INCONSISTENT, SEVERITY_ERROR, f"{task_id}: {exc}"))

    ready, reason = orch.evaluate_task_readiness(task)

    view["readiness"] = ready
    view["reason"] = reason

    return view, issues


def describe_task_id(task_id: str | None) -> str:
    """稳定可读的 task id 展示（缺省 → ``none``）。"""

    return task_id if task_id else "none"


def pointer_value(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def terminal_result_ids(
    statuses: dict[str, str],
    project_prefix: str | None = None,
) -> list[str]:
    """全部终态（completed / blocked）result，按确定性任务序（项目前缀优先）。"""

    return sorted(
        (
            task_id
            for task_id, status in statuses.items()
            if status in orch.TERMINAL_RESULT_STATUSES
        ),
        key=lambda task_id: task_rank(task_id, project_prefix),
    )


def project_task_prefix(state: dict[str, Any]) -> str | None:
    """从 PROJECT_STATE 推导项目自身任务前缀（例如 ``GOLD``）。

    只读推导，绝不写入；推导失败返回 ``None``（此时退化为纯字母序排序）。
    """

    for key in ("current_task", "last_completed_task", "last_reviewed_task"):
        pointer = pointer_value(state, key)

        if pointer is not None:
            prefix = task_id_prefix(pointer)

            if prefix:
                return prefix

    declared, _ = declared_queue(state)

    for task_id in declared:
        prefix = task_id_prefix(task_id)

        if prefix:
            return prefix

    return None


def single_pointer_issues(
    key: str,
    pointer: str | None,
    statuses: dict[str, str],
    tasks_dir: Path,
    latest: str | None,
) -> list[dict[str, str]]:
    """单个 PROJECT_STATE 指针 vs 真实 results 的确定性判定。"""

    if pointer is None:
        if latest is not None:
            return [
                make_issue(
                    ISSUE_POINTER_MISSING,
                    SEVERITY_ERROR,
                    f"{key} 缺失，但 results 已有终态任务: {latest}",
                )
            ]

        return []

    status = statuses.get(pointer)

    if status in orch.TERMINAL_RESULT_STATUSES:
        if latest is not None and is_newer(latest, pointer):
            return [
                make_issue(
                    ISSUE_POINTER_BEHIND_RESULTS,
                    SEVERITY_ERROR,
                    f"{key}={pointer} 落后于已完成 results：latest_terminal={latest}",
                )
            ]

        return []

    # 指针指向的任务还没有终态 result（但任务 / result 确实存在）⇒ 指针超出 results。
    if (tasks_dir / f"{pointer}.json").exists() or pointer in statuses:
        return [
            make_issue(
                ISSUE_POINTER_AHEAD_OF_RESULTS,
                SEVERITY_ERROR,
                f"{key}={pointer} 尚无终态 result（status="
                f"{status if status is not None else 'pending'}）："
                f"指针超出 results（latest_terminal={describe_task_id(latest)}）",
            )
        ]

    return [
        make_issue(
            ISSUE_POINTER_UNKNOWN_TASK,
            SEVERITY_ERROR,
            f"{key}={pointer} 在 tasks/ 与 results/ 中都不存在",
        )
    ]


def pointer_section(
    state: dict[str, Any],
    statuses: dict[str, str],
    tasks_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """最近 reviewed / completed 指针 vs 真实 results（只报告漂移，绝不改写）。"""

    issues: list[dict[str, str]] = []

    terminal_ids = terminal_result_ids(statuses, project_task_prefix(state))

    latest = terminal_ids[-1] if terminal_ids else None

    section: dict[str, Any] = {
        "current_task": pointer_value(state, "current_task"),
        "last_completed_task": pointer_value(state, "last_completed_task"),
        "last_reviewed_task": pointer_value(state, "last_reviewed_task"),
        "latest_terminal_result": latest,
        "latest_terminal_status": statuses[latest] if latest is not None else None,
        "terminal_results": terminal_ids,
    }

    for key in ("last_completed_task", "last_reviewed_task"):
        issues.extend(single_pointer_issues(key, section[key], statuses, tasks_dir, latest))

    current = section["current_task"]

    if current is not None:
        current_status = statuses.get(current)

        if current_status in orch.TERMINAL_RESULT_STATUSES:
            issues.append(
                make_issue(
                    ISSUE_POINTER_BEHIND_RESULTS,
                    SEVERITY_ERROR,
                    f"current_task={current} 已有终态 result={current_status}："
                    "PROJECT_STATE 指针落后于 results"
                    f"（latest_terminal={describe_task_id(latest)}）",
                )
            )

    return section, issues


def declared_queue(state: dict[str, Any]) -> tuple[list[str], str | None]:
    """只读读取 PROJECT_STATE.task_queue（非法类型 → error，绝不猜测）。"""

    raw = state.get("task_queue")

    if raw is None:
        return [], None

    if isinstance(raw, list) and all(isinstance(item, str) and item.strip() for item in raw):
        return [item.strip() for item in raw], None

    return [], "task_queue 必须是字符串数组"


def queue_section(
    state: dict[str, Any],
    tasks_dir: Path,
    statuses: dict[str, str],
    schedule: dict[str, Any],
    stop_reason: str,
    runnable_id: str | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """queue 声明 vs 真实非终态任务（只报告不一致，绝不补队列）。"""

    issues: list[dict[str, str]] = []

    pending = list(schedule["pending"])

    target = int(schedule["target"])

    queue_status = state.get("queue_status")

    declared, declared_error = declared_queue(state)

    if declared_error is not None:
        issues.append(
            make_issue(
                ISSUE_QUEUE_DECLARATION_MISMATCH,
                SEVERITY_ERROR,
                f"PROJECT_STATE queue 声明不可读: {declared_error}",
            )
        )

    declared_missing = sorted(
        (task_id for task_id in declared if not (tasks_dir / f"{task_id}.json").exists()),
        key=task_id_sort_key,
    )

    for task_id in declared_missing:
        issues.append(
            make_issue(
                ISSUE_QUEUE_TASK_MISSING,
                SEVERITY_ERROR,
                f"task_queue 声明了不存在的 task 文件: {task_id}",
            )
        )

    declared_but_terminal = sorted(
        (
            task_id
            for task_id in declared
            if statuses.get(task_id) in orch.TERMINAL_RESULT_STATUSES
        ),
        key=task_id_sort_key,
    )

    pending_but_not_declared = sorted(
        (task_id for task_id in pending if task_id not in declared),
        key=task_id_sort_key,
    )

    declared_order_matches = [task_id for task_id in declared if task_id in pending] == [
        task_id for task_id in pending if task_id in declared
    ]

    mismatch_parts: list[str] = []

    if declared_but_terminal:
        mismatch_parts.append(f"declared_but_terminal={declared_but_terminal}")

    if pending_but_not_declared:
        mismatch_parts.append(f"pending_but_not_declared={pending_but_not_declared}")

    if declared and not declared_order_matches:
        mismatch_parts.append("declared_order_differs_from_pending")

    if queue_status == "ACTIVE" and not pending:
        mismatch_parts.append("queue_status_ACTIVE_without_pending_tasks")

    if mismatch_parts:
        issues.append(
            make_issue(
                ISSUE_QUEUE_DECLARATION_MISMATCH,
                SEVERITY_ERROR,
                f"queue 声明与实际非终态任务不一致（{', '.join(mismatch_parts)}）: "
                f"declared={declared} pending={pending}",
            )
        )

    first_stop_task = pending[0] if pending else None

    gate_match = HUMAN_GATE_REASON_PATTERN.search(stop_reason)

    blocking_gate_at_head = gate_match.group(1) if gate_match else None

    if (
        runnable_id is None
        and blocking_gate_at_head in orch.BLOCKING_HUMAN_GATES
        and queue_status == "ACTIVE"
    ):
        issues.append(
            make_issue(
                ISSUE_QUEUE_GATE_STALLED,
                SEVERITY_WARNING,
                f"queue_status=ACTIVE 但队列在 {describe_task_id(first_stop_task)} 处等待 "
                f"human_gate={blocking_gate_at_head}：L3/L4 永不自动跨越，需 GPT / 人工显式决策",
            )
        )

    section: dict[str, Any] = {
        "declared": declared,
        "declared_error": declared_error,
        "queue_status": queue_status,
        "pending": pending,
        "count": len(pending),
        "target": target,
        "underfilled": len(pending) < target,
        "declared_but_terminal": declared_but_terminal,
        "declared_missing_files": declared_missing,
        "pending_but_not_declared": pending_but_not_declared,
        "declared_order_matches_pending": declared_order_matches,
        "first_pending": first_stop_task,
        "first_stop_task": None if runnable_id else first_stop_task,
        "runnable_task": runnable_id,
        "first_stop_reason": stop_reason,
        "blocking_gate_at_head": blocking_gate_at_head,
    }

    return section, issues


def gate_codes(entries: Any) -> list[str]:
    """从 PROJECT_STATE.blockers / human_gates 里只读提取稳定 code 列表。"""

    if not isinstance(entries, list):
        return []

    codes: list[str] = []

    for entry in entries:
        if isinstance(entry, dict):
            code = entry.get("code")

            if isinstance(code, str) and code.strip():
                codes.append(code.strip())

    return sorted(set(codes))


def invariant_list(state: dict[str, Any]) -> list[str]:
    """只读读取 PROJECT_STATE.invariants（非字符串项忽略）。"""

    raw = state.get("invariants")

    if not isinstance(raw, list):
        return []

    return [item.strip() for item in raw if isinstance(item, str)]


def gates_section(
    state: dict[str, Any],
    views: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """安全 Gate / blocker / 不变量的一致性诊断（只报告，绝不改 Gate）。"""

    issues: list[dict[str, str]] = []

    queue_gate_tasks = sorted(
        (
            str(view["task_id"])
            for view in views
            if view.get("human_gate") in orch.BLOCKING_HUMAN_GATES
        ),
        key=task_id_sort_key,
    )

    invariants = invariant_list(state)

    missing_invariants = [
        item for item in REQUIRED_SAFETY_INVARIANTS if item not in invariants
    ]

    for item in missing_invariants:
        issues.append(
            make_issue(
                ISSUE_SAFETY_INVARIANT_MISSING,
                SEVERITY_ERROR,
                f"PROJECT_STATE.invariants 缺少安全不变量: {item}",
            )
        )

    status = state.get("status")

    blocker_codes = gate_codes(state.get("blockers"))

    normalized_status = status.strip().upper() if isinstance(status, str) else ""

    if normalized_status == "BLOCKED" and not blocker_codes:
        issues.append(
            make_issue(
                ISSUE_BLOCKER_STATE_INCONSISTENT,
                SEVERITY_ERROR,
                "PROJECT_STATE.status=BLOCKED 但没有声明任何 blocker",
            )
        )

    if normalized_status and normalized_status != "BLOCKED" and blocker_codes:
        issues.append(
            make_issue(
                ISSUE_BLOCKER_STATE_INCONSISTENT,
                SEVERITY_ERROR,
                f"PROJECT_STATE.status={status} 与 blockers={blocker_codes} 不一致",
            )
        )

    section: dict[str, Any] = {
        "phase": state.get("phase"),
        "status": status,
        "blocker_codes": blocker_codes,
        "state_human_gate_codes": gate_codes(state.get("human_gates")),
        "blocking_human_gate_levels": sorted(orch.BLOCKING_HUMAN_GATES),
        "queue_tasks_waiting_on_human_gate": queue_gate_tasks,
        "safety_invariants": {
            "required": list(REQUIRED_SAFETY_INVARIANTS),
            "present": [item for item in REQUIRED_SAFETY_INVARIANTS if item in invariants],
            "missing": missing_invariants,
        },
        "phase_transition_requires_human_gate": True,
        "executor_may_transition_phase": False,
        "executor_may_cross_human_gate": False,
    }

    return section, issues


def results_section(
    statuses: dict[str, str],
    tasks_dir: Path,
    project_prefix: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """result 终态汇总 + 未知状态诊断（只报告，绝不回写历史 result）。"""

    issues: list[dict[str, str]] = []

    terminal = {
        task_id: statuses[task_id]
        for task_id in terminal_result_ids(statuses, project_prefix)
    }

    unresolved = sorted(
        (
            task_id
            for task_id, status in statuses.items()
            if status in orch.UNRESOLVED_RESULT_STATUSES
        ),
        key=task_id_sort_key,
    )

    unknown = sorted(
        (
            task_id
            for task_id, status in statuses.items()
            if status not in orch.KNOWN_RESULT_STATUSES
        ),
        key=task_id_sort_key,
    )

    for task_id in unknown:
        issues.append(
            make_issue(
                ISSUE_UNKNOWN_RESULT_STATUS,
                SEVERITY_ERROR,
                f"result status unknown: {task_id}={statuses[task_id]}",
            )
        )

    missing_results = sorted(
        (task_id for task_id in task_file_ids(tasks_dir) if task_id not in statuses),
        key=task_id_sort_key,
    )

    section: dict[str, Any] = {
        "total": len(statuses),
        "terminal": terminal,
        "unresolved": unresolved,
        "unknown": unknown,
        "missing_results": missing_results,
        "known_statuses": sorted(orch.KNOWN_RESULT_STATUSES),
        "terminal_statuses": sorted(orch.TERMINAL_RESULT_STATUSES),
    }

    return section, issues


# ============================================================
# Git 事实（只读）：规划必须绑定到确切的代码版本
# ============================================================

GIT_HEAD_REF_PREFIX = "ref: "

GIT_REFS_HEADS_PREFIX = "refs/heads/"


def read_text_if_possible(path: Path) -> str | None:
    """只读文本（不存在 / 不可读 → ``None``）；绝不创建或修改文件。"""

    try:
        return path.read_text(encoding="utf-8", errors="replace")

    except OSError:
        return None


def resolve_git_dir(root: Path) -> Path | None:
    """``<root>/.git`` 目录；``.git`` 是 gitdir 指针文件（worktree）时按指针解析。"""

    candidate = Path(root) / ".git"

    if candidate.is_dir():
        return candidate

    if candidate.is_file():
        pointer = (read_text_if_possible(candidate) or "").strip()

        if pointer.startswith("gitdir:"):
            resolved = (candidate.parent / pointer[len("gitdir:") :].strip()).resolve()

            if resolved.is_dir():
                return resolved

    return None


def read_ref_sha(directory: Path, ref: str) -> str | None:
    """解析 ref 的 commit SHA（先 loose ref，再 ``packed-refs``）；解析不了返回 ``None``。"""

    loose = read_text_if_possible(directory.joinpath(*ref.split("/")))

    if loose is not None and loose.strip():
        return loose.strip()

    packed = read_text_if_possible(directory / "packed-refs")

    if packed is not None:
        for line in packed.splitlines():
            entry = line.strip()

            if not entry or entry.startswith("#") or entry.startswith("^"):
                continue

            parts = entry.split(maxsplit=1)

            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()

    return None


def git_section(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """只读 Git 事实（branch / head / head_short）。

    只读取 ``<root>/.git/HEAD`` 与 ref 文件（或 worktree 的 gitdir 指针 + ``packed-refs``），
    不执行任何 Git 写操作、不切分支、不写索引，对本机仓库零影响；
    解析不了时 fail-closed 报告 ``GIT_INFO_UNAVAILABLE``（绝不猜测版本）。
    """

    directory = resolve_git_dir(root)

    branch: str | None = None
    head: str | None = None

    if directory is not None:
        head_text = (read_text_if_possible(directory / "HEAD") or "").strip()

        if head_text.startswith(GIT_HEAD_REF_PREFIX):
            ref = head_text[len(GIT_HEAD_REF_PREFIX) :].strip()

            if ref.startswith(GIT_REFS_HEADS_PREFIX):
                branch = ref[len(GIT_REFS_HEADS_PREFIX) :].strip() or None

            head = read_ref_sha(directory, ref) if ref else None

        elif head_text:
            # detached HEAD：HEAD 文件里直接就是 commit SHA。
            head = head_text

    section: dict[str, Any] = {
        "branch": branch,
        "head": head,
        "head_short": head[:8] if head else None,
        "detached": bool(head) and branch is None,
        "available": bool(branch) and bool(head),
    }

    issues: list[dict[str, str]] = []

    if not section["available"]:
        issues.append(
            make_issue(
                ISSUE_GIT_INFO_UNAVAILABLE,
                SEVERITY_ERROR,
                f"无法从 {root} 读取 Git branch/head 事实："
                "规划必须绑定确切代码版本（snapshot 不猜测、不跳过）",
            )
        )

    return section, issues


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """``PROJECT_STATE`` 的只读摘要（原样回显仍在 ``project_state``，两者都只报告）。"""

    declared, declared_error = declared_queue(state)

    return {
        "schema_version": state.get("schema_version"),
        "project": state.get("project"),
        "branch": state.get("branch"),
        "phase": state.get("phase"),
        "status": state.get("status"),
        "current_task": pointer_value(state, "current_task"),
        "last_completed_task": pointer_value(state, "last_completed_task"),
        "last_reviewed_task": pointer_value(state, "last_reviewed_task"),
        "queue_status": state.get("queue_status"),
        "queue_target_size": state.get("queue_target_size"),
        "declared_queue": declared,
        "declared_queue_error": declared_error,
        "blocker_codes": gate_codes(state.get("blockers")),
        "human_gate_codes": gate_codes(state.get("human_gates")),
        "invariants": invariant_list(state),
        "next_action": state.get("next_action"),
    }


def snapshot_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in snapshot.items() if key not in FACTS_EXCLUDED_KEYS}


def snapshot_facts_digest(snapshot: dict[str, Any]) -> str:
    """确定性事实的 sha256：相同 Git 树 + 相同输入 ⇒ 相同 digest，且幂等。"""

    canonical = json.dumps(
        snapshot_facts(snapshot),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "ordering": dict(SNAPSHOT_ORDERING),
    }


def exit_code_for_issues(issues: list[dict[str, str]]) -> int:
    """退出码：``0`` 无漂移 / ``2`` 有漂移 / ``3`` PROJECT_STATE 不可读。

    ``4``（``--output`` 目标被拒）由 :mod:`orchestrator.planner_snapshot_output` 定义，
    只在写路径守卫里出现，不影响这里的漂移语义。
    """

    codes = {issue["code"] for issue in issues}

    if ISSUE_PROJECT_STATE_UNREADABLE in codes:
        return EXIT_STATE_UNREADABLE

    if issues:
        return EXIT_DRIFT

    return EXIT_OK


def planner_snapshot_exit_code(snapshot: dict[str, Any]) -> int:
    """从快照读出退出码（供 CLI / 人工一致使用）。"""

    return exit_code_for_issues(list(snapshot["issues"]))


def build_planner_snapshot(
    *,
    root: Path | None = None,
    state_path: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """构建只读 planner 快照（版本化 + 一致性诊断）。**绝不修改任何项目状态**。"""

    resolved_root = Path(root) if root is not None else ROOT

    resolved_state = Path(state_path) if state_path is not None else PROJECT_STATE_PATH

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else orch.TASK_DIR

    resolved_results = Path(results_dir) if results_dir is not None else orch.RESULT_DIR

    issues: list[dict[str, str]] = []

    with orchestrator_view(resolved_tasks, resolved_results):
        state, state_error = load_project_state(resolved_state)

        if state_error is not None:
            issues.append(
                make_issue(
                    ISSUE_PROJECT_STATE_UNREADABLE,
                    SEVERITY_ERROR,
                    f"{resolved_state}: {state_error}",
                )
            )

        state = state or {}

        git_view, git_issues = git_section(resolved_root)

        statuses = result_statuses(resolved_results)

        schedule = orch.queue_snapshot()

        pending = sorted(schedule["pending"], key=task_id_sort_key)

        views: list[dict[str, Any]] = []

        for task_id in pending:
            view, view_issues = task_view(task_id, resolved_tasks)

            views.append(view)
            issues.extend(view_issues)

        for error in orch.dependency_graph_errors():
            issues.append(make_issue(ISSUE_DEPENDENCY_GRAPH_INVALID, SEVERITY_ERROR, error))

        runnable, stop_reason = orch.find_next_task_with_reason()

        pointer, pointer_issues = pointer_section(state, statuses, resolved_tasks)

        queue, queue_issues = queue_section(
            state,
            resolved_tasks,
            statuses,
            schedule,
            stop_reason,
            runnable.stem if runnable is not None else None,
        )

        gates, gate_issues = gates_section(state, views)

        results, result_issues = results_section(
            statuses,
            resolved_tasks,
            project_task_prefix(state),
        )

    issues.extend(git_issues)
    issues.extend(pointer_issues)
    issues.extend(queue_issues)
    issues.extend(gate_issues)
    issues.extend(result_issues)

    deduped = list(
        {(issue["code"], issue["severity"], issue["detail"]): issue for issue in issues}.values()
    )

    ordered = sorted(deduped, key=lambda issue: (issue["code"], issue["detail"]))

    error_count = sum(1 for issue in ordered if issue["severity"] == SEVERITY_ERROR)

    snapshot: dict[str, Any] = {
        "schema": PLANNER_SNAPSHOT_SCHEMA,
        "schema_version": PLANNER_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "git": git_view,
        "paths": {
            "root": str(resolved_root),
            "project_state": str(resolved_state),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
        },
        "state": state_summary(state),
        "project_state": state or None,
        "pointer": pointer,
        "queue": queue,
        "gates": gates,
        "results": results,
        "tasks": views,
        "artifacts": {
            "task_files": task_file_ids(resolved_tasks),
            "result_files": result_file_ids(resolved_results),
        },
        "role_contract": role_contract(),
        "issues": ordered,
        "summary": {
            "issue_count": len(ordered),
            "error_count": error_count,
            "warning_count": len(ordered) - error_count,
            "has_drift": bool(ordered),
            "exit_code": exit_code_for_issues(ordered),
        },
    }

    # digest / determinism 由 facts 派生，且自身被排除在 facts 之外（幂等、无自引用）。
    snapshot["facts_digest"] = snapshot_facts_digest(snapshot)
    snapshot["determinism"] = determinism_section()

    return snapshot


def render_planner_snapshot(snapshot: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys）。

    默认 ``ensure_ascii=True``：输出纯 ASCII，任何控制台编码下都不会崩；
    机器侧用 ``json.loads`` 读取即可拿回原始中文。
    """

    return json.dumps(snapshot, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.planner_snapshot",
        description=(
            "GPT Planner 只读项目快照 + 一致性诊断（GOLD-023 / GOLD-024 版本化契约）："
            "绝不修改任何项目状态。"
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
        "--output",
        type=Path,
        default=None,
        help=(
            "可选：把快照 JSON 写到用户显式指定的受控路径"
            "（只允许 <root>/.ai/runtime/** 或系统临时目录；写其它位置一律拒绝）"
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：默认快照写 stdout，``--output`` 时写受控文件；摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    state_path = (
        Path(args.state) if args.state is not None else root / ".ai" / "PROJECT_STATE.json"
    )

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    snapshot = build_planner_snapshot(
        root=root,
        state_path=state_path,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        generated_at=args.generated_at,
    )

    rendered = render_planner_snapshot(snapshot)

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

        print(f"[info] snapshot 已写入受控路径: {target}", file=sys.stderr)

    for issue in snapshot["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    return planner_snapshot_exit_code(snapshot)


if __name__ == "__main__":
    raise SystemExit(main())
