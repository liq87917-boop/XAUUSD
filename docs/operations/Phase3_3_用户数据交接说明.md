# Phase 3.3 用户数据交接说明

## 1. 需要用户提供：真实作者帖子

请复制 `templates/phase3_3/author_posts_template.csv` 后填写；模板只有表头，不含 Mock 行。每个来源
账号还必须在 `templates/phase3_3/author_source_authorizations_template.csv` 中登记授权证据。

| 列 | 必填 | 填写要求 |
|---|---|---|
| `id` | 是 | 来源站点内稳定且唯一的帖子 ID；不要自行重复编号 |
| `source` | 是 | 来源名称，建议 `manual-平台名` |
| `author_name` | 是 | 页面显示的作者名 |
| `external_account_id` | 是 | 平台账号 ID；没有时填可核验的主页标识，不要填昵称副本 |
| `content` | 是 | 原文，不改写、不总结；观点语料建议至少 90 字 |
| `published_at` | 是 | 原始发布时间，必须含时区，例如 `2026-09-18T08:30:00+08:00` |
| `collected_at` | 是 | 实际看到/导出该帖的时间，必须独立记录且晚于 `published_at`，不能复制发布时间 |
| `effective_at` | 是 | 填 `published_at` 与 `collected_at` 中较晚者 |
| `url` | 是 | 可复核且不重复的原始页面或授权档案 `http/https` 地址 |
| `has_media` | 是 | `true` / `false`；图片内有关键点位时填 `true` |
| `source_type` | 是 | 作者观点固定填 `NEWS`，不要填 `EVENT` |
| `collection_time_provenance` | 是 | 真实独立记录填 `independent_observation`；禁止填 fallback |

硬门槛：按 `source + external_account_id` 识别的每个平台账号至少 30 条可信且可标注观点；同名的
不同账号不能合并凑数，同一账号的 `author_name` 必须一致。为了能做时间外推，建议每个平台账号
提供至少 50 条且覆盖多个自然日；同文转载不会增加独立样本数。

禁止事项：

- 不得把 `collected_at` 从 `published_at` 复制或推算；
- 不得用模型生成/改写内容补足条数；
- 不得删除失败观点或只挑命中的帖子；
- 不得把截图中的时间凭印象换算，无法确认时保留原截图并标记待核验。
- 不得因网页可公开浏览就默认允许自动抓取或用于模型研究；必须同时确认站点条款、API 授权或
  内容提供方书面许可。

填写后先运行只读体检（不会写数据库）：

```powershell
.\.venv\Scripts\python.exe scripts/check_phase3_3_author_input.py `
  --input <填写后的CSV或XLSX> `
  --authorizations <填写后的来源授权CSV或XLSX> `
  --report logs/phase3_3_author_input_check.md
```

只有报告结论为 `PASS` 才进入后续人工抽检与追加导入；`BLOCKED` 时按逐行错误修正原始来源，
不能让程序自动猜值。

### 来源授权表各列

| 列 | 填写要求 |
|---|---|
| `source` | 必须与帖子表完全一致 |
| `external_account_id` | 必须与帖子表的稳定账号 ID 完全一致 |
| `authorization_status` | 只有证据已核验后填 `APPROVED`；否则填 `PENDING` 或 `REJECTED` |
| `authorization_basis` | `official_api`、`license_agreement`、`written_permission`、`user_owned` 四选一 |
| `authorization_reference` | 可复核的 `https` 条款 URL 或项目中 `docs/legal/` 内现存许可文件路径；URL 的法律内容仍需人工核验 |
| `permits_automated_collection` | 是否明确允许自动采集，填 `true/false` |
| `permits_local_storage` | 是否明确允许本地保存，填 `true/false` |
| `permits_research_use` | 是否明确允许研究/模型处理，填 `true/false` |
| `reviewed_by` | 实际核验授权的人，不得填模型名冒充人工 |
| `reviewed_at` | 核验时间，必须带时区 |
| `valid_from` | 授权生效时间，必须带时区 |
| `expires_at` | 有期限时填带时区的到期时间；无期限可留空 |

机械门禁只有在三项许可全部为 `true`、授权当前有效且账号键与帖子一致时才放行。授权表为空、
状态待定、授权过期或只允许浏览但不允许存储/研究，都会保持 BLOCKED。
体检使用带时区的当前时钟；发布时间、采集时间和授权核验时间即使只领先当前时钟一分钟，也视为未来时间，
不再给予一小时宽限。若源系统时钟不准确，请先核对原始证据和设备时间，不得人为修改时间使其通过。
门禁只核对授权登记的字段、证据地址格式和本地文件是否存在；`APPROVED` 和三个 `true` 都是
人工签认声明，程序不能证明 URL 的法律效力、许可范围或签认人身份。未经实际人工核验不得填真。

## 2. 新闻历史：暂时不要填写普通 CSV

当前 `raw_items` 契约令 `effective_at >= collected_at`。今天下载的历史新闻即使有旧
`published_at`，在研究系统中也只能从今天起可用，不能倒填成历史可见，否则构成前视泄漏。
因此，“再提供 200 条旧新闻标题”本身不能解除 News Alpha 阻塞。

用户只需提供候选数据源信息：数据供应商/官方档案名称、授权或公开使用依据、导出字段说明，
以及能否证明每条记录的历史发布/可用时间。工程侧在确认契约后再给正式导入模板；在此之前不应
手工把历史新闻写入 PostgreSQL。

## 2.1 作者公开网页试采结论（2026-09-19）

- 中金在线黄金网候选页出现证书域名不匹配，未绕过浏览器安全警告，暂不使用。
- Kitco 的作者页具备稳定作者标识、文章链接和时间信息，但其公开《Terms of Use》禁止机器人、
  自动检索/数据挖掘以及未经授权存储或复制内容，因此不能作为本项目的自动采集源。
- 已停止试采并清理临时正文样本；未写数据库。继续前需要用户提供允许自动采集和研究使用的
  官方 API、数据许可或内容方书面授权。

## 3. 文件处理边界

收到作者文件后，先运行只读体检与去重；任何硬错误都会生成逐行清单，不会自动猜时区或改原文。
只有体检通过且用户确认来源后，才允许追加写入研究库。原文件始终保留，不覆盖。
