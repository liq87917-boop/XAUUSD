# TECH_DEBT.md — GOLD-AI Phase 1 技术债与已知限制登记册

> **状态**：Phase 1（Gold Intelligence Database）数据管道已于 V0.3 **冻结**。
> 本文件是冻结后**唯一**的技术债登记处；`docs/04_数据表结构及字段定义.md` §43 与
> `docs/05_分阶段开发路线图.md`「Phase 1 冻结记录」均指向本文件。
>
> **冻结口径（团队裁决）**：
> 1. 测试 100% Mock，**不联真实外部 API**（`tests/conftest.py` 有 autouse socket 守卫强制此约束）；
> 2. `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 不可修改（最高级别红线）；
> 3. 允许的遗留项记为技术债，**禁止伪完成**（不用 TODO 占位、不用假数据冒充真实接口）。

---

## 1. 冻结时点的交付基线

| 项 | 数量 / 状态 |
|---|---|
| Alembic 迁移 | 0001_phase1_schema → 0007_phase3_feature_tables（单一 head） |
| Phase 1 表 | 15 张（`database/models/__init__.py::PHASE1_TABLES`），全部由迁移创建 |
| Phase 2 / W0-5 表 | Phase 2 4 张 + W0-5 4 张（`ALL_TABLES` = 23 张） |
| 采集器 | 3 个已注册：`market_collector` / `news_collector` / `macro_collector` |
| 种子 | `instruments` 11 条（含 XAUUSD/DXY/US10Y/USDCNY）+ `sources` 9 条（其中 `econ_calendar_investing` / `market_stooq_backup` 为 `enabled=false`）+ `authors` 3 条（Phase 2 内置 Mock，含 3 个平台账号；全部挂 NEWS 来源，**不碰微博**） |
| Phase 2 基建 | 作者库仓储 + 审计留痕、作者 CSV 导入 CLI（`--scope authors` / `--authors-csv`）、Post → Opinion 管道骨架（纯 Mock）、传播去重（1+99 → 独立观点数 1） |
| Phase 2 演练数据 | `scripts/generate_mock_posts.py`：250 条合成博文（24 列、`id`/`source`/`content` 别名列、126 条对抗样本 / 8 类、5 条同文转载、时间自洽）；抽样输入缺失时自动补数据 + 来源体检（`input_is_mock` 入元数据） |
| Phase 2 多模型标注对比 | `scripts/compare_model_annotations.py`（**标准库读 xlsx**，零新增依赖）：三模型一致性统计 + 待人工裁决清单 `logs/pending_review.csv` + 共识 `logs/model_consensus.csv` + 报告 `docs/experiments/annotation_model_comparison.md`；**模型输出不作为金标准**；已填写的裁决表默认拒绝覆盖（退出码 4） |
| Phase 2 金标准（ground truth） | `scripts/build_ground_truth.py`：人工裁决 + 三模型共识 → `logs/ground_truth_200.csv`（1000 格，`source` 区分 `human-adjudicated` / `3-model-consensus`，含 `overturn` 推翻标记）+《金标准生成报告》`docs/experiments/ground_truth_report.md`；人工裁决文件先字节级归档到 `logs/archive/` |
| Phase 2 基线评估 | `scripts/evaluate_extractor.py`：正则抽取器 vs 人工金标准，**区分该判未判 / 提取错误 / 不该判却判**，输出准确率、召回率、精确率、混淆矩阵、点位差值分布 → `logs/extractor_eval.csv`（1000 格）+《Phase 2 基线评估报告》`docs/experiments/Phase2_基线评估报告.md` |
| 测试 | PostgreSQL 研究库环境最终全量：2649 passed / 1 skipped（2026-09-18） |
| 质量门禁 | `ruff`（E/F/I/UP/B/SIM）、`mypy`（config + database + src + scripts）、CI（Python 3.12/3.13 + PostgreSQL 16 作业） |
| 端到端复核 | `tests/integration/test_pipeline_integrity.py` + `scripts/phase1_pipeline_report.py` |

---

## 2. 未完成事项（技术债登记表）

优先级定义：**P0** = 进入下一阶段前必须解决；**P1** = 影响研究结论可信度，需在对应阶段解决；
**P2** = 工程便利性/可维护性，可择期处理。

| 编号 | 优先级 | 事项 | 计划阶段 |
|---|---|---|---|
| TD-01 | P1（部分解除） | FRED / Yahoo / RSS 已完成真实或合规缓存链路验证；行情备用源、完整新闻历史与持续在线冒烟仍未交付 | 上线前数据源收尾 |
| TD-02 | ✅ 已解除（2026-09-17） | 本地 PostgreSQL 16.15 已完成 `0001→0006` 迁移、原生类型/种子检查、三源采集写入及二轮幂等验证；不变式违规 0 | 已修 |
| TD-03 | ✅ 已解除（2026-09-17） | `4h` 已由 1h 满桶聚合并通过幂等测试；每个标的的完整 4h 快照已写不可覆盖 `data_versions`，记录 processor 版本、范围、行数与 SHA-256 | 已修 |
| TD-04 | P1 | 新闻时区不明确（naive）的行不生成 `news_events` | Phase 2 决策 |
| TD-05 | P1 | `econ_calendar_collector` 未实现（CPI/PCE/NFP/FOMC 日历与预期值） | Phase 1 收尾轮 |
| TD-06 | P1 | `weibo_collector` 未实现（Phase 2 主体） | Phase 2 |
| TD-07 | P2 | `macro_events.forecast_value` / `previous_value` 恒为 NULL | 随 TD-05 |
| TD-08 | P1 | 生产 RSS / 行情备用源未配置（`feeds` 为空、Stooq 关闭） | 上线前运维配置 |
| TD-09 | ✅ 已解除（2026-09-22） | ~~Scheduler（每 30 分钟任务框架）未实现~~ → `src/scheduler/`（UTC 对齐确定性 30 分钟槽 + `job_runs` 幂等 + stale RUNNING/RETRYING 接管 + 单源构造/执行故障隔离）+ `scripts/run_collector_scheduler.py`（`--once` / 常驻循环，Ctrl+C 正常退出）；复用现有 `job_runs`，无新 migration/schema；证据见 `PROGRESS_LOG.md` 第六十五轮（新增 60 项 Mock 测试，全量 pytest / ruff / mypy 通过） | 已修 |
| TD-10 | P1 | Dashboard / API 未实现（Phase 1 交付物中的监控与查询页面） | Phase 1 收尾轮 / Phase 2 |
| TD-11 | ✅ 已解除（2026-09-22） | ~~Processor 层未独立建立（normalize/dedup/timezone 目前内嵌在采集器内）~~ → `src/processors/collection/`（`contracts` / `normalize` / `identity` / `validate` / `pipeline`）：确定性四阶段流水线（normalize → timezone/effective_at → identity/dedup → validation/audit）+ 幂等写 `processed_items`（append-only，`(raw_item_id, processor_name, processor_version)` 唯一 + 存在性检查）+ 白名单审计摘要 + 凭据擦除（`src/common/redaction.py`）；采集侧接线点 `BaseCollector(post_processor=...)`（默认 None，零行为变化），参考实现 RSS `collect_rss.py --to-db`；无新 migration/schema；证据见 `PROGRESS_LOG.md` 第六十六轮（新增 73 项 Mock 测试：交付 72 + R1 复核回归 1；全量 pytest / ruff / mypy 通过） | 已修 |
| TD-12 | P2（部分解除，2026-09-22 收紧） | `job_runs` 已由 30 分钟 Scheduler 写入（TD-09）；`processed_items` 已有两条写入路径（`src/processors/collection/pipeline.py` 采集后处理 + `src/processors/opinion_pipeline.py`）；`audit_logs` 已由作者库仓储写入（`database/repositories/audit.py`）；`data_versions` 已由 `src/features/market.py` / `src/alpha/regime.py` 写入。**剩余**：`processed_items` 无 `error_message` 列（失败详情在 `structured_json`，见 TD-19）、非行情数据集的 `data_versions` 快照仍随各阶段补齐 | 随需求 |
| TD-13 | P2 | `raw_media` 无下载器（图片二进制未落地，`storage_uri` 写入路径未验证） | Phase 2（微博图片） |
| TD-14 | P2 | 依赖仅声明下界，无 lock 文件（可复现构建依赖 pip 解析） | 择期 |
| TD-15 | ✅ 已解除 | 仓库已初始化 Git；2026-09-17 已在 W0-5 前创建本地数据库快照，代码检查点待本轮全量门禁通过后建立 | 本轮收尾 |
| TD-16 | ✅ 已解除（框架） | ~~**作者库 CRUD 与首期 50~100 作者导入未实现**~~ → 仓储 + 审计 + CSV 导入 CLI 已交付；**剩余**：首期 50~100 位**真实**作者名单（需业务方提供，非代码问题） | Phase 2（名单等到即导入） |
| TD-17 | P2 | 传播去重为**桶内两两比较（O(n²)）** + 连通分量"链式合并"；缺"链式证据 / 星型拓扑"列（schema 已冻结） | Phase 3（大语料需 SimHash / 向量检索） |
| TD-18 | P2 | 分词升级路径未启用：默认 `cjk-ngram-v1`，jieba 切分需显式开关（切换 = `model_version` 变更，必须整体重算相似度） | Phase 3（中文分词与实体识别） |
| TD-19 | P2 | `processed_items` 无 `error_message` 列 → 抽取失败详情写在 `structured_json`，SQL 级失败巡检需新迁移 | 有明确巡检需求时 |
| TD-20 | P2（部分解除） | ~~观点管道无 CLI；`raw_items` → `author_posts` 归属链路未实现~~ → W0-4 已交付 `import_real_posts.py` 与 `run_opinion_pipeline.py`，真实 19 帖可幂等进入完整链路；**剩余仅 Scheduler 自动调度与非手工来源的作者归属** | Scheduler 阶段 |
| TD-21 | ✅ 已解除 | ~~已批准的 Phase 2 依赖（numpy / pandas / scikit-learn / httpx / jieba）未安装~~ → 用户已 `pip install -e ".[dev]"` 完成安装；`tests/unit/test_text_similarity.py` 的"未安装 jieba"分支因此转为 skip（不是失败） | — |
| TD-22 | ✅ 已解除（2026-09-18） | 原明文文件已不在工作区，受控扫描未发现疑似密钥；用户已确认旧 DeepSeek / 数据库凭据均已在供应商侧轮换失效 | 已修；本地 PostgreSQL 项目密码亦已单独轮换 |
| TD-23 | P1 | **正则抽取器在点位/信息类型字段召回率过低**：全量 1000 格中该判未判 289 格（占失败 **73%**），核心四字段漏判占失败 77.1%；点位字段精确率 100%/90.5% 但召回率仅 33.1%/31.4%。根因：①抽取器按 `docs/10 §4.1/§4.4` 对条件句/引用/复盘/双重否定**一律降级 UNKNOWN 并丢弃方向不一致价格**，而人工裁决在对抗样本上仍给出了点位；②`entry_low/entry_high` 未纳入金标准 → 无法评估；③`information_type` 词典覆盖不足（80 格漏判 + 45 格错判） | 与产品口径对齐后（`docs/10 §4.4` 明确"对抗样本是否仍标注点位"），再改 `regex_extractor` 并复跑 `scripts/evaluate_extractor.py` |
| TD-24 | ✅ 已解除 | ~~**Prompt few-shot 与评测语料逐字重叠（数据泄漏，红线）**~~ → `opinion-prompt-v2` 起 **12 条 few-shot 全部换成语料外自造句**，实测最长公共子串 **≤ 9 字符**（无整句重合）；新增回归测试 `test_few_shot_has_no_overlap_with_evaluation_corpus` 用 LCS 机械拦截（阈值 12） | 已修（2026-09-13，v2/v4/v5 复核） |
| TD-25 | ✅ 已解除 | ~~**`information_type` 判定偏保守（看到数字就判 TECHNICAL）**~~ → 按人工裁决口径"**操作优先，驱动决定分类**"写入 `docs/10 §4.9` 规则 9 的 L1~L6 阶梯，并由 Prompt v2→v5 迭代落实：人工子集准确率 **46.2% → 92.3%**（12/13），100 格 `wrong_value` **8 → 1**。残余 1 格见 TD-27 | 已修（2026-09-13） |
| TD-26 | ✅ 已解除 | ~~**`no_opinion` 与 `docs/10 §4.9` 规则 4 冲突**~~ → 二次裁决确立语义：`no_opinion=true` = "没有可交易的方向性观点"，宏观播报 = `UNKNOWN` + `MACRO` + `no_opinion=true`；`opinions=[]` 只留给"连信息类型线索都没有"的帖子。v2 起 20 条试点**无观点率 10% → 0%**，`mock-post-0006/0022` 的 `stance`/`information_type` 由"漏判"变"命中" | 已修（2026-09-13） |
| TD-27 | ✅ 已解除 | ~~**Prompt 残余 1 格 + `horizon` 口径冲突**~~ → 人工最终裁决：①`mock-post-0009` **修改人工金标准为 `TECHNICAL`**（"操作优先于情绪"，模型纠正人工；已同步 `docs/10 §4.9` 规则 9 L3）；②`docs/10 §4.2` **补"短线思路/短线观望 → `15m`"**，金标准保留 `15M`。复核：20 条试点 Prompt v6 **五字段全 100%**；全量 200 条 `horizon` 由 0% → **100%**、`stop_loss` 0% → **100%**、`take_profit` 25% → **100%** | 已修（2026-09-13） |
| TD-28 | ✅ 已按「路径 B」落地 | ~~**`information_type` 残余误差：驱动型文本 + 操作价位**~~ → 阶梯补 **`L2.5` 消息→`NEWS`** + L3 细化「操作价位优先，但情绪/消息若是**交易理由**（『…因此做多/做空』）则按驱动分类；仅背景附注式才判 `TECHNICAL`」；`docs/10 §4.9` 规则 9 与 `§4.5` 已同步（旧优先级 `MACRO > NEWS > POSITIONING > …` 标注为**以 §4.9 为准**）。实测（全量 200 条人工子集）：信息类型 **87.4% → 91.9%**、**回归 0 格**、核心四字段回到 **100%（四项全 PASS）**。残余 11 格见 TD-29 | 已修（2026-09-13，v13 定版） |
| TD-29 | P2（待真实语料复核） | **「操作块 + 驱动尾句」的 `information_type` 边界**：定版后人工子集仍有 **11 格**不一致（`SENTIMENT→TECHNICAL` 7、`MACRO→TECHNICAL` 4），全部是「完整点位 + 驱动尾句」形态；同一形态在人工金标准里既判过 `MACRO`（`0027`）又判过 `TECHNICAL`（`0009`）、`SENTIMENT`（`0079` 等）→ 属**口径边界**（继续对 Mock 模板调 Prompt 会过拟合，v7→v11 已验证「修 A 坏 B」）。解除条件：用**真实作者帖子**重新裁决该边界后定 v14；验收：真实语料信息类型 ≥ 90% 且同类模板裁决内部一致。**在复核前冻结 v13 与 `§4.9` 规则 9** | Phase 2 真实语料验收阶段 |
| TD-30 | P1（阻塞：正文语料不足） | **源与语料现状**：可用源仅 `fred_blog`（**正文 10 条**，median 2.5k）；`fed_press`/`ecb_press` 标题级 → 均定位 `EVENT` 且 `ecb_press` 已 `enabled=false`；`yahoo_gold`/`jin10`/`fx678`/`wallstreetcn`/`investing_gold` 及**第四轮 5 个黄金垂类源（kitco 404 / mining robots 403 / bullionvault 404 / goldseek robots 取不到 / investing news_301 robots 403）全部不可用** | 要凑 ≥100 条正文语料：**(A)** 用户提供已验证"全文 RSS"（我逐个冒烟）；**(B)** 授权"入口探测"（只读首页找官方 feed 链接）；**(C)** 调低验收目标到现有量级（如 30 条）；**(D)** 另行评审合规/成本的新闻 API。**标注流程按用户裁决暂不启动** |
| TD-31 | P2（已实测，待决策） | **RSS 语料多为摘要级正文**：实测首轮 25 行中 **15 行 `content == title`**（`ecb_press` 14+、`fed_press` 全部），`fred_blog` 10 行才有正文（median 2.5k）；另 `fred_blog` **每篇都带图**（`has_media=true`，图内观点文本不可抽取）→ 观点语料可用量≈10 条 | 决策：①只保留"全文 RSS"源作观点语料；②标题级源统一 `source_type=EVENT`（`ecb_press` 待批）；③或对符合 robots 的源单独立项抓正文 |
| TD-32 | ✅ 已解决（2026-09-17） | ~~`scripts/collect_rss.py --to-db` 未接线~~ → 已接通 `run_collector`，逐源写入 `raw_items + news_events + collector_runs`；默认仍 dry-run，真实写入需显式 `--to-db --no-dry-run`；带 90 天窗口、幂等复跑与单源 >40% 集中度告警 | 已修；W0-3 实测首轮 30 新增、复跑 0 新增 |
| TD-33 | ✅ 已按 (b) 钉版本 | ~~**`ruff format --check` 随 ruff 版本漂移**~~ → venv 里 ruff 为 **0.16.7** 时全仓 76 文件报 "would be reformatted"（71 个是历史文件；88 列 black 风格折行 / docstring 归一化 vs 配置 `line-length = 100`）。**处置：在 `pyproject.toml` 的 dev 依赖里钉死 `ruff==0.16.7`**（文件内注明：升级 ruff 必须独立提交 + 同次跑 `ruff format .` 与全量门禁）；`.github/workflows/ci.yml` 实际门禁为 `ruff check .` + `mypy` + `pytest -q`（**不含 `format --check`**），故 CI 稳定性已恢复。遗留（另立待办）：76 个历史文件与 0.16.7 格式化结果不一致，如需开 `format --check` 门禁，须单独一次 `ruff format .` 纯格式提交 | 已修（2026-09-14） |
| TD-34 | ✅ 已解决（2026-09-13） | ~~**`notes_collect` 混入源配置 `notes`**~~ → 用户裁决"只保留原始发布信息，剔除配置类内容"：`scripts/collect_rss.py::_to_row` **不再拼接 `spec.notes`**，本列只保留本次采集产生的质量标记（含图片 / 无可用发布时间 / 缺时区未猜测 / 采集用途=EVENT）；新增回归测试 `test_csv_notes_exclude_config_metadata` 机械拦截 | 已修 |
| TD-35 | ✅ 已结案（2026-09-14，按**已知边界**归档） | **开源标注基准（标题级情感）不能替代 `docs/08 §5` 验收**：改用 HF 数据集（黄金 100 + 股吧 50）做 regex vs LLM v13 对照，`stance` 命中率仅 **2/100（正则）**、**0/100（LLM）**；但该基准是**标题级情感分类**（≠ 博主帖子观点抽取），且金标准来自外部数据集而非本项目 `docs/10` 口径的人工裁决 | 结论只作**工程对照**，写进《Phase 2 真实语料验收报告》（口径见 `docs/11 §1.6`）；**真实语料验收仍需人工裁决语料**；`docs/05` 的 Phase 2 验收结论**不得**依据本报告；**2026-09-14 裁决：按已知边界归档、不修补**；真实观点提取验证移交 Phase 3 |
| TD-36 | ✅ 已结案（2026-09-14，按**已知边界**归档） | **正则抽取器在英文/标题级语料上基本不产出观点**：黄金 100 条英文标题里无观点 **90 条**、命中 **2 条**——它是为中文博主长帖设计的（词典 + 数字点位规则） | 记为**已知工具设计边界**（报告 §0/§6.2/§8 已写明）；如未来要覆盖英文源，须单独立项做英文规则集，**不在 Phase 2 范围**；**2026-09-14 裁决：按已知边界归档、不修补** |
| TD-37 | ✅ 已结案（2026-09-14，用户裁决：**不改 Prompt**） | **LLM v13 对"描述型新闻标题"大量判 `UNKNOWN`**：黄金 100 条中 **90 格方向拒答**，且**全部** `wrong_value` 都是 `UNKNOWN`（无一格判反方向）——模型把"金价下跌 0.9%"当作**事实描述**而非**可交易观点**（符合 `docs/10 §4.9.0` 抽象原则与"只降不猜"） | **用户裁决（2026-09-14）：维持现状、不做代码修补** —— `opinion-prompt-v13` **保持冻结**（绝不教模型把描述句标成 `SHORT`，避免污染模型定义、未来把新闻当预测）；描述句不出方向、标题级源只作事件层（与 TD-31 一致）；**标题级情感分类留 Phase 3 的 News Alpha**。备选方案（已否决）：新增"新闻事实 → 方向映射"例外口径（会触动 `docs/10 §4.1.5`） |
| TD-38 | ✅ 已加硬门禁（2026-09-17） | W0-4 的 19 条历史帖子没有独立采集时间，旧版曾生成 12 条可计算标签；`forward-return-v2` 现将全部 31 个评价行标记为 `UNTRUSTED_COLLECTION_TIME`，只允许验证管道，禁止进入 OOS / Alpha / 作者权重 | 只有取得可核验的独立 `collected_at` 后才能解除数据限制 |
| TD-39 | ✅ 已解决（2026-09-17） | Phase 3 规划曾把特征表迁移编号写成 `0006`，与已经落地的 `0006_macro_event_vintages` 冲突 | 后续迁移已整体顺延为 `0007`–`0010` |
| TD-40 | ✅ 已解除（2026-09-18） | 冻结 SQLite 快照已事务性迁入独立 `gold_ai_research`：19 表、110,992 行逐表数量与 SHA-256 全部一致；本地 `.env` 已切换，原验收库保留 | 已修；报告见 `docs/experiments/Phase3_W0_PostgreSQL迁移报告.md` |
| TD-41 | ✅ 已解除（2026-09-18） | Yahoo `GC=F` 期货代理的 242 个异常槽令 Regime `UNKNOWN=15.62%`；未插值、未放宽门槛 | 改用独立 `XAUUSD_DUKASCOPY` 现货序列：733 个 BI5 日文件、11,872 根严格 1h，`UNKNOWN=0.606%`，机器门禁全通过。旧 `GC=F` 数据保留且不与现货拼接；仍待 50 点人工盲评完成最终验收 |
| TD-42 | P1（负面研究结论） | Phase 3.2 冻结基线均未过门槛：Technical 校准 LR 的 IC=-0.0951/ICIR=-0.4968；Macro IC=0.0645 但 ICIR=0.1437、方向 p=0.3793。不得为追求 PASS 使用测试集调参，也不得写入 Alpha 事实表 | 保留负面结果；先由用户裁决是否进入 Phase 3.3，或另立新实验版本扩充数据/特征。任何新实验必须重新预注册并保留 untouched test |
| TD-43 | P0（数据阻塞） | Phase 3.3 Author / News 真实数据资格不足：作者可信标签 0（31 行全部采集时间不可信，19 帖缺独立 collected_at）；新闻仅 30 条/56 天且单源占 66.67%。Author Alpha 管线验证已完成（Mock 跑通，见 `docs/experiments/Phase3_3_Author_Alpha_管线验证报告.md`），但真实数据仍 0 可信标签 | **待合法授权数据源**：需要带独立发布时间与采集时间的真实作者帖子；需要合规、带时间戳的新闻历史达到 >=200 条、>=90 天、单源 <=40%。达标前不建条件权重表、不训练模型、不写事实 |
| TD-44 | P0（新闻历史时间语义） | `raw_items` 强制 `effective_at >= collected_at`；今天下载的历史新闻只能从今天起使用，不能把旧 `published_at` 冒充历史可用时间。因此普通历史 CSV 即使补到 200 条也不能用于历史 OOS | 先确认具备授权且可审计历史可用时刻的数据源，再设计 append-only 的历史可用性契约与泄漏测试；在此之前禁止手工回填 News Alpha 历史事实 |
| TD-45 | P0（作者内容采集授权） | 公开可访问不等于允许自动采集或训练。Kitco 条款明确禁止机器人/自动设备检索、数据挖掘及未经授权存储或复制内容；中金在线候选页又存在证书域名不匹配 | 不绕过证书警告，不对 Kitco 启动自动采集。用户需提供具备自动采集/研究使用授权的数据源、官方 API 或书面许可；授权确认前只保留合规审查，不保存正文样本、不写数据库 |
| TD-46 | P2（可观测性剩余） | GOLD-004 已交付**只读**健康度 / 资格观测（`src/monitoring/` + `scripts/report_collector_health.py`，证据见 `PROGRESS_LOG.md` 第六十八轮）；**剩余**：仅有报告、无告警推送与时间序列趋势，且 `processed_items.status` 的 `SKIPPED` 无法在 SQL 层区分 `DUPLICATE` / `REJECTED`（口径见 TD-19 与 `src/monitoring/collector_health.py` 模块 docstring，需要时读 `structured_json.outcome` 抽样或新列） | 有运维/巡检需求时（TD-10 前置） |
| TD-47 | P0（证据入口边界，仍 BLOCKED） | GOLD-005 已交付**合规授权数据 Evidence Intake Gateway**（`src/evidence/` + `scripts/intake_evidence.py`，契约 `evidence-intake-v1`；证据见 `PROGRESS_LOG.md` 第六十九轮）。**剩余**：①仓库内**没有任何**经该入口认证的真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45 未解除）；②入口只写 `raw_items` + `processed_items`，**不**创建 `authors` / `author_accounts` / `author_posts`（作者链接入仍走既有授权门禁 + 导入流程），Author Alpha 的可信标签仍需真实数据 + 标注；③`available_at` 证据的**法律/提供方真实性**由人工核验，程序只校验字段齐全与时间自洽；④`evidence_intake` 子报告只在资格报告里展示，尚未做告警 | 业务方按契约提交已授权数据 + 人工核验授权后，再看是否达标；若需自动化的作者链落库，另立任务并评估是否复用 `import_real_posts.py` 口径 |
| TD-48 | P0（证据就绪度 / 一键复核的剩余边界） | GOLD-006 已交付 operator 证据模板（`examples/evidence/`，示例行显式标记 `record_kind=example`、`is_mock=true`）+ 只读 preflight/readiness（`src/monitoring/evidence_readiness.py`）+ 一键 `recheck` CLI（`scripts/evidence_readiness.py`，证据见 `PROGRESS_LOG.md` 第七十轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47 未解除），工具只报告缺口、不放行；②模板 / 示例行判 `SYNTHETIC_EVIDENCE` 隔离，**不能**用于达标；③readiness 只覆盖证据入口台账口径（Author 可信 eligible 帖子数；News 条数 / 覆盖天数 / 单源占比），不含作者链落库与人工标注；④无告警推送（与 TD-46 同源） | 业务方按契约提交已授权数据 + 人工核验后重跑 `recheck`；若需自动作者链落库，另立任务评估复用 `import_real_posts.py` 口径 |
| TD-49 | P0（证据 operator 工作流 / 作者链的剩余边界，仍 BLOCKED） | GOLD-007 已交付**单入口 operator workflow**（`scripts/evidence_operator.py` + `src/evidence/workflow.py`：`template → preflight → quarantine → intake → recheck`，默认 dry-run / 零网络 / 零写入，写入前二次完整 Evidence Gateway 校验 + 写入门禁与脱敏隔离摘要；证据见 `PROGRESS_LOG.md` 第七十一轮）与 **gateway-only 作者归属链**（`src/evidence/author_chain.py`：只消费 `raw_json.evidence` 带 `evidence-intake-v1` + `scope=author` + `oos_eligible=true` 的记录；普通 CSV / 历史样本没有证据块，**无法绕过** gateway；身份冲突跳过且不覆盖；新建 `author_accounts` 一律 `enabled=false`）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47/48 未解除），`blocker_active` / `human_gate_required` 恒为 true；②`scripts/import_real_posts.py` 仍是**历史 W0-4 / 演练用普通 CSV 入口**（`raw_json` 只写 `import_kind=manual_real_corpus`，无证据块），其口径**不**适用于 Phase 3.3 资格证据，作者链也不会消费它（未新增 migration，沿用既有 `authors` / `author_accounts` / `author_posts` schema）；③作者链只建立归属，不做观点抽取 / 人工标注，也未接入任何采集；④无告警推送（与 TD-46 同源） | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据 + 人工核验后重跑 `scripts.evidence_operator workflow`；作者链落库后仍需真实标注才能产生可信标签 |
| TD-50 | P0（人工交接包，仍 BLOCKED） | GOLD-008 已交付**只读 / 默认 dry-run 的 Evidence 人工交接包**（`scripts/evidence_handoff.py` + `src/evidence/handoff.py`：稳定 JSON + 人类可读 Markdown，量化 Author/News `eligible`/`required`/`remaining`、coverage gap、source-share 可评估性、主要隔离原因码；六类人工证据 checklist（authorization / provenance / published_at / collected_at / availability / identity）；模板 / Mock / 示例 / 历史 CSV 醒目标记为**不计资格**；证据见 `PROGRESS_LOG.md` 第七十二轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47/48/49 未解除），`blocker_active` / `human_gate_required` 恒为 true，`data_qualification_passed` / `phase_transition_allowed` 恒为 false；②交接包**只降低人工交接摩擦**，量化达标时最多 `ready_for_human_review=true`，Phase 切换仍须 `DEVELOPMENT_PROTOCOL` 的 L3 人工确认；③`blocker_active` 需要在**人工 Gate** 完成并留下可审计留痕后才可能变化（当前工具不具备该能力）；④无告警推送（与 TD-46 同源） | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据 + 人工核验授权与历史可用性，再重跑 `scripts.evidence_operator workflow` 与 `scripts.evidence_handoff` |
| TD-51 | P0（readiness 状态变更通知，仍 BLOCKED） | GOLD-009 已交付**只读 / 默认 dry-run 的 readiness 状态变更通知层**（`src/evidence/readiness_watch.py` + `scripts/evidence_readiness_watch.py`：确定性脱敏白名单快照 + SHA-256 指纹 + 幂等变化检测 `FIRST_SNAPSHOT` / `BLOCKER_GAP_CHANGED` / `REASON_CODES_CHANGED` / `READY_FOR_HUMAN_REVIEW_ENABLED` / `READY_FOR_HUMAN_REVIEW_REVOKED`；口径复用 `src.evidence.handoff` / `src.monitoring.evidence_readiness`，**不复制阈值算法**；证据见 `PROGRESS_LOG.md` 第七十三轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47/48/49/50 未解除），`blocker_active` / `human_gate_required` 恒为 true，`data_qualification_passed` / `phase_transition_allowed` 恒为 false；②通知层**只减少盯盘 / 轮询**，事件只落本地文件型安全出口（显式 `--out` / `--events` 原子写），**不接**邮件 / 短信 / Webhook / 第三方推送；`ready_for_human_review=true` 也仍须 L3 人工 Gate；③state 损坏（JSON / schema / 指纹校验失败 / 声称 blocker 已解除）一律**安全失败**退出 `4` 且不写任何输出；④事件只含脱敏白名单标量与稳定原因码，不含正文、凭据或完整 source config | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据 + 人工核验授权与历史可用性后重跑 `workflow` / `handoff`，并以 `--state` + `--out` 让通知层只报**有意义变化**；Phase 切换仍须 L3 人工确认 |
| TD-52 | P0（周期 tick runner，仍 BLOCKED） | GOLD-010 已交付**纯本地单次 tick runner**（`src/evidence/readiness_runner.py` + `scripts/evidence_readiness_runner.py`：只做**一次** tick、由**外部定时器**（Windows Task Scheduler / 现有本地 orchestrator）调用，不自带常驻循环、不新增第三方 scheduler 依赖、不自动修改 OS 计划任务；OS 级**单实例锁**（`msvcrt` / `fcntl`，owner / pid / acquired_at 可审计且不含敏感数据、**绝不删除 / 绝不改写**活动锁）与**陈旧锁安全接管**（`recovered_stale` + `previous_owner` 留痕）；工作目录内三类 artifact（`readiness_state.json` / `readiness_events.jsonl` / `readiness_status.json`）全部**原子写**，事件日志保留最新 200 条且**本次新事件永不丢弃**；写入顺序**事件优先**（宁可重复，绝不丢失）；无变化 → 0 重复事件；损坏 state / 事件日志 → 退出 `4`、锁冲突 → `6`、资格计算失败 → `7`、工作目录 / artifact 不可写 → `3`，全部 **fail-closed** 且保留旧 state、stdout 为空、错误脱敏；证据见 `PROGRESS_LOG.md` 第七十四轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47/48/49/50/51 未解除），`blocker_active` / `human_gate_required` 恒为 true、`data_qualification_passed` / `phase_transition_allowed` 恒为 false；②runner **只把盯盘自动化**，不是资格判定器，量化达标最多 `ready_for_human_review=true`，Phase 切换仍须 L3 人工确认；③只写显式 `--work-dir`，不联网 / 不写数据库 / 不接第三方推送 / 不自动配置计划任务；④退出码 `5` 是**预期 BLOCKED**，不是定时器故障（真正的故障只有 `2` / `3` / `4` / `6` / `7`） | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据 + 人工核验授权与历史可用性后重跑 `workflow` / `handoff`；再由**人工**把 runner 挂到 Windows Task Scheduler / 本地 orchestrator（例如每 30 分钟一次；本工具不会替你配置），并以 `readiness_status.json` / `readiness_events.jsonl` 只看**有意义变化** |
| TD-53 | P0（本地 inbox 发现与预检，仍 BLOCKED） |  Evidence Inbox 发现与预检**（`src/evidence/inbox.py` + `scripts/evidence_inbox.py`：只扫描显式 `--inbox-dir` 的**直接子目录**，候选包必须由 `manifest.json` 显式关联证据文件（`evidence_type` / `source`（或别名 `provider`）/ `authorization_reference` / `time_semantics` / `availability_semantics` / `historical_oos_applicable` / `files[].path` + `sha256`）；逐文件实测 SHA-256 并核对声明，不一致即隔离且**不再解析内容**；候选包指纹只由内容摘要与结构标记派生（**不含** 文件名 / mtime / 绝对路径 / 扫描时间）；pending 清单以指纹为键**幂等**（同内容不重复生成、内容变化 → 新条目、同指纹状态变化记入 `status_changed`）且**原子写**；路径穿越 / 绝对路径 / 符号链接 / 未声明文件 / 摘要不一致 / 凭据泄漏 / 示例模板一律 **fail-closed** + 稳定原因码；逐行预检复用 Evidence Gateway 的 `assess_row` 与 `valid_reference`（`evidence-intake-v1`），**不复制、不降低**任何阈值；**绝不移动 / 删除 / 改写原始证据、绝不自动 intake、零网络、零数据库写入**；`blocker_active` / `human_gate_required` 恒为 true、`data_qualification_passed` / `phase_transition_allowed` 恒为 false、`requires_human_action` 恒为 true（证据见 `PROGRESS_LOG.md` 第七十五轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~52 未解除）；②`preflight_pass` 只进**人工确认 / 显式 intake** 队列，不证明授权法律效力、不解除 blocker，落库仍须 `evidence_operator workflow --no-dry-run` + `handoff`/`recheck` 与 L3 人工 Gate；③`files[].path` 只允许包内**单级文件名**，包内除 manifest 外的文件必须**全部**声明（否则整包隔离）；④pending 清单是当前 inbox 内容的镜像，不接邮件 / 短信 / Webhook / 第三方推送 | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据（放入 inbox 子目录 + `manifest.json`）→ `scripts.evidence_inbox --out` 预检 → 人工确认后 `scripts.evidence_operator workflow --no-dry-run` 显式落库 → `handoff` / `recheck` 复核；Phase 切换仍须 L3 人工确认 |
| TD-54 | P0（人工复核决策与审计，仍 BLOCKED） | GOLD-012 已交付**纯本地的人工复核决策与审计闭环**（`src/evidence/review.py` + `scripts/evidence_review.py`：只对**显式内容级指纹**记录 `APPROVE` / `REJECT` / `NEEDS_CHANGES`（受控词表，原因码必须与决策匹配）；`APPROVE` 必须①该指纹**当前仍在** inbox 扫描结果中、②当前预检 `PREFLIGHT_PASS`、③**不是**模板 / 示例 / Mock，内容变化 → **新指纹**（旧批准绝不继承），候选消失 / 预检回退 / ledger 损坏 / 元数据含凭据一律 **fail-closed** 且零写入；**追加式** ledger（`kind=evidence_inbox_review_ledger`、确定性 `decision_id`、完全相同决策**幂等**不新增记录、任何差异必须显式 `--revision <既有+1>` + `--override` 且 `supersedes` 留痕、**绝不静默覆盖**、原子写 + 单实例锁）；`--out` 才写 ledger、`--ledger` 只读、`--approved-out` 才写**脱敏** approved-for-explicit-intake 清单，且清单在**当前**扫描结果上**重新验证**（失效批准进 `invalidated`：`CANDIDATE_MISSING` / `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`）；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络、零新增依赖 / migration**；复核元数据（reviewer / note / 文件名 / mtime / 复核时间 / 人工批准本身）**不是**证据时间；四个安全字段恒为 true / true / false / false（与 approve 数量无关）。证据见 `PROGRESS_LOG.md` 第七十六轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~53 未解除）；②人工 `APPROVE` **只表示人工预审通过**，不等于 data qualification PASS、不解除 blocker，落库仍须 `evidence_operator workflow --no-dry-run` + `handoff`/`recheck` 与 L3 人工 Gate；③复核元数据只接受**非敏感**最小字段，凭据类内容一律拒绝记录（不会落盘为 `***`）；④ledger 是唯一事实来源，批量 approve 不会改变任何安全字段，也不触发任何自动化 | 业务方按 `evidence-intake-v1` 提供真实授权 Author/News 数据 → `scripts.evidence_inbox --out` 预检 → 本工具逐指纹 `--decision approve` 记录人工决策并生成批准清单 → 人工按清单**显式** `scripts.evidence_operator workflow --no-dry-run` 落库 → `handoff` / `recheck` 复核；Phase 切换仍须 L3 人工确认 |
| TD-55 | P0（最终写入前 intake plan 门禁，仍 BLOCKED） | GOLD-013 已交付**纯本地只读**的 approved-for-explicit-intake **intake plan** 与最终写入前门禁（`src/evidence/intake_plan.py` + `scripts/evidence_intake_plan.py`：把 GOLD-012 批准清单与**当前** inbox / review ledger **重新绑定核验** —— ①清单结构自洽（文档标识 / schema / 契约版本 / `approved_count` / `invalidated_count` 与列表长度一致 / 四个安全字段与 `approval_scope` 不可被 state 削弱 / **不得**出现任何证据时间字段）；②每条批准必须**当前仍是** ledger 上该指纹的**最新有效**决策（`revision` / `decision_id` 完全一致，`review override` 后旧批准一律失效）；③该指纹**当前仍在** inbox 且仍 `PREFLIGHT_PASS`、非模板 / 示例 / Mock、摘要与复核时一致；④清单与"用当前 inbox + ledger 重新算出的批准集合"完全一致（条目 / 失效项 / 计数 / 决策计数）—— 任一不一致 → **fail-closed**（退出码 `4`、**零写入**）；输出**确定性脱敏**、**内容级** `plan_id`（不含 `generated_at`；同输入同 `plan_id`、同审计时点逐字节稳定）的只读计划（默认零写入，只有显式 `--out` 才**原子**落盘计划本身），`handoff` 只给**字符串**命令模板（必带 `--no-dry-run` 与显式 `--input`），`auto_intake_allowed` / `writes_database` 恒 false、`requires_explicit_operator_action` 恒 true；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络、零新增依赖 / migration**。证据见 `PROGRESS_LOG.md` 第七十七轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~54 未解除）；②计划**不是**资格判定器：`approved_for_explicit_intake` 与 `data_qualification_passed` 是两个独立字段，后者**恒为** false（`data_qualification_passed_count` 恒为 0），批准数量不改变任何安全字段；③真实落库必须由人工**显式**执行 `scripts.evidence_operator.py workflow --no-dry-run`（本工具不自动执行、不写研究数据库），随后 `handoff` / `recheck` + L3 人工 Gate；④计划只反映**当前** inbox / ledger 事实：内容或 review revision 变化必须重新生成（旧计划 / 旧批准不静默继承） | 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据 → `scripts.evidence_inbox --out` 预检 → `scripts.evidence_review --decision approve ... --approved-out` 人工复核 → 本工具 `--out` 生成计划与显式 `handoff` 命令 → 人工**显式** `scripts.evidence_operator workflow --no-dry-run` 落库 → `handoff` / `recheck` 复核；Phase 切换仍须 L3 人工确认 |
| TD-56 | P0（执行后的 Intake Receipt 与资格复核审计，仍 BLOCKED） | GOLD-014 已交付**纯本地只读**的执行后收据核验层（`src/evidence/intake_receipt.py` + `scripts/evidence_intake_receipt.py`：把 GOLD-013 plan、**当前** inbox / review 状态、人工**显式** Evidence Operator 执行结果（`--no-dry-run --manifest` 产物）与随后的 qualification recheck 绑定成**确定性脱敏、内容寻址**（`receipt_id`）的收据；plan stale / fingerprint drift / review override / 执行结果 dry-run 或零落库或计数矛盾或输入内容不符 / recheck 缺失或早于执行或被改写 → **fail-closed**；`intake_executed` / `receipt_verified` **绝不**蕴含 `data_qualification_passed` / `phase_transition_allowed`（恒 false）；默认零写入，只有显式 `--out` 才原子落盘收据）。最小路径：`evidence_intake_plan --out` 生成计划 → 人工**显式** `scripts.evidence_operator workflow --no-dry-run --manifest` 落库 → `recheck --report` → 本工具 `--operator-result` + `--recheck` 生成收据 → **L3 人工 Gate**；Phase 切换仍须 L3 人工确认 |
| TD-58 | P0（L3 人工决策记录与防伪审计，仍 BLOCKED） | GOLD-016 已交付**纯本地、显式人工输入**的 L3 人工决策记录层（`src/evidence/decision_record.py` + `scripts/evidence_decision_record.py`：把**人工显式**给出的 `approve` / `reject` / `needs_changes` 绑定到**具体**的 GOLD-015 packet（`packet_id` + `content_sha256` + `artifact_sha256`），生成**确定性、脱敏、内容寻址**（`record_id`）的决策记录；packet **逐项**完整性核验（文档身份 / schema / 契约版本 / 安全字段不可被削弱 / 缺口与检查**算术可重算** / 三个布尔必须等于 `submit_to_l3_human_gate` 的合取 / readiness 快照**指纹同源** / 收据与 `receipt_verified` 往返一致 / recheck 与 `qualification_recheck_ready` 合取一致 / 批准与执行计数自洽 / `verification.violations` 为空 / **递归禁止任何证据时间键** / **禁止未来时间**）→ 任一不一致 **fail-closed**（稳定原因码 + 零写入，退出 `3` / `4` / `6`）；`approve` **只**允许落在 packet 本身 `submit_to_l3_human_gate=true` 且完整性核验通过时（BLOCKED → `PACKET_NOT_SUBMITTABLE`，退出 `5`、零写入），`reject` / `needs_changes` 可记录但**绝不**改变任何资格状态；`human_decision_recorded` / `human_decision` / `packet_verified` 三个事实独立，`data_qualification_passed` / `phase_transition_allowed` / `phase_transition_executed` 恒 false、`blocker_active` / `human_gate_required` 恒 true、`human_gate_level` 恒 `L3`（**硬编码**）；`record_id` **不含**任何审计时间（同一 packet + 同一人工决策 + 同一 `revision` / `supersedes` 幂等、同审计时点逐字节稳定），写下的记录**绝不**被静默覆盖（覆盖必须显式 `--revision` + `--supersedes` 指向**当前**记录且 revision 严格递增）；`--verify-record` 提供**防伪核验**（由记录文档重新推导 `record_id`、与**当前** packet 比对 `packet_id` / 内容摘要、判定当初的 `approve` 是否仍成立，并区分 `PACKET_STALE` / `RECORD_ID_MISMATCH` / `RECORD_TAMPERED`）；人工身份只接受**非敏感 label**（拒绝空值 / 超长 / 非法字符 / 疑似凭据，**绝不**采集口令 / token / 密钥），`note` / `reason-code` 一律 `src.common.redaction` 脱敏 + 限长（过长 fail-closed）；`decision_at` / `generated_at` / 文件 mtime **绝不当证据时间**；默认零写入，只有显式 `--out` 才先取单实例锁再**原子**落盘记录本身；**绝不写数据库、绝不调用 intake / commit、绝不修改 `PROJECT_STATE`、零网络、零新增依赖 / migration**；证据见 `PROGRESS_LOG.md` 第八十轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~57 未解除）；②本工具**只证明"某个具体 `packet_id` 收到过明确的人工决策"**，**不是**资格判定器、**不是** Phase transition executor：记录存在 / `packet_verified=true` 也**不会**自动改变任何安全字段；③Phase 切换仍须 `DEVELOPMENT_PROTOCOL` 的 **L3 人工确认**并由人工 / Orchestrator 显式更新 `PROJECT_STATE`（本工具不具备该能力） | 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据 → `scripts.evidence_inbox --out` 预检 → `scripts.evidence_review --decision approve ... --approved-out` 人工复核 → `scripts.evidence_intake_plan --out` 生成计划 → 人工**显式** `scripts.evidence_operator workflow --no-dry-run --manifest` 落库 → `recheck --report` → `scripts.evidence_intake_receipt --out` → `scripts.evidence_decision_packet --out` 生成决策包 → **本工具 `--decision approve --reviewer <label> --out` 记录 L3 人工决策** → `--verify-record` 事后防伪复核 → **L3 人工确认** |
| TD-61 | P0（证据缺口只读诊断，仍 BLOCKED） | GOLD-026 已交付**只读**的 `PHASE3_3_DATA` 缺口诊断（`src/monitoring/evidence_gap_diagnostic.py` + `scripts/evidence_gap_diagnostic.py`：复用 readiness（GOLD-006）/ handoff（GOLD-008）/ inbox manifest 预检（GOLD-011）/ `evidence-intake-v1` 契约字段与原因码，**不新增 / 不降低**任何阈值；四态分类 `CODE_READY` / `EVIDENCE_MISSING` / `HUMAN_VERIFICATION_REQUIRED` / `GATE_BLOCKED`；顶层 `readiness` 恒 `GATE_BLOCKED`，`advance_allowed` / `l3_l4_auto_advance_allowed` / `data_qualification_passed` / `phase_transition_allowed` 恒 false；Mock / 模板 / 示例恒 `counts_toward_eligibility=false`；缺失字段只报事实与人工下一步，**绝不**用当前时间 / 文件 mtime / 抓取时间 / 推断值填补；证据见 `PROGRESS_LOG.md` 第八十九轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~60 未解除）；②诊断**不是**资格判定器、不采集、不写库、不推进 Phase，也没有任何 `qualify` / `approve` / `advance` 参数；③manifest 事实只来自**显式** `--inbox-dir` 的本地目录（缺省不扫描），合成 / 模板标记只有在扫描到候选包时才可见；④L3 / L4 仍只能人工推进 | 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据并人工核验后重跑 `scripts.evidence_gap_diagnostic --json`；Phase 切换仍须 L3 人工 Gate |
| TD-57 | P0（L3 人工决策包，仍 BLOCKED） | GOLD-015 已交付**纯本地只读**的 L3 人工决策包（`src/evidence/decision_packet.py` + `scripts/evidence_decision_packet.py`：把最新 readiness / handoff（GOLD-008/010）、批准清单与 GOLD-013 plan、GOLD-014 收据与 qualification recheck 聚合为**确定性、脱敏、内容寻址**（`packet_id`）的决策包；handoff 结构自洽（内部算术 / `thresholds` 必须等于**当前**唯一阈值来源 / 安全字段不可被削弱 / 拒绝任何证据时间键）、`--readiness` 快照必须与 handoff **同源**（逐字段 + 指纹）、plan / 批准 / 执行结果 / recheck 沿用 GOLD-014 的稳定原因码、可选 GOLD-014 收据文件再做一次内容寻址交叉核对（`receipt_id` / `plan_id` / 指纹 / review revision / 执行摘要 / recheck 摘要）、handoff 不得早于最近一次显式落库（`HANDOFF_STALE`）→ 任一不一致 **fail-closed**（退出 `4`、零写入）；显式区分 `evidence_ready_for_human_review` / `receipt_verified` / `qualification_recheck_ready`，只有三者**同时**成立才 `submit_to_l3_human_gate=true`（退出 `0`），否则退出 `5`（预期 BLOCKED）；`data_qualification_passed` / `phase_transition_allowed` 恒 false、`blocker_active` / `human_gate_required` 恒 true、`human_gate_level` 恒 `L3`（**硬编码**）；`packet_id` **不含** `generated_at`（同输入幂等、同审计时点逐字节稳定）；默认零写入，只有显式 `--out` 才先取单实例锁再原子落盘；证据见 `PROGRESS_LOG.md` 第七十九轮）。**剩余**：①库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`（TD-43/44/45/47~56 未解除）；②决策包**只回答"证据是否齐备 / 还缺什么 / 是否可提交人工 Gate"**，**不是**资格判定器：三个布尔为 true 也**不会**自动改变任何安全字段；③Phase 切换仍须 `DEVELOPMENT_PROTOCOL` 的 **L3 人工确认**，本工具不具备解除 blocker 的能力；④可选 `--readiness` 是把 handoff 锚定到**真实 tick 产物**的唯一离线手段，缺省只绑定 handoff 自身（标注 `NOT_PROVIDED`） | 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据 → `scripts.evidence_inbox --out` 预检 → `scripts.evidence_review --decision approve ... --approved-out` 人工复核 → `scripts.evidence_intake_plan --out` 生成计划 → 人工**显式** `scripts.evidence_operator workflow --no-dry-run --manifest` 落库 → `recheck --report` → `scripts.evidence_intake_receipt --out` → **本工具 `--out` 生成决策包** → **L3 人工确认** |
| TD-64 | P0（APPROVE 门禁绑定材料级核验凭证，仍 BLOCKED） | GOLD-029 已把 GOLD-012 的 `APPROVE` 从"人工填受控 reason code 即可批准"升级为**必须绑定 GOLD-028 材料级人工核验凭证**：`src/evidence/review.py::build_attestation_binding()` 在记录前用 `load_attestation_document()` + `verify_attestation()` 与**当前**候选目录逐项重新绑定核验（凭证文档完整性 / package fingerprint / package 内容身份 / handoff 内容身份 / `scope` / `preflight_pass` / `all_required_verified`），缺失 / 陈旧 / 漂移 / 被篡改 / 部分核验一律 `ReviewAttestationError`（退出码 `4`、零写入）；`ReviewRecord` 只保存**最小** `attestation` binding 白名单（binding 版本 / GOLD-028 schema + contract 版本 / `attestation_id` / 凭证文档 `sha256` / package + handoff 内容身份 / `scope` / `all_required_verified`），**绝不**保存凭证正文 / 材料备注 / `reviewer` / 任何证据时间；`REJECT` / `NEEDS_CHANGES` **不**需要凭证；旧 ledger（无 binding 的 `APPROVE`）仍可读、`compute_decision_id` 派生口径不变、**绝不**原地迁移 / 重写，但 `build_approved_intake_list()` 与 `evidence_intake_plan` 会以稳定原因码 `ATTESTATION_MISSING` 把它列入 `invalidated`（新门禁只对后续 / 当前重新验证生效，绝不追溯升级历史） | 业务方先用 `scripts.evidence_human_verification_attestation --out` 生成当前凭证，再 `scripts.evidence_review --decision approve --attestation <凭证> --out <ledger> --approved-out <list>`；Phase 切换仍须 L3 人工确认，`PHASE3_3_DATA` 保持 BLOCKED |





