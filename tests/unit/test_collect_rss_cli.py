"""`scripts/collect_rss.py` CLI 测试（**零网络**：`--fixture` 指向本地 XML + Mock 传输）。

覆盖团队要求：
1. `--dry-run`（缺省即 dry-run）：**不发任何请求、不写 CSV/数据库**，只做预览；
2. `--fixture` 离线跑通全链路（解析 → 字段映射 → CSV 导出）；
3. 导出的 CSV 列与 `docs/11 §1.1` 列契约一致（含 `effective_at` 回落规则）；
4. 源全部禁用 / 参数冲突 / 无缓存无 fixture → 明确退出码（2/3），绝不静默成功；
5. **冒烟硬性要求（2026-09-14）**：真实请求间隔 ≥1s（硬下限）、每个源单独落一份
   原始 JSON（含 robots 判定 / HTTP 状态 / 原始响应体）、robots 禁止 → 不抓 feed 只留痕。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import collect_rss
from scripts.collect_rss import CSV_COLUMNS, MIN_REQUEST_INTERVAL, RateLimiter, main
from src.collectors.transport import HttpRequest, HttpResponse

pytestmark = pytest.mark.unit

FEED_URL = "https://feed.invalid/rss.xml"

RSS_TWO_ITEMS = (
    '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
    "<title>Gold</title>"
    "<item><title><![CDATA[黄金 3400 上方继续看多]]></title>"
    "<link>https://feed.invalid/a</link><guid>a</guid>"
    "<description><![CDATA[<p>3400 上方减仓后<strong>继续持有</strong>。</p>]]></description>"
    "<pubDate>Mon, 14 Sep 2026 08:30:00 GMT</pubDate></item>"
    "<item><title>无时间戳的快讯</title><link>https://feed.invalid/b</link><guid>b</guid>"
    "<description><![CDATA[金价短线回落。]]></description></item>"
    "</channel></rss>"
)


def _write_sources(
    tmp_path: Path,
    *,
    enabled: bool = True,
    sources_id: str = "feed_a",
    source_type: str | None = None,
    notes: str | None = None,
) -> Path:
    source: dict[str, object] = {
        "id": sources_id,
        "name": "测试源",
        "url": FEED_URL,
        "source_name": "test_feed",
        "author_name": "测试作者",
        "language": "zh",
        "enabled": enabled,
    }
    if source_type is not None:
        source["source_type"] = source_type
    if notes is not None:
        source["notes"] = notes
    path = tmp_path / "rss_sources.json"
    path.write_text(
        json.dumps({"sources": [source]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_fixture(tmp_path: Path, sources_id: str = "feed_a") -> Path:
    directory = tmp_path / "fixtures"
    directory.mkdir(exist_ok=True)
    (directory / f"{sources_id}.xml").write_text(RSS_TWO_ITEMS, encoding="utf-8")
    return directory


def test_dry_run_with_fixture_previews_without_writing(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    fixtures = _write_fixture(tmp_path)
    out = tmp_path / "should_not_exist.csv"
    stats = tmp_path / "stats.json"

    code = main(
        [
            "--sources",
            str(sources),
            "--fixture",
            str(fixtures),
            "--dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            "",  # 测试不得往仓库 logs/ 写文件
            "--verify-log",
            "",
            "--out",
            str(out),
            "--json-out",
            str(stats),
        ]
    )

    assert code == 0
    assert not out.exists()  # dry-run 不写 CSV
    payload = json.loads(stats.read_text(encoding="utf-8"))
    assert payload["mode"] == "fixture"
    assert payload["entries"] == 2
    assert payload["per_source"]["feed_a"]["status"] == "fetched"
    assert payload["per_source"]["feed_a"]["entries"] == 2


def test_offline_export_matches_docs11_column_contract(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    fixtures = _write_fixture(tmp_path)
    out = tmp_path / "real_posts_2026-09-14.csv"

    code = main(
        [
            "--sources",
            str(sources),
            "--fixture",
            str(fixtures),
            "--no-dry-run",  # 有 fixture → 仍不联网
            "--out",
            str(out),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            "",  # 测试不得往仓库 logs/ 写文件
            "--verify-log",
            "",
        ]
    )

    assert code == 0
    assert out.exists()
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == list(CSV_COLUMNS)  # 与 docs/11 §1.1 契约逐列一致
    for required in ("id", "content", "published_at", "effective_at", "source", "author_name"):
        assert required in rows[0]

    dated, undated = rows
    assert dated["id"] == "a"
    assert dated["content"] == "3400 上方减仓后 继续持有 。"
    assert dated["published_at"] == "2026-09-14T08:30:00+00:00"
    assert dated["effective_at"] == dated["published_at"]
    assert dated["source"] == "test_feed"
    assert dated["author_name"] == "测试作者"
    assert dated["url"] == "https://feed.invalid/a"
    assert dated["has_media"] == "false"

    assert undated["published_at"] == ""
    assert undated["effective_at"] == undated["collected_at"]  # 无发布时间 → 回落
    assert "无可用发布时间" in undated["notes_collect"]


def test_exit_code_2_when_no_source_enabled(tmp_path: Path, capsys) -> None:
    sources = _write_sources(tmp_path, enabled=False)
    code = main(["--sources", str(sources), "--cache-dir", str(tmp_path / "cache")])
    assert code == 2
    assert "没有启用的源" in capsys.readouterr().err


def test_exit_code_2_on_conflicting_flags(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    code = main(
        ["--sources", str(sources), "--dry-run", "--no-dry-run", "--cache-dir", str(tmp_path / "c")]
    )
    assert code == 2


def test_dry_run_without_fixture_refuses_network(tmp_path: Path) -> None:
    """无 fixture 且缓存未命中 → readonly 拒绝联网 → 退出码 3（没有任何可用条目）。"""
    sources = _write_sources(tmp_path)
    code = main(
        [
            "--sources",
            str(sources),
            "--dry-run",
            "--cache-dir",
            str(tmp_path / "empty-cache"),
            "--raw-dir",
            "",  # 测试不得往仓库 logs/ 写文件
            "--verify-log",
            "",
            "--out",
            str(tmp_path / "never.csv"),
        ]
    )
    assert code == 3
    assert not (tmp_path / "never.csv").exists()


def test_exit_code_2_when_config_missing(tmp_path: Path) -> None:
    code = main(["--sources", str(tmp_path / "nope.json"), "--cache-dir", str(tmp_path / "c")])
    assert code == 2


# ---------------------------------------------------------------------------
# 冒烟硬性要求：限速 ≥1s / 每源原始 JSON / robots+HTTP 可追溯（**全部零网络**）
# ---------------------------------------------------------------------------
class _FakeTime:
    """可推进的假时钟（限速测试用：**不真正等待**）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _NoWaitLimiter(RateLimiter):
    """测试用限速器：保留硬下限计算，但不真正 sleep（避免单测白等 1 秒）。"""

    def __init__(self, min_interval: float) -> None:
        super().__init__(min_interval, clock=lambda: 0.0)

    async def wait(self) -> float:
        self.waits.append(0.0)
        return 0.0


