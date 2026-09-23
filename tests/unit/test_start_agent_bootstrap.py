"""GOLD-021：start_agent 启动前 bootstrap sync 与版本可见性回归测试。

覆盖三层：

1. launcher 契约（`start_agent.bat`）：bootstrap sync 必须在 Python 进程之前，
   失败必须 fail-closed 且不覆盖本地修改；
2. 同步语义（`orchestrator.bootstrap_sync`，fake git）：命令白名单、fail-closed 路径；
3. 真实 git 集成（临时 bare 远端）：同步之后**新起的** Python 进程读到的是新版本，
   dirty worktree / rebase 冲突一律停线且不丢本地 commit/修改。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import bootstrap_sync

ROOT = Path(__file__).resolve().parents[2]

LAUNCHER = ROOT / "start_agent.bat"

BOOTSTRAP_COMMAND = "py -u orchestrator\\bootstrap_sync.py"

ORCHESTRATOR_COMMAND = "py -u orchestrator\\ai_orchestrator.py"

REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="需要本机 git 才能跑 bootstrap sync 集成回归",
)

# 演示用 SHA（只在前两层使用，永不访问真实仓库）。
SHA_OLD = "1" * 40

SHA_NEW = "2" * 40

SHA_OTHER = "3" * 40


# ============================================================
# fake git runner
# ============================================================


def ok(stdout: str = "") -> bootstrap_sync.CommandResult:
    return bootstrap_sync.CommandResult(returncode=0, stdout=stdout)


def failed(stderr: str, returncode: int = 1) -> bootstrap_sync.CommandResult:
    return bootstrap_sync.CommandResult(returncode=returncode, stderr=stderr)


class FakeRunner:
    """脚本化 git runner：只记录命令与 cwd，绝不访问真实仓库。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.cwds: list[Path] = []
        self.responses: dict[str, list[bootstrap_sync.CommandResult]] = {}

    def expect(self, command: str, response: bootstrap_sync.CommandResult) -> None:
        self.responses.setdefault(command, []).append(response)

    def __call__(self, command: str, cwd: Path) -> bootstrap_sync.CommandResult:
        self.commands.append(command)
        self.cwds.append(cwd)

        queue = self.responses.get(command)

        if queue:
            return queue.pop(0)

        if queue is not None:
            raise AssertionError(f"git command called more times than expected: {command}")

        raise AssertionError(f"unexpected git command: {command}")


def assert_commands_are_safe(commands: list[str]) -> None:
    """任何路径都不得出现破坏性 / 覆盖性 git 命令。"""

    joined = " ".join(commands)

    for forbidden in (
        "--force",
        "-f ",
        "reset --hard",
        "clean -",
        "checkout",
        "switch",
        "push ",
        "stash",
    ):
        assert forbidden not in joined, f"{forbidden} 绝不允许出现在 bootstrap sync 中"

    assert joined != ""


def sync_with(runner: FakeRunner, repo: Path) -> bootstrap_sync.SyncOutcome:
    outcome = bootstrap_sync.synchronize(
        root=repo,
        branch="cline-agent",
        remote="origin",
        runner=runner,
    )

    assert all(cwd == repo for cwd in runner.cwds)

    return outcome


def head_read_commands() -> list[str]:
    """只读探测 + fetch 的稳定命令序列（顺序即安全契约）。"""

    return [
        "branch --show-current",
        "rev-parse HEAD",
        "status --porcelain",
        "fetch --prune origin cline-agent",
        "rev-parse origin/cline-agent",
    ]


# ============================================================
# 1) 同步语义（fake git）
# ============================================================


