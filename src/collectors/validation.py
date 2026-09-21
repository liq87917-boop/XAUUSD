"""数据质量统一校验层（在数据库 CHECK 约束之前）。

不把数据质量检查散落在各采集器 / DB CHECK 上；数据进入数据库之前统一 validation。
无效数据返回 ``ValidationIssue``（skip + reason），**不允许一条坏数据导致整个 batch 失败**
——采集器捕获后跳过该条并计数（``skipped_count``），其余照常入库。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = ["ValidationIssue", "validate_market_bar"]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条校验失败，含字段 + 原因（供 skip 记录与数据质量报告）。"""

    field: str
    reason: str


def validate_market_bar(
    *,
    open_time: datetime | None,
    close_time: datetime | None,
    open_: Decimal | None,
    high: Decimal | None,
    low: Decimal | None,
    close: Decimal | None,
    volume: Decimal | None,
) -> tuple[ValidationIssue, ...]:
    """校验一根 market bar；返回 issues（空 tuple = 合法）。

    覆盖：timestamp 齐全且有序 / OHLC 齐全 / OHLC 关系 / 价格 > 0 / volume >= 0。
    """
    issues: list[ValidationIssue] = []

    if open_time is None or close_time is None:
        issues.append(ValidationIssue("timestamp", "缺 open_time/close_time"))
    elif close_time <= open_time:
        issues.append(ValidationIssue("timestamp", "close_time <= open_time"))

    if open_ is None or high is None or low is None or close is None:
        issues.append(ValidationIssue("ohlc", "缺 OHLC 字段"))
    else:
        if high < max(open_, close):
            issues.append(ValidationIssue("high", "high < max(open, close)"))
        if low > min(open_, close):
            issues.append(ValidationIssue("low", "low > min(open, close)"))
        if high < low:
            issues.append(ValidationIssue("ohlc", "high < low"))
        if min(open_, high, low, close) <= 0:
            issues.append(ValidationIssue("price", "价格 <= 0"))

    if volume is not None and volume < 0:
        issues.append(ValidationIssue("volume", "volume < 0"))

    return tuple(issues)
