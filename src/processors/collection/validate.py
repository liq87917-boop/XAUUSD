"""数据质量校验（Processor 第四阶段：``validation``）。

设计原则（与采集层 ``src.collectors.validation`` 一致的思路，但**不重复导入**采集模块）：
- 坏数据返回 ``ValidationIssue``（字段 + 原因），**绝不抛异常打断整批**；
- 不猜时间、不猜内容：不知道就判 ``REJECTED`` 并留下原因，由调用方决定重试或人工处理；
- 结论可以写进 ``processed_items.structured_json.reason``（脱敏后）与批次审计摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.common.redaction import safe_text

__all__ = ["ValidationIssue", "format_issues", "validate_normalized_record"]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条校验失败（字段 + 机器可读原因）。"""

    field: str
    reason: str


def validate_normalized_record(
    *,
    source_record_id: str,
    normalized_title: str | None,
    normalized_text: str | None,
    collected_at: datetime | None,
    published_at: datetime | None,
    reference: datetime,
    future_tolerance: timedelta,
) -> tuple[ValidationIssue, ...]:
    """校验一条**已归一化**记录的可用性；返回 issues（空 tuple = 可用）。

    规则（全部为确定性判定，不含"当前时间"以外的运行期状态）：

    ============================== ==================================================
    条件                            结论
    ============================== ==================================================
    ``source_record_id`` 为空       ``source_record_id: missing``（无法构成幂等键）
    ``collected_at`` 缺失           ``collected_at: missing``（时间因果无依据）
    归一化标题与正文均为空           ``content: empty``（空文本不得冒充事实）
    ``published_at`` 超出未来容差    ``published_at: in_future``（时间戳可疑，宁可拒绝）
    ============================== ==================================================
    """
    issues: list[ValidationIssue] = []

    if not str(source_record_id or "").strip():
        issues.append(ValidationIssue("source_record_id", "missing"))
    if collected_at is None:
        issues.append(ValidationIssue("collected_at", "missing"))
    if not (normalized_title or normalized_text):
        issues.append(ValidationIssue("content", "empty"))
    if published_at is not None and published_at > reference + future_tolerance:
        issues.append(ValidationIssue("published_at", "in_future"))
    return tuple(issues)


def format_issues(issues: tuple[ValidationIssue, ...], *, max_chars: int = 200) -> str:
    """issues → 单行原因（写库 / 日志；统一脱敏并截断）。"""
    text = ";".join(f"{issue.field}:{issue.reason}" for issue in issues)
    return safe_text(text, max_chars=max_chars)
