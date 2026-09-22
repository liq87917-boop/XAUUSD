"""RSS 采集 CLI：把真实 RSS 源抓成 `docs/11 §1.1` 列契约的 CSV（或落库）。

两种运行方式：

1. **导出 CSV（本次验收主路径）**：不碰数据库，直接产出
   ``logs/real_posts_<日期>.csv``（列 = `docs/11 §1.1` 契约），供
   `scripts/sample_annotation_set.py --input-csv` → 人工标注 → 验收链路使用；
2. **落库（可选）**：``--to-db`` 走完整框架（`run_collector` → `raw_items` 幂等
   + `collector_runs` 审计），需要可用的数据库与迁移。

安全与成本：
- **默认 `--dry-run`**：不发任何真实请求（`--fixture` 指向本地 XML，或 `--cache-mode readonly`
  重放缓存），只打印"计划抓什么 / 解析出多少条 / 字段预览"；
- 真实抓取必须显式加 ``--no-dry-run``；只抓 robots.txt 允许的源，30s 超时 + 3 次重试；
- 缓存目录默认 ``logs/rss_cache``（`.gitignore` 已覆盖），复跑零请求。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout  # noqa: E402
from src.collectors.rss_cache import RssCache  # noqa: E402
from src.collectors.rss_collector import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    DEFAULT_SOURCE_TYPE,
    DEFAULT_SOURCES_PATH,
    RSS_PARSER_VERSION,
    FeedFetchResult,
    RssEntry,
    RssSourceSpec,
    fetch_spec_entries,
    load_rss_sources,
    parse_feed_entries,
)
from src.collectors.transport import (  # noqa: E402
    AiohttpTransport,
    HttpRequest,
    HttpResponse,
    Transport,
    send_with_retry,
)

DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "logs"
#: 每个源的**单独原始 JSON**落盘目录（复盘用；`--raw-dir ""` 可关闭）
DEFAULT_RAW_DIR: Final[Path] = DEFAULT_OUT_DIR / "rss_raw"
#: 逐源验证日志（JSONL，append-only：robots 判定 / HTTP 状态 / 条目数；`--verify-log ""` 可关闭）
DEFAULT_VERIFY_LOG: Final[Path] = DEFAULT_OUT_DIR / "rss_verification.log"
#: 真实抓取时"任意两次请求"的最小间隔（秒）——**硬下限**，命令行给更小的值也会被抬到该值
MIN_REQUEST_INTERVAL: Final[float] = 1.0
#: 表示"关闭某条落盘"的取值（PowerShell 5.1 会吞掉空字符串参数，故需显式哨兵）
_DISABLED_VALUES: Final[frozenset[str]] = frozenset({"", "-", "none", "null", "off"})
#: 导出 CSV 的列（= `docs/11 §1.1` 契约：`content`/`published_at`/`source`/…）
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "content",
    "title",
    "published_at",
    "effective_at",
    "source",
    "author_name",
    "url",
    "has_media",
    "language",
    "category",
    "collected_at",
    "notes_collect",
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/collect_rss.py",
        description="抓取 RSS 源 → logs/real_posts_<日期>.csv（列契约见 docs/11 §1.1）",
    )
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES_PATH), help="源清单 JSON")
    parser.add_argument("--source", action="append", default=None, help="只跑指定源 id（可多次）")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少条（0 = 不限）")
    parser.add_argument(
        "--per-feed-limit",
        "--feed-limit",
        type=int,
        default=None,
        help="单源最多取多少条（`--feed-limit` 为等价别名）",
    )
    parser.add_argument("--out", default=None, help="导出 CSV（默认 logs/real_posts_<日期>.csv）")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="缓存目录")
    parser.add_argument(
        "--cache-mode",
        choices=("auto", "readonly", "refresh", "off"),
        default="auto",
        help="auto=命中即用/未命中抓取；readonly=只读（未命中拒绝联网）；refresh=忽略旧缓存",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="本地 feed 目录（**零网络**）：每个源按 <源id>.xml 读取，配合 --dry-run 使用",
    )
    parser.add_argument("--to-db", action="store_true", help="改为落库（审计留痕）")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="落库时只保留最近 N 天的条目（默认 90；必须大于 0）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览：不写 CSV/数据库，不发起真实请求（缺省即 dry-run）",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="显式允许真实抓取（与 --dry-run 互斥）",
    )
    parser.add_argument("--json-out", default=None, help="把运行统计写成 JSON（可选）")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=MIN_REQUEST_INTERVAL,
        help=(
            f"任意两次真实请求的最小间隔（秒，硬下限 {MIN_REQUEST_INTERVAL:g}）："
            "对源站友好；给更小的值会被抬到下限"
        ),
    )
    parser.add_argument(
        "--raw-dir",
        default=str(DEFAULT_RAW_DIR),
        help="每个源单独写一份原始 JSON（复盘用：robots/HTTP/原始响应体）；off=关闭",
    )
    parser.add_argument(
        "--verify-log",
        default=str(DEFAULT_VERIFY_LOG),
        help="逐源验证日志（JSONL 追加：robots 判定/HTTP 状态/条目数）；off=关闭",
    )
    return parser.parse_args(argv)


class RateLimiter:
    """真实请求限速：保证**任意两次请求**之间的间隔 ≥ ``min_interval`` 秒。

    设计：
    - **硬下限** :data:`MIN_REQUEST_INTERVAL`（1 秒）——命令行给更小的值也会被抬到下限
      （团队硬性要求：不给对方服务器造成压力）；
    - ``sleep`` / ``clock`` 可注入 → 单元测试里"零等待"验证间隔计算，不起真实定时器。
    """

    def __init__(
        self,
        min_interval: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = max(MIN_REQUEST_INTERVAL, float(min_interval))
        self.waits: list[float] = []
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    async def wait(self) -> float:
        """必要时等待；返回本次实际等待秒数（0 = 无需等待）。"""
        now = self._clock()
        delay = 0.0 if self._last is None else max(0.0, self.min_interval - (now - self._last))
        if delay > 0:
            await self._sleep(delay)
        self.waits.append(round(delay, 3))
        self._last = self._clock()
        return delay


def _optional_path(value: str) -> Path | None:
    """把「关闭哨兵」视为不落盘：``""`` / ``off`` / ``none`` / ``-`` / ``null``。

    为什么要哨兵：**PowerShell 5.1 会在传参时吞掉空字符串**（`--raw-dir ""` 会变成缺参报错），
    因此除空串外还必须接受 `off` 之类的显式关闭值。
    """
    return None if value.strip().lower() in _DISABLED_VALUES else Path(value)


def _robots_label(result: FeedFetchResult) -> str:
    """robots 判定的人类可读标签（用于终端与报告）。"""
    if result.robots is None:
        return "-"
    if result.robots.allowed:
        return "允许"
    return "规则禁止" if result.robots.by_rule else "不可验证"


def _write_raw_json(
    spec: RssSourceSpec,
    result: FeedFetchResult,
    *,
    raw_dir: Path,
    collected_at: str,
    cache: RssCache,
) -> Path:
    """单源抓取结果 → 一份**原始 JSON**（复盘用）。

    含：源配置、robots 判定、HTTP 状态、缓存元信息（ETag/Last-Modified）、
    解析后的条目（含 `published_raw` 原始时间串）与**原始响应体**（`raw_body`）。
    """
    cache_key = RssCache.key_for(spec.url)
    cached = cache.load(cache_key)
    body = cached.body if cached else ""
    payload: dict[str, Any] = {
        "collected_at": collected_at,
        "parser_version": RSS_PARSER_VERSION,
        "spec": {
            "id": spec.id,
            "name": spec.name,
            "url": spec.url,
            "source_name": spec.source_name,
            "source_type": spec.source_type,
            "author_name": spec.author_name,
            "language": spec.language,
            "category": spec.category,
            "robots_check": spec.robots_check,
            "max_items": spec.max_items,
            "notes": spec.notes,
        },
        "robots": None
        if result.robots is None
        else {
            "allowed": result.robots.allowed,
            "by_rule": result.robots.by_rule,
            "reason": result.robots.reason,
        },
        "fetch": {
            "status": result.status,
            "http_status": result.http_status,
            "entries": len(result.entries),
            "skipped_by_window": result.skipped_by_window,
            "warnings": list(result.warnings),
        },
        "cache": None
        if cached is None
        else {
            "path": str(cache.path_for(cache_key)),
            "status": cached.status,
            "fetched_at": cached.fetched_at,
            "etag": cached.etag,
            "last_modified": cached.last_modified,
            "error": cached.error,
        },
        "raw_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
        "raw_body_chars": len(body),
        "entries": [
            {
                "id": entry.record_id,
                "title": entry.title,
                "content_text": entry.content_text,
                "link": entry.link,
                "author_name": entry.author_name,
                "language": entry.language,
                "categories": list(entry.categories),
                "has_media": entry.has_media,
                "published_raw": entry.published.raw,
                "published_at": None
                if entry.published.value is None
                else entry.published.value.isoformat(timespec="seconds"),
                "published_tz_ambiguous": entry.published.tz_ambiguous,
                "published_reason": entry.published.reason,
            }
            for entry in result.entries
        ],
        "raw_body": body,
    }
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = collected_at.replace(":", "").replace("-", "")
    path = raw_dir / f"{spec.id}_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


async def _close_transport(transport: Transport | None) -> None:
    """关闭真实 HTTP 会话（测试注入的 MockTransport 没有 ``close``，直接跳过）。"""
    close = getattr(transport, "close", None)
    if close is not None:
        await close()


def _append_verify_log(
    path: Path,
    *,
    spec: RssSourceSpec,
    result: FeedFetchResult,
    checked_at: str,
    run_mode: str,
) -> None:
    """把**单源验证结论**追加到 JSONL 日志（团队要求：robots 检查结果必须留痕）。

    每行一条 JSON：源 id / feed URL / robots 判定与原因 / HTTP 状态 / 结论状态 / 条目数 / 告警。
    """
    robots = result.robots
    record: dict[str, Any] = {
        "checked_at": checked_at,
        "run_mode": run_mode,
        "source_id": spec.id,
        "source_name": spec.source_name,
        "feed_url": spec.url,
        "robots_checked": spec.robots_check,
        "robots_allowed": None if robots is None else robots.allowed,
        "robots_by_rule": None if robots is None else robots.by_rule,
        "robots_reason": None if robots is None else robots.reason,
        "http_status": result.http_status,
        "status": result.status,
        "entries": len(result.entries),
        "warnings": list(result.warnings),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


async def _drive(args: argparse.Namespace, specs: Sequence[RssSourceSpec]) -> int:
    """按配置驱动抓取/预览；返回退出码（0=成功 / 3=没有可用条目）。"""
    fixture_dir = Path(args.fixture) if args.fixture else None
    cache_mode = "readonly" if args.dry_run and fixture_dir is None else args.cache_mode
    cache = RssCache(Path(args.cache_dir), mode=cache_mode)
    offline = args.dry_run and fixture_dir is not None
    transport: Transport | None = None if offline else AiohttpTransport()
    #: 真实抓取限速（≥1 秒/请求，硬下限；`--fixture` 零网络时不需要）
    limiter = None if offline else RateLimiter(args.min_interval)
    raw_dir = _optional_path(args.raw_dir)
    verify_log = _optional_path(args.verify_log)
    collected_at = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    run_stats: dict[str, Any] = {
        "parser_version": RSS_PARSER_VERSION,
        "mode": "fixture" if fixture_dir else ("dry-run" if args.dry_run else "live"),
        "cache_mode": cache.mode,
        "min_interval": None if limiter is None else limiter.min_interval,
        "raw_dir": None if raw_dir is None else str(raw_dir),
        "verify_log": None if verify_log is None else str(verify_log),
        "per_source": {},
    }

    try:
        for spec in specs:
            result = await _fetch_one(
                spec,
                args=args,
                cache=cache,
                transport=transport,
                fixture_dir=fixture_dir,
                limiter=limiter,
            )
            per_source: dict[str, Any] = {
                "status": result.status,
                "entries": len(result.entries),
                "skipped_by_window": result.skipped_by_window,
                "http_status": result.http_status,
                "robots": None
                if result.robots is None
                else {
                    "allowed": result.robots.allowed,
                    "by_rule": result.robots.by_rule,
                    "reason": result.robots.reason,
                },
                "warnings": list(result.warnings),
            }
            if raw_dir is not None:
                raw_path = _write_raw_json(
                    spec, result, raw_dir=raw_dir, collected_at=collected_at, cache=cache
                )
                per_source["raw_json"] = str(raw_path)
            run_stats["per_source"][spec.id] = per_source
            if verify_log is not None:  # 团队要求：robots 检查结果必须留痕（JSONL 日志）
                _append_verify_log(
                    verify_log,
                    spec=spec,
                    result=result,
                    checked_at=collected_at,
                    run_mode=str(run_stats["mode"]),
                )
            warnings.extend(result.warnings)
            for entry in result.entries:
                if args.limit and len(rows) >= args.limit:
                    break
                rows.append(_to_row(spec, entry, collected_at=collected_at))
            http_label = "-" if result.http_status is None else str(result.http_status)
            print(
                f"[rss] {spec.id:<18} {result.status:<13} 条目 {len(result.entries):>3}"
                f" http={http_label:<4} robots={_robots_label(result)} type={spec.source_type}"
            )
    finally:
        # 真实抓取后必须显式关闭 HTTP 会话（否则 aiohttp 打印 Unclosed client session）
        await _close_transport(transport)
    if limiter is not None:
        run_stats["rate_limit_waits"] = limiter.waits

    run_stats["entries"] = len(rows)
    run_stats["warnings"] = warnings
    print(f"[rss] 模式={run_stats['mode']} 缓存={cache_mode} 源={len(specs)} 可用条目={len(rows)}")
    for problem in warnings:
        print(f"[rss][告警] {problem}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(run_stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[rss] 统计：{args.json_out}")
    if args.dry_run:
        print(f"[rss] --dry-run：未写 CSV、未落库（可导出 {len(rows)} 条）")
        return 0 if rows else 3

    out_path = (
        Path(args.out) if args.out else DEFAULT_OUT_DIR / f"real_posts_{collected_at[:10]}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[rss] 导出：{out_path}（{len(rows)} 行；列契约见 docs/11 §1.1）")
    return 0 if rows else 3


async def _fetch_one(
    spec: RssSourceSpec,
    *,
    args: argparse.Namespace,
    cache: RssCache,
    transport: Transport | None,
    fixture_dir: Path | None,
    limiter: RateLimiter | None = None,
) -> FeedFetchResult:
    """取单个源：`--fixture` → 本地 XML（零网络）；否则走 `fetch_spec_entries`。"""
    if fixture_dir is not None:
        fixture = fixture_dir / f"{spec.id}.xml"
        if not fixture.is_file():
            return FeedFetchResult(
                spec.id, "failed", warnings=(f"{spec.id}：缺少 fixture 文件 {fixture}",)
            )
        entries, problems = parse_feed_entries(
            fixture.read_bytes(), spec=spec, max_items=args.per_feed_limit
        )
        return FeedFetchResult(spec.id, "fetched", entries=entries, warnings=problems)
    if transport is None:  # pragma: no cover - 仅 dry-run 且无 fixture 时
        return FeedFetchResult(
            spec.id,
            "skipped",
            warnings=(f"{spec.id}：--dry-run 未提供 --fixture，且不接受联网（零请求保证）",),
        )

    async def _request(request: HttpRequest) -> HttpResponse:
        if limiter is not None:
            await limiter.wait()  # 任意两次请求间隔 ≥ min_interval（硬下限 1s）
        return await send_with_retry(transport, request)

    return await fetch_spec_entries(
        spec,
        request=_request,
        cache=cache,
        window=None,
        max_items=args.per_feed_limit,
    )


def _to_row(spec: RssSourceSpec, entry: RssEntry, *, collected_at: str) -> dict[str, str]:
    """`RssEntry` → `docs/11 §1.1` 列契约的 CSV 行（时间不猜：没有就置空）。"""
    published = entry.published.value
    published_at = published.isoformat(timespec="seconds") if published is not None else ""
    notes: list[str] = []
    if published is None:
        notes.append(
            f"无可用发布时间（{entry.published.reason or 'unknown'}）→ effective_at=collected_at"
        )
    if entry.published.tz_ambiguous:
        notes.append("源时间缺时区，按要求未猜测")
    if entry.has_media:
        notes.append("含图片（图内观点无法由文本抽取）")
    if spec.source_type != DEFAULT_SOURCE_TYPE:
        notes.append(f"采集用途={spec.source_type}（仅事件源，不进入观点语料）")
    # 注意：**不拼接 `spec.notes`**（TD-34）——那是源配置说明（"冒烟通过"等），
    # 对人工标注是噪音；本列只保留"本次采集产生的质量标记"。
    return {
        "id": entry.record_id,
        "content": entry.content_text or entry.title or "",
        "title": entry.title or "",
        "published_at": published_at,
        "effective_at": published_at or collected_at,
        "source": entry.source_name,
        "author_name": entry.author_name or "",
        "url": entry.link or "",
        "has_media": "true" if entry.has_media else "false",
        "language": entry.language,
        "category": spec.category,
        "collected_at": collected_at,
        "notes_collect": "；".join(notes),
    }


class _RateLimitedTransport:
    """为落库链路复用同一限速器；每次真实 HTTP 尝试都遵守硬下限。"""

    def __init__(self, transport: Transport, limiter: RateLimiter) -> None:
        self.transport = transport
        self.limiter = limiter

    async def send(self, request: HttpRequest) -> HttpResponse:
        await self.limiter.wait()
        return await self.transport.send(request)


async def _drive_to_db(args: argparse.Namespace, specs: Sequence[RssSourceSpec]) -> int:
    """逐源运行正式采集框架，写入 raw_items/news_events/collector_runs（+ processed_items）。"""
    import sqlalchemy as sa

    from database.models import Source
    from database.session import build_engine, session_scope
    from src.collectors.rss_collector import RssCollector
    from src.collectors.runner import run_collector
    from src.collectors.types import CollectWindow
    from src.processors.collection.pipeline import CollectionProcessor

    now = datetime.now(UTC)
    window = CollectWindow(start_at=now - timedelta(days=args.lookback_days), end_at=now)
    raw_transport = AiohttpTransport()
    limiter = RateLimiter(args.min_interval)
    transport = _RateLimitedTransport(raw_transport, limiter)
    results: list[dict[str, Any]] = []
    aliases = {"fed_press": "fed_press_releases"}

    try:
        engine = build_engine()
        with session_scope(engine=engine) as session:
            for spec in specs:
                db_name = aliases.get(spec.source_name, spec.source_name)
                source = session.scalar(sa.select(Source).where(Source.name == db_name))
                if source is None:
                    print(
                        f"[rss][错误] 数据库缺少来源 {db_name!r}（RSS 源 {spec.id}）；"
                        "请先执行 python -m database.seeds --scope sources",
                        file=sys.stderr,
                    )
                    return 2
                collector = RssCollector(
                    source,
                    specs=[spec],
                    transport=transport,
                    cache_dir=Path(args.cache_dir),
                    cache_mode=args.cache_mode,
                    max_items_per_feed=args.per_feed_limit,
                    post_processor=CollectionProcessor(),
                )
                result = await run_collector(session, collector, window=window, resume=False)
                outcome = result.outcome
                row = {
                    "source_id": spec.id,
                    "database_source": db_name,
                    "status": result.status.value,
                    "fetched": 0 if outcome is None else outcome.fetched_count,
                    "inserted": 0 if outcome is None else outcome.inserted_count,
                    "duplicate": 0 if outcome is None else outcome.duplicate_count,
                    "failed": 0 if outcome is None else outcome.failed_count,
                    "warnings": list(result.warnings),
                    "error": result.error_message,
                    # 采集后处理（TD-11）：白名单摘要（processor/version/计数/告警），不含凭据
                    "processing": collector.processing_summary(),
                }
                results.append(row)
                print(
                    f"[rss][db] {spec.id:<18} {row['status']:<14} "
                    f"fetched={row['fetched']} inserted={row['inserted']} "
                    f"duplicate={row['duplicate']} failed={row['failed']}"
                )
    finally:
        await _close_transport(raw_transport)

    total_fetched = sum(int(row["fetched"]) for row in results)
    distribution = {
        str(row["source_id"]): {
            "count": int(row["fetched"]),
            "share": round(int(row["fetched"]) / total_fetched, 4) if total_fetched else 0.0,
        }
        for row in results
    }
    concentration_warnings = [
        f"{source_id} 单源占比 {detail['share']:.1%} > 40%"
        for source_id, detail in distribution.items()
        if detail["share"] > 0.4
    ]
    for warning in concentration_warnings:
        print(f"[rss][告警] {warning}")

    stats = {
        "mode": "database",
        "window_start": window.start_utc.isoformat(),
        "window_end": window.end_utc.isoformat(),
        "lookback_days": args.lookback_days,
        "sources": results,
        "source_distribution": distribution,
        "concentration_warnings": concentration_warnings,
        "rate_limit_waits": limiter.waits,
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[rss] 统计：{args.json_out}")
    return 0 if results and any(row["status"] == "SUCCESS" for row in results) else 3


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功 / 2=参数或配置问题 / 3=没有可用条目。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[rss] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    # 缺省即 dry-run：避免误触发真实抓取（真实抓取必须显式 --no-dry-run）
    args.dry_run = not args.no_dry_run
    if not args.dry_run and args.min_interval < MIN_REQUEST_INTERVAL:
        print(
            f"[rss][告警] --min-interval={args.min_interval:g}s 低于硬下限 "
            f"{MIN_REQUEST_INTERVAL:g}s，已抬到下限（团队硬性要求：不给源站压力）"
        )
    if args.lookback_days <= 0:
        print("[rss] --lookback-days 必须大于 0", file=sys.stderr)
        return 2

    try:
        specs, problems = load_rss_sources(Path(args.sources))
    except Exception as exc:  # noqa: BLE001 - CollectorError 等统一转成退出码 2
        print(f"[rss] 源清单错误：{exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"[rss][告警] {problem}")
    enabled = [spec for spec in specs if spec.enabled]
    if args.source:
        wanted = set(args.source)
        enabled = [spec for spec in enabled if spec.id in wanted]
    if not enabled:
        print(
            "[rss] 没有启用的源（enabled 均为 false）：请先完成冒烟验证，"
            "再把已验证源置为 enabled=true / verified=true",
            file=sys.stderr,
        )
        return 2
    if args.to_db and not args.dry_run:
        try:
            return asyncio.run(_drive_to_db(args, enabled))
        except Exception as exc:  # noqa: BLE001 - CLI 必须给出清晰失败并返回非零码
            print(f"[rss] 落库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_drive(args, enabled))


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
