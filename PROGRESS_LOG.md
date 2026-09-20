# PROGRESS_LOG.md — 周末自动驾驶开发日志

> 规则（团队授权，周末有限自主开发）：
> 1. 第一轮只做 **Phase 2 第二步** 的两件事：`OpinionExtractor`（接口 + Regex Mock + 单元测试）、
>    `scripts/sample_annotation_set.py`（抽样脚本）；**不做** CRUD、**不写**新采集器。
>    **第二轮（周末狂飙权限）追加三项基建**：①作者库 CRUD 与导入框架（TD-16）；
>    ②Post → Opinion 管道骨架（纯 Mock）；③传播去重（1+99）。三项完成后**立即停止**。
> 2. 每个小任务完成后必须运行**完整测试套件**；收工时 pytest 必须 100% 通过，ruff / mypy 全绿。
> 3. 不新增未批准依赖、不改 `pyproject.toml`（除已批准依赖外）、**不改数据库 schema**（migration 0005 锁版）。
> 4. 遇阻（需改核心架构 / 同一问题失败 3 次 / 需人工外部联调）→ **停止编码**，把分析写进 `TECH_DEBT.md`。
> 5. 不代替人工标注：抽样脚本只导出待标注 CSV，**标注列一律留空**；
>    **严禁**用代码或大模型生成标注数据（科研诚信红线）。

---

## 日志

### 2026-09-12（周六）起步
- **完成**：权限与规则确认；读取上下文（`docs/08` Phase 2 验收、`docs/10` 规范、`tests/leakage` 现有样式、`.gitignore`、`src/common/exceptions.py`）。
- **发现（需在文档中对齐）**：`docs/08 §5` 要求 entry/SL/TP 明确数字样本准确率 **≥ 90%**，而 `docs/10` 草案写的是 85% —— 统一按更严的 `docs/08`（90%）。
- **团队默认批复（本次执行依据）**：`docs/10` 批准（阈值/边界规则/分层结构照准）；跨表时间因果**不新增 DB 列**，改由提取器 + Leakage 测试强制；TD-16 用「机构/分析师 RSS + 本地 CSV」解决（不碰微博）；依赖仅 A/B/C 类；标注由人工完成。
- **基线**：`pytest` 451 passed；`ruff` / `mypy` 全绿；`alembic head = 0005_phase2_author_lab_tables`。

### 2026-09-12 任务 1-A：观点抽取数据契约
- **新增**：`src/processors/schemas.py`（`AuthorOpinionDraft`，`extra="forbid"` + frozen；
  数值范围与 `docs/04 §10` 的 DB CHECK 一一对应；`effective_at` / 归属字段**刻意不在契约内**）、
  `tests/unit/test_opinion_schemas.py`（48 项）。
- **要点**：契约字段集 / 必填项 / `additionalProperties=false` 与 `docs/10 §5` 严格对齐（有测试断言）；
  `KNOWN_INSTRUMENTS` 与 `database/seeds/instruments.py` 白名单由测试锁定不漂移（含 `SGE_AU9999`、
  `US10Y_REAL` 等下划线代码，故标的正则为 `^[A-Z][A-Z0-9_/]{1,19}$`）。
- **测试状态**：`pytest` 499 passed；`ruff` 修掉 3 个风格问题后全绿；`mypy` 全绿（45 files）。

### 2026-09-12 安全提醒（待人工处理）
- 终端输入行出现一段疑似 API Key 的残留文本（`sk-` + 32 位十六进制）。**未复述、未写入任何文件**；
  若为真实密钥，请周一**立即轮换**（`DEEPSEEK_API_KEY` 等只放 `.env`，不得粘贴到命令行/仓库）。

### 2026-09-12 任务 1-B/C：抽取器接口 + Mock 实现 + 时间因果契约

- **新增**：
  - `src/processors/opinion_extractor.py`：`OpinionExtractor` 协议（runtime_checkable）、
    `OpinionExtractionResult`、`ExtractionDiagnostic`/`DiagnosticCode`、`build_drafts()` 共享校验；
  - `src/processors/regex_extractor.py`：`RegexOpinionExtractor`（`parser_version="mock-regex-v1"`），
    词典 + 正则实现，覆盖条件性 / 引用 / 复盘 / 否定 / 弱化 / 多标的多方向 / 中英文 / 周期词典 /
    非价格数字过滤 / 方向一致性筛选 / 图片无文字 / 空文本；**确定性输出**；
  - `src/processors/timeline.py`：团队裁决 2 的落地（`resolve_opinion_effective_at` +
    `assert_opinion_available_after_post`，不新增数据库列）；
  - `src/processors/__init__.py` 统一导出。
- **过程中发现并修正的 3 个真实设计缺陷**（均由新增测试锁定，非"测试写错"）：
  1. 价格数字与关键词间距规则：最初"绝对距离最近"会把 `stop 2400` 的前一个数字当止损；
  2. 改为"之后优先"后，又会把 `2360 止损` 的 2450 当止损（中文数字前置写法）；
  3. 最终规则：**扫描 ±16 字符、仅采纳与关键词间距 ≤2 字符的候选 + 方向一致性迭代筛选 +
     入场价避开已被止损/目标认领的数字**（宁可留空不可猜错）。
  另修正：`info_type` 需按**整句**主要驱动判定（否则 "CPI 超预期，黄金看多" 会被判成 TECHNICAL）；
  英文关键词改用 `\b` 词边界（避免 `at` 命中 `target`）。
- **新增测试**：`tests/unit/test_opinion_schemas.py`（48）、
  `tests/unit/test_regex_opinion_extractor.py`（53）、
  `tests/leakage/test_opinion_timeline.py`（8）。
- **测试状态**：`pytest` **560 passed**；`ruff` 全绿（修掉 4 个风格问题）；`mypy` 全绿（49 files）。

### 2026-09-12 任务 2：标注抽样脚本
- **新增**：`scripts/sample_annotation_set.py`
  （`python -m scripts.sample_annotation_set`）
  - 数据来源：数据库 `author_posts`（JOIN 作者/来源/原始哈希）或 `--input-csv`（作者库导入前的演练）；
  - **去重**：`content_hash`（缺失时用规范化文本），保留最早可用一条，空文本单独计数；
  - **分层**：文本长度三档（≤50 / 51–200 / >200）各约 1/3、单一来源占比 ≤40%、
    含媒体样本 ≥20 条（配额）、按交易日轮转展开以提升时间覆盖；
  - **可复现**：`--seed`（默认 20260912）写入元数据；同种子结果完全一致（有测试锁定）；
  - **导出**：待标注 CSV（列＝`docs/10` 附录 A + `author_name`/`spec_version`），
    **判定列一律留空**；`<out>.meta.json` 记录种子、去重统计、分层达成情况与注意事项；
  - 退出码：0 成功 / 2 无候选 / 3 `--require-full` 且样本不足。
- **新增测试**：`tests/unit/test_annotation_sampling.py`（33）+ `tests/integration/test_annotation_sampling_db.py`（3）。
- 过程中修复的缺陷：`sa.text()` 在 SQLite 返回**字符串**（PG 才转 datetime）→ 改为 ORM 类型化
  select + `_as_utc()` 容错（集成测试锁定）；`CSV_COLUMNS - frozenset` 类型错误。
- **冒烟验证**（真实 CLI 调用，非测试）：60 条候选 → 抽样 20 条，长度分档 7/7/6、
  来源 4/4/4/4/4（上限 8）、含媒体 5 条、覆盖 5 个交易日、零告警，标注列全空。
- **测试状态**：`pytest` **596 passed**（单元 351 / 集成 203 / 数据质量 25 / 泄漏 17）；
  `ruff` 全绿；`mypy` 全绿（50 files）；`alembic head` 仍为 `0005_phase2_author_lab_tables`（**未动 schema**）。

---

## 收工状态（周一起点）

### 本次新增/修改文件
- 新增：`src/processors/{__init__,schemas,opinion_extractor,regex_extractor,timeline}.py`、
  `scripts/sample_annotation_set.py`、`tests/unit/test_opinion_schemas.py`、
  `tests/unit/test_regex_opinion_extractor.py`、`tests/unit/test_annotation_sampling.py`、
  `tests/integration/test_annotation_sampling_db.py`、`tests/leakage/test_opinion_timeline.py`、
  `PROGRESS_LOG.md`
- 修改（仅文档，无 schema / 无依赖变更）：`docs/10_标注规范.md`（升为已批准 v1.0）、
  `docs/04 §44`（裁决 6 落定）、`docs/05`（Phase 2 进度）、`README.md`、`TECH_DEBT.md`

### 门禁结果
| 检查 | 结果 |
|---|---|
| `pytest -q` | **596 passed** |
| `ruff check .` | All checks passed |
| `mypy` | Success（50 source files） |
| `alembic heads` | `0005_phase2_author_lab_tables`（单一 head，未改动） |
| 数据库 schema | **未变更**（migration 0005 锁版） |
| 依赖 | **未新增**（`pyproject.toml` 的 Phase 2 依赖仍为已批准集合，本机未安装成功，见下） |

### 周一需要你做的事（按优先级）
1. **轮换疑似泄漏的密钥**（重要）：终端缓冲里出现过一段 `sk-` + 32 位十六进制的文本；
   若那是真实 Key，请立即轮换，并确保只放在本地 `.env`（`.gitignore` 已覆盖）。
2. **安装已批准依赖**（本机网络受限，我未能装成）：
   `.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"`；
   验证：`python -c "import numpy, pandas, sklearn, httpx, jieba"`。
3. **代码审查**（建议重点）：
   - `src/processors/regex_extractor.py` 的**价格归属规则**（间距 ≤2 + 方向一致性 + 入场价让路）
     与 `info_type` 按整句判定；
   - `docs/10` §4.3 弱化措辞的两档映射（`可能/不排除` ≤0.4、`倾向/大概率` ≤0.5）；
   - 抽样脚本的 `--require-full` 语义与 40% 来源上限（样本池集中时会显式告警并放宽）。
4. **人工标注 200 条**（团队裁决：必须你本人完成，我不代填）：
   - 无作者库时先用 CSV 演练：`python -m scripts.sample_annotation_set --input-csv logs/posts.csv --limit 200 --require-full`
   - 正式路径（作者库导入后）：`python -m scripts.sample_annotation_set --limit 200 --seed 20260912`
   - 标注规范：`docs/10`（已批准 v1.0），按附录 A 填写，判定列不要留空（无法判定写
     `no_opinion=true` 或 `stance=UNKNOWN`）。
5. **下一步开发指令**（等你批准后再动）：作者库 CRUD/导入（TD-16）→「Post → Opinion」管道
   （写 `processed_items` + `author_opinions`）→ 标注比对脚本与准确率报告 → 传播去重。

### 遇阻记录（按规则停下的点）
- **依赖安装**：PyPI 下载 5 分钟无进展（本环境网络受限），已终止进程并如实记录，
  **未修改** `pyproject.toml` 的依赖集合（仍为团队批准的 A/B/C 类）。
- 其余无阻塞：两项任务均按其定义完成，所有门禁全绿。

---

## 周末第二轮（狂飙权限）：Phase 2 基建

### 2026-09-12 任务一：作者库 CRUD 与导入框架（TD-16）
- **新增**：
  - `database/repositories/audit.py`：`record_audit()` + `snapshot_model()`（JSON 可序列化快照，
    与业务修改**同事务**；审计口径：管理性修改必留痕，采集性心跳不写审计，避免淹没审计表）；
  - `database/repositories/authors.py`：`AuthorRepository` / `AuthorAccountRepository`
    （查询 / create / ensure 幂等 / update_status / update_profile / soft_delete /
    账号 upsert（默认不覆盖运维配置）/ set_enabled / touch_last_collected_at）；
  - `database/seeds/authors.py`：`AuthorSeed` + `AUTHOR_SEEDS`（3 条，**全部挂 NEWS 来源，不碰微博**）
    + CSV 解析与导入（`load_author_seeds_from_csv` / `import_authors_from_csv`，含 dry-run）
    + `AuthorImportReport`（created/existing/accounts/problems）；
  - `database/seeds/runner.py`：新增 `seed_authors()`、`seed_database(scope="authors",
    authors_csv=...)`，`seed_all()` 纳入作者；`--scope` 与 `_REQUIRED_TABLES` 同步扩展；
  - `database/seeds/__main__.py`：`--scope authors`、`--authors-csv`、参数校验与截断输出；
  - `database/repositories/__init__.py` 导出新仓储。
- **设计取舍（均为显式记录）**：
  1. 作者 + 平台账号是**一个导入单元**：来源缺失时整行跳过（不留"没有账号的半个作者"）；
  2. 已存在记录**不覆盖**（`ensure` / `upsert(update_existing=False)`），与种子口径一致；
  3. CSV 存在问题行时：`import_authors_from_csv` 上报并继续处理其它行，
     而 `seed_database(authors_csv=...)` 选择**整批回滚 + 报错**（不留半成品）。
- **新增测试**：`tests/unit/test_author_seeds.py`（20）、
  `tests/integration/test_author_repository.py`（22）、
  `tests/integration/test_author_import.py`（16）。
- **测试状态**：`pytest` **654 passed**；`ruff` / `mypy`（53 files）全绿；
  **未新增迁移、未改 schema、未加依赖**。

### 2026-09-12 任务二：Post → Opinion 管道骨架（纯 Mock）
- **新增**：`src/processors/opinion_pipeline.py`
  - `OpinionPipeline.run(limit=...)`：`author_posts` →（抽取器）→ `processed_items`
    → `author_opinions`；`PipelineReport`（扫描 / 处理 / 无观点 / 空文本 / 失败 / 观点数 / 未知标的 / 诊断计数）；
  - 幂等：`processed_items(raw_item_id, processor_name, processor_version)` 唯一键 + 处理前存在性检查；
  - 失败隔离：单帖异常 → 该帖 `FAILED`（异常写入 `structured_json`），其余照常处理；
  - 空文本 → `SKIPPED` + `status_reason`；无观点 → `SUCCESS` + `no_opinion=true`（已处理语义）；
  - 未知标的 → 跳过该条观点 + 计数（不自动造标的）；时间因果：`opinion.effective_at == post.effective_at`。
- **修 timing 缺陷（真实守卫命中）**：SQLite 读回 `effective_at` 为 naive，
  `timeline` 严格守卫抛 `TimeSemanticsError` → 新增
  `timeline.ensure_utc_from_database()`（**仅**用于数据库读回值；外部输入仍必须显式声明时区）。
- **修 schema 约束缺陷**：`processed_items` **无** `error_message` 列（schema 已冻结）
  → 失败详情改写入 `structured_json`；已记入 `docs/04 §44.2` 决策 11。
- **新增测试**：`tests/unit/test_opinion_pipeline_helpers.py`（12）、
  `tests/integration/test_opinion_pipeline.py`（11）、
  `tests/leakage/test_opinion_timeline.py`（+3，共 11）。
- **测试状态**：`pytest` **680 passed**；`ruff` / `mypy`（54 files）全绿。

### 2026-09-12 任务三：传播去重（1+99 构造）
- **新增**：`src/processors/similarity.py`（零依赖 TF-IDF 余弦相似度：
  CJK 字符 n-gram + ASCII 词，归一化复用 `normalize_for_hash`；jieba 为**显式可选**开关）、
  `src/processors/propagation.py`（`PropagationDetector`：分桶 → 相似度 → 传播边 → 连通分量聚类；
  `PropagationResult.independent_opinion_count`；`store_propagation_edges` 批量查重落库）。
- **阈值取值（实测，写入文档）**：同文 = 1.000、"转发：… @账号"噪声 ≈ 0.82、
  同句式反向（做多/做空）≈ 0.60 → 默认阈值 **0.80**（两侧留余量）。
- **1+99 结论**：`items=100 edges=4950 clusters=1 duplicate_ratio=0.99` ——
  **100 条内容相同的帖子只算 1 个独立观点**（不重复计数）。
- **新增测试**：`tests/unit/test_text_similarity.py`（24）、
  `tests/unit/test_propagation_dedup.py`（25）、
  `tests/integration/test_propagation_edges.py`（7）。
- **测试状态**：`pytest` **736 passed**；`ruff` / `mypy`（56 files）全绿。

### 2026-09-12 收工验证（非测试的真实调用）
- 临时 SQLite + `alembic upgrade head` + 真实 CLI：
  `python -m database.seeds --scope all` → instruments 11 / sources 9 / **authors 3（+3 平台账号）**；
  再跑 `--scope authors` → 新增 0、已存在 3（幂等）；
  `--scope authors --authors-csv <CSV>` → 新增 2（`inst-a` / `analyst-b`）。
- 真实库上跑管道 + 传播：`pipeline 1st: scanned=1 processed=1 opinions=1`、
  `2nd: scanned=0`（幂等）；观点 = `LONG / 1h / conf 0.9 / 2380 / 2365 / 2450 / mock-regex-v1`；
  `propagation: items=100 edges=4950 clusters=1 duplicate_ratio=0.99`，
  边落库 4950 条、重跑 0 条、`raw_items` 仍为 100（原始层零改动）。
- `alembic heads` = `0005_phase2_author_lab_tables`（**未产生新迁移**）；
  `pyproject.toml` 依赖集合未变（仍为团队批准清单）。
- **按指令停止**：不再开始任何依赖真实标注或真实 API 的工作。

### 2026-09-13（周日）阻塞修复：抽样输入数据源（用户报障）

- **用户报障**：`python scripts/sample_annotation_set.py --input-csv logs/posts.csv --limit 200 --require-full`
  → `FileNotFoundError: logs\posts.csv`（周末只在单元测试里用内存构造，从未落地该文件）。
- **修复（三步）**：
  1. **新增 `scripts/generate_mock_posts.py`**：确定性生成 250 条结构化合成博文 →
     `logs/posts.csv`（24 列，含 `id`/`source`/`content` 三个别名列 + 规范列）；
     默认拒绝覆盖已存在文件（`--force` 才覆盖，保护将来真实导出数据）；
  2. **`sample_annotation_set.py` 支持"输入缺失自动补数据"**：
     `--input-csv` 不存在且未加 `--no-mock-fill` → 自动生成（`--mock-fill`，默认 250）并
     在 stderr 打醒目警告；新增 `--mock-fill` / `--mock-seed` / `--no-mock-fill`；
  3. **输入来源体检** `detect_mock_provenance()`：扫描 `is_mock` / `mock_batch` 列，
     把 `input_is_mock` / `input_mock_rows` / `input_mock_batch` 写进抽样元数据，
     已有合成标记的输入也会照样警告（防止合成数据静默进入标注与验收）。
- **Mock 数据设计（对齐 `docs/10 §2.2`）**：4 个已注册 NEWS 来源（**不含微博**）× 10+ 交易日；
  长度三档 100/73/77；含媒体 46 条；**对抗样本 8 类 × 15~17 = 126 条**（条件性 / 引用他人 /
  事后复盘 / 双重否定 / 点位方向不一致 / 纯信息播报 / 反问反讽 / 图表无文字）；
  5 条同文转载；`effective_at = max(published_at, collected_at)`，含"缺发布时间"(3) 与
  "采集早于发布"(2) 两类时间边界；每条文本都带**方向词 + 目标 + 止损**（无方向类刻意不带）。
- **真实缺陷（跑真实进程才发现，已全部修复 + 回归测试）**：
  1. **中文 Windows 控制台乱码/崩溃**：脚本无条件把 stdout 切成 UTF-8，而 GBK 控制台会把中文
     打成乱码；且打印 `⚠` 在 GBK 下直接 `UnicodeEncodeError` → 改为
     `configure_stdout()`（只在当前编码无法表示中文时切换）+ 去掉非 GBK 字符；
  2. **脚本模式导入失败**：`python scripts/x.py` 时仓库根不在 `sys.path`，
     `from scripts import generate_mock_posts` 抛 `ModuleNotFoundError`
     → 加 `sys.path` 引导 + `scripts/__init__.py`（也让 mypy 不再报 "Source file found twice"）；
  3. **Excel 乱码**：标注 CSV 原为无 BOM 的 UTF-8，Excel 双击中文乱码 → 改 `utf-8-sig`（带 BOM）。
- **新增测试 52 项**（含真实进程冒烟 `tests/integration/test_cli_scripts.py`）：
  `tests/unit/test_mock_posts_generator.py`（42）、`tests/integration/test_cli_scripts.py`（7）、
  抽样脚本新增 9 项（别名列 / 大小写 / 自动补数据 / 来源体检 / 不覆盖既有输入）。
  其中关键锁定：**抽样 200 条后每个对抗类别仍 ≥10 条**（`docs/10 §2.2` 硬指标）。
- **真实执行证据（非测试）**：
  `python scripts/generate_mock_posts.py --force` → 250 行；`logs/posts.csv` 24 列 / 205 KB；
  `python scripts/sample_annotation_set.py --input-csv logs/posts.csv --limit 200 --require-full`
  → **退出码 0**，写出 `logs/annotation_sample.csv`（200 行，判定列 16 列全空）+
  `logs/annotation_sample.meta.json`（`input_is_mock=true`、`notes=[]`、分档 67/67/66、来源上限 80 内）；
  抽样后对抗样本 105/200（每类 10~15 条）。
