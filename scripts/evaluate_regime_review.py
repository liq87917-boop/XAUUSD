"""校验并评分 Phase 3.1 第二轮即时状态盲评。"""

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

from database.models import Regime  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402

VALID_LABELS = {label.value for label in Regime}


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评分 Phase 3.1 第二轮盲评")
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.8)
    return parser.parse_args(argv)


def evaluate(review_path: Path, key_path: Path, *, threshold: float = 0.8) -> dict[str, object]:
    with review_path.open(encoding="utf-8-sig", newline="") as handle:
        review = list(csv.DictReader(handle))
    with key_path.open(encoding="utf-8-sig", newline="") as handle:
        key = {row["review_id"]: row for row in csv.DictReader(handle)}
    errors: list[str] = []
    ids = [row.get("review_id", "") for row in review]
    if len(review) != 50:
        errors.append(f"必须恰好 50 行，当前 {len(review)} 行")
    if len(ids) != len(set(ids)):
        errors.append("review_id 存在重复")
    matched = 0
    for row in review:
        review_id = row.get("review_id", "")
        label = row.get("human_label", "").strip().upper()
        if label not in VALID_LABELS:
            errors.append(f"{review_id}: human_label 无效或为空")
            continue
        answer = key.get(review_id)
        if answer is None:
            errors.append(f"{review_id}: 答案键缺失")
            continue
        if label == answer["review_target_label"]:
            matched += 1
    agreement = matched / len(review) if review else 0.0
    return {
        "rows": len(review),
        "matched": matched,
        "agreement": agreement,
        "threshold": threshold,
        "passed": not errors and agreement >= threshold,
        "errors": errors,
    }


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _args(argv)
    result = evaluate(args.review, args.key, threshold=args.threshold)
    print("[regime-review] " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["errors"]:
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
