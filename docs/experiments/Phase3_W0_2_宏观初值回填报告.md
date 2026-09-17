# Phase 3.0 W0-2 宏观 Initial Release 回填报告

> **结论：PASS（Initial Release Only 口径）**。本报告证明宏观初值在真实发布时间之后才可见，
> 不证明完整修订历史已经重建。普通 FRED 最新修订值禁止用于历史训练。

## 1. 时间与数据契约

- `event_at`：观测所属期，不是发布时间；
- `released_at`：ALFRED `realtime_start` 次日 00:00 UTC（日期精度下的保守边界）；
- `effective_at >= max(released_at, collected_at)`；
- 历史回填使用 `output_type=4`（Initial Release Only）；
- `output_type=1` 的 realtime 边界会被查询窗口裁剪，故未用于修订链重建；
- initial-release 响应的 `realtime_end` 不作为真实 revision end，`vintage_end_at=NULL`。

## 2. 真实联调

| 项 | 结果 |
|---|---|
| Key 加载 | `.env` → `Settings.fred_api_key`（SecretStr） |
| DFF 冒烟 | SUCCESS，6 条，0 重试 |
| Key 泄漏 | 日志 / `source_url` / `raw_json`：0 |
| 传输 | `AiohttpTransport`，显式关闭 session |

## 3. 历史回填

- 区间：2016-01-01 ～ 2026-09-16；
- 序列：CPIAUCSL / PCEPI / PAYEMS / DFF / DFII10 / DGS10 / DTWEXBGS / FEDFUNDS；
- 分块：每块 ≤365 天，8×11 = 88 请求；
- 首轮：新插入 11,674；加 DFF 冒烟 6 条，共 11,680；
- 二轮：`inserted=0`、`duplicate=11,680`、`failed_chunks=0`。

| series | 行数 |
|---|---:|
| CPIAUCSL | 128 |
| DFF | 3,912 |
| DFII10 | 2,676 |
| DGS10 | 2,676 |
| DTWEXBGS | 1,902 |
| FEDFUNDS | 129 |
| PAYEMS | 129 |
| PCEPI | 128 |

DTWEXBGS 的 2016–2018 三个窗口由官方明确返回“不存在 ALFRED 历史”；系统保留空洞并告警，
没有用今天的修订终值回填。

## 4. 数据质量与安全门禁

| 检查 | 结果 |
|---|---:|
| `released_at > effective_at` | 0 |
| `collected_at > effective_at` | 0 |
| 非法 vintage window | 0 |
| 重复自然键 | 0 |
| Key 泄漏记录 | 0 |

第二轮元数据：`logs/macro_backfill/backfill_meta_round2.json`，sha256：
`b94ff0d50533aa90fd72ff43c85a3c382efb0b286a584a6f64e44e1dedfb0b49`。

## 5. 局限与下一步

1. 当前是初值数据，不包含每次后续修订；
2. 完整修订链需单独设计变更矩阵重建，禁止直接相信有界查询的 `realtime_end`；
3. `forecast_value` / `previous_value` 仍为空，不得计算 surprise；
4. W0-3 新闻回填可以启动；Macro Alpha 必须继续使用 as-of API 与 initial-release 口径。

## 6. 复现命令

```powershell
python scripts/backfill_macro_vintages.py --start 2016-01-01 --end 2026-09-16
python scripts/backfill_macro_vintages.py --start 2016-01-01 --end 2026-09-16 --no-dry-run
```