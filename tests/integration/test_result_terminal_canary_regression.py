"""结果终态 canary 的**新进程**端到端回归（GOLD-047）。

为什么放在 integration
----------------------
- 只有**真实子进程**才能证明「终态解析 → 归一化 → terminal-consistency → result 持久化
  决策 → completion commit 决策」是由**新进程加载的真模块**执行的，而不是复用测试进程内
  monkeypatch / 旧模块状态；
- 同时证明 canary 的两条红线：真实 ``.ai/results`` / ``.ai/tasks`` / ``PROJECT_STATE`` /
  ``GPT_REVIEW_LEDGER`` 与 worktree 在命令前后**逐字节不变**，且绝不 spawn 真实
  Cline / 真实 git / 网络；
- 还证明 canary 自身**不是**空过：新进程边界被污染（父进程已 import pytest 与旧模块）时
  必须 fail-closed 并给出稳定 reason code。

红线：本文件只读 ``.ai/**``，绝不 commit / push / reset / checkout，也绝不改写历史 result。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import result_terminal_canary as canary
from orchestrator import result_terminal_consistency as terminal

REPO_ROOT = Path(__file__).resolve().parents[2]

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

PROBE_FILE = REPO_ROOT / "orchestrator" / "result_terminal_canary_probe.py"

# 故意污染新进程边界的驱动脚本：先 import pytest 与边界模块，再调用 canary。
BOUNDARY_DRIVER = """from __future__ import annotations

import sys

import pytest  # noqa: F401 - 故意污染边界
from orchestrator import ai_orchestrator  # noqa: F401
from orchestrator import result_terminal_canary as canary
from orchestrator import result_terminal_consistency  # noqa: F401

