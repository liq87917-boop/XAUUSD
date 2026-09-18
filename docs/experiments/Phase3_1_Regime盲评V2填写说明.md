# Phase 3.1 Regime 第二轮盲评填写说明

## 1. 文件与验收口径

- 待填写文件：`logs/phase3_1_dukascopy_regime_blind_review_v2.csv`
- 共 50 行，按 6 类非 UNKNOWN 即时状态分层抽样。
- 本轮只评价“当前时点的即时状态语义”；12 根最短持有与 3 根确认的时序平滑由机器门禁单独验证。
- 只填写 `human_label` 和 `human_note`；其余列不得修改，答案键不得查看。
- 50 行必须全部填写，合法标签一致率达到 80% 才通过。

## 2. 各列含义

| 列 | 含义 |
|---|---|
| `review_id` | 样本编号；不得修改 |
| `start_at_utc` | 1h K 线开始时间，UTC |
| `start_at_beijing` | 同一时点的北京时间 |
| `close` | XAUUSD BID 收盘价，美元/盎司 |
| `close_vs_ema20_pct` | 收盘价相对 EMA20 的百分比；正数在均线上方，负数在下方 |
| `ema_20` | 20 小时指数移动均线 |
| `ema_60` | 60 小时指数移动均线 |
| `ema20_vs_ema60_pct` | EMA20 相对 EMA60 的百分比；正数表示短均线在长均线上方 |
| `ema_20_slope_4_pct` | EMA20 最近 4 小时变化百分比；正数向上，负数向下 |
| `ema_60_slope_4_pct` | EMA60 最近 4 小时变化百分比；正数向上，负数向下 |
| `adx_14` | 14 期 ADX 趋势强度；小于 20 偏震荡，大于等于 25 趋势较强 |
| `atr_14_percent` | ATR14 占价格的百分比，越高表示绝对波动越大 |
| `atr_rank_percentile` | 当前 ATR 在最近 60 小时中的百分位；80 表示高于约 80% 的近期时段 |
| `news_count_4h` | 过去 4 小时内可用新闻数量 |
| `macro_count_4h` | 过去 4 小时内可用宏观事件数量 |
| `human_label` | 人工判断标签；必填 |
| `human_note` | 简短判断理由；必填，建议写触发条件 |

## 3. 标签与唯一判断顺序

必须按以下优先级从上往下判断，命中第一项后停止：

1. `news_count_4h >= 3` 或 `macro_count_4h >= 1` → `NEWS_DRIVEN`
2. `atr_rank_percentile >= 80` → `HIGH_VOLATILITY`
3. `adx_14 >= 25`，且 `ema20_vs_ema60_pct > 0`、两条 slope 均大于 0 → `TREND_UP`
4. `adx_14 >= 25`，且 `ema20_vs_ema60_pct < 0`、两条 slope 均小于 0 → `TREND_DOWN`
5. `adx_14 < 20` → `RANGE`
6. `atr_rank_percentile <= 20` → `LOW_VOLATILITY`
7. 其余情况 → `RANGE`

合法标签只有：`NEWS_DRIVEN`、`HIGH_VOLATILITY`、`TREND_UP`、`TREND_DOWN`、`RANGE`、
`LOW_VOLATILITY`、`UNKNOWN`。本轮样本都已具备成熟指标，正常情况下不应填写 `UNKNOWN`。

## 4. 填写示例

只示范格式，不对应任何真实样本：

```text
human_label=NEWS_DRIVEN
human_note=macro_count_4h=1，按最高优先级判为新闻驱动
```

```text
human_label=TREND_UP
human_note=ADX>=25，EMA20在EMA60上方，且两条4小时斜率均为正
```

完成后保持 CSV 文件名和列结构不变，通知系统进行自动评分。
