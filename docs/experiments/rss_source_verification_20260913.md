# RSS 源验证报告（Phase 2 真实语料，阶段一：逐源冒烟）

> 执行时间：**2026-09-13 11:02 UTC**（本机时间 2026-09-13）
> 执行人：AI（Cline）｜批准：用户（2026-09-14 明确放行"阶段一"真实小流量抓取）
> 采集器：`scripts/collect_rss.py` + `src/collectors/rss_collector.py`（`rss-feedparser-v1`）
> 探测配置：`logs/_probe_sources.json`（**临时文件、已被 .gitignore 忽略**；
> `config/rss_sources.json` 正式清单**未做任何改动**，4 个源仍为 `enabled=false`）

---

## 1. 结论（TL;DR）

| 项 | 结果 |
|---|---|
| 通过验证的源 | **0 / 4** |
| 失败原因分布 | **3 个**：候选 feed URL **HTTP 404**（路径不存在）；**1 个**：robots.txt **HTTP 403** → 合规拒绝（按 fail-closed **未抓取 feed**） |
| 是否启用任何源 | **否**（`config/rss_sources.json` 保持全部 `enabled=false`/`verified=false`，等人工确认） |
| 是否拿到可用语料 | **否**（0 条），故**阶段三（抓 100~200 条）不启动** |

**一句话**：4 个候选地址全部无效——3 个是我预填的 URL 路径不对，1 个站点连 robots.txt 都不给读（403），
按合规红线直接放弃该源。**需要新的、已验证的 feed 地址才能继续。**

---

## 2. 逐源验证结果

| # | 源 id | 站点 | robots.txt 判定 | feed HTTP | 条目 | 判定 | 依据（留痕） |
|---|---|---|---|---|---|---|---|
| 1 | `jin10_flash` | www.jin10.com | ✅ 允许（**404 → 视为无限制**） | **404** | 0 | ❌ **不可用** | `logs/_probe_jin10_flash.json` |
| 2 | `fx678_gold` | www.fx678.com | ✅ 允许 | **404** | 0 | ❌ **不可用** | `logs/_probe_fx678_gold.json` |
| 3 | `wallstreetcn_feed` | wallstreetcn.com | ✅ 允许 | **404** | 0 | ❌ **不可用** | `logs/_probe_wallstreetcn_feed.json` |
| 4 | `investing_gold` | www.investing.com | ❌ **不可验证**（robots.txt 返回 **403**） | 未请求 | 0 | ❌ **不可用**（合规拒绝） | `logs/_probe_investing_gold.json` |

失败原文（可直接复盘）：

- `jin10_flash`：`HTTP 404（响应非 2xx）`，robots 原因：`robots.txt 不存在（404）→ 视为无限制`
- `fx678_gold`：`HTTP 404（响应非 2xx）`，robots 原因：`robots.txt 允许`
- `wallstreetcn_feed`：`HTTP 404（响应非 2xx）`，robots 原因：`robots.txt 允许`
- `investing_gold`：`robots 检查未通过（robots.txt 返回 HTTP 403（非 200/404）→ 拒绝）`，**未发 feed 请求**

---

## 3. 执行方式（可复现）

```powershell
# 0) 零网络预演：确认探测配置能被正确加载（4 源全部 readonly 拒绝联网）
python scripts/collect_rss.py --sources logs/_probe_sources.json --dry-run

# 1) 逐源真实冒烟（每次只跑一个源，便于隔离失败）
python scripts/collect_rss.py --sources logs/_probe_sources.json --source <源id> `
  --no-dry-run --per-feed-limit 5 --min-interval 1.5 `
  --raw-dir logs/rss_raw --out logs/_probe_<源id>.csv --json-out logs/_probe_<源id>.json
```

**三条硬性要求的落实情况**：

