# 11_Phase2 真实语料验收指南

> 适用阶段：**Phase 2 验收（真实语料）**。前置结论：Mock 语料上的工程链路已验证通过
> （见 `docs/05` Phase 2 进度表与 `docs/experiments/Phase 2 基线对比报告.md`）；
> 本指南定义**真实作者帖子**的采集字段、质量自检、标注规范与端到端验收流程。
>
> 目标读者：数据采集人（你）+ 标注人（你/团队）+ 复评执行者（AI/工程师）。

---

## 1. 数据收集：CSV 列契约

### 1.1 硬性要求（缺一不可）

| 列名（推荐） | 接受别名 | 类型/格式 | 说明 |
|---|---|---|---|
| `id` | `post_id` | 字符串，**全局唯一且稳定** | 帖子唯一标识；建议 `real-<source>-<平台ID>` 或 `real-<sha1(正文)前12位>` |
| `content` | `text_content` / `text` | 字符串，**正文原文** | 抽取器的唯一输入；**不改写、不摘要、不删标点**（换行统一 `\n`） |
| `published_at` | `published` / `publish_time` | ISO8601 **带时区**，如 `2026-09-13T08:30:00+00:00` | 帖子发布时间；**必须 UTC 或带偏移**，禁止裸本地时间 |
| `source` | `source_name` | 字符串 | 来源标识：`weibo_xxx`（禁用）/ `x_<handle>` / `rss_<site>` / `manual_csv` 等；同一来源写法要一致 |
| `effective_at` | — | ISO8601 带时区 | **信息可用时间**（防泄漏的关键）；缺省时由脚本按 `max(published_at, collected_at)` 计算 |

> **最小可用输入 = 上面 5 列**（`sample_annotation_set.py --input-csv` 会按
> `_COLUMN_ALIASES` 自动映射，缺 `post_id`/`author_id`/`raw_item_id` 时按内容哈希生成确定性 ID）。
> 其余列（`author_name` / `has_media` / `collected_at` …）建议提供，缺失不会导致失败。

### 1.2 强烈建议提供（影响验收分层与可追溯性）

| 列名 | 说明 |
|---|---|
| `author_name` | 作者名（用于分层统计与「作者独立性」后续分析；同一作者写法统一） |
| `author_id` | 作者稳定 ID（若跨平台同名，务必区分） |
| `url` | 原帖链接（人工复核时回看原文用） |
| `has_media` | `true/false`：正文是否**依赖图片才能理解**（图内观点无法从文本抽取 → 评估时分层看） |
| `collected_at` | 本次采集时间（ISO8601 UTC），用于计算 `effective_at` 与审计 |
| `language` | `zh` / `en`（当前 Prompt 为中文指令 + 中英混排，英文占比高时需单独分层） |
| `notes_collect` | 采集备注（如「疑似广告」「转发他人观点」） |

### 1.3 收集红线（`.clinerules` + `docs/10`）

1. **禁止采集微博**（团队已裁决，`TECH_DEBT.md` TD-06）——可用机构/分析师公开 RSS、X/Twitter 公开帖、
   公众号导出、或你手工整理的 CSV；
2. **只收集公开可访问的内容**；不抓取登录墙后内容、不绕过访问控制；
3. **不下载图片二进制**（`has_media=true` 标记即可，图片理解属 Phase 2 后续/Phase 3）；
4. **不得含真实账户密钥/个人信息**（CSV 里出现手机号、私信内容请删除）；
5. **原始数据只增不改**：如发现错误，另存新版本（`content_v2`）而不是覆盖原行。

### 1.4 CSV 样例（可直接照抄表头）

```csv
id,content,published_at,effective_at,source,author_name,author_id,url,has_media,collected_at,language
real-x-1001,"黄金（XAUUSD）逢高做空，入场 2652-2660，止损 2678，目标 2590，日内交易。",2026-09-10T02:15:00+00:00,2026-09-10T02:15:00+00:00,x_goldmacro,黄金宏观笔记,au-001,https://example.com/p/1001,false,2026-09-13T09:00:00+00:00,zh
```

