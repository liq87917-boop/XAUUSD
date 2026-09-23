# GOLD-AI · Phase 3 Alpha Lab

黄金智能交易研究与策略进化系统（研究型 / 回测型 / 模拟盘型）。
当前阶段：**Phase 3.3 数据资格阻塞**。Phase 3.0 数据底座与 Phase 3.1 Regime 已通过；
Phase 3.2 Technical / Macro 独立 OOS 基线均未达到预注册门槛；Phase 3.3 Author / News
尚未满足可信样本要求。当前正确状态是保留负面证据与门禁，不创建 Alpha / Prediction /
Strategy 事实，不进入实盘。

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
| unit / integration / data_quality / leakage 测试 | `tests/`（最终全量：**3379 passed + 1 skipped**；另有 `ruff` / `mypy` / PostgreSQL 范围边界门禁） |
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
| **采集后处理 Processor Pipeline**（normalize → timezone/effective_at → identity/dedup → validation/audit → `processed_items`，append-only + 幂等 + 坏数据隔离 + 摘要脱敏；RSS 采集路径已最小接线） | `src/processors/collection/`、`src/common/redaction.py`、`src/collectors/base.py`（`post_processor` 钩子） |
| **只读运行健康度 / 数据资格观测层**（窗口内 source 级运行与加工状态 + Phase 3.3 资格缺口机器可读输出；`--json` 稳定结构、默认 dry-run、输出脱敏，**不解除** `PHASE3_3_DATA`） | `src/monitoring/`、`scripts/report_collector_health.py` |
| **授权证据接收入口 Evidence Intake Gateway**（版本化契约 `evidence-intake-v1`：来源身份 / 时间语义 / 出处 / 授权声明 / 历史可用证据；默认 dry-run 与零网络、坏行隔离 + 稳定原因码、幂等且不覆盖历史事实，**不解除** `PHASE3_3_DATA`） | `src/evidence/`、`scripts/intake_evidence.py` |
| **证据就绪度 / 一键资格复核入口**（Author / News operator 模板，示例行显式标记 `record_kind=example`、`is_mock=true`，导入判 `SYNTHETIC_EVIDENCE` 隔离、永不计入台账；默认只读 preflight 量化 `accepted/quarantined/duplicate/conflict/not_oos_eligible` 与 Author/News 的 remaining gap；`recheck` 一键串联只读台账与 Phase 3.3 qualification report，**不解除** `PHASE3_3_DATA`） | `src/evidence/templates.py`、`src/monitoring/evidence_readiness.py`、`scripts/evidence_readiness.py`、`examples/evidence/` |
| **单入口 Evidence Operator 工作流**（GOLD-007：`template → preflight → quarantine → intake → recheck` 串联；默认 dry-run / 零网络 / 零写入，写入前二次完整 Evidence Gateway 校验 + 写入门禁，隔离行永不进台账；输出 Author/News remaining gap 与稳定原因码） | `src/evidence/workflow.py`、`scripts/evidence_operator.py` |
| **gateway-only 作者归属链**（GOLD-007：只消费 `evidence-intake-v1` + `scope=author` + `oos_eligible=true` 的记录；普通 CSV / 历史样本无证据块，**无法绕过** gateway；身份冲突跳过且不覆盖；新建 `author_accounts` 一律 `enabled=false`） | `src/evidence/author_chain.py`、`scripts/evidence_operator.py` |
| **Evidence 人工交接包**（GOLD-008：只读 / 默认 dry-run 的 `scripts/evidence_handoff.py`；机器可读 JSON + 人类可读 Markdown，量化 Author/News `eligible`/`required`/`remaining`、coverage gap、source-share 可评估性与主要隔离原因码；明确人工证据 checklist（authorization / provenance / published_at / collected_at / availability / identity），模板 / Mock / 示例醒目标记为不计资格；`data_qualification_passed` / `phase_transition_allowed` 恒为 false，**只减少人工交接摩擦、不解除** `PHASE3_3_DATA`） | `src/evidence/handoff.py`、`scripts/evidence_handoff.py` |
| **Evidence Readiness 状态变更通知**（GOLD-009：只读 / 默认 dry-run 的 `scripts/evidence_readiness_watch.py`；确定性脱敏快照指纹 + 幂等变化检测：首次快照 / BLOCKED 缺口变化 / reason-code 集合变化 / `ready_for_human_review` 双向变化；默认零写入、零网络，只有显式 `--out` / `--events` 才**原子**落盘快照与事件；不接邮件 / 短信 / Webhook / 第三方推送；`ready_for_human_review=true` 仍明确要求 L3 人工 Gate，`phase_transition_allowed` 恒为 false，**不解除** `PHASE3_3_DATA`） | `src/evidence/readiness_watch.py`、`scripts/evidence_readiness_watch.py` |
| **Evidence Readiness 单次本地 tick runner**（GOLD-010：`scripts/evidence_readiness_runner.py` **只做一次** tick，供 Windows Task Scheduler / 现有本地 orchestrator 等**外部定时器**调用；自带 OS 级**单实例锁**（owner / pid / 时间可审计、**绝不删除**活动锁）与陈旧锁安全接管；三类本地 artifact（snapshot state / 事件日志 / status）全部**原子写**且有界滚动，无变化零重复事件；损坏 state / 锁冲突 / 资格计算失败一律 **fail-closed** 并保留旧 state；零网络、零数据库写入、**不自带常驻循环**、不自动改 OS 计划任务；四个安全字段恒定，**不解除** `PHASE3_3_DATA`） | `src/evidence/readiness_runner.py`、`scripts/evidence_readiness_runner.py` |
| **Evidence 本地 Inbox 发现与预检**（GOLD-011：`scripts/evidence_inbox.py` **只读**扫描显式 `--inbox-dir` 的直接子目录；候选包必须由 `manifest.json` 显式关联证据文件（`evidence_type` / `source` / `authorization_reference` / `time_semantics` / `availability_semantics` / `historical_oos_applicable` / `files[].path`+`sha256`）；逐文件实测 SHA-256 并核对声明，候选包指纹只由内容摘要与结构标记派生（不含文件名 / mtime / 绝对路径 / 扫描时间），pending 清单以指纹为键**幂等**且**原子写**；路径穿越 / 绝对路径 / 符号链接 / 未声明文件 / 摘要不一致 / 凭据泄漏 / 模板示例一律 **fail-closed**；逐行预检复用 Evidence Gateway 的 `assess_row`，**绝不移动 / 删除原始证据、绝不自动 intake、零网络、零数据库写入**；`preflight_pass` 只进**人工确认队列**，四个安全字段恒定，**不解除** `PHASE3_3_DATA`） | `src/evidence/inbox.py`、`scripts/evidence_inbox.py` |
| **Evidence Inbox 人工复核决策与审计**（GOLD-012：`scripts/evidence_review.py` 对**显式内容级指纹**记录 `approve` / `reject` / `needs_changes`；`APPROVE` 必须①该指纹**当前仍在** inbox 扫描结果中、②当前预检 `PREFLIGHT_PASS`、③**不是**模板 / 示例 / Mock，内容变化 → 新指纹（**旧批准绝不继承**），候选消失 / 预检回退 / ledger 损坏 / 元数据含凭据一律 **fail-closed**；追加式 ledger（确定性 `decision_id`、完全相同决策**幂等**、任何差异必须显式 `--revision` + `--override`、**绝不静默覆盖**、原子写 + 单实例锁）与**脱敏** approved-for-explicit-intake 清单（生成前在当前扫描结果上**重新验证**，失效批准进 `invalidated`）；`--out` 是唯一 ledger 写开关，`--ledger` 只读；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除原始 evidence、零网络**；复核元数据**不是**证据时间；四个安全字段恒定，**不解除** `PHASE3_3_DATA`） | `src/evidence/review.py`、`scripts/evidence_review.py` |
| **Evidence 显式 Intake Receipt 与资格复核审计闭环**（GOLD-014：`scripts/evidence_intake_receipt.py` 把 GOLD-013 plan、**当前** inbox / review 状态、人工**显式** Evidence Operator 执行结果（`--no-dry-run --manifest` 产物）与随后的 qualification recheck 绑定成**确定性、脱敏、内容寻址**（`receipt_id`）的**只读**收据 —— plan 结构自洽 + plan 必须**当前仍成立**（用**当前** inbox + ledger + 批准清单重新算出的 `plan_id` 与条目摘要完全一致，plan stale / fingerprint drift / 候选消失 / preflight 回退 / **review override** 一律 fail-closed）+ 执行结果必须按**内容 SHA-256** 覆盖每条批准且 `dry_run=false` / `persisted>=1`（dry-run / 空落库 / 计数自相矛盾 / scope 不符 / 用了**未被批准**的输入 / 覆盖不全 → fail-closed）+ recheck 必须存在且**不早于**显式执行（缺失 / 早于执行 / 声称 blocker 已解除 → fail-closed）；明确区分四个布尔：`intake_executed` / `receipt_verified` 恒与 `data_qualification_passed` / `phase_transition_allowed`（**恒为** false）**互不蕴含**；`receipt_id` 只由（策略块 + plan_id + 批准条目 + 执行摘要 + 复核摘要 + 两个布尔）派生（**不含** `receipt_at`；同输入幂等、同审计时点逐字节稳定）；只有显式 `--out` 才**原子**落盘收据本身（先取单实例锁）；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除 / 改写原始 evidence、零网络**；四个安全字段恒定，**不解除** `PHASE3_3_DATA`） | `src/evidence/intake_receipt.py`、`scripts/evidence_intake_receipt.py` |
| **Evidence Qualification L3 人工决策包**（GOLD-015：`scripts/evidence_decision_packet.py` 把最新 readiness / handoff（GOLD-008/010）、批准清单与 GOLD-013 intake plan、GOLD-014 verified receipt 与 qualification recheck 聚合成**确定性、脱敏、内容寻址**（`packet_id`）的**只读**决策包 —— handoff 必须结构自洽（内部算术 / `thresholds` 必须等于**当前**唯一阈值来源 / 安全字段不可被削弱 / 拒绝任何证据时间键）、`--readiness` 快照必须与 handoff **同源**（含指纹）、plan stale / fingerprint drift / review override / 执行结果缺失或被改写 / recheck 缺失或早于执行或结论不一致 / handoff 早于最近一次显式落库（stale）一律 fail-closed、可选 GOLD-014 收据文件再做一次内容寻址交叉核对（`receipt_id` / `plan_id` / 指纹 / review revision / 执行摘要 / recheck 摘要不一致 → 稳定原因码）；显式区分 `evidence_ready_for_human_review` / `receipt_verified` / `qualification_recheck_ready`，`data_qualification_passed` / `phase_transition_allowed` **恒为** false、`blocker_active` / `human_gate_required` 恒为 true（**硬编码**），只能给出 `submit_to_l3_human_gate` 与缺口 / 原因码；`packet_id` 只由（策略块 + handoff / readiness 摘要 + plan + 收据 + recheck + 五个布尔）派生（**不含** `generated_at`）；只有显式 `--out` 才**原子**落盘 packet 本身（先取单实例锁）；**绝不自动 intake、绝不写数据库、绝不移动 / 删除 / 改写原始 evidence、零网络**；`PHASE3_3_DATA` **保持 BLOCKED**） | `src/evidence/decision_packet.py`、`scripts/evidence_decision_packet.py` |
| **Evidence Qualification L3 人工决策记录**（GOLD-016：`scripts/evidence_decision_record.py` 把**人工显式**给出的 `approve` / `reject` / `needs_changes` 绑定到**具体**的 GOLD-015 packet（`packet_id` + 内容摘要 `content_sha256` + 产物摘要 `artifact_sha256`），生成**确定性、脱敏、内容寻址**（`record_id`）的决策记录 —— packet **逐项**完整性核验（文档身份 / schema / 契约版本 / 安全字段不可被削弱 / 缺口与检查**算术可重算** / 三个布尔必须等于 `submit_to_l3_human_gate` 的合取 / readiness 快照**指纹同源** / 收据与 `receipt_verified` 往返一致 / recheck 与 `qualification_recheck_ready` 合取一致 / 批准与执行计数自洽 / `verification.violations` 必须为空 / **递归禁止任何证据时间键** / **禁止未来时间**）任一不一致 → **fail-closed**（稳定原因码 + 零写入）；`approve` **只**允许落在 packet 本身 `submit_to_l3_human_gate=true` 且完整性核验通过时（BLOCKED → `PACKET_NOT_SUBMITTABLE`，退出 `5`），`reject` / `needs_changes` 可记录但**绝不**改变任何资格状态；`human_decision_recorded` / `human_decision` / `packet_verified` 三个事实独立，`data_qualification_passed` / `phase_transition_allowed` / `phase_transition_executed` **恒为** false、`blocker_active` / `human_gate_required` 恒为 true、`human_gate_level` 恒为 `L3`（**硬编码**）；`record_id` **不含**任何审计时间（同 packet + 同人工决策幂等、同审计时点逐字节稳定），写下的记录**绝不**被静默覆盖（覆盖必须显式 `--revision` + `--supersedes`）；`--verify-record` 提供**防伪核验**（重新推导 `record_id`、与**当前** packet 比对 `packet_id` / 内容摘要、判定当初的 `approve` 是否仍成立）；人工身份只接受**非敏感 label**（不采集凭据），note / reason-code 一律脱敏 + 限长，`decision_at` / `generated_at` **绝不当证据时间**；只有显式 `--out` 才**原子**落盘记录本身（先取单实例锁）；**绝不写数据库、绝不调用 intake / commit、绝不修改 `PROJECT_STATE`、零网络**；`PHASE3_3_DATA` **保持 BLOCKED**） | `src/evidence/decision_record.py`、`scripts/evidence_decision_record.py` |
| **Evidence 本地 Package / Manifest Builder**（GOLD-017：`scripts/evidence_package.py` 把**人工显式**给出的 `evidence_type` / `source` / `authorization_reference` / `time_semantics` / `availability_semantics` / `historical_oos_applicable` 与**人工指定**的现有 evidence 文件（package 目录内**单级**文件名）整理成 GOLD-011 inbox 可直接消费的 `manifest.json` —— 字段 / 允许键 / 必填项 / 格式词表**复用** GOLD-011 契约（`MANIFEST_REQUIRED_FIELDS` / `ALLOWED_MANIFEST_KEYS` / `FILE_ENTRY_REQUIRED_FIELDS` / `SUPPORTED_FILE_FORMATS`）与 `evidence-intake-v1`，**不复制、不降低**任何规则；每个文件按**原始字节**计算 SHA-256，`manifest.files` 按规范化相对路径**确定性排序**（同输入 → **byte-stable**，manifest 里**没有任何时间字段**，绝不用 mtime / 当前时间充数）；**默认 dry-run 零写入**，只有显式 `--out` 才写，且**必须正好**是 `<package-dir>/manifest.json`（先取单实例锁 → **原子写** → **写后复读自检**），既有 manifest **逐字节一致** → 幂等、**内容不同** → `MANIFEST_CONFLICT` fail-closed（**没有** `--force` / `--overwrite`，更正必须新建 package 目录）；写盘前先做与 GOLD-005 / GOLD-011 **同源**的本地逐行预检（`read_input_file` + `assess_row`）并输出 `accepted` / `quarantined` / `not_oos_eligible` 与稳定原因码；绝对路径 / `..` / 多级路径 / 符号链接 / 目录 / `manifest.json` 自引用 / 未支持格式 / 重复或大小写冲突路径 / 包内未声明文件 / 示例或合成名称 / 敏感值（凭据键名、疑似 blob、可被脱敏规则命中的取值）一律 **fail-closed**；**绝不**移动 / 删除 / 改写原始 evidence、**绝不**自动 intake、**绝不**写数据库、零网络，四个安全字段恒定，`PHASE3_3_DATA` **保持 BLOCKED**） | `src/evidence/package_builder.py`、`scripts/evidence_package.py` |
| **Evidence Approved-for-Explicit-Intake Intake Plan 与最终写入前门禁**（GOLD-013：`scripts/evidence_intake_plan.py` 把 GOLD-012 的批准清单与**当前** inbox / review ledger **重新绑定核验** —— 清单结构自洽（文档标识 / schema / 契约版本 / 计数与列表长度一致 / 安全字段与 `approval_scope` 不可被削弱 / **不得**出现证据时间字段）、每条批准必须**当前仍是** ledger 上该指纹的**最新有效**决策（`review override` 后旧批准失效）、候选**当前仍在** inbox 且仍 `PREFLIGHT_PASS` 且非合成、清单与"用当前 inbox + ledger 重新算出的批准集合"完全一致；任一不一致 → **fail-closed**（退出码 `4`、**零写入**）；输出**确定性脱敏**、**内容级** `plan_id`（不含 `generated_at`；同输入同 `plan_id`、同审计时点逐字节稳定）的**只读**计划，`approved_for_explicit_intake` 与 `data_qualification_passed` 是两个独立字段（后者恒 false、`data_qualification_passed_count` 恒 0）；只有显式 `--out` 才**原子**落盘计划本身（先取单实例锁），`handoff` 只给**字符串**命令模板（必带 `--no-dry-run` 与显式 `--input`），`auto_intake_allowed` / `writes_database` 恒 false、`requires_explicit_operator_action` 恒 true；**绝不写数据库、绝不调用 intake / commit、绝不移动 / 删除原始 evidence、零网络**；四个安全字段恒定，**不解除** `PHASE3_3_DATA`） | `src/evidence/intake_plan.py`、`scripts/evidence_intake_plan.py` |
| **包边界惰性导出与 Import-Order 门禁**（GOLD-018：修复 `import src.monitoring` → `import src.evidence` 时 `src.evidence.__init__` eager 导入整个依赖图（`decision_packet` → `readiness_runner` → `handoff` → `src.monitoring.evidence_readiness`）造成的**初始化期循环导入** `ImportError: cannot import name 'BatchQuantification' from partially initialized module`；改为 **PEP 562 惰性导出**：包初始化阶段不加载任何子模块，惰性表只登记 `公开名 -> 定义子模块`（**不复制**任何类 / 阈值 / 枚举 / 常量），`__all__` 与既有 `from src.evidence import X` / `from src.monitoring import Y` / `from src.<pkg> import <子模块>` 用法**完全兼容**；**不改**资格算法 / 阈值 / 证据契约 / 安全字段，`PHASE3_3_DATA` **保持 BLOCKED**） | `src/evidence/__init__.py`、`src/monitoring/__init__.py`、`tests/unit/test_lazy_package_exports.py`、`tests/integration/test_evidence_import_order.py`、`tests/integration/test_evidence_cli_smoke.py` |


