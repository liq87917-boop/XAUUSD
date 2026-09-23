"""``orchestrator.result_terminal_consistency`` 终态一致性门禁回归（GOLD-039）。

覆盖（与 GOLD-039 requirements 一一对应）：

1. 正常成功 / blocked / provider fatal / timeout / retry exhausted 都必须是**稳定且自洽**
   的终态语义（不误报）；
2. ``completed`` + 最终 attempt ``aborted`` / ``failed``、``blocked`` + 最终 attempt
   ``completed`` 等矛盾一律 fail-closed，并给出**稳定 reason code**；
3. 未知状态 / 未知 outcome / 结构非法 / 非归一化 raw 值落进归一化键 ⇒ fail-closed；
4. 历史 result 只读兼容：只报告矛盾，绝不修改入参、绝不重算身份；
5. Orchestrator 写入前门禁：``write_final_result`` 拒绝落盘自相矛盾 result；
6. 边界：纯只读（无写入 / 无外部进程 / 无网络痕迹）、不产生任何 review verdict。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import result_terminal_consistency as terminal

MODULE_SOURCE = Path(terminal.__file__).read_text(encoding="utf-8")

# 只读模块源码里绝不出现的写入 / 子进程 / 非确定性痕迹。
FORBIDDEN_SOURCE_SUBSTRINGS = (
    "write_text",
    "write_bytes",
    "mkdir",
    "rmtree",
    "unlink",
    "rmdir",
    "shutil",
    "os.replace",
    "os.remove",
    "chmod",
    "tempfile",
    "subprocess",
    "open(",
    "json.dump(",
    "time.time",
    "datetime.now",
    "random",
)

# Executor 侧绝不存在的 review / planner / 权限 API。
FORBIDDEN_PLANNING_API = (
    "write_review_ledger",
    "append_review_entry",
    "sign_review",
    "update_project_state",
    "generate_follow_on_task",
    "refill_rolling_queue",
    "decide_phase",
)


@pytest.fixture()
def result_fs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """把 tasks / results 重定向到 tmp_path（对真实仓库零影响）。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"

    tasks.mkdir()
    results.mkdir()

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)

    return tasks, results


def passing_validation() -> dict[str, Any]:
    return {"command": "pytest", "returncode": 0, "timed_out": False}


def failing_validation() -> dict[str, Any]:
    return {"command": "pytest", "returncode": 1, "timed_out": False}


def completed_result(**extra: Any) -> dict[str, Any]:
    """归一化格式的正常成功 result（与 Orchestrator 真实写入形状一致）。"""

    attempt: dict[str, Any] = {
        "attempt": 1,
        "cline_exit_code": 0,
        "cline_timed_out": False,
        "failure_class": "none",
        "failure_code": None,
        "recovery_path": None,
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "finish_reason": "completed",
        "cline_finish_reason_raw": "aborted",
        "validations": [passing_validation()],
    }

    payload: dict[str, Any] = {
        "task_id": "GOLD-101",
        "status": "completed",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "attempts": [attempt],
    }

    payload.update(extra)

    return payload


def codes(payload: object) -> list[str]:
    return terminal.analyze_result_terminal_consistency(payload)["reason_codes"]


# ============================================================
# 1. 正常 / 稳定的终态语义（绝不误报）
# ============================================================


def test_normalized_completed_result_is_consistent() -> None:
    """成功任务：``finish_reason=completed`` + raw ``aborted`` 必须完全自洽。"""

    report = terminal.analyze_result_terminal_consistency(completed_result())

    assert report["schema"] == terminal.SCHEMA
    assert report["schema_version"] == terminal.SCHEMA_VERSION
    assert report["consistent"] is True
    assert report["reason_codes"] == []
    assert report["format"] == terminal.FORMAT_NORMALIZED
    assert report["status_terminal"] is True
    assert report["final_attempt"] == 1
    assert report["final_cline_finish_reason_raw"] == "aborted"


