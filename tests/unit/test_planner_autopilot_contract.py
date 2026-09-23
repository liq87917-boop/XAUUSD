"""GOLD-036：GPT 三任务前瞻自动补队列契约（``orchestrator.planner_autopilot_contract``）。

覆盖：
1) 冻结常量：``lookahead_target`` 与 refill 事实包同源为 3、target 下限为 1、
   ``executor_can_refill=False``、``planning_authority="gpt_only"``、
   blocking human gates = L3/L4、violation / work_class 词表唯一且完整划分；
2) 端到端前瞻矩阵：running+3 follow-ons ⇒ 无需补；running+2 ⇒ 缺 1；
   running+0 ⇒ 缺 3；可运行任务必须在 human-gated tail 之前；
   完全 human-only 时允许停线但不得制造 filler；
3) Phase 3.3 blocker 门禁：``PHASE3_3_DATA`` 存在时可执行的只允许
   blocker-facing / control-plane / evidence-preparation 工作；Phase 3.4 功能任务
   被标记可执行一律 fail-closed；
4) 反例门禁：Executor 自我提升为 Planner、把 follow-on target 降为 0、
   绕过 hard gate、制造 filler 全部被稳定 violation code 拒绝；
5) 只读 / 无副作用：模块不读 / 不写文件、无网络 / LLM / API key / 规划 API，
   ``plan_digest`` 确定性且对候选 plan 变化敏感。

所有测试只在内存结构上运行，绝不对真实仓库做任何读写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_autopilot_contract as contract
from orchestrator import planner_refill_request as refill

MODULE_SOURCE = Path(contract.__file__).read_text(encoding="utf-8")

PHASE_BLOCKER = "PHASE3_3_DATA"

# Executor 绝不允许出现的规划 / 状态写入 API（与 GOLD-023/034 守卫同口径）。
FORBIDDEN_PLANNING_API = (
    "generate_follow_on_task",
    "create_task",
    "write_task",
    "append_task",
    "refill_rolling_queue",
    "plan_next_task",
    "next_task",
    "write_result",
    "update_project_state",
    "modify_project_state",
    "decide_phase",
    "loosen_acceptance",
    "review_task_result",
)

# 源码守卫：这些子串一旦出现，说明本模块可能有文件 / 进程 / 网络 / 环境副作用。
FORBIDDEN_SOURCE_SNIPPETS = (
    "write_text",
    "write_bytes",
    "open(",
    "mkdir",
    "rmtree",
    "shutil",
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "aiohttp",
    "import os",
    "os.remove",
    "os.replace",
    "os.environ",
    "getenv",
)

# 任何形式的 planner API key / 凭据痕迹：本契约必须为零。
FORBIDDEN_SECRET_SNIPPETS = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "sk-",
    "Authorization",
    "Bearer ",
)


# ============================================================
# 只读辅助（全部发生在内存）
# ============================================================


def task(task_id: str, **extra: Any) -> dict[str, Any]:
    """构造一个候选 task 结构（只读输入，绝不落盘）。"""

    return {"task_id": task_id, **extra}


def plan(*tasks: dict[str, Any]) -> list[dict[str, Any]]:
    return list(tasks)


def audit(**kwargs: Any) -> dict[str, Any]:
    return contract.audit_follow_on_plan(**kwargs)


def collect_keys(payload: object) -> set[str]:
    found: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            found |= collect_keys(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_keys(item)

    return found


# ============================================================
# 1. 冻结常量与词表
# ============================================================


def test_contract_constants_freeze_three_task_lookahead() -> None:
    assert contract.LOOKAHEAD_TARGET == 3
    assert contract.FOLLOW_ON_TARGET_MINIMUM == 1
    assert contract.PLANNING_AUTHORITY == "gpt_only"
    assert contract.EXECUTOR_CAN_REFILL is False
    assert contract.HARD_GATE_TAIL_IS_ONLY_DEFICIT_EXCEPTION is True
    assert frozenset({"L3", "L4"}) == contract.BLOCKING_HUMAN_GATES
    assert frozenset({"L1", "L2", "L3", "L4"}) == contract.SUPPORTED_HUMAN_GATES
    assert contract.PHASE_GATING_BLOCKER_CODES == ("PHASE3_3_DATA",)
    assert contract.FORBIDDEN_PHASE_UNDER_BLOCKER == "Phase 3.4"
    assert contract.MIN_FORBIDDEN_PHASE == (3, 4)


def test_lookahead_target_matches_refill_budget() -> None:
    assert contract.LOOKAHEAD_TARGET == refill.LOOKAHEAD_TARGET == 3
    assert refill.PLANNER_LOOKAHEAD_STATE_KEY == "planner_lookahead_size"


def test_vocabularies_are_unique_and_partitioned() -> None:
    for vocabulary in (
        contract.VIOLATION_CODES,
        contract.WORK_CLASSES,
        contract.WORK_CLASSES_UNDER_PHASE_BLOCKER,
        contract.FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER,
    ):
        assert len(set(vocabulary)) == len(vocabulary)

    admissible = set(contract.WORK_CLASSES_UNDER_PHASE_BLOCKER)
    forbidden = set(contract.FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER)

    assert admissible & forbidden == set()
    assert admissible | forbidden == set(contract.WORK_CLASSES)

    for code in contract.VIOLATION_CODES:
        assert code and code == code.upper()


def test_contract_facts_freeze_authority_and_target() -> None:
    facts = contract.contract_facts()

    assert facts["schema"] == contract.PLANNER_AUTOPILOT_CONTRACT_SCHEMA
    assert facts["schema_version"] == contract.PLANNER_AUTOPILOT_CONTRACT_SCHEMA_VERSION == 1
    assert facts["read_only"] is True
    assert facts["planning_authority"] == "gpt_only"
    assert facts["lookahead_target"] == 3
    assert facts["lookahead_target_is_frozen"] is True
    assert facts["follow_on_target_minimum"] == 1
    assert facts["lookahead_target_below_minimum_allowed"] is False
    assert facts["hard_gate_tail_allowed_is_only_deficit_exception"] is True
    assert facts["stop_at_human_only_tail_allowed"] is True
    assert facts["filler_tasks_allowed"] is False
    assert facts["runnable_tasks_must_precede_gated_tail"] is True
    assert facts["blocking_human_gates"] == ["L3", "L4"]
    assert facts["phase_gating_blocker_codes"] == ["PHASE3_3_DATA"]
    assert facts["forbidden_phase_under_blocker"] == "Phase 3.4"
    assert facts["violation_codes"] == list(contract.VIOLATION_CODES)

    for key, value in facts.items():
        if key.startswith("executor_can_"):
            assert value is False, key


# ============================================================
# 2. 端到端前瞻矩阵（3 / 2 / 0 follow-ons、排序、human-only）
# ============================================================


def test_matrix_running_plus_three_follow_ons_needs_no_refill() -> None:
    report = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_GPT_AUTOPILOT_CONTRACT"),
            task("GOLD-037", type="CONTROL_PLANE_REVIEW_BACKLOG"),
            task("GOLD-038", type="CONTROL_PLANE_PLANNER_CONCURRENCY_GUARD"),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert report["follow_on_count"] == 3
    assert report["lookahead_target"] == 3
    assert report["deficit"] == 0
    assert report["refill_required"] is False
    assert report["compliant"] is True
    assert report["hard_gate_tail_allowed"] is False
    assert report["reason_codes"] == []
    assert report["executable_task_indices"] == [0, 1, 2]


def test_matrix_running_plus_two_follow_ons_is_one_short() -> None:
    report = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_GPT_AUTOPILOT_CONTRACT"),
            task("GOLD-037", type="CONTROL_PLANE_REVIEW_BACKLOG"),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert report["follow_on_count"] == 2
    assert report["deficit"] == 1
    assert report["refill_required"] is True
    # 缺口只是计数事实：合规性由工作类别与排序决定，不因缺 1 而违规
    assert report["compliant"] is True
    assert report["reason_codes"] == []


def test_matrix_running_plus_zero_follow_ons_is_three_short() -> None:
    report = audit(tasks=plan(), blockers=[PHASE_BLOCKER])

    assert report["follow_on_count"] == 0
    assert report["deficit"] == 3
    assert report["refill_required"] is True
    # 空队列不是 human-only gated tail：不得据此停线
    assert report["hard_gate_tail_allowed"] is False


@pytest.mark.parametrize("bad_target", [0, -1, True, "3", 3.0, None])
def test_follow_on_target_below_minimum_is_rejected(bad_target: Any) -> None:
    report = audit(
        tasks=plan(task("GOLD-036", type="CONTROL_PLANE_X")),
        lookahead_target=bad_target,
    )

    assert report["lookahead_target_valid"] is False
    assert report["effective_lookahead_target"] == contract.LOOKAHEAD_TARGET
    assert contract.VIOLATION_FOLLOW_ON_TARGET_BELOW_MINIMUM in report["reason_codes"]
    assert report["compliant"] is False


def test_runnable_tasks_must_precede_human_gated_tail() -> None:
    compliant = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_X"),
            task("GOLD-037", type="CONTROL_PLANE_Y", human_gate="L4"),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert compliant["compliant"] is True
    assert compliant["executable_task_indices"] == [0]

    violating = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_X", human_gate="L4"),
            task("GOLD-037", type="CONTROL_PLANE_Y"),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert violating["compliant"] is False
    assert violating["reason_codes"] == [contract.VIOLATION_RUNNABLE_TASK_AFTER_GATED_TAIL]
    assert [item["task_id"] for item in violating["violations"]] == ["GOLD-037"]


def test_human_only_tail_may_stop_but_must_not_manufacture_filler() -> None:
    human_only = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_X", auto_start=False),
            task("GOLD-037", type="CONTROL_PLANE_Y", requires_human_approval=True),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert human_only["compliant"] is True
    assert human_only["hard_gate_tail_allowed"] is True
    assert human_only["stop_at_human_only_tail_allowed"] is True
    # 计数事实依然保留：GPT 能看到缺几个，但可以合法停在 gated tail
    assert human_only["follow_on_count"] == 2
    assert human_only["deficit"] == 1
    assert human_only["refill_required"] is True

    with_filler = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_X", auto_start=False),
            task("GOLD-FILLER", type="FILLER", filler=True),
        ),
        blockers=[PHASE_BLOCKER],
    )

    assert with_filler["tasks"][1]["work_class"] == contract.WORK_CLASS_FILLER
    assert with_filler["tasks"][1]["filler"] is True
    assert contract.VIOLATION_FILLER_TASK_FORBIDDEN in with_filler["reason_codes"]
    assert with_filler["compliant"] is False


# ============================================================
# 3. 反例门禁（Executor 变 Planner / 绕过 hard gate / 降 target）
# ============================================================


def test_executor_cannot_become_planner() -> None:
    report = audit(tasks=plan(), executor_claims_planner=True)

    assert contract.VIOLATION_EXECUTOR_CLAIMS_PLANNER in report["reason_codes"]
    assert report["compliant"] is False
    assert report["planning_authority"] == "gpt_only"
    assert report["executor_can_refill"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"human_gate": "L3"},
        {"human_gate": "L4"},
        {"human_gate": {"level": "L3"}},
        {"human_gate": 3},
        {"auto_start": False},
        {"requires_human_approval": True},
    ],
)
def test_human_gate_bypass_attempt_is_rejected(extra: dict[str, Any]) -> None:
    report = audit(
        tasks=plan(task("GOLD-036", type="CONTROL_PLANE_X", planned_executable=True, **extra))
    )

    assert report["tasks"][0]["executable"] is False
    assert contract.VIOLATION_HUMAN_GATE_BYPASS in report["reason_codes"]
    assert report["compliant"] is False
    assert report["executable_task_indices"] == []


def test_plan_executable_inconsistency_is_rejected() -> None:
    report = audit(
        tasks=plan(
            task("GOLD-036", type="CONTROL_PLANE_X", human_gate="L9", planned_executable=True)
        )
    )

    assert contract.VIOLATION_PLAN_EXECUTABLE_INCONSISTENT in report["reason_codes"]
    assert contract.VIOLATION_HUMAN_GATE_BYPASS not in report["reason_codes"]
    assert report["compliant"] is False


# ============================================================
# 4. Phase 3.3 blocker 门禁（工作类别 / Phase 3.4 功能任务）
# ============================================================


@pytest.mark.parametrize(
    ("task_type", "expected_class"),
    [
        ("BLOCKER_TOOLING", contract.WORK_CLASS_BLOCKER_FACING),
        ("BUGFIX_BLOCKER_TOOLING", contract.WORK_CLASS_BLOCKER_FACING),
        ("PHASE3_3_BLOCKER_TOOLING", contract.WORK_CLASS_BLOCKER_FACING),
        ("CONTROL_PLANE_REVIEW_BACKLOG", contract.WORK_CLASS_CONTROL_PLANE),
        ("CONTROL_PLANE_QUEUE_LOW_WATERMARK", contract.WORK_CLASS_CONTROL_PLANE),
        ("PLANNER_TOOLING", contract.WORK_CLASS_CONTROL_PLANE),
        ("AUTOMATION_INFRA", contract.WORK_CLASS_CONTROL_PLANE),
        ("INTEGRATION_GUARD", contract.WORK_CLASS_CONTROL_PLANE),
        ("RECOVERY", contract.WORK_CLASS_CONTROL_PLANE),
        ("EVIDENCE_INTAKE", contract.WORK_CLASS_EVIDENCE_PREPARATION),
        ("PHASE3_3_EVIDENCE_TOOLING", contract.WORK_CLASS_EVIDENCE_PREPARATION),
        ("ATTESTATION_PACK", contract.WORK_CLASS_EVIDENCE_PREPARATION),
        ("FEATURE", contract.WORK_CLASS_FEATURE),
        ("ALPHA_PIPELINE", contract.WORK_CLASS_FEATURE),
        ("FILLER", contract.WORK_CLASS_FILLER),
        ("SOMETHING_ELSE", contract.WORK_CLASS_UNKNOWN),
    ],
)
def test_work_class_classification(task_type: str, expected_class: str) -> None:
    assert contract.task_work_class({"type": task_type}) == expected_class

    # 大小写不敏感：自动规划不能靠改了 casing 就逃过门禁
    assert contract.task_work_class({"type": task_type.lower()}) == expected_class


def test_admissible_work_classes_stay_executable_under_phase_blocker() -> None:
    tasks = plan(
        task("GOLD-036", type="BLOCKER_TOOLING"),
        task("GOLD-037", type="CONTROL_PLANE_REVIEW_BACKLOG"),
        task("GOLD-038", type="PHASE3_3_EVIDENCE_TOOLING"),
    )

    report = audit(tasks=tasks, blockers=[PHASE_BLOCKER])

    assert report["phase_blocker_active"] is True
    assert report["phase_blockers"] == [PHASE_BLOCKER]
    assert report["admissible_work_classes"] == list(contract.WORK_CLASSES_UNDER_PHASE_BLOCKER)
    assert report["executable_task_indices"] == [0, 1, 2]
    assert report["compliant"] is True
    assert report["reason_codes"] == []


@pytest.mark.parametrize(
    "phase_value",
    ["Phase 3.4", "PHASE3_4", "3.4", 3.4, "Phase 4.0", "phase_3_4_alpha"],
)
def test_phase34_feature_task_executable_is_rejected_under_blocker(phase_value: Any) -> None:
    report = audit(
        tasks=plan(task("GOLD-900", type="FEATURE", target_phase=phase_value)),
        blockers=[PHASE_BLOCKER],
    )

    assert report["phase_blocker_active"] is True
    assert report["tasks"][0]["work_class"] == contract.WORK_CLASS_FEATURE
    assert report["tasks"][0]["executable"] is True
    assert contract.VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE in report["reason_codes"]
    assert report["compliant"] is False


def test_feature_task_without_forbidden_phase_is_still_inadmissible_under_blocker() -> None:
    report = audit(tasks=plan(task("GOLD-901", type="FEATURE")), blockers=[PHASE_BLOCKER])

    assert contract.VIOLATION_INADMISSIBLE_WORK_CLASS_EXECUTABLE in report["reason_codes"]
    assert contract.VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE not in report["reason_codes"]
    assert report["compliant"] is False


def test_unknown_work_class_executable_is_rejected_under_blocker() -> None:
    report = audit(
        tasks=plan(task("GOLD-904", type="SOMETHING_ELSE")),
        blockers=[PHASE_BLOCKER],
    )

    assert report["tasks"][0]["work_class"] == contract.WORK_CLASS_UNKNOWN
    assert contract.VIOLATION_INADMISSIBLE_WORK_CLASS_EXECUTABLE in report["reason_codes"]
    assert report["compliant"] is False


def test_explicit_work_class_field_overrides_type_prefix() -> None:
    allowed = audit(
        tasks=plan(task("GOLD-905", type="FEATURE", work_class="control_plane")),
        blockers=[PHASE_BLOCKER],
    )

    assert allowed["tasks"][0]["work_class"] == contract.WORK_CLASS_CONTROL_PLANE
    assert allowed["compliant"] is True

    unknown = audit(
        tasks=plan(task("GOLD-906", work_class="NOT_A_CLASS")),
        blockers=[PHASE_BLOCKER],
    )

    assert unknown["tasks"][0]["work_class"] == contract.WORK_CLASS_UNKNOWN
    assert unknown["compliant"] is False


def test_gated_phase34_feature_task_is_not_executable_and_not_a_violation() -> None:
    report = audit(
        tasks=plan(task("GOLD-903", type="FEATURE", target_phase="Phase 3.4", auto_start=False)),
        blockers=[PHASE_BLOCKER],
    )

    assert report["tasks"][0]["executable"] is False
    assert report["compliant"] is True
    assert report["hard_gate_tail_allowed"] is True
    assert report["stop_at_human_only_tail_allowed"] is True


def test_feature_task_is_allowed_when_no_phase_blocker_is_active() -> None:
    report = audit(tasks=plan(task("GOLD-902", type="FEATURE", target_phase="Phase 3.4")))

    assert report["phase_blocker_active"] is False
    assert report["admissible_work_classes"] == list(contract.WORK_CLASSES)
    assert report["forbidden_work_classes"] == []
    assert report["compliant"] is True


def test_non_phase_blocker_does_not_restrict_work_classes() -> None:
    report = audit(
        tasks=plan(task("GOLD-907", type="FEATURE")),
        blockers=["VALIDATION_PYTHON_ENV", "OTHER"],
    )

    assert report["declared_blockers"] == ["VALIDATION_PYTHON_ENV", "OTHER"]
    assert report["phase_blockers"] == []
    assert report["phase_blocker_active"] is False
    assert report["compliant"] is True


def test_phase_blocker_facts_deduplicates_and_ignores_blanks() -> None:
    facts = contract.phase_blocker_facts(["PHASE3_3_DATA", " PHASE3_3_DATA ", "", "OTHER"])

    assert facts["declared"] == ["PHASE3_3_DATA", "OTHER"]
    assert facts["phase_gating"] == ["PHASE3_3_DATA"]
    assert facts["phase_blocker_active"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Phase 3.4", (3, 4)),
        ("PHASE3_4", (3, 4)),
        ("phase 3.3", (3, 3)),
        ("3.4", (3, 4)),
        (3.4, (3, 4)),
        (3, (3, 0)),
        (True, None),
        (None, None),
        ("", None),
        ("not-a-phase", None),
    ],
)
def test_parse_phase(value: Any, expected: tuple[int, int] | None) -> None:
    assert contract.parse_phase(value) == expected


# ============================================================
# 5. 确定性 / 只读 / 无 API key / 无规划 API
# ============================================================


def test_audit_is_deterministic_and_change_sensitive() -> None:
    tasks = plan(
        task("GOLD-036", type="CONTROL_PLANE_X"),
        task("GOLD-037", type="CONTROL_PLANE_Y"),
    )

    first = audit(tasks=tasks, blockers=[PHASE_BLOCKER])
    second = audit(tasks=list(tasks), blockers=[PHASE_BLOCKER])

    assert first == second
    assert first["plan_digest"] == contract.plan_digest(first)

    fewer = audit(tasks=plan(task("GOLD-036", type="CONTROL_PLANE_X")), blockers=[PHASE_BLOCKER])

    assert fewer["plan_digest"] != first["plan_digest"]

    other_blockers = audit(tasks=list(tasks), blockers=[PHASE_BLOCKER, "OTHER"])

    assert other_blockers["plan_digest"] != first["plan_digest"]

    assert first["determinism"]["wall_clock_in_facts"] is False
    assert first["determinism"]["writes_files"] is False


def test_audit_payload_never_contains_task_content_or_decision_keys() -> None:
    report = audit(
        tasks=plan(task("GOLD-036", type="CONTROL_PLANE_X")),
        blockers=[PHASE_BLOCKER],
    )

    keys = collect_keys(report)

    for forbidden in refill.FORBIDDEN_REQUEST_KEYS:
        assert forbidden not in keys, forbidden


def test_module_exposes_no_planning_or_write_api() -> None:
    public = {name for name in dir(contract) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)
    assert "audit_follow_on_plan" in public
    assert "contract_facts" in public
    assert "gpt_cloud_check_boundary" in public

    for snippet in FORBIDDEN_SOURCE_SNIPPETS:
        assert snippet not in MODULE_SOURCE, snippet

    for snippet in FORBIDDEN_SECRET_SNIPPETS:
        assert snippet not in MODULE_SOURCE, snippet


def test_contract_never_stores_or_requires_planner_api_key() -> None:
    boundary = contract.gpt_cloud_check_boundary()

    assert boundary["planner_runtime"] == "gpt_cloud"
    assert boundary["planner_api_key_in_repo"] is False
    assert boundary["planner_api_key_env_required"] is False
    assert boundary["local_executor_can_call_gpt"] is False
    assert boundary["local_executor_can_generate_task"] is False
    assert boundary["local_buffer_target"] == contract.LOOKAHEAD_TARGET == 3
    assert boundary["local_buffer_signal_codes"] == [
        "GPT_PLANNER_REFILL_REQUIRED",
        "GPT_PLANNER_REFILL_SATISFIED",
    ]
    assert boundary["exception_recovery"]

    # 边界事实必须完整出现在契约里（GPT 与人工都能 grep 到同一份说明）
    assert contract.contract_facts()["gpt_cloud_check_boundary"] == boundary

