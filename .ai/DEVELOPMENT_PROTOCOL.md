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

## 2.1 Rolling Task Queue

Orchestrator 使用 **3-task rolling queue** 降低任务供给断档：

- Planner 默认维持未来 **3 个非终态任务**；目标数量由 `AI_QUEUE_TARGET_SIZE` 控制，默认 `3`。
- Task 可增加 `depends_on: ["GOLD-xxx"]`；只有全部依赖的 result 都是 `completed` 才能自动开始。
- Task 可增加 `auto_start: false` 或 `requires_human_approval: true` 强制等待人工动作。
- Task 可增加 `human_gate: "L1"|"L2"|"L3"|"L4"`；**L3/L4 永不自动跨越**，队列在该任务处停线。
- 队列严格按任务文件顺序处理：第一个非终态任务若依赖未满足 / 被 BLOCKED / 等待 Human Gate，**不得跳过它执行后续任务**。
- **元数据严格校验（fail-closed）**：Task 元数据违反任一条即停线，绝不静默跳过、绝不继续扫描后续任务：
  - `depends_on` 只能是非空 task_id 字符串，或由非空 task_id 字符串组成的数组（缺字段 / `null` 视为无依赖）；
  - `auto_start` / `requires_human_approval` 必须是真正的 bool（`true` / `false`）；字符串 `"true"`、数字 `1`、`null` 一律非法；
  - `human_gate` 只接受 `L1` / `L2` / `L3` / `L4`（大小写不敏感），未知档位一律非法；
  - result 状态只认 `pending` / `running` / `completed` / `blocked`，其它状态一律视为 unknown 并停线。
- **依赖图 fail-closed 校验**：第一个非终态任务会连同其依赖闭包一起校验 self dependency、
  直接 / 间接依赖环（含多节点环）、缺失依赖、依赖 task 损坏（JSON 损坏 / task_id 与文件名不一致）、
  依赖元数据非法；命中任一项即停线，并给出稳定可读原因，例如
  `dependency cycle: GOLD-018 -> GOLD-019 -> GOLD-020 -> GOLD-018`。
- `blocked` 依赖会阻断所有（含间接）依赖它的任务；上述停线判断只读，**绝不重写历史 result**。
- 队列诊断输出（`queue_diagnostic`）包含 pending count/target、首个停线 task 与停线原因，并仍受 1800s idle throttle 保护。
- 一个任务只有在 validation 全通过、commit 完成且 push 成功后，Orchestrator 才会立即检查下一项；push pending 时先恢复 Git 同步，不得继续后续任务。
- 任一依赖任务为 `blocked` 时，依赖它的后续任务保持等待；不得因为队列中还有其他任务就绕过失败。
- Git 轮询仍默认每 **20 秒**一次；`No runnable task` 日志默认每 **1800 秒**最多打印一次，降日志噪声但不降低检测频率。

任务示例：

```json
{
  "task_id": "GOLD-019",
  "depends_on": ["GOLD-018"],
  "auto_start": true,
  "human_gate": "L1"
}
```

## 2.2 Result 字段与 Git Push 恢复契约

### Result / attempt 字段语义

Cline raw metadata 与 Orchestrator 判定必须**分开保存**，避免「任务已完成却显示 aborted」这类语义歧义：

| 字段 | 位置 | 含义 |
| --- | --- | --- |
| `status` | result | Orchestrator 终态：`completed` / `blocked`（`failed` 只出现在历史 V1 result） |
| `execution_outcome` | result / attempt | Orchestrator 归一化判定：`completed` / `blocked` / `failed` / `waiting_external`，必须与 `status` 一致 |
| `normalized_finish_reason` | result / attempt | 稳定 finish reason（与 `execution_outcome` 同词表） |
| `finish_reason` | attempt | **兼容旧键**；新结果写入归一化值，成功任务不再显示 `aborted` |
| `cline_finish_reason_raw` | attempt | Cline CLI 自报的原始 finish reason（例如 `aborted`），只读审计 |

- 成功判定唯一来源：`cline_exit_code == 0` 且全部 validation `returncode == 0`；
  `execution_outcome` 与该判定**同源**，绝不被 raw finish reason 覆盖。
- 历史 result **不回写**：只有新生成的 result 带新字段；解析旧 result（无新字段）保持兼容。

### push pending / Git 恢复状态机

- 任务只有在 validation 全通过、commit 成功、push 成功之后才算完整收口。
- 本地 result 已写 `completed` 但 push 失败时：
  1. **不重新调用 Cline**（result 已是终态，队列按终态只读跳过）；
  2. runtime 写入 `push_pending` 状态（含 `local_result=completed` / `push_status=pending` /
     `remote` / `branch`），本地 commit 与 result **一律保留**，绝不丢弃；
  3. 本轮不再启动任何任务。
