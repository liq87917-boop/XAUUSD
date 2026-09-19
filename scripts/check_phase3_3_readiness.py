"""生成 Phase 3.3 Author / News Alpha 数据资格报告；只读、零写库。"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.alpha.evidence_gate import (  # noqa: E402
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
    Phase33Readiness,
    load_phase33_readiness,
)

REPORT = ROOT / "docs" / "experiments" / "Phase3_3_数据资格门禁报告.md"
HF_GOLD = ROOT / "logs" / "archive" / "phase2_hf_benchmark" / "hf_benchmark_texts.csv"


def _hf_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def render(result: Phase33Readiness) -> str:
    author_rows = [
        f"| {item.display_name} | {item.opinions} | {item.trusted_labels} | "
        f"{'PASS' if item.ready else 'BLOCKED'} |"
        for item in result.authors
    ] or ["| — | 0 | 0 | BLOCKED |"]
    news_rows = [
        f"| {source} | {count} | {count / result.news.events:.2%} |"
        for source, count in result.news.source_counts
    ] or ["| — | 0 | 0.00% |"]
    status_text = (
        ", ".join(f"{status}={count}" for status, count in result.label_status_counts) or "无标签"
    )
    return "\n".join(
        [
            "# Phase 3.3 Author / News Alpha 数据资格门禁报告",
            "",
            "> 本报告只判断数据能否进入 OOS Alpha；资格不足时不写技能、权重或信号事实。",
            "",
            "## 1. 结论",
            "",
            f"- Author Alpha：{'PASS' if result.author_ready else 'BLOCKED'}。",
            f"- News Alpha：{'PASS' if result.news_ready else 'BLOCKED'}。",
            "- 因 Phase 3.2 两个基线均为负面结果，本轮不做任何跨 Alpha 融合。",
            "",
            "## 2. Author 资格",
            "",
            f"硬门槛：每位作者至少 {MIN_AUTHOR_SAMPLES} 条可信、可标注观点。",
            "",
            "| 作者 | 观点数 | 可信标签数 | 状态 |",
            "|---|---:|---:|---|",
            *author_rows,
            "",
            f"标签状态：{status_text}。",
            "",
            "资格不足时不写 `author_skill_snapshots` 或 `author_weight_snapshots`。"
            "现有 `weight` 为 NOT NULL，因此旧规划中的“写 NULL 权重”不可执行，已修正文档。",
            "",
            "## 3. News 资格",
            "",
            f"预注册工程下限：事件数 >= {MIN_NEWS_EVENTS}、"
            f"可用时间跨度 >= {MIN_NEWS_HISTORY_DAYS} 天、"
            f"单一来源占比 <= {MAX_NEWS_SOURCE_SHARE:.0%}。",
            "",
            f"- 事件数：{result.news.events}",
            f"- 可用时间跨度：{result.news.history_days} 天"
            "（逐条取 news_events 与 raw_items 的较晚 effective_at）",
            f"- 最大来源占比：{result.news.largest_source_share:.2%}",
            "",
            "| 来源 | 事件数 | 占比 |",
            "|---|---:|---:|",
            *news_rows,
            "",
            f"HF 黄金标题弱监督行数：{result.hf_weak_supervision_rows}。该数据没有本项目可审计的"
            "事件发布时间链，不能直接生成黄金前瞻收益标签，也不得回灌为观点金标准。",
            "",
            "## 4. 下一动作",
            "",
            "1. 保留只读门禁和负面证据，不创建 Phase 3.3 条件权重迁移。",
            "2. 等待真实作者帖子具备独立 `published_at` 与 `collected_at` 后重新跑标签。",
            "3. 新闻不能靠今天下载旧标题直接补历史：现有契约要求 `effective_at >= collected_at`。",
            "   须取得能证明历史可用时间的合规数据源；未达到门槛前不训练 News Alpha。",
            "4. 可继续开发与数据无关的泄漏测试、报告和门禁，但不得产出效果结论。",
            "",
            "用户交接字段与禁止事项见 `docs/operations/Phase3_3_用户数据交接说明.md`。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    configure_stdout()
    factory = build_session_factory(build_engine())
    with factory() as session:
        result = load_phase33_readiness(session, hf_weak_supervision_rows=_hf_rows(HF_GOLD))
    report = render(result)
    safe_print(report)
    if args.no_dry_run:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