def test_blocked_after_retry_exhaustion_is_consistent() -> None:
    """retry exhausted：顶层 ``blocked`` + 最终 attempt ``failed`` + 失败证据 ⇒ 自洽。"""

    payload = completed_result(
        status="blocked",
        execution_outcome="blocked",
        normalized_finish_reason="blocked",
        attempts=[
            {
                "attempt": 1,
                "cline_exit_code": 1,
                "cline_timed_out": False,
                "failure_class": "execution_failure",
                "failure_code": "CLINE_EXECUTION_FAILED",
                "execution_outcome": "failed",
                "normalized_finish_reason": "failed",
                "finish_reason": "failed",
                "cline_finish_reason_raw": "error",
                "validations": [],
            }
        ],
    )

    report = terminal.analyze_result_terminal_consistency(payload)

    assert report["consistent"] is True
    assert report["reason_codes"] == []
    assert report["failure_evidence"] == ["cline_exit_code", "failure_class", "failure_code"]


def test_provider_fatal_blocked_result_is_consistent() -> None:
    """provider fatal：``waiting_external`` + non-retryable 证据 ⇒ 自洽。"""

    payload = completed_result(
        status="blocked",
        execution_outcome="waiting_external",
        normalized_finish_reason="waiting_external",
        attempts=[
            {
                "attempt": 1,
                "cline_exit_code": 0,
                "failure_class": "non_retryable_external",
                "failure_code": "INSUFFICIENT_BALANCE",
                "execution_outcome": "waiting_external",
                "normalized_finish_reason": "waiting_external",
                "finish_reason": "waiting_external",
                "validations": [],
            }
        ],
    )

    assert terminal.analyze_result_terminal_consistency(payload)["consistent"] is True
    assert codes(payload) == []


def test_timeout_is_consistent_but_never_a_success() -> None:
    """timeout：blocked + failed + timeout 证据 ⇒ 自洽；completed + timeout ⇒ 矛盾。"""

    timed_out_attempt = {
        "attempt": 1,
        "cline_exit_code": 124,
        "cline_timed_out": True,
        "failure_class": "retryable_external",
        "failure_code": "CLINE_TIMEOUT",
        "execution_outcome": "failed",
        "normalized_finish_reason": "failed",
        "finish_reason": "failed",
        "validations": [],
    }

    blocked = completed_result(
        status="blocked",
        execution_outcome="blocked",
        normalized_finish_reason="blocked",
        attempts=[timed_out_attempt],
    )

    assert terminal.analyze_result_terminal_consistency(blocked)["consistent"] is True

    completed = completed_result(attempts=[timed_out_attempt])

    completed_codes = codes(completed)

    assert terminal.REASON_FINAL_TIMEOUT_CONTRADICTS_STATUS in completed_codes
    assert terminal.REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS in completed_codes
    assert terminal.REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS in completed_codes


def test_result_without_attempt_evidence_is_only_checked_at_top_level() -> None:
    """没有 attempt 事实（合成 fixture / 历史 V1）⇒ 只校验顶层，绝不凭空指控。"""

    payload = {
        "task_id": "GOLD-101",
        "status": "completed",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
    }

    report = terminal.analyze_result_terminal_consistency(payload)

    assert report["consistent"] is True
    assert report["final_attempt"] is None
    assert report["reason_codes"] == []


# ============================================================
# 2. 自相矛盾必须 fail-closed（GOLD-035 形状）
# ============================================================


def test_completed_with_raw_aborted_in_normalized_key_is_fail_closed() -> None:
    """``completed`` 但归一化键里是 raw ``aborted`` ⇒ 稳定 code，绝不猜 PASS。"""

    payload = completed_result()

    payload["attempts"][0]["finish_reason"] = "aborted"

    payload["attempts"][0]["normalized_finish_reason"] = "aborted"

    result_codes = codes(payload)

    assert terminal.REASON_FINISH_REASON_INVALID in result_codes
    assert terminal.REASON_FINISH_REASON_MISMATCH in result_codes
    assert terminal.REASON_RAW_FINISH_REASON_MISPLACED in result_codes
    assert terminal.is_inconsistent(payload) is True

    with pytest.raises(terminal.TerminalConsistencyError) as excinfo:
        terminal.assert_result_terminal_consistency(payload)

    assert terminal.REASON_FINISH_REASON_INVALID in str(excinfo.value)


