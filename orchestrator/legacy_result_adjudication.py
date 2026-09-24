"""历史 legacy result 矛盾的 GPT 专属裁决契约（GOLD-042）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2.13（GOLD-039）把历史矛盾**显式暴露**给 GPT：
``.ai/results/GOLD-028|031|035|038|039|040|041.json`` 顶层 ``status=completed``，
而最终 attempt 的 raw ``finish_reason=aborted`` —— 旧版 Orchestrator 把 Cline raw 值直接
写进了那个「唯一」字段。GPT 因此拒绝为这些 result 写正式 Review 台账（不猜测、不静默
归一化、不回写历史），正式台账停在 GOLD-027，review closure 缺一条**合法恢复路径**。

本模块提供这条路径的**契约**（而不是替 GPT 做决定）：

1. :data:`SCHEMA`（``gold-ai/legacy-result-adjudication/v1``）定义**唯一**的历史矛盾裁决
   记录形状：每条裁决把 ``task_id`` / 裁决类型 / **原 result sha256 + status** /
   **completion commit sha + branch** / 原始 contradiction reason codes / GPT reviewer
   身份与角色 / 理由摘要 / 裁决时间 **绑定在一起**；
2. 裁决记录存放于 :data:`ADJUDICATION_STORE_RELATIVE_PATH`
   （``.ai/adjudications/legacy_result_adjudications.json``），与 ``.ai/results`` **物理分离**，
   **绝不**替换、归一化、删除或静默修补任何历史 result；
3. :func:`validate_adjudications` / :func:`build_adjudication_report` 做**只读、确定性、
   fail-closed** 校验：缺失 / 越权（Cline / DeepSeek / 未知身份）/ 重复 / 冲突 / 过期 /
   身份漂移（result sha256、status、commit sha、commit branch、contradiction codes）
   全部给出稳定 reason code，且**原 result 的 sha256 与字节逐字节不变**；
4. 无论裁决是否有效，报告都**原样回显**从 ``orchestrator.result_terminal_consistency``
   （唯一规则来源）算出的原始 terminal contradiction reason code —— 历史事实绝不因裁决
   而消失、降级或伪装；
5. CLI ``python -m orchestrator.legacy_result_adjudication`` 输出同一份版本化 JSON
   （纯 ASCII stdout，stderr 只放人类摘要）。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- **本任务只实现契约、validator、CLI、文档与测试**：本模块**没有任何写入路径**
  （源码守卫测试锁定），绝不创建真实 GPT 裁决、绝不写
  ``.ai/GPT_REVIEW_LEDGER.json``、``.ai/PROJECT_STATE.json``、``.ai/tasks/**``、
  ``.ai/results/**``、``.ai/adjudications/**``，也绝不 commit / push / reset / checkout；
- **裁决 ≠ Review verdict**：裁决只恢复「GPT 可以对该历史矛盾做实质 review」的可能性，
  不产生 PASS / FAIL、不推进 ``last_reviewed_task``、不解除 ``PHASE3_3_DATA`` blocker、
  不决定 Phase、不改变任何业务资格；
- **GPT-only**：只有 ``reviewer_role=GPT`` 且 reviewer 命中 §2.5 planner 身份
  （``orchestrator.planner_snapshot.PLANNER_AGENTS``）的裁决才是有效事实；Cline / DeepSeek
  / 未知身份一律 fail-closed；
- 零网络、零数据库、零模型调用；唯一的只读 Git 事实来自 :mod:`orchestrator.review_binding`
  的既有口径（本模块自身不启动任何外部进程），**不存在第二套 result / commit 身份算法**。

用法
----
.. code-block:: text

    python -m orchestrator.legacy_result_adjudication                        # 巡检已知矛盾集合
    python -m orchestrator.legacy_result_adjudication --task GOLD-035        # 指定任务（可重复）
    python -m orchestrator.legacy_result_adjudication --as-of 2026-09-24T00:00:00+08:00

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放人类
可读的 issue 摘要（不参与机器解析）。退出码：``0`` 全部请求任务均为有效裁决 /
``2`` fail-closed（缺失 / 越权 / 重复 / 冲突 / 过期 / 身份漂移）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import planner_snapshot as planner
from orchestrator import result_terminal_consistency as terminal_consistency

ROOT = Path(__file__).resolve().parent.parent

# 版本化契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
SCHEMA = "gold-ai/legacy-result-adjudication/v1"

SCHEMA_VERSION = 1

STORE_SCHEMA = "gold-ai/legacy-result-adjudication-store/v1"

STORE_SCHEMA_VERSION = 1

AUTHORITY_SCHEMA = "gold-ai/legacy-result-adjudication-authority/v1"

CONTRACT_SCHEMA = "gold-ai/legacy-result-adjudication-contract/v1"

# 裁决记录**唯一** canonical 位置（相对仓库根）：与 ``.ai/results`` 物理分离。
ADJUDICATION_STORE_RELATIVE_PATH = ".ai/adjudications/legacy_result_adjudications.json"

DEFAULT_ADJUDICATION_STORE_PATH = (
    ROOT / ".ai" / "adjudications" / "legacy_result_adjudications.json"
)

# 不参与确定性 digest 的字段（``as_of`` 是调用方显式传入的参照时间；其余为自引用字段）。
FACTS_EXCLUDED_KEYS = ("as_of", "facts_digest", "determinism")

# GPT 是唯一合法裁决人（reviewer_role，大小写不敏感）。
REVIEWER_ROLE = "GPT"

# 唯一合法裁决类型：历史 result 终态矛盾（§2.13 legacy raw finish_reason 冲突）。
ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION = "legacy_terminal_contradiction"

ADJUDICATION_TYPES = (ADJUDICATION_TYPE_LEGACY_TERMINAL_CONTRADICTION,)

# 已知历史矛盾集合（仅用于 CLI 默认巡检与 fixture / 回归形状参考；
# **绝不**代表这里存在任何真实 GPT 裁决）。
KNOWN_CONTRADICTION_TASKS = (
    "GOLD-028",
    "GOLD-031",
    "GOLD-035",
    "GOLD-038",
    "GOLD-039",
    "GOLD-040",
    "GOLD-041",
)

# 每个请求任务的裁决状态（机器可读，恒定形状）。
TASK_STATE_ADJUDICATED = "adjudicated"

TASK_STATE_UNADJUDICATED = "unadjudicated"

TASK_STATE_INVALID = "invalid"

TASK_STATE_NOT_APPLICABLE = "not_contradictory"

TASK_STATES = (
    TASK_STATE_ADJUDICATED,
    TASK_STATE_UNADJUDICATED,
    TASK_STATE_INVALID,
    TASK_STATE_NOT_APPLICABLE,
)

# 裁决条目必须携带的字段（缺一即 fail-closed，绝不「猜」缺失的审计信息）。
ENTRY_REQUIRED_FIELDS = (
    "adjudication_id",
    "task_id",
    "adjudication_type",
    "reviewer",
    "reviewer_role",
    "reason_summary",
    "adjudicated_at",
    "bound_result",
    "bound_commit",
    "contradiction_reason_codes",
)

BOUND_RESULT_REQUIRED_FIELDS = ("sha256", "status")

BOUND_COMMIT_REQUIRED_FIELDS = ("sha", "branch")

# task_id 只允许安全字符（防路径穿越；``..`` 一律非法）。
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 内容身份口径与 §2.8 Review 台账**完全一致**：result sha256 = 64 位小写十六进制；
# completion commit = 完整 40 位小写 SHA（缩写一律拒绝）。
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

EXIT_OK = 0

EXIT_FAIL_CLOSED = 2

# ---------- 稳定 reason code（供 GPT / 人工 grep 与测试断言，绝不随文案变化） ----------

ISSUE_STORE_MISSING = "ADJUDICATION_STORE_MISSING"
ISSUE_STORE_UNREADABLE = "ADJUDICATION_STORE_UNREADABLE"
ISSUE_STORE_SCHEMA_UNSUPPORTED = "ADJUDICATION_STORE_SCHEMA_UNSUPPORTED"
ISSUE_STORE_ENTRIES_INVALID = "ADJUDICATION_STORE_ENTRIES_INVALID"
ISSUE_ENTRY_INVALID = "ADJUDICATION_ENTRY_INVALID"
ISSUE_TASK_ID_INVALID = "ADJUDICATION_TASK_ID_INVALID"
ISSUE_ADJUDICATION_ID_DUPLICATE = "ADJUDICATION_ID_DUPLICATE"
ISSUE_TASK_ID_DUPLICATE = "ADJUDICATION_TASK_ID_DUPLICATE"
ISSUE_TASK_ID_CONFLICT = "ADJUDICATION_TASK_ID_CONFLICT"
ISSUE_ADJUDICATION_TYPE_INVALID = "ADJUDICATION_TYPE_INVALID"
ISSUE_REVIEWER_MISSING = "ADJUDICATION_REVIEWER_MISSING"
ISSUE_REVIEWER_ROLE_INVALID = "ADJUDICATION_REVIEWER_ROLE_INVALID"
ISSUE_REVIEWER_NOT_AUTHORIZED = "ADJUDICATION_REVIEWER_NOT_AUTHORIZED"
ISSUE_EXECUTOR_FORBIDDEN = "ADJUDICATION_EXECUTOR_FORBIDDEN"
ISSUE_REASON_SUMMARY_MISSING = "ADJUDICATION_REASON_SUMMARY_MISSING"
ISSUE_TIMESTAMP_INVALID = "ADJUDICATION_TIMESTAMP_INVALID"
ISSUE_EXPIRY_INVALID = "ADJUDICATION_EXPIRY_INVALID"
ISSUE_EXPIRED = "ADJUDICATION_EXPIRED"
ISSUE_EXPIRY_UNVERIFIABLE = "ADJUDICATION_EXPIRY_UNVERIFIABLE"
ISSUE_BOUND_RESULT_INVALID = "ADJUDICATION_BOUND_RESULT_INVALID"
ISSUE_BOUND_COMMIT_INVALID = "ADJUDICATION_BOUND_COMMIT_INVALID"
ISSUE_CONTRADICTION_CODES_INVALID = "ADJUDICATION_CONTRADICTION_CODES_INVALID"
ISSUE_LIVE_FACTS_INCOMPLETE = "ADJUDICATION_LIVE_FACTS_INCOMPLETE"
ISSUE_RESULT_NOT_CONTRADICTORY = "ADJUDICATION_RESULT_NOT_CONTRADICTORY"
ISSUE_RESULT_SHA256_DRIFT = "ADJUDICATION_RESULT_SHA256_DRIFT"
ISSUE_RESULT_STATUS_DRIFT = "ADJUDICATION_RESULT_STATUS_DRIFT"
ISSUE_COMMIT_SHA_DRIFT = "ADJUDICATION_COMMIT_SHA_DRIFT"
ISSUE_COMMIT_BRANCH_DRIFT = "ADJUDICATION_COMMIT_BRANCH_DRIFT"
ISSUE_CONTRADICTION_CODE_DRIFT = "ADJUDICATION_CONTRADICTION_CODES_DRIFT"

REASON_CODES = (
    ISSUE_STORE_MISSING,
    ISSUE_STORE_UNREADABLE,
    ISSUE_STORE_SCHEMA_UNSUPPORTED,
    ISSUE_STORE_ENTRIES_INVALID,
    ISSUE_ENTRY_INVALID,
    ISSUE_TASK_ID_INVALID,
    ISSUE_ADJUDICATION_ID_DUPLICATE,
    ISSUE_TASK_ID_DUPLICATE,
    ISSUE_TASK_ID_CONFLICT,
    ISSUE_ADJUDICATION_TYPE_INVALID,
    ISSUE_REVIEWER_MISSING,
    ISSUE_REVIEWER_ROLE_INVALID,
    ISSUE_REVIEWER_NOT_AUTHORIZED,
    ISSUE_EXECUTOR_FORBIDDEN,
    ISSUE_REASON_SUMMARY_MISSING,
    ISSUE_TIMESTAMP_INVALID,
    ISSUE_EXPIRY_INVALID,
    ISSUE_EXPIRED,
    ISSUE_EXPIRY_UNVERIFIABLE,
    ISSUE_BOUND_RESULT_INVALID,
    ISSUE_BOUND_COMMIT_INVALID,
    ISSUE_CONTRADICTION_CODES_INVALID,
    ISSUE_LIVE_FACTS_INCOMPLETE,
    ISSUE_RESULT_NOT_CONTRADICTORY,
    ISSUE_RESULT_SHA256_DRIFT,
    ISSUE_RESULT_STATUS_DRIFT,
    ISSUE_COMMIT_SHA_DRIFT,
    ISSUE_COMMIT_BRANCH_DRIFT,
    ISSUE_CONTRADICTION_CODE_DRIFT,
)

SEVERITY_ERROR = "error"

# ---------- 机器可读契约（只声明规则，不含任何事实） ----------

AUTHORITY: dict[str, Any] = {
    "schema": AUTHORITY_SCHEMA,
    "read_only": True,
    "pure_function": True,
    "emits_review_outcome": False,
    "writes_adjudications": False,
    "writes_results": False,
    "writes_tasks": False,
    "writes_project_state": False,
    "writes_review_ledger": False,
    "tool_can_sign_adjudication": False,
    "tool_can_sign_review": False,
    "tool_can_repair_result": False,
    "tool_can_advance_state": False,
    "tool_can_lift_phase_blocker": False,
    "job": (
        "validate GPT-only legacy result adjudications bound to original "
        "result / commit / contradiction identity"
    ),
}

DETERMINISM: dict[str, Any] = {
    "schema": "gold-ai/legacy-result-adjudication-determinism/v1",
    "wall_clock_fields": ["as_of"],
    "excluded_from_digest": list(FACTS_EXCLUDED_KEYS),
    "ordering": {
        "tasks": "sorted by planner_snapshot.task_id_sort_key",
        "issues": "sorted by (code, detail)",
        "contradiction_reason_codes": "sorted unique",
        "reason_codes": "sorted unique",
    },
    "digest_algorithm": "sha256 over canonical json (sort_keys=True, separators=(',', ':'))",
}


def adjudication_contract() -> dict[str, Any]:
    """GPT 可写 / Executor 只读的裁决契约（机器可读，只声明规则）。"""

    return {
        "schema": CONTRACT_SCHEMA,
        "adjudication_schema": SCHEMA,
        "adjudication_schema_version": SCHEMA_VERSION,
        "store_schema": STORE_SCHEMA,
        "store_schema_version": STORE_SCHEMA_VERSION,
        "store_relative_path": ADJUDICATION_STORE_RELATIVE_PATH,
        "separate_from_results_dir": True,
        "writer_agents": list(planner.PLANNER_AGENTS),
        "reviewer_role": REVIEWER_ROLE,
        "adjudication_types": list(ADJUDICATION_TYPES),
        "required_entry_fields": list(ENTRY_REQUIRED_FIELDS),
        "required_bound_result_fields": list(BOUND_RESULT_REQUIRED_FIELDS),
        "required_bound_commit_fields": list(BOUND_COMMIT_REQUIRED_FIELDS),
        "executor_agents": list(planner.EXECUTOR_AGENTS),
        "executor_can_adjudicate": False,
        "unknown_identity_can_adjudicate": False,
        "module_can_write_adjudication": False,
        "module_can_sign_review": False,
        "module_can_advance_state": False,
        "adjudication_implies_verdict": False,
        "adjudication_advances_review_pointer": False,
        "adjudication_lifts_phase3_3_blocker": False,
        "terminal_consistency_schema": terminal_consistency.SCHEMA,
        "terminal_consistency_rule_source": "orchestrator.result_terminal_consistency",
    }


# ============================================================
# 通用只读工具（确定性）
# ============================================================


def make_issue(code: str, detail: str) -> dict[str, str]:
    """构造一条诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": SEVERITY_ERROR, "detail": detail}


