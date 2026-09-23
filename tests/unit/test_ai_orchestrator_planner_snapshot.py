"""GOLD-023：GPT Planner 只读快照与职责边界契约的回归测试。

覆盖：
1) 只读性 / 确定性：快照前后工作树逐字节不变、两次构建完全一致、渲染可 round-trip；
2) 本轮真实漂移：PROJECT_STATE 指针仍停在 GOLD-017/018、results 已到 GOLD-020
   （以及当前仓库的 GOLD-020/021 vs GOLD-022）——只报告、绝不自动修复；
3) queue 声明 vs 真实非终态 task、未知 task / result status、Gate / blocker /
   安全不变量不一致的确定性诊断；
4) GPT = Planner/Reviewer/Architect、Cline/DeepSeek = Executor 的机器可测契约
   （Executor 无 follow-on planning 权限，未知能力 fail-closed）；
5) CLI（`python -m orchestrator.planner_snapshot`）的 stdout 机器通道与退出码。

GOLD-024 追加覆盖：

6) 版本化契约（`schema` + 整数 `schema_version`）与 Git branch/head / PROJECT_STATE 摘要；
7) 确定性：`facts_digest` 与 wall-clock 解耦、对状态变化敏感、可机器复算；
8) GOLD-021/022/023 全部 completed 但 PROJECT_STATE 仍落后时的 fail-closed 报告；
9) 受控 `--output`（runtime / 临时路径写入、禁止路径 fail-closed 拒绝、默认零写入）；
10) Executor 不具备任何 follow-on planning / 状态写入 API。

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
from orchestrator import planner_snapshot_output as snapshot_output

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-24T00:00:00+08:00"

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULE_SOURCE = Path(planner.__file__).read_text(encoding="utf-8")

OUTPUT_MODULE_SOURCE = Path(snapshot_output.__file__).read_text(encoding="utf-8")

FAKE_BRANCH = "cline-agent"

FAKE_HEAD = "3f1a9c8e7b6d5f4a3b2c1d0e9f8a7b6c5d4e3f21"


def seed_fake_git(root: Path, branch: str = FAKE_BRANCH, head: str = FAKE_HEAD) -> None:
    """构造只读 Git 事实（``.git/HEAD`` + loose ref），让快照不依赖机器上的真实仓库。"""

    git_dir = root / ".git"

    refs = git_dir / "refs" / "heads"

    refs.mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    (refs / branch).write_text(f"{head}\n", encoding="utf-8")


@dataclass(frozen=True)
class PlannerFS:
    root: Path
    tasks: Path
    results: Path
    state_path: Path


@pytest.fixture()
def planner_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlannerFS:
    """把 tasks / results / PROJECT_STATE 全部重定向到 tmp_path（对真实仓库零影响）。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"
    tasks.mkdir()
    results.mkdir()
    state_path = tmp_path / "PROJECT_STATE.json"

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(planner, "PROJECT_STATE_PATH", state_path)
    monkeypatch.setattr(planner, "ROOT", tmp_path)

    seed_fake_git(tmp_path)

    return PlannerFS(root=tmp_path, tasks=tasks, results=results, state_path=state_path)


