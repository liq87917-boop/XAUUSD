"""GOLD-035：Orchestrator 在队列低水位时主动请求 GPT Planner（只读提示）。

覆盖：

1. 成功 commit+push 之后**立即**重算 refill 事实，并在 `deficit > 0` 时输出
   结构化日志 `GPT_PLANNER_REFILL_REQUIRED`（`follow_on_count` / `target` /
   `deficit` / `head` / `reason_codes`）；
2. idle / no-runnable 路径同样能报告低水位，但**去重 / 节流**（同一事实状态在
   `REFILL_HINT_SECONDS` 内只提示一次），事实一变立即重新提示；
3. 只读镜像写 `<root>/.ai/runtime/**`（复用 `planner_snapshot_output` 的
   fail-closed 守卫），**绝不**写 `.ai/tasks` / `.ai/results` /
   `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json` / result；
4. 提示绝不阻塞当前合法任务、绝不绕过 blocked / human-gated queue head、
   绝不生成任务、绝不改项目状态（Executor 权限不变）；
5. 只读模块加载在「脚本直跑」（`sys.path[0]` = `orchestrator/`）下同样可用。

所有测试只在 `tmp_path` 内构造文件；`run_iteration` 使用 fake git / fake Cline，
绝不触发真实子进程，绝不对真实仓库做任何写操作。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner

# Executor 侧绝不存在的规划 / 补队列 / 改状态 API（与 GOLD-023/031/032/034 同口径）。
FORBIDDEN_PLANNING_API = (
    "generate_follow_on_task",
    "refill_rolling_queue",
    "create_task",
    "append_task",
    "plan_next_task",
    "write_task",
    "write_result",
    "update_project_state",
    "modify_project_state",
    "decide_phase",
    "loosen_acceptance",
    "review_task_result",
)

MODULE_SOURCE = Path(orch.__file__).read_text(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]


# ============================================================
# 只读假文件系统（tmp_path）
# ============================================================


@dataclass(frozen=True)
class HintFS:
    root: Path
    state_dir: Path
    tasks: Path
    results: Path
    runtime: Path
    state_path: Path


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def write_state(fs: HintFS, task_queue: list[str], **extra: Any) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "project": "GOLD",
        "branch": "cline-agent",
        "phase": "Phase 3",
        "status": "ACTIVE",
        "current_task": task_queue[0] if task_queue else None,
        "last_completed_task": None,
        "last_reviewed_task": None,
        "blockers": [],
        "queue_target_size": 3,
        "task_queue": list(task_queue),
        "queue_status": "ACTIVE",
        "planner_lookahead_size": 3,
        **extra,
    }

    fs.state_path.write_text(json.dumps(payload), encoding="utf-8")


def write_task(fs: HintFS, task_id: str, **extra: Any) -> Path:
    path = fs.tasks / f"{task_id}.json"

    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, "auto_start": True, **extra}),
        encoding="utf-8",
    )

    return path


def seed_queue(
    fs: HintFS,
    task_ids: list[str],
    *,
    extras: dict[str, dict[str, Any]] | None = None,
) -> None:
    """写入 task 文件 + 与之一致的 PROJECT_STATE 队列声明（无漂移）。"""

    for task_id in task_ids:
        write_task(fs, task_id, **(extras or {}).get(task_id, {}))

    write_state(fs, task_ids)


def add_task(fs: HintFS, task_id: str, **extra: Any) -> None:
    """追加一个已批准 follow-on（同时更新队列声明，保持无漂移）。"""

    state = json.loads(fs.state_path.read_text(encoding="utf-8"))

    queue = [str(item) for item in state.get("task_queue") or []]

    queue.append(task_id)

    write_task(fs, task_id, **extra)

    write_state(fs, queue)


def hint_lines(caplog: pytest.LogCaptureFixture, code: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if code in record.getMessage()]


@pytest.fixture()
def hint_fs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> HintFS:
    """把 ROOT / tasks / results / runtime / PROJECT_STATE 全部重定向到 tmp_path。"""

    root = tmp_path / "repo"
    state_dir = root / ".ai"
    tasks = state_dir / "tasks"
    results = state_dir / "results"
    runtime = state_dir / "runtime"
    task_states = runtime / "tasks"
    recovery = runtime / "recovery"

    for directory in (tasks, results, runtime, task_states, recovery):
        directory.mkdir(parents=True, exist_ok=True)

    fs = HintFS(
        root=root,
        state_dir=state_dir,
        tasks=tasks,
        results=results,
        runtime=runtime,
        state_path=state_dir / "PROJECT_STATE.json",
    )

    write_state(fs, [])

    monkeypatch.setattr(orch, "ROOT", root)
    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(orch, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch, "PROJECT_STATE_PATH", fs.state_path)
    monkeypatch.setattr(orch, "TASK_STATE_DIR", task_states)
    monkeypatch.setattr(orch, "RECOVERY_DIR", recovery)
    monkeypatch.setattr(orch, "RECOVERY_STATE_FILE", recovery / "recovery_state.json")
    monkeypatch.setattr(orch, "RECOVERY_LOCK_ARCHIVE_DIR", recovery / "locks")
    monkeypatch.setattr(orch, "LOCK_FILE", runtime / "orchestrator.lock")
    monkeypatch.setattr(orch, "LOG_DIR", root / "logs")
    monkeypatch.setattr(orch, "REMOTE_NAME", "origin")
    monkeypatch.setattr(orch, "REQUIRED_BRANCH", "cline-agent")

    return fs


@pytest.fixture(autouse=True)
def forbid_real_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全网：本模块任何测试都不得真的启动 Cline / subprocess / 真实 git。

    需要 fake 的测试在自己的测试体内用 ``monkeypatch.setattr`` 覆盖；
    这里只是保证「忘记覆盖」时测试立即失败而不是真的跑起外部进程。
    """

    def refuse_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("this test must not execute a real external process")

    monkeypatch.setattr(orch, "run_cline", refuse_call)
    monkeypatch.setattr(orch, "run_validations", refuse_call)
    monkeypatch.setattr(orch, "run_command", refuse_call)
    monkeypatch.setattr(orch, "git", refuse_call)


