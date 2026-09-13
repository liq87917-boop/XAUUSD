# 07_每个阶段的Cline执行Prompt.md

# GOLD-AI 每个阶段的 Cline 执行 Prompt V1.0

> 使用方式：进入对应阶段时，将该阶段 Prompt 直接交给 Cline。执行前必须让 Cline 同时读取 01～08 开发文档。

---

# Phase 0 Prompt：工程骨架

```text
你现在开发 GOLD-AI 黄金智能交易研究与策略进化系统。

先完整阅读项目中的：
01_系统总体架构设计
02_系统详细设计.md
03_数据库完整设计.md
04_数据表结构及字段定义.md
05_分阶段开发路线图.md
06_Cline开发规则.md
08_测试与验收标准.md

本轮只执行 Phase 0：工程骨架与开发基础。

目标：
1. 建立 FastAPI 后端项目结构。
2. 配置 PostgreSQL、SQLAlchemy、Alembic。
3. 配置 Redis 和异步 Worker 基础结构。
4. 建立 config/settings/logging/common 模块。
5. 建立统一 UTC 时间规则。
6. 建立 UUID v7 主键工具。
7. 建立统一异常和 API 错误结构。
8. 建立 /api/v1/system/health。
9. 建立 pytest 测试结构。
10. 建立 Docker Compose 开发环境。
11. 创建 .env.example，并强制：LIVE_TRADING=false，ALLOW_EXTERNAL_ORDER_SUBMISSION=false。
12. 如前端尚不存在，建立 React+TypeScript+Vite 基础骨架。

不要开发 Collector、Author Alpha、Strategy、Backtest 等后续功能。

开始编码前先输出“本轮计划修改清单”。
完成后按 06_Cline开发规则规定格式汇报，并运行测试。
```

---

# Phase 1 Prompt：Gold Intelligence Database

```text
继续开发 GOLD-AI。

先读取 01～08 文档以及现有代码，然后只执行 Phase 1：Gold Intelligence Database。

本轮目标：

A. 数据库
建立并迁移：
- sources
- authors
- author_accounts
- raw_items
- raw_media
- collector_runs
- processed_items
- author_posts
- instruments
- market_bars
- news_events
- macro_events
- job_runs
- data_versions
- audit_logs

B. 数据源管理
- Source CRUD
- Author CRUD
- Author Account CRUD
- 首期支持配置 50～100 作者

C. Collector Framework
- BaseCollector
- collector registry
- health check
- retry
- idempotency
- cursor/checkpoint
- collector_runs

D. 数据处理基础
- normalize
- deduplicate
- content_hash
- timezone normalization
- published_at / collected_at / effective_at

E. 市场数据
首期支持 XAUUSD，架构上可扩展 DXY、美债、USD/CNY、COMEX、Silver、Oil、BTC。

F. Scheduler
建立每 30 分钟调度框架，但当前只运行 Phase 1 已存在的数据任务。

G. Dashboard
最少提供：
- 数据源状态
- Author 列表
- 原始数据列表
- Market Bar 查询
- Collector Run / Job Monitor

最高优先级：
- 原始数据永不覆盖
- 幂等
- 时间字段正确
- 数据可追溯

不要实现 Author Skill、Alpha、Strategy。

开始前输出计划清单；完成后运行 unit + integration + data quality 测试。
```

---

# Phase 2 Prompt：Author Lab

```text
执行 Phase 2：Author Lab。

必须建立：
1. author_opinions
2. propagation_edges
3. author_skill_snapshots
4. author_weight_snapshots 的基础结构

开发：
- Post -> Opinion 解析 Pipeline
- stance: LONG/SHORT/FLAT/UNKNOWN
- confidence
- horizon
- entry_low/entry_high
- stop_loss
- take_profit
- rationale
- information_type

图片：
- Level 1 OCR 必须完成
- Level 2 可预留接口，不强制复杂视觉模型

建立历史评价：
- 15m
- 30m
- 1h
- 4h
- 1d

建立 Author Skill：
- Direction
- Timing
- Entry
- Exit
- Horizon
- Calibration
- Independence

必须实现样本量修正，不能简单用胜率作为总分。

传播关系至少支持：
- repost/quote
- semantic similarity
- independence score

Dashboard：
- Author Lab
- Author Detail
- Opinion Timeline
- Skill Snapshot
- Independence

所有 LLM/解析器输出必须记录 parser/model version。
所有测试必须避免 future leakage。
```

---

# Phase 3 Prompt：Alpha Lab

```text
执行 Phase 3：Alpha Lab。

建立：
- feature_sets
- feature_snapshots
- market_regimes
- alpha_models
- alpha_signals
- prediction models
- predictions
- calibration models
- ensemble 基础接口

Regime 第一版采用规则 + 统计，不要直接上深度学习。

Regime：
TREND_UP
TREND_DOWN
RANGE
HIGH_VOLATILITY
LOW_VOLATILITY
NEWS_DRIVEN
UNKNOWN

建立 Alpha：
- Author Alpha
- News Alpha
- Macro Alpha
- Technical Alpha
- Market Structure Alpha 接口

Technical 首期特征：
- return
- momentum
- moving average
- RSI
- MACD
- ATR
- realized volatility
- breakout

Benchmark Model：
- Logistic Regression
- LightGBM
- XGBoost（如依赖环境允许）

Prediction 输出：
- future return
- up/down probability
- volatility
- interval
- tail risk
- no trade probability

Calibration：Platt / Isotonic 至少一种完成。

严格要求：
feature.max_effective_at <= prediction_at

必须增加 leakage test。

Dashboard：
- Regime
- Alpha Lab
- Model Compare
- Prediction
```

