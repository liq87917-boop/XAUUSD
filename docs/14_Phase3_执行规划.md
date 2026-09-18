# 14_Phase3 执行规划（Alpha Lab）· V0.2

> **状态（2026-09-18）**：用户已批准 Phase 3.0 按本规划推进。W0-1 行情数据底座已完成受限验收；
> W0-2 已按 Initial Release Only 口径验收通过；W0-3 新闻回填已受限验收通过；W0-4 作者观点链也已受限验收通过；W0-5 特征底座已通过验收。本文件仍不授权提前实施
> Regime、Alpha、Prediction、Strategy 或 Risk。
> **依据**：`docs/01 §7–§10`（Regime / Alpha / 预测 / Meta Ensemble）、`docs/03 §3.5–3.7`（表设计）、`docs/04 §16–23`（字段定义）、
> `docs/05 Phase 3`、`docs/07 Phase 3 Prompt`、`docs/08 §6`（Phase 3 验收）、`.clinerules`（红线）。
> **红线提醒**（全程适用）：`LIVE_TRADING=false`；**禁止 DL / RL**（不引入 torch/transformers）；**禁止未来数据泄漏**；
> 预测 / 策略 / 风险三层解耦（Alpha 层不得含风控逻辑）；LLM 不得直接决定下单；原始数据只增不改。

## 0. 阶段目标（一句话）

用**可科学验证**的方式回答一个问题：**「哪一类信息（作者观点 / 新闻 / 宏观 / 技术）对黄金未来收益有独立、可复现的预测力」**，
并把结论落成可追溯的 `feature_snapshots → alpha_signals → predictions` 事实链；**不产出交易策略、不接实盘**。

范围边界（**不做**）：不做策略/回测/风控（Phase 4–5）、不做实盘（全程）、不做 DL/RL、不做在线学习与自动权重上线（Phase 6）。

## 1. 依赖盘点

### 1.1 Phase 1–2 真实落地状态（核对自代码 + `docs/09` + 迁移 0001–0005）

状态标记：**✅ 已落地**（ORM + 迁移 + 测试齐全）｜**🟡 框架已落地但无数据**（表/采集器在，行数≈0）｜**📐 仅设计未落地**（docs 有设计，无 ORM/迁移）｜**❌ 缺失**（连设计都需补）

| 资产 | 承载 | 状态 | 实际情况（实测） |
|---|---|---|---|
| 标的字典 | `instruments` | ✅ | 11 个：XAUUSD / XAGUSD / COMEX_GC / SGE_AU9999 / DXY / US10Y / US02Y / US10Y_REAL / USDCNY / WTI / BTCUSD |
| 行情 K 线 | `market_bars` | 🟡 **已回填、受限可用** | 47,019 根（XAUUSD/DXY/USDCNY 的 1d、1h、派生 4h）；幂等与 OHLC/时间约束已复核。XAUUSD 实际 provider 为 `GC=F`（COMEX 连续期货代理），且小时级仍有异常缺口，不能无条件用于训练 |
| 行情采集器 | `src/collectors/market.py` | ✅ | 走 **Yahoo Finance chart JSON**（无需 Key，走本项目 Transport，可 Mock/重试），保持 provider 原始粒度 |
| 新闻事件 | `news_events` + `raw_items(NEWS)` | 🟡 **已回填、受限可用** | RSS 白名单已落库 **30 条**（`fred_blog=10`、`fed_press=20`），`raw_items` 与 `news_events` 一一可追溯；当前只是 feed 快照，不是完整历史档案，且 `fed_press` 占 66.7% 已告警 |
| 宏观事件 | `macro_events` + `raw_items(MACRO)` | 🟡 **且有时序隐患** | **2 行**（FRED）；采集器保持"provider 原始粒度（日/月/季）"，**当前落的是观测期日期 → 用于 Macro Alpha 会有前视风险**（见 §4.1-R3） |
| 作者/账号/帖子 | `authors` / `author_accounts` / `author_posts` | 🟡 **真实小样本已入库** | 2 位真实媒体作者、2 个账号、19 条真实中文黄金快讯；Mock 200 条未混入研究库；重复导入 0 新增 |
| 作者观点 | `author_opinions` | 🟡 **基线已产出** | `mock-regex-v1` 从 19 条真实帖子产出 11 条观点；14 帖无观点、3 帖单观点、2 帖各 4 观点均留痕。当前只是规则基线，不代表 LLM v13 或人工金标准 |
| 作者技能/权重快照 | `author_skill_snapshots` / `author_weight_snapshots` | 🟡 | 表与约束在（migration 0005），**计算逻辑未实现**（Phase 3.3 才做） |
| 传播边 | `propagation_edges` | 🟡 | 表在，去重/传播算法在 `src/processors/{similarity,propagation}.py`，**真实语料未入库** |
| 特征 / Regime 表 | `feature_sets` / `feature_snapshots` / `feature_values` / `market_regimes` | ✅ **W0-5 已落地** | migration `0007_phase3_feature_tables`；特征定义/快照/单值与 Regime 追溯结构均有 ORM、迁移、约束与 append-only 守卫；Regime 识别逻辑仍未实现 |
| Alpha / 预测及相邻待建表 | `alpha_models` / `alpha_signals` / `predictions` / `calibration_models` / `ensemble_*` / `author_*_skills` / `experiments` / `walk_forward_*` | 📐 | 仍只有设计，未提前建表 |
| Phase 3 相关枚举 | — | 🟡 | `Regime` / `FeatureSetKind` 已落地；`AlphaType` / `CalibrationMethod` 等仍待对应工作包 |
| 建模依赖 | `pyproject.toml` | ✅ 部分 | 已批准：**numpy / pandas / scikit-learn**（+ httpx / jieba / feedparser）；**未引入**：lightgbm / xgboost / hmmlearn / statsmodels（**需你批准**，见 §4.3） |
| 泄漏门禁与测试基建 | `tests/`（2264 passed / 1 skipped）+ `database/` CHECK 约束 | ✅ | 已有 `leakage` marker 与时间因果守卫（`collected_at <= effective_at`、`ensure_utc_from_database`），Phase 3 可直接复用 |
| 数据库 | PostgreSQL（迁移 0001–0007）+ SQLite（测试） | ✅ | 23 张表；W0-5 四表已加入 append-only 保护，真实研究库有 1 个特征集、1 份快照、6 个规范化值 |