**当前阻塞（需要真实数据，不得用 Mock 绕过）**：作者侧只有 11 条观点，31 个评价行全部因
采集时间不可信而隔离；新闻侧只有 30 条 / 56 天，最大单源占比 66.67%。详见
`docs/experiments/Phase3_3_数据资格门禁报告.md` 与 TD-43。微博采集器仍须合规前提；
Scheduler（30 分钟框架，TD-09）已交付，但**不改变**上述数据资格门禁结论。

Phase 2 四张表由 migration 0005 建立，宏观 vintage 由 0006 建立，Phase 3 特征与 Regime
四张表由 0007 建立。由于 Phase 3.2 / 3.3 门禁未通过，`alpha_models`、`alpha_signals`、
`predictions`、Strategy 与 Trading 表仍**故意不建**；`scripts/check_phase3_boundaries.py`
会机械验证这个边界。

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

### 30 分钟调度（`scripts/run_collector_scheduler.py`，TD-09）

> 这是**研究数据采集服务**，不是实盘服务（`LIVE_TRADING=false` 仍是硬门禁）。

调度核心在 `src/scheduler/`（只做调度：UTC 对齐确定性槽 + `job_runs` 幂等 +
stale RUNNING/RETRYING 接管 + 单源构造/执行故障隔离），采集器注册与构造在
`src/collectors/bootstrap.py`，两者严格解耦（核心无 provider URL / 解析逻辑）。

```bash
# 单轮：只执行"当前 UTC 30 分钟槽"一次（同槽重复执行不会重复采集）
python -m scripts.run_collector_scheduler --once

# 常驻：每个 30 分钟槽执行一次，Ctrl+C 正常退出
python -m scripts.run_collector_scheduler --interval-minutes 30

# 自定义"在途多久可接管"（默认 = 3 倍调度间隔，30 分钟 → 90 分钟）
python -m scripts.run_collector_scheduler --stale-after-minutes 45

# 采集后立即加工（raw_items → processed_items）：显式开启 Processor（默认关闭）
python -m scripts.run_collector_scheduler --interval-minutes 30 --with-processor
```

- 只读取 `sources`（`enabled=true` 且配置 `config_json["collector"]`）；各采集器仍自行强制
  授权 / robots / 证书门禁，本 CLI **不做任何绕过**；
- `--with-processor` **默认关闭**（不传即注入 `post_processor=None`，行为与 GOLD-001-R2 一致）；
  显式开启后由 CLI 构造 `CollectionProcessor` 并经
  `src/collectors/bootstrap.py::default_collector_factory(post_processor=...)` 注入采集器，
  使 30 分钟采集完成 `raw_items → processed_items` 闭环——**Scheduler 核心仍不感知 Processor**；
- Processor 故障按源隔离：异常经 `src/common/redaction.py` 脱敏后同时写入日志与
  `job_runs.output_json.warnings`（`***` 替换凭据），不阻断其它源、不丢 `raw_items`；
- `job_runs.output_json` 只写白名单摘要（source / status / run_id / fetched / inserted /
  duplicate / failed / skipped / retry_count / warnings），**不写** token / API key /
  Authorization / 完整 source 配置；
- 无新增依赖（仅标准库 `asyncio` / `time`）、无新增 migration / schema。

### 采集后处理 Processor Pipeline（`src/processors/collection/`，TD-11）

> 从采集器里抽离出来的**采集后处理层**：不再把 normalize / dedup / timezone 逻辑内嵌在
> 采集器内（`docs/02 §5.2` 的 Processor 分层）。

确定性流水线（同一输入必得同一输出）：

```text
ProcessorInput
  → ① normalize              Unicode NFKC / 去零宽字符 / 折叠空白 / 超长截断
  → ② timezone/effective_at  统一 UTC；effective_at = max(published_at, collected_at, 上游下界)
  → ③ identity/dedup         内容指纹 + 幂等键（source_id + source_record_id + content_hash）
  → ④ validation/audit       数据质量校验 + 元数据脱敏 → 白名单审计摘要
  → processed_items（append-only；含 processor_name / processor_version / status）
```

- **职责边界**：本层不联网、不读 robots、不做 provider 授权/解析、不做调度；
  事实时间只来自 `published_at` / `collected_at`，时间不可信时判 `REJECTED`
  且**不落库**（宁可不写，也不用"现在"伪造）；
- **append-only + 幂等**：`processed_items` 以 `(raw_item_id, processor_name,
  processor_version)` 唯一 + 落库前存在性检查 → 重复输入 / 重复运行不产生重复结果；
  全部原始记录只读，绝不 UPDATE；
- **坏数据隔离**：单条失败（`REJECTED` / `FAILED`）只影响自己，批次状态诚实区分
  `SUCCESS` / `PARTIAL_FAILED` / `FAILED`；审计摘要只含
  `processor / version / status / input_count / output_count / duplicate_count /
  rejected_count / failed_count / warnings / error` 白名单字段，凭据统一由
  `src/common/redaction.py` 擦除（URL 去掉 query/userinfo）；
- **接线点**：`BaseCollector(..., post_processor=...)`（默认 `None`，零行为变化）；
  参考实现是 RSS 采集路径 `python scripts/collect_rss.py --to-db`（CLI 统计里多出
  `processing` 摘要），生产工厂入口 `src/collectors/bootstrap.py::default_collector_factory(
  post_processor=...)`——Scheduler 核心不感知 Processor；
- **常驻调度接线（GOLD-003）**：`scripts/run_collector_scheduler.py --with-processor`
  显式开启后，30 分钟采集与 Processor 形成生产级闭环；默认关闭即零行为差异；
- **测试**：`tests/unit/test_collection_processor.py`（35 项）、
  `tests/unit/test_redaction.py`（27 项，含 URL 空值 / 非字符串标量不被伪造成 URL 的回归）、
  `tests/integration/test_collection_processor_persistence.py`（8 项）、
  `tests/integration/test_collection_processor_wiring.py`（4 项）、
  `tests/integration/test_scheduler_processor_wiring.py`（5 项，Scheduler ↔ Processor 接线、
  幂等与单源故障隔离），全部 Mock、零网络；
- 无新增依赖、无新增 migration / schema（复用现有 `processed_items` 与 `ProcessStatus`）。


### 运行健康度与 Phase 3.3 数据资格观测（`scripts/report_collector_health.py`，GOLD-004）

> **只读观测层**：从 `job_runs` / `collector_runs` / `raw_items` / `processed_items` /
> `sources` 汇总运行质量与资格缺口；不写库、不改写历史事实、**不解除** `PHASE3_3_DATA`
> blocker。报告业务逻辑集中在 `src/monitoring/`，**Scheduler 核心零改动**。

```powershell
# 人类可读摘要（默认；只读，不落盘）
.\.venv\Scripts\python.exe -m scripts.report_collector_health

# 稳定 JSON（机器可读；字段与 schema_version 由测试锁定）
.\.venv\Scripts\python.exe -m scripts.report_collector_health --json

# 指定窗口 / 审计时点（可复现）
.\.venv\Scripts\python.exe -m scripts.report_collector_health --window-hours 48 --as-of 2026-09-22T12:00:00+00:00

# 落盘 Markdown 报告（必须显式 --no-dry-run，与项目其它脚本一致）
.\.venv\Scripts\python.exe -m scripts.report_collector_health --no-dry-run --report docs/experiments/collector_health.md
```

- **source 级健康度**（窗口内）：运行次数、SUCCESS / PARTIAL_FAILED / FAILED / 在途、
  连败次数、最近成功时间、陈旧标记（默认 90 分钟 = 3 × 30 分钟槽）、`raw_items` 实际条数、
  `inserted` / `duplicate` 上报值、`processed_items` 成功 / 拒绝 / 失败、加工观测状态；
- **状态词汇表**（绝不把 unknown 当 healthy）：`HEALTHY` / `DEGRADED` / `FAILED` /
  `STALE` / `NEVER_RUN` / `NEVER_SUCCEEDED` / `UNKNOWN` / `DISABLED` / `NO_SOURCES`；
  `NOT_OBSERVED` 明确表示"有原始数据但没有加工结果"（Processor 未启用 / 未运行）→ 整体降级；
- **复用口径，不另造阈值**：`JOB_TYPE` / `collector_name_for` / `default_stale_after`
  与 `src/alpha/evidence_gate.py` 的 Phase 3.3 阈值（30 条可信帖子 / 200 事件 / 90 天 /
  单源 40%）；观测层不修改 `src/alpha/**`；
- **Phase 3.3 资格缺口（机器可读）**：每项输出当前值、要求值、比较方式、`PASS/BLOCKED`、
  原因与证据时间范围；来源授权与"历史可用时间证据"只能由人工 Gate 通过，本报告恒为
  `BLOCKED`，并始终显式输出 blocker 代码 `PHASE3_3_DATA`；
- **脱敏**：只输出白名单字段；`sources.config_json` **整体不进入输出**；错误摘要经
  `src/common/redaction.py` 擦除凭据，`base_url` 去掉 userinfo / query / fragment；
- **测试**：`tests/unit/test_monitoring_health_report.py`、
  `tests/unit/test_phase33_qualification_report.py`、`tests/unit/test_monitoring_report.py`、
  `tests/integration/test_monitoring_health_integration.py`（29 项，全部 Mock / SQLite、零网络）；
- 无新增依赖、无新增 migration / schema、不进入 Phase 3.4、不生成交易信号。

### 授权证据接收入口（`scripts/intake_evidence.py`，GOLD-005）

> **合规红线**：本入口只做**字段级机械校验**。授权声明（`APPROVED`、三项 `permits_*`、
> 许可引用、人工签认人）是**人工签认的事实**，程序不证明其法律效力；
> 命令**不联网**、不抓取站点、不绕过 robots / 条款 / 证书限制；
> 任何记录都不能凭代码或 Mock 测试解除 `PHASE3_3_DATA`。

```powershell
# 默认 dry-run：只校验并打印报告（不写库、不落文件、不联网）
.\.venv\Scripts\python.exe -m scripts.intake_evidence --scope author --input logs/evidence/authors.jsonl

# 稳定 JSON（--json 时提示信息走 stderr，stdout 是纯 JSON）
.\.venv\Scripts\python.exe -m scripts.intake_evidence --scope news --input logs/evidence/news.csv --json

# 显式提交：append-only 写入 raw_items + processed_items，并落 manifest / quarantine
.\.venv\Scripts\python.exe -m scripts.intake_evidence --scope news --input logs/evidence/news.csv `
  --no-dry-run --manifest logs/evidence/news_manifest.json --quarantine logs/evidence/news_quarantine.jsonl
```

- **退出码**：`0` 全部通过 / `2` 参数或输入文件错误 / `3` 输入没有数据行 /
  `4` 存在被隔离的行（数据已隔离，未计入可信证据）；
- **输入格式**：JSONL（`.jsonl` / `.ndjson` / `.json`）或 CSV（UTF-8 / UTF-8-BOM），
  `--format auto` 按后缀识别；不使用任何第三方解析依赖；
- **可复现**：`--as-of`（必须带时区）同时决定"未来时间"判定与系统 `ingested_at`；
- **脱敏**：报告 / manifest / quarantine 只含白名单字段（来源、记录 ID、指纹、原因码、时间），
  **绝不输出正文与凭据**；输入行含凭据类列或凭据值（`api_key` / `Bearer` / `sk-` / `token=`）
  时整行隔离（`SENSITIVE_VALUE_DETECTED`，只记录列名）。

#### 证据契约 `evidence-intake-v1`（`src/evidence/contracts.py` 为唯一来源）

| 组 | 列（别名见 `alias_map()`） | 要求 |
|---|---|---|
| 来源身份 | `source`（`source_name`）、`source_record_id`（`id` / `post_id`）；Author 追加 `author_name`（`author`）、`external_account_id`（`account_id`） | 必填 |
| 内容 | `content`（`text_content` / `content_text` / `text`）或 `content_ref`（可核验引用） | 至少一项非空 |
| 时间 | `published_at`（`published`）、`collected_at`（`collected`） | 必填；必须带时区、不得未来、`collected_at` 必须**晚于** `published_at` |
| 出处 | `provenance_reference`（`provenance`） | 必填；`https` URL 或 `docs/legal/` 内相对路径 |
| 授权 | `authorization_status`（必须显式 `APPROVED`）、`authorization_basis`（四项白名单）、`authorization_reference`、`authorization_reviewed_by`、`authorization_reviewed_at`、`permits_automated_collection` / `permits_local_storage` / `permits_research_use`（均为 `true`） | 必填；`authorization_valid_from` / `authorization_expires_at` 可选，`collected_at` 必须落在授权有效期内 |
| 历史可用证据 | `available_at`、`availability_provenance`、`availability_reference` | 可选；**缺失或不自洽**（不满足 `published_at ≤ available_at ≤ collected_at`）时标记 `AVAILABILITY_UNPROVEN` → `NOT_OOS_ELIGIBLE` |
| 系统赋值 | `ingested_at`（`fingerprint` 由系统计算） | 输入提供的值一律**忽略**（防伪造审计时间） |

#### 关键行为

- **授权不可推断**：`authorization_status` 为缺失 / `UNKNOWN` / `DENIED` 一律
  `AUTHORIZATION_MISSING` 隔离，绝不因为"网页可公开访问"而放行；
- **不伪造时间**：naive / 未来 / 因果颠倒的时间一律隔离；`effective_at` 只按
  `max(published_at, collected_at)` 派生，`ingested_at` 由系统赋值；
- **历史 CSV 不具 OOS 资格**：`published_at` 早于 `collected_at` 只说明"当时已发布"，
  没有独立 `available_at` 证据的记录**明确** `NOT_OOS_ELIGIBLE`（仍可作为有效原始事实）；
- **幂等且不覆盖**：同一 `(source, source_record_id)` 内容一致 → `DUPLICATE`（不重复落库）；
  内容不同 → `IDENTITY_CONFLICT` 隔离，**绝不** UPDATE 历史事实；
- **坏行隔离**：无法解析的 JSONL 行 `ROW_UNREADABLE`、空内容 `CONTENT_EMPTY`、
  范围不符 `SCOPE_MISMATCH` 等都有稳定原因码，逐行隔离且不中断整批；
- **不新增采集能力**：自动创建的 `sources` 行 `enabled=false` 且不写 `config_json`
  （不注册采集器），不会被 Scheduler 采集；来源启用仍需人工 Gate；
- **只写既有 append-only 路径**：`raw_items`（证据元数据写入 `raw_json.evidence`）
  + `processed_items`（经现有 `CollectionProcessor`，processor 版本留痕）；
  本层**不**创建 `authors` / `author_posts`（作者链接入仍走既有授权门禁与导入流程）；
- **与 GOLD-004 观测集成**：`report_collector_health` 的资格报告新增
  `evidence_intake` 子报告（只统计经本入口认证、且有独立历史可用证据的记录），
  但**不会**解除 blocker——`ready` 仍由恒为 `BLOCKED` 的人工 Gate 项决定；
- **测试**：`tests/unit/test_evidence_contracts.py`、`tests/unit/test_evidence_validation.py`、
  `tests/unit/test_evidence_intake.py`、`tests/integration/test_evidence_intake_integration.py`
  （全部 Mock / 临时文件 / SQLite，零网络）；
- 无新增依赖、无新增 migration / schema、不进入 Phase 3.4、不生成交易信号或订单。
- **仍未解决（保持 BLOCKED）**：目前仓库内**没有**任何经本入口认证的真实授权证据，
  `PHASE3_3_DATA` 继续 `active=true`；业务方需按上述契约提供已授权 Author / News 数据，
  并由人工核验授权后才能产生可信证据。

### 证据就绪度与一键资格复核（`scripts/evidence_readiness.py`，GOLD-006）

> **合规红线**：本入口只**量化证据是否足够**并给出缺口；不判断授权法律效力、不抓取任何站点、
> 不伪造时间。`blocker_active` 与 `human_gate_required` **恒为 true**，任何 PASS 都不解除
> `PHASE3_3_DATA`。

Operator 工作流（全部默认只读、默认零网络）：

```powershell
# ① prepare template：打印 / 写入可填写模板（示例行显式标记 synthetic/example）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness template --scope author
.\.venv\Scripts\python.exe -m scripts.evidence_readiness template --scope news `
  --out examples/evidence/news_evidence_template.csv

# ② dry-run / preflight：只读库内台账 + 候选文件 dry-run 量化（零写入、零网络）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness preflight --scope news `
  --input logs/evidence/news.csv --json

# ③ inspect quarantine：看报告里的批次量化 / 原因码（或 intake 的 --quarantine 文件）
# ④ explicit intake：显式提交（默认 dry-run，提交需 --no-dry-run）
.\.venv\Scripts\python.exe -m scripts.intake_evidence --scope news --input logs/evidence/news.csv `
  --no-dry-run --manifest logs/evidence/news_manifest.json --quarantine logs/evidence/news_quarantine.jsonl

# ⑤ qualification recheck：一键串联只读台账与现有 Phase 3.3 qualification report
.\.venv\Scripts\python.exe -m scripts.evidence_readiness recheck --json
```

- **模板**：`examples/evidence/author_evidence_template.csv` / `news_evidence_template.csv`
  与生成器一致；每行带 `record_kind=example`、`is_mock=true`，且其余字段填**非法占位值**
  （时间不是合法 ISO8601、`authorization_status=PENDING`、三项 `permits_* = false`）。
  导入时整行判 `SYNTHETIC_EVIDENCE` 隔离：**模板 / 示例永不计入 qualification ledger**；
  即使有人删掉标记列，该行仍会因授权缺失 / 时间非法被隔离。`--out` 默认拒绝覆盖已存在文件；
- **preflight（就绪度）**：逐 scope 输出 `eligible_count` / `certified_count` /
  `not_oos_eligible_count` / `coverage_days` / `max_source_share`，以及每条检查的
  **当前值、阈值、比较方式、状态、remaining gap、证据时间范围**；候选文件（`--input`）
  额外量化 `accepted` / `quarantined` / `duplicate` / `conflict`（`IDENTITY_CONFLICT`）/
  `not_oos_eligible` 与稳定原因码计数；
