"""GOLD-034：GPT Planner 队列补给请求事实包（``orchestrator.planner_refill_request``）。

覆盖：
1) 版本化契约 + 只读事实：``schema`` / ``schema_version`` / ``read_only`` 与
   ``queue_head`` / ``follow_on_count`` / ``lookahead_target`` / ``deficit`` /
   ``refill_required`` / ``hard_gate_tail_allowed`` / ``latest_completed`` /
   ``completed_but_unreviewed`` / ``blockers`` / ``human_gates`` / ``safety_invariants``；
2) 场景回归：队列足量、低水位（缺 1）、只有 head（缺 3）、空队列、head 处 L3/L4、
   仅剩 human-only hard gate、state/result 漂移、completed-but-unreviewed；
3) 确定性：``facts_digest`` 与 wall-clock 解耦、对事实变化敏感、可独立复算；
4) 职责边界：事实包里没有任何后续任务内容 / Phase 决定字段，模块没有任何规划 / 写状态 API，
   源码级守卫证明它不写 tracked planner/task/state 文件（唯一写路径是受控 ``--output``）；
5) CLI：纯 ASCII JSON、稳定退出码、``--output`` 只允许 runtime/临时路径（其余 fail-closed）。

所有测试只在 ``tmp_path`` 内构造文件，绝不对真实仓库做任何写操作。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-24T00:00:00+08:00"

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULE_SOURCE = Path(refill.__file__).read_text(encoding="utf-8")

FAKE_BRANCH = "cline-agent"

FAKE_HEAD = "3f1a9c8e7b6d5f4a3b2c1d0e9f8a7b6c5d4e3f21"

# 版本化契约的顶层字段（shape 冻结：新增字段必须同时 bump schema）。
REQUIRED_TOP_LEVEL_KEYS = frozenset(
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

# Executor 绝不允许出现的规划 / 状态写入 API（与 GOLD-023/031/032 守卫同口径）。
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

# 源码守卫：这些子串一旦出现，说明本模块可能产生文件 / 进程 / 网络副作用。
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
)


# ============================================================
# 只读 fake 仓库（tasks / results / PROJECT_STATE / fake .git）
# ============================================================


def seed_fake_git(root: Path, branch: str = FAKE_BRANCH, head: str = FAKE_HEAD) -> None:
    """构造只读 Git 事实（``.git/HEAD`` + loose ref），让快照不依赖真实仓库。"""

    git_dir = root / ".git"

    refs = git_dir / "refs" / "heads"

    refs.mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    (refs / branch).write_text(f"{head}\n", encoding="utf-8")


@dataclass(frozen=True)
class RefillFS:
    root: Path
    tasks: Path
    results: Path
    state_path: Path


@pytest.fixture()
def refill_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RefillFS:
    """把 tasks / results / PROJECT_STATE / runtime 全部重定向到 tmp_path。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"
    runtime = tmp_path / ".ai" / "runtime"

    tasks.mkdir()
    results.mkdir()
    runtime.mkdir(parents=True)

    state_path = tmp_path / "PROJECT_STATE.json"

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(planner, "PROJECT_STATE_PATH", state_path)
    monkeypatch.setattr(planner, "ROOT", tmp_path)

    seed_fake_git(tmp_path)

    return RefillFS(root=tmp_path, tasks=tasks, results=results, state_path=state_path)


def write_task(tasks: Path, task_id: str, **extra: object) -> Path:
    path = tasks / f"{task_id}.json"

    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )

    return path


def write_result(results: Path, task_id: str, status: str) -> Path:
    path = results / f"{task_id}.json"

    path.write_text(
        json.dumps({"task_id": task_id, "status": status}, ensure_ascii=False),
        encoding="utf-8",
    )

    return path


def queue_state(pending: Sequence[str], **overrides: Any) -> dict[str, Any]:
    """零漂移基线 PROJECT_STATE（declared queue == 真实非终态任务）。"""

    state: dict[str, Any] = {
        "schema_version": 1,
        "project": "XAUUSD",
        "branch": FAKE_BRANCH,
        "phase": "Phase 3",
        "status": "ACTIVE",
        "blockers": [],
        "human_gates": [],
        "invariants": ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"],
        "queue_target_size": 3,
        "task_queue": list(pending),
        "queue_status": "ACTIVE",
    }

    state.update(overrides)

    return state


