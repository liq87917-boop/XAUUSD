"""`scripts/load_hf_benchmark.py` 单测（**零网络、零真实数据集**：假 fetcher + `tmp_path`）。

覆盖：
1. **字段映射**：`Price Direction Up/Down/Constant` → `LONG/SHORT/FLAT`；股吧 `label`
   0/1/2 → `SHORT/LONG/FLAT`（**原始标签不被改动，只留档**）；
2. **无标签行**：黄金数据集方向列全 0 → 默认跳过并记账；`--include-unlabeled` 才保留为 FLAT；
   未知股吧 label（如 7）→ 跳过，**不猜**；
3. **留空字段**：`horizon`/`stop_loss`/`take_profit` 显式留空（→ `correct_absent`/`spurious`）；
4. **取数**：分页够数即停、跳过计数正确；HTTP 非 200 明确报错（不静默产空基准）；
5. **落盘契约**：产物能被 `evaluate_extractor.load_gold` / `load_texts` 读回，并能直接跑
   `evaluate()`（用离线 `RegexOpinionExtractor`）；退出码 2 的冲突参数；
6. **红线**：`--dry-run` 不写任何文件；meta 里记录"原始标签未改动"与任务差异说明。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.evaluate_extractor import (
    NOT_EVALUATED_MARKER,
    OUTCOMES,
    RegexOpinionExtractor,
    evaluate,
    load_gold,
    load_texts,
)
from scripts.load_hf_benchmark import (
    GOLD_DATASET,
    GUBA_DATASET,
    NOT_EVALUATED_SCORING,
    SCORED_EMPTY_FIELDS,
    datasets_lib_available,
    fetch_rows_via_api,
    gather_mapped_rows,
    main,
    map_gold_dataset_row,
    map_guba_row,
    resolve_fetcher,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
def _gold_row(**overrides: Any) -> dict[str, Any]:
    """一条**结构真实**的黄金数据集行（字段名与 Hugging Face features 一致）。"""
    row: dict[str, Any] = {
        "Dates": "26-02-2017",
        "URL": "https://example.invalid/a",
        "News": "gold rises 1% on a weaker dollar",
        "Price Direction Up": 0,
        "Price Direction Constant": 0,
        "Price Direction Down": 1,
        "Asset Comparision": 0,
        "Past Information": 1,
        "Future Information": 0,
        "Price Sentiment": "negative",
    }
    row.update(overrides)
    return row


def _guba_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"title": "看多黄金，逢低做多", "label": 1}
    row.update(overrides)
    return row


#: 方向三列全 0（数据集里真实存在的"无方向标签"行）
_NO_DIRECTION: dict[str, Any] = {
    "Price Direction Up": 0,
    "Price Direction Constant": 0,
    "Price Direction Down": 0,
}


def _fake_fetcher(
    pages: dict[str, dict[int, list[dict[str, Any]]]], calls: list[tuple[str, int, int]]
):
    """按 (dataset, offset) 返回预置页；记录调用以便断言"够数即停"。"""

    def fetch(
        dataset: str, *, config: str, split: str, offset: int, length: int
    ) -> list[dict[str, Any]]:
        calls.append((dataset, offset, length))
        return pages.get(dataset, {}).get(offset, [])

    return fetch


# ---------------------------------------------------------------------------
# 黄金数据集映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("Price Direction Up", "LONG"),
        ("Price Direction Down", "SHORT"),
        ("Price Direction Constant", "FLAT"),
    ],
)
def test_gold_direction_maps_to_stance(field_name: str, expected: str) -> None:
    """方向三列 → stance；原始列值全部留档（**不改数据集标签**）。"""
    flags = {
        "Price Direction Up": 0,
        "Price Direction Constant": 0,
        "Price Direction Down": 0,
        field_name: 1,
    }
    text, gold, problems = map_gold_dataset_row(
        _gold_row(**flags), position=7, information_type="SENTIMENT"
    )
    assert problems == []
    assert text is not None and text.post_id == "hf-gold-00007"
    stance_cell = next(cell for cell in gold if cell.field_name == "stance")
    assert stance_cell.value == expected
    assert stance_cell.value_raw == f"{field_name}=1"
    # 原始标签留档：值原样复制（未被改写）
    assert text.raw["Price Direction Up"] == flags["Price Direction Up"]
    assert text.raw["Price Direction Down"] == flags["Price Direction Down"]
    assert text.raw["Price Sentiment"] == "negative"
    assert text.text_content == "gold rises 1% on a weaker dollar"


def test_gold_without_direction_is_skipped_by_default() -> None:
    """方向列全 0（数据集中确实存在）→ 默认跳过，并给出可追溯原因。"""
    text, gold, problems = map_gold_dataset_row(
        _gold_row(**_NO_DIRECTION),
        position=1,
        information_type="SENTIMENT",
    )
    assert text is None and gold == []
    assert problems and "无方向标签" in problems[0]


def test_include_unlabeled_maps_to_flat() -> None:
    """显式开启 --include-unlabeled 后，无方向标签行按 FLAT 保留。"""
    text, gold, problems = map_gold_dataset_row(
        _gold_row(**_NO_DIRECTION),
        position=1,
        information_type="SENTIMENT",
        unlabeled_stance="FLAT",
    )
    assert problems == []
    assert text is not None
    stance_cell = next(cell for cell in gold if cell.field_name == "stance")
    assert stance_cell.value == "FLAT" and stance_cell.value_raw == "no-direction-flag=1"


def test_absent_fields_are_explicit_empty_cells() -> None:
    """标题级数据没有点位/周期 → 显式空行（评估器据此判 correct_absent / spurious）。"""
    _, gold, _ = map_gold_dataset_row(_gold_row(), position=0, information_type="SENTIMENT")
    empties = {
        cell.field_name: cell.value for cell in gold if cell.field_name in SCORED_EMPTY_FIELDS
    }
    assert set(empties) == set(SCORED_EMPTY_FIELDS)
    assert all(value == "" for value in empties.values())


def test_information_type_mapping_can_be_disabled() -> None:
    """`--information-type empty`：字段**仍产出**（便于观察模型给值），但标记为未评估。"""
    _, gold_with, _ = map_gold_dataset_row(_gold_row(), position=0, information_type="SENTIMENT")
    _, gold_without, _ = map_gold_dataset_row(_gold_row(), position=0, information_type="")
    assert [cell.field_name for cell in gold_with] == [
        "stance",
        "information_type",
        *SCORED_EMPTY_FIELDS,
    ]
    assert [cell.field_name for cell in gold_without] == [
        "stance",
        "information_type",
        *SCORED_EMPTY_FIELDS,
    ]
    scored = gold_with[1]
    assert scored.value == "SENTIMENT" and scored.scoring == ""
    unscored = gold_without[1]
    assert unscored.value == "" and unscored.scoring == NOT_EVALUATED_SCORING
    # 跨脚本**字符串契约**：加载器的标记必须与评估器的完全一致
    assert unscored.scoring == NOT_EVALUATED_MARKER
    assert "不评分" in unscored.note


def test_gold_row_without_news_is_skipped() -> None:
    """标题为空的行跳过（不产出无文本的基准条目）。"""
    text, gold, problems = map_gold_dataset_row(
        _gold_row(News="   "), position=3, information_type="SENTIMENT"
    )
    assert text is None and gold == [] and "News 为空" in problems[0]


# ---------------------------------------------------------------------------
# 中文股吧标题映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("label", "expected"), [(0, "SHORT"), (1, "LONG"), (2, "FLAT")])
def test_guba_label_maps_to_stance(label: int, expected: str) -> None:
    """卡片口径 0=negative/1=positive/2=neutral → SHORT/LONG/FLAT（原 label 留档）。"""
    text, gold, problems = map_guba_row(
        _guba_row(label=label), position=2, information_type="SENTIMENT"
    )
    assert problems == []
    assert text is not None and text.post_id == "hf-guba-00002"
    assert text.raw["label"] == label  # 原始标签未被改动
    stance_cell = next(cell for cell in gold if cell.field_name == "stance")
    assert stance_cell.value == expected and stance_cell.value_raw == f"label={label}"
    assert "docs/10 §4.1.5" in stance_cell.note  # 口径差异必须留痕


def test_guba_unknown_label_is_skipped_without_guessing() -> None:
    """未知 label（如 7）或空标题 → 跳过，**不猜**。"""
    text, gold, problems = map_guba_row(
        _guba_row(label=7), position=4, information_type="SENTIMENT"
    )
    assert text is None and gold == [] and "不在 0/1/2 内" in problems[0]

    text, gold, problems = map_guba_row(
        _guba_row(title="  "), position=4, information_type="SENTIMENT"
    )
    assert text is None and "title 为空" in problems[0]

    text, gold, problems = map_guba_row(
        _guba_row(label="bullish"), position=4, information_type="SENTIMENT"
    )
    assert text is None and "非整数" in problems[0]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def test_gather_stops_when_enough_rows_and_counts_skips() -> None:
    """够数即停：一轮分页取满即结束，且跳过（无标签）行被计数。"""
    calls: list[tuple[str, int, int]] = []
    page = [
        _gold_row(**_NO_DIRECTION),  # 无方向标签 → 跳过
        _gold_row(),
        _gold_row(**{"Price Direction Down": 0, "Price Direction Up": 1}),
        _gold_row(**{"Price Direction Down": 0, "Price Direction Constant": 1}),
    ]
    fetcher = _fake_fetcher({GOLD_DATASET: {0: page}}, calls)
    benchmark = gather_mapped_rows(
        fetcher,
        map_gold_dataset_row,
        dataset=GOLD_DATASET,
        rows_needed=3,
        information_type="SENTIMENT",
    )
    assert [text.post_id for text in benchmark.texts] == [
        "hf-gold-00001",
        "hf-gold-00002",
        "hf-gold-00003",
    ]
    assert benchmark.stats["rows_skipped"] == 1
    assert benchmark.stats["rows_taken"] == 3
    assert benchmark.stats["rows_scanned"] == 4
    assert len(calls) == 1  # 只请求了一页，不再多拉
    assert any("无方向标签" in problem for problem in benchmark.problems)


def test_fetch_rows_via_api_rejects_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 非 200 → 明确抛错（不允许静默产出空基准）。"""

    class _Response:
        status_code = 404
        text = "no such dataset"

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Response())
    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch_rows_via_api(GOLD_DATASET, config="default", split="test", offset=0, length=5)