| 要求 | 落实 | 实测证据 |
|---|---|---|
| 抓取前必须查 robots.txt，禁止则标记不可用 | 采集器内置、**fail-closed**；`investing_gold` 因此被判 `failed` 且**未发 feed 请求** | `raw_body_chars=0`、`http=null`、`transport` 仅 1 次请求 |
| 请求间隔 ≥ 1 秒 | CLI 新增 `--min-interval`（**硬下限 1.0s**，低于则抬升 + 告警）+ `RateLimiter` 保证**任意两次请求**间隔 | 本次实跑 `min_interval=1.5`，第 2 个请求实际等待 **1.369 / 1.398 / 1.453 s** |
| 每个源单独保存原始 JSON | CLI 新增 `--raw-dir`（默认 `logs/rss_raw`） | 4 份 `logs/rss_raw/<源id>_<UTC时间戳>.json`（含 spec / robots / HTTP 状态 / 缓存 ETag / 解析条目 / **原始响应体** `raw_body`） |

---

## 4. 留痕文件清单（全部在 `logs/`，已被 .gitignore 忽略）

| 文件 | 内容 |
|---|---|
| `logs/_probe_sources.json` | 探测用临时源清单（`enabled=true` 只存在于这里，绝不提交） |
| `logs/_probe_<源id>.json` | 每源运行统计（status / http_status / robots / warnings / raw_json 路径） |
| `logs/_probe_<源id>.csv` | 每源导出结果（本次均为 0 行） |
| `logs/rss_raw/<源id>_<时间戳>.json` | **单源原始 JSON**（复盘用，含原始响应体） |
| `logs/rss_cache/<hash>.json` | 响应缓存（404 也留痕：`status=404` + `error`），复跑零请求 |

---

## 5. 技术观察（对后续有价值的部分）

1. **fail-closed 设计在真实网络下按预期生效**：`investing.com` 的 robots.txt 返回 403（不是 200/404），
   系统判"技术性无法验证" → `failed`，**没有静默成功**，也**没有越线去抓 feed**。
2. **robots 404 语义已区分**：`jin10.com` 没有 robots.txt → 按 RFC 视为无限制（允许），与"取不到"（403/超时→拒绝）严格分开。
3. **404 不一定是"没有 feed"**：三家站点可能只是我的候选路径不对，也可能对非浏览器 UA 返回 404（WAF 行为）。
   在**不改装 UA**（保持诚实声明身份）的前提下，正确做法是**找到站点自述的 feed 地址**，而不是猜路径。
4. **发现并修复的真实缺陷（P2，已回归测试锁定）**：CLI 用完 `AiohttpTransport` 未关闭会话，
   真实运行会打印 `Unclosed client session`；现已在 `finally` 中显式 `close()`，
   并加零网络回归测试 `test_live_run_closes_the_http_session`（外加一次真实复跑确认告警消失）。
5. **测试隔离缺陷已修**：原先 3 个 CLI 测试会用默认 `--raw-dir` 把文件写进仓库 `logs/rss_raw`
   （发现 `feed_a_*.json` 污染）；现已全部改为 `--raw-dir ""`，实测测试跑完后不再产生仓库文件。

---

## 7. 第二轮（2026-09-13 11:13 UTC）：用户提供的 4 个新源，**前 2 个冒烟通过**

### 7.1 本轮改动（`config/rss_sources.json` → `rss-sources-v2`）

- **新增 4 个源**（用户 2026-09-13 人工验证并提供）：
  | id | URL | 状态 |
  |---|---|---|
  | `fred_blog` | `https://fredblog.stlouisfed.org/feed/` | **`enabled=true`**（待冒烟） |
  | `fed_press` | `https://www.federalreserve.gov/feeds/press_all.xml` | **`enabled=true`**（待冒烟） |
  | `ecb_press` | `https://www.ecb.europa.eu/rss/press.html` | `enabled=false`（等前 2 个结果 + 人工确认） |
  | `yahoo_gold` | `https://finance.yahoo.com/rss/headline?s=GC=F` | `enabled=false`（同上） |
- 旧 4 个失败候选**保留在清单内但保持禁用**，`notes` 写明 2026-09-13 的失败原因（可追溯）。