def test_completed_with_failed_final_attempt_is_fail_closed() -> None:
    payload = completed_result()

    payload["attempts"][0]["execution_outcome"] = "failed"

    payload["attempts"][0]["normalized_finish_reason"] = "failed"

    payload["attempts"][0]["finish_reason"] = "failed"

    payload["attempts"][0]["cline_exit_code"] = 1

    result_codes = codes(payload)

    assert terminal.REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS in result_codes
    assert terminal.REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS in result_codes


def test_blocked_with_completed_final_attempt_is_fail_closed() -> None:
    payload = completed_result(
        status="blocked",
        execution_outcome="blocked",
        normalized_finish_reason="blocked",
    )

    assert terminal.REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS in codes(payload)


def test_completed_with_failed_validation_is_fail_closed() -> None:
    payload = completed_result()

    payload["attempts"][0]["validations"] = [passing_validation(), failing_validation()]

    assert terminal.REASON_FINAL_VALIDATION_CONTRADICTS_STATUS in codes(payload)


def test_completed_with_failure_class_or_timed_out_is_fail_closed() -> None:
    payload = completed_result()

    payload["attempts"][0]["failure_class"] = "execution_failure"

    payload["attempts"][0]["cline_timed_out"] = True

    result_codes = codes(payload)

    assert terminal.REASON_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS in result_codes
    assert terminal.REASON_FINAL_TIMEOUT_CONTRADICTS_STATUS in result_codes


def test_status_outcome_and_finish_reason_mismatches_are_fail_closed() -> None:
    payload = completed_result(
        status="completed",
        execution_outcome="failed",
        normalized_finish_reason="blocked",
        attempts=[],
    )

    result_codes = codes(payload)

    assert terminal.REASON_STATUS_OUTCOME_MISMATCH in result_codes
    assert terminal.REASON_OUTCOME_FINISH_REASON_MISMATCH in result_codes


def test_unknown_combinations_are_fail_closed() -> None:
    assert terminal.REASON_STATUS_UNKNOWN in codes({"status": "someday", "attempts": []})

    assert terminal.REASON_STATUS_MISSING in codes({"attempts": []})

    assert terminal.REASON_RESULT_INVALID in codes(["not", "a", "dict"])

    assert terminal.REASON_ATTEMPTS_INVALID in codes(
        {"status": "completed", "attempts": {"attempt": 1}}
    )

    assert terminal.REASON_ATTEMPT_INVALID in codes(
        {"status": "completed", "attempts": [{"attempt": 1}, "broken"]}
    )

    assert terminal.REASON_OUTCOME_INVALID in codes(
        {"status": "completed", "execution_outcome": "mystery", "attempts": []}
    )

    assert terminal.REASON_FAILURE_EVIDENCE_MISSING in codes(
        {
            "status": "blocked",
            "execution_outcome": "blocked",
            "normalized_finish_reason": "blocked",
            "attempts": [
                {
                    "attempt": 1,
                    "cline_exit_code": 0,
                    "execution_outcome": "failed",
                    "normalized_finish_reason": "failed",
                    "finish_reason": "failed",
                }
            ],
        }
    )


def test_unknown_top_level_outcome_is_fail_closed() -> None:
    payload = completed_result(
        execution_outcome="finished",
        normalized_finish_reason="finished",
        attempts=[],
    )

    assert terminal.REASON_OUTCOME_INVALID in codes(payload)


# ============================================================
# 3. 历史 result 只读兼容路径
# ============================================================


