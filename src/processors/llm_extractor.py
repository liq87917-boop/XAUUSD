"""LLM 观点抽取器（DeepSeek，单轮 JSON；**缓存优先 + 同一套契约校验**）。

对应文档与要求：
- `docs/10_标注规范.md` §5（JSON Schema）与 §4.9（2026-09-13 人工裁决口径）；
- `docs/02` §5.2：抽取器必须可追溯（`parser_version` 入库）；
- 团队要求（2026-09-13）：缓存优先、预算门禁、失败不中断、密钥不外泄。

设计要点：
1. **复用契约校验**：模型输出交给 `build_drafts()`（`extra="forbid"` + 数值范围），
   非法候选变成 `INVALID_DRAFT` 诊断，绝不写脏数据进库。
2. **缓存即事实**：同一 `(prompt_version, model, text, has_media)` 只付费一次；
   `readonly` 模式零网络零成本，用于复跑与 CI（未命中会显式报错，不偷偷联网）。
3. **失败降级**：单帖的 429 / 5xx / 超时 / 解析失败**不抛出**，转为 `LLM_API_ERROR` /
   `LLM_PARSE_ERROR` 诊断 + 警告，保证 200 条批量评估不因一条失败而全废；
   但**配置错误（无 key）与鉴权失败（401/403）直接抛出**——那是系统性故障，
   静默降级只会产出 200 条"看起来很差"的假结果。
4. **不信任模型**：模型自带的 `parser_version` / 时间 / 归属字段一律剔除，
   多余键剔除并记警告（避免一个越界字段毁掉整条候选）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from config.settings import Settings, get_settings
from src.processors.llm_cache import (
    STATUS_ERROR,
    STATUS_OK,
    LLMCache,
    LLMCacheEntry,
    cache_key,
    text_digest,
)
from src.processors.llm_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_THINKING_MODE,
    DEFAULT_TIMEOUT_SECONDS,
    DeepSeekClient,
    LLMAuthError,
    LLMConfigError,
    LLMError,
    estimate_cost_usd,
)
from src.processors.opinion_extractor import (
    DiagnosticCode,
    ExtractionDiagnostic,
    OpinionExtractionResult,
    build_drafts,
)
from src.processors.prompt_opinion import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_message,
    prepare_text,
)

__all__ = ["DEFAULT_CACHE_DIR", "DEFAULT_PARSER_VERSION", "LLMOpinionExtractor"]

#: 入库版本号（`author_opinions.parser_version`）；换模型 / 换 Prompt 必须换版本
#: v6：配合 `opinion-prompt-v6`（few-shot 去污染 + 决策阶梯 L1~L6 + `no_opinion` 语义
#: + 观望→FLAT / 情绪≠FLAT + **L3 操作价位优先于情绪** + 「短线思路→15m」周期补丁）
DEFAULT_PARSER_VERSION: Final[str] = "llm-deepseek-v6"
#: 默认缓存目录（仓库根 `logs/llm_cache`）
DEFAULT_CACHE_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "logs" / "llm_cache"
#: 允许模型输出的键（其余一律剔除并记警告）
_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "stance",
        "instrument",
        "horizon",
        "confidence",
        "entry_low",
        "entry_high",
        "stop_loss",
        "take_profit",
        "information_type",
        "rationale",
    }
)
#: 模型绝对不许自填的键（时间 / 归属 / 解析器版本由系统注入）
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {"parser_version", "effective_at", "author_id", "author_post_id", "collected_at"}
)


class LLMOpinionExtractor:
    """DeepSeek 观点抽取器（实现 `OpinionExtractor` 协议）。

    Args:
        client: 已构造的客户端（单测注入带 Mock transport 的客户端）；缺省则**延迟**
            从 `Settings` 构造——`readonly` 纯缓存复跑因此**不需要 API Key**。
        cache: 缓存实例；缺省按 `cache_dir` + `cache_mode` 构造。
        cache_dir: 缓存目录（默认 `logs/llm_cache`）。
        cache_mode: `auto` / `readonly` / `refresh` / `off`。
        prompt_version: 参与缓存键；默认取 Prompt 模块版本。
        parser_version: 入库版本号。
        model / settings / base_url: 覆盖默认模型、配置或服务地址。
        max_api_calls: 预算门禁（委托给客户端；重试也计入）。
        max_retries / rate_limit_per_minute / thinking_mode: 委托给客户端的调用策略；
            `thinking_mode` 默认 `disabled`（抽取任务不需要 CoT，且新模型默认开启会变贵）。
    """

    def __init__(
        self,
        *,
        client: DeepSeekClient | None = None,
        cache: LLMCache | None = None,
        cache_dir: str | Path | None = None,
        cache_mode: str = "auto",
        prompt_version: str = PROMPT_VERSION,
        parser_version: str = DEFAULT_PARSER_VERSION,
        model: str | None = None,
        settings: Settings | None = None,
        max_api_calls: int = DEFAULT_MAX_API_CALLS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        thinking_mode: str = DEFAULT_THINKING_MODE,
        base_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self.prompt_version = prompt_version
        self.parser_version = parser_version
        resolved_model = model or (client.model if client is not None else None)
        self.model = resolved_model or DEFAULT_MODEL
        self.max_api_calls = max_api_calls
        self.max_retries = max_retries
        self.rate_limit_per_minute = rate_limit_per_minute
        self.thinking_mode = thinking_mode
        self.base_url = base_url
        self.cache = cache if cache is not None else LLMCache(
            Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR, mode=cache_mode
        )
        self.api_failures = 0
        self.parse_failures = 0
        self.api_posts = 0
        self.cache_posts = 0
        self.posts = 0
        # Token 台账：`api` = 本次真实付费（新调用），`cached` = 复用历史缓存（¥0）
        self.api_usage: dict[str, int] = _zero_usage()
        self.cached_usage: dict[str, int] = _zero_usage()

    # ---------------- 客户端（延迟构造：readonly 复跑无需 Key） ----------------
    def _ensure_client(self) -> DeepSeekClient:
        if self._client is None:
            settings = self._settings or get_settings()
            if not settings.deepseek_configured:
                raise LLMConfigError(
                    "缺少 DEEPSEEK_API_KEY，无法调用 LLM；"
                    "若只想用缓存复跑请使用 --llm-cache-mode readonly"
                )
            secret = settings.deepseek_api_key
            assert secret is not None  # noqa: S101 - 上面已校验过
            self._client = DeepSeekClient(
                secret.get_secret_value(),
                base_url=self.base_url or settings.deepseek_base_url or DEFAULT_BASE_URL,
                model=self.model,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                max_retries=self.max_retries,
                rate_limit_per_minute=self.rate_limit_per_minute,
                max_api_calls=self.max_api_calls,
                thinking_mode=self.thinking_mode,
            )
        return self._client

    # ---------------- 主流程 ----------------
    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        """抽取一条帖子的观点（缓存优先；单帖失败降级为诊断，不中断整批）。"""
        self.posts += 1
        cleaned, truncated = prepare_text(text)
        if not cleaned:
            return OpinionExtractionResult(
                parser_version=self.parser_version,
                diagnostics=(
                    ExtractionDiagnostic(
                        code=DiagnosticCode.EMPTY_TEXT, message="空文本，无观点可抽取"
                    ),
                ),
            )
        key = cache_key(
            prompt_version=self.prompt_version,
            model=self.model,
            text=cleaned,
            has_media=has_media,
        )
        entry = self.cache.load(key)
        if entry is not None:
            if not entry.ok:
                return self._failure_result(
                    code=DiagnosticCode.LLM_API_ERROR,
                    detail=entry.error or "缓存中记录的失败结果",
                    text=cleaned,
                    source="cache-error",
                )
            self.cache_posts += 1
            _add_usage(self.cached_usage, entry.usage)
            return self._build_result(
                entry.raw_content,
                source="cache",
                text=cleaned,
                attempts=entry.attempts,
            )
        if self.cache.mode == "readonly":
            # 显式失败：readonly 复跑绝不允许"静默联网"烧钱
            raise LLMError(
                "readonly 模式下缓存未命中（这条帖子还没真实跑过）："
                f"cache={self.cache.path_for(key)}；如需联网请改用 cache_mode=auto"
            )
        warnings: list[str] = ["llm-source=api"]
        if truncated:
            warnings.append("正文超长已截断（见 prompt_opinion.MAX_TEXT_CHARS）")
        try:
            client = self._ensure_client()
            response = client.chat_json(
                system=SYSTEM_PROMPT,
                user=build_user_message(cleaned, has_media=has_media),
            )
        except (LLMConfigError, LLMAuthError):
            raise  # 系统性故障：必须让调用方立刻看到，不能静默降级
        except LLMError as exc:
            self.api_failures += 1
            self._store(
                key=key,
                status=STATUS_ERROR,
                error=str(exc),
                text=cleaned,
                has_media=has_media,
            )
            return self._failure_result(
                code=DiagnosticCode.LLM_API_ERROR,
                detail=str(exc),
                text=cleaned,
                source="api-error",
                warnings=tuple(warnings),
            )
        self.api_posts += 1
        _add_usage(
            self.api_usage,
            {
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cache_hit_tokens": response.cache_hit_tokens,
                "cache_miss_tokens": response.cache_miss_tokens,
            },
        )
        self._store(
            key=key,
            status=STATUS_OK,
            raw_content=response.content,
            text=cleaned,
            has_media=has_media,
            attempts=response.attempts,
            latency_ms=response.latency_ms,
            usage={
                "model": response.model,
                "request_id": response.request_id,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cache_hit_tokens": response.cache_hit_tokens,
                "cache_miss_tokens": response.cache_miss_tokens,
            },
        )
        return self._build_result(
            response.content,
            source="api",
            text=cleaned,
            attempts=response.attempts,
            warnings=tuple(warnings),
        )

    # ---------------- 解析 + 校验 ----------------
    def _build_result(
        self,
        content: str,
        *,
        source: str,
        text: str,
        attempts: int = 0,
        warnings: tuple[str, ...] = (),
    ) -> OpinionExtractionResult:
        """把模型原始 JSON 文本转成 `OpinionExtractionResult`（校验由 `build_drafts` 兜底）。"""
        merged = list(warnings)
        if f"llm-source={source}" not in merged:
            merged.insert(0, f"llm-source={source}")
        if attempts > 1:
            merged.append(f"llm-attempts={attempts}")
        candidates, issues = _to_candidates(content)
        if candidates is None:
            self.parse_failures += 1
            return self._failure_result(
                code=DiagnosticCode.LLM_PARSE_ERROR,
                detail=issues[0],
                text=text,
                source=source,
                warnings=tuple(merged) + tuple(issues[1:]),
            )
        sanitized, dropped = _sanitize_candidates(candidates)
        drafts, diagnostics = build_drafts(sanitized, parser_version=self.parser_version)
        return OpinionExtractionResult(
            parser_version=self.parser_version,
            drafts=drafts,
            diagnostics=diagnostics + dropped,
            warnings=tuple(merged) + tuple(issues),
        )

    def _failure_result(
        self,
        *,
        code: DiagnosticCode,
        detail: str,
        text: str,
        source: str,
        warnings: tuple[str, ...] = (),
    ) -> OpinionExtractionResult:
        """失败降级结果：0 观点 + 诊断（不抛异常，保证批量评估继续跑完）。"""
        merged = list(warnings)
        if f"llm-source={source}" not in merged:
            merged.append(f"llm-source={source}")
        return OpinionExtractionResult(
            parser_version=self.parser_version,
            diagnostics=(
                ExtractionDiagnostic(code=code, message=detail, snippet=text[:200] or None),
            ),
            warnings=tuple(merged),
        )

    def _store(
        self,
        *,
        key: str,
        status: str,
        text: str,
        has_media: bool,
        raw_content: str = "",
        error: str = "",
        attempts: int = 0,
        latency_ms: int = 0,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """落盘一条缓存（`readonly` / `off` 模式自动跳过）。"""
        self.cache.store(
            LLMCacheEntry(
                key=key,
                prompt_version=self.prompt_version,
                model=self.model,
                text_sha256=text_digest(text),
                has_media=has_media,
                created_at=LLMCache.now_iso(),
                status=status,
                raw_content=raw_content,
                error=error,
                usage=usage,
                latency_ms=latency_ms,
                attempts=attempts,
            )
        )

    def stats(self) -> dict[str, Any]:
        """运行统计（不含密钥；可安全打印 / 写进报告）。

        `usage` 是**费用台账**：
        - `api` = 本次真实调用的 token（真正花钱的部分，含价格估算的峰/谷两档）；
        - `cached` = 复用历史缓存的 token（**本次 ¥0**，但仍要披露"这套 200 条总共烧了多少"）；
        - 只统计**成功响应**：失败请求与重试尝试服务端同样计费，但响应里拿不到 token 数。
        """
        api_cost_peak, api_cost_offpeak = self._cost(self.api_usage)
        full_peak, full_offpeak = self._cost(
            {
                name: self.api_usage[name] + self.cached_usage[name]
                for name in self.api_usage
            }
        )
        return {
            "parser_version": self.parser_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "thinking_mode": self.thinking_mode,
            "posts": self.posts,
            "posts_via_api": self.api_posts,
            "posts_via_cache": self.cache_posts,
            "api_failures": self.api_failures,
            "parse_failures": self.parse_failures,
            "cache": self.cache.stats(),
            "usage": {
                "api": dict(self.api_usage),
                "cached": dict(self.cached_usage),
                "api_estimated_cost_usd_peak": api_cost_peak,
                "api_estimated_cost_usd_offpeak": api_cost_offpeak,
                "full_set_estimated_cost_usd_peak": full_peak,
                "full_set_estimated_cost_usd_offpeak": full_offpeak,
            },
            "client": self._client.stats() if self._client is not None else None,
        }

    def _cost(self, usage: Mapping[str, int]) -> tuple[float | None, float | None]:
        """按官方价目表估算 `(峰值价, 低谷价)`（USD）；模型不在价目表则 `(None, None)`。"""
        peak = estimate_cost_usd(
            model=self.model,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cache_hit_tokens=usage["cache_hit_tokens"],
            cache_miss_tokens=usage["cache_miss_tokens"],
            peak=True,
        )
        offpeak = estimate_cost_usd(
            model=self.model,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cache_hit_tokens=usage["cache_hit_tokens"],
            cache_miss_tokens=usage["cache_miss_tokens"],
            peak=False,
        )
        return peak, offpeak


def _zero_usage() -> dict[str, int]:
    """一条空的 token 台账（`posts` 在累加时 +1）。"""
    return {
        "posts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
    }


def _add_usage(target: dict[str, int], usage: Mapping[str, Any] | None) -> None:
    """把一次调用的 usage 累加进台账（**容忍缺字段**：缓存里的旧记录可能没有 cache 分项）。"""
    if not usage:
        return
    target["posts"] += 1
    for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens"):
        value = usage.get(key, 0)
        if isinstance(value, (int, float)):
            target[key] += int(value)


def _strip_code_fence(text: str) -> str:
    """去掉模型可能加的 markdown 代码块包裹。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.strip("`").strip()
    if body[:4].lower() == "json":
        body = body[4:]
    return body.strip()


