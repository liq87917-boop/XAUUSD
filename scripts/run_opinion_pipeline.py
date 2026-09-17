"""运行 author_posts → processed_items → author_opinions；默认 dry-run。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.processors.opinion_pipeline import OpinionPipeline  # noqa: E402


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Post → Opinion 管道")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[opinion-pipeline] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("[opinion-pipeline] --limit 必须大于 0", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    try:
        with session_scope() as session:
            nested = session.begin_nested() if dry_run else None
            report = OpinionPipeline(session).run(limit=args.limit)
            if nested is not None:
                nested.rollback()
        print(f"[opinion-pipeline] {'DRY-RUN ' if dry_run else ''}{asdict(report)}")
        return 0 if report.posts_failed == 0 else 3
    except Exception as exc:  # noqa: BLE001
        print(f"[opinion-pipeline] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
