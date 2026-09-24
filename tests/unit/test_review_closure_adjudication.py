"""GOLD-044：把有效 §2.15 GPT 裁决接入 review binding / backlog / integrity / refill 的回归。

覆盖（与 GOLD-044 requirements 一一对应）：

1. ``review_binding`` 对 legacy 终态矛盾**默认**仍是 ``facts_complete=false`` /
   ``facts_ready=false``（statically fail-closed）；
2. 只有 schema 合法、GPT 权威有效、原 result / commit / contradiction 身份完全匹配且
   不冲突的裁决，才把该项标为 ``adjudicated`` + ``facts_ready=true``；
3. 即使裁决有效，manifest 仍**原样保留**原始 contradiction finding、原 result ``sha256``
   与裁决 identity（绝不删除 / 降级 / 伪装历史事实）；
4. 越权 / 未知身份 / 重复 / 冲突 / 身份漂移 / store 损坏一律 fail-closed；
5. ``review_backlog`` / ``review_ledger_integrity`` / ``planner_mutation_precondition`` /
   refill 诊断复用**同一**裁决事实，稳定区分未裁决矛盾 / 已裁决待 review / 已绑定 / 漂移；
6. 有效裁决**不**产生 PASS/FAIL、**不**写 ledger、**不**推进指针、**不**解除 Phase 3.3。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不在真实仓库创建裁决 store。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import planner_mutation_precondition as precondition
from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner
from orchestrator import result_terminal_consistency as terminal
from orchestrator import review_backlog as backlog
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

AUDIT_TIME = "2026-09-24T00:00:00+08:00"

BRANCH = "cline-agent"

HEAD_SHA = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"

COMMIT_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

BLOB_ID = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

TASK_ID = "GOLD-900"

LEGACY_CODE = terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS


# ============================================================
# fixture：最小只读仓库（tasks / results / state / ledger / fake .git）
# ============================================================


def legacy_contradiction_result(task_id: str) -> dict[str, Any]:
    """历史矛盾形状：顶层 ``status=completed`` + 最终 attempt ``finish_reason=aborted``。"""

    return {
        "task_id": task_id,
        "title": f"{task_id} fixture",
        "status": "completed",
        "finished_at": "2026-09-23T20:07:56+08:00",
        "attempt_count": 1,
        "max_attempts": 3,
        "attempts": [
            {
                "attempt": 1,
                "cline_exit_code": 0,
                "cline_timed_out": False,
                "failure_class": "none",
                "failure_code": None,
                "finish_reason": "aborted",
                "validations": [{"command": "pytest", "returncode": 0, "timed_out": False}],
            }
        ],
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def seed_fake_git(root: Path) -> None:
    git_dir = root / ".git"

    refs = git_dir / "refs" / "heads"

    refs.mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{BRANCH}\n", encoding="utf-8")

    (refs / BRANCH).write_text(f"{HEAD_SHA}\n", encoding="utf-8")


@dataclass(frozen=True)
class RepoFS:
    root: Path
    tasks: Path
    results: Path
    state: Path
    ledger: Path
    store: Path


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepoFS:
    """把 tasks / results / ROOT 重定向到 tmp_path（对真实仓库零影响）。"""

    tasks = tmp_path / ".ai" / "tasks"
    results = tmp_path / ".ai" / "results"

    tasks.mkdir(parents=True)

    results.mkdir(parents=True)

    monkeypatch.setattr(orch, "TASK_DIR", tasks)

    monkeypatch.setattr(orch, "RESULT_DIR", results)

    monkeypatch.setattr(binding, "ROOT", tmp_path)

    seed_fake_git(tmp_path)

    fs = RepoFS(
        root=tmp_path,
        tasks=tasks,
        results=results,
        state=tmp_path / ".ai" / "PROJECT_STATE.json",
        ledger=tmp_path / ".ai" / "GPT_REVIEW_LEDGER.json",
        store=tmp_path / ".ai" / "adjudications" / "legacy_result_adjudications.json",
    )

    write_json(
        fs.state,
        {
            "schema_version": 1,
            "project": "XAUUSD",
            "branch": BRANCH,
            "phase": "Phase 3",
            "status": "BLOCKED",
            "current_task": TASK_ID,
            "last_completed_task": TASK_ID,
            "last_reviewed_task": "GOLD-899",
            "blockers": [{"code": "PHASE3_3_DATA", "detail": "blocked", "retryable": False}],
            "invariants": ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"],
            "task_queue": [TASK_ID],
            "queue_status": "ACTIVE",
            "queue_target_size": 3,
            "planner_lookahead_size": 3,
        },
    )

    write_json(
        fs.ledger,
        {
            "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
            "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
            "reviewed_from": "GOLD-899",
            "entries": [],
        },
    )

    return fs


def seed_task(fs: RepoFS, task_id: str = TASK_ID) -> dict[str, Any]:
    """写 task / result（历史矛盾形状），返回 result payload。"""

    write_json(
        fs.tasks / f"{task_id}.json",
        {
            "task_id": task_id,
            "title": f"{task_id} fixture",
            "type": "TEST",
            "human_gate": "L1",
            "depends_on": [],
            "auto_start": True,
            "requires_human_approval": False,
            "max_attempts": 3,
        },
    )

    result = legacy_contradiction_result(task_id)

    write_json(fs.results / f"{task_id}.json", result)

    return result


def result_canonical_sha(fs: RepoFS, task_id: str = TASK_ID) -> str:
    return hashlib.sha256((fs.results / f"{task_id}.json").read_bytes()).hexdigest()


def make_entry(
    task_id: str,
    *,
    result_sha: str,
    status: str = "completed",
    commit_sha: str = COMMIT_SHA,
    branch: str = BRANCH,
    reviewer: str = "gpt",
    reviewer_role: str = "GPT",
    adjudication_id: str | None = None,
    reason_summary: str = "legacy raw finish_reason artifact; no rewrite of history",
    adjudicated_at: str = "2026-09-24T10:00:00+08:00",
    codes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "adjudication_id": adjudication_id or f"ADJ-{task_id}-1",
        "task_id": task_id,
        "adjudication_type": adjudication.ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,
        "reviewer": reviewer,
        "reviewer_role": reviewer_role,
        "reason_summary": reason_summary,
        "adjudicated_at": adjudicated_at,
        "bound_result": {"sha256": result_sha, "status": status},
        "bound_commit": {"sha": commit_sha, "branch": branch},
        "contradiction_reason_codes": codes if codes is not None else [LEGACY_CODE],
    }


def write_store(store_path: Path, entries: list[dict[str, Any]]) -> Path:
    write_json(
        store_path,
        {
            "schema": adjudication.STORE_SCHEMA,
            "schema_version": adjudication.STORE_SCHEMA_VERSION,
            "adjudications": entries,
        },
    )

    return store_path


def valid_store(fs: RepoFS, task_id: str = TASK_ID, **overrides: Any) -> Path:
    defaults: dict[str, Any] = {"result_sha": result_canonical_sha(fs, task_id)}

    defaults.update(overrides)

    return write_store(fs.store, [make_entry(task_id, **defaults)])


# ============================================================
# 只读 Git 注入点（零真实子进程：身份来自确定性 fake）
# ============================================================


def fake_git(fs: RepoFS, task_id: str = TASK_ID) -> dict[str, Any]:
    """只读 Git 事实：唯一终态 commit + 与工作树一致的 blob。"""

    entries = [
        {
            "sha": COMMIT_SHA,
            "committed_at": "2026-09-23T21:00:00+08:00",
            "subject": f"ai: complete {task_id}",
        }
    ]

    task_rel = (fs.tasks / f"{task_id}.json").relative_to(fs.root).as_posix()

    result_rel = (fs.results / f"{task_id}.json").relative_to(fs.root).as_posix()

    blob_ids = {task_rel: BLOB_ID, result_rel: BLOB_ID}

    canonical = {BLOB_ID: (fs.results / f"{task_id}.json").read_bytes()}

    def commit_log_provider(root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
        return list(entries), None

    def blob_id_reader(root: Path, sha: str, relative: str) -> tuple[str | None, str | None]:
        if relative in blob_ids:
            return blob_ids[relative], None

        return None, binding.GIT_BLOB_ABSENT

    def blob_bytes_reader(root: Path, blob_id: str) -> tuple[bytes | None, str | None]:
        if blob_id in canonical:
            return canonical[blob_id], None

        return None, binding.GIT_BLOB_UNAVAILABLE

    def worktree_blob_id_reader(root: Path, relative: str) -> tuple[str | None, str | None]:
        if relative in blob_ids:
            return blob_ids[relative], None

        return None, binding.GIT_BLOB_UNAVAILABLE

    return {
        "commit_log_provider": commit_log_provider,
        "blob_id_reader": blob_id_reader,
        "blob_bytes_reader": blob_bytes_reader,
        "worktree_blob_id_reader": worktree_blob_id_reader,
    }


def build_manifest(
    fs: RepoFS,
    task_id: str = TASK_ID,
    *,
    store_path: Path | None = None,
) -> dict[str, Any]:
    return binding.build_review_binding_manifest(
        task_id,
        root=fs.root,
        tasks_dir=fs.tasks,
        results_dir=fs.results,
        adjudication_store_path=store_path,
        generated_at=AUDIT_TIME,
        **fake_git(fs, task_id),
    )


def manifest_builder(fs: RepoFS) -> Any:
    """给 backlog / integrity 注入的 manifest 来源（同一 §2.8 口径 + §2.15 裁决事实）。"""

    def build(task_id: str) -> dict[str, Any]:
        return build_manifest(fs, task_id)

    return build


# ============================================================
# 1. review_binding：默认 fail-closed
# ============================================================


def test_without_store_binding_stays_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_complete"] is False
    assert manifest["binding"]["facts_ready"] is False
    assert manifest["binding"]["adjudicated"] is False
    assert manifest["binding"]["facts_ready_source"] == binding.FACTS_READY_SOURCE_NONE
    assert manifest["binding"]["reason_codes"] == [LEGACY_CODE]
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT

    assert manifest["adjudication"]["store_present"] is False
    assert manifest["adjudication"]["state"] == adjudication.TASK_STATE_UNADJUDICATED
    assert manifest["adjudication"]["adjudication"] is None


def test_missing_store_file_is_never_created(repo: RepoFS) -> None:
    seed_task(repo)

    build_manifest(repo)

    assert repo.store.exists() is False


# ============================================================
# 2. 有效裁决 ⇒ adjudicated facts-ready（但不改历史、不产 verdict）
# ============================================================


def test_valid_gpt_adjudication_marks_facts_ready(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_complete"] is False
    assert manifest["binding"]["facts_ready"] is True
    assert manifest["binding"]["adjudicated"] is True
    assert manifest["binding"]["facts_ready_source"] == binding.FACTS_READY_SOURCE_ADJUDICATED
    assert manifest["summary"]["exit_code"] == binding.EXIT_OK

    # 原始 contradiction finding 与原 result sha256 原样保留（绝不删除 / 降级）。
    assert manifest["binding"]["reason_codes"] == [LEGACY_CODE]
    assert [issue["code"] for issue in manifest["issues"]] == [LEGACY_CODE]
    assert manifest["result"]["sha256"] == result_canonical_sha(repo)

    section = manifest["adjudication"]

    assert section["store_present"] is True
    assert section["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert section["valid"] is True
    assert section["adjudication"]["adjudication_id"] == f"ADJ-{TASK_ID}-1"
    assert section["adjudication"]["reviewer"] == "gpt"
    assert section["adjudication"]["reviewer_role"] == "GPT"
    assert section["adjudication"]["bound_result"]["sha256"] == result_canonical_sha(repo)
    assert section["adjudication"]["bound_commit"]["sha"] == COMMIT_SHA
    assert section["contract_source"] == adjudication.SCHEMA


def test_adjudicated_binding_never_emits_review_outcome(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    manifest = build_manifest(repo)

    rendered = binding.render_review_binding_manifest(manifest)

    assert rendered.isascii()

    payload = json.loads(rendered)

    assert "verdict" not in payload["binding"]
    assert "acceptance_summary" not in payload
    assert "reviewed_at" not in payload

    authority = manifest["authority"]

    assert authority["review_authority"] == "gpt_only"
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_sign_adjudication"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["writes_adjudications"] is False
    assert authority["adjudication_implies_verdict"] is False
    assert authority["adjudication_advances_review_pointer"] is False
    assert authority["adjudication_lifts_phase3_3_blocker"] is False


def test_adjudication_does_not_change_result_bytes(repo: RepoFS) -> None:
    seed_task(repo)

    before = (repo.results / f"{TASK_ID}.json").read_bytes()

    valid_store(repo)

    build_manifest(repo)

    assert (repo.results / f"{TASK_ID}.json").read_bytes() == before


# ============================================================
# 3. 越权 / 漂移 / 重复 / 冲突 / store 损坏 ⇒ fail-closed
# ============================================================


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"reviewer": "cline", "reviewer_role": "Executor"}, "ADJUDICATION_EXECUTOR_FORBIDDEN"),
        ({"reviewer": "nobody", "reviewer_role": "GPT"}, "ADJUDICATION_REVIEWER_NOT_AUTHORIZED"),
        ({"result_sha": "0" * 64}, "ADJUDICATION_RESULT_SHA256_DRIFT"),
        ({"commit_sha": "9" * 40}, "ADJUDICATION_COMMIT_SHA_DRIFT"),
        ({"branch": "other-branch"}, "ADJUDICATION_COMMIT_BRANCH_DRIFT"),
        (
            {"codes": ["RESULT_TERMINAL_STATUS_UNKNOWN"]},
            "ADJUDICATION_CONTRADICTION_CODES_DRIFT",
        ),
    ],
)
def test_invalid_adjudication_is_fail_closed(
    repo: RepoFS,
    overrides: dict[str, Any],
    expected_code: str,
) -> None:
    seed_task(repo)

    valid_store(repo, **overrides)

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_ready"] is False
    assert manifest["binding"]["adjudicated"] is False
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT

    assert manifest["adjudication"]["state"] == adjudication.TASK_STATE_INVALID
    assert expected_code in manifest["binding"]["reason_codes"]
    # 原始矛盾仍然可见。
    assert LEGACY_CODE in manifest["binding"]["reason_codes"]


def test_duplicate_adjudications_are_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    entry = make_entry(TASK_ID, result_sha=result_canonical_sha(repo))

    write_store(repo.store, [entry, dict(entry)])

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_ready"] is False
    assert "ADJUDICATION_TASK_ID_DUPLICATE" in manifest["binding"]["reason_codes"]


def test_conflicting_adjudications_are_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    sha = result_canonical_sha(repo)

    write_store(
        repo.store,
        [
            make_entry(TASK_ID, result_sha=sha),
            make_entry(TASK_ID, result_sha=sha, adjudication_id="ADJ-2", reason_summary="conflict"),
        ],
    )

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_ready"] is False
    assert "ADJUDICATION_TASK_ID_CONFLICT" in manifest["binding"]["reason_codes"]


def test_corrupt_store_is_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    write_json(
        repo.store,
        {
            "schema": "gold-ai/legacy-result-adjudication-store/v999",
            "schema_version": 999,
            "adjudications": [],
        },
    )

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_ready"] is False
    assert adjudication.ISSUE_STORE_SCHEMA_UNSUPPORTED in manifest["binding"]["reason_codes"]


def test_unreadable_store_is_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    repo.store.parent.mkdir(parents=True, exist_ok=True)

    repo.store.write_text("{not json", encoding="utf-8")

    manifest = build_manifest(repo)

    assert manifest["binding"]["facts_ready"] is False
    assert adjudication.ISSUE_STORE_UNREADABLE in manifest["binding"]["reason_codes"]


# ============================================================
# 4. review_backlog：稳定区分未裁决矛盾 / 已裁决待 review / 已绑定 / 漂移
# ============================================================


def ledger_entry_for(repo: RepoFS) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "verdict": "PASS",
        "reviewer": "gpt",
        "reviewer_role": "GPT",
        "acceptance_summary": f"{TASK_ID}: fake GPT review summary",
        "reviewed_at": "2026-09-24T11:00:00+08:00",
        "reviewed_result": {
            "result_sha256": result_canonical_sha(repo),
            "status": "completed",
            "finished_at": "2026-09-23T20:07:56+08:00",
        },
        "reviewed_commit": {"sha": COMMIT_SHA, "branch": BRANCH},
    }


def write_ledger(repo: RepoFS, entries: list[dict[str, Any]], reviewed_from: str) -> None:
    write_json(
        repo.ledger,
        {
            "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
            "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
            "reviewed_from": reviewed_from,
            "entries": entries,
        },
    )


def build_backlog(repo: RepoFS) -> dict[str, Any]:
    return backlog.build_review_backlog_manifest(
        root=repo.root,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        state_path=repo.state,
        ledger_path=repo.ledger,
        adjudication_store_path=repo.store,
        generated_at=AUDIT_TIME,
        manifest_builder=manifest_builder(repo),
    )


def test_backlog_unresolved_contradiction_is_invalid_without_adjudication(repo: RepoFS) -> None:
    seed_task(repo)

    payload = build_backlog(repo)

    item = payload["backlog"][0]

    assert item["task_id"] == TASK_ID
    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert item["facts_ready"] is False
    assert item["adjudicated"] is False
    assert backlog.ISSUE_BACKLOG_ITEM_FACTS_INCOMPLETE in item["reason_codes"]
    assert payload["summary"]["unresolved_contradiction_count"] == 1
    assert payload["summary"]["adjudicated_count"] == 0


def test_backlog_adjudicated_item_is_pending_substantive_review(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    payload = build_backlog(repo)

    item = payload["backlog"][0]

    assert item["review_status"] == backlog.REVIEW_STATUS_PENDING
    assert item["facts_ready"] is True
    assert item["adjudicated"] is True
    assert item["facts_ready_source"] == binding.FACTS_READY_SOURCE_ADJUDICATED
    assert item["adjudication"]["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert item["adjudication"]["adjudication"]["reviewer"] == "gpt"
    # 原始矛盾 code 仍然在 missing_reason_codes 里（事实不隐藏）。
    assert LEGACY_CODE in item["missing_reason_codes"]
    assert payload["summary"]["adjudicated_pending_count"] == 1
    assert payload["summary"]["unresolved_contradiction_count"] == 0


def test_backlog_invalid_adjudication_is_fail_closed(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo, reviewer="cline", reviewer_role="Executor")

    payload = build_backlog(repo)

    item = payload["backlog"][0]

    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert item["adjudicated"] is False
    assert backlog.ISSUE_BACKLOG_ITEM_ADJUDICATION_INVALID in item["reason_codes"]


def test_backlog_bound_entry_requires_matching_identity(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    write_ledger(repo, [ledger_entry_for(repo)], TASK_ID)

    payload = build_backlog(repo)

    item = payload["backlog"][0]

    # 身份一致 ⇒ 已裁决事实让「已绑定」可被判定；任何漂移仍 fail-closed。
    assert item["review_status"] == backlog.REVIEW_STATUS_BOUND
    assert item["ledger"]["entry_valid"] is True

    drifted = ledger_entry_for(repo)

    drifted["reviewed_result"] = dict(drifted["reviewed_result"], result_sha256="0" * 64)

    write_ledger(repo, [drifted], TASK_ID)

    payload = build_backlog(repo)

    item = payload["backlog"][0]

    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert backlog.ISSUE_BACKLOG_ITEM_RESULT_HASH_DRIFT in item["reason_codes"]


# ============================================================
# 5. review_ledger_integrity：台账条目按 facts_ready 核对
# ============================================================


def build_integrity(repo: RepoFS, entries: list[dict[str, Any]]) -> dict[str, Any]:
    write_ledger(repo, entries, TASK_ID)

    return integrity.build_integrity_report(
        root=repo.root,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        ledger_path=repo.ledger,
        adjudication_store_path=repo.store,
        generated_at=AUDIT_TIME,
        manifest_builder=manifest_builder(repo),
    )


def issue_codes(report: dict[str, Any]) -> set[str]:
    return {str(issue["code"]) for issue in report["issues"]}


def test_integrity_without_adjudication_flags_manifest_incomplete(repo: RepoFS) -> None:
    seed_task(repo)

    report = build_integrity(repo, [ledger_entry_for(repo)])

    assert report["summary"]["bound_count"] == 0
    assert integrity.ISSUE_MANIFEST_FACTS_INCOMPLETE in issue_codes(report)


def test_integrity_binds_adjudicated_entry_and_records_fact(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    report = build_integrity(repo, [ledger_entry_for(repo)])

    assert report["summary"]["bound_count"] == 1
    assert report["ledger"]["adjudicated_entry_count"] == 1

    view = report["bindings"][0]

    assert view["bound"] is True
    assert view["manifest_facts_ready"] is True
    assert view["manifest_adjudicated"] is True
    assert view["manifest_adjudication_state"] == adjudication.TASK_STATE_ADJUDICATED


def test_integrity_still_detects_identity_drift_on_adjudicated_task(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    entry = ledger_entry_for(repo)

    entry["reviewed_commit"] = {"sha": COMMIT_SHA, "branch": "drifted-branch"}

    report = build_integrity(repo, [entry])

    assert report["summary"]["bound_count"] == 0
    assert integrity.ISSUE_COMMIT_BRANCH_MISMATCH in issue_codes(report)


def test_invalid_adjudication_is_fail_closed_in_integrity(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo, commit_sha="9" * 40)

    report = build_integrity(repo, [ledger_entry_for(repo)])

    assert report["summary"]["bound_count"] == 0
    assert integrity.ISSUE_MANIFEST_FACTS_INCOMPLETE in issue_codes(report)


# ============================================================
# 6. planner mutation precondition + refill 诊断复用同一裁决事实
# ============================================================


def build_snapshot(repo: RepoFS) -> dict[str, Any]:
    return planner.build_planner_snapshot(
        root=repo.root,
        state_path=repo.state,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        generated_at=AUDIT_TIME,
    )


def build_precondition(repo: RepoFS, snapshot: dict[str, Any]) -> dict[str, Any]:
    backlog_manifest = build_backlog(repo)

    return precondition.build_planner_mutation_precondition(
        root=repo.root,
        state_path=repo.state,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        ledger_path=repo.ledger,
        adjudication_store_path=repo.store,
        generated_at=AUDIT_TIME,
        snapshot=snapshot,
        backlog_builder=lambda: backlog_manifest,
    )


def test_precondition_surfaces_adjudicated_counts(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    snapshot = build_snapshot(repo)

    payload = build_precondition(repo, snapshot)

    pointer = payload["review_backlog"]["pointer"]

    assert pointer["adjudicated_count"] == 1
    assert pointer["adjudicated_pending_count"] == 1
    assert pointer["unresolved_contradiction_count"] == 0
    assert payload["summary"]["review_adjudicated_count"] == 1
    assert payload["authority"]["tool_can_sign_adjudication"] is False


def test_precondition_unresolved_contradiction_has_no_adjudicated_count(repo: RepoFS) -> None:
    seed_task(repo)

    snapshot = build_snapshot(repo)

    payload = build_precondition(repo, snapshot)

    pointer = payload["review_backlog"]["pointer"]

    assert pointer["adjudicated_count"] == 0
    assert pointer["unresolved_contradiction_count"] == 1


def refill_payload(repo: RepoFS, snapshot: dict[str, Any]) -> dict[str, Any]:
    return refill.build_planner_refill_request(
        root=repo.root,
        state_path=repo.state,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        adjudication_store_path=repo.store,
        generated_at=AUDIT_TIME,
        snapshot=snapshot,
    )


def test_refill_diagnostics_report_store_presence_only(repo: RepoFS) -> None:
    seed_task(repo)

    snapshot = build_snapshot(repo)

    payload = refill_payload(repo, snapshot)

    section = payload["completed_but_unreviewed"]

    assert section["count"] == 1
    assert section["adjudication"]["store_present"] is False
    assert section["adjudication"]["declared_count"] == 0
    assert section["adjudication"]["declared_implies_valid"] is False
    assert payload["summary"]["adjudication_declared_count"] == 0
    assert payload["planner_authority"]["tool_can_write_adjudication"] is False


def test_refill_diagnostics_detect_declared_adjudication(repo: RepoFS) -> None:
    seed_task(repo)

    valid_store(repo)

    snapshot = build_snapshot(repo)

    payload = refill_payload(repo, snapshot)

    section = payload["completed_but_unreviewed"]

    assert section["count"] == 1
    assert section["adjudication"]["store_present"] is True
    assert section["adjudication"]["declared_task_ids"] == [TASK_ID]
    assert section["adjudication"]["declared_count"] == 1
    # presence ≠ 有效：有效性仍由 §2.8 / §2.15 判定。
    assert section["adjudication"]["declared_implies_valid"] is False
    assert payload["summary"]["adjudication_declared_count"] == 1


# ============================================================
# 7. 源码守卫：本层绝不暴露 adjudication / ledger / state 写入 API
# ============================================================


@pytest.mark.parametrize(
    "module",
    [binding, backlog, integrity, precondition, refill],
)
def test_read_only_modules_never_expose_write_api(module: Any) -> None:
    public = {name for name in dir(module) if not name.startswith("_")}

    for forbidden in (
        "write_adjudication",
        "append_adjudication",
        "sign_adjudication",
        "write_review_ledger",
        "advance_review_pointer",
        "update_project_state",
    ):
        assert forbidden not in public