---

## 3. 技术债明细

### TD-01 外部数据源未真实联调（P1）

- **现状**：`market_collector`（Yahoo chart）、`news_collector`（RSS/Atom）、`macro_collector`（FRED）
  的解析逻辑全部由 Mock 响应驱动；`AiohttpTransport` 的真实网络分支**没有测试覆盖**（按裁决刻意如此）。
- **影响**：provider 的字段/限流/UA 策略变化无法被自动发现；首次真实运行时可能出现 403/429 或结构变更。
- **解除条件**：在本地 `.env` 配置 `FRED_API_KEY` 后，对单个 series 执行一次低量真实冒烟
  （1 个来源、1 个 series、单轮），确认：HTTP 200、`observations` 结构未变、`macro_events` 落库正确、
  密钥不出现在日志 / `source_url` / `raw_json`。
- **验收标准**：冒烟记录（请求数、落库行数、告警数）写入本文件「变更日志」，并把异常处理分支补齐测试。

- **W0-2 实测（2026-09-17）**：FRED Key 经集中配置读取且不进入 repr/日志/raw；DFF 单 series
  冒烟成功（6 条、0 重试）；采用官方 `output_type=4`（Initial Release Only）按年度分块回填
  CPIAUCSL/PCEPI/PAYEMS/DFF/DFII10/DGS10/DTWEXBGS/FEDFUNDS，共 **11,680** 条。
  第二轮 `inserted=0`、`duplicate=11,680`、`failed_chunks=0`；时间违规/重复键/密钥泄漏均为 0。
  TD-01 中 **FRED 真实联调部分已解除**；Yahoo/RSS 与 PostgreSQL 写路径仍按各自技术债管理。
- **保留限制**：完整 ALFRED 修订链未回填。`output_type=1` 的 realtime 边界会被查询窗口裁剪，
  不能作为真实 revision end；当前数据只代表初值，禁止使用普通 FRED 最新修订值补历史空洞。

### TD-02 PostgreSQL 采集写路径端到端验证（已解除）

- **现状**：单元/集成测试统一跑 SQLite（`tests/conftest.py::sqlite_url`）。CI 的 `postgres` 作业
  只做 `alembic upgrade head` + `scripts/check_pg_schema.py`（原生类型 + 种子幂等）+ `downgrade base`；
  **没有在 PG 上跑过任何采集器 / 端到端管道**。
- **影响**：SQLite 不保存时区偏移、无 JSONB 语义，采集写入在 PG 上可能暴露
  （a）`TIMESTAMPTZ` 精度/时区归一化差异；（b）`warnings_json` JSONB 写入路径；
  （c）唯一索引 `uq_market_bars_source_instrument_timeframe_open` 的 `NULLS` / 批量插入行为差异。
