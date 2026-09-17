# Phase 3 W0-3 新闻回填报告

> 执行时间：2026-09-17（Asia/Shanghai）
> 状态：**PASS（受限数据底座）**
> 范围：只完成 RSS 新闻底座，不进入 News Alpha、Regime、预测、策略或实盘。

## 1. 结论

白名单源 `fred_blog` 与 `fed_press` 已通过正式采集框架写入本地研究库：首轮 **30 条新增**，第二轮及最终复跑均为 **0 新增 / 30 重复**。每条数据同时保留 `raw_items` 原始层和 `news_events` 结构化层，时间约束违规为 0。

本轮使用 2026-09-13 已完成 robots 合规验证并保存的响应缓存，落库与复跑阶段为**零网络重放**。这能证明工程链路和可复现性，但 30 条只是源端当前可见快照，不代表完整的 90 天新闻历史。

## 2. 数据结果

| 来源 | 用途 | 条数 | 占比 | 发布时间范围（UTC） | 结论 |
|---|---|---:|---:|---|---|
| `fred_blog` | NEWS（宏观分析长文） | 10 | 33.3% | 2026-08-06 13:00 ～ 2026-09-10 13:00 | 可作为新闻/背景特征候选 |
| `fed_press` | EVENT（美联储公告） | 20 | 66.7% | 2026-07-16 15:00 ～ 2026-09-11 14:00 | 只作事件源，不冒充作者观点 |
| **合计** | — | **30** | 100% | — | 结构化率 30/30 |

自动集中度门禁已触发：`fed_press` 单源占比 **66.7% > 40%**。该门禁是告警而非数据删除规则；后续 News Alpha 报告必须继续披露这一偏斜，不能把当前样本描述为多源均衡语料。

结构化事件类型分布：`Uncategorized=10`、`Enforcement Actions=8`、`Banking and Consumer Regulatory Policy=5`、`Orders on Banking Applications=4`、`Monetary Policy=3`。

## 3. 合规与时间门禁

- `fred_blog`：2026-09-13 实测 robots 允许、feed HTTP 200；
- `fed_press`：2026-09-13 实测 robots.txt 404（按规则视为无限制）、feed HTTP 200；
- 两源的逐源判定、HTTP 状态和条目数均留在 `logs/rss_verification.log`；
- `ecb_press` 按既有业务裁决保持 `enabled=false`，本轮未请求；其余未验证或失败源也未请求；
- 30 条均有明确时区的发布时间，`published_at <= effective_at` 违规 **0**；
- 采集器新增硬规则：发布时间不可用时只写 `raw_items`，不生成 `news_events`，避免把 `collected_at` 冒充发布时间。

## 4. 工程交付

1. `scripts/collect_rss.py --to-db` 已从占位接通至 `run_collector`；真实写库仍需显式 `--no-dry-run`；
2. 每个 RSS 源映射到独立数据库来源，写入 `collector_runs`，保留成功、插入、重复与失败计数；
3. `RssCollector` 增加跨来源内容哈希去重及 `news_events` 结构化落库；
4. `--lookback-days` 默认 90，超窗条目由采集器过滤，不缩窗、不伪造历史；
5. 统计 JSON 新增来源分布与单源占比 >40% 告警；
6. 新增 `fred_blog` 来源种子，并支持通过 `source_ids` 把调度器限制到指定白名单源。

## 5. 验收对照

| W0-3 验收项 | 结果 |
|---|---|
| 每源 robots 判定留痕 | PASS（2026-09-13 实测日志） |
| 条数与来源分布报告 | PASS（10 / 20，共 30） |
| 单源占比 ≤40% 告警 | PASS（66.7%，自动告警已触发） |
| 原始层与结构化层可追溯 | PASS（30 `raw_items` / 30 `news_events`） |
| 幂等复跑 | PASS（0 新增 / 30 重复） |
| 时间因果 | PASS（违规 0） |

## 6. 限制与下一步

- RSS 只暴露当前有限条目，`lookback_days=90` 是过滤窗口，不会凭空补出源端未提供的历史；
- 当前样本量不足以训练或宣称 News Alpha 有效，且来源偏向美联储公告；
- 下一工作包应进入 W0-4 作者观点链；若后续需要扩大新闻历史，应新增经过同等 robots/授权验证的来源或评审正式新闻 API，不能抓取未授权页面。