- 下一轮循环第一步固定是 Git sync：`pull --rebase <remote> <branch>` → retry `push <remote> <branch>`：
  - 任一步失败：fail-closed，保留本地 commit/result，下一轮继续恢复；
    **绝不 force push、绝不 reset 已完成的 commit、绝不静默丢弃 result**；
  - 同步成功后清理 `push_pending` 状态并输出 `remote synced` 日志，
    之后 rolling queue 才允许执行后续任务。
- 日志分别输出 `completed locally` / `push pending` / `remote synced` / `rolling queue continue`，
  避免把 Git 同步问题误报为任务 validation 失败。

## 2.3 启动前 bootstrap sync 与版本可见性契约（GOLD-021）

- **启动顺序固定为：`bootstrap sync` → `Python/Orchestrator load`。**
  `start_agent.bat` 必须先以独立进程运行 `py -u orchestrator\bootstrap_sync.py`
  （等价于 `python -m orchestrator.bootstrap_sync`），只有它以退出码 `0` 成功，
  才允许启动 `orchestrator/ai_orchestrator.py`。严禁把代码同步只留在 Python 进程内部
  （那会造成「磁盘代码已更新、运行中的 Orchestrator 仍执行旧代码」的窗口）。
- 启动前同步只允许**安全同步**：`fetch` → 本地落后时 `merge --ff-only` →
  真正分叉时 `rebase <remote>/<branch>`（失败必须 `rebase --abort` 回原状）。
  绝不 `push --force`、绝不 `reset --hard`、绝不 `clean`、绝不自动 `checkout` / `switch`。
- **fail-closed（退出码非 0 ⇒ launcher 不启动 Orchestrator）**：
  dirty worktree（`3`）、当前分支不符（`2`）、git 不可用（`5`）、
  fetch / 远端 ref / HEAD 校验失败（`4`）。任何情况下本地修改与本地 commit 一律保留。
- 同步结果落盘 `.ai/runtime/bootstrap_state.json`（Git 已忽略）。Orchestrator 启动日志打印
  `Branch` / `HEAD`（短 SHA）/ `Bootstrap sync: OK|FAILED result=... branch=...
  head_before=... head_after=... provider=... model=... checked_at=...`，
  用于确认当前进程实际加载的版本；若磁盘 HEAD 与 bootstrap 记录的 `head_after`
  不一致，会额外告警，提示确认版本或重启 launcher。
- 运行中 `pull` 若让磁盘代码前进，Orchestrator 会告警
  「当前进程仍运行启动时加载的代码，请重启 start_agent.bat」——
  运行中的进程**不会**热加载新代码，必须重启 launcher 才生效。
- 启动日志只含 branch / HEAD / provider / model，**绝不打印 API Key 或任何凭据**；
  DeepSeek provider hard-pin 与 provider fail-fast 语义保持不变。

## 2.5 GPT Planner / Executor 职责边界与只读项目快照（GOLD-023）

- **角色唯一性（不可协商）**：GPT 是**唯一**的 **Planner / Reviewer / Architect**；
  Cline 与 DeepSeek 一律只是 **Executor**。Executor 只能消费**已被批准**的
  `.ai/tasks/<task>.json`，不得成为 planner 或 reviewer。
- **Executor 明确禁止**（任一命中即属越权，必须停线并请求 GPT）：
  - 生成 / 拆分 / 追加 follow-on task，或自行补齐 rolling queue（`generate_follow_on_task`、
    `refill_rolling_queue`）；
  - 修改 `.ai/PROJECT_STATE.json`（含 `current_task` / `last_completed_task` /
    `last_reviewed_task` 指针、`blockers`、`task_queue`、`queue_status`）；
  - 决定 Phase 切换（`decide_phase`）；放宽 / 改写 task 的 `acceptance`
    （`loosen_acceptance`）；跨越 L3 / L4 Human Gate（`cross_human_gate`）；
  - 自我 Review 自己的 task（`review_task_result`）或自我提升为 planner
    （`self_promote_to_planner`，架构变更同理属于 Architect 权限）。
- **Executor 允许能力（白名单，其余一律拒绝）**：`consume_approved_task`、
  `run_validation`、`report_task_result`、`recover_interrupted_worktree`（§2.4）、
  `request_planner_decision`。**未知能力 fail-closed**：未登记不等于许可。
- **机器可测试契约**：`orchestrator/planner_snapshot.py` 导出
  `role_contract()`（`schema=gold-ai/role-contract/v1`，含 `planner_only_decisions` +
  `planner_decision_guards` + 全部 `executor_can_* = false`）与
  `executor_capability(capability) -> (allowed, reason)` / `executor_allowed(capability)`。
  任何「Executor 可以规划 / 改状态 / 决定 Phase」的改动都会让契约测试直接失败。
