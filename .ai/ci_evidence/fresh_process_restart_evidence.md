# CONTROL_PLANE_FRESH_PROCESS_RESTART — Human Gate Clearance Evidence

## 1. 旧 orchestrator/launcher 进程终止

- 旧 supervisor 进程 PID: `13204`（已终止）
- 旧 agent launcher `cmd.exe` PID: `15300`, `23056`, `24768`, `26072`, `10936`, `28924`, `27844`（已终止，连同其 powershell/conhost 子进程）
- 终止时间: `2026-09-25T18:22:58+08:00`

## 2. git fetch origin/cline-agent 后的 HEAD 校验

- 本地 HEAD: `439f81c6ae01863d3b59369321a8213e76b969df`
- 远端 HEAD: `439f81c6ae01863d3b59369321a8213e76b969df`
- 一致性: 本地 HEAD == 远端 HEAD ✓

## 3. 全新 supervisor 进程

- 新进程 PID: `28368`
- 启动时间: `2026-09-25T19:47:46+08:00`
- 启动时 HEAD: `439f81c6ae01863d3b59369321a8213e76b969df`

## 4. 全绿 CI run

- run ID: `36130656107`
- HEAD: `439f81c6ae01863d3b59369321a8213e76b969df`
- conclusion: `success`（Python 3.12 / Python 3.13 / PostgreSQL 全绿）

## 5. 声明

Human operator has terminated the old orchestrator, fetched origin, verified local HEAD == remote HEAD, and started a fresh supervisor process. All three requirements of CONTROL_PLANE_FRESH_PROCESS_RESTART are satisfied.

## 6. GOLD-047 历史矛盾（请求 GPT 下发 REPAIR 任务，不直接改历史）

GOLD-047 terminal-state inconsistency (completed + aborted) is a historical immutable record. Human operator requests GPT to issue a dedicated REPAIR task via the normal task queue, rather than modifying history directly.

## 7. PHASE3_3_DATA（冻结，本证据不解除）

PHASE3_3_DATA 属业务数据资格门禁（需合法授权 Author/News 证据），本证据不涉及、不解除。本证据仅用于解除 CONTROL_PLANE_FRESH_PROCESS_RESTART 与请求 GOLD-047 的 REPAIR 任务。
