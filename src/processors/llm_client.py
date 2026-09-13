"""DeepSeek Chat Completions 客户端（**同步**；可注入 transport，单测零网络）。

对应团队要求（2026-09-13）：
- **超时重试 ≤ 3 次**、**速率限制 60 次/分钟**、显式处理 **429 / 5xx**；
- **密钥绝不外泄**：不 print、不进日志、不进缓存；`repr()` 显示掩码；
  异常消息与响应文本统一走 `redact()`（防上游回显）；
- 可注入 `httpx.BaseTransport`：单测用 `httpx.MockTransport` 覆盖
  正常 / 超时 / 429 / 5xx / 401，**不发真实请求、不花一个 token**；
- **预算门禁**：`max_api_calls` 内请求数超限直接报错，绝不让脚本悄悄烧钱。

与 `src/collectors/transport.py` 的关系：那边是采集器用的 asyncio+aiohttp 版本；
抽取器协议（`OpinionExtractor`）是同步的，所以这里给同步实现，
但沿用同样的约定：只重试可重试错误、异常统一映射、传输层可注入、绝不记录敏感信息。

**2026-09-13 用真实 API 实测确认的 4 条事实**（探针 `logs/_probe_deepseek.py`，耗 ~65 tokens）：

1. `GET /models` 当前只返回 `deepseek-flash` / `deepseek-v4-pro`；旧名 `deepseek-chat`
   **仍可调用但被静默路由到 `deepseek-flash`**（响应里 `model` 回显 `deepseek-flash`）。
   因此默认模型改为 `deepseek-flash`：模型名是**缓存键的一部分**，名字与实际模型不一致
   会让"缓存里到底是谁算的"说不清。
2. 新一代模型**默认开启思考模式**（`thinking.enabled`，effort=high）：对"抽取 → 严格 JSON"
   这种确定性任务，思考会吃掉 `max_tokens` 且更贵，所以默认显式
   `thinking={"type": "disabled"}`（实测 200 且不返回 `reasoning_content`）。
3. `response_format={"type": "json_object"}` 可用。
4. `usage` 里带 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
   → **可以按真实缓存命中/未命中分别计费**，成本估算不用猜。
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_API_CALLS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MODEL",
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "DEFAULT_THINKING_MODE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PRICING_SOURCE",
    "PRICING_USD_PER_MILLION",
    "THINKING_MODES",
    "DeepSeekClient",
    "LLMAuthError",
    "LLMConfigError",
    "LLMError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMResponse",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "RateLimiter",
    "estimate_cost_usd",
    "is_peak_utc",
    "redact",
]

DEFAULT_BASE_URL: Final[str] = "https://api.deepseek.com"
#: 2026-09-13 实测：`deepseek-chat` 已被静默路由到 `deepseek-flash`，故直接写后者
DEFAULT_MODEL: Final[str] = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_RATE_LIMIT_PER_MINUTE: Final[int] = 60
DEFAULT_MAX_API_CALLS: Final[int] = 250
DEFAULT_MAX_TOKENS: Final[int] = 600
DEFAULT_TEMPERATURE: Final[float] = 0.0
#: 思考模式开关：抽取任务要"确定性 + 严格 JSON + 便宜"，默认关闭
THINKING_MODES: Final[tuple[str, ...]] = ("disabled", "enabled")
DEFAULT_THINKING_MODE: Final[str] = "disabled"
BACKOFF_BASE_SECONDS: Final[float] = 0.5
BACKOFF_MAX_SECONDS: Final[float] = 8.0
CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"
#: 任何形如 sk-xxxx 的串都要被掩码（含 32 位 hex 的真 key 与测试用的假 key）
_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")

# ---------------------------------------------------------------------------
# 价格表（**唯一来源**：https://api-docs.deepseek.com/quick_start/pricing，2026-09-13 抓取）
# 单位：USD / 1M tokens。峰谷价：峰值 = 01:00-04:00 与 06:00-10:00 UTC（周一至周五），
# 其余时段为低谷价（低谷 = 峰值一半）。价格随时会变 → 只用于**估算**，不作为账单依据；
# 需要更准的数字请用 `estimate_cost_usd(..., price_table=...)` 覆盖或直接看控制台账单。
# ---------------------------------------------------------------------------
PRICING_SOURCE: Final[str] = "https://api-docs.deepseek.com/quick_start/pricing (2026-09-13)"
PRICING_USD_PER_MILLION: Final[dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "peak_input_cache_hit": 0.006,
        "peak_input_cache_miss": 0.3,
        "peak_output": 1.2,
        "offpeak_input_cache_hit": 0.003,
        "offpeak_input_cache_miss": 0.15,
        "offpeak_output": 0.6,
    },
    "deepseek-v4-pro": {
        "peak_input_cache_hit": 0.044,
        "peak_input_cache_miss": 1.32,
        "peak_output": 3.96,
        "offpeak_input_cache_hit": 0.022,
        "offpeak_input_cache_miss": 0.66,
        "offpeak_output": 1.98,
    },
}
#: 旧模型名 → 当前实际计费模型的别名（实测 `deepseek-chat` 返回 `deepseek-flash`）
_PRICING_ALIASES: Final[dict[str, str]] = {"deepseek-chat": "deepseek-flash"}


def is_peak_utc(moment: datetime) -> bool:
    """是否处于 DeepSeek 的**峰值计费时段**（01:00-04:00、06:00-10:00 UTC，周一至周五）。"""
    if moment.tzinfo is None:
        raise ValueError("is_peak_utc 需要带时区的时间（系统统一 UTC）")
    utc = moment.astimezone(UTC)
    if utc.weekday() >= 5:  # 周六 / 周日全为低谷
        return False
    return 1 <= utc.hour < 4 or 6 <= utc.hour < 10


def estimate_cost_usd(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int | None = None,
    peak: bool = True,
    price_table: Mapping[str, Mapping[str, float]] | None = None,
) -> float | None:
    """按官方价目表估算费用（USD）。

    Args:
        model: 模型名（`deepseek-flash` / `deepseek-v4-pro`；旧名会自动映射）。
        prompt_tokens / completion_tokens: 输入 / 输出 token 数。
        cache_hit_tokens: 命中上下文缓存（更便宜）的输入 token 数。
        cache_miss_tokens: 未命中的输入 token 数；缺省 = `prompt_tokens - cache_hit_tokens`。
        peak: `True` 用峰值价（**保守默认**，宁可高估不低估）。
        price_table: 覆盖价目表（测试或官方调价后使用）。

    Returns:
        估算金额（USD）；**未知模型返回 `None`**（不瞎猜，由调用方决定怎么报）。
    """
    table = price_table or PRICING_USD_PER_MILLION
    key = _PRICING_ALIASES.get(model, model)
    prices = table.get(key)
    if prices is None:
        return None
    hit_tokens = max(0, cache_hit_tokens)
    miss_tokens = (
        max(0, prompt_tokens - hit_tokens)
        if cache_miss_tokens is None
        else max(0, cache_miss_tokens)
    )
    prefix = "peak" if peak else "offpeak"
    total = (
        hit_tokens * prices[f"{prefix}_input_cache_hit"]
        + miss_tokens * prices[f"{prefix}_input_cache_miss"]
        + max(0, completion_tokens) * prices[f"{prefix}_output"]
    )
    return total / 1_000_000


def redact(text: str, *, secret: str | None = None) -> str:
    """把密钥掩码掉（`redact` 用于所有异常消息与落盘文本，防泄漏）。"""
    masked = _SECRET_PATTERN.sub("sk-***", text or "")
    if secret:
        masked = masked.replace(secret, "sk-***")
    return masked


class LLMError(RuntimeError):
    """LLM 客户端错误基类（所有消息都已脱敏）。"""


class LLMConfigError(LLMError):
    """配置缺失/非法（如没配 `DEEPSEEK_API_KEY`）。"""


class LLMAuthError(LLMError):
    """401/403：密钥无效或权限不足（**不重试**，重试也没用）。"""


class LLMRequestError(LLMError):
    """其它 4xx：请求本身有问题（**不重试**）。"""


class LLMRateLimitError(LLMError):
    """429 且重试耗尽。"""


class LLMServerError(LLMError):
    """5xx 且重试耗尽。"""


class LLMTimeoutError(LLMError):
    """超时且重试耗尽。"""


class LLMResponseError(LLMError):
    """HTTP 200 但响应结构不可用（缺 choices / 内容为空 / 不是 JSON）。"""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次成功的 LLM 调用结果（**原始文本**原样保留，供缓存与审计）。"""

    content: str
    model: str
    request_id: str
    latency_ms: int
    attempts: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 命中上下文缓存的输入 token（DeepSeek 单价更低）——`usage.prompt_cache_hit_tokens`
    cache_hit_tokens: int = 0
    #: 未命中缓存的输入 token —— `usage.prompt_cache_miss_tokens`
    cache_miss_tokens: int = 0

    def to_metadata(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


class RateLimiter:
    """滑动窗口限速（默认 60 次/分钟）；`clock` / `sleep` 可注入 → 单测零等待。"""

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests 必须 ≥ 1")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self.waited_seconds = 0.0

    def acquire(self) -> float:
        """必要时等待，返回本次等待秒数（0 表示可直接发请求）。"""
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= self.window_seconds:
            self._stamps.popleft()
        waited = 0.0
        if len(self._stamps) >= self.max_requests:
            waited = self.window_seconds - (now - self._stamps[0])
            if waited > 0:
                self._sleep(waited)
                self.waited_seconds += waited
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self.window_seconds:
                    self._stamps.popleft()
        self._stamps.append(self._clock())
        return waited

    def stats(self) -> dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
            "in_window": len(self._stamps),
            "waited_seconds": round(self.waited_seconds, 3),
        }