**格式要求**
- 编码 **UTF-8 带 BOM（`utf-8-sig`）**——Excel 双击不乱码；脚本可自动识别 UTF-8/GBK/Big5；
- 一行一帖；字段内有逗号/换行时用双引号包裹（标准 CSV）；
- **不要把 CSV 另存为 xlsx**（本项目踩过坑：`.csv` 实际是 xlsx → 读取按内容嗅探，能读但不应主动制造）；
- 文件名建议 `logs/real_posts_<采集日期>.csv`（**`logs/` 已被 `.gitignore` 忽略**，真实语料不入版本库）。

---

## 2. 数据质量自检（导入前必做，脚本化）

导入脚本（待实现，见 §5）会对 CSV 做**只读体检**并输出报告；以下是**你必须先自查**的清单：

| 检查项 | 通过标准 | 不通过的常见原因 |
|---|---|---|
| 正文非空 | 100% 行 `content` 非空且长度 ≥ 10 字符 | 采集时只存了标题/链接 |
| 正文完整 | 无 `...`/`[图片]`/`展开全文`截断 | 平台折叠未展开就抓取 |
| 时间可解析 | 100% 行 `published_at` 为 ISO8601 **带时区** | 直接抄了「今天 15:30」 |
| 时间合理 | `effective_at >= published_at`，且**无未来时间**（≤ 采集时刻） | 预发布 / 时区写错 |
| 重复率 | 完全重复正文 ≤ 5%；**转载**单独标注 | 多来源抓同一帖 |
| 来源分布 | 单一来源 ≤ 40%（避免"一个号主导结论"） | 只抓了一个大 V |
| 作者数 | ≥ 10 位不同作者（50~100 条时建议 ≥ 15 位） | 单人语料无法评估泛化 |
| 媒体帖占比 | 记录比例；**建议 ≤ 20%**（图内观点无法用文本评估） | 大量"看图操作"帖 |
| 长度分布 | 中位数 40~300 字；**> 4000 字符会被抽取器截断** | 长文研报 |
| 内容相关性 | 人工抽看 10 条，必须与"黄金观点"相关 | 混入行情播报/无关内容 |

> 体检结论会写入 `logs/real_posts_<日期>.meta.json`（与 Mock 抽样元数据同一机制），
> 报告里必须披露：**来源分布、作者数、媒体占比、重复率**——否则结论无法解释。

---

## 3. 标注规范（对照 `docs/10` 执行，此处只列**必读要点**）

### 3.1 五字段的取值（完整规则见 `docs/10 §4`）

| 字段 | 取值 | 记忆要点 |
|---|---|---|
| `stance` | `LONG` / `SHORT` / `FLAT` / `UNKNOWN` | 观望/等信号/已离场 = `FLAT`；条件句/引用/复盘/弱化/纯情绪 = `UNKNOWN` |
| `horizon` | `15m` / `30m` / `1h` / `4h` / `1d` / 空 | 无周期线索**必须留空**，禁止默认 `1d`（映射表见 `§4.2`，**已冻结**） |
| 价格四列 | `entry_low` / `entry_high` / `stop_loss` / `take_profit` | 没给就留空；**禁止填 0 或猜测**；多目标取**第一目标**；`UNKNOWN` 也照标点位 |
| `information_type` | `MACRO` / `TECHNICAL` / `NEWS` / `SENTIMENT` / `POSITIONING` / `OTHER` | 按 **§4.9.0 抽象原则 + 规则 9 阶梯**判定 |
| `confidence` | 0~1 或空 | 弱化词（不排除/或许/可能）≤ 0.4；一般弱化（预计/有望）≤ 0.5 |

### 3.2 最高优先级抽象原则（`docs/10 §4.9.0`，**冲突时以此为准**）

> 当**操作价位**与**驱动句**并存时：
> - 驱动句是**明确因果连接**（「…**因此**做多」「消息**引发**波动率跳升」）→
>   **按驱动分类**（`MACRO` / `NEWS` / `SENTIMENT` / `POSITIONING`）；
> - 驱动句仅是**背景或附注**（「**关注** xxx」「盘面情绪指标显示…」「需继续跟踪…」）→
>   **按操作分类**（`TECHNICAL`）。

### 3.3 标注工作流（50~100 条规模，建议 2 天）

1. **人工独立标注**：按 `docs/10` 填标注 CSV（列定义见 `docs/10` 附录 A，共 29 列）；
   前半段（id/时间/正文）由脚本预填，**只需填后半段**：
   `no_opinion / opinion_index / stance / instrument / horizon / confidence / entry_low /
   entry_high / stop_loss / take_profit / information_type / rationale / notes`；
