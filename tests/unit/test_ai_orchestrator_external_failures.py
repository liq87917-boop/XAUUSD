from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import ai_orchestrator as orch


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Insufficient balance. Your Cline Credits balance is $0.00", "INSUFFICIENT_BALANCE"),
        ("quota exceeded for this account", "QUOTA_EXHAUSTED"),
        ("authentication failed", "AUTHENTICATION_FAILED"),
        ("invalid api key", "INVALID_API_KEY"),
        ("model not found", "MODEL_UNAVAILABLE"),
    ],
)
def test_non_retryable_external_failures(
    message: str,
    code: str,
) -> None:
    failure_class, failure_code = orch.classify_cline_failure(
        {
            "returncode": 1,
            "stdout": "",
            "stderr": message,
            "timed_out": False,
        }
    )

    assert failure_class == "non_retryable_external"
    assert failure_code == code


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("rate limit exceeded", "RATE_LIMITED"),
        ("503 service unavailable", "PROVIDER_TEMPORARY_UNAVAILABLE"),
        ("connection reset by peer", "NETWORK_ERROR"),
    ],
)
def test_retryable_external_failures(
    message: str,
    code: str,
) -> None:
    failure_class, failure_code = orch.classify_cline_failure(
        {
            "returncode": 1,
            "stdout": "",
            "stderr": message,
            "timed_out": False,
        }
    )

    assert failure_class == "retryable_external"
    assert failure_code == code


def test_timeout_is_retryable_external() -> None:
    assert orch.classify_cline_failure(
        {
            "returncode": 124,
            "stdout": "",
            "stderr": "Cline process timed out.",
            "timed_out": True,
        }
    ) == ("retryable_external", "CLINE_TIMEOUT")


def test_success_has_no_failure_class() -> None:
    assert orch.classify_cline_failure(
        {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "summary": {"finish_reason": "completed"},
        }
    ) == ("none", None)


def test_zero_exit_with_raw_aborted_terminal_is_a_retryable_failure() -> None:
    """GOLD-046：exit code 0 但权威 raw 终态 ``aborted`` ⇒ 明确失败（带证据）。"""

    assert orch.classify_cline_failure(
        {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "summary": {"finish_reason": "aborted"},
        }
    ) == (orch.TERMINAL_FAILURE_CLASS, orch.TERMINAL_FAILURE_CODE)


def test_zero_exit_without_raw_terminal_is_a_retryable_failure() -> None:
    """缺失权威 raw 终态 ⇒ fail-closed（绝不从 exit code 0 推断成功）。"""

    assert orch.classify_cline_failure(
        {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
    ) == (orch.TERMINAL_FAILURE_CLASS, orch.TERMINAL_FAILURE_CODE)


def test_provider_fatal_stderr_wins_even_with_zero_exit_code() -> None:
    assert orch.classify_cline_failure(
        {
            "returncode": 0,
            "stdout": "",
            "stderr": "insufficient_credits",
            "timed_out": False,
        }
    ) == ("non_retryable_external", "INSUFFICIENT_BALANCE")


def test_cline_args_pin_provider_without_exposing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(orch, "CLINE_MODEL", "")
    monkeypatch.setattr(orch, "CLINE_TIMEOUT_SECONDS", 3600)

    args = orch.build_cline_args(
        "cline.cmd",
        "do the task",
    )

    provider_index = args.index("--provider")
    assert args[provider_index + 1] == "deepseek"
    assert "--key" not in args
    assert "--model" not in args


def test_cline_args_allow_explicit_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "CLINE_PROVIDER", "deepseek")
    monkeypatch.setattr(orch, "CLINE_MODEL", "deepseek-flash")

    args = orch.build_cline_args(
        "cline.cmd",
        "do the task",
    )

    model_index = args.index("--model")
    assert args[model_index + 1] == "deepseek-flash"