- **Planner 的确定性输入（只读）**：GPT 每次规划前用

  ```text
  python -m orchestrator.planner_snapshot            # 快照写 stdout（纯 ASCII JSON）
  ```

  读取 `schema=gold-ai/planner-snapshot/v2` 快照：`PROJECT_STATE`（原样回显）+
  非终态 task（依赖 / `human_gate` / readiness 与原因）+ result 终态
  （`terminal` / `unresolved` / `unknown` / `missing_results`）+
  最近 reviewed/completed 指针 vs `latest_terminal_result` + 安全 Gate
  （blockers、`invariants`、L3/L4 等待项）+ `role_contract`。
- **一致性诊断（只报告，绝不自动修复）**：`issues` 使用稳定 code ——
  `PROJECT_STATE_POINTER_BEHIND_RESULTS`（指针落后于 results，例如 state 仍 GOLD-017/018
  而 results 已到 GOLD-020）、`PROJECT_STATE_POINTER_AHEAD_OF_RESULTS` /
  `_MISSING` / `_UNKNOWN_TASK`、`QUEUE_DECLARATION_MISMATCH`（`task_queue` 声明与真实
  非终态任务不一致 / 含已终态 / 顺序不符）、`QUEUE_TASK_MISSING`、`UNKNOWN_RESULT_STATUS`、
  `TASK_FILE_INVALID`、`TASK_METADATA_INVALID`、`DEPENDENCY_GRAPH_INVALID`、
  `GATE_INCONSISTENT`、`QUEUE_GATE_STALLED`（ACTIVE 队列却在 L3/L4 处停线，warning）、
  `BLOCKER_STATE_INCONSISTENT`、`SAFETY_INVARIANT_MISSING`、`PROJECT_STATE_UNREADABLE`、
  `GIT_INFO_UNAVAILABLE`（无法把规划绑定到确切代码版本）。
  退出码：`0` 无漂移 / `2` 检出漂移 / `3` `PROJECT_STATE` 不可读。
- **只读保证**：快照**绝不写** `.ai/PROJECT_STATE.json`、`.ai/tasks/**`、`.ai/results/**`
  （模块内不存在任何写入路径，并有源码守卫测试锁定）；发现漂移时**只报告**，
  由 GPT 决定如何修正状态指针、补队列或进入下一 Phase。
- **边界不变**：§2.5 不改变 Phase 3.3 blocker、数据资格 Gate、Human Gate 档位、
  `LIVE_TRADING` 或外部订单开关；快照只读，不与 §2.1 rolling queue、§2.4 恢复状态机冲突。

## 2.6 稳定 Planner Snapshot CLI / JSON 契约（GOLD-024）

- **单一入口**：`python -m orchestrator.planner_snapshot` 是 GPT 规划前**唯一**的只读事实入口；
  CLI 选项固定为 `--root` / `--state` / `--tasks-dir` / `--results-dir` / `--generated-at` /
  `--output`，不含任何 `--plan` / `--next-task` / `--create-task` / `--write-result` /
  `--update-state` 之类规划或写状态开关（契约测试锁定）。
- **版本化**：输出 `schema=gold-ai/planner-snapshot/v2` + 整数 `schema_version=2`；
  新增事实段不改变既有字段语义（`paths` / `pointer` / `queue` / `gates` / `results` / `tasks` /
  `artifacts` / `role_contract` / `issues` / `summary` 全部保留）。
- **规划所需事实一次给全**：`git`（`branch` / `head` / `head_short` / `detached` / `available`，
  只读读取 `<root>/.git` 的 HEAD 与 ref，不执行任何 Git 写操作）、`state`（PROJECT_STATE 摘要：
  phase / status / 三个指针 / declared queue / blocker 与 human gate code / invariants /
  next_action）、非终态 task（依赖 / `human_gate` / readiness 与原因）、result 终态汇总、
  queue 漂移、指针漂移、Gate 与诊断 `issues`。
- **确定性**：所有列表按固定规则排序（见 `determinism.ordering`）；wall-clock **只**出现在
  `generated_at` 审计字段并被明确排除在 facts 之外；`facts_digest` 是状态事实的 sha256，
  相同 Git 树 + 相同输入必然相同，GPT 可独立复算（`snapshot_facts_digest`）。
- **fail-closed 诊断（只报告，绝不自动修复）**：损坏 task / result JSON、未知 task / result
  status、依赖缺失 / 环、PROJECT_STATE 指针落后或超前、queue 声明漂移、Gate / blocker /
  安全不变量不一致一律进入 `issues` 并退出码 `2`；绝不静默修复、绝不补队列、绝不回写历史 result。
- **受控输出 `--output`**：默认只写 stdout（纯 ASCII JSON，`stderr` 只有人类摘要）；
  `--output` 只允许写 `<root>/.ai/runtime/**` 或系统临时目录，写 `.ai/tasks`、`.ai/results`、
  `.ai/PROJECT_STATE.json`、`.git`、`src`、`database`、`config`、`data`、`docs` 等一律 fail-closed
  拒绝（退出码 `4`，且**不写任何文件**）；`..` 逃逸先解析再判定，父目录不存在时**不创建目录**。
  唯一写操作集中在 `orchestrator/planner_snapshot_output.py`（快照模块仍然零写入路径）。
