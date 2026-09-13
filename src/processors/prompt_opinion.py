"""LLM 观点抽取的 Prompt（`opinion-prompt-v3`）——**唯一依据是 `docs/10 §4.9` 的裁决口径**。

设计要点：
1. **单轮**：system（规范 + 规则 + 决策阶梯 + Schema + few-shot）+ user（单条帖子 JSON）；
2. **中文指令 + 英文键名**（团队 2026-09-13 确认）；
3. `PROMPT_VERSION` 参与缓存键：**改这个文件必须同时改版本号**，否则会读到旧缓存；
4. **few-shot 必须与评测语料零重叠**（见下）。

版本演进（每一步都对应一条技术债或人工裁决）：

- **v2（TD-24 / TD-25 / TD-26）**：
  - TD-24（数据泄漏红线）：v1 的 6 条 few-shot 里有 4 条的输入句能在
    `logs/annotation_sample.csv` 里**逐字**找到（语料是按模板批量生成的）→ 模型等于"开卷考试"。
    v2 起全部换成**语料外的自造句子**，并由
    `tests/unit/test_prompt_opinion.py::test_few_shot_has_no_overlap_with_evaluation_corpus`
    做**最长公共子串**回归锁定（阈值 ≤ 12 字符）。
  - TD-25（信息类型优先级）：新增"信息类型决策阶梯"（人工裁决口径：
    **操作优先，驱动决定分类**）——L1 持仓/资金流 → POSITIONING、L2 宏观 → MACRO、
    L3 情绪 → SENTIMENT、L4 引用/复盘 → OTHER、L5 无驱动时的操作或纯图表 → TECHNICAL。
  - TD-26（`no_opinion` 语义）：宏观数据播报 = `stance=UNKNOWN` + `information_type=MACRO` +
    `no_opinion=true`（"没有可交易的方向性观点" ≠ "没有信息"）。
- **v6（20 条试点后收口）**：L3 改为"操作价位优先于情绪"（`mock-post-0009` 裁决）；
  规则 8 增加「**短线思路 → `15m`**」（`docs/10 §4.2` 补丁）。
- **v7（全量 200 后按 TD-28 收口，走"路径 B"）**：
  ①阶梯补 **`L2.5` 消息/事件驱动 → `NEWS`**（此前枚举里有 `NEWS` 但阶梯漏了这一级，
  导致所有消息驱动帖都落到 `TECHNICAL`）；
  ②L3 增加**两个例外** + 一条**判别线**——"**是交易理由，还是背景/附注**"：
  情绪/消息句与方向有因果连接（"因此/导致/引发"）→ 按驱动分类（`SENTIMENT` / `NEWS`）；
  仅"关注…""情绪指标显示分歧"这类补充说明 → 走 L3 判 `TECHNICAL`；
  ③few-shot 增至 **15 条**（新增"情绪是交易理由 → SENTIMENT"与"消息是波动来源 → NEWS"两例）。
- **v8（20 条回归验证后修正 v7 的两处措辞）**：
  ①**判别线只管情绪/消息**——`L1` 持仓 与 `L2` 宏观**不受"背景附注"降级影响**；
  ②**明确周期词优先于"短线思路"**。
- **v9（v8 的再修正：两处"过宽"措辞，均有金标准反例）**：
  ①`L2` 宏观驱动必须是**具体宏观变量/事件**；泛泛的"宏观环境/宏观面"与免责声明式提及
  （"若宏观环境变化将及时更新"）**不算**驱动（v8 把 `mock-post-0019` 的正确 `OTHER`
  误判成 `MACRO`）；
  ②`horizon` 改为**按明确度排序取首个线索**（见规则 8）。
- **v11（最终定版）**：在 v9 规则基础上**只保留**一条经验证的 few-shot
  （第 16 条："操作价位 + 关注<具体宏观变量> → `MACRO`"，修 `mock-post-0027`），
  **撤回** v10 试过的两处"宏观优先"措辞（它们让 `mock-post-0009/0019` 由对变错）。
  实证：v9 → 20 条仅 1 格残余（0027）；v10 → 2 格（0009/0019）→ v11 取二者之长。
"""