2. **自审**：同一帖内"方向 vs 点位 vs 类型"三者互不矛盾；`UNKNOWN` **不算**"无观点"；
3. **互审（推荐）**：抽 20% 由第二人复标，统计一致率（Kappa）；
4. **留理由**：凡判"未给出"或与模型/他人不同，都在 `notes` 写一句理由
   —— **这决定后续能否解释差异**（`docs/10 §9` 变更流程也依赖它）。

### 3.4 校准案例（TD-29 冻结的 11 格，**真实语料标注前先看这 11 例**）

这 11 格是 Mock 语料里**人工自身不一致**的边界（模型判 `TECHNICAL`、人工判别的驱动），
真实语料标注遇到同类结构时，**统一按 §4.9.0 抽象原则判**，并在 `notes` 记录你的选择：

| # | 帖子 | 结构 | 人工（Mock） | 模型 |
|---|---|---|---|---|
| 1 | `mock-post-0027` | 操作块 +「关注美债收益率回落」 | `MACRO` | `TECHNICAL` |
| 2 | `mock-post-0079` | 操作块 + 情绪/风险偏好尾句 | `SENTIMENT` | `TECHNICAL` |
| 3 | `mock-post-0109` | 同上 | `SENTIMENT` | `TECHNICAL` |
| 4 | `mock-post-0119` | 「还有多少人愿意进场？反正我是不着急」+ 模板尾句 | `SENTIMENT` | `TECHNICAL` |
| 5 | `mock-post-0127` | 操作块 +「超短线，分钟级」 | `MACRO` | `TECHNICAL` |
| 6 | `mock-post-0139` | 操作块 + 情绪尾句 | `SENTIMENT` | `TECHNICAL` |
| 7 | `mock-post-0159` | `bias: short…` + 情绪尾句 | `SENTIMENT` | `TECHNICAL` |
| 8 | `mock-post-0169` | 操作块 + 情绪尾句 | `SENTIMENT` | `TECHNICAL` |
| 9 | `mock-post-0187` | 操作块 +「超短线，分钟级」 | `MACRO` | `TECHNICAL` |
| 10 | `mock-post-0207` | `bias: short…` + 宏观尾句 | `MACRO` | `TECHNICAL` |
| 11 | `mock-post-0219` | `bias: long…` + 情绪尾句 | `SENTIMENT` | `TECHNICAL` |

> 处置：**冻结为已知噪音**（不改 Mock 金标准、不再调 Prompt）。真实语料上若再次大量出现，
> 说明是**真实世界口径问题**，届时按 §3.3 工作流重新裁决并同步 `docs/10`。


---

## 4. 端到端流程（命令清单，**真实语料版**）

> 与 Mock 流程的差别只在第 ① 步：`--input-csv` 指向你的真实 CSV（并**禁用自动生成 Mock**）。

```powershell
# ① 抽样与体检：真实 CSV → 标注表（列齐 29 列，前段预填、后段待人工填）
python -m scripts.sample_annotation_set `
  --input-csv logs/real_posts_20260915.csv `
  --out logs/real_annotation_sample.csv `
  --limit 100 --seed 20260915 --no-mock-fill --require-full
#   退出码：0=成功 / 2=无候选 / 3=样本不足（--require-full）；同时产出 .meta.json 体检元数据

# ②（可选）模型参考：把同一批样本交给 3 个模型标注，导出分歧清单
python scripts/compare_model_annotations.py `
  --input logs/real_annotation_airesult.xlsx `
  --pending-out logs/real_pending_review.csv `
  --consensus-out logs/real_model_consensus.csv `
  --reference-csv logs/real_annotation_sample.csv

# ③ 人工标注：填 logs/real_annotation_sample.csv 的后半段（或填 real_pending_review.csv 的 final_gold_standard）