- **Author readiness**：至少报告"可信 eligible post count"（≥ 30 才达标）；
- **News readiness**：至少报告 `eligible_count`（≥ 200）、`coverage_days`（≥ 90）、
  `max_source_share`（≤ 40%）；无 OOS eligible 记录时集中度检查**不得判 PASS**（`evaluable=false`）；
- **recheck**：稳定 JSON 含 `blocker_active`、`human_gate_required`、`gate`
  （qualification 的 PASS/BLOCKED 计数与 readiness 状态）、`readiness`（实际值 + 阈值 +
  remaining gap）与现有 `qualification` 报告全文；人类可读模式同时输出两份 Markdown；
- **默认零写入**：`--report` 必须显式配 `--no-dry-run` 才落盘；退出码
  `0` 报告成功（BLOCKED 也是正常结果）/ `2` 参数或输入错误 / `3` 输入没有数据行；
- **脱敏**：只输出白名单标量（计数 / 阈值 / 缺口 / 原因码 / 已脱敏来源名 / 时间），
  不读取 `sources.config_json`、不输出正文，token / API key / Authorization 一律
  `***`（来源名也过 `safe_text`）；
- **测试**：`tests/unit/test_evidence_templates.py`、`tests/unit/test_evidence_readiness.py`、
  `tests/integration/test_evidence_readiness_integration.py`（全部 Mock / 临时文件 / SQLite，零网络）；
- **仍未解决（保持 BLOCKED）**：真实库内仍无足量已授权证据，`PHASE3_3_DATA` 保持
  `active=true`；本工具只列出缺口，不修改阈值、不放宽授权 / 时间 / availability 规则。

### 单入口 Evidence Operator 工作流（`scripts/evidence_operator.py`，GOLD-007）

> **合规红线**：本工作流只做**字段级机械校验**与量化；授权声明的法律效力、许可范围与
> 历史可用时间证据**仍需人工核验**。命令**不联网**、不抓取站点、不绕过 robots / 条款 / 证书；
> `blocker_active` / `human_gate_required` **恒为 true**；无真实合格证据时
> `PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# ① 单入口（推荐）：默认 dry-run，一次完成 preflight / 隔离摘要 / recheck（零写入、零网络）
.\.venv\Scripts\python.exe -m scripts.evidence_operator workflow --scope news `
  --input logs/evidence/news.csv --json

# ② 唯一显式写入路径：先跑完整 dry-run 写入门禁，再 append-only 落库（含 manifest / 隔离清单）
.\.venv\Scripts\python.exe -m scripts.evidence_operator workflow --scope news `
  --input logs/evidence/news.csv --no-dry-run `
  --manifest logs/evidence/news_manifest.json `
  --quarantine logs/evidence/news_quarantine.jsonl

# ③ 单步命令（与 workflow 同一套校验逻辑；均默认 dry-run）
.\.venv\Scripts\python.exe -m scripts.evidence_operator template --scope author
.\.venv\Scripts\python.exe -m scripts.evidence_operator preflight --scope author --input authors.jsonl --json
.\.venv\Scripts\python.exe -m scripts.evidence_operator quarantine --scope author --input authors.jsonl
.\.venv\Scripts\python.exe -m scripts.evidence_operator intake --scope author --input authors.jsonl
.\.venv\Scripts\python.exe -m scripts.evidence_operator recheck --json

# ④ gateway-only 作者归属链（默认 dry-run；只消费已通过网关的 Author 证据）
.\.venv\Scripts\python.exe -m scripts.evidence_operator author-chain --json
```

- **单入口串联**：`workflow` 一次完成 `template → preflight → quarantine → intake → recheck`，
  JSON 含 `steps` / `preflight.readiness` / `write_gate` / `quarantine` / `intake` /
  `author_chain` / `qualification`，并显式输出 `blocker_code` 与 `blocker_active=true`；
- **写入前二次验证**：`intake` / `workflow --no-dry-run` 会先跑一次完整 dry-run
  Evidence Gateway 校验并汇总**写入门禁**（`accepted` / `quarantined` / `duplicate` /
  `conflict` / `not_oos_eligible` / `synthetic` / `authorization_incomplete` /
  `time_invalid` / `identity_issues` / `sensitive_detected` / `availability_unproven`），
  只有 `ACCEPTED` 行才 append-only 落库；
  未授权 / 合成示例 / 时间非法 / 身份冲突 / 凭据 / 坏行一律隔离，**绝不**进入 qualification
  ledger，也**绝不**覆盖历史事实（`IDENTITY_CONFLICT` 拒绝覆盖并给稳定原因码）；
- **幂等**：重复 dry-run 零写入；重复显式 intake 只产 `DUPLICATE`（原始层与台账不增长）；
  同一 evidence identity 不会重复计入资格；
- **gateway-only 作者归属链**：`author_chain` 只消费 `raw_json.evidence` 里带
  `evidence-intake-v1` + `scope=author` + `oos_eligible=true` 的记录
  （`scripts/import_real_posts.py` 的普通 CSV 载荷没有证据块，恒不是候选 → **无法绕过**
  gateway）；同一 `raw_item` 只归属一次；身份冲突（账号已归属其他作者）跳过且不覆盖；
  新建 `author_accounts` 一律 `enabled=false`（只建立归属，不启用采集）；
- **默认零写入**：`--report` / `--manifest` / `--quarantine` 必须显式配 `--no-dry-run`；
  退出码 `0` 报告成功（BLOCKED 也是正常结果）/ `2` 参数或输入错误 / `3` 输入没有数据行 /
  `4` `intake` 存在被隔离的行；
- **脱敏**：只输出计数 / 稳定原因码 / 已脱敏来源名 / 指纹前缀 / 时间，
  绝不输出正文、token / API key / Authorization 或完整 source config；
- **测试**：`tests/unit/test_evidence_workflow.py`、`tests/unit/test_evidence_author_chain.py`、
  `tests/integration/test_evidence_operator_integration.py`（全部 Mock / 临时文件 / SQLite，零网络）；
- 无新增依赖、无新增 migration / schema、不进入 Phase 3.4、不生成交易信号或订单；
- **仍未解决（保持 BLOCKED）**：库内仍无足量真实授权证据，`PHASE3_3_DATA` 保持
  `active=true`；本工具只列出缺口与隔离原因，不修改阈值、不放宽授权 / 时间 / availability 规则。
  真实数据仍需**业务方提供**并**人工核验**。

### Evidence 人工交接包（`scripts/evidence_handoff.py`，GOLD-008）

> **合规红线**：本入口只**只读**地把当前资格状态与人工证据口径交给 operator；不抓取站点、
> 不自动 intake、不修改数据库、不降低任何门槛。`blocker_active` / `human_gate_required`
> 恒为 true，`data_qualification_passed` / `phase_transition_allowed` 恒为 false；
> 无真实合格授权证据时 `PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# 默认：只读台账 + 只打印 stdout（零写入、零网络）
.\.venv\Scripts\python.exe -m scripts.evidence_handoff --json
.\.venv\Scripts\python.exe -m scripts.evidence_handoff --as-of 2026-09-22T12:00:00+00:00

# 追加候选文件 dry-run 隔离原因计数（仍零写入）
.\.venv\Scripts\python.exe -m scripts.evidence_handoff --scope news `
  --input logs/evidence/news.csv --json

# 唯一写文件路径：必须显式给出 --out
.\.venv\Scripts\python.exe -m scripts.evidence_handoff --json `
  --out logs/evidence/handoff.json
```

- **机器可读 JSON**：含 `blocker_code` / `blocker_active` / `human_gate_required` / `status` /
  `quantified_thresholds_met` / `ready_for_human_review` / `thresholds` / `author` / `news` /
  `checklist` / `excluded_evidence` / `quarantine_reason_counts` / `batch` / `notes`；
- **缺口量化**：Author / News 的 `eligible` / `required` / `remaining`、`coverage_days` 缺口与
  `source_share_evaluable`（无 OOS eligible 记录时**不得判 PASS**）；口径完全复用
  `src.monitoring.evidence_readiness`（阈值来自 `src.alpha.evidence_gate`），**不复制第二套算法**；
- **人工证据 checklist**：六大类 authorization / provenance / published_at / collected_at /
  availability / identity，逐项给出契约字段与机械校验范围，且每项 `human_review_required=true`
  （机械校验**不等于**法律效力核验）；
- **明确不计资格**：模板 / 示例（`SYNTHETIC_EVIDENCE`）、Mock / 演练语料、普通历史 CSV
  （`NOT_CERTIFIED`）、缺独立 `available_at`（`AVAILABILITY_UNPROVEN`）一律
  `counts_toward_eligibility=false`，醒目标记**不得**用于达标；
- **默认零写入**：不传 `--out` 时只打印 stdout；`--input` 只做 dry-run（显式回滚，零落库），
  本命令**没有** `--no-dry-run`，不存在自动 intake；
- **退出码**：`0` 量化门槛达标（**仍需人工 Gate**，不代表数据资格通过）/ `2` 参数或输入错误 /
  `3` 输入没有数据行 / `5` 仍未达标（诚实保持 BLOCKED）；
- **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名 / 时间，绝不输出正文、
  token / API key / Authorization 或完整 source config；
- **测试**：`tests/unit/test_evidence_handoff.py`、
  `tests/integration/test_evidence_handoff_integration.py`（全部 Mock / 临时文件 / SQLite，零网络）；
- **仍未解决（保持 BLOCKED）**：本工具**只减少人工交接摩擦**，不解除 `PHASE3_3_DATA`；
  真实授权证据仍需业务方提供 + 人工核验。

### Evidence Readiness 状态变更通知（`scripts/evidence_readiness_watch.py`，GOLD-009）

> **合规红线**：通知层只把 readiness **状态变化**转换为**本地、脱敏、幂等**事件，用于减少
> operator 盯盘 / 轮询。不联网、不写数据库、不新增 migration/schema、不接邮件 / 短信 /
> Webhook / 第三方推送、**不自动解除 blocker**、不进入 Phase 3.4。`blocker_active` /
> `human_gate_required` 恒为 true，`data_qualification_passed` / `phase_transition_allowed`
> 恒为 false；无真实合格授权证据时 `PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# 默认：只读台账 + 只打印 stdout（零写入、零网络；此时视为首次快照）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_watch --json

# 与上次快照比较（--state 只读；文件不存在 = 首次运行；损坏则安全失败退出 4）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_watch `
  --state logs/evidence/readiness_state.json --json

# 唯一写文件开关：显式 --out（原子写快照）+ 显式 --events（原子写事件）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_watch `
  --state logs/evidence/readiness_state.json `
  --out logs/evidence/readiness_state.json `
  --events logs/evidence/readiness_events.json --json

# 追加候选文件 dry-run（零写入），让隔离原因码变化可被检测
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_watch `
  --scope news --input logs/evidence/news.csv --json
```

- **确定性指纹 / 快照**：只基于**脱敏白名单字段**（`status` / `ready_for_human_review` /
  各 scope 的 `eligible` / `required` / `remaining` / coverage 缺口 / `source_share_evaluable` /
  稳定原因码）计算 SHA-256 指纹；scope 名与原因码**全部排序**，且 **`generated_at` 不参与指纹**
  （时间戳变化不算状态变化）；口径完全复用 `src.monitoring.evidence_readiness` 与
  `src.evidence.handoff`（阈值来自 `src.alpha.evidence_gate`），**不复制第二套阈值算法**；
- **有意义变化才告警**：首次快照（`FIRST_SNAPSHOT`）、BLOCKED 缺口变化
  （`BLOCKER_GAP_CHANGED`）、原因码集合变化（`REASON_CODES_CHANGED`）、
  `ready_for_human_review` false→true（`READY_FOR_HUMAN_REVIEW_ENABLED`）/
  true→false（`READY_FOR_HUMAN_REVIEW_REVOKED`）；**完全相同状态重复运行 → 0 事件**（幂等）；
- **默认只读 / 零写入 / 零网络**：`--state` 只读；只有显式 `--out` / `--events` 才写文件，
  且均为**原子写**（同目录临时文件 + `fsync` + `os.replace`，无半写状态、无残留临时文件）；
  `--input` 只做 dry-run（显式回滚，零落库），本命令**没有** `--no-dry-run`；
- **安全失败**：state 文件为空 / JSON 损坏 / schema 版本不符 / **指纹校验失败** /
  声称 blocker 已解除 → 退出码 `4`，**不写任何输出**（绝不把坏状态静默当作首次快照）；
- **脱敏**：原因码与事件消息统一经 `src.common.redaction.safe_text`（token / API key /
  Authorization 一律 `***`）；绝不输出正文、完整 source config 或原始 evidence payload；
- **退出码**：`0` 量化门槛达标（**仍需人工 Gate**，不代表数据资格通过）/ `2` 参数或输入错误 /
  `3` 输入没有数据行 / `4` state 损坏（安全失败）/ `5` 仍未达标（诚实保持 BLOCKED）；
- **测试**：`tests/unit/test_evidence_readiness_watch.py`、
  `tests/integration/test_evidence_readiness_watch_integration.py`（全部 Mock / 临时文件 /
  SQLite，零网络）；
- **仍未解决（保持 BLOCKED）**：通知层**只是减少盯盘 / 轮询**，不解除 `PHASE3_3_DATA`；
  即使 `ready_for_human_review=true`，Phase 切换仍须 `.ai/DEVELOPMENT_PROTOCOL.md` 的 L3 人工确认。

### Evidence Readiness 单次本地 tick runner（`scripts/evidence_readiness_runner.py`，GOLD-010）

> **合规红线**：本 runner 只做**一次**本地 tick，调度交给**外部定时器**（Windows Task Scheduler /
> 现有本地 orchestrator）。它不联网、不抓取站点、不写数据库、不新增 migration / schema、
> 不接邮件 / 短信 / Webhook / 第三方推送、**不自带常驻循环**、也**不自动修改**你的 OS 计划任务；
> `blocker_active` / `human_gate_required` 恒为 true，`data_qualification_passed` /
> `phase_transition_allowed` 恒为 false；无真实合格授权证据时 `PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# 单次 tick（唯一写入口 = 显式 --work-dir；state / 事件日志 / status 都写在该目录内）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_runner `
  --work-dir logs/evidence/runner --json

# 固定审计时点（ISO8601 必须带时区；用于人工复现 / 培训，不改变任何口径）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness_runner `
  --work-dir logs/evidence/runner --as-of 2026-09-23T00:00:00+00:00
```

**工作目录内的 artifact**（固定文件名，全部原子写：同目录临时文件 + `fsync` + `os.replace`）：

| 文件 | 作用 |
|---|---|
| `readiness_state.json` | GOLD-009 快照 state（等价于 watch CLI 的 `--state` / `--out`） |
| `readiness_events.jsonl` | 有界事件日志：只追加**有意义变化**的脱敏事件，保留最新 200 条且**本次新事件永不丢弃** |
| `readiness_status.json` | 本次 tick 的简洁 status（锁信息 / 事件数 / 四个安全字段 / 指纹 / 缺口三元组） |
| `readiness_tick.lock` | 单实例锁（可审计 owner / pid / 获取时间；**不删除**，保留为审计留痕） |

- **单实例锁（防并发重入）**：同一工作目录的 tick 用 **OS 级独占文件锁**
  （Windows `msvcrt` / POSIX `fcntl`，零第三方依赖）串行化；锁文件记录
  `owner` / `pid` / `acquired_at`（**不含任何敏感数据**），正常退出释放锁但**不删除文件**；
  锁冲突 → 退出码 `6` 且**零写入**——因为 OS 锁才是"是否活动"的唯一权威，
  工具**绝不删除 / 绝不改写**活动锁，因此**不可能**造成并发写 state；
- **陈旧锁安全接管**：崩溃进程遗留的锁文件（无活动 OS 锁）可被安全接管，
  并在新锁记录与 status 中留下 `recovered_stale=true` + `previous_owner` 审计留痕
  （锁文件内容不可解析时同样只留痕、不崩溃、不删除）；
- **幂等**：完全相同 readiness 状态连续 tick → **0 事件**、事件日志逐字节不变
  （指纹不包含时间戳，`--as-of` 变化不算状态变化）；有意义变化仍沿用 GOLD-009 的
  脱敏事件（`FIRST_SNAPSHOT` / `BLOCKER_GAP_CHANGED` / `REASON_CODES_CHANGED` /
  `READY_FOR_HUMAN_REVIEW_ENABLED` / `REVOKED`）；
- **写入顺序事件优先**：先追加事件日志 → 再原子替换 state → 最后写 status。
  即使进程在写入途中被强杀，最坏只会"多一条待确认事件"，
  而**不会**出现"状态已更新、事件永久丢失"（那会让 operator 永远看不到变化）；
- **有界保留（确定性）**：事件日志保留**最新 200 条**，滚动丢弃条数写入 status 的
  `journal.dropped`；本次 tick 的新事件**永不被丢弃**（上限不足时自动放宽到本次事件数）；
- **fail-closed**：state / 事件日志损坏或被篡改（含"声称 blocker 已解除 / 资格已通过"）
  → 退出码 `4`；锁冲突 → `6`；资格计算失败 → `7`；工作目录或 artifact 不可写 → `3`。
  这些路径**保留旧的有效 state**、**不把异常当成首次快照**、**不自动重置资格状态**、
  也不产生任何输出文件（stdout 为空，错误只走 stderr 且已脱敏，绝不回显数据库连接串 / 凭据）；
- **诚实**：status 与事件恒为 `blocker_active=true` / `human_gate_required=true` /
  `data_qualification_passed=false` / `phase_transition_allowed=false`（**硬编码**，不被上游或
  被篡改的 state 透传影响）；即使 `ready_for_human_review=true`，Phase 切换仍须
  `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**；
- **退出码**：`0` 量化门槛达标（**仍需 L3 人工 Gate**）/ `2` 参数错误（未显式给出
  `--work-dir`、`--as-of` 无时区）/ `3` 工作目录或 artifact 不可写 /
  `4` state 或事件日志损坏（安全失败）/ `5` **仍未达标（预期 BLOCKED，不是定时器故障）** /
  `6` 锁冲突（另一个 tick 正在运行）/ `7` 资格计算失败；
