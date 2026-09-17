# GOLD-AI · Phase 1 Gold Intelligence Database

黄金智能交易研究与策略进化系统（研究型 / 回测型 / 模拟盘型）。
当前阶段：**Phase 1（Gold Intelligence Database）第 1 步：数据库地基**。

> 开发规则以 `.clinerules` 与 `docs/01~08` 为准；本文件只说明「如何运行」与「本步已落地 / 未落地」。

## 1. 本步已完成

| 项 | 落地文件 |
|---|---|
| 工程最小骨架、配置系统、安全门禁 | `pyproject.toml`、`.env.example`、`config/settings.py` |
| 统一 UTC 时间语义（published_at / collected_at / effective_at / event_at） | `src/common/time.py` |
| UUID v7 主键工具（时间有序、进程内单调） | `src/common/uuid7.py` |
| 内容哈希与文本规范化（幂等去重基础） | `src/common/hashing.py` |
| Phase 1 第一批 15 张表 ORM 模型 | `database/models/*.py` |
| Alembic 迁移 0001（含完整 downgrade） | `database/migrations/versions/0001_phase1_core_tables.py` |
| 事实表「不可覆盖」守卫（flush 期拦截） | `database/protection.py` |
| 时间因果 / 数据质量约束（CHECK / FK / UNIQUE） | 各模型 `__table_args__` + 同结构迁移 |
| unit / integration / data_quality / leakage 测试 | `tests/`（797 项：单元 483 / 集成 269 / 数据质量 25 / 泄漏 20；其中 23 项兼作 `regression`，当前 796 passed + 1 skipped） |
| 基础数据种子（instruments / sources，幂等 + CLI） | `database/seeds/`（`python -m database.seeds`） |
| raw_items 修正路径①仓储（先插新记录再回填指针） | `database/repositories/raw_items.py` |
| mypy 静态类型检查 + CI（含 PostgreSQL 作业） | `pyproject.toml`、`.github/workflows/ci.yml`、`scripts/check_pg_schema.py` |
| **Collector Framework**（`BaseCollector` + 注册表 + 运行编排） | `src/collectors/` |
| 传输层：3 次重试 / 指数退避 / 超时 / 429 `Retry-After`，可注入 Mock | `src/collectors/transport.py` |
| 日志系统（PyYAML 解析 `config/logging.yaml`） | `config/logging.py`、`config/logging.yaml` |
| **migration 0002**：`collector_runs.retry_count`（重试次数持久化） | `database/migrations/versions/0002_collector_run_retry_count.py` |
| **market_collector**（Yahoo chart JSON：1m K 线 / OHLCV / UTC 分钟精度） | `src/collectors/market.py` |
| 行情数据质量：条数不足与 K 线缺口 WARNING 告警 | `src/collectors/base.py`、`src/collectors/market.py` |
| **migration 0003**：`market_bars` 按来源的唯一索引（团队批复） | `database/migrations/versions/0003_market_bars_source_unique.py` |
| **news_collector**（公开 RSS/Atom + CSV 兜底；UTC 强制对齐 + 跨源去重） | `src/collectors/news.py` |
| **macro_collector**（FRED 观测 + CSV 兜底；密钥仅环境变量、URL 脱敏、保持原始粒度） | `src/collectors/macro.py` |
| **migration 0004**：`collector_runs.warnings_json`（数据质量告警持久化） | `database/migrations/versions/0004_collector_run_warnings.py` |
| 端到端 Mock 复核工具（种子 → 三源管道 → 数据量 + 约束校验 → Markdown 报告） | `scripts/phase1_pipeline_report.py`、`docs/09_Phase1_数据管道总结报告.md` |
| **Phase 1 冻结登记**：技术债与已知限制（TD-01 ~ TD-15） | `TECH_DEBT.md`、`docs/04 §43`、`docs/05`「Phase 1 冻结记录」 |
| **Phase 2 表结构（migration 0005）**：`author_opinions` / `propagation_edges` / `author_skill_snapshots` / `author_weight_snapshots` | `database/models/author_lab.py`、`database/migrations/versions/0005_phase2_author_lab_tables.py` |
| Phase 2 观点标注规范（stance/horizon/confidence/点位/信息类型 + JSON Schema + 验收指标） | `docs/10_标注规范.md` |
| Phase 2 实现层决策登记（幂等自然键 / 时间 CHECK / 哨兵 ANY / append-only） | `docs/04 §44` |
| **Phase 2 观点抽取器**（严格契约 + 可注入接口 + 词典/正则 Mock，`parser_version=mock-regex-v1`） | `src/processors/`（`schemas` / `opinion_extractor` / `regex_extractor`） |
| Phase 2 时间因果契约（`opinion.effective_at >= post.effective_at`，不新增数据库列） | `src/processors/timeline.py`、`tests/leakage/test_opinion_timeline.py` |
| Phase 2 标注抽样脚本（随机种子 + 分层 + 去重 → 待人工标注 CSV，判定列留空） | `scripts/sample_annotation_set.py` |
| **Phase 2 多模型标注对比工具**（豆包/千问/文心的一致性统计 → 待人工裁决清单 `logs/pending_review.csv` + 共识 `logs/model_consensus.csv`） | `scripts/compare_model_annotations.py`、`docs/experiments/annotation_model_comparison.md` |
| **Phase 2 金标准生成工具**（人工裁决 + 三模型共识 → `logs/ground_truth_200.csv`，含 `source` / `overturn` 溯源） | `scripts/build_ground_truth.py`、`docs/experiments/ground_truth_report.md` |
| **Phase 2 基线评估工具**（正则抽取器 vs 人工金标准：区分该判未判 / 提取错误 / 不该判却判，出混淆矩阵） | `scripts/evaluate_extractor.py`、`logs/extractor_eval.csv`、`docs/experiments/Phase2_基线评估报告.md` |
| **Phase 2 作者库 CRUD + 审计留痕**（作者 / 平台账号：查询、幂等写入、状态变更、软删除；每次管理性修改写 `audit_logs`） | `database/repositories/authors.py`、`database/repositories/audit.py` |
| **Phase 2 作者库种子与 CSV 导入**（内置 3 条 Mock 作者，全部挂 NEWS 来源、**不碰微博**；`--scope authors` / `--authors-csv`） | `database/seeds/authors.py`、`database/seeds/__main__.py` |
| **Phase 2 Post → Opinion 管道骨架**（`author_posts` → `processed_items` → `author_opinions`；幂等、失败隔离、未知标的跳过、纯 Mock、不接真实 LLM） | `src/processors/opinion_pipeline.py` |
| **Phase 2 传播去重（1+99 构造）**：零依赖 TF-IDF 余弦相似度 + 传播边落库 + 独立观点数（100 条同文 → **1** 个独立观点） | `src/processors/similarity.py`、`src/processors/propagation.py` |
| **Phase 2 Mock 演练数据生成器**（250 条结构化合成博文：`id`/`source`/`content` + `effective_at=max(published_at,collected_at)` + 126 条对抗样本 + 5 条同文转载 + 4 个已注册 NEWS 来源；每行 `is_mock=true`） | `scripts/generate_mock_posts.py` |
| 抽样**输入缺失自动补数据** + 输入**来源体检**（`input_is_mock` 写入元数据）+ 控制台醒目警告 | `scripts/sample_annotation_set.py`、`logs/annotation_sample.meta.json` |
| CLI 脚本**双通道**（`python scripts/x.py` 与 `python -m scripts.x` 都可运行）+ **真实进程**冒烟测试 + CSV 编码 `utf-8-sig`（Excel 中文不乱码） | `scripts/__init__.py`、`tests/integration/test_cli_scripts.py` |