- **退出码**：`0` 无漂移 / `2` 检出漂移 issue / `3` `PROJECT_STATE` 不可读 /
  `4` `--output` 目标被拒。
- **边界不变**：本契约不新增模型 / 供应商 API 或网络依赖，不改变 Phase 3.3 blocker、L3/L4 Gate、
  `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；Executor 依然不具备
  follow-on planning / 改状态 / 决定 Phase 权限（`role_contract` + 契约测试锁定）。

## 2.7 GPT Review Ledger 与状态推进一致性门禁（GOLD-025）

- **规则来源**：§2 状态机要求 `COMPLETED` 先 Review、Review PASS 之后才允许更新
  `.ai/PROJECT_STATE.json` 并创建下一任务。§2.7 把这条规则变成**机器可审计**的记录，
  消除「Executor 写完 `status=completed` 就自称 reviewed」的语义黑洞。
- **契约与位置**：ledger 是**单个**版本化 JSON 文件，canonical 路径
  `.ai/GPT_REVIEW_LEDGER.json`（`schema=gold-ai/gpt-review-ledger/v1` + 整数
  `schema_version=1`）。每条 entry 必须记录：
  - `task_id`；
  - `verdict`（`PASS` / `FAIL`，大小写不敏感，规范化后入库）；
  - `reviewer_role`（必须是 `GPT`）与 `reviewer`（必须是 planner agent；`cline` /
    `deepseek` / 未知身份一律 `EXECUTOR_REVIEW_FORBIDDEN` / `REVIEWER_ROLE_INVALID`）；
  - `reviewed_result`：被 review 的 result **文件 sha256** + `status` + `finished_at`
    （result 一旦被替换 / 改写，旧 review 立即失效 → `REVIEW_RESULT_IDENTITY_MISMATCH`）；
  - `reviewed_commit`：完整 40 位 commit sha + branch（Git identity；sha 非法报
    `REVIEW_COMMIT_INVALID`，branch 与现行事实不符报 `REVIEW_BRANCH_MISMATCH`）；
  - `acceptance_summary`（非空验收结论摘要）与 `reviewed_at`（ISO-8601）。
  ledger 级可选字段 `reviewed_from`（历史覆盖下限 task_id，声明「从哪个任务起纳入 review
  覆盖」）；未知字段一律忽略（向后兼容）。
- **职责边界（不可协商）**：GPT 是**唯一**能写 ledger / 签发 verdict 的角色；
  Executor（Cline / DeepSeek）**只读**：不能写 ledger、不能声明 verdict，更不能凭自己的
  `completed` result 冒充 reviewed。机器可读契约见 `orchestrator/review_ledger.py` 的
  `review_write_contract()`（`schema=gold-ai/review-write-contract/v1`，
  `executor_can_write_ledger=false` / `executor_can_sign_review=false` /
  `ledger_can_cross_human_gate=false` / `ledger_can_transition_phase=false`）与
  `review_authority(agent) -> (allowed, reason)`（未知身份 fail-closed 拒绝）。
  该模块**没有**任何写入 / 追加 / 修复 ledger 的 API。
- **一致性门禁（`python -m orchestrator.review_ledger`，只读）**：
  - `last_reviewed_task` 不得**超前**于 `completed` result
    （`REVIEW_POINTER_AHEAD_OF_COMPLETION`），也不得**落后**于 ledger 最新 PASS review
    （`REVIEW_POINTER_BEHIND_LEDGER`：指针倒退）；
  - 指针指向的任务若已 `completed` 却没有有效 GPT PASS 记录，报
    `COMPLETED_BUT_UNREVIEWED`；`FAIL` 结论不得推进（`REVIEW_POINTER_ON_FAILED_REVIEW`）；
  - rolling queue **首任务**（第一个非终态任务，严格文件顺序，§2.1）的依赖闭包必须已
    `completed`（`QUEUE_HEAD_DEPENDENCY_MISSING` / `_BLOCKED` / `_NOT_COMPLETED`），
    且若协议要求 review（§2 COMPLETED→Review→下一任务）则已 GPT-reviewed
    （`QUEUE_HEAD_DEPENDENCY_UNREVIEWED`）；
  - 覆盖下限（`reviewed_from`，否则最新 PASS review）之前的历史不做追溯误报；
    下限之后出现未 review 的 `completed` 一律报 `COMPLETED_BUT_UNREVIEWED`；
  - 同一 task 多条 review（verdict 冲突）报 `REVIEW_ENTRY_DUPLICATE`，并把该 task 的
    review **整体作废**（绝不挑一个更宽松的 verdict）；非法 / 不完整条目永远不算通过；
  - ledger 缺失 / 损坏 / schema 不认识（`REVIEW_LEDGER_MISSING` / `_UNREADABLE` /
    `_SCHEMA_UNSUPPORTED` / `_ENTRIES_INVALID`）一律 fail-closed：`advance_allowed=false`；
  - queue 首任务处于 L3/L4 报 `QUEUE_HEAD_HUMAN_GATE`（warning）且
    `gate.crosses_human_gate=true`：本工具只报告，**永不跨越**。
- **可选 Git identity 可达性**：`--verify-ancestry` 时对每条有效 review 执行只读
  `git merge-base --is-ancestor <reviewed_sha> HEAD`；不可达报 `REVIEW_COMMIT_UNREACHABLE`，
  无法验证（git 不可用 / 超时 / 非 Git 仓库）报 `REVIEW_ANCESTRY_UNVERIFIABLE`。
  默认**不**运行任何子进程；除该命令外本模块不执行任何 Git / 网络命令。
- **只读保证**：报告只写 stdout（纯 ASCII JSON，`stderr` 只有人类摘要）；模块内不存在任何
  写入路径（源码守卫测试锁定），绝不写 ledger / `PROJECT_STATE` / tasks / results，
  绝不回写历史 result，也绝不 commit / reset / checkout。
- **退出码**：`0` 一致（`advance_allowed=true`）/ `2` 检出漂移或阻塞 /
  `3` `PROJECT_STATE` 不可读。
- **边界不变**：§2.7 不改变 Phase 3.3 blocker、数据资格 Gate、L1~L4 档位、
  `LIVE_TRADING=false`、`ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；ledger 只记录 review
  结论，不能解除 blocker、不能资格化数据、也不能代替人工 Gate。