- **门禁**：`pytest` **796 passed / 1 skipped**、`ruff` 全绿、`mypy`（58 files）全绿、
  `alembic heads` 仍为 `0005_phase2_author_lab_tables`（**未新增迁移、未改 schema、未加依赖**）。
- **周一待办（更新）**：①轮换疑似泄漏的 API Key；②人工标注 `logs/annotation_sample.csv`
  （**周五/周一用 Excel 直接打开即可，编码已修**）；③回填后跑标注比对脚本（尚未实现）。

### 2026-09-13（周日）多模型标注对比：把"模型输出"和"金标准"分开

- **背景**：用户上传 `logs/annotation_sample_airesult.xlsx`——豆包（`_db`）/ 千问（`_qw`）/
  文心一言（`_bd`）三个大模型对同一批 200 条样本的标注。**明确指示：不得用模型评测模型**，
  必须先由人工裁定出金标准。
- **新增 `scripts/compare_model_annotations.py`（只读文件，不碰数据库）**：
  - **零新增依赖**读 xlsx：`openpyxl` 未安装而项目依赖须经批准，
    故用 `zipfile` + `xml.etree` 直接解析（共享字符串 / 内联字符串 / 数字 / 布尔 /
    **稀疏单元格** / 空行），所有行为由自造 xlsx 的单元测试覆盖；
  - 列名匹配**大小写 / 下划线 / 连字符不敏感**——真实文件里就是
    `stop_loss_DB` / `stop_loss_QW` / `stop_loss_bd` 混用；
  - 对比 5 个字段（`stance` / `horizon` / `stop_loss` / `take_profit` / `information_type`）：
    完全一致比例（归一化 / 严格 / 原始字符串三口径）、字段分歧率、2:1 与三方各异、
    三方都留空、两两一致率、各模型留空率；
  - **归一化只用于比较**（`做多`＝`LONG`、`15 分钟`＝`15M`、`2,450`＝`2450`），
    展示与落盘一律保留模型原文，避免"同义不同写"被误判成分歧；
  - 导出 `logs/pending_review.csv`（一行 = 一个"样本 × 分歧字段"，含原文与三方判定，
    **`final_gold_standard` 及人工列全部留空**）与 `logs/model_consensus.csv`
    （774 行三模型共识，带 `provenance`，供裁决后合并成 `ground_truth.csv`）；
  - 生成报告 `docs/experiments/annotation_model_comparison.md`（含红线提示、统计口径、
    归一化规则、人工裁决流程、输入文件 sha256 与复现命令）。
- **实测结果（200 条）**：
  - **三模型完全一致：60 / 200 = 30.0%**（归一化口径；严格口径 18/200 = 9.0%，
    原始字符串口径同为 30.0%）；
  - 字段分歧率：`stance` 7.0%、`horizon` 2.0%、`stop_loss` 14.5%、`take_profit` 22.0%、
    **`information_type` 67.5%**（103 条 2:1 + 32 条三方各异）→ 最大冲突点；
  - 覆盖度：`horizon` 三家留空率 ~70%、`stop_loss`/`take_profit` ~46%、
    `information_type` 文心留空 12%、方向 0%；
  - **待人工裁决：140 个样本、226 条记录**；
  - 与 `logs/annotation_sample.csv` 对齐检查通过（200/200 条 `post_id` 一致）。
- **顺手修的工程问题**：把控制台编码工具抽成共享模块 `scripts/_console.py`
  （3 个脚本复用，`generate_mock_posts` 改为再导出，行为不变）。
- **新增测试 67 项**（`tests/unit/test_model_annotation_comparison.py`）：
  读取器（自造 xlsx）、列名匹配、归一化、四类分歧形态、统计口径、导出契约
  （`final_gold_standard` 必须为空）、CLI 端到端与退出码、以及
  **"脚本不得出现 `sqlalchemy` / `database` / 未批准依赖"** 的源码级红线断言。
- **门禁**：`pytest` **863 passed / 1 skipped**、`ruff` 全绿、`mypy`（60 files）全绿；
  未新增迁移、未改 schema、未新增依赖。
- **新登记**：`TECH_DEBT.md` **TD-22（P0，安全）**——仓库根目录 `新建 文本文档.txt`
  含 `DEEPSEEK_API_KEY` 明文，须轮换 + 删除（当前无 `.git`，尚未进版本历史）。
- **下一步（等人工）**：填完 `logs/pending_review.csv` 的 `final_gold_standard` →
  与 `logs/model_consensus.csv` 合并成 `ground_truth.csv`（保留 provenance）→
  **那时**才写脚本评估 `RegexOpinionExtractor`（本次**未**做，也不该做）。

### 2026-09-13（周日）金标准合并（人工裁决已完成）+ 两处真实环境缺陷

- **背景**：用户已完成 226 条人工裁决（`logs/pending_review.csv`；Excel 另存后实际是
  **xlsx 容器**，扩展名仍叫 `.csv`），其中 1 条（`R0113` / `mock-post-0126` / `horizon`）
  有意留空并在 `notes` 写明"文本没有提到，留空"。
- **新增 `scripts/build_ground_truth.py`**（只读、不碰数据库）：
  - 合并「人工裁决」+「三模型共识」→ `logs/ground_truth_200.csv`
    （长表 11 列：`post_id/field/value/value_raw/source/provenance/models_agreement/overturn/reviewer/note`）；
  - **红线**：人工漏填**且**无理由的格子**绝不回填模型值** → 不产出条目、单独列出、退出码 3；
  - `final_gold_standard` 空但 `notes` 有理由 → 视为**人工裁决"未给出"**（合法金标准，`value=""`）；
  - 人工裁决文件先**字节级归档**到 `logs/archive/`（`..._adjudicated_<时间戳>.xlsx`），人工成果只此一份；
  - 按**内容**识别输入（xlsx / CSV）并自动识别编码（UTF-8 / BOM / GBK / Big5）；
  - 生成《金标准生成报告》`docs/experiments/ground_truth_report.md`（覆盖率、推翻分析、分布对比、下游用法）。
- **实测结果（200 样本 × 5 字段 = 1000 格）**：
  - **人工推翻模型 42 次 / 226 条人工裁决 = 18.6%**
    （采纳少数派 **36**、三家都错 **6**；未推翻 184：采纳多数派 152、三家各异采纳其一 32）；
  - 按字段推翻率：周期 **75%**（3/4）、止损 37.9%（11/29）、目标位 29.5%（13/44）、
    信息类型 11.1%（15/135）、方向 0%（0/14，14 条人工全判 `UNKNOWN`）；
  - **金标准覆盖率 1000 / 1000 = 100.0%**：人工 226 格（22.6%）+ 模型共识 774 格（77.4%），
    其中"三家都未给出"295 格；**有具体取值 704 格（70.4%）**；
  - 网格完整性：与 `logs/annotation_sample.csv` 完全对齐（无缺失格子）。
- **两处真实缺陷（都已修 + 回归测试）**：
  1. **xlsx 关系路径解析 bug**：`_sheet_targets` 只认相对路径，遇到 openpyxl 写出的
     **绝对路径**（`/xl/worksheets/sheet1.xml`）会拼成 `xl/xl/...` → `KeyError`
     → 已用 `posixpath.normpath` 归一化；新测试改用 **openpyxl 生成夹具**（正好覆盖该路径）；
  2. **`.env` 配置缺陷（阻塞 248 项测试）**：`DATABASE_URL=sqlite:///…` **缺少 SQLAlchemy 驱动前缀**
     → `config/settings.py` 校验直接抛 `ValidationError`（此前文件名不是 `.env`、根本没被加载，
     改名后才暴露）→ 已改为 `sqlite+pysqlite:///./database/gold_ai.db`（**API Key 行未改动**）。
- **`compare_model_annotations.py` 增强**：
  - **防覆盖保护**：`pending_review.*` 已有人工填写时默认**拒绝覆盖**（退出码 4，`--force` 才允许）；
  - 读表按**内容**识别（xlsx 伪装成 `.csv` 也能读）+ 文本编码自动识别（UTF-8/BOM/GBK/Big5）。
- **新增测试 101 项**：`tests/unit/test_ground_truth_build.py`（31）、
  `tests/unit/test_model_annotation_comparison.py`（67，含 IO/防覆盖回归）、CLI 冒烟 +3。
- **门禁**：`pytest` **897 passed / 1 skipped**（898 项）、`ruff` 全绿、`mypy`（61 files）全绿；
  未新增迁移、未改 schema；依赖仅新增已批准的 `openpyxl`（dev extra，脚本运行时不依赖）。
- **暂停（按指令）**：等用户确认后，再写"用 `ground_truth_200.csv` 评估 `RegexOpinionExtractor`"的脚本。

### 2026-09-13（周日）Phase 2 基线评估（正则抽取器 vs 人工金标准）

- **新增 `scripts/evaluate_extractor.py`**（只读、不碰数据库）：逐格比对 `logs/ground_truth_200.csv`
  与 `RegexOpinionExtractor` 的输出，**严格区分三类失败**：
  `该判未判（missed）` / `提取错误（wrong_value）` / `不该判却判（spurious）`，
  另有 `命中` 与 `双方都未给出` 两类的计数；抽取器通过 `OpinionExtractor` 协议**可注入**
  （单测用假抽取器覆盖统计逻辑）。
  - 产出：`logs/extractor_eval.csv`（1000 行逐格明细，可人工复核每格）+
    《Phase 2 基线评估报告》`docs/experiments/Phase2_基线评估报告.md`（含两类混淆矩阵、
    点位差值分布、典型案例、质量指标、PASS/FAIL 结论）。
  - 口径：**人工子集（226 格）= 验收依据**；模型共识子集（774 格）仅参考（`.clinerules`）。
- **实测结果（`parser_version=mock-regex-v1`，200 条帖子）**：
  - 抽取器行为：无观点 27 条（13.5%）、给出观点 173 条、多观点 38 条、
    UNKNOWN 方向率 33.2%、**confidence 非法 0**（`docs/08` 该项 PASS）；
  - **人工子集准确率**：方向 **100.0%**（14/14，PASS）、周期 0.0%（3 格）、
    止损 0.0%（29 格全漏）、目标位 25.0%（44 格）、信息类型 40.7%（135 格）；
  - **全量 1000 格**：命中 312、双方都判未给出 292、**该判未判 289**、提取错误 103、
    不该判却判 **4**；核心四字段 **漏判占失败 77.1%**；
  - 点位字段"**精确率 100% / 90.5%，召回率只 33.1% / 31.4%**"——
    即：认出来的点位基本都对，但**大量该判的没判**；
  - 根因：人工子集恰好是"三模型分歧行"（= 条件句/引用/复盘/双重否定/点位不一致等对抗样本），
    而抽取器按 `docs/10 §4.1/§4.4` 设计对这些文本**一律降级 UNKNOWN 并丢弃方向不一致价格**，
    两者口径尚未对齐；另一方面 `entry_low/entry_high` **不在金标准里**（只能报覆盖率）。
- **发现并修复的工程缺陷**：
  1. 单元测试用**默认输出路径**跑 `main()`，把真实产物 `logs/extractor_eval.csv` 覆盖成 1 行
     → 所有 CLI 测试改为显式传 `--eval-out/--report` 到 `tmp_path`（已加注释说明原因）；
  2. 报告"典型案例"里出现未插值的 `{cell.pred_value…}`（换行脚本拆行时丢了 `f` 前缀）→ 已修。
- **新增测试 30 项**（`tests/unit/test_extractor_evaluation.py`：五类判定、指标口径、分层、
  混淆矩阵、质量指标、CLI 端到端 + 不碰数据库红线）。
- **门禁**：`pytest` **927 passed / 1 skipped**（928 项）、`ruff` 全绿、`mypy`（62 files）全绿。
- **登记技术债 TD-23（P1）**：点位/信息类型字段**召回率过低**（该判未判占失败 73%）。
- **暂停（按指令）**：等用户确认后再动规则（`regex_extractor`）或补标注口径。

---

## 第四轮（2026-09-13）：LLM 观点抽取器基建（Mock 先行，先不跑真实 API）

> 用户确认 5 项设计（中文指令 + 英文键名 / `deepseek-chat` / 先 20 条试点 / 缓存 `auto` + 测试 `readonly`
> / `--max-api-calls 250`），并追加 4 项强制要求：①超时重试 ≤3 + 限速 60 次每分 + 429/5xx 处理；
> ②**测试先行**（Mock 覆盖正常/超时/429，单测 100% 通过前不许碰真实 API）；
> ③Mock 通过后才跑真实 20 条并落 `logs/extractor_eval_llm.csv`；④出《Phase 2 基线对比报告》；
> ⑤设计"避免 `--api-key` 泄漏到标准输出"。

- **新增源码（4 个模块，未改任何 schema / 未加依赖）**：
  - `src/processors/llm_client.py`：同步 DeepSeek 客户端。
    重试策略：**只重试可重试错误**（超时 / 429 / 5xx），401/403 与其它 4xx 立即失败；
    429 优先遵守 `Retry-After`（上限 8s），5xx/超时走确定性指数退避（0.5→1→2→8s 封顶）；
    速率限制为**滑动窗口 60 次/分钟**（`clock`/`sleep` 可注入 → 单测零等待）；
    `max_api_calls` 预算门禁（**重试也计入**，避免限流时预算失控）；
    安全：`SecretStr` 入参、`repr()` 掩码、不 print、异常消息与返回内容统一 `redact()`。
  - `src/processors/prompt_opinion.py`：`opinion-prompt-v1`。
    11 条规则**逐条对齐 `docs/10 §4.9`**（条件句/引用/复盘/弱化/多目标取第一目标/
    `UNKNOWN ≠ 无观点`/禁止默认标的与周期…），附 6 条 few-shot 与严格 JSON Schema；
    `build_user_message` 用 JSON 包装正文（抗提示注入）；超长文本截断并记 warning。
  - `src/processors/llm_cache.py`：键 `sha256(prompt_version|model|text|has_media)` →
    **改 Prompt 自动失效**；模式 `auto/readonly/refresh/off`；``logs/llm_cache/<前2位>/<key>.json``
    二级分片；写 `.tmp` + `os.replace` 原子替换；文件损坏视为未命中；**失败也缓存**。
  - `src/processors/llm_extractor.py`：`LLMOpinionExtractor`（实现 `OpinionExtractor` 协议）。
    流程：空文本短路 → 缓存命中直接复用（含"缓存里的失败"）→ `readonly` 未命中**显式报错**
    （绝不偷偷联网）→ 调 API → 原始 JSON 落缓存 → **复用 `build_drafts()`** 做契约校验。
    容错：markdown 代码块 / 顶层数组 / 非对象元素 / 未知顶层键 → 警告但继续；
    模型幻觉字段（`parser_version` / `effective_at` / `author_id`…）**真剔除 + 记诊断**，
    保证"一条越界键不会毁掉整条观点"；单帖 API 失败/解析失败 → 降级诊断，批量评估不断流。
- **新增诊断码**（`DiagnosticCode`）：`llm_api_error`、`llm_parse_error`（仅诊断统计，非 DB 枚举）。
- **新增配置**：`config/settings.py` 的 `deepseek_api_key`（`SecretStr`，`repr()` 为 `**********`）、
  `deepseek_base_url`、`deepseek_model`、`deepseek_configured`；`.env.example` 同步补齐安全约定。
- **新增测试 85 项（全部 Mock，零网络零 token）**：
  - `tests/unit/test_llm_client.py`（34）：请求体契约、用量/`request_id`、密钥只在 `Authorization`、
    `repr`/`stats`/异常消息/返回内容**均不含真实 Key**、超时重试与耗尽、429（`Retry-After` +
    无头退避 + 上限截断 + 耗尽）、5xx 重试与耗尽、401/403/422 不重试、200 但结构异常、
    限速窗口与预算门禁、**守卫有效性回归**；
  - `tests/unit/test_llm_cache.py`（23）：键确定性/敏感性（正文、媒体、模型、**prompt 版本**）、
    四种模式、原子写入无残留、损坏文件降级、失败条目可读回、缓存文件不含 `sk-`；
  - `tests/unit/test_llm_extractor.py`（28）：正常抽取、`parser_version` 由系统注入、
    幻觉字段剔除但保住观点、容错解析四类、解析失败诊断、5xx 降级 + **失败缓存后不再联网**、
    401/缺 Key **必须抛出**、二次调用零请求、`has_media`/prompt 版本换键、`refresh` 覆盖、
    `readonly` 无 Key 复跑可行且未命中报错、协议一致性、统计可安全打印。
- **发现并修复的真实安全缺陷**：`tests/conftest.py` 的"禁止外部网络"守卫只在 **socket 层**拦
  非本机地址。实测本机有代理（`127.0.0.1:7892`）时，httpx 会把目的地址写成 `127.0.0.1` →
  **第一层放行 → 真实请求真的打到了 `api.deepseek.com`（返回 401，未消耗 token）**。
  修复：新增**第二层 httpcore 后端拦截**（`SyncBackend` / `AnyIOBackend` 的
  `connect_tcp`/`connect_unix_socket`），并加回归测试锁定；`httpx.MockTransport` 不经过后端，
  全部 Mock 测试不受影响。
- **门禁**：`pytest` **1012 passed / 1 skipped**（1013 项）、`ruff` 全绿、`mypy`（66 files）全绿；
  **未新增迁移、未改 schema、未新增依赖**。
- **成本口径修正（诚实报告）**：`opinion-prompt-v1` 的 system prompt 实测 **3380 字符
  ≈ 2.2k tokens**（比初稿估算的 900~1100 tokens 高一倍）→ 全量 200 条输入约 **47 万 tokens**，
  费用估算由 `¥0.1~1.2` 上修到 **`≈¥0.6 ~ ¥1.3`**；`readonly` 复跑仍为 **¥0**。
- **未做（等用户确认后再执行）**：把 `--extractor llm --limit 20` 接进 `scripts/evaluate_extractor.py`；
  真实 API 试点 20 条；全量 200 条；`scripts/compare_extractor_baselines.py` 与对比报告。

---

## 第五轮（2026-09-13）：评估脚本接线 + LLM 真实试点 20 条

> 用户指令：把 `--extractor llm --limit 20 --llm-cache-mode auto --llm-max-api-calls 250`
> 接进评估脚本；结果写 `logs/extractor_eval_llm.csv`、原始 JSON 写 `logs/llm_cache/`；
> **允许真实调用 DeepSeek**；**跑完 20 条后暂停**（不要立刻跑全量 200），先交 Token/成本与人工对比样本。

### 1. 真实 API 探针（先探清接口，再写代码）

新增临时探针 `logs/_probe_deepseek.py`（**共耗 ~65 tokens**），实测确认 4 条事实：

| 事实 | 实测结果 | 代码动作 |
|---|---|---|
| 可用模型 | `GET /models` 只返回 `deepseek-flash` / `deepseek-v4-pro`；旧名 `deepseek-chat` **不报错但被静默路由**（响应 `model` 回显 `deepseek-flash`） | 默认模型改 `deepseek-flash`（模型名进缓存键，名字必须与实际一致） |
| 思考模式 | 新一代模型**默认开启**（effort=high）；`thinking={"type":"disabled"}` 实测 200 且不返回 `reasoning_content` | 请求显式关思考模式（省钱、不挤占 `max_tokens`） |
| JSON 模式 | `response_format={"type":"json_object"}` 可用 | 保持 |
| 计费明细 | `usage` 含 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | 按真实命中/未命中分段计费，成本不用猜 |

### 2. 代码变更

- `src/processors/llm_client.py`：`DEFAULT_MODEL=deepseek-flash`；`thinking_mode` 参数（默认 `disabled`）；
  `LLMResponse` 增加 cache hit/miss token；客户端累加 token；新增 **官方价目表**（峰/谷两档 + 旧名别名）
  与 `estimate_cost_usd()` / `is_peak_utc()`；`stats()` 带 `usage`。
- `src/processors/llm_extractor.py`：`max_api_calls` / `max_retries` / `rate_limit_per_minute` /
  `thinking_mode` / `base_url` 可注入；**token 台账**分 `api`（本次真花钱）与 `cached`（复用缓存，¥0）
  两本，`stats()["usage"]` 给出本次与全量的峰/谷费用估算。
- `scripts/evaluate_extractor.py`：新增 `--extractor regex|llm`、`--limit`、`--sample head|stratified`、
  `--llm-model/--llm-cache-dir/--llm-cache-mode/--llm-max-api-calls/--llm-base-url/--llm-usage-out`；
  **两种抽取器的默认产物路径分开**（LLM 不覆盖正则基线报告）；跑完打印台账；报告新增
  「7.1 Token 与费用台账」与"零成本复跑"命令；LLM 系统性故障（401/缺 Key/readonly 未命中）
  **返回码 2 快速失败**，不产出误导性结果。
