# Phase 3.3 用户数据交接说明

## 1. 需要用户提供：真实作者帖子

请复制 `templates/phase3_3/author_posts_template.csv` 后填写；模板只有表头，不含 Mock 行。

| 列 | 必填 | 填写要求 |
|---|---|---|
| `id` | 是 | 来源站点内稳定且唯一的帖子 ID；不要自行重复编号 |
| `source` | 是 | 来源名称，建议 `manual-平台名` |
| `author_name` | 是 | 页面显示的作者名 |
| `external_account_id` | 是 | 平台账号 ID；没有时填可核验的主页标识，不要填昵称副本 |
| `content` | 是 | 原文，不改写、不总结；观点语料建议至少 90 字 |
| `published_at` | 是 | 原始发布时间，必须含时区，例如 `2026-09-18T08:30:00+08:00` |
| `collected_at` | 是 | 实际看到/导出该帖的时间，必须独立记录，不能复制 `published_at` |
| `effective_at` | 是 | 填 `published_at` 与 `collected_at` 中较晚者 |
| `url` | 是 | 可复核的原始页面地址或授权档案地址 |
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

填写后先运行只读体检（不会写数据库）：

```powershell
.\.venv\Scripts\python.exe scripts/check_phase3_3_author_input.py `
  --input <填写后的CSV或XLSX> `
  --report logs/phase3_3_author_input_check.md
```

只有报告结论为 `PASS` 才进入后续人工抽检与追加导入；`BLOCKED` 时按逐行错误修正原始来源，
不能让程序自动猜值。

## 2. 新闻历史：暂时不要填写普通 CSV

当前 `raw_items` 契约令 `effective_at >= collected_at`。今天下载的历史新闻即使有旧
`published_at`，在研究系统中也只能从今天起可用，不能倒填成历史可见，否则构成前视泄漏。
因此，“再提供 200 条旧新闻标题”本身不能解除 News Alpha 阻塞。

用户只需提供候选数据源信息：数据供应商/官方档案名称、授权或公开使用依据、导出字段说明，
以及能否证明每条记录的历史发布/可用时间。工程侧在确认契约后再给正式导入模板；在此之前不应
手工把历史新闻写入 PostgreSQL。

## 3. 文件处理边界

收到作者文件后，先运行只读体检与去重；任何硬错误都会生成逐行清单，不会自动猜时区或改原文。
只有体检通过且用户确认来源后，才允许追加写入研究库。原文件始终保留，不覆盖。
