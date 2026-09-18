# Phase 3 W0-5 特征底座验收报告

日期：2026-09-18  
状态：**PASS（底座范围）**

## 1. 交付边界

- 新增 `feature_sets`、`feature_snapshots`、`feature_values`、`market_regimes` 四表及
  `Regime` / `FeatureSetKind` 枚举。
- 新增 `market-core 1.0.0` 最小生成器，仅用于验证严格 as-of、缺口拒绝、数据版本和回放链。
- 未实现 Regime 识别、Alpha、预测、策略、回测、风控或实盘功能。

## 2. 时间因果与不可变性

- 行情输入必须同时满足 `close_time <= as_of` 和 `effective_at <= as_of`。
- 只使用 5 根连续完整 4h K 线；找不到连续窗口就失败，不插值、不跨缺口。
- `feature_snapshots.max_effective_at <= as_of` 同时由生成器检查和数据库 CHECK 强制。
- 特征定义、快照、规范化单值、Regime 结果全部为 append-only。
- 每份快照绑定 `data_version_id`；DataVersion 保存输入行 ID 和完整 SHA-256。

## 3. 真实 PostgreSQL 验收

- 迁移：`0006_macro_event_vintages -> 0007_phase3_feature_tables` 成功。
- PostgreSQL 结构检查：23 张表、UUID / JSONB / TIMESTAMPTZ / NUMERIC 原生类型和种子幂等通过。
- 首份快照：XAUUSD、4h、`as_of=2026-09-18T01:11:47.362985Z`。
- 输入哈希：`e1a3221c5853855d8d28441caed4f64e86bbba0d5e4f6d2998639c624969733b`。
- 快照 ID：`01a0b211-d243-7894-8a0e-bd75ed626e68`；6 个规范化特征值。
- 同一 `as_of` 复跑：`inserted=false`，返回相同快照 ID。
- 按冻结输入行 ID 回放：`REPLAY PASS`，哈希与六项值完全一致。
- 当前行数：`feature_sets=1`、`feature_snapshots=1`、`feature_values=6`、
  `market_regimes=0`；时间违规为 0。

## 4. 测试门禁

- W0-5 迁移、SQLite→PostgreSQL 工具、防泄漏专项：71 passed。
- 全部 leakage 与不可变性专项：48 passed。
- 全量：2530 passed / 1 skipped。
- `ruff check .`：PASS；`mypy config database src scripts`：PASS（90 个源文件）。
- W0-5 新增文件的 `ruff format --check` 通过；全仓检查仍命中 TD-33 已登记的历史格式债
  （当前 81 个旧文件），未在本工作包混入大范围纯格式改写。

## 5. 已知限制

- `market-core 1.0.0` 只有 close、1/4 步动量、4 步均线偏离、4 步实现波动率和区间比例，
  不是 Phase 3.2 规划的完整 Technical Alpha 特征集。
- 当前只正式写入一份 XAUUSD 4h 验收快照；尚未进行逐时点历史回填。
- `market_regimes` 保持 0 行是刻意的阶段边界；W0-5 PASS 不代表 Phase 3.1 PASS。