## 2.8 确定性 GPT Review Binding Manifest（GOLD-031）

- **为什么**：§2.7 的 ledger 需要 GPT 手工填写 `reviewed_result.result_sha256` 与
  `reviewed_commit.sha`。§2.8 提供一个**纯只读、确定性**的事实清单，让 GPT 直接取得这两个
  内容身份，而**不**把「产生 Review 证据」的能力交给 Executor。
- **命令**：`python -m orchestrator.review_binding --task <task_id>`（只读；stdout 是纯 ASCII
  JSON，stderr 只有人类摘要）。契约 `schema=gold-ai/review-binding-manifest/v1` +
  `schema_version=1`，字段顺序固定（`MANIFEST_FIELD_ORDER`），`facts_digest` 只覆盖确定性事实
  （排除 `generated_at` / `facts_digest` / `determinism`），绝不使用 mtime / 当前时间 / 模型输出
  作为内容身份。
- **输出事实**（只产事实，不含任何结论）：
  - `task` / `result` 内容身份：`sha256` + `bytes` 为 **canonical 口径**（目标 commit 里 Git
    存储 / GitHub 提供的字节，与 ledger `reviewed_result.result_sha256` 同口径，可由
    `git cat-file` / 独立 `hashlib` 复算）；`worktree_sha256` + `worktree_bytes` 为本地工作树
    原始字节口径；`worktree_matches_commit` 以 Git blob id 口径判定两者是否一致
    （`core.autocrlf` 等换行转换**不算**漂移）；
  - `commit`：终态完成 commit identity（`sha` / `branch` / `head` / `subject` / `committed_at`），
    只从 **HEAD 可达历史**里按 `ai: complete <task_id>` / `ai: blocked <task_id>`
    （与 `ai_orchestrator.commit_task_result` 同源）解析；
  - `validation`：result 里**已记录**的 validation 摘要（命令 / 返回码 / 是否超时），
    **绝不重新执行**任何测试。
- **fail-closed（稳定 reason code）**：`TASK_ID_INVALID` / `TASK_FILE_MISSING` /
  `TASK_FILE_UNREADABLE` / `TASK_FILE_TASK_ID_MISMATCH` / `RESULT_MISSING` /
  `RESULT_UNREADABLE` / `RESULT_TASK_ID_MISMATCH` / `RESULT_STATUS_UNKNOWN` /
  `RESULT_NOT_TERMINAL` / `GIT_INFO_UNAVAILABLE` / `GIT_LOG_UNAVAILABLE` /
  `COMPLETION_COMMIT_NOT_FOUND` / `COMPLETION_COMMIT_AMBIGUOUS` /
  `COMPLETION_COMMIT_SHA_INVALID` / `RESULT_NOT_IN_COMMIT` / `TASK_NOT_IN_COMMIT` /
  `WORKTREE_COMMIT_MISMATCH`。同一 task 出现多个终态 commit 时**绝不猜测**（歧义必须由
  GPT / 人工裁决）。
- **职责边界（不可协商）**：本工具**只产事实**，绝不签发 verdict、绝不写
  `.ai/GPT_REVIEW_LEDGER.json`、绝不推进任何 review 指针、绝不修改 `PROJECT_STATE`；
  `binding.facts_complete` 只表示「事实是否齐全」，**不是** Review 结论。机器可读契约见
  manifest 的 `authority` 段（`review_authority=gpt_only`、`tool_can_sign_review=false`、
  `tool_can_advance_state=false`、`writes_* = false`）。
