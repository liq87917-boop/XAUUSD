"""历史矛盾任务的确定性裁决证据包（GOLD-043）。

为什么需要它
------------
GOLD-039（§2.13）把历史矛盾**显式暴露**、GOLD-042（§2.15）提供了 GPT 专属裁决
**契约**，但 GPT 仍要逐项手跑 ``review_binding`` / ``legacy_result_adjudication`` /
``review_backlog`` 才能看清「``last_reviewed_task`` 之后的完整 backlog 里，每一项到底是
可直接实质 review、还是必须先裁决、还是事实不齐」。本模块就是那份**单一、只读、
确定性**的证据 manifest：把 backlog 逐项用**同一套既有事实算法**汇总成一份可被 GPT
独立复算的视图。

本模块提供
----------
1. :func:`build_review_evidence_manifest`：只读汇总 ``coverage`` / ``items[]`` / ``ledger`` /
   ``adjudication`` / ``issues`` 与稳定 ``facts_digest``；
2. **唯一**事实口径复用（绝不另造 result / commit 身份算法）：
   - 内容身份 / 终态 commit / validation **全部**来自 :mod:`orchestrator.review_binding`
     （§2.8 只读 Git 白名单：``log`` / ``ls-tree`` / ``cat-file`` / ``hash-object``）；
   - 终态矛盾判定**全部**来自 :mod:`orchestrator.result_terminal_consistency`（§2.13 唯一规则源）；
   - 裁决状态**全部**来自 :mod:`orchestrator.legacy_result_adjudication`（§2.15 唯一契约）；
   - ledger / 连续性事实**全部**来自 :mod:`orchestrator.review_ledger` 与
     :mod:`orchestrator.review_ledger_integrity`；
3. 逐项 ``classification`` 明确区分四种**客观**状态（绝不是 verdict 词表）：

   - ``facts-ready``：事实齐全、无未解阻塞（无 legacy 矛盾，或矛盾已有有效 GPT 裁决，
     或已被正式 ledger 合法绑定）⇒ GPT 可据此实质 review / 收口；
   - ``needs-gpt-adjudication``：事实齐全，但存在**未裁决的 legacy 终态矛盾** ⇒ 必须先由
     GPT 裁决（原始 contradiction reason code 依旧显式可见）；
   - ``pending-substantive-review``：事实齐全、无矛盾、尚未绑定 ⇒ 等待 GPT 实质 review；
   - ``invalid``：缺 commit / hash 漂移 / 事实不齐 / ledger 非法 / 裁决冲突等 ⇒ fail-closed。

   **测试通过、``exit_code=0`` 或任何 validation ``returncode=0`` 都不等于 GPT PASS**：
   manifest 里不存在 ``verdict`` / ``acceptance_summary`` / ``reviewed_at`` 等结论字段，
   ``exit_code=0`` 只表示「客观事实可复算、无 fail-closed 项」，绝不代表 review 已完成；
4. fail-closed：任一缺 commit / hash 漂移 / 矛盾裁决 / 不完整事实都给出稳定 reason code，
   **绝不猜测 commit / hash、绝不自动修复、绝不推进任何指针**；
5. 受控输出：``--output`` **复用** :mod:`orchestrator.planner_snapshot_output` 的 fail-closed
   路径守卫（只允许 ``<root>/.ai/runtime/**`` 或系统临时目录）；该守卫已把
   ``.ai/tasks`` / ``.ai/results`` / ``.ai/PROJECT_STATE.json`` / ``.ai/GPT_REVIEW_LEDGER.json``
   列为拒绝路径，因此本工具**不可能**写到 tasks / results / state / ledger / adjudication。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：默认 stdout；唯一显式写操作是 ``--output`` 到受控 runtime / 临时路径
  （由源码守卫测试锁定）；绝不写 ``.ai/tasks/**``、``.ai/results/**``、
  ``.ai/PROJECT_STATE.json``、``.ai/GPT_REVIEW_LEDGER.json``、``.ai/adjudications/**``；
- **绝不签发 review / adjudication**：``authority`` 段硬编码 ``review_authority=gpt_only`` /
  ``tool_can_sign_review=false`` / ``tool_can_sign_adjudication=false`` /
  ``tool_can_advance_review_pointer=false``；
- 外部进程调用**只经由** :mod:`orchestrator.review_binding` 的只读 Git 白名单；本模块自身
  不启动任何外部进程，零网络、零数据库、零模型调用、零 wall-clock 参与内容身份；
- 不改变 ``PHASE3_3_DATA`` blocker、Phase 3.4 边界、L1~L4 档位、rolling queue、
  ``LIVE_TRADING=false`` 或 ``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``。

用法
----
.. code-block:: text

    python -m orchestrator.review_evidence_manifest                 # 只读 manifest 写 stdout
    python -m orchestrator.review_evidence_manifest --output <受控 runtime/临时路径>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放人类
可读的 issue 摘要（不参与机器解析）。

退出码：``0`` 客观事实可复算且无 fail-closed 项（**绝不是** GPT PASS）/ ``2`` fail-closed
（缺 commit、hash 漂移、事实不齐、裁决冲突，或存在待裁决矛盾）/ ``3`` ``PROJECT_STATE`` 或
ledger 不可用 / ``4`` ``--output`` 目标被 fail-closed 拒绝（此时绝不写文件）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import legacy_result_adjudication as adjudication
from orchestrator import planner_snapshot as planner
from orchestrator import planner_snapshot_output as snapshot_output
from orchestrator import result_terminal_consistency as terminal_consistency
from orchestrator import review_backlog as backlog
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod
from orchestrator import review_ledger_integrity as integrity

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
SCHEMA = "gold-ai/review-evidence-manifest/v1"

SCHEMA_VERSION = 1

AUTHORITY_SCHEMA = "gold-ai/review-evidence-manifest-authority/v1"

CONTRACT_SCHEMA = "gold-ai/review-evidence-manifest-contract/v1"

# 逐项**客观**分类（绝不是 verdict 词表）。
CLASSIFICATION_FACTS_READY = "facts-ready"

CLASSIFICATION_NEEDS_GPT_ADJUDICATION = "needs-gpt-adjudication"

CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW = "pending-substantive-review"

CLASSIFICATION_INVALID = "invalid"

CLASSIFICATIONS = (
    CLASSIFICATION_FACTS_READY,
    CLASSIFICATION_NEEDS_GPT_ADJUDICATION,
    CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW,
    CLASSIFICATION_INVALID,
)

# ledger 绑定状态（沿用 §2.11 的客观语义，绝不是结论）。
LEDGER_STATUS_ABSENT = "absent"

LEDGER_STATUS_BOUND = "bound"

LEDGER_STATUS_INVALID = "invalid"

EXIT_OK = 0

EXIT_FAIL_CLOSED = 2

EXIT_UNAVAILABLE = 3

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不作为内容身份；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# 输出顶层**绝不允许**出现的 Review 结论字段（GPT-only Review 的机器可测边界）。
# 裁决记录里的 reviewer 身份是**事实**（嵌套在 ``items[].adjudication``），不是结论字段。
FORBIDDEN_MANIFEST_KEYS = (
    "verdict",
    "review_verdict",
    "acceptance_summary",
    "reviewed_at",
    "reviewer",
    "reviewer_role",
)

# ---------- 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言） ----------

ISSUE_PROJECT_STATE_UNREADABLE = "PROJECT_STATE_UNREADABLE"

ISSUE_LAST_REVIEWED_POINTER_MISSING = "LAST_REVIEWED_TASK_POINTER_MISSING"

ISSUE_ITEM_MANIFEST_UNUSABLE = "EVIDENCE_ITEM_MANIFEST_UNUSABLE"

ISSUE_ITEM_INVALID = "EVIDENCE_ITEM_INVALID"

ISSUE_ITEM_ADJUDICATION_INVALID = "EVIDENCE_ITEM_ADJUDICATION_INVALID"

ISSUE_ITEM_LEDGER_INVALID = "EVIDENCE_ITEM_LEDGER_INVALID"

ISSUE_ITEM_CHANGED_PATHS_UNAVAILABLE = "EVIDENCE_ITEM_CHANGED_PATHS_UNAVAILABLE"

ISSUE_KNOWN_CONTRADICTION_NOT_IN_BACKLOG = "KNOWN_CONTRADICTION_NOT_IN_BACKLOG"

# ``PROJECT_STATE`` 或 ledger 不可用 ⇒ 退出码 3（fail-closed，绝不把「读不到」当「没问题」）。
UNAVAILABLE_CODES = (ISSUE_PROJECT_STATE_UNREADABLE, *integrity.LEDGER_UNAVAILABLE_CODES)

# Manifest 注入点：测试可注入纯确定性 fake，完全零子进程 / 零 Git。
ManifestBuilder = Callable[[str], dict[str, Any]]

# 只读 Git 事实注入点：``(root, sha) -> (changed_paths, error)``。
ChangedPathsProvider = Callable[[Path, str], "tuple[list[str] | None, str | None]"]

EVIDENCE_ORDERING: dict[str, str] = {
    "items": "backlog order（review_backlog 的 planner.task_rank；项目前缀最新排最后）",
    "items[].commit.changed_paths": "sorted unique（Git 只读 log 输出）",
    "items[].validation.return_codes": "result attempts 顺序 + 段内 validation 顺序",
    "items[].reason_codes": "sorted unique",
    "issues": "sorted by (code, detail)",
    "reason_codes": "sorted unique issue codes",
}

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


def sort_issues(issues: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """确定性排序 + 去重（稳定 reason code + detail）。"""

    unique = {(issue["code"], issue["detail"]): issue for issue in issues}

    return sorted(unique.values(), key=lambda item: (item["code"], item["detail"]))


# ============================================================
# 只读 Git 事实（复用 §2.8 白名单，绝不另造 commit 身份算法）
# ============================================================


def git_changed_paths(root: Path, sha: str) -> tuple[list[str] | None, str | None]:
    """只读列出某 completion commit 改动的仓库相对路径。

    **复用** :func:`orchestrator.review_binding.run_git` 的只读子命令白名单
    （只需要 ``log``）；非白名单子命令会被拒绝执行，因此本函数结构上不可能写仓库。
    """

    if binding.FULL_COMMIT_SHA_PATTERN.match(str(sha)) is None:
        return None, f"非法 commit sha: {sha!r}"

    payload, error = binding.run_git(
        root, ["log", "-1", "--name-only", "--no-renames", "--format=", sha]
    )

    if error is not None or payload is None:
        return None, error or "empty git log output"

    paths = sorted(
        {line.strip() for line in payload.decode("utf-8", "replace").splitlines() if line.strip()}
    )

    return paths, None


# ============================================================
# 逐项事实：终态一致性（§2.13 唯一规则源）/ ledger 绑定 / 裁决状态（§2.15）
# ============================================================


def terminal_consistency_facts(payload: object) -> dict[str, Any]:
    """复用 §2.13 唯一规则源判定该 result 的终态一致性（只报告）。"""

    if not isinstance(payload, dict):
        return {
            "available": False,
            "consistent": None,
            "contradiction": None,
            "format": None,
            "reason_codes": [],
            "details": [],
        }

    report = terminal_consistency.analyze_result_terminal_consistency(payload)

    consistent = report.get("consistent") is True

    return {
        "available": True,
        "consistent": consistent,
        "contradiction": not consistent,
        "format": report.get("format"),
        "reason_codes": [str(code) for code in report.get("reason_codes") or []],
        "details": [dict(item) for item in report.get("details") or []],
    }


def is_recoverable_legacy_contradiction(terminal: Mapping[str, Any]) -> bool:
    """该矛盾是否属于 §2.15 有合法恢复路径的 legacy 终态矛盾。"""

    return (
        terminal.get("available") is True
        and terminal.get("contradiction") is True
        and terminal.get("format") == terminal_consistency.FORMAT_LEGACY
    )


def ledger_status_facts(
    task_id: str,
    entry_views: Sequence[dict[str, Any]],
    result_facts: Mapping[str, Any],
    commit_facts: Mapping[str, Any],
    facts_complete: bool,
) -> tuple[dict[str, Any], list[dict[str, str]], str]:
    """逐项 ledger 绑定事实（复用 §2.11 的 ``entry_identity`` / 漂移判定）。"""

    entries = backlog.ledger_entries_for(entry_views, task_id)

    entry_present = bool(entries)

    valid_entries = [entry for entry in entries if entry.get("valid") is True]

    entry_valid = entry_present and len(entries) == 1 and len(valid_entries) == 1

    issues: list[dict[str, str]] = []

    if entry_present and not entry_valid:
        issues.append(
            make_issue(
                ISSUE_ITEM_LEDGER_INVALID,
                f"{task_id}: ledger 存在 {len(entries)} 条记录（合法 {len(valid_entries)} 条），"
                "重复 / 非法条目整体作废，fail-closed",
            )
        )

    drift: list[dict[str, str]] = []

    if entry_valid and facts_complete:
        drift = backlog.identity_drift_issues(
            task_id,
            backlog.entry_identity(valid_entries[0]),
            backlog.manifest_identity(dict(result_facts), dict(commit_facts)),
        )

        issues.extend(drift)

    if entry_valid and not facts_complete:
        issues.append(
            make_issue(
                ISSUE_ITEM_LEDGER_INVALID,
                f"{task_id}: ledger 条目存在但 result / commit 事实不齐，无法核对客观身份",
            )
        )

    if (entry_present and not entry_valid) or (entry_valid and not facts_complete) or drift:
        status = LEDGER_STATUS_INVALID
    elif entry_valid:
        status = LEDGER_STATUS_BOUND
    else:
        status = LEDGER_STATUS_ABSENT

    section = {
        "entry_present": entry_present,
        "entry_count": len(entries),
        "entry_valid": entry_valid,
        "identity_consistent": None if not entry_valid else not drift,
        "status": status,
    }

    return section, issues, status


def classify_item(
    *,
    facts_complete: bool,
    binding_reason_codes: Sequence[str],
    terminal: Mapping[str, Any],
    adjudication_state: str | None,
    ledger_status: str,
) -> str:
    """四态客观分类（见模块 docstring；绝不产生 verdict）。

    只有 §2.15 可恢复的 **legacy** 终态矛盾才走「裁决」路径；其它缺 commit / hash 漂移 /
    事实不齐 / ledger 非法 / 裁决冲突一律 ``invalid``（fail-closed）。
    """

    if adjudication_state == adjudication.TASK_STATE_INVALID:
        return CLASSIFICATION_INVALID

    if ledger_status == LEDGER_STATUS_INVALID:
        return CLASSIFICATION_INVALID

    legacy_code = terminal_consistency.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS

    non_recoverable = [code for code in binding_reason_codes if code != legacy_code]

    recoverable = is_recoverable_legacy_contradiction(terminal)

    if not facts_complete and not (recoverable and not non_recoverable):
        return CLASSIFICATION_INVALID

    if recoverable and adjudication_state != adjudication.TASK_STATE_ADJUDICATED:
        return CLASSIFICATION_NEEDS_GPT_ADJUDICATION

    if recoverable:
        return CLASSIFICATION_FACTS_READY

    if ledger_status == LEDGER_STATUS_BOUND:
        return CLASSIFICATION_FACTS_READY

    return CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW


def fact_subset(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """按固定字段顺序取事实子集（保证输出稳定、无 wall-clock 混入）。"""

    return {key: source.get(key) for key in keys}


def live_facts_from_manifest(
    manifest: Mapping[str, Any] | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 §2.8 manifest 事实转成 §2.15 ``evaluate_task`` 需要的 live facts（同一身份口径）。"""

    if not isinstance(manifest, Mapping):
        return {}

    result = manifest.get("result")
    commit = manifest.get("commit")

    result_section = result if isinstance(result, Mapping) else {}
    commit_section = commit if isinstance(commit, Mapping) else {}

    return {
        "result_path": result_section.get("path"),
        "result_sha256": result_section.get("sha256"),
        "result_status": result_section.get("status"),
        "result_payload": payload,
        "commit_sha": commit_section.get("sha"),
        "commit_branch": commit_section.get("branch"),
        "commit_resolved": commit_section.get("resolved") is True,
    }