def _to_candidates(content: str) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """解析模型 JSON：返回 `(候选列表, 警告)`；`None` = 结构不可用（记解析失败）。

    容错但不放水：允许顶层是对象或数组、允许代码块包裹；
    但 `opinions` 必须是数组、元素必须是对象，否则视为解析失败。
    """
    try:
        payload = json.loads(_strip_code_fence(content))
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"模型输出不是合法 JSON（{type(exc).__name__}）"]
    warnings: list[str] = []
    opinions: Any
    if isinstance(payload, list):
        opinions = payload
        warnings.append("模型输出为顶层数组（期望对象），已按 opinions 处理")
    elif isinstance(payload, Mapping):
        opinions = payload.get("opinions")
        unknown = sorted(set(payload) - {"opinions", "no_opinion"})
        if unknown:
            warnings.append(f"忽略未知顶层键：{','.join(unknown)}")
    else:
        return None, ["模型输出既不是对象也不是数组"]
    if opinions is None:
        return None, ["模型输出缺少 opinions 字段"]
    if not isinstance(opinions, list):
        return None, ["模型输出的 opinions 不是数组"]
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(opinions):
        if not isinstance(item, Mapping):
            warnings.append(f"第 {index + 1} 条候选不是 JSON 对象，已跳过")
            continue
        candidates.append(dict(item))
    return candidates, warnings


