"""Phase 2 基线评估：用人工金标准评估观点抽取器（只读、不碰数据库）。

对应文档：
- `docs/08 §5` Phase 2 验收：Stance 准确率 ≥ 90%、Horizon ≥ 85%、Entry/SL/TP ≥ 90%、
  Confidence 必须落在 0~1；
- `docs/10 §7` 验收指标：拒答率（UNKNOWN）/ 无观点率 / parser_failure 率；
- `logs/ground_truth_200.csv`（人工裁决 + 三模型共识，`source` 可分层）。

**严格区分两类失败**（这是本脚本的核心价值，也是 `docs/08` 的要求）：
- **该判未判（missed）**：金标准有值，抽取器什么都没给（NULL / 无观点）；
- **提取错误（wrong_value）**：抽取器给了值，但与金标准不同；
- 另外单独统计 **不该判却判（spurious）**：金标准是"未给出"，抽取器却给了值（假阳性）；
- 以及 `correct_absent`：双方都判"未给出"（真负类，属于正确）。

**分层原则**（`.clinerules`：模型输出不是金标准）：
- `source=human-adjudicated` 子集 = **唯一有验收效力**的口径（报告里作为判定依据）；
- `source=3-model-consensus` 子集只作参考，不用于 PASS/FAIL。

**入口约定**：抽取器通过 `OpinionExtractor` 协议注入（`--extractor regex|llm`），
因此评估逻辑可被单元测试用假抽取器完全覆盖，换 LLM 抽取器只需换实现。

**两种抽取器分开落地**（避免 LLM 试点覆盖掉正则基线结论）：
- `--extractor regex`（默认）→ `logs/extractor_eval.csv` + 《Phase 2 基线评估报告.md》；
- `--extractor llm` → `logs/extractor_eval_llm.csv` + 《Phase 2 LLM 试点报告.md》，
  并额外打印/落盘 **Token 与费用估算**（数据来自 API 的 `usage`，非估算出来的调用次数）。

用法::

    python scripts/evaluate_extractor.py
    python scripts/evaluate_extractor.py --extractor llm --limit 20 --sample stratified
    python scripts/evaluate_extractor.py --gold logs/ground_truth_200.csv --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种运行方式（同 `scripts/build_ground_truth.py`）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 已安装（editable）时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
import argparse
import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from scripts._console import configure_stdout
from scripts.build_ground_truth import SOURCE_CONSENSUS, SOURCE_HUMAN
from scripts.compare_model_annotations import file_digest, normalize_value, read_any_table
from src.processors.llm_client import DEFAULT_MAX_API_CALLS as LLM_DEFAULT_MAX_API_CALLS
from src.processors.llm_client import DEFAULT_MODEL as LLM_DEFAULT_MODEL
from src.processors.llm_client import LLMError
from src.processors.llm_extractor import (
    DEFAULT_CACHE_DIR,
    LLMOpinionExtractor,
)
from src.processors.llm_extractor import (
    DEFAULT_PARSER_VERSION as LLM_PARSER_VERSION,
)
from src.processors.opinion_extractor import OpinionExtractionResult
from src.processors.regex_extractor import RegexOpinionExtractor

DEFAULT_GOLD: Final[Path] = REPO_ROOT / "logs" / "ground_truth_200.csv"
#: 帖子正文来源（抽样清单：200 条样本的 text_content / has_media）
DEFAULT_TEXTS: Final[Path] = REPO_ROOT / "logs" / "annotation_sample.csv"
#: 逐格明细输出：两种抽取器**分开落盘**，互不覆盖
DEFAULT_EVAL_OUT_REGEX: Final[Path] = REPO_ROOT / "logs" / "extractor_eval.csv"
DEFAULT_EVAL_OUT_LLM: Final[Path] = REPO_ROOT / "logs" / "extractor_eval_llm.csv"
#: 报告输出（同样分开）：正则 = 基线报告；LLM = 试点/对比报告
DEFAULT_REPORT_REGEX: Final[Path] = (
    REPO_ROOT / "docs" / "experiments" / "Phase2_基线评估报告.md"
)
DEFAULT_REPORT_LLM: Final[Path] = REPO_ROOT / "docs" / "experiments" / "Phase2_LLM试点报告.md"
#: 兼容旧引用（= 正则口径的默认路径）
DEFAULT_EVAL_OUT: Final[Path] = DEFAULT_EVAL_OUT_REGEX
DEFAULT_REPORT: Final[Path] = DEFAULT_REPORT_REGEX
#: 按抽取器解析默认输出路径（`--eval-out` / `--report` 显式给出时以显式为准）
DEFAULTS_BY_EXTRACTOR: Final[dict[str, tuple[Path, Path]]] = {
    "regex": (DEFAULT_EVAL_OUT_REGEX, DEFAULT_REPORT_REGEX),
    "llm": (DEFAULT_EVAL_OUT_LLM, DEFAULT_REPORT_LLM),
}
EXTRACTORS: Final[tuple[str, ...]] = ("regex", "llm")
EXTRACTOR_LABEL: Final[dict[str, str]] = {
    "regex": "RegexOpinionExtractor（纯规则基线）",
    "llm": "LLMOpinionExtractor（DeepSeek deepseek-flash）",
}
#: 抽样模式：`head` = 按 post_id 正序前 N 条（最朴素、完全确定）；
#: `stratified` = 按文本线索分桶轮询（保证条件句/引用/弱暗示都进得来）。
SAMPLE_MODES: Final[tuple[str, ...]] = ("head", "stratified")
#: 线索分桶（顺序即轮询顺序）：用于 `--sample stratified` 与提交给人工复核时的分类
#: 注意：这里用的是**子串匹配**，所以线索词必须足够具体——
#: 例如不能只写 `据`（会命中「数据」），也不能只写 `称`（会命中「名称」）。
CUE_RULES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("conditional", ("如果", "若", "一旦", "假如", "除非", "倘若")),
    (
        "quote",
        (
            "机构认为",
            "机构表示",
            "据悉",
            "据报道",
            "据消息",
            "消息人士",
            "分析师",
            "引用",
            "援引",
        ),
    ),
    ("weak", ("不排除", "或许", "也许", "可能", "倾向", "大概", "有待", "暂时看")),
    ("posthoc", ("昨天", "昨日", "上周", "已止盈", "复盘", "回顾", "此前", "已经")),
    ("media", ("看图", "见图", "图表", "图上", "如图")),
    # 多目标（第一目标/第二目标）单独成桶：取点是 §4.9 §4.4 明确的易错点，试点必须覆盖
    ("multitarget", ("第一目标", "第二目标", "首个目标", "次级目标")),
    ("price", ("止损", "目标", "入场", "支撑", "阻力", "压力位")),
)

#: 有金标准、可算准确率的核心字段
CORE_FIELDS: Final[tuple[str, ...]] = ("stance", "horizon", "stop_loss", "take_profit")
#: 有金标准但不是 docs/08 §5 的硬指标字段
SIDE_FIELDS: Final[tuple[str, ...]] = ("information_type",)
#: 金标准里**没有**的字段 → 只能报"抽取覆盖率"
COVERAGE_ONLY_FIELDS: Final[tuple[str, ...]] = ("entry_low", "entry_high")
EVAL_FIELDS: Final[tuple[str, ...]] = CORE_FIELDS + SIDE_FIELDS
#: `docs/08 §5` 的门槛（判定依据用人工子集）
THRESHOLDS: Final[dict[str, float]] = {
    "stance": 0.90,
    "horizon": 0.85,
    "stop_loss": 0.90,
    "take_profit": 0.90,
}
FIELD_LABEL_CN: Final[dict[str, str]] = {
    "stance": "方向",
    "horizon": "周期",
    "stop_loss": "止损",
    "take_profit": "目标位",
    "information_type": "信息类型",
    "entry_low": "入场下限",
    "entry_high": "入场上限",
}
#: 单格结果（5 类，互斥且穷尽）
OUTCOME_CORRECT_VALUE: Final[str] = "correct_value"  # 命中
OUTCOME_CORRECT_ABSENT: Final[str] = "correct_absent"  # 双方都判"未给出"（真负类）
OUTCOME_MISSED: Final[str] = "missed"  # **该判未判**
OUTCOME_WRONG: Final[str] = "wrong_value"  # **提取错误**
OUTCOME_SPURIOUS: Final[str] = "spurious"  # **不该判却判**（假阳性）
OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_CORRECT_VALUE,
    OUTCOME_CORRECT_ABSENT,
    OUTCOME_MISSED,
    OUTCOME_WRONG,
    OUTCOME_SPURIOUS,
)
OUTCOME_LABEL_CN: Final[dict[str, str]] = {
    OUTCOME_CORRECT_VALUE: "命中",
    OUTCOME_CORRECT_ABSENT: "双方都判未给出",
    OUTCOME_MISSED: "该判未判（漏判）",
    OUTCOME_WRONG: "提取错误（值不符）",
    OUTCOME_SPURIOUS: "不该判却判（假阳性）",
}
#: 无观点标记（报告里显示用；内部一律用空串表示"未给出 / 无观点"）
NO_OPINION_LABEL: Final[str] = "∅（未给出）"
EVAL_COLUMNS: Final[tuple[str, ...]] = (
    "post_id",
    "field",
    "gold_value",
    "gold_source",
    "gold_raw",
    "pred_value",
    "pred_raw",
    "outcome",
    "note",
)
#: 价格字段的容差分析区间（|gold - pred| 落在哪一档）
PRICE_BANDS: Final[tuple[tuple[str, Decimal], ...]] = (
    ("精确相等", Decimal("0")),
    ("≤2", Decimal("2")),
    ("≤5", Decimal("5")),
    ("≤10", Decimal("10")),
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
class ExtractorProtocol(Protocol):
    """与 `src.processors.opinion_extractor.OpinionExtractor` 相同的协议（便于注入假实现）。"""

    parser_version: str

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult: ...


@dataclass(frozen=True, slots=True)
class GoldCell:
    """金标准的一格（`post_id` × `field`）。"""

    post_id: str
    field_name: str
    value: str
    source: str
    raw: str


@dataclass(frozen=True, slots=True)
class PostText:
    post_id: str
    text: str
    has_media: bool


@dataclass(frozen=True, slots=True)
class CellResult:
    """一格评估结果（含金标准来源，便于分层统计）。"""

    post_id: str
    field_name: str
    gold_value: str
    gold_source: str
    gold_raw: str
    pred_value: str
    pred_raw: str
    outcome: str
    note: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "field": self.field_name,
            "gold_value": self.gold_value,
            "gold_source": self.gold_source,
            "gold_raw": self.gold_raw,
            "pred_value": self.pred_value,
            "pred_raw": self.pred_raw,
            "outcome": self.outcome,
            "note": self.note,
        }


@dataclass(slots=True)
class FieldMetrics:
    """单字段指标（分母口径全部写清，避免"准确率"含义漂移）。"""

    field_name: str
    cells: int = 0
    gold_present: int = 0
    gold_absent: int = 0
    correct_value: int = 0
    correct_absent: int = 0
    missed: int = 0
    wrong_value: int = 0
    spurious: int = 0
    #: 其中"金标准 = UNKNOWN，抽取器却判无观点"的子计数（解释漏判来源）
    unknown_vs_no_opinion: int = 0

    @property
    def accuracy_on_gold_present(self) -> float:
        """`docs/08 §5` 口径：**金标准有值的样本里，抽对的比例**。"""
        return self.correct_value / self.gold_present if self.gold_present else 0.0

    @property
    def recall(self) -> float:
        """召回率：该判的（金标准有值）里抽对了多少（漏判会拉低它）。"""
        return self.accuracy_on_gold_present

    @property
    def precision(self) -> float:
        """精确率：抽取器给出的值里有多少是对的（多判会拉低它）。"""
        given = self.correct_value + self.wrong_value + self.spurious
        return self.correct_value / given if given else 0.0

    @property
    def exact_accuracy(self) -> float:
        """严格全对率：(命中 + 双方都判未给出) / 全部格子。"""
        return (self.correct_value + self.correct_absent) / self.cells if self.cells else 0.0

    @property
    def missed_rate(self) -> float:
        """**该判未判率**（分母：金标准有值）。"""
        return self.missed / self.gold_present if self.gold_present else 0.0

    @property
    def wrong_rate(self) -> float:
        """**提取错误率**（分母：金标准有值）。"""
        return self.wrong_value / self.gold_present if self.gold_present else 0.0

    @property
    def spurious_rate(self) -> float:
        """**不该判却判率**（分母：金标准为"未给出"）。"""
        return self.spurious / self.gold_absent if self.gold_absent else 0.0


@dataclass(slots=True)
class EvalStats:
    """整批评估结果。"""

    parser_version: str = ""
    cells: list[CellResult] = field(default_factory=list)
    posts_total: int = 0
    posts_with_opinion: int = 0
    posts_no_opinion: int = 0
    posts_multi_draft: int = 0
    drafts_total: int = 0
    drafts_unknown_stance: int = 0
    confidence_missing: int = 0
    confidence_invalid: int = 0
    diagnostics: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)
    price_bands: dict[str, Counter[str]] = field(default_factory=dict)
    coverage_only: dict[str, int] = field(default_factory=dict)
    missing_texts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def no_opinion_rate(self) -> float:
        return self.posts_no_opinion / self.posts_total if self.posts_total else 0.0

    @property
    def unknown_stance_rate(self) -> float:
        return self.drafts_unknown_stance / self.drafts_total if self.drafts_total else 0.0


def field_metrics(
    cells: Sequence[CellResult], fields: Sequence[str], *, source: str | None = None
) -> dict[str, FieldMetrics]:
    """按字段汇总指标；`source` 可只统计某一层（`human-adjudicated` / `3-model-consensus`）。"""
    metrics = {name: FieldMetrics(field_name=name) for name in fields}
    for cell in cells:
        target = metrics.get(cell.field_name)
        if target is None:
            continue
        if source is not None and cell.gold_source != source:
            continue
        target.cells += 1
        if cell.gold_value:
            target.gold_present += 1
        else:
            target.gold_absent += 1
        if cell.outcome == OUTCOME_CORRECT_VALUE:
            target.correct_value += 1
        elif cell.outcome == OUTCOME_CORRECT_ABSENT:
            target.correct_absent += 1
        elif cell.outcome == OUTCOME_MISSED:
            target.missed += 1
            if cell.gold_value == "UNKNOWN" and not cell.pred_value:
                target.unknown_vs_no_opinion += 1
        elif cell.outcome == OUTCOME_WRONG:
            target.wrong_value += 1
        else:
            target.spurious += 1
    return metrics


def aggregate(metrics: Mapping[str, FieldMetrics], *, label: str) -> FieldMetrics:
    """把多字段合并成一个"合计"指标（用于一行摘要）。"""
    total = FieldMetrics(field_name=label)
    for item in metrics.values():
        total.cells += item.cells
        total.gold_present += item.gold_present
        total.gold_absent += item.gold_absent
        total.correct_value += item.correct_value
        total.correct_absent += item.correct_absent
        total.missed += item.missed
        total.wrong_value += item.wrong_value
        total.spurious += item.spurious
        total.unknown_vs_no_opinion += item.unknown_vs_no_opinion
    return total


# ---------------------------------------------------------------------------
# 读入
# ---------------------------------------------------------------------------
def load_gold(
    path: Path, fields: Sequence[str] = EVAL_FIELDS
) -> tuple[dict[str, dict[str, GoldCell]], list[str]]:
    """读金标准长表 → ``{post_id: {field: GoldCell}}`` + 问题清单。"""
    table = read_any_table(path)
    wanted = set(fields)
    gold: dict[str, dict[str, GoldCell]] = {}
    problems: list[str] = []
    for row in table.rows:
        post_id = (row.get("post_id") or "").strip()
        name = (row.get("field") or "").strip()
        if not post_id:
            problems.append("金标准里存在 post_id 为空的行")
            continue
        if name not in wanted:
            continue  # 金标准里没有的字段（如 entry_low/high）自然跳过
        slot = gold.setdefault(post_id, {})
        if name in slot:
            problems.append(f"金标准重复格子：{post_id}|{name}")
            continue
        slot[name] = GoldCell(
            post_id=post_id,
            field_name=name,
            value=(row.get("value") or "").strip(),
            source=(row.get("source") or "").strip(),
            raw=(row.get("value_raw") or "").strip(),
        )
    return gold, problems


def load_texts(path: Path) -> dict[str, PostText]:
    """读帖子正文（`post_id` / `text_content` / `has_media`）。"""
    table = read_any_table(path)
    texts: dict[str, PostText] = {}
    for row in table.rows:
        post_id = (row.get("post_id") or "").strip()
        if not post_id:
            continue
        texts[post_id] = PostText(
            post_id=post_id,
            text=(row.get("text_content") or "").strip(),
            has_media=(row.get("has_media") or "").strip().lower() in {"1", "true", "yes", "y"},
        )
    return texts


# ---------------------------------------------------------------------------
# 预测与判定
# ---------------------------------------------------------------------------
def predict_cell(result: OpinionExtractionResult, field_name: str) -> tuple[str, str]:
    """把抽取结果映射成该字段的 ``(归一化预测值, 原始预测值)``。

    空串统一表示"抽取器未给出该字段"（含 `drafts` 为空的"无观点"）。
    **多观点帖取第一条 draft**（输出顺序确定，见 `docs/10 §5`）；
    多 draft 的比例会在报告里单独统计，避免口径含糊。
    """
    if not result.drafts:
        return "", ""
    draft = result.drafts[0]
    if field_name == "stance":
        raw = draft.stance.value
    elif field_name == "horizon":
        raw = draft.horizon.value if draft.horizon is not None else ""
    elif field_name == "information_type":
        raw = draft.information_type.value if draft.information_type is not None else ""
    elif field_name in {"stop_loss", "take_profit", "entry_low", "entry_high"}:
        value = getattr(draft, field_name, None)
        raw = "" if value is None else str(value)
    else:
        raise KeyError(f"未知字段：{field_name}")
    return normalize_value(field_name, raw), raw


def classify_outcome(gold_value: str, pred_value: str) -> str:
    """五类互斥判定（**该判未判** 与 **提取错误** 必须分开）。"""
    if gold_value and pred_value:
        return OUTCOME_CORRECT_VALUE if gold_value == pred_value else OUTCOME_WRONG
    if gold_value and not pred_value:
        return OUTCOME_MISSED
    if pred_value and not gold_value:
        return OUTCOME_SPURIOUS
    return OUTCOME_CORRECT_ABSENT


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def _price(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (ArithmeticError, ValueError):
        return None


def evaluate(
    gold: Mapping[str, Mapping[str, GoldCell]],
    texts: Mapping[str, PostText],
    extractor: ExtractorProtocol,
    *,
    fields: Sequence[str] = EVAL_FIELDS,
    coverage_fields: Sequence[str] = COVERAGE_ONLY_FIELDS,
) -> EvalStats:
    """跑抽取器并逐格比对（确定性：按 `post_id` 排序处理）。

    多观点帖取第一条 draft（`predict_cell`），多 draft 比例单独统计。
    """
    stats = EvalStats(parser_version=getattr(extractor, "parser_version", ""))
    stats.price_bands = {name: Counter() for name in ("stop_loss", "take_profit")}
    stats.coverage_only = {name: 0 for name in coverage_fields}

    for post_id in sorted(gold):
        post = texts.get(post_id)
        if post is None:
            stats.missing_texts.append(post_id)
            continue
        result = extractor.extract(post.text, has_media=post.has_media)
        stats.posts_total += 1
        if result.drafts:
            stats.posts_with_opinion += 1
        else:
            stats.posts_no_opinion += 1
        if len(result.drafts) > 1:
            stats.posts_multi_draft += 1
        stats.drafts_total += len(result.drafts)
        stats.drafts_unknown_stance += result.unknown_stance_count
        for draft in result.drafts:
            if draft.confidence is None:
                stats.confidence_missing += 1
            elif not (Decimal("0") <= draft.confidence <= Decimal("1")):
                stats.confidence_invalid += 1
        for diagnostic in result.diagnostics:
            stats.diagnostics[diagnostic.code.value] += 1
        for warning in result.warnings:
            stats.warnings[warning.split("：", 1)[0][:60]] += 1
        for name in coverage_fields:
            value, _raw = predict_cell(result, name)
            if value:
                stats.coverage_only[name] += 1

        for name in fields:
            cell = gold[post_id].get(name)
            if cell is None:
                continue
            pred_value, pred_raw = predict_cell(result, name)
            outcome = classify_outcome(cell.value, pred_value)
            note = ""
            if outcome == OUTCOME_MISSED and not result.drafts:
                note = "抽取器判无观点（drafts 为空）"
            elif outcome == OUTCOME_WRONG and name in stats.price_bands:
                gold_price, pred_price = _price(cell.value), _price(pred_value)
                if gold_price is not None and pred_price is not None:
                    diff = abs(gold_price - pred_price)
                    band = next(
                        (label for label, limit in PRICE_BANDS if diff <= limit), ">10"
                    )
                    stats.price_bands[name][band] += 1
                    note = f"差值 {diff}"
            stats.cells.append(
                CellResult(
                    post_id=post_id,
                    field_name=name,
                    gold_value=cell.value,
                    gold_source=cell.source,
                    gold_raw=cell.raw,
                    pred_value=pred_value,
                    pred_raw=pred_raw,
                    outcome=outcome,
                    note=note,
                )
            )
    return stats


#: 混淆矩阵的规范类别顺序（其余类别按字母序追加，"未给出"排最后）
CANONICAL_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "stance": ("LONG", "SHORT", "FLAT", "UNKNOWN"),
    "horizon": ("15M", "30M", "1H", "4H", "1D", "UNKNOWN"),
    "information_type": (
        "MACRO",
        "TECHNICAL",
        "NEWS",
        "SENTIMENT",
        "POSITIONING",
        "OTHER",
        "UNKNOWN",
    ),
}


def label_order(field_name: str, seen: Sequence[str]) -> tuple[str, ...]:
    """混淆矩阵的行列标签顺序：规范序 → 其余按字母序 → `∅（未给出）` 收尾。"""
    canonical = [label for label in CANONICAL_ORDER.get(field_name, ()) if label in set(seen)]
    extras = sorted(
        {
            label
            for label in seen
            if label and label != NO_OPINION_LABEL and label not in canonical
        }
    )
    return (*canonical, *extras, NO_OPINION_LABEL)


def confusion_matrix(
    cells: Sequence[CellResult], field_name: str, *, source: str | None = None
) -> tuple[tuple[str, ...], list[list[int]]]:
    """混淆矩阵（行 = 金标准，列 = 抽取器；`∅（未给出）` 表示未给出/无观点）。"""

    def label(value: str) -> str:
        return value or NO_OPINION_LABEL

    selected = [
        cell
        for cell in cells
        if cell.field_name == field_name and (source is None or cell.gold_source == source)
    ]
    seen = [label(cell.gold_value) for cell in selected] + [
        label(cell.pred_value) for cell in selected
    ]
    order = label_order(field_name, seen)
    index = {name: position for position, name in enumerate(order)}
    matrix = [[0] * len(order) for _ in order]
    for cell in selected:
        matrix[index[label(cell.gold_value)]][index[label(cell.pred_value)]] += 1
    return order, matrix


def write_eval_csv(path: Path, stats: EvalStats) -> int:
    """逐格明细（1000 行）——便于人工复核"错在哪一格"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EVAL_COLUMNS))
        writer.writeheader()
        for cell in stats.cells:
            writer.writerow(cell.to_row())
    return len(stats.cells)


