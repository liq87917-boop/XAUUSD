"""凭据擦除与安全摘要（**唯一实现**，禁止各层各写一套）。

对应规则与红线：
- `.clinerules` 第 6 条：禁止把 API Key / Token / 密码写入日志或提交到仓库；
- `.ai/DEVELOPMENT_PROTOCOL.md` 第 5 节不变量：不绕过授权、不泄露凭据；
- `docs/02 §5.2`：Processor 与 Scheduler 的摘要都必须"可审计但不泄密"。

为什么必须唯一实现：
    Scheduler 的 ``job_runs.output_json``、Processor 的 ``processed_items.structured_json``
    以及采集运行摘要都会**写库**。若各自维护一套正则，迟早出现"一条链路擦了、另一条没擦"。
    因此所有"写库前的文本 / 结构化摘要"都必须经过本模块。

本模块提供三件事：
1. :func:`redact_secrets`：把文本中出现的凭据擦成 ``***``（用于 error / warning 摘要）；
2. :func:`is_sensitive_key`：判断键名是否属于**凭据类**键（api_key / token / authorization …）；
3. :func:`sanitize_mapping` / :func:`safe_url` / :func:`safe_text`：把结构化元数据裁剪成
   **白名单形态**——丢弃敏感键、丢弃嵌套对象、截断超长值，并剥离 URL 的
   userinfo / query / fragment（查询串里常见 ``?api_key=...``）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

__all__ = [
    "MAX_AUDIT_VALUE_CHARS",
    "REDACTED",
    "is_sensitive_key",
    "redact_secrets",
    "safe_text",
    "safe_url",
    "sanitize_mapping",
    "sanitize_value",
]

#: 擦除后的占位符（便于人读与断言）
REDACTED: Final[str] = "***"

#: 凭据擦除规则（宁多擦不可漏：Bearer / Authorization / sk- / api_key / token / secret）
#: 注意 ``["']?`` —— 摘要里可能是 JSON（``{"api_key": "..."}``），键名后的引号必须容忍。
#:
#: ⚠️ 顺序有语义：``Bearer <token>`` 必须**先**擦。否则 ``Authorization: Bearer xxxx``
#: 会被 ``authorization`` 规则截成 ``Authorization=*** xxxx``，把真实 token 留在摘要里
#: （2026-09-22 由 ``tests/unit/test_collection_processor.py`` 中的
#: ``test_metadata_is_sanitized_in_audit_summary`` 实测发现并锁定）。
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]+"), r"\1 ***"),
    (re.compile(r"(?i)\b(authorization)\b[\"']?\s*[:=]\s*[^\s,;]+"), r"\1=***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-***"),
    (re.compile(r"(?i)\b(api[_-]?key)\b[\"']?\s*[:=]\s*[^\s,;]+"), r"\1=***"),
    (
        re.compile(
            r"(?i)\b(access[_-]?token|refresh[_-]?token|secret|token)\b[\"']?\s*[:=]\s*[^\s,;]+"
        ),
        r"\1=***",
    ),
)

#: 凭据类键名片段（分词后逐 token 比对，避免误伤 ``author_name``）
_SENSITIVE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "key",
        "keys",
        "passwd",
        "password",
        "pwd",
        "secret",
        "secrets",
        "session",
        "sessions",
        "signature",
        "token",
        "tokens",
    }
)

#: 键名分词（``api_key`` / ``api-key`` / ``apiKey`` 统一成 token 序列）
_KEY_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: 审计摘要中单个字符串值的最大长度（防止异常信息把 JSON 列撑爆）
MAX_AUDIT_VALUE_CHARS: Final[int] = 300


def redact_secrets(text: str) -> str:
    """把文本里的凭据擦成 ``***``（用于错误 / 告警摘要，避免进数据库或日志）。"""
    redacted = text
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def safe_text(value: str, *, max_chars: int = MAX_AUDIT_VALUE_CHARS) -> str:
    """文本 → 擦除凭据并截断（写库 / 打印前的统一入口）。"""
    return redact_secrets(str(value))[:max_chars]


def is_sensitive_key(key: str) -> bool:
    """键名是否属于凭据类键（``api_key`` / ``fred_api_key`` / ``Authorization`` …）。

    实现要点：先按非字母数字字符分词，再逐 token 比对，
    因此 ``author_name``（``author`` / ``name``）**不会**被误判为凭据键。
    """
    tokens = [token for token in _KEY_TOKEN_SPLIT.split(str(key).lower()) if token]
    return any(token in _SENSITIVE_TOKENS for token in tokens)


def _is_url_like_key(key: str) -> bool:
    """键名是否属于 URL 类键（含 ``url`` / ``uri`` / ``link`` 片段，大小写无关）。"""
    lowered = key.lower()
    return "url" in lowered or "uri" in lowered or "link" in lowered


def safe_url(value: str, *, max_chars: int = MAX_AUDIT_VALUE_CHARS) -> str:
    """URL → 仅保留 ``scheme://host/path``（丢弃 userinfo / query / fragment）。

    为什么必须丢 query：数据源 URL 常把 ``api_key`` / ``token`` 放在查询串里
    （FRED / OpenNews 风格接口），整串写进审计摘要等于把密钥落库。
    无法解析为绝对 URL 时退回 :func:`safe_text`（保守擦除）。
    """
    text = str(value).strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.hostname:
        return safe_text(text, max_chars=max_chars)
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"[:max_chars]


def sanitize_value(value: Any, *, max_chars: int = MAX_AUDIT_VALUE_CHARS) -> Any:
    """把单个值裁剪为审计可写形态（标量保留、字符串擦除截断、其余丢弃为 None）。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return safe_text(value, max_chars=max_chars)
    return None