def text_value(value: object) -> str | None:
    """非空字符串（strip 后）→ 字符串，否则 ``None``。"""

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def canonical_json(payload: object) -> str:
    """确定性 JSON 渲染（字段排序固定 + 无多余空白）。"""

    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_digest(payload: object) -> str:
    """确定性内容摘要（sha256，纯函数，不含 wall-clock）。"""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def iso_timestamp(value: object) -> str | None:
    """合法性校验：可被 ``datetime.fromisoformat`` 解析且**带时区**的时间戳。"""

    text = text_value(value)

    if text is None:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))

    except ValueError:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    return text


def timestamp_value(value: object) -> datetime | None:
    """解析为**带时区**的 ``datetime``；不合法 / 无时区一律 ``None``（fail-closed）。"""

    text = iso_timestamp(value)

    if text is None:
        return None

    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def empty_task_view() -> dict[str, Any]:
    """恒定的空任务视图（保证报告形状固定、字段顺序稳定）。"""

    return {
        "task_id": None,
        "state": None,
        "valid": False,
        "adjudication": None,
        "original_contradiction": {
            "contradiction": False,
            "format": None,
            "reason_codes": [],
            "details": [],
        },
        "live_identity": {
            "result_path": None,
            "result_sha256": None,
            "result_status": None,
            "commit_sha": None,
            "commit_branch": None,
            "commit_resolved": False,
        },
        "issues": [],
        "reason_codes": [],
    }