class DeepSeekClient:
    """DeepSeek `/chat/completions` 的同步客户端。

    Args:
        api_key: 密钥（来自 `Settings.deepseek_api_key` / 环境变量）；**只存内存**。
        base_url / model: 服务地址与模型名。
        timeout_seconds: 单次请求超时。
        max_retries: 重试次数（对超时 / 429 / 5xx 生效，**不含**首次尝试）。
        rate_limit_per_minute: 每分钟最多请求数（滑动窗口）。
        max_api_calls: 预算门禁——本实例累计真实请求数超限即报错。
        thinking_mode: `disabled`（默认）/ `enabled`；抽取任务必须关闭思考模式
            （新一代模型**默认开启**，会额外烧输出 token 并挤占 `max_tokens`）。
        transport: 可注入的 `httpx.BaseTransport`（单测用 `MockTransport`）。
        clock / sleep: 可注入时钟与休眠（单测零等待）。
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        max_api_calls: int = DEFAULT_MAX_API_CALLS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        thinking_mode: str = DEFAULT_THINKING_MODE,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_key.strip():
            raise LLMConfigError(
                "缺少 DEEPSEEK_API_KEY：请在本地 .env 或环境变量中配置"
                "（禁止硬编码；密钥不会出现在任何输出里）"
            )
        if thinking_mode not in THINKING_MODES:
            raise LLMConfigError(f"未知 thinking_mode：{thinking_mode!r}（可选 {THINKING_MODES}）")
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking_mode = thinking_mode
        self.max_api_calls = max_api_calls
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self.limiter = RateLimiter(
            max_requests=rate_limit_per_minute, clock=clock, sleep=sleep
        )
        self.api_calls = 0
        self.retries = 0
        self.rate_limit_hits = 0
        self.server_error_hits = 0
        self.timeout_hits = 0
        # Token 累加（只统计**成功响应**；失败/重试尝试的 token 服务端仍会计费，
        # 这部分无法从响应里拿到 → 报告里必须明说这是"成功响应的 token"）
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0

    # ---------------- 安全：任何输出都不得带密钥 ----------------
    @property
    def masked_api_key(self) -> str:
        """只用于确认「配没配」，永远不暴露真实值。"""
        return f"sk-***({len(self._api_key)} chars)"

    def __repr__(self) -> str:
        return (
            f"DeepSeekClient(model={self.model!r}, base_url={self.base_url!r}, "
            f"api_key=sk-***, calls={self.api_calls})"
        )

    # ---------------- 主流程 ----------------
    def chat_json(self, *, system: str, user: str) -> LLMResponse:
        """发一次单轮对话请求，返回模型的原始 JSON 文本。

        Raises:
            LLMConfigError / LLMAuthError / LLMRequestError / LLMRateLimitError /
            LLMServerError / LLMTimeoutError / LLMResponseError
        """
        if self.api_calls >= self.max_api_calls:
            raise LLMError(
                f"已达到本次运行的 API 调用上限（max_api_calls={self.max_api_calls}）："
                "如需继续请显式提高上限（防止脚本悄悄烧钱）"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            # 显式关闭思考模式：新一代模型默认开启，抽取任务不需要 CoT
            "thinking": {"type": self.thinking_mode},
        }
        last_error: LLMError | None = None
        for attempt in range(1, self.max_retries + 2):  # 首次 + 重试
            self.limiter.acquire()
            self.api_calls += 1
            started = self._clock()
            try:
                response = self._post(payload)
            except httpx.TimeoutException as exc:
                self.timeout_hits += 1
                last_error = LLMTimeoutError(
                    f"请求超时（attempt={attempt}，{type(exc).__name__}）"
                )
            except httpx.HTTPError as exc:  # 连接失败 / 协议错误等
                last_error = LLMError(f"传输失败（attempt={attempt}，{type(exc).__name__}）")
            else:
                status = response.status_code
                if status == 200:
                    return self._parse_response(response, attempt=attempt, started=started)
                if status in {401, 403}:
                    raise LLMAuthError(
                        f"鉴权失败（HTTP {status}）："
                        "请检查 .env 的 DEEPSEEK_API_KEY 是否有效/已轮换"
                    )
                if status == 429:
                    self.rate_limit_hits += 1
                    last_error = LLMRateLimitError(f"触发限流（HTTP 429，attempt={attempt}）")
                    if attempt <= self.max_retries:
                        self.retries += 1
                        self._sleep(self._retry_after_seconds(response))
                        continue
                    raise last_error
                if status >= 500:
                    self.server_error_hits += 1
                    last_error = LLMServerError(
                        f"服务端错误（HTTP {status}，attempt={attempt}）"
                    )
                else:
                    raise LLMRequestError(
                        "请求被拒（HTTP "
                        f"{status}）：{redact(self._error_message(response), secret=self._api_key)}"
                    )
            if attempt <= self.max_retries:
                self.retries += 1
                self._sleep(self._backoff_seconds(attempt))
                continue
            raise last_error
        raise LLMError("不可达：重试循环异常退出")  # pragma: no cover

    # ---------------- 内部工具 ----------------
    def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as client:
            return client.post(f"{self.base_url}{CHAT_COMPLETIONS_PATH}", json=dict(payload))

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        """429 的 `Retry-After`（秒）优先；解析不出来则退避。"""
        raw = response.headers.get("retry-after", "").strip()
        try:
            return max(0.0, min(float(raw), BACKOFF_MAX_SECONDS))
        except ValueError:
            return BACKOFF_BASE_SECONDS

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """指数退避（确定性 → 单测可直接断言休眠时长）。"""
        return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return response.text[:200]
        if isinstance(body, Mapping):
            error = body.get("error")
            if isinstance(error, Mapping):
                return str(error.get("message", ""))[:200]
        return str(body)[:200]

    def _parse_response(
        self, response: httpx.Response, *, attempt: int, started: float
    ) -> LLMResponse:
        """解析 200 响应；结构不可用即报 `LLMResponseError`（**不重试**，省 token）。"""
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(f"响应不是合法 JSON：{type(exc).__name__}") from exc
        if not isinstance(body, Mapping):
            raise LLMResponseError("响应不是 JSON 对象")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                redact(f"响应缺少 choices：{str(body)[:200]}", secret=self._api_key)
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("响应 choices[0].message.content 为空")
        usage_raw = body.get("usage")
        usage: Mapping[str, Any] = usage_raw if isinstance(usage_raw, Mapping) else {}
        latency_ms = int(max(0.0, self._clock() - started) * 1000)
        request_id = response.headers.get("x-request-id") or str(body.get("id", ""))
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        cache_miss_tokens = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cache_hit_tokens += cache_hit_tokens
        self.cache_miss_tokens += cache_miss_tokens
        return LLMResponse(
            content=redact(content, secret=self._api_key),
            model=str(body.get("model", self.model)),
            request_id=str(request_id),
            latency_ms=latency_ms,
            attempts=attempt,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )

    def usage_totals(self) -> dict[str, Any]:
        """累计 token 用量 + 费用估算（**只统计成功响应**，失败/重试的 token 拿不到）。"""
        peak_cost = estimate_cost_usd(
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_hit_tokens=self.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens,
            peak=True,
        )
        offpeak_cost = estimate_cost_usd(
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_hit_tokens=self.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens,
            peak=False,
        )
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "estimated_cost_usd_peak": peak_cost,
            "estimated_cost_usd_offpeak": offpeak_cost,
            "pricing_source": PRICING_SOURCE,
        }

    def stats(self) -> dict[str, Any]:
        """调用统计（**不含任何密钥信息**，可安全打印/落盘）。"""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_calls": self.api_calls,
            "retries": self.retries,
            "max_api_calls": self.max_api_calls,
            "thinking_mode": self.thinking_mode,
            "rate_limit_hits": self.rate_limit_hits,
            "server_error_hits": self.server_error_hits,
            "timeout_hits": self.timeout_hits,
            "limiter": self.limiter.stats(),
            "usage": self.usage_totals(),
        }