def write_task(tasks: Path, task_id: str, **extra: object) -> Path:
    path = tasks / f"{task_id}.json"
    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_raw_task(tasks: Path, file_stem: str, payload: object) -> Path:
    path = tasks / f"{file_stem}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def write_result(results: Path, task_id: str, status: str) -> Path:
    path = results / f"{task_id}.json"
    path.write_text(
        json.dumps({"task_id": task_id, "status": status}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_raw_result(results: Path, task_id: str, payload: object) -> Path:
    path = results / f"{task_id}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def baseline_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "schema_version": 1,
        "project": "XAUUSD",
        "branch": "cline-agent",
        "phase": "Phase 3",
        "status": "BLOCKED",
        "current_task": "GOLD-018",
        "last_completed_task": "GOLD-017",
        "last_reviewed_task": "GOLD-017",
        "blockers": [
            {"code": "PHASE3_3_DATA", "detail": "真实证据不足", "retryable": False}
        ],
        "human_gates": [
            {"code": "PHASE3_3_L3_DECISION", "detail": "Phase 切换需人工 L3"}
        ],
        "invariants": [
            "LIVE_TRADING=false",
            "ALLOW_EXTERNAL_ORDER_SUBMISSION=false",
        ],
        "queue_target_size": 3,
        "task_queue": ["GOLD-018"],
        "queue_status": "ACTIVE",
    }
    state.update(overrides)
    return state


def write_state(state_path: Path, state: dict[str, object]) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_snapshot(fs: PlannerFS, **overrides: object) -> dict[str, Any]:
    """带固定 generated_at 的只读快照（显式传入路径，便于断言）。"""

    params: dict[str, object] = {
        "state_path": fs.state_path,
        "tasks_dir": fs.tasks,
        "results_dir": fs.results,
        "generated_at": AUDIT_TIME,
    }
    params.update(overrides)
    return planner.build_planner_snapshot(**params)  # type: ignore[arg-type]


def issue_codes(snapshot: dict[str, Any]) -> list[str]:
    return [str(issue["code"]) for issue in snapshot["issues"]]


def issue_details(snapshot: dict[str, Any], code: str) -> str:
    return "\n".join(
        str(issue["detail"])
        for issue in snapshot["issues"]
        if issue["code"] == code
    )


def tree_digest(root: Path) -> dict[str, str]:
    """目录内全部文件的 sha256（用于证明快照是只读的）。"""

    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


def seed_clean_queue(fs: PlannerFS) -> None:
    """构造一个完全一致、零漂移的最小项目（GOLD-021 已完成，GOLD-022 待执行）。"""

    for task_id in ("GOLD-021", "GOLD-022"):
        write_task(fs.tasks, task_id)

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


# ============================================================
# 1. 只读性 / 确定性
# ============================================================


def test_snapshot_is_read_only_and_deterministic(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)

    before = tree_digest(planner_fs.root)

    first = build_snapshot(planner_fs)

    after = tree_digest(planner_fs.root)

    second = build_snapshot(planner_fs)

    assert first == second
    assert before == after
    assert first["read_only"] is True
    assert first["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert first["issues"] == []
    assert first["summary"] == {
        "issue_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "has_drift": False,
        "exit_code": planner.EXIT_OK,
    }

    rendered = planner.render_planner_snapshot(first)

    assert json.loads(rendered) == first
    assert rendered == planner.render_planner_snapshot(second)


def test_snapshot_stdout_payload_is_ascii(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)

    rendered = planner.render_planner_snapshot(build_snapshot(planner_fs))

    assert rendered.isascii()
    assert json.loads(rendered)["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA


def test_default_paths_follow_orchestrator_view_and_project_state(
    planner_fs: PlannerFS,
) -> None:
    seed_clean_queue(planner_fs)

    snapshot = planner.build_planner_snapshot(generated_at=AUDIT_TIME)

    assert snapshot["paths"]["project_state"] == str(planner_fs.state_path)
    assert snapshot["paths"]["tasks_dir"] == str(planner_fs.tasks)
    assert snapshot["paths"]["results_dir"] == str(planner_fs.results)
    assert snapshot["artifacts"]["task_files"] == ["GOLD-021", "GOLD-022"]
    assert snapshot["queue"]["pending"] == ["GOLD-022"]
    assert snapshot["generated_at"] == AUDIT_TIME


def test_build_restores_orchestrator_view(
    planner_fs: PlannerFS,
    tmp_path: Path,
) -> None:
    seed_clean_queue(planner_fs)

    other_tasks = tmp_path / "other-tasks"
    other_results = tmp_path / "other-results"
    other_tasks.mkdir()
    other_results.mkdir()

    planner.build_planner_snapshot(
        state_path=planner_fs.state_path,
        tasks_dir=other_tasks,
        results_dir=other_results,
        generated_at=AUDIT_TIME,
    )

    assert planner_fs.tasks == orch.TASK_DIR
    assert planner_fs.results == orch.RESULT_DIR


def test_snapshot_source_has_no_write_path() -> None:
    """源码守卫：只读契约不允许出现任何文件写入 / 删除 / 网络 / 子进程路径。"""

    for forbidden in (
        "write_text",
        "write_bytes",
        "open(",
        "os.replace",
        "shutil",
        "mkdir",
        "unlink",
        "rmtree",
        "subprocess",
        "import os",
        "while ",
        "httpx",
        "requests",
        "aiohttp",
        "openai",
        "socket",
        "tempfile",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden


# ============================================================
# 2. PROJECT_STATE 指针 vs results 漂移（本轮真实场景）
# ============================================================


def test_detects_project_state_pointer_behind_results_gold_017_018_vs_gold_020(
    planner_fs: PlannerFS,
) -> None:
    """本轮实际漂移：state 仍停在 GOLD-017/018，results 已经到 GOLD-020。"""

    for task_id in ("GOLD-017", "GOLD-018", "GOLD-019", "GOLD-020"):
        write_task(planner_fs.tasks, task_id)
        write_result(planner_fs.results, task_id, "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-018",
            last_completed_task="GOLD-017",
            last_reviewed_task="GOLD-017",
            task_queue=["GOLD-018"],
            queue_status="IDLE",
        ),
    )

    state_before = planner_fs.state_path.read_text(encoding="utf-8")
    before = tree_digest(planner_fs.root)

    snapshot = build_snapshot(planner_fs)

    assert snapshot["pointer"]["latest_terminal_result"] == "GOLD-020"
    assert snapshot["pointer"]["terminal_results"] == [
        "GOLD-017",
        "GOLD-018",
        "GOLD-019",
        "GOLD-020",
    ]

    codes = issue_codes(snapshot)

    assert planner.ISSUE_POINTER_BEHIND_RESULTS in codes
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT

    details = issue_details(snapshot, planner.ISSUE_POINTER_BEHIND_RESULTS)

    assert "last_completed_task=GOLD-017" in details
    assert "last_reviewed_task=GOLD-017" in details
    assert "current_task=GOLD-018" in details
    assert "latest_terminal=GOLD-020" in details

    # 只报告，绝不自动修复
    assert planner_fs.state_path.read_text(encoding="utf-8") == state_before
    assert tree_digest(planner_fs.root) == before


def test_detects_current_repo_drift_pattern_state_020_021_vs_results_022(
    planner_fs: PlannerFS,
) -> None:
    """当前仓库同型漂移：state 指针 GOLD-020/021，results 已到 GOLD-022。"""

    for task_id in ("GOLD-020", "GOLD-021", "GOLD-022"):
        write_task(planner_fs.tasks, task_id)

    write_result(planner_fs.results, "GOLD-020", "completed")
    write_result(planner_fs.results, "GOLD-021", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-021",
            last_completed_task="GOLD-020",
            last_reviewed_task="GOLD-020",
            task_queue=["GOLD-021", "GOLD-022"],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert snapshot["pointer"]["latest_terminal_result"] == "GOLD-021"
    assert planner.ISSUE_POINTER_BEHIND_RESULTS in codes
    assert planner.ISSUE_QUEUE_DECLARATION_MISMATCH in codes
    assert snapshot["queue"]["pending"] == ["GOLD-022"]
    assert snapshot["queue"]["declared_but_terminal"] == ["GOLD-021"]
    assert "GOLD-021" in issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT


def test_detects_pointer_ahead_of_results(planner_fs: PlannerFS) -> None:
    write_task(planner_fs.tasks, "GOLD-020")
    write_task(planner_fs.tasks, "GOLD-021")
    write_result(planner_fs.results, "GOLD-020", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-021",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-020",
            task_queue=["GOLD-021"],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert planner.ISSUE_POINTER_AHEAD_OF_RESULTS in codes
    assert planner.ISSUE_POINTER_BEHIND_RESULTS not in codes

    details = issue_details(snapshot, planner.ISSUE_POINTER_AHEAD_OF_RESULTS)

    assert "last_completed_task=GOLD-021" in details
    assert "尚无终态 result" in details


def test_detects_pointer_unknown_task(planner_fs: PlannerFS) -> None:
    write_task(planner_fs.tasks, "GOLD-020")
    write_result(planner_fs.results, "GOLD-020", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-020",
            last_completed_task="GOLD-020",
            last_reviewed_task="GOLD-099",
            task_queue=[],
            queue_status="IDLE",
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_POINTER_UNKNOWN_TASK in issue_codes(snapshot)
    assert "GOLD-099" in issue_details(snapshot, planner.ISSUE_POINTER_UNKNOWN_TASK)


def test_detects_missing_pointers_when_results_exist(planner_fs: PlannerFS) -> None:
    write_task(planner_fs.tasks, "GOLD-020")
    write_result(planner_fs.results, "GOLD-020", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task=None,
            last_completed_task=None,
            last_reviewed_task=None,
            task_queue=[],
            queue_status="IDLE",
        ),
    )

    snapshot = build_snapshot(planner_fs)

    details = issue_details(snapshot, planner.ISSUE_POINTER_MISSING)

    assert planner.ISSUE_POINTER_MISSING in issue_codes(snapshot)
    assert "last_completed_task 缺失" in details
    assert "last_reviewed_task 缺失" in details


def test_non_project_prefix_result_is_not_treated_as_latest(
    planner_fs: PlannerFS,
) -> None:
    for task_id in ("GOLD-020", "GOLD-021"):
        write_task(planner_fs.tasks, task_id)
        write_result(planner_fs.results, task_id, "completed")

    # 历史遗留的非项目任务：字母序排在 GOLD 之后，但绝不是「最新项目结果」。
    write_result(planner_fs.results, "TEST-002", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task=None,
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=[],
            queue_status="IDLE",
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert snapshot["pointer"]["latest_terminal_result"] == "GOLD-021"
    assert planner.ISSUE_POINTER_BEHIND_RESULTS not in issue_codes(snapshot)
    assert planner.ISSUE_POINTER_AHEAD_OF_RESULTS not in issue_codes(snapshot)
    assert snapshot["summary"]["exit_code"] == planner.EXIT_OK


# ============================================================
# 3. queue 声明 vs 真实非终态 task
# ============================================================


def seed_pending_queue(fs: PlannerFS, declared: object) -> None:
    """GOLD-021 已完成，GOLD-022 / GOLD-023 待执行；指针与 results 完全一致。"""

    for task_id in ("GOLD-021", "GOLD-022", "GOLD-023"):
        write_task(fs.tasks, task_id)

    write_result(fs.results, "GOLD-021", "completed")

    write_state(
        fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=declared,
        ),
    )


def test_undeclared_pending_task_is_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, ["GOLD-022"])

    snapshot = build_snapshot(planner_fs)

    assert issue_codes(snapshot) == [planner.ISSUE_QUEUE_DECLARATION_MISMATCH]

    details = issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)

    assert "pending_but_not_declared=['GOLD-023']" in details
    assert snapshot["queue"]["pending"] == ["GOLD-022", "GOLD-023"]
    assert snapshot["queue"]["declared"] == ["GOLD-022"]
    assert snapshot["queue"]["pending_but_not_declared"] == ["GOLD-023"]
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT


def test_declared_task_already_terminal_is_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, ["GOLD-021", "GOLD-022", "GOLD-023"])

    snapshot = build_snapshot(planner_fs)

    details = issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)

    assert "declared_but_terminal=['GOLD-021']" in details
    assert snapshot["queue"]["declared_but_terminal"] == ["GOLD-021"]


def test_declared_queue_task_without_file_is_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, ["GOLD-022", "GOLD-023", "GOLD-099"])

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert planner.ISSUE_QUEUE_TASK_MISSING in codes
    assert "GOLD-099" in issue_details(snapshot, planner.ISSUE_QUEUE_TASK_MISSING)
    assert snapshot["queue"]["declared_missing_files"] == ["GOLD-099"]


