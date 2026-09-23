"""真实仓库上的 Review Ledger 完整性 / 连续性回归（GOLD-032）。

为什么放在 integration
----------------------
- 只有真实 Git 仓库才能证明台账里记录的客观身份（result SHA-256、完成 commit
  identity）与 ``orchestrator.review_binding`` **独立复算**的事实一致；
- 同时证明命令 **零网络 / 零数据库 / 零业务证据写入**：工作树、``.ai/tasks``、
  ``.ai/results``、项目状态与 Review 台账前后字节完全不变。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout；
所有篡改场景都在 ``tmp_path`` 内的**台账副本**上构造。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

LEDGER_TASK_ID = "GOLD-027"


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


def git_blob_bytes(revision: str) -> bytes:
    completed = subprocess.run(
        ["git", "cat-file", "blob", revision],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

    return completed.stdout


def own_terminal_commit(task_id: str) -> str:
    """测试**自己**用 git 解析该 task 的终态 commit（独立于被测模块）。"""

    completed = subprocess.run(
        ["git", "log", "--format=%H%x1f%s"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr

    wanted = {f"ai: complete {task_id}", f"ai: blocked {task_id}"}

    matches = [
        line.split("\x1f", 1)[0]
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip() and line.split("\x1f", 1)[1] in wanted
    ]

    assert len(matches) == 1, matches

    return matches[0]


def build_report(ledger_path: Path | None = None) -> dict[str, Any]:
    return integrity.build_integrity_report(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        ledger_path=ledger_path if ledger_path is not None else REVIEW_LEDGER,
        generated_at=AUDIT_TIME,
    )


def load_ledger() -> dict[str, Any]:
    return json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))


def write_ledger_copy(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "GPT_REVIEW_LEDGER.json"

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return path


def test_real_ledger_is_integrity_ok() -> None:
    report = build_report()

    assert report["schema"] == integrity.LEDGER_INTEGRITY_SCHEMA
    assert report["schema_version"] == integrity.LEDGER_INTEGRITY_SCHEMA_VERSION
    assert report["read_only"] is True
    assert report["issues"] == []
    assert report["summary"]["integrity_ok"] is True
    assert report["summary"]["exit_code"] == integrity.EXIT_OK
    assert report["ledger"]["schema"] == ledger_mod.REVIEW_LEDGER_SCHEMA
    assert report["ledger"]["coverage_floor_source"] == "reviewed_from"
    assert report["ledger"]["duplicate_tasks"] == []
    assert report["chain"]["order_is_ascending"] is True
    assert report["chain"]["reviewed_at_is_ascending"] is True
    assert report["chain"]["missing_in_window"] == []

    entries = load_ledger()["entries"]

    assert report["summary"]["entry_count"] == len(entries)
    assert report["summary"]["bound_count"] == len(entries)

    for view in report["bindings"]:
        assert view["bound"] is True
        assert view["manifest_facts_complete"] is True
        assert all(value is True for value in view["matches"].values())


def test_real_ledger_binding_is_independently_recomputable() -> None:
    """台账里 GOLD-027 的 result_sha256 / reviewed commit 必须能被测试自己复算。"""

    entry = next(
        item for item in load_ledger()["entries"] if item["task_id"] == LEDGER_TASK_ID
    )

    expected_sha = hashlib.sha256(
        git_blob_bytes(f"HEAD:.ai/results/{LEDGER_TASK_ID}.json")
    ).hexdigest()

    assert entry["reviewed_result"]["result_sha256"] == expected_sha
    assert entry["reviewed_commit"]["sha"] == own_terminal_commit(LEDGER_TASK_ID)

    report = build_report()

    view = next(item for item in report["bindings"] if item["task_id"] == LEDGER_TASK_ID)

    assert view["actual"]["result_sha256"] == expected_sha
    assert view["actual"]["commit_sha"] == own_terminal_commit(LEDGER_TASK_ID)


def test_tampered_real_ledger_hash_is_fail_closed(tmp_path: Path) -> None:
    payload = load_ledger()

    payload["entries"][1]["reviewed_result"]["result_sha256"] = "0" * 64

    report = build_report(write_ledger_copy(tmp_path, payload))

    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_RESULT_HASH_MISMATCH in codes
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL


def test_tampered_real_ledger_commit_is_fail_closed(tmp_path: Path) -> None:
    payload = load_ledger()

    payload["entries"][0]["reviewed_commit"]["sha"] = "0" * 40

    report = build_report(write_ledger_copy(tmp_path, payload))

    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_COMMIT_SHA_MISMATCH in codes
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL


def test_deleted_real_ledger_entry_is_fail_closed(tmp_path: Path) -> None:
    payload = load_ledger()

    payload["entries"] = [
        entry for entry in payload["entries"] if entry["task_id"] != "GOLD-026"
    ]

    report = build_report(write_ledger_copy(tmp_path, payload))

    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_LEDGER_CHAIN_GAP in codes
    assert report["chain"]["missing_in_window"] == ["GOLD-026"]
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL


def test_reordered_real_ledger_is_fail_closed(tmp_path: Path) -> None:
    payload = load_ledger()

    payload["entries"] = list(reversed(payload["entries"]))

    report = build_report(write_ledger_copy(tmp_path, payload))

    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_LEDGER_ORDER_REGRESSION in codes
    assert report["chain"]["order_is_ascending"] is False


def test_duplicated_real_ledger_task_is_fail_closed(tmp_path: Path) -> None:
    payload = load_ledger()

    payload["entries"] = [*payload["entries"], dict(payload["entries"][0])]

    report = build_report(write_ledger_copy(tmp_path, payload))

    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_LEDGER_DUPLICATE_TASK in codes
    assert report["ledger"]["duplicate_tasks"] == ["GOLD-025"]


def test_report_is_read_only_on_real_repo() -> None:
    before_tasks = tree_digest(TASKS_DIR)

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    build_report()

    assert tree_digest(TASKS_DIR) == before_tasks
    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_report_facts_digest_is_deterministic() -> None:
    first = build_report()

    second = build_report()

    assert first["facts_digest"] == second["facts_digest"]
    assert integrity.render_integrity_report(first) == integrity.render_integrity_report(second)


def test_cli_is_deterministic_and_zero_side_effect() -> None:
    command = [
        sys.executable,
        "-m",
        "orchestrator.review_ledger_integrity",
        "--generated-at",
        AUDIT_TIME,
    ]

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == integrity.EXIT_OK
    assert first.stderr == b""
    assert first.stdout == second.stdout
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["schema"] == integrity.LEDGER_INTEGRITY_SCHEMA
    assert payload["summary"]["integrity_ok"] is True
    assert payload["generated_at"] == AUDIT_TIME

    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    build_report()

    state = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert state["phase"] == "Phase 3"
    assert state["status"] == "BLOCKED"
    assert "PHASE3_3_DATA" in [blocker["code"] for blocker in state["blockers"]]
    assert "LIVE_TRADING=false" in state["invariants"]
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]
    assert state["queue_status"] == "ACTIVE"



