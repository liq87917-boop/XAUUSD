"""从 author_opinions + market_bars 生成可复算的 W0-4 前瞻收益标签 CSV。"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.processors.opinion_labels import OpinionLabel, build_opinion_labels  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "logs" / "phase3_w0_4_opinion_labels.csv"
DEFAULT_META = REPO_ROOT / "logs" / "phase3_w0_4_opinion_labels.meta.json"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="W0-4：构建作者观点前瞻收益标签")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--meta-out", default=str(DEFAULT_META))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[opinion-labels] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    with session_scope() as session:
        labels = build_opinion_labels(session)
    rows = [label.to_dict() for label in labels]
    statuses = Counter(row["status"] for row in rows)
    evaluation_grid_rows = sum(row["horizon_source"] == "evaluation_grid" for row in rows)
    missing_horizon_opinions = len(
        {row["opinion_id"] for row in rows if row["horizon_source"] == "evaluation_grid"}
    )
    print(f"[opinion-labels] opinions={len(rows)} statuses={dict(statuses)}")
    if dry_run:
        print("[opinion-labels] DRY-RUN：未写 CSV/meta")
        return 0
    out = Path(args.out)
    meta_out = Path(args.meta_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else list(OpinionLabel.__dataclass_fields__)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    meta_out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": len(rows),
                "statuses": dict(statuses),
                "labeled": statuses.get("LABELED", 0),
                "evaluation_grid_rows": evaluation_grid_rows,
                "missing_horizon_opinions": missing_horizon_opinions,
                "rules": {
                    "entry": "first bar with open_time > opinion.effective_at",
                    "max_entry_lag": (
                        "entry_at - effective_at must be <= evaluated horizon; "
                        "otherwise ENTRY_LAG_EXCEEDED"
                    ),
                    "exit": "entry_at + opinion.horizon",
                    "collection_time": (
                        "input_effective_at_fallback is pipeline-only and becomes "
                        "UNTRUSTED_COLLECTION_TIME"
                    ),
                    "missing": "drop and count; never interpolate or shorten",
                    "missing_horizon": (
                        "evaluate separately on 1h/4h/1d grid; "
                        "never infer a declared horizon"
                    ),
                    "provider_symbol": "GC=F",
                    "instrument_proxy": "COMEX_CONTINUOUS_FUTURES",
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[opinion-labels] wrote {out} and {meta_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
