"""作者技能快照组装层 CLI（build_opinion_labels → author_skill_snapshots）。

链路：``build_opinion_labels`` 输出 → :class:`OpinionLabel` → :class:`SkillSample`
→ :func:`compute_author_skill` → ``author_skill_snapshots``（幂等）。

报告分两层，严格区分合成（SYNTHETIC）与真实语料：
- Mock 层（SYNTHETIC）：展示 direction_skill_raw / direction_skill / timing_skill_raw /
  timing_skill / calibration_score 五列，标注「仅验证计算逻辑」；
- 真实层：展示「0 可用标签」，标注「待合法授权数据」。

防泄漏：``as_of`` 取所有 LABELED 标签的 ``exit_at`` 最大值（收益全部实现的最晚时点），
保证 ``compute_author_skill`` 的 ``effective_at <= as_of`` 过滤不含未来收益。

默认 dry-run（只计算 + 报告，不写库）；``--no-dry-run`` 才落库。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.models import Author, AuthorOpinion  # noqa: E402
from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.alpha.author_alpha import TIMING_THRESHOLD, AuthorSkillResult  # noqa: E402
from src.alpha.author_skill_assembly import (  # noqa: E402
    assemble_skill_samples,
    compute_all_author_skills,
    persist_skill_snapshots,
)
from src.common.time import parse_iso8601  # noqa: E402
from src.processors.opinion_labels import OpinionLabel, build_opinion_labels  # noqa: E402

DEFAULT_REPORT = REPO_ROOT / "logs" / "phase3_3_author_skill_report.md"
#: 合成数据标记（与 scripts/import_mock_posts.py 一致）
DATA_SOURCE_SYNTHETIC = "SYNTHETIC"


def resolve_as_of(labels: Sequence[OpinionLabel]) -> datetime:
    """评估时点 = 所有 LABELED 标签的 ``exit_at`` 最大值（收益全部实现，防未来泄漏）。

    无 LABELED 标签时回退当前 UTC 时刻（此时没有任何可评估样本，as_of 无实际影响）。
    """
    exits = [
        parse_iso8601(label.exit_at, field_name="label.exit_at")
        for label in labels
        if label.status == "LABELED" and label.exit_at
    ]
    if exits:
        return max(exits)
    return datetime.now(UTC)


def _format_score(value: float | None, digits: int = 4) -> str:
    """None → NOT_EVALUATED；否则保留 ``digits`` 位小数。"""
    return "NOT_EVALUATED" if value is None else f"{value:.{digits}f}"


def render_report(
    results: Sequence[AuthorSkillResult],
    *,
    authors: dict[str, Author],
    opinion_author: dict[str, str],
    labels: Sequence[OpinionLabel],
    as_of: datetime,
    written: int,
    dry_run: bool,
) -> str:
    """渲染 Markdown 报告：Mock 层（五列）+ 真实层（0 可用标签）。"""
    result_by_author = {result.author_id: result for result in results}
    opinion_counts = Counter(opinion_author.values())
    labeled_by_author: Counter[str] = Counter()
    for label in labels:
        author_id = opinion_author.get(label.opinion_id)
        if author_id is not None and label.status == "LABELED":
            labeled_by_author[author_id] += 1

    all_author_ids = sorted(set(opinion_counts) | set(result_by_author))

    def is_mock(author_id: str) -> bool:
        author = authors.get(author_id)
        if author is None:
            return False
        meta = author.metadata_json or {}
        return meta.get("data_source") == DATA_SOURCE_SYNTHETIC

    mock_ids = [aid for aid in all_author_ids if is_mock(aid)]
    real_ids = [aid for aid in all_author_ids if not is_mock(aid)]

    write_note = "dry-run，未写库" if dry_run else "幂等已提交"
    lines: list[str] = [
        "# 作者技能快照管线报告",
        "",
        "> 本次仅验证管线正确性，不作任何真实作者技能结论。",
        "",
        f"- 评估时点 as_of：{as_of.isoformat()}",
        f"- timing 阈值：{TIMING_THRESHOLD}（10 bps ≈ 黄金典型点差+滑点量级）",
        f"- 写入 author_skill_snapshots：{written} 行（{write_note}）",
        "",
        "## 1. Mock 层（SYNTHETIC，仅验证计算逻辑）",
        "",
        "| 作者 | 可评价样本 | direction_skill_raw | direction_skill | timing_skill_raw | "
        "timing_skill | calibration_score | ready |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    for aid in mock_ids:
        author = authors.get(aid)
        name = author.display_name if author is not None else aid
        result = result_by_author.get(aid)
        if result is None:
            lines.append(
                f"| {name} | 0 | NOT_EVALUATED | NOT_EVALUATED | NOT_EVALUATED | "
                "NOT_EVALUATED | NOT_EVALUATED | False |"
            )
            continue
        lines.append(
            f"| {name} | {result.sample_size} "
            f"| {_format_score(result.direction_raw)} "
            f"| {_format_score(result.direction_skill)} "
            f"| {_format_score(result.timing_raw, 6)} "
            f"| {_format_score(result.timing_skill)} "
            f"| {_format_score(result.calibration_score)} "
            f"| {result.ready} |"
        )

    lines += [
        "",
        "## 2. 真实层（待合法授权数据）",
        "",
        "| 作者 | 观点数 | 可用标签 |",
        "|---|---:|---:|",
    ]
    for aid in real_ids:
        author = authors.get(aid)
        name = author.display_name if author is not None else aid
        lines.append(f"| {name} | {opinion_counts.get(aid, 0)} | {labeled_by_author.get(aid, 0)} |")

    lines += [
        "",
        "> 真实层观点均为 UNTRUSTED_COLLECTION_TIME（输入缺独立 collected_at），",
        "> forward-return-v2 在查询行情前直接拒绝，0 可用标签，不参与任何技能结论。",
        "",
    ]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="作者技能快照组装层（报告分 Mock/真实两层）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[author-skill] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    try:
        with session_scope() as session:
            labels = build_opinion_labels(session)
            authors = {str(a.id): a for a in session.scalars(sa.select(Author)).all()}
            opinion_author = {
                str(o.id): str(o.author_id)
                for o in session.scalars(sa.select(AuthorOpinion)).all()
            }
            as_of = resolve_as_of(labels)
            samples = assemble_skill_samples(session, labels)
            results = compute_all_author_skills(samples, as_of=as_of)
            written = 0
            if not dry_run:
                written = persist_skill_snapshots(session, results)
        report = render_report(
            results,
            authors=authors,
            opinion_author=opinion_author,
            labels=labels,
            as_of=as_of,
            written=written,
            dry_run=dry_run,
        )
        safe_print(report)
        if not dry_run:
            Path(args.report).write_text(report, encoding="utf-8")
            safe_print(f"报告已写入：{args.report}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[author-skill] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

