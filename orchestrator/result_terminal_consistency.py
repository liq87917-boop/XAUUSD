"""确定性 result 终态一致性校验器（GOLD-039）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2.2 规定：Orchestrator 的判定（``status`` /
``execution_outcome`` / ``normalized_finish_reason``）与 Cline CLI 自报的原始
``cline_finish_reason_raw`` 必须**分开保存**，成功任务绝不能出现「唯一
``finish_reason`` = ``aborted``」的语义歧义。

GOLD-035 的真实 result 却仍然带着历史矛盾：顶层 ``status=completed``，而最终
attempt 的 ``finish_reason=aborted``（旧版本 Orchestrator 把 Cline raw 值直接写进了
那个「唯一」字段）。GPT 因此拒绝为它写正式 Review 台账——不猜测、不静默归一化、
不回写历史。本模块把「终态事实是否自洽」变成**纯函数、确定性、fail-closed** 的判定，
供 Orchestrator 写入前门禁与 GPT review tooling 共用**唯一一份规则**：

- 只有成功语义（``completed``）才能对应顶层 ``status=completed``；
- ``blocked`` / ``failed`` / provider-fatal（最终 attempt ``waiting_external`` 且有
  non-retryable 证据）/ retry-exhausted（顶层 ``blocked`` + 最终 attempt ``failed``
  且有失败证据）必须与最终 attempt 的 ``finish_reason`` / ``failure_class`` /
  ``failure_code`` 组合自洽；
- 未知 / 无法识别的组合一律 fail-closed（稳定 reason code），绝不猜测；
- 历史 result（没有 GOLD-020 归一化字段）走**只读兼容路径**：raw ``finish_reason``
  不再冒充 Orchestrator 判定，只报告矛盾，绝不重写。

边界（不可协商）
----------------
- 本模块**只判事实**：不写 result / task / ``PROJECT_STATE`` / review ledger，
  不签发 review verdict，绝不把历史异常改写成「通过」；
- 纯函数 + 纯标准库：零网络、零数据库、零外部进程、零 wall-clock、零随机；
- **不是**第二套业务资格 / Planner 权限 / 队列算法：只回答「这份 result 的终态
  元数据是否自相矛盾」。

无 attempt 证据时的口径（兼容而非猜测）
----------------------------------------
合成 fixture 与历史 V1 result 可能没有 ``attempts``（或只有迁移过来的空壳 attempt）。
此时本模块只校验**顶层**字段（status / execution_outcome / normalized_finish_reason），
并把 ``final_attempt`` 报为 ``None``：**没有事实就没有组合判定**，既不猜测成功，
也不凭空指控矛盾。任何**已记录**的终态事实一旦冲突或不在词表内，一律 fail-closed。
"""

from __future__ import annotations

from typing import Any

# 版本化契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
SCHEMA = "gold-ai/result-terminal-consistency/v1"

SCHEMA_VERSION = 1

# ---------- Orchestrator 终态词表（与 ai_orchestrator 同义，绝不复用 raw Cline 值） ----------

STATUS_COMPLETED = "completed"

STATUS_BLOCKED = "blocked"

# ``failed`` 只出现在历史 V1 result；新 result 的顶层状态只有 completed / blocked。
STATUS_FAILED = "failed"

OUTCOME_COMPLETED = "completed"

OUTCOME_BLOCKED = "blocked"

OUTCOME_FAILED = "failed"

OUTCOME_WAITING_EXTERNAL = "waiting_external"

TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_BLOCKED, STATUS_FAILED)

NON_TERMINAL_STATUSES = ("pending", "running")

KNOWN_STATUSES = TERMINAL_STATUSES + NON_TERMINAL_STATUSES

OUTCOMES = (OUTCOME_COMPLETED, OUTCOME_BLOCKED, OUTCOME_FAILED, OUTCOME_WAITING_EXTERNAL)

