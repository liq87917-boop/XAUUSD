# AI Development Continuity Protocol

本文件定义 XAUUSD 项目的 AI 连续开发协议。GitHub 仓库是项目状态的唯一事实来源，聊天记录只用于交互，不作为开发状态依据。

## 1. 每次“继续开发”前必须读取

1. `.ai/PROJECT_STATE.json`
2. 当前 `.ai/tasks/<current_task>.json`
3. 对应 `.ai/results/<current_task>.json`（若存在）
4. 最近 Git commits / diff
5. `PROGRESS_LOG.md`
6. `TECH_DEBT.md`

禁止只依赖聊天上下文决定下一任务。

## 2. 状态机

### COMPLETED
先 Review task / result / commit / diff / tests。
- Review PASS：更新 PROJECT_STATE，再创建下一任务。
- Review FAIL：创建修复任务，不得直接推进下一阶段。

### FAILED
分析失败原因并创建修复任务；保留原 task/result，不覆盖历史。

### BLOCKED
只处理 blocker。不得绕过 blocker 创建新的正常开发任务。

### RUNNING
不得创建并行冲突任务，等待当前任务结束。

## 3. 恢复任务命名

原任务失败或阻塞后不得修改既有审计历史。
恢复任务使用：
- `GOLD-001-R1`
- `GOLD-001-R2`

恢复任务必须记录 `supersedes` 或等价上下文，说明恢复哪个任务以及原因。

## 4. 人工 Gate

- L1：工程修复 / 测试 / 重构，可自动连续。
- L2：架构变化 / 新数据源 / 新依赖，AI Review 后继续。
- L3：Phase 切换 / 模型实验口径变化，必须用户确认。
- L4：模拟盘 / 实盘 / 风控参数，必须用户明确批准。

## 5. 不变量

- LIVE_TRADING=false。
- 不伪造 published_at / collected_at / effective_at。
- 不绕过数据源授权、robots、证书或安全限制。
- 不用测试集调参追求 PASS。
- Phase 3.3 数据资格未解除前不得进入 Phase 3.4。
- Cline 不执行 git commit/push/reset/rebase/merge；Git 由 Orchestrator 管理。

## 6. Validation 环境

任务 validation 不应使用不确定的 `py` / `python` 解释器。
应由 Orchestrator 使用已确认的项目 Python 解释器绝对路径或项目 venv 路径执行测试。

## 7. 外部阻塞

Quota / rate-limit / provider outage 等外部错误应标记为可重试 blocker，不应立刻消耗所有任务重试次数。
恢复后创建 recovery task 或重新排队。
