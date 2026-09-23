"""GPT Review Ledger 完整性 / 连续性只读门禁（GOLD-032）。

为什么需要它
------------
GOLD-025 把 GPT Review 变成机器可审计的台账（``.ai/GPT_REVIEW_LEDGER.json``），
GOLD-031 提供只读的 **Review Binding Manifest**（result SHA-256 + reviewed commit
identity）。但两者之间**没有任何东西**保证台账本身没有被**静默删改**：

- 删掉某条 review 条目 / 调换条目顺序 / 只改一个 ``result_sha256`` 或 ``commit.sha``，
  旧实现无法机器判定（只能靠人肉 diff）；
- 台账里记录的客观身份（result 内容 SHA-256、完成 commit identity）可能与
  ``orchestrator.review_binding`` 复算出来的**事实**脱节，而没有人会发现。

本模块只做一件事：把「台账是不是仍然与客观事实绑定、顺序是否仍然连续」变成
**纯只读、确定性、fail-closed** 的验证，并给出稳定 reason code。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：本模块没有任何写入路径：不写 ``.ai/GPT_REVIEW_LEDGER.json`` /
  ``.ai/PROJECT_STATE.json`` / tasks / results，也绝不 commit / push / reset /
  checkout（由源码守卫测试锁定）；
- **绝不签发 / 修改 Review 结论**：输出里只会**原样回显**台账已有的 ``verdict`` /
  ``reviewed_at``，**绝不**创建或修改 verdict / ``acceptance_summary`` /
  ``reviewed_at``；``authority`` 段硬编码 ``tool_can_sign_review=false`` /
  ``tool_can_repair_ledger=false`` / ``tool_can_advance_state=false`` /
  ``writes_*=false``；
- **绝不自动修复**：任何缺项 / 重复 / 顺序回退 / hash 或 commit 不一致 / 未知
  verdict 或 schema 一律 fail-closed **只报告**，绝不重排、绝不补条目、绝不改哈希；
- 外部进程调用**只经由** ``orchestrator.review_binding`` 的只读 Git 白名单
  （``log`` / ``ls-tree`` / ``cat-file`` / ``hash-object``）；本模块自身不启动任何
  外部进程，零网络、零数据库、零业务证据、零模型调用；
- 不改变 rolling queue / Phase / L1~L4 档位 / 数据资格 Gate / 交易安全开关。

用法
----
.. code-block:: text

    python -m orchestrator.review_ledger_integrity               # 只读报告写 stdout
    python -m orchestrator.review_ledger_integrity --root .      # 指定仓库根
    python -m orchestrator.review_ledger_integrity --ledger <path>

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放
人类可读的 issue 摘要（不参与机器解析）。
退出码：``0`` 台账完整且与客观事实绑定 / ``2`` 检出完整性漂移 /
``3`` 台账缺失 / 损坏 / schema 或 entries 不可用（均 fail-closed）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner
from orchestrator import review_binding as binding
from orchestrator import review_ledger as ledger_mod

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
LEDGER_INTEGRITY_SCHEMA = "gold-ai/review-ledger-integrity/v1"

LEDGER_INTEGRITY_SCHEMA_VERSION = 1

LEDGER_INTEGRITY_AUTHORITY_SCHEMA = "gold-ai/review-ledger-integrity-authority/v1"

SEVERITY_ERROR = "error"

EXIT_OK = 0

EXIT_INTEGRITY_FAIL = 2

EXIT_LEDGER_UNAVAILABLE = 3

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不作为内容身份；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# ---------- 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言） ----------

ISSUE_LEDGER_MISSING = "LEDGER_MISSING"
ISSUE_LEDGER_UNREADABLE = "LEDGER_UNREADABLE"
ISSUE_LEDGER_SCHEMA_UNSUPPORTED = "LEDGER_SCHEMA_UNSUPPORTED"
ISSUE_LEDGER_ENTRIES_INVALID = "LEDGER_ENTRIES_INVALID"
ISSUE_LEDGER_METADATA_INVALID = "LEDGER_METADATA_INVALID"
ISSUE_LEDGER_ENTRY_INVALID = "LEDGER_ENTRY_INVALID"
ISSUE_LEDGER_DUPLICATE_TASK = "LEDGER_DUPLICATE_TASK"
ISSUE_LEDGER_ORDER_REGRESSION = "LEDGER_ORDER_REGRESSION"
ISSUE_LEDGER_REVIEW_TIME_REGRESSION = "LEDGER_REVIEW_TIME_REGRESSION"
ISSUE_LEDGER_CHAIN_GAP = "LEDGER_CHAIN_GAP"
ISSUE_MANIFEST_FACTS_INCOMPLETE = "LEDGER_MANIFEST_FACTS_INCOMPLETE"
ISSUE_RESULT_HASH_MISMATCH = "LEDGER_RESULT_HASH_MISMATCH"
ISSUE_RESULT_STATUS_MISMATCH = "LEDGER_RESULT_STATUS_MISMATCH"
ISSUE_RESULT_FINISHED_AT_MISMATCH = "LEDGER_RESULT_FINISHED_AT_MISMATCH"
ISSUE_COMMIT_SHA_MISMATCH = "LEDGER_COMMIT_SHA_MISMATCH"
ISSUE_COMMIT_BRANCH_MISMATCH = "LEDGER_COMMIT_BRANCH_MISMATCH"

# 台账本身不可用 ⇒ 退出码 3（fail-closed，绝不把「读不到」当成「没问题」）。
LEDGER_UNAVAILABLE_CODES = (
    ISSUE_LEDGER_MISSING,
    ISSUE_LEDGER_UNREADABLE,
    ISSUE_LEDGER_SCHEMA_UNSUPPORTED,
    ISSUE_LEDGER_ENTRIES_INVALID,
)

# review_ledger 的 issue code → 本模块稳定 code（未知一律归入条目级 invalid）。
LEDGER_ISSUE_CODE_MAP: dict[str, str] = {
    ledger_mod.ISSUE_LEDGER_MISSING: ISSUE_LEDGER_MISSING,
    ledger_mod.ISSUE_LEDGER_UNREADABLE: ISSUE_LEDGER_UNREADABLE,
    ledger_mod.ISSUE_LEDGER_SCHEMA_UNSUPPORTED: ISSUE_LEDGER_SCHEMA_UNSUPPORTED,
    ledger_mod.ISSUE_LEDGER_ENTRIES_INVALID: ISSUE_LEDGER_ENTRIES_INVALID,
    ledger_mod.ISSUE_LEDGER_METADATA_INVALID: ISSUE_LEDGER_METADATA_INVALID,
    ledger_mod.ISSUE_ENTRY_DUPLICATE: ISSUE_LEDGER_DUPLICATE_TASK,
}

# 确定性排序契约（机器可读，供 GPT 与测试断言；只描述规则，不含事实）。
INTEGRITY_ORDERING = {
    "ledger.entries": "ledger 文件原始顺序（台账必须保持确定性升序）",
    "bindings": "sorted by planner.task_id_sort_key（仅收录完整合法条目）",
    "issues": "sorted by (code, detail)",
}

# Manifest 注入点：测试可注入纯确定性 fake，完全零子进程 / 零 Git。
ManifestBuilder = Callable[[str], dict[str, Any]]


def make_issue(code: str, detail: str) -> dict[str, str]:
    """构造一条 fail-closed 诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": SEVERITY_ERROR, "detail": detail}


