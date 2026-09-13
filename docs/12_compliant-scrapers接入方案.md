# 12_compliant-scrapers 接入方案（调研稿，待确认后再实施）

> 调研时间：2026-09-13｜调研方式：**只读公开资料**（GitHub 公开 API / 官方 RAW 文件 / Apify 商城页与官方文档）。
> **未注册 Apify 账号、未运行任何 Actor、未调用任何付费端点、未产生费用。**
> 你给的链接是 `https://github.com/`（无仓库路径），我按关键词检索到并核实了真实仓库：
> **`Casterdly/compliant-scrapers`**（Apify 商城账号为 **`topsail`**）。

---

## 0. 结论速览（先看这里）

| 问题 | 结论 |
|---|---|
| 它是什么形态？ | **不是** Python 包、**不是** CLI、**不是**独立 API 服务；它是**三个 Apify Actor 的源码**，运行入口是 **Apify 平台**（按次运行、按结果计费） |
| 有黄金支持吗？ | ✅ 有：`topsail/compliant-commodity-intel`（"oil, gold & uranium news"），默认源里含 **Kitco 黄金频道** 与 **BullionVault gold-news** |
| 单次抓多少 / 频率？ | 单次运行默认 **6 源 × 25 条 = 最多 150 条**、约 1 分钟；**没有"频率"概念**（按运行计费，可定时调度） |
| 需要 API Key 吗？ | ✅ 需要 **Apify API Token**（Console → Integrations）；建议环境变量 `APIFY_API_TOKEN` |
| 有免费额度吗？ | ✅ Free 计划 **$5 免费用量、无需信用卡**；compute unit $0.2 |
| 100 条成本？ | **≈ $0.05**（页面标价 from **$0.50 / 1,000 results**）+ 极小的 actor-start 事件费 → 实际 **< $0.06** |
| robots.txt 合规？ | ✅ **代码级 fail-closed 已核实**（见 §2.1）；但 ⚠️ **Apify 平台自身的 Acceptable Use Policy 没有 robots.txt 条款**（合规是作者承诺，不是平台强制） |
| **能直接用于 Phase 2 验收吗？** | ❌ **不建议**：它**只输出标题**（无正文）、**没有发布时间**（只有 `fetched_at` 抓取时刻）、没有作者 → 与我们验收所需的 `content`/`published_at` 字段不匹配（详见 §3） |
| 待你决定 | ①是否注册账号拿 Token；②**用途定位**：走"路线 1（Phase 3 新闻源）"还是"路线 2（自建正文抓取做 Phase 2 语料）" |

---

## 1. 调研结果

### 1.1 项目形态与调用入口（已核实）

| 项 | 事实 | 证据 |
|---|---|---|
| 仓库 | `Casterdly/compliant-scrapers`（Python，**0 star**，创建 2026-06-08，最后推送 2026-06-08，**无 LICENSE 文件**） | GitHub API `repos/Casterdly/compliant-scrapers` |
| 内容 | 三个 Actor 目录 + README：`commodity-intel/`、`crypto-news/`、`ai-research/`（每个含 `.actor/`、`src/`、`requirements.txt`） | GitHub API `contents/` |
| 商城账号 | **`topsail`**（开发者 Connor Teskey），README 明确"Each runs on the **Apify platform** — no setup, **pay-per-result**" | 仓库 README |
| 黄金 Actor | **`topsail/compliant-commodity-intel`**（0 评分、3 个用户、3 个月前最后修改） | 商城页 |
| **调用方式** | **Apify 平台 API**（无本地包/CLI）。官方推荐也可用官方 Python client `apify-client`（自动处理 429 退避） | Apify API 文档（v2，OAS 版本 2026-09-10） |
| 具体端点 | `POST /v2/acts/{actorId}/runs`（启动，返回 runId+datasetId）→ `GET /v2/actor-runs/{runId}`（轮询状态）→ `GET /v2/datasets/{datasetId}/items`（取结果）；`actorId` 可用 `username~actor-name` 形式：**`topsail~compliant-commodity-intel`** | Apify API 文档 §Actor runs / §Datasets |
| 认证 | `Authorization: Bearer <token>`（推荐）或 `?token=`（不安全）；文档注明"public Actor 的资源可匿名读取"，但**运行收费 Actor 必须带 token 才能计费到你的账号** | Apify API 文档 §Authentication |