def test_legacy_historical_contradiction_is_reported_read_only() -> None:
    """GOLD-035 真实形状：legacy 格式 + ``completed`` + raw ``aborted``。"""

    legacy = {
        "task_id": "GOLD-035",
        "status": "completed",
        "finished_at": "2026-09-23T20:07:56+08:00",
        "attempts": [
            {
                "attempt": 1,
                "cline_exit_code": 0,
                "cline_timed_out": False,
                "failure_class": "none",
                "failure_code": None,
                "finish_reason": "aborted",
                "validations": [passing_validation()],
            }
        ],
    }

    snapshot = copy.deepcopy(legacy)

    first = terminal.analyze_result_terminal_consistency(legacy)

    second = terminal.analyze_result_terminal_consistency(legacy)

    assert legacy == snapshot  # 只读：绝不回写 / 归一化历史 result
    assert first == second  # 确定：相同输入 ⇒ 相同报告
    assert first["format"] == terminal.FORMAT_LEGACY
    assert first["consistent"] is False
    assert first["reason_codes"] == [
        terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS
    ]
    assert "aborted" in first["details"][0]["detail"]

    with pytest.raises(terminal.TerminalConsistencyError):
        terminal.assert_result_terminal_consistency(legacy)

    assert legacy == snapshot


def test_legacy_completed_with_raw_completed_is_consistent() -> None:
    """历史格式里唯一 ``finish_reason`` 恰好是 ``completed`` ⇒ 不指控矛盾。"""

    legacy = {
        "task_id": "GOLD-027",
        "status": "completed",
        "attempts": [{"attempt": 1, "cline_exit_code": 0, "finish_reason": "completed"}],
    }

    assert terminal.analyze_result_terminal_consistency(legacy)["consistent"] is True


def test_legacy_blocked_or_missing_raw_reason_is_not_guessed() -> None:
    blocked = {
        "task_id": "GOLD-001",
        "status": "blocked",
        "attempts": [{"attempt": 1, "cline_exit_code": 1, "finish_reason": "error"}],
    }

    assert terminal.analyze_result_terminal_consistency(blocked)["consistent"] is True

    missing = {
        "task_id": "GOLD-001",
        "status": "blocked",
        "attempts": [{"attempt": 1, "cline_exit_code": 1}],
    }

    assert terminal.analyze_result_terminal_consistency(missing)["consistent"] is True

    blocked_with_completed_raw = {
        "task_id": "GOLD-001",
        "status": "blocked",
        "attempts": [{"attempt": 1, "cline_exit_code": 0, "finish_reason": "completed"}],
    }

    assert terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS in codes(
        blocked_with_completed_raw
    )


def test_legacy_result_status_helper_is_read_only() -> None:
    legacy = {"status": "Completed", "attempts": []}

    snapshot = copy.deepcopy(legacy)

    assert terminal.result_status(legacy) == "completed"
    assert terminal.result_status({"attempts": []}) is None
    assert terminal.result_status("not-a-dict") is None
    assert legacy == snapshot


def test_v1_migrated_empty_attempt_is_not_guessed() -> None:
    """V1 迁移的空壳 attempt（只有迁移标记）⇒ 没有事实就不做组合判定。"""

    payload = {
        "task_id": "GOLD-001",
        "status": "blocked",
        "execution_outcome": "blocked",
        "normalized_finish_reason": "blocked",
        "attempts": [{"attempt": 1, "migrated_from_v1": True, "cline_exit_code": None}],
    }

    assert terminal.analyze_result_terminal_consistency(payload)["consistent"] is True


# ============================================================
# 4. Orchestrator 写入前门禁
# ============================================================


def test_repo_scoped_import_exposes_single_rule_source() -> None:
    module = orch.repo_scoped_import("orchestrator.result_terminal_consistency")

    assert module is terminal
    assert module.SCHEMA == terminal.SCHEMA


