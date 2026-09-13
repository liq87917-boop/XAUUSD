# Phase 2 基线对比报告 · 正则抽取器 vs LLM 抽取器

- 生成时间（UTC）：`2026-09-13T08:31:37+00:00`
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
| 信息类型（`information_type`） | 135 | 55 | 40.7% | 118 | 87.4% | **+46.7 pp** | —（无门槛） |

## 2. 五类判定构成（全量口径，含共识格）

| 抽取器 | 字段 | 格子 | 命中 | 双方未给出 | 该判未判 | 提取错误 | 不该判却判 | 精确率 |
|---|---|---|---|---|---|---|---|---|
| 正则 | 方向 | 200 | 144 | 0 | 27 | 29 | 0 | 83.2% |
| 正则 | 周期 | 200 | 15 | 134 | 22 | 25 | 4 | 34.1% |
| 正则 | 止损 | 200 | 40 | 79 | 81 | 0 | 0 | 100.0% |
| 正则 | 目标位 | 200 | 38 | 79 | 79 | 4 | 0 | 90.5% |
| 正则 | 信息类型 | 200 | 75 | 0 | 80 | 45 | 0 | 62.5% |
| LLM | 方向 | 200 | 200 | 0 | 0 | 0 | 0 | 100.0% |
| LLM | 周期 | 200 | 62 | 137 | 0 | 0 | 1 | 98.4% |
| LLM | 止损 | 200 | 121 | 40 | 0 | 0 | 39 | 75.6% |
| LLM | 目标位 | 200 | 121 | 40 | 0 | 0 | 39 | 75.6% |
| LLM | 信息类型 | 200 | 179 | 0 | 0 | 21 | 0 | 89.5% |

## 3. LLM 相对正则的逐格变化

- **修复（正则未命中 → LLM 命中）：385 格**
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
  - …其余 373 格见 `logs/extractor_eval_llm.csv`
- **回归（正则命中 → LLM 未命中）：14 格**
  - `mock-post-0029` / 信息类型：金标准 `SENTIMENT`，正则 `SENTIMENT`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0059` / 信息类型：金标准 `SENTIMENT`，正则 `SENTIMENT`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0060` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0080` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0089` / 信息类型：金标准 `SENTIMENT`，正则 `SENTIMENT`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0090` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0110` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0140` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0160` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0170` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0190` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）
  - `mock-post-0220` / 信息类型：金标准 `NEWS`，正则 `NEWS`（命中），LLM `TECHNICAL`（wrong_value）

## 4. Token 与费用（LLM 侧）

- `api`：`{'cache_hit_tokens': 567808, 'cache_miss_tokens': 37391, 'completion_tokens': 16615, 'posts': 180, 'prompt_tokens': 605199}`
- `api_estimated_cost_usd_offpeak`：`0.017281074`
- `api_estimated_cost_usd_peak`：`0.034562148`
- `cached`：`{'cache_hit_tokens': 60672, 'cache_miss_tokens': 6568, 'completion_tokens': 1852, 'posts': 20, 'prompt_tokens': 67240}`
- `full_set_estimated_cost_usd_offpeak`：`0.01955949`
- `full_set_estimated_cost_usd_peak`：`0.03911898`

## 5. 结论与下一步

- **人工子集口径**：LLM **PASS 4 项**（方向、周期、止损、目标位）、**FAIL 0 项**（无）；同一口径下正则 PASS 1 项。
- 信息类型若未达 90%：按 `docs/10 §4.9` 规则 9（L1~L6 阶梯）继续收敛口径，或把对应格子提交**人工二次裁决**（模型纠正人工 / 人工纠正模型都属正常对齐）。
- 本批语料为 **Mock 语料**（模板生成、含 126 条对抗样本）；**真实语料验收必须用真实作者帖子 + 全人工裁决重新抽样**（`docs/08 §5`）。

复现命令：

```powershell
python scripts/evaluate_extractor.py --extractor regex
python scripts/evaluate_extractor.py --extractor llm
python scripts/compare_extractor_baselines.py
```

