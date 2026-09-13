"""传输层与重试机制单元测试（06_Cline开发规则 第 2 条）。

全部使用 :class:`MockTransport` 与合成异常，**不访问任何外部网络**。
覆盖三种极端场景：网络超时、429 限流、500 服务端错误。
"""

from __future__ import annotations

import aiohttp
import pytest

from src.collectors.errors import (
    CollectorFetchError,
    TransportError,
    TransportTimeoutError,
)
from src.collectors.transport import (
    AiohttpTransport,
    HttpRequest,
    HttpResponse,
    RetryPolicy,
    Transport,
    map_transport_exception,
    send_with_retry,
)

pytestmark = pytest.mark.unit

REQUEST = HttpRequest(url="https://example.invalid/feed")
OK = HttpResponse(status=200, json_body={"items": []})


async def test_single_success_does_not_retry(mock_transport, recording_sleep) -> None:
    transport = mock_transport([OK])

    response = await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    assert response.status == 200
    assert transport.calls == 1
    assert recording_sleep.delays == []


async def test_network_timeout_triggers_exactly_three_attempts(
    mock_transport, recording_sleep
) -> None:
    transport = mock_transport([TransportTimeoutError("读取超时")] * 3)

    with pytest.raises(CollectorFetchError) as excinfo:
        await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    error = excinfo.value
    assert transport.calls == 3
    assert error.retry_count == 3
    assert error.details["last_error"].startswith("读取超时")
    assert all(attempt.status is None for attempt in error.attempts)
    assert [round(delay, 2) for delay in recording_sleep.delays] == [1.0, 2.0]  # 指数退避


async def test_asyncio_timeout_error_is_also_retried(mock_transport, recording_sleep) -> None:
    """Mock / 第三方库可能直接抛 asyncio.TimeoutError，同样必须触发重试。"""
    transport = mock_transport([TimeoutError("超时")] * 3)

    with pytest.raises(CollectorFetchError) as excinfo:
        await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    assert transport.calls == 3
    assert excinfo.value.retry_count == 3


async def test_timeout_then_success_recovers(mock_transport, recording_sleep) -> None:
    transport = mock_transport([TransportTimeoutError("超时"), OK])

    response = await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    assert response.status == 200
    assert transport.calls == 2
    assert recording_sleep.delays == [1.0]


async def test_rate_limit_429_is_retried_then_succeeds(mock_transport, recording_sleep) -> None:
    transport = mock_transport(
        [
            HttpResponse(status=429, headers={"Retry-After": "2"}),
            HttpResponse(status=429),
            OK,
        ]
    )

    attempts: list[int] = []
    response = await send_with_retry(
        transport, REQUEST, sleep=recording_sleep, on_attempt=lambda a: attempts.append(a.attempt)
    )

    assert response.status == 200
    assert transport.calls == 3
    assert attempts == [1, 2, 3]
    # 第一次遵守 Retry-After=2；第二次走指数退避 delay_for(2)=2.0
    assert recording_sleep.delays == [2.0, 2.0]


async def test_rate_limit_exhausted_fails_after_three_attempts(
    mock_transport, recording_sleep
) -> None:
    transport = mock_transport([HttpResponse(status=429)] * 3)

    with pytest.raises(CollectorFetchError) as excinfo:
        await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    error = excinfo.value
    assert transport.calls == 3
    assert [attempt.status for attempt in error.attempts] == [429, 429, 429]
    assert error.retry_count == 3


async def test_server_error_500_retries_then_fails(mock_transport, recording_sleep) -> None:
    transport = mock_transport([HttpResponse(status=500)] * 3)

    with pytest.raises(CollectorFetchError) as excinfo:
        await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    error = excinfo.value
    assert transport.calls == 3
    assert "HTTP 500" in str(error)
    assert error.retry_count == 3


async def test_non_retryable_status_is_returned_without_retry(
    mock_transport, recording_sleep
) -> None:
    """404/403 等不可重试状态原样返回，交由采集器判断，不浪费重试次数。"""
    transport = mock_transport([HttpResponse(status=404)])

    response = await send_with_retry(transport, REQUEST, sleep=recording_sleep)

    assert response.status == 404
    assert transport.calls == 1
    assert recording_sleep.delays == []


async def test_retry_after_is_capped_by_max_delay(mock_transport, recording_sleep) -> None:
    transport = mock_transport([HttpResponse(status=429, headers={"Retry-After": "600"}), OK])
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=5.0)

    await send_with_retry(transport, REQUEST, policy, sleep=recording_sleep)

    assert recording_sleep.delays == [5.0]


def test_retry_policy_default_is_three_attempts() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert 429 in policy.retry_statuses
    assert 500 in policy.retry_statuses


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": 0},
        {"backoff_factor": 0.5},
        {"base_delay_seconds": 10, "max_delay_seconds": 1},
    ],
)
def test_retry_policy_rejects_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_retry_policy_backoff_grows_and_caps() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, backoff_factor=2.0, max_delay_seconds=5.0)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]
    with pytest.raises(ValueError, match="attempt"):
        policy.delay_for(0)


def test_map_transport_exception_classifies_errors() -> None:
    timeout = map_transport_exception(TimeoutError(), url="https://x.invalid")
    assert isinstance(timeout, TransportTimeoutError)
    assert timeout.details["url"] == "https://x.invalid"

    client = map_transport_exception(aiohttp.ClientPayloadError("payload"), url="https://x.invalid")
    assert isinstance(client, TransportError)
    assert not isinstance(client, TransportTimeoutError)

    network = map_transport_exception(OSError("network down"), url="https://x.invalid")
    assert isinstance(network, TransportError)

    unexpected = map_transport_exception(ValueError("boom"), url="https://x.invalid")
    assert isinstance(unexpected, TransportError)
    assert "未预期" in str(unexpected)


def test_http_response_ok_flag() -> None:
    assert HttpResponse(status=200).ok is True
    assert HttpResponse(status=204).ok is True
    assert HttpResponse(status=301).ok is False
    assert HttpResponse(status=500).ok is False


def test_aiohttp_transport_satisfies_protocol_without_network() -> None:
    """生产传输层可被构造并满足 Transport 协议；测试不发起任何请求。"""
    transport = AiohttpTransport(default_timeout_seconds=3.0)
    assert isinstance(transport, Transport)
    assert transport._session is None


def test_network_guard_blocks_external_connections() -> None:
    """机制自检：conftest 的守卫必须真的拦截外部连接（防止测试悄悄爬外网）。

    直接对 IP 调用 connect，规避 DNS 解析，保证断言即时且不依赖网络状态。
    """
    import socket

    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(AssertionError, match="禁止访问外部网络"),
    ):
        sock.connect(("93.184.216.34", 80))