def test_fetch_rows_via_api_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功路径：从 `rows[].row` 取出原始行。"""

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "rows": [{"row_idx": 0, "row": _gold_row()}, {"row_idx": 1, "row": _guba_row()}]
            }

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Response())
    rows = fetch_rows_via_api(GOLD_DATASET, config="default", split="test", offset=0, length=2)
    assert len(rows) == 2 and rows[0]["News"] == _gold_row()["News"]


def test_resolve_fetcher_prefers_datasets_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`auto`：装了 datasets 用它；没装则回落只读 HTTP 接口（本 venv 现状）。"""
    assert resolve_fetcher("api")[1] == "datasets-server-api"
    assert resolve_fetcher("datasets")[1] == "datasets-lib"
    monkeypatch.setattr("scripts.load_hf_benchmark.datasets_lib_available", lambda: True)
    assert resolve_fetcher("auto")[1] == "datasets-lib"
    monkeypatch.setattr("scripts.load_hf_benchmark.datasets_lib_available", lambda: False)
    assert resolve_fetcher("auto")[1] == "datasets-server-api"
    assert datasets_lib_available() is False  # 当前环境未安装（脚本因此默认走 API）


# ---------------------------------------------------------------------------
# CLI / 落盘契约
# ---------------------------------------------------------------------------
ALIGNED_PAGES: dict[str, dict[int, list[dict[str, Any]]]] = {
    GOLD_DATASET: {
        0: [
            _gold_row(),  # Down=1 → SHORT
            _gold_row(**{"Price Direction Down": 0, "Price Direction Up": 1}),  # → LONG
        ]
    },
    GUBA_DATASET: {
        0: [_guba_row(label=0), _guba_row(label=2)],  # → SHORT / FLAT
    },
}


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch, pages: dict[str, dict[int, list[dict[str, Any]]]]
) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []
    fake = _fake_fetcher(pages, calls)
    monkeypatch.setattr(
        "scripts.load_hf_benchmark.resolve_fetcher", lambda source: (fake, "fake-fetcher")
    )
    return calls