def adjudication_section(adjudication_view: Mapping[str, Any] | None) -> dict[str, Any]:
    """逐项裁决状态（来自 §2.15 ``evaluate_task`` 视图；无视图 ⇒ 未评估）。"""

    if not isinstance(adjudication_view, Mapping):
        return {
            "state": None,
            "valid": None,
            "adjudication": None,
            "reason_codes": [],
        }

    return {
        "state": adjudication_view.get("state"),
        "valid": adjudication_view.get("valid"),
        "adjudication": adjudication_view.get("adjudication"),
        "reason_codes": list(adjudication_view.get("reason_codes") or []),
    }


TASK_FACT_KEYS = (
    "path",
    "sha256",
    "bytes",
    "worktree_sha256",
    "worktree_bytes",
    "worktree_matches_commit",
    "task_id",
    "title",
    "type",
    "human_gate",
    "depends_on",
    "auto_start",
    "requires_human_approval",
    "max_attempts",
)

RESULT_FACT_KEYS = (
    "path",
    "sha256",
    "bytes",
    "worktree_sha256",
    "worktree_bytes",
    "worktree_matches_commit",
    "task_id",
    "status",
    "known_status",
    "terminal",
    "execution_outcome",
    "normalized_finish_reason",
    "finished_at",
    "attempt_count",
    "max_attempts",
)

