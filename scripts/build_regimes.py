"""Phase 3.1 Regime 计算、机器门禁、回填与人工盲评清单。"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.models import Regime, Timeframe  # noqa: E402
from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.alpha.regime import (  # noqa: E402
    REGIME_MODEL_VERSION,
    RegimePoint,
    build_regime_run,
    load_regime_inputs,
    persist_regime_run,
    regime_metrics,
)

DEFAULT_REVIEW = REPO_ROOT / "logs" / "phase3_1_regime_blind_review.csv"
DEFAULT_KEY = REPO_ROOT / "logs" / "phase3_1_regime_blind_review_key.csv"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 3.1：构建 Market Regime（默认 dry-run）")
    parser.add_argument("--instrument", default="XAUUSD")
    parser.add_argument("--timeframe", choices=["1h"], default="1h")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--write-review", action="store_true")
    parser.add_argument("--review-out", default=str(DEFAULT_REVIEW))
    parser.add_argument("--key-out", default=str(DEFAULT_KEY))
    return parser.parse_args(argv)


def _sample_points(points: tuple[RegimePoint, ...], count: int = 50) -> list[RegimePoint]:
    eligible = [point for point in points if point.regime is not Regime.UNKNOWN]
    if len(eligible) <= count:
        return eligible
    return [eligible[round(index * (len(eligible) - 1) / (count - 1))] for index in range(count)]


def _write_review_files(
    points: tuple[RegimePoint, ...], *, review_path: Path, key_path: Path
) -> None:
    selected = _sample_points(points)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    common = [
        "review_id",
        "start_at",
        "close",
        "ema_20",
        "ema_60",
        "adx_14",
        "atr_14_pct",
        "atr_pct_rank_60",
        "news_count_4h",
        "macro_count_4h",
    ]
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*common, "human_label", "human_note"])
        writer.writeheader()
        for index, point in enumerate(selected, 1):
            writer.writerow(
                {
                    "review_id": f"R{index:03d}",
                    "start_at": point.start_at.isoformat(),
                    **{name: point.values.get(name, "") for name in common[2:]},
                    "human_label": "",
                    "human_note": "",
                }
            )
    with key_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["review_id", "start_at", "engine_label", "raw_label", "confidence"],
        )
        writer.writeheader()
        for index, point in enumerate(selected, 1):
            writer.writerow(
                {
                    "review_id": f"R{index:03d}",
                    "start_at": point.start_at.isoformat(),
                    "engine_label": point.regime.value,
                    "raw_label": point.raw_regime.value,
                    "confidence": point.confidence,
                }
            )


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[regime] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    with session_scope() as session:
        instrument, bars, events = load_regime_inputs(
            session,
            instrument_symbol=args.instrument,
            timeframe=Timeframe(args.timeframe),
        )
        run = build_regime_run(
            instrument=instrument,
            timeframe=Timeframe(args.timeframe),
            bars=bars,
            events=events,
        )
        metrics = regime_metrics(run)
        result = None
        if not dry_run:
            if not metrics["machine_pass"]:
                print(
                    "[regime] REFUSED：机器门禁未通过，不写 market_regimes；"
                    + json.dumps(metrics["gates"], ensure_ascii=False, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            result = persist_regime_run(session, run)

    if args.write_review:
        _write_review_files(
            run.points,
            review_path=Path(args.review_out),
            key_path=Path(args.key_out),
        )
    payload = {
        "mode": "DRY-RUN" if dry_run else "WRITE",
        "model_version": REGIME_MODEL_VERSION,
        "instrument": run.instrument_symbol,
        "timeframe": run.timeframe.value,
        "metrics": metrics,
        "write_result": None
        if result is None
        else {
            "inserted_snapshots": result.inserted_snapshots,
            "inserted_regimes": result.inserted_regimes,
            "existing_regimes": result.existing_regimes,
        },
        "review_written": bool(args.write_review),
    }
    print("[regime] " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
