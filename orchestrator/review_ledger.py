"""GPT Review Ledger 与状态推进一致性门禁（GOLD-025）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2 规定：任务 COMPLETED 之后必须先由 **GPT** Review
task / result / commit / diff / tests，Review PASS 之后才允许更新
``.ai/PROJECT_STATE.json`` 并创建下一任务。这条规则在历史事故里**无法机器验证**：

- Executor（Cline / DeepSeek）只要写出 ``status=completed`` 的 result，
  就无法区分「只是完成」与「已被 GPT Review 通过」；
- ``last_reviewed_task`` 指针可以凭空超前 / 落后于真实 results，且没有任何 review
  evidence 支撑；
- 被 review 过的 result 如果之后被替换 / 改写，旧 review 会静默「继续有效」。

本模块把 review 变成**机器可审计、版本化、只读验证**的记录：

1. :data:`REVIEW_LEDGER_SCHEMA`（``gold-ai/gpt-review-ledger/v1``）定义 ledger 契约：
   每个条目记录 ``task_id`` / ``verdict`` / ``reviewer_role=GPT`` / 被 review 的 result
   身份（文件 sha256 + status + finished_at）/ 对应 Git identity（commit sha + branch）/
   ``acceptance_summary`` / ``reviewed_at``；
2. :func:`validate_review_ledger` 做 schema + 条目级 fail-closed 校验：缺字段 /
   未知 verdict / 非 GPT 身份 / commit sha 非法 / 重复条目一律产生稳定 issue，
   且**非法或冲突的条目永远不能算作一次通过**；
3. :func:`build_review_report` 做状态推进一致性检查：``last_reviewed_task`` 不得超前于
   completed result、不得落后于 ledger（指针倒退）、review 必须绑定**存在且未被改写**的
   terminal result 与 Git identity、rolling queue 首任务的依赖必须已 ``completed``
   且已 GPT-reviewed；stale / missing / mismatched 全部 fail-closed 报告；
4. :func:`review_write_contract` / :func:`review_authority` 给出「GPT 可写、Executor
   只读」的机器可读边界：只有 GPT 能签发 review，未知身份 fail-closed 拒绝；
5. CLI ``python -m orchestrator.review_ledger`` 输出同一份版本化 JSON（纯 ASCII stdout）。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **只读**：本模块没有任何写入路径（源码守卫测试锁定），绝不写
  ``.ai/GPT_REVIEW_LEDGER.json``、``.ai/PROJECT_STATE.json``、``.ai/tasks/**``、
  ``.ai/results/**``，也绝不 commit / push / reset / checkout；
- 绝不代替 GPT 签发 review（本模块**没有**任何写入 / 追加 / 修复 ledger 的 API），
  绝不自动修复漂移、绝不改 Phase、数据资格 Gate 或 L3/L4 Human Gate；
- 唯一的外部进程调用是 ``--verify-ancestry`` 显式开启时的只读
  ``git merge-base --is-ancestor``（默认关闭，无法验证时 fail-closed）。

用法
----
.. code-block:: text

    python -m orchestrator.review_ledger                    # 只读报告写 stdout（ASCII JSON）
    python -m orchestrator.review_ledger --root .           # 指定仓库根
    python -m orchestrator.review_ledger --verify-ancestry  # 额外只读校验 commit 可达性

退出码：``0`` 一致 / ``2`` 检出漂移或阻塞 / ``3`` ``PROJECT_STATE`` 不可读。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner

ROOT = Path(__file__).resolve().parent.parent

# GPT Planner / Executor 共用的只读事实入口（PROJECT_STATE 路径与快照模块保持一致）。
PROJECT_STATE_PATH = ROOT / ".ai" / "PROJECT_STATE.json"

# GPT 可写、Executor 只读的 review ledger（唯一 canonical 位置，相对仓库根）。
REVIEW_LEDGER_RELATIVE_PATH = ".ai/GPT_REVIEW_LEDGER.json"

DEFAULT_REVIEW_LEDGER_PATH = ROOT / ".ai" / "GPT_REVIEW_LEDGER.json"

# shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
REVIEW_LEDGER_SCHEMA = "gold-ai/gpt-review-ledger/v1"

REVIEW_LEDGER_SCHEMA_VERSION = 1

REVIEW_REPORT_SCHEMA = "gold-ai/review-gate-report/v1"

REVIEW_REPORT_SCHEMA_VERSION = 1

REVIEW_WRITE_CONTRACT_SCHEMA = "gold-ai/review-write-contract/v1"

# reviewer 唯一合法身份（大小写不敏感）。
REVIEWER_ROLE = "GPT"

# 稳定 verdict 词表：只有 PASS 才能推进状态指针 / 队列。
VERDICT_PASS = "PASS"

VERDICT_FAIL = "FAIL"

REVIEW_VERDICTS = (VERDICT_PASS, VERDICT_FAIL)

# ledger 条目必须携带的字段（缺一即 fail-closed，绝不「猜」缺失的审计信息）。
ENTRY_REQUIRED_FIELDS = (
    "task_id",
    "verdict",
    "reviewer",
    "reviewer_role",
    "acceptance_summary",
    "reviewed_result",
    "reviewed_commit",
    "reviewed_at",
)

REVIEWED_RESULT_REQUIRED_FIELDS = ("result_sha256", "status")

REVIEWED_COMMIT_REQUIRED_FIELDS = ("sha", "branch")

# 被 review 的 Git identity 必须是完整 40 位小写 commit sha（缩写一律拒绝）。
FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不参与状态事实；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生，必须排除以避免自引用。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# 确定性排序契约（机器可读，供 GPT 与测试断言；只描述规则，不含事实）。
REPORT_ORDERING = {
    "issues": "sorted by (code, detail)",
    "ledger.entries": "sorted by task_id_sort_key",
    "ledger.pass_reviews": "sorted by task_id_sort_key",
    "ledger.failed_reviews": "sorted by task_id_sort_key",
    "results.completed": "sorted by task_id_sort_key",
    "queue.head_dependencies": "sorted by task_id_sort_key",
}

# ---------- 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言） ----------

ISSUE_PROJECT_STATE_UNREADABLE = "PROJECT_STATE_UNREADABLE"
ISSUE_LEDGER_MISSING = "REVIEW_LEDGER_MISSING"
ISSUE_LEDGER_UNREADABLE = "REVIEW_LEDGER_UNREADABLE"
ISSUE_LEDGER_SCHEMA_UNSUPPORTED = "REVIEW_LEDGER_SCHEMA_UNSUPPORTED"
ISSUE_LEDGER_ENTRIES_INVALID = "REVIEW_LEDGER_ENTRIES_INVALID"
ISSUE_LEDGER_METADATA_INVALID = "REVIEW_LEDGER_METADATA_INVALID"
ISSUE_ENTRY_INVALID = "REVIEW_ENTRY_INVALID"
ISSUE_ENTRY_DUPLICATE = "REVIEW_ENTRY_DUPLICATE"
ISSUE_VERDICT_INVALID = "REVIEW_VERDICT_INVALID"
ISSUE_REVIEWER_ROLE_INVALID = "REVIEWER_ROLE_INVALID"
ISSUE_EXECUTOR_REVIEW_FORBIDDEN = "EXECUTOR_REVIEW_FORBIDDEN"
ISSUE_RESULT_MISSING = "REVIEW_RESULT_MISSING"
ISSUE_RESULT_NOT_TERMINAL = "REVIEW_RESULT_NOT_TERMINAL"
ISSUE_RESULT_IDENTITY_MISMATCH = "REVIEW_RESULT_IDENTITY_MISMATCH"
ISSUE_PASS_ON_BLOCKED_RESULT = "REVIEW_PASS_ON_BLOCKED_RESULT"
ISSUE_COMMIT_INVALID = "REVIEW_COMMIT_INVALID"
ISSUE_BRANCH_MISMATCH = "REVIEW_BRANCH_MISMATCH"
ISSUE_COMMIT_UNREACHABLE = "REVIEW_COMMIT_UNREACHABLE"
ISSUE_ANCESTRY_UNVERIFIABLE = "REVIEW_ANCESTRY_UNVERIFIABLE"
ISSUE_COMPLETED_BUT_UNREVIEWED = "COMPLETED_BUT_UNREVIEWED"
ISSUE_POINTER_MISSING = "REVIEW_POINTER_MISSING"
ISSUE_POINTER_UNKNOWN_TASK = "REVIEW_POINTER_UNKNOWN_TASK"
ISSUE_POINTER_AHEAD_OF_COMPLETION = "REVIEW_POINTER_AHEAD_OF_COMPLETION"
ISSUE_POINTER_BEHIND_LEDGER = "REVIEW_POINTER_BEHIND_LEDGER"
ISSUE_POINTER_ON_FAILED_REVIEW = "REVIEW_POINTER_ON_FAILED_REVIEW"
ISSUE_QUEUE_HEAD_TASK_INVALID = "QUEUE_HEAD_TASK_INVALID"
ISSUE_QUEUE_HEAD_DEPENDENCY_MISSING = "QUEUE_HEAD_DEPENDENCY_MISSING"
ISSUE_QUEUE_HEAD_DEPENDENCY_NOT_COMPLETED = "QUEUE_HEAD_DEPENDENCY_NOT_COMPLETED"
ISSUE_QUEUE_HEAD_DEPENDENCY_BLOCKED = "QUEUE_HEAD_DEPENDENCY_BLOCKED"
ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED = "QUEUE_HEAD_DEPENDENCY_UNREVIEWED"
ISSUE_QUEUE_HEAD_HUMAN_GATE = "QUEUE_HEAD_HUMAN_GATE"

SEVERITY_ERROR = "error"

SEVERITY_WARNING = "warning"

EXIT_OK = 0

EXIT_DRIFT = 2

EXIT_STATE_UNREADABLE = 3

# 只读的 commit 可达性探测：默认**不**运行任何子进程。
ANCESTRY_TIMEOUT_SECONDS = 30


# ============================================================
# 职责边界：只有 GPT 可以签发 review（fail-closed）
# ============================================================


def make_issue(code: str, severity: str, detail: str) -> dict[str, str]:
    """构造一条诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": severity, "detail": detail}


