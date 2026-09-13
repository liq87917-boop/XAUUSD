"""`RegexOpinionExtractor` 单元测试（100% Mock：无网络、无数据库、无时间依赖）。

覆盖团队要求的三类：**多分类**（同帖多标的/多方向）、**无匹配**（无观点样本）、
**中英文**与**边界条件**（条件性、引用、复盘、否定、弱化措辞、非价格数字、
方向不一致点位、空文本、图片无文字、确定性、Protocol 契约）。

判定依据全部来自 `docs/10_标注规范`（已批准 v1.0）§4，测试名即规则编号对应关系。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from database.models.enums import InformationType, OpinionHorizon, OpinionStance
from src.processors.opinion_extractor import (
    DiagnosticCode,
    OpinionExtractionResult,
    OpinionExtractor,
    build_drafts,
)
from src.processors.regex_extractor import DEFAULT_PARSER_VERSION, RegexOpinionExtractor

pytestmark = pytest.mark.unit

EXTRACTOR = RegexOpinionExtractor()


def _codes(result: OpinionExtractionResult) -> set[str]:
    return {diagnostic.code.value for diagnostic in result.diagnostics}


# ---------------------------------------------------------------------------
# A. 方向与基本字段（docs/10 §4.1）
# ---------------------------------------------------------------------------
def test_chinese_long_opinion_with_full_plan() -> None:
    """docs/10 附录 B-1：明确方向 + 完整点位 → LONG / 1h / confidence 0.9。"""
    result = EXTRACTOR.extract("黄金 2380 附近轻仓多，止损 2365，目标 2450，日内短线。")

    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert draft.stance is OpinionStance.LONG
    assert draft.instrument == "XAUUSD"
    assert draft.horizon is OpinionHorizon.H1
    assert draft.confidence == Decimal("0.9")
    assert draft.entry_low == Decimal("2380")
    assert draft.entry_high == Decimal("2380")
    assert draft.stop_loss == Decimal("2365")
    assert draft.take_profit == Decimal("2450")
    assert draft.information_type is InformationType.TECHNICAL
    assert draft.parser_version == DEFAULT_PARSER_VERSION
    assert result.no_opinion is False


def test_english_short_opinion_with_full_plan() -> None:
    """英文表达同样生效（团队要求覆盖中英文）。"""
    result = EXTRACTOR.extract("Gold short from 2380, stop 2400, target 2300")

    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert draft.stance is OpinionStance.SHORT
    assert draft.instrument == "XAUUSD"
    assert draft.entry_low == Decimal("2380")
    assert draft.stop_loss == Decimal("2400")
    assert draft.take_profit == Decimal("2300")
    assert draft.horizon is None  # 文本未给周期 → 留空，不得默认 1d


def test_flat_opinion_keeps_stance_and_no_direction_prices() -> None:
    result = EXTRACTOR.extract("黄金短线观望，等信号再进。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.FLAT
    assert draft.horizon is OpinionHorizon.H1
    assert draft.confidence == Decimal("0.6")
    assert draft.information_type is InformationType.OTHER  # 无关键词但有方向 → OTHER
    assert draft.has_price_plan is False


def test_direction_wins_over_hedge_word_in_same_clause() -> None:
    """"2380-2395 区间做多"：区间（FLAT 词）不得覆盖明确方向。"""
    result = EXTRACTOR.extract("黄金 2380-2395 区间做多，止损 2365。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.LONG
    assert draft.entry_low == Decimal("2380")
    assert draft.entry_high == Decimal("2395")


def test_horizon_absent_is_null() -> None:
    """正文没有周期线索时留空（禁止默认 1d，docs/10 §4.2）。"""
    assert EXTRACTOR.extract("黄金看多，目标 2450").drafts[0].horizon is None


# ---------------------------------------------------------------------------
# B. 周期词典（docs/10 §4.2）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("黄金超短线看多，目标 2450", OpinionHorizon.M15),
        ("黄金半小时内看多，目标 2450", OpinionHorizon.M30),
        ("黄金日内短线看多，目标 2450", OpinionHorizon.H1),
        ("黄金美盘时段看多，目标 2450", OpinionHorizon.H4),
        ("黄金本周看多，目标 2450", OpinionHorizon.D1),
        ("黄金中线看多，目标 2450", OpinionHorizon.D1),
    ],
)
def test_horizon_dictionary_mapping(text: str, expected: OpinionHorizon) -> None:
    assert EXTRACTOR.extract(text).drafts[0].horizon is expected


def test_horizon_uses_closing_hint_for_conditional_text() -> None:
    """docs/10 附录 B-2 的样本：条件句 + "收盘" → 1d（周期仍可提取）。"""
    result = EXTRACTOR.extract("如果今天黄金收盘跌破 2380，那我会转空看 2330。")

    assert result.drafts[0].horizon is OpinionHorizon.D1


# ---------------------------------------------------------------------------
# C. 标的解析（docs/10 §4.7）
# ---------------------------------------------------------------------------
def test_multi_instrument_produces_one_opinion_each() -> None:
    """多分类：同一方向句提到两个标的 → 两条观点（各自带标的）。"""
    result = EXTRACTOR.extract("黄金和白银都看多，目标 2450。")

    assert [draft.instrument for draft in result.drafts] == ["XAUUSD", "XAGUSD"]
    assert all(draft.stance is OpinionStance.LONG for draft in result.drafts)
    assert all(draft.take_profit == Decimal("2450") for draft in result.drafts)


def test_multi_direction_same_sentence_is_split() -> None:
    """同一句不同方向 → 拆成两条（禁止合并成"中性"）。"""
    result = EXTRACTOR.extract("黄金看多，白银看空。")

    assert [(draft.stance, draft.instrument) for draft in result.drafts] == [
        (OpinionStance.LONG, "XAUUSD"),
        (OpinionStance.SHORT, "XAGUSD"),
    ]


def test_instrument_from_same_sentence_is_inherited() -> None:
    """"黄金，2380 做多"：标的在另一个分句里，仍属同一句 → 可继承。"""
    result = EXTRACTOR.extract("黄金，2380 做多，止损 2360。")

    assert result.drafts[0].instrument == "XAUUSD"


def test_instrument_is_null_when_not_mentioned() -> None:
    """未明确提到标的 → 留空（禁止默认 XAUUSD）。"""
    result = EXTRACTOR.extract("2380 做多，止损 2360，目标 2450。")

    assert result.drafts[0].instrument is None
    assert result.warnings == ()


def test_unknown_instrument_produces_warning_not_error() -> None:
    """白名单外的**显式代码**：不拒绝（可能是新标的），但必须告警（DB 外键兜底）。"""
    result = EXTRACTOR.extract("XPTUSD 日内看多，目标 1100。")

    draft = result.drafts[0]
    assert draft.instrument == "XPTUSD"
    assert draft.stance is OpinionStance.LONG
    assert any("XPTUSD" in warning for warning in result.warnings), result.warnings


def test_uppercase_noise_is_not_treated_as_instrument() -> None:
    """大写噪声词（COVID）不得被当成标的代码。"""
    result = EXTRACTOR.extract("COVID 影响下黄金看多，目标 2450。")

    assert result.drafts[0].instrument == "XAUUSD"
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# D. 特殊情形（docs/10 §4.1.1 ~ §4.1.5）
# ---------------------------------------------------------------------------
def test_conditional_text_yields_unknown_with_marker() -> None:
    """docs/10 附录 B-2：条件性观点 → UNKNOWN，且 rationale 保留完整条件句。"""
    result = EXTRACTOR.extract("如果今天黄金收盘跌破 2380，那我会转空看 2330。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.UNKNOWN
    assert draft.horizon is OpinionHorizon.D1
    assert draft.confidence is None
    assert draft.has_price_plan is False  # 降级为 UNKNOWN 时不得保留点位
    assert "conditional" in (draft.rationale or "")
    assert DiagnosticCode.CONDITIONAL.value in _codes(result)


def test_quoted_third_party_view_yields_unknown() -> None:
    result = EXTRACTOR.extract("高盛认为黄金将跌破 2300。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.UNKNOWN
    assert "quote-only" in (draft.rationale or "")
    assert DiagnosticCode.QUOTE_ONLY.value in _codes(result)


def test_first_person_view_is_not_treated_as_quote() -> None:
    result = EXTRACTOR.extract("我认为黄金将跌破 2300。")

    assert result.drafts[0].stance is OpinionStance.SHORT
    assert DiagnosticCode.QUOTE_ONLY.value not in _codes(result)


def test_post_hoc_review_yields_unknown() -> None:
    result = EXTRACTOR.extract("昨天黄金 2380 多单已经止盈，收益率 2%。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.UNKNOWN
    assert "post-hoc" in (draft.rationale or "")
    assert DiagnosticCode.POST_HOC.value in _codes(result)


def test_negated_direction_yields_unknown_not_opposite() -> None:
    """"不看多" ≠ "看空"：方向作废记 UNKNOWN，绝不反转为相反方向（过度解读风险）。"""
    result = EXTRACTOR.extract("现在不看多黄金，先等机会。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.UNKNOWN
    assert DiagnosticCode.NEGATED_DIRECTION.value in _codes(result)


def test_double_negative_weakens_but_keeps_direction() -> None:
    """"不排除…看涨"是弱化方向，不是否定（docs/10 §4.1.5）。"""
    result = EXTRACTOR.extract("不排除黄金进一步看涨。")

    draft = result.drafts[0]
    assert draft.stance is OpinionStance.LONG
    assert draft.confidence == Decimal("0.4")
    assert DiagnosticCode.WEAK_LANGUAGE.value in _codes(result)


# ---------------------------------------------------------------------------
# E. confidence（docs/10 §4.3）
# ---------------------------------------------------------------------------
def test_confidence_is_lower_without_price_plan() -> None:
    assert EXTRACTOR.extract("黄金看多。").drafts[0].confidence == Decimal("0.7")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # docs/10 §4.1.5：可能/不排除/或许 属"弱暗示"档 → confidence ≤ 0.4
        ("黄金可能看多，目标 2450。", Decimal("0.4")),
        ("黄金不排除看多。", Decimal("0.4")),
        ("黄金或许看空，目标 2300。", Decimal("0.4")),
        # docs/10 §4.3：倾向/大概率/预计 属"倾向性措辞"档 → confidence ≤ 0.5
        ("黄金倾向看多，目标 2450。", Decimal("0.5")),
        ("黄金大概率看空，目标 2300。", Decimal("0.5")),
    ],
)
def test_weak_language_caps_confidence(text: str, expected: Decimal) -> None:
    assert EXTRACTOR.extract(text).drafts[0].confidence == expected


def test_unknown_stance_has_no_confidence() -> None:
    """UNKNOWN 不携带 confidence（没有方向就没有方向强度可言）。"""
    assert EXTRACTOR.extract("如果黄金跌破 2380 则做空。").drafts[0].confidence is None


# ---------------------------------------------------------------------------
# F. 数字与点位（docs/10 §4.4）
# ---------------------------------------------------------------------------
def test_percentage_and_date_numbers_are_not_prices() -> None:
    """CPI 3.2% / 8 月 这类数字不得被当成行情点位。"""
    result = EXTRACTOR.extract("美国 8 月 CPI 同比 3.2%，高于预期 3.0%，黄金看多，目标 2600。")

    draft = result.drafts[0]
    assert draft.take_profit == Decimal("2600")
    assert draft.information_type is InformationType.MACRO
    assert draft.instrument == "XAUUSD"


def test_number_with_unclear_unit_is_dropped() -> None:
    """"2450 点" 单位不明确 → 留空（禁止推算，docs/10 §4.4）。"""
    draft = EXTRACTOR.extract("黄金做多，目标 2450 点。").drafts[0]

    assert draft.take_profit is None
    assert draft.entry_low is None
    assert draft.confidence == Decimal("0.7")  # 无点位 → 只有方向词


def test_stop_loss_above_entry_is_dropped_for_long() -> None:
    result = EXTRACTOR.extract("黄金 2380 做多，止损 2420，目标 2500。")

    draft = result.drafts[0]
    assert draft.entry_low == Decimal("2380")
    assert draft.stop_loss is None  # 与 LONG 方向不一致 → 丢弃
    assert draft.take_profit == Decimal("2500")
    assert DiagnosticCode.PRICE_DROPPED.value in _codes(result)


def test_take_profit_below_entry_is_dropped_for_long() -> None:
    result = EXTRACTOR.extract("黄金 2380 做多，止损 2360，目标 2350。")

    draft = result.drafts[0]
    assert draft.stop_loss == Decimal("2360")
    assert draft.take_profit is None  # 目标低于入场 → 丢弃
    assert DiagnosticCode.PRICE_DROPPED.value in _codes(result)


def test_entry_range_is_parsed_into_low_and_high() -> None:
    draft = EXTRACTOR.extract("黄金 2380~2395 做多，止损 2360。").drafts[0]

    assert draft.entry_low == Decimal("2380")
    assert draft.entry_high == Decimal("2395")
    assert draft.stop_loss == Decimal("2360")


def test_stop_loss_before_entry_single_point() -> None:
    """单位置写法（"2360 止损"）也能识别（关键词在数字之后）。"""
    draft = EXTRACTOR.extract("黄金 2380 做多，2360 止损，2450 目标。").drafts[0]

    assert draft.entry_low == Decimal("2380")
    assert draft.stop_loss == Decimal("2360")
    assert draft.take_profit == Decimal("2450")


# ---------------------------------------------------------------------------
# G. 无匹配 / 边界（docs/10 §2.2、§4.6）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "   ", "\n\t  "])
def test_empty_text_returns_no_opinion(text: str) -> None:
    result = EXTRACTOR.extract(text)

    assert result.no_opinion is True
    assert result.drafts == ()
    assert DiagnosticCode.EMPTY_TEXT.value in _codes(result)


def test_pure_data_broadcast_is_a_no_opinion_sample() -> None:
    """docs/10 附录 B-3：纯数据播报 → 无观点（no_opinion=true），不是 UNKNOWN 观点。"""
    result = EXTRACTOR.extract("美国 8 月 CPI 同比 3.2%，高于预期 3.0%。")

    assert result.no_opinion is True
    assert result.drafts == ()
    assert DiagnosticCode.NO_STANCE_KEYWORD.value in _codes(result)


def test_image_only_text_is_flagged() -> None:
    result = EXTRACTOR.extract("看图操作。", has_media=True)

    assert result.no_opinion is True
    assert DiagnosticCode.IMAGE_ONLY.value in _codes(result)


def test_has_media_without_direction_is_flagged_as_image_only() -> None:
    result = EXTRACTOR.extract("今日盘面更新", has_media=True)

    assert DiagnosticCode.IMAGE_ONLY.value in _codes(result)


def test_has_media_with_direction_still_extracts() -> None:
    """有文字方向时，图片不影响抽取（OCR 属后续阶段）。"""
    result = EXTRACTOR.extract("黄金看多，目标 2450。", has_media=True)

    assert len(result.drafts) == 1


def test_rationale_is_truncated_to_documented_limit() -> None:
    draft = EXTRACTOR.extract("黄金看多" + "啊" * 300 + "。").drafts[0]

    assert draft.rationale is not None
    assert len(draft.rationale) <= 120


def test_multiple_sentences_are_returned_in_document_order() -> None:
    result = EXTRACTOR.extract("黄金看多，目标 2450。白银看空，目标 2300。")

    assert [(draft.stance, draft.instrument) for draft in result.drafts] == [
        (OpinionStance.LONG, "XAUUSD"),
        (OpinionStance.SHORT, "XAGUSD"),
    ]


def test_extraction_is_deterministic() -> None:
    """同输入必须同输出（验收比对与复现的前提）。"""
    text = "黄金 2380 做多，止损 2360，目标 2450，日内短线。白银可能看空。"

    first = EXTRACTOR.extract(text)
    second = EXTRACTOR.extract(text)

    assert first.drafts == second.drafts
    assert first.diagnostics == second.diagnostics


# ---------------------------------------------------------------------------
# H. 接口契约与共享校验工具
# ---------------------------------------------------------------------------
def _accepts_protocol(extractor: OpinionExtractor) -> str:
    """类型检查（mypy）层面的结构一致性断言：实现必须满足 OpinionExtractor 协议。"""
    return extractor.parser_version


def test_regex_extractor_implements_opinion_extractor_protocol() -> None:
    assert _accepts_protocol(RegexOpinionExtractor()) == DEFAULT_PARSER_VERSION


def test_parser_version_can_be_overridden() -> None:
    extractor = RegexOpinionExtractor(parser_version="mock-regex-v2")

    assert extractor.extract("黄金看多。").drafts[0].parser_version == "mock-regex-v2"


def test_build_drafts_rejects_invalid_candidates() -> None:
    """LLM/规则输出必须过同一层校验：非法候选转诊断，绝不带病入库。"""
    drafts, diagnostics = build_drafts(
        [
            {"stance": "MOON", "parser_version": "x"},
            {"stance": "LONG", "explanation": "多余字段", "parser_version": "x"},
        ],
        parser_version="x",
    )

    assert drafts == ()
    assert len(diagnostics) == 2
    assert all(item.code is DiagnosticCode.INVALID_DRAFT for item in diagnostics)


def test_build_drafts_fills_missing_parser_version() -> None:
    drafts, diagnostics = build_drafts([{"stance": "LONG"}], parser_version="unit-test-v1")

    assert diagnostics == ()
    assert drafts[0].parser_version == "unit-test-v1"


def test_result_summary_reports_counts_and_diagnostics() -> None:
    summary = EXTRACTOR.extract("黄金看多，目标 2450。").summary()

    assert DEFAULT_PARSER_VERSION in summary
    assert "drafts=1" in summary


def test_unknown_stance_count_supports_refusal_rate_reporting() -> None:
    """docs/10 §7 要求同时报告拒答率（UNKNOWN 比例）。"""
    result = EXTRACTOR.extract("如果黄金跌破 2380 则做空。黄金看多。")

    assert len(result.drafts) == 2
    assert result.unknown_stance_count == 1
