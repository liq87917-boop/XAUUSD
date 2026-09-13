# GOLD-AI Phase 1 数据管道总结报告（冻结复核）

> 本报告由 `scripts/phase1_pipeline_report.py` 自动生成；**100% Mock 数据，零外部网络**。
> 覆盖链路：种子（instruments / sources）→ 三源采集（market / news / macro）
> → `raw_items` → `market_bars` / `news_events` / `macro_events` → `collector_runs`。

## 1. 运行环境与口径

| 项 | 值 |
|---|---|
| 生成时间（UTC） | 2026-09-12 08:41:06 |
| 数据库 | `sqlite+pysqlite:///C:/Users/Admin/AppData/Local/Temp/gold_ai_phase1_report_hy6p_gye/phase1_report.db` |
| Alembic 版本 | `0005_phase2_author_lab_tables` |
| 采集窗口 | 2024-01-02T08:00:00+00:00 → 2024-01-02T08:10:00+00:00（过去时间，保证不触发未来语义） |
| 采集器注册表 | macro_collector, market_collector, news_collector |
| Python | 3.13.3 |
| SQLAlchemy | 2.0.52 |
| 未注册采集器的来源 | econ_calendar_collector（enabled=false）、weibo_collector（Phase 2） |

## 2. 种子与采集轮次

- 种子（第 1 次）：instruments: 新增 11 条，已存在 0 条；sources: 新增 9 条，已存在 0 条
- 种子（第 2 次，幂等验证）：instruments: 新增 0 条，已存在 11 条；sources: 新增 0 条，已存在 9 条
- 报告专用来源：report-market, report-news, report-macro（配置形状与生产种子一致，URL 指向 Mock 主机）

### 第 1 轮：三源采集

| 采集器 | 来源 | 状态 | fetched | inserted | duplicate | failed | 告警 |
|---|---|---|---|---|---|---|---|
| market_collector | report-market | SUCCESS | 10 | 10 | 0 | 0 | 0 |
| news_collector | report-news | SUCCESS | 2 | 2 | 0 | 0 | 0 |
| macro_collector | report-macro | SUCCESS | 2 | 2 | 0 | 0 | 0 |

Mock HTTP 请求数：**4**（全部由脚本内路由返回，无外部网络）

### 第 2 轮：幂等复跑

| 采集器 | 来源 | 状态 | fetched | inserted | duplicate | failed | 告警 |
|---|---|---|---|---|---|---|---|
| market_collector | report-market | SUCCESS | 10 | 0 | 10 | 0 | 0 |
| news_collector | report-news | SUCCESS | 2 | 0 | 2 | 0 | 0 |
| macro_collector | report-macro | SUCCESS | 2 | 0 | 2 | 0 | 0 |

Mock HTTP 请求数：**4**（全部由脚本内路由返回，无外部网络）

## 3. 落库数据量（Phase 1 十五张 + Phase 2 四张 = 19 张）

| 表 | 第 1 轮后 | 第 2 轮后（幂等复跑） | Δ |
|---|---|---|---|
| `sources` | 12 | 12 | +0 |
| `authors` | 0 | 0 | +0 |
| `author_accounts` | 0 | 0 | +0 |
| `raw_items` | 14 | 14 | +0 |
| `raw_media` | 0 | 0 | +0 |
| `collector_runs` | 3 | 6 | +3 |
| `processed_items` | 0 | 0 | +0 |
| `author_posts` | 0 | 0 | +0 |
| `instruments` | 11 | 11 | +0 |
| `market_bars` | 10 | 10 | +0 |
| `news_events` | 2 | 2 | +0 |
| `macro_events` | 2 | 2 | +0 |
| `job_runs` | 0 | 0 | +0 |
| `data_versions` | 0 | 0 | +0 |
| `audit_logs` | 0 | 0 | +0 |
| `author_opinions` | 0 | 0 | +0 |
| `propagation_edges` | 0 | 0 | +0 |
| `author_skill_snapshots` | 0 | 0 | +0 |
| `author_weight_snapshots` | 0 | 0 | +0 |

### 3.1 原始层明细（raw_items 按 item_type）

| item_type | 行数 |
|---|---|
| MACRO | 2 |
| NEWS | 2 |
| QUOTE | 10 |

### 3.2 collector_runs 明细

| # | 采集器 | 状态 | fetched | inserted | duplicate | failed | retry | 告警 |
|---|---|---|---|---|---|---|---|---|
| 1 | market_collector | SUCCESS | 10 | 10 | 0 | 0 | 0 | 0 |
| 2 | news_collector | SUCCESS | 2 | 2 | 0 | 0 | 0 | 0 |
| 3 | macro_collector | SUCCESS | 2 | 2 | 0 | 0 | 0 | 0 |
| 4 | market_collector | SUCCESS | 10 | 0 | 10 | 0 | 0 | 0 |
| 5 | news_collector | SUCCESS | 2 | 0 | 2 | 0 | 0 | 0 |
| 6 | macro_collector | SUCCESS | 2 | 0 | 2 | 0 | 0 | 0 |

