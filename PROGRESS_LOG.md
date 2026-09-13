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