# ④ 合并金标准
python scripts/build_ground_truth.py `
  --pending logs/real_pending_review.csv `
  --consensus logs/real_model_consensus.csv `
  --ground-truth logs/real_ground_truth.csv `
  --report docs/experiments/real_ground_truth_report.md `
  --reference-csv logs/real_annotation_sample.csv

# ⑤ 跑两个抽取器（正则基线 + LLM v13）
python scripts/evaluate_extractor.py --extractor regex `
  --gold logs/real_ground_truth.csv --texts logs/real_annotation_sample.csv `
  --eval-out logs/real_extractor_eval.csv --report docs/experiments/real_baseline_report.md
python scripts/evaluate_extractor.py --extractor llm `
  --gold logs/real_ground_truth.csv --texts logs/real_annotation_sample.csv `
  --eval-out logs/real_extractor_eval_llm.csv --report docs/experiments/real_llm_report.md

# ⑥ 最终对比报告（含「人工 ↔ 模型」案例；`--gold-before` 传本批的"修订前"版本，若无则留空）
python scripts/compare_extractor_baselines.py `
  --regex logs/real_extractor_eval.csv `
  --llm logs/real_extractor_eval_llm.csv `
  --usage logs/real_extractor_eval_llm_usage.json `
  --gold logs/real_ground_truth.csv `
  --texts logs/real_annotation_sample.csv `
  --report "docs/experiments/Phase 2 真实语料验收报告.md"
```

**命令要点**
- `--no-mock-fill`：**必须加**，否则输入文件缺失时脚本会静默生成 Mock 数据（Mock 只在演练时用）；
- `--require-full`：样本不足 100 条时以退出码 3 失败，避免"静默少抽"；
- 所有产物写 `logs/`（已 `.gitignore`）；只有 `docs/experiments/*.md` 进版本库；
- LLM 侧成本预估：单条 ≈ **$0.0002**（v13 实测），100 条 ≈ **$0.02**；
  重复跑用 `--llm-cache-mode readonly`（**¥0**，命中缓存）。

---

## 5. 数据导入方案（两种，按需选择）

### 方案 A（**推荐先做**）：文件路径直连评估链（本次验收用）

- **不需要写数据库**，也不需要新脚本——`sample_annotation_set.py` 已支持 `--input-csv`
  （列别名 `id`/`source`/`content`/`published` 自动映射；**唯一硬要求是正文列**）；
- 你的 CSV → 标注表 → 金标准 → `evaluate_extractor.py` → `compare_extractor_baselines.py`
  全程只读文件，**零 DB 依赖、零迁移**，符合 `.clinerules` 对 Mock/真实数据的隔离要求；
- 唯一需要新增的是**导入体检脚本**（见下），用于把 §2 的体检项自动化并产出 `meta.json`。

### 方案 B（验收通过后再做）：真实帖子正式入库

- 目标表：`author_accounts`（作者）→ `author_posts`（帖子）→ `author_opinions`（观点，
  由 `opinion_pipeline` 写入，`parser_version=llm-deepseek-v13` 留痕）；
- 复用现有能力：`database/seeds/authors.py`（作者 CSV 导入 CLI，幂等 + 审计）、
  `src/processors/opinion_pipeline.py`（幂等 + 失败隔离）、`propagation.py`（转载去重）；
- 注意：`author_posts` 有严格时间约束（`effective_at >= published_at`，
  由 `timeline.py` + leakage 测试强制），导入时必须传 UTC 带时区时间。

### 待实现的导入脚本契约（`scripts/import_real_posts.py`，**等你数据到位后再写**）

| 项 | 设计 |
|---|---|
| 入参 | `--input logs/real_posts_20260915.csv`（必填）、`--out logs/real_annotation_sample.csv`、`--meta-out logs/real_posts_20260915.meta.json`、`--target {annotation,db}`、`--limit`、`--window-start/--window-end`（时间窗过滤）、`--dry-run` |
| 列映射 | 复用 `sample_annotation_set._COLUMN_ALIASES`（**同一套别名表**，避免两处口径漂移） |
| 校验（硬失败，退出码 2） | 缺正文列 / 正文全空 / 时间不可解析 / `effective_at < published_at` / 出现未来时间 |
| 校验（软告警，写报告） | 重复率、来源分布、作者数、媒体占比、长度分布、`content` 超 4000 字符（会被抽取器截断） |
| 输出 | ①标准化 CSV（29 列，前段预填、后段留空给人工）②`meta.json`（体检结论 + 文件 sha256 + 行数） |
| 红线 | **只读输入、不覆盖原始 CSV**；不写 `.env`/密钥；`--target db` 时先跑迁移检查 |
| 测试 | `tests/unit/test_real_post_import.py`：列别名映射、硬失败分支、软告警计数、`--dry-run` 不落盘、不碰数据库（源码扫描） |

> 我会在你说"数据已准备好"之后实现该脚本 + 单测（并同步 `docs/05` 与本指南 §5 的"已实现"状态）。

