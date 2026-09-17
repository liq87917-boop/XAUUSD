"""``NewsCollector`` 解析层单元测试（纯函数，不触数据库、不触网络）。

覆盖团队批复的关键点：
- 标准 RSS 2.0 / Atom / RDF 解析；
- **时区强制转 UTC**（RFC822 带偏移、ISO8601 带偏移/Z）；
- 时区不明确（naive）必须被标记为 ambiguous，**绝不猜测**；
- 时间无法解析 / 空值要带 reason（供上层告警，不静默）；
- XML 格式异常、DOCTYPE（XXE 防护）、超大响应、未知根元素；
- CSV 兜底解析器；
- content_hash 是跨源去重的基础。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from database.models import Source
from database.models.enums import SourceType
from src.collectors.errors import CollectorError
from src.collectors.news import (
    CSV_PARSER_VERSION,
    NEWS_PARSER_VERSION,
    NewsCollector,
    parse_feed_timestamp,
    parse_news_csv,
    parse_rss_feed,
    strip_html,
)
from src.collectors.types import CollectWindow, RawItemPayload

pytestmark = pytest.mark.unit

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <link>https://feed.invalid/</link>
    <item>
      <title>{title}</title>
      <link>https://feed.invalid/a</link>
      <description>{description}</description>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_date}</pubDate>
      <category>{category}</category>
    </item>
  </channel>
</rss>
"""


def _rss(
    *,
    title: str = "Gold rises on Fed bets",
    description: str = "<p>Gold <b>rises</b> 1%.</p>",
    guid: str = "item-1",
    pub_date: str = "Mon, 07 Sep 2026 12:00:00 GMT",
    category: str = "Gold",
) -> str:
    return RSS_TEMPLATE.format(
        title=title, description=description, guid=guid, pub_date=pub_date, category=category
    )


def _atom(*, published: str = "2026-09-07T12:00:00Z") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Fed Press Releases</title>
  <entry>
    <title>Federal Reserve issues FOMC statement</title>
    <link href="https://www.federalreserve.gov/newsevents/pressreleases/x.htm"/>
    <id>tag:federalreserve.gov,2026-09-07:/newsevents/pressreleases/x.htm</id>
    <updated>{published}</updated>
    <published>{published}</published>
    <summary>Committee decides to maintain the target range.</summary>
    <category term="Monetary Policy"/>
  </entry>