def test_declared_queue_order_difference_is_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, ["GOLD-023", "GOLD-022"])

    snapshot = build_snapshot(planner_fs)

    details = issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)

    assert "declared_order_differs_from_pending" in details
    assert snapshot["queue"]["declared_order_matches_pending"] is False


def test_invalid_task_queue_declaration_is_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, "GOLD-022")

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_QUEUE_DECLARATION_MISMATCH in issue_codes(snapshot)
    assert snapshot["queue"]["declared_error"] == "task_queue 必须是字符串数组"
    assert snapshot["queue"]["declared"] == []


def test_queue_status_active_without_pending_tasks_is_reported(
    planner_fs: PlannerFS,
) -> None:
    write_task(planner_fs.tasks, "GOLD-021")
    write_result(planner_fs.results, "GOLD-021", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task=None,
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=[],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    details = issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)

    assert "queue_status_ACTIVE_without_pending_tasks" in details
    assert snapshot["queue"]["pending"] == []
    assert snapshot["queue"]["underfilled"] is True


def test_consistent_queue_declaration_is_not_reported(planner_fs: PlannerFS) -> None:
    seed_pending_queue(planner_fs, ["GOLD-022", "GOLD-023"])

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_QUEUE_DECLARATION_MISMATCH not in issue_codes(snapshot)
    assert snapshot["queue"]["declared_order_matches_pending"] is True
    assert snapshot["queue"]["first_pending"] == "GOLD-022"
    assert snapshot["queue"]["runnable_task"] == "GOLD-022"
    assert snapshot["queue"]["first_stop_reason"] == "ready"
    assert snapshot["summary"]["exit_code"] == planner.EXIT_OK


# ============================================================
# 4. 未知 task / result status 与依赖图诊断
# ============================================================


def seed_single_pending(fs: PlannerFS, **task_extra: object) -> None:
    """只有 GOLD-023 待执行（无终态 result）的最小项目。"""

    write_task(fs.tasks, "GOLD-023", **task_extra)

    write_state(
        fs.state_path,
        baseline_state(
            current_task="GOLD-023",
            last_completed_task=None,
            last_reviewed_task=None,
            task_queue=["GOLD-023"],
        ),
    )


def test_unknown_result_status_is_reported(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs)
    write_result(planner_fs.results, "GOLD-023", "frobnicated")

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_UNKNOWN_RESULT_STATUS in issue_codes(snapshot)
    assert "GOLD-023=frobnicated" in issue_details(
        snapshot, planner.ISSUE_UNKNOWN_RESULT_STATUS
    )
    assert snapshot["results"]["unknown"] == ["GOLD-023"]
    assert snapshot["tasks"][0]["readiness"] is False
    assert "result status unknown" in snapshot["tasks"][0]["reason"]
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT


def test_result_without_status_field_is_unknown(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs)
    write_raw_result(planner_fs.results, "GOLD-023", {"task_id": "GOLD-023"})

    snapshot = build_snapshot(planner_fs)

    assert snapshot["results"]["unknown"] == ["GOLD-023"]
    assert "GOLD-023=unknown" in issue_details(
        snapshot, planner.ISSUE_UNKNOWN_RESULT_STATUS
    )


def test_corrupt_result_file_is_unknown(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs)
    write_raw_result(planner_fs.results, "GOLD-023", "{ not json")

    snapshot = build_snapshot(planner_fs)

    assert snapshot["results"]["unknown"] == ["GOLD-023"]
    assert planner.ISSUE_UNKNOWN_RESULT_STATUS in issue_codes(snapshot)


