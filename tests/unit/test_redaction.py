"""凭据擦除与安全摘要单元测试（`src/common/redaction.py`）。

覆盖：凭据文本擦除、键名判定（不误伤 ``author_name``）、URL 去 query/userinfo、
结构化元数据裁剪（丢弃嵌套 / 截断 / 丢弃凭据键）。**不联网、不依赖数据库。**
"""

from __future__ import annotations

import json

import pytest

from src.common.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_secrets,
    safe_text,
    safe_url,
    sanitize_mapping,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer sk-abcdef123456", "abcdef123456"),
        ("api_key=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ("token: SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ('{"api_key": "SUPERSECRETVALUE"}', "SUPERSECRETVALUE"),
        ("refresh_token=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ("secret=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
    ],
)
def test_redact_secrets_removes_credentials(text: str, secret: str) -> None:
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert REDACTED in redacted


def test_redact_secrets_keeps_plain_text() -> None:
    plain = "connect timeout after 3 attempts"
    assert redact_secrets(plain) == plain


def test_safe_text_redacts_and_truncates() -> None:
    assert safe_text("api_key=SECRET", max_chars=100) == "api_key=***"
    assert safe_text("x" * 50, max_chars=10) == "x" * 10


@pytest.mark.parametrize(
    "key",
    ["api_key", "apiKey", "api-key", "fred_api_key", "Authorization", "access_token", "Password"],
)
def test_is_sensitive_key_detects_credentials(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["author_name", "language", "source_type", "has_media", "published_raw", "categories"],
)
def test_is_sensitive_key_does_not_flag_business_fields(key: str) -> None:
    assert is_sensitive_key(key) is False


def test_safe_url_drops_credentials_and_query() -> None:
    url = "https://user:pass@api.example.invalid/v1/news?api_key=SECRETVALUE&page=2#frag"
    assert safe_url(url) == "https://api.example.invalid/v1/news"


def test_safe_url_keeps_port_and_handles_non_url() -> None:
    assert safe_url("https://api.example.invalid:8443/feed.xml") == (
        "https://api.example.invalid:8443/feed.xml"
    )
    # 非绝对 URL：保守擦除（宁可少记，不可泄露）
    assert "SECRETVALUE" not in safe_url("api_key=SECRETVALUE")


def test_sanitize_mapping_drops_credentials_nested_and_url_query() -> None:
    raw = {
        "api_key": "SECRETVALUE",
        "authorization": "Bearer SECRETVALUE",
        "feed_url": "https://feeds.example.invalid/rss.xml?token=SECRETVALUE",
        "source_name": "金十数据",
        "language": "zh",
        "has_media": False,
        "categories": ["gold", "macro"],
        "headers": {"Authorization": "Bearer SECRETVALUE"},
        "note": "token=SECRETVALUE 被写入",
    }

    sanitized = sanitize_mapping(raw)

    assert "api_key" not in sanitized
    assert "authorization" not in sanitized
    assert "headers" not in sanitized  # 嵌套结构一律丢弃
    assert sanitized["feed_url"] == "https://feeds.example.invalid/rss.xml"
    assert sanitized["source_name"] == "金十数据"
    assert sanitized["language"] == "zh"
    assert sanitized["has_media"] is False
    assert sanitized["categories"] == ["gold", "macro"]
    assert "SECRETVALUE" not in str(sanitized)
    assert REDACTED in str(sanitized)


def test_sanitize_mapping_keeps_null_url_values_as_null() -> None:
    """URL 类键的空值必须保持 ``None``（不得被字符串化成 ``"None"``）。

    回归背景（GOLD-002-R1 复核发现）：``sanitize_mapping`` 对 URL 类键无条件调用
    ``safe_url(str(value))``，于是 ``{"feed_url": None}`` 会被写成 ``"None"``——
    审计摘要凭空造出一个"看起来像值"的字符串，与"缺失即缺失"的口径冲突。
    """

    sanitized = sanitize_mapping({"feed_url": None, "url": None, "source_name": "金十数据"})

    assert sanitized["feed_url"] is None
    assert sanitized["url"] is None
    assert sanitized["source_name"] == "金十数据"
    # 落库形态（JSON）里必须是 null，而不是字符串 "None"
    payload = json.dumps(sanitized, ensure_ascii=False)
    assert '"feed_url": null' in payload
    assert "None" not in payload


def test_sanitize_mapping_does_not_stringify_non_string_url_values() -> None:
    """URL 类键的**非字符串标量**绝不能被 ``str()`` 伪造成字符串 URL。

    回归背景（GOLD-003）：``sanitize_mapping`` 早期对 URL 类键无条件 ``safe_url(str(value))``，
    于是 ``{"is_url": True}`` → ``"True"``、``{"feed_url": 123}`` → ``"123"``——
    审计摘要里凭空多出"看起来像 URL 的字符串"，与"只记录真实值"的口径冲突。
    本测试锁定确定性行为：``str`` 走 :func:`safe_url`（去 query/userinfo）；
    ``None`` 保持 ``None``；``bool`` / ``int`` / ``float`` 保留原值（不字符串化）；
    复杂对象直接丢弃。
    """

    sanitized = sanitize_mapping(
        {
            "feed_url": 123,
            "is_url": True,
            "uri_length": 4.5,
            "url": None,
            "source_url": "https://feeds.example.invalid/rss.xml?token=SECRETVALUE",
            "callback_object": object(),
        }
    )

    assert sanitized["feed_url"] == 123
    assert isinstance(sanitized["feed_url"], str) is False
    assert sanitized["is_url"] is True
    assert sanitized["uri_length"] == 4.5
    assert sanitized["url"] is None
    assert sanitized["source_url"] == "https://feeds.example.invalid/rss.xml"
    assert "callback_object" not in sanitized  # 复杂对象丢弃，绝不字符串化

    payload = json.dumps(sanitized, ensure_ascii=False)
    assert '"feed_url": 123' in payload
    assert '"is_url": true' in payload
    assert '"123"' not in payload
    assert '"True"' not in payload
    assert "SECRETVALUE" not in payload


def test_sanitize_mapping_is_bounded_and_returns_new_object() -> None:
    raw = {f"k{index}": index for index in range(50)}
    sanitized = sanitize_mapping(raw, max_entries=3)
    assert len(sanitized) == 3
    assert sanitized is not raw

    long_value = sanitize_mapping({"title": "x" * 500}, max_value_chars=10)
    assert long_value["title"] == "x" * 10

    assert sanitize_mapping(None) == {}
    assert sanitize_mapping({}) == {}