COMMIT_FACT_KEYS = (
    "resolved",
    "reason_code",
    "sha",
    "branch",
    "head",
    "subject",
    "committed_at",
    "history_scope",
)


def validation_facts(validation_section: Mapping[str, Any]) -> dict[str, Any]:
    """validation 事实（命令 / 返回码 / 超时；**绝不在本工具里重新执行**）。"""

    results = validation_section.get("results")

    return {
        "status": validation_section.get("status"),
        "source": validation_section.get("source"),
        "attempt_count": validation_section.get("attempt_count"),
        "validation_count": validation_section.get("validation_count"),
        "commands": list(validation_section.get("commands") or []),
        "return_codes": [
            dict(entry)
            for entry in (results if isinstance(results, list) else [])
            if isinstance(entry, Mapping)
        ],
        "digest": validation_section.get("digest"),
    }


def build_evidence_item(
    task_id: str,
    manifest: Mapping[str, Any] | None,
    *,
    payload: dict[str, Any] | None,
    changed_paths: list[str] | None,
    changed_paths_error: str | None,
    adjudication_view: Mapping[str, Any] | None,
    entry_views: Sequence[dict[str, Any]],
    manifest_error: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """构建单个 backlog 项的确定性证据（只产事实，绝不签发结论）。"""

    usable = isinstance(manifest, Mapping) and manifest_error is None

    source: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}

    def section(key: str) -> Mapping[str, Any]:
        value = source.get(key)

        return value if isinstance(value, Mapping) else {}

    task_section = section("task")
    result_section = section("result")
    commit_section = section("commit")
    binding_section = section("binding")

    facts_complete = bool(binding_section.get("facts_complete"))

    binding_codes = sorted({str(code) for code in binding_section.get("reason_codes") or []})

    commit_resolved = commit_section.get("resolved") is True

    result_facts = fact_subset(result_section, RESULT_FACT_KEYS)

    commit_facts = {
        **fact_subset(commit_section, COMMIT_FACT_KEYS),
        "changed_paths": changed_paths,
        "changed_paths_reason_code": changed_paths_error,
    }

    terminal = terminal_consistency_facts(payload)

    ledger_facts, ledger_issues, ledger_status = ledger_status_facts(
        task_id, entry_views, result_facts, commit_facts, facts_complete
    )

    adj_state = adjudication_view.get("state") if isinstance(adjudication_view, Mapping) else None

    issues: list[dict[str, str]] = []

    if not usable:
        issues.append(
            make_issue(
                ISSUE_ITEM_MANIFEST_UNUSABLE,
                f"{task_id}: review_binding manifest 不可用"
                f"（{manifest_error or 'not a mapping'}）：事实不齐，fail-closed",
            )
        )

    issues.extend(ledger_issues)

    changed_paths_failed = commit_resolved and changed_paths_error is not None

    if changed_paths_failed:
        issues.append(
            make_issue(
                ISSUE_ITEM_CHANGED_PATHS_UNAVAILABLE,
                f"{task_id}: commit {commit_section.get('sha')!r} 的 changed paths 不可读"
                f"（{changed_paths_error}）：事实不齐，fail-closed",
            )
        )

    if not usable or changed_paths_failed:
        classification = CLASSIFICATION_INVALID
    else:
        classification = classify_item(
            facts_complete=facts_complete,
            binding_reason_codes=binding_codes,
            terminal=terminal,
            adjudication_state=adj_state,
            ledger_status=ledger_status,
        )

    if classification == CLASSIFICATION_INVALID:
        adj_codes = list(adjudication_view.get("reason_codes") or []) if isinstance(
            adjudication_view, Mapping
        ) else []

        if usable and adj_state == adjudication.TASK_STATE_INVALID:
            issues.append(
                make_issue(
                    ISSUE_ITEM_ADJUDICATION_INVALID,
                    f"{task_id}: 裁决记录存在但非法 / 冲突"
                    f"（{', '.join(adj_codes) or 'unknown'}）：fail-closed",
                )
            )
        elif usable and ledger_status != LEDGER_STATUS_INVALID and not facts_complete and not (
            is_recoverable_legacy_contradiction(terminal)
        ):
            issues.append(
                make_issue(
                    ISSUE_ITEM_INVALID,
                    f"{task_id}: review_binding facts_complete=false"
                    f"（{', '.join(binding_codes) or 'unknown'}）："
                    "缺 commit / hash 漂移 / 事实不齐，禁止猜测",
                )
            )

    reason_codes = sorted({*binding_codes, *(issue["code"] for issue in issues)})

    item: dict[str, Any] = {
        "task_id": task_id,
        "classification": classification,
        "facts_complete": facts_complete,
        "missing_reason_codes": binding_codes,
        "task": fact_subset(task_section, TASK_FACT_KEYS),
        "result": result_facts,
        "commit": commit_facts,
        "validation": validation_facts(section("validation")),
        "terminal_consistency": terminal,
        "ledger": ledger_facts,
        "adjudication": adjudication_section(adjudication_view),
        "binding_source": binding.REVIEW_BINDING_SCHEMA,
        "reason_codes": reason_codes,
    }

    return item, issues


