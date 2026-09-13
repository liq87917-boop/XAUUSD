"""三模型标注结果对比脚本（**只读 xlsx、只写 CSV/Markdown，绝不碰数据库**）。

背景（用户 2026-09-13 指示）：
    豆包（`_db`）/ 千问（`_qw`）/ 文心一言（`_bd`）三个大模型已各自标注了同一批 200 条样本。
    **这些模型输出不是金标准**——用模型评测模型没有意义（`.clinerules`：LLM 输出不得直接充当
    验收依据）。因此本脚本只做三件事，把"真正的金标准"留给人工：

1. **一致性统计**：`stance` / `horizon` / `stop_loss` / `take_profit` / `information_type`
   五个字段的三模型完全一致比例、各字段分歧率、两两一致率、空值率、分歧形态
   （2:1 / 三方各异 / 有模型留空）；
2. **待人工裁决清单** `logs/pending_review.csv`：一行 = 一个"样本 × 分歧字段"，
   含 `text_content` + 三个模型的**原始判定** + **空的 `final_gold_standard` 列**（人工填写）；
3. **三模型共识** `logs/model_consensus.csv`：完全一致的 (样本, 字段) 及其取值，
   便于人工裁决完成后与之合并生成 `ground_truth.csv`。

并生成报告 `docs/experiments/annotation_model_comparison.md`（统计口径、结果、复现命令、下一步）。

为什么用标准库读 xlsx（而不是 openpyxl / pandas）：
    本环境**未安装 openpyxl**（pandas 读 xlsx 同样依赖它），而项目依赖必须经团队批准才能新增。
    xlsx 本质是"zip + XML"，这里用 ``zipfile`` + ``xml.etree`` 直接读：零新增依赖、离线可复现。
    读取器只支持"按表头取值"这一种用法（不处理样式、公式、日期序列号），行为可预期，
    并由单元测试用**自造的真实 xlsx**（含共享字符串 / 稀疏单元格 / 布尔 / 数字）覆盖。

用法::

    # 默认读 logs/annotation_sample_airesult.xlsx，写 logs/ 与 docs/experiments/
    python scripts/compare_model_annotations.py
    # 指定输入 / 工作表 / 输出
    python scripts/compare_model_annotations.py --input logs/xxx.xlsx --sheet annotation_sample
    # 只看统计不写文件
    python scripts/compare_model_annotations.py --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种运行方式（同 `scripts/sample_annotation_set.py`）：
#   1) 脚本模式 `python scripts/compare_model_annotations.py` —— 需要仓库根在 sys.path 上，
#      否则 `from scripts._console import ...` 会 ModuleNotFoundError；
#   2) 包模式 `from scripts.compare_model_annotations import ...`（测试 / mypy）。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 已安装（editable）时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
import argparse
import csv
import hashlib
import io
import posixpath
import re
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET

from scripts._console import configure_stdout

#: 输入文件候选（按顺序取第一个存在的）；用户在项目里放过多个位置
DEFAULT_INPUT_CANDIDATES: Final[tuple[Path, ...]] = (
    REPO_ROOT / "logs" / "annotation_sample_airesult.xlsx",
    REPO_ROOT / "annotation_sample_airesult.xlsx",
    REPO_ROOT / "logs" / "标注结果.xlsx",
    REPO_ROOT / "标注结果.xlsx",
)
DEFAULT_SHEET: Final[str] = "annotation_sample"
DEFAULT_PENDING_OUT: Final[Path] = REPO_ROOT / "logs" / "pending_review.csv"
DEFAULT_CONSENSUS_OUT: Final[Path] = REPO_ROOT / "logs" / "model_consensus.csv"
DEFAULT_REPORT_OUT: Final[Path] = (
    REPO_ROOT / "docs" / "experiments" / "annotation_model_comparison.md"
)
#: 参考文件（可选）：用于校验 xlsx 与抽样清单是否同一批样本
DEFAULT_REFERENCE_CSV: Final[Path] = REPO_ROOT / "logs" / "annotation_sample.csv"

#: 三个模型的列后缀（**大小写不敏感**：真实文件里有
#: `stop_loss_DB` / `stop_loss_QW` / `stop_loss_bd` 这种混用写法）
MODELS: Final[tuple[str, ...]] = ("db", "qw", "bd")
MODEL_LABELS: Final[dict[str, str]] = {"db": "豆包", "qw": "千问", "bd": "文心一言"}
#: 本次对比的字段（用户指定；可用 `--fields` 扩展）
DEFAULT_FIELDS: Final[tuple[str, ...]] = (
    "stance",
    "horizon",
    "stop_loss",
    "take_profit",
    "information_type",
)
FIELD_LABELS: Final[dict[str, str]] = {
    "stance": "方向",
    "horizon": "周期",
    "stop_loss": "止损",
    "take_profit": "目标位",
    "information_type": "信息类型",
    "instrument": "标的",
    "confidence": "置信度",
    "entry_low": "入场下限",
    "entry_high": "入场上限",
    "no_opinion": "无观点",
    "leakage_suspect": "疑似泄漏",
}
#: 待裁决清单列（`final_gold_standard` 及其后三列留空，供人工填写）
PENDING_COLUMNS: Final[tuple[str, ...]] = (
    "review_id",
    "post_id",
    "field",
    "field_label",
    "model_db",
    "model_qw",
    "model_bd",
    "normalized_db",
    "normalized_qw",
    "normalized_bd",
    "agreeing_models",
    "disagreeing_models",
    "blank_models",
    "text_content",
    "source_name",
    "published_at",
    "effective_at",
    "final_gold_standard",
    "reviewer",
    "reviewed_at",
    "notes",
)
#: 共识清单列（**未经人工确认**，仅作合并素材）
CONSENSUS_COLUMNS: Final[tuple[str, ...]] = (
    "post_id",
    "field",
    "field_label",
    "value_agreed",
    "raw_values",
    "models",
    "text_content",
    "source_name",
    "published_at",
    "effective_at",
    "provenance",
)
CONSENSUS_PROVENANCE: Final[str] = "3-model-consensus (NOT human-verified)"
#: 视为"未给出"的写法（不参与取值比较；但"有人留空"本身算一次分歧）
MISSING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "—",
        "n/a",
        "na",
        "nan",
        "none",
        "null",
        "nil",
        "无",
        "没有",
        "未给出",
        "未提供",
        "空",
    }
)
_MAIN_NS: Final[str] = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS: Final[str] = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL_NS: Final[str] = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z]+)(\d+)")
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")
_HEADER_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[\s_\-()（）]+")
#: 文本编码探测顺序：Excel「另存为 CSV」在中文 Windows 上常写成 GBK；
#: **不放 latin-1**（它永远不会失败，会把中文静默解成乱码）
TEXT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "gbk", "big5")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SheetTable:
    """一张表（xlsx 工作表或 CSV）：表头 + 行（列名 → 字符串值，缺失列为空串）。"""

    source: str
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    sheet_name: str = ""
    sheet_names: tuple[str, ...] = ()
    #: 文本输入时实际使用的编码（xlsx 为空）；写进报告便于排查乱码
    encoding: str = ""


@dataclass(frozen=True, slots=True)
class ModelAnnotation:
    """一条样本的三模型标注。

    ``raw`` = 原样字符串（**给人看**，用于人工裁决）；``normalized`` = 归一化后（**给机器比**）。
    """

    post_id: str
    text_content: str
    source_name: str
    published_at: str
    effective_at: str
    raw: Mapping[str, Mapping[str, str]]
    normalized: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class FieldOutcome:
    """单个"样本 × 字段"的对比结论。"""

    post_id: str
    field_name: str
    raw: Mapping[str, str]
    normalized: Mapping[str, str]
    pattern: str  # unanimous | unanimous_blank | two_vs_one | three_way
    agreeing_models: tuple[str, ...]
    disagreeing_models: tuple[str, ...]
    blank_models: tuple[str, ...]

    @property
    def is_unanimous(self) -> bool:
        """三模型一致（含"三方都留空"，它会单独统计，见 `pattern`）。"""
        return self.pattern in {"unanimous", "unanimous_blank"}

    @property
    def key(self) -> str:
        return f"{self.post_id}|{self.field_name}"


@dataclass(slots=True)
class FieldStats:
    """单字段的统计（行级口径：每行算一次）。"""

    field_name: str
    rows_total: int = 0
    unanimous: int = 0
    unanimous_blank: int = 0
    two_vs_one: int = 0
    three_way: int = 0
    blank_counts: dict[str, int] = field(default_factory=dict)
    pairwise_agree: dict[str, int] = field(default_factory=dict)

    @property
    def unanimous_total(self) -> int:
        return self.unanimous + self.unanimous_blank

    @property
    def unanimous_rate(self) -> float:
        return self.unanimous_total / self.rows_total if self.rows_total else 0.0

    @property
    def disagreement_rate(self) -> float:
        return 1.0 - self.unanimous_rate


@dataclass(slots=True)
class ComparisonReport:
    """整批对比结果（统计 + 三类明细 + 问题清单）。"""

    source: str
    sheet_name: str = ""
    rows_total: int = 0
    #: 归一化口径：所有字段都一致（含"三方都留空"的字段）
    rows_full_agreement: int = 0
    #: 严格口径：所有字段都一致，且不含"三方都留空"的字段
    rows_full_agreement_strict: int = 0
    #: 原始字符串口径：三模型逐字相同（不归一化）
    rows_full_agreement_raw: int = 0
    field_stats: dict[str, FieldStats] = field(default_factory=dict)
    outcomes: list[FieldOutcome] = field(default_factory=list)
    pending_outcomes: list[FieldOutcome] = field(default_factory=list)
    consensus_outcomes: list[FieldOutcome] = field(default_factory=list)
    extra_model_fields: tuple[str, ...] = ()
    problems: list[str] = field(default_factory=list)
    reference_check: str = ""

    @property
    def rows_full_agreement_rate(self) -> float:
        return self.rows_full_agreement / self.rows_total if self.rows_total else 0.0

    @property
    def pending_sample_count(self) -> int:
        return len({outcome.post_id for outcome in self.pending_outcomes})


# ---------------------------------------------------------------------------
# 读表：xlsx（标准库解析）/ csv
# ---------------------------------------------------------------------------
def _column_index(cell_ref: str) -> int:
    """``"AB12"`` → ``27``（0 基列号）。"""
    match = _CELL_REF_RE.match(cell_ref or "")
    if match is None:
        raise ValueError(f"非法单元格引用：{cell_ref!r}")
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _plain_number(text: str) -> str:
    """Excel 把整数存成 ``2450``、浮点存成 ``0.9``；只去掉不影响语义的 ``.0`` 尾巴。"""
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".")[0]
    return text


def _cell_value(cell: ET.Element, shared: Sequence[str]) -> str:
    """取单个单元格的文本（支持共享字符串 / 内联字符串 / 布尔 / 公式结果 / 数字）。"""
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_MAIN_NS}t")).strip()
    value_node = cell.find(f"{_MAIN_NS}v")
    if value_node is None or value_node.text is None:
        return ""
    text = value_node.text
    cell_type = cell.get("t")
    if cell_type == "s":
        index = int(text)
        return shared[index] if 0 <= index < len(shared) else ""
    if cell_type == "b":
        return "true" if text.strip() in {"1", "true"} else "false"
    if cell_type in {"str", "e"}:
        return text.strip()
    return _plain_number(text)


def _read_shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return tuple(
        "".join(node.text or "" for node in item.iter(f"{_MAIN_NS}t"))
        for item in root.iter(f"{_MAIN_NS}si")
    )


def _sheet_targets(archive: zipfile.ZipFile) -> tuple[tuple[str, str], ...]:
    """返回 ``((工作表名, zip 内路径), ...)``，顺序与 Excel 中一致。

    路径既可能是相对的（``worksheets/sheet1.xml``，手写/多数导出器），
    也可能是**绝对**的（``/xl/worksheets/sheet1.xml``，openpyxl 就是这样写的），
    两种都要归一化到 ``xl/...``（真实踩坑：只判断相对形式的写法会把路径拼成 ``xl/xl/...``）。
    """
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rels = {
        relationship.get("Id", ""): relationship.get("Target", "")
        for relationship in rels_root.iter(f"{_PACKAGE_REL_NS}Relationship")
    }
    targets: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_MAIN_NS}sheet"):
        raw_target = rels.get(sheet.get(f"{_REL_NS}id", ""), "")
        if not raw_target:
            continue
        target = posixpath.normpath(raw_target.lstrip("/"))
        if not target.startswith("xl/"):
            target = posixpath.normpath(f"xl/{target}")
        targets.append((sheet.get("name", ""), target))
    return tuple(targets)


def read_xlsx(path: Path, *, sheet: str | None = None) -> SheetTable:
    """把 xlsx 的指定工作表读成 :class:`SheetTable`（默认第一张表）。

    Raises:
        LookupError: 指定的工作表不存在。
        ValueError: 文件不是合法 xlsx / 没有工作表 / 单元格引用非法。
    """
    with zipfile.ZipFile(path) as archive:
        targets = _sheet_targets(archive)
        if not targets:
            raise ValueError(f"{path} 中没有任何工作表")
        names = tuple(name for name, _ in targets)
        selected = sheet or names[0]
        match = next((item for item in targets if item[0] == selected), None)
        if match is None:
            raise LookupError(f"工作表不存在：{selected!r}（可用：{list(names)}）")
        shared = _read_shared_strings(archive)
        root = ET.fromstring(archive.read(match[1]))

    raw_rows: list[list[str]] = []
    for row in root.iter(f"{_MAIN_NS}row"):
        values: list[str] = []
        for cell in row.iter(f"{_MAIN_NS}c"):
            index = _column_index(cell.get("r") or "")
            while len(values) <= index:
                values.append("")
            values[index] = _cell_value(cell, shared)
        raw_rows.append(values)
    if not raw_rows:
        return SheetTable(
            source=f"xlsx:{path}#{selected}",
            headers=(),
            rows=(),
            sheet_name=selected,
            sheet_names=names,
        )
    headers = tuple(value.strip() for value in raw_rows[0])
    rows = tuple(
        {
            header: (row[index] if index < len(row) else "")
            for index, header in enumerate(headers)
            if header
        }
        for row in raw_rows[1:]
        if any(value.strip() for value in row)
    )
    return SheetTable(
        source=f"xlsx:{path}#{selected}",
        headers=headers,
        rows=rows,
        sheet_name=selected,
        sheet_names=names,
    )


def read_csv_table(path: Path) -> SheetTable:
    """读 CSV：**自动识别编码**（UTF-8 / 带 BOM / GBK / Big5），兼容 Excel 导出的中文 CSV。"""
    text, encoding = read_text(path)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple((name or "").strip() for name in (reader.fieldnames or ()))
    rows = tuple(
        {(key or ""): (value or "") for key, value in row.items() if key} for row in reader
    )
    return SheetTable(
        source=f"csv:{path}",
        headers=headers,
        rows=rows,
        sheet_name=path.name,
        encoding=encoding,
    )


def sniff_is_xlsx(path: Path) -> bool:
    """按**文件内容**判断是否为 xlsx（ZIP 容器）。

    为什么不能只看扩展名：Excel 里「另存为」很容易把文件存成工作簿格式，
    但文件名仍然叫 `xxx.csv`（本项目真实踩过：人工裁决后的 `pending_review.csv`
    实际是 xlsx）。因此读表一律以内容为准。
    """
    with path.open("rb") as handle:
        return handle.read(4) == b"PK\x03\x04"


def read_text(path: Path) -> tuple[str, str]:
    """读文本文件，返回 ``(内容, 实际使用的编码)``；按 `TEXT_ENCODINGS` 依次探测。

    Raises:
        ValueError: 所有候选编码都失败（不做"静默乱码"兜底）。
    """
    raw = path.read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(f"无法识别文本编码（已尝试 {TEXT_ENCODINGS}）：{path}")


def read_any_table(path: Path, *, sheet: str | None = None) -> SheetTable:
    """按**内容**读取表格：ZIP/xlsx → 标准库解析；否则按文本 CSV 解析（自动识别编码）。"""
    if sniff_is_xlsx(path):
        return read_xlsx(path, sheet=sheet)
    return read_csv_table(path)


def pending_has_human_work(path: Path) -> bool:
    """待裁决文件里是否已经有**人工填写**（`final_gold_standard` 非空）。

    用途：保护人工成果——重新跑对比脚本时默认**不得覆盖**已填写的裁决表。
    """
    if not path.exists():
        return False
    try:
        table = read_any_table(path)
    except (LookupError, ValueError, OSError, zipfile.BadZipFile):
        return False
    return any((row.get("final_gold_standard") or "").strip() for row in table.rows)


def read_table(path: Path, *, sheet: str | None = None) -> SheetTable:
    """按后缀分发读取（``.xlsx``/``.xlsm`` → 标准库解析；``.csv`` → 标准读取）。"""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx(path, sheet=sheet)
    if suffix in {".csv", ".txt"}:
        return read_csv_table(path)
    raise ValueError(f"不支持的输入格式：{suffix!r}（只支持 .xlsx / .csv）")


def resolve_input_path(explicit: str | None) -> Path:
    """定位输入文件：显式路径优先，否则取 `DEFAULT_INPUT_CANDIDATES` 中第一个存在的。"""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"输入文件不存在：{path}")
        return path
    for candidate in DEFAULT_INPUT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "未找到三模型标注结果文件，请用 --input 指定；候选路径："
        + "、".join(str(item) for item in DEFAULT_INPUT_CANDIDATES)
    )


def file_digest(path: Path) -> str:
    """文件 sha256 前 16 位（写进报告，便于事后确认"跑的是哪份文件"）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 取值归一化（**只用于比较**；报告与待裁决清单始终展示模型原始输出）
