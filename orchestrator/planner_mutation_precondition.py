"""GPT Planner 写队列前的远端 HEAD 并发保护事实包（GOLD-038）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2.10（GPT Rolling Queue Autopilot）要求 GPT 在队列低水位时
补 follow-on task、并在必要时更新 ``.ai/PROJECT_STATE.json`` / ``.ai/tasks/**``。但 GPT 运行在
云端：它**读取事实**（§2.5 planner snapshot / §2.10 refill 事实包 / §2.11 review backlog）与
真正**写队列**之间存在时间窗口，而 Executor 在同一窗口里会完成当前任务并 ``push`` result +
completion commit。没有并发保护时，GPT 会基于**过期快照**写队列，可能覆盖或错判刚刚完成的
工作。本模块就是这段窗口的确定性、只读保护事实包。

它回答的唯一问题
----------------
「我上次读到的远端 HEAD，与我即将写入时的 HEAD 是否完全一致；``PROJECT_STATE`` 声称的
``current_task`` / ``last_completed_task`` 是否仍与 results / HEAD 事实一致？」

设计原则（与既有 control-plane 工具一致）
------------------------------------------
1. **复用，不复制**：branch / HEAD 事实直接复用
   :func:`orchestrator.planner_snapshot.git_section`（经 planner snapshot 的 ``git`` 段，零额外
   IO）；queue head / task_queue / 水位 / formal backlog 指针分别复用
   :func:`orchestrator.planner_refill_request.build_planner_refill_request` 与
   :func:`orchestrator.review_backlog.build_review_backlog_manifest`（后者内部又复用
   :mod:`orchestrator.review_binding`）。本模块**不**新增第二套 readiness / 依赖 / 终态 /
   task 资格 / commit 身份算法；
2. **确定性**：``precondition_digest`` 只覆盖事实（``generated_at`` / ``determinism`` / 自身
   被显式排除），相同仓库事实必然得到相同 digest；
3. **fail-closed**：``--expected-head-sha`` 与当前 ``observed_head_sha`` 不一致 ⇒
   ``STALE_REMOTE_HEAD``；HEAD 不可解析 ⇒ ``GIT_INFO_UNAVAILABLE``；
   ``PROJECT_STATE.branch`` 与 observed HEAD 分支漂移 ⇒ ``STATE_BRANCH_DRIFT``；
   ``current_task`` / ``last_completed_task`` 与 results 事实漂移 ⇒ 复用 planner snapshot 的
   稳定 code 并叠加 ``STATE_RESULT_DRIFT``。命中任一项都显式
   ``planner_mutation.allowed=false`` + ``forbidden=true`` + ``requires_reread=true``；
   **绝不猜测、绝不自动 merge / rebase / force push、绝不隐藏远端变化**；
4. **默认零写入**：唯一写操作是显式 ``--output``，先拒绝 ``<root>/.ai/**``（``runtime`` 除外）
   的**任何**目标，再**复用** :mod:`orchestrator.planner_snapshot_output` 的 fail-closed 路径守卫
   （只允许 ``<root>/.ai/runtime/**`` 或系统临时目录）⇒ 不可能写 ``.ai/tasks`` / ``.ai/results`` /
   ``.ai/PROJECT_STATE.json`` / ``.ai/GPT_REVIEW_LEDGER.json``；
5. **只产事实、不产权限**：本模块**没有**任何 task / state / ledger 写入或 Planner 权限提升；
   它也不阻塞 Executor 执行已批准任务（``executor_execution_blocked=false``）。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- 只读：绝不写 ``.ai/PROJECT_STATE.json`` / ``.ai/tasks/**`` / ``.ai/results/**`` /
  ``.ai/GPT_REVIEW_LEDGER.json``（源码守卫测试锁定）；绝不执行任何 Git 写操作，绝不
  ``fetch`` / ``pull`` / ``merge`` / ``rebase`` / ``force push``（观察到的 HEAD 就是本地
  ``.git`` 中的分支尖端，与远端一致由 Orchestrator 的同步循环保证）；
- 零网络、零数据库、零业务证据、零模型调用；外部进程调用**只经由**
  :mod:`orchestrator.review_binding` 的只读 Git 白名单；
- 不改变 ``PHASE3_3_DATA`` blocker、Phase 3.4 边界、L1~L4 档位、rolling queue、
  ``LIVE_TRADING=false`` 或 ``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``。

用法
----
.. code-block:: text

    python -m orchestrator.planner_mutation_precondition                     # 只读事实包写 stdout
    python -m orchestrator.planner_mutation_precondition --root .            # 指定仓库根
    python -m orchestrator.planner_mutation_precondition --expected-head-sha <上次读到的 HEAD>
    python -m orchestrator.planner_mutation_precondition --output <受控 runtime/临时路径>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放人类可读的
issue 摘要与一行 precondition 提示（绝不参与机器解析）。给出 ``--output`` 时 JSON 只写受控
文件、``stdout`` 保持为空。

退出码：``0`` HEAD 一致且无事实漂移（可安全写队列）/ ``2`` fail-closed（stale / 漂移 /
HEAD 不可解析）/ ``3`` ``PROJECT_STATE`` 不可读 / ``4`` ``--output`` 目标被 fail-closed 拒绝
（此时绝不写任何文件）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_refill_request as refill
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import review_backlog as backlog_mod
from orchestrator import review_ledger as ledger_mod

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
PRECONDITION_SCHEMA = "gold-ai/planner-mutation-precondition/v1"

PRECONDITION_SCHEMA_VERSION = 1

PRECONDITION_AUTHORITY_SCHEMA = "gold-ai/planner-mutation-precondition-authority/v1"

# 只读事实来源（机器可读）：本模块**不**新增第二套判定算法。
FACTS_SOURCES = {
    "remote_head": ".git HEAD/refs via orchestrator.planner_snapshot (read-only)",
    "planner_snapshot": "orchestrator.planner_snapshot.build_planner_snapshot",
    "refill": "orchestrator.planner_refill_request.build_planner_refill_request",
    "review_backlog": (
        "orchestrator.review_backlog.build_review_backlog_manifest (-> orchestrator.review_binding)"
    ),
    "state": ".ai/PROJECT_STATE.json (read-only echo + blob sha256)",
    "tasks": "<root>/.ai/tasks/** bytes (sha256)",
    "results": "<root>/.ai/results/** bytes (sha256)",
}

BACKLOG_SOURCE = FACTS_SOURCES["review_backlog"]

# ---------- 本模块自定义的稳定 reason code ----------
# 其余 code 全部复用既有模块（planner_snapshot / review_ledger ...），保持单一来源。
REASON_STALE_REMOTE_HEAD = "STALE_REMOTE_HEAD"

REASON_EXPECTED_HEAD_SHA_INVALID = "EXPECTED_HEAD_SHA_INVALID"

REASON_STATE_RESULT_DRIFT = "STATE_RESULT_DRIFT"

REASON_STATE_BRANCH_DRIFT = "STATE_BRANCH_DRIFT"

REASON_REVIEW_BACKLOG_FACTS_UNAVAILABLE = "REVIEW_BACKLOG_FACTS_UNAVAILABLE"

# 复用 planner_snapshot 的稳定「state 声称 vs results 事实」漂移 code。
STATE_FACT_REASON_CODES = (
    planner.ISSUE_POINTER_BEHIND_RESULTS,
    planner.ISSUE_POINTER_AHEAD_OF_RESULTS,
    planner.ISSUE_POINTER_MISSING,
    planner.ISSUE_POINTER_UNKNOWN_TASK,
    planner.ISSUE_QUEUE_DECLARATION_MISMATCH,
    planner.ISSUE_QUEUE_TASK_MISSING,
)

# planner_snapshot 的指针类 code（其 detail 以 ``<指针名>=`` / ``<指针名> 缺失`` 开头）。
POINTER_REASON_CODES = (
    planner.ISSUE_POINTER_BEHIND_RESULTS,
    planner.ISSUE_POINTER_AHEAD_OF_RESULTS,
    planner.ISSUE_POINTER_MISSING,
    planner.ISSUE_POINTER_UNKNOWN_TASK,
)

# ``last_reviewed_task`` 指针落后 / 超前属于 **formal review backlog** 事实（§2.11 的口径，
# 由 GPT 以 review 方式处理），**不是**「state 声称的执行指针 vs results / HEAD」漂移：
# 本工具只报告它，不据此禁止写队列（否则任何未 review 的 backlog 都会误判为并发漂移）。
REVIEW_POINTER_DETAIL_PREFIX = "last_reviewed_task"

# 本模块自己的 blocked code（其余 blocked code 动态来自 planner snapshot 的 error issue）。
OWN_BLOCKING_REASON_CODES = (
    REASON_STALE_REMOTE_HEAD,
    REASON_EXPECTED_HEAD_SHA_INVALID,
    REASON_STATE_BRANCH_DRIFT,
    REASON_REVIEW_BACKLOG_FACTS_UNAVAILABLE,
)

# 调用方传入的 HEAD 只接受 7~40 位十六进制（更短 / 非十六进制一律 fail-closed 拒绝比较）。
HEAD_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不作为事实身份；
# - ``precondition_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "precondition_digest", "determinism")

# 输出里**绝不允许**出现的 Review 结论 / 权限提升字段（与 §2.11 同一口径）。
FORBIDDEN_PRECONDITION_KEYS = (
    "verdict",
    "acceptance_summary",
    "reviewed_at",
    "reviewer",
    "reviewer_role",
)

PRECONDITION_ORDERING = {
    "reason_codes": "sorted unique",
    "blocking_reason_codes": "sorted unique",
    "issues": "sorted by (code, detail)",
    "task_queue.declared": "PROJECT_STATE declaration order",
    "task_queue.pending": "planner_snapshot queue order (task_id_sort_key)",
    "tasks_digest.files": "sorted relative posix path",
    "results_digest.files": "sorted relative posix path",
}

EXIT_OK = 0

# HEAD 变化 / 状态漂移 / HEAD 不可解析：fail-closed（只报告，绝不自动修复）。
EXIT_FAIL_CLOSED = 2

EXIT_STATE_UNREADABLE = 3

# ``--output`` 被拒的退出码复用同一受控输出守卫（``snapshot_output.EXIT_OUTPUT_REJECTED`` = 4）。
STATE_SOURCE = ".ai/PROJECT_STATE.json (read-only echo + blob sha256)"

HeadProvider = Callable[[Path], dict[str, Any]]

RefillBuilder = Callable[[dict[str, Any]], dict[str, Any]]

BacklogBuilder = Callable[[], dict[str, Any]]


# ============================================================
# 通用只读工具（确定性）
# ============================================================


def canonical_digest(payload: object) -> str:
    """确定性 JSON 摘要（sort_keys + 紧凑分隔符 ⇒ 相同事实必然相同 digest）。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def own_issue(code: str, detail: str) -> dict[str, str]:
    """本模块自有诊断（fail-closed：只报告，绝不触发任何修复动作）。"""

    return {
        "code": code,
        "severity": planner.SEVERITY_ERROR,
        "detail": detail,
        "source": "orchestrator.planner_mutation_precondition",
    }