def canonical_digest(payload: object) -> str:
    """确定性 JSON 摘要（固定 sort_keys + 紧凑分隔符 ⇒ 相同事实必然相同 digest）。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# ============================================================
# 台账原始顺序 / 时间事实（保持 ledger 文件顺序，绝不被 view 的重排覆盖）
# ============================================================


def raw_entry_facts(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """台账 entries 的**原始文件顺序**事实（``task_id`` / ``reviewed_at``）。

    ``review_ledger.validate_review_ledger`` 会按 task_id 重排 entries；顺序回退
    必须针对**文件里的真实顺序**判定，因此这里单独读原始 payload。
    """

    facts: list[dict[str, Any]] = []

    if not isinstance(payload, dict):
        return facts

    entries = payload.get("entries")

    if not isinstance(entries, list):
        return facts

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            facts.append({"index": index, "task_id": None, "reviewed_at": None})

            continue

        facts.append(
            {
                "index": index,
                "task_id": ledger_mod.text_value(entry.get("task_id")),
                "reviewed_at": ledger_mod.iso_timestamp(entry.get("reviewed_at")),
            }
        )

    return facts


def order_regression_issues(facts: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """顺序回退：台账条目必须按 ``planner.task_id_sort_key`` 升序排列。"""

    issues: list[dict[str, str]] = []

    previous_id: str | None = None
    previous_key: tuple[int, str, int, int, str] | None = None

    for fact in facts:
        task_id = fact["task_id"]

        if task_id is None:
            continue

        key = planner.task_id_sort_key(task_id)

        if previous_key is not None and key < previous_key:
            issues.append(
                make_issue(
                    ISSUE_LEDGER_ORDER_REGRESSION,
                    f"ledger 顺序回退：entries[{fact['index']}] {task_id} 排在 {previous_id} 之后"
                    "（台账必须保持确定性升序；本工具只报告，绝不重排 / 修复）",
                )
            )

        previous_id = task_id
        previous_key = key

    return issues


def review_time_regression_issues(facts: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """review 时间回退：``reviewed_at`` 不得在台账里倒退（连续审阅语义）。"""

    issues: list[dict[str, str]] = []

    previous_at: datetime | None = None
    previous_id: str | None = None

    for fact in facts:
        text = fact["reviewed_at"]

        if text is None:
            continue

        current = datetime.fromisoformat(text)

        if (
            previous_at is not None
            and (previous_at.tzinfo is None) == (current.tzinfo is None)
            and current < previous_at
        ):
            issues.append(
                make_issue(
                    ISSUE_LEDGER_REVIEW_TIME_REGRESSION,
                    f"ledger review 时间回退：{fact['task_id']} 的 reviewed_at={text} 早于"
                    f" 前一条 {previous_id} 的 reviewed_at（连续性漂移；只报告）",
                )
            )

        previous_at = current
        previous_id = fact["task_id"]

    return issues


def completed_result_ids(results_dir: Path) -> list[str]:
    """全部 ``completed`` 的 result task_id（升序，只读）。"""

    statuses = planner.result_statuses(results_dir)

    return sorted(
        (task_id for task_id, status in statuses.items() if status == "completed"),
        key=planner.task_id_sort_key,
    )


def chain_section(
    facts: Sequence[dict[str, Any]],
    reviewed_ids: Sequence[str],
    ledger_view: dict[str, Any],
    results_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """台账连续性视图：顺序 / review 时间单调性 + 覆盖窗口内的缺项。"""

    issues: list[dict[str, str]] = []

    issues.extend(order_regression_issues(facts))

    issues.extend(review_time_regression_issues(facts))

    ordered_reviewed = sorted(reviewed_ids, key=planner.task_id_sort_key)

    floor = ledger_mod.text_value(ledger_view.get("coverage_floor"))

    if ordered_reviewed:
        window_from: str | None = floor or ordered_reviewed[0]
        window_to: str | None = ordered_reviewed[-1]
    else:
        window_from = floor
        window_to = None

    completed_in_window: list[str] = []
    missing_in_window: list[str] = []

    if window_from is not None and window_to is not None:
        lower = planner.task_id_sort_key(window_from)
        upper = planner.task_id_sort_key(window_to)
        reviewed = set(ordered_reviewed)

        for task_id in completed_result_ids(results_dir):
            key = planner.task_id_sort_key(task_id)

            if key < lower or key > upper:
                continue

            completed_in_window.append(task_id)

            if task_id in reviewed:
                continue

            missing_in_window.append(task_id)

            issues.append(
                make_issue(
                    ISSUE_LEDGER_CHAIN_GAP,
                    f"{task_id}: result completed 且位于台账覆盖窗口"
                    f" [{window_from}, {window_to}] 内，但 ledger 无对应记录："
                    "条目被删除 / 遗漏（fail-closed，绝不自动补项）",
                )
            )

    section: dict[str, Any] = {
        "ledger_order": [fact["task_id"] for fact in facts],
        "reviewed_chain": ordered_reviewed,
        "ordered_by": "planner.task_id_sort_key",
        "order_is_ascending": not any(
            issue["code"] == ISSUE_LEDGER_ORDER_REGRESSION for issue in issues
        ),
        "reviewed_at_is_ascending": not any(
            issue["code"] == ISSUE_LEDGER_REVIEW_TIME_REGRESSION for issue in issues
        ),
        "window": {"from": window_from, "to": window_to},
        "completed_results_in_window": completed_in_window,
        "missing_in_window": missing_in_window,
    }

    return section, issues


# ============================================================
# 台账条目 vs GOLD-031 manifest 的客观事实绑定
# ============================================================


def binding_entry_view(
    entry_view: dict[str, Any],
    builder: ManifestBuilder,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把一条有效台账条目的客观身份与 manifest 事实逐项比对（只读、fail-closed）。"""

    task_id = str(entry_view["task_id"])

    manifest = builder(task_id)

    declared_result = entry_view["reviewed_result"]
    declared_commit = entry_view["reviewed_commit"]

    expected: dict[str, Any] = {
        "result_sha256": declared_result.get("result_sha256"),
        "result_status": declared_result.get("status"),
        "result_finished_at": declared_result.get("finished_at"),
        "commit_sha": declared_commit.get("sha"),
        "commit_branch": declared_commit.get("branch"),
    }

    manifest_binding = manifest.get("binding") if isinstance(manifest, dict) else None
    manifest_result = manifest.get("result") if isinstance(manifest, dict) else None
    manifest_commit = manifest.get("commit") if isinstance(manifest, dict) else None

    facts_complete = bool((manifest_binding or {}).get("facts_complete"))

    reason_codes = [str(code) for code in (manifest_binding or {}).get("reason_codes") or []]

    actual: dict[str, Any] = {
        "result_sha256": (manifest_result or {}).get("sha256"),
        "result_status": (manifest_result or {}).get("status"),
        "result_finished_at": (manifest_result or {}).get("finished_at"),
        "commit_sha": (manifest_commit or {}).get("sha"),
        "commit_branch": (manifest_commit or {}).get("branch"),
    }

    issues: list[dict[str, str]] = []

    matches: dict[str, bool | None] = {field: None for field in expected}

    if not facts_complete:
        issues.append(
            make_issue(
                ISSUE_MANIFEST_FACTS_INCOMPLETE,
                f"{task_id}: GOLD-031 manifest facts_complete=false"
                f"（{', '.join(reason_codes) or 'unknown'}）：客观绑定事实不齐，"
                "台账条目不可信",
            )
        )
    else:
        checks = (
            ("result_sha256", ISSUE_RESULT_HASH_MISMATCH),
            ("result_status", ISSUE_RESULT_STATUS_MISMATCH),
            ("result_finished_at", ISSUE_RESULT_FINISHED_AT_MISMATCH),
            ("commit_sha", ISSUE_COMMIT_SHA_MISMATCH),
            ("commit_branch", ISSUE_COMMIT_BRANCH_MISMATCH),
        )

        for field, code in checks:
            declared_value = expected[field]
            manifest_value = actual[field]

            if declared_value is None or manifest_value is None:
                # 任何一方缺失都在 manifest facts 层 fail-closed；此处保守跳过不可比项。
                continue

            ok = declared_value == manifest_value

            matches[field] = ok

            if not ok:
                issues.append(
                    make_issue(
                        code,
                        f"{task_id}: ledger {field}={declared_value!r} 与 manifest "
                        f"{manifest_value!r} 不一致（台账删改 / 漂移，fail-closed）",
                    )
                )

    view: dict[str, Any] = {
        "task_id": task_id,
        "verdict": entry_view.get("verdict"),
        "reviewed_at": entry_view.get("reviewed_at"),
        "expected": expected,
        "actual": actual,
        "manifest_facts_complete": facts_complete,
        "manifest_reason_codes": reason_codes,
        "manifest_facts_digest": (
            manifest.get("facts_digest") if isinstance(manifest, dict) else None
        ),
        "matches": matches,
        "bound": facts_complete and not issues,
    }

    return view, issues


