"""HTTP 传输层（可注入 Mock，保证测试完全不依赖外部网络）。

设计目标（06_Cline开发规则 第 2 条 / 第 12 条）：
- 重试最多 3 次 + 指数退避 + 超时；
- 传输层与站点逻辑解耦：测试注入 ``MockTransport`` 即可覆盖
  **网络超时 / 429 限流 / 500 服务端错误** 等极端场景，无需访问任何真实站点。

关键约定：
- 所有底层异常都映射为 :class:`TransportError` / :class:`TransportTimeoutError`；
- ``send_with_retry`` 只对"可重试错误"（传输异常、429、5xx 白名单）重试，
  4xx（404/403 等）原样返回交给采集器判断，不浪费重试次数。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import aiohttp

from src.collectors.errors import (
    CollectorFetchError,
    FetchAttempt,
    TransportError,
    TransportTimeoutError,
)

__all__ = [
    "AiohttpTransport",
    "HttpRequest",
    "HttpResponse",
    "RetryPolicy",
    "Transport",
    "map_transport_exception",
    "send_with_retry",
]

SleepFn = Callable[[float], Awaitable[None]]
AttemptHook = Callable[[FetchAttempt], None]


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """与具体 HTTP 客户端无关的请求描述。"""

    url: str
    method: str = "GET"
    params: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """与具体 HTTP 客户端无关的响应描述。"""

    status: int
    json_body: Any = None
    text: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class Transport(Protocol):
    """传输层协议：生产用 :class:`AiohttpTransport`，测试用 Mock。"""

    async def send(self, request: HttpRequest) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """重试策略：最多 3 次尝试 + 指数退避（06_Cline开发规则 第 2 条）。"""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    backoff_factor: float = 2.0
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds 必须为正数")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor 必须 >= 1")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不得小于 base_delay_seconds")

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次尝试失败后的等待时长（指数退避，带上限）。"""
        if attempt < 1:
            raise ValueError("attempt 从 1 开始")
        delay = self.base_delay_seconds * (self.backoff_factor ** (attempt - 1))
        return float(min(delay, self.max_delay_seconds))


def map_transport_exception(exc: BaseException, *, url: str) -> TransportError:
    """把底层异常映射为领域异常（单测可直接喂合成异常，不触发任何 I/O）。"""
    detail = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return TransportTimeoutError(f"请求超时：{url}（{type(exc).__name__}）", details=detail)
    if isinstance(exc, aiohttp.ClientError):
        return TransportError(f"传输失败：{url}（{type(exc).__name__}: {exc}）", details=detail)
    if isinstance(exc, OSError):
        return TransportError(f"网络错误：{url}（{type(exc).__name__}: {exc}）", details=detail)
    return TransportError(
        f"未预期的传输错误：{url}（{type(exc).__name__}: {exc}）", details=detail
    )


def _retry_after_seconds(response: HttpResponse) -> float | None:
    """解析 ``Retry-After``（仅支持秒数形式；HTTP-date 形式暂不解析）。"""
    for key, value in response.headers.items():
        if key.lower() == "retry-after":
            try:
                seconds = float(str(value).strip())
            except ValueError:
                return None
            return seconds if seconds >= 0 else None
    return None


def _resolve_delay(response: HttpResponse, policy: RetryPolicy, attempt: int) -> float:
    """退避时长：优先遵守 429 的 ``Retry-After``，但不超过 ``max_delay_seconds``。"""
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        return float(min(retry_after, policy.max_delay_seconds))
    return policy.delay_for(attempt)


def _record(attempts: list[FetchAttempt], attempt: FetchAttempt, hook: AttemptHook | None) -> None:
    attempts.append(attempt)
    if hook is not None:
        hook(attempt)


async def send_with_retry(
    transport: Transport,
    request: HttpRequest,
    policy: RetryPolicy | None = None,
    *,
    sleep: SleepFn | None = None,
    on_attempt: AttemptHook | None = None,
) -> HttpResponse:
    """发送请求并在可重试错误上重试（最多 ``policy.max_attempts`` 次）。

    Args:
        transport: 传输实现（生产 = AiohttpTransport，测试 = MockTransport）。
        request: 请求描述。
        policy: 重试策略，默认 :class:`RetryPolicy`（3 次尝试 + 指数退避）。
        sleep: 休眠函数；测试注入假实现以校验退避时长且不真正等待。
        on_attempt: 每次尝试结束后的回调（用于统计重试次数）。

    Returns:
        成功响应，或非重试类响应（如 404，交由采集器判断）。

    Raises:
        CollectorFetchError: 尝试次数耗尽仍失败（附每次尝试记录与重试次数）。
    """
    retry_policy = policy or RetryPolicy()
    sleeper: SleepFn = sleep or asyncio.sleep
    attempts: list[FetchAttempt] = []
    last_error = "unknown"

    for attempt in range(1, retry_policy.max_attempts + 1):
        is_last = attempt == retry_policy.max_attempts

        try:
            response = await transport.send(request)
        except (TransportError, TimeoutError) as exc:
            last_error = str(exc)
            delay = 0.0 if is_last else retry_policy.delay_for(attempt)
            _record(attempts, FetchAttempt(attempt, None, last_error, delay), on_attempt)
            if is_last:
                break
            await sleeper(delay)
            continue

        if response.status in retry_policy.retry_statuses:
            last_error = f"HTTP {response.status}"
            delay = 0.0 if is_last else _resolve_delay(response, retry_policy, attempt)
            _record(attempts, FetchAttempt(attempt, response.status, last_error, delay), on_attempt)
            if is_last:
                break
            await sleeper(delay)
            continue

        _record(attempts, FetchAttempt(attempt, response.status, None, 0.0), on_attempt)
        return response

    raise CollectorFetchError(
        f"请求在 {retry_policy.max_attempts} 次尝试后仍失败：{request.url}（{last_error}）",
        attempts=tuple(attempts),
        details={"url": request.url, "last_error": last_error},
    )


class AiohttpTransport:
    """真实 HTTP 传输（生产使用）。

    测试**不实例化**本类：采集器测试一律注入 MockTransport，
    因此测试套件不会访问任何外部站点（也不会因网络抖动而失败）。
    """

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 15.0,
        user_agent: str = "gold-ai-collector/0.2",
    ) -> None:
        self._default_timeout_seconds = default_timeout_seconds
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> AiohttpTransport:
        self._require_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": self._user_agent})
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def send(self, request: HttpRequest) -> HttpResponse:
        """发送请求；底层异常统一映射为领域传输异常。"""
        session = self._require_session()
        timeout = aiohttp.ClientTimeout(
            total=request.timeout_seconds or self._default_timeout_seconds
        )
        try:
            async with session.request(
                method=request.method.upper(),
                url=request.url,
                params=dict(request.params) or None,
                headers=dict(request.headers) or None,
                json=dict(request.json_body) if request.json_body is not None else None,
                timeout=timeout,
            ) as response:
                text = await response.text()
                return HttpResponse(
                    status=response.status,
                    json_body=_safe_json(text),
                    text=text,
                    headers=dict(response.headers),
                )
        except BaseException as exc:  # 统一交给映射函数分类（超时 / 连接 / 其它）
            raise map_transport_exception(exc, url=request.url) from exc


def _safe_json(text: str) -> Any:
    """尽力解析 JSON；非 JSON 响应返回 None（原始文本仍保留在 ``text``）。"""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


