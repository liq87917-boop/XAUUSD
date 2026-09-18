"""审计 Phase 3 当前检查点是否越过 Alpha / Strategy / Live Trading 边界。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings  # noqa: E402
from database.session import build_engine  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.alpha.boundaries import Phase3BoundaryAudit, assess_phase3_boundaries  # noqa: E402

REPORT = ROOT / "docs" / "experiments" / "Phase3_范围边界审计报告.md"


def run_audit() -> Phase3BoundaryAudit:
    settings = get_settings()
    engine = build_engine(settings=settings)
    with engine.connect() as connection:
        revision = str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))
        tables = set(sa.inspect(connection).get_table_names())
        skill_rows = int(
            connection.scalar(sa.text("SELECT count(*) FROM author_skill_snapshots")) or 0
        )
        weight_rows = int(
            connection.scalar(sa.text("SELECT count(*) FROM author_weight_snapshots")) or 0
        )
    return assess_phase3_boundaries(
        revision=revision,
        table_names=tables,
        author_skill_rows=skill_rows,
        author_weight_rows=weight_rows,
        live_trading=settings.live_trading,
        external_order_submission=settings.allow_external_order_submission,
    )


def render(audit: Phase3BoundaryAudit) -> str:
    unexpected = ", ".join(audit.unexpected_fact_tables) or "无"
    verdict = "PASS" if audit.passed else "FAIL"
    return "\n".join(
        [
            "# Phase 3 范围边界审计报告",
            "",
            f"> **结论：{verdict}**。该报告验证负面/阻塞门禁没有被绕过。",
            "",
            f"- Alembic revision：`{audit.revision}`",
            f"- 不应存在的 Alpha/Prediction/Strategy/Trading 表：{unexpected}",
            f"- `author_skill_snapshots` 行数：{audit.author_skill_rows}",
            f"- `author_weight_snapshots` 行数：{audit.author_weight_rows}",
            f"- `LIVE_TRADING`：{str(audit.live_trading).lower()}",
            f"- `ALLOW_EXTERNAL_ORDER_SUBMISSION`：{str(audit.external_order_submission).lower()}",
            "",
            "## 解释",
            "",
            "Phase 3.2 Technical/Macro 均未过效果门槛，Phase 3.3 Author/News 均未过数据资格。"
            "因此当前正确状态是：保留特征与 Regime 事实，不创建 Alpha/Prediction/Strategy 表，"
            "不写作者技能或权重，不开启任何交易能力。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    configure_stdout()
    audit = run_audit()
    report = render(audit)
    safe_print(report)
    if args.no_dry_run:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0 if audit.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
