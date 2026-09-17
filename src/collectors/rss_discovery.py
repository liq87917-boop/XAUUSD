"""RSS 入口探测：从站点首页发现"官方声明的 feed 地址"（Phase 2 真实语料）。

背景（用户 2026-09-13 批准"入口探测"）：直接猜 feed 路径大量 404
（见 `docs/experiments/rss_source_verification_20260913.md` §12），
正确做法是读站点**自己声明**的 `<link rel="alternate" type="application/rss+xml">`。

合规纪律与采集器完全一致：
- **robots.txt 前置检查（fail-closed）**：不通过 → 连首页都不请求；
- **每个站点只发 1 次首页请求**，**不重试**；
- 请求间隔由调用方（CLI 的 `RateLimiter`）保证 ≥1.5 秒。

本模块只做"发现 + 解析"，**不下载 feed 正文**（正文冒烟交给 `scripts/collect_rss.py`）。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

from src.collectors.rss_collector import DEFAULT_USER_AGENT, RobotsDecision, robots_allows
from src.collectors.transport import HttpRequest, HttpResponse

__all__ = [
    "FEED_LINK_TYPES",
    "DiscoveryResult",
    "FeedLink",
    "discover_feed_links",
    "parse_feed_links",
]

#: 视为 feed 的 `<link type=...>` 取值（含 RDF/Atom/RSS 常见类型）
FEED_LINK_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/xml",
        "text/xml",
    }
)

_LINK_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
)
_HTML_ACCEPT: Final[str] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


@dataclass(frozen=True, slots=True)
class FeedLink:
    """站点首页声明的 feed 链接。"""

    url: str
    type: str = ""
    title: str = ""
    rel: str = "alternate"


def _tag_attrs(tag: str) -> dict[str, str]:
    """把标签属性解析成字典（大小写不敏感；支持双引号/单引号/无引号）。"""
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[match.group(1).lower()] = value.strip()
    return attrs


def parse_feed_links(html: str | None, *, base_url: str) -> tuple[FeedLink, ...]:
    """解析 HTML 里声明的 RSS/Atom feed 链接（按文档顺序，去重，相对地址转绝对）。

    Args:
        html: 首页 HTML（``None`` / 空串 → 返回空元组）。
        base_url: 用于把相对 `href` 转成绝对地址的基准 URL。

    Returns:
        去重后的 `FeedLink` 元组（只保留 `rel` 含 ``alternate``、
        且 `type` 属于 :data:`FEED_LINK_TYPES` 的 `<link>`）。
    """
    if not html:
        return ()
    found: list[FeedLink] = []
    seen: set[str] = set()
    for tag in _LINK_TAG_RE.findall(html):
        attrs = _tag_attrs(tag)
        link_type = (attrs.get("type") or "").split(";")[0].strip().lower()
        if link_type not in FEED_LINK_TYPES:
            continue
        rel = (attrs.get("rel") or "").strip().lower()
        if "alternate" not in rel:
            continue
        href = attrs.get("href") or ""
        if not href:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(FeedLink(url=absolute, type=link_type, title=attrs.get("title", ""), rel=rel))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """单个站点的探测结果（``status=None`` 表示"未发首页请求"）。"""

    homepage: str
    robots: RobotsDecision | None
    status: int | None = None
    links: tuple[FeedLink, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """首页是否返回 2xx。"""
        return self.status is not None and 200 <= self.status < 300

    def to_payload(self) -> dict[str, object]:
        """转成可 JSON 序列化的字典（供 CLI 落盘复盘）。"""
        return {
            "homepage": self.homepage,
            "robots": None
            if self.robots is None
            else {
                "allowed": self.robots.allowed,
                "by_rule": self.robots.by_rule,
                "reason": self.robots.reason,
            },
            "status": self.status,
            "links": [
                {"url": link.url, "type": link.type, "title": link.title, "rel": link.rel}
                for link in self.links
            ],
            "warnings": list(self.warnings),
        }


Requester = Callable[[HttpRequest], Awaitable[HttpResponse]]


async def discover_feed_links(
    homepage_url: str,
    *,
    request: Requester,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_seconds: float = 30.0,
) -> DiscoveryResult:
    """探测单个站点：robots 前置 → **1 次**首页请求 → 解析声明的 feed 链接。

    - robots 判定不通过（规则禁止 / 取不到）→ **不发首页请求**，直接返回（fail-closed）；
    - 首页非 2xx / 请求异常 → **不猜、不重试**，返回带告警的结果；
    - 全程只发 1 次首页请求（robots 请求由 :func:`robots_allows` 负责；间隔由调用方限速器保证）。
    """
    decision = await robots_allows(request, homepage_url, user_agent=user_agent)
    if not decision.allowed:
        return DiscoveryResult(
            homepage=homepage_url,
            robots=decision,
            status=None,
            warnings=(f"{homepage_url}：robots 检查未通过（{decision.reason}）→ 跳过首页请求",),
        )

    try:
        response = await request(
            HttpRequest(
                url=homepage_url,
                headers={"User-Agent": user_agent, "Accept": _HTML_ACCEPT},
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可读失败，不重试
        return DiscoveryResult(
            homepage=homepage_url,
            robots=decision,
            status=None,
            warnings=(f"{homepage_url}：首页请求失败（{type(exc).__name__}）",),
        )

    if not response.ok:
        return DiscoveryResult(
            homepage=homepage_url,
            robots=decision,
            status=response.status,
            warnings=(f"{homepage_url}：首页返回 HTTP {response.status}（非 2xx），不重试",),
        )

    links = parse_feed_links(response.text, base_url=homepage_url)
    warnings: list[str] = []
    if not links:
        warnings.append(f"{homepage_url}：首页未声明任何 RSS/Atom feed（<link rel=alternate>）")
    return DiscoveryResult(
        homepage=homepage_url,
        robots=decision,
        status=response.status,
        links=links,
        warnings=tuple(warnings),
    )
