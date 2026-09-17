# Phase 2 真实语料验收报告 · 开源标注数据集基准（regex vs LLM v13）

> **性质**：**工程对照报告**（标题级情感任务，语料来自公开标注数据集）。**不是** `docs/08 §5` 的验收达标结论——后者要求全人工裁决的博主帖子金标准。

- 生成时间（UTC）：`2026-09-14T12:51:33+00:00`
- 抽取器 A：RegexOpinionExtractor（纯规则基线）（`parser_version = mock-regex-v1`） → `logs/hf_benchmark_eval_regex.csv`
- 抽取器 B：LLMOpinionExtractor（DeepSeek deepseek-flash）（`parser_version = llm-deepseek-v13`） → `logs/hf_benchmark_eval_llm.csv`
- 金标准：`logs/hf_benchmark_gold.csv`（sha256 前 16 位 `422acd6eb1dcae32`，长表含 `scoring` 列）
- 文本：`logs/hf_benchmark_texts.csv`（150 条：黄金 100 + 中文股吧 50）
- LLM 台账：`logs/hf_benchmark_eval_llm_usage.json`（真实调用 150 条，费用估算（峰值价）$0.0250）

## 免责声明与适用范围（**用户裁决 2026-09-14，必读**）

1. **任务错配（本基准的根本局限）**：它测量的是**标题级方向分类**（新闻/股吧标题的情感标签），与本项目的**博主观点提取**任务有本质差异——没有正文语境、没有仓位意图、没有点位与周期。**分数低 ≠ 抽取器差**。
2. **LLM 的 `UNKNOWN` 是合规行为，不是错误**：它拒绝把「金价下跌 0.9%」这类**事实描述**当作**可交易观点**，符合 `docs/10 §4.9.0` 抽象原则与「只降不猜」。**不得**为迎合标题级数据集而改 Prompt —— 用户裁决：`opinion-prompt-v13` **保持冻结**，不教模型把描述句标成 `SHORT`（否则会污染模型定义，未来把新闻当预测）。
3. **本基准能证明什么**：①**正则抽取器在英文/标题级语料上完全失效**（黄金层 100 条英文标题里**整帖无观点 95 条**）；②**LLM 至少能正确识别「无观点」**（整帖无观点 46 条、方向拒答 93.3%，且**无一格判反方向**）。**不能**用它证明 LLM 的观点提取能力。
4. **真正的观点提取验证放到 Phase 3**：用手工录入的中文快讯（`logs/real_posts_opinion_2026_09_14.csv`，19 条）或抓取的真实博主帖子重新标注评估；**标题级情感分类**留给 Phase 3 的 **News Alpha** 使用。

## 0. 结论摘要（**只看 `stance`**）

| 层 | 抽取器 | stance 命中/分母 | 准确率（95% CI） | `docs/08 §5` 门槛（仅参考） |
|---|---|---|---|---|
| 黄金（N=100） | RegexOpinionExtractor（纯规则基线） | 2/100 | 2.0%（95% CI 0.6%~7.0%） | ❌ 未达 ≥ 90% |
| 黄金（N=100） | LLMOpinionExtractor（DeepSeek deepseek-flash） | 0/100 | 0.0%（95% CI 0.0%~3.7%） | ❌ 未达 ≥ 90% |

- 中文股吧层（N=50）**只作辅助参考**，单列在 §7，不进入本表与主结论。
- **LLM 相对正则：-2.0 pp**（两者 95% CI 重叠 → **差异不显著**）；LLM 修好 `0` 格、弄坏 `2` 格。
- LLM 行为画像（全 150 条口径）：整帖无观点 `46` 条；**方向拒答（UNKNOWN）`97/104` = 93.3%** —— 它大量判「方向不明」，而本基准金标准**必有方向**，故 `UNKNOWN` 一律计入**提取错误**（`docs/10` 要求「只降不猜」，这是模型**守规矩**的表现，不是坏行为）。
- 正则行为画像：整帖无观点 `135` 条 —— 印证**正则抽取器不适用于英文/标题级语料**（它为中文博主长帖设计），属**工具设计边界**，不是能力缺陷。
- `docs/08 §5` 的 stance ≥ 90% 门槛**仅作参考**：本基准是标题级情感分类，与「博主观点抽取」不是同一任务，达标与否都不构成本项目验收结论。