- `.env.example`：`DEEPSEEK_MODEL=deepseek-flash` + 新增 `DEEPSEEK_THINKING_MODE`。
- `.gitignore`：新增显式 `logs/llm_cache/` 规则（`logs/` 已整体忽略，这里是**显式声明**：
  缓存里是付费 API 的原始响应，属原始数据，严禁入库）。

### 3. 试点实测（`--extractor llm --limit 20`，`--sample head`）

- **成功率**：20/20 调用成功，**0 API 失败、0 解析失败**；思考模式 `disabled`。
- **Token / 成本**（官方价目表，2026-09-13 抓取）：

| 口径 | 数值 |
|---|---|
| 输入 token | **33,180**（上下文缓存**命中 27,520** / 未命中 5,660） |
| 输出 token | **1,690** |
| 本次费用估算 | **峰值价 $0.0039（≈¥0.028）** / 低谷价 $0.0019 |
| 单条均价 | ≈ **$0.0002/条（≈¥0.0014）** |
| 外推全量 200 条 | ≈ **$0.039（≈¥0.28）**（缓存命中率会更高，实际更低） |
| `readonly` 复跑 | **20/20 命中、0 次请求、$0.0000**（已验证） |

- **字段准确率（人工子集口径，13 格）**：核心四字段 **92.3%** —
  方向 100%（2/2）、止损 100%（4/4）、目标位 100%（6/6）、**周期 0%（1 格漏判）**、
  信息类型 **46.2%**（13 格错 7 格）。
- **与正则基线对比（同为人工子集口径）**：止损 0% → **100%**、目标位 25% → **100%**、
  信息类型 40.7% → 46.2%、方向 100% → 100%。→ **LLM 把正则的"该判未判"问题基本解决了**。

### 4. 试点暴露的 3 个真问题（已登记 TD-24/25/26，全量 200 前必须先定）

1. **TD-24（红线）few-shot 与评测语料逐字重叠**：6 条 few-shot 里 4 条的输入句能在语料里
   逐字找到（条件句模板 15 条、引用/复盘/弱化各 1 条）→ **18/200 条语料"模型见过答案"**，
   20 条试点中 4 条被污染。剔除污染帖后指标几乎不变（方向 100%、止损/目标 100%、
   信息类型 36.4%），但**违反"严禁未来数据泄漏"**，必须换语料外的自造例句 + 升 `opinion-prompt-v2`。
   审计工具：`logs/_llm_contamination_check.py`。
2. **TD-25 信息类型偏保守**：7 格错误**全部**是 `gold=SENTIMENT/POSITIONING/MACRO/OTHER`
   ↔ `LLM=TECHNICAL`（如「还有多少人愿意进场」「ETF 持仓连续三日增仓」「美债收益率回落」）→
   需要人工补"多线索并存时的优先级"口径，再写进 Prompt v2。
3. **TD-26 `no_opinion` 与 §4.9 规则 4 冲突**：CPI 数据播报被判"无观点"，
   按 §4.9 应为 `stance=UNKNOWN + information_type=MACRO`；**且 few-shot 第 6 例自己教错了**。

### 5. 工程与质量门禁

- 新增测试 **27 项**：思考模式开关/校验、缓存命中与未命中 token、费用估算（含峰谷价差、
  旧名别名、未知模型返回 `None`）、峰值时段判定、台账 `api`/`cached` 分层、
  抽样（head/stratified/线索识别/子串误伤回归）、LLM 端到端（假抽取器：三份产物 + 台账打印）、
  LLM 致命错误返回码 2、regex 回归。
- `pytest` **1039 passed / 1 skipped**、`ruff` 全绿、`mypy`（66 files）全绿；
  **未新增迁移、未改 schema、未新增依赖**。
- 安全复核：20 个缓存文件**无 `sk-` 明文**、无 `.tmp` 残留；`--api-key` 这类参数**不存在**，
  密钥只从 `.env`/环境变量读。

### 6. 产物

| 文件 | 内容 |
|---|---|
| `logs/extractor_eval_llm.csv` | 20 条 × 5 字段 = **100 行**逐格明细（可人工复核每一格） |
| `logs/llm_cache/<前2位>/<key>.json` | **20 个**原始响应（含 usage/request_id/attempts，供审计与零成本复跑） |
| `logs/extractor_eval_llm_usage.json` | Token/费用台账（`api` / `cached` / 峰谷价估算） |
| `docs/experiments/Phase2_LLM试点报告.md` | 试点报告（含 7.1 Token 与费用台账 + 复现命令） |
| `logs/_pilot_digest.txt` | **人工复核素材**：20 条「原文 + LLM JSON + 金标准」（零成本从缓存生成） |
| `logs/_probe_deepseek.py` / `_llm_pilot_review.py` / `_llm_contamination_check.py` | 临时审计脚本（在 `logs/` 内，不入库） |

### 7. 暂停点（等用户决策）

1. 是否按 TD-24/25/26 改 Prompt（`opinion-prompt-v2`）+ 重跑 20 条验证（≈ **$0.004**）——**建议先做**；
2. `information_type` 优先级口径需人工裁决（写进 `docs/10 §4.9`）；
3. 通过后再跑全量 200（≈ **$0.04**）并出《Phase 2 基线对比报告》（正则 vs LLM）。

### 8. 用户裁决（2026-09-13，选项 A）与落实结果

用户裁决：①**TD-24 必须彻底堵死**（评测集原句不得出现在 few-shot）；
②`information_type` 口径 = **"操作优先，驱动决定分类"**（含 L1~L5 五条优先级）；
③宏观数据播报一律 `stance=UNKNOWN` + `information_type=MACRO` + `no_opinion=true`；
④**`human-adjudicated` 是唯一绝对金标准**，共识子集只作参考并**标注来源分层**。

**落实（Prompt v2 → v5，同一批 20 条逐版真实 API 验证）**：

| 版本 | 变更 | 100 格命中/错判/漏判/多判 | 人工子集信息类型 |
|---|---|---|---|
| v1 | 初版（**few-shot 与语料逐字重叠**） | 54 / 8 / 5 / 10 | 6/13 = 46.2% |
| v2 | few-shot 全换语料外自造句 + 决策阶梯 + `no_opinion` 语义 | 64 / 2 / 1 / 10 | 12/13 = 92.3% |
| v3 | 补「观望→FLAT、情绪≠FLAT」 | 62 / 4 / 1 / 10 | 11/13 = 84.6%（**回归**，已定位） |
| v4 | L3 收紧（情绪须是主旨或方向依据）+ 对比样例 | **65 / 1 / 1 / 10** | 12/13 = 92.3% |
| v5 | L3 判别线：情绪句须**带方向含义**才算驱动 | **65 / 1 / 1 / 10** | 12/13 = 92.3% |

- **人工子集（26 格）**：v5 命中 **24/26 = 92.3%**（方向 2/2、止损 4/4、目标位 6/6、
  信息类型 12/13、周期 0/1）；`spurious = 0`、`wrong_value = 0`（人工口径内）。
- **10 个 spurious 全为共识金标准过期**：条件句/引用句/复盘句的点位，共识写"未给出"、
  人工口径要求**照标** → 属金标准分层差异（已按裁决在报告中标注来源分层）。
- **成本**：每个版本 20 条 ≈ **$0.004**（输入 3.3~6.3 万 tokens、缓存命中 83%~91%、
  输出 0.17~0.19 万 tokens）；v1→v5 合计 ≈ **$0.023**；`readonly` 复跑 **¥0**。
- **数据泄漏复核**：12 条 few-shot 与 200 条语料的最长公共子串 **≤ 9 字符**（无整句重合）；
  回归测试 `tests/unit/test_prompt_opinion.py::test_few_shot_has_no_overlap_with_evaluation_corpus`
  已锁定（阈值 12 字符）。
- **交付文档**：`docs/experiments/Phase2_LLM试点_v1-v5对比.md`（逐字段五版对比 +
  条件句/引用句/多目标逐帖「原文 + 金标准 + v1/v5 预测 + v5 原始 JSON」+ 成本与复现命令）。
- **登记 TD-27（P2，待一句话裁决）**：①`mock-post-0009`（操作块 + "risk-on 情绪主导，承压"）
  人工 `SENTIMENT`、v5 `TECHNICAL`；②`mock-post-0028` 人工 `15M` 但正文只写"短线"，
  与 `docs/10 §4.2`「短线 → 1H」冲突。
- **仍未做（等用户确认，指令要求）**：全量 200 条（预估 **≈ $0.045**，见对比文档 §5）。

### 9. 最终交付（2026-09-13，全量 200 条 + 基线对比 + git 初始化）

按用户最终裁决执行（①`mock-post-0009` 金标准改判 `TECHNICAL`；②`docs/10 §4.2` 增加
「短线思路 → `15m`」映射，金标准保留 `15M`；③随即跑全量 200；④出《Phase 2 基线对比报告》；
⑤`git init` 并首次提交）：

**1) 规则与金标准落地**

- `docs/10 §4.9` 规则 9 调整为 **L1 持仓 > L2 宏观 > L3 操作价位 > L4 情绪 > L5 引用/复盘 > L6 兜底**，
  并明确"**操作优先于情绪**"（`mock-post-0009` 依据）；`§4.2` 增加「短线思路/短线观望 → `15m`」。
- `logs/ground_truth_200.csv`：`mock-post-0009 / information_type` 由 `SENTIMENT` **改判 `TECHNICAL`**
  （`reviewer=chenxiangxie`、`provenance` 追加裁决记录；改前版本归档
  `logs/archive/ground_truth_200_pre_20260913.csv`）。仍为 1000 格（人工 226 / 共识 774）。
- Prompt 升 **`opinion-prompt-v6`**（`parser_version=llm-deepseek-v6`）：L3 改为"操作价位优先"、
  rule 8 增加"短线思路→15m"、few-shot 增至 13 条（含"价位 + 情绪背景 → TECHNICAL"对比样例）。

**2) 全量 200 条实测（真实 DeepSeek API，`--llm-max-api-calls 250` 预算门禁内）**

| 指标 | 正则（`mock-regex-v1`） | LLM（`llm-deepseek-v6`） | 提升 |
|---|---|---|---|
| 方向 `stance`（人工 14 格） | 100.0% | **100.0%** | +0.0 pp |
| 周期 `horizon`（人工 3~4 格） | 0.0% | **100.0%** | **+100.0 pp** |
| 止损 `stop_loss`（人工 29 格） | 0.0% | **100.0%** | **+100.0 pp** |
| 目标位 `take_profit`（人工 44 格） | 25.0% | **100.0%** | **+75.0 pp** |
| 信息类型 `information_type`（人工 135 格） | 40.7% | **87.4%** | **+46.7 pp** |
| 核心四字段合计（人工 90 格） | 27.8% | **100.0%** | +72.2 pp |

- 逐格变化：**修复 385 格 / 回归 14 格**（回归全部是 `information_type`：金标准
  `SENTIMENT`(8) / `NEWS`(8) / `MACRO`(1) ↔ LLM `TECHNICAL`）。
- LLM 质量指标：无观点率 **0%**、UNKNOWN 方向率 46.5%、confidence 非法 **0**、
  API 失败 **0**、解析失败 **0**。
- 人工子集 226 格：命中 208 / 错判 17 / 多判 1；共识 774 格：命中 475 / 双方未给出 217 /
  多判 78（**全为条件句·引用句·复盘句的点位**，属金标准分层差异，见 `docs/10 §4.9` 规则 8）。
- **成本**：本次 180 次调用（20 条复用试点缓存）→ 输入 605,199 tokens（缓存命中 567,808 /
  未命中 37,391）+ 输出 16,615 → **峰值价 $0.0346（≈¥0.25）**；含缓存复用的全量口径 $0.0391。
- 产物：`logs/extractor_eval_llm.csv`（1000 行）、`logs/extractor_eval_llm_usage.json`、
  `docs/experiments/Phase2_LLM试点报告.md`（归档 `logs/archive/full200_llm_v6/`）。

**3) 新增基线对比工具（可入库、带测试）**

- `scripts/compare_extractor_baselines.py`：读两份逐格明细（正则 / LLM），
  **先校验金标准一致性**（不一致即返回码 2，不静默比较），再出逐字段准确率、
  提升百分点、五类判定构成、**修复/回归逐格清单**、Token 台账、
  `docs/08 §5` 门槛 PASS/FAIL 结论。
- 报告：`docs/experiments/Phase 2 基线对比报告.md`（1000 格对齐、0 口径问题）。
- 测试：`tests/unit/test_baseline_comparison.py`（13 项：对齐校验、分层口径、修复/回归、
  报告渲染、CLI 端到端、不碰数据库红线）。

**4) 工程与安全**

- `pytest` **1380 passed / 1 skipped**、`ruff` 全绿、`mypy`（67 files）全绿；
  **未新增迁移、未改 schema、未新增依赖**。
- `git init` + 首次提交：确认 `.gitignore` 覆盖 `.env` / `.env.*` / `logs/` / `logs/llm_cache/` /
  `*.db` / `.venv/`，提交前用 `git status --porcelain` 逐项核对**无密钥、无数据、无缓存**入库。

**5) 暂停点（等用户确认，不进入 Phase 3）**

登记 **TD-28（P2，需一句话裁决）**：全量跑出的 14 格回归 / 17 格错判，
根因是"驱动句（情绪/消息）+ 明确操作价位"与"决策阶梯缺 `NEWS` 一级"。
两条互斥路径见 `TECH_DEBT.md`：**路径 A**（操作绝对优先 → 再改 ~16 格人工金标准）；
**路径 B（建议）**（阶梯补 `NEWS` + 区分"驱动是交易理由 vs 背景附注" → 预计信息类型 ≈ 99%）。

---

## 第六轮（2026-09-13）：TD-28 路径 B 落地 + 全量 200 定版 + 最终对比报告

> 用户裁决：走**路径 B**（①阶梯补 `L2.5` 消息驱动 → `NEWS`；②细化 L3：
> 操作价位优先，但情绪/消息若是**交易理由**则按驱动分类，仅"背景/附注式"走 `TECHNICAL`）；
> 更新 `docs/10 §4`；重跑全量 200（真实 API，预估 ≈$0.035）；出**最终版**
> 《Phase 2 基线对比报告.md》（含**所有"人工纠正模型 / 模型纠正人工"典型案例**）；完成后暂停。

### 1. Prompt 迭代（v7 → v13，每次迭代都重跑同一批 20 条验算）

| 版本 | 改动 | 20 条人工子集结果 | 结论 |
|---|---|---|---|
| v7 | `L2.5` 消息→`NEWS`；L3 两个例外 + "交易理由 vs 背景附注"判别线 | 2 格错（0027、0016） | 阶梯方向正确，但"判别线"措辞波及宏观与周期 |
| v8 | 判别线只管情绪/消息；明确周期词优先 | 2 格错（0019、0028） | 修好 0016，又碰坏 0019/0028 |
| v9 | 宏观驱动须为**具体变量**；horizon 按明确度排序 | 1 格错（0027） | 最优候选，但 horizon 规则仍是隐患 |
| v10 | 追加"宏观即使以'关注…'出现也判 MACRO" | 2 格错（0009、0019） | **过度倾向 MACRO**，撤回 |
| v11 | 保留 v10 唯一有效的 few-shot，撤回两处措辞 | **0 格错** | 20 条满分 |
| v12 | v6 的 horizon 规则 + v11 的阶梯 | 1 格错（0027） | 20 条少一条 few-shot |
| **v13（定版）** | v12 + 那条 few-shot | **0 格错** | ✅ 最终采用 |

**关键工程发现**：`v11` 在 20 条满分、但在**全量 200** 上把 `horizon` 从 100% 打到 66.7%
（`4H/30M/1D` 共 10 格判错）——**小样本满分不等于全量最优**。
逐格对比 v6/v11 归档明细后确认：v6 的 horizon 规则（简单映射表）在 200 条上**全部命中**，
而"按明确度排序 + 栏目名不计"的改写反而制造回归 → **回退规则 8**，只保留阶梯改进。

### 2. 全量 200 条定版结果（`llm-deepseek-v13`，人工子集 = 唯一验收口径）

| 字段 | 金标准有值 | 正则 `mock-regex-v1` | LLM v13 | 提升 | 结论 |
|---|---|---|---|---|---|
| 方向 `stance` | 14 | 100.0% | **100.0%** | +0.0 pp | ✅ PASS |
| 周期 `horizon` | 3 | 0.0% | **100.0%** | **+100.0 pp** | ✅ PASS |
| 止损 `stop_loss` | 29 | 0.0% | **100.0%** | **+100.0 pp** | ✅ PASS |
| 目标位 `take_profit` | 44 | 25.0% | **100.0%** | **+75.0 pp** | ✅ PASS |
| **核心四字段** | 90 | **27.8%** | **100.0%** | **+72.2 pp** | ✅ 四项全 PASS |
| 信息类型 `information_type` | 135 | 40.7% | **91.9%** | **+51.1 pp** | 无门槛（91.9%） |

- 逐格变化：**修复 380 格 / 回归 0 格**（v6 的 14 格回归全部消除）。
- 质量：无观点率 0%、UNKNOWN 方向率 47.0%、confidence 非法 0、API 失败 0、解析失败 0。
- **成本**：180 次调用（20 条复用缓存）→ 输入 721,119（缓存命中 683,904 = **94.9%**）
  + 输出 16,728 → **$0.0353（≈¥0.25）**；含缓存口径 $0.0392。
- 产物：`logs/extractor_eval_llm.csv`（1000 行）、`docs/experiments/Phase2_LLM试点报告.md`
  （归档 `logs/archive/full200_llm_v13/`）。

### 3. 最终《Phase 2 基线对比报告.md》

`scripts/compare_extractor_baselines.py` 新增 **§5「人工 ↔ 模型 对齐案例」**（含单测）：

- **5.1 模型纠正人工**（金标准按模型答案改判）：`mock-post-0009 / information_type`
  `SENTIMENT → TECHNICAL`（裁决人 `chenxiangxie`；理由：操作优先）→ **LLM 已对齐 ✅**。
  实现方式：与改判前金标准归档（`logs/archive/ground_truth_200_pre_20260913.csv`）逐格 diff 得出，
  **可复算、不靠人工回忆**。
- **5.2 人工纠正模型**（金标准维持、模型判错）：**11 格**逐格列出（原文 + 金标准 + 模型 + 正则侧），
  全部是"**操作块 + 驱动尾句**"形态（`0027/0079/0109/0119/0127/0139` 等）→ 登记 **TD-29**。

### 4. 文档与规则同步

- `docs/10 §4.9` 规则 9：阶梯重写为 `L1 持仓 > L2 宏观 > L2.5 消息 > L4/L2.5 例外
  > L3 操作价位 > L5 引用/复盘 > L6 兜底`；判别线**只作用于情绪/消息**，
  且**既不降级 L1/L2、也不升格尾句**；新增「已知残余边界（TD-29）」段落。
- `docs/10 §4.5`：旧优先级 `MACRO > NEWS > POSITIONING > SENTIMENT > TECHNICAL > OTHER`
  与二次裁决冲突 → 标注**以 §4.9 规则 9 为准**。
- `docs/10 §4.2`：映射表补「短线思路→`15m`」，并写明"本表即实测最优、**不可再细化**"。
- `TECH_DEBT.md`：**TD-28 关闭**（路径 B 落地 + 实测对比表）、新登记 **TD-29**（口径边界待真实语料复核）。

### 5. 门禁与提交

- `pytest` **1891 passed / 1 skipped**、`ruff` 全绿、`mypy`（67 files）全绿；
  **未新增迁移、未改 schema、未新增依赖**。
- `git` 第二个提交包含本轮全部源码/文档/测试变更（`logs/` 与 `.env` 仍被忽略）。

### 6. 暂停点（等用户确认，**不进入 Phase 3**）

1. 真实语料复核 **TD-29**："操作块 + 驱动尾句"的 `information_type` 边界（当前 Mock 上人工自身不一致）；
2. 是否按 `docs/08 §5` 启动**真实作者帖子**抽样（替换 Mock 语料做终验）；
3. Phase 2 收尾清单（报告归档、`docs/05` 路线图勾选）待确认后再动。



---

## 第七轮（2026-09-14）：Phase 2 主数据源改为**自建 RSS 采集器**（路线 1 裁决落地）

### 1. 用户裁决（2026-09-14）

1. **compliant-scrapers 降级**：定位为 **Phase 3 可选新闻源**（标题级）、**未验证第三方服务**、**默认禁用**；
   **不注册 Apify 账号、不用于 Phase 2 验收**（`docs/12` 已按此重写 §0 定位 / §7 决议 / §8 排期）。