def test_corrupt_task_file_is_reported(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs)
    write_raw_task(planner_fs.tasks, "GOLD-023", "{ not json")

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert planner.ISSUE_TASK_FILE_INVALID in codes
    assert planner.ISSUE_DEPENDENCY_GRAPH_INVALID in codes

    assert "task file invalid" in issue_details(snapshot, planner.ISSUE_TASK_FILE_INVALID)
    assert snapshot["tasks"][0]["readiness"] is False


def test_task_id_filename_mismatch_is_reported(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs)
    write_raw_task(planner_fs.tasks, "GOLD-023", {"task_id": "GOLD-024", "title": "t"})

    snapshot = build_snapshot(planner_fs)

    assert "task_id 与文件名不一致" in issue_details(
        snapshot, planner.ISSUE_TASK_FILE_INVALID
    )


def test_invalid_depends_on_is_metadata_invalid(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs, depends_on=123)

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_TASK_METADATA_INVALID in issue_codes(snapshot)
    assert "depends_on" in issue_details(snapshot, planner.ISSUE_TASK_METADATA_INVALID)
    assert snapshot["tasks"][0]["dependencies"] is None
    assert snapshot["tasks"][0]["readiness"] is False


def test_non_bool_auto_start_is_metadata_invalid(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs, auto_start="true")

    snapshot = build_snapshot(planner_fs)

    assert "auto_start" in issue_details(snapshot, planner.ISSUE_TASK_METADATA_INVALID)
    assert snapshot["tasks"][0]["auto_start"] is None


def test_unknown_human_gate_is_reported_as_gate_inconsistent(
    planner_fs: PlannerFS,
) -> None:
    seed_single_pending(planner_fs, human_gate="L9")

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_GATE_INCONSISTENT in issue_codes(snapshot)
    assert "human_gate 未知档位: L9" in issue_details(
        snapshot, planner.ISSUE_GATE_INCONSISTENT
    )
    assert snapshot["tasks"][0]["human_gate"] is None
    assert snapshot["gates"]["queue_tasks_waiting_on_human_gate"] == []


def test_missing_dependency_is_reported_as_dependency_graph_invalid(
    planner_fs: PlannerFS,
) -> None:
    seed_single_pending(planner_fs, depends_on=["GOLD-099"])

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert planner.ISSUE_DEPENDENCY_GRAPH_INVALID in codes
    assert "dependency missing: GOLD-099" in issue_details(
        snapshot, planner.ISSUE_DEPENDENCY_GRAPH_INVALID
    )
    assert snapshot["tasks"][0]["dependencies"] == ["GOLD-099"]
    assert snapshot["tasks"][0]["readiness"] is False


def test_dependency_cycle_is_reported(planner_fs: PlannerFS) -> None:
    write_task(planner_fs.tasks, "GOLD-023", depends_on=["GOLD-024"])
    write_task(planner_fs.tasks, "GOLD-024", depends_on=["GOLD-023"])

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-023",
            last_completed_task=None,
            last_reviewed_task=None,
            task_queue=["GOLD-023", "GOLD-024"],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert "dependency cycle" in issue_details(
        snapshot, planner.ISSUE_DEPENDENCY_GRAPH_INVALID
    )


# ============================================================
# 5. Gate / blocker / 安全不变量一致性
# ============================================================


def test_blocked_status_without_blockers_is_inconsistent(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)
    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
            blockers=[],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_BLOCKER_STATE_INCONSISTENT in issue_codes(snapshot)
    assert "没有声明任何 blocker" in issue_details(
        snapshot, planner.ISSUE_BLOCKER_STATE_INCONSISTENT
    )


def test_non_blocked_status_with_blockers_is_inconsistent(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)
    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
            status="ACTIVE",
        ),
    )

    snapshot = build_snapshot(planner_fs)

    details = issue_details(snapshot, planner.ISSUE_BLOCKER_STATE_INCONSISTENT)

    assert "status=ACTIVE" in details
    assert "PHASE3_3_DATA" in details


def test_missing_safety_invariant_is_reported(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)
    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
            invariants=["LIVE_TRADING=false"],
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_SAFETY_INVARIANT_MISSING in issue_codes(snapshot)
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in issue_details(
        snapshot, planner.ISSUE_SAFETY_INVARIANT_MISSING
    )
    assert snapshot["gates"]["safety_invariants"]["present"] == ["LIVE_TRADING=false"]
    assert snapshot["gates"]["safety_invariants"]["missing"] == [
        "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"
    ]


def test_missing_invariants_list_reports_both(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)
    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
            invariants=None,
        ),
    )

    snapshot = build_snapshot(planner_fs)

    missing = [
        issue
        for issue in snapshot["issues"]
        if issue["code"] == planner.ISSUE_SAFETY_INVARIANT_MISSING
    ]

    assert len(missing) == 2
    assert snapshot["gates"]["safety_invariants"]["present"] == []
    assert snapshot["summary"]["error_count"] >= 2


def test_blocking_gate_at_queue_head_is_reported_as_warning(
    planner_fs: PlannerFS,
) -> None:
    seed_single_pending(planner_fs, human_gate="L3")

    snapshot = build_snapshot(planner_fs)

    stalled = [
        issue
        for issue in snapshot["issues"]
        if issue["code"] == planner.ISSUE_QUEUE_GATE_STALLED
    ]

    assert len(stalled) == 1
    assert stalled[0]["severity"] == planner.SEVERITY_WARNING
    assert "human_gate=L3" in stalled[0]["detail"]

    assert snapshot["gates"]["queue_tasks_waiting_on_human_gate"] == ["GOLD-023"]
    assert snapshot["queue"]["blocking_gate_at_head"] == "L3"
    assert snapshot["queue"]["first_stop_task"] == "GOLD-023"
    assert "human_gate=L3" in snapshot["queue"]["first_stop_reason"]
    assert snapshot["summary"]["warning_count"] == 1


def test_blocking_gate_with_inactive_queue_is_not_warned(planner_fs: PlannerFS) -> None:
    seed_single_pending(planner_fs, human_gate="L4")
    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-023",
            last_completed_task=None,
            last_reviewed_task=None,
            task_queue=["GOLD-023"],
            queue_status="IDLE",
        ),
    )

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_QUEUE_GATE_STALLED not in issue_codes(snapshot)
    assert snapshot["queue"]["blocking_gate_at_head"] == "L4"


def test_safety_gate_contract_flags_never_allow_executor(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)

    gates = build_snapshot(planner_fs)["gates"]

    assert gates["blocking_human_gate_levels"] == ["L3", "L4"]
    assert gates["phase_transition_requires_human_gate"] is True
    assert gates["executor_may_transition_phase"] is False
    assert gates["executor_may_cross_human_gate"] is False
    assert gates["state_human_gate_codes"] == ["PHASE3_3_L3_DECISION"]