- **测试**：`tests/unit/test_evidence_readiness_runner.py`（23 项：锁冲突 / 陈旧锁接管 /
  正常与无变化 tick / 有界保留 / 损坏 state 与事件日志 / 资格失败脱敏 / 原子写失败 /
  退出码映射 / 无常驻循环源码守卫）、
  `tests/integration/test_evidence_readiness_runner_integration.py`（7 项：真实 CLI + 临时
  SQLite，只读台账、退出码、锁冲突、陈旧锁、损坏 state、参数错误；全部零网络）；
- **仍未解决（保持 BLOCKED）**：runner **只是把盯盘自动化**（把"人工反复执行"换成"外部定时器
  调用一次"），**不是**资格判定器，也不具备解除 blocker 的能力；库内仍无足量真实授权证据，
  `PHASE3_3_DATA` 保持 `active=true`。

#### 外部定时调用示例（**仅供人工配置**；本工具不会自动修改你的计划任务）

Windows 任务计划程序（taskschd.msc）→ 创建任务：

- **程序或脚本**：`D:\VSCodeProject\XAUUSD\.venv\Scripts\python.exe`
- **添加参数**：`-m scripts.evidence_readiness_runner --work-dir logs\evidence\runner --json`
- **起始于**：`D:\VSCodeProject\XAUUSD`（`-m` 需要仓库根作为工作目录）
- **触发器**：例如每 30 分钟一次；建议勾选"如果任务已在运行，则不要启动新实例"
  （本 runner 的单实例锁是第二层保护，两层同时存在更稳）

等价 `schtasks`（**人工执行，本工具不会替你跑**）：

```bat
schtasks /Create /TN "GOLD-AI Evidence Readiness Tick" /SC MINUTE /MO 30 /ST 00:00 ^
  /TR "\"D:\VSCodeProject\XAUUSD\.venv\Scripts\python.exe\" -m scripts.evidence_readiness_runner --work-dir logs\evidence\runner --json" ^
  /RL LIMITED
```

> ⚠️ 退出码 `5` 表示"数据仍未达标"——这是**预期**的诚实状态，**不是**定时器故障：
> 真正的故障只有 `2` / `3` / `4` / `6` / `7`。若希望任务计划程序不因 `5` 报红，
> 请在包装脚本（`.bat` / `.ps1`）里把 `5` 视为成功，或只检查
> `logs\evidence\runner\readiness_status.json` 的 `blocker_active` 与 `journal`；
> 状态文件与事件日志本身**不含**任何正文 / 凭据，可安全交给本地运维查看。
> 本工具**不会**联网、不会发通知，README 只提供调用方式。

### Evidence 本地 Inbox 发现与预检（`scripts/evidence_inbox.py`，GOLD-011）

> **合规红线**：本工具只做**纯本地只读发现与预检** —— 只扫描你显式给出的 `--inbox-dir`，
> **绝不**移动 / 重命名 / 删除 / 改写 inbox 内任何原始 evidence，不联网、不写数据库、
> 不新增 migration / schema、不接第三方推送、**不自动 intake**。
> `blocker_active` / `human_gate_required` 恒为 true，`data_qualification_passed` /
> `phase_transition_allowed` 恒为 false；`preflight_pass` 也只进**人工确认 / 显式 intake** 队列，
> 无真实合格授权证据时 `PHASE3_3_DATA` 保持 **BLOCKED**。

**operator 放置候选 evidence 的最小目录 / manifest 示例**（候选包 = inbox 的**子目录**）：

```text
logs/evidence/inbox/                  # --inbox-dir（只扫描它的直接子目录）
└── vendor-author-2026-06/            # 一个候选包
    ├── manifest.json                 # 必填：显式关联证据文件与语义
    └── author.jsonl                  # 候选证据（JSONL / CSV，一行一条记录）
```

```json
{
  "schema_version": 1,
  "contract_version": "evidence-intake-v1",
  "evidence_type": "author",
  "source": "vendor-author",
  "authorization_reference": "https://vendor.example/terms",
  "time_semantics": "provider_export_iso8601_with_tz",
  "availability_semantics": "provider_archive_export_daily_snapshot",
  "historical_oos_applicable": true,
  "files": [
    {"path": "author.jsonl", "sha256": "<64 位小写十六进制摘要>", "format": "jsonl"}
  ]
}
```

```powershell
# 只读扫描（零写入；stdout 打印脱敏结果）
.\.venv\Scripts\python.exe -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox --json

# 唯一写开关：--out 原子落盘 pending 清单（--state 只读；自带单实例锁防并发重扫）
.\.venv\Scripts\python.exe -m scripts.evidence_inbox `
  --inbox-dir logs/evidence/inbox `
  --state logs/evidence/inbox_pending.json `
  --out   logs/evidence/inbox_pending.json --json
```

- **候选包 = 子目录 + manifest**：`manifest.json` 必填 `evidence_type`（author|news）/
  `source`（或别名 `provider`）/ `authorization_reference` / `time_semantics` /
  `availability_semantics` / `historical_oos_applicable`（布尔）/ `files`（每个文件
  `path` + `sha256`）；缺任一项 / 格式错误 / schema 或 contract 版本不符 / 证据类型不支持
  → **fail-closed** + 稳定原因码；inbox 根目录下的**散落文件**不是候选包，
  会以 `MANIFEST_MISSING` 隔离提示（**不会被移动或删除**）；
- **内容 SHA-256 与确定性指纹**：逐文件实测 SHA-256 并与声明核对（不一致即隔离且**不再解析
  内容**）；候选包指纹只由内容摘要与结构标记派生，**不含**文件名 / mtime / 绝对路径 / 扫描时间
  （改 mtime、换 `--as-of` 都不改变指纹）；
- **幂等**：pending 清单以指纹为键（当前 inbox 内容的镜像）——同内容重复扫描 `discovered=0`、
  条目与 `first_seen_at` 不变，只推进 `scan_count` / `last_seen_at`；内容变化 → 新指纹（新条目）；
  同一指纹的状态变化记入 `status_changed`；清单**原子写**（无残留 `.tmp`）；
- **只允许单级文件名**：`files[].path` 必须是候选包目录下的**直接子文件**；包内除
  `manifest.json` 之外的**所有**文件都必须声明，否则整包隔离；
- **安全拒绝（fail-closed）**：路径穿越 / 绝对路径 / 多级路径 / 符号链接（文件与目录）/
  未声明文件 / 摘要不一致 / 凭据类键或值 / 示例模板（`is_mock` / `is_example` / `synthetic` /
  `record_kind=example`，或包名 / 文件名命中示例词表）/ 无可用行 / 缺 OOS 证据一律隔离并给稳定
  原因码；**越界文件从未被读取**（只依据候选包目录内的内容摘要判定）；
- **复用同一口径**：逐行预检直接调用 Evidence Gateway 的 `assess_row`（`evidence-intake-v1`，
  见 §7「授权证据接收入口」），授权 / 时间 / availability / 隔离原因码与 `intake` **完全同源**，
  **不复制、不降低任何阈值**；`DUPLICATE` / `IDENTITY_CONFLICT`（需要读库）留给显式 intake；
- **脱敏**：只输出计数 / 稳定原因码 / 来源名 / 指纹前缀 / 时间与已脱敏引用（URL 去 query 与
  userinfo），**不含** evidence 正文；凭据类字段只记录**键名**；
- **退出码**：`0` 存在可确认候选（**仍需人工 + 显式 intake**）/ `2` 参数错误 /
  `3` inbox 目录或输出不可用（含把 `--out` 写进 inbox 的拒绝）/ `4` 既有 pending state 损坏
  （安全失败，零写入）/ `5` 无候选（**预期 BLOCKED**）/ `6` 锁冲突；
- **预检通过后的显式 intake（人工）**：人工核验授权与可用性证据后，仍须显式落库：
  ```powershell
  .\.venv\Scripts\python.exe -m scripts.evidence_operator workflow `
    --scope author --input logs/evidence/inbox/vendor-author-2026-06/author.jsonl `
    --no-dry-run --manifest logs/evidence/author_manifest.json
  .\.venv\Scripts\python.exe -m scripts.evidence_operator recheck --json
  ```
  落库后仍以 `scripts/evidence_readiness_watch.py` / `scripts/evidence_handoff.py` 复核；
  Phase 切换仍须 `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**；
- **测试**：`tests/unit/test_evidence_inbox.py`（55 项）、
  `tests/integration/test_evidence_inbox_integration.py`（10 项，含真实子进程 CLI 冒烟）；
  全部临时目录 / 零网络 / 零数据库；
- **仍未解决（保持 BLOCKED）**：inbox 只是"摆放与预检"入口，**不是**资格判定器，也不具备解除
  blocker 的能力；库内仍无足量真实授权证据 → `PHASE3_3_DATA` 保持 `active=true`。

### Evidence Inbox 人工复核决策与审计（`scripts/evidence_review.py`，GOLD-012）

> **合规红线**：本工具只做**纯本地**的人工复核决策与审计留痕 —— 不联网、不写数据库、
> 不新增 migration / schema、**绝不**调用任何 intake / commit 路径、**绝不**移动 / 重命名 /
> 删除 / 改写 inbox 内任何原始 evidence。`APPROVE` **只表示人工预审通过**，
> **不等于** data qualification PASS；`blocker_active` / `human_gate_required` 恒为 true，
> `data_qualification_passed` / `phase_transition_allowed` 恒为 false；approve 数量达到任何
> 阈值都**不会**自动改变这四个字段，`PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# ① 只读列出：现有 ledger 的最新决策 + 当前仍成立的批准（零写入）
.\.venv\Scripts\python.exe -m scripts.evidence_review `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json --json

# ② 记录人工决策（不给 --out 就是 dry-run；--out 才原子落盘 ledger）
.\.venv\Scripts\python.exe -m scripts.evidence_review `
  --inbox-dir logs/evidence/inbox `
  --decision approve --fingerprint <GOLD-011 的 64 位内容级指纹> `
  --reviewer operator-li --reason-code APPROVED_FOR_EXPLICIT_INTAKE `
  --ledger logs/evidence/inbox_review_ledger.json `
  --out    logs/evidence/inbox_review_ledger.json `
  --approved-out logs/evidence/approved_for_intake.json --json

# ③ 推翻既有决策：必须**显式**给出新 revision + --override（历史全部保留）
.\.venv\Scripts\python.exe -m scripts.evidence_review `
  --inbox-dir logs/evidence/inbox --decision reject --fingerprint <同一指纹> `
  --reviewer operator-li --reason-code REJECTED_AUTHORIZATION_INSUFFICIENT `
  --ledger logs/evidence/inbox_review_ledger.json `
  --out    logs/evidence/inbox_review_ledger.json --revision 2 --override
```

- **决策与词表**：`--decision` 只有 `approve` / `reject` / `needs_changes`；`--reason-code`
  必须落在与决策匹配的**受控词表**（跨决策使用 → 退出码 `2`）；决策必须**显式引用** GOLD-011 的
  **内容级** fingerprint（文件名 / mtime / 扫描时间都**不是**指纹）；
- **最小审计元数据**：`--reviewer`（非敏感标识，必填）、`--reviewed-at`（ISO8601 必须带时区，
  且不得晚于审计时点）、`--note`（可选，≤300 字符）；**凭据类内容一律拒绝记录**（fail-closed，
  绝不把擦除后的 `***` 写进审计历史）；
- **approve 门禁（fail-closed）**：①该指纹**当前仍在** inbox 扫描结果中、②当前预检
  `PREFLIGHT_PASS`、③**不是**模板 / 示例 / Mock（`SYNTHETIC_EVIDENCE` 永不可批准）；
  内容变化 → **新指纹**（旧批准绝不继承）；候选消失 / 预检回退 / ledger 损坏 → 拒绝记录且零写入；
- **追加式 ledger（只 append）**：`kind=evidence_inbox_review_ledger`；`decision_id` 确定性
  （只由指纹 / 决策 / revision / 原因码派生）；同一指纹 + **完全相同**决策重复提交**幂等**
  （不新增记录、不改写历史）；任何差异都必须显式 `--revision <既有 + 1>` + `--override`
  （否则退出码 `4`），`supersedes` 指向被取代的决策，**历史全部保留**；
- **批准清单 ≠ 资格**：`--approved-out` 只生成**脱敏**的 `approved-for-explicit-intake` 清单，
  且必须在**当前**扫描结果上**重新验证**（候选仍在、仍 `PREFLIGHT_PASS`、摘要与复核时一致）；
  不满足的批准进入 `invalidated`（`CANDIDATE_MISSING` / `PREFLIGHT_NOT_PASSING` /
  `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`）——**绝不因为「曾经批准过」就放行**；
- **写开关只有一个**：`--out` 才写 ledger（原子写 + 单实例锁），`--approved-out` 才写批准清单；
  `--ledger` **只读**（损坏 → 退出码 `4`，旧文件原样保留）；所有输出必须位于 inbox **之外**；
- **脱敏**：artifact 只含计数 / 稳定原因码 / 指纹 / 来源名 / 已脱敏引用（URL 去 query 与
  userinfo）/ 审计时间与 reviewer 非敏感标识，**不含** evidence 正文；
- **退出码**：`0` 决策已记录（或幂等重复）/ `2` 参数或词表错误 / `3` inbox 或输出不可用
  （含把 `--out` 写进 inbox 的拒绝）/ `4` ledger 损坏、决策冲突或目标不满足门禁（fail-closed，
  零写入）/ `5` 只读运行且**没有任何**仍成立的批准（**预期 BLOCKED**）/ `6` 锁冲突；
  失败路径 stdout 为空、stderr 已脱敏；
- **复核元数据不是证据**：`reviewer` / `note` / 文件名 / mtime / 复核时间 / 「曾被人批准」
  都**不是** `published_at` / `collected_at` / `effective_at` / `availability` 证据；
  本工具的 artifact 只有上述审计字段，绝不合成任何证据时间；
- **测试**：`tests/unit/test_evidence_review.py`、`tests/integration/test_evidence_review_integration.py`
  （临时目录 / 零网络 / 零数据库；含真实子进程 CLI 冒烟）。

**最小操作路径：inbox 扫描 → 人工复核 → 批准清单 → 显式 intake**：

```powershell
# ① 发现与预检（GOLD-011）
.\.venv\Scripts\python.exe -m scripts.evidence_inbox `
  --inbox-dir logs/evidence/inbox --state logs/evidence/inbox_pending.json `
  --out logs/evidence/inbox_pending.json --json
# ② 人工复核决策 + 批准清单（GOLD-012；见上面第 ② 条）
# ③ 人工按清单**显式**落库（GOLD-007；不自动、不可省）
.\.venv\Scripts\python.exe -m scripts.evidence_operator workflow `
  --scope author --input logs/evidence/inbox/<候选包>/author.jsonl `
  --no-dry-run --manifest logs/evidence/author_manifest.json
.\.venv\Scripts\python.exe -m scripts.evidence_operator recheck --json
```

任何一步都**不会**自动解除 `PHASE3_3_DATA`；落库后仍以
`scripts/evidence_readiness_watch.py` / `scripts/evidence_handoff.py` 复核，
Phase 切换仍是 `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**。

### Evidence Intake Plan（`scripts/evidence_intake_plan.py`，GOLD-013）

> **合规红线**：本工具是**最终写入前**的**只读**门禁 —— 不联网、不写数据库、不新增
> migration / schema、**绝不**调用任何 intake / commit 路径、**绝不**移动 / 重命名 / 删除 /
> 改写 inbox 内任何原始 evidence。它把 GOLD-012 的批准清单与**当前** inbox / review ledger
> **重新绑定核验**；`approved_for_explicit_intake` 与 `data_qualification_passed` 是两个**独立**
> 字段，后者**恒为** false（`data_qualification_passed_count` 恒为 `0`）；`blocker_active` /
> `human_gate_required` 恒为 true、`phase_transition_allowed` / `auto_intake_allowed` /
> `writes_database` 恒为 false；`PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# ① 只读核验并打印计划（零写入；stdout 为纯 JSON）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_plan `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json --json

# ② 唯一写开关：显式 --out 原子落盘计划本身（仍**不**写数据库、**不**自动 intake）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_plan `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --out logs/evidence/evidence_intake_plan.json --json

# ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_plan `
  --inbox-dir logs/evidence/inbox --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json --as-of 2026-09-23T00:00:00+00:00 --json
