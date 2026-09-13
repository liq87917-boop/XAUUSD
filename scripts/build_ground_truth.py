"""金标准合并脚本：人工裁决 + 三模型共识 → `logs/ground_truth_200.csv`（+《金标准生成报告》）。

红线（`.clinerules` / `docs/10`）：
1. **模型输出不是金标准**。只有两类条目会进 ground truth：
   - `source=human-adjudicated`：`pending_review.*` 里人工填写的 `final_gold_standard`；
   - `source=3-model-consensus`：三模型完全一致的字段（`provenance` 显式标注"未经人工确认"），
     评估时可**分层使用**（人工子集看真实准确率，共识子集只能当参考）。
2. **漏填绝不回填模型值**（否则等于让模型冒充金标准）：
   - `final_gold_standard` 空 **且** `notes` 也空 → 视为"未裁决"，不产出条目，单独列出并退出码 3；
   - 空但 `notes` 有内容 → 视为**人工裁决"未给出"**（例如 notes 写"文本没有提到，留空"），
     产出 `value=""` 的人工条目——这是合法金标准：该字段确实不存在。
3. 输入按**内容**识别（Excel 常把 `.csv` 另存成 xlsx，编码可能是 UTF-8/GBK）；
   人工裁决文件会先**归档复制**到 `logs/archive/`，避免后续重跑覆盖（人工成果只此一份）。
4. 只读输入、只写 CSV/Markdown，**不碰数据库**。

用法::

    python scripts/build_ground_truth.py
    python scripts/build_ground_truth.py --pending logs/pending_review.csv --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种运行方式（同 `scripts/sample_annotation_set.py`）：脚本模式需要仓库根在 sys.path 上
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 已安装（editable）时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
import argparse
import csv
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from scripts._console import configure_stdout
from scripts.compare_model_annotations import (
    DEFAULT_FIELDS,
    FIELD_LABELS,
    MODELS,
    SheetTable,
    file_digest,
    normalize_value,
    read_any_table,
    sniff_is_xlsx,
)

DEFAULT_PENDING: Final[Path] = REPO_ROOT / "logs" / "pending_review.csv"
DEFAULT_CONSENSUS: Final[Path] = REPO_ROOT / "logs" / "model_consensus.csv"
DEFAULT_GROUND_TRUTH: Final[Path] = REPO_ROOT / "logs" / "ground_truth_200.csv"
DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "experiments" / "ground_truth_report.md"
DEFAULT_ARCHIVE_DIR: Final[Path] = REPO_ROOT / "logs" / "archive"
#: 网格完整性参考（抽样清单：200 个样本）——用于验证 1000 个格子的覆盖
DEFAULT_REFERENCE_CSV: Final[Path] = REPO_ROOT / "logs" / "annotation_sample.csv"

#: 金标准长表列（`value` = 归一化规范值，`value_raw` = 原始文本）
GROUND_TRUTH_COLUMNS: Final[tuple[str, ...]] = (
    "post_id",
    "field",
    "field_label",
    "value",
    "value_raw",
    "source",
    "provenance",
    "models_agreement",
    "overturn",
    "reviewer",
    "note",
)
SOURCE_HUMAN: Final[str] = "human-adjudicated"
SOURCE_CONSENSUS: Final[str] = "3-model-consensus"
#: 人工裁决与模型的关系（仅对 pending 行有意义）
OVERTURN_MAJORITY: Final[str] = "matches-majority"  # 采纳多数派（2:1 里的多数）
OVERTURN_MINORITY: Final[str] = "matches-minority"  # 采纳少数派 → 推翻多数派
OVERTURN_SINGLE: Final[str] = "matches-single"  # 三家各异时采纳其中一家（无多数派）
OVERTURN_NONE: Final[str] = "matches-none"  # 三家都错：人工给出第四种答案
#: **计入"推翻模型"的标签**（采纳少数派 / 三家都错）
OVERTURN_LABELS: Final[frozenset[str]] = frozenset({OVERTURN_MINORITY, OVERTURN_NONE})
OVERTURN_LABEL_CN: Final[dict[str, str]] = {
    OVERTURN_MAJORITY: "采纳多数派（未推翻）",
    OVERTURN_MINORITY: "采纳少数派（推翻多数派）",
    OVERTURN_SINGLE: "三家各异·采纳其一（未推翻）",
    OVERTURN_NONE: "三家都错·新答案（推翻全部）",
}
CONSENSUS_NOTE: Final[str] = "三模型一致；未经人工确认，评估时请分层使用"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TruthRow:
    """一行金标准（`post_id` × `field`）。"""

    post_id: str
    field_name: str
    #: 归一化后的规范值（机器用）；空串 = 该字段"未给出"（合法金标准）
    value: str
    #: 原始文本（人工填写原样 / 模型共识原值），供人复核
    value_raw: str
    source: str
    provenance: str
    models_agreement: str
    overturn: str
    reviewer: str
    note: str

    def to_row(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "field": self.field_name,
            "field_label": FIELD_LABELS.get(self.field_name, self.field_name),
            "value": self.value,
            "value_raw": self.value_raw,
            "source": self.source,
            "provenance": self.provenance,
            "models_agreement": self.models_agreement,
            "overturn": self.overturn,
            "reviewer": self.reviewer,
            "note": self.note,
        }


@dataclass(slots=True)
class BuildStats:
    """合并统计（写进报告）。"""

    pending_rows_total: int = 0
    pending_with_value: int = 0
    pending_intentional_blank: int = 0
    pending_unadjudicated: int = 0
    pending_unadjudicated_ids: list[str] = field(default_factory=list)
    consensus_rows_total: int = 0
    consensus_with_value: int = 0
    consensus_blank: int = 0
    overturn_counts: dict[str, int] = field(default_factory=dict)
    overturn_by_field: dict[str, dict[str, int]] = field(default_factory=dict)
    coverage_by_field: dict[str, dict[str, int]] = field(default_factory=dict)
    expected_cells: int = 0
    grid_missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def pending_adjudicated(self) -> int:
        """已人工裁决的格子（含"有意留空=未给出"）。"""
        return self.pending_with_value + self.pending_intentional_blank

    @property
    def covered_cells(self) -> int:
        """有金标准的格子（人工裁决 + 三模型共识）。"""
        return self.pending_adjudicated + self.consensus_rows_total

    @property
    def value_cells(self) -> int:
        """其中**有具体取值**的格子（不含"未给出"）。"""
        return self.pending_with_value + self.consensus_with_value

    @property
    def coverage_rate(self) -> float:
        return self.covered_cells / self.expected_cells if self.expected_cells else 0.0

    @property
    def overturn_total(self) -> int:
        """人工**推翻模型**的次数（采纳少数派 + 三家都错）。"""
        return sum(self.overturn_counts.get(label, 0) for label in OVERTURN_LABELS)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _cell(row: Mapping[str, str], name: str) -> str:
    return (row.get(name) or "").strip()


def classify_overturn(
    human_value: str, model_values: Mapping[str, str], agreeing_models: Sequence[str]
) -> str:
    """判断人工裁决与三模型的关系（`pending` 行本身就是"至少一家不同"的分歧行）。

    - 2:1 行：人工取值 == 多数派 → `matches-majority`；否则 `matches-minority`（采纳少数派）
      或 `matches-none`（三家都错）；
    - 三方各异行（无多数派）：人工取值命中其中一家 → `matches-single`；都不命中 → `matches-none`。
    """
    matches = [model for model in MODELS if model_values.get(model, "") == human_value]
    if len(agreeing_models) == 2:
        majority_value = model_values.get(agreeing_models[0], "")
        if human_value == majority_value:
            return OVERTURN_MAJORITY
        return OVERTURN_MINORITY if matches else OVERTURN_NONE
    return OVERTURN_SINGLE if matches else OVERTURN_NONE


def archive_input(path: Path, archive_dir: Path, *, stamp: str) -> Path:
    """把人工裁决输入**字节级复制**到归档目录（人工成果只此一份，先保住再处理）。"""
    archive_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".xlsx" if sniff_is_xlsx(path) else (path.suffix or ".csv")
    target = archive_dir / f"{path.stem}_adjudicated_{stamp}{suffix}"
    shutil.copy2(path, target)
    return target


def reference_grid(path: Path, fields: Sequence[str]) -> list[tuple[str, str]]:
    """参考网格：抽样清单里每个样本 × 每个字段（用于验证金标准覆盖完整）。"""
    if not path.exists():
        return []
    table = read_any_table(path)
    post_ids = [_cell(row, "post_id") for row in table.rows]
    return [(post_id, name) for post_id in post_ids if post_id for name in fields]


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def merge(
    pending: SheetTable,
    consensus: SheetTable,
    *,
    expected: Sequence[tuple[str, str]] = (),
    fields: Sequence[str] = DEFAULT_FIELDS,
) -> tuple[list[TruthRow], BuildStats]:
    """把「人工裁决」与「三模型共识」合并成长表金标准，并产出统计。"""
    fallback_cells = len(pending.rows) + len(consensus.rows)
    stats = BuildStats(expected_cells=len(expected) or fallback_cells)
    truth: list[TruthRow] = []
    seen: dict[tuple[str, str], str] = {}

    def add(row: TruthRow) -> None:
        key = (row.post_id, row.field_name)
        if key in seen:
            stats.problems.append(
                f"格子重复：{row.post_id}|{row.field_name}（{seen[key]} 与 {row.source}）"
            )
            return
        seen[key] = row.source
        truth.append(row)

    # ---------------- 人工裁决（优先级最高） ----------------
    for raw in pending.rows:
        stats.pending_rows_total += 1
        field_name = _cell(raw, "field")
        post_id = _cell(raw, "post_id")
        value_raw = _cell(raw, "final_gold_standard")
        note = _cell(raw, "notes")
        reviewer = _cell(raw, "reviewer")
        if not value_raw and not note:
            # 空值且没有任何理由说明 → 视为"未裁决"，绝不回填模型值
            stats.pending_unadjudicated += 1
            stats.pending_unadjudicated_ids.append(
                f"{_cell(raw, 'review_id')} | {post_id} | {field_name} | "
                f"db/qw/bd={_cell(raw, 'model_db')}/{_cell(raw, 'model_qw')}/"
                f"{_cell(raw, 'model_bd')}"
            )
            continue
        agreeing = [name for name in _cell(raw, "agreeing_models").split("/") if name]
        model_values = {
            model: normalize_value(field_name, _cell(raw, f"normalized_{model}"))
            for model in MODELS
        }
        value = normalize_value(field_name, value_raw)
        if value_raw:
            stats.pending_with_value += 1
        else:
            stats.pending_intentional_blank += 1
            note = note or "人工裁决为「未给出」"
        overturn = classify_overturn(value, model_values, agreeing)
        stats.overturn_counts[overturn] = stats.overturn_counts.get(overturn, 0) + 1
        per_field = stats.overturn_by_field.setdefault(field_name, {})
        per_field[overturn] = per_field.get(overturn, 0) + 1
        coverage = stats.coverage_by_field.setdefault(
            field_name, {"human": 0, "consensus": 0, "blank": 0}
        )
        coverage["human"] += 1
        raw_models = "/".join(_cell(raw, f"model_{model}") or "∅" for model in MODELS)
        add(
            TruthRow(
                post_id=post_id,
                field_name=field_name,
                value=value,
                value_raw=value_raw,
                source=SOURCE_HUMAN,
                provenance=(
                    f"{_cell(raw, 'review_id')} | db/qw/bd={raw_models} | "
                    f"blank={_cell(raw, 'blank_models') or '-'}"
                ),
                models_agreement="two_vs_one" if len(agreeing) == 2 else "three_way",
                overturn=overturn,
                reviewer=reviewer,
                note=note,
            )
        )

    # ---------------- 三模型共识（未经人工确认） ----------------
    for raw in consensus.rows:
        stats.consensus_rows_total += 1
        field_name = _cell(raw, "field")
        value_raw = _cell(raw, "value_agreed")
        value = normalize_value(field_name, value_raw)
        coverage = stats.coverage_by_field.setdefault(
            field_name, {"human": 0, "consensus": 0, "blank": 0}
        )
        if value:
            stats.consensus_with_value += 1
            coverage["consensus"] += 1
        else:
            stats.consensus_blank += 1
            coverage["blank"] += 1
        add(
            TruthRow(
                post_id=_cell(raw, "post_id"),
                field_name=field_name,
                value=value,
                value_raw=value_raw,
                source=SOURCE_CONSENSUS,
                provenance=(
                    f"{_cell(raw, 'provenance')} | raw={_cell(raw, 'raw_values')}"
                ),
                models_agreement="unanimous_blank" if not value else "unanimous",
                overturn="",
                reviewer="",
                note=CONSENSUS_NOTE,
            )
        )

    if expected:
        stats.grid_missing = [
            f"{post_id}|{name}" for post_id, name in expected if (post_id, name) not in seen
        ]

    order = {name: index for index, name in enumerate(fields)}
    truth.sort(key=lambda item: (item.post_id, order.get(item.field_name, len(fields))))
    return truth, stats


def write_ground_truth(path: Path, rows: Sequence[TruthRow]) -> int:
    """写出金标准长表（UTF-8-SIG，Excel 友好），返回行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(GROUND_TRUTH_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_row())
    return len(rows)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "—"
    return f"{numerator / denominator * 100:.1f}%"


