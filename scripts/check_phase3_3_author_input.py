"""只读体检用户提供的 Phase 3.3 作者帖子 CSV/XLSX。"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.import_manual_posts import normalize_rows, read_table  # noqa: E402
from src.alpha.author_input import AuthorInputAudit, validate_author_input  # noqa: E402


def render(audit: AuthorInputAudit, input_path: Path, sha256: str) -> str:
    author_lines = [f"| {name} | {count} |" for name, count in audit.author_counts]
    if not author_lines:
        author_lines.append("| — | 0 |")
    error_lines = [f"- {item}" for item in audit.errors] or ["- 无"]
    warning_lines = [f"- {item}" for item in audit.warnings] or ["- 无"]
    return "\n".join(
        [
            "# Phase 3.3 作者输入体检报告",
            "",
            f"> **结论：{'PASS' if audit.ready else 'BLOCKED'}**。体检不会修改输入或数据库。",
            "",
            f"- 输入：`{input_path}`",
            f"- SHA-256：`{sha256}`",
            f"- 数据行：{audit.rows}",
            "",
            "| 作者 | 行数 |",
            "|---|---:|",
            *author_lines,
            "",
            "## 硬错误",
            "",
            *error_lines,
            "",
            "## 样本量提示",
            "",
            *warning_lines,
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    configure_stdout()
    raw_rows, info = read_table(args.input)
    audit = validate_author_input(normalize_rows(raw_rows), now=datetime.now(UTC))
    report = render(audit, args.input, str(info["sha256"]))
    safe_print(report)
    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0 if audit.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