**未落地（后续步骤）**：首期 50~100 位真实作者名单导入（框架已就绪，等业务方名单：TD-16）、
真实 LLM 抽取器（当前仅 Mock 实现，配置 `DEEPSEEK_API_KEY` 后接入）、微博采集器（TD-06，合规前提）、
`raw_items` → `author_posts` 归属链路与观点管道 CLI / Scheduler（TD-20）、
作者技能与权重快照计算、标注比对脚本（等周一人工标注完成）。
Phase 2 的四张表已在 migration 0005 建好；Phase 3 及以后的表
（feature_snapshots、alpha_signals、strategies…）**故意不建**，并由测试强制校验。

Phase 3 W0-4 已接通 19 条已验收真实快讯的可追溯研究链（命令均默认 dry-run）：

```powershell
python scripts/import_real_posts.py --no-dry-run
python scripts/run_opinion_pipeline.py --no-dry-run
python scripts/build_opinion_labels.py --no-dry-run
```

标签严格使用晚于观点 `effective_at` 的下一根同 horizon K 线；未声明周期时只在 1h/4h/1d 评价网格分别计算，并显式标记为 `evaluation_grid`，不冒充作者声明。

**标注演练数据（已就绪）**：`logs/posts.csv`（250 条**合成**博文，`is_mock=true`）→
`logs/annotation_sample.csv`（200 条待人工标注，判定列全空）。合成数据只用于跑通流程与校准
标注标准，**任何准确率结论必须用真实数据复跑**（`docs/10 §2.2`）。