SUCCESS_OUTCOME = OUTCOME_COMPLETED

FAILURE_OUTCOMES = (OUTCOME_FAILED, OUTCOME_WAITING_EXTERNAL)

# ``failure_class`` 的「无失败」词表（缺失视为未记录，绝不当成成功证据）。
NO_FAILURE_CLASSES = ("none",)

FORMAT_NORMALIZED = "normalized"

FORMAT_LEGACY = "legacy"

# 顶层 ``status`` → 允许的**顶层** ``execution_outcome``。
ALLOWED_TOP_LEVEL_OUTCOMES: dict[str, tuple[str, ...]] = {
    STATUS_COMPLETED: (OUTCOME_COMPLETED,),
    STATUS_BLOCKED: (OUTCOME_BLOCKED, OUTCOME_WAITING_EXTERNAL),
    STATUS_FAILED: (OUTCOME_FAILED,),
}

# 顶层 ``status`` → 允许的**最终 attempt** outcome：
# completed 只能是成功；blocked 允许 Cline 自报 blocked、retry-exhausted（failed）、
# provider-fatal（waiting_external）。
ALLOWED_FINAL_OUTCOMES: dict[str, tuple[str, ...]] = {
    STATUS_COMPLETED: (OUTCOME_COMPLETED,),
    STATUS_BLOCKED: (OUTCOME_BLOCKED, OUTCOME_FAILED, OUTCOME_WAITING_EXTERNAL),
    STATUS_FAILED: (OUTCOME_FAILED,),
}

# 归一化格式里唯一合法的成功 finish reason。
SUCCESS_FINISH_REASON = OUTCOME_COMPLETED

# ---------- 权威终态来源（GOLD-046）----------
# Cline CLI 自报的 raw 终态事件是**唯一权威终态来源**。「成功收尾」只认原样保留的
# ``completed``：任何其它 raw 值（``aborted`` / ``error`` / 未知值）以及缺失的 raw 终态
# 都**不能**被 exit code 0 或 validation 全通过改写成 ``completed``。
CLINE_RAW_SUCCESS_FINISH_REASONS = ("completed",)

# 归一化字段名（出现任一即视为 GOLD-020 之后的「归一化格式」）。
NORMALIZED_MARKER_KEYS = (
    "execution_outcome",
    "normalized_finish_reason",
    "cline_finish_reason_raw",
)

# 只读边界（机器可读契约；源码守卫测试锁定本模块没有任何写入路径）。
AUTHORITY: dict[str, Any] = {
    "schema": "gold-ai/result-terminal-consistency-authority/v1",
    "read_only": True,
    "pure_function": True,
    "emits_review_outcome": False,
    "tool_can_sign_review": False,
    "tool_can_repair_result": False,
    "tool_can_advance_state": False,
    "writes_results": False,
    "writes_tasks": False,
    "writes_project_state": False,
    "writes_review_ledger": False,
    "job": "judge whether a result's terminal metadata is self-consistent",
}

# ---------- 稳定 reason code（供 GPT / 人工 grep 与测试断言，绝不随文案变化） ----------

