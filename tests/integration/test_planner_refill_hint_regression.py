"""真实仓库上的 GOLD-035 Orchestrator refill 提示回归（只读）。

为什么放在 integration
----------------------
- 只有真实仓库才能证明 Orchestrator 报告的 `head` / `follow_on_count` / `deficit` /
  `reason_codes` 与 GOLD-034 事实包 CLI **完全一致**（单一事实来源，不产生第二套判断）；
- 同时证明整个提示链路在真实仓库上**零写入**：`.ai/tasks` / `.ai/results` /
  `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json` 逐字节不变，
  `git status --porcelain` 前后一致（镜像只落 runtime，且 runtime 已被 .gitignore 忽略）；
- 还证明生产启动方式（`py -u orchestrator/ai_orchestrator.py`，`sys.path[0]` 是
  `orchestrator/`）下只读模块仍可加载并产出提示。

红线：本文件只读 —— 绝不 commit / push / reset / checkout，绝不生成或追加任何 task，
绝不修改 PROJECT_STATE / review ledger；镜像断言只写 tmp_path。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_refill_request as refill

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"


def sha256_bytes(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def worktree_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace")


def cli_payload() -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchestrator.planner_refill_request",
            "--generated-at",
            AUDIT_TIME,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode in {0, 2}, completed.stderr.decode("utf-8", errors="replace")

    return json.loads(completed.stdout.decode("ascii"))


# ============================================================
# 1. 事实一致：Orchestrator 提示与 GOLD-034 CLI 同源
# ============================================================


def test_orchestrator_facts_match_refill_cli_on_real_repo() -> None:
    payload = orch.planner_refill_facts()

    assert payload is not None

    cli = cli_payload()

    assert payload["schema"] == refill.PLANNER_REFILL_REQUEST_SCHEMA
    assert payload["schema_version"] == refill.PLANNER_REFILL_REQUEST_SCHEMA_VERSION
    assert payload["facts_digest"] == cli["facts_digest"]
    assert payload["queue_head"] == cli["queue_head"]
    assert payload["follow_on_count"] == cli["follow_on_count"]
    assert payload["lookahead_target"] == cli["lookahead_target"]
    assert payload["deficit"] == cli["deficit"]
    assert payload["refill_required"] == cli["refill_required"]
    assert payload["reason_codes"] == cli["reason_codes"]

    # 低水位数学自洽：deficit 只是「少了几个已批准 follow-on」的计数事实
    assert payload["deficit"] == max(
        0, int(payload["lookahead_target"]) - int(payload["follow_on_count"])
    )
    assert payload["refill_required"] == (payload["deficit"] > 0)

    # 提示 = 事实 + 只读权限声明；绝不含任何后续任务内容
    line = orch.planner_refill_hint_line(
        payload, context=orch.REFILL_HINT_CONTEXT_POST_PUSH
    )

    expected_code = (
        orch.PLANNER_REFILL_REQUIRED_CODE
        if payload["refill_required"]
        else orch.PLANNER_REFILL_SATISFIED_CODE
    )

    assert line.startswith(expected_code)
    assert f"head={payload['queue_head']}" in line
    assert f"follow_on_count={payload['follow_on_count']}" in line
    assert f"target={payload['lookahead_target']}" in line
    assert f"deficit={payload['deficit']}" in line
    assert "executor_can_refill=false" in line

    assert payload["planner_authority"]["executor_can_refill"] is False
    assert payload["planner_authority"]["planning_authority"] == "gpt_only"


def test_real_repo_facts_report_zero_writes_and_controlled_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks_before = tree_digest(TASKS_DIR)
    results_before = tree_digest(RESULTS_DIR)
    state_before = sha256_bytes(PROJECT_STATE)
    ledger_before = sha256_bytes(REVIEW_LEDGER)
    worktree_before = worktree_status()

    # 镜像改写路径只指向 tmp（真实 runtime 不落文件）
    monkeypatch.setattr(orch, "RUNTIME_DIR", tmp_path)

    with caplog.at_level(logging.INFO, logger="ai_orchestrator"):
        report = orch.planner_refill_report(
            context=orch.REFILL_HINT_CONTEXT_IDLE,
            throttle=False,
        )

    assert report["emitted"] is True
    assert report["line"] == orch.planner_refill_hint_line(
        report["payload"], context=orch.REFILL_HINT_CONTEXT_IDLE
    )

    mirrored = tmp_path / orch.REFILL_REQUEST_RUNTIME_NAME

    assert mirrored.is_file()

    payload = json.loads(mirrored.read_text(encoding="utf-8"))

    assert payload["read_only"] is True
    assert payload["planner_authority"]["executor_can_refill"] is False

    # 零写入：tasks / results / PROJECT_STATE / review ledger / 工作树逐字节一致
    assert tree_digest(TASKS_DIR) == tasks_before
    assert tree_digest(RESULTS_DIR) == results_before
    assert sha256_bytes(PROJECT_STATE) == state_before
    assert sha256_bytes(REVIEW_LEDGER) == ledger_before
    assert worktree_status() == worktree_before


def test_default_mirror_target_is_runtime_guard_approved() -> None:
    """生产镜像目标必须落在 `<root>/.ai/runtime/**`，且被 fail-closed 守卫接受。"""

    snapshot_output = orch.repo_scoped_import("orchestrator.planner_snapshot_output")

    assert snapshot_output is not None

    target = REPO_ROOT / ".ai" / "runtime" / orch.REFILL_REQUEST_RUNTIME_NAME

    resolved, reason = snapshot_output.resolve_output_target(REPO_ROOT, target)

    assert reason is None
    assert resolved == target.resolve()

    # 相对仓库根的 runtime 子路径正是 `.ai/runtime`
    assert target.parent == REPO_ROOT / ".ai" / "runtime"


# ============================================================
# 2. 生产启动方式（脚本直跑）下只读模块仍可加载
# ============================================================

SCRIPT_LAUNCH_PROBE = r"""
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path.cwd().resolve()
orchestrator_dir = root / "orchestrator"