**一句话结论**：Phase 1–2 交付的是**管道与契约**，不是**数据量**。Phase 3 的第一个卡点不是模型，而是
**「行情/宏观/新闻历史几乎没有」+「作者观点不在库里」**——必须先解决数据底座，否则任何 Alpha 都无法做 OOS 验证（`docs/08 §6`）。

### 1.2 逐模块输入清单（对照 `docs/01 §7–§10`）

**1.2.1 Market Regime 引擎**（`docs/01 §7`：状态 = TREND_UP / TREND_DOWN / RANGE / HIGH_VOLATILITY / LOW_VOLATILITY / NEWS_DRIVEN / UNKNOWN）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| XAUUSD 多周期 K 线 | 趋势/波动率判定 | `market_bars`（1d/1h/**4h 需聚合**） | 🟡 10 行、无 4h | **回填历史** + Processor 聚合 4h（关 TD-03） |
| 波动率特征（ATR/realized vol 分位） | HIGH/LOW_VOLATILITY | `feature_values` | 🟡 表已建 | 3.1 增加正式 Regime 特征；W0-5 的 `realized_vol_4` 只用于验证底座 |
| 新闻到达密度（近 N 小时 NEWS 条数） | NEWS_DRIVEN | `news_events`/`raw_items` | 🟡 2 行 | 回填 + 定义密度口径（N=?） |
| 宏观发布日历（近 24h 是否有重要发布） | NEWS_DRIVEN 辅助 | `macro_events` | 🟡 2 行 | 回填（含**发布时刻**） |
| 标的字典/时区 | 对齐与展示 | `instruments` | ✅ | 直接用 |
| **输出** | `market_regimes` | `regime_type/confidence/start_at/end_at/detected_at/model_version/feature_snapshot_id` | 🟡 表已建、0 行 | 3.1 实现识别器后才允许写入 |

**1.2.2 Technical Alpha**（`docs/05`：首期 8 特征 = return / momentum / MA / RSI / MACD / ATR / realized volatility / breakout）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| XAUUSD 历史 K 线（1h/4h/1d） | 全部技术特征 | `market_bars` | 🟡 10 行 | **回填（关键路径）** |
| 特征快照（as_of 严格 ≤ 预测时刻） | 可追溯 | `feature_snapshots`/`feature_values` | ✅ W0-5 底座 | 已有严格 as-of 生成、冻结输入哈希、幂等写入与回放；正式全量技术特征留待 3.2 |
| 前瞻收益标签（未来 1h/4h/1d 收益） | 训练/评估目标 | 由 `market_bars` 派生（**不入原表**） | ❌ | 定义对齐口径（§3.4） |
| 基准对照 | 门槛比较 | 代码内实现 | ❌ | random / always long / momentum / MA（`docs/08 §6`） |

**1.2.3 Macro Alpha**（`docs/01 §8`：实际利率、美元、通胀、Fed）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| FRED 序列：实际利率（如 DFII10）、10Y（DGS10）、美元（DTWEXBGS/DXY）、通胀（CPI）、政策利率（FEDFUNDS） | 宏观因子 | `macro_events` | 🟡 2 行 | **回填 + 补 `released_at`/vintage**（否则前视，见 §4.1-R3） |
| 市场化宏观代理行情：US10Y_REAL / DXY / USDCNY | 与 FRED 互证 | `market_bars` | 🟡 | 随行情回填一起做 |
| FRED API Key | 拉取 | `.env`（禁止硬编码，TD-22） | ❓ | **需你确认可用性** |
| **输出** | `alpha_signals`（`alpha_type=MACRO`）+ `predictions` | 📐 | 随 3.2 建表 |

**1.2.4 Author Alpha**（`docs/01 §6 §8`：`Weight(author | regime, horizon, information_type)`）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| 作者观点（stance/horizon/SL/TP/confidence/information_type/effective_at） | 条件化技能 | `author_opinions` | 🟡 **方案 B 已落地**：19 帖 → 11 条 regex 基线观点 | 保留 parser_version，后续真实 LLM/人工版本只追加不覆盖 |
| 前瞻收益标签（按观点 horizon 计算方向命中） | 技能评估 | 派生 CSV | 🟡 **已实现**：31 条评价行，12 条可标注；缺 horizon 的 10 条观点按 1h/4h/1d 网格分别评价并显式标记 `evaluation_grid`，不冒充作者声明周期 |
| 作者独立性 / 传播去重 | 剔除跟风 | `propagation_edges` | 🟡 | 现有算法可复用于离线评估 |
| 技能快照 6 项（direction/timing/entry/exit/independence/calibration）+ `marginal_alpha` | 技能落库 | `author_skill_snapshots` | 🟡 表在、逻辑空 | 3.3 实现计算与写盘 |
| 条件化技能表（regime × horizon × information_type） | 动态权重依据 | `author_regime_skills` 等 3 张 | 📐 **无字段定义** | 先补 `docs/04` 字段设计再建表 |
| **⚠️ 硬约束** | 样本量：作者级样本 < 30 条观点时**不产出权重**（只报"样本不足"），避免用 3~5 条样本宣称技能 | — | — | 写进验收标准（§2 P3.3） |

**1.2.5 News Alpha**（`docs/01 §8`：新闻/事件对黄金未来收益的影响）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| 新闻/事件（标题+正文+发布时间） | 事件冲击 | `news_events` + `raw_items(NEWS)` | 🟡 2 行 | RSS 白名单回填近 N 天（robots 合规） |
| **标题级情感数据集（HF benchmark）** | **弱监督基线**（2026-09-14 裁决：标题级情感用于 News Alpha） | `logs/archive/phase2_hf_benchmark/` | ✅ 150 条 + 报告 | 直接复用为特征源（**只做特征/弱标签，不作观点金标准**） |
| 事件实体（品种/机构/指标） | 事件归类 | `event_entities` | ❌ **未实现** | 3.3 评估是否需要（可先用关键词/规则打标） |
| 前瞻收益标签 | 训练/评估 | 派生 | ❌ | 同上 |

**1.2.6 Meta Ensemble 基础层**（`docs/01 §10`）

| 输入 | 用途 | 承载 | 现状 | 动作 |
|---|---|---|---|---|
| 各 Alpha 的标准化信号 | 融合输入 | `alpha_signals` | 📐 | 3.2/3.3 产出 |
| Regime 条件 | 条件权重 | `market_regimes` | 📐 | 3.1 产出 |
| 作者权重快照 | 加权 | `author_weight_snapshots` | 🟡 | 3.3 产出 |
| 独立性/分歧（Opinion Dispersion） | 风险提示（**不在此层做仓位**） | 需设计 | ❌ | 离线统计，落 `ensemble_*` |
| **输出** | `ensemble_runs`/`ensemble_components` + **Ablation 报告** | 📐 | 3.4 建表；**动态权重上线留给 Phase 6**（见 §4.2-C1） |

### 1.3 关键数据缺口（**Phase 3 的真正卡点**）

| 缺口 | 现状 | 影响 | 建议动作（需你确认范围） |
|---|---|---|---|
| **G1 行情历史** | `market_bars` 10 行 | Technical Alpha / Regime **完全无法做 OOS** | 回填 XAUUSD 等标的：**1d 尽量长（10 年）**、**1h 近 2 年**（Yahoo 1h 上限≈730 天）、4h 由 1h 聚合；15m/30m 仅近 ~60 天 → 是否把 15m/30m 纳入 Phase 3 需你裁决 |
| **G2 宏观历史与发布时刻** | 2 行、只有观测期 | Macro Alpha 无法验证；且有**前视风险** | 回填 FRED 序列 + **补 `released_at`（vintage）**；需 FRED Key 与序列清单确认 |
| **G3 新闻历史** | 2 行 | News Alpha 样本不足 | RSS 白名单回填近 N 天 + 复用 HF 标题情感弱监督 |
| **G4 作者观点入库** | **方案 B 已落地**：真实 19 帖、11 条 regex 基线观点 | 样本仍远低于作者权重门槛 | 后续增加人工/LLM parser_version，不覆盖基线事实 |
| **G5 标签口径** | **已实现并测试（v2 门禁）** | 严格取 `open_time > effective_at` 的下一根同 horizon K 线；缺口不插值；入场延迟不得超过被评价 horizon；`collected_at` 为回填值的帖子只验证管道 | 当前 31 行全部因 `input_effective_at_fallback` 标记为 `UNTRUSTED_COLLECTION_TIME`，不得进入 OOS / Alpha |

### 1.4 表与枚举缺口（迁移规划，均为**新增**、不动既有表）

| 迁移 | 覆盖子阶段 | 内容 |
|---|---|---|
| `0007_phase3_feature_tables` | 3.1 | 枚举 `Regime` / `FeatureSetKind`；表 `feature_sets` / `feature_snapshots` / `feature_values` / `market_regimes`（`0006` 已由宏观 vintage 使用；字段依 `docs/04 §16–18`，缺的 `feature_values` 先补 `docs/04`） |
| `0008_phase3_alpha_tables` | 3.2 / 3.3 | 枚举 `AlphaType`；表 `alpha_models` / `alpha_signals` / `calibration_models`（`docs/04 §21–22` + 补 `calibration_models` 字段） |
| `0009_phase3_author_conditional_skills` | 3.3 | 表 `author_regime_skills` / `author_horizon_skills` / `author_information_type_skills`（**先补 `docs/04` 设计**） |
| `0010_phase3_ensemble_experiment_tables` | 3.4 | 表 `ensemble_runs` / `ensemble_components` / `experiments` / `experiment_artifacts` / `walk_forward_runs` / `walk_forward_windows` |
| 统一约束（沿用 migration 0005 写法） | 全部 | append-only 守卫、幂等自然键、时间 CHECK（`collected_at <= effective_at` 类）、`confidence ∈ [0,1]`、`created_at >= as_of` |

---

## 2. 里程碑拆解（含验收标准）

> 顺序按你的指定：**3.1 Regime → 3.2 Technical + Macro → 3.3 Author + News → 3.4 Meta Ensemble**。
> 每个子阶段独立验收，**未过验收不进下一个**；每个子阶段都产出「代码 + 单测 + 报告 + PROGRESS_LOG/TECH_DEBT 更新」。

### Phase 3.0 数据底座（**跨阶段前置**，建议随 3.1 一起交付；若你希望单列，它就是 3.0）

目标：把"几乎为空"的输入变成**可回填、可复现、可审计**的训练/评估素材。

| 工作包 | 内容 | 验收标准 |
|---|---|---|
| W0-1 行情回填 | XAUUSD（1d 尽量长 / 1h ≈2 年）+ DXY / US10Y_REAL / USDCNY；Processor 由 1h 聚合 **4h**（TD-03） | **PARTIAL（2026-09-17）**：①幂等复跑 0 新增；②OHLC/时间约束通过；③缺口已审计；④只 append；⑤4h 快照已绑定不可覆盖的 `data_versions` 指纹。限制：`XAUUSD → GC=F` 为期货代理；XAUUSD/DXY 1h 有异常缺口，USDCNY 1h 会话归类退化；下游必须丢弃含缺口 horizon，不插值、不缩窗 |
| W0-2 宏观回填 + **发布时刻** | FRED/ALFRED 初值序列落库；**新增 `released_at` / `vintage_end_at`** | **PASS（2026-09-17，Initial Release Only）**：8 序列 11,680 条；二轮 0 新增；发布前不可见、append-only、密钥脱敏通过。完整修订链未交付，普通“今天最新值”不得用于历史训练 |
| W0-3 新闻回填 | RSS 白名单（`fred_blog`/`fed_press`；`ecb_press` 依既有裁决保持禁用）近 90 天窗口；robots fail-closed | **PASS（受限，2026-09-17）**：30 条落入 `raw_items + news_events`，幂等复跑 0 新增；robots 判定已有逐源留痕；来源分布 10/20，`fed_press=66.7%` 的 >40% 告警已自动触发。限制：RSS 当前快照不等于完整 90 天历史 |
| W0-4 作者观点链 | 采用 `docs/11 §5` 方案 B 正式入库；Regex 作为可复现基线 | **PASS（仅工程管道，2026-09-17）**：19 帖幂等入库、11 观点、31 评价行；v2 门禁确认 31 行均缺可信独立采集时间，统一标记 `UNTRUSTED_COLLECTION_TIME`，因此正式研究可用标签为 0。入场延迟超过对应 horizon 亦拒绝评价。样本不得进入 OOS、作者权重或 Alpha 结论 |
| W0-5 特征底座 | `feature_sets` / `feature_snapshots` / `feature_values` + 生成器 | **PASS（2026-09-18）**：`max_effective_at <= as_of` 由查询、代码与 DB CHECK 三层保证；未来生效行注入与缺口拒绝 leakage 测试通过；同一 `as_of` 二次写入 `inserted=false`，冻结行 ID 回放逐值一致。当前只含 W0-5 最小 `market-core 1.0.0`，不等于 3.2 完整特征工程 |

### Phase 3.1 Market Regime 引擎（规则 + 统计，**不引入 DL/RL**）

**技术路线**（详见 §3.1）：第 1 层规则阈值（趋势/波动/区间）→ 第 2 层统计（波动率分位 / 聚类）→ 输出 6 状态 + `UNKNOWN`；`NEWS_DRIVEN` 用新闻/宏观密度覆盖。

| 交付 | 内容 |
|---|---|
| 迁移 | `0007_phase3_feature_tables`（枚举 + 4 张表；W0-5 已完成） |
| 代码 | `src/alpha/regime.py`（判定器）+ `scripts/build_regimes.py`（回填 CLI，默认 `--dry-run`） |
| 报告 | `docs/experiments/Phase3_1_Regime报告.md`（状态占比、平均持续时长、切换频次、与波动率/事件对照、`UNKNOWN` 比例） |

**验收标准（全部满足才算过）**

1. **覆盖率**：评估期内 ≥ **99%** 的 K 线有 Regime 标签，`UNKNOWN` ≤ 1%（空洞必须解释）；
2. **稳定性**：状态平均持续时长 ≥ **12 根 1h K 线**（防抖动）；日级切换次数 ≤ **6 次/天**；
3. **泄漏门禁（P0）**：`feature_snapshot.as_of <= detected_at` 且 `feature.max_effective_at <= as_of`；**未来注入测试**（人为加入 `detected_at + 1min` 的信息）必须**失败**（`docs/08 §6`）；
4. **可复现**：同一输入重复运行结果逐行一致（幂等自然键 `(instrument_id, start_at)`）；
5. **人工抽检**：你抽 50 个时点盲评，与引擎标签一致率 ≥ **80%**（低于则调整阈值口径并重跑）。

> **机器验收现状（2026-09-18）**：Yahoo `GC=F` 首轮因 `UNKNOWN=15.62%` 被硬门禁拒绝；
> 随后建立不与期货代理拼接的独立 `XAUUSD_DUKASCOPY` 现货序列。733 个原始日文件聚合出
> 11,872 根严格 1h，`UNKNOWN=0.606%`、平均持续 17.25 根、日最大切换 2 次、规则/统计
> 一致率 94.42%、时间因果违规 0，机器门禁全通过并幂等写入 11,872 个 Regime/快照。
> 第二轮 50 点人工盲评 46/50 一致（92% ≥80%）；Phase 3.1 已最终 PASS。详见 TD-41 与
> `docs/experiments/Phase3_1_Regime报告.md`。

### Phase 3.2 Technical Alpha + Macro Alpha 基线（**先各自独立验证，不混合**）

| 交付 | Technical | Macro |
|---|---|---|
| 特征 | return / momentum / MA / RSI / MACD / ATR / realized vol / breakout（首期 8 个，`docs/05`） | FRED 序列差分/水平 + 市场化代理（DXY/US10Y_REAL 收益率与动量） |
| 模型 | ① 单特征规则基线 ② **Logistic Regression**（可解释主基准） ③ LightGBM（**需依赖批准**） | 同左（先线性） |
| 报告 | `docs/experiments/Phase3_2_Technical_Alpha报告.md` | `docs/experiments/Phase3_2_Macro_Alpha报告.md` |

**验收标准**

1. **时间切分**：训练/验证/测试**按时间**切分（禁止 shuffle）；训练窗口与测试窗口之间留 **≥ 1 个 label 长度**的隔离带；
2. **OOS 必做**：每个 Alpha 必须有独立 OOS 区间结果（`docs/08 §6`），并与 **4 个基准**（random / always long / momentum / MA）同表对比；
3. **有效门槛（建议值，待你确认）**：OOS 的 **IC ≥ 0.03** 且 **ICIR ≥ 0.3**（1d horizon），或方向命中率显著优于 random（二项检验 **p < 0.05**）；**命中率必须给 Wilson 95% CI**（沿用 Phase 2 的报告纪律）；
4. **校准**：概率类输出必须做 **Platt 或 Isotonic**，报 **Brier Score + 校准曲线**；未校准的概率不得进 `predictions`；
5. **成本口径**：本阶段只报"信号质量"，**不报 PnL**（PnL/滑点属 Phase 4 回测，避免越界）。

### Phase 3.3 Author Alpha + News Alpha

**Author Alpha**（`Weight(author | regime, horizon, information_type)`）

| 交付 | 内容 |
|---|---|
| 迁移 | `0009_phase3_author_conditional_skills`（3 张条件化技能表，先补 `docs/04` 字段设计） |
| 代码 | `src/alpha/author_alpha.py`（技能计算 + 样本量修正）+ 写 `author_skill_snapshots` / `author_weight_snapshots` |
| 报告 | `docs/experiments/Phase3_3_Author_Alpha报告.md`（技能分布、置信区间、样本量分布、独立性与跟风剔除前后对比） |

**News Alpha**

| 交付 | 内容 |
|---|---|
| 代码 | `src/alpha/news_alpha.py`（事件/标题特征 → 未来收益） |
| 数据 | `news_events`（回填）+ **HF 标题情感 150 条弱监督基线**（只作特征，见 §4.1-R6） |
| 报告 | `docs/experiments/Phase3_3_News_Alpha报告.md`（含"标题级情感 ≠ 观点提取"caveat） |

**验收标准**

1. **样本量硬门槛**：作者级观点样本 **< 30 条** → 该维度**不产出权重**，`author_weight_snapshots` 写 **NULL 并留痕"样本不足"**（禁止用 3~5 条宣称技能）；
2. **技能必须时间外推**：用 `t` 之前（含 `as_of`）的观点算技能，预测 `t` 之后观点的表现；**严禁**用全样本算技能再回测同一样本（那是泄漏）；
3. **样本量修正**：技能值须做收缩（如 Beta 后验均值 / 经验贝叶斯），并报告"原始 vs 收缩后"差异；
4. **基准对比**：条件化权重必须优于"等权全部作者"与"随机作者"两个基准（OOS）；
5. **正交性（News/Author vs 已有 Alpha）**：与 Technical/Macro 信号的 **|Spearman| < 0.5**，否则标记为"信息重复"，不得进 Ensemble 当作独立来源；
6. **News Alpha**：OOS 指标同 §2 P3.2 口径；HF 标题数据**只能**作特征/弱标签，**不得**回灌为观点金标准（2026-09-14 裁决）。

### Phase 3.4 Meta Ensemble 基础层 + 边际贡献（**动态权重上线留 Phase 6**）

| 交付 | 内容 |
|---|---|
| 迁移 | `0010_phase3_ensemble_experiment_tables`（`ensemble_runs`/`ensemble_components`/`experiments`/`walk_forward_*`） |
| 代码 | `src/alpha/ensemble.py`：**固定规则的离线融合 + 基础接口**（后续 Phase 6 换成动态权重，接口不变） |
| 分析 | **Ablation / Counterfactual**：移除某 Alpha / 只用新闻 / 只用技术 / 只用作者，看 OOS 指标变化 → **Marginal Alpha Contribution** |
| 报告 | `docs/experiments/Phase3_4_Meta_Ensemble报告.md`（边际贡献表 + 分散度统计 + 明确"权重未上线"） |

**验收标准**

1. **边际贡献可量化**：每个 Alpha 的 ΔIC / ΔBrier 及其 bootstrap 置信区间；
2. **解耦红线（代码级）**：`src/alpha/ensemble.py` **不得 import 任何 risk/strategy 模块**（单测断言，`docs/01 §11–12` 分层要求）；
3. **融合不越界**：只输出"概率 / 预期收益 / 区间 / NO TRADE 概率"，**不输出仓位、不输出订单**（`.clinerules` 第 5 条）；
4. **消融一致性**：`ensemble_runs` 记录的组件权重与本次运行的实际权重逐字一致（可复现）；
5. **结论诚实性**：若某 Alpha 边际贡献不显著，报告必须写 **"不显著"**（禁止只报最好的组合）。

---

## 3. 关键技术选型

### 3.1 Regime 识别：规则 + 统计（**推荐方案 A+B；HMM 需依赖批准**）

| 方案 | 做法 | 优点 | 风险/成本 | 建议 |
|---|---|---|---|---|
| **A. 规则阈值**（推荐起点） | EMA(20)/EMA(60) 斜率 + ADX(14) ≥ 25 定趋势；ATR(14)/close 的 60 期分位 > 80% / < 20% 定波动；ADX < 20 且价格在带宽内 → RANGE；近 4h 新闻/宏观密度超阈值 → NEWS_DRIVEN 覆盖；指标未成熟（< 60 根）→ UNKNOWN | 零新依赖、完全可解释、可人工抽检、无训练泄漏 | 阈值需标定；边界抖动 | ✅ 3.1 主力 |
| **B. 统计聚类/分位**（推荐叠加） | 对（波动率分位、趋势斜率、ATR、区间宽度）做标准化 → KMeans/GMM 或纯分位切档；**只用滚动/扩展窗口统计量** | 自适应、仍属统计模型（非 DL）、sklearn 已有 | 类标签需人工命名与稳定性检验 | ✅ 作为 A 的校验层（两法一致率 ≥ 80% 才发布） |
| **C. HMM / Markov 切换** | 2~3 状态高斯 HMM（波动率+收益均值） | 天然的"状态持续"假设、切换概率 | **需 `hmmlearn`（新依赖）**；参数选择易过拟合；小样本不稳 | ⏸ 仅当 A+B 不达标且你批准依赖时启用 |
| ❌ DL（LSTM/Transformer） | — | — | **红线：`.clinerules` 禁止 Phase 1–3 引入 DL/RL** | 🚫 不做 |

**状态优先级**（互斥判定，杜绝一个 bar 多状态）：`UNKNOWN`（数据不足）> `NEWS_DRIVEN` > `HIGH_VOLATILITY` > `TREND_UP`/`TREND_DOWN` > `LOW_VOLATILITY`/`RANGE`。

### 3.2 Alpha 建模：线性主基准 → GBDT 增益（**全部监督学习，禁止 DL**）

| 模型 | 角色 | 说明 |
|---|---|---|
| **Logistic Regression**（L2 + 标准化） | **主基准 + 结论载体** | 系数符号/大小可解释，便于你审阅；先跑它再谈 GBDT |
| **LightGBM** | 增益对照 | **需你批准 `lightgbm` 依赖**；必须同表对比 LR，并报特征重要度 |
| **XGBoost** | 可选增益对照 | `docs/07` 写"如依赖环境允许"；需批准 |
| Ridge / 分位回归（可选） | 预期收益 / 区间 | 用于 `predictions.expected_return` 与 interval |
| ❌ 深度学习 / RL | — | 红线禁止 |

**过拟合纪律**：超参搜索空间 ≤ 3 个组合、固定随机种子、指标以 OOS 为准、多模型/多特征比较时做 **BH(FDR) 多重检验校正**、禁止用测试集做任何选择。

### 3.3 每个 Alpha 的独立验证协议（统一口径，逐 Alpha 复现）

| 环节 | 规则 |
|---|---|
| 切分 | **时间序切分**（如 train 60% / valid 20% / test 20%），**禁止 shuffle**（`docs/08 §6`） |
| 隔离带 | 训练与验证/测试之间留 **≥ 1 个 horizon 的 embargo**，避免标签重叠导致的信息渗透 |
| Walk-forward | 滚动窗口（建议 train 24 个月 / test 3 个月 / 步长 3 个月），逐窗口报指标（不只报合并值） |
| 指标 | **IC（Spearman）**、**ICIR = mean(IC)/std(IC)**、方向命中率（+ **Wilson 95% CI**）、**Brier Score**、校准曲线、AUC；回归任务另报 MAE |
| 基准 | random / always long / simple momentum / simple MA（`docs/08 §6`） |
| 稳定性 | 逐窗口指标符号一致率（如 IC > 0 的窗口占比 ≥ 60%） |
| 留痕 | 数据快照 ID + 特征集 ID + 模型版本 + 种子 + 代码 commit 全部写入 `alpha_models`/`experiments` |

### 3.4 标签口径（前瞻收益，**防泄漏的核心**）

```text
entry_at   = 严格晚于 signal.effective_at 的「下一根 K 线」的 open_time
exit_at    = entry_at + horizon（1h / 4h / 1d，按可用数据）
label      = sign( log(close[exit_at] / open[entry_at]) )   # 方向标签
```

1. **绝不使用包含发布时刻的、尚未走完的 K 线**（这是最容易漏的泄漏点）；
2. 特征侧同理：`feature.max_effective_at <= as_of <= signal_at`（`docs/07` 硬要求）；
3. 宏观值只用 **`released_at` 之前可获得**的版本（vintage），禁止用修订后数据；
4. horizon 内数据缺失 → **丢弃并计数**，不插值、不缩短窗口；
5. 多 horizon 标签重叠 → 允许，但 IC 的显著性按 **非重叠样本数** 保守估计。
6. 使用 W0-1 的 `XAUUSD` 序列时，产物元数据必须写明 `provider_symbol=GC=F` 与
   `instrument_proxy=COMEX_CONTINUOUS_FUTURES`；不得将结果表述为现货 XAUUSD 的验证结论。

---

## 4. 风险与未决问题

### 4.1 可能触碰 `.clinerules` 红线的地方（逐条给对策）

| # | 红线 | Phase 3 的具体触发点 | 对策（必须落到测试或检查清单） |
|---|---|---|---|
| **R1** | **未来数据泄漏**（最高优先级） | ① 标签用了"包含发布时刻、尚未走完"的 K 线；② **全样本标准化**（用整段历史的 mean/std 做特征）；③ Regime 阈值用**全样本分位**；④ 宏观用修订后值 | 滚动/扩展窗口统计量；`feature.max_effective_at <= as_of <= signal_at`；**未来注入测试**必须失败；每阶段跑 `leakage` marker 测试 |
| **R2** | 禁止 DL / RL | 想用 LSTM/Transformer 做 Regime 或 Alpha；用 `hmmlearn`（统计模型，但属新依赖） | 依赖白名单 + `pyproject.toml` 显式声明；HMM **需你批准**；不引入 torch/transformers |
| **R3** | **宏观前视**（新发现） | `macro_events` 现只存**观测期**，等于"发布前就知道数字" | W0-2 必须补 `released_at`（ALFRED vintage）；缺失即硬失败；有"发布前取不到"的注入测试 |
| **R4** | 预测/策略/风险解耦 | 在 Alpha 层加"仓位建议"或"止损过滤"；Ensemble 里掺风控 | Alpha 层只输出信号/概率；`ensemble.py` **不得 import** risk/strategy（单测断言） |
| **R5** | LLM 不得决定下单 | 让 LLM 直接产出交易指令；用未经抽取链的 LLM 文本当信号 | LLM 输出只作 `author_opinions` 的**结构化观点**（Prompt v13 冻结）；Alpha 层无下单接口 |
| **R6** | 标题级数据越界 | 把 HF 标题情感标签当作"观点金标准"或写进 `author_opinions` | 只作 **News Alpha 特征/弱标签**；报告与文档均标注任务差异（Phase 2 裁决） |
| **R7** | 原始数据不覆盖 | 回填时 UPDATE/DELETE 既有 bar；"修正"历史宏观值 | append-only + 幂等自然键；修正走新 `data_versions`/新快照 |
| **R8** | 禁止实盘 | 顺手接交易接口（Phase 3 完全不需要） | `LIVE_TRADING=false` 全程不变；Phase 3 不写任何订单代码 |
| **R9** | 测试不得联网 | 回填脚本被测试直接调用真网络 | collectors 一律注入 MockTransport（`tests/conftest.py` socket 守卫）；回填 CLI **默认 `--dry-run`** |

### 4.2 与既有文档的**口径冲突**（需你选一个，我再改文档）

| # | 冲突 | 现状 | 我的建议 |
|---|---|---|---|
| **C1** | **Meta Ensemble 归属** | `docs/01 §10`、`docs/07`（Phase 6 Prompt）、`docs/08 §10` 都把 **Meta Ensemble + 动态权重**放在 **Phase 6**；你的里程碑把它放在 **3.4** | **3.4 只做「基础接口 + 离线 Ablation 报告」**（与 `docs/07` Phase 3 的"ensemble 基础接口"表述一致），**动态权重上线留 Phase 6**；若你要求 3.4 就产出动态权重，我会同步改 docs/05/07/08 的口径 |
| **C2** | **Dashboard** | `docs/05` Phase 3 交付物含 Regime 面板 / Prediction Dashboard / Alpha Lab，但项目**无 web 框架依赖** | Phase 3 用**静态报告**（Markdown + CSV/HTML 快照）替代真 Dashboard（零新依赖）；若要真 Dashboard，需你批准 `fastapi`/`streamlit` |
| **C3** | **15m / 30m horizon** | `docs/05`/`docs/09` 提到多周期；但 Yahoo 的 15m 仅回溯 ~60 天 | Phase 3 只做 **1h / 4h / 1d**；15m/30m 留到有稳定数据源时再评估 |
| **C4** | **表设计缺失** | `feature_values`/`calibration_models`/`ensemble_*`/`author_*_skills`（条件化）**在 `docs/04` 没有字段定义** | 按变更流程**先补 `docs/04` 字段设计**，再写迁移（不跳步骤） |
| **C5** | **`event_entities`** | `docs/03` 列了该表，未设计 | News Alpha 首期用**关键词/规则打标**，不建表；确有需要再评估 |

### 4.3 需要你提供的资源 / 参数（**缺一项就卡住对应子阶段**）

| # | 资源 | 用途 | 影响 |
|---|---|---|---|
| A1 | **行情回填范围**：标的清单 + 周期（1d/1h/4h）+ 时间窗 | 全部 Alpha 的原料 | 无此 → 3.1/3.2 无法开工 |
| A2 | **FRED API Key**（走 `.env`，禁止入库） | 宏观回填 | 无此 → Macro Alpha 只能退化为"市场化代理"版本 |
| A3 | **是否批准新依赖**：`lightgbm` / `xgboost` / `hmmlearn` | 模型对照 / HMM | 不批 → 全部用 sklearn（LR + GBDT 可用 sklearn 的 HistGradientBoosting 替代） |
| A4 | **真实博主语料**（含 `published_at`） | Author Alpha 的样本量 | 无此 → Author Alpha 只能交付"方法论 + 离线框架 + 样本不足结论" |
| A5 | **新闻回填范围**（近 N 天）+ 是否启用 `ecb_press` | News Alpha 样本 | 无此 → News Alpha 只能靠 HF 标题弱监督跑基线 |
| A6 | **人工抽检**：Regime 50 个时点盲评 | 3.1 验收第 5 条 | 无此 → 3.1 只能"机器验收"，不能过人工关 |
| A7 | **门槛确认**：IC ≥ 0.03 / ICIR ≥ 0.3 / Regime 覆盖率 ≥ 99% / 作者样本门槛 30 | 各阶段 PASS/FAIL | 未确认 → 报告无法判 PASS/FAIL |

### 4.4 其他工程风险

1. **统计效力**：1h 数据 2 年 ≈ 1.2 万根/标的，1d 10 年 ≈ 2500 根 → 日频样本对小 IC 的检验力有限（必须报 CI，沿用 Phase 2 纪律）；
2. **多重检验**：反复试特征/参数必然假阳性 → 采用"**预注册**"式流程：先固定"特征集+模型+指标"，只跑一次 OOS，之后任何调整都记 `experiments` 并重跑；
3. **数据量估算**：多标的 × 多周期特征快照会产生数万行 → 建表时预先评估索引（`docs/03 §8` 已给 `(asset, horizon, signal_at)` 等）与未来分区（`docs/03 §9`）；
4. **Windows 环境**：无 HMM/GBDT 系统库依赖（sklearn 自带实现可用）；时区问题已由 `tzdata` 解决；
5. **可复现性**：所有产物必须有"数据快照 ID + 特征集 ID + 模型版本 + 随机种子 + commit"五件套。

---

## 5. 交付物与门禁（每个子阶段统一要求）

| 类型 | 要求 |
|---|---|
| 代码 | 新模块放 `src/alpha/`；脚本放 `scripts/`（**默认 `--dry-run`**）；不改既有表、不动 Phase 1/2 契约 |
| 迁移 | 每个子阶段最多 1 个迁移，含"实现说明"（仿 migration 0005 的写法），append-only + 幂等自然键 + 时间 CHECK |
| 测试 | 单元 + 集成 + **leakage**（`pytest -m leakage`）；覆盖率要求同 Phase 2（核心函数必须有测试） |
| 报告 | `docs/experiments/Phase3_x_*.md`：指标 + Wilson CI + 基准对比 + 失败案例 + 复现命令 + **免责/边界声明** |
| 留痕 | `PROGRESS_LOG.md`（每轮）+ `TECH_DEBT.md`（新债/延期）+ 相关 `docs` 同步 |
| 门禁 | `pytest` 全绿、`ruff check` / `ruff format --check`、`mypy`；**未批准依赖一律不加**；不碰实盘/风控/任务下单 |

---

## 6. 需要你裁决的问题汇总（**先答这些，我才开工**）

| # | 问题 | 选项 |
|---|---|---|
| **D1** | 从哪个子阶段开工？ | ①**先做最小验证探针**（只读 1 年 1d + 2 年 1h，算一次 OOS IC，判断"值不值得投"，零迁移）②直接 3.0 数据底座 + 3.1 Regime ③其他 |
| **D2** | 行情回填范围（A1） | ①1d 10 年 + 1h 2 年（推荐）②只做 1d（快，但日内 horizon 无法验证）③你指定 |
| **D3** | 是否批准新依赖（A3） | ①都不批（全 sklearn）②只批 `lightgbm` ③批 `lightgbm` + `xgboost` ④另加 `hmmlearn` |
| **D4** | Meta Ensemble 归属（C1） | ①3.4 只做基础接口 + Ablation，动态权重留 Phase 6（推荐）②3.4 出动态权重（我同步改 docs/05/07/08） |
| **D5** | Dashboard（C2） | ①静态报告替代（零依赖，推荐）②批准 FastAPI/Streamlit |
| **D6** | Author Alpha 语料（A4） | ①你提供真实博主帖子 ②先用现有 CSV（19 条真实 + 200 条 Mock）跑框架，结论标注"样本不足" |
| **D7** | horizon 取舍（C3） | ①只做 1h/4h/1d（推荐）②坚持含 15m/30m（需另找数据源，工期与合规另议） |

---

## 7. 执行纪律（教训沉淀，2026-09-15）

1. **任何 Alpha 探针都必须先做「小样本验证」**（默认 `--dry-run` + 最小数据量 + OOS IC），
   用**负面结果**决定是否继续投入；严禁先写完整底座再验证价值。
   已据此**停止**技术面 Alpha 的后续投入（D2「10 年日线复核」经用户裁决**跳过**）。
2. **探针结论必须带「能证明什么 / 不能证明什么」**（见《Phase 3 最小验证探针报告》§0 与归档
   `logs/archive/phase3_probe_technical_ic/`）。
3. **每个工作包单独验收**（W0-1 … W0-5 各自留痕 `PROGRESS_LOG.md`），未过验收不进下一步。
4. **取数层两条硬要求**（实测踩过）：① 必须带 `User-Agent`（否则 provider `HTTP 429`）；
   ② `HTTP 200 但 0 根有效 bar` 必须视为失败（裸 `DXY`/`TNX` 就是这种）。
5. **数据库读回值一律用 `ensure_utc_from_database` 归一**（SQLite 返回 naive datetime，
   否则比较/去重会静默出错 —— W0-1 已因此修掉两个真实缺陷）。

---

## 附录 A. Phase 3 **不做**的事（防范围蔓延）


- 不做策略生成、不做回测、不做滑点/手续费建模（Phase 4）；
- 不做风控（单笔/总暴露/回撤/新闻窗口）（Phase 5）；
- 不做在线学习、不做自动权重上线、不做实盘（Phase 6+ / 全程禁止）；
- 不引入深度学习 / 强化学习（`.clinerules` 第 4 条）；
- 不为了让模型在公开数据集上"好看"而改 Prompt（2026-09-14 裁决）；
- 不使用未在 `config/rss_sources.json` 显式启用并通过 robots 验证的采集源。

## 附录 B. 本规划的依据文件

`docs/01 §7–§10`、`docs/03 §3.5–3.7 / §8 / §9`、`docs/04 §16–23`、`docs/05 Phase 3`、`docs/07 Phase 3 Prompt`、
`docs/08 §6`、`docs/10 §7.1`、`docs/11 §1.5–1.6`、`.clinerules`、`pyproject.toml`、`database/models/*`、
`docs/09_Phase1_数据管道总结报告.md`、`docs/experiments/Phase 2 真实语料验收报告.md`、`TECH_DEBT.md`（TD-03/TD-22/TD-31/TD-35–37）。
