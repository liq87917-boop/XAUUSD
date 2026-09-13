# 金标准生成报告（ground_truth_200）

- 生成时间（UTC）：`2026-09-13T06:20:57+00:00`
- 人工裁决输入：`D:\VSCodeProject\XAUUSD\logs\pending_review.csv`（sha256 前 16 位 `a688e86036773661`，格式：xlsx（ZIP 容器，文件名扩展名不代表真实格式））
- 三模型共识输入：`D:\VSCodeProject\XAUUSD\logs\model_consensus.csv`（sha256 前 16 位 `62820ffed947fa26`，格式：csv）
- 人工裁决归档副本：`D:\VSCodeProject\XAUUSD\logs\archive\pending_review_adjudicated_20260913-062057.xlsx`
- 输出：`D:\VSCodeProject\XAUUSD\logs\ground_truth_200.csv`（1000 行）；网格参考：`D:\VSCodeProject\XAUUSD\logs\annotation_sample.csv`

> **红线**：`source=3-model-consensus` 的条目**未经人工确认**。评估抽取器时必须**分层**：人工子集看真实准确率，共识子集只能当参考（`.clinerules`：LLM 输出不得充当验收依据）。
> `value` 为空串表示**人工/模型判定该字段不存在（未给出）**——这是合法金标准，评估时不要当缺失值丢弃。

## 1. 结论摘要

- **人工推翻模型：42 次 / 226 条人工裁决（18.6%）**
  - 采纳少数派（推翻多数派）：**36** 次
  - 三家都错（人工给出新答案）：**6** 次
  - 未推翻：采纳多数派 152 次；三家各异·采纳其一 32 次
- **金标准覆盖率：1000 / 1000 = 100.0%**（格子级：样本 × 字段）
  - 人工裁决：226 格（22.6%）——其中有具体取值 225、人工判定「未给出」1
  - 三模型共识（有值）：479 格（47.9%）
  - 三模型共识（三家都未给出）：295 格（29.5%）
  - 未裁决（人工漏填且无理由）：0 格
- **有具体取值的格子：704 / 1000 = 70.4%**
- 网格完整性：与抽样清单一致（无缺失格子）。

## 2. 字段级覆盖

| 字段 | 应有格 | 人工裁决 | 模型共识（有值） | 模型共识（都未给出） | 未裁决 |
|---|---|---|---|---|---|
| 方向（`stance`） | 200 | 14 | 186 | 0 | 0 |
| 周期（`horizon`） | 200 | 4 | 59 | 137 | 0 |
| 止损（`stop_loss`） | 200 | 29 | 92 | 79 | 0 |
| 目标位（`take_profit`） | 200 | 44 | 77 | 79 | 0 |
| 信息类型（`information_type`） | 200 | 135 | 65 | 0 | 0 |

## 3. 人工裁决 vs 模型（推翻分析）

| 人工裁决与三模型的关系 | 条数 | 占比 | 说明 |
|---|---|---|---|
| 采纳多数派（未推翻）（`matches-majority`） | 152 | 67.3% | 不计入推翻 |
| 采纳少数派（推翻多数派）（`matches-minority`） | 36 | 15.9% | **计入推翻** |
| 三家各异·采纳其一（未推翻）（`matches-single`） | 32 | 14.2% | 不计入推翻 |
| 三家都错·新答案（推翻全部）（`matches-none`） | 6 | 2.7% | **计入推翻** |

| 字段 | 人工裁决数 | 推翻数 | 推翻率 | 采纳少数派 | 三家都错 |
|---|---|---|---|---|---|
| 方向 | 14 | 0 | 0.0% | 0 | 0 |
| 周期 | 4 | 3 | 75.0% | 0 | 3 |
| 止损 | 29 | 11 | 37.9% | 11 | 0 |
| 目标位 | 44 | 13 | 29.5% | 13 | 0 |
| 信息类型 | 135 | 15 | 11.1% | 12 | 3 |

## 4. 最终金标准的取值分布（人工 vs 三模型共识）

| 字段 | 人工裁决 Top5 | 三模型共识 Top5 |
|---|---|---|
| 方向 | UNKNOWN×14 | UNKNOWN×79、LONG×53、SHORT×39、FLAT×15 |
| 周期 | 15M×3、∅（未给出）×1 | ∅（未给出）×137、1D×23、30M×10、1H×9、15M×9 |
| 止损 | 2387×2、2477×2、2318×1、2297×1、2332×1 | ∅（未给出）×79、2378×4、2360×4、2342×3、2324×3 |
| 目标位 | 2377×3、2445×3、2275×2、2292×2、2326×2 | ∅（未给出）×79、2411×5、2360×4、2292×3、2309×3 |
| 信息类型 | TECHNICAL×57、OTHER×24、SENTIMENT×23、POSITIONING×16、NEWS×8 | MACRO×33、TECHNICAL×22、POSITIONING×6、SENTIMENT×2、NEWS×2 |

## 5. 未裁决清单（若有）

无。（人工裁决要么给了取值，要么在 `notes` 里写明了「未给出」的理由。）

## 6. 输出与下游用法

| 列 | 含义 |
|---|---|
| `post_id` / `field` | 主键（样本 × 字段） |
| `value` | **归一化后的规范值**（评估用）；空串 = 该字段不存在（未给出） |
| `value_raw` | 原始文本（人工填写原样 / 模型共识原值） |
| `source` | `human-adjudicated` 或 `3-model-consensus` |
| `provenance` | 溯源：`review_id` + 三方原文 + 留空情况 / 共识 `raw_values` |
| `models_agreement` | `two_vs_one` / `three_way` / `unanimous` / `unanimous_blank` |
| `overturn` | 人工与模型的关系（仅人工行）：`matches-majority` / `matches-minority` / `matches-single` / `matches-none` |
| `reviewer` / `note` | 人工署名与判定理由 |

下游用法（评估 `RegexOpinionExtractor` 时）：

1. 只对 `source=human-adjudicated` 子集报**准确率**（唯一有验收效力的部分）；
2. `source=3-model-consensus` 子集单独列出，只用于**观察**抽取器与模型的一致性（不得当验收依据）；
3. `value` 为空串的格子语义是「应判为未给出」，评估时**不要**当缺失值丢弃。

复现命令：

```powershell
python scripts/build_ground_truth.py
```

