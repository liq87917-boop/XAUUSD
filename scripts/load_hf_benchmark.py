"""加载开源标注数据集作为**工程验证用**观点基准（替代人工金标准）。

对应裁决（2026-09-13）：用户决定不再手工标注 19 条真实快讯，改用现成开源标注数据做
"正则 vs LLM v13" 的工程对比。

数据集（均为**标题级情感分类**任务，**非**博主帖子观点提取）：

| 数据集 | split | 取多少 | 文本列 | 标签列 |
|---|---|---|---|---|
| `SaguaroCapital/...-gold` | `test` | 前 100 条（有方向标签） | `News` | 方向 + 情感 |
| `HikasaHana/eastmoney_guba_title` | `test` | 前 50 条 | `title` | `label`(0/1/2) |

完整 id：`SaguaroCapital/sentiment-analysis-in-commodity-market-gold`（其 `Price Sentiment` 取值
`none`/`positive`/`negative`）。

字段映射（**不改数据集原始标签**，只做等价翻译，原值另存 `value_raw` / `*_raw` 列）：

| 数据集标签 | 我们的字段 |
|---|---|
| `Price Direction Up=1` | `stance=LONG` |
| `Price Direction Down=1` | `stance=SHORT` |
| `Price Direction Constant=1` | `stance=FLAT` |
| `Price Sentiment=*` | `information_type=SENTIMENT`（可用 `--information-type empty` 关闭） |
| `label=1` / `label=0` / `label=2` | `stance=LONG` / `SHORT` / `FLAT` |
| 其余字段（`horizon`/`stop_loss`/`take_profit`） | **显式留空** |

- 股吧卡片口径：`0=negative`（看跌 / 对上涨消极）、`1=positive`、`2=neutral`；已按此翻译为
  `SHORT`/`LONG`/`FLAT`，**原始 label 值另存 `value_raw`，不修改数据集**。
- 标题级数据没有点位/周期 → 这些字段记 `correct_absent`；抽取器若给出值则记 `spurious`（假阳性）。

数据获取（**零新增依赖**）：
- `--source datasets`：用 `datasets` 库（**当前 venv 未装**，需团队批准后再装）；
- `--source api`（默认 `auto` 会在未装时自动回落到它）：只用**已安装的 `httpx`** 调 Hugging Face
  **datasets-server** 只读接口，**只取需要的前 N 行**，不下载整份数据集；
- 两种路径产出的行结构一致，映射逻辑共用（有单测锁定）。

产物（`--out-prefix`，默认 `logs/hf_benchmark`）：
- `<prefix>_texts.csv`：`post_id,text_content,has_media,source,split`（+ 原始标签留档列）→ 供
  `evaluate_extractor.py --texts` 使用；
- `<prefix>_gold.csv`：长表 `post_id,field,field_label,value,value_raw,source,note` → 供
  `evaluate_extractor.py --gold` 使用（**长表格式与 `logs/ground_truth_200.csv` 一致**）；
- `<prefix>_meta.json`：取数统计、跳过的"无方向标签"行数、标签分布、数据来源（datasets/API）、
  时间戳（**审计用**）。

红线：
- **默认 `--dry-run`**（只打印计划与统计，不写文件）；
- **不改数据集原始标签**（原值一律另存，映射表写进 meta）；
- 不写 `.env`/密钥；不抓 HTML 页面（只读数据集接口）。

用法::

    python scripts/load_hf_benchmark.py --dry-run                 # 先看计划（零写入）
    python scripts/load_hf_benchmark.py --no-dry-run              # 拉取并落盘 150 条基准
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout  # noqa: E402

#: 黄金数据集
GOLD_DATASET: Final[str] = "SaguaroCapital/sentiment-analysis-in-commodity-market-gold"
#: 中文补充数据集（东方财富·华夏上证50ETF 股吧标题情感）
GUBA_DATASET: Final[str] = "HikasaHana/eastmoney_guba_title"
#: 默认 config / split
DEFAULT_CONFIG: Final[str] = "default"
DEFAULT_SPLIT: Final[str] = "test"
#: 默认取数
DEFAULT_GOLD_ROWS: Final[int] = 100
DEFAULT_GUBA_ROWS: Final[int] = 50
#: 数据集接口（只读）
DATASETS_SERVER: Final[str] = "https://datasets-server.huggingface.co"
#: API 分页上限（跳过"无方向标签"行时的扫描护栏）
DEFAULT_MAX_SCAN: Final[int] = 1000
#: 方向标签 → stance（**不改原始标签，只做等价翻译**）
DIRECTION_TO_STANCE: Final[tuple[tuple[str, str], ...]] = (
    ("Price Direction Up", "LONG"),
    ("Price Direction Down", "SHORT"),
    ("Price Direction Constant", "FLAT"),
)
#: 东方财富股吧 label → stance（来源：数据集卡片 "0(negative)，1(positive)，2(neutral)"）
GUBA_LABEL_TO_STANCE: Final[dict[int, str]] = {0: "SHORT", 1: "LONG", 2: "FLAT"}
#: 参与评估的字段（其余字段显式留空 → 记 `correct_absent`）
SCORED_EMPTY_FIELDS: Final[tuple[str, ...]] = ("horizon", "stop_loss", "take_profit")
#: 未评分标记：与 `scripts/evaluate_extractor.NOT_EVALUATED_MARKER` 是**同一个字符串契约**
#: （两边漂移会被单元测试抓住）。含义：金标准为空 + 带此标记 → 抽取器给值**不计假阳性**，
#: 只记「模型给出了额外信息」。
NOT_EVALUATED_SCORING: Final[str] = "NOT_EVALUATED"
#: 字段中文名（与 `logs/ground_truth_200.csv` 的 `field_label` 列一致）
FIELD_LABELS: Final[dict[str, str]] = {
    "stance": "方向",
    "horizon": "周期",
    "stop_loss": "止损",
    "take_profit": "目标位",
    "information_type": "信息类型",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TextRow:
    """一条基准文本（供 `evaluate_extractor.py --texts`）。"""

    post_id: str
    text_content: str
    has_media: str
    source: str
    split: str
    raw: dict[str, Any] = field(default_factory=dict)  # 原始标签留档（不改动，只复制）

    def to_row(self) -> dict[str, str]:
        payload = {
            "post_id": self.post_id,
            "text_content": self.text_content,
            "has_media": self.has_media,
            "source": self.source,
            "split": self.split,
        }
        payload.update({f"raw_{key}": str(value) for key, value in self.raw.items()})
        return payload


@dataclass(frozen=True, slots=True)
class GoldCellRow:
    """金标准长表的一行（与 `logs/ground_truth_200.csv` 同构）。"""

    post_id: str
    field_name: str
    value: str
    value_raw: str
    source: str
    note: str = ""
    #: 评分标记：空串 = 正常评分；`NOT_EVALUATED` = 本轮不评分（不进准确率、不计假阳性）
    scoring: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "post_id": self.post_id,
            "field": self.field_name,
            "field_label": FIELD_LABELS.get(self.field_name, self.field_name),
            "value": self.value,
            "value_raw": self.value_raw,
            "source": self.source,
            "provenance": f"{self.source}（开源标注数据集，**非人工复核**）",
            "note": self.note,
            "scoring": self.scoring,
        }


@dataclass(slots=True)
class Benchmark:
    """加载结果（文本 + 金标准 + 问题 + 统计）。"""

    texts: list[TextRow] = field(default_factory=list)
    gold: list[GoldCellRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _absent_rows(post_id: str, source: str) -> list[GoldCellRow]:
    """标题级数据没有的字段：**显式留空**。

    评估时这些格子记 `correct_absent`；抽取器若给出值则记 `spurious`（假阳性）。
    """
    return [
        GoldCellRow(post_id, name, "", "", source, "标题级数据无此字段（显式留空）")
        for name in SCORED_EMPTY_FIELDS
    ]


def _stance_from_direction(row: Mapping[str, Any]) -> tuple[str, str]:
    """黄金数据集方向标签 → ``(stance, 触发字段)``（无标签返回空串）。"""
    for field_name, mapped in DIRECTION_TO_STANCE:
        try:
            flag = int(row.get(field_name) or 0)
        except (TypeError, ValueError):
            continue
        if flag == 1:
            return mapped, field_name
    return "", ""


def map_gold_dataset_row(
    row: Mapping[str, Any],
    *,
    position: int,
    information_type: str,
    unlabeled_stance: str = "",
) -> tuple[TextRow | None, list[GoldCellRow], list[str]]:
    """SaguaroCapital 一行 → ``(文本行, 金标准行, 问题)``；无方向标签的行返回 ``(None, [], 说明)``。

    **不改原始标签**：`Price Direction *` / `Price Sentiment` 等原值全部留档在 `TextRow.raw`。
    """
    news = str(row.get("News") or "").strip()
    if not news:
        return None, [], [f"[gold] 第 {position} 行 News 为空，已跳过"]
    stance, trigger = _stance_from_direction(row)
    if not stance:
        if unlabeled_stance:
            stance, trigger = unlabeled_stance, "no-direction-flag"
        else:
            flags = {name: row.get(name) for name, _ in DIRECTION_TO_STANCE}
            return (
                None,
                [],
                [
                    f"[gold] 第 {position} 行无方向标签 {flags}"
                    "（默认跳过；如需保留请加 --include-unlabeled）",
                ],
            )

    post_id = f"hf-gold-{position:05d}"
    source = "hf:saguaro-gold"
    text = TextRow(
        post_id=post_id,
        text_content=news,  # 标题即全部文本（标题级任务）
        has_media="false",
        source=source,
        split=DEFAULT_SPLIT,
        raw={
            "dataset": GOLD_DATASET,
            "Dates": row.get("Dates"),
            "URL": row.get("URL"),
            "Price Direction Up": row.get("Price Direction Up"),
            "Price Direction Constant": row.get("Price Direction Constant"),
            "Price Direction Down": row.get("Price Direction Down"),
            "Price Sentiment": row.get("Price Sentiment"),
            "Asset Comparision": row.get("Asset Comparision"),
            "Past Information": row.get("Past Information"),
            "Future Information": row.get("Future Information"),
        },
    )
    gold = [
        GoldCellRow(
            post_id,
            "stance",
            stance,
            f"{trigger}=1",
            source,
            "数据集方向标签的等价翻译（原标签未改动）",
        )
    ]
    if information_type:
        gold.append(
            GoldCellRow(
                post_id,
                "information_type",
                information_type,
                str(row.get("Price Sentiment") or ""),
                source,
                "按用户裁决：`Price Sentiment` → `information_type`（原标签未改动）",
            )
        )
    else:
        # `--information-type empty`：金标准留空 **且标记未评估** →
        # 模型给出值只记「额外信息」，**不计假阳性**（用户裁决 2026-09-14）
        gold.append(
            GoldCellRow(
                post_id,
                "information_type",
                "",
                str(row.get("Price Sentiment") or ""),
                source,
                "本轮不评分：标题级数据没有可信的 information_type 金标准（原始标签仅留档）",
                NOT_EVALUATED_SCORING,
            )
        )
    gold.extend(_absent_rows(post_id, source))
    return text, gold, []


def map_guba_row(
    row: Mapping[str, Any], *, position: int, information_type: str
) -> tuple[TextRow | None, list[GoldCellRow], list[str]]:
    """东方财富股吧标题 → ``(文本行, 金标准行, 问题)``；label 口径见数据集卡片。"""
    title = str(row.get("title") or "").strip()
    if not title:
        return None, [], [f"[guba] 第 {position} 行 title 为空，已跳过"]
    raw_label = row.get("label")
    try:
        label = int(raw_label)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, [], [f"[guba] 第 {position} 行 label 非整数（{raw_label!r}），已跳过（不猜）"]
    stance = GUBA_LABEL_TO_STANCE.get(label)
    if stance is None:
        return None, [], [f"[guba] 第 {position} 行 label={label} 不在 0/1/2 内，已跳过（不猜）"]

    post_id = f"hf-guba-{position:05d}"
    source = "hf:eastmoney-guba"
    text = TextRow(
        post_id=post_id,
        text_content=title,
        has_media="false",
        source=source,
        split=DEFAULT_SPLIT,
        raw={"dataset": GUBA_DATASET, "label": label},
    )
    gold = [
        GoldCellRow(
            post_id,
            "stance",
            stance,
            f"label={label}",
            source,
            "卡片口径 0=negative/1=positive/2=neutral；"
            "**其负向含「不看跌」，与 docs/10 §4.1.5（不看多≠看空）不同**",
        )
    ]
    if information_type:
        gold.append(
            GoldCellRow(
                post_id,
                "information_type",
                information_type,
                f"label={label}",
                source,
                "按用户裁决统一映射为 SENTIMENT（原标签未改动）",
            )
        )
    else:
        # 同 `map_gold_dataset_row`：留空 + 未评估（不计假阳性）
        gold.append(
            GoldCellRow(
                post_id,
                "information_type",
                "",
                f"label={label}",
                source,
                "本轮不评分：股吧标题没有可信的 information_type 金标准（原始标签仅留档）",
                NOT_EVALUATED_SCORING,
            )
        )
    gold.extend(_absent_rows(post_id, source))
    return text, gold, []


# ---------------------------------------------------------------------------
# 取数（零新增依赖优先）
# ---------------------------------------------------------------------------
#: 取数函数签名：``fetcher(dataset, config=, split=, offset=, length=) -> [原始行]``
Fetcher = Callable[..., list[dict[str, Any]]]
#: 映射函数签名：``mapper(原始行, position=, information_type=) -> (文本行, 金标准行, 问题)``
Mapper = Callable[..., tuple[TextRow | None, list[GoldCellRow], list[str]]]


def datasets_lib_available() -> bool:
    """`datasets` 库是否已安装（未安装时回落到只读 HTTP 接口）。"""
    return importlib.util.find_spec("datasets") is not None


def fetch_rows_via_api(
    dataset: str, *, config: str, split: str, offset: int, length: int, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """用**已安装的 httpx** 调 Hugging Face datasets-server 的只读 `/rows` 接口。

    只取 ``offset..offset+length`` 这些行，**不下载整份数据集**、不写盘。
    """
    import httpx  # noqa: PLC0415 - 已装的运行时依赖（`pyproject.toml` 已声明 httpx>=0.27）

    response = httpx.get(
        f"{DATASETS_SERVER}/rows",
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
            "offset": offset,
            "length": length,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{dataset} 取数失败：HTTP {response.status_code} {response.text[:200]}")
    payload = response.json()
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError(f"{dataset} 返回结构异常：缺少 rows 列表")
    return [item.get("row", {}) for item in rows if isinstance(item, Mapping)]


def fetch_rows_via_datasets_lib(
    dataset: str, *, config: str, split: str, offset: int, length: int
) -> list[dict[str, Any]]:
    """用 **`datasets` 库**取行（需先安装该可选依赖；首次会下载 parquet 到本地缓存）。

    团队未批准前请用 ``--source api``（只读 HTTP 接口，零新增依赖）。
    """
    from datasets import load_dataset  # noqa: PLC0415 - 可选依赖，按需导入

    loaded = load_dataset(dataset, config, split=split)
    end = min(offset + length, len(loaded))
    return [dict(loaded[index]) for index in range(offset, end)]


def resolve_fetcher(source: str) -> tuple[Fetcher, str]:
    """按 ``--source`` 选择取数实现，返回 ``(fetcher, 实际来源标签)``。

    ``auto``（默认）：**装了 `datasets` 就用它，否则用只读 HTTP 接口**（零新增依赖也能跑）。
    """
    if source == "datasets":
        return fetch_rows_via_datasets_lib, "datasets-lib"
    if source == "api":
        return fetch_rows_via_api, "datasets-server-api"
    if datasets_lib_available():
        return fetch_rows_via_datasets_lib, "datasets-lib"
    return fetch_rows_via_api, "datasets-server-api"


def gather_mapped_rows(
    fetcher: Fetcher,
    mapper: Mapper,
    *,
    dataset: str,
    rows_needed: int,
    information_type: str,
    config: str = DEFAULT_CONFIG,
    split: str = DEFAULT_SPLIT,
    max_scan: int = DEFAULT_MAX_SCAN,
    page_size: int = 100,
) -> Benchmark:
    """分页取数 → 逐行映射，直到**可映射的行**够数或达到扫描上限（`max_scan`）。"""
    benchmark = Benchmark()
    position = 0
    skipped = 0
    while len(benchmark.texts) < rows_needed and position < max_scan:
        length = min(page_size, max_scan - position)
        page = fetcher(dataset, config=config, split=split, offset=position, length=length)
        if not page:
            break
        for index, raw in enumerate(page):
            text, gold_rows, problems = mapper(
                raw, position=position + index, information_type=information_type
            )
            benchmark.problems.extend(problems)
            if text is None:
                skipped += 1
                continue
            benchmark.texts.append(text)
            benchmark.gold.extend(gold_rows)
            if len(benchmark.texts) >= rows_needed:
                break
        position += len(page)
    benchmark.stats.update(
        {
            "dataset": dataset,
            "config": config,
            "split": split,
            "rows_needed": rows_needed,
            "rows_taken": len(benchmark.texts),
            "rows_scanned": position,
            "rows_skipped": skipped,
            "gold_cells": len(benchmark.gold),
        }
    )
    return benchmark


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def write_texts(path: Path, texts: Sequence[TextRow]) -> None:
    """写 `<prefix>_texts.csv`（核心列在前，原始标签留档列按首次出现顺序追加）。"""
    columns: list[str] = ["post_id", "text_content", "has_media", "source", "split"]
    for row in texts:
        for key in row.to_row():
            if key not in columns:
                columns.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in texts:
            writer.writerow(row.to_row())


def write_gold(path: Path, gold: Sequence[GoldCellRow]) -> None:
    """写 `<prefix>_gold.csv`（长表，列顺序与 `logs/ground_truth_200.csv` 一致）。"""
    columns = [
        "post_id",
        "field",
        "field_label",
        "value",
        "value_raw",
        "source",
        "provenance",
        "note",
        "scoring",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for cell in gold:
            writer.writerow(cell.to_row())


def write_meta(path: Path, payload: Mapping[str, Any]) -> None:
    """写 `<prefix>_meta.json`（审计：来源、映射表、统计、问题清单）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/load_hf_benchmark.py",
        description="加载开源标注数据集作为工程验证基准（标题级情感任务；**不改原始标签**）",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "api", "datasets"),
        default="auto",
        help="取数方式：auto=装了 datasets 就用它，否则用只读 HTTP 接口（零新增依赖）",
    )
    parser.add_argument(
        "--gold-rows",
        type=int,
        default=DEFAULT_GOLD_ROWS,
        help=f"黄金数据集取多少条（默认 {DEFAULT_GOLD_ROWS}；默认只取**有方向标签**的行）",
    )
    parser.add_argument(
        "--guba-rows",
        type=int,
        default=DEFAULT_GUBA_ROWS,
        help=f"中文股吧标题取多少条（默认 {DEFAULT_GUBA_ROWS}）",
    )
    parser.add_argument(
        "--split", default=DEFAULT_SPLIT, help=f"数据集 split（默认 {DEFAULT_SPLIT}）"
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, help=f"数据集 config（默认 {DEFAULT_CONFIG}）"
    )
    parser.add_argument(
        "--information-type",
        choices=("sentiment", "empty"),
        default="sentiment",
        help="information_type 映射：sentiment=统一写 SENTIMENT（用户裁决）；empty=该字段完全留空",
    )
    parser.add_argument(
        "--include-unlabeled",
        action="store_true",
        help="黄金数据集里无方向标签的行也保留（stance=FLAT）；默认跳过并计数",
    )
    parser.add_argument(
        "--max-scan", type=int, default=DEFAULT_MAX_SCAN, help="跳过无标签行时的扫描上限"
    )
    parser.add_argument(
        "--out-prefix",
        default=str(REPO_ROOT / "logs" / "hf_benchmark"),
        help="输出前缀（生成 <prefix>_texts.csv / _gold.csv / _meta.json）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划与统计，不写文件")
    parser.add_argument(
        "--no-dry-run", action="store_true", help="显式允许写盘（与 --dry-run 互斥）"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=参数或取数失败 / 3=没有映射出任何文本。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[hf] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    args.dry_run = not args.no_dry_run

    information_type = "SENTIMENT" if args.information_type == "sentiment" else ""
    fetcher, source_label = resolve_fetcher(args.source)
    gold_mapper: Mapper = partial(
        map_gold_dataset_row, unlabeled_stance="FLAT" if args.include_unlabeled else ""
    )
    print(f"[hf] 取数方式={source_label}（datasets 库已安装={datasets_lib_available()}）")
    print(
        f"[hf] 计划：{GOLD_DATASET} → {args.gold_rows} 条（split={args.split}，"
        f"{'含无标签行→FLAT' if args.include_unlabeled else '只取有方向标签的行'}）"
    )
    info_label = information_type or f"留空 + scoring={NOT_EVALUATED_SCORING}（不计分）"
    print(f"[hf] 计划：{GUBA_DATASET} → {args.guba_rows} 条；information_type={info_label}")

    try:
        gold_bench = gather_mapped_rows(
            fetcher,
            gold_mapper,
            dataset=GOLD_DATASET,
            rows_needed=args.gold_rows,
            information_type=information_type,
            config=args.config,
            split=args.split,
            max_scan=args.max_scan,
        )
        guba_bench = gather_mapped_rows(
            fetcher,
            map_guba_row,
            dataset=GUBA_DATASET,
            rows_needed=args.guba_rows,
            information_type=information_type,
            config=args.config,
            split=args.split,
            max_scan=args.max_scan,
        )
    except Exception as exc:  # noqa: BLE001 - 取数失败必须显式报错，不静默产出空基准
        print(f"[hf] 取数失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    texts = [*gold_bench.texts, *guba_bench.texts]
    gold = [*gold_bench.gold, *guba_bench.gold]
    problems = [*gold_bench.problems, *guba_bench.problems]
    stance_distribution = Counter(cell.value for cell in gold if cell.field_name == "stance")
    gold_taken = len(gold_bench.texts)
    guba_taken = len(guba_bench.texts)
    print(f"[hf] 已映射：文本 {len(texts)} 条（黄金 {gold_taken} + 中文股吧 {guba_taken}）")
    print(f"[hf] 金标准：{len(gold)} 格")
    print(f"[hf] stance 分布：{dict(stance_distribution)}")
    gold_stats, guba_stats = gold_bench.stats, guba_bench.stats
    print(
        f"[hf] 扫描行数：gold {gold_stats['rows_scanned']}"
        f"（跳过无标签 {gold_stats['rows_skipped']}）"
        f"｜guba {guba_stats['rows_scanned']}"
    )
    for problem in problems[:5]:
        print(f"[hf][告警] {problem}")
    if len(problems) > 5:
        print(f"[hf][告警] 另有 {len(problems) - 5} 条问题（全部写入 meta）")

    meta: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source_label,
        "datasets_lib_installed": datasets_lib_available(),
        "datasets": {
            "gold": GOLD_DATASET,
            "guba": GUBA_DATASET,
            "config": args.config,
            "split": args.split,
        },
        "rows": {
            "texts": len(texts),
            "gold_cells": len(gold),
            "not_evaluated_cells": sum(1 for cell in gold if cell.scoring),
        },
        "stance_distribution": dict(stance_distribution),
        "original_labels_untouched": True,
        "label_mapping": {
            "gold": {
                "Price Direction Up": "LONG",
                "Price Direction Down": "SHORT",
                "Price Direction Constant": "FLAT",
                "Price Sentiment": information_type
                or f"(未使用：value 留空 + scoring={NOT_EVALUATED_SCORING})",
            },
            "guba": {"label=0": "SHORT", "label=1": "LONG", "label=2": "FLAT"},
        },
        "caveats": [
            "标题级情感分类任务，**不等于**博主帖子观点提取（无正文、无点位、无周期）",
            "gold 数据集为商品/黄金新闻标题；guba 数据集为**上证50ETF 股吧**标题（非黄金）",
            "guba 卡片自述「负向含不看跌，与 docs/10 §4.1.5 口径不同」，且提示标注者"
            "「有时自己也犯迷糊，建议自行筛选」",
            "information_type 本轮以 `--information-type empty` 落盘：金标准 `value` 留空 + "
            f"`scoring={NOT_EVALUATED_SCORING}` → 模型给值只记「额外信息」，**不计假阳性**"
            "（口径见 docs/10 §7.1）",
            "黄金数据集内部存在「方向标签全 0」的行（无方向标签）→ 默认跳过并计数",
        ],
        "stats": {"gold": gold_bench.stats, "guba": guba_bench.stats},
        "problems": problems,
    }

    if args.dry_run:
        print("[hf] --dry-run：未写任何文件（加 --no-dry-run 才落盘）")
        return 0 if texts else 3

    prefix = Path(args.out_prefix)
    texts_path = prefix.with_name(f"{prefix.name}_texts.csv")
    gold_path = prefix.with_name(f"{prefix.name}_gold.csv")
    meta_path = prefix.with_name(f"{prefix.name}_meta.json")
    write_texts(texts_path, texts)
    write_gold(gold_path, gold)
    write_meta(meta_path, meta)
    print(f"[hf] 文本：{texts_path}（{len(texts)} 行）")
    print(f"[hf] 金标准：{gold_path}（{len(gold)} 格，长表）")
    print(f"[hf] 元数据：{meta_path}")
    return 0 if texts else 3


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
