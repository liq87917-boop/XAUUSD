# 13_NewsAPI 接入调研方案（**仅调研，未注册、未写代码**）

> 调研时间：2026-09-13｜调研方式：**只读官方文档**（`newsapi.org/docs`、`/pricing`、`/docs/endpoints/everything`）
> **未注册账号、未申请 API Key、未发任何请求到 NewsAPI。**
> 委托背景：用户提出"引入 NewsAPI.org 免费额度（认为每月 100 次）作为兜底语料源"，
> 并要求先判断"它是否返回正文；若只返回摘要，摘要 ≥150 字符是否够做观点抽取"。

---

## 1. 结论摘要（TL;DR）

| 问题 | 结论 |
|---|---|
| 免费额度是"每月 100 次"吗？ | ❌ **不是**。官方定价页写的是 **`100 requests per day`（每天 100 次）**，且 **"No extra requests available"**（无超额购买） |
| 免费计划能用于本项目吗？ | ⚠️ **不能用于"生产/预发（含内部）"**：官方 FAQ 明确 Developer 计划 **"may be used for development and testing in a development environment only, and cannot be used in a staging or production environment (including internally)"** |
| 免费计划还有什么限制？ | ① **文章有 24 小时延迟**；② 只能检索**最近 1 个月**（付费才 5 年）；③ CORS 仅 localhost；④ 无 SLA |
| **返回正文吗？** | ❌ **任何套餐都不返回全文**。官方 FAQ：**"Is the full article content available with any plan? No, unfortunately we cannot provide the full content with our search results."** `content` 字段文档原文：**"The unformatted content of the article, where available. This is truncated to 200 chars."** |
| 摘要够做观点抽取吗？ | ⚠️ **勉强不够**：`description` 是 snippet（与 `content` 同为 ~200 字符级）。黄金观点抽取需要**方向 + 入场区间/止损/目标**，200 字符截断文本通常给不全；**若只做"事件/情绪对照"则可用** |
| 建议 | ❌ **不作为 Phase 2 观点语料主源**；✅ 可考虑作**"文章 URL 发现层"**（title/description/url/publishedAt），正文另行合规获取 —— 但那会引入新的合规与维护面 |

---

## 2. 官方事实（逐条带出处）

| 事实 | 出处 |
|---|---|
| Developer：**$0**，`100 requests per day`，`No extra requests available`，`No uptime SLA`，`Basic support` | `/pricing` |
| Developer：`Articles have a 24 hour delay`、`Search articles up to a month old`、`CORS enabled for localhost` | `/pricing` |
| Developer **仅限开发环境**，不得用于 staging/production（含内部使用） | `/pricing` FAQ |
| Business **$449/月**（250,000 req/月）、Advanced **$1749/月**（2,000,000 req/月，99.95% SLA） | `/pricing` |
| **任何套餐都不提供全文**，文档建议"用返回的 URL 自行抓取正文" | `/pricing` FAQ |
| `/v2/everything` 的 `content`：**"truncated to 200 chars"**；`description`：snippet | `/docs/endpoints/everything` |
| 请求参数：`q`（≤500 字符，支持 `""`/`+`/`-`/`AND OR NOT`）、`searchIn=title,description,content`、`sources`（≤20）、`domains`/`excludeDomains`、`from`/`to`（ISO8601）、`language`（含 `zh`）、`sortBy=relevancy\|popularity\|publishedAt`、`pageSize`（默认 100，**最大 100**）、`page` | `/docs/endpoints/everything` |
| 响应字段：`status`、`totalResults`、`articles[]`（`source{id,name}`、`author`、`title`、`description`、`url`、`urlToImage`、`publishedAt`（UTC）、`content`） | `/docs/endpoints/everything` |
| "一次请求"的定义：**任意单次 HTTP 请求即计 1 次**（与参数/返回条数无关） | `/pricing` FAQ |

---

## 3. 与我们 Phase 2 验收需求的差距分析

| 需求（`docs/11 §1.1`） | NewsAPI 能否满足 | 说明 |
|---|---|---|
| `content` 正文（观点抽取的基础） | ❌ 不能 | 只有 ≤200 字符的 `content` / snippet，**且不保证有**（"where available"） |
| `published_at`（UTC，可追溯） | ✅ 能 | `publishedAt` 是 UTC |
| `author_name` | ⚠️ 部分 | 有 `author`，但常为空/机构名 |
| `url`（可回溯） | ✅ 能 | `url` 直链 |
| 时间纪律（禁止未来信息） | ⚠️ 有隐藏风险 | 免费计划 **24h 延迟** + 只能查 1 个月内 → 时间窗要显式控制 |
| 合规（robots/授权） | ⚠️ 需评审 | 走第三方 API 本身合规，但**正文要自己抓**（回到 robots/付费墙问题） |
| 可复现（Mock 可测） | ✅ 能 | JSON API，天然可 Mock |
| 成本 | ⚠️ | 免费=dev-only；要"生产可用"需 **$449/月**起 |

**关键判断**：我们 Phase 2 需要的是**真实作者正文**（用于观点字段标注与抽取对比）。
NewsAPI 的 `content` 被**硬截断到 200 字符**、且**官方明确不提供全文** →
**它无法单独支撑观点抽取验收**；只能作"标题/摘要级事件层"，或作"URL 发现层"（正文另抓）。

---

## 4. 若将来接入：建议的边界与落地方式（**本轮不做**）

1. **定位**：`source_type=EVENT` 同级 —— 只做"事件/摘要层"，**不进观点语料**；
   或作"URL 发现层"，正文由 `scripts/collect_rss.py` 之外的独立合规组件获取（需另立评审）。
2. **密钥**：`NEWSAPI_API_KEY` **只放 `.env`**（`.env` 已在 `.gitignore`；不得写入代码/配置/日志/报告）。
3. **接口**：`GET https://newsapi.org/v2/everything`
   - 建议参数：`q=gold OR bullion OR 黄金`（≤500 字符）、`searchIn=title,description`、
     `language=zh,en`（注意 `language` 是单值，需要两次请求或只取 `en`）、`sortBy=publishedAt`、
     `pageSize=100`、`from`/`to` 严控时间窗（防未来数据）、`page` 分页；
   - 建议 `domains=` 白名单限定可信财经域，降低噪声。
4. **配额设计**：免费 100 req/day → 每天最多 100×100 = 10,000 条元数据；**但免费仅限开发**，
   真要用在生产必须走 Business（$449/月）→ 需预算决策。
5. **字段映射**（如启用）：`title→title`、`description→content`（并记 `raw_json.newsapi_content`）、
   `publishedAt→published_at/effective_at`、`author→author_name`、`url→url`。
6. **缓存与复现**：沿用 `src/collectors/rss_cache.py` 思路（键=URL+参数哈希；`readonly` 零请求重放）。

---

## 5. 待用户决策

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | NewsAPI 是否作为**元数据/事件层**接入（不进观点语料）？ | 可选，但收益有限（已有 `fed_press`/`ecb_press` 覆盖事件层） |
| 2 | 是否愿意接受 **$449/月** 才能获得"生产可用 + 更多正文"？ | 仍**不含全文** → 不建议 |
| 3 | 继续找**全文 RSS**（WordPress 类站点/研究机构/交易所公告/券商研究栏目）？ | ✅ **推荐**（零成本、可复现、正文完整） |
| 4 | 是否下调验收目标（用现有 10 条正文做小样本链路演练，报告中写明样本量限制）？ | ✅ 备选（成本最低） |

> 备注：本报告只做事实调研与差距分析，**未注册账号、未申请 Key、未调用任何 NewsAPI 接口、未写任何接入代码**。