### 7.2 冒烟结果（逐源执行；robots 前置、`--min-interval 1.5`、每源留痕）

| 源 | robots.txt | feed HTTP | 条目 | 结论 |
|---|---|---|---|---|
| `fred_blog` | ✅ 允许 | **200** | **5** | ✅ **技术通过** |
| `fed_press` | ✅ 允许（robots.txt 404 → 视为无限制） | **200** | **5** | ✅ **技术通过**（但正文=标题，见 7.4） |

### 7.3 字段完整性（`docs/11 §1.1` 列契约，5/5 行全字段非空）

| 源 | 13 列非空 | `published_at` | content 长度 min/median/max | `has_media` | 残留 HTML 实体 |
|---|---|---|---|---|---|
| `fred_blog` | **5/5 全列** | 100% | 2120 / 2523 / 4562 | 5/5（含图） | **0**（修复前 34） |
| `fed_press` | **5/5 全列** | 100% | 74 / 100 / 154 | 0/5 | 0 |

- 两源时间**都带时区**→ 正常转 UTC，无一条回落 `collected_at`（无时区猜测）；
- `effective_at == published_at`，未触发任何"无发布时间"告警。

### 7.4 内容质量观察（决定"能不能做观点语料"）

- **`fred_blog`**：WordPress 全文 RSS（`content:encoded`）→ 正文 2.1k~4.7k 字符的宏观分析长文，
  含 `The takeaway`/图表说明；**每篇都带图片**（`has_media=true`，图内信息无法文本抽取，见 TD-31）。
  结论：**可用作 Phase 2 观点语料候选源**（仍需人工看几篇判断"观点密度"）。
- **`fed_press`**：RSS `description` 就是标题本身（74~154 字符）→ **没有正文**。
  结论：**技术可用，但不适合做观点抽取语料**，只适合作"权威事件/公告"触发源。

### 7.5 本轮发现并修复的第 3 个真实缺陷：**HTML 实体未解码**

- 现象：`fred_blog` 的 `content` 里残留 `&#8217;` / `&#8220;` / `&#8230;`（原始 feed 里 63 处），
  会污染正则/LLM 抽取与人工标注；
- 定位：`src/collectors/news.py::strip_html` 只做"去标签 + 压缩空白"，**没做实体解码**；
- 修复：去标签后 `html.unescape`（`&lt;p&gt;` 这类转义文本仍还原为字面量），
  新增回归测试 `test_strip_html_decodes_entities_from_real_feeds`；
- 验证：**只读缓存重放（零网络）**重跑两源 → 残留实体 **34 → 0**，正文变为 `New York Fed’s`。
  对照文件 `logs/_smoke_*_fixed.csv` 与修复前 `logs/_smoke_*.csv` 可直接 diff。

### 7.6 新增的"可审计"设施（本轮为满足硬性要求而加）

| 要求 | 落地 |
|---|---|
| robots 结果记日志 | 新增 `--verify-log`（默认 `logs/rss_verification.log`，**JSONL 追加**）：每源一行，含 `robots_allowed` / `robots_by_rule` / `robots_reason` / `http_status` / `status` / `entries` |
| 请求间隔 ≥1s | `--min-interval`（硬下限 1.0s，给更小值会被抬升并告警）；`RateLimiter` 保证**任意两次请求**（含重试）达标 |
| 非 200 立即失败、不重试超 3 次 | 框架既有 `RetryPolicy(max_attempts=3)`：**仅**对 `429/500/502/503/504` 与传输异常重试；`404/403` 等**立即判失败**，不重试 |
| 每源原始 JSON | `--raw-dir`（默认 `logs/rss_raw`）：`fred_blog` 67 KB / `fed_press` 20 KB，含 robots、缓存 ETag、解析条目与**原始响应体** |
| 关闭落盘的正确姿势 | PowerShell 5.1 会吞掉空字符串参数 → 新增 `off/none/-` 哨兵（`--raw-dir off`） |

### 7.7 结论与等待批准