def test_recovery_snapshot_keeps_patch_and_untracked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    recovery_dir = root / ".ai" / "runtime" / "recovery"
    recovery_dir.mkdir(parents=True)

    untracked = root / "new_file.py"
    untracked.write_text("print('recovery')\n", encoding="utf-8")

    monkeypatch.setattr(orch, "ROOT", root)
    monkeypatch.setattr(orch, "RECOVERY_DIR", recovery_dir)
    monkeypatch.setattr(
        orch,
        "recovery_snapshot_dir",
        lambda task_id, attempt: recovery_dir / f"{task_id}-{attempt}",
    )
    monkeypatch.setattr(
        orch,
        "get_changed_files",
        lambda: [" M tracked.py", "?? new_file.py"],
    )

    def fake_git(command: str, timeout: int = 120) -> dict[str, object]:
        del timeout
        if command == "diff --binary HEAD":
            return {
                "returncode": 0,
                "stdout": "diff --git a/tracked.py b/tracked.py\n",
                "stderr": "",
                "timed_out": False,
            }
        if command == "ls-files --others --exclude-standard":
            return {
                "returncode": 0,
                "stdout": "new_file.py\n",
                "stderr": "",
                "timed_out": False,
            }
        # GOLD-022：snapshot metadata 追加可恢复性证据所需的只读探测。
        if command == "status --porcelain":
            return {
                "returncode": 0,
                "stdout": " M tracked.py\n?? new_file.py\n",
                "stderr": "",
                "timed_out": False,
            }
        if command == "rev-parse HEAD":
            return {
                "returncode": 0,
                "stdout": "b" * 40 + "\n",
                "stderr": "",
                "timed_out": False,
            }
        if command == "branch --show-current":
            return {
                "returncode": 0,
                "stdout": "cline-agent\n",
                "stderr": "",
                "timed_out": False,
            }
        raise AssertionError(command)

    monkeypatch.setattr(orch, "git", fake_git)

    snapshot = orch.save_recovery_snapshot(
        "GOLD-018",
        1,
        "non_retryable_external",
        "INSUFFICIENT_BALANCE",
    )

    assert (snapshot / "changes.patch").read_text(encoding="utf-8").startswith(
        "diff --git"
    )
    assert (snapshot / "untracked" / "new_file.py").read_text(
        encoding="utf-8"
    ) == "print('recovery')\n"

    metadata = json.loads(
        (snapshot / "recovery.json").read_text(encoding="utf-8")
    )
    assert metadata["failure_code"] == "INSUFFICIENT_BALANCE"
    assert metadata["untracked_files"] == ["new_file.py"]

    # GOLD-022：快照必须自带可恢复性证据，且可被校验函数判定为可恢复。
    assert metadata["schema"] == orch.RECOVERY_SNAPSHOT_SCHEMA
    assert metadata["tracked_files"] == ["tracked.py"]
    assert metadata["patch_bytes"] == (snapshot / "changes.patch").stat().st_size
    assert metadata["patch_sha256"] == orch.sha256_file(snapshot / "changes.patch")
    assert metadata["untracked_sha256"] == {
        "new_file.py": orch.sha256_file(snapshot / "untracked" / "new_file.py")
    }
    assert orch.verify_recovery_snapshot(snapshot)[0] is True


def test_non_retryable_external_stops_before_validation_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_file = tmp_path / "GOLD-018.json"
    task_file.write_text(
        json.dumps(
            {
                "task_id": "GOLD-018",
                "title": "test",
                "max_attempts": 3,
                "validation_commands": ["SHOULD_NOT_RUN"],
            }
        ),
        encoding="utf-8",
    )

    result_dir = tmp_path / "results"
    result_dir.mkdir()

    recovery = tmp_path / ".ai" / "runtime" / "recovery" / "snapshot"
    recovery.mkdir(parents=True)

    monkeypatch.setattr(orch, "ROOT", tmp_path)
    monkeypatch.setattr(orch, "RESULT_DIR", result_dir)

    dirty_states = iter([False, True])
    monkeypatch.setattr(orch, "git_is_dirty", lambda: next(dirty_states))
    monkeypatch.setattr(
        orch,
        "run_cline",
        lambda _: {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                "Insufficient balance. "
                "Your Cline Credits balance is $0.00"
            ),
            "timed_out": False,
            "summary": {},
        },
    )

    def validation_must_not_run(task: dict[str, object]) -> list[object]:
        del task
        raise AssertionError("validation must not run")

    monkeypatch.setattr(orch, "run_validations", validation_must_not_run)
    monkeypatch.setattr(orch, "get_changed_files", lambda: [" M file.py"])
    monkeypatch.setattr(orch, "get_diff_stat", lambda: "file.py | 1 +")
    monkeypatch.setattr(
        orch,
        "save_recovery_snapshot",
        lambda *args, **kwargs: recovery,
    )

    reset_calls: list[bool] = []
    monkeypatch.setattr(
        orch,
        "reset_task_changes",
        lambda: reset_calls.append(True),
    )

    states: list[tuple[str, dict[str, object]]] = []

    def capture_state(
        task_id: str,
        status: str,
        **extra: object,
    ) -> None:
        assert task_id == "GOLD-018"
        states.append((status, extra))

    monkeypatch.setattr(orch, "write_task_state", capture_state)

    outcome = orch.process_task(task_file)

    assert outcome == "waiting_external"
    assert reset_calls == [True]
    assert [status for status, _ in states] == [
        "running",
        "waiting_external",
    ]
    waiting = states[-1][1]
    assert waiting["failure_code"] == "INSUFFICIENT_BALANCE"
    assert waiting["attempt"] == 1
    assert not (result_dir / "GOLD-018.json").exists()
