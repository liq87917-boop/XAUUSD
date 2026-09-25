"""GOLD-045：真实仓库上的 review closure precondition 集成回归（只读）。

覆盖（与 GOLD-045 requirements / acceptance 一一对应）：

1. 真实仓库上「候选写集 == 已提交事实」的一次性预检 ⇒ ``candidate_ready=true`` /
   退出码 ``0``，且 ``closure`` 明确 ``review_completed=false``（候选通过 ≠ review 完成）；
2. 端到端**零写入**：``.ai/results`` 全树 / ``.ai/tasks`` 全树 / ``PROJECT_STATE`` /
   ``GPT_REVIEW_LEDGER`` / ``.ai/adjudications`` 前后字节一致，worktree 状态不变；
3. 幂等：同一候选 + 同一已提交事实 ⇒ 相同 ``precondition_digest``；
4. fail-closed 回归：stale HEAD / 候选指针漂移 / 删除 ``PHASE3_3_DATA`` blocker /
   ledger 链断裂一律 ``exit 2`` 且零写入；
5. CLI 端到端（显式临时文件候选）与内存 API 结论一致，且不写任何 managed 状态。

红线：本文件只读 —— 绝不写 ``.ai/**``（临时候选只写系统临时目录）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import planner_snapshot as planner
from orchestrator import review_backlog as backlog
from orchestrator import review_closure_precondition as precondition
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

REPO_ROOT = Path(__file__).resolve().parents[2]

AI_DIR = REPO_ROOT / ".ai"

TASKS_DIR = AI_DIR / "tasks"

RESULTS_DIR = AI_DIR / "results"

PROJECT_STATE = AI_DIR / "PROJECT_STATE.json"

REVIEW_LEDGER = AI_DIR / "GPT_REVIEW_LEDGER.json"

ADJUDICATION_DIR = AI_DIR / "adjudications"

AUDIT_TIME = "2026-09-24T00:00:00+08:00"

STALE_HEAD = "0" * 40

_CACHE: dict[str, Any] = {}


def canonical_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, check=False
    )

    assert completed.returncode == 0, (args, completed.stderr)

    return completed.stdout.decode("utf-8", errors="replace")


def worktree_status() -> str:
    return git("status", "--porcelain")


def repo_snapshot() -> dict[str, Any]:
    return {
        "results": tree_digest(RESULTS_DIR),
        "tasks": tree_digest(TASKS_DIR),
        "adjudications": tree_digest(ADJUDICATION_DIR),
        "state": PROJECT_STATE.read_bytes(),
        "ledger": REVIEW_LEDGER.read_bytes(),
        "status": worktree_status(),
    }


def assert_repo_unchanged(before: dict[str, Any]) -> None:
    assert tree_digest(RESULTS_DIR) == before["results"]
    assert tree_digest(TASKS_DIR) == before["tasks"]
    assert tree_digest(ADJUDICATION_DIR) == before["adjudications"]
    assert PROJECT_STATE.read_bytes() == before["state"]
    assert REVIEW_LEDGER.read_bytes() == before["ledger"]
    assert worktree_status() == before["status"]


ADJUDICATION_STORE = ADJUDICATION_DIR / "legacy_result_adjudications.json"


def load_project_state() -> dict[str, Any]:
    return json.loads(PROJECT_STATE.read_text(encoding="utf-8"))


def load_review_ledger() -> dict[str, Any]:
    return json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))


def observed_head() -> str:
    git_view, _ = planner.git_section(REPO_ROOT)

    head = git_view.get("head")

    assert isinstance(head, str) and head, git_view

    return head


def adjudication_store_sha() -> str | None:
    if not ADJUDICATION_STORE.exists():
        return None

    return sha256_file(ADJUDICATION_STORE)


def committed_base(*, head_sha: str | None = None, **overrides: Any) -> dict[str, Any]:
    """与 §2.12 完全同口径的已提交摘要（测试**自己**复算）。"""

    base: dict[str, Any] = {
        "head_sha": observed_head() if head_sha is None else head_sha,
        "results_digest": canonical_digest(tree_digest(RESULTS_DIR)),
        "tasks_digest": canonical_digest(tree_digest(TASKS_DIR)),
        "project_state_sha256": sha256_file(PROJECT_STATE),
        "review_ledger_sha256": sha256_file(REVIEW_LEDGER),
        "adjudication_store_sha256": adjudication_store_sha(),
    }

    base.update(overrides)

    return base


def latest_completed_task() -> str:
    statuses = planner.result_statuses(RESULTS_DIR)

    completed = [
        task_id
        for task_id in planner.terminal_result_ids(
            statuses, planner.project_task_prefix(load_project_state())
        )
        if statuses[task_id] == "completed"
    ]

    assert completed, "真实仓库必须存在 completed result"

    return completed[-1]


def pending_task() -> str | None:
    statuses = planner.result_statuses(RESULTS_DIR)

    declared, _ = planner.declared_queue(load_project_state())

    candidates = sorted(
        set(declared) | set(planner.task_file_ids(TASKS_DIR)), key=planner.task_id_sort_key
    )

    for task_id in candidates:
        if statuses.get(task_id) not in {"completed", "blocked"}:
            return task_id

    return None


def consistent_state() -> dict[str, Any]:
    """把所有指针对齐真实 results 的候选 state（只改内存副本）。"""

    state = load_project_state()

    state["last_completed_task"] = latest_completed_task()

    # current_task 必须始终对齐真实 results：pending 为 None 时清空，否则会残留
    # 指向已有终态 result 的旧值，被 pointer_section 误判为 CANDIDATE_STATE_POINTER_DRIFT。
    state["current_task"] = pending_task()

    return state


def make_candidate(
    *,
    project_state: dict[str, Any] | None = None,
    review_ledger: dict[str, Any] | None = None,
    adjudication_store_payload: dict[str, Any] | None = None,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": precondition.CANDIDATE_SCHEMA,
        "schema_version": precondition.CANDIDATE_SCHEMA_VERSION,
        "base": committed_base() if base is None else base,
        "adjudication_store": adjudication_store_payload,
        "review_ledger": review_ledger,
        "project_state": project_state,
    }


def real_committed_facts() -> tuple[dict[str, Any], dict[str, Any]]:
    """真实 §2.11 backlog / §2.9 integrity 事实（只构建一次并复用，成本受控）。"""

    if "facts" not in _CACHE:
        _CACHE["facts"] = (
            backlog.build_review_backlog_manifest(
                root=REPO_ROOT,
                tasks_dir=TASKS_DIR,
                results_dir=RESULTS_DIR,
                state_path=PROJECT_STATE,
                ledger_path=REVIEW_LEDGER,
                adjudication_store_path=ADJUDICATION_STORE,
                generated_at=AUDIT_TIME,
            ),
            integrity.build_integrity_report(
                root=REPO_ROOT,
                tasks_dir=TASKS_DIR,
                results_dir=RESULTS_DIR,
                ledger_path=REVIEW_LEDGER,
                adjudication_store_path=ADJUDICATION_STORE,
                generated_at=AUDIT_TIME,
            ),
        )

    return _CACHE["facts"]


def run_precondition(
    candidate: dict[str, Any],
    *,
    backlog_builder: precondition.BacklogBuilder | None = None,
    integrity_builder: precondition.IntegrityBuilder | None = None,
) -> dict[str, Any]:
    return precondition.build_review_closure_precondition(
        candidate=candidate,
        candidate_source="<integration>",
        root=REPO_ROOT,
        generated_at=AUDIT_TIME,
        as_of=AUDIT_TIME,
        backlog_builder=backlog_builder,
        integrity_builder=integrity_builder,
    )


def run_light(candidate: dict[str, Any]) -> dict[str, Any]:
    """committed backlog / integrity 事实**只构建一次**并复用（真实 builder，成本受控）。"""

    backlog_facts, integrity_facts = real_committed_facts()

    return run_precondition(
        candidate,
        backlog_builder=lambda: backlog_facts,
        integrity_builder=lambda: integrity_facts,
    )




# ============================================================
# 1. 真实仓库：候选 == 已提交事实 ⇒ candidate-ready + 零写入 + 幂等
# ============================================================


def test_real_repo_matching_candidate_is_ready_and_zero_write() -> None:
    before = repo_snapshot()

    candidate = make_candidate(
        project_state=consistent_state(),
        review_ledger=load_review_ledger(),
    )

    payload = run_light(candidate)

    assert payload["reason_codes"] == []
    assert payload["candidate_ready"] is True
    assert payload["summary"]["exit_code"] == precondition.EXIT_OK
    assert payload["head"]["resolved"] is True
    assert payload["base"]["matches_expected"] is True

    closure = payload["closure"]

    assert closure["review_completed"] is False
    assert closure["review_verdict_issued"] is False
    assert closure["phase_gate_lifted"] is False
    assert closure["executor_write_triggered"] is False
    assert closure["state_advanced"] is False
    assert closure["auto_push"] is False

    # 幂等：相同候选 + 相同已提交事实 ⇒ 相同 digest。
    assert payload["precondition_digest"] == run_light(candidate)["precondition_digest"]

    assert_repo_unchanged(before)


def test_real_repo_committed_ledger_identity_is_bound() -> None:
    payload = run_light(
        make_candidate(
            project_state=consistent_state(),
            review_ledger=load_review_ledger(),
        )
    )

    bindings = payload["candidate"]["review_ledger"]["bindings"]

    assert bindings
    assert all(binding["bound"] is True for binding in bindings)
    assert payload["candidate"]["review_ledger"]["removed_task_ids"] == []
    assert payload["candidate"]["review_ledger"]["chain"]["missing_in_window"] == []


# ============================================================
# 2. 真实仓库：fail-closed 回归（stale HEAD / 指针漂移 / blocker / 链断裂）
# ============================================================


def test_real_repo_stale_head_fails_closed() -> None:
    before = repo_snapshot()

    payload = run_light(
        make_candidate(
            project_state=consistent_state(),
            review_ledger=load_review_ledger(),
            base=committed_base(head_sha=STALE_HEAD),
        )
    )

    assert precondition.REASON_STALE_REMOTE_HEAD in payload["reason_codes"]
    assert payload["summary"]["stale_remote_head"] is True
    assert payload["candidate_ready"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED

    assert_repo_unchanged(before)


def test_real_repo_state_pointer_drift_fails_closed() -> None:
    before = repo_snapshot()

    state = consistent_state()

    state["current_task"] = latest_completed_task()

    payload = run_light(
        make_candidate(project_state=state, review_ledger=load_review_ledger())
    )

    assert precondition.REASON_STATE_POINTER_DRIFT in payload["reason_codes"]
    assert payload["candidate_ready"] is False

    assert_repo_unchanged(before)



def test_real_repo_phase3_3_blocker_removal_fails_closed() -> None:
    before = repo_snapshot()

    state = consistent_state()

    # PHASE3_3_DATA 是 active blocker；删除它必须 fail-closed。
    state["blockers"] = [
        blocker
        for blocker in state.get("blockers", [])
        if blocker.get("code") != precondition.PHASE3_3_BLOCKER
    ]

    payload = run_light(
        make_candidate(project_state=state, review_ledger=load_review_ledger())
    )

    assert precondition.REASON_PHASE3_3_BLOCKER_REMOVED in payload["reason_codes"]
    assert payload["candidate_ready"] is False

    assert_repo_unchanged(before)


def test_real_repo_ledger_chain_gap_fails_closed() -> None:
    before = repo_snapshot()

    ledger = {
        "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
        "reviewed_from": "GOLD-028",
        "entries": [
            {
                "task_id": "GOLD-041",
                "verdict": "PASS",
                "reviewer_role": "GPT",
                "reviewer": "gpt",
                "reviewed_result": {
                    "result_sha256": "a" * 64,
                    "status": "completed",
                    "finished_at": "2026-09-24T00:00:00+08:00",
                },
                "reviewed_commit": {"sha": "b" * 40, "branch": "cline-agent"},
                "acceptance_summary": "fixture（集成测试，绝不写入仓库）",
                "reviewed_at": "2026-09-24T00:30:00+08:00",
            }
        ],
    }

    payload = run_light(
        make_candidate(project_state=consistent_state(), review_ledger=ledger)
    )

    assert integrity.ISSUE_LEDGER_CHAIN_GAP in payload["reason_codes"]
    assert precondition.REASON_LEDGER_ENTRY_REMOVED in payload["reason_codes"]
    assert payload["candidate_ready"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED

    assert_repo_unchanged(before)


# ============================================================
# 3. 真实仓库：CLI 端到端（显式临时文件候选）零写入
# ============================================================


def test_real_repo_cli_temp_file_candidate_zero_write(
    tmp_path: Path, capsys: Any
) -> None:
    before = repo_snapshot()

    candidate_path = tmp_path / "closure-candidate.json"

    candidate_path.write_text(
        json.dumps(
            make_candidate(
                project_state=consistent_state(),
                review_ledger=load_review_ledger(),
            )
        ),
        encoding="utf-8",
    )

    exit_code = precondition.main(
        [
            "--root",
            str(REPO_ROOT),
            "--candidate",
            str(candidate_path),
            "--generated-at",
            AUDIT_TIME,
            "--as-of",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    payload = json.loads(captured.out)

    assert exit_code == precondition.EXIT_OK
    assert payload["candidate_ready"] is True
    assert payload["candidate_source"] == str(candidate_path.resolve())
    assert payload["closure"]["review_completed"] is False
    # CLI 端到端走**真实**默认 builder：committed backlog / integrity 必须可用。
    assert payload["committed_current"]["review_backlog"]["available"] is True
    assert payload["committed_current"]["review_integrity"]["available"] is True
    assert payload["committed_current"]["review_backlog"]["backlog_digest"]

    assert_repo_unchanged(before)


def test_real_repo_module_does_not_expose_mutation_api() -> None:
    public = {name for name in dir(precondition) if not name.startswith("_")}

    for name in (
        "apply_candidate",
        "write_adjudication",
        "write_review_ledger",
        "update_project_state",
        "advance_review_pointer",
        "sign_adjudication",
        "sign_review",
    ):
        assert name not in public

