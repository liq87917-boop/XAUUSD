"""结果终态控制面的**新进程端到端** canary（GOLD-047）。

为什么需要它
------------
GOLD-039 建立终态一致性门禁，GOLD-046 把「成功判定」与「completion commit」统一到
**权威 raw 终态**上。但这两次修复都只在**测试进程内**被验证：同进程 monkeypatch、
旧模块缓存、宽松 mock 与 fixture 终态改写都可能让「纯函数测试通过」掩盖**真实收尾
路径并未生效**——GOLD-044 的 `completed` + `cline_finish_reason_raw=aborted` 正是这种
回归。本模块把「终态解析 → 归一化 → terminal-consistency → result 持久化决策 →
completion commit 决策」的**真实入口链**放进一个**独立子进程**跑两个自洽场景：

- ``raw completed``：exit code 0 + validation 全通过 + 权威 raw ``completed``
  ⇒ completed result + completion commit（**正常路径必须保持可用**）；
- ``raw aborted``：exit code 0 + validation 全通过 + 工作树有变更，但权威 raw
  ``aborted`` ⇒ 只允许 ``failed`` attempt + ``blocked`` 顶层语义，**不得**产生
  completed result、completion commit 或完成推进。

它回答的是「新进程加载的**真模块**是否已按修复后的契约工作」，而不是「某个纯函数
是否还自洽」。

边界（不可协商）
----------------
- 只在**临时 workdir** 内写 task / result / runtime；绝不触碰真实 ``.ai/tasks`` /
  ``.ai/results`` / ``.ai/PROJECT_STATE.json`` / ``GPT_REVIEW_LEDGER`` / adjudication store；
- 绝不 spawn 真实 Cline CLI、绝不 commit / push / reset 真实仓库、绝不访问网络：
  所有 ``git`` 调用必须落在 :class:`RecordingGit` 白名单内，否则 fail-closed；
- 绝不替换终态链上的入口（``attempt_outcome`` / ``normalize_finish_reason`` /
  ``build_attempt_record`` / ``build_final_result`` / ``ensure_result_terminal_consistency``
  / ``ensure_completion_terminal_consistency`` / ``write_final_result`` /
  ``commit_task_result`` / ``cline_terminal_is_success`` / ``authoritative_cline_terminal``）
  ——只替换进程外的 ``run_cline`` / ``run_validations`` / ``get_changed_files`` /
  ``get_diff_stat`` / ``git`` 与只读 refill 提示；
- 只读报告：不签发 review verdict、不写 ledger / state、不改变 Phase 与交易安全不变量；
- 判定规则的**唯一来源**仍是 ``orchestrator.result_terminal_consistency``
  （``raw_terminal_is_success`` / ``REASON_CODES``）；本模块绝不复制第二套词表。

用法::

    python -m orchestrator.result_terminal_canary [workdir]

退出码：``0`` PASS / ``1`` FAIL（fail-closed）。报告是 **ASCII** JSON。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "gold-ai/result-terminal-canary/v1"

EXIT_PASS = 0

EXIT_FAIL = 1

VERDICT_PASS = "PASS"

VERDICT_FAIL = "FAIL"

RAW_COMPLETED = "completed"

RAW_ABORTED = "aborted"

STATUS_COMPLETED = "completed"

STATUS_BLOCKED = "blocked"

OUTCOME_COMPLETED = "completed"

OUTCOME_FAILED = "failed"

TERMINAL_OUTCOMES = (STATUS_COMPLETED, STATUS_BLOCKED)

PIPELINE_ENTRY = "orchestrator.ai_orchestrator.process_task"

# canary 里假 git 的稳定 HEAD / 分支（与真实仓库无关，只用于机器可读比对）。
CANARY_HEAD = "a" * 40

CANARY_BRANCH = os.getenv("AI_BRANCH", "cline-agent").strip() or "cline-agent"

# 假 changed file：只存在于 canary 的临时 workdir 内，绝不落到真实仓库。
PROBE_RELATIVE = "orchestrator/result_terminal_canary_probe.py"

PROBE_DIFF_STAT = f" {PROBE_RELATIVE} | 1 +\n 1 file changed, 1 insertion(+)\n"

CANARY_TASK_IDS = {
    "raw_completed": "GOLD-CANARY-100",
    "raw_aborted": "GOLD-CANARY-200",
}

CANARY_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "name": "raw_completed",
        "raw": RAW_COMPLETED,
        "task_id": CANARY_TASK_IDS["raw_completed"],
        "expected_attempt_outcome": OUTCOME_COMPLETED,
        "expected_result_status": STATUS_COMPLETED,
        "expect_completion_commit": True,
    },
    {
        "name": "raw_aborted",
        "raw": RAW_ABORTED,
        "task_id": CANARY_TASK_IDS["raw_aborted"],
        "expected_attempt_outcome": OUTCOME_FAILED,
        "expected_result_status": STATUS_BLOCKED,
        "expect_completion_commit": False,
    },
)

# 新进程边界必须**全新加载**的模块：任何一个已在 ``sys.modules`` 里都说明这不是
# 一次干净的新进程加载（fail-closed，稳定 reason code）。
FRESH_BOUNDARY_MODULES = (
    "orchestrator.ai_orchestrator",
    "orchestrator.result_terminal_consistency",
)


# ---------- 稳定 reason code（供 GPT / 人工 grep 与测试断言，绝不随文案变化） ----------

RESULT_TERMINAL_CANARY_INTERNAL_ERROR = "RESULT_TERMINAL_CANARY_INTERNAL_ERROR"

RESULT_TERMINAL_CANARY_RESULT_MISSING = "RESULT_TERMINAL_CANARY_RESULT_MISSING"

RESULT_TERMINAL_CANARY_RESULT_UNREADABLE = "RESULT_TERMINAL_CANARY_RESULT_UNREADABLE"

RESULT_TERMINAL_CANARY_ITERATION_NOT_TERMINAL = (
    "RESULT_TERMINAL_CANARY_ITERATION_NOT_TERMINAL"
)

RESULT_TERMINAL_CANARY_UNEXPECTED_GIT_COMMAND = (
    "RESULT_TERMINAL_CANARY_UNEXPECTED_GIT_COMMAND"
)

RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN = (
    "RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN"
)

RESULT_TERMINAL_CANARY_AUTHORITY_MODULE_NOT_LOADED = (
    "RESULT_TERMINAL_CANARY_AUTHORITY_MODULE_NOT_LOADED"
)

RESULT_TERMINAL_CANARY_AUTHORITY_RULE_INCONSISTENT = (
    "RESULT_TERMINAL_CANARY_AUTHORITY_RULE_INCONSISTENT"
)

RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED = (
    "RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED"
)

RESULT_TERMINAL_CANARY_FRESH_IMPORT_BOUNDARY_BROKEN = (
    "RESULT_TERMINAL_CANARY_FRESH_IMPORT_BOUNDARY_BROKEN"
)

RESULT_TERMINAL_CANARY_RAW_ABORTED_FIXTURE_DEGRADED = (
    "RESULT_TERMINAL_CANARY_RAW_ABORTED_FIXTURE_DEGRADED"
)

RESULT_TERMINAL_CANARY_RAW_ABORTED_ATTEMPT_NOT_FAILED = (
    "RESULT_TERMINAL_CANARY_RAW_ABORTED_ATTEMPT_NOT_FAILED"
)

RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED = (
    "RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED"
)

RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_RAW_EVIDENCE = (
    "RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_RAW_EVIDENCE"
)

RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT = (
    "RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT"
)

RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_COMPLETION_COMMIT = (
    "RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_COMPLETION_COMMIT"
)

REASON_CODES: tuple[str, ...] = (
    RESULT_TERMINAL_CANARY_INTERNAL_ERROR,
    RESULT_TERMINAL_CANARY_RESULT_MISSING,
    RESULT_TERMINAL_CANARY_RESULT_UNREADABLE,
    RESULT_TERMINAL_CANARY_ITERATION_NOT_TERMINAL,
    RESULT_TERMINAL_CANARY_UNEXPECTED_GIT_COMMAND,
    RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN,
    RESULT_TERMINAL_CANARY_AUTHORITY_MODULE_NOT_LOADED,
    RESULT_TERMINAL_CANARY_AUTHORITY_RULE_INCONSISTENT,
    RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED,
    RESULT_TERMINAL_CANARY_FRESH_IMPORT_BOUNDARY_BROKEN,
    RESULT_TERMINAL_CANARY_RAW_ABORTED_FIXTURE_DEGRADED,
    RESULT_TERMINAL_CANARY_RAW_ABORTED_ATTEMPT_NOT_FAILED,
    RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED,
    RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_RAW_EVIDENCE,
    RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT,
    RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_COMPLETION_COMMIT,
)


# ---------- 假 git：白名单 + 记录（绝不 spawn 真实命令） ----------

ALLOWED_GIT_PREFIXES: tuple[str, ...] = (
    "status --porcelain",
    "status --short",
    "rev-parse HEAD",
    "branch --show-current",
    "diff --stat",
    "diff --cached --stat",
    "add -A",
    "commit ",
    "rev-list --count",
    "push ",
    "reset --hard HEAD",
    "clean -fd",
)


class CanaryGitError(RuntimeError):
    """canary 的假 git 拒绝未知命令（**绝不**回落到真实进程）。"""


class RecordingGit:
    """只记录、只回放稳定输出的假 ``git``。

    - 白名单外的命令一律 raise :class:`CanaryGitError`（fail-closed，绝不真跑）；
    - ``commit_commands`` 给 completion commit 决策留下机器可读证据；
    - ``status --porcelain`` 恒为 clean（任务启动前工作区 clean 是 Orchestrator 前置），
      而 ``status --short`` 报告 canary 的 probe 文件，用来证明「工作树确实有变更」。
    """

    def __init__(self, head: str = CANARY_HEAD) -> None:
        self.commands: list[str] = []

        self._head = head

    @property
    def commit_commands(self) -> list[str]:
        return [command for command in self.commands if command.startswith("commit ")]

    def __call__(self, command: object, timeout: int = 120) -> dict[str, Any]:
        text = str(command).strip()

        self.commands.append(text)

        if not any(text.startswith(prefix) for prefix in ALLOWED_GIT_PREFIXES):
            raise CanaryGitError(f"canary must not run a real git command: {text}")

        return {
            "returncode": 0,
            "stdout": self._stdout_for(text),
            "stderr": "",
            "timed_out": False,
        }

    def _stdout_for(self, text: str) -> str:
        if text.startswith("status --short"):
            return f" M {PROBE_RELATIVE}\n"

        if text.startswith("rev-parse HEAD"):
            return self._head

        if text.startswith("diff --stat"):
            return PROBE_DIFF_STAT

        if text.startswith("rev-list --count"):
            return "1"

        if text.startswith("branch --show-current"):
            return CANARY_BRANCH

        return ""


def completion_commit_command(task_id: str) -> str:
    """completion commit 的**唯一**稳定形状（与 ``commit_task_result`` 同源）。"""

    return f'commit -m "ai: complete {task_id}"'


# ---------- 场景 fixture（全部落在临时 workdir 内） ----------


def write_canary_task(task_dir: Path, scenario: dict[str, Any]) -> Path:
    """写出与真实 task 同形状的最小 task 文件（文件名必须等于 task_id）。

    ``validation_commands`` 里放的是**永远不会被真实执行**的哨兵串：validations 走假
    runner；若替换失败而真跑，它会失败 ⇒ canary fail-closed，绝不产生假通过。
    """

    task_id = str(scenario["task_id"])

    task = {
        "task_id": task_id,
        "title": f"canary terminal scenario {scenario['name']}",
        "max_attempts": 1,
        "auto_start": False,
        "requires_human_approval": False,
        "human_gate": "L1",
        "validation_commands": ["canary-validation (never executed)"],
    }

    task_dir.mkdir(parents=True, exist_ok=True)

    task_file = task_dir / f"{task_id}.json"

    task_file.write_text(
        json.dumps(task, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return task_file


def raw_cline_stdout(scenario: dict[str, Any]) -> str:
    """构造 Cline CLI 事件流（由**真实** ``parse_cline_json_output`` 消费）。

    - 首行是非 JSON 噪声：证明解析对噪声容忍；
    - ``raw aborted`` 场景在 ``run_result`` 之后再补一个**不带 reason** 的
      ``agent_event.done``：证明「后覆盖先 + 不带 reason 不擦除」的权威终态选择在
      新进程里同样成立（``aborted`` 不会被擦成缺失）。
    """

    events: list[dict[str, Any]] = [
        {
            "type": "run_result",
            "finishReason": scenario["raw"],
            "text": "" if scenario["raw"] == RAW_ABORTED else "canary complete",
            "iterations": 144,
            "durationMs": 864377,
            "aggregateUsage": {"inputTokens": 1, "outputTokens": 1},
            "model": {"id": "canary-model"},
        }
    ]

    if scenario["raw"] == RAW_ABORTED:
        events.append({"type": "agent_event", "event": {"type": "done", "text": ""}})

    lines = ["canary noise line (not JSON)"]

    lines.extend(json.dumps(event, ensure_ascii=False) for event in events)

    return "\n".join(lines) + "\n"


# ---------- 事实读取（只读 result 文件，不 import 被测判定） ----------


def read_result_payload(result_file: Path | str) -> dict[str, Any] | None:
    """读取 result JSON 原文；缺失 / 损坏一律返回 ``None``（只报告，不猜测）。"""

    path = Path(result_file)

    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def _facts_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把终端 result 载荷投影成只读事实（只回显已记录事实，不推断终态）。"""

    attempts = payload.get("attempts")

    final: dict[str, Any] = {}

    if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict):
        final = attempts[-1]

    raw_validations = final.get("validations")

    returncodes: list[Any] | None = None

    if isinstance(raw_validations, list):
        returncodes = [
            item.get("returncode") for item in raw_validations if isinstance(item, dict)
        ]

    return {
        "result_present": True,
        "result_status": payload.get("status"),
        "result_execution_outcome": payload.get("execution_outcome"),
        "result_normalized_finish_reason": payload.get("normalized_finish_reason"),
        "result_changed_files": payload.get("changed_files"),
        "attempt_count": len(attempts) if isinstance(attempts, list) else None,
        "attempt_cline_exit_code": final.get("cline_exit_code"),
        "attempt_cline_timed_out": final.get("cline_timed_out"),
        "attempt_execution_outcome": final.get("execution_outcome"),
        "attempt_finish_reason": final.get("finish_reason"),
        "attempt_normalized_finish_reason": final.get("normalized_finish_reason"),
        "attempt_cline_finish_reason_raw": final.get("cline_finish_reason_raw"),
        "attempt_failure_class": final.get("failure_class"),
        "attempt_failure_code": final.get("failure_code"),
        "attempt_changed_files": final.get("changed_files"),
        "validations_returncodes": returncodes,
    }