## 1. 数据来源与口径

| 层 | 数据集 | 行数 | 金标准映射（**原始标签未改写**） |
|---|---|---|---|
| 黄金 | `SaguaroCapital/sentiment-analysis-in-commodity-market-gold`（split=`test`） | 100 | `Price Direction Up/Down/Constant` → `LONG/SHORT/FLAT` |
| 中文股吧 | `HikasaHana/eastmoney_guba_title` | 50 | `label` 0/1/2 → `SHORT/LONG/FLAT` |

- ⚠️ 标题级情感分类任务，**不等于**博主帖子观点提取（无正文、无点位、无周期）
- ⚠️ gold 数据集为商品/黄金新闻标题；guba 数据集为**上证50ETF 股吧**标题（非黄金）
- ⚠️ guba 卡片自述「负向含不看跌，与 docs/10 §4.1.5 口径不同」，且提示标注者「有时自己也犯迷糊，建议自行筛选」
- ⚠️ information_type 本轮以 `--information-type empty` 落盘：金标准 `value` 留空 + `scoring=NOT_EVALUATED` → 模型给值只记「额外信息」，**不计假阳性**（口径见 docs/10 §7.1）
- ⚠️ 黄金数据集内部存在「方向标签全 0」的行（无方向标签）→ 默认跳过并计数
- **原始标签逐条留档**：`raw_*` 列在 `texts.csv` 里原样保存（含 `Dates`/`URL`/`Asset Comparision` 等），映射结果另存 `value_raw`（如 `Price Direction Down=1`）。
- `information_type` 本轮**不评分**（`scoring=NOT_EVALUATED`，口径见 `docs/10 §7.1`）：模型给值只记「额外信息」**不计假阳性**；本批 LLM 在 150 个未评估格子里给出了 104 个值。
- 抽取器的 `horizon`/`stop_loss`/`take_profit` 在本基准**没有金标准**（标题级数据无点位/周期）→ 这三格只用于观测**假阳性**（凭空编点位）。

## 2. 置信度边界（Wilson 95% CI；**不得只看点估计**）

| 层 | 抽取器 | 命中/分母 | 准确率（95% CI） | CI 半宽 |
|---|---|---|---|---|
| 黄金数据集（商品/黄金新闻标题，N=100） | RegexOpinionExtractor（纯规则基线） | 2/100 | 2.0%（95% CI 0.6%~7.0%） | ±3.2 pp |
| 黄金数据集（商品/黄金新闻标题，N=100） | LLMOpinionExtractor（DeepSeek deepseek-flash） | 0/100 | 0.0%（95% CI 0.0%~3.7%） | ±1.8 pp |
| 中文股吧（上证50ETF 标题，N=50） | RegexOpinionExtractor（纯规则基线） | 6/50 | 12.0%（95% CI 5.6%~23.8%） | ±9.1 pp |
| 中文股吧（上证50ETF 标题，N=50） | LLMOpinionExtractor（DeepSeek deepseek-flash） | 7/50 | 14.0%（95% CI 7.0%~26.2%） | ±9.6 pp |

- 读法：Wilson 半宽**随命中率变化**——命中率接近 50% 时最宽（N=100 约 ±10 pp、N=50 约 ±14 pp）；本批命中率极低（0~2%），区间自然收窄（±2~3 pp），**但真正该看的是上界**：N=100 时 `0/100` 的上界仍有 3.7%、`2/100` 的上界 7.0%——「接近 0」并不等于「精确测出 0」。
- **比较纪律**：N=100 的两组差异小于约 7 pp、N=50 小于约 14 pp 时，**不得声称优劣**；要缩窄区间只能扩样（或改用人工裁决语料）。

## 3. 主结论：`stance` 逐抽取器（黄金层 N=100）

| 抽取器 | 命中 | 准确率＝召回率（95% CI） | 精确率 | 该判未判 | 提取错误 | 不该判却判 | 双方未给出 |
|---|---|---|---|---|---|---|---|
| RegexOpinionExtractor（纯规则基线） | 2 | 2.0%（95% CI 0.6%~7.0%） | 40.0% | 95 | 3 | 0 | 0 |
| LLMOpinionExtractor（DeepSeek deepseek-flash） | 0 | 0.0%（95% CI 0.0%~3.7%） | 0.0% | 10 | 90 | 0 | 0 |