### 1.2 黄金类目支持（已核实）

- Actor 自带**默认源清单**（源码 `DEFAULT_SOURCES`）：

| 默认源 URL | commodity |
|---|---|
| `https://www.kitco.com/news/category/gold` | **gold** |
| `https://www.bullionvault.com/gold-news` | **gold** |
| `https://oilprice.com/` | oil |
| `https://www.kitco.com/news/category/silver` | silver |
| `https://www.world-nuclear-news.org/` | uranium |
| `https://sprott.com/insights/?category=uranium` | uranium |

- 输入可**自定义**：`{"sources":[{"url":"...","commodity":"gold"}]}`（页面 FAQ 明确"Add any public news listing page with a commodity tag of your choosing. **It must pass the robots.txt check or it will be skipped.**"）；
- 一次默认运行的上限：**每源 25 条**（`extract_headlines(..., max_items=25)`）、默认 6 源 → **≤150 条/次**。

### 1.3 配额与频率

- **没有"抓取频率/订阅"概念**：它是 Apify 上的按次运行（run）；要"每小时更新"就在 Apify 建 Schedule 或自己定时调 API；
- **Apify 平台通用限制**：API 有速率限制（官方要求客户端做**指数退避**：收到 429 时等待 `DELAY`~`2×DELAY` 毫秒并翻倍；官方 Python/JS client 已内置）；Free 计划的**并发运行额度较低**（并发是加购项 `$5 / run`）；
- 单次运行耗时：官方 FAQ"typically completes in about a minute"。

### 1.4 输出字段（**关键限制**）

官方给出的记录结构（6 个字段，**仅标题级**）：

```json
{ "commodity": "gold", "title": "…headline…", "url": "https://…",
  "source": "kitco.com", "fetched_at": "2026-06-08T00:00:00+00:00",
  "extraction": "selector_free_v1" }
```

- `title`：**列表页上的标题文本**（源码过滤：28~200 字符、≥5 个词、剔除导航/页脚/订阅等噪声）；
- **没有正文**、**没有发布时间**、**没有作者**、没有图片标记；
- 明确承诺："它**不**绕过付费墙/登录，只读公开列表页，**不采集个人信息**"（页面 FAQ 原文）。

---

## 2. 合规性评估（逐条回答你的问题）

### 2.1 是否严格遵守 robots.txt？——**是，且是代码级 fail-closed（我读过源码）**

作者的核心实现（`commodity-intel/src/extract.py`）原文要点：

```python
USER_AGENT = "Mozilla/5.0 (compatible; CommodityIntelBot/0.1; +https://apify.com/; respects robots.txt)"

async def robots_allows(client, url, user_agent=USER_AGENT) -> bool:
    """True iff robots.txt allows user_agent on this URL. Fail-closed on any error or
    non-200/404 status — this is the 'compliant' guarantee, made real."""
    ...
    if r.status_code == 404:
        return True          # 没有 robots.txt = 无限制（符合 RFC 语义）
    if r.status_code != 200:
        return False         # 其它状态码 → 拒绝（fail-closed）
    rp = RobotFileParser(); rp.parse((r.text or "").splitlines())
    return rp.can_fetch(user_agent, url)
```