- **技术层面 2/2 通过**；`fred_blog` 可作语料候选源，`fed_press` 仅建议作事件源；
- `ecb_press` / `yahoo_gold` **本轮未做任何请求**（保持禁用）；
- **尚未**把任何源的 `verified` 置为 `true`，**尚未**做全量抓取 —— 按红线等人工批准。


## 8. 下一步：历史决策记录（第一轮三选项）+ 当前待批准事项

> 第一轮（4 个旧候选全失败）时给出的三选项见下表，**用户已选择 A**（提供已验证 feed 地址），结果见 §7。
>
> **当前待批准（4 项）**：
> ① 是否把 `fred_blog` / `fed_press` 的 `verified` 置为 `true`；
> ② `fed_press` 是否只作"事件/公告源"（正文=标题，不进观点语料）；
> ③ 是否冒烟 `ecb_press` / `yahoo_gold`（现在仍禁用，零请求）；
> ④ 是否批准阶段三全量抓取（≥1 源启用后，100~200 条 → `logs/real_posts_<date>.csv`）。

## 9. 第三轮冒烟（2026-09-13）：`ecb_press` 通过、`yahoo_gold` 被合规拒绝

| 源 | robots.txt | feed HTTP | 条目 | 判定 |
|---|---|---|---|---|
| `ecb_press` | ✅ 允许 | **200** | **5** | ✅ 技术通过 |
| `yahoo_gold` | ❌ **403（不可验证）** | **未请求** | 0 | ❌ **不可用**（fail-closed；只发了 1 次请求，未重试） |

> `yahoo_gold` 与第一轮 `investing.com` 同类：站点对非浏览器 UA 直接 403，**合规上不可用**，
> 保留在清单内但保持 `enabled=false`。

### 9.1 四源最终状态（用户 2026-09-13 逐源批准）

| 源 | enabled | verified | source_type | 依据 |
|---|---|---|---|---|
| `fred_blog` | ✅ true | ✅ true | `NEWS` | 冒烟通过（HTTP 200 / robots 允许 / 长文正文 2.1k~4.6k） |
| `fed_press` | ✅ true | ✅ true | **`EVENT`** | 冒烟通过，但正文=标题 → **只作宏观事件源，不进观点语料** |
| `ecb_press` | ✅ true | ✅ true | `NEWS` | 冒烟通过（HTTP 200 / robots 允许 / 5 条） |
| `yahoo_gold` | ❌ false | ❌ false | `NEWS` | **robots 403 → 合规拒绝，不可用** |

---

## 10. 首轮真实语料抓取（2026-09-13）：25 条 → `logs/real_posts_2026_09_14.csv`

### 10.1 执行

```powershell
python scripts/collect_rss.py --source fred_blog --source ecb_press `
  --no-dry-run --limit 100 --min-interval 1.5 `
  --raw-dir logs/rss_raw --verify-log logs/rss_verification.log `
  --out logs/real_posts_2026_09_14.csv --json-out logs/collect_2026_09_14.json
```

- `fed_press`（EVENT）按用户指令**本轮不进入语料** → 用 `--source` 过滤，**零请求**；
- `yahoo_gold` 禁用，**零请求**。

### 10.2 逐源条数

| 源 | 请求结果 | 条目 | 说明 |
|---|---|---|---|
| `fred_blog` | **HTTP 304**（robots 允许） | **10** | 自上次冒烟以来无更新 → 条目由**本地缓存重放** |
| `ecb_press` | **HTTP 304**（robots 允许） | **15** | 同上 |
| **合计** | 2 次 robots + 2 次条件请求 | **25** | 写入 CSV 25 行 |

> **为什么不是 100 条**：①两个源当时都返回 304（无新内容）；②这两个 feed 自身**只暴露 10 / 15 条**，
> 25 条就是"当前快照的上限"。`--limit 100` 写在命令里，但**受源端可供条目数限制**。

### 10.3 字段完整性（`docs/11 §1.1` 列契约）

| 检查 | 结果 |
|---|---|
| 列数量与顺序 | **与 §1.1 契约逐列一致** ✅（13 列） |
| 全列非空率 | **13/13 列均为 25/25** ✅ |
| `published_at` 缺失 | **0/25**（两源时间都带时区，无猜测） |
| `effective_at != published_at` | **0/25**（未触发任何"回落 collected_at"） |
| 残留 HTML 实体 `&#…;` | **0** |
| 残留 HTML 标签 | **0/25** |
| content 长度 | min **25** / median **82** / max **4562** |
| `has_media=true` | **10/25**（全部来自 `fred_blog`） |
| 时间跨度 | 2026-08-06T13:00Z ~ 2026-09-12T20:00Z |