# ---------------------------------------------------------------------------
# 抽样与抽取器构建（CLI 用；纯函数 → 单测可覆盖）
# ---------------------------------------------------------------------------
def text_cues(text: str, *, has_media: bool = False) -> tuple[str, ...]:
    """返回文本命中的**线索桶**（可多个；一个都没命中则 `("plain",)`）。"""
    matched = [name for name, needles in CUE_RULES if any(needle in text for needle in needles)]
    if has_media:
        matched.append("has_media")
    return tuple(matched) or ("plain",)


def _stratified(
    ordered: Sequence[str], texts: Mapping[str, PostText], limit: int
) -> list[str]:
    """按线索桶轮询取样：**保证每个桶都进来**（条件句/引用/弱暗示等对抗样本不会被前 N 条挤掉）。"""
    order = [name for name, _ in CUE_RULES] + ["plain"]
    buckets: dict[str, list[str]] = {name: [] for name in order}
    for post_id in ordered:
        post = texts.get(post_id)
        first = (
            text_cues(post.text, has_media=post.has_media)[0] if post is not None else "plain"
        )
        buckets.setdefault(first, []).append(post_id)
    chosen: list[str] = []
    while len(chosen) < limit and any(buckets[name] for name in order):
        for name in order:
            if len(chosen) >= limit:
                break
            if buckets[name]:
                chosen.append(buckets[name].pop(0))
    return sorted(chosen)