</feed>"""


def _source(**overrides: object) -> Source:
    defaults: dict[str, object] = {
        "name": "news-unit",
        "source_type": SourceType.NEWS,
        "base_url": "https://news.invalid",
        "enabled": True,
        "config_json": {"feeds": ["https://feed.invalid/rss"]},
    }
    defaults.update(overrides)
    return Source(**defaults)  # type: ignore[arg-type]


def _window() -> CollectWindow:
    return CollectWindow(
        start_at=datetime(2026, 9, 7, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 9, 8, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# 时间解析（UTC 强制 + 歧义不猜测）
# ---------------------------------------------------------------------------
def test_rfc822_with_gmt_is_parsed_as_utc() -> None:
    parsed = parse_feed_timestamp("Mon, 07 Sep 2026 12:00:00 GMT")

    assert parsed.tz_ambiguous is False
    assert parsed.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert parsed.raw == "Mon, 07 Sep 2026 12:00:00 GMT"


def test_rfc822_with_offset_is_converted_to_utc() -> None:
    """★ UTC 转换正确性：-0400 的 12:00 必须变成 16:00Z。"""
    parsed = parse_feed_timestamp("Mon, 07 Sep 2026 12:00:00 -0400")

    assert parsed.tz_ambiguous is False
    assert parsed.value == datetime(2026, 9, 7, 16, 0, tzinfo=UTC)
    assert parsed.value is not None
    assert parsed.value.tzinfo == UTC


def test_iso8601_zulu_is_parsed_as_utc() -> None:
    parsed = parse_feed_timestamp("2026-09-07T12:00:00Z")

    assert parsed.tz_ambiguous is False
    assert parsed.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_iso8601_with_offset_is_converted_to_utc() -> None:
    """★ UTC 转换正确性：+08:00 的 20:00 必须变成 12:00Z。"""
    parsed = parse_feed_timestamp("2026-09-07T20:00:00+08:00")

    assert parsed.tz_ambiguous is False
    assert parsed.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [
        "Mon, 07 Sep 2026 12:00:00",  # RFC822 无时区
        "2026-09-07T12:00:00",  # ISO8601 naive
        "2026-09-07 12:00:00",
    ],
)
def test_ambiguous_timezone_is_flagged_and_not_guessed(value: str) -> None:
    """时区不明确 → 必须标记 ambiguous 且不返回时间（由采集器置空 published_at）。"""
    parsed = parse_feed_timestamp(value)

    assert parsed.tz_ambiguous is True
    assert parsed.value is None
    assert parsed.reason in {"rfc822_without_timezone", "iso8601_without_timezone"}


@pytest.mark.parametrize("value", ["", "   ", None, "not-a-date", "2026-13-45T99:99:99Z"])
def test_unparsable_or_empty_timestamps_carry_reason(value: str | None) -> None:
    parsed = parse_feed_timestamp(value)

    assert parsed.value is None
    assert parsed.tz_ambiguous is False
    assert parsed.reason in {"empty", "unparsable"}


def test_strip_html_removes_tags_and_collapses_whitespace() -> None:
    assert strip_html("<p>Gold <b>rises</b>\n  1%</p>") == "Gold rises 1%"
    assert strip_html("   ") is None
    assert strip_html(None) is None


def test_strip_html_decodes_entities_from_real_feeds() -> None:
    """真实源大量用数字/命名实体（`&#8217;`/`&#8220;`/`&#8230;`）：必须解码，否则污染语料。

    回归背景：2026-09-13 冒烟实测 `fredblog.stlouisfed.org/feed/` 正文含 63 处实体，
    落到 CSV `content` 里成了 `New York Fed&#8217;s`。
    """
    assert strip_html("New York Fed&#8217;s data") == "New York Fed’s data"
    assert strip_html("&#8220;time saved,&#8221;") == "“time saved,”"
    assert strip_html("more &#8230;") == "more …"
    assert strip_html("Tom &amp; Jerry &lt;p&gt;literal&lt;/p&gt;") == "Tom & Jerry <p>literal</p>"
    assert strip_html("<p>a&nbsp;b</p>") == "a b"


# ---------------------------------------------------------------------------
# RSS / Atom 解析
# ---------------------------------------------------------------------------
def test_parse_rss_2_0_item() -> None:
    items = parse_rss_feed(_rss())

    assert len(items) == 1
    item = items[0]
    assert item.record_id == "item-1"
    assert item.title == "Gold rises on Fed bets"
    assert item.summary == "Gold rises 1%."  # HTML 已清理
    assert item.link == "https://feed.invalid/a"
    assert item.categories == ("Gold",)
    assert item.published.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_parse_atom_entry_with_namespace() -> None:
    items = parse_rss_feed(_atom())

    assert len(items) == 1
    item = items[0]
    assert item.title == "Federal Reserve issues FOMC statement"
    assert item.link == "https://www.federalreserve.gov/newsevents/pressreleases/x.htm"
    assert item.record_id.startswith("tag:federalreserve.gov")
    assert item.categories == ("Monetary Policy",)
    assert item.published.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_parse_rss_falls_back_to_content_hash_for_record_id() -> None:
    xml = """<rss version="2.0"><channel><item>
      <title>No guid here</title><description>body text</description>
      <pubDate>Mon, 07 Sep 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""

    items = parse_rss_feed(xml)

    assert len(items) == 1
    assert len(items[0].record_id) == 64  # SHA256 兜底，稳定可复现