def section_value(manifest: Mapping[str, Any] | None, key: str, field: str) -> Any:
    """从 manifest 的某个 mapping 段里安全读取单个字段（缺失 / 类型不符 ⇒ ``None``）。"""

    if not isinstance(manifest, Mapping):
        return None

    section = manifest.get(key)

    if not isinstance(section, Mapping):
        return None

    return section.get(field)


def default_manifest_builder(
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
) -> ManifestBuilder:
    """默认 manifest 来源：``orchestrator.review_binding``（§2.8 只读 Git 白名单）。"""

    def build(task_id: str) -> dict[str, Any]:
        return binding.build_review_binding_manifest(
            task_id,
            root=root,
            tasks_dir=tasks_dir,
            results_dir=results_dir,
        )

    return build


def collect_adjudication_store(store_path: Path) -> dict[str, Any]:
    """只读收集 §2.15 裁决 store 事实（``missing`` 是中性事实，绝不创建 / 修复 store）。

    **复用** :func:`orchestrator.legacy_result_adjudication.collect_store_index`
    （唯一 store 读取入口），本模块不另造第二套 store 解析 / 归集口径。
    """

    collected = adjudication.collect_store_index(store_path)

    # 「store 不存在」是正常初始状态（尚无真实裁决），不是 fail-closed；其余一律保留。
    issues = [
        issue
        for issue in collected["issues"]
        if str(issue.get("code")) != adjudication.ISSUE_STORE_MISSING
    ]

    return {
        "store_section": collected["store_section"],
        "issues": issues,
        "indexed_entries": collected["indexed_entries"],
        "consistency": collected["consistency"],
        "entries_by_task": collected["entries_by_task"],
    }