def inherited_issue(issue: dict[str, Any]) -> dict[str, str]:
    """给复用来的 issue 标注来源（只读透传 code / severity / detail）。"""

    return {
        "code": str(issue.get("code")),
        "severity": str(issue.get("severity")),
        "detail": str(issue.get("detail")),
        "source": "orchestrator.planner_snapshot",
    }


def is_review_pointer_fact(issue: dict[str, Any]) -> bool:
    """该 issue 是否只是 ``last_reviewed_task`` 指针落后 / 超前（formal review backlog 事实）。"""

    return str(issue.get("code")) in POINTER_REASON_CODES and str(issue.get("detail")).startswith(
        REVIEW_POINTER_DETAIL_PREFIX
    )


def file_facts(path: Path) -> tuple[int | None, str | None]:
    """``(bytes, sha256)``；不存在 / 不可读 ⇒ ``(None, None)``（绝不创建或修改文件）。"""

    try:
        raw = Path(path).read_bytes()

    except OSError:
        return None, None

    return len(raw), hashlib.sha256(raw).hexdigest()


def directory_facts(directory: Path) -> dict[str, Any]:
    """目录内容事实：每个文件的相对 posix 路径 → sha256，以及整体 digest。

    只读遍历；读取失败的文件以 ``UNREADABLE`` 占位（绝不静默忽略，否则 digest 会「漏掉」变化）。
    """

    files: dict[str, str] = {}

    if Path(directory).is_dir():
        for path in sorted(Path(directory).rglob("*")):
            if not path.is_file():
                continue

            relative = path.relative_to(directory).as_posix()

            try:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

            except OSError:
                files[relative] = "UNREADABLE"

    return {"digest": canonical_digest(files), "file_count": len(files), "files": files}