def select_posts(
    gold: Mapping[str, Mapping[str, GoldCell]],
    texts: Mapping[str, PostText],
    *,
    limit: int = 0,
    mode: str = "head",
) -> tuple[list[str], dict[str, str]]:
    """挑选本轮要评估的帖子（**只看金标准里有格子的帖子**，保证口径可比）。

    Args:
        limit: `<= 0` 或大于总数 → 全选。
        mode: `head`（按 `post_id` 正序，完全确定）/ `stratified`（线索分桶轮询）。

    Returns:
        `(post_id 列表, {post_id: 线索分类串})`；分类串用于报告与人工复核时快速定位样本类型。
    """
    if mode not in SAMPLE_MODES:
        raise ValueError(f"未知抽样模式：{mode!r}（可选 {SAMPLE_MODES}）")
    ordered = sorted(gold)  # 确定性：与 evaluate() 的处理顺序一致
    if limit <= 0 or limit >= len(ordered):
        chosen = list(ordered)
    elif mode == "head":
        chosen = ordered[:limit]
    else:
        chosen = _stratified(ordered, texts, limit)
    tags: dict[str, str] = {}
    for post_id in chosen:
        post = texts.get(post_id)
        cues = text_cues(post.text, has_media=post.has_media) if post else ("missing-text",)
        tags[post_id] = "+".join(cues)
    return chosen, tags


