"""`scripts/import_manual_posts.py` 单测（**零网络、零真实数据**：全部在 tmp_path 上）。

覆盖：
1. **分流规则**：`source_type=EVENT` / `manual-` 前缀 / 正文 <200 字符 → 事件层；其余 → 观点语料；
2. **列归一**：别名映射（`content`→content、`source`→source、`published`→published_at）
   **且保留别名表外的列**（title/url/language/…）；
3. **硬校验**（退出码 2）：缺必需列 / 正文空 / 时间缺时区 /
   `effective_at < published_at` / 未来时间；
4. **去重**：同 url 或同正文指纹只保留一条；
5. **读入格式**：Excel（xlsx，含"被存成 .csv"的坑）自动识别；GBK CSV 告警后可读；
6. **红线**：`--dry-run` 不落盘；输出与输入同路径 → 拒绝；**输入文件永不被修改**。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.import_manual_posts import (
    LAYER_BACKGROUND,
    LAYER_EVENT,
    LAYER_OPINION,
    OPINION_MIN_CHARS,
    dedupe,
    main,
    mark_layer,
    normalize_rows,
    read_table,
    route_row,
    validate,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
LONG_BODY = "黄金" * 150  # 300 字符（≥200；无尾空格，便于断言）
SHORT_FLASH = "汇通网9月12日讯——现货黄金短线走高，关注4320阻力。"  # <200


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "id": "p1",
        "content": LONG_BODY,
        "source": "fred_blog",
        "published_at": "2026-09-10T13:00:00+00:00",
        "effective_at": "2026-09-10T13:00:00+00:00",
        "url": "https://example.invalid/p1",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 分流规则
# ---------------------------------------------------------------------------
def test_route_row_priority_order() -> None:
    """分层优先级：EVENT 标记 > 太短 > 手工快讯（观点语料）> 其余（宏观背景）。"""
    layer, reason = route_row(
        _row(source_type="EVENT"), manual_prefix="manual-", opinion_min_chars=OPINION_MIN_CHARS
    )
    assert layer == LAYER_EVENT and "source_type" in reason

    layer, reason = route_row(
        _row(content=SHORT_FLASH), manual_prefix="manual-", opinion_min_chars=OPINION_MIN_CHARS
    )
    assert layer == LAYER_EVENT and str(OPINION_MIN_CHARS) in reason

    layer, reason = route_row(
        _row(source="manual-汇通网"), manual_prefix="manual-", opinion_min_chars=OPINION_MIN_CHARS
    )
    assert layer == LAYER_OPINION and "观点语料" in reason

    layer, reason = route_row(_row(), manual_prefix="manual-", opinion_min_chars=OPINION_MIN_CHARS)
    assert layer == LAYER_BACKGROUND and "宏观背景" in reason


def test_mark_layer_sets_source_type_and_note() -> None:
    opinion = mark_layer(_row(source="manual-汇通网"), LAYER_OPINION)
    assert opinion["source_type"] == "NEWS"
    assert "opinion_corpus" in opinion["notes_collect"]

    background = mark_layer(_row(notes_collect="含图片"), LAYER_BACKGROUND)
    assert background["source_type"] == "EVENT"
    assert background["notes_collect"].startswith("含图片")
    assert "macro_background" in background["notes_collect"]

    event = mark_layer(_row(), LAYER_EVENT)
    assert event["source_type"] == "EVENT" and "event_layer" in event["notes_collect"]


# ---------------------------------------------------------------------------
# 列归一 + 校验 + 去重
# ---------------------------------------------------------------------------
def test_normalize_rows_maps_aliases_and_keeps_other_columns() -> None:
    rows = normalize_rows(
        [
            {
                "content": LONG_BODY,
                "source": "fred_blog",
                "published": "2026-09-10T13:00:00+00:00",
                "title": "t",
                "url": "u",
            }
        ]
    )
    assert rows[0]["content"] == LONG_BODY and rows[0]["source"] == "fred_blog"
    assert rows[0]["published_at"] == "2026-09-10T13:00:00+00:00"
    assert rows[0]["title"] == "t" and rows[0]["url"] == "u"  # 别名表外的列被保留


def test_normalize_rows_lowercases_boolean_columns() -> None:
    """Excel 往返会把 `has_media` 变成 `True/False`：必须归一为契约要求的小写。"""
    rows = normalize_rows([_row(has_media="True")])
    assert rows[0]["has_media"] == "true"
    rows = normalize_rows([_row(has_media="FALSE")])
    assert rows[0]["has_media"] == "false"


def test_validate_hard_failures() -> None:
    assert validate([_row()], now=NOW) == []
    assert any("content 为空" in e for e in validate([_row(content="   ")], now=NOW))
    assert any("缺少必需列" in e for e in validate([{"content": "x"}], now=NOW))
    assert any(
        "不可解析" in e for e in validate([_row(published_at="2026-09-10 13:00:00")], now=NOW)
    )
    assert any(
        "时间因果违规" in e
        for e in validate([_row(effective_at="2026-09-09T13:00:00+00:00")], now=NOW)
    )
    future = (NOW + timedelta(days=2)).isoformat()
    assert any(
        "未来时间" in e for e in validate([_row(published_at=future, effective_at=future)], now=NOW)
    )


def test_dedupe_by_url_and_content_fingerprint() -> None:
    kept, duplicates, _ = dedupe(
        [_row(), _row(id="p2"), _row(id="p3", url="other", content=LONG_BODY)]
    )
    assert len(kept) == 1 and duplicates == 2  # 同 url 与同正文指纹都算重复


# ---------------------------------------------------------------------------
# 读入格式：xlsx（含"被存成 .csv"）+ GBK
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: list[dict[str, str]], *, encoding: str = "utf-8-sig") -> Path:
    columns = ["id", "content", "source", "published_at", "effective_at", "url"]
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_xlsx(path: Path, rows: list[dict[str, str]]) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    columns = ["id", "content", "source", "published_at", "effective_at", "url"]
    sheet.append(columns)
    for row in rows:
        sheet.append([row.get(column, "") for column in columns])
    book.save(path)
    return path


def test_read_csv_and_gbk_fallback(tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path / "ok.csv", [_row()])
    rows, info = read_table(csv_path)
    assert info["format"] == "csv" and info["encoding"].startswith("utf-8") and len(rows) == 1

    gbk_path = _write_csv(
        tmp_path / "gbk.csv", [_row(content="汇通网快讯：" + LONG_BODY)], encoding="gbk"
    )
    rows, info = read_table(gbk_path)
    assert len(rows) == 1 and "warning" in info and "UTF-8" in str(info["warning"])


def test_read_xlsx_even_when_named_csv(tmp_path: Path) -> None:
    """回归：被 Excel 存成 `.csv` 扩展名的 xlsx（ZIP 魔数）必须能读出来。"""
    fake_csv = _write_xlsx(tmp_path / "looks_like.csv", [_row()])
    rows, info = read_table(fake_csv)

    assert info["format"] == "xlsx" and len(rows) == 1
    assert rows[0]["content"] == LONG_BODY


# ---------------------------------------------------------------------------
# CLI：dry-run 不落盘 / 落盘正确 / 输入只读 / 同路径拒绝
# ---------------------------------------------------------------------------
def test_cli_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    corpus = _write_csv(
        tmp_path / "corpus.csv",
        [
            _row(),  # fred_blog 长文 → 宏观背景
            _row(id="f1", content="汇通网快讯：" + LONG_BODY, source="manual-汇通网", url="u2"),
            _row(id="f2", content=SHORT_FLASH, source="manual-汇通网", url="u3"),
        ],
    )
    out_corpus = tmp_path / "opinion.csv"
    out_background = tmp_path / "background.csv"
    out_flash = tmp_path / "flash.csv"

    code = main(
        [
            "--corpus",
            str(corpus),
            "--out-corpus",
            str(out_corpus),
            "--out-background",
            str(out_background),
            "--out-flash",
            str(out_flash),
            "--dry-run",
        ]
    )

    assert code == 0
    assert not any(path.exists() for path in (out_corpus, out_background, out_flash))
    printed = capsys.readouterr().out
    assert "观点语料 1 / 宏观背景 1 / 事件层 1" in printed
    assert "--dry-run：未写任何文件" in printed


def test_cli_writes_outputs_and_never_touches_inputs(tmp_path: Path) -> None:
    corpus = _write_csv(
        tmp_path / "corpus.csv",
        [
            _row(),
            _row(id="f1", content="汇通网快讯：" + LONG_BODY, source="manual-汇通网", url="u2"),
            _row(id="f2", content=SHORT_FLASH, source="manual-汇通网", url="u3"),
        ],
    )
    before = corpus.read_bytes()
    out_corpus = tmp_path / "opinion.csv"
    out_background = tmp_path / "background.csv"
    out_flash = tmp_path / "flash.csv"
    meta = tmp_path / "import.meta.json"

    code = main(
        [
            "--corpus",
            str(corpus),
            "--out-corpus",
            str(out_corpus),
            "--out-background",
            str(out_background),
            "--out-flash",
            str(out_flash),
            "--meta-out",
            str(meta),
            "--no-dry-run",
        ]
    )

    assert code == 0
    assert corpus.read_bytes() == before  # 输入只读
    with out_corpus.open(encoding="utf-8-sig", newline="") as handle:
        opinion_rows = list(csv.DictReader(handle))
    with out_background.open(encoding="utf-8-sig", newline="") as handle:
        background_rows = list(csv.DictReader(handle))
    with out_flash.open(encoding="utf-8-sig", newline="") as handle:
        flash_rows = list(csv.DictReader(handle))

    assert len(opinion_rows) == 1 and opinion_rows[0]["source_type"] == "NEWS"
    assert "opinion_corpus" in opinion_rows[0]["notes_collect"]
    assert len(background_rows) == 1 and background_rows[0]["source_type"] == "EVENT"
    assert "macro_background" in background_rows[0]["notes_collect"]
    assert len(flash_rows) == 1 and flash_rows[0]["source_type"] == "EVENT"
    assert "event_layer" in flash_rows[0]["notes_collect"]

    payload = json.loads(meta.read_text(encoding="utf-8"))
    assert payload["opinion_rows"] == 1
    assert payload["background_rows"] == 1 and payload["event_rows"] == 1
    assert payload["inputs"][0]["sha256"]


def test_cli_refuses_output_equal_to_input(tmp_path: Path, capsys) -> None:
    corpus = _write_csv(tmp_path / "corpus.csv", [_row()])
    code = main(
        [
            "--corpus",
            str(corpus),
            "--out-corpus",
            str(corpus),
            "--out-flash",
            str(tmp_path / "f.csv"),
        ]
    )
    assert code == 2
    assert "输出路径与输入相同" in capsys.readouterr().err


def test_cli_hard_failure_returns_2(tmp_path: Path, capsys) -> None:
    corpus = _write_csv(tmp_path / "bad.csv", [_row(content="")])
    code = main(
        [
            "--corpus",
            str(corpus),
            "--out-corpus",
            str(tmp_path / "o.csv"),
            "--out-flash",
            str(tmp_path / "f.csv"),
        ]
    )
    assert code == 2
    assert "硬校验失败" in capsys.readouterr().err
