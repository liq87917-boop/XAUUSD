# Phase 3.3 Author / News Alpha 数据资格门禁报告

> 本报告只判断数据能否进入 OOS Alpha；资格不足时不写技能、权重或信号事实。

## 1. 结论

- Author Alpha：BLOCKED。
- News Alpha：BLOCKED。
- 因 Phase 3.2 两个基线均为负面结果，本轮不做任何跨 Alpha 融合。

## 2. Author 资格

硬门槛：每位作者至少 30 条可信、可标注观点。

| 作者 | 观点数 | 可信标签数 | 状态 |
|---|---:|---:|---|
| 华尔街见闻 | 9 | 0 | BLOCKED |
| 汇通网 | 2 | 0 | BLOCKED |

标签状态：UNTRUSTED_COLLECTION_TIME=31。

资格不足时不写 `author_skill_snapshots` 或 `author_weight_snapshots`。现有 `weight` 为 NOT NULL，因此旧规划中的“写 NULL 权重”不可执行，已修正文档。

## 3. News 资格

预注册工程下限：事件数 >= 200、历史 >= 90 天、单一来源占比 <= 40%。

- 事件数：30
- 历史跨度：56 天
- 最大来源占比：66.67%

| 来源 | 事件数 | 占比 |
|---|---:|---:|
| fed_press_releases | 20 | 66.67% |
| fred_blog | 10 | 33.33% |

HF 黄金标题弱监督行数：150。该数据没有本项目可审计的事件发布时间链，不能直接生成黄金前瞻收益标签，也不得回灌为观点金标准。

## 4. 下一动作

1. 保留只读门禁和负面证据，不创建 Phase 3.3 条件权重迁移。
2. 等待真实作者帖子具备独立 `published_at` 与 `collected_at` 后重新跑标签。
3. 新闻不能靠“今天下载旧标题”直接补历史：现有契约要求 `effective_at >= collected_at`，普通
   历史 CSV 会在今天才变得可用。须先取得能证明历史可用时间的数据源并设计对应契约；未达到
   门槛前不训练 News Alpha。
4. 可继续开发与数据无关的泄漏测试、报告和门禁，但不得产出效果结论。

用户交接字段与禁止事项见 `docs/operations/Phase3_3_用户数据交接说明.md`。