def _top_values(
    rows: Sequence[TruthRow], field_name: str, *, source: str = "", limit: int = 5
) -> str:
    counter = Counter(
        row.value or "∅（未给出）"
        for row in rows
        if row.field_name == field_name and (not source or row.source == source)
    )
    if not counter:
        return "—"
    return "、".join(f"{value}×{count}" for value, count in counter.most_common(limit))


def render_report(
    rows: Sequence[TruthRow],
    stats: BuildStats,
    *,
    generated_at: datetime,
    pending_path: Path,
    pending_digest: str,
    pending_format: str,
    consensus_path: Path,
    consensus_digest: str,
    out_path: Path,
    archive_path: Path,
    reference_path: Path,
    fields: Sequence[str],
) -> str:
    """生成《金标准生成报告》（Markdown）。"""
    lines: list[str] = []
    add = lines.append
    add("# 金标准生成报告（ground_truth_200）")
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(
        f"- 人工裁决输入：`{pending_path}`（sha256 前 16 位 `{pending_digest}`，"
        f"格式：{pending_format}）"
    )
    add(
        f"- 三模型共识输入：`{consensus_path}`（sha256 前 16 位 `{consensus_digest}`，格式：csv）"
    )
    add(f"- 人工裁决归档副本：`{archive_path}`")
    add(f"- 输出：`{out_path}`（{len(rows)} 行）；网格参考：`{reference_path}`")
    add("")
    add(
        "> **红线**：`source=3-model-consensus` 的条目**未经人工确认**。评估抽取器时必须**分层**："
        "人工子集看真实准确率，共识子集只能当参考（`.clinerules`：LLM 输出不得充当验收依据）。"
    )
    add("> `value` 为空串表示**人工/模型判定该字段不存在（未给出）**——这是合法金标准，"
        "评估时不要当缺失值丢弃。")
    add("")
    add("## 1. 结论摘要")
    add("")
    judged = stats.pending_adjudicated
    majority = stats.overturn_counts.get(OVERTURN_MAJORITY, 0)
    minority = stats.overturn_counts.get(OVERTURN_MINORITY, 0)
    single = stats.overturn_counts.get(OVERTURN_SINGLE, 0)
    none = stats.overturn_counts.get(OVERTURN_NONE, 0)
    add(
        f"- **人工推翻模型：{stats.overturn_total} 次 / {judged} 条人工裁决"
        f"（{_pct(stats.overturn_total, judged)}）**"
    )
    add(f"  - 采纳少数派（推翻多数派）：**{minority}** 次")
    add(f"  - 三家都错（人工给出新答案）：**{none}** 次")
    add(f"  - 未推翻：采纳多数派 {majority} 次；三家各异·采纳其一 {single} 次")
    add(
        f"- **金标准覆盖率：{stats.covered_cells} / {stats.expected_cells} = "
        f"{_pct(stats.covered_cells, stats.expected_cells)}**（格子级：样本 × 字段）"
    )
    add(
        f"  - 人工裁决：{judged} 格（{_pct(judged, stats.expected_cells)}）"
        f"——其中有具体取值 {stats.pending_with_value}、人工判定「未给出」"
        f"{stats.pending_intentional_blank}"
    )
    add(
        f"  - 三模型共识（有值）：{stats.consensus_with_value} 格"
        f"（{_pct(stats.consensus_with_value, stats.expected_cells)}）"
    )
    add(
        f"  - 三模型共识（三家都未给出）：{stats.consensus_blank} 格"
        f"（{_pct(stats.consensus_blank, stats.expected_cells)}）"
    )
    add(f"  - 未裁决（人工漏填且无理由）：{stats.pending_unadjudicated} 格")
    add(
        f"- **有具体取值的格子：{stats.value_cells} / {stats.expected_cells} = "
        f"{_pct(stats.value_cells, stats.expected_cells)}**"
    )
    if stats.grid_missing:
        add(
            f"- ⚠ 网格完整性：相对抽样清单仍缺 {len(stats.grid_missing)} 格"
            f"（示例 {stats.grid_missing[:5]}）"
        )
    else:
        add("- 网格完整性：与抽样清单一致（无缺失格子）。")
    add("")
    add("## 2. 字段级覆盖")
    add("")
    add("| 字段 | 应有格 | 人工裁决 | 模型共识（有值） | 模型共识（都未给出） | 未裁决 |")
    add("|---|---|---|---|---|---|")
    for name in fields:
        coverage = stats.coverage_by_field.get(name, {"human": 0, "consensus": 0, "blank": 0})
        expected_per_field = (
            stats.expected_cells // len(fields) if fields and stats.expected_cells else 0
        )
        missing = expected_per_field - (
            coverage["human"] + coverage["consensus"] + coverage["blank"]
        )
        add(
            f"| {FIELD_LABELS.get(name, name)}（`{name}`） | {expected_per_field} "
            f"| {coverage['human']} | {coverage['consensus']} | {coverage['blank']} "
            f"| {max(missing, 0)} |"
        )
    add("")
    add("## 3. 人工裁决 vs 模型（推翻分析）")
    add("")
    add("| 人工裁决与三模型的关系 | 条数 | 占比 | 说明 |")
    add("|---|---|---|---|")
    for label in (OVERTURN_MAJORITY, OVERTURN_MINORITY, OVERTURN_SINGLE, OVERTURN_NONE):
        add(
            f"| {OVERTURN_LABEL_CN[label]}（`{label}`） "
            f"| {stats.overturn_counts.get(label, 0)} "
            f"| {_pct(stats.overturn_counts.get(label, 0), judged)} "
            f"| {'**计入推翻**' if label in OVERTURN_LABELS else '不计入推翻'} |"
        )
    add("")
    add("| 字段 | 人工裁决数 | 推翻数 | 推翻率 | 采纳少数派 | 三家都错 |")
    add("|---|---|---|---|---|---|")
    for name in fields:
        per_field = stats.overturn_by_field.get(name, {})
        total = sum(per_field.values())
        if not total:
            continue
        overturned = per_field.get(OVERTURN_MINORITY, 0) + per_field.get(OVERTURN_NONE, 0)
        add(
            f"| {FIELD_LABELS.get(name, name)} | {total} | {overturned} "
            f"| {_pct(overturned, total)} | {per_field.get(OVERTURN_MINORITY, 0)} "
            f"| {per_field.get(OVERTURN_NONE, 0)} |"
        )
    add("")
    add("## 4. 最终金标准的取值分布（人工 vs 三模型共识）")
    add("")
    add("| 字段 | 人工裁决 Top5 | 三模型共识 Top5 |")
    add("|---|---|---|")
    for name in fields:
        add(
            f"| {FIELD_LABELS.get(name, name)} "
            f"| {_top_values(rows, name, source=SOURCE_HUMAN)} "
            f"| {_top_values(rows, name, source=SOURCE_CONSENSUS)} |"
        )
    add("")
    add("## 5. 未裁决清单（若有）")
    add("")
    if stats.pending_unadjudicated:
        add(
            f"共 **{stats.pending_unadjudicated}** 格既无人工取值、也没有留下理由说明。"
            "按红线**没有回填模型值**，因此这些格子在金标准里不存在（视为未覆盖）："
        )
        add("")
        for item in stats.pending_unadjudicated_ids:
            add(f"- `{item}`")
    else:
        add("无。（人工裁决要么给了取值，要么在 `notes` 里写明了「未给出」的理由。）")
    add("")
    add("## 6. 输出与下游用法")
    add("")
    add("| 列 | 含义 |")
    add("|---|---|")
    add("| `post_id` / `field` | 主键（样本 × 字段） |")
    add("| `value` | **归一化后的规范值**（评估用）；空串 = 该字段不存在（未给出） |")
    add("| `value_raw` | 原始文本（人工填写原样 / 模型共识原值） |")
    add("| `source` | `human-adjudicated` 或 `3-model-consensus` |")
    add("| `provenance` | 溯源：`review_id` + 三方原文 + 留空情况 / 共识 `raw_values` |")
    add("| `models_agreement` | `two_vs_one` / `three_way` / `unanimous` / `unanimous_blank` |")
    add(
        "| `overturn` | 人工与模型的关系（仅人工行）：`matches-majority` / "
        "`matches-minority` / `matches-single` / `matches-none` |"
    )
    add("| `reviewer` / `note` | 人工署名与判定理由 |")
    add("")
    add("下游用法（评估 `RegexOpinionExtractor` 时）：")
    add("")
    add("1. 只对 `source=human-adjudicated` 子集报**准确率**（唯一有验收效力的部分）；")
    add(
        "2. `source=3-model-consensus` 子集单独列出，只用于**观察**抽取器与模型的一致性"
        "（不得当验收依据）；"
    )
    add("3. `value` 为空串的格子语义是「应判为未给出」，评估时**不要**当缺失值丢弃。")
    add("")
    add("复现命令：")
    add("")
    add("```powershell")
    add("python scripts/build_ground_truth.py")
    add("```")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.build_ground_truth",
        description="合并人工裁决 + 三模型共识 → 金标准长表（只读 CSV/xlsx，不碰数据库）",
    )
    parser.add_argument("--pending", default=str(DEFAULT_PENDING), help="人工裁决表（.csv/.xlsx）")
    parser.add_argument("--consensus", default=str(DEFAULT_CONSENSUS), help="三模型共识表")
    parser.add_argument("--out", default=str(DEFAULT_GROUND_TRUTH), help="金标准输出路径")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告（Markdown）输出路径")
    parser.add_argument(
        "--archive-dir", default=str(DEFAULT_ARCHIVE_DIR), help="人工裁决归档目录（只增不改）"
    )
    parser.add_argument(
        "--reference-csv", default=str(DEFAULT_REFERENCE_CSV), help="抽样清单（验证网格完整性）"
    )
    parser.add_argument("--fields", default=",".join(DEFAULT_FIELDS), help="字段（逗号分隔）")
    parser.add_argument("--no-archive", action="store_true", help="跳过归档复制")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写任何文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """合并并导出金标准；退出码 0=完整 / 2=输入问题 / 3=存在未裁决格子（仍写出文件）。"""
    configure_stdout()
    args = _parse_args(argv)
    fields = tuple(name.strip() for name in args.fields.split(",") if name.strip())
    if not fields:
        print("[truth] --fields 不能为空", file=sys.stderr)
        return 2

    pending_path = Path(args.pending)
    consensus_path = Path(args.consensus)
    for path in (pending_path, consensus_path):
        if not path.exists():
            print(f"[truth] 输入文件不存在：{path}", file=sys.stderr)
            return 2

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    pending_format = "xlsx（ZIP 容器，文件名扩展名不代表真实格式）" if sniff_is_xlsx(
        pending_path
    ) else f"csv（编码 {read_any_table(pending_path).encoding}）"
    try:
        pending = read_any_table(pending_path)
        consensus = read_any_table(consensus_path)
    except (LookupError, ValueError, OSError) as exc:
        print(f"[truth] 读取失败：{exc}", file=sys.stderr)
        return 2
    if not pending.rows or not consensus.rows:
        print("[truth] 输入为空（人工裁决表或共识表没有数据行）", file=sys.stderr)
        return 2

    expected = reference_grid(Path(args.reference_csv), fields)
    rows, stats = merge(pending, consensus, expected=expected, fields=fields)

    print(f"[truth] 人工裁决：{stats.pending_rows_total} 行 → 已裁决 {stats.pending_adjudicated}"
          f"（有值 {stats.pending_with_value} / 判定未给出 {stats.pending_intentional_blank}）"
          f" + 未裁决 {stats.pending_unadjudicated}")
    print(
        f"[truth] 三模型共识：{stats.consensus_rows_total} 行"
        f"（有值 {stats.consensus_with_value} / 三家都未给出 {stats.consensus_blank}）"
    )
    kept = stats.overturn_counts.get(OVERTURN_MAJORITY, 0) + stats.overturn_counts.get(
        OVERTURN_SINGLE, 0
    )
    print(
        f"[truth] 人工推翻模型：{stats.overturn_total} 次"
        f"（采纳少数派 {stats.overturn_counts.get(OVERTURN_MINORITY, 0)} / "
        f"三家都错 {stats.overturn_counts.get(OVERTURN_NONE, 0)}）；未推翻 {kept} 次"
    )
    print(
        f"[truth] 金标准覆盖率：{stats.covered_cells}/{stats.expected_cells} = "
        f"{_pct(stats.covered_cells, stats.expected_cells)}"
        f"（其中有具体取值 {stats.value_cells}）"
    )
    if stats.grid_missing:
        print(
            f"[truth] 网格完整性：仍缺 {len(stats.grid_missing)} 格"
            f"（示例 {stats.grid_missing[:3]}）"
        )
    for problem in stats.problems:
        print(f"[truth][问题] {problem}")

    if args.dry_run:
        print("[truth] --dry-run：未写任何文件")
        return 0 if not stats.pending_unadjudicated else 3

    archive_path = (
        pending_path
        if args.no_archive
        else archive_input(pending_path, Path(args.archive_dir), stamp=stamp)
    )
    out_path = Path(args.out)
    written = write_ground_truth(out_path, rows)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            rows,
            stats,
            generated_at=datetime.now(UTC),
            pending_path=pending_path,
            pending_digest=file_digest(pending_path),
            pending_format=pending_format,
            consensus_path=consensus_path,
            consensus_digest=file_digest(consensus_path),
            out_path=out_path,
            archive_path=archive_path,
            reference_path=Path(args.reference_csv),
            fields=fields,
        ),
        encoding="utf-8",
    )
    print(f"[truth] 金标准：{out_path}（{written} 行）")
    print(f"[truth] 报告：{report_path}")
    if not args.no_archive:
        print(f"[truth] 人工裁决已归档：{archive_path}")
    if stats.pending_unadjudicated:
        print(
            f"[truth][提醒] 仍有 {stats.pending_unadjudicated} 格未裁决（未回填模型值）："
            "见报告第 5 节；这些格子金标准里没有",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