def build_review_evidence_manifest(
    *,
    root: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    state_path: Path | None = None,
    ledger_path: Path | None = None,
    adjudication_store_path: Path | None = None,
    generated_at: str | None = None,
    manifest_builder: ManifestBuilder | None = None,
    changed_paths_provider: ChangedPathsProvider | None = None,
) -> dict[str, Any]:
    """构建只读历史矛盾裁决证据包（**只产事实，绝不签发 Review / 裁决结论**）。"""

    resolved_root = Path(root) if root is not None else ROOT

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else Path(orch.TASK_DIR)

    resolved_results = Path(results_dir) if results_dir is not None else Path(orch.RESULT_DIR)

    resolved_state = Path(state_path) if state_path is not None else planner.PROJECT_STATE_PATH

    resolved_ledger = (
        Path(ledger_path)
        if ledger_path is not None
        else resolved_root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
    )

    resolved_store = (
        Path(adjudication_store_path)
        if adjudication_store_path is not None
        else resolved_root / adjudication.ADJUDICATION_STORE_RELATIVE_PATH
    )

    builder = (
        manifest_builder
        if manifest_builder is not None
        else default_manifest_builder(resolved_root, resolved_tasks, resolved_results)
    )

    changed_provider = (
        changed_paths_provider if changed_paths_provider is not None else git_changed_paths
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

    entry_views = [entry for entry in ledger_view["entries"] if isinstance(entry, dict)]

    last_reviewed = planner.pointer_value(state, "last_reviewed_task")

    if last_reviewed is None:
        issues.append(
            make_issue(
                ISSUE_LAST_REVIEWED_POINTER_MISSING,
                "PROJECT_STATE.last_reviewed_task 缺失：无法确定证据包边界，fail-closed",
            )
        )

    project_prefix = planner.project_task_prefix(state) if state else None

    completed_ids = ledger_mod.completed_result_ids(statuses, project_prefix)

    backlog_ids = (
        []
        if last_reviewed is None
        else [task_id for task_id in completed_ids if planner.is_newer(task_id, last_reviewed)]
    )

    for task_id in adjudication.KNOWN_CONTRADICTION_TASKS:
        if statuses.get(task_id) == "completed" and task_id not in backlog_ids:
            issues.append(
                make_issue(
                    ISSUE_KNOWN_CONTRADICTION_NOT_IN_BACKLOG,
                    f"{task_id}: 已知历史矛盾任务已完成却不在 backlog（"
                    f"last_reviewed_task={last_reviewed!r}）：矛盾未经裁决即被越过，fail-closed",
                )
            )

    store = collect_adjudication_store(resolved_store)

    issues.extend(store["issues"])

    items: list[dict[str, Any]] = []

    for task_id in backlog_ids:
        manifest: dict[str, Any] | None = None

        manifest_error: str | None = None

        try:
            manifest = builder(task_id)

        except Exception as exc:  # noqa: BLE001 - builder 异常必须 fail-closed，绝不中断整轮
            manifest_error = f"{type(exc).__name__}: {exc}"

        payload: dict[str, Any] | None = None

        result_path = section_value(manifest, "result", "path")

        if isinstance(result_path, str):
            payload, _error = planner.read_json_mapping(Path(result_path))

        changed_paths: list[str] | None = None

        changed_error: str | None = None

        commit_sha = section_value(manifest, "commit", "sha")

        if isinstance(commit_sha, str) and commit_sha:
            changed_paths, changed_error = changed_provider(resolved_root, commit_sha)

        live = live_facts_from_manifest(manifest, payload)

        adjudication_view = adjudication.evaluate_task(
            task_id,
            entries_by_task=store["entries_by_task"],
            indexed_entries=store["indexed_entries"],
            consistency=store["consistency"],
            live=live,
            as_of=None,
        )

        item, item_issues = build_evidence_item(
            task_id,
            manifest,
            payload=payload,
            changed_paths=changed_paths,
            changed_paths_error=changed_error,
            adjudication_view=adjudication_view,
            entry_views=entry_views,
            manifest_error=manifest_error,
        )

        items.append(item)

        issues.extend(item_issues)

    ordered_issues = sort_issues(issues)

    classification_counts = {
        classification: sum(1 for item in items if item["classification"] == classification)
        for classification in CLASSIFICATIONS
    }

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "paths": {
            "root": str(resolved_root),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
            "project_state": str(resolved_state),
            "review_ledger": str(resolved_ledger),
            "adjudication_store": str(resolved_store),
        },
        "coverage": {
            "last_reviewed_task_pointer": last_reviewed,
            "last_reviewed_task_pointer_present": last_reviewed is not None,
            "project_prefix": project_prefix,
            "completed_result_count": len(completed_ids),
            "newest_completed_result": completed_ids[-1] if completed_ids else None,
            "item_count": len(backlog_ids),
            "items_first": backlog_ids[0] if backlog_ids else None,
            "items_last": backlog_ids[-1] if backlog_ids else None,
            "known_contradiction_tasks": list(adjudication.KNOWN_CONTRADICTION_TASKS),
            "known_contradiction_tasks_in_backlog": [
                task_id
                for task_id in adjudication.KNOWN_CONTRADICTION_TASKS
                if task_id in backlog_ids
            ],
        },
        "items": items,
        "ledger": {
            "available": ledger_view["available"],
            "schema": ledger_view["schema"],
            "schema_version": ledger_view["schema_version"],
            "entry_count": len(entry_views),
            "valid_entry_count": sum(1 for entry in entry_views if entry.get("valid") is True),
            "entry_task_ids": [entry.get("task_id") for entry in entry_views],
            "duplicate_tasks": list(ledger_view["duplicate_tasks"]),
            "coverage_floor": ledger_view["coverage_floor"],
        },
        "adjudication": dict(store["store_section"]),
        "authority": authority_section(),
        "contract": contract_section(),
        "determinism": determinism_section(),
        "issues": ordered_issues,
        "reason_codes": sorted({str(issue["code"]) for issue in ordered_issues}),
        "missing_reason_codes": sorted(
            {code for item in items for code in item["missing_reason_codes"]}
        ),
        "summary": {
            "item_count": len(items),
            "facts_ready_count": classification_counts[CLASSIFICATION_FACTS_READY],
            "needs_gpt_adjudication_count": classification_counts[
                CLASSIFICATION_NEEDS_GPT_ADJUDICATION
            ],
            "pending_substantive_review_count": classification_counts[
                CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW
            ],
            "invalid_count": classification_counts[CLASSIFICATION_INVALID],
            "classification_counts": classification_counts,
            "contradiction_count": sum(
                1 for item in items if item["terminal_consistency"]["contradiction"] is True
            ),
            "adjudicated_count": sum(
                1
                for item in items
                if item["adjudication"]["state"] == adjudication.TASK_STATE_ADJUDICATED
            ),
            "ledger_bound_count": sum(
                1 for item in items if item["ledger"]["status"] == LEDGER_STATUS_BOUND
            ),
            "issue_count": len(ordered_issues),
            "error_count": len(ordered_issues),
            "warning_count": 0,
            "exit_code": evidence_exit_code(ordered_issues, items),
        },
    }

    report["facts_digest"] = evidence_facts_digest(report)

    return report


