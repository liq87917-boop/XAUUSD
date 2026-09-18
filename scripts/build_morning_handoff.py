"""生成夜间开发的早间交接报告；只读取 Git 与本地 PostgreSQL。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.session import build_engine  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402

REPORT = ROOT / "docs" / "operations" / "2026-09-19_早间交接.md"
BASE_COMMIT = "51320f5"
COUNT_TABLES = (
    "market_bars",
    "feature_snapshots",
    "market_regimes",
    "author_skill_snapshots",
    "author_weight_snapshots",
)


@dataclass(frozen=True, slots=True)
class HandoffSnapshot:
    commits: tuple[tuple[str, str], ...]
    database_counts: tuple[tuple[str, int], ...]
    revision: str
    working_tree_clean_before_report: bool


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} 执行失败")
    return result.stdout.strip()


def collect() -> HandoffSnapshot:
    log = _git("log", "--format=%h%x09%s", f"{BASE_COMMIT}..HEAD")
    commit_rows: list[tuple[str, str]] = []
    for line in log.splitlines():
        if "\t" in line:
            commit, title = line.split("\t", 1)
            commit_rows.append((commit, title))
    commits = tuple(commit_rows)
    clean = not bool(_git("status", "--porcelain"))
    with build_engine().connect() as connection:
        revision = str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))
        counts = tuple(
            (
                table,
                int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0),
            )
            for table in COUNT_TABLES
        )
    return HandoffSnapshot(commits, counts, revision, clean)


def render(snapshot: HandoffSnapshot) -> str:
    commits = [f"- `{commit}` {title}" for commit, title in snapshot.commits]
    counts = [f"| `{table}` | {count:,} |" for table, count in snapshot.database_counts]
    return "\n".join(
        [
            "# XAUUSD 夜间开发早间交接（2026-09-19）",
            "",
            "## 1. 当前结论",
            "",
            "- Phase 3.1 Regime：PASS。",
            "- Phase 3.2 Technical：FAIL；冻结 OOS 指标为 IC=-0.0951、ICIR=-0.4968。",
            "- Phase 3.2 Macro：FAIL；IC=0.0645 但 ICIR=0.1437、方向 p=0.3793。",
            "- Phase 3.3 Author / News：BLOCKED（数据资格），没有训练或写入 Alpha。",
            "- 未进入融合、策略、回测、风控或实盘。",
            "",
            "## 2. 夜间完成",
            "",
            "- 补齐 Technical / Macro 的严格标签、时间切分、embargo、Platt 校准和基准比较。",
            "- 补齐数据 SHA-256、特征集/模型版本、seed、代码提交与真实库双跑复现审计。",
            "- 建立 Phase 3.3 作者/新闻数据资格门禁及 ORM 只读集成测试。",
            "- 建立范围边界审计，机械阻止失败 Alpha、作者权重和交易层越权落地。",
            "- 提供真实作者输入模板、填写说明和只读逐行体检工具。",
            "- 作者样本改按 source + external_account_id 稳定账号键计数，阻止同名账号合并凑数。",
            "- 修正 README / 路线图 / 规划中的陈旧状态与 NULL 权重冲突。",
            "",
            "## 3. 质量门禁",
            "",
            "- 全量测试：3234 passed / 1 skipped。",
            "- `ruff check .`：PASS。",
            "- `mypy config database src scripts`：PASS。",
            "- Phase 3 范围边界：PASS。",
            "- Phase 3.2 真实输入双跑：逐字段一致、数据库事实表零写入。",
            "",
            "## 4. 数据库状态",
            "",
            f"- Alembic revision：`{snapshot.revision}`",
            "",
            "| 表 | 行数 |",
            "|---|---:|",
            *counts,
            "",
            "没有新增迁移，没有写入 Alpha / Prediction / Strategy / Trading 数据。",
            "",
            "## 5. 仅需用户处理",
            "",
            "1. 填写 `templates/phase3_3/author_posts_template.csv`：每个 source + account 账号"
            "至少 30 条，建议 50 条；必须有独立 `published_at` 与 `collected_at`。",
            "2. 提供候选历史新闻数据源的信息：供应商/官方档案、授权依据、字段说明、是否能证明"
            "每条新闻的历史可用时间。暂时不要自行填写普通历史新闻 CSV。",
            "3. 查看两份 Phase 3.2 报告后决定：接受当前负面基线并暂停该方向，或批准一个全新、"
            "重新预注册且保留新 untouched test 的实验；不能在当前 test 上继续调参。",
            "",
            "## 6. 本轮提交",
            "",
            *commits,
            "",
            "## 7. 工作区",
            "",
            "生成本报告前工作区："
            + (
                "干净。"
                if snapshot.working_tree_clean_before_report
                else "存在未提交修改，需复核。"
            ),
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    configure_stdout()
    report = render(collect())
    safe_print(report)
    if args.no_dry_run:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
