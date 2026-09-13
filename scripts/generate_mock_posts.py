"""Phase 2 Mock 博文数据生成器（**合成演练数据，绝不冒充真实数据**）。

对应文档：
- `docs/10_标注规范.md` §2.2 分层要求（长度三档大致三等分 / 单一来源 ≤40% /
  含媒体 ≥20 条 / ≥5 个交易日）
- `docs/10_标注规范.md` §3 时间语义（``effective_at = max(published_at, collected_at)``）
- `docs/10_标注规范.md` §4 判定规则（方向 / 周期 / 点位方向一致性 /
  条件句 / 引用 / 复盘 / 双重否定）
- `.clinerules`：严禁伪造**标注**——本脚本只生成"待标注的原始文本"，判定列一律留空。

为什么需要本脚本：
    作者库（`author_posts`）在 Phase 2 第一步仍是空的（TD-16 / TD-20），
    而 `docs/08 §5` 的 200 条人工标注必须有输入。本脚本生成**结构完整、时间自洽**的
    合成博文并写入 `logs/posts.csv`，作为 `scripts.sample_annotation_set.py --input-csv` 的输入。

批内容（默认 250 条，全部确定性可复现）：
- 4 个已注册 NEWS 来源（`jin10_flash` / `sina_finance_gold` / `investing_news` /
  `fed_press_releases`）均衡分布、覆盖 10 个交易日；长度三档（短/中/长）大致 4:3:3；
- 含媒体样本 ≥20 条（其中"图表无文字"对抗样本一定带图）；
- **对抗样本 8 类 × 13 条**（条件性 / 引用他人 / 事后复盘 / 双重否定 / 点位方向不一致 /
  纯信息播报 / 反问反讽 / 图表无文字），对应 `docs/10 §2.2` 的对抗样本表与 §4.1/§4.4 规则，
  保证 200 条抽样后每类 ≥10 条（有测试锁定）；
- 末尾 5 条"同文转载"（跨来源 + 更晚时间）用于演练内容去重。

红线（务必遵守）：
1. 合成数据的标识是**强制的**：`is_mock=true` + `mock_batch=<批次号>` 写在每一行，
   且批号与"自动生成"事实会写进抽样元数据（`annotation_sample.meta.json`）；
2. 本脚本**不生成任何标注结论**（`stance` / `horizon` / 点位 / 信息类型等一律留空，
   由人工按 `docs/10` 填写）；
3. 合成数据只用于**把管道与标注流程跑通**与回归演练；任何研究结论、模型训练与准确率结论
   都必须用真实数据复跑（本脚本输出的目录 `logs/` 已被 `.gitignore` 忽略，不会入 Git）。

用法::

    # 生成默认 250 条到 logs/posts.csv（已存在则拒绝覆盖，保护真实数据）
    python -m scripts.generate_mock_posts
    # 自定义条数 / 时间窗口 / 允许覆盖
    python -m scripts.generate_mock_posts --count 300 --window-start 2026-08-24 --days 10 --force
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种运行方式（同 `scripts/sample_annotation_set.py`）：
#   1) 脚本模式 `python scripts/generate_mock_posts.py` —— 需要仓库根在 sys.path 上，
#      否则 `from scripts._console import ...` 会 ModuleNotFoundError；
#   2) 包模式 `from scripts.generate_mock_posts import ...`（测试 / mypy）。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 已安装（editable）时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
import argparse
import csv
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from scripts._console import configure_stdout
from src.common.hashing import content_hash, normalize_for_hash
from src.common.time import resolve_effective_at, utc_now

__all__ = [
    "DEFAULT_POST_COUNT",
    "DEFAULT_POSTS_CSV",
    "DEFAULT_SEED",
    "MOCK_ACCOUNTS",
    "MOCK_BATCH",
    "MockPost",
    "POST_COLUMNS",
    "configure_stdout",
    "generate_mock_posts",
    "main",
    "write_posts_csv",
]

#: 默认输出（也是 `docs/10 §2.2` 约定的演练输入路径）
DEFAULT_POSTS_CSV: Final[Path] = REPO_ROOT / "logs" / "posts.csv"
DEFAULT_POST_COUNT: Final[int] = 250
DEFAULT_SEED: Final[int] = 20260912
#: 批次号：出现在每行的 `mock_batch` 列与抽样元数据里（可追溯"谁是合成数据"）
MOCK_BATCH: Final[str] = "mock-synthetic-v1"
#: 时间窗口起点（必须落在**过去**，见 `_assert_past_window`）
DEFAULT_WINDOW_START: Final[date] = date(2026, 8, 24)
DEFAULT_TRADING_DAYS: Final[int] = 10
#: 末尾几条故意做成"转载同文"，用于演练内容去重（`docs/10 §2.2`）
DEFAULT_REPOST_COUNT: Final[int] = 5
_REPOST_ORIGINALS: Final[tuple[int, ...]] = (9, 59, 109, 159, 209)

#: 作者账号 → 来源名（来源取自 `database/seeds/sources.py` 的**已注册 NEWS 来源**；
#: 刻意**不含 `weibo_main`**——团队裁决：Phase 2 不采集微博，Mock 数据也不做示范）
MOCK_ACCOUNTS: Final[tuple[tuple[str, str], ...]] = (
    ("金十数据快讯", "jin10_flash"),
    ("新浪财经黄金频道", "sina_finance_gold"),
    ("投研社-黄金策略组", "investing_news"),
    ("宏观速递-FOMC观察", "fed_press_releases"),
)

#: CSV 列：前 6 列是**人读友好别名**（`id` / `source` / `content`）+ 规范列，
#: 两者内容完全一致（`tests/unit/test_mock_posts_generator.py` 断言不漂移）；
#: `post_id` / `text_content` / `source_name` / `effective_at` 是抽样脚本真正读取的列。
POST_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "post_id",
    "source",
    "source_name",
    "content",
    "text_content",
    "raw_item_id",
    "author_id",
    "author_name",
    "published_at",
    "collected_at",
    "effective_at",
    "time_precision",
    "length_bucket",
    "has_media",
    "media_type",
    "language",
    "content_hash",
    "url",
    "platform",
    "edge_case",
    "repost_of",
    "is_mock",
    "mock_batch",
)

#: 长度分档循环（10 行一个周期：4 短 / 3 中 / 3 长），保证 `docs/10 §2.2` 的三档配额
_BUCKET_CYCLE: Final[tuple[str, ...]] = (
    "short",
    "short",
    "short",
    "short",
    "medium",
    "medium",
    "medium",
    "long",
    "long",
    "long",
)
_BUCKET_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "short": (1, 50),
    "medium": (51, 200),
    "long": (201, 1200),
}
#: 开仓时段（Asia/Shanghai，模拟真实的盘中发布节奏）
_SESSION_TIMES: Final[tuple[tuple[str, int, int], ...]] = (
    ("亚盘", 7, 45),
    ("亚盘", 9, 20),
    ("亚盘", 10, 5),
    ("亚盘", 11, 35),
    ("欧盘", 14, 10),
    ("欧盘", 15, 50),
    ("欧盘", 16, 30),
    ("美盘", 20, 15),
    ("美盘", 21, 5),
    ("美盘", 22, 40),
)
#: 方向池（约 3:3:1）
_STANCE_CYCLE: Final[tuple[str, ...]] = (
    "LONG",
    "LONG",
    "LONG",
    "SHORT",
    "SHORT",
    "SHORT",
    "FLAT",
)
#: 各方向的措辞（中文 + 英文混排；与 `src/processors/regex_extractor.py` 的词典同源）
_STANCE_WORDS: Final[dict[str, tuple[str, str]]] = {
    "LONG": ("做多", "long / bullish"),
    "SHORT": ("做空", "short / bearish"),
    "FLAT": ("观望", "flat / range"),
}
#: 周期线索（写入文本，供抽取器与人工判定）：(中文线索, 期望周期)
_HORIZON_HINTS: Final[tuple[tuple[str, str], ...]] = (
    ("超短线，分钟级操作", "15m"),
    ("30 分钟图上的机会", "30m"),
    ("日内交易", "1h"),
    ("4 小时级别", "4h"),
    ("日线中线趋势", "1d"),
    ("本周波段", "1d"),
)
#: 驱动因素（覆盖 MACRO / NEWS / POSITIONING / SENTIMENT / TECHNICAL 五类信息类型）
_DRIVERS: Final[tuple[str, ...]] = (
    "美联储降息预期升温，实际利率回落",
    "CPI 数据高于预期，市场重新定价利率路径",
    "地缘消息扰动，避险情绪升温",
    "美元指数走弱，DXY 跌破关键支撑",
    "ETF 持仓连续三日增仓，资金流回暖",
    "日线均线与 MACD 同步金叉",
    "美债收益率回落，支撑金价估值",
    "投机净多头持仓回落至中性区间",
    "risk-on 情绪主导，黄金短线承压",
    "突发消息引发波动率跳升",
)
#: 方向词（与 `src/processors/regex_extractor.py` 的词典保持一致的**超集**）：
#: 用于自检"该有方向的样本必须有方向、该无方向的样本必须干净"
_DIRECTION_WORDS: Final[tuple[str, ...]] = (
    "做多",
    "做空",
    "观望",
    "看多",
    "看空",
    "看涨",
    "看跌",
    "买入",
    "卖出",
    "多单",
    "空单",
    "加仓",
    "减仓",
    "抄底",
    "上破",
    "跌破",
    "flat",
    "long",
    "short",
    "bullish",
    "bearish",
    "buy",
    "sell",
)
#: 长文本补充句（只做"客观描述"，**全部不含方向词**；既有方向样本与无方向样本都用它填充）
_FILLERS: Final[tuple[str, ...]] = (
    "从结构上看，价格仍在震荡区间内运行。",
    "当前关键在于美盘开盘后的成交量能否放大。",
    "上方阻力与下方支撑相对清晰，暂未出现假突破。",
    "宏观层面需继续跟踪实际利率与美元指数的联动。",
    "数据公布前后波动率往往快速抬升，注意仓位管理。",
    "若价格与预期相反，先降低仓位再等新的信号。",
    "本文仅为个人研究记录，不构成任何投资建议。",
    "行情数据来自公开渠道，可能存在滞后或误差。",
    "以上判断基于当前信息，若宏观环境变化将及时更新。",
    "不同周期的信号可能冲突，需以交易周期为准。",
    "仓位与风险控制优先于方向判断，这是长期生存前提。",
    "盘面情绪指标显示多空分歧仍然较大。",
)
#: 中性检查：确认所有补充句都不含方向词（否则"无方向样本"会被污染）
assert not any(  # noqa: S101 - 模块级不变式，导入即校验
    word in filler for filler in _FILLERS for word in _DIRECTION_WORDS
)

# 文本模板（每个模板都**必含**：方向词 + 目标/止盈 + 止损，便于正则抽取与人工标注）
# ---------------------------------------------------------------------------
_CN: Final[str] = "黄金"
_SYM: Final[str] = "XAUUSD"
_SHORT_HINTS: Final[tuple[str, ...]] = ("日内", "短线", "中线", "本周", "美盘")
_SHORT_TEMPLATES: Final[tuple[str, ...]] = (
    "{cn} {word}，目标 {tp}，止损 {sl}",
    "{cn}{word}，目标{tp}，止损{sl}，{hint_short}",
    "{sym} {word_en}({word}) target {tp} stop {sl}",
    "【{session}】{cn} {word}，目标 {tp}，止损 {sl}",
    "{cn} {word}，止损 {sl}，目标 {tp}",
    "{cn}{word}，目标 {tp}-{tp2}，止损 {sl}",
    "{cn} {word}，第一目标 {tp}，止损 {sl}",
    "{sym} {word} target {tp}，止损 {sl}",
)
_MEDIUM_TEMPLATES: Final[tuple[str, ...]] = (
    "{cn}（{sym}）{word}，入场 {e}-{e2}，止损 {sl}，第一目标 {tp}，"
    "第二目标 {tp2}{hint_cn}。{driver}。",
    "【{session}快评】{cn}{word}：{driver}；入场 {e} 附近，止损 {sl}，目标 {tp}{hint_cn}。",
    "{sym} bias: {word_en}. Entry {e}-{e2}, stop {sl}, target {tp}{hint_cn}. 关注{driver}。",
    "{cn}短线思路：{word}，{driver}，止损放在 {sl}，目标看向 {tp}{hint_cn}。",
    "{driver}，因此{cn}{word}；入场区间 {e}-{e2}，止损 {sl}，止盈 {tp}{hint_cn}。",
    "{cn}{word}（{sym}）：{driver}。止损 {sl}，目标 {tp}{hint_cn}，仓位不超过两成。",
)
_LONG_OPENERS: Final[tuple[str, ...]] = (
    "【{session}深度】本周{cn}（{sym}）思路更新：",
    "【{session}复盘与展望】{cn}中期观点如下：",
    "【策略笔记】{cn}（{sym}）正处于关键位置，",
)
#: 边界样本（对应 `docs/10 §2.2` 对抗样本表 + §4.1/§4.4/§4.5 判定规则）。
#: 注意：只在 posts.csv 里标注 kind（`edge_case` 列），**不会**出现在标注 CSV 中，
#: 因此不会锚定标注人——见 `docs/10 §2.3` 盲标要求。
_EDGE_CORES: Final[dict[str, str]] = {
    "conditional": "如果晚间的通胀数据超预期，黄金才会转做空，目标 {tp}，止损 {sl}；否则继续观察。",
    "quote_only": "有机构认为黄金应当做多，目标 {tp}，止损 {sl}，我的看法稍后单独给出。",
    "post_hoc": "昨天黄金做多已止盈 {tp}，止损 {sl} 没有被触发，今天重新评估。",
    "double_negative": "不排除黄金做多，目标 {tp}，止损 {sl}，方向仍需要数据确认。",
    "inconsistent_price": "黄金做多，入场 {e} 附近，止损 {e_over}，目标 {tp}。",
    "macro_only": "美国 {month} 月 CPI 同比 3.{dec}%，高于市场预期，美元指数短线走高。",
    "rhetorical": "{sym} {e} 这个位置，现在还有多少人愿意进场？反正我是不着急。",
    "image_only": "看图操作，{sym} 在 {e} 附近的关键位置已经标注在图上。",
}
#: 无方向样本（docs/10 期望标注 = UNKNOWN）：自检时**必须**不含任何方向词
_NON_DIRECTIONAL_EDGE: Final[frozenset[str]] = frozenset(
    {"macro_only", "rhetorical", "image_only"}
)
#: 边界样本种类顺序（每 `_EDGE_PERIOD` 行里前 8 行各取一类）
_EDGE_ORDER: Final[tuple[str, ...]] = (
    "conditional",
    "quote_only",
    "post_hoc",
    "double_negative",
    "inconsistent_price",
    "macro_only",
    "rhetorical",
    "image_only",
)
#: 每 16 行里有 8 行是对抗样本（≈50%）：`docs/10 §2.2` 要求 200 条标注集里对抗样本
#: **每类 ≥10 条**（下限即 70/200 = 35%），这里留出富余以保证**抽样之后**仍满足（有测试锁定）。
_EDGE_PERIOD: Final[int] = 16
#: 时间语义异常样本（演练 `docs/10 §3` 的两种边界）
_NO_PUBLISHED_INDEXES: Final[tuple[int, ...]] = (4, 104, 204)
_PUBLISHED_AFTER_COLLECTED_INDEXES: Final[tuple[int, ...]] = (55, 155)
#: 含媒体样本（`index % 8 == 0`；配额见 `docs/10 §2.2`）
_MEDIA_CYCLE: Final[tuple[str, ...]] = ("image", "chart", "video", "chart")
_SHANGHAI: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
_RSS_PLATFORM: Final[str] = "rss"


@dataclass(frozen=True, slots=True)
class MockPost:
    """一条合成博文（`author_posts` 演练行）。"""

    post_id: str
    raw_item_id: str
    author_id: str
    author_name: str
    source_name: str
    published_at: datetime | None
    collected_at: datetime
    effective_at: datetime
    text_content: str
    has_media: bool
    media_type: str
    url: str
    edge_case: str
    repost_of: str

    @property
    def length_bucket(self) -> str:
        length = len(self.text_content)
        for name in ("short", "medium", "long"):
            low, high = _BUCKET_BOUNDS[name]
            if low <= length <= high:
                return name
        return "long"

    @property
    def time_precision(self) -> str:
        return "published" if self.published_at is not None else "collected_only"

    @property
    def content_hash(self) -> str:
        return content_hash(normalize_for_hash(self.text_content))

    @property
    def language(self) -> str:
        has_cjk = any("\u4e00" <= char <= "\u9fff" for char in self.text_content)
        has_latin = any(char.isascii() and char.isalpha() for char in self.text_content)
        if has_cjk and has_latin:
            return "zh-en"
        return "zh" if has_cjk else ("en" if has_latin else "und")

    def to_row(self) -> dict[str, str]:
        """转成 CSV 行（别名列 `id`/`source`/`content` 与规范列内容一致）。"""
        return {
            "id": self.post_id,
            "post_id": self.post_id,
            "source": self.source_name,
            "source_name": self.source_name,
            "content": self.text_content,
            "text_content": self.text_content,
            "raw_item_id": self.raw_item_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "published_at": self.published_at.isoformat() if self.published_at else "",
            "collected_at": self.collected_at.isoformat(),
            "effective_at": self.effective_at.isoformat(),
            "time_precision": self.time_precision,
            "length_bucket": self.length_bucket,
            "has_media": "true" if self.has_media else "false",
            "media_type": self.media_type,
            "language": self.language,
            "content_hash": self.content_hash,
            "url": self.url,
            "platform": _RSS_PLATFORM,
            "edge_case": self.edge_case,
            "repost_of": self.repost_of,
            "is_mock": "true",
            "mock_batch": MOCK_BATCH,
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _trading_days(start: date, count: int) -> tuple[date, ...]:
    """从 `start` 起取 `count` 个**交易日**（跳过周末）。"""
    if count < 1:
        raise ValueError("trading_days 必须 ≥ 1")
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def _assert_past_window(days: Sequence[date]) -> None:
    """窗口必须完全落在过去（防"未来数据"混进演练集，`.clinerules` 红线 1）。"""
    end_of_last = datetime.combine(days[-1] + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    if end_of_last > utc_now():
        raise ValueError(
            f"Mock 数据窗口必须完全在过去：最后一天 {days[-1]} 尚未结束"
            f"（现在是 {utc_now().isoformat()}）"
        )


def _levels(index: int, stance: str) -> dict[str, int]:
    """按行号生成方向自洽的点位（`docs/10 §4.4`）：LONG 目标在上 / 止损在下，SHORT 相反。"""
    entry = 2300 + index
    span = 5 + (index % 11)
    offset_stop = 12 + (index % 9)
    offset_target = 25 + (index % 17)
    if stance == "LONG":
        return {
            "e": entry,
            "e2": entry + span,
            "sl": entry - offset_stop,
            "tp": entry + offset_target,
            "tp2": entry + offset_target * 2,
            "e_over": entry + 30,
        }
    if stance == "SHORT":
        return {
            "e": entry,
            "e2": entry + span,
            "sl": entry + offset_stop,
            "tp": entry - offset_target,
            "tp2": entry - offset_target * 2,
            "e_over": entry - 30,
        }
    return {
        "e": entry,
        "e2": entry + span,
        "sl": entry - 10,
        "tp": entry + 10,
        "tp2": entry + 20,
        "e_over": entry + 30,
    }


def _enforce_bucket(text: str, bucket: str, rng: random.Random) -> str:
    """保证文本长度落在目标分档内（不足则补中性描述句；越界直接报错，绝不静默产出脏数据）。"""
    low, high = _BUCKET_BOUNDS[bucket]
    if len(text) > high:
        raise ValueError(f"{bucket} 档文本超长（{len(text)} > {high}）：{text[:40]}…")
    if len(text) >= low:
        return text
    pool = list(_FILLERS)
    rng.shuffle(pool)
    for filler in pool:
        if len(text) >= low:
            break
        candidate = text + filler
        if len(candidate) <= high:
            text = candidate
    if not low <= len(text) <= high:
        raise ValueError(f"无法填充到 {bucket} 档（{low}~{high} 字），请扩充 _FILLERS")
    return text


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def _build_text(
    index: int, stance: str, bucket: str, rng: random.Random, edge_case: str
) -> str:
    """按行号拼出一条文本（含方向词 + 目标/止损 + 约 85% 的周期线索）。"""
    levels = _levels(index, stance)
    word, word_en = _STANCE_WORDS[stance]
    # 周期线索：约 85% 的样本带线索，其余演练"周期缺失应留空"（docs/10 §4.2）
    hint_cn = ""
    if index % 7 != 6:
        hint_cn = f"，{_HORIZON_HINTS[index % len(_HORIZON_HINTS)][0]}"
    context = {
        "cn": _CN,
        "sym": _SYM,
        "word": word,
        "word_en": word_en,
        "session": _SESSION_TIMES[index % len(_SESSION_TIMES)][0],
        "hint_cn": hint_cn,
        "hint_short": _SHORT_HINTS[index % len(_SHORT_HINTS)],
        "driver": _DRIVERS[index % len(_DRIVERS)],
        # 无方向对抗样本要用行号相关的数据点保证文本唯一（否则整批会被去重掉）
        "month": str(1 + index % 12),
        "dec": str(index % 10),
        **{key: str(value) for key, value in levels.items()},
    }
    if edge_case:
        core = _EDGE_CORES[edge_case].format(**context)
        return _enforce_bucket(core, bucket, rng)
    if bucket == "short":
        core = _SHORT_TEMPLATES[index % len(_SHORT_TEMPLATES)].format(**context)
    elif bucket == "medium":
        core = _MEDIUM_TEMPLATES[index % len(_MEDIUM_TEMPLATES)].format(**context)
    else:
        opener = _LONG_OPENERS[index % len(_LONG_OPENERS)].format(**context)
        core = opener + _MEDIUM_TEMPLATES[index % len(_MEDIUM_TEMPLATES)].format(**context)
    return _enforce_bucket(core, bucket, rng)


def _validate_text(post: MockPost) -> None:
    """文本自检（`.clinerules` 红线：样本必须"说的是什么就是什么"）。

    - 普通样本 / 有方向的对抗样本：必须同时含"方向词"与"目标/止损"；
    - 无方向对抗样本（`macro_only` / `rhetorical` / `image_only`）：**必须不含**方向词
      （它们的期望标注就是 UNKNOWN，这正是要演练的点）。
    """
    text = post.text_content
    has_direction = any(word in text for word in _DIRECTION_WORDS)
    if post.edge_case in _NON_DIRECTIONAL_EDGE:
        if has_direction:
            raise ValueError(f"无方向样本却含方向词：{post.post_id}：{text}")
        return
    has_target = any(word in text for word in ("目标", "止盈", "target"))
    has_stop = any(word in text for word in ("止损", "stop"))
    if not (has_direction and has_target and has_stop):
        raise ValueError(f"文本缺少方向词或目标/止损：{post.post_id}：{text}")


def _validate(posts: Sequence[MockPost]) -> None:
    """整体自检：id 唯一、文本合格、时间自洽、重复内容只允许来自"转载样本"。"""
    if not posts:
        raise ValueError("未生成任何 Mock 博文")
    ids = [post.post_id for post in posts]
    if len(set(ids)) != len(ids):
        raise ValueError("post_id 出现重复")
    reposts = sum(1 for post in posts if post.repost_of)
    counts: dict[str, int] = {}
    for post in posts:
        counts[post.text_content] = counts.get(post.text_content, 0) + 1
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    if duplicates != reposts:
        raise ValueError(
            f"意外重复内容 {duplicates} 条（转载样本 {reposts} 条）：非转载文本必须唯一，"
            "否则抽样总数会被悄悄削掉"
        )
    for post in posts:
        _validate_text(post)
        expected = (
            post.collected_at
            if post.published_at is None
            else max(post.published_at, post.collected_at)
        )
        if post.effective_at != expected:
            raise ValueError(
                f"时间语义错误：{post.post_id} effective_at != max(published_at, collected_at)"
            )
        if post.effective_at < post.collected_at:
            raise ValueError(f"时间语义错误：{post.post_id} effective_at < collected_at")


def generate_mock_posts(
    *,
    count: int = DEFAULT_POST_COUNT,
    seed: int = DEFAULT_SEED,
    window_start: date = DEFAULT_WINDOW_START,
    trading_days: int = DEFAULT_TRADING_DAYS,
) -> list[MockPost]:
    """生成 `count` 条 Mock 博文（同 seed + 同窗口 ⇒ 逐字节可复现）。

    Args:
        count: 条数（默认 250：够 200 条抽样留余量；末尾 5 条是"转载同文"用于演练去重）。
        seed: 随机种子（影响模板 / 补充句的挑选顺序，不影响 id 与点位）。
        window_start: 时间窗口起点（周末自动跳过）。
        trading_days: 覆盖的交易日数量（`docs/10 §2.2` 建议 ≥5）。

    Returns:
        按行号排列的 :class:`MockPost` 列表。

    Raises:
        ValueError: 参数非法、窗口落在未来，或整体自检（`_validate`）不通过。
    """
    if count < 1:
        raise ValueError("count 必须 ≥ 1")
    days = _trading_days(window_start, trading_days)
    _assert_past_window(days)
    rng = random.Random(seed)

    repost_map: dict[int, int] = {}
    if count - DEFAULT_REPOST_COUNT > _REPOST_ORIGINALS[-1]:
        for offset, target in enumerate(range(count - DEFAULT_REPOST_COUNT, count)):
            repost_map[target] = _REPOST_ORIGINALS[offset % len(_REPOST_ORIGINALS)]

    posts: list[MockPost] = []
    by_index: dict[int, MockPost] = {}
    for index in range(count):
        slot = index % len(MOCK_ACCOUNTS)
        author_name, source_name = MOCK_ACCOUNTS[slot]
        day = days[index % len(days)]
        _, hour, minute = _SESSION_TIMES[index % len(_SESSION_TIMES)]
        published_local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=_SHANGHAI
        ).astimezone(UTC) + timedelta(minutes=(index * 7) % 60)
        published_at: datetime | None = published_local
        collected_at = published_local + timedelta(minutes=1 + (index * 13) % 45)
        has_media = index % 8 == 0
        media_type = _MEDIA_CYCLE[index % len(_MEDIA_CYCLE)] if has_media else ""
        edge_index = index % _EDGE_PERIOD
        edge_case = _EDGE_ORDER[edge_index] if edge_index < len(_EDGE_ORDER) else ""
        if edge_case == "image_only":  # 图表无文字样本：必须真的带图（docs/10 §2.2）
            has_media = True
            media_type = "image"
        bucket = _BUCKET_CYCLE[index % len(_BUCKET_CYCLE)]
        stance = _STANCE_CYCLE[index % len(_STANCE_CYCLE)]
        repost_of = ""
        text = _build_text(index, stance, bucket, rng, edge_case)

        if index in _NO_PUBLISHED_INDEXES:  # 缺发布时间 → 只能按采集时间可用
            collected_at = published_local
            published_at = None
        elif index in _PUBLISHED_AFTER_COLLECTED_INDEXES:  # 采集早于发布（跨时区 / 预发布）
            collected_at = published_local - timedelta(minutes=12)

        original_index = repost_map.get(index)
        if original_index is not None:
            original = by_index[original_index]
            # 转载账号 = 原文账号的下一个来源（保证"跨来源传播"，用于演练同文去重）
            repost_slot = (original_index % len(MOCK_ACCOUNTS) + 1) % len(MOCK_ACCOUNTS)
            author_name, source_name = MOCK_ACCOUNTS[repost_slot]
            text = original.text_content  # 同文转载：用于演练内容去重
            repost_of = original.post_id
            has_media = original.has_media
            media_type = original.media_type
            # 转载行沿用原文的"对抗样本类别"（标签描述的是**内容**，内容与原文一致）
            edge_case = original.edge_case
            original_published = original.published_at or original.collected_at
            repost_published = original_published + timedelta(minutes=95)
            published_at = repost_published
            collected_at = repost_published + timedelta(minutes=6)
        else:
            repost_slot = slot

        post = MockPost(
            post_id=f"mock-post-{index + 1:04d}",
            raw_item_id=f"mock-raw-{index + 1:04d}",
            author_id=f"mock-author-{repost_slot + 1:02d}",
            author_name=author_name,
            source_name=source_name,
            published_at=published_at,
            collected_at=collected_at,
            effective_at=resolve_effective_at(published_at=published_at, collected_at=collected_at),
            text_content=text,
            has_media=has_media,
            media_type=media_type,
            url=f"https://example.invalid/{MOCK_BATCH}/{index + 1:04d}",
            edge_case=edge_case,
            repost_of=repost_of,
        )
        # 转载行沿用原文长度（分档检查只对原生行有意义）
        if not repost_of and post.length_bucket != bucket:
            # pragma: no cover - 由 _enforce_bucket 保证
            raise ValueError(f"{post.post_id} 分档不符：期望 {bucket}，实际 {post.length_bucket}")
        posts.append(post)
        by_index[index] = post

    _validate(posts)
    return posts


def write_posts_csv(path: Path, posts: Sequence[MockPost]) -> int:
    """写出 Mock 博文 CSV 并返回行数。

    编码用 ``utf-8-sig``：Excel 双击打开不乱码；抽样脚本以 ``utf-8-sig`` 读取，同样兼容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(POST_COLUMNS))
        writer.writeheader()
        for post in posts:
            writer.writerow(post.to_row())
    return len(posts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.generate_mock_posts",
        description="生成 Phase 2 Mock 博文（**演练用合成数据**，不是真实数据）",
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_POSTS_CSV), help="输出 CSV 路径（默认 logs/posts.csv）"
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_POST_COUNT, help="生成条数（默认 250）"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（默认 20260912）")
    parser.add_argument(
        "--window-start",
        default=DEFAULT_WINDOW_START.isoformat(),
        help="时间窗口起点 YYYY-MM-DD（默认 2026-08-24；周末自动跳过，必须落在过去）",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_TRADING_DAYS, help="覆盖的交易日数量（默认 10）"
    )
    parser.add_argument(
        "--force", action="store_true", help="目标文件已存在时覆盖（默认拒绝，保护真实数据）"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """生成 Mock 博文并写盘；退出码 0=成功 / 4=目标文件已存在且未加 --force。"""
    args = _parse_args(argv)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"[mock] 目标文件已存在，拒绝覆盖：{out}", file=sys.stderr)
        print(
            "[mock] 这是故意的保护（该路径可能已经是真实导出数据）；确认可覆盖后加 --force。",
            file=sys.stderr,
        )
        return 4

    posts = generate_mock_posts(
        count=args.count,
        seed=args.seed,
        window_start=date.fromisoformat(args.window_start),
        trading_days=args.days,
    )
    rows = write_posts_csv(out, posts)

    configure_stdout()
    buckets: Counter[str] = Counter(post.length_bucket for post in posts)
    sources: Counter[str] = Counter(post.source_name for post in posts)
    edge_counts: Counter[str] = Counter(post.edge_case for post in posts if post.edge_case)
    media = sum(1 for post in posts if post.has_media)
    edges = sum(edge_counts.values())
    reposts = sum(1 for post in posts if post.repost_of)
    days = len({post.effective_at.date() for post in posts})
    print(
        f"[mock][警告] 合成演练数据（{MOCK_BATCH}）：禁止用于研究结论 / 模型训练；"
        "任何结论都必须用真实数据复跑"
    )
    print(f"[mock] 已写出 {rows} 行 → {out}")
    print(
        f"[mock] 长度分档：{dict(sorted(buckets.items()))}；"
        f"来源分布：{dict(sorted(sources.items()))}"
    )
    print(
        f"[mock] 含媒体 {media} 条；覆盖 {days} 个交易日；转载样本 {reposts} 条；"
        f"对抗样本 {edges} 条"
    )
    edge_line = dict(sorted(edge_counts.items()))
    print(f"[mock] 对抗样本分布（docs/10 §2.2 要求每类 ≥10 条）:{edge_line}")
    print(
        "[mock] 下一步：python -m scripts.sample_annotation_set "
        "--input-csv logs/posts.csv --limit 200 --require-full"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())


# ---------------------------------------------------------------------------