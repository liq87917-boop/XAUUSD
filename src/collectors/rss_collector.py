"""RSS 采集器：``rss_collector``（Phase 2 真实语料主数据源）。

对应文档与团队要求：
- `docs/11_Phase2真实语料验收指南.md` §1.1（**列契约**）：`content` / `published_at` /
  `effective_at` / `source` / `author_name` / `has_media` / `url`；
- `docs/08 §5`（验收门槛）、`.clinerules` 第 2 条（asyncio + 超时 + 失败重试 ≤3 + 单源失败隔离）、
  第 6 条（原始数据只追加不覆盖）；
- 团队裁决（2026-09-14）：Phase 2 语料改用**自建 RSS 采集器**；第三方托管抓取
  （compliant-scrapers / Apify）降级为 **Phase 3 可选新闻源且默认禁用**（见 `docs/12`）。

设计要点：
1. **代码里不硬编码任何站点地址**：源清单来自 `config/rss_sources.json`
   （默认路径，可用 ``sources.config_json["sources_path"]`` 覆盖）；单源条目可单独 `enabled=false`；
2. **时间纪律（与 `news_collector` 完全一致）**：解析 `published` / `updated` 的**原始字符串**，
   带时区 → 转 UTC；**时区不明确或无法解析 → 不猜**，`published_at=None`，
   使 `effective_at = collected_at`（防泄漏），并把原始串与原因写入 `raw_json`；
3. **robots.txt：默认检查、fail-closed**（未取到 200 的 robots.txt 即跳过该源并告警）；
   确认为公开 feed 的源可在配置里显式 `robots_check=false`；
4. **缓存复用**：:mod:`src.collectors.rss_cache`（`auto/readonly/refresh/off`），
   `readonly` 模式**未命中即拒绝联网**（零请求、零成本，供 `--dry-run` / 离线复现）；
   条件请求头 `If-None-Match` / `If-Modified-Since` 从缓存恢复；
5. **失败隔离**：单个源失败 → 记警告并继续下一个源；**所有源都失败且一条没抓到** → 抛错
   （让 `collector_runs` 记 FAILED，绝不静默成功）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, NamedTuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import NewsEvent, RawItem
from database.models.enums import RawItemType, SourceType
from src.collectors.base import PERSIST_DUPLICATE, BaseCollector
from src.collectors.errors import CollectorError
from src.collectors.news import ParsedTimestamp, parse_feed_timestamp, strip_html
from src.collectors.registry import register_collector
from src.collectors.rss_cache import RssCache, RssCacheEntry
from src.collectors.transport import HttpRequest, HttpResponse
from src.collectors.types import CollectorHealth, CollectWindow, FetchPage, RawItemPayload

__all__ = [
    "ALLOWED_SOURCE_TYPES",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_SOURCES_PATH",
    "DEFAULT_SOURCE_TYPE",
    "DEFAULT_USER_AGENT",
    "RSS_PARSER_VERSION",
    "FeedFetchResult",
    "RobotsDecision",
    "RssCollector",
    "RssEntry",
    "RssSourceSpec",
    "fetch_spec_entries",
    "load_rss_sources",
    "parse_feed_entries",
    "robots_allows",
]

_log = get_logger("collectors.rss")

#: 采集器版本（写入 ``raw_json["parser_version"]``，便于事后追溯"这条是哪版解析器抓的"）
RSS_PARSER_VERSION: Final[str] = "rss-feedparser-v1"
#: 源清单默认路径（仓库根 ``config/rss_sources.json``）
DEFAULT_SOURCES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "rss_sources.json"
)
#: 抓取缓存目录（``logs/`` 已在 .gitignore 中）
DEFAULT_CACHE_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "logs" / "rss_cache"
#: 默认 User-Agent（声明身份 + 遵守 robots，便于源站识别与联系）
DEFAULT_USER_AGENT: Final[str] = "GOLD-AI-RSS-Collector/0.1 (+local research; respects robots.txt)"
#: 只接受 feed 类型，避免误抓 HTML 页面
FEED_ACCEPT_HEADER: Final[str] = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
)
#: 单源默认最多取多少条（防止单源刷屏；可在源清单里按源覆盖）
DEFAULT_MAX_ITEMS_PER_FEED: Final[int] = 50
#: **采集用途**（采集侧标记，与 DB 的 ``SourceType`` 枚举解耦；DB 枚举暂无 EVENT）：
#: - ``NEWS`` ：常规新闻/观点语料候选源 → **允许进入观点语料**；
#: - ``EVENT``：事件/公告源（如美联储新闻稿，RSS 正文=标题）→ **不进入观点语料**，
#:   仅作事件触发与背景；下游抽样（`scripts/sample_annotation_set.py`）必须按此过滤。
ALLOWED_SOURCE_TYPES: Final[tuple[str, ...]] = ("NEWS", "EVENT")
#: 默认采集用途（未显式声明时按常规新闻源处理）
DEFAULT_SOURCE_TYPE: Final[str] = "NEWS"

Requester = Callable[[HttpRequest], Awaitable[HttpResponse]]


# ---------------------------------------------------------------------------
# 源清单（config/rss_sources.json）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RssSourceSpec:
    """一个 RSS 源的配置（来自 ``config/rss_sources.json``）。"""

    id: str
    name: str
    url: str
    source_name: str
    author_name: str = ""
    language: str = "zh"
    category: str = "gold"
    enabled: bool = False
    verified: bool = False
    robots_check: bool = True
    source_type: str = DEFAULT_SOURCE_TYPE
    max_items: int = DEFAULT_MAX_ITEMS_PER_FEED
    min_items: int = 0
    notes: str = ""

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], *, index: int, defaults: Mapping[str, Any]
    ) -> tuple[RssSourceSpec | None, list[str]]:
        """把 JSON 里的一条源定义转成 `RssSourceSpec`（缺关键字段则跳过并记问题）。"""
        problems: list[str] = []
        merged: dict[str, Any] = {**defaults, **raw}
        source_id = str(merged.get("id", "")).strip()
        name = str(merged.get("name", "")).strip() or source_id
        url = str(merged.get("url", "")).strip()
        scheme = urlparse(url).scheme.lower() if url else ""
        if not source_id:
            problems.append(f"第 {index} 条源缺少 id，已跳过")
        if not url:
            problems.append(f"{source_id or f'第 {index} 条源'}缺少 url，已跳过")
        elif scheme not in {"http", "https"}:
            problems.append(f"{source_id}：url 必须是 http/https（实际 {scheme or '空'}），已跳过")
        if problems:
            return None, problems

        def _flag(key: str, fallback: bool) -> bool:
            value = merged.get(key, fallback)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "y"}

        try:
            max_items = int(merged.get("max_items", DEFAULT_MAX_ITEMS_PER_FEED) or 1)
        except (TypeError, ValueError):
            problems.append(f"{source_id}：max_items 不是整数，已回退默认值")
            max_items = DEFAULT_MAX_ITEMS_PER_FEED
        try:
            min_items = int(merged.get("min_items", 0) or 0)
        except (TypeError, ValueError):
            problems.append(f"{source_id}：min_items 不是整数，已回退 0")
            min_items = 0

        source_type = str(merged.get("source_type", DEFAULT_SOURCE_TYPE) or "").strip().upper()
        if source_type not in ALLOWED_SOURCE_TYPES:
            problems.append(
                f"{source_id}：source_type 必须是 {ALLOWED_SOURCE_TYPES} 之一"
                f"（实际 {source_type or '空'}），已回退 {DEFAULT_SOURCE_TYPE}"
            )
            source_type = DEFAULT_SOURCE_TYPE

        return (
            cls(
                id=source_id,
                name=name,
                url=url,
                source_name=str(merged.get("source_name", "")).strip() or source_id,
                author_name=str(merged.get("author_name", "")).strip(),
                language=str(merged.get("language", "zh")).strip() or "zh",
                category=str(merged.get("category", "gold")).strip() or "gold",
                enabled=_flag("enabled", False),
                verified=_flag("verified", False),
                robots_check=_flag("robots_check", True),
                source_type=source_type,
                max_items=max(1, max_items),
                min_items=max(0, min_items),
                notes=str(merged.get("notes", "")).strip(),
            ),
            problems,
        )


def load_rss_sources(path: Path | str) -> tuple[tuple[RssSourceSpec, ...], list[str]]:
    """读源清单 JSON，返回 ``(全部源, 问题列表)``。

    Raises:
        CollectorError: 文件缺失 / 不是合法 JSON / 结构不对（**不静默**）。
    """
    target = Path(path)
    if not target.exists():
        raise CollectorError(
            f"RSS 源清单不存在：{target}（应为 config/rss_sources.json）",
            details={"path": str(target)},
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(
            f"RSS 源清单读取失败：{target}（{type(exc).__name__}）",
            details={"path": str(target)},
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sources"), list):
        raise CollectorError(f"RSS 源清单结构不对（需要 {{'sources': [...]}}）：{target}")

    raw_defaults = payload.get("defaults")
    defaults: Mapping[str, Any] = raw_defaults if isinstance(raw_defaults, Mapping) else {}
    specs: list[RssSourceSpec] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload["sources"], start=1):
        if not isinstance(raw, Mapping):
            problems.append(f"第 {index} 条源不是对象，已跳过")
            continue
        spec, item_problems = RssSourceSpec.from_mapping(raw, index=index, defaults=defaults)
        problems.extend(item_problems)
        if spec is None:
            continue
        if spec.id in seen:
            problems.append(f"源 id 重复：{spec.id}（保留第一条，已跳过后者）")
            continue
        seen.add(spec.id)
        specs.append(spec)
    if not specs:
        raise CollectorError(f"RSS 源清单里没有可用源：{target}")
    return tuple(specs), problems


# ---------------------------------------------------------------------------
# robots.txt（默认检查、fail-closed）
# ---------------------------------------------------------------------------
class RobotsDecision(NamedTuple):
    """robots.txt 判定结果（`by_rule=True` 表示站点规则明确禁止）。"""

    allowed: bool
    reason: str
    by_rule: bool = False


async def robots_allows(
    request: Requester, url: str, *, user_agent: str = DEFAULT_USER_AGENT
) -> RobotsDecision:
    """判断 ``user_agent`` 是否允许抓取 ``url``（**fail-closed**）。

    规则（与团队采纳的合规姿态一致）：
    - 取不到 robots.txt（网络错误 / 非 200/404）→ **不允许**（宁可少抓，不越线）；
    - 404 → 允许（RFC：没有 robots.txt 即无限制）；
    - 解析失败 → **不允许**；
    - 其余 → `RobotFileParser.can_fetch(user_agent, url)`。

    Returns:
        `RobotsDecision`：`allowed`（是否允许）、`reason`（原因，可直接写进告警）、
        `by_rule`（是否**站点规则明确禁止**——用于区分"合规拒绝"与"技术性无法验证"，
        后者按 `failed` 处理，避免整轮采集静默变成 SUCCESS）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return RobotsDecision(False, f"url 形态非法：{url}")
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = await request(
            HttpRequest(url=robots_url, headers={"User-Agent": user_agent}, timeout_seconds=20)
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都按"不允许"处理（fail-closed）
        return RobotsDecision(False, f"robots.txt 请求失败（{type(exc).__name__}）")
    if response.status == 404:
        return RobotsDecision(True, "robots.txt 不存在（404）→ 视为无限制")
    if response.status != 200:
        return RobotsDecision(False, f"robots.txt 返回 HTTP {response.status}（非 200/404）→ 拒绝")
    parser = RobotFileParser()
    try:
        parser.parse((response.text or "").splitlines())
    except Exception as exc:  # noqa: BLE001 - 解析失败也拒绝
        return RobotsDecision(False, f"robots.txt 解析失败（{type(exc).__name__}）→ 拒绝")
    if not parser.can_fetch(user_agent, url):
        return RobotsDecision(False, "robots.txt 明确禁止本 User-Agent 抓取该路径", True)
    return RobotsDecision(True, "robots.txt 允许")


# ---------------------------------------------------------------------------
# feed 解析（feedparser；时间仍走项目自己的"不猜时区"逻辑）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RssEntry:
    """一条解析后的 feed 条目（与 `docs/11 §1.1` 列契约一一对应）。"""

    feed_id: str
    source_name: str
    record_id: str
    title: str | None
    content_text: str | None
    link: str | None
    author_name: str
    language: str
    categories: tuple[str, ...]
    published: ParsedTimestamp
    has_media: bool = False


def _header_value(headers: Mapping[str, str], name: str) -> str:
    """大小写不敏感地取响应头。

    aiohttp 的 headers 是大小写不敏感的，但 `MockTransport` / 普通 dict 不是；
    条件请求（`ETag` / `Last-Modified`）必须两种情形都能取到。
    """
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value or "")
    return ""