## 4. 约束校验

### 4.1 表结构约束清单（由数据库反射得出）

| 表 | 列数 | NOT NULL 列 | PK | UNIQUE | FK | CHECK |
|---|---|---|---|---|---|---|
| `sources` | 11 | 8 | id | name | 0 | 1 |
| `authors` | 10 | 6 | id | canonical_name | 0 | 1 |
| `author_accounts` | 14 | 9 | id | source_id+external_account_id | 2 | 0 |
| `raw_items` | 14 | 9 | id | source_id+source_record_id | 2 | 3 |
| `raw_media` | 11 | 5 | id | raw_item_id+sha256 | 1 | 3 |
| `collector_runs` | 15 | 10 | id | — | 1 | 7 |
| `processed_items` | 11 | 7 | id | raw_item_id+processor_name+processor_version | 1 | 1 |
| `author_posts` | 10 | 8 | id | raw_item_id | 3 | 2 |
| `instruments` | 11 | 8 | id | symbol | 0 | 1 |
| `market_bars` | 14 | 12 | id | instrument_id+timeframe+open_time | 2 | 6 |
| `news_events` | 12 | 6 | id | raw_item_id+parser_version | 1 | 3 |
| `macro_events` | 12 | 8 | id | source_id+event_code+country+event_at | 1 | 2 |
| `job_runs` | 13 | 8 | id | idempotency_key | 0 | 3 |
| `data_versions` | 12 | 5 | id | dataset_name+version | 0 | 1 |
| `audit_logs` | 9 | 3 | id | — | 0 | 0 |
| `author_opinions` | 16 | 7 | id | — | 3 | 7 |
| `propagation_edges` | 9 | 7 | id | from_item_id+to_item_id+relation_type+model_version | 2 | 5 |
| `author_skill_snapshots` | 13 | 5 | id | author_id+as_of | 1 | 3 |
| `author_weight_snapshots` | 10 | 8 | id | author_id+regime_type+horizon+information_type+as_of | 1 | 3 |

### 4.2 运行期不变式（期望违规行数 = 0；时间类即防未来数据泄漏门禁）

| 不变式 | 违规行数 | 结论 |
|---|---|---|
| 行情：open_time ≤ effective_at | 0 | PASS |
| 行情：collected_at ≤ effective_at | 0 | PASS |
| 行情：close_time > open_time | 0 | PASS |
| 行情：low ≤ high | 0 | PASS |
| 行情：价格 > 0 | 0 | PASS |
| 行情：K 线不重复（instrument + timeframe + open_time） | 0 | PASS |
| 新闻：published_at ≤ effective_at | 0 | PASS |
| 宏观：event_at ≤ effective_at | 0 | PASS |
| 宏观：collected_at ≤ effective_at | 0 | PASS |
| 原始层：published_at ≤ effective_at | 0 | PASS |
| 原始层：collected_at ≤ effective_at | 0 | PASS |
| 全局：effective_at ≤ 本轮采集结束时间 | 0 | PASS |

### 4.3 行为级校验

| 校验项 | 结果 |
|---|---|
| 唯一约束拒绝重复原始记录 | PASS（IntegrityError） |
| 原始数据不可覆盖（UPDATE 被拒绝） | PASS（ImmutableRecordError） |
| 不可覆盖守卫注册表 | PASS（author_opinions, author_skill_snapshots, author_weight_snapshots, processed_items, propagation_edges, raw_items, raw_media） |

## 5. 结论

- 结论：**PASS** —— 三源采集全部 SUCCESS；原始层三类 item_type 齐全；结构化层行数与原始层一致；全部时间因果不变式零违规；重跑幂等（Δ=0）；唯一约束与不可覆盖守卫均按预期生效。

### 未覆盖范围（技术债，详见根目录 `TECH_DEBT.md`）

- **TD-01**：本报告全部为 Mock 数据，未与 FRED / Yahoo / RSS 真实接口联调；
- **TD-02**：默认在临时 SQLite 上运行（PG 验证需显式 `--db-url` + `--migrate`）；
- **TD-03**：`market_bars` 无 `timeframe='4h'` 行（provider 无该粒度，待 Processor 层聚合）；
- **TD-04**：时区不明确的新闻不生成 `news_events`（本报告样本时区明确：GMT / +0800）；
- **TD-05 / TD-07**：`macro_events.forecast_value` / `previous_value` 恒为 NULL；
- **TD-09 / TD-10 / TD-11**：Scheduler、Dashboard / 只读 API、独立 Processor 层未实现；
- **Phase 2（TD-16）**：`author_opinions` 等四张表已由 migration 0005 建立，但作者库 CRUD / 导入与观点提取器尚未交付，故本报告中它们的行数为 0。