def evidence_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in payload.items() if key not in FACTS_EXCLUDED_KEYS}


def evidence_facts_digest(payload: Mapping[str, Any]) -> str:
    """``facts_digest``：相同仓库事实 ⇒ 相同 digest（幂等、无自引用、排除 wall-clock）。"""

    return canonical_digest(evidence_facts(payload))


def evidence_exit_code(
    issues: Sequence[dict[str, str]],
    items: Sequence[dict[str, Any]],
) -> int:
    """退出码：``3`` state / ledger 不可用；``2`` fail-closed；``0`` 无 fail-closed 项。

    ``2`` 同时覆盖「存在待裁决矛盾」——它是 GPT 必须处理的事实，绝不是「已通过」。
    """

    codes = {str(issue.get("code")) for issue in issues}

    if any(code in codes for code in UNAVAILABLE_CODES):
        return EXIT_UNAVAILABLE

    if issues:
        return EXIT_FAIL_CLOSED

    blocking = (CLASSIFICATION_INVALID, CLASSIFICATION_NEEDS_GPT_ADJUDICATION)

    if any(item.get("classification") in blocking for item in items):
        return EXIT_FAIL_CLOSED

    return EXIT_OK


def authority_section() -> dict[str, Any]:
    """本工具的**职责边界**（机器可读：只产客观事实，绝不签发 / 写任何结论）。"""

    return {
        "schema": AUTHORITY_SCHEMA,
        "read_only": True,
        "emits_review_outcome": False,
        "emits_review_verdict_values": False,
        "creates_verdict": False,
        "tool_can_sign_review": False,
        "tool_can_sign_adjudication": False,
        "tool_can_write_review_ledger": False,
        "tool_can_write_adjudication": False,
        "tool_can_advance_review_pointer": False,
        "tool_can_advance_state": False,
        "tool_can_generate_tasks": False,
        "tool_can_refill_queue": False,
        "tool_can_qualify_data": False,
        "tool_can_cross_human_gate": False,
        "writes_review_ledger": False,
        "writes_adjudication": False,
        "writes_project_state": False,
        "writes_tasks": False,
        "writes_results": False,
        "network_access": False,
        "model_calls": False,
        "validation_pass_is_not_review_verdict": True,
        "exit_code_zero_is_not_review_pass": True,
        "review_authority": "gpt_only",
        "planner_agents": list(planner.PLANNER_AGENTS),
        "executor_agents": list(planner.EXECUTOR_AGENTS),
        "classification_values": list(CLASSIFICATIONS),
        "binding_source": binding.REVIEW_BINDING_SCHEMA,
        "terminal_consistency_source": terminal_consistency.SCHEMA,
        "adjudication_contract_source": adjudication.SCHEMA,
        "ledger_source": ledger_mod.REVIEW_LEDGER_SCHEMA,
    }


