"""真实仓库上的 GPT Review Backlog 回归（GOLD-037）。

为什么放在 integration
----------------------
- 只有真实 Git 仓库才能证明 backlog 里的 result SHA-256 / 完成 commit identity 真的来自
  ``orchestrator.review_binding`` 的**只读 Git 事实**：测试自己用 ``git`` 独立复算
  （``git log`` 解析终态 commit、``git cat-file`` 复算 result 字节 SHA-256），而不是信任
  被测模块；
- 同时证明命令默认**零写入**：工作树状态、``.ai/tasks``、``.ai/results``、``PROJECT_STATE``
  与 ``GPT_REVIEW_LEDGER`` 在命令前后字节完全不变。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout。
"""

from __future__ import annotations

import functools
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_snapshot as planner
from orchestrator import review_backlog as backlog
from orchestrator import review_ledger as ledger_mod

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

# GOLD-039：历史 result（GOLD-020 之前的「唯一 finish_reason」格式）的稳定矛盾 code。
# 测试**自己**复算这个结论，绝不 import 被测模块的判定。
LEGACY_CONTRADICTION_CODE = "RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS"

NORMALIZED_MARKER_KEYS = (
    "execution_outcome",
    "normalized_finish_reason",
    "cline_finish_reason_raw",
)


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, check=False
    )

    assert completed.returncode == 0, (args, completed.stderr)

    return completed.stdout.decode("utf-8", errors="replace")


def git_blob_bytes(revision: str) -> bytes:
    completed = subprocess.run(
        ["git", "cat-file", "blob", revision],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

    return completed.stdout


def worktree_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace")


def head_reachable_terminal(task_id: str) -> str:
    """测试**自己**用 git 解析该 task 的终态 commit（独立于被测模块）。"""

    wanted = {f"ai: complete {task_id}", f"ai: blocked {task_id}"}

    matches = [sha for sha, subject in head_log_rows() if subject in wanted]

    assert len(matches) == 1, (task_id, matches)

    return matches[0]


@functools.lru_cache(maxsize=1)
def head_log_rows() -> tuple[tuple[str, str], ...]:
    """HEAD 可达历史的 ``(sha, subject)``（缓存避免重复 ``git log``）。"""

    rows: list[tuple[str, str]] = []

    for line in git("log", "--format=%H%x1f%s").splitlines():
        if not line.strip():
            continue

        sha, subject = line.split("\x1f", 1)

        rows.append((sha, subject))

    return tuple(rows)


def project_state() -> dict[str, Any]:
    payload = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)

    return payload


def independent_legacy_contradiction(payload: dict[str, Any]) -> bool:
    """测试自己按 §2.2 / GOLD-039 口径复算「历史 result 终态矛盾」。

    规则（只覆盖本仓库真实存在的历史形状，故意不复用被测模块）：

    - 顶层与最终 attempt 都没有 GOLD-020 归一化字段（即历史「唯一 finish_reason」格式）；
    - ``status=completed``；
    - 那个唯一的 ``finish_reason`` 既不是空值也不是 ``completed``（raw Cline 值）。
    """

    if any(key in payload for key in NORMALIZED_MARKER_KEYS):
        return False

    attempts = payload.get("attempts")

    if not isinstance(attempts, list) or not attempts:
        return False

    final = attempts[-1]

    if not isinstance(final, dict) or any(key in final for key in NORMALIZED_MARKER_KEYS):
        return False

    if str(payload.get("status", "")).strip().lower() != "completed":
        return False

    raw = final.get("finish_reason")

    return isinstance(raw, str) and raw.strip() not in ("", "completed")


def expected_backlog_ids() -> list[str]:
    """测试**自己**复算「last_reviewed_task 之后所有 completed result」。"""

    state = project_state()

    statuses: dict[str, str] = {}

    for path in sorted(RESULTS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))

        statuses[path.stem] = str(payload.get("status", "")).strip().lower()

    completed = ledger_mod.completed_result_ids(statuses, planner.project_task_prefix(state))

    pointer = planner.pointer_value(state, "last_reviewed_task")

    if pointer is None:
        return []

    return [task_id for task_id in completed if planner.is_newer(task_id, pointer)]


def build_manifest() -> dict[str, Any]:
    """缓存的 manifest（同一测试会话内仓库事实不变，避免重复 Git 往返）。"""

    return _cached_manifest()


@functools.lru_cache(maxsize=1)
def _cached_manifest() -> dict[str, Any]:
    return fresh_manifest()


def fresh_manifest() -> dict[str, Any]:
    """强制重新只读构建（用于「命令前后零改写」这类必须真正执行一次的场景）。"""

    return backlog.build_review_backlog_manifest(root=REPO_ROOT, generated_at=AUDIT_TIME)


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