2. **Phase 2 语料主数据源改为自建 RSS 采集器**（`feedparser` 新增依赖）；
3. **执行顺序**：先交付代码 + Mock 测试（**不发真实请求**）→ 单测通过 → 小流量真实验证 20 条
   → 抓 100 条产出 `logs/real_posts_*.csv` → **暂停等人工标注** → 正则 vs LLM 最终对比。

### 2. 交付物（本轮）

| 文件 | 内容 |
|---|---|
| `config/rss_sources.json`（**新建**） | 源清单：金十数据快讯 / 汇通网黄金频道 / 华尔街见闻 / Investing.com Gold。**全部 `enabled=false` + `verified=false` + `robots_check=true`**（候选 URL 待冒烟验证，TD-30） |
| `src/collectors/rss_cache.py`（**新建**） | 响应缓存（与 `llm_cache` 同语义）：`auto`/`readonly`/`refresh`/`off`、`sha256(url)` 键、原子写、**失败也缓存**、损坏→miss |
| `src/collectors/rss_collector.py`（**新建**） | `RssSourceSpec` + `load_rss_sources`（缺字段/非法 URL/id 重复 → 记问题并跳过）；`robots_allows`（**fail-closed**，并区分"规则禁止→skipped"与"取不到→failed"）；`parse_feed_entries`（feedparser 解析，**时间仍走项目自有"不猜时区"逻辑**）；`fetch_spec_entries`（robots→缓存→条件请求 304→解析→窗口过滤，采集器与 CLI **共用同一实现**）；`RssCollector(BaseCollector)`（每源一页 + `feed_index` 游标 + 单源失败隔离 + **所有源失败 → CollectorError → 运行记 FAILED**） |
| `scripts/collect_rss.py`（**新建**） | CLI：**默认 `--dry-run`**（零网络，`--fixture` 或 `readonly` 缓存）；`--no-dry-run` 才真实抓取；导出 CSV 列 = `docs/11 §1.1` 契约 + `title/category/notes_collect`；退出码 0/2/3 |
| 测试（**新增 56 项**） | `tests/unit/test_rss_cache.py`（9）、`tests/unit/test_rss_collector.py`（34：源清单/解析/robots/缓存协商/304/只读拒绝联网/窗口/映射/健康检查/统计）、`tests/integration/test_rss_collector_persistence.py`（6：落库字段契约/幂等/单源失败隔离/全失败→FAILED/游标续采/只读零请求复放）、`tests/unit/test_collect_rss_cli.py`（6：dry-run 不写不请求/离线导出列契约/退出码） |

### 3. 关键工程决策（与既有架构一致）

- **时间不猜**：feedparser 会把无时区时间当 UTC，本项目**不采用**它的时间解析结果，只用它解析结构与正文；
  无时区/无法解析 → `published_at` 留空 + `raw_json` 记录 `published_raw`/`reason`，`effective_at` 回落 `collected_at`。
- **缓存语义分层**：`auto` = 协商缓存（有 `ETag`/`Last-Modified` 就发条件请求，**否则直接复用缓存**）；
  `readonly` = 纯重放（未命中即**拒绝联网**）→ `--dry-run` 的零请求保证由此而来。
- **robots 判定分类**：`by_rule=True`（规则明确禁止）→ `skipped`；`by_rule=False`（403/超时/解析失败）→ `failed`
  → 避免"网络全挂"被记成 SUCCESS 的静默成功。
- **`feedparser>=6.0.11` 已写入 `pyproject.toml`**（团队批准；已安装 6.0.14）；**未新增迁移、未改 schema**。

### 4. 门禁结果

- `pytest` **1891 → 1947 passed / 1 skipped**（新增 56 项，全部零网络：靠 `MockTransport` + autouse 网络守卫）；
  分块复核（每块一次 `pytest` 调用，逐块确认）：`tests/unit` **1624 passed / 1 skipped**（=收集数 1625）、
  `tests/data_quality` **25 passed**、`tests/leakage` **20 passed**、`tests/integration` **278 passed**
  （消息采集/行情/宏观/新闻/作者库/CLI/迁移/Phase1+Phase2 schema/RSS 落库 等 18 个文件逐一确认）；
  合计 **1947 passed / 1 skipped = 收集数 1948**，与全量 `pytest -q` 一致；
- `ruff check .` **0 issue**；`mypy` **70 source files** 全绿；
- ⚠️ **`ruff format --check .` 不再全绿**：venv 里的 ruff 已升到 **0.16.7**，对全仓 **76** 个文件报
  "would be reformatted"（其中 71 个是本次未改动的历史文件，属 88 列 black 风格 vs 配置 `line-length = 100`
  的**版本漂移**，非本次引入）→ 记 **TD-33**，建议单独一次纯格式提交或钉住 ruff 版本，不与功能改动混合；
- **本轮未发起任何真实网络请求**（符合用户"先不要跑真实网络请求"的要求）。

### 5. 下一步（等用户放行）

1. **放行小流量真实验证**：逐个源跑 `--no-dry-run --per-feed-limit 5`，核对 HTTP 状态 / robots / 条数 / 字段质量，
   通过的源在 `config/rss_sources.json` 置 `enabled=true`/`verified=true`（TD-30）；
2. 抓 100~200 条 → `logs/real_posts_<date>.csv` → **暂停等人工标注**；
3. 标注完成后：`sample_annotation_set.py --no-mock-fill --require-full` → `build_ground_truth.py`
   → `evaluate_extractor.py`（regex / llm）→ `compare_extractor_baselines.py` → 真实语料验收报告；
4. **报告前不得进入 Phase 3**。

---

## 第八轮（2026-09-14）：ruff 钉版本 + 冒烟硬性要求落地 + **阶段一逐源冒烟（4/4 未通过）**

### 1. 用户裁决

1. **TD-33 选 (b)**：在 `pyproject.toml` **钉死 ruff 版本**（`ruff==0.16.7`，注释写明"升级必须独立提交 + 同次跑
   `ruff format .` 与全量门禁"）→ 已落地；`.github/workflows/ci.yml` 的实际门禁是 `ruff check .` + `mypy` + `pytest -q`
   （**不含 format --check**），故 CI 稳定性恢复；
2. **放行阶段一真实抓取**，并追加三条硬性要求：robots 前置、请求间隔 ≥1s、**每源单独原始 JSON**；
3. **不许自动启用任何源**（`enabled=true` 必须等人工确认）。

### 2. 为此新增的工程能力（全部有 Mock 单测，零真实网络）

| 变更 | 内容 |
|---|---|
| `src/collectors/rss_collector.py` | `FeedFetchResult` 新增 **`robots`（robots 判定与原因）** 与 **`http_status`** 两个观测字段（默认空，向后兼容），供《源验证报告》与复盘使用 |
| `scripts/collect_rss.py` | 新增 **`--min-interval`**（任意两次请求最小间隔，**硬下限 1.0s**，低于则抬升 + 告警）与 **`RateLimiter`**（可注入 clock/sleep，便于零等待单测）；新增 **`--raw-dir`**（每源单独原始 JSON：spec / robots / HTTP / 缓存 ETag / 解析条目 / **原始响应体**）；`--feed-limit` 作为 `--per-feed-limit` 等价别名；终端逐源打印 `status / 条目 / http / robots` |
| 缺陷修复 | ①CLI 用完 `AiohttpTransport` **未关闭会话**（真实运行打印 `Unclosed client session`）→ 在 `finally` 中 `close()`，并加回归测试 + 真实复跑确认告警消失；②3 个 CLI 测试会用默认 `--raw-dir` 往仓库 `logs/rss_raw` 写文件（发现 `feed_a_*.json` 污染）→ 全部改为 `--raw-dir ""` |
| 测试 | `tests/unit/test_collect_rss_cli.py` 6 → **11** 项；`tests/unit/test_rss_collector.py` 33 → **34** 项（净新增 6 项，全部零网络：假时钟限速 / Mock 传输下的 robots+HTTP+原始 JSON / robots 禁止时零抓取 / 别名 / 会话关闭 / 观测字段） |

### 3. 阶段一真实冒烟结果（2026-09-13 11:02 UTC，逐源执行）

| 源 | robots.txt | feed HTTP | 条目 | 判定 |
|---|---|---|---|---|
| `jin10_flash` | 允许（robots 404 → 无限制） | **404** | 0 | ❌ 不可用 |
| `fx678_gold` | 允许 | **404** | 0 | ❌ 不可用 |
| `wallstreetcn_feed` | 允许 | **404** | 0 | ❌ 不可用 |
| `investing_gold` | **403 → 不可验证（合规拒绝）** | 未请求 | 0 | ❌ 不可用 |

- **结论：0/4 通过**，`config/rss_sources.json` **未做任何改动**（仍全 `enabled=false`/`verified=false`），
  **阶段三（抓 100~200 条）不启动**；
- 硬性要求实测：`--min-interval 1.5` 下第 2 个请求实际等待 **1.369 / 1.398 / 1.453 s**；
  `investing_gold` 因 robots 403 **只发了 1 次请求**（未抓 feed）；4 份原始 JSON 已落 `logs/rss_raw/`；
- 报告：**`docs/experiments/rss_source_verification_20260913.md`**（含留痕文件清单与复现命令）；
- `docs/11 §1.0` 的 ⓪-2 已改写为「临时探测配置 + 逐源冒烟」的标准流程并挂上本轮结论。

### 4. 门禁

- `ruff check .` **0 issue**（ruff 已钉 0.16.7）、`mypy` **70 files** 全绿；
- `pytest`：`tests/unit` + `data_quality` + `leakage` **1676 passed / 1 skipped**（分块复核）、
  `tests/integration` 采集器相关批次 **31 passed**（RSS 落库 6 + runner 25）；
- 本轮真实请求仅限 4 个源的 robots/feed 探测（共 7 次请求，1.5s 间隔），**未抓任何 HTML 页面**。

### 5. 下一步（等用户决策，三选一）

- **A** 用户提供**已验证的 feed 地址** → 走同一套冒烟；
- **B** 用户批准"入口探测"（只读首页解析 `<link rel="alternate" type="application/rss+xml">`）；
- **C** 换数据源（机器人友好的公开 RSS）。
通过 ≥1 个源并**经人工确认**后，才改 `config/rss_sources.json` 的 `enabled/verified`，再执行阶段三。

---

## 第九轮（2026-09-13）：用户选 A 提供 4 个新源 → **前 2 个冒烟通过**；治 3 个真实缺陷

### 1. 用户决策与输入

- 选 **A**：用户人工验证并提供 4 个源（`fred_blog` / `fed_press` / `ecb_press` / `yahoo_gold`）；
- 指令：**只启用前 2 个**做冒烟、不许自动启用其它源、robots 结果要记日志、非 200 立刻失败且重试 ≤3、
  **全量抓取必须另行批准**。

### 2. 落地

- `config/rss_sources.json` → **`rss-sources-v2`**：新增 4 源，`fred_blog`/`fed_press` `enabled=true`
  （`verified=false`，等冒烟 + 人工确认）；`ecb_press`/`yahoo_gold` 保持禁用；
  旧 4 个失败候选保留并写明失败原因（可追溯）。
- `tests/unit/test_rss_collector.py`：把"默认全禁用"旧契约升级为 **"只有人工批准的源才允许启用"**
  （`APPROVED_ENABLED_SOURCES = {fred_blog, fed_press}` + 全部 `robots_check=true` + 全 https）。
- CLI 新增 **`--verify-log`**（默认 `logs/rss_verification.log`，JSONL 追加）：每源一行记录
  `robots_allowed` / `robots_by_rule` / `robots_reason` / `http_status` / `status` / `entries`（满足"robots 结果记日志"）。
- CLI 新增 **`off`/`none`/`-` 关闭哨兵**：PowerShell 5.1 会吞掉空字符串参数（实测 `--raw-dir ""` 报缺参），
  故 `_optional_path()` 统一处理，并有单测锁定。

### 3. 冒烟结果（逐源；robots 前置 + `--min-interval 1.5` + 每源原始 JSON）

| 源 | robots.txt | HTTP | 条目 | 13 列非空 | content 长度（min/median/max） | 结论 |
|---|---|---|---|---|---|---|
| `fred_blog` | 允许 | 200 | 5 | 5/5 | 2120 / 2523 / 4562 | ✅ 技术通过（**长文，可作观点语料候选**） |
| `fed_press` | 允许（robots 404→无限制） | 200 | 5 | 5/5 | 74 / 100 / 154 | ✅ 技术通过（**正文=标题 → 建议仅作事件源**） |

- `published_at` 两源均 100% 有时区 → 正常转 UTC，**无一条猜测**；
- 留痕：`logs/_smoke_<id>.json`（统计）、`logs/rss_raw/<id>_*.json`（原始 JSON，67 KB / 20 KB）、
  `logs/rss_verification.log`（JSONL）、`logs/rss_cache/`（响应缓存，复跑零请求）。

### 4. 本轮真实抓取暴露的第 3 个缺陷（已修 + 测试）—— **HTML 实体未解码**

- 现象：`fred_blog` 的 `content` 残留 `&#8217;` 等（原始 feed 63 处）→ 会污染抽取与人工标注；
- 定位：`src/collectors/news.py::strip_html` 只去标签、未解码实体；
- 修复：去标签后 `html.unescape`；新增 `test_strip_html_decodes_entities_from_real_feeds`；
- 验证（**零网络**）：`--cache-mode readonly` 只读重放两源 → 残留实体 **34 → 0**，
  正文变为 `New York Fed’s`（对照 `logs/_smoke_*_fixed.csv`）。

### 5. 门禁

- `pytest`：unit + data_quality + leakage **1702 passed / 1 skipped**；
- `ruff check .` **0 issue**（ruff==0.16.7 已钉）；`mypy` **70 files** 全绿；
- 本轮真实请求：`fred_blog`/`fed_press` 各 1 次 robots + 1 次 feed（共 4 次），
  `ecb_press`/`yahoo_gold` **零请求**；未抓任何 HTML 页面。

### 6. 未做（等批准）

① 未把任何 `verified` 置 true；② 未冒烟后 2 个源；③ **未做全量抓取**（阶段三需明确批准）。

---

## 第十轮（2026-09-13）：四源最终定态 + **首轮真实语料 25 条**（含 2 个新缺陷修复）

### 1. 用户裁决（选项 3 + 逐源批准）

- `fred_blog` → enabled/verified=true, `source_type=NEWS`；
- `fed_press` → enabled/verified=true, **`source_type=EVENT`**（只作宏观事件，不进观点语料）；
- `ecb_press` → enabled/verified=true, `source_type=NEWS`（用户确认其冒烟 http=200 / robots 允许 / 5 条）；
- `yahoo_gold` → 保持 enabled=false / verified=false（robots 403，合规拒绝）；
- 抓 100 条（fred_blog + ecb_press，各 ~50），输出 `logs/real_posts_2026_09_14.csv`，抓完暂停等人工标注。

### 2. 新增能力：`source_type`（采集侧用途标记）

- 背景：DB 枚举 `SourceType`（WEIBO/NEWS/MARKET/MACRO）与 `RawItemType`（POST/NEWS/MACRO/QUOTE）**都没有 EVENT**，
  为不伪造 DB 值，`source_type` 实现为**采集侧标记**：`NEWS`（可进观点语料）/ `EVENT`（不进观点语料）；
- 落地：写入 `raw_json["source_type"]` + CSV `notes_collect`（"采集用途=EVENT（仅事件源，不进入观点语料）"）
  + CLI 终端逐源打印 `type=EVENT`，下游抽样可按此过滤；
- 测试：`test_load_sources_normalises_source_type_and_rejects_unknown`（大小写归一 / 非法值回退+记问题）、
  载荷断言、CLI 的 CSV+原始 JSON 标记断言；
- 配置测试升级：`APPROVED_ENABLED_SOURCES = {fred_blog, fed_press, ecb_press}`（**新增启用必须人工确认 + 同步本常量**）。

### 3. 首轮真实语料抓取（`--source fred_blog --source ecb_press --no-dry-run --limit 100 --min-interval 1.5`）

| 源 | 请求结果 | 条目 |
|---|---|---|
| `fred_blog` | HTTP **304**（robots 允许） | **10**（缓存重放） |
| `ecb_press` | HTTP **304**（robots 允许） | **15**（缓存重放） |
| 合计 | 2 robots + 2 条件请求 | **25 行** → `logs/real_posts_2026_09_14.csv` |

- **HTTP 异常 = 0**（无 4xx/5xx、无超时、无重试）；`fed_press` 与 `yahoo_gold` 本轮**零请求**；
- 字段完整性：**列顺序与 §1.1 契约逐列一致；13/13 列全为 25/25 非空**；`published_at` 缺失 0/25；
  `effective_at != published_at` 0/25；残留 HTML 实体/标签 0；
- **为什么不是 100 条**：两源均 304（无更新）+ 这两个 feed 自身只有 10/15 条 → 25 条是当前上限（记 TD-30）。

### 4. 第 4 个真实缺陷（本次抓取暴露）：**304 会导致语料导出 0 条**

- 现象：首次导出 `fred_blog` 返回 304 → `可用条目=0`（缓存里明明有内容）；
- 修复：**304 时用本地缓存体重放条目**（`status=not_modified`、`http_status=304`、`warnings` 留痕
  "条目改由本地缓存重放（可复现）"；DB 路径由幂等去重兜底）；
- 测试：改写 `test_conditional_request_handles_304` + 新增 `test_304_without_cached_body_yields_no_entries`；
- 效果：重跑即得 10+15=25 条，**零新增网络请求**。

### 5. 关键质量发现（影响"能否做观点语料"）

- **`ecb_press` 实测也是标题级源**（15 行 `content == title`，25~90 字符）→ 与 `fed_press` 同类，
  建议改 `source_type=EVENT`（等用户批准）；
- 25 行里**只有 10 行（fred_blog）有真正文**（median 2.5k），且每篇带图 → **观点语料可用量≈10 条**；
- `notes_collect` 混入了源配置 notes（"✅ 冒烟通过…"）→ 记 **TD-34**（待批准后精简）。

### 6. 门禁

- `pytest`：unit + data_quality + leakage + RSS 落库集成 + collector_runner **1751 passed / 1 skipped**（35.07s, 裸跑）；
- `ruff check .` **0 issue**（ruff==0.16.7 已钉）、`mypy` **70 files** 全绿；
- 上一轮用户自跑的全量：**1984 passed / 1 skipped**（121.79s）。

### 7. 暂停点（等人工标注）

- **不自动进入下一轮**；待用户决定：①语料量如何补足（补全文源 / 先按 25 条走）；②`ecb_press` 是否改 EVENT；
  ③是否精简 `notes_collect`；④是否开始人工标注。

---

## 第十一轮（2026-09-13）：四项裁决落地 + **黄金垂类源探测 0/5**

### 1. 用户四项裁决

1. ①语料量 → 补黄金垂类 RSS 源（本轮探测 5 个）；
2. ②`ecb_press` → `source_type=EVENT`（与 `fed_press` 同组，只做宏观事件对照）+ **`enabled=false`**；
3. ③`notes_collect` → 精简，剔除所有配置类内容；
4. ④标注流程 → **暂不启动**，等正文语料 ≥100 条。

### 2. 落地内容

| 项 | 变更 |
|---|---|
| ② 配置 | `ecb_press`：`enabled=false`、`source_type=EVENT`、notes 写明理由（实测 15 行 content==title）；`APPROVED_ENABLED_SOURCES` 同步为 `{fred_blog, fed_press}` |
| ③ 代码 | `_to_row` **不再拼接 `spec.notes`**，本列只留采集侧质量标记；新增回归测试 `test_csv_notes_exclude_config_metadata`（断言配置说明不入 CSV、质量标记仍在）；TD-34 解除 |
| ① 配置 | 新增 5 个黄金垂类候选源（`kitco_news` / `mining_com` / `bullionvault` / `goldseek`，并把 `investing_gold` 换为用户给的新路径 `news_301`），**全部 `enabled=false`** 待冒烟 |

### 3. 黄金垂类探测结果（每源一条命令；临时探测配置 `logs/_probe_gold.json`，正式清单不动）

| 源 | robots.txt | feed HTTP | 条目 | 正文中位数 | 判定 |
|---|---|---|---|---|---|
| `kitco_news` | 允许 | **404** | 0 | — | ❌ 不可用 |
| `mining_com` | **403 → 立即跳过** | 未请求 | 0 | — | ❌ 不可用 |
| `bullionvault` | 允许 | **404** | 0 | — | ❌ 不可用 |
| `goldseek` | **请求失败 → fail-closed** | 未请求 | 0 | — | ❌ 不可用 |
| `investing_gold`（news_301） | **403 → 立即跳过** | 未请求 | 0 | — | ❌ 不可用 |

- **0/5 通过**，且无一走到"正文 ≥200 字符"判定（要么 robots 拒绝，要么 feed 404）；
- 请求纪律：robots 403/失败 → **立即跳过不重试**；feed 非 200 → **立即失败不重试**（无一条触发 429/5xx 重试）；
- 结论：**黄金垂类公开 RSS 普遍对非浏览器 UA 关闭**；唯一有正文的可用源仍是 `fred_blog`（10 条）。