def test_parse_rss_skips_empty_entries() -> None:
    xml = """<rss version="2.0"><channel>
      <item></item>
      <item><title>Real item</title><guid>g-2</guid>
      <pubDate>Mon, 07 Sep 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""

    items = parse_rss_feed(xml)

    assert len(items) == 1
    assert items[0].title == "Real item"


def test_parse_rss_handles_missing_timezone_with_flag() -> None:
    items = parse_rss_feed(_rss(pub_date="Mon, 07 Sep 2026 12:00:00"))

    assert items[0].published.tz_ambiguous is True
    assert items[0].published.value is None


# ---------------------------------------------------------------------------
# 异常与安全
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "xml_text",
    ["<rss><channel><item></rss>", "not xml at all", "<rss><channel><item>"],
)
def test_malformed_xml_raises_collector_error(xml_text: str) -> None:
    with pytest.raises(CollectorError, match="XML 解析失败"):
        parse_rss_feed(xml_text)


def test_empty_feed_raises_collector_error() -> None:
    with pytest.raises(CollectorError, match="为空"):
        parse_rss_feed("   ")


def test_doctype_is_rejected_to_prevent_xxe() -> None:
    xml = """<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&xxe;</title></item></channel></rss>"""

    with pytest.raises(CollectorError, match="DOCTYPE"):
        parse_rss_feed(xml)


def test_oversized_feed_is_rejected(monkeypatch) -> None:
    import src.collectors.news as news_module

    monkeypatch.setattr(news_module, "MAX_FEED_BYTES", 100)

    with pytest.raises(CollectorError, match="过大"):
        parse_rss_feed(_rss(description="x" * 500))


def test_unsupported_root_element_raises() -> None:
    with pytest.raises(CollectorError, match="不支持的 feed 根元素"):
        parse_rss_feed("<opml><body/></opml>")


# ---------------------------------------------------------------------------
# CSV 兜底解析器
# ---------------------------------------------------------------------------
def test_parse_news_csv_reads_required_columns() -> None:
    csv_text = (
        "record_id,title,summary,link,published_at,category\n"
        "n-1,Gold up,gold body,https://news.invalid/1,2026-09-07T12:00:00Z,Commodities\n"
    )

    items = parse_news_csv(csv_text)

    assert len(items) == 1
    assert items[0].record_id == "n-1"
    assert items[0].summary == "gold body"
    assert items[0].categories == ("Commodities",)
    assert items[0].published.value == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_parse_news_csv_flags_naive_timestamps() -> None:
    csv_text = "record_id,title,published_at\nn-2,No timezone,2026-09-07 12:00:00\n"

    items = parse_news_csv(csv_text)

    assert items[0].published.tz_ambiguous is True
    assert items[0].published.value is None


@pytest.mark.parametrize(
    ("csv_text", "expected"),
    [
        ("", "为空"),
        ("title,published_at\na,b\n", "缺少必需列"),
        ("record_id,title\na,b\n", "缺少必需列"),
    ],
)
def test_parse_news_csv_rejects_invalid_input(csv_text: str, expected: str) -> None:
    with pytest.raises(CollectorError, match=expected):
        parse_news_csv(csv_text)


# ---------------------------------------------------------------------------
# 采集器配置与游标
# ---------------------------------------------------------------------------
def test_collector_requires_feeds_or_csv_path() -> None:
    with pytest.raises(CollectorError, match="未配置数据源"):
        NewsCollector(_source(config_json={"collector": "news_collector"}))


def test_collector_rss_mode_targets_follow_config_order() -> None:
    collector = NewsCollector(
        _source(config_json={"feeds": ["https://a.invalid/rss", "https://b.invalid/atom"]})
    )

    assert collector.parser_version == NEWS_PARSER_VERSION
    assert collector._targets == (  # noqa: SLF001 - 游标顺序即抓取顺序，属契约
        "https://a.invalid/rss",
        "https://b.invalid/atom",
    )
    assert collector._next_cursor(0) == {  # noqa: SLF001
        "target_index": 1,
        "target": "https://b.invalid/atom",
    }
    assert collector._next_cursor(1) is None  # noqa: SLF001


def test_collector_csv_mode_takes_precedence() -> None:
    collector = NewsCollector(
        _source(
            config_json={
                "feeds": ["https://a.invalid/rss"],
                "csv_path": "data/raw/news_archive.csv",
            }
        )
    )

    assert collector.parser_version == CSV_PARSER_VERSION
    assert collector._targets == ("data/raw/news_archive.csv",)  # noqa: SLF001


def test_collector_window_filter_keeps_ambiguous_items() -> None:
    window = _window()
    in_window = parse_rss_feed(_rss(pub_date="Mon, 07 Sep 2026 06:00:00 GMT"))[0]
    out_of_window = parse_rss_feed(_rss(pub_date="Tue, 01 Sep 2026 06:00:00 GMT"))[0]
    ambiguous = parse_rss_feed(_rss(pub_date="Mon, 07 Sep 2026 06:00:00"))[0]

    assert NewsCollector._in_window(in_window, window) is True  # noqa: SLF001
    assert NewsCollector._in_window(out_of_window, window) is False  # noqa: SLF001
    assert NewsCollector._in_window(ambiguous, window) is True  # noqa: SLF001


def test_content_hash_is_stable_across_sources_for_same_article() -> None:
    """跨源转载去重的基础：同一篇报道即使 guid 不同，content_hash 也必须一致。"""
    from database.models.enums import RawItemType

    first = RawItemPayload(
        source_record_id="guid-a",
        item_type=RawItemType.NEWS,
        title="Gold rises on Fed bets",
        content_text="Gold rises 1%.",
    )
    second = RawItemPayload(
        source_record_id="guid-b",
        item_type=RawItemType.NEWS,
        title="  gold   rises on fed bets ",
        content_text="Gold rises 1%.",
    )

    assert first.resolved_content_hash() == second.resolved_content_hash()


