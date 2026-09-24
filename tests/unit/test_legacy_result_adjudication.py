"""``orchestrator.legacy_result_adjudication`` 历史矛盾 GPT 裁决契约回归（GOLD-042）。

覆盖（与 GOLD-042 requirements 一一对应）：

1. 版本化 schema + 只读 validator + CLI：裁决记录与 ``.ai/results`` 物理分离，
   绝不替换 / 归一化 / 删除 / 静默修补任何历史 result；
2. 有效裁决必须绑定原 result sha256 + status、completion commit sha + branch、task_id、
   裁决类型、GPT reviewer identity、理由摘要、时间与原始 contradiction reason codes；
3. GPT-only：Cline / DeepSeek / 未知身份 / 错误 reviewer_role 一律 fail-closed；
4. 缺失 / 越权 / 重复 / 冲突 / 过期 / 身份漂移全部 fail-closed，且原始 contradiction
   reason code 继续显式可见（绝不因裁决而消失或降级）；
5. 已知矛盾集合 GOLD-028 / 031 / 035 / 038 / 039 / 040 / 041 的 fixture 形状回归
   （fixture **不是**真实裁决，绝不写入仓库）；
6. 边界：纯只读（无写入 / 无外部进程 / 无 wall-clock 依赖）、不产生任何 review verdict、
   不推进 ``last_reviewed_task``、不改变 Phase 3.3 blocker。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import result_terminal_consistency as terminal

MODULE_SOURCE = Path(adjudication.__file__).read_text(encoding="utf-8")

LEGACY_CODE = terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS

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

# Executor 侧绝不存在的裁决 / 规划 / 状态所有权 API。
FORBIDDEN_PLANNING_API = (
    "write_adjudication",
    "append_adjudication",
    "sign_adjudication",
    "write_review_ledger",
    "append_review_entry",
    "sign_review",
    "update_project_state",
    "generate_follow_on_task",
    "refill_rolling_queue",
    "decide_phase",
    "lift_phase_blocker",
)


# ============================================================
# fixture 工厂（形状与真实历史 result 一致；**不是**真实裁决）
# ============================================================


def result_sha_for(task_id: str) -> str:
    return hashlib.sha256(f"result:{task_id}".encode()).hexdigest()


def commit_sha_for(task_id: str) -> str:
    return hashlib.sha1(f"commit:{task_id}".encode()).hexdigest()


def legacy_contradiction_result(task_id: str) -> dict[str, Any]:
    """历史矛盾形状：顶层 ``status=completed`` + 最终 attempt ``finish_reason=aborted``。"""

    return {
        "task_id": task_id,
        "title": f"{task_id} fixture",
        "status": "completed",
        "finished_at": "2026-09-23T20:07:56+08:00",
        "attempt_count": 1,
        "max_attempts": 3,
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


def consistent_result(task_id: str) -> dict[str, Any]:
    """自洽（归一化）result：没有任何终态矛盾。"""

    return {
        "task_id": task_id,
        "status": "completed",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "attempts": [
            {
                "attempt": 1,
                "cline_exit_code": 0,
                "cline_timed_out": False,
                "failure_class": "none",
                "execution_outcome": "completed",
                "normalized_finish_reason": "completed",
                "finish_reason": "completed",
                "cline_finish_reason_raw": "completed",
                "validations": [{"command": "pytest", "returncode": 0, "timed_out": False}],
            }
        ],
    }


def live_facts_for(
    task_id: str,
    *,
    result_sha: str | None = None,
    status: str = "completed",
    commit_sha: str | None = None,
    branch: str = "cline-agent",
    commit_resolved: bool = True,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由 §2.8 review_binding 同一口径给出的 live facts（测试注入 fake，无 Git）。"""

    return {
        "result_path": f"/tmp/{task_id}.json",
        "result_sha256": result_sha if result_sha is not None else result_sha_for(task_id),
        "result_status": status,
        "result_payload": legacy_contradiction_result(task_id) if payload is None else payload,
        "commit_sha": commit_sha if commit_sha is not None else commit_sha_for(task_id),
        "commit_branch": branch,
        "commit_resolved": commit_resolved,
    }