> 口径：分母 = **金标准有值**的格子（本基准 `stance` 恒有值，故分母 = 样本数）；因此召回率与准确率同值；`UNKNOWN` 与错误方向都计入**提取错误**（`wrong_value`）；`该判未判`在本基准 = 抽取器判「无观点」（`drafts` 为空）。

- **RegexOpinionExtractor（纯规则基线）**：3 格「提取错误」中 `UNKNOWN` 拒答 1 格、判了具体方向但不符 2 格（共给出方向 5 格）。
- **LLMOpinionExtractor（DeepSeek deepseek-flash）**：90 格「提取错误」**全部是 `UNKNOWN` 方向拒答**，没有一格是「判了方向但判反」→ 短板是**不敢下结论**，不是判断力。

## 4. 混淆矩阵（黄金层；行 = 金标准，列 = 抽取器）

### 4.1 RegexOpinionExtractor（纯规则基线）（`parser_version = mock-regex-v1`）

| 金标准 ↓ \ 抽取器 → | LONG | SHORT | FLAT | UNKNOWN | ∅（未给出） |
|---|---|---|---|---|---|
| **LONG** | 0 | 1 | 1 | 0 | 53 |
| **SHORT** | 0 | 0 | 0 | 1 | 40 |
| **FLAT** | 0 | 0 | 2 | 0 | 2 |
| **UNKNOWN** | 0 | 0 | 0 | 0 | 0 |
| **∅（未给出）** | 0 | 0 | 0 | 0 | 0 |

### 4.2 LLMOpinionExtractor（DeepSeek deepseek-flash）（`parser_version = llm-deepseek-v13`）

| 金标准 ↓ \ 抽取器 → | LONG | SHORT | FLAT | UNKNOWN | ∅（未给出） |
|---|---|---|---|---|---|
| **LONG** | 0 | 0 | 0 | 49 | 6 |
| **SHORT** | 0 | 0 | 0 | 38 | 3 |
| **FLAT** | 0 | 0 | 0 | 3 | 1 |
| **UNKNOWN** | 0 | 0 | 0 | 0 | 0 |
| **∅（未给出）** | 0 | 0 | 0 | 0 | 0 |

## 5. 正则 vs LLM v13（黄金层）

- **LLM 修好**（正则非命中 → LLM 命中）：`0` 格；**LLM 弄坏**（正则命中 → LLM 非命中）：`2` 格；净变化 **-2.0 pp**。

### 5.1 LLM 修好的格子（0）

- （无）

### 5.2 LLM 弄坏的格子（2）

- `hf-gold-00064`：gold to trade in 28670-29160 range: achiievers equities
  - 金标准 `FLAT` ↔ 正则 `FLAT` ↔ LLM `UNKNOWN`
- `hf-gold-00095`：gold, silver move in narrow range
  - 金标准 `FLAT` ↔ 正则 `FLAT` ↔ LLM ``

### 5.3 与 Mock 200 条基线的对读（**语料/任务/金标准来源均不同，禁止直接比较**）

| 语料 | 抽取器 | stance 命中/分母 | 准确率（95% CI） |
|---|---|---|---|
| Mock 200（人工裁决子集） | 正则 | 14/14 | 100.0%（95% CI 78.5%~100.0%） |
| Mock 200（人工裁决子集） | LLM | 14/14 | 100.0%（95% CI 78.5%~100.0%） |

## 6. 典型错误案例（黄金层）

### 6.1 正则：方向判错（`wrong_value`）

