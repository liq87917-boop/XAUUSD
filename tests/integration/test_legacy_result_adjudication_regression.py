"""GOLD-042 历史矛盾 GPT 裁决契约的**真实仓库**端到端回归（只读）。

覆盖：

1. 当前已知矛盾集合（GOLD-028 / 031 / 035 / 038 / 039 / 040 / 041）的真实 result
   确实处于 §2.13 legacy 矛盾终态，且内容身份可由测试**独立复算**（raw ``git`` blob）；
2. 在没有真实 GPT 裁决时，validator 一律 ``unadjudicated`` + fail-closed，且原始
   contradiction reason code 继续显式可见；
3. validator / CLI 对真实仓库**零写入**：``.ai/results`` 全树摘要、``PROJECT_STATE``、
   ``GPT_REVIEW_LEDGER`` 与裁决 store 前后完全不变（原 result 逐字节不变）；
4. CLI 输出确定性、纯 ASCII JSON；不因本工具而弱化 §2.13 / §2.8 的 fail-closed 暴露。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import result_terminal_consistency as terminal

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
        str(path): (file_digest(path) if path.exists() else "missing")
        for path in GUARDED_PATHS
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


def live_facts_for(task_id: str) -> dict[str, object]:
    return adjudication.review_binding_live_facts(
        task_id, root=REPO_ROOT, tasks_dir=TASKS_DIR, results_dir=RESULTS_DIR
    )


@pytest.mark.parametrize("task_id", adjudication.KNOWN_CONTRADICTION_TASKS)
def test_real_known_contradiction_is_bound_and_untouched(task_id: str) -> None:
    result_path = RESULTS_DIR / f"{task_id}.json"

    assert result_path.exists() is True

    before = result_path.read_bytes()

    live = live_facts_for(task_id)

    assert live["commit_resolved"] is True
    assert live["result_status"] == "completed"

    # 身份独立复算：canonical sha256 = Git 存储字节摘要（测试自己用 raw git 复算）。
    assert live["result_sha256"] == git_blob_sha256(
        str(live["commit_sha"]), f".ai/results/{task_id}.json"
    )

    store_existed = STORE_PATH.exists()

    report = adjudication.build_adjudication_report(
        [task_id], store_path=STORE_PATH, live_facts={task_id: live}
    )

    view = report["tasks"][0]

    assert view["task_id"] == task_id
    assert view["original_contradiction"]["contradiction"] is True
    assert view["original_contradiction"]["format"] == "legacy"
    assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]
    assert view["live_identity"]["result_sha256"] == live["result_sha256"]
    assert view["live_identity"]["commit_sha"] == live["commit_sha"]
    assert view["adjudication"] is None
    assert report["summary"]["fail_closed"] is True
    assert report["summary"]["exit_code"] == adjudication.EXIT_FAIL_CLOSED

    if not store_existed:
        assert view["state"] == adjudication.TASK_STATE_UNADJUDICATED

    # 原 result 逐字节不变 + 裁决 store 绝不被创建 / 改写。
    assert result_path.read_bytes() == before
    assert STORE_PATH.exists() == store_existed


def test_validator_never_writes_repo_state_across_known_set() -> None:
    results_before = tree_digest(RESULTS_DIR)

    guarded_before = guarded_snapshot()

    store_existed = STORE_PATH.exists()

    live = {
        task_id: live_facts_for(task_id) for task_id in adjudication.KNOWN_CONTRADICTION_TASKS
    }

    report = adjudication.build_adjudication_report(
        adjudication.KNOWN_CONTRADICTION_TASKS, store_path=STORE_PATH, live_facts=live
    )

    summary = report["summary"]

    assert summary["task_count"] == len(adjudication.KNOWN_CONTRADICTION_TASKS)
    assert summary["contradiction_count"] == len(adjudication.KNOWN_CONTRADICTION_TASKS)
    assert summary["invalid_count"] == 0
    assert summary["fail_closed"] is True

    for view in report["tasks"]:
        assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]

    if not store_existed:
        assert summary["adjudicated_count"] == 0
        assert summary["unadjudicated_count"] == len(adjudication.KNOWN_CONTRADICTION_TASKS)

    assert tree_digest(RESULTS_DIR) == results_before
    assert guarded_snapshot() == guarded_before
    assert STORE_PATH.exists() == store_existed


def test_cli_is_deterministic_ascii_and_read_only_on_real_repo() -> None:
    results_before = tree_digest(RESULTS_DIR)

    guarded_before = guarded_snapshot()

    store_existed = STORE_PATH.exists()

    def run_cli() -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = adjudication.main(["--task", "GOLD-035", "--task", "GOLD-028"])

        return code, out.getvalue()

    first = run_cli()

    second = run_cli()

    assert first == second

    code, stdout = first

    assert code == adjudication.EXIT_FAIL_CLOSED
    assert stdout.isascii() is True

    report = json.loads(stdout)

    assert report["schema"] == adjudication.SCHEMA
    assert report["schema_version"] == adjudication.SCHEMA_VERSION
    assert report["read_only"] is True
    assert report["store"]["present"] == store_existed
    assert [view["task_id"] for view in report["tasks"]] == ["GOLD-028", "GOLD-035"]

    for view in report["tasks"]:
        assert view["original_contradiction"]["reason_codes"] == [LEGACY_CODE]

    assert tree_digest(RESULTS_DIR) == results_before
    assert guarded_snapshot() == guarded_before
    assert STORE_PATH.exists() == store_existed


def test_review_binding_fail_closed_exposure_is_unchanged() -> None:
    """GOLD-042 不弱化 §2.13 / §2.8：矛盾 result 的 facts_complete 仍为 ``false``。"""

    from orchestrator import review_binding

    manifest = review_binding.build_review_binding_manifest(
        "GOLD-035", root=REPO_ROOT, tasks_dir=TASKS_DIR, results_dir=RESULTS_DIR
    )

    codes = {issue["code"] for issue in manifest["issues"]}

    assert LEGACY_CODE in codes
    assert manifest["binding"]["facts_complete"] is False


def test_executor_did_not_create_a_real_adjudication_store() -> None:
    """Executor 只实现契约：绝不创建真实 GPT 裁决 / 不写裁决 store。"""

    assert STORE_PATH.exists() is False
    assert (REPO_ROOT / ".ai" / "adjudications").exists() is False