def make_adjudication(
    task_id: str,
    *,
    result_sha: str | None = None,
    commit_sha: str | None = None,
    branch: str = "cline-agent",
    codes: list[str] | None = None,
    status: str = "completed",
    **extra: Any,
) -> dict[str, Any]:
    """一条形状合法的裁决（默认绑定 :func:`live_facts_for` 的同一任务身份）。"""

    entry: dict[str, Any] = {
        "adjudication_id": f"ADJ-{task_id}-1",
        "task_id": task_id,
        "adjudication_type": adjudication.ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,
        "reviewer": "gpt",
        "reviewer_role": "GPT",
        "reason_summary": (
            "GPT 裁决：raw finish_reason=aborted 属旧版 Orchestrator 写入缺陷，"
            "原 result 逐字节不可改写。"
        ),
        "adjudicated_at": "2026-09-24T10:00:00+08:00",
        "bound_result": {
            "sha256": result_sha if result_sha is not None else result_sha_for(task_id),
            "status": status,
        },
        "bound_commit": {
            "sha": commit_sha if commit_sha is not None else commit_sha_for(task_id),
            "branch": branch,
        },
        "contradiction_reason_codes": codes if codes is not None else [LEGACY_CODE],
    }

    entry.update(extra)

    return entry


def write_store(path: Path, *entries: dict[str, Any]) -> Path:
    payload = {
        "schema": adjudication.STORE_SCHEMA,
        "schema_version": adjudication.STORE_SCHEMA_VERSION,
        "adjudications": list(entries),
    }

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return path


def build_report(
    task_ids: list[str],
    *,
    store_path: Path,
    live: dict[str, dict[str, Any]],
    as_of: str | None = None,
) -> dict[str, Any]:
    return adjudication.build_adjudication_report(
        task_ids, store_path=store_path, live_facts=live, as_of=as_of
    )


def task_view(report: dict[str, Any], task_id: str) -> dict[str, Any]:
    for view in report["tasks"]:
        if view["task_id"] == task_id:
            return view

    raise AssertionError(f"task {task_id} not in report")


# ============================================================
# 1. 职责边界与只读契约（GPT-only / 无写入能力）
# ============================================================


def test_only_gpt_planner_identity_may_sign_adjudication() -> None:
    assert adjudication.can_sign_adjudication("gpt") is True
    assert adjudication.can_sign_adjudication("GPT") is True

    assert adjudication.can_sign_adjudication("cline") is False
    assert adjudication.can_sign_adjudication("deepseek") is False
    assert adjudication.can_sign_adjudication("some-unknown-agent") is False


def test_reviewer_authority_reason_codes_are_distinct() -> None:
    assert adjudication.reviewer_authority("gpt")[1] is None

    assert adjudication.reviewer_authority("cline")[1] == adjudication.ISSUE_EXECUTOR_FORBIDDEN

    assert (
        adjudication.reviewer_authority("nobody")[1] == adjudication.ISSUE_REVIEWER_NOT_AUTHORIZED
    )