def contract_section() -> dict[str, Any]:
    """分类词表 / fail-closed 规则的机器可读声明（只有规则，不含状态事实）。"""

    return {
        "schema": CONTRACT_SCHEMA,
        "classification_values": list(CLASSIFICATIONS),
        "classification_semantics": {
            CLASSIFICATION_FACTS_READY: (
                "事实齐全且无未解阻塞（无 legacy 矛盾 / 矛盾已有有效 GPT 裁决 / "
                "已被 ledger 合法绑定）"
            ),
            CLASSIFICATION_NEEDS_GPT_ADJUDICATION: (
                "事实齐全但存在未裁决的 legacy 终态矛盾，必须先由 GPT 裁决"
            ),
            CLASSIFICATION_PENDING_SUBSTANTIVE_REVIEW: (
                "事实齐全、无矛盾、尚未绑定，等待 GPT 实质 review"
            ),
            CLASSIFICATION_INVALID: (
                "缺 commit / hash 漂移 / 事实不齐 / ledger 非法 / 裁决冲突，fail-closed"
            ),
        },
        "recoverable_contradiction_code": (
            terminal_consistency.REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS
        ),
        "terminal_consistency_rule_source": terminal_consistency.SCHEMA,
        "binding_rule_source": binding.REVIEW_BINDING_SCHEMA,
        "adjudication_contract_source": adjudication.SCHEMA,
        "ledger_rule_source": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "backlog_scope_rule": (
            "completed results strictly newer than PROJECT_STATE.last_reviewed_task"
        ),
        "fail_closed_on": [
            "missing completion commit",
            "content hash drift",
            "incomplete binding facts",
            "invalid / conflicting adjudication",
            "invalid / duplicate ledger entry",
            "unavailable changed paths for a resolved commit",
        ],
        "validation_pass_is_not_review_verdict": True,
        "exit_code_zero_is_not_review_pass": True,
        "exit_code_semantics": {
            "0": "客观事实可复算且无 fail-closed 项（绝不是 GPT PASS）",
            "2": "fail-closed（缺 commit / hash 漂移 / 事实不齐 / 裁决冲突 / 待裁决矛盾）",
            "3": "PROJECT_STATE 或 review ledger 不可用",
            "4": "--output 目标被 fail-closed 拒绝（绝不写文件）",
        },
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "mtime_used_as_identity": False,
        "model_output_used_as_identity": False,
        "identity_source": (
            "orchestrator.review_binding canonical git blob bytes + HEAD-reachable terminal commit"
        ),
        "changed_paths_source": (
            "orchestrator.review_binding.run_git read-only `log -1 --name-only`"
        ),
        "ordering": dict(EVIDENCE_ORDERING),
    }