- **解除条件**：对 PostgreSQL 实例执行
`python scripts/phase1_pipeline_report.py --db-url postgresql+psycopg://... --migrate`，全部约束探测为 PASS。
- **验收标准**：报告 PG 段落中「时间因果违规 = 0」「唯一约束拒绝重复插入 = PASS」，
且各表 `count(*)` 与 SQLite 基线一致。

- **解除记录（2026-09-17）**：安装 PostgreSQL **16.15**，服务 `postgresql-x64-16` 为
  `Running / Automatic`；项目角色 `gold_ai_app` 默认时区为 UTC。`alembic upgrade head` 到
  `0006_macro_event_vintages`，`scripts/check_pg_schema.py` 的 15 表原生类型与种子幂等检查 PASS。
  `phase1_pipeline_report.py` 在 PostgreSQL 上连续运行两轮：market/news/macro 均 SUCCESS，
  `raw_items=14`、`market_bars=10`、`news_events=2`、`macro_events=2`，不变式违规 0。
  验证过程中发现控制台曾打印原始连接 URL；项目角色密码已立即轮换使旧值失效，控制台改为
  `_mask_url` 并增加回归测试，复跑只显示 `***`。

### TD-03 `4h` K 线聚合缺失（P1）

- **现状**：`market_yahoo` 的 `config_json.timeframes` 声明了 `4h`，但 Yahoo chart API 只提供
  `1m/2m/5m/15m/30m/60m/90m/1d/...`；`MarketCollector` 对 provider 不支持的周期**跳过并告警**，
  `market_bars` 中**不存在** `timeframe='4h'` 行（不伪造、不上采样）。
- **影响**：Phase 2 的 4h 前瞻评价窗口（`15m/30m/1h/4h/1d`）缺少现成 4h K 线。
- **解除条件**：在 Processor 层新增 `bar_aggregator`（写 `processed_items.processor_name`），
  由 `1h`（或 `1m`）确定性聚合出 `timeframe='4h'` 并写入 `market_bars`；必须保证：
  聚合只在**已收盘**窗口上进行（`close_time <= collected_at`），且聚合行
  `effective_at >= 最后一根子 K 线 close_time`（否则构成泄漏）。
- **验收标准**：新增 `pytest` 用例覆盖（a）4h 行 OHLC 与子 K 线一致；（b）未收盘窗口不产出；
  （c）`effective_at` 不变式成立；（d）重跑幂等。

- **进展（2026-09-17，Phase 3.0 W0-1）**：`scripts/_market_data.py::aggregate_bars_to_4h` 与
  `scripts/backfill_market_bars.py::aggregate_and_insert_4h` 已交付；真实回填产生 XAUUSD/DXY/USDCNY
  的派生 4h bar，半桶分别丢弃 620/239/1444 个，连续复跑的 4h 新插入均为 0。相关单元/集成测试
  覆盖 OHLC、半桶、时区读回、`effective_at` 与幂等。
- **剩余解除条件**：聚合尚未作为独立 Processor 写入 `processed_items` 与 `data_versions`，不满足
  `docs/06 §13` 的完整加工血缘要求；在补齐 append-only Processor 记录和 PostgreSQL 写路径验收前，
  TD-03 保持 **P1 / 部分解除**，不得标为完全关闭。

### TD-04 时区不明确的新闻不生成 `news_events`（P1）

- **现状**：feed 未给时区（RFC822 无 `GMT`/`-0400`、ISO8601 naive）时，`news_collector` 采取保守策略：
  `raw_items.published_at = NULL`、`effective_at = collected_at`、
  `raw_json.published_at_tz_ambiguous = true`，**不生成** `news_events`，并写入
  `collector_runs.warnings_json`。
- **影响**：这类新闻（尤其国内财经站点）目前**不可用于研究**（只留原始层）。
  好处是绝不猜测时区、也不可能泄漏。
- **解除条件**：Phase 2 决策二选一：
  （a）为每个 `sources` 显式登记默认时区（`sources.timezone`）并按该时区解释 naive 时间，
  在 `raw_json` 记录推理依据，然后**回填**历史 `news_events`（新增版本，不覆盖原始层）；
  （b）继续排除此类新闻，但在研究层显式标注覆盖率与被排除量。
- **验收标准**：决策写入 `docs/04 §14` 与 `docs/05`；实现后必须有一致性测试
  （回填记录 `effective_at >= published_at`，且引用的 `raw_items` 内容未被修改）。

### TD-05 / TD-07 经济日历采集器未实现，宏观预期值缺失（P1/P2）

- **现状**：`econ_calendar_investing` 源已入库但 `enabled=false`，`config_json.collector` 为
  `econ_calendar_collector`（**未注册**，运行期会明确报错而不是静默跳过）。
  `macro_events.forecast_value` / `previous_value` 恒为 `NULL`（FRED 观测不含预期值，禁止凭空推断）。
- **影响**：Phase 3 的 Macro Alpha 无法使用「预期差（surprise = actual − forecast）」这一高价值特征。
- **解除条件**：实现 `econ_calendar_collector`（仅限公开/授权可抓取的日历接口，**严禁未授权爬取**），
  以 `(event_code, country, event_at)` 与 FRED 观测对齐后补全 `forecast_value` / `previous_value`。
- **验收标准**：surprise 可计算且通过时间因果检查 —— 日历的预期值必须在 `event_at` **之前**已知，
  其 `effective_at` 必须 ≥ 日历发布时间（而非 `event_at`）。

### TD-06 微博采集器未实现（P1，Phase 2）

- **现状**：`weibo_main` 源已入库（合规确认前不应开启 `enabled`），无 `weibo_collector` 代码。
- **影响**：Author Lab 的核心输入（作者观点文本）依赖它。
- **解除条件**：明确合规途径后实现；`.clinerules` 明确**严禁未授权抓取微博**。
- **验收标准**：采集器具备 cookie/限流控制、退避重试、`raw_media` 图片下载与 SHA256 去重。

### TD-08 生产源地址未配置（P1）

- **现状**：`jin10_flash` / `sina_finance_gold` / `investing_news` 的 `config_json.feeds` 为 `[]`；
  `market_stooq_backup` 为 `enabled=false` 备用源。留空时采集器在运行期给出**明确错误**
  （不会静默成功）。
- **解除条件**：运维填入经授权的公开 RSS/Atom 地址并 `enabled=true`。
- **验收标准**：`python -m database.seeds --dry-run` 演练通过，且启用后各来源有成功轮次（`collector_runs` 有记录）。

### TD-09 Scheduler 未实现（P0）

- **现状**：只有「单轮运行」编排（`src/collectors/runner.py::run_collectors`），没有 30 分钟调度器、
  幂等 `job_runs` 写入、worker 崩溃恢复。
- **影响**：无法满足 `docs/05` 的 Phase 1 验收门槛「连续运行至少 7 天」。
- **解除条件**：实现调度框架（APScheduler 或自研循环 + `job_runs.idempotency_key` 幂等），
  复用 `run_collectors`，并保证单源失败不影响他源（该点已由 runner 保证）。
- **验收标准**：`job_runs` 幂等键冲突不重复执行；连续多轮不产生重复数据。

### TD-10 Dashboard / API 未实现（P1）

- **现状**：Phase 1 交付物中的「采集监控页面 / 原始数据查询页面 / 行情查询页面」均未实现
  （当前为纯数据 + 采集代码库，尚无 FastAPI 应用）。
- **解除条件**：按 `docs/02` 的分层提供只读 API（sources 状态、collector_runs、raw_items、market_bars）。

### TD-11 / TD-12 Processor 层与部分表未启用（P1/P2）

> **更新（2026-09-22，GOLD-002）**：TD-11 **已解除**。加工层已独立为
> `src/processors/collection/`（`contracts` / `normalize` / `identity` / `validate` / `pipeline`）：
>
> - **确定性四阶段流水线**：`normalize`（NFKC / 去零宽 / 折叠空白 / 超长截断）→
>   `timezone/effective_at`（统一 UTC，`effective_at = max(published_at, collected_at, 上游下界)`）→
>   `identity/dedup`（内容指纹 + 幂等键 `source_id + source_record_id + content_hash`）→
>   `validation/audit`（数据质量校验 + 元数据脱敏 → 白名单摘要）；
> - **append-only 幂等落库**：写 `processed_items(processor_name, processor_version, status,
>   normalized_text, language, structured_json, effective_at)`，`(raw_item_id, processor_name,
>   processor_version)` 唯一约束 + 落库前存在性检查 → 重复输入 / 重复运行新增 0 行；
>   **不新增 migration / schema**；
> - **不伪造时间**：`published_at` / `collected_at` / 上游 `effective_at` 都不可用时判
>   `REJECTED` 且 `persistable=False`（不写库），绝不使用"当前时间"冒充事实时间；
> - **坏数据隔离 + 诚实状态**：单条 `REJECTED` / `FAILED` 只影响自己，批次状态区分
>   `SUCCESS` / `PARTIAL_FAILED` / `FAILED`；摘要只含白名单字段，凭据统一由
>   `src/common/redaction.py` 擦除（`Bearer` 规则必须先于 `Authorization` 规则，
>   否则 `Authorization: Bearer xxx` 会残留真实 token——已由单测锁定）；
> - **采集侧最小接线**：`BaseCollector(post_processor=...)`（默认 `None`，零行为变化，
>   未接线时不会产生任何 `processed_items`）；参考实现 = RSS 采集路径
>   `python scripts/collect_rss.py --to-db`；生产工厂入口
>   `src/collectors/bootstrap.py::default_collector_factory(post_processor=...)`。
>   **Scheduler 核心（`src/scheduler/core.py`）不含任何 Processor 逻辑**（接线点评估结论：
>   由 bootstrap/CLI 注入，不进调度核心）；
> - **测试**：新增 **73 项**（GOLD-002 交付 72 项 + GOLD-002-R1 复核补的 1 项 URL 空值回归）：
>   `tests/unit/test_collection_processor.py` 35、
>   `tests/unit/test_redaction.py` 26、`tests/integration/test_collection_processor_persistence.py` 8、
>   `tests/integration/test_collection_processor_wiring.py` 4，全部 Mock、零网络；
>   覆盖确定性、时区边界、`effective_at`、重复输入/重复运行、坏数据隔离、敏感字段过滤、空批次。
>
> TD-12 据此**收紧为"部分解除"**：`processed_items`（采集后处理 + 观点管道）、
> `audit_logs`（作者库仓储）、`data_versions`（`src/features/market.py`、`src/alpha/regime.py`）
> 均已具备写入路径；**仍未完成**的是"所有数据集都建立 `data_versions` 快照"这一更严口径，
> 以及 `processed_items` 缺少 `error_message` 列（见 TD-19）。
>
> 原始冻结描述（保留审计历史）：`processed_items`（加工结果，append-only）原本无写入路径；
> `data_versions` / `audit_logs` 同样只建表未使用；normalize / dedup / timezone 处理内嵌在
> 采集器内（职责边界尚可，但不满足 `docs/02` 的 Processor 分层）。
>
> **更新（2026-09-22，GOLD-003）**：接线范围扩展到**常驻采集入口**——
> `scripts/run_collector_scheduler.py --with-processor`（**默认关闭**，不传即零行为差异）
> 把现有 `CollectionProcessor` 经 `default_collector_factory(post_processor=...)` 注入采集器，
> 使 30 分钟采集完成 `raw_items → processed_items` 闭环；单源 Processor 故障按源隔离
> （原始数据不丢、其它源照常调度），故障告警在**写日志与写 `job_runs.output_json.warnings` 前**
> 统一经 `src/common/redaction.py` 脱敏；`src/scheduler/core.py` 仍**零** Processor 逻辑。
> TD-11 维持「已解除」；**TD-12 维持「部分解除」**——剩余缺口仍是「所有数据集建立
> `data_versions` 快照」与 `processed_items` 缺 `error_message` 列（TD-19），
> 本任务**不**宣称 TD-12 全部解除。

### TD-13 `raw_media` 无下载器（P2）

- **现状**：表与模型齐备（含 `sha256`、`storage_uri`），但没有图片下载/存储实现；
  `storage_uri` 是 `ALLOWED_UPDATE_COLUMNS` 中唯一允许更新的列。

### TD-14 无依赖 lock 文件（P2）

- **现状**：`pyproject.toml` 只声明下界（`sqlalchemy>=2.0.30` 等）。
- **影响**：不同时间安装的开发环境可能出现行为漂移（例如 ruff/mypy 新规则让 CI 变红）。
- **解除条件**：引入 `uv.lock` 或 `requirements.lock`，并在 CI 使用锁定安装。

### TD-15 仓库未初始化 Git（P2）

- **现状**：`git status` 返回 `fatal: not a git repository`。**当前所有变更没有版本历史**。
- **风险**：无法回溯"某次迁移 / 某行代码何时引入"，与 `docs/03` 的代码版本可追溯原则冲突；
  Phase 2 起表结构会继续演进，风险放大。
- **解除条件**：`git init` + 首次提交（`.gitignore` 已就绪，可立即执行，非代码任务）。

### TD-16 作者库 CRUD 与首期作者导入未实现（P1）

- **现状**：Phase 1 范围第 1 项（作者库：作者 CRUD、平台账号、首期 50~100 作者、
  ACTIVE/DISABLED 状态）**只建了表**（`authors` / `author_accounts`，含
  `(source_id, external_account_id)` 唯一约束），**没有任何写入代码**：
  `authors` / `author_accounts` 行数为 0（见 `docs/09` 报告第 3 节）。
  Phase 2 的四张表（`author_opinions` 等）已由 migration 0005 建好，但**作者身份为空**
  意味着观点无法归属 → 本项是 Phase 2 第二步的**唯一卡点**。
- **团队裁决（Phase 2 开工前置）**：**不采集微博**；作者来源改为
  「机构 / 分析师 RSS 账号」或「本地 CSV 模拟导入」，先把 Author Lab 管道跑通。
- **影响**：Phase 2「Author Lab」的一切（观点归属、作者技能、独立性）都建立在作者身份之上，
  没有作者数据就无法开始；`raw_items` → `author_posts` 的归属链路也无法端到端验证。
- **解除条件**：
  （a）实现 `AuthorRepository` / `AuthorAccountRepository`（写 `audit_logs` 留痕）；
  （b）提供 50~100 位作者的导入方式（YAML/CSV 导入器 + `python -m database.seeds --scope authors`，
  与现有种子同一幂等口径：自然键 = `canonical_name` / `(source_id, external_account_id)`，
  已存在记录一律不覆盖）；
  （c）`author_accounts.last_collected_at` 由采集器在归属成功后回填。
- **验收标准**：导入重复执行不产生重复行；`enabled=false` 的账号不被采集；
  新增作者不改变已有 `raw_items`（原始层不可覆盖）。
- **✅ 状态（2026-09-12 周末第二轮）**：解除条件 (a) 与 (b) 已交付并可运行：
  - (a) `database/repositories/authors.py`（`AuthorRepository` / `AuthorAccountRepository`）
    + `database/repositories/audit.py`（每次管理性修改写 `audit_logs`，含 before/after 快照）；
  - (b) `database/seeds/authors.py`（`AuthorSeed` + 3 条内置 Mock 作者 + CSV 解析/导入）
    + `python -m database.seeds --scope authors [--authors-csv PATH]`（幂等；不覆盖既有运维配置）；
  - (c) 仍待采集器接入（TD-20 / TD-06）。
  **剩余（非代码）**：首期 50~100 位真实作者名单与账号 ID（需业务方提供 CSV，
  格式见 `database/seeds/authors.py::CSV_COLUMNS`）。
  验收证据：`tests/unit/test_author_seeds.py`（20）、`tests/integration/test_author_repository.py`（22）、
  `tests/integration/test_author_import.py`（16）+ 真实 CLI 冒烟（`PROGRESS_LOG.md` 收工验证节）。

---

### TD-17 传播去重的规模与链式合并限制（P2）

- **现状**：`src/processors/propagation.py` 在**同桶内**做两两比较（100 条 = 4950 对，秒级；
  已用 `max_pairs` 硬门禁，超限直接报错而非静默截断）。连通分量会把
  A≈B、B≈C 但 A≉C 的情况合并为同一簇（"链式合并"）。
- **影响**：传播簇可能偏大 → 独立观点数偏**保守**（少算），不会把转载当成独立观点（符合安全方向）；
  但大队列（如 1000+ 条）会产生 O(n²) 条边，DB 行数增长快。
- **解除条件**：改用 SimHash / 倒排索引 / 向量检索做候选召回，再对候选做精确比较；
  若要记录"星型拓扑 + 链式证据"，需要为新列写新 migration（schema 当前冻结）。
- **验收标准**：1000 条同文队列的建边耗时 < 10s、边数 = O(n)（星型）且独立观点数仍为 1。

---

### TD-18 分词升级路径未启用（P2）

- **现状**：默认切分策略是零依赖的 `cjk-ngram-v1`（CJK 字符 2-gram + ASCII 词）；
  `jieba` 路径已实现但需显式 `use_jieba=True`，且**当前环境未安装 jieba**（TD-21）。
- **影响**：中文长句的语义区分度弱于分词方案（阈值护栏测试显示：同文 1.000、噪声转载 ≈0.82、
  反向观点 ≈0.60，当前阈值 0.80 可用但余量有限）。
- **解除条件**：安装 jieba → 跑一遍阈值标定 → 以新的 `model_version` 重算传播边（禁止改写旧边）。
- **验收标准**：切换后 `tests/unit/test_text_similarity.py` 的阈值护栏测试仍全绿（必要时重新标定阈值）。

---

### TD-19 `processed_items` 无 `error_message` 列（P2）

- **现状**：Phase 1 冻结结构中 `processed_items` 没有错误列；观点管道把抽取失败详情
  写入 `structured_json`（`{"exception": ..., "error_message": ...}`），状态置 `FAILED`。
- **影响**：无法用纯 SQL 直接按错误文本过滤（需要 JSON 提取函数）；失败统计需读 `structured_json`。
- **解除条件**：若确需 SQL 级巡检，新增 migration 增加列（禁止修改已发布迁移）。
- **验收标准**：`docs/04` 表注记同步更新，并新增回归测试锁定新列的写入路径。

---

### TD-20 观点管道缺 CLI / Scheduler 与归属链路（P1）

- **现状**：`OpinionPipeline` 是可注入的库级组件，已有完整 Mock 测试，但**没有 CLI 入口**；
  `raw_items` → `author_posts` 的归属（采集器写入）尚未实现（TD-06 合规前提）；Scheduler（TD-09）未实现。
- **影响**：目前只能在脚本/测试中调用管道，无法按 30 分钟调度周期自动运行。
- **解除条件**：新增 `scripts/` 或 `src/pipeline` 级 CLI（复用 `job_runs` 幂等键）；
  归属链路随采集器（RSS/CSV 来源）实现；接入 Scheduler。
- **验收标准**：同一调度窗口重复触发只执行一次（`job_runs.idempotency_key` 唯一）；
  管道重跑幂等（已有测试保证）。

---

### TD-21 已批准依赖在本机未安装成功（P1）

- **现状**：`pyproject.toml` 已声明团队批准的 Phase 2 依赖（`numpy` / `pandas` /
  `scikit-learn` / `httpx` / `jieba`），但本机 `pip install -e ".[dev]"` 因 **PyPI 网络受限**
  未完成；`pip` 进程被终止，**未修改依赖清单**。
- **影响**：相似度用零依赖实现兜底（TD-18）；标注一致性统计（Cohen's Kappa / ECE）、
  pandas 版标注比对脚本等**尚不能运行**（属于周一人工标注之后的步骤）。
- **解除条件**：网络可达后执行 `.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"`，
  并用 `python -c "import numpy, pandas, sklearn, httpx, jieba"` 验证。
- **验收标准**：上述导入全部成功；相似度/统计脚本可在不改变现有测试基线的前提下启用。
- **✅ 状态（2026-09-13）**：用户已完成 `pip install -e ".[dev]"`，依赖全部就位。
  连带效果：`tests/unit/test_text_similarity.py` 中"jieba 未安装"的条件分支由 pass 变为
  **skip**（`796 passed + 1 skipped`）——这是**正常现象**，不是回归。
  剩余可选项：跑一次 jieba 阈值标定并按新 `model_version` 重算传播边（TD-18）。

---

### TD-22 历史明文密钥处置（✅ 已解除，2026-09-18）

- **历史问题**：根目录曾有未受忽略规则保护的明文 DeepSeek Key 与数据库 URL 副本，按已泄漏处理。
- **工程处置**：原文件已删除；密钥只从本地 `.env` 读取；`.env` 与 `.postgres-local/` 均被
  Git 忽略；受控扫描未发现新的真实凭据；数据库 URL 的控制台输出已强制脱敏并有回归测试。
- **外部处置**：用户于 2026-09-18 确认旧 DeepSeek / 历史数据库凭据均已在供应商侧轮换失效；
  本地 PostgreSQL 项目角色密码也已在端到端验收时单独轮换。
- **结论**：文件删除、Git 忽略、扫描、输出脱敏与供应商侧轮换四项均完成，P0 关闭。
---


### TD-24 Prompt few-shot 与评测语料逐字重叠（✅ 已解除，2026-09-13）

- **原状**：`opinion-prompt-v1` 的 6 条 few-shot 里有 4 条的输入句**逐字**出现在评测语料中
  （条件句模板 15 条、引用/复盘/弱化各 1 条 → 18/200 条"模型见过答案"，20 条试点里 4 条被污染）。
- **修复**：`opinion-prompt-v2` 起 12 条 few-shot **全部重写为语料外自造句**（措辞、价位、场景都不同），
  实测**最长公共子串 ≤ 9 字符**（无整句/整分句重合）。
- **防复发**：新增 `tests/unit/test_prompt_opinion.py::test_few_shot_has_no_overlap_with_evaluation_corpus`
  ——用**最长公共子串**（6-gram 预筛 + DP）机械拦截，阈值 12 字符；语料缺失（`logs/` 不入库）时 skip。
- **审计工具**：`logs/_llm_contamination_check.py`（零 API 调用）、`logs/_pilot_compare.py`
  （对比文档生成，含 few-shot 重叠复核）。

---

### TD-25 `information_type` 判定偏保守（✅ 已解除，2026-09-13；残余 1 格见 TD-27）

- **原状**：v1 在 20 条试点的人工子集 13 格里错 7 格（46.2%），且 7 格**全部**是
  `gold=SENTIMENT/POSITIONING/MACRO/OTHER` ↔ `LLM=TECHNICAL`（看到数字就判技术面）。
- **口径（人工裁决，已写入 `docs/10 §4.9` 规则 9）**：**"操作优先，驱动决定分类"**——
  L1 持仓/资金流 → `POSITIONING`；L2 宏观 → `MACRO`；L3 情绪 → `SENTIMENT`；
  L4 引用/复盘 → `OTHER`；L5 无驱动时的操作/纯图表 → `TECHNICAL`；**L1~L4 优先于 L5**。
  L3 的判别线（v5）：情绪须是**主旨**或**语句本身带方向含义**；**中性盘面观察**（"多空分歧较大"）
  不算驱动 → 落 L5。
- **结果**：`opinion-prompt-v5` 下人工子集 **12/13 = 92.3%**；100 格 `wrong_value` **8 → 1**；
  `mock-post-0015/0025`（ETF 持仓）、`0027`（美债）、`0007/0023`（情绪）、`0019`（复盘）
  全部从"提取错误"变"命中"。
- **验证**：`docs/experiments/Phase2_LLM试点_v1-v5对比.md`（同一批 20 条、5 个版本对照）。

---

### TD-26 `no_opinion` 与 `docs/10 §4.9` 规则 4 冲突（✅ 已解除，2026-09-13）

- **原状**：宏观数据播报（CPI 等）被判"无观点"（`opinions=[]`），而按 `§4.9` 应输出
  `stance=UNKNOWN + information_type=MACRO`；**v1 的 few-shot 第 6 例本身把这个教错了**。
- **口径（人工二次裁决）**：`no_opinion=true` = "**没有可交易的方向性观点**"，
  允许与"1 条 `UNKNOWN` 观点"并存；`opinions=[]` 只用于"连信息类型线索都没有"的帖子。
  已同步写入 `docs/10 §4.9` 规则 4、`§5 契约细节 #2` 与 §9 示例 B-3。
- **结果**：20 条试点**无观点率 10% → 0%**；`mock-post-0006/0022` 的 `stance`/`information_type`
  由"漏判"变"命中"。

---

### TD-27 Prompt 残余 1 格 + `horizon` 口径冲突（✅ 已解除，2026-09-13）

- **人工最终裁决（2026-09-13）**：
  1. `mock-post-0009` 的 `information_type` **人工金标准改判为 `TECHNICAL`**——理由："操作优先"，
     文本既有明确入场/止损/目标价，即使提及情绪背景也判 `TECHNICAL`（**模型纠正人工**，
     属正常对齐过程）。已同步：`docs/10 §4.9` 规则 9 的 **L3 操作价位优先**。
  2. `docs/10 §4.2` **补充映射「短线思路 / 短线观望 → `15m`」**，人工金标准保留 `15M`
     （模型留空/判 `1d` 均计未命中）；已在 Prompt 规则 8 落实。
- **落地证据**：`logs/ground_truth_200.csv` 该格已改（`reviewer`/`provenance`/`note` 留痕，
  改前版本归档 `logs/archive/ground_truth_200_pre_20260913.csv`）；Prompt 升
  `opinion-prompt-v6` / `parser_version=llm-deepseek-v6`。
- **验收结果**：20 条试点五字段 **100%**；全量 200 条人工子集
  `horizon` 0% → **100%**、`stop_loss` 0% → **100%**、`take_profit` 25% → **100%**、
  `stance` 100% 持平、核心四字段 **27.8% → 100%**。

---



### TD-28 `information_type` 残余误差：驱动型文本 + 操作价位（✅ 已按"路径 B"落地，2026-09-13）

- **人工裁决（2026-09-13）**：走**路径 B**——阶梯补 **`L2.5` 消息/事件驱动 → `NEWS`**，
  并把 L3 细化为"**操作价位优先，但若情绪/消息是文本给出的交易理由（"…因此做多/做空"）
  则按驱动分类；仅背景/附注式（"关注 risk-on 情绪"）才判 `TECHNICAL`**"，判别线只作用于情绪/消息。
  已写入 `docs/10 §4.9` 规则 9 与 `§4.5`（后者旧优先级 `MACRO > NEWS > POSITIONING > …`
  与二次裁决冲突，已标注**以 §4.9 规则 9 为准**）。
- **落地实现**：`opinion-prompt-v7 → v13`（见下方迭代记录），`parser_version=llm-deepseek-v13`，
  few-shot 16 条（新增"情绪是交易理由 → `SENTIMENT`""消息是波动来源 → `NEWS`"
  "操作 + 关注<宏观变量> → `MACRO`"三条，全部语料外自造句，LCS 最长重叠 ≤ 9 字符）。
- **验收结果（全量 200 条、人工子集口径）**：

| 版本 | 信息类型 | 核心四字段 | 说明 |
|---|---|---|---|
| v6 | 87.4%（错 17） | 100%（PASS） | NEWS 级缺失 + 情绪理由被判 TECHNICAL |
| v11 | 88.1%（错 16） | **98.9%（horizon 回归 FAIL）** | 阶梯修好 8 格 NEWS，但改了 horizon 规则导致 10 格周期判错 |
| **v13（定版）** | **91.9%（错 11）** | **100%（全 PASS）** | = v6 的 horizon 规则 + v11/v12 的阶梯 |

