"""GPT Review 收口写入前的原子一致性只读预检（GOLD-045）。

为什么需要它
------------
§2.15 给出历史矛盾的**裁决契约**，§2.16 给出**证据包**，§2.17 把**有效**裁决接入
review binding / integrity / backlog 事实链。但 GPT 真正要**写** 三份东西
（``.ai/adjudications/legacy_result_adjudications.json`` /
``.ai/GPT_REVIEW_LEDGER.json`` / ``.ai/PROJECT_STATE.json``）时是分次写入的：一旦写到
一半（例如裁决已落盘但 ledger 与 ``last_reviewed_task`` 还没跟上、或 ledger 越过仍未裁决
的矛盾），控制面就会出现**新的** ledger / state drift，而现有工具各自只看自己那一层。

本模块提供**单一只读预检**：GPT 在真正写入前，把「候选的裁决 store + 候选 ledger +
候选 PROJECT_STATE」与**当前已提交事实**（HEAD、tasks/results/state/ledger/store 摘要、
§2.15 裁决校验、§2.8 binding、§2.11 backlog、§2.9 integrity）一次性比对，任何 stale /
不连续 / 越权 / 安全不变量变化都 **fail-closed 只报告**。

它回答的唯一问题
----------------
「把这份候选写下去，是否仍然保持 review 链的连续性、身份绑定与安全不变量？」

设计原则（与既有 control-plane 工具一致）
------------------------------------------
1. **只读、零写入**：本模块没有任何写入 `adjudication` / `ledger` / `state` / `tasks` /
   `results` 的路径（源码守卫测试锁定）；候选输入**只**来自显式临时文件或 ``stdin``；
   唯一可选写操作是显式 ``--output``（**复用** §2.12 的 fail-closed 路径守卫：
   ``<root>/.ai/runtime/**`` 或系统临时目录），因此结构上不可能写 managed 状态；
2. **候选 ≠ 权限**：没有 ``--apply`` / ``--fix`` / ``--advance`` / ``--sign`` 等变更开关；
   ``candidate_ready=true`` 只表示「候选写集与已提交事实原子一致」，**绝不**等于 review
   已完成、**绝不**解除 Phase gate、**绝不**触发 Executor 写入或自动 push；
3. **复用，不复制**：HEAD / 指针事实复用 :mod:`orchestrator.planner_snapshot`；身份 /
   commit 复用 :mod:`orchestrator.review_binding`；ledger 校验与连续性复用
   :mod:`orchestrator.review_ledger` + :mod:`orchestrator.review_ledger_integrity`；
   裁决校验复用 :mod:`orchestrator.legacy_result_adjudication`（§2.15 唯一口径）；
   backlog 复用 :mod:`orchestrator.review_backlog`。本模块自身的判定算法**为零**
   （只做稳定 code 归一化与比对）；
4. **确定性**：``precondition_digest`` 只覆盖事实（``generated_at`` / ``as_of`` /
   ``precondition_digest`` / ``determinism`` 被显式排除），相同候选 + 相同已提交事实必然
   得到相同 digest。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **GPT-only**：本工具只验证候选，绝不签发 review / 裁决，绝不推进指针、绝不解除
  ``PHASE3_3_DATA``、绝不决定 Phase；
- 历史 result 永久只读；不得弱化 §2.13 terminal-consistency 或 ledger 连续性门禁；
- ``PHASE3_3_DATA`` 保持 BLOCKED；不得进入 Phase 3.4 或跨 L3/L4；
- ``LIVE_TRADING=false``；``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``；
- 零网络、零数据库、零业务证据、零模型调用；外部进程调用**只经由** §2.8 review_binding
  的只读 Git 白名单。

用法
----
.. code-block:: text

    python -m orchestrator.review_closure_precondition --candidate <临时文件>
    python -m orchestrator.review_closure_precondition --candidate - < candidate.json
    python -m orchestrator.review_closure_precondition --candidate <临时文件> --as-of <ISO>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放人类可读
的 issue 摘要（绝不参与机器解析）。给出 ``--output`` 时 JSON 只写受控文件、``stdout`` 为空。

退出码：``0`` 候选与已提交事实原子一致（candidate-ready，**不是** review 完成）/
``2`` fail-closed（stale HEAD / 漂移 / 链不连续 / 越权 / 安全不变量变化）/
``3`` 候选不可用（缺失 / 不可读 / 非法 JSON / schema 不支持）或 PROJECT_STATE 不可读 /
``4`` ``--output`` 目标被 fail-closed 拒绝（此时绝不写任何文件）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import planner_mutation_precondition as mutation_precondition
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import review_backlog as backlog_mod
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
PRECONDITION_SCHEMA = "gold-ai/review-closure-precondition/v1"

PRECONDITION_SCHEMA_VERSION = 1

AUTHORITY_SCHEMA = "gold-ai/review-closure-precondition-authority/v1"

# 候选写集契约（GPT 显式提供；只允许来自临时文件 / stdin）。
CANDIDATE_SCHEMA = "gold-ai/review-closure-candidate/v1"

CANDIDATE_SCHEMA_VERSION = 1

EXIT_OK = 0

# stale HEAD / 漂移 / 链不连续 / 越权 / 安全不变量变化：fail-closed（只报告，绝不修复）。
EXIT_FAIL_CLOSED = 2

# 候选 / 已提交事实不可用（绝不把「读不到」当成「没问题」）。
EXIT_INPUT_UNAVAILABLE = 3

# ``--output`` 被拒的退出码复用同一受控输出守卫（``snapshot_output.EXIT_OUTPUT_REJECTED`` = 4）。

# 不参与确定性 digest 的字段。
FACTS_EXCLUDED_KEYS = ("as_of", "generated_at", "precondition_digest", "determinism")

# 必须保持阻塞的 blocker（候选不得删除）。
PHASE3_3_BLOCKER = "PHASE3_3_DATA"

# ---------- 本模块自定义的稳定 reason code ----------
# 其余 code 全部复用既有模块（planner / binding / ledger / integrity / adjudication），
# 保持单一事实来源。
REASON_CANDIDATE_MISSING = "CANDIDATE_MISSING"

REASON_CANDIDATE_UNREADABLE = "CANDIDATE_UNREADABLE"

REASON_CANDIDATE_JSON_INVALID = "CANDIDATE_JSON_INVALID"

REASON_CANDIDATE_NOT_OBJECT = "CANDIDATE_NOT_OBJECT"

REASON_CANDIDATE_SCHEMA_UNSUPPORTED = "CANDIDATE_SCHEMA_UNSUPPORTED"

REASON_CANDIDATE_SECTION_INVALID = "CANDIDATE_SECTION_INVALID"

REASON_CANDIDATE_BASE_MISSING = "CANDIDATE_BASE_MISSING"

REASON_CANDIDATE_BASE_INVALID = "CANDIDATE_BASE_INVALID"

REASON_STALE_REMOTE_HEAD = "STALE_REMOTE_HEAD"

REASON_BASE_RESULTS_DRIFT = "CANDIDATE_BASE_RESULTS_DRIFT"

REASON_BASE_TASKS_DRIFT = "CANDIDATE_BASE_TASKS_DRIFT"

REASON_BASE_STATE_DRIFT = "CANDIDATE_BASE_STATE_DRIFT"

REASON_BASE_LEDGER_DRIFT = "CANDIDATE_BASE_LEDGER_DRIFT"

REASON_BASE_ADJUDICATION_DRIFT = "CANDIDATE_BASE_ADJUDICATION_DRIFT"

REASON_LEDGER_ENTRY_REMOVED = "CANDIDATE_LEDGER_ENTRY_REMOVED"

REASON_PASS_PAST_UNRESOLVED_CONTRADICTION = "CANDIDATE_PASS_PAST_UNRESOLVED_CONTRADICTION"

REASON_ADJUDICATION_INVALID = "CANDIDATE_ADJUDICATION_INVALID"

REASON_ADJUDICATION_NOT_GPT = "CANDIDATE_ADJUDICATION_NOT_GPT"

REASON_LAST_REVIEWED_REGRESSION = "CANDIDATE_LAST_REVIEWED_REGRESSION"

REASON_LAST_REVIEWED_AHEAD = "CANDIDATE_LAST_REVIEWED_AHEAD"

REASON_STATE_POINTER_DRIFT = "CANDIDATE_STATE_POINTER_DRIFT"

REASON_PHASE3_3_BLOCKER_REMOVED = "CANDIDATE_PHASE3_3_BLOCKER_REMOVED"

REASON_BLOCKER_REMOVED = "CANDIDATE_BLOCKER_REMOVED"

REASON_PHASE3_4_ENTRY = "CANDIDATE_PHASE3_4_ENTRY"

REASON_TRADING_SAFETY_CHANGED = "CANDIDATE_TRADING_SAFETY_CHANGED"

REASON_COMMITTED_FACTS_UNAVAILABLE = "COMMITTED_FACTS_UNAVAILABLE"

# 候选不可用（退出码 3）的 code 集合。
CANDIDATE_UNAVAILABLE_CODES = (
    REASON_CANDIDATE_MISSING,
    REASON_CANDIDATE_UNREADABLE,
    REASON_CANDIDATE_JSON_INVALID,
    REASON_CANDIDATE_NOT_OBJECT,
    REASON_CANDIDATE_SCHEMA_UNSUPPORTED,
)

# §2.15「非 GPT 裁决」类稳定 code（越权 / 未知身份 / role 非法 / reviewer 缺失）。
NON_GPT_ADJUDICATION_CODES = (
    adjudication.ISSUE_REVIEWER_ROLE_INVALID,
    adjudication.ISSUE_REVIEWER_NOT_AUTHORIZED,
    adjudication.ISSUE_EXECUTOR_FORBIDDEN,
    adjudication.ISSUE_REVIEWER_MISSING,
)

# 复用 §2.9 的连续性稳定 code（供测试 / GPT 断言）。
LEDGER_CHAIN_GAP_CODE = integrity.ISSUE_LEDGER_CHAIN_GAP

LEDGER_CONTINUITY_CODES = (
    integrity.ISSUE_LEDGER_CHAIN_GAP,
    integrity.ISSUE_LEDGER_ORDER_REGRESSION,
    integrity.ISSUE_LEDGER_REVIEW_TIME_REGRESSION,
)

# 候选输入只允许来自系统临时目录或 ``<root>/.ai/runtime/**``（显式临时文件 / stdin）。
CANDIDATE_SOURCE_STDIN = "stdin"

CANDIDATE_STDIN_TOKENS = ("-", "stdin")

# 阶段解析（``Phase 3`` / ``Phase 3.4``）；解析不了 → 不据此放行（只报告）。
PHASE_PATTERN = re.compile(r"^\s*phase\s*(\d+)(?:\s*\.\s*(\d+))?\s*$", re.IGNORECASE)

PHASE_3_4_RANK = (3, 4)

# HEAD / 摘要形式（只读比对用）。
HEAD_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# 复用 §2.12 的确定性摘要 / 文件事实实现（保证 base digest 与 §2.12 完全同口径）。
canonical_digest = mutation_precondition.canonical_digest

file_facts = mutation_precondition.file_facts

directory_facts = mutation_precondition.directory_facts

# 确定性排序契约（机器可读）。
PRECONDITION_ORDERING = {
    "reason_codes": "sorted unique",
    "issues": "sorted by (code, detail)",
    "candidate.review_ledger.entries": "ledger file order",
    "candidate.review_ledger.bindings": "sorted by planner.task_id_sort_key",
    "candidate.adjudication.tasks": "sorted by planner.task_id_sort_key",
    "committed_current.review_binding.tasks": "sorted by planner.task_id_sort_key",
    "tasks_digest.files": "sorted relative posix path",
    "results_digest.files": "sorted relative posix path",
}

ManifestBuilder = Callable[[str], dict[str, Any]]

BacklogBuilder = Callable[[], dict[str, Any]]

IntegrityBuilder = Callable[[], dict[str, Any]]

LiveFactsProvider = Callable[[str], dict[str, Any]]



# ============================================================
# 通用只读工具（确定性）
# ============================================================


def own_issue(code: str, detail: str) -> dict[str, str]:
    """本模块自有诊断（fail-closed：只报告，绝不触发任何修复动作）。"""

    return {
        "code": code,
        "severity": planner.SEVERITY_ERROR,
        "detail": detail,
        "source": "orchestrator.review_closure_precondition",
    }


def inherited_issue(code: object, detail: object, source: str) -> dict[str, str]:
    """给复用来的事实 / 门禁 code 标注来源（只读透传，绝不改写语义）。"""

    return {
        "code": str(code),
        "severity": planner.SEVERITY_ERROR,
        "detail": str(detail),
        "source": source,
    }


def text_value(value: object) -> str | None:
    """非空字符串值（否则 ``None``，绝不猜测）。"""

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def normalize_head_sha(value: object) -> tuple[str | None, str | None]:
    """规范化候选声明的 HEAD；非法形式返回 ``(text, reason)``。"""

    if value is None:
        return None, None

    text = str(value).strip().lower()

    if not text:
        return None, None

    if not HEAD_SHA_PATTERN.match(text):
        return text, (
            f"base.head_sha 非法（需要 7~40 位十六进制字符）: {text!r}："
            "无法证明远端 HEAD 未变化，fail-closed"
        )

    return text, None


def phase_rank(value: object) -> tuple[int, int] | None:
    """``Phase X[.Y]`` → ``(X, Y)``；解析不了返回 ``None``（绝不猜测）。"""

    text = text_value(value)

    if text is None:
        return None

    match = PHASE_PATTERN.match(text)

    if match is None:
        return None

    return int(match.group(1)), int(match.group(2) or 0)


def sorted_unique(values: Sequence[object]) -> list[str]:
    """确定性去重排序（稳定 reason code 列表）。"""

    return sorted({str(value) for value in values if value is not None})


def sorted_issues(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """去重（code, severity, detail, source）后按 ``(code, detail)`` 排序。"""

    deduped = {
        (
            str(issue.get("code")),
            str(issue.get("severity")),
            str(issue.get("detail")),
            str(issue.get("source")),
        ): dict(issue)
        for issue in issues
    }

    return sorted(deduped.values(), key=lambda issue: (issue["code"], issue["detail"]))


# ============================================================
# 候选输入：解析 / 信封校验 / 显式来源守卫（零写入）
# ============================================================


def parse_candidate_text(text: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """把候选文本解析为 JSON 对象；失败一律 fail-closed（绝不猜）。"""

    try:
        payload = json.loads(text)

    except ValueError as exc:
        return None, [
            own_issue(REASON_CANDIDATE_JSON_INVALID, f"候选不是合法 JSON（fail-closed）: {exc}")
        ]

    if not isinstance(payload, dict):
        return None, [
            own_issue(REASON_CANDIDATE_NOT_OBJECT, "候选顶层必须是 JSON 对象（fail-closed）")
        ]

    return payload, []


def load_candidate_file(path: Path) -> tuple[str | None, list[dict[str, str]]]:
    """**只读**读取候选文件；不可读 ⇒ fail-closed（绝不创建 / 修复）。"""

    try:
        text = Path(path).read_text(encoding="utf-8")

    except (OSError, UnicodeDecodeError) as exc:
        return None, [
            own_issue(REASON_CANDIDATE_UNREADABLE, f"{path}: 候选不可读（fail-closed）: {exc}")
        ]

    return text, []


def resolve_candidate_source(root: Path, candidate: str | Path) -> tuple[str | None, str | None]:
    """把 ``--candidate`` 解析为已通过守卫的来源；``(source, reason)``。

    - ``-`` / ``stdin`` ⇒ 标准输入（显式，无需落盘的文件）；
    - 其它值必须是**显式临时文件**：只允许系统临时目录或 ``<root>/.ai/runtime/**``；
    - ``.ai/tasks`` / ``.ai/results`` / ``.ai/PROJECT_STATE.json`` /
      ``.ai/GPT_REVIEW_LEDGER.json`` / ``.ai/adjudications/**`` 等 managed 状态一律拒绝，
      防止把控制面状态文件当作候选输入。
    """

    raw = str(candidate).strip()

    if not raw or raw.lower() in CANDIDATE_STDIN_TOKENS:
        return CANDIDATE_SOURCE_STDIN, None

    target = Path(raw)

    if not target.is_absolute():
        target = Path(root) / target

    resolved = target.resolve()

    resolved_root = Path(root).resolve()

    ai_dir = resolved_root / ".ai"

    runtime_dir = resolved_root / snapshot_output.RUNTIME_SUBPATH

    if snapshot_output.is_within(resolved, ai_dir) and not snapshot_output.is_within(
        resolved, runtime_dir
    ):
        return None, (
            "候选只允许来自系统临时目录 / <root>/.ai/runtime/** 或 stdin："
            f"拒绝把 managed 状态当作候选: {resolved}"
        )

    temp_dir = snapshot_output.temp_directory().resolve()

    if not (snapshot_output.is_within(resolved, runtime_dir) or snapshot_output.is_within(
        resolved, temp_dir
    )):
        return None, (
            "候选只允许来自显式临时文件（系统临时目录 / <root>/.ai/runtime/**）或 stdin："
            f"{resolved}"
        )

    return str(resolved), None


def validate_candidate_envelope(candidate: Mapping[str, Any]) -> list[dict[str, str]]:
    """候选信封契约（schema + base）；不合规一律 fail-closed。"""

    issues: list[dict[str, str]] = []

    schema = candidate.get("schema")

    version = candidate.get("schema_version")

    if schema != CANDIDATE_SCHEMA or version != CANDIDATE_SCHEMA_VERSION:
        issues.append(
            own_issue(
                REASON_CANDIDATE_SCHEMA_UNSUPPORTED,
                f"候选 schema={schema!r} / schema_version={version!r} 不是 "
                f"{CANDIDATE_SCHEMA} v{CANDIDATE_SCHEMA_VERSION}（schema 变化必须 bump 版本，"
                "绝不猜）",
            )
        )

    base = candidate.get("base")

    if not isinstance(base, Mapping):
        issues.append(
            own_issue(
                REASON_CANDIDATE_BASE_MISSING,
                "候选缺少 base 段（head_sha / results_digest / tasks_digest / "
                "project_state_sha256 / review_ledger_sha256 / adjudication_store_sha256）："
                "无法证明候选基于当前已提交事实，fail-closed",
            )
        )

    return issues



# ============================================================
# 已提交事实（committed-current）：HEAD / 摘要 / state / ledger / store / backlog / integrity
# ============================================================


def head_facts(
    *,
    snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """只读 HEAD 事实（**复用** planner snapshot 的 ``git`` 段，零额外 IO）。"""

    raw = snapshot.get("git")

    git_facts = dict(raw) if isinstance(raw, dict) else {}

    raw_head = git_facts.get("head")

    observed = (
        raw_head.strip().lower() if isinstance(raw_head, str) and raw_head.strip() else None
    )

    resolved = bool(git_facts.get("available")) and observed is not None

    issues: list[dict[str, str]] = []

    if not resolved:
        observed = None

        issues.append(
            inherited_issue(
                planner.ISSUE_GIT_INFO_UNAVAILABLE,
                "无法从 planner snapshot 读取 Git branch/head 事实：无法证明远端 HEAD 未变化 "
                "⇒ fail-closed（绝不猜测版本）",
                "orchestrator.planner_snapshot",
            )
        )

    section: dict[str, Any] = {
        "branch": git_facts.get("branch"),
        "detached": bool(git_facts.get("detached")),
        "observed_head_sha": observed,
        "observed_head_short": observed[:8] if observed else None,
        "resolved": resolved,
        "source": ".git HEAD/refs via orchestrator.planner_snapshot (read-only)",
        "sync_note": (
            "observed head 来自本地 .git 的分支尖端（Orchestrator 持续同步）；"
            "本工具绝不 fetch / pull / merge / rebase / force push"
        ),
    }

    return section, issues


def state_section(state: Mapping[str, Any], state_path: Path) -> dict[str, Any]:
    """``PROJECT_STATE`` 的只读事实（原样回显指针 + blob sha256 + 稳定 code 列表）。"""

    byte_count, blob_sha = file_facts(state_path)

    payload = dict(state)

    declared, declared_error = planner.declared_queue(payload)

    return {
        "blob_sha256": blob_sha,
        "blocker_codes": planner.gate_codes(payload.get("blockers")),
        "branch": payload.get("branch"),
        "bytes": byte_count,
        "current_task": planner.pointer_value(payload, "current_task"),
        "human_gate_codes": planner.gate_codes(payload.get("human_gates")),
        "invariants": planner.invariant_list(payload),
        "last_completed_task": planner.pointer_value(payload, "last_completed_task"),
        "last_reviewed_task": planner.pointer_value(payload, "last_reviewed_task"),
        "path": str(state_path),
        "phase": payload.get("phase"),
        "queue_status": payload.get("queue_status"),
        "readable": blob_sha is not None,
        "status": payload.get("status"),
        "task_queue": declared,
        "task_queue_error": declared_error,
    }



def committed_facts(
    *,
    root: Path,
    state_path: Path,
    tasks_dir: Path,
    results_dir: Path,
    ledger_path: Path,
    adjudication_store_path: Path,
    snapshot: Mapping[str, Any],
    backlog_builder: BacklogBuilder | None,
    integrity_builder: IntegrityBuilder | None,
    generated_at: str,
) -> dict[str, Any]:
    """已提交事实（只读摘要 + ledger / store blob 摘要 + backlog / integrity 事实）。"""

    state_raw = snapshot.get("project_state")

    state = dict(state_raw) if isinstance(state_raw, Mapping) else {}

    store_bytes, store_sha = file_facts(adjudication_store_path)

    ledger_bytes, ledger_sha = file_facts(ledger_path)

    return {
        "adjudication_store": {
            "blob_sha256": store_sha,
            "bytes": store_bytes,
            "path": str(adjudication_store_path),
            "present": adjudication_store_path.exists(),
        },
        "paths": {
            "adjudication_store": str(adjudication_store_path),
            "project_state": str(state_path),
            "results_dir": str(results_dir),
            "review_ledger": str(ledger_path),
            "root": str(root),
            "tasks_dir": str(tasks_dir),
        },
        "project_state": state_section(state, state_path),
        "project_state_payload": state,
        "results_digest": directory_facts(results_dir),
        "review_backlog": backlog_facts(
            root=root,
            tasks_dir=tasks_dir,
            results_dir=results_dir,
            state_path=state_path,
            ledger_path=ledger_path,
            adjudication_store_path=adjudication_store_path,
            generated_at=generated_at,
            backlog_builder=backlog_builder,
        ),
        "review_integrity": integrity_facts(
            root=root,
            tasks_dir=tasks_dir,
            results_dir=results_dir,
            ledger_path=ledger_path,
            adjudication_store_path=adjudication_store_path,
            generated_at=generated_at,
            integrity_builder=integrity_builder,
        ),
        "review_ledger": {
            "blob_sha256": ledger_sha,
            "bytes": ledger_bytes,
            "path": str(ledger_path),
            "present": ledger_path.exists(),
        },
        "tasks_digest": directory_facts(tasks_dir),
    }



def backlog_facts(
    *,
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
    state_path: Path,
    ledger_path: Path,
    adjudication_store_path: Path,
    generated_at: str,
    backlog_builder: BacklogBuilder | None,
) -> dict[str, Any]:
    """已提交 §2.11 backlog 事实（**复用** manifest；绝不签发 review 结论）。"""

    try:
        manifest = (
            backlog_builder()
            if backlog_builder is not None
            else backlog_mod.build_review_backlog_manifest(
                root=root,
                tasks_dir=tasks_dir,
                results_dir=results_dir,
                state_path=state_path,
                ledger_path=ledger_path,
                adjudication_store_path=adjudication_store_path,
                generated_at=generated_at,
            )
        )

    except Exception as exc:  # noqa: BLE001 - 事实来源异常必须 fail-closed，绝不中断整轮
        return {
            "available": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "schema": backlog_mod.REVIEW_BACKLOG_SCHEMA,
            "source": "orchestrator.review_backlog.build_review_backlog_manifest",
        }

    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}

    coverage = manifest.get("coverage") if isinstance(manifest.get("coverage"), dict) else {}

    return {
        "available": True,
        "backlog_digest": manifest.get("backlog_digest"),
        "coverage": {
            "backlog_count": coverage.get("backlog_count"),
            "backlog_first": coverage.get("backlog_first"),
            "backlog_last": coverage.get("backlog_last"),
            "last_reviewed_task_pointer": coverage.get("last_reviewed_task_pointer"),
            "newest_completed_result": coverage.get("newest_completed_result"),
        },
        "reason_codes": sorted_unique(manifest.get("reason_codes") or []),
        "schema": manifest.get("schema"),
        "source": "orchestrator.review_backlog.build_review_backlog_manifest",
        "summary": {
            "adjudicated_count": summary.get("adjudicated_count"),
            "backlog_count": summary.get("backlog_count"),
            "bound_count": summary.get("bound_count"),
            "facts_ready_count": summary.get("facts_ready_count"),
            "unresolved_contradiction_count": summary.get("unresolved_contradiction_count"),
        },
    }


def integrity_facts(
    *,
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
    ledger_path: Path,
    adjudication_store_path: Path,
    generated_at: str,
    integrity_builder: IntegrityBuilder | None,
) -> dict[str, Any]:
    """已提交 §2.9 ledger 完整性事实（**复用** integrity report）。"""

    try:
        report = (
            integrity_builder()
            if integrity_builder is not None
            else integrity.build_integrity_report(
                root=root,
                tasks_dir=tasks_dir,
                results_dir=results_dir,
                ledger_path=ledger_path,
                adjudication_store_path=adjudication_store_path,
                generated_at=generated_at,
            )
        )

    except Exception as exc:  # noqa: BLE001 - 事实来源异常必须 fail-closed，绝不中断整轮
        return {
            "available": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "schema": integrity.LEDGER_INTEGRITY_SCHEMA,
            "source": "orchestrator.review_ledger_integrity.build_integrity_report",
        }

    ledger_section = report.get("ledger") if isinstance(report.get("ledger"), dict) else {}

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}

    return {
        "available": True,
        "adjudicated_entry_count": ledger_section.get("adjudicated_entry_count"),
        "integrity_ok": summary.get("integrity_ok"),
        "reason_codes": sorted_unique(
            str(issue.get("code"))
            for issue in (report.get("issues") or [])
            if isinstance(issue, dict)
        ),
        "schema": report.get("schema"),
        "source": "orchestrator.review_ledger_integrity.build_integrity_report",
        "summary": {
            "bound_count": summary.get("bound_count"),
            "entry_count": summary.get("entry_count"),
            "issue_count": summary.get("issue_count"),
        },
    }



# ============================================================
# 候选 §2.15 裁决 store（in-memory；复用 §2.15 唯一口径）
# ============================================================


def collect_candidate_store(
    store: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把候选 store **payload** 归集成与 §2.15 ``collect_store_index`` **同 shape** 的事实。

    复用 §2.15 的纯函数（``store_entries`` / ``entry_task_id`` / ``store_consistency_issues``），
    因此候选裁决的 schema / 越权 / 重复 / 冲突判定与真实 store **同一口径**；本函数只做
    归集，绝不创建、修复或改写任何 store。
    """

    section: dict[str, Any] = {
        "adjudication_count": 0,
        "present": store is not None,
        "schema": None,
        "schema_version": None,
    }

    collected: dict[str, Any] = {
        "consistency": {},
        "entries_by_task": {},
        "indexed_entries": {},
        "store_section": section,
    }

    if store is None:
        return collected, []

    section["schema"] = text_value(store.get("schema"))

    section["schema_version"] = store.get("schema_version")

    raw_entries, issues = adjudication.store_entries(store)

    collected_issues = [
        inherited_issue(
            issue["code"],
            issue["detail"],
            "orchestrator.legacy_result_adjudication",
        )
        for issue in issues
    ]

    section["adjudication_count"] = len(raw_entries)

    indexed: dict[int, Mapping[str, Any]] = {}

    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, Mapping):
            collected_issues.append(
                inherited_issue(
                    adjudication.ISSUE_ENTRY_INVALID,
                    f"adjudications[{index}] 不是 JSON 对象",
                    "orchestrator.legacy_result_adjudication",
                )
            )

            continue

        if adjudication.entry_task_id(entry) is None:
            collected_issues.append(
                inherited_issue(
                    adjudication.ISSUE_TASK_ID_INVALID,
                    f"adjudications[{index}]: task_id 缺失或非法，无法归集到具体任务",
                    "orchestrator.legacy_result_adjudication",
                )
            )

            continue

        indexed[index] = entry

    entries_by_task: dict[str, list[int]] = {}

    for index, entry in indexed.items():
        task_id = adjudication.entry_task_id(entry)

        if task_id is not None:
            entries_by_task.setdefault(task_id, []).append(index)

    collected["indexed_entries"] = indexed

    collected["entries_by_task"] = entries_by_task

    collected["consistency"] = adjudication.store_consistency_issues(indexed)

    section["task_ids"] = sorted(entries_by_task, key=planner.task_id_sort_key)

    return collected, collected_issues


def evaluate_candidate_task(
    task_id: str,
    *,
    store_facts: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    as_of: str | None,
) -> dict[str, Any]:
    """复用 §2.15 ``evaluate_task`` 判定候选裁决状态（绝不另造判定算法）。"""

    return adjudication.evaluate_task(
        task_id,
        entries_by_task=store_facts.get("entries_by_task") or {},
        indexed_entries=store_facts.get("indexed_entries") or {},
        consistency=store_facts.get("consistency") or {},
        live=live,
        as_of=as_of,
    )


def adjudication_task_issues(task_id: str, view: Mapping[str, Any]) -> list[dict[str, str]]:
    """把候选裁决状态映射为稳定 fail-closed code（越权 / 漂移 / 冲突等原样透传）。"""

    state = view.get("state")

    if state != adjudication.TASK_STATE_INVALID:
        return []

    codes = sorted_unique(view.get("reason_codes") or [])

    issues = [
        inherited_issue(
            code,
            f"{task_id}: §2.15 候选裁决 fail-closed code",
            "orchestrator.legacy_result_adjudication",
        )
        for code in codes
    ]

    issues.append(
        own_issue(
            REASON_ADJUDICATION_INVALID,
            f"{task_id}: 候选裁决记录非法 / 冲突（{', '.join(codes) or 'unknown'}）："
            "fail-closed，绝不把非法裁决当作可用事实",
        )
    )

    if set(codes) & set(NON_GPT_ADJUDICATION_CODES):
        issues.append(
            own_issue(
                REASON_ADJUDICATION_NOT_GPT,
                f"{task_id}: 候选裁决不是合法 GPT 裁决（{', '.join(codes)}）："
                "GPT 是唯一 Reviewer，越权 / 未知身份一律拒绝",
            )
        )

    return issues


def evaluate_candidate_adjudication(
    store_facts: Mapping[str, Any],
    *,
    task_ids: Sequence[str],
    live_provider: LiveFactsProvider,
    as_of: str | None,
) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, dict[str, Any]]]:
    """候选裁决事实：``(section, issues, state_by_task)``（只读、fail-closed）。"""

    issues: list[dict[str, str]] = []

    tasks: list[dict[str, Any]] = []

    state_by_task: dict[str, dict[str, Any]] = {}

    for task_id in sorted(dict.fromkeys(task_ids), key=planner.task_id_sort_key):
        live = live_provider(task_id)

        view = evaluate_candidate_task(
            task_id, store_facts=store_facts, live=live, as_of=as_of
        )

        state_by_task[task_id] = dict(view)

        issues.extend(adjudication_task_issues(task_id, view))

        tasks.append(
            {
                "adjudication_id": (
                    view["adjudication"].get("adjudication_id")
                    if isinstance(view.get("adjudication"), dict)
                    else None
                ),
                "reason_codes": sorted_unique(view.get("reason_codes") or []),
                "reviewer": (
                    view["adjudication"].get("reviewer")
                    if isinstance(view.get("adjudication"), dict)
                    else None
                ),
                "reviewer_role": (
                    view["adjudication"].get("reviewer_role")
                    if isinstance(view.get("adjudication"), dict)
                    else None
                ),
                "state": view.get("state"),
                "task_id": task_id,
                "valid": bool(view.get("valid")),
            }
        )

    section: dict[str, Any] = {
        "live_facts_source": "orchestrator.legacy_result_adjudication.review_binding_live_facts",
        "store": store_facts["store_section"],
        "tasks": tasks,
    }

    return section, issues, state_by_task



# ============================================================
# 候选 review ledger（复用 §2.9 连续性 + §2.8 身份比对）
# ============================================================


def patched_manifest_builder(
    base_builder: ManifestBuilder,
    state_by_task: Mapping[str, Mapping[str, Any]],
) -> ManifestBuilder:
    """把候选裁决注入 §2.8 manifest 事实视图（只改 ``binding`` 事实，不改身份算法）。

    §2.8 默认读取**已提交** store；候选可能同时新增裁决与 ledger 条目，因此这里只在
    「候选裁决已 ``adjudicated``，且 manifest 的阻塞 code 只有 legacy 终态矛盾」时把
    ``binding.facts_ready`` / ``adjudicated`` 置为候选事实；其余情况一字不放宽。
    """

    def build(task_id: str) -> dict[str, Any]:
        manifest = base_builder(task_id)

        view = state_by_task.get(task_id)

        if (
            not isinstance(view, Mapping)
            or view.get("state") != adjudication.TASK_STATE_ADJUDICATED
        ):
            return manifest

        section = manifest.get("binding")

        if not isinstance(section, dict):
            return manifest

        codes = [str(code) for code in (section.get("reason_codes") or [])]

        non_legacy = [code for code in codes if code != binding.LEGACY_CONTRADICTION_CODE]

        if non_legacy:
            return manifest

        patched = dict(manifest)

        patched["binding"] = {
            **section,
            "adjudicated": True,
            "facts_ready": True,
            "facts_ready_source": binding.FACTS_READY_SOURCE_ADJUDICATED,
        }

        return patched

    return build


def candidate_ledger_facts(
    ledger: Mapping[str, Any],
    *,
    base_builder: ManifestBuilder,
    state_by_task: Mapping[str, Mapping[str, Any]],
    results_dir: Path,
    committed_reviewed_ids: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """候选 ledger 事实：连续性 / 顺序 / 身份绑定 / PASS 越过未裁决矛盾。"""

    payload = dict(ledger)

    ledger_view, ledger_issues = ledger_mod.validate_review_ledger(payload, None)

    issues = [
        inherited_issue(
            integrity.translate_ledger_issue(str(issue["code"])),
            f"{issue['code']}: {issue['detail']}",
            "orchestrator.review_ledger",
        )
        for issue in ledger_issues
    ]

    facts = integrity.raw_entry_facts(payload)

    valid_entries = [entry for entry in ledger_view["entries"] if entry.get("valid") is True]

    reviewed_ids = [str(entry["task_id"]) for entry in valid_entries]

    chain, chain_issues = integrity.chain_section(facts, reviewed_ids, ledger_view, results_dir)

    issues.extend(
        inherited_issue(
            issue["code"], issue["detail"], "orchestrator.review_ledger_integrity"
        )
        for issue in chain_issues
    )

    builder = patched_manifest_builder(base_builder, state_by_task)

    bindings: list[dict[str, Any]] = []

    for entry in sorted(
        valid_entries, key=lambda item: planner.task_id_sort_key(str(item["task_id"]))
    ):
        view, binding_issues = integrity.binding_entry_view(entry, builder)

        bindings.append(
            {
                "bound": view.get("bound"),
                "manifest_adjudicated": view.get("manifest_adjudicated"),
                "manifest_facts_ready": view.get("manifest_facts_ready"),
                "manifest_reason_codes": view.get("manifest_reason_codes"),
                "task_id": view.get("task_id"),
                "verdict": view.get("verdict"),
            }
        )

        issues.extend(
            inherited_issue(
                issue["code"], issue["detail"], "orchestrator.review_ledger_integrity"
            )
            for issue in binding_issues
        )

    for entry in valid_entries:
        task_id = str(entry["task_id"])

        view = state_by_task.get(task_id)

        contradiction = (
            bool(view["original_contradiction"]["contradiction"])
            if isinstance(view, Mapping)
            and isinstance(view.get("original_contradiction"), Mapping)
            else False
        )

        adjudicated = isinstance(view, Mapping) and view.get("state") == (
            adjudication.TASK_STATE_ADJUDICATED
        )

        pass_past_contradiction = (
            str(entry.get("verdict")) == ledger_mod.VERDICT_PASS
            and contradiction
            and not adjudicated
        )

        if pass_past_contradiction:
            issues.append(
                own_issue(
                    REASON_PASS_PAST_UNRESOLVED_CONTRADICTION,
                    f"{task_id}: 候选 ledger 以 PASS 越过**未裁决**的历史终态矛盾："
                    "必须先有 §2.15 有效 GPT 裁决，或不要把该任务写进 ledger（fail-closed）",
                )
            )

    removed = sorted(
        set(committed_reviewed_ids) - set(reviewed_ids), key=planner.task_id_sort_key
    )

    for task_id in removed:
        issues.append(
            own_issue(
                REASON_LEDGER_ENTRY_REMOVED,
                f"{task_id}: 候选 ledger 删除了已提交的有效条目 ⇒ 破坏 review 连续性"
                "（fail-closed，绝不接受指针回退）",
            )
        )

    section: dict[str, Any] = {
        "available": ledger_view.get("available"),
        "bindings": bindings,
        "chain": chain,
        "coverage_floor": ledger_view.get("coverage_floor"),
        "duplicate_tasks": list(ledger_view.get("duplicate_tasks") or []),
        "entry_count": len(facts),
        "removed_task_ids": removed,
        "reviewed_ids": sorted(reviewed_ids, key=planner.task_id_sort_key),
        "schema": ledger_view.get("schema"),
        "schema_version": ledger_view.get("schema_version"),
        "task_ids": [fact["task_id"] for fact in facts],
        "valid_entry_count": len(valid_entries),
    }

    return section, issues



# ============================================================
# 候选 PROJECT_STATE（指针连续性 + blocker / Phase / 交易安全不变量）
# ============================================================


def candidate_pointer_drift_issues(
    candidate_state: Mapping[str, Any],
    *,
    statuses: Mapping[str, str],
    tasks_dir: Path,
) -> list[dict[str, str]]:
    """候选执行指针（``last_completed_task`` / ``current_task``）vs results 的确定性漂移。

    与 :func:`orchestrator.planner_snapshot.pointer_section` 的唯一差异：``last_completed_task``
    对齐**最新 completed result**，而不是「最新 terminal（含 blocked）」。真实仓库里 blocked
    result 排在最后时（例如 ``PHASE3_3_DATA`` 使后续任务 blocked），``last_completed_task``
    仍应指向最后一个 completed result，不得被误判为「落后于 results」。

    ``last_reviewed_task`` 的落后 / 超前属于 formal review backlog 事实，已在上游
    （``REASON_LAST_REVIEWED_REGRESSION`` / ``REASON_LAST_REVIEWED_AHEAD``）单独判定，
    此处不再重复；``current_task`` 指向终态 result 仍是执行指针漂移。
    """

    candidate_payload = dict(candidate_state)

    statuses_payload = dict(statuses)

    prefix = planner.project_task_prefix(candidate_payload)

    terminal_ids = planner.terminal_result_ids(statuses_payload, prefix)

    completed_ids = [
        task_id for task_id in terminal_ids if statuses_payload.get(task_id) == "completed"
    ]

    latest_completed = completed_ids[-1] if completed_ids else None

    issues: list[dict[str, str]] = []

    for issue in planner.single_pointer_issues(
        "last_completed_task",
        planner.pointer_value(candidate_payload, "last_completed_task"),
        statuses_payload,
        tasks_dir,
        latest_completed,
    ):
        issues.append(
            own_issue(
                REASON_STATE_POINTER_DRIFT,
                f"{issue['code']}: {issue['detail']}",
            )
        )

    current = planner.pointer_value(candidate_payload, "current_task")

    if current is not None and statuses_payload.get(current) in orch.TERMINAL_RESULT_STATUSES:
        issues.append(
            own_issue(
                REASON_STATE_POINTER_DRIFT,
                f"{planner.ISSUE_POINTER_BEHIND_RESULTS}: current_task={current} 已有终态 result="
                f"{statuses_payload.get(current)}：PROJECT_STATE 指针落后于 results",
            )
        )

    return issues


def candidate_state_facts(
    candidate_state: Mapping[str, Any],
    *,
    committed_state: Mapping[str, Any],
    statuses: Mapping[str, str],
    tasks_dir: Path,
    candidate_reviewed_ids: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """候选 ``PROJECT_STATE`` 事实：指针不变量 + blocker / Phase / 交易安全。"""

    issues: list[dict[str, str]] = []

    candidate_lr = planner.pointer_value(dict(candidate_state), "last_reviewed_task")

    committed_lr = planner.pointer_value(dict(committed_state), "last_reviewed_task")

    reviewed = set(candidate_reviewed_ids)

    if committed_lr is not None:
        if candidate_lr is None:
            issues.append(
                own_issue(
                    REASON_LAST_REVIEWED_REGRESSION,
                    f"候选删除了已提交的 last_reviewed_task={committed_lr}：review 指针不得倒退 / "
                    "消失（fail-closed）",
                )
            )

        elif planner.is_newer(committed_lr, candidate_lr):
            issues.append(
                own_issue(
                    REASON_LAST_REVIEWED_REGRESSION,
                    f"候选 last_reviewed_task={candidate_lr} 倒退到已提交 {committed_lr} 之前："
                    "review 连续性不得回退（fail-closed）",
                )
            )

    if candidate_lr is not None:
        status = statuses.get(candidate_lr)

        if candidate_lr not in reviewed:
            issues.append(
                own_issue(
                    REASON_LAST_REVIEWED_AHEAD,
                    f"候选 last_reviewed_task={candidate_lr} 在候选 ledger 中没有对应有效条目："
                    "指针超前于已 review 事实（fail-closed，绝不代改指针）",
                )
            )

        if status not in orch.TERMINAL_RESULT_STATUSES:
            issues.append(
                own_issue(
                    REASON_LAST_REVIEWED_AHEAD,
                    f"候选 last_reviewed_task={candidate_lr} 没有终态 result"
                    f"（status={status if status is not None else 'pending'}）："
                    "指针超出 results（fail-closed）",
                )
            )

    # last_completed_task 对齐最新 completed result（不是 latest terminal 含 blocked），
    # current_task 指向终态 result 才算执行指针漂移；两者确定性计算、只报告，绝不代改指针。
    issues.extend(
        candidate_pointer_drift_issues(candidate_state, statuses=statuses, tasks_dir=tasks_dir)
    )

    # history_blockers 里的归档 blocker（如 PHASE3_3_DATA）同样受只读保护：
    # 候选删除它们也必须 fail-closed，否则 GPT 归档后 PHASE3_3_DATA 失去保护。
    committed_gates = (committed_state.get("blockers") or []) + (
        committed_state.get("history_blockers") or []
    )
    committed_blockers = set(planner.gate_codes(committed_gates))

    candidate_gates = (candidate_state.get("blockers") or []) + (
        candidate_state.get("history_blockers") or []
    )
    candidate_blockers = set(planner.gate_codes(candidate_gates))

    for code in sorted(committed_blockers - candidate_blockers):
        if code == PHASE3_3_BLOCKER:
            issues.append(
                own_issue(
                    REASON_PHASE3_3_BLOCKER_REMOVED,
                    f"候选删除了 {PHASE3_3_BLOCKER} blocker：业务 Phase 3.3 数据资格不得因 "
                    "review / 裁决 / 代码而解除（fail-closed）",
                )
            )

            continue

        issues.append(
            own_issue(
                REASON_BLOCKER_REMOVED,
                f"候选删除了已提交 blocker={code}：blocker 只能由 GPT 显式裁决并留痕，"
                "禁止在收口写入里静默删除（fail-closed）",
            )
        )

    rank = phase_rank(candidate_state.get("phase"))

    if rank is not None and rank >= PHASE_3_4_RANK:
        issues.append(
            own_issue(
                REASON_PHASE3_4_ENTRY,
                f"候选 phase={candidate_state.get('phase')!r} 表示已进入 Phase 3.4（或更后）："
                "Phase 3.3 数据资格未解除前严禁提前进入（fail-closed）",
            )
        )

    committed_invariants = set(planner.invariant_list(dict(committed_state)))

    candidate_invariants = set(planner.invariant_list(dict(candidate_state)))

    for required in planner.REQUIRED_SAFETY_INVARIANTS:
        if required not in candidate_invariants:
            issues.append(
                own_issue(
                    REASON_TRADING_SAFETY_CHANGED,
                    f"候选缺少安全不变量 {required}"
                    f"（已提交{'包含' if required in committed_invariants else '未包含'}）："
                    "交易安全开关绝不因收口写入而改变（fail-closed）",
                )
            )

    section: dict[str, Any] = {
        "blocker_codes": sorted(candidate_blockers),
        "candidate_reviewed_ids": sorted(reviewed, key=planner.task_id_sort_key),
        "committed_blocker_codes": sorted(committed_blockers),
        "human_gate_codes": planner.gate_codes(candidate_state.get("human_gates")),
        "invariants": planner.invariant_list(dict(candidate_state)),
        "last_completed_task": planner.pointer_value(dict(candidate_state), "last_completed_task"),
        "last_reviewed_task": candidate_lr,
        "phase": candidate_state.get("phase"),
        "phase_rank": list(rank) if rank is not None else None,
        "status": candidate_state.get("status"),
        "trading_safety_invariants": {
            "required": list(planner.REQUIRED_SAFETY_INVARIANTS),
            "present": [
                item for item in planner.REQUIRED_SAFETY_INVARIANTS if item in candidate_invariants
            ],
        },
    }

    return section, issues



# ============================================================
# 候选 base（HEAD + 摘要）漂移
# ============================================================


def compare_expected_head(
    expected_raw: object,
    head: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把候选 ``base.head_sha`` 与 observed HEAD 比对（stale ⇒ fail-closed）。"""

    issues: list[dict[str, str]] = []

    expected, expected_error = normalize_head_sha(expected_raw)

    observed = head.get("observed_head_sha")

    if expected_error is not None:
        issues.append(own_issue(REASON_CANDIDATE_BASE_INVALID, expected_error))

    elif expected is None:
        issues.append(
            own_issue(
                REASON_CANDIDATE_BASE_INVALID,
                "候选 base.head_sha 缺失 / 为空：无法证明候选基于当前 HEAD，fail-closed",
            )
        )

    matches: bool | None = None

    stale: bool | None = None

    if expected_error is None and expected is not None and isinstance(observed, str):
        matches = observed.startswith(expected)

        stale = not matches

        if stale:
            issues.append(
                own_issue(
                    REASON_STALE_REMOTE_HEAD,
                    f"base.head_sha={expected} != observed_head_sha={observed}：远端 HEAD 已前进 / "
                    "变化，候选失效 ⇒ fail-closed；绝不自动 merge / rebase / force push，"
                    "请基于最新 HEAD 重建候选",
                )
            )

    section: dict[str, Any] = {
        "expected_head_error": expected_error,
        "expected_head_short": expected[:8] if expected else None,
        "expected_head_sha": expected,
        "matches_expected": matches,
        "observed_head_sha": observed,
        "observed_head_short": head.get("observed_head_short"),
        "stale": stale,
    }

    return section, issues


def base_drift_issues(
    base: Mapping[str, Any],
    committed: Mapping[str, Any],
) -> list[dict[str, str]]:
    """候选 base 摘要 vs 已提交事实（任一漂移 ⇒ fail-closed）。"""

    checks = (
        (
            "results_digest",
            committed["results_digest"]["digest"],
            REASON_BASE_RESULTS_DRIFT,
            "候选基于的 results 摘要已过期：可能已有新 result 落盘",
        ),
        (
            "tasks_digest",
            committed["tasks_digest"]["digest"],
            REASON_BASE_TASKS_DRIFT,
            "候选基于的 tasks 摘要已过期：可能已有新 task 落盘",
        ),
        (
            "project_state_sha256",
            committed["project_state"]["blob_sha256"],
            REASON_BASE_STATE_DRIFT,
            "候选基于的 PROJECT_STATE 摘要已过期：state 已被改写",
        ),
        (
            "review_ledger_sha256",
            committed["review_ledger"]["blob_sha256"],
            REASON_BASE_LEDGER_DRIFT,
            "候选基于的 review ledger 摘要已过期：ledger 已被改写",
        ),
        (
            "adjudication_store_sha256",
            committed["adjudication_store"]["blob_sha256"],
            REASON_BASE_ADJUDICATION_DRIFT,
            "候选基于的 adjudication store 摘要已过期：store 已被改写 / 新增",
        ),
    )

    issues: list[dict[str, str]] = []

    for key, actual, code, reason in checks:
        declared = base.get(key)

        if declared != actual:
            issues.append(
                own_issue(
                    code,
                    f"{reason}（base.{key}={declared!r} != committed={actual!r}，fail-closed）",
                )
            )

    return issues


# ============================================================
# 职责边界 / 确定性契约（机器可读）
# ============================================================


def authority_section() -> dict[str, Any]:
    """本工具的职责边界：只验证候选，绝不应用 / 签发 / 推进。"""

    return {
        "schema": AUTHORITY_SCHEMA,
        "review_authority": "gpt_only",
        "tool_role": "read_only_precondition",
        "candidate_ready_is_not_review_verdict": True,
        "candidate_ready_triggers_executor_write": False,
        "candidate_ready_triggers_auto_push": False,
        "candidate_ready_lifts_phase_gate": False,
        "model_calls": False,
        "mutation_switches_exposed": [],
        "network_access": False,
        "tool_can_advance_pointer": False,
        "tool_can_advance_state": False,
        "tool_can_apply_candidate": False,
        "tool_can_decide_phase": False,
        "tool_can_lift_blocker": False,
        "tool_can_sign_adjudication": False,
        "tool_can_sign_review": False,
        "tool_can_update_project_state": False,
        "tool_can_write_adjudication": False,
        "tool_can_write_results": False,
        "tool_can_write_review_ledger": False,
        "tool_can_write_tasks": False,
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约（机器可读；不含事实）。"""

    return {
        "creates_files": False,
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "digest_excluded_keys": list(FACTS_EXCLUDED_KEYS),
        "external_processes": (
            "read-only git via orchestrator.review_binding (log/ls-tree/cat-file/hash-object)"
        ),
        "model_calls": False,
        "network_access": False,
        "wall_clock_in_content_identity": False,
        "writes_adjudication_store": False,
        "writes_project_state": False,
        "writes_results": False,
        "writes_review_ledger": False,
        "writes_tasks": False,
    }



# ============================================================
# 只读预检构建（组合已提交事实 + 候选校验）
# ============================================================


def resolve_paths(
    root: Path | None,
    state_path: Path | None,
    tasks_dir: Path | None,
    results_dir: Path | None,
    ledger_path: Path | None,
    adjudication_store_path: Path | None,
) -> dict[str, Path]:
    """解析各路径（默认值与其他 control-plane 工具完全一致）。"""

    resolved_root = Path(root) if root is not None else ROOT

    return {
        "adjudication_store": (
            Path(adjudication_store_path)
            if adjudication_store_path is not None
            else resolved_root / binding.ADJUDICATION_STORE_RELATIVE_PATH
        ),
        "ledger": (
            Path(ledger_path)
            if ledger_path is not None
            else resolved_root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
        ),
        "results": (
            Path(results_dir) if results_dir is not None else resolved_root / ".ai" / "results"
        ),
        "root": resolved_root,
        "state": (
            Path(state_path)
            if state_path is not None
            else resolved_root / ".ai" / "PROJECT_STATE.json"
        ),
        "tasks": (
            Path(tasks_dir) if tasks_dir is not None else resolved_root / ".ai" / "tasks"
        ),
    }


def ledger_valid_ids(payload: Mapping[str, Any] | None, error: str | None) -> list[str]:
    """台账里**有效**条目的 task_id（复用 §2.8 校验，只读）。"""

    view, _ = ledger_mod.validate_review_ledger(
        dict(payload) if isinstance(payload, Mapping) else None, error
    )

    return [
        str(entry["task_id"])
        for entry in view["entries"]
        if entry.get("valid") is True and entry.get("task_id") is not None
    ]


def candidate_task_ids(
    store_facts: Mapping[str, Any],
    candidate_ledger: Mapping[str, Any] | None,
    candidate_state: Mapping[str, Any] | None,
) -> list[str]:
    """候选涉及的全部 task_id（store 条目 + ledger 有效条目 + state 指针）。"""

    ids: set[str] = set(store_facts["store_section"].get("task_ids") or [])

    if candidate_ledger is not None:
        ids.update(ledger_valid_ids(candidate_ledger, None))

    if candidate_state is not None:
        for key in ("current_task", "last_completed_task", "last_reviewed_task"):
            value = planner.pointer_value(dict(candidate_state), key)

            if value is not None:
                ids.add(value)

    return sorted(ids, key=planner.task_id_sort_key)


def precondition_exit_code(*, reason_codes: Sequence[str], blocking: Sequence[str]) -> int:
    """退出码：``0`` candidate-ready / ``2`` fail-closed / ``3`` 输入事实不可用。"""

    codes = set(reason_codes)

    if codes & set(CANDIDATE_UNAVAILABLE_CODES):
        return EXIT_INPUT_UNAVAILABLE

    if planner.ISSUE_PROJECT_STATE_UNREADABLE in codes:
        return EXIT_INPUT_UNAVAILABLE

    if blocking:
        return EXIT_FAIL_CLOSED

    return EXIT_OK



def precondition_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in payload.items() if key not in FACTS_EXCLUDED_KEYS}


def precondition_facts_digest(payload: Mapping[str, Any]) -> str:
    """``precondition_digest``：相同候选 + 相同已提交事实 ⇒ 相同 digest（幂等）。"""

    return canonical_digest(precondition_facts(payload))


def candidate_sections(
    candidate: Mapping[str, Any] | None,
) -> tuple[dict[str, Any | None], list[dict[str, str]]]:
    """候选三段（裁决 store / review ledger / PROJECT_STATE）的存在性 + 形状校验。"""

    issues: list[dict[str, str]] = []

    resolved: dict[str, Any | None] = {}

    for name in ("adjudication_store", "review_ledger", "project_state"):
        value = candidate.get(name) if isinstance(candidate, Mapping) else None

        if value is not None and not isinstance(value, Mapping):
            issues.append(
                own_issue(
                    REASON_CANDIDATE_SECTION_INVALID,
                    f"候选 {name} 必须是 JSON 对象（fail-closed）",
                )
            )

            resolved[name] = None

            continue

        resolved[name] = dict(value) if isinstance(value, Mapping) else None

    return resolved, issues


def empty_head_comparison(head: Mapping[str, Any]) -> dict[str, Any]:
    """候选没有可用 base 时的空比对段（绝不放行，由 BASE_MISSING 表达）。"""

    return {
        "expected_head_error": None,
        "expected_head_short": None,
        "expected_head_sha": None,
        "matches_expected": None,
        "observed_head_sha": head.get("observed_head_sha"),
        "observed_head_short": head.get("observed_head_short"),
        "stale": None,
    }


def unavailable_manifest(task_id: str) -> dict[str, Any]:
    """§2.8 manifest 构建失败时的 fail-closed 占位（绝不猜测身份 / 绝不放行）。"""

    return {
        "task_id": task_id,
        "result": {},
        "commit": {},
        "adjudication": {"state": None, "valid": None, "adjudication": None, "reason_codes": []},
        "binding": {
            "facts_complete": False,
            "facts_ready": False,
            "adjudicated": False,
            "facts_ready_source": binding.FACTS_READY_SOURCE_NONE,
            "reason_codes": [REASON_COMMITTED_FACTS_UNAVAILABLE],
        },
        "issues": [],
    }



def build_review_closure_precondition(
    *,
    candidate: Mapping[str, Any] | None = None,
    candidate_source: str = "unspecified",
    candidate_issues: Sequence[Mapping[str, Any]] = (),
    root: Path | None = None,
    state_path: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    ledger_path: Path | None = None,
    adjudication_store_path: Path | None = None,
    generated_at: str | None = None,
    as_of: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
    backlog_builder: BacklogBuilder | None = None,
    integrity_builder: IntegrityBuilder | None = None,
    manifest_builder: ManifestBuilder | None = None,
    live_facts_provider: LiveFactsProvider | None = None,
) -> dict[str, Any]:
    """构建 GPT Review 收口写入前的**只读**原子一致性预检事实包。

    组合「最新 HEAD + results/tasks 摘要 + §2.15 裁决校验 + §2.8 binding + §2.11 backlog +
    §2.9 integrity + 候选 PROJECT_STATE 指针事实」，任何 stale / 不连续 / 越权 / 安全不变量
    变化都 fail-closed。**绝不写入任何 managed 状态，绝不应用候选**。

    各事实来源都允许注入已构建好的结果（便于测试与复用）；默认逐项调用既有只读 builder。
    """

    paths = resolve_paths(
        root, state_path, tasks_dir, results_dir, ledger_path, adjudication_store_path
    )

    resolved_root = paths["root"]

    resolved_state = paths["state"]

    resolved_tasks = paths["tasks"]

    resolved_results = paths["results"]

    resolved_ledger = paths["ledger"]

    resolved_store = paths["adjudication_store"]

    resolved_generated_at = generated_at if generated_at is not None else orch.now_iso()

    resolved_snapshot = (
        dict(snapshot)
        if isinstance(snapshot, Mapping)
        else planner.build_planner_snapshot(
            root=resolved_root,
            state_path=resolved_state,
            tasks_dir=resolved_tasks,
            results_dir=resolved_results,
            generated_at=generated_at,
        )
    )

    committed = committed_facts(
        root=resolved_root,
        state_path=resolved_state,
        tasks_dir=resolved_tasks,
        results_dir=resolved_results,
        ledger_path=resolved_ledger,
        adjudication_store_path=resolved_store,
        snapshot=resolved_snapshot,
        backlog_builder=backlog_builder,
        integrity_builder=integrity_builder,
        generated_at=resolved_generated_at,
    )

    head_section, head_issues = head_facts(snapshot=resolved_snapshot)

    issues: list[dict[str, Any]] = [dict(issue) for issue in candidate_issues]

    issues.extend(head_issues)

    if not committed["project_state"]["readable"]:
        issues.append(
            inherited_issue(
                planner.ISSUE_PROJECT_STATE_UNREADABLE,
                f"{resolved_state}: PROJECT_STATE 不可读（fail-closed，绝不猜测指针）",
                "orchestrator.planner_snapshot",
            )
        )

    statuses = planner.result_statuses(resolved_results)

    candidate_payload = dict(candidate) if isinstance(candidate, Mapping) else None

    if candidate_payload is None:
        issues.append(
            own_issue(
                REASON_CANDIDATE_MISSING,
                "未提供候选（--candidate <显式临时文件> 或 --candidate - / stdin）："
                "无法预检，fail-closed",
            )
        )

    else:
        issues.extend(validate_candidate_envelope(candidate_payload))

    base_raw = candidate_payload.get("base") if candidate_payload is not None else None

    base = dict(base_raw) if isinstance(base_raw, Mapping) else None

    if base is None:
        base_comparison = empty_head_comparison(head_section)

    else:
        base_comparison, base_head_issues = compare_expected_head(
            base.get("head_sha"), head_section
        )

        issues.extend(base_head_issues)

        if committed["project_state"]["readable"]:
            issues.extend(base_drift_issues(base, committed))


    sections, section_issues = candidate_sections(candidate_payload)

    issues.extend(section_issues)

    store = sections["adjudication_store"]

    candidate_ledger = sections["review_ledger"]

    candidate_state = sections["project_state"]

    store_facts, store_issues = collect_candidate_store(store)

    issues.extend(store_issues)

    task_ids = candidate_task_ids(store_facts, candidate_ledger, candidate_state)

    provider_issues: list[dict[str, Any]] = []

    def default_provider(task_id: str) -> dict[str, Any]:
        try:
            return adjudication.review_binding_live_facts(
                task_id,
                root=resolved_root,
                tasks_dir=resolved_tasks,
                results_dir=resolved_results,
            )

        except Exception as exc:  # noqa: BLE001 - 事实来源异常必须 fail-closed
            provider_issues.append(
                own_issue(
                    REASON_COMMITTED_FACTS_UNAVAILABLE,
                    f"{task_id}: 无法解析 §2.8 live 事实（fail-closed）: "
                    f"{type(exc).__name__}: {exc}",
                )
            )

            return {"result_payload": None, "commit_resolved": False}

    provider = live_facts_provider if live_facts_provider is not None else default_provider

    adj_section, adj_issues, state_by_task = evaluate_candidate_adjudication(
        store_facts, task_ids=task_ids, live_provider=provider, as_of=as_of
    )

    issues.extend(adj_issues)

    builder = (
        manifest_builder
        if manifest_builder is not None
        else lambda task_id: binding.build_review_binding_manifest(
            task_id,
            root=resolved_root,
            tasks_dir=resolved_tasks,
            results_dir=resolved_results,
            adjudication_store_path=resolved_store,
        )
    )

    manifest_cache: dict[str, dict[str, Any]] = {}

    builder_issues: list[dict[str, Any]] = []

    def cached_builder(task_id: str) -> dict[str, Any]:
        if task_id not in manifest_cache:
            try:
                manifest_cache[task_id] = builder(task_id)

            except Exception as exc:  # noqa: BLE001 - 事实来源异常必须 fail-closed
                builder_issues.append(
                    own_issue(
                        REASON_COMMITTED_FACTS_UNAVAILABLE,
                        f"{task_id}: 无法构建 §2.8 review binding manifest（fail-closed）: "
                        f"{type(exc).__name__}: {exc}",
                    )
                )

                manifest_cache[task_id] = unavailable_manifest(task_id)

        return manifest_cache[task_id]

    committed_ledger_payload, committed_ledger_error = ledger_mod.load_review_ledger(
        resolved_ledger
    )

    committed_reviewed_ids = ledger_valid_ids(committed_ledger_payload, committed_ledger_error)

    if candidate_ledger is not None:
        ledger_section, ledger_issues = candidate_ledger_facts(
            candidate_ledger,
            base_builder=cached_builder,
            state_by_task=state_by_task,
            results_dir=resolved_results,
            committed_reviewed_ids=committed_reviewed_ids,
        )

        issues.extend(ledger_issues)

    else:
        ledger_section = {
            "available": False,
            "present": False,
            "reviewed_ids": [],
        }

    if candidate_state is not None:
        state_facts, state_issues = candidate_state_facts(
            candidate_state,
            committed_state=committed["project_state_payload"],
            statuses=statuses,
            tasks_dir=resolved_tasks,
            candidate_reviewed_ids=ledger_section.get("reviewed_ids") or [],
        )

        issues.extend(state_issues)

    else:
        state_facts = {"present": False}

    binding_tasks = [
        binding_task_facts(task_id, cached_builder(task_id)) for task_id in task_ids
    ]

    committed["review_binding"] = {
        "source": "orchestrator.review_binding.build_review_binding_manifest",
        "tasks": binding_tasks,
    }

    issues.extend(builder_issues)

    issues.extend(provider_issues)

    ordered_issues = sorted_issues(issues)

    reason_codes = sorted_unique(issue["code"] for issue in ordered_issues)

    exit_code = precondition_exit_code(reason_codes=reason_codes, blocking=reason_codes)

    candidate_ready = not ordered_issues and candidate_payload is not None

    candidate_section: dict[str, Any] = {
        "adjudication": adj_section,
        "base": base,
        "present": candidate_payload is not None,
        "project_state": state_facts,
        "review_ledger": ledger_section,
        "schema": candidate_payload.get("schema") if candidate_payload is not None else None,
        "schema_version": (
            candidate_payload.get("schema_version") if candidate_payload is not None else None
        ),
        "sections": {
            "adjudication_store_present": store is not None,
            "project_state_present": candidate_state is not None,
            "review_ledger_present": candidate_ledger is not None,
        },
        "source": candidate_source,
        "task_ids": task_ids,
    }

    closure: dict[str, Any] = {
        "auto_push": False,
        "candidate_ready": candidate_ready,
        "candidate_ready_semantics": (
            "candidate write set is atomically consistent with committed-current facts; "
            "NOT a review verdict and NOT a permission to write"
        ),
        "candidate_ready_triggers_auto_push": False,
        "candidate_ready_triggers_executor_write": False,
        "committed_current": {
            "head_resolved": bool(head_section["resolved"]),
            "project_state_readable": bool(committed["project_state"]["readable"]),
            "review_backlog_available": bool(committed["review_backlog"].get("available")),
            "review_integrity_available": bool(committed["review_integrity"].get("available")),
        },
        "executor_write_triggered": False,
        "phase_gate_lifted": False,
        "review_completed": False,
        "review_verdict_issued": False,
        "state_advanced": False,
    }

    payload: dict[str, Any] = {
        "as_of": as_of,
        "authority": authority_section(),
        "base": base_comparison,
        "candidate": candidate_section,
        "candidate_ready": candidate_ready,
        "candidate_source": candidate_source,
        "closure": closure,
        "committed_current": committed,
        "generated_at": resolved_generated_at,
        "head": head_section,
        "issues": ordered_issues,
        "precondition_kind": "review_closure_precondition",
        "read_only": True,
        "reason_codes": reason_codes,
        "schema": PRECONDITION_SCHEMA,
        "schema_version": PRECONDITION_SCHEMA_VERSION,
        "summary": {
            "candidate_present": candidate_payload is not None,
            "candidate_ready": candidate_ready,
            "candidate_task_count": len(task_ids),
            "error_count": len(ordered_issues),
            "exit_code": exit_code,
            "expected_head_sha": base_comparison.get("expected_head_sha"),
            "issue_count": len(ordered_issues),
            "observed_head_sha": head_section["observed_head_sha"],
            "observed_head_short": head_section["observed_head_short"],
            "stale_remote_head": bool(base_comparison.get("stale")),
            "warning_count": 0,
        },
    }

    payload["precondition_digest"] = precondition_facts_digest(payload)

    payload["determinism"] = determinism_section()

    return payload



def binding_task_facts(task_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """单任务 §2.8 事实子集（**复用** §2.11 ``manifest_item_facts``，单一口径）。"""

    return backlog_mod.manifest_item_facts(dict(manifest), task_id)


# ============================================================
# 渲染 / 只读 CLI（python -m orchestrator.review_closure_precondition）
# ============================================================


def render_review_closure_precondition(
    payload: Mapping[str, Any], *, ensure_ascii: bool = True
) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_closure_precondition",
        description=(
            "GPT Review 收口写入前的原子一致性只读预检（GOLD-045）：把候选的裁决 store / "
            "review ledger / PROJECT_STATE 与已提交 HEAD、摘要、§2.15 裁决校验、§2.8 binding、"
            "§2.11 backlog、§2.9 integrity 一次性比对；只验证候选，绝不写入 / 应用 / 推进任何状态。"
        ),
        allow_abbrev=False,
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
    )

    parser.add_argument(
        "--candidate",
        default=None,
        help=(
            "候选写集来源：显式临时文件路径 / <root>/.ai/runtime/** 路径，或 ``-`` / ``stdin`` "
            "从标准输入读取（managed 状态路径一律拒绝；默认未提供即 fail-closed）"
        ),
    )

    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="PROJECT_STATE 路径（默认 <root>/.ai/PROJECT_STATE.json）",
    )

    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help="tasks 目录（默认 <root>/.ai/tasks）",
    )

    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="results 目录（默认 <root>/.ai/results）",
    )

    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help=f"review ledger 路径（默认 <root>/{ledger_mod.REVIEW_LEDGER_RELATIVE_PATH}）",
    )

    parser.add_argument(
        "--adjudication-store",
        type=Path,
        default=None,
        help=(
            "§2.15 历史矛盾裁决 store（默认 "
            f"<root>/{binding.ADJUDICATION_STORE_RELATIVE_PATH}）；只读，绝不创建或改写"
        ),
    )

    parser.add_argument(
        "--as-of",
        default=None,
        help="裁决有效期参照时间（带时区 ISO；候选声明 expires_at 时必须提供）",
    )

    parser.add_argument(
        "--generated-at",
        default=None,
        help="固定 generated_at（便于审计；默认取当前时间，且不参与内容 digest）",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "可选：把预检 JSON 写到用户显式指定的受控路径"
            "（只允许 <root>/.ai/runtime/** 或系统临时目录；写其它位置一律拒绝）"
        ),
    )

    return parser



def load_candidate_argument(
    root: Path, candidate: str | None
) -> tuple[dict[str, Any] | None, str, list[dict[str, str]]]:
    """把 CLI ``--candidate`` 解析为 ``(payload, source, issues)``（只读）。"""

    if candidate is None:
        return None, "unspecified", []

    source, reason = resolve_candidate_source(root, candidate)

    if source is None:
        return (
            None,
            "rejected",
            [own_issue(REASON_CANDIDATE_UNREADABLE, str(reason))],
        )

    if source == CANDIDATE_SOURCE_STDIN:
        text = sys.stdin.read()

    else:
        text, issues = load_candidate_file(Path(source))

        if text is None:
            return None, source, issues

    payload, parse_issues = parse_candidate_text(text)

    return payload, source, parse_issues


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：默认写 stdout，``--output`` 时写受控文件；摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    candidate, candidate_source, candidate_issues = load_candidate_argument(
        root, args.candidate
    )

    payload = build_review_closure_precondition(
        candidate=candidate,
        candidate_source=candidate_source,
        candidate_issues=candidate_issues,
        root=root,
        state_path=Path(args.state) if args.state is not None else None,
        tasks_dir=Path(args.tasks_dir) if args.tasks_dir is not None else None,
        results_dir=Path(args.results_dir) if args.results_dir is not None else None,
        ledger_path=Path(args.ledger) if args.ledger is not None else None,
        adjudication_store_path=(
            Path(args.adjudication_store) if args.adjudication_store is not None else None
        ),
        generated_at=args.generated_at,
        as_of=args.as_of,
    )

    rendered = render_review_closure_precondition(payload)

    if args.output is None:
        sys.stdout.write(rendered)
        sys.stdout.flush()

    else:
        target, reason = mutation_precondition.resolve_output_target(root, args.output)

        if target is None:
            print(
                f"[error] {snapshot_output.ISSUE_OUTPUT_PATH_REJECTED}: {reason}",
                file=sys.stderr,
            )

            return snapshot_output.EXIT_OUTPUT_REJECTED

        snapshot_output.write_snapshot_output(target, rendered)

        print(f"[info] review closure precondition 已写入受控路径: {target}", file=sys.stderr)

    for issue in payload["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    closure = payload["closure"]

    print(
        "[precondition] head={head} expected={expected} stale={stale} "
        "candidate_ready={ready} review_completed={reviewed} codes={codes}".format(
            head=payload["head"]["observed_head_short"],
            expected=payload["base"]["expected_head_short"],
            stale=payload["base"]["stale"],
            ready=closure["candidate_ready"],
            reviewed=closure["review_completed"],
            codes=",".join(payload["reason_codes"]),
        ),
        file=sys.stderr,
    )

    return int(payload["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