评估：
- ✅ **robots 检查发生在每次运行**（不是一次性白名单），并用**自定义 UA** 自称遵守 robots；
- ✅ **异常/网络错误/非 200/404 状态一律拒绝**（fail-closed）——这是"合规"承诺里最容易注水的一项，它做对了；
- ✅ 只读**公开列表页**；不绕付费墙/登录；不采集 PII（页面 FAQ 明示）；
- ⚠️ **边界**：robots 只检查**列表页**（源 URL）；标题里指向的文章正文**没有抓取**（也就没有对正文页做 robots 检查）；
- ⚠️ **平台层面无强制**：我读了 Apify《Acceptable Use Policy》（最后更新 2026-02-20）全文，**没有 robots.txt 条款**，其禁止清单是 DDoS/群发/欺诈/刷评/造假/SEO 操纵等。因此"robots 合规"是**作者承诺 + 代码实现**，不是 Apify 的强制约束；
- ⚠️ **责任归属**：使用方（我们）仍需自证"只采集公开、非个人、非侵权内容"。

**对照本项目红线**（`docs/11 §1.3`）：公开可访问 ✅｜不绕登录墙 ✅｜不存图片二进制 ✅（它也不提供图片）｜不采个人信息 ✅｜**不采集微博** ✅（无关）。→ **合规风险显著低于自建爬虫**。

### 2.2 API Key：需要，放哪个变量？

- 需要 **Apify API Token**（Console → *Integrations* 获取）；
- **建议变量名**：`APIFY_API_TOKEN`（`.env`；`.env.example` 同步补一行，`config/settings.py` 用 `SecretStr` + `apify_configured` 属性，与 `DEEPSEEK_API_KEY` 完全同构）；
- 安全约定（沿袭现有做法）：token **只进 `Authorization` 头**；`repr()`/日志/异常消息全部掩码（`redact()`）；**不提供 `--token` 命令行参数**；未配置时任何真实调用**直接报配置错误**（Mock/`--dry-run` 路径不受影响）。

### 2.3 免费额度与成本

| 项 | 数值（已核实） | 说明 |
|---|---|---|
| Free 计划 | **$0/月，送 $5 用量，无需信用卡** | 官方 pricing 页 |
| 计费单位 | compute unit **$0.2**；商城 Actor 多按 **PAY_PER_EVENT**（每个 dataset item 一次事件） | 官方 pricing 页 |
| 本 Actor 标价 | **from $0.50 / 1,000 results** + actor-start 事件（一次性，按内存 GB 计，$0.00005~$0.01 级） | 商城页/计价结构 |
| **100 条预估** | **$0.05 ~ $0.06**（≈¥0.4） | 100 × $0.0005 |
| 500 条预估 | $0.25 ~ $0.30 | — |
| 每日 150 条 × 30 天 | ≈ **$2.3/月**（含 start 事件仍 < $3） | 若长期跑，Starter $19/月含 $19 用量更宽裕 |
| 免费 $5 能跑多少 | ≈ **8,000~10,000 条结果**（约 60~100 次 150 条运行） | 足够 Phase 2/3 的验证与小规模日常 |

> 另有 **x402 代理支付**（无需注册账号、有固定 spend cap，但需加密钱包）——本阶段**不建议**使用。

---

## 3. 与本项目验收链路的字段对照（**必须先决的问题**）

`docs/11 §1.1` 要求的标准列 vs 本 Actor 实际输出：

| 我们需要（docs/11） | Actor 能否提供 | 影响 |
|---|---|---|
| `content`（**正文**，含方向/点位） | ❌ **只有 `title`**（28~200 字符标题） | 新闻标题极少含"入场/止损/目标" → 抽取器会大量输出 `UNKNOWN`/无观点，**stance / stop_loss / take_profit 无法有效评估** |
| `published_at`（ISO8601 带时区） | ❌ **只有 `fetched_at`**（= 本次运行时刻） | 会把"抓取时刻"当成"发布时间" → 违反时间因果与防泄漏纪律（`docs/11 §2`、`database/protection.py`）。**我们绝不冒充**（与 `news_collector` 同一纪律：宁可置空 `published_at`，让 `effective_at = collected_at`） |
| `id` | ✅ 可用 `url`（稳定、可去重） | 可用 |
| `source` | ✅ `source`（域名） | 可用（但同一域名多频道需再细分） |
| `author_name` / `author_id` | ❌ **是媒体域名，不是博主** | 与"作者观点评估 + 作者独立性"目标不符（Phase 2 验收的对象是**作者观点帖**） |
| `has_media` | ❌ 不提供 | 只能默认 `false` 并标注"未知" |
| `url` | ✅ | 可用 |
| `commodity` / `extraction` | 额外字段 | 可存入 `raw_json` 供审计 |