```

- **三个输入全部必填**：`--inbox-dir`（GOLD-011 只读扫描）、`--ledger`（GOLD-012 review
  ledger，只读）、`--approved-list`（GOLD-012 脱敏批准清单，只读）；缺任一项 → 退出码 `2` / `4`
  （**绝不**猜、绝不自动生成批准）；
- **最终写入前逐项核验（fail-closed）**：①清单**结构自洽**（文档标识 / schema / 契约版本 /
  `approved_count` / `invalidated_count` 与列表长度一致 / 四个安全字段与 `approval_scope`
  不可被 state 削弱 / **不得**出现任何证据时间字段）→ `IntakePlanStateError`；②每条批准必须
  **当前仍是** ledger 上该指纹的**最新有效**决策（`revision` / `decision_id` 完全一致，
  `review override` 后旧批准一律失效：`LEDGER_MISSING_APPROVAL` /
  `LEDGER_DECISION_SUPERSEDED` / `LEDGER_REVISION_SUPERSEDED`）；③该指纹**当前仍在** inbox
  且仍 `PREFLIGHT_PASS`、**不是**模板 / 示例 / Mock、摘要与复核时一致（`FINGERPRINT_MISSING` /
  `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`）；④清单与"用
  **当前** inbox + ledger 重新算出的批准集合"**完全一致**（`APPROVED_LIST_STALE` /
  `APPROVED_LIST_TAMPERED` / `INVALIDATED_MISMATCH` / `COUNT_MISMATCH`）；⑤计划审计时点不得
  早于清单生成时间（`PLAN_TIME_BEFORE_APPROVAL`）；任一不一致 → `IntakePlanInconsistentError`
  （退出码 `4`、**零写入**）；
- **内容级 `plan_id` / 幂等**：`plan_id` 只由（策略块 + 批准条目 + ledger 最新决策摘要）派生，
  **不含** `generated_at`；同一输入重复生成得到同一 `plan_id`，同一审计时点逐字节稳定；**输入
  内容或 review revision 变化**必然产生新 `plan_id`（旧计划 / 旧批准**绝不静默继承**）；
- **写开关只有一个**：`--out` 才写**计划本身**（原子写 + 单实例锁，无残留 `.tmp`）；所有输入 /
  输出必须位于 inbox **之外**；只读运行**零写入**；
- **显式衔接，绝不执行**：计划里的 `handoff` 只是**字符串**命令模板（每个证据文件一条，必带
  `--no-dry-run` 与显式 `--input`，并给出 `input_path`）；`auto_intake_allowed` /
  `writes_database` 恒为 false、`requires_explicit_operator_action` 恒为 true；本工具**没有**
  任何 intake / `--no-dry-run` 参数，真实落库必须由人工**显式**执行
  `scripts/evidence_operator.py workflow --no-dry-run`（执行前自行补显式 `--manifest`），
  随后以 `handoff` / `recheck` 复核；
- **脱敏**：artifact 只含计数 / 稳定原因码 / 指纹 / 来源名 / 审计时间 / 已脱敏路径，**不含**
  evidence 正文；`reviewer` / `note` / 文件名 / mtime / `reviewed_at` / `generated_at` /
  「曾被人批准」都**不是**证据时间，产出里根本没有这些证据时间键；
- **退出码**：`0` 计划生成成功且**至少一条**批准仍成立 / `2` 参数错误（缺必填参数 / 时区缺失 /
  试图传 `--no-dry-run`）/ `3` inbox 或输出不可用（含把 `--out` 写进 inbox 的拒绝）/ `4` 批准
  清单 / ledger 损坏被篡改或核验不通过（fail-closed，零写入）/ `5` 计划生成成功但**没有任何**
  仍成立的批准（**预期 BLOCKED**）/ `6` 锁冲突；失败路径 stdout 为空、stderr 已脱敏；
- **测试**：`tests/unit/test_evidence_intake_plan.py`、
  `tests/integration/test_evidence_intake_plan_integration.py`
  （临时目录 / 零网络 / 零数据库；含真实子进程 CLI 冒烟）。

**最小操作路径：inbox 扫描 → 人工复核 → 批准清单 → intake plan → 人工显式 Evidence Operator intake → handoff/recheck**：

```powershell
# ① 发现与预检（GOLD-011）
.\.venv\Scripts\python.exe -m scripts.evidence_inbox `
  --inbox-dir logs/evidence/inbox --state logs/evidence/inbox_pending.json `
  --out logs/evidence/inbox_pending.json --json
# ② 人工复核决策 + 批准清单（GOLD-012；见上面第 ② 条）
.\.venv\Scripts\python.exe -m scripts.evidence_review `
  --inbox-dir logs/evidence/inbox --decision approve --fingerprint <指纹> `
  --reviewer operator-li --reason-code APPROVED_FOR_EXPLICIT_INTAKE `
  --ledger logs/evidence/inbox_review_ledger.json `
  --out logs/evidence/inbox_review_ledger.json `
  --approved-out logs/evidence/approved_for_intake.json --json
# ③ 最终写入前只读核验 + 显式衔接命令（GOLD-013；见上面第 ①/② 条）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_plan `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --out logs/evidence/evidence_intake_plan.json --json
# ④ 人工按计划里的 handoff **显式**落库（GOLD-007；不自动、不可省，需自行补 --manifest）
.\.venv\Scripts\python.exe -m scripts.evidence_operator workflow `
  --scope author --input logs/evidence/inbox/<候选包>/author.jsonl `
  --no-dry-run --manifest logs/evidence/author_manifest.json
# ⑤ 落库后复核
.\.venv\Scripts\python.exe -m scripts.evidence_operator recheck --json
```

任何一步都**不会**自动解除 `PHASE3_3_DATA`；计划里的 `READY_FOR_EXPLICIT_INTAKE` 只表示
"人工预审通过且当前仍一致"，**不等于** data qualification PASS；落库后仍以
`scripts/evidence_readiness_watch.py` / `scripts/evidence_handoff.py` 复核，
Phase 切换仍是 `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**。

### Evidence 显式 Intake Receipt（`scripts/evidence_intake_receipt.py`，GOLD-014）

> **合规红线**：本工具是**执行之后**的**只读**审计层 —— 不联网、不写数据库、不新增
> migration / schema、**绝不**调用任何 intake / commit 路径、**绝不**移动 / 重命名 / 删除 /
> 改写 inbox 内任何原始 evidence、**绝不**修改 `.ai/PROJECT_STATE.json` 或解除 blocker。
> 它把 GOLD-013 plan、**当前** inbox / review 状态、人工**显式** Evidence Operator 执行结果与
> 随后的 qualification recheck 绑定成**内容寻址**的收据；`intake_executed` / `receipt_verified`
> **绝不蕴含** `data_qualification_passed`（恒 false）或 `phase_transition_allowed`（恒 false）；
> `blocker_active` / `human_gate_required` 恒 true，`PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# ① 只读核验并打印收据（零写入；stdout 为纯 JSON）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_receipt `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json `
  --recheck logs/evidence/phase33_recheck.json --json

# ② 唯一写开关：显式 --out 原子落盘收据本身（仍**不**写数据库、**不**执行任何 intake）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_receipt `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json `
  --recheck logs/evidence/phase33_recheck.json `
  --out logs/evidence/evidence_intake_receipt.json --json

# ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_receipt `
  --inbox-dir logs/evidence/inbox --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json --recheck logs/evidence/phase33_recheck.json `
  --as-of 2026-09-23T03:00:00+00:00 --json
```

- **四个输入必填**：`--inbox-dir`（GOLD-011 只读扫描）、`--ledger`（GOLD-012 review ledger）、
  `--approved-list`（GOLD-012 脱敏批准清单）、`--plan`（GOLD-013 intake plan）；缺任一项 →
  退出码 `2`（**绝不**猜、绝不自动生成 plan / 批准）；
- **执行与复核都要绑定（fail-closed）**：`--operator-result` 可重复给出（`--no-dry-run --manifest`
  产物），被批准的**每个证据文件都必须被覆盖**（按**内容 SHA-256** 绑定，不看文件名 / mtime），
  且 `dry_run=false`、`persisted>=1`、`counts` 自洽（行数一致 / `accepted+quarantined+duplicate<=rows`
  / `persisted<=accepted`）；`--recheck` 必须给出且**不早于**最后一次显式执行
  （`phase33_qualification_recheck`），并保持 `blocker_active=true` / `human_gate_required=true`；
- **plan 必须仍然成立**：用**当前** inbox + ledger + 批准清单重新算出 `plan_id` 与条目摘要
  （指纹 / `decision_id` / `review revision` / `scope` / 文件清单）；plan stale、候选消失、
  preflight 回退、**review override**、批准清单 / ledger 被篡改 → `PLAN_STALE` /
  `PLAN_ENTRY_MISMATCH` / `PLAN_TAMPERED`（退出码 `4`、**零写入**）；
- **明确区分四个布尔**：`intake_executed`（人工显式落库确实发生）与 `receipt_verified`
  （本收据的绑定核验通过）**互不蕴含**，且**绝不**等于资格通过；
  `data_qualification_passed` / `data_qualification_passed_count` **恒为** false / 0，
  `phase_transition_allowed` 恒为 false；收据里的 `qualification_recheck` 只是"复核发生过"的
  事实记录（`is_qualification_decision=false`），Phase 切换仍是 **L3 人工 Gate**；
- **内容级 `receipt_id` / 幂等**：`receipt_id` 只由（策略块 + `plan_id` + 批准条目 + 执行摘要 +
  复核摘要 + 两个布尔）派生，**不含** `receipt_at`；同一输入重复生成得到同一 `receipt_id`，
  同一审计时点逐字节稳定；plan / review revision / 指纹 / 执行结果 / 复核结果任一变化 → 新
  `receipt_id` 或直接 fail-closed（旧收据**绝不静默继承**）；
- **写开关只有一个**：`--out` 才写**收据本身**（原子写 + 单实例锁，无残留 `.tmp`）；所有输入 /
  输出必须位于 inbox **之外**；只读运行**零写入**；
- **脱敏 / 时间语义**：收据只含计数、稳定原因码、指纹、摘要、已脱敏路径与**审计操作时间**；
  产出里**根本没有** `published_at` / `collected_at` / `effective_at` / `available_at` /
  `availability_provenance` / `availability_reference` 这些证据时间键（输入文档含这些键也会
  被拒），**绝不**把 `receipt_at` / `reviewed_at` / 文件 mtime 当作证据时间；
- **退出码**：`0` 收据生成且执行与复核绑定成功 / `2` 参数错误（缺必填参数 / 时区缺失 /
  试图传 `--no-dry-run`）/ `3` inbox 或输出不可用（含把 `--out` 写进 inbox 的拒绝）/
  `4` plan、执行结果、复核结果或 ledger 损坏、被篡改或核验不通过（fail-closed，零写入）/
  `5` 收据生成但**没有任何**仍成立的批准（**预期 BLOCKED**）/ `6` 锁冲突；失败路径 stdout 为空、
  stderr 已脱敏；
- **测试**：`tests/unit/test_evidence_intake_receipt.py`、
  `tests/integration/test_evidence_intake_receipt_integration.py`
  （临时目录 / 零网络；集成用例先跑**真实** operator 落库与**真实** qualification recheck 再核验
  收据，并含真实子进程 CLI 冒烟）。

**最小操作路径：inbox → review → approved list → intake plan → 人工显式 intake → receipt verify → qualification recheck → L3 human gate**：

```powershell
# ① 发现与预检（GOLD-011）
.\.venv\Scripts\python.exe -m scripts.evidence_inbox `
  --inbox-dir logs/evidence/inbox --state logs/evidence/inbox_pending.json `
  --out logs/evidence/inbox_pending.json --json
# ② 人工复核决策 + 批准清单（GOLD-012）
.\.venv\Scripts\python.exe -m scripts.evidence_review `
  --inbox-dir logs/evidence/inbox --decision approve --fingerprint <指纹> `
  --reviewer operator-li --reason-code APPROVED_FOR_EXPLICIT_INTAKE `
  --ledger logs/evidence/inbox_review_ledger.json `
  --out logs/evidence/inbox_review_ledger.json `
  --approved-out logs/evidence/approved_for_intake.json --json
# ③ 最终写入前只读核验 + 显式衔接命令（GOLD-013）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_plan `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --out logs/evidence/evidence_intake_plan.json --json
# ④ 人工按计划里的 handoff **显式**落库并留下 manifest（GOLD-007；不自动、不可省）
.\.venv\Scripts\python.exe -m scripts.evidence_operator workflow `
  --scope author --input logs/evidence/inbox/<候选包>/author.jsonl `
  --no-dry-run --manifest logs/evidence/author_manifest.json
# ⑤ 落库后复核（生成 recheck 产物）
.\.venv\Scripts\python.exe -m scripts.evidence_operator recheck --json `
  --no-dry-run --report logs/evidence/phase33_recheck.json
# ⑥ 执行后的只读收据核验（GOLD-014；绑定 plan + 执行结果 + recheck）
.\.venv\Scripts\python.exe -m scripts.evidence_intake_receipt `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json `
  --recheck logs/evidence/phase33_recheck.json `
  --out logs/evidence/evidence_intake_receipt.json --json
```

任何一步都**不会**自动解除 `PHASE3_3_DATA`；收据里的 `intake_executed` / `receipt_verified`
只说明"人工显式落库发生过"与"绑定核验通过"，**不等于** data qualification PASS；
`qualification_recheck` 的 `ready` 也**不是**放行结论，Phase 切换仍是
`.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**。真实证据不足时 `PHASE3_3_DATA` 仍保持
**BLOCKED**（仓库内仍无足量合法授权且具可信独立时间 / availability / OOS 证据的
Author / News 数据）。

### Evidence Qualification 人工决策包（`scripts/evidence_decision_packet.py`，GOLD-015）

> **合规红线**：本工具是 **L3 人工 Gate 之前**的**只读**聚合层 —— 不联网、不写数据库、不新增
> migration / schema、**绝不**调用任何 intake / commit 路径、**绝不**移动 / 重命名 / 删除 /
> 改写 inbox 内任何原始 evidence、**绝不**修改 `.ai/PROJECT_STATE.json` 或解除 blocker。
> 它把 readiness / handoff、批准清单与 GOLD-013 plan、GOLD-014 收据与 qualification recheck
> 聚合为**内容寻址**（`packet_id`）的决策包；`evidence_ready_for_human_review` /
> `receipt_verified` / `qualification_recheck_ready` 是三个**独立**事实，
> `data_qualification_passed` / `phase_transition_allowed` **恒为** false、
> `blocker_active` / `human_gate_required` 恒为 true，`PHASE3_3_DATA` 保持 **BLOCKED**。

```powershell
# ① 只读聚合（零写入；stdout 为纯 JSON）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_packet `
  --handoff logs/evidence/handoff.json `
  --readiness logs/evidence/readiness_state.json `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json `
  --recheck logs/evidence/phase33_recheck.json `
  --receipt logs/evidence/evidence_intake_receipt.json --json

# ② 唯一写开关：显式 --out 原子落盘决策包本身（仍**不**写数据库、**不**执行任何 intake）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_packet ... `
  --out logs/evidence/evidence_decision_packet.json --json

# ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_packet ... `
  --as-of 2026-09-23T05:00:00+00:00
```

- **五个输入必填**：`--handoff`（GOLD-008 交接包）、`--inbox-dir`（GOLD-011 只读扫描）、
  `--ledger`（GOLD-012 review ledger）、`--approved-list`（GOLD-012 脱敏批准清单）、
  `--plan`（GOLD-013 intake plan）；缺任一项 → 退出码 `2`（**绝不**猜、绝不自动生成）；
- **聚合 fail-closed**：handoff 必须结构自洽（内部算术自洽 / `thresholds` 必须等于**当前**唯一
  阈值来源 / 四个安全字段不可被削弱 / **不得**出现任何证据时间键）；给出 `--readiness` 时快照
  必须与 handoff **同源**（逐字段 + **指纹**）；plan / 批准 / 显式执行结果 / recheck 的核验
  **沿用 GOLD-014 的稳定原因码**；handoff 不得早于最近一次人工显式落库（否则 `HANDOFF_STALE`）；
  可选 `--receipt` 再做一次**内容寻址**交叉核对（`RECEIPT_ID_MISMATCH` / `PLAN_ID_MISMATCH` /
  `FINGERPRINT_MISMATCH` / `REVIEW_REVISION_MISMATCH` / `RECHECK_MISMATCH`）
  → 任一不一致退出码 `4` 且**零写入**；
- **五个布尔互不蕴含**：`evidence_ready_for_human_review`（只由 readiness / handoff 推导）、
  `receipt_verified`（GOLD-014 重新绑定核验是否通过）、`qualification_recheck_ready`（recheck
  存在 + 绑定成功 + 自报 `ready`）；只有三者**同时**成立才是
  `submit_to_l3_human_gate=true`（退出码 `0`，仍**不是**资格通过），否则退出码 `5`
  （**预期 BLOCKED**，并给出缺口与稳定原因码）；
- **内容级 `packet_id`**：只由（策略块 + handoff / readiness 摘要 + `plan_id` + 收据摘要 +
  recheck 摘要 + 五个布尔 + 状态）派生，**不含** `generated_at`（同输入同 `packet_id`；同一
  审计时点下逐字节稳定；任一关键输入变化 → 新 `packet_id` 或直接 fail-closed，旧 packet
  **绝不静默继承**）；
- **没有任何 intake 参数**：本工具**没有** `--no-dry-run`，也**不**写研究数据库、**不**自动
  修改 `.ai/PROJECT_STATE.json`、**不**解除 blocker、**不**进入 Phase 3.4。

完整人工路径（**第 ①~⑦ 步**，任何一步都**不会**自动解除 `PHASE3_3_DATA`）：

```powershell
# ⑦ L3 人工决策包（GOLD-015；聚合 readiness / handoff + 批准 + plan + 收据 + recheck）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_packet `
  --handoff logs/evidence/handoff.json `
  --readiness logs/evidence/readiness_state.json `
  --inbox-dir logs/evidence/inbox `
  --ledger logs/evidence/inbox_review_ledger.json `
  --approved-list logs/evidence/approved_for_intake.json `
  --plan logs/evidence/evidence_intake_plan.json `
  --operator-result logs/evidence/author_manifest.json `
  --recheck logs/evidence/phase33_recheck.json `
  --receipt logs/evidence/evidence_intake_receipt.json `
  --out logs/evidence/evidence_decision_packet.json --json
```

`submit_to_l3_human_gate=true` 只表示「量化门槛达标 + 显式落库可核验 + 复核已完成」**同时**
成立，可提交 **L3 人工 Gate** 复核真实授权与历史可用性证据；它**不是**资格通过，也不代表任何
数据已被允许用于训练。真实证据不足时 `PHASE3_3_DATA` 仍保持 **BLOCKED**。

### Evidence Qualification L3 人工决策记录（`scripts/evidence_decision_record.py`，GOLD-016）

> **合规红线**：本工具是 **L3 人工 Gate** 的**人工决策审计层** —— 不联网、不写数据库、不新增
> migration / schema、**绝不**调用任何 intake / commit 路径、**绝不**移动 / 重命名 / 删除 /
> 改写原始 evidence、**绝不**修改 `.ai/PROJECT_STATE.json`、**绝不**解除 blocker、**绝不**执行
> Phase transition。它把**人工显式**给出的 `approve` / `reject` / `needs_changes` 绑定到
> **具体**的 GOLD-015 packet（`packet_id` + `content_sha256` + `artifact_sha256`），
> `human_decision_recorded` / `human_decision` / `packet_verified` 是三个**独立**事实，
> `data_qualification_passed` / `phase_transition_allowed` / `phase_transition_executed`
> **恒为** false、`blocker_active` / `human_gate_required` 恒为 true，`PHASE3_3_DATA`
> 保持 **BLOCKED**。**记录一份人工决策 ≠ 资格通过。**

```powershell
# ① 只读预检：打印"将要写入"的决策记录（零写入；stdout 为纯 JSON）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record `
  --packet logs/evidence/evidence_decision_packet.json `
  --decision approve `
  --reviewer operator-l3-li `
  --reason-code APPROVED_AFTER_L3_REVIEW `
  --note "已人工复核授权来源与独立历史可用性证据" --json

