# Phase 2 基线对比报告 · 正则抽取器 vs LLM 抽取器

- 生成时间（UTC）：`2026-09-13T09:07:53+00:00`
- 金标准：`D:\VSCodeProject\XAUUSD\logs\ground_truth_200.csv`（sha256 前 16 位 `da3af3d3f0928450`）
- 正则明细：`D:\VSCodeProject\XAUUSD\logs\extractor_eval.csv`
- LLM 明细：`D:\VSCodeProject\XAUUSD\logs\extractor_eval_llm.csv`
- 对比格子：**1000** 格（200 帖 × 5 字段）

> **判定依据**：`source=human-adjudicated` 子集是**唯一绝对金标准**；`3-model-consensus` 子集只作参考。LLM 明细里的 `spurious` 若来自共识金标准的「未给出」格（条件句/引用句/复盘句的点位），属**金标准分层差异**而非模型错误——见 `docs/10 §4.9` 规则 8。

## 1. 逐字段准确率（人工子集口径 = 验收口径）

| 字段 | 金标准有值 | 正则命中 | 正则准确率 | LLM 命中 | LLM 准确率 | 提升 | 结论（按 LLM） |
|---|---|---|---|---|---|---|---|
| 方向（`stance`） | 14 | 14 | 100.0% | 14 | 100.0% | **+0.0 pp** | ✅ PASS（门槛 ≥ 90.0%） |
| 周期（`horizon`） | 3 | 0 | 0.0% | 3 | 100.0% | **+100.0 pp** | ✅ PASS（门槛 ≥ 85.0%） |
| 止损（`stop_loss`） | 29 | 0 | 0.0% | 29 | 100.0% | **+100.0 pp** | ✅ PASS（门槛 ≥ 90.0%） |
| 目标位（`take_profit`） | 44 | 11 | 25.0% | 44 | 100.0% | **+75.0 pp** | ✅ PASS（门槛 ≥ 90.0%） |
| 信息类型（`information_type`） | 135 | 55 | 40.7% | 124 | 91.9% | **+51.1 pp** | —（无门槛） |

## 2. 五类判定构成（全量口径，含共识格）

| 抽取器 | 字段 | 格子 | 命中 | 双方未给出 | 该判未判 | 提取错误 | 不该判却判 | 精确率 |
|---|---|---|---|---|---|---|---|---|
| 正则 | 方向 | 200 | 144 | 0 | 27 | 29 | 0 | 83.2% |
| 正则 | 周期 | 200 | 15 | 134 | 22 | 25 | 4 | 34.1% |
| 正则 | 止损 | 200 | 40 | 79 | 81 | 0 | 0 | 100.0% |
| 正则 | 目标位 | 200 | 38 | 79 | 79 | 4 | 0 | 90.5% |
| 正则 | 信息类型 | 200 | 75 | 0 | 80 | 45 | 0 | 62.5% |
| LLM | 方向 | 200 | 199 | 0 | 0 | 1 | 0 | 99.5% |
| LLM | 周期 | 200 | 62 | 137 | 0 | 0 | 1 | 98.4% |
| LLM | 止损 | 200 | 121 | 40 | 0 | 0 | 39 | 75.6% |
| LLM | 目标位 | 200 | 121 | 40 | 0 | 0 | 39 | 75.6% |
| LLM | 信息类型 | 200 | 189 | 0 | 0 | 11 | 0 | 94.5% |

## 3. LLM 相对正则的逐格变化

- **修复（正则未命中 → LLM 命中）：380 格**
  - `mock-post-0001` / 信息类型
  - `mock-post-0002` / 信息类型
  - `mock-post-0004` / 信息类型
  - `mock-post-0004` / 止损
  - `mock-post-0004` / 目标位
  - `mock-post-0005` / 目标位
  - `mock-post-0006` / 信息类型
  - `mock-post-0006` / 方向
  - `mock-post-0007` / 信息类型
  - `mock-post-0007` / 方向
  - `mock-post-0008` / 信息类型
  - `mock-post-0008` / 方向
  - …其余 368 格见 `logs/extractor_eval_llm.csv`
- **回归（正则命中 → LLM 未命中）：0 格**
  - （无）

## 4. Token 与费用（LLM 侧）

