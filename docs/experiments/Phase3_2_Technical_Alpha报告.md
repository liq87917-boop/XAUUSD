# Phase 3.2 Technical Alpha 独立 OOS 门禁报告

> **结论：FAIL（停止写入 Alpha 事实表）**。本报告只评价信号质量，不是交易策略或收益回测。

## 1. 冻结口径

- 数据：`XAUUSD_DUKASCOPY` 独立 Dukascopy 现货 1h 序列，不与 `GC=F` 混合。
- 特征：return / momentum / MA / RSI / MACD / ATR / realized vol / breakout。
- 标签：特征 bar 完成后，严格选择 `open_time > signal_at` 的下一根；持有 24 根可交易 1h bar；允许日常计划性停盘，周末/异常长缺口丢弃。
- 切分：60% train / 20% validation / 20% untouched test；边界各 embargo 24 根；禁止 shuffle。
- 模型：StandardScaler + L2 Logistic Regression；Platt 只在 validation 拟合。
- 正式统计：test 每 24 根抽 1 条非重叠标签；随机种子 42。
- PASS：IC >= 0.03 且 ICIR >= 0.3，或单侧精确二项检验 p < 0.05。

## 2. 数据与切分

- 原始 11,872；可用 9,343。
- train 5,605；validation 1,845；test 1,845。
- 正式非重叠 test 样本 77。
- 信号区间：2024-09-16T18:00:00+00:00 至 2026-09-16T20:00:00+00:00。
- 数据 SHA-256：`d0e0b39e1220b24428aa1cf39d7f68b9b5dc16ef8d25762d319eda77f7234d0d`
- 特征集：`technical-8-v1`；模型：`technical-lr-platt-v1`；seed=42；代码提交：`c20208714d5140c9a1f4e3d436fc6410f0c57af8`。

## 3. OOS 结果

| 信号 | IC | ICIR | 命中率 | Wilson 95% CI | p(>50%) | Brier | N |
|---|---:|---:|---:|---:|---:|---:|---:|
| model_lr_platt | -0.0951 | -0.4968 | 38.96% | [28.84%, 50.13%] | 1.0000 | 0.2540 | 77 |
| random | 0.1484 | 0.5014 | 54.55% | [43.47%, 65.19%] | 0.2472 | 0.3134 | 77 |
| always_long | — | — | 38.96% | [28.84%, 50.13%] | 1.0000 | 0.6104 | 77 |
| momentum | -0.0331 | -0.3536 | 51.95% | [40.96%, 62.75%] | 0.4099 | 0.4805 | 77 |
| ma | 0.0465 | 0.2526 | 53.25% | [42.22%, 63.97%] | 0.3244 | 0.4675 | 77 |

## 4. 概率校准

- 未校准 test Brier：0.2524
- Platt 校准 test Brier：0.2513
- 下表是完整 test 的可靠性分箱；未校准概率不得进入下游信号。

| 分箱 | N | 平均预测概率 | 实际上涨率 |
|---:|---:|---:|---:|
| 4 | 152 | 0.4921 | 0.6184 |
| 5 | 1693 | 0.5151 | 0.4737 |

## 5. 门禁动作

门槛未通过：不建立/不写入 `alpha_models`、`alpha_signals` 或 `predictions`；保留负面结果，随后独立验证 Macro Alpha，不做技术+宏观混合。

## 6. 边界

本结果不能证明可交易性；未计手续费、滑点、仓位、止损或风险预算。
