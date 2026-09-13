"""评估脚本的单元测试（用**假抽取器**覆盖统计逻辑，不依赖真实抽取器的行为）。

覆盖重点：
1. 五类判定（命中 / 双方未给出 / **该判未判** / **提取错误** / **不该判却判**）互斥且穷尽；
2. `predict_cell` 的字段映射（含 `drafts` 为空、`Decimal` 归一化）；
3. 指标口径：准确率（金标准有值为分母）、精确率、召回率、严格全对率、各失败率；
4. 分层统计（人工子集 vs 模型共识子集）；
5. 混淆矩阵（行=金标准，列=抽取器，含"未给出"列）；
6. 质量指标：无观点率、UNKNOWN 率、confidence 非法、诊断码计数、点位差值档位；
7. CLI 端到端（用真实抽取器跑合成小样本）+ 不碰数据库的红线。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from database.models.enums import OpinionStance
from scripts.build_ground_truth import SOURCE_CONSENSUS, SOURCE_HUMAN
from scripts.evaluate_extractor import (
    DEFAULT_EVAL_OUT_LLM,
    DEFAULT_EVAL_OUT_REGEX,
    DEFAULT_REPORT_LLM,
    DEFAULT_REPORT_REGEX,
    EVAL_COLUMNS,
    OUTCOME_CORRECT_ABSENT,
    OUTCOME_CORRECT_VALUE,
    OUTCOME_MISSED,
    OUTCOME_SPURIOUS,
    OUTCOME_WRONG,
    GoldCell,
    PostText,
    aggregate,
    classify_outcome,
    confusion_matrix,
    evaluate,
    field_metrics,
    label_order,
    load_gold,
    load_texts,
    main,
    predict_cell,
    select_posts,
    text_cues,
    write_eval_csv,
)
from src.processors.llm_client import LLMError
from src.processors.opinion_extractor import (
    DiagnosticCode,
    ExtractionDiagnostic,
    OpinionExtractionResult,
)
from src.processors.regex_extractor import RegexOpinionExtractor
from src.processors.schemas import AuthorOpinionDraft

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 假抽取器
# ---------------------------------------------------------------------------
class StubExtractor:
    """按文本前缀返回预设结果（`NO_OPINION…` → 无观点；`MULTI…` → 两条观点）。"""

    parser_version = "stub-v1"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        self.seen.append(text)
        if text.startswith("NO_OPINION"):
            return OpinionExtractionResult(
                self.parser_version,
                (),
                (ExtractionDiagnostic(DiagnosticCode.NO_STANCE_KEYWORD, "无方向词", None),),
                (),
            )
        if text.startswith("MULTI"):
            draft = AuthorOpinionDraft(stance="LONG", parser_version=self.parser_version)
            return OpinionExtractionResult(self.parser_version, (draft, draft), (), ())
        if text.startswith("INVALID_CONF"):
            # 真实 schema 会拒绝越界 confidence（校验层保护），这里绕过校验专门测评估器的兜底统计
            draft = AuthorOpinionDraft.model_construct(
                stance=OpinionStance.LONG,
                confidence=Decimal("1.5"),
                parser_version=self.parser_version,
            )
            return OpinionExtractionResult(self.parser_version, (draft,), (), ())
        draft = AuthorOpinionDraft(
            stance="SHORT",
            horizon="1d",
            stop_loss=Decimal("2420.00"),
            take_profit=Decimal("2292"),
            entry_low=Decimal("2300"),
            information_type="TECHNICAL",
            confidence=Decimal("0.9"),
            parser_version=self.parser_version,
        )
        return OpinionExtractionResult(
            self.parser_version, (draft,), (), ("标的 XAUUSD 不在白名单内：需先补种子",)
        )


def _gold(rows: list[tuple[str, str, str, str]]) -> dict[str, dict[str, GoldCell]]:
    gold: dict[str, dict[str, GoldCell]] = {}
    for post_id, field_name, value, source in rows:
        gold.setdefault(post_id, {})[field_name] = GoldCell(
            post_id=post_id, field_name=field_name, value=value, source=source, raw=value
        )
    return gold


def _texts(mapping: dict[str, str]) -> dict[str, PostText]:
    return {
        post_id: PostText(post_id=post_id, text=text, has_media=False)
        for post_id, text in mapping.items()
    }


# ---------------------------------------------------------------------------
# 1) 判定与预测
# ---------------------------------------------------------------------------
def test_classify_outcome_five_buckets() -> None:
    assert classify_outcome("LONG", "LONG") == OUTCOME_CORRECT_VALUE
    assert classify_outcome("", "") == OUTCOME_CORRECT_ABSENT
    assert classify_outcome("LONG", "") == OUTCOME_MISSED  # 该判未判
    assert classify_outcome("LONG", "SHORT") == OUTCOME_WRONG  # 提取错误
    assert classify_outcome("", "LONG") == OUTCOME_SPURIOUS  # 不该判却判


def test_predict_cell_no_drafts_means_absent() -> None:
    assert predict_cell(OpinionExtractionResult("stub"), "stance") == ("", "")


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("stance", "SHORT"),
        ("horizon", "1D"),
        ("information_type", "TECHNICAL"),
        ("stop_loss", "2420"),
        ("take_profit", "2292"),
        ("entry_low", "2300"),
    ],
)
def test_predict_cell_maps_fields_and_normalizes(field_name: str, expected: str) -> None:
    value, _raw = predict_cell(StubExtractor().extract("普通文本"), field_name)

    assert value == expected


def test_predict_cell_rejects_unknown_field() -> None:
    with pytest.raises(KeyError, match="未知字段"):
        predict_cell(StubExtractor().extract("普通文本"), "rationale")


# ---------------------------------------------------------------------------
# 2) 读入
# ---------------------------------------------------------------------------
def test_load_gold_skips_fields_without_gold(tmp_path: Path) -> None:
    path = tmp_path / "gold.csv"
    path.write_text(
        "post_id,field,value,value_raw,source\n"
        f"p1,stance,LONG,LONG,{SOURCE_HUMAN}\n"
        f"p1,entry_low,2300,2300,{SOURCE_HUMAN}\n"
        f"p1,horizon,,,{SOURCE_CONSENSUS}\n",
        encoding="utf-8-sig",
    )

    gold, problems = load_gold(path)

    assert problems == []
    assert set(gold["p1"]) == {"stance", "horizon"}  # entry_low 不在评估字段里
    assert gold["p1"]["horizon"].value == ""


def test_load_gold_reports_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "gold.csv"
    path.write_text(
        "post_id,field,value,source\n"
        f"p1,stance,LONG,{SOURCE_HUMAN}\n"
        f"p1,stance,SHORT,{SOURCE_HUMAN}\n",
        encoding="utf-8",
    )

    gold, problems = load_gold(path)

    assert gold["p1"]["stance"].value == "LONG"
    assert len(problems) == 1 and "重复格子" in problems[0]


def test_load_texts_parses_has_media(tmp_path: Path) -> None:
    path = tmp_path / "texts.csv"
    path.write_text(
        "post_id,text_content,has_media\np1,黄金做多,true\np2,看图,0\n", encoding="utf-8"
    )

    texts = load_texts(path)

    assert texts["p1"].has_media is True
    assert texts["p2"].has_media is False
    assert texts["p2"].text == "看图"


# ---------------------------------------------------------------------------
# 3) 指标口径
# ---------------------------------------------------------------------------
def _cells() -> list:
    """覆盖五类判定的小样本（人工 4 格 + 共识 2 格）。"""
    from scripts.evaluate_extractor import CellResult

    return [
        CellResult(
            "p1", "stance", "LONG", SOURCE_HUMAN, "LONG", "LONG", "LONG", OUTCOME_CORRECT_VALUE
        ),
        CellResult(
            "p1", "horizon", "1D", SOURCE_HUMAN, "1d", "1D", "1d", OUTCOME_CORRECT_VALUE
        ),
        CellResult("p1", "stop_loss", "2420", SOURCE_HUMAN, "2420", "", "", OUTCOME_MISSED),
        CellResult(
            "p1", "take_profit", "2292", SOURCE_HUMAN, "2292", "2400", "2400", OUTCOME_WRONG
        ),
        CellResult("p2", "stance", "", SOURCE_CONSENSUS, "", "SHORT", "SHORT", OUTCOME_SPURIOUS),
        CellResult("p2", "horizon", "", SOURCE_CONSENSUS, "", "", "", OUTCOME_CORRECT_ABSENT),
    ]


def test_field_metrics_rates() -> None:
    metrics = field_metrics(_cells(), ("stance", "horizon", "stop_loss", "take_profit"))
    stance = metrics["stance"]

    assert (stance.cells, stance.gold_present, stance.gold_absent) == (2, 1, 1)
    assert stance.accuracy_on_gold_present == 1.0
    assert stance.precision == 0.5  # 给了两个值，只有一个对（另一个是假阳性）
    assert stance.recall == 1.0
    assert stance.exact_accuracy == 0.5
    assert stance.spurious_rate == 1.0
    assert metrics["stop_loss"].missed_rate == 1.0
    assert metrics["take_profit"].wrong_rate == 1.0
    assert metrics["horizon"].exact_accuracy == 1.0


def test_field_metrics_layer_filtering() -> None:
    human = field_metrics(_cells(), ("stance",), source=SOURCE_HUMAN)
    consensus = field_metrics(_cells(), ("stance",), source=SOURCE_CONSENSUS)

    assert human["stance"].gold_present == 1
    assert consensus["stance"].gold_present == 0
    assert consensus["stance"].accuracy_on_gold_present == 0.0


def test_aggregate_sums_fields() -> None:
    total = aggregate(
        field_metrics(_cells(), ("stance", "horizon", "stop_loss", "take_profit")),
        label="核心四字段",
    )

    assert total.cells == 6
    assert total.gold_present == 4  # 共识层两格金标准为空
    assert total.correct_value == 2
    assert total.missed == 1 and total.wrong_value == 1 and total.spurious == 1


def test_unknown_vs_no_opinion_is_counted() -> None:
    from scripts.evaluate_extractor import CellResult

    cells = [
        CellResult("p1", "stance", "UNKNOWN", SOURCE_HUMAN, "UNKNOWN", "", "", OUTCOME_MISSED)
    ]

    metrics = field_metrics(cells, ("stance",))

    assert metrics["stance"].missed == 1
    assert metrics["stance"].unknown_vs_no_opinion == 1


# ---------------------------------------------------------------------------
# 4) 混淆矩阵
# ---------------------------------------------------------------------------
def test_label_order_prefers_canonical_and_absent_last() -> None:
    assert label_order("stance", ["UNKNOWN", "LONG", "LONG", "∅（未给出）"]) == (
        "LONG",
        "UNKNOWN",
        "∅（未给出）",
    )


def test_confusion_matrix_counts_and_shape() -> None:
    from scripts.evaluate_extractor import CellResult

    cells = [
        CellResult("p1", "stance", "LONG", SOURCE_HUMAN, "LONG", "SHORT", "SHORT", OUTCOME_WRONG),
        CellResult(
            "p2", "stance", "LONG", SOURCE_HUMAN, "LONG", "LONG", "LONG", OUTCOME_CORRECT_VALUE
        ),
        CellResult("p3", "stance", "", SOURCE_CONSENSUS, "", "", "", OUTCOME_CORRECT_ABSENT),
    ]

    order, matrix = confusion_matrix(cells, "stance")

    assert order == ("LONG", "SHORT", "∅（未给出）")
    assert matrix[0][0] == 1  # LONG → LONG
    assert matrix[0][1] == 1  # LONG → SHORT
    assert matrix[2][2] == 1  # 未给出 → 未给出


def test_confusion_matrix_layer_filtering() -> None:
    from scripts.evaluate_extractor import CellResult

    cells = [
        CellResult(
            "p1", "stance", "LONG", SOURCE_HUMAN, "LONG", "LONG", "LONG", OUTCOME_CORRECT_VALUE
        ),
        CellResult(
            "p2", "stance", "SHORT", SOURCE_CONSENSUS, "SHORT", "LONG", "LONG", OUTCOME_WRONG
        ),
    ]

    order, matrix = confusion_matrix(cells, "stance", source=SOURCE_HUMAN)

    assert order == ("LONG", "∅（未给出）")
    assert sum(sum(row) for row in matrix) == 1


# ---------------------------------------------------------------------------
# 5) 端到端（假抽取器）与质量指标
# ---------------------------------------------------------------------------
def test_evaluate_counts_posts_opinions_and_quality() -> None:
    gold = _gold(
        [
            ("p1", "stance", "SHORT", SOURCE_HUMAN),
            ("p1", "stop_loss", "2420", SOURCE_HUMAN),
            ("p1", "horizon", "", SOURCE_CONSENSUS),
            ("p2", "stance", "LONG", SOURCE_HUMAN),  # 抽取器判无观点 → 该判未判
            ("p3", "stance", "LONG", SOURCE_CONSENSUS),  # 多观点帖
            ("p4", "stance", "LONG", SOURCE_CONSENSUS),  # confidence 非法
        ]
    )
    texts = _texts(
        {
            "p1": "普通文本",
            "p2": "NO_OPINION 这条没有方向",
            "p3": "MULTI 两个观点",
            "p4": "INVALID_CONF 置信度越界",
        }
    )

    stats = evaluate(gold, texts, StubExtractor())

    assert stats.parser_version == "stub-v1"
    assert stats.posts_total == 4
    assert stats.posts_no_opinion == 1
    assert stats.posts_with_opinion == 3
    assert stats.posts_multi_draft == 1
    assert stats.drafts_unknown_stance == 0
    assert stats.confidence_invalid == 1
    assert stats.confidence_missing == 2  # MULTI 分支的两条 draft 未给置信度
    assert stats.diagnostics["no_stance_keyword"] == 1
    assert stats.warnings["标的 XAUUSD 不在白名单内"] == 1  # 只有 p1 走默认分支
    assert stats.coverage_only == {"entry_low": 1, "entry_high": 0}
    assert stats.no_opinion_rate == 0.25


def test_evaluate_records_missing_texts_and_price_bands() -> None:
    gold = _gold(
        [
            ("p1", "stop_loss", "2400", SOURCE_HUMAN),  # 抽到 2420 → 差 20
            ("p9", "stance", "LONG", SOURCE_HUMAN),  # 找不到正文
        ]
    )
    texts = _texts({"p1": "普通文本"})

    stats = evaluate(gold, texts, StubExtractor())

    assert stats.missing_texts == ["p9"]
    assert stats.posts_total == 1
    bands = stats.price_bands["stop_loss"]
    assert bands[">10"] == 1  # 2400 与 2420 相差 20，落在最远一档
    assert all(bands.get(label, 0) == 0 for label in ("精确相等", "≤2", "≤5", "≤10"))
    wrong = [cell for cell in stats.cells if cell.outcome == OUTCOME_WRONG]
    assert len(wrong) == 1 and "差值 20" in wrong[0].note


def test_evaluate_is_deterministic_in_post_order() -> None:
    gold = _gold(
        [
            ("p2", "stance", "SHORT", SOURCE_HUMAN),
            ("p1", "stance", "SHORT", SOURCE_HUMAN),
        ]
    )
    texts = _texts({"p1": "普通文本", "p2": "普通文本"})

    stats = evaluate(gold, texts, StubExtractor())

    assert [cell.post_id for cell in stats.cells] == ["p1", "p2"]


def test_write_eval_csv_has_documented_columns(tmp_path: Path) -> None:
    stats = evaluate(
        _gold([("p1", "stance", "SHORT", SOURCE_HUMAN)]),
        _texts({"p1": "普通文本"}),
        StubExtractor(),
    )
    path = tmp_path / "eval.csv"

    written = write_eval_csv(path, stats)

    assert written == 1
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == list(EVAL_COLUMNS)
    assert rows[0]["outcome"] == OUTCOME_CORRECT_VALUE
    assert rows[0]["gold_source"] == SOURCE_HUMAN


# ---------------------------------------------------------------------------
# 6) 报告与 CLI
# ---------------------------------------------------------------------------
def _report(tmp_path: Path, stats) -> str:
    from scripts.evaluate_extractor import render_report

    return render_report(
        stats,
        gold_path=tmp_path / "ground_truth_200.csv",
        gold_digest="deadbeefdeadbeef",
        texts_path=tmp_path / "annotation_sample.csv",
        eval_out=tmp_path / "extractor_eval.csv",
        generated_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        texts=_texts({"p1": "普通文本", "p2": "NO_OPINION 无方向"}),
    )


def test_render_report_contains_verdicts_and_failure_breakdown(tmp_path: Path) -> None:
    stats = evaluate(
        _gold(
            [
                ("p1", "stance", "SHORT", SOURCE_HUMAN),
                ("p2", "stance", "LONG", SOURCE_HUMAN),
            ]
        ),
        _texts({"p1": "普通文本", "p2": "NO_OPINION 无方向"}),
        StubExtractor(),
    )

    text = _report(tmp_path, stats)

    assert "Phase 2 基线评估报告" in text
    assert "该判未判" in text and "提取错误" in text and "不该判却判" in text
    assert "混淆矩阵" in text and "PASS" in text and "FAIL" in text
    assert "deadbeefdeadbeef" in text
    assert "无观点率" in text


def test_main_end_to_end_with_real_extractor(tmp_path: Path, capsys) -> None:
    gold_path = tmp_path / "gold.csv"
    gold_path.write_text(
        "post_id,field,value,value_raw,source\n"
        f"p1,stance,SHORT,做空,{SOURCE_HUMAN}\n"
        f"p1,stop_loss,2420,2420,{SOURCE_HUMAN}\n",
        encoding="utf-8-sig",
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text(
        'post_id,text_content,has_media\np1,"黄金 做空，目标 2292，止损 2420",false\n',
        encoding="utf-8-sig",
    )
    eval_out = tmp_path / "eval.csv"
    report_path = tmp_path / "report.md"

    exit_code = main(
        [
            "--gold",
            str(gold_path),
            "--texts",
            str(texts_path),
            "--eval-out",
            str(eval_out),
            "--report",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "准确率" in captured.out
    assert eval_out.exists() and report_path.exists()
    with eval_out.open("r", encoding="utf-8-sig", newline="") as handle:
        outcomes = {row["field"]: row["outcome"] for row in csv.DictReader(handle)}
    assert outcomes["stance"] == OUTCOME_CORRECT_VALUE
    assert outcomes["stop_loss"] == OUTCOME_CORRECT_VALUE
    assert "PASS" in report_path.read_text(encoding="utf-8")


def test_main_missing_input_returns_2(tmp_path: Path, capsys) -> None:
    exit_code = main(["--gold", str(tmp_path / "nope.csv")])

    assert exit_code == 2
    assert "输入文件不存在" in capsys.readouterr().err


def test_main_returns_2_when_gold_has_no_eval_fields(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.csv"
    gold_path.write_text(
        f"post_id,field,value,source\np1,entry_low,2300,{SOURCE_HUMAN}\n", encoding="utf-8"
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text("post_id,text_content\np1,黄金做多\n", encoding="utf-8")

    exit_code = main(["--gold", str(gold_path), "--texts", str(texts_path)])

    assert exit_code == 2


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    gold_path = tmp_path / "gold.csv"
    gold_path.write_text(
        f"post_id,field,value,source\np1,stance,SHORT,{SOURCE_HUMAN}\n", encoding="utf-8"
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text("post_id,text_content\np1,黄金 做空，止损 2420\n", encoding="utf-8")
    eval_out = tmp_path / "eval.csv"

    exit_code = main(
        [
            "--gold",
            str(gold_path),
            "--texts",
            str(texts_path),
            "--eval-out",
            str(eval_out),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert not eval_out.exists()
    assert "--dry-run" in capsys.readouterr().out


def test_main_returns_3_when_texts_missing(tmp_path: Path, capsys) -> None:
    gold_path = tmp_path / "gold.csv"
    gold_path.write_text(
        f"post_id,field,value,source\np1,stance,SHORT,{SOURCE_HUMAN}\np2,stance,LONG,{SOURCE_HUMAN}\n",
        encoding="utf-8",
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text("post_id,text_content\np1,黄金 做空，止损 2420\n", encoding="utf-8")
    report_path = tmp_path / "report.md"

    exit_code = main(
        [
            "--gold",
            str(gold_path),
            "--texts",
            str(texts_path),
            # 一律显式指定输出路径：避免测试把 logs/ 下的真实产物覆盖掉（曾真实发生）
            "--eval-out",
            str(tmp_path / "eval.csv"),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 3
    assert report_path.exists()  # 缺正文也要出报告，便于排查
    assert (tmp_path / "eval.csv").exists()
    assert "找不到正文" in capsys.readouterr().err


def test_script_stays_off_database() -> None:
    import scripts.evaluate_extractor as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    for banned in ("sqlalchemy", "from database", "import database", "build_engine"):
        assert banned not in source, f"评估脚本不得触碰数据库：{banned}"


# ---------------------------------------------------------------------------
# 7) 抽样（--limit / --sample）：决定 20 条试点到底跑哪 20 条
# ---------------------------------------------------------------------------
def _cue_texts() -> dict[str, PostText]:
    """覆盖各类线索 + 一条普通文本（用于抽样测试）。"""
    return {
        "p1": PostText("p1", "如果通胀超预期，黄金才会转做空，目标 2325", True),
        "p2": PostText("p2", "机构认为黄金应当做多，目标 2327", False),
        "p3": PostText("p3", "不排除黄金做多，止损 2318", False),
        "p4": PostText("p4", "昨天黄金做多已止盈 2326", False),
        "p5": PostText("p5", "黄金做空，入场 2324-2331", False),
        "p6": PostText("p6", "晚间数据公布", False),
        "p7": PostText("p7", "看图操作，图上标注 2483", True),
    }


def _gold_for(post_ids: list[str]) -> dict[str, dict[str, GoldCell]]:
    return {
        post_id: {"stance": GoldCell(post_id, "stance", "LONG", SOURCE_HUMAN, "做多")}
        for post_id in post_ids
    }


def test_text_cues_detects_buckets_and_falls_back_to_plain() -> None:
    assert "conditional" in text_cues("如果黄金跌破 2300")
    assert "quote" in text_cues("据消息人士称，美联储将转向")
    assert "weak" in text_cues("不排除继续上行")
    assert "posthoc" in text_cues("昨天已止盈")
    assert "media" in text_cues("看图操作")
    assert "has_media" in text_cues("无数字的文本", has_media=True)
    assert text_cues("晚间数据公布") == ("plain",)


def test_text_cues_do_not_misfire_on_common_words() -> None:
    """★ 子串匹配的经典坑：`据` 会命中「数据」、`称` 会命中「名称」——线索词必须具体。"""
    assert text_cues("美国 CPI 数据公布") == ("plain",)
    assert "quote" not in text_cues("黄金名称叫 XAUUSD")


def test_select_posts_head_is_deterministic_prefix() -> None:
    gold = _gold_for(["p3", "p1", "p2", "p4"])

    chosen, tags = select_posts(gold, _cue_texts(), limit=2, mode="head")

    assert chosen == ["p1", "p2"]  # 按 post_id 正序
    assert set(tags) == {"p1", "p2"}


def test_select_posts_zero_limit_takes_everything() -> None:
    gold = _gold_for(["p1", "p2", "p3"])

    chosen, _ = select_posts(gold, _cue_texts(), limit=0, mode="head")

    assert chosen == ["p1", "p2", "p3"]


def test_select_posts_stratified_covers_adversarial_buckets() -> None:
    """★ 分层抽样必须把条件句/引用/弱暗示都带进来——否则试点会全落在普通句上。"""
    gold = _gold_for(["p1", "p2", "p3", "p4", "p5", "p6"])

    chosen, tags = select_posts(gold, _cue_texts(), limit=5, mode="stratified")

    assert len(chosen) == 5
    assert {"p1", "p2", "p3"} <= set(chosen)  # 条件句 / 引用 / 弱暗示
    assert "conditional" in {cue for value in tags.values() for cue in value.split("+")}


def test_select_posts_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知抽样模式"):
        select_posts(_gold_for(["p1"]), _cue_texts(), limit=1, mode="random")


def test_build_extractor_switches_implementation(monkeypatch) -> None:
    import scripts.evaluate_extractor as module

    regex = module.build_extractor(
        module._parse_args(["--extractor", "regex", "--parser-version", "mock-regex-v9"])
    )
    assert isinstance(regex, RegexOpinionExtractor)
    assert regex.parser_version == "mock-regex-v9"

    captured: dict[str, Any] = {}

    class FakeLLM:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.parser_version = kwargs.get("parser_version", "")

        def extract(self, text: str, *, has_media: bool = False) -> Any:  # pragma: no cover
            raise AssertionError("本测试只验证构造参数")

    monkeypatch.setattr(module, "LLMOpinionExtractor", FakeLLM)
    module.build_extractor(
        module._parse_args(["--extractor", "llm", "--llm-max-api-calls", "7", "--llm-model", "m"])
    )

    assert captured["max_api_calls"] == 7
    assert captured["model"] == "m"
    assert captured["cache_mode"] == "auto"
    assert captured["parser_version"] == module.LLM_PARSER_VERSION


def test_default_outputs_differ_per_extractor() -> None:
    """LLM 试点不得覆盖正则基线产物（路径必须分开）。"""
    assert DEFAULT_EVAL_OUT_LLM.name == "extractor_eval_llm.csv"
    assert DEFAULT_EVAL_OUT_REGEX.name == "extractor_eval.csv"
    assert DEFAULT_REPORT_LLM != DEFAULT_REPORT_REGEX


# ---------------------------------------------------------------------------
# 8) LLM 模式端到端（**假抽取器**，零网络零 token）
# ---------------------------------------------------------------------------
class FakeLLMExtractor:
    """假 LLM 抽取器：固定观点 + 一份 token 台账（验证接线与渲染，不联网）。"""

    parser_version = "llm-fake-v1"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.seen: list[str] = []

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        self.seen.append(text)
        return OpinionExtractionResult(
            parser_version=self.parser_version,
            drafts=(
                AuthorOpinionDraft(
                    stance=OpinionStance.LONG,
                    parser_version=self.parser_version,
                    stop_loss=Decimal("2420"),
                ),
            ),
            warnings=("llm-source=api",),
        )

    def stats(self) -> dict[str, Any]:
        return {
            "parser_version": self.parser_version,
            "model": "deepseek-flash",
            "thinking_mode": "disabled",
            "posts_via_api": len(self.seen),
            "posts_via_cache": 0,
            "api_failures": 0,
            "parse_failures": 0,
            "client": {"api_calls": len(self.seen)},
            "usage": {
                "api": {
                    "posts": len(self.seen),
                    "prompt_tokens": 3380 * len(self.seen),
                    "completion_tokens": 120 * len(self.seen),
                    "cache_hit_tokens": 3000 * len(self.seen),
                    "cache_miss_tokens": 380 * len(self.seen),
                },
                "cached": {"posts": 0, "prompt_tokens": 0, "completion_tokens": 0},
                "api_estimated_cost_usd_peak": 0.0003,
                "api_estimated_cost_usd_offpeak": 0.0002,
                "full_set_estimated_cost_usd_peak": 0.0003,
                "full_set_estimated_cost_usd_offpeak": 0.0002,
            },
        }


def _llm_run_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """两份最小输入（金标准 + 正文），返回 `(gold, texts, eval_out, report)`。"""
    gold_path = tmp_path / "gold.csv"
    gold_path.write_text(
        "post_id,field,value,value_raw,source\n"
        f"p1,stance,LONG,做多,{SOURCE_HUMAN}\n"
        f"p1,stop_loss,2420,2420,{SOURCE_HUMAN}\n"
        f"p2,stance,SHORT,做空,{SOURCE_HUMAN}\n",
        encoding="utf-8-sig",
    )
    texts_path = tmp_path / "texts.csv"
    texts_path.write_text(
        "post_id,text_content,has_media\n"
        'p1,"如果通胀超预期，黄金才会转做空，止损 2420",false\n'
        'p2,"黄金做空，目标 2292",false\n',
        encoding="utf-8-sig",
    )
    return gold_path, texts_path, tmp_path / "eval_llm.csv", tmp_path / "report_llm.md"


def _llm_argv(
    gold_path: Path, texts_path: Path, eval_out: Path, report_path: Path, *extra: str
) -> list[str]:
    return [
        "--extractor",
        "llm",
        *extra,
        "--gold",
        str(gold_path),
        "--texts",
        str(texts_path),
        "--eval-out",
        str(eval_out),
        "--report",
        str(report_path),
    ]


def test_main_llm_mode_writes_eval_report_and_usage(tmp_path: Path, monkeypatch, capsys) -> None:
    """★ LLM 模式端到端：逐格明细 + 报告 + 台账三份产物都要落盘，台账要打印。"""
    import scripts.evaluate_extractor as module

    monkeypatch.setattr(module, "LLMOpinionExtractor", FakeLLMExtractor)
    gold_path, texts_path, eval_out, report_path = _llm_run_files(tmp_path)

    exit_code = module.main(_llm_argv(gold_path, texts_path, eval_out, report_path))

    assert exit_code == 0
    assert eval_out.exists() and report_path.exists()
    usage_path = eval_out.with_name(f"{eval_out.stem}_usage.json")
    assert usage_path.exists()
    usage = json.loads(usage_path.read_text(encoding="utf-8"))["usage"]
    assert usage["api"]["prompt_tokens"] == 3380 * 2  # 2 条帖子
    report_text = report_path.read_text(encoding="utf-8")
    assert "LLMOpinionExtractor" in report_text
    assert "Token 与费用台账" in report_text
    assert "--llm-cache-mode readonly" in report_text  # 报告给出零成本复跑命令
    stdout = capsys.readouterr().out
    assert "Token/费用台账" in stdout
    assert "思考模式 disabled" in stdout


def test_main_llm_mode_limit_reduces_sample(tmp_path: Path, monkeypatch, capsys) -> None:
    import scripts.evaluate_extractor as module

    monkeypatch.setattr(module, "LLMOpinionExtractor", FakeLLMExtractor)
    gold_path, texts_path, eval_out, report_path = _llm_run_files(tmp_path)

    exit_code = module.main(
        _llm_argv(gold_path, texts_path, eval_out, report_path, "--limit", "1", "--sample", "head")
    )

    assert exit_code == 0
    assert "抽样：head / limit=1 → 实选 1 条" in capsys.readouterr().out
    assert "抽样" in report_path.read_text(encoding="utf-8")
    usage = json.loads(
        eval_out.with_name(f"{eval_out.stem}_usage.json").read_text(encoding="utf-8")
    )
    assert usage["posts_total"] == 1  # 只跑了 1 条 → 台账也只记 1 条


def test_main_llm_mode_fails_fast_on_llm_error(tmp_path: Path, monkeypatch, capsys) -> None:
    """系统性故障（401 / 缺 Key / readonly 未命中）必须返回 2，不得产出误导性结果。"""
    import scripts.evaluate_extractor as module

    class BrokenLLM(FakeLLMExtractor):
        def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
            raise LLMError("鉴权失败（HTTP 401）")

    monkeypatch.setattr(module, "LLMOpinionExtractor", BrokenLLM)
    gold_path, texts_path, eval_out, report_path = _llm_run_files(tmp_path)

    exit_code = module.main(_llm_argv(gold_path, texts_path, eval_out, report_path))

    assert exit_code == 2
    assert not eval_out.exists()
    assert "LLM 调用失败" in capsys.readouterr().err


def test_main_regex_mode_ignores_llm_flags(tmp_path: Path, capsys) -> None:
    """回归：加 LLM 参数后，默认 regex 路径与产物名保持不变。"""
    gold_path, texts_path, eval_out, report_path = _llm_run_files(tmp_path)

    exit_code = main(
        [
            "--gold",
            str(gold_path),
            "--texts",
            str(texts_path),
            "--eval-out",
            str(eval_out),
            "--report",
            str(report_path),
            "--llm-max-api-calls",
            "3",  # regex 模式下应被忽略
        ]
    )

    assert exit_code == 0
    report_text = report_path.read_text(encoding="utf-8")
    assert "RegexOpinionExtractor（纯规则基线）" in report_text
    assert "LLMOpinionExtractor" not in report_text
    assert "Token 与费用台账" not in report_text
    assert "抽取器 mock-regex-v1" in capsys.readouterr().out


def test_usage_lines_render_token_and_cost() -> None:
    from scripts.evaluate_extractor import _usage_lines

    extractor = FakeLLMExtractor()
    extractor.extract("a")
    extractor.extract("b")  # 两条 → 台账里 posts=2

    text = "\n".join(_usage_lines(extractor.stats()["usage"], posts=2))

    assert "本次真实调用：2 条" in text
    assert "缓存命中 6000" in text
    assert "峰值价 $0.0003" in text
    assert "只统计**成功响应**的 token" in text


def test_usage_lines_render_unknown_model_cost() -> None:
    from scripts.evaluate_extractor import _usage_lines

    errors: dict[str, Any] = {"api": {}, "cached": {}, "api_estimated_cost_usd_peak": None}
    text = "\n".join(_usage_lines(errors, posts=1))

    assert "未知（模型不在价目表内）" in text