def build_extractor(args: argparse.Namespace) -> ExtractorProtocol:
    """按 `--extractor` 构造抽取器（LLM 相关参数只在 `llm` 模式下生效）。"""
    if args.extractor == "regex":
        if args.parser_version:
            return RegexOpinionExtractor(parser_version=args.parser_version)
        return RegexOpinionExtractor()
    return LLMOpinionExtractor(
        cache_dir=Path(args.llm_cache_dir),
        cache_mode=args.llm_cache_mode,
        model=args.llm_model,
        parser_version=args.parser_version or LLM_PARSER_VERSION,
        max_api_calls=args.llm_max_api_calls,
        base_url=args.llm_base_url or None,
    )


def _cue_summary(tags: Mapping[str, str]) -> str:
    """线索分布摘要（如 `conditional 4、quote 3、has_media 12`）——用于确认抽样是否覆盖对抗样本。"""
    if not tags:
        return "—"
    counter: Counter[str] = Counter()
    for value in tags.values():
        for cue in value.split("+"):
            counter[cue] += 1
    return "、".join(f"{name} {count}" for name, count in counter.most_common())


def _sample_note(args: argparse.Namespace, *, selected_count: int, tags: Mapping[str, str]) -> str:
    """报告头部的一行样本说明（写清"这批数到底跑了多少条、怎么挑的"）。"""
    if args.limit <= 0:
        return f"全部帖子（{selected_count} 条，`--limit 0`）"
    return (
        f"`--limit {args.limit} --sample {args.sample}` → 实选 **{selected_count} 条**；"
        f"线索分布：{_cue_summary(tags)}"
    )