# ② 唯一写开关：显式 --out 原子落盘记录本身（仍**不**写数据库、**不**改 PROJECT_STATE）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record ... `
  --out logs/evidence/evidence_l3_human_decision_record.json --json

# ③ 覆盖既有记录必须**显式**声明 revision + supersedes（绝不静默改写历史）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record ... `
  --revision 2 --supersedes <既有 record_id> `
  --out logs/evidence/evidence_l3_human_decision_record.json --json

# ④ 防伪核验（纯只读）：把既有记录与**当前** packet 逐项比对
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record `
  --packet logs/evidence/evidence_decision_packet.json `
  --verify-record logs/evidence/evidence_l3_human_decision_record.json --json
```

- **人工输入必须显式**：`--packet` / `--decision` / `--reviewer` 必填，缺任一项 → 退出码 `2`
  （**绝不**猜、**绝不**自行生成批准）；`--decision` 只接受 `approve` / `reject` /
  `needs_changes`（大小写敏感，不做静默归一）；
- **`approve` 门禁**：只有 packet 本身 `submit_to_l3_human_gate=true` **且**完整性核验
  （文档身份 / 安全字段 / 缺口算术 / 三个布尔合取 / readiness 同源 / 收据与 recheck 绑定 /
  计数自洽 / **禁止证据时间键** / **禁止未来时间**）**全部**通过时才允许记录；packet
  `BLOCKED` → `PACKET_NOT_SUBMITTABLE`（退出码 `5`、零写入）；`reject` / `needs_changes`
  可记录但**绝不**改变任何资格状态；
- **fail-closed**：packet 缺失 / 损坏 / 被篡改 / 内容或 `packet_id` 不一致 / 出现证据时间键 /
  出现未来时间 / 既有记录被改写 / 与既有记录冲突 / 并发锁冲突 → 稳定原因码 + **零写入**
  （退出码 `3` / `4` / `6`）；
- **幂等 / 内容寻址 / 不静默改写历史**：`record_id` 只由（策略块 + `decision` + `reviewer` +
  受约束的 `note` / `reason_code` + `revision` / `supersedes` + packet 绑定摘要）派生，
  **不含**任何审计时间；写下的记录**绝不**被静默覆盖（必须显式 `--revision` +
  `--supersedes` 指向当前记录，且 revision 必须严格递增）；
- **身份与脱敏**：`--reviewer` 只接受**非敏感 label**（拒绝空值 / 超长 / 非法字符 /
  看起来像凭据的取值；**不采集**任何口令、token、密钥）；`--note` / `--reason-code` 一律走
  `src.common.redaction` **脱敏并限长**（过长输入 fail-closed，不静默截断成假事实）；
- **操作时间不是证据时间**：`decision_at` / `generated_at` / packet 的 `generated_at` /
  文件 mtime **都只是审计操作时间**，记录里 `evidence_time_semantics.contains_evidence_times`
  **恒为** false，加载 packet 与既有记录时都会**递归拒绝**任何证据时间键。

完整人工路径（**八步**，任何一步都**不会**自动解除 `PHASE3_3_DATA`）：

```powershell
# ⑧ L3 人工决策记录（GOLD-016；绑定具体 packet_id + 内容摘要，并可事后防伪核验）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record `
  --packet logs/evidence/evidence_decision_packet.json `
  --decision approve --reviewer operator-l3-li `
  --reason-code APPROVED_AFTER_L3_REVIEW `
  --out logs/evidence/evidence_l3_human_decision_record.json --json

# 事后审计：任何人可用同一 packet 复核这份记录是否仍然成立（只读）
.\.venv\Scripts\python.exe -m scripts.evidence_decision_record `
  --packet logs/evidence/evidence_decision_packet.json `
  --verify-record logs/evidence/evidence_l3_human_decision_record.json --json
```

人工决策记录**只证明**「某个具体 `packet_id` 收到过**明确**的人工决策」；它**不是**资格通过，
也**不是** Phase 切换授权：真实数据资格仍需独立复核（授权 / 独立历史可用性 / OOS 证据），
Phase 切换仍需 `.ai/DEVELOPMENT_PROTOCOL.md` 的 **L3 人工确认**并由人工 / Orchestrator
显式更新 `PROJECT_STATE`（**不由**本工具完成）。真实证据不足时 `PHASE3_3_DATA` 仍保持
**BLOCKED**。

### Evidence 本地 Package / Manifest Builder（`scripts/evidence_package.py`，GOLD-017）

> **合规红线**：本工具是 **纯本地机械整理** 入口 —— 只把**人工显式提供**的授权 / 时间 /
> availability / OOS 元数据与**人工指定的现有 evidence 文件**整理成 GOLD-011 inbox 可直接消费的
> `manifest.json`；**不推断授权**、**不伪造发布时间 / 采集时间 / availability 语义**、
> 不联网、不写数据库、**不移动 / 删除 / 改写**任何原始 evidence、**不自动 intake**。
> `blocker_active` / `human_gate_required` / `requires_human_action` 恒为 true；
> `data_qualification_passed` / `phase_transition_allowed` / `auto_intake_allowed` /
> `writes_database` 恒为 false；`PHASE3_3_DATA` **保持 BLOCKED**。
> **生成 manifest ≠ 授权已核验 ≠ 资格通过 ≠ 可提交 L3。**

```powershell
# ① 默认 dry-run：只打印确定性、脱敏的 manifest 预览与逐行预检（零写入、零网络）
.\.venv\Scripts\python.exe -m scripts.evidence_package `
  --package-dir logs/evidence/inbox/vendor-author-2026-06 `
  --file author-2026-06.jsonl `
  --evidence-type author --source vendor-author `
  --authorization-reference https://vendor.example/terms `
  --time-semantics provider_export_iso8601_with_tz `
  --availability-semantics provider_archive_export_daily_snapshot `
  --historical-oos-applicable true --json

# ② 唯一写开关：--out 必须**正好**是 <package-dir>/manifest.json（原子写 + 单实例锁）
.\.venv\Scripts\python.exe -m scripts.evidence_package ... `
  --out logs/evidence/inbox/vendor-author-2026-06/manifest.json --json

# ③ 端到端：写完后交给**真实** GOLD-011 inbox scanner 做只读预检
.\.venv\Scripts\python.exe -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox --json
```

- **元数据必须显式**：`--evidence-type`（`author` / `news`，大小写敏感）/ `--source` /
  `--authorization-reference` / `--time-semantics` / `--availability-semantics` /
  `--historical-oos-applicable true|false` 与 `--file`（可重复）**缺一即 fail-closed**；
  工具**绝不**从文件名 / 正文 / URL / mtime / 当前时间或其他上下文推断 / 补造授权、发布时间、
  采集时间、availability 或 OOS 语义；
- **复用同一契约**：manifest 字段 / 允许键 / 必填项 / 格式词表**直接复用** GOLD-011 的
  `MANIFEST_REQUIRED_FIELDS` / `ALLOWED_MANIFEST_KEYS` / `FILE_ENTRY_REQUIRED_FIELDS` /
  `SUPPORTED_FILE_FORMATS` 与 `evidence-intake-v1`（`schema_version` / `contract_version` 自动写入），
  引用校验复用 `valid_reference`、逐行预检复用 `assess_row`，**不复制、不降低**任何规则；
- **内容级 SHA-256 且确定性排序**：每个文件按**原始字节**计算 SHA-256；`manifest.files` 按规范化
  相对路径排序；同一输入 + 同一显式元数据（含声明顺序不同）→ **byte-stable** manifest；
  manifest 里**没有**任何时间字段（真实身份 = 文件内容摘要），文件 mtime / ctime 绝不写入；
- **默认零写入**：不传 `--out` 时只打印 stdout（不取锁、不写任何文件）；`--out` 只能指向
  `<package-dir>/manifest.json`，指向任何其它路径 → 退出码 `3`、零写入；
- **不静默覆盖**：既有 manifest 与本次规范化内容**逐字节一致** → 幂等（`IDEMPOTENT_UNCHANGED`）；
  **内容不同** → `MANIFEST_CONFLICT`（退出码 `5`、零写入）；**没有** `--force` / `--overwrite`，
  更正必须新建 package 目录 / 新的内容身份；
- **写盘前同源预检**：先做与 GOLD-005 / GOLD-011 **同源**的只读逐行预检，输出 `accepted` /
  `quarantined` / `not_oos_eligible` / `NO_ROWS` 与稳定原因码（含逐文件计数）；
  预检**不写入** evidence 原始行，`preflight` 通过**不表示**授权已人工核验、**不表示**数据资格通过；
- **fail-closed 清单**：绝对路径 / `..` / 多级路径 / 符号链接 / 目录 / `manifest.json` 自引用 /
  未支持扩展或格式 / 重复或大小写冲突声明 / 包目录不存在 / 包内未声明文件 / package 目录名或文件名
  命中示例合成词表 / 敏感值（凭据键名、疑似 blob、`api_key=` 之类可被脱敏规则命中的取值）；
  **越界文件从不被读取**；
- **锁文件刻意放在 package 目录之外**（缺省 = 同父目录 `<package-dir>.manifest.lock`），
  否则锁文件会成为「包内未声明文件」并让 scanner 整包隔离；package 位于 inbox 内时建议显式
  `--lock <inbox 之外的工作目录>/xxx.lock`；
- **退出码**：`0` 预览已生成 / manifest 已写入或幂等未变（**仍不是**资格通过）/ `2` 参数错误 /
  `3` package 目录或输出路径不可用（含 `--out` 越界）/ `4` 输入未通过 fail-closed 校验 /
  `5` `MANIFEST_CONFLICT` / `6` 锁冲突；失败路径 stdout 为空、stderr 已脱敏；
- **测试**：`tests/unit/test_evidence_package_builder.py`、
  `tests/integration/test_evidence_package_builder_integration.py`（全部临时目录 / Mock / 本地文件、
  零网络、零数据库；端到端用例先用 builder 生成 manifest，再用**真实** GOLD-011 scanner 预检，
  并验证「证据被改写后 scanner 仍 fail-closed、builder 再次写入被 `MANIFEST_CONFLICT` 拒绝」）；
- **仍未解决（保持 BLOCKED）**：本工具**只降低真实证据的交付摩擦**（免手工写 manifest / 手工算
  SHA-256），**不产生**任何真实授权证据、**不解除** `PHASE3_3_DATA`；真实资格仍需业务方授权 +
  人工核验 + L3 人工 Gate。

真实 evidence 的**人工路径从「package builder → inbox」开始**（完整九步见 `PROGRESS_LOG.md`）：

```powershell
# ⓪ GOLD-017：显式元数据 → manifest（默认 dry-run；确认无误后再加 --out）
.\.venv\Scripts\python.exe -m scripts.evidence_package `
  --package-dir logs/evidence/inbox/vendor-author-2026-06 --file author-2026-06.jsonl `
  --evidence-type author --source vendor-author `
  --authorization-reference https://vendor.example/terms `
  --time-semantics provider_export_iso8601_with_tz `
  --availability-semantics provider_archive_export_daily_snapshot `
  --historical-oos-applicable true --json
.\.venv\Scripts\python.exe -m scripts.evidence_package ... `
  --out logs/evidence/inbox/vendor-author-2026-06/manifest.json

# ① GOLD-011：真实 scanner 只读预检 → 交付**人工确认队列**

.\.venv\Scripts\python.exe -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox --json
```

### 包边界与导入顺序（`src/evidence/__init__.py` / `src/monitoring/__init__.py`，GOLD-018）

> **合规红线**：本项**只治理包边界**（模块导入顺序），不改任何资格算法 / 阈值 / 证据契约 /
> 安全字段 / L3 / L4 Gate；`PHASE3_3_DATA` **保持 BLOCKED**（见 TD-60）。

- **修复的真实缺陷**（GOLD-017 任务外发现，纯净 HEAD 复现）：

  ```text
  .\.venv\Scripts\python.exe -c "import src.monitoring; import src.evidence"
  ImportError: cannot import name 'BatchQuantification' from partially initialized module
  'src.monitoring.evidence_readiness' (most likely due to a circular import)
  ```

  旧版两个包的 `__init__` 在**包初始化阶段**就 eager import 整个依赖图
  （`src.evidence.__init__` → `decision_packet` → `readiness_runner` → `handoff` →
  `src.monitoring.evidence_readiness` → `src.evidence.contracts`），于是"先导入谁"决定成败：
  monitoring-first 必崩、evidence-first 正常；
- **修复方式**：两个包的 `__init__` 改为 **PEP 562 惰性导出**（模块级 `__getattr__` + `__dir__`），
  初始化阶段**不加载任何子模块**；惰性表只登记 `公开名 -> 定义子模块`（别名单独登记），
  **不复制**任何类 / 阈值 / 枚举 / 常量，单一事实源仍是各子模块。`__all__` 未变，
  以下既有用法**完全保持可用**：`from src.evidence import EvidenceIntakeReport`、
  `from src.monitoring import PHASE3_3_BLOCKER_CODE`、`from src.evidence import decision_packet`、
  `import src.monitoring` 后再取任意公开属性；
- **门禁（已由测试锁定）**：任意导入顺序（monitoring-first / evidence-first / 直接子模块 first /
  from-import / star-import / 重复与交错导入）在 **fresh subprocess** 中全部成功，且包初始化不再
  拉入对方包（`tests/integration/test_evidence_import_order.py`）；`__all__` 与惰性表同源、每个
  公开名 `is` 其定义子模块上的对象、源码级禁止 eager 子模块导入
  （`tests/unit/test_lazy_package_exports.py`）；14 个 `scripts/evidence_*.py` +
  `scripts/intake_evidence.py` + `scripts/report_collector_health.py` 均可 import + `--help`
  可用，且 cwd 为空的临时目录 → 断言**零文件写入**（`tests/integration/test_evidence_cli_smoke.py`）。

### PHASE3_3_DATA 证据缺口只读诊断（`scripts/evidence_gap_diagnostic.py`，GOLD-026）

> **合规红线**：本项**只做只读诊断** —— 不采集、不写库、不联网、不绕过 robots / 证书、
> 不放宽任何资格门槛；`PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

- **一句话**：把"还缺什么、必须由谁完成"汇总成**单一**诊断（机器可读 JSON + 人类可读 Markdown），
  供 GPT 与人工判断剩余的真实授权 Author / News 证据缺口；
- **复用而不复制**：缺口 / 阈值 / 清单完全复用 readiness（GOLD-006）+ handoff（GOLD-008）+
  inbox manifest 预检（GOLD-011）+ `evidence-intake-v1` 契约字段与原因码，**不新增 / 不降低**阈值；
- **四态分类**（每项机器可读）：`CODE_READY`（代码 / 契约已就绪）/
  `EVIDENCE_MISSING`（真实证据缺失、未 ingest 或未达标）/
  `HUMAN_VERIFICATION_REQUIRED`（证据已在，法律效力 / 出处 / 时间语义只能人工核验）/
  `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate，工具永不解除）；
- **必看结论**：顶层 `readiness` 恒为 `GATE_BLOCKED`；`advance_allowed` /
  `l3_l4_auto_advance_allowed` / `data_qualification_passed` / `phase_transition_allowed`
  恒为 `false`；Mock / 模板 / 示例一律 `counts_toward_eligibility=false`；
- **不伪造缺失字段**：`published_at` / `collected_at` / `effective_at` / `available_at` / OOS
  证据缺失时只报告事实与人工下一步，**绝不**用当前时间 / 文件 mtime / 抓取时间 / 推断值填补；
- 用法：

  ```bash
  # ① 只读诊断（默认只打印 stdout；零写入、零网络）
  .venv\Scripts\python.exe -m scripts.evidence_gap_diagnostic --json

  # ② 固定审计时点 + 纳入本地候选 manifest 事实（仍零写入、不 ingest）
  .venv\Scripts\python.exe -m scripts.evidence_gap_diagnostic \
      --as-of 2026-09-22T12:00:00+00:00 --inbox-dir logs/evidence/inbox

  # ③ 唯一写开关：显式 --out 原子落盘诊断本身（不写库、不 intake、不解除 BLOCKED）
  .venv\Scripts\python.exe -m scripts.evidence_gap_diagnostic --json \
      --out logs/evidence/gap_diagnostic.json
  ```

- 退出码：`0` 无实质证据缺口（gate 仍 BLOCKED）/ `2` 参数或输入错误 /
  `4` `--out` 不可写 / `5` 仍有实质证据缺口（**当前预期**：`PHASE3_3_DATA` 保持 BLOCKED）；
- 回归测试：`tests/unit/test_evidence_gap_diagnostic.py`（25 项）+
  `tests/integration/test_evidence_gap_diagnostic_integration.py`（8 项），其中包含
  "量化门槛全达标仍 BLOCKED"、"mtime 绝不作为证据时间"、"无网络 / 无写库 / 无 `os.stat`"
  等源码级与运行期守卫。

### 人工证据 Intake Handoff 契约与只读预检（`scripts/evidence_intake_handoff.py`，GOLD-027）

> **合规红线**：本项**只做纯本地只读预检** —— 不采集、不写库、不联网、不绕过 robots / 证书、
> 不放宽任何资格门槛；`preflight_pass` **只**表示 package 结构完整，`PHASE3_3_DATA`
> **保持 BLOCKED**，L3 / L4 只能人工推进。

- **一句话**：把 GOLD-026 的只读缺口诊断变成**单一、版本化**的人工证据提交 / 预检 handoff ——
  业务人员按**同一契约**准备真实授权 Author / News 材料，预检只回答"材料是否齐全、
  字段是否可解析、来源 / 时间声明是否带独立证据引用"，**绝不**回答"是否合格"；
- **版本化 schema**（`--schema` / `intake_handoff_schema()`）：`kind` +
  `schema_version=1` + `contract_version=evidence-intake-v1` + 目录契约（沿用 GOLD-011 inbox
  布局：`manifest.json` 必填声明 + `files[path, sha256]`）+ Author / News 必需契约字段
  （Author 额外 `author_name` / `external_account_id`）+ 10 条材料契约 + 6 条独立时间语义要求
  + 状态词表；阈值**只引用** `src.alpha.evidence_gate`（经 `handoff.thresholds`），
  **不新增、不降低**任何资格门槛；