- `api`：`{'cache_hit_tokens': 683904, 'cache_miss_tokens': 37215, 'completion_tokens': 16728, 'posts': 180, 'prompt_tokens': 721119}`
- `api_estimated_cost_usd_offpeak`：`0.017670762`
- `api_estimated_cost_usd_peak`：`0.035341524`
- `cached`：`{'cache_hit_tokens': 76160, 'cache_miss_tokens': 3960, 'completion_tokens': 1875, 'posts': 20, 'prompt_tokens': 80120}`
- `full_set_estimated_cost_usd_offpeak`：`0.019618241999999998`
- `full_set_estimated_cost_usd_peak`：`0.039236483999999995`

## 5. 人工 ↔ 模型 对齐案例

### 5.1 模型纠正人工（金标准按模型的答案改判）

- **`mock-post-0009` / 信息类型**：`SENTIMENT` → `TECHNICAL`（裁决人 `chenxiangxie`；改判理由：二次裁决（2026-09-13）：文本含明确止损/目标价，按「操作优先」判 TECHNICAL（模型纠正人工；已同步 docs/10 §4.9 规则 9 L3））
  - ✅ 已对齐（LLM 与改判后金标准一致）
  - 原文：【策略笔记】黄金（XAUUSD）正处于关键位置，XAUUSD bias: long / bullish. Entry 2…

### 5.2 人工纠正模型（金标准维持、模型判错——需人工解释或再裁决）

- **`mock-post-0027` / 信息类型**：金标准 `MACRO` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `OTHER`
  - 原文：XAUUSD bias: short / bearish. Entry 2326-2335, stop 2346, ta…
- **`mock-post-0079` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：【美盘深度】本周黄金（XAUUSD）思路更新：黄金（XAUUSD）做多，入场 2378-2384，止损 2360，第一目…
- **`mock-post-0109` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：【美盘深度】本周黄金（XAUUSD）思路更新：黄金（XAUUSD）做空，入场 2408-2422，止损 2420，第一目…
- **`mock-post-0119` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `OTHER`
  - 原文：XAUUSD 2418 这个位置，现在还有多少人愿意进场？反正我是不着急。不同周期的信号可能冲突，需以交易周期为准。行情…
- **`mock-post-0127` / 信息类型**：金标准 `MACRO` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：黄金（XAUUSD）做多，入场 2426-2436，止损 2414，第一目标 2458，第二目标 2490，超短线，分钟…
- **`mock-post-0139` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：【美盘深度】本周黄金（XAUUSD）思路更新：黄金（XAUUSD）做空，入场 2438-2449，止损 2453，第一目…
- **`mock-post-0159` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `OTHER`
  - 原文：【策略笔记】黄金（XAUUSD）正处于关键位置，XAUUSD bias: short / bearish. Entry …
- **`mock-post-0169` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：【美盘深度】本周黄金（XAUUSD）思路更新：黄金（XAUUSD）做多，入场 2468-2476，止损 2450，第一目…
- **`mock-post-0187` / 信息类型**：金标准 `MACRO` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `TECHNICAL`
  - 原文：黄金（XAUUSD）做空，入场 2486-2501，止损 2504，第一目标 2445，第二目标 2404，超短线，分钟…
- **`mock-post-0207` / 信息类型**：金标准 `MACRO` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `OTHER`
  - 原文：XAUUSD bias: short / bearish. Entry 2506-2519, stop 2526, ta…
- **`mock-post-0219` / 信息类型**：金标准 `SENTIMENT` ↔ 模型 `TECHNICAL`（wrong_value）；正则侧 `OTHER`
  - 原文：【策略笔记】黄金（XAUUSD）正处于关键位置，XAUUSD bias: long / bullish. Entry 2…

## 6. 结论与下一步

- **人工子集口径**：LLM **PASS 4 项**（方向、周期、止损、目标位）、**FAIL 0 项**（无）；同一口径下正则 PASS 1 项。
- 信息类型若未达 90%：按 `docs/10 §4.9` 规则 9（L1~L6 阶梯）继续收敛口径，或把对应格子提交**人工二次裁决**（模型纠正人工 / 人工纠正模型都属正常对齐）。
- 本批语料为 **Mock 语料**（模板生成、含 126 条对抗样本）；**真实语料验收必须用真实作者帖子 + 全人工裁决重新抽样**（`docs/08 §5`）。

复现命令：

```powershell
python scripts/evaluate_extractor.py --extractor regex
python scripts/evaluate_extractor.py --extractor llm
python scripts/compare_extractor_baselines.py
```

