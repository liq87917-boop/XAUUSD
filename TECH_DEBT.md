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