### 3.1 两条路线（请你选一条）

**路线 1（推荐）：把它定位为 Phase 3 的"标题级新闻源"，Phase 2 验收语料另找**
- Phase 3（News Alpha）本来就需要"新闻标题 + 时间 + 来源"做事件/情绪特征，标题级刚好够用；
- Phase 2 的真实语料仍按 `docs/11` 用**作者帖子**（人工整理 / 授权导出 / 机构 RSS 正文 feed），验收口径与门槛**完全不变**；
- 工程量：本阶段只做**接入调研与 dry-run 骨架**（可选），不阻塞验收。

**路线 2（不推荐，但在你坚持时可行）：用它做 Phase 2 语料的"URL 发现器"，再自建正文抓取**
- 需新增：①列表标题 → 文章 URL；②**逐条抓正文页**（此时 robots/ToS 责任回到我们，须自查每站 robots）；③**从正文页解析发布时间**（不同站点结构不同，`selector_free` 不覆盖这一步）；
- 需改验收定义：`docs/08 §5` 的字段门槛要重新对齐"标题级 vs 正文级"分层；`docs/10` 的对抗样本规则需在新闻语料上复核；
- 风险：把"外部站点结构变化"引入验收链路，**验收结论的可比性下降**（本条是我最担心的）。

### 3.2 若走路线 1，还有两个次要风险要知道

1. **来源分布**：黄金只有 2 个默认源 → 150 条里黄金可能 50 条左右且**单源占比 > 40%**（`docs/11 §2` 阈值），需自定义补充 robots 允许的黄金源；
2. **成熟度**：仓库 **0 star、无 LICENSE、3 个月未更新**，Actor 只有 **3 个用户** → 生产可用性属"未验证"；建议接入后加**健康检查 + 低于预期条数告警**（复用 `min_records_per_run` 机制）。


---

## 4. 集成方案设计（**待你确认后再写代码**）

> 设计原则：**不新增运行时依赖**（用现有 `httpx` + 框架自带 `Transport`）、**默认关闭**、**dry-run 优先**、
> **测试零网络零花费**（沿用 `tests/conftest.py` 的双层网络守卫 + `MockTransport`）、
> **缓存语义与 `llm_cache` 完全一致**（`auto/readonly/refresh/off`，改 input 自动失效，失败也缓存）。

### 4.1 模块划分（3 个新文件 + 1 处注册 + 1 个种子）

| 文件 | 职责 |
|---|---|
| `src/collectors/apify_client.py` | Apify 平台 API 客户端（启动/轮询/取结果），**与 `DeepSeekClient` 同构**：预算门禁、重试、脱敏、`repr()` 掩码、不 print |
| `src/collectors/apify_cache.py` | 结果缓存（dataset items 落盘），键 = `sha256(actor_id\|input_json\|window)`；**照抄 `llm_cache` 的结构与语义** |
| `src/collectors/compliant_scraper.py` | `CompliantScraperCollector(BaseCollector)`：读配置 → 缓存 → 调 API → 映射 `RawItemPayload` → 交给基类幂等落库 |
| `src/collectors/__init__.py` | 副作用导入（注册采集器） |
| `database/seeds/sources.py` | 新增一行源定义（**默认 `enabled=false`**，与"未验收来源必须禁用"的纪律一致） |

### 4.2 采集器类（与 `BaseCollector` 逐字对齐）