## 2. 环境准备

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env      # 按本地环境修改 DATABASE_URL
```

实盘安全门禁（MVP 阶段禁止修改为 true，否则配置层直接抛错、系统无法启动）：

```
LIVE_TRADING=false
ALLOW_EXTERNAL_ORDER_SUBMISSION=false
```

## 3. 数据库与迁移

生产 / 研究环境唯一目标数据库是 **PostgreSQL**；SQLite 仅用于本地开发与测试
（同一份 ORM 元数据 + 同一份迁移，`JSONB`/`UUID`/`TIMESTAMPTZ` 在 PG 上为原生类型）。

```powershell
# PostgreSQL（需先创建库）
.\.venv\Scripts\python.exe -m alembic upgrade head

# 本地 SQLite 快速验证（含回滚）
.\.venv\Scripts\python.exe -m alembic -x db_url="sqlite+pysqlite:///./.tmp.db" upgrade head
.\.venv\Scripts\python.exe -m alembic -x db_url="sqlite+pysqlite:///./.tmp.db" downgrade base
```

规则（`docs/03` §19）：所有结构修改必须通过 Alembic；禁止手改数据库、
禁止删除或修改已发布的 migration；迁移必须可回滚或明确说明不可回滚。

## 4. 测试与检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q              # 全量
.\.venv\Scripts\python.exe -m pytest -m unit -q      # 单元
.\.venv\Scripts\python.exe -m pytest -m leakage -q   # 时间因果（防未来数据泄漏）
.\.venv\Scripts\python.exe -m ruff check .
```

`tests/integration/test_migration_matches_metadata.py` 是防漂移关键测试：
它逐表比对 **Alembic 迁移结果**与 **ORM 元数据**（列/类型/可空、主键、唯一约束、
CHECK 名称与表达式、外键、索引），任何不一致都会让构建失败。

## 5. 基础数据种子（instruments / sources）

```powershell
python -m alembic upgrade head                    # 前置：先完成结构迁移
python -m database.seeds                          # all：instruments + sources
python -m database.seeds --scope instruments      # 只写标的白名单
python -m database.seeds --dry-run                # 演练：执行后回滚，不落库
```

- **instruments（11 个标的）**：XAUUSD / XAGUSD / COMEX_GC / SGE_AU9999 / DXY /
  US10Y / US02Y / US10Y_REAL / USDCNY / WTI / BTCUSD（覆盖 05 §5 首期必备与可追加项）。
- **sources（8 个来源）**：微博、金十、新浪财经、Investing（NEWS）、FRED、财经日历（MACRO）、
  Yahoo 行情（MARKET）、Stooq 备用行情（默认 `enabled=false`）。
- **幂等**：以 `instruments.symbol` / `sources.name` 为自然键，`insert-if-absent`；
  已存在记录**一律不修改**（不覆盖运维配置）。重复执行只报 `已存在 N 条`。
- `sources.config_json` 中的采集器名将在第 2 步 Collector Framework 中实现；
  在此之前该字段只描述计划配置，不会产生任何真实网络请求。

## 7. Collector Framework（第 2 步：采集框架）

| 文件 | 职责 |
|---|---|
| `src/collectors/types.py` | 数据契约：`RawItemPayload` / `MediaPayload` / `FetchPage` / `CollectOutcome`（字段对齐 04） |
| `src/collectors/transport.py` | HTTP 传输层：3 次重试 + 指数退避 + 超时 + `Retry-After`；`Transport` 协议可注入 Mock |
| `src/collectors/base.py` | `BaseCollector`：`collect()` 只做**站点无关**编排；`_do_fetch()` 留给具体采集器实现 |
| `src/collectors/registry.py` | 注册表：`sources.config_json["collector"]` → 采集器类 |
| `src/collectors/runner.py` | 运行编排：游标续采、`collector_runs` 落库、**单源失败隔离** |

编写一个新采集器（Step 3 的做法；`base.py` 中没有任何站点 URL）：

```python
@register_collector
class WeiboCollector(BaseCollector):
    collector_name = "weibo_collector"     # 与 sources.config_json["collector"] 一致
    source_type = SourceType.WEIBO

    async def _do_fetch(self, cursor, window) -> FetchPage:
        response = await self._request(HttpRequest(url=..., params=...))  # 自动 3 次重试
        ...
        return FetchPage(payloads=payloads, next_cursor=new_cursor)
```

`collect()` 已内置（子类无需重复实现）：

- **幂等**：同数据源内 `source_record_id` 或 `content_hash` 相同 → 判重，只落库一次；
- **时间语义**：`effective_at = max(published_at, collected_at)`，naive 时间直接拒绝；
- **断点续采**：`next_cursor` 落库到 `collector_runs.cursor_json`，下一轮从断点继续；
- **统计**：fetched / inserted / duplicate / failed / pages / retries；
- **状态**：`SUCCESS` / `PARTIAL_FAILED`（部分推进后失败，游标保留）/ `FAILED`；
- **每轮最少记录数**：`sources.config_json["min_records_per_run"]`（0 = 不检查）。低于阈值时
  打 WARNING 日志并写入 `collector_runs.warnings_json`（migration 0004），
  **非 NULL 即表示该轮为 WARNING 级运行**（`status` 语义不变），Dashboard / 数据质量巡检可直接查询。