- `hf-gold-00007`：Buy gold if it dips to $1,245-48/oz
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ RegexOpinionExtractor（纯规则基线） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00022`：feb. gold erases gains, turns roughly flat at $1,194 an oz.
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ RegexOpinionExtractor（纯规则基线） 抽取 `FLAT`（`wrong_value`）
- `hf-gold-00045`：silver plunges on heavy sell-off, gold edges up
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ RegexOpinionExtractor（纯规则基线） 抽取 `SHORT`（`wrong_value`）

### 6.2 正则：在没有金标准的字段上凭空给值（`spurious` 假阳性）

- **正则在这批标题上一次都没有凭空给点位/周期**（0 格假阳性）——原因是它在英文标题上基本不产出观点（见 §0 正则无观点条数），**「零假阳性」在此是「几乎不作为」的结果，不是精确性的证据**。
### 6.3 LLM：漏判（`missed`，判「无观点」）

- `hf-gold-00028`：Gold prices continue gains in Asia from overnight
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00037`：barrick gold q3 net falls
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00040`：gold falls near one-week low
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00063`：bay street gains; gold, energy strong
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00067`：gold demand slips as crops fail to earn
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00070`：gold futures rise rs 34 on global cues
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00079`：gold futures' $5 climb boosts shares
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00082`：MCX GOLDPETAL Feb contract rises
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00091`：gold prices, company shares gain
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-gold-00095`：gold, silver move in narrow range
  - 金标准 `FLAT`（原始标签 `Price Direction Constant=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）

### 6.4 LLM：方向拒答 `UNKNOWN`（**计为提取错误**，但属「只降不猜」的合规行为）

- `hf-gold-00002`：feb. gold settles at $1,097,90/oz on comex, down 0.9% for the session
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00003`：dec gold rises 30c to $443.40/oz in morning ny trade
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00004`：Gold holds modest losses after Chicago PMI miss
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00005`：December gold $4.90, or 0.4%, lower at $1,313.20/oz.
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00006`：gold prices gain in asia on technical rebound, boj ahead
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00007`：Buy gold if it dips to $1,245-48/oz
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00009`：Dec. gold settles at $1,282.90/oz, up $3.20, or 0.3%
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00010`：Gold for December delivery up 0.5% at $1,289.10
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00011`：gold dips as dollar rallies
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00012`：gold closes higher as oil price rises; copper hit by strike
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00013`：gold, silver slide on selling, weak demand
  - 金标准 `SHORT`（原始标签 `Price Direction Down=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-gold-00014`：gold, silver start week on firm note, rise on global cues
  - 金标准 `LONG`（原始标签 `Price Direction Up=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）

### 6.5 数据集标注存疑候选（**启发式，待人工复核；不否定数据集**）

- （启发式未发现存疑案例）

## 7. 辅助层：中文股吧（N=50，**不进主结论**）

| 抽取器 | 命中/分母 | 准确率（95% CI） | 该判未判 | 提取错误 | 不该判却判 |
|---|---|---|---|---|---|
| RegexOpinionExtractor（纯规则基线） | 6/50 | 12.0%（95% CI 5.6%~23.8%） | 40 | 4 | 0 |
| LLMOpinionExtractor（DeepSeek deepseek-flash） | 7/50 | 14.0%（95% CI 7.0%~26.2%） | 36 | 7 | 0 |

> 该层**只作辅助参考**：语料是**上证50ETF 股吧标题**（非黄金），且数据集卡片自述「负向包含不看跌」，与 `docs/10 §4.1.5`（不看多≠看空）**口径冲突**；任何结论都不得从本节外推到黄金语料。

### 7.1 股吧混淆矩阵（LLM）

| 金标准 ↓ \ 抽取器 → | LONG | SHORT | FLAT | UNKNOWN | ∅（未给出） |
|---|---|---|---|---|---|
| **LONG** | 7 | 0 | 0 | 3 | 10 |
| **SHORT** | 0 | 0 | 0 | 2 | 10 |
| **FLAT** | 0 | 0 | 0 | 2 | 16 |
| **UNKNOWN** | 0 | 0 | 0 | 0 | 0 |
| **∅（未给出）** | 0 | 0 | 0 | 0 | 0 |

### 7.2 股吧案例（LLM 非命中的前几例）