- **五态状态**（每个材料机器可读）：`MISSING`（缺失或机械校验未通过）/
  `PRESENT_UNVERIFIED`（已提交但独立证据引用不足，不能进入人工核验队列）/
  `HUMAN_VERIFICATION_REQUIRED`（结构完整：法律效力 / 出处 / 时间语义只能人工核验）/
  `NON_QUALIFYING`（Mock / 模板 / 示例 / 合成：**永远**不计资格）/
  `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate，Gate 材料恒为此态）；
- **必看结论**：`preflight_pass` **只**表示结构完整（无缺失、无待核验缺口、非合成、
  GOLD-011 预检零原因码）；`evidence_qualified` / `data_qualification_passed` /
  `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒为 `false`；
  `blocker_active` / `human_gate_required` / `gate_blocked` 恒为 `true`；
- **不伪造时间事实**：缺 `time_semantics` / 缺 `available_at` 时只报告字段名与人工下一步，
  **绝不**用当前时间 / 文件 mtime / 抓取时间 / 推断值填补；
- 用法：

  ```bash
  # ① 打印版本化 handoff 契约（人工 / 业务方据此准备材料；零写入）
  .venv\Scripts\python.exe -m scripts.evidence_intake_handoff --schema

  # ② 只读预检显式本地候选目录（默认只打印 stdout；零写入、零数据库）
  .venv\Scripts\python.exe -m scripts.evidence_intake_handoff \
      --inbox-dir logs/evidence/inbox --json

  # ③ 固定审计时点（ISO8601 必须带时区）
  .venv\Scripts\python.exe -m scripts.evidence_intake_handoff \
      --inbox-dir logs/evidence/inbox --as-of 2026-09-23T00:00:00+00:00

  # ④ 唯一写开关：显式 --out 原子落盘预检本身（不写库、不 intake、不解除 BLOCKED）
  .venv\Scripts\python.exe -m scripts.evidence_intake_handoff \
      --inbox-dir logs/evidence/inbox --json \
      --out logs/evidence/intake_handoff.json
  ```

- 退出码：`0` 至少一个候选包**结构完整**（仍**不是**资格通过）/ `2` 参数或输入错误 /
  `4` `--out` 不可写 / `5` 没有任何结构完整的候选包（**当前预期**：`PHASE3_3_DATA` 保持 BLOCKED）；
- 回归测试：`tests/unit/test_evidence_intake_handoff.py`（32 项）+
  `tests/integration/test_evidence_intake_handoff_integration.py`（13 项），其中包含
  "结构完整 ≠ 资格通过"、"Mock / 缺授权 / 缺独立时间语义 / 缺独立可用证据全部 fail-closed"、
  "除 `as_of` 外无任何时间戳、mtime 绝不出现"、"候选证据内容与 mtime 零变化"、
  "无网络 / 无写库 / 无 `open` / 无 `os.stat`"等源码级与运行期守卫。

### 材料级人工核验凭证（`scripts/evidence_human_verification_attestation.py`，GOLD-028）

> **合规红线**：本项**只做纯本地只读预检 + 人工凭证绑定** —— 不采集、不写库、不联网、不绕过
> robots / 证书、不放宽任何资格门槛；`all_required_verified` **只**代表**材料级人工核验完成**，
> `PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

- **一句话**：把 GOLD-027 的 handoff **结构完整预检**进一步转换为**可审计的材料级人工核验
  凭证** —— 明确记录「授权证明 / 来源身份与出处 / `published_at` / `collected_at` /
  `effective_at` 独立时间语义 / availability-OOS 独立证据」分别**由谁、何时、依据哪条独立引用**
  核验成了什么；
- **内容寻址**（`attestation_id`）：由「硬编码策略块 + `revision` / `supersedes` + package 绑定
  + 逐材料核验」派生（**不含**审计时间，同输入 → byte-stable），并硬绑定**当前**候选包
  `fingerprint` + package 内容身份 + GOLD-027 handoff / manifest 内容身份
  （`handoff_content_sha256`）+ `scope`；任何 package / manifest / content fingerprint 漂移都使
  旧凭证**失效**（`verify` 模式给出 `PACKAGE_FINGERPRINT_DRIFT` / `PACKAGE_CONTENT_DRIFT` /
  `HANDOFF_CONTENT_DRIFT`，**绝不静默继承**）；
- **受控人工输入**（`--verification`，本地 JSON）：每个必核验材料必须有
  `VERIFIED` / `REJECTED` / `NEEDS_CHANGES` 决策 + 稳定大写 `reason_code` + 显式 `reviewer`
  label（**不采集任何凭据**）+ 带时区 `reviewed_at`（**不是**证据时间，naive / 未来一律拒绝）
  + 独立 `evidence_reference`（`https://...` 或 `docs/legal/...`）；未知字段 / 重复材料 / 疑似
  凭据 / 裸文件名一律 fail-closed；
- **必看结论**：`all_required_verified=true` **只**表示「非 Mock / 模板 / 示例 / 合成、
  GOLD-027 `preflight_pass=true`、每个必核验材料都有带独立引用的 `VERIFIED`」；
  `evidence_qualified` / `data_qualification_passed` / `phase_transition_allowed` /
  `advance_allowed` / `l3_l4_auto_advance_allowed` 恒为 `false`；`blocker_active` /
  `human_gate_required` / `gate_blocked` 恒为 `true`（**硬编码**）；
- **Mock 永不 qualify**：Mock / 模板 / 示例 / 合成、`preflight_pass=false`、以及结构上不是
  `HUMAN_VERIFICATION_REQUIRED` 的材料**一律拒绝**被声明为 `VERIFIED`
  （`NON_QUALIFYING_PACKAGE` / `PREFLIGHT_NOT_PASSED` / `MATERIAL_NOT_VERIFIABLE`）；
- **既有凭证不可静默改写**：同 `attestation_id` 幂等；否则必须显式
  `--revision >= 2 --supersedes <attestation_id>`；凭证被改写（决策 / 计数 / 安全字段 /
  独立引用）→ `verify` 一律 `ATTESTATION_TAMPERED` fail-closed；
- 用法：

  ```bash
  # ① 打印版本化凭证契约（零写入、零网络）
  .venv\Scripts\python.exe -m scripts.evidence_human_verification_attestation --schema

  # ② 只读预检：按人工核验输入生成凭证（默认只打印 stdout；零写入、零数据库）
  .venv\Scripts\python.exe -m scripts.evidence_human_verification_attestation \
      --inbox-dir logs/evidence/inbox \
      --verification logs/evidence/human_verification.json --json

  # ③ 唯一写开关：显式 --out 原子落盘凭证（不写库、不 intake、不解除 BLOCKED）
  .venv\Scripts\python.exe -m scripts.evidence_human_verification_attestation \
      --inbox-dir logs/evidence/inbox \
      --verification logs/evidence/human_verification.json --json \
      --out logs/evidence/phase33_human_verification_attestation.json

  # ④ 防伪核验（纯只读）：重新绑定当前 package 并检测漂移 / 篡改 / 缺引用
  .venv\Scripts\python.exe -m scripts.evidence_human_verification_attestation \
      --inbox-dir logs/evidence/inbox \
      --verify-attestation logs/evidence/phase33_human_verification_attestation.json --json
  ```

- 退出码：`0` 全部必核验材料已有 `VERIFIED`（仍**不是**资格通过）/ `2` 参数或输入错误 /
  `3` 路径 / 输出不可用（含把凭证写进 inbox 或覆盖核验输入的拒绝）/
  `4` 漂移 / 篡改 / 冲突 / 非法人工输入（fail-closed，零写入）/
  `5` 未全部核验或不可核验（**当前预期**：`PHASE3_3_DATA` 保持 BLOCKED）；
- 回归测试：`tests/unit/test_evidence_human_verification_attestation.py`（41 项）+
  `tests/integration/test_evidence_human_verification_attestation_integration.py`（18 项），
  其中包含「材料级核验 ≠ 资格通过」「缺授权证明 / 缺独立时间语义 / 缺 availability-OOS 全部
  fail-closed」「Mock / 模板永不 qualify」「package / handoff 内容漂移使旧凭证失效」
  「凭证篡改 / 安全字段削弱 / 伪造 `all_required_verified` 全部 fail-closed」
  「重复幂等 + 显式 `revision` / `supersedes`」「敏感值 / naive 或未来 `reviewed_at` 一律拒绝」
  「除审计时点与人工 `reviewed_at` 外无任何时间戳、mtime 绝不出现」
  「候选证据内容与 mtime 零变化」「无网络 / 无写库 / 无 `open` / 无 `os.stat`」等源码级与
  运行期守卫。


### APPROVE 门禁绑定材料级人工核验凭证（`scripts/evidence_review.py --attestation`，GOLD-029）

- **为什么**：GOLD-028 只产出了凭证；GOLD-029 把 GOLD-012 的 `APPROVE` 从"人工填一个受控
  reason code 就能批准"升级为**必须绑定一份当前、完整、未漂移的 GOLD-028 凭证**；
- **门禁（fail-closed、零写入）**：`--decision approve` **必须**显式给出
  `--attestation <phase33_human_verification_attestation.json>`，记录前用
  `load_attestation_document()` + `verify_attestation()` 与**当前**候选目录逐项重新绑定核验：
  凭证文档完整性（重新推导 `attestation_id` / 安全字段 / 计数自洽 / 无证据时间键）、
  `package fingerprint`、package 内容身份、`handoff` 内容身份、`scope`、`preflight_pass`、
  `all_required_verified=true`；任一不匹配 → 退出码 `4`（`ATTESTATION_MISSING` /
  `ATTESTATION_INCOMPLETE` / `ATTESTATION_STALE` / `ATTESTATION_TAMPERED`）且**零写入**；
- **只保存最小绑定**：新 `APPROVE` 记录只带 `attestation` 白名单块（binding 版本、GOLD-028
  schema / contract 版本、`attestation_id`、凭证文档 `sha256`、package / handoff 内容身份、
  `scope`、`all_required_verified`），**绝不**保存凭证正文、材料备注、`reviewer` 或任何证据时间；
- **负向决策不被阻塞**：`reject` / `needs_changes` **不**需要凭证；
- **历史只读兼容、绝不追溯升级**：旧 ledger（没有 binding 的 `APPROVE`）仍可读、`decision_id`
  派生口径不变、文件**绝不**被原地迁移 / 改写；但
  `build_approved_intake_list()` / `evidence_intake_plan` 会以稳定原因码 `ATTESTATION_MISSING`
  把它列入 `invalidated`（新门禁只对后续 / 当前重新验证生效）；
- **批准清单 / 计划链重新验证**：生成批准清单时用**当前** inbox / handoff 重新推导绑定身份，
  被批准候选包的材料结构内容身份或 `scope` 漂移 → `ATTESTATION_STALE`；
  `evidence_intake_plan` 的 `PlanVerificationCode` 显式映射这四个原因码；
- **用法**：`evidence_human_verification_attestation ... --out <凭证>` →
  `evidence_review --decision approve --fingerprint <fp> --reviewer <label> --reason-code
  APPROVED_FOR_EXPLICIT_INTAKE --attestation <凭证> --out <ledger> --approved-out <list>`；
  `--help` 暴露 `--attestation`，但**绝不**暴露任何 `qualify` / `advance` / `--no-dry-run` 开关；
- **回归测试**：`tests/unit/test_evidence_review.py`（新增 GOLD-029 用例）+
  `tests/integration/test_evidence_review_integration.py`（CLI 缺凭证 / 凭证篡改 / reject 无凭证）
  \+ `tests/unit/test_evidence_intake_plan.py`（计划链 `ATTESTATION_MISSING` / `ATTESTATION_STALE`）。


### 证据链端到端只读审计与单一 rehearsal 命令（`scripts/evidence_chain_audit.py`，GOLD-030）

> **合规红线**：本项**只做纯本地只读审计 / 演练** —— 不采集、不写库、不联网、不执行 intake、
> 不移动 / 删除 / 改写任何原始 evidence 或历史 artifact；演练只用 fixture /
> Mock operator result，`PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

- **一句话**：把 `GOLD-027 handoff → GOLD-028 材料级人工核验凭证 → GOLD-012/029 review 批准 →
  GOLD-013 plan → 人工显式执行结果 → GOLD-014 receipt/recheck → GOLD-015 packet →
  GOLD-016 L3 决策记录` 串成**一条**可重复、**fail-closed** 的端到端验收链：逐段核验
  artifact identity / schema / content binding，任一漂移都给出**最早失败阶段**与**稳定原因码**；
- **复用而不复制**：每一段都直接调用既有核验入口（`load_intake_handoff` /
  `verify_attestation` / `build_approved_intake_list` / `build_intake_plan` /
  `verify_intake_receipt` / `verify_decision_packet` / `verify_decision_record`），
  本工具**不新增、不降低**任何资格阈值，也不重新解释任何契约字段；
- **八段固定顺序**（`stage_order`）：`intake_handoff` / `attestation` / `review_approval` /
  `approved_list_and_plan` / `operator_result` / `intake_receipt` / `decision_packet` /
  `decision_record`；任一段未通过即**短路**，其后为 `NOT_EVALUATED`；
- **内容身份**：确定性 `facts_digest`（各段身份事实：指纹 / 摘要 / id / 计数）与
  `chain_id`（策略块 + 段 / 状态 / 原因码 + `facts_digest`）；任一 artifact 漂移
  （含某段从 `PASS` 变 `FAIL`）必然产生**新** `chain_id`；
- **四个独立结论**：`engineering_chain_ready`（工程链契约是否全部一致）/
  `real_evidence_missing` / `human_verification_missing` / `l3_human_gate_pending`；
  **即使工程链全绿**，`data_qualification_passed` / `phase_transition_allowed`
  **恒为 false**、`blocker_active` / `human_gate_required` / `gate_blocked` 恒为 true
  （**硬编码**），`PHASE3_3_DATA` 保持 BLOCKED；
- **演练只证明工具链健康**：`evidence_source=rehearsal_fixture` 时
  `real_evidence_missing` / `human_verification_missing` / `l3_human_gate_pending`
  **恒为 true**：fixture / Mock **绝不**被当作真实资格证据、**绝不**解除 blocker、
  **绝不**产生真实 L3 批准；
- **用法**（面向 operator 的**单一** rehearsal 命令 + 只读审计）：

  ```bash
  # ① 单一 rehearsal 命令：先验证工具链健康（纯本地 fixture + Mock 执行结果）
  .venv\Scripts\python.exe -m scripts.evidence_chain_audit ^
      --rehearsal --work-dir logs/chain_rehearsal --json

  # ② 诚实 BLOCKED 场景（packet 不可提交 + reject 记录）
  .venv\Scripts\python.exe -m scripts.evidence_chain_audit ^
      --rehearsal --work-dir logs/chain_rehearsal_blocked --scenario blocked --json

  # ③ 只读审计**真实** artifact 链（零写入；stdout 为纯 JSON）
  .venv\Scripts\python.exe -m scripts.evidence_chain_audit ^
      --inbox-dir logs/evidence/inbox --handoff logs/evidence/handoff.json ^
      --attestation logs/evidence/phase33_human_verification_attestation.json ^
      --ledger logs/evidence/inbox_review_ledger.json ^
      --approved-list logs/evidence/approved_for_intake.json ^
      --plan logs/evidence/evidence_intake_plan.json ^
      --operator-result logs/evidence/author_manifest.json ^
      --recheck logs/evidence/phase33_recheck.json ^
      --readiness logs/evidence/readiness_state.json ^
      --receipt logs/evidence/evidence_intake_receipt.json ^
      --packet logs/evidence/evidence_decision_packet.json ^
      --record logs/evidence/l3_human_decision_record.json --json

  # ④ 唯一写开关：显式 --out 原子落盘**审计报告本身**（仍不写库、不 intake、不解除 blocker）
  .venv\Scripts\python.exe -m scripts.evidence_chain_audit --rehearsal ^
      --work-dir logs/chain_rehearsal --json ^
      --out logs/evidence/phase33_evidence_chain_audit.json
  ```

- **故障定位口径**（业务方提交真实证据**前**的预检流程）：先跑 ①；若 `exit=5`，读 JSON 的
  `earliest_failure_stage` 与对应段的 `reason_codes`——例如 `attestation` 段的
  `ATTESTATION_STALE` / `CHAIN_INVALID_CANDIDATE` 说明"提交材料或凭证已漂移，需重新核验"，
  `intake_receipt` 段的 `RECEIPT_ID_MISMATCH` 说明"收据与当前 plan/执行/复核不再一致"，
  `decision_record` 段的 `RECORD_ID_MISMATCH` / `PACKET_CONTENT_MISMATCH` 说明"记录绑定的
  packet 已变化"。修复该段后**必须重新生成下游 artifact**（旧结论绝不静默继承）再重跑；
- 退出码：`0` 工程链全部 PASS（**不是**资格通过）/ `2` 参数或时区错误 /
  `3` 路径不可用（inbox / 输出不可用、把输出写进 inbox、演练工作目录落在仓库非 `logs/` 之处）/
  `4` artifact 损坏 / 被篡改 / 漂移或校验失败（fail-closed，零写入）/
  `5` 证据链未走通（**当前预期**：缺真实证据 / 未人工核验）/ `6` 锁冲突；
- 回归测试：`tests/unit/test_evidence_chain_audit.py`（44 项）+
  `tests/integration/test_evidence_chain_audit_integration.py`（16 项），其中包含
  「happy path（ready / blocked）8 段全 PASS」「每一关键绑定点的 tamper / stale / missing →
  稳定原因码 + 最早失败阶段」「`chain_id` / `facts_digest` 确定性且漂移必变」
  「工程链全绿仍 `data_qualification_passed=false` / `blocker_active=true`」
  「审计与演练**零改写**、`--out` 只写报告本身、写进 inbox 被拒」
  「演练工作目录**不得**污染仓库」「fresh subprocess 零副作用 + `--help` 冒烟」
  「源码级守卫：审计 / 演练模块不 import 数据库 / 网络 / 子进程」等。


### 人工证据提交就绪包（`scripts/evidence_submission_readiness.py`，GOLD-033）

> **合规红线**：本项**只做只读聚合** —— 不采集、不写库、不联网、不执行 intake、不移动 /
> 删除 / 改写任何原始 evidence 或历史 artifact；Mock / fixture / 模板**永不**计为真实材料；
> `PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

