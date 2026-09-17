# Phase 3 W0-4 作者观点链报告

> 执行时间：2026-09-17（Asia/Shanghai）
> 状态：**PASS（受限工程链）**
> 边界：只验证真实帖子入库、观点基线与前瞻标签，不计算作者技能/权重，不宣称 Alpha。

## 1. 结论

采用 `docs/11 §5` 的方案 B，将 Phase 2 已验收的 19 条真实中文黄金快讯正式接入：

```text
CSV → raw_items(POST) → author_posts → processed_items → author_opinions
                                              ↓
                              GC=F 前瞻收益评价 CSV
```

首次导入新增 19 条，复跑新增 0 / 重复 19；观点管道首次处理 19 帖，复跑扫描 0。Regex 基线产出 11 条观点，并生成 31 条 horizon 评价行。初版曾有 12 条可计算收益；完成前置审计后，v2 门禁将 31 行全部标记为 `UNTRUSTED_COLLECTION_TIME`，正式研究可用标签为 0。

## 2. 入库与抽取结果

| 项 | 结果 |
|---|---:|
| 真实来源 / 作者 / 账号 | 2 / 2 / 2 |
| `raw_items(POST)` / `author_posts` | 19 / 19 |
| `processed_items` | 19 |
| `author_opinions` | 11（`parser_version=mock-regex-v1`） |
| 无观点帖子 | 14 |
| 单观点帖子 | 3 |
| 多观点帖子 | 2（各 4 条） |
| 管道失败 | 0 |

这里的 `mock-regex-v1` 只是确定性、可复现的工程基线，不是人工金标准，也不是 `llm-deepseek-v13` 的真实语料结论。Mock 200 条没有导入研究库。

19 行输入缺少独立 `collected_at`，导入器沿用此前已审计 CSV 的 `effective_at`，并在每条 `raw_json.collected_at_provenance=input_effective_at_fallback` 留痕。后续新增语料必须优先提供真实采集时间。

上述回填值只能证明导入、抽取和标签程序可以运行，不能证明这些信息在历史时点已经被系统获取。因此 `forward-return-v2` 会在查询行情前直接拒绝这些行，禁止其进入 OOS、作者技能或 Alpha 研究。

## 3. 标签口径

- entry：严格选择 `open_time > opinion.effective_at` 的下一根同 horizon K 线；等于信号时刻的 K 线会被测试强制排除；
- entry lag：`entry_at - effective_at` 必须小于等于被评价 horizon；周末或休市导致的超长等待记 `ENTRY_LAG_EXCEEDED`；
- exit：该完整 K 线的 `close_time = entry_at + horizon`；跨度不符记 `GAP_IN_HORIZON`；
- return：`log(exit_close / entry_open)`；
- 缺口：不插值、不缩窗，按状态计数；
- 未声明 horizon：不推断作者意图，而是在 Phase 3 批准的 `1h / 4h / 1d` 评价网格分别生成行，并标记 `horizon_source=evaluation_grid`；
- 行情代理：所有产物明确写 `provider_symbol=GC=F`、`instrument_proxy=COMEX_CONTINUOUS_FUTURES`，不得称为现货黄金结论。

## 4. 标签结果

| 状态 | 行数 | 解释 |
|---|---:|---|
| `UNTRUSTED_COLLECTION_TIME` | 31 | `collected_at` 由 `effective_at` 回填，只允许验证管道 |
| **合计** | **31** | 10 条缺 horizon 观点各展开 1h/4h/1d；另 1 条使用声明周期 |

初版的 12 条标签及其命中率仅保留为历史工程调试记录，不再视为有效研究结果。只有获得可核验的独立采集时间后，才能重新生成正式标签。

## 5. 验收对照

| W0-4 验收项 | 结果 |
|---|---|
| 观点 → 前瞻收益可复算 | PASS（固定版本、CSV + meta；不可信采集时间被硬拒绝） |
| 与 §3.4 对齐 | PASS（严格下一根、完整 horizon、对数收益） |
| 无观点留痕 | PASS（14 帖） |
| 多观点留痕 | PASS（2 帖各 4 条） |
| 缺失留痕 | PASS（采集时间 / horizon / stance / instrument 分开统计） |
| 幂等 | PASS（帖子复跑 0 新增；观点复跑扫描 0） |
| 防未来泄漏 | PASS（等时 K 线、回填采集时间、超 horizon 入场延迟均被排除） |

## 6. 限制与下一步

每位作者的有效标签远低于 Phase 3.3 的 30 条硬门槛，因此本轮不写 `author_weight_snapshots`，不计算作者排名，也不宣称任何 Alpha。下一工作包是 W0-5 特征底座；在其验收前不进入 Regime 或 Alpha 模型。