def render_review_evidence_manifest(payload: Mapping[str, Any]) -> str:
    """确定性 JSON 渲染（固定字段顺序 + 缩进；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_evidence_manifest",
        description=(
            "历史矛盾任务的确定性裁决证据包（GOLD-043）：只读汇总 last_reviewed_task "
            "之后的完整 backlog（内容身份 / 完成 commit / changed paths / validation 返回码 / "
            "终态一致性 / ledger 与裁决状态），并给出稳定分类；绝不签发 Review 或裁决、"
            "绝不修改任何历史 result、ledger、state、tasks 或 adjudication。"
        ),
    )

    parser.add_argument(
        "--root", type=Path, default=None, help="仓库根目录（默认：本模块所在项目根）"
    )

    parser.add_argument(
        "--tasks-dir", type=Path, default=None, help="tasks 目录（默认 <root>/.ai/tasks）"
    )

    parser.add_argument(
        "--results-dir", type=Path, default=None, help="results 目录（默认 <root>/.ai/results）"
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
        help="review ledger 路径（默认 <root>/.ai/GPT_REVIEW_LEDGER.json）",
    )

    parser.add_argument(
        "--adjudication-store",
        type=Path,
        default=None,
        help=f"裁决 store 路径（默认 <root>/{adjudication.ADJUDICATION_STORE_RELATIVE_PATH}）",
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

    store_path = (
        Path(args.adjudication_store)
        if args.adjudication_store is not None
        else root / adjudication.ADJUDICATION_STORE_RELATIVE_PATH
    )

    payload = build_review_evidence_manifest(
        root=root,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        state_path=state_path,
        ledger_path=ledger_path,
        adjudication_store_path=store_path,
        generated_at=args.generated_at,
    )

    rendered = render_review_evidence_manifest(payload)

    if args.output is None:
        sys.stdout.write(rendered)

        sys.stdout.flush()
    else:
        target, reason = snapshot_output.resolve_output_target(root, args.output)

        if target is None:
            print(
                f"[error] {snapshot_output.ISSUE_OUTPUT_PATH_REJECTED}: {reason}",
                file=sys.stderr,
            )

            return snapshot_output.EXIT_OUTPUT_REJECTED

        snapshot_output.write_snapshot_output(target, rendered)

        print(f"[info] review evidence manifest 已写入受控路径: {target}", file=sys.stderr)

    for issue in payload["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    summary = payload["summary"]

    print(
        "[evidence] last_reviewed={last_reviewed} items={count} facts_ready={ready} "
        "needs_adjudication={needs} pending_review={pending} invalid={invalid} exit={code}".format(
            last_reviewed=payload["coverage"]["last_reviewed_task_pointer"],
            count=summary["item_count"],
            ready=summary["facts_ready_count"],
            needs=summary["needs_gpt_adjudication_count"],
            pending=summary["pending_substantive_review_count"],
            invalid=summary["invalid_count"],
            code=summary["exit_code"],
        ),
        file=sys.stderr,
    )

    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
