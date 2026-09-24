"""GOLD-022：异常中断任务的安全现场恢复状态机回归测试。

覆盖：
1) crash / restart：可证明属于同一 task 的中断现场 → snapshot → 校验 → 清理 → 重试同一 task；
2) dirty user change / 证据不足 → 严格停线，绝不 reset/clean，绝不写 result；
3) snapshot failure / 篡改 / 与工作区不一致 → 拒绝清理并停线；
4) stale / active / 来源不明 lock 的判定与留痕；
5) snapshot 真实可恢复（临时真实 git 仓库 round trip）。

所有测试都在 tmp_path 内构造 fake git 或临时仓库，
绝不触碰真实工作区，也绝不访问真实远端。
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator import ai_orchestrator as orch


class RecoveryGit:
    """脚本化 git fake：只记录命令，绝不访问真实仓库 / 远端。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.status_lines: list[str] = [" M app.py"]
        self.patch_text = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        self.branch = "cline-agent"
        self.head = "c" * 40
        self.untracked_files: list[str] = []
        self.reverse_check_rc = 0
        self.reverse_check_stderr = "boom"
        self.ahead = "0"

    def _ok(self, stdout: str = "") -> dict[str, object]:
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
        }

    def _fail(self, stderr: str, returncode: int = 1) -> dict[str, object]:
        return {
            "returncode": returncode,
            "stdout": "",
            "stderr": stderr,
            "timed_out": False,
        }

    def __call__(self, command: str, timeout: int = 120) -> dict[str, object]:
        del timeout
        self.commands.append(command)

        if command.startswith("status --porcelain") or command.startswith(
            "status --short"
        ):
            return self._ok("".join(f"{line}\n" for line in self.status_lines))

        if command == "rev-parse HEAD":
            return self._ok(f"{self.head}\n")

        if command.startswith("branch --show-current"):
            return self._ok(f"{self.branch}\n")

        if command == "diff --binary HEAD":
            return self._ok(self.patch_text)

        if command == "diff --stat":
            return self._ok("app.py | 1 +\n")

        if command.startswith("ls-files --others --exclude-standard"):
            return self._ok("".join(f"{item}\n" for item in self.untracked_files))

        if command.startswith("apply --check --reverse --binary"):
            if self.reverse_check_rc == 0:
                return self._ok()
            return self._fail(self.reverse_check_stderr, self.reverse_check_rc)

        if command.startswith("apply --binary"):
            return self._ok()

        if command.startswith("reset --hard HEAD"):
            return self._ok()

        if command.startswith("clean -fd"):
            # 模拟真实清理结果：tracked 复位 + untracked 删除 ⇒ 工作区变干净。
            self.status_lines = []
            return self._ok()

        if command.startswith("add -A") or command.startswith("commit "):
            return self._ok()

        if command.startswith("push "):
            return self._ok()

        if command.startswith("rev-list --count"):
            return self._ok(f"{self.ahead}\n")

        if command.startswith("pull --rebase"):
            return self._ok()

        raise AssertionError(f"unexpected git command: {command}")