- **残余 11 格（登记 TD-29）**：`SENTIMENT→TECHNICAL` 7 格、`MACRO→TECHNICAL` 4 格，
  全部是 **"操作块 + 驱动尾句"** 这一形态。

---

### TD-29 "操作块 + 驱动尾句"的 `information_type` 边界（🔒 已冻结为已知噪音，2026-09-13）

- **现状**：全量 200 条定版后，人工子集仍有 **11 格** `information_type` 不一致
  （91.9%），全部呈现为"**操作块（入场/止损/目标）+ 驱动尾句**"：
  - `mock-post-0027`「…日内交易. 关注美债收益率回落，支撑金价估值」→ 人工 `MACRO`，模型 `TECHNICAL`；
  - `mock-post-0079/0109/0139`「【美盘深度】…（完整点位）…」→ 人工 `SENTIMENT`，模型 `TECHNICAL`；
  - `mock-post-0119`「XAUUSD 2418 这个位置，现在还有多少人愿意进场？反正我是不着急」→ 人工 `SENTIMENT`；
  - `mock-post-0127`「…超短线，分钟级操作」→ 人工 `MACRO`，模型 `TECHNICAL`；
  - 另有 `mock-post-0126` 的 `horizon` 一格（人工"未给出"、模型 `1D`）。
- **根因（口径边界，非模型能力）**：同一模板家族里，人工对尾句的处理并不一致——
  `0027` 判 `MACRO`（把"关注美债"当驱动）、`0009` 判 `TECHNICAL`（把"关注 risk-on"当背景），
  两者结构几乎相同。**继续对着单条 Mock 模板调 Prompt 只会过拟合**（v7→v11 已验证会出现
  "修好 A 就坏 B"的反复）。
- **解除条件**：用**真实作者帖子**重新抽样并全人工裁决该边界（`docs/08 §5`），
  在真实语料上确认"尾句是否算驱动"的统一规则后，再定 Prompt v14。
- **验收标准**：真实语料上 `information_type` 人工子集 ≥ 90%，且同类模板的裁决**内部一致**
  （同一"操作块 + 尾句"结构不再出现两种人工结论）。
- **纪律（🔒 2026-09-13 人工最终裁决：冻结）**：**不改**这 11 格 Mock 金标准、**不再**为它们调 Prompt；
  以 `docs/10 §4.9.0` **最高优先级抽象原则**为唯一判据
  （驱动句是因果连接 → 按驱动分类；驱动句只是背景/附注 → 按操作分类）。
  这 11 格转为**真实语料标注的校准案例**（逐格列表：`docs/11_Phase2真实语料验收指南.md` §3.4）。
  Mock 验收报告需披露该噪音（信息类型 91.9% 中的 7 格 `SENTIMENT→TECHNICAL`、4 格 `MACRO→TECHNICAL`）。
- **解冻条件（仅在真实语料上）**：若真实语料同类结构再次大量出现，判为**真实世界口径问题**，
  按 `docs/11 §3.3` 工作流重新裁决并同步 `docs/10`（届时才允许动 Prompt，升 `v14`）。

---

### TD-46 采集运行健康度 / Phase 3.3 数据资格观测的剩余边界（P2，2026-09-22）

- **现状（GOLD-004 已交付）**：`src/monitoring/` 提供**只读**聚合（`collector_health` /
  `phase33_qualification` / `report`），CLI 为 `scripts/report_collector_health.py`
  （默认人类可读；`--json` 稳定机器可读；`--report` 需配 `--no-dry-run` 才落盘）。
- **已覆盖**：source 级运行次数 / SUCCESS-PARTIAL-FAILED-在途 / 连败 / 陈旧 /
  `raw_items` 实际条数 / `inserted`-`duplicate` 上报值 / `processed_items`
  成功-拒绝-失败 / 加工观测状态，以及 Phase 3.3 资格缺口（当前值、要求值、比较方式、
  PASS/BLOCKED、原因、证据时间范围），并持续显式输出 `PHASE3_3_DATA`。
- **剩余（本条目跟踪）**：
  1. **无告警推送**：只有拉取式报告，没有阈值告警通道（邮件 / webhook）——观测层刻意不做副作用；
  2. **无时间序列**：报告是窗口快照，未落库为趋势指标（Dashboard 属 TD-10）；
  3. **拒绝原因不可细分**：`processed_items.status` 只区分 `SUCCESS/SKIPPED/FAILED`，
     `SKIPPED` 同时涵盖 `DUPLICATE` 与 `REJECTED`（映射见
     `src/processors/collection/contracts.py`）；需要细分时读 `structured_json.outcome`
     （当前不做 SQL 级 JSON 抽取，以保持 SQLite / PostgreSQL 可移植）或新增列
     （属 schema 变更，需 migration 并与本条目同步）。
- **解除条件**：出现明确运维 / 巡检需求（或 Dashboard 落地）时，设计告警与趋势存储，
  并同步 `docs/03` / `docs/04`。
- **不变量**：观测层永远只读；不得因观测结果 PASS 而解除 `PHASE3_3_DATA`。

---

### TD-47 授权证据接收入口的剩余边界（P0，2026-09-22，GOLD-005）

- **已交付**：`src/evidence/`（`contracts` / `validation` / `intake` / `report` / `ledger`）
  与 CLI `scripts/intake_evidence.py`，输入契约 `evidence-intake-v1`
  （Author / News 两类；字段组：来源身份、内容/内容引用、`published_at` / `collected_at`、
  出处引用、授权声明 + 三项许可 + 人工签认、可选历史可用证据 `available_at`）。
- **关键保证（已由测试锁定）**：
  1. 默认 dry-run：不写库、不建 `sources`、不落 manifest / quarantine；
  2. 授权不可推断：缺失 / `UNKNOWN` / `DENIED` 一律 `AUTHORIZATION_MISSING` 隔离；
  3. 时间不可伪造：naive / 未来 / `collected_at <= published_at` 一律隔离，
     `ingested_at` 由系统赋值（输入值忽略）；
  4. 历史 CSV 不具 OOS 资格：无独立 `available_at` 证据 → `AVAILABILITY_UNPROVEN`
     → `NOT_OOS_ELIGIBLE`（记录仍落库，但不计入 OOS 证据）；
  5. 幂等且不覆盖：同 `(source, source_record_id)` 同内容 → `DUPLICATE`；
     内容不同 → `IDENTITY_CONFLICT` 隔离（绝不 UPDATE 历史事实）；
  6. 凭据防护：凭据类列/值整行隔离（`SENSITIVE_VALUE_DETECTED`），
     报告 / manifest / quarantine / `raw_json` 都不含凭据值。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 0 条经入口认证的记录 → `PHASE3_3_DATA` 保持 `active=true`；
     TD-43 / TD-44 / TD-45 未解除，任何代码或 Mock 完成都不会解除；
  2. **不接通作者链**：入口只写 `raw_items`（`item_type=POST`）+ `processed_items`，
     不创建 `authors` / `author_accounts` / `author_posts`；因此 Author Alpha 的可信标签
     仍依赖真实数据 + 人工标注 + 既有 `check_phase3_3_author_input.py` 门禁；
  3. **证据真实性属人工**：程序只校验声明与证据字段是否齐全、时间是否自洽，
     不判断许可 URL 的法律效力、提供方档案的真实性或签认人身份；
  4. **无告警**：`evidence_intake` 只在 `report_collector_health` 报告里展示，
     没有阈值告警通道（与 TD-46 同源）。
- **解除条件**：业务方按契约提交**已授权**数据，经人工核验后由入口提交落库，
  且达到 `src/alpha/evidence_gate.py` 的数量/跨度/集中度门槛；届时才讨论解除 blocker。
- **不变量**：入口永不联网、永不抓取、永不绕过 robots / 条款 / 证书；
  永不因代码完成或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-48 证据就绪度 / 一键资格复核的剩余边界（P0，2026-09-22，GOLD-006）

- **已交付**：`src/evidence/templates.py`（Author / News operator 模板 + `SYNTHETIC_EVIDENCE`
  标记识别）、`src/monitoring/evidence_readiness.py`（只读就绪度 / preflight）、
  `scripts/evidence_readiness.py`（`template` / `preflight` / `recheck` 三个子命令）、
  `examples/evidence/{author,news}_evidence_template.csv`。
- **关键保证（已由测试锁定）**：
  1. 模板示例行带 `record_kind=example` / `is_mock=true`，导入判
     `SYNTHETIC_EVIDENCE` 隔离，**不写库、不计入 qualification ledger**；
     即使删掉标记列，示例行的 `authorization_status=PENDING` / `permits_*=false` /
     非法时间仍会被隔离；
  2. `preflight` / `recheck` 默认只读、零网络、零写入（`--report` 必须配 `--no-dry-run`）；
  3. 阈值完全复用 `src/alpha/evidence_gate.py`（30 / 200 / 90 天 / 单源 40%），
     只报告当前值、阈值与 remaining gap，不修改任何阈值与授权 / 时间 / availability 规则；
  4. `blocker_active` 与 `human_gate_required` 恒为 `true`：量化达标（如 News 三项全 PASS）
     也不解除 `PHASE3_3_DATA`；
  5. 报告只输出白名单标量：不读取 `sources.config_json`、不输出正文，
     来源名过 `safe_text`，token / API key / Authorization 一律 `***`。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 0 条经入口认证的 eligible 记录 →
     `PHASE3_3_DATA` 保持 `active=true`（TD-43 / TD-44 / TD-45 / TD-47 未解除）；
  2. **不产生真实证据**：模板、示例、Mock、历史 CSV 都不能用于达标，工具只量化缺口；
  3. **口径范围**：readiness 只覆盖证据入口台账（Author 可信 eligible 帖子数；
     News 条数 / 覆盖天数 / 单源占比），不含作者链（`authors` / `author_posts`）落库与人工标注；
  4. **无告警**：没有阈值告警推送（与 TD-46 同源）。
- **解除条件**：业务方按 `evidence-intake-v1` 契约提交**已授权**数据并经人工核验落库，
  再由 `scripts/evidence_readiness.py recheck` 显示量化门槛达标；届时才讨论解除 blocker。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书；
  永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-49 单入口 Evidence Operator 工作流与 gateway-only 作者归属链的剩余边界（P0，2026-09-22，GOLD-007）

- **已交付**：`src/evidence/workflow.py`（写入门禁 + 脱敏隔离摘要，纯函数）、
  `scripts/evidence_operator.py`（单入口 `workflow` + `template` / `preflight` /
  `quarantine` / `intake` / `recheck` / `author-chain`）、
  `src/evidence/author_chain.py`（gateway-only 作者归属链）。
- **关键保证（已由测试锁定）**：
  1. 单入口 `workflow` 一次串联 `template → preflight → quarantine → intake → recheck`，
     默认 dry-run、零网络、零写入；`--report` / `--manifest` / `--quarantine` 必须显式配
     `--no-dry-run`；
  2. **写入前二次验证**：`intake` / `workflow --no-dry-run` 先跑一次完整 dry-run
     Evidence Gateway 校验并汇总写入门禁（`accepted` / `quarantined` / `duplicate` /
     `conflict` / `not_oos_eligible` / `synthetic` / `authorization_incomplete` /
     `time_invalid` / `identity_issues` / `sensitive_detected` / `availability_unproven`），
     只有 `ACCEPTED` 行 append-only 落库；未授权 / 合成示例 / 时间非法 / 身份冲突 / 凭据 /
     坏行一律隔离，**绝不**进入 qualification ledger；
  3. `IDENTITY_CONFLICT` 拒绝覆盖历史事实（稳定原因码，不静默改写），重复显式 intake 只产
     `DUPLICATE`（原始层与台账不增长）；
  4. **作者链 gateway-only**：只消费 `raw_json.evidence` 带 `evidence-intake-v1` +
     `scope=author` + `oos_eligible=true` + 作者身份齐全的记录；`import_real_posts.py`
     的普通 CSV 载荷没有证据块 → 恒不是候选，**无法绕过** gateway；新建
     `author_accounts` 一律 `enabled=false`（不启用采集）；同一 `raw_item` 只归属一次；
     身份冲突（账号已归属其他作者）跳过且不覆盖；
  5. 阈值仍完全复用 `src/alpha/evidence_gate.py`（30 / 200 / 90 天 / 单源 40%），
     `blocker_active` 与 `human_gate_required` 恒为 `true`。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 eligible 记录不足以达标 → `PHASE3_3_DATA` 保持
     `active=true`（TD-43 / TD-44 / TD-45 / TD-47 / TD-48 未解除）；
  2. **历史 CSV 口径不复用**：`scripts/import_real_posts.py` 仍是 W0-4 / 演练用普通 CSV 入口，
     其 `raw_json` 只写 `import_kind=manual_real_corpus`，既不计入资格台账、也不会被作者链消费；
     未新增 migration（沿用既有 `authors` / `author_accounts` / `author_posts` schema）；
  3. **只建立归属**：作者链不做观点抽取 / 人工标注，也未接入任何采集；
  4. **无告警**：没有缺口阈值告警推送（与 TD-46 同源）。
- **解除条件**：业务方按 `evidence-intake-v1` 契约提交**已授权** Author / News 数据并经
  人工核验落库（可用 `scripts.evidence_operator workflow --no-dry-run`），再由
  `scripts.evidence_operator recheck` / `scripts.evidence_readiness.py recheck` 显示量化门槛达标；
  届时才讨论解除 blocker。
- **不变量**：工作流永不联网、永不抓取、永不绕过 robots / 条款 / 证书；
  永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-50 Evidence 人工交接包的剩余边界（P0，2026-09-22，GOLD-008）

- **已交付**：`src/evidence/handoff.py`（只读交接包：`ScopeGap` / `ChecklistItem` /
  `ExcludedEvidence` / `EvidenceHandoffReport` + 稳定 JSON + Markdown 渲染）、
  `scripts/evidence_handoff.py`（默认只读、默认零写入、默认零网络的 CLI）。
- **关键保证（已由测试锁定）**：
  1. **复用唯一口径**：缺口 / 阈值完全来自现有 `src.monitoring.evidence_readiness`
     （阈值来自 `src.alpha.evidence_gate`：30 / 200 / 90 天 / 单源 40%），
     **不复制第二套阈值算法**，也不修改任何阈值；
  2. **只读 / 零网络 / 零写入**：CLI 不抓取站点、不绕过 robots / 条款 / 证书；
     `--input` 只做 dry-run（显式回滚，零落库），**没有** `--no-dry-run`，不存在自动 intake；
     不传 `--out` 时只打印 stdout；`--out` 拒绝指向输入文件；
  3. **诚实 blocker 状态**：`blocker_active` / `human_gate_required` 恒为 true，
     `data_qualification_passed` / `phase_transition_allowed` 恒为 false；
     未达标时 `status=BLOCKED` / `ready_for_human_review=false`，
     退出码为 `5`（BLOCKED）；量化达标时最多 `status=BLOCKED_PENDING_HUMAN_REVIEW` /
     `ready_for_human_review=true`，退出码 `0`，**仍不**自动切 Phase；
  4. **可量化交接**：Author / News 的 `eligible` / `required` / `remaining`、coverage gap、
     `source_share_evaluable`（无 OOS eligible 记录时不得判 PASS）与主要隔离原因码计数
     （候选文件 dry-run 时给出）；
  5. **明确不计资格**：模板 / 示例（`SYNTHETIC_EVIDENCE`）、Mock / 演练语料、
     普通历史 CSV（`NOT_CERTIFIED`）、缺独立 `available_at`（`AVAILABILITY_UNPROVEN`）
     一律 `counts_toward_eligibility=false`；
  6. **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名 / 时间，
     token / API key / Authorization 一律 `***`，正文与输入额外列值不进入输出。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 eligible 记录不足以达标 → `PHASE3_3_DATA` 保持
     `active=true`（TD-43 / TD-44 / TD-45 / TD-47 / TD-48 / TD-49 未解除）；
  2. **只减少人工交接摩擦**：本工具是**交接包**，不是资格判定器；量化达标也必须经
     `.ai/DEVELOPMENT_PROTOCOL.md` 的 L3 人工确认才能改变 blocker / Phase；
  3. **不产生真实证据**：模板、示例、Mock、历史 CSV 都不能用于达标；
  4. **无告警**：没有缺口阈值告警推送（与 TD-46 同源）。
- **解除条件**：业务方按 `evidence-intake-v1` 契约提交**已授权** Author / News 数据并经
  人工核验落库（可用 `scripts.evidence_operator workflow --no-dry-run`），再由
  `scripts.evidence_handoff` / `scripts.evidence_readiness.py recheck` 显示量化门槛达标，
  且完成 **L3 人工 Gate**；届时才讨论解除 blocker。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书；
  永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-51 Evidence Readiness 状态变更通知的剩余边界（P0，2026-09-22，GOLD-009）

- **已交付**：`src/evidence/readiness_watch.py`（只读通知核心：`ReadinessSnapshot` /
  `ScopeSnapshot` / `WatchEvent` / `detect_changes` + 原子写
  `write_snapshot_state` / `write_events` / `load_snapshot_state`）、
  `scripts/evidence_readiness_watch.py`（默认只读、默认零写入、默认零网络的 CLI）。
- **关键保证（已由测试锁定）**：
  1. **复用唯一口径**：快照只消费 `EvidenceHandoffReport`（缺口 / 阈值同源于
     `src.monitoring.evidence_readiness` 与 `src.alpha.evidence_gate`：30 / 200 / 90 天 /
     单源 40%），**不复制第二套阈值算法**，也不修改任何阈值或人工 Gate；
  2. **确定性指纹**：只基于脱敏白名单字段（`status` / `ready_for_human_review` /
     各 scope `eligible` / `required` / `remaining` / coverage 缺口 /
     `source_share_evaluable` / 稳定原因码）计算 SHA-256；scope 与原因码全部排序，
     **`generated_at` 不参与指纹** → 完全相同状态重复运行产生 **0 事件**（幂等）；
  3. **有意义变化才告警**：首次快照、BLOCKED 缺口变化、原因码集合变化、
     `ready_for_human_review` false→true / true→false；事件顺序由 `EVENT_ORDER` 固定；
  4. **默认只读 / 零网络 / 零写入**：`--state` 只读；只有显式 `--out` / `--events` 才写文件，
     且均为**原子写**（同目录临时文件 + `fsync` + `os.replace`，无半写状态、无残留临时文件）；
     `--input` 只做 dry-run（显式回滚，零落库），**没有** `--no-dry-run`；
  5. **安全失败**：state 为空 / JSON 损坏 / schema 不符 / 指纹校验失败 / 声称 blocker 已解除
     一律退出码 `4` 且**不写任何输出**（不把坏状态静默当作首次快照）；
  6. **诚实与脱敏**：`blocker_active` / `human_gate_required` 恒为 true，
     `data_qualification_passed` / `phase_transition_allowed` 恒为 false；
     原因码与事件消息统一过 `src.common.redaction.safe_text`，不含正文、凭据或完整 source config。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 eligible 记录不足以达标 → `PHASE3_3_DATA` 保持
     `active=true`（TD-43 / TD-44 / TD-45 / TD-47 / TD-48 / TD-49 / TD-50 未解除）；
  2. **只减少盯盘 / 轮询**：通知层不是资格判定器，也不具备解除 blocker 的能力；
     量化达标最多 `ready_for_human_review=true`，Phase 切换仍须 L3 人工确认；
  3. **纯本地出口**：事件只落 stdout 或显式指定的本地 JSON 文件，
     **不接**邮件 / 短信 / Webhook / 第三方推送，也不写数据库（无 migration / schema 变更）；
  4. **不产生真实证据**：模板、示例、Mock、历史 CSV 都不能用于达标。
- **解除条件**：业务方按 `evidence-intake-v1` 契约提交**已授权** Author / News 数据并经
  人工核验落库，`workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**；
  届时才讨论解除 blocker（通知层届时只负责把状态变化如实上报）。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不自动解除 blocker；
  永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-52 Evidence Readiness 周期 tick runner 的剩余边界（P0，2026-09-23，GOLD-010）

- **已交付**：`src/evidence/readiness_runner.py`（单次 tick 核心：`SingleInstanceLock` /
  `TickPaths` / `LockInfo` / `TickReport` / `run_tick` / `build_snapshot_from_session` /
  `load_event_journal` / `write_event_journal` / `write_status` / `render_tick_summary` /
  `exit_code_for`）+ `scripts/evidence_readiness_runner.py`（唯一写入口 = 显式 `--work-dir`
  的单次 tick CLI）。`src/evidence/readiness_watch.py` 的原子写原语改为公开
  `atomic_write_text`，供 runner 复用**同一**安全原语（不各写一套）。
- **关键保证（已由测试锁定）**：
  1. **不自带调度**：只跑一次 tick；模块内**没有任何循环**（源码守卫测试）；
     不引入 `schedule` / `apscheduler` / `croniter` / `subprocess` / `threading`；
     不自动修改 OS 计划任务（README 只给人工配置示例）；
  2. **单实例锁**：OS 级独占锁（Windows `msvcrt` / POSIX `fcntl`，零第三方依赖）；
     锁记录 `owner` / `pid` / `acquired_at`（不含敏感数据）；
     **绝不删除 / 绝不改写**活动锁 → 不可能"强删活动锁导致并发写 state"；
     锁冲突退出 `6` 且**零写入**；
  3. **陈旧锁安全接管**：崩溃进程遗留的锁文件（含内容不可解析）可被安全接管，
     新锁记录与 status 保留 `recovered_stale=true` + `previous_owner` 审计留痕；
  4. **零网络 / 零数据库写入**：`build_snapshot_from_session` 只读台账并显式 `rollback`；
     集成测试断言 `raw_items` / `sources` 行数与运行前一致；
  5. **口径完全复用 GOLD-009**：指纹 / 事件类型 / 缺口 / 阈值全部来自
     `src.evidence.readiness_watch` 与 `src.evidence.handoff`（阈值同源于
     `src.alpha.evidence_gate`），**不复制、不降低任何阈值算法**；
  6. **幂等 + 有界 + 事件优先**：无变化 → 0 事件且事件日志逐字节不变；
     事件日志保留最新 200 条、**本次新事件永不丢弃**（上限不足自动放宽）；
     先追加事件日志 → 再原子替换 state → 最后写 status（强杀最坏多一条待确认事件，
     **绝不**丢变化）；
  7. **fail-closed 与稳定退出码**：`4` state / 事件日志损坏或被篡改（含"声称 blocker
     已解除 / 资格已通过"）、`6` 锁冲突、`7` 资格计算失败、`3` 工作目录或 artifact 不可写、
     `5` **预期 BLOCKED**、`0` 量化达标（仍需 L3 人工 Gate）；失败路径**零写入**、
     保留旧 state、stdout 为空、stderr 已脱敏（不回显数据库连接串 / 凭据）。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：库内 eligible 记录不足以达标 → `PHASE3_3_DATA` 保持
     `active=true`（TD-43 / TD-44 / TD-45 / TD-47 ~ TD-51 未解除）；runner 只如实上报；
  2. **只把盯盘自动化**：runner 不是资格判定器，也不具备解除 blocker 的能力；
     量化达标最多 `ready_for_human_review=true`，Phase 切换仍须 L3 人工确认；
  3. **调度仍是人工责任**：README 给出 Windows 任务计划程序 / `schtasks` 调用方式，
     但工具不会安装或修改计划任务；退出码 `5` 需要包装脚本按"预期 BLOCKED"处理；
  4. **事件日志是本地有界留痕**，不是投递通道（不接邮件 / 短信 / Webhook / 第三方推送）；
     超出上限的旧事件按确定性规则滚动丢弃，丢弃条数记在 status 的 `journal.dropped`。
- **解除条件**：业务方按 `evidence-intake-v1` 契约提交**已授权** Author / News 数据并经
  人工核验落库，`workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不自动解除 blocker、
  永不自动修改 OS 计划任务；永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

---

### TD-53 Evidence 本地 Inbox 发现与预检的剩余边界（P0，2026-09-23，GOLD-011）

- **已交付**：`src/evidence/inbox.py`（`InboxStatus` / `InboxReasonCode` / `ManifestFileRef` /
  `CandidatePackage` / `PendingEntry` / `PendingRegister` / `InboxPreflightReport` /
  `scan_inbox` / `run_inbox_scan` / `load_pending_register` / `write_pending_register` /
  `render_inbox_summary` / `exit_code_for`）+ `scripts/evidence_inbox.py`
  （`--inbox-dir` 必填、`--state` 只读、`--out` 唯一写开关的**单次只读**扫描 CLI）。
  `src/evidence/validation.py` 的引用校验原语公开为 `valid_reference`，
  inbox 预检与 Evidence Gateway 复用**同一**口径。
- **关键保证（已由测试锁定）**：
  1. **只读发现**：只扫描显式 `--inbox-dir` 的**直接子目录**；**绝不**移动 / 重命名 / 删除 /
     改写 inbox 内任何文件（源码守卫禁止 `unlink` / `rmtree` / `os.remove` / `os.rename` /
     `shutil.move`）；零网络（不引入 `aiohttp` / `httpx` / `requests` / `socket` / `urllib`）、
     零数据库写入、零 migration/schema、零第三方依赖；
  2. **manifest 显式关联**：`manifest.json` 必填 `evidence_type` / `source`（或别名
     `provider`）/ `authorization_reference` / `time_semantics` / `availability_semantics` /
     `historical_oos_applicable` / `files`（`path` + `sha256`）；缺失 / 格式错误 /
     schema 或 contract 版本不符 / 证据类型不支持 → fail-closed + 稳定 `InboxReasonCode`；
  3. **内容 SHA-256 与确定性指纹**：逐文件实测 SHA-256 并与声明核对（不一致 → 隔离且
     **不再解析内容**）；候选包指纹只由文件内容摘要与结构标记派生，**不含** 文件名 / mtime /
     绝对路径 / 扫描时间（测试锁定：改 mtime、换审计时点都不改变指纹）；
  4. **幂等**：pending 清单是当前 inbox 内容的镜像（以内容指纹为键）；同内容重复扫描 →
     `discovered=0`、条目数与 `first_seen_at` 不变、仅 `scan_count` / `last_seen_at` 推进；
     内容变化 → 新指纹（新条目）；**同一指纹状态变化**（例如审计时点推进让"未来时间"变合法）
     记入 `status_changed`，绝不静默；pending 清单**原子写**（同目录临时文件 + `fsync` +
     `os.replace`，无残留 `.tmp`）；
  5. **安全拒绝**：绝对路径 / `..` / 多级路径 / 符号链接（文件与目录）/ 未声明文件一律
     fail-closed；越界文件**从未被读取**（只依据候选包目录内的内容摘要判定）；
     凭据类键名只记录键名、URL 引用去 query / userinfo 后再落盘 / 打印；
  6. **模板 / 示例 / Mock 永不计入**：manifest 显式标记（`is_mock` / `is_example` /
     `synthetic` / `record_kind=example`）或包名 / 文件名命中示例词表 →
     `SYNTHETIC_EVIDENCE` **整包隔离**；
  7. **复用 gateway 口径**：逐行预检调用 `src.evidence.validation.assess_row`
     （`evidence-intake-v1`），授权 / 时间 / availability / 隔离原因码与 `intake` 完全同源，
     **不复制、不降低**任何阈值；`DUPLICATE` / `IDENTITY_CONFLICT`（需要读库）留给显式 intake；
  8. **绝不自动 intake**：模块内不存在 intake / commit 调用；`preflight_pass` 只表示
     "可进入**人工确认 / 显式 intake** 队列"；`blocker_active` / `human_gate_required` 恒为
     true，`data_qualification_passed` / `phase_transition_allowed` 恒为 false，
     `requires_human_action` 恒为 true（**硬编码**，不被 manifest 或被篡改的 state 透传影响）；
  9. **fail-closed 与稳定退出码**：`4` 既有 pending state 损坏 / 被篡改、`3` inbox 目录或输出
     不可用（含把 `--out` 写进 inbox 的拒绝）、`6` 锁冲突（单实例锁复用 GOLD-010 原语）、
     `2` 参数错误、`5` 扫描完成但**没有任何**可进人工队列的候选（预期 BLOCKED）、
     `0` 存在候选（仍需人工确认 + 显式 intake）；失败路径 stdout 为空、stderr 已脱敏、
     **零写入**、旧 state 原样保留。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：inbox 只是"摆放与预检"入口，库内 eligible 记录不足 →
     `PHASE3_3_DATA` 保持 `active=true`（TD-43 / TD-44 / TD-45 / TD-47 ~ TD-52 未解除）；
  2. **预检 ≠ 资格**：`preflight_pass` 不证明授权法律效力、不证明可用性语义，也不解除 blocker；
     真正的落库仍须 operator 显式执行 `scripts/evidence_operator.py workflow --no-dry-run`，
     随后以 `handoff` / `recheck` 复核；
  3. **单级文件名约束**：`files[].path` 只允许候选包目录下的直接子文件（不接受多级 / 绝对 /
     符号链接路径）；包内除 manifest 外的文件必须**全部**声明，否则整包隔离；
  4. **pending 清单为内容镜像**：已从 inbox 移除的内容不再出现在清单里（变化仍由本次 artifact 的
     `discovered` / `status_changed` / `quarantined` 如实报出）；不接邮件 / 短信 / Webhook /
     第三方推送。
