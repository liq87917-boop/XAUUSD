"""启动前 bootstrap sync（GOLD-021）。

旧行为（真实出现的问题）：``start_agent.bat`` 直接启动
``orchestrator/ai_orchestrator.py``，Git 同步发生在 Python 进程**内部**
（``sync_repository()`` 的 ``pull --rebase``），因此存在
「磁盘上的代码已经更新，但已加载的 Orchestrator 仍执行旧代码」的窗口。

新契约（本模块）：launcher 必须按固定顺序启动

    1) ``python -m orchestrator.bootstrap_sync``（本模块，独立进程）
    2) 仅当 (1) 以退出码 0 成功，才启动 ``orchestrator/ai_orchestrator.py``

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）：

- 只做**只读检查**与 **fast-forward / rebase** 兼容同步；
  绝不 ``push --force``、绝不 ``reset --hard``、绝不静默清理 dirty worktree；
- dirty worktree / 当前分支不符 / 网络失败 / 远端 ref 缺失 / rebase 冲突
  一律 **fail-closed**（非 0 退出码，由 launcher 停止启动），
  本地修改与本地 commit 一律保留；
- 只打印 branch / HEAD / provider / model / 同步结果，绝不打印任何凭据。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_BRANCH = os.getenv("AI_BRANCH", "cline-agent")

REMOTE_NAME = os.getenv("AI_REMOTE", "origin")

CLINE_PROVIDER = os.getenv("AI_CLINE_PROVIDER", "deepseek").strip()

CLINE_MODEL = os.getenv("AI_CLINE_MODEL", "").strip()

GIT_TIMEOUT_SECONDS = 180

BOOTSTRAP_STATE_SCHEMA = "gold-ai/bootstrap-sync/v1"

# 同步结果词表（稳定字符串，写进 runtime 状态与日志）。
SYNC_RESULT_UP_TO_DATE = "up_to_date"
SYNC_RESULT_FAST_FORWARD = "fast_forward"
SYNC_RESULT_REBASED = "rebased"
SYNC_RESULT_AHEAD_ONLY = "ahead_only"
SYNC_RESULT_SKIPPED_DIRTY = "skipped_dirty"
SYNC_RESULT_WRONG_BRANCH = "wrong_branch"
SYNC_RESULT_FETCH_FAILED = "fetch_failed"
SYNC_RESULT_REMOTE_REF_MISSING = "remote_ref_missing"
SYNC_RESULT_REBASE_CONFLICT = "rebase_conflict"
SYNC_RESULT_FAILED = "failed"

EXIT_OK = 0
EXIT_WRONG_BRANCH = 2
EXIT_DIRTY_WORKTREE = 3
EXIT_SYNC_FAILED = 4
EXIT_GIT_UNAVAILABLE = 5

# fail-closed 结果 → launcher 退出码（0 只属于真正同步成功）。
FAIL_CLOSED_EXIT_CODES = {
    SYNC_RESULT_SKIPPED_DIRTY: EXIT_DIRTY_WORKTREE,
    SYNC_RESULT_WRONG_BRANCH: EXIT_WRONG_BRANCH,
}


# ============================================================
# Git 结果与可注入 runner
# ============================================================


@dataclass(frozen=True)
class CommandResult:
    """一次 git 调用的结果（与 orchestrator.run_command 同构）。"""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class GitRunner(Protocol):
    """``git`` 命令执行器协议（测试可注入 fake，永不访问真实远端）。"""

    def __call__(self, command: str, cwd: Path) -> CommandResult: ...


def run_git(command: str, cwd: Path) -> CommandResult:
    """真实执行 ``git <command>``（cwd 必须显式传入，绝不隐式指向项目根）。"""

    try:
        completed = subprocess.run(
            f"git {command}",
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )

    except subprocess.TimeoutExpired:
        return CommandResult(
            returncode=124,
            stderr=f"git {command} 超时（{GIT_TIMEOUT_SECONDS}s）",
            timed_out=True,
        )

    except OSError as exc:
        return CommandResult(
            returncode=127,
            stderr=f"无法执行 git: {exc}",
        )

    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


# ============================================================
# 同步结果
# ============================================================


@dataclass
class SyncOutcome:
    """一次启动前同步的完整结果（可审计、可落盘）。"""

    result: str = SYNC_RESULT_FAILED
    ok: bool = False
    reason: str = ""
    branch: str | None = None
    head_before: str | None = None
    head_after: str | None = None
    remote: str = REMOTE_NAME
    remote_sha: str | None = None
    checked_at: str = ""
    commands: list[str] = field(default_factory=list)

    @property
    def code_updated(self) -> bool:
        """磁盘代码是否真的被换成了新版本（旧进程内存不会跟着变）。"""
        return (
            self.ok
            and bool(self.head_before)
            and bool(self.head_after)
            and self.head_before != self.head_after
        )

    def as_state(self) -> dict[str, object]:
        return {
            "schema": BOOTSTRAP_STATE_SCHEMA,
            "checked_at": self.checked_at or now_iso(),
            "result": self.result,
            "ok": self.ok,
            "reason": self.reason,
            "branch": self.branch,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "remote": self.remote,
            "remote_sha": self.remote_sha,
            "code_updated": self.code_updated,
            "provider": CLINE_PROVIDER,
            "model": CLINE_MODEL,
        }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def abbrev_sha(value: object) -> str:
    """把 SHA（完整或已缩写）统一成 8 位短 SHA；无效值返回 ``<unknown>``。"""

    if not isinstance(value, str):
        return "<unknown>"

    text = value.strip()

    if not text:
        return "<unknown>"

    return text[:8]


def first_line(result: CommandResult) -> str:
    """错误摘要只取首行，避免把整段 git 输出灌进日志。"""

    text = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"

    return text.splitlines()[0].strip()


def exit_code_for(outcome: SyncOutcome) -> int:
    if outcome.ok:
        return EXIT_OK

    return FAIL_CLOSED_EXIT_CODES.get(outcome.result, EXIT_SYNC_FAILED)


def default_state_path(root: Path) -> Path:
    return root / ".ai" / "runtime" / "bootstrap_state.json"


# ============================================================
# 启动前同步
# ============================================================


def _finish_ok(outcome: SyncOutcome, result: str) -> SyncOutcome:
    outcome.result = result
    outcome.ok = True
    outcome.reason = ""

    return outcome


def _finish_failed(outcome: SyncOutcome, result: str, reason: str) -> SyncOutcome:
    outcome.result = result
    outcome.ok = False
    outcome.reason = reason
    # fail-closed 绝不改动工作区：HEAD 视为「与同步前一致」。
    outcome.head_after = outcome.head_before

    return outcome


def synchronize(
    root: Path | None = None,
    branch: str | None = None,
    remote: str | None = None,
    runner: GitRunner | None = None,
) -> SyncOutcome:
    """启动前安全同步：只允许 fast-forward / rebase，任何风险一律 fail-closed。"""

    repo = Path(root) if root is not None else ROOT

    target_branch = branch or REQUIRED_BRANCH

    remote_name = remote or REMOTE_NAME

    git_runner: GitRunner = runner or run_git

    outcome = SyncOutcome(result=SYNC_RESULT_FAILED, remote=remote_name, checked_at=now_iso())

    def call(command: str) -> CommandResult:
        outcome.commands.append(command)

        return git_runner(command, repo)

    # --------------------------------------------------------
    # 1) 只读探测：分支 + 本地 HEAD（绝不 checkout / merge / rebase）
    # --------------------------------------------------------

    current = call("branch --show-current")

    if not current.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot read current branch: {first_line(current)}",
        )

    outcome.branch = current.stdout.strip()

    if outcome.branch != target_branch:
        return _finish_failed(
            outcome,
            SYNC_RESULT_WRONG_BRANCH,
            f"current branch is {outcome.branch or '<detached>'} but {target_branch} required: "
            "fail-closed, never auto checkout/switch",
        )

    head = call("rev-parse HEAD")

    if not head.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot read local HEAD: {first_line(head)}",
        )

    outcome.head_before = head.stdout.strip()

    # --------------------------------------------------------
    # 2) dirty worktree：在**任何** fetch/merge/rebase 之前 fail-closed
    # --------------------------------------------------------

    status = call("status --porcelain")

    if not status.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot read worktree status (not a git repo?): {first_line(status)}",
        )

    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]

    if dirty_lines:
        return _finish_failed(
            outcome,
            SYNC_RESULT_SKIPPED_DIRTY,
            "worktree has uncommitted changes"
            f" ({len(dirty_lines)} items): fail-closed, "
            "no fetch/merge/rebase, local changes and commits preserved",
        )

    # --------------------------------------------------------
    # 3) 只 fetch，不动工作区
    # --------------------------------------------------------

    fetch = call(f"fetch --prune {remote_name} {target_branch}")

    if not fetch.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FETCH_FAILED,
            f"git fetch failed (network/permission?): {first_line(fetch)}",
        )

    remote_ref = call(f"rev-parse {remote_name}/{target_branch}")

    if not remote_ref.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_REMOTE_REF_MISSING,
            f"cannot resolve {remote_name}/{target_branch}: {first_line(remote_ref)}",
        )

    outcome.remote_sha = remote_ref.stdout.strip()

    if outcome.remote_sha == outcome.head_before:
        outcome.head_after = outcome.head_before

        return _finish_ok(outcome, SYNC_RESULT_UP_TO_DATE)

    return _sync_divergence(
        outcome,
        call,
        remote_name,
        target_branch,
    )


def _sync_divergence(
    outcome: SyncOutcome,
    call: Callable[[str], CommandResult],
    remote_name: str,
    target_branch: str,
) -> SyncOutcome:
    """本地与远端不一致时的安全分支：只允许 fast-forward / rebase。"""

    remote_ref = f"{remote_name}/{target_branch}"

    # --------------------------------------------------------
    # 4) 本地落后 → fast-forward（绝不产生 merge commit）
    # --------------------------------------------------------

    behind = call(f"merge-base --is-ancestor HEAD {remote_ref}")

    if behind.returncode == 0:
        merge = call(f"merge --ff-only {remote_ref}")

        if not merge.ok:
            return _finish_failed(
                outcome,
                SYNC_RESULT_FAILED,
                f"fast-forward merge failed: {first_line(merge)}",
            )

        return _verify_after_sync(outcome, call, SYNC_RESULT_FAST_FORWARD)

    if behind.returncode != 1:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot determine local vs remote: {first_line(behind)}",
        )

    # --------------------------------------------------------
    # 5) 本地领先（例如上一轮 push 未完成）→ 不动工作区，
    #    交给 Orchestrator 后续 push pending 流程恢复。
    # --------------------------------------------------------

    ahead = call(f"merge-base --is-ancestor {remote_ref} HEAD")

    if ahead.returncode == 0:
        outcome.head_after = outcome.head_before

        return _finish_ok(outcome, SYNC_RESULT_AHEAD_ONLY)

    if ahead.returncode != 1:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot determine local vs remote: {first_line(ahead)}",
        )

    # --------------------------------------------------------
    # 6) 真正分叉 → 只允许 rebase；失败必须 abort 回原状
    # --------------------------------------------------------

    rebase = call(f"rebase {remote_ref}")

    if not rebase.ok:
        abort = call("rebase --abort")

        reason = (
            "rebase conflict: fail-closed and rebase --abort to restore, "
            f"local commits preserved ({first_line(rebase)})"
        )

        if not abort.ok:
            reason += f"; rebase --abort also failed ({first_line(abort)}), check manually"

        return _finish_failed(outcome, SYNC_RESULT_REBASE_CONFLICT, reason)

    return _verify_after_sync(outcome, call, SYNC_RESULT_REBASED)


def _verify_after_sync(
    outcome: SyncOutcome,
    call: Callable[[str], CommandResult],
    success_result: str,
) -> SyncOutcome:
    """同步后必须核对 HEAD == 远端 SHA，否则视为失败（绝不静默放行）。"""

    after = call("rev-parse HEAD")

    if not after.ok:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            f"cannot read HEAD after sync: {first_line(after)}",
        )

    head_after = after.stdout.strip()

    if head_after != outcome.remote_sha:
        return _finish_failed(
            outcome,
            SYNC_RESULT_FAILED,
            "HEAD after sync differs from remote: fail-closed, check manually",
        )

    outcome.head_after = head_after

    return _finish_ok(outcome, success_result)


# ============================================================
# 落盘 / 日志 / 入口
# ============================================================


def write_state(path: Path, outcome: SyncOutcome) -> None:
    """原子写 runtime bootstrap 状态（供 Orchestrator 启动日志核对）。

    只含 branch / HEAD / provider / model / 同步结果，**绝不含任何凭据**。
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    payload = json.dumps(outcome.as_state(), ensure_ascii=False, indent=2)

    temp_path.write_text(payload, encoding="utf-8")

    os.replace(temp_path, path)