def review_authority(agent: object) -> tuple[bool, str]:
    """谁能签发 review：只有 GPT（返回 ``(allowed, reason)``，未知身份一律拒绝）。"""

    name = str(agent).strip().lower()

    if name in planner.PLANNER_AGENTS:
        return True, f"planner reviewer: {name}"

    if name in planner.EXECUTOR_AGENTS:
        return False, f"executor cannot sign review: {name}"

    return False, f"unknown reviewer agent: {name!r} (fail-closed)"


def can_sign_review(agent: object) -> bool:
    """该 agent 是否可以签发 review（未知身份一律 ``False``）。"""

    return review_authority(agent)[0]


def review_write_contract() -> dict[str, Any]:
    """GPT 可写 / Executor 只读的职责边界（机器可读契约，只声明规则）。"""

    return {
        "schema": REVIEW_WRITE_CONTRACT_SCHEMA,
        "read_only_for_executor": True,
        "ledger_relative_path": REVIEW_LEDGER_RELATIVE_PATH,
        "ledger_schema": REVIEW_LEDGER_SCHEMA,
        "ledger_schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "writer_agents": list(planner.PLANNER_AGENTS),
        "reviewer_role": REVIEWER_ROLE,
        "reader_agents": list(planner.EXECUTOR_AGENTS),
        "verdict_vocabulary": list(REVIEW_VERDICTS),
        "required_entry_fields": list(ENTRY_REQUIRED_FIELDS),
        "required_reviewed_result_fields": list(REVIEWED_RESULT_REQUIRED_FIELDS),
        "required_reviewed_commit_fields": list(REVIEWED_COMMIT_REQUIRED_FIELDS),
        "executor_can_write_ledger": False,
        "executor_can_sign_review": False,
        "executor_can_declare_verdict": False,
        "module_can_write_ledger": False,
        "ledger_can_transition_phase": False,
        "ledger_can_cross_human_gate": False,
        "ledger_can_qualify_data": False,
        "blocking_human_gates": sorted(orch.BLOCKING_HUMAN_GATES),
        "pass_verdict_grants": "state_pointer_advance_only",
    }


# ============================================================
# ledger 读取与条目校验（只读，绝无写入路径）
# ============================================================