**测试策略（强制）**：

- 采集器测试**只使用 Mock 传输层**（`tests/conftest.py::MockTransport`）；conftest 里还有
  autouse 守卫，任何非本机 socket 连接会直接失败 → 测试永远不会去爬微博/新闻站；
- 已覆盖：网络超时、429 限流、500 服务端错误（3 次重试 + 退避断言）、幂等（含
  `content_hash` 判重）、断点续采、单源失败隔离。

现状：注册表已包含 3 个采集器（`market_collector` / `news_collector` / `macro_collector`）；
`sources.config_json` 中的 `econ_calendar_collector`（经济日历，Step 3 后续）与 `weibo_collector`
（Phase 2）**尚未实现**，前者来源已置 `enabled=false`，运行时不会被调度（禁止伪完成）。

#### 每个来源的阈值与告警（团队批复）

`sources.config_json` 必须声明 `min_records_per_run`（0 = 不检查），当前取值与理由：

| 来源 | 阈值 | 理由 |
|---|---|---|
| `market_yahoo` | 0 | 行情条数由"窗口 ÷ 周期"自动推断（`MarketCollector` 覆盖 `_expected_min_records`） |
| `jin10_flash` | 5 | 快讯为高频源，30 分钟少于 5 条通常意味着 feed 异常 |
| `sina_finance_gold` / `investing_news` | 1 | 至少应有 1 条，否则视为源不可用 |
| `fed_press_releases` | 0 | 官方源每周仅数次，窗口内"安静"是正常状态，避免噪声告警（可用性由 health_check 兜底） |
| `fred_macro` | 1 | 有回看窗口，正常每轮都能复采到观测；取不到即 API 异常 |
| `weibo_main` | 1 | 采集器属 Phase 2（当前不会运行） |
| 默认关闭的来源 | 0 | `market_stooq_backup`（备用）、`econ_calendar_investing`（待实现） |

低于阈值时的行为：**WARNING 日志 + `collector_runs.warnings_json` 落库**（测试见
`tests/integration/test_collector_runner.py::test_low_record_warning_is_persisted_in_collector_runs`）。

### 已实现采集器：`market_collector`（Step 3 第 1 个）

| 项 | 说明 |
|---|---|
| 数据来源 | Yahoo Finance chart JSON（`/v8/finance/chart/{symbol}`，即 yfinance 内部调用的接口）；主机名取自 `sources.base_url`，代码不硬编码站点 |
| 落库目标 | `market_bars`（结构化）+ `raw_items(item_type=QUOTE)`（逐根原始 JSON 切片，保证可追溯） |
| 字段 | `open_time / close_time`（UTC，分钟精度，`close_time = open + 周期`）、`open / high / low / close / volume`（`NUMERIC(20,8)`，Decimal） |
| 时间语义 | `published_at = close_time`，`effective_at = max(published_at, collected_at)`（回补历史也不会泄漏未来信息） |
| 采集计划 | `config_json["symbols"] × ["timeframes"]` 笛卡尔积，每个组合一"页"，游标 `plan_index` 支持断点续采 |
| 周期支持 | 1m / 5m / 15m / 30m / 1h / 1d；**4h 需客户端聚合**，会被跳过并打印告警 |
| 数据质量 | 单轮条数 < 期望（窗口 ÷ 周期，留 1 根容差）→ WARNING；相邻 K 线缺口 → WARNING（含缺失根数与示例时间）；空值/非法 bar 跳过并计数 |
| 失败隔离 | 标的 404 → 跳过该组合并告警；单条落库失败 → 计数并继续（PARTIAL_FAILED，不中断整轮） |
| 测试 | 单元 27 项（解析/精度/缺口/配置）+ 集成 15 项（正常落库、`unittest.mock` 模拟 HTTP、429 限流、超时重试、幂等、404、断点续采、告警） |

重试次数的持久化：`collector_runs.retry_count`（migration 0002）记录本轮实际重试次数，
成功与失败路径都会写入，便于数据质量分析（例如"限流是否在恶化"）。

### 已实现采集器：`news_collector`（Step 3 第 2 个）