raise SystemExit(canary.emit(canary.run_canary(sys.argv[1])))
"""

STABLE_FACT_KEYS = (
    "result_status",
    "result_execution_outcome",
    "result_normalized_finish_reason",
    "attempt_execution_outcome",
    "attempt_cline_finish_reason_raw",
    "attempt_failure_class",
    "attempt_failure_code",
    "validations_all_passed",
    "completion_commit_recorded",
    "completion_gate_accepted",
    "produced_completed",
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


def run_canary_new_process(
    workdir: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)

    env["PYTHONPATH"] = str(REPO_ROOT)

    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, "-m", "orchestrator.result_terminal_canary", str(workdir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
        env=env,
    )


def decode_report(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    decoded = completed.stdout.decode("ascii", errors="replace")

    payload = json.loads(decoded)

    assert isinstance(payload, dict)

    return payload


def by_raw(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(scenario["raw"]): scenario
        for scenario in report["scenarios"]
        if isinstance(scenario, dict)
    }


# ============================================================
# 1. 新进程 canary PASS，且真实仓库零改写
# ============================================================


def test_new_process_canary_passes_and_never_touches_real_repository(tmp_path: Path) -> None:
    workdir = tmp_path / "canary-sandbox"

    before_results = tree_digest(RESULTS_DIR)

    before_tasks = tree_digest(TASKS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    completed = run_canary_new_process(workdir)

    report = decode_report(completed)

    assert completed.returncode == canary.EXIT_PASS

    assert completed.stdout.isascii()

    assert report["schema"] == canary.SCHEMA

    assert report["status"] == canary.VERDICT_PASS

    assert report["verdict"] == canary.VERDICT_PASS

    assert report["reason_codes"] == []

    assert report["pipeline_entry"] == canary.PIPELINE_ENTRY

    # 新进程边界必须是干净的：未 import pytest，边界模块都是这一进程新加载的。
    assert report["fresh_process"] is True

    assert report["pytest_imported"] is False

    assert report["authority_rule_consistent"] is True

    assert Path(report["ai_orchestrator_file"]).resolve() == (
        REPO_ROOT / "orchestrator" / "ai_orchestrator.py"
    )

    assert Path(report["authority_module_file"]).resolve() == (
        REPO_ROOT / "orchestrator" / "result_terminal_consistency.py"
    )

    workdir_resolved = workdir.resolve()

    assert workdir_resolved in Path(report["observed_task_dir"]).resolve().parents

    assert workdir_resolved in Path(report["observed_result_dir"]).resolve().parents

    assert report["dirs_ok"] is True

    assert set(report["verdicts"].values()) == {canary.VERDICT_PASS}

    assert set(by_raw(report)) == {canary.RAW_COMPLETED, canary.RAW_ABORTED}

    # 真实仓库（历史 result / task / state / ledger / worktree）逐字节不变。
    assert tree_digest(RESULTS_DIR) == before_results

    assert tree_digest(TASKS_DIR) == before_tasks

    assert PROJECT_STATE.read_bytes() == before_state

    assert REVIEW_LEDGER.read_bytes() == before_ledger

    assert worktree_status() == before_status

    # canary 的 probe 文件只允许存在于临时 workdir，绝不落到真实仓库。
    assert not PROBE_FILE.exists()


# ============================================================
# 2. raw completed 可用，raw aborted 不得形成 completed / completion commit
# ============================================================


def test_canary_proves_raw_aborted_and_raw_completed_semantics_independently(
    tmp_path: Path,
) -> None:
    report = decode_report(run_canary_new_process(tmp_path / "canary-semantics"))

    scenarios = by_raw(report)

    assert scenarios[canary.RAW_COMPLETED]["reason_codes"] == []

    assert scenarios[canary.RAW_ABORTED]["reason_codes"] == []

    completed = scenarios[canary.RAW_COMPLETED]

    aborted = scenarios[canary.RAW_ABORTED]

    # 两条场景必须是**同样降级**的前置：exit 0 + validation 全过 + 工作树有变更。
    for facts in (completed, aborted):
        assert facts["attempt_cline_exit_code"] == 0

        assert facts["validations_all_passed"] is True

        assert facts["attempt_changed_files"]

        assert facts["result_present"] is True

        assert facts["iteration_outcome"] in (canary.STATUS_COMPLETED, canary.STATUS_BLOCKED)

    # raw completed：正常路径必须保持可用（completed result + completion commit）。
    assert completed["attempt_cline_finish_reason_raw"] == canary.RAW_COMPLETED

    assert completed["attempt_execution_outcome"] == canary.OUTCOME_COMPLETED

    assert completed["result_status"] == canary.STATUS_COMPLETED

    assert completed["result_execution_outcome"] == canary.OUTCOME_COMPLETED

    assert completed["result_normalized_finish_reason"] == canary.OUTCOME_COMPLETED

    assert completed["completion_commit_recorded"] is True

    assert completed["completion_gate_accepted"] is True

    assert canary.completion_commit_command(completed["task_id"]) in completed["commit_commands"]

    # raw aborted：退出码 0、validation 全过、工作树有变更，仍必须 fail-closed。
    assert aborted["attempt_cline_finish_reason_raw"] == canary.RAW_ABORTED

    assert aborted["attempt_execution_outcome"] == canary.OUTCOME_FAILED

    assert aborted["attempt_failure_class"] == "terminal_not_completed"

    assert aborted["attempt_failure_code"] == "CLINE_TERMINAL_NOT_COMPLETED"

    assert aborted["result_status"] == canary.STATUS_BLOCKED

    assert aborted["result_execution_outcome"] == canary.STATUS_BLOCKED

    assert aborted["result_normalized_finish_reason"] == canary.STATUS_BLOCKED

    assert aborted["produced_completed"] is False

    assert aborted["completion_commit_recorded"] is False

    assert aborted["completion_gate_accepted"] is False

    # completion-commit 门禁必须由**权威模块自己的**稳定 code 拒绝。
    assert aborted["completion_gate_reason_code"] in terminal.REASON_CODES

    assert canary.completion_commit_command(aborted["task_id"]) not in aborted["commit_commands"]

    assert all("ai: complete" not in command for command in aborted["commit_commands"])


# ============================================================
# 3. 新进程语义稳定（两次独立运行机器可读结果一致）
# ============================================================


def test_new_process_semantics_are_stable_across_runs(tmp_path: Path) -> None:
    first = decode_report(run_canary_new_process(tmp_path / "run-a"))

    second = decode_report(run_canary_new_process(tmp_path / "run-b"))

    assert first["status"] == second["status"] == canary.VERDICT_PASS

    assert first["reason_codes"] == second["reason_codes"] == []

    assert first["verdicts"] == second["verdicts"]

    assert first["fresh_process"] is True

    assert second["fresh_process"] is True

    first_scenarios = by_raw(first)

    second_scenarios = by_raw(second)

    assert set(first_scenarios) == set(second_scenarios)

    for raw, facts in first_scenarios.items():
        assert {key: facts[key] for key in STABLE_FACT_KEYS} == {
            key: second_scenarios[raw][key] for key in STABLE_FACT_KEYS
        }


def test_canary_is_immune_to_parent_process_monkeypatch_state(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """父进程（测试进程）里的模块状态被改写时，新进程 canary 必须不受影响。"""

    from orchestrator import ai_orchestrator as orch

    poisoned_results = tmp_path / "poisoned-results"

    poisoned_tasks = tmp_path / "poisoned-tasks"

    monkeypatch.setattr(orch, "RESULT_DIR", poisoned_results)

    monkeypatch.setattr(orch, "TASK_DIR", poisoned_tasks)

    workdir = tmp_path / "canary-sandbox"

    report = decode_report(run_canary_new_process(workdir))

    assert report["status"] == canary.VERDICT_PASS

    assert report["reason_codes"] == []

    assert report["fresh_process"] is True

    # 新进程只写自己的临时 workdir；父进程的「污染目录」必须零创建。
    assert not poisoned_results.exists()

    assert not poisoned_tasks.exists()

    assert tmp_path in Path(report["observed_result_dir"]).resolve().parents


# ============================================================
# 4. 边界被污染时 must fail-closed（canary 不是空过）
# ============================================================


def test_canary_fails_closed_when_fresh_import_boundary_is_broken(tmp_path: Path) -> None:
    driver = tmp_path / "boundary_driver.py"

    driver.write_text(BOUNDARY_DRIVER, encoding="utf-8")

    env = dict(os.environ)

    env["PYTHONPATH"] = str(REPO_ROOT)

    completed = subprocess.run(
        [sys.executable, str(driver), str(tmp_path / "broken-boundary")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
        env=env,
    )

    assert completed.returncode == canary.EXIT_FAIL

    payload = json.loads(completed.stdout.decode("ascii", errors="replace"))

    assert payload["status"] == canary.VERDICT_FAIL

    assert payload["fresh_process"] is False

    assert payload["pytest_imported"] is True

    assert canary.RESULT_TERMINAL_CANARY_FRESH_IMPORT_BOUNDARY_BROKEN in payload["reason_codes"]


# ============================================================
# 5. Phase 3.3 blocker / 交易安全不变量不变
# ============================================================


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    state = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert state["phase"] == "Phase 3"

    assert state["status"] == "BLOCKED"

    blocker_codes = {blocker["code"] for blocker in (state.get("blockers") or [])}
    history_codes = {blocker["code"] for blocker in (state.get("history_blockers") or [])}
    assert "PHASE3_3_DATA" in (blocker_codes | history_codes)

    assert "LIVE_TRADING=false" in state["invariants"]

    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]

    assert state["queue_status"] in {"ACTIVE", "HOLD", "BLOCKED"}
    if state["status"] == "BLOCKED":
        assert state["queue_status"] in {"BLOCKED", "HOLD"}