REASON_RESULT_INVALID = "RESULT_TERMINAL_RESULT_INVALID"
REASON_STATUS_MISSING = "RESULT_TERMINAL_STATUS_MISSING"
REASON_STATUS_UNKNOWN = "RESULT_TERMINAL_STATUS_UNKNOWN"
REASON_ATTEMPTS_INVALID = "RESULT_TERMINAL_ATTEMPTS_INVALID"
REASON_ATTEMPT_INVALID = "RESULT_TERMINAL_ATTEMPT_INVALID"
REASON_OUTCOME_INVALID = "RESULT_TERMINAL_OUTCOME_INVALID"
REASON_STATUS_OUTCOME_MISMATCH = "RESULT_TERMINAL_STATUS_OUTCOME_MISMATCH"
REASON_OUTCOME_FINISH_REASON_MISMATCH = "RESULT_TERMINAL_OUTCOME_FINISH_REASON_MISMATCH"
REASON_FINISH_REASON_INVALID = "RESULT_TERMINAL_FINISH_REASON_INVALID"
REASON_FINISH_REASON_MISMATCH = "RESULT_TERMINAL_FINISH_REASON_MISMATCH"
REASON_RAW_FINISH_REASON_MISPLACED = "RESULT_TERMINAL_RAW_FINISH_REASON_MISPLACED"
REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS = "RESULT_TERMINAL_FINAL_ATTEMPT_CONTRADICTS_STATUS"
REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS = (
    "RESULT_TERMINAL_FINAL_EXIT_CODE_CONTRADICTS_STATUS"
)
REASON_FINAL_TIMEOUT_CONTRADICTS_STATUS = (
    "RESULT_TERMINAL_FINAL_TIMEOUT_CONTRADICTS_STATUS"
)
REASON_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS = (
    "RESULT_TERMINAL_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS"
)
REASON_FINAL_VALIDATION_CONTRADICTS_STATUS = (
    "RESULT_TERMINAL_FINAL_VALIDATION_CONTRADICTS_STATUS"
)
REASON_FAILURE_EVIDENCE_MISSING = "RESULT_TERMINAL_FAILURE_EVIDENCE_MISSING"
REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS = (
    "RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS"
)
# GOLD-046：权威最终 raw 终态不是成功收尾（例如 ``aborted``），却被归一成 ``completed``。
REASON_RAW_TERMINAL_CONTRADICTS_COMPLETED = (
    "RESULT_TERMINAL_RAW_TERMINAL_CONTRADICTS_COMPLETED"
)

REASON_CODES = (
    REASON_RESULT_INVALID,
    REASON_STATUS_MISSING,
    REASON_STATUS_UNKNOWN,
    REASON_ATTEMPTS_INVALID,
    REASON_ATTEMPT_INVALID,
    REASON_OUTCOME_INVALID,
    REASON_STATUS_OUTCOME_MISMATCH,
    REASON_OUTCOME_FINISH_REASON_MISMATCH,
    REASON_FINISH_REASON_INVALID,
    REASON_FINISH_REASON_MISMATCH,
    REASON_RAW_FINISH_REASON_MISPLACED,
    REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS,
    REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS,
    REASON_FINAL_TIMEOUT_CONTRADICTS_STATUS,
    REASON_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS,
    REASON_FINAL_VALIDATION_CONTRADICTS_STATUS,
    REASON_FAILURE_EVIDENCE_MISSING,
    REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS,
    REASON_RAW_TERMINAL_CONTRADICTS_COMPLETED,
)


# ============================================================
# 通用只读工具（确定性）
# ============================================================


def text_value(value: object) -> str | None:
    """非空字符串（strip 后）→ 字符串，否则 ``None``。"""

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def int_value(value: object) -> int | None:
    """真正的整数（排除 ``bool``）→ int，否则 ``None``（绝不把 ``True`` 当 1）。"""

    if isinstance(value, int) and not isinstance(value, bool):
        return value

    return None


def normalized_token(value: object) -> str | None:
    """归一化比较用 token：strip + lower；空值一律 ``None``。"""

    text = text_value(value)

    return text.lower() if text is not None else None


def attempt_outcome(attempt: dict[str, Any]) -> str | None:
    """最终 attempt 的 Orchestrator 判定（优先归一化字段，绝不看 raw 字段）。"""

    for key in ("execution_outcome", "normalized_finish_reason"):
        value = normalized_token(attempt.get(key))

        if value is not None:
            return value

    return None