- **一句话**：把 GOLD-026 ~ GOLD-030 已有的只读结论（缺口诊断 / handoff / intake handoff
  契约 / 材料级人工核验 / 端到端链审计）汇总成**单一、确定、可操作**的"业务方现在还缺哪些
  **真实材料**、哪些只能人工做、哪些工程侧已就绪"清单（稳定 JSON + 人类可读 Markdown）；
- **复用而不复制**：缺口 / 阈值 / 材料清单 / 原因码完全复用既有能力（`build_readiness_report`
  + `build_handoff_report` + GOLD-027 `MATERIAL_SPECS` + GOLD-028 `attestable_material_keys` +
  GOLD-030 链审计事实），本工具**不新增、不降低**任何资格阈值，也**不重新解释**任何契约字段；
- **五个结论互相独立**（机器可读字段）：
  `engineering_ready`（工程链 / 契约是否一致，**只**说明工具链健康）/
  `submission_materials_complete`（业务方提交的真实材料**结构完整**，仍待人工核验）/
  `human_verification_complete`（当前 GOLD-028 材料级人工核验已完成）/
  `data_qualification_passed` / `phase_transition_allowed`；后两者**恒为 false**，
  `l3_gate_pending` / `blocker_active` / `human_gate_required` / `gate_blocked` **恒为 true**
  （**硬编码**）；
- **缺材料时只报事实**：输出**稳定 missing reason codes**（如
  `SUBMISSION_AUTHORIZATION_MISSING` / `SUBMISSION_AVAILABILITY_OOS_MISSING` /
  `SUBMISSION_SYNTHETIC_MATERIAL_ONLY` / `SUBMISSION_ATTESTATION_STALE` /
  `SUBMISSION_ENGINEERING_CHAIN_INCOMPLETE`）与**人工动作清单**（每条只引用既有契约字段 /
  既有阈值 / 既有命令），并逐材料列出 `IntakeStatus`；
  **绝不生成或推断** `published_at` / `collected_at` / `effective_at` / `available_at` /
  OOS 值（载荷里连这些键名都不出现，唯一时间戳是审计操作时间 `generated_at`）；
- **Mock / fixture / 模板永不计数**：只带示例 / 合成标记或名称命中示例词表的候选包一律
  `NON_QUALIFYING`；`--rehearsal` 只接受 fixture / Mock 输入（**禁止**与 `--inbox-dir` /
  `--attestation` / `--chain-audit` 同时给出），此时
  `submission_materials_complete` / `human_verification_complete` **恒为 false**；
- **工程链全绿不掩盖真实材料缺失**：`engineering_ready=true` 时
  `submission_materials_complete` / `human_verification_complete` /
  `data_qualification_passed` / `phase_transition_allowed` **不受影响**；
- **内容身份**：确定性 `facts_digest` 与 `pack_id`（策略块 + 证据来源 + 摘要）**不含**路径与
  审计时点：同一份提交材料换目录 / 换时刻计算得到**同一**身份，材料 / 结论 / 凭证 / 链路漂移
  必然产生**新**身份（旧结论绝不静默继承）；
- **用法**：

  ```bash
  # ① 只读汇总（默认只打印 stdout；零写入、零网络、零数据库写入）
  .venv\Scripts\python.exe -m scripts.evidence_submission_readiness --json

  # ② 纳入本地候选目录（GOLD-027 只读预检）+ 当前凭证核验（GOLD-028）
  .venv\Scripts\python.exe -m scripts.evidence_submission_readiness \
      --inbox-dir logs/evidence/inbox \
      --attestation logs/evidence/phase33_human_verification_attestation.json --json

  # ③ 纳入 GOLD-030 链审计结论（工程链是否一致）
  .venv\Scripts\python.exe -m scripts.evidence_submission_readiness \
      --chain-audit logs/evidence/phase33_evidence_chain_audit.json --json

  # ④ 单一 rehearsal：先验证工具链健康（纯本地 fixture；**不是**真实证据）
  .venv\Scripts\python.exe -m scripts.evidence_submission_readiness --rehearsal \
      --work-dir logs/submission_rehearsal --json

  # ⑤ 唯一写开关：显式 --out 原子落盘**本包本身**（不写库、不 intake、不解除 blocker）
  .venv\Scripts\python.exe -m scripts.evidence_submission_readiness --json \
      --out logs/evidence/phase33_human_evidence_submission_pack.json
  ```

- 退出码：`0` 无待办缺口（**仍不是**资格通过）/ `2` 参数或输入错误（含 inbox 目录缺失）/
  `3` 路径不可用（链审计报告 / 凭证 / 输出不可用、把输出写进 inbox、演练工作目录落在仓库非
  `logs/` 之处）/ `4` 输入 artifact 损坏 / 被篡改 / 漂移（fail-closed，零写入）/
  `5` 仍有待办缺口（**当前预期**：真实材料 / 人工核验 / 工程链至少缺一项）/ `6` 锁冲突；
- 回归测试：`tests/unit/test_evidence_submission_readiness.py`（27 项）+
  `tests/integration/test_evidence_submission_readiness_integration.py`（15 项），其中包含
  「空输入逐材料 MISSING + 稳定原因码」「Mock 候选包 `NON_QUALIFYING` 且**永不计**真实材料」
  「真实材料结构完整仍 `data_qualification_passed=false` / `l3_gate_pending=true`」
  「rehearsal 工程链全绿仍保持 blocker（真实材料缺失**不**被掩盖）」
  「凭证漂移 → `SUBMISSION_ATTESTATION_STALE`」「`pack_id` / `facts_digest` 确定性且漂移必变」
  「载荷**没有**任何证据时间键」「默认零写入 / `--out` 只写本包 + 锁文件、写进 inbox 被拒」
  「源码级守卫：模块与 CLI 无网络 / 文件时间 / 数据库写入调用」等。


### GPT Rolling Queue Autopilot 三任务前瞻契约（`orchestrator/planner_autopilot_contract.py`，GOLD-036）

> **合规红线**：本项**只读、纯函数** —— 不生成 / 追加 task、不补队列、不改
> `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json` / result、不决定 Phase、
> 不跨 L3/L4；仓库里**不存在**任何 planner API key，本地 Executor **绝不**调用 GPT
> 生成 task；`PHASE3_3_DATA` 保持 BLOCKED。

- **一句话**：把「GPT 唯一 Planner + 当前任务之外默认维持 3 个已批准 follow-on」
  从文字约定固化成**机器可测契约**（冻结常量 + 确定性审计 + 稳定 violation code），
  阻止「Executor 变 Planner」「follow-on target 降为 0」「绕过 hard gate」再次回归；
- **冻结常量**：`LOOKAHEAD_TARGET=3`、`FOLLOW_ON_TARGET_MINIMUM=1`、
  `PLANNING_AUTHORITY="gpt_only"`、`EXECUTOR_CAN_REFILL=False`、
  `BLOCKING_HUMAN_GATES={L3,L4}`、`PHASE_GATING_BLOCKER_CODES=("PHASE3_3_DATA",)`；
  GOLD-034 事实包（`orchestrator.planner_refill_request`）把同一份契约内嵌到
  `planner_authority.autopilot_contract`，并在 `summary` 显式给出
  `executor_can_refill=false` / `follow_on_target_minimum=1`；
- **端到端回归矩阵**（`tests/unit/test_planner_autopilot_contract.py` +
  `tests/integration/test_planner_autopilot_contract_regression.py`）：
  running+3 follow-ons ⇒ 无需补（`deficit=0`）；running+2 ⇒ `deficit=1`；
  running+0 ⇒ `deficit=3`；可运行任务必须位于 human-gated tail 之前
  （`RUNNABLE_TASK_AFTER_GATED_TAIL`）；完全 human-only 时允许停线
  （`hard_gate_tail_allowed=true`，计数事实仍保留）但**不得**制造 filler
  （`FILLER_TASK_FORBIDDEN`）；
- **blocker 下的工作范围**：`PHASE3_3_DATA` 存在时可被标记可执行的只允许
  `BLOCKER_FACING` / `CONTROL_PLANE` / `EVIDENCE_PREPARATION`
  （`INADMISSIBLE_WORK_CLASS_EXECUTABLE_UNDER_PHASE_BLOCKER`）；
  Phase 3.4 功能任务若被标成可执行，一律
  `PHASE34_FEATURE_TASK_EXECUTABLE` fail-closed；被 gate 挡住的功能任务
  （`auto_start=false` / L3、L4）属于合法 gated tail，不算违规；
- **职责边界（云端条件检查 vs 本地三任务缓冲）**：
  - **云端 GPT Planner**：唯一规划权，按平台调度做**条件检查**（周期不受仓库控制），
    每次写 task / state 前必须重新读取只读事实（
    `python -m orchestrator.planner_refill_request` 与
    `orchestrator.planner_autopilot_contract`）；
  - **本地 Orchestrator / Executor**：只负责「看得见缺口」——成功 commit + push 后与
    idle 时输出 `GPT_PLANNER_REFILL_REQUIRED` / `GPT_PLANNER_REFILL_SATISFIED`
    （节流 + 只读镜像 `.ai/runtime/planner_refill_request.json`），
    **绝不**补队列、**绝不**代 GPT 生成 task；
  - **三任务缓冲的角色**：用本地已批准任务覆盖云端检查间隔，而不是把规划权下放；
- **异常恢复**：看到低水位提示后，GPT 重新读事实再补队列；若期间远端 HEAD 已被
  Executor 推进，以新 HEAD 重新读取事实（绝不 force push / 覆盖刚完成的工作）；
  若已只剩 human-only hard gate，允许队列停在 gated tail 等待人工，**不得**为凑数量
  造任务；Executor 侧发现 `PROJECT_STATE` 指针 / 队列声明漂移时只报告、绝不修复；
- **用法**（只读）：

  ```bash
  # ① GOLD-034 事实包：队列水位 / 缺口 / 阻塞原因（纯 ASCII JSON；零写入）
  .venv\Scripts\python.exe -m orchestrator.planner_refill_request --generated-at 2026-09-23T00:00:00+08:00

  # ② GOLD-036 契约审计（对候选 plan 做确定性判定；纯函数、零写入）
  .venv\Scripts\python.exe -c "from orchestrator import planner_autopilot_contract as c, json; \
print(json.dumps(c.audit_follow_on_plan(tasks=[{'task_id':'GOLD-037','type':'CONTROL_PLANE_REVIEW_BACKLOG'}], \
blockers=['PHASE3_3_DATA']), ensure_ascii=True))"
  ```

### GPT Review Backlog 批量绑定事实清单（`orchestrator/review_backlog.py`，GOLD-037）

> **合规红线**：本项**只读、只产事实** —— 不签发 verdict、不写
> `.ai/GPT_REVIEW_LEDGER.json` / `.ai/PROJECT_STATE.json` / tasks / results，
> 不推进 `last_reviewed_task`、不决定 Phase、不跨 L3/L4；`PHASE3_3_DATA` 保持 BLOCKED，
> `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 不变。

- **一句话**：把 `PROJECT_STATE.last_reviewed_task` 之后**所有** `completed` result 汇总成
  一份确定性 backlog manifest，逐项**复用** `orchestrator.review_binding` 的客观事实
  （result SHA-256 / 完成 commit identity），让 GPT 能安全、批量地完成实质审查与加密绑定；
- **逐项字段**：`task_id` / `result.status` / `result.finished_at` / `result.sha256` /
  `commit.sha` / `commit.branch` / `facts_complete` / `missing_reason_codes` /
  `review_status`（**只有** `pending` / `bound` / `invalid` 三个客观取值，绝不是 PASS/FAIL）/
  `ledger`（条目是否存在、是否唯一合法、hash 与 commit 是否一致）；顶层另有 `coverage` /
  `ledger` / `chain` / `pointer` / `authority` / `issues` 与 `backlog_digest`
  （wall-clock 不参与）；
- **fail-closed（稳定 reason code）**：`LAST_REVIEWED_TASK_POINTER_MISSING`（边界不明 ⇒ 不猜）/
  `BACKLOG_ITEM_FACTS_INCOMPLETE` / `BACKLOG_ITEM_MANIFEST_UNUSABLE` /
  `BACKLOG_ITEM_LEDGER_INVALID` / `BACKLOG_ITEM_RESULT_HASH_DRIFT` /
  `BACKLOG_ITEM_RESULT_STATUS_DRIFT` / `BACKLOG_ITEM_RESULT_FINISHED_AT_DRIFT` /
  `BACKLOG_ITEM_COMMIT_SHA_DRIFT` / `BACKLOG_ITEM_COMMIT_BRANCH_DRIFT`，以及复用门禁的
  `LEDGER_*`（顺序回退 / 覆盖窗口缺口 / 重复条目 / 不可用）与 `REVIEW_POINTER_*`
  （指针 vs 台账 vs results）；任何 facts 不齐 / 漂移都**绝不猜测 commit 或 hash、
  绝不自动修复、绝不推进状态**；
- **退出码**：`0` 事实齐全且无漂移 / `2` fail-closed / `3` `PROJECT_STATE` 或台账不可用 /
  `4` `--output` 目标被拒（该守卫只允许 `.ai/runtime/**` 或系统临时目录）；
- **回归测试**：`tests/unit/test_ai_orchestrator_review_backlog.py`（18 项，含 GOLD-028~033
  式连续 backlog、部分已绑定、hash 漂移、commit 不可绑定、台账缺口 / 重复 / 不可用、
  指针缺失、CLI fail-closed、源码守卫「Executor 不能自签 review」）+
  `tests/integration/test_review_backlog_regression.py`（7 项：真实仓库 result SHA-256 /
  终态 commit 由 `git` 独立复算、前后零改写、CLI 幂等）；
- **用法**（只读）：

  ```bash
  # ① 一次性看清全部 formal review backlog 与每项可绑定事实（纯 ASCII JSON；零写入）
  .venv\Scripts\python.exe -m orchestrator.review_backlog --generated-at 2026-09-23T00:00:00+08:00

  # ② 需要人工诊断时才写受控镜像（只允许 .ai/runtime/** 或系统临时目录）
  .venv\Scripts\python.exe -m orchestrator.review_backlog --output .ai\runtime\review_backlog.json
  ```


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
- **TD-11 / TD-09 已解除（2026-09-22）**：TD-11 独立 Processor 层见 §7「采集后处理 Processor
  Pipeline」；TD-09 见 §7「30 分钟调度」。**TD-12 仍为部分解除**：`processed_items` /
  `audit_logs` / `data_versions` 均已有写入路径，**剩余**非行情数据集的 `data_versions`
  快照与 `processed_items` 缺 `error_message` 列（TD-19），见 `TECH_DEBT.md` TD-12；
- TD-05 / TD-07：`econ_calendar_collector` 与宏观预期值补全；TD-10：API 与 Dashboard
  （其只读前置观测层已由 GOLD-004 交付，见 §7「运行健康度与 Phase 3.3 数据资格观测」与
  `TECH_DEBT.md` TD-46 的剩余边界）。
- **TD-43 / TD-44 / TD-45 仍未解除**：证据接收入口已由 GOLD-005 交付并可用，
  就绪度 / 一键复核工具已由 GOLD-006 交付，单入口 operator workflow 与 gateway-only
  作者链已由 GOLD-007 交付，人工交接包已由 GOLD-008 交付，readiness 状态变更通知层已由
  GOLD-009 交付，readiness **周期 tick runner** 已由 GOLD-010 交付，**本地 inbox 发现与预检**
  已由 GOLD-011 交付，**人工复核决策与审计层**已由 GOLD-012 交付，**最终写入前 intake plan
  门禁**已由 GOLD-013 交付，**执行后的只读 Intake Receipt 与资格复核审计层**已由 GOLD-014 交付，**L3 人工决策包**已由 GOLD-015 交付，**L3 人工决策记录（防伪审计闭环）**已由 GOLD-016 交付，**本地 package / manifest builder（真实证据人工路径第一步）**已由 GOLD-017 交付（见 §7「授权证据接收入口」、
  §7「证据就绪度与一键资格复核」、§7「单入口 Evidence Operator 工作流」、
  §7「Evidence 人工交接包」、§7「Evidence Readiness 状态变更通知」、
  §7「Evidence Readiness 单次本地 tick runner」、§7「Evidence 本地 Inbox 发现与预检」、
  §7「Evidence Inbox 人工复核决策与审计」、§7「Evidence Intake Plan」、
  §7「Evidence 显式 Intake Receipt」、「Evidence Qualification 人工决策包」、
  §7「Evidence Qualification L3 人工决策记录」与
  §7「Evidence 本地 Package / Manifest Builder」），
  但**仓库内没有任何经该入口认证的真实授权证据**，
  因此 `PHASE3_3_DATA` 保持 `active=true`；下一步是业务方按 `evidence-intake-v1` 契约
  提交已授权数据（**第一步**：把真实授权文件放进 package 目录后用 `scripts/evidence_package.py`
  生成 `manifest.json`（GOLD-017），再交给 `scripts/evidence_inbox.py` 预检）+ 人工核验授权
  （详见 `TECH_DEBT.md` TD-47 / TD-48 / TD-49 / TD-50 /
  TD-51 / TD-52 / TD-53 / TD-54 / TD-55 / TD-56 / TD-57 / TD-58 / TD-59）。GOLD-007 ~ GOLD-017 的工具**只减少人工交接、盯盘、
  定时执行、候选摆放 / 预检、「候选包的 manifest 该写什么 / 摘要算对了吗」、
  「谁批了哪一版内容」与「落库前清单是否还是当前事实」、
  「落库后有没有可复核的收据」、「能不能提交人工 Gate」与「有没有可核验的人工决策记录」的审计摩擦**，
  不解除该 blocker；任何数量达标（含全部候选被 approve / 计划状态为
  `READY_FOR_EXPLICIT_INTAKE` / 收据为 `VERIFIED_EXECUTION_RECORDED` /
  决策包为 `READY_FOR_L3_HUMAN_GATE` / 决策记录为 `human_decision_recorded=true`）最多只到
  `ready_for_human_review=true` / 有 manifest / 有批准清单 / 有计划 / 有收据 / 有决策包 / 有人工决策记录，
  生成 manifest、人工批准、计划、收据、决策包与决策记录**都不等于** data qualification PASS，
  Phase 切换仍是 L3 人工 Gate。

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