def _entry_html(entry: Mapping[str, Any]) -> str:
    """取条目正文 HTML：优先 `content`，其次 `summary` / `description`。"""
    contents = entry.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, Mapping) and item.get("value"):
                return str(item["value"])
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _entry_categories(entry: Mapping[str, Any]) -> tuple[str, ...]:
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        str(tag.get("term", "")).strip()
        for tag in tags
        if isinstance(tag, Mapping) and str(tag.get("term", "")).strip()
    )


def parse_feed_entries(
    payload: str | bytes,
    *,
    spec: RssSourceSpec,
    max_items: int | None = None,
) -> tuple[tuple[RssEntry, ...], tuple[str, ...]]:
    """解析 feed 文本 → `RssEntry` 列表 + 告警。

    **时间不猜**：`published` / `updated` 只取**原始字符串**交给
    :func:`src.collectors.news.parse_feed_timestamp`（feedparser 自己会把无时区时间当成 UTC，
    那会破坏本项目的时间因果纪律，因此不采用它的解析结果）。
    """
    parsed = feedparser.parse(payload)
    warnings: list[str] = []
    if getattr(parsed, "bozo", 0):
        warnings.append(f"{spec.id}：feed 解析告警（{getattr(parsed, 'bozo_exception', '未知')}）")
    limit = max(1, max_items if max_items is not None else spec.max_items)
    entries: list[RssEntry] = []
    seen: set[str] = set()
    duplicates = 0
    for raw in list(getattr(parsed, "entries", []) or [])[: limit * 2]:
        if not isinstance(raw, Mapping):
            continue
        link = str(raw.get("link", "") or "").strip() or None
        link_or_id = str(raw.get("id", "") or "").strip() or link or ""
        if not link_or_id:
            warnings.append(f"{spec.id}：跳过一条既无 id 也无 link 的条目")
            continue
        if link_or_id in seen:
            duplicates += 1
            continue
        seen.add(link_or_id)
        html = _entry_html(raw)
        published_raw = raw.get("published") or raw.get("updated") or raw.get("created")
        entries.append(
            RssEntry(
                feed_id=spec.id,
                source_name=spec.source_name,
                record_id=link_or_id,
                title=strip_html(str(raw.get("title", "") or "") or None),
                content_text=strip_html(html or None),
                link=link,
                author_name=strip_html(str(raw.get("author", "") or "") or None)
                or spec.author_name,
                language=spec.language,
                categories=_entry_categories(raw),
                published=parse_feed_timestamp(
                    str(published_raw) if published_raw is not None else None
                ),
                has_media=bool(
                    "<img" in html.lower()
                    or raw.get("media_content")
                    or raw.get("enclosures")
                    or raw.get("media_thumbnail")
                ),
            )
        )
        if len(entries) >= limit:
            break
    if duplicates:
        warnings.append(f"{spec.id}：同源内重复条目 {duplicates} 条已忽略")
    if not entries:
        warnings.append(f"{spec.id}：feed 未解析出任何条目（可能为空 feed 或格式不兼容）")
    return tuple(entries), tuple(warnings)



