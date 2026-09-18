"""Phase 3.1 Regime 计算、机器门禁、回填与人工盲评清单。"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

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

DEFAULT_REVIEW = REPO_ROOT / "logs" / "phase3_1_regime_blind_review_v2.csv"
DEFAULT_KEY = REPO_ROOT / "logs" / "phase3_1_regime_blind_review_key_v2.csv"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
REVIEW_LABEL_ORDER = (
    Regime.NEWS_DRIVEN,
    Regime.HIGH_VOLATILITY,
    Regime.TREND_UP,
    Regime.TREND_DOWN,
    Regime.RANGE,
    Regime.LOW_VOLATILITY,
)


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
    """按即时状态分层抽样；人工语义验收不再与时序平滑结果混为一谈。"""
    groups = {
        label: [point for point in points if point.raw_regime is label]
        for label in REVIEW_LABEL_ORDER
    }
    available = [label for label in REVIEW_LABEL_ORDER if groups[label]]
    if not available:
        return []
    quotas = {label: count // len(available) for label in available}
    for label in available[: count % len(available)]:
        quotas[label] += 1
    selected: list[RegimePoint] = []
    for label in available:
        pool = groups[label]
        quota = min(quotas[label], len(pool))
        if quota == 1:
            selected.append(pool[len(pool) // 2])
        elif quota > 1:
            selected.extend(
                pool[round(index * (len(pool) - 1) / (quota - 1))] for index in range(quota)
            )
    return sorted(selected, key=lambda point: point.start_at)


def _write_review_files(
    points: tuple[RegimePoint, ...], *, review_path: Path, key_path: Path
) -> None:
    selected = _sample_points(points)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    review_columns = [
        "review_id",
        "start_at_utc",
        "start_at_beijing",
        "close",
        "close_vs_ema20_pct",
        "ema_20",
        "ema_60",
        "ema20_vs_ema60_pct",
        "ema_20_slope_4_pct",
        "ema_60_slope_4_pct",
        "adx_14",
        "atr_14_percent",
        "atr_rank_percentile",
        "news_count_4h",
        "macro_count_4h",
        "human_label",
        "human_note",
    ]
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review_columns)
        writer.writeheader()
        for index, point in enumerate(selected, 1):
            close = float(point.values["close"])
            ema20 = float(point.values["ema_20"])
            ema60 = float(point.values["ema_60"])
            writer.writerow(
                {
                    "review_id": f"R{index:03d}",
                    "start_at_utc": point.start_at.isoformat(),
                    "start_at_beijing": point.start_at.astimezone(BEIJING_TZ).isoformat(),
                    "close": point.values["close"],
                    "close_vs_ema20_pct": round((close / ema20 - 1) * 100, 6),
                    "ema_20": point.values["ema_20"],
                    "ema_60": point.values["ema_60"],
                    "ema20_vs_ema60_pct": round((ema20 / ema60 - 1) * 100, 6),
                    "ema_20_slope_4_pct": round(float(point.values["ema_20_slope_4"]) * 100, 6),
                    "ema_60_slope_4_pct": round(float(point.values["ema_60_slope_4"]) * 100, 6),
                    "adx_14": point.values["adx_14"],
                    "atr_14_percent": round(float(point.values["atr_14_pct"]) * 100, 6),
                    "atr_rank_percentile": round(float(point.values["atr_pct_rank_60"]) * 100, 4),
                    "news_count_4h": point.values["news_count_4h"],
                    "macro_count_4h": point.values["macro_count_4h"],
                    "human_label": "",
                    "human_note": "",
                }
            )
    with key_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "review_id",
                "start_at_utc",
                "review_target_label",
                "final_smoothed_label",
                "confidence",
            ],
        )
        writer.writeheader()
        for index, point in enumerate(selected, 1):
            writer.writerow(
                {
                    "review_id": f"R{index:03d}",
                    "start_at_utc": point.start_at.isoformat(),
                    "review_target_label": point.raw_regime.value,
                    "final_smoothed_label": point.regime.value,
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
