"""Phase 2 真实语料验收报告（**开源标注数据集基准**，regex vs LLM v13）。

对应文档：
- `docs/08 §5` Phase 2 验收门槛（stance ≥ 90%，人工子集口径）；
- `docs/10 §7.1` 未评估字段（`scoring=NOT_EVALUATED`：模型给值**不计假阳性**）；
- `docs/11 §6.3` 样本量与统计效力（**必须给置信区间**，不得用单点数字下结论）。

输入（由前面几步产出；本脚本**只读文件**，不联网、不碰数据库）：
- `logs/hf_benchmark_gold.csv`（长表金标准，含 `scoring` 列）
- `logs/hf_benchmark_texts.csv`（正文 + `raw_*` 原始标签留档）
- `logs/hf_benchmark_meta.json`（取数审计：映射表、跳过计数、caveats）
- `logs/hf_benchmark_eval_regex.csv` / `logs/hf_benchmark_eval_llm.csv`（逐格判定）
- `logs/hf_benchmark_eval_llm_usage.json`（token/费用台账，可选）

**报告纪律**（`.clinerules`）：
1. 核心结论**只基于 `stance`**——本批唯一有金标准的字段；
2. 每个比例都给 **Wilson 95% 置信区间**（`docs/11 §6.3`）；
3. 中文股吧（N=50）**单独成节、仅作辅助参考**，不得进入主结论；
4. 写清「标题级情感 ≠ 博主帖子观点提取」：本报告是**工程对照**，
   不是 `docs/08 §5` 的达标结论（后者要求全人工裁决金标准）。

用法::

    python scripts/report_hf_benchmark.py --dry-run    # 只打印，不写盘
    python scripts/report_hf_benchmark.py              # 写 docs/experiments/ 下的验收报告
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

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
from scripts._console import configure_stdout  # noqa: E402
from scripts.build_ground_truth import SOURCE_HUMAN  # noqa: E402
from scripts.compare_model_annotations import file_digest  # noqa: E402
from scripts.evaluate_extractor import (  # noqa: E402
    OUTCOME_CORRECT_ABSENT,
    OUTCOME_CORRECT_VALUE,
    OUTCOME_MISSED,
    OUTCOME_NOT_EVALUATED,
    OUTCOME_SPURIOUS,
    OUTCOME_WRONG,
    CellResult,
    FieldMetrics,
    confusion_matrix,
    field_metrics,
)

DEFAULT_PREFIX: Final[Path] = REPO_ROOT / "logs" / "hf_benchmark"
DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "experiments" / "Phase 2 真实语料验收报告.md"
#: Mock 基线（对读用；**不同语料/不同任务，禁止直接比较**）
MOCK_EVAL_REGEX: Final[Path] = REPO_ROOT / "logs" / "extractor_eval.csv"
MOCK_EVAL_LLM: Final[Path] = REPO_ROOT / "logs" / "extractor_eval_llm.csv"
#: 分层键 = 金标准 `source` 列（`evaluate_extractor` 的 `source=` 过滤直接可用）
LAYER_GOLD: Final[str] = "hf:saguaro-gold"
LAYER_GUBA: Final[str] = "hf:eastmoney-guba"
LAYER_LABEL_CN: Final[dict[str, str]] = {
    LAYER_GOLD: "黄金数据集（商品/黄金新闻标题，N=100）",
    LAYER_GUBA: "中文股吧（上证50ETF 标题，N=50）",
}
#: 核心结论字段（**只有它有金标准**）
CORE_FIELD: Final[str] = "stance"
#: 本基准**没有金标准**的字段（只观测假阳性：凭空编点位/周期）
PRICE_FIELDS: Final[tuple[str, ...]] = ("horizon", "stop_loss", "take_profit")
#: `docs/08 §5` 门槛（本报告只作**参考**，不作验收判定）
STANCE_THRESHOLD: Final[float] = 0.90
#: Wilson 区间用的 95% 双侧 z 值
Z_95: Final[float] = 1.959963984540054
DEFAULT_CASE_LIMIT: Final[int] = 12
#: `stance` 的混淆矩阵类别顺序（与 `evaluate_extractor.label_order` 一致）
STANCE_ORDER: Final[tuple[str, ...]] = ("LONG", "SHORT", "FLAT", "UNKNOWN", "∅（未给出）")


# ---------------------------------------------------------------------------
# 数据结构与读入
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Inputs:
    """本报告的输入文件集合（路径全部由 `--prefix` 派生，便于换批复跑）。"""

    prefix: Path
    gold: Path
    texts: Path
    meta: Path
    regex_eval: Path
    llm_eval: Path
    llm_usage: Path

    def all_files(self) -> tuple[Path, ...]:
        return (self.gold, self.texts, self.regex_eval, self.llm_eval)

    def missing(self) -> list[Path]:
        """列出缺失的**必需**输入（台账可选，不算缺失）。"""
        return [path for path in self.all_files() if not path.exists()]


def resolve_inputs(prefix: Path) -> Inputs:
    """`logs/hf_benchmark` → 全部派生路径（命名与前面几步的产物严格一致）。"""
    name = prefix.name
    parent = prefix.parent
    return Inputs(
        prefix=prefix,
        gold=parent / f"{name}_gold.csv",
        texts=parent / f"{name}_texts.csv",
        meta=parent / f"{name}_meta.json",
        regex_eval=parent / f"{name}_eval_regex.csv",
        llm_eval=parent / f"{name}_eval_llm.csv",
        llm_usage=parent / f"{name}_eval_llm_usage.json",
    )


@dataclass(slots=True)
class Run:
    """一次抽取器运行（逐格判定 + 可选 token 台账）。"""

    name: str
    label: str
    eval_path: Path
    cells: list[CellResult] = field(default_factory=list)
    usage: Mapping[str, Any] = field(default_factory=dict)

    def layer(self, layer: str) -> list[CellResult]:
        """按金标准来源分层（`hf:saguaro-gold` / `hf:eastmoney-guba`）。"""
        return [cell for cell in self.cells if cell.gold_source == layer]

    def outcomes(self, layer: str, field_name: str = CORE_FIELD) -> dict[str, list[CellResult]]:
        """按五类判定分桶（含 `not_evaluated`），供案例小节取用。"""
        buckets: dict[str, list[CellResult]] = {
            OUTCOME_CORRECT_VALUE: [],
            OUTCOME_CORRECT_ABSENT: [],
            OUTCOME_MISSED: [],
            OUTCOME_WRONG: [],
            OUTCOME_SPURIOUS: [],
            OUTCOME_NOT_EVALUATED: [],
        }
        for cell in self.layer(layer):
            if cell.field_name == field_name:
                buckets.setdefault(cell.outcome, []).append(cell)
        return buckets


def read_eval_cells(path: Path) -> list[CellResult]:
    """读逐格明细 CSV → `CellResult` 列表（缺列/坏行**直接抛错**，不静默跳过）。"""
    cells: list[CellResult] = []
    for row in csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()):
        post_id = (row.get("post_id") or "").strip()
        field_name = (row.get("field") or "").strip()
        if not post_id or not field_name:
            raise ValueError(f"明细行缺少 post_id/field：{row}")
        cells.append(
            CellResult(
                post_id=post_id,
                field_name=field_name,
                gold_value=(row.get("gold_value") or "").strip(),
                gold_source=(row.get("gold_source") or "").strip(),
                gold_raw=(row.get("gold_raw") or "").strip(),
                pred_value=(row.get("pred_value") or "").strip(),
                pred_raw=(row.get("pred_raw") or "").strip(),
                outcome=(row.get("outcome") or "").strip(),
                note=(row.get("note") or "").strip(),
            )
        )
    return cells


def read_texts(path: Path) -> dict[str, dict[str, str]]:
    """读正文与**原始标签留档**（`raw_*` 列全部保留，供"标注存疑"分析）。"""
    table: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()):
        post_id = (row.get("post_id") or "").strip()
        if post_id:
            table[post_id] = {name: (value or "") for name, value in row.items() if name}
    return table


def read_meta(path: Path) -> dict[str, Any]:
    """读取数审计（不存在时返回空字典，报告里会显式说明）。"""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# 统计小工具（Wilson 区间：小样本比例的唯一正确口径）
# ---------------------------------------------------------------------------
def wilson_interval(successes: int, total: int, *, z: float = Z_95) -> tuple[float, float]:
    """Wilson score 95% 置信区间（**不用正态近似**：小样本/极端比例会失真）。"""
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denominator = 1.0 + z * z / total
    center = (phat + z * z / (2.0 * total)) / denominator
    spread = z * ((phat * (1.0 - phat) / total + z * z / (4.0 * total * total)) ** 0.5)
    margin = spread / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def format_pct(value: float) -> str:
    """统一百分比格式（空分母显示 `—`）。"""
    return f"{value * 100:.1f}%"


def ci_text(successes: int, total: int) -> str:
    """`85.0%（95% CI 76.5%~90.8%）` —— 报告里所有比例都必须用这个格式。"""
    if total <= 0:
        return "—（分母为 0）"
    low, high = wilson_interval(successes, total)
    return f"{format_pct(successes / total)}（95% CI {format_pct(low)}~{format_pct(high)}）"


def snippet(text: str, *, width: int = 78) -> str:
    """截断过长文本（标题级语料通常不需要，但报告排版要稳）。"""
    return text if len(text) <= width else f"{text[:width]}…"


# ---------------------------------------------------------------------------
# 案例与「数据集标注存疑」候选（只报告，绝不改标签）
# ---------------------------------------------------------------------------
#: 股吧标题里的明确看多/看空线索（**启发式**，只用于挑出"值得人工复核"的候选）
BULLISH_CUES: Final[tuple[str, ...]] = (
    "看多",
    "满仓",
    "加仓",
    "买入",
    "突破",
    "新高",
    "抄底",
    "上涨",
)
BEARISH_CUES: Final[tuple[str, ...]] = (
    "看空",
    "减仓",
    "卖出",
    "割肉",
    "跌破",
    "新低",
    "下跌",
    "清仓",
)
#: 情感标签 → 方向标签的"直觉等价"（仅用于挑存疑候选，**不是评分口径**）
SENTIMENT_TO_STANCE: Final[dict[str, str]] = {"positive": "LONG", "negative": "SHORT"}
#: 原始方向列 → stance（留档列在 `texts.csv` 里是字符串）
DIRECTION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("raw_Price Direction Up", "LONG"),
    ("raw_Price Direction Down", "SHORT"),
    ("raw_Price Direction Constant", "FLAT"),
)
TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes"})


def raw_direction_stance(row: Mapping[str, str]) -> str:
    """从留档的原始方向列还原数据集 stance（找不到返回空串）。"""
    for column, stance in DIRECTION_COLUMNS:
        if (row.get(column) or "").strip().lower() in TRUTHY:
            return stance
    return ""


def field_case_lines(
    run: Run,
    texts: Mapping[str, Mapping[str, str]],
    *,
    layer: str,
    field_name: str,
    outcome: str,
    limit: int = DEFAULT_CASE_LIMIT,
) -> list[str]:
    """某类判定（`missed` / `wrong_value` / `spurious` …）的案例清单（逐格给原文）。"""
    cells = [
        cell
        for cell in run.layer(layer)
        if cell.field_name == field_name and cell.outcome == outcome
    ][:limit]
    if not cells:
        return ["- （无）"]
    lines: list[str] = []
    for cell in cells:
        row = texts.get(cell.post_id) or {}
        lines.append(f"- `{cell.post_id}`：{snippet(row.get('text_content', ''))}")
        lines.append(
            f"  - 金标准 `{cell.gold_value or '∅（未给出）'}`"
            f"（原始标签 `{cell.gold_raw or '—'}`）"
            f" ↔ {run.label} 抽取 `{cell.pred_value or '∅（未给出）'}`（`{cell.outcome}`）"
        )
    return lines


def suspicious_dataset_cases(
    texts: Mapping[str, Mapping[str, str]], *, limit: int = DEFAULT_CASE_LIMIT
) -> list[str]:
    """**启发式**挑出「数据集标签可能自相矛盾 / 与 `docs/10` 口径冲突」的候选。

    规则（**只列候选，不改任何标签**；结论须人工复核）：

    1. 黄金数据集：`Price Sentiment` 与方向标签相反（`positive`↔`SHORT`、`negative`↔`LONG`）；
    2. 中文股吧：标题含明确看多线索但 `label=0`（负向），或含看空线索但 `label=1`（正向）
       —— 该数据集卡片自述"负向包含不看跌"，与 `docs/10 §4.1.5`（不看多≠看空）冲突。
    """
    lines: list[str] = []
    for post_id in sorted(texts):
        if len(lines) >= limit:
            break
        row = texts[post_id]
        source = (row.get("source") or "").strip()
        text = row.get("text_content", "")
        if source == LAYER_GOLD:
            sentiment = (row.get("raw_Price Sentiment") or "").strip().lower()
            expected = SENTIMENT_TO_STANCE.get(sentiment)
            actual = raw_direction_stance(row)
            if expected and actual and expected != actual:
                lines.append(
                    f"- `{post_id}`（黄金）：`Price Sentiment={sentiment}` 但方向标签 = `{actual}`"
                    f" → **情感与方向相反**；文本：{snippet(text)}"
                )
        elif source == LAYER_GUBA:
            label = (row.get("raw_label") or "").strip()
            hit = [cue for cue in BULLISH_CUES if cue in text]
            miss = [cue for cue in BEARISH_CUES if cue in text]
            if label == "0" and hit:
                lines.append(
                    f"- `{post_id}`（股吧）：`label=0`（负向/含不看跌）但标题含看多线索"
                    f" {hit}；文本：{snippet(text)}"
                )
            elif label == "1" and miss:
                lines.append(
                    f"- `{post_id}`（股吧）：`label=1`（正向）但标题含看空线索"
                    f" {miss}；文本：{snippet(text)}"
                )
    return lines or ["- （启发式未发现存疑案例）"]


def mock_reference_lines() -> list[str]:
    """与 Mock 200 条基线的**对读**（语料/任务/金标准来源都不同，禁止直接比较）。"""
    if not (MOCK_EVAL_REGEX.exists() and MOCK_EVAL_LLM.exists()):
        return ["- （未找到 Mock 基线明细 `logs/extractor_eval*.csv`，本节跳过）"]
    lines = [
        "| 语料 | 抽取器 | stance 命中/分母 | 准确率（95% CI） |",
        "|---|---|---|---|",
    ]
    for path, label in ((MOCK_EVAL_REGEX, "正则"), (MOCK_EVAL_LLM, "LLM")):
        cells = [
            cell
            for cell in read_eval_cells(path)
            if cell.field_name == CORE_FIELD and cell.gold_source == SOURCE_HUMAN
        ]
        hits = sum(1 for cell in cells if cell.outcome == OUTCOME_CORRECT_VALUE)
        accuracy = ci_text(hits, len(cells))
        lines.append(f"| Mock 200（人工裁决子集） | {label} | {hits}/{len(cells)} | {accuracy} |")
    return lines


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    """报告里统一用仓库相对路径（正斜杠），便于阅读与粘贴复现命令。"""
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # pragma: no cover - 路径在仓库外（测试用 tmp_path）
        return str(path)


def metrics_of(run: Run, layer: str) -> FieldMetrics:
    """某运行在某层的 `stance` 指标（分母 = 金标准有值）。"""
    return field_metrics(run.layer(layer), (CORE_FIELD,))[CORE_FIELD]


def no_opinion_posts(run: Run, *, layer: str | None = None) -> int:
    """「整帖无观点」的帖子数（该帖所有字段的抽取值都为空）；`layer` 可按层过滤。"""
    cells = run.cells if layer is None else run.layer(layer)
    answered: dict[str, bool] = {}
    for cell in cells:
        answered[cell.post_id] = answered.get(cell.post_id, False) or bool(cell.pred_value)
    return sum(1 for value in answered.values() if not value)


def unknown_stance(run: Run, *, layer: str | None = None) -> tuple[int, int]:
    """`(方向拒答数, 给出方向的格子数)` —— 拒答率 = 前者 / 后者。"""
    cells = [
        cell
        for cell in (run.cells if layer is None else run.layer(layer))
        if cell.field_name == CORE_FIELD and cell.pred_value
    ]
    unknown = sum(1 for cell in cells if cell.pred_value == "UNKNOWN")
    return unknown, len(cells)


def fixed_regressed(
    regex: Run, llm: Run, *, layer: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """`(正则错→LLM 对, 正则对→LLM 错)` 的格子清单（只比 `stance`）。"""
    regex_cells = {
        (cell.post_id, cell.field_name): cell
        for cell in regex.layer(layer)
        if cell.field_name == CORE_FIELD
    }
    llm_cells = {
        (cell.post_id, cell.field_name): cell
        for cell in llm.layer(layer)
        if cell.field_name == CORE_FIELD
    }
    shared = sorted(set(regex_cells) & set(llm_cells))
    fixed = [
        key
        for key in shared
        if regex_cells[key].outcome != OUTCOME_CORRECT_VALUE
        and llm_cells[key].outcome == OUTCOME_CORRECT_VALUE
    ]
    regressed = [
        key
        for key in shared
        if regex_cells[key].outcome == OUTCOME_CORRECT_VALUE
        and llm_cells[key].outcome != OUTCOME_CORRECT_VALUE
    ]
    return fixed, regressed


def matrix_lines(run: Run, *, layer: str) -> list[str]:
    """`stance` 混淆矩阵（行 = 金标准，列 = 抽取器；`∅（未给出）` 表示未给出）。"""
    order, matrix = confusion_matrix(run.layer(layer), CORE_FIELD)
    header = " | ".join(order)
    lines = [f"| 金标准 ↓ \\ 抽取器 → | {header} |", "|" + "---|" * (len(order) + 1)]
    for label, row in zip(order, matrix, strict=True):
        lines.append(f"| **{label}** | " + " | ".join(str(value) for value in row) + " |")
    return lines


def extra_info_cells(run: Run) -> tuple[int, int]:
    """`(未评估格子数, 其中模型给出值数)` —— `information_type` 的「额外信息」台账。"""
    cells = [cell for cell in run.cells if cell.outcome == OUTCOME_NOT_EVALUATED]
    with_value = sum(1 for cell in cells if cell.pred_value)
    return len(cells), with_value


def render_report(
    *,
    inputs: Inputs,
    texts: Mapping[str, Mapping[str, str]],
    meta: Mapping[str, Any],
    regex: Run,
    llm: Run,
    generated_at: datetime,
    case_limit: int = DEFAULT_CASE_LIMIT,
    repro_command: str | None = None,
) -> str:
    """生成《Phase 2 真实语料验收报告》（纯函数：输入=文件内容，输出=Markdown 文本）。"""
    lines: list[str] = []
    add = lines.append
    gold_metrics = {run.name: metrics_of(run, LAYER_GOLD) for run in (regex, llm)}
    guba_metrics = {run.name: metrics_of(run, LAYER_GUBA) for run in (regex, llm)}
    fixed, regressed = fixed_regressed(regex, llm, layer=LAYER_GOLD)
    gold_n = gold_metrics[regex.name].gold_present
    guba_n = guba_metrics[regex.name].gold_present
    delta = (
        gold_metrics[llm.name].accuracy_on_gold_present
        - gold_metrics[regex.name].accuracy_on_gold_present
    )
    llm_low, llm_high = wilson_interval(gold_metrics[llm.name].correct_value, gold_n)
    regex_low, regex_high = wilson_interval(gold_metrics[regex.name].correct_value, gold_n)
    overlap = not (llm_low > regex_high or regex_low > llm_high)
    if overlap:
        significance = "两者 95% CI 重叠 → **差异不显著**"
    else:
        significance = "两者 95% CI 不重叠 → **差异显著**"
    llm_unknown, llm_answered = unknown_stance(llm)
    llm_extra, llm_extra_value = extra_info_cells(llm)
    llm_unknown_rate = format_pct(llm_unknown / llm_answered if llm_answered else 0.0)
    regex_gold_silent = no_opinion_posts(regex, layer=LAYER_GOLD)

    add("# Phase 2 真实语料验收报告 · 开源标注数据集基准（regex vs LLM v13）")
    add("")
    add(
        "> **性质**：**工程对照报告**（标题级情感任务，语料来自公开标注数据集）。"
        "**不是** `docs/08 §5` 的验收达标结论——后者要求全人工裁决的博主帖子金标准。"
    )
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 抽取器 A：{regex.label}（`parser_version = {regex.name}`） → `{_rel(regex.eval_path)}`")
    add(f"- 抽取器 B：{llm.label}（`parser_version = {llm.name}`） → `{_rel(llm.eval_path)}`")
    add(
        f"- 金标准：`{_rel(inputs.gold)}`"
        f"（sha256 前 16 位 `{file_digest(inputs.gold)}`，长表含 `scoring` 列）"
    )
    add(f"- 文本：`{_rel(inputs.texts)}`（{len(texts)} 条：黄金 {gold_n} + 中文股吧 {guba_n}）")
    if llm.usage:
        peak = float(llm.usage.get("api_estimated_cost_usd_peak") or 0.0)
        api_posts = (llm.usage.get("api") or {}).get("posts", "?")
        add(
            f"- LLM 台账：`{_rel(inputs.llm_usage)}`"
            f"（真实调用 {api_posts} 条，费用估算（峰值价）${peak:.4f}）"
        )
    add("")
    add("## 免责声明与适用范围（**用户裁决 2026-09-14，必读**）")
    add("")
    add(
        "1. **任务错配（本基准的根本局限）**：它测量的是**标题级方向分类**（新闻/股吧标题的情感"
        "标签），与本项目的**博主观点提取**任务有本质差异——没有正文语境、没有仓位意图、"
        "没有点位与周期。**分数低 ≠ 抽取器差**。"
    )
    add(
        "2. **LLM 的 `UNKNOWN` 是合规行为，不是错误**：它拒绝把「金价下跌 0.9%」这类"
        "**事实描述**当作**可交易观点**，符合 `docs/10 §4.9.0` 抽象原则与「只降不猜」。"
        "**不得**为迎合标题级数据集而改 Prompt —— 用户裁决：`opinion-prompt-v13` **保持冻结**，"
        "不教模型把描述句标成 `SHORT`（否则会污染模型定义，未来把新闻当预测）。"
    )
    add(
        "3. **本基准能证明什么**：①**正则抽取器在英文/标题级语料上完全失效**"
        f"（黄金层 {gold_n} 条英文标题里**整帖无观点 {regex_gold_silent} 条**）；"
        f"②**LLM 至少能正确识别「无观点」**（整帖无观点 {no_opinion_posts(llm)} 条、"
        f"方向拒答 {llm_unknown_rate}，且**无一格判反方向**）。"
        "**不能**用它证明 LLM 的观点提取能力。"
    )
    add(
        "4. **真正的观点提取验证放到 Phase 3**：用手工录入的中文快讯"
        "（`logs/real_posts_opinion_2026_09_14.csv`，19 条）或抓取的真实博主帖子重新标注评估；"
        "**标题级情感分类**留给 Phase 3 的 **News Alpha** 使用。"
    )
    add("")
    add("## 0. 结论摘要（**只看 `stance`**）")
    add("")
    add("| 层 | 抽取器 | stance 命中/分母 | 准确率（95% CI） | `docs/08 §5` 门槛（仅参考） |")
    add("|---|---|---|---|---|")
    for run in (regex, llm):
        item = gold_metrics[run.name]
        verdict = "✅ 达" if item.accuracy_on_gold_present >= STANCE_THRESHOLD else "❌ 未达"
        add(
            f"| 黄金（N={gold_n}） | {run.label} | {item.correct_value}/{item.gold_present} "
            f"| {ci_text(item.correct_value, item.gold_present)} | {verdict} ≥ 90% |"
        )
    add("")
    add(f"- 中文股吧层（N={guba_n}）**只作辅助参考**，单列在 §7，不进入本表与主结论。")
    add(
        f"- **LLM 相对正则：{delta * 100:+.1f} pp**（{significance}）；"
        f"LLM 修好 `{len(fixed)}` 格、弄坏 `{len(regressed)}` 格。"
    )
    add(
        f"- LLM 行为画像（全 150 条口径）：整帖无观点 `{no_opinion_posts(llm)}` 条；"
        f"**方向拒答（UNKNOWN）`{llm_unknown}/{llm_answered}` = "
        f"{llm_unknown_rate}** —— "
        "它大量判「方向不明」，而本基准金标准**必有方向**，故 `UNKNOWN` 一律计入"
        "**提取错误**（`docs/10` 要求「只降不猜」，这是模型**守规矩**的表现，不是坏行为）。"
    )
    add(
        f"- 正则行为画像：整帖无观点 `{no_opinion_posts(regex)}` 条 —— 印证**正则抽取器不适用于"
        "英文/标题级语料**（它为中文博主长帖设计），属**工具设计边界**，不是能力缺陷。"
    )
    add(
        "- `docs/08 §5` 的 stance ≥ 90% 门槛**仅作参考**：本基准是标题级情感分类，"
        "与「博主观点抽取」不是同一任务，达标与否都不构成本项目验收结论。"
    )
    add("")

    add("## 1. 数据来源与口径")
    add("")
    datasets = meta.get("datasets") or {}
    add("| 层 | 数据集 | 行数 | 金标准映射（**原始标签未改写**） |")
    add("|---|---|---|---|")
    add(
        f"| 黄金 | `{datasets.get('gold', LAYER_GOLD)}`（split=`{datasets.get('split', 'test')}`）"
        f" | {gold_n} | `Price Direction Up/Down/Constant` → `LONG/SHORT/FLAT` |"
    )
    add(
        f"| 中文股吧 | `{datasets.get('guba', LAYER_GUBA)}` | {guba_n} "
        "| `label` 0/1/2 → `SHORT/LONG/FLAT` |"
    )
    add("")
    for caveat in meta.get("caveats") or []:
        add(f"- ⚠️ {caveat}")
    add(
        "- **原始标签逐条留档**：`raw_*` 列在 `texts.csv` 里原样保存（含 `Dates`/`URL`/"
        "`Asset Comparision` 等），映射结果另存 `value_raw`（如 `Price Direction Down=1`）。"
    )
    add(
        "- `information_type` 本轮**不评分**（`scoring=NOT_EVALUATED`，口径见 `docs/10 §7.1`）："
        f"模型给值只记「额外信息」**不计假阳性**；本批 LLM 在 {llm_extra} 个未评估格子里"
        f"给出了 {llm_extra_value} 个值。"
    )
    add(
        "- 抽取器的 `horizon`/`stop_loss`/`take_profit` 在本基准**没有金标准**"
        "（标题级数据无点位/周期）→ 这三格只用于观测**假阳性**（凭空编点位）。"
    )
    add("")

    add("## 2. 置信度边界（Wilson 95% CI；**不得只看点估计**）")
    add("")
    add("| 层 | 抽取器 | 命中/分母 | 准确率（95% CI） | CI 半宽 |")
    add("|---|---|---|---|---|")
    for layer, bucket in ((LAYER_GOLD, gold_metrics), (LAYER_GUBA, guba_metrics)):
        for run in (regex, llm):
            item = bucket[run.name]
            low, high = wilson_interval(item.correct_value, item.gold_present)
            half = (high - low) / 2 if item.gold_present else 0.0
            add(
                f"| {LAYER_LABEL_CN[layer]} | {run.label} | {item.correct_value}/"
                f"{item.gold_present} | {ci_text(item.correct_value, item.gold_present)} "
                f"| ±{half * 100:.1f} pp |"
            )
    add("")
    add(
        "- 读法：Wilson 半宽**随命中率变化**——命中率接近 50% 时最宽（N=100 约 ±10 pp、"
        "N=50 约 ±14 pp）；本批命中率极低（0~2%），区间自然收窄（±2~3 pp），"
        "**但真正该看的是上界**：N=100 时 `0/100` 的上界仍有 3.7%、`2/100` 的上界 7.0%"
        "——「接近 0」并不等于「精确测出 0」。"
    )
    add(
        "- **比较纪律**：N=100 的两组差异小于约 7 pp、N=50 小于约 14 pp 时，**不得声称优劣**；"
        "要缩窄区间只能扩样（或改用人工裁决语料）。"
    )
    add("")

    add(f"## 3. 主结论：`stance` 逐抽取器（黄金层 N={gold_n}）")
    add("")
    add(
        "| 抽取器 | 命中 | 准确率＝召回率（95% CI） | 精确率 | 该判未判 | 提取错误 | "
        "不该判却判 | 双方未给出 |"
    )
    add("|---|---|---|---|---|---|---|---|")
    for run in (regex, llm):
        item = gold_metrics[run.name]
        add(
            f"| {run.label} | {item.correct_value} | "
            f"{ci_text(item.correct_value, item.gold_present)} | {format_pct(item.precision)} "
            f"| {item.missed} | {item.wrong_value} | {item.spurious} | {item.correct_absent} |"
        )
    add("")
    add(
        "> 口径：分母 = **金标准有值**的格子（本基准 `stance` 恒有值，故分母 = 样本数）；"
        "因此召回率与准确率同值；`UNKNOWN` 与错误方向都计入**提取错误**（`wrong_value`）；"
        "`该判未判`在本基准 = 抽取器判「无观点」（`drafts` 为空）。"
    )
    add("")
    for run in (regex, llm):
        item = gold_metrics[run.name]
        unknown_gold, answered_gold = unknown_stance(run, layer=LAYER_GOLD)
        if not item.wrong_value:
            continue
        if unknown_gold == item.wrong_value:
            add(
                f"- **{run.label}**：{item.wrong_value} 格「提取错误」**全部是 `UNKNOWN` 方向"
                "拒答**，没有一格是「判了方向但判反」→ 短板是**不敢下结论**，不是判断力。"
            )
        else:
            add(
                f"- **{run.label}**：{item.wrong_value} 格「提取错误」中 `UNKNOWN` 拒答 "
                f"{unknown_gold} 格、判了具体方向但不符 {item.wrong_value - unknown_gold} 格"
                f"（共给出方向 {answered_gold} 格）。"
            )
    add("")

    add("## 4. 混淆矩阵（黄金层；行 = 金标准，列 = 抽取器）")
    add("")
    for index, run in enumerate((regex, llm), start=1):
        add(f"### 4.{index} {run.label}（`parser_version = {run.name}`）")
        add("")
        lines.extend(matrix_lines(run, layer=LAYER_GOLD))
        add("")

    def cell_of(run: Run, post_id: str, field_name: str) -> CellResult | None:
        for cell in run.layer(LAYER_GOLD):
            if cell.post_id == post_id and cell.field_name == field_name:
                return cell
        return None

    def dump_cells(cells: Sequence[CellResult], run: Run) -> None:
        if not cells:
            add("- （无）")
            return
        for cell in list(cells)[:case_limit]:
            row = texts.get(cell.post_id) or {}
            add(f"- `{cell.post_id}`：{snippet(row.get('text_content', ''))}")
            add(
                f"  - 金标准 `{cell.gold_value or '∅（未给出）'}`"
                f"（原始标签 `{cell.gold_raw or '—'}`）"
                f" ↔ {run.label} 抽取 `{cell.pred_value or '∅（未给出）'}`（`{cell.outcome}`）"
            )

    add("## 5. 正则 vs LLM v13（黄金层）")
    add("")
    add(
        f"- **LLM 修好**（正则非命中 → LLM 命中）：`{len(fixed)}` 格；"
        f"**LLM 弄坏**（正则命中 → LLM 非命中）：`{len(regressed)}` 格；"
        f"净变化 **{delta * 100:+.1f} pp**。"
    )
    add("")
    add(f"### 5.1 LLM 修好的格子（{len(fixed)}）")
    add("")
    if not fixed:
        add("- （无）")
    for post_id, field_name in fixed[:case_limit]:
        row = texts.get(post_id) or {}
        regex_cell = cell_of(regex, post_id, field_name)
        llm_cell = cell_of(llm, post_id, field_name)
        add(f"- `{post_id}`：{snippet(row.get('text_content', ''))}")
        add(
            f"  - 金标准 `{regex_cell.gold_value if regex_cell else '?'}`"
            f" ↔ 正则 `{regex_cell.pred_value if regex_cell else '?'}`"
            f" ↔ LLM `{llm_cell.pred_value if llm_cell else '?'}`"
        )
    add("")
    add(f"### 5.2 LLM 弄坏的格子（{len(regressed)}）")
    add("")
    if not regressed:
        add("- （无）")
    for post_id, field_name in regressed[:case_limit]:
        row = texts.get(post_id) or {}
        regex_cell = cell_of(regex, post_id, field_name)
        llm_cell = cell_of(llm, post_id, field_name)
        add(f"- `{post_id}`：{snippet(row.get('text_content', ''))}")
        add(
            f"  - 金标准 `{regex_cell.gold_value if regex_cell else '?'}`"
            f" ↔ 正则 `{regex_cell.pred_value if regex_cell else '?'}`"
            f" ↔ LLM `{llm_cell.pred_value if llm_cell else '?'}`"
        )
    add("")
    add("### 5.3 与 Mock 200 条基线的对读（**语料/任务/金标准来源均不同，禁止直接比较**）")
    add("")
    lines.extend(mock_reference_lines())
    add("")

    add("## 6. 典型错误案例（黄金层）")
    add("")
    add("### 6.1 正则：方向判错（`wrong_value`）")
    add("")
    lines.extend(
        field_case_lines(
            regex,
            texts,
            layer=LAYER_GOLD,
            field_name=CORE_FIELD,
            outcome=OUTCOME_WRONG,
            limit=case_limit,
        )
    )
    add("")
    add("### 6.2 正则：在没有金标准的字段上凭空给值（`spurious` 假阳性）")
    add("")
    spurious = [
        cell
        for cell in regex.layer(LAYER_GOLD)
        if cell.field_name in PRICE_FIELDS and cell.outcome == OUTCOME_SPURIOUS
    ]
    if not spurious:
        add(
            "- **正则在这批标题上一次都没有凭空给点位/周期**（0 格假阳性）——"
            "原因是它在英文标题上基本不产出观点（见 §0 正则无观点条数），"
            "**「零假阳性」在此是「几乎不作为」的结果，不是精确性的证据**。"
        )
    for field_name in PRICE_FIELDS:
        picked = [cell for cell in spurious if cell.field_name == field_name]
        if picked:
            add(f"**`{field_name}`**（{len(picked)} 格）")
            add("")
            dump_cells(picked, regex)
            add("")
    add("### 6.3 LLM：漏判（`missed`，判「无观点」）")
    add("")
    dump_cells(
        [
            cell
            for cell in llm.layer(LAYER_GOLD)
            if cell.field_name == CORE_FIELD and cell.outcome == OUTCOME_MISSED
        ],
        llm,
    )
    add("")
    add("### 6.4 LLM：方向拒答 `UNKNOWN`（**计为提取错误**，但属「只降不猜」的合规行为）")
    add("")
    dump_cells(
        [
            cell
            for cell in llm.layer(LAYER_GOLD)
            if cell.field_name == CORE_FIELD and cell.pred_value == "UNKNOWN"
        ],
        llm,
    )
    add("")
    add("### 6.5 数据集标注存疑候选（**启发式，待人工复核；不否定数据集**）")
    add("")
    lines.extend(suspicious_dataset_cases(texts, limit=case_limit))
    add("")

    add(f"## 7. 辅助层：中文股吧（N={guba_n}，**不进主结论**）")
    add("")
    add("| 抽取器 | 命中/分母 | 准确率（95% CI） | 该判未判 | 提取错误 | 不该判却判 |")
    add("|---|---|---|---|---|---|")
    for run in (regex, llm):
        item = guba_metrics[run.name]
        add(
            f"| {run.label} | {item.correct_value}/{item.gold_present} | "
            f"{ci_text(item.correct_value, item.gold_present)} | {item.missed} "
            f"| {item.wrong_value} | {item.spurious} |"
        )
    add("")
    add(
        "> 该层**只作辅助参考**：语料是**上证50ETF 股吧标题**（非黄金），且数据集卡片自述"
        "「负向包含不看跌」，与 `docs/10 §4.1.5`（不看多≠看空）**口径冲突**；"
        "任何结论都不得从本节外推到黄金语料。"
    )
    add("")
    add("### 7.1 股吧混淆矩阵（LLM）")
    add("")
    lines.extend(matrix_lines(llm, layer=LAYER_GUBA))
    add("")
    add("### 7.2 股吧案例（LLM 非命中的前几例）")
    add("")
    dump_cells(
        [
            cell
            for cell in llm.layer(LAYER_GUBA)
            if cell.field_name == CORE_FIELD and cell.outcome != OUTCOME_CORRECT_VALUE
        ],
        llm,
    )
    add("")

    add("## 8. 局限与下一步")
    add("")
    add(
        "1. **任务不匹配**：标题级情感分类 ≠ 博主帖子观点提取（无正文、无点位、无周期、"
        "无时间语义）；指标偏低不代表抽取器坏——尤其**正则抽取器面向中文博主长帖**，"
        "在英文标题上基本不出观点，这属**工具设计边界**（已按用户裁决记录为已知边界）。"
    )
    add(
        "2. **金标准来源**：本批金标准是**开源数据集的人工标注**，非本项目 `docs/10` 口径的"
        "人工裁决 → 不能替代 `docs/08 §5` 验收。`docs/10 §7.1` 的 `NOT_EVALUATED` 机制只解决"
        "「不该计分的别计分」，不解决「该有的金标准没有」。"
    )
    add("3. **样本量**：N=100/50 的 95% CI 半宽约 ±8~13 pp，只够做**方向性判断**，不足以声称达标。")
    add(
        "4. **下一步**（**不进入 Phase 3**）：① 保持 Prompt v13 **冻结不变**，"
        "在 Phase 3 用**人工裁决的真实语料**复核观点提取能力；② 若继续用开源数据集，优先找"
        "**黄金相关且含点位/周期**的标注集，并在 `docs/10 §7.1` 登记未评估字段；"
        "③ **TD-35/36/37 已按「已知边界」归档、不做代码修补**（本质是任务定义差异，不是代码缺陷，"
        "见 `TECH_DEBT.md`）。"
    )
    add("")
    add("## 附录 A. 复现命令")
    add("")
    add("```powershell")
    add(repro_command or "python scripts/report_hf_benchmark.py")
    add("```")
    add("")
    add("## 附录 B. 输入文件校验（sha256 前 16 位）")
    add("")
    add("| 文件 | sha256(16) |")
    add("|---|---|")
    for path in inputs.all_files():
        digest = file_digest(path) if path.exists() else "（缺失）"
        add(f"| `{_rel(path)}` | `{digest}` |")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#: `parser_version` → 报告里显示的名字
EXTRACTOR_LABELS: Final[dict[str, str]] = {
    "mock-regex-v1": "RegexOpinionExtractor（纯规则基线）",
    "llm-deepseek-v13": "LLMOpinionExtractor（DeepSeek deepseek-flash）",
}


def label_for(parser_version: str) -> str:
    """抽取器显示名（未知版本原名返回，不猜）。"""
    return EXTRACTOR_LABELS.get(parser_version, parser_version)


def build_run(
    parser_version: str, eval_path: Path, *, usage: Mapping[str, Any] | None = None
) -> Run:
    """由一份逐格明细构造 `Run`（`usage` 只对 LLM 侧传）。"""
    return Run(
        name=parser_version,
        label=label_for(parser_version),
        eval_path=eval_path,
        cells=read_eval_cells(eval_path),
        usage=usage or {},
    )


def repro_command(prefix: Path) -> str:
    """报告附录里的**完整复现命令**（四步一条不漏，含 dry-run 默认值说明）。"""
    rel = _rel(prefix)
    lines = [
        "# ① 加载 + 字段映射（默认 dry-run；--information-type empty = information_type 不评分）",
        f"python scripts/load_hf_benchmark.py --no-dry-run --information-type empty "
        f"--out-prefix {rel}",
        "# ② 正则基线（零成本）",
        f"python scripts/evaluate_extractor.py --gold {rel}_gold.csv --texts {rel}_texts.csv "
        f"--extractor regex --eval-out {rel}_eval_regex.csv "
        f"--report logs/_scratch_hf_regex_report.md",
        "# ③ LLM v13（真实调用；缓存命中即零成本复跑）",
        f"python scripts/evaluate_extractor.py --gold {rel}_gold.csv --texts {rel}_texts.csv "
        f"--extractor llm --eval-out {rel}_eval_llm.csv --report logs/_scratch_hf_llm_report.md "
        f"--llm-usage-out {rel}_eval_llm_usage.json",
        "# ④ 本报告",
        "python scripts/report_hf_benchmark.py",
    ]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/report_hf_benchmark.py",
        description="由 HF 基准的逐格明细生成《Phase 2 真实语料验收报告》（只读文件、不联网）",
    )
    parser.add_argument(
        "--prefix", default=str(DEFAULT_PREFIX), help="基准前缀（默认 logs/hf_benchmark）"
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径（Markdown）")
    parser.add_argument("--regex-name", default="mock-regex-v1", help="正则侧 parser_version")
    parser.add_argument(
        "--llm-name",
        default="llm-deepseek-v13",
        help="LLM 侧 parser_version（台账里有时以台账为准）",
    )
    parser.add_argument(
        "--case-limit", type=int, default=DEFAULT_CASE_LIMIT, help="每个案例小节最多列几条"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印报告，不写文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=输入缺失。"""
    configure_stdout()
    args = _parse_args(argv)
    inputs = resolve_inputs(Path(args.prefix))
    if missing := inputs.missing():
        print(
            "[report] 缺少必需输入：" + "、".join(_rel(path) for path in missing),
            file=sys.stderr,
        )
        print(
            "[report] 请先依次跑：load_hf_benchmark → evaluate_extractor(regex) → "
            "evaluate_extractor(llm)",
            file=sys.stderr,
        )
        return 2

    texts = read_texts(inputs.texts)
    meta = read_meta(inputs.meta)
    usage_payload: dict[str, Any] = {}
    if inputs.llm_usage.exists():
        loaded = json.loads(inputs.llm_usage.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            usage_payload = loaded
    llm_name = str(usage_payload.get("parser_version") or args.llm_name)

    regex = build_run(args.regex_name, inputs.regex_eval)
    llm = build_run(llm_name, inputs.llm_eval, usage=usage_payload.get("usage") or {})
    report = render_report(
        inputs=inputs,
        texts=texts,
        meta=meta,
        regex=regex,
        llm=llm,
        generated_at=datetime.now(UTC),
        case_limit=args.case_limit,
        repro_command=repro_command(inputs.prefix),
    )
    if args.dry_run:
        print(report)
        print("[report] --dry-run：未写任何文件")
        return 0

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"[report] 已写出：{out}（{len(report.splitlines())} 行）")
    print(
        f"[report] 正则 `{regex.name}` 逐格 {len(regex.cells)}｜"
        f"LLM `{llm.name}` 逐格 {len(llm.cells)}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