# ============================================================
# fake git（只读脚本化，绝不访问真实仓库 / 远端）
# ============================================================


class HintGit:
    """脚本化 git fake：只记录命令，绝不访问真实远端。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.branch = "cline-agent"
        self.head = "a" * 40
        self.status_stdout = ""

    def _ok(self, stdout: str = "") -> dict[str, object]:
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
        }

    def __call__(self, command: str, timeout: int = 120) -> dict[str, object]:
        del timeout
        self.commands.append(command)

        if command.startswith("status --porcelain"):
            return self._ok(self.status_stdout)

        if command.startswith("branch --show-current"):
            return self._ok(f"{self.branch}\n")

        if command == "rev-parse HEAD":
            return self._ok(f"{self.head}\n")

        if command.startswith("rev-list --count"):
            return self._ok("0\n")

        if command.startswith("pull --rebase"):
            return self._ok()

        if command.startswith("push "):
            return self._ok()

        if command.startswith("add -A") or command.startswith("commit "):
            return self._ok()

        raise AssertionError(f"unexpected git command: {command}")


def passing_validation() -> list[dict[str, object]]:
    return [
        {
            "command": ".venv\\Scripts\\python.exe -m pytest tests -q",
            "returncode": 0,
            "timed_out": False,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    ]


# ============================================================
# 1. 成功 commit+push 之后立即提示（结构化日志 + runtime 镜像）
# ============================================================


def test_post_push_report_emits_required_line_and_mirrors_facts(
    hint_fs: HintFS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-201", "GOLD-202"])

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        report = orch.planner_refill_report(
            context=orch.REFILL_HINT_CONTEXT_POST_PUSH,
            only_when_required=True,
            throttle=False,
        )

    assert report["emitted"] is True

    line = str(report["line"])

    assert line.startswith(orch.PLANNER_REFILL_REQUIRED_CODE)
    assert "context=post_successful_commit_push" in line
    assert "head=GOLD-201" in line
    assert "follow_on_count=1" in line
    assert "target=3" in line
    assert "deficit=2" in line
    assert "REFILL_REQUIRED" in line
    assert "reason_codes=[" in line
    assert "executor_can_refill=false" in line

    assert hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE) == [line]

    mirror = hint_fs.runtime / orch.REFILL_REQUEST_RUNTIME_NAME

    assert mirror.exists()

    payload = json.loads(mirror.read_text(encoding="utf-8"))

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["read_only"] is True
    assert payload["refill_required"] is True
    assert payload["deficit"] == 2
    assert payload["planner_authority"]["executor_can_refill"] is False
    assert payload["planner_authority"]["planning_authority"] == "gpt_only"


def test_post_push_report_is_silent_when_queue_is_at_target(
    hint_fs: HintFS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-211", "GOLD-212", "GOLD-213", "GOLD-214"])

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        report = orch.planner_refill_report(
            context=orch.REFILL_HINT_CONTEXT_POST_PUSH,
            only_when_required=True,
            throttle=False,
        )

    assert report["emitted"] is False
    assert report["line"] is None
    assert isinstance(report["signature"], str)
    assert caplog.text == ""
    assert not (hint_fs.runtime / orch.REFILL_REQUEST_RUNTIME_NAME).exists()


def test_planner_refill_hint_line_matches_mirrored_facts(
    hint_fs: HintFS,
) -> None:
    seed_queue(hint_fs, ["GOLD-215", "GOLD-216", "GOLD-217"])

    payload = orch.planner_refill_facts()

    assert payload is not None

    line = orch.planner_refill_hint_line(payload, context=orch.REFILL_HINT_CONTEXT_IDLE)

    assert f"head={payload['queue_head']}" in line
    assert f"follow_on_count={payload['follow_on_count']}" in line
    assert f"target={payload['lookahead_target']}" in line
    assert f"deficit={payload['deficit']}" in line
    assert payload["deficit"] == max(
        0, payload["lookahead_target"] - payload["follow_on_count"]
    )
# ============================================================
# 2. idle / no-runnable 路径：能报告低水位，但去重 / 节流
# ============================================================


def test_idle_hint_is_throttled_until_facts_change(
    hint_fs: HintFS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-221"])

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        first = orch.planner_refill_idle_hint(None, current_time=100.0)

        assert hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)
        assert "head=GOLD-221" in hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)[0]
        assert first["emitted_at"] == 100.0

        caplog.clear()

        second = orch.planner_refill_idle_hint(first, current_time=101.0)

        # 同一事实状态：去重 / 节流，绝不每 20 秒刷屏
        assert caplog.records == []
        assert second["signature"] == first["signature"]

        caplog.clear()

        third = orch.planner_refill_idle_hint(
            second, current_time=100.0 + orch.REFILL_HINT_SECONDS
        )

        assert hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)

        caplog.clear()

        # 事实一变（多了一个已批准 follow-on）⇒ 立即重新提示，不受节流窗口限制
        add_task(hint_fs, "GOLD-222")

        fourth = orch.planner_refill_idle_hint(
            third, current_time=100.0 + orch.REFILL_HINT_SECONDS + 1.0
        )

        assert hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)
        assert fourth["signature"] != third["signature"]
        assert "follow_on_count=1" in hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)[0]


def test_idle_hint_reports_satisfied_state_once(
    hint_fs: HintFS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-226", "GOLD-227", "GOLD-228", "GOLD-229"])

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        state = orch.planner_refill_idle_hint(None, current_time=0.0)

        assert orch.PLANNER_REFILL_SATISFIED_CODE in caplog.text
        assert orch.PLANNER_REFILL_REQUIRED_CODE not in caplog.text

        caplog.clear()

        orch.planner_refill_idle_hint(state, current_time=1.0)

        assert caplog.records == []


def test_should_emit_refill_hint_throttle_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "REFILL_HINT_SECONDS", 1800)

    assert orch.should_emit_refill_hint("a", None, None) is True
    assert orch.should_emit_refill_hint("a", "a", None) is True
    assert orch.should_emit_refill_hint("a", "a", 100.0, current_time=1899.0) is False
    assert orch.should_emit_refill_hint("a", "a", 100.0, current_time=1900.0) is True
    # 事实变化 ⇒ 立即重新提示
    assert orch.should_emit_refill_hint("b", "a", 1899.0, current_time=1899.5) is True


def test_planner_refill_hint_state_is_fail_safe() -> None:
    assert orch.planner_refill_hint_state(None) == (None, None)
    assert orch.planner_refill_hint_state({"signature": 1, "emitted_at": True}) == (None, None)
    assert orch.planner_refill_hint_state(
        {"signature": "s", "emitted_at": 5.5}
    ) == ("s", 5.5)
    assert orch.planner_refill_hint_state({"signature": "s", "emitted_at": "x"}) == ("s", None)


def test_hint_throttle_default_is_not_below_poll_seconds() -> None:
    assert orch.REFILL_HINT_SECONDS >= orch.POLL_SECONDS
# ============================================================
# 3. Orchestrator 生命周期接入（成功 push 之后 / idle）
# ============================================================


def test_run_iteration_reports_refill_after_successful_push(
    hint_fs: HintFS,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-231", "GOLD-232", "GOLD-233"])

    monkeypatch.setattr(orch, "git", HintGit())
    monkeypatch.setattr(orch, "run_validations", lambda task: passing_validation())
    monkeypatch.setattr(orch, "get_changed_files", lambda: [" M orchestrator/ai_orchestrator.py"])
    monkeypatch.setattr(
        orch, "get_diff_stat", lambda: "orchestrator/ai_orchestrator.py | 1 +"
    )

    calls: list[Path] = []

    def fake_run_cline(task_file: Path) -> dict[str, object]:
        calls.append(task_file)
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "summary": {"finish_reason": "completed", "model": "deepseek-flash"},
        }

    monkeypatch.setattr(orch, "run_cline", fake_run_cline)

    tasks_before = tree_digest(hint_fs.tasks)
    state_before = hint_fs.state_path.read_bytes()

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        step = orch.run_iteration(None)

    # 当前合法任务照常执行：低水位提示绝不阻塞 rolling queue
    assert step["action"] == orch.ITERATION_CONTINUE
    assert step["outcome"] == "completed"
    assert calls == [hint_fs.tasks / "GOLD-231.json"]

    lines = hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)

    assert len(lines) == 1

    assert "context=post_successful_commit_push" in lines[0]
    assert "head=GOLD-232" in lines[0]  # GOLD-231 完成后 queue head 前移
    assert "follow_on_count=1" in lines[0]
    assert "target=3" in lines[0]
    assert "deficit=2" in lines[0]
    assert "reason_codes=[" in lines[0]

    # 绝不生成任务、绝不改项目状态
    assert tree_digest(hint_fs.tasks) == tasks_before
    assert hint_fs.state_path.read_bytes() == state_before
    assert sorted(path.name for path in hint_fs.results.glob("*.json")) == ["GOLD-231.json"]

    mirror = hint_fs.runtime / orch.REFILL_REQUEST_RUNTIME_NAME

    assert json.loads(mirror.read_text(encoding="utf-8"))["queue_head"] == "GOLD-232"


def test_run_iteration_idle_reports_low_watermark_without_crossing_gate(
    hint_fs: HintFS,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-241", "GOLD-242"], extras={"GOLD-241": {"human_gate": "L3"}})

    monkeypatch.setattr(orch, "git", HintGit())

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("Cline must not run: queue head is human-gated")

    monkeypatch.setattr(orch, "run_cline", refuse)

    tasks_before = tree_digest(hint_fs.tasks)
    state_before = hint_fs.state_path.read_bytes()

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        step = orch.run_iteration(None)

    assert step["action"] == orch.ITERATION_SLEEP
    assert step["outcome"] == "idle"

    lines = hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE)

    assert len(lines) == 1
    assert "context=idle_no_runnable_task" in lines[0]
    assert "head=GOLD-241" in lines[0]
    assert "follow_on_count=1" in lines[0]
    assert "deficit=2" in lines[0]
    assert "BLOCKING_HUMAN_GATE_AT_HEAD" in lines[0]
    assert "QUEUE_HEAD_NOT_RUNNABLE" in lines[0]

    # 绝不绕过 gated head：没执行任何任务、没写 result、没生成 task、没改状态
    assert list(hint_fs.results.glob("*.json")) == []
    assert tree_digest(hint_fs.tasks) == tasks_before
    assert hint_fs.state_path.read_bytes() == state_before

    caplog.clear()

    second = orch.run_iteration(step["last_idle_log_at"], step["refill_hint_state"])

    # 下一轮同状态：节流状态跨轮传递 ⇒ 不重复刷屏
    assert second["outcome"] == "idle"
    assert hint_lines(caplog, orch.PLANNER_REFILL_REQUIRED_CODE) == []
    assert second["refill_hint_state"]["signature"] == step["refill_hint_state"]["signature"]


def test_refill_facts_unavailable_degrades_without_blocking(
    hint_fs: HintFS,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(orch, "repo_scoped_import", lambda name: None)

    assert orch.planner_refill_facts() is None
    assert orch.mirror_planner_refill_request({"schema": "unused"}) is None

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        report = orch.planner_refill_report(
            context=orch.REFILL_HINT_CONTEXT_POST_PUSH, throttle=False
        )

    assert report == {
        "payload": None,
        "signature": None,
        "emitted": False,
        "line": None,
        "emitted_at": None,
    }

    # 事实不可读时 idle 路径照常返回 idle（只读提示降级，绝不阻塞队列）
    seed_queue(hint_fs, ["GOLD-261"], extras={"GOLD-261": {"auto_start": False}})

    monkeypatch.setattr(orch, "git", HintGit())

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("Cline must not run")

    monkeypatch.setattr(orch, "run_cline", refuse)

    step = orch.run_iteration(None)

    assert step["outcome"] == "idle"
    assert step["refill_hint_state"] == {"signature": None, "emitted_at": None}


# ============================================================
# 4. 只读保证与 Executor 权限守卫
# ============================================================


def test_hints_never_write_tasks_results_state_or_ledger(
    hint_fs: HintFS,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_queue(hint_fs, ["GOLD-251"])

    ledger = hint_fs.state_dir / "GPT_REVIEW_LEDGER.json"

    ledger.write_text('{"schema": "gold-ai/review-ledger/v1"}\n', encoding="utf-8")

    tasks_before = tree_digest(hint_fs.tasks)
    results_before = tree_digest(hint_fs.results)
    state_before = hint_fs.state_path.read_bytes()
    ledger_before = ledger.read_bytes()

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        orch.planner_refill_report(context=orch.REFILL_HINT_CONTEXT_IDLE, throttle=False)

    assert tree_digest(hint_fs.tasks) == tasks_before
    assert tree_digest(hint_fs.results) == results_before
    assert hint_fs.state_path.read_bytes() == state_before
    assert ledger.read_bytes() == ledger_before

    # 唯一写操作是受控 runtime 镜像
    assert (hint_fs.runtime / orch.REFILL_REQUEST_RUNTIME_NAME).exists()


def test_mirror_refill_request_is_fail_closed_outside_runtime(
    hint_fs: HintFS,
) -> None:
    payload = orch.planner_refill_facts()

    assert payload is not None

    # 1) 真实仓库：review ledger / PROJECT_STATE / tasks 一律拒写，且逐字节不变
    real_targets = (
        REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json",
        REPO_ROOT / ".ai" / "PROJECT_STATE.json",
        REPO_ROOT / ".ai" / "tasks" / "GOLD-999.json",
    )

    for target in real_targets:
        before = target.read_bytes() if target.exists() else None

        assert (
            orch.mirror_planner_refill_request(payload, root=REPO_ROOT, target=target)
            is None
        ), target

        assert (target.read_bytes() if target.exists() else None) == before, target

    # 2) 假仓库根：tasks / results / PROJECT_STATE / tracked 业务文件同样 fail-closed
    ledger = hint_fs.state_dir / "GPT_REVIEW_LEDGER.json"

    ledger.write_text('{"schema": "gold-ai/review-ledger/v1"}\n', encoding="utf-8")

    existing_before = {
        hint_fs.state_path: hint_fs.state_path.read_bytes(),
        ledger: ledger.read_bytes(),
    }

    forbidden_targets = (
        hint_fs.tasks / "GOLD-999.json",
        hint_fs.results / "GOLD-999.json",
        hint_fs.root / "PROGRESS_LOG.md",
        hint_fs.root / "pyproject.toml",
        hint_fs.state_path,
    )

    for target in forbidden_targets:
        assert (
            orch.mirror_planner_refill_request(payload, root=hint_fs.root, target=target)
            is None
        ), target

    for target in forbidden_targets:
        assert not target.exists() or target in existing_before, target

    for path, before in existing_before.items():
        assert path.read_bytes() == before, path

    # 3) 唯一允许的写入位置：runtime 受控路径
    allowed = orch.mirror_planner_refill_request(
        payload,
        root=hint_fs.root,
        target=hint_fs.runtime / "manual_refill.json",
    )

    assert allowed is not None
    assert allowed.name == "manual_refill.json"
    assert json.loads(allowed.read_text(encoding="utf-8"))["read_only"] is True


def test_executor_still_rejects_planning_and_refill_apis() -> None:
    public = {name for name in dir(orch) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    for forbidden in FORBIDDEN_PLANNING_API:
        assert f"def {forbidden}(" not in MODULE_SOURCE, forbidden

    # 只读提示 API 存在，且只是「读 + 报告」
    for name in (
        "planner_refill_facts",
        "planner_refill_report",
        "planner_refill_idle_hint",
        "mirror_planner_refill_request",
        "should_emit_refill_hint",
    ):
        assert name in public, name

    assert orch.PLANNER_REFILL_REQUIRED_CODE == "GPT_PLANNER_REFILL_REQUIRED"
    assert orch.REFILL_HINT_CONTEXT_POST_PUSH == "post_successful_commit_push"
    assert orch.REFILL_HINT_CONTEXT_IDLE == "idle_no_runnable_task"
    assert orch.REFILL_REQUEST_RUNTIME_NAME == "planner_refill_request.json"

    contract = planner.role_contract()

    assert contract["executor_can_refill_rolling_queue"] is False
    assert contract["executor_can_modify_project_state"] is False
    assert contract["phase_transition_requires_human_gate"] is True


def test_mirror_write_failure_never_breaks_the_hint(
    hint_fs: HintFS,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """镜像只是人工诊断副本：写失败也必须降级为日志，绝不抛出、绝不影响队列。"""

    class FailingSnapshotOutput:
        def resolve_output_target(
            self, root: Path, target: Path
        ) -> tuple[Path, None]:
            del root
            return Path(target), None

        def write_snapshot_output(self, target: Path, rendered: str) -> None:
            del target, rendered
            raise OSError("disk full")

        def render_planner_refill_request(self, payload: dict[str, Any]) -> str:
            del payload
            return "{}\n"

    real_import = orch.repo_scoped_import

    def fake_import(name: str) -> Any:
        if name.endswith("planner_snapshot_output"):
            return FailingSnapshotOutput()
        return real_import(name)

    monkeypatch.setattr(orch, "repo_scoped_import", fake_import)

    assert orch.mirror_planner_refill_request({"schema": "unused"}) is None

    seed_queue(hint_fs, ["GOLD-281"])

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        report = orch.planner_refill_report(
            context=orch.REFILL_HINT_CONTEXT_IDLE, throttle=False
        )

    assert report["emitted"] is True
    assert orch.PLANNER_REFILL_REQUIRED_CODE in caplog.text


def test_hint_signature_is_deterministic_and_wall_clock_free(
    hint_fs: HintFS,
) -> None:
    seed_queue(hint_fs, ["GOLD-271", "GOLD-272"])

    first = orch.planner_refill_facts()
    second = orch.planner_refill_facts()

    assert first is not None
    assert second is not None

    assert orch.planner_refill_hint_signature(first) == orch.planner_refill_hint_signature(
        second
    )
    assert "generated_at" not in orch.planner_refill_hint_signature(first)
    assert first["facts_digest"] == second["facts_digest"]


def test_repo_scoped_import_loads_readonly_modules_and_restores_sys_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "_REPO_SCOPED_MODULES", {})

    before = list(sys.path)

    refill_module = orch.repo_scoped_import("orchestrator.planner_refill_request")

    assert refill_module is not None
    assert hasattr(refill_module, "build_planner_refill_request")

    # 导入本身绝不污染 sys.path，而且是幂等的（只执行一次模块级代码）
    assert sys.path == before
    assert orch.repo_scoped_import("orchestrator.planner_refill_request") is refill_module