| 项 | 说明 |
|---|---|
| 数据源 | **公开 RSS/Atom**（RSS 2.0 / RSS 1.0(RDF) / Atom），URL 由 `sources.config_json["feeds"]` 配置，代码不写死站点；**（团队批复允许的）离线兜底**：`config_json["csv_path"]` 指向本地 CSV 时走 CSV 导入 |
| 合规 | 只读公开 feed；**严禁抓取微博或未授权站点**（微博文本分析属 Phase 2）；拒绝 DOCTYPE（防 XXE / 实体膨胀）、限制响应大小（5MB） |
| 时间对齐 | 强制 UTC：RFC822（`GMT` / `-0400`）与 ISO8601（`Z` / `+08:00`）全部换算；**时区不明确（naive）→ WARNING + `published_at` 置空 + `effective_at = collected_at`**，原始字符串存 `raw_json.published_at_raw`，且不生成 `news_events`（绝不猜测/冒充） |
| 幂等 | **全局 content_hash 去重**：同一篇新闻被多个 RSS 源转载时只落库一次（基类去重是"本来源内"的，新闻在其之前再做跨来源检查） |
| 落库 | 严格按 04 §14 写 `news_events`（headline / summary / event_type=feed 分类 / published_at / effective_at / parser_version）；同时逐条保留 `raw_items(item_type=NEWS)` 原始切片 |
| 断点续采 | 每个 feed 一"页"，游标 `target_index`；单个 feed 失败不影响其他 feed，下一轮从断点继续 |
| 测试 | 单元 35 项（RSS/Atom 解析、UTC 转换、歧义标记、XML 异常/DOCTYPE/超大响应、CSV、配置与游标）+ 集成 18 项（正常落库与 `news_events` 字段、跨源去重、幂等、超时/429/500 重试、XML 异常、歧义时区、窗口过滤、多 feed 续采、CSV 模式、条数阈值告警） |

Phase 3 W0-3 的白名单 RSS 回填使用 `rss_collector`。CLI 默认只预览；正式落库必须显式开启，且可用只读缓存完成零网络复跑：

```powershell
python scripts/collect_rss.py --dry-run --cache-mode readonly --source fred_blog --source fed_press
python scripts/collect_rss.py --to-db --no-dry-run --cache-mode readonly `
  --source fred_blog --source fed_press --lookback-days 90