def failing_validation_records(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    """该 attempt 里**已记录**的失败 validation（返回码非 0 或超时）。"""

    validations = attempt.get("validations")

    if not isinstance(validations, list):
        return []

    failing: list[dict[str, Any]] = []

    for entry in validations:
        if not isinstance(entry, dict):
            continue

        returncode = int_value(entry.get("returncode"))

        if (returncode is not None and returncode != 0) or entry.get("timed_out") is True:
            failing.append(entry)

    return failing


def failure_evidence(attempt: dict[str, Any]) -> list[str]:
    """最终 attempt 上**可核对的失败证据**（空列表 ⇒ 无法证明失败语义自洽）。"""

    evidence: list[str] = []

    exit_code = int_value(attempt.get("cline_exit_code"))

    if exit_code is not None and exit_code != 0:
        evidence.append("cline_exit_code")

    if attempt.get("cline_timed_out") is True:
        evidence.append("cline_timed_out")

    failure_class = normalized_token(attempt.get("failure_class"))

    if failure_class is not None and failure_class not in NO_FAILURE_CLASSES:
        evidence.append("failure_class")

    if text_value(attempt.get("failure_code")) is not None:
        evidence.append("failure_code")

    if text_value(attempt.get("recovery_path")) is not None:
        evidence.append("recovery_path")

    if failing_validation_records(attempt):
        evidence.append("validations")

    return evidence


def raw_terminal_is_success(raw_reason: object) -> bool:
    """权威 raw 终态是否证明 CLI 正常收尾（只认原样保留的 ``completed``）。

    - ``completed`` ⇒ ``True``；
    - 其它任何值（``aborted`` / ``error`` / 未知）或缺失 ⇒ ``False``（fail-closed：
      没有权威成功终态就不允许把任务判定为成功）。
    """

    value = normalized_token(raw_reason)

    return value is not None and value in CLINE_RAW_SUCCESS_FINISH_REASONS


def has_terminal_evidence(attempt: dict[str, Any]) -> bool:
    """该 attempt 是否记录了**任何**终态字段（没有事实就不做组合判定）。"""

    keys = (
        "finish_reason",
        "normalized_finish_reason",
        "execution_outcome",
        "cline_exit_code",
        "cline_timed_out",
        "failure_class",
        "failure_code",
        "validations",
    )

    return any(key in attempt for key in keys)


# ============================================================
# 终态一致性判定（纯函数）
# ============================================================


def empty_report() -> dict[str, Any]:
    """恒定形状 / 固定字段顺序的初始报告（确定性：相同输入 ⇒ 相同输出）。"""

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": None,
        "status_known": False,
        "status_terminal": False,
        "format": FORMAT_LEGACY,
        "outcome": None,
        "normalized_finish_reason": None,
        "attempt_count": None,
        "final_attempt": None,
        "final_outcome": None,
        "final_finish_reason": None,
        "final_cline_finish_reason_raw": None,
        "failure_evidence": [],
        "consistent": False,
        "reason_codes": [],
        "details": [],
    }