- **解除条件**：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据并经人工核验落库，
  `workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不移动 / 删除原始证据、
  永不自动 intake、永不自动解除 blocker；永不因代码完成、模板或 Mock 测试解除 `PHASE3_3_DATA`。

### TD-54 Evidence Inbox 人工复核决策与审计的剩余边界（P0，2026-09-23，GOLD-012）

- **已交付**：`src/evidence/review.py`（`ReviewDecision` / `ReviewReasonCode`（受控词表，
  按决策分组）/ `InvalidationReason` / `ReviewRecord` / `ReviewLedger` / `DecidedReview` /
  `ApprovedEntry` / `InvalidatedApproval` / `ApprovedIntakeList` / `ReviewReport` /
  `record_review_decision` / `build_approved_intake_list` / `load_review_ledger` /
  `write_review_ledger` / `write_approved_intake_list` / `run_review` /
  `render_review_summary` / `exit_code_for`）+ `scripts/evidence_review.py`
  （`--inbox-dir` 必填、`--ledger` 只读、`--out` 唯一 ledger 写开关、`--approved-out` 批准清单、
  `--decision` / `--fingerprint` / `--reviewer` / `--reason-code` / `--note` / `--reviewed-at` /
  `--revision` / `--override` / `--lock` / `--as-of` / `--json`）。
  `src/evidence/inbox.py` 的路径守护原语公开为 `ensure_outside_inbox`（复核层复用**同一**口径，
  行为不变），`src/evidence/__init__.py` 导出新 API。
- **关键保证（已由测试锁定）**：
  1. **只有三种决策**：`APPROVE` / `REJECT` / `NEEDS_CHANGES`；原因码必须落在与该决策匹配的
     受控词表（跨决策使用 → 参数错误）；决策必须**显式引用** GOLD-011 的 64 位**内容级**指纹；
  2. **approve 门禁 fail-closed**：该指纹必须**当前仍出现**在 inbox 扫描结果中、当前预检
     `PREFLIGHT_PASS`、且**不是**模板 / 示例 / Mock；内容变化 → 新指纹（**旧批准不继承**）、
     候选消失 / 预检回退 / ledger 损坏 / 复核元数据含凭据 → 拒绝记录且**零写入**；
  3. **追加式审计**：`decisions` 只 append（**绝不静默覆盖**）；`decision_id` 由
     （指纹 / 决策 / revision / 原因码）确定性派生（篡改即 fail-closed）；同一指纹 + **完全相同**
     决策内容重复提交**幂等**（不新增记录、不改写历史）；任何差异必须显式
     `revision = 既有 + 1` + `override`，`supersedes` 指向被取代的决策，**历史全部保留**；
     ledger 恢复时严格校验安全字段（声称 blocker 已解除即拒绝加载）；
  4. **批准清单 ≠ 资格**：清单在**当前**扫描结果上**重新验证**（候选仍在、仍 `PREFLIGHT_PASS`、
     摘要与复核时一致）；失效批准进 `invalidated`（`CANDIDATE_MISSING` /
     `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`），
     **绝不因为「曾经批准过」就放行**；
  5. **原子写与并发**：`--out` / `--approved-out` 复用 GOLD-009 的 `atomic_write_text`
     （无残留 `.tmp`），写前先取 GOLD-010 的 `SingleInstanceLock`（锁冲突 → 退出码 `6`、零写入），
     并发写不产生半写文档（测试用多线程实测）；
  6. **脱敏**：reviewer / note 含凭据一律**拒绝记录**（不落 `***`）；引用类字段用 `safe_url`
     去 query / userinfo；artifact 只含计数、稳定原因码、指纹、来源名与审计字段，**不含正文**；
  7. **复核元数据不是证据**：`reviewer` / `note` / 文件名 / mtime / 复核时间 / 人工批准本身都
     **不是** `published_at` / `collected_at` / `effective_at` / `availability` 证据；
     产出结构里根本没有这些键（测试对 `FORBIDDEN_EVIDENCE_FIELDS` 做全量递归断言）；
  8. **绝不自动 intake**：模块内不存在 intake / commit 调用，也**绝不**写数据库、
     **绝不**移动 / 删除 / 改写原始 evidence（源码守卫：无 `aiohttp` / `httpx` / `requests` /
     `socket` / `urllib` / `sqlalchemy` / `subprocess`，无 `unlink` / `rmtree` / `os.remove` /
     `os.rename` / `shutil.move`，无 `intake_evidence`，无 `while` 循环）；
  9. **诚实**：`blocker_active` / `human_gate_required` 恒为 true、
     `data_qualification_passed` / `phase_transition_allowed` 恒为 false（**硬编码**），
     approve 数量达到任何阈值都不改变它们。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：review 层只是「谁批了哪一版内容、为什么」的审计入口，库内 eligible
     记录不足 → `PHASE3_3_DATA` 保持 `active=true`（TD-43 / TD-44 / TD-45 / TD-47 ~ TD-53 未解除）；
  2. **人工 APPROVE ≠ data qualification PASS**：落库仍须 operator **显式**执行
     `scripts.evidence_operator.py workflow --no-dry-run`，随后以 `handoff` / `recheck` 复核，
     Phase 切换仍是 **L3 人工确认**；
  3. **复核元数据最小化**：`reviewer` 必须是非敏感标识、`note` ≤ 300 字符，含凭据（`api_key=` /
     `token=` / `Bearer …` / `sk-…`）一律拒绝记录（fail-closed，不做"擦一擦再落盘"）；
  4. **ledger 是唯一事实来源**：只 append、不复写历史；不接邮件 / 短信 / Webhook / 第三方推送，
     也不触发任何自动化（无 scheduler、无 OS 计划任务改动）。
- **解除条件**：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据并经人工核验落库，
  `workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不移动 / 删除原始证据、
  永不自动 intake、永不自动解除 blocker；永不因代码完成、模板、Mock 测试或人工批准数量解除
  `PHASE3_3_DATA`。

---

### TD-55 Evidence Approved-for-Explicit-Intake Intake Plan 与最终写入前门禁的剩余边界（P0，2026-09-23，GOLD-013）

- **已交付**：`src/evidence/intake_plan.py`（`IntakePlanStatus` / `PlanVerificationCode` /
  `PlanViolation` / `PlanEntry` / `OperatorHandoffStep` / `ApprovedListDocument` / `IntakePlan` /
  `load_approved_intake_list` / `verify_intake_plan_inputs` / `build_intake_plan` /
  `compute_plan_id` / `intake_plan_handoff` / `run_intake_plan` / `render_intake_plan_summary` /
  `exit_code_for`）+ `scripts/evidence_intake_plan.py`（`--inbox-dir` / `--ledger` /
  `--approved-list` 必填、`--out` 唯一写开关、`--lock` / `--as-of` / `--json`；
  **没有**任何 intake / `--no-dry-run` 参数）。`src/evidence/__init__.py` 导出新 API。
- **关键保证（已由测试锁定）**：
  1. **复用而非复制**：批准清单的重新验证**直接复用** GOLD-012 的
     `build_approved_intake_list`（同一 inbox 预检 + 同一 ledger 语义），不复制、不降低任何
     资格规则；
  2. **最终写入前逐项核验（fail-closed）**：①批准清单结构自洽（文档标识 / schema / 契约版本 /
     `approved_count` / `invalidated_count` 与列表长度一致 / 四个安全字段与 `approval_scope`
     不可被 state 削弱 / **不得**出现任何证据时间字段 → `IntakePlanStateError`）；②每条批准必须
     **当前仍是** ledger 上该指纹的**最新有效**决策（`LEDGER_MISSING_APPROVAL` /
     `LEDGER_DECISION_SUPERSEDED` / `LEDGER_REVISION_SUPERSEDED`，`review override` 后旧批准
     一律失效）；③该指纹**当前仍在** inbox 且仍 `PREFLIGHT_PASS`、非合成、摘要与复核时一致
     （`FINGERPRINT_MISSING` / `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` /
     `EVIDENCE_INCONSISTENT`）；④清单与"用当前 inbox + ledger 重新算出的批准集合"完全一致
     （`APPROVED_LIST_STALE` / `APPROVED_LIST_TAMPERED` / `INVALIDATED_MISMATCH` /
     `COUNT_MISMATCH`）；⑤计划审计时点不得早于清单生成时间（`PLAN_TIME_BEFORE_APPROVAL`）；
     任一不一致 → `IntakePlanInconsistentError`（退出码 `4`、**零写入**）；
  3. **内容级 `plan_id` 与幂等**：`plan_id` 只由（策略块 + 批准条目 + ledger 最新决策摘要）派生
     （**不含** `generated_at`）；同一输入重复生成得到同一 `plan_id`，同一审计时点逐字节稳定；
     输入内容或 review revision 变化必然产生新 `plan_id`（旧计划 / 旧批准不静默继承）；
  4. **默认只读 / 原子写**：只有显式 `--out` 才写计划本身（复用 GOLD-009 的 `atomic_write_text`，
     无残留 `.tmp`），写前先取 GOLD-010 的 `SingleInstanceLock`（锁冲突 → 退出码 `6`、零写入）；
     所有输入 / 输出必须在 inbox **之外**；只读运行零写入；
  5. **显式衔接、绝不自动执行**：`handoff` 只给**字符串**命令模板（必带 `--no-dry-run` 与显式
     `--input`，并给出 `input_path`）；`auto_intake_allowed` / `writes_database` 恒为 false、
     `requires_explicit_operator_action` 恒为 true；模块内**不存在** intake / commit 调用
     （源码守卫：无 `aiohttp` / `httpx` / `requests` / `socket` / `urllib` / `sqlalchemy` /
     `subprocess`，无 `unlink` / `rmtree` / `os.remove` / `os.rename` / `shutil.move`，无
     `intake_evidence` / `evaluate_write_gate`，无 `while` 循环）；
  6. **诚实**：`blocker_active` / `human_gate_required` 恒为 true、
     `data_qualification_passed` / `phase_transition_allowed` / `auto_intake_allowed` /
     `writes_database` 恒为 false（**硬编码**）；`approved_for_explicit_intake` 与
     `data_qualification_passed` 是两个独立字段，`data_qualification_passed_count` 恒为 0，
     批准数量（含全部候选被批准）不改变任何安全字段；
  7. **脱敏 / 时间语义**：artifact 只含计数、稳定原因码、指纹、来源名、审计字段与已脱敏路径；
     产出结构里根本没有证据时间键（测试对 `FORBIDDEN_EVIDENCE_FIELDS` 做全量递归断言，并断言
     行内证据时间未被复制）。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：计划层只是"最终写入前把清单与当前事实重新绑定"的门禁，
     库内 eligible 记录不足 → `PHASE3_3_DATA` 保持 `active=true`（TD-43 / TD-44 / TD-45 /
     TD-47 ~ TD-54 未解除）；
  2. **计划 ≠ 资格**：`READY_FOR_EXPLICIT_INTAKE` 只表示"人工预审通过且当前仍一致"，落库仍须
     人工**显式**执行 `scripts.evidence_operator.py workflow --no-dry-run`，随后以 `handoff` /
     `recheck` 复核，Phase 切换仍是 **L3 人工确认**；
  3. **计划不是"批准缓存"**：内容变化 → 新指纹、review revision 变化 → 旧批准失效，计划必须
     重新生成；本工具不会把旧计划"升级"成新计划；
  4. **无自动化**：不联网、不接第三方推送、不改 OS 计划任务、不写研究数据库。