# ============================================================
# 6. PROJECT_STATE 不可读
# ============================================================


def test_missing_project_state_is_reported_with_exit_code_3(
    planner_fs: PlannerFS,
) -> None:
    write_task(planner_fs.tasks, "GOLD-023")

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_PROJECT_STATE_UNREADABLE in issue_codes(snapshot)
    assert snapshot["project_state"] is None
    assert snapshot["pointer"]["latest_terminal_result"] is None
    assert snapshot["summary"]["exit_code"] == planner.EXIT_STATE_UNREADABLE
    assert planner.planner_snapshot_exit_code(snapshot) == planner.EXIT_STATE_UNREADABLE


def test_corrupt_project_state_is_reported(planner_fs: PlannerFS) -> None:
    planner_fs.state_path.write_text("{ not json", encoding="utf-8")

    snapshot = build_snapshot(planner_fs)

    assert "unreadable" in issue_details(snapshot, planner.ISSUE_PROJECT_STATE_UNREADABLE)
    assert snapshot["summary"]["exit_code"] == planner.EXIT_STATE_UNREADABLE


def test_project_state_must_be_json_object(planner_fs: PlannerFS) -> None:
    planner_fs.state_path.write_text("[1, 2, 3]", encoding="utf-8")

    snapshot = build_snapshot(planner_fs)

    assert "not a JSON object" in issue_details(
        snapshot, planner.ISSUE_PROJECT_STATE_UNREADABLE
    )


# ============================================================
# 7. GPT Planner / Executor 职责边界契约
# ============================================================


def test_role_contract_planner_is_gpt_only() -> None:
    contract = planner.role_contract()

    assert contract["schema"] == planner.ROLE_CONTRACT_SCHEMA
    assert contract["read_only"] is True
    assert contract["planner_agents"] == ["gpt"]
    assert set(contract["planner_roles"]) == {"planner", "reviewer", "architect"}
    assert contract["executor_agents"] == ["cline", "deepseek"]
    assert contract["executor_roles"] == ["executor"]


def test_executor_forbidden_capabilities_are_all_denied() -> None:
    contract = planner.role_contract()

    assert contract["executor_forbidden_capabilities"]

    for capability in contract["executor_forbidden_capabilities"]:
        allowed, reason = planner.executor_capability(capability)

        assert allowed is False
        assert "forbidden" in reason
        assert planner.executor_allowed(capability) is False


def test_executor_can_only_use_whitelisted_capabilities() -> None:
    assert planner.EXECUTOR_ALLOWED_CAPABILITIES

    for capability in planner.EXECUTOR_ALLOWED_CAPABILITIES:
        assert planner.executor_capability(capability) == (True, "allowed")
        assert planner.executor_allowed(capability) is True


def test_executor_unknown_capability_fails_closed() -> None:
    for capability in ("deploy_to_production", "", "   ", "modify_project_state_v2"):
        allowed, reason = planner.executor_capability(capability)

        assert allowed is False
        assert "unknown capability" in reason


@pytest.mark.parametrize(
    "capability",
    [
        "generate_follow_on_task",
        "refill_rolling_queue",
        "modify_project_state",
        "decide_phase",
        "loosen_acceptance",
        "cross_human_gate",
        "review_task_result",
        "self_promote_to_planner",
    ],
)
def test_executor_has_no_follow_on_planning_authority(capability: str) -> None:
    assert planner.executor_allowed(capability) is False


def test_decision_guards_cover_every_planner_only_decision() -> None:
    forbidden = set(planner.EXECUTOR_FORBIDDEN_CAPABILITIES)
    allowed = set(planner.EXECUTOR_ALLOWED_CAPABILITIES)

    assert allowed.isdisjoint(forbidden)
    assert set(planner.PLANNER_DECISION_GUARDS) == set(planner.PLANNER_ONLY_DECISIONS)
    assert set(planner.PLANNER_DECISION_GUARDS.values()) <= forbidden

    for guard in planner.PLANNER_DECISION_GUARDS.values():
        assert planner.executor_allowed(guard) is False


def test_snapshot_embeds_role_contract_without_executor_authority(
    planner_fs: PlannerFS,
) -> None:
    seed_clean_queue(planner_fs)

    contract = build_snapshot(planner_fs)["role_contract"]

    assert contract["schema"] == planner.ROLE_CONTRACT_SCHEMA
    assert contract["executor_can_generate_follow_on_tasks"] is False
    assert contract["executor_can_refill_rolling_queue"] is False
    assert contract["executor_can_modify_project_state"] is False
    assert contract["executor_can_decide_phase"] is False
    assert contract["executor_can_loosen_acceptance"] is False
    assert contract["executor_can_cross_human_gate"] is False
    assert contract["executor_can_review_task_result"] is False
    assert contract["executor_can_self_promote_to_planner"] is False
    assert contract["phase_transition_requires_human_gate"] is True

    # 任何 executor_can_* 标志都不允许是 True
    assert not [key for key, value in contract.items() if key.startswith("executor_can_") and value]


# ============================================================
# 8. 只读 CLI（python -m orchestrator.planner_snapshot）
# ============================================================


@pytest.fixture()
def cli_root(tmp_path: Path) -> Path:
    """构造 <root>/.ai/{tasks,results,runtime,PROJECT_STATE.json} 的 CLI 目录布局。"""

    ai_dir = tmp_path / ".ai"
    tasks = ai_dir / "tasks"
    results = ai_dir / "results"
    tasks.mkdir(parents=True)
    results.mkdir(parents=True)
    (ai_dir / "runtime").mkdir(parents=True)

    seed_fake_git(tmp_path)

    for task_id in ("GOLD-021", "GOLD-022"):
        write_task(tasks, task_id)

    write_result(results, "GOLD-021", "completed")

    write_state(
        ai_dir / "PROJECT_STATE.json",
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
        ),
    )

    return tmp_path