```python
@register_collector
class CompliantScraperCollector(BaseCollector):
    collector_name: ClassVar[str] = "compliant_scraper"        # 与 sources.config_json["collector"] 一致
    source_type: ClassVar[SourceType] = SourceType.NEWS         # 新闻类来源（04 §2）
    max_pages_per_run: ClassVar[int] = 1                        # Actor 一次运行即全量，无需翻页
    min_records_per_run: ClassVar[int] = 20                     # 低于此值 → WARNING（复用基类机制）
    default_retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=3)   # 3 次 + 指数退避

    async def _do_fetch(self, cursor: dict[str, Any] | None, window: CollectWindow) -> FetchPage:
        """① 读 config_json → ② 查缓存 → ③（未命中）调 Apify API → ④ 映射为 RawItemPayload[]"""
```

- `sources.config_json` 契约（**代码里不硬编码任何站点地址**，与 `news_collector` 同纪律）：

```json
{
  "collector": "compliant_scraper",
  "actor_id": "topsail~compliant-commodity-intel",
  "sources": [
    {"url": "https://www.kitco.com/news/category/gold", "commodity": "gold"},
    {"url": "https://www.bullionvault.com/gold-news",  "commodity": "gold"}
  ],
  "max_items_per_source": 25,
  "max_results_per_run": 150,
  "budget_usd": 0.10,
  "cache_dir": "logs/apify_cache",
  "cache_mode": "auto",
  "mock_dataset_path": null
}
```

- **字段映射（对齐 `docs/11 §1.1`，诚实缺位）**：

| Actor 字段 | → `RawItemPayload` | 说明 |
|---|---|---|
| `title` | `title` **且** `content_text=None` | **不把标题伪装成正文** |
| `url` | `source_record_id` + `source_url` | 幂等键（同源内去重） |
| `source`（域名） | `raw_json["source_domain"]` | 参与来源分层 |
| `fetched_at` | `raw_json["fetched_at"]` | **只作审计**，不进 `published_at` |
| `commodity` / `extraction` | `raw_json` | 保留 |
| — | `published_at=None` | **绝不冒充**；`effective_at = max(published_at, collected_at)` 由基类计算 |
| — | `item_type=RawItemType.NEWS` | 04 §5 |

### 4.3 缓存（`llm_cache` 风格）

- 键：`sha256(actor_id | canonical_input_json | window_bucket)`；二级分片 `logs/apify_cache/<前2位>/<key>.json`；
- 模式：`auto` / `readonly`（**未命中直接报错，绝不偷偷花钱**）/ `refresh` / `off`；
- 写入：`.tmp` + `os.replace` 原子替换；**失败也缓存**（`status=failed`，避免重复付费重试）；
- 价值：同一 input 的重复验收与回归测试 **¥0**，且可离线审计"那次到底拿到了什么"。

### 4.4 预算门禁与密钥

- `budget_usd`（默认 0.10）+ `max_results_per_run`（默认 150）：预估 `results × $0.0005`，超限**直接拒绝运行**；
- `config/settings.py` 新增：`apify_api_token: SecretStr | None`、`apify_base_url`、`apify_configured`；
- token 只进 `Authorization` 头；`redact()` 覆盖异常与日志；**无 `--token` CLI 参数**；
- 未配置 token → 任何真实调用抛配置错误（`--dry-run`/Mock 不受影响）。

### 4.5 网络守卫与 Mock 测试（零网络零花费）

沿用既有机制：`tests/conftest.py` 的**双层守卫**（socket 层 + **httpcore 后端层**——后者是实测发现本机代理可绕过 socket 层后补的），
单测一律注入 **MockTransport**（同 `llm_client` 测试的 `ScriptedTransport`）：