def write_state(fs: RefillFS, state: dict[str, Any]) -> None:
    fs.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_queue(
    fs: RefillFS,
    task_ids: Sequence[str],
    *,
    extras: dict[str, dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写入一批非终态 task + 对应的 PROJECT_STATE，返回实际写入的 state。"""

    extras = extras or {}

    for task_id in task_ids:
        write_task(fs.tasks, task_id, **extras.get(task_id, {}))

    payload = state if state is not None else queue_state(task_ids)

    write_state(fs, payload)

    return payload


def build_request(fs: RefillFS, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "root": fs.root,
        "state_path": fs.state_path,
        "tasks_dir": fs.tasks,
        "results_dir": fs.results,
        "generated_at": AUDIT_TIME,
    }

    kwargs.update(overrides)

    return refill.build_planner_refill_request(**kwargs)


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
# 1. 队列水位场景（足量 / 低水位 / 空队列）
# ============================================================


def test_queue_at_target_reports_no_deficit(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-010", "GOLD-011", "GOLD-012", "GOLD-013"])

    payload = build_request(refill_fs)

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["schema_version"] == refill.PLANNER_REFILL_REQUEST_SCHEMA_VERSION == 1
    assert payload["read_only"] is True

    assert payload["queue_head"] == "GOLD-010"
    assert payload["queue_head_runnable"] is True
    assert payload["queue"]["pending"] == ["GOLD-010", "GOLD-011", "GOLD-012", "GOLD-013"]
    assert payload["queue"]["follow_on_task_ids"] == ["GOLD-011", "GOLD-012", "GOLD-013"]

    assert payload["follow_on_count"] == 3
    assert payload["lookahead_target"] == 3
    assert payload["deficit"] == 0
    assert payload["refill_required"] is False
    assert payload["hard_gate_tail_allowed"] is False

    assert payload["reason_codes"] == ["QUEUE_AT_TARGET", "QUEUE_HEAD_RUNNABLE"]
    assert payload["issues"] == []
    assert payload["summary"]["exit_code"] == refill.EXIT_OK
    assert set(payload) == REQUIRED_TOP_LEVEL_KEYS


def test_low_watermark_reports_one_missing_follow_on(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-010", "GOLD-011", "GOLD-012"])

    payload = build_request(refill_fs)

    assert payload["follow_on_count"] == 2
    assert payload["deficit"] == 1
    assert payload["refill_required"] is True
    assert "REFILL_REQUIRED" in payload["reason_codes"]
    assert "QUEUE_AT_TARGET" not in payload["reason_codes"]
    assert payload["summary"]["deficit"] == 1
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED


def test_head_only_queue_reports_full_deficit(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-020"])

    payload = build_request(refill_fs)

    assert payload["queue_head"] == "GOLD-020"
    assert payload["queue"]["follow_on_task_ids"] == []
    assert payload["follow_on_count"] == 0
    assert payload["deficit"] == refill.LOOKAHEAD_TARGET == 3
    assert payload["refill_required"] is True
    assert payload["reason_codes"] == ["QUEUE_HEAD_RUNNABLE", "REFILL_REQUIRED"]
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED


def test_empty_queue_has_no_head_and_reports_full_deficit(refill_fs: RefillFS) -> None:
    write_state(refill_fs, queue_state([], queue_status=None, status="ACTIVE"))

    payload = build_request(refill_fs)

    assert payload["queue_head"] is None
    assert payload["queue_head_runnable"] is False
    assert payload["queue"]["pending"] == []
    assert payload["follow_on_count"] == 0
    assert payload["deficit"] == 3
    assert payload["refill_required"] is True
    assert payload["reason_codes"] == ["QUEUE_EMPTY", "REFILL_REQUIRED"]
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED


def test_no_queue_head_token_is_stable_across_all_entry_points(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GOLD-040：无 head 的机器可读表示只有一个口径（``-``），绝不 ``head=None`` 漂移。"""

    assert refill.QUEUE_HEAD_ABSENT == "-"
    assert refill.queue_head_token(None) == "-"
    assert refill.queue_head_token("") == "-"
    assert refill.queue_head_token("   ") == "-"
    assert refill.queue_head_token(0) == "-"
    assert refill.queue_head_token("  GOLD-260  ") == "GOLD-260"

    # Orchestrator 侧与只读模块必须同源（同一个常量、同一个函数结果）
    assert orch.QUEUE_HEAD_ABSENT == refill.QUEUE_HEAD_ABSENT
    assert orch.queue_head_token(None) == refill.QUEUE_HEAD_ABSENT

    empty_payload: dict[str, Any] = {
        "queue_head": None,
        "follow_on_count": 0,
        "lookahead_target": 3,
        "deficit": 3,
        "refill_required": True,
        "hard_gate_tail_allowed": False,
        "reason_codes": ["QUEUE_EMPTY", "REFILL_REQUIRED"],
        "facts_digest": "0" * 64,
    }

    line = orch.planner_refill_hint_line(empty_payload, context=orch.REFILL_HINT_CONTEXT_IDLE)

    assert " head=- " in line
    assert "head=None" not in line
    assert "|head=-" in orch.planner_refill_hint_signature(empty_payload)

    # GOLD-034 CLI 的 stderr 同一口径：空队列 ⇒ `head=-`
    write_state(refill_fs, queue_state([], queue_status=None, status="ACTIVE"))

    exit_code = refill.main(cli_args(refill_fs))

    captured = capsys.readouterr()

    assert exit_code == refill.EXIT_REFILL_REQUIRED
    assert "[refill] head=- follow_on=0/3 deficit=3" in captured.err
    assert "head=None" not in captured.err


def test_queue_head_follows_deterministic_task_order(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-101", "GOLD-009", "GOLD-010"])

    payload = build_request(refill_fs)

    assert payload["queue"]["pending"] == ["GOLD-009", "GOLD-010", "GOLD-101"]
    assert payload["queue_head"] == "GOLD-009"
    assert payload["queue"]["follow_on_task_ids"] == ["GOLD-010", "GOLD-101"]


# ============================================================
# 2. Human Gate 场景（head 处 L3/L4、仅剩 human-only hard gate）
# ============================================================


def test_blocking_gate_at_head_is_reported_without_task_content(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-030", "GOLD-031", "GOLD-032"],
        extras={"GOLD-030": {"human_gate": "L3"}},
    )

    payload = build_request(refill_fs)

    assert payload["queue_head"] == "GOLD-030"
    assert payload["queue_head_runnable"] is False
    assert payload["queue"]["blocking_gate_at_head"] == "L3"
    assert payload["human_gates"]["queue_head_blocking_gate"] == "L3"
    assert payload["human_gates"]["blocking_levels"] == ["L3", "L4"]
    assert payload["human_gates"]["executor_may_cross_human_gate"] is False
    assert payload["human_gates"]["pending_task_gates"][0]["human_gate"] == "L3"
    assert payload["human_gates"]["blocking_task_ids"] == ["GOLD-030"]

    # 后面仍有可运行任务 ⇒ 不是 human-only hard gate（不得被误判成「无合法工作」）
    assert payload["hard_gate_tail_allowed"] is False
    assert payload["human_gates"]["human_only_hard_gate"] is False

    assert "BLOCKING_HUMAN_GATE_AT_HEAD" in payload["reason_codes"]
    assert "QUEUE_HEAD_NOT_RUNNABLE" in payload["reason_codes"]
    assert "QUEUE_TASKS_WAITING_ON_HUMAN_GATE" in payload["reason_codes"]
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED


@pytest.mark.parametrize("gate", ["L3", "L4"])
def test_human_only_hard_gate_allows_stopping_at_gated_tail(
    refill_fs: RefillFS,
    gate: str,
) -> None:
    seed_queue(refill_fs, ["GOLD-040"], extras={"GOLD-040": {"human_gate": gate}})

    payload = build_request(refill_fs)

    assert payload["queue_head"] == "GOLD-040"
    assert payload["hard_gate_tail_allowed"] is True
    assert payload["human_gates"]["human_only_hard_gate"] is True
    assert "HUMAN_ONLY_HARD_GATE" in payload["reason_codes"]
    # 计数事实依然保留：GPT 能看到缺几个，但可以合法停在 gated tail
    assert payload["deficit"] == 3
    assert payload["refill_required"] is True
    assert "REFILL_REQUIRED" in payload["reason_codes"]


@pytest.mark.parametrize(
    "task_extra",
    [
        {"auto_start": False},
        {"requires_human_approval": True},
        {"human_gate": "L4", "auto_start": True},
    ],
)
def test_human_only_hard_gate_covers_every_human_dependency(
    refill_fs: RefillFS,
    task_extra: dict[str, Any],
) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-050", "GOLD-051"],
        extras={"GOLD-050": task_extra, "GOLD-051": {"auto_start": False}},
    )

    payload = build_request(refill_fs)

    assert payload["hard_gate_tail_allowed"] is True
    assert "HUMAN_ONLY_HARD_GATE" in payload["reason_codes"]


def test_partially_runnable_queue_is_not_a_hard_gate(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-060", "GOLD-061"],
        extras={"GOLD-060": {"auto_start": False}},
    )

    payload = build_request(refill_fs)

    assert payload["hard_gate_tail_allowed"] is False
    assert "HUMAN_ONLY_HARD_GATE" not in payload["reason_codes"]
    assert "QUEUE_HEAD_NOT_RUNNABLE" in payload["reason_codes"]


# ============================================================
# 3. state / result 漂移与 Review 事实
# ============================================================


def test_state_result_drift_is_reported_without_repair(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-007"])
    write_result(refill_fs.results, "GOLD-005", "completed")
    write_result(refill_fs.results, "GOLD-006", "completed")
    write_state(
        refill_fs,
        queue_state(
            ["GOLD-007"],
            current_task="GOLD-005",
            last_completed_task="GOLD-005",
            last_reviewed_task="GOLD-005",
        ),
    )

    payload = build_request(refill_fs)

    drift = payload["state_result_drift"]

    assert drift["detected"] is True
    assert drift["codes"] == ["PROJECT_STATE_POINTER_BEHIND_RESULTS"]
    assert drift["latest_terminal_result"] == "GOLD-006"
    assert drift["current_task_pointer"] == "GOLD-005"
    assert drift["last_completed_task_pointer"] == "GOLD-005"

    assert "STATE_RESULT_DRIFT" in payload["reason_codes"]
    assert "PLANNER_SNAPSHOT_DRIFT" in payload["reason_codes"]
    assert payload["summary"]["drift_detected"] is True
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED

    assert payload["latest_completed"]["task_id"] == "GOLD-006"
    assert payload["latest_completed"]["project_state_last_completed_task"] == "GOLD-005"
    assert payload["latest_completed"]["pointer_matches_latest_completed"] is False


def test_consistent_state_reports_no_drift(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-007"])
    write_result(refill_fs.results, "GOLD-005", "completed")
    write_result(refill_fs.results, "GOLD-006", "completed")
    write_state(
        refill_fs,
        queue_state(
            ["GOLD-007"],
            last_completed_task="GOLD-006",
            last_reviewed_task="GOLD-006",
        ),
    )

    payload = build_request(refill_fs)

    assert payload["state_result_drift"]["detected"] is False
    assert "STATE_RESULT_DRIFT" not in payload["reason_codes"]
    assert payload["latest_completed"]["pointer_matches_latest_completed"] is True
    assert payload["completed_but_unreviewed"]["count"] == 0


def test_completed_but_unreviewed_lists_results_after_review_pointer(
    refill_fs: RefillFS,
) -> None:
    seed_queue(refill_fs, ["GOLD-007"])
    for task_id in ("GOLD-001", "GOLD-002", "GOLD-003"):
        write_result(refill_fs.results, task_id, "completed")
    write_state(
        refill_fs,
        queue_state(
            ["GOLD-007"],
            last_completed_task="GOLD-003",
            last_reviewed_task="GOLD-001",
        ),
    )

    payload = build_request(refill_fs)

    unreviewed = payload["completed_but_unreviewed"]

    assert unreviewed["task_ids"] == ["GOLD-002", "GOLD-003"]
    assert unreviewed["count"] == 2
    assert unreviewed["latest"] == "GOLD-003"
    assert unreviewed["last_reviewed_pointer"] == "GOLD-001"
    assert unreviewed["review_authority"] == "gpt_only"
    assert "COMPLETED_BUT_UNREVIEWED" in payload["reason_codes"]
    assert payload["summary"]["completed_but_unreviewed_count"] == 2


def test_missing_review_pointer_marks_every_completed_task_unreviewed(
    refill_fs: RefillFS,
) -> None:
    seed_queue(refill_fs, ["GOLD-007"])
    write_result(refill_fs.results, "GOLD-001", "completed")
    write_result(refill_fs.results, "GOLD-002", "blocked")
    write_state(refill_fs, queue_state(["GOLD-007"], last_completed_task="GOLD-001"))

    payload = build_request(refill_fs)

    unreviewed = payload["completed_but_unreviewed"]

    assert unreviewed["task_ids"] == ["GOLD-001"]
    assert unreviewed["reviewed_pointer_missing"] is True
    assert payload["latest_completed"]["latest_terminal_status"] == "blocked"
    assert payload["latest_completed"]["task_id"] == "GOLD-001"


def test_blockers_state_status_and_human_gate_codes_are_mirrored(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-070"],
        state=queue_state(
            ["GOLD-070"],
            status="BLOCKED",
            blockers=[
                {"code": "PHASE3_3_DATA", "detail": "真实证据不足", "retryable": False},
                {"code": "VALIDATION_PYTHON_ENV", "detail": "历史", "retryable": False},
            ],
            human_gates=[
                {"code": "PHASE3_3_L3_DECISION", "detail": "需人工 L3"},
                {"code": "PHASE3_3_AUTHORIZED_EVIDENCE", "detail": "需业务方材料"},
            ],
        ),
    )

    payload = build_request(refill_fs)

    assert payload["blockers"] == [
        {"code": "PHASE3_3_DATA", "detail": "真实证据不足", "retryable": False},
        {"code": "VALIDATION_PYTHON_ENV", "detail": "历史", "retryable": False},
    ]
    assert payload["summary"]["blocker_count"] == 2
    assert "ACTIVE_BLOCKERS_PRESENT" in payload["reason_codes"]

    assert payload["human_gates"]["state_codes"] == [
        "PHASE3_3_AUTHORIZED_EVIDENCE",
        "PHASE3_3_L3_DECISION",
    ]
    assert payload["human_gates"]["phase_transition_requires_human_gate"] is True


def test_safety_invariants_present_and_missing_is_reported(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-080"])

    payload = build_request(refill_fs)

    safety = payload["safety_invariants"]

    assert safety["required"] == ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"]
    assert safety["present"] == ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"]
    assert safety["missing"] == []
    assert safety["all_present"] is True
    assert safety["live_trading_disabled"] is True
    assert safety["external_order_submission_disabled"] is True

    seed_queue(
        refill_fs,
        ["GOLD-081"],
        state=queue_state(["GOLD-081"], invariants=["LIVE_TRADING=false"]),
    )

    degraded = build_request(refill_fs)

    assert degraded["safety_invariants"]["missing"] == ["ALLOW_EXTERNAL_ORDER_SUBMISSION=false"]
    assert degraded["safety_invariants"]["all_present"] is False
    assert degraded["safety_invariants"]["external_order_submission_disabled"] is False
    assert "SAFETY_INVARIANT_MISSING" in degraded["reason_codes"]
    assert "SAFETY_INVARIANT_MISSING" in degraded["snapshot_ref"]["issue_codes"]


def test_phase_facts_never_allow_executor_transition(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-090"])

    payload = build_request(refill_fs)

    phase = payload["phase"]

    assert phase["current"] == "Phase 3"
    assert phase["status"] == "ACTIVE"
    assert phase["transition_allowed"] is False
    assert phase["transition_requires_human_gate"] is True
    assert phase["executor_may_transition_phase"] is False


# ============================================================
# 4. 前瞻目标（默认 3 / PROJECT_STATE / 显式 / 非法回落）
# ============================================================


def test_lookahead_target_defaults_to_three_when_state_has_no_override(
    refill_fs: RefillFS,
) -> None:
    seed_queue(refill_fs, ["GOLD-100", "GOLD-101", "GOLD-102", "GOLD-103"])

    payload = build_request(refill_fs)

    assert payload["lookahead_target"] == refill.LOOKAHEAD_TARGET == 3
    assert payload["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_DEFAULT
    assert payload["follow_on_count"] == 3
    assert payload["deficit"] == 0


def test_lookahead_target_can_come_from_project_state(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-100", "GOLD-101", "GOLD-102", "GOLD-103"],
        state=queue_state(
            ["GOLD-100", "GOLD-101", "GOLD-102", "GOLD-103"],
            planner_lookahead_size=2,
        ),
    )

    payload = build_request(refill_fs)

    assert payload["lookahead_target"] == 2
    assert payload["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_PROJECT_STATE
    assert payload["follow_on_count"] == 3
    assert payload["deficit"] == 0
    assert "REFILL_REQUIRED" not in payload["reason_codes"]


@pytest.mark.parametrize("invalid", [0, -3, True, False, "3", 2.5, [], {}])
def test_invalid_lookahead_target_falls_back_to_three(
    refill_fs: RefillFS,
    invalid: object,
) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-110", "GOLD-111", "GOLD-112", "GOLD-113"],
        state=queue_state(
            ["GOLD-110", "GOLD-111", "GOLD-112", "GOLD-113"],
            planner_lookahead_size=invalid,
        ),
    )

    payload = build_request(refill_fs)

    assert payload["lookahead_target"] == 3
    assert payload["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_DEFAULTED_INVALID
    assert payload["deficit"] == 0
    assert payload["refill_required"] is False
    assert "LOOKAHEAD_TARGET_INVALID_DEFAULTED" in payload["reason_codes"]
    assert [issue["code"] for issue in payload["issues"]] == ["LOOKAHEAD_TARGET_INVALID"]
    # warning 级回落不改变退出码
    assert payload["summary"]["exit_code"] == refill.EXIT_OK


def test_explicit_lookahead_target_changes_deficit_only(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-120", "GOLD-121", "GOLD-122", "GOLD-123"])

    at_one = build_request(refill_fs, lookahead_target=1)

    assert at_one["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_EXPLICIT
    assert at_one["deficit"] == 0
    assert at_one["refill_required"] is False

    at_five = build_request(refill_fs, lookahead_target=5)

    assert at_five["deficit"] == 2
    assert at_five["refill_required"] is True

    invalid = build_request(refill_fs, lookahead_target=0)

    assert invalid["lookahead_target"] == 3
    assert invalid["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_DEFAULTED_INVALID


# ============================================================
# 5. 确定性（wall-clock 解耦 / 可复算 / 对事实敏感）
# ============================================================


def recompute_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        refill.request_facts(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def test_facts_digest_is_wall_clock_independent_and_recomputable(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-130", "GOLD-131", "GOLD-132"])

    first = build_request(refill_fs, generated_at=AUDIT_TIME)

    second = build_request(refill_fs, generated_at=AUDIT_TIME_LATER)

    assert first["generated_at"] == AUDIT_TIME
    assert second["generated_at"] == AUDIT_TIME_LATER

    assert first["facts_digest"] == second["facts_digest"]
    assert first["facts_digest"] == recompute_digest(first)

    assert first["determinism"]["wall_clock_in_facts"] is False
    assert first["determinism"]["eligibility_uses_wall_clock"] is False
    assert first["determinism"]["digest_field"] == "facts_digest"
    assert list(first["determinism"]["excluded_from_facts"]) == list(refill.FACTS_EXCLUDED_KEYS)
    assert set(refill.FACTS_EXCLUDED_KEYS) <= set(first)

    # 同一 generated_at 下 render 必须字节级一致
    same_time = build_request(refill_fs, generated_at=AUDIT_TIME)

    assert refill.render_planner_refill_request(first) == refill.render_planner_refill_request(
        same_time
    )


def test_facts_digest_reacts_to_queue_changes(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-140", "GOLD-141", "GOLD-142"])

    before = build_request(refill_fs)

    write_task(refill_fs.tasks, "GOLD-143")
    write_state(refill_fs, queue_state(["GOLD-140", "GOLD-141", "GOLD-142", "GOLD-143"]))

    after = build_request(refill_fs)

    assert before["deficit"] == 1
    assert after["deficit"] == 0
    assert before["facts_digest"] != after["facts_digest"]
    assert before["refill_required"] is True
    assert after["refill_required"] is False


def test_build_is_read_only_and_repeatable(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-150", "GOLD-151"],
        extras={"GOLD-150": {"human_gate": "L3"}},
        state=queue_state(
            ["GOLD-150", "GOLD-151"],
            status="BLOCKED",
            blockers=[{"code": "PHASE3_3_DATA", "detail": "x", "retryable": False}],
        ),
    )

    before = tree_digest(refill_fs.root)

    first = build_request(refill_fs)
    second = build_request(refill_fs)

    assert first == second
    assert tree_digest(refill_fs.root) == before


# ============================================================
# 6. 职责边界：无规划权、无写状态 API、不产出任务内容
# ============================================================


def test_payload_never_contains_task_content_or_decision_keys(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-160", "GOLD-161"])

    payload = build_request(refill_fs)

    keys = collect_keys(payload)

    for forbidden in refill.FORBIDDEN_REQUEST_KEYS:
        assert forbidden not in keys, forbidden

    authority = payload["planner_authority"]

    assert authority["planning_authority"] == "gpt_only"
    assert authority["read_only"] is True
    assert authority["request_only"] is True
    assert authority["next_task_content_included"] is False
    assert "refill_rolling_queue" in authority["executor_forbidden_capabilities"]

    for key, value in authority.items():
        if key.startswith(("executor_can_", "tool_can_")):
            assert value is False, key

    # 只暴露既存队列的 id + 事实，不暴露任何标题 / 内容 / 建议
    assert "follow_on_task_ids" in payload["queue"]
    assert "title" not in keys
    assert "suggestion" not in keys


def test_module_exposes_no_planning_or_write_api() -> None:
    public = {name for name in dir(refill) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    assert "build_planner_refill_request" in public
    assert "render_planner_refill_request" in public
    assert "refill_exit_code" in public

    for snippet in FORBIDDEN_SOURCE_SNIPPETS:
        assert snippet not in MODULE_SOURCE, snippet

    # 唯一允许出现的写调用是受控输出模块（runtime / 临时路径守卫）
    assert "snapshot_output.write_snapshot_output" in MODULE_SOURCE


def test_constants_match_planner_contract() -> None:
    assert refill.LOOKAHEAD_TARGET == 3
    assert refill.PLANNER_REFILL_REQUEST_SCHEMA_VERSION == 1
    assert refill.REQUIRED_LIVE_TRADING_INVARIANT in planner.REQUIRED_SAFETY_INVARIANTS
    assert refill.REQUIRED_EXTERNAL_ORDER_INVARIANT in planner.REQUIRED_SAFETY_INVARIANTS

    assert len(set(refill.REASON_CODES)) == len(refill.REASON_CODES)

    lookup_sources = {
        refill.LOOKAHEAD_SOURCE_PROJECT_STATE,
        refill.LOOKAHEAD_SOURCE_DEFAULT,
        refill.LOOKAHEAD_SOURCE_DEFAULTED_INVALID,
        refill.LOOKAHEAD_SOURCE_EXPLICIT,
    }

    assert len(lookup_sources) == 4

    for code in refill.REASON_CODES:
        assert code and code == code.upper()

    assert set(refill.DRIFT_ISSUE_CODES) <= {
        planner.ISSUE_POINTER_BEHIND_RESULTS,
        planner.ISSUE_POINTER_AHEAD_OF_RESULTS,
        planner.ISSUE_POINTER_MISSING,
        planner.ISSUE_POINTER_UNKNOWN_TASK,
        planner.ISSUE_QUEUE_DECLARATION_MISMATCH,
        planner.ISSUE_QUEUE_TASK_MISSING,
    }

    assert planner.role_contract()["executor_can_refill_rolling_queue"] is False

    authority = refill.authority_section()

    assert authority["executor_can_refill"] is False
    assert authority["schema"] == refill.PLANNER_REFILL_AUTHORITY_SCHEMA
    assert authority["blocking_human_gates"] == sorted(orch.BLOCKING_HUMAN_GATES)
    assert authority["phase_transition_requires_human_gate"] is True


def test_reason_codes_stay_within_declared_vocabulary(refill_fs: RefillFS) -> None:
    seed_queue(
        refill_fs,
        ["GOLD-170"],
        extras={"GOLD-170": {"human_gate": "L4"}},
        state=queue_state(
            ["GOLD-170"],
            status="BLOCKED",
            blockers=[{"code": "PHASE3_3_DATA", "detail": "x", "retryable": False}],
        ),
    )

    payload = build_request(refill_fs)

    assert payload["reason_codes"]
    assert set(payload["reason_codes"]) <= set(refill.REASON_CODES)
    assert payload["reason_codes"] == sorted(set(payload["reason_codes"]))


# ============================================================
# 7. 只读 CLI（纯 ASCII JSON / 退出码 / 受控 --output）
# ============================================================


def cli_args(fs: RefillFS) -> list[str]:
    return [
        "--root",
        str(fs.root),
        "--state",
        str(fs.state_path),
        "--tasks-dir",
        str(fs.tasks),
        "--results-dir",
        str(fs.results),
        "--generated-at",
        AUDIT_TIME,
    ]


def test_cli_option_contract_excludes_planning_flags() -> None:
    parser = refill.build_parser()

    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert option_strings == {
        "-h",
        "--help",
        "--root",
        "--state",
        "--tasks-dir",
        "--results-dir",
        "--generated-at",
        "--lookahead-target",
        "--output",
    }

    for forbidden in (
        "--plan",
        "--next-task",
        "--create-task",
        "--write-task",
        "--write-result",
        "--update-state",
        "--refill",
        "--approve",
    ):
        assert forbidden not in option_strings


def test_cli_help_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.planner_refill_request", "--help"],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode == 0
    assert b"--output" in completed.stdout
    assert b"--lookahead-target" in completed.stdout

    for forbidden in (b"--write-task", b"--create-task", b"--approve", b"--no-dry-run"):
        assert forbidden not in completed.stdout


def test_cli_prints_ascii_json_and_returns_zero(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-180", "GOLD-181", "GOLD-182", "GOLD-183"])

    exit_code = refill.main(cli_args(refill_fs))

    captured = capsys.readouterr()

    assert exit_code == refill.EXIT_OK
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["deficit"] == 0
    assert payload["summary"]["exit_code"] == refill.EXIT_OK
    assert "[refill] head=GOLD-180 follow_on=3/3 deficit=0 refill_required=False" in captured.err


def test_cli_returns_exit_code_2_when_refill_required(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-190", "GOLD-191"])

    exit_code = refill.main(cli_args(refill_fs))

    captured = capsys.readouterr()

    assert exit_code == refill.EXIT_REFILL_REQUIRED

    payload = json.loads(captured.out)

    assert payload["deficit"] == 2
    assert payload["refill_required"] is True
    assert "REFILL_REQUIRED" in captured.err


def test_cli_reports_unreadable_state_with_exit_code_3(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-200", "GOLD-201"])

    refill_fs.state_path.unlink()

    exit_code = refill.main(cli_args(refill_fs))

    captured = capsys.readouterr()

    assert exit_code == refill.EXIT_STATE_UNREADABLE
    assert "PROJECT_STATE_UNREADABLE" in captured.err

    payload = json.loads(captured.out)

    assert payload["summary"]["exit_code"] == refill.EXIT_STATE_UNREADABLE
    assert payload["blockers"] == []


def test_cli_writes_output_to_runtime_path_and_keeps_stdout_empty(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-210", "GOLD-211"])

    target = refill_fs.root / ".ai" / "runtime" / "refill.json"

    exit_code = refill.main([*cli_args(refill_fs), "--output", str(target)])

    captured = capsys.readouterr()

    assert exit_code == refill.EXIT_REFILL_REQUIRED
    assert captured.out == ""

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["queue_head"] == "GOLD-210"
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED

    # 只新增这一个受控文件；tasks / results / state 逐字节不变
    assert sorted(path.name for path in target.parent.iterdir()) == ["refill.json"]


@pytest.mark.parametrize(
    "relative",
    [
        ".ai/tasks/GOLD-999.json",
        ".ai/results/GOLD-999.json",
        ".ai/PROJECT_STATE.json",
        "PROGRESS_LOG.md",
        "pyproject.toml",
        ".clinerules",
    ],
)
def test_cli_output_rejects_forbidden_targets_with_exit_code_4(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
    relative: str,
) -> None:
    seed_queue(refill_fs, ["GOLD-220", "GOLD-221"])

    before = tree_digest(refill_fs.root)

    exit_code = refill.main(
        [*cli_args(refill_fs), "--output", str(refill_fs.root / relative)]
    )

    captured = capsys.readouterr()

    assert exit_code == snapshot_output.EXIT_OUTPUT_REJECTED == 4
    assert captured.out == ""
    assert snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err
    assert tree_digest(refill_fs.root) == before


def test_cli_output_prepares_runtime_parent_directory(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GOLD-040：已批准 runtime 路径的缺失父目录被确定性准备（干净 CI 也能镜像）。"""

    seed_queue(refill_fs, ["GOLD-230"])

    target = refill_fs.root / ".ai" / "runtime" / "missing-dir" / "refill.json"

    assert not target.parent.is_dir()

    exit_code = refill.main([*cli_args(refill_fs), "--output", str(target)])

    captured = capsys.readouterr()

    assert exit_code in {refill.EXIT_OK, refill.EXIT_REFILL_REQUIRED}
    assert "[info] refill request 已写入受控路径" in captured.err
    assert target.is_file()

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["read_only"] is True
    assert payload["queue_head"] == "GOLD-230"

    # tasks / results / state 依旧零写入，且绝不创建 runtime 之外的目录
    assert not (refill_fs.root / ".ai" / "tasks" / "missing-dir").exists()
    assert not (refill_fs.root / ".ai" / "missing-dir").exists()


def test_cli_output_rejects_path_outside_runtime_without_parent_directory(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """目录准备**只**对 `<root>/.ai/runtime/**` 生效：其它位置依旧 fail-closed。"""

    seed_queue(refill_fs, ["GOLD-231"])

    before = tree_digest(refill_fs.root)

    target = refill_fs.root / ".ai" / "logs" / "missing-dir" / "refill.json"

    exit_code = refill.main([*cli_args(refill_fs), "--output", str(target)])

    captured = capsys.readouterr()

    assert exit_code == snapshot_output.EXIT_OUTPUT_REJECTED
    assert not target.exists()
    assert not target.parent.exists()
    assert snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err
    assert tree_digest(refill_fs.root) == before


def test_cli_default_run_writes_nothing(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-240", "GOLD-241"])

    before = tree_digest(refill_fs.root)

    refill.main(cli_args(refill_fs))

    capsys.readouterr()

    assert tree_digest(refill_fs.root) == before


def test_cli_lookahead_target_flag_never_lowers_below_one(
    refill_fs: RefillFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-250", "GOLD-251", "GOLD-252", "GOLD-253"])

    exit_code = refill.main([*cli_args(refill_fs), "--lookahead-target", "0"])

    captured = capsys.readouterr()

    payload = json.loads(captured.out)

    assert payload["lookahead_target"] == 3
    assert payload["lookahead_target_source"] == refill.LOOKAHEAD_SOURCE_DEFAULTED_INVALID
    assert payload["refill_required"] is False
    assert exit_code == refill.EXIT_OK


def test_cli_output_accepts_system_temp_directory(
    refill_fs: RefillFS,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_queue(refill_fs, ["GOLD-270"])

    temp_root = tmp_path / "fake-temp"
    temp_root.mkdir()

    monkeypatch.setattr(snapshot_output, "temp_directory", lambda: temp_root)

    target = temp_root / "refill.json"

    exit_code = refill.main([*cli_args(refill_fs), "--output", str(target)])

    capsys.readouterr()

    assert exit_code == refill.EXIT_REFILL_REQUIRED
    assert json.loads(target.read_text(encoding="utf-8"))["queue_head"] == "GOLD-270"


def test_cli_subprocess_is_byte_stable_and_ascii(refill_fs: RefillFS) -> None:
    seed_queue(refill_fs, ["GOLD-260", "GOLD-261"])

    before = tree_digest(refill_fs.root)

    command = [
        sys.executable,
        "-m",
        "orchestrator.planner_refill_request",
        *cli_args(refill_fs),
    ]

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == refill.EXIT_REFILL_REQUIRED
    assert first.returncode == second.returncode
    assert first.stdout == second.stdout
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["queue_head"] == "GOLD-260"
    assert payload["follow_on_count"] == 1
    assert payload["deficit"] == 2
    assert payload["summary"]["exit_code"] == refill.EXIT_REFILL_REQUIRED

    assert tree_digest(refill_fs.root) == before