@pytest.fixture
def recovery_fs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RecoveryGit:
    """把 Orchestrator 的 runtime / tasks / results 全部重定向到 tmp_path。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"
    state_dir = tmp_path / "runtime" / "tasks"
    recovery_dir = tmp_path / "runtime" / "recovery"

    for directory in (tasks, results, state_dir, recovery_dir):
        directory.mkdir(parents=True, exist_ok=True)

    fake = RecoveryGit()

    monkeypatch.setattr(orch, "ROOT", tmp_path)
    monkeypatch.setattr(orch, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(orch, "TASK_STATE_DIR", state_dir)
    monkeypatch.setattr(orch, "RECOVERY_DIR", recovery_dir)
    monkeypatch.setattr(
        orch, "RECOVERY_STATE_FILE", recovery_dir / "recovery_state.json"
    )
    monkeypatch.setattr(orch, "RECOVERY_LOCK_ARCHIVE_DIR", recovery_dir / "locks")
    monkeypatch.setattr(orch, "LOCK_FILE", tmp_path / "orchestrator.lock")
    monkeypatch.setattr(orch, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(orch, "git", fake)
    monkeypatch.setattr(orch, "is_pid_running", lambda pid: False)
    monkeypatch.setattr(orch, "running_process_image", lambda pid: None)
    monkeypatch.setattr(orch, "_last_worktree_block_log_at", None)
    monkeypatch.setattr(orch, "_lock_owned", False)

    return fake


def write_task(tasks: Path, task_id: str, **extra: object) -> Path:
    path = tasks / f"{task_id}.json"
    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, **extra}),
        encoding="utf-8",
    )
    return path


def write_result(results: Path, task_id: str, status: str) -> Path:
    path = results / f"{task_id}.json"
    path.write_text(
        json.dumps({"task_id": task_id, "status": status}),
        encoding="utf-8",
    )
    return path


def write_running_attempt(
    state_dir: Path,
    task_id: str,
    *,
    head: str,
    pid: int = 919191,
    branch: str = "cline-agent",
    attempt: int = 1,
    **extra: object,
) -> Path:
    """写入一条「attempt 正在执行」的 runtime 现场基线证据。"""

    path = state_dir / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": "running",
                "updated_at": "2026-09-23T11:00:01+08:00",
                "attempt": attempt,
                "max_attempts": 3,
                "started_at": "2026-09-23T11:00:00+08:00",
                "evidence_schema": orch.ATTEMPT_EVIDENCE_SCHEMA,
                "pid": pid,
                "branch": branch,
                "head": head,
                "worktree_clean": True,
                "baseline_at": "2026-09-23T11:00:00+08:00",
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return path


def recovery_events(tmp_path: Path) -> list[dict[str, object]]:
    state = json.loads(
        (tmp_path / "runtime" / "recovery" / "recovery_state.json").read_text(
            encoding="utf-8"
        )
    )
    return state["events"]


def cleanup_commands(fake: RecoveryGit) -> list[str]:
    """所有可能破坏现场的 git 命令（reset / clean / stash）。"""

    return [
        command
        for command in fake.commands
        if command.startswith("reset ")
        or command.startswith("clean ")
        or command.startswith("stash")
    ]


def snapshot_directories(tmp_path: Path) -> list[Path]:
    recovery_dir = tmp_path / "runtime" / "recovery"

    if not recovery_dir.exists():
        return []

    return [path for path in sorted(recovery_dir.iterdir()) if path.is_dir()]


# ============================================================
# 1) 工作区分类：可证明的中断现场 vs 未知修改
# ============================================================


def test_dirty_worktree_without_attempt_evidence_is_unknown(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")

    assessment, reason, state = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "no running attempt record" in reason
    assert state is None


def test_clean_worktree_needs_no_recovery(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    recovery_fs.status_lines = []
    write_task(tmp_path / "tasks", "GOLD-101")

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_CONTINUE
    assert recovery["status"] == orch.RECOVERY_OUTCOME_CLEAN
    assert cleanup_commands(recovery_fs) == []
    assert not (tmp_path / "runtime" / "recovery" / "recovery_state.json").exists()


def test_unknown_dirty_worktree_is_never_cleaned(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_STOP
    assert recovery["status"] == orch.RECOVERY_OUTCOME_MANUAL

    # 绝不 reset/clean，绝不 snapshot，绝不写 result（也绝不提交）
    assert cleanup_commands(recovery_fs) == []
    assert snapshot_directories(tmp_path) == []
    assert list((tmp_path / "results").glob("*.json")) == []

    event = recovery_events(tmp_path)[-1]
    assert event["kind"] == orch.RECOVERY_EVENT_BLOCKED
    assert event["decision"] == "manual_intervention"
    assert event["cleaned"] is False
    assert event["dirty_files"] == [" M app.py"]


# ============================================================
# 2) crash / restart：可证明的中断现场 → snapshot → 校验 → 清理
# ============================================================


def _prepare_interrupted_scene(
    fake: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=fake.head,
    )
    fake.status_lines = [" M app.py", "?? new_file.py"]
    fake.untracked_files = ["new_file.py"]
    (tmp_path / "new_file.py").write_text("print('recovery')\n", encoding="utf-8")


def test_provable_interrupted_scene_snapshots_verifies_then_cleans(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    _prepare_interrupted_scene(recovery_fs, tmp_path)

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_CONTINUE
    assert recovery["status"] == orch.RECOVERY_OUTCOME_RECOVERED
    assert cleanup_commands(recovery_fs) == ["reset --hard HEAD", "clean -fd"]

    # 恢复绝不是任务完成：绝不写 result
    assert list((tmp_path / "results").glob("*.json")) == []

    snapshot = tmp_path / str(recovery["snapshot"])
    assert (snapshot / "changes.patch").read_text(
        encoding="utf-8"
    ) == recovery_fs.patch_text
    assert (snapshot / "untracked" / "new_file.py").read_text(
        encoding="utf-8"
    ) == "print('recovery')\n"

    metadata = json.loads((snapshot / "recovery.json").read_text(encoding="utf-8"))
    assert metadata["failure_class"] == orch.INTERRUPTED_FAILURE_CLASS
    assert metadata["failure_code"] == orch.INTERRUPTED_FAILURE_CODE
    assert metadata["tracked_files"] == ["app.py"]
    assert metadata["untracked_files"] == ["new_file.py"]
    assert metadata["untracked_sha256"] == {
        "new_file.py": orch.sha256_file(snapshot / "untracked" / "new_file.py")
    }

    # 清理之前必须已经证明 snapshot 可恢复
    assert orch.verify_recovery_snapshot(snapshot)[0] is True

    event = recovery_events(tmp_path)[-1]
    assert event["kind"] == orch.RECOVERY_EVENT_WORKTREE
    assert event["decision"] == "auto_recover"
    assert event["snapshot_verified"] is True
    assert event["cleaned"] is True
    assert event["dirty_files"] == [" M app.py", "?? new_file.py"]
    assert event["evidence"] == {
        "pid": 919191,
        "branch": recovery_fs.branch,
        "head": recovery_fs.head,
        "started_at": "2026-09-23T11:00:00+08:00",
        "evidence_schema": orch.ATTEMPT_EVIDENCE_SCHEMA,
    }

    # 证据一次性消费：中断证据失效，同一 task 重新从 attempt 1 开始
    state = json.loads(
        (tmp_path / "runtime" / "tasks" / "GOLD-101.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "recovered"
    assert state["recovery_snapshot"] == recovery["snapshot"]
    assert state["failure_code"] == orch.INTERRUPTED_FAILURE_CODE
    assert orch.running_attempt_states() == []


def test_recovered_evidence_cannot_clean_a_later_dirty_scene(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    _prepare_interrupted_scene(recovery_fs, tmp_path)

    first = orch.recover_interrupted_worktree()
    assert first["status"] == orch.RECOVERY_OUTCOME_RECOVERED

    # 清理之后，同一工作区又被人工改脏：此时证据已经消费，必须停线
    recovery_fs.status_lines = [" M app.py"]

    second = orch.recover_interrupted_worktree()

    assert second["action"] == orch.ITERATION_STOP
    assert second["status"] == orch.RECOVERY_OUTCOME_MANUAL
    assert cleanup_commands(recovery_fs).count("reset --hard HEAD") == 1
    assert cleanup_commands(recovery_fs).count("clean -fd") == 1
    assert len(snapshot_directories(tmp_path)) == 1




# ============================================================
# 3) 证据不足的每一种情形都必须 fail-closed
# ============================================================


def test_running_attempt_for_another_task_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")
    write_task(tmp_path / "tasks", "GOLD-102")
    write_result(tmp_path / "results", "GOLD-101", "completed")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
    )

    assessment, reason, state = orch.interrupted_scene_assessment("GOLD-102")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "running attempt record is GOLD-101" in reason
    assert "GOLD-102" in reason
    assert state is None


def test_multiple_running_attempt_records_fail_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "runtime" / "tasks"
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(state_dir, "GOLD-101", head=recovery_fs.head)
    write_running_attempt(state_dir, "GOLD-102", head=recovery_fs.head)

    assessment, reason, state = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "2 running attempt records" in reason
    assert state is None


def test_attempt_owner_still_running_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "is_pid_running", lambda pid: True)
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
    )

    assessment, reason, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "is still running" in reason


def test_head_drift_after_crash_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head="d" * 40,
    )

    assessment, reason, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "HEAD changed since attempt start" in reason


def test_branch_drift_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
        branch="other-branch",
    )

    assessment, reason, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "branch changed since attempt start" in reason


def test_legacy_running_state_without_evidence_schema_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "runtime" / "tasks"
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(state_dir, "GOLD-101", head=recovery_fs.head)

    state = json.loads((state_dir / "GOLD-101.json").read_text(encoding="utf-8"))
    state.pop("evidence_schema")
    (state_dir / "GOLD-101.json").write_text(json.dumps(state), encoding="utf-8")

    assessment, reason, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "no compatible evidence schema" in reason


@pytest.mark.parametrize("field", ["pid", "head", "branch", "started_at", "attempt"])
def test_running_state_missing_baseline_field_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    field: str,
) -> None:
    state_dir = tmp_path / "runtime" / "tasks"
    write_task(tmp_path / "tasks", "GOLD-101")
    write_running_attempt(state_dir, "GOLD-101", head=recovery_fs.head)

    state = json.loads((state_dir / "GOLD-101.json").read_text(encoding="utf-8"))
    state.pop(field)
    (state_dir / "GOLD-101.json").write_text(json.dumps(state), encoding="utf-8")

    assessment, _, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN


def test_terminal_result_for_recorded_task_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")
    write_result(tmp_path / "results", "GOLD-101", "completed")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
    )

    assessment, reason, _ = orch.interrupted_scene_assessment("GOLD-101")

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "already has a terminal result" in reason


def test_no_runnable_task_fails_closed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
    )

    assessment, reason, _ = orch.interrupted_scene_assessment(None)

    assert assessment == orch.WORKTREE_UNKNOWN
    assert "no runnable task" in reason


def test_parse_status_line_handles_rename_and_short_input() -> None:
    assert orch.parse_status_line(" M app.py") == (" M", "app.py")
    assert orch.parse_status_line("?? new.py") == ("??", "new.py")
    assert orch.parse_status_line("R  old.py -> new.py") == ("R ", "new.py")
    assert orch.parse_status_line(" M") is None
    assert orch.parse_status_line("") is None

    tracked, untracked = orch.split_dirty_paths(
        [" M app.py", "?? new.py", "R  a.py -> b.py", ""]
    )

    assert tracked == ["app.py", "b.py"]
    assert untracked == ["new.py"]



# ============================================================
# 4) snapshot 失败 / 被篡改 / 与工作区不一致 ⇒ 拒绝清理
# ============================================================


def _make_snapshot(
    fake: RecoveryGit,
    tmp_path: Path,
) -> Path:
    fake.untracked_files = ["new_file.py"]
    (tmp_path / "new_file.py").write_text("print('recovery')\n", encoding="utf-8")

    return orch.save_recovery_snapshot(
        "GOLD-101",
        1,
        orch.INTERRUPTED_FAILURE_CLASS,
        orch.INTERRUPTED_FAILURE_CODE,
    )


def test_snapshot_write_failure_stops_line_without_cleaning(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_interrupted_scene(recovery_fs, tmp_path)

    def broken_snapshot(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(orch, "save_recovery_snapshot", broken_snapshot)

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_STOP
    assert recovery["status"] == orch.RECOVERY_OUTCOME_SNAPSHOT_FAILED
    assert cleanup_commands(recovery_fs) == []
    assert list((tmp_path / "results").glob("*.json")) == []

    event = recovery_events(tmp_path)[-1]
    assert event["outcome"] == orch.RECOVERY_OUTCOME_SNAPSHOT_FAILED
    assert event["cleaned"] is False


def test_snapshot_verification_failure_blocks_cleanup(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_interrupted_scene(recovery_fs, tmp_path)
    monkeypatch.setattr(
        orch,
        "verify_recovery_snapshot",
        lambda *args, **kwargs: (False, "snapshot patch sha256 mismatch"),
    )

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_STOP
    assert recovery["status"] == orch.RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED
    assert cleanup_commands(recovery_fs) == []
    assert list((tmp_path / "results").glob("*.json")) == []
    # 现场与快照都保留，供人工判断
    assert len(snapshot_directories(tmp_path)) == 1

    event = recovery_events(tmp_path)[-1]
    assert event["outcome"] == orch.RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED
    assert event["snapshot_verified"] is False
    assert event["cleaned"] is False
    assert event["verification"] == "snapshot patch sha256 mismatch"


def test_cleanup_failure_stops_line_after_verified_snapshot(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_interrupted_scene(recovery_fs, tmp_path)

    def broken_reset() -> None:
        raise RuntimeError("git clean -fd 失败")

    monkeypatch.setattr(orch, "reset_task_changes", broken_reset)

    recovery = orch.recover_interrupted_worktree()

    assert recovery["action"] == orch.ITERATION_STOP
    assert recovery["status"] == orch.RECOVERY_OUTCOME_CLEANUP_FAILED
    assert "git clean -fd 失败" in str(recovery["reason"])
    assert len(snapshot_directories(tmp_path)) == 1

    event = recovery_events(tmp_path)[-1]
    assert event["outcome"] == orch.RECOVERY_OUTCOME_CLEANUP_FAILED
    assert event["snapshot_verified"] is True
    assert event["cleaned"] is False



def test_verify_recovery_snapshot_detects_tampered_patch(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    snapshot = _make_snapshot(recovery_fs, tmp_path)

    assert orch.verify_recovery_snapshot(snapshot) == (True, "verified")

    patch_path = snapshot / "changes.patch"
    patch_path.write_text(
        patch_path.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )

    assert orch.verify_recovery_snapshot(snapshot) == (
        False,
        "snapshot patch sha256 mismatch",
    )


def test_verify_recovery_snapshot_detects_missing_untracked_copy(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    snapshot = _make_snapshot(recovery_fs, tmp_path)

    (snapshot / "untracked" / "new_file.py").unlink()

    assert orch.verify_recovery_snapshot(snapshot) == (
        False,
        "snapshot copy missing for untracked file: new_file.py",
    )


def test_verify_recovery_snapshot_detects_changed_untracked_copy(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    snapshot = _make_snapshot(recovery_fs, tmp_path)

    (snapshot / "untracked" / "new_file.py").write_text(
        "print('changed')\n",
        encoding="utf-8",
    )

    assert orch.verify_recovery_snapshot(snapshot) == (
        False,
        "snapshot copy sha256 mismatch for untracked file: new_file.py",
    )


def test_verify_recovery_snapshot_requires_worktree_match(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    snapshot = _make_snapshot(recovery_fs, tmp_path)

    # 默认只校验快照自身完整性
    assert orch.verify_recovery_snapshot(snapshot)[0] is True

    # 与当前工作区不一致（patch 无法反向应用）⇒ 拒绝
    recovery_fs.reverse_check_rc = 1
    recovery_fs.reverse_check_stderr = "error: patch does not apply"

    verified, reason = orch.verify_recovery_snapshot(
        snapshot, require_worktree_match=True
    )

    assert verified is False
    assert "does not match the current worktree" in reason

    # untracked 工作区文件消失同样拒绝
    recovery_fs.reverse_check_rc = 0
    (tmp_path / "new_file.py").unlink()

    verified, reason = orch.verify_recovery_snapshot(
        snapshot, require_worktree_match=True
    )

    assert verified is False
    assert "workspace file missing" in reason


def test_verify_recovery_snapshot_rejects_empty_snapshot(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    recovery_fs.status_lines = []
    recovery_fs.patch_text = ""
    recovery_fs.untracked_files = []

    snapshot = orch.save_recovery_snapshot(
        "GOLD-101",
        1,
        orch.INTERRUPTED_FAILURE_CLASS,
        orch.INTERRUPTED_FAILURE_CODE,
    )

    assert orch.verify_recovery_snapshot(snapshot) == (
        False,
        "snapshot records no recoverable change",
    )


def test_restore_recovery_snapshot_refuses_tampered_snapshot(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    snapshot = _make_snapshot(recovery_fs, tmp_path)
    (snapshot / "untracked" / "new_file.py").unlink()

    with pytest.raises(RuntimeError, match="不可恢复"):
        orch.restore_recovery_snapshot(snapshot)



# ============================================================
# 5) 单实例 lock：stale / active / 来源不明
# ============================================================


def lock_json(tmp_path: Path, **extra: object) -> str:
    payload: dict[str, object] = {
        "schema": orch.LOCK_SCHEMA,
        "pid": 919191,
        "started_at": "2026-09-23T11:00:00+08:00",
        "project": str(tmp_path),
        **extra,
    }
    return json.dumps(payload)


def write_lock(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "orchestrator.lock"
    path.write_text(text, encoding="utf-8")
    return path


def lock_archives(tmp_path: Path) -> list[Path]:
    archive_dir = tmp_path / "runtime" / "recovery" / "locks"

    if not archive_dir.exists():
        return []

    return sorted(archive_dir.glob("*.json"))


def test_stale_lock_is_archived_and_reclaimed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    lock_path = write_lock(tmp_path, lock_json(tmp_path, pid=919191))

    orch.acquire_lock()

    assert orch._lock_owned is True

    created = json.loads(lock_path.read_text(encoding="utf-8"))
    assert created["schema"] == orch.LOCK_SCHEMA
    assert created["pid"] == os.getpid()
    assert created["project"] == str(tmp_path)

    archives = lock_archives(tmp_path)
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["pid"] == 919191
    assert archived["started_at"] == "2026-09-23T11:00:00+08:00"

    event = recovery_events(tmp_path)[-1]
    assert event["kind"] == orch.RECOVERY_EVENT_LOCK
    assert event["decision"] == "auto_recover"
    assert event["cleaned"] is True
    assert event["evidence"]["pid"] == 919191
    assert event["archive"] == str(archives[0].relative_to(tmp_path))

    orch.release_lock()

    assert not lock_path.exists()


def test_legacy_stale_lock_without_schema_is_reclaimed(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    payload = json.loads(lock_json(tmp_path, pid=919191))
    payload.pop("schema")
    lock_path = write_lock(tmp_path, json.dumps(payload))

    orch.acquire_lock()

    created = json.loads(lock_path.read_text(encoding="utf-8"))
    assert created["pid"] == os.getpid()
    assert len(lock_archives(tmp_path)) == 1

    orch.release_lock()


def test_active_lock_stops_line_without_deleting_it(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "is_pid_running", lambda pid: True)
    monkeypatch.setattr(orch, "running_process_image", lambda pid: "python.exe")
    text = lock_json(tmp_path, pid=919191)
    lock_path = write_lock(tmp_path, text)

    with pytest.raises(RuntimeError, match="已有 Orchestrator 正在运行") as excinfo:
        orch.acquire_lock()

    assert "919191" in str(excinfo.value)
    assert "python.exe" in str(excinfo.value)
    assert lock_path.read_text(encoding="utf-8") == text
    assert lock_archives(tmp_path) == []
    assert not (tmp_path / "runtime" / "recovery" / "recovery_state.json").exists()


def test_lock_owned_by_current_process_is_active(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    text = lock_json(tmp_path, pid=os.getpid())
    lock_path = write_lock(tmp_path, text)

    with pytest.raises(RuntimeError, match="已有 Orchestrator 正在运行"):
        orch.acquire_lock()

    assert lock_path.read_text(encoding="utf-8") == text



@pytest.mark.parametrize(
    ("text_builder", "expected"),
    [
        (lambda path: "{ not json", "not a readable JSON object"),
        (
            lambda path: json.dumps({"pid": 919191, "started_at": "2026-09-23"}),
            "no project owner",
        ),
        (
            lambda path: json.dumps({"project": str(path), "started_at": "x"}),
            "no usable pid",
        ),
        (
            lambda path: json.dumps(
                {"pid": "abc", "project": str(path), "started_at": "x"}
            ),
            "no usable pid",
        ),
        (
            lambda path: json.dumps(
                {"pid": -5, "project": str(path), "started_at": "x"}
            ),
            "no usable pid",
        ),
        (
            lambda path: json.dumps(
                {"pid": True, "project": str(path), "started_at": "x"}
            ),
            "no usable pid",
        ),
        (
            lambda path: json.dumps(
                {"pid": 919191, "project": str(path.parent), "started_at": "x"}
            ),
            "another project",
        ),
        (
            lambda path: json.dumps(
                {
                    "schema": "weird/v9",
                    "pid": 919191,
                    "project": str(path),
                    "started_at": "x",
                }
            ),
            "schema unknown",
        ),
        (
            lambda path: json.dumps({"pid": 919191, "project": str(path)}),
            "no started_at timestamp",
        ),
    ],
)
def test_unknown_lock_sources_stop_line_without_deleting(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    text_builder: object,
    expected: str,
) -> None:
    text = str(text_builder(tmp_path))  # type: ignore[operator]
    lock_path = write_lock(tmp_path, text)

    with pytest.raises(RuntimeError, match="来源不明") as excinfo:
        orch.acquire_lock()

    assert expected in str(excinfo.value)
    assert lock_path.read_text(encoding="utf-8") == text
    assert lock_archives(tmp_path) == []

    event = recovery_events(tmp_path)[-1]
    assert event["kind"] == orch.RECOVERY_EVENT_LOCK
    assert event["decision"] == "manual_intervention"
    assert event["cleaned"] is False


def test_lock_liveness_probe_failure_is_treated_as_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测失败 = 无法证明 owner 不存在 ⇒ 停线（绝不按「已死」放行）。

    本用例刻意不使用 `recovery_fs`：需要真实（未被替换的）`is_pid_running`。
    """

    recovery_dir = tmp_path / "runtime" / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orch, "ROOT", tmp_path)
    monkeypatch.setattr(orch, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(orch, "TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(orch, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(orch, "TASK_STATE_DIR", tmp_path / "runtime" / "tasks")
    monkeypatch.setattr(orch, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(orch, "LOCK_FILE", tmp_path / "orchestrator.lock")
    monkeypatch.setattr(
        orch, "RECOVERY_STATE_FILE", recovery_dir / "recovery_state.json"
    )
    monkeypatch.setattr(orch, "RECOVERY_LOCK_ARCHIVE_DIR", recovery_dir / "locks")
    monkeypatch.setattr(orch, "_lock_owned", False)
    monkeypatch.setattr(orch, "_last_worktree_block_log_at", None)

    def broken_probe(pid: int) -> str | None:
        raise OSError("tasklist 不可用")

    monkeypatch.setattr(orch, "running_process_image", broken_probe)

    text = lock_json(tmp_path, pid=919191)
    lock_path = write_lock(tmp_path, text)

    # 探测失败 ⇒ 无法证明 owner 不存在 ⇒ 按存活处理
    assert orch.is_pid_running(919191) is True

    with pytest.raises(RuntimeError, match="已有 Orchestrator 正在运行"):
        orch.acquire_lock()

    assert lock_path.read_text(encoding="utf-8") == text
    assert lock_archives(tmp_path) == []


def test_is_pid_running_treats_unusable_pid_as_not_running() -> None:
    assert orch.is_pid_running(0) is False
    assert orch.is_pid_running(-1) is False
    assert orch.is_pid_running(None) is False
    assert orch.is_pid_running(os.getpid()) is True


# ============================================================
# GOLD-040：跨平台 PID 存活探测（Windows tasklist / POSIX os.kill 同一三态语义）
# ============================================================


def posix_os_shim(kill: object) -> object:
    """构造「POSIX 形态」的 ``os`` 替身：只改 ``name`` 与 ``kill``，其余转发真实 os。

    这样在 Windows 上也能验证 POSIX 分支的语义，且不触碰真实进程。
    """

    class PosixOS:
        name = "posix"

        def __init__(self, kill_callable: object) -> None:
            self.kill = kill_callable

        def __getattr__(self, item: str) -> object:
            return getattr(os, item)

    return PosixOS(kill)


def test_posix_liveness_probe_sees_missing_process_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX：``ProcessLookupError`` 才算「确实不存在」（不是探测失败）。"""

    def kill(pid: int, sig: int) -> None:
        del pid, sig
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(orch, "os", posix_os_shim(kill))

    assert orch.posix_process_image(919191) is None
    assert orch.is_pid_running(919191) is False


def test_posix_liveness_probe_failure_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX：``os.kill`` 异常（无法证明不存在）必须 fail-safe 按存活处理。"""

    def kill(pid: int, sig: int) -> None:
        del pid, sig
        raise OSError(errno.EINVAL, "invalid probe")

    monkeypatch.setattr(orch, "os", posix_os_shim(kill))

    assert orch.is_pid_running(919191) is True


def test_posix_liveness_probe_permission_error_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX：``EPERM`` 只能说明「查不到」，不能说明「不存在」。"""

    def kill(pid: int, sig: int) -> None:
        del pid, sig
        raise PermissionError(errno.EPERM, "not permitted")

    monkeypatch.setattr(orch, "os", posix_os_shim(kill))

    assert orch.posix_process_image(919191) == orch.POSIX_IMAGE_UNKNOWN
    assert orch.is_pid_running(919191) is True


def test_posix_liveness_probe_returns_stable_placeholder_for_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX：探测成功（签名 0）⇒ 绝不返回 None（None 语义是「确实不存在」）。"""

    calls: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr(orch, "os", posix_os_shim(kill))

    image = orch.posix_process_image(4242)

    assert calls == [(4242, 0)]
    assert image is not None
    assert image != ""
    assert orch.is_pid_running(4242) is True


def test_running_process_image_dispatches_by_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个平台共用同一三态契约：分派按 ``os.name``，绝不互相吞掉探测失败。"""

    seen: list[tuple[str, int]] = []

    def windows_probe(pid: int) -> str | None:
        seen.append(("windows", pid))
        return "python.exe"

    def posix_probe(pid: int) -> str | None:
        seen.append(("posix", pid))
        return None

    monkeypatch.setattr(orch, "windows_process_image", windows_probe)
    monkeypatch.setattr(orch, "posix_process_image", posix_probe)

    class PosixOS:
        name = "posix"

    class WindowsOS:
        name = "nt"

    monkeypatch.setattr(orch, "os", PosixOS())

    assert orch.running_process_image(4321) is None
    assert seen[-1] == ("posix", 4321)

    monkeypatch.setattr(orch, "os", WindowsOS())

    assert orch.running_process_image(4321) == "python.exe"
    assert seen[-1] == ("windows", 4321)


@pytest.mark.skipif(os.name != "nt", reason="tasklist 只在 Windows 上存在")
def test_is_pid_running_uses_exact_pid_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompleted:
        returncode = 0
        stdout = (
            "python.exe                   45678 Console    1     10,000 K\n"
        )
        stderr = ""

    class FakeSubprocess:
        SubprocessError = subprocess.SubprocessError

        @staticmethod
        def run(*args: object, **kwargs: object) -> FakeCompleted:
            return FakeCompleted()

    monkeypatch.setattr(orch, "subprocess", FakeSubprocess())

    # 只有 PID 列精确相等才算命中（45678 不应命中 4567）
    assert orch.is_pid_running(45678) is True
    assert orch.is_pid_running(4567) is False


def test_classify_lock_state_without_lock_file_is_unknown(
    recovery_fs: RecoveryGit,
) -> None:
    state, reason, payload = orch.classify_lock_state()

    assert state == orch.LOCK_UNKNOWN
    assert reason == "lock file missing"
    assert payload is None


def test_release_lock_never_removes_a_foreign_lock(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    text = lock_json(tmp_path, pid=919191)
    lock_path = write_lock(tmp_path, text)

    orch.release_lock()

    assert lock_path.read_text(encoding="utf-8") == text

    orch.acquire_lock()
    orch.release_lock()

    assert not lock_path.exists()



# ============================================================
# 6) run_iteration：恢复后重试同一 task / 未知修改停线
# ============================================================


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


def test_run_iteration_recovers_interrupted_scene_then_reruns_same_task(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_file = write_task(
        tmp_path / "tasks", "GOLD-101", validation_commands=["pytest -q"]
    )
    _prepare_interrupted_scene(recovery_fs, tmp_path)

    calls: list[Path] = []

    def fake_run_cline(task_path: Path) -> dict[str, object]:
        calls.append(task_path)
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "summary": {"finish_reason": "completed", "model": "deepseek-flash"},
        }

    monkeypatch.setattr(orch, "run_cline", fake_run_cline)
    monkeypatch.setattr(orch, "run_validations", lambda task: passing_validation())
    monkeypatch.setattr(orch, "get_changed_files", lambda: [" M app.py"])
    monkeypatch.setattr(orch, "get_diff_stat", lambda: "app.py | 1 +")
    monkeypatch.setattr(orch, "REMOTE_NAME", "origin")
    monkeypatch.setattr(orch, "REQUIRED_BRANCH", "cline-agent")

    step = orch.run_iteration(None)

    assert step["action"] == orch.ITERATION_CONTINUE
    assert step["outcome"] == "completed"

    # 清理之后重试的是**同一** task（不是下一个 task）
    assert calls == [task_file]
    assert cleanup_commands(recovery_fs) == ["reset --hard HEAD", "clean -fd"]

    result = json.loads(
        (tmp_path / "results" / "GOLD-101.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "completed"
    assert result["attempt_count"] == 1
    assert result["attempts"][0]["cline_finish_reason_raw"] == "completed"


def test_run_iteration_stops_on_unknown_dirty_worktree(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_task(tmp_path / "tasks", "GOLD-101")

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("Cline must not run on an unattributable dirty worktree")

    monkeypatch.setattr(orch, "run_cline", refuse)

    step = orch.run_iteration(None)

    assert step["action"] == orch.ITERATION_SLEEP
    assert step["outcome"] == orch.RECOVERY_OUTCOME_MANUAL
    assert cleanup_commands(recovery_fs) == []
    assert list((tmp_path / "results").glob("*.json")) == []


# ============================================================
# 7) 达到最大尝试次数时同样绝不清理无法证明的现场
# ============================================================


def write_exhausted_result(results: Path, task_id: str) -> Path:
    """写入一份「attempt 已用尽」的非终态 result（历史结果绝不回写）。"""

    path = results / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": "running",
                "max_attempts": 1,
                "attempts": [{"attempt": 1}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_max_attempts_with_unknown_dirty_worktree_defers_without_reset(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_file = write_task(tmp_path / "tasks", "GOLD-101", max_attempts=1)
    result_path = write_exhausted_result(tmp_path / "results", "GOLD-101")

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("Cline must not run")

    monkeypatch.setattr(orch, "run_cline", refuse)

    outcome = orch.process_task(task_file)

    assert outcome == "deferred"
    assert cleanup_commands(recovery_fs) == []
    # 历史 result 绝不因无法证明的现场而被改写
    history = json.loads(result_path.read_text(encoding="utf-8"))
    assert history == {
        "task_id": "GOLD-101",
        "status": "running",
        "max_attempts": 1,
        "attempts": [{"attempt": 1}],
    }

    event = recovery_events(tmp_path)[-1]
    assert event["context"] == "max_attempts_reached"
    assert event["cleaned"] is False
    assert event["dirty_files"] == [" M app.py"]


def test_max_attempts_with_provable_interrupted_scene_is_cleaned(
    recovery_fs: RecoveryGit,
    tmp_path: Path,
) -> None:
    task_file = write_task(tmp_path / "tasks", "GOLD-101", max_attempts=1)
    write_exhausted_result(tmp_path / "results", "GOLD-101")
    write_running_attempt(
        tmp_path / "runtime" / "tasks",
        "GOLD-101",
        head=recovery_fs.head,
    )

    outcome = orch.process_task(task_file)

    assert outcome == "blocked"
    assert cleanup_commands(recovery_fs) == ["reset --hard HEAD", "clean -fd"]

    result = json.loads(
        (tmp_path / "results" / "GOLD-101.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "blocked"
    assert result["execution_outcome"] == "blocked"



# ============================================================
# 8) 真实 git：snapshot 确实可恢复（临时仓库，零真实远端）
# ============================================================


def git_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "GOLD-022 Test",
            "GIT_AUTHOR_EMAIL": "gold-022@example.invalid",
            "GIT_COMMITTER_NAME": "GOLD-022 Test",
            "GIT_COMMITTER_EMAIL": "gold-022@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-global-config"),
        }
    )
    return env


def run_git(cwd: Path, env: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    assert completed.returncode == 0, f"git {' '.join(args)} 失败: {completed.stderr}"

    return completed.stdout.strip()


def test_real_git_snapshot_restore_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """snapshot 必须是**真可恢复**的：清理后能 1:1 还原中断现场。"""

    env = git_env(tmp_path)
    (tmp_path / "empty-global-config").write_text("", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, env, "init", "-b", "cline-agent")

    # .ai/runtime 已忽略 ⇒ `git clean -fd` 不会删除 recovery 副本（与真实仓库一致）
    (repo / ".gitignore").write_text(".ai/runtime/\n", encoding="utf-8")
    (repo / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    run_git(repo, env, "add", "-A")
    run_git(repo, env, "commit", "-m", "baseline")

    recovery_dir = repo / ".ai" / "runtime" / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orch, "ROOT", repo)
    monkeypatch.setattr(orch, "RECOVERY_DIR", recovery_dir)
    monkeypatch.setattr(
        orch, "RECOVERY_STATE_FILE", recovery_dir / "recovery_state.json"
    )

    # 制造中断现场：tracked 修改 + untracked 新文件
    (repo / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    (repo / "new_file.py").write_text("print('recovery')\n", encoding="utf-8")

    expected_app = (repo / "app.py").read_text(encoding="utf-8")
    expected_new = (repo / "new_file.py").read_text(encoding="utf-8")
    dirty_lines = orch.get_git_status_lines()
    assert dirty_lines != []

    snapshot = orch.save_recovery_snapshot(
        "GOLD-101",
        1,
        orch.INTERRUPTED_FAILURE_CLASS,
        orch.INTERRUPTED_FAILURE_CODE,
    )

    assert orch.verify_recovery_snapshot(
        snapshot, require_worktree_match=True
    ) == (True, "verified")

    orch.reset_task_changes()

    assert orch.git_is_dirty() is False
    assert not (repo / "new_file.py").exists()
    assert (repo / "app.py").read_text(encoding="utf-8") != expected_app
    # 清理之后 recovery 副本仍在（ignored 目录不被 clean -fd 删除）
    assert (snapshot / "recovery.json").exists()

    restored = orch.restore_recovery_snapshot(snapshot)

    assert sorted(restored) == ["app.py", "new_file.py"]
    assert (repo / "app.py").read_text(encoding="utf-8") == expected_app
    assert (repo / "new_file.py").read_text(encoding="utf-8") == expected_new
    assert sorted(orch.get_git_status_lines()) == sorted(dirty_lines)

