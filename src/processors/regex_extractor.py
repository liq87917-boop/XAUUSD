"""基于词典 + 正则的 Mock 观点抽取器（``parser_version = "mock-regex-v1"``）。

定位（团队裁决）：Phase 2 先用**可离线、可复现、零成本**的 Mock 抽取器把 Author Lab
管道与 200 条人工标注验收跑通；真实 LLM 提取器后续通过同一 ``OpinionExtractor`` 协议接入
（配置 `DEEPSEEK_API_KEY` 后再做真实联调）。

实现要点（逐条对应 `docs/10 §4` 判定规则）：

1. **文本切分**：句子（。！？；换行 / .!?;）→ 逗号分句 → **观点单元**：
   含方向词的分句开启一个单元，其后**不含**方向词的分句并入该单元
   （于是"2380 附近做多，止损 2365，目标 2450"是**一个**单元，而非三条）。
2. **特殊情形**（一律降级为 UNKNOWN，绝不硬猜）：
   - 条件性（如果/若/一旦…）→ `conditional`
   - 引用他方（机构认为 / according to…；第一人称"我认为"不算引用）→ `quote-only`
   - 事后复盘（昨天/上周/已止盈/复盘）→ `post-hoc`
   - 否定（不看多 / 不再做多）→ `negated_direction`；
     双重否定弱化词（不排除 / 不是不可能）只降 confidence，**不改方向**（docs §4.1.5）。
3. **弱化措辞**（或许/可能/倾向/大概率/预计）只影响 confidence 上限（0.4 / 0.5），不制造方向。
4. **数字**：只有在**价格关键词附近**才视为点位，并排除
   `3.2%`、`2026 年`、`9 月`、`2450 点`、`24.5 万` 这类非价格语境；
   方向不一致的止损/目标会被**丢弃并记 `price_dropped`**（docs §4.4）。
5. **标的**：只在文本**明确提到**时才填（禁止默认 XAUUSD）；同一单元多标的 → 每个标的各出一条。
6. **确定性**：输出顺序 = 句序 → 分句序 → 标的字母序；同输入必然同输出（可复现验收）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from database.models.enums import InformationType, OpinionHorizon, OpinionStance
from src.processors.opinion_extractor import (
    DiagnosticCode,
    ExtractionDiagnostic,
    OpinionExtractionResult,
    build_drafts,
)
from src.processors.schemas import KNOWN_INSTRUMENTS

__all__ = ["RegexOpinionExtractor"]

# ---------------------------------------------------------------------------
# 词典（中文 + 英文；英文一律忽略大小写）
# ---------------------------------------------------------------------------
_LONG_TERMS: tuple[str, ...] = (
    "看多",
    "做多",
    "买入",
    "加仓",
    "仓多",
    "多单",
    "看涨",
    "上破",
    "抄底",
    "long",
    "bullish",
    "buy",
)
_SHORT_TERMS: tuple[str, ...] = (
    "看空",
    "做空",
    "卖出",
    "减仓",
    "仓空",
    "空单",
    "看跌",
    "跌破",
    "下跌",
    "short",
    "bearish",
    "sell",
)
_FLAT_TERMS: tuple[str, ...] = (
    "观望",
    "区间",
    "震荡",
    "不参与",
    "等信号",
    "空仓",
    "离场",
    "已平仓",
    "持平",
    "flat",
    "neutral",
    "wait",
    "range",
)
#: 弱化措辞：只降 confidence（docs/10 §4.3）
_WEAK_TERMS: tuple[str, ...] = (
    "不排除",
    "或许",
    "也许",
    "可能",
    "大概",
    "倾向",
    "大概率",
    "预计",
    "有望",
    "maybe",
    "possibly",
    "perhaps",
    "likely",
    "might",
)
#: 强弱化（confidence ≤ 0.4）：不排除 / 或许 / 可能
_STRONG_WEAK_TERMS: tuple[str, ...] = (
    "不排除",
    "或许",
    "也许",
    "可能",
    "maybe",
    "possibly",
    "might",
)
#: 条件性标记：条件未满足 → UNKNOWN（docs/10 §4.1.1）
_CONDITIONAL_TERMS: tuple[str, ...] = (
    "如果",
    "假如",
    "若",
    "一旦",
    "只要",
    "倘若",
    "除非",
    "if",
    "unless",
)
#: 引用标记：他人观点且作者未表态 → UNKNOWN（docs/10 §4.1.2）
_QUOTE_TERMS: tuple[str, ...] = (
    "认为",
    "表示",
    "称",
    "指出",
    "据",
    "援引",
    "says",
    "said",
    "according to",
    "thinks",
)
#: 第一人称主语（出现在引用标记前 → 是作者自己的观点，不算引用）
_FIRST_PERSON_SUBJECTS: tuple[str, ...] = ("我", "本人", "我们", "我司")
#: 事后复盘标记 → UNKNOWN（docs/10 §4.1.3）
_POST_HOC_TERMS: tuple[str, ...] = (
    "昨天",
    "昨日",
    "前天",
    "上周",
    "此前",
    "复盘",
    "回顾",
    "已止盈",
    "已经止盈",
    "已止损",
    "收益率",
    "yesterday",
    "last week",
    "earlier",
)
#: 否定词（紧邻方向词前 → 方向作废，而不是反转为相反方向）
_NEGATION_TERMS: tuple[str, ...] = ("不", "没", "没有", "未", "别", "不再", "不是", "勿")
#: 图片/图表指代词：无方向词时视为"图片无文字"样本（docs/10 §2.2）
_IMAGE_TERMS: tuple[str, ...] = (
    "看图",
    "见图",
    "如上图",
    "下图",
    "图表",
    "看图操作",
    "see chart",
    "chart below",
)

#: 标的别名 → instruments 白名单代码（只在文本明确提到时使用）
_INSTRUMENT_ALIASES: tuple[tuple[str, str], ...] = (
    ("现货黄金", "XAUUSD"),
    ("黄金", "XAUUSD"),
    ("金价", "XAUUSD"),
    ("xauusd", "XAUUSD"),
    ("gold", "XAUUSD"),
    ("白银", "XAGUSD"),
    ("银价", "XAGUSD"),
    ("xagusd", "XAGUSD"),
    ("silver", "XAGUSD"),
    ("纽约金", "COMEX_GC"),
    ("comex", "COMEX_GC"),
    ("沪金", "SGE_AU9999"),
    ("上金所", "SGE_AU9999"),
    ("au9999", "SGE_AU9999"),
    ("美元指数", "DXY"),
    ("美指", "DXY"),
    ("dxy", "DXY"),
    ("十年期美债", "US10Y"),
    ("10年期美债", "US10Y"),
    ("美债收益率", "US10Y"),
    ("us10y", "US10Y"),
    ("两年期美债", "US02Y"),
    ("us02y", "US02Y"),
    ("实际利率", "US10Y_REAL"),
    ("tips", "US10Y_REAL"),
    ("美元兑人民币", "USDCNY"),
    ("人民币", "USDCNY"),
    ("usdcny", "USDCNY"),
    ("原油", "WTI"),
    ("wti", "WTI"),
    ("oil", "WTI"),
    ("比特币", "BTCUSD"),
    ("btc", "BTCUSD"),
    ("bitcoin", "BTCUSD"),
)

#: 信息类型判定顺序（docs/10 §4.5：MACRO > NEWS > POSITIONING > SENTIMENT > TECHNICAL）
_INFO_TYPE_RULES: tuple[tuple[InformationType, tuple[str, ...]], ...] = (
    (
        InformationType.MACRO,
        (
            "cpi",
            "pce",
            "非农",
            "议息",
            "利率决议",
            "美联储",
            "央行",
            "鲍威尔",
            "降息",
            "加息",
            "点阵图",
            "fomc",
            "fed",
            "inflation",
            "gdp",
            "失业率",
            "国债收益率",
        ),
    ),
    (
        InformationType.NEWS,
        (
            "消息",
            "传闻",
            "突发",
            "地缘",
            "战争",
            "制裁",
            "关税",
            "大选",
            "纪要",
            "财报",
            "news",
            "breaking",
            "sanction",
            "tariff",
            "election",
            "minutes",
        ),
    ),
    (
        InformationType.POSITIONING,
        ("持仓", "净多头", "etf", "资金流", "增仓", "spdr", "positioning", "flows", "库存"),
    ),
    (
        InformationType.SENTIMENT,
        ("情绪", "恐慌", "贪婪", "risk-on", "risk off", "避险情绪", "sentiment", "fear", "greed"),
    ),
    (
        InformationType.TECHNICAL,
        (
            "均线",
            "macd",
            "rsi",
            "支撑",
            "阻力",
            "趋势线",
            "形态",
            "k线",
            "布林",
            "斐波",
            "指标",
            "technical",
            "support",
            "resistance",
            "breakout",
            "moving average",
        ),
    ),
)

_ENTRY_MARKERS: tuple[str, ...] = (
    "入场",
    "进场",
    "做多",
    "做空",
    "买入",
    "卖出",
    "多单",
    "空单",
    "附近",
    "区间",
    "long",
    "short",
    "entry",
    "buy",
    "sell",
    "from",
    "at",
    "around",
    "near",
)
_STOP_MARKERS: tuple[str, ...] = (
    "止损",
    "停损",
    "stop loss",
    "stop-loss",
    "stoploss",
    "stop",
    "sl",
)
_TARGET_MARKERS: tuple[str, ...] = (
    "目标",
    "止盈",
    "看至",
    "看向",
    "take profit",
    "target",
    "tp",
)

#: 价格数字：2~7 位整数（可带小数），且不是更长数字的一部分
_NUMBER_RE = re.compile(r"(?<![\d.])(\d{2,7}(?:\.\d{1,4})?)(?![\d])")
#: 数字后紧跟这些单位/语境时**不是**价格（CPI 3.2%、2026 年、9 月、2450 点、24.5 万）
_NON_PRICE_SUFFIX_RE = re.compile(r"^\s*(?:%|％|年|月|日|点|万|亿|倍|个|次|手|人|吨)")
#: 价格区间：2380-2395 / 2380~2395 / 2380 至 2395 / 2380 到 2395
_RANGE_RE = re.compile(
    r"(?<![\d.])(\d{2,7}(?:\.\d{1,4})?)\s*(?:-|~|—|－|至|到)\s*(\d{2,7}(?:\.\d{1,4})?)(?![\d])"
)
#: 句子分隔（强分隔符）
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+|(?<=[.!?])\s+")
#: 分句分隔（逗号类）
_CLAUSE_SPLIT_RE = re.compile(r"[，,、]")
#: rationale 原文片段上限（docs/10 §4.6：≤120 字）
_RATIONALE_LIMIT = 120
#: 价格关键词附近允许的**紧凑**间距（字符）；超过则视为"未明确给出"而留空
_TIGHT_GAP = 2
#: 扫描半径：仅用于"找出候选数字"，是否采纳由 `_TIGHT_GAP` 决定（数字本身最长 12 字符）
_SCAN_RADIUS = 16
#: 显式标的代码形状（避免把 COVID 这类大写词误当标的）：
#: 三个字母 + 报价货币（XPTUSD/XAUUSD/USDCNY…）、美债代码（US10Y / US10Y_REAL）、
#: 上金所（SGE_AU9999）、COMEX（COMEX_GC）
_CODE_PATTERN = re.compile(
    r"\b(?:[A-Z]{3}(?:USD|CNY|EUR|JPY|GBP)|US\d{2}Y(?:_REAL)?|SGE_AU\d{4}|COMEX_[A-Z]{2})\b"
)


@dataclass(frozen=True, slots=True)
class _Unit:
    """一个"观点单元"：一个方向词 + 其后并入的分句。"""

    sentence_index: int
    clause_index: int
    stance: OpinionStance
    sentence_text: str
    text: str
    negated: bool
    weak: bool
    weak_strong: bool
    conditional: bool
    quoted: bool
    post_hoc: bool

    @property
    def order_key(self) -> tuple[int, int]:
        return (self.sentence_index, self.clause_index)

    @property
    def markers(self) -> tuple[str, ...]:
        """rationale 标记词（docs/10 §4.6）。"""
        marks: list[str] = []
        if self.conditional:
            marks.append("conditional")
        if self.quoted:
            marks.append("quote-only")
        if self.post_hoc:
            marks.append("post-hoc")
        if self.negated:
            marks.append("negated")
        return tuple(marks)


def _marker_positions(text: str, marker: str) -> list[int]:
    """返回关键词在文本中的所有出现位置。

    - 纯 ASCII 关键词（stop / buy / at…）使用 ``\\b`` 词边界匹配，
      避免 "at" 命中 "target" 这类子串误匹配；
    - 含中文的关键词直接用 ``str.find`` 循环（中文无需词边界）。
    """
    if marker.isascii():
        pattern = re.compile(r"\b" + re.escape(marker) + r"\b", re.IGNORECASE)
        return [match.start() for match in pattern.finditer(text)]

    positions: list[int] = []
    lowered = text.lower()
    needle = marker.lower()
    start = 0
    while True:
        found = lowered.find(needle, start)
        if found < 0:
            return positions
        positions.append(found)
        start = found + 1


def _find_first(text: str, terms: tuple[str, ...]) -> tuple[str, int] | None:
    """返回最先出现的词及其位置（英文按词边界匹配，避免子串误命中）。"""
    best: tuple[str, int] | None = None
    for term in terms:
        positions = _marker_positions(text, term)
        if positions and (best is None or positions[0] < best[1]):
            best = (term, positions[0])
    return best


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    return _find_first(text, terms) is not None


def _detect_stance(text: str) -> OpinionStance | None:
    """方向判定：方向词优先于"区间/震荡"这类对冲词（docs/10 §4.1.5 的口径）。"""
    long_hit = _find_first(text, _LONG_TERMS)
    short_hit = _find_first(text, _SHORT_TERMS)
    if long_hit and short_hit:
        return OpinionStance.LONG if long_hit[1] <= short_hit[1] else OpinionStance.SHORT
    if long_hit:
        return OpinionStance.LONG
    if short_hit:
        return OpinionStance.SHORT
    if _matches(text, _FLAT_TERMS):
        return OpinionStance.FLAT
    return None


def _direction_term(stance: OpinionStance, text: str) -> str | None:
    """取该方向在文本中实际命中的方向词（用于否定判定）。"""
    terms = _LONG_TERMS if stance is OpinionStance.LONG else _SHORT_TERMS
    hit = _find_first(text, terms)
    return hit[0] if hit else None


def _is_negated(text: str, term: str) -> bool:
    """方向词前是否紧邻否定词（"不看多" → 方向作废；"不排除上行" → 弱化，不算否定）。"""
    lowered = text.lower()
    position = lowered.find(term.lower())
    if position <= 0:
        return False
    prefix = lowered[max(0, position - 3) : position]
    if any(weak in prefix for weak in ("不排除", "不是不", "未必不", "不否认")):
        return False
    return any(prefix.endswith(negation) for negation in _NEGATION_TERMS)


def _detect_horizon(*texts: str) -> OpinionHorizon | None:
    """周期识别（按 `docs/10 §4.2` 词典，逐优先级匹配；都未命中则 None）。"""
    for text in texts:
        for pattern, horizon in _HORIZON_PATTERNS:
            if pattern.search(text):
                return horizon
    return None


def _detect_information_type(
    text: str, *, stance: OpinionStance, has_price_plan: bool
) -> InformationType | None:
    """信息类型（优先级见 `docs/10 §4.5`）；无任何线索时按诚实原则取 OTHER / None。"""
    for info_type, terms in _INFO_TYPE_RULES:
        if _matches(text, terms):
            return info_type
    if has_price_plan:
        return InformationType.TECHNICAL
    if stance is not OpinionStance.UNKNOWN:
        return InformationType.OTHER
    return None


def _detect_instruments(text: str) -> tuple[str, ...]:
    """按出现位置返回文本中**明确提到**的标的代码（绝不默认 XAUUSD）。

    两条来源：
    1. 中文/英文别名（黄金 → XAUUSD 等）；
    2. **显式代码**（XAUUSD / XPTUSD / US10Y / SGE_AU9999 / COMEX_GC…），
       但只接受"看起来像标的"的形状，避免把 COVID 这类大写词当标的。
    未命中白名单的显式代码**不拒绝**（可能是新标的），由调用方产生告警。
    """
    lowered = text.lower()
    positions: dict[str, int] = {}
    for alias, symbol in _INSTRUMENT_ALIASES:
        position = lowered.find(alias.lower())
        if position >= 0 and (symbol not in positions or position < positions[symbol]):
            positions[symbol] = position
    for match in _CODE_PATTERN.finditer(text):
        code = match.group(0)
        positions.setdefault(code, match.start())
    return tuple(
        symbol for symbol, _ in sorted(positions.items(), key=lambda item: (item[1], item[0]))
    )


def _plausible_price(text: str, match: re.Match[str]) -> Decimal | None:
    """数字是否可视为行情价格（排除 %/年/月/日/点/万 等非价格语境与不合理量级）。"""
    if _NON_PRICE_SUFFIX_RE.match(text[match.end() : match.end() + 2]):
        return None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:  # pragma: no cover - 正则已保证可解析
        return None
    if value < Decimal("1") or value > Decimal("1000000"):
        return None
    return value


def _price_candidates(
    text: str, markers: tuple[str, ...], consumed: list[tuple[int, int]]
) -> list[tuple[int, int, Decimal, tuple[int, int]]]:
    """收集价格关键词附近的**紧凑候选**（间距 ≤ `_TIGHT_GAP`），按 (间距, 前/后, 数值) 排序。

    为什么要求"紧凑"（≤2 字符）：多点位文本（"2380 做多，2360 止损，2450 目标"）里，
    仅凭"距离最近"无法区分数字归属，任何猜测都会污染研究数据。docs/10 的原则是
    **宁可留空，不可猜错**；真正的消歧交给方向一致性筛选（见 `_first_consistent`）。
    """
    found: dict[tuple[int, int], tuple[int, int, Decimal, tuple[int, int]]] = {}
    for marker in markers:
        for occurrence in _marker_positions(text, marker):
            marker_end = occurrence + len(marker)
            window_start = max(0, occurrence - _SCAN_RADIUS)
            window_end = min(len(text), marker_end + _SCAN_RADIUS)
            for match in _NUMBER_RE.finditer(text, window_start, window_end):
                span = (match.start(), match.end())
                if any(low <= span[0] < high for low, high in consumed):
                    continue
                value = _plausible_price(text, match)
                if value is None:
                    continue
                if span[0] >= marker_end:
                    gap, after_rank = span[0] - marker_end, 0
                elif span[1] <= occurrence:
                    gap, after_rank = occurrence - span[1], 1
                else:  # 数字与关键词重叠（异常文本）：跳过，不猜
                    continue
                if gap > _TIGHT_GAP:
                    continue
                key = (gap, after_rank)
                if span not in found or key < found[span][:2]:
                    found[span] = (gap, after_rank, value, span)
    return sorted(found.values(), key=lambda item: (item[0], item[1], item[2]))


def _values(candidates: list[tuple[int, int, Decimal, tuple[int, int]]]) -> str:
    """候选数值的可读列表（仅用于诊断信息）。"""
    return "/".join(str(candidate[2]) for candidate in candidates)


def _first_consistent(
    candidates: list[tuple[int, int, Decimal, tuple[int, int]]],
    *,
    reference: Decimal | None,
    above: bool,
) -> tuple[int, int, Decimal, tuple[int, int]] | None:
    """按候选顺序取第一个**与方向一致**的点位（无参考价时取第一个候选）。

    - ``above=True``：该字段应高于参考价（止损之于 SHORT、目标之于 LONG）；
    - ``above=False``：该字段应低于参考价（止损之于 LONG、目标之于 SHORT）。
    """
    for candidate in candidates:
        value = candidate[2]
        if reference is None or (value > reference if above else value < reference):
            return candidate
    return None


def _extract_range(
    text: str, consumed: list[tuple[int, int]]
) -> tuple[Decimal, Decimal, tuple[int, int]] | None:
    """提取"2380-2395 / 2380 至 2395"形式的入场区间。"""
    for match in _RANGE_RE.finditer(text):
        span = (match.start(), match.end())
        if any(low <= span[0] < high for low, high in consumed):
            continue
        try:
            low_value = Decimal(match.group(1))
            high_value = Decimal(match.group(2))
        except InvalidOperation:  # pragma: no cover - 正则已保证可解析
            continue
        if low_value <= 0 or high_value <= 0 or low_value > high_value:
            continue
        if low_value > Decimal("1000000") or high_value > Decimal("1000000"):
            continue
        return low_value, high_value, span
    return None


def _is_quoted(text: str, terms: tuple[str, ...]) -> bool:
    """引用判定：引用标记前 3 字若是第一人称（我/本人），则是作者自己的观点。"""
    hit = _find_first(text, terms)
    if hit is None:
        return False
    _, position = hit
    prefix = text[max(0, position - 3) : position]
    return not any(subject in prefix for subject in _FIRST_PERSON_SUBJECTS)


def _snippet(text: str, limit: int = _RATIONALE_LIMIT) -> str:
    """原文片段（折叠空白 + 截断，供人工复核）。"""
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def _make_unit(sentence_index: int, clause_index: int, clauses: list[str], sentence: str) -> _Unit:
    """把"方向分句 + 其后并入的分句"组装成一个观点单元。"""
    unit_text = "，".join(clauses)
    stance = _detect_stance(unit_text)
    assert stance is not None  # 由 _build_units 保证：只有含方向词的分句才会建单元

    scope = f"{sentence} {unit_text}"
    negated = False
    if stance in (OpinionStance.LONG, OpinionStance.SHORT):
        term = _direction_term(stance, unit_text)
        negated = term is not None and _is_negated(unit_text, term)

    conditional = _matches(sentence, _CONDITIONAL_TERMS)
    quoted = _is_quoted(sentence, _QUOTE_TERMS)
    post_hoc = _matches(sentence, _POST_HOC_TERMS)
    # 特殊情形下 rationale 记完整句子（条件句必须可复核，docs/10 附录 B-2）
    text = sentence if (conditional or quoted or post_hoc) else unit_text
    return _Unit(
        sentence_index=sentence_index,
        clause_index=clause_index,
        stance=stance,
        sentence_text=sentence,
        text=text,
        negated=negated,
        weak=_matches(scope, _WEAK_TERMS),
        weak_strong=_matches(scope, _STRONG_WEAK_TERMS),
        conditional=conditional,
        quoted=quoted,
        post_hoc=post_hoc,
    )


def _build_units(text: str) -> tuple[_Unit, ...]:
    """句子 → 分句 → 观点单元（顺序稳定：句序 → 分句序）。"""
    units: list[_Unit] = []
    for sentence_index, raw_sentence in enumerate(_SENTENCE_SPLIT_RE.split(text)):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        clauses = [
            clause.strip() for clause in _CLAUSE_SPLIT_RE.split(sentence) if clause.strip()
        ]
        pending: list[str] = []
        pending_index = 0
        for clause_index, clause in enumerate(clauses):
            if _detect_stance(clause) is not None:
                if pending:
                    units.append(_make_unit(sentence_index, pending_index, pending, sentence))
                pending = [clause]
                pending_index = clause_index
            elif pending:
                pending.append(clause)
        if pending:
            units.append(_make_unit(sentence_index, pending_index, pending, sentence))
    return tuple(units)


def _resolve_instruments(unit: _Unit) -> tuple[str | None, ...]:
    """标的解析：单元内 → 句内唯一标的 → 不填（禁止默认 XAUUSD）。"""
    in_unit = _detect_instruments(unit.text)
    if in_unit:
        return in_unit
    in_sentence = _detect_instruments(unit.sentence_text)
    if len(in_sentence) == 1:
        return in_sentence
    return (None,)


def _score_confidence(
    stance: OpinionStance, unit: _Unit, *, has_price_plan: bool
) -> Decimal | None:
    """confidence 只反映**文本强度**，不评价作者水平（docs/10 §4.3）。"""
    if stance is OpinionStance.UNKNOWN:
        return None
    if stance is OpinionStance.FLAT:
        base = Decimal("0.6")
    else:
        base = Decimal("0.9") if has_price_plan else Decimal("0.7")
    if unit.weak_strong:
        return min(base, Decimal("0.4"))
    if unit.weak:
        return min(base, Decimal("0.5"))
    return base


_MARKER_MESSAGES: dict[DiagnosticCode, str] = {
    DiagnosticCode.CONDITIONAL: "条件性观点（条件未满足）→ 按 docs/10 §4.1.1 记 UNKNOWN",
    DiagnosticCode.QUOTE_ONLY: "引用他方观点且作者未表态 → 按 docs/10 §4.1.2 记 UNKNOWN",
    DiagnosticCode.POST_HOC: "事后复盘不构成前瞻观点 → 按 docs/10 §4.1.3 记 UNKNOWN",
    DiagnosticCode.NEGATED_DIRECTION: "方向被否定（不看多/不再做多）→ 按 docs/10 §4.1 记 UNKNOWN",
}


def _build_rationale(unit: _Unit, snippet: str) -> str:
    """原文片段 + 特殊标记词（docs/10 §4.6：必须可复核）。"""
    markers = unit.markers
    if markers:
        return f"{'/'.join(markers)}：{snippet}"
    return snippet


def _build_payload(
    unit: _Unit,
    instrument: str | None,
    *,
    parser_version: str,
    diagnostics: list[ExtractionDiagnostic],
    warnings: list[str],
) -> dict[str, object]:
    """把观点单元转成契约 payload（非法组合在此被剔除或降级，绝不带病入库）。"""
    snippet = _snippet(unit.text)
    forced_unknown = unit.negated or unit.conditional or unit.quoted or unit.post_hoc
    stance = OpinionStance.UNKNOWN if forced_unknown else unit.stance
    for code, flag in (
        (DiagnosticCode.CONDITIONAL, unit.conditional),
        (DiagnosticCode.QUOTE_ONLY, unit.quoted),
        (DiagnosticCode.POST_HOC, unit.post_hoc),
        (DiagnosticCode.NEGATED_DIRECTION, unit.negated),
    ):
        if flag:
            diagnostics.append(ExtractionDiagnostic(code, _MARKER_MESSAGES[code], snippet))

    consumed: list[tuple[int, int]] = []
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    if stance in (OpinionStance.LONG, OpinionStance.SHORT):
        extracted = _extract_range(unit.text, consumed)
        if extracted is not None:
            entry_low, entry_high, span = extracted
            consumed.append(span)

        # 先算出"止损 / 目标"候选，用于给入场价让路：
        # 中文常见写法 "2380 做多，2360 止损" 里，2360 同时紧邻"做多"与"止损"，
        # 若先挑入场价会把止损抢走 —— 因此入场价优先选**没有被止损/目标认领**的数字。
        stop_candidates = _price_candidates(unit.text, _STOP_MARKERS, consumed)
        claimed = {candidate[3] for candidate in stop_candidates}
        claimed |= {
            candidate[3]
            for candidate in _price_candidates(unit.text, _TARGET_MARKERS, consumed)
        }
        if entry_low is None:
            entry_candidates = _price_candidates(unit.text, _ENTRY_MARKERS, consumed)
            preferred = [
                candidate for candidate in entry_candidates if candidate[3] not in claimed
            ] or entry_candidates
            if preferred:
                entry_low = entry_high = preferred[0][2]
                consumed.append(preferred[0][3])

        reference = entry_low if entry_low is not None else entry_high
        stop = _first_consistent(
            stop_candidates, reference=reference, above=stance is OpinionStance.SHORT
        )
        if stop is not None:
            stop_loss = stop[2]
            consumed.append(stop[3])
        elif stop_candidates:
            diagnostics.append(
                ExtractionDiagnostic(
                    DiagnosticCode.PRICE_DROPPED,
                    f"止损候选({_values(stop_candidates)}) 与 {stance.value} 方向不一致，"
                    "已丢弃（docs/10 §4.4）",
                    snippet,
                )
            )

        target_candidates = _price_candidates(unit.text, _TARGET_MARKERS, consumed)
        target = _first_consistent(
            target_candidates, reference=reference, above=stance is OpinionStance.LONG
        )
        if target is not None:
            take_profit = target[2]
            consumed.append(target[3])
        elif target_candidates:
            diagnostics.append(
                ExtractionDiagnostic(
                    DiagnosticCode.PRICE_DROPPED,
                    f"目标候选({_values(target_candidates)}) 与 {stance.value} 方向不一致，"
                    "已丢弃（docs/10 §4.4）",
                    snippet,
                )
            )
    elif stance is OpinionStance.FLAT:
        extracted = _extract_range(unit.text, consumed)
        if extracted is not None:
            entry_low, entry_high, _span = extracted

    has_price_plan = any(
        value is not None for value in (entry_low, entry_high, stop_loss, take_profit)
    )
    if unit.weak and stance is not OpinionStance.UNKNOWN:
        diagnostics.append(
            ExtractionDiagnostic(
                DiagnosticCode.WEAK_LANGUAGE,
                "文本含弱化措辞（可能/不排除/倾向…）→ confidence 已按 docs/10 §4.3 封顶",
                snippet,
            )
        )
    if instrument is not None and instrument not in KNOWN_INSTRUMENTS:
        warnings.append(
            f"标的 {instrument} 不在 instruments 白名单内：需先补种子/白名单（数据库外键兜底）"
        )

    horizon = _detect_horizon(unit.text, unit.sentence_text)
    # 信息类型按**句子**的主要驱动判定：观点常由同一句里的宏观数据/消息引出
    # （例如"CPI 高于预期，黄金看多"的驱动是 MACRO，而不是价位本身）
    info_type = _detect_information_type(
        unit.sentence_text or unit.text, stance=stance, has_price_plan=has_price_plan
    )
    return {
        "stance": stance.value,
        "instrument": instrument,
        "horizon": horizon.value if horizon is not None else None,
        "confidence": _score_confidence(stance, unit, has_price_plan=has_price_plan),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "information_type": info_type.value if info_type is not None else None,
        "rationale": _build_rationale(unit, snippet),
        "parser_version": parser_version,
    }


#: Mock 抽取器版本（入库到 `author_opinions.parser_version`；升级需递增）
DEFAULT_PARSER_VERSION = "mock-regex-v1"


class RegexOpinionExtractor:
    """词典 + 正则实现的 Mock 观点抽取器（确定性、零依赖、零网络）。

    用途：在真实 LLM 接入之前，把 Phase 2 的「Post → Opinion」链路与 200 条人工标注
    验收流程跑通（团队裁决：Mock 优先，禁止现在调用真实付费 API）。

    用法::

        extractor = RegexOpinionExtractor()
        result = extractor.extract("2380 附近做多，止损 2365，目标 2450，日内短线")
        result.drafts[0].stance     # OpinionStance.LONG
        result.drafts[0].horizon    # OpinionHorizon.H1
    """

    def __init__(self, *, parser_version: str = DEFAULT_PARSER_VERSION) -> None:
        self.parser_version = parser_version

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        """抽取 0..N 条观点（空文本 / 无方向词 / 图片无文字均返回空 drafts + 诊断）。"""
        diagnostics: list[ExtractionDiagnostic] = []
        if not isinstance(text, str) or not text.strip():
            diagnostics.append(
                ExtractionDiagnostic(DiagnosticCode.EMPTY_TEXT, "文本为空，无观点可抽取", None)
            )
            return OpinionExtractionResult(self.parser_version, (), tuple(diagnostics), ())

        units = _build_units(text)
        if not units:
            if _matches(text, _IMAGE_TERMS) or has_media:
                diagnostics.append(
                    ExtractionDiagnostic(
                        DiagnosticCode.IMAGE_ONLY,
                        "仅有图片/图表指代，无文字方向判断（后续 OCR 阶段处理）",
                        _snippet(text),
                    )
                )
            else:
                diagnostics.append(
                    ExtractionDiagnostic(
                        DiagnosticCode.NO_STANCE_KEYWORD,
                        "未发现方向性表述 → 本条为无观点样本（no_opinion=true）",
                        _snippet(text),
                    )
                )
            return OpinionExtractionResult(self.parser_version, (), tuple(diagnostics), ())

        warnings: list[str] = []
        payloads: list[dict[str, object]] = []
        for unit in units:
            for instrument in _resolve_instruments(unit):
                payloads.append(
                    _build_payload(
                        unit,
                        instrument,
                        parser_version=self.parser_version,
                        diagnostics=diagnostics,
                        warnings=warnings,
                    )
                )

        drafts, invalid = build_drafts(payloads, parser_version=self.parser_version)
        diagnostics.extend(invalid)
        return OpinionExtractionResult(
            self.parser_version, drafts, tuple(diagnostics), tuple(dict.fromkeys(warnings))
        )
_HORIZON_PATTERNS: tuple[tuple[re.Pattern[str], OpinionHorizon], ...] = (
    (re.compile(r"超短|几分钟|分钟级|分时|scalp|minute", re.IGNORECASE), OpinionHorizon.M15),
    (re.compile(r"半小时|30\s*分钟|30min|half an hour", re.IGNORECASE), OpinionHorizon.M30),
    (re.compile(r"日内|短线|当天|今日|intraday|short[- ]term", re.IGNORECASE), OpinionHorizon.H1),
    (
        re.compile(r"半天|几小时|数小时|美盘|欧盘|亚盘|session|hours", re.IGNORECASE),
        OpinionHorizon.H4,
    ),
    (
        re.compile(
            r"中线|长线|本周|下周|未来几天|数日|趋势|波段|收盘|日线|swing|week|daily",
            re.IGNORECASE,
        ),
        OpinionHorizon.D1,
    ),
)