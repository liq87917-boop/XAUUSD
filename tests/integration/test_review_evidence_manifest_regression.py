"""GOLD-043 裁决证据包的**真实仓库**端到端回归（只读）。

覆盖：

1. manifest 覆盖 ``last_reviewed_task`` 之后的**完整 backlog**（与 §2.11
   ``review_backlog`` 的 backlog 边界完全一致），逐项身份可由测试**独立复算**
   （raw ``git`` blob / diff-tree）；
2. 已知历史矛盾集合（GOLD-028 / 031 / 035 / 038 / 039 / 040 / 041）逐项可被证据包
   识别为 ``needs-gpt-adjudication``，且原始 contradiction reason code 显式可见；
3. 普通 complete 项为 ``pending-substantive-review``；没有真实 GPT 裁决时
   ``facts-ready`` / ``adjudicated`` 计数为 0，且 **绝不是** GPT PASS；
4. 证据工具对真实仓库**零写入**：``.ai/results`` 全树摘要、``PROJECT_STATE``、
   ``GPT_REVIEW_LEDGER`` 与裁决 store 前后完全不变（原 result 逐字节不变）；
5. CLI 输出确定性、纯 ASCII JSON；不弱化 §2.13 / §2.8 的 fail-closed 暴露；
   Phase 3.3 blocker / L3/L4 / 交易安全开关全部不变。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import result_terminal_consistency as terminal
from orchestrator import review_backlog as backlog
from orchestrator import review_binding
from orchestrator import review_evidence_manifest as evidence

REPO_ROOT = Path(__file__).resolve().parents[2]

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

STORE_PATH = REPO_ROOT / adjudication.ADJUDICATION_STORE_RELATIVE_PATH

LEGACY_CODE = terminal.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS

GUARDED_PATHS = (
    REPO_ROOT / ".ai" / "PROJECT_STATE.json",
    REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json",
)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    """``.ai/results`` 全树（文件名 + 原始字节）确定性摘要。"""

    digest = hashlib.sha256()

    for path in sorted(root.rglob("*.json")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))

        digest.update(path.read_bytes())

    return digest.hexdigest()


def guarded_snapshot() -> dict[str, str]:
    return {
        str(path): (file_digest(path) if path.exists() else "missing") for path in GUARDED_PATHS
    }


def git_blob_sha256(commit: str, relative_path: str) -> str:
    """测试**独立**复算 canonical 内容身份：raw ``git`` blob 字节 sha256。"""

    listing = subprocess.run(
        ["git", "ls-tree", "-z", commit, "--", relative_path],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
    ).stdout

    blob_id = listing.split(b"\x00")[0].split(b"\t", 1)[0].split()[2].decode("ascii")

    content = subprocess.run(
        ["git", "cat-file", "blob", blob_id],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
    ).stdout

    return hashlib.sha256(content).hexdigest()


def git_changed_paths(commit: str) -> list[str]:
    """测试**独立**复算 changed paths（用 diff-tree 而非 log --name-only）。"""

    out = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")

    return sorted({line.strip() for line in out.splitlines() if line.strip()})


_MANIFEST_CACHE: dict[str, dict[str, Any]] = {}

_BINDING_CACHE: dict[str, dict[str, Any]] = {}


def build_manifest(generated_at: str = "2026-09-24T00:00:00+08:00") -> dict[str, Any]:
    """构建（同一 ``generated_at`` 只跑一次真实 Git：测试只读、结果确定）。"""

    if generated_at in _MANIFEST_CACHE:
        return _MANIFEST_CACHE[generated_at]

    payload = evidence.build_review_evidence_manifest(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        state_path=REPO_ROOT / ".ai" / "PROJECT_STATE.json",
        ledger_path=REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json",
        adjudication_store_path=STORE_PATH,
        generated_at=generated_at,
    )

    _MANIFEST_CACHE[generated_at] = payload

    return payload


def items_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["task_id"]: item for item in payload["items"]}


def binding_manifest(task_id: str) -> dict[str, Any]:
    """§2.8 manifest（缓存；测试只读）。"""

    if task_id not in _BINDING_CACHE:
        _BINDING_CACHE[task_id] = review_binding.build_review_binding_manifest(
            task_id, root=REPO_ROOT, tasks_dir=TASKS_DIR, results_dir=RESULTS_DIR
        )

    return _BINDING_CACHE[task_id]


# ============================================================
# 1. backlog 覆盖范围 / 分类（与 §2.11 同一口径）
# ============================================================


def test_backlog_scope_matches_review_backlog() -> None:
    payload = build_manifest()

    reference = backlog.build_review_backlog_manifest(
        root=REPO_ROOT,
        tasks_dir=TASKS_DIR,
        results_dir=RESULTS_DIR,
        state_path=REPO_ROOT / ".ai" / "PROJECT_STATE.json",
        ledger_path=REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json",
        generated_at="2026-09-24T00:00:00+08:00",
    )

    assert [item["task_id"] for item in payload["items"]] == [
        entry["task_id"] for entry in reference["backlog"]
    ]

    assert payload["coverage"]["last_reviewed_task_pointer"] == "GOLD-027"


def test_known_contradictions_are_needs_gpt_adjudication() -> None:
    payload = build_manifest()

    items = items_by_id(payload)

    assert payload["coverage"]["known_contradiction_tasks_in_backlog"] == list(
        adjudication.KNOWN_CONTRADICTION_TASKS
    )

    for task_id in adjudication.KNOWN_CONTRADICTION_TASKS:
        item = items[task_id]

        assert item["classification"] == evidence.CLASSIFICATION_NEEDS_GPT_ADJUDICATION

        assert item["terminal_consistency"]["contradiction"] is True

        assert item["terminal_consistency"]["reason_codes"] == [LEGACY_CODE]

        assert LEGACY_CODE in item["missing_reason_codes"]

        assert item["adjudication"]["state"] == adjudication.TASK_STATE_UNADJUDICATED

    assert payload["summary"]["needs_gpt_adjudication_count"] == len(
        adjudication.KNOWN_CONTRADICTION_TASKS
    )


def test_normal_completed_items_are_pending_substantive_review() -> None:
    payload = build_manifest()

    pending = [
        item
        for item in payload["items"]
        if item["classification"] == evidence.CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW
    ]

    assert pending

    for item in pending:
        assert item["facts_complete"] is True
        assert item["terminal_consistency"]["contradiction"] is False

    assert payload["summary"]["invalid_count"] == 1
    assert payload["summary"]["facts_ready_count"] == 0
    assert payload["summary"]["adjudicated_count"] == 0
    assert payload["summary"]["exit_code"] == evidence.EXIT_FAIL_CLOSED


# ============================================================
# 2. 身份可由测试独立复算（无第二套 result / commit 身份算法）
# ============================================================


@pytest.mark.parametrize(
    "task_id",
    [*adjudication.KNOWN_CONTRADICTION_TASKS, "GOLD-029", "GOLD-042"],
)
def test_item_identity_is_independently_recomputable(task_id: str) -> None:
    payload = build_manifest()

    item = items_by_id(payload)[task_id]

    manifest = binding_manifest(task_id)

    # 同一口径：evidence manifest 的 result / commit / validation 事实 == §2.8 manifest。
    assert item["result"]["sha256"] == manifest["result"]["sha256"]
    assert item["result"]["status"] == manifest["result"]["status"]
    assert item["commit"]["sha"] == manifest["commit"]["sha"]
    assert item["commit"]["branch"] == manifest["commit"]["branch"]
    assert item["commit"]["subject"] == manifest["commit"]["subject"]
    assert item["validation"]["return_codes"] == manifest["validation"]["results"]

    # 独立复算：canonical sha256 == raw git blob 字节摘要。
    assert item["result"]["sha256"] == git_blob_sha256(
        str(item["commit"]["sha"]), f".ai/results/{task_id}.json"
    )

    # 独立复算：changed paths == raw git diff-tree。
    assert item["commit"]["changed_paths"] == git_changed_paths(str(item["commit"]["sha"]))

    assert item["commit"]["changed_paths_reason_code"] is None


def test_facts_digest_is_deterministic_and_excludes_wall_clock() -> None:
    first = build_manifest("2026-09-24T00:00:00+08:00")

    results_before = tree_digest(RESULTS_DIR)

    guarded_before = guarded_snapshot()

    second = build_manifest("2026-12-31T23:59:59+08:00")

    assert first["generated_at"] != second["generated_at"]
    assert first["facts_digest"] == second["facts_digest"]

    # wall-clock 字段不参与内容身份。
    assert "generated_at" not in evidence.evidence_facts(first)

    # 真实 Git 重算零写入。
    assert tree_digest(RESULTS_DIR) == results_before
    assert guarded_snapshot() == guarded_before


# ============================================================
# 3. 零写入 / fail-closed 暴露不变 / CLI
# ============================================================


def run_cli() -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = evidence.main(
            ["--root", str(REPO_ROOT), "--generated-at", "2026-09-24T00:00:00+08:00"]
        )

    return code, out.getvalue()


def test_tool_never_writes_repo_state() -> None:
    results_before = tree_digest(RESULTS_DIR)

    guarded_before = guarded_snapshot()

    store_existed = STORE_PATH.exists()

    build_manifest()

    assert tree_digest(RESULTS_DIR) == results_before
    assert guarded_snapshot() == guarded_before
    assert STORE_PATH.exists() == store_existed


def test_review_binding_fail_closed_exposure_is_unchanged() -> None:
    """GOLD-043 不弱化 §2.13 / §2.8：矛盾 result 的 facts_complete 仍为 ``false``。"""

    manifest = binding_manifest("GOLD-035")

    codes = {issue["code"] for issue in manifest["issues"]}

    assert LEGACY_CODE in codes
    assert manifest["binding"]["facts_complete"] is False


def test_executor_did_not_create_a_real_adjudication_store() -> None:
    payload = build_manifest()

    assert STORE_PATH.exists() is False
    assert payload["adjudication"]["present"] is False
    assert payload["summary"]["adjudicated_count"] == 0
    assert payload["summary"]["facts_ready_count"] == 0


def test_cli_is_deterministic_ascii_and_read_only() -> None:
    results_before = tree_digest(RESULTS_DIR)

    guarded_before = guarded_snapshot()

    first = run_cli()

    second = run_cli()

    assert first == second

    code, stdout = first

    assert code == evidence.EXIT_FAIL_CLOSED
    assert stdout.isascii() is True

    payload = json.loads(stdout)

    assert payload["schema"] == evidence.SCHEMA
    assert payload["schema_version"] == evidence.SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["coverage"]["last_reviewed_task_pointer"] == "GOLD-027"
    assert payload["summary"]["needs_gpt_adjudication_count"] == len(
        adjudication.KNOWN_CONTRADICTION_TASKS
    )

    for key in evidence.FORBIDDEN_MANIFEST_KEYS:
        assert key not in payload

    assert tree_digest(RESULTS_DIR) == results_before
    assert guarded_snapshot() == guarded_before


def test_cli_output_rejects_results_path() -> None:
    target = RESULTS_DIR / "GOLD-999.json"

    missing_state = REPO_ROOT / ".ai" / "MISSING_PROJECT_STATE.json"

    out, err = io.StringIO(), io.StringIO()

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = evidence.main(
            [
                "--root",
                str(REPO_ROOT),
                "--state",
                str(missing_state),
                "--output",
                str(target),
            ]
        )

    assert code == evidence.snapshot_output.EXIT_OUTPUT_REJECTED
    assert target.exists() is False
    assert evidence.snapshot_output.ISSUE_OUTPUT_PATH_REJECTED in err.getvalue()


def test_safety_invariants_and_pointers_unchanged() -> None:
    state = json.loads(
        (REPO_ROOT / ".ai" / "PROJECT_STATE.json").read_text(encoding="utf-8")
    )

    blocker_codes = {blocker["code"] for blocker in state["blockers"]}

    assert "PHASE3_3_DATA" in blocker_codes
    assert state["status"] == "BLOCKED"
    assert state["last_reviewed_task"] == "GOLD-027"
    assert "LIVE_TRADING=false" in state["invariants"]
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]