def render_report(outcome: SyncOutcome, root: Path) -> str:
    """启动日志：branch / HEAD 短 SHA / provider / model / sync 结果。"""

    lines = [
        "========================================",
        "  Bootstrap Sync (pre-launch)  GOLD-021",
        "========================================",
        f"Repository : {root}",
        f"Branch     : {outcome.branch or '<unknown>'}",
        f"HEAD before: {abbrev_sha(outcome.head_before)}",
        f"Provider   : {CLINE_PROVIDER or '<unknown>'}",
        f"Model      : {CLINE_MODEL or '<provider default>'}",
        f"Remote     : {outcome.remote}/{outcome.branch or REQUIRED_BRANCH}"
        f" -> {abbrev_sha(outcome.remote_sha)}",
        f"Sync result: {outcome.result}",
        f"HEAD after : {abbrev_sha(outcome.head_after)}",
        "----------------------------------------",
    ]

    if outcome.ok:
        if outcome.code_updated:
            lines.append(
                f"bootstrap sync OK: on-disk code updated to {abbrev_sha(outcome.head_after)}, "
                "subsequent Python/Orchestrator processes will load this version."
            )
        else:
            lines.append(
                f"bootstrap sync OK ({outcome.result}): "
                f"on-disk code stays {abbrev_sha(outcome.head_after)}, "
                "subsequent Python/Orchestrator processes will load this version."
            )
    else:
        lines.extend(
            [
                "*** bootstrap sync FAILED (fail-closed) ***",
                f"Reason     : {outcome.reason}",
                "Orchestrator launch stopped: local changes and commits preserved, ",
                "resolve manually (commit / restore network / switch branch) then re-run start_agent.bat.",
            ]
        )

    lines.append("========================================")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.bootstrap_sync",
        description="启动前安全同步 cline-agent（GOLD-021）：必须先于 Orchestrator 进程运行。",
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
    )

    parser.add_argument(
        "--branch",
        default=None,
        help="要求的同步分支（默认 AI_BRANCH 或 cline-agent）",
    )

    parser.add_argument(
        "--remote",
        default=None,
        help="远端名（默认 AI_REMOTE 或 origin）",
    )

    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="bootstrap 状态落盘路径（默认 <root>/.ai/runtime/bootstrap_state.json）",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = args.root.resolve() if args.root is not None else ROOT

    if shutil.which("git") is None:
        outcome = SyncOutcome(
            result=SYNC_RESULT_FAILED,
            ok=False,
            reason="git executable not found: cannot sync before launch",
            branch=args.branch,
            remote=args.remote or REMOTE_NAME,
            checked_at=now_iso(),
        )

        print(render_report(outcome, root))

        sys.stdout.flush()

        return EXIT_GIT_UNAVAILABLE

    outcome = synchronize(
        root=root,
        branch=args.branch,
        remote=args.remote,
    )

    state_path = args.state_path if args.state_path is not None else default_state_path(root)

    try:
        write_state(state_path, outcome)

    except OSError as exc:
        # 状态落盘失败不影响同步结论，但必须显式告警（Orchestrator 会记录 not recorded）。
        print(f"[WARN] cannot write bootstrap state {state_path}: {exc}")

    print(render_report(outcome, root))

    sys.stdout.flush()

    return exit_code_for(outcome)


if __name__ == "__main__":
    sys.exit(main())

