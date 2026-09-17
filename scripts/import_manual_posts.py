"""语料分流导入：**观点语料 / 宏观背景 / 事件层** 三层分开落盘。

用户裁决（2026-09-13，`docs/11 §1.5`）：
- **`opinion_corpus`（观点语料）** = 中文黄金快讯（`manual-*`，90~1000 字符，含金价/点位/持仓）
  → **准确率验收的唯一分母**；
- **`macro_background`（宏观背景）** = FRED Blog 英文宏观长文 →
  **不进准确率分母**（分母≈0 无意义）；
- **`event_layer`（事件层，兜底）** = 显式 `source_type=EVENT` 或正文 < 90 字符的数据。

分流规则（确定性、按优先级，dry-run 会逐行打印理由）：

| 优先级 | 条件 | 去向 |
|---|---|---|
| 1 | 显式 `source_type == EVENT` | `event_layer` |
| 2 | `len(content) < --opinion-min-chars`（默认 90，快讯形态下限） | `event_layer` |
| 3 | `source` 以 `--manual-prefix`（默认 `manual-`）开头 | **`opinion_corpus`** |
| 4 | 其余 | `macro_background` |

> 注：原 `MIN_CONTENT_CHARS = 200` 是按"必须有正文"的探测标准写进工具链的**常量**，
> 不是 `docs/11 §1.1` 的列契约；分层后按层取尺（见 `docs/11 §1.5.2`），**列契约未改动**。

红线：
- **输入只读**：绝不覆盖任何输入文件（输出路径与输入相同 → 硬失败，除非显式
  `--allow-in-place`）；
- **默认 `--dry-run`**：只体检 + 打印分流计划，不落盘；
- 列名映射复用 `sample_annotation_set._COLUMN_ALIASES`（与抽样同一套口径）；
- 不写 `.env`/密钥；输出一律 **UTF-8(BOM)**。

输入格式：`.csv`（UTF-8/UTF-8-BOM；若是 GBK 会**告警后尝试**解码）或 **Excel `.xlsx`**
（含"被 Excel 存成 .csv 扩展名的 xlsx"——按 ZIP 魔数自动识别，避免再次踩坑）。

用法::

    python scripts/import_manual_posts.py --dry-run        # 先看分流计划（默认就是 dry-run）
    python scripts/import_manual_posts.py --no-dry-run `
        --corpus logs/real_posts_2026_09_14.csv `
        --out-corpus logs/real_posts_opinion_2026_09_14.csv `
        --out-background logs/macro_background_2026_09_14.csv `
        --meta-out logs/import_manual_posts.meta.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import statistics
import sys
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout  # noqa: E402
from scripts.collect_rss import CSV_COLUMNS, DEFAULT_OUT_DIR  # noqa: E402
from scripts.sample_annotation_set import _COLUMN_ALIASES, _normalize_row  # noqa: E402 - 契约复用

#: `opinion_corpus` 的最小正文字符数（**快讯形态下限**；依据见 `docs/11 §1.5.2`）
OPINION_MIN_CHARS: Final[int] = 90
#: 手工录入来源前缀：这些来源（中文黄金快讯）进 **观点语料** 层
DEFAULT_MANUAL_PREFIX: Final[str] = "manual-"
#: 三层名称（写进终端输出 / meta / notes 标记，便于审计）
LAYER_OPINION: Final[str] = "opinion_corpus"
LAYER_BACKGROUND: Final[str] = "macro_background"
LAYER_EVENT: Final[str] = "event_layer"
#: 事件层标记
SOURCE_TYPE_EVENT: Final[str] = "EVENT"
SOURCE_TYPE_NEWS: Final[str] = "NEWS"
#: 时间字段的硬校验容差（吸收机器时钟偏差，避免误杀）
FUTURE_TOLERANCE: Final[timedelta] = timedelta(hours=1)
#: 读入时按别名归一化后必须存在的列（缺任一 → 硬失败）
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("content", "published_at", "effective_at", "source")
#: 去重键（命中任一即视为同一条）
DEDUP_FIELDS: Final[tuple[str, ...]] = ("url", "id")
#: 需要归一大小写的布尔列（Excel 往返会把 `true/false` 变成 `True/False`，而下游只认小写）
BOOLEAN_COLUMNS: Final[tuple[str, ...]] = ("has_media",)
#: 抽样脚本的规范列名 → 语料列名（`docs/11 §1.1`）
_CANONICAL_TO_CORPUS: Final[dict[str, str]] = {
    "post_id": "id",
    "text_content": "content",
    "source_name": "source",
}
#: 语料输出列顺序（= §1.1 契约 + `source_type`；其余列按首次出现顺序追加）
_CORPUS_COLUMNS: Final[tuple[str, ...]] = (*CSV_COLUMNS, "source_type")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_text(raw: bytes) -> tuple[str, str]:
    """解码文本：UTF-8(BOM) 优先，GBK/GB18030 兜底（返回 ``(text, encoding)``）。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码：既不是 UTF-8，也不是 GBK/GB18030")