def test_main_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """默认/显式 `--dry-run`：一条文件都不写（红线）。"""
    _patch_fetcher(monkeypatch, ALIGNED_PAGES)
    prefix = tmp_path / "out" / "hf_benchmark"
    code = main(["--dry-run", "--gold-rows", "2", "--guba-rows", "2", "--out-prefix", str(prefix)])
    assert code == 0
    assert list(tmp_path.rglob("*.csv")) == []
    assert list(tmp_path.rglob("*.json")) == []


def test_main_writes_reader_compatible_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """落盘产物必须能被 `evaluate_extractor` 读回并直接跑评估（离线正则抽取器）。"""
    _patch_fetcher(monkeypatch, ALIGNED_PAGES)
    prefix = tmp_path / "hf_benchmark"
    code = main(
        ["--no-dry-run", "--gold-rows", "2", "--guba-rows", "2", "--out-prefix", str(prefix)]
    )
    assert code == 0
    texts_path = tmp_path / "hf_benchmark_texts.csv"
    gold_path = tmp_path / "hf_benchmark_gold.csv"
    meta_path = tmp_path / "hf_benchmark_meta.json"
    assert texts_path.exists() and gold_path.exists() and meta_path.exists()

    posts = load_texts(texts_path)
    assert set(posts) == {
        "hf-gold-00000",
        "hf-gold-00001",
        "hf-guba-00000",
        "hf-guba-00001",
    }
    assert posts["hf-gold-00000"].text == "gold rises 1% on a weaker dollar"
    assert posts["hf-guba-00000"].has_media is False

    gold, problems = load_gold(gold_path)
    assert problems == []
    assert gold["hf-gold-00000"]["stance"].value == "SHORT"
    assert gold["hf-gold-00001"]["stance"].value == "LONG"
    assert gold["hf-guba-00000"]["stance"].value == "SHORT"
    assert gold["hf-guba-00001"]["stance"].value == "FLAT"
    assert gold["hf-gold-00000"]["information_type"].value == "SENTIMENT"
    for cells in gold.values():  # 标题级数据：点位/周期显式留空
        assert cells["horizon"].value == ""
        assert cells["stop_loss"].value == ""
        assert cells["take_profit"].value == ""

    stats = evaluate(gold, posts, RegexOpinionExtractor(), fields=("stance",))
    assert stats.posts_total == 4
    assert stats.cells and {cell.outcome for cell in stats.cells} <= set(OUTCOMES)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["original_labels_untouched"] is True
    assert meta["label_mapping"]["guba"]["label=0"] == "SHORT"
    assert meta["stats"]["gold"]["rows_taken"] == 2
    assert any("标题级情感" in caveat for caveat in meta["caveats"])
    assert any("上证50ETF" in caveat for caveat in meta["caveats"])


