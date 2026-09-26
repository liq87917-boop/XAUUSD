"""真实仓库上的 GOLD-036 三任务前瞻契约端到端回归（只读）。

为什么放在 integration
----------------------
- 只有真实仓库才能证明：契约事实与 GOLD-034 refill 事实包**同源**
  （``planner_authority.autopilot_contract`` 内嵌同一份契约事实），
  且真实 ``PROJECT_STATE`` 的 ``PHASE3_3_DATA`` blocker 会让 Phase 3.4 功能任务
  在机器门禁下直接 fail-closed；
- 只有真实仓库才能证明：真实 rolling queue 的 follow-on 数量 / 缺口 / 是否需补
  与契约审计**逐项一致**（不产生第二套判断）；
- 同时证明整条链路在真实仓库上**零写入**：``.ai/tasks`` / ``.ai/results`` /
  ``.ai/PROJECT_STATE.json`` / ``.ai/GPT_REVIEW_LEDGER.json`` 逐字节不变，
  ``git status --porcelain`` 前后一致。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout，
绝不生成或追加任何 task。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_autopilot_contract as contract
from orchestrator import planner_refill_request as refill

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

# 任何形式的 planner API key / 凭据痕迹：orchestrator 侧必须为零。
FORBIDDEN_SECRET_SNIPPETS = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "sk-",
    "Authorization",
    "Bearer ",
)


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


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)

    return payload


def refill_payload() -> dict[str, Any]:
    """只读运行 GOLD-034 CLI（固定审计时点，零写入）。"""

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchestrator.planner_refill_request",
            "--generated-at",
            AUDIT_TIME,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode in {refill.EXIT_OK, refill.EXIT_REFILL_REQUIRED}, completed.stderr

    assert completed.stdout.isascii()

    return json.loads(completed.stdout.decode("ascii"))


def blocker_codes(state: dict[str, Any]) -> list[str]:
    entries: list[Any] = []

    blockers = state.get("blockers")
    history = state.get("history_blockers")

    if isinstance(blockers, list):
        entries.extend(blockers)
    if isinstance(history, list):
        entries.extend(history)

    return [str(entry["code"]) for entry in entries if isinstance(entry, dict) and "code" in entry]


def follow_on_tasks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """只读构建真实 follow-on 候选 plan（queue head 之后的真实 task 文件）。"""

    pending = [str(task_id) for task_id in (payload["queue"]["pending"] or [])]

    follow_on_ids = [task_id for task_id in pending if task_id != payload["queue_head"]]

    tasks: list[dict[str, Any]] = []

    for task_id in follow_on_ids:
        path = TASKS_DIR / f"{task_id}.json"

        assert path.is_file(), f"queue 里的 task 文件缺失: {task_id}"

        tasks.append(read_json(path))

    return tasks


def audit_real_plan(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return contract.audit_follow_on_plan(
        tasks=follow_on_tasks(payload),
        blockers=blocker_codes(state),
        lookahead_target=int(payload["lookahead_target"]),
        origin="real_repo_queue",
    )


# ============================================================
# 1. refill 事实包内嵌冻结契约（requirement 1）
# ============================================================


def test_refill_payload_embeds_frozen_autopilot_contract() -> None:
    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    authority = payload["planner_authority"]
    embedded = authority["autopilot_contract"]

    assert embedded == contract.contract_facts()
    assert embedded["lookahead_target"] == 3
    assert embedded["follow_on_target_minimum"] == 1
    assert embedded["executor_can_refill"] is False
    assert embedded["planning_authority"] == "gpt_only"
    assert embedded["hard_gate_tail_allowed_is_only_deficit_exception"] is True
    assert embedded["filler_tasks_allowed"] is False

    assert authority["executor_can_refill"] is False
    assert payload["summary"]["executor_can_refill"] is False
    assert payload["summary"]["follow_on_target_minimum"] == 1

    # 事实包报告的 target 必须等于 PROJECT_STATE 声明值（缺省即 3）
    assert payload["lookahead_target"] == state.get("planner_lookahead_size", 3)
    assert payload["lookahead_target"] >= contract.FOLLOW_ON_TARGET_MINIMUM

    # 未声明覆盖时，确定性默认值必须是 3
    assert refill.resolve_lookahead_target({}) == (3, refill.LOOKAHEAD_SOURCE_DEFAULT)


def test_refill_hint_line_carries_frozen_contract_facts() -> None:
    payload = refill_payload()

    line = orch.planner_refill_hint_line(payload, context=orch.REFILL_HINT_CONTEXT_POST_PUSH)

    assert line.startswith(
        orch.PLANNER_REFILL_REQUIRED_CODE
        if payload["refill_required"]
        else orch.PLANNER_REFILL_SATISFIED_CODE
    )
    assert f"follow_on_count={payload['follow_on_count']}" in line
    assert f"target={payload['lookahead_target']}" in line
    assert f"deficit={payload['deficit']}" in line
    assert f"hard_gate_tail_allowed={payload['hard_gate_tail_allowed']}" in line
    assert "executor_can_refill=false" in line


# ============================================================
# 2. 真实队列 ↔ 契约矩阵逐项一致（requirement 2）
# ============================================================


def test_real_queue_follow_on_matrix_matches_contract_audit() -> None:
    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    report = audit_real_plan(payload, state)

    # 契约审计与 refill 事实包必须同源同数（不产生第二套判断）
    assert report["follow_on_count"] == payload["follow_on_count"]
    assert report["lookahead_target"] == payload["lookahead_target"]
    assert report["deficit"] == payload["deficit"]
    assert report["refill_required"] == payload["refill_required"]
    assert report["executor_can_refill"] is False
    assert report["planning_authority"] == "gpt_only"

    # 前瞻矩阵在真实数据上自洽：>= target ⇒ 不缺；< target ⇒ 缺口 = target - follow_on
    target = int(payload["lookahead_target"])
    follow_on = int(payload["follow_on_count"])

    if follow_on >= target:
        assert payload["deficit"] == 0
        assert payload["refill_required"] is False
    else:
        assert payload["deficit"] == target - follow_on
        assert payload["refill_required"] is True

    # queue head 可运行时不得声称「只剩 human-only hard gate」
    if payload["queue_head_runnable"]:
        assert payload["hard_gate_tail_allowed"] is False
        assert report["hard_gate_tail_allowed"] is False


def test_real_queue_only_marks_admissible_work_executable_under_blocker() -> None:
    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    report = audit_real_plan(payload, state)

    assert report["phase_blocker_active"] is True
    assert report["phase_blockers"] == ["PHASE3_3_DATA"]
    assert report["compliant"] is True, report["violations"]

    admissible = set(contract.WORK_CLASSES_UNDER_PHASE_BLOCKER)

    for entry in report["tasks"]:
        if entry["executable"]:
            assert entry["work_class"] in admissible, entry


# ============================================================
# 3. PHASE3_3_DATA 下的 Phase 3.4 功能任务门禁（requirement 3）
# ============================================================


def test_real_project_state_activates_phase_blocker_gate() -> None:
    state = read_json(PROJECT_STATE)

    codes = blocker_codes(state)

    assert "PHASE3_3_DATA" in codes
    assert "LIVE_TRADING=false" in (state.get("invariants") or [])
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in (state.get("invariants") or [])

    report = contract.audit_follow_on_plan(tasks=[], blockers=codes)

    assert report["phase_blocker_active"] is True
    assert report["phase_blockers"] == ["PHASE3_3_DATA"]
    assert report["admissible_work_classes"] == list(contract.WORK_CLASSES_UNDER_PHASE_BLOCKER)
    assert report["forbidden_work_classes"] == list(
        contract.FORBIDDEN_WORK_CLASSES_UNDER_PHASE_BLOCKER
    )
    assert report["forbidden_phase_under_blocker"] == "Phase 3.4"


def test_real_plan_is_compliant_while_phase34_counterexample_is_rejected() -> None:
    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    codes = blocker_codes(state)
    real_plan = follow_on_tasks(payload)

    assert contract.audit_follow_on_plan(tasks=real_plan, blockers=codes)["compliant"] is True

    base = real_plan[0] if real_plan else {"task_id": "GOLD-XXX", "type": "CONTROL_PLANE_X"}

    counterexample = dict(base)

    counterexample.update({"type": "FEATURE", "target_phase": "Phase 3.4", "auto_start": True})
    counterexample.pop("human_gate", None)
    counterexample.pop("requires_human_approval", None)

    report = contract.audit_follow_on_plan(tasks=[counterexample], blockers=codes)

    assert report["compliant"] is False
    assert report["tasks"][0]["work_class"] == contract.WORK_CLASS_FEATURE
    assert report["tasks"][0]["executable"] is True
    assert contract.VIOLATION_PHASE34_FEATURE_TASK_EXECUTABLE in report["reason_codes"]


# ============================================================
# 4. 真实仓库零写入 + 无 planner API key / 无本地 GPT 调用（requirement 5）
# ============================================================


def test_contract_chain_writes_nothing_on_real_repo() -> None:
    before_tasks = tree_digest(TASKS_DIR)
    before_results = tree_digest(RESULTS_DIR)
    before_state = PROJECT_STATE.read_bytes()
    before_ledger = REVIEW_LEDGER.read_bytes()
    before_status = worktree_status()

    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    contract.contract_facts()
    contract.gpt_cloud_check_boundary()
    audit_real_plan(payload, state)
    orch.planner_refill_hint_line(payload, context=orch.REFILL_HINT_CONTEXT_IDLE)
    orch.planner_refill_facts()

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_orchestrator_carries_no_planner_api_key_or_provider_call() -> None:
    for path in sorted(ORCHESTRATOR_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")

        for snippet in FORBIDDEN_SECRET_SNIPPETS:
            assert snippet not in source, f"{path.name}: {snippet}"

    boundary = contract.gpt_cloud_check_boundary()

    assert boundary["planner_api_key_in_repo"] is False
    assert boundary["planner_api_key_env_required"] is False
    assert boundary["local_executor_can_call_gpt"] is False
    assert boundary["local_executor_can_generate_task"] is False

    refill_public = {name for name in dir(refill) if not name.startswith("_")}

    for forbidden in (
        "generate_follow_on_task",
        "refill_rolling_queue",
        "next_task",
        "write_task",
        "modify_project_state",
    ):
        assert forbidden not in refill_public


def test_contract_report_never_exposes_task_content() -> None:
    payload = refill_payload()
    state = read_json(PROJECT_STATE)

    report = audit_real_plan(payload, state)

    for entry in report["tasks"]:
        assert {"task_id", "work_class", "executable"} <= set(entry)
        assert "title" not in entry
        assert "goal" not in entry
        assert "requirements" not in entry