from __future__ import annotations

import json
from typing import Final

__all__ = [
    "MAX_TEXT_CHARS",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "build_messages",
    "build_user_message",
    "prepare_text",
]

PROMPT_VERSION: Final[str] = "opinion-prompt-v13"
MAX_TEXT_CHARS: Final[int] = 4000

_RULES: Final[str] = """\
严格遵守规范 v1.0（§4.9 人工裁决口径）。逐条执行，不得自由发挥：

1) 条件句（如果/若/一旦/假如/除非/倘若…）→ stance=UNKNOWN（不许猜方向）；
   但**必须照抄文本中的点位**，并按文本主旨给 information_type。
2) 引用/转述他人观点（某机构认为、据悉、转述、他人方案…）且作者未表态 → stance=UNKNOWN +
   information_type=OTHER；点位照抄（不要因为"是引用"就把数字丢掉）。
3) 事后复盘（昨天/上周/已止盈/复盘/回顾/此前）→ stance=UNKNOWN（不构成前瞻观点）；
   information_type=OTHER（除非文本另有 L1~L3 驱动）；点位照抄。
4) **stance=UNKNOWN ≠ 无观点**：只要文本含点位或信息类型线索，opinions 必须非空。
   `no_opinion=true` 的含义是"**没有可交易的方向性观点**"，此时**仍要输出**能提供的字段——
   典型：宏观数据播报 → stance=UNKNOWN + information_type=MACRO + no_opinion=true。
   `opinions=[]` 只用于**连信息类型线索都没有**的帖子（纯寒暄、纯链接、纯图片）。
5) 弱化/双重否定（不排除/或许/也许/可能/倾向/大概/有待）**不足以构成方向** → stance=UNKNOWN；
   仅当另有明确方向动词（做多/看空/买入/卖出/多单/空单/沽空…）时才判方向，
   且 confidence≤0.4（不排除/或许/可能）或 ≤0.5（预计/有望/倾向）。
6) 图表无文字（看图/见图/图表/图上）→ stance=UNKNOWN；文本含价格数字 → 点位照抄，
   information_type 按决策阶梯（纯图表分析 → TECHNICAL）。
7) 多目标：文本同时给出"第一目标/第二目标"时 take_profit 取**第一目标**；
   stop_loss 只填**一个**值；文本没给就填 null（**禁止填 0、禁止猜测**）。
8) horizon 只在文本有周期线索时填：超短/分钟级→15m；**"短线思路"/"短线观望"/"思路更新"
   （计划型表述）→15m**；半小时/30 分钟→30m；短线/日内（执行型表述）→1h；
   半天/几小时/美盘/4 小时级别→4h；中线/本周/趋势/日线→1d；否则 null（不要默认 1d）。
9) instrument 只在文本**明确提到**标的时填（黄金/金价/XAUUSD/gold→XAUUSD；美元指数→DXY；
   白银→XAGUSD；原油→WTI…），**禁止默认 XAUUSD**；没提到就 null。
10) rationale：≤120 字，摘录原文关键片段（点位与方向词），不要写你的推理过程。
11) 时间与作者字段一律**不要输出**（由系统注入，模型不得编造）。
12) stance 取值口径（§4.1）：
    - `FLAT` 只用于**行为**表述："观望 / 不参与 / 等信号 / 区间震荡不追 / 已平仓离场"；
    - **情绪或态度**（"不着急 / 不慌 / 有点担心 / 看好但暂不下单"）**不是** `FLAT`，
      仍判 `UNKNOWN`（并按 L3 给 `SENTIMENT`）；
    - `UNKNOWN` 用于**无法判定方向**：条件句、引用、复盘、弱化倾向、纯情绪表达、图表无方向。

information_type 决策阶梯（人工裁决口径：**操作优先，驱动决定分类**）。按顺序判断，**命中即停**：
  L1 有持仓/资金流驱动（ETF 持仓、投机净多头/净空头、增减仓、持仓变化、资金流）→ POSITIONING
  L2 有宏观驱动（CPI/PCE/非农/FOMC、利率、美债收益率、美元指数、实际利率、通胀、就业）→ MACRO
     —— 泛泛的"宏观环境 / 宏观面 / 宏观层面"或免责声明式提及（"若宏观环境变化将及时更新"）不算；
  L2.5 有消息/事件驱动（突发消息、地缘或政策突发事件；**消息被指为波动来源**）→ NEWS
  L3 操作价位优先：文本含**明确入场 / 止损 / 目标价** → TECHNICAL（**即使同时提到情绪背景**）。
     但有**两个例外**，必须按驱动分类，不判 TECHNICAL：
       (a) 情绪/风险偏好被明确写成**交易理由**（与方向有因果连接）→ SENTIMENT（走 L4）
       (b) 消息/事件被明确写成**波动来源或交易理由** → NEWS（走 L2.5）
  L4 情绪/风险偏好是文本**主旨**（且无明确操作价位），或是 L3 的例外 (a) → SENTIMENT
  L5 引用/转述他人观点（作者未表态）或事后复盘且无其它驱动 → OTHER
  L6 只有图表、无任何驱动 → TECHNICAL；仍无法归类 → OTHER
**判别线："是交易理由，还是背景/附注"——只适用于 L4 情绪与 L2.5 消息**：
  - 交易理由：情绪/消息句与方向之间有**因果连接**（"因此 / 所以 / 受此影响 / 导致 / 引发"），
    或该句本身就在解释方向来源（如"风险偏好转弱，因此做多"、"消息引发波动率跳升"）；
  - 背景附注：情绪/消息仅出现在补充说明里（"关注 risk-on 情绪"、"盘面情绪指标显示多空分歧"），
    不解释方向来源 → 只算背景 → 走 L3 判 TECHNICAL。
  - **判别线只作用于 L4 情绪 与 L2.5 消息**：`L1` 持仓/资金流 与 `L2` 宏观 照常按各自规则判断
    （即：文本确实把持仓/宏观当作主要驱动时才走 L1/L2），**不要**因为判别线把宏观/持仓降级成
    `TECHNICAL`，也**不要**把一笔带过的宏观/情绪尾句升格成 `MACRO`/`SENTIMENT`。
"""