# ---------------------------------------------------------------------------
def is_blank(raw: str) -> bool:
    """是否属于"未给出"（空白或常见占位符）。"""
    return raw.strip().lower() in MISSING_TOKENS


def _header_key(name: str) -> str:
    """列名归一：去掉空白/下划线/连字符/括号并转小写（`stop_loss_DB` → `stoplossdb`）。"""
    return _HEADER_NOISE_RE.sub("", name.strip().lower())


def find_field_columns(
    headers: Sequence[str], fields: Sequence[str]
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """定位"字段 × 模型"对应的实际列名（**大小写 / 下划线不敏感**）。

    Returns:
        ``(mapping, problems)``，其中 ``mapping[field][model] = 实际列名``。
    """
    index = {_header_key(header): header for header in headers if header}
    mapping: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for name in fields:
        columns: dict[str, str] = {}
        for model in MODELS:
            column = index.get(f"{_header_key(name)}{model}")
            if column is None:
                problems.append(f"缺少列：{name}_{model}（列名按大小写/下划线不敏感匹配）")
            else:
                columns[model] = column
        mapping[name] = columns
    return mapping, problems


def detect_extra_fields(headers: Sequence[str], known_fields: Sequence[str]) -> tuple[str, ...]:
    """发现"同样带三模型列、但本次未纳入对比"的字段（提示可用 `--fields` 扩展）。"""
    known = {_header_key(name) for name in known_fields}
    seen: dict[str, set[str]] = {}
    for key in {_header_key(header) for header in headers if header}:
        for model in MODELS:
            if key.endswith(model) and len(key) > len(model):
                seen.setdefault(key[: -len(model)], set()).add(model)
    return tuple(
        sorted(
            base
            for base, models in seen.items()
            if len(models) == len(MODELS) and base not in known
        )
    )


_STANCE_SYNONYMS: Final[dict[str, str]] = {
    "long": "LONG",
    "bullish": "LONG",
    "buy": "LONG",
    "多": "LONG",
    "看多": "LONG",
    "做多": "LONG",
    "多单": "LONG",
    "买入": "LONG",
    "看涨": "LONG",
    "偏多": "LONG",
    "short": "SHORT",
    "bearish": "SHORT",
    "sell": "SHORT",
    "空": "SHORT",
    "看空": "SHORT",
    "做空": "SHORT",
    "空单": "SHORT",
    "卖出": "SHORT",
    "看跌": "SHORT",
    "偏空": "SHORT",
    "flat": "FLAT",
    "neutral": "FLAT",
    "range": "FLAT",
    "观望": "FLAT",
    "中性": "FLAT",
    "震荡": "FLAT",
    "区间": "FLAT",
    "盘整": "FLAT",
    "持平": "FLAT",
    "无方向": "FLAT",
    "unknown": "UNKNOWN",
    "未知": "UNKNOWN",
    "无法判断": "UNKNOWN",
    "不确定": "UNKNOWN",
    "不适用": "UNKNOWN",
    "未表态": "UNKNOWN",
}
_HORIZON_SYNONYMS: Final[dict[str, str]] = {
    "15m": "15M",
    "15分钟": "15M",
    "15分": "15M",
    "超短": "15M",
    "分钟级": "15M",
    "30m": "30M",
    "30分钟": "30M",
    "半小时": "30M",
    "1h": "1H",
    "60m": "1H",
    "1小时": "1H",
    "小时级": "1H",
    "日内": "1H",
    "短线": "1H",
    "4h": "4H",
    "4小时": "4H",
    "半天": "4H",
    "1d": "1D",
    "日": "1D",
    "日线": "1D",
    "中线": "1D",
    "长线": "1D",
    "本周": "1D",
    "周线": "1D",
    "波段": "1D",
    "unknown": "UNKNOWN",
    "未知": "UNKNOWN",
    "无法判断": "UNKNOWN",
}
_INFO_TYPE_SYNONYMS: Final[dict[str, str]] = {
    "macro": "MACRO",
    "宏观": "MACRO",
    "宏观经济": "MACRO",
    "基本面": "MACRO",
    "政策": "MACRO",
    "technical": "TECHNICAL",
    "技术": "TECHNICAL",
    "技术面": "TECHNICAL",
    "技术分析": "TECHNICAL",
    "news": "NEWS",
    "消息": "NEWS",
    "新闻": "NEWS",
    "事件": "NEWS",
    "sentiment": "SENTIMENT",
    "情绪": "SENTIMENT",
    "市场情绪": "SENTIMENT",
    "positioning": "POSITIONING",
    "持仓": "POSITIONING",
    "仓位": "POSITIONING",
    "资金流": "POSITIONING",
    "other": "OTHER",
    "其他": "OTHER",
    "unknown": "UNKNOWN",
    "未知": "UNKNOWN",
}
_NUMERIC_FIELDS: Final[frozenset[str]] = frozenset(
    {"stop_loss", "take_profit", "entry_low", "entry_high", "confidence"}
)


def normalize_number(text: str) -> str:
    """提取并归一化第一个数值。

    支持 ``2,450`` / ``≈2340`` / ``2340 美元`` / ``2340-2350``（取 2340）；
    并统一小数写法（``2420.0`` → ``2420``、``2340.50`` → ``2340.5``），
    避免"同一个价位不同写法"被误判成分歧。
    """
    if is_blank(text):
        return ""
    cleaned = text.replace("，", ",").replace("－", "-").replace("—", "-")
    match = _NUMBER_RE.search(cleaned)
    if match is None:
        return re.sub(r"\s+", " ", text.strip()).lower()
    raw = match.group(0).replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:  # pragma: no cover - 正则已保证匹配到数字
        return raw
    return format(value.normalize(), "f")


def normalize_value(field_name: str, raw: str) -> str:
    """把模型输出归一化成可比较的形式（**只影响比较，不影响展示**）。"""
    text = raw.strip()
    if is_blank(text):
        return ""
    if field_name in _NUMERIC_FIELDS:
        return normalize_number(text)
    collapsed = re.sub(r"[\s_\-/]+", "", text).lower()
    if field_name == "stance":
        return _STANCE_SYNONYMS.get(collapsed, collapsed.upper())
    if field_name == "horizon":
        return _HORIZON_SYNONYMS.get(collapsed, collapsed.upper())
    if field_name == "information_type":
        return _INFO_TYPE_SYNONYMS.get(collapsed, collapsed.upper())
    return re.sub(r"\s+", " ", text).lower()


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------
def build_annotations(
    table: SheetTable, fields: Sequence[str]
) -> tuple[list[ModelAnnotation], dict[str, dict[str, str]], list[str]]:
    """把表转成逐样本的三模型标注（同时保留 raw 与 normalized 两套值）。"""
    mapping, problems = find_field_columns(table.headers, fields)
    annotations: list[ModelAnnotation] = []
    for index, row in enumerate(table.rows, start=2):
        raw: dict[str, dict[str, str]] = {}
        normalized: dict[str, dict[str, str]] = {}
        for name in fields:
            columns = mapping.get(name, {})
            raw[name] = {model: row.get(columns.get(model, ""), "") for model in MODELS}
            normalized[name] = {
                model: normalize_value(name, raw[name][model]) for model in MODELS
            }
        annotations.append(
            ModelAnnotation(
                post_id=row.get("post_id", "").strip() or f"row-{index}",
                text_content=row.get("text_content", "").strip(),
                source_name=row.get("source_name", "").strip(),
                published_at=row.get("published_at", "").strip(),
                effective_at=row.get("effective_at", "").strip(),
                raw=raw,
                normalized=normalized,
            )
        )
    return annotations, mapping, problems


def compare_field(
    post_id: str, field_name: str, raw: Mapping[str, str], normalized: Mapping[str, str]
) -> FieldOutcome:
    """单字段单样本对比，判定形态：一致 / 三方都留空 / 2:1 / 三方各异。"""
    values = {model: normalized.get(model, "") for model in MODELS}
    blank_models = tuple(model for model in MODELS if not values[model])
    distinct = set(values.values())
    if len(distinct) == 1:
        pattern = "unanimous_blank" if not values[MODELS[0]] else "unanimous"
        agreeing: tuple[str, ...] = MODELS
        disagreeing: tuple[str, ...] = ()
    else:
        top_value, top_count = Counter(values.values()).most_common(1)[0]
        if top_count == 2:
            pattern = "two_vs_one"
            agreeing = tuple(model for model in MODELS if values[model] == top_value)
            disagreeing = tuple(model for model in MODELS if values[model] != top_value)
        else:
            pattern = "three_way"
            agreeing = ()
            disagreeing = MODELS
    return FieldOutcome(
        post_id=post_id,
        field_name=field_name,
        raw=dict(raw),
        normalized=dict(normalized),
        pattern=pattern,
        agreeing_models=agreeing,
        disagreeing_models=disagreeing,
        blank_models=blank_models,
    )


def compare(
    annotations: Sequence[ModelAnnotation],
    fields: Sequence[str],
    *,
    source: str = "",
    sheet_name: str = "",
) -> ComparisonReport:
    """整批对比：字段统计 + 行级一致性 + 待裁决 / 共识明细。"""
    report = ComparisonReport(source=source, sheet_name=sheet_name, rows_total=len(annotations))
    for name in fields:
        report.field_stats[name] = FieldStats(field_name=name, rows_total=len(annotations))

    for annotation in annotations:
        row_agreement = True
        row_agreement_strict = True
        row_agreement_raw = True
        for name in fields:
            outcome = compare_field(
                annotation.post_id, name, annotation.raw[name], annotation.normalized[name]
            )
            report.outcomes.append(outcome)
            stats = report.field_stats[name]
            if outcome.pattern == "unanimous":
                stats.unanimous += 1
            elif outcome.pattern == "unanimous_blank":
                stats.unanimous_blank += 1
                row_agreement_strict = False  # 严格口径不把"三方都留空"算作有效一致
            elif outcome.pattern == "two_vs_one":
                stats.two_vs_one += 1
            else:
                stats.three_way += 1

            for model in MODELS:
                if not outcome.normalized[model]:
                    stats.blank_counts[model] = stats.blank_counts.get(model, 0) + 1
            for left in range(len(MODELS)):
                for right in range(left + 1, len(MODELS)):
                    first, second = MODELS[left], MODELS[right]
                    value = outcome.normalized[first]
                    # 两两一致只在"双方都给出了值"时计入（空值不算一致，避免虚高）
                    if value and value == outcome.normalized[second]:
                        key = f"{first}|{second}"
                        stats.pairwise_agree[key] = stats.pairwise_agree.get(key, 0) + 1

            if not outcome.is_unanimous:
                row_agreement = False
            if outcome.pattern != "unanimous":
                # 严格口径只认"三模型都给出了相同值"：留空、2:1、三方各异都不算
                row_agreement_strict = False
            if len({annotation.raw[name][model].strip() for model in MODELS}) != 1:
                row_agreement_raw = False

            if outcome.is_unanimous:
                report.consensus_outcomes.append(outcome)
            else:
                report.pending_outcomes.append(outcome)

        report.rows_full_agreement += int(row_agreement)
        report.rows_full_agreement_strict += int(row_agreement_strict)
        report.rows_full_agreement_raw += int(row_agreement_raw)

    report.pending_outcomes.sort(
        key=lambda item: (item.post_id, fields.index(item.field_name))
        if item.field_name in fields
        else (item.post_id, len(fields))
    )
    report.consensus_outcomes.sort(
        key=lambda item: (item.post_id, fields.index(item.field_name))
        if item.field_name in fields
        else (item.post_id, len(fields))
    )
    return report


# ---------------------------------------------------------------------------
# 导出：待人工裁决清单 / 三模型共识
# ---------------------------------------------------------------------------
def _field_rank(field_name: str, fields: Sequence[str]) -> int:
    return fields.index(field_name) if field_name in fields else len(fields)


def write_pending_review(
    path: Path,
    report: ComparisonReport,
    annotations_by_id: Mapping[str, ModelAnnotation],
    *,
    fields: Sequence[str],
) -> int:
    """写出待人工裁决清单（一行 = 一个"样本 × 分歧字段"），返回行数。

    **`final_gold_standard` 及之后的列一律留空**：金标准只能由人工填写。
    """
    ordered = sorted(
        report.pending_outcomes,
        key=lambda item: (item.post_id, _field_rank(item.field_name, fields)),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(PENDING_COLUMNS)
        for index, outcome in enumerate(ordered, start=1):
            annotation = annotations_by_id.get(outcome.post_id)
            writer.writerow(
                [
                    f"R{index:04d}",
                    outcome.post_id,
                    outcome.field_name,
                    FIELD_LABELS.get(outcome.field_name, outcome.field_name),
                    *(outcome.raw.get(model, "") for model in MODELS),
                    *(outcome.normalized.get(model, "") for model in MODELS),
                    "/".join(outcome.agreeing_models),
                    "/".join(outcome.disagreeing_models),
                    "/".join(outcome.blank_models),
                    annotation.text_content if annotation else "",
                    annotation.source_name if annotation else "",
                    annotation.published_at if annotation else "",
                    annotation.effective_at if annotation else "",
                    "",  # final_gold_standard（**人工填写**）
                    "",  # reviewer
                    "",  # reviewed_at
                    "",  # notes
                ]
            )
    return len(ordered)


def write_consensus(
    path: Path,
    report: ComparisonReport,
    annotations_by_id: Mapping[str, ModelAnnotation],
    *,
    fields: Sequence[str],
) -> int:
    """写出三模型共识（一行 = 一个"样本 × 一致字段"），返回行数。

    供人工裁决完成后与 `pending_review.csv` 合并成 `ground_truth.csv`；
    `value_agreed` 用**归一化后的规范值**（便于机器使用），`raw_values` 保留三方原文以便追溯。
    """
    ordered = sorted(
        report.consensus_outcomes,
        key=lambda item: (item.post_id, _field_rank(item.field_name, fields)),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CONSENSUS_COLUMNS)
        for outcome in ordered:
            annotation = annotations_by_id.get(outcome.post_id)
            writer.writerow(
                [
                    outcome.post_id,
                    outcome.field_name,
                    FIELD_LABELS.get(outcome.field_name, outcome.field_name),
                    outcome.normalized.get(MODELS[0], ""),
                    " | ".join(outcome.raw.get(model, "") for model in MODELS),
                    "/".join(MODELS),
                    annotation.text_content if annotation else "",
                    annotation.source_name if annotation else "",
                    annotation.published_at if annotation else "",
                    annotation.effective_at if annotation else "",
                    CONSENSUS_PROVENANCE,
                ]
            )
    return len(ordered)


# ---------------------------------------------------------------------------
# 报告（Markdown）
# ---------------------------------------------------------------------------
def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "—"
    return f"{numerator / denominator * 100:.1f}%"


def _check_reference(path: Path, annotations: Sequence[ModelAnnotation]) -> str:
    """与抽样清单（`logs/annotation_sample.csv`）比对 post_id，确认是同一批样本。"""
    if not path.exists():
        return f"未找到参考清单 `{path}`，跳过对齐检查"
    table = read_csv_table(path)
    expected = [
        row.get("post_id", "").strip() for row in table.rows if row.get("post_id", "").strip()
    ]
    actual = {annotation.post_id for annotation in annotations}
    missing = [post_id for post_id in expected if post_id not in actual]
    extra = sorted(actual - set(expected))
    if not missing and not extra:
        return f"通过：{len(actual)}/{len(expected)} 条 `post_id` 与参考清单完全一致"
    detail = (
        f"异常：参考 {len(expected)} 条 / 实际 {len(actual)} 条；"
        f"缺失 {len(missing)} 条、多出 {len(extra)} 条"
    )
    if missing:
        detail += f"；缺失示例 {missing[:3]}"
    if extra:
        detail += f"；多出示例 {extra[:3]}"
    return detail


def render_report(
    report: ComparisonReport,
    *,
    input_path: Path,
    digest: str,
    fields: Sequence[str],
    generated_at: datetime,
    pending_path: Path,
    consensus_path: Path,
    pending_rows: int,
    consensus_rows: int,
) -> str:
    """生成对比分析报告（Markdown 文本）。"""
    lines: list[str] = []
    add = lines.append
    field_labels = "、".join(FIELD_LABELS.get(name, name) for name in fields)
    add("# 多模型标注对比分析（豆包 / 千问 / 文心一言）")
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 输入文件：`{input_path}`（sha256 前 16 位：`{digest}`）")
    add(f"- 工作表：`{report.sheet_name}`")
    add(f"- 样本数：**{report.rows_total}**；对比字段（{len(fields)} 个）：{field_labels}")
    add(f"- 对齐检查：{report.reference_check or '（未执行）'}")
    add("")
    add("> **必读（红线）**：这三份标注都是**大模型输出，不是金标准**。")
    add("> 用模型去评测模型没有意义（`.clinerules`：LLM 输出不得直接充当验收依据）。")
    add("> 本报告只用来**定位分歧、缩小人工裁决范围**；`ground_truth.csv` 必须由人工确认后生成，")
    add("> 之后才谈得上用 `RegexOpinionExtractor` 去对标（评估脚本尚未编写，本次**未执行**）。")
    add("")
    add("## 1. 结论摘要")
    add("")
    add(
        "- **三模型完全一致**（归一化口径，5 个字段全部一致，含\"三方都留空\"）："
        f"**{report.rows_full_agreement} / {report.rows_total} = "
        f"{_pct(report.rows_full_agreement, report.rows_total)}**"
    )
    add(
        "- 严格口径（三模型都给出相同值，不含\"三方都留空\"）："
        f"{report.rows_full_agreement_strict} / {report.rows_total} = "
        f"{_pct(report.rows_full_agreement_strict, report.rows_total)}"
    )
    add(
        "- 原始字符串口径（逐字相同，不做归一化）："
        f"{report.rows_full_agreement_raw} / {report.rows_total} = "
        f"{_pct(report.rows_full_agreement_raw, report.rows_total)}"
    )
    add(
        f"- **需要人工裁决**：{report.pending_sample_count} 个样本、"
        f"{len(report.pending_outcomes)} 条\"样本 × 字段\"记录（已导出 `{pending_path.name}`）"
    )
    add("")
    add("## 2. 字段级一致性")
    add("")
    add(
        "| 字段 | 三模型一致 | 其中三方都留空 | 2:1 分歧 | 三方各异 | **分歧率** | "
        "db-qw 一致 | db-bd 一致 | qw-bd 一致 |"
    )
    add("|---|---|---|---|---|---|---|---|---|")
    for name in fields:
        stats = report.field_stats[name]
        pair = stats.pairwise_agree
        add(
            f"| {FIELD_LABELS.get(name, name)}（`{name}`） "
            f"| {stats.unanimous_total}（{_pct(stats.unanimous_total, stats.rows_total)}） "
            f"| {stats.unanimous_blank} "
            f"| {stats.two_vs_one} "
            f"| {stats.three_way} "
            f"| **{_pct(stats.two_vs_one + stats.three_way, stats.rows_total)}** "
            f"| {_pct(pair.get('db|qw', 0), stats.rows_total)} "
            f"| {_pct(pair.get('db|bd', 0), stats.rows_total)} "
            f"| {_pct(pair.get('qw|bd', 0), stats.rows_total)} |"
        )
    add("")
    add("> \"三方都留空\" = 三家都判断该字段不存在（例如 FLAT 观点没有止损位）；它算\"一致\"，")
    add("> 但单独列出以便看清**覆盖度**。两两一致率只统计\"双方都给出了值\"的情形，空值不算一致。")
    add("")
    add("## 3. 各模型留空率（覆盖度）")
    add("")
    add("| 字段 | 豆包 `_db` | 千问 `_qw` | 文心一言 `_bd` |")
    add("|---|---|---|---|")
    for name in fields:
        stats = report.field_stats[name]
        add(
            f"| {FIELD_LABELS.get(name, name)} "
            f"| {_pct(stats.blank_counts.get('db', 0), stats.rows_total)} "
            f"| {_pct(stats.blank_counts.get('qw', 0), stats.rows_total)} "
            f"| {_pct(stats.blank_counts.get('bd', 0), stats.rows_total)} |"
        )
    add("")
    add("## 4. 输出文件与人工裁决流程")
    add("")
    add(f"1. `{pending_path}`：**{pending_rows} 行**待裁决（一行 = 一个\"样本 × 分歧字段\"，")
    add("   含 `text_content` 与三个模型的原始判定）；只填 `final_gold_standard` 一列即可，")
    add("   `notes` 可用于记录判定理由。")
    add(f"2. `{consensus_path}`：**{consensus_rows} 行**三模型共识（**未经人工确认**）；")
    add("   `value_agreed` 是归一化后的规范值，`raw_values` 保留三方原文便于追溯。")
    add("3. 人工裁决完成后：`pending_review.csv` 的 `final_gold_standard` 与")
    add("   `model_consensus.csv` 的 `value_agreed` 都是 `post_id, field, value` 长表，")
    add("   合并即得 `ground_truth.csv`；**务必保留 provenance 列**（人工裁决 vs 三模型共识）。")
    add("4. 只有到这一步之后，才用 `ground_truth.csv` 评估 `RegexOpinionExtractor` 的")
    add("   stance / horizon / 点位 / 信息类型准确率（评估脚本尚未编写，本次未执行）。")
    add("")
    add("## 5. 归一化口径（**只用于比较**）")
    add("")
    add("- 展示与落盘一律保留**模型原始输出**；只有比较时才归一化，避免\"做多 vs LONG\"")
    add("  这类同义不同写被误判成分歧。")
    add(
        "- 方向：`做多/看多/多单/bullish/long` → `LONG`；"
        "`做空/看空/空单/bearish/short` → `SHORT`；"
    )
    add("  `观望/中性/震荡/flat/neutral` → `FLAT`；`未知/无法判断/不适用` → `UNKNOWN`。")
    add("- 周期：`15分钟/超短` → `15M`；`半小时/30分钟` → `30M`；`日内/短线/1小时` → `1H`；")
    add("  `4小时/半天` → `4H`；`日线/中线/本周/波段` → `1D`。")
    add("- 信息类型：`宏观/基本面` → `MACRO`；`技术面` → `TECHNICAL`；`消息/新闻` → `NEWS`；")
    add("  `情绪` → `SENTIMENT`；`持仓/资金流` → `POSITIONING`；`其他` → `OTHER`。")
    add("- 止损/目标位：取第一个数值（`2,450`→`2450`、`≈2340`→`2340`、`2340-2350`→`2340`）；")
    add("  空白与 `-` / `N/A` / `None` / `无` 一律视为**未给出**。")
    add("")
    add("## 6. 附录")
    add("")
    if report.extra_model_fields:
        add(
            "- 文件里还有其它带三模型列、但**本次未纳入对比**的字段："
            + "、".join(f"`{name}`" for name in report.extra_model_fields)
            + "（需要时用 `--fields` 追加）"
        )
    else:
        add("- 文件里没有其它三模型字段。")
    add(
        "- 列名匹配规则：大小写 / 下划线 / 连字符不敏感"
        "（真实文件里存在 `stop_loss_DB`、`stop_loss_QW`、`stop_loss_bd` 这种混用写法）。"
    )
    if report.problems:
        add("- 读取/匹配问题：")
        for problem in report.problems:
            add(f"  - {problem}")
    else:
        add("- 读取/匹配问题：无。")
    add("")
    add("复现命令：")
    add("")
    add("```powershell")
    add("python scripts/compare_model_annotations.py")
    add("```")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.compare_model_annotations",
        description="对比豆包/千问/文心的标注一致性，导出待人工裁决清单（只读，不碰数据库）",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="三模型标注结果（.xlsx / .csv；默认自动查找 logs/annotation_sample_airesult.xlsx）",
    )
    parser.add_argument(
        "--sheet",
        default=DEFAULT_SHEET,
        help=f"xlsx 工作表名（默认 {DEFAULT_SHEET}；传空串则取第一张表）",
    )
    parser.add_argument(
        "--fields", default=",".join(DEFAULT_FIELDS), help="要对比的字段（逗号分隔）"
    )
    parser.add_argument(
        "--pending-out", default=str(DEFAULT_PENDING_OUT), help="待人工裁决清单输出路径"
    )
    parser.add_argument(
        "--consensus-out", default=str(DEFAULT_CONSENSUS_OUT), help="三模型共识输出路径"
    )
    parser.add_argument(
        "--report-out", default=str(DEFAULT_REPORT_OUT), help="对比报告（Markdown）输出路径"
    )
    parser.add_argument(
        "--reference-csv",
        default=str(DEFAULT_REFERENCE_CSV),
        help="样本对齐参考文件（默认 logs/annotation_sample.csv）",
    )
    parser.add_argument("--no-reference-check", action="store_true", help="跳过样本对齐检查")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使待裁决清单里已有人工填写也允许覆盖（默认拒绝，保护人工成果）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写任何文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行对比并导出文件；退出码 0=成功 / 2=输入或参数问题 / 3=关键列缺失。"""
    configure_stdout()
    args = _parse_args(argv)
    fields = tuple(name.strip() for name in args.fields.split(",") if name.strip())
    if not fields:
        print("[compare] --fields 不能为空", file=sys.stderr)
        return 2
    try:
        input_path = resolve_input_path(args.input)
    except FileNotFoundError as exc:
        print(f"[compare] {exc}", file=sys.stderr)
        return 2
    try:
        table = read_table(input_path, sheet=args.sheet or None)
    except (LookupError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"[compare] 读取失败：{exc}", file=sys.stderr)
        return 2

    annotations, mapping, problems = build_annotations(table, fields)
    if not annotations:
        print("[compare] 表里没有数据行", file=sys.stderr)
        return 2
    missing = [name for name in fields if not mapping.get(name)]
    if missing:
        print(
            f"[compare] 关键列缺失：{', '.join(missing)}；请核对 --fields 与文件列名",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"[compare]   - {problem}", file=sys.stderr)
        return 3

    report = compare(annotations, fields, source=table.source, sheet_name=table.sheet_name)
    report.problems = list(problems)
    report.extra_model_fields = detect_extra_fields(table.headers, fields)
    if not args.no_reference_check:
        report.reference_check = _check_reference(Path(args.reference_csv), annotations)
    annotations_by_id = {annotation.post_id: annotation for annotation in annotations}

    print(
        f"[compare] 输入：{table.source}"
        f"（{report.rows_total} 条样本；字段：{', '.join(fields)}）"
    )
    print(
        f"[compare] 三模型完全一致（归一化口径）：{report.rows_full_agreement}/{report.rows_total}"
        f" = {_pct(report.rows_full_agreement, report.rows_total)}"
    )
    for name in fields:
        stats = report.field_stats[name]
        print(
            f"[compare]   字段 {name}：分歧率 "
            f"{_pct(stats.two_vs_one + stats.three_way, stats.rows_total)}"
            f"（2:1 {stats.two_vs_one} / 三方各异 {stats.three_way} / "
            f"三方都留空 {stats.unanimous_blank}）"
        )
    print(
        f"[compare] 待人工裁决：{report.pending_sample_count} 个样本、"
        f"{len(report.pending_outcomes)} 条记录"
    )
    if report.reference_check:
        print(f"[compare] 对齐检查：{report.reference_check}")

    if args.dry_run:
        print("[compare] --dry-run：未写任何文件")
        return 0

    pending_path = Path(args.pending_out)
    if not args.force and pending_has_human_work(pending_path):
        print(
            f"[compare] 待裁决清单里已有**人工填写**，拒绝覆盖：{pending_path}",
            file=sys.stderr,
        )
        print(
            "[compare] 这是保护人工成果（裁决表只此一份）；确认可覆盖时加 --force。",
            file=sys.stderr,
        )
        return 4
    consensus_path = Path(args.consensus_out)
    report_path = Path(args.report_out)
    pending_rows = write_pending_review(
        pending_path, report, annotations_by_id, fields=fields
    )
    consensus_rows = write_consensus(
        consensus_path, report, annotations_by_id, fields=fields
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            report,
            input_path=input_path,
            digest=file_digest(input_path),
            fields=fields,
            generated_at=datetime.now(UTC),
            pending_path=pending_path,
            consensus_path=consensus_path,
            pending_rows=pending_rows,
            consensus_rows=consensus_rows,
        ),
        encoding="utf-8",
    )
    print(f"[compare] 待裁决清单：{pending_path}（{pending_rows} 行，final_gold_standard 留空）")
    print(f"[compare] 三模型共识：{consensus_path}（{consensus_rows} 行，未经人工确认）")
    print(f"[compare] 对比报告：{report_path}")
    print("[compare] 提醒：模型输出不是金标准——请先人工填 final_gold_standard，再谈评测抽取器")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