---

## 6. 验收指标与判定

### 6.1 门槛（`docs/08 §5`，人工子集口径）

| 字段 | 门槛 | Mock 实测（v13，200 条） |
|---|---|---|
| `stance` 准确率 | ≥ 90% | **100%** |
| `horizon` 准确率 | ≥ 85% | **100%** |
| `stop_loss` 准确率 | ≥ 90% | **100%** |
| `take_profit` 准确率 | ≥ 90% | **100%** |
| `confidence` 合法性 | 必须落在 0~1（非法即 FAIL） | 0 非法 |
| `information_type`（侧字段） | 无硬门槛，报告须给出准确率 | **91.9%** |

### 6.2 报告必须包含（沿用 `compare_extractor_baselines.py` 结构）

1. 逐字段准确率 **regex vs LLM** + 提升百分点 + PASS/FAIL；
2. 五类判定构成：**该判未判 / 提取错误 / 不该判却判**（三者必须分开，不能只报"准确率"）；
3. **逐格变化**：修复 N 格 / 回归 M 格（回归必须逐格解释）；
4. `人工 ↔ 模型` 对齐案例：模型纠正人工 / 人工纠正模型（脚本自动 diff 金标准版本）；
5. Token 与费用；6. 局限与下一步。

### 6.3 样本量与统计效力（**必须写进报告**）

- 50 条 ≈ 250 格、100 条 ≈ 500 格；按人工裁决比例，`stop_loss` / `take_profit` 的有效格
  可能只有 20~80 格 → **单个字段的置信区间会很宽**；
- 因此：**不得**用 50 条的单一数字宣称"某字段达标/不达标"，应给出
  *命中数/分母 + 百分比*，并对**关键失败模式**逐例说明；
- 若真实语料上出现**新的失败模式**（如口语化表达、表情符号、@提及、多帖连发），
  必须在报告里单列，并决定"改 Prompt / 改规范 / 记为已知噪音"。

---

## 7. 交付检查表（数据到位后逐项打勾）

- [ ] `logs/real_posts_<日期>.csv`：UTF-8-BOM、5 个必须列齐全、正文未被截断
- [ ] 体检通过：重复率 ≤ 5%、来源 ≤ 40%、作者 ≥ 10、媒体帖 ≤ 20%、无未来时间
- [ ] `import_real_posts.py` 实现 + 单测（含 `--dry-run`）
- [ ] `logs/real_annotation_sample.csv`（29 列，后段人工填完）
- [ ] `logs/real_ground_truth.csv` + 金标准生成报告（`build_ground_truth.py`）
- [ ] 正则基线 + LLM v13 两次评估（`logs/real_extractor_eval*.csv`）
- [ ] 《Phase 2 真实语料验收报告.md`（§6.2 六节齐全，含失败模式逐例）
- [ ] `docs/05` 勾选 Phase 2 验收结论；`TECH_DEBT.md` 更新（TD-29 是否解除）
- [ ] **不进入 Phase 3**（等人工确认）

---

## 8. 常见坑（本项目已踩过，务必避免）

| 坑 | 症状 | 规避 |
|---|---|---|
| Excel 另存为 xlsx 但文件名仍是 `.csv` | 读表按内容嗅探才能识别 | 用「CSV UTF-8」导出；脚本已做内容嗅探 |
| 时间无时区 | `effective_at` 计算偏移、leakage 校验失败 | 采集时直接存 `+00:00` |
| 正文含 `\r\n` 混排 | 哈希/去重不一致 | 统一 `\n`（脚本会归一化） |
| 同一帖多来源转载 | 观点被重复计数（1 条原创 + N 转载） | 标 `duplicate_of` 或用 `propagation.py` 去重（阈值 0.80） |
| 图片帖（"看图操作"） | 文本无线索 → 判"无观点"，拉低召回 | `has_media=true` 单独分层统计 |
| 超长研报（> 4000 字） | 被截断后丢失关键信息 | 报告里披露截断条数；必要时拆分帖子 |
| 用 LLM 结果当"预标注"直接改人工表 | 人工裁决失去独立性（等于自证） | 只看分歧、**人工独立判定**，分歧留在 `notes` |
| 拿 Mock 指标当"真实效果" | 用模板语料的高分误导决策 | 本指南 §6.3：真实语料必须重新报数 |

