# Phase 2 · LLM 抽取器 Prompt 迭代对比（20 条试点 · v1 → v6）

> 数据来源：同一批 20 条样本（`--limit 20 --sample head`，`logs/annotation_sample.csv` 的前 20 个 `post_id`），5 个 Prompt 版本各跑一次真实 API。
> 判定依据：**`source=human-adjudicated` 子集是唯一绝对金标准**；`3-model-consensus` 子集只作参考（其「未给出」格生成于 `docs/10 §4.9` 冻结之前）。

## 1. 逐字段准确率（人工子集口径）

| 字段 | 金标准有值 | v1 | v2 | v3 | v4 | v5 | v6 |
|---|---|---|---|---|---|---|---|
| 方向（stance） | 2 | 2/2 = 100.0% | 2/2 = 100.0% | 2/2 = 100.0% | 2/2 = 100.0% | 2/2 = 100.0% | 2/2 = 100.0% |
| 周期（horizon） | 1 | 0/1 = 0.0% | 0/1 = 0.0% | 0/1 = 0.0% | 0/1 = 0.0% | 0/1 = 0.0% | 1/1 = 100.0% |
| 止损（stop_loss） | 4 | 4/4 = 100.0% | 4/4 = 100.0% | 4/4 = 100.0% | 4/4 = 100.0% | 4/4 = 100.0% | 4/4 = 100.0% |
| 目标位（take_profit） | 6 | 6/6 = 100.0% | 6/6 = 100.0% | 6/6 = 100.0% | 6/6 = 100.0% | 6/6 = 100.0% | 6/6 = 100.0% |
| 信息类型（information_type） | 13 | 6/13 = 46.2% | 12/13 = 92.3% | 11/13 = 84.6% | 12/13 = 92.3% | 12/13 = 92.3% | 13/13 = 100.0% |

## 2. 五类判定（100 格全量）

| 版本 | 命中 | 双方未给出 | 漏判 | 错判 | 多判 |
|---|---|---|---|---|---|
| v1 | 54 | 23 | 5 | 8 | 10 |
| v2 | 64 | 23 | 1 | 2 | 10 |
| v3 | 62 | 23 | 1 | 4 | 10 |
| v4 | 65 | 23 | 1 | 1 | 10 |
| v5 | 65 | 23 | 1 | 1 | 10 |
| v6 | 67 | 23 | 0 | 0 | 10 |

> 说明：`v5` 的 10 个「多判」**全部来自共识金标准**（条件句/引用句/复盘句的止损与目标位，共识写「未给出」、人工口径要求**照标**）→ 按 `docs/10 §4.9` 规则 8 属于**金标准分层差异，不是模型错误**；人工子集内 `spurious = 0`。

## 3. 重点场景：条件句 / 引用句 / 多目标（原文 + 金标准 + v1/v5 预测）

### conditional（条件句）

**`mock-post-0001`**　原文：如果晚间的通胀数据超预期，黄金才会转做空，目标 2325，止损 2288；否则继续观察。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "MACRO（3-model-consensus）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2288", "take_profit": "2325", "information_type": "MACRO"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2288", "take_profit": "2325", "information_type": "MACRO"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2288,"take_profit":2325,"information_type":"MACRO","rationale":"若通胀数据超预期才转做空，目标 2325，止损 2288"}],"no_opinion":false}`

**`mock-post-0008`**　原文：看图操作，XAUUSD 在 2307 附近的关键位置已经标注在图上。仓位与风险控制优先于方向判断，这是长期生存前提。数据公布前后波动率往往快速抬升，注意仓位管理。以上判断基于当前信息，若宏观环境变化将及时更新。本文仅为个人研究记录，不构成任何投资建议。从结构上看，价格仍在震荡区间内运行。当前关键在于美盘开盘后的成交量能否放大。行情数据来自公开渠道，可能存在滞后或误差。不同周期的信号可能冲突，需以交易周期为准。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "TECHNICAL（3-model-consensus）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "-", "take_profit": "-", "information_type": "TECHNICAL"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "-", "take_profit": "-", "information_type": "TECHNICAL"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":null,"take_profit":null,"information_type":"TECHNICAL","rationale":"看图操作，XAUUSD 2307 附近关键位置已标注，价格仍在震荡区间内运行"}],"no_opinion":false}`

**`mock-post-0017`**　原文：如果晚间的通胀数据超预期，黄金才会转做空，目标 2357，止损 2297；否则继续观察。不同周期的信号可能冲突，需以交易周期为准。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "MACRO（3-model-consensus）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2297", "take_profit": "2357", "information_type": "MACRO"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2297", "take_profit": "2357", "information_type": "MACRO"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2297,"take_profit":2357,"information_type":"MACRO","rationale":"若通胀数据超预期才转做空，目标 2357，止损 2297；否则继续观察"}],"no_opinion":false}`