def _sanitize_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[ExtractionDiagnostic, ...]]:
    """剔除越界键（模型幻觉字段），返回 `(净化后的候选, 诊断)`。

    为什么必须真剔除而不是只报错：`AuthorOpinionDraft` 是 `extra="forbid"`，
    模型多写一个 `"time": null` 就会让**整条观点**被拒——
    我们要的是"记录模型越界行为 + 保住它真正的观点内容"。
    """
    sanitized: list[dict[str, Any]] = []
    diagnostics: list[ExtractionDiagnostic] = []
    for index, candidate in enumerate(candidates):
        extra_keys = sorted(set(candidate) - _ALLOWED_KEYS)
        forbidden = sorted(set(candidate) & _FORBIDDEN_KEYS)
        if extra_keys:
            detail = "、".join(extra_keys)
            if forbidden:
                detail += f"（其中 {('、'.join(forbidden))} 由系统注入，模型不得自填）"
            diagnostics.append(
                ExtractionDiagnostic(
                    code=DiagnosticCode.INVALID_DRAFT,
                    message=f"第 {index + 1} 条候选含越界键：{detail}；已剔除后继续校验",
                    snippet=str(candidate)[:200],
                )
            )
        sanitized.append({k: v for k, v in candidate.items() if k in _ALLOWED_KEYS})
    return sanitized, tuple(diagnostics)