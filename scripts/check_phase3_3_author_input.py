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
from src.alpha.source_authorization import (  # noqa: E402
    SourceAuthorizationAudit,
    validate_source_authorizations,
)


def render(
    audit: AuthorInputAudit,
    input_path: Path,
    sha256: str,
    authorization_audit: SourceAuthorizationAudit,
    authorization_path: Path,
    authorization_sha256: str,
) -> str:
    ready = audit.ready and authorization_audit.ready
    author_lines = [f"| {name} | {count} |" for name, count in audit.author_counts]
    if not author_lines:
        author_lines.append("| — | 0 |")
    error_lines = [f"- {item}" for item in audit.errors] or ["- 无"]
    warning_lines = [f"- {item}" for item in audit.warnings] or ["- 无"]
    authorization_errors = [f"- {item}" for item in authorization_audit.errors] or ["- 无"]
    authorization_warnings = [f"- {item}" for item in authorization_audit.warnings] or ["- 无"]
    return "\n".join(
        [
            "# Phase 3.3 作者输入体检报告",
            "",
            f"> **结论：{'PASS' if ready else 'BLOCKED'}**。体检不会修改输入或数据库。",
            "",
            f"- 输入：`{input_path}`",
            f"- SHA-256：`{sha256}`",
            f"- 数据行：{audit.rows}",
            f"- 授权表：`{authorization_path}`",
            f"- 授权表 SHA-256：`{authorization_sha256}`",
            f"- 逐行授权检查通过账号：{len(authorization_audit.approved_accounts)}",
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
            "## 来源授权硬错误",
            "",
            *authorization_errors,
            "",
            "## 来源授权提示",
            "",
            *authorization_warnings,
            "",
        ]
    )


def validate_report_target(report: Path, input_path: Path, authorization_path: Path) -> None:
    """报告不得覆盖两份不可改写的用户输入，包括硬链接别名。"""
    target = report.resolve()
    for protected in (input_path, authorization_path):
        if target == protected.resolve() or (
            target.exists() and protected.exists() and target.samefile(protected)
        ):
            raise ValueError("--report 不能与帖子输入或来源授权表指向同一文件")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--authorizations", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    configure_stdout()
    if args.report is not None:
        try:
            validate_report_target(args.report, args.input, args.authorizations)
        except ValueError as exc:
            parser.error(str(exc))
    raw_rows, info = read_table(args.input)
    authorization_rows, authorization_info = read_table(args.authorizations)
    now = datetime.now(UTC)
    authorization_audit = validate_source_authorizations(
        normalize_rows(authorization_rows), now=now, evidence_root=ROOT
    )
    audit = validate_author_input(
        normalize_rows(raw_rows),
        now=now,
        authorization_windows={
            account: (valid_from, expires_at)
            for account, valid_from, expires_at in authorization_audit.approved_windows
        },
    )
    report = render(
        audit,
        args.input,
        str(info["sha256"]),
        authorization_audit,
        args.authorizations,
        str(authorization_info["sha256"]),
    )
    safe_print(report)
    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0 if audit.ready and authorization_audit.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