def scenario_facts(result_file: Path | str) -> tuple[dict[str, Any] | None, bool]:
    """读取终态 result，抽取机器可读事实。

    返回 ``(facts, present)``：

    - ``present=False`` ⇒ result 文件缺失（caller 报 ``RESULT_MISSING``）；
    - ``present=True`` 且 ``facts=None`` ⇒ 文件存在但不可解析（``RESULT_UNREADABLE``）；
    - ``facts`` 只回显**已记录的事实**，不推断终态、不复制判定词表。
    """

    path = Path(result_file)

    if not path.exists():
        return None, False

    payload = read_result_payload(path)

    if payload is None:
        return None, True

    return _facts_from_payload(payload), True


# ---------- 纯判定：事实 → 稳定 reason codes ----------


def _token(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    text = value.strip().lower()

    return text or None


def _int_token(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dedupe(codes: list[str]) -> list[str]:
    unique: list[str] = []

    for code in codes:
        if code not in unique:
            unique.append(code)

    return unique


def validations_all_passed(returncodes: object) -> bool:
    """真实 attempt 记录的 validation return codes 是否**全部**为 0（空 = 未通过）。"""

    if not isinstance(returncodes, list) or not returncodes:
        return False

    return all(_int_token(code) == 0 for code in returncodes)


def produced_completed(facts: dict[str, Any]) -> bool:
    """任一「完成语义」痕迹出现即视为已经产出 completed（fail-closed 的指控依据）。"""

    return (
        _token(facts.get("result_status")) == STATUS_COMPLETED
        or _token(facts.get("result_execution_outcome")) == OUTCOME_COMPLETED
        or _token(facts.get("result_normalized_finish_reason")) == OUTCOME_COMPLETED
        or bool(facts.get("completion_commit_recorded"))
        or bool(facts.get("completion_gate_accepted"))
    )


def fixture_degraded(facts: dict[str, Any]) -> bool:
    """``raw aborted`` 场景是否**真的**是「exit 0 + validation 全过 + 有变更」。

    只有这种降级 fixture 才具备「按旧口径本可被误判为成功」的证明力；否则 canary 宁肯
    fail-closed（``FIXTURE_DEGRADED``），也绝不给出无意义的通过。
    """

    return (
        _int_token(facts.get("attempt_cline_exit_code")) == 0
        and bool(facts.get("validations_all_passed"))
        and bool(facts.get("attempt_changed_files"))
    )


def scenario_codes(facts: dict[str, Any]) -> list[str]:
    """纯函数：把一份事实判成稳定 reason codes（零 I/O、零 wall-clock、零随机）。"""

    raw = _token(facts.get("raw"))

    codes: list[str] = []

    if not facts.get("result_present"):
        codes.append(RESULT_TERMINAL_CANARY_RESULT_MISSING)

    if _token(facts.get("iteration_outcome")) not in TERMINAL_OUTCOMES:
        codes.append(RESULT_TERMINAL_CANARY_ITERATION_NOT_TERMINAL)

    if raw == RAW_ABORTED:
        if not fixture_degraded(facts):
            codes.append(RESULT_TERMINAL_CANARY_RAW_ABORTED_FIXTURE_DEGRADED)

        if _token(facts.get("attempt_cline_finish_reason_raw")) != RAW_ABORTED:
            codes.append(RESULT_TERMINAL_CANARY_RAW_ABORTED_RAW_REWRITTEN)

        if _token(facts.get("attempt_execution_outcome")) != OUTCOME_FAILED:
            codes.append(RESULT_TERMINAL_CANARY_RAW_ABORTED_ATTEMPT_NOT_FAILED)

        if produced_completed(facts):
            codes.append(RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED)

        if facts.get("completion_commit_recorded"):
            codes.append(RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETION_COMMIT)
    elif raw == RAW_COMPLETED:
        if (
            _token(facts.get("attempt_execution_outcome")) != OUTCOME_COMPLETED
            or _token(facts.get("result_status")) != STATUS_COMPLETED
        ):
            codes.append(RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED)

        if _token(facts.get("attempt_cline_finish_reason_raw")) != RAW_COMPLETED:
            codes.append(RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_RAW_EVIDENCE)

        if not facts.get("completion_commit_recorded"):
            codes.append(RESULT_TERMINAL_CANARY_RAW_COMPLETED_MISSING_COMPLETION_COMMIT)
    else:
        codes.append(RESULT_TERMINAL_CANARY_INTERNAL_ERROR)

    if bool(facts.get("completion_gate_accepted")) != bool(
        facts.get("expect_completion_commit")
    ):
        codes.append(
            RESULT_TERMINAL_CANARY_RAW_ABORTED_PRODUCED_COMPLETED
            if raw == RAW_ABORTED
            else RESULT_TERMINAL_CANARY_RAW_COMPLETED_NOT_COMPLETED
        )

    return _dedupe(codes)


# ---------- 场景执行：真实 process_task + 假进程边界 ----------


def _override(module: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    original: dict[str, Any] = {}

    for name, value in overrides.items():
        original[name] = getattr(module, name)

        setattr(module, name, value)

    return original


def _restore(module: Any, original: dict[str, Any]) -> None:
    for name, value in original.items():
        with contextlib.suppress(Exception):
            setattr(module, name, value)


def _no_refill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """只读 refill 提示的 no-op（与终态链无关；避免读真实仓库 / 写 runtime 镜像）。"""

    return {
        "payload": None,
        "signature": None,
        "emitted": False,
        "line": None,
        "emitted_at": None,
    }


def _authority_reason_code(message: str, authority: Any | None) -> str | None:
    """从门禁异常文案里取回**权威模块自己的**稳定 reason code（不复制词表）。"""

    codes = getattr(authority, "REASON_CODES", ()) if authority is not None else ()

    if not isinstance(codes, (list, tuple)):
        return None

    for code in codes:
        if isinstance(code, str) and code in message:
            return code

    return None


def probe_completion_gate(
    module: Any,
    authority: Any | None,
    task_file: Path,
    payload: dict[str, Any] | None,
    workdir: Path,
) -> tuple[bool, str | None]:
    """用**同一份事实**强制走 completion-commit 决策入口（GOLD-046 门禁）。

    这是「completion commit 决策入口真的在新进程里生效」的直接证据：

    - 入口缺失（修复不在新进程加载的模块里）⇒ ``(False, None)``；
    - 入口 raise（fail-closed）⇒ ``(False, <稳定 reason code>)``；
    - 入口接受 ⇒ ``(True, None)``。
    """

    gate = getattr(module, "ensure_completion_terminal_consistency", None)

    if gate is None:
        return False, None

    attempts = payload.get("attempts") if isinstance(payload, dict) else None

    if not isinstance(attempts, list):
        attempts = []

    try:
        task = module.load_task(task_file)
    except Exception:
        return False, None

    try:
        gate(
            task,
            STATUS_COMPLETED,
            attempts,
            changed_files=[f" M {PROBE_RELATIVE}"],
            diff_stat=PROBE_DIFF_STAT,
            execution_outcome=OUTCOME_COMPLETED,
        )
    except Exception as exc:
        return False, _authority_reason_code(str(exc), authority)

    return True, None


def run_scenario(
    module: Any,
    authority: Any | None,
    workdir: Path,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """在**新进程加载的真模块**上跑一个终态场景，返回机器可读事实 + reason codes。"""

    task_id = str(scenario["task_id"])

    task_dir = Path(module.TASK_DIR)

    result_dir = Path(module.RESULT_DIR)

    task_file = write_canary_task(task_dir, scenario)

    probe = workdir / PROBE_RELATIVE

    probe.parent.mkdir(parents=True, exist_ok=True)

    probe.write_text("# canary probe\n", encoding="utf-8")

    stdout = raw_cline_stdout(scenario)

    recorder = RecordingGit()

    def fake_run_cline(_task_file: Path) -> dict[str, Any]:
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
            # 真实解析器：终态事件选择由被测模块自己决定（canary 不代劳）。
            "summary": module.parse_cline_json_output(stdout),
        }

    def fake_run_validations(task: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "command": command,
                "returncode": 0,
                "timed_out": False,
                "stdout_tail": "",
                "stderr_tail": "",
            }
            for command in task.get("validation_commands", [])
        ]

    def fake_changed_files() -> list[str]:
        return [f" M {PROBE_RELATIVE}"]

    def fake_diff_stat() -> str:
        return PROBE_DIFF_STAT

    codes: list[str] = []

    iteration_outcome: Any = None

    original = _override(
        module,
        {
            "run_cline": fake_run_cline,
            "run_validations": fake_run_validations,
            "get_changed_files": fake_changed_files,
            "get_diff_stat": fake_diff_stat,
            "git": recorder,
            "planner_refill_report": _no_refill,
        },
    )

    try:
        iteration_outcome = module.process_task(task_file)
    except CanaryGitError:
        codes.append(RESULT_TERMINAL_CANARY_UNEXPECTED_GIT_COMMAND)
    except Exception:
        codes.append(RESULT_TERMINAL_CANARY_INTERNAL_ERROR)
    finally:
        _restore(module, original)

    result_file = result_dir / f"{task_id}.json"

    facts, present = scenario_facts(result_file)

    if not present:
        codes.append(RESULT_TERMINAL_CANARY_RESULT_MISSING)
    elif facts is None:
        codes.append(RESULT_TERMINAL_CANARY_RESULT_UNREADABLE)

    payload = read_result_payload(result_file)

    gate_accepted, gate_code = probe_completion_gate(
        module, authority, task_file, payload, workdir
    )

    merged: dict[str, Any] = dict(facts) if facts is not None else {"result_present": False}

    merged.update(
        {
            "name": scenario["name"],
            "raw": scenario["raw"],
            "task_id": task_id,
            "iteration_outcome": iteration_outcome,
            "pipeline_entry": PIPELINE_ENTRY,
            "commit_commands": recorder.commit_commands,
            "completion_commit_recorded": (
                completion_commit_command(task_id) in recorder.commit_commands
            ),
            "completion_gate_accepted": gate_accepted,
            "completion_gate_reason_code": gate_code,
            "expect_completion_commit": bool(scenario["expect_completion_commit"]),
        }
    )

    merged["validations_all_passed"] = validations_all_passed(
        merged.get("validations_returncodes")
    )

    merged["produced_completed"] = produced_completed(merged)

    codes.extend(scenario_codes(merged))

    merged["reason_codes"] = _dedupe(codes)

    return merged


# ---------- 新进程边界证明 ----------


def fresh_process_boundary_intact() -> bool:
    """新进程边界是否**干净**：未 import pytest，且边界模块此前都没被加载过。"""

    if "pytest" in sys.modules:
        return False

    return not any(name in sys.modules for name in FRESH_BOUNDARY_MODULES)


def loaded_boundary_files() -> list[str]:
    """本进程实际加载的边界模块源文件（证明新进程加载的是哪一份源码）。"""

    paths: list[str] = []

    for name in FRESH_BOUNDARY_MODULES:
        loaded = sys.modules.get(name)

        file = getattr(loaded, "__file__", None) if loaded is not None else None

        if isinstance(file, str) and file:
            paths.append(str(Path(file).resolve()))

    return sorted(paths)


def authority_rule_consistent(authority: Any | None) -> bool:
    """权威规则必须仍然严格：只有 ``completed`` 是成功收尾（复用唯一来源）。"""

    resolver = getattr(authority, "raw_terminal_is_success", None)

    if not callable(resolver):
        return False

    try:
        return (
            bool(resolver(RAW_COMPLETED)) is True
            and bool(resolver(RAW_ABORTED)) is False
            and bool(resolver(None)) is False
            and bool(resolver("")) is False
        )
    except Exception:
        return False


# ---------- 入口 ----------


def run_canary(workdir: Path | str | None = None) -> dict[str, Any]:
    """在临时 workdir 里跑完整 canary，返回机器可读报告。"""

    boundary_intact = fresh_process_boundary_intact()

    pytest_imported = "pytest" in sys.modules

    root = (
        Path(workdir).resolve()
        if workdir is not None
        else Path(tempfile.mkdtemp(prefix="gold-terminal-canary-"))
    )

    from orchestrator import ai_orchestrator as module

    authority: Any | None = None

    try:
        from orchestrator import result_terminal_consistency as authority_module

        authority = authority_module
    except Exception:
        authority = None

    ai_dir = root / ".ai"

    original = _override(
        module,
        {
            "ROOT": root,
            "TASK_DIR": ai_dir / "tasks",
            "RESULT_DIR": ai_dir / "results",
            "RUNTIME_DIR": ai_dir / "runtime",
            "LOG_DIR": ai_dir / "logs",
            "PROJECT_STATE_PATH": ai_dir / "PROJECT_STATE.json",
            "TASK_STATE_DIR": ai_dir / "runtime" / "tasks",
            "RECOVERY_DIR": ai_dir / "runtime" / "recovery",
            "RECOVERY_STATE_FILE": ai_dir / "runtime" / "recovery" / "recovery_state.json",
            "RECOVERY_LOCK_ARCHIVE_DIR": ai_dir / "runtime" / "recovery" / "locks",
            "LOCK_FILE": ai_dir / "runtime" / "orchestrator.lock",
            "LOG_FILE": ai_dir / "logs" / "orchestrator.log",
        },
    )

    # 只准备 canary 自己临时 workdir 内的目录（与 Orchestrator 的 ensure_directories 同形）。
    for directory in (
        Path(module.TASK_DIR),
        Path(module.RESULT_DIR),
        Path(module.LOG_DIR),
        Path(module.TASK_STATE_DIR),
        Path(module.RECOVERY_DIR),
    ):
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)

    scenarios: list[dict[str, Any]] = []

    codes: list[str] = []

    observed_task_dir = ai_dir / "tasks"

    observed_result_dir = ai_dir / "results"

    dirs_ok = False

    try:
        if authority is None:
            # 生产加载路径：与 Orchestrator 自己用的 repo_scoped_import 同一个入口。
            authority = module.repo_scoped_import(
                "orchestrator.result_terminal_consistency"
            )

        for scenario in CANARY_SCENARIOS:
            try:
                scenarios.append(run_scenario(module, authority, root, scenario))
            except Exception:
                scenarios.append(
                    {
                        "name": scenario["name"],
                        "raw": scenario["raw"],
                        "task_id": scenario["task_id"],
                        "result_present": False,
                        "reason_codes": [RESULT_TERMINAL_CANARY_INTERNAL_ERROR],
                    }
                )

        observed_task_dir = Path(module.TASK_DIR)

        observed_result_dir = Path(module.RESULT_DIR)

        dirs_ok = all(
            directory.exists() and root in directory.resolve().parents
            for directory in (
                observed_task_dir,
                observed_result_dir,
                Path(module.TASK_STATE_DIR),
                Path(module.RECOVERY_DIR),
            )
        )
    finally:
        _restore(module, original)

    if not boundary_intact:
        codes.append(RESULT_TERMINAL_CANARY_FRESH_IMPORT_BOUNDARY_BROKEN)

    rule_consistent = authority_rule_consistent(authority)

    if authority is None:
        codes.append(RESULT_TERMINAL_CANARY_AUTHORITY_MODULE_NOT_LOADED)
    elif not rule_consistent:
        codes.append(RESULT_TERMINAL_CANARY_AUTHORITY_RULE_INCONSISTENT)

    for scenario in scenarios:
        scenario_code_list = scenario.get("reason_codes")

        if isinstance(scenario_code_list, list):
            codes.extend(scenario_code_list)

    codes = _dedupe(codes)

    if not dirs_ok:
        # 无法确认「canary 只在自己的临时 workdir 内产生终态事实」⇒ fail-closed。
        codes = _dedupe([*codes, RESULT_TERMINAL_CANARY_INTERNAL_ERROR])

    verdict = VERDICT_PASS if not codes else VERDICT_FAIL

    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "status": verdict,
        "pipeline_entry": PIPELINE_ENTRY,
        "workdir": str(root),
        "fresh_process": boundary_intact,
        "pytest_imported": pytest_imported,
        "fresh_import_dirs": loaded_boundary_files(),
        "ai_orchestrator_file": getattr(module, "__file__", None),
        "authority_module_file": getattr(authority, "__file__", None),
        "authority_rule_consistent": rule_consistent,
        "observed_task_dir": str(observed_task_dir),
        "observed_result_dir": str(observed_result_dir),
        "dirs_ok": dirs_ok,
        "scenarios": scenarios,
        "verdicts": {
            str(scenario.get("name")): (
                VERDICT_PASS if not scenario.get("reason_codes") else VERDICT_FAIL
            )
            for scenario in scenarios
        },
        "reason_codes": codes,
    }


def emit(report: dict[str, Any], stream: Any | None = None) -> int:
    """输出 **ASCII** JSON 报告并返回退出码（PASS ``0`` / FAIL ``1``）。"""

    target = sys.stdout if stream is None else stream

    target.write(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n")

    return EXIT_PASS if report.get("verdict") == VERDICT_PASS else EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    try:
        report = run_canary(args[0] if args else None)
    except Exception as exc:
        report = {
            "schema": SCHEMA,
            "verdict": VERDICT_FAIL,
            "status": VERDICT_FAIL,
            "pipeline_entry": PIPELINE_ENTRY,
            "reason_codes": [RESULT_TERMINAL_CANARY_INTERNAL_ERROR],
            "error": str(exc),
        }

    return emit(report)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

