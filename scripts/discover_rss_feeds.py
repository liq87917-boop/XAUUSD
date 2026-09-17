"""RSS 入口探测 CLI：逐站发现"官方声明的 feed 地址"（Phase 2 真实语料）。

合规纪律（比采集更严格）：
- **默认 `--dry-run`**：不发任何请求；真实探测必须显式 `--no-dry-run`；
- robots.txt **前置检查**（fail-closed）：403/取不到 → 跳过该站，**连首页都不请求**；
- **每站只发 1 次首页请求，绝不重试**；
- 任意两次请求间隔 ≥ `--min-interval`（硬下限 1.0s，默认 1.5s）；
- 只解析 HTML 里声明的 feed 链接，**不抓取 feed 正文**（正文冒烟用 `scripts/collect_rss.py`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout  # noqa: E402
from scripts.collect_rss import (  # noqa: E402
    DEFAULT_OUT_DIR,
    MIN_REQUEST_INTERVAL,
    RateLimiter,
    _close_transport,
)
from src.collectors.rss_discovery import DiscoveryResult, discover_feed_links  # noqa: E402
from src.collectors.transport import AiohttpTransport, HttpRequest, HttpResponse  # noqa: E402


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/discover_rss_feeds.py",
        description="从站点首页探测官方 RSS/Atom feed 链接（robots 前置 / 每站 1 次请求 / 不重试）",
    )
    parser.add_argument(
        "--url", action="append", required=True, help="站点首页 URL（可多次，按顺序探测）"
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.5,
        help=f"任意两次请求的最小间隔（秒，硬下限 {MIN_REQUEST_INTERVAL:g}）",
    )
    parser.add_argument(
        "--json-out", default=None, help="探测结果 JSON（默认 logs/rss_discovery_<日期>.json）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印计划（不发任何请求，缺省即此模式）"
    )
    parser.add_argument(
        "--no-dry-run", action="store_true", help="显式允许真实请求（与 --dry-run 互斥）"
    )
    return parser.parse_args(argv)


def _robots_label(result: DiscoveryResult) -> str:
    """robots 判定的人类可读标签。"""
    if result.robots is None:
        return "-"
    if result.robots.allowed:
        return "允许"
    return "规则禁止" if result.robots.by_rule else "不可验证"


async def _drive(args: argparse.Namespace, *, min_interval: float) -> int:
    """按顺序探测每站；返回退出码（0=发现至少 1 个 feed / 3=一个都没发现）。"""
    limiter = RateLimiter(min_interval)
    live_transport: AiohttpTransport | None = None if args.dry_run else AiohttpTransport()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[DiscoveryResult] = []

    async def _request(request: HttpRequest) -> HttpResponse:
        await limiter.wait()  # 任意两次请求间隔 ≥ min_interval
        if live_transport is None:  # pragma: no cover - dry-run 分支不会发请求
            raise RuntimeError("dry-run 不应发起请求")
        return await live_transport.send(request)  # 不重试：单次请求

    try:
        for url in args.url:
            if live_transport is None:
                print(f"[discover] {url}  （dry-run：未发请求）")
                continue
            result = await discover_feed_links(url, request=_request)
            results.append(result)
            status = "-" if result.status is None else str(result.status)
            print(
                f"[discover] {url}  robots={_robots_label(result)}  http={status}"
                f"  发现 feed={len(result.links)}"
            )
            for link in result.links:
                print(f"    - {link.url}  type={link.type}  title={link.title}")
            for warning in result.warnings:
                print(f"[discover][告警] {warning}")
    finally:
        await _close_transport(live_transport)

    payload: dict[str, Any] = {
        "checked_at": checked_at,
        "min_interval": limiter.min_interval,
        "rate_limit_waits": limiter.waits,
        "sites": [result.to_payload() for result in results],
    }
    out_path = (
        Path(args.json_out)
        if args.json_out
        else DEFAULT_OUT_DIR / f"rss_discovery_{checked_at[:10]}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    found = sum(len(result.links) for result in results)
    mode = "dry-run" if args.dry_run else "live"
    print(f"[discover] 模式={mode} 站点={len(args.url)} feed={found}")
    print(f"[discover] 结果 JSON：{out_path}")
    return 0 if found else 3


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=发现 feed / 2=参数问题 / 3=未发现任何 feed。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[discover] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    args.dry_run = not args.no_dry_run
    if not args.dry_run and args.min_interval < MIN_REQUEST_INTERVAL:
        print(
            f"[discover][告警] --min-interval={args.min_interval:g}s 低于硬下限 "
            f"{MIN_REQUEST_INTERVAL:g}s，已抬到下限"
        )
    return asyncio.run(_drive(args, min_interval=args.min_interval))


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
