"""GPT Review Backlog 批量绑定事实清单（GOLD-037）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2 要求每个 ``COMPLETED`` 任务都由 **GPT** Review：
§2.7（GPT Review Ledger）把 review 变成机器可审计台账，§2.8（Review Binding Manifest，
``python -m orchestrator.review_binding --task <task_id>``）与 §2.9（
``python -m orchestrator.review_ledger_integrity``）提供 per-task 的客观内容身份与台账
完整性门禁。但当 backlog 里同时挂着**多个**已完成却尚未进入正式 Review Ledger 的任务时，
GPT 只能逐个手跑 §2.8，既容易漏项，也无法一次性看清「哪些还欠 review、每项的可绑定事实
是什么、有没有已经绑定或已经漂移的项」。

本模块就是那份**单一确定性 manifest**：把 ``PROJECT_STATE.last_reviewed_task`` 之后
**所有** ``completed`` result 汇总成一个只读 backlog，并**逐项复用**
:mod:`orchestrator.review_binding` 的客观 binding facts（result SHA-256 / 完成 commit
identity），因此**不存在第二套结果 / commit 身份算法**。

本模块提供
----------
1. :func:`build_review_backlog_manifest`：只读汇总 ``coverage`` / ``backlog[]`` /
   ``ledger`` / ``chain`` / ``pointer`` / ``issues`` 与稳定 ``backlog_digest``；
2. 逐项的 ``review_status`` 只有三个**客观**取值：``pending``（无 ledger 条目、facts 完整）
   / ``bound``（ledger 条目唯一且与 manifest 事实逐项一致）/ ``invalid``（facts 不齐、
   条目非法或 hash / commit 漂移）。**绝不产生** PASS / FAIL 之类 verdict；
   manifest 里也不存在 ``verdict`` / ``acceptance_summary`` / ``reviewed_at`` /
   ``reviewer`` 字段（见 ``FORBIDDEN_MANIFEST_KEYS`` 的否定声明）；
3. 复用既有只读门禁来检测「ledger 已绑定项」「completed-but-unreviewed」
   「result / commit 不可绑定」「顺序缺口」「重复项」：
   :func:`orchestrator.review_ledger.pointer_section`（指针 vs ledger vs results）、
   :func:`orchestrator.review_ledger_integrity.chain_section`（顺序回退 / 覆盖窗口缺项）、
   ``review_ledger.validate_review_ledger``（重复条目 / 非法条目）；
4. fail-closed：任何事实不完整（result / task 缺失、commit 找不到或有歧义、工作树漂移、
   条目重复、hash 或 commit 不一致）一律给出稳定 reason code 并标记该项 ``invalid``，
   **绝不猜测 commit / hash，绝不自动修复，绝不推进任何状态**；
5. 受控输出：``--output`` **复用** :mod:`orchestrator.planner_snapshot_output` 的
   fail-closed 路径守卫（只允许 ``<root>/.ai/runtime/**`` 或系统临时目录），
   该守卫已把 ``.ai/tasks`` / ``.ai/results`` / ``.ai/PROJECT_STATE.json`` /
   ``.ai/GPT_REVIEW_LEDGER.json`` 列为拒绝路径，因此本工具**不可能**写到 planner / state。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：绝不写 ``.ai/GPT_REVIEW_LEDGER.json`` / ``.ai/PROJECT_STATE.json`` /
  ``.ai/tasks/**`` / ``.ai/results/**``，绝不 commit / push / reset / checkout
  （由源码守卫测试锁定）；唯一写操作是显式 ``--output`` 到受控 runtime / 临时路径；
- **绝不签发 review**：``authority`` 段硬编码 ``review_authority=gpt_only`` /
  ``tool_can_sign_review=false`` / ``tool_can_write_review_ledger=false`` /
  ``tool_can_advance_review_pointer=false``；``review_status`` 只是客观绑定状态；
- 外部进程调用**只经由** :mod:`orchestrator.review_binding` 的只读 Git 白名单
  （``log`` / ``ls-tree`` / ``cat-file`` / ``hash-object``）；本模块自身不启动任何
  外部进程，零网络、零数据库、零业务证据、零模型调用；
- 不改变 ``PHASE3_3_DATA`` blocker、Phase 3.4 边界、L1~L4 档位、rolling queue、
  ``LIVE_TRADING=false`` 或 ``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``。

用法
----
.. code-block:: text

    python -m orchestrator.review_backlog                     # 只读 manifest 写 stdout
    python -m orchestrator.review_backlog --root .            # 指定仓库根
    python -m orchestrator.review_backlog --output <受控 runtime/临时路径>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放
人类可读的 issue 摘要（不参与机器解析）。

退出码：``0`` backlog 事实齐全且无漂移 / ``2`` fail-closed（facts 不齐、hash /
commit 漂移、顺序缺口、重复项、指针缺失）/ ``3`` ``PROJECT_STATE`` 或 ledger 不可用 /
``4`` ``--output`` 目标被 fail-closed 拒绝（此时绝不写文件）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
REVIEW_BACKLOG_SCHEMA = "gold-ai/review-backlog-manifest/v1"

REVIEW_BACKLOG_SCHEMA_VERSION = 1

REVIEW_BACKLOG_AUTHORITY_SCHEMA = "gold-ai/review-backlog-authority/v1"

# backlog 逐项的**客观** review 绑定状态（绝不是 verdict 词表）。
REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_BOUND = "bound"
REVIEW_STATUS_INVALID = "invalid"

REVIEW_STATUSES = (REVIEW_STATUS_PENDING, REVIEW_STATUS_BOUND, REVIEW_STATUS_INVALID)

EXIT_OK = 0

EXIT_FAIL_CLOSED = 2

EXIT_UNAVAILABLE = 3

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不作为内容身份；
# - ``backlog_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "backlog_digest", "determinism")

# 输出里**绝不允许**出现的 Review 结论字段（GPT-only Review 的机器可测边界）。
FORBIDDEN_MANIFEST_KEYS = (
    "verdict",
    "acceptance_summary",
    "reviewed_at",
    "reviewer",
    "reviewer_role",
)

# ---------- 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言） ----------

ISSUE_PROJECT_STATE_UNREADABLE = "PROJECT_STATE_UNREADABLE"
ISSUE_LAST_REVIEWED_POINTER_MISSING = "LAST_REVIEWED_TASK_POINTER_MISSING"
ISSUE_BACKLOG_ITEM_MANIFEST_UNUSABLE = "BACKLOG_ITEM_MANIFEST_UNUSABLE"
ISSUE_BACKLOG_ITEM_FACTS_INCOMPLETE = "BACKLOG_ITEM_FACTS_INCOMPLETE"
ISSUE_BACKLOG_ITEM_LEDGER_INVALID = "BACKLOG_ITEM_LEDGER_INVALID"
ISSUE_BACKLOG_ITEM_RESULT_HASH_DRIFT = "BACKLOG_ITEM_RESULT_HASH_DRIFT"
ISSUE_BACKLOG_ITEM_RESULT_STATUS_DRIFT = "BACKLOG_ITEM_RESULT_STATUS_DRIFT"
ISSUE_BACKLOG_ITEM_RESULT_FINISHED_AT_DRIFT = "BACKLOG_ITEM_RESULT_FINISHED_AT_DRIFT"
ISSUE_BACKLOG_ITEM_COMMIT_SHA_DRIFT = "BACKLOG_ITEM_COMMIT_SHA_DRIFT"
ISSUE_BACKLOG_ITEM_COMMIT_BRANCH_DRIFT = "BACKLOG_ITEM_COMMIT_BRANCH_DRIFT"

# ``PROJECT_STATE`` 或 ledger 不可用 ⇒ 退出码 3（fail-closed，绝不把「读不到」当「没问题」）。
UNAVAILABLE_CODES = (ISSUE_PROJECT_STATE_UNREADABLE, *integrity.LEDGER_UNAVAILABLE_CODES)

# 确定性排序契约（机器可读，供 GPT 与测试断言；只描述规则，不含事实）。
BACKLOG_ORDERING: dict[str, str] = {
    "backlog": "planner.task_rank（项目前缀最新排最后，再按 task_id_sort_key）",
    "backlog[].missing_reason_codes": "sorted unique（review_binding reason codes）",
    "backlog[].reason_codes": "sorted unique",
    "ledger.entry_task_ids": "ledger 文件原始顺序",
    "ledger.duplicate_tasks": "planner.task_id_sort_key",
    "chain": "review_ledger_integrity.chain_section（本模块不重排台账）",
    "issues": "sorted by (code, detail)",
    "missing_reason_codes": "sorted unique（backlog 逐项 missing 汇总）",
    "reason_codes": "sorted unique issue codes",
}

# Manifest 注入点：测试可注入纯确定性 fake，完全零子进程 / 零 Git。
ManifestBuilder = Callable[[str], dict[str, Any]]


# ============================================================
# 通用只读工具（确定性）
# ============================================================


def make_issue(code: str, detail: str) -> dict[str, str]:
    """构造一条 fail-closed 诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": planner.SEVERITY_ERROR, "detail": detail}