def test_authority_contract_denies_every_write_power() -> None:
    authority = adjudication.AUTHORITY

    assert authority["read_only"] is True
    assert authority["pure_function"] is True
    assert authority["emits_review_outcome"] is False
    assert authority["writes_adjudications"] is False
    assert authority["writes_results"] is False
    assert authority["writes_tasks"] is False
    assert authority["writes_project_state"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["tool_can_sign_adjudication"] is False
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_repair_result"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["tool_can_lift_phase_blocker"] is False


def test_contract_is_separate_from_results_and_executor_read_only() -> None:
    contract = adjudication.adjudication_contract()

    assert contract["adjudication_schema"] == adjudication.SCHEMA
    assert contract["adjudication_schema_version"] == adjudication.SCHEMA_VERSION
    assert contract["store_schema"] == adjudication.STORE_SCHEMA
    assert contract["separate_from_results_dir"] is True
    assert contract["store_relative_path"] == adjudication.ADJUDICATION_STORE_RELATIVE_PATH
    assert not adjudication.ADJUDICATION_STORE_RELATIVE_PATH.startswith(".ai/results")
    assert contract["writer_agents"] == ["gpt"]
    assert contract["reviewer_role"] == "GPT"
    assert contract["executor_agents"] == ["cline", "deepseek"]
    assert contract["executor_can_adjudicate"] is False
    assert contract["unknown_identity_can_adjudicate"] is False
    assert contract["module_can_write_adjudication"] is False
    assert contract["module_can_sign_review"] is False
    assert contract["module_can_advance_state"] is False
    assert contract["adjudication_implies_verdict"] is False
    assert contract["adjudication_advances_review_pointer"] is False
    assert contract["adjudication_lifts_phase3_3_blocker"] is False
    assert contract["terminal_consistency_schema"] == terminal.SCHEMA


def test_module_source_has_no_write_or_process_traces() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_exposes_no_planning_or_state_api() -> None:
    for forbidden in FORBIDDEN_PLANNING_API:
        assert f"def {forbidden}" not in MODULE_SOURCE, forbidden


def test_reason_codes_are_stable_and_unique() -> None:
    assert len(adjudication.REASON_CODES) == len(set(adjudication.REASON_CODES))

    for code in adjudication.REASON_CODES:
        assert code.startswith("ADJUDICATION_")
        assert code.isascii()
        assert code.isupper()


# ============================================================
# 2. store 缺失 / 有效裁决 / 原始矛盾始终可见
# ============================================================


def test_missing_store_is_fail_closed_and_never_creates_file(tmp_path: Path) -> None:
    store = tmp_path / "legacy_result_adjudications.json"

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    assert store.exists() is False
    assert report["store"]["present"] is False
    assert report["store"]["adjudication_count"] == 0
    assert report["summary"]["fail_closed"] is True
    assert report["summary"]["exit_code"] == adjudication.EXIT_FAIL_CLOSED
    assert adjudication.ISSUE_STORE_MISSING in {issue["code"] for issue in report["issues"]}

    view = task_view(report, "GOLD-035")

    assert view["state"] == adjudication.TASK_STATE_UNADJUDICATED
    assert view["adjudication"] is None
    assert view["original_contradiction"]["contradiction"] is True
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]
    assert view["original_contradiction"]["format"] == "legacy"
    assert "verdict" not in view


def test_valid_gpt_adjudication_is_adjudicated_and_keeps_original_finding(
    tmp_path: Path,
) -> None:
    store = write_store(tmp_path / "store.json", make_adjudication("GOLD-035"))

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    view = task_view(report, "GOLD-035")

    assert view["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert view["valid"] is True
    assert view["reason_codes"] == []
    assert view["adjudication"]["reviewer"] == "gpt"
    assert view["adjudication"]["reviewer_role"] == "GPT"
    assert view["adjudication"]["bound_result"] == {
        "sha256": result_sha_for("GOLD-035"),
        "status": "completed",
    }
    assert view["adjudication"]["bound_commit"] == {
        "sha": commit_sha_for("GOLD-035"),
        "branch": "cline-agent",
    }
    assert view["adjudication"]["contradiction_reason_codes"] == [LEGACY_CODE]
    # 裁决不删除、不降级、不伪装历史事实
    assert view["original_contradiction"]["contradiction"] is True
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]
    assert view["live_identity"]["result_sha256"] == result_sha_for("GOLD-035")
    assert report["summary"]["adjudicated_count"] == 1
    assert report["summary"]["fail_closed"] is False
    assert report["summary"]["exit_code"] == adjudication.EXIT_OK


