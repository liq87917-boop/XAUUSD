"""W0-5 特征快照生成与回放命令（默认 dry-run）。"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.models import Timeframe  # noqa: E402
from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.features.market import (  # noqa: E402
    compute_market_feature_candidate,
    persist_market_feature_candidate,
    replay_market_feature_snapshot,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of 必须包含时区，例如 2026-09-18T08:00:00Z")
    return parsed.astimezone(UTC)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="W0-5：生成或回放严格 as-of 的特征快照")
    parser.add_argument("--instrument", default="XAUUSD")
    parser.add_argument("--timeframe", choices=[item.value for item in Timeframe], default="4h")
    parser.add_argument("--as-of", type=_aware_datetime)
    parser.add_argument("--replay", type=uuid.UUID, help="按快照 ID 回放，不生成新快照")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[features] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    if args.replay is not None:
        with session_scope() as session:
            candidate = replay_market_feature_snapshot(session, args.replay)
        print(
            "[features] REPLAY PASS "
            + json.dumps(
                {
                    "snapshot_id": str(args.replay),
                    "as_of": candidate.as_of.isoformat(),
                    "data_hash": candidate.data_hash,
                    "values": candidate.values,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    as_of = args.as_of or datetime.now(UTC)
    dry_run = not args.no_dry_run
    with session_scope() as session:
        candidate = compute_market_feature_candidate(
            session,
            instrument_symbol=args.instrument,
            timeframe=Timeframe(args.timeframe),
            as_of=as_of,
        )
        result = None if dry_run else persist_market_feature_candidate(session, candidate)
    payload = {
        "mode": "DRY-RUN" if dry_run else "WRITE",
        "instrument": candidate.instrument_symbol,
        "timeframe": candidate.timeframe.value,
        "as_of": candidate.as_of.isoformat(),
        "max_effective_at": candidate.max_effective_at.isoformat(),
        "data_hash": candidate.data_hash,
        "input_bar_ids": [str(item) for item in candidate.input_bar_ids],
        "values": candidate.values,
        "snapshot_id": None if result is None else str(result.snapshot_id),
        "inserted": None if result is None else result.inserted,
    }
    print("[features] " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
