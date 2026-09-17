"""`RssCollector` 单元测试：源清单校验 + feed 解析（**零网络**）。

集成/落库部分在 ``tests/integration/test_rss_collector.py``。

覆盖团队要求：
1. 源清单：缺字段/非法 URL/id 重复 → 记问题并跳过（不静默）、文件缺失 → 明确报错；
2. 解析：RSS 2.0 / Atom / CDATA / 摘要含 HTML / 无 pubDate / **无时区时间不猜**；
3. 字段映射对齐 `docs/11 §1.1` 契约（content / published_at / source / author_name / has_media）；
4. 抓取链路：robots fail-closed、缓存命中零网络、304、readonly 未命中拒绝联网、窗口过滤。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from database.models.enums import RawItemType
from src.collectors.errors import CollectorError
from src.collectors.rss_cache import RssCache
from src.collectors.rss_collector import (
    RSS_PARSER_VERSION,
    RssCollector,
    RssSourceSpec,
    fetch_spec_entries,
    load_rss_sources,
    parse_feed_entries,
    robots_allows,
)
from src.collectors.transport import HttpRequest, HttpResponse, RetryPolicy, send_with_retry
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.unit

FEED_URL = "https://feed.invalid/rss.xml"
ROBOTS_URL = "https://feed.invalid/robots.txt"
WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 30, tzinfo=UTC)
)
#: 用户明确批准启用的源（2026-09-13：`fred_blog` / `fed_press` 冒烟通过后，由用户逐源授权启用；
#: `ecb_press` 曾获批启用，后因"正文=标题"被用户改为 `source_type=EVENT` + `enabled=false`）。
#: **新增启用必须先拿到人工确认**，再同步本集合与 `config/rss_sources.json`。
APPROVED_ENABLED_SOURCES: frozenset[str] = frozenset({"fred_blog", "fed_press"})


def _spec(**overrides: object) -> RssSourceSpec:
    defaults: dict[str, object] = {
        "id": "feed_one",
        "name": "测试源",
        "url": FEED_URL,
        "source_name": "test_feed",
        "author_name": "测试作者",
        "enabled": True,
        "verified": True,
    }
    defaults.update(overrides)
    return RssSourceSpec(**defaults)  # type: ignore[arg-type]


def _rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>Gold Feed</title>" + "".join(items) + "</channel></rss>"
    )


def _item(
    guid: str = "post-1",
    *,
    title: str = "黄金短线看涨",
    description: str = "<p>金价<b>突破</b> 3400，减仓后继续持有。</p>",
    pub_date: str = "Mon, 14 Sep 2026 08:30:00 GMT",
    author: str = "",
) -> str:
    return (
        "<item>"
        f"<title><![CDATA[{title}]]></title>"
        f"<link>https://feed.invalid/{guid}</link>"
        f"<description><![CDATA[{description}]]></description>"
        f'<guid isPermaLink="false">{guid}</guid>'
        f"<pubDate>{pub_date}</pubDate>"
        + (f"<author>{author}</author>" if author else "")
        + "</item>"
    )


def _atom(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Atom Feed</title>" + "".join(entries) + "</feed>"
    )


# ---------------------------------------------------------------------------
# 源清单
# ---------------------------------------------------------------------------
def _write_config(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "rss_sources.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_sources_applies_defaults(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "defaults": {"enabled": False, "language": "zh"},
            "sources": [
                {"id": "a", "name": "A", "url": "https://a.invalid/f.xml"},
                {"id": "b", "name": "B", "url": "https://b.invalid/f.xml", "enabled": "true"},
            ],
        },
    )
    specs, problems = load_rss_sources(path)
    assert problems == []
    assert [(s.id, s.enabled, s.language) for s in specs] == [
        ("a", False, "zh"),
        ("b", True, "zh"),
    ]
    assert specs[1].source_name == "b" and specs[1].robots_check is True
    assert [s.source_type for s in specs] == ["NEWS", "NEWS"]  # 未声明时默认 NEWS


def test_load_sources_normalises_source_type_and_rejects_unknown(tmp_path: Path) -> None:
    """`source_type`：大小写归一；非法值 → 记问题并回退 NEWS（EVENT=仅事件源，不进观点语料）。"""
    path = _write_config(
        tmp_path,
        {
            "sources": [
                {"id": "event_src", "url": "https://e.invalid/f.xml", "source_type": "event"},
                {"id": "weird", "url": "https://w.invalid/f.xml", "source_type": "OPINION"},
            ]
        },
    )
    specs, problems = load_rss_sources(path)
    assert [(s.id, s.source_type) for s in specs] == [("event_src", "EVENT"), ("weird", "NEWS")]
    assert any("source_type 必须是" in p for p in problems)


def test_load_sources_reports_problems_and_skips(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "sources": [
                {"name": "缺 id", "url": "https://a.invalid/f.xml"},
                {"id": "bad_scheme", "url": "ftp://a.invalid/f.xml"},
                {"id": "good", "url": "https://good.invalid/f.xml"},
                {"id": "good", "url": "https://dup.invalid/f.xml"},
                {"id": "bad_max", "url": "https://b.invalid/f.xml", "max_items": "many"},
                "not-an-object",
            ]
        },
    )
    specs, problems = load_rss_sources(path)
    assert [s.id for s in specs] == ["good", "bad_max"]
    joined = " / ".join(problems)
    assert "缺少 id" in joined
    assert "http/https" in joined
    assert "id 重复" in joined
    assert "max_items 不是整数" in joined
    assert "不是对象" in joined


def test_load_sources_missing_file_or_bad_json_raises(tmp_path: Path) -> None:
    with pytest.raises(CollectorError, match="源清单不存在"):
        load_rss_sources(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(CollectorError, match="读取失败"):
        load_rss_sources(broken)
    wrong_shape = _write_config(tmp_path, {"sources": {}})
    with pytest.raises(CollectorError, match="结构不对"):
        load_rss_sources(wrong_shape)
    empty = _write_config(tmp_path, {"sources": []})
    with pytest.raises(CollectorError, match="没有可用源"):
        load_rss_sources(empty)


def test_repo_config_is_valid_and_only_approved_sources_enabled() -> None:
    """仓库自带源清单必须合法；**只有经人工批准的源才允许 enabled=true**（红线）。

    `APPROVED_ENABLED_SOURCES` 是人工批准清单：新增启用必须先拿到人工确认，
    再同步这个常量和 `config/rss_sources.json`（自动化流程不得自己启用任何源）。
    """
    specs, problems = load_rss_sources("config/rss_sources.json")
    assert problems == []
    expected = {
        "jin10_flash",
        "fx678_gold",
        "wallstreetcn_feed",
        "investing_gold",
        "fred_blog",
        "fed_press",
        "ecb_press",
        "yahoo_gold",
        "kitco_news",
        "mining_com",
        "bullionvault",
        "goldseek",
    }
    assert {s.id for s in specs} == expected
    assert {s.id for s in specs if s.enabled} == APPROVED_ENABLED_SOURCES
    assert all(s.robots_check for s in specs)  # robots 检查永远不得关闭
    assert all(s.url.startswith("https://") for s in specs)



# ---------------------------------------------------------------------------
# feed 解析
# ---------------------------------------------------------------------------
def test_parse_rss_item_strips_html_and_converts_time_to_utc() -> None:
    entries, warnings = parse_feed_entries(_rss(_item()), spec=_spec())
    assert warnings == ()
    entry = entries[0]
    assert entry.record_id == "post-1"
    assert entry.title == "黄金短线看涨"
    assert entry.content_text == "金价 突破 3400，减仓后继续持有。"
    assert entry.link == "https://feed.invalid/post-1"
    assert entry.source_name == "test_feed"
    assert entry.author_name == "测试作者"  # 条目无 author → 回退源级作者
    assert entry.published.value == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert entry.published.tz_ambiguous is False
    assert entry.has_media is False


def test_parse_item_author_overrides_source_author() -> None:
    entries, _ = parse_feed_entries(_rss(_item(author="张三")), spec=_spec())
    assert entries[0].author_name == "张三"


def test_timezone_less_timestamp_is_not_guessed() -> None:
    """★ 时间纪律：pubDate 无时区 → published_at 置空 + 明确原因（绝不当成 UTC）。"""
    entries, warnings = parse_feed_entries(
        _rss(_item(pub_date="Mon, 14 Sep 2026 08:30:00")), spec=_spec()
    )
    published = entries[0].published
    assert published.value is None
    assert published.tz_ambiguous is True
    assert published.raw == "Mon, 14 Sep 2026 08:30:00"
    assert published.reason == "rfc822_without_timezone"
    assert warnings == ()  # 时区告警由 fetch_spec_entries 统计（避免重复计数）


def test_missing_pubdate_yields_empty_reason() -> None:
    xml = _rss(
        "<item><title>无时间</title><link>https://feed.invalid/x</link><guid>x</guid></item>"
    )
    entries, _ = parse_feed_entries(xml, spec=_spec())
    assert entries[0].published.value is None
    assert entries[0].published.reason == "empty"


def test_parse_atom_entry_with_media() -> None:
    xml = _atom(
        "<entry><title>Gold outlook</title><id>atom-1</id>"
        '<link href="https://feed.invalid/atom-1"/>'
        "<updated>2026-09-14T08:30:00Z</updated>"
        '<content type="html">&lt;p&gt;看多 &lt;img src="x.png"/&gt;&lt;/p&gt;</content>'
        "<author><name>Alice</name></author></entry>"
    )
    entries, warnings = parse_feed_entries(xml, spec=_spec(language="en"))
    assert warnings == ()
    entry = entries[0]
    assert entry.record_id == "atom-1"
    assert entry.published.value == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert entry.has_media is True
    assert entry.language == "en"


def test_parse_drops_duplicate_ids_within_feed_and_respects_limit() -> None:
    xml = _rss(_item("dup"), _item("dup"), _item("p2"), _item("p3"))
    entries, warnings = parse_feed_entries(xml, spec=_spec(), max_items=2)
    assert [e.record_id for e in entries] == ["dup", "p2"]
    assert any("重复条目" in w for w in warnings)


def test_parse_empty_and_broken_feed_warns() -> None:
    entries, warnings = parse_feed_entries(_rss(), spec=_spec())
    assert entries == ()
    assert any("未解析出任何条目" in w for w in warnings)
    broken, broken_warnings = parse_feed_entries("<rss><channel>", spec=_spec())
    assert broken == ()
    assert any("feed 解析告警" in w for w in broken_warnings)


def test_parse_reports_version() -> None:
    assert RSS_PARSER_VERSION == "rss-feedparser-v1"


# ---------------------------------------------------------------------------
# robots.txt（默认检查、fail-closed）
# ---------------------------------------------------------------------------
def _requester(mock_transport, script: list[object]):
    """把 MockTransport 包装成 `fetch_spec_entries` 需要的 request 可调用对象。"""
    transport = mock_transport(script)
    policy = RetryPolicy(max_attempts=1)  # 单测不重试：脚本长度可控、无等待

    async def request(http_request: HttpRequest) -> HttpResponse:
        return await send_with_retry(transport, http_request, policy)

    return transport, request


async def test_robots_404_means_allowed(mock_transport) -> None:
    transport, request = _requester(mock_transport, [HttpResponse(404)])
    decision = await robots_allows(request, FEED_URL)
    assert decision.allowed is True and "404" in decision.reason
    assert decision.by_rule is False
    assert transport.calls == 1
    assert transport.requests[0].url == ROBOTS_URL


@pytest.mark.parametrize(
    ("response", "expected", "by_rule"),
    [
        (HttpResponse(403), "HTTP 403", False),
        (HttpResponse(200, text="User-agent: *\nDisallow: /"), "明确禁止", True),
    ],
)
async def test_robots_denial_is_fail_closed(mock_transport, response, expected, by_rule) -> None:
    _, request = _requester(mock_transport, [response])
    decision = await robots_allows(request, FEED_URL)
    assert decision.allowed is False
    assert expected in decision.reason
    assert decision.by_rule is by_rule


async def test_robots_network_error_is_fail_closed() -> None:
    async def boom(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("connection reset")

    decision = await robots_allows(boom, FEED_URL)
    assert decision.allowed is False and "请求失败" in decision.reason
    assert decision.by_rule is False


async def test_robots_rejects_non_http_url_without_request(mock_transport) -> None:
    transport, request = _requester(mock_transport, [])
    decision = await robots_allows(request, "ftp://feed.invalid/rss.xml")
    assert decision.allowed is False and "非法" in decision.reason
    assert transport.calls == 0


# ---------------------------------------------------------------------------
# 抓取链路：robots → 缓存 → 条件请求 → 解析
# ---------------------------------------------------------------------------
async def test_fetch_writes_cache_then_replays_with_zero_requests(mock_transport, tmp_path) -> None:
    cache = RssCache(tmp_path, mode="auto")
    transport, request = _requester(
        mock_transport,
        [HttpResponse(404), HttpResponse(200, text=_rss(_item()), headers={"ETag": 'W/"v1"'})],
    )
    first = await fetch_spec_entries(_spec(), request=request, cache=cache)
    assert first.status == "fetched" and len(first.entries) == 1 and first.warnings == ()
    assert transport.calls == 2  # robots + feed
    assert cache.stats()["writes"] == 1

    # readonly 重放：**零网络**（`--dry-run` 的离线保证）
    replay_transport, replay_request = _requester(mock_transport, [])
    second = await fetch_spec_entries(
        _spec(), request=replay_request, cache=RssCache(tmp_path, mode="readonly")
    )
    assert second.status == "cached" and len(second.entries) == 1
    assert replay_transport.calls == 0


async def test_auto_mode_revalidates_with_conditional_request(mock_transport, tmp_path) -> None:
    """`auto` 模式必须协商缓存（否则真实采集永远拿不到新条目）。"""
    cache = RssCache(tmp_path, mode="auto")
    _, request = _requester(
        mock_transport,
        [
            HttpResponse(404),
            HttpResponse(
                200,
                text=_rss(_item()),
                headers={"ETag": 'W/"v1"', "Last-Modified": "Mon, 14 Sep 2026 08:30:00 GMT"},
            ),
        ],
    )
    await fetch_spec_entries(_spec(), request=request, cache=cache)

    transport, second_request = _requester(
        mock_transport,
        [HttpResponse(404), HttpResponse(200, text=_rss(_item("fresh")))],
    )
    result = await fetch_spec_entries(_spec(), request=second_request, cache=cache)
    assert result.status == "fetched"
    assert [e.record_id for e in result.entries] == ["fresh"]
    assert transport.requests[1].headers.get("If-None-Match") == 'W/"v1"'
    assert (
        transport.requests[1].headers.get("If-Modified-Since")
        == "Mon, 14 Sep 2026 08:30:00 GMT"
    )  # 两个校验器都回放（响应头大小写不敏感）


async def test_conditional_request_handles_304(mock_transport, tmp_path) -> None:
    cache = RssCache(tmp_path, mode="auto")
    _, request = _requester(
        mock_transport,
        [HttpResponse(404), HttpResponse(200, text=_rss(_item()), headers={"ETag": 'W/"v1"'})],
    )
    await fetch_spec_entries(_spec(), request=request, cache=cache)

    transport, second_request = _requester(mock_transport, [HttpResponse(404), HttpResponse(304)])
    result = await fetch_spec_entries(_spec(), request=second_request, cache=cache)
    assert result.status == "not_modified"
    assert result.http_status == 304
    # 304 = 源站无更新：条目**改由本地缓存重放**（否则语料导出会莫名变成 0 条）
    assert [entry.record_id for entry in result.entries] == ["post-1"]
    assert any("304" in w and "缓存重放" in w for w in result.warnings)
    assert transport.requests[1].headers.get("If-None-Match") == 'W/"v1"'  # 条件请求生效
    # 304 后缓存仍可离线重放（body 不被清空）
    replay = await fetch_spec_entries(
        _spec(), request=request, cache=RssCache(tmp_path, mode="readonly")
    )
    assert replay.status == "cached" and replay.entries[0].record_id == "post-1"


async def test_304_without_cached_body_yields_no_entries(mock_transport, tmp_path) -> None:
    """首次采集就 304 且本地无缓存体 → 明确 0 条 + 告警（不静默、不报成功假象）。"""
    transport, request = _requester(mock_transport, [HttpResponse(404), HttpResponse(304)])
    result = await fetch_spec_entries(
        _spec(), request=request, cache=RssCache(tmp_path, mode="auto")
    )
    assert result.status == "not_modified" and result.entries == ()
    assert any("本地无缓存体" in w for w in result.warnings)


async def test_readonly_mode_refuses_network_when_cache_misses(mock_transport, tmp_path) -> None:
    transport, request = _requester(mock_transport, [])
    result = await fetch_spec_entries(
        _spec(), request=request, cache=RssCache(tmp_path, mode="readonly")
    )
    assert result.status == "skipped"
    assert any("拒绝联网" in w for w in result.warnings)
    assert transport.calls == 0



async def test_robots_denied_source_is_skipped_before_fetch(mock_transport, tmp_path) -> None:
    transport, request = _requester(
        mock_transport, [HttpResponse(200, text="User-agent: *\nDisallow: /")]
    )
    result = await fetch_spec_entries(_spec(), request=request, cache=RssCache(tmp_path))
    assert result.status == "skipped" and result.entries == ()
    assert any("robots" in w for w in result.warnings)
    assert transport.calls == 1  # 只查了 robots，没碰 feed


async def test_unverifiable_robots_counts_as_failed_not_success(mock_transport, tmp_path) -> None:
    """robots 取不到（403/网络故障）属于"技术性无法验证" → failed（不得静默 SUCCESS）。"""
    transport, request = _requester(mock_transport, [HttpResponse(403)])
    result = await fetch_spec_entries(
        _spec(), request=request, cache=RssCache(tmp_path, mode="off")
    )
    assert result.status == "failed" and result.entries == ()
    assert any("robots" in w for w in result.warnings)
    assert transport.calls == 1


async def test_http_error_and_transport_error_become_failed(mock_transport, tmp_path) -> None:
    _, request = _requester(mock_transport, [HttpResponse(403)])
    cache = RssCache(tmp_path, mode="auto")
    blocked = await fetch_spec_entries(_spec(robots_check=False), request=request, cache=cache)
    assert blocked.status == "failed" and any("HTTP 403" in w for w in blocked.warnings)
    stored = cache.load(RssCache.key_for(FEED_URL))
    assert stored is not None and not stored.ok and "403" in stored.error

    async def boom(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("dns failure")

    broken = await fetch_spec_entries(_spec(), request=boom, cache=RssCache(tmp_path))
    assert broken.status == "failed" and any("请求失败" in w for w in broken.warnings)


async def test_disabled_source_makes_no_request(mock_transport, tmp_path) -> None:
    transport, request = _requester(mock_transport, [])
    result = await fetch_spec_entries(
        _spec(enabled=False), request=request, cache=RssCache(tmp_path)
    )
    assert result.status == "skipped" and transport.calls == 0


async def test_window_filter_and_tz_ambiguous_warning(mock_transport, tmp_path) -> None:
    xml = _rss(
        _item("in-window", pub_date="Mon, 14 Sep 2026 08:30:00 GMT"),
        _item("out-of-window", pub_date="Sat, 01 Aug 2026 08:30:00 GMT"),
        _item("no-tz", pub_date="Mon, 14 Sep 2026 09:00:00"),
    )
    _, request = _requester(mock_transport, [HttpResponse(404), HttpResponse(200, text=xml)])
    result = await fetch_spec_entries(
        _spec(), request=request, cache=RssCache(tmp_path, mode="off"), window=WINDOW
    )
    assert [e.record_id for e in result.entries] == ["in-window", "no-tz"]
    assert result.skipped_by_window == 1
    assert any("超出采集窗口" in w for w in result.warnings)
    assert any("时区不明确" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 采集器装配：启用过滤 / 字段映射（docs/11 §1.1）/ 健康检查 / 统计
# ---------------------------------------------------------------------------
def _source(*, config: dict[str, object] | None = None, name: str = "rss-source"):
    from types import SimpleNamespace

    return SimpleNamespace(name=name, base_url=FEED_URL, config_json=config or {})


def test_collector_requires_at_least_one_enabled_source(tmp_path) -> None:
    with pytest.raises(CollectorError, match="没有启用的 RSS 源"):
        RssCollector(_source(), specs=[_spec(enabled=False)], cache_dir=tmp_path)


def test_collector_payload_mapping_matches_docs11_contract() -> None:
    entries, _ = parse_feed_entries(_rss(_item(author="李四")), spec=_spec())
    payload = RssCollector._to_payload(_spec(), entries[0])

    assert payload.item_type is RawItemType.NEWS
    assert payload.source_record_id == "post-1"
    assert payload.title == "黄金短线看涨"
    assert payload.content_text == "金价 突破 3400，减仓后继续持有。"
    assert payload.source_url == "https://feed.invalid/post-1"
    assert payload.published_at == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    raw = payload.raw_json or {}
    assert raw["feed_id"] == "feed_one"
    assert raw["feed_url"] == FEED_URL
    assert raw["source_name"] == "test_feed"
    assert raw["author_name"] == "李四"
    assert raw["has_media"] is False
    assert raw["published_tz_ambiguous"] is False
    assert raw["parser_version"] == RSS_PARSER_VERSION
    assert raw["source_type"] == "NEWS"  # 默认按常规新闻源（进观点语料）

    event_payload = RssCollector._to_payload(_spec(source_type="EVENT"), entries[0])
    assert (event_payload.raw_json or {})["source_type"] == "EVENT"  # 事件源必须可被下游过滤


def test_collector_payload_leaves_published_at_empty_when_tz_unknown() -> None:
    entries, _ = parse_feed_entries(
        _rss(_item(pub_date="Mon, 14 Sep 2026 08:30:00")), spec=_spec()
    )
    payload = RssCollector._to_payload(_spec(), entries[0])
    assert payload.published_at is None  # 落库时 effective_at = collected_at
    assert (payload.raw_json or {})["published_tz_ambiguous"] is True


def test_collector_rejects_missing_config_file(tmp_path) -> None:
    with pytest.raises(CollectorError, match="源清单不存在"):
        RssCollector(
            _source(config={"sources_path": str(tmp_path / "nope.json")}), cache_dir=tmp_path
        )


async def test_collector_health_check_reads_local_config_only(tmp_path) -> None:
    config = tmp_path / "rss_sources.json"
    config.write_text(
        json.dumps({"sources": [{"id": "a", "name": "A", "url": FEED_URL, "enabled": True}]}),
        encoding="utf-8",
    )
    collector = RssCollector(_source(config={"sources_path": str(config)}), cache_dir=tmp_path)
    health = await collector._do_health_check()  # noqa: SLF001 - 无网络的本地检查
    assert health.healthy is True
    assert health.details["enabled_sources"] == ["a"]
    assert health.details["cache"]["mode"] == "auto"

    config.unlink()  # 清单被移走后必须转为不健康（可观测）
    unhealthy = await collector._do_health_check()  # noqa: SLF001
    assert unhealthy.healthy is False and "源清单不可用" in (unhealthy.message or "")


def test_collector_stats_reports_cache_and_sources(tmp_path) -> None:
    collector = RssCollector(
        _source(config={"min_records_per_run": 0}),
        specs=[_spec(id="a"), _spec(id="b", enabled=False)],
        cache_dir=tmp_path,
    )
    stats = collector.stats()
    assert stats["enabled_sources"] == 1 and stats["disabled_sources"] == 1
    assert stats["cache"]["mode"] == "auto"
    assert stats["parser_version"] == RSS_PARSER_VERSION
    # 团队批复语义：min_records_per_run 来自 sources.config_json（基类存在 _min_records）
    assert collector._min_records == 0  # noqa: SLF001 - 只读校验配置生效


def test_repo_config_loads_into_collector_paths() -> None:
    """仓库清单可被采集器加载，且**只加载人工批准启用的源**。"""
    specs, _ = load_rss_sources("config/rss_sources.json")
    enabled = {s.id for s in specs if s.enabled}
    assert enabled == APPROVED_ENABLED_SOURCES  # 未验证/未获批准的源一个都不启用


async def test_fetch_result_exposes_robots_and_http_status(mock_transport, tmp_path) -> None:
    """观测字段：robots 判定与 HTTP 状态必须回传（CLI《源验证报告》依赖它们）。"""
    transport = mock_transport(
        [
            HttpResponse(status=200, text="User-agent: *\nAllow: /\n"),
            HttpResponse(status=200, text=_rss(_item())),
        ]
    )
    result = await fetch_spec_entries(
        _spec(),
        request=transport.send,
        cache=RssCache(tmp_path),
        max_items=5,
    )

    assert result.status == "fetched"
    assert result.http_status == 200
    assert result.robots is not None
    assert result.robots.allowed is True and result.robots.by_rule is False
    assert "robots.txt" in result.robots.reason