### 4. 门禁

- RSS 测试组（collector + CLI + cache）**60 passed**；`ruff check .` 0 issue；`mypy` 70 files 全绿；
- 本轮真实请求：5 个源各 1 次 robots（其中 3 个还在 robots 允许后各 1 次 feed），**共 8 次**，
  全部 ≥1.5s 间隔、零重试；**未抓任何 HTML 页面**。

### 5. 暂停点（等用户决定 A/B/C/D）

- **(A)** 用户提供已验证的"全文 RSS"（我逐个冒烟）；**(B)** 授权"入口探测"（只读首页找官方 feed 链接）；
  **(C)** 调低验收目标到现有量级；**(D)** 另行评审合规/成本的新闻 API。
  在拿到足够正文语料前，**不启动标注流程**。

---

## 第十二轮（2026-09-13）：B 方案入口探测（0/3）+ NewsAPI 调研（不建议作主源）

### 1. 用户裁决

- 执行 **B**：只探测 3 站首页（kitco.com / gold.org / gold-eagle.com），每站 **1 次请求**、不重试、间隔 ≥1.5s、
  robots 前置（403 立即跳过）；找到 feed 后再单独 1 次冒烟验证"正文 ≥200 字符"；
- **同步执行 D 兜底**：调研 NewsAPI.org 免费额度（用户以为"每月 100 次"）、是否返回正文；
  **只写调研报告，不注册账号、不写代码**；Key 只放 `.env` 的 `NEWSAPI_API_KEY`；
- 现有 10 条语料：**保持不动**并备份到 `D:\backup\`；语料源解决后再统一标注。

### 2. 交付

| 项 | 内容 |
|---|---|
| 新工具 | `src/collectors/rss_discovery.py` + `scripts/discover_rss_feeds.py`（默认 dry-run；robots fail-closed；每站 1 次请求、**绝不重试**；≥1.5s 限速）；`tests/unit/test_rss_discovery.py` **9 项 Mock 测试** |
| 探测结果 | 3 站 robots 均**允许**、首页均 **HTTP 200**、**均未声明任何 RSS/Atom feed（0/3）** → 按规则不再发正文冒烟请求（本轮共 6 次请求，全 ≥1.5s、零重试） |
| 留痕 | `logs/rss_discovery_20260913.json` |
| 备份 | `D:\backup\gold-ai-rss-20260913\`（10 行语料 CSV + 25 行旧版 CSV + collect JSON） |
| NewsAPI 报告 | **`docs/13_NewsAPI接入调研方案.md`**（只读官方文档；未注册/未申请 Key/未写代码） |

### 3. NewsAPI 调研关键结论（纠正用户前提）

- 免费 Developer 计划是 **100 requests / day**（**不是** 100 次/月），且不可加购；
- **仅限开发环境**，官方明确禁止用于 staging/production（含内部）→ 不能作正式语料源；
- **任何套餐都不提供全文**（官方 FAQ 原文），`content` 字段**截断到 200 字符**，`description` 亦为 snippet；
- ⇒ **无法支撑观点抽取**（方向/入场/止损/目标都在正文里）；只能作"事件/摘要层"或"URL 发现层"。

### 4. 门禁

- `pytest`：RSS 测试组（discovery + CLI + collector + cache）**69 passed**；`ruff check .` 0 issue；
  `mypy` **72 source files** 全绿。

### 5. 暂停点（等用户决定）

① 是否批准"页脚 `<a href>` 扫描"扩展（再各 1 次首页请求）；② 是否加一次**对照测试**
（对已知有 feed 的 `fredblog.stlouisfed.org` 跑探测，证明工具有效，2 次请求）；
③ 是否改走"用户提供全文 RSS"或"下调验收目标"；④ 标注流程继续暂停。

---

## 第十三轮（2026-09-13）：语料源分流落地（观点 10 / 事件 19）+ 两个 Excel 数据事故修复

### 1. 用户裁决

- 采用推荐项：把修正后的**真 CSV** 重存为 `logs/real_posts_2026_09_14.csv`（UTF-8 BOM），
  原始 **xlsx 保留在 `D:\backup\`**；工具链只读 CSV；
- `logs/real_posts_annotation_10.csv`（10 条待标注表）**保留不动**；
- 手动快讯 **单独落盘并标 `source_type=EVENT`**，只作事件层输入，**不进观点提取管道**；
- **不改 `docs/11 §1.1` 列契约**；**不进 Phase 3**。

### 2. 事故一：`real_posts_2026_09_14.csv` 实为 Excel 工作簿（xlsx）

- 现象：读取报 `UnicodeDecodeError: byte 0x87`；文件头 `50 4b 03 04`（ZIP）→ 内含 `xl/workbook.xml`；
  即"在 Excel 里另存但保留 `.csv` 文件名"。
- 处置：`logs/_recovered/` 下复制成真 `.xlsx` → openpyxl **无损读出 29 行** → 重存为 UTF-8 BOM 的
  `logs/real_posts_2026_09_14.csv`（**原件转存** `D:\backup\gold-ai-rss-20260913\real_posts_2026_09_14_original.xlsx`）；
- 新脚本 `scripts/import_manual_posts.py` 增加 **ZIP 魔数嗅探 → 按 xlsx 解析**（且必须用 `io.BytesIO`：
  openpyxl 会按**扩展名**拒绝 `looks_like.csv`），并有回归测试锁定。

### 3. 事故二：Excel 往返把 `has_media` 变成 `True/False`（大小写）

- 风险：`sample_annotation_set` 的真值判定只认小写（`_TRUTHY_VALUES`）→ `"True"` 会被静默判成 False；
- 处置：`normalize_rows` 对 `BOOLEAN_COLUMNS`（`has_media`）做 **true/false 小写归一**（+ 单测）；
  重跑后复核：观点语料 `has_media` 取值集合 = `{'true'}` ✔。

### 4. 新增脚本：`scripts/import_manual_posts.py`（双输入分流，504 行）

| 项 | 内容 |
|---|---|
| 分流优先级 | ① `source_type=EVENT` ② `source` 以 `manual-` 开头（手工录入=快讯）③ `content < 200` 字符 ④ 其余 → **观点语料** |
| 输入 | `--corpus`（默认 `logs/real_posts_2026_09_14.csv`）+ `--flash`（默认 `logs/manual_flash_news.csv`，缺失只告警）；CSV(UTF-8/GBK) 与 xlsx 均可 |
| 输出 | `--out-corpus`（`real_posts_opinion_2026_09_14.csv`，13 列 + `source_type`）、`--out-flash`（`event_flash_news.csv`，强制 `source_type=EVENT` + notes 标记）、`--meta-out`（体检元数据） |
| 红线 | **默认 `--dry-run`**；输出==输入 → 硬失败（除非 `--allow-in-place`）；**输入永不被改写**（单测断言字节不变） |
| 校验 | 硬失败（缺列/空正文/时间缺时区/`effective_at < published_at`/未来时间）→ 退出码 2；软告警写 meta |
| 测试 | `tests/unit/test_import_manual_posts.py` **12 项**（分流优先级、布尔归一、别名与保留列、5 类硬失败、去重、GBK、xlsx 伪装、dry-run 不落盘、输入只读、同路径拒绝、退出码） |

### 5. 执行结果（dry-run 与实际分流一致）

```
[import] 读入 29 行（去重 0 条）→ 观点语料 10 / 事件层 19
[import][告警] 观点语料：10 条；来源分布={'fred_blog': 10}；长度 min=2120 median=2588 max=4562
[import][告警] 观点语料：1 条正文 > 4000 字符（抽取器可能截断）
[import][告警] 事件层：19 条；来源分布={'manual-汇通网': 9, 'manual-华尔街见闻': 10}；长度 min=91 median=218 max=459
```

- 复核：`logs/real_posts_opinion_2026_09_14.csv` 10 行（14 列含 `source_type`，`has_media` 全 `true`）；
  `logs/event_flash_news.csv` 19 行（`source_type` 全 `EVENT`）；输入 SHA256 未变 ✔；
- 软告警（留痕待用户处理）：①1 条正文 > 4000 字符（抽取器可能截断）；②事件层 19 行的 `id`/`collected_at`
  为空（§1.1 要求 `id`，但事件层不进观点管道，影响有限）；③其中 1 条事件行 `url` 指向 DeepSeek 会话
  （非来源文章页），可追溯性下降。

### 6. 门禁

`pytest` 新增 12 项全绿；`ruff check .` 0 issue；`mypy` **73 files** 全绿。

### 7. 下一步（等用户确认后执行）

- 正则 vs **LLM（当前定版 `opinion-prompt-v13`）** 各跑一遍 10 条真实语料（v7 的 prompt 源码已不存在，
  仅历史缓存里有 v7 调用记录 → 待用户确认版本口径）；
- 生成《Phase 2 真实语料验收报告》（Mock 200 vs 真实 10 分栏 + N=10 置信度边界）；**不进 Phase 3**。








## 第十四轮（2026-09-14）：开源标注基准对照（regex vs LLM v13，150 条）

### 1. 用户裁决（本轮 5 个确认点）

1. 无方向标签行：**跳过**，不加 `--include-unlabeled`；
2. `information_type`：加 `--information-type empty`，该字段**全量留空**；
3. 中文股吧数据集：**仅作辅助参考**，报告单独分层，不进核心结论；
4. 正则英文边界：接受为**已知工具边界**（不是能力缺陷）；
5. LLM 费用 $0.038 批准，直接跑。

### 2. 用户追加的关键要求（已落地）

- **`information_type` 必须标记 `NOT_EVALUATED`**：金标准全空时，若模型给出 `MACRO` 等值，
  原 `classify_outcome` 会误判 `spurious`（假阳性）→ 新增机制：**金标准为空 + `scoring=NOT_EVALUATED` 时，
  模型给值只记「模型给出了额外信息」，绝不计假阳性、不进任何分母**；
- 报告结构：核心结论**只基于 `stance`**、显式标注 N=100/50 的置信度边界、
  正则 vs LLM 的准确率/召回率/混淆矩阵、典型错误案例（正则误报 / LLM 漏判 / 标注存疑）、
  股吧单独一节且不进主结论。

### 3. 交付

| 文件 | 说明 |
|---|---|
| `scripts/evaluate_extractor.py`（改） | 新增 `NOT_EVALUATED_MARKER` / `OUTCOME_NOT_EVALUATED`；`classify_outcome(..., scoring=)` **最高优先级**；`FieldMetrics.not_evaluated[_with_value]` 与 `scored_cells`；报告新增 §2.1「未评估字段」 |
| `scripts/load_hf_benchmark.py`（改） | `GoldCellRow.scoring` 列；`--information-type empty` 落盘「空值 + `NOT_EVALUATED`」；meta 增加 `not_evaluated_cells` |
| `scripts/report_hf_benchmark.py`（新） | Wilson 95% CI 报告生成器：§0 摘要 → §8 局限 + 附录（复现命令 / 输入 sha256）；股吧单列 §7 |
| `tests/unit/test_report_hf_benchmark.py`（新，23 项） | Wilson 已知值、坏行报错、层过滤、案例启发式、报告必备内容、CLI（dry-run 不写盘 / 缺输入退 2） |
| `tests/unit/test_{extractor_evaluation,load_hf_benchmark}.py`（改，+6 项） | 未评估标记（含**跨脚本字符串契约**守护）+ `--information-type empty` 落盘契约 |
| `docs/10_标注规范.md` | 新增 **§7.1 未评估字段（`scoring=NOT_EVALUATED`）** |
| `docs/11_Phase2真实语料验收指南.md` | 新增 **§1.6 开源标注数据集基准**；§6.3 增补 CI 与比较纪律 |
| `docs/experiments/Phase 2 真实语料验收报告.md` | 本轮交付报告（251 行） |
| `TECH_DEBT.md` | 登记 **TD-35/36/37** + 变更日志一行 |

### 4. 执行结果（严格按 5 步走）

1. `--dry-run --information-type empty`：150 条文本（黄金 100 + 股吧 50）、金标准 750 格（150×5）、
   扫描 200 行跳过 **21** 行无方向标签、**零写盘** ✔；
2. `--no-dry-run`：落盘四件套，契约经 `load_gold` / `load_texts` 读回验证 ✔；
3. `evaluate_extractor.py --extractor regex`：150 条 → **无观点 135（90.0%）**、`UNKNOWN` 率 11.8%、逐格 750；
4. `evaluate_extractor.py --extractor llm`（真实 API）：**150 次调用、失败 0、解析失败 0**，
   输入 593,875 tokens（缓存命中 557,184）+ 输出 8,896 → **$0.0250**（峰值价；低谷价 $0.0125）；
5. `report_hf_benchmark.py` → 《Phase 2 真实语料验收报告.md》（251 行，含 Wilson 区间与逐例错误）。

### 5. 关键发现（诚实结论）

- 黄金层 `stance`：**正则 2/100（2.0%，95% CI 0.6%~7.0%）**、**LLM 0/100（0.0%，95% CI 0.0%~3.7%）**，
  两者 CI 重叠 → **差异不显著**；≥90% 门槛仅作参考（任务不同）；
- **LLM 的 90 格提取错误 100% 是 `UNKNOWN` 方向拒答，无一格判反方向** → 它把"金价下跌 0.9%"
  视为**事实描述**而非**可交易观点**（符合 `docs/10 §4.9.0` 与"只降不猜"）→ 登记 **TD-37 待一句话裁决**；
- 正则**无观点 90%**、零假阳性 → 「零假阳性」是"几乎不作为"的结果，**不是精确性证据**（报告已写明）；
- 中文股吧层（N=50，非黄金、标签口径冲突）：正则 6/50、LLM 7/50 —— 仅辅助参考；
- **已明确写入报告**：本报告是工程对照，**不是** `docs/08 §5` 验收达标结论（登记 **TD-35**）。

### 6. 门禁

`pytest` 全套 **2261 passed / 1 skipped**（本轮 +45 项）；`ruff check .` 0 issue；
`ruff format --check` 通过；`mypy` 全绿；LLM 台账与缓存均落在 `logs/`（gitignored，**未入库**）。

### 7. 暂停点（**等用户确认，绝不进入 Phase 3**）

- 待用户裁决：**TD-37**（描述型新闻标题是否要给方向）；
- 若要真实语料验收，仍需**人工裁决语料**（`docs/11 §3` 流程）；
- 本轮结论只作工程对照，`docs/05` 的 Phase 2 验收结论保持未勾选。

## 第十五轮（2026-09-14）：Phase 2 正式收官

### 1. 用户裁决（本轮）

1. **TD-37 结案：不改 Prompt** —— 维持 `opinion-prompt-v13` 冻结。
   理由（用户原话）：不要为了让 LLM 在标题级数据集上得高分，去教它把"金价下跌 0.9%"这类
   描述性陈述标成 `SHORT`，**那会污染模型定义**，让它未来在真实博主观点提取时把新闻当预测；
2. **报告定位调整**：新增《免责声明与适用范围》节 —— ①标题级方向分类 ≠ 观点提取任务（本质差异）；
   ②LLM 的 `UNKNOWN` 是**合规行为**（拒绝把描述当观点），不是错误；③本基准**能证明**「正则英文语料失效 +
   LLM 能识别无观点」、**不能证明** LLM 的观点提取能力；④真正的观点提取验证放到 **Phase 3**；
3. **真正的观点提取验证移交 Phase 3**：用 19 条中文快讯或抓取真实博主帖子；标题级情感分类留 Phase 3 的 News Alpha；
4. **Phase 2 正式收官**：归档 + `PROGRESS_LOG` + `docs/05`；
5. **TD-35/36/37 全部按「已知边界」归档、不做代码修补**（本质是任务定义差异，不是代码缺陷）。

### 2. 交付

| 项 | 内容 |
|---|---|
| 报告改造（生成器内） | `scripts/report_hf_benchmark.py` 新增 `## 免责声明与适用范围` 节（四条裁决）+ §8 下一步改写（TD-35/36/37 已归档不修补）；`no_opinion_posts(run, layer=)` 支持按层统计（免责声明用**黄金层**数字） |
| 报告再生成 | `docs/experiments/Phase 2 真实语料验收报告.md`（**258 行**，可复现） |
| 归档 | `logs/archive/phase2_hf_benchmark/`：`hf_benchmark_{texts,gold,meta,eval_regex,eval_llm,eval_llm_usage}` + 报告副本 + **`MANIFEST.md`**（语料 / 结果 / 四条裁决 / 复现命令 / sha256 前 16 位 / gitignore 提示），共 8 个文件 |
| `TECH_DEBT.md` | TD-35/36/37 → **✅ 已结案（按已知边界归档）**，TD-37 处置改写为用户裁决原文；§6 变更日志新增「Phase 2 收官」行 |
| `docs/05_分阶段开发路线图.md` | 新增 **`## Phase 2 收官（2026-09-14，已通过）`** 表；真实语料验收 4 行状态更新（收集 ✅ / 导入脚本 ✅ / 人工标注 ⚠️未执行 / 报告 ✅）；「进入 Phase 3 ⛔ 禁止」行改为 **Phase 2 状态 ✅ 已通过**；验收章节新增「执行情况」；版本表 `V0.4 ✅ 已完成` |
| `PROGRESS_LOG.md` | 本第十五轮记录 |

### 3. 门禁

`pytest`（新增免责声明断言后）**24 项报告测试全绿**；`ruff check` / `ruff format --check` / `mypy` 全绿；
报告已由生成器重新产出（非手工编辑），归档文件与 `docs/experiments` 正本一致。

### 4. 暂停点

**Phase 2 状态：✅ 已通过（2026-09-14）**；**Phase 3 等用户人工审核报告通过后启动**。
本轮至此**暂停**，未写任何 Phase 3 代码（`.clinerules` 第 2 条）。

## 第十六轮（2026-09-14）：Phase 3 执行规划草案（**未开工，零代码**）

### 1. 用户指令

先不写任何代码，输出《Phase 3 执行规划》：①依赖盘点（对照 `docs/01 §7–8`，标注已落地/需新增）②里程碑 3.1–3.4 与验收标准
③关键技术选型（Regime 方法、Alpha 模型、独立验证）④风险与未决问题（红线、需用户提供的资源）⑤只输出规划。

### 2. 交付

| 项 | 内容 |
|---|---|
| `docs/14_Phase3_执行规划.md`（**新建**） | §1 依赖盘点（含 Phase 1–2 真实落地状态表：✅已落地 / 🟡表在无数据 / 📐仅设计 / ❌缺失；逐模块输入清单；**G1–G5 数据缺口**；迁移 0006–0009 规划）→ §2 里程碑（3.0 数据底座 + 3.1 Regime + 3.2 Technical/Macro + 3.3 Author/News + 3.4 Ensemble 基础层，**每阶段给可判定验收标准**）→ §3 技术选型（Regime 规则/统计/HMM 三方案对比、LR→GBDT、验证协议含切分/embargo/walk-forward/IC·ICIR/Wilson CI/Brier、**标签口径防泄漏**）→ §4 风险（R1–R9 红线逐条对策、C1–C5 文档口径冲突、A1–A7 需用户提供资源、工程风险）→ §5 交付与门禁 → §6 **待裁决 D1–D7** → 附录 A 不做清单 + 附录 B 依据文件 |
| `docs/05_分阶段开发路线图.md` | Phase 3 章节顶部加一行指向 `docs/14`，并标记"**未开工**，需先裁决 D1–D7" |

### 3. 规划中的关键发现（供用户决策）

- **真正卡点是数据量而非模型**：`market_bars` **仅 10 行**、`news_events`/`macro_events` **各 2 行**、`author_opinions` **0 行**
  → 任何 Alpha 都无法做 OOS；Phase 3 必须先做**数据底座**（G1–G4）；
- **新发现的前视风险（R3）**：`macro_events` 当前只记录**观测期**日期，未记录**发布时刻** →
  Macro Alpha 若不补 `released_at`（ALFRED vintage）会天然泄漏未来信息；
- **口径冲突需裁决（C1）**：`docs/01 §10`/`docs/07`/`docs/08 §10` 把 Meta Ensemble + 动态权重放在 **Phase 6**，
  而本轮里程碑列为 **3.4** → 建议 3.4 只做「基础接口 + 离线 Ablation」，动态权重留 Phase 6；
- **依赖待批（A3）**：`lightgbm`/`xgboost`/`hmmlearn` 均未安装；不批则全部用 sklearn（LR + `HistGradientBoosting`）；
- **Dashboard 能力缺口（C2）**：`docs/05` 要求 Regime 面板/Prediction Dashboard，但项目无 web 框架 →
  建议用静态报告替代（零新依赖），或由用户批准 FastAPI/Streamlit。

### 4. 门禁与暂停点

