"""GOLD-044：真实仓库上的 review closure adjudication 集成回归（只读）。

覆盖（与 GOLD-044 requirements / acceptance 一一对应）：

1. 没有裁决时历史 legacy 矛盾继续 fail-closed（``facts_ready=false`` / exit ``2``）；
2. 合法 GPT 裁决（独立用 raw ``git`` blob + ``hashlib`` 复算身份）⇒ ``facts_ready=true``
   / exit ``0``，且**不改写**任何历史 result / ledger / state；
3. 越权（Cline）/ 身份漂移 / 重复裁决一律 fail-closed；
4. 裁决 ≠ review verdict：不写 ``GPT_REVIEW_LEDGER``、不推进 ``last_reviewed_task``、
   不解除 ``PHASE3_3_DATA``；
5. ledger 连续性仍严格执行：即使 GOLD-035 有有效裁决，把台账「跳过」中间的未 review
   任务仍会被 ``LEDGER_CHAIN_GAP`` fail-closed。

红线：本文件只读 —— 绝不写 ``.ai/**``；裁决 store 只写临时目录，绝不写入仓库。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import review_backlog as backlog
from orchestrator import review_binding as binding
from orchestrator import review_ledger_integrity as integrity

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

ADJUDICATION_DIR = REPO_ROOT / ".ai" / "adjudications"

TASK_ID = "GOLD-035"

AUDIT_TIME = "2026-09-24T00:00:00+08:00"

LEGACY_CODE = "RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS"


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


def repo_snapshot() -> dict[str, Any]:
    return {
        "results": tree_digest(RESULTS_DIR),
        "tasks": tree_digest(TASKS_DIR),
        "state": PROJECT_STATE.read_bytes(),
        "ledger": REVIEW_LEDGER.read_bytes(),
        "status": worktree_status(),
    }


def assert_repo_unchanged(before: dict[str, Any]) -> None:
    assert tree_digest(RESULTS_DIR) == before["results"]
    assert tree_digest(TASKS_DIR) == before["tasks"]
    assert PROJECT_STATE.read_bytes() == before["state"]
    assert REVIEW_LEDGER.read_bytes() == before["ledger"]
    assert worktree_status() == before["status"]
    # 仓库里**不存在**任何真实裁决 store（Executor 绝不创建）。
    assert ADJUDICATION_DIR.exists() is False


def build_manifest(store_path: Path | None = None, task_id: str = TASK_ID) -> dict[str, Any]:
    return binding.build_review_binding_manifest(
        task_id,
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        adjudication_store_path=store_path,
        generated_at=AUDIT_TIME,
    )


def make_entry(
    manifest: dict[str, Any],
    *,
    task_id: str = TASK_ID,
    reviewer: str = "gpt",
    reviewer_role: str = "GPT",
    result_sha256: str | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    result = manifest["result"]

    commit = manifest["commit"]

    return {
        "adjudication_id": f"ADJ-{task_id}-1",
        "task_id": task_id,
        "adjudication_type": adjudication.ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,
        "reviewer": reviewer,
        "reviewer_role": reviewer_role,
        "reason_summary": f"{task_id}: legacy raw finish_reason artifact; no history rewrite",
        "adjudicated_at": "2026-09-24T10:00:00+08:00",
        "bound_result": {
            "sha256": result_sha256 if result_sha256 is not None else result["sha256"],
            "status": result["status"],
        },
        "bound_commit": {
            "sha": commit_sha if commit_sha is not None else commit["sha"],
            "branch": branch if branch is not None else commit["branch"],
        },
        "contradiction_reason_codes": [LEGACY_CODE],
    }


def write_store(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    store = tmp_path / "legacy_result_adjudications.json"

    store.write_text(
        json.dumps(
            {
                "schema": adjudication.STORE_SCHEMA,
                "schema_version": adjudication.STORE_SCHEMA_VERSION,
                "adjudications": entries,
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return store


@pytest.fixture()
def before_repo() -> dict[str, Any]:
    return repo_snapshot()


# ============================================================
# 1. 默认（无裁决）⇒ 历史矛盾继续 fail-closed
# ============================================================


def test_without_adjudication_history_stays_fail_closed(before_repo: dict[str, Any]) -> None:
    manifest = build_manifest()

    assert manifest["result"]["status"] == "completed"
    assert manifest["binding"]["facts_complete"] is False
    assert manifest["binding"]["facts_ready"] is False
    assert manifest["binding"]["adjudicated"] is False
    assert manifest["binding"]["reason_codes"] == [LEGACY_CODE]
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT

    assert manifest["adjudication"]["store_present"] is False
    assert manifest["adjudication"]["state"] == adjudication.TASK_STATE_UNADJUDICATED

    assert_repo_unchanged(before_repo)


def test_missing_adjudication_store_is_never_created(before_repo: dict[str, Any]) -> None:
    build_manifest()

    assert ADJUDICATION_DIR.exists() is False

    assert_repo_unchanged(before_repo)


# ============================================================
# 2. 合法 GPT 裁决 ⇒ facts-ready（身份由 raw git + hashlib 独立复算）
# ============================================================


def test_valid_gpt_adjudication_restores_bindability(
    tmp_path: Path, before_repo: dict[str, Any]
) -> None:
    manifest = build_manifest()

    # 独立复算：result canonical sha256（Git blob 字节）+ 终态 commit sha。
    expected_sha = hashlib.sha256(git_blob_bytes(f"HEAD:.ai/results/{TASK_ID}.json")).hexdigest()

    assert manifest["result"]["sha256"] == expected_sha

    commit_sha = own_terminal_commit(TASK_ID)

    assert manifest["commit"]["sha"] == commit_sha

    store = write_store(tmp_path, [make_entry(manifest)])

    adjudicated = build_manifest(store)

    assert adjudicated["binding"]["facts_complete"] is False
    assert adjudicated["binding"]["facts_ready"] is True
    assert adjudicated["binding"]["adjudicated"] is True
    assert adjudicated["binding"]["facts_ready_source"] == binding.FACTS_READY_SOURCE_ADJUDICATED
    assert adjudicated["summary"]["exit_code"] == binding.EXIT_OK

    # 历史事实原样保留：原始 contradiction code 与 result sha256 都不变。
    assert adjudicated["binding"]["reason_codes"] == [LEGACY_CODE]
    assert [issue["code"] for issue in adjudicated["issues"]] == [LEGACY_CODE]
    assert adjudicated["result"]["sha256"] == expected_sha
    assert adjudicated["commit"]["sha"] == commit_sha

    section = adjudicated["adjudication"]

    assert section["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert section["valid"] is True
    assert section["adjudication"]["reviewer"] == "gpt"
    assert section["adjudication"]["reviewer_role"] == "GPT"
    assert section["adjudication"]["bound_result"]["sha256"] == expected_sha
    assert section["adjudication"]["bound_commit"]["sha"] == commit_sha

    # 裁决绝不改写历史：仓库字节完全不变。
    assert_repo_unchanged(before_repo)



# ============================================================
# 3. 越权 / 身份漂移 / 重复裁决 ⇒ fail-closed
# ============================================================


def test_executor_adjudication_is_fail_closed(tmp_path: Path) -> None:
    manifest = build_manifest()

    store = write_store(
        tmp_path,
        [make_entry(manifest, reviewer="cline", reviewer_role="Executor")],
    )

    adjudicated = build_manifest(store)

    assert adjudicated["binding"]["facts_ready"] is False
    assert adjudicated["adjudication"]["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_EXECUTOR_FORBIDDEN in adjudicated["binding"]["reason_codes"]
    assert adjudicated["summary"]["exit_code"] == binding.EXIT_DRIFT


def test_result_sha256_drift_is_fail_closed(tmp_path: Path) -> None:
    manifest = build_manifest()

    store = write_store(tmp_path, [make_entry(manifest, result_sha256="0" * 64)])

    adjudicated = build_manifest(store)

    assert adjudicated["binding"]["facts_ready"] is False
    assert adjudication.ISSUE_RESULT_SHA256_DRIFT in adjudicated["binding"]["reason_codes"]


def test_commit_branch_drift_is_fail_closed(tmp_path: Path) -> None:
    manifest = build_manifest()

    store = write_store(tmp_path, [make_entry(manifest, branch="other-branch")])

    adjudicated = build_manifest(store)

    assert adjudicated["binding"]["facts_ready"] is False
    assert adjudication.ISSUE_COMMIT_BRANCH_DRIFT in adjudicated["binding"]["reason_codes"]


def test_duplicate_adjudications_are_fail_closed(tmp_path: Path) -> None:
    manifest = build_manifest()

    entry = make_entry(manifest)

    store = write_store(tmp_path, [entry, dict(entry)])

    adjudicated = build_manifest(store)

    assert adjudicated["binding"]["facts_ready"] is False
    assert "ADJUDICATION_TASK_ID_DUPLICATE" in adjudicated["binding"]["reason_codes"]


# ============================================================
# 4. 裁决 ≠ ledger 写入 / 指针推进 / Phase 解除
# ============================================================


def test_valid_adjudication_does_not_touch_ledger_or_state(
    tmp_path: Path, before_repo: dict[str, Any]
) -> None:
    manifest = build_manifest()

    store = write_store(tmp_path, [make_entry(manifest)])

    report = integrity.build_integrity_report(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        ledger_path=REVIEW_LEDGER,
        adjudication_store_path=store,
        generated_at=AUDIT_TIME,
    )

    # 正式台账仍连续到 GOLD-027：裁决不让本工具自动写台账 / 推进指针。
    assert report["summary"]["integrity_ok"] is True
    assert report["ledger"]["valid_entry_count"] == 3
    assert report["ledger"]["adjudicated_entry_count"] == 0

    state = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert state["last_reviewed_task"] == "GOLD-027"
    assert "PHASE3_3_DATA" in [blocker["code"] for blocker in state["blockers"]]
    assert report["authority"]["writes_review_ledger"] is False
    assert report["authority"]["tool_can_sign_adjudication"] is False

    assert_repo_unchanged(before_repo)



# ============================================================
# 5. ledger 连续性仍严格执行（裁决不能让台账跳过未 review 任务）
# ============================================================


def temp_ledger_with_adjudicated_entry(
    tmp_path: Path,
    store_path: Path,
    *,
    reviewed_at: str = "2026-09-24T12:00:00+08:00",
) -> Path:
    """把正式台账复制到临时目录，并追加一条 GOLD-035 条目（**绝不写仓库**）。"""

    payload = json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))

    manifest = build_manifest(store_path)

    payload["entries"].append(
        {
            "task_id": TASK_ID,
            "verdict": "PASS",
            "reviewer_role": "GPT",
            "reviewer": "gpt",
            "reviewed_result": {
                "result_sha256": manifest["result"]["sha256"],
                "status": manifest["result"]["status"],
                "finished_at": manifest["result"]["finished_at"],
            },
            "reviewed_commit": {
                "sha": manifest["commit"]["sha"],
                "branch": manifest["commit"]["branch"],
            },
            "acceptance_summary": "GPT review PASS: adjudicated legacy contradiction fixture",
            "reviewed_at": reviewed_at,
        }
    )

    target = tmp_path / "GPT_REVIEW_LEDGER.json"

    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return target


def test_continuity_gate_still_fails_closed_on_skipped_gap(
    tmp_path: Path, before_repo: dict[str, Any]
) -> None:
    store = write_store(tmp_path, [make_entry(build_manifest())])

    ledger = temp_ledger_with_adjudicated_entry(tmp_path, store)

    report = integrity.build_integrity_report(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        ledger_path=ledger,
        adjudication_store_path=store,
        generated_at=AUDIT_TIME,
    )

    view = {entry["task_id"]: entry for entry in report["bindings"]}[TASK_ID]

    # 有效裁决让**这一条**台账条目的客观身份可核对（facts-ready）……
    assert view["bound"] is True
    assert view["manifest_adjudicated"] is True

    # ……但连续性门禁绝不因此放宽：GOLD-028..034 的未 review 空档仍 fail-closed。
    codes = {issue["code"] for issue in report["issues"]}

    assert integrity.ISSUE_LEDGER_CHAIN_GAP in codes
    assert report["summary"]["integrity_ok"] is False

    assert_repo_unchanged(before_repo)


# ============================================================
# 6. review_backlog 端到端：同一裁决事实 ⇒ 已裁决待 review
# ============================================================


def test_backlog_classifies_adjudicated_contradiction_end_to_end(
    tmp_path: Path, before_repo: dict[str, Any]
) -> None:
    store = write_store(tmp_path, [make_entry(build_manifest())])

    payload = backlog.build_review_backlog_manifest(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        state_path=PROJECT_STATE,
        ledger_path=REVIEW_LEDGER,
        adjudication_store_path=store,
        generated_at=AUDIT_TIME,
    )

    items = {item["task_id"]: item for item in payload["backlog"]}

    adjudicated_item = items[TASK_ID]

    assert adjudicated_item["adjudicated"] is True
    assert adjudicated_item["facts_ready"] is True
    assert adjudicated_item["review_status"] == backlog.REVIEW_STATUS_PENDING
    assert adjudicated_item["adjudication"]["state"] == adjudication.TASK_STATE_ADJUDICATED

    # 其它已知矛盾仍无裁决 ⇒ 仍然 fail-closed（unresolved）。
    others = [item for task, item in items.items() if task != TASK_ID]

    assert all(item["adjudicated"] is False for item in others)
    assert all(
        item["review_status"] == backlog.REVIEW_STATUS_INVALID
        for item in others
        if item["facts_complete"] is False
    )

    assert payload["summary"]["adjudicated_count"] == 1
    assert payload["summary"]["adjudicated_pending_count"] == 1
    assert payload["summary"]["unresolved_contradiction_count"] == 7

    assert_repo_unchanged(before_repo)