# ---------------------------------------------------------------------------
# 单源抓取（robots → 缓存 → 条件请求 → 解析）：采集器与 CLI **共用同一实现**
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    """单个源的抓取结果。

    ``status`` 取值：
    - ``cached``       ：直接用本地缓存（`readonly` 重放，或缓存无校验器可协商）→ **零网络**
    - ``fetched``      ：真实抓取成功（`auto` 模式下带 `If-None-Match` / `If-Modified-Since` 协商）
    - ``not_modified`` ：HTTP 304 —— 自上次以来没有新内容（返回 0 条）
    - ``skipped``      ：合规性跳过（源被禁用 / robots 规则明确禁止 / `readonly` 未命中拒绝联网）
    - ``failed``       ：抓取或解析失败（含 robots 技术性无法验证），已写缓存错误条目

    另外携带两项**观测信息**（供 CLI 的《源验证报告》与复盘使用，不参与判定）：
    ``robots``（robots.txt 判定与原因）与 ``http_status``（本次真实请求的状态码）。

    ``not_modified``（HTTP 304）的语义：**自上次采集以来源站无更新**，此时条目**改由本地缓存重放**
    （保证语料导出可复现；DB 落库路径由 `source_record_id`/`content_hash` 幂等去重兜底），
    并在 ``warnings`` 里留痕说明。
    """

    spec_id: str
    status: str
    entries: tuple[RssEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    skipped_by_window: int = 0
    robots: RobotsDecision | None = None
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"cached", "fetched", "not_modified"}


