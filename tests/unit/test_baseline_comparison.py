"""基线对比脚本的单元测试（用合成明细覆盖统计口径，**不依赖真实产物、不碰数据库**）。

覆盖重点：
1. 对齐校验：金标准不一致 / 缺格子 → 记为问题（不静默比较）；
2. 指标口径：准确率分母 = 金标准有值格（`docs/08 §5`），可分层（人工子集 / 全量）；
3. 逐格变化：修复数（正则未命中→LLM 命中）与回归数（正则命中→LLM 未命中）；
4. 报告渲染：含提升百分点、PASS/FAIL 判定、Token 台账小节；
5. CLI：输入缺失/口径问题返回 2；`--dry-run` 不写文件；正常路径写出报告；
6. 红线：脚本不得触碰数据库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.build_ground_truth import SOURCE_CONSENSUS, SOURCE_HUMAN
from scripts.compare_extractor_baselines import (
    Comparison,
    FieldStat,
    field_stats,
    join_runs,
    main,
    read_eval_csv,
    read_gold_cells,
    read_texts,
    render_report,
)

pytestmark = pytest.mark.unit

COLUMNS = (
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


def _write(path: Path, rows: list[tuple[str, str, str, str, str, str, str]]) -> Path:
    """写合成明细（post_id, field, gold_value, gold_source, pred_value, outcome, note）。"""
    path.write_text(
        ",".join(COLUMNS)
        + "\n"
        + "\n".join(
            ",".join((post_id, field, gold, source, "", pred, "", outcome, note))
            for post_id, field, gold, source, pred, outcome, note in rows
        )
        + "\n",
        encoding="utf-8-sig",
    )
    return path


def _regex_rows() -> list[tuple[str, str, str, str, str, str, str]]:
    return [
        ("p1", "stop_loss", "2420", SOURCE_HUMAN, "", "missed", ""),
        ("p1", "take_profit", "2450", SOURCE_HUMAN, "2400", "wrong_value", "差值 50"),
        ("p1", "information_type", "TECHNICAL", SOURCE_HUMAN, "TECHNICAL", "correct_value", ""),
        ("p2", "stance", "LONG", SOURCE_HUMAN, "LONG", "correct_value", ""),
        ("p2", "stop_loss", "2410", SOURCE_HUMAN, "", "missed", ""),
    ]


def _llm_rows() -> list[tuple[str, str, str, str, str, str, str]]:
    return [
        ("p1", "stop_loss", "2420", SOURCE_HUMAN, "2420", "correct_value", ""),
        ("p1", "take_profit", "2450", SOURCE_HUMAN, "2450", "correct_value", ""),
        ("p1", "information_type", "TECHNICAL", SOURCE_HUMAN, "SENTIMENT", "wrong_value", ""),
        ("p2", "stance", "LONG", SOURCE_HUMAN, "LONG", "correct_value", ""),
        ("p2", "stop_loss", "2410", SOURCE_HUMAN, "2410", "correct_value", ""),
    ]


def _analysis(tmp_path: Path) -> tuple[Comparison, Path, Path]:
    regex_path = _write(tmp_path / "regex.csv", _regex_rows())
    llm_path = _write(tmp_path / "llm.csv", _llm_rows())
    return join_runs(read_eval_csv(regex_path), read_eval_csv(llm_path)), regex_path, llm_path


def test_read_eval_csv_rejects_duplicate_cells(tmp_path: Path) -> None:
    path = _write(tmp_path / "dup.csv", [*_regex_rows(), _regex_rows()[0]])

    with pytest.raises(ValueError, match="重复格子"):
        read_eval_csv(path)


def test_join_runs_flags_gold_mismatch(tmp_path: Path) -> None:
    regex_path = _write(tmp_path / "regex.csv", _regex_rows())
    llm_rows = _llm_rows()
    llm_rows[0] = ("p1", "stop_loss", "9999", SOURCE_HUMAN, "9999", "correct_value", "")
    llm_path = _write(tmp_path / "llm.csv", llm_rows)

    comparison = join_runs(read_eval_csv(regex_path), read_eval_csv(llm_path))

    assert any("金标准不一致" in problem for problem in comparison.problems)
    assert ("p1", "stop_loss") not in comparison.pairs  # 不一致的格子不进统计


def test_join_runs_flags_missing_cells(tmp_path: Path) -> None:
    regex_path = _write(tmp_path / "regex.csv", _regex_rows())
    llm_path = _write(tmp_path / "llm.csv", _llm_rows()[:-1])

    comparison = join_runs(read_eval_csv(regex_path), read_eval_csv(llm_path))

    assert any("LLM 明细缺少格子" in problem for problem in comparison.problems)


def test_field_stats_human_subset_and_full_scope(tmp_path: Path) -> None:
    comparison, _, _ = _analysis(tmp_path)
    comparison.pairs[("p3", "stance")] = (
        {
            "gold_value": "SHORT",
            "gold_source": SOURCE_CONSENSUS,
            "pred_value": "SHORT",
            "outcome": "correct_value",
        },
        {
            "gold_value": "SHORT",
            "gold_source": SOURCE_CONSENSUS,
            "pred_value": "SHORT",
            "outcome": "correct_value",
        },
    )

    human = field_stats(comparison, side="llm")
    full = field_stats(comparison, side="llm", source=None)

    assert human["stop_loss"].gold_present == 2 and human["stop_loss"].hit == 2
    assert full["stance"].cells == 2  # 人工 1 + 共识 1
    assert human["stance"].cells == 1


def test_fixed_and_regressed_cells(tmp_path: Path) -> None:
    comparison, _, _ = _analysis(tmp_path)

    fixed = comparison.fixed_by_llm()

    assert ("p1", "stop_loss") in fixed and ("p1", "take_profit") in fixed
    assert ("p2", "stop_loss") in fixed
    assert comparison.regressed_by_llm() == [("p1", "information_type")]


def test_field_stat_ratios() -> None:
    stat = FieldStat(name="stop_loss", cells=3, gold_present=2, hit=2, wrong=1)

    assert stat.accuracy == pytest.approx(1.0)
    assert stat.precision == pytest.approx(2 / 3)
    assert FieldStat(name="x").accuracy == 0.0
    assert FieldStat(name="x").precision == 0.0
    assert FieldStat(name="x").exact == 0.0


def test_render_report_contains_deltas_verdicts_and_usage(tmp_path: Path) -> None:
    comparison, regex_path, llm_path = _analysis(tmp_path)

    report = render_report(
        comparison,
        regex_path=regex_path,
        llm_path=llm_path,
        gold_path=tmp_path / "gold.csv",
        gold_digest="deadbeefdeadbeef",
        generated_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        usage={"prompt_tokens": 100},
    )

    assert "基线对比报告" in report
    assert "deadbeefdeadbeef" in report
    assert "**+100.0 pp**" in report  # 止损 0% → 100%
    assert "PASS" in report and "FAIL" in report
    assert "修复（正则未命中 → LLM 命中）：3 格" in report
    assert "回归（正则命中 → LLM 未命中）：1 格" in report
    assert "`prompt_tokens`" in report


def test_render_report_without_usage_says_so(tmp_path: Path) -> None:
    comparison, regex_path, llm_path = _analysis(tmp_path)

    report = render_report(
        comparison,
        regex_path=regex_path,
        llm_path=llm_path,
        gold_path=tmp_path / "gold.csv",
        gold_digest="x",
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        usage=None,
    )

    assert "未提供 LLM 台账" in report


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    _, regex_path, llm_path = _analysis(tmp_path)

    exit_code = main(
        [
            "--regex",
            str(regex_path),
            "--llm",
            str(llm_path),
            "--usage",
            str(tmp_path / "no_usage.json"),
            "--report",
            str(tmp_path / "report.md"),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert not (tmp_path / "report.md").exists()
    assert "正则" in capsys.readouterr().out


def test_gold_adoption_section_reports_model_correction(tmp_path: Path) -> None:
    """5.1「模型纠正人工」：金标准被改判 + LLM 是否已对齐。"""
    comparison, _, _ = _analysis(tmp_path)
    gold_before = {
        ("p1", "information_type"): {"value": "SENTIMENT", "source": SOURCE_HUMAN},
    }
    gold_now = {
        ("p1", "information_type"): {
            "value": "TECHNICAL",
            "source": SOURCE_HUMAN,
            "reviewer": "chenxiangxie",
            "note": "二次裁决：操作优先 → TECHNICAL",
        }
    }

    report = render_report(
        comparison,
        regex_path=tmp_path / "regex.csv",
        llm_path=tmp_path / "llm.csv",
        gold_path=tmp_path / "gold.csv",
        gold_digest="x",
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        gold_before=gold_before,
        gold_now=gold_now,
        texts={"p1": "黄金 2380 做多，止损 2365"},
    )

    assert "人工 ↔ 模型 对齐案例" in report
    assert "模型纠正人工" in report
    assert "`SENTIMENT` → `TECHNICAL`" in report
    assert "chenxiangxie" in report
    assert "原文：黄金 2380 做多" in report


def test_gold_adoption_section_skipped_without_history(tmp_path: Path) -> None:
    comparison, _, _ = _analysis(tmp_path)

    report = render_report(
        comparison,
        regex_path=tmp_path / "regex.csv",
        llm_path=tmp_path / "llm.csv",
        gold_path=tmp_path / "gold.csv",
        gold_digest="x",
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert "未提供改判前金标准" in report


def test_model_error_section_lists_human_corrected_cells(tmp_path: Path) -> None:
    """5.2「人工纠正模型」：金标准维持、模型判错的格子（带原文）。"""
    comparison, _, _ = _analysis(tmp_path)

    report = render_report(
        comparison,
        regex_path=tmp_path / "regex.csv",
        llm_path=tmp_path / "llm.csv",
        gold_path=tmp_path / "gold.csv",
        gold_digest="x",
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        texts={"p1": "如果跌破 2380 则转空"},
    )

    assert "人工纠正模型" in report
    assert "金标准 `TECHNICAL` ↔ 模型 `SENTIMENT`" in report
    assert "原文：如果跌破 2380 则转空" in report


def test_read_gold_cells_and_read_texts(tmp_path: Path) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "post_id,field,value,source,reviewer,note\n"
        "p1,stance,LONG,human-adjudicated,me,ok\n",
        encoding="utf-8-sig",
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text(
        "post_id,text_content,has_media\np1,黄金做多,false\n", encoding="utf-8-sig"
    )

    assert read_gold_cells(gold)[("p1", "stance")]["value"] == "LONG"
    assert read_texts(texts_path) == {"p1": "黄金做多"}


def test_main_writes_report_and_reads_usage(tmp_path: Path) -> None:
    _, regex_path, llm_path = _analysis(tmp_path)
    (tmp_path / "usage.json").write_text('{"usage": {"prompt_tokens": 42}}', encoding="utf-8")
    report_path = tmp_path / "report.md"

    exit_code = main(
        [
            "--regex",
            str(regex_path),
            "--llm",
            str(llm_path),
            "--usage",
            str(tmp_path / "usage.json"),
            "--gold",
            str(tmp_path / "gold.csv"),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    text = report_path.read_text(encoding="utf-8")
    assert "`prompt_tokens`" in text
    assert "（未知）" in text  # 金标准缺失时如实标注摘要


def test_main_returns_2_on_missing_input(tmp_path: Path, capsys) -> None:
    exit_code = main(["--regex", str(tmp_path / "nope.csv")])

    assert exit_code == 2
    assert "输入文件不存在" in capsys.readouterr().err


def test_main_returns_2_on_gold_mismatch(tmp_path: Path, capsys) -> None:
    regex_path = _write(tmp_path / "regex.csv", _regex_rows())
    llm_rows = _llm_rows()
    llm_rows[0] = ("p1", "stop_loss", "9999", SOURCE_HUMAN, "9999", "correct_value", "")
    llm_path = _write(tmp_path / "llm.csv", llm_rows)

    exit_code = main(["--regex", str(regex_path), "--llm", str(llm_path)])

    assert exit_code == 2
    assert "口径问题" in capsys.readouterr().err


def test_script_stays_off_database() -> None:
    import scripts.compare_extractor_baselines as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    for banned in ("sqlalchemy", "from database", "import database", "build_engine"):
        assert banned not in source, f"对比脚本不得触碰数据库：{banned}"