def canonical_digest(payload: object) -> str:
    """确定性 JSON 摘要（固定 sort_keys + 紧凑分隔符 ⇒ 相同事实必然相同 digest）。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def default_manifest_builder(
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
) -> ManifestBuilder:
    """默认 manifest 来源：``orchestrator.review_binding``（只读 Git 白名单）。"""

    def build(task_id: str) -> dict[str, Any]:
        return binding.build_review_binding_manifest(
            task_id,
            root=root,
            tasks_dir=tasks_dir,
            results_dir=results_dir,
        )

    return build


# ============================================================
# 逐项事实：ledger 条目（只读） + GOLD-031 manifest 客观绑定
# ============================================================


def ledger_entries_for(
    entry_views: Sequence[dict[str, Any]],
    task_id: str,
) -> list[dict[str, Any]]:
    """该 task 在 ledger 里的**全部**条目视图（含非法 / 冲突条目，绝不挑选）。"""

    return [entry for entry in entry_views if str(entry.get("task_id")) == task_id]


def entry_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """一条 ledger 条目声明的客观身份（**不含** verdict / reviewed_at 等结论字段）。"""

    reviewed_result = entry.get("reviewed_result")
    reviewed_commit = entry.get("reviewed_commit")

    result_section = reviewed_result if isinstance(reviewed_result, dict) else {}
    commit_section = reviewed_commit if isinstance(reviewed_commit, dict) else {}

    return {
        "result_sha256": result_section.get("result_sha256"),
        "result_status": result_section.get("status"),
        "result_finished_at": result_section.get("finished_at"),
        "commit_sha": commit_section.get("sha"),
        "commit_branch": commit_section.get("branch"),
    }


def manifest_identity(result: dict[str, Any], commit: dict[str, Any]) -> dict[str, Any]:
    """GOLD-031 manifest 事实 → 与 :func:`entry_identity` **同口径**的客观身份。"""

    return {
        "result_sha256": result.get("sha256"),
        "result_status": result.get("status"),
        "result_finished_at": result.get("finished_at"),
        "commit_sha": commit.get("sha"),
        "commit_branch": commit.get("branch"),
    }


def manifest_item_facts(manifest: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    """从 GOLD-031 manifest 里**只取事实子集**（丢弃 wall-clock / 结论字段）。"""

    payload = manifest if isinstance(manifest, dict) else {}

    result = payload.get("result")
    commit = payload.get("commit")
    binding_section = payload.get("binding")

    result_section = result if isinstance(result, dict) else {}
    commit_section = commit if isinstance(commit, dict) else {}
    binding_facts = binding_section if isinstance(binding_section, dict) else {}

    return {
        "task_id": task_id,
        "facts_complete": bool(binding_facts.get("facts_complete")),
        "missing_reason_codes": sorted(
            {str(code) for code in binding_facts.get("reason_codes") or []}
        ),
        "result": {
            "path": result_section.get("path"),
            "sha256": result_section.get("sha256"),
            "bytes": result_section.get("bytes"),
            "worktree_sha256": result_section.get("worktree_sha256"),
            "worktree_matches_commit": result_section.get("worktree_matches_commit"),
            "status": result_section.get("status"),
            "known_status": result_section.get("known_status"),
            "terminal": result_section.get("terminal"),
            "finished_at": result_section.get("finished_at"),
            "attempt_count": result_section.get("attempt_count"),
        },
        "commit": {
            "resolved": commit_section.get("resolved"),
            "reason_code": commit_section.get("reason_code"),
            "sha": commit_section.get("sha"),
            "branch": commit_section.get("branch"),
            "head": commit_section.get("head"),
            "subject": commit_section.get("subject"),
            "committed_at": commit_section.get("committed_at"),
        },
    }


# ledger 身份字段 → 漂移诊断 code（字段名与 ``entry_identity`` 一致）。
IDENTITY_DRIFT_CHECKS: tuple[tuple[str, str], ...] = (
    ("result_sha256", ISSUE_BACKLOG_ITEM_RESULT_HASH_DRIFT),
    ("result_status", ISSUE_BACKLOG_ITEM_RESULT_STATUS_DRIFT),
    ("result_finished_at", ISSUE_BACKLOG_ITEM_RESULT_FINISHED_AT_DRIFT),
    ("commit_sha", ISSUE_BACKLOG_ITEM_COMMIT_SHA_DRIFT),
    ("commit_branch", ISSUE_BACKLOG_ITEM_COMMIT_BRANCH_DRIFT),
)


def identity_drift_issues(
    task_id: str,
    declared: dict[str, Any],
    actual: dict[str, Any],
) -> list[dict[str, str]]:
    """ledger 声明的身份 vs manifest 事实逐项比对（缺失 / 不一致一律 fail-closed）。"""

    issues: list[dict[str, str]] = []

    for field, code in IDENTITY_DRIFT_CHECKS:
        declared_value = declared.get(field)
        actual_value = actual.get(field)

        if declared_value is None or actual_value is None:
            issues.append(
                make_issue(
                    code,
                    f"{task_id}: ledger / manifest 的 {field} 任一侧缺失"
                    f"（ledger={declared_value!r} manifest={actual_value!r}）："
                    "无法核对客观身份，fail-closed",
                )
            )

            continue

        if declared_value != actual_value:
            issues.append(
                make_issue(
                    code,
                    f"{task_id}: ledger {field}={declared_value!r} 与 manifest "
                    f"{actual_value!r} 不一致（身份漂移，fail-closed，绝不自动修复）",
                )
            )

    return issues


def backlog_item(
    task_id: str,
    manifest: dict[str, Any] | None,
    entries: Sequence[dict[str, Any]],
    *,
    manifest_error: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """构建 backlog 单项事实 + 稳定 issue（``review_status`` 只表达客观绑定状态）。

    - ``pending``：无 ledger 条目且 ``facts_complete=true``（正常待 GPT review 的项）；
    - ``bound``：ledger 条目唯一、合法，且其客观身份与 manifest 逐项一致；
    - ``invalid``：facts 不齐 / 条目非法或重复 / hash 或 commit 漂移（一律 fail-closed）。

    出现 ``bound`` 意味着 ``PROJECT_STATE.last_reviewed_task`` 落后于 ledger（GPT 应先修
    指针而不是重复 review）；本工具只报告，绝不代改任何指针。
    """

    issues: list[dict[str, str]] = []

    if manifest_error is not None:
        issues.append(
            make_issue(
                ISSUE_BACKLOG_ITEM_MANIFEST_UNUSABLE,
                f"{task_id}: 无法取得客观 binding manifest（{manifest_error}），fail-closed",
            )
        )

    facts = manifest_item_facts(manifest, task_id)

    facts_complete = bool(facts["facts_complete"]) and manifest_error is None

    if manifest_error is None and not facts_complete:
        issues.append(
            make_issue(
                ISSUE_BACKLOG_ITEM_FACTS_INCOMPLETE,
                f"{task_id}: review_binding facts_complete=false"
                f"（{', '.join(facts['missing_reason_codes']) or 'unknown'}）："
                "result / commit 身份不齐，禁止猜测 commit 或 hash",
            )
        )

    valid_entries = [entry for entry in entries if entry.get("valid") is True]

    entry_present = bool(entries)

    entry_valid = entry_present and len(entries) == 1 and len(valid_entries) == 1

    if entry_present and not entry_valid:
        issues.append(
            make_issue(
                ISSUE_BACKLOG_ITEM_LEDGER_INVALID,
                f"{task_id}: ledger 存在 {len(entries)} 条记录（合法 {len(valid_entries)} 条），"
                "重复 / 非法条目整体作废，fail-closed（绝不挑一个更宽松的结论）",
            )
        )

    declared: dict[str, Any] | None = None

    if entry_valid and facts_complete:
        declared = entry_identity(valid_entries[0])

        issues.extend(
            identity_drift_issues(
                task_id,
                declared,
                manifest_identity(facts["result"], facts["commit"]),
            )
        )

    if not facts_complete or (entry_present and not entry_valid) or issues:
        review_status = REVIEW_STATUS_INVALID
    elif entry_valid:
        review_status = REVIEW_STATUS_BOUND
    else:
        review_status = REVIEW_STATUS_PENDING

    item: dict[str, Any] = {
        "task_id": task_id,
        "review_status": review_status,
        "facts_complete": facts_complete,
        "missing_reason_codes": (
            facts["missing_reason_codes"] if manifest_error is None else []
        ),
        "reason_codes": sorted({issue["code"] for issue in issues}),
        "result": facts["result"],
        "commit": facts["commit"],
        "ledger": {
            "entry_present": entry_present,
            "entry_count": len(entries),
            "entry_valid": entry_valid,
            "result_sha256_matches": (
                None
                if declared is None
                else declared.get("result_sha256") == facts["result"].get("sha256")
            ),
            "commit_sha_matches": (
                None
                if declared is None
                else declared.get("commit_sha") == facts["commit"].get("sha")
            ),
        },
        "binding_source": binding.REVIEW_BINDING_SCHEMA,
    }

    return item, issues


def build_backlog_item(
    task_id: str,
    builder: ManifestBuilder,
    entries: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """调用（可注入的）manifest builder 并构建单项事实；builder 异常一律 fail-closed。"""

    manifest: dict[str, Any] | None = None

    manifest_error: str | None = None

    try:
        manifest = builder(task_id)

    except Exception as exc:  # noqa: BLE001 - builder 异常必须 fail-closed，绝不中断整轮
        manifest_error = f"{type(exc).__name__}: {exc}"

    return backlog_item(task_id, manifest, entries, manifest_error=manifest_error)


# ============================================================
# 职责边界 / 确定性契约（只有规则，不含状态事实）
# ============================================================


def authority_section() -> dict[str, Any]:
    """本工具的**职责边界**（机器可读：只产客观事实，绝不签发 / 写任何结论）。"""

    contract = planner.role_contract()

    return {
        "schema": REVIEW_BACKLOG_AUTHORITY_SCHEMA,
        "read_only": True,
        "emits_review_outcome": False,
        "emits_review_verdict_values": False,
        "creates_verdict": False,
        "tool_can_sign_review": False,
        "tool_can_write_review_ledger": False,
        "tool_can_advance_review_pointer": False,
        "tool_can_advance_state": False,
        "tool_can_generate_tasks": False,
        "tool_can_refill_queue": False,
        "tool_can_qualify_data": False,
        "tool_can_cross_human_gate": False,
        "writes_review_ledger": False,
        "writes_project_state": False,
        "writes_tasks": False,
        "writes_results": False,
        "review_authority": "gpt_only",
        "planner_agents": list(contract["planner_agents"]),
        "executor_agents": list(contract["executor_agents"]),
        "executor_can_review_task_result": contract["executor_can_review_task_result"],
        "review_status_semantics": (
            "objective binding state only (pending/bound/invalid); never a review outcome"
        ),
        "review_status_values": list(REVIEW_STATUSES),
        "backlog_scope_rule": (
            "completed results strictly newer than PROJECT_STATE.last_reviewed_task"
        ),
        "binding_source": binding.REVIEW_BINDING_SCHEMA,
        "ledger_source": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "ledger_integrity_source": integrity.LEDGER_INTEGRITY_SCHEMA,
        "blocking_human_gates": list(contract["blocking_human_gates"]),
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "backlog_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "mtime_used_as_identity": False,
        "model_output_used_as_identity": False,
        "identity_source": (
            "orchestrator.review_binding canonical git blob bytes + HEAD-reachable "
            "terminal commit"
        ),
        "ordering": dict(BACKLOG_ORDERING),
    }


def backlog_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in payload.items() if key not in FACTS_EXCLUDED_KEYS}


def backlog_facts_digest(payload: dict[str, Any]) -> str:
    """``backlog_digest``：相同仓库事实 ⇒ 相同 digest（幂等、无自引用）。"""

    return canonical_digest(backlog_facts(payload))


def backlog_exit_code(issues: Sequence[dict[str, str]]) -> int:
    """退出码：``0`` 无漂移 / ``2`` fail-closed 漂移 / ``3`` state 或 ledger 不可用。"""

    codes = {str(issue.get("code")) for issue in issues}

    if any(code in codes for code in UNAVAILABLE_CODES):
        return EXIT_UNAVAILABLE

    if issues:
        return EXIT_FAIL_CLOSED

    return EXIT_OK


def render_review_backlog_manifest(payload: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


# ============================================================
# backlog manifest 组装（只读事实；绝不签发 Review 结论）
# ============================================================


def build_review_backlog_manifest(
    *,
    root: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    state_path: Path | None = None,
    ledger_path: Path | None = None,
    generated_at: str | None = None,
    manifest_builder: ManifestBuilder | None = None,
) -> dict[str, Any]:
    """构建只读 GPT Review Backlog manifest（**只产事实，绝不签发 Review 结论**）。

    ``manifest_builder`` 允许注入已经构建好的 GOLD-031 manifest 来源（便于测试与复用）；
    默认逐项调用 :func:`orchestrator.review_binding.build_review_binding_manifest`。
    """

    resolved_root = Path(root) if root is not None else ROOT

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else Path(orch.TASK_DIR)

    resolved_results = Path(results_dir) if results_dir is not None else Path(orch.RESULT_DIR)

    resolved_state = Path(state_path) if state_path is not None else planner.PROJECT_STATE_PATH

    resolved_ledger = (
        Path(ledger_path)
        if ledger_path is not None
        else resolved_root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
    )

    builder = (
        manifest_builder
        if manifest_builder is not None
        else default_manifest_builder(resolved_root, resolved_tasks, resolved_results)
    )

    issues: list[dict[str, str]] = []

    state, state_error = planner.load_project_state(resolved_state)

    if state is None:
        issues.append(
            make_issue(
                ISSUE_PROJECT_STATE_UNREADABLE,
                f"PROJECT_STATE 不可读（fail-closed）: {state_error}",
            )
        )

        state = {}

    statuses = planner.result_statuses(resolved_results)

    ledger_payload, ledger_error = ledger_mod.load_review_ledger(resolved_ledger)

    ledger_view, ledger_issues = ledger_mod.validate_review_ledger(ledger_payload, ledger_error)

    for ledger_issue in ledger_issues:
        issues.append(
            make_issue(
                integrity.translate_ledger_issue(str(ledger_issue["code"])),
                f"{ledger_issue['code']}: {ledger_issue['detail']}",
            )
        )

    raw_entries = integrity.raw_entry_facts(ledger_payload)

    entry_views = [entry for entry in ledger_view["entries"] if isinstance(entry, dict)]

    valid_entry_ids = sorted(
        {
            str(entry["task_id"])
            for entry in entry_views
            if entry.get("valid") is True and entry.get("task_id") is not None
        },
        key=planner.task_id_sort_key,
    )

    last_reviewed = planner.pointer_value(state, "last_reviewed_task")

    if last_reviewed is None:
        issues.append(
            make_issue(
                ISSUE_LAST_REVIEWED_POINTER_MISSING,
                "PROJECT_STATE.last_reviewed_task 缺失：无法确定 formal review backlog 边界，"
                "fail-closed（绝不猜测从哪个任务开始 review）",
            )
        )

    project_prefix = planner.project_task_prefix(state) if state else None

    completed_ids = ledger_mod.completed_result_ids(statuses, project_prefix)

    backlog_ids = (
        []
        if last_reviewed is None
        else [task_id for task_id in completed_ids if planner.is_newer(task_id, last_reviewed)]
    )

    chain, chain_issues = integrity.chain_section(
        raw_entries,
        valid_entry_ids,
        ledger_view,
        resolved_results,
    )

    issues.extend(chain_issues)

    pointer_section, pointer_issues = ledger_mod.pointer_section(
        state,
        ledger_view,
        statuses,
        resolved_tasks,
    )

    issues.extend(pointer_issues)

    backlog: list[dict[str, Any]] = []

    for task_id in backlog_ids:
        item, item_issues = build_backlog_item(
            task_id,
            builder,
            ledger_entries_for(entry_views, task_id),
        )

        backlog.append(item)

        issues.extend(item_issues)

    ordered_issues = sorted(
        {(issue["code"], issue["detail"]): issue for issue in issues}.values(),
        key=lambda issue: (str(issue["code"]), str(issue["detail"])),
    )

    status_counts = {
        status: sum(1 for item in backlog if item["review_status"] == status)
        for status in REVIEW_STATUSES
    }

    missing_reason_codes = sorted(
        {code for item in backlog for code in item["missing_reason_codes"]}
    )

    payload: dict[str, Any] = {
        "schema": REVIEW_BACKLOG_SCHEMA,
        "schema_version": REVIEW_BACKLOG_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "paths": {
            "root": str(resolved_root),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
            "project_state": str(resolved_state),
            "review_ledger": str(resolved_ledger),
        },
        "coverage": {
            "last_reviewed_task_pointer": last_reviewed,
            "last_reviewed_task_pointer_present": last_reviewed is not None,
            "last_reviewed_task_has_valid_ledger_entry": (
                last_reviewed is not None and last_reviewed in valid_entry_ids
            ),
            "project_prefix": project_prefix,
            "current_task_pointer": planner.pointer_value(state, "current_task"),
            "last_completed_task_pointer": planner.pointer_value(state, "last_completed_task"),
            "completed_result_count": len(completed_ids),
            "newest_completed_result": completed_ids[-1] if completed_ids else None,
            "backlog_count": len(backlog_ids),
            "backlog_first": backlog_ids[0] if backlog_ids else None,
            "backlog_last": backlog_ids[-1] if backlog_ids else None,
        },
        "backlog": backlog,
        "ledger": {
            "available": ledger_view["available"],
            "schema": ledger_view["schema"],
            "schema_version": ledger_view["schema_version"],
            "entry_count": len(raw_entries),
            "valid_entry_count": len(valid_entry_ids),
            "entry_task_ids": [fact["task_id"] for fact in raw_entries],
            "valid_entry_task_ids": valid_entry_ids,
            "duplicate_tasks": list(ledger_view["duplicate_tasks"]),
            "coverage_floor": ledger_view["coverage_floor"],
            "coverage_floor_source": ledger_view["coverage_floor_source"],
        },
        "chain": chain,
        "pointer": {
            "last_reviewed_task": pointer_section.get("last_reviewed_task"),
            "newest_completed_result": pointer_section.get("newest_completed_result"),
            "last_reviewed_task_has_valid_ledger_entry": (
                pointer_section.get("entry_present") is True
            ),
        },
        "authority": authority_section(),
        "determinism": determinism_section(),
        "issues": ordered_issues,
        "missing_reason_codes": missing_reason_codes,
        "reason_codes": sorted({str(issue["code"]) for issue in ordered_issues}),
        "summary": {
            "backlog_count": len(backlog),
            "bound_count": status_counts[REVIEW_STATUS_BOUND],
            "pending_count": status_counts[REVIEW_STATUS_PENDING],
            "invalid_count": status_counts[REVIEW_STATUS_INVALID],
            "status_counts": status_counts,
            "facts_incomplete_count": sum(1 for item in backlog if not item["facts_complete"]),
            "duplicate_task_count": len(ledger_view["duplicate_tasks"]),
            "chain_gap_count": len(chain.get("missing_in_window") or []),
            "issue_count": len(ordered_issues),
            "error_count": len(ordered_issues),
            "warning_count": 0,
            "exit_code": backlog_exit_code(ordered_issues),
        },
    }

    payload["backlog_digest"] = backlog_facts_digest(payload)

    return payload


# ============================================================
# 只读 CLI（python -m orchestrator.review_backlog）
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_backlog",
        description=(
            "GPT Review Backlog 批量绑定事实清单（GOLD-037）：只读汇总 "
            "PROJECT_STATE.last_reviewed_task 之后所有 completed result，逐项复用 "
            "orchestrator.review_binding 的客观 result SHA-256 / 完成 commit identity；"
            "绝不签发 Review 结论、绝不写 ledger / PROJECT_STATE / tasks / results。"
        ),
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
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
        "--state",
        type=Path,
        default=None,
        help="PROJECT_STATE 路径（默认 <root>/.ai/PROJECT_STATE.json）",
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
        "--output",
        type=Path,
        default=None,
        help=(
            "可选：把 manifest JSON 写到用户显式指定的受控路径"
            "（只允许 <root>/.ai/runtime/** 或系统临时目录；写其它位置一律拒绝）"
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：默认写 stdout，``--output`` 时写受控文件；摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    state_path = (
        Path(args.state) if args.state is not None else root / ".ai" / "PROJECT_STATE.json"
    )

    ledger_path = (
        Path(args.ledger)
        if args.ledger is not None
        else root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
    )

    payload = build_review_backlog_manifest(
        root=root,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        state_path=state_path,
        ledger_path=ledger_path,
        generated_at=args.generated_at,
    )

    rendered = render_review_backlog_manifest(payload)

    if args.output is None:
        sys.stdout.write(rendered)
        sys.stdout.flush()
    else:
        target, reason = snapshot_output.resolve_output_target(root, args.output)

        if target is None:
            message = f"[error] {snapshot_output.ISSUE_OUTPUT_PATH_REJECTED}: {reason}"

            print(message, file=sys.stderr)

            return snapshot_output.EXIT_OUTPUT_REJECTED

        snapshot_output.write_snapshot_output(target, rendered)

        print(f"[info] review backlog 已写入受控路径: {target}", file=sys.stderr)

    for issue in payload["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    print(
        "[backlog] last_reviewed={last_reviewed} count={count} bound={bound} "
        "pending={pending} invalid={invalid} missing={missing} codes={codes}".format(
            last_reviewed=payload["coverage"]["last_reviewed_task_pointer"],
            count=payload["summary"]["backlog_count"],
            bound=payload["summary"]["bound_count"],
            pending=payload["summary"]["pending_count"],
            invalid=payload["summary"]["invalid_count"],
            missing=",".join(payload["missing_reason_codes"]),
            codes=",".join(payload["reason_codes"]),
        ),
        file=sys.stderr,
    )

    return int(payload["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
