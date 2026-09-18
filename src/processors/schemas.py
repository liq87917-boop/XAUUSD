"""观点抽取的数据契约（Pydantic ↔ docs/10 §5 JSON Schema ↔ docs/04 §10 数据库约束）。

对应文档：
- `docs/10_标注规范.md` §5「输出结构（严格 JSON Schema）」——已获团队批准（v1.0）
- `docs/04_数据表结构及字段定义.md` §10 `author_opinions` 字段与 §44 实现层决策
- `docs/07` Phase 2 Prompt：所有 LLM/解析器输出必须记录 parser / model version

设计要点（为什么这样约束）：
1. ``extra="forbid"``：抽取器**不得**输出多余字段（防止把模型的解释性文字塞进结构化结果）。
2. 校验规则与数据库 CHECK **一一对应**（confidence ∈ [0,1]、四项价格 > 0、
   ``entry_low <= entry_high``）：非法输出在入库前就被拒绝，而不是等数据库报错。
3. ``effective_at`` / ``author_id`` / ``author_post_id`` **不在本契约内**：
   由管道从上游帖子注入（禁止模型编造时间与归属）。时间因果由
   :mod:`src.processors.timeline` 强制，并由 leakage 测试覆盖。
4. 数值用 ``Decimal``（禁止 float 传输关键金融数值，`docs/02` §5）。
5. ``null`` 表示"未给出 / 未计算"（**禁止**用 0 或默认标的冒充）；
   整帖无观点用"空数组"表达，而不是 ``stance=UNKNOWN``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from database.models.enums import InformationType, OpinionHorizon, OpinionStance

__all__ = [
    "KNOWN_INSTRUMENTS",
    "MAX_RATIONALE_LENGTH",
    "PARSER_VERSION_MAX_LENGTH",
    "AuthorOpinionDraft",
    "InstrumentCode",
    "PriceValue",
    "ProbabilityValue",
]

#: 抽取器输出中 rationale 的最大长度（与 docs/10 §5 Schema 的 maxLength 一致）
MAX_RATIONALE_LENGTH = 2000
#: parser_version 最大长度（与 author_opinions.parser_version VARCHAR(50) 一致）
PARSER_VERSION_MAX_LENGTH = 50

#: 已知标的（与 `database/seeds/instruments.py` 的白名单保持一致，
#: 由 `tests/unit/test_opinion_schemas.py` 断言两者不漂移）。
#: 未知但格式合法的标的**不拒绝**，只产生 WARNING（新标的应先入 instruments 白名单）。
KNOWN_INSTRUMENTS: frozenset[str] = frozenset(
    {
        "XAUUSD",
        "XAUUSD_DUKASCOPY",
        "XAGUSD",
        "COMEX_GC",
        "SGE_AU9999",
        "DXY",
        "US10Y",
        "US02Y",
        "US10Y_REAL",
        "USDCNY",
        "WTI",
        "BTCUSD",
    }
)

#: 标的代码格式：大写字母开头，允许数字 / 下划线 / 斜杠（如 USD/CNY）
InstrumentCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_/]{1,19}$"),
]
#: 概率 / 置信度 / 相似度：0~1（对应 NUMERIC(12,10) 与数据库 CHECK）
ProbabilityValue = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
#: 价格：严格大于 0（对应 NUMERIC(20,8) 与数据库 CHECK）
PriceValue = Annotated[Decimal, Field(gt=Decimal(0))]
#: 理由：必须可复核，长度与 schema 上限一致
RationaleText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_RATIONALE_LENGTH)
]


class AuthorOpinionDraft(BaseModel):
    """单条观点的抽取结果（**入库前**的形态，不含归属与时间字段）。

    与 `docs/10 §5` 的 JSON Schema 严格一致：字段名相同、无额外字段、
    数值范围相同、枚举取值相同。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    stance: OpinionStance
    instrument: InstrumentCode | None = None
    horizon: OpinionHorizon | None = None
    confidence: ProbabilityValue | None = None
    entry_low: PriceValue | None = None
    entry_high: PriceValue | None = None
    stop_loss: PriceValue | None = None
    take_profit: PriceValue | None = None
    information_type: InformationType | None = None
    rationale: RationaleText | None = None
    parser_version: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=PARSER_VERSION_MAX_LENGTH
        ),
    ]

    @model_validator(mode="after")
    def _check_entry_range(self) -> AuthorOpinionDraft:
        """``entry_low <= entry_high``（与数据库 CHECK ``entry_range_ordered`` 对齐）。"""
        if (
            self.entry_low is not None
            and self.entry_high is not None
            and self.entry_low > self.entry_high
        ):
            raise ValueError(
                f"entry_low({self.entry_low}) 不得大于 entry_high({self.entry_high})"
            )
        return self

    @property
    def has_price_plan(self) -> bool:
        """是否包含任何点位信息（用于覆盖率与 confidence 校验）。"""
        return any(
            value is not None
            for value in (self.entry_low, self.entry_high, self.stop_loss, self.take_profit)
        )

    def to_json_schema_example(self) -> dict[str, object]:
        """按 ``exclude_none`` 输出（便于日志与人工比对，绝不吞掉 stance/parser_version）。"""
        return self.model_dump(mode="json", exclude_none=True)
