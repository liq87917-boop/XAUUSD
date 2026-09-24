"""``orchestrator.review_evidence_manifest`` 裁决证据包回归（GOLD-043）。

覆盖（与 GOLD-043 requirements 一一对应）：

1. 只读 evidence manifest builder/CLI：复用 §2.8 review_binding / §2.13 terminal
   consistency / §2.15 adjudication / §2.11 backlog 的既有事实算法，不另造身份口径；
2. 逐项输出 task/result sha256、completion commit sha/branch/subject、changed paths、
   validation return codes、terminal consistency findings、ledger/adjudication 状态与
   稳定 reason codes；
3. 四态分类 ``facts-ready`` / ``needs-gpt-adjudication`` / ``pending-substantive-review`` /
   ``invalid`` 明确区分；测试通过 / ``exit_code=0`` 绝不等于 GPT PASS；
4. ``facts_digest`` 排除 wall-clock，相同仓库事实输出确定一致；默认 stdout 只读，
   显式 ``--output`` 仅允许 ``.ai/runtime`` 或系统临时目录；
5. 缺 commit / hash 漂移 / 矛盾裁决 / 不完整事实一律 fail-closed；工具无法自签
   review / adjudication。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import result_terminal_consistency as terminal
from orchestrator import review_evidence_manifest as evidence

MODULE_SOURCE = Path(evidence.__file__).read_text(encoding="utf-8")

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
    "requests",
    "urllib",
    "httpx",
)

# Executor 侧绝不存在的签发 / 规划 / 状态所有权 API。
FORBIDDEN_PLANNING_API = (
    "sign_review",
    "sign_adjudication",
    "write_review_ledger",
    "write_adjudication",
    "append_review_entry",
    "update_project_state",
    "advance_review_pointer",
    "generate_follow_on_task",
    "refill_rolling_queue",
    "decide_phase",
    "lift_phase_blocker",
)


# ============================================================
# fixture 工厂（形状与真实 §2.8 manifest / 历史 result 一致；**不是**真实裁决）
# ============================================================


def result_sha_for(task_id: str) -> str:
    return hashlib.sha256(f"result:{task_id}".encode()).hexdigest()


def commit_sha_for(task_id: str) -> str:
    return hashlib.sha1(f"commit:{task_id}".encode()).hexdigest()


def legacy_contradiction_result(task_id: str) -> dict[str, Any]:
    """历史矛盾形状：顶层 ``status=completed`` + 最终 attempt ``finish_reason=aborted``。"""

    return {
        "task_id": task_id,
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
        "finished_at": "2026-09-23T20:07:56+08:00",
        "attempt_count": 1,
        "max_attempts": 3,
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


def write_result(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return path


def make_manifest(
    task_id: str,
    *,
    result_path: str,
    result_sha: str | None,
    commit_sha: str | None,
    facts_complete: bool,
    reason_codes: list[str],
    resolved: bool = True,
    branch: str = "cline-agent",
    status: str = "completed",
) -> dict[str, Any]:
    """形状与 §2.8 review_binding manifest 一致的注入 fake（零 Git）。"""

    return {
        "schema": "gold-ai/review-binding-manifest/v1",
        "schema_version": 1,
        "task_id": task_id,
        "task": {"path": f"/tasks/{task_id}.json", "sha256": result_sha, "bytes": 10},
        "result": {
            "path": result_path,
            "sha256": result_sha,
            "bytes": 10,
            "status": status,
            "known_status": True,
            "terminal": True,
            "finished_at": "2026-09-23T20:07:56+08:00",
            "attempt_count": 1,
        },
        "commit": {
            "resolved": resolved,
            "reason_code": None if resolved else "COMPLETION_COMMIT_NOT_FOUND",
            "sha": commit_sha,
            "branch": branch,
            "head": "0" * 40,
            "subject": f"ai: complete {task_id}",
            "committed_at": "2026-09-23T20:08:00+08:00",
            "history_scope": "HEAD-reachable git log",
        },
        "validation": {
            "status": "passed",
            "source": "result file attempts[].validations",
            "attempt_count": 1,
            "validation_count": 1,
            "commands": ["pytest"],
            "results": [
                {
                    "attempt": 1,
                    "command": "pytest",
                    "returncode": 0,
                    "timed_out": False,
                    "passed": True,
                }
            ],
            "digest": "0" * 64,
        },
        "binding": {"facts_complete": facts_complete, "reason_codes": reason_codes},
    }


def live_facts(task_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "result_path": f"/tmp/{task_id}.json",
        "result_sha256": result_sha_for(task_id),
        "result_status": "completed",
        "result_payload": legacy_contradiction_result(task_id) if payload is None else payload,
        "commit_sha": commit_sha_for(task_id),
        "commit_branch": "cline-agent",
        "commit_resolved": True,
    }


def make_adjudication(
    task_id: str,
    *,
    result_sha: str | None = None,
    commit_sha: str | None = None,
    codes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "adjudication_id": f"ADJ-{task_id}-1",
        "task_id": task_id,
        "adjudication_type": adjudication.ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,
        "reviewer": "gpt",
        "reviewer_role": "GPT",
        "reason_summary": "GPT 裁决：raw finish_reason=aborted 属旧版写入缺陷。",
        "adjudicated_at": "2026-09-24T10:00:00+08:00",
        "bound_result": {
            "sha256": result_sha if result_sha is not None else result_sha_for(task_id),
            "status": "completed",
        },
        "bound_commit": {
            "sha": commit_sha if commit_sha is not None else commit_sha_for(task_id),
            "branch": "cline-agent",
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

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return path


def adjudication_view(
    task_id: str,
    store: Path,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    collected = evidence.collect_adjudication_store(store)

    return adjudication.evaluate_task(
        task_id,
        entries_by_task=collected["entries_by_task"],
        indexed_entries=collected["indexed_entries"],
        consistency=collected["consistency"],
        live=live_facts(task_id, payload),
        as_of=None,
    )


def build_item(
    task_id: str,
    manifest: dict[str, Any] | None,
    *,
    payload: dict[str, Any] | None = None,
    changed_paths: list[str] | None = None,
    changed_paths_error: str | None = None,
    adjudication_view_value: dict[str, Any] | None = None,
    entry_views: list[dict[str, Any]] | None = None,
    manifest_error: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    return evidence.build_evidence_item(
        task_id,
        manifest,
        payload=payload if payload is not None else legacy_contradiction_result(task_id),
        changed_paths=changed_paths,
        changed_paths_error=changed_paths_error,
        adjudication_view=adjudication_view_value,
        entry_views=entry_views or [],
        manifest_error=manifest_error,
    )


def contradiction_manifest(task_id: str, *, resolved: bool = True) -> dict[str, Any]:
    return make_manifest(
        task_id,
        result_path=f"/tmp/{task_id}.json",
        result_sha=result_sha_for(task_id),
        commit_sha=commit_sha_for(task_id) if resolved else None,
        facts_complete=False,
        reason_codes=[LEGACY_CODE],
        resolved=resolved,
    )


def consistent_manifest(task_id: str) -> dict[str, Any]:
    return make_manifest(
        task_id,
        result_path=f"/tmp/{task_id}.json",
        result_sha=result_sha_for(task_id),
        commit_sha=commit_sha_for(task_id),
        facts_complete=True,
        reason_codes=[],
    )


# ============================================================
# 1. 职责边界 / 确定性契约 / 源码守卫
# ============================================================


def test_authority_and_contract_declare_read_only_and_gpt_only() -> None:
    authority = evidence.authority_section()

    assert authority["read_only"] is True
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_sign_adjudication"] is False
    assert authority["tool_can_advance_review_pointer"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["writes_adjudication"] is False
    assert authority["writes_project_state"] is False
    assert authority["writes_tasks"] is False
    assert authority["writes_results"] is False
    assert authority["network_access"] is False
    assert authority["model_calls"] is False
    assert authority["review_authority"] == "gpt_only"
    assert authority["planner_agents"] == ["gpt"]
    assert authority["executor_agents"] == ["cline", "deepseek"]
    assert authority["classification_values"] == list(evidence.CLASSIFICATIONS)
    assert authority["validation_pass_is_not_review_verdict"] is True
    assert authority["exit_code_zero_is_not_review_pass"] is True


def test_contract_declares_all_four_classifications_and_exit_semantics() -> None:
    contract = evidence.contract_section()

    assert contract["classification_values"] == list(evidence.CLASSIFICATIONS)

    assert set(contract["classification_semantics"]) == set(evidence.CLASSIFICATIONS)

    assert contract["recoverable_contradiction_code"] == LEGACY_CODE
    assert contract["validation_pass_is_not_review_verdict"] is True
    assert contract["exit_code_zero_is_not_review_pass"] is True
    assert contract["exit_code_semantics"]["0"].startswith("客观事实")


def test_determinism_section_excludes_wall_clock() -> None:
    determinism = evidence.determinism_section()

    assert determinism["digest_field"] == "facts_digest"
    assert "generated_at" in determinism["excluded_from_facts"]
    assert determinism["wall_clock_in_facts"] is False
    assert determinism["mtime_used_as_identity"] is False


def test_module_source_has_no_write_or_process_traces() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_exposes_no_signing_or_planning_api() -> None:
    for forbidden in FORBIDDEN_PLANNING_API:
        assert f"def {forbidden}" not in MODULE_SOURCE, forbidden


def test_classification_and_reason_codes_are_stable_ascii() -> None:
    assert len(set(evidence.CLASSIFICATIONS)) == 4

    for value in evidence.CLASSIFICATIONS:
        assert value.isascii()
        assert value == value.strip()

    for code in (
        evidence.ISSUE_PROJECT_STATE_UNREADABLE,
        evidence.ISSUE_LAST_REVIEWED_POINTER_MISSING,
        evidence.ISSUE_ITEM_MANIFEST_UNUSABLE,
        evidence.ISSUE_ITEM_INVALID,
        evidence.ISSUE_ITEM_ADJUDICATION_INVALID,
        evidence.ISSUE_ITEM_LEDGER_INVALID,
        evidence.ISSUE_ITEM_CHANGED_PATHS_UNAVAILABLE,
        evidence.ISSUE_KNOWN_CONTRADICTION_NOT_IN_BACKLOG,
    ):
        assert code.isascii()
        assert code.isupper()


# ============================================================
# 2. 逐项四态分类（facts-ready / needs-gpt-adjudication / pending / invalid）
# ============================================================


def bound_entry(task_id: str) -> dict[str, Any]:
    """形状与 §2.11 ledger 条目一致的合法绑定（身份与 :func:`consistent_manifest` 匹配）。"""

    return {
        "task_id": task_id,
        "valid": True,
        "reviewed_result": {
            "result_sha256": result_sha_for(task_id),
            "status": "completed",
            "finished_at": "2026-09-23T20:07:56+08:00",
        },
        "reviewed_commit": {"sha": commit_sha_for(task_id), "branch": "cline-agent"},
    }


def test_consistent_item_is_pending_substantive_review() -> None:
    item, issues = build_item(
        "GOLD-029",
        consistent_manifest("GOLD-029"),
        payload=consistent_result("GOLD-029"),
        changed_paths=["src/a.py"],
    )

    assert item["classification"] == evidence.CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW
    assert item["facts_complete"] is True
    assert item["terminal_consistency"]["contradiction"] is False
    assert item["ledger"]["status"] == evidence.LEDGER_STATUS_ABSENT
    assert item["commit"]["changed_paths"] == ["src/a.py"]
    assert item["validation"]["return_codes"][0]["returncode"] == 0
    assert issues == []


def test_legacy_contradiction_without_adjudication_needs_adjudication(tmp_path: Path) -> None:
    store = tmp_path / "store.json"

    view = adjudication_view("GOLD-035", store)

    item, issues = build_item(
        "GOLD-035",
        contradiction_manifest("GOLD-035"),
        adjudication_view_value=view,
        changed_paths=["src/a.py"],
    )

    assert item["classification"] == evidence.CLASSIFICATION_NEEDS_GPT_ADJUDICATION
    assert item["facts_complete"] is False
    assert item["terminal_consistency"]["contradiction"] is True
    assert item["terminal_consistency"]["format"] == "legacy"
    assert item["terminal_consistency"]["reason_codes"] == [LEGACY_CODE]
    assert LEGACY_CODE in item["missing_reason_codes"]
    assert item["adjudication"]["state"] == adjudication.TASK_STATE_UNADJUDICATED
    assert issues == []


def test_valid_gpt_adjudication_yields_facts_ready(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store.json", make_adjudication("GOLD-035"))

    view = adjudication_view("GOLD-035", store)

    item, issues = build_item(
        "GOLD-035",
        contradiction_manifest("GOLD-035"),
        adjudication_view_value=view,
        changed_paths=["src/a.py"],
    )

    assert item["classification"] == evidence.CLASSIFICATION_FACTS_READY
    assert item["adjudication"]["state"] == adjudication.TASK_STATE_ADJUDICATED
    assert item["adjudication"]["adjudication"]["reviewer"] == "gpt"

    # 原始矛盾事实绝不因裁决而消失或降级。
    assert item["terminal_consistency"]["contradiction"] is True
    assert item["terminal_consistency"]["reason_codes"] == [LEGACY_CODE]
    assert LEGACY_CODE in item["reason_codes"]
    assert issues == []


def test_conflicting_adjudication_is_invalid_and_fail_closed(tmp_path: Path) -> None:
    store = write_store(
        tmp_path / "store.json",
        make_adjudication("GOLD-035"),
        make_adjudication("GOLD-035", adjudication_id="ADJ-2", reason_summary="冲突裁决"),
    )

    view = adjudication_view("GOLD-035", store)

    item, issues = build_item(
        "GOLD-035",
        contradiction_manifest("GOLD-035"),
        adjudication_view_value=view,
    )

    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert item["adjudication"]["state"] == adjudication.TASK_STATE_INVALID
    assert any(issue["code"] == evidence.ISSUE_ITEM_ADJUDICATION_INVALID for issue in issues)


def test_executor_adjudication_is_invalid(tmp_path: Path) -> None:
    store = write_store(
        tmp_path / "store.json",
        make_adjudication("GOLD-035", reviewer="cline", reviewer_role="Executor"),
    )

    view = adjudication_view("GOLD-035", store)

    item, _issues = build_item(
        "GOLD-035",
        contradiction_manifest("GOLD-035"),
        adjudication_view_value=view,
    )

    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert item["adjudication"]["state"] == adjudication.TASK_STATE_INVALID


def test_missing_commit_is_invalid() -> None:
    manifest = make_manifest(
        "GOLD-029",
        result_path="/tmp/GOLD-029.json",
        result_sha=result_sha_for("GOLD-029"),
        commit_sha=None,
        facts_complete=False,
        reason_codes=["COMPLETION_COMMIT_NOT_FOUND"],
        resolved=False,
    )

    item, issues = build_item("GOLD-029", manifest, payload=consistent_result("GOLD-029"))

    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert item["commit"]["resolved"] is False
    assert any(issue["code"] == evidence.ISSUE_ITEM_INVALID for issue in issues)


def test_normalized_contradiction_is_invalid_not_recoverable() -> None:
    """归一化格式的矛盾**没有** §2.15 恢复路径 ⇒ 必须 fail-closed，绝不当成待裁决。"""

    payload = {
        "task_id": "GOLD-029",
        "status": "completed",
        "execution_outcome": "completed",
        "normalized_finish_reason": "completed",
        "attempts": [
            {
                "attempt": 1,
                "execution_outcome": "failed",
                "normalized_finish_reason": "failed",
                "validations": [{"command": "pytest", "returncode": 1, "timed_out": False}],
            }
        ],
    }

    manifest = make_manifest(
        "GOLD-029",
        result_path="/tmp/GOLD-029.json",
        result_sha=result_sha_for("GOLD-029"),
        commit_sha=commit_sha_for("GOLD-029"),
        facts_complete=False,
        reason_codes=[terminal.REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS],
    )

    item, _issues = build_item("GOLD-029", manifest, payload=payload)

    assert item["terminal_consistency"]["contradiction"] is True
    assert item["terminal_consistency"]["format"] == "normalized"
    assert item["classification"] == evidence.CLASSIFICATION_INVALID


def test_unavailable_changed_paths_for_resolved_commit_is_invalid() -> None:
    item, issues = build_item(
        "GOLD-029",
        consistent_manifest("GOLD-029"),
        payload=consistent_result("GOLD-029"),
        changed_paths=None,
        changed_paths_error="fatal: bad object",
    )

    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert item["commit"]["changed_paths_reason_code"] == "fatal: bad object"
    assert any(
        issue["code"] == evidence.ISSUE_ITEM_CHANGED_PATHS_UNAVAILABLE for issue in issues
    )


def test_bound_ledger_matching_identity_is_facts_ready() -> None:
    item, issues = build_item(
        "GOLD-029",
        consistent_manifest("GOLD-029"),
        payload=consistent_result("GOLD-029"),
        changed_paths=["src/a.py"],
        entry_views=[bound_entry("GOLD-029")],
    )

    assert item["ledger"]["status"] == evidence.LEDGER_STATUS_BOUND
    assert item["classification"] == evidence.CLASSIFICATION_FACTS_READY
    assert issues == []


def test_ledger_identity_drift_is_invalid() -> None:
    drift_entry = bound_entry("GOLD-029")

    drift_entry["reviewed_result"]["result_sha256"] = "f" * 64

    item, issues = build_item(
        "GOLD-029",
        consistent_manifest("GOLD-029"),
        payload=consistent_result("GOLD-029"),
        changed_paths=["src/a.py"],
        entry_views=[drift_entry],
    )

    assert item["ledger"]["status"] == evidence.LEDGER_STATUS_INVALID
    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert any(issue["code"] == "BACKLOG_ITEM_RESULT_HASH_DRIFT" for issue in issues)


def test_unusable_manifest_is_invalid() -> None:
    item, issues = build_item("GOLD-035", None, manifest_error="RuntimeError: boom")

    assert item["classification"] == evidence.CLASSIFICATION_INVALID
    assert item["facts_complete"] is False
    assert any(issue["code"] == evidence.ISSUE_ITEM_MANIFEST_UNUSABLE for issue in issues)


# ============================================================
# 3. manifest 组装（注入 fake，零 Git；只读、确定性、fail-closed）
# ============================================================


def make_repo(tmp_path: Path, *, last_reviewed: str = "GOLD-027") -> dict[str, Path]:
    ai = tmp_path / ".ai"

    results = ai / "results"

    tasks = ai / "tasks"

    tasks.mkdir(parents=True, exist_ok=True)

    state = ai / "PROJECT_STATE.json"

    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "XAUUSD",
                "branch": "cline-agent",
                "phase": "Phase 3",
                "status": "BLOCKED",
                "current_task": "GOLD-029",
                "last_completed_task": "GOLD-029",
                "last_reviewed_task": last_reviewed,
                "invariants": ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"],
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    ledger = ai / "GPT_REVIEW_LEDGER.json"

    ledger.write_text(
        json.dumps(
            {
                "schema": "gold-ai/gpt-review-ledger/v1",
                "schema_version": 1,
                "reviewed_from": "GOLD-025",
                "entries": [],
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    store = ai / "adjudications" / "legacy_result_adjudications.json"

    return {
        "root": tmp_path,
        "tasks": tasks,
        "results": results,
        "state": state,
        "ledger": ledger,
        "store": store,
    }


def make_spec_builder(
    paths: dict[str, Path],
    specs: dict[str, tuple[dict[str, Any], bool, list[str]]],
) -> evidence.ManifestBuilder:
    """预写 result 文件（让 backlog 边界可见），再按 spec 返回注入 manifest。"""

    result_paths: dict[str, str] = {}

    for task_id, (payload, _facts_complete, _codes) in specs.items():
        result_paths[task_id] = str(write_result(paths["results"] / f"{task_id}.json", payload))

    def build(task_id: str) -> dict[str, Any]:
        _payload, facts_complete, codes = specs[task_id]

        return make_manifest(
            task_id,
            result_path=result_paths[task_id],
            result_sha=result_sha_for(task_id),
            commit_sha=commit_sha_for(task_id),
            facts_complete=facts_complete,
            reason_codes=codes,
        )

    return build


def build_manifest(
    paths: dict[str, Path],
    builder: evidence.ManifestBuilder,
    *,
    generated_at: str,
) -> dict[str, Any]:
    return evidence.build_review_evidence_manifest(
        root=paths["root"],
        tasks_dir=paths["tasks"],
        results_dir=paths["results"],
        state_path=paths["state"],
        ledger_path=paths["ledger"],
        adjudication_store_path=paths["store"],
        generated_at=generated_at,
        manifest_builder=builder,
        changed_paths_provider=lambda root, sha: (["src/a.py"], None),
    )


def test_build_manifest_is_deterministic_and_classifies_backlog(tmp_path: Path) -> None:
    paths = make_repo(tmp_path)

    builder = make_spec_builder(
        paths,
        {
            "GOLD-028": (legacy_contradiction_result("GOLD-028"), False, [LEGACY_CODE]),
            "GOLD-029": (consistent_result("GOLD-029"), True, []),
        },
    )

    first = build_manifest(paths, builder, generated_at="2026-09-24T00:00:00+08:00")

    second = build_manifest(paths, builder, generated_at="2026-09-25T01:02:03+08:00")

    assert first["schema"] == evidence.SCHEMA
    assert first["schema_version"] == evidence.SCHEMA_VERSION
    assert first["read_only"] is True

    # wall-clock 不参与内容身份：不同 generated_at ⇒ 同一 facts_digest。
    assert first["facts_digest"] == second["facts_digest"]
    assert first["generated_at"] != second["generated_at"]

    assert [item["task_id"] for item in first["items"]] == ["GOLD-028", "GOLD-029"]

    assert first["items"][0]["classification"] == evidence.CLASSIFICATION_NEEDS_GPT_ADJUDICATION
    assert first["items"][1]["classification"] == evidence.CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW

    assert first["coverage"]["last_reviewed_task_pointer"] == "GOLD-027"
    assert first["coverage"]["item_count"] == 2
    assert first["adjudication"]["present"] is False
    assert paths["store"].exists() is False

    assert first["summary"]["needs_gpt_adjudication_count"] == 1
    assert first["summary"]["pending_substantive_review_count"] == 1
    assert first["summary"]["invalid_count"] == 0
    assert first["summary"]["exit_code"] == evidence.EXIT_FAIL_CLOSED

    for key in evidence.FORBIDDEN_MANIFEST_KEYS:
        assert key not in first


def test_all_pending_backlog_is_exit_zero_but_not_a_review_pass(tmp_path: Path) -> None:
    paths = make_repo(tmp_path)

    builder = make_spec_builder(
        paths,
        {"GOLD-029": (consistent_result("GOLD-029"), True, [])},
    )

    payload = build_manifest(paths, builder, generated_at="t")

    assert payload["summary"]["exit_code"] == evidence.EXIT_OK
    assert payload["summary"]["pending_substantive_review_count"] == 1
    assert payload["summary"]["needs_gpt_adjudication_count"] == 0

    # exit_code=0 只是「客观事实可复算」，绝不是 GPT PASS。
    assert payload["authority"]["exit_code_zero_is_not_review_pass"] is True
    assert payload["contract"]["validation_pass_is_not_review_verdict"] is True
    assert "verdict" not in payload


def test_known_contradiction_skipped_by_pointer_is_fail_closed(tmp_path: Path) -> None:
    paths = make_repo(tmp_path, last_reviewed="GOLD-035")

    builder = make_spec_builder(
        paths,
        {"GOLD-028": (legacy_contradiction_result("GOLD-028"), False, [LEGACY_CODE])},
    )

    payload = build_manifest(paths, builder, generated_at="t")

    codes = {issue["code"] for issue in payload["issues"]}

    assert evidence.ISSUE_KNOWN_CONTRADICTION_NOT_IN_BACKLOG in codes
    assert payload["summary"]["exit_code"] == evidence.EXIT_FAIL_CLOSED


# ============================================================
# 4. CLI（只读 stdout / 受控 --output / 纯 ASCII 机器通道）
# ============================================================


def test_git_changed_paths_rejects_invalid_sha() -> None:
    paths, error = evidence.git_changed_paths(Path("."), "not-a-sha")

    assert paths is None
    assert error is not None


def test_cli_stdout_is_deterministic_ascii_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = make_repo(tmp_path)

    builder = make_spec_builder(
        paths,
        {"GOLD-028": (legacy_contradiction_result("GOLD-028"), False, [LEGACY_CODE])},
    )

    monkeypatch.setattr(
        evidence, "default_manifest_builder", lambda root, tasks_dir, results_dir: builder
    )

    monkeypatch.setattr(evidence, "git_changed_paths", lambda root, sha: (["src/a.py"], None))

    def run() -> tuple[int, str]:
        code = evidence.main(["--root", str(tmp_path), "--generated-at", "t"])

        return code, capsys.readouterr().out

    first_code, first_out = run()

    second_code, second_out = run()

    assert first_code == second_code == evidence.EXIT_FAIL_CLOSED
    assert first_out == second_out
    assert first_out.isascii() is True

    payload = json.loads(first_out)

    assert payload["schema"] == evidence.SCHEMA
    assert payload["read_only"] is True
    assert [item["task_id"] for item in payload["items"]] == ["GOLD-028"]
    assert payload["items"][0]["classification"] == evidence.CLASSIFICATION_NEEDS_GPT_ADJUDICATION


def test_cli_output_rejects_forbidden_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = make_repo(tmp_path)

    monkeypatch.setattr(evidence, "git_changed_paths", lambda root, sha: ([], None))

    target = paths["results"] / "evil.json"

    code = evidence.main(["--root", str(tmp_path), "--output", str(target)])

    captured = capsys.readouterr()

    assert code == evidence.snapshot_output.EXIT_OUTPUT_REJECTED
    assert target.exists() is False
    assert evidence.snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err


def test_cli_output_allowed_only_in_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_repo(tmp_path)

    monkeypatch.setattr(evidence, "git_changed_paths", lambda root, sha: ([], None))

    target = tmp_path / ".ai" / "runtime" / "evidence.json"

    code = evidence.main(
        ["--root", str(tmp_path), "--generated-at", "t", "--output", str(target)]
    )

    captured = capsys.readouterr()

    assert code == evidence.EXIT_OK
    assert target.exists() is True
    assert captured.out == ""

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema"] == evidence.SCHEMA
    assert payload["summary"]["item_count"] == 0


def test_cli_reports_unavailable_state_as_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_repo(tmp_path)

    (tmp_path / ".ai" / "PROJECT_STATE.json").unlink()

    monkeypatch.setattr(evidence, "git_changed_paths", lambda root, sha: ([], None))

    code = evidence.main(["--root", str(tmp_path)])

    assert code == evidence.EXIT_UNAVAILABLE