def finalize_report(
    report: dict[str, Any],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    """稳定排序 / 去重后收口：``consistent`` 只表示「未检出终态矛盾」。"""

    unique = sorted(
        {(item["code"], item["detail"]): item for item in findings}.values(),
        key=lambda item: (item["code"], item["detail"]),
    )

    report["details"] = unique

    report["reason_codes"] = sorted({item["code"] for item in unique})

    report["consistent"] = not report["reason_codes"]

    return report


def result_status(payload: object) -> str | None:
    """只读取 result 顶层 ``status``（归一化小写），无法判定时 ``None``。"""

    if not isinstance(payload, dict):
        return None

    return normalized_token(payload.get("status"))


def is_inconsistent(payload: object) -> bool:
    """便捷判定：这份 result 是否检出终态矛盾（纯只读，零副作用）。"""

    return not analyze_result_terminal_consistency(payload)["consistent"]


def analyze_result_terminal_consistency(payload: object) -> dict[str, Any]:
    """判定一份 result 的**终态事实是否自洽**（纯函数、确定性、fail-closed）。

    返回恒定形状的报告：``consistent`` / ``reason_codes`` / ``details`` + 关键事实回显
    （status / format / 最终 attempt 事实）。**只读**：绝不修改入参，也绝不猜测。
    """

    report = empty_report()

    findings: list[dict[str, str]] = []

    def flag(code: str, detail: str) -> None:
        findings.append({"code": code, "detail": detail})

    if not isinstance(payload, dict):
        flag(REASON_RESULT_INVALID, "result 不是 JSON 对象，无法判定终态自洽性")

        return finalize_report(report, findings)

    status = normalized_token(payload.get("status"))

    report["status"] = status

    report["status_known"] = status in KNOWN_STATUSES

    report["status_terminal"] = status in TERMINAL_STATUSES

    if status is None:
        flag(REASON_STATUS_MISSING, "result 缺少 status：终态事实缺失")
    elif status not in KNOWN_STATUSES:
        flag(REASON_STATUS_UNKNOWN, f"result status={status!r} 不在已知状态词表内")

    final_attempt = collect_final_attempt(report, payload, flag)

    report["format"] = detect_format(payload, final_attempt)

    collect_top_level_findings(report, payload, status, flag)

    if final_attempt is not None and status in TERMINAL_STATUSES:
        collect_attempt_findings(report, final_attempt, status, flag)

    return finalize_report(report, findings)


def collect_final_attempt(
    report: dict[str, Any],
    payload: dict[str, Any],
    flag: Any,
) -> dict[str, Any] | None:
    """读取 ``attempts`` 事实并回显最终 attempt；结构非法一律 fail-closed。"""

    attempts = payload.get("attempts")

    if attempts is None:
        declared = int_value(payload.get("attempt_count"))

        if declared is not None:
            report["attempt_count"] = declared

        return None

    if not isinstance(attempts, list):
        flag(REASON_ATTEMPTS_INVALID, "result attempts 不是数组：最终 attempt 不可判定")

        return None

    report["attempt_count"] = len(attempts)

    if not attempts:
        return None

    candidate = attempts[-1]

    if not isinstance(candidate, dict):
        flag(REASON_ATTEMPT_INVALID, "result attempts 末项不是对象：最终 attempt 不可判定")

        return None

    report["final_attempt"] = len(attempts)

    return candidate


def detect_format(
    payload: dict[str, Any],
    final_attempt: dict[str, Any] | None,
) -> str:
    """``normalized``（GOLD-020 之后带归一化字段）还是 ``legacy``（历史格式）。"""

    for source in (payload, final_attempt):
        if isinstance(source, dict) and any(key in source for key in NORMALIZED_MARKER_KEYS):
            return FORMAT_NORMALIZED

    return FORMAT_LEGACY


class TerminalConsistencyError(RuntimeError):
    """result 终态事实自相矛盾（调用方必须 fail-closed，绝不猜测 / 静默修复）。"""


def collect_top_level_findings(
    report: dict[str, Any],
    payload: dict[str, Any],
    status: str | None,
    flag: Any,
) -> None:
    """顶层 ``execution_outcome`` / ``normalized_finish_reason`` vs ``status``。"""

    outcome = normalized_token(payload.get("execution_outcome"))

    normalized_reason = normalized_token(payload.get("normalized_finish_reason"))

    report["outcome"] = outcome

    report["normalized_finish_reason"] = normalized_reason

    if outcome is not None and outcome not in OUTCOMES:
        flag(
            REASON_OUTCOME_INVALID,
            f"result execution_outcome={outcome!r} 不在 Orchestrator 词表内（未知组合）",
        )

    if normalized_reason is not None and normalized_reason not in OUTCOMES:
        flag(
            REASON_OUTCOME_INVALID,
            f"result normalized_finish_reason={normalized_reason!r} 不在 Orchestrator "
            "词表内（未知组合）",
        )

    if outcome is not None and normalized_reason is not None and outcome != normalized_reason:
        flag(
            REASON_OUTCOME_FINISH_REASON_MISMATCH,
            f"result execution_outcome={outcome!r} 与 normalized_finish_reason="
            f"{normalized_reason!r} 不一致",
        )

    if status in TERMINAL_STATUSES and outcome in OUTCOMES:
        allowed = ALLOWED_TOP_LEVEL_OUTCOMES[status]

        if outcome not in allowed:
            flag(
                REASON_STATUS_OUTCOME_MISMATCH,
                f"result status={status!r} 与 execution_outcome={outcome!r} 不自洽"
                f"（允许：{', '.join(allowed)}）",
            )


def collect_attempt_findings(
    report: dict[str, Any],
    final_attempt: dict[str, Any],
    status: str,
    flag: Any,
) -> None:
    """最终 attempt 的终态事实 vs 顶层 ``status``（未知组合一律 fail-closed）。"""

    final_outcome = attempt_outcome(final_attempt)

    final_reason = normalized_token(final_attempt.get("finish_reason"))

    raw_reason = text_value(final_attempt.get("cline_finish_reason_raw"))

    evidence = failure_evidence(final_attempt)

    report["final_outcome"] = final_outcome

    report["final_finish_reason"] = final_reason

    report["final_cline_finish_reason_raw"] = raw_reason

    report["failure_evidence"] = evidence

    if not has_terminal_evidence(final_attempt):
        # 纯迁移来的空壳 attempt：没有事实 ⇒ 不做组合判定（也绝不猜测）。
        return

    if report["format"] == FORMAT_LEGACY:
        collect_legacy_findings(final_reason, status, flag)
    else:
        collect_normalized_findings(
            final_outcome,
            final_reason,
            raw_reason,
            status,
            evidence,
            flag,
        )

    collect_completed_status_findings(final_attempt, status, flag)


def collect_normalized_findings(
    final_outcome: str | None,
    final_reason: str | None,
    raw_reason: str | None,
    status: str,
    evidence: list[str],
    flag: Any,
) -> None:
    """归一化格式：``finish_reason`` 必须是 Orchestrator 词表内的稳定判定。"""

    if final_outcome is not None and final_outcome not in OUTCOMES:
        flag(
            REASON_OUTCOME_INVALID,
            f"最终 attempt execution_outcome={final_outcome!r} 不在 Orchestrator 词表内",
        )

    if final_reason is not None and final_reason not in OUTCOMES:
        flag(
            REASON_FINISH_REASON_INVALID,
            f"最终 attempt finish_reason={final_reason!r} 不在 Orchestrator 词表内"
            "（raw Cline 值只允许出现在 cline_finish_reason_raw）",
        )

        if raw_reason is not None and raw_reason == final_reason:
            flag(
                REASON_RAW_FINISH_REASON_MISPLACED,
                f"最终 attempt 的 raw Cline 值 {raw_reason!r} 写进了归一化键 finish_reason，"
                "语义位置错误",
            )

    if final_outcome is not None and final_reason is not None and final_outcome != final_reason:
        flag(
            REASON_FINISH_REASON_MISMATCH,
            f"最终 attempt execution_outcome={final_outcome!r} 与 finish_reason="
            f"{final_reason!r} 不一致",
        )

    # GOLD-046：权威最终 raw 终态是唯一来源。``status=completed`` 时，
    # 原样保留的 ``cline_finish_reason_raw`` 必须是成功收尾；``aborted`` / ``error`` /
    # 未知值 / 缺失一律 fail-closed，绝不因 exit code 0 或 validation 通过而放行。
    if status == STATUS_COMPLETED and not raw_terminal_is_success(raw_reason):
        flag(
            REASON_RAW_TERMINAL_CONTRADICTS_COMPLETED,
            "status=completed 但最终 attempt 的权威 raw 终态 cline_finish_reason_raw="
            f"{raw_reason!r} 不是成功收尾（只允许 "
            f"{'/'.join(CLINE_RAW_SUCCESS_FINISH_REASONS)}）：fail-closed，"
            "绝不把非成功 raw 终态归一为 completed",
        )

    effective = final_outcome if final_outcome is not None else final_reason

    allowed = ALLOWED_FINAL_OUTCOMES[status]

    if effective in OUTCOMES and effective not in allowed:
        flag(
            REASON_FINAL_ATTEMPT_CONTRADICTS_STATUS,
            f"顶层 status={status!r} 但最终 attempt outcome={effective!r}"
            f"（允许：{', '.join(allowed)}）",
        )

    if effective in FAILURE_OUTCOMES and not evidence:
        flag(
            REASON_FAILURE_EVIDENCE_MISSING,
            f"最终 attempt outcome={effective!r} 但没有任何可核对的失败证据"
            "（退出码 / 超时 / failure_class / failure_code / validation）",
        )


def collect_legacy_findings(
    final_reason: str | None,
    status: str,
    flag: Any,
) -> None:
    """历史格式（无归一化字段）：唯一 ``finish_reason`` 是 raw Cline 值。

    raw 值**不能**证明 Orchestrator 的成功语义，因此：

    - ``status=completed`` 而 raw 不是 ``completed`` ⇒ 明确冲突；
    - ``status=blocked`` / ``failed`` 而 raw 却是 ``completed`` ⇒ 同样冲突；
    - 其它 raw 值（``error`` / ``aborted`` / 缺失）不据此猜想，只作为事实回显。
    """

    if final_reason is None:
        return

    contradicts = (
        status == STATUS_COMPLETED and final_reason != SUCCESS_FINISH_REASON
    ) or (
        status in (STATUS_BLOCKED, STATUS_FAILED) and final_reason == SUCCESS_FINISH_REASON
    )

    if contradicts:
        flag(
            REASON_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS,
            f"历史 result（无归一化字段）status={status!r} 但唯一 finish_reason="
            f"{final_reason!r}（raw Cline 值，无法证明成功语义）",
        )


def collect_completed_status_findings(
    final_attempt: dict[str, Any],
    status: str,
    flag: Any,
) -> None:
    """``status=completed`` 的客观证据必须全部指向成功（与格式无关）。"""

    if status != STATUS_COMPLETED:
        return

    exit_code = int_value(final_attempt.get("cline_exit_code"))

    if exit_code is not None and exit_code != 0:
        flag(
            REASON_FINAL_EXIT_CODE_CONTRADICTS_STATUS,
            f"status=completed 但最终 attempt cline_exit_code={exit_code}（非 0）",
        )

    if final_attempt.get("cline_timed_out") is True:
        flag(
            REASON_FINAL_TIMEOUT_CONTRADICTS_STATUS,
            "status=completed 但最终 attempt cline_timed_out=true",
        )

    failure_class = normalized_token(final_attempt.get("failure_class"))

    if failure_class is not None and failure_class not in NO_FAILURE_CLASSES:
        flag(
            REASON_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS,
            f"status=completed 但最终 attempt failure_class={failure_class!r}",
        )

    failing = failing_validation_records(final_attempt)

    if failing:
        flag(
            REASON_FINAL_VALIDATION_CONTRADICTS_STATUS,
            f"status=completed 但最终 attempt 记录了 {len(failing)} 条失败 validation",
        )


def assert_result_terminal_consistency(payload: object) -> dict[str, Any]:
    """自洽则返回报告；检出矛盾一律 raise（fail-closed，绝不静默归一化）。"""

    report = analyze_result_terminal_consistency(payload)

    if not report["consistent"]:
        raise TerminalConsistencyError(
            "result 终态事实自相矛盾（fail-closed）：" + ", ".join(report["reason_codes"])
        )

    return report
