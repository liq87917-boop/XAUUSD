"""真实仓库上的终态一致性门禁回归（GOLD-039）。

为什么放在 integration
----------------------
- 只有真实语料（``.ai/results/*.json`` 的 40+ 份历史 result）才能证明：门禁对
  **GOLD-035 这一类历史矛盾**给出稳定 reason code，而对自洽结果（例如正式台账
  已绑定的 GOLD-025~027）绝不误报；
- 测试**自己**按协议口径复算「历史终态矛盾」，不 import 被测模块的判定；
- 同时证明命令默认**零写入**：``.ai/tasks`` / ``.ai/results`` / ``PROJECT_STATE`` /
  ``GPT_REVIEW_LEDGER`` 与工作树状态在命令前后完全不变。

红线：本文件只读 —— 绝不写 ``.ai/**``，绝不 commit / push / reset / checkout。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import control_plane_invariants
from orchestrator import result_terminal_consistency as terminal
from orchestrator import review_binding as binding

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_DIR = REPO_ROOT / ".ai" / "tasks"

RESULTS_DIR = REPO_ROOT / ".ai" / "results"

PROJECT_STATE = REPO_ROOT / ".ai" / "PROJECT_STATE.json"

REVIEW_LEDGER = REPO_ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

AUDIT_TIME = "2026-09-23T00:00:00+08:00"

LEGACY_CONTRADICTION_CODE = "RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS"

NORMALIZED_MARKER_KEYS = (
    "execution_outcome",
    "normalized_finish_reason",
    "cline_finish_reason_raw",
)


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


def result_payloads() -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}

    for path in sorted(RESULTS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert isinstance(payload, dict), path

        payloads[path.stem] = payload

    return payloads


def independent_legacy_contradiction(payload: dict[str, Any]) -> bool:
    """测试自己按 §2.2 / GOLD-039 口径复算「历史 result 终态矛盾」。"""

    if any(key in payload for key in NORMALIZED_MARKER_KEYS):
        return False

    attempts = payload.get("attempts")

    if not isinstance(attempts, list) or not attempts:
        return False

    final = attempts[-1]

    if not isinstance(final, dict) or any(key in final for key in NORMALIZED_MARKER_KEYS):
        return False

    if str(payload.get("status", "")).strip().lower() != "completed":
        return False

    raw = final.get("finish_reason")

    return isinstance(raw, str) and raw.strip() not in ("", "completed")


def independent_normalized_raw_contradiction(payload: dict[str, Any]) -> bool:
    """测试自己按 GOLD-046 口径复算「归一化格式的权威 raw 终态矛盾」。"""

    if not any(key in payload for key in NORMALIZED_MARKER_KEYS):
        return False

    attempts = payload.get("attempts")

    if not isinstance(attempts, list) or not attempts:
        return False

    final = attempts[-1]

    if not isinstance(final, dict) or not any(key in final for key in NORMALIZED_MARKER_KEYS):
        return False

    if str(payload.get("status", "")).strip().lower() != "completed":
        return False

    raw = final.get("cline_finish_reason_raw")

    return not (isinstance(raw, str) and raw.strip().lower() == "completed")


def ledger_task_ids() -> list[str]:
    ledger = json.loads(REVIEW_LEDGER.read_text(encoding="utf-8"))

    return [
        str(entry["task_id"])
        for entry in ledger["entries"]
        if isinstance(entry, dict) and entry.get("task_id") is not None
    ]


# ============================================================
# 1. 真实语料：稳定 code 与独立复算一致（不误报、不漏报）
# ============================================================


def test_corpus_flags_match_independent_recomputation() -> None:
    payloads = result_payloads()

    assert payloads, "真实仓库应至少有历史 result"

    flagged: list[str] = []

    normalized_flagged: list[str] = []

    for task_id, payload in payloads.items():
        report = terminal.analyze_result_terminal_consistency(payload)

        module_flag = LEGACY_CONTRADICTION_CODE in report["reason_codes"]

        raw_flag = terminal.REASON_RAW_TERMINAL_CONTRADICTS_COMPLETED in report["reason_codes"]

        # 模块判定 == 测试自己按协议口径复算（两类矛盾各自独立复算）。
        assert module_flag is independent_legacy_contradiction(payload), task_id
        assert raw_flag is independent_normalized_raw_contradiction(payload), task_id
        assert report["consistent"] is (not (module_flag or raw_flag)), task_id

        if module_flag:
            flagged.append(task_id)

        if raw_flag:
            normalized_flagged.append(task_id)

    # GOLD-035 的真实矛盾必须被暴露（本任务的直接验收对象）
    assert "GOLD-035" in flagged

    # 仍未漏掉其它同类历史矛盾（GOLD-028 / GOLD-031 / GOLD-038）
    assert {"GOLD-028", "GOLD-031", "GOLD-038"} <= set(flagged)

    # GOLD-046：GOLD-044 的归一化矛盾（completed + raw aborted）必须被暴露。
    assert "GOLD-044" in normalized_flagged


def test_corpus_check_is_read_only_and_deterministic() -> None:
    before_results = tree_digest(RESULTS_DIR)

    before_tasks = tree_digest(TASKS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    first = {
        task_id: terminal.analyze_result_terminal_consistency(payload)
        for task_id, payload in result_payloads().items()
    }

    second = {
        task_id: terminal.analyze_result_terminal_consistency(payload)
        for task_id, payload in result_payloads().items()
    }

    assert first == second

    assert tree_digest(RESULTS_DIR) == before_results
    assert tree_digest(TASKS_DIR) == before_tasks
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger


def test_historical_contradiction_is_a_read_only_report_for_gold035() -> None:
    """GOLD-035：只报告矛盾，绝不改写历史 result，也绝不产生 Review 结论。"""

    path = RESULTS_DIR / "GOLD-035.json"

    payload = json.loads(path.read_text(encoding="utf-8"))

    attempt = payload["attempts"][-1]

    assert payload["status"] == "completed"
    assert attempt["finish_reason"] == "aborted"  # raw Cline 值落在唯一字段上
    assert "execution_outcome" not in attempt
    assert "cline_finish_reason_raw" not in attempt

    report = terminal.analyze_result_terminal_consistency(payload)

    assert report["consistent"] is False
    assert report["format"] == terminal.FORMAT_LEGACY
    assert report["reason_codes"] == [LEGACY_CONTRADICTION_CODE]

    assert json.loads(path.read_text(encoding="utf-8")) == payload


# ============================================================
# 2. 正式 Review 台账链不受影响（不误伤已评审任务）
# ============================================================


def test_formal_ledger_bound_tasks_stay_consistent() -> None:
    payloads = result_payloads()

    reviewed = ledger_task_ids()

    assert reviewed, "正式台账应至少有已绑定任务"

    for task_id in reviewed:
        payload = payloads[task_id]

        report = terminal.analyze_result_terminal_consistency(payload)

        assert report["consistent"] is True, (task_id, report["reason_codes"])


def test_review_ledger_integrity_still_passes_on_real_repository() -> None:
    """§2.9 台账完整性门禁必须仍然通过：新门禁不动台账、不改历史。"""

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    completed = subprocess.run(
        [sys.executable, "-m", "orchestrator.review_ledger_integrity"],
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    payload = json.loads(completed.stdout.decode("ascii"))

    assert payload["summary"]["integrity_ok"] is True
    assert completed.returncode == 0
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger


def test_review_binding_cli_exposes_contradiction_without_writing() -> None:
    """GPT review 入口对 GOLD-035 fail-closed，且命令前后零改写。"""

    command = [
        sys.executable,
        "-m",
        "orchestrator.review_binding",
        "--task",
        "GOLD-035",
        "--generated-at",
        AUDIT_TIME,
    ]

    before_results = tree_digest(RESULTS_DIR)

    before_state = PROJECT_STATE.read_bytes()

    before_ledger = REVIEW_LEDGER.read_bytes()

    before_status = worktree_status()

    first = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    second = subprocess.run(command, capture_output=True, cwd=str(REPO_ROOT), check=False)

    assert first.returncode == binding.EXIT_DRIFT
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr
    assert first.stdout.isascii()

    payload = json.loads(first.stdout.decode("ascii"))

    assert payload["binding"]["facts_complete"] is False
    assert payload["binding"]["reason_codes"] == [LEGACY_CONTRADICTION_CODE]

    assert tree_digest(RESULTS_DIR) == before_results
    assert PROJECT_STATE.read_bytes() == before_state
    assert REVIEW_LEDGER.read_bytes() == before_ledger
    assert worktree_status() == before_status


# ============================================================
# 3. Phase / 交易安全边界不变
# ============================================================


def test_phase33_blocker_and_trading_invariants_unchanged() -> None:
    for payload in result_payloads().values():
        terminal.analyze_result_terminal_consistency(payload)

    state = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))

    assert state["phase"] == "Phase 3"
    assert state["status"] == "BLOCKED"
    blocker_codes = {blocker["code"] for blocker in (state.get("blockers") or [])}
    history_codes = {blocker["code"] for blocker in (state.get("history_blockers") or [])}
    assert "PHASE3_3_DATA" in (blocker_codes | history_codes)
    assert "LIVE_TRADING=false" in state["invariants"]
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in state["invariants"]
    assert control_plane_invariants.queue_status_consistency_violations(state) == []