def empty_entry_view() -> dict[str, Any]:
    """恒定的空裁决视图（字段顺序稳定 = 输出可重复）。"""

    return {
        "adjudication_id": None,
        "task_id": None,
        "adjudication_type": None,
        "reviewer": None,
        "reviewer_role": None,
        "reason_summary": None,
        "adjudicated_at": None,
        "expires_at": None,
        "bound_result": {"sha256": None, "status": None},
        "bound_commit": {"sha": None, "branch": None},
        "contradiction_reason_codes": [],
    }


def sorted_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """确定性排序（稳定 reason code + detail）。"""

    return sorted(issues, key=lambda item: (item["code"], item["detail"]))


def issue_codes(issues: list[dict[str, str]]) -> list[str]:
    """issue 集合 → 稳定、去重、排序的 code 列表。"""

    return sorted({issue["code"] for issue in issues})


# ============================================================
# 职责边界：只有 GPT 可以裁决历史矛盾（fail-closed）
# ============================================================


def reviewer_authority(agent: object) -> tuple[bool, str | None]:
    """谁能签发裁决：只有 planner 身份（GPT）。返回值 ``(allowed, reason_code)``。

    未知身份一律拒绝（fail-closed）；Executor（Cline / DeepSeek）单独给出稳定 code，
    便于审计区分「越权尝试」与「未知身份」。
    """

    name = str(agent).strip().lower()

    if name in planner.PLANNER_AGENTS:
        return True, None

    if name in planner.EXECUTOR_AGENTS:
        return False, ISSUE_EXECUTOR_FORBIDDEN

    return False, ISSUE_REVIEWER_NOT_AUTHORIZED


