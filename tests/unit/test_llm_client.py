"""DeepSeek 客户端的单元测试（**零网络、零 token**：httpx.MockTransport + 假时钟）。

覆盖重点（对应团队 2026-09-13 的强制要求）：
1. **正常返回**：请求体契约（system/user/response_format）、用量、request_id、attempts；
2. **超时重试**：第 1 次超时 → 第 2 次成功；连续超时 → `LLMTimeoutError`（重试 ≤3 次）；
3. **429 限流**：优先遵守 `Retry-After`；重试耗尽 → `LLMRateLimitError`；
4. **5xx**：指数退避后重试；耗尽 → `LLMServerError`；
5. **不可重试错误**：401/403 → `LLMAuthError`；400 → `LLMRequestError`（各只发 1 次）；
6. **速率限制**：滑动窗口 60 次/分钟（注入假时钟 → 断言"等待 60 秒"而非真等）；
7. **预算门禁**：`max_api_calls` 用尽必须报错（重试也计入）；
8. **密钥安全**：密钥只出现在 `Authorization` 头；`repr` / `stats` / 异常消息 /
   返回内容一律掩码（`redact`），**任何输出都不得包含真实 key**。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from src.processors.llm_client import (
    DeepSeekClient,
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    RateLimiter,
    estimate_cost_usd,
    is_peak_utc,
    redact,
)

pytestmark = pytest.mark.unit

#: 测试用假密钥（形如真 key，用于验证"永不泄漏"）
FAKE_KEY = "sk-test-deadbeefdeadbeefdeadbeef0123"
MODEL = "deepseek-chat"


def _ok_body(content: str = '{"opinions": [], "no_opinion": true}') -> dict[str, Any]:
    return {
        "id": "chatcmpl-test-1",
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


class FakeClock:
    """可注入的单调时钟 + 休眠记录（**不真等待**，测试毫秒级完成）。"""

    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class ScriptedTransport(httpx.MockTransport):
    """按脚本依次返回响应/抛异常；并记录收到的请求（供契约断言）。"""

    def __init__(self, script: Sequence[httpx.Response | Exception]) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("脚本已耗尽：出现了预期之外的额外 HTTP 请求")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_client(
    script: Sequence[httpx.Response | Exception] = (),
    *,
    clock: FakeClock | None = None,
    **kwargs: Any,
) -> tuple[DeepSeekClient, ScriptedTransport, FakeClock]:
    transport = ScriptedTransport(script)
    fake_clock = clock or FakeClock()
    client = DeepSeekClient(
        FAKE_KEY,
        model=MODEL,
        transport=transport,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
        **kwargs,
    )
    return client, transport, fake_clock


def _call(client: DeepSeekClient, text: str = "黄金做多，止损 2420") -> Any:
    return client.chat_json(system="system-prompt", user=text)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_successful_call_returns_content_and_usage() -> None:
    client, transport, _ = _make_client([httpx.Response(200, json=_ok_body('{"a": 1}'))])

    response = _call(client)

    assert response.content == '{"a": 1}'
    assert response.model == MODEL
    assert response.request_id == "chatcmpl-test-1"
    assert response.prompt_tokens == 120
    assert response.completion_tokens == 30
    assert response.attempts == 1
    assert response.latency_ms >= 0
    assert len(transport.requests) == 1


def test_request_payload_contract() -> None:
    """请求体必须带 json 模式与温度 0（可复现），且 system/user 顺序正确。"""
    client, transport, _ = _make_client([httpx.Response(200, json=_ok_body())])

    _call(client, "黄金 2380 做多")

    request = transport.requests[0]
    payload = json.loads(request.content.decode("utf-8"))
    assert request.url.path.endswith("/chat/completions")
    assert payload["model"] == MODEL
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "黄金 2380 做多"


def test_request_id_header_wins_over_body() -> None:
    client, _, _ = _make_client(
        [httpx.Response(200, json=_ok_body(), headers={"x-request-id": "req-header-9"})]
    )

    assert _call(client).request_id == "req-header-9"


# ---------------------------------------------------------------------------
# 密钥安全（最高优先级）
# ---------------------------------------------------------------------------
def test_api_key_only_travels_in_authorization_header() -> None:
    client, transport, _ = _make_client([httpx.Response(200, json=_ok_body())])

    _call(client)

    request = transport.requests[0]
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    # 请求体（会进日志 / 缓存）绝不能带密钥
    assert FAKE_KEY not in request.content.decode("utf-8")


def test_repr_and_stats_never_leak_key() -> None:
    client, _, _ = _make_client()

    for text in (
        repr(client),
        json.dumps(client.stats(), ensure_ascii=False),
        client.masked_api_key,
    ):
        assert FAKE_KEY not in text
    assert client.masked_api_key.startswith("sk-***")


def test_response_content_is_redacted() -> None:
    """防御性：上游若回显密钥，进入内容前必须掩码。"""
    client, _, _ = _make_client([httpx.Response(200, json=_ok_body(f"key={FAKE_KEY}"))])

    response = _call(client)

    assert FAKE_KEY not in response.content
    assert "sk-***" in response.content


def test_redact_masks_keys_and_keeps_plain_text() -> None:
    assert redact(f"oops {FAKE_KEY}") == "oops sk-***"
    assert redact("普通文本不受影响") == "普通文本不受影响"
    assert redact("sk-short") == "sk-short"  # 太短不匹配（避免误伤）
    assert redact("泄漏 sk-abcdefghij", secret="sk-abcdefghij") == "泄漏 sk-***"


def test_error_message_body_echoing_key_is_redacted() -> None:
    """4xx 的错误体可能回显密钥 → 异常消息必须已掩码。"""
    client, _, _ = _make_client(
        [httpx.Response(400, json={"error": {"message": f"bad key {FAKE_KEY}"}})]
    )

    with pytest.raises(LLMRequestError) as excinfo:
        _call(client)

    assert FAKE_KEY not in str(excinfo.value)
    assert "sk-***" in str(excinfo.value)


def test_missing_key_raises_config_error() -> None:
    for value in (None, "", "   "):
        with pytest.raises(LLMConfigError):
            DeepSeekClient(value)


# ---------------------------------------------------------------------------
# 测试套件守卫有效性（防"误联网烧 token"）
# ---------------------------------------------------------------------------
def test_suite_guard_blocks_real_httpx_connections() -> None:
    """★ 双保险回归：未注入 MockTransport 的真实客户端必须被套件守卫拦住。

    背景（2026-09-13 实测）：本机代理（如 127.0.0.1:7892）会让"只拦非本机地址"
    的 socket 守卫失效——httpx 经代理真的访问到了 api.deepseek.com。
    因此 conftest 增加了 httpcore 后端级拦截，本测试锁定该防线。
    """
    client = DeepSeekClient(FAKE_KEY, timeout_seconds=5.0)  # 故意不注入 transport

    with pytest.raises(AssertionError) as excinfo:
        _call(client)

    assert "禁止真实 HTTP 建连" in str(excinfo.value)
    assert client.stats()["api_calls"] == 1  # 只尝试了一次，没有被重试放大


# ---------------------------------------------------------------------------
# 超时重试
# ---------------------------------------------------------------------------
def test_timeout_then_success_retries_once() -> None:
    client, transport, clock = _make_client(
        [httpx.ReadTimeout("read timed out"), httpx.Response(200, json=_ok_body())]
    )

    response = _call(client)

    assert response.attempts == 2
    assert client.timeout_hits == 1
    assert client.retries == 1
    assert len(transport.requests) == 2
    assert clock.sleeps == [0.5]  # 指数退避第一次 0.5s


def test_timeout_exhausts_retries() -> None:
    client, transport, clock = _make_client(
        [httpx.ReadTimeout("t1"), httpx.ReadTimeout("t2"), httpx.ReadTimeout("t3")],
        max_retries=2,
    )

    with pytest.raises(LLMTimeoutError):
        _call(client)

    assert len(transport.requests) == 3  # 首次 + 2 次重试
    assert client.api_calls == 3
    assert clock.sleeps == [0.5, 1.0]


def test_default_retry_budget_is_three_retries() -> None:
    client, transport, _ = _make_client(
        [httpx.ConnectError("boom") for _ in range(4)]
    )

    with pytest.raises(LLMError):
        _call(client)

    assert len(transport.requests) == 4  # 首次 + 3 次重试（团队要求 ≤3）
    assert client.max_retries == 3


# ---------------------------------------------------------------------------
# 429 限流
# ---------------------------------------------------------------------------
def test_rate_limit_honours_retry_after_header() -> None:
    client, transport, clock = _make_client(
        [
            httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {}}),
            httpx.Response(200, json=_ok_body()),
        ]
    )

    response = _call(client)

    assert response.attempts == 2
    assert client.rate_limit_hits == 1
    assert clock.sleeps == [3.0]  # 严格按 Retry-After 等待，而不是盲目退避
    assert len(transport.requests) == 2


def test_rate_limit_without_header_uses_backoff() -> None:
    client, _, clock = _make_client(
        [httpx.Response(429), httpx.Response(200, json=_ok_body())]
    )

    _call(client)

    assert clock.sleeps == [0.5]


def test_rate_limit_exhausted_raises() -> None:
    client, transport, _ = _make_client(
        [httpx.Response(429), httpx.Response(429)], max_retries=1
    )

    with pytest.raises(LLMRateLimitError):
        _call(client)

    assert client.rate_limit_hits == 2
    assert len(transport.requests) == 2


def test_retry_after_header_is_capped() -> None:
    """上游若返回荒谬的 Retry-After（如 9999 秒），必须被上限截断。"""
    client, _, clock = _make_client(
        [
            httpx.Response(429, headers={"Retry-After": "9999"}),
            httpx.Response(200, json=_ok_body()),
        ]
    )

    _call(client)

    assert clock.sleeps == [8.0]  # BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------------------
# 5xx
# ---------------------------------------------------------------------------
def test_server_error_then_success() -> None:
    client, transport, clock = _make_client(
        [httpx.Response(503, json={"error": {}}), httpx.Response(200, json=_ok_body())]
    )

    response = _call(client)

    assert response.attempts == 2
    assert client.server_error_hits == 1
    assert clock.sleeps == [0.5]
    assert len(transport.requests) == 2


def test_server_error_exhausted_raises() -> None:
    client, _, _ = _make_client(
        [httpx.Response(500), httpx.Response(502)], max_retries=1
    )

    with pytest.raises(LLMServerError):
        _call(client)

    assert client.server_error_hits == 2


# ---------------------------------------------------------------------------
# 不可重试错误（401/403/400）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_does_not_retry(status: int) -> None:
    client, transport, clock = _make_client(
        [httpx.Response(status, json={"error": {"message": "invalid api key"}})]
    )

    with pytest.raises(LLMAuthError):
        _call(client)

    assert len(transport.requests) == 1  # 重试没有意义，不许浪费请求
    assert client.retries == 0
    assert clock.sleeps == []


def test_bad_request_does_not_retry_and_mentions_status() -> None:
    client, transport, _ = _make_client(
        [httpx.Response(422, json={"error": {"message": "messages 字段非法"}})]
    )

    with pytest.raises(LLMRequestError) as excinfo:
        _call(client)

    assert "422" in str(excinfo.value)
    assert "messages 字段非法" in str(excinfo.value)
    assert len(transport.requests) == 1


def test_bad_request_with_plain_text_body() -> None:
    client, _, _ = _make_client(
        [httpx.Response(400, text="<html>bad gateway</html>")]
    )

    with pytest.raises(LLMRequestError) as excinfo:
        _call(client)

    assert "bad gateway" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 响应结构异常（200 但不可用）
# ---------------------------------------------------------------------------
def test_non_json_body_raises_response_error_without_retry() -> None:
    client, transport, _ = _make_client(
        [httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})]
    )

    with pytest.raises(LLMResponseError):
        _call(client)

    assert len(transport.requests) == 1


def test_missing_choices_raises_response_error() -> None:
    client, _, _ = _make_client([httpx.Response(200, json={"id": "x", "model": MODEL})])

    with pytest.raises(LLMResponseError):
        _call(client)


def test_empty_content_raises_response_error() -> None:
    body = _ok_body()
    body["choices"][0]["message"]["content"] = "   "
    client, _, _ = _make_client([httpx.Response(200, json=body)])

    with pytest.raises(LLMResponseError):
        _call(client)


def test_missing_usage_defaults_to_zero() -> None:
    body = _ok_body()
    body.pop("usage")
    client, _, _ = _make_client([httpx.Response(200, json=body)])

    response = _call(client)

    assert response.prompt_tokens == 0
    assert response.completion_tokens == 0


# ---------------------------------------------------------------------------
# 速率限制（滑动窗口）
# ---------------------------------------------------------------------------
def test_rate_limiter_allows_burst_until_limit() -> None:
    clock = FakeClock()
    limiter = RateLimiter(max_requests=3, clock=clock.monotonic, sleep=clock.sleep)

    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert clock.sleeps == []


def test_rate_limiter_waits_for_window_to_slide() -> None:
    clock = FakeClock()
    limiter = RateLimiter(
        max_requests=1, window_seconds=60.0, clock=clock.monotonic, sleep=clock.sleep
    )

    limiter.acquire()
    waited = limiter.acquire()

    assert waited == 60.0
    assert clock.sleeps == [60.0]
    assert limiter.stats()["waited_seconds"] == 60.0


def test_rate_limiter_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_requests=0)


def test_client_enforces_per_minute_limit() -> None:
    """第 3 次请求必须等满窗口（默认 60 次/分钟；此处设置为 2 次便于验证）。"""
    clock = FakeClock()
    client, transport, _ = _make_client(
        [httpx.Response(200, json=_ok_body()) for _ in range(3)], clock=clock,
        rate_limit_per_minute=2,
    )

    for _ in range(3):
        _call(client)

    assert len(transport.requests) == 3
    assert clock.sleeps == [60.0]
    assert client.stats()["limiter"]["in_window"] == 1


# ---------------------------------------------------------------------------
# 预算门禁与统计
# ---------------------------------------------------------------------------
def test_max_api_calls_blocks_further_requests() -> None:
    """★ 预算门禁：超过上限直接报错，避免脚本悄悄烧钱。"""
    client, transport, _ = _make_client(
        [httpx.Response(200, json=_ok_body())], max_api_calls=1
    )

    _call(client)

    with pytest.raises(LLMError) as excinfo:
        _call(client)

    assert "max_api_calls=1" in str(excinfo.value)
    assert len(transport.requests) == 1  # 第二次没有真的发出去
    assert client.api_calls == 1


def test_retries_count_towards_budget() -> None:
    """重试也是真实请求，必须计入预算（否则限流时预算会失控）。"""
    client, transport, _ = _make_client(
        [httpx.Response(500), httpx.Response(500)], max_retries=1, max_api_calls=2
    )

    with pytest.raises(LLMServerError):
        _call(client)

    assert len(transport.requests) == 2
    stats = client.stats()
    assert stats["api_calls"] == 2
    assert stats["server_error_hits"] == 2
    assert stats["max_api_calls"] == 2


def test_stats_shape_is_safe_for_reporting() -> None:
    client, _, _ = _make_client([httpx.Response(200, json=_ok_body())])

    _call(client)
    stats = client.stats()

    assert set(stats) == {
        "model",
        "base_url",
        "api_calls",
        "retries",
        "max_api_calls",
        "thinking_mode",
        "rate_limit_hits",
        "server_error_hits",
        "timeout_hits",
        "limiter",
        "usage",
    }
    assert stats["api_calls"] == 1
    assert stats["base_url"].startswith("https://")
    assert stats["thinking_mode"] == "disabled"
    assert stats["usage"]["prompt_tokens"] == 120
    assert stats["usage"]["pricing_source"].startswith("https://")


# ---------------------------------------------------------------------------
# 思考模式 / 上下文缓存计费 / 费用估算（2026-09-13 实测口径）
# ---------------------------------------------------------------------------
def test_payload_disables_thinking_by_default() -> None:
    """新一代模型默认开启思考模式 → 请求体必须显式 `thinking.type=disabled`。"""
    client, transport, _ = _make_client([httpx.Response(200, json=_ok_body())])

    _call(client)

    payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert payload["thinking"] == {"type": "disabled"}


def test_payload_can_enable_thinking_explicitly() -> None:
    client, transport, _ = _make_client(
        [httpx.Response(200, json=_ok_body())], thinking_mode="enabled"
    )

    _call(client)

    payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert payload["thinking"] == {"type": "enabled"}


def test_invalid_thinking_mode_is_rejected() -> None:
    with pytest.raises(LLMConfigError) as excinfo:
        DeepSeekClient(FAKE_KEY, thinking_mode="maybe")

    assert "thinking_mode" in str(excinfo.value)


def test_cache_hit_and_miss_tokens_are_recorded() -> None:
    body = _ok_body()
    body["usage"] = {
        "prompt_tokens": 3000,
        "completion_tokens": 40,
        "prompt_cache_hit_tokens": 2048,
        "prompt_cache_miss_tokens": 952,
    }
    client, _, _ = _make_client([httpx.Response(200, json=body)])

    response = _call(client)

    assert response.cache_hit_tokens == 2048
    assert response.cache_miss_tokens == 952
    usage = client.usage_totals()
    assert usage["cache_hit_tokens"] == 2048
    assert usage["cache_miss_tokens"] == 952
    assert usage["total_tokens"] == 3040


def test_usage_totals_are_zero_before_any_call() -> None:
    client, _, _ = _make_client()

    usage = client.usage_totals()

    assert usage["prompt_tokens"] == 0
    assert usage["estimated_cost_usd_peak"] is not None


def test_estimate_cost_usd_splits_cache_hit_and_miss() -> None:
    """缓存命中单价远低于未命中 → 命中部分必须按命中价算（不能整体按未命中算）。"""
    common = {"model": "deepseek-flash", "prompt_tokens": 1_000_000, "completion_tokens": 0}

    all_hit = estimate_cost_usd(**common, cache_hit_tokens=1_000_000, peak=True)
    all_miss = estimate_cost_usd(**common, cache_hit_tokens=0, peak=True)

    assert all_hit == pytest.approx(0.006)
    assert all_miss == pytest.approx(0.3)
    assert all_hit is not None and all_miss is not None and all_hit < all_miss


def test_estimate_cost_usd_peak_is_double_offpeak() -> None:
    kwargs = {
        "model": "deepseek-flash",
        "prompt_tokens": 0,
        "completion_tokens": 1_000_000,
        "cache_hit_tokens": 0,
    }

    peak = estimate_cost_usd(**kwargs, peak=True)
    offpeak = estimate_cost_usd(**kwargs, peak=False)

    assert peak == pytest.approx(1.2)
    assert offpeak == pytest.approx(0.6)


def test_estimate_cost_usd_maps_legacy_model_name_and_unknown_returns_none() -> None:
    legacy = estimate_cost_usd(
        model="deepseek-chat", prompt_tokens=1_000_000, completion_tokens=0, peak=True
    )
    unknown = estimate_cost_usd(
        model="gpt-4o", prompt_tokens=1_000_000, completion_tokens=0, peak=True
    )

    assert legacy == pytest.approx(0.3)  # 旧名实测路由到 flash → 按 flash 计价
    assert unknown is None  # 不瞎猜


def test_is_peak_utc_windows() -> None:
    """峰值时段：01:00-04:00 与 06:00-10:00 UTC（周一至周五）。"""
    monday = datetime(2026, 9, 14, tzinfo=UTC)  # 周一
    saturday = datetime(2026, 9, 19, tzinfo=UTC)

    assert is_peak_utc(monday.replace(hour=2)) is True
    assert is_peak_utc(monday.replace(hour=7)) is True
    assert is_peak_utc(monday.replace(hour=5)) is False  # 两个峰值窗口之间的低谷
    assert is_peak_utc(monday.replace(hour=12)) is False
    assert is_peak_utc(saturday.replace(hour=2)) is False  # 周末全低谷


def test_is_peak_utc_requires_timezone() -> None:
    with pytest.raises(ValueError):
        is_peak_utc(datetime(2026, 9, 14, 2))  # noqa: DTZ001 - 故意传 naive 时间