- `hf-guba-00000`：还没上天？赶紧的
  - 金标准 `LONG`（原始标签 `label=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00001`：50目标位在2.72
  - 金标准 `FLAT`（原始标签 `label=2`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-guba-00002`：没底线了吗？
  - 金标准 `SHORT`（原始标签 `label=0`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00003`：国家队在不做多，小盘股又该跳水了
  - 金标准 `SHORT`（原始标签 `label=0`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-guba-00004`：不知道你们怕啥？上面几个缺口了？三个缺口了，也就是只要敢跌你就加油买，最终第三个
  - 金标准 `LONG`（原始标签 `label=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-guba-00005`：做多倾家荡产做空富三代
  - 金标准 `SHORT`（原始标签 `label=0`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00006`：创新高，买跌的朋友你还在吗？黄金爆跌我爆涨，翻倍翻倍，都是钱的声音[大笑]
  - 金标准 `LONG`（原始标签 `label=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-guba-00007`：又开始耍赖拉保险
  - 金标准 `FLAT`（原始标签 `label=2`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00008`：赎回！
  - 金标准 `SHORT`（原始标签 `label=0`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00009`：周末重仓消息，明天将如何演变？短空长多？利空落实还是利空落地？
  - 金标准 `FLAT`（原始标签 `label=2`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `UNKNOWN`（`wrong_value`）
- `hf-guba-00010`：是的，近期会有大涨！
  - 金标准 `LONG`（原始标签 `label=1`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）
- `hf-guba-00011`：43万张3500购，26万张3400购，这批卖方开盘如果买平，卖方会不会缺席？扎
  - 金标准 `FLAT`（原始标签 `label=2`） ↔ LLMOpinionExtractor（DeepSeek deepseek-flash） 抽取 `∅（未给出）`（`missed`）

## 8. 局限与下一步

1. **任务不匹配**：标题级情感分类 ≠ 博主帖子观点提取（无正文、无点位、无周期、无时间语义）；指标偏低不代表抽取器坏——尤其**正则抽取器面向中文博主长帖**，在英文标题上基本不出观点，这属**工具设计边界**（已按用户裁决记录为已知边界）。
2. **金标准来源**：本批金标准是**开源数据集的人工标注**，非本项目 `docs/10` 口径的人工裁决 → 不能替代 `docs/08 §5` 验收。`docs/10 §7.1` 的 `NOT_EVALUATED` 机制只解决「不该计分的别计分」，不解决「该有的金标准没有」。
3. **样本量**：N=100/50 的 95% CI 半宽约 ±8~13 pp，只够做**方向性判断**，不足以声称达标。
4. **下一步**（**不进入 Phase 3**）：① 保持 Prompt v13 **冻结不变**，在 Phase 3 用**人工裁决的真实语料**复核观点提取能力；② 若继续用开源数据集，优先找**黄金相关且含点位/周期**的标注集，并在 `docs/10 §7.1` 登记未评估字段；③ **TD-35/36/37 已按「已知边界」归档、不做代码修补**（本质是任务定义差异，不是代码缺陷，见 `TECH_DEBT.md`）。

## 附录 A. 复现命令

```powershell
# ① 加载 + 字段映射（默认 dry-run；--information-type empty = information_type 不评分）
python scripts/load_hf_benchmark.py --no-dry-run --information-type empty --out-prefix logs/hf_benchmark
# ② 正则基线（零成本）
python scripts/evaluate_extractor.py --gold logs/hf_benchmark_gold.csv --texts logs/hf_benchmark_texts.csv --extractor regex --eval-out logs/hf_benchmark_eval_regex.csv --report logs/_scratch_hf_regex_report.md
# ③ LLM v13（真实调用；缓存命中即零成本复跑）
python scripts/evaluate_extractor.py --gold logs/hf_benchmark_gold.csv --texts logs/hf_benchmark_texts.csv --extractor llm --eval-out logs/hf_benchmark_eval_llm.csv --report logs/_scratch_hf_llm_report.md --llm-usage-out logs/hf_benchmark_eval_llm_usage.json
# ④ 本报告
python scripts/report_hf_benchmark.py
```

## 附录 B. 输入文件校验（sha256 前 16 位）

| 文件 | sha256(16) |
|---|---|
| `logs/hf_benchmark_gold.csv` | `422acd6eb1dcae32` |
| `logs/hf_benchmark_texts.csv` | `39c05d0e845c3405` |
| `logs/hf_benchmark_eval_regex.csv` | `af754f7301dc949f` |
| `logs/hf_benchmark_eval_llm.csv` | `956f2472c781fdd8` |