_SCHEMA: Final[str] = """\
只输出**一个 JSON 对象**，不要解释、不要 markdown 代码块。键名固定，无值时填 null：

{"opinions":[{"stance":"LONG|SHORT|FLAT|UNKNOWN","instrument":null,
"horizon":"15m|30m|1h|4h|1d|null","confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,
"information_type":"MACRO|TECHNICAL|NEWS|SENTIMENT|POSITIONING|OTHER",
"rationale":null}],"no_opinion":false}

约束：
- confidence 为 0~1 的小数；价格字段为数字（不带单位与千分位）；
- 同一帖出现多个方向（多空并存）时**拆成多条 opinions**；禁止输出上述以外的键；
- `no_opinion=true` **不等于** `opinions=[]`：前者= 没有可交易的方向性观点（但仍要给出
  stance=UNKNOWN 与信息类型，如宏观播报 → MACRO）；后者= 连信息类型线索都没有。
"""

#: few-shot：9 类关键场景。**输入句必须与评测语料零重叠**（TD-24）；
#: 由 `tests/unit/test_prompt_opinion.py` 用最长公共子串做回归锁定。
_FEW_SHOT: Final[str] = """\
示例（输入 → 期望输出）：
[1] 输入：只有零售销售明显走弱，金价才会真正转强，那时我再考虑逢低买入，止盈参考 2648，
    防守 2584；不然就一直空仓等。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":2584,"take_profit":2648,"information_type":"MACRO",
"rationale":"零售销售走弱才考虑买入，止盈 2648，防守 2584"}],"no_opinion":false}
[2] 输入：看到券商晨报给黄金的方案是逢高沽空，止盈 2705，防守 2742，我本人暂不下单。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":2742,"take_profit":2705,"information_type":"OTHER",
"rationale":"券商晨报方案：逢高沽空，止盈 2705，防守 2742"}],"no_opinion":false}
[3] 输入：上周五的逢高沽空已经按 2698 止盈离场，防守位 2725 始终没被碰到，今天重新评估方向。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":null,"horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":2725,"take_profit":2698,"information_type":"OTHER",
"rationale":"上周五逢高沽空已按 2698 止盈，复盘"}],"no_opinion":false}
[4] 输入：我不排除黄金继续逢高沽空，止盈 2652，防守 2694，方向还要再等信号确认。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":0.4,
"entry_low":null,"entry_high":null,"stop_loss":2694,"take_profit":2652,
"information_type":"TECHNICAL","rationale":"不排除继续沽空，止盈 2652，防守 2694"}],
"no_opinion":false}
[5] 输入：黄金逢高沽空，进场区间 2668-2677，防守 2691，第一目标 2603，第二目标 2551，
    属于小时级别操作。
    输出：{"opinions":[{"stance":"SHORT","instrument":"XAUUSD","horizon":"1h","confidence":0.7,
"entry_low":2668,"entry_high":2677,"stop_loss":2691,"take_profit":2603,
"information_type":"TECHNICAL","rationale":"逢高沽空，进场 2668-2677，防守 2691，第一目标 2603"}],
"no_opinion":false}
[6] 输入：美国 3 月核心 PCE 环比 0.4%，读数强于一致预期，美元随之走强。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"DXY","horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,
"information_type":"MACRO","rationale":"核心 PCE 环比 0.4%，强于预期，美元走强"}],
"no_opinion":true}
[7] 输入：CFTC 持仓报告显示投机净空头连续两周减仓，资金面转暖，因此逢低做多黄金；
    进场 2645-2652，防守 2628，目标 2716。
    输出：{"opinions":[{"stance":"LONG","instrument":"XAUUSD","horizon":null,"confidence":0.7,
"entry_low":2645,"entry_high":2652,"stop_loss":2628,"take_profit":2716,
"information_type":"POSITIONING","rationale":"投机净空头连续两周减仓，逢低做多，目标 2716"}],
"no_opinion":false}
[8] 输入：金价刚跌完这一波，群里已经有人在喊见顶，我倒觉得没必要这么慌。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,
"information_type":"SENTIMENT","rationale":"刚跌完一波，群里喊见顶，我倒觉得没必要慌"}],
"no_opinion":false}
[9] 输入：图上把 2725 与 2688 两条线画出来了，破哪边就跟哪边，其余先不预设。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":null,"horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,
"information_type":"TECHNICAL","rationale":"图上标注 2725/2688，破位跟随"}],"no_opinion":false}
[10] 输入：这两天先观望，等非农落地再说，暂时不参与。
    输出：{"opinions":[{"stance":"FLAT","instrument":null,"horizon":null,"confidence":null,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,"information_type":"MACRO",
"rationale":"先观望，等非农落地，不参与"}],"no_opinion":false}
[11] 输入：避险资金持续流入，风险偏好明显转弱，我偏向逢低做多，暂时不给具体价位。
    输出：{"opinions":[{"stance":"LONG","instrument":"XAUUSD","horizon":null,"confidence":0.6,
"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,
"information_type":"SENTIMENT",
"rationale":"避险资金流入、风险偏好转弱，偏向做多（未给价位）"}],"no_opinion":false}
[12] 输入：不排除逢低做多黄金，止盈 2681，防守 2643；群里分歧声音不少，但按纪律执行就行。
    输出：{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":0.4,
"entry_low":null,"entry_high":null,"stop_loss":2643,"take_profit":2681,
"information_type":"TECHNICAL",
"rationale":"不排除逢低做多，止盈 2681，防守 2643（有明确价位 → 操作优先于情绪）"}],
"no_opinion":false}
[13] 输入：黄金逢高沽空，进场 2695-2703，防守 2728，目标 2629；就算避险情绪升温，这个计划也不改。
    输出：{"opinions":[{"stance":"SHORT","instrument":"XAUUSD","horizon":null,"confidence":0.7,
"entry_low":2695,"entry_high":2703,"stop_loss":2728,"take_profit":2629,
"information_type":"TECHNICAL",
"rationale":"逢高沽空，进场 2695-2703，防守 2728，目标 2629（情绪句只是背景）"}],
"no_opinion":false}
[14] 输入：避险情绪明显升温，资金持续往金市挪，因此我选择逢低做多，进场 2712-2720，防守 2694，
    目标 2770。
    输出：{"opinions":[{"stance":"LONG","instrument":"XAUUSD","horizon":null,"confidence":0.7,
"entry_low":2712,"entry_high":2720,"stop_loss":2694,"take_profit":2770,
"information_type":"SENTIMENT",
"rationale":"避险情绪升温、资金流入，因此做多（情绪是交易理由）"}],"no_opinion":false}
[15] 输入：金管局意外上调黄金储备配置，消息一出金价跳空走高，我跟进做多，进场 2687 附近，
    防守 2668，目标 2745。
    输出：{"opinions":[{"stance":"LONG","instrument":"XAUUSD","horizon":null,"confidence":0.7,
"entry_low":2687,"entry_high":2687,"stop_loss":2668,"take_profit":2745,
"information_type":"NEWS",
"rationale":"金管局上调黄金储备，消息引发跳空（消息是波动来源）"}],"no_opinion":false}
[16] 输入：黄金逢高沽空，进场 2676 附近，防守 2702，目标 2618；同时关注美债收益率回落对估值的支撑。
    输出：{"opinions":[{"stance":"SHORT","instrument":"XAUUSD","horizon":null,"confidence":0.7,
"entry_low":2676,"entry_high":2676,"stop_loss":2702,"take_profit":2618,
"information_type":"MACRO",
"rationale":"逢高沽空，进场 2676，防守 2702；关注美债收益率回落（宏观驱动，非尾句升格）"}],
"no_opinion":false}
"""