def _repro_command(args: argparse.Namespace) -> str:
    """报告里的复现命令（含零成本复跑；`--api-key` 这类参数**不存在**，密钥只走 .env）。"""
    parts = ["python scripts/evaluate_extractor.py", f"--extractor {args.extractor}"]
    if args.limit > 0:
        parts.append(f"--limit {args.limit} --sample {args.sample}")
    if args.extractor == "llm":
        parts.append(f"--llm-model {args.llm_model}")
        parts.append(f"--llm-max-api-calls {args.llm_max_api_calls}")
    command = " ".join(parts)
    if args.extractor == "llm":
        return (
            f"{command} --llm-cache-mode auto    # 首次：真实调用（写完缓存）\n"
            f"{command} --llm-cache-mode readonly --dry-run    # 复跑：零网络零费用"
        )
    return command


def _usage_lines(usage: Mapping[str, Any], *, posts: int) -> list[str]:
    """把抽取器的 token/费用台账渲染成可读行（**LLM 模式专用**）。

    Args:
        usage: `LLMOpinionExtractor.stats()["usage"]` 那份子映射
            （含 `api` / `cached` 两本台账与费用估算）。
        posts: 本轮实际处理的帖子数（用于说明"多少条是本次新调用"）。
    """
    api = usage.get("api") or {}
    cached = usage.get("cached") or {}
    api_posts = int(api.get("posts", 0) or 0)
    cached_posts = int(cached.get("posts", 0) or 0)
    api_input = int(api.get("prompt_tokens", 0) or 0)
    api_output = int(api.get("completion_tokens", 0) or 0)
    api_hit = int(api.get("cache_hit_tokens", 0) or 0)
    api_miss = int(api.get("cache_miss_tokens", 0) or 0)
    cached_input = int(cached.get("prompt_tokens", 0) or 0)
    cached_output = int(cached.get("completion_tokens", 0) or 0)

    def _cost(value: object) -> str:
        return f"${value:.4f}" if isinstance(value, (int, float)) else "未知（模型不在价目表内）"

    lines = [
        f"- 本次真实调用：{api_posts} 条（缓存复用 {cached_posts} 条 / 共 {posts} 条）",
        f"- 本次 token：输入 {api_input}"
        f"（缓存命中 {api_hit} / 未命中 {api_miss}） + 输出 {api_output}",
        f"- 本次费用估算：峰值价 {_cost(usage.get('api_estimated_cost_usd_peak'))}"
        f" / 低谷价 {_cost(usage.get('api_estimated_cost_usd_offpeak'))}",
        f"- 全量（含缓存复用）token：输入 {api_input + cached_input}"
        f" + 输出 {api_output + cached_output}；整套费用估算（峰值价） "
        f"{_cost(usage.get('full_set_estimated_cost_usd_peak'))}",
        "- 口径说明：只统计**成功响应**的 token；失败请求与重试尝试服务端也计费，"
        "但响应里拿不到 token 数，故未计入。",
    ]
    return lines


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _verdict(field_name: str, accuracy: float) -> str:
    threshold = THRESHOLDS.get(field_name)
    if threshold is None:
        return "—（无门槛）"
    return f"{'✅ PASS' if accuracy >= threshold else '❌ FAIL'}（门槛 ≥ {_pct(threshold)}）"


def _matrix_markdown(order: Sequence[str], matrix: Sequence[Sequence[int]]) -> list[str]:
    header = " | ".join(order)
    lines = [
        f"| 金标准 ↓ \\ 抽取器 → | {header} |",
        "|" + "---|" * (len(order) + 1),
    ]
    for label, row in zip(order, matrix, strict=True):
        lines.append(f"| **{label}** | " + " | ".join(str(value) for value in row) + " |")
    return lines


def _examples(
    stats: EvalStats,
    texts: Mapping[str, PostText],
    outcome: str,
    *,
    limit: int = 8,
) -> list[str]:
    lines: list[str] = []
    picked = [cell for cell in stats.cells if cell.outcome == outcome][:limit]
    for cell in picked:
        text = texts.get(cell.post_id)
        raw_text = text.text if text else ""
        excerpt = raw_text[:70] + "…" if len(raw_text) > 70 else raw_text
        lines.append(
            f"- `{cell.post_id}` / {FIELD_LABEL_CN.get(cell.field_name, cell.field_name)}："
            f"金标准 `{cell.gold_value or NO_OPINION_LABEL}` ↔ 抽取 "
                f"`{cell.pred_value or NO_OPINION_LABEL}`"
            f"{'（' + cell.note + '）' if cell.note else ''}"
        )
        lines.append(f"  - 原文：{excerpt}")
    if not picked:
        lines.append("- （无）")
    return lines


