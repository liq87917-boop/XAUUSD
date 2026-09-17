"""`rss_discovery` 入口探测测试（**零网络**：`MockTransport` 脚本化响应）。

覆盖团队要求（2026-09-13 用户批准"入口探测"）：
1. 解析首页 `<link rel="alternate" type="application/rss+xml">`
   （绝对/相对地址、Atom、去重、忽略非 feed 类型）；
2. 探测链路：**robots 403 → 连首页都不请求**；首页 200 → 恰好 1 次首页请求；
   首页非 2xx → 不猜不重试；
3. CLI：**默认 dry-run 零请求**；`--no-dry-run`（Mock 传输）落 JSON 结果并给出退出码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import discover_rss_feeds
from src.collectors.rss_discovery import discover_feed_links, parse_feed_links
from src.collectors.transport import HttpResponse

pytestmark = pytest.mark.unit

HOME = "https://site.invalid/"
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /\n"
HOMEPAGE = (
    "<html><head>"
    '<link rel="alternate" type="application/rss+xml" title="Gold RSS" href="/feed.xml">'
    "</head><body>hi</body></html>"
)


def _robot(body: str) -> HttpResponse:
    return HttpResponse(status=200, text=body)


def _html(body: str = HOMEPAGE, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, text=body)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_parse_feed_links_handles_absolute_relative_and_atom() -> None:
    html = (
        "<head>"
        '<link rel="alternate" type="application/rss+xml" title="RSS" href="/feed/">'
        "<link rel='alternate' type='application/atom+xml' href='https://x.invalid/atom.xml'>"
        '<link rel="alternate" type="text/html" href="/">'
        '<link rel="stylesheet" type="text/css" href="/a.css">'
        '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        "</head>"
    )
    links = parse_feed_links(html, base_url="https://site.invalid/news")

    assert [link.url for link in links] == [
        "https://site.invalid/feed/",
        "https://x.invalid/atom.xml",
    ]  # 相对地址已转绝对；重复项去重；非 feed 类型被忽略
    assert links[0].title == "RSS"
    assert links[1].type == "application/atom+xml"


def test_parse_feed_links_empty_inputs() -> None:
    assert parse_feed_links(None, base_url=HOME) == ()
    assert parse_feed_links("", base_url=HOME) == ()
    assert parse_feed_links("<html><body>无 feed 声明</body></html>", base_url=HOME) == ()


# ---------------------------------------------------------------------------
# 探测链路
# ---------------------------------------------------------------------------
async def test_discovery_skips_homepage_when_robots_denies(mock_transport) -> None:
    transport = mock_transport([_robot(ROBOTS_DENY)])
    result = await discover_feed_links(HOME, request=transport.send)

    assert result.robots is not None and result.robots.allowed is False
    assert result.status is None and result.links == ()
    assert transport.calls == 1  # **只请求了 robots.txt**，首页一个请求都没发
    assert any("跳过首页请求" in warning for warning in result.warnings)


async def test_discovery_fetches_homepage_once_and_finds_feeds(mock_transport) -> None:
    transport = mock_transport([_robot(ROBOTS_ALLOW), _html()])
    result = await discover_feed_links(HOME, request=transport.send)

    assert result.ok and result.status == 200
    assert [link.url for link in result.links] == ["https://site.invalid/feed.xml"]
    assert transport.calls == 2  # robots + 首页，各 1 次（**绝不重试**）
    assert result.warnings == ()


async def test_discovery_reports_non_2xx_homepage_without_retry(mock_transport) -> None:
    transport = mock_transport([HttpResponse(status=404), _html(status=404)])
    result = await discover_feed_links(HOME, request=transport.send)

    assert result.status == 404 and result.links == () and result.ok is False
    assert transport.calls == 2  # 404 不重试
    assert any("非 2xx" in warning for warning in result.warnings)


async def test_discovery_reports_homepage_without_feed_declaration(mock_transport) -> None:
    transport = mock_transport([_robot(ROBOTS_ALLOW), _html("<html><head></head></html>")])
    result = await discover_feed_links(HOME, request=transport.send)

    assert result.status == 200 and result.links == ()
    assert any("未声明任何 RSS/Atom feed" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# CLI（默认 dry-run 零请求；live 走 Mock 传输）
# ---------------------------------------------------------------------------
class _NoWaitLimiter(discover_rss_feeds.RateLimiter):
    """测试用限速器：保留硬下限计算，但不真正 sleep。"""

    def __init__(self, min_interval: float) -> None:
        super().__init__(min_interval, clock=lambda: 0.0)

    async def wait(self) -> float:
        self.waits.append(0.0)
        return 0.0


def test_cli_dry_run_makes_no_request(tmp_path: Path, capsys) -> None:
    out = tmp_path / "discovery.json"
    code = discover_rss_feeds.main(["--url", HOME, "--dry-run", "--json-out", str(out)])

    assert code == 3  # dry-run 不发现任何 feed
    assert "dry-run：未发请求" in capsys.readouterr().out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sites"] == []


def test_cli_live_mode_reports_discovered_feeds(
    tmp_path: Path, monkeypatch, mock_transport, capsys
) -> None:
    transport = mock_transport([_robot(ROBOTS_ALLOW), _html()])
    monkeypatch.setattr(discover_rss_feeds, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(discover_rss_feeds, "RateLimiter", _NoWaitLimiter)
    out = tmp_path / "discovery.json"

    code = discover_rss_feeds.main(["--url", HOME, "--no-dry-run", "--json-out", str(out)])

    assert code == 0
    assert transport.calls == 2  # robots + 首页
    printed = capsys.readouterr().out
    assert "robots=允许" in printed and "https://site.invalid/feed.xml" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sites"][0]["links"][0]["url"] == "https://site.invalid/feed.xml"
    assert payload["sites"][0]["robots"]["allowed"] is True
    assert payload["min_interval"] == pytest.approx(1.5)  # 探测默认 1.5s
    assert payload["min_interval"] >= discover_rss_feeds.MIN_REQUEST_INTERVAL  # 硬下限 1.0s


def test_cli_rejects_conflicting_flags() -> None:
    assert discover_rss_feeds.main(["--url", HOME, "--dry-run", "--no-dry-run"]) == 2