def _robot_allow() -> HttpResponse:
    return HttpResponse(status=200, text="User-agent: *\nAllow: /\n")


def _robot_deny() -> HttpResponse:
    return HttpResponse(status=200, text="User-agent: *\nDisallow: /\n")


def _feed_ok() -> HttpResponse:
    return HttpResponse(status=200, text=RSS_TWO_ITEMS, headers={"ETag": '"v1"'})


async def test_rate_limiter_enforces_interval_and_hard_floor() -> None:
    fake = _FakeTime()
    limiter = RateLimiter(0.2, sleep=fake.sleep, clock=fake.clock)

    assert limiter.min_interval == MIN_REQUEST_INTERVAL  # 低于硬下限 → 抬到下限
    assert await limiter.wait() == 0.0  # 首个请求无需等待
    assert await limiter.wait() == pytest.approx(MIN_REQUEST_INTERVAL)  # 紧接着 → 补足间隔
    fake.advance(2.0)
    assert await limiter.wait() == 0.0  # 已间隔 2s → 无需等待
    assert fake.sleeps == [MIN_REQUEST_INTERVAL]
    assert limiter.waits == [0.0, MIN_REQUEST_INTERVAL, 0.0]


def test_csv_notes_exclude_config_metadata(
    tmp_path: Path, monkeypatch, mock_transport
) -> None:
    """TD-34：`notes_collect` 只留"本次采集产生的质量标记"，绝不混入源配置说明（标注噪音）。"""
    sources = _write_sources(tmp_path, notes="配置类说明：2026-09-13 冒烟通过 / 源模式=live")
    out = tmp_path / "posts.csv"
    transport = mock_transport([_robot_allow(), _feed_ok()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            "",
            "--verify-log",
            "",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    notes = [row["notes_collect"] for row in rows]
    assert all("冒烟通过" not in note and "源模式" not in note for note in notes)
    assert "无可用发布时间" in notes[1]  # 采集侧质量标记仍保留（第二条无 pubDate）


def test_event_source_is_marked_in_csv_and_raw_json(
    tmp_path: Path, monkeypatch, mock_transport
) -> None:
    """`source_type=EVENT` 必须落进 CSV 备注与原始 JSON —— 下游据此把事件源排除出观点语料。"""
    sources = _write_sources(tmp_path, source_type="EVENT")
    raw_dir = tmp_path / "raw"
    out = tmp_path / "posts.csv"
    transport = mock_transport([_robot_allow(), _feed_ok()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            str(raw_dir),
            "--verify-log",
            "",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    with out.open(encoding="utf-8-sig", newline="") as handle:
        row = next(iter(csv.DictReader(handle)))
    assert "采集用途=EVENT" in row["notes_collect"]
    raw_file = next(iter(raw_dir.glob("feed_a_*.json")))
    assert json.loads(raw_file.read_text(encoding="utf-8"))["spec"]["source_type"] == "EVENT"


def test_disabled_sentinels_turn_off_file_outputs(tmp_path: Path) -> None:
    """`off` / `none` / `-` 与空串等价（PowerShell 5.1 会吞掉空字符串参数，必须有哨兵）。"""
    assert collect_rss._optional_path("off") is None  # noqa: SLF001 - 直接锁定哨兵语义
    assert collect_rss._optional_path("") is None  # noqa: SLF001
    assert collect_rss._optional_path(" none ") is None  # noqa: SLF001
    assert collect_rss._optional_path("-") is None  # noqa: SLF001
    assert collect_rss._optional_path(str(tmp_path)) == tmp_path  # noqa: SLF001


def test_live_run_records_robots_http_and_raw_json(
    tmp_path: Path, monkeypatch, mock_transport, capsys
) -> None:
    """真实模式（Mock 传输）：robots 判定 + HTTP 状态 + **每源一份原始 JSON**。"""
    sources = _write_sources(tmp_path)
    raw_dir = tmp_path / "raw"
    stats = tmp_path / "stats.json"
    out = tmp_path / "posts.csv"
    verify_log = tmp_path / "verify.jsonl"
    transport = mock_transport([_robot_allow(), _feed_ok()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            str(raw_dir),
            "--verify-log",
            str(verify_log),
            "--json-out",
            str(stats),
            "--out",
            str(out),
            "--per-feed-limit",
            "2",
        ]
    )

    assert code == 0
    assert transport.calls == 2  # robots.txt + feed（各一次）
    payload = json.loads(stats.read_text(encoding="utf-8"))
    per_source = payload["per_source"]["feed_a"]
    assert per_source["status"] == "fetched"
    assert per_source["http_status"] == 200
    assert per_source["robots"]["allowed"] is True
    assert payload["min_interval"] == MIN_REQUEST_INTERVAL
    assert "robots=允许" in capsys.readouterr().out

    raw = json.loads(Path(per_source["raw_json"]).read_text(encoding="utf-8"))
    assert raw["robots"]["allowed"] is True and raw["robots"]["by_rule"] is False
    assert raw["fetch"]["http_status"] == 200 and raw["fetch"]["entries"] == 2
    assert raw["cache"]["etag"] == '"v1"'
    assert "<item>" in raw["raw_body"] and raw["raw_body_chars"] > 0
    assert [entry["id"] for entry in raw["entries"]] == ["a", "b"]
    assert raw["entries"][0]["published_raw"] == "Mon, 14 Sep 2026 08:30:00 GMT"
    assert raw["entries"][1]["published_at"] is None  # 无 pubDate → 不猜测

    # 逐源验证日志（JSONL）：robots 检查结果必须留痕（团队要求）
    line = json.loads(verify_log.read_text(encoding="utf-8").strip())
    assert line["source_id"] == "feed_a" and line["feed_url"] == FEED_URL
    assert line["robots_allowed"] is True and line["robots_by_rule"] is False
    assert line["robots_reason"] == "robots.txt 允许"
    assert line["http_status"] == 200 and line["status"] == "fetched" and line["entries"] == 2


def test_live_run_skips_source_when_robots_denies(
    tmp_path: Path, monkeypatch, mock_transport
) -> None:
    """robots 明确禁止 → **不抓 feed**（只发 robots 一次请求），判定与原因全部留痕。"""
    sources = _write_sources(tmp_path)
    raw_dir = tmp_path / "raw"
    stats = tmp_path / "stats.json"
    transport = mock_transport([_robot_deny()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            str(raw_dir),
            "--verify-log",
            "",
            "--json-out",
            str(stats),
            "--out",
            str(tmp_path / "posts.csv"),
        ]
    )

    assert code == 3  # 没有可用条目
    assert transport.calls == 1  # 只请求了 robots.txt
    per_source = json.loads(stats.read_text(encoding="utf-8"))["per_source"]["feed_a"]
    assert per_source["status"] == "skipped"
    assert per_source["robots"]["allowed"] is False
    assert per_source["robots"]["by_rule"] is True
    raw = json.loads(Path(per_source["raw_json"]).read_text(encoding="utf-8"))
    assert raw["robots"]["by_rule"] is True and raw["raw_body"] == ""


def test_feed_limit_alias_and_min_interval_floor_warning(
    tmp_path: Path, monkeypatch, mock_transport, capsys
) -> None:
    """`--feed-limit` = `--per-feed-limit` 别名；`--min-interval` 低于 1s → 抬升并告警。"""
    sources = _write_sources(tmp_path)
    out = tmp_path / "posts.csv"
    transport = mock_transport([_robot_allow(), _feed_ok()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--feed-limit",
            "1",
            "--min-interval",
            "0.1",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            "",
            "--verify-log",
            "",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert "低于硬下限" in capsys.readouterr().out
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1  # --feed-limit 生效
    assert rows[0]["id"] == "a"


class _ClosableTransport:
    """最小传输实现 + ``close()`` 记录（回归：真实抓取必须关闭 aiohttp 会话）。"""

    def __init__(self, script: list[HttpResponse]) -> None:
        self._script = list(script)
        self.closed = False
        self.calls = 0

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.calls += 1
        return self._script.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_live_run_closes_the_http_session(tmp_path: Path, monkeypatch) -> None:
    """真实抓取结束（含失败）后必须关闭 HTTP 会话，否则 aiohttp 会打印 Unclosed 警告。"""
    sources = _write_sources(tmp_path)
    transport = _ClosableTransport([_robot_allow(), _feed_ok()])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr(collect_rss, "RateLimiter", _NoWaitLimiter)

    code = main(
        [
            "--sources",
            str(sources),
            "--no-dry-run",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--raw-dir",
            "",
            "--verify-log",
            "",
            "--out",
            str(tmp_path / "posts.csv"),
        ]
    )

    assert code == 0
    assert transport.calls == 2
    assert transport.closed is True
