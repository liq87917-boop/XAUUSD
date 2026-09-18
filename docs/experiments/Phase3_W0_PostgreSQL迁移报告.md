# Phase 3 W0 SQLite → PostgreSQL 迁移报告

> 执行时间：2026-09-18（Asia/Shanghai）
> 状态：**PASS**
> 目标：把已冻结的 W0-1～W0-4 研究事实迁入正式 PostgreSQL 研究库，不覆盖原验收库。

## 1. 输入、目标与恢复点

| 项目 | 值 |
|---|---|
| 源快照 | `database/backups/gold_ai_w0_preflight_closed_20260917.db` |
| 源 SHA-256 | `518AD17599DEEB693FC490B00EBB31C07FEA8B3ED131F9B7E3E05A419CB18B46` |
| 目标 | `postgresql+psycopg://gold_ai_app:***@127.0.0.1:5432/gold_ai_research` |
| PostgreSQL | 16.15，角色默认时区 UTC |
| Alembic | `0006_macro_event_vintages (head)` |
| PostgreSQL 原生备份 | `.postgres-local/gold_ai_research_pre_w0_5.dump` |
| 备份 SHA-256 | `37A98A0299AE45E78117F35B343C3CFB14A88D5DE4FD0A9E708D71166E3A4EBF` |

原 `gold_ai` 验收库没有删除或覆盖；本地 `.env` 只在全部迁移校验通过并完成备份后切换到
`gold_ai_research`。

## 2. 迁移规则

- 默认 dry-run，显式 `--no-dry-run` 才写入；
- 目标 19 张业务表必须全部为空，非空立即拒绝；
- 所有写入与校验位于单一事务，失败整体回滚；
- SQLite naive datetime 按项目契约恢复为 UTC；
- `raw_items.superseded_by_id` 两阶段写入，保持自引用外键；
- 每张表按主键稳定排序，将时间、UUID、Decimal、枚举和 JSON 规范化后计算 SHA-256；
- 源/目标行数和 SHA-256 必须同时一致。

## 3. 逐表对账

| 表 | SQLite 行数 | PostgreSQL 行数 | 规范化内容 SHA-256 | 结果 |
|---|---:|---:|---|---|
| `sources` | 12 | 12 | `cce5d59681d51f8a16e7d28e4ba2c5a3ca7ae23f90af23acdf4237a56f9a5801` | PASS |
| `authors` | 2 | 2 | `11c8e3e8485307afbca6929ae70e06bc3f5785d4f54567c3b797e7e7cc87f8fc` | PASS |
| `author_accounts` | 2 | 2 | `4fc4d56ba20ef2dcb88b9e291cf0d479bdd3866e897c369641e85b75f28a5828` | PASS |
| `raw_items` | 51,941 | 51,941 | `6e445da8b4cff2a9c29d0961968e3f56b8908c886bf57d8b36518183bffee1e0` | PASS |
| `raw_media` | 0 | 0 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | PASS |
| `collector_runs` | 237 | 237 | `0abc28aef27a8865a4220eddfa8367990db0094276b52ec180b5bab8212e31ab` | PASS |
| `processed_items` | 19 | 19 | `f3bc0adb15be87530d43e005e1e027c5b11bf57f52f902bc44fd12ad4b17bc93` | PASS |
| `author_posts` | 19 | 19 | `8a4b797eac3a63d644f4a9ceb44c2a2d2e4918e975a76584d765e7f9327134f8` | PASS |
| `instruments` | 11 | 11 | `01a33613a846fe8142311414ad9890ea2eaae0da34940ff26fa95e08e55cb9e4` | PASS |
| `market_bars` | 47,019 | 47,019 | `ec02a845cb1dcafeb14cb0abb4c9687f4de25ec47b0ea2726e4f4a4a74f8dab2` | PASS |
| `news_events` | 30 | 30 | `28aee6bca52edb089770647778122d553b2b02ae775cd63a4baf9d14483918cd` | PASS |
| `macro_events` | 11,680 | 11,680 | `b93d58ce74b99e9717dd82bf77249d1414885f6b50e44be2f1cc2721267a96b9` | PASS |
| `job_runs` | 0 | 0 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | PASS |
| `data_versions` | 3 | 3 | `aba0639854ddf65429c62936eff75a9b5226e5b9303cb64e3cc89d196b0aa8bc` | PASS |
| `audit_logs` | 6 | 6 | `fb2b5fe08e761f565792cc64117ff6abf0f434191798ef97840b7a6e3ea9fd76` | PASS |
| `author_opinions` | 11 | 11 | `e2651f32db568facd95c54da6c3a5cc390378290a9df75eebc814c24fe7baf79` | PASS |
| `propagation_edges` | 0 | 0 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | PASS |
| `author_skill_snapshots` | 0 | 0 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | PASS |
| `author_weight_snapshots` | 0 | 0 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | PASS |
| **合计** | **110,992** | **110,992** | — | **PASS** |

## 4. 切换后复核

- 应用配置连接 `gold_ai_research`；
- `alembic current = 0006_macro_event_vintages`；
- 关键数量：`raw_items=51,941`、`market_bars=47,019`、`macro_events=11,680`、
  `news_events=30`、`author_opinions=11`、`data_versions=3`；
- 作者标签 dry-run 仍为 31 行 `UNTRUSTED_COLLECTION_TIME`，没有因数据库迁移绕过可信度门禁。
- 最终质量门禁：`ruff` PASS、`mypy` 86 文件 PASS、`pytest` 2649 passed / 1 skipped。

迁移完成只解决数据库一致性，不改变既有数据限制：`GC=F` 仍是期货代理，新闻与作者样本仍不足，
缺可信采集时间的作者帖子仍禁止进入 OOS / Alpha。
