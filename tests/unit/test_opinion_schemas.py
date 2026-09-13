"""观点抽取数据契约测试（docs/10 §5 JSON Schema ↔ Pydantic ↔ DB CHECK）。

测试目标：
1. **契约严格性**：多余字段、非法枚举、越界数值一律被拒绝（不得"看起来合法"就放行）；
2. **与文档一致**：字段集合 / 必填项 / `additionalProperties=false` 与 docs/10 §5 相同；
3. **与数据库一致**：数值范围与 `docs/04 §10` 的 CHECK 相同（提前拦截，别等 PG 报错）；
4. **与种子一致**：`KNOWN_INSTRUMENTS` 与 `database/seeds/instruments.py` 白名单不漂移。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from database.models.enums import InformationType, OpinionHorizon, OpinionStance
from database.seeds.instruments import INSTRUMENT_SEEDS
from src.processors.schemas import (
    KNOWN_INSTRUMENTS,
    MAX_RATIONALE_LENGTH,
    AuthorOpinionDraft,
)

pytestmark = pytest.mark.unit

PARSER_VERSION = "mock-regex-v1"


def _draft(**overrides: object) -> AuthorOpinionDraft:
    payload: dict[str, object] = {"stance": "LONG", "parser_version": PARSER_VERSION}
    payload.update(overrides)
    return AuthorOpinionDraft(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1) 正常路径与字段映射
# ---------------------------------------------------------------------------
def test_draft_accepts_documented_fields() -> None:
    draft = _draft(
        instrument="XAUUSD",
        horizon="1h",
        confidence=Decimal("0.7"),
        entry_low=Decimal("2380"),
        entry_high=Decimal("2395"),
        stop_loss=Decimal("2365"),
        take_profit=Decimal("2450"),
        information_type="TECHNICAL",
        rationale="2380 附近做多，目标 2450",
    )

    assert draft.stance is OpinionStance.LONG
    assert draft.horizon is OpinionHorizon.H1
    assert draft.information_type is InformationType.TECHNICAL
    assert draft.has_price_plan is True
    assert draft.confidence == Decimal("0.7")


def test_draft_allows_unknown_stance_with_nulls() -> None:
    """看不到方向时必须能写 UNKNOWN + 全空字段（禁止猜测）。"""
    draft = _draft(stance="UNKNOWN")

    assert draft.stance is OpinionStance.UNKNOWN
    assert draft.has_price_plan is False
    assert draft.instrument is None
    assert draft.horizon is None
    assert draft.confidence is None


def test_draft_is_frozen() -> None:
    draft = _draft(stance="SHORT")
    with pytest.raises(ValidationError):
        draft.stance = OpinionStance.LONG  # type: ignore[misc]


def test_json_example_excludes_none_but_keeps_required() -> None:
    payload = _draft().to_json_schema_example()

    assert payload == {"stance": "LONG", "parser_version": PARSER_VERSION}


# ---------------------------------------------------------------------------
# 2) 契约严格性（多余字段 / 非法枚举）
# ---------------------------------------------------------------------------
def test_extra_field_is_rejected() -> None:
    """抽取器不得输出多余字段（防止把模型解释性文字塞进结构化结果）。"""
    with pytest.raises(ValidationError) as excinfo:
        _draft(explanation="模型觉得应该看多")

    assert "explanation" in str(excinfo.value)


def test_missing_required_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthorOpinionDraft()  # type: ignore[call-arg]


@pytest.mark.parametrize("stance", ["MOON", "long", "", "看多"])
def test_stance_must_be_documented_value(stance: str) -> None:
    with pytest.raises(ValidationError):
        _draft(stance=stance)


@pytest.mark.parametrize("horizon", ["5m", "1m", "1D", "天天"])
def test_horizon_rejects_values_outside_phase2_windows(horizon: str) -> None:
    """只允许 Phase 2 的五个评价窗口（1m/5m 属行情 K 线，不是观点周期）。"""
    with pytest.raises(ValidationError):
        _draft(horizon=horizon)


@pytest.mark.parametrize("horizon", [member.value for member in OpinionHorizon])
def test_horizon_accepts_every_documented_window(horizon: str) -> None:
    assert _draft(horizon=horizon).horizon is OpinionHorizon(horizon)


def test_information_type_must_be_documented_value() -> None:
    with pytest.raises(ValidationError):
        _draft(information_type="GOSSIP")


# ---------------------------------------------------------------------------
# 3) 数值约束（与数据库 CHECK 一致）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("confidence", [Decimal("1.5"), Decimal("-0.01")])
def test_confidence_out_of_range_is_rejected(confidence: Decimal) -> None:
    with pytest.raises(ValidationError):
        _draft(confidence=confidence)


@pytest.mark.parametrize("confidence", [Decimal("0"), Decimal("1")])
def test_confidence_boundaries_are_accepted(confidence: Decimal) -> None:
    assert _draft(confidence=confidence).confidence == confidence


@pytest.mark.parametrize("field", ["entry_low", "entry_high", "stop_loss", "take_profit"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1")])
def test_prices_must_be_positive(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        _draft(**{field: value})


def test_entry_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        _draft(entry_low=Decimal("2420"), entry_high=Decimal("2400"))


def test_entry_range_single_point_is_accepted() -> None:
    draft = _draft(entry_low=Decimal("2380"), entry_high=Decimal("2380"))
    assert draft.entry_low == draft.entry_high


# ---------------------------------------------------------------------------
# 4) 文本字段
# ---------------------------------------------------------------------------
def test_rationale_max_length_is_enforced() -> None:
    with pytest.raises(ValidationError):
        _draft(rationale="长" * (MAX_RATIONALE_LENGTH + 1))


def test_rationale_is_stripped() -> None:
    assert _draft(rationale="  看多  ").rationale == "看多"


@pytest.mark.parametrize("parser_version", ["", "   ", "v" * 51])
def test_parser_version_is_required_and_bounded(parser_version: str) -> None:
    with pytest.raises(ValidationError):
        _draft(parser_version=parser_version)


# ---------------------------------------------------------------------------
# 5) 标的格式与白名单一致性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("instrument", ["xauusd", "1AUUSD", "AU USD", "A"])
def test_instrument_pattern_is_enforced(instrument: str) -> None:
    with pytest.raises(ValidationError):
        _draft(instrument=instrument)


@pytest.mark.parametrize("instrument", ["XAUUSD", "US10Y_REAL", "SGE_AU9999"])
def test_instrument_accepts_seeded_whitelist_symbols(instrument: str) -> None:
    assert _draft(instrument=instrument).instrument == instrument


def test_known_instruments_do_not_drift_from_seed_whitelist() -> None:
    """防止"表里有的标的、契约里没有"这类漂移（白名单唯一来源是种子定义）。"""
    seeded = {seed.symbol for seed in INSTRUMENT_SEEDS}
    assert seeded == KNOWN_INSTRUMENTS


# ---------------------------------------------------------------------------
# 6) 与 docs/10 §5 JSON Schema 的一致性
# ---------------------------------------------------------------------------
def test_generated_json_schema_matches_documented_contract() -> None:
    schema = AuthorOpinionDraft.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"stance", "parser_version"}
    assert set(schema["properties"]) == {
        "stance",
        "instrument",
        "horizon",
        "confidence",
        "entry_low",
        "entry_high",
        "stop_loss",
        "take_profit",
        "information_type",
        "rationale",
        "parser_version",
    }

