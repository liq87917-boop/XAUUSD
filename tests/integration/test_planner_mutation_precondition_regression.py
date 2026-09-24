"""真实仓库上的 GPT Planner 远端 HEAD 并发保护回归（GOLD-038）。

为什么放在 integration
----------------------
- 只有真实 Git 仓库才能证明 ``observed_head_sha`` / ``branch`` 真的来自只读 Git 事实：
  测试自己用 ``git rev-parse`` 独立复算，而不是信任被测模块；
- 同时证明命令默认**零写入**：``.ai/tasks`` / ``.ai/results`` / ``PROJECT_STATE`` /
  ``GPT_REVIEW_LEDGER`` 在命令前后字节完全不变，工作树状态也不变。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_mutation_precondition as precondition
from orchestrator import planner_snapshot as planner

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

WRONG_HEAD_SHA = "0" * 40


def git(*args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, check=False)

    assert completed.returncode == 0, (args, completed.stderr)

    return completed.stdout.decode("utf-8", errors="replace").strip()


def worktree_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace")


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def canonical_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def directory_digest(root: Path) -> str:
    """测试**自己**复算目录 digest（独立于被测模块）。"""

    return canonical_digest(tree_digest(root))


def real_head() -> str:
    return git("rev-parse", "HEAD")


def real_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def build_payload(*, expected_head_sha: object = None) -> dict[str, Any]:
    return precondition.build_planner_mutation_precondition(
        root=REPO_ROOT,
        generated_at=AUDIT_TIME,
        expected_head_sha=expected_head_sha,
    )


def project_state() -> dict[str, Any]:
    payload = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)

    return payload


def result_statuses() -> dict[str, str]:
    """测试**自己**读取 results 的 status（独立于被测模块）。"""

    statuses: dict[str, str] = {}

    for path in sorted(RESULTS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))

        statuses[path.stem] = str(payload.get("status", "")).strip().lower()

    return statuses


def expected_state_drift_codes() -> list[str]:
    """测试**自己**复算「执行指针 vs results」漂移所需的稳定 code 集合。"""

    state = project_state()

    statuses = result_statuses()

    project_prefix = None

    for key in ("current_task", "last_completed_task", "last_reviewed_task"):
        value = state.get(key)

        if isinstance(value, str) and "-" in value:
            project_prefix = value.split("-", 1)[0]

            break

    completed = sorted(
        (
            task_id
            for task_id, status in statuses.items()
            if status == "completed"
            and (project_prefix is None or task_id.startswith(f"{project_prefix}-"))
        ),
        key=planner.task_id_sort_key,
    )

    codes: set[str] = set()

    current = state.get("current_task")

    if isinstance(current, str) and statuses.get(current) in {"completed", "blocked"}:
        codes.add(planner.ISSUE_POINTER_BEHIND_RESULTS)

    last_completed = state.get("last_completed_task")

    newest_completed = completed[-1] if completed else None

    if (
        isinstance(last_completed, str)
        and newest_completed is not None
        and last_completed != newest_completed
        and planner.is_newer(newest_completed, last_completed)
    ):
        codes.add(planner.ISSUE_POINTER_BEHIND_RESULTS)

    return sorted(codes)


@pytest.fixture(scope="module")
def head_sha() -> str:
    return real_head()


@pytest.fixture(scope="module")
def branch_name() -> str:
    return real_branch()


def test_head_facts_and_digests_are_independently_verifiable(
    head_sha: str,
    branch_name: str,
) -> None:
    payload = build_payload(expected_head_sha=head_sha)

    # HEAD 事实由测试自己的 `git rev-parse` 复算
    assert payload["remote_head"]["observed_head_sha"] == head_sha
    assert payload["remote_head"]["branch"] == branch_name
    assert payload["remote_head"]["expected_head_sha"] == head_sha
    assert payload["remote_head"]["matches_expected"] is True
    assert payload["remote_head"]["stale"] is False
    assert payload["remote_head"]["resolved"] is True

    # 目录 / state digest 由测试自己的 hashlib 复算
    assert payload["tasks_digest"]["digest"] == directory_digest(TASKS_DIR)
    assert payload["results_digest"]["digest"] == directory_digest(RESULTS_DIR)

    state = PROJECT_STATE.read_bytes()

    assert payload["state"]["blob_sha256"] == hashlib.sha256(state).hexdigest()
    assert payload["state"]["digest"] == canonical_digest(json.loads(state.decode("utf-8")))
    assert payload["state"]["current_task"] == project_state().get("current_task")
    assert payload["state"]["last_completed_task"] == project_state().get("last_completed_task")

    # 队列 / refill / formal review backlog 指针都来自既有只读事实
    assert payload["queue_head"] == payload["refill"]["queue_head"]
    assert payload["task_queue"]["head"] == payload["queue_head"]
    assert payload["review_backlog"]["available"] is True
    assert payload["review_backlog"]["pointer"]["last_reviewed_task"] == project_state().get(
        "last_reviewed_task"
    )
    assert len(payload["refill"]["facts_digest"]) == 64

    # 稳定 precondition_digest + gate 与 blocking code 一一对应
    assert len(payload["precondition_digest"]) == 64

    mutation = payload["planner_mutation"]

    assert mutation["allowed"] == (mutation["blocking_reason_codes"] == [])
    assert mutation["forbidden"] is (not mutation["allowed"])
    assert mutation["requires_reread"] is (not mutation["allowed"])
    assert mutation["tool_can_mutate"] is False
    assert mutation["execution_blocked"] is False
    assert set(mutation["blocking_reason_codes"]) <= set(payload["reason_codes"])

    if mutation["allowed"]:
        assert payload["summary"]["exit_code"] == precondition.EXIT_OK
    else:
        assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_state_result_drift_gate_matches_independent_recomputation(head_sha: str) -> None:
    payload = build_payload(expected_head_sha=head_sha)

    expected_codes = expected_state_drift_codes()

    drift = payload["drift"]

    assert set(expected_codes) <= set(drift["state_fact_codes"])

    if drift["state_fact_codes"]:
        assert precondition.REASON_STATE_RESULT_DRIFT in payload["reason_codes"]
        assert payload["planner_mutation"]["allowed"] is False
        assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED
    else:
        assert precondition.REASON_STATE_RESULT_DRIFT not in payload["reason_codes"]
        assert payload["summary"]["state_result_drift"] is False

    # last_reviewed_task 落后属于 review backlog 事实：它只被报告，**不**作为 gate 依据。
    # （同一个稳定 code 可能同时来自执行指针与 review 指针，因此按 issue 来源逐条核对。）
    assert set(drift["review_pointer_codes"]) <= set(drift["codes"])

    # GOLD-041 单一事实源：`blocking_reason_codes` 必须与 issues 的 gating ERROR fact 集合
    # **恒等**，聚合标记 `STATE_RESULT_DRIFT` 也必须是其中一条真实 ERROR issue。
    gating_error_codes = sorted(
        {
            str(issue["code"])
            for issue in payload["issues"]
            if issue["severity"] == planner.SEVERITY_ERROR
            and not precondition.is_review_pointer_fact(issue)
        }
    )

    assert payload["planner_mutation"]["blocking_reason_codes"] == gating_error_codes

    assert drift["aggregate_code"] == (
        precondition.REASON_STATE_RESULT_DRIFT if drift["state_fact_codes"] else None
    )

    for code in payload["planner_mutation"]["blocking_reason_codes"]:
        assert any(
            issue["code"] == code
            and issue["severity"] == planner.SEVERITY_ERROR
            and not precondition.is_review_pointer_fact(issue)
            for issue in payload["issues"]
        ), code


def test_stale_expected_head_on_real_repository_is_fail_closed(head_sha: str) -> None:
    fresh = build_payload(expected_head_sha=head_sha)

    stale = build_payload(expected_head_sha=WRONG_HEAD_SHA)

    assert stale["remote_head"]["observed_head_sha"] == head_sha
    assert stale["remote_head"]["expected_head_sha"] == WRONG_HEAD_SHA
    assert stale["remote_head"]["matches_expected"] is False
    assert stale["remote_head"]["stale"] is True
    assert precondition.REASON_STALE_REMOTE_HEAD in stale["reason_codes"]
    assert stale["planner_mutation"]["allowed"] is False
    assert stale["planner_mutation"]["forbidden"] is True
    assert stale["planner_mutation"]["requires_reread"] is True
    assert stale["planner_mutation"]["auto_merge"] is False
    assert stale["planner_mutation"]["auto_rebase"] is False
    assert stale["authority"]["force_push"] is False
    assert stale["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED

    # 只有 HEAD 相关事实变化：旧 precondition 与当前仓库事实不再匹配
    assert stale["precondition_digest"] != fresh["precondition_digest"]
    assert stale["tasks_digest"] == fresh["tasks_digest"]
    assert stale["results_digest"] == fresh["results_digest"]
    assert stale["state"]["blob_sha256"] == fresh["state"]["blob_sha256"]
    assert stale["queue_head"] == fresh["queue_head"]


def test_cli_is_idempotent_ascii_and_zero_write(head_sha: str) -> None:
    command = [
        sys.executable,
        "-m",
        "orchestrator.planner_mutation_precondition",
        "--generated-at",
        AUDIT_TIME,
        "--expected-head-sha",
        head_sha,
    ]

    before_tasks = tree_digest(TASKS_DIR)

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    run_first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    run_second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert run_first.returncode == run_second.returncode

    assert run_first.stdout == run_second.stdout
    assert run_first.stdout.isascii()

    payload = json.loads(run_first.stdout.decode("ascii"))

    assert payload["schema"] == precondition.PRECONDITION_SCHEMA
    assert payload["remote_head"]["observed_head_sha"] == head_sha
    assert run_first.returncode == payload["summary"]["exit_code"]

    # 退出码由**门禁事实**决定（GOLD-038）：`review 指针落后`属于 backlog 事实，
    # 只被报告、绝不 gate（见 test_review_pointer_lag_alone_does_not_block_planner_mutation）。
    gating_error_codes = sorted(
        {
            str(issue["code"])
            for issue in payload["issues"]
            if issue["severity"] == planner.SEVERITY_ERROR
            and not precondition.is_review_pointer_fact(issue)
        }
    )

    if gating_error_codes:
        assert run_first.returncode in (
            precondition.EXIT_FAIL_CLOSED,
            precondition.EXIT_STATE_UNREADABLE,
        )
    else:
        assert run_first.returncode == precondition.EXIT_OK

    # GOLD-041 单一事实源：`blocking_reason_codes` 必须与 issues 里的 gating ERROR 集合
    # **恒等** —— 包括聚合标记 STATE_RESULT_DRIFT 也必须是真实 ERROR issue；既不允许多出
    # 无事实支撑的 code，也不允许真实 gating fact 被漏掉。
    assert payload["planner_mutation"]["blocking_reason_codes"] == gating_error_codes

    # review 指针滞后（`last_reviewed_task`）只报告、绝不作为 gate 依据：每个 blocking code 都
    # 必须有**非 review 指针**的 ERROR issue 支撑。同一个稳定 code 可能同时来自执行指针与
    # review 指针，因此按 issue 来源逐条核对，而不是按 code 集合比较。
    for code in payload["planner_mutation"]["blocking_reason_codes"]:
        assert any(
            issue["code"] == code
            and not precondition.is_review_pointer_fact(issue)
            for issue in payload["issues"]
            if issue["severity"] == planner.SEVERITY_ERROR
        ), code

    # 库 API 与 CLI 必须给出同一份事实包（同一 digest）
    assert (
        payload["precondition_digest"]
        == build_payload(expected_head_sha=head_sha)["precondition_digest"]
    )

    # 前后零改写
    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_phase33_blocker_and_trading_invariants_unchanged(head_sha: str) -> None:
    payload = build_payload(expected_head_sha=head_sha)

    state = project_state()

    assert state["phase"] == "Phase 3"
    assert state["status"] == "BLOCKED"
    assert "PHASE3_3_DATA" in [blocker["code"] for blocker in state["blockers"]]
    assert "LIVE_TRADING=false" in state["invariants"]
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]

    assert payload["state"]["phase"] == "Phase 3"
    assert "PHASE3_3_DATA" in payload["state"]["blocker_codes"]

    authority = payload["authority"]

    assert authority["tool_can_cross_human_gate"] is False
    assert authority["tool_can_qualify_data"] is False
    assert authority["tool_can_transition_phase"] is False
    assert authority["planning_authority"] == "gpt_only"
    assert authority["executor_can_modify_project_state"] is False