**`mock-post-0018`**　原文：有机构认为黄金应当做多，目标 2292，止损 2337，我的看法稍后单独给出。当前关键在于美盘开盘后的成交量能否放大。以上判断基于当前信息，若宏观环境变化将及时更新。若价格与预期相反，先降低仓位再等新的信号。行情数据来自公开渠道，可能存在滞后或误差。上方阻力与下方支撑相对清晰，暂未出现假突破。本文仅为个人研究记录，不构成任何投资建议。从结构上看，价格仍在震荡区间内运行。数据公布前后波动率往往快速抬升，注意仓位管理。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "OTHER（human-adjudicated）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2337", "take_profit": "2292", "information_type": "OTHER"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2337", "take_profit": "2292", "information_type": "OTHER"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2337,"take_profit":2292,"information_type":"OTHER","rationale":"机构认为黄金做多，目标 2292，止损 2337，作者未表态"}],"no_opinion":false}`

**`mock-post-0019`**　原文：昨天黄金做多已止盈 2292，止损 2330 没有被触发，今天重新评估。仓位与风险控制优先于方向判断，这是长期生存前提。当前关键在于美盘开盘后的成交量能否放大。以上判断基于当前信息，若宏观环境变化将及时更新。本文仅为个人研究记录，不构成任何投资建议。不同周期的信号可能冲突，需以交易周期为准。宏观层面需继续跟踪实际利率与美元指数的联动。盘面情绪指标显示多空分歧仍然较大。数据公布前后波动率往往快速抬升，注意仓位管理。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "OTHER（human-adjudicated）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2330", "take_profit": "2292", "information_type": "TECHNICAL"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2330", "take_profit": "2292", "information_type": "OTHER"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2330,"take_profit":2292,"information_type":"OTHER","rationale":"昨天黄金做多已止盈 2292，止损 2330 未触发，今天重新评估"}],"no_opinion":false}`

**`mock-post-0020`**　原文：不排除黄金做多，目标 2292，止损 2332，方向仍需要数据确认。上方阻力与下方支撑相对清晰，暂未出现假突破。仓位与风险控制优先于方向判断，这是长期生存前提。盘面情绪指标显示多空分歧仍然较大。若价格与预期相反，先降低仓位再等新的信号。以上判断基于当前信息，若宏观环境变化将及时更新。不同周期的信号可能冲突，需以交易周期为准。数据公布前后波动率往往快速抬升，注意仓位管理。行情数据来自公开渠道，可能存在滞后或误差。

- 金标准：`{"stance": "UNKNOWN（human-adjudicated）", "horizon": "未给出（3-model-consensus）", "stop_loss": "2332（human-adjudicated）", "take_profit": "2292（human-adjudicated）", "information_type": "TECHNICAL（human-adjudicated）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2332", "take_profit": "2292", "information_type": "TECHNICAL"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2332", "take_profit": "2292", "information_type": "TECHNICAL"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":0.4,"entry_low":null,"entry_high":null,"stop_loss":2332,"take_profit":2292,"information_type":"TECHNICAL","rationale":"不排除黄金做多，目标 2292，止损 2332，方向仍需数据确认"}],"no_opinion":false}`

### quote（引用句）

**`mock-post-0002`**　原文：有机构认为黄金应当做多，目标 2327，止损 2288，我的看法稍后单独给出。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "OTHER（human-adjudicated）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2288", "take_profit": "2327", "information_type": "OTHER"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2288", "take_profit": "2327", "information_type": "OTHER"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2288,"take_profit":2327,"information_type":"OTHER","rationale":"机构认为黄金做多，目标 2327，止损 2288，作者未表态"}],"no_opinion":false}`

**`mock-post-0018`**　原文：有机构认为黄金应当做多，目标 2292，止损 2337，我的看法稍后单独给出。当前关键在于美盘开盘后的成交量能否放大。以上判断基于当前信息，若宏观环境变化将及时更新。若价格与预期相反，先降低仓位再等新的信号。行情数据来自公开渠道，可能存在滞后或误差。上方阻力与下方支撑相对清晰，暂未出现假突破。本文仅为个人研究记录，不构成任何投资建议。从结构上看，价格仍在震荡区间内运行。数据公布前后波动率往往快速抬升，注意仓位管理。