---

# Phase 4 Prompt：Strategy Lab + Backtest

```text
执行 Phase 4：Strategy Lab + Backtest。

建立：
- strategies
- strategy_versions
- strategy_dna
- experiments
- backtest_runs
- backtest_trades
- backtest_metrics
- walk_forward_runs/windows
- monte_carlo_runs
- stress_test_runs

Strategy DNA 必须包含：
Signal
Confirmation
Market Condition
Entry
Stop Loss
Take Profit
Holding Period
News Filter
Position Rule

任何修改都产生新 strategy_version，禁止覆盖。

Backtest 必须加入：
- spread
- fee
- slippage
- transaction cost

必须支持：
- normal backtest
- walk-forward
- OOS
- Monte Carlo 基础版
- Stress Test 基础版

指标至少：
Return/CAGR
Sharpe
Sortino
Max Drawdown
Profit Factor
Expectancy
Calmar
Turnover
Tail Risk

禁止任何未来价格泄漏。

Dashboard：
- Strategy Lab
- Version History
- Backtest Lab
- Equity Curve
- Trade List
- Metrics
- OOS Compare
```

---

# Phase 5 Prompt：Risk + Paper Trading

```text
执行 Phase 5：Risk Engine + Paper Trading。

建立：
- risk_policies
- risk_evaluations
- paper_accounts
- paper_orders
- paper_fills
- paper_positions
- paper_trades
- risk_snapshots

Risk Engine 必须独立于 Prediction/Strategy。

至少实现：
- risk budget
- max risk per trade
- max total exposure
- max drawdown guard
- volatility position scaling
- news event guard
- consecutive loss deleverage
- model/data invalid => NO TRADE

Risk Evaluation 输出：
allowed
risk_score
recommended_position
max_position
rejection_reasons

Paper Trading：
- CREATED
- SUBMITTED
- PARTIALLY_FILLED
- FILLED
- CANCELLED
- REJECTED
- CLOSED

必须模拟 slippage 和 fee。

禁止真实券商订单接口。
LIVE_TRADING 必须继续 false。

Dashboard：
- Risk
- Paper Orders
- Fills
- Positions
- PnL
```

---

# Phase 6 Prompt：Meta Ensemble + Dynamic Weight

```text
执行 Phase 6：Meta Ensemble + Dynamic Weight。

建立：
- ensemble_runs
- ensemble_components
- attribution_runs/items
- ablation_runs/results
- bandit_states

融合必须考虑：
- author weight
- independence
- regime match
- calibrated confidence
- correlation
- historical marginal contribution

实现：
- opinion dispersion
- correlation de-duplication
- contribution breakdown
- ablation
- counterfactual 基础版

作者动态权重：
- Bayesian update 基础版
- Contextual Bandit 接口
- Thompson Sampling 可实现基础版本

作者低权重进入 PROBATION，不永久删除。

必须用 OOS 证明动态 ensemble 相比 baseline 的提升。
```

---

# Phase 7 Prompt：Evolution Engine

```text
执行 Phase 7：Evolution Engine。

建立：
- evolution_rounds
- evolution_candidates
- mutation_records
- crossover_records
- promotion_records

策略生命周期：
DRAFT
CANDIDATE
CHALLENGER
CHAMPION
RETIRED
REJECTED

实现：
- mutation
- crossover
- candidate generation
- evaluation pipeline
- promotion gate
- demotion
- retirement
- regime-based reactivation

强制门禁：
Backtest -> Walk-forward -> OOS -> Paper Trading -> Risk -> Promotion

任何阶段失败不得 Champion。

所有进化过程必须可重放、可追溯父策略和参数变化。
```

---

# Phase 8 Prompt：Autonomous Research Agent

```text
执行 Phase 8：Autonomous Research Agent。

目标不是自动实盘，而是自动研究。

Agent 可以：
- 读取已有实验
- 找表现差异
- 提出研究假设
- 创建新 Experiment
- 创建新 Feature/Model/Strategy candidate
- 自动触发 Backtest/OOS
- 比较结果
- 生成研究报告
- 将有效结果提交为 Challenger

Agent 禁止：
- 修改 LIVE_TRADING
- 绕过 Risk
- 直接真实下单
- 删除历史实验
- 无验证晋级 Champion

每个 Agent Action 必须 audit。

必须加入预算限制：
- 单轮最大实验数
- 最大运行时间
- 最大并发
- 失败上限
```

---

# 通用 Bug 修复 Prompt

```text
请修复当前 GOLD-AI Bug。

规则：
1. 先阅读 06_Cline开发规则.md。
2. 先复现 Bug。
3. 找根因，不做表面绕过。
4. 添加失败测试/回归测试。
5. 再修改代码。
6. 运行相关测试和全量测试。
7. 不修改与该 Bug 无关的架构。
8. 如果涉及数据库，必须通过 Alembic migration。
9. 如果涉及时间、特征、回测，必须检查未来数据泄漏。
10. 完成后列出根因、修改文件、测试结果。
```

---

# 通用阶段验收 Prompt

```text
现在不要继续新增功能。
对当前 Phase 做完整验收。

依据：
- 05_分阶段开发路线图.md
- 08_测试与验收标准.md

检查：
- 功能完整性
- API
- DB
- migration
- unit test
- integration test
- regression test
- data quality
- leakage
- scheduler
- error handling
- logs
- audit
- LIVE_TRADING safety

输出：
PASS / PARTIAL / FAIL
并列出所有未通过项和对应文件，不要直接掩盖问题。
```