SYSTEM_PROMPT: Final[str] = (
    "你是黄金（XAUUSD）研究助理，把一条财经博主/机构帖子转成结构化观点。\n"
    "任务：按下面的规范抽取观点；文本可能没有方向性观点（no_opinion=true），也可能是对抗样本"
    "（条件句/引用/复盘/弱化/多目标），必须按规则处理，不得为了「有输出」而猜方向。\n\n"
    f"{_RULES}\n输出格式：\n{_SCHEMA}\n{_FEW_SHOT}"
)


def prepare_text(text: str) -> tuple[str, bool]:
    """正文预处理：去首尾空白；超长截断（返回 `(文本, 是否截断)`）。"""
    cleaned = (text or "").strip()
    if len(cleaned) <= MAX_TEXT_CHARS:
        return cleaned, False
    return cleaned[:MAX_TEXT_CHARS], True


def build_user_message(text: str, *, has_media: bool = False) -> str:
    """用 JSON 包装正文：避免文本里的引号/指令造成"提示注入"。"""
    payload = {"has_media": bool(has_media), "text": prepare_text(text)[0]}
    return json.dumps(payload, ensure_ascii=False)


def build_messages(text: str, *, has_media: bool = False) -> tuple[dict[str, str], ...]:
    """构造单轮 messages（system + user）。"""
    return (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(text, has_media=has_media)},
    )

