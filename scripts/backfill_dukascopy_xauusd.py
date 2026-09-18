"""回填独立 Dukascopy XAUUSD 现货 1h 序列（默认 dry-run）。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.models import (  # noqa: E402
    Instrument,
    MarketBar,
    RawItem,
    RawItemType,
    Source,
    Timeframe,
)
from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.collectors.dukascopy import (  # noqa: E402
    DUKASCOPY_INSTRUMENT,
    HourCandle,
    day_url,
    parse_and_aggregate,
)
from src.common.uuid7 import uuid7  # noqa: E402
from src.processors.timeline import ensure_utc_from_database  # noqa: E402

DEFAULT_START = date(2024, 9, 15)
DEFAULT_RAW_DIR = REPO_ROOT / "logs" / "archive" / "phase3_1_dukascopy_raw"
SOURCE_NAME = "market_dukascopy_xauusd"
USER_AGENT = "gold-ai-collector/0.2"


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 Dukascopy XAUUSD 1h（默认 dry-run）")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument(
        "--end", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=1)
    )
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8)
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def _days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("--end 不得早于 --start")
    return [start + timedelta(days=index) for index in range((end - start).days + 1)]


def _raw_path(root: Path, day: date) -> Path:
    return root / f"{day.year}" / f"{day.month:02d}" / f"{day.day:02d}.bi5"


def _fetch_or_cache(client: httpx.Client, root: Path, day: date) -> tuple[bytes | None, str]:
    path = _raw_path(root, day)
    url = day_url(day)
    if path.exists():
        return path.read_bytes(), "cache"
    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            response = client.get(url)
            if response.status_code < 500:
                break
        except httpx.HTTPError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    if response is None:  # pragma: no cover - defensive; loop either assigns or raises
        raise RuntimeError(f"Dukascopy 请求未产生响应：{url}")
    if response.status_code == 404:
        return None, "closed_or_missing"
    response.raise_for_status()
    payload = response.content
    # 原始文件只追加：同一路径已经存在时绝不覆盖；当前进程内采用独占创建。
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        cached = path.read_bytes()
        if hashlib.sha256(cached).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError(f"原始文件内容冲突：{path}") from exc
        payload = cached
    return payload, "network"


def persist_hours(
    session: Session, hours: list[HourCandle], *, collected_at: datetime
) -> tuple[int, int]:
    inserted = duplicate = 0
    instrument = session.scalar(
        sa.select(Instrument).where(Instrument.symbol == DUKASCOPY_INSTRUMENT)
    )
    source = session.scalar(sa.select(Source).where(Source.name == SOURCE_NAME))
    if instrument is None or source is None:
        raise RuntimeError(
            "请先执行 python -m database.seeds --scope instruments 和 --scope sources"
        )
    existing = {
        ensure_utc_from_database(moment, field_name="market_bars.open_time")
        for moment in session.scalars(
            sa.select(MarketBar.open_time).where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.timeframe == Timeframe.H1,
            )
        ).all()
    }
    for bar in hours:
        if bar.open_time in existing:
            duplicate += 1
            continue
        url = day_url(bar.open_time.date())
        session.add(
            MarketBar(
                id=uuid7(),
                instrument_id=instrument.id,
                timeframe=Timeframe.H1,
                open_time=bar.open_time,
                close_time=bar.close_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source_id=source.id,
                collected_at=collected_at,
                effective_at=max(collected_at, bar.close_time),
            )
        )
        raw_json = {
            "provider": "dukascopy_bi5",
            "provider_symbol": "XAUUSD",
            "instrument_symbol": DUKASCOPY_INSTRUMENT,
            "timeframe": "1h",
            "open_time": bar.open_time.isoformat(),
            "close_time": bar.close_time.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "source_sha256": bar.source_sha256,
            "aggregation": "complete-60x1m-bid-v1",
        }
        session.add(
            RawItem(
                id=uuid7(),
                source_id=source.id,
                source_record_id=f"XAUUSD:1h:{bar.open_time.isoformat()}",
                item_type=RawItemType.QUOTE,
                title=f"XAUUSD Dukascopy 1h {bar.open_time.isoformat()}",
                content_text=None,
                raw_json=raw_json,
                source_url=url,
                content_hash=hashlib.sha256(
                    json.dumps(raw_json, sort_keys=True).encode()
                ).hexdigest(),
                published_at=bar.close_time,
                collected_at=collected_at,
                effective_at=max(collected_at, bar.close_time),
            )
        )
        existing.add(bar.open_time)
        inserted += 1
    session.flush()
    return inserted, duplicate


def _persist(hours: list[HourCandle], *, collected_at: datetime) -> tuple[int, int]:
    with session_scope() as session:
        return persist_hours(session, hours, collected_at=collected_at)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _args(argv)
    hours: list[HourCandle] = []
    stats = {"network": 0, "cache": 0, "closed_or_missing": 0, "invalid": 0}
    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        days = _days(args.start, args.end)
        fetched: dict[date, tuple[bytes | None, str]] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_fetch_or_cache, client, args.raw_dir, day): day for day in days}
            for completed, future in enumerate(as_completed(futures), 1):
                day = futures[future]
                fetched[day] = future.result()
                if completed % 50 == 0 or completed == len(days):
                    print(f"[dukascopy] fetched {completed}/{len(days)}", flush=True)
        for day in days:
            payload, status = fetched[day]
            stats[status] += 1
            if payload is None:
                continue
            try:
                hours.extend(parse_and_aggregate(payload, day=day))
            except ValueError as exc:
                stats["invalid"] += 1
                print(f"[dukascopy] INVALID {day}: {exc}", file=sys.stderr)

    inserted = duplicate = 0
    if args.no_dry_run:
        inserted, duplicate = _persist(hours, collected_at=datetime.now(UTC))
    print(
        "[dukascopy] "
        + json.dumps(
            {
                "mode": "WRITE" if args.no_dry_run else "DRY-RUN",
                "instrument": DUKASCOPY_INSTRUMENT,
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "hour_bars": len(hours),
                "files": stats,
                "inserted": inserted,
                "duplicate": duplicate,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if stats["invalid"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