### 10.4 关键发现（对"能不能做观点语料"影响很大）

1. **`ecb_press` 也是"标题级"源**：15 行里 **`content == title`**（RSS description 就是标题，25~90 字符），
   与 `fed_press` 同类 → **没有可抽取观点的正文**。
2. 因此本次 25 行语料里 **只有 10 行（`fred_blog`）有真正文**（median 2.5k 字符）；
   而 `fred_blog` 的每篇**都带图片**（图内观点无法文本抽取）。
3. **`notes_collect` 列带入了源配置的 `notes`**（即"✅ 冒烟通过…"那段说明），对人工标注是噪音。
4. 本轮 **HTTP 异常 = 0**：没有 4xx/5xx、没有超时、没有触发任何重试；只有 2 次 **304**（条件请求命中）。

### 10.5 本轮顺带修复的第 4 个真实缺陷：**304 会导出 0 条**

- 现象：首次导出时 `fred_blog` 返回 304，`可用条目=0` —— 明明缓存里有内容，语料却空了；
- 定位：`fetch_spec_entries` 的 304 分支直接 `return` 0 条（对"增量落库"正确，对"语料导出"是致命的）；
- 修复：**304 时用本地缓存体重放条目**（状态仍 `not_modified`、`http_status=304`、`warnings` 留痕
  "条目改由本地缓存重放（可复现）"；DB 路径由 `source_record_id`/`content_hash` 幂等去重兜底）；
- 测试：更新 `test_conditional_request_handles_304`，新增 `test_304_without_cached_body_yields_no_entries`；
- 效果：重跑命令即得到 10 + 15 = 25 条（零新增网络请求）。

### 10.6 待用户决定（不自动执行）

1. **语料量**：25 条（其中仅 10 条有正文）＜ 目标的 100 条 → ①再补 1~2 个"全文 RSS"源；②或先按 25 条走标注（观点抽取可用量≈10 条）；
2. **`ecb_press` 定位**：数据表明它也是标题级 → 是否改为 `source_type=EVENT`（与 `fed_press` 一致）；
3. **`notes_collect` 精简**：是否只保留采集侧质量标记（不再拼接源配置 notes）；
4. **是否开始人工标注**（暂不自动进入下一轮）。

## 12. 第四轮：黄金垂类源探测（2026-09-13）—— **0/5 通过**

用户裁决：①补黄金垂类源以凑正文语料；②`ecb_press` 改 `source_type=EVENT` 且 `enabled=false`；
③精简 `notes_collect`（剔除配置类内容）；④标注流程等正文语料 ≥100 条再启动。

### 12.1 探测结果（每源一条命令；robots 前置 + `--min-interval 1.5` + `--feed-limit 5`）

| 源 | robots.txt 结论 | feed HTTP | 条目 | 正文长度中位数 | 判定 |
|---|---|---|---|---|---|
| `kitco_news` | ✅ 允许 | **404** | 0 | — | ❌ 不可用（候选路径无效） |
| `mining_com` | ❌ **403 → 立即跳过** | **未请求** | 0 | — | ❌ 不可用（robots 拒绝） |
| `bullionvault` | ✅ 允许 | **404** | 0 | — | ❌ 不可用（候选路径无效） |
| `goldseek` | ❌ **请求失败（CollectorFetchError）→ fail-closed** | **未请求** | 0 | — | ❌ 不可用 |
| `investing_gold`（新路径 `news_301`） | ❌ **403 → 立即跳过** | **未请求** | 0 | — | ❌ 不可用（robots 拒绝） |