# ============================================================
# 职责边界 / 确定性契约（只有规则，不含状态事实）
# ============================================================


def authority_section() -> dict[str, Any]:
    """本工具的**职责边界**（机器可读契约：只验证客观事实，不产结论、不写状态）。"""

    return {
        "schema": LEDGER_INTEGRITY_AUTHORITY_SCHEMA,
        "read_only": True,
        "emits_review_outcome": False,
        "echoes_ledger_facts_only": True,
        "creates_verdicts": False,
        "modifies_verdicts": False,
        "creates_acceptance_summary": False,
        "creates_reviewed_at": False,
        "tool_can_sign_review": False,
        "tool_can_repair_ledger": False,
        "tool_can_advance_state": False,
        "tool_can_qualify_data": False,
        "tool_can_cross_human_gate": False,
        "writes_review_ledger": False,
        "writes_project_state": False,
        "writes_tasks": False,
        "writes_results": False,
        "review_authority": "gpt_only",
        "verdict_source": "echoed from GPT ledger; never created or modified by this tool",
        "manifest_source": binding.REVIEW_BINDING_SCHEMA,
        "ledger_schema": ledger_mod.REVIEW_LEDGER_SCHEMA,
        "blocking_human_gates": sorted(orch.BLOCKING_HUMAN_GATES),
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "mtime_used_as_identity": False,
        "content_identity_source": "orchestrator.review_binding canonical git blob bytes",
        "ordering": dict(INTEGRITY_ORDERING),
    }


