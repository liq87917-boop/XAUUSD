"""``orchestrator.review_ledger_integrity`` 只读完整性门禁回归（GOLD-032）。

覆盖：
1. 正常链：合法台账 + 与 manifest 一致的客观身份 ⇒ 零 issue、integrity_ok、退出码 0；
2. 删项（覆盖窗口内 completed 但无 ledger 条目）⇒ ``LEDGER_CHAIN_GAP``；
3. 改 hash / 改 commit（sha、branch）/ 改 result status 或 finished_at ⇒ 稳定 mismatch code；
4. 重复 task ⇒ ``LEDGER_DUPLICATE_TASK``；乱序 ⇒ ``LEDGER_ORDER_REGRESSION``；
5. 未知 verdict / 未知 schema / 缺失或损坏台账 ⇒ fail-closed（退出码 2 / 3）；
6. ledger / manifest 漂移（manifest facts_complete=false）⇒ ``LEDGER_MANIFEST_FACTS_INCOMPLETE``；
7. 只读与确定性：两次构建字节相同、facts_digest 不含 wall-clock、文件树逐字节不变；
8. 职责边界：只读、无 verdict / ledger / state 写权限，CLI 无写 / 规划开关，源码无写入路径。

所有测试只在 ``tmp_path`` 内构造文件，绝不对真实仓库做任何写操作；manifest 通过假
builder 注入，因此测试完全零子进程 / 零 Git。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

AUDIT_TIME_LATER = "2026-09-24T00:00:00+08:00"

BRANCH = "cline-agent"

FINISHED_AT = "2026-09-23T13:50:47+08:00"

REVIEWED_AT = "2026-09-23T14:10:00+08:00"

COMMIT_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

COMMIT_B = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345679"

COMMIT_C = "c1b2c3d4e5f60718293a4b5c6d7e8f9012345670"

OTHER_COMMIT = "d1b2c3d4e5f60718293a4b5c6d7e8f9012345671"

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULE_SOURCE = Path(integrity.__file__).read_text(encoding="utf-8")

CHAIN = ("GOLD-025", "GOLD-026", "GOLD-027")

COMMIT_BY_TASK = {"GOLD-025": COMMIT_A, "GOLD-026": COMMIT_B, "GOLD-027": COMMIT_C}

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
    "append_review_entry",
    "sign_review",
    "repair_ledger",
    "write_review_ledger",
}

# 只读模块源码里绝不出现的写入 / 状态 / 网络痕迹。
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
    "tempfile",
    "subprocess",
    "requests",
    "httpx",
    "aiohttp",
    "psycopg",
    "sqlalchemy",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True)
class IntegrityFS:
    root: Path
    tasks: Path
    results: Path
    ledger_path: Path


@pytest.fixture()
def integrity_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IntegrityFS:
    """把 tasks / results / ledger 全部重定向到 tmp_path（对真实仓库零影响）。"""

    tasks = tmp_path / "tasks"
    results = tmp_path / "results"

    tasks.mkdir()
    results.mkdir()

    ledger_path = tmp_path / "GPT_REVIEW_LEDGER.json"

    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)

    return IntegrityFS(root=tmp_path, tasks=tasks, results=results, ledger_path=ledger_path)


def write_result(
    fs: IntegrityFS,
    task_id: str,
    *,
    status: str = "completed",
    finished_at: str = FINISHED_AT,
) -> None:
    write_json(
        fs.results / f"{task_id}.json",
        {"task_id": task_id, "status": status, "finished_at": finished_at, "attempts": []},
    )


def result_sha(fs: IntegrityFS, task_id: str) -> str:
    return hashlib.sha256((fs.results / f"{task_id}.json").read_bytes()).hexdigest()


def make_entry(
    task_id: str,
    *,
    result_sha256: str,
    commit_sha: str,
    verdict: str = "PASS",
    status: str = "completed",
    finished_at: str = FINISHED_AT,
    branch: str = BRANCH,
    reviewed_at: str = REVIEWED_AT,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "verdict": verdict,
        "reviewer": "gpt",
        "reviewer_role": "GPT",
        "acceptance_summary": "GPT review PASS: fixture",
        "reviewed_result": {
            "result_sha256": result_sha256,
            "status": status,
            "finished_at": finished_at,
        },
        "reviewed_commit": {"sha": commit_sha, "branch": branch},
        "reviewed_at": reviewed_at,
    }


def write_ledger(
    fs: IntegrityFS,
    entries: list[dict[str, Any]],
    *,
    reviewed_from: str | None = None,
    schema: str = ledger_mod.REVIEW_LEDGER_SCHEMA,
    schema_version: object = ledger_mod.REVIEW_LEDGER_SCHEMA_VERSION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": schema,
        "schema_version": schema_version,
        "entries": entries,
    }

    if reviewed_from is not None:
        payload["reviewed_from"] = reviewed_from

    write_json(fs.ledger_path, payload)

    return payload


def make_manifest(
    *,
    result_sha256: str | None,
    commit_sha: str | None,
    status: str | None = "completed",
    finished_at: str | None = FINISHED_AT,
    branch: str | None = BRANCH,
    facts_complete: bool = True,
    reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "binding": {"facts_complete": facts_complete, "reason_codes": list(reason_codes)},
        "result": {"sha256": result_sha256, "status": status, "finished_at": finished_at},
        "commit": {"sha": commit_sha, "branch": branch},
        "facts_digest": sha256_text(f"{result_sha256}|{commit_sha}"),
    }


def builder_from(mapping: dict[str, dict[str, Any]]) -> integrity.ManifestBuilder:
    """基于显式 mapping 的 manifest 假 builder（零 Git / 零子进程）。"""

    def build(task_id: str) -> dict[str, Any]:
        if task_id in mapping:
            return mapping[task_id]

        return make_manifest(
            result_sha256=None,
            commit_sha=None,
            status=None,
            finished_at=None,
            branch=None,
            facts_complete=False,
            reason_codes=("RESULT_MISSING",),
        )

    return build


def seed_chain(
    fs: IntegrityFS,
    task_ids: tuple[str, ...] = CHAIN,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """构造 completed results + 与 manifest 完全一致的合法台账条目。"""

    entries: list[dict[str, Any]] = []
    manifests: dict[str, dict[str, Any]] = {}

    for task_id in task_ids:
        write_result(fs, task_id)

        sha = result_sha(fs, task_id)

        entries.append(
            make_entry(task_id, result_sha256=sha, commit_sha=COMMIT_BY_TASK[task_id])
        )

        manifests[task_id] = make_manifest(
            result_sha256=sha,
            commit_sha=COMMIT_BY_TASK[task_id],
        )

    return entries, manifests


def build_report(
    fs: IntegrityFS,
    builder: integrity.ManifestBuilder,
    *,
    ledger_path: Path | None = None,
    generated_at: str = AUDIT_TIME,
) -> dict[str, Any]:
    return integrity.build_integrity_report(
        root=fs.root,
        tasks_dir=fs.tasks,
        results_dir=fs.results,
        ledger_path=ledger_path if ledger_path is not None else fs.ledger_path,
        generated_at=generated_at,
        manifest_builder=builder,
    )


def issue_codes(report: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


# ============================================================
# 1. 正常链：合法台账 + 客观身份一致
# ============================================================


def test_normal_chain_is_bound_and_integrity_ok(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert report["schema"] == integrity.LEDGER_INTEGRITY_SCHEMA
    assert report["schema_version"] == integrity.LEDGER_INTEGRITY_SCHEMA_VERSION
    assert report["read_only"] is True
    assert report["issues"] == []
    assert report["summary"]["integrity_ok"] is True
    assert report["summary"]["exit_code"] == integrity.EXIT_OK
    assert report["summary"]["bound_count"] == 3
    assert report["chain"]["ledger_order"] == list(CHAIN)
    assert report["chain"]["order_is_ascending"] is True
    assert report["chain"]["reviewed_at_is_ascending"] is True
    assert report["chain"]["window"] == {"from": "GOLD-025", "to": "GOLD-027"}
    assert report["chain"]["missing_in_window"] == []

    for view in report["bindings"]:
        assert view["bound"] is True
        assert all(value is True for value in view["matches"].values())


def test_ledger_verdicts_are_echoed_not_created(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert report["ledger"]["verdicts"] == {
        "GOLD-025": "PASS",
        "GOLD-026": "PASS",
        "GOLD-027": "PASS",
    }

    authority = report["authority"]

    assert authority["emits_review_outcome"] is False
    assert authority["creates_verdicts"] is False
    assert authority["modifies_verdicts"] is False
    assert authority["creates_acceptance_summary"] is False
    assert authority["creates_reviewed_at"] is False


# ============================================================
# 2. 删项（覆盖窗口内 completed 但台账无条目）
# ============================================================


def test_deleted_entry_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    kept = [entry for entry in entries if entry["task_id"] != "GOLD-026"]

    write_ledger(integrity_fs, kept, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_CHAIN_GAP in issue_codes(report)
    assert report["summary"]["integrity_ok"] is False
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL
    assert report["chain"]["missing_in_window"] == ["GOLD-026"]


# ============================================================
# 3. 改 hash / 改 commit / 改 status / 改 finished_at
# ============================================================


def test_changed_result_hash_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[1]["reviewed_result"]["result_sha256"] = "0" * 64

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_RESULT_HASH_MISMATCH in issue_codes(report)
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL

    view = next(item for item in report["bindings"] if item["task_id"] == "GOLD-026")

    assert view["bound"] is False
    assert view["matches"]["result_sha256"] is False


def test_changed_commit_sha_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[2]["reviewed_commit"]["sha"] = OTHER_COMMIT

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_COMMIT_SHA_MISMATCH in issue_codes(report)

    view = next(item for item in report["bindings"] if item["task_id"] == "GOLD-027")

    assert view["matches"]["commit_sha"] is False


def test_changed_commit_branch_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[0]["reviewed_commit"]["branch"] = "other-branch"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_COMMIT_BRANCH_MISMATCH in issue_codes(report)


def test_changed_result_status_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[1]["reviewed_result"]["status"] = "blocked"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_RESULT_STATUS_MISMATCH in issue_codes(report)


def test_changed_result_finished_at_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[1]["reviewed_result"]["finished_at"] = "2020-01-01T00:00:00+08:00"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_RESULT_FINISHED_AT_MISMATCH in issue_codes(report)


# ============================================================
# 4. 重复 task / 乱序 / review 时间回退
# ============================================================


def test_duplicate_task_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    duplicated = [*entries, dict(entries[1])]

    write_ledger(integrity_fs, duplicated, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_DUPLICATE_TASK in issue_codes(report)
    assert report["ledger"]["duplicate_tasks"] == ["GOLD-026"]
    assert report["summary"]["integrity_ok"] is False


def test_out_of_order_entries_are_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    reordered = [entries[2], entries[0], entries[1]]

    write_ledger(integrity_fs, reordered, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_ORDER_REGRESSION in issue_codes(report)
    assert report["chain"]["order_is_ascending"] is False
    assert report["chain"]["ledger_order"] == ["GOLD-027", "GOLD-025", "GOLD-026"]


def test_review_time_regression_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[0]["reviewed_at"] = "2026-09-23T18:00:00+08:00"
    entries[1]["reviewed_at"] = "2026-09-23T17:00:00+08:00"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_REVIEW_TIME_REGRESSION in issue_codes(report)
    assert report["chain"]["reviewed_at_is_ascending"] is False


# ============================================================
# 5. ledger / manifest 漂移
# ============================================================


def test_manifest_facts_incomplete_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    manifests["GOLD-026"] = make_manifest(
        result_sha256=None,
        commit_sha=None,
        status=None,
        finished_at=None,
        branch=None,
        facts_complete=False,
        reason_codes=("RESULT_MISSING",),
    )

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_MANIFEST_FACTS_INCOMPLETE in issue_codes(report)

    view = next(item for item in report["bindings"] if item["task_id"] == "GOLD-026")

    assert view["manifest_facts_complete"] is False
    assert view["manifest_reason_codes"] == ["RESULT_MISSING"]
    assert view["bound"] is False


def test_unknown_verdict_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    entries[0]["verdict"] = "MAYBE"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_ENTRY_INVALID in issue_codes(report)
    assert report["summary"]["exit_code"] == integrity.EXIT_INTEGRITY_FAIL


def test_unsupported_schema_is_fail_closed_with_exit_3(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, schema="gold-ai/gpt-review-ledger/v0")

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_SCHEMA_UNSUPPORTED in issue_codes(report)
    assert report["summary"]["exit_code"] == integrity.EXIT_LEDGER_UNAVAILABLE


def test_entries_not_a_list_is_fail_closed_with_exit_3(integrity_fs: IntegrityFS) -> None:
    write_json(
        integrity_fs.ledger_path,
        {"schema": ledger_mod.REVIEW_LEDGER_SCHEMA, "schema_version": 1, "entries": {}},
    )

    report = build_report(integrity_fs, builder_from({}))

    assert integrity.ISSUE_LEDGER_ENTRIES_INVALID in issue_codes(report)
    assert report["summary"]["exit_code"] == integrity.EXIT_LEDGER_UNAVAILABLE


def test_missing_ledger_is_fail_closed_with_exit_3(integrity_fs: IntegrityFS) -> None:
    report = build_report(integrity_fs, builder_from({}))

    assert integrity.ISSUE_LEDGER_MISSING in issue_codes(report)
    assert report["ledger"]["available"] is False
    assert report["bindings"] == []
    assert report["summary"]["exit_code"] == integrity.EXIT_LEDGER_UNAVAILABLE


def test_corrupted_ledger_is_fail_closed_with_exit_3(integrity_fs: IntegrityFS) -> None:
    integrity_fs.ledger_path.write_text("{not json", encoding="utf-8")

    report = build_report(integrity_fs, builder_from({}))

    assert integrity.ISSUE_LEDGER_UNREADABLE in issue_codes(report)
    assert report["summary"]["exit_code"] == integrity.EXIT_LEDGER_UNAVAILABLE


def test_ledger_metadata_invalid_is_fail_closed(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_json(
        integrity_fs.ledger_path,
        {
            "schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
            "schema_version": 1,
            "reviewed_from": 123,
            "entries": entries,
        },
    )

    report = build_report(integrity_fs, builder_from(manifests))

    assert integrity.ISSUE_LEDGER_METADATA_INVALID in issue_codes(report)


# ============================================================
# 6. 只读性与确定性
# ============================================================


def test_report_is_deterministic_and_read_only(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    before_tasks = tree_digest(integrity_fs.tasks)
    before_results = tree_digest(integrity_fs.results)
    before_ledger = integrity_fs.ledger_path.read_bytes()

    first = build_report(integrity_fs, builder_from(manifests))
    second = build_report(integrity_fs, builder_from(manifests))

    assert integrity.render_integrity_report(first) == integrity.render_integrity_report(second)

    assert tree_digest(integrity_fs.tasks) == before_tasks
    assert tree_digest(integrity_fs.results) == before_results
    assert integrity_fs.ledger_path.read_bytes() == before_ledger


def test_facts_digest_excludes_wall_clock(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    early = build_report(integrity_fs, builder_from(manifests), generated_at=AUDIT_TIME)
    late = build_report(integrity_fs, builder_from(manifests), generated_at=AUDIT_TIME_LATER)

    assert "generated_at" not in integrity.integrity_facts(early)
    assert early["generated_at"] != late["generated_at"]
    assert early["facts_digest"] == late["facts_digest"]


def test_facts_digest_is_recomputable(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    report = build_report(integrity_fs, builder_from(manifests))

    expected = hashlib.sha256(
        json.dumps(
            integrity.integrity_facts(report),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()

    assert report["facts_digest"] == expected


def test_changed_ledger_single_byte_changes_digest(integrity_fs: IntegrityFS) -> None:
    entries, manifests = seed_chain(integrity_fs)

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    baseline = build_report(integrity_fs, builder_from(manifests))["facts_digest"]

    entries[0]["reviewed_commit"]["sha"] = OTHER_COMMIT

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    drifted = build_report(integrity_fs, builder_from(manifests))

    assert drifted["facts_digest"] != baseline
    assert integrity.ISSUE_COMMIT_SHA_MISMATCH in issue_codes(drifted)


# ============================================================
# 7. 职责边界：只读、无 verdict / ledger / state 写权限
# ============================================================


def test_authority_section_declares_read_only() -> None:
    authority = integrity.authority_section()

    assert authority["schema"] == integrity.LEDGER_INTEGRITY_AUTHORITY_SCHEMA
    assert authority["read_only"] is True
    assert authority["emits_review_outcome"] is False
    assert authority["echoes_ledger_facts_only"] is True
    assert authority["tool_can_sign_review"] is False
    assert authority["tool_can_repair_ledger"] is False
    assert authority["tool_can_advance_state"] is False
    assert authority["tool_can_qualify_data"] is False
    assert authority["tool_can_cross_human_gate"] is False
    assert authority["writes_review_ledger"] is False
    assert authority["writes_project_state"] is False
    assert authority["writes_tasks"] is False
    assert authority["writes_results"] is False
    assert authority["review_authority"] == "gpt_only"


def test_module_exposes_no_write_or_planning_api() -> None:
    public = {name for name in dir(integrity) if not name.startswith("_")}

    assert public.isdisjoint(FORBIDDEN_PLANNING_API)

    for name in (
        "build_integrity_report",
        "binding_entry_view",
        "chain_section",
        "authority_section",
        "report_exit_code",
        "render_integrity_report",
        "main",
    ):
        assert name in public, name


def test_module_source_has_no_write_or_network_path() -> None:
    for forbidden in FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden not in MODULE_SOURCE, forbidden


def test_cli_option_contract_excludes_write_and_planning_flags() -> None:
    parser = integrity.build_parser()

    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert option_strings == {
        "-h",
        "--help",
        "--root",
        "--tasks-dir",
        "--results-dir",
        "--ledger",
        "--generated-at",
    }

    for forbidden in (
        "--out",
        "--output",
        "--write",
        "--append-entry",
        "--sign-review",
        "--approve",
        "--repair",
        "--update-state",
        "--plan",
        "--no-dry-run",
    ):
        assert forbidden not in option_strings


def test_cli_help_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.review_ledger_integrity", "--help"],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert completed.returncode == 0
    assert b"--ledger" in completed.stdout
    assert b"--generated-at" in completed.stdout

    for forbidden in (b"--out", b"--write", b"--approve", b"--no-dry-run"):
        assert forbidden not in completed.stdout


def test_cli_reports_missing_ledger_with_exit_3(
    integrity_fs: IntegrityFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = integrity.main(
        [
            "--root",
            str(integrity_fs.root),
            "--tasks-dir",
            str(integrity_fs.tasks),
            "--results-dir",
            str(integrity_fs.results),
            "--ledger",
            str(integrity_fs.ledger_path),
            "--generated-at",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == integrity.EXIT_LEDGER_UNAVAILABLE
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["schema"] == integrity.LEDGER_INTEGRITY_SCHEMA
    assert payload["summary"]["exit_code"] == integrity.EXIT_LEDGER_UNAVAILABLE
    assert integrity.ISSUE_LEDGER_MISSING in {issue["code"] for issue in payload["issues"]}


def test_cli_reports_drift_with_exit_2(
    integrity_fs: IntegrityFS,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries, _ = seed_chain(integrity_fs)

    entries[0]["verdict"] = "MAYBE"

    write_ledger(integrity_fs, entries, reviewed_from="GOLD-025")

    exit_code = integrity.main(
        [
            "--root",
            str(integrity_fs.root),
            "--tasks-dir",
            str(integrity_fs.tasks),
            "--results-dir",
            str(integrity_fs.results),
            "--ledger",
            str(integrity_fs.ledger_path),
            "--generated-at",
            AUDIT_TIME,
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == integrity.EXIT_INTEGRITY_FAIL
    assert captured.out.isascii()

    payload = json.loads(captured.out)

    assert payload["summary"]["integrity_ok"] is False
    assert integrity.ISSUE_LEDGER_ENTRY_INVALID in {
        issue["code"] for issue in payload["issues"]
    }