- **只读保证**：唯一外部进程调用是**只读** git 白名单（`log` / `ls-tree` / `cat-file` /
  `hash-object`；代码级白名单拒绝其余子命令）；零网络、零数据库、零业务证据写入、
  零模型调用；模块内不存在任何写入路径（源码守卫测试锁定）。
- **退出码**：`0` 事实齐全 / `2` fail-closed（缺事实、漂移或身份不可解析）。
- **边界不变**：§2.8 不改变 planner snapshot、ledger schema、rolling queue、L1~L4 档位、
  Phase 3.3 data blocker 与 `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。

## 2.9 GPT Review Ledger 完整性 / 连续性只读门禁（GOLD-032）

- **为什么**：§2.7 让 review 变成机器可审计的台账，§2.8 让 GPT 能拿到客观内容身份；但台账
  本身仍可能被**静默删改**（删条目 / 换顺序 / 改一个 `result_sha256` 或 `commit.sha`）而与
  客观事实脱节。§2.9 把「台账是否仍与 GOLD-031 manifest 的事实绑定、顺序是否连续」变成
  纯只读、确定性、fail-closed 的验证。
- **命令**：`python -m orchestrator.review_ledger_integrity`（只读；stdout 纯 ASCII JSON，
  stderr 只有人类摘要）。契约 `schema=gold-ai/review-ledger-integrity/v1` + `schema_version=1`。
- **验证内容（全部是客观事实）**：
  - ledger schema 与 entries 结构（复用 `orchestrator.review_ledger.validate_review_ledger`）；
  - `task_id` 唯一（重复 ⇒ `LEDGER_DUPLICATE_TASK`，冲突条目整体作废）；
  - review 顺序（按 `planner.task_id_sort_key` 升序；回退 ⇒ `LEDGER_ORDER_REGRESSION`）
    与 `reviewed_at` 单调性（回退 ⇒ `LEDGER_REVIEW_TIME_REGRESSION`）；
  - 覆盖窗口连续性（`reviewed_from` / 最新 PASS review 到最新条目之间，若有 completed
    result 却没有对应 ledger 条目 ⇒ `LEDGER_CHAIN_GAP`：删项 / 漏项）；
  - 每条有效条目的 `reviewed_result` 与 `reviewed_commit` 必须与
    `orchestrator.review_binding` 复算的 manifest 事实逐项一致：`result_sha256`
    （`LEDGER_RESULT_HASH_MISMATCH`）、`status`（`LEDGER_RESULT_STATUS_MISMATCH`）、
    `finished_at`（`LEDGER_RESULT_FINISHED_AT_MISMATCH`）、`commit.sha`
    （`LEDGER_COMMIT_SHA_MISMATCH`）、`commit.branch`（`LEDGER_COMMIT_BRANCH_MISMATCH`）；
    manifest `facts_complete=false` ⇒ `LEDGER_MANIFEST_FACTS_INCOMPLETE`。
- **fail-closed**：缺失 / 损坏 / schema 不认识 / entries 非法 ⇒ `LEDGER_MISSING` /
  `LEDGER_UNREADABLE` / `LEDGER_SCHEMA_UNSUPPORTED` / `LEDGER_ENTRIES_INVALID`
  （退出码 `3`）；其它完整性漂移退出码 `2`；全部一致退出码 `0`。**绝不自动修复**
  （不重排、不补条目、不改哈希）。
- **职责边界（不可协商）**：本工具**只验证客观事实**，只**原样回显**台账已有 `verdict` /
  `reviewed_at`，**绝不**创建 / 修改 verdict、`acceptance_summary`、`reviewed_at`，绝不写
  `.ai/GPT_REVIEW_LEDGER.json` / `PROJECT_STATE` / tasks / results。机器可读契约见 `authority`
  段（`tool_can_sign_review=false` / `tool_can_repair_ledger=false` /
  `tool_can_advance_state=false` / `writes_*=false`）。
- **只读保证**：唯一外部进程调用经 `orchestrator.review_binding` 的只读 Git 白名单；本模块
  自身不启动任何外部进程，零网络、零数据库、零业务证据、零模型调用；模块内不存在写入路径
  （源码守卫测试锁定）。
- **边界不变**：§2.9 不改变 planner snapshot、ledger schema、rolling queue、L1~L4 档位、
  Phase 3.3 data blocker 与 `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。

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

## 7. 外部 Provider / Billing 阻塞

Orchestrator 必须把 Cline 任务失败与 Provider 外部失败分开处理：

