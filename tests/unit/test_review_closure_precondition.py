"""``orchestrator.review_closure_precondition`` 只读原子一致性预检回归（GOLD-045）。

覆盖（与 GOLD-045 requirements / acceptance 一一对应）：

1. 组合事实：HEAD / result+tasks 摘要 / §2.15 裁决校验 / §2.11 backlog / §2.9 integrity /
   候选 PROJECT_STATE 指针事实全部出现在**单一**只读事实包里；
2. 候选输入只来自显式临时文件或 stdin；工具**没有** ``--apply`` / ``--fix`` / ``--advance`` /
   ``--sign`` 等变更开关；源码里没有任何 managed 状态写入路径；
3. fail-closed：候选缺失 / 非法 / schema 不支持 / base 缺失 / stale HEAD / base 摘要漂移 /
   ledger 链断裂 / PASS 越过未裁决矛盾 / 身份漂移 / 非 GPT 裁决 / last_reviewed 倒退或超前 /
   PHASE3_3 blocker 被删除 / Phase 3.4 提前进入 / 交易安全开关变化；
4. ``candidate_ready`` 只表示「候选写集与已提交事实原子一致」，**绝不**等于 review 完成，
   也绝不触发 Executor 写入或自动 push；
5. 确定性 digest（排除 wall-clock）、幂等、Windows / Linux 路径与 zero-write CLI。

红线：本文件只读 —— 绝不写 ``.ai/**``；所有仓库事实都在 ``tmp_path`` 内构造。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import result_terminal_consistency as terminal
from orchestrator import review_backlog as backlog
from orchestrator import review_binding as binding
from orchestrator import review_closure_precondition as precondition
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

AUDIT_TIME = "2026-09-24T00:00:00+08:00"

AUDIT_TIME_OTHER = "2026-09-25T06:00:00+08:00"

BRANCH = "cline-agent"

HEAD_SHA = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"

STALE_HEAD_SHA = "f0e1d2c3b4a5968778695a4b3c2d1e0f12345678"

COMMIT_ONE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

COMMIT_TWO = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

LEGACY_CODE = terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS

LEGACY_FINISHED_AT = "2026-09-23T20:07:56+08:00"

CLEAN_TASKS = ("GOLD-001", "GOLD-002")

PENDING_TASK = "GOLD-003"

MODULE_SOURCE = Path(precondition.__file__).read_text(encoding="utf-8")

# 只读模块源码里绝不出现的写入 / 子进程 / 非确定性痕迹。
FORBIDDEN_SOURCE_SUBSTRINGS = (
    "write_text",
    "write_bytes",
    "mkdir(",
    "rmtree",
    "unlink",
    "os.replace",
    "os.remove",
    "chmod",
    "tempfile",
    "subprocess",
    "check_output",
    "Popen",
    "os.system",
    "json.dump(",
    "hashlib.md5",
)

# 本层绝不暴露的变更 / 签发 API。
FORBIDDEN_MUTATION_API = (
    "apply_candidate",
    "append_adjudication",
    "advance_review_pointer",
    "fix_ledger",
    "sign_adjudication",
    "sign_review",
    "update_project_state",
    "write_adjudication",
    "write_review_ledger",
    "rebase_remote",
    "force_push",
)


def canonical_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def tree_digest(directory: Path) -> str:
    """测试**自己**复算目录 digest（独立于被测模块）。"""

    files = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }

    return canonical_digest(files)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    return file_sha256(path)


def clean_result(task_id: str) -> dict[str, Any]:
    """归一化成功 result（§2.13 判定自洽）。"""

    return {
        "task_id": task_id,
        "status": "completed",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "finished_at": "2026-09-23T20:00:00+08:00",
        "attempts": [
            {
                "attempt": 1,
                "cline_exit_code": 0,
                "cline_timed_out": False,
                "failure_class": "none",
                "failure_code": None,
                "execution_outcome": "completed",
                "normalized_finish_reason": "completed",
                "finish_reason": "completed",
                "cline_finish_reason_raw": "completed",
                "validations": [{"command": "pytest", "returncode": 0, "timed_out": False}],
            }
        ],
    }


def legacy_result(task_id: str) -> dict[str, Any]:
    """历史矛盾形状：``status=completed`` + 最终 attempt ``finish_reason=aborted``。"""

    return {
        "task_id": task_id,
        "status": "completed",
        "finished_at": LEGACY_FINISHED_AT,
        "attempt_count": 1,
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


def base_state(*, last_reviewed: str = "GOLD-001") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": "XAUUSD",
        "branch": BRANCH,
        "phase": "Phase 3",
        "status": "BLOCKED",
        "current_task": PENDING_TASK,
        "last_completed_task": "GOLD-002",
        "last_reviewed_task": last_reviewed,
        "blockers": [
            {"code": precondition.PHASE3_3_BLOCKER, "retryable": False},
            {"code": "GPT_REVIEW_TERMINAL_HISTORY", "retryable": True},
        ],
        "human_gates": [{"code": "PHASE3_3_L3_DECISION"}],
        "invariants": [
            "LIVE_TRADING=false",
            "ALLOW_EXTERNAL_ORDER_SUBMISSION=false",
            "GPT 是唯一 Planner/Reviewer/Architect；Cline/DeepSeek 仅执行已批准任务",
        ],
        "queue_status": "ACTIVE",
        "task_queue": [PENDING_TASK],
    }


COMMIT_BY_TASK = {"GOLD-001": COMMIT_ONE, "GOLD-002": COMMIT_TWO}

BACKLOG_FACTS: dict[str, Any] = {
    "schema": backlog.REVIEW_BACKLOG_SCHEMA,
    "schema_version": backlog.REVIEW_BACKLOG_SCHEMA_VERSION,
    "backlog_digest": "b1" * 32,
    "coverage": {
        "backlog_count": 1,
        "backlog_first": "GOLD-002",
        "backlog_last": "GOLD-002",
        "last_reviewed_task_pointer": "GOLD-001",
        "newest_completed_result": "GOLD-002",
    },
    "reason_codes": [],
    "summary": {
        "adjudicated_count": 0,
        "backlog_count": 1,
        "bound_count": 0,
        "facts_ready_count": 1,
        "unresolved_contradiction_count": 0,
    },
}

INTEGRITY_FACTS: dict[str, Any] = {
    "schema": integrity.LEDGER_INTEGRITY_SCHEMA,
    "schema_version": integrity.LEDGER_INTEGRITY_SCHEMA_VERSION,
    "ledger": {"adjudicated_entry_count": 0},
    "issues": [],
    "summary": {"bound_count": 0, "entry_count": 1, "issue_count": 0, "integrity_ok": True},
}


def empty_ledger() -> dict[str, Any]:
    return {
        "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
        "entries": [],
    }


def ledger_entry(
    task_id: str,
    *,
    result_sha256: str,
    commit_sha: str,
    verdict: str = "PASS",
    reviewer: str = "gpt",
    reviewer_role: str = "GPT",
    finished_at: str = "2026-09-23T20:00:00+08:00",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "verdict": verdict,
        "reviewer_role": reviewer_role,
        "reviewer": reviewer,
        "reviewed_result": {
            "result_sha256": result_sha256,
            "status": "completed",
            "finished_at": finished_at,
        },
        "reviewed_commit": {"sha": commit_sha, "branch": BRANCH},
        "acceptance_summary": f"GPT review {verdict}: {task_id} fixture（只读测试）",
        "reviewed_at": "2026-09-24T00:30:00+08:00",
    }


def candidate_ledger(
    entries: list[dict[str, Any]], *, reviewed_from: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
        "entries": entries,
    }

    if reviewed_from is not None:
        payload["reviewed_from"] = reviewed_from

    return payload


def adjudication_entry(
    task_id: str,
    *,
    result_sha256: str,
    commit_sha: str,
    codes: list[str] | None = None,
    reviewer: str = "gpt",
    reviewer_role: str = "GPT",
) -> dict[str, Any]:
    return {
        "adjudication_id": f"ADJ-{task_id}",
        "task_id": task_id,
        "adjudication_type": adjudication.ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,
        "reviewer": reviewer,
        "reviewer_role": reviewer_role,
        "reason_summary": f"{task_id}: legacy completed+aborted 历史矛盾已由 GPT 裁决",
        "adjudicated_at": "2026-09-23T22:00:00+08:00",
        "bound_result": {"sha256": result_sha256, "status": "completed"},
        "bound_commit": {"sha": commit_sha, "branch": BRANCH},
        "contradiction_reason_codes": list(codes or [LEGACY_CODE]),
    }


def adjudication_store(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": adjudication.STORE_SCHEMA,
        "schema_version": adjudication.STORE_SCHEMA_VERSION,
        "adjudications": entries,
    }



def make_manifest(
    task_id: str,
    *,
    result_sha256: str | None = None,
    status: str | None = None,
    finished_at: str | None = None,
    commit_sha: str | None = None,
    branch: str | None = BRANCH,
    reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """与 §2.8 manifest **同 shape** 的事实子集（零 Git / 零子进程）。"""

    codes = sorted(reason_codes)

    facts_complete = not codes

    return {
        "schema": binding.REVIEW_BINDING_SCHEMA,
        "schema_version": binding.REVIEW_BINDING_SCHEMA_VERSION,
        "task_id": task_id,
        "result": {
            "sha256": result_sha256,
            "status": status,
            "finished_at": finished_at,
            "terminal": status in {"completed", "blocked"},
        },
        "commit": {"sha": commit_sha, "branch": branch, "resolved": commit_sha is not None},
        "adjudication": {"state": None, "valid": None, "adjudication": None, "reason_codes": []},
        "binding": {
            "facts_complete": facts_complete,
            "facts_ready": facts_complete,
            "adjudicated": False,
            "facts_ready_source": (
                binding.FACTS_READY_SOURCE_FACTS
                if facts_complete
                else binding.FACTS_READY_SOURCE_NONE
            ),
            "reason_codes": codes,
        },
        "issues": [],
    }


def default_live(task_id: str, payload: dict[str, Any] | None, sha: str | None) -> dict[str, Any]:
    commit_sha = COMMIT_BY_TASK.get(task_id)

    return {
        "result_path": None,
        "result_sha256": sha,
        "result_status": payload.get("status") if isinstance(payload, dict) else None,
        "result_payload": payload,
        "commit_sha": commit_sha,
        "commit_branch": BRANCH if commit_sha is not None else None,
        "commit_resolved": commit_sha is not None,
    }


@dataclass
class FakeRepo:
    root: Path
    tasks: Path
    results: Path
    state_path: Path
    ledger_path: Path
    state_payload: dict[str, Any]
    manifests: dict[str, dict[str, Any]]
    live: dict[str, dict[str, Any]]

    def snapshot(
        self,
        *,
        head_sha: str = HEAD_SHA,
        state: dict[str, Any] | None = None,
        available: bool = True,
    ) -> dict[str, Any]:
        return {
            "git": {
                "available": available,
                "branch": BRANCH if available else None,
                "detached": False,
                "head": head_sha if available else None,
                "head_short": head_sha[:8] if available else None,
            },
            "issues": [],
            "project_state": self.state_payload if state is None else state,
        }

    def base(self, *, head_sha: str = HEAD_SHA, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "head_sha": head_sha,
            "results_digest": tree_digest(self.results),
            "tasks_digest": tree_digest(self.tasks),
            "project_state_sha256": file_sha256(self.state_path),
            "review_ledger_sha256": file_sha256(self.ledger_path),
            "adjudication_store_sha256": None,
        }

        base.update(overrides)

        return base

    def manifest_builder(self) -> precondition.ManifestBuilder:
        def build(task_id: str) -> dict[str, Any]:
            return self.manifests.get(task_id) or make_manifest(task_id)

        return build

    def live_provider(self, task_id: str) -> dict[str, Any]:
        return self.live[task_id]



def make_repo(
    tmp_path: Path,
    *,
    results: dict[str, dict[str, Any]] | None = None,
    state_payload: dict[str, Any] | None = None,
    committed_ledger: dict[str, Any] | None = None,
    manifests: dict[str, dict[str, Any]] | None = None,
    live: dict[str, dict[str, Any]] | None = None,
) -> FakeRepo:
    """构造只读 fake 仓库（tasks / results / PROJECT_STATE / ledger）。"""

    tasks = tmp_path / "tasks"

    results_dir = tmp_path / "results"

    write_json(tasks / f"{PENDING_TASK}.json", {"task_id": PENDING_TASK, "human_gate": "L1"})

    result_payloads = (
        results
        if results is not None
        else {task_id: clean_result(task_id) for task_id in CLEAN_TASKS}
    )

    result_sha = {
        task_id: write_json(results_dir / f"{task_id}.json", payload)
        for task_id, payload in result_payloads.items()
    }

    state = state_payload if state_payload is not None else base_state()

    state_path = tmp_path / "PROJECT_STATE.json"

    write_json(state_path, state)

    ledger_path = tmp_path / "GPT_REVIEW_LEDGER.json"

    write_json(
        ledger_path, committed_ledger if committed_ledger is not None else empty_ledger()
    )

    derived_manifests: dict[str, dict[str, Any]] = {PENDING_TASK: make_manifest(PENDING_TASK)}

    for task_id, payload in result_payloads.items():
        if manifests is not None and task_id in manifests:
            derived_manifests[task_id] = manifests[task_id]

            continue

        consistent = bool(terminal.analyze_result_terminal_consistency(payload)["consistent"])

        derived_manifests[task_id] = make_manifest(
            task_id,
            result_sha256=result_sha[task_id],
            status=payload.get("status"),
            finished_at=payload.get("finished_at"),
            commit_sha=COMMIT_BY_TASK.get(task_id),
            reason_codes=() if consistent else (LEGACY_CODE,),
        )

    if manifests is not None:
        for task_id, manifest in manifests.items():
            derived_manifests.setdefault(task_id, manifest)

    derived_live = {
        task_id: default_live(task_id, payload, result_sha.get(task_id))
        for task_id, payload in result_payloads.items()
    }

    derived_live[PENDING_TASK] = default_live(PENDING_TASK, None, None)

    if live is not None:
        derived_live.update(live)

    return FakeRepo(
        root=tmp_path,
        tasks=tasks,
        results=results_dir,
        state_path=state_path,
        ledger_path=ledger_path,
        state_payload=state,
        manifests=derived_manifests,
        live=derived_live,
    )


def make_candidate(
    repo: FakeRepo,
    *,
    project_state: dict[str, Any] | None = None,
    review_ledger: dict[str, Any] | None = None,
    adjudication_store_payload: dict[str, Any] | None = None,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": precondition.CANDIDATE_SCHEMA,
        "schema_version": precondition.CANDIDATE_SCHEMA_VERSION,
        "base": base if base is not None else repo.base(),
        "adjudication_store": adjudication_store_payload,
        "review_ledger": review_ledger,
        "project_state": project_state,
    }


def run_precondition(
    repo: FakeRepo,
    candidate: dict[str, Any] | None,
    *,
    head_sha: str = HEAD_SHA,
    state_payload: dict[str, Any] | None = None,
    head_available: bool = True,
    generated_at: str = AUDIT_TIME,
    as_of: str | None = AUDIT_TIME,
    candidate_source: str = "<tmp>/candidate.json",
    candidate_issues: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    return precondition.build_review_closure_precondition(
        candidate=candidate,
        candidate_source=candidate_source,
        candidate_issues=candidate_issues,
        root=repo.root,
        state_path=repo.state_path,
        tasks_dir=repo.tasks,
        results_dir=repo.results,
        ledger_path=repo.ledger_path,
        generated_at=generated_at,
        as_of=as_of,
        snapshot=repo.snapshot(head_sha=head_sha, state=state_payload, available=head_available),
        backlog_builder=lambda: BACKLOG_FACTS,
        integrity_builder=lambda: INTEGRITY_FACTS,
        manifest_builder=repo.manifest_builder(),
        live_facts_provider=repo.live_provider,
    )


def ready_candidate(repo: FakeRepo) -> dict[str, Any]:
    """一份与已提交事实原子一致的候选（review GOLD-002）。"""

    payload = repo.results / "GOLD-002.json"

    entry = ledger_entry(
        "GOLD-002",
        result_sha256=file_sha256(payload),
        commit_sha=COMMIT_TWO,
    )

    candidate_state = dict(repo.state_payload)

    candidate_state["last_reviewed_task"] = "GOLD-002"

    return make_candidate(
        repo,
        project_state=candidate_state,
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
    )


def codes_of(payload: dict[str, Any]) -> list[str]:
    return list(payload["reason_codes"])



# ============================================================
# 1. 契约 / 职责边界 / 零写入源码守卫
# ============================================================


def test_schema_and_read_only_contract(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, ready_candidate(repo))

    assert payload["schema"] == precondition.PRECONDITION_SCHEMA
    assert payload["schema_version"] == precondition.PRECONDITION_SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["precondition_kind"] == "review_closure_precondition"
    assert payload["candidate"]["schema"] == precondition.CANDIDATE_SCHEMA
    assert payload["candidate"]["schema_version"] == precondition.CANDIDATE_SCHEMA_VERSION

    determinism = payload["determinism"]

    for key in (
        "creates_files",
        "writes_adjudication_store",
        "writes_project_state",
        "writes_results",
        "writes_review_ledger",
        "writes_tasks",
        "network_access",
        "model_calls",
        "wall_clock_in_content_identity",
    ):
        assert determinism[key] is False

    assert "generated_at" in determinism["digest_excluded_keys"]


def test_authority_forbids_every_mutation(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, ready_candidate(repo))

    authority = payload["authority"]

    assert authority["schema"] == precondition.AUTHORITY_SCHEMA
    assert authority["review_authority"] == "gpt_only"
    assert authority["mutation_switches_exposed"] == []
    assert authority["candidate_ready_is_not_review_verdict"] is True
    assert authority["candidate_ready_triggers_executor_write"] is False
    assert authority["candidate_ready_triggers_auto_push"] is False
    assert authority["candidate_ready_lifts_phase_gate"] is False

    for key in (
        "tool_can_advance_pointer",
        "tool_can_advance_state",
        "tool_can_apply_candidate",
        "tool_can_decide_phase",
        "tool_can_lift_blocker",
        "tool_can_sign_adjudication",
        "tool_can_sign_review",
        "tool_can_update_project_state",
        "tool_can_write_adjudication",
        "tool_can_write_results",
        "tool_can_write_review_ledger",
        "tool_can_write_tasks",
    ):
        assert authority[key] is False


def test_source_has_no_write_or_mutation_paths() -> None:
    for needle in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert needle not in MODULE_SOURCE, needle

    public = {name for name in dir(precondition) if not name.startswith("_")}

    for name in FORBIDDEN_MUTATION_API:
        assert name not in public, name


def test_parser_exposes_no_mutation_switches() -> None:
    parser = precondition.build_parser()

    assert parser.parse_args(["--candidate", "-"]).candidate == "-"

    for flag in ("--apply", "--fix", "--advance", "--sign", "--write", "--push"):
        with pytest.raises(SystemExit):
            parser.parse_args([flag])



# ============================================================
# 2. candidate-ready：组合事实 / 零写入 / 确定性语义
# ============================================================


def test_candidate_ready_is_not_review_completed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, ready_candidate(repo))

    assert payload["candidate_ready"] is True
    assert payload["reason_codes"] == []
    assert payload["issues"] == []
    assert payload["summary"]["exit_code"] == precondition.EXIT_OK

    closure = payload["closure"]

    assert closure["review_completed"] is False
    assert closure["review_verdict_issued"] is False
    assert closure["phase_gate_lifted"] is False
    assert closure["executor_write_triggered"] is False
    assert closure["state_advanced"] is False
    assert closure["auto_push"] is False
    assert closure["committed_current"]["head_resolved"] is True
    assert closure["committed_current"]["project_state_readable"] is True


def test_fact_bundle_combines_committed_facts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, ready_candidate(repo))

    committed = payload["committed_current"]

    assert payload["head"]["observed_head_sha"] == HEAD_SHA
    assert committed["tasks_digest"]["digest"] == tree_digest(repo.tasks)
    assert committed["results_digest"]["digest"] == tree_digest(repo.results)
    assert committed["project_state"]["blob_sha256"] == file_sha256(repo.state_path)
    assert committed["review_ledger"]["blob_sha256"] == file_sha256(repo.ledger_path)
    assert committed["adjudication_store"]["present"] is False
    assert committed["review_backlog"]["available"] is True
    assert committed["review_integrity"]["available"] is True
    assert committed["review_backlog"]["source"].endswith("build_review_backlog_manifest")
    assert committed["review_integrity"]["source"].endswith("build_integrity_report")

    binding_tasks = committed["review_binding"]["tasks"]

    assert [task["task_id"] for task in binding_tasks] == ["GOLD-002", "GOLD-003"]

    candidate = payload["candidate"]

    assert candidate["review_ledger"]["reviewed_ids"] == ["GOLD-002"]
    assert candidate["project_state"]["last_reviewed_task"] == "GOLD-002"
    assert candidate["adjudication"]["store"]["present"] is False
    assert candidate["sections"] == {
        "adjudication_store_present": False,
        "project_state_present": True,
        "review_ledger_present": True,
    }


def test_precondition_is_zero_write(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    before = (
        tree_digest(repo.tasks),
        tree_digest(repo.results),
        file_sha256(repo.state_path),
        file_sha256(repo.ledger_path),
    )

    run_precondition(repo, ready_candidate(repo))

    after = (
        tree_digest(repo.tasks),
        tree_digest(repo.results),
        file_sha256(repo.state_path),
        file_sha256(repo.ledger_path),
    )

    assert after == before
    assert not (repo.root / ".ai").exists()


def test_deterministic_digest_excludes_wall_clock(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    first = run_precondition(repo, candidate, generated_at=AUDIT_TIME)
    second = run_precondition(repo, candidate, generated_at=AUDIT_TIME_OTHER)

    assert first["precondition_digest"] == second["precondition_digest"]
    assert first["generated_at"] != second["generated_at"]


def test_precondition_digest_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    assert (
        run_precondition(repo, candidate)["precondition_digest"]
        == run_precondition(repo, candidate)["precondition_digest"]
    )



# ============================================================
# 3. fail-closed：候选输入 / base / HEAD
# ============================================================


def test_candidate_missing_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, None)

    assert precondition.REASON_CANDIDATE_MISSING in codes_of(payload)
    assert payload["summary"]["exit_code"] == precondition.EXIT_INPUT_UNAVAILABLE
    assert payload["candidate_ready"] is False
    assert payload["closure"]["review_completed"] is False


def test_candidate_json_invalid_fails_closed() -> None:
    payload, issues = precondition.parse_candidate_text("{not json")

    assert payload is None
    assert [issue["code"] for issue in issues] == [precondition.REASON_CANDIDATE_JSON_INVALID]


def test_candidate_not_object_fails_closed() -> None:
    payload, issues = precondition.parse_candidate_text("[1, 2, 3]")

    assert payload is None
    assert [issue["code"] for issue in issues] == [precondition.REASON_CANDIDATE_NOT_OBJECT]


def test_candidate_unreadable_file_fails_closed(tmp_path: Path) -> None:
    text, issues = precondition.load_candidate_file(tmp_path / "missing.json")

    assert text is None
    assert [issue["code"] for issue in issues] == [precondition.REASON_CANDIDATE_UNREADABLE]


def test_candidate_schema_unsupported_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["schema"] = "gold-ai/review-closure-candidate/v99"

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_CANDIDATE_SCHEMA_UNSUPPORTED in codes_of(payload)
    assert payload["summary"]["exit_code"] == precondition.EXIT_INPUT_UNAVAILABLE


def test_candidate_base_missing_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate.pop("base")

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_CANDIDATE_BASE_MISSING in codes_of(payload)
    assert payload["candidate"]["base"] is None
    assert payload["candidate_ready"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_candidate_section_invalid_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["review_ledger"] = ["not", "an", "object"]

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_CANDIDATE_SECTION_INVALID in codes_of(payload)


def test_stale_remote_head_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["base"] = repo.base(head_sha=STALE_HEAD_SHA)

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_STALE_REMOTE_HEAD in codes_of(payload)
    assert payload["summary"]["stale_remote_head"] is True
    assert payload["base"]["matches_expected"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_short_head_prefix_matches(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["base"] = repo.base(head_sha=HEAD_SHA[:8])

    payload = run_precondition(repo, candidate)

    assert payload["base"]["matches_expected"] is True
    assert payload["candidate_ready"] is True


def test_unresolvable_head_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    payload = run_precondition(repo, ready_candidate(repo), head_available=False)

    assert planner.ISSUE_GIT_INFO_UNAVAILABLE in codes_of(payload)
    assert payload["head"]["resolved"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_base_summary_drift_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["base"] = repo.base(results_digest="0" * 64)

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_BASE_RESULTS_DRIFT in codes_of(payload)

    candidate_state = ready_candidate(repo)

    candidate_state["base"] = repo.base(project_state_sha256="1" * 64)

    payload_state = run_precondition(repo, candidate_state)

    assert precondition.REASON_BASE_STATE_DRIFT in codes_of(payload_state)

    candidate_ledger = ready_candidate(repo)

    candidate_ledger["base"] = repo.base(review_ledger_sha256="2" * 64)

    payload_ledger = run_precondition(repo, candidate_ledger)

    assert precondition.REASON_BASE_LEDGER_DRIFT in codes_of(payload_ledger)


def test_invalid_expected_head_form_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    candidate["base"] = repo.base(head_sha="zzz-not-hex")

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_CANDIDATE_BASE_INVALID in codes_of(payload)



# ============================================================
# 4. fail-closed：ledger 连续性 / 矛盾 / 身份 / 删除
# ============================================================


def advanced_state(repo: FakeRepo, *, last_reviewed: str = "GOLD-002") -> dict[str, Any]:
    state = dict(repo.state_payload)

    state["last_reviewed_task"] = last_reviewed

    return state


def test_ledger_chain_gap_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    entry = ledger_entry(
        "GOLD-002",
        result_sha256=file_sha256(repo.results / "GOLD-002.json"),
        commit_sha=COMMIT_TWO,
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-001"),
    )

    payload = run_precondition(repo, candidate)

    assert integrity.ISSUE_LEDGER_CHAIN_GAP in codes_of(payload)
    assert payload["candidate"]["review_ledger"]["chain"]["missing_in_window"] == ["GOLD-001"]
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_pass_past_unresolved_contradiction_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        results={"GOLD-001": clean_result("GOLD-001"), "GOLD-002": legacy_result("GOLD-002")},
    )

    entry = ledger_entry(
        "GOLD-002",
        result_sha256=file_sha256(repo.results / "GOLD-002.json"),
        commit_sha=COMMIT_TWO,
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_PASS_PAST_UNRESOLVED_CONTRADICTION in codes_of(payload)
    assert not payload["candidate_ready"]


def test_valid_gpt_adjudication_restores_candidate_ready(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        results={"GOLD-001": clean_result("GOLD-001"), "GOLD-002": legacy_result("GOLD-002")},
    )

    sha = file_sha256(repo.results / "GOLD-002.json")

    entry = ledger_entry(
        "GOLD-002", result_sha256=sha, commit_sha=COMMIT_TWO, finished_at=LEGACY_FINISHED_AT
    )

    store = adjudication_store(
        [adjudication_entry("GOLD-002", result_sha256=sha, commit_sha=COMMIT_TWO)]
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
        adjudication_store_payload=store,
    )

    payload = run_precondition(repo, candidate)

    assert payload["reason_codes"] == []
    assert payload["candidate_ready"] is True
    assert payload["candidate"]["adjudication"]["store"]["present"] is True

    tasks = payload["candidate"]["adjudication"]["tasks"]

    assert [task["task_id"] for task in tasks] == ["GOLD-002", "GOLD-003"]
    assert tasks[0]["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert tasks[0]["reviewer_role"] == "GPT"
    # 候选通过 ≠ review 完成：closure 语义一字不放宽。
    assert payload["closure"]["review_completed"] is False



def test_non_gpt_adjudication_rejected(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        results={"GOLD-001": clean_result("GOLD-001"), "GOLD-002": legacy_result("GOLD-002")},
    )

    sha = file_sha256(repo.results / "GOLD-002.json")

    entry = ledger_entry(
        "GOLD-002", result_sha256=sha, commit_sha=COMMIT_TWO, finished_at=LEGACY_FINISHED_AT
    )

    store = adjudication_store(
        [
            adjudication_entry(
                "GOLD-002",
                result_sha256=sha,
                commit_sha=COMMIT_TWO,
                reviewer="cline",
                reviewer_role="Cline",
            )
        ]
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
        adjudication_store_payload=store,
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_ADJUDICATION_NOT_GPT in codes_of(payload)
    assert precondition.REASON_ADJUDICATION_INVALID in codes_of(payload)
    assert not payload["candidate_ready"]


def test_candidate_adjudication_identity_drift_rejected(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        results={"GOLD-001": clean_result("GOLD-001"), "GOLD-002": legacy_result("GOLD-002")},
    )

    sha = file_sha256(repo.results / "GOLD-002.json")

    store = adjudication_store(
        [adjudication_entry("GOLD-002", result_sha256="0" * 64, commit_sha=COMMIT_TWO)]
    )

    entry = ledger_entry(
        "GOLD-002", result_sha256=sha, commit_sha=COMMIT_TWO, finished_at=LEGACY_FINISHED_AT
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
        adjudication_store_payload=store,
    )

    payload = run_precondition(repo, candidate)

    assert adjudication.ISSUE_RESULT_SHA256_DRIFT in codes_of(payload)
    assert not payload["candidate_ready"]


def test_candidate_ledger_result_hash_drift_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    entry = ledger_entry("GOLD-002", result_sha256="0" * 64, commit_sha=COMMIT_TWO)

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert integrity.ISSUE_RESULT_HASH_MISMATCH in codes_of(payload)
    assert not payload["candidate_ready"]


def test_committed_ledger_entry_removal_rejected(tmp_path: Path) -> None:
    committed = candidate_ledger(
        [ledger_entry("GOLD-001", result_sha256="a" * 64, commit_sha=COMMIT_ONE)],
        reviewed_from="GOLD-001",
    )

    repo = make_repo(tmp_path, committed_ledger=committed)

    entry = ledger_entry(
        "GOLD-002",
        result_sha256=file_sha256(repo.results / "GOLD-002.json"),
        commit_sha=COMMIT_TWO,
    )

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([entry], reviewed_from="GOLD-001"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_LEDGER_ENTRY_REMOVED in codes_of(payload)
    assert payload["candidate"]["review_ledger"]["removed_task_ids"] == ["GOLD-001"]



# ============================================================
# 5. fail-closed：PROJECT_STATE 指针 / blocker / Phase / 交易安全
# ============================================================


def reviewed_entry(repo: FakeRepo) -> dict[str, Any]:
    return ledger_entry(
        "GOLD-002",
        result_sha256=file_sha256(repo.results / "GOLD-002.json"),
        commit_sha=COMMIT_TWO,
    )


def test_last_reviewed_regression_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, state_payload=base_state(last_reviewed="GOLD-002"))

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo, last_reviewed="GOLD-001"),
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_LAST_REVIEWED_REGRESSION in codes_of(payload)
    assert not payload["candidate_ready"]


def test_last_reviewed_ahead_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo, last_reviewed=PENDING_TASK),
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_LAST_REVIEWED_AHEAD in codes_of(payload)
    assert not payload["candidate_ready"]


def test_last_reviewed_without_ledger_entry_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate = make_candidate(
        repo,
        project_state=advanced_state(repo),
        review_ledger=candidate_ledger([], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_LAST_REVIEWED_AHEAD in codes_of(payload)
    assert payload["candidate"]["review_ledger"]["reviewed_ids"] == []


def test_phase3_3_blocker_removal_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate_state = advanced_state(repo)

    candidate_state["blockers"] = [{"code": "GPT_REVIEW_TERMINAL_HISTORY", "retryable": True}]

    candidate = make_candidate(
        repo,
        project_state=candidate_state,
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_PHASE3_3_BLOCKER_REMOVED in codes_of(payload)
    assert not payload["candidate_ready"]


def test_other_blocker_removal_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate_state = advanced_state(repo)

    candidate_state["blockers"] = [{"code": precondition.PHASE3_3_BLOCKER, "retryable": False}]

    candidate = make_candidate(
        repo,
        project_state=candidate_state,
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_BLOCKER_REMOVED in codes_of(payload)


def test_phase3_4_entry_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate_state = advanced_state(repo)

    candidate_state["phase"] = "Phase 3.4"

    candidate = make_candidate(
        repo,
        project_state=candidate_state,
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_PHASE3_4_ENTRY in codes_of(payload)
    assert payload["candidate"]["project_state"]["phase_rank"] == [3, 4]
    assert not payload["candidate_ready"]


def test_trading_safety_invariant_change_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    candidate_state = advanced_state(repo)

    candidate_state["invariants"] = ["ALLOW_EXTERNAL_ORDER_SUBMISSION=false"]

    candidate = make_candidate(
        repo,
        project_state=candidate_state,
        review_ledger=candidate_ledger([reviewed_entry(repo)], reviewed_from="GOLD-002"),
    )

    payload = run_precondition(repo, candidate)

    assert precondition.REASON_TRADING_SAFETY_CHANGED in codes_of(payload)
    assert not payload["candidate_ready"]


def test_last_completed_task_aligned_with_blocked_at_end_not_drift(tmp_path: Path) -> None:
    """blocked result 排在最后时，``last_completed_task`` 不得被误判为「落后于 results」。

    GOLD-048 回归：``pointer_section`` 曾把「最新 terminal（含 blocked）」当作
    ``last_completed_task`` 的对齐基准，导致 blocked result 排在最后时，正确指向最新
    completed result 的候选被误判为 ``CANDIDATE_STATE_POINTER_DRIFT``。
    """

    tasks_dir = tmp_path / "tasks"

    tasks_dir.mkdir()

    for task_id in ("GOLD-001", "GOLD-002", "GOLD-003", "GOLD-004", "GOLD-005"):
        (tasks_dir / f"{task_id}.json").write_text("{}", encoding="utf-8")

    statuses = {
        "GOLD-001": "completed",
        "GOLD-002": "completed",
        "GOLD-003": "blocked",
        "GOLD-004": "blocked",
    }

    candidate_state = {
        "current_task": "GOLD-005",
        "last_completed_task": "GOLD-002",
        "last_reviewed_task": "GOLD-001",
    }

    issues = precondition.candidate_pointer_drift_issues(
        candidate_state, statuses=statuses, tasks_dir=tasks_dir
    )

    assert issues == []


def test_last_completed_task_behind_completed_result_drifts(tmp_path: Path) -> None:
    """``last_completed_task`` 落后于最新 completed result 仍是真实漂移（fail-closed）。"""

    tasks_dir = tmp_path / "tasks"

    tasks_dir.mkdir()

    for task_id in ("GOLD-001", "GOLD-002", "GOLD-003"):
        (tasks_dir / f"{task_id}.json").write_text("{}", encoding="utf-8")

    statuses = {"GOLD-001": "completed", "GOLD-002": "completed"}

    candidate_state = {
        "current_task": "GOLD-003",
        "last_completed_task": "GOLD-001",
        "last_reviewed_task": "GOLD-001",
    }

    issues = precondition.candidate_pointer_drift_issues(
        candidate_state, statuses=statuses, tasks_dir=tasks_dir
    )

    codes = {issue["code"] for issue in issues}

    assert precondition.REASON_STATE_POINTER_DRIFT in codes
    assert any("last_completed_task" in issue["detail"] for issue in issues)


def test_phase_rank_parsing() -> None:
    assert precondition.phase_rank("Phase 3") == (3, 0)
    assert precondition.phase_rank("phase 3.4") == (3, 4)
    assert precondition.phase_rank("Phase 4") == (4, 0)
    assert precondition.phase_rank("Phase Three") is None
    assert precondition.phase_rank(None) is None



# ============================================================
# 6. 候选来源守卫（临时文件 / stdin / Windows+Linux 路径）与零写入 CLI
# ============================================================


def test_resolve_candidate_source_accepts_stdin_tokens(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    assert precondition.resolve_candidate_source(root, "-") == ("stdin", None)
    assert precondition.resolve_candidate_source(root, "stdin") == ("stdin", None)
    assert precondition.resolve_candidate_source(root, "  -  ") == ("stdin", None)


def test_resolve_candidate_source_accepts_temp_and_runtime(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    runtime = root / ".ai" / "runtime" / "candidate.json"

    source, reason = precondition.resolve_candidate_source(root, runtime)

    assert reason is None
    assert source == str(runtime.resolve())

    relative = Path(".ai") / "runtime" / "candidate.json"

    source_rel, reason_rel = precondition.resolve_candidate_source(root, relative)

    assert reason_rel is None
    assert source_rel == str((root / relative).resolve())

    assert snapshot_output.is_within(Path(source_rel), (root / ".ai" / "runtime").resolve())


def test_resolve_candidate_source_rejects_managed_and_escape_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    managed = (
        Path(".ai") / "PROJECT_STATE.json",
        Path(".ai") / "GPT_REVIEW_LEDGER.json",
        Path(".ai") / "tasks" / "GOLD-001.json",
        Path(".ai") / "results" / "GOLD-001.json",
        Path(".ai") / "adjudications" / "legacy_result_adjudications.json",
        Path(".ai") / "runtime" / ".." / "tasks" / "GOLD-001.json",
    )

    for relative in managed:
        source, reason = precondition.resolve_candidate_source(root, relative)

        assert source is None, relative
        assert reason, relative


def test_resolve_candidate_source_requires_temp_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_temp = tmp_path / "temp"

    fake_temp.mkdir()

    root = tmp_path / "repo"

    monkeypatch.setattr(snapshot_output, "temp_directory", lambda: fake_temp)

    source, reason = precondition.resolve_candidate_source(root, root / "candidate.json")

    assert source is None
    assert reason

    good, reason_good = precondition.resolve_candidate_source(root, fake_temp / "c.json")

    assert reason_good is None
    assert good == str((fake_temp / "c.json").resolve())


def test_cli_stdin_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(tmp_path)

    before = (
        tree_digest(repo.tasks),
        tree_digest(repo.results),
        file_sha256(repo.state_path),
        file_sha256(repo.ledger_path),
    )

    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))

    exit_code = precondition.main(
        [
            "--root",
            str(repo.root),
            "--state",
            str(repo.state_path),
            "--tasks-dir",
            str(repo.tasks),
            "--results-dir",
            str(repo.results),
            "--ledger",
            str(repo.ledger_path),
            "--candidate",
            "-",
            "--generated-at",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    payload = json.loads(captured.out)

    assert exit_code == precondition.EXIT_INPUT_UNAVAILABLE
    assert precondition.REASON_CANDIDATE_JSON_INVALID in payload["reason_codes"]
    assert payload["candidate_source"] == "stdin"
    assert captured.out.isascii()

    after = (
        tree_digest(repo.tasks),
        tree_digest(repo.results),
        file_sha256(repo.state_path),
        file_sha256(repo.ledger_path),
    )

    assert after == before
    assert not (repo.root / ".ai").exists()


def test_cli_output_rejects_managed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    exit_code = precondition.main(
        [
            "--root",
            str(repo.root),
            "--candidate",
            "-",
            "--output",
            str(repo.root / ".ai" / "PROJECT_STATE.json"),
        ]
    )

    assert exit_code == snapshot_output.EXIT_OUTPUT_REJECTED
    assert not (repo.root / ".ai").exists()


def test_cli_output_writes_only_inside_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(tmp_path)

    candidate = ready_candidate(repo)

    target = repo.root / ".ai" / "runtime" / "precondition.json"

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(candidate)))

    exit_code = precondition.main(
        [
            "--root",
            str(repo.root),
            "--state",
            str(repo.state_path),
            "--tasks-dir",
            str(repo.tasks),
            "--results-dir",
            str(repo.results),
            "--ledger",
            str(repo.ledger_path),
            "--candidate",
            "-",
            "--generated-at",
            AUDIT_TIME,
            "--as-of",
            AUDIT_TIME,
            "--output",
            str(target),
        ]
    )

    captured = capsys.readouterr()

    assert captured.out == ""
    assert target.exists()

    written = json.loads(target.read_text(encoding="utf-8"))

    assert written["schema"] == precondition.PRECONDITION_SCHEMA
    assert written["candidate_source"] == "stdin"
    assert exit_code == written["summary"]["exit_code"]