def _read_xlsx(path: Path) -> list[dict[str, str]]:
    """读取 Excel 工作簿（按需导入 openpyxl；扩展名不影响，按内容判断）。"""
    try:
        import openpyxl  # noqa: PLC0415 - 按需导入，缺依赖时给可执行提示
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise ValueError(
            f"{path} 是 Excel 工作簿（xlsx），需要 openpyxl（dev 依赖）：pip install openpyxl"
        ) from exc
    # 必须用 file-like 读：openpyxl 会按**扩展名**校验，而"被 Excel 存成 .csv"的文件名
    # 不带 .xlsx，直接传路径会被 InvalidFileException 拒绝（实测坑）。
    with io.BytesIO(path.read_bytes()) as buffer:
        book = openpyxl.load_workbook(buffer, read_only=True, data_only=True)
        sheet = book[book.sheetnames[0]]
        rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(cell or "").strip() for cell in rows[0]]
    records: list[dict[str, str]] = []
    for raw in rows[1:]:
        record = {
            header[index]: ("" if value is None else str(value))
            for index, value in enumerate(raw)
            if index < len(header) and header[index]
        }
        if any(value.strip() for value in record.values()):  # 跳过整行空白
            records.append(record)
    return records


def read_table(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """读入 CSV / Excel，返回 ``(原始行, 格式元信息)``；**只读、不做任何写操作**。

    - 文件头是 ZIP 魔数（``PK\\x03\\x04``）→ 按 **xlsx** 解析（覆盖"被 Excel 存成 .csv"的坑）；
    - 否则按文本 CSV 解析：UTF-8 优先，GBK 兜底（**并写告警**，提示另存为 CSV UTF-8）。
    """
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    info: dict[str, Any] = {"path": str(path), "sha256": _sha256(path), "format": "csv"}
    if zipfile.is_zipfile(path):
        info["format"] = "xlsx"
        rows = _read_xlsx(path)
    else:
        text, encoding = _decode_text(path.read_bytes())
        info["encoding"] = encoding
        if encoding not in {"utf-8-sig", "utf-8"}:
            info["warning"] = (
                f"{path.name} 不是 UTF-8（实测 {encoding}）——已按该编码读取，"
                "建议在 Excel 里『另存为 → CSV UTF-8』避免后续口径问题"
            )
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
    info["rows"] = len(rows)
    return rows, info


def normalize_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """列名归一：别名 → 规范（复用 `_normalize_row`），**并保留别名表外的列**（title/url/…）。

    同时把 :data:`BOOLEAN_COLUMNS` 的 `True/False`（Excel 往返产物）归一为契约要求的
    `true/false` 小写——否则下游 `sample_annotation_set` 的真值判定（只认小写）会静默判 False。
    """
    normalized: list[dict[str, str]] = []
    for row in rows:
        mapped = _normalize_row(row)
        record: dict[str, str] = {
            _CANONICAL_TO_CORPUS.get(name, name): ("" if value is None else str(value).strip())
            for name, value in mapped.items()
        }
        for key, value in row.items():
            if key is None or str(key).strip().lower() in _COLUMN_ALIASES:
                continue
            record.setdefault(str(key).strip(), "" if value is None else str(value).strip())
        for column in BOOLEAN_COLUMNS:
            if column in record and record[column].lower() in {"true", "false"}:
                record[column] = record[column].lower()
        normalized.append(record)
    return normalized


def parse_time(value: str | None) -> datetime | None:
    """解析 ISO8601 时间；无时区视为不可解析（本管道不做时区猜测）。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def validate(rows: Sequence[Mapping[str, str]], *, now: datetime) -> list[str]:
    """硬校验（有任意一条 → 退出码 2）：缺必需列 / 正文空 / 时间不可解析 /
    `effective_at < published_at` / 出现未来时间（容差 1 小时）。"""
    errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        missing = [column for column in REQUIRED_COLUMNS if column not in row]
        if missing:
            errors.append(f"第 {index} 行缺少必需列：{missing}")
            continue
        if not row["content"].strip():
            errors.append(f"第 {index} 行 content 为空")
        published = parse_time(row["published_at"])
        effective = parse_time(row["effective_at"])
        if published is None:
            errors.append(f"第 {index} 行 published_at 不可解析/缺时区：{row['published_at']!r}")
        if effective is None:
            errors.append(f"第 {index} 行 effective_at 不可解析/缺时区：{row['effective_at']!r}")
        if published is not None and effective is not None and effective < published:
            errors.append(f"第 {index} 行 effective_at 早于 published_at（时间因果违规）")
        if published is not None and published > now + FUTURE_TOLERANCE:
            errors.append(f"第 {index} 行 published_at 是未来时间：{row['published_at']!r}")
    return errors


def route_row(
    row: Mapping[str, str], *, manual_prefix: str, opinion_min_chars: int
) -> tuple[str, str]:
    """判定单行去向：返回 ``(层名, 理由)``。

    层 ∈ {``opinion_corpus``, ``macro_background``, ``event_layer``}。
    """
    if (row.get("source_type") or "").strip().upper() == SOURCE_TYPE_EVENT:
        return LAYER_EVENT, "显式声明 source_type=EVENT"
    length = len((row.get("content") or "").strip())
    if length < opinion_min_chars:
        return LAYER_EVENT, f"正文仅 {length} 字符 < {opinion_min_chars}（太短）"
    source = (row.get("source") or "").strip()
    if manual_prefix and source.lower().startswith(manual_prefix.lower()):
        return (
            LAYER_OPINION,
            f"手工录入中文快讯（前缀 {manual_prefix!r}）→ 观点语料（{length} 字符）",
        )
    return LAYER_BACKGROUND, f"非快讯来源 {source or '<空>'}（{length} 字符）→ 宏观背景层"


def _normalized_text(text: str) -> str:
    return " ".join(text.split()).lower()


def fingerprint(row: Mapping[str, str]) -> str:
    """正文指纹（规范化后哈希）：跨来源识别"同一条快讯"。"""
    return hashlib.sha256(_normalized_text(row.get("content", "")).encode("utf-8")).hexdigest()


def dedupe(
    rows: Iterable[Mapping[str, str]], *, seen: set[str] | None = None
) -> tuple[list[dict[str, str]], int, set[str]]:
    """按 `url` / `id` / 正文指纹去重；返回 ``(保留行, 重复条数, 已知键集合)``。"""
    known: set[str] = set(seen or ())
    kept: list[dict[str, str]] = []
    duplicates = 0
    for row in rows:
        keys = {
            f"{field}={row.get(field, '').strip()}"
            for field in DEDUP_FIELDS
            if row.get(field, "").strip()
        }
        keys.add(f"fp={fingerprint(row)}")
        if keys & known:
            duplicates += 1
            continue
        known |= keys
        kept.append(dict(row))
    return kept, duplicates, known


def mark_layer(row: Mapping[str, str], layer: str) -> dict[str, str]:
    """按层给行打标记（`source_type` + `notes_collect` 追加说明，可审计）。"""
    marked = dict(row)
    marker = {
        LAYER_OPINION: "层=opinion_corpus（观点语料；准确率验收分母）",
        LAYER_BACKGROUND: "采集用途=EVENT（层=macro_background；不进观点语料）",
        LAYER_EVENT: "采集用途=EVENT（层=event_layer；不进观点语料）",
    }[layer]
    marked["source_type"] = SOURCE_TYPE_NEWS if layer == LAYER_OPINION else SOURCE_TYPE_EVENT
    note = (marked.get("notes_collect") or "").strip()
    marked["notes_collect"] = marker if not note or marker in note else f"{note}；{marker}"
    return marked


def soft_warnings(rows: Sequence[Mapping[str, str]], *, label: str) -> list[str]:
    """软告警（写 meta，不阻塞）：条数 / 来源分布 / 长度分布 / 缺 url / 超长截断风险。"""
    if not rows:
        return [f"{label}：没有任何记录"]
    warnings: list[str] = []
    sources = Counter((row.get("source") or "").strip() or "<空>" for row in rows)
    lengths = [len((row.get("content") or "").strip()) for row in rows]
    warnings.append(
        f"{label}：{len(rows)} 条；来源分布={dict(sources)}；"
        f"长度 min={min(lengths)} median={int(statistics.median(lengths))} max={max(lengths)}"
    )
    missing_url = sum(1 for row in rows if not (row.get("url") or "").strip())
    if missing_url:
        warnings.append(f"{label}：{missing_url} 条缺 url（可追溯性下降）")
    too_long = sum(1 for length in lengths if length > 4000)
    if too_long:
        warnings.append(f"{label}：{too_long} 条正文 > 4000 字符（抽取器可能截断）")
    return warnings


def output_columns(rows: Sequence[Mapping[str, str]]) -> list[str]:
    """列顺序：§1.1 契约 + `source_type` 在前，其余列按首次出现顺序追加。"""
    columns = list(_CORPUS_COLUMNS)
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def write_csv(path: Path, rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> None:
    """写 UTF-8(BOM) CSV（列顺序固定，缺列补空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/import_manual_posts.py",
        description="语料分流导入：观点语料（正文）与事件层（快讯）分开落盘（输入只读）",
    )
    parser.add_argument(
        "--corpus",
        "--input",
        action="append",
        dest="corpus",
        default=None,
        help="主语料来源（CSV/xlsx，可多次）；默认 logs/real_posts_2026_09_14.csv",
    )
    parser.add_argument(
        "--flash",
        action="append",
        default=None,
        help="手工快讯来源（可多次）；默认 logs/manual_flash_news.csv（不存在时跳过并告警）",
    )
    parser.add_argument(
        "--out-corpus",
        default=str(DEFAULT_OUT_DIR / "real_posts_opinion_2026_09_14.csv"),
        help="观点语料输出（仅 ≥min-chars 的非手工行）",
    )
    parser.add_argument(
        "--out-flash",
        default=str(DEFAULT_OUT_DIR / "event_flash_news.csv"),
        help="事件层输出（source_type=EVENT，兜底层）",
    )
    parser.add_argument(
        "--out-background",
        default=str(DEFAULT_OUT_DIR / "macro_background_2026_09_14.csv"),
        help="宏观背景层输出（如 FRED 英文长文；**不进观点准确率分母**）",
    )
    parser.add_argument(
        "--meta-out", default=None, help="体检元数据 JSON（默认 <out-corpus>.meta.json）"
    )
    parser.add_argument(
        "--opinion-min-chars",
        type=int,
        default=OPINION_MIN_CHARS,
        help=f"opinion_corpus 最小正文字符数（默认 {OPINION_MIN_CHARS}；见 docs/11 §1.5.2）",
    )
    parser.add_argument(
        "--manual-prefix",
        default=DEFAULT_MANUAL_PREFIX,
        help=f"手工录入来源前缀（默认 {DEFAULT_MANUAL_PREFIX}），命中一律进事件层",
    )
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help="允许输出路径与输入路径相同（默认禁止：保护输入只读）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只体检 + 打印分流计划（缺省即此模式）"
    )
    parser.add_argument(
        "--no-dry-run", action="store_true", help="显式允许落盘（与 --dry-run 互斥）"
    )
    return parser.parse_args(argv)