def render_report(
    stats: EvalStats,
    *,
    gold_path: Path,
    gold_digest: str,
    texts_path: Path,
    eval_out: Path,
    generated_at: datetime,
    texts: Mapping[str, PostText],
    extractor_label: str = EXTRACTOR_LABEL["regex"],
    usage: Mapping[str, Any] | None = None,
    sample_note: str | None = None,
    repro_command: str | None = None,
) -> str:
    """生成《Phase 2 基线评估报告》（`usage` 只在 LLM 模式下传入）。"""
    lines: list[str] = []
    add = lines.append
    fields = EVAL_FIELDS
    human = field_metrics(stats.cells, fields, source=SOURCE_HUMAN)
    overall = field_metrics(stats.cells, fields)
    consensus = field_metrics(stats.cells, fields, source=SOURCE_CONSENSUS)

    add(f"# Phase 2 基线评估报告 · {extractor_label}")
    add("")
    add(f"- 抽取器：`{extractor_label}`（`parser_version = {stats.parser_version}`）")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 金标准：`{gold_path}`（sha256 前 16 位 `{gold_digest}`，1000 格 = 200 样本 × 5 字段）")
    add(f"- 帖子正文来源：`{texts_path}`（{stats.posts_total} 条；缺失 {len(stats.missing_texts)} "
        "条）")
    add(f"- 逐格明细：`{eval_out}`（{len(stats.cells)} 行，可人工复核每一格）")
    if sample_note:
        add(f"- 本轮样本：{sample_note}")
    add("")
    add(
        "> **判定依据**：`source=human-adjudicated` 子集（226 格，人工裁决）"
            "是唯一有验收效力的口径；"
        "`source=3-model-consensus` 子集（774 格）只作参考，不用于 PASS/FAIL。"
    )
    add(
        "> **两类失败严格区分**：`该判未判（missed）`= 金标准有值但抽取器未给出；"
        "`提取错误（wrong_value）`= 给了值但值不符；`不该判却判（spurious）`= "
            "金标准为未给出却给了值。"
    )
    add(
        "> **语料说明**：本批是 `logs/posts.csv` 生成的**Mock 语料**（含 8 类对抗样本共 126 条，"
        "含 5 条同文转载）；数字只用于**基线定位**，不能替代真实语料验收。"
    )
    add("")
    add("## 1. 摘要（核心字段 · 人工子集口径）")
    add("")
    add("| 字段 | 金标准有值 | 命中 | **准确率** | 该判未判 | 提取错误 | 不该判却判 | 结论 |")
    add("|---|---|---|---|---|---|---|---|")
    for name in fields:
        item = human[name]
        add(
            f"| {FIELD_LABEL_CN.get(name, name)}（`{name}`） | {item.gold_present} "
            f"| {item.correct_value} | **{_pct(item.accuracy_on_gold_present)}** "
            f"| {item.missed} | {item.wrong_value} | {item.spurious} "
            f"| {_verdict(name, item.accuracy_on_gold_present)} |"
        )
    core_human = aggregate({name: human[name] for name in CORE_FIELDS}, label="核心四字段")
    add(
        f"| **核心四字段合计** | {core_human.gold_present} | {core_human.correct_value} "
        f"| **{_pct(core_human.accuracy_on_gold_present)}** | {core_human.missed} "
        f"| {core_human.wrong_value} | {core_human.spurious} | — |"
    )
    add("")
    add(
        f"- 抽取器在本批 {stats.posts_total} 条帖子上的**无观点率**："
        f"{stats.posts_no_opinion}/{stats.posts_total} = {_pct(stats.no_opinion_rate)}"
        f"（{stats.posts_with_opinion} 条给出观点）"
    )
    add(
        f"- 观点级 **UNKNOWN 方向率**：{stats.drafts_unknown_stance}/{stats.drafts_total} = "
        f"{_pct(stats.unknown_stance_rate)}（`docs/10 §7` 拒答率）"
    )
    add(
        f"- 多观点帖（drafts > 1）：{stats.posts_multi_draft} 条"
        f"（评估按**第一条 draft** 取字段，比例低时对结论影响可忽略）"
    )
    add("")
    add("## 2. 五类判定全景（全量口径）")
    add("")
    add(
        "| 字段 | 格子 | 金标准有值 | 值缺失 | 命中 | 双方未给出 | **该判未判** | "
        "**提取错误** | **不该判却判** | 精确率 | 召回率 | 严格全对率 |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in fields:
        item = overall[name]
        add(
            f"| {FIELD_LABEL_CN.get(name, name)} | {item.cells} | {item.gold_present} "
            f"| {item.gold_absent} | {item.correct_value} | {item.correct_absent} "
            f"| {item.missed} | {item.wrong_value} | {item.spurious} "
            f"| {_pct(item.precision)} | {_pct(item.recall)} | {_pct(item.exact_accuracy)} |"
        )
    add("")
    add("## 3. 分层：人工子集（验收口径）vs 模型共识子集（仅参考）")
    add("")
    add("| 字段 | 人工：有值 | 人工：准确率 | 人工：漏判/错判/多判 | 共识：有值 | 共识：准确率 |")
    add("|---|---|---|---|---|---|")
    for name in fields:
        human_item = human[name]
        consensus_item = consensus[name]
        add(
            f"| {FIELD_LABEL_CN.get(name, name)} | {human_item.gold_present} "
            f"| **{_pct(human_item.accuracy_on_gold_present)}** "
            f"| {human_item.missed} / {human_item.wrong_value} / {human_item.spurious} "
            f"| {consensus_item.gold_present} | {_pct(consensus_item.accuracy_on_gold_present)} |"
        )
    add("")
    add(
        "> 模型共识子集的准确率**不能**当作验收结论（金标准本身来自模型）；"
        "它只反映「抽取器与三模型的一致性」。"
    )
    add("")
    add("## 4. 混淆矩阵（行 = 金标准，列 = 抽取器）")
    add("")
    for name in ("stance", "horizon", "information_type"):
        for source, title in ((SOURCE_HUMAN, "人工子集"), (None, "全量")):
            order, matrix = confusion_matrix(stats.cells, name, source=source)
            add(f"### 4.{name}.{title} — {FIELD_LABEL_CN.get(name, name)}")
            add("")
            lines.extend(_matrix_markdown(order, matrix))
            add("")
    add("## 5. 点位字段的误差分布（只统计「值不符」的格子）")
    add("")
    add("| 字段 | 提取错误数 | " + " | ".join(label for label, _ in PRICE_BANDS) + " | >10 |")
    add("|---|---|" + "---|" * (len(PRICE_BANDS) + 1))
    for name in ("stop_loss", "take_profit"):
        bands = stats.price_bands.get(name, Counter())
        total = sum(bands.values())
        add(
            f"| {FIELD_LABEL_CN.get(name, name)} | {total} | "
            + " | ".join(str(bands.get(label, 0)) for label, _ in PRICE_BANDS)
            + f" | {bands.get('>10', 0)} |"
        )
    add("")
    add("> 差值 = |金标准 − 抽取值|。同一条文本里可能同时出现「第一目标 / 第二目标」，"
        "若差值集中在 ≤5，多半是**取点口径**问题（应在 `docs/10 §4.4` 钉死取哪个），"
        "而不是数字识别错误。")
    add("")
    add("## 6. 典型案例（便于定位规则缺陷）")
    add("")
    add("### 6.1 该判未判（missed）")
    add("")
    lines.extend(_examples(stats, texts, OUTCOME_MISSED))
    add("")
    add("### 6.2 提取错误（wrong_value）")
    add("")
    lines.extend(_examples(stats, texts, OUTCOME_WRONG))
    add("")
    add("### 6.3 不该判却判（spurious）")
    add("")
    lines.extend(_examples(stats, texts, OUTCOME_SPURIOUS))
    add("")
    add("## 7. 质量与覆盖指标（`docs/08 §5` / `docs/10 §7`）")
    add("")
    add("| 指标 | 数值 | 说明 |")
    add("|---|---|---|")
    add(
        f"| Confidence 合法性 | 非法 {stats.confidence_invalid} / 未给出 "
            f"{stats.confidence_missing} "
        f"/ 共 {stats.drafts_total} | `docs/08`：数值必须在 0~1（非法即 FAIL） |"
    )
    add(f"| 无观点率 | {_pct(stats.no_opinion_rate)} | `drafts` 为空的帖子占比 |")
    add(f"| UNKNOWN 方向率 | {_pct(stats.unknown_stance_rate)} | 观点级拒答率（只降不猜） |")
    add(f"| 解析失败（诊断数） | {sum(stats.diagnostics.values())} | 见下表 |")
    if stats.coverage_only:
        parts = [
            f"`{name}` {count}/{stats.posts_total}"
            for name, count in sorted(stats.coverage_only.items())
        ]
        covered = "、".join(parts)
    else:
        covered = "—"
    add(f"| 点位字段覆盖率 | {covered} | 金标准**未覆盖** entry 字段 → 只能报覆盖率，不报准确率 |")
    add("")
    add("| 诊断码 | 次数 |")
    add("|---|---|")
    for code, count in stats.diagnostics.most_common():
        add(f"| `{code}` | {count} |")
    if stats.warnings:
        add("")
        add("| 警告 | 次数 |")
        add("|---|---|")
        for warning, count in stats.warnings.most_common():
            add(f"| {warning} | {count} |")
    if usage is not None:
        add("")
        add("### 7.1 Token 与费用台账（仅 LLM 模式）")
        add("")
        lines.extend(_usage_lines(usage, posts=stats.posts_total))
    add("")
    add("## 8. 结论与下一步")
    add("")
    passed = [
        name
        for name in fields
        if name in THRESHOLDS and human[name].accuracy_on_gold_present >= THRESHOLDS[name]
    ]
    failed = [name for name in fields if name in THRESHOLDS and name not in passed]
    add(
        f"- 人工子集口径下：**PASS {len(passed)} "
            f"项**（{'、'.join(FIELD_LABEL_CN.get(n, n) for n in passed) or '无'}）；"
        f"**FAIL {len(failed)} "
            f"项**（{'、'.join(FIELD_LABEL_CN.get(n, n) for n in failed) or '无'}）"
    )
    add(
        "- 失败模式的构成（该判未判 vs 提取错误 vs 不该判却判）见第 2 节；"
        "**该判未判**通常靠补词典/正则规则解决，**提取错误**要先看第 5 节的差值分布，"
        "判断是「口径问题」还是「识别问题」。"
    )
    add(
        "- 本批语料是 Mock 生成（含 126 条对抗样本），且金标准 77.4% 来自模型共识；"
        "**真实语料上的验收必须用真实作者帖子 + 全人工裁决重新抽样**（`docs/08 §5`）。"
    )
    add(
        "- 建议下一步：①按第 6 节样例修 `regex_extractor` 的规则；②把第 5 节确认的取点口径写进 "
        "`docs/10 §4.4`；③补 entry 字段的标注口径，下一轮抽样时纳入金标准。"
    )
    add("")
    add("复现命令：")
    add("")
    add("```powershell")
    add(repro_command or "python scripts/evaluate_extractor.py")
    add("```")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.evaluate_extractor",
        description="用人工金标准评估观点抽取器（只读文件，不碰数据库）",
    )
    parser.add_argument(
        "--gold", default=str(DEFAULT_GOLD), help="金标准长表（ground_truth_200.csv）"
    )
    parser.add_argument(
        "--texts", default=str(DEFAULT_TEXTS), help="帖子正文来源（含 text_content）"
    )
    parser.add_argument(
        "--eval-out",
        default=None,
        help="逐格明细 CSV 输出路径（缺省按抽取器决定：regex→extractor_eval.csv，"
        "llm→extractor_eval_llm.csv）",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="报告（Markdown）输出路径（缺省按抽取器决定，避免 LLM 试点覆盖正则基线报告）",
    )
    parser.add_argument(
        "--parser-version", default=None, help="覆盖抽取器的 parser_version（默认用实现自带）"
    )
    parser.add_argument(
        "--extractor",
        choices=EXTRACTORS,
        default="regex",
        help="抽取器：regex=纯规则基线（默认）；llm=DeepSeek（真实调用 API，注意预算）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只评估前 N 条（0 = 全部；配合 --sample 决定取哪 N 条）",
    )
    parser.add_argument(
        "--sample",
        choices=SAMPLE_MODES,
        default="head",
        help="limit 的取样方式：head=post_id 正序；stratified=按文本线索分桶轮询"
        "（保证条件句/引用/弱暗示等对抗样本都进来）",
    )
    # ---- LLM 专用（regex 模式下忽略）----
    parser.add_argument(
        "--llm-model",
        default=LLM_DEFAULT_MODEL,
        help=f"DeepSeek 模型名（默认 {LLM_DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--llm-cache-dir", default=str(DEFAULT_CACHE_DIR), help="LLM 原始响应缓存目录"
    )
    parser.add_argument(
        "--llm-cache-mode",
        choices=("auto", "readonly", "refresh", "off"),
        default="auto",
        help="auto=命中即用/未命中调 API；readonly=只读（未命中报错，¥0 复跑）；"
        "refresh=忽略旧缓存重跑；off=不读不写",
    )
    parser.add_argument(
        "--llm-max-api-calls",
        type=int,
        default=LLM_DEFAULT_MAX_API_CALLS,
        help=f"预算门禁：最多真实请求数（含重试，默认 {LLM_DEFAULT_MAX_API_CALLS}）",
    )
    parser.add_argument(
        "--llm-base-url", default=None, help="覆盖 API 地址（默认取 .env / 官方地址）"
    )
    parser.add_argument(
        "--llm-usage-out",
        default=None,
        help="把 token/费用台账写成 JSON（缺省：llm 模式写到 <eval-out 同名>_usage.json）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写任何文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """跑评估并导出；退出码 0=成功 / 2=输入问题 / 3=有帖子正文缺失（仍写出文件）。"""
    configure_stdout()
    args = _parse_args(argv)
    gold_path = Path(args.gold)
    texts_path = Path(args.texts)
    for path in (gold_path, texts_path):
        if not path.exists():
            print(f"[eval] 输入文件不存在：{path}", file=sys.stderr)
            return 2
    try:
        gold, problems = load_gold(gold_path)
        texts = load_texts(texts_path)
    except (LookupError, ValueError, OSError) as exc:
        print(f"[eval] 读取失败：{exc}", file=sys.stderr)
        return 2
    if not gold:
        print("[eval] 金标准里没有可评估的格子（检查 --gold 与字段名）", file=sys.stderr)
        return 2

    default_eval_out, default_report = DEFAULTS_BY_EXTRACTOR[args.extractor]
    eval_out = Path(args.eval_out) if args.eval_out else default_eval_out
    report_path = Path(args.report) if args.report else default_report

    # 抽样：只评估被选中的帖子（金标准同步收窄，保证分母一致）
    try:
        selected, tags = select_posts(gold, texts, limit=args.limit, mode=args.sample)
    except ValueError as exc:
        print(f"[eval] 抽样参数错误：{exc}", file=sys.stderr)
        return 2
    if args.limit > 0:
        gold = {post_id: gold[post_id] for post_id in selected}
        print(
            f"[eval] 抽样：{args.sample} / limit={args.limit} → 实选 {len(selected)} 条"
            f"（线索分布：{_cue_summary(tags)}）"
        )

    extractor = build_extractor(args)
    try:
        stats = evaluate(gold, texts, extractor)
    except LLMError as exc:
        # 系统性故障（缺 Key / 401 / readonly 未命中）：立刻失败，不产出误导性结果
        print(f"[eval][致命] LLM 调用失败：{exc}", file=sys.stderr)
        return 2
    stats.problems = list(problems)

    human = field_metrics(stats.cells, EVAL_FIELDS, source=SOURCE_HUMAN)
    print(
        f"[eval] 抽取器 {stats.parser_version}；帖子 {stats.posts_total} 条"
        f"（无观点 {stats.posts_no_opinion}、给出观点 {stats.posts_with_opinion}、"
        f"多观点 {stats.posts_multi_draft}）"
    )
    for name in EVAL_FIELDS:
        item = human[name]
        print(
            f"[eval]   {name:<17} 人工子集：有值 {item.gold_present:>3} | "
            f"准确率 {_pct(item.accuracy_on_gold_present):>6} | "
            f"该判未判 {item.missed:>3} | 提取错误 {item.wrong_value:>3} | "
            f"不该判却判 {item.spurious:>3}"
        )
    core = aggregate({name: human[name] for name in CORE_FIELDS}, label="核心四字段")
    print(
        f"[eval] 核心四字段合计：有值 {core.gold_present} | 准确率 "
        f"{_pct(core.accuracy_on_gold_present)} | 该判未判 {core.missed} | "
        f"提取错误 {core.wrong_value} | 不该判却判 {core.spurious}"
    )
    print(
        f"[eval] 无观点率 {_pct(stats.no_opinion_rate)} | "
        f"UNKNOWN 方向率 {_pct(stats.unknown_stance_rate)} | "
        f"confidence 非法 {stats.confidence_invalid}"
    )

    usage: dict[str, Any] | None = None
    if args.extractor == "llm":
        raw_stats = getattr(extractor, "stats", None)
        if callable(raw_stats):
            stats_dict = raw_stats()
            usage = stats_dict.get("usage")
            print(
                f"[eval] LLM：模型 {stats_dict.get('model')}"
                f"（思考模式 {stats_dict.get('thinking_mode')}）| "
                f"真实调用 {stats_dict.get('posts_via_api')} 条 / 缓存复用 "
                f"{stats_dict.get('posts_via_cache')} 条 | API 调用 "
                f"{(stats_dict.get('client') or {}).get('api_calls')} 次 | "
                f"失败 {stats_dict.get('api_failures')} / 解析失败 "
                f"{stats_dict.get('parse_failures')}"
            )
            if isinstance(usage, Mapping):
                for line in _usage_lines(usage, posts=stats.posts_total):
                    print(f"[eval] {line}")
    for problem in stats.problems:
        print(f"[eval][问题] {problem}")
    if stats.missing_texts:
        print(
            f"[eval][问题] {len(stats.missing_texts)} 条金标准样本找不到正文："
            f"{stats.missing_texts[:3]}",
            file=sys.stderr,
        )

    if args.dry_run:
        print("[eval] --dry-run：未写任何文件")
        return 0

    written = write_eval_csv(eval_out, stats)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            stats,
            gold_path=gold_path,
            gold_digest=file_digest(gold_path),
            texts_path=texts_path,
            eval_out=eval_out,
            generated_at=datetime.now(UTC),
            texts=texts,
            extractor_label=EXTRACTOR_LABEL[args.extractor],
            usage=usage if isinstance(usage, Mapping) else None,
            sample_note=_sample_note(args, selected_count=len(selected), tags=tags),
            repro_command=_repro_command(args),
        ),
        encoding="utf-8",
    )
    print(f"[eval] 逐格明细：{eval_out}（{written} 行）")
    print(f"[eval] 报告：{report_path}")
    if args.extractor == "llm" and isinstance(usage, Mapping):
        usage_path = (
            Path(args.llm_usage_out)
            if args.llm_usage_out
            else eval_out.with_name(f"{eval_out.stem}_usage.json")
        )
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "extractor": EXTRACTOR_LABEL["llm"],
                    "posts_total": stats.posts_total,
                    "parser_version": stats.parser_version,
                    "usage": dict(usage),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[eval] Token/费用台账：{usage_path}")
    return 3 if stats.missing_texts else 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
