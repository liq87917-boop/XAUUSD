"""GOLD-025：GPT Review Ledger 与状态推进一致性门禁的回归测试。

覆盖：

1) 合法 reviewed chain：completed + GPT PASS + 指针一致 + queue 首任务依赖已 reviewed
   ⇒ 零 issue、门禁允许推进、退出码 0；
2) completed-but-unreviewed（Executor 不得凭 completed 自称 reviewed）；
3) review identity：result 被替换 / 改写、commit sha 非法、branch 不符、可达性不可验证；
4) 状态漂移：``last_reviewed_task`` 超前于 completed result、落后于 ledger（指针倒退）、
   缺失、FAIL review 不得推进；
5) queue 首任务依赖：未 completed / blocked / 缺失 / 已 completed 但未 GPT-reviewed；
6) L3/L4 Human Gate 永不自动跨越（门禁 fail-closed）；
7) 只读性与确定性：报告前后工作树逐字节不变、历史 result / PROJECT_STATE / ledger 不被改写、
   facts digest 可复算；
8) 职责边界：只有 GPT 能签发 review，Executor / 未知身份 fail-closed；模块无写入路径、
   无 follow-on planning API，唯一子进程是只读 ``git merge-base --is-ancestor``（默认关闭）。

所有测试只在 ``tmp_path`` 内构造文件，绝不对真实仓库做任何写操作。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner
from orchestrator import review_ledger as review

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-24T00:00:00+08:00"

BRANCH = "cline-agent"

FAKE_HEAD = "3f1a9c8e7b6d5f4a3b2c1d0e9f8a7b6c5d4e3f21"

RESULT_FINISHED_AT = "2026-09-23T13:50:47+08:00"

REVIEWED_AT = "2026-09-23T14:10:00+08:00"

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULE_SOURCE = Path(review.__file__).read_text(encoding="utf-8")

# Executor 侧绝不存在的规划 / 状态写入 API。
FORBIDDEN_PLANNING_API = {
    "generate_follow_on_task",
    "refill_rolling_queue",
    "write_final_result",
    "write_task_state",
    "update_project_state",
    "modify_project_state",
    "commit_task_result",
    "reset_task_changes",
    "process_task",
    "restore_recovery_snapshot",
}


def seed_fake_git(root: Path, branch: str = BRANCH, head: str = FAKE_HEAD) -> None:
    """构造只读 Git 事实（``.git/HEAD`` + loose ref），不依赖机器上的真实仓库。"""

    git_dir = root / ".git"

    refs = git_dir / "refs" / "heads"

    refs.mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    (refs / branch).write_text(f"{head}\n", encoding="utf-8")


@dataclass(frozen=True)
class ReviewFS:
    root: Path
    tasks: Path
    results: Path
    state_path: Path
    ledger_path: Path


@pytest.fixture()
def review_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewFS:
    """把 tasks / results / PROJECT_STATE / ledger 全部重定向到 tmp_path。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"
    tasks.mkdir()
    results.mkdir()
    state_path = tmp_path / "PROJECT_STATE.json"
    ledger_path = tmp_path / "GPT_REVIEW_LEDGER.json"

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(review, "PROJECT_STATE_PATH", state_path)
    monkeypatch.setattr(review, "ROOT", tmp_path)

    seed_fake_git(tmp_path)

    return ReviewFS(
        root=tmp_path,
        tasks=tasks,
        results=results,
        state_path=state_path,
        ledger_path=ledger_path,
    )


def write_task(tasks: Path, task_id: str, **extra: object) -> Path:
    path = tasks / f"{task_id}.json"

    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )

    return path