# 模拟生产启动：sys.path[0] = orchestrator/，仓库根**不在** sys.path 上
sys.path[:] = [
    entry for entry in sys.path if entry and pathlib.Path(entry).resolve() != root
]
sys.path.insert(0, str(orchestrator_dir))

plain_import = True

try:
    import orchestrator  # noqa: F401
except Exception:
    plain_import = False

spec = importlib.util.spec_from_file_location(
    "orchestrator_launch_probe", str(orchestrator_dir / "ai_orchestrator.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

facts = module.planner_refill_facts()
line = module.planner_refill_hint_line(
    facts, context=module.REFILL_HINT_CONTEXT_POST_PUSH
)

print(
    json.dumps(
        {
            "plain_import": plain_import,
            "facts_ok": isinstance(facts, dict),
            "hint_code": line.split(" ")[0],
            "head_present": "head=" in line,
            "sys_path_restored": str(root) not in sys.path,
        }
    )
)
"""


def test_script_launch_shape_can_load_readonly_modules() -> None:
    """`py -u orchestrator/ai_orchestrator.py`（sys.path[0] = orchestrator/）也能出提示。"""

    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SCRIPT_LAUNCH_PROBE],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")

    payload = json.loads(completed.stdout.decode("utf-8"))

    # 该启动形态下 `import orchestrator` 确实不可用（否则本测试没有证明力）
    assert payload["plain_import"] is False
    assert payload["facts_ok"] is True
    assert payload["hint_code"] in {
        orch.PLANNER_REFILL_REQUIRED_CODE,
        orch.PLANNER_REFILL_SATISFIED_CODE,
    }
    assert payload["head_present"] is True
    assert payload["sys_path_restored"] is True


def test_main_loop_threads_refill_hint_state() -> None:
    """main loop 必须把 `refill_hint_state` 跨轮传递（否则每 20 秒刷屏）。"""

    source = Path(orch.__file__).read_text(encoding="utf-8")

    normalized = " ".join(source.split())

    assert "refill_hint_state = step.get(" in source
    assert "refill_hint_state = planner_refill_idle_hint(" in source
    assert "context=REFILL_HINT_CONTEXT_POST_PUSH" in source
    assert "step = run_iteration( last_idle_log_at, refill_hint_state )" in normalized

    assert orch.REFILL_HINT_SECONDS >= orch.POLL_SECONDS
    assert orch.REFILL_REQUEST_RUNTIME_NAME == "planner_refill_request.json"