- 金标准：`{"stance": "UNKNOWN（3-model-consensus）", "horizon": "未给出（3-model-consensus）", "stop_loss": "未给出（3-model-consensus）", "take_profit": "未给出（3-model-consensus）", "information_type": "OTHER（human-adjudicated）"}`
- v1 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2337", "take_profit": "2292", "information_type": "OTHER"}`
- v5 预测：`{"stance": "UNKNOWN", "horizon": "-", "stop_loss": "2337", "take_profit": "2292", "information_type": "OTHER"}`
- v5 原始 JSON：`{"opinions":[{"stance":"UNKNOWN","instrument":"XAUUSD","horizon":null,"confidence":null,"entry_low":null,"entry_high":null,"stop_loss":2337,"take_profit":2292,"information_type":"OTHER","rationale":"机构认为黄金做多，目标 2292，止损 2337，作者未表态"}],"no_opinion":false}`

### multitarget（多目标）

**`mock-post-0025`**　原文：黄金（XAUUSD）做空，入场 2324-2331，止损 2342，第一目标 2292，第二目标 2260，超短线，分钟级操作。ETF 持仓连续三日增仓，资金流回暖。

- 金标准：`{"stance": "SHORT（3-model-consensus）", "horizon": "15M（3-model-consensus）", "stop_loss": "2342（3-model-consensus）", "take_profit": "2292（human-adjudicated）", "information_type": "POSITIONING（human-adjudicated）"}`
- v1 预测：`{"stance": "SHORT", "horizon": "15M", "stop_loss": "2342", "take_profit": "2292", "information_type": "TECHNICAL"}`
- v5 预测：`{"stance": "SHORT", "horizon": "15M", "stop_loss": "2342", "take_profit": "2292", "information_type": "POSITIONING"}`
- v5 原始 JSON：`{"opinions":[{"stance":"SHORT","instrument":"XAUUSD","horizon":"15m","confidence":0.7,"entry_low":2324,"entry_high":2331,"stop_loss":2342,"take_profit":2292,"information_type":"POSITIONING","rationale":"黄金做空，入场 2324-2331，止损 2342，第一目标 2292；ETF 持仓连续三日增仓，资金流回暖"}],"no_opinion":false}`

## 4. 残余分歧（v5）与待人工裁决项

1. **`mock-post-0009` 信息类型**：金标准 `SENTIMENT`（人工）↔ v5 `TECHNICAL`。该帖同时含英文操作块（bias/Entry/stop/target）与「关注 risk-on 情绪主导，黄金短线承压」。**待裁决**：这类「操作 + 带方向含义的情绪句」应判 `SENTIMENT` 还是 `TECHNICAL`？（v2 判对、v4/v5 判错；而「中性盘面情绪描述」的 `mock-post-0020` 恰好相反）——建议在 `docs/10 §4.9` 规则 9 的 L3 下再补一句判定线。
2. **`mock-post-0028` 周期**：金标准 `15M`（人工）↔ 各版本均未给出。正文只有「短线思路」字样，而 `docs/10 §4.2` 的映射表是「**短线 → 1H**」。**待裁决**：是金标准该改为 `1H`，还是 `§4.2` 需要为「短线思路：观望」这类表述新增一条 → `15m`？

## 5. 成本与安全

- 每个版本 20 条真实调用：输入约 3.3 万 ~ 6.3 万 tokens（上下文缓存命中 83% ~ 91%）、输出约 0.17 万 tokens → **单版本 ≈ $0.004（≈¥0.03）**；v1→v5 合计 ≈ **$0.023**。
- 全量 200 条预估：输入 ≈ 63 万 tokens → **≈ $0.045（≈¥0.32）**。
- 缓存：`logs/llm_cache/`（`.gitignore` 已显式忽略）；`readonly` 复跑 **¥0**。
- 密钥只从 `.env` 读取，无 `--api-key` 参数；响应缓存经 `redact()`，实测无 `sk-` 明文。

复现命令：

```powershell
python scripts/evaluate_extractor.py --extractor llm --limit 20 --llm-cache-mode auto
python scripts/evaluate_extractor.py --extractor llm --limit 20 --llm-cache-mode readonly  # ¥0 复跑
```