def test_write_final_result_rejects_contradictory_attempt(
    result_fs: tuple[Path, Path],
) -> None:
    """未来不再能写出 ``completed`` + 最终 attempt ``aborted`` 的矛盾 result。"""

    _, results = result_fs

    contradictory_attempt = {
        "attempt": 1,
        "cline_exit_code": 0,
        "finish_reason": "aborted",
        "validations": [passing_validation()],
    }

    with pytest.raises(orch.ResultTerminalConsistencyError) as excinfo:
        orch.write_final_result(
            {"task_id": "GOLD-101", "title": "t"},
            "completed",
            [contradictory_attempt],
        )

    assert terminal.REASON_FINISH_REASON_INVALID in str(excinfo.value)
    assert not (results / "GOLD-101.json").exists()  # 矛盾 result 绝不落盘


def test_write_final_result_rejects_status_outcome_mismatch(
    result_fs: tuple[Path, Path],
) -> None:
    _, results = result_fs

    with pytest.raises(orch.ResultTerminalConsistencyError):
        orch.write_final_result(
            {"task_id": "GOLD-103", "title": "t"},
            "blocked",
            [],
            execution_outcome=orch.EXECUTION_OUTCOME_COMPLETED,
        )

    assert not (results / "GOLD-103.json").exists()


def test_write_final_result_accepts_consistent_results(
    result_fs: tuple[Path, Path],
) -> None:
    _, results = result_fs

    consistent_attempt = {
        "attempt": 1,
        "cline_exit_code": 0,
        "cline_timed_out": False,
        "failure_class": "none",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "finish_reason": "completed",
        "cline_finish_reason_raw": "aborted",
        "validations": [passing_validation()],
    }

    written = orch.write_final_result(
        {"task_id": "GOLD-101", "title": "t"},
        "completed",
        [consistent_attempt],
        execution_outcome=orch.EXECUTION_OUTCOME_COMPLETED,
    )

    on_disk = json.loads((results / "GOLD-101.json").read_text(encoding="utf-8"))

    assert on_disk == written
    assert on_disk["status"] == "completed"
    assert on_disk["attempts"][0]["finish_reason"] == "completed"
    assert on_disk["attempts"][0]["cline_finish_reason_raw"] == "aborted"


def test_write_final_result_without_attempt_evidence_still_writes(
    result_fs: tuple[Path, Path],
) -> None:
    """没有 attempt 事实（老路径 / 合成）⇒ 顶层自洽即允许写入，绝不误伤。"""

    _, results = result_fs

    orch.write_final_result({"task_id": "GOLD-102", "title": "t"}, "blocked", [])

    payload = json.loads((results / "GOLD-102.json").read_text(encoding="utf-8"))

    assert payload["status"] == "blocked"
    assert payload["execution_outcome"] == "blocked"


def test_write_gate_fails_closed_when_validator_is_unavailable(
    result_fs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验器不可用 ⇒ fail-closed（无法证明自洽就不写终态事实）。"""

    _, results = result_fs

    monkeypatch.setattr(orch, "repo_scoped_import", lambda name: None)

    with pytest.raises(orch.ResultTerminalConsistencyError):
        orch.write_final_result({"task_id": "GOLD-104", "title": "t"}, "completed", [])

    assert not (results / "GOLD-104.json").exists()


# ============================================================
# 5. 只读 / 权限边界（源码守卫 + 机器可读契约）
# ============================================================


def test_module_source_has_no_write_or_process_traces() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_exposes_no_planning_or_review_api() -> None:
    for forbidden in FORBIDDEN_PLANNING_API:
        assert f"def {forbidden}" not in MODULE_SOURCE, forbidden


def test_authority_contract_denies_every_write_power() -> None:
    authority = terminal.AUTHORITY

    assert authority["read_only"] is True
    assert authority["pure_function"] is True
    assert authority["emits_review_outcome"] is False
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_repair_result"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["writes_results"] is False
    assert authority["writes_tasks"] is False
    assert authority["writes_project_state"] is False
    assert authority["writes_review_ledger"] is False


def test_reason_codes_are_stable_and_unique() -> None:
    assert len(terminal.REASON_CODES) == len(set(terminal.REASON_CODES))

    for code in terminal.REASON_CODES:
        assert code.startswith("RESULT_TERMINAL_")
        assert code.isascii()
        assert code.isupper()
