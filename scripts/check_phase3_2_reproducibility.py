"""在真实研究库上重复运行 Phase 3.2 门禁，验证逐字段确定性与零写库。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.session import build_engine  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.run_macro_alpha_gate import load_inputs  # noqa: E402
from scripts.run_technical_alpha_gate import load_bars  # noqa: E402
from src.alpha.macro import MacroGateResult, evaluate_macro_gate  # noqa: E402
from src.alpha.reproducibility import require_identical  # noqa: E402
from src.alpha.technical import TechnicalGateResult, evaluate_technical_gate  # noqa: E402

REPORT = ROOT / "docs" / "experiments" / "Phase3_2_复现审计报告.md"
FACT_TABLES = (
    "feature_snapshots",
    "market_regimes",
    "author_skill_snapshots",
    "author_weight_snapshots",
)


def _counts() -> dict[str, int]:
    with build_engine().connect() as connection:
        return {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
            for table in FACT_TABLES
        }


def run() -> tuple[TechnicalGateResult, MacroGateResult, dict[str, int]]:
    before = _counts()
    technical_bars = load_bars("XAUUSD_DUKASCOPY")
    technical_first = evaluate_technical_gate(technical_bars)
    technical_second = evaluate_technical_gate(technical_bars)
    require_identical(technical_first, technical_second, label="Technical Alpha")

    macro_inputs = load_inputs()
    macro_first = evaluate_macro_gate(*macro_inputs)
    macro_second = evaluate_macro_gate(*macro_inputs)
    require_identical(macro_first, macro_second, label="Macro Alpha")
    after = _counts()
    require_identical(before, after, label="数据库事实表行数")
    return technical_first, macro_first, after


def render(technical: TechnicalGateResult, macro: MacroGateResult, counts: dict[str, int]) -> str:
    count_lines = [f"| `{name}` | {value} |" for name, value in counts.items()]
    return "\n".join(
        [
            "# Phase 3.2 复现审计报告",
            "",
            "> **结论：PASS**。同一真实输入连续运行两次，结果逐字段一致，数据库事实表行数不变。",
            "",
            f"- Technical 数据 SHA-256：`{technical.data_hash}`",
            f"- Macro 组合 SHA-256：`{macro.data_hash}`",
            "- 比较纪律：逐字段精确相等，不使用数值容差。",
            "",
            "| 事实表 | 审计后行数 |",
            "|---|---:|",
            *count_lines,
            "",
            "该审计只证明确定性与零写库，不改变两个 Alpha 的 FAIL 结论。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    configure_stdout()
    technical, macro, counts = run()
    report = render(technical, macro, counts)
    safe_print(report)
    if args.no_dry_run:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