```

落库路径同时写 `raw_items`、有明确发布时间的 `news_events` 与 `collector_runs`；无可靠发布时间的条目只保留原始层。重复运行按内容哈希幂等，并在任一来源占比超过 40% 时输出集中度告警。

> 顺带修复的真实缺陷（有回归测试）：Alembic `env.py` 的 `logging.config.fileConfig()` 默认
> `disable_existing_loggers=True`，会把 `gold_ai.*` logger 全部禁用 —— 凡在已配置日志的进程里
> 跑过迁移，后续采集日志就静默消失。现已显式传 `disable_existing_loggers=False`。
>
> 收尾轮修复的第二个真实缺陷（有回归测试）：`alembic.ini` 的
> `[formatter_generic] format` 误用了 `%%` 转义。`logging.config.fileConfig()` 以
> `raw=True` 读取 `format` / `datefmt`（**不走 configparser 插值**），因此 `%%` 会被原样交给
> `logging.Formatter`，结果是**每条迁移日志只打印格式串本身**，`alembic.runtime.migration` 的
> 「Running upgrade 0001 -> 0002 …」等内容全部丢失（`alembic upgrade head` 表面上"没输出"）。
> 现已改为单个 `%`；`[alembic]` 段仍必须写 `%%`（Alembic 是带插值读的）。
> 回归测试：`tests/integration/test_migration_matches_metadata.py::test_alembic_console_formatter_renders_log_records`。
>
> Phase 2 轮修复的第三个真实缺陷（有回归测试）：`tests/unit/test_uuid7.py::test_timestamp_roundtrip`
> 是一颗**时间炸弹**——它用硬编码时刻 `2026-09-12 08:30:15.123 UTC` 断言 roundtrip，而同文件
> 前一个测试已按"当前时间"颁发过 2000 个 UUID；由于 `uuid7()` 为严格单调（请求时间戳
> `<= 已颁发最大时间戳` 时**钳位**，见 `src/common/uuid7.py`），该断言实际依赖"测试在
> 该毫秒之前运行"：2026-09-12 08:30:15 UTC 之后必然失败。现改为显式重置进程内单调状态，
> 并**新增一条测试锁定"过去时间被钳位、主键不得回退"的有意行为**
> （`test_past_timestamp_is_clamped_to_keep_keys_monotonic`），同时在 `uuid7` 模块文档中记录该取舍。

### 已实现采集器：`macro_collector`（Step 3 第 3 个）

| 项 | 说明 |
|---|---|
| 数据源 | **FRED（美联储经济数据）官方公开 API** `/fred/series/observations`，series 列表由 `sources.config_json["series"]` 配置（支持 `"CPIAUCSL"` 或 `{"series_id","country","unit"}`）；**离线兜底**：`config_json["csv_path"]` 指向本地 CSV（无 Key 环境/存档数据）。**严禁爬取收费站点** |
| 密钥安全 | API Key 只从**环境变量**读取（`config_json["api_key_env"]`，默认 `FRED_API_KEY`）：**绝不硬编码 / 写库**；`source_url` 与 `raw_json` 中一律脱敏为 `api_key=***`（有专项测试） |
| 时间对齐（防泄漏） | `event_at` = 观测所属期（不是发布时间）；历史回填使用 ALFRED `output_type=4`（Initial Release Only）。API 只有日期精度，故 `released_at` 保守取 `realtime_start` 次日 00:00 UTC；查询边界不冒充修订失效日。`effective_at >= max(released_at, collected_at)`，缺 release 硬失败 |
| 落库 | 严格按 04 §15 写 `macro_events`；同一观测期的不同修订按 `(source,event_code,country,event_at,released_at)` 追加保存，表为 append-only；`forecast_value` 与 `previous_value` 保持 NULL（不凭空推断）；逐条保留 `raw_items(item_type=MACRO)` |
| 采集计划 | 每个 series 一"页"，游标 `series_index` 支持断点续采；单个 series 报错 → 本轮 `PARTIAL_FAILED` 并保留游标，下一轮从断点继续 |
| 回看窗口 | `lookback_days`（默认 30，种子配置 45）：30 分钟调度窗口内通常没有新观测，故每轮**复采近期观测**（幂等去重），同时让 `min_records_per_run=1` 真正能发现 API 故障 |
| 数据质量 | 缺失值（FRED 的 `.`）、非法观测期、非数值、重复 vintage、未来观测期 → 跳过并告警；缺失/非法 `realtime_start` 或 CSV `released_at` → **硬失败** |
| 测试 | 单元 + 集成 + leakage：解析与密钥脱敏、release 前不可见、修订边界切换、非法时间窗拒绝、append-only、迁移与 ORM 漂移均有覆盖；测试全程禁止外部网络 |
| 历史回填 | `scripts/backfill_macro_vintages.py` 默认 dry-run；年度分块规避 2000 vintage 上限。当前 8 序列 11,680 条，幂等复跑 0 新增。完整修订链未交付，不能把当前初值数据描述为“全 vintage” |

### 端到端管道复核（`tests/integration/test_pipeline_integrity.py`）

一个文件把整条链路串起来验证（全部 Mock，零外部网络）：种子 → market / news / macro 三源
→ `raw_items`（QUOTE / NEWS / MACRO）→ `market_bars` / `news_events` / `macro_events`
→ `collector_runs`，并断言：

1. 三层落库条数一致、结构化行可回溯到原始层（`news_events.raw_item_id`、宏观 `series_id`）；
2. **全表时间因果不变式**：`open_time/event_at <= effective_at`、`collected_at <= effective_at`、
   `effective_at <= 本轮结束时间`（防未来数据泄漏）；
3. 重跑幂等（零新增、行数不变）；
4. **单源失败隔离**：新闻源 500 → 仅新闻 `FAILED`（`retry_count=3`），行情与宏观照常落库。

## 8. 数据模型

### 8.1 Phase 1（15 张表，migration 0001 ~ 0004）

| 表 | 作用 |
|---|---|
| `sources` / `authors` / `author_accounts` | 信息源与作者身份（首期 50~100 作者） |
| `raw_items` / `raw_media` | 原始数据与媒体（append-only，永不覆盖） |
| `collector_runs` | 采集运行统计、断点（cursor）、失败与重试、数据质量告警（`warnings_json`） |
| `processed_items` | 加工结果（`processor_name + processor_version` 幂等） |
| `author_posts` | 原始记录 → 作者归属 |
| `instruments` / `market_bars` | 标的白名单（XAUUSD 等）与 K 线（OHLC 约束） |
| `news_events` / `macro_events` | 新闻与宏观事件（CPI / PCE / NFP / FOMC…） |
| `job_runs` / `data_versions` / `audit_logs` | 调度幂等、数据集版本、审计留痕 |

### 8.2 Phase 2（4 张表，migration 0005）

| 表 | 作用 | 幂等自然键 |
|---|---|---|
| `author_opinions` | 作者观点（stance / horizon / confidence / 点位 / 信息类型 / parser_version） | 无（由 `processed_items` 唯一键 + 提取器前置检查保证） |
| `propagation_edges` | 信息传播关系（REPOST / QUOTE / SEMANTIC_SIMILAR / SAME_SOURCE） | `(from_item_id, to_item_id, relation_type, model_version)` |
| `author_skill_snapshots` | 作者技能快照（方向 / 时机 / 入场 / 退出 / 独立性 / 校准 / 样本量） | `(author_id, as_of)` |
| `author_weight_snapshots` | 动态权重快照 `Weight(author \| regime, horizon, information_type)` | `(author_id, regime_type, horizon, information_type, as_of)` |

四张表均 append-only（不可覆盖 / 不可删除），并对时间因果设数据库 CHECK
（`created_at >= effective_at / detected_at / as_of`）；实现层决策详见 `docs/04 §44`。

## 9. 关键设计决策（含理由）

1. **`effective_at` 是防泄漏的核心字段**：`effective_at = max(published_at, collected_at)`，
   缺失 `published_at` 时以 `collected_at` 兜底（不得用"现在"，否则历史批次重放会漂移）。
   数据库 CHECK 强制 `effective_at >= collected_at` 且 `>= published_at`。
2. **事实表 append-only 双保险**：`raw_items` / `raw_media` / `processed_items` 在 ORM flush 期
   被守卫拒绝任何覆盖写与物理删除（唯一例外 `raw_items.superseded_by_id`）。
   修正路径已实现为「**先插入新记录（新 `source_record_id`）→ 再回填旧记录指针**」
   （`database/repositories/raw_items.py::supersede_raw_item`，幂等且禁止改写取代链）。
3. **枚举用 `VARCHAR(n) + CHECK`**（非 PostgreSQL native enum）：研究系统会持续新增取值
   （Regime、alpha_type、状态机），native enum 的 `ALTER TYPE` 在历史迁移上维护成本过高。
4. **幂等键即自然键**：`(source_id, source_record_id)`、`(instrument_id, timeframe, open_time)`、
   `job_runs.idempotency_key` 等唯一约束保证重复采集 / 重复调度不产生重复数据。
5. **迁移手写 DDL**：不在 SQLite 上 autogenerate（会把类型渲染成 SQLite 变体），
   枚举取值冻结在 revision 内，保证历史迁移可重放、结构不漂移。
6. **时间列全部 `TIMESTAMPTZ`**：由测试基于 ORM 元数据强制校验，禁止 naive datetime。
7. **Phase 2 的 NULL 与幂等**：分值 / 价格 / 置信度可空（NULL = 未计算，禁止用 0 冒充）；
   `author_weight_snapshots` 的维度列缺省用哨兵 `'ANY'`，因为 SQL 唯一约束对 NULL 不去重，
   用 NULL 会让"同一作者同一时点同一上下文"重复落库（`docs/04 §44` 决策 3）。
8. **跨表时间因果的口径统一**：`processed_items.effective_at >= raw_items.effective_at` 与
   `author_opinions.effective_at >= author_posts.effective_at` 都不由数据库 CHECK 强制
   （单表无法表达），而是"提取器层保证 + `leakage` 标记测试强制"，与 Phase 1 完全一致。

## 10. 下一步

> Phase 1 数据管道已冻结（V0.3）；**Phase 2（Author Lab）已开工**：
> 表结构（migration 0005）与标注规范（`docs/10`）已交付。
> 完整技术债与验收标准见 `TECH_DEBT.md`；Phase 1 复核报告见 `docs/09`。

**Phase 2 第一步（已完成）**

1. ✅ 前置裁决：不采集微博（用机构/分析师 RSS 账号或本地 CSV 导入作为作者数据源）；
   LLM 接口隔离、**Mock 优先**（不接真实付费 API）；标注先定规范再抽样。
2. ✅ 表结构：migration 0005 建 4 张表 + 严格时间约束（`docs/04 §44`）。
3. ✅ 标注规范：`docs/10_标注规范.md` **已批准 v1.0**（含 JSON Schema 与验收指标）。
4. ✅ **OpinionExtractor 接口 + Mock 实现 + 测试**：`src/processors/`
   （Pydantic 严格契约 + 可注入协议 + 词典/正则抽取器，100% Mock 测试）。
5. ✅ 抽样脚本 `scripts/sample_annotation_set.py`（随机种子 + 分层 + 去重 + 导出 CSV，
   判定列一律留空）。

**Phase 2 第二步（建议顺序）**

1. ⏭ **人工完成 200 条标注**（`python -m scripts.sample_annotation_set --limit 200`），
   再交付比对脚本（混淆矩阵 / Cohen's Kappa / ECE）与准确率报告。
2. ⏭ 作者库 CRUD 与首批作者导入（TD-16，`python -m database.seeds --scope authors`）。
3. ⏭ 「Post → Opinion」管道：`processed_items(processor_version)` + `author_opinions` 落库
   （复用 `src/processors/` 与 `timeline` 契约），并补集成与 leakage 测试。
4. ⏭ 传播去重（`propagation_edges` + 相似度基线，`docs/08 §5` 的 1+99 构造）。

**Phase 2 第三步（进行中）：DeepSeek LLM 观点抽取器（缓存优先 + 预算门禁）**

> 目的一句话：用**同一份 200 条人工金标准**对比"正则基线 vs LLM"，量化每个字段的提升，
> 同时保证**复跑零成本**、**密钥零泄漏**、**单测零网络**。

交付物（2026-09-13）：

| 文件 | 作用 |
|---|---|
| `src/processors/llm_client.py` | 同步 DeepSeek 客户端：超时重试 ≤3、滑动窗口限速 60 次/分、429（遵守 `Retry-After`）/5xx/401 分类处理、`max_api_calls` 预算门禁、`redact()` 全链路脱敏 |
| `src/processors/prompt_opinion.py` | `opinion-prompt-v1`：中文指令 + 英文键名 + 6 条 few-shot（条件句/引用/复盘/弱化/多目标/无观点），规则逐条对应 `docs/10 §4.9` |
| `src/processors/llm_cache.py` | 文件缓存：键 = `sha256(prompt_version｜model｜text｜has_media)`（**改 Prompt 自动失效**）；模式 `auto/readonly/refresh/off`；原子写入；**失败也缓存** |
| `src/processors/llm_extractor.py` | `LLMOpinionExtractor`（实现 `OpinionExtractor` 协议）：缓存优先 → 调 API → **复用 `build_drafts()` 契约校验**；单帖失败降级为诊断，401/缺 Key 直接抛错 |
| `src/processors/opinion_extractor.py` | 新增两个诊断码：`llm_api_error` / `llm_parse_error`（只影响诊断统计，不动 schema） |
| `tests/unit/test_llm_{client,cache,extractor}.py` | 85 项单测：MockTransport 覆盖正常/超时/429/5xx/401/限速/预算/脱敏，**零网络零 token** |

密钥安全设计（**没有 `--api-key` 参数**）：

1. Key 只从 `.env` / 环境变量读（`config/settings.py::deepseek_api_key` 用 `SecretStr`，
   `repr()` 显示 `**********`）；
2. 客户端不 print 任何内容；`stats()` 只输出计数与模型名可安全打印；
3. 所有异常消息、返回内容、落盘缓存统一过 `redact()`（`sk-***`）；
4. 命令行只暴露 `--llm-model` / `--llm-cache-mode` / `--llm-max-api-calls` 这类非敏感项。

成本口径（`deepseek-chat`，system prompt ≈3.4k 字符 ≈2.2k tokens）：

| 场景 | 请求数 | 输入 tokens | 输出 tokens | 估算费用 |
|---|---|---|---|---|
| 试点 20 条（首次） | 20 | ≈4.7 万 | ≈0.5 万 | **< ¥0.1** |
| 全量 200 条（首次） | 200 | ≈47 万 | ≈5 万 | **≈ ¥0.6 ~ ¥1.3** |
| 任意复跑（`readonly`） | 0 | 0 | 0 | **¥0** |

下一步：把 `--extractor llm --limit 20` 接进 `scripts/evaluate_extractor.py` →
**先跑 20 条试点 + 人工比对** → 再跑全量 200 条并出《Phase 2 基线对比报告》。

**Phase 2 并行推进（不阻塞）**

- TD-02：PostgreSQL 上跑通采集写路径（`python -m scripts.phase1_pipeline_report --db-url ... --migrate`）；
- TD-01：本地配置 `FRED_API_KEY` 后做一次单 series 真实冒烟；
- TD-09 / TD-11：Scheduler（30 分钟框架）与独立 Processor 层（含 TD-03 的 4h 聚合）；
- TD-05 / TD-07：`econ_calendar_collector` 与宏观预期值补全；TD-10：API 与 Dashboard。

## 11. 强制约束速查（团队决定）

| 约束 | 落地方式 |
|---|---|
| 生产 / 研究环境必须 PostgreSQL | 注释标注（settings/types/session/conftest）+ CI `postgres` 作业 + `scripts/check_pg_schema.py` |
| 本地 SQLite 仅限开发与测试 | 测试库为一次性 SQLite 文件；类型语义（UUID/JSONB/TIMESTAMPTZ）由 PG 作业证明 |
| 原始数据永不被覆盖 | ORM 守卫（flush 期拒绝 UPDATE/DELETE）+ 唯一允许列 `superseded_by_id` |
| 修正走"先插新记录、再回填指针" | `database/repositories/raw_items.py::supersede_raw_item`（幂等、禁止改写取代链） |
| 配置文件必须 UTF-8 无 BOM（`.ini` 必须纯 ASCII） | `tests/unit/test_config_encoding.py` 全仓库扫描 |
| 静态类型检查 | `python -m mypy`（config / database / src / scripts）+ CI |
| **测试禁止访问外部网络** | 双层守卫（`tests/conftest.py` autouse）：① socket 层拦非本机地址（aiohttp/裸 socket）；② **httpcore 后端层拦截真实 httpx 建连**（防本机代理 127.0.0.1 绕过第一层）；Mock 传输层不受影响 |
| 实盘门禁 | `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`（配置层硬拒绝） |
