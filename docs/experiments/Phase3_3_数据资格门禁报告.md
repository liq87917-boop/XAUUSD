# Phase 3.3 Author / News Alpha 数据资格门禁报告

> 本报告只读审计库内数量、标签和时间门槛；不核验来源许可或法律授权。

## 1. 结论

- 可用性审计时点：2026-09-19T16:57:58.166542+00:00。
- Author Alpha：BLOCKED（本报告未核验来源授权）。
- News Alpha：BLOCKED（本报告未核验来源授权及历史可用证据）。
- Author 库内门槛：BLOCKED；News 库内门槛：BLOCKED。
- 库内门槛 PASS 不等于 Alpha 放行；还需独立授权审查与合格数据交接。
- 因 Phase 3.2 两个基线均为负面结果，本轮不做任何跨 Alpha 融合。

## 2. Author 资格

硬门槛：每个稳定来源账号至少 30 条在审计时点前已完成可信标签的独立帖子；同一作者跨账号不能合并凑数。
可信帖子还必须在原始记录中标明独立采集时间来源 `collected_at_provenance=independent_observation`；缺失标记或仅有输入/回填时间不计入。

| 作者 | 观点数 | 有可信标签的独立帖子数 | 状态 |
|---|---:|---:|---|
| 华尔街见闻 | 9 | 0 | BLOCKED |
| 汇通网 | 2 | 0 | BLOCKED |

| 作者 | 来源/账号 | 有可信标签的独立帖子数 | 状态 |
|---|---|---:|---|
| 华尔街见闻 | manual-华尔街见闻/manual-华尔街见闻 | 0 | BLOCKED |
| 汇通网 | manual-汇通网/manual-汇通网 | 0 | BLOCKED |

账号与作者归属不一致：无。

标签状态：UNTRUSTED_COLLECTION_TIME=31。

整体资格不足时不写 `author_skill_snapshots` 或 `author_weight_snapshots`。现有 `weight` 为 NOT NULL，因此旧规划中的“写 NULL 权重”不可执行，已修正文档。

## 3. News 资格

预注册工程下限：事件数 >= 200、可用时间跨度 >= 90 天、单一来源占比 <= 40%。

- 事件数：30
- 未来可用解析行排除数：0
- 可用时间跨度：0 天（逐条取 news_events 与 raw_items 的较晚 effective_at）
- 最大来源占比：66.67%

| 来源 | 事件数 | 占比 |
|---|---:|---:|
| fed_press_releases | 20 | 66.67% |
| fred_blog | 10 | 33.33% |

HF 黄金标题弱监督行数：150。该数据没有本项目可审计的事件发布时间链，不能直接生成黄金前瞻收益标签，也不得回灌为观点金标准。

## 4. 下一动作

1. 保留只读门禁和负面证据，不创建 Phase 3.3 条件权重迁移。
2. 等待真实作者帖子具备独立 `published_at` 与 `collected_at` 后重新跑标签。
3. 新闻不能靠今天下载旧标题直接补历史：现有契约要求 `effective_at >= collected_at`。
   须取得能证明历史可用时间的合规数据源；未达到门槛前不训练 News Alpha。
4. 可继续开发与数据无关的泄漏测试、报告和门禁，但不得产出效果结论。

用户交接字段与禁止事项见 `docs/operations/Phase3_3_用户数据交接说明.md`。