- **解除条件**：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据并经人工核验落库，
  `workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不移动 / 删除原始证据、
  永不自动 intake、永不自动解除 blocker；永不因代码完成、模板、Mock 测试或人工批准数量解除
  `PHASE3_3_DATA`。

---

### TD-56 Evidence 显式 Intake Receipt 与资格复核审计的剩余边界（P0，2026-09-23，GOLD-014）

- **已交付**：`src/evidence/intake_receipt.py`（`IntakeReceiptStatus` / `ReceiptVerificationCode`
  / `ReceiptViolation` / `PlanEntryDocument` / `IntakePlanDocument` / `OperatorResult` /
  `QualificationRecheck` / `ReceiptEntry` / `IntakeReceipt` / `load_intake_plan_document` /
  `load_operator_result` / `load_qualification_recheck` / `compute_receipt_id` /
  `verify_intake_receipt` / `build_intake_receipt` / `run_intake_receipt` /
  `render_intake_receipt_summary` / `exit_code_for`）+ `scripts/evidence_intake_receipt.py`
  （`--inbox-dir` / `--ledger` / `--approved-list` / `--plan` 必填；`--operator-result` 可重复、
  `--recheck` 可选；`--out` 唯一写开关；`--lock` / `--as-of` / `--json`；
  **没有**任何 intake / `--no-dry-run` 参数）。`src/evidence/__init__.py` 导出新 API。
- **关键保证（已由测试锁定）**：
  1. **执行后重新绑定（fail-closed）**：①plan 文档结构自洽（文档标识 / schema / 契约版本 /
     四个安全字段与 `auto_intake_allowed` / `writes_database` / `requires_explicit_operator_action`
     不可被削弱 / 条目白名单键 / 计数自洽 / **不得**出现证据时间键 / `plan_id` 必须 64 位小写
     十六进制）→ `IntakeReceiptStateError`；②plan 必须**当前仍然成立**：用**当前** inbox +
     ledger + 批准清单重新算出 `plan_id` 与条目摘要（指纹 / `decision_id` / `review revision` /
     `scope` / 文件清单）完全一致，否则 `PLAN_STALE` / `PLAN_ENTRY_MISMATCH` / `PLAN_TAMPERED`
     （覆盖 stale 指纹、候选消失、preflight 回退、**review override**、清单 / ledger 篡改）；
  2. **人工显式执行结果按内容绑定**：`--operator-result`（`EvidenceIntakeReport.to_dict()` 形态）
     可重复给出；每个被批准的证据文件都必须被覆盖，绑定键是**内容 SHA-256**
     （不看文件名 / mtime / 目录），且 `dry_run=false`、`persisted>=1`、`rows` 条数与 `counts.rows`
     一致、`accepted+quarantined+duplicate<=rows`、`persisted<=accepted`、scope 与批准条目一致；
     缺失 / dry-run / 零落库 / 计数矛盾 / 输入内容不符 / 用了**未被批准**的输入 / 覆盖不全 →
     `OPERATOR_RESULT_MISSING` / `OPERATOR_NOT_EXECUTED` / `OPERATOR_FAILED` /
     `OPERATOR_TAMPERED` / `OPERATOR_INPUT_MISMATCH` / `OPERATOR_SCOPE_MISMATCH` /
     `OPERATOR_COVERAGE_INCOMPLETE`；
  3. **qualification recheck 必须存在且覆盖本次落库**：`phase33_qualification_recheck` 产物、
     `blocker_code` 必须仍是 `PHASE3_3_DATA`、`blocker_active` / `human_gate_required` 必须仍为
     true、时点**不得早于**最后一次显式执行，且任何操作时间都不得晚于收据审计时点
     （`RECHECK_MISSING` / `RECHECK_BEFORE_INTAKE` / `RECHECK_TAMPERED` / `FUTURE_TIMESTAMP`）；
  4. **四个布尔互不蕴含**：`intake_executed`（人工显式落库确实发生）与 `receipt_verified`
     （绑定核验通过）是两个独立字段；`data_qualification_passed` /
     `data_qualification_passed_count` **恒为** false / 0、`phase_transition_allowed` 恒为 false；
     即使 recheck 自报 `ready=true`，收据也**绝不**把它升级为资格 / Phase 结论
     （`is_qualification_decision=false`）；无批准时收据为 `BLOCKED_NO_APPROVED_EVIDENCE`
     且两个布尔均为 false；
  5. **内容级 `receipt_id` 与幂等**：只由（策略块 + `plan_id` + 批准条目 + 执行摘要 + 复核摘要 +
     两个布尔）派生（**不含** `receipt_at`）；同输入同 `receipt_id`、同审计时点逐字节稳定；
     plan / review revision / 指纹 / 执行结果 / 复核结果任一变化 → 新 `receipt_id` 或直接
     fail-closed（旧收据不静默继承）；
  6. **默认只读 / 原子写**：只有显式 `--out` 才写收据本身（复用 GOLD-009 的 `atomic_write_text`，
     无残留 `.tmp`），写前先取 GOLD-010 的 `SingleInstanceLock`（锁冲突 → 退出码 `6`、零写入）；
     所有输入 / 输出必须在 inbox **之外**；只读运行零写入；
  7. **脱敏 / 时间语义**：收据只含计数、稳定原因码、指纹、摘要、已脱敏路径与**审计操作时间**；
     产出结构里没有证据时间键（测试对 `FORBIDDEN_EVIDENCE_FIELDS` 做全量递归断言），
     加载 plan / 执行结果 / 复核结果时也会拒绝含这些键的文档；
  8. **源码守卫**：无 `aiohttp` / `httpx` / `requests` / `socket` / `urllib` / `sqlalchemy` /
     `subprocess`，无 `unlink` / `rmtree` / `os.remove` / `os.rename` / `shutil.move`，
     无 `intake_evidence` / `evaluate_write_gate`，无 `while` 循环。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：收据层只是"把已发生的显式落库与随后的复核绑定成可复核审计记录"，
     库内 eligible 记录不足 → `PHASE3_3_DATA` 保持 `active=true`（TD-43 / TD-44 / TD-45 /
     TD-47 ~ TD-55 未解除）；
  2. **收据 ≠ 资格**：`VERIFIED_EXECUTION_RECORDED` 只说明"人工显式落库发生过且绑定核验通过"，
     放行仍须 `handoff` / `recheck` 与 **L3 人工 Gate**；
  3. **收据不是"免复核通行证"**：任何输入（plan / review revision / 执行结果 / 复核结果）变化后
     必须重新生成收据，本工具不会把旧收据"升级"成新收据；
  4. **无自动化**：不联网、不接第三方推送、不改 OS 计划任务、不写研究数据库、不改
     `.ai/PROJECT_STATE.json`。
- **解除条件**：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据并经人工核验落库，
  `workflow` / `handoff` / `recheck` 显示量化门槛达标，且完成 **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不移动 / 删除原始证据、
  永不自动 intake、永不自动解除 blocker；永不因代码完成、模板、Mock 测试、人工批准数量或
  收据状态解除 `PHASE3_3_DATA`。

### TD-59 Evidence Package / Manifest Builder 的剩余边界（P0，2026-09-23，GOLD-017）

- **已交付**：`src/evidence/package_builder.py`（`EvidencePackageCode` / `ManifestWriteStatus` /
  `PreflightOutcome` / `EvidencePackage*Error` / `ManifestFileEntry` / `RowPreflight` /
  `EvidencePackagePreview` / `build_manifest_preview` / `preflight_rows` / `manifest_bytes` /
  `manifest_digest` / `run_package_builder` / `render_package_summary` / `exit_code_for` /
  `EvidencePackageCode` 稳定原因码 / `EVIDENCE_TYPES` / `PACKAGE_BUILDER_*` /
  `MAX_SOURCE_CHARS` / `MAX_SEMANTICS_CHARS` / `MAX_NOTES_*` / `MAX_FILE_COUNT`）+
  `scripts/evidence_package.py`（`--package-dir` / `--file`（可重复）/ `--evidence-type` /
  `--source` / `--authorization-reference` / `--time-semantics` / `--availability-semantics` /
  `--historical-oos-applicable true|false` 必填；`--notes` / `--out` / `--lock` / `--as-of` /
  `--json` 可选；**没有**任何 intake / `--no-dry-run` / `--force` / `--overwrite` 参数）。
  `src/evidence/__init__.py` 导出新 API。
- **关键保证（已由测试锁定）**：
  1. **人工显式输入，绝不推断**：`evidence_type`（大小写敏感）/ `source` /
     `authorization_reference` / `time_semantics` / `availability_semantics` /
     `historical_oos_applicable`（严格布尔）与文件清单**必须**由人工给出；工具**绝不**从文件名 /
     正文 / URL / mtime / 当前时间或其他上下文推断、补造授权、发布时间、采集时间、availability
     或 OOS 语义（缺失 / 非法一律稳定原因码 + 零写入）；
  2. **复用而不复制契约**：manifest 字段 / 允许键 / 必填项 / 格式词表**直接复用** GOLD-011 的
     `MANIFEST_REQUIRED_FIELDS` / `ALLOWED_MANIFEST_KEYS` / `FILE_ENTRY_REQUIRED_FIELDS` /
     `SUPPORTED_FILE_FORMATS` / `INBOX_SCHEMA_VERSION` 与 `evidence-intake-v1`；引用校验复用
     `valid_reference`、逐行预检复用 `read_input_file` + `assess_row`（与 GOLD-005 / GOLD-011
     同源）；另加内部自检：生成的 manifest 必须**正好**满足 GOLD-011 契约；
  3. **内容级 SHA-256 与 byte-stability**：每个文件按**原始字节**计算 SHA-256、`manifest.files`
     按规范化相对路径**确定性排序**；同输入 + 同显式元数据（含声明顺序不同）→ 逐字节一致；
     manifest 里**没有任何时间字段**（无 `generated_at`；绝不读 mtime / ctime）；
  4. **只读原始 evidence + fail-closed 清单**：拒绝绝对路径 / `..` / 多级路径 / 符号链接
     （文件与 package 目录均不跟随）/ 目录 / `manifest.json` 自引用 / 未支持扩展或格式 /
     重复或大小写冲突声明 / package 目录不存在或不可读 / **包内未声明文件** / package 目录名或
     文件名命中示例合成词表 / 敏感值（凭据键名、疑似 blob、可被脱敏规则命中的取值）；
     **绝不**移动 / 删除 / 改名 / 改写任何原始文件（越界文件从不被读取）；
  5. **默认零写入 + 唯一写开关**：不传 `--out` 时只打印 stdout（不取锁、不写任何文件）；
     `--out` 必须**正好**是 `<package-dir>/manifest.json`，否则退出码 `3`、零写入；
  6. **原子写 + 单实例锁 + 不静默覆盖**：先取 GOLD-010 单实例锁（缺省锁文件**刻意放在 package
     目录之外**，避免成为"包内未声明文件"），锁内**重新**读取与预检（防 TOCTOU），既有 manifest
     不存在则创建、**逐字节一致 → 幂等**、**内容不同 → `MANIFEST_CONFLICT`**（退出码 `5`、
     零写入、**没有** `--force` / `--overwrite`），落盘后**复读自检**；
  7. **写入前同源预检**：`preflight_rows(...)` 输出 `accepted` / `quarantined` /
     `not_oos_eligible` / 稳定原因码计数与逐文件计数（描述性 `outcome`），
     预检结果**不写入** evidence 原始行，**绝不**宣称授权已核验或 data qualification PASS；
  8. **持续硬编码 blocker**：`blocker_active` / `human_gate_required` / `requires_human_action`
     恒为 true，`data_qualification_passed` / `phase_transition_allowed` / `auto_intake_allowed` /
     `writes_database` / `writes_project_state` 恒为 false；生成 manifest ≠ 授权已核验 ≠ 资格通过
     ≠ 可提交 L3；
  9. **源码守卫**：无 `aiohttp` / `httpx` / `requests` / `socket` / `urllib` / `sqlalchemy` /
     `subprocess`，无 `unlink` / `rmtree` / `os.remove` / `os.rename` / `shutil.move`，
     无 `intake_evidence`，无 `getmtime` / `getctime` / `st_mtime` / `st_ctime` / `datetime.now`，
     无 `PROJECT_STATE.json` 读写，无 `while` 常驻循环。
- **剩余边界（本条目跟踪）**：
  1. **仍无真实授权证据**：builder 只是"把已授权的真实文件机械整理成 GOLD-011 可直接扫描的
     manifest"，**不产生**任何证据、**不判断**法律效力 → `PHASE3_3_DATA` 保持 `active=true`
     （TD-43 / TD-44 / TD-45 / TD-47 ~ TD-58 未解除）；
  2. **manifest ≠ 资格**：`manifest.json` 只是候选包**入口声明**，必须交给
     `scripts/evidence_inbox.py` 做只读预检，并继续走人工复核 / intake plan / 显式落库 /
     收据 / 决策包 / 决策记录与 **L3 人工 Gate**；
  3. **授权与语义仍由人工负责**：`authorization_reference` / `time_semantics` /
     `availability_semantics` / `historical_oos_applicable` 都是**人工声明**，程序不证明其真实性；
     声明错误 → scanner / intake 依据既有契约 fail-closed，builder **绝不**代人校正；
  4. **无自动化、无绕过**：不联网、不抓取、不读浏览器 / 邮件 / 云盘凭据、不改 OS 计划任务、
     不写研究数据库、不改 `.ai/PROJECT_STATE.json`；不提供任何"跳过预检 / 跳过人工"的开关。
- **解除条件**：业务方把真实授权 Author / News 数据放进 package 目录，用本 builder 生成 manifest
  后经 `evidence_inbox` / `review` / `intake_plan` / 显式 `workflow --no-dry-run` / `recheck` /
  `intake_receipt` / `decision_packet` / `decision_record` 全链路，且量化门槛达标并通过
  **L3 人工 Gate**。
- **不变量**：工具永不联网、永不抓取、永不绕过 robots / 条款 / 证书、永不推断授权或时间语义、
  永不移动 / 删除 / 改写原始证据、永不自动 intake、永不静默覆盖既有 manifest、永不自动解除
  blocker；永不因代码完成、模板、Mock 测试或"manifest 已生成"解除 `PHASE3_3_DATA`。

### TD-60 monitoring ↔ evidence 包边界的导入顺序（P1，已修复，2026-09-23，GOLD-018）

- **已修复的真实缺陷**（GOLD-017 任务外发现，并在**纯净 HEAD** 复现）：

  ```text
  .venv\Scripts\python.exe -c "import src.monitoring; import src.evidence"
  ImportError: cannot import name 'BatchQuantification' from partially initialized module
  'src.monitoring.evidence_readiness' (most likely due to a circular import)
  ```

  根因是**包边界**问题而非业务逻辑问题：两个包的 `__init__` 都在**包初始化阶段** eager import
  整个依赖图 —— `src.evidence.__init__` → `decision_packet` → `readiness_runner` → `handoff` →
  `src.monitoring.evidence_readiness` → `src.evidence.contracts`。两条链在"先导入谁"不同时会踩到
  **尚未初始化完**的模块：monitoring-first 必崩、evidence-first 正常（同一份代码）。
- **修复方式（只改模块边界，不改业务语义）**：`src/evidence/__init__.py` 与
  `src/monitoring/__init__.py` 改为 **PEP 562 惰性导出**（模块级 `__getattr__` + `__dir__`），
  包初始化阶段**不加载任何子模块**；惰性表只登记 `公开名 -> 定义子模块`（别名单独登记），
  **不复制**任何类 / 阈值 / 枚举 / 常量，单一事实源仍是各子模块。`__all__` 逐字节未变，
  `from src.evidence import X` / `from src.monitoring import Y` / `from src.<pkg> import <子模块>` /
  `import src.<pkg>` 后取属性的既有用法**全部保持可用**。
- **关键保证（已由测试锁定）**：
  1. **任意导入顺序稳定**：monitoring-first / evidence-first / 直接子模块 first / from-import /
     star-import / 重复与交错导入在 **fresh subprocess** 中全部成功，且不再出现循环导入签名
     （`tests/integration/test_evidence_import_order.py`）；
  2. **包初始化不再加载依赖图**：`import src.evidence` 不得拉入 `src.monitoring`；
     `import src.monitoring` 不得拉入 `src.evidence.handoff` / `decision_packet`（同一文件）；
  3. **零语义漂移**：`__all__` 与惰性表同源、每个公开名 `is` 其定义子模块上的对象（**禁止第二份
     事实源**）、未登记名字抛 `AttributeError`、既有公开导出见证仍在
     （`tests/unit/test_lazy_package_exports.py`）；
  4. **CLI 门禁**：`scripts/evidence_*.py`（12 个）+ `scripts/intake_evidence.py` +
     `scripts/report_collector_health.py` 在 fresh subprocess 里可 import、`--help` 可用，
     且 cwd 为**空**临时目录 → 断言零文件写入 / 零网络 / 零数据库
     （`tests/integration/test_evidence_cli_smoke.py`）；
  5. **源码级守卫**：两个包的 `__init__` 模块级**只允许** `__future__` / `importlib` / `typing`
     （防止有人把 eager 子模块导入写回来）。
- **剩余边界（本条目跟踪）**：
  1. 惰性导出把"包初始化即暴露全部名"改为"首次访问才 import 其定义子模块"（解析后缓存进模块
     字典，后续零额外开销）；这要求子模块导入**无环** —— 现状成立并由上述用例锁定；
  2. 惰性表与 `__all__` 仍需**人工同步**：新增公开导出必须同时更新两处；漏更新会被单元门禁当场
     抓住（`set(__all__) == set(_LAZY_EXPORTS)`），但仍属人工纪律；
  3. 本次**只修包边界**：未改 `evidence-intake-v1`、`PHASE3_3_DATA` 阈值、readiness 算术、
     安全字段、L3/L4 Gate、`LIVE_TRADING` / 外部订单设置；`PHASE3_3_DATA` **仍 BLOCKED**；
  4. GOLD-005 ~ GOLD-017 的人工路径与 fail-closed 语义不变：导入顺序修复**不构成**任何资格推进，
     也不减少任何人工 Gate。
- **不变量**：两个包的 `__init__` 永不 eager import 子模块；惰性表永不复制业务对象；
  永不因代码完成 / 导入变快 / 测试通过解除 `PHASE3_3_DATA`。

---

### TD-61 PHASE3_3_DATA 证据缺口只读诊断的剩余边界（P0，2026-09-23，GOLD-026）

- **已交付**（`src/monitoring/evidence_gap_diagnostic.py` + `scripts/evidence_gap_diagnostic.py`）：
  把"还缺什么、必须由谁完成"汇总为**单一**诊断（稳定 JSON + 人类可读 Markdown），
  **只读**复用既有能力 —— readiness（GOLD-006）+ handoff（GOLD-008）+ inbox manifest 预检
  （GOLD-011，`scan_inbox`）+ `evidence-intake-v1` 契约字段 / 原因码 + `src.alpha.evidence_gate`
  阈值；本模块**不新增、不降低**任何资格阈值，也不改 `src/alpha/**` 与既有 evidence 模块；
- **四态分类**（每项机器可读）：`CODE_READY`（代码 / 契约已就绪，含输入契约字段、manifest 声明、
  原因码词表、只读保证）/ `EVIDENCE_MISSING`（真实证据缺失、未 ingest 或未达标）/
  `HUMAN_VERIFICATION_REQUIRED`（证据已在，法律效力 / 出处 / 时间语义只能人工核验）/
  `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate）；分类按既有契约分组
  （authorization / source / published_at / collected_at / effective_at / availability_oos /
  time_semantics / sample_size / coverage），共 25 个诊断项、确定性排序；
- **关键保证（已由测试锁定）**：
  1. **顶层恒 BLOCKED**：`readiness` 恒 `GATE_BLOCKED`，`blocker_active` / `human_gate_required`
     恒 true，`data_qualification_passed` / `phase_transition_allowed` / `advance_allowed` /
     `l3_l4_auto_advance_allowed` 恒 false（**硬编码**，不被上游或篡改输入透传）；
  2. **Mock 永不 qualify**：模板 / Mock / 示例恒 `counts_toward_eligibility=false`；
     候选 manifest 的 `qualifies` 恒 false；检测到合成标记时 `exclude_synthetic_evidence`
     步骤降为 `MACHINE_UNSATISFIED`；
  3. **不伪造缺失字段**：缺失时只报告 `missing_fields`（字段名）与 `missing_count`，
     并以正则断言"报告中除 `as_of` 外**没有任何**时间戳"；另有测试把候选文件 mtime 改为
     2019-01-02 后断言该日期**绝不出现**在诊断中；
  4. **零网络 / 零写库 / 无文件时间**：源码级守卫断言模块与 CLI 只 import 既有模块，
     且不调用 `open` / `os.stat` / `getmtime` / `utime` / 任何网络或 `intake_evidence` / `commit`；
     CLI 唯一写开关是显式 `--out`（原子写），默认只打印 stdout；
  5. **量化门槛全达标仍 BLOCKED**：Author ≥ 30 / News ≥ 200 / ≥ 90 天 / 单源 ≤ 40% 全部满足时，
     实质缺口归零（退出码 `0`），但顶层仍 `GATE_BLOCKED`、仍须 L3 人工决策；
- **剩余边界（本条目跟踪）**：
  1. 库内仍**没有**足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`
    （TD-43/44/45/47~60 均未解除）；本诊断**不解除** blocker，也不采集数据；
  2. 诊断**不是**资格判定器：CLI 没有任何 `qualify` / `approve` / `advance` 参数
     （未知参数一律退出 `2`），也不写 `PROJECT_STATE`、不签发 review；
  3. manifest 事实只来自**显式** `--inbox-dir` 的本地目录（缺省不扫描）：未提供时合成标记
     与时间语义声明不可见（诊断会显式标注"未提供候选目录"）；
  4. 分类为"证据已在 / 需人工核验"**不等于**授权已核验：法律效力、许可范围、签认人身份、
     独立可用时间出处的判定永远属人工与 L3；
- **不变量**：诊断永不采集、永不写库、永不联网、永不用当前时间 / 文件 mtime / 抓取时间 /
  推断值填补 `published_at` / `collected_at` / `effective_at` / `available_at` / OOS 证据；
  永不因代码完成或测试通过解除 `PHASE3_3_DATA` 或推进 L3 / L4。

---

### TD-62 人工证据 Intake Handoff 预检的剩余边界（P0，2026-09-23，GOLD-027）

- **已交付**（`src/evidence/intake_handoff.py` + `scripts/evidence_intake_handoff.py`）：
  把 GOLD-026 的只读缺口诊断转成**单一、版本化**的人工证据提交 / 预检 handoff，
  业务人员按同一契约准备真实授权 Author / News 材料，预检只回答"材料是否齐全、字段是否可解析、
  来源 / 时间声明是否带独立证据引用"，**绝不**回答"是否合格"；
- **版本化 schema**（`intake_handoff_schema()` / `--schema`）：`kind` +
  `schema_version=1` + `contract_version=evidence-intake-v1` + 目录契约（沿用
  GOLD-011 inbox 布局：`manifest.json` 必填声明 `evidence_type` / `source` /
  `authorization_reference` / `time_semantics` / `availability_semantics` /
  `historical_oos_applicable` / `files[path,sha256]`）+ Author / News 必需契约字段
  （`required_field_names`，Author 额外 `author_name` / `external_account_id`）+
  10 条材料要求（`evidence_records` / `source_identity` / `authorization_declaration` /
  `time_semantics` / `published_at` / `collected_at` / `effective_at` / `availability_oos` /
  `author_identity` / `phase33_data_gate`）+ 独立时间语义要求（6 条）+ 状态词表与
  `preflight_pass` 的诚实语义；阈值**只引用** `src.alpha.evidence_gate`
  （经 `src.evidence.handoff.thresholds`），**不新增、不降低**任何资格门槛；
- **五态状态**（每材料机器可读）：`MISSING`（缺失或机械校验未通过）/
  `PRESENT_UNVERIFIED`（已提交但独立证据引用 / 机械校验不足，不能进入人工核验队列）/
  `HUMAN_VERIFICATION_REQUIRED`（结构完整，法律效力 / 出处 / 时间语义只能人工核验）/
  `NON_QUALIFYING`（Mock / 模板 / 示例 / 合成：**永远**不计资格）/
  `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate，Gate 材料恒为此态）；
- **关键保证（已由测试锁定）**：
  1. **结构完整 ≠ 资格通过**：`preflight_pass=true` **只**表示 package 结构完整
     （无 `MISSING` / 无 `PRESENT_UNVERIFIED` / 非合成 / GOLD-011 预检零原因码），
     文档与机器字段（`preflight_pass_meaning` / `preflight_pass_does_not_imply`）都显式写明；
  2. **安全布尔硬编码**：`evidence_qualified` / `data_qualification_passed` /
     `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false；
     `blocker_active` / `human_gate_required` / `gate_blocked` 恒 true；
  3. **Mock 永不 qualify**：行级（`record_kind=example` / `is_mock=true`）与 manifest 级合成标记
     都使全部材料恒 `NON_QUALIFYING`（且不误报为"缺失字段"），`counts_toward_eligibility` 恒 false；
  4. **不伪造时间事实**：缺 `time_semantics` / 缺 `available_at` 时只报告字段名与人工下一步，
     测试以正则断言"报告中除 `as_of` 外没有任何时间戳"，并把候选文件 mtime 改为 2019-01-02
     后断言该日期**绝不出现**；`url` 等**可选**字段绝不误报为缺失；
  5. **只读**：源码级守卫断言模块与 CLI 只 import 既有模块且不调用
     `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / `write_text`
     （CLI 唯一写开关是显式 `--out`，复用 `atomic_write_text` 原子写）；另有测试断言预检前后
     候选包内所有文件的**内容与 mtime 完全不变**、目录项不增不减；
- **剩余边界（本条目跟踪）**：
  1. 库内仍**没有**足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`
     （TD-43/44/45/47~62 中的资格项均未解除）；预检**不解除** blocker，也不采集数据；
  2. 预检**不是**资格判定器：CLI 没有任何 `qualify` / `approve` / `advance` 参数
     （未知参数一律退出 `2`），不写 `PROJECT_STATE`、不签发 review、不推进 L3 / L4；
  3. 材料级判定只使用**既有** inbox 预检的字段级事实（manifest 声明 + 行级机械校验结论）：
     行级具体原因码未透传到材料级，因此"存在被隔离行"统一按 `PRESENT_UNVERIFIED` 报告
     （fail-closed，绝不按"已通过"处理）；如需逐行原因码，仍看 `evidence_ready` /
     `evidence_gap_diagnostic` / `evidence_inbox` 的产物；
  4. `HUMAN_VERIFICATION_REQUIRED` **不等于**授权已核验：法律效力、许可范围、签认人身份、
     独立可用时间出处的判定永远属人工与 L3；
- **不变量**：预检永不采集、永不写库、永不联网、永不用当前时间 / 文件 mtime / 抓取时间 /
  推断值填补 `published_at` / `collected_at` / `effective_at` / `available_at` / OOS 证据；
  永不因代码完成或测试通过解除 `PHASE3_3_DATA` 或推进 L3 / L4。

---

### TD-63 材料级人工核验凭证的剩余边界（P0，2026-09-23，GOLD-028）

- **已交付**（`src/evidence/human_verification_attestation.py` +
  `scripts/evidence_human_verification_attestation.py`）：把 GOLD-027 的**结构完整预检**进一步
  转换为**可审计的材料级人工核验凭证** —— 对每个必核验材料记录受控决策
  （`VERIFIED` / `REJECTED` / `NEEDS_CHANGES`）、稳定 `reason_code`、显式 `reviewer` label、
  带时区 `reviewed_at` 与**独立 `evidence_reference`**，并**硬绑定**当前候选包 fingerprint +
  GOLD-027 handoff / manifest 内容身份 + scope；
- **版本化契约**（`attestation_schema()` / `--schema`）：`kind` + `schema_version=1` +
  `contract_version=human-verification-attestation-v1` + 必核验材料清单（**只引用** GOLD-027
  `MATERIAL_SPECS` 的非 Gate 材料，覆盖 authorization / source / time_semantics /
  published_at / collected_at / effective_at / availability_oos，Author 额外 author_identity）
  + 决策词表 + 稳定原因码 + 核验输入文件格式 + `all_required_verified` 的诚实语义；**不新增、
  不降低**任何资格门槛；
- **内容寻址**（`attestation_id`）：由「硬编码策略块 + `revision` / `supersedes` + package 绑定
  + 逐材料核验」派生；**不含**审计时间（换 `--as-of` 不改变 id），同输入 → byte-stable；
- **关键保证（已由 59 项测试锁定）**：
  1. **材料级核验 ≠ 资格通过**：`all_required_verified=true` 只表示「非 Mock / 模板 / 示例 /
     合成、GOLD-027 `preflight_pass=true`、每个必核验材料都有带独立引用的 `VERIFIED`」；
     `evidence_qualified` / `data_qualification_passed` / `phase_transition_allowed` /
     `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false、`blocker_active` /
     `human_gate_required` / `gate_blocked` 恒 true（**硬编码**，文档与机器字段都写明）；
  2. **对不可核验材料声明 `VERIFIED` 一律 fail-closed**：Mock / 模板 / 合成 →
     `NON_QUALIFYING_PACKAGE`；`preflight_pass=false` → `PREFLIGHT_NOT_PASSED`；材料状态不是
     `HUMAN_VERIFICATION_REQUIRED` → `MATERIAL_NOT_VERIFIABLE`；缺材料决策 →
     `MATERIAL_DECISION_MISSING` 且 `all_required_verified=false`；Gate 材料不可被核验；
  3. **漂移不继承**：核验输入声明的 fingerprint / handoff 内容身份 / scope 与**当前**候选目录
     不一致 → `PACKAGE_FINGERPRINT_MISMATCH` / `HANDOFF_CONTENT_MISMATCH` / `SCOPE_MISMATCH`；
     `verify` 模式重新绑定当前 package，检测 `PACKAGE_FINGERPRINT_DRIFT` /
     `PACKAGE_CONTENT_DRIFT` / `HANDOFF_CONTENT_DRIFT` / `PREFLIGHT_DRIFT` 与缺引用；
  4. **防伪 / 防静默改写**：`verify` 重新推导 `attestation_id` 并逐项复核安全字段、计数 /
     合取自洽、无证据时间键，任一被改写 → `ATTESTATION_TAMPERED`；同 id 幂等，内容不同必须显式
     `--revision >= 2 --supersedes <attestation_id>`；
  5. **零证据改写 / 零伪造时间**：候选证据内容与 mtime 零变化；`reviewed_at` naive / 未来、
     疑似凭据、未知字段、重复材料、裸文件名引用一律拒绝；除审计时点与人工 `reviewed_at` 外
     报告中没有任何时间戳，候选文件 mtime 绝不出现；
  6. **只读**：源码级守卫断言模块与 CLI 只 import 既有模块且不调用
     `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / 直接 `write_text`
     （CLI 唯一写开关是显式 `--out`，复用 `atomic_write_text` 原子写；默认零写入）；
- **剩余边界（本条目跟踪）**：
  1. 库内仍**没有**足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`
     （TD-43/44/45/47~63 中的资格项均未解除）；凭证**不解除** blocker，也不采集数据；
  2. 凭证**不是**资格判定器：CLI 没有任何 `qualify` / `approve` / `advance` 参数
     （未知参数一律退出 `2`），不写 `PROJECT_STATE`、不签发 L3 / L4 review；
  3. 材料级判定仍只消费**既有** inbox 预检的字段级事实：行级具体原因码未透传到材料级，
     "存在被隔离行"统一按 `PRESENT_UNVERIFIED` 报告（fail-closed）；
  4. `reviewed_at` / `reviewer` 的**真实性**（谁核验、何时核验、引用是否真的支撑该结论）
     永远属于人工与 L3 责任：凭证只保证"人工显式声明 + 可追溯 + 不可静默改写"；
  5. 未实现单实例锁（与 GOLD-016 不同）：并发写入依赖原子写与既有凭证冲突检查；
     如需强并发保证，可在后续任务中引入锁；
- **不变量**：凭证永不采集、永不写库、永不联网、永不用当前时间 / 文件 mtime / 抓取时间 /
  推断值填补 `published_at` / `collected_at` / `effective_at` / `available_at` / OOS 证据；
  永不因代码完成或测试通过解除 `PHASE3_3_DATA` 或推进 L3 / L4。

### TD-64 APPROVE 门禁的 attestation binding 剩余边界（P0，2026-09-23，GOLD-029）

- **现状**：`src/evidence/review.py` 已把新 `APPROVE` 强制绑定 GOLD-028 材料级人工核验凭证
  （`build_attestation_binding()`：凭证文档完整性 + 与**当前**候选目录逐项重新绑定核验），
  批准记录只保存**最小** binding 白名单；`build_approved_intake_list()` 与
  `src/evidence/intake_plan.py` 会在**当前** inbox / handoff 上重新验证该 binding，
  缺失 / 未完整核验 / 漂移 / 被篡改时以稳定原因码（`ATTESTATION_MISSING` /
  `ATTESTATION_INCOMPLETE` / `ATTESTATION_STALE` / `ATTESTATION_TAMPERED`）失效批准；
  旧 ledger（无 binding 的 `APPROVE`）保持可读且**绝不**被追溯升级。
- **剩余边界（本条目跟踪）**：
  1. **手写 ledger 的伪造风险**：binding 只是"批准时写入的内容摘要"。对 ledger 有写权限的人
     可以**直接改写** `attestation` 块（伪造 `attestation_id` / 摘要 / `scope`）而通过结构白名单；
     `build_approved_intake_list()` 在没有凭证文件时无法重新推导 `attestation_id`，只能校验
     结构 + 比对**当前候选包**的材料结构内容身份。建议后续引入基于外部密钥 / 追加式签名日志
     （或把 binding 摘要锚定到不可改写的外部审计）的完整性保护；
  2. **handoff 快照身份只在 APPROVE 门禁复核**：`handoff_content_sha256`（整个 handoff 快照，
     含全部候选包）只在凭证文件在场的 `APPROVE` 门禁逐项复核；批准清单 / plan 链以**被批准
     候选包自身**的材料结构内容身份 + `scope` 为准（刻意如此：新增无关候选包不应静默作废
     既有批准）。若需"整批快照"语义，应在后续任务中显式引入批次绑定；
  3. 库内仍**没有**足量真实授权证据，`PHASE3_3_DATA` 保持 `active=true`
     （TD-43/44/45/47~63 中的资格项均未解除）；本门禁**不是**资格判定器，
     `data_qualification_passed` / `phase_transition_allowed` 恒为 false、
     `blocker_active` / `human_gate_required` 恒为 true，Phase 切换仍须 L3 人工 Gate；
  4. `reviewer` / `reviewed_at` 的真实性（谁核验、何时核验）仍属人工与 L3 责任：
     本门禁只保证"批准**必须**绑定一份当前、完整、未漂移的凭证，并留下可追溯的最小绑定"。
- **不变量**：本门禁永不采集、永不写库、永不联网、永不用当前时间 / 文件 mtime 填补证据时间；
  永不因代码完成或测试通过解除 `PHASE3_3_DATA` 或推进 L3 / L4。

---


---

## 4. 已知限制（设计取舍，非缺陷）

| 项 | 说明 | 依据 |
|---|---|---|
| 测试环境为 SQLite | 不保存时区偏移、无 JSONB/原生 UUID；测试只验证**可移植的约束与语义**，原生类型由 PG 迁移 + CI 的 PG 作业保证 | `docs/03` 第 1 节、`database/types.py` |
| 采集器同步写库 | 网络并发用 `asyncio + aiohttp`，DB 写入保持同步 SQLAlchemy Session（便于研究脚本复用 repository） | `database/session.py` 顶部说明 |
| `effective_at = max(published_at, collected_at)` | 发布时间晚于采集时间（跨时区错误 / 预发布）时取较晚者，宁可推迟可用 | `src/common/time.py::resolve_effective_at` |
| 宏观 `event_at` 保持原始粒度 | 日/月/季观测不重采样、不拉平；provider 未提供发布时间时 `published_at` 置空 | `docs/04 §15` |
| `fed_press_releases` 阈值为 0 | 官方源每周仅数次，窗口内"安静"是正常状态，避免噪声告警 | `README.md §7` 阈值表 |
| 未注册采集器的来源必须 `enabled=false` | 例如 `econ_calendar_investing`：运行期会明确报错，不静默跳过 | `database/seeds/sources.py` |
| 4h 周期在采集层被跳过 | 属上表 TD-03，不是静默失败：`MarketCollector` 会给出跳过年限与告警 | `src/collectors/market.py` |

---

## 5. 技术债关闭流程（强制）

1. 修复必须**先有失败测试**（`docs/06` 通用 Bug 修复流程），测试带 `regression` 标记；
2. 涉及表结构变更 → 必须新增 Alembic 迁移（**禁止**修改已发布的迁移文件）；
3. 涉及时间字段 → 必须检查未来数据泄漏（带 `leakage` 标记的测试）；
4. 关闭时同步更新三处：本文件登记表（移入「变更日志」）、`docs/04` 对应表注记、`docs/05` 对应阶段段落；
5. 不得用"注释掉测试 / skip / TODO 占位 / 假数据"作为关闭方式（`docs/06` 第 24 条：禁止伪完成）。

---

## 6. 变更日志

| 日期 | 变更 |
|---|---|
| Phase 1 冻结（本轮） | 建立本登记册；登记 TD-01 ~ TD-15；记录团队裁决：100% Mock 保持、宏观预期值留空、`econ_calendar_collector` 禁用、4h 聚合延后、新闻时区歧义交 Phase 2 |
| Phase 1 收尾轮 | 新增端到端复核工具 `scripts/phase1_pipeline_report.py` 与报告 `docs/09_Phase1_数据管道总结报告.md`（结论 PASS：三源 SUCCESS、不变式零违规、重跑幂等）；`docs/04 §43` / `docs/05` 增加冻结与遗留登记；修复真实缺陷 **alembic.ini formatter 误用 `%%`**（导致迁移日志只打印格式串、内容全丢），并新增回归测试 |
| Phase 2 第一步 | migration `0005_phase2_author_lab_tables`：新增 4 张研究事实表（严格时间 CHECK、幂等自然键、append-only 守卫）+ `tests/integration/test_phase2_author_lab_schema.py`（37 项）；`docs/10_标注规范.md` 草案（含 JSON Schema 与验收指标）；`docs/04 §44` 实现层决策登记。团队裁决：不再采集微博、LLM 接口隔离 Mock 优先、先定标注规范后抽样标注。**修复真实缺陷**：`test_uuid7.py::test_timestamp_roundtrip` 时间炸弹（硬编码时刻 + `uuid7()` 单调钳位 → 2026-09-12 08:30:15 UTC 后必然失败），改为显式重置单调状态并新增钳位语义回归测试；`pyproject.toml` 声明团队批准的 Phase 2 依赖（numpy/pandas/scikit-learn/httpx/jieba） |
| Phase 2 第二步（周末自动驾驶） | `src/processors/`：观点抽取**严格契约**（`schemas.py`，`extra="forbid"`）+ **可注入协议**（`opinion_extractor.py`）+ **词典/正则 Mock 实现**（`regex_extractor.py`，`parser_version="mock-regex-v1"`）+ **时间因果契约**（`timeline.py`）；`scripts/sample_annotation_set.py` 抽样脚本（随机种子 / 分层 / 去重 / 标注列留空）。`docs/10` 升为**已批准 v1.0**（entry 门槛统一 ≥0.90、补充实现口径）。新增测试 141 项（单元 133 / 集成 3 / 泄漏 8）；期间修复 3 个真实设计缺陷（价格数字归属规则、信息类型判定范围、英文关键词词边界）与 1 个方言缺陷（`sa.text()` 在 SQLite 返回字符串）。全量测试 **596 passed**；`ruff` / `mypy` 全绿 |
| 周末第二轮（狂飙权限，2026-09-12） | 交付三项 Phase 2 基建：①**作者库 CRUD + 审计**（`database/repositories/authors.py` / `audit.py`）+ **作者 CSV 导入 CLI**（`--scope authors` / `--authors-csv`，内置 3 条 Mock 作者、不碰微博）→ **TD-16 框架部分解除**；②**Post → Opinion 管道骨架**（`src/processors/opinion_pipeline.py`：幂等 / 失败隔离 / 空文本 SKIPPED / 无观点留痕 / 未知标的跳过）；③**传播去重**（`src/processors/similarity.py` 零依赖 TF-IDF 余弦 + `propagation.py` 传播边与独立观点数；1+99 → `clusters=1`、`duplicate_ratio=0.99`，阈值 0.80 由实测标定）。**修复 2 个真实缺陷**：SQLite 读回 naive `effective_at` 触发严格守卫（新增 `timeline.ensure_utc_from_database`，仅用于数据库读回值）；`processed_items` 无 `error_message` 列（schema 冻结）→ 失败详情改写入 `structured_json`。新增测试 140 项（596 → **736 passed**）；`ruff` / `mypy`（56 files）全绿；**未新增迁移、未加依赖**；新增技术债 TD-17 ~ TD-21 |
| 周日阻塞修复（2026-09-13） | 解决「人工标注没有输入数据」：新增 `scripts/generate_mock_posts.py`（250 条合成博文，含 126 条对抗样本 / 8 类、5 条转载、时间自洽、`is_mock` 留痕）+ 抽样脚本"输入缺失自动补数据"与**来源体检**（`input_is_mock` 入元数据）+ 三处真实缺陷修复（GBK 控制台崩溃/乱码、脚本模式 `ModuleNotFoundError`、标注 CSV 无 BOM 导致 Excel 乱码）。新增测试 61 项（736 → **797 项 / 796 passed + 1 skipped**），含**真实进程**冒烟 `tests/integration/test_cli_scripts.py`；**TD-21 解除**（依赖已安装，jieba 分支转为 skip）；`ruff` / `mypy`（58 files）全绿；仍未新增迁移、未改 schema |
| 多模型标注对比（2026-09-13） | 新增 `scripts/compare_model_annotations.py`（**标准库 zipfile+XML 读 xlsx**，零新增依赖）：5 字段 × 3 模型的完全一致比例、字段分歧率、两两一致率、留空率、分歧形态；导出 `logs/pending_review.csv`（226 行待人工裁决，`final_gold_standard` 留空）与 `logs/model_consensus.csv`（774 行共识，带 provenance）；报告 `docs/experiments/annotation_model_comparison.md`。实测：**三模型完全一致 60/200 = 30.0%**，最大分歧在 `information_type`（67.5%）；抽取控制台编码工具到 `scripts/_console.py`；新增测试 67 项；**登记 TD-22（明文密钥文件，P0）** |
| 依赖变更（2026-09-13，团队批准） | `pyproject.toml` 的 **dev** 依赖新增 `openpyxl>=3.1`（实际安装 3.1.5，连带 `et-xmlfile` 2.0.0），用于后续「人工裁决结果合并 / `pandas.read_excel` 复核」工具链；**运行时代码不依赖它**——`scripts/compare_model_annotations.py` 仍用标准库 `zipfile` + `xml.etree` 解析 xlsx（有单测锁定不得引入 openpyxl/pandas）。TD-22 状态更新：用户已把明文密钥文件改名为 `.env`（已被 `.gitignore` 覆盖），**仍需轮换该 Key** |
| 基线评估（2026-09-13） | 新增 `scripts/evaluate_extractor.py`（只读、可注入抽取器）：`logs/ground_truth_200.csv` vs `mock-regex-v1`，**严格区分该判未判 / 提取错误 / 不该判却判**，出混淆矩阵与点位差值分布 → `logs/extractor_eval.csv` + 《Phase 2 基线评估报告》。实测（人工子集口径）：方向 **100%**（PASS）、目标位 25%、止损 0%、周期 0%、信息类型 40.7%；全量 1000 格：命中 312 / 该判未判 **289** / 提取错误 103 / 不该判却判 **4**（漏判占失败 73%）。**登记 TD-23（召回率过低，P1）**；修两个工程缺陷（测试覆盖真实产物、报告 f-string 未插值）；测试 898 → **928 项**（927 passed + 1 skipped） |
| LLM 抽取器基建（2026-09-13，团队确认 5 项设计 + 4 项强制要求） | 新增 `src/processors/llm_client.py`（同步 DeepSeek 客户端：超时重试 ≤3 / 滑动窗口 60 次每分钟 / 429 遵守 `Retry-After` / 5xx 指数退避 / 401 不重试 / `max_api_calls` 预算门禁 / `redact()` 全链路脱敏）、`prompt_opinion.py`（`opinion-prompt-v1`，中文指令 + 英文键名 + 6 few-shot，规则逐条对齐 `docs/10 §4.9`）、`llm_cache.py`（键含 `prompt_version` → 改 Prompt 自动失效；`auto/readonly/refresh/off`；原子写入；失败也缓存）、`llm_extractor.py`（缓存优先 → API → 复用 `build_drafts()` 校验；单帖失败降级诊断，401/缺 Key 抛错）；`DiagnosticCode` 新增 `llm_api_error` / `llm_parse_error`；`config/settings.py` 新增 `SecretStr` 类型的 `deepseek_*` 配置（`repr()` 掩码）。新增测试 **85 项**（MockTransport + 假时钟，零网络零 token），全套 **1012 passed / 1 skipped**；`ruff` / `mypy`（66 files）全绿；**未新增迁移、未改 schema、未加依赖**。**修复真实缺陷（安全）**：`tests/conftest.py` 的网络守卫只拦 socket 非本机地址，实测在**本机代理（127.0.0.1:7892）**环境下 httpx 仍可真实访问外网（探针真的收到了 api.deepseek.com 的 401，未消耗 token）→ 新增 **httpcore 后端层拦截**（`SyncBackend/AnyIOBackend.connect_tcp`），并用回归测试锁定 |
| LLM 试点 20 条（2026-09-13，真实 API，团队授权）+ 评估脚本接线 | ①`scripts/evaluate_extractor.py` 接上 `--extractor llm / --limit N / --sample head\|stratified`：LLM 产物**独立落盘**（`logs/extractor_eval_llm.csv` + 《Phase2_LLM试点报告.md》 + `<eval>_usage.json`），并把 **token/费用台账**（本次真实调用 vs 缓存复用、峰/谷两档价）打进控制台与报告；`--sample stratified` 按线索桶（条件句/引用/弱化/复盘/看图/点位）轮询，保证对抗样本进得了试点。②**真实 API 探针**（`logs/_probe_deepseek.py`，耗 ~65 tokens）确认 4 条事实：`GET /models` 只有 `deepseek-flash`/`deepseek-v4-pro`（`deepseek-chat` 已被**静默路由**到 flash，响应 `model` 回显 flash）、新模型**默认开启思考模式**（故显式 `thinking={"type":"disabled"}`）、`response_format=json_object` 可用、`usage` 含 `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` → 默认模型改为 `deepseek-flash`，新增 `estimate_cost_usd()`/`is_peak_utc()` 与官方价目表（含峰谷价、旧名别名）。③**20 条试点实测**：20/20 成功、0 失败 0 解析失败；输入 **33,180 tokens**（上下文缓存命中 27,520 / 未命中 5,660）+ 输出 1,690 → 峰值价 **$0.0039**（≈¥0.028）/ 低谷价 $0.0019；`readonly` 复跑 **20/20 命中、0 次请求、$0**；核心四字段人工子集 **92.3%**（方向 100%、止损 100%、目标位 100%、周期 0%），信息类型 46.2%。④**发现 3 个真问题并登记 TD-24/25/26**：few-shot 与语料**逐字重叠**（18/200 条污染，违反数据泄漏红线，必须换语料外例句 + 升 prompt v2）、`information_type` 见数字就判 TECHNICAL（7 格全错，需人工补优先级口径）、`no_opinion` 与 §4.9 规则 4 冲突（few-shot 第 6 例自己教错了）。⑤`.gitignore` 增加显式 `logs/llm_cache/` 规则（缓存=付费 API 原始数据，严禁入库）；实测 20 个缓存文件**无 `.tmp` 残留、无 `sk-` 明文**。新增测试 **27 项**（思考模式/缓存计费/费用估算/台账分层/抽样/LLM 端到端/致命错误快速失败），全套 **1039 passed / 1 skipped**；`ruff` / `mypy`（66 files）全绿 |
| 开源标注基准对照（2026-09-14） | 新增 `scripts/load_hf_benchmark.py`（HF 数据集加载 + 字段映射：默认 `--dry-run`；未装 `datasets` 时走 datasets-server **只读 HTTP** 接口，**零新增依赖**；原始标签逐条留档、不改写）+ `scripts/report_hf_benchmark.py`（Wilson 95% CI 报告生成器，核心结论只基于 `stance`）与 45 项单测；`scripts/evaluate_extractor.py` 新增 **`scoring=NOT_EVALUATED`** 机制（金标准留空 + 该标记时，模型给值只记「额外信息」，**不计假阳性**、不进任何分母；口径见 `docs/10 §7.1`）；实测 150 条（黄金 100 + 股吧 50）：正则 2/100、LLM 0/100（其中 90 格为 `UNKNOWN` 合规拒答，**无判反方向**），LLM 真实调用 150 次 **$0.0250**、失败 0/解析失败 0；报告见 `docs/experiments/Phase 2 真实语料验收报告.md`；登记 **TD-35/36/37**；全套 **2261 passed / 1 skipped**，`ruff` / `mypy` 全绿 |
| **Phase 2 收官（2026-09-14）** | 用户裁决：①**不改 Prompt**（`opinion-prompt-v13` 保持冻结，TD-37 结案）；②报告新增《免责声明与适用范围》节（任务错配 / `UNKNOWN` 合规 / 能证明什么 / 验证移交 Phase 3）；③**TD-35/36/37 全部按「已知边界」归档、不做代码修补**（本质是任务定义差异，非代码缺陷）；④150 条基准产物归档至 `logs/archive/phase2_hf_benchmark/`（含 `MANIFEST.md`：语料 / 结果 / 四条裁决 / 复现命令 / sha256）；⑤`docs/05` 标记 **Phase 2 已通过**，Phase 3 **待人工审核报告后启动**；真实观点提取验证（中文快讯 19 条或真实博主帖子）与标题级情感分类（News Alpha）移交 Phase 3 |
| Prompt 迭代 v2→v5（2026-09-13，人工裁决落地） | 按用户二次裁决完成 `opinion-prompt` 四次迭代并**用同一批 20 条真实 API 逐版验证**（合计 ≈ $0.023）：①**TD-24 修掉**：12 条 few-shot 全部换成语料外自造句，实测最长公共子串 **≤ 9 字符**，新增 LCS 回归测试机械拦截数据泄漏；②**TD-25 修掉**：`docs/10 §4.9` 新增规则 9「information_type 决策阶梯」（**操作优先，驱动决定分类**：L1 持仓→POSITIONING、L2 宏观→MACRO、L3 情绪→SENTIMENT、L4 引用/复盘→OTHER、L5 无驱动时的操作/纯图表→TECHNICAL；L1~L4 优先于 L5），人工子集信息类型准确率 **46.2% → 92.3%**、100 格 `wrong_value` **8 → 1**；③**TD-26 修掉**：`no_opinion` 语义二次裁决（宏观播报 = `UNKNOWN` + `MACRO` + `no_opinion=true`，`opinions=[]` 只留给"连信息类型线索都没有"的帖子），试点**无观点率 10% → 0%**；④v3 补「观望→FLAT、情绪≠FLAT」（`§4.1`），v5 补 L3 判别线（情绪须为主旨或**语句带方向含义**）；⑤新增对比交付文档 `docs/experiments/Phase2_LLM试点_v1-v5对比.md`（逐字段五版对比 + 条件句/引用句/多目标逐帖原文与原始 JSON）；⑥登记 **TD-27**（残余 1 格 `mock-post-0009` 判别线 + `mock-post-0028` 的 `15M` 与 `§4.2`「短线→1H」冲突，需一句话裁决）；新增测试 22 项（prompt 模块），全套 **1099 passed / 1 skipped**；`ruff` / `mypy`（66 files）全绿 |
| 最终交付（2026-09-13，全量 200 + 基线对比 + git 初始化） | ①**全量 200 条真实 API 运行**（`llm-deepseek-v6`）：人工子集核心四字段 **27.8% → 100%**（方向 100%、周期 0%→100%、止损 0%→100%、目标位 25%→100%）、信息类型 **40.7% → 87.4%**；逐格**修复 385 / 回归 14**；成本 **$0.0346**（180 次调用、缓存命中 93.8%），API 失败 0、解析失败 0、confidence 非法 0；②新增 `scripts/compare_extractor_baselines.py` + `tests/unit/test_baseline_comparison.py`（13 项）→《Phase 2 基线对比报告.md》（1000 格对齐、0 口径问题、含修复/回归逐格清单与 `docs/08 §5` PASS/FAIL）；③按人工最终裁决改判 `mock-post-0009` 金标准为 `TECHNICAL`（`provenance/reviewer` 留痕 + 改前版本归档）并把「短线思路 → `15m`」写入 `docs/10 §4.2`（**TD-27 关闭**）；④**登记 TD-28**（信息类型残余 17 格 = 驱动型文本 + 操作价位 + 阶梯缺 `NEWS` 级；路径 A/B 待一句话裁决）；⑤`git init` + 首次提交（`.gitignore` 覆盖 `.env`/`logs/`/`*.db`，提交前用 `git status --porcelain` 核对**无密钥、无数据、无缓存**入库）；测试 1099 → **1380 passed / 1 skipped**，`ruff` / `mypy`（67 files）全绿；未新增迁移、未改 schema、未新增依赖 |
| 全量重跑 + 最终对比报告（2026-09-13，人工二次裁决「路径 B」） | ①`opinion-prompt-v7 → v13` 共 7 次迭代（每次都**重跑并留档**，见 `docs/experiments/Phase2_LLM_Prompt迭代对比_v1-v6.md（v7~v13 的逐版结论见本行与 §3 TD-28）`）：补 **`L2.5` 消息→`NEWS`**、L3 增加「交易理由 vs 背景附注」两个例外与判别线、宏观驱动须为具体变量、horizon 规则经实测**回退**到 v6 版本；②`docs/10 §4.9` 规则 9 阶梯重写（L1 持仓 > L2 宏观 > L2.5 消息 > L4/L2.5 例外 > L3 操作价位 > L5 引用/复盘 > L6 兜底），`§4.5` 旧优先级标注**以 §4.9 为准**，`§4.2` 补「短线思路→15m」并注明**不可再细化**（实测回退经验）；③**全量 200 条定版结果**（`llm-deepseek-v13`，人工子集）：方向 100%、周期 100%、止损 100%、目标位 100%、**信息类型 91.9%**，相对正则 **+0/+100/+100/+75/+51.1 pp**，逐格**修复 380 格、回归 0 格**；成本 180 次调用 **$0.0353**（缓存命中 94.8%），失败 0；④`scripts/compare_extractor_baselines.py` 新增「**人工 ↔ 模型 对齐案例**」章节：5.1 模型纠正人工（`mock-post-0009`：`SENTIMENT→TECHNICAL`，已对齐）、5.2 人工纠正模型（11 格逐格原文）；⑤登记 **TD-29**（口径边界待真实语料复核）；测试 13 → **17 项**（对比脚本），全套 **1380 → 1891 passed / 1 skipped**，`ruff` / `mypy`（67 files）全绿 |
| 证据 operator 工作流（2026-09-22，GOLD-007） | 新增 `src/evidence/workflow.py`（写入门禁 + 脱敏隔离摘要）+ `scripts/evidence_operator.py`（单入口 `workflow`：`template → preflight → quarantine → intake → recheck`；单步 `template/preflight/quarantine/intake/recheck/author-chain`；默认 dry-run / 零网络 / 零写入，写入前二次完整 Evidence Gateway 校验，隔离行永不进台账）+ `src/evidence/author_chain.py`（**gateway-only** 作者归属链：只消费 `evidence-intake-v1` + `scope=author` + `oos_eligible=true`，普通 CSV 无法绕过；身份冲突跳过不覆盖；账号 `enabled=false`）；新增 23 项测试（单元 9 + 集成 14）；登记 **TD-49**；**未解除** `PHASE3_3_DATA`（真实授权证据仍需业务方提供 + 人工核验） |
| 证据人工交接包（2026-09-22，GOLD-008） | 新增 `src/evidence/handoff.py`（**只读**交接包：`ScopeGap` / `ChecklistItem` / `ExcludedEvidence` / `EvidenceHandoffReport`；稳定 JSON + 人类可读 Markdown；复用 `src.monitoring.evidence_readiness` 唯一口径，**不复制阈值算法**）+ `scripts/evidence_handoff.py`（默认只读 / 零网络 / 零写入；`--input` 仅 dry-run；唯一写文件开关为 `--out`；退出码 `5` 表示仍 BLOCKED）；量化 Author/News `eligible`/`required`/`remaining`、coverage gap、source-share 可评估性、主要隔离原因码；六类人工证据 checklist（authorization / provenance / published_at / collected_at / availability / identity）；模板 / Mock / 示例 / 历史 CSV 醒目标记为不计资格；新增 16 项测试（单元 10 + 集成 6）；登记 **TD-50**；**未解除** `PHASE3_3_DATA`（仅减少人工交接摩擦，Phase 切换仍须 L3 人工 Gate） |
| readiness 状态变更通知（2026-09-22，GOLD-009） | 新增 `src/evidence/readiness_watch.py`（**只读**通知核心：`ReadinessSnapshot` / `WatchEvent` / `detect_changes` / 原子写 `write_snapshot_state` + `write_events`；确定性脱敏白名单快照 + SHA-256 指纹（`generated_at` 不参与指纹）+ 幂等变化检测 `FIRST_SNAPSHOT` / `BLOCKER_GAP_CHANGED` / `REASON_CODES_CHANGED` / `READY_FOR_HUMAN_REVIEW_ENABLED` / `REVOKED`；复用 `src.evidence.handoff` / `src.monitoring.evidence_readiness` 唯一口径，**不复制阈值算法**）+ `scripts/evidence_readiness_watch.py`（默认只读 / 零网络 / 零写入；`--state` 只读、损坏则安全失败退出 `4` 且不写任何输出；只有显式 `--out` / `--events` 才**原子**落盘快照与事件；不接邮件 / 短信 / Webhook / 第三方推送）；新增 19 项测试（单元 12 + 集成 7）；登记 **TD-51**；**未解除** `PHASE3_3_DATA`（通知层只减少盯盘 / 轮询，`ready_for_human_review=true` 仍须 L3 人工 Gate） |
| readiness 周期 tick runner（2026-09-23，GOLD-010） | 新增 `src/evidence/readiness_runner.py`（**纯本地单次 tick** 核心：`SingleInstanceLock`（OS 级独占文件锁 `msvcrt` / `fcntl`，owner / pid / acquired_at 可审计、**绝不删除 / 绝不改写**活动锁、陈旧锁安全接管并留 `recovered_stale` + `previous_owner` 审计痕迹）+ `TickPaths` / `TickReport` / `run_tick` + `build_snapshot_from_session`（**只读**台账，显式 `rollback`）+ `load_event_journal` / `write_event_journal`（**有界保留**最新 200 条且本次新事件永不丢弃）+ `write_status` / `render_tick_summary` / `exit_code_for`；写入顺序**事件优先** → state → status，全部**原子写**）+ `scripts/evidence_readiness_runner.py`（`--work-dir` 必填的单次 tick CLI；零网络、零数据库写入、**无常驻循环**、不接第三方推送、不自动修改 OS 计划任务；退出码 `0` 达标（仍需 L3 人工 Gate）/ `2` 参数 / `3` 不可写 / `4` state 或事件日志损坏 / `5` 预期 BLOCKED / `6` 锁冲突 / `7` 资格计算失败，失败路径零写入并保留旧 state）；复用 `src.evidence.readiness_watch.py` 的口径与原子写原语（`atomic_write_text` 改为公开导出，不各写一套）；新增 30 项测试（单元 23 + 集成 7，含真实 CLI 冒烟与源码守卫）；登记 **TD-52**；**未解除** `PHASE3_3_DATA`（runner 只把盯盘自动化，达标最多 `ready_for_human_review=true`，Phase 切换仍须 L3 人工 Gate） |
| 本地 inbox 发现与预检（2026-09-23，GOLD-011） | 新增 `src/evidence/inbox.py`（**纯本地只读**的 Evidence Inbox 发现与预检核心：`InboxStatus` / `InboxReasonCode`（复用 `ReasonCode` 已有一致的字符串值）/ `ManifestFileRef` / `CandidatePackage` / `PendingEntry` / `PendingRegister` / `InboxPreflightReport`；`scan_inbox` 只读扫描**显式目录的直接子目录**，`run_inbox_scan` 仅在你显式给 `--out` 时先取**单实例锁**再原子落盘 pending；`_layout` 逐文件算 SHA-256 并**不跟随符号链接**，`_package_fingerprint` 只由内容摘要与结构标记派生（不含文件名 / mtime / 绝对路径 / 扫描时间）；`_check_files` 拒绝绝对路径 / `..` / 多级路径 / 未声明文件且**绝不越界读取**；`_preflight_rows` 直接调用 `src.evidence.validation.assess_row`（`evidence-intake-v1`）复用 gateway 的隔离 / 授权 / 时间 / availability 口径，再叠加"无可用行 / 存在隔离行 / 缺 OOS 证据 / 模板示例"整包 fail-closed；`_sensitive_reasons` 只记录凭据类**键名**、引用类字段用 `safe_url` 去 query）+ `scripts/evidence_inbox.py`（`--inbox-dir` 必填、`--state` 只读、`--out` 唯一写开关、`--lock` 可选、`--as-of` 必带时区、`--json`；退出码 `0` 有候选（仍需人工 + 显式 intake）/ `2` 参数 / `3` inbox 或输出不可用（含 `--out` 写进 inbox 的拒绝）/ `4` 既有 pending state 损坏 / `5` 无候选（预期 BLOCKED）/ `6` 锁冲突，失败路径 stdout 为空、stderr 脱敏、零写入）；`src/evidence/validation.py` 的引用校验公开为 `valid_reference` 供预检复用**同一**口径；`src/evidence/__init__.py` 导出新 API；新增 **65 项**测试（单元 55 + 集成 10：空 inbox / 合法 JSONL+CSV / 缺 manifest / manifest 损坏 / 缺必填声明 / 版本不符 / 摘要不一致 / 未声明文件 / 路径穿越 / 绝对路径 / 符号链接 / 示例模板 / 无可用行 / 部分隔离 / 重复扫描幂等 / 内容变更 / 状态变化 / mtime 与扫描时间不参与指纹 / 原子写 / 损坏 state / 锁冲突 / 防敏感信息泄漏 / 源码守卫 + 真实子进程 CLI 冒烟；全部临时目录、零网络、零数据库）；登记 **TD-53**；**未解除** `PHASE3_3_DATA`（inbox 只是"摆放 + 预检"入口，`preflight_pass` 仍须人工确认与显式 `evidence_operator workflow --no-dry-run`，Phase 切换仍须 L3 人工 Gate） |
| 人工复核决策与审计（2026-09-23，GOLD-012） | 新增 `src/evidence/review.py`（**纯本地**人工复核决策与审计核心：`ReviewDecision`（`APPROVE` / `REJECT` / `NEEDS_CHANGES`）/ `ReviewReasonCode`（受控词表，`REASON_CODES_BY_DECISION` 按决策分组）/ `InvalidationReason` / `ReviewRecord`（最小审计元数据：reviewer 非敏感标识、reviewed_at 带时区、reason_code、可选非敏感 note、supersedes、override、脱敏候选包摘要）/ `ReviewLedger`（**追加式**、`decision_id` 确定性派生、严格校验 `decision_id` / revision 连续性 / `supersedes` 链 / 安全字段不可被改写）/ `DecidedReview` / `ApprovedEntry` / `InvalidatedApproval` / `ApprovedIntakeList` / `ReviewReport`；`record_review_decision` 对**显式内容级指纹**记录决策：`APPROVE` 必须①指纹当前仍在扫描结果中、②`PREFLIGHT_PASS`、③非模板 / 示例 / Mock，完全相同的重复提交**幂等**，任何差异必须显式 `revision = 既有 + 1` + `override`（否则 fail-closed，**绝不静默覆盖**，历史全部保留）；`build_approved_intake_list` 在**当前**扫描结果上**重新验证**每条批准（失效 → `CANDIDATE_MISSING` / `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`）；`run_review` 默认只读，只有显式 `out_path` 才先取 GOLD-010 的**单实例锁**再原子落盘 ledger（复用 GOLD-009 的 `atomic_write_text`），`approved_out_path` 才写批准清单）+ `scripts/evidence_review.py`（`--inbox-dir` 必填、`--ledger` 只读、`--out` 唯一 ledger 写开关、`--approved-out` 批准清单、`--decision` / `--fingerprint` / `--reviewer` / `--reason-code` / `--note` / `--reviewed-at` / `--revision` / `--override` / `--lock` / `--as-of` / `--json`；退出码 `0` 决策已记录（或幂等重复）/ `2` 参数或词表错误 / `3` inbox 或输出不可用（含 `--out` 写进 inbox 的拒绝）/ `4` ledger 损坏、决策冲突或目标不满足门禁（fail-closed，零写入）/ `5` 只读运行且无仍成立的批准（预期 BLOCKED）/ `6` 锁冲突，失败路径 stdout 为空、stderr 脱敏）；`src/evidence/inbox.py` 的路径守护公开为 `ensure_outside_inbox` 供复核层复用**同一**口径（行为不变）；`src/evidence/__init__.py` 导出新 API；新增 **62 项**测试（单元 52 + 集成 10，含真实子进程 CLI 冒烟；全部临时目录、零网络、零数据库；覆盖 approve / reject / needs_changes 门禁、受控词表、幂等、冲突必须显式 revision + override、内容变化 / 候选消失 / 预检回退导致批准失效、ledger 篡改与断链、原子写与多线程并发、锁冲突、脱敏、复核元数据不得冒充证据时间、源码守卫）；登记 **TD-54**；**未解除** `PHASE3_3_DATA`（人工批准只是预审，落库仍须 `evidence_operator workflow --no-dry-run`，Phase 切换仍须 L3 人工 Gate） |
| 执行后的 Intake Receipt 与资格复核审计（2026-09-23，GOLD-014） | 新增 `src/evidence/intake_receipt.py`（**纯本地只读**的执行后收据核验层：`IntakeReceiptStatus` / `ReceiptVerificationCode`（17 个稳定原因码）/ `ReceiptViolation` / `PlanEntryDocument` / `IntakePlanDocument`（GOLD-013 plan 严格只读视图：文档标识 / schema / 契约版本 / 安全字段不可被削弱 / 条目白名单键 / 计数自洽 / 拒绝任何证据时间键）/ `OperatorResult`（`EvidenceIntakeReport.to_dict()` manifest 严格只读视图）/ `QualificationRecheck`（`phase33_qualification_recheck` 严格只读视图）/ `ReceiptEntry` / `IntakeReceipt`（内容级 `receipt_id`；四个布尔 + 安全字段恒定）；`load_intake_plan_document` / `load_operator_result` / `load_qualification_recheck` / `compute_receipt_id`（只由策略块 + `plan_id` + 批准条目 + 执行摘要 + 复核摘要 + 两个布尔派生，**不含** `receipt_at`）/ `verify_intake_receipt` / `build_intake_receipt`（**复用** GOLD-013 的 `build_intake_plan` 重新绑定，**不复制、不降低**任何资格规则）/ `run_intake_receipt`（默认只读；只有显式 `out_path` 才先取 GOLD-010 单实例锁再原子落盘收据）/ `render_intake_receipt_summary` / `exit_code_for`）+ `scripts/evidence_intake_receipt.py`（`--inbox-dir` / `--ledger` / `--approved-list` / `--plan` 必填、`--operator-result` 可重复、`--recheck`、`--out` 唯一写开关、`--lock` / `--as-of` / `--json`；**没有**任何 intake / `--no-dry-run` 参数；退出码 `0` 执行与复核绑定成功 / `2` 参数 / `3` inbox 或输出不可用（含写进 inbox 的拒绝）/ `4` plan、执行结果、复核结果或 ledger 损坏被篡改或核验不通过（fail-closed，零写入）/ `5` 无仍成立的批准（预期 BLOCKED）/ `6` 锁冲突）；fail-closed 覆盖 plan stale / fingerprint drift / review override / 条目摘要变化 / 执行结果缺失或 dry-run 或零落库或计数矛盾或输入内容不符或 scope 不符或用了未批准输入或覆盖不全 / recheck 缺失或早于执行或声称 blocker 已解除 / 未来时间；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络、零新增依赖 / migration**；`src/evidence/__init__.py` 导出新 API；新增 **102 项**测试（单元 91 + 集成 11，集成用例先跑**真实** operator 落库与**真实** qualification recheck 再核验收据，含真实子进程 CLI 冒烟，全部临时目录 / 临时 SQLite / 零网络）；登记 **TD-56**；**未解除** `PHASE3_3_DATA`（收据 ≠ 资格，Phase 切换仍须 L3 人工 Gate） |
| L3 人工决策记录与防伪审计（2026-09-23，GOLD-016） | 新增 `src/evidence/decision_record.py`（**纯本地、显式人工输入**的 L3 人工决策记录：`HumanDecision`（`approve` / `reject` / `needs_changes`）/ `DecisionRecordCode`（21 个稳定原因码；与 GOLD-015 同义者**沿用**其字符串）/ `PacketBinding`（GOLD-015 packet 只读绑定：文档身份 / 安全字段 / 缺口算术 / 三个布尔合取 / readiness 指纹同源 / 收据与 recheck 绑定 / 计数自洽 / 递归禁止证据时间键 / 禁止未来时间，任一不一致即 fail-closed）/ `HumanDecisionRecord`（内容寻址 `record_id`；`human_decision_recorded` / `human_decision` / `packet_verified` 三个独立事实，`data_qualification_passed` / `phase_transition_allowed` / `phase_transition_executed` 恒 false、`blocker_active` / `human_gate_required` 恒 true、`human_gate_level` 恒 `L3` 硬编码）/ `DecisionRecordVerification`（防伪核验结论）；`load_decision_packet` / `build_decision_record` / `compute_record_id`（只由策略块 + 人工决策 + reviewer + note / reason_code + revision / supersedes + packet 绑定派生，**不含**任何审计时间）/ `verify_decision_record`（重新推导 `record_id`、与**当前** packet 比对 `packet_id` / 内容摘要、判定当初的 `approve` 是否仍成立）/ `run_decision_record`（默认只读；只有显式 `out_path` 才先取 GOLD-010 单实例锁、锁内**重新**核验 packet 防 TOCTOU、再检查既有记录冲突、最后**原子**写）/ `render_decision_record_summary` / `decision_record_exit_code_for`）+ `scripts/evidence_decision_record.py`（`--packet` / `--decision` / `--reviewer` 必填，`--note` / `--reason-code` / `--revision` / `--supersedes` / `--out` / `--lock` / `--as-of` / `--json` 与纯只读 `--verify-record`；退出码 `0` 已记录 / `2` 参数 / `3` 路径或输出不可用（含把记录写到 packet 文件上的拒绝）/ `4` 缺失 / 损坏 / 篡改 / stale / 冲突 / `record_id` 不一致或防伪核验不通过（fail-closed，零写入）/ `5` `approve` 被拒（packet 不可提交，预期 BLOCKED）/ `6` 锁冲突）；人工身份只接受非敏感 label（拒绝空值 / 超长 / 非法字符 / 疑似凭据），note / reason-code 走 `src.common.redaction` 脱敏 + 限长；**绝不写数据库、绝不调用 intake / commit、绝不修改 `PROJECT_STATE`、绝不解除 blocker、零网络、零新增依赖 / migration**；`src/evidence/__init__.py` 导出新 API；新增 **206 项**测试（单元 202 + 集成 4，集成用例跑**真实** operator 落库 / recheck / handoff / GOLD-015 决策包后再记录人工决策与防伪核验，含真实子进程 CLI 冒烟，全部临时目录 / 临时 SQLite / 零网络）；登记 **TD-58**；**未解除** `PHASE3_3_DATA`（人工决策记录 ≠ 资格通过，Phase 切换仍须 L3 人工 Gate） |
| Evidence Package / Manifest Builder（2026-09-23，GOLD-017） | 新增 `src/evidence/package_builder.py`（**纯本地、显式人工输入、默认 dry-run** 的 evidence package manifest builder：`EvidencePackageCode`（复用 GOLD-011 inbox / `evidence-intake-v1` 同义原因码 + builder 专有码）/ `ManifestWriteStatus` / `PreflightOutcome` / `ManifestFileEntry` / `RowPreflight` / `EvidencePackagePreview` / `build_manifest_preview` / `preflight_rows` / `manifest_bytes` / `manifest_digest` / `run_package_builder` / `render_package_summary` / `exit_code_for`；manifest 字段 / 允许键 / 必填项 / 格式词表**复用** GOLD-011 的 `MANIFEST_REQUIRED_FIELDS` / `ALLOWED_MANIFEST_KEYS` / `FILE_ENTRY_REQUIRED_FIELDS` / `SUPPORTED_FILE_FORMATS` 与 `evidence-intake-v1`，引用校验复用 `valid_reference`、逐行预检复用 `read_input_file` + `assess_row`，**不复制、不降低**任何资格规则；每个文件按**原始字节**计算 SHA-256、`files` 按规范化相对路径确定性排序、同输入 byte-stable 且 manifest **没有任何时间字段**；拒绝绝对路径 / `..` / 多级路径 / 符号链接 / 目录 / `manifest.json` 自引用 / 未支持格式 / 重复或大小写冲突 / 包内未声明文件 / 示例合成名称 / 敏感值（凭据键名、疑似 blob、可被脱敏规则命中的取值）；默认零写入，`--out` 只能写 `<package-dir>/manifest.json`（单实例锁 + 原子写 + 写后复读自检；锁文件**刻意放在 package 目录之外**），既有 manifest 逐字节一致 → 幂等、内容不同 → `MANIFEST_CONFLICT`，**没有** `--force` / `--overwrite`）+ `scripts/evidence_package.py`（元数据与 `--file` 必填、`--json`、默认 dry-run；**没有**任何 intake / `--no-dry-run` 参数）；**绝不**推断授权 / 时间 / availability / OOS、**绝不**移动 / 删除 / 改写原始 evidence、**绝不**自动 intake、**绝不**写数据库、零网络、零新增依赖 / migration；`src/evidence/__init__.py` 导出新 API；新增 **65 项**测试（单元 57 + 集成 8，集成用例先用 builder 生成 manifest 再用**真实** GOLD-011 inbox scanner 预检，并验证坏包 / 篡改包仍 fail-closed、builder 绝不静默覆盖，含真实子进程 CLI 冒烟，全部临时目录 / 零网络 / 零数据库）；登记 **TD-59**；**未解除** `PHASE3_3_DATA`（生成 manifest ≠ 授权已核验 ≠ 资格通过，Phase 切换仍须 L3 人工 Gate） |
| L3 人工决策包（2026-09-23，GOLD-015） | 新增 `src/evidence/decision_packet.py`（**纯本地只读**的 L3 人工决策包：`DecisionPacketStatus` / `PacketVerificationCode`（13 个稳定原因码；与 GOLD-014 同义的核验失败**沿用**其稳定原因码）/ `PacketViolation` / `HandoffDocument`（GOLD-008 handoff 严格只读视图：文档标识 / schema / 契约版本 / 安全字段 / `thresholds` 必须等于当前唯一来源 / 缺口与检查**算术自洽** / 拒绝任何证据时间键）/ `ReadinessStateDocument`（GOLD-010 `readiness_state.json` 只读视图；必须与 handoff **同源**）/ `ReceiptDocument`（GOLD-014 收据只读视图 + 内容寻址交叉核对）/ `DecisionPacket`（内容级 `packet_id`；五个布尔 + 安全字段恒定）；`load_handoff_document` / `load_readiness_state_document` / `load_intake_receipt_document` / `verify_decision_packet`（**复用** GOLD-013/014 的重新绑定口径，**不复制、不降低**任何资格规则）/ `build_decision_packet` / `compute_packet_id`（只由策略块 + handoff / readiness 摘要 + `plan_id` + 收据摘要 + recheck 摘要 + 五个布尔 + 状态派生，**不含** `generated_at`）/ `run_decision_packet`（默认只读；只有显式 `out_path` 才先取 GOLD-010 单实例锁再原子落盘 packet）/ `render_decision_packet_summary` / `exit_code_for`）+ `scripts/evidence_decision_packet.py`（`--handoff` / `--inbox-dir` / `--ledger` / `--approved-list` / `--plan` 必填、`--readiness` / `--receipt` / `--operator-result` / `--recheck` 可选、`--out` 唯一写开关、`--lock` / `--as-of` / `--json`；**没有**任何 intake / `--no-dry-run` 参数；退出码 `0` 可提交 L3 人工 Gate / `2` 参数 / `3` inbox 或输出不可用（含写进 inbox 的拒绝）/ `4` 任一输入缺失 / 损坏 / stale / 篡改或核验不通过（fail-closed，零写入）/ `5` 不可提交（预期 BLOCKED）/ `6` 锁冲突）；fail-closed 覆盖 handoff 结构 / 算术 / thresholds / 安全字段 / 证据时间键、readiness 快照不同源或指纹校验失败、plan stale / fingerprint drift / review override、执行结果缺失或被改写、recheck 缺失或早于执行或结论不一致、handoff stale（早于最近一次显式落库）、收据 `receipt_id` 不自洽或与当前重新绑定结果不一致、未来时间；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络、零新增依赖 / migration**；`src/evidence/__init__.py` 导出新 API；新增 **83 项**测试（单元 71 + 集成 12，集成用例先跑**真实** operator 落库、**真实** recheck 与**真实** handoff 再核验决策包，含真实子进程 CLI 冒烟，全部临时目录 / 临时 SQLite / 零网络）；登记 **TD-57**；**未解除** `PHASE3_3_DATA`（决策包 ≠ 资格，Phase 切换仍须 L3 人工 Gate） |

| 最终写入前 intake plan 门禁（2026-09-23，GOLD-013） | 新增 `src/evidence/intake_plan.py`（**纯本地只读**的 approved-for-explicit-intake **intake plan** 与最终写入前门禁：`IntakePlanStatus` / `PlanVerificationCode`（14 个稳定原因码）/ `PlanViolation` / `PlanEntry` / `OperatorHandoffStep` / `ApprovedListDocument`（批准清单严格只读视图：结构自洽 + 安全字段与 `approval_scope` 不可被削弱 + 拒绝任何证据时间字段）/ `IntakePlan`（内容级 `plan_id`、`handoff` 字符串模板、四个安全字段 + `auto_intake_allowed` / `writes_database` 恒为 false 硬编码）；`load_approved_intake_list` / `verify_intake_plan_inputs` / `build_intake_plan`（**复用** GOLD-012 的 `build_approved_intake_list` 做重新验证，**不复制、不降低**任何资格规则）/ `compute_plan_id`（只由策略块 + 批准条目 + ledger 最新决策摘要派生，**不含** `generated_at`）/ `intake_plan_handoff`（每个证据文件一条显式命令模板，必带 `--no-dry-run` 与显式 `--input`）/ `run_intake_plan`（默认只读；只有显式 `out_path` 才先取 GOLD-010 单实例锁再原子落盘计划）/ `render_intake_plan_summary` / `exit_code_for`）+ `scripts/evidence_intake_plan.py`（`--inbox-dir` / `--ledger` / `--approved-list` 必填、`--out` 唯一写开关、`--lock` / `--as-of` / `--json`；**没有**任何 intake / `--no-dry-run` 参数；退出码 `0` 有仍成立的批准 / `2` 参数 / `3` inbox 或输出不可用（含写进 inbox 的拒绝）/ `4` 清单或 ledger 损坏被篡改或核验不通过（fail-closed，零写入）/ `5` 无仍成立的批准（预期 BLOCKED）/ `6` 锁冲突）；fail-closed 覆盖 stale 指纹 / 候选消失 / preflight 回退 / review override / approved-list tamper / synthetic evidence / 计数与失效集合不一致 / 计划时间早于批准 / 损坏 state；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络、零新增依赖 / migration**；`src/evidence/__init__.py` 导出新 API；新增 **57 项**测试（单元 47 + 集成 10，含真实子进程 CLI 冒烟，全部临时目录 / 零网络 / 零数据库）；登记 **TD-55**；**未解除** `PHASE3_3_DATA`（计划 ≠ 资格，落库仍须人工**显式** `evidence_operator workflow --no-dry-run` + `handoff` / `recheck` + L3 人工 Gate） |
| 包边界惰性导出与 Import-Order 门禁（2026-09-23，GOLD-018） | 修复 GOLD-017 任务外发现、纯净 HEAD 复现的 `src.monitoring` ↔ `src.evidence` **包初始化期循环导入**（`import src.monitoring` 后再进入 `src.evidence` 链 → `ImportError: cannot import name 'BatchQuantification' from partially initialized module 'src.monitoring.evidence_readiness'`；反向顺序却正常）：`src/evidence/__init__.py`（376 个公开名）/ `src/monitoring/__init__.py`（39 个公开名）改为 **PEP 562 惰性导出**（模块级 `__getattr__` + `__dir__`，包初始化阶段不加载任何子模块），惰性表只登记 `公开名 -> 定义子模块`（别名单独登记，**不复制**任何业务类 / 阈值 / 枚举 / 常量），`__all__` 逐字节未变、既有 `from src.evidence import X` / `from src.monitoring import Y` / `from src.<pkg> import <子模块>` 用法**完全兼容**；新增 **56 项**测试（单元 15 + 集成 41：9 种导入顺序的 fresh-subprocess 回归 + "包初始化不拉入对方包" + 公开名 `is` 同源 + 源码级禁止 eager 子模块导入 + 13 个证据 / 观测 CLI 的 import 与 `--help` 冒烟并以空 cwd 断言零写入）；登记 **TD-60**；**未改**任何资格算法 / 阈值 / 证据契约 / 安全字段，`PHASE3_3_DATA` **保持 BLOCKED** |
| 证据缺口只读诊断（2026-09-23，GOLD-026） | 新增 `src/monitoring/evidence_gap_diagnostic.py`（**只读**四态缺口诊断：`ReadinessClass`（`CODE_READY` / `EVIDENCE_MISSING` / `HUMAN_VERIFICATION_REQUIRED` / `GATE_BLOCKED`）/ `DiagnosticItem`（key / category / scope / readiness / machine_fact / human_next_step / current / required / missing_fields / missing_count / references / 证据时间窗，`mock_or_template_qualifies` 恒 false）/ `ManifestFact` / `ManifestDiagnostic`（复用 GOLD-011 `scan_inbox` 的**只读** manifest 字段级事实，`qualifies` 恒 false）/ `HumanStep` + `MANDATORY_HUMAN_STEPS`（6 步固定清单，阈值文本由 `src.alpha.evidence_gate` 常量格式化）/ `EvidenceGapDiagnostic`（顶层 `readiness` 恒 `GATE_BLOCKED`；`blocker_active` / `human_gate_required` 恒 true；`data_qualification_passed` / `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false，**硬编码**）/ `build_gap_diagnostic`（纯函数，25 个诊断项、确定性排序）/ `build_manifest_diagnostic`（纯映射）/ `load_gap_diagnostic`（只读台账 + 可选**本地** `--inbox-dir` 预检，缺目录 fail-closed）/ `render_gap_diagnostic_markdown`；**复用而不复制** readiness（GOLD-006）/ handoff（GOLD-008）/ inbox（GOLD-011）/ `evidence-intake-v1` 字段与原因码 / `src.alpha.evidence_gate` 阈值，**不新增、不降低**任何阈值，也未改动既有 evidence 模块与 `src/alpha/**`）+ `scripts/evidence_gap_diagnostic.py`（`--inbox-dir` / `--as-of` / `--json` / `--out`（唯一写开关，复用 `atomic_write_text` 原子写）；**没有**任何 intake / 写库 / `qualify` / `approve` / `advance` 参数；退出码 `0` 无实质证据缺口（gate 仍 BLOCKED）/ `2` 参数或输入错误 / `4` `--out` 不可写 / `5` 仍有实质证据缺口（预期 BLOCKED））+ `src/monitoring/__init__.py` 惰性导出 21 个新名字；**不伪造缺失字段**：缺失时只报告字段名与缺口计数，测试以正则断言"报告中除 `as_of` 外没有任何时间戳"并断言候选文件 mtime（2019-01-02）**绝不出现**；源码级守卫断言模块与 CLI 只 import 既有模块且不调用 `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / `commit`；新增 **33 项**测试（单元 25 + 集成 8，含"量化门槛全达标仍 BLOCKED 且只剩人工核验"、"合成候选永不 qualify"、"缺 `--inbox-dir` fail-closed"、"默认零写入 / 数据库零变化"），并把新 CLI 登记进 `tests/integration/test_evidence_cli_smoke.py`（13 个 `scripts/evidence_*.py`）；登记 **TD-61**；**未解除** `PHASE3_3_DATA`（诊断 ≠ 资格，Phase 切换仍须 L3 人工 Gate） |
| 人工证据 Intake Handoff 预检（2026-09-23，GOLD-027） | 新增 `src/evidence/intake_handoff.py`：**版本化** intake handoff schema + **纯本地只读**预检，把 GOLD-026 的缺口诊断转成"业务人员按单一契约提交真实授权 Author / News 材料"的可执行 handoff。`intake_handoff_schema()`（`kind=phase33_evidence_intake_handoff` / `schema_version=1` / `contract_version=evidence-intake-v1` / 目录契约（沿用 GOLD-011 inbox：`manifest.json` 声明 `files[path,sha256]`）/ Author + News 必需契约字段（`required_field_names`，Author 额外 `author_name` / `external_account_id`）/ 10 条材料契约 / 6 条独立时间语义要求 / 状态词表 / `preflight_pass` 的诚实语义；阈值**只引用** `src.alpha.evidence_gate`（经 `handoff.thresholds`），**不新增、不降低**任何门槛）；`MaterialSpec` + `MATERIAL_SPECS`（`evidence_records` / `source_identity` / `authorization_declaration` / `time_semantics` / `published_at` / `collected_at` / `effective_at` / `availability_oos` / `author_identity` / `phase33_data_gate`，category 与 GOLD-026 缺口分类同词表）；`IntakeStatus` **五态**（`MISSING` / `PRESENT_UNVERIFIED` / `HUMAN_VERIFICATION_REQUIRED` / `NON_QUALIFYING` / `GATE_BLOCKED`）+ `HandoffMaterial` / `PackageHandoff` / `IntakeHandoffDocument`（`preflight_pass` 只表示**结构完整**，`evidence_qualified` / `data_qualification_passed` / `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false，`blocker_active` / `human_gate_required` / `gate_blocked` 恒 true，**硬编码**；每个材料带 `present_fields` / `missing_fields` / `machine_fact` / `human_next_step`，`counts_toward_eligibility` 恒 false）+ `build_intake_handoff`（纯函数）/ `load_intake_handoff`（只读 `scan_inbox`；缺目录 fail-closed）/ `render_intake_handoff_markdown` / `unknown_contract_fields`（机器可检查"只引用既有契约字段"）+ `scripts/evidence_intake_handoff.py`（`--schema` 打印契约 / `--inbox-dir`（除 `--schema` 外必填）/ `--as-of` / `--json` / `--out`（**唯一**写开关，复用 `atomic_write_text`）；**没有**任何 intake / 写库 / `qualify` / `approve` / `advance` 参数；退出码 `0` 至少一个包结构完整（**不是**资格通过）/ `2` 参数或输入错误 / `4` `--out` 不可写 / `5` 没有任何结构完整的包（预期 BLOCKED））+ `src/evidence/__init__.py` 惰性导出 20 个新名字；**不伪造时间事实**（缺 `time_semantics` / `available_at` 时只报字段名与人工下一步，测试断言"除 `as_of` 外无任何时间戳"且 mtime 2019-01-02 绝不出现）+ **只读**（源码级禁 `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / 直接 `write_text`；测试断言候选包内容与 mtime 零变化）；新增 **45 项**测试（单元 32 + 集成 13，含 `--schema` 契约、Mock / 缺授权 / 缺独立时间语义 / 缺独立可用证据全部 fail-closed、"结构完整 ≠ 资格通过"、fresh subprocess 空 cwd 零写入），并把新 CLI 登记进 `tests/integration/test_evidence_cli_smoke.py`（14 个 `scripts/evidence_*.py`）；登记 **TD-62**；**未解除** `PHASE3_3_DATA`（预检 ≠ 资格，Phase 切换仍须 L3 人工 Gate） |
| 材料级人工核验凭证（2026-09-23，GOLD-028） | 新增 `src/evidence/human_verification_attestation.py`：**版本化** Human Verification Attestation + **纯本地只读**材料级人工核验，把 GOLD-027 的**结构完整预检**进一步转换为「每项关键材料是否被人工核验、由谁核验、依据哪条独立引用」的可审计凭证。`attestation_schema()`（`kind=phase33_human_verification_attestation` / `schema_version=1` / `contract_version=human-verification-attestation-v1` / 必核验材料清单**只引用** GOLD-027 `MATERIAL_SPECS` 的非 Gate 材料（覆盖 authorization / source / time_semantics / published_at / collected_at / effective_at / availability_oos，Author 额外 author_identity）/ 决策词表 `VERIFIED` / `REJECTED` / `NEEDS_CHANGES` / 稳定原因码 / 核验输入文件格式 / `all_required_verified` 的诚实语义；**不新增、不降低**任何门槛）；`VerificationDecision` + `MaterialDecision` + `VerificationInput`（`load_verification_input`：未知字段 / 重复材料 / 未知材料 / Gate 材料 / 非受控决策 / 非稳定 `reason_code` / 非独立引用（只接受 `https://` 或 `docs/legal/`）/ 疑似凭据 / naive 或未来 `reviewed_at` 一律 fail-closed）+ `MaterialAttestation` / `HumanVerificationAttestation`（内容寻址 `attestation_id` 由「硬编码策略块 + `revision` / `supersedes` + package 绑定 + 逐材料核验」派生、**不含**审计时间；`evidence_qualified` / `data_qualification_passed` / `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false、`blocker_active` / `human_gate_required` / `gate_blocked` 恒 true，**硬编码**）+ `build_attestation`（纯函数；核验输入声明的 fingerprint / handoff 内容身份 / scope 与**当前**候选目录不一致 → `PACKAGE_FINGERPRINT_MISMATCH` / `HANDOFF_CONTENT_MISMATCH` / `SCOPE_MISMATCH`；对 Mock / 合成 / `preflight_pass=false` / 非 `HUMAN_VERIFICATION_REQUIRED` 材料声明 `VERIFIED` → `NON_QUALIFYING_PACKAGE` / `PREFLIGHT_NOT_PASSED` / `MATERIAL_NOT_VERIFIABLE` fail-closed）+ `run_attestation`（默认零写入；只有显式 `out_path` 才先做既有凭证冲突检查（同 id 幂等，否则必须显式 `supersedes` + 更大 `revision`）再原子落盘；拒绝把凭证写进 `inbox_dir` 或覆盖核验输入）+ `verify_attestation`（纯只读**防伪核验**：重新推导 `attestation_id`、复核安全字段 / 计数 / 合取自洽 / 无证据时间键，任一被改写 → `ATTESTATION_TAMPERED`；重新绑定**当前**候选目录并检测 `PACKAGE_FINGERPRINT_DRIFT` / `PACKAGE_CONTENT_DRIFT` / `HANDOFF_CONTENT_DRIFT` / `PREFLIGHT_DRIFT` 与缺引用）+ `handoff_content_sha256` / `package_content_sha256` / `render_attestation_summary` / `exit_code_for` + `scripts/evidence_human_verification_attestation.py`（`--schema` / `--inbox-dir` / `--package` / `--verification` / `--as-of` / `--revision` / `--supersedes` / `--json` / `--out`（**唯一**写开关，复用 `atomic_write_text`）/ `--verify-attestation`（纯只读模式）；**没有**任何 intake / 写库 / `qualify` / `approve` / `advance` 参数；退出码 `0` 全部必核验材料已 `VERIFIED`（**不是**资格通过）/ `2` 参数或输入错误 / `3` 路径或输出不可用 / `4` 漂移 / 篡改 / 冲突 / 非法人工输入（fail-closed，零写入）/ `5` 未全部核验或不可核验（预期 BLOCKED））+ `src/evidence/__init__.py` 惰性导出 34 个新名字；**不伪造时间事实**（`reviewed_at` / `attested_at` 只是审计操作时间；除审计时点与人工 `reviewed_at` 外报告无任何时间戳，测试断言候选文件 mtime（2019-01-02）绝不出现）；源码级守卫断言模块与 CLI 只 import 既有模块且不调用 `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / 直接 `write_text`；新增 **59 项**测试（单元 41 + 集成 18，含「材料级核验 ≠ 资格通过」「缺授权证明 / 缺独立时间语义 / 缺 availability-OOS 全部 fail-closed」「Mock / 模板永不 qualify」「package / handoff 内容漂移使旧凭证失效」「凭证篡改 / 安全字段削弱 / 伪造 `all_required_verified` 全部 fail-closed」「重复幂等 + 显式 revision / supersedes」「候选证据内容与 mtime 零变化」），并把新 CLI 登记进 `tests/integration/test_evidence_cli_smoke.py`（15 个 `scripts/evidence_*.py`）；登记 **TD-63**；**未解除** `PHASE3_3_DATA`（材料级人工核验 ≠ 资格，Phase 切换仍须 L3 人工 Gate） |
| APPROVE 门禁绑定材料级核验凭证（2026-09-23，GOLD-029） | `src/evidence/review.py` 新增 `build_attestation_binding()`：新 `APPROVE` **必须**显式给出 GOLD-028 材料级人工核验凭证并逐项重新绑定核验（package fingerprint / package 内容身份 / handoff 内容身份 / `scope` / `preflight_pass` / `all_required_verified`），缺失 / 陈旧 / 漂移 / 被篡改 / 部分核验一律 `ReviewAttestationError`（退出码 `4`、零写入）；`ReviewRecord` 新增**最小** `attestation` binding 白名单（binding 版本 / GOLD-028 schema + contract 版本 / `attestation_id` / 凭证文档 `sha256` / package + handoff 内容身份 / `scope` / `all_required_verified`），**绝不**保存凭证正文 / 备注 / `reviewer` / 证据时间；`REJECT` / `NEEDS_CHANGES` 不需要凭证；旧 ledger 保持可读、`compute_decision_id` 口径不变、绝不原地迁移 / 重写；`build_approved_intake_list()` 用当前 inbox / handoff 重新验证 binding（`ATTESTATION_MISSING` / `ATTESTATION_INCOMPLETE` / `ATTESTATION_STALE` / `ATTESTATION_TAMPERED`）；CLI 新增 `--attestation`；`intake_plan` 显式映射四个原因码；新增 GOLD-029 单元 + 集成回归 |