async def fetch_spec_entries(
    spec: RssSourceSpec,
    *,
    request: Requester,
    cache: RssCache,
    window: CollectWindow | None = None,
    max_items: int | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FeedFetchResult:
    """抓取并解析**单个源**（含 robots 检查、缓存、条件请求、窗口过滤）。

    该函数是"采集器落库路径"与"CLI 导出 CSV 路径"的**唯一**抓取实现，
    避免两处逻辑漂移（同一份 robots/缓存/时间纪律）。
    """
    warnings: list[str] = []
    if not spec.enabled:
        return FeedFetchResult(spec.id, "skipped", warnings=(f"{spec.id}：源已被禁用",))

    key = RssCache.key_for(spec.url)
    cached = cache.load(key)
    body: str | None = None
    etag = cached.etag if cached else ""
    last_modified = cached.last_modified if cached else ""
    status = "fetched"
    #: 观测信息（写进《源验证报告》）：robots 判定 + 本次真实请求的状态码
    robots_decision: RobotsDecision | None = None
    http_status: int | None = None

    if cached is not None and cached.ok:
        if cache.mode == "readonly" or not (etag or last_modified):
            # 只读重放：零网络；无校验器（ETag/Last-Modified）时无法协商，也只能直接用缓存
            body = cached.body
            status = "cached"
    elif cache.mode == "readonly":
        return FeedFetchResult(
            spec.id,
            "skipped",
            warnings=(
                f"{spec.id}：readonly 模式未命中缓存，**拒绝联网**"
                f"（cache={cache.path_for(key)}）",
            ),
        )
    if body is None:
        # robots 只在"确实要发请求"前检查：只读重放保持零网络
        if spec.robots_check:
            decision = await robots_allows(request, spec.url, user_agent=user_agent)
            robots_decision = decision
            if not decision.allowed:
                # 站点规则明确禁止 → 合规跳过；技术性无法验证 → failed（不得静默成功）
                return FeedFetchResult(
                    spec.id,
                    "skipped" if decision.by_rule else "failed",
                    warnings=(f"{spec.id}：robots 检查未通过（{decision.reason}）",),
                    robots=decision,
                )
        headers = {"User-Agent": user_agent, "Accept": FEED_ACCEPT_HEADER}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            response = await request(HttpRequest(url=spec.url, headers=headers, timeout_seconds=30))
        except Exception as exc:  # noqa: BLE001 - 统一转成可读失败（由上层决定是否抛错）
            message = f"{spec.id}：请求失败（{type(exc).__name__}）"
            cache.store(
                RssCacheEntry(
                    url=spec.url, status=0, fetched_at=RssCache.now_iso(), error=message
                )
            )
            return FeedFetchResult(spec.id, "failed", warnings=(message,), robots=robots_decision)
        if response.status == 304:
            cache.store(
                RssCacheEntry(
                    url=spec.url,
                    status=200,
                    fetched_at=RssCache.now_iso(),
                    body=cached.body if cached else "",
                    etag=etag,
                    last_modified=last_modified,
                )
            )
            if not (cached and cached.body):
                return FeedFetchResult(
                    spec.id,
                    "not_modified",
                    warnings=(f"{spec.id}：HTTP 304，但本地无缓存体，无条目可用",),
                    robots=robots_decision,
                    http_status=304,
                )
            # 304 = 源站无更新 → **用缓存体重放条目**（否则 CSV 导出会莫名其妙变成 0 条）
            body = cached.body
            status = "not_modified"
            http_status = 304
            warnings.append(
                f"{spec.id}：HTTP 304（自上次以来无更新）→ 条目改由本地缓存重放（可复现）"
            )
        else:
            if not response.ok:
                message = f"{spec.id}：HTTP {response.status}（响应非 2xx）"
                cache.store(
                    RssCacheEntry(
                        url=spec.url,
                        status=response.status,
                        fetched_at=RssCache.now_iso(),
                        error=message,
                    )
                )
                return FeedFetchResult(
                    spec.id,
                    "failed",
                    warnings=(message,),
                    robots=robots_decision,
                    http_status=response.status,
                )
            body = response.text or ""
            http_status = response.status
            cache.store(
                RssCacheEntry(
                    url=spec.url,
                    status=response.status,
                    fetched_at=RssCache.now_iso(),
                    body=body,
                    etag=_header_value(response.headers, "ETag"),
                    last_modified=_header_value(response.headers, "Last-Modified"),
                )
            )

    entries, parse_warnings = parse_feed_entries(body, spec=spec, max_items=max_items)
    warnings.extend(parse_warnings)
    kept = entries
    skipped = 0
    if window is not None:
        in_window: list[RssEntry] = []
        for entry in entries:
            published = entry.published.value
            if published is None or window.start_utc <= published <= window.end_utc:
                in_window.append(entry)
            else:
                skipped += 1
        kept = tuple(in_window)
        if skipped:
            warnings.append(f"{spec.id}：{skipped} 条超出采集窗口已跳过")
    ambiguous = sum(1 for entry in kept if entry.published.tz_ambiguous)
    if ambiguous:
        warnings.append(
            f"{spec.id}：{ambiguous} 条发布时间时区不明确 → published_at 置空"
            "（effective_at = collected_at，绝不猜测）"
        )
    return FeedFetchResult(
        spec.id,
        status,
        entries=kept,
        warnings=tuple(warnings),
        skipped_by_window=skipped,
        robots=robots_decision,
        http_status=http_status,
    )



# ---------------------------------------------------------------------------
# 采集器（BaseCollector 子类：落库 + 幂等 + 游标 + 告警）
# ---------------------------------------------------------------------------
@register_collector
class RssCollector(BaseCollector):
    """RSS 采集器（**Phase 2 真实语料主数据源**）。

    - 源清单来自 ``config/rss_sources.json``（或 ``config_json["sources_path"]``）；
      只抓 `enabled=true` 的源，单源失败不影响其它源；
    - 每个源作为一"页"，游标 ``feed_index`` 支持断点续采；
    - 落库由基类完成：同源内 ``source_record_id`` / ``content_hash`` 幂等去重。
    """

    collector_name: ClassVar[str] = "rss_collector"
    source_type: ClassVar[SourceType] = SourceType.NEWS
    parser_version: ClassVar[str] = RSS_PARSER_VERSION
    #: 源数量通常 ≤ 20；留足余量同时防止无限翻页
    max_pages_per_run: ClassVar[int] = 64
    #: 数据质量下限：低于该值 → WARNING（可在 config_json["min_records_per_run"] 覆盖）
    min_records_per_run: ClassVar[int] = 1

    def __init__(
        self,
        source: Any,
        *,
        sources_path: Path | str | None = None,
        specs: Sequence[RssSourceSpec] | None = None,
        cache: RssCache | None = None,
        cache_dir: Path | str | None = None,
        cache_mode: str = "auto",
        max_items_per_feed: int | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, **kwargs)
        config = dict(source.config_json or {})
        self.sources_path = Path(sources_path or config.get("sources_path") or DEFAULT_SOURCES_PATH)
        self.user_agent = str(config.get("user_agent") or user_agent)
        self.max_items_per_feed = max_items_per_feed

        if specs is None:
            all_specs, self.config_problems = load_rss_sources(self.sources_path)
            self._specs_injected = False
        else:
            all_specs, self.config_problems = tuple(specs), []
            self._specs_injected = True
        configured_ids = {
            str(source_id).strip()
            for source_id in (config.get("source_ids") or [])
            if str(source_id).strip()
        }
        if configured_ids:
            known_ids = {spec.id for spec in all_specs}
            missing_ids = sorted(configured_ids - known_ids)
            if missing_ids:
                self.config_problems.append(
                    "sources.config_json.source_ids 含未知源：" + ", ".join(missing_ids)
                )
            all_specs = tuple(spec for spec in all_specs if spec.id in configured_ids)
        self.specs = tuple(spec for spec in all_specs if spec.enabled)
        self.disabled_specs = tuple(spec for spec in all_specs if not spec.enabled)
        if not self.specs:
            raise CollectorError(
                f"没有启用的 RSS 源：{self.sources_path}"
                "（请把已验证的源置为 enabled=true；未验证源必须保持禁用）",
                details={"path": str(self.sources_path)},
            )

        self.cache = cache or RssCache(
            Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR, mode=cache_mode
        )
        self._extra_warnings: list[str] = []
        self._failures = 0
        self._collected = 0
        self._attempted = 0

    # ---------------- 运行期状态 / 可观测性 ----------------
    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self._extra_warnings.clear()
        self._failures = 0
        self._collected = 0
        self._attempted = 0

    def _evaluate_run_warnings(self, window: CollectWindow, outcome: Any) -> tuple[str, ...]:
        """在基类"条数不足"告警之外，追加每源级别告警（robots / 304 / 时区 / 失败）。"""
        base = super()._evaluate_run_warnings(window, outcome)
        return (*base, *self._extra_warnings)

    async def _do_health_check(self) -> CollectorHealth:
        """健康检查：注入源清单时只看源数量；否则要求清单文件存在（**不发网络请求**）。"""
        exists = self.sources_path.exists()
        healthy = bool(self.specs) and (exists or self._specs_injected)
        return CollectorHealth(
            collector_name=self.collector_name,
            healthy=healthy,
            checked_at=self._clock(),
            message=None
            if healthy
            else f"源清单不可用：{self.sources_path}（存在={exists}，启用源={len(self.specs)}）",
            details={
                "sources_path": str(self.sources_path),
                "enabled_sources": [spec.id for spec in self.specs],
                "disabled_sources": [spec.id for spec in self.disabled_specs],
                "config_problems": list(self.config_problems),
                "cache": self.cache.stats(),
            },
        )


    # ---------------- 站点相关：抓取（每页一个源） ----------------
    async def _do_fetch(self, cursor: dict[str, Any] | None, window: CollectWindow) -> FetchPage:
        feed_index = int((cursor or {}).get("feed_index", 0))
        if feed_index >= len(self.specs):
            return FetchPage(next_cursor=None)

        spec = self.specs[feed_index]
        if not spec.verified:
            self._extra_warnings.append(
                f"{spec.id}：源被启用但尚未标记 verified=true（请先完成冒烟验证）"
            )
        self._attempted += 1
        result = await fetch_spec_entries(
            spec,
            request=self._request,
            cache=self.cache,
            window=window,
            max_items=self.max_items_per_feed,
            user_agent=self.user_agent,
        )
        self._extra_warnings.extend(result.warnings)
        if result.status == "failed":
            self._failures += 1
            _log.warning("rss 源抓取失败：%s", result.spec_id)
        elif result.status == "fetched":
            _log.info("rss 源抓取成功：%s（%d 条）", result.spec_id, len(result.entries))

        next_index = feed_index + 1
        next_cursor: dict[str, Any] | None = {"feed_index": next_index}
        if next_index >= len(self.specs):
            next_cursor = None
            # 所有源都失败且一条没抓到 → 明确失败（绝不静默 SUCCESS）
            if self._failures and self._failures == self._attempted and self._collected == 0:
                raise CollectorError(
                    "所有 RSS 源均抓取失败：" + "；".join(self._extra_warnings[-self._failures :]),
                    details={"source": self.source.name, "attempted": self._attempted},
                )

        payloads = tuple(self._to_payload(spec, entry) for entry in result.entries)
        self._collected += len(payloads)
        return FetchPage(payloads=payloads, next_cursor=next_cursor, raw_count=len(result.entries))

    # ---------------- 字段映射（对齐 docs/11 §1.1 列契约） ----------------
    @staticmethod
    def _to_payload(spec: RssSourceSpec, entry: RssEntry) -> RawItemPayload:
        """`RssEntry` → `RawItemPayload`（时间不猜；标题与正文分开存放）。"""
        published: ParsedTimestamp = entry.published
        return RawItemPayload(
            source_record_id=entry.record_id,
            item_type=RawItemType.NEWS,
            title=entry.title,
            content_text=entry.content_text,
            raw_json={
                "feed_id": entry.feed_id,
                "feed_url": spec.url,
                "source_name": entry.source_name,
                "source_type": spec.source_type,
                "author_name": entry.author_name,
                "language": entry.language,
                "category": spec.category,
                "categories": list(entry.categories),
                "has_media": entry.has_media,
                "published_raw": published.raw,
                "published_tz_ambiguous": published.tz_ambiguous,
                "published_reason": published.reason,
                "parser_version": RSS_PARSER_VERSION,
            },
            source_url=entry.link,
            published_at=published.value,
        )

    # ---------------- 幂等与结构化落库（raw_items + news_events） ----------------
    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
        """按正文哈希做跨来源去重，与既有 ``NewsCollector`` 的语义保持一致。"""
        digest = payload.resolved_content_hash()
        already_stored = session.scalar(
            sa.select(RawItem.id).where(RawItem.content_hash == digest).limit(1)
        )
        if already_stored is not None:
            return PERSIST_DUPLICATE
        return super()._persist_payload(session, payload)

    def _after_persist(
        self, session: Session, raw_item: RawItem, payload: RawItemPayload
    ) -> None:
        """为时间明确的 RSS 条目生成 ``news_events``；时间不明时只保留原始层。"""
        meta = payload.raw_json
        if payload.published_at is None:
            reason = meta.get("published_reason") or "missing"
            warning = (
                f"{payload.source_record_id}：发布时间不可用"
                f"（原始：{meta.get('published_raw')!r}，原因：{reason}）"
                "→ 仅落 raw_items，不生成 news_events"
            )
            _log.warning("%s | %s", self.collector_name, warning)
            self._extra_warnings.append(warning)
            return

        categories = meta.get("categories") or []
        event_type = str(categories[0]) if categories else str(meta.get("category") or "")
        session.add(
            NewsEvent(
                raw_item_id=raw_item.id,
                headline=payload.title,
                summary=payload.content_text,
                event_type=event_type or None,
                importance=None,
                sentiment=None,
                event_at=None,
                published_at=payload.published_at,
                effective_at=raw_item.effective_at,
                parser_version=self.parser_version,
            )
        )
        session.flush()

    # ---------------- 统计（供 CLI / 报告使用） ----------------
    def stats(self) -> dict[str, Any]:
        """本轮运行统计（不含密钥；可安全打印）。"""
        return {
            "collector_name": self.collector_name,
            "sources_path": str(self.sources_path),
            "enabled_sources": len(self.specs),
            "disabled_sources": len(self.disabled_specs),
            "attempted": self._attempted,
            "failures": self._failures,
            "collected": self._collected,
            "parser_version": RSS_PARSER_VERSION,
            "cache": self.cache.stats(),
            "warnings": list(self._extra_warnings),
        }