def _load_inputs(
    paths: Sequence[Path], *, kind: str, warnings: list[str]
) -> tuple[list[dict[str, str]], list[dict[str, Any]], int]:
    """读入一批输入（kind=``corpus``/``flash``）；返回 ``(归一化行, 元信息, 退出码)``。

    ``flash`` 的**默认路径**不存在时跳过并告警（快讯是补充路径，不该阻塞主流程）；
    显式指定的路径不存在 → 交回退出码 2。
    """
    rows: list[dict[str, str]] = []
    provenance: list[dict[str, Any]] = []
    for path in paths:
        if kind == "flash" and not path.exists():
            if path == Path(str(DEFAULT_OUT_DIR / "manual_flash_news.csv")):
                warnings.append(f"默认快讯文件不存在，已跳过：{path}")
                continue
            print(f"[import] 读取失败：输入文件不存在：{path}", file=sys.stderr)
            return [], provenance, 2
        try:
            raw, info = read_table(path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[import] 读取失败：{exc}", file=sys.stderr)
            return [], provenance, 2
        info["kind"] = kind
        provenance.append(info)
        if info.get("warning"):
            warnings.append(str(info["warning"]))
        rows.extend(normalize_rows(raw))
    return rows, provenance, 0


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=硬失败或参数问题 / 3=没有任何记录通过分流。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[import] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    args.dry_run = not args.no_dry_run
    if args.opinion_min_chars < OPINION_MIN_CHARS:
        print(
            f"[import][告警] --opinion-min-chars={args.opinion_min_chars} 低于 docs/11 §1.5.2 的 "
            f"{OPINION_MIN_CHARS}：更短的文本会混进观点语料，**不建议**"
        )

    corpus_inputs = [
        Path(path) for path in (args.corpus or [str(DEFAULT_OUT_DIR / "real_posts_2026_09_14.csv")])
    ]
    flash_inputs = [
        Path(path) for path in (args.flash or [str(DEFAULT_OUT_DIR / "manual_flash_news.csv")])
    ]
    out_corpus = Path(args.out_corpus)
    out_background = Path(args.out_background)
    out_flash = Path(args.out_flash)
    meta_out = Path(args.meta_out) if args.meta_out else out_corpus.with_suffix(".meta.json")

    inputs = {path.resolve() for path in (*corpus_inputs, *flash_inputs)}
    if not args.allow_in_place:
        for output in (out_corpus, out_background, out_flash, meta_out):
            if output.resolve() in inputs:
                print(
                    f"[import] 输出路径与输入相同：{output}（输入只读红线）。"
                    "请换输出路径，或显式加 --allow-in-place",
                    file=sys.stderr,
                )
                return 2

    warnings: list[str] = []
    corpus_rows, corpus_info, code = _load_inputs(corpus_inputs, kind="corpus", warnings=warnings)
    if code:
        return code
    flash_rows, flash_info, code = _load_inputs(flash_inputs, kind="flash", warnings=warnings)
    if code:
        return code
    raw_rows = [*corpus_rows, *flash_rows]
    provenance = [*corpus_info, *flash_info]
    if not raw_rows:
        print("[import] 输入没有任何记录", file=sys.stderr)
        return 3

    errors = validate(raw_rows, now=datetime.now(UTC))
    if errors:
        print(f"[import] 硬校验失败（{len(errors)} 条）：", file=sys.stderr)
        for message in errors[:20]:
            print(f"  - {message}", file=sys.stderr)
        return 2

    unique_rows, duplicates, _ = dedupe(raw_rows)
    buckets: dict[str, list[dict[str, str]]] = {
        LAYER_OPINION: [],
        LAYER_BACKGROUND: [],
        LAYER_EVENT: [],
    }
    decisions: list[dict[str, Any]] = []
    for row in unique_rows:
        layer, reason = route_row(
            row, manual_prefix=args.manual_prefix, opinion_min_chars=args.opinion_min_chars
        )
        row_key = (row.get("url") or row.get("id") or fingerprint(row)[:12]).strip()
        decisions.append({"row": row_key, "layer": layer, "reason": reason})
        buckets[layer].append(mark_layer(row, layer))

    opinion = buckets[LAYER_OPINION]
    background = buckets[LAYER_BACKGROUND]
    event = buckets[LAYER_EVENT]
    warnings.extend(soft_warnings(opinion, label="opinion_corpus（观点语料）"))
    warnings.extend(soft_warnings(background, label="macro_background（宏观背景）"))
    warnings.extend(soft_warnings(event, label="event_layer（事件层）"))
    print(
        f"[import] 读入 {len(raw_rows)} 行（去重 {duplicates} 条）→ "
        f"观点语料 {len(opinion)} / 宏观背景 {len(background)} / 事件层 {len(event)}"
    )
    for decision in decisions:
        print(f"  [{decision['layer']:<16}] {decision['row'][:64]}  ← {decision['reason']}")
    for warning in warnings:
        print(f"[import][告警] {warning}")

    if args.dry_run:
        print("[import] --dry-run：未写任何文件（加 --no-dry-run 才落盘）")
        return 0 if (opinion or background or event) else 3

    write_csv(out_corpus, opinion, output_columns(opinion))
    write_csv(out_background, background, output_columns(background))
    write_csv(out_flash, event, output_columns(event))
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    meta_out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "inputs": provenance,
                "rows_in": len(raw_rows),
                "duplicates_removed": duplicates,
                "opinion_rows": len(opinion),
                "background_rows": len(background),
                "event_rows": len(event),
                "opinion_min_chars": args.opinion_min_chars,
                "manual_prefix": args.manual_prefix,
                "decisions": decisions,
                "warnings": warnings,
                "outputs": {
                    "opinion_corpus": str(out_corpus),
                    "macro_background": str(out_background),
                    "event_layer": str(out_flash),
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[import] 观点语料（{LAYER_OPINION}）：{out_corpus}（{len(opinion)} 行）")
    print(f"[import] 宏观背景（{LAYER_BACKGROUND}）：{out_background}（{len(background)} 行）")
    print(f"[import] 事件层（{LAYER_EVENT}）：{out_flash}（{len(event)} 行）")
    print(f"[import] 元数据：{meta_out}")
    return 0 if (opinion or background or event) else 3


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