def write_result(
    results: Path,
    task_id: str,
    status: str = "completed",
    finished_at: str = RESULT_FINISHED_AT,
    **extra: object,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "finished_at": finished_at,
        **extra,
    }

    (results / f"{task_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    return payload


def result_sha256(results: Path, task_id: str) -> str:
    return hashlib.sha256((results / f"{task_id}.json").read_bytes()).hexdigest()


def ledger_entry(
    task_id: str,
    results: Path,
    verdict: str = review.VERDICT_PASS,
    reviewer: str = "GPT",
    reviewer_role: str = review.REVIEWER_ROLE,
    acceptance_summary: str = "validation 全绿；acceptance 满足，无未来数据泄漏",
    commit_sha: str = FAKE_HEAD,
    branch: str = BRANCH,
    reviewed_at: str = REVIEWED_AT,
    finished_at: str = RESULT_FINISHED_AT,
    **extra: object,
) -> dict[str, Any]:
    """构造一条**合法** ledger 条目（默认绑定当前 result 文件的 sha256）。"""

    entry: dict[str, Any] = {
        "task_id": task_id,
        "verdict": verdict,
        "reviewer": reviewer,
        "reviewer_role": reviewer_role,
        "acceptance_summary": acceptance_summary,
        "reviewed_result": {
            "result_sha256": result_sha256(results, task_id),
            "status": "completed",
            "finished_at": finished_at,
        },
        "reviewed_commit": {"sha": commit_sha, "branch": branch},
        "reviewed_at": reviewed_at,
    }

    entry.update(extra)

    return entry


def write_ledger(ledger_path: Path, entries: list[dict[str, Any]], **overrides: object) -> None:
    payload: dict[str, Any] = {
        "schema": review.REVIEW_LEDGER_SCHEMA,
        "schema_version": review.REVIEW_LEDGER_SCHEMA_VERSION,
        "project": "XAUUSD",
        "reviewer": "GPT",
        "entries": entries,
    }

    payload.update(overrides)

    write_raw_ledger(ledger_path, payload)


def write_raw_ledger(ledger_path: Path, payload: Any) -> None:
    """写入任意（可能非法的）ledger 内容，用于 fail-closed 校验测试。"""

    ledger_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def baseline_state(**overrides: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": 1,
        "project": "XAUUSD",
        "branch": BRANCH,
        "phase": "Phase 3",
        "status": "BLOCKED",
        "current_task": "GOLD-022",
        "last_completed_task": "GOLD-021",
        "last_reviewed_task": "GOLD-021",
        "blockers": [{"code": "PHASE3_3_DATA", "detail": "真实授权证据不足"}],
        "human_gates": [{"code": "PHASE3_3_AUTHORIZED_EVIDENCE", "detail": "人工证据"}],
        "invariants": [
            "LIVE_TRADING=false",
            "ALLOW_EXTERNAL_ORDER_SUBMISSION=false",
        ],
        "queue_target_size": 3,
        "task_queue": ["GOLD-022"],
        "queue_status": "ACTIVE",
    }

    state.update(overrides)

    return state


def build_report(fs: ReviewFS, **overrides: object) -> dict[str, Any]:
    """带固定 generated_at 的只读报告（显式传入路径，便于断言）。"""

    params: dict[str, Any] = {
        "root": fs.root,
        "state_path": fs.state_path,
        "tasks_dir": fs.tasks,
        "results_dir": fs.results,
        "ledger_path": fs.ledger_path,
        "generated_at": AUDIT_TIME,
    }

    params.update(overrides)

    return review.build_review_report(**params)


def issue_codes(report: dict[str, Any]) -> list[str]:
    return [str(issue["code"]) for issue in report["issues"]]


def issue_details(report: dict[str, Any], code: str) -> str:
    return "\n".join(str(issue["detail"]) for issue in report["issues"] if issue["code"] == code)


def tree_digest(root: Path) -> dict[str, str]:
    """目录内全部文件的 sha256（用于证明工具是只读的）。"""

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def seed_reviewed_project(fs: ReviewFS, head_gate: str | None = None) -> None:
    """GOLD-021 已 completed + GPT PASS；GOLD-022 是队列首任务。"""

    write_task(fs.tasks, "GOLD-021")

    head_extra: dict[str, object] = {"depends_on": ["GOLD-021"]}

    if head_gate is not None:
        head_extra["human_gate"] = head_gate

    write_task(fs.tasks, "GOLD-022", **head_extra)

    write_result(fs.results, "GOLD-021", "completed")

    write_state(
        fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
        ),
    )

    write_ledger(fs.ledger_path, [ledger_entry("GOLD-021", fs.results)])


# ============================================================
# 1. 合法 reviewed chain
# ============================================================