| 新增测试 | 覆盖重点（全部 Mock） |
|---|---|
| `tests/unit/test_apify_client.py` | 启动请求契约（路径 / `Authorization` / input JSON）、`user~name` 形式、轮询到 `SUCCEEDED`、`FAILED/ABORTED/TIMED-OUT` 分支、429/5xx 重试与耗尽、401 不重试、预算门禁、`repr`/`stats`/异常**不含 token**、超时 |
| `tests/unit/test_apify_cache.py` | 键确定性与敏感性、四模式语义、原子写无 `.tmp` 残留、损坏文件当未命中、失败条目可读回 |
| `tests/unit/test_compliant_scraper_collector.py` | 字段映射（`title`→`title`、`content_text is None`、`published_at is None`）、`raw_json` 保留 `fetched_at/commodity/extraction`、**幂等**（同 url → duplicate）、低于 `min_records_per_run` → WARNING、`mock_dataset_path` **零网络**、重启后走缓存零网络、未配置 token 时真实路径抛错 |
| `tests/integration/`（可选） | `MockTransport` 走 `run_collector()` 全链路，验证 `collector_runs` 状态与 `raw_items` 落库（不触网） |

### 4.6 `--dry-run` 免付费通路（你点名要的）

1. **Mock 数据路径**：`config_json["mock_dataset_path"]` → 本地 JSON（形如 Actor 输出数组）→ **完全不触网**，可反复跑通"映射 + 落库 + 幂等 + 告警"；
2. **只读缓存路径**：`cache_mode="readonly"` → 用真实运行落下的缓存复跑，未命中即报错；
3. **dry-run 语义**（拟新增薄 CLI `python -m scripts.collect_apify --source <name> --dry-run`）：
   打印「将发送的 input JSON、预估条数与成本、字段映射预览、将被跳过项」，**不 commit、不写缓存**；
4. 真实运行的**唯一入口**：显式 `--no-dry-run` + 已配置 token + 预算内 —— **三重门禁**。

### 4.7 依赖与门禁

- **不新增依赖**（`httpx` 已在 runtime deps；不用 `apify-client`，避免新依赖审批）；
- `ruff` / `mypy` 全绿；测试全离线；**不新增迁移、不改 schema**（源定义走 `sources` 种子）；
- 类属性契约由 `BaseCollector.__init_subclass__` 强制（`collector_name` / `source_type` / `max_pages_per_run`）。

---

## 5. 成本汇总（含免费额度）

| 场景 | 结果数 | 预估费用 | 备注 |
|---|---|---|---|
| 冒烟（1 次默认运行） | ≤150 | **≈ $0.075** | 含 start 事件 |
| Phase 2 语料（路线 2） | 100 | 标题级 $0.05 + **自建正文抓取（自担合规）** | 不推荐 |
| Phase 3 新闻源（路线 1，每日 1 次） | 150/日 | ≈ **$2.3/月** | 免费 $5 可跑约 2 个月 |
| 回归 / 复跑 | 0 | **$0** | `readonly` 缓存复跑 |


---

## 6. 风险与限制（如实登记，拟记入 `TECH_DEBT`）

| # | 风险 | 处置建议 |
|---|---|---|
| 1 | **无正文、无发布时间、无作者**（字段级硬缺口） | 路线 1：只作 Phase 3 新闻源；路线 2：需自建正文抓取并重定义验收口径 |
| 2 | 仓库 **0 star / 无 LICENSE / 3 个月未更新 / Actor 仅 3 用户** | 视为"未验证第三方服务"：默认 `enabled=false`、健康检查 + 条数告警、缓存留档（服务停了也能复跑） |
| 3 | Apify AUP **不含 robots 条款**，合规依赖作者实现 | 保留源码审计记录（§2.1）+ 只调列表页 + 不存 PII |
| 4 | 黄金默认源只有 2 个 → 单源占比可能 **> 40%**（`docs/11 §2` 阈值） | 自定义补充 robots 允许的黄金源；报告披露来源分布 |
| 5 | 按事件计费，重试/超时可能额外产生 start 事件 | 预算门禁 + 运行前后记 `stats`；Free 计划超额度会被阻断（不产生账单） |
| 6 | 未来若需"正文级"新闻，ToS/robots 责任回到我们 | 单独立项（`docs/11 §5` 方案 B 的正文抓取由我们自担） |

