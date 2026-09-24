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


## 第六十四轮（2026-09-21）：Author Alpha 管线验证冻结 + News Alpha 离线框架

### 1. Author Alpha 管线验证完成（冻结）

- 修正 `compute_timing_skill`：从 sigmoid 改为「方向命中样本中有向收益 > threshold 的比例」
  （`TIMING_THRESHOLD=0.001`，10 bps ≈ 黄金典型点差+滑点量级），与 `direction_skill` 解耦；
  无命中或命中数 < 30 → `timing_skill=None`（报告标注 NOT_EVALUATED）；
- 新增组装层 `src/alpha/author_skill_assembly.py`（OpinionLabel → SkillSample →
  compute_author_skill → author_skill_snapshots，幂等写入）+ 组装层单测/集成测试；
- 新增 `scripts/import_mock_posts.py`（250 条 Mock 合成博文入库：raw_json 写
  `data_source=SYNTHETIC` + `is_mock=true` + `mock_batch`，作者 `canonical_name=mock:*` +
  `display_name=[MOCK]`，与真实语料严格区分）与 `scripts/build_author_skill.py`（组装层
  CLI，报告分 Mock/真实两层，分列 direction_skill_raw / direction_skill /
  timing_skill_raw / timing_skill / calibration_score）；
- 跑通：250 Mock 帖 → 275 观点 → 256 LABELED（全来自 Mock）+ 31 UNTRUSTED（真实 19 帖
  缺 collected_at，不篡改）；4 位 Mock 作者 skill 快照幂等写入（重跑新增 0），真实层
  0 可用标签；
- 报告：`docs/experiments/Phase3_3_Author_Alpha_管线验证报告.md`（顶部一句
  「本次仅验证管线正确性，不作任何真实作者技能结论」）。

### 2. News Alpha 离线框架（纯函数，未跑真实数据）

- 新增 `src/alpha/news_alpha.py`：`NewsObservation` → `add_forward_return_labels`
  （时间因果：入场 bar 的 open_time 严格晚于 effective_at）→ `time_split`
  （60/20/20 + embargo）→ `evaluate_news_gate`（train LR → validation Platt →
  untouched test，4 基准对比：model_lr_platt / random / always_long / sentiment）；
- 特征第一版：`sentiment` + `importance`（来自 news_events 结构化字段）；标签 horizon
  24 根 1h bar（1d）；
- HF 150 条标题情感只能作弱标签/特征，不得回灌为观点金标准（Phase 2 裁决）；
- 单测 9 项；未跑真实数据，待用户确认后按 Mock/弱监督/真实三层验证。

### 3. 状态

- 全量 **1479 passed / 1 skipped**（较第六十三轮 +42）；`ruff` 通过；
- TD-43 / TD-45 仍为 P0（待合法授权数据源）；未改 docs/10、docs/11 口径；未新增依赖
  （沿用 sklearn）；未写 Alpha 事实表、未进入 Phase 3.4 Meta Ensemble。


## 第六十五轮（2026-09-22）：GOLD-001-R2 —— 30 分钟 Collector Scheduler + 质量门禁修复

> **补记说明**：GOLD-001-R2 交付时未按任务要求写本日志（该缺口连同 GOLD-002 的日志缺口
> 一并在 GOLD-002-R1 复核中补齐）。以下内容**只**来自 `.ai/results/GOLD-001-R2.json`
> 审计记录与仓库现状（`git show 8895e70`），不含任何未发生的结论。

### 1. 交付内容

- **新增调度层** `src/scheduler/`（`slots.py` / `core.py` / `__init__.py`）：
  UTC 对齐的**确定性 30 分钟槽**（`resolve_slot`，同一时刻必得同一槽，可重放）、
  `job_runs` 幂等（同槽重复运行不重复执行）、stale `RUNNING` / `RETRYING` 接管、
  单源「构造 / 执行」两级故障隔离（一个源坏掉不影响其它源）；
- **新增常驻入口** `scripts/run_collector_scheduler.py`（`--once` 与常驻循环，Ctrl+C 正常退出）；
- **新增生产工厂** `src/collectors/bootstrap.py`（注册副作用 + 按 `sources.config_json["collector"]`
  构造采集器并注入 `AiohttpTransport`）；
- **复用现有 `job_runs` 表**：无新增 migration / schema，无新增依赖；
- **质量门禁修复**：`orchestrator/ai_orchestrator.py`、`scripts/collectors/dry_run_collectors.py`、
  `scripts/collectors/smoke_collectors.py`；`README.md` §7 补「30 分钟调度」、
  `TECH_DEBT.md` TD-09 解除。

### 2. 测试与门禁（GOLD-001-R2 审计记录）

- 新增 **60 项 Mock 测试**（5 个测试文件：`tests/integration/test_scheduler_core.py`、
  `tests/integration/test_collector_scheduler_cli.py`、`tests/unit/test_scheduler_slots.py`、
  `tests/unit/test_scheduler_core_units.py`、`tests/unit/test_collector_scheduler_cli_args.py`）；
  **GOLD-002-R1 复核时用 `pytest --collect-only` 机械计数确认为 60 项**；
- 全量 `pytest` **1589 passed / 1 skipped**；`ruff check .` → `All checks passed!`；
  `mypy config database src scripts` → 127 source files 全绿
  （GOLD-002 新增 7 个源文件后 = 134，与该差值自洽）；
- 无联网测试、无 Mock 冒充真实数据、无 schema 变更。

### 3. 本轮的已知瑕疵（已在 GOLD-002 处理）

- R2 误把 7 个 `.tmp_pytest*` 临时测试产物提交进仓库（`.tmp_pytest{,2}_{err,out,pid}.*`），
  已在 GOLD-002 清理并加 `.gitignore` 规则，见第六十六轮。


## 第六十六轮（2026-09-22）：GOLD-002 —— 独立 Processor Pipeline 与数据处理审计基础（TD-11）

### 1. 交付内容（commit `d7b0d42`）

- **新增 `src/processors/collection/`**（采集后处理层，**不联网 / 不做 provider 授权 / 不做调度**）：
  `contracts.py`（`ProcessorInput` / `ProcessedRecord` / `ProcessingReport` / `RawItemLike` 协议 /
  `RecordOutcome` / `BatchStatus`；`processor_name=collection_normalizer`、
  `processor_version=collection-normalizer-v1`）、`normalize.py`（NFKC / 去零宽 / 折叠空白 / 截断）、
  `identity.py`（内容指纹 + 幂等键 + 批次内去重索引，纯内存无时间依赖）、
  `validate.py`（`source_record_id` / `collected_at` / 空文本 / 未来时间戳）、
  `pipeline.py`（normalize → timezone/effective_at → identity/dedup → validation/audit 四阶段编排 +
  幂等落库 + 审计摘要）；
- **新增 `src/common/redaction.py`**（凭据擦除**唯一实现**）：`redact_secrets` / `is_sensitive_key` /
  `safe_text` / `safe_url` / `sanitize_mapping`；`src/scheduler/core.py` 原有内联规则上移复用
  （对外仍导出 `redact_secrets`，实现改为共享模块）；规则顺序有语义——`Bearer` 必须先于
  `Authorization` 擦除，否则 `Authorization: Bearer xxx` 会残留真实 token（单测锁定）；
- **append-only + 幂等**：只写 `processed_items`，`(raw_item_id, processor_name, processor_version)`
  唯一约束 + 落库前存在性检查 → 重复输入 / 重复运行新增 **0 行**；且 `processed_items` 已在
  `database/protection.py` 的不可覆盖表清单（`allowed_updates=∅`）——UPDATE / DELETE 在 flush 期
  直接报错，属**结构性** append-only；
- **时间语义**：`effective_at = max(published_at, collected_at, 上游 raw_items.effective_at)`；
  三者都不可用时判 `REJECTED` 且 `persistable=False`（**不落库**），绝不用「现在」冒充事实时间；
- **坏数据隔离 + 诚实状态**：单条 `REJECTED` / `FAILED` 只影响自己；批次状态区分
  `SUCCESS` / `PARTIAL_FAILED` / `FAILED`；落库状态复用现有 `ProcessStatus`（`DUPLICATE` / `REJECTED`
  → `SKIPPED`，留痕但不冒充加工成功），**无新增枚举 / migration / schema**；
- **审计摘要白名单**：`processor / processor_version / status / stages / input_count / output_count /
  duplicate_count / rejected_count / failed_count / started_at / finished_at / warnings / error`；
  元数据经 `sanitize_mapping`（丢弃凭据键、丢弃嵌套结构、URL 去 query/userinfo、超长截断）；
- **采集侧最小接线**：`BaseCollector(..., post_processor=...)`（默认 `None` → 与引入前**零行为差异**）+
  `src/collectors/bootstrap.py::default_collector_factory(post_processor=...)`；参考实现 = RSS 路径
  `python scripts/collect_rss.py --to-db`（CLI 统计新增 `processing` 摘要）；Processor 异常在采集层
  兜住并转成**脱敏**告警，不阻断采集、不丢原始数据；
- **Scheduler / Processor 边界**：`src/scheduler/**` 与 `scripts/run_collector_scheduler.py` 中
  **零** Processor 引用（GOLD-002-R1 用 grep 机械确认）——接线由 bootstrap / CLI 注入，不进调度核心；
- **清理临时产物**：删除 R2 误提交的 7 个 `.tmp_pytest*`，`.gitignore` 增 `.tmp_pytest*` 规则。

### 2. 测试与门禁（GOLD-002 交付时审计记录）

- 新增 **72 项 Mock 测试**（全部零网络、零真实数据源）：
  `tests/unit/test_collection_processor.py` 35、`tests/unit/test_redaction.py` 25、
  `tests/integration/test_collection_processor_persistence.py` 8、
  `tests/integration/test_collection_processor_wiring.py` 4；覆盖确定性、时区边界、`effective_at`、
  重复输入 / 重复运行、坏数据隔离、敏感字段过滤、空批次与批次状态映射；
- 全量 `pytest` **1663 passed / 1 skipped**；`ruff check .` 全通过；
  `mypy config database src scripts` → 134 source files 全绿（Orchestrator 审计记录）；
- 无新增依赖、无新增 migration / schema、未进入 Phase 3.4、未训练 Alpha、未生成交易信号。

### 3. 文档同步

- `README.md` §7 新增「采集后处理 Processor Pipeline」小节（流水线、职责边界、幂等、接线点、测试）；
- `TECH_DEBT.md`：TD-11 **解除**；TD-12 收紧为**部分解除**（`processed_items` / `audit_logs` /
  `data_versions` 均已有写入路径，剩余：非行情数据集的 `data_versions` 快照、
  `processed_items` 无 `error_message` 列 → TD-19）；**未宣称 TD-12 全部解除**；
- 遗留缺陷（GOLD-002-R1 复核修复）：见 §4。


### 4. GOLD-002-R1 复核与验收记录补齐（2026-09-22）

> 复核范围**刻意聚焦**（append-only / 幂等键 / `effective_at` / 坏数据隔离 / 审计摘要脱敏 /
> Scheduler-Processor 边界 / RSS 接线），不扩功能、不改架构。

- **结论：无未处理的 P0/P1 回归。** 逐项证据：
  - **append-only**：`processed_items` 已在 `database/protection.py` 的不可覆盖表清单
    （`allowed_updates=frozenset()`），flush 期改写抛 `ImmutableRecordError`
    （既有测试 `tests/integration/test_opinion_pipeline.py::test_pipeline_processed_items_are_append_only` 锁定）；
    `src/processors/collection/**` 中 **0 处** `update()` / `delete()`（grep 机械确认），只 `session.add`；
  - **幂等键**：`(raw_item_id, processor_name, processor_version)` 唯一约束 + `_already_processed`
    存在性检查 → 重复输入 / 重复运行**新增 0 行**；由
    `test_repeat_run_does_not_create_duplicate_processed_rows`、
    `test_known_identity_key_marks_rerun_as_duplicate` 锁定；
  - **`effective_at`**：`max(published_at, collected_at, 上游 raw_items.effective_at)`；
    三个时间源全缺 → `REJECTED` + `persistable=False`（**不落库**），绝不用「现在」冒充事实时间；
    测试锁定 `processed.effective_at >= raw.effective_at`（含「发布时间晚于采集时间」边界）；
  - **坏数据隔离**：`process_one` 把单条异常兜成 `FAILED` 记录；采集层 `_post_process` 再兜一层
    （脱敏告警 + 不阻断采集 + 不丢原始数据）；批次状态映射有参数化测试；
  - **审计摘要脱敏**：`sanitize_mapping` 丢弃凭据键与嵌套结构、URL 去 query/userinfo、值截断；
    集成测试断言真实 `api_key` / `Authorization` 不出现在 `structured_json`；
  - **Scheduler / Processor 边界**：`src/scheduler/**` 与 `scripts/run_collector_scheduler.py` 中
    Processor 引用数 **0**（grep 机械确认）；接线由 `bootstrap` / CLI 注入；
  - **RSS 接线**：`tests/integration/test_collection_processor_wiring.py` 4 项全绿（含 CLI
    `--to-db` 端到端 + 「不注入 → 零 `processed_items`」的零回归断言）。
- **发现并修复 1 处真实缺陷（P2）**：`src/common/redaction.py::sanitize_mapping` 对 URL 类键
  无条件 `safe_url(str(value))`，于是 `{"feed_url": None}` 在审计摘要里被写成**字符串** `"None"`——
  把「未知 URL」伪造成一个看起来像值的字符串。修复：空值原样保留 `None`；新增回归测试
  `tests/unit/test_redaction.py::test_sanitize_mapping_keeps_null_url_values_as_null`
  （断言 JSON 落库形态为 `null`）。未改公共接口、未改其它调用点。
- **文档一致性修正**：
  - `README.md` §10「Phase 2 并行推进」里「TD-11：独立 Processor 层（含 TD-03 的 4h 聚合）」属
    **陈旧条目**（TD-11 已解除、TD-03 已解除），已改写为「TD-11 / TD-09 已解除 + TD-12 部分解除
    （列明剩余范围，指向 TECH_DEBT）」，与 `TECH_DEBT.md` 口径一致；
  - README §7 测试计数 25 → 26（复核新增 1 项回归）；`TECH_DEBT.md` TD-11 的 72 → 73
    （交付 72 + 复核 1），两处口径一致；
  - 新增本日志的**第六十五轮**（GOLD-001-R2）与**第六十六轮**（GOLD-002）记录：TECH_DEBT 中
    「证据见 PROGRESS_LOG 第六十五 / 六十六轮」此前指向**不存在的条目**（原任务未写日志），
    本轮补齐后引用可解析；
  - TD-12 口径核对：`processed_items`（采集后处理 + `src/processors/opinion_pipeline.py`）、
    `audit_logs`（`database/repositories/audit.py`）、`data_versions`（`src/features/market.py`、
    `src/alpha/regime.py`）写入路径均在代码中确认存在 → 维持「**部分解除**」，未宣称全部解除；
    TD-11 有实现 + 测试 + 接线证据 → 维持「已解除」。
- **门禁（本轮实测，项目 `.venv`）**：
  - `pytest tests -q` → **1666 passed / 1 skipped**（收集 1667 项；唯一 skip 为 `jieba` 已安装分支）；
  - `ruff check .` → `All checks passed!`；
  - `mypy config database src scripts` → `Success: no issues found in 134 source files`；
  - **环境提示（非代码回归、非本轮引入）**：若当前 shell 已导出 `FRED_API_KEY`，
    `tests/unit/test_config_security.py::test_defaults_are_safe` 会失败——该测试断言「默认值安全」，
    而 pydantic-settings 会读取 OS 环境变量；仅清空该变量后即 **1666 passed / 1 skipped**。
    本轮**未**修改该测试（越出 GOLD-002-R1 范围，仅记录）。
- **范围守规**：未新增依赖、未新增/修改 migration 与 schema、未进入 Phase 3.4、未训练 Alpha、
  未生成交易信号、未触碰 `.ai/tasks/**` 与 `.ai/results/**`。
- **次要观察（GOLD-003 已修复）**：`sanitize_mapping` 对 URL 类键的非字符串标量
  （如 `{"is_url": True}`）会 `str()` 成 `"True"`；属 P2 展示口径问题，
  已在 GOLD-003 收紧为「只有字符串走 `safe_url`，`bool`/`int`/`float` 保留原值」
  并补回归测试锁定，见第六十七轮。

## 第六十七轮（2026-09-22）：GOLD-003 —— Processor 可选接入常驻 Scheduler 并加固运行边界

### 1. 交付内容

- **常驻入口显式可选接线**（`scripts/run_collector_scheduler.py`）：
  新增 `--with-processor`（`store_true`，**默认关闭**）与纯函数
  `build_post_processor(enabled)`（关闭时返回 `None`）；`main()` 用
  `default_collector_factory(post_processor=...)` 把实例交给 bootstrap 注入采集器。
  未开启时注入 `post_processor=None` ⇒ 与 GOLD-001-R2 **零行为差异**；
  `src/scheduler/core.py` **零改动**（Processor 业务逻辑既不在 CLI、也不在调度核心）；
- **单源 Processor 故障的脱敏记录**（`src/collectors/base.py`）：采集层
  `_record_processing_warning` 统一负责「脱敏 → 写日志 → 记入运行摘要」，修掉了
  **真实缺陷**——旧实现把**未脱敏**的异常文本直接写日志，Processor 异常里带
  `api_key=...` 会把凭据落进日志文件；告警仍经 `collector.last_warnings` →
  `CollectorRunResult.warnings` → `job_runs.output_json.warnings`（Scheduler 侧再擦一遍），
  容量上限只限制留痕条数、不抑制日志；
- **`sanitize_mapping` URL 类键收紧**（`src/common/redaction.py`）：只有 `str` 才走
  `safe_url`（去 userinfo / query / fragment）；`None` 原样保留；`bool` / `int` / `float`
  **保留原值**、绝不 `str()` 伪造成字符串 URL；其它复杂对象丢弃。修复前
  `{"is_url": True}` → `"True"`、`{"feed_url": 123}` → `"123"`
  （即第六十六轮 §4 记录的「次要观察」，本轮闭环）；
- **契约类型微调**（`src/processors/collection/contracts.py`）：
  `PersistedItemProcessor` 的 `processor_name` / `processor_version` 由可变属性改为
  **只读 `@property`**，使 `CollectionProcessor`（property 实现）与测试替身（类属性实现）
  都满足协议，接线点不再需要 `# type: ignore`（mypy 全绿的必要条件）；
- **config security 测试与环境解耦**（`tests/unit/test_config_security.py`）：
  新增 `isolated_host_env` 夹具（删除宿主 `FRED_API_KEY` 等）后验证 defaults；
  并新增 `test_environment_variables_are_still_honored_for_secrets` 锁定「生产配置仍读取
  环境变量」不被削弱——第六十六轮 §4 记录的假失败不再出现，运行者无需手工清环境。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **10 项 Mock 测试**（零网络、零真实数据源、零外部站点）：
  - `tests/integration/test_scheduler_processor_wiring.py` **5**（新增文件）：默认关闭零行为差异 /
    `--with-processor` 注入真实 `CollectionProcessor` / 生产路径（真实 bootstrap + Scheduler +
    runner + Processor）`raw_items → processed_items` 闭环（含 `effective_at` 下界与凭据不落审计）/
    同槽与重复加工幂等 / 单源 Processor 故障隔离（原始数据不丢、其它源照常加工）+ 日志与审计脱敏；
  - `tests/unit/test_redaction.py` **+1**（共 27）：URL 类键的非字符串标量不被伪造成字符串 URL；
  - `tests/unit/test_collector_scheduler_cli_args.py` **+3**（共 6）：`--with-processor` 默认关闭 /
    显式开启、`build_post_processor(False) is None`、`build_post_processor(True)` 返回真实 Processor；
  - `tests/unit/test_config_security.py` **+1**（共 9）：环境变量仍被尊重（不削弱生产读取能力）；
- **负向验证**（确认回归测试真能拦住缺陷）：临时把采集层日志改回「未脱敏」形态后，
  `test_single_source_processor_failure_is_isolated_and_redacted` 立即失败
  （日志中出现 `api_key=SECRETVALUE-9f1c`），恢复修复后重新通过；
- **全量门禁**（宿主已导出 `FRED_API_KEY` 的情况下实测，证明环境脆弱性已消除）：
  - `pytest tests -q` → **1678 passed / 1 skipped in 200.38s**（收集 1679 项；唯一 skip 为
    `jieba` 已安装分支），stderr 为空；
  - `ruff check .` → `All checks passed!`；
  - `mypy config database src scripts` → `Success: no issues found in 134 source files`。

### 3. 范围守规

- 未新增依赖、未新增/修改 migration 与 schema、未进入 Phase 3.4、未训练 Alpha、
  未生成或执行任何实盘交易信号（`LIVE_TRADING=false` 门禁未触碰）；
- `src/scheduler/**` 未改动（接线点只在 CLI + bootstrap 注入处）；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`.env`；
- `TD-11` 维持「已解除」、`TD-12` 维持「部分解除」（未因本任务宣称全部解除）；
- 遗留（未修，超出本轮范围）：`processed_items` 缺 `error_message` 列（TD-19）、
  非行情数据集的 `data_versions` 快照（TD-12 剩余口径）等既有技术债照旧。


## 第六十八轮（2026-09-22）：GOLD-004 —— 建立 Collector 运行健康度与数据资格观测层

### 1. 交付内容

- **独立只读观测层**（`src/monitoring/`，Scheduler 核心**零改动**）：
  - `collector_health.py`：从 `collector_runs`（source 级运行事实）/ `raw_items`（实际采集）/`processed_items`（加工，经 `raw_items.source_id` 归因）/ `job_runs`（调度槽）/ `sources`
    （**只取**启用状态与 `config_json["collector"]` 派生值）汇总窗口内健康度：运行次数、
    SUCCESS/PARTIAL_FAILED/FAILED/在途、连败次数、最近成功时间、陈旧标记（复用
    `default_stale_after` = 90 分钟）、`inserted`/`duplicate` 上报值、实际原始条数、
    加工成功/拒绝/失败、加工观测状态；
  - `phase33_qualification.py`：**复用** `src/alpha/evidence_gate.py` 的阈值与判定
    （30 条可信帖子 / 200 事件 / 90 天 / 单源 40%），把 Author / News 资格缺口输出为机器可读
    结构（当前值、要求值、比较方式、PASS/BLOCKED、原因、证据时间范围），并**持续显式输出**
    blocker 代码 `PHASE3_3_DATA`（`blocker_active=true`、`ready=false`）；来源授权与
    "历史可用时间证据" 两项**恒为 BLOCKED**（人工 Gate，不得用 Mock/缺失数据判 PASS）；
  - `report.py`：组合报告（Markdown 人类可读 + 稳定 JSON），健康度与资格共用**同一审计时钟**。
- **CLI 入口** `scripts/report_collector_health.py`：默认人类可读；`--json` 稳定机器可读
  （`sort_keys` + `schema_version`）；`--window-hours` / `--stale-after-minutes` / `--as-of`
  可复现；`--report` **必须**配 `--no-dry-run` 才落盘（默认 dry-run，与项目其它脚本一致）；
  非法参数（窗口 ≤ 0、`--as-of` 无时区）由 `parser.error` 拒绝。
- **状态词汇表（绝不把 unknown 当 healthy）**：`HEALTHY` / `DEGRADED` / `FAILED` /
  `STALE` / `NEVER_RUN` / `NEVER_SUCCEEDED` / `UNKNOWN` / `DISABLED` / `NO_SOURCES`；
  空库 → `NO_SOURCES`；从未成功 → `NEVER_RUN`/`NEVER_SUCCEEDED`；只有在途 → `UNKNOWN`；
  连败 ≥ 3 或窗口内全部硬失败 → `FAILED`；`Processor NOT_OBSERVED`（有原始数据无加工结果）
  → 整体 `DEGRADED`。
- **脱敏（唯一实现复用）**：错误摘要经 `src/common/redaction.py::safe_text` 擦除
  （`api_key=***`）并截断；`base_url` 经 `safe_url` 去掉 userinfo / query / fragment；
  `sources.config_json` **整体不进入输出**（只输出 `collector` 派生名与布尔状态）。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **29 项**测试（全部 Mock / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_monitoring_health_report.py` **8**：连败口径（部分失败打断连败）、
    状态判定全分支、加工状态三态、凭据擦除与 URL query 剥离、空库 schema 稳定、
    `unknown ≠ healthy`、连败源 → 整体 FAILED、幂等重复运行（inserted=0 / duplicate>0）；
  - `tests/unit/test_phase33_qualification_report.py` **8**：库内门槛全 PASS 仍被人工 Gate
    拦下（PASS=5 / BLOCKED=2）、缺失证据绝不 PASS、阈值与 `evidence_gate` 一致、
    单源集中度 BLOCKED、证据时间范围逐 scope、机器可读结构稳定、渲染保留 blocker；
  - `tests/unit/test_monitoring_report.py` **2**：组合 JSON 结构、双章节渲染确定性；
  - `tests/integration/test_monitoring_health_integration.py` **11**：空库、窗口边界
    （恰好落在起点计入 / 更早排除）、连败 / 部分失败 / 在途 / 陈旧 / 从未运行 / 禁用、
    幂等重复运行与 Processor 未观测降级、调度槽汇总（含 `output_json` 不可解析计数与
    `source_id` 为空的未归因运行）、端到端脱敏（凭据 / `Bearer` / URL query 均不出现）、
    资格 blocker 与证据窗口、CLI `--json`/dry-run/`--no-dry-run` 落盘、非法参数退出码、加载器
    参数校验。
- **全量门禁**：
  - `pytest tests -q` → **1709 passed / 1 skipped in 203.94s**（唯一 skip 为 `jieba` 已安装分支）；
  - `ruff check .` → `All checks passed!`；
  - `mypy config database src scripts` → `Success: no issues found in 139 source files`。

### 3. 范围守规

- 未新增依赖、未新增/修改 migration 与 schema、未训练 Alpha、未生成交易信号或订单；
- `src/scheduler/**`、`src/alpha/**` 未改动（只**复用**其口径与阈值）；未进入 Phase 3.4；
- **未解除** `PHASE3_3_DATA` blocker（报告与 JSON 中持续显式输出，`ready` 恒为 false）；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`.env`；
- 同步 `README.md`（§1 交付表 + §7 新章节 + §10 TD-10 前置说明）与 `TECH_DEBT.md`
  （新增 TD-46：观测层剩余边界——无告警推送/无时间序列/`SKIPPED` 无法区分
  `DUPLICATE` 与 `REJECTED`）。


## 第六十九轮（2026-09-22）：GOLD-005 —— 建立合规授权数据 Evidence Intake Gateway

### 1. 交付内容

- **独立证据接收入口**（`src/evidence/`，与 Collector / Scheduler / Alpha 解耦；
  `src/scheduler/**`、`src/alpha/**` 零改动）：
  - `contracts.py`：版本化输入契约 `evidence-intake-v1`（字段注册表 + 别名 +
    Author / News 必填差异 + 原因码分组 + 授权依据白名单，与
    `src/alpha/source_authorization.py` **同一词汇表**）；
  - `validation.py`：逐行机械校验（授权声明 / 来源身份 / 时间语义 / 内容完整性 /
    历史可用证据 / 凭据防护），**纯函数、零 I/O**；输出脱敏原因与可选证据记录；
  - `intake.py`：validate-first / dry-run-first 的导入引擎（格式识别 JSONL / CSV、
    坏行隔离、批次内 + 跨批幂等判定、`IDENTITY_CONFLICT` 保护、append-only 落库、
    manifest / quarantine 载荷）；`report.py`：Markdown 渲染；
  - `ledger.py`：**只读**台账（只统计带 `raw_json["evidence"]` 标记的 `POST` / `NEWS`，
    普通导入 / Mock / 其它 item_type 一律不计）。
- **CLI** `scripts/intake_evidence.py`：`--scope author|news`、`--input`、`--format auto|jsonl|csv`、
  `--as-of`（必须带时区）、`--json`（stdout 纯 JSON，提示走 stderr）、
  `--manifest` / `--quarantine`（仅在 `--no-dry-run` 时落盘）、`--dry-run` / `--no-dry-run`；
  退出码 `0` 全部通过 / `2` 输入或参数错误 / `3` 无数据行 / `4` 有隔离行；
  拒绝 `--manifest` / `--quarantine` 与 `--input` 同一文件（含硬链接）。
- **时间与授权语义（本轮核心防泄漏/防伪造）**：
  - `published_at` / `collected_at` / `available_at` 必须带时区、不得未来、
    `collected_at` 必须晚于 `published_at`；`effective_at = max(published_at, collected_at)`；
  - `ingested_at` 由系统赋值，输入提供的值一律忽略（防止伪造审计时间）；
  - `published_at` 早于 `collected_at` **不等于**历史可用：必须有独立
    `available_at` + `availability_provenance` + `availability_reference`，
    且满足 `published_at ≤ available_at ≤ collected_at`；否则
    `AVAILABILITY_UNPROVEN` → `NOT_OOS_ELIGIBLE`（记录仍落库，但不计入 OOS 证据）；
  - 授权只有显式 `APPROVED` + 白名单依据 + 可核验引用 + 三项许可 + 人工签认人/时间，
    且 `collected_at` 落在授权有效期内才放行；缺失 / `UNKNOWN` / `DENIED` 一律隔离。
- **落库路径**：只写既有 append-only 路径——`raw_items`（证据元数据写入
  `raw_json.evidence`，另有扁平键供加工层审计）+ `processed_items`
  （复用 `CollectionProcessor`，processor 版本留痕）；**不**创建
  `authors` / `author_accounts` / `author_posts`（作者链接入仍走既有门禁）。
  自动创建的 `sources` 行一律 `enabled=false` 且不写 `config_json`（不注册采集器）。
- **与 GOLD-004 资格报告的最小集成**：`QualificationReport` 新增
  `evidence_intake` 子报告（Author / News 各一条 `>=` 检查，阈值复用
  `evidence_gate` 的 30 / 200），`QUALIFICATION_SCHEMA_VERSION` 升为 **2**；
  子报告 PASS **不改变**主检查，人工 Gate 项恒为 `BLOCKED`，`blocker_active` 恒 `true`。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **54 项**测试（全部 Mock / 临时文件 / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_evidence_contracts.py` **6**：契约版本 / 必填分组 / 别名映射 /
    原因码分组 / 授权词汇表 / 系统赋值字段；
  - `tests/unit/test_evidence_validation.py` **19**：合规行通过、授权缺失/未知/拒绝/无效引用、
    授权窗口（事后授权 / 过期）、出处缺失、naive/未来时间、`collected_at <= published_at`、
    空内容 / 仅内容引用、来源身份缺失、历史 CSV `NOT_OOS_ELIGIBLE`、可用性证据不自洽、
    `SCOPE_MISMATCH`、别名列映射、凭据隔离（值不回声）、`ingested_at` 忽略、
    News/Author 两类 scope、指纹确定性与内容敏感、标识列长度截断、`moment` 时区守卫；
  - `tests/unit/test_evidence_intake.py` **9**：格式识别、JSONL 坏行隔离、CSV 行号、
    文件 SHA-256 / 空输入、报告 JSON 结构稳定、quarantine 载荷、Markdown 渲染
    （含 `NOT_OOS_ELIGIBLE` 与 `PHASE3_3_DATA` 说明）、凭据不出现在报告/载荷；
  - `tests/unit/test_phase33_qualification_report.py` 新增 **3**（原 7 项保留，现共 10 项）：
    空台账恒 BLOCKED、子报告 PASS 也不解除 blocker（主检查 `PASS=5/BLOCKED=2` 不变）、
    认证但缺历史可用证据不得计入 OOS；
  - `tests/integration/test_evidence_intake_integration.py` **17**：dry-run 零写入
    （表计数 0）、提交写入 raw+processed（证据元数据 / 加工层扁平键）、重复导入幂等、
    内容冲突隔离且不覆盖历史、坏行原因码稳定、无法解析行 `ROW_UNREADABLE`、
    新建来源 `enabled=false` 且无 `config_json`、既有来源不被改写、凭据不入库不入报告、
    无可用证据记录落库但不计入 OOS、台账只计认证记录（普通导入 / `MACRO` 不计）、
    资格报告集成（计数可见但 blocker 不解除 / 空台账恒 BLOCKED）、
    CLI dry-run 零写入 + 提交落 manifest/quarantine、隔离清单原因码、
    退出码与参数校验（含 `--manifest` 覆盖输入、同目标）、CSV 别名列端到端。
- **全量门禁（本轮实测，项目 `.venv`）**：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1764 passed / 1 skipped in 211.83s**
    （唯一 skip 为 `jieba` 已安装分支）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 146 source files`。

### 3. 范围守规

- 未新增依赖、未新增/修改 migration 与 schema、未训练 Alpha、未生成交易信号或订单；
- `src/scheduler/**`、`src/alpha/**` 未改动（只**复用**其阈值与词汇表）；
- **未解除** `PHASE3_3_DATA`（资格报告持续显式输出，`ready` 恒为 false）；
- 未联网、未抓取微博 / Kitco / 任何站点，未触碰 `.env`；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节 + §10 阻塞说明）与 `TECH_DEBT.md`（新增 TD-47）。

## 第七十轮（2026-09-22）：GOLD-006 —— 证据就绪度与一键资格复核入口

### 1. 交付内容

- **operator 证据模板**（`src/evidence/templates.py` + `examples/evidence/`）：
  - `template --scope author|news --format csv|jsonl`：列 = 契约字段（去掉系统赋值列，
    保留可选 `available_at` / `availability_*`）+ 标记列 `record_kind,is_mock`；
  - 示例行**显式标记** `record_kind=example` / `is_mock=true`，其余字段填非法占位值
    （时间不是合法 ISO8601、`authorization_status=PENDING`、三项 `permits_*=false`）；
  - 新增 `ReasonCode.SYNTHETIC_EVIDENCE`（隔离类）：`assess_row` 命中标记列即整行隔离，
    **不写库、不计入 qualification ledger**；即使有人删掉标记列，示例行仍会因
    授权缺失 / 时间非法被隔离（双层防护）；
  - `write_template` 默认拒绝覆盖已存在文件，防止覆盖 operator 已填写内容。
- **只读证据台账扩展**（`src/evidence/ledger.py`）：`ScopeLedger` 新增 OOS eligible 记录的
  `source_counts`（来源名过 `safe_text`）与 `coverage_days` / `max_source_share` 属性。
- **就绪度 / preflight 报告**（`src/monitoring/evidence_readiness.py`）：
  - 阈值完全复用 `src/alpha/evidence_gate.py`（30 条 / 200 事件 / 90 天 / 单源 40%），
    不另造阈值、不修改 `src/alpha/**`；
  - 逐 scope 输出 `eligible_count` / `certified_count` / `not_oos_eligible_count` /
    `coverage_days` / `max_source_share`，以及每条检查的当前值、阈值、比较方式、状态、
    **remaining gap**、证据时间范围；无证据时 `evaluable=false`，集中度检查**不得判 PASS**；
  - 批次量化（dry-run）：`accepted` / `quarantined` / `duplicate` / `conflict`
    （`IDENTITY_CONFLICT`）/ `not_oos_eligible` + 稳定原因码计数；
  - `blocker_active` 与 `human_gate_required` **恒为 true**；稳定 JSON（`sort_keys`）。
- **CLI** `scripts/evidence_readiness.py`：`template` / `preflight` / `recheck` 三个子命令，
  默认只读、零网络、零写入（`--report` 必须显式配 `--no-dry-run`）；`recheck` 一键串联只读台账与
  现有 Phase 3.3 qualification report，JSON 含 `blocker_active`、`gate`
  （qualification PASS/BLOCKED 计数 + readiness 状态）、`readiness`（实际值 / 阈值 / remaining gap）
  与 `qualification` 全文；人类可读模式输出两份 Markdown；
  退出码 `0`（BLOCKED 也是正常结果）/ `2` 参数或输入错误 / `3` 输入没有数据行。
- **脱敏**：报告只含白名单标量，不读取 `sources.config_json`、不输出正文，
  来源名过 `safe_text`，token / API key / Authorization 一律 `***`。
- **operator 工作流**（README §7 与 `examples/evidence/README.md`）：
  prepare template → dry-run/preflight → inspect quarantine → explicit intake → qualification recheck。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **39 项**测试（全部 Mock / 临时文件 / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_evidence_templates.py` **12**：标记列契约一致性、模板列、CSV/JSONL 示例标记、
    格式校验、拒绝覆盖、仓库模板文件存在且可机械识别、示例行判 `SYNTHETIC_EVIDENCE` 隔离、
    删掉标记列后仍被隔离（授权 / 时间）；
  - `tests/unit/test_evidence_readiness.py` **15**：阈值与比较方式与 `evidence_gate` 一致、
    空库全 BLOCKED 且 remaining gap 准确、Author 可信 eligible 缺口、availability 不达标不计入、
    News 条数 / 覆盖天数 / 单源占比缺口、平衡场景量化 PASS **但不解除 blocker**、批次量化五项计数、
    稳定 JSON、来源名凭据脱敏、naive `as_of` 拒绝、Markdown 渲染、scope 过滤；
  - `tests/integration/test_evidence_readiness_integration.py` **12**：空库 preflight/recheck JSON、
    模板文件 preflight 全隔离且 DB 零写入、重复 / 内容冲突 / 授权缺失批次量化、有效输入 dry-run 零写入、
    授权提交后计数可见但 blocker 不解、News 三项达标仍 BLOCKED、凭据与 source config 不入报告、
    `--report` 默认不落盘、参数与输入错误退出码、`template` 打印 / 写入 / 拒绝覆盖、Markdown 报告。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1806 passed / 1 skipped in 223.38s**
    （唯一 skip 为 `jieba` 已安装分支）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 149 source files`。

### 3. 范围守规

- 未新增依赖、未新增/修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`；
- `src/alpha/**`、`src/scheduler/**`、`src/collectors/**` 未改动（只**复用**其阈值与口径）；
- **未解除** `PHASE3_3_DATA`（报告与 JSON 持续显式输出 `blocker_active=true`），未进入 Phase 3.4；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节 + §10 阻塞说明）、`TECH_DEBT.md`（新增 TD-48）与
  `examples/evidence/README.md`。

## 第七十一轮（2026-09-22）：GOLD-007 —— 单入口 Evidence Operator 工作流与 gateway-only 作者归属链

### 1. 交付内容

- **单入口 operator workflow**（`scripts/evidence_operator.py`；`workflow` 子命令一次串联
  `template → preflight → quarantine → intake → recheck`，另有 `template` / `preflight` /
  `quarantine` / `intake` / `recheck` / `author-chain` 单步命令）：
  - **默认 dry-run / 零网络 / 零写入**：`--report` / `--manifest` / `--quarantine` 必须显式配
    `--no-dry-run`；`recheck` 直接**复用** `scripts.evidence_readiness.py`（不另造第二套资格口径）；
  - **写入前二次验证**（`src/evidence/workflow.py::evaluate_write_gate`）：先跑一次完整 dry-run
    Evidence Gateway 校验并汇总写入门禁（`accepted` / `quarantined` / `duplicate` / `conflict` /
    `not_oos_eligible` / `synthetic` / `authorization_incomplete` / `time_invalid` /
    `identity_issues` / `sensitive_detected` / `availability_unproven`），只有 `ACCEPTED` 行
    append-only 落库；未授权 / 合成示例 / 时间非法 / 身份冲突 / 凭据 / 坏行一律隔离；
  - **隔离摘要**（`build_quarantine_summary`）：逐行给出状态 / 稳定原因码 / 已脱敏来源名 /
    记录 ID / 指纹前缀，供 operator 人工复核；不含正文与输入额外列值；
  - **稳定 JSON**：`workflow` 输出 `steps` / `preflight.readiness` / `write_gate` / `quarantine` /
    `intake` / `author_chain` / `qualification`，并显式带 `blocker_code` / `blocker_active=true` /
    `human_gate_required=true`；退出码 `0` / `2` / `3` /（`intake` 有隔离行时）`4`。
- **gateway-only 作者归属链**（`src/evidence/author_chain.py`）：
  - **唯一合法来源**：`raw_json.evidence` 带 `evidence-intake-v1` + `scope=author` +
    `oos_eligible=true` + 作者身份齐全的记录；`scripts/import_real_posts.py` 的普通 CSV 载荷
    只写 `import_kind=manual_real_corpus`（无证据块）→ **恒非候选**，无法绕过 gateway；
  - 幂等：同一 `raw_item` 只归属一次（`author_posts.raw_item_id` 唯一）；重复运行为 `duplicate`；
  - **不启用采集**：新建 `author_accounts` 一律 `enabled=false`（只建立归属）；
  - **身份冲突不覆盖**：同一 `(source, external_account_id)` 已归属其他作者时**跳过并上报**
    （`identity_conflicts`），绝不静默改写历史归属；
  - 沿用既有 `authors` / `author_accounts` / `author_posts` schema：**未新增 migration**。
- **`scripts/import_real_posts.py` 复用评估结论**：其作者归属链读取**普通 CSV**（无证据块），
  不可安全复用于 Phase 3.3 资格证据；因此**不改造**该脚本，而是新增 gateway-only 归属链，
  并把「历史 CSV 口径不适用于资格证据」写入 `TECH_DEBT.md` TD-49（记录技术债，未新增 schema）。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **23 项**测试（全部 Mock / 临时文件 / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_evidence_workflow.py` **5**：步骤名 / schema 版本；写入门禁对
    accepted / quarantined / duplicate / conflict / not_oos_eligible / synthetic /
    authorization / time / identity / sensitive / availability 的分类计数；无可写行时
    `write_allowed=false`；隔离摘要脱敏（`token=***` / `bearer ***`，指纹只留前缀）与空批次渲染；
  - `tests/unit/test_evidence_author_chain.py` **4**：普通 CSV / 历史样本 / Mock / 缺证据块
    **恒非候选**；只有契约 Author + OOS 证据才算候选；候选字段脱敏；报告 JSON 结构稳定且脱敏；
  - `tests/integration/test_evidence_operator_integration.py` **14**：单入口默认 dry-run 零写入 +
    blocker 恒 active；`--report` 默认不落盘、显式 `--no-dry-run` 才写；模板示例文件全部
    `SYNTHETIC_EVIDENCE` 隔离且台账 / DB 为 0；未授权 / 时间不足只隔离；显式 intake 落库 +
    manifest / quarantine + 台账只计 accepted；**重复显式 intake 幂等**（只产 DUPLICATE，不增长）；
    **身份冲突拒绝覆盖**（`raw_items.content_hash` 不变）；作者链忽略普通 CSV；作者链消费
    gateway 证据且幂等（账号 `enabled=false`）；作者链身份冲突跳过不覆盖；
    `workflow --author-chain` 只归属 gateway 证据；News `eligible_count` / `coverage_days` /
    `max_source_share` 缺口量化；单步 `quarantine` / `preflight` / `recheck` 与单入口同一套加固且零写入；
    凭据不出现在 JSON 与 Markdown；参数 / 输入错误退出码。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1831 passed / 1 skipped in 224.04s**
    （唯一 skip 为 `jieba` 已安装分支）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 152 source files`。

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/scheduler/**`、`src/collectors/**` 未改动
  （只**复用**其阈值与口径）；`scripts/import_real_posts.py` 未改造（改为登记技术债）；
- **未解除** `PHASE3_3_DATA`：所有输出持续显式 `blocker_active=true` / `human_gate_required=true`，
  未进入 Phase 3.4，未生成任何交易信号或订单；`LIVE_TRADING=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节）与 `TECH_DEBT.md`（新增 TD-49 + 变更日志行）。
- **遗留 / 下一步**：仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 BLOCKED。业务方须按
  `evidence-intake-v1` 提供真实授权的 Author / News 数据并**人工核验**授权与历史可用性，
  再用 `scripts.evidence_operator workflow --no-dry-run` 显式落库、`recheck` 显示量化门槛是否达标。



## 第七十二轮（2026-09-22）：GOLD-008 —— Evidence 人工交接包与可复核验收报告

### 1. 交付内容

- **只读交接包**（`src/evidence/handoff.py`）：
  - `build_handoff_report(readiness, *, batch=None, quarantine_reason_counts=None)`：
    **复用** `src.monitoring.evidence_readiness.EvidenceReadinessReport`
    （阈值来自 `src.alpha.evidence_gate`），把当前真实库内资格状态重排为确定性交接包，
    **不新造第二套阈值算法**；
  - `ScopeGap`：Author / News 的 `eligible` / `required` / `remaining`、`certified` /
    `not_oos_eligible`、`coverage_days` 缺口、`source_share_evaluable` 与 `remaining_checks`；
  - `ChecklistItem`：六类人工证据 checklist（authorization / provenance / published_at /
    collected_at / availability / identity），逐项给出契约字段、机械校验范围与
    `human_review_required=true`（机械校验 ≠ 法律效力核验）；
  - `ExcludedEvidence`：模板 / 示例（`SYNTHETIC_EVIDENCE`）、Mock / 演练语料、
    普通历史 CSV（`NOT_CERTIFIED`）、缺独立 `available_at`（`AVAILABILITY_UNPROVEN`）
    一律 `counts_toward_eligibility=false`（醒目标记不计资格）；
  - 稳定 JSON（`sort_keys`）+ 人类可读 Markdown（状态 / 缺口 / 隔离原因码 / checklist /
    不计资格 / 口径边界）。
- **CLI** `scripts/evidence_handoff.py`：
  - **默认只读 / 零网络 / 零写入**：不传 `--out` 只打印 stdout；`--input` 只做 dry-run
    （`session.rollback()` 显式回滚，零落库），**没有** `--no-dry-run`，不存在自动 intake；
  - 唯一写文件开关为 `--out`（显式路径），并拒绝把报告写到输入文件上；
  - **诚实 blocker 状态**：`blocker_active` / `human_gate_required` 恒 true，
    `data_qualification_passed` / `phase_transition_allowed` 恒 false；
    未达标 → `status=BLOCKED` / `ready_for_human_review=false` / 退出码 `5`；
    量化达标 → 最多 `status=BLOCKED_PENDING_HUMAN_REVIEW` / `ready_for_human_review=true` /
    退出码 `0`，**仍不**自动切 Phase（L3 人工确认）；
  - 退出码 `0` / `2`（参数或输入错误）/ `3`（无数据行）/ `5`（仍 BLOCKED）；
  - **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名 / 时间，
    正文与输入额外列值不进入输出。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **16 项**测试（全部 Mock / 临时文件 / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_evidence_handoff.py` **10**：空库诚实 BLOCKED 与缺口量化、
    部分达标（News PASS / Author BLOCKED）仍 BLOCKED、达阈值仅 `ready_for_human_review=true`、
    checklist 六类齐全且契约字段不漂移、不计资格项一律 false、稳定排序 / 确定性 JSON、
    敏感字段脱敏、Markdown 章节完整；
  - `tests/integration/test_evidence_handoff_integration.py` **6**：空库退出码 BLOCKED + 零写入、
    `--out` 是唯一写文件开关（默认不落盘）、候选文件 dry-run 给出隔离原因码且零落库、
    达标（Mock 证据块入库）仍要求人工复核、敏感值不泄漏、参数 / 输入错误退出码。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1849 passed / 1 skipped in 228.51s**
    （唯一 skip 为 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 154 source files`。

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/monitoring/**`、`src/scheduler/**`、
  `src/collectors/**` 未改动（只**复用**其阈值与口径）；`scripts/import_real_posts.py` 未改造；
- **未解除** `PHASE3_3_DATA`：所有输出持续显式 `blocker_active=true` /
  `human_gate_required=true`，`data_qualification_passed=false`，未进入 Phase 3.4，
  未生成任何交易信号或订单；`LIVE_TRADING=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节 + §10 说明）与 `TECH_DEBT.md`
  （新增 TD-50 登记行 + 明细 + 变更日志行）。
- **遗留 / 下一步**：仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 BLOCKED。本工具**只减少
  人工交接摩擦**，业务方仍须按 `evidence-intake-v1` 提供真实授权的 Author / News 数据并
  **人工核验**授权与历史可用性，再用 `scripts.evidence_operator workflow --no-dry-run`
  显式落库，并以 `scripts.evidence_handoff` / `recheck` 复核量化门槛；Phase 切换仍需 L3 人工确认。

## 第七十三轮（2026-09-22）：GOLD-009 —— Evidence Readiness 状态变更通知闭环

### 1. 交付内容

- **只读通知核心**（`src/evidence/readiness_watch.py`）：
  - `build_snapshot(handoff, *, generated_at)`：**复用** `src.evidence.handoff` 的
    `EvidenceHandoffReport`（阈值来自 `src.alpha.evidence_gate`），只提取**脱敏白名单**字段
    （`status` / `ready_for_human_review` / 各 scope `eligible` / `required` / `remaining` /
    coverage 缺口 / `source_share_evaluable` / 稳定原因码），计算 **SHA-256 指纹**；
    scope 名与原因码全部排序，且 **`generated_at` 不参与指纹**（时间戳变化不算状态变化）；
  - `detect_changes(previous, current)`：首次快照（`FIRST_SNAPSHOT`）、BLOCKED 缺口变化
    （`BLOCKER_GAP_CHANGED`，带逐字段 `scope_changes`）、原因码集合变化
    （`REASON_CODES_CHANGED`，带 added / removed）、`ready_for_human_review` 双向变化
    （`READY_FOR_HUMAN_REVIEW_ENABLED` / `READY_FOR_HUMAN_REVIEW_REVOKED`）；
    事件顺序由 `EVENT_ORDER` 固定；**完全相同状态重复运行 → 0 事件**（幂等）；
  - `WatchEvent.to_dict()` / `write_events()`：`blocker_active` / `human_gate_required` 恒为
    true，`data_qualification_passed` / `phase_transition_allowed` 恒为 false（**硬编码**，
    绝不被上游或被篡改的 state 透传影响）；
  - `load_snapshot_state()` / `write_snapshot_state()`：**原子写**（同目录临时文件 +
    `fsync` + `os.replace`）；state 为空 / JSON 损坏 / schema 不符 / **指纹校验失败** /
    声称 blocker 已解除一律抛 `SnapshotStateError`（**安全失败**，绝不静默当作首次快照）。
- **CLI** `scripts/evidence_readiness_watch.py`：
  - **默认只读 / 零网络 / 零写入**：不传 `--state` / `--out` / `--events` 时只打印 stdout
    （无历史可比，视为首次快照）；`--state` 只读，文件不存在 = 首次运行；
  - 只有显式 `--out`（快照）/ `--events`（事件）才写文件，且都是原子写；
    `--out` / `--events` / `--state` / `--input` 相互指向同一文件时参数错误退出 `2`；
  - `--input` 只做 dry-run（`session.rollback()` 显式回滚，零落库），**没有** `--no-dry-run`；
  - **不接邮件 / 短信 / Webhook / 第三方推送**；不写数据库、不新增 migration / schema；
  - 退出码 `0`（量化达标，仍需人工 Gate）/ `2`（参数或输入错误）/ `3`（无数据行）/
    `4`（state 损坏，安全失败且不写输出）/ `5`（仍未达标，诚实 BLOCKED）。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **20 项**测试（全部 Mock / 临时文件 / SQLite，零网络，未新增依赖）：
  - `tests/unit/test_evidence_readiness_watch.py` **13**：首次事件诚实、完全相同状态 0 事件、
    缺口变化、原因码双向变化、ready 双向变化（false→true 仍要求 L3 人工 Gate、
    `phase_transition_allowed=false`）、事件顺序确定性、敏感值脱敏、白名单字段
    （不含来源名 / notes / checklist）、序列化确定性、state 往返与原子替换、
    缺失 / 损坏 / 篡改 / 声称解锁的 state 安全失败、**CLI 退出码必须在 `__main__` 守卫之前定义**
    （回归：冒烟实测发现常量被放到守卫之后会导致直接运行 CLI 时 `NameError` 退出 1）；
  - `tests/integration/test_evidence_readiness_watch_integration.py` **7**：默认只读零写入、
    显式 `--out` / `--events` 才落盘且重复运行 0 事件（指纹稳定）、缺口变化事件、
    ready 双向事件、损坏 state 退出 `4` 且不写任何输出、候选文件 dry-run 脱敏且零落库、
    参数 / 输入错误退出码。
- **真实 CLI 冒烟**（临时 SQLite + 派生 schema，`DATABASE_URL` 指向临时库）：
  首轮 退出码 `5` / 1 个 `FIRST_SNAPSHOT` 事件 / `blocker_active=true`、
  `human_gate_required=true`、`data_qualification_passed=false`、
  `phase_transition_allowed=false`、author `0/30`、coverage remaining `90`；
  第二轮 退出码 `5` / **0 事件** / `changed=false` 且指纹与 state 文件一致；
  无残留 `.tmp`；损坏 state → 退出码 `4`、stdout 为空、不写任何输出、stderr 已脱敏；
  数据库 `raw_items=0` / `sources=0`（零写入）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1871 passed / 1 skipped in 225.63s**
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 156 source files`

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/monitoring/**`、`src/scheduler/**`、
  `src/collectors/**` 未改动（只**复用**其阈值与口径）；`scripts/import_real_posts.py` 未改造；
- **未解除** `PHASE3_3_DATA`：所有输出持续显式 `blocker_active=true` /
  `human_gate_required=true`，`data_qualification_passed=false`、
  `phase_transition_allowed=false`；未进入 Phase 3.4，未生成任何交易信号或订单；
  `LIVE_TRADING=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节 + §10 说明）与 `TECH_DEBT.md`
  （新增 TD-51 登记行 + 明细 + 变更日志行）。
- **遗留 / 下一步**：仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 BLOCKED。通知层**只减少
  盯盘 / 轮询**，不解除该 blocker；业务方仍须按 `evidence-intake-v1` 提供真实授权的
  Author / News 数据并**人工核验**，用 `scripts.evidence_operator workflow --no-dry-run`
  显式落库，再以 `handoff` / `recheck` 复核；Phase 切换仍需 L3 人工确认。

## 第七十四轮（2026-09-23）：GOLD-010 —— Evidence Readiness 单次本地 tick runner 与防重入闭环

### 1. 交付内容

- **单次 tick 核心**（`src/evidence/readiness_runner.py`，纯本地 / 零网络 / 零数据库写入）：
  - `SingleInstanceLock`：**OS 级独占文件锁**（Windows `msvcrt` / POSIX `fcntl`，零第三方依赖）
    防止同一工作目录的 tick 并发重入；锁文件内写入**可审计** `owner` / `pid` / `acquired_at`
    且不含敏感信息；正常退出**释放锁但不删除文件**；活动锁冲突 → `LockConflictError` 且
    **零写入**、**绝不删除 / 绝不改写**活动锁（OS 锁才是"是否活动"的唯一权威 →
    不可能"强删活动锁导致并发写 state"）；崩溃进程遗留的**陈旧锁**（含内容不可解析）
    可被安全接管，并留下 `recovered_stale=true` + `previous_owner` 审计痕迹；
  - `TickPaths`：三类 artifact 的固定文件名（`readiness_state.json` / `readiness_events.jsonl` /
    `readiness_status.json`）+ 锁文件，全部位于**显式配置的工作目录**内；
  - `run_tick(...)`：一次 tick 的完整链路 —— 取锁 → 读上次快照（GOLD-009 `load_snapshot_state`）
    → 读事件日志 → 计算快照（`SnapshotBuilder`，注入真实只读 builder 或测试双）→
    `detect_changes` → **先追加事件日志** → 再原子替换 state → 最后写 status；
    写入失败一律 `ArtifactWriteError`（fail-closed）；
  - `build_snapshot_from_session(...)`：**只读**台账（`load_evidence_ledger` → `build_readiness_report`
    → `build_handoff_report` → `build_snapshot`，与既有 CLI **同源**，不复制任何阈值 / 指纹 /
    事件算法），会话显式 `rollback`；
  - `load_event_journal` / `write_event_journal`：**有界保留**（保留最新 200 条，确定性滚动，
    丢弃条数记入 status 的 `journal.dropped`），**本次 tick 的新事件永不被丢弃**；
    加载时校验四个安全字段（被篡改即 fail-closed）；无变化时**不重写**既有日志；
  - `write_status` / `TickReport.to_dict()`：简洁 status，四个安全字段**硬编码**
    （`blocker_active=true` / `human_gate_required=true` / `data_qualification_passed=false` /
    `phase_transition_allowed=false`），含锁信息 / 事件数 / 指纹 / 缺口三元组；
  - `exit_code_for`：稳定退出码映射（`4` state 损坏 / `6` 锁冲突 / `7` 资格失败 /
    `3` 不可写 / 网络与数据库零写入）。
- **CLI** `scripts/evidence_readiness_runner.py`：`--work-dir` **必填**（唯一写入口）、
  可选 `--as-of`（ISO8601 必须带时区）/ `--json`；**只跑一次即返回**，不自带循环、
  不安装 / 不修改 OS 计划任务；失败路径 stdout 为空、stderr 已脱敏。
- **复用而非复制**：`src/evidence/readiness_watch.py` 的私有原子写原语改为**公开**
  `atomic_write_text`，runner 直接复用（不各写一套）；`src/evidence/__init__.py` 导出新 API。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **30 项**测试（全部临时文件 / 临时 SQLite / Mock 证据块，零网络，未新增依赖）：
  - `tests/unit/test_evidence_readiness_runner.py` **23**：退出码映射、工作目录派生、
    正常 tick（三类 artifact + 原子写无残留 `.tmp`）、锁结束即释放且可重取、
    连续无变化 tick 幂等（0 事件且日志逐字节不变）、变化只追加新事件、
    **活动锁冲突零写入且不动活动锁**、陈旧锁安全接管（可审计留痕）、
    锁文件不可解析仍可接管、锁幂等获取、损坏 state / 被篡改 state（声称解锁）、
    损坏事件日志、事件日志安全字段被篡改、缺失日志、资格计算失败（脱敏，异常正文不入输出）、
    builder 返回类型非法、artifact 写入失败（事件优先 + 重放 + 无残留临时文件）、
    工作目录是文件、达标时安全字段不变且要求 L3 人工确认、artifact 与摘要全面脱敏、
    有界保留不丢本次新事件、**源码守卫**（runner 模块无任何循环、无 scheduler /
    subprocess / threading 依赖，CLI 无 `while` / `time.sleep`）；
  - `tests/integration/test_evidence_readiness_runner_integration.py` **7**：真实 CLI +
    临时 SQLite —— 只读且幂等（含 `raw_items` / `sources` 零写入断言）、
    达标事件（安全字段不变、退出码 `0`）、锁冲突退出 `6` 且零写入、
    陈旧锁接管（`previous_owner` 审计）、损坏 state 退出 `4` 且保留原文件、
    资格计算失败退出 `7` 且不泄露连接串 / 凭据、参数错误退出 `2`。
- **真实 CLI 冒烟**（临时 SQLite + 派生 schema，`DATABASE_URL` 指向临时库，两个真实进程）：
  第 1 次 退出码 `5` / 1 个 `FIRST_SNAPSHOT` / `blocker_active=true`、
  `human_gate_required=true`、`data_qualification_passed=false`、
  `phase_transition_allowed=false`、author `0/30`、news `0/200`；
  第 2 次 退出码 `5` / **0 事件** / `changed=false` / 指纹与 state 一致 / 日志仍 1 行 /
  锁被安全接管（`recovered_stale=true` + 上一位 owner 留痕）；
  目录内只有 4 个 artifact、**无残留 `.tmp`**；损坏 state → 退出码 `4`、原文件保留、status 未变；
  并发持锁运行 → 退出码 `6`、stdout 为空、持锁进程仍能读到自己的 owner 记录；
  数据库 `raw_items=0` / `sources=0`（零写入）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1903 passed / 1 skipped in 226.40s**
    （唯一 skip 为 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；
    本轮新增 30 项，基线 1873 项全部保持通过）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 158 source files`（GOLD-009 为 156，本轮 +2 个新模块文件）。

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema（仅本地文件型 artifact）、未联网、
  未抓取任何站点、未触碰 `.env`、**未安装 / 未修改任何 OS 计划任务**；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/monitoring/**`、`src/scheduler/**`、
  `src/collectors/**` 未改动（只**复用**其阈值与口径）；`src/evidence/readiness_watch.py`
  仅把私有原子写原语改为**公开** `atomic_write_text`（行为不变，GOLD-009 的 19 项测试全绿）；
- **未解除** `PHASE3_3_DATA`：status / 事件持续显式 `blocker_active=true` /
  `human_gate_required=true`，`data_qualification_passed=false`、
  `phase_transition_allowed=false`；未进入 Phase 3.4，未生成任何交易信号或订单；
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表 + §7 新章节「Evidence Readiness 单次本地 tick runner」
  含**仅供人工配置**的任务计划程序 / `schtasks` 示例 + §10 说明）与 `TECH_DEBT.md`
  （新增 TD-52 登记行 + 明细 + 变更日志行）。
- **遗留 / 下一步**：仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 BLOCKED。runner **只把盯盘
  自动化**（把"人工反复执行"换成"外部定时器调用一次"），不解除该 blocker；业务方仍须按
  `evidence-intake-v1` 提供真实授权的 Author / News 数据并**人工核验**，用
  `scripts.evidence_operator workflow --no-dry-run` 显式落库，再以 `handoff` / `recheck` 复核；
  调度方式由**人工**配置（README 给出示例，工具不会自动安装计划任务）；Phase 切换仍需 L3 人工确认。


## 第七十五轮（2026-09-23）：GOLD-011 —— 授权 Evidence 本地 Inbox 发现与预检闭环

### 1. 交付内容

- **inbox 核心**（`src/evidence/inbox.py`，纯本地 / **只读发现** / 零网络 / 零数据库写入）：
  - `scan_inbox(...)`：**只读**扫描显式 inbox 目录的**直接子目录**；符号链接子目录列入
    `skipped`（**不跟随**）；inbox 根目录下的散落文件按 `MANIFEST_MISSING` 隔离提示
    （**不移动 / 不删除**）；输出按内容指纹确定性排序；
  - `_layout(...)`：对候选包做**内容级**目录快照（`sha256:<hex>` / `symlink` / `dir` /
    `other` / `unreadable`）；**不读 mtime**、**不跟随符号链接**、子目录与非常规条目一律记
    稳定原因码；
  - `_package_fingerprint(...)`：候选包指纹只由**文件内容摘要与结构标记**派生（排序、确定性），
    **不含**文件名 / mtime / 绝对路径 / 扫描时间；测试锁定「改 mtime / 换审计时点都不改变指纹」；
  - `_read_manifest` / `_parse_declarations` / `_parse_file_entries`：`manifest.json` 必填
    `evidence_type`（author|news）/ `source`（或别名 `provider`）/ `authorization_reference` /
    `time_semantics` / `availability_semantics` / `historical_oos_applicable`（bool）/
    `files`（`path` + `sha256` + 可选 `format`）；缺失 / 类型错误 / schema 或 contract 版本
    不符 / 证据类型不支持 → 稳定 `InboxReasonCode`；
  - `_check_files`：声明路径必须是**包内单级文件名**（拒绝绝对路径 / `..` / 多级 / 盘符 /
    UNC）；**只依据包内内容摘要判定**（绝不越界读取）；逐文件核对 64 位 `sha256` 与内容是否
    一致（不一致即隔离且**不再解析内容**）；包内除 manifest 外的文件必须**全部**声明，
    否则 `EVIDENCE_FILE_UNDECLARED`；
  - `_preflight_rows`：**复用 Evidence Gateway** 的 `assess_row`（`evidence-intake-v1`）做
    逐行机械校验（授权 / 时间 / availability / 隔离原因码与 `intake` **完全同源**），汇总
    `rows` / `acceptable` / `quarantined` / `not_oos_eligible` 与稳定原因码计数；
    `DUPLICATE` / `IDENTITY_CONFLICT`（需要读库）留给**显式 intake**；
  - `_sensitive_reasons`：凭据类**键名**只记录键名（`authorization_reference` 等契约字段按
    intake 的列白名单豁免，避免"名字含 authorization 被自伤"），值级凭据（`token=` /
    `api_key=`）只记录字段名；引用类字段用 `safe_url` 去掉 query / userinfo；绝不输出 evidence
    正文；
  - `CandidatePackage` / `InboxPreflightReport`：稳定、脱敏的 `discovered` /
    `already_pending` / `status_changed` / `preflight_pass` / `quarantined` / `skipped` /
    `requires_human_action`；四个安全字段**硬编码**为 true / true / false / false；
  - `PendingEntry` / `PendingRegister` / `load_pending_register` / `write_pending_register`：
    pending 清单以内容指纹为键（**当前 inbox 内容的镜像**）：同内容重复扫描**不重复生成**、
    `first_seen_at` 保留、只推进 `scan_count` / `last_seen_at`；内容变化 → 新指纹（新条目）；
    同一指纹状态变化记入 `status_changed`；既有 state 损坏 / 被篡改 → **严格校验 fail-closed**；
    清单**原子写**（复用 GOLD-009 的 `atomic_write_text`）；
  - `run_inbox_scan(...)`：只有显式 `out_path` 才写文件，且先取**单实例锁**
    （复用 GOLD-010 的 `SingleInstanceLock`）再读 state；输出 / 状态**必须位于 inbox 之外**
    （否则会被下一次扫描判为未声明文件）；`exit_code_for` 复用 GOLD-010 的退出码取值。
- **CLI** `scripts/evidence_inbox.py`：`--inbox-dir` **必填**、`--state` 只读、`--out` 唯一写
  开关、`--lock` 可选（缺省 `<out>.lock`）、`--as-of`（ISO8601 必须带时区）、`--json`；
  **只跑一次即返回**；失败路径 stdout 为空、stderr 已脱敏。
- **复用而非复制**：`src/evidence/validation.py` 的私有引用校验原语改为**公开**
  `valid_reference`（inbox 与 Evidence Gateway 共用**同一**口径，行为不变）；
  `src/evidence/__init__.py` 导出新 API。


### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **65 项**测试（临时目录 / Mock 证据块 / **无数据库** / **零网络**，未新增依赖）：
  - `tests/unit/test_evidence_inbox.py` **55**：退出码映射、空 inbox、缺失目录、无时区时点
    拒绝、合法 JSONL / CSV（含 `provider` 别名）、行级隔离复用 gateway 原因码、
    缺可用性证据、缺 manifest、manifest 损坏（缺省 / 非对象，参数化）、缺必填声明
    （7 个字段参数化并逐字段期望原因码）、类型 / 版本不符、引用非法、摘要不一致 / 非法、
    未声明文件、路径穿越 / 绝对路径 / 盘符 / UNC（参数化）、空 / 空白 `path`、
    符号链接文件与符号链接候选包、示例模板（4 种标记参数化）、示例命名包、空证据文件、
    部分隔离整包 fail-closed、重复扫描幂等（`first_seen_at` 保留）、内容变更 → 新指纹、
    同指纹状态变化 → `status_changed`、mtime / 扫描时间不参与指纹、pending 原子写、
    输出写在 inbox 内被拒、损坏 state fail-closed、state 不能削弱硬编码安全字段、
    锁冲突零写入、全面脱敏，以及**源码守卫**（无 `aiohttp` / `httpx` / `requests` /
    `socket` / `urllib` / `sqlalchemy` / `subprocess`；无 `unlink` / `rmtree` /
    `os.remove` / `os.rename` / `shutil.move`；无 `intake_evidence`；无 `while` 循环）；
  - `tests/integration/test_evidence_inbox_integration.py` **10**：真实 CLI —— 空 inbox 退出
    `5` 且零写入、合法候选退出 `0` + pending 原子落盘 + 幂等重扫（`discovered=0`、
    `first_seen_at` 保留、`scan_count` 递增）+ **原始 evidence 字节级未被改写**、
    隔离候选退出 `5` + 稳定原因码、默认 Markdown 摘要保留 blocker 与安全字段、
    损坏 state 退出 `4` 且旧文件原样保留、锁冲突退出 `6` 且零写入、把 `--out` 写进 inbox
    退出 `3`、inbox 目录缺失退出 `3`、参数错误退出 `2`，以及**真实子进程**
    `python -m scripts.evidence_inbox` 端到端冒烟（stdout 为纯 JSON）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **1970 passed / 1 skipped in 232.16s**
    （唯一 skip 仍是 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；
    本轮新增 65 项：单元 55 + 集成 10；GOLD-010 记录的基线为 1903 passed / 1 skipped）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 160 source files`（GOLD-010 为 158，本轮 +2 个新模块文件）。

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`、
  未安装 / 未修改任何 OS 计划任务；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/monitoring/**`、`src/scheduler/**`、
  `src/collectors/**`、`src/processors/**` 未改动（只**复用**其契约 / 校验 / 阈值 / 原因码）；
  `src/evidence/validation.py` 仅把私有引用校验改为**公开** `valid_reference`（行为不变，
  GOLD-005 / GOLD-006 的既有测试全绿）；`src/evidence/readiness_runner.py` 只被**复用**
  （单实例锁与退出码取值），未改动；
- **未解除** `PHASE3_3_DATA`：预检报告、pending 清单与人类可读摘要持续显式
  `blocker_active=true` / `human_gate_required=true`、`data_qualification_passed=false`、
  `phase_transition_allowed=false`、`requires_human_action=true`；未进入 Phase 3.4，
  未生成任何交易信号或订单；`LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；
- 同步 `README.md`（§1 交付表新增一行 + §7 新章节「Evidence 本地 Inbox 发现与预检」，
  含最小目录 / manifest 示例、CLI 用法、安全拒绝清单、退出码与**显式 intake** 步骤 +
  §10 仍未解除说明）与 `TECH_DEBT.md`（新增 **TD-53** 登记行 + 明细 + 变更日志行）。
- **遗留 / 下一步**：仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。inbox **只做
  "摆放与预检"**（把"候选放哪里、如何确定性发现、manifest 是否齐全、摘要是否一致"补齐），
  **不是**资格判定器、也不具备解除 blocker 的能力；业务方仍须按 `evidence-intake-v1` 提供真实
  授权的 Author / News 数据（放入 inbox 子目录 + `manifest.json`），经
  `scripts.evidence_inbox --out` 预检与**人工核验**后，用
  `scripts.evidence_operator workflow --no-dry-run` 显式落库，再以 `handoff` / `recheck`
  复核；Phase 切换仍需 L3 人工确认。


## 第七十六轮（2026-09-23）：GOLD-012 —— Evidence Inbox 人工复核决策与审计闭环

### 1. 交付内容

- **review 核心**（`src/evidence/review.py`，纯本地 / **零网络** / 零数据库写入）：
  - `ReviewDecision`（`APPROVE` / `REJECT` / `NEEDS_CHANGES`）与 `ReviewReasonCode`
    （受控词表；`REASON_CODES_BY_DECISION` 按决策分组，跨决策使用即参数错误）；
  - `record_review_decision(...)`：只对**显式给出的 64 位内容级指纹**记录决策 —— `APPROVE`
    必须 ①该指纹**当前仍在** inbox 扫描结果中、②当前预检 `PREFLIGHT_PASS`、③**不是**
    模板 / 示例 / Mock；内容变化 → 新指纹（**旧批准绝不继承**），候选消失 / 预检回退 /
    元数据非法一律 **fail-closed**（零写入）；
  - 最小审计元数据：`reviewer`（非敏感标识，必填）、`reviewed_at`（必须带时区且不得晚于审计
    时点）、`reason_code`、可选非敏感 `note`；**凭据类内容一律拒绝记录**（不做「擦一擦再落盘」，
    避免把 `***` 写进审计历史）；
  - `ReviewRecord` / `ReviewLedger`：**追加式**（只 append，**绝不静默覆盖**）；`decision_id`
    由（指纹 / 决策 / revision / 原因码）**确定性**派生（篡改即 fail-closed）；同一指纹 +
    **完全相同**的决策内容重复提交**幂等**（不新增记录、不改写历史）；任何差异必须显式
    `revision = 既有 + 1` + `override`，`supersedes` 指向被取代的决策，**历史全部保留**；
    恢复时严格校验（文档标识 / schema / 契约版本 / 安全字段 / `decision_id` / revision 连续性 /
    `supersedes` 链 / 原因码与决策匹配 / 时区）；
  - `build_approved_intake_list(...)`：在**当前**扫描结果上**重新验证**每条 `APPROVE`
    （候选仍在、仍 `PREFLIGHT_PASS`、非合成、摘要与复核时一致），生成**脱敏**的
    `approved-for-explicit-intake` 清单；失效批准进 `invalidated`（`CANDIDATE_MISSING` /
    `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` / `EVIDENCE_INCONSISTENT`），
    **绝不因为「曾经批准过」就放行**；
  - `run_review(...)`：默认**只读**；只有显式 `out_path` 才写 ledger，且先取 GOLD-010 的
    **单实例锁**再读 ledger（锁冲突 → 零写入）；`approved_out_path` 才写批准清单；
    落盘顺序「批准清单（派生）→ ledger（唯一事实来源）」，两者都复用 GOLD-009 的**原子写**；
  - `render_review_summary(...)` / `exit_code_for(...)`：脱敏的人类可读摘要与稳定退出码；
    四个安全字段（`blocker_active` / `human_gate_required` / `data_qualification_passed` /
    `phase_transition_allowed`）**硬编码**，与 approve 数量无关。
- **CLI** `scripts/evidence_review.py`：`--inbox-dir` **必填**、`--ledger` 只读、`--out` 唯一
  ledger 写开关、`--approved-out` 批准清单、`--decision` / `--fingerprint` / `--reviewer` /
  `--reason-code` / `--note` / `--reviewed-at` / `--revision` / `--override` / `--lock` /
  `--as-of` / `--json`；**只跑一次即返回**；失败路径 stdout 为空、stderr 已脱敏。
- **复用而非复制**：`src/evidence/inbox.py` 的路径守护原语改为**公开** `ensure_outside_inbox`
  （复核层复用**同一**口径，行为不变，GOLD-011 既有测试全绿）；`src/evidence/__init__.py`
  导出新 API。


### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **62 项**测试（临时目录 / Mock 证据块 / **无数据库** / **零网络**，未新增依赖）：
  - `tests/unit/test_evidence_review.py`（**52 项** / 38 个测试函数，含参数化）：退出码映射与
    `decision_id` 确定性；受控词表（未知决策 / 原因码与决策不匹配 / 指纹非法 / reviewer 为空 /
    note 超长）；`approve` 门禁（隔离候选 / 4 类模板示例标记 / 指纹不存在）；`reject` 与
    `needs_changes` 可对已隔离候选记录；reviewer / note 含凭据**拒绝记录**（零写入）；
    `reviewed_at` 时区与"不得晚于审计时点"；**幂等**（完全相同决策重复提交 → ledger 字节级不变、
    不新增记录）；**冲突**（缺 revision / 缺 override / 跳号 → fail-closed；显式 revision +
    override → 追加新 revision 且 `supersedes` 留痕、历史全保留）；ledger 原子写与自描述、
    参数化篡改（文档标识 / schema / 契约版本 / 4 个安全字段 / decisions 非数组 / 记录非法 /
    缺 `generated_at`）与不可读 ledger、**篡改 `decision_id` / revision 断链 / supersedes 断链 /
    原因码不匹配 / 无时区 reviewed_at** 全部 fail-closed；损坏 ledger 零写入；锁冲突零写入；
    只读运行零写入且原始 evidence 字节不变；输出写在 inbox 内被拒；**内容变化**导致旧批准失效
    （`CANDIDATE_MISSING`）且对旧指纹 approve 失败；**候选消失**失效；**预检回退**（同一指纹在
    较早审计时点隔离）失效；批准清单只含当前仍成立的批准（含 `decision_counts`）；脱敏（引用去
    query、`SECRET` 不出现）；**复核元数据不含任何证据时间字段**（对 `FORBIDDEN_EVIDENCE_FIELDS`
    递归断言 + 行内证据时间值不得被复制）；批量 approve 不改变四个安全字段；Markdown 摘要保留
    blocker / human gate / 显式 intake 命令；多线程**并发写**不产生半写文档；以及**源码守卫**
    （无 `aiohttp` / `httpx` / `requests` / `socket` / `urllib` / `sqlalchemy` / `subprocess`；
    无 `unlink` / `rmtree` / `os.remove` / `os.rename` / `shutil.move`；无 `intake_evidence`；
    无 `while` 循环；`atomic_write_text` 是唯一写路径）；
  - `tests/integration/test_evidence_review_integration.py`（**10 项**）：真实 CLI —— 空 inbox
    只读退出 `5` 且零写入；approve 退出 `0` + ledger / 批准清单原子落盘 + **幂等重跑**
    （`DECISION_IDEMPOTENT`、ledger 字节级不变、原始 evidence 字节不变）；内容变化 → 旧批准失效
    且对旧指纹 approve 退出 `4`（stdout 为空、零写入）；冲突退出 `4`，显式 revision + override
    退出 `0` 且 `decision_count=2`；参数 / 词表错误退出 `2`（含 `--override` 缺 `--revision`、
    `--revision` 缺 `--decision`、原因码与决策不匹配）；inbox 缺失 / `--out` 写进 inbox 退出 `3`；
    损坏 ledger 退出 `4` 且旧文件原样保留；锁冲突退出 `6` 且零写入；Markdown 摘要保留 blocker；
    以及**真实子进程** `python -m scripts.evidence_review` 端到端冒烟（stdout 为纯 JSON）。
- **人工端到端冒烟**（真实 CLI 子进程，7 步，独立于 pytest）：inbox 预检 → 只读列出（`5` /
  `LIST_ONLY` / 零写入）→ approve（`0` / `approved_count=1` / 四个安全字段恒定）→ 重复 approve
  （`0` / `DECISION_IDEMPOTENT` / ledger 字节不变）→ 冲突 reject（`4` / fail-closed / ledger 不变）
  → 显式 `--revision 2 --override`（`0` / `decision_count=2` / `supersedes` 正确）→ 内容变化后再
  approve 旧指纹（`4` / 无继承）与只读列出（`5` / `approved=0`）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2034 passed / 1 skipped in 230.03s**
    （唯一 skip 仍是 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；
    本轮新增 62 项：单元 52 + 集成 10；GOLD-011 记录的基线为 1970 passed / 1 skipped —— 本轮
    **未修改任何既有测试文件**，剩余 2 项差值来自既有测试的收集口径，非本轮改动）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 162 source files`（GOLD-011 为 160，本轮 +2 个新模块文件）。


### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`、
  未安装 / 未修改任何 OS 计划任务；
- `src/alpha/**`（含阈值 `evidence_gate.py`）、`src/monitoring/**`、`src/scheduler/**`、
  `src/collectors/**`、`src/processors/**` 未改动（只**复用**其契约 / 校验 / 阈值 / 原因码）；
  `src/evidence/inbox.py` 仅把私有路径守护改名为**公开** `ensure_outside_inbox`（行为不变，
  GOLD-011 既有测试全绿）；`src/evidence/readiness_runner.py` / `readiness_watch.py` 只被**复用**
  （单实例锁、退出码取值与原子写原语），未改动；
- **未解除** `PHASE3_3_DATA`：reports / ledger / 批准清单与人类可读摘要持续显式
  `blocker_active=true` / `human_gate_required=true`、`data_qualification_passed=false`、
  `phase_transition_allowed=false`；approve 数量（含全部候选被 approve）**不会**自动改变任何
  安全字段；未进入 Phase 3.4，未训练 Alpha、未生成任何交易信号或订单；
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）；
- 同步 `README.md`（§1 交付表新增一行 + §7 新章节「Evidence Inbox 人工复核决策与审计」，
  含 CLI 用法、approve 门禁、追加式 ledger 语义、批准清单失效口径、退出码与
  **inbox scan → human review → approved list → 显式 intake** 最小操作路径 + §10 仍未解除说明）、
  `TECH_DEBT.md`（新增 **TD-54** 登记行 + 明细 + 变更日志行）与 `PROGRESS_LOG.md`（本轮）。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。review 层**只是**「谁批了哪一版
  内容、为什么、是否被推翻」的**本地审计入口**：它把人工决策变成可复核、不可静默覆盖、
  可重新验证的 artifact，但**不是**资格判定器，也不具备解除 blocker 的能力；
- 业务方按 `evidence-intake-v1` 提供真实授权的 Author / News 数据（放入 inbox 子目录 +
  `manifest.json`）→ `scripts.evidence_inbox --out` 发现与预检 → 以本工具逐指纹
  `--decision approve ... --out <ledger> --approved-out <list>` 记录人工决策并生成批准清单 →
  人工按清单**显式**执行 `scripts.evidence_operator workflow --no-dry-run` 落库 →
  `handoff` / `recheck` 复核；Phase 切换仍需 **L3 人工确认**；
- 复核元数据只接受**非敏感**最小字段（凭据类内容一律拒绝记录）；复核时间 / reviewer / note /
  文件名 / mtime / 「曾被人批准」都**不是**证据时间，本层产量中不存在这些证据字段。



## 第七十七轮（2026-09-23）：GOLD-013 —— Approved Evidence 显式 Intake Plan 与最终写入前门禁

### 1. 交付内容

- **intake plan 核心**（`src/evidence/intake_plan.py`，纯本地 / **零网络** / 零数据库写入）：
  - `IntakePlanStatus`（`READY_FOR_EXPLICIT_INTAKE` / `BLOCKED_NO_APPROVED_EVIDENCE`）与
    `PlanVerificationCode`（14 个稳定原因码：清单 tamper / 计数 / 时间、ledger 缺失 / 被推翻 /
    旧 revision、指纹消失 / 预检回退 / 合成证据 / 摘要不一致）；
  - `ApprovedListDocument` + `load_approved_intake_list(...)`：GOLD-012 批准清单的**严格只读视图**
    —— 文档标识 / schema / 契约版本 / `approved_count` / `invalidated_count` 与列表长度一致 /
    四个安全字段与 `approval_scope` 不可被 state 削弱 / `requires_explicit_intake` 不可被改成
    false / **任何证据时间字段一律拒绝加载**（`IntakePlanStateError`，fail-closed）；
  - `verify_intake_plan_inputs(...)` + `build_intake_plan(...)`：把批准清单与**当前** inbox /
    ledger **重新绑定核验** —— ①每条批准必须**当前仍是** ledger 上该指纹的**最新有效**决策
    （`revision` / `decision_id` 完全一致；`LEDGER_MISSING_APPROVAL` /
    `LEDGER_DECISION_SUPERSEDED`（**review override** 后的旧批准）/ `LEDGER_REVISION_SUPERSEDED`）；
    ②该指纹**当前仍在** inbox 扫描结果中且仍 `PREFLIGHT_PASS`、**不是**模板 / 示例 / Mock、
    摘要与复核时一致（`FINGERPRINT_MISSING` / `PREFLIGHT_NOT_PASSING` / `SYNTHETIC_EVIDENCE` /
    `EVIDENCE_INCONSISTENT`）；③清单必须与"用**当前** inbox + ledger 重新算出的批准集合"
    **完全一致**（条目集合 / 失效集合 / `approved_count` / `decision_counts`：
    `APPROVED_LIST_STALE` / `APPROVED_LIST_TAMPERED` / `INVALIDATED_MISMATCH` / `COUNT_MISMATCH`）；
    ④计划审计时点不得早于清单生成时间（`PLAN_TIME_BEFORE_APPROVAL`）；任一不一致 →
    `IntakePlanInconsistentError`（**fail-closed**，零写入，绝不产出"看起来可以落库"的计划）；
  - **复用而非复制**：批准集合的重新验证**直接调用** GOLD-012 的 `build_approved_intake_list`
    （同一 inbox 预检与同一 ledger 语义），**不复制、不降低**任何资格规则；
  - `compute_plan_id(...)`：**内容级** `plan_id`（只由策略块 + 批准条目 + ledger 最新决策摘要
    派生，**不含** `generated_at`）→ 同一输入重复生成得到同一 `plan_id`、同审计时点逐字节稳定；
    输入内容或 review revision 变化必然产生新 `plan_id`（旧计划 / 旧批准**绝不静默继承**）；
  - `handoff`：每个证据文件一条**字符串**命令模板（必带 `--no-dry-run` 与显式 `--input`，并给出
    `input_path`），仅用于人工复制执行；`auto_intake_allowed` / `writes_database` 恒为 false、
    `requires_explicit_operator_action` 恒为 true；
  - `run_intake_plan(...)`：默认**只读**；只有显式 `out_path` 才写**计划本身**（先取 GOLD-010 的
    **单实例锁**，再复用 GOLD-009 的 `atomic_write_text` 原子落盘，无残留 `.tmp`）；所有输入 /
    输出必须在 inbox **之外**；`render_intake_plan_summary(...)` / `exit_code_for(...)` 给出脱敏
    Markdown 与稳定退出码；
  - **明确区分 ≠ 资格**：`approved_for_explicit_intake` 与 `data_qualification_passed` 是两个
    **独立**字段；后者恒为 `false`、`data_qualification_passed_count` 恒为 `0`；四个安全字段
    （`blocker_active` / `human_gate_required` / `data_qualification_passed` /
    `phase_transition_allowed`）**硬编码**，与批准数量无关；`PHASE3_3_DATA` 保持 **BLOCKED**。
- **CLI** `scripts/evidence_intake_plan.py`：`--inbox-dir` / `--ledger` / `--approved-list` **必填**、
  `--out` 唯一写开关、`--lock` / `--as-of`（必须带时区）/ `--json`；**没有**任何 intake /
  `--no-dry-run` 参数（试图传入 → argparse 退出码 `2`）；**只跑一次即返回**；失败路径 stdout
  为空、stderr 已脱敏。
- **导出**：`src/evidence/__init__.py` 增加 GOLD-013 的模块说明与 30 余个新 API 导出。
- **文档**：`README.md`（§1 交付表新增 GOLD-013 行、§7 新增「Evidence Intake Plan」章节含用法 /
  fail-closed 口径 / 退出码 / **inbox 扫描 → 人工复核 → 批准清单 → intake plan → 人工显式
  Evidence Operator intake → handoff/recheck** 最小路径、§10 明确仍未解除）、`TECH_DEBT.md`
  （新增 **TD-55** 登记行 + 明细 + 变更日志行）、`PROGRESS_LOG.md`（本轮）。


### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **57 项**测试（临时目录 / Mock 证据包 / **无数据库** / **零网络**，未新增依赖）：
  - `tests/unit/test_evidence_intake_plan.py`（**47 项**）：退出码映射与参数 / 时区错误；只读路径
    正常生成计划（状态 / 条目 / `plan_id` / 四个安全字段 / `data_qualification_passed_count` /
    零写入 / 原始 evidence 字节不变）；空批准 → `BLOCKED_*` 且绝不伪造成功；`plan_id` 与审计时点
    无关、ledger 决策集变化 → 新计划、旧清单 → `APPROVED_LIST_STALE`；stale 指纹 / 候选消失 /
    preflight 回退 / **review override** 逐项 fail-closed；**approved-list tamper**（reviewer /
    rows / package_dir / 额外字段 / decision_id / decision_counts / invalidated / 未来
    generated_at）与 **state 结构篡改**（kind / schema / contract / 四个安全字段 /
    approval_scope / 计数 / 缺字段 / 非法指纹 / `requires_explicit_intake=false` / 出现证据时间
    字段 / 非法类型 / naive generated_at）全部 fail-closed；合成候选即便"曾被批准"也必须
    `SYNTHETIC_EVIDENCE`；缺 / 损坏 / 被篡改的 ledger 与批准清单；只读运行零写入；输出写进 inbox
    被拒；原子写幂等（逐字节稳定、无 `.tmp`）；锁冲突零写入；多线程并发写不产生半写文档；
    handoff 必带显式开关且 `auto_executed=false`；批量 approve 不改变安全字段；**计划里不存在任何
    证据时间字段**（递归键断言 + 行内证据时间不被复制）；包名含凭据样式字符串被脱敏；Markdown
    摘要保留 blocker / human gate；以及**源码守卫**（无 `aiohttp` / `httpx` / `requests` /
    `socket` / `urllib` / `sqlalchemy` / `subprocess`，无 `unlink` / `rmtree` / `os.remove` /
    `os.rename` / `shutil.move`，无 `intake_evidence` / `evaluate_write_gate`，无 `while` 循环，
    `atomic_write_text` 是唯一写路径）；
  - `tests/integration/test_evidence_intake_plan_integration.py`（**10 项**）：真实 CLI —— 只读核验
    退出 `0` 且 stdout 纯 JSON、零写入；`--out` 原子落盘 + 幂等（逐字节稳定）；无批准退出 `5`；
    核验不通过退出 `4`（stdout 为空、stderr 带 `FINGERPRINT_MISSING`、零写入）；参数错误退出 `2`
    （含缺参 / naive `--as-of` / 试图传 `--no-dry-run`）；inbox 不可用或 `--out` 写进 inbox 退出
    `3`；损坏清单 / ledger 退出 `4` 且旧文件原样保留；锁冲突退出 `6` 且零写入；Markdown 摘要保留
    blocker；**真实子进程** `python -m scripts.evidence_intake_plan` 端到端冒烟（stdout 为纯 JSON）。
- **人工端到端冒烟**（真实 CLI 子进程，独立于 pytest）：inbox 预检（`0`）→
  `evidence_review --decision approve --approved-out`（`0`）→ intake plan 只读（`0`、
  `plan_id=72713073…`、四个安全字段恒定、`handoff` 命令为
  `python -m scripts.evidence_operator workflow --scope author --input <inbox>/pkg-author-01/author.jsonl --no-dry-run`、
  只读时不产生任何文件）→ `--out` 写入计划且重复运行 SHA-256 不变 → 内容变化后再计划
  （退出码 `4`、stdout **0 字节**、**零写入**、stderr 带 `FINGERPRINT_MISSING`）→ 计划 JSON 结构
  核对（`kind=evidence_intake_plan`、`plan_id`、`approved_for_explicit_intake_count=1`、
  `data_qualification_passed_count=0`、`auto_intake_allowed=false`、`writes_database=false`、
  `blocker_active=true` / `human_gate_required=true`）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2093 passed / 1 skipped in 233.49s**
    （唯一 skip 仍是 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；
    本轮新增 **57** 项：单元 47 + 集成 10。GOLD-012 记录的基线为 2034 passed / 1 skipped ——
    GOLD-012 自身也已记录过"既有测试收集口径差 2 项"，本轮观察到的差值与该口径一致，
    本轮**未修改任何既有测试文件**）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 164 source files`（GOLD-012 为 162，本轮 +2 个新模块文件）。


### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`、
  未安装 / 未修改任何 OS 计划任务；
- `src/alpha/**`、`src/monitoring/**`、`src/scheduler/**`、`src/collectors/**`、
  `src/processors/**` 未改动（只**复用**其契约 / 原因码 / 阈值）；GOLD-005 ~ GOLD-012 的既有模块
  （`inbox.py` / `review.py` / `readiness_runner.py` / `readiness_watch.py`）只被**复用**，未改动；
- **未解除** `PHASE3_3_DATA`：计划 / 摘要持续显式 `blocker_active=true` /
  `human_gate_required=true`、`data_qualification_passed=false`、`phase_transition_allowed=false`、
  `auto_intake_allowed=false`、`writes_database=false`；批准数量（含全部候选被 approve）**不会**
  自动改变任何安全字段；未进入 Phase 3.4，未训练 Alpha、未生成任何交易信号或订单；
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。GOLD-013 **只是**"最终写入前把批准
  清单与当前 inbox / ledger 重新绑定"的**只读门禁**：它把"清单是否还是当前事实、批准是不是最新
  revision、落库前还缺哪一步"变成可复算的 artifact，但**不是**资格判定器，也不具备解除 blocker
  的能力；
- 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据（放入 inbox 子目录 +
  `manifest.json`）→ `scripts.evidence_inbox --out` 预检 → `scripts.evidence_review --decision
  approve ... --approved-out` 人工复核 → `scripts.evidence_intake_plan --out` 生成计划与显式
  `handoff` 命令 → 人工**显式**执行 `scripts.evidence_operator workflow --no-dry-run` 落库 →
  `handoff` / `recheck` 复核；Phase 切换仍需 **L3 人工确认**；
- 计划**不缓存批准**：内容变化 → 新指纹、review revision 变化 → 旧批准失效，必须重新生成计划
  （本工具不会把旧计划"升级"成新计划）；`reviewer` / `note` / 文件名 / mtime / `reviewed_at` /
  `generated_at` / "曾被人批准" 都**不是**证据时间，产出里根本没有这些证据时间键。


---

## 第七十八轮（2026-09-23）：GOLD-014 —— Evidence 显式 Intake Receipt 与资格复核审计闭环

### 1. 交付内容

在 `PHASE3_3_DATA` 仍 **BLOCKED** 的前提下，为 GOLD-013 生成的 approved Evidence 显式人工
intake 建立**纯本地、可复核、fail-closed** 的 **post-intake receipt / verification 层**：

- **核心 `src/evidence/intake_receipt.py`**（零网络 / 零数据库 / 零新增依赖）：
  - `IntakeReceiptStatus`（`VERIFIED_EXECUTION_RECORDED` / `BLOCKED_NO_APPROVED_EVIDENCE`）与
    `ReceiptVerificationCode`（17 个**稳定**原因码）+ `ReceiptViolation`；
  - `IntakePlanDocument` / `PlanEntryDocument` / `load_intake_plan_document(...)`：GOLD-013 plan 的
    严格**只读视图**（kind / schema / 契约版本、四个安全字段与 `auto_intake_allowed` /
    `writes_database` / `requires_explicit_operator_action` 不可被 state 削弱、条目**白名单键**、
    `approved_for_explicit_intake_count` 与列表长度自洽、`data_qualification_passed_count` 必须为 0、
    `plan_id` 必须 64 位小写十六进制、**任何证据时间键一律拒绝加载**）→ `IntakeReceiptStateError`；
  - `OperatorResult` / `load_operator_result(...)`：人工**显式** Evidence Operator 执行结果
    （`EvidenceIntakeReport.to_dict()` manifest）的严格只读视图；记录**内容寻址**的
    `artifact_sha256`、`input.sha256`、`scope`、`dry_run`、`generated_at` 与全部 `counts`；
  - `QualificationRecheck` / `load_qualification_recheck(...)`：`phase33_qualification_recheck`
    产物的严格只读视图（`as_of` / `blocker_code` / `blocker_active` / `human_gate_required` /
    `ready` / gate 计数）；
  - `verify_intake_receipt(...)` / `build_intake_receipt(...)`：**复用** GOLD-013 的
    `build_intake_plan` 做重新绑定核验（**不复制、不降低**任何资格规则）——
    ①plan 必须**当前仍然成立**（`plan_id` 与条目摘要（指纹 / `decision_id` / `review revision` /
    `scope` / 文件清单）与用**当前** inbox + ledger + 批准清单重新算出的完全一致：
    `PLAN_STALE` / `PLAN_ENTRY_MISMATCH` / `PLAN_TAMPERED`）；
    ②人工显式执行结果必须**按内容 SHA-256** 覆盖**每一个**被批准的证据文件且 scope 一致、
    `dry_run=false`、`persisted>=1`、`counts` 自洽（`rows` 条数一致 /
    `accepted+quarantined+duplicate<=rows` / `persisted<=accepted`）：
    `OPERATOR_RESULT_MISSING` / `OPERATOR_NOT_EXECUTED` / `OPERATOR_FAILED` /
    `OPERATOR_TAMPERED` / `OPERATOR_INPUT_MISMATCH` / `OPERATOR_SCOPE_MISMATCH` /
    `OPERATOR_COVERAGE_INCOMPLETE`；无批准却给出执行结果 → `OPERATOR_UNBOUND`；
    ③qualification recheck 必须存在且**不早于**最后一次显式执行、`blocker_code` 仍为
    `PHASE3_3_DATA` 且 `blocker_active` / `human_gate_required` 仍为 true：
    `RECHECK_MISSING` / `RECHECK_BEFORE_INTAKE` / `RECHECK_TAMPERED` / `RECHECK_UNBOUND`；
    ④任何操作时间都不得晚于收据审计时点（`FUTURE_TIMESTAMP`）或早于 plan（`INTAKE_BEFORE_PLAN`）；
    任一不一致 → `IntakeReceiptVerificationError`（exit 4、**零写入**）；
  - **四个布尔互不蕴含**：`intake_executed`（人工显式落库确实发生）与 `receipt_verified`
    （绑定核验通过）独立；`data_qualification_passed` / `data_qualification_passed_count` 恒为
    false / 0、`phase_transition_allowed` 恒为 false、`blocker_active` / `human_gate_required` 恒为
    true（**硬编码**）；即使 recheck 自报 `ready=true` 也**绝不**升级为资格 / Phase 结论
    （`is_qualification_decision=false`）；
  - 内容级 `compute_receipt_id(...)`（只由策略块 + `plan_id` + 批准条目 + 执行摘要 + 复核摘要 +
    两个布尔派生，**不含** `receipt_at`）；`run_intake_receipt(...)` 默认只读，只有显式 `out_path`
    才（先取单实例锁）**原子**写收据本身；`render_intake_receipt_summary(...)` / `exit_code_for(...)`；
  - 收据结构里**没有**任何证据时间键（`evidence_time_semantics.contains_evidence_times=false`，
    并显式声明只含审计操作时间）。
- **CLI `scripts/evidence_intake_receipt.py`**：`--inbox-dir` / `--ledger` / `--approved-list` /
  `--plan` 必填，`--operator-result` 可重复，`--recheck` 可选，`--out` 唯一写开关，
  `--lock` / `--as-of` / `--json`；**没有任何 intake / `--no-dry-run` 参数**（传入 → 退出码 2）；
  退出码 0/2/3/4/5/6；失败路径 stdout 为空、stderr 已脱敏。
- **导出**：`src/evidence/__init__.py` 增加 GOLD-014 的模块说明与 30+ 个新 API 导出。
- **文档**：`README.md`（§1 交付表新增 GOLD-014 行、§7 新增「Evidence 显式 Intake Receipt」章节含
  用法 / fail-closed 口径 / 退出码 / **inbox → review → approved list → intake plan → 人工显式
  intake → receipt verify → qualification recheck → L3 human gate** 最小路径、§10 明确仍未解除）、
  `TECH_DEBT.md`（新增 **TD-56** 登记行 + 明细 + 变更日志行）、`PROGRESS_LOG.md`（本轮）。

### 2. 测试与门禁（本轮实测，项目 `.venv`）

- 新增 **102 项**测试（临时目录 / Mock 与**真实**审计产物 / **无数据库（单元）** / 临时 SQLite
  （集成） / **零网络**，未新增依赖）：
  - `tests/unit/test_evidence_intake_receipt.py`（**91 项**）：退出码映射与参数 / 时区 / inbox 错误；
    真实 GOLD-013 plan 绑定成功路径（状态 / 条目内容摘要 / 四个布尔 / 零写入 / 原始 evidence 字节不变）；
    无批准 → `BLOCKED_NO_APPROVED_EVIDENCE` 且两个布尔为 false；无批准却给出执行结果 →
    `OPERATOR_UNBOUND` / `RECHECK_UNBOUND`；**plan 结构篡改 24 种口径**（kind / schema / 契约 /
    四个安全字段 / `auto_intake_allowed` / `writes_database` / `requires_explicit_operator_action` /
    `plan_id` / 计数 / naive 时间 / 证据时间键 / 条目缺键 / 多余键 / 未批准 / 资格声称 / 无显式动作 /
    scope / 指纹 / 空文件清单 / 负数行数）全部 fail-closed；plan 被改写 `plan_id` → `PLAN_STALE`；
    内容变化 → `PLAN_STALE` + `PLAN_ENTRY_MISMATCH`；**review override** → 旧收据失效，且用**当前**
    事实重新生成 plan 后得到 BLOCKED 收据（旧批准不继承）；执行结果缺失 / dry-run / 零落库 /
    输入摘要不符 / scope 不符 / 用了**未批准**输入 / 多批准覆盖不全 / 计数矛盾（3 种）/ 早于 plan /
    未来时间 逐项 fail-closed；执行结果 12 种与 recheck 8 种损坏口径 → `IntakeReceiptStateError`；
    recheck 声称 blocker 解除 / 其他 `blocker_code` / 早于显式执行 → fail-closed；`receipt_id` 与
    审计时点无关、执行结果或复核结果变化 → 新 `receipt_id`；`--out` 幂等（逐字节稳定、无 `.tmp`）；
    只读零写入；输出写进 inbox 被拒；写失败 fail-closed；锁冲突零写入；多线程并发不产生半写文档；
    全部候选被 approve 也不改变安全字段；`intake_executed` / `receipt_verified` 绝不蕴含资格
    （即使 recheck 自报 `ready=true`）；公开 `verify_intake_receipt` 返回稳定原因码而不抛错；
    递归键断言"没有证据时间字段"；包名含凭据样式字符串被脱敏；Markdown 摘要保留 blocker / human
    gate；以及**源码守卫**（无 `aiohttp` / `httpx` / `requests` / `socket` / `urllib` /
    `sqlalchemy` / `subprocess`，无 `unlink` / `rmtree` / `os.remove` / `os.rename` /
    `shutil.move`，无 `intake_evidence` / `evaluate_write_gate`，无 `while` 循环，
    `atomic_write_text` 是唯一写路径）与 CLI 参数守卫（缺必填 → 2；`--no-dry-run` → 2；
    `--operator-result` 可重复给出）；
  - `tests/integration/test_evidence_intake_receipt_integration.py`（**11 项**）：**真实链路** ——
    inbox 预检 → `evidence_review --approved-out` → `evidence_intake_plan --out` →
    **真实** `evidence_operator intake --no-dry-run --manifest`（临时 SQLite 真落库）→
    **真实** `evidence_readiness recheck --no-dry-run --report` → GOLD-014 收据核验；只读退出 `0`
    且 stdout 纯 JSON、收据字段与安全字段全部核对、真实 manifest 的 `input.sha256` 与产物摘要被绑定；
    `--out` 原子落盘 + 幂等；无批准退出 `5`；执行结果被改成 dry-run / 内容变化导致 plan 过期 →
    退出 `4`（stdout 为空、stderr 带原因码、零写入）；损坏 recheck → 退出 `4` 且旧文件保留；
    参数错误退出 `2`（缺 `--plan` / naive `--as-of`）；`--out` 写进 inbox / inbox 不存在退出 `3`；
    锁冲突退出 `6` 且零写入；Markdown 摘要保留 blocker；**真实子进程**
    `python -m scripts.evidence_intake_receipt` 端到端冒烟（stdout 为纯 JSON）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2197 passed / 1 skipped in 240.79s**
    （唯一 skip 仍是 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；
    本轮新增 **102** 项：单元 91 + 集成 11；GOLD-013 记录的基线为 2034 → 2093 passed / 1 skipped，
    本轮观察到的收集口径差与该系列记录中的"既有测试收集口径差 2 项"一致，
    本轮**未修改任何既有测试文件**）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 166 source files`（GOLD-013 为 164，本轮 +2 个新模块文件）。


### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`、
  未安装 / 未修改任何 OS 计划任务；
- `src/alpha/**`、`src/monitoring/**`、`src/scheduler/**`、`src/collectors/**`、
  `src/processors/**` 未改动（只**复用**其契约 / 原因码 / 阈值）；GOLD-005 ~ GOLD-013 的既有模块
  （`inbox.py` / `review.py` / `intake_plan.py` / `readiness_runner.py` / `readiness_watch.py`）
  只被**复用**，未改动；
- **未解除** `PHASE3_3_DATA`：收据持续显式 `blocker_active=true` / `human_gate_required=true` /
  `data_qualification_passed=false` / `phase_transition_allowed=false`；
  `intake_executed` / `receipt_verified` **不会**自动改变任何安全字段；未进入 Phase 3.4，
  未训练 Alpha、未生成任何交易信号或订单；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。GOLD-014 **只是**"把已发生的显式落库
  与随后的复核绑定成可复核审计记录"的**只读**收据层：它把"到底有没有人显式执行过、执行的是不是
  被批准的那一版内容、执行之后有没有复核"变成可复算的 artifact，但**不是**资格判定器，也不具备
  解除 blocker 的能力；
- 业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据（放入 inbox 子目录 +
  `manifest.json`）→ `evidence_inbox --out` 预检 → `evidence_review --decision approve
  --approved-out` 人工复核 → `evidence_intake_plan --out` 生成计划与显式 `handoff` 命令 →
  人工**显式**执行 `evidence_operator workflow --no-dry-run --manifest` 落库 →
  `evidence_operator recheck --no-dry-run --report` 复核 → `evidence_intake_receipt --out` 生成收据
  → **L3 人工确认**；
- 收据**不缓存**任何输入：plan / review revision / 指纹 / 执行结果 / 复核结果任一变化 → 新
  `receipt_id` 或直接 fail-closed（旧收据不会"升级"成新收据）；`receipt_at` / `plan.generated_at` /
  `operator_results[].generated_at` / `qualification_recheck.as_of` 都**只是审计操作时间**，
  产出里根本没有证据时间键。



---

## 第七十九轮（2026-09-23）：GOLD-015 —— Evidence Qualification L3 人工决策包与 Gate 审计入口

### 1. 交付内容

在 `PHASE3_3_DATA` 仍 **BLOCKED** 的前提下，为"能不能提交 **L3 人工 Gate**"这件事补上**最后一张
只读聚合视图**：把最新 readiness / handoff（GOLD-008/010）、批准清单与 GOLD-013 plan、
GOLD-014 verified receipt 与 qualification recheck 聚合成**确定性、脱敏、内容寻址**的决策包：

- **核心 `src/evidence/decision_packet.py`**（零网络 / 零数据库 / 零新增依赖）：
  - `DecisionPacketStatus`（`READY_FOR_L3_HUMAN_GATE` / `BLOCKED_PENDING_EVIDENCE`）与
    `PacketVerificationCode`（13 个**稳定**原因码；与 GOLD-014 同义的核验失败**沿用**其稳定
    原因码字符串，**不另造词**）+ `PacketViolation`；
  - `HandoffDocument` / `load_handoff_document(...)`：GOLD-008 handoff 的严格**只读视图** ——
    文档标识 / schema / 契约版本 / 四个安全字段不可被削弱 / `thresholds` 必须等于**当前**代码里
    的唯一阈值来源 / 缺口与检查的**算术自洽**（`status` / `remaining` / `remaining_checks` 必须与
    `current` / `required` / `comparator` / `evaluable` 一致）/ **任何证据时间键一律拒绝加载**；
    并用 `readiness_watch.build_snapshot` **重新推导**同源快照（指纹 / 原因码 / 未 PASS 检查键）；
  - `ReadinessStateDocument` / `load_readiness_state_document(...)`：GOLD-010
    `readiness_state.json` 的严格只读视图（**复用**其指纹校验与安全字段守卫）；
  - `ReceiptDocument` / `load_intake_receipt_document(...)`：GOLD-014 收据的严格只读视图；
    重建 `IntakeReceipt` 以便用 **GOLD-014 的 `compute_receipt_id`** 做内容寻址核对；
  - `verify_decision_packet(...)` / `build_decision_packet(...)`：**复用** GOLD-013 的
    `build_intake_plan` 与 GOLD-014 的 `build_intake_receipt` 做重新绑定（**不复制、不降低**任何
    资格规则）—— ①handoff 时点 / 未来时间；②给出 `--readiness` 时快照必须与 handoff **逐字段 +
    指纹**同源（`READINESS_MISMATCH`）；③plan stale / fingerprint drift / review override /
    执行结果缺失或 dry-run 或零落库 / recheck 缺失或早于执行或结论不一致（沿用 GOLD-014 原因码）；
    ④handoff **不得早于**最近一次人工显式落库（`HANDOFF_STALE`）；⑤可选 GOLD-014 收据文件再做一次
    交叉核对（`RECEIPT_ID_MISMATCH` / `PLAN_ID_MISMATCH` / `FINGERPRINT_MISMATCH` /
    `REVIEW_REVISION_MISMATCH` / `RECHECK_MISMATCH`）→ 任一不一致 →
    `DecisionPacketVerificationError`（退出 `4`、**零写入**）；
  - **五个布尔互不蕴含**：`evidence_ready_for_human_review`（只由 readiness / handoff 推导）/
    `receipt_verified`（GOLD-014 重新绑定核验）/ `qualification_recheck_ready`（recheck 存在 +
    绑定成功 + 自报 `ready`）三个独立事实；只有**同时**成立才是 `submit_to_l3_human_gate=true`；
    `data_qualification_passed` / `phase_transition_allowed` **恒为** false、
    `blocker_active` / `human_gate_required` 恒为 true、`human_gate_level` 恒为 `L3`（**硬编码**）；
  - 内容级 `compute_packet_id(...)`（只由策略块 + handoff state / 产物摘要 + readiness 摘要 +
    `plan_id` + 收据摘要 + recheck 摘要 + 五个布尔 + 状态派生，**不含** `generated_at`）；
    `run_decision_packet(...)` 默认只读，只有显式 `out_path` 才（先取单实例锁）**原子**写 packet；
    `render_decision_packet_summary(...)` / `exit_code_for(...)`；
  - 产出里**没有**任何证据时间键（`evidence_time_semantics.contains_evidence_times=false`，并显式
    声明只含审计操作时间）。
- **CLI `scripts/evidence_decision_packet.py`**：`--handoff` / `--inbox-dir` / `--ledger` /
  `--approved-list` / `--plan` 必填，`--readiness` / `--receipt` / `--operator-result`（可重复）/
  `--recheck` 可选，`--out` 唯一写开关，`--lock` / `--as-of` / `--json`；**没有任何 intake /
  `--no-dry-run` 参数**；退出码 0/2/3/4/5/6；失败路径 stdout 为空、stderr 已脱敏。
- **导出**：`src/evidence/__init__.py` 增加 GOLD-015 的模块说明与 30+ 个新 API 导出。
- **文档**：`README.md`（§1 交付表新增 GOLD-015 行、§7 新增「Evidence Qualification 人工决策包」
  章节含用法 / 边界 / 七步人工路径、§10 更新下一步与 TD-57）；`TECH_DEBT.md` 登记 **TD-57** 与
  变更日志；`PROGRESS_LOG.md` 本轮记录。三处均明确 **`PHASE3_3_DATA` 仍 BLOCKED**、
  Phase 切换仍是 **L3 人工 Gate**。

### 2. 测试与验收

- **`tests/unit/test_evidence_decision_packet.py`（**71 项**）**：退出码映射；缺参数 / naive 时点；
  inbox 缺失 / 输出写进 inbox；handoff 的 **22 种**结构 / 安全字段 / 算术 / 阈值篡改 + 证据时间键
  一律 fail-closed；readiness state 缺失 / 损坏 / **不同源**；完整链路绑定成功（内容级 `packet_id`、
  五个布尔、安全字段、`approved_entry_count` / `landed_rows`、`STATE_FILE_MATCHED`、零写入）；
  无批准 → 预期 BLOCKED；执行结果缺失 / dry-run / 零落库 / plan stale / recheck 早于执行或结论
  不一致 / handoff stale / 未来时间；可选 GOLD-014 收据文件的 5 种"改写并重签"仍 fail-closed +
  `receipt_id` 篡改 + 收据缺字段 / 安全字段被削弱；`verify_decision_packet` 公开 API 返回稳定原因码
  而不抛错；幂等 / byte-stable / 关键输入变化出新 `packet_id`；只读（含原始 evidence 字节不变）/
  原子写 / 锁冲突 / 多线程并发 / 写失败；递归键断言"没有证据时间字段"、凭据样式字符串被脱敏、
  Markdown 摘要保留 blocker / human Gate；**源码守卫**（无 `aiohttp` / `httpx` / `requests` /
  `socket` / `urllib` / `sqlalchemy` / `subprocess`，无 `unlink` / `rmtree` / `os.remove` /
  `os.rename` / `shutil.move`，无 `intake_evidence` / `evaluate_write_gate`，无 `while` 循环，
  `atomic_write_text` 是唯一写路径，安全字段与执行模式硬编码）与 CLI 参数守卫（缺必填 → 2；
  `--no-dry-run` → 2；`--operator-result` 可重复、`--readiness` / `--receipt` 可解析）。
- **`tests/integration/test_evidence_decision_packet_integration.py`（**12 项**）**：**真实链路**
  —— inbox 预检 → `evidence_review --approved-out` → `evidence_intake_plan --out` → **真实**
  `evidence_operator intake --no-dry-run --manifest`（临时 SQLite 真落库）→ **真实**
  `evidence_readiness recheck --report` → **真实** `scripts.evidence_handoff --out` →
  GOLD-015 决策包：①证据未达标时**诚实 BLOCKED**（退出 `5`，`receipt_verified=true` /
  `evidence_ready=false`，文件与数据库零变化）；②库内达标（Mock 证据块 + 合法格式 recheck 控制流）
  → `submit_to_l3_human_gate=true`（退出 `0`，但 `data_qualification_passed` **恒 false**）；
  `--out` 原子写 + 幂等；handoff / readiness state 被改写 → 退出 `4`（stdout 为空、stderr 带稳定
  原因码、零写入）；内容变化 → plan stale；handoff 早于显式落库 → `HANDOFF_STALE`；参数错误退出
  `2`；`--out` 写进 inbox / inbox 不存在退出 `3`；锁冲突退出 `6` 且零写入；Markdown 摘要保留
  blocker；**真实子进程** `python -m scripts.evidence_decision_packet` 端到端冒烟（stdout 为纯 JSON）。
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2282 passed / 1 skipped in 251.92s**（唯一 skip
    仍是 `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba」分支；本轮新增 **83** 项
    （`--collect-only` 实测 = 71 单元 + 12 集成），GOLD-014 记录的基线为 2197 passed / 1 skipped，
    本轮**未修改任何既有测试文件**；与该系列记录中的"既有测试收集口径差 2 项"一致，
    本轮同样观察到同一现象）；
  - `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` →
    `Success: no issues found in 168 source files`（GOLD-014 为 166，本轮 +2 个新模块文件）。

### 3. 范围守规

- 未新增依赖、未新增 / 修改 migration 与 schema、未联网、未抓取任何站点、未触碰 `.env`、
  未安装 / 未修改任何 OS 计划任务；
- `src/alpha/**`、`src/monitoring/**`、`src/scheduler/**`、`src/collectors/**`、
  `src/processors/**` 未改动（只**复用**其契约 / 原因码 / 阈值）；GOLD-005 ~ GOLD-014 的既有模块
  （`inbox.py` / `review.py` / `intake_plan.py` / `intake_receipt.py` / `readiness_watch.py` /
  `readiness_runner.py`）只被**复用**，未改动；
- **未解除** `PHASE3_3_DATA`：决策包持续显式 `blocker_active=true` / `human_gate_required=true` /
  `data_qualification_passed=false` / `phase_transition_allowed=false`；三个布尔为 true 也**不会**
  自动改变任何安全字段；未进入 Phase 3.4，未训练 Alpha、未生成任何交易信号或订单；
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。GOLD-015 **只是**"把散落在多个 JSON 里的
  事实绑成一张可复核的人工 Gate 输入"的**只读**聚合层：它**不是**资格判定器，也**不具备**解除
  blocker 的能力；
- 完整人工路径：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据（放入 inbox 子目录 +
  `manifest.json`）→ `evidence_inbox --out` 预检 → `evidence_review --decision approve
  --approved-out` 人工复核 → `evidence_intake_plan --out` 生成计划 → 人工**显式**执行
  `evidence_operator workflow --no-dry-run --manifest` 落库 → `evidence_operator recheck
  --no-dry-run --report` 复核 → `evidence_intake_receipt --out` 生成收据 →
  `evidence_decision_packet --out` 生成决策包（建议同时给 `--readiness` 把 handoff 锚定到真实 tick
  产物）→ **L3 人工确认**；
- 决策包**不缓存**任何输入：handoff / readiness 指纹 / plan / review revision / 指纹 / 执行结果 /
  复核结果 / 收据任一变化 → 新 `packet_id` 或直接 fail-closed（旧决策包不会"升级"成新决策包）；
  `generated_at` / `handoff.as_of` / `readiness_state.generated_at` /
  `operator_results[].generated_at` / `qualification_recheck.as_of` 都**只是审计操作时间**，
  产出里根本没有证据时间键。

---

## 第八十轮（2026-09-23）：GOLD-016 —— L3 Human Gate 决策记录与防伪审计闭环

### 1. 交付内容

在 `PHASE3_3_DATA` 仍 **BLOCKED** 的前提下，为"**人到底作出过什么决策**"补上**可核验的审计记录**：
把**人工显式**给出的 L3 Gate 决策绑定到**具体**的 GOLD-015 packet，并支持事后**防伪核验**：

- **核心 `src/evidence/decision_record.py`**（零网络 / 零数据库 / 零新增依赖）：
  - `HumanDecision`（`approve` / `reject` / `needs_changes`；**必须**人工显式给出，工具**绝不**
    自行生成批准、大小写敏感、不做静默归一）与 `DecisionRecordCode`（21 个**稳定**原因码；
    与 GOLD-015 同义者**沿用**其字符串：`READINESS_MISMATCH` / `EVIDENCE_TIME_SUBSTITUTION` /
    `FUTURE_TIMESTAMP`）+ `DecisionRecordArgumentError` / `...StateError` / `...PathError` /
    `...VerificationError` / `...NotSubmittableError` / `...WriteError`；
  - `PacketBinding` / `load_decision_packet(...)`：GOLD-015 packet 的**严格只读绑定** + **逐项**
    完整性核验 —— 文档身份（`kind` / 报告名 / schema / 契约版本 / 执行模式）、11 个安全字段
    不可被削弱（含 `data_qualification_passed_count` / `auto_intake_allowed` / `writes_database` /
    `operator_explicit_flag`）、`packet_id` 必须是内容寻址摘要、handoff 标识 / schema / 安全字段 /
    `thresholds` 必须等于**当前**唯一阈值来源 / 状态与 `ready_for_human_review` 一致、
    **缺口与检查的算术可重算**（`status` / `remaining` / `remaining_checks` / `coverage_applicable`
    与 `current` / `required` / `comparator` / `evaluable` 逐条自洽）、`unmet_check_keys` 必须可由
    各 scope 检查状态**重新推导**、`submit_to_l3_human_gate` 必须等于三个独立事实的**合取**、
    readiness 快照必须与 handoff **指纹同源**、收据状态与 `receipt_verified` 往返一致、recheck 与
    `qualification_recheck_ready` 合取一致、批准 / ledger / 执行结果计数自洽、
    `verification.violations` 必须为空且 `revalidated_at` 与 `generated_at` 一致、
    **递归禁止任何证据时间键**、**禁止未来时间**（不得对尚未产生的 packet 作决策）；
    另绑定 `content_sha256`（规范化 JSON 内容摘要，格式无关）与 `artifact_sha256`（原始字节摘要）；
  - `HumanDecisionRecord` / `build_decision_record(...)` / `compute_record_id(...)`：内容寻址
    `record_id`（只由策略块 + `decision` + `reviewer` + 受约束的 `note` / `reason_code` +
    `revision` / `supersedes` + packet 绑定派生，**不含**任何审计时间）；`human_decision_recorded` /
    `human_decision` / `packet_verified` 三个事实独立，`data_qualification_passed` /
    `phase_transition_allowed` / `phase_transition_executed` **恒为** false、
    `blocker_active` / `human_gate_required` 恒为 true、`human_gate_level` 恒 `L3`（**硬编码**）；
    `approve` **只**允许落在 packet 本身 `submit_to_l3_human_gate=true` 时（否则
    `PACKET_NOT_SUBMITTABLE`，**预期 BLOCKED**、零写入）；
  - `verify_decision_record(...)` + `DecisionRecordVerification`：**防伪核验** —— 由记录文档
    **重新推导** `record_id`（`RECORD_ID_MISMATCH`）、与**当前** packet 比对 `packet_id` /
    内容摘要（`PACKET_ID_MISMATCH` / `PACKET_CONTENT_MISMATCH`）、判定当前 packet 是否更新
    （`PACKET_STALE`）、并判定当初的 `approve` **是否仍然成立**（`PACKET_NOT_SUBMITTABLE`）；
    记录自身被改写 / 安全字段被削弱 / 出现证据时间键 → `RECORD_TAMPERED`（fail-closed）；
  - `run_decision_record(...)`：默认**只读预检**；只有显式 `out_path` 才先取 GOLD-010
    **单实例锁**，锁内**重新**核验 packet（`packet_id` / 内容摘要 / 产物摘要任一变化 →
    `PACKET_STALE`，防 TOCTOU），再做既有记录冲突检查，最后**原子**落盘；**没有任何** intake /
    commit / 数据库调用；`render_decision_record_summary(...)` / `decision_record_exit_code_for(...)`；
  - **幂等 / 不静默改写历史**：同一 packet + 同一人工决策 + 同一 `revision` / `supersedes` →
    同一 `record_id`，同一审计时点**逐字节稳定**；`packet_id` / `decision` / `reviewer` / `note` /
    `reason_code` / `revision` / `supersedes` 或 packet 内容任一变化 → **新** `record_id`；
    覆盖既有记录必须显式 `--revision`（严格大于既有）**且** `--supersedes` 必须等于**当前**
    `record_id`，否则 `RECORD_CONFLICT` / `SUPERSEDES_MISMATCH` / `REVISION_INVALID`（零写入）；
  - **人工身份与脱敏**：reviewer 只接受**非敏感 label**（拒绝空值 / 超长 / 非法字符 / 疑似凭据 /
    长 blob，**绝不**采集口令 / token / 密钥）；`note` / `reason_code` 走
    `src.common.redaction` 脱敏 + 限长（超长输入 fail-closed，不静默截断成假事实）；
    `decision_at` / `generated_at` / 文件 mtime **都只是审计操作时间**（`contains_evidence_times`
    恒 false）；
- **CLI `scripts/evidence_decision_record.py`**：`--packet` / `--decision` / `--reviewer` 必填，
  `--note` / `--reason-code` / `--revision` / `--supersedes` / `--out`（**唯一**写开关）/
  `--lock` / `--as-of` / `--json`，以及纯只读的 `--verify-record`（与写参数互斥，退出码 `2`）；
  **没有**任何 intake / 写库 / 改 `PROJECT_STATE` 的参数；退出码 `0` 已记录 / `2` 参数 /
  `3` 路径或输出不可用（含把记录写到 packet 文件上的拒绝 `PACKET_OVERWRITE_REFUSED`）/
  `4` 缺失 / 损坏 / 篡改 / stale / 冲突 / `record_id` 不一致或防伪核验不通过（fail-closed，
  零写入）/ `5` `approve` 被拒（packet 不可提交，**预期 BLOCKED**）/ `6` 锁冲突；
- **导出**：`src/evidence/__init__.py` 增加 GOLD-016 的模块说明与 40+ 个新 API 导出。
- **文档**：`README.md`（§1 交付表新增 GOLD-016 行、§7 新增「Evidence Qualification L3 人工决策记录」
  与**八步**人工路径、§10 下一步与摩擦清单）、`TECH_DEBT.md`（TD-58 + 变更日志）、
  `PROGRESS_LOG.md`（本轮）三处均明确 `PHASE3_3_DATA` **保持 BLOCKED**、Phase 切换仍是 L3 人工 Gate。

### 2. 测试与验收

- **`tests/unit/test_evidence_decision_record.py`（**202 项**）**：退出码映射；人工输入逐项校验
  （决策必须显式且不做静默归一、reviewer 凭据 / 非法字符 / 超长拒绝、note 脱敏 + 限长、
  reason-code 形态、`revision` / `supersedes` 规则、naive 时点）；packet **逐项**完整性核验
  （文档身份 5 项、安全字段 11 项、`packet_id` 形态 4 项、handoff 块 13 项、缺口算术与检查重算
  18 项、readiness 同源 8 项、布尔合取 6 项、plan / 收据 10 项、recheck 12 项、批准与执行计数 15 项、
  verification 块 8 项、证据时间键递归拒绝、未来时间、`written_path` 绑定、内容摘要与格式无关）；
  `approve` 门禁（BLOCKED → 拒绝）与 `reject` / `needs_changes` 可记录且**不改变**任何资格状态；
  记录文档的独立性布尔 / 审计时间清单 / `record_id` 幂等与内容寻址 / byte-stable；运行入口默认
  只读、原子写、幂等复写、锁冲突、写失败包装、目录输出拒绝、不得覆盖 packet 自身；冲突 / revision /
  supersedes / 既有记录被改写 / 未知既有文档 fail-closed；防伪核验 6 项（通过、packet 被更新 →
  `PACKET_CONTENT_MISMATCH` + `PACKET_STALE`、记录被改写 → `RECORD_ID_MISMATCH`、记录被削弱 →
  `RECORD_TAMPERED`、`approve` 失效 → `PACKET_NOT_SUBMITTABLE`、缺文件）；CLI 编排（缺参数 /
  naive 时点 / 互斥参数 / 退出码 / stdout 纯净 / Markdown 保留 blocker / 防伪核验模式）；
  源码守卫（**无** sqlalchemy / requests / aiohttp / httpx / subprocess / socket / urlopen /
  `intake_evidence` / `os.stat` / `getmtime` / `PROJECT_STATE.json`，且不直接 `open`）；
- **`tests/integration/test_evidence_decision_record_integration.py`（**4 项**）**：**真实链路**
  （真实 inbox 预检 → 真实人工复核 / 批准清单 → 真实 GOLD-013 plan → **人工显式** operator
  真落库 → 真实 recheck / **真实** handoff → **真实** GOLD-015 决策包）→ GOLD-016：①证据未达标时
  决策包诚实 BLOCKED（退出 `5`）→ `approve` 被拒（退出 `5`、**零写入**）而 `needs_changes`
  可记录（退出 `0`，`data_qualification_passed` 恒 false）→ 防伪核验通过；②库内达标（Mock 证据块，
  **仅控制流**）→ `approve` 可记录（退出 `0`）→ 防伪核验通过 → 覆盖既有记录必须先显式
  `revision` + `supersedes`（未声明 → 退出 `4` 且记录不变；声明正确 → revision 2 落盘并可复核）
  → packet 被改写 → `approve` 与防伪核验**双双** fail-closed（退出 `4`、零写入）；③并发锁冲突
  （退出 `6`、零写入）；④**真实子进程** `python -m scripts.evidence_decision_record` 端到端
  冒烟（stdout 为纯 JSON）。全链路临时目录 / 临时 SQLite / 零网络，且断言**数据库零变化**、
  packet 与原始 evidence 零改写；
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2490 passed / 1 skipped**（新增 206 项）；
  - `.venv\Scripts\python.exe -m ruff check .` → **All checks passed!**；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` → **Success: no issues found
    in 170 source files**（GOLD-015 为 168）。

### 3. 范围守规

- 只读输入、显式人工输入、默认零写入；`--out` 是唯一写开关（先取单实例锁再原子落盘）；
- 未新增 / 未升级任何第三方依赖；未新增 migration / schema；未联网、未接第三方推送、
  未安装 / 未修改任何 OS 计划任务；
- *复用而不复制*：`packet_id` / 原因码 / 脱敏 / 原子写 / 单实例锁全部复用 GOLD-010 / 015 的既有
  契约；`src/alpha/**`、`src/monitoring/**`、`src/scheduler/**`、`src/collectors/**`、
  `src/processors/**` 与 GOLD-005 ~ GOLD-015 的既有模块**均未改动**（只**复用**其契约 / 原因码 /
  阈值；`src/evidence/__init__.py` 仅新增导出与说明）；
- **未解除** `PHASE3_3_DATA`：记录持续显式 `blocker_active=true` / `human_gate_required=true` /
  `data_qualification_passed=false` / `phase_transition_allowed=false` /
  `phase_transition_executed=false`；即使记录了 `approve` 也**不会**自动改变任何安全字段；
  未进入 Phase 3.4，未训练 Alpha、未生成任何交易信号或订单；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。GOLD-016 **只是**"把一次人工决策
  绑定到具体决策包并可事后防伪核验"的**审计层**：它**不是**资格判定器、**不是** Phase transition
  executor，也**不具备**解除 blocker 的能力；
- 完整人工路径（**八步**）：业务方按 `evidence-intake-v1` 提供真实授权 Author / News 数据 →
  `evidence_inbox --out` 预检 → `evidence_review --decision approve --approved-out` 人工复核 →
  `evidence_intake_plan --out` 生成计划 → 人工**显式**执行 `evidence_operator workflow
  --no-dry-run --manifest` 落库 → `evidence_operator recheck --no-dry-run --report` 复核 →
  `evidence_intake_receipt --out` 生成收据 → `evidence_decision_packet --out` 生成决策包 →
  `evidence_decision_record --decision approve --reviewer <label> --out` 记录 L3 人工决策 →
  `--verify-record` 事后防伪复核 → **L3 人工确认**（由人工 / Orchestrator 显式更新
  `PROJECT_STATE`）；
- 记录**不缓存**任何输入：packet 内容 / `packet_id` / 人工决策 / reviewer / note / revision 任一
  变化 → 新 `record_id` 或直接 fail-closed（旧记录不会"升级"）；`decision_at` / `generated_at` /
  `packet.generated_at` 都**只是审计操作时间**，产出里根本没有证据时间键。
## 第八十一轮（2026-09-23）：GOLD-017 —— 真实 Evidence Package 本地 Manifest Builder 与安全交付入口

### 1. 交付内容

在 `PHASE3_3_DATA` 仍 **BLOCKED** 的前提下，补齐"业务方如何把真实授权 Author / News 文件制作成
GOLD-011 可直接扫描的 evidence package"这一**实际交付入口**的最后摩擦点（此前业务方仍要**手工**写
manifest、**手工**算 SHA-256，容易造成格式 / 摘要 / 路径错误）：

- **核心 `src/evidence/package_builder.py`**（零网络 / 零数据库 / 零新增依赖）：
  - **人工显式输入，绝不推断**：`evidence_type`（`author` / `news`，大小写敏感不做静默归一）/
    `source` / `authorization_reference` / `time_semantics` / `availability_semantics` /
    `historical_oos_applicable`（严格布尔）与要纳入 package 的文件清单**必须**由人工给出；
    工具**绝不**从文件名 / 正文 / URL / mtime / 当前时间或其他上下文推断、补造授权、发布时间、
    采集时间、availability 或 OOS 语义；
  - **复用而不复制契约**：manifest 字段 / 允许键 / 必填项 / 格式词表**直接复用** GOLD-011 的
    `MANIFEST_REQUIRED_FIELDS` / `ALLOWED_MANIFEST_KEYS` / `FILE_ENTRY_REQUIRED_FIELDS` /
    `SUPPORTED_FILE_FORMATS` / `INBOX_SCHEMA_VERSION` 与 `evidence-intake-v1`
    （`schema_version` / `contract_version` 自动写入），引用校验复用 `valid_reference`、
    逐行预检复用 `read_input_file` + `assess_row`，**不复制、不降低**任何资格规则与阈值；
    另加**内部自检** `_check_manifest_contract(...)`：生成的 manifest 必须**正好**满足 GOLD-011
    契约（键 / 必填声明 / 文件条目键 / 格式词表 / 64 位小写摘要），否则 `EvidencePackageInternalError`；
  - **内容级 SHA-256 且确定性排序**：每个文件按**原始字节**计算 SHA-256，`manifest.files` 按规范化
    相对路径**确定性排序**；同一输入 + 同一显式元数据（含声明顺序不同）→ **byte-stable** manifest；
    manifest 里**没有任何时间字段**（无 `generated_at`、不读 mtime / ctime），
    真实身份 = 文件内容摘要 + 结构标记；
  - **只读既有 evidence + fail-closed 清单**：拒绝绝对路径（POSIX / Windows / UNC）/ `..` 路径穿越 /
    多级路径 / 符号链接（文件与 package 目录都不跟随）/ 目录 / `manifest.json` 自引用 /
    未支持扩展或格式（`auto` 按后缀解析，未知后缀拒绝） / 重复声明 / 大小写冲突路径 /
    package 目录不存在或不可读 / **包内未声明文件** / package 目录名或文件名命中示例合成词表
    （`SYNTHETIC_EVIDENCE`）；**绝不**移动 / 删除 / 改名 / 改写任何原始 evidence（越界文件从不被读取）；
  - **默认 dry-run / 唯一写开关**：`run_package_builder(..., out_path=None)` 只返回预览（**不取锁、
    不写任何文件**）；只有显式 `out_path` 才写，且**必须正好**是 `<package-dir>/manifest.json`
    （写到其它任意路径 → `MANIFEST_OUT_NOT_TARGET`，退出码 `3`、零写入）；
  - **原子写 + 单实例锁 + 不静默覆盖**：写盘前先取 GOLD-010 **单实例锁**（缺省锁文件**刻意放在
    package 目录之外**：同父目录 `<package-dir>.manifest.lock`，否则会成为"包内未声明文件"
    并让 scanner 整包隔离），锁内**重新**读取文件、重新计算摘要与预检（防 TOCTOU），再做既有
    manifest 核验 —— **不存在 → 创建**；**逐字节一致 → 幂等成功**（`IDEMPOTENT_UNCHANGED`，
    不重写）；**内容不同 → `MANIFEST_CONFLICT`** fail-closed（**没有** `--force` / `--overwrite`，
    更正必须新建 package 目录 / 新内容身份）；落盘后**复读自检**（字节不一致 → `MANIFEST_VERIFY_FAILED`）；
  - **写入前同源预检**：`preflight_rows(...)` 复用 `read_input_file` + `assess_row`，输出
    `accepted` / `quarantined` / `not_oos_eligible` / 稳定原因码计数与**逐文件计数**，
    并给出描述性 `outcome`（`NO_ROWS` / `HAS_QUARANTINED_ROWS` / `NO_ACCEPTABLE_ROWS` /
    `ACCEPTED_WITHOUT_OOS` / `ACCEPTED_WITH_OOS`）；预检结果**不写入** evidence 原始行，
    **绝不**把预检通过宣称为"授权已人工确认"或"data qualification PASS"；
  - **凭据与敏感值 fail-closed**：`authorization_reference` 走 `valid_reference`；
    `source` / `time_semantics` / `availability_semantics` / `notes` 走长度 / 控制字符校验；
    凭据类键名（`api_key` / `token` / `secret` …）、疑似凭据 blob、以及**会被既有脱敏规则命中的
    取值**一律**拒绝**（宁拒绝不静默改写），错误信息只给字段名与稳定原因码、**绝不回显取值**；
    `notes` 走 `safe_text` 脱敏 + 限长（输入上限 `MAX_NOTES_INPUT_CHARS=4000`，超长 fail-closed）；
  - **持续硬编码 blocker**：所有 artifact 恒为 `blocker_active=true` / `human_gate_required=true` /
    `requires_human_action=true` / `data_qualification_passed=false` /
    `phase_transition_allowed=false` / `auto_intake_allowed=false` / `writes_database=false` /
    `writes_project_state=false`；`PACKAGE_BUILDER_NOTE` / `PREFLIGHT_NOTE` /
    `MANIFEST_TIME_FREE_NOTE` 明示"生成 manifest ≠ 授权已核验 ≠ 资格通过 ≠ 可提交 L3"，
    `PHASE3_3_DATA` **保持 BLOCKED**；
  - `EvidencePackageCode`（复用 GOLD-011 inbox / `evidence-intake-v1` 同义原因码 + builder 专有码）、
    `ManifestWriteStatus` / `PreflightOutcome` / `ManifestFileEntry` / `RowPreflight` /
    `EvidencePackagePreview`（`to_dict()` 稳定机器可读）。
- **CLI `scripts/evidence_package.py`**：`--package-dir` / `--file`（可重复）/
  `--evidence-type` / `--source` / `--authorization-reference` / `--time-semantics` /
  `--availability-semantics` / `--historical-oos-applicable true|false` **必填**；`--notes` / `--out` /
  `--lock` / `--as-of`（ISO8601 必须带时区）/ `--json` 可选；**没有**任何 intake / `--no-dry-run` /
  `--force` / `--overwrite` 参数；`--json` 时 stdout 保持纯 JSON（提示走 stderr）、失败路径 stdout
  为空且 stderr 已脱敏；退出码 `0`（预览 / 写入 / 幂等）`2`（参数）`3`（目录或输出不可用）`4`
  （输入 fail-closed）`5`（`MANIFEST_CONFLICT`）`6`（锁冲突）；
- **`src/evidence/__init__.py`**：导出新 API（`package_builder_exit_code_for` / `EvidencePackage*` /
  `ManifestFileEntry` / `ManifestWriteStatus` / `PreflightOutcome` / `RowPreflight` /
  `EVIDENCE_TYPES` / `PACKAGE_BUILDER_*` / `PREFLIGHT_NOTE` / `MAX_*` 等）并在包文档说明边界。

### 2. 测试与验收

- **`tests/unit/test_evidence_package_builder.py`（57 项）**：确定性摘要与排序 / byte-stability /
  GOLD-011 契约自洽（键与必填项）/**递归无任何时间键** / csv & jsonl & `.json` 格式解析 /
  显式元数据缺失与非法值（含大小写敏感、`http://`、`docs/legal/`、非布尔 OOS）/
  空文件清单 / naive 时点 / package 目录缺失或符号链接 / 绝对路径 / `..` / 多级路径 /
  `manifest.json` 自引用 / 未支持格式 / 缺失文件 / 重复声明 / 大小写冲突 / 目录 / 未声明文件 /
  被链接的 evidence 文件 / 示例合成名称 / 凭据类引用（不回显）/ 凭据类 notes / notes 脱敏限长 /
  预检隔离与 not_oos 计数 / 非法行 `ROW_UNREADABLE` / 声明 OOS=false 的告警 /
  `--out` 越界拒绝 / 原子写无残留且锁在包外 / 幂等同内容 / 不同内容冲突且旧 manifest 保留 /
  既有损坏 manifest 不被覆盖 / 锁冲突零写入 / News scope / Markdown 保留 blocker 且不泄密 /
  源码守卫（无网络 / 无数据库 / 无 `unlink`/`rmtree`/`os.remove`/`os.rename`/`shutil.move` /
  无 `intake_evidence` / 无 `getmtime`/`getctime`/`st_mtime`/`st_ctime` / 无 `datetime.now` /
  无 `PROJECT_STATE.json` / 无常驻循环）；
- **`tests/integration/test_evidence_package_builder_integration.py`（8 项）**：**真链路** ——
  CLI dry-run（零写入）→ CLI `--out` 原子写 → **真实 GOLD-011 inbox scanner** 只读预检
  （`preflight_pass=1`、`quarantined=0`、`skipped=0`、逐文件 `digest_verified=true`、
  摘要等于真实字节摘要、安全字段恒定）；**坏包仍 fail-closed** —— 证据被追加一行后 scanner 判
  `EVIDENCE_DIGEST_MISMATCH` 并隔离（退出 `5`、`preflight_pass=0`），builder 再次写入 →
  `MANIFEST_CONFLICT`（退出 `5`、零写入、旧 manifest 逐字节保留）；无 manifest 的"裸"包
  由 scanner 判 `MANIFEST_MISSING`（证明 builder 产物**不是**绕过手段）；CLI 退出码
  `2`（缺参数 / naive `--as-of` / 非法枚举）`3`（package 目录缺失、`--out` 越界）`4`（声明文件缺失）
  `6`（锁冲突零写入）与 stdout / stderr 纯净性；**真实子进程**冒烟
  （`python -m scripts.evidence_package` 写入 + 幂等复跑 → `python -m scripts.evidence_inbox` 预检）。
  全部临时目录 / 本地文件 / 零网络 / 零数据库；
- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2557 passed / 1 skipped**（新增 65 项）；
  - `.venv\Scripts\python.exe -m ruff check .` → **All checks passed!**；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` → **Success: no issues found
    in 172 source files**（GOLD-016 为 170）。

### 3. 范围守规

- 只读输入、显式人工输入、默认零写入；`--out` 是唯一写开关（先取单实例锁再原子写 + 写后复检），
  且只能写 `<package-dir>/manifest.json`；
- 未新增 / 未升级任何第三方依赖；未新增 migration / schema；未联网、未接第三方推送、
  未读取浏览器 / 邮件 / 云盘凭据、未修改任何 OS 计划任务、零数据库写入；
- *复用而不复制*：manifest 契约 / 原因码 / 引用校验 / 逐行预检 / 脱敏 / 原子写 / 单实例锁全部复用
  GOLD-005 / GOLD-010 / GOLD-011 的既有实现；`src/alpha/**`、`src/monitoring/**`、
  `src/scheduler/**`、`src/collectors/**`、`src/processors/**` 与 GOLD-005 ~ GOLD-016 的既有模块
  **均未改动**（`src/evidence/__init__.py` 仅新增导出与说明）；
- **未降低任何阈值 / 资格规则**，未用 Mock / 模板 / 示例宣称真实数据资格；
- **未解除** `PHASE3_3_DATA`：artifact 持续显式 `blocker_active=true` /
  `human_gate_required=true` / `data_qualification_passed=false` /
  `phase_transition_allowed=false` / `auto_intake_allowed=false` / `writes_database=false`；
  未进入 Phase 3.4、未训练 Alpha、未生成任何交易信号或订单；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 未变；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git
  写操作（commit / push / reset / rebase / merge 由 Orchestrator 负责）；
- 顺手把 GOLD-016 文档中的全量 pytest 数字对齐 `.ai/results/GOLD-016.json` 的权威结果
  （2488 → **2490 passed / 1 skipped**），**未改**历史 result artifact。

### 4. 遗留 / 下一步

- 仍无真实合格授权证据 → `PHASE3_3_DATA` 保持 **BLOCKED**。GOLD-017 **只是**"把人工显式元数据 +
  人工指定的现有文件机械整理成 GOLD-011 可直接扫描的 manifest"的**交付入口**：它**不产生**任何
  真实授权证据、**不判断**法律效力、**不具备**解除 blocker 的能力；
- 完整人工路径（**九步**）：①业务方把真实授权 Author / News 文件放进 package 目录 →
  **`evidence_package`（GOLD-017）** 用显式元数据生成 `manifest.json`（默认 dry-run，确认后
  `--out`）→ ② `evidence_inbox --out` 只读预检 → ③ `evidence_review --decision approve
  --approved-out` 人工复核 → ④ `evidence_intake_plan --out` 生成计划 → ⑤ 人工**显式**执行
  `evidence_operator workflow --no-dry-run --manifest` 落库 → ⑥ `evidence_operator recheck
  --no-dry-run --report` 复核 → ⑦ `evidence_intake_receipt --out` 生成收据 →
  ⑧ `evidence_decision_packet --out` 生成决策包 → ⑨ `evidence_decision_record --decision approve
  --reviewer <label> --out` 记录 L3 人工决策 → `--verify-record` 事后防伪复核 →
  **L3 人工确认**（由人工 / Orchestrator 显式更新 `PROJECT_STATE`）；
- builder **不缓存**任何输入：元数据 / 文件内容 / 声明顺序任一变化 → 新 manifest 内容（新摘要）
  或直接 fail-closed；既有 manifest 内容不同时**绝不**静默覆盖，更正必须新建 package 目录；
- `--out` 是**唯一**写入口；package 位于 inbox 内时建议显式 `--lock <inbox 之外>/xxx.lock`，
  避免在 inbox 根目录留下锁文件痕迹（锁文件**绝不**放在 package 目录内）。

---

## 第八十二轮（2026-09-23）：GOLD-018 —— monitoring/evidence 循环导入修复与 Import-Order 回归门禁

### 1. 交付内容

修复 GOLD-017 任务外发现、并在**纯净 HEAD** 复现的**包初始化期循环导入**（`goal` 只允许改模块
边界与导出依赖，**不得**改 Phase 3.3 资格算法 / 阈值 / 证据契约 / 安全语义）：

- **故障与根因**（先锁定最小可复现，再动手）：

  ```text
  # 修复前（纯净 HEAD 实测）
  .venv\Scripts\python.exe -c "import src.monitoring; import src.evidence"
  ImportError: cannot import name 'BatchQuantification' from partially initialized module
  'src.monitoring.evidence_readiness' (most likely due to a circular import)

  # 同一份代码，反过来的顺序却正常
  .venv\Scripts\python.exe -c "import src.evidence; import src.monitoring"   # OK
  ```

  两个包的 `__init__` 都在**包初始化阶段** eager import 整个依赖图：
  `src.monitoring.__init__` → `evidence_readiness` → `src.evidence.contracts`（触发
  `src.evidence.__init__`）→ `decision_packet` → `readiness_runner` → `handoff` →
  回跳 `src.monitoring.evidence_readiness`（此时它只执行到第 38 行、`BatchQuantification`
  尚未定义）→ `ImportError`。因此"先导入谁"决定成败，属**包边界治理缺陷**；
- **修复方式（包边界治理，PEP 562 惰性导出）**：`src/evidence/__init__.py`（376 个公开名）
  与 `src/monitoring/__init__.py`（39 个公开名）不再在初始化阶段 import 任何子模块，改为：

  - `_LAZY_EXPORTS_BY_MODULE`：只登记 `公开名 -> 定义子模块`（**不复制**任何类 / 阈值 / 枚举 /
    常量，单一事实源仍是子模块）；
  - `_LAZY_EXPORT_ALIASES`：公开名与定义处属性名不同的别名（6 个 `*_exit_code_for`）；
  - `_LAZY_EXPORTS`：合并后的 `公开名 -> (子模块, 属性名)` 总表；
  - 模块级 `__getattr__`：**首次访问**才 import 其定义子模块并缓存进模块字典；子模块名
    （`from src.evidence import decision_packet` 等）同样惰性可见；未登记名字抛 `AttributeError`
    （**绝不**静默返回 None、**绝不**吞掉 `ImportError`）；
  - 模块级 `__dir__`：`dir()` 与 eager 版一致（模块字典 ∪ 公开导出）；
  - `__all__` **逐字节未变**；`from src.evidence import X` / `from src.monitoring import Y` /
    `from src.<pkg> import <子模块>` / `import src.<pkg>` 后取属性的既有用法**完全兼容**；
- **新增门禁测试（3 个文件，共 56 项）**：
  - `tests/unit/test_lazy_package_exports.py`（15 项）：`__all__` 与惰性表**同源**；每个公开名
    必须 **is** 其定义子模块上的对象（证明无第二份事实源）；别名表自洽；首次访问后缓存进模块
    字典；既有公开导出见证（GOLD-005 ~ GOLD-017）仍在；子模块名仍可访问；未登记名字抛
    `AttributeError`；重复导入幂等；**源码级守卫**：两个包的 `__init__` 模块级只允许
    `__future__` / `importlib` / `typing`（谁把 eager 子模块导入写回来，用例立刻失败）；
  - `tests/integration/test_evidence_import_order.py`（12 项）：在 **fresh subprocess** 里跑 9 种
    导入顺序（monitoring-first / evidence-first / `handoff` first / `evidence_readiness` first /
    `decision_packet` first / from-import 两种方向 / star-import / 重复与交错导入），断言成功且
    不再出现循环导入签名；另加"包初始化不得拉入对方包"（`import src.evidence` 不得拉入
    `src.monitoring`；`import src.monitoring` 不得拉入 `src.evidence.handoff` /
    `decision_packet`）与"公开名仍 `is` 同一对象、`PHASE3_3_DATA` 仍 BLOCKED"两项语义门禁；
  - `tests/integration/test_evidence_cli_smoke.py`（29 项）：`scripts/evidence_*.py`（12 个）+
    `scripts/intake_evidence.py` + `scripts/report_collector_health.py` 在 fresh subprocess 里
    **可 import** 且 `--help` 可用；子进程 cwd 指向**空**临时目录（项目以 `PYTHONPATH` 注入）→
    跑完断言**零文件写入**（零网络 / 零数据库，`--help` 在 argparse 阶段退出）；另加覆盖守门：
    `scripts/evidence_*.py` 新增入口必须同步登记，否则用例失败。

### 2. 测试与验收

- **全量门禁**（本轮实测，项目 `.venv`）：
  - `.venv\Scripts\python.exe -m pytest tests -q` → **2646 passed / 1 skipped（315.10s）**
    （新增 56 项；HEAD 基线为 2590 passed + 1 skipped = 2557（GOLD-017 权威结果）+
    33（随后 3 个 orchestrator 测试提交）；另注：`tests/unit/test_config_encoding.py`
    会 `rglob` 仓库内配置文件，本机 `.ai/runtime/**` 的运行期 JSON 会让收集数再 +2，
    与本次改动无关）；
  - `.venv\Scripts\python.exe -m ruff check .` → **All checks passed!**；
  - `.venv\Scripts\python.exe -m mypy config database src scripts` → **Success: no issues found
    in 172 source files**（与 GOLD-017 同数：本次未新增 / 删除源文件，只改两个包的 `__init__`）；
- **修复前失败证据（本次实测）**：把 3 个新测试文件放进 `git archive HEAD` 解出的**纯净**树
  （未做任何修复）后运行 → **16 failed / 40 passed**，失败集中在 monitoring-first 系列导入顺序、
  "包初始化不得拉入对方包"、惰性表一致性、源码级守卫与 `report_collector_health` CLI 冒烟
  （evidence-first 序列与证据链 CLI 冒烟按预期通过）→ 证明新增门禁**确实**能抓住旧行为；
- 三个新增文件在本工作区（修复后）单独复跑：**56 passed**（单元 15 / 集成 41），其中 26 个 CLI
  子进程用例与 12 个导入顺序子进程用例全部通过（子进程 cwd 为空临时目录 → 零写入可断言）。

### 3. 范围守规

- 只改**两个包的 `__init__`**（导入块 → PEP 562 惰性导出表）+ 文档 + 测试；业务子模块
  （`contracts` / `intake` / `ledger` / `decision_packet` / `handoff` / `readiness_runner` /
  `evidence_readiness` / `phase33_qualification` …）**未改动一行**；
- **未改** `evidence-intake-v1`、`PHASE3_3_DATA` 阈值、readiness 算术、安全字段、L3/L4 Gate、
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未新增 / 未升级任何第三方依赖；未新增 migration / schema；**未联网**（新增用例只做本地
  import 与 `--help`，不建连、不连真实数据库）、零数据库写入、零文件写入（以空 cwd 断言）；
- **未通过**删除公开 API、注释 / skip 测试、导入期吞异常来掩盖循环依赖：`__all__` 未变，公开名
  解析对象与旧版**逐一同源**（单元用例以 `is` 断言），未登记名字**显式**抛 `AttributeError`；
- **未解除** `PHASE3_3_DATA`（仍 BLOCKED）；未进入 Phase 3.4、未训练 Alpha、未生成交易信号或订单；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`；未执行任何 git 写操作
  （commit / push / reset / rebase / merge 由 Orchestrator 负责）；生成脚本仅存在于系统临时目录，
  未进入仓库。

### 4. 遗留 / 下一步

- 惰性导出把"包初始化即暴露全部名"改为"首次访问才 import 其定义子模块"（解析结果缓存，后续零
  额外开销）：这要求子模块导入保持**无环**，已由导入顺序用例锁定；后续若新增跨包依赖，必须先跑
  `tests/integration/test_evidence_import_order.py`；
- 惰性表与 `__all__` 仍需**人工同步**（新增公开导出必须同时更新），漏更新会被单元门禁当场抓住；
- `PHASE3_3_DATA` **仍 BLOCKED**：本轮只修导入顺序，不产生任何真实授权证据；真实证据仍需业务方
  提供 + 人工核验 + **L3 人工 Gate**（完整九步人工路径见第八十一轮 §4）；
- 建议下一步：真实授权 Author / News 语料按 GOLD-017 → GOLD-011 → GOLD-012 → GOLD-013 →
  显式落库 → GOLD-014 → GOLD-015 → GOLD-016 的九步路径推进，由人工完成授权与 L3 Gate。

---

## 第八十三轮（2026-09-23）：GOLD-020 —— Git Push 冲突恢复状态机与 Result 语义一致性加固

### 1. 背景（GOLD-016 实际暴露的两个问题）

- 任务 / validation / commit 全部成功，首次 `push` 因 `remote contains work` 被拒；
  下一轮 `pull --rebase` 后 push 成功，且**没有重跑任务**——这条恢复路径实际有效，
  本轮用 mock 回归测试把它锁死。
- GOLD-016 result 同时出现 `status=completed` / `cline_exit_code=0` / 全部 validation 通过，
  但唯一的 `finish_reason=aborted`（其实是 Cline raw 值）→ 结果语义歧义。

### 2. 交付内容

- `orchestrator/ai_orchestrator.py`：
  - 新增 Orchestrator 判定词表（`EXECUTION_OUTCOME_*` / `NORMALIZED_FINISH_REASONS`）与
    `normalize_finish_reason()` / `cline_finish_reason_raw()` / `attempt_outcome()`；
  - `build_attempt_record()` 同时写 `cline_finish_reason_raw`（raw，可能是 `aborted`）与
    `execution_outcome` / `normalized_finish_reason`；兼容旧键 `finish_reason` 改写入**归一化值**；
  - `write_final_result()` 增加 result 级 `execution_outcome` / `normalized_finish_reason`；
  - `process_task()` 把 success 判定收敛为 `outcome == EXECUTION_OUTCOME_COMPLETED`
    （attempt 记录与最终成功判定**同源**，不再可能自相矛盾）；
  - push 失败时写 runtime `push_pending` 状态（`local_result=completed` /
    `push_status=pending` / `remote` / `branch`），本轮不启动任何任务、不重跑 Cline；
  - `sync_repository()` 增加 fail-closed 日志，并在 `remote synced` 后清理 `push_pending` 状态；
  - 抽出 `run_iteration()`（main loop 单轮），固化「先 Git sync，同步成功后才允许 rolling queue 继续」。
- `tests/unit/test_ai_orchestrator_queue.py`：+16 用例（`FakeGit` 全 mock，零真实远端）：
  非 fast-forward 首推失败 → 下一轮 rebase + push 成功；`pull --rebase` 冲突严格停线且
  Cline 不重跑、本地 commit/result 保留；`ahead=0/1/unknown` 兼容且无 force push / reset；
  raw `aborted` 与归一化 `completed` 并存；旧 result（无新字段）仍可解析且不回写。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.2「Result 字段与 Git Push 恢复契约」。

### 3. 范围守规

- 未新增第三方依赖；Git 行为全部 mock，**不访问真实 GitHub**；未对真实仓库 force push / reset / rebase；
- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`src/**`、`database/**`；
- 未改业务 Phase、数据资格 Gate、`LIVE_TRADING`；Cline 未执行任何 git 写操作。

### 4. 遗留 / 下一步

- 历史 result（含 GOLD-016 的 `finish_reason=aborted`）**保持原样**，新字段/语义只对新 result 生效；
- 建议下一步：真实语料继续按 Evidence 九步路径推进（授权 → inbox → review → plan → 显式落库 →
  receipt → 决策包 → L3 Gate），Phase 切换仍需人工 L3。

---

## 第八十四轮（2026-09-23）：GOLD-021 —— start_agent 启动前 bootstrap sync 与版本可见性加固

### 1. 背景（本轮实际暴露的问题）

- 旧 `start_agent.bat` 直接启动 `orchestrator/ai_orchestrator.py`，Git 代码同步只发生在
  Python 进程**内部**（`sync_repository()` 的 `pull --rebase`）：磁盘上的代码已经更新，
  但已加载的 Orchestrator 仍执行旧代码内存，且启动日志里看不到「本进程到底加载了哪个版本」。

### 2. 交付内容

- 新增 `orchestrator/bootstrap_sync.py`（启动前同步模块，可 `python -m` 直接运行）：
  - 只读探测 `branch --show-current` / `rev-parse HEAD` → dirty 检查 → `fetch --prune`
    → 本地落后 `merge --ff-only`；真正分叉才 `rebase <remote>/<branch>`；
    同步后必须核对 `HEAD == remote_sha`，否则 fail-closed；
  - **fail-closed 词表**：`skipped_dirty`（退出码 `3`）/ `wrong_branch`（`2`）/
    `fetch_failed` / `remote_ref_missing` / `rebase_conflict` / `failed`（`4`）/ git 不可用（`5`）；
    dirty 时连 `fetch` 都不执行；rebase 冲突必须 `rebase --abort` 回原状，本地 commit/修改一律保留；
  - 启动日志打印 branch / HEAD 短 SHA / provider / model / sync 结果，并原子落盘
    `.ai/runtime/bootstrap_state.json`（Git 已忽略）；**绝不打印任何凭据**。
- `start_agent.bat`：启动顺序固定为 `py -u orchestrator\bootstrap_sync.py` →
  `if errorlevel 1 (… pause & exit /b 1)` → `py -u orchestrator\ai_orchestrator.py`；
  同步失败绝不启动 Orchestrator；DeepSeek hard-pin 与队列默认值保持不变。
- `orchestrator/ai_orchestrator.py`：新增 `head_short_sha()` / `abbrev_sha()` /
  `commit_prefix_matches()` / `read_bootstrap_sync_state()` / `describe_bootstrap_sync()` /
  `bootstrap_head_mismatch()`；启动日志打印 `HEAD` 与 `Bootstrap sync: …`，
  磁盘 HEAD 与 bootstrap 记录的 `head_after` 不一致时告警；运行中检测到磁盘代码前进时告警
  「当前进程仍运行启动时加载的代码，请重启 start_agent.bat」（运行中的进程不会热加载）。
- `tests/unit/test_start_agent_bootstrap.py`：新增 **33 项**测试（单元 27 + 集成 6）：
  - fake git（单元 27 项）：稳定命令白名单与顺序、dirty/分支不符/网络失败/远端 ref 缺失/
    ff 后 HEAD 不一致/rebase 冲突 + abort/本地领先 等 fail-closed 路径，
    状态落盘与报告**不含任何凭据**，launcher 顺序与 `exit /b 1` 守卫的文本契约，
    以及 Orchestrator 侧版本可见性函数（`describe_bootstrap_sync` / `bootstrap_head_mismatch`）；
  - 真实 git 集成（6 项，临时 bare 远端、零真实网络、零真实仓库写操作）：
    `v1` → 远端发布 `v2` → bootstrap sync fast-forward 后**新起的 Python 进程**读到 `v2`
    （证明加载的是新版本而不是旧进程内存）、幂等 `up_to_date`、
    dirty 保留本地修改且连 `fetch` 都不执行、rebase 冲突 `--abort` 后本地 commit 与工作区完好、
    CLI 退出码与 runtime 状态。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.3「启动前 bootstrap sync 与版本可见性契约」。

### 3. 双方证据（真实执行）

- 真实仓库（工作区 dirty）执行 `py -u orchestrator\bootstrap_sync.py`：
  `Sync result: skipped_dirty`、退出码 `3`、`git status --short` 前后**逐行一致**（0 处差异）。
- 干净临时 clone 执行同一 CLI：`Sync result: up_to_date`、退出码 `0`、
  落盘状态含 `provider=deepseek`、`head_before == head_after`；
  以该状态文件驱动 Orchestrator 侧可见性函数输出
  `Bootstrap sync: OK result=up_to_date branch=cline-agent head_before=… head_after=… provider=deepseek`。

### 4. 范围守规

- 未新增第三方依赖；未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、
  `src/**`、`database/**`；未改业务 Phase、数据资格 Gate、`LIVE_TRADING`；
- Cline 未执行 `git commit/push/reset/rebase/merge`（校验用的 git 命令仅发生在**临时目录**
  的自建仓库里，从未对真实仓库做写操作）。

### 5. 遗留 / 下一步

- 运行中的 Orchestrator 仍**不会**热加载新代码：若同一进程运行期间 `pull` 带来新 commit，
  只会有明确告警，必须重启 `start_agent.bat`（本轮按最小改动不引入自动 re-exec / 自重启）；
- 建议下一步：真实语料继续按 Evidence 九步路径推进（授权 → inbox → review → plan → 显式落库 →
  receipt → 决策包 → L3 Gate），Phase 切换仍需人工 L3。

## 第八十五轮（2026-09-23）：GOLD-022 —— 异常中断任务的安全现场恢复状态机

### 1. 背景（GOLD-018 重启后遗留 dirty worktree 的真实事故）

- 进程被中断 / 重启后，工作区遗留一批**没有进入 Git** 的修改，现场证据只存在于工作区本身。
- 旧实现的两个问题：① `run_iteration()` 第一步 `sync_repository()` 一遇 dirty 就 fail-closed
  （outcome `deferred`）⇒ rolling queue 永久卡住，只能人工处理；② `process_task()` 中两处
  `if git_is_dirty(): reset_task_changes()` 会无条件 `git reset --hard HEAD` + `git clean -fd`
  ⇒ 既可能抹掉人工修改，也会把唯一的现场证据一并删除。
- 现实痕迹（仓库内可见）：`.ai/runtime/recovery/GOLD-018-attempt-0-20260923T110004+0800/`
  是**人工**抢救出来的现场；旧代码里没有任何可复现的状态机来做这件事。

### 2. 交付内容

- `orchestrator/ai_orchestrator.py`（只加不减，业务与队列语义不变）：
  - 新增恢复状态机常量与稳定词表：`ATTEMPT_EVIDENCE_SCHEMA` / `RECOVERY_SNAPSHOT_SCHEMA` /
    `RECOVERY_STATE_SCHEMA` / `LOCK_SCHEMA` / `WORKTREE_*` / `RECOVERY_OUTCOME_*` /
    `RECOVERY_EVENT_*` / `LOCK_ACTIVE|STALE|UNKNOWN`；
  - `current_attempt_baseline()`：attempt 开始时（工作区已确认 clean）写入现场基线
    `evidence_schema / pid / branch / head / worktree_clean / baseline_at`；
  - `interrupted_scene_assessment()`：6 条证据链的确定性判定——runtime **恰好 1 条** `running` 记录、
    记录 task == rolling queue 当前真正会执行的 task、带本版本 `evidence_schema`、
    owner PID 已不存在、HEAD/branch 与基线一致、该 task 无终态 result；缺一即 `WORKTREE_UNKNOWN`；
  - `recover_interrupted_worktree()`：单轮入口，固定跑在 Git sync 之前；干净 ⇒ 继续；
    可证明的中断现场 ⇒ snapshot → 校验 → 清理 → 重试**同一** task；其它 dirty ⇒ 停线
    （限流日志 + runtime 审计），绝不 reset / clean；
  - `save_recovery_snapshot()`：metadata 增补 `schema` / `tracked_files` / `untracked_sha256` /
    `patch_bytes` / `patch_sha256` / `head` / `branch` / `pid`（原字段全部保留，向后兼容）；
  - `verify_recovery_snapshot()` / `restore_recovery_snapshot()`：可恢复性校验（patch 哈希与字节数、
    untracked 副本哈希、patch 能被**反向应用**到当前工作区、副本与工作区内容一致）与 fail-closed 还原
    （拒绝覆盖内容不同的人工文件、拒绝恢复未通过校验的快照）；
  - `record_recovery_event()`：`.ai/runtime/recovery/recovery_state.json`（有界 20 条、原子写）；
  - `cleanup_interrupted_scene()`：snapshot 写入失败 / 校验失败 / 清理失败一律只停线并保留现场；
    成功后把 runtime 证据状态改写为 `recovered`（**证据一次性消费**，防止同一条证据之后被复用于
    清理另一次可能来自人工的 dirty 现场）；
  - `process_task()`：达到最大尝试次数时的清理不再是无条件动作——只有同一 task 的中断证据成立才允许
    清理，否则 `deferred` + 审计事件，历史 result 绝不改写；
  - lock：新增 `read_lock_evidence()` / `classify_lock_state()` / `archive_stale_lock()` /
    `running_process_image()`；`acquire_lock()` 改为三态 **fail-closed**（`active` / `unknown` 停线且
    **绝不删除**；`stale` 才归档留痕后清理）；新建 lock 带 `schema`；`is_pid_running()` 改为 PID 列
    **精确匹配**，且判活探测失败（tasklist 不可用 / 超时 / 非 0 退出）一律按「存活」处理。
- `tests/unit/test_ai_orchestrator_recovery_state_machine.py`（**新增 51 项**，全部在 `tmp_path` 内运行，
  对真实工作区零影响）：crash/restart 恢复全链路（快照内容、审计事件、**绝不写 result**）、
  证据一次性消费、未知 dirty worktree 绝不清理、证据链逐项缺失（多记录 / 异 task / PID 存活 /
  HEAD 漂移 / 分支漂移 / 缺 schema / 缺基线段 / 终态 result / 无可执行 task）、snapshot 写入失败、
  校验失败、清理失败、patch 被篡改、untracked 副本缺失 / 被改、`require_worktree_match`、空快照、
  restore 拒绝不可恢复快照；lock 的 stale 归档清理、旧版无 schema lock、active / 本进程 lock、
  9 类来源不明 lock（含判活探测失败、PID 精确匹配）、release 不删外来 lock；
  `run_iteration` 端到端（恢复后重跑**同一** task 并成功收口 / 未知 dirty 停线且不调用 Cline）；
  最大尝试次数分支（有证据才清理 / 无证据 `deferred`）；
  以及**真实 git** 临时仓库 round trip（snapshot → verify（反向 apply + 逐字节）→ reset/clean →
  restore 1:1 还原现场）。
- `tests/unit/test_ai_orchestrator_queue.py` / `tests/unit/test_ai_orchestrator_external_failures.py`：
  仅为新探测命令（`branch --show-current` / `rev-parse HEAD` / `status --porcelain`）扩展 fake git，
  并新增对 snapshot 可恢复性证据的断言（原有断言一条未删）。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.4「异常中断任务的安全现场恢复状态机（GOLD-022）」。

### 3. 范围守规

- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`src/**`、`database/**`；
- 未改业务 Phase、数据资格 Gate、`LIVE_TRADING`；未新增任何依赖；
- Cline 未执行任何 git 写操作；清理 / 恢复行为的测试全部发生在 `tmp_path` 的 fake git 或临时仓库内；
- 恢复判定是纯确定性代码，**不调用 Cline、不询问 LLM**，也不规划下一任务。

### 4. 遗留 / 下一步

- 无法证明来源的 dirty worktree（人工修改、多重证据、缺基线）仍然**必须人工处理**（设计如此）；
- snapshot 记录 tracked patch + untracked 内容：恢复不重建原来的 staged 状态（已在 §2.4 注明）；
- 建议下一步：按队列继续 GOLD-023（GPT planner / executor 边界契约）。


## 第八十六轮（2026-09-23）：GOLD-023 —— GPT Planner 只读项目快照与职责边界契约

### 1. 背景

- GPT 是**唯一**的 Planner / Reviewer / Architect；Cline / DeepSeek 只是 Executor。
  但在 GOLD-017/018 期间真实出现过「`.ai/PROJECT_STATE.json` 指针落后于 `.ai/results/**`」
  与「rolling queue 的 `task_queue` 声明与真实非终态任务漂移」：规划依据与实际仓库状态不一致，
  而当时没有任何**机器可读、只读、确定**的快照来暴露这件事。
- 本轮只建 **handoff / diagnostic 契约**，不接入任何新的模型 / 供应商 API，
  也不把 GPT 的规划权下放到本地模型。

### 2. 交付内容

- `orchestrator/planner_snapshot.py`（新增，**纯只读**）：
  - `build_planner_snapshot(...)`：汇总 `PROJECT_STATE`（原样回显）+ 非终态 task
    （依赖 / `human_gate` / `auto_start` / `requires_human_approval` / readiness 与原因）
    + result 终态（`terminal` / `unresolved` / `unknown` / `missing_results`）
    + 最近 reviewed/completed 指针 vs `latest_terminal_result` + 安全 Gate
    （blockers / invariants / L3、L4 等待项）+ `role_contract`，输出
    `schema=gold-ai/planner-snapshot/v1`；所有列表按**确定性任务序**排序
    （项目自身前缀例如 `GOLD` 视为最新，避免 `TEST-002` 这类历史任务被误判成最新结果）。
  - 一致性诊断 `issues`（稳定 code，**只报告不修复**）：`PROJECT_STATE_POINTER_BEHIND_RESULTS`
    / `_AHEAD_OF_RESULTS` / `_MISSING` / `_UNKNOWN_TASK`、`QUEUE_DECLARATION_MISMATCH`、
    `QUEUE_TASK_MISSING`、`UNKNOWN_RESULT_STATUS`、`TASK_FILE_INVALID`、
    `TASK_METADATA_INVALID`、`DEPENDENCY_GRAPH_INVALID`、`GATE_INCONSISTENT`、
    `QUEUE_GATE_STALLED`（warning）、`BLOCKER_STATE_INCONSISTENT`、
    `SAFETY_INVARIANT_MISSING`、`PROJECT_STATE_UNREADABLE`；同码同因去重后稳定排序。
  - `role_contract()` / `executor_capability()` / `executor_allowed()`：机器可测试的职责边界；
    Executor 只有白名单能力（`consume_approved_task` / `run_validation` /
    `report_task_result` / `recover_interrupted_worktree` / `request_planner_decision`），
    **未登记能力一律 fail-closed 拒绝**；`planner_decision_guards` 把每条 planner-only 决策
    绑定到拦住它的禁止能力。
  - 语义只复用 `ai_orchestrator`（queue / readiness / 依赖图 / 状态词表）：
    通过 `orchestrator_view()` 临时重定向只读视图并**必定还原**，不复制第二套实现。
  - CLI：`python -m orchestrator.planner_snapshot [--root/--state/--tasks-dir/--results-dir/--generated-at]`，
    `stdout` 是**纯 ASCII JSON**（机器通道，任意代码页安全），`stderr` 只放人类可读 issue 摘要；
    退出码 `0` 无漂移 / `2` 检出漂移 / `3` `PROJECT_STATE` 不可读。
- `tests/unit/test_ai_orchestrator_planner_snapshot.py`（**新增 57 项**，全部在 `tmp_path` 内，
  对真实仓库零写入）：只读性（快照前后工作树 sha256 完全一致、两次构建完全一致、
  渲染可 round-trip）/ 源码守卫（模块内不存在 `write_text` / `open(` / `mkdir` / `unlink` /
  `rmtree` / `os.replace` / `subprocess` / 网络库与 `while` 循环）；本轮真实漂移场景
  （state 仍 GOLD-017/018、results 已到 GOLD-020；以及当前仓库 GOLD-020/021 vs GOLD-022）
  与指针 ahead / missing / unknown task；queue 声明不一致的 7 种形态（未声明 pending、
  已终态仍在声明、声明缺文件、顺序不符、非法类型、ACTIVE 却无 pending、一致时不误报）；
  未知 task / result status（`frobnicated`、缺 `status` 字段、损坏 result、损坏 task、
  task_id 与文件名不一致、非法 `depends_on` / 非 bool `auto_start` / 未知 `human_gate`、
  缺失依赖、依赖环）；Gate / blocker / 安全不变量一致性与 L3 停线 warning；职责边界契约
  （planner 只属 GPT、禁止能力全拒、白名单允许、未知能力 fail-closed、flag 全 false）与
  CLI（stdout/stderr 分流、退出码 0/2/3、子进程字节级稳定、默认读真实仓库且**零写入**）。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.5「GPT Planner / Executor 职责边界与只读项目快照」。

### 3. 实测结果（只读，未做任何修复）

- 对**当前真实仓库**运行 `python -m orchestrator.planner_snapshot` 得到退出码 `2`，
  检出 4 条漂移：`current_task=GOLD-021` 与 `last_completed_task` / `last_reviewed_task=GOLD-020`
  落后于 `latest_terminal_result=GOLD-022`（3 条 `PROJECT_STATE_POINTER_BEHIND_RESULTS`），
  以及 `task_queue=['GOLD-021','GOLD-022','GOLD-023']` 中 `GOLD-021/022` 已终态
  （1 条 `QUEUE_DECLARATION_MISMATCH`）。
- 快照**没有**修改 `.ai/PROJECT_STATE.json`、`.ai/tasks/**`、`.ai/results/**`
  （测试用 sha256 逐文件验证；CLI 默认读真实仓库同样零写入）。

### 4. 范围守规

- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`src/**`、`database/**`；
- 未改业务 Phase、数据资格 Gate、Human Gate 档位、`LIVE_TRADING` 或外部订单开关；
  未新增任何依赖、未接入任何模型 / 供应商 API；Cline 未执行任何 git 写操作。

### 5. 遗留 / 下一步

- `PROJECT_STATE` 指针仍落后于 results（`GOLD-021/022` 已终态但 state 未更新）：
  **按设计只报告、不自动修复**，是否更新状态指针 / 补 rolling queue 由 GPT 判断；
- `TEST-001` / `TEST-002` 历史任务与 result 仍保留在队列目录中，快照已用「项目前缀优先」
  的确定性排序避免其干扰 `latest_terminal_result` 判断；
- 建议下一步：GPT 按快照输出决定队列补充与状态指针更新，并继续推进真实语料 Evidence 路径。


## 第八十七轮（2026-09-23）：GOLD-024 —— Planner Snapshot 固化为稳定 CLI 与机器可读交接产物

### 1. 背景

- GOLD-023 已交付只读 planner snapshot，但 GPT 每轮仍要从分散文件重建状态：
  没有 Git branch/head 绑定、没有版本化 JSON 契约、`generated_at` 与状态事实混在一起
  导致「确定性」不可机器判定，CLI 也没有任何受控落盘通道。
- 本轮只把 GOLD-023 的只读快照**固化为稳定、版本化、确定性的 CLI / JSON 契约**，
  同时保持 Executor 无规划权限；不进入 Phase 3，不新增模型 / 供应商 API 或网络依赖。

### 2. 交付内容

- `orchestrator/planner_snapshot.py`（改造，仍然**纯只读**）：
  - 版本化：`schema=gold-ai/planner-snapshot/v2` + 整数 `schema_version=2`；
  - 新增 `git`（`branch` / `head` / `head_short` / `detached` / `available`）：只读解析
    `<root>/.git/HEAD` 与 loose ref / `packed-refs`（兼容 `.git` 为 gitdir 指针的 worktree），
    不执行任何 Git 命令或写操作；解析不了时 fail-closed 报告 `GIT_INFO_UNAVAILABLE`；
  - 新增 `state`（PROJECT_STATE 摘要：phase / status / 三个指针 / declared queue /
    blocker 与 human gate code / invariants / next_action），原样回显 `project_state` 保留；
  - 新增确定性契约：`facts_digest`（状态事实 sha256，wall-clock 与自引用字段
    `generated_at` / `facts_digest` / `determinism` 全部排除）+ `determinism`
    （`ordering` 排序规则 / `excluded_from_facts` / `wall_clock_in_facts=false`）；
  - `build_planner_snapshot(root=...)` 让 Git 事实与 `paths.root` 一起绑定被检查的仓库根；
  - CLI 新增受控 `--output`：默认仍只写 stdout；给 `--output` 时 JSON 只写受控文件、
    stdout 保持为空；目标被拒时退出码 `4` 且**不写任何文件**。
- `orchestrator/planner_snapshot_output.py`（新增，唯一写路径单点隔离）：
  `resolve_output_target()` / `write_snapshot_output()` / `guard_summary()`；只允许
  `<root>/.ai/runtime/**` 与系统临时目录；`.ai/tasks`、`.ai/results`、`.ai/PROJECT_STATE.json`、
  `.git`、`src`、`database`、`config`、`data`、`docs` 等一律拒绝；`..` 逃逸先解析再判定；
  父目录不存在时**不创建目录**；不做 Git / 网络 / 子进程操作。
- `tests/unit/test_ai_orchestrator_planner_snapshot.py`（**新增 14 项，共 71 项**）：
  版本化契约 + Git / state 事实；缺失 Git 事实 fail-closed；`facts_digest` 与 wall-clock 解耦、
  可复算、对状态变化敏感；GOLD-021/022/023 全部 completed 而 state 落后；
  损坏 task + 未知 result status + queue 漂移下的零修复断言；CLI 选项契约无规划开关；
  快照 / 输出模块无 follow-on planning 与状态写入 API（含源码守卫）；受控 `--output` 的
  允许 / 禁止路径、`..` 逃逸、父目录缺失、退出码 `4`、默认零写入、子进程字节稳定。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.6「稳定 Planner Snapshot CLI / JSON 契约（GOLD-024）」，
  并把 §2.5 的 schema 引用更新为 v2、补 `GIT_INFO_UNAVAILABLE`。

### 3. 测试结果

- `pytest tests/unit/test_ai_orchestrator_planner_snapshot.py -q` → `71 passed`；
- `pytest tests -q` → 全量通过；
- `ruff check .` → All checks passed；`mypy config database src scripts` → Success。

### 4. 范围守规

- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`src/**`、`database/**`；
- 未改 Phase 3.3 blocker、L3/L4 Gate、`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未新增依赖 / 模型 / 供应商 API / 网络调用；Cline 未执行任何 Git 写操作；
- 写入测试全部发生在 `tmp_path`（并显式构造 fake `.git`），对真实仓库零写入。

### 5. 遗留 / 下一步

- `PROJECT_STATE` 指针仍落后于 results：按设计只报告、不自动修复，由 GPT 决定；
- 「临时路径」允许根取自 `tempfile.gettempdir()`：测试用 monkeypatch 固定，避免依赖 basetemp 位置；
- 建议下一步：GPT 消费 v2 快照（`facts_digest` 可直接比对）决定队列补充与状态指针更新。

## 第八十八轮（2026-09-23）：GOLD-025 — GPT Review Ledger 与状态推进一致性门禁

### 1. 背景

- §2 状态机要求「COMPLETED 先 Review，Review PASS 才能更新 PROJECT_STATE 并创建下一任务」，
  但这条规则此前**无法机器验证**：Executor 只要写出 `status=completed` 的 result，
  就与「已被 GPT Review 通过」在状态上无法区分；`last_reviewed_task` 指针可以凭空
  超前 / 落后于 results；被 review 过的 result 之后被替换 / 改写时旧 review 会静默继续有效。
- 本轮只建立 review 契约与只读一致性门禁：**不**由 Cline / DeepSeek 签发任何 PASS review，
  不修改 `.ai/PROJECT_STATE.json`、不补队列、不改 Phase / 数据资格 Gate / L3-L4。

### 2. 交付内容

- `orchestrator/review_ledger.py`（新增，**纯只读**）：
  - 版本化契约：`REVIEW_LEDGER_SCHEMA=gold-ai/gpt-review-ledger/v1` + 整数
    `schema_version=1`，canonical 路径 `.ai/GPT_REVIEW_LEDGER.json`；
  - `validate_review_ledger()`：schema + 条目级 fail-closed 校验（verdict 词表、
    `reviewer_role=GPT`、`reviewed_result` 的 result sha256 / status / finished_at、
    `reviewed_commit` 的 40 位 commit sha + branch、`acceptance_summary`、`reviewed_at`），
    非法 / 不完整 / 冲突条目**永远不算通过**；
  - `build_review_report()`：`last_reviewed_task` 超前 / 落后（指针倒退）/ 缺失、
    completed-but-unreviewed、review identity mismatch（result 被替换）、PASS 落在 blocked、
    queue 首任务依赖（missing / blocked / not completed / unreviewed）、L3-L4 停线、
    可选 `--verify-ancestry`（只读 `git merge-base --is-ancestor`）→ 统一 `issues` +
    `gate.advance_allowed` 门禁结论；
  - `review_write_contract()` / `review_authority()`：只有 GPT 能签发 review，
    Cline / DeepSeek / 未知身份 fail-closed 拒绝；模块内**没有**任何写入 ledger 的 API；
  - CLI `python -m orchestrator.review_ledger`：只读 stdout（纯 ASCII JSON）+ stderr 摘要，
    退出码 `0` 一致 / `2` 漂移或阻塞 / `3` PROJECT_STATE 不可读。
- `tests/unit/test_ai_orchestrator_review_ledger.py`（新增 42 项）：合法 reviewed chain、
  completed-but-unreviewed、ledger 缺失 / 损坏 / schema 不支持 / 条目非法、
  result 被替换 / 非终态 / branch 不符、指针超前 / 倒退 / 缺失 / FAIL / unknown task、
  queue 依赖 missing / blocked / not completed / unreviewed（含传递依赖与覆盖下限）、
  L3-L4 永不跨越、ancestry 默认关闭与 fail-closed、只读性与确定性（工作树逐字节不变、
  facts_digest 可复算）、CLI 退出码与选项契约、源码守卫（无写入路径 / 无 planning API /
  唯一子进程为只读 git）。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.7「GPT Review Ledger 与状态推进一致性门禁（GOLD-025）」。


### 3. 测试结果

- `pytest tests/unit/test_ai_orchestrator_review_ledger.py -q` → `42 passed`；
- `pytest tests/unit/test_ai_orchestrator_queue.py -q` → 全绿；
- `pytest tests -q` → 全量通过；
- `ruff check .` → All checks passed；`mypy config database src scripts` → Success。

### 4. 范围守规

- 未触碰 `.ai/tasks/**`、`.ai/results/**`、`.ai/PROJECT_STATE.json`、`src/**`、`database/**`；
- 未改 Phase 3.3 blocker、数据资格 Gate、L3/L4 档位、`LIVE_TRADING=false`、
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未新增依赖 / 模型 / 供应商 API / 网络调用；Cline 未执行任何 Git 写操作；
- 写入测试全部发生在 `tmp_path`（含 fake `.git`），对真实仓库零写入。

### 5. 遗留 / 下一步

- `.ai/GPT_REVIEW_LEDGER.json` 目前**尚不存在**（本任务禁止 Executor 建档），
  因此 `python -m orchestrator.review_ledger` 当前按设计 fail-closed 报
  `REVIEW_LEDGER_MISSING` + 历史 completed 未 review + queue 依赖未 review，
  门禁 `advance_allowed=false`（只报告，不阻断 Orchestrator 运行）；
- 建议下一步：GPT 依据 §2.7 用真实 result sha256 与对应 commit sha 建档
  （可声明 `reviewed_from` 作为历史覆盖下限），此后门禁才可能转绿；建档属 GPT 职责，
  Cline / DeepSeek 永远只读。

## 第八十九轮（2026-09-23）：GOLD-026 — PHASE3_3_DATA 证据缺口只读 Readiness Diagnostic

### 1. 背景

- `PHASE3_3_DATA` 的真实缺口此前分散在 readiness（GOLD-006）/ handoff（GOLD-008）/
  inbox 预检（GOLD-011）/ `evidence-intake-v1` 契约与多个 CLI 里：GPT 与人工每轮都要手工重建
  "**还缺什么、必须由谁完成**"，且"代码已经就绪"与"真实证据仍缺"很容易被混读；
- 本轮只做**只读诊断**：不采集、不写库、不联网、不伪造、不放宽任何资格门槛；
  `PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

### 2. 交付内容

- `src/monitoring/evidence_gap_diagnostic.py`（新增，**只读**）：
  - **四态分类** `ReadinessClass`：`CODE_READY`（代码 / 契约已就绪）/ `EVIDENCE_MISSING`
    （真实证据缺失、未 ingest 或未达标）/ `HUMAN_VERIFICATION_REQUIRED`（证据已在，
    法律效力 / 出处 / 时间语义只能人工核验）/ `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate）；
  - `DiagnosticItem`：`key` / `category` / `scope` / `readiness` / `machine_fact` /
    `human_next_step` / `current` / `required` / `missing_fields` / `missing_count` /
    `references` / 证据时间窗，`mock_or_template_qualifies` **恒 false**；
  - `ManifestFact` / `ManifestDiagnostic`：复用 **GOLD-011** `scan_inbox` 的只读预检，
    给出**字段级**事实（声明 / 缺失字段、时间语义与 availability 声明、合成标记、原因码、行计数），
    `qualifies` 恒 false（候选 ≠ 授权已核验 ≠ 资格通过）；对 inbox 新增必填项做**契约漂移**
    fail-closed（`ValueError`，绝不静默忽略）；
  - `HumanStep` + `MANDATORY_HUMAN_STEPS`：固定的 6 步人工清单（提供授权证据 / 核验授权法律效力 /
    提供并核验独立可用证据 / 达到既有量化门槛 / 排除 Mock 与模板 / L3 人工 Gate），
    每步带 `verification`（`MACHINE_CONFIRMED` / `MACHINE_UNSATISFIED` / `HUMAN_ONLY`，未登记步骤
    一律按最保守的 `HUMAN_ONLY` 处理）且 `blocking` 恒 true；阈值文本由 `src.alpha.evidence_gate`
    常量格式化（**不另造数字**）；
  - `EvidenceGapDiagnostic`：顶层 `readiness` **恒 `GATE_BLOCKED`**，
    `blocker_active` / `human_gate_required` 恒 true，
    `data_qualification_passed` / `phase_transition_allowed` / `advance_allowed` /
    `l3_l4_auto_advance_allowed` 恒 false（**硬编码**，不被上游或被篡改输入透传）；
    25 个诊断项（4 契约 + 3 门禁 + Author 8 + News 10）+ 分类计数 + 不计资格证据 + manifest 段；
  - `build_gap_diagnostic`（纯函数、确定性排序）/ `build_manifest_diagnostic`（纯映射）/
    `load_gap_diagnostic`（只读台账 + 可选**本地** `--inbox-dir` 预检；缺目录 fail-closed）/
    `render_gap_diagnostic_markdown`；
  - **复用而不复制**：readiness（GOLD-006）+ handoff（GOLD-008）+ inbox（GOLD-011）+
    `evidence-intake-v1` 字段与原因码 + `src.alpha.evidence_gate` 阈值；
    **未新增、未降低**任何资格阈值，也未改动既有 evidence 模块与 `src/alpha/**`；
- `scripts/evidence_gap_diagnostic.py`（新增）：`--inbox-dir` / `--as-of` / `--json` /
  `--out`（**唯一**写开关，复用 `atomic_write_text` 原子写）；**没有**任何 intake / 写库 /
  `qualify` / `approve` / `advance` 参数；退出码 `0` 无实质证据缺口（gate 仍 BLOCKED）/
  `2` 参数或输入错误 / `4` `--out` 不可写 / `5` 仍有实质证据缺口（**预期** BLOCKED）；
- `src/monitoring/__init__.py`：新增 `evidence_gap_diagnostic` 惰性导出（21 个新公开名 +
  模块说明），`__all__` 与惰性表保持同源（既有门禁用例覆盖）。

### 3. 测试结果

- `pytest tests/unit/test_evidence_gap_diagnostic.py -q` → **25 passed**：
  空库 / 部分达标 / 门槛全达标三态分类；顶层恒 `GATE_BLOCKED` 与安全布尔全 false；
  25 项 key 的**确定性顺序**与分类合法性；"除 `as_of` 外无任何时间戳"正则断言；
  `NOT_OOS_ELIGIBLE` → `EVIDENCE_MISSING`；契约引用与阈值同源（`field_by_name` /
  `evidence_gate` 常量 / `MANIFEST_REQUIRED_FIELDS`）；重复构造 byte-stable；
  人工步骤 `blocking` 恒 true 与 `verification` 语义；不计资格证据恒 false；
  manifest 字段级事实 / 缺声明只报事实 / 合成候选永不 qualify / **mtime 绝不作为证据时间**；
  缺 `--inbox-dir` 与 naive `as_of` fail-closed；源码级守卫（只 import 既有模块、
  无 `open` / `os.stat` / `getmtime` / `utime` / 网络 / `intake_evidence` / `commit`）；
  Markdown 保留 blocker 且不回显证据正文；
- `pytest tests/integration/test_evidence_gap_diagnostic_integration.py -q` → **8 passed**：
  空库 → 退出 `5`、`advance_allowed=false`、**数据库零变化**；CLI 无
  `qualify` / `approve` / `advance` / `--no-dry-run` 参数（未知参数退出 `2`）；
  `--out` 是唯一写开关（默认零写入）；`--inbox-dir` 只读预检（`qualifies=false`、零 ingest）；
  缺目录 fail-closed（退出 `2`、stdout 为空）；`--out` 不可写 → 退出 `4`；
  **真实 intake 写入达标证据后仍 `GATE_BLOCKED`**（实质缺口归零、退出 `0`，但
  `data_qualification_passed` / `advance_allowed` 仍 false、仍需 L3 人工决策）；
- `pytest tests/unit/test_lazy_package_exports.py -q` → **15 passed**（包边界未漂移）；
- `ruff check .` → **All checks passed!**；`mypy config database src scripts` →
  **Success: no issues found in 174 source files**（GOLD-025 为 172，新增本模块 + 本 CLI）；
- 全量 `pytest tests -q` → **2948 passed / 1 skipped in 321.82s**（1 skipped 仍为
  `test_text_similarity.py` 的"已安装 jieba"分支）；新 CLI 已登记进
  `tests/integration/test_evidence_cli_smoke.py`，因此 `scripts/evidence_gap_diagnostic` 的
  fresh-subprocess import / `--help` / 空 cwd **零写入**门禁也包含在内；
- 真实端到端复核（临时 SQLite + 空 cwd，真实子进程）：`python -m scripts.evidence_gap_diagnostic
  --json --as-of 2026-09-23T00:00:00+00:00` → 退出码 `5`、cwd **零文件写入**、
  `advance_allowed=false` / `blocker_active=true`、
  `class_counts = {CODE_READY: 4, EVIDENCE_MISSING: 18, HUMAN_VERIFICATION_REQUIRED: 0,
  GATE_BLOCKED: 3}`、25 个诊断项 / 6 个人工步骤、报告的**唯一**时间戳就是 `as_of`。


### 4. 范围守规

- 只读：零网络（不抓取、不绕过 robots / 证书）、零数据库写入、零模型训练、零交易；
  默认只打印 stdout，唯一写开关是显式 `--out`（原子写）；
- 未新增 / 未升级任何依赖，未新增 migration / schema，未安装或修改任何 OS 计划任务；
- 未改 Phase 3.3 blocker / 数据资格阈值 / L3-L4 Gate / `LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；未触碰 `.ai/tasks/**`、`.ai/results/**`、
  `.ai/PROJECT_STATE.json`、`src/alpha/**`、`src/execution/**`；
- 写入测试全部发生在 `tmp_path`（临时 SQLite / 临时 manifest），对真实仓库零写入；
- Cline 未执行任何 Git 写操作（提交由 Orchestrator 负责）。

### 5. 遗留 / 下一步

- 诊断**不是**资格判定器：它只回答"还缺什么、必须由谁完成"；`PHASE3_3_DATA` 仍由**真实授权
  证据 + 人工 Gate** 决定，本工具不采集、不签发、不推进；
- manifest 事实只来自**显式** `--inbox-dir` 的本地目录（缺省不扫描）：未提供时合成标记与
  时间语义声明不可见（诊断会显式标注"未提供候选目录"）；
- 建议下一步：GPT / 人工按诊断输出的 `human_steps`（含每步 `verification` 状态）与
  `EVIDENCE_MISSING` 项清单补齐真实授权证据；补齐后重跑
  `scripts.evidence_gap_diagnostic --json` 复核，再走 `evidence_handoff` / 决策包与 **L3 人工 Gate**。


## 第九十轮（2026-09-23）：GOLD-027 — 人工证据 Intake Handoff 契约与只读预检

### 1. 背景

- GOLD-026 已经能"只读诊断还缺什么"，但**业务方仍不知道按什么契约准备材料**：
  Author / News 需要哪些来源身份、授权证明、原始证据引用、`published_at` / `collected_at` /
  `effective_at` / `available_at` / OOS 与独立时间语义要求，此前分散在
  `src/evidence/contracts.py`、`src/evidence/handoff.py` 的 checklist、GOLD-011 的 inbox manifest
  与多个 CLI 里，每轮都要人工重建"提交格式"；
- 本轮把缺口诊断转成**单一、版本化**的人工证据提交 / 预检 handoff：业务人员按同一契约准备材料，
  预检只回答"材料是否齐全、字段是否可解析、来源 / 时间声明是否带独立证据引用"，
  **绝不**回答"是否合格"；`PHASE3_3_DATA` **保持 BLOCKED**，L3 / L4 只能人工推进。

### 2. 交付内容

- `src/evidence/intake_handoff.py`（新增，**纯本地只读**）：
  - **版本化 schema** `intake_handoff_schema()`：`kind=phase33_evidence_intake_handoff` /
    `schema_version=1` / `contract_version=evidence-intake-v1` / 目录契约（沿用 GOLD-011 inbox：
    `manifest.json` 必填声明 + `files[path, sha256]`）/ Author + News 必需契约字段
    （`required_field_names`，Author 额外 `author_name` / `external_account_id`）/
    **10 条材料契约**（`evidence_records` / `source_identity` / `authorization_declaration` /
    `time_semantics` / `published_at` / `collected_at` / `effective_at` / `availability_oos` /
    `author_identity` / `phase33_data_gate`，`category` 与 GOLD-026 缺口分类**同一词表**）/
    **6 条独立时间语义要求** / 状态词表 + 释义 / `preflight_pass` 的诚实语义与
    `preflight_pass_does_not_imply` 列表；阈值**只引用** `src.alpha.evidence_gate`
    （经 `src.evidence.handoff.thresholds`），**不新增、不降低**任何资格门槛；
  - `MaterialSpec` + `MATERIAL_SPECS`：每条材料声明 `requirement` / `contract_fields` /
    `reference_fields`（独立证据引用）/ `declaration_fields`（manifest 声明）；
    `unknown_contract_fields()` 提供"只引用既有契约字段、不偷偷加列"的机器可检查断言（健康树为空）；
  - `IntakeStatus` **五态**（稳定字符串）：`MISSING`（缺失或机械校验未通过）/
    `PRESENT_UNVERIFIED`（已提交但独立证据引用 / 机械校验不足，不能进入人工核验队列）/
    `HUMAN_VERIFICATION_REQUIRED`（结构完整：法律效力 / 出处 / 时间语义只能人工核验）/
    `NON_QUALIFYING`（Mock / 模板 / 示例 / 合成：**永远**不计资格）/
    `GATE_BLOCKED`（`PHASE3_3_DATA` 与 L3 / L4 人工 Gate，Gate 材料恒为此态）；
  - `HandoffMaterial` / `PackageHandoff` / `IntakeHandoffDocument`：每个材料带
    `present_fields` / `missing_fields` / `machine_fact` / `human_next_step`，
    `counts_toward_eligibility` **恒 false**；`preflight_pass` **只**表示 package 结构完整
    （无 `MISSING` / 无 `PRESENT_UNVERIFIED` / 非合成 / GOLD-011 预检零原因码），
    并通过 `preflight_pass_meaning`（每包）/ schema 段 / notes 显式写明
    "**结构完整 ≠ evidence qualified ≠ 解除 PHASE3_3_DATA ≠ L3/L4 通过**"；
    `evidence_qualified` / `data_qualification_passed` / `phase_transition_allowed` /
    `advance_allowed` / `l3_l4_auto_advance_allowed` **恒 false**，
    `blocker_active` / `human_gate_required` / `gate_blocked` **恒 true**（**硬编码**）；
  - `build_intake_handoff`（纯函数）/ `load_intake_handoff`（只读复用 GOLD-011 `scan_inbox` +
    GOLD-026 `build_manifest_diagnostic`；缺目录 fail-closed）/ `render_intake_handoff_markdown`；
    `_check_spec_coverage()` 在运行期对"材料新增却忘记判定器"做 fail-closed；
  - **不伪造时间事实**：缺 `time_semantics` / 缺 `available_at` 时只报告字段名与人工下一步，
    `url` 等**可选**字段绝不误报为缺失；绝不使用当前时间 / 文件 mtime / 抓取时间 / 推断值；
- `scripts/evidence_intake_handoff.py`（新增）：`--schema`（打印版本化契约，不需要 `--inbox-dir`）/
  `--inbox-dir`（除 `--schema` 外必填）/ `--as-of` / `--json` / `--out`（**唯一**写开关，
  复用 `atomic_write_text` 原子写）；**没有**任何 intake / 写库 / `qualify` / `approve` /
  `advance` / `--no-dry-run` 参数；退出码 `0` 至少一个包结构完整（**不是**资格通过）/
  `2` 参数或输入错误 / `4` `--out` 不可写 / `5` 没有任何结构完整的包（预期 BLOCKED）；
- `src/evidence/__init__.py`：新增 `intake_handoff` 惰性导出（20 个新公开名），
  `__all__` 与惰性表保持同源（既有门禁用例覆盖）；
- `tests/unit/test_evidence_intake_handoff.py`（新增 32 项）+
  `tests/integration/test_evidence_intake_handoff_integration.py`（新增 13 项），并把新 CLI 登记进
  `tests/integration/test_evidence_cli_smoke.py`（现覆盖 14 个 `scripts/evidence_*.py`）；
- `README.md` / `TECH_DEBT.md`（新增 **TD-62**）/ `PROGRESS_LOG.md` 同步登记。


### 3. 测试结果

- `pytest tests/unit/test_evidence_intake_handoff.py -q` → **32 passed**：
  schema 版本化且只引用既有契约字段（`unknown_contract_fields() == ()`、`category ∈ GAP_CATEGORIES`、
  `field_by_name` 全部可解析、阈值同源）/ Author 与 News 材料范围差异 / 五态判定 /
  "结构完整 ≠ 资格通过"（`preflight_pass=true` 与 `evidence_qualified=false` 并存）/
  缺授权证明 / 缺独立时间语义 / 缺独立可用证据（`PRESENT_UNVERIFIED`）/
  行级授权证据缺失（`PRESENT_UNVERIFIED`）/ Mock 行与 Mock manifest 全部 `NON_QUALIFYING` /
  无 manifest fail-closed / 计数与查表助手 / "除 `as_of` 外无任何时间戳"且 mtime（2019-01-02）绝不出现 /
  naive `as_of` 拒绝 / 重复构造 byte-stable / 候选包内容与 mtime 零变化 /
  Markdown 保留 blocker 且不回显正文 / 源码级守卫（只 import 既有模块；无 `open` / `os.stat` /
  `getmtime` / `utime` / 网络 / `intake_evidence` / 直接 `write_text`；安全布尔硬编码）；
- `pytest tests/integration/test_evidence_intake_handoff_integration.py -q` → **13 passed**：
  `--help` 仅暴露只读开关（参数集合精确断言）/ 五类禁止参数退出 `2` / 缺 `--inbox-dir` 与
  naive `--as-of` fail-closed（stdout 为空）/ `--schema` 契约输出且默认零写入 /
  `--schema --out` 是唯一写开关 / 结构完整 → 退出 `0` 但全部安全布尔为 false /
  空目录与 Mock → 退出 `5` / `--out` 只写 artifact 且候选证据内容 + mtime 零变化 /
  `--out` 不可写 → 退出 `4` / Markdown 保留 blocker / **fresh subprocess** 端到端 cwd 零写入；
- `pytest tests/unit/test_lazy_package_exports.py -q` → **15 passed**（包边界未漂移）；
- 全量 `pytest tests -q` → **2996 passed / 1 skipped in 325.06s**（1 skipped 仍为
  `tests/unit/test_text_similarity.py` 的"本环境已安装 jieba"分支——与基线一致）；
  新 CLI 已登记进 `tests/integration/test_evidence_cli_smoke.py`，因此
  `scripts.evidence_intake_handoff` 的 fresh-subprocess import / `--help` / 空 cwd
  **零写入**门禁也包含在内；
- `ruff check .` → **All checks passed!**；`mypy config database src scripts` →
  **Success: no issues found in 176 source files**（GOLD-026 为 174，新增本模块 + 本 CLI）。

### 4. 范围守规

- 只读：零网络（不抓取、不绕过 robots / 证书）、零数据库访问（本 CLI 不建 engine、不 import
  `sqlalchemy`）、零模型训练、零交易；默认只打印 stdout，唯一写开关是显式 `--out`（原子写）；
- 未新增 / 未升级任何依赖，未新增 migration / schema，未修改 `.ai/**`、`src/alpha/**`、
  `src/execution/**`、`config/rss_sources.json`、`docs/**`；
- 未改 Phase 3.3 blocker / 数据资格阈值 / L3-L4 Gate / `LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 写入测试全部发生在 `tmp_path`（临时候选包 / 临时输出），对真实仓库零写入；
- Cline 未执行任何 Git 写操作（提交由 Orchestrator 负责）。

### 5. 遗留 / 下一步

- 预检**不是**资格判定器：`preflight_pass` 只表示"材料齐全、字段可解析、声明带独立引用"，
  授权法律效力、许可范围、签认人身份与独立可用时间出处仍**只能人工核验**（TD-62）；
- 材料级判定只消费既有 inbox 预检的字段级事实：行级具体原因码未透传，因此"存在被隔离行"
  统一按 `PRESENT_UNVERIFIED` 报告（fail-closed）；逐行原因码仍看 `evidence_inbox` /
  `evidence_gap_diagnostic` 的产物；
- 建议下一步：业务方用 `scripts.evidence_intake_handoff --schema` 作为**单一契约**准备真实授权
  Author / News 材料 → 放入显式 `--inbox-dir` → 跑 `--json` 预检修正缺失项 →
  再走 `evidence_inbox` / `evidence_review` / `evidence_intake_plan` /
  `evidence_operator workflow --no-dry-run` / `evidence_gap_diagnostic` 与 **L3 人工 Gate**；
  `PHASE3_3_DATA` 仍 BLOCKED。


## 第九十一轮（2026-09-23）：GOLD-028 — 材料级 Human Verification Attestation 与证据绑定审计

### 1. 背景

- GOLD-027 已能回答"材料是否齐全、字段是否可解析、来源 / 时间声明是否带独立证据引用"，
  但对"**每一项关键材料到底有没有被人工核验、由谁核验、依据哪条独立引用、结论是什么**"
  没有任何**可审计凭证**：`HUMAN_VERIFICATION_REQUIRED` 只是"可以进入人工核验队列"，
  核验结果只存在于人的脑子里或聊天记录里，无法被机器复核、无法检测 package / manifest 漂移、
  也无法防止事后静默改写；
- 本轮把结构完整预检进一步转换为**材料级人工核验凭证**（Human Verification Attestation）：
  受控决策 + 稳定 reason code + 显式 reviewer label + 带时区 `reviewed_at` + 独立引用，
  **硬绑定**当前候选 package fingerprint + GOLD-027 handoff / manifest 内容身份 + scope，
  并提供**纯只读防伪核验**（检测 stale / tampered / missing reference）；
  `all_required_verified` **只**代表材料级人工核验完成，`PHASE3_3_DATA` **保持 BLOCKED**，
  L3 / L4 只能人工推进。

### 2. 交付内容

- `src/evidence/human_verification_attestation.py`（新增，**纯本地只读 + 显式人工输入**）：
  - **版本化契约** `attestation_schema()`：`kind=phase33_human_verification_attestation` /
    `schema_version=1` / `contract_version=human-verification-attestation-v1` / 必核验材料清单
    （**只引用** GOLD-027 `MATERIAL_SPECS` 的非 Gate 材料：`evidence_records` /
    `source_identity` / `authorization_declaration` / `time_semantics` / `published_at` /
    `collected_at` / `effective_at` / `availability_oos` + Author 专有 `author_identity`）/
    受控决策词表 / 稳定原因码 / 核验输入文件格式 / `all_required_verified` 的诚实语义与
    `all_required_verified_does_not_imply` 列表；`_check_material_coverage()` 在运行期
    fail-closed 校验"至少覆盖 authorization / source / 独立时间语义 / availability-OOS"，
    **不新增、不降低**任何资格门槛；
  - `VerificationDecision`（`VERIFIED` / `REJECTED` / `NEEDS_CHANGES`）+ `MaterialDecision` +
    `VerificationInput` + `load_verification_input()`：人工核验输入**必须**显式声明
    `package_fingerprint`（+ 可选 handoff 内容身份 / scope），逐材料校验受控决策 / 稳定
    `reason_code` / 显式 reviewer label / 带时区 `reviewed_at`（naive 或晚于审计时点一律拒绝）/
    独立 `evidence_reference`（只接受 `https://...` 或 `docs/legal/...`，拒绝裸文件名）；
    未知字段 / 未知材料 / 重复材料 / Gate 材料 / 疑似凭据（token / key / blob 形态）一律 fail-closed；
  - `MaterialAttestation` / `HumanVerificationAttestation`：每材料给出 `intake_status` /
    `decision` / `reason_code` / `reviewer` / `reviewed_at` / `evidence_reference` /
    `counts_as_verified` / 稳定原因码；凭证级别给出 `required_material_count` /
    `verified_material_count` / `rejected_material_count` / `needs_changes_material_count` /
    `missing_decision_material_count` 与内容寻址 `attestation_id`（由「硬编码策略块 +
    `revision` / `supersedes` + package 绑定 + 逐材料核验」派生，**不含**审计时间 → 同输入
    byte-stable）；安全字段**硬编码**：`evidence_qualified` / `data_qualification_passed` /
    `phase_transition_allowed` / `advance_allowed` / `l3_l4_auto_advance_allowed` 恒 false、
    `blocker_active` / `human_gate_required` / `gate_blocked` 恒 true；
  - `handoff_content_sha256()` / `package_content_sha256()`：GOLD-027 handoff / manifest 与
    候选包材料结构的**内容身份**（不含 `as_of`，可复现）；漂移检测的唯一口径；
  - `build_attestation()`（纯函数）：核验输入的 fingerprint / handoff 内容身份 / scope 与
    **当前**候选目录不一致 → `PACKAGE_FINGERPRINT_MISMATCH` / `HANDOFF_CONTENT_MISMATCH` /
    `SCOPE_MISMATCH`；对 Mock / 模板 / 示例 / 合成声明 `VERIFIED` → `NON_QUALIFYING_PACKAGE`；
    对 `preflight_pass=false` 的包声明 `VERIFIED` → `PREFLIGHT_NOT_PASSED`；对结构上不是
    `HUMAN_VERIFICATION_REQUIRED` 的材料声明 `VERIFIED` → `MATERIAL_NOT_VERIFIABLE`；
    缺材料决策 → `MATERIAL_DECISION_MISSING` 且 `all_required_verified=false`；
  - `run_attestation()`：默认**零写入**；只有显式 `out_path` 才先做既有凭证冲突检查
    （同 `attestation_id` 幂等；内容不同必须显式 `supersedes` + 更大 `revision`，否则
    `ATTESTATION_CONFLICT` / `SUPERSEDES_MISMATCH` / `REVISION_INVALID`）再**原子**落盘；
    拒绝把凭证写进 `inbox_dir`（否则改变候选集）或覆盖核验输入文件；
  - `verify_attestation()`（**纯只读防伪核验**）：重新推导 `attestation_id`、逐项复核
    kind / schema / 契约 / 策略块 / 安全字段 / 计数与合取自洽 / 无证据时间键 / 独立引用非空，
    任一被改写 → `ATTESTATION_TAMPERED` / `ATTESTATION_ID_MISMATCH`；重新绑定**当前**候选目录并
    检测 `PACKAGE_FINGERPRINT_DRIFT` / `PACKAGE_CONTENT_DRIFT` / `HANDOFF_CONTENT_DRIFT` /
    `PREFLIGHT_DRIFT` / `MATERIAL_NOT_VERIFIABLE` / `MATERIAL_REFERENCE_MISSING`；
  - `render_attestation_summary()`（脱敏 Markdown，不解除 blocker）+ `exit_code_for()` /
    `main_verification_exit_code()` 稳定退出码映射；
- `scripts/evidence_human_verification_attestation.py`（新增）：`--schema` 打印契约 /
  `--inbox-dir` / `--package`（目录名或 fingerprint；候选唯一时可省略）/ `--verification` /
  `--as-of` / `--revision` / `--supersedes` / `--json` / `--out`（**唯一**写开关，复用
  `atomic_write_text`）/ `--verify-attestation`（纯只读重新绑定核验模式，与写参数互斥）；
  **没有**任何 intake / 写库 / `qualify` / `approve` / `advance` 参数；退出码
  `0` 全部必核验材料已 `VERIFIED`（**不是**资格通过）/ `2` 参数或输入错误 /
  `3` 路径或输出不可用 / `4` 漂移 / 篡改 / 冲突 / 非法人工输入（fail-closed，零写入）/
  `5` 未全部核验或不可核验（**当前预期**：`PHASE3_3_DATA` 保持 BLOCKED）；
- `src/evidence/__init__.py`：新增 `human_verification_attestation` 惰性导出（34 个名字），
  包 docstring 与 CLI 入口清单同步更新（`__all__` 与惰性表同源门禁仍绿）；
- `tests/integration/test_evidence_cli_smoke.py`：登记新 CLI（现覆盖 15 个
  `scripts/evidence_*.py` + 单步 intake 入口）；
- 文档：`README.md` 新增 GOLD-028 小节（用法 / 语义 / 退出码 / 回归测试）；`TECH_DEBT.md`
  登记 **TD-63** 与变更日志行。


### 3. 测试结果

- `pytest tests/unit/test_evidence_human_verification_attestation.py -q` → **41 passed**：
  版本化契约（只引用 GOLD-027 材料契约 / 必核验类别覆盖 / 决策词表 / 诚实语义）、
  正向（作者与新闻 scope、内容寻址 `attestation_id` 与审计时点无关、byte-stable）、
  缺授权证明 / 缺独立时间语义 / 缺 availability-OOS 一律 fail-closed、
  Mock / 模板永不 qualify、REJECTED / NEEDS_CHANGES 绝不计入、
  fingerprint / handoff 内容 / scope 漂移被拒、写入后内容漂移使旧凭证失效、
  篡改（决策 / 安全字段 / 伪造 `all_required_verified` / 缺引用）全部 fail-closed、
  重复幂等 + 显式 revision / supersedes、敏感值 / 未知字段 / 重复材料 / Gate 材料拒绝、
  naive 或未来 `reviewed_at` 拒绝、naive `--as-of` 拒绝、`--out` 不得进 inbox 或覆盖核验输入、
  候选证据内容与 mtime 零变化、除审计时点与人工 `reviewed_at` 外无任何时间戳且 mtime 绝不出现、
  源码级守卫（只 import 既有模块；无 `open` / `os.stat` / `getmtime` / `utime` / 网络 /
  `intake_evidence` / 直接 `write_text`；安全布尔硬编码）；
- `pytest tests/integration/test_evidence_human_verification_attestation_integration.py -q`
  → **18 passed**：`--help` 仅暴露只读开关（五类禁止参数退出 `2`）、`--schema` 输出契约且默认
  零写入、缺 `--inbox-dir` / `--verification` 退出 `2`、naive `--as-of` fail-closed（stdout 空）、
  全部核验 → 退出 `0` 且安全布尔全为 false / true、默认零写入、`--out` 只写 artifact、
  `--out` 不可写退出 `3`、未全部核验与 Mock → 退出 `5`、空候选 → `PACKAGE_NOT_FOUND`、
  防伪核验通过 / 漂移 / 篡改 → `0` / `4` / `4`、`--verify-attestation` 与写参数互斥、
  **fresh subprocess** 端到端 cwd 零写入；
- `pytest tests/unit/test_lazy_package_exports.py -q` → **15 passed**（`__all__` 与惰性表同源，
  34 个新名字全部 `is` 定义子模块对象）；
- `pytest tests/integration/test_evidence_cli_smoke.py -q` → 新 CLI 的 fresh-subprocess
  import / `--help` / 空 cwd **零写入**门禁全部通过（覆盖 15 个 `scripts/evidence_*.py`）；
- 全量 `pytest tests -q` → **3060 passed, 1 skipped in 395.65s**（`1 skipped` 仍为
  `tests/unit/test_text_similarity.py` 的「本环境已安装 jieba，跳过未安装分支」——
  与基线一致；新 CLI 的 fresh-subprocess import / `--help` / 空 cwd **零写入**门禁已含在内）；
- `ruff check .` → **All checks passed!**；`mypy config database src scripts` →
  **Success: no issues found in 178 source files**（GOLD-027 为 176，新增本模块 + 本 CLI）。

### 4. 范围守规

- 只读：零网络（不抓取、不绕过 robots / 证书）、零数据库访问（模块与 CLI 不建 engine、
  不 import `sqlalchemy`）、零模型训练、零交易；默认只打印 stdout，唯一写开关是显式 `--out`
  （原子写），且拒绝写进候选目录或覆盖人工核验输入；
- 未新增 / 未升级任何依赖，未新增 migration / schema，未修改 `.ai/**`（任务 / 结果 /
  `PROJECT_STATE`）、`src/alpha/**`、`src/execution/**`、`database/**`、
  `config/rss_sources.json`、`docs/**`、`.env`；
- 未改 Phase 3.3 blocker / 数据资格阈值 / L3-L4 Gate / `LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；`PHASE3_3_DATA` 仍 BLOCKED；
- 写入测试全部发生在 `tmp_path`（临时候选包 / 临时凭证），对真实仓库零写入；
- Cline 未执行任何 Git 写操作（提交由 Orchestrator 负责）。

### 5. 遗留 / 下一步

- 凭证**不是**资格判定器：`all_required_verified` 只表示材料级人工核验完成，
  授权法律效力 / 许可范围 / 签认人身份与独立可用时间出处的判定仍属人工与 L3（TD-63）；
- 材料级判定仍只消费既有 inbox 预检的字段级事实：行级具体原因码未透传，
  "存在被隔离行"统一按 `PRESENT_UNVERIFIED` 报告（fail-closed）；
- 未实现单实例锁（与 GOLD-016 不同）：并发写入依赖原子写 + 既有凭证冲突检查，
  如需强并发保证可在后续任务引入（TD-63）；
- 建议下一步：业务方以 `scripts.evidence_intake_handoff --schema` 准备真实授权 Author / News
  材料 → 放入显式 `--inbox-dir` → 按 `--schema` 写出人工核验输入 →
  `scripts.evidence_human_verification_attestation --inbox-dir ... --verification ... --out ...`
  生成材料级核验凭证 → 再用 `--verify-attestation` 复核（漂移 / 篡改 / 缺引用）→
  最后走 `evidence_inbox` / `evidence_review` / `evidence_intake_plan` /
  `evidence_operator workflow --no-dry-run` / `evidence_gap_diagnostic` 与 **L3 人工 Gate**；
  `PHASE3_3_DATA` 仍 BLOCKED。


---

## 第九十二轮（2026-09-23）：GOLD-029 — 把 Human Verification Attestation 接入 Evidence APPROVE 门禁

### 1. 背景 / 问题

GOLD-028 交付了**材料级人工核验凭证**（`phase33_human_verification_attestation`），但 GOLD-012 的
`APPROVE` 仍然只凭"人工填一个受控 reason code"就能把候选包放进 `approved-for-explicit-intake`：
**凭证与批准之间没有强制绑定**，一份陈旧 / 漂移 / 被篡改 / 只核验了一半的凭证也照样能"批准"。

### 2. 变更（最小范围）

- `src/evidence/review.py`：
  - 新增 `build_attestation_binding()`：`APPROVE` 记录前**必须**显式给出 GOLD-028 凭证文件，并用
    `load_attestation_document()` + `verify_attestation()` 与**当前**候选目录逐项重新绑定核验
    （`package fingerprint` / `package 内容身份` / `handoff 内容身份` / `scope` /
    `preflight_pass` / `all_required_verified`）；缺失 / 陈旧 / 漂移 / 被篡改 / 部分核验一律
    `ReviewAttestationError`（继承 `ReviewTargetError` → 退出码 `4`）且**零写入**；
  - `ReviewRecord` 新增**最小** `attestation` binding（`ATTESTATION_BINDING_KEYS`：binding 版本 /
    GOLD-028 schema + contract 版本 / `attestation_id` / 凭证文档 `sha256` / package + handoff
    内容身份 / `scope` / `all_required_verified`）；**绝不**保存凭证正文、材料备注、`reviewer`
    或任何证据时间；
  - `REJECT` / `NEEDS_CHANGES` **不**需要凭证（负向决策审计不被阻塞）；
  - 旧 ledger（无 binding 的 `APPROVE`）**保持可读**、`decision_id` 派生口径不变（`compute_decision_id`
    未改）、**绝不原地迁移 / 重写**；新门禁只对后续 / 当前重新验证生效；
  - `build_approved_intake_list()` 新增 `_attestation_binding_invalidation()`：用**当前** inbox /
    handoff 重新推导绑定身份，缺失 binding → `ATTESTATION_MISSING`、未完整核验 →
    `ATTESTATION_INCOMPLETE`、指纹与记录不一致 → `ATTESTATION_TAMPERED`、
    候选包内容身份 / `scope` 漂移 → `ATTESTATION_STALE`（稳定原因码，批准失效并进 `invalidated`）；
  - `_sanitize_attestation_binding()`：ledger 读入时逐字段白名单校验（未授权键 / 类型 / 摘要 /
    scope / 非 `APPROVE` 携带 binding 一律 `ReviewLedgerStateError`）。
- `scripts/evidence_review.py`：新增 `--attestation <凭证文件>`（`approve` 必填；其它决策不需要），
  并在 `--help` / 文档串中说明门禁与退出码。
- `src/evidence/intake_plan.py`：`PlanVerificationCode` 与 `_INVALIDATION_CODES` 显式映射四个
  GOLD-029 失效原因码，使**后续 intake plan** 链在 binding 缺失 / 漂移时给出稳定原因码
  （计划仍**只读**、**不自动 intake**）。
- `src/evidence/__init__.py`：惰性导出新增 `ATTESTATION_BINDING_KEYS` / `ATTESTATION_BINDING_VERSION` /
  `ReviewAttestationError`（与 `__all__` 同源）。

### 3. 验证

- 新增单元回归：`tests/unit/test_evidence_review.py` 覆盖「无凭证的 APPROVE → `ATTESTATION_MISSING`
  且零写入 + 原始 evidence 零变化」「部分核验 → `ATTESTATION_INCOMPLETE`」「绑错 package /
  package 漂移 → `ATTESTATION_STALE`」「凭证被改写 → `ATTESTATION_TAMPERED`」「合法绑定 APPROVE
  只保存**最小** binding（无材料 / 无证据时间）」「负向决策无需凭证」「历史 ledger 可读但不满足
  新门禁且文件零改写」「批准清单重新验证对 binding 漂移失效」「ledger binding 被改写 / 削弱 /
  非 APPROVE 携带 → fail-closed」；
- 新增集成回归：`tests/integration/test_evidence_review_integration.py` 覆盖 CLI
  「缺 `--attestation` → 退出码 `4`、stdout 为空、零写入」「凭证被篡改 → 退出码 `4`」
  「`reject` 无凭证仍成功落盘」；`tests/unit/test_evidence_intake_plan.py` 覆盖后续 plan 链
  的 `ATTESTATION_MISSING` / `ATTESTATION_STALE` 稳定原因码；
- 全量回归：`pytest tests` 分批执行，`unit` 与 `integration` 全部通过（既有 GOLD-012 ~ GOLD-028
  测试在补齐合法凭证后保持一致语义）；`ruff check .` → **All checks passed!**；
  `mypy config database src scripts` → **Success: no issues found in 178 source files**。

### 4. 范围守规

- 零网络 / 零数据库 / 零模型训练 / 零交易；不新增 / 不升级依赖；未改 Phase 3.3 blocker、
  数据资格阈值、L3-L4 Gate、`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
  `PHASE3_3_DATA` 仍 BLOCKED；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`、
  `src/alpha/**`、`src/execution/**`、`database/**`、`docs/**`、`.env`、`config/rss_sources.json`；
- 写入测试全部发生在 `tmp_path`；Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- **手写 ledger 的伪造风险**：binding 是"批准时的内容摘要"，ledger 若被拥有写权限的人直接改成
  另一份**结构合法**的 binding（伪造 `attestation_id` / 摘要），`build_approved_intake_list()`
  只能按结构白名单接受（无法在没有凭证文件时重新推导）；已在 TECH_DEBT 记录，建议后续给
  ledger 增加基于外部密钥 / 追加式签名日志的完整性保护；
- `handoff_content_sha256`（整个 handoff 快照身份）只在 **APPROVE 门禁**（凭证文件在场）复核；
  批准清单 / plan 复核以**被批准候选包自身**的材料结构内容身份为准（新增无关候选包不会静默
  作废既有批准，也不放过被批准包的漂移）；
- 建议下一步：GOLD-029 之后的资格链仍须以 GOLD-028 凭证 + 现有人工 Gate 为准，
  `evidence_review --decision approve --attestation <凭证>` → `--approved-out` →
  `evidence_intake_plan` → **人工显式** `evidence_operator workflow --no-dry-run`；
  `PHASE3_3_DATA` 仍 BLOCKED。


## 第九十三轮（2026-09-23）：GOLD-030 — Phase 3.3 Evidence Chain 端到端防漂移验收

### 1. 背景 / 问题

GOLD-027 ~ GOLD-029 各自都有严格的**只读**核验入口，但**没有任何一个**入口把整条链串起来：
业务方在提交真实授权证据前，只能人工逐个跑 5 ~ 6 个 CLI，且"某一段 artifact 与上游内容
身份是否仍然一致"（凭证绑定的 package、批准引用的 review revision、plan / receipt / packet /
record 之间的绑定）**没有被一次性验证**。真实证据到来时才暴露跨模块契约漂移，代价极高。

### 2. 变更（最小范围）

- `src/evidence/chain_audit.py`（新增，只读审计层）：
  - 固定**八段**顺序（`ChainStage` / `CHAIN_STAGE_ORDER`）：`intake_handoff` →
    `attestation` → `review_approval` → `approved_list_and_plan` → `operator_result` →
    `intake_receipt` → `decision_packet` → `decision_record`；任一段未通过即**短路**，其后为
    `NOT_EVALUATED`，`earliest_failure_stage` **明确指向最早失败阶段**；
  - **逐段复用既有核验**（不复制资格规则）：`load_intake_handoff` / `verify_attestation` /
    `build_approved_intake_list` + GOLD-029 binding 勾稽 / `build_intake_plan` /
    `verify_intake_receipt`（含 receipt_id 重算）/ `verify_decision_packet` +
    GOLD-016 `load_decision_packet` / `verify_decision_record`；
  - **稳定原因码**：`ChainStageStatus`（PASS / FAIL / MISSING / NOT_EVALUATED）+
    `ChainAuditCode`（`CHAIN_ARTIFACT_MISSING` / `CHAIN_ARTIFACT_TAMPERED` /
    `CHAIN_BINDING_MISMATCH` / `CHAIN_SCHEMA_DRIFT` / `CHAIN_INVALID_CANDIDATE` /
    `CHAIN_NO_APPROVED_EVIDENCE` …）+ 直接沿用上游模块原因码（`ATTESTATION_*` /
    `PLAN_*` / `OPERATOR_*` / `RECHECK_*` / `RECEIPT_ID_MISMATCH` / `RECORD_ID_MISMATCH` …）；
  - **内容身份**：`compute_chain_facts_digest()`（各段身份事实，**不含路径与时间**）+
    `compute_chain_id()`（策略块 + 段 / 状态 / 原因码 + facts_digest）；重复审计同一份 artifact
    得到同一身份，任一漂移必然改变；
  - **四个独立结论 + 硬编码安全语义**：`engineering_chain_ready` / `real_evidence_missing` /
    `human_verification_missing` / `l3_human_gate_pending`；`data_qualification_passed` /
    `phase_transition_allowed` 恒 false、`blocker_active` / `human_gate_required` /
    `gate_blocked` 恒 true，`evidence_source=rehearsal_fixture` 时前三个"缺失 / 待人工"结论
    恒为 true；
  - `run_chain_audit()`：默认零写入，只有显式 `out_path` 才（先取单实例锁）**原子**写审计报告
    本身，并拒绝写进 inbox。
- `src/evidence/rehearsal.py`（新增，纯本地演练 fixture）：
  - `build_rehearsal_chain(work_dir, moment, scenario=ready|blocked)` **原样调用**既有模块
    （`run_attestation` / `run_review` / `run_intake_plan` / `run_intake_receipt` /
    `run_decision_packet` / `run_decision_record`）搭起整条链；
    人工"显式执行结果"是**显式标注**的 Mock manifest（`fixture=true`，**未真实落库**、
    **未建数据库连接**、**未联网**）；GOLD-008 handoff 与 qualification recheck 是**显式
    fixture** 产物（算术自洽、阈值只引用 `src.alpha.evidence_gate`）；
  - `ensure_rehearsal_work_dir()`：仓库内**仅允许** `logs/` 之下，其余仓库路径一律拒绝
    （防止 fixture 污染仓库、被误当真实证据）。
- `scripts/evidence_chain_audit.py`（新增 CLI）：`--rehearsal --work-dir <dir>
  [--scenario ready|blocked]` 单一 rehearsal 命令；或 `--inbox-dir … --handoff … --attestation …
  --ledger … --approved-list … --plan … --operator-result … --recheck … --readiness … --receipt …
  --packet … --record …` 只读审计既有 artifact；`--out` 是唯一写开关；`--help` **不含**任何
  intake / `--no-dry-run` / qualify / advance / 交易参数；退出码 `0/2/3/4/5/6` 与既有 CLIs 同口径。
- `src/evidence/__init__.py`：惰性导出新增 `chain_audit` / `rehearsal` 两个子模块的公开名
  （`__all__` 与 `_LAZY_EXPORTS` 同源；共享的 `EXIT_*` 仍由既有模块提供，不重复登记）。

### 3. 验证

- 新增单元回归 `tests/unit/test_evidence_chain_audit.py`（**44 项**）：ready / blocked happy path
  8 段全 PASS；`chain_id` / `facts_digest` 确定性与漂移必变；**每一关键绑定点的 tamper / stale /
  missing**（候选包内容漂移 → `CHAIN_INVALID_CANDIDATE`；候选移除 / 凭证改写 → `ATTESTATION_*`；
  ledger 缺 binding → `ATTESTATION_MISSING`；ledger 绑定他证 → `ATTESTATION_TAMPERED`；
  批准清单改写 / plan_id 改写 → `APPROVED_LIST_*` / `PLAN_STALE`；dry-run → `OPERATOR_NOT_EXECUTED`；
  recheck 改写 → `RECEIPT_ID_MISMATCH`；readiness 改写 → `READINESS_TAMPERED`；
  packet 安全字段削弱；record reviewer / packet 内容漂移 → `RECORD_ID_MISMATCH`）且断言
  **此前全 PASS、其后全 `NOT_EVALUATED`**；工程链全绿仍 `data_qualification_passed=false` /
  `blocker_active=true`；审计零改写 + `--out` 只写报告 + 写进 inbox 被拒 + 锁冲突零写入；
  演练工作目录拒绝仓库路径；源码级守卫（审计 / 演练模块不 import 数据库 / 网络 / 子进程）；
- 新增集成回归 `tests/integration/test_evidence_chain_audit_integration.py`（**16 项**）：
  CLI 只读审计（纯 JSON、artifact 零改写）、缺 artifact / 收据篡改 → 退出码 `5` + 最早失败阶段、
  `--out` 原子写 + 锁冲突退出码 `6` 零写入、参数互斥、演练工作目录拒绝仓库路径（退出码 `3`）、
  演练 ready / blocked 模式只在显式工作目录内落盘、**fresh subprocess**（干净 cwd）演练与审计
  **零副作用**、`--help` 冒烟；`tests/integration/test_evidence_cli_smoke.py` 同步登记新入口；
- 全量回归：`pytest tests`（unit + integration）**3142 passed / 1 skipped**；
  `ruff check .` → **All checks passed!**；
  `mypy config database src scripts` → **Success: no issues found in 181 source files**。

### 4. 范围守规

- 零网络 / 零数据库 / 零模型训练 / 零交易；不新增 / 不升级依赖；未改 Phase 3.3 blocker、
  数据资格阈值、L3-L4 Gate、`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
  `PHASE3_3_DATA` 仍 BLOCKED；演练**绝不**真实落库、**绝不**产生真实 L3 批准；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`、
  `src/alpha/**`、`src/execution/**`、`database/**`、`docs/**`、`.env`、`config/rss_sources.json`；
- 写入测试全部发生在 `tmp_path` / 显式工作目录；Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- 本层只做"**内容身份是否前后一致**"的绑定核对：它**不能**证明授权材料真实存在或法律有效，
  也不判断人工核验者身份——这些仍归 GOLD-028 材料级核验与 **L3 人工 Gate**；`--audit` 模式下
  `real_evidence_missing=false` 只表示"工程审计未发现结构性缺口"，README 已显式写明该语义；
- 演练 fixture 的 GOLD-008 handoff / recheck 是**人工构造**的算术自洽产物（用于工程链演练），
  不代表真实量化结论；后续若 GOLD-008 / recheck 契约演进，需要同步更新 fixture 并由本层回归
  测试立即暴露（当前已由 8 段断言钉住）；
- 建议下一步：把本层接到定时/交接流程（例如每次提交真实证据前跑一次 single rehearsal；
  生成真实 artifact 后跑 `--audit`），并把 `earliest_failure_stage` + `reason_codes` 作为
  交接单的必填字段。


---

## GOLD-031：确定性 GPT Review Binding Manifest

### 1. 背景 / 问题

- `.ai/DEVELOPMENT_PROTOCOL.md` §2.7 要求 GPT 在 Review 台账里手工填写
  `reviewed_result.result_sha256` 与 `reviewed_commit.sha`，但当时没有任何**只读**工具
  能确定性地产出这两个内容身份：
  - 人工 `sha256sum` 会受 `core.autocrlf` 影响（Windows 工作树是 CRLF，GitHub / Git 存储
    是 LF），口径不一致就会让台账绑定到一个**无法被外部复算**的摘要；
  - 「找完成 commit」「确认工作树版本就是被 review 的版本」全靠肉眼；
  - 若把这件事交给 Executor，就等于把 Review 证据的生成权交给了执行方。
- GOLD-028 在 `PROJECT_STATE.blockers` 里也明确记录了「cryptographic Review Ledger binding
  不是编造出来的」这一 blocker 语义，本任务提供可复算的事实来源以便 GPT 后续安全绑定。

### 2. 变更（最小范围）

- `orchestrator/review_binding.py`（新增，纯只读）：
  - CLI `python -m orchestrator.review_binding --task <task_id>`（stdout 纯 ASCII JSON，
    stderr 只有人类摘要）；契约 `schema=gold-ai/review-binding-manifest/v1` +
    `schema_version=1`，字段顺序固定（`MANIFEST_FIELD_ORDER`），
    `facts_digest` 只覆盖确定性事实（排除 `generated_at` / `facts_digest` / `determinism`）；
  - **内容身份双口径**：`task` / `result` 的 `sha256` + `bytes` 是 **canonical 口径**
    （目标 commit 里 Git 存储 / GitHub 提供的字节，与 ledger 的
    `reviewed_result.result_sha256` 同口径），`worktree_sha256` + `worktree_bytes` 是本地
    工作树原始字节口径，`worktree_matches_commit` 以 **Git blob id** 口径判定漂移
    （换行转换不算漂移）；
  - `commit` 段只从 **HEAD 可达历史**里按 `ai: complete <task_id>` / `ai: blocked <task_id>`
    （与 `ai_orchestrator.commit_task_result` 同源）解析终态 commit identity
    （`sha` / `branch` / `head` / `subject` / `committed_at`）；
  - `validation` 段只汇总 result 里**已记录**的 validation（命令 / 返回码 / 超时），
    **不重新执行**任何测试；
  - **fail-closed** 稳定原因码：`TASK_ID_INVALID` / `TASK_FILE_MISSING` /
    `TASK_FILE_UNREADABLE` / `TASK_FILE_TASK_ID_MISMATCH` / `RESULT_MISSING` /
    `RESULT_UNREADABLE` / `RESULT_TASK_ID_MISMATCH` / `RESULT_STATUS_UNKNOWN` /
    `RESULT_NOT_TERMINAL` / `GIT_INFO_UNAVAILABLE` / `GIT_LOG_UNAVAILABLE` /
    `COMPLETION_COMMIT_NOT_FOUND` / `COMPLETION_COMMIT_AMBIGUOUS` /
    `COMPLETION_COMMIT_SHA_INVALID` / `RESULT_NOT_IN_COMMIT` / `TASK_NOT_IN_COMMIT` /
    `WORKTREE_COMMIT_MISMATCH`（多终态 commit 歧义**绝不猜测**）；
  - **GPT-only 边界**：输出里没有 `verdict` / `acceptance_summary` / `reviewed_at` /
    `reviewer`；`authority` 段硬编码 `review_authority=gpt_only` /
    `tool_can_sign_review=false` / `tool_can_advance_state=false` /
    `tool_can_qualify_data=false` / `tool_can_cross_human_gate=false` / `writes_*=false`；
  - 唯一外部进程调用是**只读** git 白名单（`log` / `ls-tree` / `cat-file` / `hash-object`），
    白名单外子命令在代码级直接拒绝；零网络 / 零数据库 / 零业务证据写入 / 零模型调用。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.8 记录 manifest 契约、口径、fail-closed 码、
  GPT-only 边界与只读保证（§2.7 ledger 语义不变）。
- 未新增依赖、未新增 migration / schema、未改 `pyproject.toml`、未改
  `.ai/tasks` / `.ai/results` / `PROJECT_STATE` / review 台账。

### 3. 验证

- 新增单元回归 `tests/unit/test_ai_orchestrator_review_binding.py`（**73 项**）：
  确定性 / 只读（两次构建字节相同、树摘要不变）、字段顺序契约、wall-clock 与 mtime 不进
  `facts_digest`、canonical 与 worktree 两套 SHA-256 可由独立 `hashlib` 复算、工作树漂移
  fail-closed、validation 摘要（passed / failed / not_reported）、全部 fail-closed 分支
  （invalid task_id / 缺失 / 损坏 / task_id 不一致 / 未知 status / 非终态 / Git 不可解析 /
  commit 缺失 / commit 歧义 / sha 非法 / blob 缺失 / blob 不可读 / 无 `.git`）、
  L3/L4 只记录不跨越、`run_git` 拒绝写子命令、源码守卫（无写入路径 / 无 ledger 与状态路径 /
  无数据库与网络 import / 单一 `subprocess.run` 收口）、CLI 选项契约与 ASCII/exit code 契约；
- 新增真实仓库集成回归 `tests/integration/test_review_binding_regression.py`（**8 项**）：
  - **必须能复算 GPT 已写入台账的绑定**：对 `GOLD-027` 复算出与
    `.ai/GPT_REVIEW_LEDGER.json` 完全一致的 `result_sha256`（`d1cf9b90…`）与
    `reviewed_commit.sha`（`e3cadbd1…`）；
  - 对 `GOLD-028`：`result.sha256` == `sha256(git cat-file blob HEAD:.ai/results/GOLD-028.json)`，
    `worktree_sha256` == 独立 `hashlib` 结果，完成 commit（`06a96622…`）可由测试自己的
    `git log` 复现且是 `HEAD` 的祖先；
  - 命令零副作用：`git status --porcelain`、`.ai/tasks` / `.ai/results` 树摘要、
    `PROJECT_STATE.json`、`GPT_REVIEW_LEDGER.json` **前后字节完全一致**；
  - Phase / blocker / 交易安全不变量未变（`Phase 3` / `BLOCKED` / `PHASE3_3_DATA` /
    `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`）。

### 4. 范围守规

- 只读、零网络、零数据库、零真实凭据；未进入 Phase 3.4、未跨 L3/L4；
  `PHASE3_3_DATA` 保持 BLOCKED；`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`；
  测试写入全部发生在 `tmp_path`；Cline 未执行任何 Git 写操作。
- planner snapshot / review ledger schema / rolling queue 语义未变。

### 5. 遗留 / 下一步

- 本层只提供**事实**：`binding.facts_complete=true` 仅代表事实齐全，**不代表**任务被 Review
  通过；verdict / `acceptance_summary` / `reviewed_at` 仍必须由 GPT 自己签发。
- 建议下一步（由 GPT 决定）：用 `python -m orchestrator.review_binding --task GOLD-028`
  的 `result.sha256` + `commit.sha` 完成 GOLD-028 的 ledger 绑定，再按 §2 状态机推进指针；
  若出现同一 task 多个终态 commit（`COMPLETION_COMMIT_AMBIGUOUS`），必须人工裁决，
  工具不会替任何一方选一个 commit。

---

## GOLD-032：GPT Review Ledger 完整性 / 连续性只读门禁

### 1. 背景 / 问题

- `.ai/DEVELOPMENT_PROTOCOL.md` §2.7 已把 GPT Review 变成机器可审计台账，§2.8（GOLD-031）已
  提供可复算的 result SHA-256 / reviewed commit identity；但**台账本身**仍可能被静默删改：
  - 删掉 / 漏掉一条 review 条目，或调换条目顺序，没有任何东西会发现；
  - 只改一个 `reviewed_result.result_sha256` 或 `reviewed_commit.sha`，台账就与客观事实脱节；
  - 若让 Executor 去"对账并修正"，等于把 Review 证据的解释权交给了执行方。

### 2. 变更（最小范围）

- `orchestrator/review_ledger_integrity.py`（新增，纯只读）：
  - CLI `python -m orchestrator.review_ledger_integrity`（stdout 纯 ASCII JSON、stderr 只有
    人类摘要）；契约 `schema=gold-ai/review-ledger-integrity/v1` + `schema_version=1`，
    `facts_digest` 只覆盖确定性事实（排除 `generated_at` / `facts_digest` / `determinism`）；
  - 复用 `orchestrator.review_ledger.validate_review_ledger` 做 schema / 条目校验，并把其
    issue code 翻译为本模块稳定 code；
  - 顺序连续性：按**台账文件原始顺序**判定升序（`LEDGER_ORDER_REGRESSION`）与 `reviewed_at`
    单调性（`LEDGER_REVIEW_TIME_REGRESSION`）；
  - 覆盖窗口连续性：`reviewed_from` / 最新 PASS review 到最新条目之间，若存在 completed
    result 却没有台账条目 ⇒ `LEDGER_CHAIN_GAP`（删项 / 漏项）；
  - 客观绑定：对每条有效条目调用 GOLD-031 `orchestrator.review_binding` 复算 manifest，逐项
    比对 `result_sha256` / `status` / `finished_at` / `commit.sha` / `commit.branch`，不一致分别
    报 `LEDGER_RESULT_HASH_MISMATCH` / `LEDGER_RESULT_STATUS_MISMATCH` /
    `LEDGER_RESULT_FINISHED_AT_MISMATCH` / `LEDGER_COMMIT_SHA_MISMATCH` /
    `LEDGER_COMMIT_BRANCH_MISMATCH`；manifest `facts_complete=false` 报
    `LEDGER_MANIFEST_FACTS_INCOMPLETE`；
  - 重复条目报 `LEDGER_DUPLICATE_TASK`；台账缺失 / 损坏 / schema 不认识 / entries 非法报
    `LEDGER_MISSING` / `LEDGER_UNREADABLE` / `LEDGER_SCHEMA_UNSUPPORTED` /
    `LEDGER_ENTRIES_INVALID`（退出码 `3`）；其它漂移退出码 `2`；全部一致退出码 `0`；
  - **绝不自动修复**（不重排、不补条目、不改哈希），**绝无任何写入路径**：不写台账 /
    `PROJECT_STATE` / tasks / results；模块自身不启动任何外部进程，零网络 / 零数据库 / 零
    业务证据 / 零模型调用；`authority` 段硬编码 `tool_can_sign_review=false` /
    `tool_can_repair_ledger=false` / `tool_can_advance_state=false` / `writes_*=false` /
    `review_authority=gpt_only`；
  - 只**原样回显**台账已有 `verdict` / `reviewed_at`（作为待验证的客观事实），**绝不**创建或
    修改 verdict / `acceptance_summary` / `reviewed_at`。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.9 记录契约、验证内容、fail-closed 码、GPT-only
  边界与只读保证（§2.7 / §2.8 语义不变）。
- 未新增依赖、未改 `pyproject.toml`、未改 `.ai/tasks` / `.ai/results` /
  `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`。

### 3. 验证

- 新增单元回归 `tests/unit/test_ai_orchestrator_review_ledger_integrity.py`（**29 项**）：
  正常链（3 条条目全部 bound、零 issue、退出码 0）、删项（`LEDGER_CHAIN_GAP`）、改 hash /
  改 commit sha / 改 branch / 改 status / 改 finished_at、重复 task、乱序、review 时间回退、
  manifest 漂移、未知 verdict、未知 schema / entries 非法 / 台账缺失 / 台账损坏 / metadata
  非法、确定性（两次构建字节相同、`facts_digest` 排除 wall-clock 且可独立复算）、源码守卫
  （无写入路径 / 无网络 import / 无 planning API）、authority 边界、CLI 选项契约与 ASCII /
  退出码契约；
- 新增真实仓库集成回归 `tests/integration/test_review_ledger_integrity_regression.py`
  （**11 项**）：真实台账 `integrity_ok=true` 且 3 条条目全部与 GOLD-031 manifest 一致；
  `GOLD-027` 的 `result_sha256`（`git cat-file blob HEAD:.ai/results/GOLD-027.json`）与
  reviewed commit 可由测试自己独立复算；对**台账副本**做改 hash / 改 commit / 删条目 / 乱序 /
  重复 task 全部 fail-closed；命令零副作用（`git status --porcelain`、`.ai/tasks` /
  `.ai/results` 树摘要、`PROJECT_STATE.json`、`GPT_REVIEW_LEDGER.json` 前后字节完全一致）；
  CLI fresh subprocess 确定性 + ASCII；Phase / blocker / 交易安全不变量未变；
- 全量回归：`pytest tests`（unit + integration）**3269 passed / 1 skipped**（363s）；
  `ruff check .` → **All checks passed!**；
  `mypy config database src scripts` → **Success: no issues found in 181 source files**。

### 4. 范围守规

- 只读、零网络、零数据库、零真实凭据；未进入 Phase 3.4、未跨 L3/L4；`PHASE3_3_DATA` 保持
  BLOCKED；`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`、
  `src/alpha/**`、`src/execution/**`、`database/**`、`.env`、`config/rss_sources.json`；
- 测试写入全部发生在 `tmp_path`；Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- 本层只验证**客观绑定与连续性**：`integrity_ok=true` 只表示"台账与事实一致且顺序连续"，
  **不是** Review 结论，也不会推进任何指针；verdict / `acceptance_summary` / `reviewed_at`
  仍必须由 GPT 自己签发；
- 覆盖窗口缺口判定只覆盖 `[reviewed_from, 最新条目]` 区间；`reviewed_from` 之前的历史不追溯，
  也不应把尚未 review 的新 completed 任务误报成"删项"（那属于 §2.7 的
  `COMPLETED_BUT_UNREVIEWED`）；
- 建议下一步（由 GPT 决定）：用 `python -m orchestrator.review_binding --task <task_id>` 取得
  事实、写 ledger 后跑一次 `python -m orchestrator.review_ledger_integrity` 作为台账自检，再按
  §2 状态机推进指针。


## GOLD-033：人工证据提交就绪包（Phase 3.3 blocker-facing 只读工具）

### 1. 背景 / 问题

- GOLD-026 ~ GOLD-030 已各自提供只读缺口诊断、intake handoff 预检、材料级人工核验凭证与
  端到端链审计，但**业务方**拿到这些工具时仍要自己拼出"我现在到底缺哪些**真实材料**、
  哪些必须人工做、哪些工程侧已就绪、L3 还等什么"；
- 缺一个**只读聚合**层：把已有结论汇总成**单一、确定、可操作**的提交就绪包。若让 Executor
  顺手"判定一下是否有资格"，就等于把资格解释权交给执行方。

### 2. 变更（最小范围）

- `src/evidence/submission_readiness.py`（新增，纯只读聚合，~1730 行含文档）：
  - 复用既有能力，**不新增 / 不降低**任何阈值：GOLD-006 `build_readiness_report` /
    GOLD-008 `build_handoff_report` / GOLD-027 `MATERIAL_SPECS` + `TIME_SEMANTICS_REQUIREMENTS`
    / GOLD-028 `attestable_material_keys` + `REQUIRED_VERIFICATION_CATEGORIES` +
    `verify_attestation` / GOLD-026 `build_gap_diagnostic`（紧凑摘要）/ GOLD-030 链审计事实
    （新增只读 `ChainBinding` + `load_chain_binding`，**不重算**阶段语义）；
  - **五个独立结论**（机器可读）：`engineering_ready`（只说明工程链 / 契约一致）/
    `submission_materials_complete`（提交材料**结构完整**，仍待人工核验）/
    `human_verification_complete`（当前 GOLD-028 凭证仍成立）/
    `data_qualification_passed` / `phase_transition_allowed`（后两者**硬编码 false**），
    `l3_gate_pending` / `blocker_active` / `human_gate_required` / `gate_blocked` 恒 true；
    `nothing_open` 仅由前四项派生，**绝不**改资格字段；
  - **稳定 missing reason codes**（`SubmissionCode`，如
    `SUBMISSION_AUTHORIZATION_MISSING` / `SUBMISSION_AVAILABILITY_OOS_MISSING` /
    `SUBMISSION_SYNTHETIC_MATERIAL_ONLY` / `SUBMISSION_ATTESTATION_STALE` /
    `SUBMISSION_ENGINEERING_CHAIN_INCOMPLETE`）+ **人工动作清单**（每条只引用既有契约字段 /
    阈值 / 命令；`blocking` 恒 true；末条恒为 `l3_human_gate_decision`）+ L3 待办清单；
  - **逐材料事实**：材料契约漂移即 fail-closed（`_verify_material_contract`）；只带合成 /
    示例标记的候选包一律 `NON_QUALIFYING`；`counts_toward_eligibility` / `qualifies` 硬编码
    false；**绝不生成或推断** `published_at` / `collected_at` / `effective_at` /
    `available_at` / OOS 值（载荷里连键名都不出现，唯一时间戳是 `generated_at`）；
  - **内容身份**：`facts_digest` / `pack_id` 只覆盖确定性事实（不含路径与审计时点），
    材料 / 结论 / 凭证 / 链漂移必然改变身份；
  - **默认零写入**：`run_submission_pack` 只有显式 `out_path` 才先取单实例锁再原子落盘
    **本包本身**，拒绝写进 inbox；模块内**没有** intake / commit / 数据库写入 / 网络调用。
- `scripts/evidence_submission_readiness.py`（新增 CLI，默认只读）：`--inbox-dir`（GOLD-027
  只读预检）/ `--attestation`（**必须**与 `--inbox-dir` 同时给出）/ `--chain-audit` /
  `--rehearsal --work-dir`（纯本地 fixture，仅供验证工具链健康）/ `--as-of` / `--json` /
  `--out`（唯一写开关）/ `--lock`；演练模式**禁止**与真实输入同时给出（fail-closed）；
  退出码 `0/2/3/4/5/6`（`5` 为**当前预期**：仍待真实材料 / 人工核验 / 工程链）；
- `src/evidence/__init__.py`：登记 `submission_readiness` 惰性导出（`__all__` 同名同源，
  包初始化仍不 eager 导入任何子模块）；
- `README.md`：新增 GOLD-033 章节（红线、五个结论语义、稳定原因码、用法、退出码、回归）；
- 未新增依赖、未改 `pyproject.toml`、未改 `.ai/**`、未改 `src/alpha/**` / `src/execution/**` /
  `database/**`。

### 3. 验证

- 新增单元回归 `tests/unit/test_evidence_submission_readiness.py`（**27 项**）：
  材料缺失原因码与 GOLD-027 材料契约对齐、版本化 schema 只读语义、源码级守卫（模块与 CLI
  都无网络 / 文件时间 / 数据库写入调用）、空输入逐材料 `MISSING` + 稳定原因码 + 退出码 5、
  人工动作清单覆盖材料与固定 gate 步骤、载荷**没有**任何证据时间键（唯一时间戳 `generated_at`）、
  Markdown 摘要保持 blocker、**rehearsal / fixture 工程链全绿仍全部 blocked**（真实材料缺失
  **不**被掩盖）、演练模式拒绝真实输入、链绑定往返一致 + 非法结构 fail-closed + 最早失败阶段透出、
  Mock 候选包 `NON_QUALIFYING` 且**永不计**真实材料、真实材料结构完整仍
  `data_qualification_passed=false` / `l3_gate_pending=true`、Mock 与真实包并存只计真实包、
  availability 未验证如实报 `PRESENT_UNVERIFIED`、凭证完成 / 漂移（`SUBMISSION_ATTESTATION_STALE`）、
  `pack_id` / `facts_digest` 确定性且漂移必变、`nothing_open=true` 也**不**改资格字段、
  退出码映射、默认零写入 / `--out` 只写本包 + 锁文件 / 写进 inbox 被拒；
- 新增集成回归 `tests/integration/test_evidence_submission_readiness_integration.py`（**15 项**，
  临时 SQLite + 本地候选目录）：CLI 选项契约（**无** qualify / advance / intake 开关）、
  空库 → 退出码 5 + 零文件写入 + 零数据库写入、`--inbox-dir` 只读预检（inbox 逐字节不变、
  不 ingest）、缺失 inbox → 退出码 2、rehearsal 只写显式 work-dir 且只证明工具链健康、
  `--chain-audit` 只让 `engineering_ready=true`、`--attestation` 只让
  `human_verification_complete=true`、`--out` 唯一写开关（原子写、无 `.tmp` 残留、写进 inbox 被拒）；
- `tests/integration/test_evidence_cli_smoke.py`：登记新 CLI → import / `--help` 冒烟 +
  覆盖守门通过（fresh subprocess、空 cwd 零副作用）；
- 全量回归：`pytest tests`（unit + integration）**3314 passed / 1 skipped**（372.20s）；
  `ruff check .` → **All checks passed!**；
  `mypy config database src scripts` → **Success: no issues found in 183 source files**。

### 4. 范围守规

- 只读聚合、零网络、零数据库写入、零交易；未进入 Phase 3.4、未跨 L3/L4；`PHASE3_3_DATA`
  保持 BLOCKED；`LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`、
  `src/alpha/**`、`src/execution/**`、`database/**`、`.env`、`config/rss_sources.json`；
- 测试写入全部发生在 `tmp_path`；Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- 本层只**汇总客观缺口**：`submission_materials_complete=true` 只表示提交材料**结构完整**、
  `human_verification_complete=true` 只表示材料级人工核验已完成、`engineering_ready=true`
  只表示工程链契约一致 —— 三者**都不**等于 `data_qualification_passed`，也**不**解除
  `PHASE3_3_DATA`（只能由真实授权证据 + L3 人工 Gate 独立改变）；
- 真实材料仍需业务方提供：按 GOLD-027 handoff 契约准备 Author / News 证据 →
  GOLD-028 逐材料人工核验 → GOLD-029 review 批准 → GOLD-030 链审计 → L3 人工决策；
- 建议下一步（由 GPT 决定）：业务方提交材料**前**先跑
  `python -m scripts.evidence_submission_readiness --rehearsal --work-dir logs/submission_rehearsal --json`
  验证工具链健康，再用 `--inbox-dir` / `--attestation` / `--chain-audit` 读取真实缺口并据此补齐材料。



## GOLD-034：GPT Planner 队列补给请求事实包（只读 control-plane 事实）

### 1. 背景 / 问题

- `.ai/DEVELOPMENT_PROTOCOL.md` §2.10 要求 GPT Planner 在「当前任务之外的 queue head」
  默认维持 **3 个已批准 follow-on tasks**，但仓库侧此前只有 `planner_snapshot` 的通用状态快照
  （§2.9），**没有**一个直接回答「当前任务之外还剩几个已批准任务、缺几个」的确定性事实包，
  于是低水位只能等到 `No runnable task` 才发现断粮；
- 与此同时红线不变：只有 GPT 能规划 / 补队列 / 改 `PROJECT_STATE`，Cline / DeepSeek
  只是 Executor。若让 Executor 顺手「补一下队列」，等于把规划权下放。

### 2. 变更（最小范围）

- `orchestrator/planner_refill_request.py`（新增，只读 builder + CLI，~980 行含文档）：
  - **复用** `orchestrator.planner_snapshot` 的只读事实（queue / pointer / gates /
    results / tasks），**不复制第二套** readiness / 依赖 / 终态 / 漂移判断；
  - 版本化契约 `gold-ai/planner-refill-request/v1`（`schema` + 整数 `schema_version`），
    核心事实：`queue_head` / `queue_head_runnable` / `follow_on_count` /
    `lookahead_target`（默认 3，可由 `planner_lookahead_size` 或显式参数覆盖，
    非法值一律 fail-safe 回落 3）/ `deficit` / `refill_required` /
    `hard_gate_tail_allowed` / `latest_completed` / `completed_but_unreviewed` /
    `state_result_drift` / `blockers` / `human_gates` / `safety_invariants` / `phase`
    （只读事实，`transition_allowed=false`）/ `reason_codes`（14 个稳定 code）/
    `snapshot_ref` / `issues` / `summary`；
  - **语义边界**：`refill_required` 只是 `deficit > 0` 的**计数事实**，不是任务内容、
    不是下一任务、不是 Phase 决定；`hard_gate_tail_allowed=true` 表示只剩 human-only
    hard gate，GPT 可停在 gated tail 而不制造 filler；载荷里不存在任何后续任务内容 /
    标题 / Phase 决定 / Review 结论字段（`FORBIDDEN_REQUEST_KEYS` 守卫）；
  - **确定性**：`facts_digest` 只覆盖状态事实，`generated_at` 被显式排除
    （`determinism.wall_clock_in_facts=false`），相同仓库事实 ⇒ 相同 digest 且可独立复算；
  - **默认零写入**：唯一写路径是显式 `--output`，且**复用**
    `orchestrator.planner_snapshot_output` 的 fail-closed 守卫（只允许
    `<root>/.ai/runtime/**` 或系统临时目录；禁止 `.ai/tasks` / `.ai/results` /
    `.ai/PROJECT_STATE.json` / 业务路径；父目录不存在也不创建）；
  - 退出码 `0` 足量无漂移 / `2` 需要 GPT 规划或检出漂移 / `3` PROJECT_STATE 不可读 /
    `4` `--output` 目标被拒（此时绝不写文件）；`stdout` 为纯 ASCII JSON，
    人类摘要只在 `stderr`；
  - 无网络 / 数据库 / 子进程 / LLM 调用，不新增依赖。
- `.ai/DEVELOPMENT_PROTOCOL.md` §2.10：新增「只读事实包（GOLD-034 起）」条目，
  写清关键字段、`refill_required` 语义、退出码与 `--output` 守卫边界；
- 未改 `pyproject.toml`、未改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`，未进入 Phase 3.4、未跨 L3/L4。

### 3. 验证

- 新增单元回归 `tests/unit/test_ai_orchestrator_planner_refill_request.py`（**54 项**）：
  版本化契约与 29 个顶层字段冻结、**队列足量 / 低水位（缺 1）/ 只有 head（缺 3）/
  空队列**、head 处 L3/L4（`blocking_gate_at_head` + 不得误判 hard gate）、
  **仅剩 human-only hard gate**（`hard_gate_tail_allowed=true` 且计数事实保留）、
  `auto_start=false` / `requires_human_approval=true` / L4 三类人工依赖、
  state/result 漂移（`STATE_RESULT_DRIFT` + 指针事实）与「无漂移」、
  completed-but-unreviewed（含 review 指针缺失）、blocker / Human Gate / 安全不变量镜像、
  `lookahead_target` 默认 3 / `planner_lookahead_size` 覆盖 / 8 类非法值回落 3 +
  warning issue、显式参数只改缺口、`facts_digest` wall-clock 无关且可复算 / 对事实敏感、
  构建前后工作树逐字节不变、载荷无任务内容 / Phase 决定键、模块无规划 / 写状态 API +
  源码级守卫（无 `write_text` / `open(` / `subprocess` / 网络 / `mkdir`）、
  CLI 选项契约（无 `--plan` / `--next-task` / `--create-task` / `--update-state`）、
  ASCII stdout + 退出码 0/2/3/4、`--output` 只写 runtime 或系统临时目录、
  禁止路径（`.ai/tasks` / `.ai/results` / `PROJECT_STATE` / tracked 文件）被拒且零写入、
  父目录缺失被拒、默认运行零写入、fresh subprocess 字节稳定；
- 新增集成回归 `tests/integration/test_planner_refill_request_regression.py`（**12 项**，
  真实仓库）：queue 事实与 `planner_snapshot` **逐项一致**（含 `snapshot_ref.facts_digest`
  与 issue codes）、低水位数学自洽（`deficit == max(0, target - follow_on)`）、
  latest_completed / completed_but_unreviewed 与 results 一致、顶层契约冻结 +
  reason codes 只在词表内、载荷无规划内容 / 无 refill 权限、`PHASE3_3_DATA` blocker 与
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` / L3-L4 边界不变、
  构建与 CLI 在真实仓库上 **deterministic + 零写入**（`.ai/tasks` / `.ai/results` /
  `PROJECT_STATE` / `GPT_REVIEW_LEDGER` 逐字节、`git status --porcelain` 前后一致）、
  `--output` 指向 tracked planner 路径一律退出码 4 且零写入；
- 全量回归：`pytest tests`（unit + integration）**3383 passed / 1 skipped**（378.20s）；
  `ruff check .` → **All checks passed!**；
  `mypy config database src scripts` → **Success: no issues found in 183 source files**。

### 4. 范围守规

- 只读、零网络、零数据库、零子进程（除测试自身的只读 `git status`）、零 LLM 调用；
  未进入 Phase 3.4、未跨 L3/L4；`PHASE3_3_DATA` 保持 BLOCKED；
  `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`src/alpha/**`、`src/execution/**`、`database/**`、
  `.env`、`config/rss_sources.json`；未新增依赖、未改 `pyproject.toml`；
- 测试写入全部发生在 `tmp_path`（受控 `--output` 断言也只在 fake root 内）；
  Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- 本工具只产**事实**：`refill_required=true` 只表示「当前 queue head 之外的已批准
  follow-on 少于 `lookahead_target`」，**不**产出任何后续任务内容，也**不**决定 Phase；
  `hard_gate_tail_allowed=true` 是「允许停在 gated tail」的合法例外；
- Orchestrator 生命周期接入（成功 commit+push 后与 idle 时主动提示
  `GPT_PLANNER_REFILL_REQUIRED`、去重/节流、只读镜像写 `.ai/runtime/**`）留给
  **GOLD-035**；三任务前瞻契约的机器化门禁（含「不得把 target 降为 0」「可运行任务
  必须排在 human-gated tail 之前」矩阵）留给 **GOLD-036**；
- 建议下一步（由 GPT 决定）：先跑
  `python -m orchestrator.planner_refill_request --generated-at <fixed>` 读取真实
  `queue_head` / `follow_on_count` / `deficit` / `reason_codes`，据此补足 follow-on；
  再按 GOLD-035 / GOLD-036 把提示与契约固化。

## GOLD-035：Orchestrator 在队列低水位主动请求 GPT Planner（只读提示接入生命周期）

### 1. 背景 / 问题

- GOLD-034 已经把「当前任务之外还剩几个已批准任务、缺几个」变成确定性只读事实包，
  但它只是一个**离线 CLI**：Orchestrator 的运行循环里没有任何调用点，于是实际运行中
  仍然只能在 `No runnable task` 反复出现后才发现断粮；
- 红线不变：只有 GPT 能规划 / 补队列 / 改 `PROJECT_STATE`；Cline / DeepSeek 只是
  Executor，任何「顺手补一下队列」都是把规划权下放，必须 fail-closed。

### 2. 变更（最小范围）

- `orchestrator/ai_orchestrator.py`（唯一改动的主代码文件，新增 §GPT Planner refill
  低水位提示）：
  - 新增 `PROJECT_STATE_PATH` 只读常量与 `repo_scoped_import`：生产以
    `py -u orchestrator/ai_orchestrator.py` 启动时 `sys.path[0]` 是 `orchestrator/`，
    因此这里只在必要时把仓库根临时放入 `sys.path`（导入后立即还原）并缓存模块，
    绝不留副作用、绝不改任何项目状态；
  - `planner_refill_facts()`：**复用** GOLD-034 `build_planner_refill_request`（只读），
    任何异常 / 模块不可用一律降级为 `None`（只读提示降级，绝不阻塞 rolling queue）；
  - `planner_refill_hint_line()`：结构化提示行，稳定字段
    `head` / `follow_on_count` / `target` / `deficit` / `reason_codes` /
    `executor_can_refill=false`；`PLANNER_REFILL_REQUIRED_CODE =
    "GPT_PLANNER_REFILL_REQUIRED"`，队列足量时为 `GPT_PLANNER_REFILL_SATISFIED`；
  - `planner_refill_report()`：只读计算 + 按需提示（`only_when_required` / `throttle`），
    返回新的节流状态；提示时把同一份事实镜像到
    `<root>/.ai/runtime/planner_refill_request.json`，复用
    `orchestrator.planner_snapshot_output` 的 fail-closed 守卫（只允许运行时/临时路径，
    路径被重定向时只记日志不写文件 —— 避免写出误导性的诊断镜像）；
  - `should_emit_refill_hint()` / `planner_refill_idle_hint()`：与 `should_log_idle`
    同构的节流（同一事实状态在 `REFILL_HINT_SECONDS` 内只提示一次；
    `REFILL_HINT_SECONDS = max(POLL_SECONDS, AI_REFILL_HINT_SECONDS 或 1800)`），
    事实一变（签名变化）立即重新提示；
  - `process_task()`：validation + commit + push **全部成功（远端可见）** 之后立即
    `planner_refill_report(context=post_successful_commit_push, only_when_required=True,
    throttle=False)`，然后再 `return "completed"`；
  - `run_iteration()`：新增可选入参 `refill_hint_state`，idle / no-runnable 路径在原有
    停线判定**之后**额外调用 `planner_refill_idle_hint(...)`，并把新的节流状态放进返回值
    `refill_hint_state`（其余 return 分支显式置 None 或原样透传）；
  - `main()`：跨轮传递 `refill_hint_state`（`step.get(...)`，兼容旧结构）并在启动信息里
    打印 `Refill hint: <s> (GPT_PLANNER_REFILL_REQUIRED throttle)`。
  - **未改变**：`find_next_task_with_reason` / `evaluate_task_readiness` / 依赖顺序 /
    Git sync / 恢复状态机 / push-pending 语义 / Gate 判定一律保持原样，提示只发生在
    「合法任务已执行完」或「已经停线 idle」之后。
- `.ai/DEVELOPMENT_PROTOCOL.md` §2.10：新增「Orchestrator 生命周期接入（GOLD-035 起）」
  条目（两个 context、节流口径、镜像边界、只报告不规划）。
- 未新增依赖、未改 `pyproject.toml`、未改 `start_agent.bat`（默认节流值已足够，且该文件
  不在本任务允许的主路径内）。


## GOLD-036：GPT 三任务前瞻自动补队列契约与端到端门禁

### 1. 背景 / 问题

- GOLD-034 把「当前任务之外还剩几个已批准任务、缺几个」变成了只读事实包，
  GOLD-035 让 Orchestrator 在低水位主动提示；但两者都只是**观察**：如果后续改动
  把 follow-on target 悄悄降为 0、把 Executor 变成 Planner、或让 human-gated 的
  hard gate 被绕过，仓库里没有任何**机器可测**的契约能拦住这类回归；
- 另一条红线同样必须机器化：只要 `PHASE3_3_DATA` 未解除，自动规划只允许产出
  blocker-facing / control-plane / 证据准备类工作，**绝不允许** Phase 3.4 功能任务
  被标记为可执行。

### 2. 变更（最小范围）

- `orchestrator/planner_autopilot_contract.py`（新增，只读纯函数模块，~810 行含文档）：
  - **冻结常量**：`LOOKAHEAD_TARGET=3`、`FOLLOW_ON_TARGET_MINIMUM=1`、
    `PLANNING_AUTHORITY="gpt_only"`、`EXECUTOR_CAN_REFILL=False`、
    `HARD_GATE_TAIL_IS_ONLY_DEFICIT_EXCEPTION=True`、`BLOCKING_HUMAN_GATES={L3,L4}`、
    `PHASE_GATING_BLOCKER_CODES=("PHASE3_3_DATA",)`、`FORBIDDEN_PHASE_UNDER_BLOCKER="Phase 3.4"`；
  - **工作类别词表**：`BLOCKER_FACING` / `CONTROL_PLANE` / `EVIDENCE_PREPARATION`
    为 blocker 下**唯一可被标记可执行**的类别，`FEATURE` / `FILLER` / `UNKNOWN`
    被禁止；类别由 task `type` 前缀确定性推导（大小写不敏感，可被显式
    `work_class` 覆盖，未知一律 fail-closed 归入 `UNKNOWN`）；
  - **稳定 violation 词表**（8 个）：`EXECUTOR_CLAIMS_PLANNER`、
    `FOLLOW_ON_TARGET_BELOW_MINIMUM`、`FILLER_TASK_FORBIDDEN`、`HUMAN_GATE_BYPASS`、
    `PLAN_EXECUTABLE_INCONSISTENT`、`INADMISSIBLE_WORK_CLASS_EXECUTABLE_UNDER_PHASE_BLOCKER`、
    `PHASE34_FEATURE_TASK_EXECUTABLE`、`RUNNABLE_TASK_AFTER_GATED_TAIL`；
  - `audit_follow_on_plan()`：对调用方传入的**候选** plan 做确定性审计，输出
    `follow_on_count` / `lookahead_target` / `deficit` / `refill_required` /
    `hard_gate_tail_allowed` / `admissible_work_classes` / `violations` /
    `reason_codes` / `compliant` / `plan_digest`；可执行判定与
    `ai_orchestrator.evaluate_task_readiness` 同口径（L3/L4、`auto_start=false`、
    `requires_human_approval=true`、非法元数据一律不可执行）；
  - `contract_facts()` / `gpt_cloud_check_boundary()`：把「GPT 唯一 Planner +
    云端条件检查 + 本地三任务缓冲 + 异常恢复」写成只读事实
    （`planner_api_key_in_repo=false`、`planner_api_key_env_required=false`、
    `local_executor_can_call_gpt=false`、`local_executor_can_generate_task=false`）；
  - **纯只读**：不读 / 不写文件、不联网、不读数据库、不调用任何 LLM，无新增依赖。
- `orchestrator/planner_refill_request.py`（最小接线）：
  - `planner_authority` 新增 `autopilot_contract`（内嵌 `contract_facts()`），
    `executor_can_refill` 改为读契约常量；
  - `summary` 新增 `follow_on_target_minimum=1` 与 `executor_can_refill=false`，
    使「不得把 target 降为 0」「Executor 不能补队列」在事实包里显式可断言。
- `.ai/DEVELOPMENT_PROTOCOL.md`：新增 §2.10.1（契约机器化门禁）与 §2.10.2
  （GPT 云端条件检查 vs 本地三任务缓冲的职责边界与异常恢复）；
- `README.md`：新增 GOLD-036 章节（红线、冻结常量、回归矩阵、blocker 下的工作范围、
  职责边界与异常恢复、只读用法）；
- 未改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json` / `src/**` / `database/**` / `.env` / `pyproject.toml`，
  未进入 Phase 3.4、未跨 L3/L4。


### 3. 验证

- 新增单元回归 `tests/unit/test_planner_autopilot_contract.py`（**67 项**）：
  冻结常量与词表完整划分（含 `lookahead_target` 与 refill 同源为 3）、
  **前瞻矩阵**（running+3 ⇒ `deficit=0` / `refill_required=false`；running+2 ⇒
  `deficit=1`；running+0 ⇒ `deficit=3`；空队列**不**算 human-only）、
  **排序**（可运行任务在 gated tail 前合法；排在 gated tail 后 ⇒
  `RUNNABLE_TASK_AFTER_GATED_TAIL`）、**human-only 可停线但不得造 filler**、
  **target 降为 0 / -1 / 非整数 / True / None 一律 fail-safe 回落 3 并违规**、
  **Executor 变 Planner** / **绕过 hard gate（L3、L4、`auto_start=false`、
  `requires_human_approval=true`、非法 gate 类型）** / **可执行声明不一致** 全部被拒、
  **blocker 下工作类别**（16 类 `type` 分类 + blocker-facing / control-plane /
  evidence-preparation 保持可执行 + FEATURE / UNKNOWN 被拒 + 显式 `work_class` 覆盖）、
  **Phase 3.4 功能任务**（`"Phase 3.4"` / `"PHASE3_4"` / `"3.4"` / `3.4` /
  `"Phase 4.0"` / `"phase_3_4_alpha"` 六种写法）被标记可执行 ⇒
  `PHASE34_FEATURE_TASK_EXECUTABLE`，被 gate 挡住则不违规、无 blocker 时不限制、
  非 Phase blocker 不限制、`parse_phase` 11 组边界、`plan_digest` 确定性与敏感度、
  载荷无任务内容 / Phase 决定键、模块无规划 / 写 API（源码级守卫）、
  契约**零 API key / 零环境变量 / 零网络 / 零 LLM**；
- 新增集成回归 `tests/integration/test_planner_autopilot_contract_regression.py`（**9 项**，
  真实仓库只读）：refill CLI 事实包内嵌的契约与 `contract_facts()` **逐字段一致**
  （`lookahead_target=3` / `follow_on_target_minimum=1` / `executor_can_refill=false`），
  提示行携带 `target` / `deficit` / `hard_gate_tail_allowed` / `executor_can_refill=false`，
  真实队列的 follow-on 计数 / 缺口 / 是否需补与契约审计**逐项一致**
  （矩阵在真实数据上自洽：`follow_on >= target ⇒ deficit=0`；否则
  `deficit = target - follow_on`），真实队列在 `PHASE3_3_DATA` 下**只**把
  blocker-facing / control-plane / evidence-preparation 标为可执行，
  同一真实队列上构造的 **Phase 3.4 功能任务反例**被
  `PHASE34_FEATURE_TASK_EXECUTABLE` 拒绝，真实 `PROJECT_STATE` 的
  `PHASE3_3_DATA` 与两条交易安全不变量仍在，整条链路（CLI + 审计 + 提示 + 事实包）
  在真实仓库**零写入**（`.ai/tasks` / `.ai/results` / `PROJECT_STATE` /
  `GPT_REVIEW_LEDGER` 逐字节不变、`git status --porcelain` 前后一致），
  `orchestrator/*.py` 无任何 planner API key / 凭据痕迹；
- 全量门禁：`.venv\Scripts\python.exe -m pytest tests -q`、
  `.venv\Scripts\python.exe -m ruff check .`、
  `.venv\Scripts\python.exe -m mypy config database src scripts` 全绿；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`src/alpha/**`、`src/execution/**`、`database/**`、
  `.env`、`config/rss_sources.json`；未新增依赖、未改 `pyproject.toml`；
  测试写入全部发生在内存 / `tmp_path`；Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- 本契约只**判定**，不规划：它不会（也不允许）生成任何 follow-on task 内容，
  也不会自动补队列；真正的规划仍由 GPT 依据只读事实（
  `python -m orchestrator.planner_refill_request` + 契约审计）完成；
- 真实仓库当前处于 `STATE_RESULT_DRIFT`（`current_task=GOLD-035` 但 result 已
  `completed`）：契约/事实包只报告该漂移（`refill_required=true`），按 §2.10.2
  由 GPT 读取最新事实后补队列与推进 `PROJECT_STATE`，Executor 不做修复；
- 建议下一步（由 GPT 决定）：GOLD-036 收口后按 `queue_target_size=3` 补足
  blocker-facing / control-plane follow-on，并按 GOLD-037 把 completed-but-unreviewed
  的正式 Review backlog 变成确定性批量绑定事实清单。

## GOLD-037：GPT Review Backlog 批量绑定事实清单（只读 control-plane 事实）

### 1. 背景 / 问题

- GOLD-025（Review Ledger）、GOLD-031（单任务 Review Binding Manifest）、
  GOLD-032（台账完整性门禁）已经把「一次 review」的客观事实做全，但 backlog 里同时挂着
  多个「已完成却尚未进入正式台账」的任务时，GPT 仍只能逐个手跑
  `python -m orchestrator.review_binding --task <id>`：容易漏项，也无法一次性看清
  「哪些还欠 review、每项可绑定事实是什么、有没有已绑定 / 已漂移的项」；
- 目标：给出**一份**确定性、只读的 formal review backlog manifest，逐项**复用**既有
  §2.8 客观事实，让 GPT 能安全、批量地完成实质审查与加密绑定；工具本身**绝不**签发
  verdict、**绝不**写 ledger。

### 2. 变更（最小范围）

- 新增 `orchestrator/review_backlog.py`（纯只读 builder + CLI，契约
  `gold-ai/review-backlog-manifest/v1`）：
  - backlog 范围**唯一口径**：`PROJECT_STATE.last_reviewed_task` 之后**所有**
    `completed` result（`orchestrator.review_ledger.completed_result_ids` +
    `planner.is_newer`，排序 `planner.task_rank`）；
  - 逐项**复用** `orchestrator.review_binding.build_review_binding_manifest`
    取得 result SHA-256 / 完成 commit identity（**未**新增第二套结果 / commit 身份算法）；
  - 逐项字段：`task_id` / `result.status` / `result.finished_at` / `result.sha256` /
    `result.worktree_matches_commit` / `commit.sha` / `commit.branch` / `commit.subject` /
    `facts_complete` / `missing_reason_codes` / `reason_codes` / `ledger`（条目是否存在、
    是否唯一合法、hash 与 commit 是否一致）/ `review_status`；
  - `review_status` 只有客观 `pending` / `bound` / `invalid` 三值；输出里**不存在**
    `verdict` / `acceptance_summary` / `reviewed_at` / `reviewer` / `reviewer_role`
    （`FORBIDDEN_MANIFEST_KEYS` 否定声明 + 测试断言「没有任何字段的值是 verdict」）；
  - 顶层 `coverage` / `ledger` / `chain` / `pointer` / `authority` / `determinism` /
    `issues` / `missing_reason_codes` / `reason_codes` / `summary` / `backlog_digest`
    （`generated_at` 等 wall-clock 不参与 digest）；
  - 复用既有门禁做检测：`review_ledger.validate_review_ledger`（重复 / 非法条目）、
    `review_ledger.pointer_section`（指针 vs 台账 vs results）、
    `review_ledger_integrity.chain_section`（台账顺序回退 / 覆盖窗口缺口），
    稳定 code 分别为 `LEDGER_DUPLICATE_TASK` / `REVIEW_POINTER_*` / `LEDGER_CHAIN_GAP` 等；
  - fail-closed：`LAST_REVIEWED_TASK_POINTER_MISSING`（边界不明 ⇒ backlog 为空、**不猜**）、
    `BACKLOG_ITEM_FACTS_INCOMPLETE`、`BACKLOG_ITEM_MANIFEST_UNUSABLE`、
    `BACKLOG_ITEM_LEDGER_INVALID`、`BACKLOG_ITEM_RESULT_HASH_DRIFT` /
    `_RESULT_STATUS_DRIFT` / `_RESULT_FINISHED_AT_DRIFT` / `_COMMIT_SHA_DRIFT` /
    `_COMMIT_BRANCH_DRIFT`；退出码 `0` / `2` / `3`（state 或台账不可用）/ `4`
    （`--output` 被拒）；
  - 默认零写入；`--output` **复用** `orchestrator.planner_snapshot_output` 的受控路径守卫
    （只允许 `<root>/.ai/runtime/**` 或系统临时目录，且 `.ai/tasks` / `.ai/results` /
    `PROJECT_STATE` / `GPT_REVIEW_LEDGER` 一律拒绝）。
- 新增 `tests/unit/test_ai_orchestrator_review_backlog.py`（**18 项**）：GOLD-028~033 式
  连续 backlog 全部 `pending`、digest 幂等且对内容敏感（wall-clock 不影响 digest）、
  部分已绑定（`bound`）+ 指针滞后 `REVIEW_POINTER_BEHIND_LEDGER` fail-closed、
  result hash 漂移、commit 缺失 / 歧义不可绑定（**不猜 commit**、`missing_reason_codes`
  原样透传）、台账覆盖窗口缺口 `LEDGER_CHAIN_GAP`、重复条目 `LEDGER_DUPLICATE_TASK`、
  台账 / `PROJECT_STATE` 不可用 ⇒ 退出码 3、指针缺失 fail-closed、`authority` 全 False、
  源码守卫（无写入 / 子进程路径，唯一写操作经受控守卫，Executor 不能自签 review）、
  CLI fail-closed 与 `--output` 只写 runtime。
- 新增 `tests/integration/test_review_backlog_regression.py`（**7 项**）：真实仓库上
  backlog 范围与测试自己复算的一致、逐项 result SHA-256 由 `git cat-file` 复算、
  终态 commit 由测试自己的 `git log` 解析、`bound` 与台账条目存在性一一对应、
  前后 `tree_digest` / `worktree_status` 零改写、CLI 幂等且 ASCII、Phase 3.3 blocker 与
  两条交易安全不变量不变。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.11（契约 / 字段 / 稳定 code / 退出码 /
  职责边界）；`README.md` 新增「GPT Review Backlog 批量绑定事实清单（GOLD-037）」小节。

### 3. 验证

- 新增测试：`tests/unit/test_ai_orchestrator_review_backlog.py` 18 passed；
  `tests/integration/test_review_backlog_regression.py` 7 passed；
- 真实仓库只读试跑（`--generated-at` 固定）：
  `[backlog] last_reviewed=GOLD-027 count=11 bound=0 pending=11 invalid=0 missing= codes=`
  退出码 `0`（backlog = `TEST-001` / `TEST-002` / `GOLD-028`..`GOLD-036`，全部
  `facts_complete=true`、`review_status=pending`；`bound=0` 说明台账确实仍停在
  GOLD-027，未被本工具推进）；
- 全量门禁：`.venv\Scripts\python.exe -m pytest tests -q`、
  `.venv\Scripts\python.exe -m ruff check .`、
  `.venv\Scripts\python.exe -m mypy config database src scripts` 全绿；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`src/alpha/**`、`src/execution/**`、`database/**`、
  `.env`；未新增依赖、未改 `pyproject.toml`；测试写入全部发生在 `tmp_path`；
  Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- 本工具只**产事实**：它不会（也**不允许**）签发 verdict、写 ledger、推进
  `last_reviewed_task`；真正的 Review 与加密绑定仍由 GPT 完成；
- 若某个 backlog 项显示 `bound` 而 `last_reviewed_task` 未推进，说明**指针漂移**
  （`REVIEW_POINTER_BEHIND_LEDGER`）：GPT 应先修正指针 / 台账一致性，而不是重复 review；
- 真实仓库当前 backlog 为 11 项全部 `pending`，且不存在 order regression / chain gap /
  重复条目：GPT 可按该 manifest 逐项核对 result SHA-256 与完成 commit 身份后写台账；
- 建议下一步（由 GPT 决定）：按 GOLD-038 建立 GPT 写队列前的远端 HEAD 并发保护事实包，
  并按 GOLD-039 建立 result 顶层状态与 attempt 终态一致性门禁。

## GOLD-038：GPT 写队列前的远端 HEAD 并发保护事实包（只读 control-plane 事实）

### 1. 背景 / 问题

- §2.10（GOLD-034 / 035 / 036）让 GPT 在队列低水位时补队列、§2.11（GOLD-037）让 GPT 一次看清
  formal review backlog；但 GPT 运行在云端：它**读事实**（snapshot / refill / backlog）与
  **写队列**（task / state）之间存在时间窗口，而 Executor 会在此期间完成当前任务并 push result +
  completion commit；没有并发保护时，GPT 会基于**过期快照**写队列，覆盖或错判刚完成的工作；
- 目标：在**任何** planner-owned 写入之前，用**一个**确定性、只读的事实包证明
  「observed HEAD == 调用方 expected HEAD，且 `PROJECT_STATE` 声称的执行指针未与 results 事实
  漂移」；任何不一致都 fail-closed 且禁止 planner mutation，并且**绝不**自动 merge / rebase /
  force push。

### 2. 变更（最小范围）

- 新增 `orchestrator/planner_mutation_precondition.py`（纯只读 builder + CLI，契约
  `gold-ai/planner-mutation-precondition/v1`）：
  - **复用，不复制**：`branch` / `observed_head_sha` 直接取 planner snapshot 的 `git` 段；
    `queue_head` / `task_queue` / `refill.facts_digest` 复用
    `planner_refill_request.build_planner_refill_request`；formal review backlog 指针 +
    `backlog_digest` 复用 `review_backlog.build_review_backlog_manifest`（内部再复用
    `review_binding`）；**未**新增第二套 readiness / 依赖 / 终态 / 任务资格 / commit 身份算法；
  - 新增内容事实：`state`（`blob_sha256` + 解析后 digest + 指针 / 队列 / 不变量回显）、
    `tasks_digest`、`results_digest`（目录内文件字节 sha256 的确定性摘要）与稳定
    `precondition_digest`（`generated_at` / `determinism` / 自身被排除 ⇒ 幂等、无自引用）；
  - **fail-closed**：`--expected-head-sha`（别名 `--expect-head`，7~40 位十六进制）与
    `observed_head_sha` 不一致 ⇒ `STALE_REMOTE_HEAD`；非法形式 ⇒ `EXPECTED_HEAD_SHA_INVALID`；
    `.git` 不可解析 ⇒ 复用 `GIT_INFO_UNAVAILABLE`；`PROJECT_STATE.branch` 漂移 ⇒
    `STATE_BRANCH_DRIFT`；`current_task` / `last_completed_task` / `task_queue` 声明与 results 事实
    漂移 ⇒ 复用 planner snapshot 的 `PROJECT_STATE_POINTER_*` / `QUEUE_DECLARATION_MISMATCH` /
    `QUEUE_TASK_MISSING` 并叠加聚合标记 `STATE_RESULT_DRIFT`；backlog 来源异常 ⇒
    `REVIEW_BACKLOG_FACTS_UNAVAILABLE`；
  - `planner_mutation.allowed == (blocking_reason_codes == [])`；`forbidden` / `requires_reread` 与
    之恒等；`execution_blocked=false`、`gates_planner_writes_only=true`（只挡 planner 写入，
    绝不阻塞 Executor）；`auto_merge=auto_rebase=force_push=auto_fetch_or_pull=false`；
  - `last_reviewed_task` 指针落后**不**参与写队列 gate（属 §2.11 的 review backlog 事实，只在
    `drift.review_pointer_codes` 报告）；
  - 默认零写入；`--output` 先拒绝 `<root>/.ai/**`（`runtime` 除外）的**任何**目标、再复用受控
    输出守卫（只允许 `.ai/runtime/**` 或系统临时目录）—— 覆盖「仓库恰好位于系统临时目录」时共享
    守卫会放行 `.ai/GPT_REVIEW_LEDGER.json` 的缺口；
  - 退出码 `0` / `2`（fail-closed）/ `3`（`PROJECT_STATE` 不可读）/ `4`（`--output` 被拒）。
- 新增 `tests/unit/test_ai_orchestrator_planner_mutation_precondition.py`（**24 项**）：事实包完整性
  （HEAD / 队列 / state blob+digest / 目录 digest / refill digest / backlog 指针 / precondition
  digest 均由测试独立用 `hashlib` 复算）、digest 幂等且排除 wall-clock、digest 对 results / tasks /
  backlog 内容敏感、`STALE_REMOTE_HEAD`、短 SHA 前缀匹配、非法 expected、HEAD 不可解析、
  `current_task` 已终态 / `last_completed` 落后 / 超前 / branch 漂移、`PROJECT_STATE` 不可读
  （退出码 3）、backlog 来源不可用、**并发回归**（Executor push 后旧 precondition 失效、忘记传
  expected 也会被 state 漂移挡住、重读最新 HEAD + 刷新 state 后可重新生成有效事实包）、review 指针
  落后不阻塞、authority 全 False、源码守卫（无写入 / 子进程 / Git 写路径，唯一写操作经受控守卫）、
  CLI fail-closed / 受控输出 / 拒绝任何 planner 路径。
- 新增 `tests/integration/test_planner_mutation_precondition_regression.py`（**5 项**）：真实仓库
  `observed_head_sha` / `branch` 由测试自己的 `git rev-parse` 复算、目录与 state digest 由测试自己的
  `hashlib` 复算、漂移 gate 与独立复算一致且每条 blocking code 都由非 review-pointer 事实支撑、
  stale 只改变 HEAD 相关事实、CLI 幂等 + ASCII + 前后零改写（tasks / results / state / ledger /
  `git status`）、Phase 3.3 blocker 与两条交易安全不变量不变。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.12（契约 / 稳定 code / 并发语义 / 退出码 / 职责边界）；
  `README.md` 新增「GPT 写队列前的远端 HEAD 并发保护事实包（GOLD-038）」小节。

### 3. 验证

- 新增测试：`tests/unit/test_ai_orchestrator_planner_mutation_precondition.py` 24 passed；
  `tests/integration/test_planner_mutation_precondition_regression.py` 5 passed；
- 真实仓库只读试跑（`--generated-at` 固定、`--expected-head-sha` 传入当前 HEAD）：
  `[precondition] head=b0a5d83c expected=b0a5d83c stale=False drift=True allowed=False
  codes=PROJECT_STATE_POINTER_BEHIND_RESULTS,QUEUE_DECLARATION_MISMATCH,STATE_RESULT_DRIFT`，
  退出码 `2` —— HEAD 一致，但 `PROJECT_STATE`（`current_task=GOLD-036` /
  `last_completed_task=GOLD-035` / `task_queue` 仍声明 GOLD-036 / 037）确实已落后于 results
  （GOLD-036 / GOLD-037 均 completed），所以工具**正确地**禁止 planner mutation 并要求 GPT 基于
  最新 HEAD 重读事实（不猜、不自动修复）；
- 全量门禁：`.venv\Scripts\python.exe -m pytest tests -q`、
  `.venv\Scripts\python.exe -m ruff check .`、
  `.venv\Scripts\python.exe -m mypy config database src scripts` 全绿；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`src/alpha/**`、`src/execution/**`、`database/**`、`.env`；
  未新增依赖、未改 `pyproject.toml`；测试写入全部发生在 `tmp_path`；Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- 本工具只**产事实**：它不会（也**不允许**）写 task / state / ledger，绝不自动 merge / rebase /
  force push；真正的并发保护动作仍是 GPT 的「重新读取最新 HEAD → 重建事实包 → 再写队列」；
- 真实仓库当前 `planner_mutation.allowed=false`：GPT 如需继续推进，应先基于最新 HEAD / results 重读
  事实并修正 `PROJECT_STATE` 指针与 `task_queue` 声明，然后重新取得有效 precondition；
- 建议下一步（由 GPT 决定）：按 GOLD-039 建立 result 顶层状态与 attempt 终态一致性门禁。

## GOLD-039：result 顶层状态与 attempt 终态一致性门禁 + 历史矛盾 fail-closed 暴露

### 1. 背景 / 问题

- §2.2（GOLD-020）已规定 Orchestrator 判定与 Cline raw finish reason 必须分开保存，但历史
  result（GOLD-020 之前旧版本进程写入）只有**唯一**的 `finish_reason`，里面是 raw Cline 值：
  GOLD-035 的 result 就是 `status=completed` + 最终 attempt `finish_reason=aborted`；
- GPT 因此拒绝为 GOLD-035 写正式 Review 台账（**不猜测、不静默归一化、不回写历史**），
  正式台账停在 GOLD-027；PROJECT_STATE 把这条事实写在 PHASE3_3_DATA blocker 里；
- 目标：① 未来**不可能**再生成自相矛盾的终态 result；② 历史矛盾被 review tooling
  **fail-closed 暴露**（稳定 reason code + `facts_complete=false`），而不是被自动改写；
  ③ 不改变业务资格、不改变 Planner 权限、不破坏台账 / 滚动队列 / Git 恢复行为。

### 2. 变更（最小范围）

- 新增 `orchestrator/result_terminal_consistency.py`（纯函数、纯标准库；零网络 / 零数据库 /
  零外部进程 / 零 wall-clock），契约 `schema=gold-ai/result-terminal-consistency/v1`：
  - **终态语义表**：`completed` 只能对应成功语义（最终 attempt `completed`、
    `cline_exit_code==0`、未超时、`failure_class=none`、已记录 validation 全通过）；
    `blocked` 允许最终 attempt `blocked` / `failed`（retry-exhausted，需失败证据）/
    `waiting_external`（provider fatal，需 non-retryable 证据）；历史 V1 顶层 `failed`
    只允许最终 attempt `failed`；
  - **fail-closed 稳定 code**：raw 值落进归一化键、`completed`+`aborted`/`failed`、
    `blocked`+最终 attempt `completed`、status 与 outcome 不一致、outcome 与
    normalized finish reason 不一致、未知 status / 未知 outcome / 结构非法 /
    失败语义缺证据 …… 全部给出稳定 reason code（绝不猜测、绝不静默归一化）；
  - **历史格式只读兼容**：顶层与最终 attempt 都没有归一化字段 ⇒ 唯一 `finish_reason` 是
    raw Cline 值，`status=completed` 而 raw 不是 `completed`（或反向）⇒
    `RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS`，只报告、绝不重写；
  - **没有事实就不做组合判定**：`attempts` 缺失 / 空 / 只有 V1 迁移空壳 attempt 时只校验顶层，
    既不猜测成功也不凭空指控（合成 fixture 与迁移路径兼容）；
  - `authority` 段机器可读声明只读（`tool_can_sign_review=false` /
    `tool_can_repair_result=false` / `tool_can_advance_state=false` / `writes_*=false`）。
- `orchestrator/ai_orchestrator.py`：新增 **写入前门禁** `ensure_result_terminal_consistency()`
  + `ResultTerminalConsistencyError`；`write_final_result()` 在 `atomic_write_json` 之前调用
  同一份规则（经 `repo_scoped_import` 复用，**不复制第二套词表 / 组合表**）：
  矛盾 / 未知组合一律 raise，**矛盾 result 绝不落盘**；校验器不可用同样 fail-closed。
- `orchestrator/review_binding.py`：`load_result_facts()` 追加 `terminal_consistency_issues()`
  ⇒ 矛盾 result 得到稳定 code ⇒ `binding.facts_complete=false`；`review_backlog` 与
  `review_ledger_integrity` 沿用既有事实链自动 fail-closed（无需复制算法，也不改 ledger schema）。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.13（契约 / 自洽语义 / 稳定 code / 写入前门禁 /
  review 暴露 / 不改动的边界）。

### 3. 验证

- 新增 `tests/unit/test_result_terminal_consistency.py`（28 项）与
  `tests/integration/test_result_terminal_consistency_regression.py`（7 项）；
- 真实语料核对：GOLD-001-R2 / 002 / 003 / 005 / 016 / 020 / 021 / 023 / 028 / 031 / 035 / 038
  被稳定 code 暴露，其中 **GOLD-035 就是本任务的直接验收对象**；测试用**自己的**
  hashlib / 协议口径独立复算，逐份与模块判定一致；
- 正式台账（GOLD-025~027）保持 `facts_complete=true`，`review_ledger_integrity` 仍退出码
  `0` / `integrity_ok=True`（新门禁不破坏既有台账链）；
- 既有 `test_review_binding_regression.py` / `test_review_backlog_regression.py` 按新语义更新
  （历史矛盾 ⇒ `facts_complete=false` / `review_status=invalid` / 退出码 2），
  GOLD-035 由 review 入口 fail-closed 暴露；
- 回归命令：`tests/unit/test_result_terminal_consistency.py` +
  `tests/integration/test_result_terminal_consistency_regression.py` +
  review binding / backlog / ledger integrity 回归共 **63 passed**（35s）；
- 全量门禁：`.venv\Scripts\python.exe -m pytest tests -q`、
  `.venv\Scripts\python.exe -m ruff check .`、
  `.venv\Scripts\python.exe -m mypy config database src scripts`；
- 未修改 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`src/alpha/**`、`src/execution/**`、`database/**`、`.env`；
  未新增依赖；测试写入全部落在 `tmp_path`；Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- 历史矛盾 result **保持原样**（只读）：GPT 何时、以何种方式为 GOLD-028~038 记录正式 Review
  仍需 Planner 决策；本任务只保证「矛盾被显式看到」而不是「被自动洗白」；
- 归一化写入路径从此有写入前门禁，若未来出现别的写 result 路径，同样必须经过
  `orchestrator.result_terminal_consistency`（唯一规则来源）；
- 建议下一步（由 GPT 决定）：基于 §2.13 的稳定 code 决定 GOLD-035 这类历史矛盾的处理口径，
  或继续补 control-plane 事实包。


## GOLD-040：GitHub CI 全历史绑定与跨平台控制面回归修复（不做业务判定变更）

### 1. 背景 / 问题

- GOLD-039 之后 GitHub CI run 35869237769 在 Python 3.12 / 3.13 上稳定失败 **17 项控制面测试**。
  本地 Windows 全绿，因此根因是**环境差异**而不是业务判定：
  1. `actions/checkout@v4` 默认 `fetch-depth: 1`（浅克隆）⇒ `git log` 只含 1 个提交，
     `orchestrator.review_binding` / `review_ledger_integrity` / `review_backlog` 无法重算
     GOLD-025~027 的历史完成 commit 绑定（12 项）；
  2. `.ai/runtime` 永不进 Git（`.gitignore`）⇒ 干净 checkout 里默认 runtime 镜像 / 输出路径
     的父目录不存在，被受控输出守卫 fail-closed 拒绝（1 项）；
  3. POSIX 上 `running_process_image()` 直接返回 `None`（"仅 Windows 可查"），而
     `is_pid_running()` 在 POSIX 分支另走 `os.kill` ⇒ 一旦调用方用该函数判定「锁持有进程已死亡」，
     Linux / CI 上会把**仍存活**的锁当成陈旧锁（1 项）；
  4. `tests/integration/test_planner_mutation_precondition_regression.py` 仍按旧假设断言
     「有 issue ⇒ 退出码必须是 2/3」，与 GOLD-038 的「review 指针落后只报告、不 gate」冲突
     （1 项，本地同样失败）。
- 目标：让**本地与 CI 对同一 Git 历史 / runtime 输出路径 / PID 存活探测 / terminal-consistency 事实
  得到一致结果**；不放宽任何 fail-closed 判定，不改变 Phase 3.3 数据资格、Planner / Executor 边界
  与交易安全开关。

### 2. 变更（最小范围）

- `.github/workflows/ci.yml`：quality（py3.12 / 3.13）与 postgres 两个 job 的
  `actions/checkout@v4` 显式 `fetch-depth: 0`（带原因注释）——控制面必须能追溯完整可达历史；
  **`COMPLETION_COMMIT_NOT_FOUND` 语义保持不变**（浅历史依旧 fail-closed）。
- `orchestrator/review_binding.py`：
  - 新增只读探测 `git_history_is_shallow()`（读 `<gitdir>/shallow`，复用
    `planner_snapshot.resolve_git_dir`，零子进程、零网络）与稳定 code
    `GIT_HISTORY_SHALLOW`：**仅当**完成 commit 找不到且仓库是浅克隆时追加该诊断
    （`commit.reason_code` 仍是 `COMPLETION_COMMIT_NOT_FOUND`，退出码仍是 `2`），
    使「历史被截断」与「任务从未完成」不再被混为一谈；
  - `review_backlog` / `review_ledger_integrity` 沿用既有事实链自动获得同一诊断，无复制算法。
- `orchestrator/planner_refill_request.py` + `orchestrator/ai_orchestrator.py`：
  新增**唯一口径**的无 head 表示 `QUEUE_HEAD_ABSENT = "-"` 与 `queue_head_token()`；
  GOLD-034 CLI stderr、Orchestrator 提示行、提示签名（节流签名）全部改为同一 token，
  消除 `head=None` / `head=-` 的入口漂移（`ai_orchestrator` 侧复用只读模块的规则，
  只读模块不可用时退化为同一常量）。
- `orchestrator/planner_snapshot_output.py`：新增 `runtime_output_root()` /
  `prepare_output_parent()`：**只对** `<root>/.ai/runtime/**` 做确定性父目录准备
  （parents-only + `exist_ok=True`），其它位置（系统临时目录 / `.ai/tasks` / `.ai/results` /
  `.ai/PROJECT_STATE.json` / `src` / `database` …）依旧 fail-closed 且**零创建**；
  `guard_summary()` 机器可读声明 `creates_directories=true` + `creates_directories_scope=.ai/runtime`。
- `orchestrator/ai_orchestrator.py`（PID 存活探测）：拆出 `windows_process_image()`（`tasklist`）与
  `posix_process_image()`（`os.kill(pid, 0)` + `/proc/<pid>/comm`），`running_process_image()` 按
  `os.name` 分派，`is_pid_running()` 统一走同一三态契约：**存在 ⇒ 映像名 / 确实不存在 ⇒ `None` /
  探测失败 ⇒ `OSError` 且调用方按「存活」处理**（POSIX 上权限不足只说明查不到，绝不等于不存在）。
- 测试：新增浅历史诊断（4 项）、无 head token 单一口径、runtime 目录准备与「runtime 之外零创建」、
  POSIX 探活三态与平台分派（5 项）；更新 `planner_snapshot` / `refill_request` 的输出守卫断言到新契约；
  修正 `test_planner_mutation_precondition_regression.py` 的退出码断言按 GOLD-038 语义（review 指针
  落后只报告、不 gate、绝不进 `blocking_reason_codes`）。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.14（CI 历史契约 / 无 head token / runtime 目录准备 /
  跨平台 PID 三态 / 不改动的边界）。

### 3. 验证

- 浅克隆复现：`git clone --depth 1` 沙箱（无 `.ai/runtime`）在修复前稳定复现 12 项历史绑定失败 +
  runtime 失败；修复后同一沙箱对 GOLD-028 输出的 `reason_codes` 为
  `['COMPLETION_COMMIT_NOT_FOUND', 'GIT_HISTORY_SHALLOW', ...]`、退出码仍为 `2`（**明确 fail-closed，
  不是伪通过**）；
- 干净全历史 checkout（全量 `git clone` + 无 `.ai/runtime`）沙箱在修复后：review binding / backlog /
  ledger integrity / refill hint / result terminal consistency / planner mutation precondition
  全绿（含 `test_default_mirror_target_is_runtime_guard_approved` 与
  `test_cli_is_idempotent_ascii_and_zero_write`），且该次运行**新建** `.ai/runtime` 而未改动任何
  受版本控制的 `.ai/**` 文件；
- 全量门禁：`.venv\Scripts\python.exe -m pytest tests -q`、`ruff check .`、
  `mypy config database src scripts`；
- 未修改 `.ai/tasks/**` / `.ai/results/**` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`、
  `src/alpha/**`、`src/execution/**`、`database/**`、`data/**`；未新增依赖；Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- GOLD-035 / GOLD-039 的 `completed` + 最终 attempt `raw aborted` **历史矛盾保持原样**（只读）：
  仍由 §2.13 的稳定 code 客观暴露，不由本任务改写；
- CI 历史契约从此由 `fetch-depth: 0` 保证；若未来需要浅克隆（性能），必须先让控制面显式消费
  `GIT_HISTORY_SHALLOW` 并停线，绝不静默重算；
- 建议下一步（由 GPT 决定）：CI 转绿后 review GOLD-036~040，再决定是否解除 `PHASE3_3_DATA` 相关阻塞。

- 建议下一步（由 GPT 决定）：CI 转绿后 review GOLD-036~040，再决定是否解除 `PHASE3_3_DATA` 相关阻塞。

## GOLD-041：planner mutation 门禁集合一致性（`STATE_RESULT_DRIFT` 单一事实源）

### 1. 背景 / 问题

- GOLD-040 之后 GitHub Actions（Python 3.12 / 3.13，完整可达历史）稳定失败于
  `tests/integration/test_planner_mutation_precondition_regression.py::test_cli_is_idempotent_ascii_and_zero_write`；
- **根因**（本地同仓库状态即可复现）：`planner_mutation.blocking_reason_codes` 里的聚合标记
  `STATE_RESULT_DRIFT` 是 `blocking_reason_codes()` 内部**临时 `codes.add(...)`** 出来的，
  payload 的 `issues` 里**没有对应事实** ⇒ 同一门禁事实在 issues / blocking 两个集合里表示
  不一致（测试按 `issues` 的 gating ERROR 集合复算时必然发现「blocking 多出
  `STATE_RESULT_DRIFT`」）；
- 另外 `blocking_reason_codes` 由「各来源 code 直接并集」组装、`issues` 另行组装，
  review 指针滞后（`last_reviewed_task`）与执行指针漂移共用**同一**稳定 code
  `PROJECT_STATE_POINTER_BEHIND_RESULTS`，使「按 code 集合比较」的断言在两者并存时自相矛盾
  （把「review 滞后不 gate」误写成「该 code 不得出现在 blocking」）。

### 2. 变更（最小范围）

- `orchestrator/planner_mutation_precondition.py`：
  - 新增单一事实源 `gating_error_issues()`（`error` 且非 review 指针 fact）/
    `state_result_drift_codes()` / `state_result_drift_issues()`：聚合标记 `STATE_RESULT_DRIFT`
    **先成为 `issues` 里的真实 `error` issue**（detail 列出底层 code），再由
    `blocking_reason_codes(issues)` 从 issue 集合**投影** ⇒ 两者恒等；
  - `blocking_reason_codes()` 只做集合投影（不再自行 `codes.add(STATE_RESULT_DRIFT)`），
    并只认 `error` 级别（与 `planner_snapshot` 退出码口径一致；`warning` 只报告、不 gate）；
  - `drift` 新增机器可读 `aggregate_code`；`OWN_BLOCKING_REASON_CODES` 目录补入该聚合 code；
  - **fail-closed 不变**：不删除 / 不降级 `STATE_RESULT_DRIFT`，不改 GOLD-038 的
    「review 指针滞后只报告、不 gate」语义，不改任何其它 blocker 分类。
- `tests/integration/test_planner_mutation_precondition_regression.py`：两处「按 code 集合比较」
  断言改为**按 issue 来源**核对 + `blocking_reason_codes == issues 的 gating ERROR 集合` 恒等
  断言（真实仓库上正好覆盖「执行指针漂移 + review 指针滞后并存」场景），不再有任何按 code
  集合的伪否定。
- `tests/unit/test_ai_orchestrator_planner_mutation_precondition.py`（24 → 26 项）：新增
  「真实 `STATE_RESULT_DRIFT` 必须是真实 ERROR issue 且与 blocking 恒等」与
  「真漂移 + review 滞后并存一致」两项；并扩展现有「仅 review 滞后」用例（无聚合标记、
  blocking 为空、exit 0）。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` §2.12 与 `README.md`（工具段 + 约束速查表）同步
  单一事实源口径与测试计数；历史 GOLD-038 日志条目不改写。

### 3. 验证

- 复现：修复前 `pytest tests/integration/test_planner_mutation_precondition_regression.py -q`
  稳定 `1 failed`（`Extra items in the left set: 'STATE_RESULT_DRIFT'`）；
- 修复后：`pytest tests/unit/test_ai_orchestrator_planner_mutation_precondition.py -q`
  → 26 passed；真实仓库 CLI 事实包中 `STATE_RESULT_DRIFT` 已是 `issues` 里的 `error` issue，
  `blocking_reason_codes` 与 `issues` 的 gating ERROR 集合逐项相等，退出码仍为 `2`；
- 全量门禁：`pytest tests -q`、`ruff check .`、`mypy config database src scripts`（全部通过）；
- 未修改 `.ai/tasks/**` / `.ai/results/**` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json`、`.github/workflows/**`、`src/alpha/**`、`src/execution/**`、
  `database/**`、`data/**`；未新增依赖；Cline 未执行任何 Git 写操作。

### 4. 遗留 / 下一步

- GOLD-035 / GOLD-039 历史 `completed` + raw `aborted` 矛盾、Phase 3.3 blocker、L3/L4、
  GPT 独占规划权与交易安全开关**全部未变**；
- 真实仓库当前仍处于「`PROJECT_STATE` 执行指针落后于 results」的真实漂移（只报告、绝不自动
  改写），`STATE_RESULT_DRIFT` 继续 fail-closed（exit `2`）；
- 建议下一步（由 GPT 决定）：CI 转绿后 review GOLD-036~041，再决定是否解除
  `PHASE3_3_DATA` 相关阻塞。

## GOLD-042：历史 Result 矛盾的 GPT 专属裁决契约（只读 validator + CLI）

### 1. 背景 / 问题

- §2.13（GOLD-039）把历史矛盾显式暴露（`GOLD-028 / 031 / 035 / 038 / 039 / 040 / 041`：
  顶层 `status=completed` + 最终 attempt raw `finish_reason=aborted`），
  `review_binding.facts_complete=false`、正式 Review 台账停在 GOLD-027；
- 但「不猜测、不静默归一化、不回写历史」并不等于「永远无法恢复」：GPT 需要一条
  **独立、可加密绑定、仅 GPT 可签发**的裁决契约，使 review closure 有合法路径，
  **同时原 result 逐字节不变**；
- 关键风险：Executor（Cline / DeepSeek）一旦能「代填」裁决，GPT-only Review 即名存实亡。

### 2. 变更（最小范围）

- 新增 `orchestrator/legacy_result_adjudication.py`（**只读、纯函数、零外部进程**）：
  - 版本化契约 `gold-ai/legacy-result-adjudication/v1` + store
    `gold-ai/legacy-result-adjudication-store/v1`；裁决记录位于
    `.ai/adjudications/legacy_result_adjudications.json`，与 `.ai/results` **物理分离**；
  - 每条裁决绑定：`adjudication_id` / `task_id` / `adjudication_type`（唯一合法值
    `legacy_terminal_contradiction`）/ `reviewer` + `reviewer_role=GPT` / `reason_summary` /
    `adjudicated_at`（带时区）/ 可选 `expires_at` / `bound_result{sha256,status}` /
    `bound_commit{sha,branch}` / `contradiction_reason_codes`；
  - fail-closed 稳定 reason code：缺失 store / store schema 不支持 / 越权
    （`ADJUDICATION_EXECUTOR_FORBIDDEN`）/ 未知身份（`..._NOT_AUTHORIZED`）/ reviewer_role
    非 GPT / 重复（`ADJUDICATION_TASK_ID_DUPLICATE` / `ADJUDICATION_ID_DUPLICATE`）/
    冲突（`ADJUDICATION_TASK_ID_CONFLICT`）/ 过期（`ADJUDICATION_EXPIRED` /
    `..._EXPIRY_UNVERIFIABLE`）/ 身份漂移（result sha256、status、commit sha、branch、
    contradiction codes）/ live facts 不完整 / 对自洽 result 误签裁决；
  - **原始矛盾始终可见**：无论裁决是否有效，报告都原样回显由 `result_terminal_consistency`
    （唯一规则来源）算出的 legacy contradiction reason code，绝不删除 / 降级 / 伪装；
  - `authority` / `contract` 机器可读声明：`writes_*=false`、`tool_can_sign_adjudication=false`、
    `tool_can_sign_review=false`、`executor_can_adjudicate=false`、
    `adjudication_implies_verdict=false`、`adjudication_advances_review_pointer=false`、
    `adjudication_lifts_phase3_3_blocker=false`；
  - CLI `python -m orchestrator.legacy_result_adjudication [--task ...] [--as-of ...]`
    只读输出 ASCII JSON（`0` 全部有效 / `2` fail-closed）；live 身份复用 §2.8
    `review_binding` 的 canonical result sha256 / completion commit 口径，**不另造身份算法**。
- 新增 `tests/unit/test_legacy_result_adjudication.py`（**48 项**）；
- 新增 `tests/integration/test_legacy_result_adjudication_regression.py`（**11 项**）；
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.15；`README.md` 工具段 + 约束速查表同步。

### 3. 验证

- `pytest tests/unit/test_legacy_result_adjudication.py -q` → 48 passed；
- `pytest tests/integration/test_legacy_result_adjudication_regression.py -q` → 11 passed
  （7 个真实矛盾 result 的身份在测试里用 raw `git ls-tree` + `cat-file blob` + `hashlib`
  **独立复算**；`.ai/results` 全树摘要、`PROJECT_STATE`、`GPT_REVIEW_LEDGER` 前后一致；
  裁决 store 目录 / 文件**不存在**，即 Executor 未创建任何真实 GPT 裁决）；
- 真实仓库 CLI：`python -m orchestrator.legacy_result_adjudication` → 7 项全部
  `unadjudicated` + `ADJUDICATION_STORE_MISSING`，退出码 `2`（fail-closed 而非伪通过）；
- 全量门禁：`pytest tests -q`、`ruff check .`、`mypy config database src scripts`（全部通过）。

### 4. 范围守规

- 未创建真实 GPT 裁决、未写 `.ai/adjudications/**`、未写 `.ai/GPT_REVIEW_LEDGER.json`、
  未推进 `last_reviewed_task`、未改 `.ai/PROJECT_STATE.json`；未修改
  `.ai/tasks/**`、`.ai/results/**`、`src/alpha/**`、`src/execution/**`、`database/**`、
  `data/**`；未新增依赖；Cline 未执行任何 Git 写操作。

### 5. 遗留 / 下一步

- 本层只提供**契约与事实**：有效裁决**不等于** Review PASS，不产生 verdict / `acceptance_summary`、
  不自动写台账、不自动推进指针、不解除 `PHASE3_3_DATA`；
- 建议下一步（由 GPT 决定）：按 §2.15 用真实 `result.sha256` + `completion commit sha/branch` +
  原始 contradiction codes 手写（或由 GPT 工具生成）裁决记录，再由 GPT 做实质 review；
  历史矛盾在裁决无效 / 缺失时继续 fail-closed。
## GOLD-043：历史矛盾裁决的确定性证据包（只读 manifest + CLI）

### 1. 背景 / 问题

- §2.13（GOLD-039）把历史矛盾 fail-closed 暴露、§2.15（GOLD-042）给出 GPT-only 裁决契约，
  但 GPT 仍要逐项手跑 `review_binding` / `legacy_result_adjudication` / `review_backlog`
  才能看清 `last_reviewed_task` 之后的完整 backlog 里每项到底「可直接实质 review / 必须先裁决 /
  事实不齐」；缺少**单一确定性证据视图**，也无法一次性证明每项身份可独立复算；
- 关键风险：任何把「测试通过 / `exit_code=0`」当成 GPT PASS 的口径，都会让 GPT-only Review
  名存实亡；证据工具必须只产事实、绝不产 verdict。

### 2. 变更（最小范围）

- 新增 `orchestrator/review_evidence_manifest.py`（**只读、纯标准库、零外部进程**）：
  - 版本化契约 `gold-ai/review-evidence-manifest/v1` + `schema_version=1`；CLI
    `python -m orchestrator.review_evidence_manifest`，默认 stdout 只读，显式 `--output`
    复用 §2.6 fail-closed 路径守卫（只允许 `<root>/.ai/runtime/**` 或系统临时目录）；
  - **唯一口径复用**：result / commit / validation 事实来自 §2.8 `review_binding`（只读 Git
    白名单）；终态矛盾来自 §2.13 `result_terminal_consistency`；裁决状态来自 §2.15
    `legacy_result_adjudication`；backlog 边界与 ledger identity 来自 §2.11 `review_backlog`
    + §2.9 `review_ledger_integrity`（**不存在第二套身份算法**）；
  - 逐项输出：`task` / `result` canonical sha256、completion
    `commit.{sha,branch,subject,committed_at}` + `changed_paths`（只读
    `git log -1 --name-only`）、`validation.commands/return_codes`、
    `terminal_consistency.{contradiction,format,reason_codes,details}`、
    `ledger.{entry_present,entry_count,entry_valid,identity_consistent,status}`、
    `adjudication.{state,valid,adjudication,reason_codes}` 与稳定 `reason_codes`；
  - 四态 `items[].classification`：`facts-ready` / `needs-gpt-adjudication` /
    `pending-substantive-review` / `invalid`；只有 §2.15 可恢复的 **legacy** 矛盾才进裁决路径，
    归一化（非 legacy）矛盾、缺 commit、hash 漂移、事实不齐、ledger 非法、裁决冲突一律
    `invalid`（fail-closed）；
  - `facts_digest` 排除 wall-clock（`generated_at` / `facts_digest` / `determinism`），
    相同仓库事实 ⇒ 相同 digest；`authority` / `contract` 机器可读声明
    `review_authority=gpt_only`、`tool_can_sign_review=false`、
    `tool_can_sign_adjudication=false`、`tool_can_advance_review_pointer=false`、
    `writes_*=false`、`network_access=false`、`model_calls=false`、
    `exit_code_zero_is_not_review_pass=true`；
  - 退出码：`0` 无 fail-closed 项（**绝不是** GPT PASS）/ `2` fail-closed 或存在待裁决矛盾 /
    `3` `PROJECT_STATE` 或 ledger 不可用 / `4` `--output` 被拒（绝不写文件）。
- 新增 `tests/unit/test_review_evidence_manifest.py`（**25 项**：authority / contract /
  determinism 只读边界、源码守卫、四态分类与 fail-closed、受控 `--output`、CLI ASCII 与
  确定性）；
- 新增 `tests/integration/test_review_evidence_manifest_regression.py`（**19 项**：真实 backlog
  与 §2.11 逐项一致、7 个已知矛盾为 `needs-gpt-adjudication`、逐项身份由 raw `git` blob +
  `diff-tree` **独立复算**、`facts_digest` 与 wall-clock 无关、`.ai/results` 全树 /
  `PROJECT_STATE` / `GPT_REVIEW_LEDGER` / 裁决 store 前后字节一致、§2.8/§2.13 fail-closed
  暴露未弱化、Phase 3.3 与交易安全不变量不变）；
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.16；`README.md` 工具段同步。

### 3. 验证

- `pytest tests/unit/test_review_evidence_manifest.py -q` → 25 passed；
- `pytest tests/integration/test_review_evidence_manifest_regression.py -q` → 19 passed；
- `pytest tests -q` → **3712 passed, 1 skipped in 535.20s**；`ruff check .` → **All checks
  passed**；`mypy config database src scripts` → **Success: no issues found in 183 source files**；
- 真实仓库当前事实：backlog 17 项（TEST-001/002 + GOLD-028~042，与 §2.11 完全一致），
  `needs-gpt-adjudication=7`（= `KNOWN_CONTRADICTION_TASKS`）、`pending-substantive-review=10`、
  `invalid=0`、`facts-ready=0`、`adjudicated=0`、`exit_code=2`；裁决 store 仍不存在
  （Executor 未创建真实裁决），`last_reviewed_task` 仍为 `GOLD-027`。

### 4. 遗留 / 建议下一步（由 GPT 决定）

- 本任务**只产证据**：真正的历史矛盾裁决、随后的实质 review、`GPT_REVIEW_LEDGER` 追加与
  `last_reviewed_task` 推进仍必须由 GPT 完成（GOLD-044 将把有效裁决接入 review binding /
  integrity，GOLD-045 提供写入前原子预检）；
- `PHASE3_3_DATA` 保持 BLOCKED；未进入 Phase 3.4，未跨 L3/L4；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 不变。


## GOLD-044：有效 GPT 裁决接入 Review Binding 与连续性门禁

### 1. 背景 / 问题

- §2.15（GOLD-042）给出历史矛盾的 GPT-only 裁决契约、§2.16（GOLD-043）给出单一证据包，
  但 review 层（§2.8 binding / §2.9 ledger integrity / §2.11 backlog / refill 诊断 /
  §2.12 precondition）仍把历史矛盾一律视为 `facts_complete=false` ⇒ 即使 GPT 已裁决，
  该 backlog 项也永远无法进入「可实质 review」；反之，若放宽为「有 store 条目就算通过」，
  越权 / 漂移 / 冲突裁决会被静默放行；
- 关键风险：把「裁决」混淆成「Review verdict」会绕过 GPT-only Review 与连续性门禁。

### 2. 变更（最小范围）

- `orchestrator/legacy_result_adjudication.py`：新增 `collect_store_index()` —— review 层
  共用的**唯一** store 读取入口（只读 + 按 task 归集 + 一致性索引）；§2.16
  `review_evidence_manifest.collect_adjudication_store` 改为复用它（单一来源，零重复口径）。
- `orchestrator/review_binding.py`（§2.8）：
  - manifest 新增 `adjudication` 段（`store_present` / `state` / `valid` / `adjudicated` /
    `adjudication` / `reason_codes` / `contract_source`）与
    `binding.{facts_ready,adjudicated,facts_ready_source}`；
  - 默认行为**不变**（无裁决 ⇒ `facts_complete=false` / `facts_ready=false` / exit `2`）；
    只有 schema 合法、GPT 权威有效、原 result sha256/status + commit sha/branch + 原始
    contradiction codes 完全匹配、非重复 / 冲突 / 过期 / 漂移且无其它阻塞 code 的裁决才
    `adjudicated=true` / `facts_ready=true` / exit `0`；
  - 原始 contradiction finding、原 result sha256、裁决 identity **原样保留**；越权 / 未知身份
    / 四类身份漂移 / 重复 / 冲突 / store 不可读 / schema 不支持等 §2.15 稳定 code 原样并入
    `binding.reason_codes`（fail-closed）；CLI 新增只读 `--adjudication-store`。
- `orchestrator/review_backlog.py`（§2.11）：逐项新增 `facts_ready` / `adjudicated` /
  `facts_ready_source` / `adjudication`，新增 `BACKLOG_ITEM_ADJUDICATION_INVALID` 与 summary
  `facts_ready_count` / `adjudicated_count` / `adjudicated_pending_count` /
  `unresolved_contradiction_count`；`review_status` 词表（`pending` / `bound` / `invalid`）
  不变，用 `adjudicated` 稳定区分「未裁决矛盾 / 已裁决待实质 review / 已绑定 / 漂移」。
- `orchestrator/review_ledger_integrity.py`（§2.9）：台账条目按 `facts_ready` 核对客观身份，
  `bindings[].{manifest_facts_ready,manifest_adjudicated,manifest_adjudication_state}` 与
  `ledger.adjudicated_entry_count` 只报告事实；五类身份漂移检测**未放宽**。
- `orchestrator/planner_mutation_precondition.py`（§2.12）：把同一裁决计数透传到
  `review_backlog` / `summary`（`adjudicated_count` / `adjudicated_pending_count` /
  `facts_ready_count` / `unresolved_contradiction_count` / `review_*_count`），
  绝不改变 `planner_mutation.allowed` 或 blocking 语义。
- `orchestrator/planner_refill_request.py`（§2.10 refill 诊断）：
  `completed_but_unreviewed.adjudication` 复用同一 store 读取入口报告 **presence** 事实
  （`declared_implies_valid=false`、`validation_source=orchestrator.review_binding`），
  `summary.adjudication_declared_count` 同源；presence ≠ 有效，绝不改任何门禁。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.17；`README.md` 工具段 + 红线表同步。

### 3. 验证

- `pytest tests/unit/test_review_closure_adjudication.py -q` → 32 passed；
- `pytest tests/integration/test_review_closure_adjudication_regression.py -q` → 10 passed；
- `pytest tests -q` / `ruff check .` / `mypy config database src scripts` → 见本次收尾结果；
- 真实仓库当前事实：没有任何真实裁决 store（Executor 未创建），`GOLD-035` 等 7 个已知矛盾
  仍 `facts_ready=false` / `needs-gpt-adjudication` / exit `2`；临时演习 store 可让
  `review_binding` 在**不改写历史**的前提下恢复 `facts_ready=true`，但
  `last_reviewed_task` 仍为 `GOLD-027`，ledger 连续性仍以 `LEDGER_CHAIN_GAP` fail-closed。

### 4. 遗留 / 建议下一步（由 GPT 决定）

- 真正的历史矛盾裁决、随后的实质 review、`GPT_REVIEW_LEDGER` 追加与 `last_reviewed_task`
  推进仍**只能由 GPT** 完成（GOLD-045 提供写入前原子预检）；
- `PHASE3_3_DATA` 保持 BLOCKED；未进入 Phase 3.4，未跨 L3/L4；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 不变。


## GOLD-045 —— GPT Review 收口写入前的原子一致性预检

### 1. 完成内容

- 新增只读模块 `orchestrator/review_closure_precondition.py`（`§2.18`）：在 GPT 真正写入历史
  裁决 / `GPT_REVIEW_LEDGER` / `PROJECT_STATE` **之前**，用**单一**只读预检把候选写集与当前
  已提交事实一次性比对，任何 stale / 不连续 / 越权 / 安全不变量变化一律 fail-closed。
- **复用，不复制**：HEAD / 指针复用 §2.5 `planner_snapshot`（经 planner snapshot 的 `git` 段）；
  result / commit 身份复用 §2.8 `review_binding`；ledger 校验与连续性复用 §2.8 `review_ledger`
  + §2.9 `review_ledger_integrity`；裁决校验复用 §2.15 `legacy_result_adjudication`
  （`store_entries` / `store_consistency_issues` / `evaluate_task`）；backlog 事实复用 §2.11
  `review_backlog`；确定性摘要与受控输出守卫复用 §2.12 `planner_mutation_precondition` +
  §2.6 `planner_snapshot_output`。本模块自身判定算法为零。
- **候选契约**：`schema=gold-ai/review-closure-candidate/v1` + `schema_version=1`，三段可选
  （`adjudication_store` / `review_ledger` / `project_state`）+ 必需 `base`
  （`head_sha` / `results_digest` / `tasks_digest` / `project_state_sha256` /
  `review_ledger_sha256` / `adjudication_store_sha256`）。候选**只允许**来自显式临时文件
  （系统临时目录 / `<root>/.ai/runtime/**`）或 stdin；managed 状态路径（`.ai/PROJECT_STATE.json`
  / `.ai/GPT_REVIEW_LEDGER.json` / `.ai/tasks` / `.ai/results` / `.ai/adjudications/**`）一律拒绝。
- **fail-closed 门禁**：`CANDIDATE_MISSING` / `..._UNREADABLE` / `..._JSON_INVALID` /
  `..._NOT_OBJECT` / `..._SCHEMA_UNSUPPORTED` / `..._SECTION_INVALID` / `..._BASE_MISSING` /
  `..._BASE_INVALID`、`STALE_REMOTE_HEAD`、五类 base 摘要漂移、`GIT_INFO_UNAVAILABLE`、
  `COMMITTED_FACTS_UNAVAILABLE`、§2.9 `LEDGER_CHAIN_GAP` / 顺序与 review 时间回退 / 五类身份
  mismatch / `LEDGER_MANIFEST_FACTS_INCOMPLETE`、`CANDIDATE_LEDGER_ENTRY_REMOVED`、
  `CANDIDATE_PASS_PAST_UNRESOLVED_CONTRADICTION`、`CANDIDATE_ADJUDICATION_NOT_GPT` /
  `CANDIDATE_ADJUDICATION_INVALID`（§2.15 稳定 code 原样透传）、
  `CANDIDATE_LAST_REVIEWED_REGRESSION` / `..._AHEAD` / `CANDIDATE_STATE_POINTER_DRIFT` /
  `CANDIDATE_PHASE3_3_BLOCKER_REMOVED` / `CANDIDATE_BLOCKER_REMOVED` /
  `CANDIDATE_PHASE3_4_ENTRY` / `CANDIDATE_TRADING_SAFETY_CHANGED`。
- **candidate-ready ≠ committed-current ≠ review 完成**：`closure` 段机器可读给出
  `candidate_ready` 与 `committed_current`（head / state / backlog / integrity 是否可读），并
  硬编码 `review_completed=false` / `review_verdict_issued=false` / `phase_gate_lifted=false` /
  `executor_write_triggered=false` / `state_advanced=false` / `auto_push=false`。
- **零写入 CLI**：`python -m orchestrator.review_closure_precondition --candidate <临时文件|->`
  （只读 `--root` / `--state` / `--tasks-dir` / `--results-dir` / `--ledger` /
  `--adjudication-store` / `--as-of` / `--generated-at`）；**没有** `--apply` / `--fix` /
  `--advance` / `--sign` 等变更开关；默认 stdout 纯 ASCII JSON，显式 `--output` 复用 §2.6
  守卫（只允许 `<root>/.ai/runtime/**` 或系统临时目录），退出码 `0` / `2` / `3` / `4`。
- **确定性**：`precondition_digest` 排除 `generated_at` / `as_of` / `precondition_digest` /
  `determinism`，相同候选 + 相同已提交事实 ⇒ 相同 digest（幂等）；Windows / Linux 路径由
  `Path.resolve()` + 允许根前缀判定统一处理。
- 文档：`.ai/DEVELOPMENT_PROTOCOL.md` 新增 §2.18；`README.md` 工具段 + 红线表同步
  （本文件新增本节）。

### 2. 修改 / 新增文件

- 新增：`orchestrator/review_closure_precondition.py`、`tests/unit/test_review_closure_precondition.py`、
  `tests/integration/test_review_closure_precondition_regression.py`；
- 修改：`.ai/DEVELOPMENT_PROTOCOL.md`（§2.18）、`README.md`（工具段 + 红线表）、
  `PROGRESS_LOG.md`（本节）。

### 3. 验证

- `pytest tests/unit/test_review_closure_precondition.py -q` → 43 passed；
- `pytest tests/integration/test_review_closure_precondition_regression.py -q` → 8 passed；
- `pytest tests -q` → **3807 passed / 1 skipped**（0 failed，605.54s）；
- `ruff check .` → All checks passed；`mypy config database src scripts` → Success（183 files）；
- 真实仓库当前事实：`PROJECT_STATE` 指针（`current_task=GOLD-042` / `last_completed_task=GOLD-041`）
  落后于已完成 results（`latest_terminal=GOLD-044`），因此**未对齐指针**的候选会被
  `CANDIDATE_STATE_POINTER_DRIFT` fail-closed；把指针对齐 results 的候选（
  `current_task=GOLD-045` / `last_completed_task=GOLD-044` / `last_reviewed_task=GOLD-027` +
  现有 ledger）⇒ `candidate_ready=true` / exit `0`，但 `review_completed=false`，
  `.ai` 全树与 worktree 前后字节一致。

### 4. 遗留 / 建议下一步（由 GPT 决定）

- 真正的历史矛盾裁决、随后的实质 review、`GPT_REVIEW_LEDGER` 追加与 `last_reviewed_task`
  推进仍**只能由 GPT** 完成（本项提供写入前原子预检，工具绝不代写 / 代签 / 代推进）；
- `PHASE3_3_DATA` 保持 BLOCKED；未进入 Phase 3.4，未跨 L3/L4；`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 不变。