**本轮未写任何代码、未新增依赖、未改数据库**；`pytest` 仍为 **2264 passed / 1 skipped**（未触碰代码）。
**等用户审阅 `docs/14` 并裁决 D1–D7 后**，再决定开工子阶段；Phase 3 **尚未开始**。

## 第十七轮（2026-09-15）：Phase 3.0 W0-1 代码交付（**未执行真实回填**）

### 1. 用户裁决（本轮）

1. **跳过 D2**（10 年日线复核）：日线 10 年也只有 ~2500 根、OOS 窗口仍不足，且已证明与简单基准重叠
   → **不再在技术面 Alpha 上投入资源**；
2. **进入 Phase 3.0 数据底座**，按 W0-1 → W0-5 执行（每个工作包单独验收、单独更新本文件）；
3. **探针结果归档**到 `logs/archive/phase3_probe_technical_ic/`（含 `MANIFEST.md`：四条裁决 + sha256 + 复现命令）；
4. **教训**：以后任何 Alpha 探针先做小样本验证，再决定是否全量投入 → 已写入 `docs/14 §7`。

### 2. 交付（W0-1 代码，**未运行真实回填**）

| 文件 | 说明 |
|---|---|
| `scripts/_market_data.py`（新） | W0-1 共享工具：**保留空槽**的 provider 取数（UA + 候选链）、**数据驱动会话日历**、缺口四分类、TD-03 4h 聚合 |
| `scripts/audit_market_gaps.py`（新） | 缺口审计 CLI：把 3059 个空 bar 拆成「常规休市 / 疑似假期 / **数据缺失** / **时间戳缺失**」；默认 `--dry-run` |
| `scripts/backfill_market_bars.py`（新） | 行情回填 CLI：**走既有 `MarketCollector` + `run_collector`**（幂等/raw 留档/防泄漏语义全复用）；`--with-4h` 走 TD-03 聚合；默认 `--dry-run` |
| `tests/unit/test_market_data_shared.py`（新） | **25 项**：取数/候选链/会话标定/四分类/4h 聚合/CLI 零落盘 |
| `tests/integration/test_market_w0_1_backfill.py`（新） | **4 项**：`preflight`、4h 幂等与半桶丢弃、回填 CLI dry-run/no-dry-run |
| `scripts/_console.py`（改） | `safe_print` 上移共享（探针与 W0-1 两个脚本共用）；`probe_ic.py` 改为引用它 |

**实测固化的 provider 映射**（写进代码注释，避免反复踩）：`XAUUSD → GC=F`、`DXY → DX-Y.NYB`、
`USDCNY → CNY=X`、`US10Y → ^TNX`；**`US10Y_REAL` Yahoo 无序列**（`DFII10` → 404）→ 由 W0-2 的 FRED 提供。

### 3. 本轮修掉的 3 个真实缺陷（均有测试守护）

| # | 缺陷 | 现象 | 修复 |
|---|---|---|---|
| 1 | `ensure_utc_from_database` 缺失 | SQLite 读回 naive `open_time` 与 aware `now` 比较 → `TypeError` | 在 DB 读回边界统一归一（`hourly_bars`） |
| 2 | 同上根因导致**幂等去重失效** | 4h 复跑时 naive vs aware 集合不匹配 → 重复插入 → **撞 UNIQUE 约束** | 已存在 `open_time` 集合同样归一 |
| 3 | `safe_print` 未导入 | 审计脚本 `--dry-run` 打印报告会 `NameError` | 统一从 `scripts/_console` 导入（并上移共享） |

### 4. 门禁与状态

`pytest` 全套 **2337 passed / 1 skipped**（本轮 +30）；`ruff check .` 0 issue；`mypy` **79 files** 全绿。

**当前阻塞（等你确认后再处理）**：`database/gold_ai.db` **尚未创建**（`db_exists=False`）→
真实回填前需先执行 `alembic upgrade head` + `python -m database.seeds --scope instruments,sources`；
脚本已做**预检硬失败**并直接打印该修复指引（不会静默建库或半边写入）。

### 5. 暂停点

W0-1 代码已交付，**未执行真实回填**（按你的要求等确认）。下一步：确认代码后跑 W0-1 实跑
（审计 + 回填 + 4h），再进 W0-2（宏观 + `released_at`，R3，P0）。

## 第十八轮（2026-09-15）：W0-1 真实回填落地（审计 → 回填 → 幂等；**修复 4 处生产缺陷**）

### 1. 执行顺序（用户批准的 4 步，全部完成）

| 步骤 | 命令 | 结果 |
|---|---|---|
| ① 建库 + 种子 | `alembic upgrade head` → `python -m database.seeds --scope instruments` → `--scope sources` | 迁移 0001→0005；标的 **11**、源 **9**（含 `market_yahoo`）|
| ② 缺口审计（dry-run） | `python scripts/audit_market_gaps.py --symbols XAUUSD --timeframes 1h,1d --dry-run` | 零落盘；结论见 §3 |
| ③ 真实回填 | `python scripts/backfill_market_bars.py --symbols XAUUSD,DXY,USDCNY --timeframes 1d,1h --no-dry-run --with-4h --report docs/experiments/Phase3_W0_1_行情回填报告.md` | 6/6 SUCCESS |
| ④ 幂等复跑（×3） | 同上命令 | **6/6 `inserted=0`；4h 3/3 `新插入=0`** |

⚠️ `--scope` **不接受逗号列表**（实测报错）→ 脚本预检提示已改为两条可直接执行的命令。

### 2. 落库结果（canonical，清理后）

| 标的 | 1d | 1h | 4h（派生，TD-03） |
|---|---|---|---|
| `XAUUSD` | 2509 | 11317 | 2445 |
| `DXY` | 2510 | 11744 | 2831 |
| `USDCNY` | 2543 | 9575 | 1545 |
| **合计** | **7562** | **32636** | **6821**（≈ **47019** 根 bar）|

`raw_items` 留档 **40174** 条（provider 原始 JSON，可追溯）。4h 半桶丢弃 620/239/1444。

### 3. 缺口审计（**DST 修正后**；XAUUSD 为②的交付结论）

| 标的 | 周期 | 槽数 | 有效 | 空槽 | 常规休市 | 疑似假期 | **数据缺失** | **时间戳缺失** |
|---|---|---|---|---|---|---|---|---|
| `XAUUSD` | 1h | 14506 | 11446 | 3060 | **2955** | 0 | **105** | **136** |
| `XAUUSD` | 1d | 2516 | 2512 | 4 | 0 | **4** | **0** | **0** |
| `DXY` | 1h | 14506 | 11953 | 2553 | 2454 | 0 | **99** | **136** |
| `DXY` | 1d | 3038 | 2513 | 525 | 522 | **3** | **0** | **0** |
| `USDCNY` | 1h | 12510 | 9664 | 2846 | 0 | 0 | **2846**（⚠方法退化）| **31** |
| `USDCNY` | 1d | 2610 | 2601 | 9 | 0 | **8** | **1** | **0** |

**关键修正**：初版按 `(UTC 星期, UTC 小时)` 分桶 → DST 造成 1d **954 条假阳性**、1h 602 条虚高。
改为**按交易所本地时区分桶 + 本地日期核算**（CME = 美东）后：XAUUSD 空槽 **96.6% 是常规休市**，
真正的异常只剩 **242 槽（1.67%）**。FX（`CNY=X`）会话不规则 → 其空槽无法按本地时段桶归因（已写入报告 §6.1）。

### 4. 本轮修复的生产缺陷（全部带回归测试）

1. **DB 读回时间戳无时区**（SQLite 不回 tz）→ 比较抛 `TypeError` + 4h 幂等失效（UNIQUE 冲突）→ `ensure_utc_from_database`；
2. **采集器不认识 provider ticker**：项目标的直接进 URL → **全量 HTTP 403**（Phase 1 走 MockTransport，真实链路从未验证）→ 新增 `sources.config_json['provider_symbols']`（`XAUUSD→GC=F` 等）；
3. **Yahoo 拒绝 aiohttp**（同参 httpx 200）→ 新增协议兼容 `HttpxTransport`（`scripts/_market_data.py`，`--transport httpx` 默认）；
4. **provider “进行中”bar 污染库**（时间戳=抓取墙钟，每轮新增 1 根/标的，无界增长）→ `parse_yahoo_chart(not_after=…)` **拒收未收盘 bar**；
   + 另修 2 处：`safe_print` 未导入（`NameError`）、种子命令提示有误。

**清理留痕**：删除 9 根墙钟垃圾行（判据 `秒≠0`）+ 8 根未收盘 bar（判据 `close_time > collected_at`），
证据 CSV 归档于 `logs/archive/phase3_w0_1_backfill/`；**提前收盘的 30 分钟 bar（如 2025-12-24 `17:30`）判为合法，未误删**。

### 5. 归档与文档

- `logs/archive/phase3_w0_1_backfill/`：审计日志、A/B/C/D/E/F 各轮日志、outcomes CSV、meta JSON、2 份删除证据 CSV + MANIFEST（sha256 + 复现命令）；
- `docs/experiments/Phase3_W0_1_行情回填报告.md`（§6 实测约束与已修缺陷**随脚本固化**，不会因再生成而丢失）。

### 6. 门禁与状态

`ruff check .` 0 issue；`mypy` 79 files 全绿；`pytest` **2345 passed / 1 skipped**（本轮 +8；新增用例：`not_after` 未收盘拒收 ×3、`HttpxTransport` ×2、`provider_symbols` 映射 ×1、其余为断言强化）。
**US10Y_REAL 保持标记为“交 W0-2 的 FRED”**（未静默跳过）。

### 7. 暂停点

**不进入 W0-2**，等用户审阅 W0-1 回填结果后再决定。

## 第十九轮（2026-09-17）：W0-1 验收固化与 Phase 3 启动门禁

### 1. 用户授权与范围

- 用户批准按既定建议推进 Phase 3，并授权将已有未提交的 W0-1 回填、缺口审计与 IC 探针作为可维护基线；
- 本轮只复核并固化 **W0-1**，不实现 Regime、特征、Alpha、Prediction、Strategy、Risk 或订单功能；
- 不执行外部网络请求、不执行真实回填写库；`LIVE_TRADING=false` 与
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 经配置对象复核均保持 false。

### 2. W0-1 验收结论：PARTIAL（受限数据底座）

1. 质量门禁：W0-1 相关测试 **119 passed**；历史全量门禁 **2345 passed / 1 skipped**；
   `pytest -m leakage` **20 passed**；`ruff` / `mypy` 通过；迁移 head 仍为 Phase 2 的 `0005`，
   未错误提前创建 Alpha schema。
2. 时间与数据完整性：未收盘 K 线拒收、4h 只聚合完整桶、半桶丢弃、回填 append-only 且连续复跑
   `inserted=0`。这些结论仅覆盖 W0-1 数据管道，不等于 Alpha 有效。
3. 强制限制：项目 `XAUUSD` 当前请求的是 `GC=F`（COMEX 连续期货），所以任何研究输出必须标注
   期货代理，不能称为现货 XAUUSD 结论；XAUUSD/DXY 1h 存在异常缺口，USDCNY 1h 会话审计退化，
   后续标签/特征必须丢弃含缺口 horizon，不插值或缩短窗口。
4. TD-03：4h 功能已交付，但未写 `processed_items` / `data_versions` 的 Processor 血缘，保持部分解除。

### 3. 下一工作包（唯一允许）

进入 **W0-2：宏观回填 + `released_at` / vintage**。每条宏观数据必须能证明在 `released_at` 后才可用；
缺真实发布时间的记录必须拒绝进入 Macro Alpha，并须配套“发布前不可取到”的 leakage 注入测试。

## 第二十轮（2026-09-17）：W0-2 宏观 vintage 时间语义（工程契约完成，真实回填阻塞）

### 1. 官方语义核对与设计

- FRED `date` 是观测期，不是发布时间；ALFRED `realtime_start/realtime_end` 表示值在历史上的已知区间；
- `series/vintagedates` 是整个序列发生新增或修订的日期集合，不能脱离 observation 冒充单条发布时间；
- 因 API 只有日期精度，本项目保守使用 `realtime_start 次日 00:00 UTC` 作为 `released_at`，
  `realtime_end 次日 00:00 UTC` 作为排他结束边界；`9999-12-31` 映射为 NULL；
- `event_at` 重新明确为 observation period，严禁用于代表发布时间。

### 2. 交付

- migration `0006_macro_event_vintages`：增加 `released_at` / `vintage_end_at`、release 时间 CHECK、
  vintage 唯一键和查询索引；旧行用既有 `effective_at` 作保守 release，不提前可见；
- `MacroCollector` 改为请求完整 realtime period，raw 幂等键包含 release，CSV 缺 release 硬失败；
- `src/processors/macro_vintages.py`：as-of 查询只返回当时真实可见的最新 vintage；
- `macro_events` 加入 append-only ORM 保护；FRED 序列增加 DFII10/DGS10/DTWEXBGS/FEDFUNDS；
- 新增 leakage 测试：发布前不可见、修订边界切换、非法时间窗拒绝、effective 早于 release 拒绝、
  历史 vintage 不可覆盖。

### 3. 数据库与阻塞

- 正式本地 SQLite 库升级前备份：`logs/archive/phase3_w0_2/gold_ai_pre_0006.db`，
  sha256=`75c95c6e256ca1807fd9e26f05413656d2fc7eddc64532674f6f9c516d12d6b5`；
- 备份大小 69,611,520 bytes；升级前 `macro_events=0`；0005 → 0006 成功；
- 当前环境 `FRED_API_KEY_CONFIGURED=False`，故未执行真实 API 冒烟与回填，W0-2 状态为 **PARTIAL**；
  不进入 W0-3，不开发 Macro Alpha。

### 4. 恢复后真实联调与 W0-2 收官（2026-09-17）

- 修复 `.env` 接线：FRED Key 纳入 `Settings.fred_api_key: SecretStr`，MacroCollector 不再散读
  `os.environ`；实测配置成功、repr 不含明文、Git 跟踪文件无 Key；
- 真实探针发现两项 provider 约束：完整 realtime 区间有 5,125 vintage dates，超过 JSON 上限 2,000；
  `output_type=1` 的 realtime 边界会被查询窗口裁剪。最终改用官方 `output_type=4`
  （Initial Release Only），按 ≤365 天分块；
- DFF 低量冒烟：SUCCESS、6 条、0 重试、时间约束通过、Key 泄漏 0；
- 新增 `scripts/backfill_macro_vintages.py`：默认 dry-run、8 序列白名单、年度分块、显式
  `--no-dry-run` 才联网写库；首轮 88 请求插入 11,674 条（加冒烟共 11,680）；
- DTWEXBGS 的 2016–2018 三窗口由官方返回“does not exist in ALFRED”，系统保持为空并留告警，
  禁止退回今天的修订终值；修复后第二轮 **inserted=0 / duplicate=11,680 / failed_chunks=0**；
- 最终分布：CPIAUCSL 128、PCEPI 128、PAYEMS 129、DFF 3,912、DFII10 2,676、
  DGS10 2,676、DTWEXBGS 1,902、FEDFUNDS 129；时间违规 0、重复键 0、Key 泄漏 0；
- **W0-2 PASS（Initial Release Only）**。完整修订链未交付，后续 Macro Alpha 只允许使用初值口径。
- 自动启动 W0-3：RSS 目标测试 56 passed；本地 fixture dry-run 因缺
  `logs/rss_fixtures/{fred_blog,fed_press}.xml` 返回 0 条，进入缓存/fixture 补全审计，未伪装通过。

## 第二十一轮（2026-09-17）：W0-3 RSS 新闻回填落库与幂等验收

### 1. 续点与缺口修复

- 复核确认 RSS 解析、缓存、robots fail-closed 已具备，但 `scripts/collect_rss.py --to-db` 仍是明确占位；
- 接通正式落库路径：逐源执行 `run_collector`，写 `raw_items + news_events + collector_runs`；
- `RssCollector` 与既有 `NewsCollector` 对齐：跨来源正文哈希去重；只有发布时间明确的条目才生成 `news_events`，时间不明只保留原始层；
- 新增 `fred_blog` 来源种子及 `source_ids` 调度过滤，避免一个数据库来源错误承载整份 RSS 清单；
- 新增默认 90 天窗口、来源分布统计和单源占比 >40% 自动告警。

### 2. 零网络演练与正式落库

- 复用 2026-09-13 已完成 robots/HTTP 验证的缓存，`--dry-run --cache-mode readonly` 解析 **30 条**：`fred_blog=10`、`fed_press=20`；
- 首轮正式落库：两个源均 `SUCCESS`，**inserted=30 / failed=0**；
- 幂等复跑：**inserted=0 / duplicate=30**；
- 数据库核验：RSS `raw_items(NEWS)=30`、`news_events(parser_version=rss-feedparser-v1)=30`、时间违规 0；
- 来源分布 33.3% / 66.7%，`fed_press` 的 **66.7% >40%** 告警按验收要求触发。

### 3. 测试与状态

- RSS/种子目标集：**118 passed**；新增 CLI 真实驱动集成用例覆盖 `raw_items + news_events + collector_runs` 三层落库；
- W0-3 状态：**PASS（受限）**。限制是 RSS 当前快照只有 30 条且来源偏斜，不得描述为完整 90 天历史或可直接训练的 News Alpha 数据集；
- 详细报告：`docs/experiments/Phase3_W0_3_新闻回填报告.md`；
- 下一唯一工作包：**W0-4 作者观点链**，不提前进入 Regime / Alpha / Prediction / Strategy / Risk。

## 第二十二轮（2026-09-17）：W0-4 真实作者观点链与前瞻收益标签

### 1. 正式入库（方案 B）

- 新增 `manual-汇通网` / `manual-华尔街见闻` 两个本地导入来源，默认禁用自动采集；
- `scripts/import_real_posts.py` 将已验收 19 条真实中文快讯写入 `raw_items(POST) → author_posts`，同时幂等建立 2 位作者与 2 个账号；
- 首轮 inserted=19，复跑 inserted=0 / duplicate=19；Mock 200 条未进入真实研究库；
- 输入缺 `collected_at` 时沿用已审计 `effective_at`，逐条写 `collected_at_provenance=input_effective_at_fallback`，不隐藏时间来源限制。

### 2. 观点管道

- 新增 `scripts/run_opinion_pipeline.py`（默认 dry-run）；
- `mock-regex-v1` 首轮处理 19 帖：无观点 14、观点 11、失败 0；帖子分布为 14×0、3×1、2×4；
- 二轮扫描 0，`processed_items` 与 `author_opinions` 幂等生效。

### 3. 前瞻收益标签

- 新增 `src/processors/opinion_labels.py` 与 `scripts/build_opinion_labels.py`；
- entry 严格取 `open_time > effective_at` 的下一根同 horizon bar；等时 bar 注入测试必须被排除；
- 缺 horizon 不猜作者意图，按 1h/4h/1d 评价网格展开并写 `horizon_source=evaluation_grid`；
- 31 个评价行：`LABELED=12`、`UNKNOWN_STANCE=16`、`MISSING_INSTRUMENT=3`；
- 产物固定写明 `GC=F` / `COMEX_CONTINUOUS_FUTURES`，缺口不插值、不缩窗。

### 4. 状态

- W0-4 **PASS（受限工程链）**；由于每位作者有效样本远低于 30 条，禁止生成权重或宣称 Alpha；
- 报告：`docs/experiments/Phase3_W0_4_作者观点链报告.md`；
- 下一唯一工作包：**W0-5 特征底座**。

## 第二十三轮（2026-09-17）：W0-5 前置冻结、可信度门禁与规划收口

### 1. 现场冻结与恢复点

- 暂停 W0-5 开发，先对 W0-1～W0-4 做只读盘点；
- 本地研究库在 `database/backups/gold_ai_pre_w0_5_20260917.db` 建立同字节快照；
- 原库与快照 SHA-256 均为
  `486A7B2F75C7AA5F06A4A526B49D02FB1354CDEA52C2A359D1B61CB5462F7B7A`；
- Alembic current / head 均为 `0006_macro_event_vintages`；
- 未跟踪文件 `disabled` 经检查确认为 W0-3 只读 RSS 摘要，不含疑似密钥，已归档到
  `logs/archive/phase3_w0_3_backfill/rss_readonly_summary.json`。

### 2. 标签可信度门禁

- 标签版本升级为 `forward-return-v2`；
- `collected_at_provenance=input_effective_at_fallback` 的历史帖子统一返回
  `UNTRUSTED_COLLECTION_TIME`，不再查询行情或产生收益标签；
- 增加入场延迟门禁：`entry_at - effective_at` 超过被评价 horizon 时返回
  `ENTRY_LAG_EXCEEDED`，避免周末 1h 观点在数十小时后仍被当作 1h 信号；
- 当前库 dry-run 结果为 31 行全部 `UNTRUSTED_COLLECTION_TIME`，正式研究可用标签为 0；
- 新增两个 leakage 测试覆盖回填采集时间和周末超长入场延迟。

### 3. 规划与技术债校正

