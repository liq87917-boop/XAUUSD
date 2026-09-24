"""``orchestrator.review_backlog`` 只读契约回归（GOLD-037）。

覆盖：
1. **GOLD-028~033 式连续 backlog**：``last_reviewed_task`` 之后所有 ``completed`` result
   一次性列出，逐项复用 ``review_binding`` 的客观事实（本测试注入确定性 fake manifest，
   零子进程 / 零 Git；SHA-256 由测试自己用 ``hashlib`` 复算）；
2. **fail-closed 场景**：部分已绑定 + 指针滞后、result hash 漂移、commit 不可绑定
   （缺失 / 歧义）、ledger 覆盖窗口缺口、ledger 重复条目、ledger / PROJECT_STATE 不可用、
   指针缺失 —— 全部给出稳定 reason code 且 ``review_status=invalid``；
3. **不产结论**：``review_status`` 只有客观 ``pending`` / ``bound`` / ``invalid``，
   manifest 里绝无 ``verdict`` 等 Review 结论字段，渲染结果里也绝不出现 PASS / FAIL；
4. **Executor 不能借此自签 review**：``authority`` 全 False、没有 ledger / state 写入 API、
   源码里没有写入 / 子进程路径（源码守卫）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import review_backlog as backlog
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

RESULT_FINISHED_AT = "2026-09-23T16:57:20+08:00"

REVIEWED_AT = "2026-09-23T20:00:00+08:00"

REVIEWED_AT_LATER = "2026-09-23T21:00:00+08:00"

BRANCH = "cline-agent"

HEAD_SHA = "8d8173eee904a4d9c652a8067f2b19e415983245"

COMMIT_AMBIGUOUS = "COMPLETION_COMMIT_AMBIGUOUS"

COMMIT_NOT_FOUND = "COMPLETION_COMMIT_NOT_FOUND"

CONTINUOUS_TASKS = ["GOLD-027"] + [f"GOLD-{number:03d}" for number in range(28, 34)]

MODULE_SOURCE = Path(backlog.__file__).read_text(encoding="utf-8")

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
    "json.dump(",
    "hashlib.md5",
)

# Executor 侧绝不存在的 review / planner 写入 API。
FORBIDDEN_PLANNING_API = {
    "write_review_ledger",
    "append_review_entry",
    "sign_review",
    "record_verdict",
    "advance_review_pointer",
    "update_project_state",
    "generate_follow_on_task",
    "refill_rolling_queue",
}


# ============================================================
# 确定性 fake 事实（零子进程 / 零 Git）
# ============================================================


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def commit_sha(task_id: str) -> str:
    """确定性 40 位 commit sha（只用于测试，绝不代表真实 Git 身份）。"""

    return hashlib.sha1(task_id.encode("utf-8")).hexdigest()


def write_result(
    results: Path,
    task_id: str,
    status: str = "completed",
    finished_at: str = RESULT_FINISHED_AT,
) -> str:
    payload = {
        "task_id": task_id,
        "status": status,
        "finished_at": finished_at,
        "attempts": [{"attempt": 1, "validations": []}],
    }

    path = results / f"{task_id}.json"

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return sha256_hex(path.read_bytes())


def write_task(tasks: Path, task_id: str) -> None:
    payload = {"task_id": task_id, "title": task_id, "human_gate": "L1"}

    (tasks / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def seed(results: Path, tasks: Path, task_ids: list[str]) -> dict[str, str]:
    shas: dict[str, str] = {}

    for task_id in task_ids:
        shas[task_id] = write_result(results, task_id)

        write_task(tasks, task_id)

    return shas


def write_state(
    state_path: Path,
    *,
    last_reviewed_task: str | None = None,
    last_completed_task: str | None = None,
    current_task: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "project": "XAUUSD",
        "branch": BRANCH,
        "phase": "Phase 3",
        "status": "BLOCKED",
    }

    pointers = {
        "last_reviewed_task": last_reviewed_task,
        "last_completed_task": last_completed_task,
        "current_task": current_task,
    }

    for key, value in pointers.items():
        if value is not None:
            payload[key] = value

    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def ledger_entry(
    task_id: str,
    result_sha256: str,
    *,
    commit: str | None = None,
    status: str = "completed",
    finished_at: str = RESULT_FINISHED_AT,
    reviewer: str = "gpt",
    reviewed_at: str = REVIEWED_AT,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "verdict": "PASS",
        "reviewer_role": "GPT",
        "reviewer": reviewer,
        "acceptance_summary": f"{task_id}: fake GPT review summary",
        "reviewed_result": {
            "result_sha256": result_sha256,
            "status": status,
            "finished_at": finished_at,
        },
        "reviewed_commit": {
            "sha": commit if commit is not None else commit_sha(task_id),
            "branch": BRANCH,
        },
        "reviewed_at": reviewed_at,
    }


def write_ledger(
    ledger_path: Path,
    entries: list[dict[str, Any]],
    *,
    reviewed_from: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
        "entries": entries,
    }

    if reviewed_from is not None:
        payload["reviewed_from"] = reviewed_from

    ledger_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def manifest_for(
    task_id: str,
    result_sha256: str | None,
    *,
    facts_complete: bool = True,
    reason_codes: list[str] | None = None,
    commit: str | None = None,
    result_status: str = "completed",
) -> dict[str, Any]:
    """构造与 ``review_binding`` 同 shape 的**事实子集** manifest（facts only）。"""

    resolved = commit is not None

    return {
        "schema": binding.REVIEW_BINDING_SCHEMA,
        "schema_version": binding.REVIEW_BINDING_SCHEMA_VERSION,
        "generated_at": AUDIT_TIME,
        "read_only": True,
        "task_id": task_id,
        "result": {
            "path": f"{task_id}.json",
            "sha256": result_sha256,
            "bytes": 128,
            "worktree_sha256": result_sha256,
            "worktree_matches_commit": True,
            "status": result_status,
            "known_status": True,
            "terminal": True,
            "finished_at": RESULT_FINISHED_AT,
            "attempt_count": 1,
        },
        "commit": {
            "resolved": resolved,
            "reason_code": None if resolved else (reason_codes or [None])[0],
            "sha": commit,
            "branch": BRANCH if resolved else None,
            "head": HEAD_SHA,
            "subject": f"ai: complete {task_id}" if resolved else None,
            "committed_at": RESULT_FINISHED_AT if resolved else None,
        },
        "binding": {
            "facts_complete": facts_complete,
            "reason_codes": sorted(reason_codes or []),
            "human_gate": "L1",
            "blocking_human_gate": False,
            "requires_human_approval": False,
        },
        "issues": [],
        "summary": {"exit_code": 0},
    }


def builder_from(manifests: dict[str, dict[str, Any]]) -> backlog.ManifestBuilder:
    def build(task_id: str) -> dict[str, Any]:
        return manifests[task_id]

    return build


def file_manifest_builder(results: Path) -> backlog.ManifestBuilder:
    """按 result 文件真实字节复算 sha256 的确定性 builder（零 Git）。"""

    def build(task_id: str) -> dict[str, Any]:
        return manifest_for(
            task_id,
            sha256_hex((results / f"{task_id}.json").read_bytes()),
            commit=commit_sha(task_id),
        )

    return build


@dataclass(frozen=True)
class BacklogFS:
    root: Path
    tasks: Path
    results: Path
    state: Path
    ledger: Path


@pytest.fixture()
def backlog_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BacklogFS:
    """把 tasks / results / PROJECT_STATE / ledger 全部指向 tmp 目录（零真实仓库影响）。"""

    root = tmp_path / "repo"

    tasks = root / ".ai" / "tasks"
    results = root / ".ai" / "results"

    tasks.mkdir(parents=True)
    results.mkdir(parents=True)

    monkeypatch.setattr(backlog, "ROOT", root)

    return BacklogFS(
        root=root,
        tasks=tasks,
        results=results,
        state=root / ".ai" / "PROJECT_STATE.json",
        ledger=root / ".ai" / "GPT_REVIEW_LEDGER.json",
    )


def build(
    fs: BacklogFS,
    builder: backlog.ManifestBuilder,
    *,
    generated_at: str | None = AUDIT_TIME,
) -> dict[str, Any]:
    return backlog.build_review_backlog_manifest(
        root=fs.root,
        tasks_dir=fs.tasks,
        results_dir=fs.results,
        state_path=fs.state,
        ledger_path=fs.ledger,
        generated_at=generated_at,
        manifest_builder=builder,
    )


def items_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["task_id"]): item for item in payload["backlog"]}


def collect_keys(payload: object) -> set[str]:
    """递归收集 key；``adjudication`` 子树是 §2.15 裁决事实，不参与结论字段断言（GOLD-044）。"""

    found: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))

            if str(key) == "adjudication":
                continue

            found |= collect_keys(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_keys(item)

    return found


def collect_string_values(payload: object) -> set[str]:
    """递归收集所有字符串叶子值（用于断言输出里没有 verdict 值）。"""

    found: set[str] = set()

    if isinstance(payload, dict):
        for value in payload.values():
            found |= collect_string_values(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_string_values(item)

    elif isinstance(payload, str):
        found.add(payload)

    return found


# ============================================================
# 1. GOLD-028~033 式连续 backlog
# ============================================================


def test_continuous_backlog_after_last_reviewed_is_all_pending(backlog_fs: BacklogFS) -> None:
    shas = seed(backlog_fs.results, backlog_fs.tasks, CONTINUOUS_TASKS)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-033",
        current_task="GOLD-034",
    )

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert payload["coverage"]["last_reviewed_task_pointer"] == "GOLD-027"
    assert payload["coverage"]["backlog_count"] == 6

    ids = [item["task_id"] for item in payload["backlog"]]

    assert ids == [f"GOLD-{number:03d}" for number in range(28, 34)]

    for item in payload["backlog"]:
        assert item["review_status"] == backlog.REVIEW_STATUS_PENDING
        assert item["facts_complete"] is True
        assert item["missing_reason_codes"] == []
        assert item["reason_codes"] == []
        assert item["ledger"]["entry_present"] is False
        assert item["result"]["status"] == "completed"
        assert item["result"]["sha256"] == shas[item["task_id"]]
        assert item["commit"]["sha"] == commit_sha(str(item["task_id"]))
        assert item["commit"]["subject"] == f"ai: complete {item['task_id']}"

    assert payload["ledger"]["valid_entry_task_ids"] == ["GOLD-027"]
    assert payload["missing_reason_codes"] == []
    assert payload["reason_codes"] == []
    assert payload["issues"] == []
    assert payload["summary"]["pending_count"] == 6
    assert payload["summary"]["bound_count"] == 0
    assert payload["summary"]["invalid_count"] == 0
    assert payload["summary"]["exit_code"] == backlog.EXIT_OK
    assert payload["schema"] == backlog.REVIEW_BACKLOG_SCHEMA


def test_backlog_digest_is_idempotent_and_content_sensitive(backlog_fs: BacklogFS) -> None:
    shas = seed(backlog_fs.results, backlog_fs.tasks, CONTINUOUS_TASKS)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-033",
        current_task="GOLD-034",
    )

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    builder = file_manifest_builder(backlog_fs.results)

    first = build(backlog_fs, builder)

    second = build(backlog_fs, builder)

    assert first["backlog_digest"] == second["backlog_digest"]

    assert backlog.render_review_backlog_manifest(first) == backlog.render_review_backlog_manifest(
        second
    )

    # wall-clock 审计字段绝不参与 digest
    later = build(backlog_fs, builder, generated_at="2026-09-24T00:00:00+08:00")

    assert later["generated_at"] != first["generated_at"]
    assert later["backlog_digest"] == first["backlog_digest"]

    # result 内容（客观身份）一变，digest 必须跟着变
    write_result(backlog_fs.results, "GOLD-030", finished_at="2026-09-23T19:00:00+08:00")

    third = build(backlog_fs, builder)

    assert third["backlog_digest"] != first["backlog_digest"]


# ============================================================
# 2. fail-closed：部分已绑定 / hash 漂移 / commit 不可绑定
# ============================================================


def test_partially_bound_backlog_marks_bound_items_and_pointer_lag_fail_closed(
    backlog_fs: BacklogFS,
) -> None:
    tasks = ["GOLD-027", "GOLD-028", "GOLD-029", "GOLD-030"]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-030",
        current_task="GOLD-031",
    )

    write_ledger(
        backlog_fs.ledger,
        [
            ledger_entry("GOLD-027", shas["GOLD-027"]),
            ledger_entry("GOLD-028", shas["GOLD-028"]),
        ],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    by_id = items_by_id(payload)

    assert sorted(by_id) == ["GOLD-028", "GOLD-029", "GOLD-030"]

    assert by_id["GOLD-028"]["review_status"] == backlog.REVIEW_STATUS_BOUND
    assert by_id["GOLD-028"]["ledger"]["entry_valid"] is True
    assert by_id["GOLD-028"]["ledger"]["result_sha256_matches"] is True
    assert by_id["GOLD-028"]["ledger"]["commit_sha_matches"] is True
    assert by_id["GOLD-029"]["review_status"] == backlog.REVIEW_STATUS_PENDING

    assert payload["summary"]["bound_count"] == 1
    assert payload["summary"]["pending_count"] == 2

    # ledger 已绑定到 GOLD-028，但 last_reviewed_task 仍停在 GOLD-027 ⇒ 指针漂移 fail-closed
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED
    assert ledger_mod.ISSUE_POINTER_BEHIND_LEDGER in payload["reason_codes"]


def test_ledger_result_hash_drift_is_fail_closed(backlog_fs: BacklogFS) -> None:
    tasks = ["GOLD-027", "GOLD-028"]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-028",
        current_task="GOLD-029",
    )

    write_ledger(
        backlog_fs.ledger,
        [
            ledger_entry("GOLD-027", shas["GOLD-027"]),
            ledger_entry("GOLD-028", "0" * 64),
        ],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    item = items_by_id(payload)["GOLD-028"]

    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert item["ledger"]["entry_present"] is True
    assert item["ledger"]["entry_valid"] is True
    assert item["ledger"]["result_sha256_matches"] is False
    assert item["ledger"]["commit_sha_matches"] is True
    assert backlog.ISSUE_BACKLOG_ITEM_RESULT_HASH_DRIFT in item["reason_codes"]
    assert payload["summary"]["invalid_count"] == 1
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED


def test_unbindable_commit_is_fail_closed_and_never_guessed(backlog_fs: BacklogFS) -> None:
    tasks = ["GOLD-027", "GOLD-028", "GOLD-029"]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-029",
        current_task="GOLD-030",
    )

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    manifests = {
        "GOLD-027": manifest_for("GOLD-027", shas["GOLD-027"], commit=commit_sha("GOLD-027")),
        "GOLD-028": manifest_for(
            "GOLD-028",
            None,
            facts_complete=False,
            reason_codes=[COMMIT_AMBIGUOUS],
        ),
        "GOLD-029": manifest_for(
            "GOLD-029",
            None,
            facts_complete=False,
            reason_codes=[COMMIT_NOT_FOUND],
        ),
    }

    payload = build(backlog_fs, builder_from(manifests))

    by_id = items_by_id(payload)

    expectations = (("GOLD-028", COMMIT_AMBIGUOUS), ("GOLD-029", COMMIT_NOT_FOUND))

    for task_id, code in expectations:
        item = by_id[task_id]

        assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
        assert item["facts_complete"] is False
        assert code in item["missing_reason_codes"]
        assert backlog.ISSUE_BACKLOG_ITEM_FACTS_INCOMPLETE in item["reason_codes"]
        # 绝不猜测 commit identity
        assert item["commit"]["sha"] is None
        assert item["commit"]["resolved"] is False
        assert item["commit"]["subject"] is None

    assert set(payload["missing_reason_codes"]) == {COMMIT_AMBIGUOUS, COMMIT_NOT_FOUND}
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED


# ============================================================
# 3. fail-closed：verification chain 缺口 / 重复项 / 不可用 / 指针缺失
# ============================================================


def test_ledger_chain_gap_is_detected(backlog_fs: BacklogFS) -> None:
    tasks = [f"GOLD-{number:03d}" for number in range(27, 34)]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-031",
        last_completed_task="GOLD-033",
        current_task="GOLD-034",
    )

    write_ledger(
        backlog_fs.ledger,
        [
            ledger_entry("GOLD-027", shas["GOLD-027"], reviewed_at=REVIEWED_AT),
            ledger_entry("GOLD-029", shas["GOLD-029"], reviewed_at=REVIEWED_AT_LATER),
            ledger_entry("GOLD-030", shas["GOLD-030"], reviewed_at=REVIEWED_AT_LATER),
            ledger_entry("GOLD-031", shas["GOLD-031"], reviewed_at=REVIEWED_AT_LATER),
        ],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert payload["chain"]["missing_in_window"] == ["GOLD-028"]
    assert integrity.ISSUE_LEDGER_CHAIN_GAP in payload["reason_codes"]
    assert payload["summary"]["chain_gap_count"] == 1
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED

    # 缺口只报告，绝不自动补条目 / 自动推进指针
    assert [item["task_id"] for item in payload["backlog"]] == ["GOLD-032", "GOLD-033"]

    assert all(
        item["review_status"] == backlog.REVIEW_STATUS_PENDING for item in payload["backlog"]
    )


def test_duplicate_ledger_entries_are_fail_closed(backlog_fs: BacklogFS) -> None:
    tasks = ["GOLD-027", "GOLD-028"]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-028",
        current_task="GOLD-029",
    )

    write_ledger(
        backlog_fs.ledger,
        [
            ledger_entry("GOLD-027", shas["GOLD-027"], reviewed_at=REVIEWED_AT),
            ledger_entry("GOLD-028", shas["GOLD-028"], reviewed_at=REVIEWED_AT),
            ledger_entry("GOLD-028", shas["GOLD-028"], reviewed_at=REVIEWED_AT_LATER),
        ],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert payload["ledger"]["duplicate_tasks"] == ["GOLD-028"]
    assert integrity.ISSUE_LEDGER_DUPLICATE_TASK in payload["reason_codes"]

    item = items_by_id(payload)["GOLD-028"]

    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert item["ledger"]["entry_present"] is True
    assert item["ledger"]["entry_valid"] is False
    assert item["ledger"]["entry_count"] == 2
    assert backlog.ISSUE_BACKLOG_ITEM_LEDGER_INVALID in item["reason_codes"]
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED


def test_missing_ledger_is_unavailable(backlog_fs: BacklogFS) -> None:
    seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-028",
        current_task="GOLD-029",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert payload["ledger"]["available"] is False
    assert integrity.ISSUE_LEDGER_MISSING in payload["reason_codes"]
    assert payload["summary"]["exit_code"] == backlog.EXIT_UNAVAILABLE


def test_missing_project_state_is_unavailable(backlog_fs: BacklogFS) -> None:
    shas = seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert backlog.ISSUE_PROJECT_STATE_UNREADABLE in payload["reason_codes"]
    assert payload["coverage"]["last_reviewed_task_pointer_present"] is False
    # 边界不明确时绝不猜测范围
    assert payload["backlog"] == []
    assert payload["summary"]["exit_code"] == backlog.EXIT_UNAVAILABLE


def test_missing_last_reviewed_pointer_fails_closed(backlog_fs: BacklogFS) -> None:
    shas = seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_state(backlog_fs.state, current_task="GOLD-028")

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    assert backlog.ISSUE_LAST_REVIEWED_POINTER_MISSING in payload["reason_codes"]
    assert ledger_mod.ISSUE_POINTER_MISSING in payload["reason_codes"]
    assert payload["backlog"] == []
    assert payload["summary"]["exit_code"] == backlog.EXIT_FAIL_CLOSED


# ============================================================
# 4. 只产事实：绝无 verdict 字段 / 绝无写入权；Executor 不能自签 review
# ============================================================


def test_manifest_is_facts_only_and_never_emits_review_outcome(backlog_fs: BacklogFS) -> None:
    tasks = ["GOLD-027", "GOLD-028"]

    shas = seed(backlog_fs.results, backlog_fs.tasks, tasks)

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-028",
        current_task="GOLD-029",
    )

    write_ledger(
        backlog_fs.ledger,
        [
            ledger_entry("GOLD-027", shas["GOLD-027"]),
            ledger_entry("GOLD-028", shas["GOLD-028"]),
        ],
        reviewed_from="GOLD-027",
    )

    payload = build(backlog_fs, file_manifest_builder(backlog_fs.results))

    rendered = backlog.render_review_backlog_manifest(payload)

    assert rendered.isascii()

    keys = collect_keys(payload)

    for forbidden in backlog.FORBIDDEN_MANIFEST_KEYS:
        assert forbidden not in keys, forbidden

    # ledger 里确实有 verdict 结论，但本工具绝不生产 / 回显任何 verdict **值**
    # （复用门禁的人类可读 detail 文案里可能出现 "PASS review" 字样，那是漂移说明，
    #  不是本工具签发的结论；这里锁死的是「没有任何字段的值是 verdict」）。
    values = collect_string_values(payload)

    assert not any(value.strip().upper() in ledger_mod.REVIEW_VERDICTS for value in values)
    assert "verdict" not in values

    authority = payload["authority"]

    assert authority["review_authority"] == "gpt_only"
    assert set(authority["review_status_values"]) == set(backlog.REVIEW_STATUSES)

    for flag in (
        "emits_review_outcome",
        "emits_review_verdict_values",
        "creates_verdict",
        "tool_can_sign_review",
        "tool_can_write_review_ledger",
        "tool_can_advance_review_pointer",
        "tool_can_advance_state",
        "tool_can_generate_tasks",
        "tool_can_refill_queue",
        "tool_can_qualify_data",
        "tool_can_cross_human_gate",
        "writes_review_ledger",
        "writes_project_state",
        "writes_tasks",
        "writes_results",
    ):
        assert authority[flag] is False, flag

    for item in payload["backlog"]:
        assert item["review_status"] in backlog.REVIEW_STATUSES


def test_executor_cannot_self_sign_review_through_this_tool() -> None:
    assert ledger_mod.can_sign_review("cline") is False
    assert ledger_mod.can_sign_review("deepseek") is False
    assert ledger_mod.review_authority("deepseek")[0] is False
    assert planner.executor_allowed("review_task_result") is False
    assert planner.executor_allowed("modify_project_state") is False

    assert FORBIDDEN_PLANNING_API.isdisjoint(set(dir(backlog)))

    authority = backlog.authority_section()

    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_write_review_ledger"] is False
    assert authority["tool_can_advance_review_pointer"] is False
    assert authority["executor_can_review_task_result"] is False
    assert authority["planner_agents"] == ["gpt"]
    assert authority["executor_agents"] == ["cline", "deepseek"]


def test_module_source_has_no_write_or_subprocess_paths() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden

    # 唯一写操作必须经由受控输出守卫（runtime / 临时目录）
    assert MODULE_SOURCE.count("snapshot_output.write_snapshot_output(") == 1

    # last_reviewed_task 只被**读取**（pointer_value / 原样回显），绝不赋值 / 改写
    assert 'pointer_value(state, "last_reviewed_task")' in MODULE_SOURCE
    assert 'last_reviewed_task =' not in MODULE_SOURCE
    assert '["last_reviewed_task"] =' not in MODULE_SOURCE
    assert "state[" not in MODULE_SOURCE
    assert "state.update(" not in MODULE_SOURCE

    assert MODULE_SOURCE.count("review_binding") >= 1
    assert "build_review_binding_manifest" in MODULE_SOURCE
    assert "completed_result_ids" in MODULE_SOURCE
    assert "chain_section" in MODULE_SOURCE
    assert "pointer_section" in MODULE_SOURCE


def test_forbidden_review_keys_only_appear_in_negative_declaration() -> None:
    for key in backlog.FORBIDDEN_MANIFEST_KEYS:
        assert MODULE_SOURCE.count(f'"{key}"') == 1, key


# ============================================================
# 5. CLI：默认只读、fail-closed、--output 只允许受控路径
# ============================================================


def test_cli_fail_closed_for_non_git_root(
    backlog_fs: BacklogFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shas = seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_state(
        backlog_fs.state,
        last_reviewed_task="GOLD-027",
        last_completed_task="GOLD-028",
        current_task="GOLD-029",
    )

    write_ledger(
        backlog_fs.ledger,
        [ledger_entry("GOLD-027", shas["GOLD-027"])],
        reviewed_from="GOLD-027",
    )

    code = backlog.main(["--root", str(backlog_fs.root), "--generated-at", AUDIT_TIME])

    captured = capsys.readouterr()

    # 非 Git 仓库 ⇒ 客观 commit identity 不可得 ⇒ 一律 fail-closed，绝不猜测
    assert code == backlog.EXIT_FAIL_CLOSED

    payload = json.loads(captured.out)

    assert payload["schema"] == backlog.REVIEW_BACKLOG_SCHEMA
    assert payload["read_only"] is True

    item = items_by_id(payload)["GOLD-028"]

    assert item["facts_complete"] is False
    assert item["review_status"] == backlog.REVIEW_STATUS_INVALID
    assert item["commit"]["sha"] is None
    assert backlog.ISSUE_BACKLOG_ITEM_FACTS_INCOMPLETE in item["reason_codes"]
    assert "[backlog]" in captured.err


def test_cli_rejects_output_outside_controlled_paths(
    backlog_fs: BacklogFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_state(backlog_fs.state, last_reviewed_task="GOLD-027")

    write_ledger(backlog_fs.ledger, [], reviewed_from="GOLD-027")

    state_bytes = backlog_fs.state.read_bytes()

    code = backlog.main(
        ["--root", str(backlog_fs.root), "--output", str(backlog_fs.state)]
    )

    captured = capsys.readouterr()

    assert code == snapshot_output.EXIT_OUTPUT_REJECTED
    assert snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err
    assert backlog_fs.state.read_bytes() == state_bytes
    assert captured.out == ""


def test_cli_writes_only_into_runtime_path(
    backlog_fs: BacklogFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(backlog_fs.results, backlog_fs.tasks, ["GOLD-027", "GOLD-028"])

    write_state(backlog_fs.state, last_reviewed_task="GOLD-027")

    write_ledger(backlog_fs.ledger, [], reviewed_from="GOLD-027")

    runtime = backlog_fs.root / ".ai" / "runtime"

    runtime.mkdir(parents=True)

    target = runtime / "review_backlog.json"

    code = backlog.main(
        [
            "--root",
            str(backlog_fs.root),
            "--generated-at",
            AUDIT_TIME,
            "--output",
            str(target),
        ]
    )

    captured = capsys.readouterr()

    assert captured.out == ""
    assert target.is_file()

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["generated_at"] == AUDIT_TIME
    assert payload["schema"] == backlog.REVIEW_BACKLOG_SCHEMA
    assert code == payload["summary"]["exit_code"]
    assert "[info]" in captured.err


def test_cli_help_has_no_write_or_planning_flags() -> None:
    options = set(backlog.build_parser()._option_string_actions)

    for forbidden in ("--write", "--no-dry-run", "--approve", "--commit", "--push"):
        assert forbidden not in options

    assert "--output" in options
