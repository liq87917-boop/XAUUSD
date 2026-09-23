"""GPT Rolling Queue Autopilot 三任务前瞻契约（GOLD-036）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2.10 规定：GPT Planner 在「当前正在执行 /
即将执行的 queue head」之外，默认维持 **3 个**已批准 follow-on task
（``planner_lookahead_size=3``）。GOLD-034 已把「当前任务之外还剩几个、缺几个」
变成只读事实包（``orchestrator.planner_refill_request``），GOLD-035 已让
Orchestrator 在低水位主动提示。但事实与提示都只是**观察**：一旦有人把
follow-on target 悄悄降为 0、把 Executor 变成 Planner、或让 human-gated 的
hard gate 被绕过，仓库里没有任何机器可测的契约能拦住这类回归。

本模块把该策略固化成**机器可测试契约**（deterministic / read-only / 无副作用）：

1. :data:`LOOKAHEAD_TARGET` == 3 与 :data:`FOLLOW_ON_TARGET_MINIMUM` == 1：
   任何「把 follow-on target 降为 0」的改动都会直接违反契约（fail-closed 拒绝）；
2. :data:`PLANNING_AUTHORITY` == ``"gpt_only"`` 与 :data:`EXECUTOR_CAN_REFILL`
   == ``False``：Executor 永远不能补队列、不能生成 follow-on task，也不能自我
   提升为 Planner；
3. :data:`HARD_GATE_TAIL_IS_ONLY_DEFICIT_EXCEPTION` == ``True``：完全
   human-only（确实不存在合法并行工作）时允许停在 gated tail，但**不得**为了
   凑数量制造 filler task；
4. :func:`audit_follow_on_plan`：对一组**候选** follow-on task（GPT 规划结果，
   或回归测试构造的反例）做确定性审计；命中违规即返回稳定的 ``violation``
   code，例如把 Phase 3.4 功能任务标成可执行
   （:data:`VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE`），或把可运行任务排在
   human-gated tail 之后（:data:`VIOLATION_RUNNABLE_TASK_AFTER_GATED_TAIL`）。

职责边界（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读 / 纯函数**：本模块不写任何文件、不读网络、不读数据库、不调用
  LLM / GPT / DeepSeek（仓库里**不存在** planner API key，也**不要求**任何
  环境变量）；它只对**调用方传入**的 task 结构做确定性判定；
- **不规划**：本模块不生成 task、不补队列、不改 ``PROJECT_STATE`` /
  ``GPT_REVIEW_LEDGER`` / result、不决定 Phase、不跨越 L3/L4；
- **不经本地 Executor 调 GPT**：GPT 规划权的触发方式与职责边界只以只读事实
  形式说明（:func:`gpt_cloud_check_boundary`），本地 Executor 绝不代 GPT 生成
  task；
- **不改变安全红线**：``PHASE3_3_DATA``、``LIVE_TRADING=false``、
  ``ALLOW_EXTERNAL_ORDER_SUBMISSION=false`` 全部原样只读，绝不解除。

用法
----
.. code-block:: python

    from orchestrator import planner_autopilot_contract as contract

    report = contract.audit_follow_on_plan(
        tasks=[{"task_id": "GOLD-037", "type": "CONTROL_PLANE_REVIEW_BACKLOG"}],
        blockers=["PHASE3_3_DATA"],
    )
    report["compliant"]      # True（control-plane 工作不受 blocker 影响）
    report["executor_can_refill"]  # False（规划权仍只属于 GPT）
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# ============================================================
# 版本化契约（shape 变化必须同时 bump 字符串 schema 与整数 schema_version）
# ============================================================

PLANNER_AUTOPILOT_CONTRACT_SCHEMA = "gold-ai/planner-autopilot-contract/v1"

PLANNER_AUTOPILOT_CONTRACT_SCHEMA_VERSION = 1

# 默认前瞻目标：当前任务之外应保留的已批准 follow-on 数量（§2.10）。
# 必须与 ``orchestrator.planner_refill_request.LOOKAHEAD_TARGET`` 一致（测试锁定）。
LOOKAHEAD_TARGET = 3

# 前瞻目标的**下限**：任何把 follow-on target 降为 0 的改动都必须被拒绝。
FOLLOW_ON_TARGET_MINIMUM = 1

# 规划权 / 补队列权：只有 GPT（云端）拥有，Executor 永远不能补队列。
PLANNING_AUTHORITY = "gpt_only"

EXECUTOR_CAN_REFILL = False

# 唯一允许「不足目标」的例外：完全 human-only 的 hard gate tail。
HARD_GATE_TAIL_IS_ONLY_DEFICIT_EXCEPTION = True

# 必须与 ``orchestrator.ai_orchestrator.BLOCKING_HUMAN_GATES`` 一致（测试锁定）。
BLOCKING_HUMAN_GATES = frozenset({"L3", "L4"})

# 允许出现在 task 元数据里的 human_gate 词表（与 ai_orchestrator 同口径）。
SUPPORTED_HUMAN_GATES = frozenset({"L1", "L2", "L3", "L4"})

# human_gate 类型非法时的稳定哨兵值：既不是「无 Gate」，也绝不可执行。
HUMAN_GATE_INVALID = "INVALID"

# ============================================================
# 工作类别词表（只描述「这个任务在做什么」，不描述「它应该排第几个」）
# ============================================================

# 面向 blocker 的修复 / 诊断工作（blocker 未解除前最该做的事）。
WORK_CLASS_BLOCKER_FACING = "BLOCKER_FACING"

# 控制面工作：编排、规划事实、评审门禁、恢复、守卫、自动化基础设施。
WORK_CLASS_CONTROL_PLANE = "CONTROL_PLANE"

# 证据准备工作：真实证据的接收入口、预检、处置链、材料级核验。
WORK_CLASS_EVIDENCE_PREPARATION = "EVIDENCE_PREPARATION"

# 功能 / 模型 / 策略工作：Phase 3.4 及之后的正常开发内容。
WORK_CLASS_FEATURE = "FEATURE"

# 纯粹为了凑满数量而存在的占位任务（本契约明确禁止）。
WORK_CLASS_FILLER = "FILLER"

# 无法分类：按 fail-closed 处理（当作不可接受的可执行工作）。
WORK_CLASS_UNKNOWN = "UNKNOWN"

WORK_CLASSES = (
    WORK_CLASS_BLOCKER_FACING,
    WORK_CLASS_CONTROL_PLANE,
    WORK_CLASS_EVIDENCE_PREPARATION,
    WORK_CLASS_FEATURE,
    WORK_CLASS_FILLER,
    WORK_CLASS_UNKNOWN,
)

# blocker 存在时**允许被标记可执行**的工作类别（自动规划的唯一合法范围）。
WORK_CLASSES_UNDER_PHASE_BLOCKER = (
    WORK_CLASS_BLOCKER_FACING,
    WORK_CLASS_CONTROL_PLANE,
    WORK_CLASS_EVIDENCE_PREPARATION,
)

# blocker 存在时**禁止被标记可执行**的工作类别。
FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER = (
    WORK_CLASS_FEATURE,
    WORK_CLASS_FILLER,
    WORK_CLASS_UNKNOWN,
)

# 从 task ``type`` 前缀推导工作类别（确定性、大小写不敏感、首个匹配生效）。
WORK_CLASS_TYPE_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (WORK_CLASS_FILLER, ("FILLER", "PLACEHOLDER")),
    (
        WORK_CLASS_EVIDENCE_PREPARATION,
        ("PHASE3_3_EVIDENCE", "EVIDENCE", "INTAKE", "ATTESTATION"),
    ),
    (
        WORK_CLASS_BLOCKER_FACING,
        ("PHASE3_3_BLOCKER", "BUGFIX_BLOCKER", "BLOCKER"),
    ),
    (
        WORK_CLASS_CONTROL_PLANE,
        (
            "CONTROL_PLANE",
            "PLANNER",
            "ORCHESTRATOR",
            "AUTOMATION_INFRA",
            "INTEGRATION_GUARD",
            "RECOVERY",
            "QUEUE",
            "GOVERNANCE",
        ),
    ),
    (
        WORK_CLASS_FEATURE,
        (
            "FEATURE",
            "ALPHA",
            "MODEL",
            "STRATEGY",
            "BACKTEST",
            "REGIME",
            "TRAINING",
            "SKILL",
            "WEIGHT",
        ),
    ),
)

# 候选 task 上可选携带的显式工作类别字段（显式声明优先于 type 前缀推导）。
WORK_CLASS_KEYS = ("work_class", "task_class", "work_category")

# 候选 task 上可选携带的 filler 标记字段（任何为真即视为 filler）。
FILLER_FLAG_KEYS = ("filler", "is_filler", "placeholder")

# 候选 task 上可选携带的「规划器声称该任务可执行」字段（用于审计 hard gate 绕过）。
PLANNED_EXECUTABLE_KEYS = ("planned_executable", "plan_executable", "executable")

# ============================================================
# Phase 阻塞事实（只读：只看 PROJECT_STATE 声明的 blocker code）
# ============================================================

# 数据资格 blocker：未解除前一律不得进入 Phase 3.4。
PHASE_GATING_BLOCKER_CODES = ("PHASE3_3_DATA",)

# blocker 存在时被禁止的目标 Phase（只读字符串 + 机器可比较的版本元组）。
FORBIDDEN_PHASE_UNDER_BLOCKER = "Phase 3.4"

MIN_FORBIDDEN_PHASE = (3, 4)

# 候选 task 上可选携带的目标 Phase 字段。
PHASE_TARGET_KEYS = ("target_phase", "phase_target", "phase", "target_phase_label", "min_phase")

# 从字符串里解析 ``3`` / ``3.4`` / ``Phase 3.4`` / ``PHASE3_4`` 这类 Phase 表达。
PHASE_NUMBER_PATTERN = re.compile(r"(\d+)(?:[._](\d+))?")

# ============================================================
# 稳定 violation code 词表（供 GPT / 人工 grep 与测试断言）
# ============================================================

# Executor 试图变成 Planner（或声称拥有补队列能力）。
VIOLATION_EXECUTOR_CLAIMS_PLANNER = "EXECUTOR_CLAIMS_PLANNER"

# follow-on target 被降到下限以下（典型：「降为 0」回归）。
VIOLATION_FOLLOW_ON_TARGET_BELOW_MINIMUM = "FOLLOW_ON_TARGET_BELOW_MINIMUM"

# 为了凑数量制造 filler task（hard gate tail 也不允许）。
VIOLATION_FILLER_TASK_FORBIDDEN = "FILLER_TASK_FORBIDDEN"

# 候选 plan 声称 human-gated / auto_start=false 任务可执行（绕过 hard gate）。
VIOLATION_HUMAN_GATE_BYPASS = "HUMAN_GATE_BYPASS"

# 候选 plan 的 executable 声明与确定性的可执行判定不一致（元数据非法等）。
VIOLATION_PLAN_EXECUTABLE_INCONSISTENT = "PLAN_EXECUTABLE_INCONSISTENT"

# Phase blocker 存在时，不可接受的工作类别被标记可执行（例如功能任务）。
VIOLATION_INADMISSIBLE_WORK_CLASS_EXECUTABLE = (
    "INADMISSIBLE_WORK_CLASS_EXECUTABLE_UNDER_PHASE_BLOCKER"
)

# Phase blocker 存在时，Phase 3.4 功能任务被标记可执行（最具体的红线）。
VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE = "PHASE34_FEATURE_TASK_EXECUTABLE"

# 可运行任务被排在 human-gated tail 之后（Orchestrator 不得跳过 queue head）。
VIOLATION_RUNNABLE_TASK_AFTER_GATED_TAIL = "RUNNABLE_TASK_AFTER_GATED_TAIL"

VIOLATION_CODES = (
    VIOLATION_EXECUTOR_CLAIMS_PLANNER,
    VIOLATION_FOLLOW_ON_TARGET_BELOW_MINIMUM,
    VIOLATION_FILLER_TASK_FORBIDDEN,
    VIOLATION_HUMAN_GATE_BYPASS,
    VIOLATION_PLAN_EXECUTABLE_INCONSISTENT,
    VIOLATION_INADMISSIBLE_WORK_CLASS_EXECUTABLE,
    VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE,
    VIOLATION_RUNNABLE_TASK_AFTER_GATED_TAIL,
)

# 本契约的确定性与职责边界声明（只有规则，不含任何状态事实）。
CONTRACT_DETERMINISM: dict[str, Any] = {
    "digest_field": "plan_digest",
    "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
    "wall_clock_in_facts": False,
    "eligibility_uses_wall_clock": False,
    "reads_files": False,
    "writes_files": False,
    "uses_network": False,
    "uses_database": False,
    "uses_llm": False,
    "planner_api_key_required": False,
    "pure_function": True,
}


# ============================================================
# 只读 task 事实派生（fail-closed：类型可疑一律按「不可执行」处理）
# ============================================================


def _first_string(task: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """只读取出第一个非空字符串字段（类型非法 / 缺失 ⇒ ``None``）。"""

    for key in keys:
        value = task.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def task_work_class(task: Mapping[str, Any]) -> str:
    """判定候选 task 的工作类别（显式声明优先，其次 ``type`` 前缀）。"""

    explicit = _first_string(task, WORK_CLASS_KEYS)

    if explicit is not None:
        normalized = explicit.strip().upper()

        if normalized in WORK_CLASSES:
            return normalized

        return WORK_CLASS_UNKNOWN

    task_type = _first_string(task, ("type",))

    if task_type is not None:
        normalized_type = task_type.strip().upper()

        for work_class, prefixes in WORK_CLASS_TYPE_PREFIXES:
            if normalized_type.startswith(prefixes):
                return work_class

    return WORK_CLASS_UNKNOWN


def task_is_filler(task: Mapping[str, Any]) -> bool:
    """是否为「为了凑数量」的 filler task（显式标记或 work_class 词表）。"""

    for key in FILLER_FLAG_KEYS:
        if task.get(key) is True:
            return True

    return task_work_class(task) == WORK_CLASS_FILLER


def task_human_gate(task: Mapping[str, Any]) -> str | None:
    """只读规范化 ``human_gate``：无 Gate ⇒ ``None``；非法类型 ⇒ ``INVALID``。"""

    gate = task.get("human_gate")

    if isinstance(gate, Mapping):
        gate = gate.get("level")

    if gate is None:
        return None

    if not isinstance(gate, str):
        return HUMAN_GATE_INVALID

    normalized = gate.strip().upper()

    return normalized or None


def task_is_human_blocked(task: Mapping[str, Any]) -> bool:
    """是否被 human gate / 人工审批挡住（含元数据非法，fail-closed）。"""

    gate = task_human_gate(task)

    if gate is None:
        gate_blocks = False
    elif gate == HUMAN_GATE_INVALID:
        gate_blocks = True
    else:
        gate_blocks = gate in BLOCKING_HUMAN_GATES

    auto_start = task.get("auto_start", True)

    approval = task.get("requires_human_approval", False)

    return gate_blocks or auto_start is not True or approval is not False


def task_is_executable(task: Mapping[str, Any]) -> bool:
    """确定性地判定候选 task 能否被 rolling queue 自动执行（fail-closed）。"""

    auto_start = task.get("auto_start", True)

    if not isinstance(auto_start, bool) or not auto_start:
        return False

    approval = task.get("requires_human_approval", False)

    if not isinstance(approval, bool) or approval:
        return False

    gate = task_human_gate(task)

    if gate is None:
        return True

    if gate not in SUPPORTED_HUMAN_GATES:
        return False

    return gate not in BLOCKING_HUMAN_GATES


def task_planned_executable(task: Mapping[str, Any]) -> bool | None:
    """只读取出「规划器声称该任务可执行」的显式声明（缺失 / 非法 ⇒ ``None``）。"""

    for key in PLANNED_EXECUTABLE_KEYS:
        value = task.get(key)

        if isinstance(value, bool):
            return value

    return None


def parse_phase(value: object) -> tuple[int, int] | None:
    """把 ``3`` / ``3.4`` / ``"Phase 3.4"`` / ``"PHASE3_4"`` 解析成可比较的版本元组。"""

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return (value, 0)

    if isinstance(value, float):
        text = repr(value)
    elif isinstance(value, str):
        text = value
    else:
        return None

    match = PHASE_NUMBER_PATTERN.search(text)

    if match is None:
        return None

    major = int(match.group(1))

    minor = int(match.group(2)) if match.group(2) is not None else 0

    return (major, minor)


def task_target_phase(task: Mapping[str, Any]) -> tuple[int, int] | None:
    """只读取出候选 task 声明的目标 Phase（缺失 / 非法 ⇒ ``None``）。"""

    for key in PHASE_TARGET_KEYS:
        if key not in task:
            continue

        parsed = parse_phase(task.get(key))

        if parsed is not None:
            return parsed

    return None


def task_targets_forbidden_phase(task: Mapping[str, Any]) -> bool:
    """目标 Phase 是否落在 blocker 下被禁止的区间（>= Phase 3.4）。"""

    target = task_target_phase(task)

    return target is not None and target >= MIN_FORBIDDEN_PHASE


def phase_blocker_facts(blockers: Iterable[str]) -> dict[str, Any]:
    """只读汇总 Phase 阻塞事实：哪些 blocker code 正在阻断 Phase 前进。"""

    normalized: list[str] = []

    for blocker in blockers:
        code = str(blocker).strip()

        if code and code not in normalized:
            normalized.append(code)

    active = [code for code in normalized if code in PHASE_GATING_BLOCKER_CODES]

    return {
        "declared": normalized,
        "phase_gating": active,
        "phase_blocker_active": bool(active),
        "phase_gating_blocker_codes": list(PHASE_GATING_BLOCKER_CODES),
        "forbidden_phase": FORBIDDEN_PHASE_UNDER_BLOCKER,
        "min_forbidden_phase": list(MIN_FORBIDDEN_PHASE),
    }


# ============================================================
# 契约事实（只读、确定性）
# ============================================================


def gpt_cloud_check_boundary() -> dict[str, Any]:
    """GPT 云端条件检查与本地三任务缓冲的职责边界（只读事实，不含任何密钥）。"""

    return {
        "planner_runtime": "gpt_cloud",
        "planner_trigger": "conditional_check_on_platform_schedule",
        "planner_api_key_in_repo": False,
        "planner_api_key_env_required": False,
        "local_executor_can_call_gpt": False,
        "local_executor_can_generate_task": False,
        "local_buffer_role": "three_approved_follow_on_tasks",
        "local_buffer_target": LOOKAHEAD_TARGET,
        "local_buffer_signal_codes": [
            "GPT_PLANNER_REFILL_REQUIRED",
            "GPT_PLANNER_REFILL_SATISFIED",
        ],
        "local_buffer_tools": [
            "orchestrator.planner_refill_request",
            "orchestrator.planner_autopilot_contract",
        ],
        "exception_recovery": [
            "low_watermark_signal_is_posted_by_orchestrator_after_push_and_when_idle",
            "gpt_rereads_readonly_facts_before_writing_any_task_or_state",
            "if_remote_head_moved_gpt_rereads_facts_instead_of_forcing_push",
            "if_only_human_only_hard_gate_remains_queue_may_stop_at_gated_tail",
            "executor_never_fills_the_queue_even_when_target_is_not_met",
        ],
    }


def contract_facts() -> dict[str, Any]:
    """本契约的冻结事实（确定性字典；只含规则与边界，不含任何任务内容）。"""

    return {
        "schema": PLANNER_AUTOPILOT_CONTRACT_SCHEMA,
        "schema_version": PLANNER_AUTOPILOT_CONTRACT_SCHEMA_VERSION,
        "read_only": True,
        "planning_authority": PLANNING_AUTHORITY,
        "executor_can_refill": EXECUTOR_CAN_REFILL,
        "executor_can_generate_follow_on_task": False,
        "executor_can_modify_project_state": False,
        "executor_can_decide_phase": False,
        "executor_can_cross_human_gate": False,
        "lookahead_target": LOOKAHEAD_TARGET,
        "lookahead_target_is_frozen": True,
        "follow_on_target_minimum": FOLLOW_ON_TARGET_MINIMUM,
        "lookahead_target_below_minimum_allowed": False,
        "hard_gate_tail_allowed_is_only_deficit_exception": (
            HARD_GATE_TAIL_IS_ONLY_DEFICIT_EXCEPTION
        ),
        "stop_at_human_only_tail_allowed": True,
        "filler_tasks_allowed": False,
        "blocking_human_gates": sorted(BLOCKING_HUMAN_GATES),
        "runnable_tasks_must_precede_gated_tail": True,
        "phase_gating_blocker_codes": list(PHASE_GATING_BLOCKER_CODES),
        "admissible_work_classes_under_phase_blocker": list(WORK_CLASSES_UNDER_PHASE_BLOCKER),
        "forbidden_work_classes_under_phase_blocker": list(
            FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER
        ),
        "forbidden_phase_under_blocker": FORBIDDEN_PHASE_UNDER_BLOCKER,
        "work_class_vocabulary": list(WORK_CLASSES),
        "violation_codes": list(VIOLATION_CODES),
        "gpt_cloud_check_boundary": gpt_cloud_check_boundary(),
        "determinism": dict(CONTRACT_DETERMINISM),
    }


# ============================================================
# 确定性审计（纯函数；只读调用方传入的候选 plan）
# ============================================================


def _violation(code: str, index: int, task_id: str | None, detail: str) -> dict[str, Any]:
    """构造一条确定性 violation 记录（稳定 code + 可选 task_id + 人类可读 detail）。"""

    return {"code": code, "index": index, "task_id": task_id, "detail": detail}


def _plan_entries(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """只读构建候选 plan 的逐项事实（顺序即 plan 顺序）。"""

    entries: list[dict[str, Any]] = []

    for index, task in enumerate(tasks):
        target_phase = task_target_phase(task)

        work_class = task_work_class(task)

        entries.append(
            {
                "index": index,
                "task_id": _first_string(task, ("task_id", "id")),
                "work_class": work_class,
                "human_gate": task_human_gate(task),
                "auto_start": task.get("auto_start", True),
                "requires_human_approval": task.get("requires_human_approval", False),
                "target_phase": list(target_phase) if target_phase is not None else None,
                "executable": task_is_executable(task),
                "planned_executable": task_planned_executable(task),
                "filler": task_is_filler(task),
                "admissible_work_class": (
                    work_class not in FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER
                ),
            }
        )

    return entries


def _collect_violations(
    entries: Sequence[dict[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    *,
    phase_blocker_active: bool,
) -> list[dict[str, Any]]:
    """按 plan 顺序收集 violation（最终排序由调用方统一完成）。"""

    violations: list[dict[str, Any]] = []

    for entry, task in zip(entries, tasks, strict=True):
        index = int(entry["index"])
        task_id = entry["task_id"]

        if entry["filler"]:
            violations.append(
                _violation(
                    VIOLATION_FILLER_TASK_FORBIDDEN,
                    index,
                    task_id,
                    "候选 plan 含 filler task：不得为满足数量指标创造无价值任务",
                )
            )

        if entry["planned_executable"] is True and not entry["executable"]:
            if task_is_human_blocked(task):
                violations.append(
                    _violation(
                        VIOLATION_HUMAN_GATE_BYPASS,
                        index,
                        task_id,
                        "候选 plan 把 human-gated / auto_start=false 任务标记为可执行"
                        "（绕过 hard gate）",
                    )
                )
            else:
                violations.append(
                    _violation(
                        VIOLATION_PLAN_EXECUTABLE_INCONSISTENT,
                        index,
                        task_id,
                        "候选 plan 的 executable 声明与确定性可执行判定不一致（fail-closed）",
                    )
                )

        if phase_blocker_active and entry["executable"] and not entry["admissible_work_class"]:
            if task_targets_forbidden_phase(task):
                violations.append(
                    _violation(
                        VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE,
                        index,
                        task_id,
                        (
                            f"Phase blocker 存在时 {FORBIDDEN_PHASE_UNDER_BLOCKER} 功能任务"
                            f"（work_class={entry['work_class']}）不得被标记可执行"
                        ),
                    )
                )
            else:
                violations.append(
                    _violation(
                        VIOLATION_INADMISSIBLE_WORK_CLASS_EXECUTABLE,
                        index,
                        task_id,
                        (
                            "Phase blocker 存在时可执行任务只允许 "
                            f"{list(WORK_CLASSES_UNDER_PHASE_BLOCKER)}，"
                            f"当前 work_class={entry['work_class']}"
                        ),
                    )
                )

    first_gated = next(
        (int(entry["index"]) for entry in entries if not entry["executable"]),
        None,
    )

    if first_gated is not None:
        for entry in entries:
            if int(entry["index"]) > first_gated and entry["executable"]:
                violations.append(
                    _violation(
                        VIOLATION_RUNNABLE_TASK_AFTER_GATED_TAIL,
                        int(entry["index"]),
                        entry["task_id"],
                        "可运行任务必须排在 human-gated / deferred tail 之前"
                        "（不得跳过 queue head）",
                    )
                )

    return violations


def audit_follow_on_plan(
    *,
    tasks: Sequence[Mapping[str, Any]],
    blockers: Iterable[str] = (),
    lookahead_target: int = LOOKAHEAD_TARGET,
    executor_claims_planner: bool = False,
    origin: str = "caller_supplied_plan",
) -> dict[str, Any]:
    """审计一组候选 follow-on task 是否符合三任务前瞻契约（只读、纯函数）。

    传入的 ``tasks`` 是**候选** GPT 规划结果（或回归测试构造的**反例**）；
    本函数不读取仓库、不写任何文件、不调用任何 LLM，只做确定性判定。
    命中违规即 ``compliant=False``，``reason_codes`` 只取稳定 violation code：

    - ``EXECUTOR_CLAIMS_PLANNER``：Executor 试图成为 Planner / 补队列；
    - ``FOLLOW_ON_TARGET_BELOW_MINIMUM``：target 被降到下限以下（典型「降为 0」）；
    - ``FILLER_TASK_FORBIDDEN``：为凑数量制造 filler task；
    - ``HUMAN_GATE_BYPASS``：把 human-gated / ``auto_start=false`` 任务标成可执行；
    - ``PLAN_EXECUTABLE_INCONSISTENT``：可执行声明与判定不一致（fail-closed）；
    - ``INADMISSIBLE_WORK_CLASS_EXECUTABLE_UNDER_PHASE_BLOCKER`` /
      ``PHASE34_FEATURE_TASK_EXECUTABLE``：blocker 存在时不可接受的类别被标记可执行；
    - ``RUNNABLE_TASK_AFTER_GATED_TAIL``：可运行任务被排在 gated tail 之后。
    """

    target_valid = (
        isinstance(lookahead_target, int)
        and not isinstance(lookahead_target, bool)
        and lookahead_target >= FOLLOW_ON_TARGET_MINIMUM
    )

    effective_target = lookahead_target if target_valid else LOOKAHEAD_TARGET

    phase = phase_blocker_facts(blockers)

    entries = _plan_entries(tasks)

    violations: list[dict[str, Any]] = []

    if not target_valid:
        violations.append(
            _violation(
                VIOLATION_FOLLOW_ON_TARGET_BELOW_MINIMUM,
                -1,
                None,
                (
                    f"follow-on target={lookahead_target!r} 低于下限 "
                    f"{FOLLOW_ON_TARGET_MINIMUM}：必须 fail-safe 回落 {LOOKAHEAD_TARGET}"
                ),
            )
        )

    if executor_claims_planner:
        violations.append(
            _violation(
                VIOLATION_EXECUTOR_CLAIMS_PLANNER,
                -1,
                None,
                (
                    "Executor 不得自我提升为 Planner / 不得补队列"
                    f"（planning_authority={PLANNING_AUTHORITY}, "
                    f"executor_can_refill={EXECUTOR_CAN_REFILL}）"
                ),
            )
        )

    violations.extend(
        _collect_violations(
            entries,
            tasks,
            phase_blocker_active=bool(phase["phase_blocker_active"]),
        )
    )

    violations.sort(
        key=lambda item: (str(item["code"]), int(item["index"]), str(item["task_id"]))
    )

    follow_on_count = len(entries)

    deficit = max(0, int(effective_target) - follow_on_count)

    executable_indices = [int(entry["index"]) for entry in entries if entry["executable"]]

    hard_gate_tail_allowed = bool(entries) and not executable_indices

    if phase["phase_blocker_active"]:
        admissible = list(WORK_CLASSES_UNDER_PHASE_BLOCKER)
        forbidden = list(FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER)
    else:
        admissible = list(WORK_CLASSES)
        forbidden = []

    report: dict[str, Any] = {
        "schema": PLANNER_AUTOPILOT_CONTRACT_SCHEMA,
        "schema_version": PLANNER_AUTOPILOT_CONTRACT_SCHEMA_VERSION,
        "read_only": True,
        "origin": origin,
        "planning_authority": PLANNING_AUTHORITY,
        "executor_can_refill": EXECUTOR_CAN_REFILL,
        "lookahead_target": lookahead_target,
        "effective_lookahead_target": int(effective_target),
        "lookahead_target_valid": target_valid,
        "follow_on_target_minimum": FOLLOW_ON_TARGET_MINIMUM,
        "follow_on_count": follow_on_count,
        "deficit": deficit,
        "refill_required": deficit > 0,
        "hard_gate_tail_allowed": hard_gate_tail_allowed,
        "stop_at_human_only_tail_allowed": hard_gate_tail_allowed,
        "executable_task_indices": executable_indices,
        "phase_blocker_active": bool(phase["phase_blocker_active"]),
        "phase_blockers": list(phase["phase_gating"]),
        "declared_blockers": list(phase["declared"]),
        "forbidden_phase_under_blocker": FORBIDDEN_PHASE_UNDER_BLOCKER,
        "admissible_work_classes": admissible,
        "forbidden_work_classes": forbidden,
        "tasks": entries,
        "violations": violations,
        "violation_codes": sorted({str(item["code"]) for item in violations}),
        "reason_codes": sorted({str(item["code"]) for item in violations}),
        "compliant": not violations,
        "determinism": dict(CONTRACT_DETERMINISM),
    }

    report["plan_digest"] = plan_digest(report)

    return report


def plan_digest(report: Mapping[str, Any]) -> str:
    """确定性审计报告 digest：相同候选 plan ⇒ 相同 digest（无自引用、无 wall-clock）。"""

    facts = {key: value for key, value in report.items() if key != "plan_digest"}

    canonical = json.dumps(facts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
