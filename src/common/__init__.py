"""GOLD-AI 通用基础能力（时间、主键、哈希、异常）。"""

from src.common.exceptions import (
    ConfigSecurityError,
    GoldAIError,
    ImmutableRecordError,
    SeedError,
    TimeSemanticsError,
)
from src.common.hashing import content_hash, normalize_for_hash, sha256_bytes, sha256_text
from src.common.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_secrets,
    safe_text,
    safe_url,
    sanitize_mapping,
)
from src.common.time import (
    UTC_TZ,
    is_future,
    parse_iso8601,
    require_aware,
    resolve_effective_at,
    resolve_timezone,
    to_naive_utc,
    to_utc,
    utc_now,
)
from src.common.uuid7 import uuid7, uuid7_from_datetime, uuid7_timestamp_ms

__all__ = [
    "ContentHash",
    "ConfigSecurityError",
    "GoldAIError",
    "ImmutableRecordError",
    "REDACTED",
    "SeedError",
    "TimeSemanticsError",
    "UTC_TZ",
    "content_hash",
    "is_future",
    "is_sensitive_key",
    "normalize_for_hash",
    "parse_iso8601",
    "redact_secrets",
    "require_aware",
    "resolve_effective_at",
    "resolve_timezone",
    "safe_text",
    "safe_url",
    "sanitize_mapping",
    "sha256_bytes",
    "sha256_text",
    "to_naive_utc",
    "to_utc",
    "utc_now",
    "uuid7",
    "uuid7_from_datetime",
    "uuid7_timestamp_ms",
]


class ContentHash(str):
    """SHA256 内容哈希（64 位十六进制小写）。"""

    __slots__ = ()

    def __new__(cls, value: str) -> "ContentHash":
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError(f"非法 SHA256 十六进制哈希：{value!r}")
        return super().__new__(cls, normalized)