def text_value(value: object) -> str | None:
    """非空字符串（strip 后）→ 字符串，否则 ``None``。"""

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def iso_timestamp(value: object) -> str | None:
    """合法性校验：可被 ``datetime.fromisoformat`` 解析的时间戳，否则 ``None``。"""

    text = text_value(value)

    if text is None:
        return None

    try:
        datetime.fromisoformat(text)

    except ValueError:
        return None

    return text


def load_review_ledger(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """只读加载 review ledger；返回 ``(payload, error)``。

    缺失 / 损坏一律返回 error（fail-closed），且**绝不创建或修复** ledger 文件。
    """

    if not path.exists():
        return None, f"missing: {path}"

    return planner.read_json_mapping(path)


def review_entry_view(
    entry: object,
    index: int,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """校验单个 ledger 条目；返回 ``(view, issues)``。

    ``view["valid"]`` 为 ``False`` 的条目**永远不能算作一次通过**
    （fail-closed：非法 review 不等于没有 review，更不等于 PASS）。
    """

    issues: list[dict[str, str]] = []

    if not isinstance(entry, dict):
        return None, [
            make_issue(ISSUE_ENTRY_INVALID, SEVERITY_ERROR, f"entries[{index}] 不是 JSON 对象")
        ]

    label = f"entries[{index}]"

    task_id = text_value(entry.get("task_id"))

    if task_id is None:
        issues.append(
            make_issue(ISSUE_ENTRY_INVALID, SEVERITY_ERROR, f"{label}: task_id 缺失或非字符串")
        )

    else:
        label = task_id

    raw_verdict = entry.get("verdict")

    verdict = text_value(raw_verdict)

    if verdict is None or verdict.upper() not in REVIEW_VERDICTS:
        issues.append(
            make_issue(
                ISSUE_VERDICT_INVALID,
                SEVERITY_ERROR,
                f"{label}: verdict 非法（只接受 {'/'.join(REVIEW_VERDICTS)}）: {raw_verdict!r}",
            )
        )

        verdict_value: str | None = None

    else:
        verdict_value = verdict.upper()

    reviewer_role = text_value(entry.get("reviewer_role"))

    if reviewer_role is None or reviewer_role.upper() != REVIEWER_ROLE:
        issues.append(
            make_issue(
                ISSUE_REVIEWER_ROLE_INVALID,
                SEVERITY_ERROR,
                f"{label}: reviewer_role 必须是 {REVIEWER_ROLE}: {entry.get('reviewer_role')!r}",
            )
        )

    reviewer = text_value(entry.get("reviewer"))

    if reviewer is None:
        issues.append(
            make_issue(ISSUE_REVIEWER_ROLE_INVALID, SEVERITY_ERROR, f"{label}: reviewer 缺失")
        )

    elif not can_sign_review(reviewer):
        _, reason = review_authority(reviewer)

        issues.append(
            make_issue(ISSUE_EXECUTOR_REVIEW_FORBIDDEN, SEVERITY_ERROR, f"{label}: {reason}")
        )

    acceptance_summary = text_value(entry.get("acceptance_summary"))

    if acceptance_summary is None:
        issues.append(
            make_issue(
                ISSUE_ENTRY_INVALID,
                SEVERITY_ERROR,
                f"{label}: acceptance_summary 缺失或为空（review 必须给出验收结论摘要）",
            )
        )

    reviewed_result = entry.get("reviewed_result")

    result_sha256: str | None = None
    result_status: str | None = None
    result_finished_at: str | None = None

    if not isinstance(reviewed_result, dict):
        issues.append(
            make_issue(
                ISSUE_ENTRY_INVALID,
                SEVERITY_ERROR,
                f"{label}: reviewed_result 必须是对象（字段: "
                f"{', '.join(REVIEWED_RESULT_REQUIRED_FIELDS)}）",
            )
        )

    else:
        result_sha256 = text_value(reviewed_result.get("result_sha256"))
        result_status = text_value(reviewed_result.get("status"))
        result_finished_at = text_value(reviewed_result.get("finished_at"))

        if result_sha256 is None or SHA256_PATTERN.match(result_sha256) is None:
            issues.append(
                make_issue(
                    ISSUE_ENTRY_INVALID,
                    SEVERITY_ERROR,
                    f"{label}: reviewed_result.result_sha256 必须是 result 文件的 sha256",
                )
            )

            result_sha256 = None

        if result_status is None:
            issues.append(
                make_issue(
                    ISSUE_ENTRY_INVALID,
                    SEVERITY_ERROR,
                    f"{label}: reviewed_result.status 缺失",
                )
            )

    reviewed_commit = entry.get("reviewed_commit")

    commit_sha: str | None = None
    commit_branch: str | None = None

    if not isinstance(reviewed_commit, dict):
        issues.append(
            make_issue(
                ISSUE_ENTRY_INVALID,
                SEVERITY_ERROR,
                f"{label}: reviewed_commit 必须是对象（字段: "
                f"{', '.join(REVIEWED_COMMIT_REQUIRED_FIELDS)}）",
            )
        )

    else:
        commit_sha = text_value(reviewed_commit.get("sha"))
        commit_branch = text_value(reviewed_commit.get("branch"))

        if commit_sha is None or FULL_COMMIT_SHA_PATTERN.match(commit_sha) is None:
            issues.append(
                make_issue(
                    ISSUE_COMMIT_INVALID,
                    SEVERITY_ERROR,
                    f"{label}: reviewed_commit.sha 必须是完整 40 位小写 commit sha："
                    f"{reviewed_commit.get('sha')!r}",
                )
            )

            commit_sha = None

        if commit_branch is None:
            issues.append(
                make_issue(
                    ISSUE_ENTRY_INVALID,
                    SEVERITY_ERROR,
                    f"{label}: reviewed_commit.branch 缺失",
                )
            )

    reviewed_at = iso_timestamp(entry.get("reviewed_at"))

    if reviewed_at is None:
        issues.append(
            make_issue(
                ISSUE_ENTRY_INVALID,
                SEVERITY_ERROR,
                f"{label}: reviewed_at 缺失或不是 ISO-8601 时间戳: {entry.get('reviewed_at')!r}",
            )
        )

    view: dict[str, Any] = {
        "task_id": task_id,
        "verdict": verdict_value,
        "reviewer": reviewer,
        "reviewer_role": reviewer_role,
        "acceptance_summary": acceptance_summary,
        "reviewed_result": {
            "result_sha256": result_sha256,
            "status": result_status,
            "finished_at": result_finished_at,
        },
        "reviewed_commit": {"sha": commit_sha, "branch": commit_branch},
        "reviewed_at": reviewed_at,
        "valid": not issues,
        "issue_codes": sorted({issue["code"] for issue in issues}),
    }

    if task_id is None:
        return None, issues

    return view, issues


def validate_review_ledger(
    payload: dict[str, Any] | None,
    error: str | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """schema + 条目级 fail-closed 校验；返回 ``(ledger_view, issues)``。

    ``ledger_view["available"]`` 为 ``False`` 时调用方**不得**把任何 task 视为已 review，
    ``pass_reviews`` / ``failed_reviews`` 只收录**完整合法且无冲突**的条目。
    """

    issues: list[dict[str, str]] = []

    view: dict[str, Any] = {
        "available": False,
        "schema": None,
        "schema_version": None,
        "error": error,
        "entries": [],
        "pass_reviews": [],
        "failed_reviews": [],
        "reviews": {},
        "duplicate_tasks": [],
        "coverage_floor": None,
        "coverage_floor_source": None,
    }

    if payload is None:
        code = (
            ISSUE_LEDGER_MISSING
            if (error or "").startswith("missing:")
            else ISSUE_LEDGER_UNREADABLE
        )

        issues.append(
            make_issue(code, SEVERITY_ERROR, f"review ledger 不可用（fail-closed）: {error}")
        )

        return view, issues

    schema = text_value(payload.get("schema"))

    version = payload.get("schema_version")

    view["schema"] = schema
    view["schema_version"] = version

    if schema != REVIEW_LEDGER_SCHEMA or version != REVIEW_LEDGER_SCHEMA_VERSION:
        issues.append(
            make_issue(
                ISSUE_LEDGER_SCHEMA_UNSUPPORTED,
                SEVERITY_ERROR,
                f"ledger schema 不支持: schema={schema!r} schema_version={version!r}"
                f"（要求 {REVIEW_LEDGER_SCHEMA} / {REVIEW_LEDGER_SCHEMA_VERSION}）",
            )
        )

        return view, issues

    entries = payload.get("entries")

    if not isinstance(entries, list):
        issues.append(
            make_issue(ISSUE_LEDGER_ENTRIES_INVALID, SEVERITY_ERROR, "ledger.entries 必须是数组")
        )

        return view, issues

    view["available"] = True

    entry_views: list[dict[str, Any]] = []

    for index, entry in enumerate(entries):
        entry_view, entry_issues = review_entry_view(entry, index)

        issues.extend(entry_issues)

        if entry_view is not None:
            entry_views.append(entry_view)

    counts: dict[str, int] = {}

    for entry_view in entry_views:
        task_id = str(entry_view["task_id"])
        counts[task_id] = counts.get(task_id, 0) + 1

    duplicates = sorted(
        (task_id for task_id, count in counts.items() if count > 1),
        key=planner.task_id_sort_key,
    )

    for task_id in duplicates:
        issues.append(
            make_issue(
                ISSUE_ENTRY_DUPLICATE,
                SEVERITY_ERROR,
                f"{task_id}: ledger 存在多条 review 记录（verdict 冲突无法裁决，fail-closed）",
            )
        )

    # 冲突条目一律失效：绝不挑一个「更宽松」的 verdict 继续。
    for entry_view in entry_views:
        if str(entry_view["task_id"]) in duplicates:
            entry_view["valid"] = False
            entry_view["issue_codes"] = sorted(
                {*entry_view["issue_codes"], ISSUE_ENTRY_DUPLICATE}
            )

    entry_views.sort(key=lambda item: planner.task_id_sort_key(str(item["task_id"])))

    pass_reviews: list[str] = []
    failed_reviews: list[str] = []
    reviews: dict[str, str] = {}

    for entry_view in entry_views:
        if not entry_view["valid"]:
            continue

        task_id = str(entry_view["task_id"])

        reviews[task_id] = str(entry_view["verdict"])

        if entry_view["verdict"] == VERDICT_PASS:
            pass_reviews.append(task_id)

        elif entry_view["verdict"] == VERDICT_FAIL:
            failed_reviews.append(task_id)

    pass_reviews.sort(key=planner.task_id_sort_key)
    failed_reviews.sort(key=planner.task_id_sort_key)

    floor: str | None = None
    floor_source: str | None = None

    if payload.get("reviewed_from") is not None:
        reviewed_from = text_value(payload.get("reviewed_from"))

        if reviewed_from is None:
            issues.append(
                make_issue(
                    ISSUE_LEDGER_METADATA_INVALID,
                    SEVERITY_ERROR,
                    "ledger.reviewed_from 必须是非空 task_id 字符串（覆盖下限）",
                )
            )

        else:
            floor = reviewed_from
            floor_source = "reviewed_from"

    if floor is None and pass_reviews:
        floor = pass_reviews[-1]
        floor_source = "newest_pass_review"

    view.update(
        {
            "entries": entry_views,
            "pass_reviews": pass_reviews,
            "failed_reviews": failed_reviews,
            "reviews": reviews,
            "duplicate_tasks": duplicates,
            "coverage_floor": floor,
            "coverage_floor_source": floor_source,
        }
    )

    return view, issues


def completed_result_ids(
    statuses: dict[str, str],
    project_prefix: str | None = None,
) -> list[str]:
    """全部 ``completed`` result（``blocked`` 不算完成），按确定性任务序（项目前缀最新）。"""

    return sorted(
        (task_id for task_id, status in statuses.items() if status == "completed"),
        key=lambda task_id: planner.task_rank(task_id, project_prefix),
    )


def result_identity(result_path: Path) -> dict[str, Any]:
    """result 文件的只读身份（sha256 + status + finished_at + execution_outcome）。

    用文件**字节**做哈希：被 review 过的 result 一旦被替换 / 改写，
    旧 review 立即失效（机器可判定，无需人工比对）。
    """

    payload, error = planner.read_json_mapping(result_path)

    if error is not None or payload is None:
        return {}

    try:
        raw = result_path.read_bytes()

    except OSError:
        return {}

    return {
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "status": str(payload.get("status", "")).strip().lower(),
        "execution_outcome": payload.get("execution_outcome"),
        "finished_at": payload.get("finished_at"),
    }


def entry_result_issues(
    entry_view: dict[str, Any],
    statuses: dict[str, str],
    results_dir: Path,
) -> list[dict[str, str]]:
    """review 条目 vs 真实 result 的绑定校验（缺失 / 非终态 / 被改写 / PASS 落在 blocked）。"""

    issues: list[dict[str, str]] = []

    task_id = str(entry_view["task_id"])

    result_file = results_dir / f"{task_id}.json"

    if not result_file.exists():
        return [
            make_issue(
                ISSUE_RESULT_MISSING,
                SEVERITY_ERROR,
                f"{task_id}: review 绑定的 terminal result 不存在（{result_file}）",
            )
        ]

    status = statuses.get(task_id, "unknown")

    if status not in orch.TERMINAL_RESULT_STATUSES:
        issues.append(
            make_issue(
                ISSUE_RESULT_NOT_TERMINAL,
                SEVERITY_ERROR,
                f"{task_id}: review 绑定的 result 不是终态（status={status}）",
            )
        )

    if status == "blocked" and entry_view["verdict"] == VERDICT_PASS:
        issues.append(
            make_issue(
                ISSUE_PASS_ON_BLOCKED_RESULT,
                SEVERITY_ERROR,
                f"{task_id}: blocked 的 result 不得被判定为 PASS",
            )
        )

    current = result_identity(result_file)

    if not current:
        issues.append(
            make_issue(
                ISSUE_RESULT_NOT_TERMINAL,
                SEVERITY_ERROR,
                f"{task_id}: result 不可读，无法校验 review 身份",
            )
        )

        return issues

    declared = entry_view["reviewed_result"]

    mismatched: list[str] = []

    if declared.get("result_sha256") != current["result_sha256"]:
        mismatched.append("result_sha256")

    declared_status = text_value(declared.get("status"))

    if declared_status is None or declared_status.lower() != current["status"]:
        mismatched.append("status")

    declared_finished_at = text_value(declared.get("finished_at"))

    if (
        declared_finished_at is not None
        and current["finished_at"] is not None
        and declared_finished_at != current["finished_at"]
    ):
        mismatched.append("finished_at")

    if mismatched:
        issues.append(
            make_issue(
                ISSUE_RESULT_IDENTITY_MISMATCH,
                SEVERITY_ERROR,
                f"{task_id}: review 绑定的 result 身份与当前文件不一致（{', '.join(mismatched)}）："
                "result 已被替换 / 改写，旧 review 失效",
            )
        )

    return issues


def entry_commit_issues(
    entry_view: dict[str, Any],
    state_branch: str | None,
    git_branch: str | None,
) -> list[dict[str, str]]:
    """review 条目 Git identity（branch）与项目现行事实的一致性。"""

    issues: list[dict[str, str]] = []

    task_id = str(entry_view["task_id"])

    branch = entry_view["reviewed_commit"].get("branch")

    for source, current in (("PROJECT_STATE.branch", state_branch), ("Git HEAD", git_branch)):
        if not current or branch == current:
            continue

        issues.append(
            make_issue(
                ISSUE_BRANCH_MISMATCH,
                SEVERITY_ERROR,
                f"{task_id}: reviewed_commit.branch={branch} 与 {source}={current} 不一致",
            )
        )

    return issues


def pointer_section(
    state: dict[str, Any],
    ledger_view: dict[str, Any],
    statuses: dict[str, str],
    tasks_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """``last_reviewed_task`` 指针 vs ledger vs 真实 results（只报告，绝不改写）。"""

    issues: list[dict[str, str]] = []

    project_prefix = planner.project_task_prefix(state)

    completed_ids = completed_result_ids(statuses, project_prefix)

    pointer = planner.pointer_value(state, "last_reviewed_task")

    pass_reviews: list[str] = ledger_view["pass_reviews"]

    reviews: dict[str, str] = ledger_view["reviews"]

    newest_completed = completed_ids[-1] if completed_ids else None

    newest_pass = pass_reviews[-1] if pass_reviews else None

    section: dict[str, Any] = {
        "last_reviewed_task": pointer,
        "last_completed_task": planner.pointer_value(state, "last_completed_task"),
        "current_task": planner.pointer_value(state, "current_task"),
        "newest_completed_result": newest_completed,
        "newest_pass_review": newest_pass,
        "entry_present": bool(pointer) and pointer in reviews,
        "entry_verdict": reviews.get(pointer) if pointer else None,
    }

    if pointer is None:
        if newest_pass is not None:
            issues.append(
                make_issue(
                    ISSUE_POINTER_MISSING,
                    SEVERITY_ERROR,
                    f"ledger 已有 {len(pass_reviews)} 条 GPT PASS review（最新 {newest_pass}），"
                    "但 PROJECT_STATE.last_reviewed_task 为空：review 指针缺失 / 倒退",
                )
            )

        return section, issues

    known_task = (tasks_dir / f"{pointer}.json").exists() or pointer in statuses

    if not known_task:
        issues.append(
            make_issue(
                ISSUE_POINTER_UNKNOWN_TASK,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 在 tasks/ 与 results/ 中都不存在",
            )
        )

        return section, issues

    status = statuses.get(pointer)

    verdict = reviews.get(pointer)

    if status != "completed":
        issues.append(
            make_issue(
                ISSUE_POINTER_AHEAD_OF_COMPLETION,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 没有 completed result"
                f"（status={status if status is not None else 'pending'}）："
                "review 指针不得超前于 completed result",
            )
        )

    elif verdict == VERDICT_FAIL:
        issues.append(
            make_issue(
                ISSUE_POINTER_ON_FAILED_REVIEW,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 的 GPT review 结论是 FAIL："
                "FAIL 不得推进状态指针 / 队列（必须先创建修复任务）",
            )
        )

    elif verdict != VERDICT_PASS:
        issues.append(
            make_issue(
                ISSUE_COMPLETED_BUT_UNREVIEWED,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 已 completed，但 ledger 没有有效的 GPT PASS 记录："
                "Executor 不得凭 completed 自称 reviewed",
            )
        )

    if newest_completed is not None and planner.is_newer(pointer, newest_completed):
        issues.append(
            make_issue(
                ISSUE_POINTER_AHEAD_OF_COMPLETION,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 超前于最新 completed result={newest_completed}",
            )
        )

    if newest_pass is not None and planner.is_newer(newest_pass, pointer):
        issues.append(
            make_issue(
                ISSUE_POINTER_BEHIND_LEDGER,
                SEVERITY_ERROR,
                f"last_reviewed_task={pointer} 落后于 ledger 最新 PASS review={newest_pass}："
                "PROJECT_STATE 指针倒退（只报告，绝不自动改写）",
            )
        )

    return section, issues


def unreviewed_completed_issues(
    statuses: dict[str, str],
    ledger_view: dict[str, Any],
    project_prefix: str | None = None,
) -> list[dict[str, str]]:
    """completed 但无 GPT PASS 记录的任务（从 ledger 覆盖下限起算）。

    覆盖下限 = ``reviewed_from``（若声明）→ 否则最新 PASS review → 否则全部历史。
    这样既可以 fail-closed 暴露新缺口，又不会把 ledger 建档之前的历史任务全部误判成漂移。
    """

    issues: list[dict[str, str]] = []

    floor = ledger_view.get("coverage_floor")

    reviewed = set(ledger_view["pass_reviews"])

    for task_id in completed_result_ids(statuses, project_prefix):
        if task_id in reviewed:
            continue

        if floor is not None and not planner.is_newer(task_id, floor) and task_id != floor:
            continue

        issues.append(
            make_issue(
                ISSUE_COMPLETED_BUT_UNREVIEWED,
                SEVERITY_ERROR,
                f"{task_id}: result completed 但 ledger 无 GPT PASS 记录"
                f"（覆盖下限={floor if floor is not None else 'none'}）："
                "协议要求 COMPLETED 后先由 GPT Review PASS 才推进",
            )
        )

    return issues


def dependency_closure(
    task_id: str,
    tasks_dir: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    """任务的**传递**依赖闭包（只读；缺失 / 损坏 / 环一律 fail-closed 报告，不跳过）。"""

    issues: list[dict[str, str]] = []

    collected: set[str] = set()

    expanded: set[str] = set()

    queue: list[str] = [task_id]

    while queue:
        current = queue.pop(0)

        if current in expanded:
            continue

        expanded.add(current)

        task_file = tasks_dir / f"{current}.json"

        if not task_file.exists():
            continue

        try:
            dependencies = orch.get_task_dependencies(orch.load_task(task_file))

        except (OSError, ValueError, orch.TaskMetadataError) as exc:
            issues.append(
                make_issue(
                    ISSUE_QUEUE_HEAD_TASK_INVALID,
                    SEVERITY_ERROR,
                    f"{current}: 依赖元数据不可读: {exc}",
                )
            )

            continue

        for dependency in dependencies:
            collected.add(dependency)

            queue.append(dependency)

    return sorted(collected, key=planner.task_id_sort_key), issues


def queue_section(
    state: dict[str, Any],
    tasks_dir: Path,
    results_dir: Path,
    statuses: dict[str, str],
    ledger_view: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """rolling queue 首任务的门禁事实：依赖 completed + 已 GPT-reviewed + L3/L4 停线。"""

    issues: list[dict[str, str]] = []

    schedule = orch.queue_snapshot()

    pending: list[str] = list(schedule["pending"])

    runnable, stop_reason = orch.find_next_task_with_reason()

    head = pending[0] if pending else None

    section: dict[str, Any] = {
        "queue_status": state.get("queue_status"),
        "pending": pending,
        "count": schedule["count"],
        "target": schedule["target"],
        "head": head,
        "head_human_gate": None,
        "head_auto_start": None,
        "head_requires_human_approval": None,
        "head_readiness": None,
        "head_readiness_reason": stop_reason,
        "runnable_task": runnable.stem if runnable is not None else None,
        "head_dependencies": {},
        "head_dependencies_satisfied": False,
        "head_dependencies_reviewed": False,
        "crosses_human_gate": False,
    }

    if head is None:
        return section, issues

    try:
        task = orch.load_task(tasks_dir / f"{head}.json")

    except (OSError, ValueError) as exc:
        issues.append(
            make_issue(
                ISSUE_QUEUE_HEAD_TASK_INVALID,
                SEVERITY_ERROR,
                f"{head}: task 文件不可读: {exc}",
            )
        )

        return section, issues

    for key, default in (("auto_start", True), ("requires_human_approval", False)):
        try:
            section[f"head_{key}"] = orch.get_task_bool_flag(task, key, default)

        except orch.TaskMetadataError as exc:
            issues.append(
                make_issue(ISSUE_QUEUE_HEAD_TASK_INVALID, SEVERITY_ERROR, f"{head}: {exc}")
            )

    gate: str | None = None

    try:
        gate = orch.get_task_human_gate(task)

    except orch.TaskMetadataError as exc:
        issues.append(
            make_issue(ISSUE_QUEUE_HEAD_TASK_INVALID, SEVERITY_ERROR, f"{head}: {exc}")
        )

    section["head_human_gate"] = gate

    ready, readiness_reason = orch.evaluate_task_readiness(task)

    section["head_readiness"] = ready
    section["head_readiness_reason"] = readiness_reason

    dependencies, dependency_issues = dependency_closure(head, tasks_dir)

    issues.extend(dependency_issues)

    dependency_status: dict[str, str] = {}

    all_completed = True
    all_reviewed = True

    for dependency in dependencies:
        result_file = results_dir / f"{dependency}.json"
        task_file = tasks_dir / f"{dependency}.json"

        if result_file.exists():
            status = statuses.get(dependency, "unknown")

        elif task_file.exists():
            status = "pending"

        else:
            status = "missing"

        dependency_status[dependency] = status

        if status == "missing":
            all_completed = False
            all_reviewed = False

            issues.append(
                make_issue(
                    ISSUE_QUEUE_HEAD_DEPENDENCY_MISSING,
                    SEVERITY_ERROR,
                    f"{head} 的依赖 {dependency} 不存在（task 与 result 都缺失）",
                )
            )

        elif status == "blocked":
            all_completed = False
            all_reviewed = False

            issues.append(
                make_issue(
                    ISSUE_QUEUE_HEAD_DEPENDENCY_BLOCKED,
                    SEVERITY_ERROR,
                    f"{head} 的依赖 {dependency} 是 blocked：队列必须停线，绝不跨越失败",
                )
            )

        elif status != "completed":
            all_completed = False
            all_reviewed = False

            issues.append(
                make_issue(
                    ISSUE_QUEUE_HEAD_DEPENDENCY_NOT_COMPLETED,
                    SEVERITY_ERROR,
                    f"{head} 的依赖 {dependency} 尚未 completed（status={status}）",
                )
            )

        elif dependency not in ledger_view["pass_reviews"]:
            all_reviewed = False

            issues.append(
                make_issue(
                    ISSUE_QUEUE_HEAD_DEPENDENCY_UNREVIEWED,
                    SEVERITY_ERROR,
                    f"{head} 的依赖 {dependency} 已 completed，但没有 GPT PASS review："
                    "协议要求依赖先被 GPT Review PASS 才能推进队列",
                )
            )

    section["head_dependencies"] = dependency_status
    section["head_dependencies_satisfied"] = all_completed
    section["head_dependencies_reviewed"] = all_completed and all_reviewed

    if gate in orch.BLOCKING_HUMAN_GATES:
        section["crosses_human_gate"] = True

        issues.append(
            make_issue(
                ISSUE_QUEUE_HEAD_HUMAN_GATE,
                SEVERITY_WARNING,
                f"queue 首任务 {head} 等待 human_gate={gate}：L3/L4 永不自动跨越，"
                "必须由 GPT / 人工显式决策（本工具只报告，绝不跨越）",
            )
        )

    return section, issues


def gate_decision(
    issues: list[dict[str, str]],
    head_human_gate: str | None,
) -> dict[str, Any]:
    """状态推进门禁结论（fail-closed）：任何 error 或 L3/L4 gate 都不允许推进。"""

    blocking = sorted({issue["code"] for issue in issues if issue["severity"] == SEVERITY_ERROR})

    crosses_human_gate = head_human_gate in orch.BLOCKING_HUMAN_GATES

    if crosses_human_gate:
        blocking = sorted({*blocking, ISSUE_QUEUE_HEAD_HUMAN_GATE})

    if blocking:
        reason = "fail-closed 停线: " + ", ".join(blocking)

    elif head_human_gate is None:
        reason = (
            "review 一致性通过：completed / reviewed / PROJECT_STATE 指针 / "
            "queue 首任务依赖全部对齐"
        )

    else:
        reason = f"review 一致性通过（queue 首任务 human_gate={head_human_gate}，非 L3/L4）"

    return {
        "advance_allowed": not blocking,
        "blocking_codes": blocking,
        "reason": reason,
        "crosses_human_gate": crosses_human_gate,
        "executor_may_advance_on_its_own": False,
        "tool_can_sign_review": False,
        "tool_can_repair_drift": False,
    }


def git_ancestry_probe(root: Path, sha: str) -> tuple[bool | None, str]:
    """**只读**探测 commit 是否为 ``HEAD`` 的祖先（``git merge-base --is-ancestor``）。

    返回 ``(verified, reason)``：``True`` 可达 / ``False`` 不可达 / ``None`` 无法验证。
    本函数**默认不调用**（仅 ``--verify-ancestry`` 显式开启），且绝不执行任何 Git 写操作。
    """

    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=ANCESTRY_TIMEOUT_SECONDS,
            check=False,
        )

    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git unavailable: {exc}"

    if completed.returncode == 0:
        return True, "ancestor of HEAD"

    if completed.returncode == 1:
        return False, "not an ancestor of HEAD"

    return None, f"git exit {completed.returncode}: {completed.stderr.strip()}"


def ancestry_section(
    ledger_view: dict[str, Any],
    root: Path,
    probe: Callable[[Path, str], tuple[bool | None, str]] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """可选 Git identity 可达性校验（无法验证一律 fail-closed 报告）。"""

    section: dict[str, Any] = {
        "enabled": probe is not None,
        "command": "git merge-base --is-ancestor <reviewed_sha> HEAD",
        "verified": [],
        "unreachable": [],
        "unverifiable": [],
    }

    issues: list[dict[str, str]] = []

    if probe is None:
        return section, issues

    for entry_view in ledger_view["entries"]:
        if not entry_view["valid"]:
            continue

        task_id = str(entry_view["task_id"])

        sha = str(entry_view["reviewed_commit"]["sha"])

        verified, reason = probe(root, sha)

        if verified is True:
            section["verified"].append(task_id)

        elif verified is False:
            section["unreachable"].append(task_id)

            issues.append(
                make_issue(
                    ISSUE_COMMIT_UNREACHABLE,
                    SEVERITY_ERROR,
                    f"{task_id}: reviewed_commit.sha={sha} 不是 HEAD 的祖先（{reason}）："
                    "Git identity 对不上，review 不可信",
                )
            )

        else:
            section["unverifiable"].append(task_id)

            issues.append(
                make_issue(
                    ISSUE_ANCESTRY_UNVERIFIABLE,
                    SEVERITY_ERROR,
                    f"{task_id}: 无法验证 reviewed_commit.sha={sha} 可达性（{reason}）",
                )
            )

    section["verified"].sort(key=planner.task_id_sort_key)
    section["unreachable"].sort(key=planner.task_id_sort_key)
    section["unverifiable"].sort(key=planner.task_id_sort_key)

    return section, issues


def review_report_facts(report: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in report.items() if key not in FACTS_EXCLUDED_KEYS}


def review_report_facts_digest(report: dict[str, Any]) -> str:
    """确定性事实的 sha256：相同输入 ⇒ 相同 digest（幂等、无自引用）。"""

    canonical = json.dumps(
        review_report_facts(report),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "ordering": dict(REPORT_ORDERING),
    }


def report_exit_code(issues: list[dict[str, str]]) -> int:
    """退出码：``0`` 一致 / ``2`` 有漂移或阻塞 / ``3`` PROJECT_STATE 不可读。"""

    codes = {issue["code"] for issue in issues}

    if ISSUE_PROJECT_STATE_UNREADABLE in codes:
        return EXIT_STATE_UNREADABLE

    if issues:
        return EXIT_DRIFT

    return EXIT_OK


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """``PROJECT_STATE`` 的只读摘要（三个指针 + Gate / blocker / 不变量）。"""

    return {
        "schema_version": state.get("schema_version"),
        "project": state.get("project"),
        "branch": state.get("branch"),
        "phase": state.get("phase"),
        "status": state.get("status"),
        "current_task": planner.pointer_value(state, "current_task"),
        "last_completed_task": planner.pointer_value(state, "last_completed_task"),
        "last_reviewed_task": planner.pointer_value(state, "last_reviewed_task"),
        "queue_status": state.get("queue_status"),
        "blocker_codes": planner.gate_codes(state.get("blockers")),
        "human_gate_codes": planner.gate_codes(state.get("human_gates")),
        "invariants": planner.invariant_list(state),
    }


def build_review_report(
    *,
    root: Path | None = None,
    state_path: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    ledger_path: Path | None = None,
    generated_at: str | None = None,
    verify_ancestry: bool = False,
    ancestry_probe: Callable[[Path, str], tuple[bool | None, str]] | None = None,
) -> dict[str, Any]:
    """构建只读 review 门禁报告（版本化 + 一致性诊断）。**绝不修改任何项目状态**。"""

    resolved_root = Path(root) if root is not None else ROOT

    resolved_state = Path(state_path) if state_path is not None else PROJECT_STATE_PATH

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else orch.TASK_DIR

    resolved_results = Path(results_dir) if results_dir is not None else orch.RESULT_DIR

    resolved_ledger = (
        Path(ledger_path)
        if ledger_path is not None
        else resolved_root / REVIEW_LEDGER_RELATIVE_PATH
    )

    issues: list[dict[str, str]] = []

    git_view, git_issues = planner.git_section(resolved_root)

    with planner.orchestrator_view(resolved_tasks, resolved_results):
        state, state_error = planner.load_project_state(resolved_state)

        if state_error is not None:
            issues.append(
                make_issue(
                    ISSUE_PROJECT_STATE_UNREADABLE,
                    SEVERITY_ERROR,
                    f"{resolved_state}: {state_error}",
                )
            )

        state = state or {}

        statuses = planner.result_statuses(resolved_results)

        project_prefix = planner.project_task_prefix(state)

        ledger_payload, ledger_error = load_review_ledger(resolved_ledger)

        ledger_view, ledger_issues = validate_review_ledger(ledger_payload, ledger_error)

        issues.extend(ledger_issues)

        state_branch = text_value(state.get("branch"))

        for entry_view in ledger_view["entries"]:
            if not entry_view["valid"]:
                continue

            issues.extend(entry_result_issues(entry_view, statuses, resolved_results))

            issues.extend(entry_commit_issues(entry_view, state_branch, git_view.get("branch")))

        pointer, pointer_issues = pointer_section(
            state,
            ledger_view,
            statuses,
            resolved_tasks,
        )

        issues.extend(pointer_issues)

        issues.extend(unreviewed_completed_issues(statuses, ledger_view, project_prefix))

        queue, queue_issues = queue_section(
            state,
            resolved_tasks,
            resolved_results,
            statuses,
            ledger_view,
        )

        issues.extend(queue_issues)

    probe = ancestry_probe if ancestry_probe is not None else None

    if probe is None and verify_ancestry:
        probe = git_ancestry_probe

    ancestry, ancestry_issues = ancestry_section(ledger_view, resolved_root, probe)

    issues.extend(git_issues)
    issues.extend(ancestry_issues)

    deduped = list(
        {(issue["code"], issue["severity"], issue["detail"]): issue for issue in issues}.values()
    )

    ordered = sorted(deduped, key=lambda issue: (issue["code"], issue["detail"]))

    gate = gate_decision(ordered, queue["head_human_gate"])

    report: dict[str, Any] = {
        "schema": REVIEW_REPORT_SCHEMA,
        "schema_version": REVIEW_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "review_contract": review_write_contract(),
        "paths": {
            "root": str(resolved_root),
            "project_state": str(resolved_state),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
            "review_ledger": str(resolved_ledger),
        },
        "git": git_view,
        "state": state_summary(state),
        "ledger": ledger_view,
        "pointer": pointer,
        "queue": queue,
        "gate": gate,
        "results": {
            "completed": completed_result_ids(statuses, project_prefix),
            "terminal": {
                task_id: statuses[task_id]
                for task_id in planner.terminal_result_ids(statuses, project_prefix)
            },
            "known_statuses": sorted(orch.KNOWN_RESULT_STATUSES),
        },
        "ancestry": ancestry,
        "issues": ordered,
        "summary": {
            "issue_count": len(ordered),
            "error_count": sum(1 for issue in ordered if issue["severity"] == SEVERITY_ERROR),
            "warning_count": sum(
                1 for issue in ordered if issue["severity"] == SEVERITY_WARNING
            ),
            "has_drift": bool(ordered),
            "advance_allowed": gate["advance_allowed"],
            "exit_code": report_exit_code(ordered),
        },
    }

    report["facts_digest"] = review_report_facts_digest(report)
    report["determinism"] = determinism_section()

    return report


def render_review_report(report: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（固定缩进 + sort_keys；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(report, ensure_ascii=ensure_ascii, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_ledger",
        description=(
            "GPT Review Ledger 只读一致性门禁（GOLD-025）：只报告 stale / missing / "
            "mismatched review 与状态指针漂移，绝不签发 review、绝不修改任何项目状态。"
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
        help=f"review ledger 路径（默认 <root>/{REVIEW_LEDGER_RELATIVE_PATH}）",
    )

    parser.add_argument(
        "--generated-at",
        default=None,
        help="固定 generated_at（便于审计与字节级复现；默认取当前时间）",
    )

    parser.add_argument(
        "--verify-ancestry",
        action="store_true",
        help=(
            "额外只读校验每条 review 的 reviewed_commit.sha 是否为 HEAD 的祖先"
            "（git merge-base --is-ancestor；无法验证时 fail-closed 报告）"
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI 入口：报告写 stdout（纯 ASCII JSON），issue / gate 摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    state_path = (
        Path(args.state) if args.state is not None else root / ".ai" / "PROJECT_STATE.json"
    )

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    ledger_path = (
        Path(args.ledger)
        if args.ledger is not None
        else root / REVIEW_LEDGER_RELATIVE_PATH
    )

    report = build_review_report(
        root=root,
        state_path=state_path,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        ledger_path=ledger_path,
        generated_at=args.generated_at,
        verify_ancestry=bool(args.verify_ancestry),
    )

    sys.stdout.write(render_review_report(report))
    sys.stdout.flush()

    for issue in report["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    gate = report["gate"]

    if not gate["advance_allowed"]:
        print(f"[gate] advance_allowed=False | {gate['reason']}", file=sys.stderr)

    return int(report["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
