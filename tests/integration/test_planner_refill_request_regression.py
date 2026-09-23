"""真实仓库上的 Planner refill request 只读回归（GOLD-034）。

为什么放在 integration
----------------------
- 只有真实仓库才能证明该事实包**与真实 rolling queue / PROJECT_STATE / results 一致**：
  测试自己独立构建 planner snapshot，再与 refill facts 逐项对齐；
- 同时证明本工具在真实仓库上**零写入**：``.ai/tasks`` / ``.ai/results`` 逐字节、
  ``PROJECT_STATE`` / ``GPT_REVIEW_LEDGER`` 逐字节、工作树 ``git status`` 前后完全一致。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout，
绝不生成 / 追加任何 task。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

# 版本化契约的顶层字段（shape 冻结：新增字段必须同时 bump schema）。
EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "generated_at",
        "read_only",
        "request_kind",
        "queue_head",
        "queue_head_runnable",
        "queue",
        "lookahead_target",
        "lookahead_target_source",
        "follow_on_count",
        "deficit",
        "refill_required",
        "refill_required_basis",
        "hard_gate_tail_allowed",
        "latest_completed",
        "completed_but_unreviewed",
        "state_result_drift",
        "blockers",
        "human_gates",
        "safety_invariants",
        "phase",
        "planner_authority",
        "reason_codes",
        "snapshot_ref",
        "issues",
        "summary",
        "facts_digest",
        "determinism",
    }
)

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

CLI_COMMAND = [
    sys.executable,
    "-m",
    "orchestrator.planner_refill_request",
    "--generated-at",
    AUDIT_TIME,
]


# ============================================================
# 只读工具函数（全部发生在 tmp / 内存，绝不写仓库）
# ============================================================


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def worktree_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace")


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


def build_request(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"root": REPO_ROOT, "generated_at": AUDIT_TIME}

    kwargs.update(overrides)

    return refill.build_planner_refill_request(**kwargs)


def build_snapshot() -> dict[str, Any]:
    return planner.build_planner_snapshot(root=REPO_ROOT, generated_at=AUDIT_TIME)


# ============================================================
# 1. 事实包 vs 真实 planner snapshot（单一事实来源）
# ============================================================


def test_queue_facts_match_planner_snapshot() -> None:
    payload = build_request()

    snapshot = build_snapshot()

    pending = list(snapshot["queue"]["pending"])

    assert payload["queue"]["pending"] == pending
    assert payload["queue"]["pending_count"] == len(pending)
    assert payload["queue_head"] == (pending[0] if pending else None)
    assert payload["queue"]["follow_on_task_ids"] == pending[1:]
    assert payload["follow_on_count"] == len(pending[1:])

    assert payload["queue"]["queue_status"] == snapshot["queue"]["queue_status"]
    assert payload["queue"]["stop_reason"] == snapshot["queue"]["first_stop_reason"]
    assert payload["queue"]["blocking_gate_at_head"] == snapshot["queue"]["blocking_gate_at_head"]
    assert payload["queue_head_runnable"] == (
        payload["queue_head"] is not None
        and snapshot["queue"]["runnable_task"] == payload["queue_head"]
    )

    # snapshot 引用必须指向同一份事实（可独立复算）
    assert payload["snapshot_ref"]["schema"] == snapshot["schema"]
    assert payload["snapshot_ref"]["schema_version"] == snapshot["schema_version"]
    assert payload["snapshot_ref"]["facts_digest"] == snapshot["facts_digest"]

    snapshot_codes = {issue["code"] for issue in snapshot["issues"]}

    assert snapshot_codes <= set(payload["snapshot_ref"]["issue_codes"])


def test_low_watermark_math_is_internally_consistent() -> None:
    payload = build_request()

    target = payload["lookahead_target"]
    follow_on = payload["follow_on_count"]
    deficit = payload["deficit"]

    assert target == refill.LOOKAHEAD_TARGET == 3
    assert follow_on == len(payload["queue"]["follow_on_task_ids"])
    assert deficit == max(0, target - follow_on)
    assert payload["refill_required"] is (deficit > 0)

    if deficit > 0:
        assert "REFILL_REQUIRED" in payload["reason_codes"]
    else:
        assert "QUEUE_AT_TARGET" in payload["reason_codes"]

    assert payload["hard_gate_tail_allowed"] == payload["human_gates"]["human_only_hard_gate"]
    assert payload["summary"]["deficit"] == deficit
    assert payload["summary"]["follow_on_count"] == follow_on
    assert payload["summary"]["refill_required"] == payload["refill_required"]
    assert payload["summary"]["exit_code"] in {refill.EXIT_OK, refill.EXIT_REFILL_REQUIRED}


def test_completed_and_unreviewed_facts_match_results() -> None:
    payload = build_request()

    snapshot = build_snapshot()

    terminal = dict(snapshot["results"]["terminal"])

    completed = [task_id for task_id, status in terminal.items() if status == "completed"]

    latest_completed = completed[-1] if completed else None

    assert payload["latest_completed"]["task_id"] == latest_completed
    assert payload["latest_completed"]["latest_terminal_result"] == (
        list(terminal)[-1] if terminal else None
    )

    last_reviewed = planner.pointer_value(snapshot["project_state"], "last_reviewed_task")

    assert payload["completed_but_unreviewed"]["last_reviewed_pointer"] == last_reviewed
    assert payload["completed_but_unreviewed"]["review_authority"] == "gpt_only"

    if last_reviewed is not None:
        assert payload["completed_but_unreviewed"]["task_ids"] == [
            task_id for task_id in completed if planner.is_newer(task_id, last_reviewed)
        ]


# ============================================================
# 2. 版本化契约 / 稳定 reason codes / 无任务内容
# ============================================================


def test_frozen_top_level_contract_and_stable_reason_codes() -> None:
    payload = build_request()

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["schema"] == "gold-ai/planner-refill-request/v1"
    assert payload["schema_version"] == refill.PLANNER_REFILL_REQUEST_SCHEMA_VERSION == 1
    assert payload["read_only"] is True
    assert payload["request_kind"] == "planner_refill_request"
    assert payload["generated_at"] == AUDIT_TIME

    assert set(payload) == EXPECTED_TOP_LEVEL_KEYS

    assert payload["reason_codes"]
    assert set(payload["reason_codes"]) <= set(refill.REASON_CODES)
    assert payload["reason_codes"] == sorted(set(payload["reason_codes"]))


def test_payload_has_no_planning_content_or_decision_keys() -> None:
    payload = build_request()

    keys = collect_keys(payload)

    for forbidden in refill.FORBIDDEN_REQUEST_KEYS:
        assert forbidden not in keys, forbidden

    assert "title" not in keys

    authority = payload["planner_authority"]

    assert authority["planning_authority"] == "gpt_only"
    assert authority["next_task_content_included"] is False
    assert authority["tool_can_write_tasks"] is False
    assert authority["tool_can_write_results"] is False
    assert authority["tool_can_write_project_state"] is False
    assert authority["tool_can_write_review_ledger"] is False

    for key, value in authority.items():
        if key.startswith(("executor_can_", "tool_can_")):
            assert value is False, key


def test_module_and_payload_grants_no_refill_authority() -> None:
    public = {name for name in dir(refill) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    for capability in ("generate_follow_on_task", "refill_rolling_queue", "modify_project_state"):
        assert planner.executor_allowed(capability) is False

    assert refill.authority_section()["planning_authority"] == "gpt_only"


# ============================================================
# 3. Phase / Human Gate / 交易安全边界不变
# ============================================================


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    payload = build_request()

    assert "PHASE3_3_DATA" in {blocker["code"] for blocker in payload["blockers"]}
    assert "ACTIVE_BLOCKERS_PRESENT" in payload["reason_codes"]

    assert payload["phase"]["current"] == "Phase 3"
    assert payload["phase"]["transition_allowed"] is False
    assert payload["phase"]["executor_may_transition_phase"] is False

    safety = payload["safety_invariants"]

    assert safety["required"] == ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"]
    assert safety["missing"] == []
    assert safety["all_present"] is True
    assert safety["live_trading_disabled"] is True
    assert safety["external_order_submission_disabled"] is True

    assert payload["human_gates"]["blocking_levels"] == ["L3", "L4"]
    assert payload["human_gates"]["executor_may_cross_human_gate"] is False
    assert payload["human_gates"]["phase_transition_requires_human_gate"] is True
    assert payload["planner_authority"]["phase_transition_requires_human_gate"] is True


# ============================================================
# 4. 真实仓库上的确定性 / 零写入
# ============================================================


def test_build_is_deterministic_and_writes_nothing() -> None:
    before_tasks = tree_digest(TASKS_DIR)
    before_results = tree_digest(RESULTS_DIR)
    before_state = PROJECT_STATE.read_bytes()
    before_ledger = REVIEW_LEDGER.read_bytes()
    before_status = worktree_status()

    first = build_request()
    second = build_request(generated_at="2026-09-24T00:00:00+08:00")

    assert first["facts_digest"] == second["facts_digest"]
    assert first["reason_codes"] == second["reason_codes"]
    assert first["refill_required"] == second["refill_required"]

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_cli_is_deterministic_and_zero_side_effect() -> None:
    before_tasks = tree_digest(TASKS_DIR)
    before_results = tree_digest(RESULTS_DIR)
    before_state = PROJECT_STATE.read_bytes()
    before_ledger = REVIEW_LEDGER.read_bytes()
    before_status = worktree_status()

    first = subprocess.run(CLI_COMMAND, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(CLI_COMMAND, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode in {refill.EXIT_OK, refill.EXIT_REFILL_REQUIRED}
    assert first.returncode == second.returncode
    assert first.stdout == second.stdout
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["summary"]["exit_code"] == first.returncode
    assert payload["generated_at"] == AUDIT_TIME
    assert b"[refill] head=" in first.stderr

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


@pytest.mark.parametrize(
    "relative",
    [".ai/tasks/GOLD-999.json", ".ai/results/GOLD-999.json", ".ai/PROJECT_STATE.json"],
)
def test_cli_output_rejects_tracked_planner_paths(relative: str) -> None:
    before_tasks = tree_digest(TASKS_DIR)
    before_results = tree_digest(RESULTS_DIR)
    before_state = PROJECT_STATE.read_bytes()

    completed = subprocess.run(
        [*CLI_COMMAND, "--output", relative],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode == 4
    assert completed.stdout == b""
    assert b"OUTPUT_PATH_REJECTED" in completed.stderr

    if not relative.endswith("PROJECT_STATE.json"):
        # 被拒的写入目标绝不允许被创建
        assert not (REPO_ROOT / relative).exists()

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