def test_self_consistent_result_needs_no_adjudication(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store.json")

    live = {"GOLD-900": live_facts_for("GOLD-900", payload=consistent_result("GOLD-900"))}

    report = build_report(["GOLD-900"], store_path=store, live=live)

    view = task_view(report, "GOLD-900")

    assert view["state"] == adjudication.TASK_STATE_NOT_APPLICABLE
    assert view["original_contradiction"]["contradiction"] is False
    assert view["original_contradiction"]["format"] == "normalized"
    assert view["original_contradiction"]["reason_codes"] == []
    assert report["summary"]["fail_closed"] is False
    assert report["summary"]["exit_code"] == adjudication.EXIT_OK


def test_adjudication_for_self_consistent_result_fails_closed(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store.json", make_adjudication("GOLD-900"))

    live = {"GOLD-900": live_facts_for("GOLD-900", payload=consistent_result("GOLD-900"))}

    view = task_view(build_report(["GOLD-900"], store_path=store, live=live), "GOLD-900")

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_RESULT_NOT_CONTRADICTORY in view["reason_codes"]


# ============================================================
# 3. 越权 / 结构非法裁决一律 fail-closed
# ============================================================


def view_with_entry(
    tmp_path: Path,
    entry: dict[str, Any],
    *,
    task_id: str = "GOLD-035",
    live: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把单条裁决放进 store 并返回 ``(task_view, report)``。"""

    store = write_store(tmp_path / "store.json", entry)

    report = build_report(
        [task_id],
        store_path=store,
        live={task_id: live if live is not None else live_facts_for(task_id)},
        as_of=as_of,
    )

    return task_view(report, task_id), report


def test_executor_reviewer_is_rejected(tmp_path: Path) -> None:
    for executor in ("cline", "deepseek", "Cline"):
        view, _ = view_with_entry(
            tmp_path, make_adjudication("GOLD-035", reviewer=executor)
        )

        assert view["state"] == adjudication.TASK_STATE_INVALID
        assert adjudication.ISSUE_EXECUTOR_FORBIDDEN in view["reason_codes"]


def test_unknown_reviewer_is_rejected(tmp_path: Path) -> None:
    view, _ = view_with_entry(tmp_path, make_adjudication("GOLD-035", reviewer="mystery"))

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_REVIEWER_NOT_AUTHORIZED in view["reason_codes"]


def test_wrong_reviewer_role_is_rejected(tmp_path: Path) -> None:
    view, _ = view_with_entry(tmp_path, make_adjudication("GOLD-035", reviewer_role="Executor"))

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_REVIEWER_ROLE_INVALID in view["reason_codes"]


def test_missing_required_field_is_fail_closed(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035")

    del entry["reason_summary"]

    view, _ = view_with_entry(tmp_path, entry)

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_ENTRY_INVALID in view["reason_codes"]
    assert adjudication.ISSUE_REASON_SUMMARY_MISSING in view["reason_codes"]


def test_invalid_adjudication_type_is_rejected(tmp_path: Path) -> None:
    view, _ = view_with_entry(
        tmp_path, make_adjudication("GOLD-035", adjudication_type="anything_else")
    )

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_ADJUDICATION_TYPE_INVALID in view["reason_codes"]


def test_invalid_bound_result_and_commit_shapes_are_rejected(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035")

    entry["bound_result"] = {"sha256": "deadbeef", "status": "completed"}
    entry["bound_commit"] = {"sha": "abc", "branch": ""}

    view, _ = view_with_entry(tmp_path, entry)

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_BOUND_RESULT_INVALID in view["reason_codes"]
    assert adjudication.ISSUE_BOUND_COMMIT_INVALID in view["reason_codes"]


def test_unknown_contradiction_codes_are_rejected(tmp_path: Path) -> None:
    for codes in ([], ["NOT_A_TERMINAL_CODE"]):
        view, _ = view_with_entry(
            tmp_path, make_adjudication("GOLD-035", codes=codes)
        )

        assert view["state"] == adjudication.TASK_STATE_INVALID
        assert adjudication.ISSUE_CONTRADICTION_CODES_INVALID in view["reason_codes"]


def test_naive_or_broken_timestamps_are_rejected(tmp_path: Path) -> None:
    for timestamp in ("2026-09-24T10:00:00", "not-a-timestamp"):
        view, _ = view_with_entry(
            tmp_path, make_adjudication("GOLD-035", adjudicated_at=timestamp)
        )

        assert view["state"] == adjudication.TASK_STATE_INVALID
        assert adjudication.ISSUE_TIMESTAMP_INVALID in view["reason_codes"]


def test_invalid_task_id_in_store_entry_is_reported(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035")

    entry["task_id"] = "../evil"

    store = write_store(tmp_path / "store.json", entry)

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    assert adjudication.ISSUE_TASK_ID_INVALID in {issue["code"] for issue in report["issues"]}
    assert report["store"]["adjudication_count"] == 1
    assert task_view(report, "GOLD-035")["state"] == adjudication.TASK_STATE_UNADJUDICATED


# ============================================================
# 4. 身份漂移（result / commit / contradiction）一律 fail-closed
# ============================================================


def test_result_sha256_drift_fails_closed(tmp_path: Path) -> None:
    view, _ = view_with_entry(
        tmp_path,
        make_adjudication("GOLD-035"),
        live=live_facts_for("GOLD-035", result_sha="0" * 64),
    )

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_RESULT_SHA256_DRIFT in view["reason_codes"]
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]


def test_result_status_drift_fails_closed(tmp_path: Path) -> None:
    view, _ = view_with_entry(
        tmp_path,
        make_adjudication("GOLD-035"),
        live=live_facts_for("GOLD-035", status="blocked"),
    )

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_RESULT_STATUS_DRIFT in view["reason_codes"]


def test_commit_sha_and_branch_drift_fail_closed(tmp_path: Path) -> None:
    view, _ = view_with_entry(
        tmp_path,
        make_adjudication("GOLD-035"),
        live=live_facts_for("GOLD-035", commit_sha="1" * 40, branch="other-branch"),
    )

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_COMMIT_SHA_DRIFT in view["reason_codes"]
    assert adjudication.ISSUE_COMMIT_BRANCH_DRIFT in view["reason_codes"]


def test_contradiction_code_drift_fails_closed(tmp_path: Path) -> None:
    drifted = legacy_contradiction_result("GOLD-035")

    drifted["attempts"][0]["cline_exit_code"] = 1

    view, _ = view_with_entry(
        tmp_path,
        make_adjudication("GOLD-035"),
        live=live_facts_for("GOLD-035", payload=drifted),
    )

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_CONTRADICTION_CODE_DRIFT in view["reason_codes"]
    assert view["original_contradiction"]["reason_codes"] == [
        terminal.REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS,
        LEGACY_CODE,
    ]


def test_incomplete_live_facts_fail_closed(tmp_path: Path) -> None:
    missing_payload = live_facts_for("GOLD-035")

    missing_payload["result_payload"] = None

    view, _ = view_with_entry(tmp_path, make_adjudication("GOLD-035"), live=missing_payload)

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_LIVE_FACTS_INCOMPLETE in view["reason_codes"]

    unresolved_commit = live_facts_for("GOLD-035", commit_resolved=False, commit_sha=None)

    view, _ = view_with_entry(tmp_path, make_adjudication("GOLD-035"), live=unresolved_commit)

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_LIVE_FACTS_INCOMPLETE in view["reason_codes"]


# ============================================================
# 5. 重复 / 冲突 / 过期裁决一律 fail-closed
# ============================================================


def test_exact_duplicate_adjudications_are_rejected(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035")

    store = write_store(tmp_path / "store.json", entry, dict(entry))

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    view = task_view(report, "GOLD-035")

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert view["adjudication"] is None
    assert adjudication.ISSUE_TASK_ID_DUPLICATE in view["reason_codes"]
    assert adjudication.ISSUE_ADJUDICATION_ID_DUPLICATE in view["reason_codes"]
    assert report["summary"]["exit_code"] == adjudication.EXIT_FAIL_CLOSED


def test_conflicting_adjudications_are_rejected(tmp_path: Path) -> None:
    first = make_adjudication("GOLD-035")

    second = make_adjudication(
        "GOLD-035", adjudication_id="ADJ-GOLD-035-2", reason_summary="另一种解释"
    )

    store = write_store(tmp_path / "store.json", first, second)

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    view = task_view(report, "GOLD-035")

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert view["adjudication"] is None
    assert adjudication.ISSUE_TASK_ID_CONFLICT in view["reason_codes"]
    assert adjudication.ISSUE_TASK_ID_DUPLICATE not in view["reason_codes"]


def test_expiry_without_reference_time_is_unverifiable(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035", expires_at="2026-12-31T00:00:00+08:00")

    view, _ = view_with_entry(tmp_path, entry)

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_EXPIRY_UNVERIFIABLE in view["reason_codes"]


def test_expired_adjudication_fails_closed(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035", expires_at="2026-09-01T00:00:00+08:00")

    view, _ = view_with_entry(tmp_path, entry, as_of="2026-09-24T00:00:00+08:00")

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_EXPIRED in view["reason_codes"]


def test_invalid_expiry_timestamp_fails_closed(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035", expires_at="not-a-time")

    view, _ = view_with_entry(tmp_path, entry, as_of="2026-09-24T00:00:00+08:00")

    assert view["state"] == adjudication.TASK_STATE_INVALID
    assert adjudication.ISSUE_EXPIRY_INVALID in view["reason_codes"]


def test_unexpired_adjudication_with_reference_time_passes(tmp_path: Path) -> None:
    entry = make_adjudication("GOLD-035", expires_at="2026-12-31T00:00:00+08:00")

    view, _ = view_with_entry(tmp_path, entry, as_of="2026-09-24T00:00:00+08:00")

    assert view["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert view["valid"] is True
    assert view["adjudication"]["expires_at"] == "2026-12-31T00:00:00+08:00"


# ============================================================
# 6. store 顶层契约 / 确定性 / 只读渲染
# ============================================================


def test_unsupported_store_schema_fails_closed(tmp_path: Path) -> None:
    store = tmp_path / "store.json"

    store.write_text(
        json.dumps(
            {
                "schema": "gold-ai/other-store/v9",
                "schema_version": 9,
                "adjudications": [make_adjudication("GOLD-035")],
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    assert adjudication.ISSUE_STORE_SCHEMA_UNSUPPORTED in {i["code"] for i in report["issues"]}
    assert report["summary"]["exit_code"] == adjudication.EXIT_FAIL_CLOSED


def test_store_entries_must_be_a_list(tmp_path: Path) -> None:
    store = tmp_path / "store.json"

    store.write_text(
        json.dumps(
            {
                "schema": adjudication.STORE_SCHEMA,
                "schema_version": adjudication.STORE_SCHEMA_VERSION,
                "adjudications": {"not": "a list"},
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    assert adjudication.ISSUE_STORE_ENTRIES_INVALID in {i["code"] for i in report["issues"]}
    assert report["store"]["adjudication_count"] == 0
    assert report["summary"]["exit_code"] == adjudication.EXIT_FAIL_CLOSED


def test_unreadable_store_is_fail_closed(tmp_path: Path) -> None:
    store = tmp_path / "store.json"

    store.write_text("{not json", encoding="utf-8")

    report = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    assert adjudication.ISSUE_STORE_UNREADABLE in {i["code"] for i in report["issues"]}
    assert report["store"]["present"] is False
    assert task_view(report, "GOLD-035")["state"] == adjudication.TASK_STATE_UNADJUDICATED


def test_report_is_deterministic_and_as_of_is_excluded_from_digest(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store.json", make_adjudication("GOLD-035"))

    live = {"GOLD-035": live_facts_for("GOLD-035")}

    first = build_report(["GOLD-035"], store_path=store, live=live)

    second = build_report(
        ["GOLD-035"], store_path=store, live=live, as_of="2026-09-24T00:00:00+08:00"
    )

    assert first["facts_digest"] == second["facts_digest"]
    assert first["as_of"] is None
    assert second["as_of"] == "2026-09-24T00:00:00+08:00"

    rerun = build_report(["GOLD-035"], store_path=store, live=live)

    assert adjudication.render_adjudication_report(
        first
    ) == adjudication.render_adjudication_report(rerun)
    assert adjudication.render_adjudication_report(first).isascii() is True


def test_digest_changes_when_live_identity_changes(tmp_path: Path) -> None:
    store = tmp_path / "store.json"

    baseline = build_report(
        ["GOLD-035"], store_path=store, live={"GOLD-035": live_facts_for("GOLD-035")}
    )

    drifted = build_report(
        ["GOLD-035"],
        store_path=store,
        live={"GOLD-035": live_facts_for("GOLD-035", commit_sha="2" * 40)},
    )

    assert baseline["facts_digest"] != drifted["facts_digest"]


def test_canonical_digest_is_order_independent() -> None:
    assert adjudication.canonical_digest({"b": 1, "a": 2}) == adjudication.canonical_digest(
        {"a": 2, "b": 1}
    )


def test_report_shape_and_task_order_are_stable(tmp_path: Path) -> None:
    report = build_report(
        ["GOLD-035", "GOLD-028"],
        store_path=tmp_path / "store.json",
        live={"GOLD-035": live_facts_for("GOLD-035"), "GOLD-028": live_facts_for("GOLD-028")},
    )

    assert list(report.keys()) == [
        "schema",
        "schema_version",
        "read_only",
        "as_of",
        "store",
        "authority",
        "contract",
        "determinism",
        "tasks",
        "issues",
        "summary",
        "facts_digest",
    ]
    assert [view["task_id"] for view in report["tasks"]] == ["GOLD-028", "GOLD-035"]
    assert list(report["tasks"][0].keys()) == [
        "task_id",
        "state",
        "valid",
        "adjudication",
        "original_contradiction",
        "live_identity",
        "issues",
        "reason_codes",
    ]


# ============================================================
# 7. 已知矛盾集合 fixture 形状 / CLI 只读行为
# ============================================================


@pytest.mark.parametrize("task_id", adjudication.KNOWN_CONTRADICTION_TASKS)
def test_known_contradiction_fixture_is_unadjudicated_without_real_verdict(
    task_id: str,
    tmp_path: Path,
) -> None:
    """fixture 只复现形状：没有真实 GPT 裁决时一律 unadjudicated + fail-closed。"""

    store = tmp_path / "store.json"

    report = build_report([task_id], store_path=store, live={task_id: live_facts_for(task_id)})

    view = task_view(report, task_id)

    assert view["state"] == adjudication.TASK_STATE_UNADJUDICATED
    assert view["original_contradiction"]["contradiction"] is True
    assert view["original_contradiction"]["format"] == "legacy"
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]
    assert view["adjudication"] is None
    assert "verdict" not in view
    assert store.exists() is False
    assert report["summary"]["fail_closed"] is True


def test_known_contradiction_tasks_constant_covers_current_set() -> None:
    assert adjudication.KNOWN_CONTRADICTION_TASKS == (
        "GOLD-028",
        "GOLD-031",
        "GOLD-035",
        "GOLD-038",
        "GOLD-039",
        "GOLD-040",
        "GOLD-041",
    )


def test_cli_writes_ascii_json_and_never_creates_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        adjudication,
        "review_binding_live_facts",
        lambda task_id, **kwargs: live_facts_for(task_id),
    )

    store = tmp_path / "legacy_result_adjudications.json"

    code = adjudication.main(["--task", "GOLD-035", "--store", str(store)])

    captured = capsys.readouterr()

    assert code == adjudication.EXIT_FAIL_CLOSED
    assert store.exists() is False
    assert captured.out.isascii() is True

    report = json.loads(captured.out)

    assert report["schema"] == adjudication.SCHEMA
    assert report["schema_version"] == adjudication.SCHEMA_VERSION
    assert report["read_only"] is True
    assert task_view(report, "GOLD-035")["state"] == adjudication.TASK_STATE_UNADJUDICATED
    assert adjudication.ISSUE_STORE_MISSING in captured.err


def test_cli_exit_zero_when_adjudication_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        adjudication,
        "review_binding_live_facts",
        lambda task_id, **kwargs: live_facts_for(task_id),
    )

    store = write_store(tmp_path / "store.json", make_adjudication("GOLD-035"))

    code = adjudication.main(["--task", "GOLD-035", "--store", str(store)])

    captured = capsys.readouterr()

    assert code == adjudication.EXIT_OK

    view = task_view(json.loads(captured.out), "GOLD-035")

    assert view["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]