def normalize_expected_head(value: object) -> tuple[str | None, str | None]:
    """规范化调用方传入的 ``expected_head_sha``；非法形式返回 ``(text, reason)``。"""

    if value is None:
        return None, None

    text = str(value).strip().lower()

    if not text:
        return None, None

    if not HEAD_SHA_PATTERN.match(text):
        return text, (
            f"expected_head_sha 非法（需要 7~40 位十六进制字符）: {text!r}："
            "无法证明远端 HEAD 未变化，fail-closed"
        )

    return text, None


def head_facts(
    *,
    root: Path,
    snapshot: dict[str, Any],
    head_provider: HeadProvider | None,
    expected_head_sha: object,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """只读 HEAD 事实 + ``expected_head_sha`` 比对（返回 ``(section, issues, gating_codes)``）。

    默认**直接复用** planner snapshot 的 ``git`` 段（零额外 IO、单一来源）；注入
    ``head_provider`` 时用它替换该段（测试 / 编排复用）。任何「解析不了 HEAD」或
    「与调用方 expected HEAD 不一致」都产生稳定 code 并进入 gating。
    """

    if head_provider is None:
        raw = snapshot.get("git")

        git_facts = dict(raw) if isinstance(raw, dict) else {}

        issues = [
            inherited_issue(issue)
            for issue in (snapshot.get("issues") or [])
            if isinstance(issue, dict)
            and str(issue.get("code")) == planner.ISSUE_GIT_INFO_UNAVAILABLE
        ]

        head_source = FACTS_SOURCES["remote_head"]
    else:
        git_facts = dict(head_provider(root))

        issues = []

        head_source = "injected head provider (编排 / 测试复用)"

    raw_head = git_facts.get("head")

    raw_observed = (
        raw_head.strip().lower() if isinstance(raw_head, str) and raw_head.strip() else None
    )

    resolved = bool(git_facts.get("available")) and raw_observed is not None

    # 解析不了 HEAD 时绝不回显一个「看起来可用」的版本号（fail-closed）。
    observed = raw_observed if resolved else None

    if not resolved and not issues:
        issues.append(
            own_issue(
                planner.ISSUE_GIT_INFO_UNAVAILABLE,
                f"{root}: 无法解析 Git branch/head 事实：无法证明远端 HEAD 未变化 ⇒ "
                "禁止 planner mutation（绝不猜测版本）",
            )
        )

    expected, expected_error = normalize_expected_head(expected_head_sha)

    if expected_error is not None:
        issues.append(own_issue(REASON_EXPECTED_HEAD_SHA_INVALID, expected_error))

    matches: bool | None = None

    stale: bool | None = None

    # 非法 expected 形式**绝不参与比对**（已单独给出 fail-closed code，绝不猜「大概一致」）。
    if expected_error is None and expected is not None and observed is not None:
        matches = observed.startswith(expected)

        stale = not matches

        if stale:
            issues.append(
                own_issue(
                    REASON_STALE_REMOTE_HEAD,
                    f"expected_head_sha={expected} != observed_head_sha={observed}："
                    "远端 HEAD 已前进 / 变化，旧 precondition 失效 ⇒ 禁止 planner mutation；"
                    "绝不自动 merge / rebase / force push，请重新读取最新 HEAD 后重建事实包",
                )
            )

    section: dict[str, Any] = {
        "branch": git_facts.get("branch"),
        "detached": bool(git_facts.get("detached")),
        "expected_head_error": expected_error,
        "expected_head_sha": expected,
        "expected_head_short": expected[:8] if expected else None,
        "matches_expected": matches,
        "observed_head_sha": observed,
        "observed_head_short": observed[:8] if observed else None,
        "resolved": resolved,
        "source": head_source,
        "stale": stale,
        "sync_note": (
            "observed head 来自本地 .git 的分支尖端（Orchestrator 持续 fetch/pull 同步）；"
            "本工具绝不 fetch / pull / merge / rebase / force push"
        ),
    }

    gating = sorted({issue["code"] for issue in issues})

    return section, issues, gating


def state_section(state: dict[str, Any], state_path: Path) -> dict[str, Any]:
    """``PROJECT_STATE`` 的只读事实（原样回显指针 + blob sha256 + 解析后 digest）。"""

    byte_count, blob_sha = file_facts(state_path)

    declared, declared_error = planner.declared_queue(state)

    return {
        "blocker_codes": planner.gate_codes(state.get("blockers")),
        "blob_sha256": blob_sha,
        "branch": state.get("branch"),
        "bytes": byte_count,
        "current_task": planner.pointer_value(state, "current_task"),
        "digest": canonical_digest(state) if state else None,
        "human_gate_codes": planner.gate_codes(state.get("human_gates")),
        "invariants": planner.invariant_list(state),
        "last_completed_task": planner.pointer_value(state, "last_completed_task"),
        "last_reviewed_task": planner.pointer_value(state, "last_reviewed_task"),
        "path": str(state_path),
        "phase": state.get("phase"),
        "queue_status": state.get("queue_status"),
        "queue_target_size": state.get("queue_target_size"),
        "readable": blob_sha is not None and bool(state),
        "source": STATE_SOURCE,
        "status": state.get("status"),
        "task_queue": declared,
        "task_queue_error": declared_error,
    }


def state_branch_drift_issues(
    state: dict[str, Any],
    remote_head: dict[str, Any],
) -> list[dict[str, str]]:
    """``PROJECT_STATE.branch`` 与 observed HEAD 分支漂移 ⇒ fail-closed（只报告，不改写）。"""

    state_branch = state.get("branch")

    head_branch = remote_head.get("branch")

    if (
        isinstance(state_branch, str)
        and state_branch.strip()
        and isinstance(head_branch, str)
        and head_branch.strip()
        and state_branch.strip() != head_branch.strip()
    ):
        return [
            own_issue(
                REASON_STATE_BRANCH_DRIFT,
                f"PROJECT_STATE.branch={state_branch.strip()} 与 observed HEAD "
                f"branch={head_branch.strip()} 漂移：state 不是基于当前 HEAD 线写成 ⇒ "
                "禁止 planner mutation，请基于最新 HEAD 重新读取事实",
            )
        ]

    return []


def refill_facts(
    *,
    snapshot: dict[str, Any],
    root: Path,
    state_path: Path,
    tasks_dir: Path,
    results_dir: Path,
    generated_at: str,
    refill_builder: RefillBuilder | None,
) -> dict[str, Any]:
    """队列补给事实（**复用** §2.10 refill 事实包，绝不复制队列资格算法）。"""

    payload = (
        refill_builder(snapshot)
        if refill_builder is not None
        else refill.build_planner_refill_request(
            root=root,
            state_path=state_path,
            tasks_dir=tasks_dir,
            results_dir=results_dir,
            generated_at=generated_at,
            snapshot=snapshot,
        )
    )

    return {
        "deficit": payload.get("deficit"),
        "facts_digest": payload.get("facts_digest"),
        "follow_on_count": payload.get("follow_on_count"),
        "lookahead_target": payload.get("lookahead_target"),
        "queue_head": payload.get("queue_head"),
        "reason_codes": sorted({str(code) for code in (payload.get("reason_codes") or [])}),
        "refill_required": payload.get("refill_required"),
        "schema": payload.get("schema"),
        "schema_version": payload.get("schema_version"),
        "snapshot_ref": payload.get("snapshot_ref"),
        "source": FACTS_SOURCES["refill"],
    }


def task_queue_section(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    refill_section: dict[str, Any],
    snapshot_codes: Sequence[str],
) -> dict[str, Any]:
    """``PROJECT_STATE.task_queue`` 声明 vs planner snapshot 的真实非终态队列（只报告）。"""

    declared, declared_error = planner.declared_queue(state)

    queue = refill.queue_facts(snapshot)

    pending = [str(task_id) for task_id in (queue.get("pending") or [])]

    return {
        "declared": declared,
        "declared_error": declared_error,
        "declaration_mismatch": planner.ISSUE_QUEUE_DECLARATION_MISMATCH in set(snapshot_codes),
        "head": refill_section.get("queue_head"),
        "pending": pending,
        "pending_count": len(pending),
        "queue_status": queue.get("queue_status"),
        "runnable_task": queue.get("runnable_task"),
        "source": (
            "PROJECT_STATE.task_queue (declaration) + "
            "orchestrator.planner_snapshot.queue (pending facts)"
        ),
        "target": queue.get("target"),
        "underfilled": bool(queue.get("underfilled")),
    }


def backlog_facts(
    *,
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
    state_path: Path,
    ledger_path: Path,
    generated_at: str,
    backlog_builder: BacklogBuilder | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """formal review backlog 指针（**复用** §2.11 manifest；绝不签发 review 结论）。

    返回 ``(section, issues)``。manifest 来源异常时 fail-closed（``available=false``），
    **绝不猜测 backlog 或 review 指针**。
    """

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
                generated_at=generated_at,
            )
        )

    except Exception as exc:  # noqa: BLE001 - 事实来源异常必须 fail-closed，绝不中断整轮
        return (
            {
                "available": False,
                "backlog_digest": None,
                "pointer": {},
                "reason_codes": [],
                "review_status_semantics": None,
                "schema": backlog_mod.REVIEW_BACKLOG_SCHEMA,
                "source": BACKLOG_SOURCE,
                "summary": {},
            },
            [
                own_issue(
                    REASON_REVIEW_BACKLOG_FACTS_UNAVAILABLE,
                    "review backlog manifest 不可用（fail-closed，绝不猜测 backlog / "
                    f"review 指针）：{type(exc).__name__}: {exc}",
                )
            ],
        )

    pointer = manifest.get("pointer") if isinstance(manifest.get("pointer"), dict) else {}

    coverage = manifest.get("coverage") if isinstance(manifest.get("coverage"), dict) else {}

    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}

    return (
        {
            "available": True,
            "backlog_digest": manifest.get("backlog_digest"),
            "pointer": {
                "backlog_count": summary.get("backlog_count"),
                "backlog_first": coverage.get("backlog_first"),
                "backlog_last": coverage.get("backlog_last"),
                "bound_count": summary.get("bound_count"),
                "invalid_count": summary.get("invalid_count"),
                "last_reviewed_task": pointer.get("last_reviewed_task"),
                "last_reviewed_task_has_valid_ledger_entry": pointer.get(
                    "last_reviewed_task_has_valid_ledger_entry"
                ),
                "last_reviewed_task_pointer_present": coverage.get(
                    "last_reviewed_task_pointer_present"
                ),
                "newest_completed_result": pointer.get("newest_completed_result"),
                "pending_count": summary.get("pending_count"),
            },
            "reason_codes": sorted({str(code) for code in (manifest.get("reason_codes") or [])}),
            "review_status_semantics": (
                "objective binding state only (pending / bound / invalid); never a review verdict"
            ),
            "schema": manifest.get("schema"),
            "source": BACKLOG_SOURCE,
            "summary": {
                "backlog_count": summary.get("backlog_count"),
                "exit_code": summary.get("exit_code"),
                "facts_incomplete_count": summary.get("facts_incomplete_count"),
                "issue_count": summary.get("issue_count"),
            },
            "tool_can_sign_review": False,
            "tool_can_write_review_ledger": False,
        },
        [],
    )