def can_sign_adjudication(agent: object) -> bool:
    """该 agent 是否可以签发历史矛盾裁决（未知 / Executor 一律 ``False``）。"""

    return reviewer_authority(agent)[0]


# ============================================================
# 裁决 store 只读加载与结构一致性（绝不创建 / 修复 / 改写）
# ============================================================


def load_adjudication_store(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """**只读**加载裁决 store；返回 ``(payload, error)``。

    缺失 / 损坏一律返回 error（fail-closed），且**绝不创建或修复** store 文件，
    也绝不触碰 ``.ai/results`` 里任何历史 result。
    """

    if not path.exists():
        return None, f"missing: {path}"

    try:
        raw = path.read_text(encoding="utf-8")

    except (OSError, UnicodeDecodeError) as exc:
        return None, f"unreadable: {exc}"

    try:
        payload = json.loads(raw)

    except ValueError as exc:
        return None, f"invalid json: {exc}"

    if not isinstance(payload, dict):
        return None, "not a JSON object"

    return payload, None


def store_entries(store: Mapping[str, Any]) -> tuple[list[object], list[dict[str, str]]]:
    """校验 store 顶层契约；返回 ``(原始条目列表, issues)``。"""

    issues: list[dict[str, str]] = []

    schema = text_value(store.get("schema"))

    if schema != STORE_SCHEMA:
        issues.append(
            make_issue(
                ISSUE_STORE_SCHEMA_UNSUPPORTED,
                f"store schema={schema!r} 不是 {STORE_SCHEMA}（schema 变化必须 bump 版本，绝不猜）",
            )
        )

    version = store.get("schema_version")

    if version != STORE_SCHEMA_VERSION:
        issues.append(
            make_issue(
                ISSUE_STORE_SCHEMA_UNSUPPORTED,
                f"store schema_version={version!r} 不是 {STORE_SCHEMA_VERSION}",
            )
        )

    entries = store.get("adjudications")

    if not isinstance(entries, list):
        issues.append(make_issue(ISSUE_STORE_ENTRIES_INVALID, "store adjudications 不是数组"))

        return [], issues

    return list(entries), issues


def entry_task_id(entry: object) -> str | None:
    """条目声明的合法 ``task_id``（非法 / 缺失 → ``None``，绝不猜测）。"""

    if not isinstance(entry, Mapping):
        return None

    task_id = text_value(entry.get("task_id"))

    if task_id is None or TASK_ID_PATTERN.match(task_id) is None:
        return None

    return task_id


def store_consistency_issues(
    indexed_entries: Mapping[int, Mapping[str, Any]],
) -> dict[int, list[dict[str, str]]]:
    """同一 store 内的重复 / 冲突检测（按条目序号归集 issue）。

    - 同一个 ``adjudication_id`` 出现多次 ⇒ ``ADJUDICATION_ID_DUPLICATE``；
    - 同一个 ``task_id`` 出现多条且 canonical JSON **完全相同** ⇒
      ``ADJUDICATION_TASK_ID_DUPLICATE``；
    - 同一个 ``task_id`` 出现多条但内容不同 ⇒ ``ADJUDICATION_TASK_ID_CONFLICT``
      （必须 GPT 人工裁决）。
    """

    issues: dict[int, list[dict[str, str]]] = {}

    def flag(index: int, code: str, detail: str) -> None:
        issues.setdefault(index, []).append(make_issue(code, detail))

    by_id: dict[str, list[int]] = {}

    for index, entry in indexed_entries.items():
        adjudication_id = text_value(entry.get("adjudication_id"))

        if adjudication_id is not None:
            by_id.setdefault(adjudication_id, []).append(index)

    for adjudication_id, indexes in by_id.items():
        if len(indexes) > 1:
            for index in indexes:
                flag(
                    index,
                    ISSUE_ADJUDICATION_ID_DUPLICATE,
                    f"adjudication_id={adjudication_id!r} 在 store 中出现 {len(indexes)} 次",
                )

    by_task: dict[str, list[int]] = {}

    for index, entry in indexed_entries.items():
        task_id = entry_task_id(entry)

        if task_id is not None:
            by_task.setdefault(task_id, []).append(index)

    for task_id, indexes in by_task.items():
        if len(indexes) < 2:
            continue

        canonical = {canonical_json(indexed_entries[index]) for index in indexes}

        if len(canonical) == 1:
            for index in indexes:
                flag(
                    index,
                    ISSUE_TASK_ID_DUPLICATE,
                    f"{task_id}: store 里有 {len(indexes)} 条完全相同裁决（重复，一律拒绝）",
                )
        else:
            for index in indexes:
                flag(
                    index,
                    ISSUE_TASK_ID_CONFLICT,
                    f"{task_id}: store 里有 {len(indexes)} 条互相冲突裁决，必须由 GPT 人工裁决",
                )

    return issues


# ============================================================
# live 事实（复用唯一来源）与单条裁决校验
# ============================================================


def analyze_live_contradiction(payload: object) -> dict[str, Any]:
    """**唯一规则来源**：用 ``result_terminal_consistency`` 判定原始终态矛盾。

    无论裁决是否有效，本函数结果都会**原样**出现在报告里：历史矛盾绝不被裁决、
    归一化或修补掩盖。``payload`` 不是 JSON 对象（result 缺失 / 损坏）时不臆测矛盾，
    改由 live facts 不完整 fail-closed 表达。
    """

    if not isinstance(payload, dict):
        return {"contradiction": False, "format": None, "reason_codes": [], "details": []}

    report = terminal_consistency.analyze_result_terminal_consistency(payload)

    return {
        "contradiction": not bool(report["consistent"]),
        "format": report["format"],
        "reason_codes": sorted({str(code) for code in report["reason_codes"]}),
        "details": [
            {"code": str(item["code"]), "detail": str(item["detail"])}
            for item in report["details"]
        ],
    }


def live_identity_view(live: Mapping[str, Any] | None) -> dict[str, Any]:
    """live 身份快照（只回显事实；缺失一律 ``None`` / ``False``）。"""

    facts = live if isinstance(live, Mapping) else {}

    return {
        "result_path": text_value(facts.get("result_path")),
        "result_sha256": text_value(facts.get("result_sha256")),
        "result_status": text_value(facts.get("result_status")),
        "commit_sha": text_value(facts.get("commit_sha")),
        "commit_branch": text_value(facts.get("commit_branch")),
        "commit_resolved": facts.get("commit_resolved") is True,
    }


def bound_result_facts(entry: Mapping[str, Any]) -> tuple[str | None, str | None, bool]:
    """``bound_result`` 事实：``(sha256, status, valid)``。"""

    raw = entry.get("bound_result")

    if not isinstance(raw, Mapping):
        return None, None, False

    sha = text_value(raw.get("sha256"))

    status = text_value(raw.get("status"))

    valid = (
        sha is not None
        and SHA256_PATTERN.match(sha) is not None
        and status is not None
    )

    return sha, status, valid


def bound_commit_facts(entry: Mapping[str, Any]) -> tuple[str | None, str | None, bool]:
    """``bound_commit`` 事实：``(sha, branch, valid)``。"""

    raw = entry.get("bound_commit")

    if not isinstance(raw, Mapping):
        return None, None, False

    sha = text_value(raw.get("sha"))

    branch = text_value(raw.get("branch"))

    valid = (
        sha is not None
        and FULL_COMMIT_SHA_PATTERN.match(sha) is not None
        and branch is not None
    )

    return sha, branch, valid


def contradiction_code_facts(entry: Mapping[str, Any]) -> tuple[list[str], bool]:
    """``contradiction_reason_codes`` 事实：``(codes, valid)``。

    只接受**官方** terminal reason code（``result_terminal_consistency.REASON_CODES``），
    保证裁决绑定的矛盾事实与唯一规则来源同词表。
    """

    raw = entry.get("contradiction_reason_codes")

    if not isinstance(raw, list) or not raw:
        return [], False

    codes: list[str] = []

    for item in raw:
        text = text_value(item)

        if text is None or text not in terminal_consistency.REASON_CODES:
            return [], False

        codes.append(text)

    return sorted(set(codes)), True


def expiry_issues(entry: Mapping[str, Any], as_of: str | None) -> list[dict[str, str]]:
    """有效期事实（可选字段；声明了就必须能被证明未过期，否则 fail-closed）。"""

    raw = entry.get("expires_at")

    if raw is None:
        return []

    expires_at = timestamp_value(raw)

    if expires_at is None:
        return [
            make_issue(
                ISSUE_EXPIRY_INVALID,
                f"expires_at 必须是带时区的 ISO 时间戳: {raw!r}",
            )
        ]

    reference = timestamp_value(as_of)

    if reference is None:
        return [
            make_issue(
                ISSUE_EXPIRY_UNVERIFIABLE,
                "裁决声明了 expires_at，但未提供可解析的 --as-of 参照时间，无法证明未过期"
                "（fail-closed）",
            )
        ]

    if expires_at <= reference:
        return [
            make_issue(
                ISSUE_EXPIRED,
                f"裁决已过期（expires_at={iso_timestamp(raw)} <= as_of={iso_timestamp(as_of)}）",
            )
        ]

    return []


def identity_binding_issues(
    *,
    live: Mapping[str, Any] | None,
    contradiction: Mapping[str, Any],
    bound_result_valid: bool,
    bound_result_sha: str | None,
    bound_result_status: str | None,
    bound_commit_valid: bool,
    bound_commit_sha: str | None,
    bound_commit_branch: str | None,
    bound_codes: list[str],
    codes_valid: bool,
) -> list[dict[str, str]]:
    """内容身份绑定校验（任一漂移 ⇒ fail-closed；缺事实一律 fail-closed）。"""

    issues: list[dict[str, str]] = []

    facts: Mapping[str, Any] = live if isinstance(live, Mapping) else {}

    payload = facts.get("result_payload")

    result_sha = text_value(facts.get("result_sha256"))

    result_status = text_value(facts.get("result_status"))

    commit_sha = text_value(facts.get("commit_sha"))

    commit_branch = text_value(facts.get("commit_branch"))

    commit_resolved = facts.get("commit_resolved") is True

    result_facts_complete = isinstance(payload, dict) and result_sha is not None

    if not result_facts_complete:
        issues.append(
            make_issue(
                ISSUE_LIVE_FACTS_INCOMPLETE,
                "缺少 result payload 或其 canonical sha256，无法核对裁决绑定的原 result 身份",
            )
        )
    elif bound_result_valid:
        if bound_result_sha != result_sha:
            issues.append(
                make_issue(
                    ISSUE_RESULT_SHA256_DRIFT,
                    f"bound_result.sha256={bound_result_sha!r} 与当前 result sha256="
                    f"{result_sha!r} 不一致（原 result 已被改写或绑定错误）",
                )
            )

        if (bound_result_status or "").lower() != (result_status or "").lower():
            issues.append(
                make_issue(
                    ISSUE_RESULT_STATUS_DRIFT,
                    f"bound_result.status={bound_result_status!r} 与当前 result status="
                    f"{result_status!r} 不一致",
                )
            )

    commit_complete = commit_resolved and commit_sha is not None and commit_branch is not None

    if not commit_complete:
        issues.append(
            make_issue(
                ISSUE_LIVE_FACTS_INCOMPLETE,
                "缺少 completion commit identity（sha / branch），无法核对裁决绑定",
            )
        )
    elif bound_commit_valid:
        if bound_commit_sha != commit_sha:
            issues.append(
                make_issue(
                    ISSUE_COMMIT_SHA_DRIFT,
                    f"bound_commit.sha={bound_commit_sha!r} 与终态 commit "
                    f"sha={commit_sha!r} 不一致",
                )
            )

        if bound_commit_branch != commit_branch:
            issues.append(
                make_issue(
                    ISSUE_COMMIT_BRANCH_DRIFT,
                    f"bound_commit.branch={bound_commit_branch!r} 与当前分支="
                    f"{commit_branch!r} 不一致",
                )
            )

    if result_facts_complete:
        if not bool(contradiction.get("contradiction")):
            issues.append(
                make_issue(
                    ISSUE_RESULT_NOT_CONTRADICTORY,
                    "当前 result 终态事实自洽，不存在需要裁决的历史矛盾",
                )
            )
        elif codes_valid:
            live_codes = sorted({str(code) for code in contradiction.get("reason_codes", [])})

            if sorted(set(bound_codes)) != live_codes:
                issues.append(
                    make_issue(
                        ISSUE_CONTRADICTION_CODE_DRIFT,
                        f"bound contradiction codes={sorted(set(bound_codes))} 与当前 terminal "
                        f"contradiction codes={live_codes} 不一致",
                    )
                )

    return issues


def validate_entry(
    entry: object,
    *,
    live: Mapping[str, Any] | None,
    as_of: str | None,
    contradiction: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """校验单条历史矛盾裁决；返回 ``(视图, issues)``（fail-closed，绝不修复）。"""

    view = empty_entry_view()

    issues: list[dict[str, str]] = []

    if not isinstance(entry, Mapping):
        return view, [make_issue(ISSUE_ENTRY_INVALID, "adjudications[i] 不是 JSON 对象")]

    for field in ENTRY_REQUIRED_FIELDS:
        if entry.get(field) is None:
            issues.append(make_issue(ISSUE_ENTRY_INVALID, f"缺少必填字段 {field}"))

    adjudication_id = text_value(entry.get("adjudication_id"))

    view["adjudication_id"] = adjudication_id

    task_id = text_value(entry.get("task_id"))

    view["task_id"] = task_id

    if task_id is None:
        issues.append(make_issue(ISSUE_TASK_ID_INVALID, "缺少 task_id"))
    elif TASK_ID_PATTERN.match(task_id) is None:
        issues.append(make_issue(ISSUE_TASK_ID_INVALID, f"task_id={task_id!r} 形式非法"))

    adjudication_type = text_value(entry.get("adjudication_type"))

    view["adjudication_type"] = adjudication_type

    if adjudication_type not in ADJUDICATION_TYPES:
        issues.append(
            make_issue(
                ISSUE_ADJUDICATION_TYPE_INVALID,
                f"adjudication_type={adjudication_type!r} 不在 {list(ADJUDICATION_TYPES)} 内",
            )
        )

    reviewer = text_value(entry.get("reviewer"))

    view["reviewer"] = reviewer

    if reviewer is None:
        issues.append(make_issue(ISSUE_REVIEWER_MISSING, "缺少 reviewer identity"))
    else:
        allowed, reason_code = reviewer_authority(reviewer)

        if not allowed:
            issues.append(
                make_issue(
                    reason_code or ISSUE_REVIEWER_NOT_AUTHORIZED,
                    f"reviewer={reviewer!r} 不得签发历史矛盾裁决（GPT-only）",
                )
            )

    reviewer_role = text_value(entry.get("reviewer_role"))

    view["reviewer_role"] = reviewer_role

    if reviewer_role is None or reviewer_role.upper() != REVIEWER_ROLE:
        issues.append(
            make_issue(
                ISSUE_REVIEWER_ROLE_INVALID,
                f"reviewer_role 必须是 {REVIEWER_ROLE}: {entry.get('reviewer_role')!r}",
            )
        )

    reason_summary = text_value(entry.get("reason_summary"))

    view["reason_summary"] = reason_summary

    if reason_summary is None:
        issues.append(make_issue(ISSUE_REASON_SUMMARY_MISSING, "缺少 reason_summary"))

    adjudicated_at = iso_timestamp(entry.get("adjudicated_at"))

    view["adjudicated_at"] = adjudicated_at

    if adjudicated_at is None:
        issues.append(
            make_issue(
                ISSUE_TIMESTAMP_INVALID,
                f"adjudicated_at 必须是带时区的 ISO 时间戳: {entry.get('adjudicated_at')!r}",
            )
        )

    issues.extend(expiry_issues(entry, as_of))

    view["expires_at"] = (
        iso_timestamp(entry.get("expires_at")) or text_value(entry.get("expires_at"))
    )

    bound_result_sha, bound_result_status, bound_result_valid = bound_result_facts(entry)

    view["bound_result"] = {"sha256": bound_result_sha, "status": bound_result_status}

    if not bound_result_valid:
        issues.append(
            make_issue(
                ISSUE_BOUND_RESULT_INVALID,
                "bound_result 必须含 64 位小写 sha256 与非空 status（原 result 内容身份）",
            )
        )

    bound_commit_sha, bound_commit_branch, bound_commit_valid = bound_commit_facts(entry)

    view["bound_commit"] = {"sha": bound_commit_sha, "branch": bound_commit_branch}

    if not bound_commit_valid:
        issues.append(
            make_issue(
                ISSUE_BOUND_COMMIT_INVALID,
                "bound_commit 必须含 40 位小写 sha 与非空 branch（completion commit 身份）",
            )
        )

    bound_codes, codes_valid = contradiction_code_facts(entry)

    view["contradiction_reason_codes"] = bound_codes

    if not codes_valid:
        issues.append(
            make_issue(
                ISSUE_CONTRADICTION_CODES_INVALID,
                "contradiction_reason_codes 必须是非空的官方 terminal reason code 列表",
            )
        )

    issues.extend(
        identity_binding_issues(
            live=live,
            contradiction=contradiction,
            bound_result_valid=bound_result_valid,
            bound_result_sha=bound_result_sha,
            bound_result_status=bound_result_status,
            bound_commit_valid=bound_commit_valid,
            bound_commit_sha=bound_commit_sha,
            bound_commit_branch=bound_commit_branch,
            bound_codes=bound_codes,
            codes_valid=codes_valid,
        )
    )

    return view, sorted_issues(issues)


# ============================================================
# 报告构建（只读、确定性、fail-closed）
# ============================================================


def evaluate_task(
    task_id: str,
    *,
    entries_by_task: Mapping[str, Sequence[int]],
    indexed_entries: Mapping[int, Mapping[str, Any]],
    consistency: Mapping[int, list[dict[str, str]]],
    live: Mapping[str, Any] | None,
    as_of: str | None,
) -> dict[str, Any]:
    """单个请求任务的裁决状态（adjudicated / unadjudicated / invalid / not_contradictory）。"""

    view = empty_task_view()

    view["task_id"] = task_id

    view["live_identity"] = live_identity_view(live)

    payload = live.get("result_payload") if isinstance(live, Mapping) else None

    contradiction = analyze_live_contradiction(payload)

    view["original_contradiction"] = contradiction

    indexes = list(entries_by_task.get(task_id, []))

    if not indexes:
        view["state"] = (
            TASK_STATE_UNADJUDICATED
            if bool(contradiction["contradiction"])
            else TASK_STATE_NOT_APPLICABLE
        )

        return view

    issues: list[dict[str, str]] = []

    entry_views: list[dict[str, Any]] = []

    for index in indexes:
        entry_view, entry_issues = validate_entry(
            indexed_entries[index], live=live, as_of=as_of, contradiction=contradiction
        )

        issues.extend(consistency.get(index, []))

        issues.extend(entry_issues)

        entry_views.append(entry_view)

    # 同一 task 的裁决必须**唯一**：多条（重复 / 冲突）时不存在可用的裁决视图。
    if len(indexes) == 1:
        view["adjudication"] = entry_views[0]

    ordered = sorted_issues(issues)

    view["issues"] = ordered

    view["reason_codes"] = issue_codes(ordered)

    view["valid"] = not ordered

    view["state"] = TASK_STATE_ADJUDICATED if not ordered else TASK_STATE_INVALID

    return view


def facts_digest(report: Mapping[str, Any]) -> str:
    """确定性内容摘要（排除 ``as_of`` / ``facts_digest`` / ``determinism``）。"""

    facts = {key: value for key, value in report.items() if key not in FACTS_EXCLUDED_KEYS}

    return canonical_digest(facts)


def collect_store(
    store_path: Path,
) -> tuple[dict[str, Any], list[dict[str, str]], dict[int, Mapping[str, Any]]]:
    """只读收集 store 事实：``(store_section, issues, indexed_entries)``。"""

    store_section: dict[str, Any] = {
        "path": str(store_path),
        "present": False,
        "schema": None,
        "schema_version": None,
        "adjudication_count": 0,
    }

    issues: list[dict[str, str]] = []

    indexed_entries: dict[int, Mapping[str, Any]] = {}

    store, error = load_adjudication_store(store_path)

    if error is not None:
        code = ISSUE_STORE_MISSING if error.startswith("missing") else ISSUE_STORE_UNREADABLE

        issues.append(make_issue(code, f"{store_path}: {error}（绝不创建 / 修复 store）"))

        return store_section, issues, indexed_entries

    if store is None:  # pragma: no cover - load_adjudication_store 只返回 dict 或 error
        return store_section, issues, indexed_entries

    store_section["present"] = True

    store_section["schema"] = text_value(store.get("schema"))

    store_section["schema_version"] = store.get("schema_version")

    raw_entries, schema_issues = store_entries(store)

    issues.extend(schema_issues)

    store_section["adjudication_count"] = len(raw_entries)

    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, Mapping):
            issues.append(
                make_issue(ISSUE_ENTRY_INVALID, f"adjudications[{index}] 不是 JSON 对象")
            )

            continue

        if entry_task_id(entry) is None:
            issues.append(
                make_issue(
                    ISSUE_TASK_ID_INVALID,
                    f"adjudications[{index}]: task_id 缺失或非法，无法归集到具体任务",
                )
            )

            continue

        indexed_entries[index] = entry

    return store_section, issues, indexed_entries


def build_adjudication_report(
    task_ids: Sequence[str],
    *,
    store_path: Path,
    live_facts: Mapping[str, Mapping[str, Any]] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """构建版本化裁决报告（只读、确定性、fail-closed；绝不写任何文件）。"""

    facts_by_task: Mapping[str, Mapping[str, Any]] = live_facts or {}

    store_section, store_issues, indexed_entries = collect_store(store_path)

    consistency = store_consistency_issues(indexed_entries)

    entries_by_task: dict[str, list[int]] = {}

    for index, entry in indexed_entries.items():
        task_id = entry_task_id(entry)

        if task_id is not None:
            entries_by_task.setdefault(task_id, []).append(index)

    tasks: list[dict[str, Any]] = []

    for task_id in sorted(dict.fromkeys(task_ids), key=planner.task_id_sort_key):
        tasks.append(
            evaluate_task(
                task_id,
                entries_by_task=entries_by_task,
                indexed_entries=indexed_entries,
                consistency=consistency,
                live=facts_by_task.get(task_id),
                as_of=as_of,
            )
        )

    all_issues = list(store_issues)

    for view in tasks:
        all_issues.extend(view["issues"])

    ordered_issues = sorted_issues(all_issues)

    unadjudicated = [view for view in tasks if view["state"] == TASK_STATE_UNADJUDICATED]

    invalid = [view for view in tasks if view["state"] == TASK_STATE_INVALID]

    fail_closed = bool(ordered_issues) or bool(unadjudicated) or bool(invalid)

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "as_of": iso_timestamp(as_of) or as_of,
        "store": store_section,
        "authority": AUTHORITY,
        "contract": adjudication_contract(),
        "determinism": DETERMINISM,
        "tasks": tasks,
        "issues": ordered_issues,
        "summary": {
            "task_count": len(tasks),
            "adjudicated_count": sum(
                1 for view in tasks if view["state"] == TASK_STATE_ADJUDICATED
            ),
            "unadjudicated_count": len(unadjudicated),
            "invalid_count": len(invalid),
            "not_applicable_count": sum(
                1 for view in tasks if view["state"] == TASK_STATE_NOT_APPLICABLE
            ),
            "contradiction_count": sum(
                1 for view in tasks if bool(view["original_contradiction"]["contradiction"])
            ),
            "issue_count": len(ordered_issues),
            "fail_closed": fail_closed,
            "exit_code": EXIT_FAIL_CLOSED if fail_closed else EXIT_OK,
        },
    }

    report["facts_digest"] = facts_digest(report)

    return report


def render_adjudication_report(report: Mapping[str, Any]) -> str:
    """确定性 JSON 渲染（默认纯 ASCII，机器通道安全）。"""

    return json.dumps(report, ensure_ascii=True, indent=2, sort_keys=False) + "\n"


# ============================================================
# 只读 CLI（python -m orchestrator.legacy_result_adjudication）
# ============================================================

# live facts 提供者：task_id → 由 §2.8 review_binding 同一口径解析出的客观事实。
LiveFactsProvider = Callable[[str], dict[str, Any]]


def review_binding_live_facts(
    task_id: str,
    *,
    root: Path,
    tasks_dir: Path,
    results_dir: Path,
) -> dict[str, Any]:
    """复用 §2.8 review_binding 的**唯一**事实口径构造 live facts（只读）。

    本模块自身不启动任何外部进程：result 的 canonical sha256 / status 与 completion
    commit sha / branch 全部取自 review_binding manifest（§2.8 只读 Git 白名单），
    result payload 由本模块只读解析，用于复用 §2.13 的唯一矛盾判定规则。
    """

    from orchestrator import review_binding

    manifest = review_binding.build_review_binding_manifest(
        task_id,
        root=root,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
    )

    result = manifest.get("result") or {}

    commit = manifest.get("commit") or {}

    result_path = result.get("path")

    payload: dict[str, Any] | None = None

    if isinstance(result_path, str):
        payload, _error = planner.read_json_mapping(Path(result_path))

    return {
        "result_path": result_path,
        "result_sha256": result.get("sha256"),
        "result_status": result.get("status"),
        "result_payload": payload,
        "commit_sha": commit.get("sha"),
        "commit_branch": commit.get("branch"),
        "commit_resolved": commit.get("resolved") is True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.legacy_result_adjudication",
        description=(
            "历史 legacy result 矛盾的 GPT 专属裁决契约（GOLD-042）：只读校验 "
            ".ai/adjudications/legacy_result_adjudications.json 与原始 result / completion "
            "commit / 原始 contradiction 身份是否一致；绝不签发裁决、绝不修改任何历史 "
            "result、review ledger 或项目状态。"
        ),
    )

    parser.add_argument(
        "--task",
        action="append",
        default=None,
        help="要校验的 task_id（可重复；默认巡检已知矛盾集合）",
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
        "--store",
        type=Path,
        default=None,
        help=f"裁决 store（默认 <root>/{ADJUDICATION_STORE_RELATIVE_PATH}）",
    )

    parser.add_argument(
        "--as-of",
        default=None,
        help=(
            "参照时间（ISO，用于裁决有效期判定；缺省时声明了 expires_at 的裁决一律 "
            "fail-closed）"
        ),
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

    store_path = (
        Path(args.store) if args.store is not None else root / ADJUDICATION_STORE_RELATIVE_PATH
    )

    task_ids: tuple[str, ...] = tuple(args.task) if args.task else KNOWN_CONTRADICTION_TASKS

    live_facts = {
        task_id: review_binding_live_facts(
            task_id, root=root, tasks_dir=tasks_dir, results_dir=results_dir
        )
        for task_id in task_ids
    }

    report = build_adjudication_report(
        task_ids, store_path=store_path, live_facts=live_facts, as_of=args.as_of
    )

    sys.stdout.write(render_adjudication_report(report))

    sys.stdout.flush()

    for issue in report["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    if report["summary"]["fail_closed"]:
        codes = ", ".join(sorted({issue["code"] for issue in report["issues"]}))

        print(f"[adjudication] fail-closed: {codes or 'unadjudicated tasks'}", file=sys.stderr)

    return int(report["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