def sanitize_mapping(
    mapping: Mapping[Any, Any] | None,
    *,
    max_entries: int = 32,
    max_value_chars: int = MAX_AUDIT_VALUE_CHARS,
    max_list_items: int = 8,
) -> dict[str, Any]:
    """把元数据裁剪成**白名单形态**的新字典（绝不返回原对象）。

    规则：
    1. 丢弃凭据类键（:func:`is_sensitive_key`）；
    2. 只保留标量；``list`` / ``tuple`` 只保留标量项（最多 ``max_list_items`` 项）；
       嵌套 ``Mapping`` 一律丢弃（防止把完整 source config / HTTP headers 带进审计摘要）；
    3. URL 类键（``url`` / ``uri`` / ``link``）只对**字符串**走 :func:`safe_url`
       （丢弃查询串与 userinfo）；``None`` 原样保留；``bool`` / ``int`` / ``float``
       保留原值而**绝不** ``str()`` 伪造成字符串 URL；其它复杂对象丢弃；
    4. 字符串擦除凭据并截断；最多保留 ``max_entries`` 个键。

    Returns:
        新的 ``dict[str, Any]``；输入为空时返回 ``{}``。
    """
    if not mapping:
        return {}

    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        if len(sanitized) >= max_entries:
            break
        if not isinstance(raw_key, (str, int)):
            continue
        key = str(raw_key).strip()
        if not key or len(key) > 100 or is_sensitive_key(key):
            continue

        if isinstance(raw_value, Mapping):
            continue  # 嵌套结构一律丢弃：无法保证其中没有配置 / 凭据
        if isinstance(raw_value, (list, tuple)):
            items = [
                sanitize_value(item, max_chars=max_value_chars)
                for item in list(raw_value)[:max_list_items]
            ]
            sanitized[key] = [item for item in items if item is not None]
            continue

        if _is_url_like_key(key):
            # ⚠️ 只有**字符串**才允许进入 :func:`safe_url`。历史实现无条件 ``str(value)``，
            # 于是 ``{"feed_url": None}`` 被写成字符串 ``"None"``（把"未知 URL"伪造成
            # "URL 就叫 None"，GOLD-002-R1 已修），而 ``{"is_url": True}`` / ``{"feed_url": 123}``
            # 被写成 ``"True"`` / ``"123"``（非字符串标量被伪造成字符串 URL）。
            # GOLD-003 起采用如下**确定性规则**（不再对非字符串标量做 ``str()``）：
            #   1. ``str`` → :func:`safe_url`（丢弃 userinfo / query / fragment）；
            #   2. ``None`` → 原样保留 ``None``；
            #   3. ``bool`` / ``int`` / ``float`` → 保留原值（诚实审计，绝不字符串化）；
            #   4. 其它对象（复杂类型）→ 丢弃该键（不猜测、不字符串化）。
            if isinstance(raw_value, str):
                sanitized[key] = safe_url(raw_value, max_chars=max_value_chars)
                continue
            scalar = sanitize_value(raw_value, max_chars=max_value_chars)
            if scalar is None and raw_value is not None:
                continue
            sanitized[key] = scalar
            continue

        value = sanitize_value(raw_value, max_chars=max_value_chars)
        if value is None and raw_value is not None:
            continue  # 不支持的复杂对象：丢弃而不是字符串化
        sanitized[key] = value
    return sanitized
