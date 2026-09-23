"""``orchestrator.review_binding`` 只读契约回归（GOLD-031）。

覆盖：
1. 确定性 / 只读：相同输入 ⇒ 字节相同 manifest，且对项目文件零改写；
2. 内容身份：result / task 的 SHA-256 可由独立 ``hashlib`` 复算，不使用 mtime / wall-clock；
3. 终态 commit 绑定：只从 HEAD 可达历史按 ``ai: complete/blocked <task_id>`` 解析，
   并把 task / result 内容与目标 commit 逐项绑定；
4. fail-closed：缺失 / 损坏 / 身份不一致 / 未知 status / Git 不可解析 / 工作树漂移
   全部给出稳定 reason code；
5. **GPT-only Review 边界**：manifest 只产事实，绝不产 verdict / acceptance_summary /
   reviewed_at，绝不写 review 台账 / 项目状态（源码级与运行期守卫）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import review_binding as binding

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-24T00:00:00+08:00"

BRANCH = "cline-agent"

FAKE_HEAD = "3f1a9c8e7b6d5f4a3b2c1d0e9f8a7b6c5d4e3f21"

COMMIT_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

OTHER_COMMIT_SHA = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345679"

BLOB_ID = hashlib.sha1(b"committed-blob").hexdigest()

BLOB_BYTES = b"committed-blob-content"

DRIFTED_BLOB_ID = hashlib.sha1(b"drifted-worktree-blob").hexdigest()

RESULT_FINISHED_AT = "2026-09-23T13:50:47+08:00"

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULE_SOURCE = Path(binding.__file__).read_text(encoding="utf-8")

# Executor 侧绝不存在的规划 / 状态写入 API。
FORBIDDEN_PLANNING_API = {
    "generate_follow_on_task",
    "refill_rolling_queue",
    "write_final_result",
    "write_task_state",
    "update_project_state",
    "modify_project_state",
    "commit_task_result",
    "reset_task_changes",
    "process_task",
}

# 只读模块源码里绝不出现的写入 / 状态路径痕迹。
FORBIDDEN_SOURCE_SUBSTRINGS = (
    "write_text",
    "write_bytes",
    "open(",
    "mkdir",
    "rmtree",
    "unlink",
    "rmdir",
    "shutil",
    "os.replace",
    "os.remove",
    "chmod",
    "tempfile",
    "GPT_REVIEW_LEDGER",
    "PROJECT_STATE",
    "last_reviewed_task",
)


def seed_fake_git(root: Path, branch: str = BRANCH, head: str = FAKE_HEAD) -> None:
    """构造只读 Git 事实（``.git/HEAD`` + loose ref），不依赖机器上的真实仓库。"""

    git_dir = root / ".git"

    refs = git_dir / "refs" / "heads"

    refs.mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    (refs / branch).write_text(f"{head}\n", encoding="utf-8")


@dataclass(frozen=True)
class BindingFS:
    root: Path
    tasks: Path
    results: Path


@pytest.fixture()
def binding_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BindingFS:
    """把 tasks / results / ROOT 全部重定向到 tmp_path（对真实仓库零影响）。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"

    tasks.mkdir()
    results.mkdir()

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    monkeypatch.setattr(binding, "ROOT", tmp_path)

    seed_fake_git(tmp_path)

    return BindingFS(root=tmp_path, tasks=tasks, results=results)