def drift_facts(
    *,
    snapshot_issues: Sequence[dict[str, Any]],
    pointer: dict[str, Any],
    remote_head: dict[str, Any],
    head_codes: Sequence[str],
    branch_codes: Sequence[str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """state 声称（``current_task`` / ``last_completed_task`` / ``branch``）vs results / HEAD。"""

    error_issues = [
        issue for issue in snapshot_issues if issue.get("severity") == planner.SEVERITY_ERROR
    ]

    gating_issues = [issue for issue in error_issues if not is_review_pointer_fact(issue)]

    codes = sorted({str(issue.get("code")) for issue in error_issues})

    state_fact_codes = sorted(
        {str(issue.get("code")) for issue in gating_issues} & set(STATE_FACT_REASON_CODES)
    )

    review_pointer_codes = sorted(
        {str(issue.get("code")) for issue in error_issues if is_review_pointer_fact(issue)}
    )

    return {
        "branch_fact_codes": sorted({str(code) for code in branch_codes}),
        "codes": codes,
        "current_task_pointer": pointer.get("current_task"),
        "details": [str(issue.get("detail")) for issue in error_issues],
        "detected": bool(gating_issues) or bool(head_codes) or bool(branch_codes),
        "expected_head_sha": remote_head.get("expected_head_sha"),
        "head_branch": remote_head.get("branch"),
        "head_fact_codes": sorted({str(code) for code in head_codes}),
        "last_completed_task_pointer": pointer.get("last_completed_task"),
        "last_reviewed_task_pointer": pointer.get("last_reviewed_task"),
        "latest_terminal_result": pointer.get("latest_terminal_result"),
        "latest_terminal_status": pointer.get("latest_terminal_status"),
        "observed_head_sha": remote_head.get("observed_head_sha"),
        "review_pointer_codes": review_pointer_codes,
        "source": "orchestrator.planner_snapshot (pointer / queue / results) + git facts",
        "stale_remote_head": bool(remote_head.get("stale")),
        "state_branch": state.get("branch"),
        "state_fact_codes": state_fact_codes,
    }


def blocking_reason_codes(
    *,
    head_issues: Sequence[dict[str, str]],
    snapshot_issues: Sequence[dict[str, str]],
    branch_issues: Sequence[dict[str, str]],
    backlog_issues: Sequence[dict[str, str]],
) -> list[str]:
    """决定「能否安全写队列」的稳定 code 集合（fail-closed；任一命中即禁止）。

    - HEAD 相关：``STALE_REMOTE_HEAD`` / ``EXPECTED_HEAD_SHA_INVALID`` /
      ``GIT_INFO_UNAVAILABLE``（``head_issues``）；
    - state 分支漂移：``STATE_BRANCH_DRIFT``（``branch_issues``）；
    - state 声称 vs results 事实漂移：复用 planner snapshot 的稳定 code，并叠加聚合标记
      ``STATE_RESULT_DRIFT``；``last_reviewed_task`` 指针类 issue 属于 review backlog 事实，
      只报告、不阻塞（见 ``REVIEW_POINTER_DETAIL_PREFIX``）；
    - 其它 planner snapshot error issue（task 文件损坏 / 依赖环 / Gate 不一致 / 安全不变量缺失
      等）同样说明事实不可信 ⇒ 一并禁止（**绝不**只挑一部分漂移来看）。
    """

    codes: set[str] = {issue["code"] for issue in head_issues}
    codes |= {issue["code"] for issue in branch_issues}
    codes |= {issue["code"] for issue in backlog_issues}

    gating_snapshot = [issue for issue in snapshot_issues if not is_review_pointer_fact(issue)]

    if {issue["code"] for issue in gating_snapshot} & set(STATE_FACT_REASON_CODES):
        codes.add(REASON_STATE_RESULT_DRIFT)

    codes |= {issue["code"] for issue in gating_snapshot}

    return sorted(codes)


# ============================================================
# 职责边界 / 确定性契约（只有规则，不含状态事实）
# ============================================================


def authority_section() -> dict[str, Any]:
    """本工具的**职责边界**（机器可读：只产事实，绝无规划 / 写状态 / 写台账能力）。"""

    contract = planner.role_contract()

    return {
        "schema": PRECONDITION_AUTHORITY_SCHEMA,
        "read_only": True,
        "facts_only": True,
        "planning_authority": "gpt_only",
        "planner_agents": list(contract["planner_agents"]),
        "executor_agents": list(contract["executor_agents"]),
        "planner_only_decisions": list(contract["planner_only_decisions"]),
        "executor_forbidden_capabilities": list(contract["executor_forbidden_capabilities"]),
        "blocking_human_gates": list(contract["blocking_human_gates"]),
        "executor_can_generate_follow_on_tasks": contract["executor_can_generate_follow_on_tasks"],
        "executor_can_refill_rolling_queue": contract["executor_can_refill_rolling_queue"],
        "executor_can_modify_project_state": contract["executor_can_modify_project_state"],
        "executor_can_decide_phase": contract["executor_can_decide_phase"],
        "executor_can_cross_human_gate": contract["executor_can_cross_human_gate"],
        "executor_can_review_task_result": contract["executor_can_review_task_result"],
        "executor_can_mutate": False,
        "executor_can_plan": False,
        "tool_can_mutate": False,
        "tool_can_generate_tasks": False,
        "tool_can_refill_queue": False,
        "tool_can_sign_review": False,
        "tool_can_advance_state": False,
        "tool_can_write_tasks": False,
        "tool_can_write_results": False,
        "tool_can_write_project_state": False,
        "tool_can_write_review_ledger": False,
        "tool_can_transition_phase": False,
        "tool_can_cross_human_gate": False,
        "tool_can_qualify_data": False,
        "writes_tasks": False,
        "writes_results": False,
        "writes_project_state": False,
        "writes_review_ledger": False,
        "auto_merge": False,
        "auto_rebase": False,
        "auto_fetch_or_pull": False,
        "force_push": False,
        "executor_execution_blocked": False,
        "gates_planner_writes_only": True,
        "mutation_gate_rule": (
            "planner_mutation.allowed == (planner_mutation.blocking_reason_codes == [])"
        ),
        "stale_recovery": (
            "re-read the latest remote HEAD and rebuild this fact package before any planner write"
        ),
        "facts_sources": dict(FACTS_SOURCES),
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "precondition_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "mtime_used_as_identity": False,
        "facts_source": "orchestrator.planner_snapshot + orchestrator.planner_refill_request + "
        "orchestrator.review_backlog",
        "head_facts_source": ".git HEAD/refs via orchestrator.planner_snapshot (read-only)",
        "second_identity_algorithm": False,
        "mutation_gate_rule": (
            "planner_mutation.allowed == (planner_mutation.blocking_reason_codes == [])"
        ),
        "blocking_reason_codes": list(OWN_BLOCKING_REASON_CODES) + list(STATE_FACT_REASON_CODES),
        "stale_head_policy": (
            "fail-closed: expected_head_sha != observed_head_sha => STALE_REMOTE_HEAD, "
            "planner mutation forbidden until facts are re-read"
        ),
        "ordering": dict(PRECONDITION_ORDERING),
    }


def mutation_section(
    *,
    remote_head: dict[str, Any],
    reason_codes: Sequence[str],
    blocking: Sequence[str],
) -> dict[str, Any]:
    """「能否安全写 planner-owned 状态」的显式判定（**只报告，绝不代为写入**）。"""

    allowed = not blocking

    return {
        "allowed": allowed,
        "authority": "gpt_only",
        "auto_merge": False,
        "auto_rebase": False,
        "blocking_reason_codes": list(blocking),
        "execution_blocked": False,
        "expected_head_sha": remote_head.get("expected_head_sha"),
        "forbidden": not allowed,
        "observed_head_sha": remote_head.get("observed_head_sha"),
        "policy": (
            "fail-closed：只要 observed HEAD 与调用方 expected HEAD 不一致、HEAD 不可解析、"
            "PROJECT_STATE.branch 与 observed HEAD 漂移，或 current_task / last_completed_task 与 "
            "results 事实漂移，就禁止任何 planner-owned 写入（task / state / queue），"
            "直到基于最新 HEAD 重新读取事实；绝不自动 merge / rebase / force push"
        ),
        "reason_codes": list(reason_codes),
        "requires_reread": not allowed,
        "scope": "planner-owned task / state / queue writes only (never blocks approved execution)",
        "stale_remote_head": bool(remote_head.get("stale")),
        "tool_can_mutate": False,
    }


# ============================================================
# 事实包组装（只读；绝不写任何 planner / state / ledger 路径）
# ============================================================


def build_planner_mutation_precondition(
    *,
    root: Path | None = None,
    state_path: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    ledger_path: Path | None = None,
    generated_at: str | None = None,
    expected_head_sha: object = None,
    snapshot: dict[str, Any] | None = None,
    head_provider: HeadProvider | None = None,
    refill_builder: RefillBuilder | None = None,
    backlog_builder: BacklogBuilder | None = None,
) -> dict[str, Any]:
    """构建只读 planner mutation precondition。**绝不修改任何项目状态、绝不写队列**。

    四个事实子包（planner snapshot / refill request / review backlog / HEAD）都允许注入已构建好
    的结果，便于测试与复用；默认逐项调用既有只读 builder，本模块自身的判定算法**为零**
    （只做稳定 code 归一化与比对）。
    """

    resolved_root = Path(root) if root is not None else ROOT

    resolved_state = (
        Path(state_path) if state_path is not None else resolved_root / ".ai" / "PROJECT_STATE.json"
    )

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else resolved_root / ".ai" / "tasks"

    resolved_results = (
        Path(results_dir) if results_dir is not None else resolved_root / ".ai" / "results"
    )

    resolved_ledger = (
        Path(ledger_path)
        if ledger_path is not None
        else resolved_root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
    )

    resolved_generated_at = generated_at if generated_at is not None else orch.now_iso()

    snapshot_payload = (
        snapshot
        if snapshot is not None
        else planner.build_planner_snapshot(
            root=resolved_root,
            state_path=resolved_state,
            tasks_dir=resolved_tasks,
            results_dir=resolved_results,
            generated_at=generated_at,
        )
    )

    state = snapshot_payload.get("project_state")

    if not isinstance(state, dict):
        state = {}

    remote_head, head_issues, head_codes = head_facts(
        root=resolved_root,
        snapshot=snapshot_payload,
        head_provider=head_provider,
        expected_head_sha=expected_head_sha,
    )

    refill_section = refill_facts(
        snapshot=snapshot_payload,
        root=resolved_root,
        state_path=resolved_state,
        tasks_dir=resolved_tasks,
        results_dir=resolved_results,
        generated_at=resolved_generated_at,
        refill_builder=refill_builder,
    )

    pointer = refill.pointer_facts(snapshot_payload)

    snapshot_issues = [
        inherited_issue(issue)
        for issue in (snapshot_payload.get("issues") or [])
        if isinstance(issue, dict)
        # 注入 head provider 时，HEAD 事实由 provider 提供：不再把 snapshot 自己的
        # GIT_INFO_UNAVAILABLE 重复计入（HEAD 事实来源只保留一个）。
        and not (
            head_provider is not None
            and str(issue.get("code")) == planner.ISSUE_GIT_INFO_UNAVAILABLE
        )
    ]

    snapshot_error_codes = sorted(
        {issue["code"] for issue in snapshot_issues if issue["severity"] == planner.SEVERITY_ERROR}
    )

    backlog_section, backlog_issues = backlog_facts(
        root=resolved_root,
        tasks_dir=resolved_tasks,
        results_dir=resolved_results,
        state_path=resolved_state,
        ledger_path=resolved_ledger,
        generated_at=resolved_generated_at,
        backlog_builder=backlog_builder,
    )

    branch_issues = state_branch_drift_issues(state, remote_head)

    blocking = blocking_reason_codes(
        head_issues=head_issues,
        snapshot_issues=snapshot_issues,
        branch_issues=branch_issues,
        backlog_issues=backlog_issues,
    )

    issues = sorted(
        {
            (issue["code"], issue["severity"], issue["detail"]): issue
            for issue in [*snapshot_issues, *head_issues, *backlog_issues, *branch_issues]
        }.values(),
        key=lambda issue: (issue["code"], issue["detail"]),
    )

    reason_codes = sorted({issue["code"] for issue in issues} | set(blocking))

    error_count = sum(1 for issue in issues if issue["severity"] == planner.SEVERITY_ERROR)

    payload: dict[str, Any] = {
        "authority": authority_section(),
        "drift": drift_facts(
            snapshot_issues=snapshot_issues,
            pointer=pointer,
            remote_head=remote_head,
            head_codes=head_codes,
            branch_codes=[issue["code"] for issue in branch_issues],
            state=state,
        ),
        "generated_at": resolved_generated_at,
        "issues": issues,
        "paths": {
            "project_state": str(resolved_state),
            "results_dir": str(resolved_results),
            "review_ledger": str(resolved_ledger),
            "root": str(resolved_root),
            "tasks_dir": str(resolved_tasks),
        },
        "planner_mutation": mutation_section(
            remote_head=remote_head,
            reason_codes=reason_codes,
            blocking=blocking,
        ),
        "planner_snapshot": {
            "error_count": sum(
                1 for issue in snapshot_issues if issue["severity"] == planner.SEVERITY_ERROR
            ),
            "facts_digest": snapshot_payload.get("facts_digest"),
            "head_provider_injected": head_provider is not None,
            "issue_codes": snapshot_error_codes,
            "schema": snapshot_payload.get("schema"),
            "schema_version": snapshot_payload.get("schema_version"),
            "source": FACTS_SOURCES["planner_snapshot"],
        },
        "precondition_kind": "planner_mutation_precondition",
        "queue_head": refill_section.get("queue_head"),
        "read_only": True,
        "reason_codes": reason_codes,
        "refill": refill_section,
        "remote_head": remote_head,
        "results_digest": directory_facts(resolved_results),
        "review_backlog": backlog_section,
        "schema": PRECONDITION_SCHEMA,
        "schema_version": PRECONDITION_SCHEMA_VERSION,
        "state": state_section(state, resolved_state),
        "task_queue": task_queue_section(
            state,
            snapshot_payload,
            refill_section,
            snapshot_error_codes,
        ),
        "tasks_digest": directory_facts(resolved_tasks),
    }

    payload["summary"] = {
        "blocking_reason_count": len(blocking),
        "branch": remote_head.get("branch"),
        "error_count": error_count,
        "exit_code": precondition_exit_code(reason_codes=reason_codes, blocking=blocking),
        "expected_head_sha": remote_head.get("expected_head_sha"),
        "head_matches_expected": remote_head.get("matches_expected"),
        "issue_count": len(issues),
        "observed_head_sha": remote_head.get("observed_head_sha"),
        "planner_mutation_allowed": bool(payload["planner_mutation"]["allowed"]),
        "queue_head": payload["queue_head"],
        "queue_pending_count": payload["task_queue"]["pending_count"],
        "refill_required": refill_section.get("refill_required"),
        "stale_remote_head": bool(remote_head.get("stale")),
        "state_result_drift": bool(payload["drift"]["state_fact_codes"]),
        "warning_count": len(issues) - error_count,
    }

    # digest / determinism 由 facts 派生，且自身被排除在 facts 之外（幂等、无自引用）。
    payload["precondition_digest"] = precondition_facts_digest(payload)

    payload["determinism"] = determinism_section()

    return payload


# ============================================================
# 确定性契约 / 退出码 / 渲染 / 只读 CLI
# ============================================================


def precondition_exit_code(*, reason_codes: Sequence[str], blocking: Sequence[str]) -> int:
    """退出码：``0`` 可安全写队列 / ``2`` fail-closed / ``3`` PROJECT_STATE 不可读。"""

    if planner.ISSUE_PROJECT_STATE_UNREADABLE in set(reason_codes):
        return EXIT_STATE_UNREADABLE

    if blocking:
        return EXIT_FAIL_CLOSED

    return EXIT_OK


def precondition_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in payload.items() if key not in FACTS_EXCLUDED_KEYS}


def precondition_facts_digest(payload: dict[str, Any]) -> str:
    """``precondition_digest``：相同仓库事实 ⇒ 相同 digest（幂等、无自引用）。"""

    return canonical_digest(precondition_facts(payload))


def resolve_output_target(root: Path, output: str | Path) -> tuple[Path | None, str | None]:
    """本模块的受控 ``--output`` 守卫（**比 §2.6 更严**）：``(target, reason)``。

    1. ``<root>/.ai/**`` 之内只允许 ``<root>/.ai/runtime/**``：任何 planner-owned 路径
       （``.ai/tasks`` / ``.ai/results`` / ``.ai/PROJECT_STATE.json`` /
       ``.ai/GPT_REVIEW_LEDGER.json`` 以及未来新增的 ``.ai/*``）一律 fail-closed 拒绝；
    2. 其余判定**复用** :func:`orchestrator.planner_snapshot_output.resolve_output_target`
       （只允许 ``<root>/.ai/runtime/**`` 或系统临时目录、父目录必须存在、绝不创建目录）。

    第 1 条不能只依赖共享守卫：系统临时目录本身也是允许根，若仓库恰好位于临时目录下，
    ``<root>/.ai/<任意文件>`` 会被共享守卫放行，因此这里先做更严的仓库内路径判定。
    """

    candidate = Path(output)

    if not candidate.is_absolute():
        candidate = Path(root) / candidate

    resolved = candidate.resolve()

    resolved_root = Path(root).resolve()

    ai_dir = resolved_root / ".ai"

    runtime_dir = resolved_root / snapshot_output.RUNTIME_SUBPATH

    if snapshot_output.is_within(resolved, ai_dir) and not snapshot_output.is_within(
        resolved, runtime_dir
    ):
        return None, (
            "只允许写入 <root>/.ai/runtime/**：planner / state / tasks / results / review ledger "
            f"一律拒绝: {resolved}"
        )

    return snapshot_output.resolve_output_target(root, output)


def render_planner_mutation_precondition(
    payload: dict[str, Any], *, ensure_ascii: bool = True
) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.planner_mutation_precondition",
        description=(
            "GPT Planner 写队列前的远端 HEAD 并发保护事实包（GOLD-038）：只读证明 observed HEAD "
            "与调用方 expected HEAD 一致、PROJECT_STATE 指针未与 results 事实漂移；"
            "绝不写 task / state / ledger，绝不 merge / rebase / force push。"
        ),
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
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
        "--generated-at",
        default=None,
        help="固定 generated_at（便于审计与字节级复现；默认取当前时间）",
    )

    parser.add_argument(
        "--expected-head-sha",
        "--expect-head",
        dest="expected_head_sha",
        default=None,
        help=(
            "调用方上次读取到的远端 HEAD（7~40 位十六进制）；与当前 observed HEAD 不一致 ⇒ "
            "fail-closed 输出 STALE_REMOTE_HEAD 并禁止 planner mutation"
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "可选：把事实包 JSON 写到用户显式指定的受控路径"
            "（只允许 <root>/.ai/runtime/** 或系统临时目录；写其它位置一律拒绝）"
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：默认写 stdout，``--output`` 时写受控文件；摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    payload = build_planner_mutation_precondition(
        root=root,
        state_path=Path(args.state) if args.state is not None else None,
        tasks_dir=Path(args.tasks_dir) if args.tasks_dir is not None else None,
        results_dir=Path(args.results_dir) if args.results_dir is not None else None,
        ledger_path=Path(args.ledger) if args.ledger is not None else None,
        generated_at=args.generated_at,
        expected_head_sha=args.expected_head_sha,
    )

    rendered = render_planner_mutation_precondition(payload)

    if args.output is None:
        sys.stdout.write(rendered)
        sys.stdout.flush()
    else:
        target, reason = resolve_output_target(root, args.output)

        if target is None:
            print(
                f"[error] {snapshot_output.ISSUE_OUTPUT_PATH_REJECTED}: {reason}", file=sys.stderr
            )

            return snapshot_output.EXIT_OUTPUT_REJECTED

        snapshot_output.write_snapshot_output(target, rendered)

        print(f"[info] precondition 已写入受控路径: {target}", file=sys.stderr)

    for issue in payload["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    mutation = payload["planner_mutation"]

    print(
        "[precondition] head={head} expected={expected} stale={stale} drift={drift} "
        "allowed={allowed} codes={codes}".format(
            head=payload["remote_head"]["observed_head_short"],
            expected=payload["remote_head"]["expected_head_short"],
            stale=mutation["stale_remote_head"],
            drift=payload["drift"]["detected"],
            allowed=mutation["allowed"],
            codes=",".join(mutation["blocking_reason_codes"]),
        ),
        file=sys.stderr,
    )

    return int(payload["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