def collect_string_values(payload: object) -> set[str]:
    found: set[str] = set()

    if isinstance(payload, dict):
        for value in payload.values():
            found |= collect_string_values(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_string_values(item)

    elif isinstance(payload, str):
        found.add(payload)

    return found


# ============================================================
# 1. backlog 范围与逐项客观事实
# ============================================================


def test_backlog_covers_every_completed_result_after_last_reviewed() -> None:
    payload = build_manifest()

    assert payload["schema"] == backlog.REVIEW_BACKLOG_SCHEMA
    assert payload["schema_version"] == backlog.REVIEW_BACKLOG_SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["generated_at"] == AUDIT_TIME

    state = project_state()

    assert payload["coverage"]["last_reviewed_task_pointer"] == state["last_reviewed_task"]

    ids = [item["task_id"] for item in payload["backlog"]]

    assert ids == expected_backlog_ids()
    assert payload["coverage"]["backlog_count"] == len(ids)

    for item in payload["backlog"]:
        assert item["review_status"] in backlog.REVIEW_STATUSES
        assert isinstance(item["facts_complete"], bool)
        assert isinstance(item["missing_reason_codes"], list)
        assert isinstance(item["reason_codes"], list)
        assert item["binding_source"] == "gold-ai/review-binding-manifest/v1"


def test_backlog_item_identity_is_independently_recomputable_with_git() -> None:
    payload = build_manifest()

    items = payload["backlog"]

    if not items:
        pytest.skip("当前仓库没有 formal review backlog（last_reviewed_task 已覆盖全部终态）")

    for item in items:
        task_id = str(item["task_id"])

        commit = item["commit"]

        assert commit["resolved"] is True
        assert commit["subject"] in {f"ai: complete {task_id}", f"ai: blocked {task_id}"}

        # 终态 commit identity 必须与测试自己解析的 HEAD 可达历史一致
        assert commit["sha"] == head_reachable_terminal(task_id)

        # result canonical 内容身份必须可由 git blob 独立复算
        blob = git_blob_bytes(f"{commit['sha']}:.ai/results/{task_id}.json")

        assert item["result"]["sha256"] == hashlib.sha256(blob).hexdigest()
        assert item["result"]["status"] == "completed"

        # GOLD-039：测试自己按协议口径复算「历史终态矛盾」，
        # 不信任被测模块的结论。
        contradicted = independent_legacy_contradiction(
            json.loads((RESULTS_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        )

        assert item["facts_complete"] is (not contradicted)

        if contradicted:
            assert item["missing_reason_codes"] == [LEGACY_CONTRADICTION_CODE]
        else:
            assert item["missing_reason_codes"] == []


def test_backlog_marks_review_status_objectively() -> None:
    payload = build_manifest()

    ledger = json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))

    bound_ids = {
        str(entry["task_id"])
        for entry in ledger["entries"]
        if isinstance(entry, dict) and entry.get("task_id") is not None
    }

    for item in payload["backlog"]:
        task_id = str(item["task_id"])

        assert item["ledger"]["entry_present"] is (task_id in bound_ids)

        if item["ledger"]["entry_present"] is False:
            # 无 ledger 条目：客观待 review（facts 齐）或客观 invalid（facts 不齐）。
            # 绝不把「事实不齐」当成可 review 的正常项。
            expected_status = (
                backlog.REVIEW_STATUS_PENDING
                if item["facts_complete"]
                else backlog.REVIEW_STATUS_INVALID
            )

            assert item["review_status"] == expected_status


# ============================================================
# 2. 只读 + 确定性 + 无 Review 结论 + 交易 / Phase 边界不变
# ============================================================


def test_backlog_build_is_read_only_on_real_repo() -> None:
    before_tasks = tree_digest(TASKS_DIR)

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    fresh_manifest()

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger

    # 工作树状态（含尚未提交的开发改动）在命令前后必须完全一致
    assert worktree_status() == before_status


def test_backlog_is_deterministic_and_cli_is_zero_side_effect() -> None:
    first = fresh_manifest()

    second = fresh_manifest()

    assert first["backlog_digest"] == second["backlog_digest"]

    assert backlog.render_review_backlog_manifest(first) == backlog.render_review_backlog_manifest(
        second
    )

    command = [
        sys.executable,
        "-m",
        "orchestrator.review_backlog",
        "--generated-at",
        AUDIT_TIME,
    ]

    before_status = worktree_status()

    before_ledger = REVIEW_LEDGER.read_bytes()

    run_first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    run_second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert run_first.stdout == run_second.stdout
    assert run_first.stdout.isascii()

    payload = json.loads(run_first.stdout.decode("ascii"))

    assert payload["backlog_digest"] == first["backlog_digest"]
    assert run_first.returncode == payload["summary"]["exit_code"]

    if payload["issues"]:
        assert run_first.returncode in (backlog.EXIT_FAIL_CLOSED, backlog.EXIT_UNAVAILABLE)
    else:
        assert run_first.returncode == backlog.EXIT_OK

    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_backlog_never_emits_review_outcome_or_write_power() -> None:
    payload = build_manifest()

    keys = collect_keys(payload)

    for forbidden in backlog.FORBIDDEN_MANIFEST_KEYS:
        assert forbidden not in keys, forbidden

    values = collect_string_values(payload)

    assert not any(value.strip().upper() in ledger_mod.REVIEW_VERDICTS for value in values)

    authority = payload["authority"]

    assert authority["review_authority"] == "gpt_only"
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_write_review_ledger"] is False
    assert authority["tool_can_advance_review_pointer"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["writes_project_state"] is False
    assert authority["writes_tasks"] is False
    assert authority["writes_results"] is False


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    build_manifest()

    state = project_state()

    assert state["phase"] == "Phase 3"
    assert state["status"] == "BLOCKED"
    assert "PHASE3_3_DATA" in [blocker["code"] for blocker in state["blockers"]]
    assert "LIVE_TRADING=false" in state["invariants"]
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]
    assert isinstance(state["last_reviewed_task"], str)

    payload = build_manifest()

    assert payload["authority"]["tool_can_cross_human_gate"] is False
    assert payload["authority"]["tool_can_qualify_data"] is False