- 默认显式使用 `AI_CLINE_PROVIDER=deepseek`，CLI 调用必须传 `--provider deepseek`；API Key 只保存在 Cline 本地认证配置中，**不得**写入仓库、task、日志或命令行 `--key`。
- `AI_CLINE_MODEL` 默认留空，沿用 Cline CLI 在 DeepSeek Provider 下已经保存的模型；只有显式设置环境变量时才传 `--model`。
- **不可重试外部错误**：余额不足、billing/payment required、quota exhausted、invalid API key、authentication failed、model unavailable。命中后：
  1. 不运行 validation；
  2. 不消耗后续 task retry；
  3. 若工作区已有 Cline 修改，先保存到 `.ai/runtime/recovery/<task-attempt-timestamp>/`（tracked diff + untracked snapshot）；
  4. 清理本次工作区修改；
  5. runtime state 标记为 `waiting_external`；
  6. 立即停止 rolling queue，由人工修复余额/认证/Provider 后重新运行 `start_agent.bat`，同一 task 从原 attempt 重新开始。
- **可重试外部错误**：timeout、rate limit、临时 5xx/provider unavailable、网络 reset/refused 等；可进入受限 retry，但 Cline 非零退出时不浪费时间运行全量 validation。
- 任意失败 attempt 在 rollback 前都应尽量保存 recovery snapshot；recovery artifact 只在本地 runtime，不进入 Git。
- 外部错误绝不能自动降级到 Cline Usage-Billing、其它 Provider、其它模型或静默切换 API Key。

## 2.4 异常中断任务的安全现场恢复状态机（GOLD-022）

- **背景（GOLD-018 真实事故）**：进程被中断 / 重启后，工作区遗留一批**尚未进入 Git** 的修改。
  旧实现只有两种反应：沉默 `deferred`（永久卡线，只能人工 `reset/clean`）或直接 `reset --hard` +
  `clean -fd`（可能抹掉人工修改，连唯一的现场证据一起抹掉）。
- **判定顺序（`recover_interrupted_worktree()`，固定跑在 Git sync 之前）**：
  1) 干净 ⇒ 无需恢复；
  2) 可证明属于「上一次**同一 task** 的中断现场」⇒ snapshot → 校验可恢复 → 清理 → 重试同一 task；
  3) 其它 dirty ⇒ **严格停线**：绝不 snapshot、绝不 reset/clean、绝不写 result、绝不 commit。
- **「可证明」的完整证据链（缺一即停线，绝不自动清理）**：
  - runtime 恰好存在 **1** 条 `status=running` 的 attempt 记录（0 条 / 多条都不算证据）；
  - 该记录的 `task_id` == rolling queue 当前真正会执行的 task
    （依赖未满足 / 被 BLOCKED / 等待 Human Gate ⇒ 队列没有可执行任务 ⇒ 停线）；
  - 记录带 `evidence_schema=gold-ai/interrupted-attempt/v1`（即本版本在**确认 clean 之后**写入的基线）；
  - 记录里的 owner PID 已不存在（Windows 用 `tasklist` 精确匹配 PID 列；**探测失败按「存活」处理**）；
  - 记录里的 `head` / `branch` 与当前完全一致（中断期间没有新 commit、没有切分支）；
  - 该 task 至今没有终态 result（已有 `completed` / `blocked` ⇒ 不可能是「未完成的中断现场」）。
- **现场基线**：每次 attempt 开始（工作区已确认 clean）时写入
  `.ai/runtime/tasks/<task>.json`：`evidence_schema / pid / branch / head / worktree_clean / baseline_at`。
  只有它能作为「这批 dirty 变化发生在本 attempt 期间」的证明。
- **自动清理的唯一前置条件（fail-closed）**：先 `save_recovery_snapshot()`（tracked patch
  `git diff --binary HEAD` + untracked 逐文件副本 + `patch_sha256` / `patch_bytes` /
  `untracked_sha256` / `head` / `branch` / `pid` metadata），再
  `verify_recovery_snapshot(..., require_worktree_match=True)`：
  patch 哈希与字节数一致、untracked 副本哈希一致、patch 能被**反向应用**到当前工作区
  （证明它与现存现场互为逆操作）、untracked 副本与工作区文件内容一致。
  任一项不成立 ⇒ 只停线并保留现场，**绝不清理**；snapshot 写入失败同样只停线。
- **证据一次性消费**：恢复成功后 runtime 状态改写为 `recovered`（含 snapshot 路径），
  该 `running` 证据立即失效——否则同一条证据之后可能被用来清理**另一次**（可能来自人工）的 dirty 现场。
- **审计与边界**：每次判定都写入 `.ai/runtime/recovery/recovery_state.json`（有界、原子写，含
  `decision` / `outcome` / `snapshot` / `snapshot_verified` / `cleaned` / `evidence`）；
  陈旧 lock 原文归档到 `.ai/runtime/recovery/locks/`。恢复**只写 runtime**：绝不写
  `.ai/results/**`、绝不 commit、绝不把恢复当任务完成（恢复后同一 task 仍从 `attempt 1` 开始），
  也绝不触碰 Phase、数据资格 Gate 或 `LIVE_TRADING`。