def write_task(tasks: Path, task_id: str, **extra: object) -> Path:
    path = tasks / f"{task_id}.json"

    path.write_text(
        json.dumps({"task_id": task_id, "title": task_id, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )

    return path


def write_raw_task(tasks: Path, file_stem: str, payload: object) -> Path:
    path = tasks / f"{file_stem}.json"

    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    path.write_text(text, encoding="utf-8")

    return path


def write_result(
    results: Path,
    task_id: str,
    status: str = "completed",
    finished_at: str = RESULT_FINISHED_AT,
    **extra: object,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "finished_at": finished_at,
        **extra,
    }

    (results / f"{task_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    return payload


def write_raw_result(results: Path, file_stem: str, payload: object) -> Path:
    path = results / f"{file_stem}.json"

    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    path.write_text(text, encoding="utf-8")

    return path


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(root).resolve()).as_posix()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_of_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    """独立复算整棵树的文件内容身份（用于证明零改写）。"""

    return {
        path.relative_to(root).as_posix(): sha256_of(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def collect_keys(payload: object) -> set[str]:
    """递归收集所有 dict key（用于断言 manifest 里没有任何 Review 结论字段）。"""

    found: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))

            found |= collect_keys(value)

    elif isinstance(payload, list):
        for item in payload:
            found |= collect_keys(item)

    return found



def commit_entry(
    task_id: str,
    sha: str = COMMIT_SHA,
    outcome: str = "complete",
    committed_at: str = "2026-09-23T13:51:00+08:00",
) -> dict[str, Any]:
    return {
        "sha": sha,
        "committed_at": committed_at,
        "subject": f"ai: {outcome} {task_id}",
    }


def fake_git(
    *,
    entries: list[dict[str, Any]] | None = None,
    log_error: str | None = None,
    commit_blobs: dict[str, str] | None = None,
    worktree_blobs: dict[str, str] | None = None,
    blob_bytes: dict[str, bytes] | None = None,
    commit_blob_error: str | None = None,
    blob_bytes_error: str | None = None,
    worktree_blob_error: str | None = None,
) -> dict[str, Any]:
    """构造可注入的 fake git 注入点（零子进程）。"""

    log_entries = list(entries) if entries is not None else []

    commit_map = dict(commit_blobs or {})

    worktree_map = dict(worktree_blobs or {})

    content_map = dict(blob_bytes or {})

    def commit_log_provider(root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
        if log_error is not None:
            return None, log_error

        return list(log_entries), None

    def blob_id_reader(root: Path, sha: str, relative: str) -> tuple[str | None, str | None]:
        if commit_blob_error is not None:
            return None, commit_blob_error

        if relative in commit_map:
            return commit_map[relative], None

        return None, binding.GIT_BLOB_ABSENT

    def blob_bytes_reader(root: Path, blob_id: str) -> tuple[bytes | None, str | None]:
        if blob_bytes_error is not None:
            return None, blob_bytes_error

        if blob_id in content_map:
            return content_map[blob_id], None

        return None, binding.GIT_BLOB_UNAVAILABLE

    def worktree_blob_id_reader(root: Path, relative: str) -> tuple[str | None, str | None]:
        if worktree_blob_error is not None:
            return None, worktree_blob_error

        if relative in worktree_map:
            return worktree_map[relative], None

        return None, binding.GIT_BLOB_UNAVAILABLE

    return {
        "commit_log_provider": commit_log_provider,
        "blob_id_reader": blob_id_reader,
        "blob_bytes_reader": blob_bytes_reader,
        "worktree_blob_id_reader": worktree_blob_id_reader,
    }


def consistent_fake(fs: BindingFS, task_id: str = "GOLD-028", **overrides: Any) -> dict[str, Any]:
    """默认 happy path：唯一终态 commit，commit / 工作树两侧 blob id 一致。"""

    task_rel = relative_path(fs.root, fs.tasks / f"{task_id}.json")

    result_rel = relative_path(fs.root, fs.results / f"{task_id}.json")

    defaults: dict[str, Any] = {
        "entries": [commit_entry(task_id)],
        "commit_blobs": {task_rel: BLOB_ID, result_rel: BLOB_ID},
        "worktree_blobs": {task_rel: BLOB_ID, result_rel: BLOB_ID},
        "blob_bytes": {BLOB_ID: BLOB_BYTES},
    }

    defaults.update(overrides)

    return fake_git(**defaults)


def seed_completed_task(
    fs: BindingFS,
    task_id: str = "GOLD-028",
    **task_extra: object,
) -> None:
    write_task(fs.tasks, task_id, human_gate="L1", max_attempts=3, **task_extra)

    write_result(fs.results, task_id)


def build(
    fs: BindingFS,
    task_id: str = "GOLD-028",
    *,
    fake: dict[str, Any] | None = None,
    generated_at: str = AUDIT_TIME,
) -> dict[str, Any]:
    """构建 manifest（默认 happy path 注入点）。"""

    injections = fake if fake is not None else consistent_fake(fs, task_id)

    return binding.build_review_binding_manifest(
        task_id,
        root=fs.root,
        tasks_dir=fs.tasks,
        results_dir=fs.results,
        generated_at=generated_at,
        **injections,
    )


def issue_codes(manifest: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in manifest["issues"]]


def issue_details(manifest: dict[str, Any], code: str) -> str:
    return " | ".join(issue["detail"] for issue in manifest["issues"] if issue["code"] == code)



def canonical_digest(payload: object) -> str:
    """独立实现的确定性摘要（与模块同口径，但由测试自己复算）。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# ============================================================
# 1. 确定性 / 只读 / 内容身份
# ============================================================


def test_manifest_is_read_only_and_deterministic(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    before = tree_digest(binding_fs.root)

    first = build(binding_fs)

    after = tree_digest(binding_fs.root)

    second = build(binding_fs)

    assert first == second
    assert before == after
    assert first["read_only"] is True
    assert first["schema"] == binding.REVIEW_BINDING_SCHEMA
    assert first["schema_version"] == binding.REVIEW_BINDING_SCHEMA_VERSION
    assert first["issues"] == []
    assert first["binding"]["facts_complete"] is True
    assert first["summary"] == {
        "issue_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "facts_complete": True,
        "exit_code": binding.EXIT_OK,
    }

    rendered = binding.render_review_binding_manifest(first)

    assert json.loads(rendered) == first
    assert rendered == binding.render_review_binding_manifest(second)


def test_manifest_field_order_is_stable(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs)

    assert tuple(manifest) == binding.MANIFEST_FIELD_ORDER
    assert tuple(manifest["paths"]) == binding.PATH_FIELD_ORDER
    assert tuple(manifest["task"]) == binding.TASK_FIELD_ORDER
    assert tuple(manifest["result"]) == binding.RESULT_FIELD_ORDER
    assert tuple(manifest["commit"]) == binding.COMMIT_FIELD_ORDER
    assert tuple(manifest["validation"]) == binding.VALIDATION_FIELD_ORDER
    assert tuple(manifest["binding"]) == binding.BINDING_FIELD_ORDER
    assert manifest["determinism"]["field_order"] == list(binding.MANIFEST_FIELD_ORDER)


def test_manifest_render_is_ascii_and_byte_stable(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs)

    rendered = binding.render_review_binding_manifest(manifest)

    assert rendered.isascii()
    assert rendered.endswith("\n")
    assert rendered == binding.render_review_binding_manifest(build(binding_fs))


def test_wall_clock_is_excluded_from_facts_digest(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    first = build(binding_fs, generated_at=AUDIT_TIME)

    second = build(binding_fs, generated_at=AUDIT_TIME_LATER)

    assert first["generated_at"] != second["generated_at"]
    assert first["facts_digest"] == second["facts_digest"]

    facts = binding.manifest_facts(first)

    for excluded in binding.FACTS_EXCLUDED_KEYS:
        assert excluded not in facts

    assert first["determinism"]["wall_clock_in_facts"] is False
    assert first["determinism"]["mtime_used_as_identity"] is False
    assert first["determinism"]["model_output_used_as_identity"] is False


def test_facts_digest_tracks_content_identity(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    before = build(binding_fs)

    write_result(binding_fs.results, "GOLD-028", finished_at="2026-09-23T14:00:00+08:00")

    after = build(binding_fs)

    assert before["facts_digest"] != after["facts_digest"]
    assert after["facts_digest"] == canonical_digest(binding.manifest_facts(after))


def test_result_sha256_is_recomputable_with_independent_hashlib(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs)

    result_path = binding_fs.results / "GOLD-028.json"

    # canonical（目标 commit 里的 Git 存储字节）与 worktree（本地原始字节）两套口径都可由
    # 独立 hashlib 复算；canonical 才是 GPT 写 Review 台账时使用的唯一口径。
    assert manifest["result"]["sha256"] == sha256_of_bytes(BLOB_BYTES)
    assert manifest["result"]["bytes"] == len(BLOB_BYTES)
    assert manifest["result"]["worktree_sha256"] == sha256_of(result_path)
    assert manifest["result"]["worktree_bytes"] == result_path.stat().st_size
    assert manifest["result"]["worktree_matches_commit"] is True
    assert manifest["result"]["task_id"] == "GOLD-028"
    assert manifest["result"]["status"] == "completed"
    assert manifest["result"]["known_status"] is True
    assert manifest["result"]["terminal"] is True
    assert manifest["result"]["finished_at"] == RESULT_FINISHED_AT
    assert manifest["result"]["attempt_count"] is None
    assert manifest["result"]["max_attempts"] is None


def test_task_content_identity_is_recomputable(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028", title="A", human_gate="L1", max_attempts=3)

    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    expected_metadata: dict[str, Any] = {
        "task_id": "GOLD-028",
        "title": "A",
        "type": None,
        "human_gate": "L1",
        "depends_on": None,
        "auto_start": None,
        "requires_human_approval": None,
        "max_attempts": 3,
    }

    assert manifest["task"]["sha256"] == sha256_of_bytes(BLOB_BYTES)
    assert manifest["task"]["worktree_sha256"] == sha256_of(binding_fs.tasks / "GOLD-028.json")
    assert manifest["task"]["worktree_matches_commit"] is True
    assert manifest["task"]["metadata_digest"] == canonical_digest(expected_metadata)
    assert manifest["task"]["human_gate"] == "L1"



# ============================================================
# 2. 终态 commit 绑定 + validation 摘要
# ============================================================


def test_completion_commit_identity_is_bound(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    commit = build(binding_fs)["commit"]

    assert commit["resolved"] is True
    assert commit["reason_code"] is None
    assert commit["sha"] == COMMIT_SHA
    assert commit["branch"] == BRANCH
    assert commit["head"] == FAKE_HEAD
    assert commit["subject"] == "ai: complete GOLD-028"
    assert commit["committed_at"] == "2026-09-23T13:51:00+08:00"
    assert commit["history_scope"].startswith("HEAD-reachable")

    manifest = build(binding_fs)

    assert manifest["result"]["worktree_matches_commit"] is True
    assert manifest["task"]["worktree_matches_commit"] is True


def test_blocked_result_binds_blocked_subject(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_result(binding_fs.results, "GOLD-028", status="blocked")

    manifest = build(
        binding_fs,
        fake=consistent_fake(binding_fs, entries=[commit_entry("GOLD-028", outcome="blocked")]),
    )

    assert manifest["commit"]["resolved"] is True
    assert manifest["commit"]["subject"] == "ai: blocked GOLD-028"
    assert manifest["issues"] == []


def test_other_task_subject_is_never_reused(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(
        binding_fs,
        fake=consistent_fake(binding_fs, entries=[commit_entry("GOLD-029")]),
    )

    assert manifest["commit"]["resolved"] is False
    assert manifest["commit"]["sha"] is None
    assert binding.ISSUE_COMMIT_NOT_FOUND in issue_codes(manifest)


def test_worktree_drift_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    task_rel = relative_path(binding_fs.root, binding_fs.tasks / "GOLD-028.json")

    result_rel = relative_path(binding_fs.root, binding_fs.results / "GOLD-028.json")

    manifest = build(
        binding_fs,
        fake=consistent_fake(
            binding_fs,
            worktree_blobs={task_rel: BLOB_ID, result_rel: DRIFTED_BLOB_ID},
        ),
    )

    assert manifest["result"]["worktree_matches_commit"] is False
    assert manifest["task"]["worktree_matches_commit"] is True
    assert binding.ISSUE_WORKTREE_COMMIT_MISMATCH in issue_codes(manifest)
    assert manifest["binding"]["facts_complete"] is False
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT


def test_mtime_is_not_used_as_content_identity(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    before = build(binding_fs)

    result_path = binding_fs.results / "GOLD-028.json"

    os.utime(result_path, (1_600_000_000, 1_600_000_000))

    after = build(binding_fs)

    assert after["result"]["worktree_sha256"] == before["result"]["worktree_sha256"]
    assert after["result"]["sha256"] == before["result"]["sha256"]
    assert after["facts_digest"] == before["facts_digest"]


def test_validation_summary_reflects_recorded_results(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_result(
        binding_fs.results,
        "GOLD-028",
        attempts=[
            {
                "attempt": 1,
                "validations": [
                    {"command": "pytest -q", "returncode": 0, "timed_out": False},
                    {"command": "ruff check .", "returncode": 1, "timed_out": False},
                    {"command": "mypy", "returncode": 0, "timed_out": True},
                ],
            }
        ],
    )

    validation = build(binding_fs)["validation"]

    assert validation["status"] == "failed"
    assert validation["attempt_count"] == 1
    assert validation["validation_count"] == 3
    assert validation["commands"] == ["mypy", "pytest -q", "ruff check ."]
    assert [item["passed"] for item in validation["results"]] == [True, False, False]
    assert validation["results"][2]["timed_out"] is True
    assert validation["digest"] == canonical_digest(validation["results"])
    assert "未重新执行" in validation["source"]


def test_validation_summary_passed_and_not_reported(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_result(
        binding_fs.results,
        "GOLD-028",
        attempts=[
            {"attempt": 1, "validations": [{"command": "pytest -q", "returncode": 0}]},
        ],
    )

    assert build(binding_fs)["validation"]["status"] == "passed"

    write_task(binding_fs.tasks, "GOLD-029")

    write_result(binding_fs.results, "GOLD-029")

    assert build(binding_fs, "GOLD-029")["validation"]["status"] == "not_reported"



# ============================================================
# 3. fail-closed（缺失 / 损坏 / 漂移 / 身份不可解析）
# ============================================================


@pytest.mark.parametrize(
    "task_id",
    ["", "   ", "../escape", "GOLD-028/../x", "GOLD-028\\x", "a b", ".hidden"],
)
def test_invalid_task_id_is_fail_closed(binding_fs: BindingFS, task_id: str) -> None:
    manifest = build(binding_fs, task_id, fake=fake_git())

    assert issue_codes(manifest) == [binding.ISSUE_TASK_ID_INVALID]
    assert manifest["task_id"] is None
    assert manifest["paths"]["task_file"] is None
    assert manifest["paths"]["result_file"] is None
    assert manifest["task"] == {field: None for field in binding.TASK_FIELD_ORDER}
    assert manifest["binding"]["facts_complete"] is False
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT


def test_missing_task_file_is_fail_closed(binding_fs: BindingFS) -> None:
    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    assert binding.ISSUE_TASK_FILE_MISSING in issue_codes(manifest)
    assert manifest["task"]["worktree_sha256"] is None
    assert manifest["task"]["task_id"] is None
    assert manifest["task"]["worktree_matches_commit"] is None
    assert manifest["binding"]["facts_complete"] is False


def test_corrupt_task_file_is_fail_closed(binding_fs: BindingFS) -> None:
    write_raw_task(binding_fs.tasks, "GOLD-028", "{not json")

    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    assert binding.ISSUE_TASK_FILE_UNREADABLE in issue_codes(manifest)
    assert manifest["task"]["worktree_sha256"] == sha256_of(binding_fs.tasks / "GOLD-028.json")
    assert manifest["task"]["sha256"] == sha256_of_bytes(BLOB_BYTES)
    assert manifest["task"]["task_id"] is None


def test_task_file_must_be_json_object(binding_fs: BindingFS) -> None:
    write_raw_task(binding_fs.tasks, "GOLD-028", "[1, 2]")

    write_result(binding_fs.results, "GOLD-028")

    assert binding.ISSUE_TASK_FILE_UNREADABLE in issue_codes(build(binding_fs))


def test_task_file_task_id_mismatch_is_fail_closed(binding_fs: BindingFS) -> None:
    write_raw_task(binding_fs.tasks, "GOLD-028", {"task_id": "GOLD-027"})

    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    assert binding.ISSUE_TASK_FILE_TASK_ID_MISMATCH in issue_codes(manifest)
    assert manifest["task"]["task_id"] == "GOLD-027"
    assert "GOLD-027" in issue_details(manifest, binding.ISSUE_TASK_FILE_TASK_ID_MISMATCH)


def test_missing_result_is_fail_closed(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    manifest = build(binding_fs)

    assert binding.ISSUE_RESULT_MISSING in issue_codes(manifest)
    assert manifest["result"]["worktree_sha256"] is None
    assert manifest["result"]["worktree_matches_commit"] is None
    assert manifest["result"]["sha256"] == sha256_of_bytes(BLOB_BYTES)
    assert manifest["validation"]["status"] == "not_reported"
    assert manifest["binding"]["facts_complete"] is False


def test_corrupt_result_is_fail_closed(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_raw_result(binding_fs.results, "GOLD-028", "{oops")

    manifest = build(binding_fs)

    assert binding.ISSUE_RESULT_UNREADABLE in issue_codes(manifest)
    assert manifest["result"]["worktree_sha256"] == sha256_of(
        binding_fs.results / "GOLD-028.json"
    )
    assert manifest["result"]["sha256"] == sha256_of_bytes(BLOB_BYTES)


def test_result_must_be_json_object(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_raw_result(binding_fs.results, "GOLD-028", "42")

    assert binding.ISSUE_RESULT_UNREADABLE in issue_codes(build(binding_fs))


def test_result_task_id_mismatch_is_fail_closed(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028")

    write_raw_result(binding_fs.results, "GOLD-028", {"task_id": "GOLD-027", "status": "completed"})

    manifest = build(binding_fs)

    assert binding.ISSUE_RESULT_TASK_ID_MISMATCH in issue_codes(manifest)
    assert manifest["result"]["task_id"] == "GOLD-027"


def test_unknown_result_status_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    write_result(binding_fs.results, "GOLD-028", status="weird")

    manifest = build(binding_fs)

    assert binding.ISSUE_RESULT_STATUS_UNKNOWN in issue_codes(manifest)
    assert manifest["result"]["known_status"] is False
    assert manifest["result"]["status"] == "weird"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_non_terminal_result_is_fail_closed(binding_fs: BindingFS, status: str) -> None:
    seed_completed_task(binding_fs)

    write_result(binding_fs.results, "GOLD-028", status=status)

    manifest = build(binding_fs)

    assert binding.ISSUE_RESULT_NOT_TERMINAL in issue_codes(manifest)
    assert manifest["result"]["terminal"] is False
    assert binding.TERMINAL_STATUS_HINT in issue_details(
        manifest, binding.ISSUE_RESULT_NOT_TERMINAL
    )



def test_git_log_unavailable_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs, fake=fake_git(log_error="git unavailable: boom"))

    assert binding.ISSUE_GIT_LOG_UNAVAILABLE in issue_codes(manifest)
    assert manifest["commit"]["resolved"] is False
    assert manifest["commit"]["reason_code"] == binding.ISSUE_GIT_LOG_UNAVAILABLE


@pytest.mark.parametrize("entries", [[], [commit_entry("GOLD-029")]])
def test_missing_completion_commit_is_fail_closed(
    binding_fs: BindingFS, entries: list[dict[str, Any]]
) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs, fake=consistent_fake(binding_fs, entries=entries))

    assert binding.ISSUE_COMMIT_NOT_FOUND in issue_codes(manifest)
    assert manifest["commit"]["reason_code"] == binding.ISSUE_COMMIT_NOT_FOUND
    assert manifest["commit"]["sha"] is None
    assert manifest["binding"]["facts_complete"] is False


def test_ambiguous_completion_commit_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(
        binding_fs,
        fake=consistent_fake(
            binding_fs,
            entries=[
                commit_entry("GOLD-028"),
                commit_entry("GOLD-028", sha=OTHER_COMMIT_SHA, outcome="blocked"),
            ],
        ),
    )

    assert binding.ISSUE_COMMIT_AMBIGUOUS in issue_codes(manifest)
    assert manifest["commit"]["reason_code"] == binding.ISSUE_COMMIT_AMBIGUOUS
    assert "绝不猜测" in issue_details(manifest, binding.ISSUE_COMMIT_AMBIGUOUS)
    assert COMMIT_SHA in issue_details(manifest, binding.ISSUE_COMMIT_AMBIGUOUS)


@pytest.mark.parametrize("sha", ["", "abc", COMMIT_SHA[:12], COMMIT_SHA.upper()])
def test_invalid_commit_sha_is_fail_closed(binding_fs: BindingFS, sha: str) -> None:
    seed_completed_task(binding_fs)

    manifest = build(
        binding_fs,
        fake=consistent_fake(binding_fs, entries=[commit_entry("GOLD-028", sha=sha)]),
    )

    assert binding.ISSUE_COMMIT_SHA_INVALID in issue_codes(manifest)
    assert manifest["commit"]["reason_code"] == binding.ISSUE_COMMIT_SHA_INVALID
    assert manifest["commit"]["sha"] is None


def test_result_absent_from_commit_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    task_rel = relative_path(binding_fs.root, binding_fs.tasks / "GOLD-028.json")

    manifest = build(binding_fs, fake=consistent_fake(binding_fs, commit_blobs={task_rel: BLOB_ID}))

    assert binding.ISSUE_RESULT_NOT_IN_COMMIT in issue_codes(manifest)
    assert manifest["result"]["sha256"] is None
    assert manifest["result"]["worktree_matches_commit"] is None


def test_task_absent_from_commit_is_fail_closed(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    result_rel = relative_path(binding_fs.root, binding_fs.results / "GOLD-028.json")

    manifest = build(
        binding_fs,
        fake=consistent_fake(binding_fs, commit_blobs={result_rel: BLOB_ID}),
    )

    assert binding.ISSUE_TASK_NOT_IN_COMMIT in issue_codes(manifest)
    assert manifest["task"]["sha256"] is None
    assert manifest["task"]["worktree_matches_commit"] is None


@pytest.mark.parametrize(
    "override",
    [
        {"commit_blob_error": binding.GIT_BLOB_UNAVAILABLE},
        {"blob_bytes_error": binding.GIT_BLOB_UNAVAILABLE},
        {"worktree_blob_error": binding.GIT_BLOB_UNAVAILABLE},
    ],
)
def test_blob_probe_failure_is_fail_closed(
    binding_fs: BindingFS, override: dict[str, Any]
) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs, fake=consistent_fake(binding_fs, **override))

    assert binding.ISSUE_GIT_LOG_UNAVAILABLE in issue_codes(manifest)
    assert manifest["binding"]["facts_complete"] is False


def test_missing_git_metadata_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 ``.git`` 事实（无法解析 branch/head）时必须 fail-closed。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"

    tasks.mkdir()
    results.mkdir()

    write_task(tasks, "GOLD-028")
    write_result(results, "GOLD-028")

    manifest = binding.build_review_binding_manifest(
        "GOLD-028",
        root=tmp_path,
        tasks_dir=tasks,
        results_dir=results,
        generated_at=AUDIT_TIME,
        **fake_git(entries=[]),
    )

    assert binding.ISSUE_GIT_INFO_UNAVAILABLE in issue_codes(manifest)
    assert manifest["commit"]["branch"] is None
    assert manifest["commit"]["head"] is None


def test_multiple_failures_are_sorted_and_all_errors(binding_fs: BindingFS) -> None:
    write_result(binding_fs.results, "GOLD-028", status="weird")

    manifest = build(binding_fs, fake=fake_git())

    codes = issue_codes(manifest)

    assert codes == sorted(codes)
    assert codes == [
        binding.ISSUE_COMMIT_NOT_FOUND,
        binding.ISSUE_RESULT_STATUS_UNKNOWN,
        binding.ISSUE_TASK_FILE_MISSING,
    ]
    assert all(issue["severity"] == binding.SEVERITY_ERROR for issue in manifest["issues"])
    assert manifest["binding"]["reason_codes"] == codes
    assert manifest["summary"]["issue_count"] == len(manifest["issues"])
    assert manifest["summary"]["error_count"] == len(manifest["issues"])
    assert manifest["summary"]["warning_count"] == 0
    assert manifest["summary"]["exit_code"] == binding.EXIT_DRIFT



# ============================================================
# 4. GPT-only Review 边界 + 源码守卫
# ============================================================


def test_manifest_has_no_review_outcome_keys(binding_fs: BindingFS) -> None:
    seed_completed_task(binding_fs)

    manifest = build(binding_fs)

    keys = collect_keys(manifest)

    for forbidden in binding.FORBIDDEN_MANIFEST_KEYS:
        assert forbidden not in keys, forbidden

    assert manifest["authority"]["emits_review_outcome"] is False
    assert "facts_complete_semantics" in manifest["authority"]


def test_authority_declares_no_write_or_review_power() -> None:
    authority = binding.authority_section()

    assert authority["schema"] == binding.REVIEW_BINDING_AUTHORITY_SCHEMA
    assert authority["read_only"] is True
    assert authority["review_authority"] == "gpt_only"
    assert authority["writer_agents"] == ["gpt"]
    assert authority["reader_agents"] == ["cline", "deepseek"]
    assert authority["blocking_human_gates"] == sorted(orch.BLOCKING_HUMAN_GATES)

    for flag in (
        "emits_review_outcome",
        "tool_can_sign_review",
        "tool_can_advance_state",
        "tool_can_qualify_data",
        "tool_can_cross_human_gate",
        "writes_review_ledger",
        "writes_project_state",
        "writes_tasks",
        "writes_results",
    ):
        assert authority[flag] is False, flag


@pytest.mark.parametrize("gate", ["L3", "L4"])
def test_blocking_human_gate_is_recorded_but_never_crossed(
    binding_fs: BindingFS, gate: str
) -> None:
    write_task(binding_fs.tasks, "GOLD-028", human_gate=gate)

    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    assert manifest["binding"]["human_gate"] == gate
    assert manifest["binding"]["blocking_human_gate"] is True
    assert manifest["issues"] == []
    assert manifest["commit"]["resolved"] is True
    assert manifest["authority"]["tool_can_cross_human_gate"] is False


def test_l1_gate_and_human_approval_flag_are_recorded(binding_fs: BindingFS) -> None:
    write_task(binding_fs.tasks, "GOLD-028", human_gate="L1", requires_human_approval=True)

    write_result(binding_fs.results, "GOLD-028")

    manifest = build(binding_fs)

    assert manifest["binding"]["human_gate"] == "L1"
    assert manifest["binding"]["blocking_human_gate"] is False
    assert manifest["binding"]["requires_human_approval"] is True


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["commit", "-m", "x"],
        ["push"],
        ["reset", "--hard"],
        ["checkout", "-b", "x"],
        ["add", "-A"],
        ["merge", "other"],
        ["clean", "-fd"],
    ],
)
def test_run_git_refuses_non_read_only_subcommands(tmp_path: Path, args: list[str]) -> None:
    payload, error = binding.run_git(tmp_path, args)

    assert payload is None
    assert error is not None
    assert "refused" in error


def test_git_read_only_allowlist_is_exact() -> None:
    assert binding.GIT_READ_ONLY_SUBCOMMANDS == ("log", "ls-tree", "cat-file", "hash-object")


def test_terminal_commit_subjects_follow_orchestrator_contract() -> None:
    assert binding.terminal_commit_subjects("GOLD-028") == (
        "ai: complete GOLD-028",
        "ai: blocked GOLD-028",
    )


def test_match_terminal_commit_is_deterministic() -> None:
    entries = [commit_entry("GOLD-028")]

    entry, code, detail = binding.match_terminal_commit(entries, "GOLD-028")

    assert code is None
    assert detail is None
    assert entry is not None
    assert entry["sha"] == COMMIT_SHA

    entry, code, detail = binding.match_terminal_commit(entries, "GOLD-029")

    assert entry is None
    assert code == binding.ISSUE_COMMIT_NOT_FOUND
    assert detail is not None

    entry, code, _ = binding.match_terminal_commit([*entries, commit_entry("GOLD-028")], "GOLD-028")

    assert entry is None
    assert code == binding.ISSUE_COMMIT_AMBIGUOUS


def test_module_source_has_no_write_or_state_paths() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_source_has_no_database_or_network_imports() -> None:
    for forbidden in (
        "sqlalchemy",
        "psycopg",
        "requests",
        "aiohttp",
        "httpx",
        "socket",
        "urllib",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden


def test_forbidden_review_keys_only_appear_in_negative_declaration() -> None:
    """Review 结论字段名只允许出现在 ``FORBIDDEN_MANIFEST_KEYS`` 这个**否定声明**里。"""

    for key in binding.FORBIDDEN_MANIFEST_KEYS:
        assert MODULE_SOURCE.count(f'"{key}"') == 1, key


def test_module_has_single_read_only_subprocess_chokepoint() -> None:
    assert MODULE_SOURCE.count("subprocess.run(") == 1
    assert MODULE_SOURCE.count('["git", *args]') == 1

    for forbidden in (
        "git commit",
        "git push",
        "git reset",
        "git checkout",
        "git add",
        "git merge ",
        "clean -fd",
    ):
        assert forbidden not in MODULE_SOURCE, forbidden


def test_module_exposes_no_planning_api() -> None:
    public = {name for name in dir(binding) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    for name in (
        "build_review_binding_manifest",
        "render_review_binding_manifest",
        "authority_section",
        "determinism_section",
        "validation_section",
        "match_terminal_commit",
        "load_task_facts",
        "load_result_facts",
        "resolve_commit_binding",
        "manifest_exit_code",
    ):
        assert name in public, name


def test_module_reuses_planner_snapshot_read_only_reader() -> None:
    from orchestrator import planner_snapshot as planner

    assert binding.planner is planner
    assert binding.REVIEW_BINDING_SCHEMA == "gold-ai/review-binding-manifest/v1"
    assert binding.REVIEW_BINDING_SCHEMA_VERSION == 1



# ============================================================
# 5. 只读 CLI
# ============================================================


def test_cli_option_contract_excludes_write_and_planning_flags() -> None:
    parser = binding.build_parser()

    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert option_strings == {
        "-h",
        "--help",
        "--task",
        "--root",
        "--tasks-dir",
        "--results-dir",
        "--generated-at",
    }

    for forbidden in (
        "--output",
        "--out",
        "--write",
        "--update-state",
        "--append-entry",
        "--sign-review",
        "--approve",
        "--plan",
        "--next-task",
        "--no-dry-run",
    ):
        assert forbidden not in option_strings


def patch_git(
    monkeypatch: pytest.MonkeyPatch,
    injections: dict[str, Any],
) -> None:
    monkeypatch.setattr(binding, "git_commit_log", injections["commit_log_provider"])
    monkeypatch.setattr(binding, "git_blob_id", injections["blob_id_reader"])
    monkeypatch.setattr(binding, "git_blob_content", injections["blob_bytes_reader"])
    monkeypatch.setattr(binding, "git_worktree_blob_id", injections["worktree_blob_id_reader"])


def test_cli_help_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.review_binding", "--help"],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode == 0
    assert b"--task" in completed.stdout
    assert b"--generated-at" in completed.stdout

    for forbidden in (b"--output", b"--write", b"--no-dry-run", b"--approve"):
        assert forbidden not in completed.stdout


def test_cli_requires_task_argument() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.review_binding"],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""


def test_cli_prints_ascii_json_and_returns_zero(
    binding_fs: BindingFS,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_completed_task(binding_fs)

    patch_git(monkeypatch, consistent_fake(binding_fs))

    exit_code = binding.main(
        [
            "--task",
            "GOLD-028",
            "--root",
            str(binding_fs.root),
            "--tasks-dir",
            str(binding_fs.tasks),
            "--results-dir",
            str(binding_fs.results),
            "--generated-at",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == binding.EXIT_OK
    assert captured.err == ""
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["schema"] == binding.REVIEW_BINDING_SCHEMA
    assert payload["generated_at"] == AUDIT_TIME
    assert payload["binding"]["facts_complete"] is True
    assert payload["summary"]["exit_code"] == binding.EXIT_OK


def test_cli_reports_fail_closed_with_exit_code_2(
    binding_fs: BindingFS,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(binding, "git_commit_log", lambda root: ([], None))
    monkeypatch.setattr(
        binding, "git_blob_id", lambda root, sha, relative: (None, binding.GIT_BLOB_ABSENT)
    )
    monkeypatch.setattr(
        binding, "git_blob_content", lambda root, blob_id: (None, binding.GIT_BLOB_UNAVAILABLE)
    )
    monkeypatch.setattr(
        binding,
        "git_worktree_blob_id",
        lambda root, relative: (None, binding.GIT_BLOB_UNAVAILABLE),
    )

    exit_code = binding.main(
        [
            "--task",
            "GOLD-028",
            "--root",
            str(binding_fs.root),
            "--tasks-dir",
            str(binding_fs.tasks),
            "--results-dir",
            str(binding_fs.results),
            "--generated-at",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == binding.EXIT_DRIFT
    assert binding.ISSUE_RESULT_MISSING in captured.err
    assert binding.ISSUE_TASK_FILE_MISSING in captured.err
    assert "[binding] facts_complete=False" in captured.err

    payload = json.loads(captured.out)

    assert payload["binding"]["facts_complete"] is False
    assert payload["summary"]["exit_code"] == binding.EXIT_DRIFT


def test_cli_subprocess_is_byte_stable_for_fail_closed_root(
    binding_fs: BindingFS,
) -> None:
    """fresh subprocess（真实 git 路径）：临时 root 无 commit ⇒ 稳定 fail-closed 输出。"""

    write_task(binding_fs.tasks, "GOLD-028")
    write_result(binding_fs.results, "GOLD-028")

    command = [
        sys.executable,
        "-m",
        "orchestrator.review_binding",
        "--task",
        "GOLD-028",
        "--root",
        str(binding_fs.root),
        "--tasks-dir",
        str(binding_fs.tasks),
        "--results-dir",
        str(binding_fs.results),
        "--generated-at",
        AUDIT_TIME,
    ]

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == binding.EXIT_DRIFT
    assert first.stdout == second.stdout
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["generated_at"] == AUDIT_TIME
    assert payload["binding"]["facts_complete"] is False