def test_cli_prints_ascii_json_and_returns_zero_for_consistent_state(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = planner.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == planner.EXIT_OK
    assert captured.err == ""
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["paths"]["tasks_dir"] == str(cli_root / ".ai" / "tasks")
    assert payload["summary"]["exit_code"] == planner.EXIT_OK


def test_cli_reports_drift_on_stdout_and_stderr(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 让 GOLD-022 也终态化：state 指针随即落后于 results。
    write_result(cli_root / ".ai" / "results", "GOLD-022", "completed")

    exit_code = planner.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == planner.EXIT_DRIFT
    assert "PROJECT_STATE_POINTER_BEHIND_RESULTS" in captured.err
    assert "QUEUE_DECLARATION_MISMATCH" in captured.err

    payload = json.loads(captured.out)

    assert payload["summary"]["has_drift"] is True
    assert payload["pointer"]["latest_terminal_result"] == "GOLD-022"


def test_cli_reports_unreadable_state_with_exit_code_3(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (cli_root / ".ai" / "PROJECT_STATE.json").unlink()

    exit_code = planner.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == planner.EXIT_STATE_UNREADABLE
    assert "PROJECT_STATE_UNREADABLE" in captured.err
    assert json.loads(captured.out)["project_state"] is None


def test_cli_subprocess_is_byte_stable_and_ascii(cli_root: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "orchestrator.planner_snapshot",
        "--root",
        str(cli_root),
        "--generated-at",
        AUDIT_TIME,
    ]

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)
    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == planner.EXIT_OK
    assert first.stdout == second.stdout
    assert first.stdout.isascii()
    assert first.stderr == b""

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert payload["read_only"] is True


def test_cli_defaults_to_repo_root_without_writing_anything() -> None:
    ai_dir = REPO_ROOT / ".ai"

    before = {
        name: tree_digest(ai_dir / name)
        for name in ("tasks", "results")
    }
    state_before = (ai_dir / "PROJECT_STATE.json").read_bytes()

    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.planner_snapshot", "--generated-at", AUDIT_TIME],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode in {
        planner.EXIT_OK,
        planner.EXIT_DRIFT,
        planner.EXIT_STATE_UNREADABLE,
    }

    payload = json.loads(completed.stdout.decode("ascii"))

    assert payload["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert payload["paths"]["tasks_dir"] == str(ai_dir / "tasks")
    assert payload["summary"]["exit_code"] == completed.returncode

    assert (ai_dir / "PROJECT_STATE.json").read_bytes() == state_before
    for name in ("tasks", "results"):
        assert tree_digest(ai_dir / name) == before[name]


# ============================================================
# 9. GOLD-024：版本化契约 / 确定性 / 受控 --output / 职责边界
# ============================================================

# Executor 绝不允许出现的规划 / 状态写入 API（follow-on planning 权限全部否定）。
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


@pytest.fixture()
def fake_temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把「系统临时目录」替换为 tmp_path 下的专用目录（不依赖 pytest basetemp 位置）。"""

    temp_root = tmp_path / "fake-temp"
    temp_root.mkdir()

    monkeypatch.setattr(snapshot_output, "temp_directory", lambda: temp_root)

    return temp_root


def test_snapshot_exposes_versioned_contract_with_git_and_state_facts(
    planner_fs: PlannerFS,
) -> None:
    seed_clean_queue(planner_fs)

    snapshot = build_snapshot(planner_fs)

    assert snapshot["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert snapshot["schema"] == "gold-ai/planner-snapshot/v2"
    assert snapshot["schema_version"] == planner.PLANNER_SNAPSHOT_SCHEMA_VERSION == 2
    assert snapshot["read_only"] is True

    assert snapshot["git"] == {
        "branch": FAKE_BRANCH,
        "head": FAKE_HEAD,
        "head_short": FAKE_HEAD[:8],
        "detached": False,
        "available": True,
    }

    state = snapshot["state"]

    assert state["schema_version"] == 1
    assert state["project"] == "XAUUSD"
    assert state["phase"] == "Phase 3"
    assert state["status"] == "BLOCKED"
    assert state["current_task"] == "GOLD-022"
    assert state["last_completed_task"] == "GOLD-021"
    assert state["last_reviewed_task"] == "GOLD-021"
    assert state["declared_queue"] == ["GOLD-022"]
    assert state["queue_target_size"] == 3
    assert state["blocker_codes"] == ["PHASE3_3_DATA"]
    assert state["human_gate_codes"] == ["PHASE3_3_L3_DECISION"]
    assert state["invariants"] == [
        "LIVE_TRADING=false",
        "ALLOW_EXTERNAL_ORDER_SUBMISSION=false",
    ]

    # 原样回显仍在 project_state；摘要不替代审计原文
    assert snapshot["project_state"]["project"] == "XAUUSD"
    assert snapshot["paths"]["root"] == str(planner_fs.root)
    assert snapshot["issues"] == []


def test_snapshot_reports_missing_git_facts_fail_closed(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)

    (planner_fs.root / ".git" / "HEAD").unlink()

    snapshot = build_snapshot(planner_fs)

    assert planner.ISSUE_GIT_INFO_UNAVAILABLE in issue_codes(snapshot)
    assert snapshot["git"]["available"] is False
    assert snapshot["git"]["branch"] is None
    assert snapshot["git"]["head"] is None
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT


def test_facts_digest_is_wall_clock_independent_and_recomputable(
    planner_fs: PlannerFS,
) -> None:
    seed_clean_queue(planner_fs)

    first = build_snapshot(planner_fs)

    second = planner.build_planner_snapshot(
        state_path=planner_fs.state_path,
        tasks_dir=planner_fs.tasks,
        results_dir=planner_fs.results,
        generated_at=AUDIT_TIME_LATER,
    )

    assert first["generated_at"] == AUDIT_TIME
    assert second["generated_at"] == AUDIT_TIME_LATER

    # wall-clock 只出现在审计字段，绝不参与 facts
    assert first["facts_digest"] == second["facts_digest"]
    assert len(first["facts_digest"]) == 64
    assert "generated_at" not in planner.snapshot_facts(first)

    # digest 可被 GPT 独立复算（幂等）
    assert planner.snapshot_facts_digest(first) == first["facts_digest"]
    assert planner.snapshot_facts_digest(second) == second["facts_digest"]

    determinism = first["determinism"]

    assert determinism["digest_field"] == "facts_digest"
    assert determinism["wall_clock_in_facts"] is False
    assert determinism["excluded_from_facts"] == [
        "generated_at",
        "facts_digest",
        "determinism",
    ]
    assert determinism["ordering"]["issues"] == "sorted by (code, detail)"
    assert determinism["ordering"]["tasks"] == "sorted by task_id_sort_key"


def test_facts_digest_reacts_to_state_changes(planner_fs: PlannerFS) -> None:
    seed_clean_queue(planner_fs)

    baseline = build_snapshot(planner_fs)

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-022",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022"],
            queue_status="PAUSED",
        ),
    )

    changed = build_snapshot(planner_fs)

    assert changed["issues"] == []
    assert changed["state"]["queue_status"] == "PAUSED"
    assert changed["facts_digest"] != baseline["facts_digest"]


def test_state_behind_reported_when_gold_021_022_023_all_completed(
    planner_fs: PlannerFS,
) -> None:
    """GOLD-021/022/023 已全部 completed，而 PROJECT_STATE 仍停在 GOLD-021。"""

    for task_id in ("GOLD-021", "GOLD-022", "GOLD-023"):
        write_task(planner_fs.tasks, task_id)
        write_result(planner_fs.results, task_id, "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-021",
            last_completed_task="GOLD-021",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-021"],
            queue_status="ACTIVE",
        ),
    )

    before = tree_digest(planner_fs.root)
    state_before = planner_fs.state_path.read_text(encoding="utf-8")

    snapshot = build_snapshot(planner_fs)

    codes = issue_codes(snapshot)

    assert planner.ISSUE_POINTER_BEHIND_RESULTS in codes
    assert planner.ISSUE_QUEUE_DECLARATION_MISMATCH in codes
    assert snapshot["pointer"]["latest_terminal_result"] == "GOLD-023"
    assert snapshot["pointer"]["terminal_results"] == ["GOLD-021", "GOLD-022", "GOLD-023"]
    assert snapshot["results"]["terminal"] == {
        "GOLD-021": "completed",
        "GOLD-022": "completed",
        "GOLD-023": "completed",
    }
    assert snapshot["queue"]["pending"] == []
    assert snapshot["queue"]["declared_but_terminal"] == ["GOLD-021"]
    assert snapshot["tasks"] == []
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT

    details = issue_details(snapshot, planner.ISSUE_POINTER_BEHIND_RESULTS)

    assert "last_completed_task=GOLD-021" in details
    assert "last_reviewed_task=GOLD-021" in details
    assert "current_task=GOLD-021" in details
    assert "latest_terminal=GOLD-023" in details

    queue_details = issue_details(snapshot, planner.ISSUE_QUEUE_DECLARATION_MISMATCH)

    assert "declared_but_terminal=['GOLD-021']" in queue_details
    assert "queue_status_ACTIVE_without_pending_tasks" in queue_details

    # 只报告，绝不自动修复
    assert planner_fs.state_path.read_text(encoding="utf-8") == state_before
    assert tree_digest(planner_fs.root) == before


def test_broken_inputs_are_fail_closed_without_silent_repair(planner_fs: PlannerFS) -> None:
    """损坏 task / 未知 result status / queue 漂移 / 指针超出 必须同时报告且零修复。"""

    write_task(planner_fs.tasks, "GOLD-021")
    write_raw_task(planner_fs.tasks, "GOLD-023", "{ not json")
    write_raw_result(
        planner_fs.results,
        "GOLD-021",
        {"task_id": "GOLD-021", "status": "frobnicated"},
    )
    write_result(planner_fs.results, "GOLD-022", "completed")

    write_state(
        planner_fs.state_path,
        baseline_state(
            current_task="GOLD-021",
            last_completed_task="GOLD-022",
            last_reviewed_task="GOLD-021",
            task_queue=["GOLD-022", "GOLD-023"],
            queue_status="ACTIVE",
        ),
    )

    before = tree_digest(planner_fs.root)
    state_before = planner_fs.state_path.read_text(encoding="utf-8")

    snapshot = build_snapshot(planner_fs)

    codes = set(issue_codes(snapshot))

    assert {
        planner.ISSUE_TASK_FILE_INVALID,
        planner.ISSUE_UNKNOWN_RESULT_STATUS,
        planner.ISSUE_POINTER_AHEAD_OF_RESULTS,
        planner.ISSUE_QUEUE_DECLARATION_MISMATCH,
    } <= codes

    assert snapshot["results"]["unknown"] == ["GOLD-021"]
    assert snapshot["queue"]["pending"] == ["GOLD-021", "GOLD-023"]
    assert snapshot["tasks"][0]["task_id"] == "GOLD-021"
    assert snapshot["tasks"][1]["task_id"] == "GOLD-023"
    assert snapshot["tasks"][1]["load_error"]
    assert snapshot["summary"]["exit_code"] == planner.EXIT_DRIFT

    # fail-closed：绝不静默修复 task / result / state
    assert planner_fs.state_path.read_text(encoding="utf-8") == state_before
    assert (planner_fs.tasks / "GOLD-023.json").read_text(encoding="utf-8") == "{ not json"
    assert not (planner_fs.results / "GOLD-023.json").exists()
    assert tree_digest(planner_fs.root) == before


def test_snapshot_and_output_modules_expose_no_planning_authority() -> None:
    """Executor 侧不存在 follow-on planning / 状态写入 API，未知能力只能 fail-closed。"""

    for module in (planner, snapshot_output):
        public = {name for name in dir(module) if not name.startswith("_")}

        assert public.isdisjoint(FORBIDDEN_PLANNING_API), module.__name__

    assert "build_planner_snapshot" in dir(planner)
    assert "role_contract" in dir(planner)
    assert "write_snapshot_output" in dir(snapshot_output)

    # 快照模块仍然完全只读（GOLD-023 源码守卫保持有效）
    for forbidden in (
        "write_text",
        "write_bytes",
        "open(",
        "mkdir",
        "rmtree",
        "shutil",
        "subprocess",
        "tempfile",
        "requests",
        "httpx",
        "aiohttp",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden

    # 受控输出模块：唯一写操作是 write_snapshot_output，无进程 / 网络 / 目录创建
    for forbidden in (
        "import os",
        "import subprocess",
        "import shutil",
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "os.replace",
        "os.remove",
        "rmtree",
        "mkdir",
    ):
        assert forbidden not in OUTPUT_MODULE_SOURCE, forbidden

    assert OUTPUT_MODULE_SOURCE.count("write_text") == 1
    assert "def write_snapshot_output" in OUTPUT_MODULE_SOURCE

    guard = snapshot_output.guard_summary()

    assert guard["read_only_by_default"] is True
    assert guard["creates_directories"] is False
    assert guard["runtime_subpath"] == ".ai/runtime"
    assert guard["rejection_code"] == snapshot_output.ISSUE_OUTPUT_PATH_REJECTED
    assert guard["rejection_exit_code"] == snapshot_output.EXIT_OUTPUT_REJECTED
    assert ".ai/tasks" in guard["forbidden_subpaths"]
    assert ".ai/results" in guard["forbidden_subpaths"]
    assert ".ai/PROJECT_STATE.json" in guard["forbidden_subpaths"]


def test_cli_option_contract_excludes_planning_flags() -> None:
    parser = planner.build_parser()

    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert option_strings == {
        "-h",
        "--help",
        "--root",
        "--state",
        "--tasks-dir",
        "--results-dir",
        "--generated-at",
        "--output",
    }

    for forbidden in ("--plan", "--next-task", "--create-task", "--write-result", "--update-state"):
        assert forbidden not in option_strings


def test_output_guard_rejects_forbidden_project_paths(
    cli_root: Path,
    fake_temp_root: Path,
) -> None:
    before = tree_digest(cli_root)

    forbidden_relatives = (
        ".ai/tasks/GOLD-099.json",
        ".ai/results/GOLD-099.json",
        ".ai/PROJECT_STATE.json",
        ".ai/logs/orchestrator.log",
        ".git/HEAD",
        "src/planner_snapshot.json",
        "database/snapshot.json",
        "config/snapshot.json",
        "data/snapshot.json",
        "docs/snapshot.json",
        "PROGRESS_LOG.md",
        "pyproject.toml",
        ".clinerules",
    )

    for relative in forbidden_relatives:
        target, reason = snapshot_output.resolve_output_target(cli_root, cli_root / relative)

        assert target is None, relative
        assert reason is not None
        assert "禁止写入" in reason, relative

    # `..` 逃逸先解析再判定，无法绕过守卫
    sneaky = cli_root / ".ai" / "runtime" / ".." / "tasks" / "GOLD-099.json"

    target, reason = snapshot_output.resolve_output_target(cli_root, sneaky)

    assert target is None
    assert reason is not None
    assert "禁止写入" in reason

    assert tree_digest(cli_root) == before
    assert not (cli_root / ".ai" / "tasks" / "GOLD-099.json").exists()


def test_output_guard_accepts_runtime_and_temp_paths_only(
    cli_root: Path,
    fake_temp_root: Path,
) -> None:
    runtime_target, runtime_reason = snapshot_output.resolve_output_target(
        cli_root, cli_root / ".ai" / "runtime" / "snapshot.json"
    )

    assert runtime_reason is None
    assert runtime_target == (cli_root / ".ai" / "runtime" / "snapshot.json").resolve()

    temp_target, temp_reason = snapshot_output.resolve_output_target(
        cli_root, fake_temp_root / "snapshot.json"
    )

    assert temp_reason is None
    assert temp_target == (fake_temp_root / "snapshot.json").resolve()

    outside_target, outside_reason = snapshot_output.resolve_output_target(
        cli_root, cli_root / "snapshot.json"
    )

    assert outside_target is None
    assert outside_reason is not None
    assert "只允许写入 runtime / 临时路径" in outside_reason

    missing_target, missing_reason = snapshot_output.resolve_output_target(
        cli_root, cli_root / ".ai" / "runtime" / "nested" / "snapshot.json"
    )

    assert missing_target is None
    assert missing_reason is not None
    assert "父目录不存在" in missing_reason
    assert not (cli_root / ".ai" / "runtime" / "nested").exists()


def test_cli_output_writes_to_runtime_path_and_keeps_stdout_empty(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = cli_root / ".ai" / "runtime" / "planner-snapshot.json"
    state_before = (cli_root / ".ai" / "PROJECT_STATE.json").read_bytes()
    protected = {
        name: tree_digest(cli_root / name)
        for name in (".git", ".ai/tasks", ".ai/results")
    }

    exit_code = planner.main(
        ["--root", str(cli_root), "--output", str(target), "--generated-at", AUDIT_TIME]
    )

    captured = capsys.readouterr()

    assert exit_code == planner.EXIT_OK
    assert captured.out == ""
    assert "[error]" not in captured.err
    assert "已写入受控路径" in captured.err
    assert target.is_file()

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert payload["schema_version"] == planner.PLANNER_SNAPSHOT_SCHEMA_VERSION
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["facts_digest"] == planner.snapshot_facts_digest(payload)

    # 默认 stdout 通道与受控文件通道对同一输入必须逐字节一致
    stdout_exit = planner.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    assert stdout_exit == planner.EXIT_OK
    assert capsys.readouterr().out == target.read_text(encoding="utf-8")

    # 除显式目标外零写入：state / tasks / results / .git 全部不变
    assert (cli_root / ".ai" / "PROJECT_STATE.json").read_bytes() == state_before
    for name, digest in protected.items():
        assert tree_digest(cli_root / name) == digest


def test_cli_output_rejects_forbidden_target_with_exit_code_4(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = tree_digest(cli_root)

    forbidden = cli_root / ".ai" / "results" / "GOLD-099.json"

    exit_code = planner.main(
        ["--root", str(cli_root), "--output", str(forbidden), "--generated-at", AUDIT_TIME]
    )

    captured = capsys.readouterr()

    assert exit_code == snapshot_output.EXIT_OUTPUT_REJECTED
    assert exit_code not in {planner.EXIT_OK, planner.EXIT_DRIFT, planner.EXIT_STATE_UNREADABLE}
    assert captured.out == ""
    assert snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err
    assert not forbidden.exists()
    assert tree_digest(cli_root) == before


def test_cli_default_output_writes_nothing_under_root(
    cli_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = tree_digest(cli_root)

    exit_code = planner.main(["--root", str(cli_root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    assert exit_code == planner.EXIT_OK
    assert captured.err == ""
    assert json.loads(captured.out)["summary"]["exit_code"] == planner.EXIT_OK
    assert tree_digest(cli_root) == before


def test_cli_subprocess_output_file_is_stable_and_stdout_free(cli_root: Path) -> None:
    target = cli_root / ".ai" / "runtime" / "subprocess-snapshot.json"

    command = [
        sys.executable,
        "-m",
        "orchestrator.planner_snapshot",
        "--root",
        str(cli_root),
        "--output",
        str(target),
        "--generated-at",
        AUDIT_TIME,
    ]

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == planner.EXIT_OK
    assert first.stdout == b""
    assert "[error]" not in first.stderr.decode("utf-8", errors="replace")
    assert target.is_file()

    content = target.read_text(encoding="utf-8")

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert second.returncode == planner.EXIT_OK
    assert second.stdout == b""
    assert target.read_text(encoding="utf-8") == content

    payload = json.loads(content)

    assert payload["schema"] == planner.PLANNER_SNAPSHOT_SCHEMA
    assert payload["schema_version"] == planner.PLANNER_SNAPSHOT_SCHEMA_VERSION
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["git"]["branch"] == FAKE_BRANCH
    assert payload["git"]["head"] == FAKE_HEAD
    assert payload["facts_digest"] == planner.snapshot_facts_digest(payload)