def test_dirty_worktree_fails_closed_before_fetch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("status --porcelain", ok(" M src/keep.py\n?? notes.txt\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY
    assert "2 项" in outcome.reason
    # dirty 时绝不 fetch / merge / rebase（本地修改必须原样保留），
    # 但 branch / HEAD 仍要打进日志，便于人工定位版本。
    assert outcome.branch == "cline-agent"
    assert outcome.head_before == SHA_OLD
    assert runner.commands == ["branch --show-current", "rev-parse HEAD", "status --porcelain"]
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_DIRTY_WORKTREE
    assert_commands_are_safe(runner.commands)


def test_unreadable_worktree_state_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "not-a-repo"

    runner = FakeRunner()
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("status --porcelain", failed("fatal: not a git repository"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_FAILED
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_SYNC_FAILED
    assert runner.commands == ["branch --show-current", "rev-parse HEAD", "status --porcelain"]


def test_wrong_branch_fails_closed_without_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("branch --show-current", ok("main\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_WRONG_BRANCH
    assert outcome.branch == "main"
    assert runner.commands == ["branch --show-current"]
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_WRONG_BRANCH
    assert_commands_are_safe(runner.commands)


def test_up_to_date_does_not_touch_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_OLD}\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is True
    assert outcome.result == bootstrap_sync.SYNC_RESULT_UP_TO_DATE
    assert outcome.head_before == SHA_OLD
    assert outcome.head_after == SHA_OLD
    assert outcome.code_updated is False
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_OK
    assert runner.commands == head_read_commands()
    assert_commands_are_safe(runner.commands)


def test_fast_forward_uses_ff_only_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_NEW}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", ok())
    runner.expect("merge --ff-only origin/cline-agent", ok())
    runner.expect("rev-parse HEAD", ok(f"{SHA_NEW}\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is True
    assert outcome.result == bootstrap_sync.SYNC_RESULT_FAST_FORWARD
    assert outcome.head_after == SHA_NEW
    assert outcome.code_updated is True
    assert runner.commands == [
        *head_read_commands(),
        "merge-base --is-ancestor HEAD origin/cline-agent",
        "merge --ff-only origin/cline-agent",
        "rev-parse HEAD",
    ]
    assert_commands_are_safe(runner.commands)


def test_fast_forward_head_mismatch_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_NEW}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", ok())
    runner.expect("merge --ff-only origin/cline-agent", ok())
    runner.expect("rev-parse HEAD", ok(f"{SHA_OTHER}\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_FAILED
    assert "HEAD 与远端不一致" in outcome.reason
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_SYNC_FAILED


def test_local_ahead_only_never_rebases_or_merges(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_NEW}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_OLD}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", failed("", 1))
    runner.expect("merge-base --is-ancestor origin/cline-agent HEAD", ok())

    outcome = sync_with(runner, repo)

    assert outcome.ok is True
    assert outcome.result == bootstrap_sync.SYNC_RESULT_AHEAD_ONLY
    assert outcome.head_after == SHA_NEW
    assert outcome.code_updated is False
    # 本地领先（例如上轮 push 未完成）→ 不动工作区，交给 Orchestrator 推送
    assert not [c for c in runner.commands if c.startswith("merge --ff-only")]
    assert not [c for c in runner.commands if c.startswith("rebase")]
    assert_commands_are_safe(runner.commands)


def test_diverged_history_uses_rebase_then_verifies_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_NEW}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", failed("", 1))
    runner.expect("merge-base --is-ancestor origin/cline-agent HEAD", failed("", 1))
    runner.expect("rebase origin/cline-agent", ok())
    runner.expect("rev-parse HEAD", ok(f"{SHA_NEW}\n"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is True
    assert outcome.result == bootstrap_sync.SYNC_RESULT_REBASED
    assert outcome.head_after == SHA_NEW
    assert outcome.code_updated is True
    assert runner.commands[-2:] == ["rebase origin/cline-agent", "rev-parse HEAD"]
    assert_commands_are_safe(runner.commands)


def test_rebase_conflict_aborts_and_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_NEW}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", failed("", 1))
    runner.expect("merge-base --is-ancestor origin/cline-agent HEAD", failed("", 1))
    runner.expect(
        "rebase origin/cline-agent",
        failed("CONFLICT (content): Merge conflict in PROGRESS_LOG.md"),
    )
    runner.expect("rebase --abort", ok())

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_REBASE_CONFLICT
    assert "rebase --abort" in outcome.reason
    # 已 abort 回原状：HEAD 视为同步前状态，本地 commit 保留
    assert outcome.head_after == SHA_OLD
    assert outcome.code_updated is False
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_SYNC_FAILED
    assert_commands_are_safe(runner.commands)


def test_rebase_abort_failure_is_reported_for_manual_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", ok(f"{SHA_NEW}\n"))
    runner.expect("merge-base --is-ancestor HEAD origin/cline-agent", failed("", 1))
    runner.expect("merge-base --is-ancestor origin/cline-agent HEAD", failed("", 1))
    runner.expect("rebase origin/cline-agent", failed("CONFLICT"))
    runner.expect("rebase --abort", failed("no rebase in progress"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_REBASE_CONFLICT
    assert "请人工检查" in outcome.reason


def test_fetch_failure_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", failed("Could not resolve host"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_FETCH_FAILED
    assert "Could not resolve host" in outcome.reason
    assert bootstrap_sync.exit_code_for(outcome) == bootstrap_sync.EXIT_SYNC_FAILED
    assert_commands_are_safe(runner.commands)


def test_missing_remote_ref_is_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    runner = FakeRunner()
    runner.expect("status --porcelain", ok(""))
    runner.expect("branch --show-current", ok("cline-agent\n"))
    runner.expect("rev-parse HEAD", ok(f"{SHA_OLD}\n"))
    runner.expect("fetch --prune origin cline-agent", ok())
    runner.expect("rev-parse origin/cline-agent", failed("unknown revision"))

    outcome = sync_with(runner, repo)

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_REMOTE_REF_MISSING
    assert_commands_are_safe(runner.commands)


# ============================================================
# 2) 状态落盘 / 启动日志（不含任何凭据）
# ============================================================


def test_state_file_lands_in_runtime_and_records_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_sync, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(bootstrap_sync, "CLINE_MODEL", "")

    outcome = bootstrap_sync.SyncOutcome(
        result=bootstrap_sync.SYNC_RESULT_FAST_FORWARD,
        ok=True,
        branch="cline-agent",
        head_before=SHA_OLD,
        head_after=SHA_NEW,
        remote="origin",
        remote_sha=SHA_NEW,
        checked_at="2026-09-23T12:00:00+08:00",
    )

    state_path = bootstrap_sync.default_state_path(tmp_path)

    assert state_path == tmp_path / ".ai" / "runtime" / "bootstrap_state.json"

    bootstrap_sync.write_state(state_path, outcome)

    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["result"] == bootstrap_sync.SYNC_RESULT_FAST_FORWARD
    assert state["ok"] is True
    assert state["code_updated"] is True
    assert state["branch"] == "cline-agent"
    assert state["head_before"] == SHA_OLD
    assert state["head_after"] == SHA_NEW
    assert state["provider"] == "deepseek"
    # 临时文件绝不残留
    assert not state_path.with_suffix(state_path.suffix + ".tmp").exists()


def test_state_and_report_never_leak_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-gold-021-should-never-appear"

    monkeypatch.setenv("AI_CLINE_API_KEY", secret)
    monkeypatch.setattr(bootstrap_sync, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(bootstrap_sync, "CLINE_MODEL", "")

    outcome = bootstrap_sync.SyncOutcome(
        result=bootstrap_sync.SYNC_RESULT_UP_TO_DATE,
        ok=True,
        branch="cline-agent",
        head_before=SHA_OLD,
        head_after=SHA_OLD,
        remote_sha=SHA_OLD,
        checked_at="2026-09-23T12:00:00+08:00",
    )

    state_path = bootstrap_sync.default_state_path(tmp_path)

    bootstrap_sync.write_state(state_path, outcome)

    report = bootstrap_sync.render_report(outcome, tmp_path)

    assert secret not in report
    assert secret not in state_path.read_text(encoding="utf-8")
    assert "--key" not in report


def test_report_prints_branch_head_provider_and_sync_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_sync, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(bootstrap_sync, "CLINE_MODEL", "")

    outcome = bootstrap_sync.SyncOutcome(
        result=bootstrap_sync.SYNC_RESULT_FAST_FORWARD,
        ok=True,
        branch="cline-agent",
        head_before=SHA_OLD,
        head_after=SHA_NEW,
        remote_sha=SHA_NEW,
        checked_at="2026-09-23T12:00:00+08:00",
    )

    report = bootstrap_sync.render_report(outcome, tmp_path)

    assert "Branch     : cline-agent" in report
    assert f"HEAD before: {SHA_OLD[:8]}" in report
    assert f"HEAD after : {SHA_NEW[:8]}" in report
    assert "Provider   : deepseek" in report
    assert "Model      : <provider default>" in report
    assert f"Sync result: {bootstrap_sync.SYNC_RESULT_FAST_FORWARD}" in report
    assert "bootstrap sync OK" in report


def test_failed_report_is_explicit_and_mentions_no_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_sync, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(bootstrap_sync, "CLINE_MODEL", "")

    outcome = bootstrap_sync.SyncOutcome(
        result=bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY,
        ok=False,
        reason="工作区存在未提交修改",
        branch="cline-agent",
        head_before=SHA_OLD,
        head_after=SHA_OLD,
        checked_at="2026-09-23T12:00:00+08:00",
    )

    report = bootstrap_sync.render_report(outcome, tmp_path)

    assert "FAILED" in report
    assert "工作区存在未提交修改" in report
    assert "本地修改与本地 commit 一律保留" in report
    assert "start_agent.bat" in report


def test_abbrev_sha_and_exit_code_mapping() -> None:
    assert bootstrap_sync.abbrev_sha(SHA_OLD) == SHA_OLD[:8]
    assert bootstrap_sync.abbrev_sha(None) == "<unknown>"
    assert bootstrap_sync.abbrev_sha("") == "<unknown>"

    dirty = bootstrap_sync.SyncOutcome(result=bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY, ok=False)
    wrong_branch = bootstrap_sync.SyncOutcome(
        result=bootstrap_sync.SYNC_RESULT_WRONG_BRANCH,
        ok=False,
    )
    fetch_failed = bootstrap_sync.SyncOutcome(result=bootstrap_sync.SYNC_RESULT_FETCH_FAILED)
    success = bootstrap_sync.SyncOutcome(result=bootstrap_sync.SYNC_RESULT_UP_TO_DATE, ok=True)

    assert bootstrap_sync.exit_code_for(dirty) == bootstrap_sync.EXIT_DIRTY_WORKTREE
    assert bootstrap_sync.exit_code_for(wrong_branch) == bootstrap_sync.EXIT_WRONG_BRANCH
    assert bootstrap_sync.exit_code_for(fetch_failed) == bootstrap_sync.EXIT_SYNC_FAILED
    assert bootstrap_sync.exit_code_for(success) == bootstrap_sync.EXIT_OK


# ============================================================
# 3) launcher 契约（start_agent.bat）
# ============================================================


def launcher_lines() -> list[str]:
    return LAUNCHER.read_text(encoding="utf-8").splitlines()


def launcher_index(lines: list[str], command: str) -> int:
    indexes = [index for index, line in enumerate(lines) if line.strip() == command]

    assert len(indexes) == 1, f"launcher 必须且只能出现一次: {command}"

    return indexes[0]


def test_launcher_runs_bootstrap_sync_before_python_orchestrator() -> None:
    lines = launcher_lines()

    bootstrap_index = launcher_index(lines, BOOTSTRAP_COMMAND)

    orchestrator_index = launcher_index(lines, ORCHESTRATOR_COMMAND)

    # 启动顺序：bootstrap sync -> Python/Orchestrator load
    assert bootstrap_index < orchestrator_index

    guard = "\n".join(lines[bootstrap_index + 1 : orchestrator_index])

    assert "if errorlevel 1" in guard
    assert "exit /b 1" in guard
    assert "Orchestrator will NOT start" in guard
    assert "pause" in guard


def test_launcher_still_hard_pins_deepseek_provider() -> None:
    lines = launcher_lines()

    pin_index = launcher_index(lines, "set AI_CLINE_PROVIDER=deepseek")

    assert pin_index < launcher_index(lines, BOOTSTRAP_COMMAND)
    assert pin_index < launcher_index(lines, ORCHESTRATOR_COMMAND)


def test_launcher_never_contains_destructive_git_commands() -> None:
    text = "\n".join(launcher_lines())

    for forbidden in ("--force", "reset --hard", "git clean", "git checkout", "git switch"):
        assert forbidden not in text


# ============================================================
# 4) Orchestrator 侧版本可见性（GOLD-021）
# ============================================================


def test_describe_bootstrap_sync_reports_result_and_provider() -> None:
    state = {
        "ok": True,
        "result": bootstrap_sync.SYNC_RESULT_FAST_FORWARD,
        "branch": "cline-agent",
        "head_before": SHA_OLD,
        "head_after": SHA_NEW,
        "provider": "deepseek",
        "model": "",
        "checked_at": "2026-09-23T12:00:00+08:00",
    }

    text = orch.describe_bootstrap_sync(state)

    assert "OK" in text
    assert bootstrap_sync.SYNC_RESULT_FAST_FORWARD in text
    assert "branch=cline-agent" in text
    assert f"head_after={SHA_NEW[:8]}" in text
    assert "provider=deepseek" in text
    assert "model=<provider default>" in text


def test_describe_bootstrap_sync_without_state_is_explicit() -> None:
    assert "not recorded" in orch.describe_bootstrap_sync(None)
    assert "not recorded" in orch.describe_bootstrap_sync("broken")


def test_describe_bootstrap_sync_marks_failed_state() -> None:
    text = orch.describe_bootstrap_sync({"ok": False, "result": "skipped_dirty"})

    assert text.startswith("FAILED")
    assert "skipped_dirty" in text


def test_bootstrap_head_mismatch_detects_post_sync_drift() -> None:
    state = {"ok": True, "head_after": SHA_NEW}

    assert orch.bootstrap_head_mismatch(state, SHA_NEW[:8]) is None
    assert orch.bootstrap_head_mismatch(state, SHA_NEW) is None

    warning = orch.bootstrap_head_mismatch(state, SHA_OTHER[:8])

    assert warning is not None
    assert "不一致" in warning
    assert SHA_NEW[:8] in warning
    assert SHA_OTHER[:8] in warning

    # 无状态 / 无 HEAD / 未记录 head_after 时不得误报
    assert orch.bootstrap_head_mismatch(None, SHA_OTHER[:8]) is None
    assert orch.bootstrap_head_mismatch(state, None) is None
    assert orch.bootstrap_head_mismatch({"ok": True}, SHA_OTHER[:8]) is None


def test_commit_prefix_matches_full_and_short_sha() -> None:
    assert orch.commit_prefix_matches(SHA_NEW, SHA_NEW[:8]) is True
    assert orch.commit_prefix_matches(SHA_NEW[:8], SHA_NEW) is True
    assert orch.commit_prefix_matches(SHA_NEW, SHA_OTHER[:8]) is False
    assert orch.commit_prefix_matches(None, SHA_NEW) is False
    assert orch.commit_prefix_matches("", SHA_NEW) is False


def test_read_bootstrap_sync_state_is_defensive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "bootstrap_state.json"

    monkeypatch.setattr(orch, "BOOTSTRAP_STATE_FILE", state_path)

    assert state_path == orch.BOOTSTRAP_STATE_FILE

    # 文件缺失 / JSON 损坏 → None（绝不抛异常、绝不猜版本）
    assert orch.read_bootstrap_sync_state() is None

    state_path.write_text("{not json", encoding="utf-8")

    assert orch.read_bootstrap_sync_state() is None

    state_path.write_text(json.dumps({"ok": True, "result": "up_to_date"}), encoding="utf-8")

    assert orch.read_bootstrap_sync_state() == {"ok": True, "result": "up_to_date"}


def test_bootstrap_state_path_is_runtime_ignored_file() -> None:
    assert orch.BOOTSTRAP_STATE_FILE == orch.RUNTIME_DIR / "bootstrap_state.json"

    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".ai/runtime/" in ignored


# ============================================================
# 5) 真实 git 集成（临时 bare 远端，零真实网络）
# ============================================================


def git_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)

    env.update(
        {
            "GIT_AUTHOR_NAME": "GOLD-021 Test",
            "GIT_AUTHOR_EMAIL": "gold-021@example.invalid",
            "GIT_COMMITTER_NAME": "GOLD-021 Test",
            "GIT_COMMITTER_EMAIL": "gold-021@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-global-config"),
        }
    )

    return env


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
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


def build_repos(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    """返回 (env, bare origin, seed clone, work clone)，全部位于临时目录内。"""

    env = git_env(tmp_path)

    (tmp_path / "empty-global-config").write_text("", encoding="utf-8")

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"

    git(tmp_path, env, "init", "--bare", "-b", "cline-agent", str(origin))
    git(tmp_path, env, "clone", str(origin), str(seed))

    (seed / "app_version.py").write_text('VERSION = "v1"\n', encoding="utf-8")
    git(seed, env, "add", "-A")
    git(seed, env, "commit", "-m", "v1")
    git(seed, env, "push", "origin", "cline-agent")

    git(tmp_path, env, "clone", str(origin), str(work))

    return env, origin, seed, work


def publish_version(seed: Path, env: dict[str, str], version: str) -> str:
    (seed / "app_version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    git(seed, env, "add", "-A")
    git(seed, env, "commit", "-m", version)
    git(seed, env, "push", "origin", "cline-agent")

    return git(seed, env, "rev-parse", "HEAD")


def fresh_process_version(repo: Path) -> str:
    """在**新起的** Python 进程里读取仓库内的版本模块（模拟 launcher 启动顺序）。

    使用 ``-B``：绝不写 ``__pycache__``，否则会把临时仓库弄脏（与真实仓库的
    ``.gitignore`` 无关，测试必须自己保证工作区清洁）。
    """

    completed = subprocess.run(
        [sys.executable, "-B", "-c", "import app_version; print(app_version.VERSION)"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.returncode == 0, completed.stderr

    return completed.stdout.strip()


def run_bootstrap_cli(repo: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["AI_CLINE_PROVIDER"] = "deepseek"

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "orchestrator.bootstrap_sync",
            "--root",
            str(repo),
            "--branch",
            "cline-agent",
            "--remote",
            "origin",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


@REQUIRES_GIT
def test_bootstrap_sync_makes_next_process_load_new_code(tmp_path: Path) -> None:
    env, _origin, seed, work = build_repos(tmp_path)

    head_before = git(work, env, "rev-parse", "HEAD")

    # 同步之前（旧进程）读到的是旧版本
    assert fresh_process_version(work) == "v1"

    remote_head = publish_version(seed, env, "v2")

    outcome = bootstrap_sync.synchronize(root=work, branch="cline-agent", remote="origin")

    assert outcome.ok is True
    assert outcome.result == bootstrap_sync.SYNC_RESULT_FAST_FORWARD
    assert outcome.head_before == head_before
    assert outcome.head_after == remote_head
    assert outcome.code_updated is True
    assert git(work, env, "rev-parse", "HEAD") == remote_head
    # 关键回归：只有「同步之后新起的进程」才加载到新代码
    assert fresh_process_version(work) == "v2"


@REQUIRES_GIT
def test_bootstrap_sync_is_idempotent_when_already_up_to_date(tmp_path: Path) -> None:
    env, _origin, _seed, work = build_repos(tmp_path)

    head_before = git(work, env, "rev-parse", "HEAD")

    first = bootstrap_sync.synchronize(root=work, branch="cline-agent", remote="origin")

    assert first.ok is True
    assert first.result == bootstrap_sync.SYNC_RESULT_UP_TO_DATE
    assert first.code_updated is False

    second = bootstrap_sync.synchronize(root=work, branch="cline-agent", remote="origin")

    assert second.ok is True
    assert second.result == bootstrap_sync.SYNC_RESULT_UP_TO_DATE
    assert git(work, env, "rev-parse", "HEAD") == head_before
    assert fresh_process_version(work) == "v1"


@REQUIRES_GIT
def test_dirty_worktree_fails_closed_and_preserves_local_edit(tmp_path: Path) -> None:
    env, _origin, seed, work = build_repos(tmp_path)

    target = work / "app_version.py"
    target.write_text('VERSION = "v1-local-edit"\n', encoding="utf-8")
    head_before = git(work, env, "rev-parse", "HEAD")

    publish_version(seed, env, "v2")

    outcome = bootstrap_sync.synchronize(root=work, branch="cline-agent", remote="origin")

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY
    # 本地修改与本地 HEAD 一律保留，且连 fetch 都没有发生
    assert git(work, env, "rev-parse", "HEAD") == head_before
    assert target.read_text(encoding="utf-8") == 'VERSION = "v1-local-edit"\n'
    assert "local-edit" in git(work, env, "diff")
    assert not [command for command in outcome.commands if command.startswith("fetch")]
    assert not [command for command in outcome.commands if command.startswith("merge")]
    assert not [command for command in outcome.commands if command.startswith("rebase")]


@REQUIRES_GIT
def test_rebase_conflict_fails_closed_without_losing_local_commit(tmp_path: Path) -> None:
    env, _origin, seed, work = build_repos(tmp_path)

    (work / "app_version.py").write_text('VERSION = "v1-local"\n', encoding="utf-8")
    git(work, env, "add", "-A")
    git(work, env, "commit", "-m", "local change")
    local_head = git(work, env, "rev-parse", "HEAD")

    publish_version(seed, env, "v2-remote")

    outcome = bootstrap_sync.synchronize(root=work, branch="cline-agent", remote="origin")

    assert outcome.ok is False
    assert outcome.result == bootstrap_sync.SYNC_RESULT_REBASE_CONFLICT
    assert "rebase --abort" in outcome.commands
    # 本地 commit 与工作区都恢复原状，没有半途 rebase
    assert git(work, env, "rev-parse", "HEAD") == local_head
    assert (work / "app_version.py").read_text(encoding="utf-8") == 'VERSION = "v1-local"\n'
    assert git(work, env, "status", "--porcelain") == ""
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()


@REQUIRES_GIT
def test_cli_reports_version_and_writes_runtime_state(tmp_path: Path) -> None:
    env, _origin, seed, work = build_repos(tmp_path)

    publish_version(seed, env, "v2")

    completed = run_bootstrap_cli(work)

    assert completed.returncode == bootstrap_sync.EXIT_OK
    assert "Branch     : cline-agent" in completed.stdout
    assert "Provider   : deepseek" in completed.stdout
    assert f"Sync result: {bootstrap_sync.SYNC_RESULT_FAST_FORWARD}" in completed.stdout
    assert "bootstrap sync OK" in completed.stdout

    state_path = work / ".ai" / "runtime" / "bootstrap_state.json"

    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["ok"] is True
    assert state["result"] == bootstrap_sync.SYNC_RESULT_FAST_FORWARD
    assert state["head_after"] == git(work, env, "rev-parse", "HEAD")
    assert state["provider"] == "deepseek"
    assert state["code_updated"] is True
    # 集成证据：CLI 同步之后的新进程确实加载到新版本
    assert fresh_process_version(work) == "v2"


@REQUIRES_GIT
def test_cli_fails_closed_on_dirty_worktree_with_nonzero_exit_code(tmp_path: Path) -> None:
    env, _origin, _seed, work = build_repos(tmp_path)

    target = work / "app_version.py"
    target.write_text('VERSION = "v1-local-edit"\n', encoding="utf-8")

    assert git(work, env, "status", "--porcelain") != ""

    completed = run_bootstrap_cli(work)

    # launcher 依据该退出码 fail-closed（不会启动 Orchestrator）
    assert completed.returncode == bootstrap_sync.EXIT_DIRTY_WORKTREE
    assert "FAILED" in completed.stdout
    assert bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY in completed.stdout
    assert target.read_text(encoding="utf-8") == 'VERSION = "v1-local-edit"\n'
    assert fresh_process_version(work) == "v1-local-edit"

    state = json.loads((work / ".ai" / "runtime" / "bootstrap_state.json").read_text("utf-8"))

    assert state["ok"] is False
    assert state["result"] == bootstrap_sync.SYNC_RESULT_SKIPPED_DIRTY