- **无一条通过**，且**都没有走到"判定正文 ≥200 字符"这一步**（要么 robots 拒绝、要么 feed 404）；
- 请求纪律：robots **403/失败 → 立即跳过、不重试**；feed **非 200 → 立即失败、不重试**
  （框架 `RetryPolicy` 仅对 429/500/502/503/504 重试，本节无一条触发）；`investing_gold` 两次冒烟
  （news_285 / news_301）robots 均 403 → 同域 robots 一致，**不再尝试**；
- 留痕：每源 `logs/_smoke_<id>.csv/.json` + `logs/rss_verification.log`（JSONL）+ `logs/rss_raw/`。

### 12.2 结论与建议

1. **黄金垂类公开 RSS 普遍对非浏览器 UA 关闭**（403/404/爬虫墙）→ 继续"猜/换路径"收益很低；
2. 现有**唯一有正文的可用源仍是 `fred_blog`（10 条）**；`fed_press`/`ecb_press` 均标题级（EVENT）；
3. 要凑到「≥100 条正文语料」，现实路径：
   - **(A) 用户提供已验证的"全文 RSS"**（如同类 WordPress 全文 feed：博客/研究机构/交易所公告/券商研究栏目），我逐个冒烟；
   - **(B) 授权一次"入口探测"**：只读站点首页/robots 附近页面，解析 `<link rel="alternate" type="application/rss+xml">` 找官方声明 feed（仍 ≥1.5s、robots 前置、逐条留痕）；
   - **(C) 降低验收目标**：把"观点抽取验收"改到 `fred_blog` 的正文语料量级（如 30 条），并在报告中明确样本量限制；
   - **(D) 换获取方式**（需另行评审合规与成本）：如合规的新闻 API（付费）或已授权的数据服务。
4. 用户裁决 ②③ 已落地：`ecb_press` → `EVENT` + `enabled=false`；`notes_collect` 只保留采集侧质量标记
   （`含图片` / `无可用发布时间` / `缺时区未猜测` / `采集用途=EVENT`），**不再拼接源配置 notes**（TD-34 已解除）。


---

## 14. 第五轮：入口探测（B 方案，2026-09-13）—— **0/3 站点声明 feed**

用户批准 B（限定 3 站、每站只发 1 次首页请求、robots 前置、403 立即跳过、找到后再单独冒烟 1 次）。

### 14.1 执行

```powershell
python scripts/discover_rss_feeds.py --no-dry-run --min-interval 1.5 `
  --url https://www.kitco.com/ --url https://www.gold.org/ --url https://www.gold-eagle.com/ `
  --json-out logs/rss_discovery_20260913.json
```

- 新增工具：`src/collectors/rss_discovery.py`（解析 `<link rel="alternate" type="application/rss+xml|atom+xml">`）
  + `scripts/discover_rss_feeds.py`（CLI）；**默认 dry-run**、robots **前置 fail-closed**、
  **每站只发 1 次首页请求且绝不重试**、间隔 ≥1.5s；9 项 Mock 单测覆盖（零网络）。
- 正式源清单**未改动**。

### 14.2 结果

| 站点 | robots.txt | 首页 HTTP | 发现的 feed 链接 | 判定 |
|---|---|---|---|---|
| `https://www.kitco.com/` | ✅ 允许 | **200** | **0** | 首页未声明 RSS/Atom |
| `https://www.gold.org/` | ✅ 允许 | **200** | **0** | 首页未声明 RSS/Atom |
| `https://www.gold-eagle.com/` | ✅ 允许 | **200** | **0** | 首页未声明 RSS/Atom |

- **未发现任何 feed → 按用户规则不再发"正文冒烟"请求**（本轮流量的 6 次请求：3×robots + 3×首页，全部 ≥1.5s、零重试）；
- 留痕：`logs/rss_discovery_20260913.json`（robots 结论 / 首页状态 / 链接列表 / 限速等待时长）。