---

## 7. 需要你确认的事项（确认后我才开工）

1. **是否注册 Apify 账号并获取 API Token？**
   - 建议：**注册**（Free 计划 $0、无需信用卡、送 $5 用量）；Token 只放 `.env` 的 `APIFY_API_TOKEN`；
2. **是否需要充值？起步多少？**
   - **不需要充值**即可完成本阶段验证（$5 免费额度 ≈ 8,000~10,000 条结果）；
   - 若长期每日运行，届时再考虑 Starter **$19/月**（含 $19 用量，超出按量付费）；
3. **用途定位选哪条路线？**
   - **路线 1（推荐）**：接入为 **Phase 3 新闻源**；本阶段只交付"dry-run 骨架 + Mock 测试"，**不改动 Phase 2 验收口径**；
   - **路线 2**：用它做 Phase 2 语料 → 需新增正文抓取 + 时间解析 + 验收口径分层调整，工程量与合规面都会扩大；
4. （若选路线 2）你希望补充哪些**黄金新闻源 URL**？我会先逐个核对 robots.txt 再入库。

---

## 8. 实施排期（你确认后）

| 步骤 | 交付 | 预估 |
|---|---|---|
| 1 | `apify_client.py` + `apify_cache.py` + 单测（Mock，零花费） | ~1 轮 |
| 2 | `compliant_scraper.py`（`BaseCollector` 子类）+ 字段映射 + 幂等测试 | ~1 轮 |
| 3 | `--dry-run` CLI + Mock 数据通路 + 源种子（`enabled=false`） | ~1 轮 |
| 4 | 你提供 Token → **1 次真实冒烟**（≤150 条，≈$0.075）→ 落缓存 + 体检报告 | 需你执行 |
| 5 | 路线 1：把该接入写入 `docs/05` Phase 3 准备项、`TECH_DEBT` 登记风险 1~6 | 文档 |

> 每步完成后跑全量门禁（`pytest` / `ruff` / `mypy`）；**未确认前不写任何代码**。

---

## 附录：本次调研的证据清单（可复核）

| # | 事实 | 来源 |
|---|---|---|
| 1 | 仓库存在、形态、Python、0 star、2026-06-08 推送、无 LICENSE | `https://api.github.com/repos/Casterdly/compliant-scrapers` |
| 2 | 目录结构（`commodity-intel/`、`crypto-news/`、`ai-research/`、`icons/`） | `.../contents/` |
| 3 | 三个 Actor 与商城 slug、pay-per-result、统一输出字段 | `raw.githubusercontent.com/.../main/README.md` |
| 4 | **robots.txt fail-closed 实现 + UA + 默认黄金源 + 标题抽取规则（28~200 字符、≥5 词、去噪）** | `.../commodity-intel/src/extract.py` |
| 5 | 商城页：**from $0.50 / 1,000 results**、输出字段、FAQ（不改源、不绕墙、robots 检查）、默认 6 源 ≤150 条、约 1 分钟 | `https://apify.com/topsail/compliant-commodity-intel` |
| 6 | API 端点（`/v2/acts/{actorId}/runs`、`/v2/actor-runs/{runId}`、`/v2/datasets/{datasetId}/items`）、`Authorization: Bearer`、429 指数退避 | `https://docs.apify.com/api/v2` |
| 7 | 免费额度 **$5 / 无需信用卡**、compute unit $0.2、并发是加购项 | `https://apify.com/pricing` |
| 8 | Apify AUP **无 robots.txt 条款** | `https://docs.apify.com/platform/legal/acceptable-use-policy` |
| 9 | 本项目采集器契约（`_do_fetch`/`_request`、`RawItemPayload`、`SourceType.NEWS`、`HttpRequest/HttpResponse/RetryPolicy`、`register_collector`） | `src/collectors/base.py`、`types.py`、`transport.py`、`registry.py`、`database/models/enums.py` |

