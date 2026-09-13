"""采集器领域异常。

规则（06_Cline开发规则 第 24 条）：异常必须携带可诊断信息，禁止静默吞掉。
所有采集器异常都继承 :class:`CollectorError`，便于 runner 做"单源失败隔离"，
也便于测试精确断言重试次数与失败原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.common.exceptions import GoldAIError

__all__ = [
    "CollectorError",
    "CollectorFetchError",
    "CollectorNotRegisteredError",
    "FetchAttempt",
    "TransportError",
    "TransportTimeoutError",
]


@dataclass(frozen=True, slots=True)
class FetchAttempt:
    """单次 HTTP 尝试的记录（用于重试诊断、日志与测试断言）。"""

    attempt: int
    status: int | None
    error: str | None
    delay_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def describe(self) -> str:
        """人读形式，便于写入日志与错误详情。"""
        outcome = f"HTTP {self.status}" if self.status is not None else (self.error or "unknown")
        return f"#{self.attempt} {outcome} (delay={self.delay_seconds:.2f}s)"


class CollectorError(GoldAIError):
    """采集器异常基类（可预期失败：网络、限流、解析等）。"""

    code = "COLLECTOR_ERROR"
    http_status = 502


class TransportError(CollectorError):
    """传输层失败（连接错误、DNS、TLS、协议异常等）。"""

    code = "COLLECTOR_TRANSPORT_ERROR"


class TransportTimeoutError(TransportError):
    """传输层超时（连接 / 读取超时）。超时同样会触发重试。"""

    code = "COLLECTOR_TRANSPORT_TIMEOUT"


class CollectorFetchError(CollectorError):
    """重试耗尽后的抓取失败，携带每一次尝试的完整记录。"""

    code = "COLLECTOR_FETCH_FAILED"

    def __init__(
        self,
        message: str,
        *,
        attempts: tuple[FetchAttempt, ...] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"attempts": [attempt.describe() for attempt in attempts]}
        merged.update(details or {})
        super().__init__(message, details=merged)
        self.attempts = attempts

    @property
    def retry_count(self) -> int:
        """失败尝试次数（即触发过的重试次数）。"""
        return sum(1 for attempt in self.attempts if not attempt.succeeded)


class CollectorNotRegisteredError(CollectorError):
    """没有与数据源匹配的采集器实现（registry 未注册）。"""

    code = "COLLECTOR_NOT_REGISTERED"