def test_main_rejects_conflicting_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--dry-run` 与 `--no-dry-run` 同时给出 → 退出码 2，不取数不写盘。"""
    calls = _patch_fetcher(monkeypatch, ALIGNED_PAGES)
    code = main(["--dry-run", "--no-dry-run", "--out-prefix", str(tmp_path / "x")])
    assert code == 2
    assert calls == []


def test_main_reports_fetch_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """取数失败 → 退出码 2，且不留下半成品文件。"""

    def boom(
        dataset: str, *, config: str, split: str, offset: int, length: int
    ) -> list[dict[str, Any]]:
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(
        "scripts.load_hf_benchmark.resolve_fetcher", lambda source: (boom, "fake-fetcher")
    )
    code = main(
        [
            "--no-dry-run",
            "--gold-rows",
            "1",
            "--guba-rows",
            "1",
            "--out-prefix",
            str(tmp_path / "y"),
        ]
    )
    assert code == 2
    assert list(tmp_path.rglob("*.csv")) == []


def test_texts_csv_keeps_all_original_label_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**不改数据集标签**：原始列（含 Dates/URL/Price*/Asset Comparision）逐条留档在 texts.csv。"""
    _patch_fetcher(monkeypatch, ALIGNED_PAGES)
    prefix = tmp_path / "hf_benchmark"
    assert (
        main(["--no-dry-run", "--gold-rows", "2", "--guba-rows", "2", "--out-prefix", str(prefix)])
        == 0
    )
    with (tmp_path / "hf_benchmark_texts.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    gold_rows = [row for row in rows if row["source"] == "hf:saguaro-gold"]
    assert len(gold_rows) == 2
    # 原始标签逐行留档（第 1 行 Down=1、第 2 行 Up=1，均未被改写）
    assert [row["raw_Price Direction Down"] for row in gold_rows] == ["1", "0"]
    assert [row["raw_Price Direction Up"] for row in gold_rows] == ["0", "1"]
    for row in gold_rows:
        assert row["raw_Price Sentiment"] == "negative"
        assert row["raw_Dates"] == "26-02-2017"
        assert row["raw_URL"] == "https://example.invalid/a"
    guba_rows = [row for row in rows if row["source"] == "hf:eastmoney-guba"]
    assert [row["raw_label"] for row in guba_rows] == ["0", "2"]


def test_main_empty_information_type_is_marked_not_evaluated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--information-type empty` 落盘契约：`value` 留空 + `scoring=NOT_EVALUATED`。"""
    _patch_fetcher(monkeypatch, ALIGNED_PAGES)
    prefix = tmp_path / "hf_benchmark"
    code = main(
        [
            "--no-dry-run",
            "--information-type",
            "empty",
            "--gold-rows",
            "2",
            "--guba-rows",
            "2",
            "--out-prefix",
            str(prefix),
        ]
    )
    assert code == 0
    gold, problems = load_gold(tmp_path / "hf_benchmark_gold.csv")

    assert problems == [] and len(gold) == 4  # 4 条文本 × 5 字段
    for cells in gold.values():
        cell = cells["information_type"]
        assert cell.value == "" and cell.scoring == NOT_EVALUATED_SCORING
        assert cell.scoring == NOT_EVALUATED_MARKER  # 与评估器同一契约
        others = [item for name, item in cells.items() if name != "information_type"]
        assert all(not item.scoring for item in others)  # 其余字段照常评分
    # 原始标签仍留档（供报告里的"数据集标注存疑"分析）：黄金=Price Sentiment，股吧=label
    assert {cells["information_type"].raw for cells in gold.values()} == {
        "negative",
        "label=0",
        "label=2",
    }

    meta = json.loads((tmp_path / "hf_benchmark_meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == {"texts": 4, "gold_cells": 20, "not_evaluated_cells": 4}