- `0006` 已由宏观 vintage 使用，Phase 3 后续迁移整体顺延为 `0007`～`0010`；
- 修正 TD-01 / TD-03 / TD-15 / TD-22 的过期描述；
- TD-22 保持 P0 待用户确认：工作区已无旧明文密钥文件，但供应商侧是否完成轮换无法由代码证明；
- PostgreSQL 端到端写路径仍未验证，当前环境没有配置可用连接，TD-02 继续阻塞 W0-5。

### 4. 全量门禁

- `ruff check .`：PASS；
- `mypy config database src scripts`：PASS（85 个源文件）；
- 首轮 `pytest -q`：2379 passed / 1 skipped；补齐 4h 血缘后的最终全量回归：
  **2513 passed / 1 skipped**；
- 待提交文件敏感信息扫描只命中测试假密钥和本地示例连接串，未发现新的真实凭据。

### 5. 4h 派生血缘收口

- `aggregate_and_insert_4h` 在写入后对当前完整 4h 快照计算稳定 SHA-256，并按数据哈希幂等写入
  `data_versions`；记录 1h→4h、`processor_version=4h-v1`、UTC 满桶规则、范围与行数；
- `data_versions` 加入 ORM 不可覆盖守卫，数据变化只能追加新版本；
- 当前库新增 3 个版本：XAUUSD 2,445 行、DXY 2,831 行、USDCNY 1,545 行；复跑未新增 K 线；
- 收口后数据库快照：`database/backups/gold_ai_w0_preflight_closed_20260917.db`，与当前库
  SHA-256 均为 `518AD17599DEEB693FC490B00EBB31C07FEA8B3ED131F9B7E3E05A419CB18B46`。

### 6. PostgreSQL 16 本地端到端验收

- 安装 PostgreSQL 16.15；服务 `postgresql-x64-16` 正在运行并设为自动启动；
- 创建项目专用数据库/角色 `gold_ai` / `gold_ai_app`，角色默认时区 UTC；随机密码只存在本地
  `.env` 与 Windows 当前用户加密的 `.postgres-local/`，二者均被 Git 忽略；
- `0001→0006` 迁移成功；`scripts/check_pg_schema.py` 的 15 表原生类型和种子幂等检查 PASS；
- PostgreSQL 连续两轮三源 Mock 采集全部 SUCCESS，幂等与时间约束通过，不变式违规 0；
- 首轮验证发现控制台打印未脱敏 URL：立即轮换项目角色密码、使旧值失效，修复为 `_mask_url`，
  新增回归测试后复跑确认只输出 `***`；
- TD-02 正式解除。完整报告保存在本地忽略文件 `logs/postgres_phase1_pipeline_report.md`。
- PostgreSQL 配置生效后的最终全量门禁：`ruff` PASS、`mypy` 85 文件 PASS、
  `pytest` **2647 passed / 1 skipped**。
- 边界：当前 PostgreSQL 只承载种子与 Mock 端到端验收数据；W0-1～W0-4 的真实研究事实仍在
  冻结的 SQLite 库中。迁移/重建与逐表对账另列 TD-40，完成前不启动 W0-5。

## 第二十四轮（2026-09-18）：W0 真实研究数据迁入 PostgreSQL

### 1. 安全迁移工具

- 新增 `scripts/migrate_sqlite_to_postgres.py`：默认 dry-run，只允许
  `postgresql+psycopg://` 目标；目标 19 张业务表必须全部为空；
- 整体写入置于单一事务，任何外键、行数或内容指纹不一致都会全量回滚；
- SQLite naive datetime 按项目既有契约恢复为 UTC；`raw_items.superseded_by_id` 用两阶段写入
  保持自引用外键；批量写入后逐表计算规范化 SHA-256；
- 新增集成测试覆盖逐表哈希、自引用恢复和非空目标拒绝。

### 2. 独立研究库与迁移结果

- 保留原 `gold_ai` 验收库不动，新建 `gold_ai_research`；执行迁移 `0001→0006`；
- 源：`database/backups/gold_ai_w0_preflight_closed_20260917.db`，SHA-256
  `518AD17599DEEB693FC490B00EBB31C07FEA8B3ED131F9B7E3E05A419CB18B46`；
- 迁移 **19 表 / 110,992 行**，逐表源/目标行数和内容 SHA-256 全部一致；
- PostgreSQL 原生备份：`.postgres-local/gold_ai_research_pre_w0_5.dump`，SHA-256
  `37A98A0299AE45E78117F35B343C3CFB14A88D5DE4FD0A9E708D71166E3A4EBF`；
- 本地 `.env` 已切换到 `gold_ai_research`；Alembic current 为 `0006_macro_event_vintages`；
- 切换后只读复核：`raw_items=51,941`、`market_bars=47,019`、`macro_events=11,680`、
  `news_events=30`、`author_opinions=11`、`data_versions=3`；31 条观点仍全部被
  `UNTRUSTED_COLLECTION_TIME` 门禁隔离。

### 3. 状态

- TD-40 已解除；完整逐表指纹见 `docs/experiments/Phase3_W0_PostgreSQL迁移报告.md`；
- 迁移后最终门禁：`ruff` PASS、`mypy` 86 文件 PASS、`pytest` **2649 passed / 1 skipped**；
- W0-5 仍保持暂停，等待用户确认历史 DeepSeek / 数据库凭据已轮换。

## 第二十五轮（2026-09-18）：历史凭据轮换确认与 W0-5 前置放行

- 用户确认旧 DeepSeek / 历史数据库凭据均已在供应商侧轮换失效；TD-22 正式解除；
- 本地 PostgreSQL 项目角色密码此前已单独轮换，连接输出脱敏回归测试已通过；
- W0-5 的代码检查点、PostgreSQL、真实数据迁移、防泄漏契约、数据版本与安全门禁全部关闭；
- `docs/experiments/Phase3_W0_前置收口报告.md` 状态改为 **PASS**；
- 本轮仅记录放行，不启动 W0-5 开发。作者时间来源、`GC=F` 代理、新闻/作者样本量仍是后续
  Alpha 验收限制，不能被描述为已解决。

## 第二十六轮（2026-09-18）：W0-5 特征底座落地与真实库回放验收

- 新增 migration `0007_phase3_feature_tables` 与四张 append-only 表：`feature_sets`、
  `feature_snapshots`、`feature_values`、`market_regimes`；补齐字段文档、枚举、唯一键、外键、
  值域和时间因果 CHECK；`market_regimes` 本轮保持 0 行，不提前进入 3.1；
- 新增 `market-core 1.0.0` 最小生成器：行情必须同时满足 `close_time/effective_at <= as_of`，
  只取最新连续 5 根 4h K 线，遇缺口拒绝且不插值；快照绑定保存输入行 ID 的 DataVersion 和
  SHA-256；相同身份幂等复用；
- 新增 leakage 测试覆盖未来生效行排除、缺口拒绝、DB 层 `max_effective_at <= as_of`、
  append-only 与冻结输入逐值回放；
- 真实 PostgreSQL 首次迁移发现 revision 名超过 Alembic `VARCHAR(32)` 上限，事务自动回滚、
  库仍完整停在 0006；缩短为 `0007_phase3_feature_tables` 并加入 revision 长度回归测试后成功；
- 真实库写入 1 份 XAUUSD 4h 快照（6 个值）；相同 `as_of` 复跑 `inserted=false`，回放 PASS，
  时间违规 0；PG 23 表原生类型检查 PASS；
- 为避免 W0 冻结 SQLite 快照被新增 Phase 3 表反向破坏，迁移工具明确固定迁移 Phase 1+2 的
  19 张历史表，新表继续由 Alembic 在 PostgreSQL 创建为空表；
- 专项 71 passed、leakage/不可变性 48 passed；全量 **2530 passed / 1 skipped**；
  `ruff check .` 与 `mypy`（90 files）通过；W0-5 新文件格式检查通过，全仓 `format --check`
  仍是 TD-33 已登记的历史格式债（81 个旧文件），本工作包未混入全仓纯格式改写；
- W0-5 **PASS（底座范围）**。详细报告：`docs/experiments/Phase3_W0_5_特征底座报告.md`。

## 第二十七轮（2026-09-18）：Phase 3.1 Regime 机器版与覆盖率硬门禁

- 实现 `regime-rules-stat-v1`：XAUUSD 1h 的 EMA20/60、ADX14、ATR14/close 因果滚动
  分位、事件密度、规则/统计双层标签，以及 12 根最短持有 + 3 根确认防抖；
- CME 周末/每日结算休市视为预期会话间隔；其他缺口重置指标成熟窗口，禁止插值、缩窗或
  跨异常缺口计算；未来 K 线/未来生效事件注入测试均不改变历史结果；
- 新增默认 dry-run 的 `scripts/build_regimes.py`，机器门禁失败时 `--no-dry-run` 硬拒绝写库；
  同时生成 50 点盲评表与隔离答案键；
- 真实库 11,317 根 XAUUSD 1h dry-run：平均持续 19.69 根、日最大切换 2、规则/统计一致率
  92.46%、时间违规 0，四项通过；但 37 个连续段造成 UNKNOWN 1,768 根（15.62%），高于
  已批准的 ≤1% 门槛；NEWS_DRIVEN=0，原因是事件回填的可用时间晚于行情评估时点；
- 对照 W0-1 provider 审计，根因是 242 个异常槽（105 data_gap + 137 missing_timestamp）；
  登记 TD-41。当前 `market_regimes=0`，没有用放宽标准或写失败结果伪装完成；
- 门禁：专项 Regime/W0-5 测试 10 passed；全量 **2536 passed / 1 skipped**；
  `ruff check .` 与 `mypy config database src scripts`（93 个源文件）通过；
- Phase 3.1 状态：**BLOCKED（数据覆盖率）**。优先补齐异常槽并重做数据对账；之后仍需用户
  完成 50 点人工盲评。报告：`docs/experiments/Phase3_1_Regime报告.md`。

## 第二十八轮（2026-09-18）：Dukascopy 独立现货序列解除 Regime 机器门禁

- 接入 Dukascopy 官方 XAUUSD 1 分钟 BID BI5：2024-09-15～2026-09-17 共 733 个日文件，
  0 缺失、0 格式错误；原始文件只追加保存并计算 SHA-256；
- 新建 `XAUUSD_DUKASCOPY` 独立研究标的，未覆盖旧 XAUUSD/`GC=F`，也未将现货与期货逐根拼接；
- 只聚合完整 60 分钟桶，缺分钟与休市零量占位均拒绝；得到 11,872 根 1h，首次入库
  11,872，幂等复跑新增 0、重复 11,872；
- 补齐黄金市场节假日休市识别；22 个旧“异常分段”均为预期节假日休市，非节假日缺口仍重置；
- Regime 机器门禁全通过：UNKNOWN 72/11,872=0.606%、平均持续 17.25、日最大切换 2、
  规则/统计一致率 94.42%、因果违规 0；正式写入 11,872 Regime/快照，复跑新增均为 0；
- 门禁：专项链路 47 passed；最终全量 **2817 passed / 1 skipped**；`ruff check .` 与
  `mypy config database src scripts`（95 个源文件）通过；
- 新盲评表含 50 点。当前状态：**机器 PASS，等待人工盲评 ≥80% 后最终 PASS**。

## 第二十九轮（2026-09-18）：修正 Regime 人工盲评设计

- 第一轮 50/50 填写完整且标签合法；对最终平滑标签一致 19/50=38%，对即时标签一致
  29/50=58%；24/50 样本的最终标签被时序平滑改变；
- 确认旧表遗漏 EMA20/60 的 4 小时斜率，且孤立时点无法公平复核 12 根持有 + 3 根确认后的
  最终状态，因此保留首轮证据但不把 38% 直接归因为人工或模型失败；
- V2 将人工语义验收限定为即时状态，补齐斜率、北京时间和百分比字段，并按 6 类即时状态
  分层抽取 50 点；答案键继续隔离；
- 新增 `evaluate_regime_review.py`：强制 50 行、唯一 ID、合法且非空标签，按即时目标计算
  ≥80% 门禁；时序平滑继续由机器持续时长/切换次数/专项测试验收；
- 门禁：专项 13 passed；全量 **2952 passed / 1 skipped**；`ruff check .` 与
  `mypy config database src scripts`（96 个源文件）通过；
- 当前状态：**等待用户填写 V2 盲评表**。

## 第三十轮（2026-09-18）：Phase 3.1 人工盲评最终通过

- 用户完成 V2 50 点盲评；表格软件将文件保存为 GB18030，首轮评分因仅支持 UTF-8 安全失败，
  未修改用户内容；评分器新增 UTF-8/GB18030 自动读取并加入中文备注回归测试；
- 完整性：50/50 已填、标签全部合法、ID/答案键全部对齐；即时状态一致 46/50=**92%**，
  超过 ≥80% 门槛；
- 4 个分歧均为人工 `TREND_UP`、目标 `LOW_VOLATILITY`，保留为后续阈值观察样本；
- 编码兼容专项 5 passed；最终全量 **3086 passed / 1 skipped**；`ruff check .` 与
  `mypy config database src scripts`（96 个源文件）通过；
- Phase 3.1 状态：**最终 PASS**。下一阶段为 Phase 3.2 Technical Alpha + Macro Alpha 独立基线。

## 第三十一轮（2026-09-18）：Phase 3.2 Technical / Macro 独立 OOS 门禁

- 新增冻结 Technical 门禁：8 个预注册技术特征、严格下一可交易 bar 标签、60/20/20 时间切分、
  两道 24 bar embargo、validation-only Platt 校准，以及非重叠 24h test 统计；
- Dukascopy 现货 Technical 结果：非重叠 test N=77，校准 LR IC=-0.0951、ICIR=-0.4968、
  命中率 38.96%（Wilson 95% [28.84%, 50.13%]），**FAIL**；
- 新增独立 Macro 门禁：8 条 FRED/ALFRED initial-release-only 序列的水平/差分 + DXY 收益/动量，
  全部按 `released_at <= signal_at` as-of 对齐，违规 0；标签明确使用 `GC=F` 连续期货代理；
- Macro 结果：test N=381，校准 LR IC=0.0645、ICIR=0.1437、命中率 50.92%、单侧二项
  p=0.3793，**FAIL**；虽 IC>0.03，但未同时达到 ICIR>=0.3，方向亦不显著；
- 两个负面结果均未写入 Alpha 事实表，未做融合、策略、PnL、手续费、仓位或风险；
- 报告：`docs/experiments/Phase3_2_Technical_Alpha报告.md` 与
  `docs/experiments/Phase3_2_Macro_Alpha报告.md`；登记 TD-42，禁止使用 test 反复调参追求 PASS。
- 专项新增门禁 8 passed；最终全量 **3227 passed / 1 skipped**；`ruff check .` 与
  `mypy config database src scripts`（100 个源文件）通过。首次全量运行仅因系统临时目录拒绝访问
  导致夹具创建错误，改用项目内隔离 `--basetemp` 后全量通过，不属于代码失败。

## 第三十二轮（2026-09-18）：Phase 3.3 Author / News 数据资格门禁

- 新增只读 `check_phase3_3_readiness.py` 与纯函数门禁，默认 dry-run，不创建迁移、不写数据库；
- Author 审计：华尔街见闻 9 条、汇通网 2 条观点；31 个 horizon 评价行全部为
  `UNTRUSTED_COLLECTION_TIME`，可信标签为 0，低于每作者 30 条硬门槛；
- News 审计：30 条事件、56 天跨度，`fed_press_releases` 占 66.67%；未达到预注册的
  >=200 条、>=90 天、单源 <=40% 三项要求；
- HF 标题弱监督共 150 条，但无本项目可审计的事件发布时间链，只保留特征/弱标签用途；
- 修正规划冲突：`author_weight_snapshots.weight` 现为 NOT NULL，样本不足时改为“不写权重行，
  在资格报告留痕”，禁止写 NULL 或 0 冒充权重；
- Phase 3.3 当前为 **BLOCKED（数据资格）**，没有创建条件权重迁移，也没有写技能、权重或信号事实。
- 补充真实 ORM 集成门禁：在一次性数据库中构造不可信作者观点与新闻事件，验证读取器确实把
  观点判为 `UNTRUSTED_COLLECTION_TIME`、正确计算新闻跨度/集中度，且运行前后技能表和权重表
  行数完全不变；Phase 3.3 资格门禁专项 3 passed。

## 第三十三轮（2026-09-18）：Phase 3 失败门禁范围审计

- 新增只读 `check_phase3_boundaries.py`，把“负面结果不能落 Alpha、数据阻塞不能落作者权重、
  Phase 3 不得进入 Strategy/Trading”变成机械检查；
- PostgreSQL 实测：revision=`0007_phase3_feature_tables`，不存在 Alpha/Prediction/Strategy/Trading
  下游事实表，`author_skill_snapshots=0`、`author_weight_snapshots=0`；
- `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`，范围边界审计 **PASS**；
- 新增单测覆盖任一越权表、提前写技能事实或打开实盘开关都会令门禁失败。

## 第三十四轮（2026-09-18）：Phase 3.2 可复现性五件套补齐

- 为 Technical / Macro 输入增加排序后内容 SHA-256，索引、时点与原始数值均参与指纹；任一值变化
  都会得到不同哈希，多输入组合按固定顺序再次摘要；
- 报告补齐数据指纹、特征集版本、模型版本、随机种子与实际运行代码提交；
- Technical 数据哈希 `d0e0b39e...f7234d0d`，Macro 组合哈希
  `aa6d1026...dc7c5bc6`；两份报告均绑定代码提交 `c202087`；
- 新增指纹稳定性/内容敏感性/组合顺序测试；原 OOS 指标和 FAIL 判定未改变，未重新调参。
- 夜间全量回归使用项目内隔离临时目录完成：**3234 passed / 1 skipped**；`ruff check .`、
  `mypy config database src scripts`（105 个源文件）及 PostgreSQL Phase 3 范围边界复核均 PASS。

## 第三十五轮（2026-09-19）：入口文档状态去陈旧化

- README 从“Phase 1 第一步”更新为当前真实检查点：Phase 3.1 PASS、Phase 3.2 双基线负面、
  Phase 3.3 数据资格阻塞；同步当前 3234 passed / 1 skipped 全量门禁；
- 删除 README 中“真实 LLM 当前仅 Mock”“Phase 3 及以后表故意不建”等已过期描述，改为
  migration 0005/0006/0007 的真实边界及未建 Alpha/Strategy 表的原因；
- `docs/05` 同步 Phase 3.2/3.3 结论，明确不得进入融合、策略或回测；
- 新增文档状态回归测试，防止入口文档再次宣称错误阶段或把负面 Alpha 写成通过；专项 4 passed。

## 第三十六轮（2026-09-19）：Phase 3.3 用户数据交接契约

- 新增仅含表头的真实作者帖子模板和逐列填写说明，强制独立 `published_at` / `collected_at`、
  `effective_at=max(...)` 与 `collection_time_provenance=independent_observation`；
- 明确每作者硬门槛 30 条、建议至少 50 条且覆盖多个自然日；禁止复制采集时间、模型补数据、
  只挑命中帖子或用同文转载凑独立样本；
- 发现并登记 TD-44：现有 `effective_at >= collected_at` 契约下，今天下载的旧新闻只能从今天起
  使用，普通历史 CSV 不能倒填为历史可见；因此暂不发新闻填写模板，先等可审计历史可用时刻的数据源；
- 新增模板结构与防新闻回填泄漏测试；交接专项 4 passed。

## 第三十七轮（2026-09-19）：真实作者输入机械体检

- 新增只读 `check_phase3_3_author_input.py`，支持既有 CSV/XLSX 读取链，不修改输入、不写数据库；
- 硬校验 12 个必填字段、带时区时间、`collected_at >= published_at`、
  `effective_at=max(published_at,collected_at)`、非未来时间、独立采集时间 provenance、NEWS 类型、
  true/false 媒体标记、source+id 唯一、正文去重与最少 90 字；
- 按作者统计独立行数，少于 30 条保持 BLOCKED；模板空表实测明确返回“输入没有数据行”，
  不会把空模板误判为通过；
- 新增 30 条通过、fallback/重复/样本不足、naive/未来时间三类测试，专项 3 passed。

## 第三十八轮（2026-09-19）：Phase 3.2 真实库双跑复现审计

- 新增 `check_phase3_2_reproducibility.py`：在同一真实 PostgreSQL 输入上连续运行 Technical 与
  Macro 两次，结果要求逐字段精确相等，不用浮点容差掩盖非确定性；
- 两次运行均复现既有数据哈希与全部指标；Technical=`d0e0b39e...f7234d0d`，
  Macro=`aa6d1026...dc7c5bc6`；
- 运行前后事实表行数完全一致：`feature_snapshots=11873`、`market_regimes=11872`、作者技能/权重均 0；
- 新增“极小数值差异也必须失败”的精确复现测试；审计 PASS，但不改变两个 Alpha 的 FAIL 结论。