- **单实例 lock（三态 fail-closed）**：
  - `active`：owner PID 仍在运行（含本进程）⇒ 停线，绝不删除；
  - `unknown`：JSON 损坏、`pid` / `project` / `started_at` 缺失或不可用、`schema` 不认识、
    来源非本项目 ⇒ 停线并给出明确人工动作（确认无其它 Orchestrator 后手工删除 lock 再重启），
    绝不自动删除；
  - `stale`：来源可证明（`project` == 本项目）+ owner PID 已不存在 ⇒ 才归档留痕并清理。
  - 判活探测失败（`tasklist` 不可用 / 超时 / 非 0 退出）一律按 `active` 处理。
- **决策边界**：恢复判定是**纯确定性代码**，绝不调用 Cline、绝不询问 LLM；
  「是否丢弃现场」「是否继续」「下一个 task 是谁」全部由证据与 rolling queue 决定（见 §2.1）。
- **已知范围**：snapshot 记录的是 tracked patch + untracked 文件内容；恢复（`restore_recovery_snapshot`）
  把 tracked 修改还原回工作区，**不**重建原来的 staged 状态；恢复不覆盖历史 result，
  也不改写历史 attempt 语义。

## 2.10 GPT Rolling Queue Autopilot / 三任务前瞻策略

- **目标语义**：除当前正在执行或即将执行的 queue head 之外，GPT Planner 默认维持
  **3 个已批准 follow-on tasks**（`planner_lookahead_size=3`）。因此当
  `GOLD-033` 正在运行时，正常可见队列应至少包含
  `GOLD-034 / GOLD-035 / GOLD-036` 三个后续任务。
- **唯一规划权不变**：只有 GPT 可以创建 / 拆分 / 追加 task、补 rolling queue、
  更新 `.ai/PROJECT_STATE.json` 或签发 review verdict。Cline / DeepSeek 仍只有
  Executor 权限；任何 `generate_follow_on_task` / `refill_rolling_queue` 尝试必须
  fail-closed。
- **自动补给触发**：每个 task 在 validation、commit、push 全部完成并在远端可见后，
  GPT Planner 应重新读取只读 planner facts；若当前任务之后的已批准 follow-on 数量
  小于 3，则补足缺口。云端 Planner 条件检查受平台调度频率限制时，依靠三任务缓冲覆盖
  检查间隔，而不是把规划权下放给 Executor。
- **Hard Gate 处理**：若存在真实 human-only / external-evidence gate，先把仍然合法、
  不跨 Gate 的 blocker-facing / control-plane 工作排在前面；最终可以放置
  `auto_start=false` / `requires_human_approval=true` 的 gated tail。
  若已经完全不存在合法并行工作，允许队列停在线上等待人工，不得为了凑满数量制造无意义任务。
- **排序要求**：可运行任务必须排在 gated/deferred tail 之前。Orchestrator 不得跳过
  queue head，因此禁止把 human-gated task 放在仍可执行任务之前。
- **安全边界**：本策略绝不解除 `PHASE3_3_DATA` blocker，不得进入 Phase 3.4，
  不得伪造 Author/News 授权、published_at / collected_at / effective_at /
  availability / OOS 证据；`LIVE_TRADING=false` 与
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 保持不变。
- **仓库侧职责**：Orchestrator 只负责产生确定性的“队列低水位 / Planner refill required”
  事实与运行时提示；真正的 follow-on task 内容仍必须由 GPT 根据 task/result/commit/diff/tests
  做 Review 后规划。
- **只读事实包（GOLD-034 起）**：`python -m orchestrator.planner_refill_request`
  输出单一确定性 JSON（`gold-ai/planner-refill-request/v1`），**复用**
  `orchestrator.planner_snapshot` 的只读事实，供 GPT 判断“是否缺后续任务、缺几个”：
  - 关键字段：`queue_head` / `follow_on_count` / `lookahead_target`（默认 3）/
    `deficit` / `refill_required` / `hard_gate_tail_allowed` / `latest_completed` /
    `completed_but_unreviewed` / `blockers` / `human_gates` / `safety_invariants` /
    `reason_codes`（稳定词表）/ `facts_digest`（wall-clock 不参与）；
  - `refill_required` 只是 `deficit > 0` 的计数事实，**不是**任务内容、**不是** Phase 决定；
    `hard_gate_tail_allowed=true` 表示只剩 human-only hard gate，GPT 可停在 gated tail；
  - 退出码：`0` 足量无漂移 / `2` 需要 GPT 规划或检出漂移 / `3` PROJECT_STATE 不可读 /
    `4` `--output` 目标被 fail-closed 拒绝；
  - `--output` 只允许写 `<root>/.ai/runtime/**` 或系统临时目录，**绝不**写
    `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`；
  - 本工具**没有任何**生成任务、补队列、改状态或跨 Gate 的能力（Executor 权限不变）。

