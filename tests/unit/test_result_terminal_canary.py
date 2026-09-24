"""结果终态 canary 的单元契约（GOLD-047）。

本文件只锁**稳定契约与纯判定**：schema / 退出码 / reason code 词表、假 git 白名单与
记录语义、事实投影、以及「正确事实零 code、退化 / 回归事实必 fail-closed」。
**新进程端到端**证明在 ``tests/integration/test_result_terminal_canary_regression.py``。

红线：本文件只读源码与自建事实，绝不写 ``.ai/**``，绝不 patch 终态链，绝不起真实进程。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator import result_terminal_canary as canary

CANARY_SOURCE_PATH = Path(canary.__file__)

# 终态链上的入口：canary **绝不**可以替换它们（只能替换进程外边界）。
FORBIDDEN_CHAIN_PATCHES = (
    "attempt_outcome",
    "normalize_finish_reason",
    "build_attempt_record",
    "build_final_result",
    "ensure_result_terminal_consistency",
    "ensure_completion_terminal_consistency",
    "write_final_result",
    "commit_task_result",
    "cline_terminal_is_success",
    "authoritative_cline_terminal",
)

# canary 自己绝不允许 spawn 真实进程（假 git 也必须 fail-closed）。
FORBIDDEN_SUBPROCESS_SUBSTRINGS = (
    "import subprocess",
    "subprocess.",
    "os.system",
    "os.popen",
    "Popen(",
)

# canary 允许替换的**进程边界**（与终态链无关）。
BOUNDARY_PATCH_KEYS = ("run_cline", "run_validations", "get_changed_files", "get_diff_stat")


def aborted_facts(**overrides: Any) -> dict[str, Any]:
    """``raw aborted`` 的**正确**事实：exit 0 + validation 全过 + 有变更，但只判 failed。"""

    facts: dict[str, Any] = {
        "raw": canary.RAW_ABORTED,
        "result_present": True,
        "iteration_outcome": canary.STATUS_BLOCKED,
        "attempt_cline_exit_code": 0,
        "attempt_cline_timed_out": False,
        "attempt_execution_outcome": canary.OUTCOME_FAILED,
        "attempt_finish_reason": canary.OUTCOME_FAILED,
        "attempt_normalized_finish_reason": canary.OUTCOME_FAILED,
        "attempt_cline_finish_reason_raw": canary.RAW_ABORTED,
        "attempt_failure_class": "terminal_not_completed",
        "attempt_failure_code": "CLINE_TERMINAL_NOT_COMPLETED",
        "attempt_changed_files": [" M orchestrator/result_terminal_canary_probe.py"],
        "validations_returncodes": [0],
        "result_status": canary.STATUS_BLOCKED,
        "result_execution_outcome": canary.STATUS_BLOCKED,
        "result_normalized_finish_reason": canary.STATUS_BLOCKED,
        "completion_commit_recorded": False,
        "completion_gate_accepted": False,
        "expect_completion_commit": False,
    }

    facts.update(overrides)

    return derive(facts)


def completed_facts(**overrides: Any) -> dict[str, Any]:
    """``raw completed`` 的**正确**事实：正常收尾 + completion commit。"""

    facts: dict[str, Any] = {
        "raw": canary.RAW_COMPLETED,
        "result_present": True,
        "iteration_outcome": canary.STATUS_COMPLETED,
        "attempt_cline_exit_code": 0,
        "attempt_cline_timed_out": False,
        "attempt_execution_outcome": canary.OUTCOME_COMPLETED,
        "attempt_finish_reason": canary.OUTCOME_COMPLETED,
        "attempt_normalized_finish_reason": canary.OUTCOME_COMPLETED,
        "attempt_cline_finish_reason_raw": canary.RAW_COMPLETED,
        "attempt_failure_class": "none",
        "attempt_failure_code": None,
        "attempt_changed_files": [" M orchestrator/result_terminal_canary_probe.py"],
        "validations_returncodes": [0],
        "result_status": canary.STATUS_COMPLETED,
        "result_execution_outcome": canary.OUTCOME_COMPLETED,
        "result_normalized_finish_reason": canary.OUTCOME_COMPLETED,
        "completion_commit_recorded": True,
        "completion_gate_accepted": True,
        "expect_completion_commit": True,
    }

    facts.update(overrides)

    return derive(facts)


def derive(facts: dict[str, Any]) -> dict[str, Any]:
    """与 ``run_scenario`` 同一口径补全派生事实（不是复制判定规则）。"""

    facts["validations_all_passed"] = canary.validations_all_passed(
        facts.get("validations_returncodes")
    )

    facts["produced_completed"] = canary.produced_completed(facts)

    return facts


# ============================================================
# 1. 稳定契约：schema / 退出码 / reason code 词表
# ============================================================


def test_schema_and_exit_codes_are_stable() -> None:
    assert canary.SCHEMA == "gold-ai/result-terminal-canary/v1"

    assert canary.EXIT_PASS == 0

    assert canary.EXIT_FAIL == 1

    assert canary.VERDICT_PASS == "PASS"

    assert canary.VERDICT_FAIL == "FAIL"

    assert canary.PIPELINE_ENTRY == "orchestrator.ai_orchestrator.process_task"

    assert canary.CANARY_BRANCH == "cline-agent"

    assert canary.CANARY_HEAD == "a" * 40


def test_reason_codes_are_stable_unique_and_prefixed() -> None:
    assert len(canary.REASON_CODES) == 16

    assert len(set(canary.REASON_CODES)) == 16

    for code in canary.REASON_CODES:
        assert code.startswith("RESULT_TERMINAL_CANARY_")

        assert code.isupper()

        assert code.isascii()

        # 每个 code 都必须有同名模块常量（供 GPT / 测试 grep）。
        assert getattr(canary, code) == code


def test_emit_maps_verdict_to_exit_code_and_prints_ascii_json() -> None:
    passed = io.StringIO()

    assert (
        canary.emit(
            {"verdict": canary.VERDICT_PASS, "status": canary.VERDICT_PASS, "reason_codes": []},
            passed,
        )
        == canary.EXIT_PASS
    )

    parsed = json.loads(passed.getvalue())

    assert parsed["verdict"] == canary.VERDICT_PASS

    assert passed.getvalue().isascii()

    failed = io.StringIO()

    report = {
        "verdict": canary.VERDICT_FAIL,
        "status": canary.VERDICT_FAIL,
        "reason_codes": [canary.RESULT_TERMINAL_CANARY_RESULT_MISSING],
        "note": "中文说明不得破坏 ASCII JSON 契约",
    }

    assert canary.emit(report, failed) == canary.EXIT_FAIL

    assert failed.getvalue().isascii()

    assert json.loads(failed.getvalue())["reason_codes"] == [
        canary.RESULT_TERMINAL_CANARY_RESULT_MISSING
    ]


# ============================================================
# 2. 假 git：白名单 + commit 证据 + 拒绝未知命令
# ============================================================


def test_recording_git_records_commit_commands() -> None:
    recorder = canary.RecordingGit()

    assert recorder("add -A")["returncode"] == 0

    assert recorder(canary.completion_commit_command("GOLD-CANARY-100"))["returncode"] == 0

    assert recorder('commit -m "ai: blocked GOLD-CANARY-200"')["returncode"] == 0

    assert recorder.commit_commands == [
        canary.completion_commit_command("GOLD-CANARY-100"),
        'commit -m "ai: blocked GOLD-CANARY-200"',
    ]

    # 工作树证据：--porcelain 干净（任务前置），--short 报告 probe 文件。
    assert recorder("status --porcelain")["stdout"] == ""

    assert recorder("status --short")["stdout"] == f" M {canary.PROBE_RELATIVE}\n"

    assert recorder("rev-parse HEAD")["stdout"] == canary.CANARY_HEAD

    assert recorder("rev-list --count origin/cline-agent..HEAD")["stdout"] == "1"

    assert recorder("push origin cline-agent")["returncode"] == 0


def test_recording_git_rejects_unknown_commands() -> None:
    recorder = canary.RecordingGit()

    with pytest.raises(canary.CanaryGitError):
        recorder("rebase --interactive")

    assert recorder.commands == ["rebase --interactive"]

    assert recorder.commit_commands == []


def test_canary_source_never_spawns_real_subprocesses() -> None:
    source = CANARY_SOURCE_PATH.read_text(encoding="utf-8")

    for forbidden in FORBIDDEN_SUBPROCESS_SUBSTRINGS:
        assert forbidden not in source, forbidden


def test_canary_source_never_patches_terminal_chain() -> None:
    source = CANARY_SOURCE_PATH.read_text(encoding="utf-8")

    for name in FORBIDDEN_CHAIN_PATCHES:
        assert f'"{name}":' not in source, name

        assert f'orch.{name} =' not in source, name

    # 允许替换的只有进程外边界。
    for boundary in BOUNDARY_PATCH_KEYS:
        assert f'"{boundary}":' in source, boundary


def test_canary_has_no_platform_specific_terminal_semantics() -> None:
    """Windows / Linux 必须得到相同机器语义：绝不按平台分支或拼路径分隔符。"""

    source = CANARY_SOURCE_PATH.read_text(encoding="utf-8")

    for forbidden in ("os.name", "sys.platform", "platform.system", "os.sep", "\\r\\n"):
        assert forbidden not in source, forbidden

    # 报告字段名与 reason code 都是平台无关的稳定常量。
    assert canary.SCHEMA.isascii()

    for scenario in canary.CANARY_SCENARIOS:
        assert scenario["raw"] in (canary.RAW_COMPLETED, canary.RAW_ABORTED)


# ============================================================
# 3. 只读事实投影
# ============================================================


def test_scenario_facts_reads_attempt_from_result(tmp_path: Path) -> None:
    payload = {
        "task_id": "GOLD-CANARY-200",
        "status": canary.STATUS_BLOCKED,
        "execution_outcome": canary.STATUS_BLOCKED,
        "normalized_finish_reason": canary.STATUS_BLOCKED,
        "changed_files": [],
        "attempts": [
            {
                "cline_exit_code": 0,
                "cline_timed_out": False,
                "execution_outcome": canary.OUTCOME_FAILED,
                "finish_reason": canary.OUTCOME_FAILED,
                "normalized_finish_reason": canary.OUTCOME_FAILED,
                "cline_finish_reason_raw": canary.RAW_ABORTED,
                "failure_class": "terminal_not_completed",
                "failure_code": "CLINE_TERMINAL_NOT_COMPLETED",
                "validations": [{"command": "canary-validation", "returncode": 0}],
                "changed_files": [" M orchestrator/result_terminal_canary_probe.py"],
            }
        ],
    }

    path = tmp_path / "GOLD-CANARY-200.json"

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    facts, present = canary.scenario_facts(path)

    assert present is True

    assert facts is not None

    assert facts["result_status"] == canary.STATUS_BLOCKED

    assert facts["attempt_cline_exit_code"] == 0

    assert facts["attempt_execution_outcome"] == canary.OUTCOME_FAILED

    assert facts["attempt_cline_finish_reason_raw"] == canary.RAW_ABORTED

    assert facts["attempt_failure_code"] == "CLINE_TERMINAL_NOT_COMPLETED"

    assert facts["validations_returncodes"] == [0]

    assert facts["attempt_changed_files"] == [" M orchestrator/result_terminal_canary_probe.py"]

    assert canary.validations_all_passed(facts["validations_returncodes"]) is True

    # 文件损坏 ⇒ 只报告不可读，绝不猜测终态。
    broken = tmp_path / "broken.json"

    broken.write_text("{not json", encoding="utf-8")

    broken_facts, broken_present = canary.scenario_facts(broken)

    assert broken_present is True

    assert broken_facts is None


# ============================================================
# 4. fail-closed 判定：正确事实零 code，退化 / 回归事实必有 code
# ============================================================


def test_correct_facts_produce_no_reason_codes() -> None:
    assert canary.scenario_codes(completed_facts()) == []

    assert canary.scenario_codes(aborted_facts()) == []


def test_missing_result_fails_closed(tmp_path: Path) -> None:
    facts, present = canary.scenario_facts(tmp_path / "missing.json")

    assert present is False

    assert facts is None

    codes = canary.scenario_codes(aborted_facts(result_present=False))

    assert canary.RESULT_TERMINAL_CANARY_RESULT_MISSING in codes


def test_iteration_not_terminal_fails_closed() -> None:
    codes = canary.scenario_codes(completed_facts(iteration_outcome="deferred"))

    assert canary.RESULT_TERMINAL_CANARY_ITERATION_NOT_TERMINAL in codes


def test_degraded_aborted_fixture_fails_closed() -> None:
    degraded = canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_FIXTURE_DEGRADED

    # exit code != 0
    assert degraded in canary.scenario_codes(aborted_facts(attempt_cline_exit_code=1))

    # validation 未全过
    assert degraded in canary.scenario_codes(aborted_facts(validations_returncodes=[1]))

    # 工作树没有变更
    assert degraded in canary.scenario_codes(aborted_facts(attempt_changed_files=[]))


def test_raw_aborted_rewritten_to_completed_fails_closed() -> None:
    codes = canary.scenario_codes(
        aborted_facts(
            attempt_cline_finish_reason_raw=canary.RAW_COMPLETED,
            attempt_execution_outcome=canary.OUTCOME_COMPLETED,
            result_status=canary.STATUS_COMPLETED,
            result_execution_outcome=canary.OUTCOME_COMPLETED,
            result_normalized_finish_reason=canary.OUTCOME_COMPLETED,
            completion_commit_recorded=True,
            completion_gate_accepted=True,
        )
    )

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN in codes

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_ATTEMPT_NOT_FAILED in codes

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED in codes

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT in codes


def test_regressed_raw_aborted_forming_completed_fails_closed() -> None:
    """GOLD-044 式回归：raw ``aborted`` 原样保留，却仍写出 completed + completion commit。"""

    codes = canary.scenario_codes(
        aborted_facts(
            attempt_execution_outcome=canary.OUTCOME_COMPLETED,
            result_status=canary.STATUS_COMPLETED,
            result_execution_outcome=canary.OUTCOME_COMPLETED,
            result_normalized_finish_reason=canary.OUTCOME_COMPLETED,
            completion_commit_recorded=True,
            completion_gate_accepted=True,
        )
    )

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED in codes

    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT in codes

    # raw 值没有被改写（回归不是「改名」造成的，而是「判定」造成的）。
    assert canary.RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN not in codes


def test_completed_without_completion_commit_fails_closed() -> None:
    codes = canary.scenario_codes(completed_facts(completion_commit_recorded=False))

    assert canary.RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_COMPLETION_COMMIT in codes

    gated = canary.scenario_codes(completed_facts(completion_gate_accepted=False))

    assert canary.RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED in gated


def test_completed_missing_raw_evidence_fails_closed() -> None:
    codes = canary.scenario_codes(completed_facts(attempt_cline_finish_reason_raw=None))

    assert canary.RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_RAW_EVIDENCE in codes


def test_scenarios_cover_both_authoritative_raw_terminals() -> None:
    names = [scenario["name"] for scenario in canary.CANARY_SCENARIOS]

    assert names == ["raw_completed", "raw_aborted"]

    raws = [scenario["raw"] for scenario in canary.CANARY_SCENARIOS]

    assert raws == [canary.RAW_COMPLETED, canary.RAW_ABORTED]

    task_ids = [scenario["task_id"] for scenario in canary.CANARY_SCENARIOS]

    assert len(set(task_ids)) == 2

    assert all(task_id.startswith("GOLD-CANARY-") for task_id in task_ids)

    expected = [scenario["expect_completion_commit"] for scenario in canary.CANARY_SCENARIOS]

    assert expected == [True, False]