### 14.3 局限与下一步（需用户批准）

1. 本工具只解析 **`<link rel="alternate">`**（站点"正式声明"的 feed）；实测这三站都**没有该声明**
   （可能只把 RSS 放在页脚 `<a href>`、或压根不提供 / 由 JS 渲染）。
2. 若要把**页脚 `<a href="…rss…">`** 也纳入探测：需要**再各发 1 次首页请求**（合计 3 次）后扫描 `<a>` 标签
   —— 属于扩大范围，**等用户批准**。
3. **建议加一次"对照测试"**：对已知声明了 feed 的 `https://fredblog.stlouisfed.org/`
   跑一次探测（robots + 首页共 2 次请求），证明"工具有效、不是因为解析器失灵才 0 命中"。
4. 结论方向不变：**黄金垂类公开 RSS 生态确实封闭**（§12 + §14 两轮共 8 个源/站点全未通过）。

---

## 15. NewsAPI 调研（用户委托，2026-09-13）—— **不建议作观点语料主源**

完整报告见 **`docs/13_NewsAPI接入调研方案.md`**（只读官方文档，**未注册账号、未申请 Key、未写接入代码**）。

关键事实（官方原文）：

| 事实 | 对我们的影响 |
|---|---|
| 免费 Developer 计划是 **100 requests / day**（不是"100 次/月"），且 `No extra requests available` | 用户前提有误；但**不是**瓶颈 |
| Developer **仅限开发环境**，**禁止用于 staging/production（含内部）** | ❌ 不能作为正式语料源（合规/许可问题） |
| 免费计划文章 **延迟 24 小时**、只能检索**最近 1 个月** | 需严控时间窗（防未来数据） |
| **任何套餐都不提供全文**：FAQ 明确 "we cannot provide the full content"；`content` **截断到 200 字符** | ❌ **无法支撑观点抽取**（方向/入场/止损/目标都在正文里） |
| `description` 为 snippet（与 content 同为 ~200 字符级） | ⚠️ 只够做"事件/摘要层"，与 `fed_press`/`ecb_press` 同级 |
| 付费 Business $449/月、Advanced $1749/月 | 仍**不含全文** |

**结论**：NewsAPI = "标题/摘要 + URL 发现器"，**不能**替代正文语料源；
若作 URL 发现层，正文仍需自行合规抓取（引入新合规面）。


---

## 16. 附录：第一轮（4 个旧候选全失败时）给出的三选项（历史记录）

> 用户当时选择 **A**（提供已验证 feed 地址），结果见 §7~§15。

| 选项 | 内容 | 我的建议 |
|---|---|---|
| **A. 你提供已验证的 feed 地址** | 你从各站页脚/官方说明里拿到真实 RSS 地址（或直接给我可用的订阅源），我逐个走同一套冒烟（robots → 限速 → 留痕） | ✅ **最快最稳**，推荐 |
| **B. 我按合规方式做"入口探测"** | 只拉取站点**首页/robots.txt 附近**的公开页面，解析 `<link rel="alternate" type="application/rss+xml">` 找官方声明的 feed；仍然 ≥1s 间隔、逐条留痕、遇 robots 禁止立即停 | ⚠️ 可行，但会多访问几个 HTML 页面（**需你显式批准这步**） |
| **C. 换数据源** | 选机器人友好的源（例如公开 RSS 的财经媒体/交易所公告），重新走白名单流程 | ⚠️ 备选 |

补充说明：
- 无论选哪条，**阶段三（`--no-dry-run --limit 100/200` 抓 100~200 条 → `logs/real_posts_<date>.csv`）都必须先有 ≥1 个通过验证的源**；
- 通过验证的源，我会给出「建议启用」清单（含 robots 结论、HTTP 状态、条数、字段完整性），
  **由你人工确认后我才改 `config/rss_sources.json` 的 `enabled/verified`**（严格按你的要求，不自动启用）。
