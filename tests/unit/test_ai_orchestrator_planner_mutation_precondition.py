"""``orchestrator.planner_mutation_precondition`` 只读并发保护契约回归（GOLD-038）。

覆盖：
1. **事实包完整性**：单个确定性事实包同时给出 branch / observed_head_sha / queue_head /
   task_queue / PROJECT_STATE blob + digest / tasks digest / results digest / refill facts
   digest / formal review backlog 指针 / ``precondition_digest``，且**复用**既有
   planner snapshot / refill / review backlog 事实（本测试用 ``hashlib`` 独立复算 digest）；
2. **fail-closed**：``expected_head_sha`` 与 observed HEAD 不一致 ⇒ ``STALE_REMOTE_HEAD``；
   非法 expected / HEAD 不可解析 / ``PROJECT_STATE.branch`` 漂移 /
   ``current_task`` / ``last_completed_task`` 与 results 漂移 ⇒ 稳定 code + 禁止 planner
   mutation（``allowed=false`` / ``forbidden=true`` / ``requires_reread=true``）；
3. **并发回归**：Executor 在 planner snapshot 之后完成并 push（HEAD 前进 + 新 result）时，
   旧 precondition 必须失效；重新读取最新 HEAD + 刷新 state 后可重新生成**有效**事实包；
4. **只产事实、不产权限**：``authority`` 全 False、无 verdict 字段、无写入 / 子进程 / Git 写
   路径（源码守卫），且 CLI 默认零写入、``--output`` 只写受控路径。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import planner_mutation_precondition as precondition
from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import review_backlog as backlog
from orchestrator import review_ledger as ledger_mod

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-23T06:00:00+08:00"

BRANCH = "cline-agent"

OTHER_BRANCH = "feature-other"

# 确定性 40 位十六进制 SHA（只用于测试，绝不代表真实 Git 身份）。
HEAD_A = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"

HEAD_B = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f809a1b2c3d4"

BACKLOG_DIGEST = "b1" * 32

BACKLOG_DIGEST_OTHER = "c2" * 32

MODULE_SOURCE = Path(precondition.__file__).read_text(encoding="utf-8")

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
    "check_output",
    "Popen",
    "os.system",
    "json.dump(",
    "hashlib.md5",
)

# Executor 侧绝不存在的 planner / state / ledger 写入 API。
FORBIDDEN_PLANNING_API = {
    "write_review_ledger",
    "append_review_entry",
    "sign_review",
    "record_verdict",
    "advance_review_pointer",
    "update_project_state",
    "generate_follow_on_task",
    "refill_rolling_queue",
    "write_task",
    "write_result",
    "merge_remote",
    "rebase_remote",
    "force_push",
}


# ============================================================
# 确定性 fake 事实（零子进程 / 零 Git）
# ============================================================


def canonical_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def tree_digest(directory: Path) -> str:
    """测试**自己**复算目录内容 digest（独立于被测模块）。"""

    files = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }

    return canonical_digest(files)


class FakeHead:
    """可变的 HEAD 观测器：模拟「Executor completion push 让远端 HEAD 前进」。"""

    def __init__(self, sha: str = HEAD_A, branch: str = BRANCH) -> None:
        self.sha = sha
        self.branch = branch
        self.available = True

    def push(self, sha: str) -> None:
        """模拟 Executor 完成并发 push 之后的远端 HEAD。"""

        self.sha = sha

    def __call__(self, root: Path) -> dict[str, Any]:
        return {
            "available": self.available,
            "branch": self.branch,
            "detached": False,
            "head": self.sha,
            "head_short": self.sha[:8],
        }


def write_result(results: Path, task_id: str, status: str = "completed") -> str:
    payload = {
        "task_id": task_id,
        "status": status,
        "finished_at": "2026-09-23T16:57:20+08:00",
        "attempts": [{"attempt": 1, "validations": []}],
    }

    path = results / f"{task_id}.json"

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_task(tasks: Path, task_id: str) -> None:
    payload = {"task_id": task_id, "title": task_id, "human_gate": "L1"}

    (tasks / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def write_state(
    state_path: Path,
    *,
    current_task: str | None = None,
    last_completed_task: str | None = None,
    last_reviewed_task: str | None = None,
    task_queue: list[str] | None = None,
    branch: str | None = BRANCH,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "project": "XAUUSD",
        "phase": "Phase 3",
        "status": "ACTIVE",
        "queue_status": "ACTIVE",
        "invariants": ["LIVE_TRADING=false", "ALLOW_EXTERNAL_ORDER_SUBMISSION=false"],
    }

    values = {
        "branch": branch,
        "current_task": current_task,
        "last_completed_task": last_completed_task,
        "last_reviewed_task": last_reviewed_task,
        "task_queue": task_queue,
    }

    for key, value in values.items():
        if value is not None:
            payload[key] = value

    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_ledger(ledger_path: Path, entries: list[dict[str, Any]] | None = None) -> None:
    payload = {
        "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "schema_version": ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
        "entries": entries or [],
    }

    ledger_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def backlog_manifest(
    *,
    last_reviewed: str | None = "GOLD-001",
    newest_completed: str | None = "GOLD-002",
    backlog_count: int = 1,
    backlog_digest: str = BACKLOG_DIGEST,
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    """与 §2.11 manifest **同 shape** 的事实子集（零 Git / 零子进程）。"""

    return {
        "schema": backlog.REVIEW_BACKLOG_SCHEMA,
        "schema_version": backlog.REVIEW_BACKLOG_SCHEMA_VERSION,
        "backlog_digest": backlog_digest,
        "coverage": {
            "backlog_count": backlog_count,
            "backlog_first": newest_completed,
            "backlog_last": newest_completed,
            "last_reviewed_task_pointer": last_reviewed,
            "last_reviewed_task_pointer_present": last_reviewed is not None,
            "newest_completed_result": newest_completed,
        },
        "pointer": {
            "last_reviewed_task": last_reviewed,
            "last_reviewed_task_has_valid_ledger_entry": True,
            "newest_completed_result": newest_completed,
        },
        "summary": {
            "backlog_count": backlog_count,
            "bound_count": 0,
            "exit_code": 0,
            "facts_incomplete_count": 0,
            "invalid_count": 0,
            "issue_count": 0,
            "pending_count": backlog_count,
        },
        "reason_codes": list(reason_codes or []),
    }


def backlog_builder_from(manifest: dict[str, Any]) -> precondition.BacklogBuilder:
    def build() -> dict[str, Any]:
        return manifest

    return build


def raising_backlog_builder() -> precondition.BacklogBuilder:
    def build() -> dict[str, Any]:
        raise RuntimeError("backlog source exploded")

    return build


@dataclass(frozen=True)
class PreconditionFS:
    root: Path
    tasks: Path
    results: Path
    state: Path
    ledger: Path


@pytest.fixture()
def pc_fs(tmp_path: Path) -> PreconditionFS:
    """把 tasks / results / PROJECT_STATE / ledger 全部指向 tmp 目录（零真实仓库影响）。"""

    root = tmp_path / "repo"

    tasks = root / ".ai" / "tasks"
    results = root / ".ai" / "results"

    tasks.mkdir(parents=True)
    results.mkdir(parents=True)

    return PreconditionFS(
        root=root,
        tasks=tasks,
        results=results,
        state=root / ".ai" / "PROJECT_STATE.json",
        ledger=root / ".ai" / "GPT_REVIEW_LEDGER.json",
    )


def build(
    fs: PreconditionFS,
    *,
    expected_head_sha: object = None,
    head: FakeHead | None = None,
    snapshot: dict[str, Any] | None = None,
    backlog_manifest_payload: dict[str, Any] | None = None,
    backlog_builder: precondition.BacklogBuilder | None = None,
    generated_at: str = AUDIT_TIME,
) -> dict[str, Any]:
    builder = backlog_builder

    if builder is None:
        builder = backlog_builder_from(
            backlog_manifest_payload if backlog_manifest_payload is not None else backlog_manifest()
        )

    return precondition.build_planner_mutation_precondition(
        root=fs.root,
        state_path=fs.state,
        tasks_dir=fs.tasks,
        results_dir=fs.results,
        ledger_path=fs.ledger,
        generated_at=generated_at,
        expected_head_sha=expected_head_sha,
        snapshot=snapshot,
        head_provider=head if head is not None else FakeHead(),
        backlog_builder=builder,
    )


def seed_consistent_fs(fs: PreconditionFS) -> FakeHead:
    """GOLD-001 completed + GOLD-002 待执行，``PROJECT_STATE`` 与实际 results 完全一致。"""

    write_result(fs.results, "GOLD-001")

    write_task(fs.tasks, "GOLD-002")

    write_state(
        fs.state,
        current_task="GOLD-002",
        last_completed_task="GOLD-001",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-002"],
    )

    write_ledger(fs.ledger)

    return FakeHead()


def collect_keys(payload: object) -> set[str]:
    found: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))

            found |= collect_keys(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_keys(item)

    return found


def collect_string_values(payload: object) -> set[str]:
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
# 1. 单个确定性事实包：HEAD / 队列 / state / 目录 digest / refill / backlog 指针
# ============================================================


def test_precondition_binds_head_queue_state_and_digests(pc_fs: PreconditionFS) -> None:
    head = seed_consistent_fs(pc_fs)

    snapshot = planner.build_planner_snapshot(
        root=pc_fs.root,
        state_path=pc_fs.state,
        tasks_dir=pc_fs.tasks,
        results_dir=pc_fs.results,
        generated_at=AUDIT_TIME,
    )

    expected_refill = refill.build_planner_refill_request(
        snapshot=snapshot,
        generated_at=AUDIT_TIME,
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A, head=head, snapshot=snapshot)

    assert payload["schema"] == precondition.PRECONDITION_SCHEMA
    assert payload["schema_version"] == precondition.PRECONDITION_SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["generated_at"] == AUDIT_TIME

    # HEAD 事实 + expected 比对（复用 planner snapshot git 段 / 注入 provider）
    remote_head = payload["remote_head"]
    assert remote_head["branch"] == BRANCH
    assert remote_head["observed_head_sha"] == HEAD_A
    assert remote_head["expected_head_sha"] == HEAD_A
    assert remote_head["matches_expected"] is True
    assert remote_head["stale"] is False
    assert remote_head["resolved"] is True

    # 队列 head / task_queue（复用 planner snapshot queue 事实）
    assert payload["queue_head"] == "GOLD-002"
    assert payload["task_queue"]["declared"] == ["GOLD-002"]
    assert payload["task_queue"]["pending"] == ["GOLD-002"]
    assert payload["task_queue"]["pending_count"] == 1
    assert payload["task_queue"]["declaration_mismatch"] is False
    assert payload["task_queue"]["head"] == "GOLD-002"
    assert payload["task_queue"]["queue_status"] == "ACTIVE"

    # PROJECT_STATE blob / digest（测试独立复算）
    state_bytes = pc_fs.state.read_bytes()

    assert payload["state"]["bytes"] == len(state_bytes)
    assert payload["state"]["blob_sha256"] == hashlib.sha256(state_bytes).hexdigest()
    assert payload["state"]["digest"] == canonical_digest(json.loads(state_bytes.decode("utf-8")))
    assert payload["state"]["readable"] is True
    assert payload["state"]["current_task"] == "GOLD-002"
    assert payload["state"]["last_completed_task"] == "GOLD-001"

    # tasks / results 目录 digest（测试独立复算）
    assert payload["tasks_digest"]["digest"] == tree_digest(pc_fs.tasks)
    assert payload["results_digest"]["digest"] == tree_digest(pc_fs.results)
    assert set(payload["tasks_digest"]["files"]) == {"GOLD-002.json"}
    assert set(payload["results_digest"]["files"]) == {"GOLD-001.json"}

    # refill facts digest（复用 §2.10 单一来源，未自建第二套 queue 判断）
    assert payload["refill"]["facts_digest"] == expected_refill["facts_digest"]
    assert payload["refill"]["queue_head"] == "GOLD-002"
    assert payload["refill"]["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA

    # formal review backlog 指针（复用 §2.11，不签发 review 结论）
    review_backlog = payload["review_backlog"]
    assert review_backlog["available"] is True
    assert review_backlog["backlog_digest"] == BACKLOG_DIGEST
    assert review_backlog["pointer"]["last_reviewed_task"] == "GOLD-001"
    assert review_backlog["pointer"]["newest_completed_result"] == "GOLD-002"
    assert review_backlog["tool_can_sign_review"] is False
    assert review_backlog["tool_can_write_review_ledger"] is False

    # 稳定 precondition_digest + 允许写队列
    assert len(payload["precondition_digest"]) == 64
    assert payload["planner_mutation"]["allowed"] is True
    assert payload["planner_mutation"]["forbidden"] is False
    assert payload["planner_mutation"]["requires_reread"] is False
    assert payload["planner_mutation"]["blocking_reason_codes"] == []
    assert payload["planner_mutation"]["tool_can_mutate"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_OK
    assert payload["summary"]["planner_mutation_allowed"] is True
    assert payload["summary"]["stale_remote_head"] is False


def test_precondition_digest_is_idempotent_and_excludes_wall_clock(
    pc_fs: PreconditionFS,
) -> None:
    seed_consistent_fs(pc_fs)

    first = build(pc_fs, expected_head_sha=HEAD_A, generated_at=AUDIT_TIME)

    second = build(pc_fs, expected_head_sha=HEAD_A, generated_at=AUDIT_TIME_LATER)

    assert first["precondition_digest"] == second["precondition_digest"]
    assert first["generated_at"] != second["generated_at"]

    # wall-clock 只出现在 generated_at，绝不进入确定性事实
    assert precondition.precondition_facts(first) == precondition.precondition_facts(second)
    assert "generated_at" not in precondition.precondition_facts(first)
    assert "precondition_digest" not in precondition.precondition_facts(first)
    assert "determinism" not in precondition.precondition_facts(first)

    assert precondition.FACTS_EXCLUDED_KEYS == (
        "generated_at",
        "precondition_digest",
        "determinism",
    )
    assert first["determinism"]["wall_clock_in_facts"] is False
    assert first["determinism"]["digest_field"] == "precondition_digest"


def test_precondition_digest_is_content_sensitive(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    baseline = build(pc_fs, expected_head_sha=HEAD_A)

    # ① 新增一个 result（等价于 Executor 写入新结果）⇒ results digest 与 precondition 都变
    write_result(pc_fs.results, "GOLD-000", status="blocked")

    after_new_result = build(pc_fs, expected_head_sha=HEAD_A)

    assert after_new_result["results_digest"]["digest"] != baseline["results_digest"]["digest"]
    assert after_new_result["precondition_digest"] != baseline["precondition_digest"]

    # ② 新增 task 文件（HEAD 不变）⇒ tasks digest 与 precondition 都变
    tasks_before = after_new_result["tasks_digest"]["digest"]

    write_task(pc_fs.tasks, "GOLD-004")

    after_new_task = build(pc_fs, expected_head_sha=HEAD_A)

    assert after_new_task["tasks_digest"]["digest"] != tasks_before
    assert after_new_task["precondition_digest"] != after_new_result["precondition_digest"]
    assert after_new_task["remote_head"]["observed_head_sha"] == HEAD_A

    # ③ backlog 指针事实变化同样进入 digest（复用 §2.11 的 digest 值）
    other_backlog = build(
        pc_fs,
        expected_head_sha=HEAD_A,
        backlog_manifest_payload=backlog_manifest(
            last_reviewed="GOLD-002",
            newest_completed="GOLD-003",
            backlog_digest=BACKLOG_DIGEST_OTHER,
        ),
    )

    assert other_backlog["review_backlog"]["backlog_digest"] == BACKLOG_DIGEST_OTHER
    assert other_backlog["review_backlog"]["pointer"]["last_reviewed_task"] == "GOLD-002"
    assert other_backlog["precondition_digest"] != after_new_task["precondition_digest"]


# ============================================================
# 2. HEAD / state 漂移一律 fail-closed
# ============================================================


def test_expected_head_mismatch_is_stale_remote_head(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    matched = build(pc_fs, expected_head_sha=HEAD_A)

    stale = build(pc_fs, expected_head_sha=HEAD_B)

    assert precondition.REASON_STALE_REMOTE_HEAD in stale["reason_codes"]
    assert (
        precondition.REASON_STALE_REMOTE_HEAD in stale["planner_mutation"]["blocking_reason_codes"]
    )
    assert stale["remote_head"]["stale"] is True
    assert stale["remote_head"]["matches_expected"] is False
    assert stale["remote_head"]["observed_head_sha"] == HEAD_A
    assert stale["drift"]["stale_remote_head"] is True
    assert stale["planner_mutation"]["allowed"] is False
    assert stale["planner_mutation"]["forbidden"] is True
    assert stale["planner_mutation"]["requires_reread"] is True
    assert stale["planner_mutation"]["stale_remote_head"] is True
    assert stale["planner_mutation"]["auto_merge"] is False
    assert stale["planner_mutation"]["auto_rebase"] is False
    assert stale["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED

    # 旧事实包（HEAD 一致）与新事实包（HEAD 变化）必然不同 ⇒ 旧 precondition 失效
    assert stale["precondition_digest"] != matched["precondition_digest"]


def test_expected_head_accepts_short_sha_prefix(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    payload = build(pc_fs, expected_head_sha=HEAD_A[:12].upper())

    assert payload["remote_head"]["expected_head_sha"] == HEAD_A[:12]
    assert payload["remote_head"]["matches_expected"] is True
    assert payload["remote_head"]["stale"] is False
    assert payload["planner_mutation"]["allowed"] is True


def test_invalid_expected_head_fails_closed(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    payload = build(pc_fs, expected_head_sha="not-a-sha")

    assert precondition.REASON_EXPECTED_HEAD_SHA_INVALID in payload["reason_codes"]
    assert payload["remote_head"]["expected_head_error"] is not None
    assert payload["remote_head"]["matches_expected"] is None
    assert payload["remote_head"]["stale"] is None
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["planner_mutation"]["forbidden"] is True
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_unresolved_head_fails_closed(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    head = FakeHead()

    head.available = False

    payload = build(pc_fs, expected_head_sha=HEAD_A, head=head)

    assert planner.ISSUE_GIT_INFO_UNAVAILABLE in payload["reason_codes"]
    assert payload["remote_head"]["observed_head_sha"] is None
    assert payload["remote_head"]["resolved"] is False
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["planner_mutation"]["requires_reread"] is True
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_current_task_already_terminal_is_state_result_drift(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    # state 仍声称 GOLD-001 在执行，但 results 已把它标成 completed（真实并发场景）
    write_state(
        pc_fs.state,
        current_task="GOLD-001",
        last_completed_task="GOLD-001",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-001"],
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert planner.ISSUE_POINTER_BEHIND_RESULTS in payload["reason_codes"]
    assert precondition.REASON_STATE_RESULT_DRIFT in payload["reason_codes"]
    assert planner.ISSUE_POINTER_BEHIND_RESULTS in payload["drift"]["state_fact_codes"]
    assert payload["summary"]["state_result_drift"] is True
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["planner_mutation"]["forbidden"] is True
    assert payload["planner_mutation"]["requires_reread"] is True
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_last_completed_pointer_behind_results_is_state_result_drift(
    pc_fs: PreconditionFS,
) -> None:
    seed_consistent_fs(pc_fs)

    write_result(pc_fs.results, "GOLD-002")

    write_task(pc_fs.tasks, "GOLD-003")

    # state 仍在 GOLD-001 / GOLD-002，而 results 已到 GOLD-002（指针落后）
    write_state(
        pc_fs.state,
        current_task="GOLD-003",
        last_completed_task="GOLD-001",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-003"],
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert planner.ISSUE_POINTER_BEHIND_RESULTS in payload["reason_codes"]
    assert (
        precondition.REASON_STATE_RESULT_DRIFT
        in payload["planner_mutation"]["blocking_reason_codes"]
    )
    assert payload["drift"]["latest_terminal_result"] == "GOLD-002"
    assert payload["drift"]["last_completed_task_pointer"] == "GOLD-001"
    assert payload["planner_mutation"]["allowed"] is False


def test_last_completed_pointer_ahead_of_results_is_state_result_drift(
    pc_fs: PreconditionFS,
) -> None:
    seed_consistent_fs(pc_fs)

    write_task(pc_fs.tasks, "GOLD-003")

    write_state(
        pc_fs.state,
        current_task="GOLD-003",
        last_completed_task="GOLD-003",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-003"],
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert planner.ISSUE_POINTER_AHEAD_OF_RESULTS in payload["reason_codes"]
    assert precondition.REASON_STATE_RESULT_DRIFT in payload["reason_codes"]
    assert payload["planner_mutation"]["allowed"] is False


def test_state_branch_drift_fails_closed(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    write_state(
        pc_fs.state,
        current_task="GOLD-002",
        last_completed_task="GOLD-001",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-002"],
        branch=OTHER_BRANCH,
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert precondition.REASON_STATE_BRANCH_DRIFT in payload["reason_codes"]
    assert payload["drift"]["branch_fact_codes"] == [precondition.REASON_STATE_BRANCH_DRIFT]
    assert payload["drift"]["state_branch"] == OTHER_BRANCH
    assert payload["drift"]["head_branch"] == BRANCH
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED


def test_missing_project_state_is_unavailable(pc_fs: PreconditionFS) -> None:
    write_result(pc_fs.results, "GOLD-001")

    write_task(pc_fs.tasks, "GOLD-002")

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert planner.ISSUE_PROJECT_STATE_UNREADABLE in payload["reason_codes"]
    assert payload["state"]["readable"] is False
    assert payload["state"]["blob_sha256"] is None
    assert payload["state"]["digest"] is None
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_STATE_UNREADABLE


def test_backlog_facts_unavailable_fails_closed(pc_fs: PreconditionFS) -> None:
    seed_consistent_fs(pc_fs)

    payload = build(
        pc_fs,
        expected_head_sha=HEAD_A,
        backlog_builder=raising_backlog_builder(),
    )

    assert precondition.REASON_REVIEW_BACKLOG_FACTS_UNAVAILABLE in payload["reason_codes"]
    assert payload["review_backlog"]["available"] is False
    assert payload["review_backlog"]["backlog_digest"] is None
    assert payload["planner_mutation"]["allowed"] is False
    assert payload["summary"]["exit_code"] == precondition.EXIT_FAIL_CLOSED

    # 其它事实仍然完整（绝不因为一个子来源异常就丢掉全部事实）
    assert payload["remote_head"]["observed_head_sha"] == HEAD_A
    assert payload["queue_head"] == "GOLD-002"
    assert len(payload["results_digest"]["digest"]) == 64


def test_review_pointer_lag_alone_does_not_block_planner_mutation(
    pc_fs: PreconditionFS,
) -> None:
    """``last_reviewed_task`` 落后属于 review backlog 事实（§2.11），只报告不阻塞写队列。"""

    seed_consistent_fs(pc_fs)

    write_result(pc_fs.results, "GOLD-002")

    write_task(pc_fs.tasks, "GOLD-003")

    # 执行指针（current_task / last_completed_task）与 results 完全一致；
    # 仅 last_reviewed_task 落后（GOLD-001，而 GOLD-002 已 completed）。
    write_state(
        pc_fs.state,
        current_task="GOLD-003",
        last_completed_task="GOLD-002",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-003"],
    )

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    assert payload["drift"]["review_pointer_codes"] == [planner.ISSUE_POINTER_BEHIND_RESULTS]
    assert payload["drift"]["state_fact_codes"] == []
    assert payload["summary"]["state_result_drift"] is False
    assert payload["planner_mutation"]["allowed"] is True
    assert payload["summary"]["exit_code"] == precondition.EXIT_OK


# ============================================================
# 3. 并发回归：Executor completion push 让旧 precondition 失效
# ============================================================


def test_executor_completion_push_invalidates_old_precondition(pc_fs: PreconditionFS) -> None:
    write_result(pc_fs.results, "GOLD-001")
    write_result(pc_fs.results, "GOLD-002")

    write_task(pc_fs.tasks, "GOLD-003")

    write_state(
        pc_fs.state,
        current_task="GOLD-003",
        last_completed_task="GOLD-002",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-003"],
    )

    write_ledger(pc_fs.ledger)

    head = FakeHead(HEAD_A)

    # ① GPT 在旧 HEAD 上读到「可以安全写队列」的事实包
    before = build(pc_fs, expected_head_sha=HEAD_A, head=head)

    assert before["planner_mutation"]["allowed"] is True

    observed_before = before["remote_head"]["observed_head_sha"]

    assert observed_before == HEAD_A

    # ② Executor 完成 GOLD-003 并 push：远端 HEAD 前进 + 新 result 落盘
    write_result(pc_fs.results, "GOLD-003")

    head.push(HEAD_B)

    # ③ 用旧 expected HEAD 复用旧快照 ⇒ 必须 fail-closed
    stale = build(pc_fs, expected_head_sha=observed_before, head=head)

    assert precondition.REASON_STALE_REMOTE_HEAD in stale["reason_codes"]
    assert stale["remote_head"]["observed_head_sha"] == HEAD_B
    assert stale["remote_head"]["stale"] is True
    assert stale["planner_mutation"]["allowed"] is False
    assert stale["planner_mutation"]["forbidden"] is True
    assert stale["precondition_digest"] != before["precondition_digest"]

    # ④ 即使调用方忘记传 expected HEAD，state 声称也会被 results 事实推翻 ⇒ 仍禁止写入
    forgotten = build(pc_fs, head=head)

    assert forgotten["remote_head"]["stale"] is None
    assert precondition.REASON_STATE_RESULT_DRIFT in forgotten["reason_codes"]
    assert forgotten["planner_mutation"]["allowed"] is False

    # ⑤ 重新读取最新 HEAD + 基于最新事实刷新 planner state 后可重新生成**有效**事实包
    write_task(pc_fs.tasks, "GOLD-004")

    write_state(
        pc_fs.state,
        current_task="GOLD-004",
        last_completed_task="GOLD-003",
        last_reviewed_task="GOLD-001",
        task_queue=["GOLD-004"],
    )

    refreshed = build(pc_fs, expected_head_sha=HEAD_B, head=head)

    assert refreshed["remote_head"]["observed_head_sha"] == HEAD_B
    assert refreshed["remote_head"]["matches_expected"] is True
    assert refreshed["remote_head"]["stale"] is False
    assert refreshed["planner_mutation"]["allowed"] is True
    assert refreshed["planner_mutation"]["requires_reread"] is False
    assert refreshed["precondition_digest"] not in {
        before["precondition_digest"],
        stale["precondition_digest"],
        forgotten["precondition_digest"],
    }


# ============================================================
# 4. 只产事实、不产权限（authority / 源码守卫）
# ============================================================


def test_precondition_is_facts_only_and_never_emits_review_verdict(
    pc_fs: PreconditionFS,
) -> None:
    seed_consistent_fs(pc_fs)

    payload = build(pc_fs, expected_head_sha=HEAD_A)

    keys = collect_keys(payload)

    for forbidden in precondition.FORBIDDEN_PRECONDITION_KEYS:
        assert forbidden not in keys, forbidden

    values = collect_string_values(payload)

    assert not any(value.strip().upper() in ledger_mod.REVIEW_VERDICTS for value in values)

    authority = payload["authority"]

    for flag in (
        "tool_can_mutate",
        "tool_can_generate_tasks",
        "tool_can_refill_queue",
        "tool_can_sign_review",
        "tool_can_advance_state",
        "tool_can_write_tasks",
        "tool_can_write_results",
        "tool_can_write_project_state",
        "tool_can_write_review_ledger",
        "tool_can_transition_phase",
        "tool_can_cross_human_gate",
        "tool_can_qualify_data",
        "writes_tasks",
        "writes_results",
        "writes_project_state",
        "writes_review_ledger",
        "auto_merge",
        "auto_rebase",
        "auto_fetch_or_pull",
        "force_push",
        "executor_can_mutate",
        "executor_can_plan",
    ):
        assert authority[flag] is False, flag

    # 只挡 planner 写入；绝不阻塞 Executor 执行已批准任务
    assert authority["executor_execution_blocked"] is False
    assert authority["gates_planner_writes_only"] is True
    assert payload["planner_mutation"]["execution_blocked"] is False
    assert payload["planner_mutation"]["scope"].endswith("never blocks approved execution)")


def test_executor_cannot_use_this_tool_to_mutate_or_plan() -> None:
    assert ledger_mod.can_sign_review("cline") is False
    assert ledger_mod.can_sign_review("deepseek") is False
    assert planner.executor_allowed("modify_project_state") is False
    assert planner.executor_allowed("refill_rolling_queue") is False
    assert planner.executor_allowed("generate_follow_on_task") is False

    assert FORBIDDEN_PLANNING_API.isdisjoint(set(dir(precondition)))

    authority = precondition.authority_section()

    assert authority["planning_authority"] == "gpt_only"
    assert authority["planner_agents"] == ["gpt"]
    assert authority["executor_agents"] == ["cline", "deepseek"]
    assert authority["executor_can_modify_project_state"] is False
    assert authority["executor_can_refill_rolling_queue"] is False
    assert authority["executor_can_generate_follow_on_tasks"] is False
    assert authority["tool_can_mutate"] is False
    assert authority["mutation_gate_rule"] == (
        "planner_mutation.allowed == (planner_mutation.blocking_reason_codes == [])"
    )


def test_module_source_has_no_write_subprocess_or_git_write_paths() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden

    # 唯一写操作必须经由受控输出守卫（runtime / 临时目录）
    assert MODULE_SOURCE.count("snapshot_output.write_snapshot_output(") == 1

    # 事实来源必须是复用，不得自建第二套判定
    assert "build_planner_snapshot" in MODULE_SOURCE
    assert "build_planner_refill_request" in MODULE_SOURCE
    assert "build_review_backlog_manifest" in MODULE_SOURCE
    assert "render_planner_mutation_precondition" in MODULE_SOURCE

    # state 只被**读取**（pointer_value / 原样回显），绝不赋值 / 改写
    assert 'pointer_value(state, "current_task")' in MODULE_SOURCE
    assert 'pointer_value(state, "last_completed_task")' in MODULE_SOURCE
    assert 'last_completed_task"] =' not in MODULE_SOURCE
    assert 'last_reviewed_task"] =' not in MODULE_SOURCE
    assert "state.update(" not in MODULE_SOURCE

    # 绝不出现任何 Git 写命令
    for forbidden_git in ("git merge", "git rebase", "git push", "push --force"):
        assert forbidden_git not in MODULE_SOURCE, forbidden_git


def test_forbidden_keys_only_appear_in_negative_declaration() -> None:
    for key in precondition.FORBIDDEN_PRECONDITION_KEYS:
        assert MODULE_SOURCE.count(f'"{key}"') == 1, key


# ============================================================
# 5. CLI：默认只读、fail-closed、--output 只允许受控路径
# ============================================================


def test_cli_help_has_no_write_or_git_flags() -> None:
    options = set(precondition.build_parser()._option_string_actions)

    for forbidden in ("--write", "--commit", "--push", "--force", "--no-dry-run", "--approve"):
        assert forbidden not in options

    assert "--output" in options
    assert "--expected-head-sha" in options
    assert "--expect-head" in options


def test_cli_is_read_only_and_fail_closed_on_non_git_root(
    pc_fs: PreconditionFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_consistent_fs(pc_fs)

    state_bytes = pc_fs.state.read_bytes()

    code = precondition.main(
        ["--root", str(pc_fs.root), "--generated-at", AUDIT_TIME, "--expected-head-sha", HEAD_A]
    )

    captured = capsys.readouterr()

    # 非 Git 仓库 ⇒ HEAD 事实不可得 ⇒ 一律 fail-closed，绝不猜测版本
    assert code == precondition.EXIT_FAIL_CLOSED

    payload = json.loads(captured.out)

    assert payload["schema"] == precondition.PRECONDITION_SCHEMA
    assert payload["read_only"] is True
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["remote_head"]["observed_head_sha"] is None
    assert payload["remote_head"]["resolved"] is False
    assert planner.ISSUE_GIT_INFO_UNAVAILABLE in payload["reason_codes"]
    assert payload["planner_mutation"]["allowed"] is False
    assert code == payload["summary"]["exit_code"]
    assert captured.out.isascii()
    assert "[precondition]" in captured.err

    # 零写入
    assert pc_fs.state.read_bytes() == state_bytes
    assert not (pc_fs.root / ".ai" / "runtime").exists()


def test_cli_rejects_output_outside_controlled_paths(
    pc_fs: PreconditionFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_consistent_fs(pc_fs)

    state_bytes = pc_fs.state.read_bytes()

    for target in (
        pc_fs.state,
        pc_fs.tasks / "GOLD-999.json",
        pc_fs.results / "GOLD-999.json",
        pc_fs.ledger,
    ):
        existed_before = target.exists()

        code = precondition.main(["--root", str(pc_fs.root), "--output", str(target)])

        captured = capsys.readouterr()

        assert code == snapshot_output.EXIT_OUTPUT_REJECTED, target
        assert snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in captured.err
        assert captured.out == ""
        # fail-closed 拒绝时绝不创建 / 覆盖任何文件
        assert target.exists() is existed_before

    assert pc_fs.state.read_bytes() == state_bytes
    assert not (pc_fs.tasks / "GOLD-999.json").exists()
    assert not (pc_fs.results / "GOLD-999.json").exists()


def test_output_guard_rejects_any_planner_owned_ai_path(pc_fs: PreconditionFS) -> None:
    """即使仓库位于系统临时目录（共享守卫的允许根）也绝不放开 ``.ai/**`` 非 runtime 路径。"""

    seed_consistent_fs(pc_fs)

    runtime = pc_fs.root / ".ai" / "runtime"

    runtime.mkdir(parents=True)

    for rejected in (
        pc_fs.root / ".ai" / "GPT_REVIEW_LEDGER.json",
        pc_fs.root / ".ai" / "PROJECT_STATE.json",
        pc_fs.root / ".ai" / "NEW_STATE.json",
        pc_fs.root / ".ai" / "tasks" / "GOLD-900.json",
        pc_fs.root / ".ai" / "results" / "GOLD-900.json",
    ):
        existed_before = rejected.exists()

        target, reason = precondition.resolve_output_target(pc_fs.root, rejected)

        assert target is None, rejected
        assert reason is not None
        assert rejected.exists() is existed_before

    allowed, reason = precondition.resolve_output_target(
        pc_fs.root, runtime / "planner_mutation_precondition.json"
    )

    assert reason is None
    assert allowed == (runtime / "planner_mutation_precondition.json").resolve()


def test_cli_writes_only_into_runtime_path(
    pc_fs: PreconditionFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_consistent_fs(pc_fs)

    runtime = pc_fs.root / ".ai" / "runtime"

    runtime.mkdir(parents=True)

    target = runtime / "planner_mutation_precondition.json"

    code = precondition.main(
        [
            "--root",
            str(pc_fs.root),
            "--generated-at",
            AUDIT_TIME,
            "--expect-head",
            HEAD_B,
            "--output",
            str(target),
        ]
    )

    captured = capsys.readouterr()

    assert captured.out == ""
    assert target.is_file()

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["generated_at"] == AUDIT_TIME
    assert payload["schema"] == precondition.PRECONDITION_SCHEMA
    assert payload["remote_head"]["expected_head_sha"] == HEAD_B
    assert payload["review_backlog"]["available"] is True
    assert code == payload["summary"]["exit_code"]
    assert "[info]" in captured.err
    assert "[precondition]" in captured.err