def integrity_facts(report: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in report.items() if key not in FACTS_EXCLUDED_KEYS}


def integrity_facts_digest(report: dict[str, Any]) -> str:
    """确定性事实的 sha256：相同输入 ⇒ 相同 digest（幂等、无自引用）。"""

    return canonical_digest(integrity_facts(report))


def report_exit_code(issues: Sequence[dict[str, str]]) -> int:
    """退出码：``0`` 完整且绑定 / ``2`` 完整性漂移 / ``3`` 台账不可用（fail-closed）。"""

    codes = {issue["code"] for issue in issues}

    if any(code in codes for code in LEDGER_UNAVAILABLE_CODES):
        return EXIT_LEDGER_UNAVAILABLE

    if issues:
        return EXIT_INTEGRITY_FAIL

    return EXIT_OK


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
# 完整性报告组装（只读事实；绝不签发 / 修改 Review 结论）
# ============================================================


def translate_ledger_issue(code: str) -> str:
    """review_ledger issue code → 本模块稳定 code（未知一律条目级 invalid）。"""

    return LEDGER_ISSUE_CODE_MAP.get(code, ISSUE_LEDGER_ENTRY_INVALID)


def build_integrity_report(
    *,
    root: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    ledger_path: Path | None = None,
    generated_at: str | None = None,
    manifest_builder: ManifestBuilder | None = None,
) -> dict[str, Any]:
    """构建只读 ledger 完整性 / 连续性报告（**只验证客观事实，绝不修复台账**）。"""

    resolved_root = Path(root) if root is not None else ROOT

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else Path(orch.TASK_DIR)

    resolved_results = Path(results_dir) if results_dir is not None else Path(orch.RESULT_DIR)

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

    payload, error = ledger_mod.load_review_ledger(resolved_ledger)

    ledger_view, ledger_issues = ledger_mod.validate_review_ledger(payload, error)

    for ledger_issue in ledger_issues:
        issues.append(
            make_issue(
                translate_ledger_issue(ledger_issue["code"]),
                f"{ledger_issue['code']}: {ledger_issue['detail']}",
            )
        )

    facts = raw_entry_facts(payload)

    valid_entries = [entry for entry in ledger_view["entries"] if entry["valid"]]

    reviewed_ids = [str(entry["task_id"]) for entry in valid_entries]

    chain, chain_issues = chain_section(facts, reviewed_ids, ledger_view, resolved_results)

    issues.extend(chain_issues)

    bindings: list[dict[str, Any]] = []

    for entry in sorted(
        valid_entries, key=lambda item: planner.task_id_sort_key(str(item["task_id"]))
    ):
        view, binding_issues = binding_entry_view(entry, builder)

        bindings.append(view)

        issues.extend(binding_issues)

    deduped = list(
        {(issue["code"], issue["detail"]): issue for issue in issues}.values()
    )

    ordered = sorted(deduped, key=lambda issue: (issue["code"], issue["detail"]))

    bound_count = sum(1 for view in bindings if view["bound"])

    report: dict[str, Any] = {
        "schema": LEDGER_INTEGRITY_SCHEMA,
        "schema_version": LEDGER_INTEGRITY_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "paths": {
            "root": str(resolved_root),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
            "review_ledger": str(resolved_ledger),
        },
        "ledger": {
            "available": ledger_view["available"],
            "schema": ledger_view["schema"],
            "schema_version": ledger_view["schema_version"],
            "entry_count": len(facts),
            "valid_entry_count": len(valid_entries),
            "task_ids": [fact["task_id"] for fact in facts],
            "verdicts": {
                str(entry["task_id"]): entry["verdict"] for entry in valid_entries
            },
            "coverage_floor": ledger_view["coverage_floor"],
            "coverage_floor_source": ledger_view["coverage_floor_source"],
            "duplicate_tasks": list(ledger_view["duplicate_tasks"]),
        },
        "chain": chain,
        "bindings": bindings,
        "authority": authority_section(),
        "determinism": determinism_section(),
        "issues": ordered,
        "summary": {
            "issue_count": len(ordered),
            "error_count": len(ordered),
            "warning_count": 0,
            "integrity_ok": not ordered,
            "entry_count": len(bindings),
            "bound_count": bound_count,
            "exit_code": report_exit_code(ordered),
        },
    }

    report["facts_digest"] = integrity_facts_digest(report)

    return report


def render_integrity_report(report: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(report, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


# ============================================================
# 只读 CLI（python -m orchestrator.review_ledger_integrity）
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_ledger_integrity",
        description=(
            "GPT Review Ledger 完整性 / 连续性只读门禁（GOLD-032）：验证 schema、"
            "唯一 task_id、review 顺序、result SHA-256 与 reviewed commit identity 是否"
            "仍与 GOLD-031 manifest 的客观事实一致；绝不签发 Review 结论、绝不修复台账、"
            "绝不修改任何项目状态。"
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI：报告写 stdout（纯 ASCII JSON），issue 摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    ledger_path = (
        Path(args.ledger)
        if args.ledger is not None
        else root / ledger_mod.REVIEW_LEDGER_RELATIVE_PATH
    )

    report = build_integrity_report(
        root=root,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        ledger_path=ledger_path,
        generated_at=args.generated_at,
    )

    sys.stdout.write(render_integrity_report(report))

    sys.stdout.flush()

    for issue in report["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    if not report["summary"]["integrity_ok"]:
        codes = ", ".join(issue["code"] for issue in report["issues"])

        print(f"[integrity] integrity_ok=False | fail-closed: {codes}", file=sys.stderr)

    return int(report["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())





