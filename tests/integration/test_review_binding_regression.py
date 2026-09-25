"""真实仓库上的 Review Binding Manifest 回归（GOLD-031 / GOLD-028 类已完成任务）。

为什么放在 integration
----------------------
- 只有真实 Git 仓库才能证明 **result SHA-256 与 reviewed commit identity 可独立复算**：
  测试自己用 ``git cat-file`` / ``git log`` 复算，而不是信任被测模块；
- 同时证明命令 **零网络 / 零数据库 / 零业务证据写入**：工作树、``.ai/tasks``、
  ``.ai/results``、项目状态与 Review 台账前后字节完全不变。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import review_binding as binding

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

TASK_ID = "GOLD-028"

LEDGER_TASK_ID = "GOLD-027"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

# GOLD-039：历史 result（GOLD-020 之前的「唯一 finish_reason」格式）的稳定矛盾 code。
LEGACY_CONTRADICTION_CODE = "RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS"

REVIEWED_LEDGER_TASK_IDS = ("GOLD-025", "GOLD-026", "GOLD-027")


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def git(*args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, check=False)

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


def own_terminal_commit(task_id: str) -> str:
    """测试**自己**用 git 解析该 task 的终态 commit（独立于被测模块）。"""

    wanted = {f"ai: complete {task_id}", f"ai: blocked {task_id}"}

    rows = [
        line.split("\x1f", 1)
        for line in git("log", "--format=%H%x1f%s").splitlines()
        if line.strip()
    ]

    matches = [sha for sha, subject in rows if subject in wanted]

    assert len(matches) == 1, matches

    return matches[0]


def collect_keys(payload: object) -> set[str]:
    """递归收集 key；``adjudication`` 子树是 §2.15 裁决事实，不参与结论字段断言（GOLD-044）。"""

    found: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))

            if str(key) == "adjudication":
                continue

            found |= collect_keys(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_keys(item)

    return found


def build_manifest(task_id: str = TASK_ID) -> dict[str, Any]:
    return binding.build_review_binding_manifest(task_id, root=REPO_ROOT, generated_at=AUDIT_TIME)


def result_sha256_from_git(task_id: str = TASK_ID) -> str:
    return hashlib.sha256(git_blob_bytes(f"HEAD:.ai/results/{task_id}.json")).hexdigest()


def test_manifest_reproduces_review_ledger_binding_for_reviewed_task() -> None:
    """manifest 必须能复算 GPT 已写入 Review 台账的 result_sha256 与 reviewed commit。"""

    ledger = json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))

    entries = {entry["task_id"]: entry for entry in ledger["entries"]}

    entry = entries[LEDGER_TASK_ID]

    manifest = build_manifest(LEDGER_TASK_ID)

    assert manifest["issues"] == []
    assert manifest["binding"]["facts_complete"] is True
    assert manifest["result"]["sha256"] == entry["reviewed_result"]["result_sha256"]
    assert manifest["result"]["status"] == entry["reviewed_result"]["status"]
    assert manifest["result"]["finished_at"] == entry["reviewed_result"]["finished_at"]
    assert manifest["commit"]["sha"] == entry["reviewed_commit"]["sha"]
    assert manifest["commit"]["branch"] == entry["reviewed_commit"]["branch"]


def test_gold028_result_sha256_is_independently_recomputable() -> None:
    """GOLD-028 内容身份仍可独立复算；它的历史终态矛盾必须被 fail-closed 暴露。"""

    manifest = build_manifest()

    # 内容身份不因「事实不齐」而丢失：canonical sha256 仍可由 git cat-file 复算
    assert manifest["result"]["sha256"] == result_sha256_from_git()
    assert manifest["result"]["bytes"] == len(git_blob_bytes(f"HEAD:.ai/results/{TASK_ID}.json"))

    # 工作树原始字节口径 —— 可由独立 hashlib 复算
    result_path = RESULTS_DIR / f"{TASK_ID}.json"

    assert manifest["result"]["worktree_sha256"] == hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    assert manifest["result"]["worktree_bytes"] == result_path.stat().st_size
    assert manifest["result"]["worktree_matches_commit"] is True
    assert manifest["result"]["finished_at"] == "2026-09-23T16:57:20+08:00"

    # GOLD-039：GOLD-028 与 GOLD-035 属同一类历史矛盾
    # （status=completed，但那个「唯一」finish_reason 里是 raw Cline 值 aborted）。
    assert manifest["binding"]["facts_complete"] is False
    assert manifest["binding"]["reason_codes"] == [LEGACY_CONTRADICTION_CODE]
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT



def test_completion_commit_identity_is_traceable() -> None:
    manifest = build_manifest()

    expected = own_terminal_commit(TASK_ID)

    assert manifest["commit"]["resolved"] is True
    assert manifest["commit"]["reason_code"] is None
    assert manifest["commit"]["sha"] == expected
    assert manifest["commit"]["subject"] == f"ai: complete {TASK_ID}"
    assert manifest["commit"]["branch"] == git("rev-parse", "--abbrev-ref", "HEAD").strip()
    assert manifest["commit"]["head"] == git("rev-parse", "HEAD").strip()
    assert manifest["commit"]["history_scope"].startswith("HEAD-reachable")

    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected, "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert ancestor.returncode == 0

    # 该 commit 里的 result 版本正是 manifest 绑定的 canonical 内容身份
    blob_id = git("rev-parse", f"{expected}:.ai/results/{TASK_ID}.json").strip()

    assert manifest["result"]["sha256"] == hashlib.sha256(git_blob_bytes(blob_id)).hexdigest()

    task_path = TASKS_DIR / f"{TASK_ID}.json"

    assert manifest["task"]["worktree_sha256"] == hashlib.sha256(task_path.read_bytes()).hexdigest()
    assert manifest["task"]["worktree_matches_commit"] is True
    assert manifest["task"]["metadata_digest"] is not None


def test_validation_summary_matches_recorded_result_file() -> None:
    manifest = build_manifest()

    payload = json.loads((RESULTS_DIR / f"{TASK_ID}.json").read_text(encoding="utf-8"))

    recorded = [
        validation["command"]
        for attempt in payload["attempts"]
        for validation in attempt["validations"]
    ]

    assert manifest["validation"]["commands"] == sorted(set(recorded))
    assert manifest["validation"]["status"] == "passed"
    assert manifest["validation"]["attempt_count"] == len(payload["attempts"])
    assert list(manifest["validation"]["commands"]) == sorted(recorded)
    assert manifest["result"]["attempt_count"] == len(payload["attempts"])


def test_manifest_is_read_only_on_real_repo() -> None:
    before_tasks = tree_digest(TASKS_DIR)

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    build_manifest()

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger

    # 工作树状态（含尚未提交的开发改动）在命令前后必须完全一致
    assert worktree_status() == before_status


def test_cli_is_deterministic_and_zero_side_effect() -> None:
    command = [
        sys.executable,
        "-m",
        "orchestrator.review_binding",
        "--task",
        TASK_ID,
        "--generated-at",
        AUDIT_TIME,
    ]

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    # 检出历史终态矛盾 ⇒ fail-closed（退出码 2 + stderr 明确 facts_complete=False）
    assert first.returncode == binding.EXIT_DRIFT
    assert b"facts_complete=False" in first.stderr
    assert first.stderr == second.stderr
    assert first.stdout == second.stdout
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["schema"] == binding.REVIEW_BINDING_SCHEMA
    assert payload["binding"]["facts_complete"] is False
    assert payload["binding"]["reason_codes"] == [LEGACY_CONTRADICTION_CODE]
    assert payload["result"]["sha256"] == result_sha256_from_git()
    assert payload["commit"]["sha"] == own_terminal_commit(TASK_ID)
    assert payload["generated_at"] == AUDIT_TIME

    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_manifest_is_facts_only_and_never_a_review_verdict() -> None:
    manifest = build_manifest()

    keys = collect_keys(manifest)

    for forbidden in binding.FORBIDDEN_MANIFEST_KEYS:
        assert forbidden not in keys, forbidden

    authority = manifest["authority"]

    assert authority["review_authority"] == "gpt_only"
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["tool_can_qualify_data"] is False
    assert authority["tool_can_cross_human_gate"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["writes_project_state"] is False


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    build_manifest()

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
        assert state["queue_status"] == "BLOCKED"
    assert isinstance(state["last_reviewed_task"], str)


def test_gold035_terminal_contradiction_is_exposed_fail_closed() -> None:
    """GOLD-039 核心验收：GOLD-035 的历史矛盾必须被 review tooling fail-closed 暴露。"""

    manifest = build_manifest("GOLD-035")

    assert manifest["result"]["status"] == "completed"

    # GPT 不猜、不静默归一化：事实不齐 + 稳定 reason code
    assert manifest["binding"]["facts_complete"] is False
    assert manifest["binding"]["reason_codes"] == [LEGACY_CONTRADICTION_CODE]
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT

    # 仍然只产事实：绝不出现任何 Review 结论字段
    keys = collect_keys(manifest)

    for forbidden in binding.FORBIDDEN_MANIFEST_KEYS:
        assert forbidden not in keys, forbidden

    # 历史 result 只读：manifest 构建前后字节完全不变
    result_file = RESULTS_DIR / "GOLD-035.json"

    before = hashlib.sha256(result_file.read_bytes()).hexdigest()

    build_manifest("GOLD-035")

    assert hashlib.sha256(result_file.read_bytes()).hexdigest() == before


def test_formal_ledger_reviewed_tasks_remain_facts_complete() -> None:
    """正式台账已绑定的任务保持自洽 ⇒ 新门禁不破坏既有 Review 台账链。"""

    for task_id in REVIEWED_LEDGER_TASK_IDS:
        manifest = build_manifest(task_id)

        assert manifest["issues"] == [], task_id
        assert manifest["binding"]["facts_complete"] is True, task_id
        assert manifest["summary"]["exit_code"] == binding.EXIT_OK, task_id
