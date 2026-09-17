"""Phase 3.0 W0-2：FRED/ALFRED initial-release 宏观历史回填。

默认 dry-run 只输出分块计划，不联网、不写库；必须显式 ``--no-dry-run`` 才执行。
每个 realtime 分块最多 365 天，避免 FRED JSON 的 2000 vintage dates 上限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings  # noqa: E402
from database.models import MacroEvent, Source  # noqa: E402
from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.collectors.macro import (  # noqa: E402
    DEFAULT_W0_2_SERIES,
    MacroCollector,
    MacroSeriesSpec,
    _normalize_series,
)
from src.collectors.runner import run_collector  # noqa: E402
from src.collectors.transport import AiohttpTransport  # noqa: E402
from src.collectors.types import CollectWindow  # noqa: E402

DEFAULT_SOURCE: Final[str] = "fred_macro"
DEFAULT_START: Final[date] = date(2016, 1, 1)
DEFAULT_CHUNK_DAYS: Final[int] = 365
OBSERVATION_LAG_BUFFER_DAYS: Final[int] = 400
DEFAULT_OUT: Final[Path] = REPO_ROOT / "logs" / "macro_backfill" / "backfill_meta.json"


@dataclass(frozen=True, slots=True)
class BackfillResult:
    series_id: str
    start: str
    end: str
    status: str
    fetched: int
    inserted: int
    duplicate: int
    failed: int
    error: str | None


def parse_date(value: str, *, name: str) -> date:
    """解析 CLI 日期。"""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 YYYY-MM-DD，收到 {value!r}") from exc


def build_chunks(start: date, end: date, *, chunk_days: int) -> tuple[tuple[date, date], ...]:
    """建立左闭右闭的 realtime 日期分块。"""
    if end < start:
        raise ValueError("--end 不得早于 --start")
    if not 1 <= chunk_days <= 365:
        raise ValueError("--chunk-days 必须在 1..365")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def parse_series(value: str, configured: Sequence[MacroSeriesSpec]) -> tuple[MacroSeriesSpec, ...]:
    """按配置白名单选择序列；空字符串表示全部。"""
    by_id = {spec.series_id: spec for spec in configured}
    wanted = [item.strip() for item in value.split(",") if item.strip()]
    if not wanted:
        return tuple(configured)
    unknown = [item for item in wanted if item not in by_id]
    if unknown:
        raise ValueError(f"未知 series：{unknown}；可选：{sorted(by_id)}")
    return tuple(by_id[item] for item in wanted)


def _as_datetime(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def run_backfill(
    session: Session,
    *,
    source: Source,
    specs: Sequence[MacroSeriesSpec],
    chunks: Sequence[tuple[date, date]],
    timeout: float,
) -> list[BackfillResult]:
    """逐序列、逐 realtime 分块执行；单块结果立即提交并留 collector_run。"""
    results: list[BackfillResult] = []
    async with AiohttpTransport(default_timeout_seconds=timeout) as transport:
        for spec in specs:
            for chunk_start, chunk_end in chunks:
                window = CollectWindow(
                    start_at=_as_datetime(chunk_start),
                    end_at=_as_datetime(chunk_end + timedelta(days=1)),
                )
                collector = MacroCollector(
                    source,
                    series=[asdict(spec)],
                    lookback_days=(chunk_end - chunk_start).days
                    + 1
                    + OBSERVATION_LAG_BUFFER_DAYS,
                    transport=transport,
                )
                run = await run_collector(
                    session, collector, window=window, resume=False
                )
                outcome = run.outcome
                results.append(
                    BackfillResult(
                        series_id=spec.series_id,
                        start=chunk_start.isoformat(),
                        end=chunk_end.isoformat(),
                        status=run.status.value,
                        fetched=outcome.fetched_count if outcome else 0,
                        inserted=outcome.inserted_count if outcome else 0,
                        duplicate=outcome.duplicate_count if outcome else 0,
                        failed=outcome.failed_count if outcome else 0,
                        error=run.error_message,
                    )
                )
                session.commit()
    return results


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="W0-2 ALFRED initial-release 回填（默认 dry-run）"
    )
    parser.add_argument("--series", default="", help="逗号分隔；空=fred_macro 配置中的全部")
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=(datetime.now(UTC).date() - timedelta(days=1)).isoformat())
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--db-url", default="", help="覆盖 DATABASE_URL")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="no-dry-run 元数据输出")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划（默认）")
    parser.add_argument("--no-dry-run", action="store_true", help="显式联网并写库")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """退出码：0 成功；2 参数/预检失败；3 有回填分块失败。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[macro-backfill] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    try:
        start = parse_date(args.start, name="--start")
        end = parse_date(args.end, name="--end")
        chunks = build_chunks(start, end, chunk_days=args.chunk_days)
    except ValueError as exc:
        print(f"[macro-backfill] {exc}", file=sys.stderr)
        return 2

    engine = build_engine(args.db_url or None)
    session = build_session_factory(engine)()
    try:
        source = session.scalar(sa.select(Source).where(Source.name == DEFAULT_SOURCE))
        if source is None:
            print("[macro-backfill] 缺少 fred_macro source；请先执行 source seeds", file=sys.stderr)
            return 2
        try:
            specs = parse_series(args.series, _normalize_series(DEFAULT_W0_2_SERIES))
        except Exception as exc:
            print(f"[macro-backfill] 预检失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(
            f"[macro-backfill] series={len(specs)} chunks={len(chunks)} "
            f"requests={len(specs) * len(chunks)} range={start}..{end}"
        )
        if not args.no_dry_run:
            print("[macro-backfill] dry-run：未联网、未写库、未写文件")
            return 0
        if not get_settings().fred_configured:
            print("[macro-backfill] FRED_API_KEY 未配置", file=sys.stderr)
            return 2
        results = asyncio.run(
            run_backfill(
                session,
                source=source,
                specs=specs,
                chunks=chunks,
                timeout=args.timeout,
            )
        )
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "mode": "initial_release_only",
                    "series": [spec.series_id for spec in specs],
                    "chunks": [asdict(result) for result in results],
                    "macro_rows": int(
                        session.scalar(sa.select(sa.func.count()).select_from(MacroEvent)) or 0
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        failed = [result for result in results if result.status not in {"SUCCESS"}]
        print(
            f"[macro-backfill] 完成：inserted={sum(r.inserted for r in results)} "
            f"duplicate={sum(r.duplicate for r in results)} failed_chunks={len(failed)}"
        )
        return 3 if failed else 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())