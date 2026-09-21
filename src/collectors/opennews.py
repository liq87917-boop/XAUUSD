"""OpenNews 新闻搜索采集器：``opennews``。

数据源：``https://ai.6551.io``，主搜索端点 ``POST /open/news_search``。
认证：**所有端点都需要** ``Authorization: Bearer $OPENNEWS_TOKEN``（从 ``.env`` 读取，
变量名 ``OPENNEWS_TOKEN``；``.env`` 已加入 ``.gitignore``，严禁硬编码）。
Token 缺失时直接报错并提示到 https://6551.io/mcp 获取（无免费降级）。
关键词：gold / XAUUSD / Federal Reserve（来自 ``config_json["keywords"]``，可配）。

落库（对齐 04 §14 ``news_events`` + 01 §4.3）：
- ``headline`` = title；``summary`` = content；``published_at`` 解析为 UTC；
- ``source_name`` / ``url`` 存于 ``raw_items.raw_json`` / ``source_url``；
- 逐条写 ``raw_items(item_type=NEWS)`` 原始切片。

时间对齐：``published_at`` 必须解析为 UTC；``effective_at = max(published_at,
collected_at)``（采集时刻 >= 发布时间时即等于 collected_at，符合"以采集时刻为准"）。
无发布时间的记录只落 raw_items，不生成 news_events（不把 collected_at 冒充发布时间）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import NewsEvent, Source
from database.models.enums import RawItemType, SourceType
from src.collectors.base import BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.transport import HttpRequest
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload

_log = get_logger("collectors.opennews")

#: 基础 URL 与搜索端点路径
BASE_URL: Final[str] = "https://ai.6551.io"
SEARCH_PATH: Final[str] = "/open/news_search"
#: Token 环境变量名（.env 读取，已加入 .gitignore）
OPENNEWS_TOKEN_ENV: Final[str] = "OPENNEWS_TOKEN"
#: 默认关键词（可被 sources.config_json["keywords"] 覆盖）
DEFAULT_KEYWORDS: Final[tuple[str, ...]] = ("gold", "XAUUSD", "Federal Reserve")


@dataclass(frozen=True, slots=True)
class OpenNewsItem:
    """一条解析后的新闻搜索结果。"""

    title: str
    published_at: datetime | None
    content: str | None = None
    source_name: str | None = None
    url: str | None = None

    def record_key(self) -> str:
        # 幂等键：以 url 优先（同一 url 只入库一次）；无 url 时用 title 兜底
        return self.url or self.title


def _parse_published_at(value: object) -> datetime | None:
    """把 provider 返回的发布时间解析为 UTC；无法解析返回 None（不冒充）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_news_search(payload: Mapping[str, Any] | None) -> tuple[OpenNewsItem, ...]:
    """把 ``POST /open/news_search`` 的响应 JSON 解析为新闻条目（纯函数）。

    约定响应结构：``{"data": [{"text", "ts", "source", "link", "description",
    "engineType", "newsType", ...}, ...]}``。缺少 ``data`` 字段抛 ``CollectorError``。
    只处理 ``engineType == "news"`` 的条目（market / onchain 等类型不进入 news_events）。
    """
    if not isinstance(payload, Mapping):
        raise CollectorError("OpenNews 响应不是 JSON 对象")
    data = payload.get("data")
    if not isinstance(data, list):
        raise CollectorError("OpenNews 响应缺少 data 列表", details={"payload_keys": list(payload)})
    items: list[OpenNewsItem] = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        # 只处理新闻类；market / onchain 等其他类型不进 news_events
        if entry.get("engineType") != "news":
            continue
        title = (entry.get("text") or "").strip()
        if not title:
            continue
        items.append(
            OpenNewsItem(
                title=title,
                published_at=_parse_published_at(entry.get("ts")),
                content=(entry.get("description") or "").strip() or None,
                source_name=(entry.get("source") or "").strip() or None,
                url=(entry.get("link") or "").strip() or None,
            )
        )
    return tuple(items)


@register_collector
class OpenNewsCollector(BaseCollector):
    """OpenNews 新闻搜索采集器（HTTP POST，transport 可 Mock）。"""

    collector_name: ClassVar[str] = "opennews"
    source_type: ClassVar[SourceType] = SourceType.NEWS

    def __init__(self, source: Source, **kwargs: Any) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})
        configured = config.get("keywords")
        self.keywords: tuple[str, ...] = (
            tuple(str(keyword) for keyword in configured)
            if isinstance(configured, (list, tuple))
            else DEFAULT_KEYWORDS
        )
        self.base_url = (source.base_url or BASE_URL).rstrip("/")

    def _require_token(self) -> str:
        token = os.environ.get(OPENNEWS_TOKEN_ENV, "").strip()
        if not token:
            raise CollectorError(
                f"缺少 {OPENNEWS_TOKEN_ENV}：所有 OpenNews 端点都需要 Authorization: Bearer 头，"
                f"请到 https://6551.io/mcp 获取 Token 并写入 .env"
            )
        return token

    def _build_request(self, keyword: str) -> HttpRequest:
        token = self._require_token()
        headers = {"Authorization": f"Bearer {token}"}
        return HttpRequest(
            url=f"{self.base_url}{SEARCH_PATH}",
            method="POST",
            headers=headers,
            json_body={"keyword": keyword},
        )

    async def _do_fetch(self, cursor: dict[str, Any] | None, window: CollectWindow) -> FetchPage:
        keyword_index = int((cursor or {}).get("keyword_index", 0))
        if keyword_index >= len(self.keywords):
            return FetchPage(next_cursor=None)
        keyword = self.keywords[keyword_index]
        response = await self._request(self._build_request(keyword))
        if not response.ok:
            raise CollectorError(
                f"OpenNews 搜索返回 HTTP {response.status}：keyword={keyword}",
                details={"status": response.status, "keyword": keyword},
            )
        items = parse_news_search(response.json_body)
        payloads = tuple(self._to_payload(item, keyword=keyword) for item in items)
        next_index = keyword_index + 1
        next_cursor = None if next_index >= len(self.keywords) else {"keyword_index": next_index}
        return FetchPage(payloads=payloads, next_cursor=next_cursor, raw_count=len(items))

    def _to_payload(self, item: OpenNewsItem, *, keyword: str) -> RawItemPayload:
        return RawItemPayload(
            source_record_id=item.record_key(),
            item_type=RawItemType.NEWS,
            title=item.title,
            content_text=item.content,
            raw_json={
                "provider": "opennews",
                "keyword": keyword,
                "title": item.title,
                "content": item.content,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "source_name": item.source_name,
                "url": item.url,
                "published_at_tz_ambiguous": False,
            },
            source_url=item.url,
            published_at=item.published_at,
        )

    def _after_persist(self, session: Session, raw_item: Any, payload: RawItemPayload) -> None:
        """``raw_items`` 落库后回填 ``news_events``（对齐 04 §14 字段）。

        无发布时间的记录只落 raw_items，不生成 news_events（不把 collected_at 冒充发布时间）。
        """
        if payload.published_at is None:
            return
        session.add(
            NewsEvent(
                raw_item_id=raw_item.id,
                headline=payload.title,
                summary=payload.content_text,
                event_type=None,
                importance=None,
                sentiment=None,
                event_at=None,
                published_at=payload.published_at,
                effective_at=raw_item.effective_at,
                parser_version="opennews_search@0.1.0",
            )
        )
        session.flush()