def test_valid_reviewed_chain_allows_advance(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    report = build_report(review_fs)

    assert report["schema"] == review.REVIEW_REPORT_SCHEMA
    assert report["schema_version"] == review.REVIEW_REPORT_SCHEMA_VERSION == 1
    assert report["read_only"] is True
    assert report["issues"] == []
    assert report["summary"] == {
        "issue_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "has_drift": False,
        "advance_allowed": True,
        "exit_code": review.EXIT_OK,
    }
    assert report["gate"]["advance_allowed"] is True
    assert report["gate"]["blocking_codes"] == []
    assert report["gate"]["executor_may_advance_on_its_own"] is False
    assert report["ledger"]["available"] is True
    assert report["ledger"]["pass_reviews"] == ["GOLD-021"]
    assert report["ledger"]["coverage_floor"] == "GOLD-021"
    assert report["pointer"]["entry_present"] is True
    assert report["pointer"]["entry_verdict"] == review.VERDICT_PASS
    assert report["queue"]["head"] == "GOLD-022"
    assert report["queue"]["head_dependencies"] == {"GOLD-021": "completed"}
    assert report["queue"]["head_dependencies_satisfied"] is True
    assert report["queue"]["head_dependencies_reviewed"] is True
    assert report["queue"]["head_human_gate"] is None
    assert report["git"]["branch"] == BRANCH
    assert report["git"]["head"] == FAKE_HEAD
    assert report["ancestry"]["enabled"] is False
    assert json.loads(review.render_review_report(report)) == report


def test_report_is_deterministic_and_wall_clock_excluded(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    first = build_report(review_fs)

    assert build_report(review_fs) == first

    assert build_report(review_fs, generated_at=AUDIT_TIME_LATER)["facts_digest"] == first[
        "facts_digest"
    ]

    assert first["determinism"]["wall_clock_in_facts"] is False
    assert first["determinism"]["digest_field"] == "facts_digest"
    assert review.review_report_facts_digest(first) == first["facts_digest"]


def test_facts_digest_tracks_review_state(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    first = build_report(review_fs)

    write_state(
        review_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task=None,
            task_queue=["GOLD-022"],
        ),
    )

    second = build_report(review_fs)

    assert second["facts_digest"] != first["facts_digest"]


# ============================================================
# 2. completed-but-unreviewed / ledger 缺失
# ============================================================


def test_completed_but_unreviewed_blocks_gate(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(review_fs.ledger_path, [])

    report = build_report(review_fs)

    codes = issue_codes(report)

    assert review.ISSUE_COMPLETED_BUT_UNREVIEWED in codes
    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED in codes
    assert report["ledger"]["available"] is True
    assert report["ledger"]["pass_reviews"] == []
    assert report["gate"]["advance_allowed"] is False
    assert review.ISSUE_COMPLETED_BUT_UNREVIEWED in report["gate"]["blocking_codes"]
    assert report["summary"]["exit_code"] == review.EXIT_DRIFT
    assert "Executor 不得凭 completed 自称 reviewed" in issue_details(
        report, review.ISSUE_COMPLETED_BUT_UNREVIEWED
    )


def test_missing_ledger_is_fail_closed(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    review_fs.ledger_path.unlink()

    report = build_report(review_fs)

    assert review.ISSUE_LEDGER_MISSING in issue_codes(report)
    assert report["ledger"]["available"] is False
    assert report["gate"]["advance_allowed"] is False
    assert report["summary"]["exit_code"] == review.EXIT_DRIFT


def test_unreadable_or_unsupported_ledger_is_fail_closed(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    review_fs.ledger_path.write_text("{not json", encoding="utf-8")

    assert review.ISSUE_LEDGER_UNREADABLE in issue_codes(build_report(review_fs))

    write_ledger(
        review_fs.ledger_path,
        [],
        schema="gold-ai/gpt-review-ledger/v9",
        schema_version=9,
    )

    unsupported = build_report(review_fs)

    assert review.ISSUE_LEDGER_SCHEMA_UNSUPPORTED in issue_codes(unsupported)
    assert unsupported["ledger"]["available"] is False

    write_raw_ledger(
        review_fs.ledger_path,
        {
            "schema": review.REVIEW_LEDGER_SCHEMA,
            "schema_version": review.REVIEW_LEDGER_SCHEMA_VERSION,
            "entries": {"GOLD-021": {}},
        },
    )

    assert review.ISSUE_LEDGER_ENTRIES_INVALID in issue_codes(build_report(review_fs))


def test_invalid_reviewed_from_is_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(
        review_fs.ledger_path,
        [ledger_entry("GOLD-021", review_fs.results)],
        reviewed_from="",
    )

    report = build_report(review_fs)

    assert review.ISSUE_LEDGER_METADATA_INVALID in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


# ============================================================
# 3. review identity：result 被替换 / 改写、Git identity
# ============================================================


def test_replaced_result_invalidates_review(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    # 被 review 过的 result 之后被改写（同 task、不同 finished_at）。
    write_result(
        review_fs.results,
        "GOLD-021",
        "completed",
        finished_at="2026-09-23T15:00:00+08:00",
    )

    before = tree_digest(review_fs.root)

    report = build_report(review_fs)

    assert review.ISSUE_RESULT_IDENTITY_MISMATCH in issue_codes(report)
    assert "result 已被替换 / 改写" in issue_details(
        report, review.ISSUE_RESULT_IDENTITY_MISMATCH
    )
    assert report["gate"]["advance_allowed"] is False

    # 工具只读：报告前后工作树（含历史 result）逐字节不变。
    assert tree_digest(review_fs.root) == before


def test_review_bound_to_missing_or_non_terminal_result(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    # review 绑定到不存在的 result。
    orphan = ledger_entry("GOLD-021", review_fs.results)

    orphan["task_id"] = "GOLD-099"

    write_ledger(
        review_fs.ledger_path,
        [ledger_entry("GOLD-021", review_fs.results), orphan],
    )

    missing = build_report(review_fs)

    assert review.ISSUE_RESULT_MISSING in issue_codes(missing)

    # review 绑定到非终态 result（running）。
    write_result(review_fs.results, "GOLD-021", "running")

    non_terminal = build_report(review_fs)

    assert review.ISSUE_RESULT_NOT_TERMINAL in issue_codes(non_terminal)
    assert review.ISSUE_RESULT_IDENTITY_MISMATCH in issue_codes(non_terminal)


def test_branch_identity_mismatch_is_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(
        review_fs.ledger_path,
        [ledger_entry("GOLD-021", review_fs.results, branch="other-branch")],
    )

    report = build_report(review_fs)

    assert review.ISSUE_BRANCH_MISMATCH in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


def test_report_leaves_project_files_untouched(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    before = tree_digest(review_fs.root)

    first = build_report(review_fs)

    assert tree_digest(review_fs.root) == before
    assert first["read_only"] is True
    assert build_report(review_fs) == first
    assert tree_digest(review_fs.root) == before


# ============================================================
# 4. 职责边界：只有 GPT 能签发 review
# ============================================================


def test_executor_signed_review_is_rejected(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(
        review_fs.ledger_path,
        [
            ledger_entry(
                "GOLD-021",
                review_fs.results,
                reviewer="cline",
                reviewer_role="cline",
            )
        ],
    )

    report = build_report(review_fs)

    codes = issue_codes(report)

    assert review.ISSUE_EXECUTOR_REVIEW_FORBIDDEN in codes
    assert review.ISSUE_REVIEWER_ROLE_INVALID in codes
    assert report["ledger"]["pass_reviews"] == []
    assert report["ledger"]["entries"][0]["valid"] is False
    assert report["gate"]["advance_allowed"] is False
    assert "executor cannot sign review" in issue_details(
        report, review.ISSUE_EXECUTOR_REVIEW_FORBIDDEN
    )


def test_review_authority_is_fail_closed() -> None:
    assert review.can_sign_review("GPT") is True
    assert review.can_sign_review("gpt") is True
    assert review.can_sign_review("cline") is False
    assert review.can_sign_review("DeepSeek") is False
    assert review.can_sign_review("gemini") is False
    assert review.can_sign_review(None) is False
    assert "fail-closed" in review.review_authority("gemini")[1]

    contract = review.review_write_contract()

    assert contract["schema"] == review.REVIEW_WRITE_CONTRACT_SCHEMA
    assert contract["writer_agents"] == ["gpt"]
    assert set(contract["reader_agents"]) == {"cline", "deepseek"}
    assert contract["reviewer_role"] == "GPT"
    assert contract["executor_can_write_ledger"] is False
    assert contract["executor_can_sign_review"] is False
    assert contract["executor_can_declare_verdict"] is False
    assert contract["module_can_write_ledger"] is False
    assert contract["ledger_can_cross_human_gate"] is False
    assert contract["ledger_can_transition_phase"] is False
    assert contract["ledger_relative_path"] == ".ai/GPT_REVIEW_LEDGER.json"
    assert contract["verdict_vocabulary"] == ["PASS", "FAIL"]

    # 与 §2.5 role_contract 交叉一致：Executor 不得 review、不得改状态。
    assert planner.executor_allowed("review_task_result") is False
    assert planner.executor_allowed("modify_project_state") is False
    assert planner.role_contract()["executor_can_review_task_result"] is False


def test_invalid_entry_never_counts_as_review(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    entry = ledger_entry("GOLD-021", review_fs.results)

    entry["acceptance_summary"] = "   "

    write_ledger(review_fs.ledger_path, [entry])

    report = build_report(review_fs)

    assert review.ISSUE_ENTRY_INVALID in issue_codes(report)
    assert report["ledger"]["entries"][0]["valid"] is False
    assert report["ledger"]["pass_reviews"] == []
    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


def test_entry_schema_violations_are_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    entry = ledger_entry("GOLD-021", review_fs.results)

    entry["verdict"] = "maybe"
    entry["reviewed_commit"] = {"sha": "abc123", "branch": BRANCH}
    entry["reviewed_at"] = "not-a-timestamp"
    entry.pop("acceptance_summary")

    write_ledger(review_fs.ledger_path, [entry])

    report = build_report(review_fs)

    codes = set(issue_codes(report))

    assert {
        review.ISSUE_VERDICT_INVALID,
        review.ISSUE_COMMIT_INVALID,
        review.ISSUE_ENTRY_INVALID,
    } <= codes
    assert report["ledger"]["entries"][0]["valid"] is False
    assert report["ledger"]["pass_reviews"] == []
    assert report["gate"]["advance_allowed"] is False


def test_duplicate_entries_never_grant_pass(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(
        review_fs.ledger_path,
        [
            ledger_entry("GOLD-021", review_fs.results),
            ledger_entry("GOLD-021", review_fs.results, verdict=review.VERDICT_FAIL),
        ],
    )

    report = build_report(review_fs)

    assert review.ISSUE_ENTRY_DUPLICATE in issue_codes(report)
    assert report["ledger"]["duplicate_tasks"] == ["GOLD-021"]
    assert report["ledger"]["reviews"] == {}
    assert report["ledger"]["pass_reviews"] == []
    assert report["gate"]["advance_allowed"] is False


# ============================================================
# 5. 状态推进漂移：指针超前 / 倒退 / 缺失 / FAIL
# ============================================================


def test_pointer_ahead_of_completion_is_drift(review_fs: ReviewFS) -> None:
    for task_id in ("GOLD-021", "GOLD-022", "GOLD-023"):
        write_task(review_fs.tasks, task_id)

    write_result(review_fs.results, "GOLD-021", "completed")

    write_state(
        review_fs.state_path,
        baseline_state(
            current_task="GOLD-023",
            last_completed_task="GOLD-022",
            last_reviewed_task="GOLD-023",
            task_queue=["GOLD-022", "GOLD-023"],
        ),
    )

    write_ledger(review_fs.ledger_path, [ledger_entry("GOLD-021", review_fs.results)])

    report = build_report(review_fs)

    assert review.ISSUE_POINTER_AHEAD_OF_COMPLETION in issue_codes(report)
    assert "没有 completed result" in issue_details(
        report, review.ISSUE_POINTER_AHEAD_OF_COMPLETION
    )
    assert report["pointer"]["newest_completed_result"] == "GOLD-021"
    assert report["gate"]["advance_allowed"] is False


def test_pointer_behind_ledger_is_reported(review_fs: ReviewFS) -> None:
    for task_id in ("GOLD-021", "GOLD-022", "GOLD-023"):
        write_task(review_fs.tasks, task_id)

    write_result(review_fs.results, "GOLD-021", "completed")
    write_result(
        review_fs.results,
        "GOLD-022",
        "completed",
        finished_at="2026-09-23T14:00:00+08:00",
    )

    write_state(
        review_fs.state_path,
        baseline_state(
            current_task="GOLD-023",
            last_completed_task="GOLD-022",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-023"],
        ),
    )

    write_ledger(
        review_fs.ledger_path,
        [
            ledger_entry("GOLD-021", review_fs.results),
            ledger_entry(
                "GOLD-022",
                review_fs.results,
                finished_at="2026-09-23T14:00:00+08:00",
            ),
        ],
    )

    report = build_report(review_fs)

    assert review.ISSUE_POINTER_BEHIND_LEDGER in issue_codes(report)
    assert "指针倒退" in issue_details(report, review.ISSUE_POINTER_BEHIND_LEDGER)
    assert review.ISSUE_COMPLETED_BUT_UNREVIEWED not in issue_codes(report)
    assert report["ledger"]["pass_reviews"] == ["GOLD-021", "GOLD-022"]
    assert report["gate"]["advance_allowed"] is False


def test_pointer_missing_while_ledger_reviewed_is_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_state(
        review_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task=None,
            task_queue=["GOLD-022"],
        ),
    )

    report = build_report(review_fs)

    assert review.ISSUE_POINTER_MISSING in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


def test_pointer_to_unknown_task_is_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_state(
        review_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-099",
            task_queue=["GOLD-022"],
        ),
    )

    report = build_report(review_fs)

    assert review.ISSUE_POINTER_UNKNOWN_TASK in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


def test_failed_review_never_advances(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_ledger(
        review_fs.ledger_path,
        [ledger_entry("GOLD-021", review_fs.results, verdict=review.VERDICT_FAIL)],
    )

    report = build_report(review_fs)

    codes = issue_codes(report)

    assert review.ISSUE_POINTER_ON_FAILED_REVIEW in codes
    assert review.ISSUE_COMPLETED_BUT_UNREVIEWED in codes
    assert report["ledger"]["failed_reviews"] == ["GOLD-021"]
    assert report["ledger"]["pass_reviews"] == []
    assert report["gate"]["advance_allowed"] is False
    assert "不得推进状态指针" in issue_details(report, review.ISSUE_POINTER_ON_FAILED_REVIEW)


def test_pass_review_on_blocked_result_is_reported(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    write_result(review_fs.results, "GOLD-021", "blocked")

    entry = ledger_entry("GOLD-021", review_fs.results)

    entry["reviewed_result"]["status"] = "blocked"

    write_ledger(review_fs.ledger_path, [entry])

    report = build_report(review_fs)

    assert review.ISSUE_PASS_ON_BLOCKED_RESULT in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


# ============================================================
# 6. queue 首任务依赖 / L3-L4 Human Gate
# ============================================================


def seed_queue_state(fs: ReviewFS, **overrides: object) -> None:
    write_state(fs.state_path, baseline_state(**overrides))


def test_queue_head_dependency_not_completed(review_fs: ReviewFS) -> None:
    # 队列严格按文件顺序处理：``GOLD-020-R1`` 这类修订任务排在 ``GOLD-020`` 之前，
    # 因此这里首任务 = GOLD-020-R1，而它的依赖 GOLD-020 尚未完成。
    write_task(review_fs.tasks, "GOLD-020-R1", depends_on=["GOLD-020"])
    write_task(review_fs.tasks, "GOLD-020")

    seed_queue_state(
        review_fs,
        current_task="GOLD-020-R1",
        last_completed_task=None,
        last_reviewed_task=None,
        task_queue=["GOLD-020-R1", "GOLD-020"],
    )

    write_ledger(review_fs.ledger_path, [])

    report = build_report(review_fs)

    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_NOT_COMPLETED in issue_codes(report)
    assert report["queue"]["head"] == "GOLD-020-R1"
    assert report["queue"]["head_dependencies"] == {"GOLD-020": "pending"}
    assert report["queue"]["head_dependencies_satisfied"] is False
    assert report["gate"]["advance_allowed"] is False


def test_queue_head_dependency_blocked(review_fs: ReviewFS) -> None:
    write_task(review_fs.tasks, "GOLD-021")
    write_task(review_fs.tasks, "GOLD-022", depends_on=["GOLD-021"])

    write_result(review_fs.results, "GOLD-021", "blocked")

    seed_queue_state(
        review_fs,
        current_task="GOLD-022",
        last_completed_task="GOLD-021",
        last_reviewed_task=None,
        task_queue=["GOLD-022"],
    )

    write_ledger(review_fs.ledger_path, [])

    report = build_report(review_fs)

    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_BLOCKED in issue_codes(report)
    assert report["queue"]["head_dependencies"] == {"GOLD-021": "blocked"}
    assert report["gate"]["advance_allowed"] is False


def test_queue_head_dependency_missing(review_fs: ReviewFS) -> None:
    write_task(review_fs.tasks, "GOLD-022", depends_on=["GOLD-099"])

    seed_queue_state(
        review_fs,
        current_task="GOLD-022",
        last_completed_task=None,
        last_reviewed_task=None,
        task_queue=["GOLD-022"],
    )

    write_ledger(review_fs.ledger_path, [])

    report = build_report(review_fs)

    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_MISSING in issue_codes(report)
    assert report["queue"]["head_dependencies"] == {"GOLD-099": "missing"}
    assert report["gate"]["advance_allowed"] is False


def test_transitive_dependency_unreviewed_blocks_queue(review_fs: ReviewFS) -> None:
    write_task(review_fs.tasks, "GOLD-020")
    write_task(review_fs.tasks, "GOLD-021", depends_on=["GOLD-020"])
    write_task(review_fs.tasks, "GOLD-022", depends_on=["GOLD-021"])

    write_result(review_fs.results, "GOLD-020", "completed")
    write_result(
        review_fs.results,
        "GOLD-021",
        "completed",
        finished_at="2026-09-23T14:00:00+08:00",
    )

    seed_queue_state(
        review_fs,
        current_task="GOLD-022",
        last_completed_task="GOLD-021",
        last_reviewed_task="GOLD-021",
        task_queue=["GOLD-022"],
    )

    write_ledger(
        review_fs.ledger_path,
        [
            ledger_entry(
                "GOLD-021",
                review_fs.results,
                finished_at="2026-09-23T14:00:00+08:00",
            )
        ],
    )

    report = build_report(review_fs)

    details = issue_details(report, review.ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED)

    assert review.ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED in issue_codes(report)
    assert "GOLD-020" in details
    assert report["queue"]["head_dependencies"] == {
        "GOLD-020": "completed",
        "GOLD-021": "completed",
    }
    # 覆盖下限（最新 PASS review）之前的历史不会被误报成漂移
    assert review.ISSUE_COMPLETED_BUT_UNREVIEWED not in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


def test_queue_head_invalid_task_is_reported(review_fs: ReviewFS) -> None:
    (review_fs.tasks / "GOLD-022.json").write_text("{broken", encoding="utf-8")

    seed_queue_state(
        review_fs,
        current_task="GOLD-022",
        last_completed_task=None,
        last_reviewed_task=None,
        task_queue=["GOLD-022"],
    )

    write_ledger(review_fs.ledger_path, [])

    report = build_report(review_fs)

    assert review.ISSUE_QUEUE_HEAD_TASK_INVALID in issue_codes(report)
    assert report["gate"]["advance_allowed"] is False


@pytest.mark.parametrize("gate", ["L3", "L4", "l3"])
def test_l3_l4_human_gate_is_never_crossed(review_fs: ReviewFS, gate: str) -> None:
    seed_reviewed_project(review_fs, head_gate=gate)

    report = build_report(review_fs)

    issues = {issue["code"]: issue for issue in report["issues"]}

    assert review.ISSUE_QUEUE_HEAD_HUMAN_GATE in issues
    assert issues[review.ISSUE_QUEUE_HEAD_HUMAN_GATE]["severity"] == review.SEVERITY_WARNING
    assert report["queue"]["head_human_gate"] == gate.upper()
    assert report["queue"]["crosses_human_gate"] is True
    assert report["gate"]["crosses_human_gate"] is True
    assert review.ISSUE_QUEUE_HEAD_HUMAN_GATE in report["gate"]["blocking_codes"]
    assert report["gate"]["advance_allowed"] is False
    assert report["summary"]["exit_code"] == review.EXIT_DRIFT


def test_l1_human_gate_does_not_block_when_consistent(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs, head_gate="L1")

    report = build_report(review_fs)

    assert report["issues"] == []
    assert report["queue"]["head_human_gate"] == "L1"
    assert report["gate"]["advance_allowed"] is True
    assert report["gate"]["crosses_human_gate"] is False


# ============================================================
# 7. 可选 Git identity 可达性校验（默认关闭）
# ============================================================


def test_ancestry_verification_is_opt_in_and_fail_closed(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    default = build_report(review_fs)

    assert default["ancestry"]["enabled"] is False
    assert default["ancestry"]["verified"] == []
    assert review.ISSUE_COMMIT_UNREACHABLE not in issue_codes(default)
    assert review.ISSUE_ANCESTRY_UNVERIFIABLE not in issue_codes(default)

    unreachable = build_report(
        review_fs,
        ancestry_probe=lambda root, sha: (False, "not an ancestor of HEAD"),
    )

    assert review.ISSUE_COMMIT_UNREACHABLE in issue_codes(unreachable)
    assert unreachable["ancestry"]["unreachable"] == ["GOLD-021"]
    assert unreachable["gate"]["advance_allowed"] is False

    unverifiable = build_report(
        review_fs,
        ancestry_probe=lambda root, sha: (None, "git unavailable"),
    )

    assert review.ISSUE_ANCESTRY_UNVERIFIABLE in issue_codes(unverifiable)
    assert unverifiable["ancestry"]["unverifiable"] == ["GOLD-021"]

    verified = build_report(
        review_fs,
        ancestry_probe=lambda root, sha: (True, "ancestor of HEAD"),
    )

    assert verified["issues"] == []
    assert verified["ancestry"]["verified"] == ["GOLD-021"]
    assert verified["gate"]["advance_allowed"] is True


def test_ancestry_probe_is_read_only_and_fails_closed_without_git(tmp_path: Path) -> None:
    # 非 Git 目录：探测必须 fail-closed 返回 None（绝不猜测 commit 可达）。
    verified, reason = review.git_ancestry_probe(tmp_path, FAKE_HEAD)

    assert verified is None
    assert reason


# ============================================================
# 8. 只读 CLI（python -m orchestrator.review_ledger）
# ============================================================


@pytest.fixture()
def cli_root(tmp_path: Path) -> Path:
    """构造 <root>/.ai/{tasks,results,PROJECT_STATE.json} 的 CLI 目录布局。"""

    ai_dir = tmp_path / ".ai"

    (ai_dir / "tasks").mkdir(parents=True)
    (ai_dir / "results").mkdir(parents=True)

    seed_fake_git(tmp_path)

    return tmp_path


def seed_cli_project(cli_root: Path, with_ledger: bool) -> None:
    tasks = cli_root / ".ai" / "tasks"
    results = cli_root / ".ai" / "results"

    write_task(tasks, "GOLD-021")
    write_task(tasks, "GOLD-022", depends_on=["GOLD-021"])

    write_result(results, "GOLD-021", "completed")

    write_state(
        cli_root / ".ai" / "PROJECT_STATE.json",
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
        ),
    )

    ledger = cli_root / ".ai" / "GPT_REVIEW_LEDGER.json"

    if with_ledger:
        write_ledger(ledger, [ledger_entry("GOLD-021", results)])

    elif ledger.exists():
        ledger.unlink()


def test_cli_prints_ascii_json_and_returns_zero_when_consistent(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_cli_project(cli_root, with_ledger=True)

    exit_code = review.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == review.EXIT_OK
    assert captured.err == ""
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["schema"] == review.REVIEW_REPORT_SCHEMA
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["paths"]["review_ledger"] == str(
        cli_root / ".ai" / "GPT_REVIEW_LEDGER.json"
    )
    assert payload["summary"]["exit_code"] == review.EXIT_OK
    assert payload["gate"]["advance_allowed"] is True


def test_cli_reports_drift_with_exit_code_2(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_cli_project(cli_root, with_ledger=False)

    exit_code = review.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == review.EXIT_DRIFT
    assert review.ISSUE_LEDGER_MISSING in captured.err
    assert "[gate] advance_allowed=False" in captured.err

    payload = json.loads(captured.out)

    assert payload["summary"]["has_drift"] is True
    assert payload["summary"]["advance_allowed"] is False


def test_cli_reports_unreadable_state_with_exit_code_3(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = review.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == review.EXIT_STATE_UNREADABLE
    assert review.ISSUE_PROJECT_STATE_UNREADABLE in captured.err
    assert json.loads(captured.out)["state"]["status"] is None


def test_cli_subprocess_is_byte_stable_and_ascii(cli_root: Path) -> None:
    seed_cli_project(cli_root, with_ledger=True)

    command = [
        sys.executable,
        "-m",
        "orchestrator.review_ledger",
        "--root",
        str(cli_root),
        "--generated-at",
        AUDIT_TIME,
    ]

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)
    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == review.EXIT_OK
    assert first.stdout == second.stdout
    assert first.stdout.isascii()
    assert first.stderr == b""
    assert json.loads(first.stdout.decode("ascii"))["read_only"] is True


def test_cli_defaults_to_repo_root_without_writing_anything() -> None:
    ledger = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

    ledger_before = ledger.read_bytes() if ledger.exists() else None

    before_tasks = tree_digest(REPO_ROOT / ".ai" / "tasks")
    before_results = tree_digest(REPO_ROOT / ".ai" / "results")
    state_before = (REPO_ROOT / ".ai" / "PROJECT_STATE.json").read_bytes()

    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.review_ledger", "--generated-at", AUDIT_TIME],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode in {
        review.EXIT_OK,
        review.EXIT_DRIFT,
        review.EXIT_STATE_UNREADABLE,
    }

    payload = json.loads(completed.stdout.decode("ascii"))

    assert payload["schema"] == review.REVIEW_REPORT_SCHEMA
    assert payload["paths"]["tasks_dir"] == str(REPO_ROOT / ".ai" / "tasks")
    assert payload["summary"]["exit_code"] == completed.returncode

    assert (REPO_ROOT / ".ai" / "PROJECT_STATE.json").read_bytes() == state_before
    assert tree_digest(REPO_ROOT / ".ai" / "tasks") == before_tasks
    assert tree_digest(REPO_ROOT / ".ai" / "results") == before_results
    assert (ledger.read_bytes() if ledger.exists() else None) == ledger_before


# ============================================================
# 9. 源码守卫：无写入路径、无 planning API、CLI 无规划 / 写状态开关
# ============================================================


def test_module_exposes_no_write_path_or_planning_api() -> None:
    public = {name for name in dir(review) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    for name in (
        "build_review_report",
        "validate_review_ledger",
        "review_entry_view",
        "review_write_contract",
        "review_authority",
        "can_sign_review",
        "git_ancestry_probe",
        "report_exit_code",
    ):
        assert name in public, name

    for forbidden in (
        "write_text",
        "write_bytes",
        "open(",
        "mkdir",
        "rmtree",
        "shutil",
        "os.replace",
        "os.remove",
        "tempfile",
        "requests",
        "httpx",
        "aiohttp",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden

    # 唯一的外部进程调用是只读的 `git merge-base --is-ancestor`。
    assert MODULE_SOURCE.count('["git", "merge-base"') == 1
    assert MODULE_SOURCE.count("subprocess.run(") == 1

    for forbidden in (
        "git commit",
        "git push",
        "git checkout",
        "git reset",
        "git rebase",
        "git merge ",
        "git add",
        "clean -fd",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_reuses_planner_snapshot_read_only_reader() -> None:
    assert review.planner is planner
    assert review.PROJECT_STATE_PATH == planner.PROJECT_STATE_PATH
    assert review.REVIEW_LEDGER_RELATIVE_PATH == ".ai/GPT_REVIEW_LEDGER.json"
    assert review.DEFAULT_REVIEW_LEDGER_PATH == REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"
    assert review.REVIEWER_ROLE == "GPT"
    assert set(review.REVIEW_VERDICTS) == {"PASS", "FAIL"}


def test_cli_option_contract_excludes_planning_and_write_flags() -> None:
    parser = review.build_parser()

    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert option_strings == {
        "-h",
        "--help",
        "--root",
        "--state",
        "--tasks-dir",
        "--results-dir",
        "--ledger",
        "--generated-at",
        "--verify-ancestry",
    }

    for forbidden in (
        "--plan",
        "--next-task",
        "--create-task",
        "--write-result",
        "--update-state",
        "--sign-review",
        "--append-entry",
        "--approve",
        "--output",
    ):
        assert forbidden not in option_strings


def test_report_contains_no_planning_or_repair_authority(review_fs: ReviewFS) -> None:
    seed_reviewed_project(review_fs)

    report = build_report(review_fs)

    for forbidden in (
        "next_task",
        "suggested_task",
        "follow_on_tasks",
        "repair_actions",
        "auto_fix",
    ):
        assert forbidden not in report

    assert report["gate"]["tool_can_sign_review"] is False
    assert report["gate"]["tool_can_repair_drift"] is False
    assert report["gate"]["executor_may_advance_on_its_own"] is False
    assert report["review_contract"]["executor_can_sign_review"] is False
