"""Phase 2 基线对比：**同一份人工金标准**下，正则抽取器 vs LLM 抽取器的逐字段提升。

对应文档：
- `docs/08 §5` Phase 2 验收门槛（stance ≥ 90%、horizon ≥ 85%、entry/SL/TP ≥ 90%）；
- `docs/10 §4.9` 判分口径（人工子集是**唯一绝对金标准**，共识子集只作参考）；
- 输入：`logs/extractor_eval.csv`（`mock-regex-v1`）与 `logs/extractor_eval_llm.csv`
  （`llm-deepseek-v*`），两份都必须是**同一份 `logs/ground_truth_200.csv`** 跑出来的。

设计要点：
1. **只用两份现成明细**做对比（不重跑抽取器、不碰数据库、不联网）→ 可复现、零成本；
2. 先**对齐校验**：同一 `(post_id, field)` 的金标准值/来源必须一致，否则报问题并要求人工确认
   （防止"拿两份不同金标准的结果做对比"这种最常见的口径事故）；
3. 输出的对比报告固定包含：人工子集逐字段准确率 + **提升百分点** + 五类判定构成 +
   **"正则错、LLM 对"的格子数（修复数）** + Token/费用台账 + PASS/FAIL 结论。

用法::

    python scripts/compare_extractor_baselines.py
    python scripts/compare_extractor_baselines.py --regex logs/extractor_eval.csv \\
        --llm logs/extractor_eval_llm.csv --usage logs/extractor_eval_llm_usage.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout  # noqa: E402
from scripts.build_ground_truth import SOURCE_HUMAN  # noqa: E402
from scripts.compare_model_annotations import file_digest  # noqa: E402
from scripts.evaluate_extractor import CORE_FIELDS, FIELD_LABEL_CN, THRESHOLDS  # noqa: E402

DEFAULT_REGEX: Final[Path] = REPO_ROOT / "logs" / "extractor_eval.csv"
DEFAULT_LLM: Final[Path] = REPO_ROOT / "logs" / "extractor_eval_llm.csv"
DEFAULT_USAGE: Final[Path] = REPO_ROOT / "logs" / "extractor_eval_llm_usage.json"
DEFAULT_GOLD: Final[Path] = REPO_ROOT / "logs" / "ground_truth_200.csv"
#: 金标准的**改判前**版本（用于识别"模型纠正人工"的格子；不存在则本节跳过）
DEFAULT_GOLD_BEFORE: Final[Path] = (
    REPO_ROOT / "logs" / "archive" / "ground_truth_200_pre_20260913.csv"
)
#: 帖子正文（用于在案例里引用原文）
DEFAULT_TEXTS: Final[Path] = REPO_ROOT / "logs" / "annotation_sample.csv"
DEFAULT_REPORT: Final[Path] = (
    REPO_ROOT / "docs" / "experiments" / "Phase 2 基线对比报告.md"
)
#: 案例小节最多列多少格
DEFAULT_CASE_LIMIT: Final[int] = 20
#: 对比的字段顺序（核心四字段 + 侧字段）
COMPARE_FIELDS: Final[tuple[str, ...]] = (*CORE_FIELDS, "information_type")
OUTCOME_HIT: Final[str] = "correct_value"
OUTCOME_ABSENT: Final[str] = "correct_absent"
OUTCOME_MISSED: Final[str] = "missed"
OUTCOME_WRONG: Final[str] = "wrong_value"
OUTCOME_SPURIOUS: Final[str] = "spurious"


@dataclass(slots=True)
class FieldStat:
    """单字段统计（`accuracy` 的分母是**金标准有值**的格子，与 `docs/08 §5` 一致）。"""

    name: str
    cells: int = 0
    gold_present: int = 0
    hit: int = 0
    absent: int = 0
    missed: int = 0
    wrong: int = 0
    spurious: int = 0

    @property
    def accuracy(self) -> float:
        return self.hit / self.gold_present if self.gold_present else 0.0

    @property
    def precision(self) -> float:
        given = self.hit + self.wrong + self.spurious
        return self.hit / given if given else 0.0

    @property
    def exact(self) -> float:
        return (self.hit + self.absent) / self.cells if self.cells else 0.0


@dataclass(slots=True)
class Comparison:
    """两套抽取器的对齐结果。"""

    pairs: dict[tuple[str, str], tuple[dict[str, str], dict[str, str]]] = field(
        default_factory=dict
    )
    problems: list[str] = field(default_factory=list)

    def fixed_by_llm(self) -> list[tuple[str, str]]:
        """正则未命中、LLM 命中的格子（"LLM 修好了什么"）。"""
        return sorted(
            key
            for key, (regex, llm) in self.pairs.items()
            if regex["outcome"] != OUTCOME_HIT and llm["outcome"] == OUTCOME_HIT
        )

    def regressed_by_llm(self) -> list[tuple[str, str]]:
        """正则命中、LLM 未命中的格子（"LLM 弄坏了什么"，必须逐格解释）。"""
        return sorted(
            key
            for key, (regex, llm) in self.pairs.items()
            if regex["outcome"] == OUTCOME_HIT and llm["outcome"] != OUTCOME_HIT
        )


def read_eval_csv(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """读逐格明细 CSV → ``{(post_id, field): row}``（缺列/重复格 -> 抛错）。"""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    reader = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
    for row in reader:
        key = (row.get("post_id", ""), row.get("field", ""))
        if not key[0] or not key[1]:
            raise ValueError(f"明细行缺少 post_id/field：{row}")
        if key in rows:
            raise ValueError(f"明细里出现重复格子：{key}")
        rows[key] = {(name or ""): (value or "") for name, value in row.items() if name}
    return rows


def join_runs(
    regex_rows: Mapping[tuple[str, str], Mapping[str, str]],
    llm_rows: Mapping[tuple[str, str], Mapping[str, str]],
) -> Comparison:
    """对齐两份明细，并**校验金标准一致性**（不一致即记为问题，不静默比较）。"""
    result = Comparison()
    for key, regex in regex_rows.items():
        llm = llm_rows.get(key)
        if llm is None:
            result.problems.append(f"LLM 明细缺少格子：{key[0]}|{key[1]}")
            continue
        if regex["gold_value"] != llm["gold_value"] or regex["gold_source"] != llm["gold_source"]:
            result.problems.append(
                f"金标准不一致（{key[0]}|{key[1]}）：正则侧 "
                f"{regex['gold_value'] or '空'}({regex['gold_source']}) vs LLM 侧 "
                f"{llm['gold_value'] or '空'}({llm['gold_source']})"
            )
            continue
        result.pairs[key] = (dict(regex), dict(llm))
    for key in sorted(set(llm_rows) - set(regex_rows)):
        result.problems.append(f"正则明细缺少格子：{key[0]}|{key[1]}")
    return result


def field_stats(
    comparison: Comparison,
    *,
    side: str,
    source: str | None = SOURCE_HUMAN,
    fields: Sequence[str] = COMPARE_FIELDS,
) -> dict[str, FieldStat]:
    """按字段汇总（`side` = `regex` / `llm`；`source=None` 表示全量口径）。"""
    stats = {name: FieldStat(name=name) for name in fields}
    for (_post_id, name), sides in comparison.pairs.items():
        target = stats.get(name)
        if target is None:
            continue
        row = sides[0] if side == "regex" else sides[1]
        if source is not None and row["gold_source"] != source:
            continue
        target.cells += 1
        if row["gold_value"]:
            target.gold_present += 1
        outcome = row["outcome"]
        if outcome == OUTCOME_HIT:
            target.hit += 1
        elif outcome == OUTCOME_ABSENT:
            target.absent += 1
        elif outcome == OUTCOME_MISSED:
            target.missed += 1
        elif outcome == OUTCOME_WRONG:
            target.wrong += 1
        else:
            target.spurious += 1
    return stats


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _delta(before: float, after: float) -> str:
    change = (after - before) * 100
    sign = "+" if change >= 0 else ""
    return f"**{sign}{change:.1f} pp**"


def _verdict(name: str, accuracy: float) -> str:
    threshold = THRESHOLDS.get(name)
    if threshold is None:
        return "—（无门槛）"
    return f"{'✅ PASS' if accuracy >= threshold else '❌ FAIL'}（门槛 ≥ {_pct(threshold)}）"


def _usage_lines(usage: Mapping[str, Any] | None) -> list[str]:
    """Token/费用小节（缺台账时显式说明，不留空白）。"""
    if not usage:
        return ["- （未提供 LLM 台账 `--usage`，本节留空）"]
    return [f"- `{key}`：`{value}`" for key, value in sorted(usage.items())]


def read_gold_cells(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """读金标准长表 → ``{(post_id, field): row}``（只用 value/source/reviewer/note）。"""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    reader = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
    for row in reader:
        key = (row.get("post_id", ""), row.get("field", ""))
        if key[0] and key[1]:
            rows[key] = {(name or ""): (value or "") for name, value in row.items() if name}
    return rows


def read_texts(path: Path) -> dict[str, str]:
    """读帖子正文（`post_id -> text_content`）。"""
    rows: dict[str, str] = {}
    reader = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
    for row in reader:
        post_id = (row.get("post_id") or "").strip()
        if post_id:
            rows[post_id] = (row.get("text_content") or "").strip()
    return rows


def _snippet(text: str, *, width: int = 60) -> str:
    return text if len(text) <= width else f"{text[:width]}…"


def _gold_adoption_lines(
    comparison: Comparison,
    gold_before: Mapping[tuple[str, str], Mapping[str, str]] | None,
    gold_now: Mapping[tuple[str, str], Mapping[str, str]] | None,
    texts: Mapping[str, str],
    *,
    limit: int = DEFAULT_CASE_LIMIT,
) -> list[str]:
    """**模型纠正人工**：金标准被改判的格子 + LLM 是否已对齐。"""
    if not gold_before or not gold_now:
        return ["- （未提供改判前金标准 `--gold-before`，本节跳过）"]
    changed = sorted(
        key
        for key in set(gold_before) & set(gold_now)
        if (gold_before[key].get("value") or "") != (gold_now[key].get("value") or "")
    )
    if not changed:
        return ["- （未发现金标准改判记录）"]
    lines: list[str] = []
    for post_id, field_name in changed[:limit]:
        before = gold_before[(post_id, field_name)].get("value", "")
        after = gold_now[(post_id, field_name)]
        reviewer = after.get("reviewer", "") or "（未记）"
        note = _snippet(after.get("note", ""), width=120)
        llm = comparison.pairs.get((post_id, field_name), ({}, {}))[1]
        pred = llm.get("pred_value", "")
        aligned = (
            "✅ 已对齐（LLM 与改判后金标准一致）"
            if pred == after.get("value", "")
            else f"⚠️ 未对齐（LLM 现判 `{pred or '未给出'}`）"
        )
        label = FIELD_LABEL_CN.get(field_name, field_name)
        lines.append(
            f"- **`{post_id}` / {label}**：`{before}` → `{after.get('value', '')}`"
            f"（裁决人 `{reviewer}`；改判理由：{note}）"
        )
        lines.append(f"  - {aligned}")
        text = texts.get(post_id)
        if text:
            lines.append(f"  - 原文：{_snippet(text)}")
    return lines


def _model_error_lines(
    comparison: Comparison,
    texts: Mapping[str, str],
    *,
    source: str = SOURCE_HUMAN,
    limit: int = DEFAULT_CASE_LIMIT,
) -> list[str]:
    """**人工纠正模型**：金标准维持、模型判错的格子（逐格给原文，便于人工解释或再裁决）。"""
    lines: list[str] = []
    for key in sorted(comparison.pairs):
        regex_row, llm_row = comparison.pairs[key]
        if llm_row.get("outcome") not in {OUTCOME_WRONG, OUTCOME_MISSED}:
            continue
        if llm_row.get("gold_source") != source:
            continue
        post_id, field_name = key
        gold = llm_row.get("gold_value") or "未给出"
        pred = llm_row.get("pred_value") or "未给出"
        label = FIELD_LABEL_CN.get(field_name, field_name)
        lines.append(
            f"- **`{post_id}` / {label}**：金标准 `{gold}` ↔ 模型 `{pred}`"
            f"（{llm_row.get('outcome')}）；正则侧 `{regex_row.get('pred_value') or '未给出'}`"
        )
        text = texts.get(post_id)
        if text:
            lines.append(f"  - 原文：{_snippet(text)}")
        if len(lines) >= limit * 2:
            lines.append(f"- …（仅列前 {limit} 格）")
            break
    return lines or ["- （人工子集内没有「金标准对、模型错」的格子）"]





def render_report(
    comparison: Comparison,
    *,
    regex_path: Path,
    llm_path: Path,
    gold_path: Path,
    gold_digest: str,
    generated_at: datetime,
    usage: Mapping[str, Any] | None = None,
    gold_before: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    gold_now: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    texts: Mapping[str, str] | None = None,
    case_limit: int = DEFAULT_CASE_LIMIT,
) -> str:
    """生成《Phase 2 基线对比报告.md》（纯函数，便于单测）。"""
    texts = texts or {}
    regex_stats = field_stats(comparison, side="regex")
    llm_stats = field_stats(comparison, side="llm")
    regex_all = field_stats(comparison, side="regex", source=None)
    llm_all = field_stats(comparison, side="llm", source=None)
    fixed = comparison.fixed_by_llm()
    regressed = comparison.regressed_by_llm()
    lines: list[str] = ["# Phase 2 基线对比报告 · 正则抽取器 vs LLM 抽取器", ""]
    add = lines.append
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 金标准：`{gold_path}`（sha256 前 16 位 `{gold_digest}`）")
    add(f"- 正则明细：`{regex_path}`")
    add(f"- LLM 明细：`{llm_path}`")
    add(f"- 对比格子：**{len(comparison.pairs)}** 格（200 帖 × 5 字段）")
    add("")
    add(
        "> **判定依据**：`source=human-adjudicated` 子集是**唯一绝对金标准**；"
        "`3-model-consensus` 子集只作参考。LLM 明细里的 `spurious` 若来自共识金标准的"
        "「未给出」格（条件句/引用句/复盘句的点位），属**金标准分层差异**而非模型错误——"
        "见 `docs/10 §4.9` 规则 8。"
    )
    add("")
    add("## 1. 逐字段准确率（人工子集口径 = 验收口径）")
    add("")
    add(
        "| 字段 | 金标准有值 | 正则命中 | 正则准确率 | LLM 命中 | LLM 准确率 "
        "| 提升 | 结论（按 LLM） |"
    )
    add("|---|---|---|---|---|---|---|---|")
    for name in COMPARE_FIELDS:
        before, after = regex_stats[name], llm_stats[name]
        add(
            f"| {FIELD_LABEL_CN.get(name, name)}（`{name}`） | {after.gold_present} "
            f"| {before.hit} | {_pct(before.accuracy)} | {after.hit} | {_pct(after.accuracy)} "
            f"| {_delta(before.accuracy, after.accuracy)} | {_verdict(name, after.accuracy)} |"
        )
    add("")
    add("## 2. 五类判定构成（全量口径，含共识格）")
    add("")
    add("| 抽取器 | 字段 | 格子 | 命中 | 双方未给出 | 该判未判 | 提取错误 | 不该判却判 | 精确率 |")
    add("|---|---|---|---|---|---|---|---|---|")
    for side, stats in (("正则", regex_all), ("LLM", llm_all)):
        for name in COMPARE_FIELDS:
            item = stats[name]
            add(
                f"| {side} | {FIELD_LABEL_CN.get(name, name)} | {item.cells} | {item.hit} "
                f"| {item.absent} | {item.missed} | {item.wrong} | {item.spurious} "
                f"| {_pct(item.precision)} |"
            )
    add("")
    add("## 3. LLM 相对正则的逐格变化")
    add("")
    add(f"- **修复（正则未命中 → LLM 命中）：{len(fixed)} 格**")
    for post_id, name in fixed[:12]:
        add(f"  - `{post_id}` / {FIELD_LABEL_CN.get(name, name)}")
    if len(fixed) > 12:
        add(f"  - …其余 {len(fixed) - 12} 格见 `logs/extractor_eval_llm.csv`")
    add(f"- **回归（正则命中 → LLM 未命中）：{len(regressed)} 格**")
    for post_id, name in regressed[:12]:
        regex_row, llm_row = comparison.pairs[(post_id, name)]
        add(
            f"  - `{post_id}` / {FIELD_LABEL_CN.get(name, name)}："
            f"金标准 `{regex_row['gold_value']}`，正则 `{regex_row['pred_value']}`（命中），"
            f"LLM `{llm_row['pred_value'] or '未给出'}`（{llm_row['outcome']}）"
        )
    if not regressed:
        add("  - （无）")
    add("")
    add("## 4. Token 与费用（LLM 侧）")
    add("")
    lines.extend(_usage_lines(usage))
    add("")
    add("## 5. 人工 ↔ 模型 对齐案例")
    add("")
    add("### 5.1 模型纠正人工（金标准按模型的答案改判）")
    add("")
    lines.extend(_gold_adoption_lines(comparison, gold_before, gold_now, texts))
    add("")
    add("### 5.2 人工纠正模型（金标准维持、模型判错——需人工解释或再裁决）")
    add("")
    lines.extend(_model_error_lines(comparison, texts))
    add("")
    add("## 6. 结论与下一步")
    add("")
    passed = [
        n for n in COMPARE_FIELDS if n in THRESHOLDS and llm_stats[n].accuracy >= THRESHOLDS[n]
    ]
    failed = [n for n in COMPARE_FIELDS if n in THRESHOLDS and n not in passed]
    regex_passed = sum(1 for n in THRESHOLDS if regex_stats[n].accuracy >= THRESHOLDS[n])
    add(
        f"- **人工子集口径**：LLM **PASS {len(passed)} 项**"
        f"（{'、'.join(FIELD_LABEL_CN.get(n, n) for n in passed) or '无'}）、"
        f"**FAIL {len(failed)} 项**"
        f"（{'、'.join(FIELD_LABEL_CN.get(n, n) for n in failed) or '无'}）；"
        f"同一口径下正则 PASS {regex_passed} 项。"
    )
    info_accuracy = llm_stats["information_type"].accuracy
    if info_accuracy >= 0.90:
        add(
            f"- 信息类型 **{_pct(info_accuracy)}**（≥ 90%）；残余分歧集中在 "
            "**「操作块 + 驱动尾句」**形态，逐格见 §5.2，已登记 **TD-29**（待真实语料复核）。"
        )
    else:
        add(
            f"- 信息类型 **{_pct(info_accuracy)}**（< 90%）：按 `docs/10 §4.9` 规则 9（L1~L6 阶梯）"
            "继续收敛口径，或把 §5.2 的格子提交**人工二次裁决**"
            "（模型纠正人工 / 人工纠正模型都属正常对齐）。"
        )
    add(
        "- 本批语料为 **Mock 语料**（模板生成、含 126 条对抗样本）；"
        "**真实语料验收必须用真实作者帖子 + 全人工裁决重新抽样**（`docs/08 §5`）。"
    )
    add("")
    add("复现命令：")
    add("")
    add("```powershell")
    add("python scripts/evaluate_extractor.py --extractor regex")
    add("python scripts/evaluate_extractor.py --extractor llm")
    add("python scripts/compare_extractor_baselines.py")
    add("```")
    add("")
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.compare_extractor_baselines",
        description="对比正则抽取器与 LLM 抽取器的逐字段准确率（只读文件，不碰数据库）",
    )
    parser.add_argument("--regex", default=str(DEFAULT_REGEX), help="正则抽取器逐格明细")
    parser.add_argument("--llm", default=str(DEFAULT_LLM), help="LLM 抽取器逐格明细")
    parser.add_argument("--usage", default=str(DEFAULT_USAGE), help="LLM token/费用台账 JSON")
    parser.add_argument("--gold", default=str(DEFAULT_GOLD), help="金标准（仅用于记录摘要）")
    parser.add_argument(
        "--gold-before",
        default=str(DEFAULT_GOLD_BEFORE),
        help="改判前的金标准（用于 5.1「模型纠正人工」小节；不存在则跳过）",
    )
    parser.add_argument(
        "--texts", default=str(DEFAULT_TEXTS), help="帖子正文（用于案例小节引用原文）"
    )
    parser.add_argument(
        "--case-limit", type=int, default=DEFAULT_CASE_LIMIT, help="案例小节最多列多少格"
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写报告")
    return parser.parse_args(argv)


def _print_summary(
    regex_stats: Mapping[str, FieldStat],
    llm_stats: Mapping[str, FieldStat],
    comparison: Comparison,
) -> None:
    """控制台摘要（人工子集口径 + 修复/回归计数）。"""
    print(f"[compare] 对齐格子 {len(comparison.pairs)}；口径问题 {len(comparison.problems)}")
    for name in COMPARE_FIELDS:
        before, after = regex_stats[name], llm_stats[name]
        print(
            f"[compare]   {name:<17} 有值 {after.gold_present:>3} | 正则 {_pct(before.accuracy):>6}"
            f" → LLM {_pct(after.accuracy):>6} | 提升 {_delta(before.accuracy, after.accuracy)}"
        )
    print(
        f"[compare] 修复 {len(comparison.fixed_by_llm())} 格 / 回归 "
        f"{len(comparison.regressed_by_llm())} 格"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """跑对比并导出报告；退出码 0=成功 / 2=输入或口径问题。"""
    configure_stdout()
    args = _parse_args(argv)
    regex_path, llm_path = Path(args.regex), Path(args.llm)
    for path in (regex_path, llm_path):
        if not path.exists():
            print(f"[compare] 输入文件不存在：{path}", file=sys.stderr)
            return 2
    try:
        comparison = join_runs(read_eval_csv(regex_path), read_eval_csv(llm_path))
    except (ValueError, OSError) as exc:
        print(f"[compare] 读取失败：{exc}", file=sys.stderr)
        return 2
    _print_summary(
        field_stats(comparison, side="regex"), field_stats(comparison, side="llm"), comparison
    )
    if comparison.problems:
        for problem in comparison.problems[:5]:
            print(f"[compare][问题] {problem}", file=sys.stderr)
        print(
            f"[compare] 共 {len(comparison.problems)} 个口径问题，请先对齐金标准", file=sys.stderr
        )
        return 2
    if args.dry_run:
        print("[compare] --dry-run：未写报告")
        return 0
    usage: Mapping[str, Any] | None = None
    usage_path = Path(args.usage)
    if usage_path.exists():
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            raw = payload.get("usage")
            usage = raw if isinstance(raw, Mapping) else payload
    gold_path = Path(args.gold)
    report_path = Path(args.report)
    gold_before_path = Path(args.gold_before)
    texts_path = Path(args.texts)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            comparison,
            regex_path=regex_path,
            llm_path=llm_path,
            gold_path=gold_path,
            gold_digest=file_digest(gold_path) if gold_path.exists() else "（未知）",
            generated_at=datetime.now(UTC),
            usage=usage,
            gold_before=read_gold_cells(gold_before_path) if gold_before_path.exists() else None,
            gold_now=read_gold_cells(gold_path) if gold_path.exists() else None,
            texts=read_texts(texts_path) if texts_path.exists() else {},
            case_limit=args.case_limit,
        ),
        encoding="utf-8",
    )
    print(f"[compare] 报告：{report_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())