## 第三十九轮（2026-09-19）：可审计早间交接

- 新增 `build_morning_handoff.py`，只读 Git 与本地 PostgreSQL，自动汇总阶段结论、质量门禁、
  数据库 revision/关键表行数、夜间提交和仅需用户处理的事项；默认 dry-run，显式参数才写报告；
- 生成前工作区干净，数据库仍为 `0007_phase3_feature_tables`；`market_bars=58891`、
  `feature_snapshots=11873`、`market_regimes=11872`，作者技能/权重表仍为 0；
- 交接明确保留三条红线：Phase 3.2 不能在当前 test 上继续调参，Phase 3.3 新闻不能普通历史回填，
  所有失败/阻塞结果不得进入融合、策略、回测或实盘；
- 专项格式、lint、类型检查和单测均通过；报告写入
  `docs/operations/2026-09-19_早间交接.md`。

## 第四十轮（2026-09-19）：作者样本身份口径加固

- 修正作者资格门禁的计数口径：从仅按易重名的 `author_name`，改为数据库自然键一致的
  `source + external_account_id`；同名不同平台账号不能合并凑足 30 条；
- 同一平台账号若出现不一致 `author_name`，现在会产生硬错误，防止账号映射漂移后误训练；
- 体检报告中的作者标签同时显示名称与稳定账号键，便于人工核对；交接说明同步明确此口径；
- 新增同名跨账号拆分和同账号名称冲突回归测试；作者输入专项 **5 passed**，格式、lint、类型检查
  全部通过；只收紧数据资格，不修改数据库，也不改变 Phase 3.3 的 BLOCKED 结论。

## 第四十一轮（2026-09-19）：作者输入可核验性加固

- 将“采集时间必须独立记录”落实为硬门禁：`collected_at` 必须严格晚于 `published_at`，相等时
  视为复制发布时间并拒绝，避免仅填写 provenance 文本就绕过时间因果要求；
- `url` 现在必须是可解析的 `http/https` 地址，同一规范化地址不能重复计为独立样本；解析器对
  畸形 URL 安全返回逐行错误，不会令整次体检崩溃；
- 用户交接说明同步强调严格时间关系和唯一可复核地址；新增复制时间、重复 URL、畸形 URL 回归；
- 作者输入专项 **6 passed**，lint 与类型检查通过；没有修改数据库或放宽任何研究门禁。

## 第四十二轮（2026-09-19）：08:00 前最终质量复核

- 使用项目内隔离临时目录完成最终全量回归：**3379 passed / 1 skipped**，唯一 skip 为可选
  `jieba` 未安装；`ruff check .` 与 `mypy config database src scripts`（109 个源文件）通过；
- PostgreSQL Phase 3 范围边界复核 PASS：revision 仍为 `0007_phase3_feature_tables`，不存在
  Alpha/Prediction/Strategy/Trading 下游事实表，作者技能/权重仍为 0，实盘与外部下单开关均关闭；
- Phase 3.2 真实库双跑复现再次 PASS：Technical/Macro 数据哈希和结果逐字段一致，事实表行数
  前后不变；该复核不改变两个 Alpha 的 FAIL 结论；
- README 与早间交接生成器同步最终测试计数；未新增迁移、未写研究事实、未使用 test 调参。

## 第四十三轮（2026-09-19）：作者公开数据源试采与合规否决

- 按用户授权先验证公开来源：中金在线候选页出现证书域名不匹配，未绕过安全警告；
- Kitco 作者页技术上可解析稳定账号、文章链接和时间，但其公开使用条款明确禁止机器人/自动检索、
  数据挖掘和未经授权存储/复制站点内容，因此不能用于持续自动采集或模型语料；
- 停止试采，清理本轮 5 条临时正文及体检产物，未创建定时任务、未写数据库；
- 新增合规审查报告并登记 TD-45。下一步仅需用户提供允许自动采集与研究使用的官方 API、许可文件
  或内容方书面授权；在此之前 Phase 3.3 Author 继续 BLOCKED。

## 第四十四轮（2026-09-19）：作者来源授权机械门禁

- 新增来源授权模板，按 `source + external_account_id` 稳定账号键登记状态、授权依据、证据引用、
  自动采集/本地存储/研究使用三项许可、人工核验人与生效/到期时间；模板不含示例假授权；
- 新增纯函数授权门禁：仅接受 `official_api`、`license_agreement`、`written_permission`、
  `user_owned`，三项许可必须同时为真，授权必须已生效且未过期，证据必须是 HTTPS URL 或
  `docs/legal/` 内文件；PENDING/REJECTED、空表和无证据授权均保持 BLOCKED；
- 作者输入门禁现在要求每个帖子账号都存在当前有效授权；仅满足内容、时间和 30 条样本量已不足以
  PASS，从代码层阻止“公开可见即默认可用”；
- 体检命令新增必填 `--authorizations`，报告同时绑定帖子与授权表 SHA-256；专项测试通过，空模板
  实跑按预期返回 BLOCKED，未写数据库。

## 第四十五轮（2026-09-19）：授权证据引用防越界

- 收紧授权证据字段：`docs/legal/` 路径不能含上级目录跳转，实际解析位置必须在该目录内且文件存在；
  HTTPS 引用拒绝畸形地址和嵌入账号密码，拒绝 HTTP 明文链接；
- 增加缺失本地文件、存在文件、路径穿越和畸形 URL 回归测试；Phase 3.3 授权/作者输入专项
  **16 passed**，格式、lint、mypy 均通过；
- 交接文档明确机器门禁只检查登记字段和证据位置，不能证明许可文本的法律效力、用途范围或
  签认人身份；这些仍需用户人工核验，未经核验不得填写 APPROVED。

## 第四十六轮（2026-09-19）：作者数据与授权时钟严格化

- 作者输入的发布时间/采集时间与授权表的 `reviewed_at` 取消一小时未来容差；只要晚于真实体检
  当前时刻，就产生硬错误，避免短窗口内前视数据被放行；
- 两个纯函数门禁拒绝缺少时区的 `now` 参数，避免使用系统本地时区隐式解释审计时刻；
- 新增一分钟未来时间和 naive 审计时钟回归测试；专项 **19 passed**、lint/mypy 通过；空表
  与未授权来源仍为 BLOCKED，数据库未修改。

## 第四十七轮（2026-09-19）：作者体检报告总门禁一致性

- 修复报告展示与进程退出状态不一致的缺陷：过去帖子资格通过但授权表其他行有硬错误时，
  命令返回 BLOCKED（退出码 2），报告总标题却可能显示 PASS；
- 总标题现要求作者输入和整个授权表同时 ready，逐行已通过账号数也明确不是整体授权结论；
- 增加“一行授权通过、另一行过期”与双门禁组合回归；相关专项 **18 passed**、ruff/mypy 通过，
  没有修改原始数据或数据库。

## 第四十八轮（2026-09-19）：帖子采集时间绑定授权有效期

- 修复来源账号在体检当日获授权、但帖子 `collected_at` 早于 `valid_from` 时仍可能通过的时间范围缺口；
  每条帖子现在必须在该账号授权有效期 `[valid_from, expires_at)` 内采集，不能以后获授权追认既往采集；
- 来源授权门禁只向作者门禁传递逐行审核通过账号的有效期；总报告仍要求整个授权表与作者输入同时
  ready。补充生效前拒绝、生效时刻允许、到期时刻拒绝及授权有效期传递回归；
- Phase 3.3 专项 **21 passed**，全量 **3528 passed / 1 skipped**；`ruff check .`、
  `mypy config database src scripts` 通过。未修改 PostgreSQL、原始作者数据或 Phase 3.2 FAIL 结论；
  Phase 3.3 仍因缺少经人工核验的合法授权和合格数据保持 BLOCKED。

## 第四十九轮（2026-09-19）：新闻历史覆盖改按可用时间计算

- 修复 Phase 3.3 News 资格报告用 `news_events.published_at` 估计 90 天历史的前视漏洞：
  今天才采集的旧标题不能算作当时已可用的新闻。每条事件的可用起点现在取
  `max(news_events.effective_at, raw_items.effective_at)`，再计算整体跨度；原始表的采集时间约束
  因此也能兜底防止加工层较早的时间戳误放行；
- 增加历史标题同日采集的数据库集成回归，证明跨 120 天发布时间不再形成可用历史；专项
  **7 passed**，全量 **3662 passed / 1 skipped**，ruff/mypy 通过；
- 只读重算现有研究库并更新资格报告：30 条新闻的原“历史跨度 56 天”按真正可用时间变为
  **0 天**，单源占比仍为 66.67%，News 继续 BLOCKED。没有改写数据库、原始数据、技能或权重，
  Phase 3.2 Technical/Macro 的 FAIL 结论仍冻结；历史合规来源仍需用户提供并核验。

## 第五十轮（2026-09-19）：准备次日仅需用户处理清单

- 新增 `docs/operations/2026-09-20_用户待处理清单.md` 准备稿，将合法作者授权、真实帖子、
  历史新闻授权与可用时间证据、Phase 3.2 负面基线裁决四项人工事项分离；明确机器不能替代
  许可审查，禁止为凑量回填历史或修改真实时间；
- 清单注明当前 News 可用时间跨度为 0 天及次日仍须复核最终状态；未更改数据、数据库、门禁
  代码或阶段结论。最终早间交接须重新核验并更新，不能把本准备稿当作最终报告。

## 第五十一轮（2026-09-19）：数量达标不等于 Alpha 放行

- 修复 Phase 3.3 库内资格报告可能在计数/标签/跨度达标时直接将 Author 或 News Alpha 标为
  PASS 的误导性状态：该报告不能人工核验来源许可、新闻历史可用证据；现在始终把两项 Alpha
  总结论保持 BLOCKED，同时单列库内门槛 PASS/BLOCKED，避免把工程计数当作合法数据资格；
- 增加两项库内门槛都 PASS 时总状态仍 BLOCKED 的回归，重算本地研究库报告仍为双 BLOCKED；
  专项 **5 passed**、全量 **3796 passed / 1 skipped**、ruff/mypy 通过。未更改数据库、原始数据、
  失败基线或交易安全开关；
- 全量测试计数随项目内不同 `.pytest-temp-*` 目录增加而增长，已定位为配置编码测试扫描根目录
  时未排除这些临时目录；通过数不能直接用来推断新增了同等数量的业务测试。此项由工程侧后续
  独立修复，不需要用户处理。

## 第五十二轮（2026-09-19）：稳定配置编码测试的收集范围

- 配置编码测试过去递归扫描整个项目，将 `.pytest-temp-*` 中测试生成的配置副本及被 Git 忽略
  的 `logs/` JSON 也当成项目配置；每次使用新的隔离测试目录，收集数便虚增；
- 现在排除自动测试缓存、原始数据和日志目录，仍检查项目内实际配置（含 `alembic.ini`、
  `pyproject.toml`、`.github` CI YAML）。增加临时目录/日志排除及真实配置保留回归，
  YAML 配置检查同步排除生成目录；创建新的隔离测试目录前后均收集 **1415 tests**；
- 配置/日志专项 **18 passed**，全量 **1414 passed / 1 skipped**，ruff/mypy 通过。
  相比此前 3796 的计数下降是剔除自动生成的配置副本和本地忽略数据，不是减少业务测试；
  数据库、原始数据及 Phase 3 的 FAIL/BLOCKED 结论均未改变。

## 第五十三轮（2026-09-19）：混合授权状态总门禁防误放行

- 修复授权表同时包含一条 APPROVED 和另一条 PENDING/REJECTED 时，后者只有 warning，
  整表却可能被判 ready 的缺口；非 APPROVED 行现在产生逐行硬错误，同时保留账号提示。
  仅逐行通过的账号可暂时进入帖子校验，但总报告仍因整表未 ready 而 BLOCKED；
- 增加“一个已批准、一个待定”回归；授权/报告专项 **10 passed**、全量
  **1415 passed / 1 skipped**，ruff/mypy 通过。没有修改数据库、用户授权表或来源内容；
  Phase 3.2 FAIL 与 Phase 3.3 BLOCKED 结论保持不变。

## 第五十四轮（2026-09-19）：新闻解析版本去重

- 修复 Phase 3.3 News 库内门槛按 `news_events` 行数计数的重复样本问题：同一
  `raw_item_id` 可有多个 `parser_version`，现在仅计为一条独立新闻；同一原始新闻各解析版本的
  可用时间取较晚者，保守防止版本加工时间将历史跨度提前；
- 增加两版本指向同一原始新闻的数据库集成回归，专项 **5 passed**，全量
  **1416 passed / 1 skipped**，ruff/mypy 通过；重新生成本地资格报告无变化，当前 30 条新闻
  的计数与 0 天可用跨度保持原值，News Alpha 仍 BLOCKED；
- 未修改 PostgreSQL、真实原始数据、Phase 3.2 FAIL 结论或实盘安全配置。

## 第五十五轮（2026-09-19）：作者可信样本按独立帖子计数

- 修复 Phase 3.3 Author 库内门槛按 `LABELED` 标签行数计数的虚增风险：一个无预设周期的
  `author_opinions` 可产生 H1/H4/D1 等多个标签，同一 `author_post_id` 的多个解析观点也
  不能成为多条独立原始样本；现在每位作者只计有至少一个可信标签的独立帖子；
- 增加单帖子 10 个观点、30 个可信标签仍只算 1 条独立帖子的数据库集成回归；报告的硬门槛
  与列名同步改为独立帖子口径。专项 **5 passed**、全量 **1417 passed / 1 skipped**，
  ruff/mypy 通过；真实库报告仅口径文字变化，当前可信独立帖子仍为 0，Author 继续 BLOCKED；
- 未改写 PostgreSQL、原始用户数据或 Phase 3.2 冻结 FAIL 结论。

## 第五十六轮（2026-09-19）：作者库内门槛绑定稳定来源账号

- 修复同一 `author_id` 下两个来源账号各 15 条可信独立帖子可能汇总成 30 条、错误判作者
  ready 的缺口；现在逐个 `source + external_account_id` 统计，所有进入当前评估的账号均须独立
  达到 30 条。观点、帖子、账号的作者归属不一致时该作者保持 BLOCKED；
- 资格报告新增账号分项与归属异常提示，本地研究库重算后两个作者账号均为 0 条可信独立帖子，
  仍为 BLOCKED；新增跨账号 15+15 不可凑数、单账号 30 可通过库内门槛的集成回归。
  专项 **7 passed**、全量 **1419 passed / 1 skipped**、ruff/mypy 通过；
- 未改写 PostgreSQL、原始数据、技能/权重事实或 Phase 3.2 冻结 FAIL 结论。

## 第五十七轮（2026-09-19）：作者跨表归属异常回归锁定

- 增加数据库集成反例：同一账号已有 30 条可信独立帖子，但另有一条观点、帖子、账号的
  `author_id` 归属不一致；即使正常账号计数达到 30，作者库内门槛仍必须 BLOCKED，
  `identity_consistent` 为 false，不能因错误行未计入样本就忽略身份污染；
- 账号分组专项 **8 passed**，全量 **1420 passed / 1 skipped**、ruff/mypy 通过；
  本轮只补测试与日志，不改生产逻辑、数据库、原始数据或既有 FAIL/BLOCKED 结论。

## 第五十八轮（2026-09-19）：新闻事件与原始类型一致性

- 修复 Phase 3.3 News 库内计数可能包含错误挂到 `RawItemType.POST` 等非新闻原始记录的
  `news_events` 行；资格查询现在只接受原始类型为 `NEWS` 的独立事件，避免跨表类型错配
  推高事件数与历史跨度；
- 新增“新闻事件指向帖子原始记录”数据库集成回归，专项 **10 passed**、全量
  **1421 passed / 1 skipped**、ruff/mypy 通过；本地资格报告重算无变化，30 条新闻与
  0 天可用跨度、Author/News 双 BLOCKED 保持不变；
- 未改写 PostgreSQL、真实数据、技能/权重事实或 Phase 3.2 FAIL 结论。

## 第五十九轮（2026-09-19）：资格审计统一截止时点

- 为 Phase 3.3 库内只读门禁加入带时区的 `as_of` 审计时点；新闻只有在原始记录和解析事件的
  较晚 `effective_at` 不晚于该时点时才计入，未来解析行单独计数并在报告披露；
- 作者可信独立帖子同时要求帖子、观点的 `effective_at` 及可信标签的 `exit_at` 均不晚于
  审计时点，避免“信息已出现但前瞻收益尚未结束”时提前算作可信标签；无时区或缺失的
  标签结束时间不能放行。SQLite 测试读回的时间按数据库 UTC 契约规范化；
- 新增未来新闻、未来作者数据、未来标签结束时间与 naive 审计时钟回归；专项
  **13 passed**、全量 **1425 passed / 1 skipped**、ruff/mypy 通过。本地报告记录审计时点，
  当前未来新闻排除数为 0，30 条新闻/0 天跨度与 Author/News 双 BLOCKED 均未变化；
  未改写 PostgreSQL 或 Phase 3.2 冻结 FAIL 结论。

## 第六十轮（2026-09-19）：作者体检报告防覆盖输入

- 修复 `check_phase3_3_author_input.py` 的 `--report` 若指向帖子 CSV/XLSX 或来源授权表，
  可能把原始用户数据覆盖成 Markdown 报告的破坏性风险；命令现在在读取、校验或写报告前
  比对解析后的路径，相同即以参数错误退出，不触碰任一输入；
- 增加两类原始文件被指定为报告目标的 CLI 回归及独立路径允许测试；专项
  **17 passed**、全量 **1428 passed / 1 skipped**、ruff/mypy 通过；用户交接说明同步注明
  报告路径必须独立。没有改写 PostgreSQL 或任何真实用户输入，阶段结论保持不变。

## 第六十一轮（2026-09-19）：作者体检报告防硬链接别名覆盖

- 扩展报告目标保护：除路径解析后相同外，也检查已存在报告文件是否与帖子表或来源授权表
  为同一底层文件，防止不同文件名的硬链接绕过保护并把用户原始输入覆盖为报告；
- 增加两份输入各自经硬链接别名指定为报告目标的命令行回归，均拒绝写入且原文件保持原样。
  专项 **7 passed**、全量 **1430 passed / 1 skipped**、ruff/mypy 通过；范围边界只读审计
  PASS（revision `0007_phase3_feature_tables`、作者技能/权重 0 行、交易开关关闭）；
- 本轮未改写 PostgreSQL、真实数据、技能/权重事实或 Phase 3.2 冻结 FAIL；
  Phase 3.3 Author/News 仍为 BLOCKED。

## 第六十二轮（2026-09-20）：作者可信样本要求独立采集时间标记

- 修复 Phase 3.3 作者库内资格漏洞：即使前瞻标签状态为 `LABELED`，原始帖子若没有
  `collected_at_provenance=independent_observation`，也不得计入可信独立帖子；缺失标记、
  仅输入时间和以 `effective_at` 回填时间均 fail-closed，避免无采集时点证据的旧记录撑高样本量；
- 增加三种无独立证据的数据库集成反例，并将原有可信样本夹具明确标为独立采集时间。
  专项 **15 passed**、全量 **1433 passed / 1 skipped**、ruff/mypy 通过。资格报告同步说明
  此门槛并重算：作者可信帖子仍为 0，新闻仍为 30 条/0 天，Author/News 双 BLOCKED；
- 未改写 PostgreSQL、真实数据、技能/权重事实或 Phase 3.2 冻结 FAIL。原始标记也不等于
  法律授权已核验，用户提供的来源许可仍须人工签认。

## 第六十三轮（2026-09-20）：作者可信样本的原始记录链与时间因果加固

- 在第六十二轮「独立采集时间标记」之上，进一步收紧 Phase 3.3 作者库内资格门禁的
  **原始记录链与时间因果**三道防线：①`raw.item_type` 必须为 `POST`（排除把 `NEWS`
  原始记录误挂到作者帖子、抬高独立样本）；②`raw.source_id` 必须与账号 `source_id`
  一致（防跨源错配、身份漂移）；③时间链 `raw_at <= post_at <= opinion_at`
  （防原始记录晚于帖子、或观点先于帖子等时间倒挂，fail-closed）；
- 新增参数化数据库集成反例 4 类（`raw_type` / `raw_source` / `raw_late` /
  `opinion_early`），专项 **19 passed**、全量 **1437 passed / 1 skipped**
  （较第六十二轮 +4）、`ruff check .` 与 `mypy config database src scripts`
  （110 个源文件）通过；本工作区两个改动文件 `ruff format --check` 通过
  （全仓 `format --check` 仍命中 TD-33 已登记的历史格式债，非本次引入）；
- 未改写 PostgreSQL、真实数据、技能/权重事实或 Phase 3.2 冻结 FAIL；本轮只收紧
  资格口径，当前作者可信独立帖子仍为 0、新闻仍为 30 条/0 天，Author/News 双 BLOCKED
  不变，继续等待用户提供数据源授权与真实语料。
