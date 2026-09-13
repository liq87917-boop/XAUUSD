"""新闻采集器：``news_collector``（Phase 1 第 3 步第 2 个具体采集器）。

数据源（团队批复）：
    只使用**公开 RSS / Atom 源**（例如美联储官方新闻稿 feed），通过
    ``sources.config_json["feeds"]`` 配置；**代码中不写死任何站点地址**，也绝不抓取
    未授权站点。微博文本分析属 Phase 2，当前完全不做。
    离线兜底：``config_json["csv_path"]`` 指向本地 CSV 时改走 CSV 导入
    （团队批复允许的 Mock 解析器，用于无可用公开 feed 的环境与框架验证）。

时间对齐（团队批复，最重要的一条）：
    - 所有时间**强制解析为 UTC**（支持 RFC822 ``Tue, 10 Sep 2026 08:00:00 GMT``
      与 ISO8601 ``2026-09-10T08:00:00Z`` / ``+08:00``）；
    - feed 给不出明确时区（naive 时间戳）时**绝不猜测**：打印 WARNING，把
      ``published_at`` 置空，使 ``effective_at = collected_at``，原始字符串保留在
      ``raw_json["published_at_raw"]`` 供审计。

幂等（团队批复）：
    - **全局 content_hash 去重**：同一篇新闻被多个 RSS 源转载时只落库一次
      （基类的去重是"本来源内"的，本采集器在此之前再做一次跨来源检查）；
    - 落库表严格遵循 04 §14 ``news_events``；时区不明确 / 无发布时间的记录只落
      ``raw_items``（不生成 news_events），并计入数据质量告警 —— 绝不把
      ``collected_at`` 冒充成发布时间写进结构化表。

⚠️ 生产 / 研究环境必须使用 PostgreSQL；SQLite 仅用于本地开发与自动化测试。
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET  # noqa: S405 - 已拒绝 DOCTYPE 且限制响应大小
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import NewsEvent, RawItem
from database.models.enums import RawItemType, SourceType
from src.collectors.base import PERSIST_DUPLICATE, BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.registry import register_collector
from src.collectors.transport import HttpRequest
from src.collectors.types import (
    CollectorHealth,
    CollectOutcome,
    CollectWindow,
    FetchPage,
    RawItemPayload,
)
from src.common.hashing import content_hash
from src.common.time import to_utc

__all__ = [
    "CSV_PARSER_VERSION",
    "NEWS_PARSER_VERSION",
    "FeedItem",
    "NewsCollector",
    "ParsedTimestamp",
    "parse_feed_timestamp",
    "parse_news_csv",
    "parse_rss_feed",
    "strip_html",
]

_log = get_logger("collectors.news")

#: 解析器版本（06_Cline开发规则 第 13 条：加工结果必须可追溯到解析版本）
NEWS_PARSER_VERSION: Final[str] = "news_rss_parser@0.1.0"
CSV_PARSER_VERSION: Final[str] = "news_csv_parser@0.1.0"

#: 单次响应大小上限（防御异常大响应 / 内存放大）
MAX_FEED_BYTES: Final[int] = 5 * 1024 * 1024

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: RSS 请求头：优先声明我们所支持的 feed 类型
FEED_ACCEPT_HEADER: Final[str] = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
)


@dataclass(frozen=True, slots=True)
class ParsedTimestamp:
    """feed 时间字段的解析结果。"""

    value: datetime | None
    tz_ambiguous: bool
    raw: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FeedItem:
    """一条解析后的新闻（与 04 §5 raw_items / §14 news_events 对齐的中间结构）。"""

    record_id: str
    title: str | None = None
    summary: str | None = None
    link: str | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)
    published: ParsedTimestamp = field(default_factory=lambda: ParsedTimestamp(None, False))


def strip_html(text: str | None) -> str | None:
    """去掉 feed 摘要里的 HTML 标签并压缩空白（RSS description 常含 HTML）。"""
    if text is None:
        return None
    plain = _TAG_RE.sub(" ", text)
    plain = _WHITESPACE_RE.sub(" ", plain).strip()
    return plain or None


def parse_feed_timestamp(raw: str | None) -> ParsedTimestamp:
    """把 RSS/Atom 时间字符串解析为 UTC。

    规则（团队批复）：
    - 带明确时区（``GMT`` / ``+0800`` / ``Z`` / ``+08:00``）→ 统一转换为 UTC；
    - 时区不明确（naive）→ **不猜测**，``tz_ambiguous=True``，调用方据此把
      ``published_at`` 置空，令 ``effective_at = collected_at``；
    - 无法解析 → ``value=None`` 且带上 ``reason``（由采集器计入告警，不静默）。
    """
    if raw is None or not str(raw).strip():
        return ParsedTimestamp(value=None, tz_ambiguous=False, raw=raw, reason="empty")

    text = str(raw).strip()

    # 1) RFC 822（RSS 2.0 常见）
    try:
        rfc822 = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        rfc822 = None
    if rfc822 is not None:
        if rfc822.tzinfo is None or rfc822.utcoffset() is None:
            return ParsedTimestamp(
                value=None, tz_ambiguous=True, raw=text, reason="rfc822_without_timezone"
            )
        return ParsedTimestamp(
            value=to_utc(rfc822, assume_tz=None, field_name="published_at"),
            tz_ambiguous=False,
            raw=text,
        )

    # 2) ISO 8601（Atom 常见；兼容末尾 Z）
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        iso = datetime.fromisoformat(iso_text)
    except ValueError:
        return ParsedTimestamp(value=None, tz_ambiguous=False, raw=text, reason="unparsable")
    if iso.tzinfo is None or iso.utcoffset() is None:
        return ParsedTimestamp(
            value=None, tz_ambiguous=True, raw=text, reason="iso8601_without_timezone"
        )
    return ParsedTimestamp(
        value=to_utc(iso, assume_tz=None, field_name="published_at"), tz_ambiguous=False, raw=text
    )


def _local_name(tag: object) -> str:
    """去掉 XML 命名空间前缀（Atom 常带 ``http://www.w3.org/2005/Atom``）。"""
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _child_text(node: ET.Element, name: str) -> str | None:
    """取直接子节点的文本（按 local name 匹配，自动去命名空间）。"""
    for child in node:
        if _local_name(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _element_text(node: ET.Element) -> str | None:
    """取元素及其子元素的全部文本（Atom 的 ``<content>`` 可能嵌套 HTML）。"""
    text = "".join(node.itertext()).strip()
    return text or None


def _link_of(node: ET.Element) -> str | None:
    """取链接：RSS = ``<link>url</link>``；Atom = ``<link href="url"/>``。"""
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            return href.strip() or None
        text = (child.text or "").strip()
        if text:
            return text
    return None


def _content_of(node: ET.Element) -> str | None:
    for child in node:
        if _local_name(child.tag) in {"description", "summary", "content", "encoded"}:
            text = _element_text(child)
            if text:
                return text
    return None


def _categories_of(node: ET.Element) -> tuple[str, ...]:
    values: list[str] = []
    for child in node:
        if _local_name(child.tag) != "category":
            continue
        text = (child.text or "").strip() or (child.get("term") or "").strip()
        if text:
            values.append(text)
    return tuple(dict.fromkeys(values))  # 去重且保持顺序


def _feed_item_of(node: ET.Element) -> FeedItem:
    """由 ``<item>`` / ``<entry>`` 构造 :class:`FeedItem`。"""
    title = strip_html(_child_text(node, "title"))
    content = strip_html(_content_of(node))
    link = _link_of(node)
    published_raw = (
        _child_text(node, "pubDate")
        or _child_text(node, "published")
        or _child_text(node, "updated")
        or _child_text(node, "date")
        or _child_text(node, "created")
    )
    record_id = (
        _child_text(node, "guid")
        or _child_text(node, "id")
        or link
        or content_hash(title, content)
    )
    return FeedItem(
        record_id=record_id,
        title=title,
        summary=content,
        link=link,
        categories=_categories_of(node),
        published=parse_feed_timestamp(published_raw),
    )


def parse_rss_feed(xml_text: str) -> tuple[FeedItem, ...]:
    """解析 RSS 2.0 / RSS 1.0(RDF) / Atom feed（仅用标准库，不引入额外依赖）。

    Raises:
        CollectorError: 响应为空 / 过大 / 含 DOCTYPE / XML 语法错误 / 根元素不受支持。
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise CollectorError("feed 响应为空，无法解析")
    if len(xml_text) > MAX_FEED_BYTES:
        raise CollectorError(
            f"feed 响应过大（{len(xml_text)} 字节 > {MAX_FEED_BYTES}），已拒绝解析"
        )
    if re.search(r"<!DOCTYPE", xml_text, re.IGNORECASE):
        # 防御 XXE 与实体膨胀攻击：合法新闻 feed 不需要 DOCTYPE
        raise CollectorError("feed 含 DOCTYPE 声明，已拒绝解析（防 XXE / 实体膨胀攻击）")

    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - 上方已拒绝 DOCTYPE 且限制大小
    except ET.ParseError as exc:
        raise CollectorError(
            f"feed XML 解析失败：{exc}", details={"parse_error": str(exc)}
        ) from exc

    root_name = _local_name(root.tag)
    if root_name in {"rss", "RDF"}:
        nodes = [node for node in root.iter() if _local_name(node.tag) == "item"]
    elif root_name == "feed":
        nodes = [node for node in root.iter() if _local_name(node.tag) == "entry"]
    else:
        raise CollectorError(
            f"不支持的 feed 根元素：{root_name!r}（支持 RSS 2.0 / RSS 1.0(RDF) / Atom）"
        )

    items: list[FeedItem] = []
    for node in nodes:
        item = _feed_item_of(node)
        if not item.title and not item.summary and not item.link:
            continue  # 完全空条目：跳过（不写入噪声数据）
        items.append(item)
    return tuple(items)


#: CSV 兜底解析器的必需列（团队批复允许的离线 Mock 解析器）
CSV_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("record_id", "title", "published_at")


def parse_news_csv(csv_text: str) -> tuple[FeedItem, ...]:
    """从本地 CSV 导入新闻（离线兜底；无可用公开 feed 的环境与框架验证用）。

    必需列：``record_id`` / ``title`` / ``published_at``；
    可选列：``summary``（或 ``content``）/ ``link`` / ``category``。
    时间为 naive（无时区）时与 RSS 一样按"时区不明确"处理。

    Raises:
        CollectorError: 内容为空或缺少必需列。
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise CollectorError("CSV 内容为空，无法解析")

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = [column for column in CSV_REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        raise CollectorError(
            f"CSV 缺少必需列：{missing}（必需：{list(CSV_REQUIRED_COLUMNS)}）",
            details={"missing_columns": missing},
        )

    items: list[FeedItem] = []
    for row in reader:
        normalized = {
            (key or "").strip().lower(): (value or "").strip() for key, value in row.items()
        }
        title = strip_html(normalized.get("title"))
        summary = strip_html(normalized.get("summary") or normalized.get("content"))
        link = normalized.get("link") or None
        category = normalized.get("category") or None
        record_id = normalized.get("record_id") or content_hash(title, summary)
        items.append(
            FeedItem(
                record_id=record_id,
                title=title,
                summary=summary,
                link=link,
                categories=(category,) if category else (),
                published=parse_feed_timestamp(normalized.get("published_at")),
            )
        )
    return tuple(items)


@register_collector
class NewsCollector(BaseCollector):
    """新闻采集器：公开 RSS/Atom（可配多源）+ 本地 CSV 兜底。

    - 代码中不含任何站点地址：所有 feed URL 来自 ``sources.config_json["feeds"]``；
    - 每个 feed 作为一"页"，游标 ``target_index`` 支持断点续采；单个 feed 的 HTTP 错误
      会被记录（runner 落 FAILED），重试由传输层负责。
    """

    collector_name = "news_collector"
    source_type = SourceType.NEWS
    max_pages_per_run = 32

    def __init__(
        self,
        source: Any,
        *,
        feeds: Sequence[str] | None = None,
        csv_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})

        configured_csv = csv_path if csv_path is not None else config.get("csv_path")
        configured_feeds = feeds if feeds is not None else config.get("feeds")

        if configured_csv:
            self._mode = "csv"
            self._csv_path: str | None = str(configured_csv)
            self._targets: tuple[str, ...] = (str(configured_csv),)
            self.parser_version = CSV_PARSER_VERSION
        else:
            feed_urls = tuple(str(url) for url in (configured_feeds or ()))
            if not feed_urls:
                raise CollectorError(
                    "新闻采集器未配置数据源：请在 sources.config_json 设置 'feeds'"
                    "（公开 RSS/Atom URL 列表），或设置 'csv_path' 走本地 CSV 兜底。"
                    "严禁抓取未授权站点（团队批复）。"
                )
            self._mode = "rss"
            self._csv_path = None
            self._targets = feed_urls
            self.parser_version = NEWS_PARSER_VERSION

        self._ambiguous_tz = 0
        self._invalid_timestamps = 0
        self._extra_warnings: list[str] = []

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self._ambiguous_tz = 0
        self._invalid_timestamps = 0
        self._extra_warnings.clear()

    async def _do_health_check(self) -> CollectorHealth:
        """CSV 模式检查文件可读；RSS 模式沿用基类（检查 base_url 配置）。"""
        if self._mode == "csv":
            exists = Path(self._csv_path or "").exists()
            return CollectorHealth(
                collector_name=self.collector_name,
                healthy=exists,
                checked_at=self._clock(),
                message=None if exists else f"CSV 新闻文件不存在：{self._csv_path}",
                details={"mode": "csv", "csv_path": self._csv_path},
            )
        return await super()._do_health_check()


    # ------------------------------------------------------------------
    # 站点相关：抓取与翻页
    # ------------------------------------------------------------------
    async def _do_fetch(
        self, cursor: dict[str, Any] | None, window: CollectWindow
    ) -> FetchPage:
        target_index = int((cursor or {}).get("target_index", 0))
        if target_index >= len(self._targets):
            return FetchPage(next_cursor=None)

        target = self._targets[target_index]
        items = (
            parse_news_csv(self._read_csv())
            if self._mode == "csv"
            else parse_rss_feed(await self._fetch_feed(target))
        )

        selected = tuple(item for item in items if self._in_window(item, window))
        skipped_by_window = len(items) - len(selected)
        if skipped_by_window:
            self._extra_warnings.append(
                f"{target}：{skipped_by_window} 条超出采集窗口已跳过"
                f"（窗口 {window.start_utc.isoformat()} ~ {window.end_utc.isoformat()}）"
            )

        payloads = tuple(self._to_payload(item, target=target) for item in selected)
        return FetchPage(
            payloads=payloads,
            next_cursor=self._next_cursor(target_index),
            raw_count=len(items),
        )

    async def _fetch_feed(self, url: str) -> str:
        """抓取一个公开 feed（超时/重试由传输层负责；HTTP 错误抛领域异常）。"""
        response = await self._request(HttpRequest(url=url, headers={"Accept": FEED_ACCEPT_HEADER}))
        if not response.ok:
            raise CollectorError(
                f"feed 返回 HTTP {response.status}：{url}",
                details={"status": response.status, "url": url},
            )
        return response.text or ""

    def _read_csv(self) -> str:
        return self._read_local_text(self._csv_path or "", kind="新闻 CSV")

    def _next_cursor(self, target_index: int) -> dict[str, Any] | None:
        next_index = target_index + 1
        if next_index >= len(self._targets):
            return None
        return {"target_index": next_index, "target": self._targets[next_index]}

    @staticmethod
    def _in_window(item: FeedItem, window: CollectWindow) -> bool:
        """窗口过滤：发布时间明确时严格过滤；时区不明确时保留（由 content_hash 幂等兜底）。"""
        published = item.published.value
        if published is None:
            return True
        return window.start_utc <= published <= window.end_utc

    def _to_payload(self, item: FeedItem, *, target: str) -> RawItemPayload:
        """把 feed 条目转成框架 payload（原始时间字符串与歧义标记全部保留，便于审计）。"""
        return RawItemPayload(
            source_record_id=item.record_id,
            item_type=RawItemType.NEWS,
            title=item.title,
            content_text=item.summary,
            raw_json={
                "provider": self._mode,
                "parser_version": self.parser_version,
                "target": target,
                "record_id": item.record_id,
                "title": item.title,
                "link": item.link,
                "categories": list(item.categories),
                "published_at_raw": item.published.raw,
                "published_at": (
                    None if item.published.value is None else item.published.value.isoformat()
                ),
                "published_at_tz_ambiguous": item.published.tz_ambiguous,
                "published_at_parse_reason": item.published.reason,
            },
            source_url=item.link or target,
            published_at=item.published.value,
        )


    # ------------------------------------------------------------------
    # 幂等（跨来源 content_hash 去重）与落库（raw_items + news_events）
    # ------------------------------------------------------------------
    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
        """★ 全局 content_hash 去重：同一篇新闻被多个 RSS 源转载时只落库一次。

        基类的去重是"本来源内"的（``source_record_id`` / ``content_hash``）；新闻场景下
        同一篇报道常同时出现在多个 feed，因此这里先做一次**跨来源**的 content_hash 检查。
        """
        digest = payload.resolved_content_hash()
        already_stored = session.scalar(
            sa.select(RawItem.id).where(RawItem.content_hash == digest).limit(1)
        )
        if already_stored is not None:
            return PERSIST_DUPLICATE
        return super()._persist_payload(session, payload)

    def _after_persist(self, session: Session, raw_item: RawItem, payload: RawItemPayload) -> None:
        """``raw_items`` 落库后回填 ``news_events``（严格按 04 §14 字段）。

        时区不明确 / 无发布时间的记录**只落 raw_items**：绝不把 ``collected_at`` 冒充成
        发布时间写进结构化表（否则研究侧会误用）；同时打印 WARNING 并计入数据质量告警。
        """
        meta = payload.raw_json
        raw_published = meta.get("published_at_raw")

        if meta.get("published_at_tz_ambiguous"):
            self._ambiguous_tz += 1
            warning = (
                f"{payload.source_record_id}：feed 未给出明确时区（原始：{raw_published!r}）"
                "→ published_at 置空、effective_at = collected_at，且不生成 news_events"
            )
            _log.warning("%s | %s", self.collector_name, warning)
            self._extra_warnings.append(warning)
            return

        if payload.published_at is None:
            self._invalid_timestamps += 1
            reason = meta.get("published_at_parse_reason") or "missing"
            warning = (
                f"{payload.source_record_id}：发布时间无法解析"
                f"（原始：{raw_published!r}，原因：{reason}）→ 仅落 raw_items，不生成 news_events"
            )
            _log.warning("%s | %s", self.collector_name, warning)
            self._extra_warnings.append(warning)
            return

        categories = meta.get("categories") or []
        session.add(
            NewsEvent(
                raw_item_id=raw_item.id,
                headline=payload.title,
                summary=payload.content_text,
                event_type=(str(categories[0]) if categories else None),
                importance=None,  # Phase 2 由 NLP 填充
                sentiment=None,  # Phase 2 由 NLP 填充
                event_at=None,  # 事件发生时间需解析器抽取，Phase 1 不猜测
                published_at=payload.published_at,
                # 与 raw_items 保持同一 effective_at，保证新旧表时间语义一致
                effective_at=raw_item.effective_at,
                parser_version=self.parser_version,
            )
        )
        session.flush()

    # ------------------------------------------------------------------
    # 数据质量告警
    # ------------------------------------------------------------------
    def _evaluate_run_warnings(
        self, window: CollectWindow, outcome: CollectOutcome
    ) -> tuple[str, ...]:
        warnings = list(super()._evaluate_run_warnings(window, outcome))
        warnings.extend(self._extra_warnings)
        if self._ambiguous_tz:
            warnings.append(
                f"本轮有 {self._ambiguous_tz} 条新闻因时区不明确按 collected_at 处理"
                "（标记见 raw_json.published_at_tz_ambiguous）"
            )
        return tuple(warnings)




