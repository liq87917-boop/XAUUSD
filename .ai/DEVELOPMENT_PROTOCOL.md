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

- 成功判定唯一来源（GOLD-046 收紧）：`cline_exit_code == 0`、全部 validation
  `returncode == 0`，**且**权威最终 raw 终态为 `completed`；`execution_outcome` 与该判定
  **同源**。Cline 自报的 raw finish reason 是**唯一权威终态来源**：`aborted` 等非成功
  raw 值（含缺失）绝不被 exit code 0 / validation 通过覆盖成成功。
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
- **Orchestrator 生命周期接入（GOLD-035 起）**：低水位不再等到「队列耗尽」才发现，
  但规划权依旧 100% 属于 GPT：
  - **成功 push 之后立即提示**：每个 task 在 validation + commit + push 全部成功
    （远端可见）后，Orchestrator 立即用同一份只读事实包重算
    `follow_on_count` / `lookahead_target` / `deficit` / `reason_codes`；`deficit > 0` 时输出
    结构化日志
    `GPT_PLANNER_REFILL_REQUIRED context=post_successful_commit_push head=... follow_on_count=... target=... deficit=... reason_codes=[...] executor_can_refill=false`；
    队列足量时该路径保持安静（不制造噪声）；
  - **idle / `No runnable task` 同样提示**：`context=idle_no_runnable_task`；
    队列足量（或只剩合法 human-gated tail）时输出 `GPT_PLANNER_REFILL_SATISFIED`，
    让「状态翻转」在日志里可见；
  - **去重 / 节流**：同一事实状态（`facts_digest` + 计数派生签名）在
    `AI_REFILL_HINT_SECONDS`（默认 `1800`s，下限 = `AI_POLL_SECONDS`）内只提示一次；
    事实一变（完成 / 阻塞 / 队列增删）立即重新提示 —— 既保证「耗尽之前」能看到，
    又绝不每 20 秒刷屏；节流状态跨轮传递，且不改变检测频率；
  - **只读镜像（人工诊断）**：每次实际提示时把同一份事实包写到
    `<root>/.ai/runtime/planner_refill_request.json`（复用
    `orchestrator.planner_snapshot_output` 的 fail-closed 守卫：只允许
    `<root>/.ai/runtime/**` 或系统临时目录；事实不来自本仓库自身 queue 时只记日志、不写文件）。
    **绝不**写 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
    `.ai/GPT_REVIEW_LEDGER.json` / result；
  - **提示只报告**：不生成任务、不补队列、不改项目状态、不决定 Phase、不跨 Gate；
    既不阻塞当前合法任务（`find_next_task_with_reason` 的 ready 判定与停线语义完全不变），
    也绝不绕过 blocked / human-gated 的 queue head —— 提示里只有「缺几个 / 卡在谁 / 为什么 /
    `executor_can_refill=false`」这类事实。

### 2.10.1 三任务前瞻契约的机器化门禁（GOLD-036）

策略不再只靠文字约定，而是由 `orchestrator.planner_autopilot_contract` 固化、
并由 `pytest` 锁定（`tests/unit/test_planner_autopilot_contract.py`、
`tests/integration/test_planner_autopilot_contract_regression.py`）：

- **冻结目标**：`LOOKAHEAD_TARGET=3`、`FOLLOW_ON_TARGET_MINIMUM=1`。任何把
  follow-on target 降为 0 的回归都会被 `FOLLOW_ON_TARGET_BELOW_MINIMUM` 拒绝；
  refill 事实包同时 fail-safe 回落 3，并把同一份契约内嵌到
  `planner_authority.autopilot_contract`（`summary` 另有显式
  `lookahead_target` / `follow_on_target_minimum` / `executor_can_refill=false`）。
- **回归矩阵**：running+3 follow-ons ⇒ `deficit=0`、`refill_required=false`；
  running+2 ⇒ `deficit=1`；running+0 ⇒ `deficit=3`；可运行任务必须排在
  human-gated tail 之前（`RUNNABLE_TASK_AFTER_GATED_TAIL`）；完全 human-only 时
  允许停线（`hard_gate_tail_allowed=true`，计数事实仍保留），但**不得**为凑数量
  制造 filler（`FILLER_TASK_FORBIDDEN`）。
- **Executor 不得变 Planner**：`EXECUTOR_CLAIMS_PLANNER` 拦住任何「Executor 补队列 /
  自我提升为 Planner」的回归；绕过 hard gate（`HUMAN_GATE_BYPASS`）与可执行声明
  不一致（`PLAN_EXECUTABLE_INCONSISTENT`）同样 fail-closed。
- **blocker 下的工作范围**：只要 `PHASE3_3_DATA` 仍在 `PROJECT_STATE.blockers`，
  可被标记可执行的只允许 `BLOCKER_FACING` / `CONTROL_PLANE` /
  `EVIDENCE_PREPARATION`（否则
  `INADMISSIBLE_WORK_CLASS_EXECUTABLE_UNDER_PHASE_BLOCKER`）；Phase 3.4 功能任务
  若被标成可执行，一律 `PHASE34_FEATURE_TASK_EXECUTABLE` fail-closed；被 gate 挡住
  （`auto_start=false` / L3、L4）的功能任务属于合法 gated tail，不算违规。
- **纯只读**：判定只吃调用方传入的 task 结构；该模块不读 / 不写文件、不联网、
  不读数据库、不调用任何 LLM；仓库中**不存在** planner API key，也**不要求**任何
  密钥环境变量。

### 2.10.2 GPT 云端条件检查 vs 本地三任务缓冲（职责边界与异常恢复）

- **云端（GPT Planner）**：唯一规划权。按平台调度做**条件检查**（周期不受仓库控制），
  每次写 task / state 之前必须重新读只读事实（
  `python -m orchestrator.planner_refill_request` 与
  `orchestrator.planner_autopilot_contract` 的审计事实），并以最新事实为准。
- **本地（Orchestrator / Executor）**：只负责「活着的时候看得见缺口」——成功
  commit + push 后与 idle 时输出 `GPT_PLANNER_REFILL_REQUIRED` /
  `GPT_PLANNER_REFILL_SATISFIED`（去重 / 节流 + 只读镜像
  `.ai/runtime/planner_refill_request.json`）；Executor **绝不**补队列、
  **绝不**调用 GPT（或任何 LLM）生成 task。
- **缓冲的角色**：本地已批准任务负责「覆盖云端检查间隔」，而不是把规划权下放；
  队列足量时该提示保持安静。
- **异常恢复**：看到低水位提示后，GPT 重新读取事实再补队列；若期间远端 HEAD 已被
  Executor 推进，以新 HEAD 重新读取事实（绝不 force push / 覆盖刚完成的工作）；
  若已只剩 human-only hard gate，允许队列停在 gated tail 等待人工，**不得**为凑数量
  造任务；Executor 侧发现 `PROJECT_STATE` 指针 / 队列声明漂移（或
  `STATE_RESULT_DRIFT`）时只报告、绝不修复。

## 2.11 GPT Review Backlog 批量绑定事实清单（GOLD-037）

- **为什么**：§2.7 让 review 变成机器可审计台账，§2.8 给出**单个**任务的客观内容身份，
  §2.9 验证台账完整性。但当一个 backlog 里同时挂着**多个**已完成却尚未进入正式台账的任务时，
  GPT 只能逐个手跑 §2.8：既容易漏项，也无法一次性看清「哪些还欠 review、每项的可绑定事实
  是什么、有没有已经绑定 / 已经漂移的项」。§2.11 把这件事变成**一份**确定性 manifest。
- **命令**：`python -m orchestrator.review_backlog`（只读；stdout 纯 ASCII JSON，stderr 只有
  人类摘要）。契约 `schema=gold-ai/review-backlog-manifest/v1` + `schema_version=1`；
  `--output` 复用 §2.6 的受控路径守卫（只允许 `<root>/.ai/runtime/**` 或系统临时目录）。
- **backlog 范围（唯一口径）**：`PROJECT_STATE.last_reviewed_task` 之后**所有**
  `completed` result（排序 `planner.task_rank`）。逐项**复用** §2.8 的
  `orchestrator.review_binding.build_review_binding_manifest` 取得 result SHA-256 /
  完成 commit identity —— **不存在第二套结果 / commit 身份算法**。
- **逐项字段**：`task_id` / `result`（`status` / `finished_at` / `sha256` / `bytes` /
  `worktree_sha256` / `worktree_matches_commit`）/ `commit`（`resolved` / `sha` / `branch` /
  `head` / `subject` / `committed_at`）/ `facts_complete` / `missing_reason_codes` /
  `reason_codes` / `ledger`（条目是否存在、是否唯一合法、hash 与 commit 是否一致）/
  `review_status`。顶层另有 `coverage` / `ledger` / `chain` / `pointer` / `authority` /
  `determinism` / `issues` / `missing_reason_codes` / `reason_codes` / `summary` 与
  `backlog_digest`（`generated_at` 等 wall-clock 不参与 digest）。
- **`review_status` 只有三个客观取值**（绝不是 verdict 词表）：
  - `pending`：无 ledger 条目且 `facts_complete=true`（正常待 GPT review 的项）；
  - `bound`：ledger 条目唯一、合法，且其客观身份与 manifest 事实逐项一致；出现 `bound`
    意味着 `last_reviewed_task` 落后于台账（属**指针漂移**，GPT 应先修指针而非重复 review）；
  - `invalid`：facts 不齐 / 条目非法或重复 / hash 或 commit 漂移，一律 fail-closed。
  输出里**不存在** `verdict` / `acceptance_summary` / `reviewed_at` / `reviewer` /
  `reviewer_role` 字段（见 `FORBIDDEN_MANIFEST_KEYS` 的否定声明）。
- **检测（全部只报告，绝不修复）**：复用
  `orchestrator.review_ledger.validate_review_ledger`（重复 / 非法条目）、
  `orchestrator.review_ledger.pointer_section`（指针 vs ledger vs results）、
  `orchestrator.review_ledger_integrity.chain_section`（台账顺序回退 / 覆盖窗口缺口）。
- **fail-closed（稳定 reason code）**：`LAST_REVIEWED_TASK_POINTER_MISSING`
  （指针缺失 ⇒ backlog 边界不明 ⇒ **不猜**、backlog 为空）、
  `BACKLOG_ITEM_FACTS_INCOMPLETE`（facts 不齐）、`BACKLOG_ITEM_MANIFEST_UNUSABLE`
  （builder 异常）、`BACKLOG_ITEM_LEDGER_INVALID`（条目重复 / 非法）、
  `BACKLOG_ITEM_RESULT_HASH_DRIFT` / `_RESULT_STATUS_DRIFT` / `_RESULT_FINISHED_AT_DRIFT` /
  `_COMMIT_SHA_DRIFT` / `_COMMIT_BRANCH_DRIFT`（身份漂移），以及复用的 `LEDGER_*` /
  `REVIEW_POINTER_*`。任何缺失都给出稳定 code，**绝不猜测 commit / hash、绝不自动修复**。
- **退出码**：`0` fact 齐全且无漂移 / `2` fail-closed / `3` `PROJECT_STATE` 或 ledger
  不可用 / `4` `--output` 目标被 fail-closed 拒绝（此时绝不写文件）。
- **职责边界（不可协商）**：本工具**只产事实**，绝不签发 verdict、绝不写
  `.ai/GPT_REVIEW_LEDGER.json` / `.ai/PROJECT_STATE.json` / tasks / results，绝不推进
  `last_reviewed_task`；`authority` 段硬编码 `review_authority=gpt_only` /
  `tool_can_sign_review=false` / `tool_can_write_review_ledger=false` /
  `tool_can_advance_review_pointer=false` / `writes_*=false`（源码守卫测试锁定）。
- **只读保证**：外部进程调用只经由 §2.8 的只读 Git 白名单；本模块自身不启动任何外部进程，
  零网络、零数据库、零业务证据、零模型调用；唯一写操作是显式 `--output` 到受控路径。
- **回归测试**：`tests/unit/test_ai_orchestrator_review_backlog.py`（18 项：连续 backlog、
  部分已绑定、hash 漂移、commit 不可绑定、台账缺口 / 重复 / 不可用、指针缺失、CLI fail-closed、
  Executor 不能自签 review 的源码守卫）+
  `tests/integration/test_review_backlog_regression.py`（7 项：真实仓库身份由 `git` 独立复算、
  前后零改写、CLI 幂等）。
- **边界不变**：§2.11 不改变 ledger schema、滚动队列、L1~L4 档位、Phase 3.3 data blocker、
  Phase 3.4 边界与 `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。

## 2.12 GPT 写队列前的远端 HEAD 并发保护事实包（GOLD-038）

- **为什么**：§2.10 让 GPT 在队列低水位时补任务、§2.11 让 GPT 一次看清 review backlog，
  但 GPT 运行在云端：**读取事实**与**写队列**之间存在时间窗口，而 Executor 在同一窗口里会
  完成当前任务并 `push` result + completion commit。没有并发保护时，GPT 会基于**过期快照**写
  task / state，覆盖或错判刚完成的工作。§2.12 把这段窗口变成**一份**确定性、只读的事实包。
- **命令**：`python -m orchestrator.planner_mutation_precondition`（只读；stdout 纯 ASCII JSON，
  stderr 只有人类摘要）。契约 `schema=gold-ai/planner-mutation-precondition/v1` +
  `schema_version=1`；`--expected-head-sha <sha>`（别名 `--expect-head`）传入「上次读到的远端
  HEAD」；`--output` 只允许 `<root>/.ai/runtime/**` 或系统临时目录。
- **事实（复用，不复制）**：`branch` / `observed_head_sha` 直接复用 §2.5 planner snapshot 的
  只读 Git 段；`queue_head` / `task_queue` / `refill.facts_digest` 复用 §2.10 refill 事实包；
  `review_backlog.backlog_digest` 与 formal review backlog 指针复用 §2.11 manifest；
  `state`（blob sha256 + 解析 digest）、`tasks_digest`、`results_digest` 是本模块新增的
  **内容事实**（文件字节 sha256 的确定性摘要）。**不存在第二套 readiness / 依赖 / 终态 /
  task 资格 / commit 身份算法**。
- **稳定 `precondition_digest`**：只覆盖事实（`generated_at` / `precondition_digest` /
  `determinism` 被排除），相同仓库事实必然相同 digest；HEAD、results、tasks、state 或 backlog
  指针任一变化都会让 digest 变化 ⇒ **旧 precondition 自动失效**。
- **fail-closed（稳定 reason code，任一命中即禁止 planner mutation）**：
  - `expected_head_sha` 与 `observed_head_sha` 不一致 ⇒ `STALE_REMOTE_HEAD`
    （`planner_mutation.allowed=false` / `forbidden=true` / `requires_reread=true`）；
  - `expected_head_sha` 形式非法 ⇒ `EXPECTED_HEAD_SHA_INVALID`（绝不猜「大概一致」）；
  - `.git` HEAD/refs 不可解析 ⇒ 复用 `GIT_INFO_UNAVAILABLE`（绝不猜测版本）；
  - `PROJECT_STATE.branch` 与 observed HEAD 分支漂移 ⇒ `STATE_BRANCH_DRIFT`；
  - `state` 声称的 `current_task` / `last_completed_task` / `task_queue` 声明与 results 事实
    漂移 ⇒ 复用 §2.5 的 `PROJECT_STATE_POINTER_*` / `QUEUE_DECLARATION_MISMATCH` /
    `QUEUE_TASK_MISSING`，并叠加聚合标记 `STATE_RESULT_DRIFT`（**GOLD-041 单一事实源**：该聚合
    标记必须先在 `issues` 里作为真实 `error` issue 存在，再由 `blocking_reason_codes` 从
    `issues` **投影** ⇒ `planner_mutation.blocking_reason_codes` 与 `issues` 的 gating ERROR
    集合恒等，`drift.aggregate_code` 给出该标记；`warning` 只报告、不 gate）；
  - review backlog 事实来源不可用 ⇒ `REVIEW_BACKLOG_FACTS_UNAVAILABLE`。
- **不阻塞正常执行**：`last_reviewed_task` 指针落后属于 **review backlog** 事实（§2.11 口径），
  只报告（`drift.review_pointer_codes`），**不**作为写队列 gate；`executor_execution_blocked=false`
  与 `gates_planner_writes_only=true` —— 本工具只挡 planner-owned 写入，绝不阻塞 Executor
  执行已批准任务。
- **并发语义（fast-forward-safe）**：绝不 fetch / pull / merge / rebase / **force push**，绝不
  隐藏远端变化，绝不自动冲突覆盖；stale 的唯一处置是「重新读取最新 HEAD 并重建事实包」。
- **职责边界（不可协商）**：本工具只产事实；`authority` 段硬编码 `tool_can_mutate=false` /
  `tool_can_write_{tasks,results,project_state,review_ledger}=false` / `tool_can_sign_review=false` /
  `tool_can_advance_state=false` / `auto_merge=auto_rebase=force_push=auto_fetch_or_pull=false`
  （源码守卫测试锁定）。
- **只读保证**：外部进程调用只经由 §2.8 的只读 Git 白名单；本模块自身不启动任何外部进程，
  零网络、零数据库、零业务证据、零模型调用；唯一写操作是显式 `--output`，先拒绝
  `<root>/.ai/**`（`runtime` 除外）的**任何**目标，再复用 §2.6 守卫 ⇒ 不可能写 `.ai/tasks` /
  `.ai/results` / `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json`。
- **退出码**：`0` HEAD 一致且无事实漂移 / `2` fail-closed（stale / 漂移 / HEAD 不可解析）/
  `3` `PROJECT_STATE` 不可读 / `4` `--output` 被拒（此时绝不写文件）。
- **回归测试**：`tests/unit/test_ai_orchestrator_planner_mutation_precondition.py`（26 项：
  事实包完整性、digest 幂等与内容敏感、stale / 非法 expected / HEAD 不可解析 / 各类 state
  漂移 fail-closed、**Executor completion push 让旧 precondition 失效且重读后可恢复**、
  authority 与源码守卫、CLI fail-closed 与受控输出，以及 **GOLD-041 的 issue/blocking
  一致性三态**：真实 `STATE_RESULT_DRIFT` / 仅 review 指针滞后 / 两者并存）+
  `tests/integration/test_planner_mutation_precondition_regression.py`（5 项：真实仓库 HEAD /
  digest 由测试独立复算、漂移 gate 与独立复算一致、stale 只改变 HEAD 事实、CLI 幂等 + 前后
  零改写、Phase 3.3 blocker 与交易安全不变量不变）。
- **边界不变**：§2.12 不改变 ledger schema、滚动队列、L1~L4 档位、Phase 3.3 data blocker、
  Phase 3.4 边界与 `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。

## 2.13 Result 终态一致性门禁与历史矛盾 fail-closed 暴露（GOLD-039）

- **为什么**：§2.2 要求 Orchestrator 判定（`status` / `execution_outcome` /
  `normalized_finish_reason`）与 Cline raw `finish_reason` **分开保存**；但历史 result
  （GOLD-020 之前旧版本进程写入）仍把 raw 值写在**唯一**的 `finish_reason` 上：
  GOLD-035 就是 `status=completed` + 最终 attempt `finish_reason=aborted`。GPT 拒绝为它写
  正式 Review 台账（不猜测、不静默归一化、不回写历史），正式台账停在 GOLD-027。
  §2.13 把「一份 result 的终态事实是否自洽」变成**确定性、可复用、fail-closed** 的判定，
  并把矛盾**显式暴露**给 GPT，而不是替 GPT 判定或篡改历史。
- **唯一规则来源**：`orchestrator/result_terminal_consistency.py`（纯函数 + 纯标准库；
  零网络、零数据库、零外部进程、零 wall-clock）。`orchestrator/ai_orchestrator.py`
  （写入前门禁）与 `orchestrator/review_binding.py`（review / backlog / ledger 事实层）
  **只调用同一份规则**，不存在第二套词表 / 组合表。
- **稳定契约**：`schema=gold-ai/result-terminal-consistency/v1` + `schema_version=1`；
  报告恒定形状（`status` / `status_known` / `status_terminal` / `format` / `outcome` /
  `normalized_finish_reason` / `attempt_count` / `final_attempt` / `final_outcome` /
  `final_finish_reason` / `final_cline_finish_reason_raw` / `failure_evidence` /
  `consistent` / `reason_codes` / `details`）。
- **自洽语义（任一不满足即 fail-closed）**：
  - 顶层 `status=completed` 只能对应成功语义：最终 attempt 的 `execution_outcome` /
    `normalized_finish_reason` / `finish_reason` 必须是 `completed`，且 `cline_exit_code==0`、
    未超时、`failure_class` 为 `none`、最终 attempt **已记录**的 validation 全部通过；
  - 顶层 `status=blocked` 允许最终 attempt `blocked`（Cline 自报阻塞）、`failed`
    （retry-exhausted，必须带失败证据）、`waiting_external`（provider fatal，必须带
    non-retryable 证据）；`failed`（历史 V1 顶层状态）只允许最终 attempt `failed`；
  - 矛盾 / 未知组合一律稳定 code：`RESULT_TERMINAL_FINISH_REASON_INVALID`（raw 值落进
    归一化键）、`RESULT_TERMINAL_RAW_FINISH_REASON_MISPLACED`、
    `RESULT_TERMINAL_FINAL_ATTEMPT_CONTRADICTS_STATUS`、
    `RESULT_TERMINAL_FINAL_EXIT_CODE_CONTRADICTS_STATUS`、
    `RESULT_TERMINAL_FINAL_TIMEOUT_CONTRADICTS_STATUS`、
    `RESULT_TERMINAL_FINAL_FAILURE_CLASS_CONTRADICTS_STATUS`、
    `RESULT_TERMINAL_FINAL_VALIDATION_CONTRADICTS_STATUS`、
    `RESULT_TERMINAL_STATUS_OUTCOME_MISMATCH`、
    `RESULT_TERMINAL_OUTCOME_FINISH_REASON_MISMATCH`（`execution_outcome` 与
    `normalized_finish_reason` 不一致）、`RESULT_TERMINAL_OUTCOME_INVALID`、
    `RESULT_TERMINAL_STATUS_MISSING` / `_UNKNOWN`、`RESULT_TERMINAL_ATTEMPTS_INVALID` /
    `_ATTEMPT_INVALID`、`RESULT_TERMINAL_FAILURE_EVIDENCE_MISSING`、
    `RESULT_TERMINAL_RESULT_INVALID`；
  - **历史格式只读兼容**：顶层与最终 attempt 都没有归一化字段 ⇒ 唯一 `finish_reason`
    是 raw Cline 值，`status=completed` 而 raw 不是 `completed`（或 `blocked`/`failed`
    而 raw 恰好是 `completed`）⇒ `RESULT_TERMINAL_LEGACY_RAW_FINISH_REASON_CONTRADICTS_STATUS`，
    只报告、绝不重写历史 result；
  - **没有事实就不做组合判定**：`attempts` 缺失 / 空 / 只有迁移空壳 attempt 时只校验顶层
    字段，既不猜测成功也不凭空指控矛盾（合成 fixture 与 V1 迁移路径兼容）。
- **写入前门禁（Orchestrator）**：`write_final_result()` 在 `atomic_write_json` **之前**
  调用同一校验器；检出矛盾 / 未知组合一律 raise `ResultTerminalConsistencyError`，
  **绝不写出一份自相矛盾的终态 result**（矛盾 result 不会落盘）；校验器不可用同理
  fail-closed（无法证明自洽就不写终态事实）。失败 attempt 的 raw 值只允许留在
  `cline_finish_reason_raw`。
- **Review 层 fail-closed 暴露**：`orchestrator.review_binding` 对矛盾 result 追加稳定
  reason code ⇒ `binding.facts_complete=false`；`orchestrator.review_backlog` 逐项
  `facts_complete=false` + `review_status=invalid` + `BACKLOG_ITEM_FACTS_INCOMPLETE`；
  §2.9 台账完整性据 manifest 得到 `LEDGER_MANIFEST_FACTS_INCOMPLETE`。GPT 因此看到的是
  「事实不齐 + 稳定 code」，**绝不**据此猜 PASS、也绝不自动改写历史。
- **不破坏既有链路**：正式台账已绑定的任务（GOLD-025~027）保持 `facts_complete=true`，
  §2.9 台账完整性仍退出码 `0`；`last_reviewed_task` 指针落后仍只是**事实**（§2.12 口径，
  不 gate planner writes）；rolling queue / Git 恢复状态机 / L1~L4 档位 / Phase 3.3 data
  blocker / `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 全部不变。
- **职责边界（不可协商）**：本门禁**只判事实**——不签发 verdict、不写 ledger / state /
  task / result、不修复历史、不提升 Executor 为 Planner、不改变业务资格；`authority`
  段机器可读地声明 `tool_can_sign_review=false` / `tool_can_repair_result=false` /
  `tool_can_advance_state=false` / `writes_*=false`（源码守卫 + 契约测试锁定）。
- **回归测试**：`tests/unit/test_result_terminal_consistency.py`（28 项：正常成功 / blocked /
  provider fatal / timeout / retry-exhausted 自洽；completed+aborted、completed+failed、
  blocked+completed、未知组合 fail-closed；历史 result 只读兼容；写入前门禁拒绝落盘；
  authority 与源码守卫）+ `tests/integration/test_result_terminal_consistency_regression.py`
  （真实语料逐份与测试**独立复算**一致、GOLD-035 矛盾被暴露、正式台账已绑定任务不误伤、
  §2.9 台账完整性仍通过、CLI 前后零改写、Phase 3.3 / 交易安全不变量不变）+
  既有 `tests/integration/test_review_binding_regression.py` /
  `test_review_backlog_regression.py` 按新语义更新（历史矛盾 ⇒ fail-closed）。



## 2.14 CI 全历史契约、无 head 稳定 token、runtime 目录准备与跨平台 PID 三态（GOLD-040）

- **为什么**：GOLD-039 之后 GitHub CI（run 35869237769，Python 3.12 / 3.13）在**干净 checkout** 上
  稳定失败 17 项控制面测试，而本地全绿。根因全是**环境契约**问题：浅克隆历史不足、`.ai/runtime`
  （永不进 Git）不存在、POSIX / Windows 的 PID 存活探测语义分叉、以及一条与 §2.12 冲突的旧断言。
  §2.14 把这些契约**显式化**，让本地与 CI 对同一 Git 历史 / runtime 输出路径 / PID 探测 /
  terminal-consistency 事实得到一致结果，**且不放宽任何 fail-closed 判定**。
- **CI checkout 历史契约**：`.github/workflows/ci.yml` 的两个 job（quality 的 py3.12 / 3.13 与
  postgres）都使用 `actions/checkout@v4` + `fetch-depth: 0`。理由：§2.8 review binding、§2.9
  ledger 完整性、§2.11 backlog 需要**独立重算历史完成 commit 的绑定**（GOLD-025~027），
  浅克隆必然假失败。**严禁**通过放宽 `COMPLETION_COMMIT_NOT_FOUND` 来「修」浅克隆。
- **浅历史显式诊断（只报告，不放宽）**：`orchestrator.review_binding` 在「找不到完成 commit」且
  仓库是浅克隆时，**追加**稳定 issue `GIT_HISTORY_SHALLOW`（探测方式：只读 `<gitdir>/shallow`，
  复用 §2.5 的 gitdir 解析，零子进程 / 零网络）。此时 `commit.reason_code` 仍是
  `COMPLETION_COMMIT_NOT_FOUND`、`binding.facts_complete=false`、退出码仍为 `2` ——
  「历史被截断」与「任务从未完成」不再被混为一谈，浅历史**明确 fail-closed 而不是伪通过**。
  `review_backlog` / `review_ledger_integrity` 沿用同一事实链自动获得该诊断。
- **无 queue head 的稳定机器可读表示（唯一口径）**：`QUEUE_HEAD_ABSENT = "-"` +
  `queue_head_token()`（**规则来源**：`orchestrator.planner_refill_request`）。§2.10 的 refill 事实
  在多个入口渲染同一「无 head」事实：CLI stderr（`[refill] head=...`）、Orchestrator 提示行
  （`GPT_PLANNER_REFILL_REQUIRED ... head=...`）与提示节流签名（`|head=...`）**必须**输出同一个
  `-`；`ai_orchestrator` 侧复用该函数（只读模块不可用时退化为同一常量），**绝不**出现
  `head=None` / 空串 / `-` 的入口漂移。
- **runtime 输出目录准备（只对已批准 runtime 路径）**：干净 CI checkout 里 `.ai/runtime` 不存在
  （`.gitignore`），但默认镜像 / 输出路径（§2.6 受控 `--output`）本就落在那里。
  `orchestrator.planner_snapshot_output` 因此新增 `runtime_output_root()` /
  `prepare_output_parent()`：
  - **允许**：只为 `<root>/.ai/runtime/**` 之内的目标创建**缺失的父目录链**
    （parents-only + `exist_ok=True`，已存在目录零副作用）；
  - **禁止**：绝不扩大到系统临时目录（仍要求父目录已存在）、`.ai/tasks` / `.ai/results` /
    `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json` / `.git` / `src` / `database` /
    `config` / `data` / `logs` / `scripts` / `tests` / `docs` 等；
  - `guard_summary()` 机器可读声明 `creates_directories=true` +
    `creates_directories_scope=".ai/runtime"`；源码守卫断言全模块**只有一处** `mkdir(`。
- **跨平台 PID 存活探测（三态契约恒定）**：`running_process_image()` 按 `os.name` 分派到
  `windows_process_image()`（`tasklist`）或 `posix_process_image()`（`os.kill(pid, 0)` +
  尽力读 `/proc/<pid>/comm`），`is_pid_running()` 统一消费同一三态语义：
  - **存在** ⇒ 返回映像名（POSIX 取不到名字时返回稳定占位名 `unknown-process`，
    `PermissionError` / `EPERM` 只说明「查不到」，**绝不**等于不存在）；
  - **确实不存在** ⇒ 返回 `None`（POSIX 仅 `ProcessLookupError`）；
  - **探测失败**（`tasklist` 缺失 / 超时 / 非 0 退出；其它 `OSError`）⇒ 抛 `OSError`，
    调用方（单实例锁）必须 **fail-safe 按「存活」处理**，绝不放行清理。
  Windows 的 `tasklist` 不可用与 POSIX 环境**都不得**把锁持有进程误判为已死亡。
- **退出码 / 边界不变**：本次只改上述契约，`COMPLETION_COMMIT_NOT_FOUND`、§2.12 的
  `state/pointer/digest` gate、§2.13 的终态一致性稳定 code、ledger schema、rolling queue、
  Git 恢复状态机、L1~L4 档位、Phase 3.3 data blocker、Phase 3.4 边界与
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 全部不变。
- **回归测试**：`tests/unit/test_ai_orchestrator_review_binding.py`（浅历史诊断 /
  浅标记不臆造 code / 探测只读）、`tests/unit/test_ai_orchestrator_recovery_state_machine.py`
  （POSIX 探活三态 + 平台分派）、`tests/unit/test_ai_orchestrator_planner_refill_request.py`
  （无 head token 单一口径，含 CLI stderr）、
  `tests/unit/test_ai_orchestrator_planner_snapshot.py`（runtime 目录准备 + runtime 之外零创建）、
  `tests/integration/test_planner_refill_hint_regression.py`（真实仓库默认 runtime 镜像目标被守卫接受）、
  `tests/integration/test_planner_mutation_precondition_regression.py`（按 §2.12 语义断言
  「review 指针落后只报告、不 gate」）。



## 2.15 历史 Result 矛盾的 GPT 专属裁决契约（GOLD-042）

- **为什么**：§2.13 让历史矛盾（`status=completed` + 最终 attempt raw `finish_reason=aborted`）
  fail-closed 暴露，但没有**恢复路径**；§2.15 增加**独立、确定性、GPT-only、内容身份绑定**的
  裁决契约，只恢复「GPT 可对该历史矛盾做实质 review」的可能性，**绝不**改写 / 归一化 /
  删除历史 result，也绝不由 Executor 代签（Executor 只实现契约、validator、CLI、文档与测试）。
- **唯一规则来源**：`orchestrator/legacy_result_adjudication.py`（只读、纯函数、零外部进程、
  零网络、零数据库、零模型调用）。矛盾判定继续复用 §2.13
  `orchestrator.result_terminal_consistency`；result canonical sha256 / completion commit
  identity 继续复用 §2.8 `orchestrator.review_binding`（**不存在第二套身份算法**）。
- **契约**：`schema=gold-ai/legacy-result-adjudication/v1` + `schema_version=1`；store
  `gold-ai/legacy-result-adjudication-store/v1`，唯一 canonical 位置
  `.ai/adjudications/legacy_result_adjudications.json`（与 `.ai/results` **物理分离**，
  本工具绝不写 `.ai/results` / 绝不创建裁决 store）。
- **裁决条目**（缺任一必填字段 ⇒ fail-closed）：`adjudication_id` / `task_id` /
  `adjudication_type`（唯一合法值 `legacy_terminal_contradiction`）/ `reviewer` /
  `reviewer_role=GPT` / `reason_summary` / `adjudicated_at`（带时区 ISO）/ 可选 `expires_at` /
  `bound_result{sha256,status}` / `bound_commit{sha,branch}` / `contradiction_reason_codes`。
- **GPT-only**：只有 `reviewer` 命中 §2.5 planner 身份（`PLANNER_AGENTS=("gpt",)`）且
  `reviewer_role=GPT` 才是有效裁决；Cline / DeepSeek ⇒ `ADJUDICATION_EXECUTOR_FORBIDDEN`，
  未知身份 ⇒ `ADJUDICATION_REVIEWER_NOT_AUTHORIZED`。
- **fail-closed（稳定 reason code）**：store 缺失 / 不可读 / schema 不支持 /
  `adjudications` 非数组；条目非对象 / 缺字段；task_id 非法；同一 store 内
  `adjudication_id` 重复（`ADJUDICATION_ID_DUPLICATE`）、同一 task 多条完全相同
  （`ADJUDICATION_TASK_ID_DUPLICATE`）或内容不同（`ADJUDICATION_TASK_ID_CONFLICT`）；
  越权 / 未知身份 / reviewer_role 非法；理由摘要缺失；时间戳非法 / 无时区；`expires_at`
  非法、缺 `--as-of` 无法证明未过期（`ADJUDICATION_EXPIRY_UNVERIFIABLE`）或已过期
  （`ADJUDICATION_EXPIRED`）；绑定形状非法；contradiction code 不在官方词表；
  **身份漂移**：`ADJUDICATION_RESULT_SHA256_DRIFT` / `..._RESULT_STATUS_DRIFT` /
  `..._COMMIT_SHA_DRIFT` / `..._COMMIT_BRANCH_DRIFT` / `..._CONTRADICTION_CODES_DRIFT`；
  live 事实不完整（`ADJUDICATION_LIVE_FACTS_INCOMPLETE`）；对**自洽** result 误签裁决
  （`ADJUDICATION_RESULT_NOT_CONTRADICTORY`）。
- **原矛盾始终可见**：报告逐任务给出 `original_contradiction`（来自 §2.13 的唯一判定，
  含原始 reason code 与细节）；裁决有效 / 无效都不改变该事实，绝不删除、降级或伪装。
- **退出码**：`0` 全部请求任务均为有效裁决（或不存在需要裁决的矛盾）/ `2` fail-closed
  （缺失 / 越权 / 重复 / 冲突 / 过期 / 漂移 / 无效）。
- **职责边界（不可协商）**：`authority` / `contract` 机器可读声明
  `writes_adjudications` / `writes_results` / `writes_tasks` / `writes_project_state` /
  `writes_review_ledger` 全为 `false`，且 `tool_can_sign_adjudication=false` /
  `tool_can_sign_review=false` / `tool_can_repair_result=false` /
  `tool_can_advance_state=false` / `tool_can_lift_phase_blocker=false` /
  `executor_can_adjudicate=false` / `adjudication_implies_verdict=false` /
  `adjudication_advances_review_pointer=false` / `adjudication_lifts_phase3_3_blocker=false`；
  裁决**不**产生 PASS/FAIL、**不**写 `GPT_REVIEW_LEDGER`、**不**推进 `last_reviewed_task`、
  **不**解除 `PHASE3_3_DATA`、**不**决定 Phase（源码守卫测试锁定）。
- **已知矛盾集合**：`GOLD-028 / 031 / 035 / 038 / 039 / 040 / 041` 只作为 CLI 默认巡检与
  fixture / 回归形状参考（`KNOWN_CONTRADICTION_TASKS`）；**fixture 绝不是真实裁决** ——
  当前仓库不存在任何真实 GPT 裁决，`unadjudicated` + fail-closed（exit `2`）才是正确事实。
- **不破坏既有链路**：§2.13 写入前门禁与 Review 层 fail-closed 暴露、§2.8 / §2.9 / §2.11 /
  §2.12 事实链、rolling queue、Git 恢复状态机、L1~L4 档位、Phase 3.3 data blocker、
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 全部不变。
- **回归测试**：`tests/unit/test_legacy_result_adjudication.py`（48 项：GPT-only 身份、
  契约 / authority 只读边界、有效裁决、越权 / 未知身份、缺失字段、重复 / 冲突 / 过期、
  五类身份漂移、store 顶层契约、确定性 digest 与 ASCII 渲染、CLI 只读行为、7 项已知矛盾
  fixture 形状）+ `tests/integration/test_legacy_result_adjudication_regression.py`（11 项：
  7 个真实矛盾 result 的身份由 raw `git` + `hashlib` **独立复算**、`.ai/results` 全树 /
  `PROJECT_STATE` / `GPT_REVIEW_LEDGER` 前后字节一致、CLI 确定性 ASCII、§2.13 fail-closed
  暴露未被弱化、Executor 未创建真实裁决 store）。
## 2.16 历史矛盾裁决的确定性证据包（GOLD-043）

- **为什么**：§2.15 提供裁决**契约**后，GPT 仍要逐项手跑 `review_binding` /
  `legacy_result_adjudication` / `review_backlog` 才能看清「`last_reviewed_task` 之后的
  完整 backlog 里，每一项是可直接实质 review、还是必须先裁决、还是事实不齐」。§2.16 提供
  那份**单一、只读、确定性**的证据 manifest：逐项汇总客观事实与稳定分类，**只产事实、
  绝不签发 verdict 或裁决**。
- **唯一规则来源**：`orchestrator/review_evidence_manifest.py`（只读、纯标准库、零网络、
  零数据库、零模型调用、零 wall-clock 参与内容身份）。内容身份 / 完成 commit / validation
  复用 §2.8 `orchestrator.review_binding`（只读 Git 白名单 `log` / `ls-tree` / `cat-file` /
  `hash-object`）；终态矛盾判定复用 §2.13 `orchestrator.result_terminal_consistency`；
  裁决状态复用 §2.15 `orchestrator.legacy_result_adjudication`；backlog 边界 / ledger
  identity 复用 §2.11 `review_backlog` + §2.9 `review_ledger_integrity`（**不存在第二套
  result / commit 身份算法**）。
- **契约**：`schema=gold-ai/review-evidence-manifest/v1` + `schema_version=1`；CLI
  `python -m orchestrator.review_evidence_manifest`。默认 stdout 只读；显式 `--output`
  **复用** §2.6 的 fail-closed 路径守卫（只允许 `<root>/.ai/runtime/**` 或系统临时目录），
  因此结构上不可能写 `.ai/tasks` / `.ai/results` / `.ai/PROJECT_STATE.json` /
  `.ai/GPT_REVIEW_LEDGER.json` / `.ai/adjudications/**`。
- **覆盖范围**：`PROJECT_STATE.last_reviewed_task` **之后**的全部 `completed` result
  （与 §2.11 backlog 同一口径），并在 `coverage` 里显式给出已知矛盾集合
  `KNOWN_CONTRADICTION_TASKS` 是否落在 backlog 内；若已知矛盾任务已完成却不在 backlog
  （指针越过未裁决矛盾）⇒ `KNOWN_CONTRADICTION_NOT_IN_BACKLOG` + fail-closed。
- **逐项事实**：`task` / `result` 的 canonical sha256（§2.8 口径）、completion
  `commit.{sha,branch,subject,committed_at}` + `changed_paths`（只读
  `git log -1 --name-only`）、`validation.status/commands/return_codes`（记录值，
  **绝不重新执行**）、`terminal_consistency.{contradiction,format,reason_codes,details}`
  （§2.13 唯一判定）、`ledger.{entry_present,entry_count,entry_valid,identity_consistent,status}`、
  `adjudication.{state,valid,adjudication,reason_codes}`（§2.15）与稳定 `reason_codes`。
- **四种客观分类（`items[].classification`，绝不是 verdict 词表）**：
  - `facts-ready`：事实齐全且无未解阻塞（无 legacy 矛盾 / 矛盾已有有效 GPT 裁决 /
    已被 ledger 合法绑定）；
  - `needs-gpt-adjudication`：事实齐全但存在**未裁决的 legacy** 终态矛盾，必须先由 GPT 裁决；
  - `pending-substantive-review`：事实齐全、无矛盾、尚未绑定，等待 GPT 实质 review；
  - `invalid`：缺 completion commit / hash 漂移 / 事实不齐 / ledger 非法 / **裁决冲突** /
    已解析 commit 的 changed paths 不可读 ⇒ fail-closed。
  归一化格式（非 legacy）的矛盾**没有**恢复路径，一律 `invalid`，绝不当作待裁决。
- **测试通过 ≠ GPT PASS**：`validation` 的 `returncode=0`、CI 通过或 `exit_code=0` 都只是
  「客观事实可复算、无 fail-closed 项」，**绝不等于** review 已完成；manifest 顶层不存在
  `verdict` / `review_verdict` / `acceptance_summary` / `reviewed_at` / `reviewer` /
  `reviewer_role`（`FORBIDDEN_MANIFEST_KEYS`，裁决 reviewer 身份作为**事实**只嵌套在
  `items[].adjudication` 内）。
- **确定性**：`facts_digest = sha256(canonical json: sort_keys + compact separators)`，
  排除 `generated_at` / `facts_digest` / `determinism`；相同仓库事实 ⇒ 相同 digest；
  不联网、不调用模型、不推进 review pointer、不写 state / ledger / adjudication。
- **退出码**：`0` 客观事实可复算且无 fail-closed 项（**绝不是** GPT PASS）/ `2` fail-closed
  （缺 commit / hash 漂移 / 事实不齐 / 裁决冲突，或存在待裁决矛盾）/ `3` `PROJECT_STATE` 或
  ledger 不可用 / `4` `--output` 目标被拒绝（绝不写文件）。
- **职责边界（不可协商）**：`authority` 段机器可读声明 `review_authority=gpt_only` /
  `tool_can_sign_review=false` / `tool_can_sign_adjudication=false` /
  `tool_can_write_review_ledger=false` / `tool_can_write_adjudication=false` /
  `tool_can_advance_review_pointer=false` / `tool_can_advance_state=false` /
  `writes_*` 全为 `false` / `network_access=false` / `model_calls=false`；工具**不能**
  自签 review 或裁决，也不改变 `PHASE3_3_DATA` blocker、Phase 3.4 边界、L1~L4、
  rolling queue、`LIVE_TRADING=false` 或 `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。
- **与 §2.14 的一致性**：真实 Git 历史不完整（浅克隆）时，逐项 facts 仍按 §2.8 口径
  fail-closed（`GIT_HISTORY_SHALLOW` + `COMPLETION_COMMIT_NOT_FOUND` ⇒ `invalid`），
  **绝不**把浅历史当「任务未完成」或伪通过。
- **回归测试**：`tests/unit/test_review_evidence_manifest.py`（25 项：authority / contract /
  determinism 只读边界、源码守卫、四态分类与 fail-closed、受控 `--output`、CLI ASCII 与
  确定性）+ `tests/integration/test_review_evidence_manifest_regression.py`（19 项：真实
  backlog 与 §2.11 逐项一致、7 个已知矛盾为 `needs-gpt-adjudication`、逐项身份由 raw
  `git` blob + `diff-tree` **独立复算**、`facts_digest` 与 wall-clock 无关、`.ai/results`
  全树 / `PROJECT_STATE` / `GPT_REVIEW_LEDGER` / 裁决 store 前后字节一致、§2.8/§2.13
  fail-closed 暴露未弱化、Phase 3.3 与交易安全不变量不变）。

## 2.17 有效 GPT 裁决接入 Review Binding 与连续性门禁（GOLD-044）

- **为什么**：§2.15 给出历史矛盾的**裁决契约**，§2.16 给出**证据包**，但 review 层
  （§2.8 binding / §2.9 ledger integrity / §2.11 backlog / §2.12 / refill 诊断）此前把
  历史矛盾一律当作 `facts_complete=false` ⇒ 即使 GPT 已裁决，也无法把该项标为
  「可实质 review」。§2.17 把**有效**裁决接入这条事实链：默认行为不变，只有合法裁决才恢复
  事实可绑定性；任何越权 / 漂移 / 重复 / 冲突矛盾继续 fail-closed。
- **唯一事实来源**：§2.15 `orchestrator.legacy_result_adjudication`
  （`collect_store_index` / `evaluate_task`，只读、纯函数）+ §2.13
  `orchestrator.result_terminal_consistency`（矛盾判定）。review 层**不另造**第二套裁决
  身份 / 漂移 / 越权判定；`collect_store_index` 是 review 层共用的**唯一** store 读取入口。
- **§2.8 `review_binding` 契约扩展**：manifest 新增 `adjudication` 段
  （`store_present` / `state` / `valid` / `adjudicated` / `adjudication` / `reason_codes` /
  `contract_source`）与 `binding.{facts_ready,adjudicated,facts_ready_source}`：
  - **默认行为不变**：没有裁决 / 无该项目裁决 ⇒ `facts_complete=false` /
    `facts_ready=false` / `facts_ready_source=blocked` / 退出码 `2`；
  - **只有有效裁决才恢复**：`state=adjudicated`（schema 合法、`reviewer_role=GPT` 且 reviewer
    命中 §2.5 planner 身份、result sha256/status + commit sha/branch + 原始 contradiction
    codes 完全匹配、非重复 / 冲突 / 过期 / 漂移）**且** `binding` 不存在任何**非 legacy** 的
    阻塞 reason code ⇒ `adjudicated=true` / `facts_ready=true` /
    `facts_ready_source=adjudicated` / 退出码 `0`；
  - **历史事实绝不隐藏**：原始 contradiction finding（§2.13 稳定 code）、原 result
    `sha256` / `status`、completion commit 身份与裁决 identity
    （`adjudication.adjudication`，含 reviewer / reviewer_role / bound_* / codes）全部原样保留；
  - **fail-closed**：越权（`ADJUDICATION_EXECUTOR_FORBIDDEN`）/ 未知身份
    （`ADJUDICATION_REVIEWER_NOT_AUTHORIZED`）/ 重复 / 冲突 / 过期 / 五种身份漂移 /
    store 不可读（`ADJUDICATION_STORE_UNREADABLE`）/ schema 不支持
    （`ADJUDICATION_STORE_SCHEMA_UNSUPPORTED`）等 §2.15 稳定 code 原样并入
    `binding.reason_codes`；store 缺失是**中性事实**（尚无裁决），不产生额外 code；
  - `reviewer` / `reviewer_role` 只作为**事实**嵌套在 `adjudication.adjudication` 内
    （§2.16 同口径），顶层仍禁止任何 Review 结论字段。
- **§2.9 `review_ledger_integrity`**：台账条目按 §2.8 `facts_ready` 核对客观身份；
  `bindings[].{manifest_facts_ready,manifest_adjudicated,manifest_adjudication_state}` 与
  `ledger.adjudicated_entry_count` 只报告事实。身份漂移检测（result hash / status /
  finished_at / commit sha / branch 五类 mismatch）**一字未放宽**。
- **§2.11 `review_backlog`**：逐项新增 `facts_ready` / `adjudicated` /
  `facts_ready_source` / `adjudication`；`review_status` 词表不变
  （`pending` / `bound` / `invalid`）。四态稳定区分：
  - **未裁决矛盾** ⇒ `facts_ready=false` ⇒ `invalid` + `BACKLOG_ITEM_FACTS_INCOMPLETE`；
  - **已裁决待实质 review** ⇒ `review_status=pending` + `adjudicated=true`；
  - **已绑定** ⇒ `review_status=bound`（identity 逐项一致）；
  - **身份漂移** ⇒ `invalid` + `BACKLOG_ITEM_*_DRIFT`。
  新增 `BACKLOG_ITEM_ADJUDICATION_INVALID` 与 summary
  `facts_ready_count` / `adjudicated_count` / `adjudicated_pending_count` /
  `unresolved_contradiction_count`。
- **§2.12 planner mutation precondition**：`review_backlog.pointer` 与 `summary` 透传同一
  裁决计数（`adjudicated_count` / `adjudicated_pending_count` / `facts_ready_count` /
  `unresolved_contradiction_count` / `review_adjudicated_count` 等）；**不**因此改变
  `planner_mutation.allowed` 与 blocking 语义。
- **refill 诊断（§2.10）**：`completed_but_unreviewed.adjudication` 只复用 §2.15 store 读取
  入口报告 **presence** 事实（`store_present` / `declared_task_ids` / `declared_count` /
  `undeclared_*` / `declared_implies_valid=false` / `validation_source`），
  `summary.adjudication_declared_count` 同源；**presence ≠ 有效**，有效性 / 身份匹配只由
  §2.8 / §2.15 判定，本层绝不重算、绝不改任何计数或门禁。
- **职责边界（不可协商）**：以上各层仍 `read_only=true`，`writes_*` 全为 `false`，
  `tool_can_sign_review=false` / `tool_can_sign_adjudication=false` /
  `tool_can_write_adjudication=false` / `adjudication_implies_verdict=false` /
  `adjudication_advances_review_pointer=false` / `adjudication_lifts_phase3_3_blocker=false`；
  有效裁决**不**产生 PASS/FAIL、**不**写 `GPT_REVIEW_LEDGER`、**不**推进
  `last_reviewed_task`、**不**解除 `PHASE3_3_DATA`、**不**决定 Phase。
- **不破坏既有门禁**：§2.13 写入前门禁、§2.8~§2.12 事实链、ledger 连续性
  （`LEDGER_CHAIN_GAP` / `LEDGER_ORDER_REGRESSION` / `LEDGER_REVIEW_TIME_REGRESSION`）、
  `GIT_HISTORY_SHALLOW` / `COMPLETION_COMMIT_NOT_FOUND` fail-closed、rolling queue、
  L1~L4 档位、Phase 3.3 data blocker、`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 全部不变。
- **回归测试**：`tests/unit/test_review_closure_adjudication.py`（32 项：默认 fail-closed、
  有效裁决 facts-ready、越权 / 未知身份 / 五类漂移 / 重复 / 冲突 / store 损坏 fail-closed、
  backlog 三态 + 漂移、integrity 绑定与漂移、precondition / refill 计数同源、只读 API 守卫）
  + `tests/integration/test_review_closure_adjudication_regression.py`（10 项：真实仓库
  `GOLD-035` 身份由 raw `git` blob + `hashlib` 独立复算、有效裁决恢复 bindability、
  越权 / 漂移 / 重复 fail-closed、`.ai/results` / tasks / `PROJECT_STATE` / `GPT_REVIEW_LEDGER`
  前后字节一致、仓库不创建裁决 store、裁决不推进 `last_reviewed_task`、ledger 连续性仍以
  `LEDGER_CHAIN_GAP` fail-closed、backlog 端到端分类）。


## 2.18 GPT Review 收口写入前的原子一致性预检（GOLD-045）

- **为什么**：§2.15 给出裁决契约，§2.16 给出证据包，§2.17 把有效裁决接入 review 事实链，
  但 GPT 真正**写**三份状态（`.ai/adjudications/legacy_result_adjudications.json`、
  `.ai/GPT_REVIEW_LEDGER.json`、`.ai/PROJECT_STATE.json`）时是**分次写入**的：写到一半
  （裁决已落盘但 ledger / `last_reviewed_task` 未跟上，或 ledger 越过仍未裁决的矛盾）就会
  制造**新的** ledger / state drift。§2.18 提供**单一只读预检**：GPT 在写入前把候选写集与
  当前已提交事实一次性比对，任何 stale / 不连续 / 越权 / 安全不变量变化一律 fail-closed。
- **唯一规则来源**：`orchestrator/review_closure_precondition.py`（只读、纯标准库、零网络、
  零数据库、零模型调用、零 wall-clock 参与内容身份）。HEAD / 指针复用 §2.5 planner snapshot；
  result / commit 身份复用 §2.8 `review_binding`（只读 Git 白名单）；ledger 校验与连续性复用
  §2.9 `review_ledger_integrity` + §2.8 `review_ledger`；裁决校验复用 §2.15
  `legacy_result_adjudication`（`store_entries` / `store_consistency_issues` /
  `evaluate_task`）；backlog 复用 §2.11 `review_backlog`；摘要算法复用 §2.12
  `planner_mutation_precondition`（`canonical_digest` / `file_facts` / `directory_facts`）
  与 §2.6 受控输出守卫（**不存在第二套身份 / 连续性算法**）。
- **候选契约**：`schema=gold-ai/review-closure-candidate/v1` + `schema_version=1`，三段可选
  （`adjudication_store` / `review_ledger` / `project_state`）加**必需的** `base`
  （`head_sha` / `results_digest` / `tasks_digest` / `project_state_sha256` /
  `review_ledger_sha256` / `adjudication_store_sha256`）。**候选只允许来自显式临时文件或
  stdin**（`--candidate <系统临时目录|.ai/runtime/** 文件>` 或 `--candidate -`）；把
  `.ai/PROJECT_STATE.json` / `.ai/GPT_REVIEW_LEDGER.json` / `.ai/tasks` / `.ai/results` /
  `.ai/adjudications/**` 当候选来源一律拒绝。
- **一次性比对的事实**：`head`（observed HEAD + `base.head_sha` 比对）、`committed_current`
  的 `tasks_digest` / `results_digest` / `project_state.blob_sha256` /
  `review_ledger.blob_sha256` / `adjudication_store.blob_sha256`、§2.11 backlog 事实、
  §2.9 integrity 事实、按候选 task 汇总的 §2.8 binding 事实子集，以及候选裁决 / ledger /
  state 三段逐项校验。
- **fail-closed 清单（稳定 reason code）**：
  - 候选层：`CANDIDATE_MISSING` / `CANDIDATE_UNREADABLE` / `CANDIDATE_JSON_INVALID` /
    `CANDIDATE_NOT_OBJECT` / `CANDIDATE_SCHEMA_UNSUPPORTED` / `CANDIDATE_SECTION_INVALID` /
    `CANDIDATE_BASE_MISSING` / `CANDIDATE_BASE_INVALID`；
  - 并发层：`STALE_REMOTE_HEAD`（`base.head_sha` 与 observed HEAD 不一致）、
    `CANDIDATE_BASE_RESULTS_DRIFT` / `..._TASKS_DRIFT` / `..._STATE_DRIFT` /
    `..._LEDGER_DRIFT` / `..._ADJUDICATION_DRIFT`、`GIT_INFO_UNAVAILABLE`、
    `COMMITTED_FACTS_UNAVAILABLE`；
  - 连续性层：`LEDGER_CHAIN_GAP` / `LEDGER_ORDER_REGRESSION` /
    `LEDGER_REVIEW_TIME_REGRESSION` / `LEDGER_RESULT_*_MISMATCH` /
    `LEDGER_COMMIT_*_MISMATCH` / `LEDGER_MANIFEST_FACTS_INCOMPLETE`（§2.9 原样复用）、
    `CANDIDATE_LEDGER_ENTRY_REMOVED`、`CANDIDATE_PASS_PAST_UNRESOLVED_CONTRADICTION`
    （PASS 越过**未裁决**的历史矛盾）；
  - 裁决层：`CANDIDATE_ADJUDICATION_NOT_GPT` / `CANDIDATE_ADJUDICATION_INVALID` 与 §2.15
    原样透传的稳定 code（越权 / 未知身份 / 重复 / 冲突 / 过期 / 五种身份漂移）；
  - 状态层：`CANDIDATE_LAST_REVIEWED_REGRESSION` / `CANDIDATE_LAST_REVIEWED_AHEAD` /
    `CANDIDATE_STATE_POINTER_DRIFT` / `CANDIDATE_PHASE3_3_BLOCKER_REMOVED` /
    `CANDIDATE_BLOCKER_REMOVED` / `CANDIDATE_PHASE3_4_ENTRY` /
    `CANDIDATE_TRADING_SAFETY_CHANGED`。
- **candidate-ready ≠ committed-current ≠ review 完成**：`closure` 段机器可读地区分
  `candidate_ready`（候选写集与已提交事实原子一致）、`committed_current`
  （head / state / backlog / integrity 是否可读），并硬编码 `review_completed=false` /
  `review_verdict_issued=false` / `phase_gate_lifted=false` /
  `executor_write_triggered=false` / `state_advanced=false` / `auto_push=false`；
  候选通过**不**触发 Executor 写入、**不**自动 push、**不**解除 Phase gate。
- **零写入与 CLI**：`python -m orchestrator.review_closure_precondition --candidate <临时文件|->`
  （另有只读 `--root` / `--state` / `--tasks-dir` / `--results-dir` / `--ledger` /
  `--adjudication-store` / `--as-of` / `--generated-at`）。**没有** `--apply` / `--fix` /
  `--advance` / `--sign` 等变更开关；默认 stdout 纯 ASCII JSON（`stderr` 只放人类摘要），
  显式 `--output` **复用** §2.6 守卫（只允许 `<root>/.ai/runtime/**` 或系统临时目录）。
- **确定性**：`precondition_digest = sha256(canonical json: sort_keys + compact separators)`，
  排除 `generated_at` / `as_of` / `precondition_digest` / `determinism`；相同候选 + 相同已提交
  事实 ⇒ 相同 digest（幂等），并区分 Windows / Linux 路径（`Path.resolve()` + 允许根前缀判定）。
- **退出码**：`0` 候选与已提交事实原子一致（candidate-ready，**绝不是** review PASS）/
  `2` fail-closed（stale HEAD / 漂移 / 链不连续 / 越权 / 安全不变量变化）/
  `3` 候选不可用或 `PROJECT_STATE` 不可读 / `4` `--output` 被拒（绝不写任何文件）。
- **职责边界（不可协商）**：`authority` 段机器可读声明 `review_authority=gpt_only` /
  `tool_can_apply_candidate=false` / `tool_can_sign_review=false` /
  `tool_can_sign_adjudication=false` / `tool_can_write_adjudication=false` /
  `tool_can_write_review_ledger=false` / `tool_can_update_project_state=false` /
  `tool_can_advance_pointer=false` / `tool_can_advance_state=false` /
  `tool_can_lift_blocker=false` / `mutation_switches_exposed=[]` / `network_access=false` /
  `model_calls=false`；候选通过**不**写裁决 / ledger / state / tasks / results，**不**推进
  `last_reviewed_task`、**不**解除 `PHASE3_3_DATA`、**不**决定 Phase。
- **不破坏既有门禁**：§2.13 写入前门禁、§2.8~§2.12 事实链、ledger 连续性、浅历史
  `GIT_HISTORY_SHALLOW` / `COMPLETION_COMMIT_NOT_FOUND` fail-closed、rolling queue、L1~L4
  档位、Phase 3.3 data blocker、`LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false` 全部不变。
- **回归测试**：`tests/unit/test_review_closure_precondition.py`（43 项：契约 / authority /
  源码只读守卫 / 无变更开关、candidate-ready 与 closure 语义、组合事实、零写入、确定性
  digest 与幂等、候选缺失 / 非法 / schema / base 缺失 / stale HEAD / 摘要漂移、链断裂 /
  PASS 越过未裁决矛盾 / 有效 GPT 裁决恢复 candidate-ready / 非 GPT / 身份漂移 / 条目删除、
  `last_reviewed` 倒退与超前 / blocker 删除 / Phase 3.4 / 交易安全不变量、临时文件与 stdin
  来源守卫与跨平台路径、CLI 零写入与受控 `--output`）
  + `tests/integration/test_review_closure_precondition_regression.py`（8 项：真实仓库候选
  与已提交事实一致 ⇒ candidate-ready、§2.8 身份绑定、`precondition_digest` 幂等、stale HEAD /
  指针漂移 / 删除 `PHASE3_3_DATA` / ledger 链断裂 fail-closed、CLI 显式临时文件端到端零写入，
  且 `.ai/results` / `.ai/tasks` / `PROJECT_STATE` / `GPT_REVIEW_LEDGER` /
  `.ai/adjudications` 前后字节一致、worktree 状态不变）。


## 2.19 权威 raw 终态写入路径与终态矛盾 fail-closed（GOLD-046）

- **为什么**：GOLD-039 的写入前门禁只校验「归一化字段内部自洽」，把成功判定继续建立在
  `cline_exit_code == 0` + validation 通过上；GOLD-044 因此再次写出 `status=completed` +
  最终 attempt `cline_finish_reason_raw=aborted` 的矛盾 result，证明未来写入门禁没有覆盖
  **真实 completion 路径**。§2.19 把「唯一权威终态来源」显式定为 Cline 自报的 raw 终态，
  并在**状态推进 / completion commit 之前**就 fail-closed。
- **唯一权威终态来源**：`orchestrator/ai_orchestrator.py` 的 `parse_cline_json_output` 取
  CLI 事件流中**最后一个携带 reason 的终态事件**（`run_result.finishReason` /
  `agent_event.done.reason`，后覆盖先；不带 reason 的 done 不擦掉已捕获的 reason）作为权威
  raw 终态，并**原样**保留在 `attempt.cline_finish_reason_raw`。绝不丢弃 `aborted`、绝不
  改名、绝不做空值替换、绝不只凭 `cline_exit_code=0` / validation 通过推断 `completed`；
  缺失 raw 终态 = 没有权威成功事实 ⇒ fail-closed。
- **成功判定收紧**：`attempt_outcome(cline_result, validations)` 现在要求
  `cline_exit_code == 0` **且**全部 validation `returncode == 0` **且**权威 raw 终态为
  `completed`（复用 §2.13 唯一词表 `result_terminal_consistency.raw_terminal_is_success`；
  模块不可用 ⇒ fail-closed）。`aborted` / 其它非成功 raw 值 / 缺失 ⇒ `failed`。
- **失败语义不被折叠**：exit code 0 但权威 raw 非成功 ⇒ 稳定
  `failure_class=terminal_not_completed` / `failure_code=CLINE_TERMINAL_NOT_COMPLETED`
  （**可重试**，受 `max_attempts` 约束，绝不无限重试；retry 耗尽后顶层 `status=blocked`）。
  timeout 仍是 `retryable_external` / `CLINE_TIMEOUT`；provider fatal 仍是
  `non_retryable_external` → `waiting_external`；显式 blocked / 人工中止语义不变。
- **同一 terminal-consistency 门禁**：新增
  `ensure_completion_terminal_consistency(task, status, attempts, ...)`，在
  `write_task_state(..., "completed")` 与 `commit_task_result(..., "completed")` **之前**
  用 §2.13 唯一判定校验将写入的终态 result；`write_final_result` 在 `atomic_write_json`
  之前再校验一次。任何一处检出矛盾 ⇒ raise `ResultTerminalConsistencyError`，绝不落盘、
  绝不完成推进。`build_final_result` / `write_final_result` 共用同一载荷形状，门禁与持久化
  看到的是**同一份事实**。
- **新稳定 reason code**（`orchestrator/result_terminal_consistency.py`）：
  `RESULT_TERMINAL_RAW_TERMINAL_CONTRADICTS_COMPLETED`——`status=completed` 但权威
  `cline_finish_reason_raw` 不是 `completed`（含缺失）。§2.8 review binding / §2.11 backlog /
  §2.16 evidence manifest 复用同一规则，因此 GOLD-044 这类矛盾在 review 层显式
  `facts_complete=false` / `invalid`，**绝不**被猜成 PASS。
- **只读 / 职责边界（不可协商）**：历史 `.ai/results`、`GPT_REVIEW_LEDGER`、
  `PROJECT_STATE`、`.ai/adjudications` 全部只读；本项不创建真实 GPT 裁决、不签发 review
  verdict、不推进 `last_reviewed_task`、不解除 `PHASE3_3_DATA`、不改变 Phase / L1~L4 /
  `LIVE_TRADING=false` / `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。
- **回归测试**：`tests/unit/test_result_terminal_consistency.py`（`completed` + raw
  `aborted` / 缺失 fail-closed、`blocked` + raw `aborted` 自洽、`raw_terminal_is_success`
  严格性）、`tests/unit/test_ai_orchestrator_queue.py`（真实写入路径：exit code 0 +
  validations PASS 但 raw `aborted` ⇒ `blocked` result 且**无** completion commit；正常 raw
  `completed` ⇒ completed result + completion commit；多终态事件选择；timeout/retry
  exhausted 不产生完成推进）、`tests/unit/test_ai_orchestrator_external_failures.py`
  （`terminal_not_completed` 证据分类）+ 真实语料集成回归
  （`test_result_terminal_consistency_regression.py` / `test_review_backlog_regression.py`
  独立复算 GOLD-044 归一化矛盾；`test_review_evidence_manifest_regression.py`
  `invalid_count` 计入 GOLD-044）。

## 2.20 结果终态控制面的新进程端到端 canary（GOLD-047）

- **为什么**：§2.13 / §2.19 的修复都只在**测试进程内**被验证。同进程 monkeypatch、旧模块
  缓存、宽松 mock 与 fixture 终态改写都可能让「纯函数测试通过」掩盖**真实收尾路径并未
  生效**——GOLD-044 的 `status=completed` + `cline_finish_reason_raw=aborted` 就是这种
  回归。§2.20 把它变成**独立子进程**的机器可读证据。
- **唯一入口**：`python -m orchestrator.result_terminal_canary [workdir]`；退出码 `0` PASS /
  `1` FAIL（fail-closed），报告是 **ASCII** JSON，schema
  `gold-ai/result-terminal-canary/v1`。
- **必须经过的真实入口链**：在**新进程加载的真模块**上调用
  `orchestrator.ai_orchestrator.process_task`，覆盖终态解析（`parse_cline_json_output` /
  `authoritative_cline_terminal`）、归一化（`attempt_outcome` / `normalize_finish_reason` /
  `build_attempt_record`）、terminal-consistency（`ensure_result_terminal_consistency` /
  `ensure_completion_terminal_consistency`）、result 持久化决策（`write_final_result`）与
  completion commit 决策（`commit_task_result` + 假 git 记录）。**严禁**只调用 validator
  纯函数，也**严禁**替换终态链上的任何入口（只允许替换进程外的 `run_cline` /
  `run_validations` / `get_changed_files` / `get_diff_stat` / `git` 与只读 refill 提示）。
- **两个自洽场景**（同一降级前置：exit code 0 + validation 全通过 + 工作树有变更）：
  - 权威 raw `completed` ⇒ `completed` result + completion commit（正常路径必须保持可用）；
  - 权威 raw `aborted` ⇒ 只允许 `failed` attempt（`terminal_not_completed` /
    `CLINE_TERMINAL_NOT_COMPLETED`）+ `blocked` 顶层；**不得**出现 completed result、
    completion commit 或完成推进，且用同一份事实强制走 completion 门禁必须被拒。
- **新稳定 reason code**（`orchestrator/result_terminal_canary.py`，共 16 个
  `RESULT_TERMINAL_CANARY_*`）：`INTERNAL_ERROR` / `RESULT_MISSING` / `RESULT_UNREADABLE` /
  `ITERATION_NOT_TERMINAL` / `UNEXPECTED_GIT_COMMAND` / `FRESH_IMPORT_BOUNDARY_BROKEN` /
  `RAW_ABORTED_RAW_REWRITTEN` / `RAW_ABORTED_FIXTURE_DEGRADED` /
  `RAW_ABORTED_ATTEMPT_NOT_FAILED` / `RAW_ABORTED_PRODUCED_COMPLETED` /
  `RAW_ABORTED_PRODUCED_COMPLETION_COMMIT` / `RAW_COMPLETED_NOT_COMPLETED` /
  `RAW_COMPLETED_MISSING_RAW_EVIDENCE` / `RAW_COMPLETED_MISSING_COMPLETION_COMMIT` /
  `AUTHORITY_MODULE_NOT_LOADED` / `AUTHORITY_RULE_INCONSISTENT`。
- **新进程边界 fail-closed**：`FRESH_IMPORT_BOUNDARY_BROKEN` 在「本进程已 import pytest」
  或「边界模块（`ai_orchestrator` / `result_terminal_consistency`）此前已被加载」时触发，
  报告 `fresh_process=false` / `pytest_imported=true`；canary 绝不复用测试进程内的旧模块
  状态、宽松 mock 或历史 fixture。判定规则的唯一来源仍是
  `result_terminal_consistency.raw_terminal_is_success`（canary 只校验它仍然严格）。
- **隔离与安全（不可协商）**：只写自己的**临时 workdir**（`TASK_DIR` / `RESULT_DIR` /
  `TASK_STATE_DIR` / `RECOVERY_DIR` / `LOG_DIR` 全部重定向到该 workdir）；假 `git` 只允许
  白名单命令，白名单外一律 raise `CanaryGitError` ⇒ `UNEXPECTED_GIT_COMMAND`，**绝不**
  spawn 真实 Cline / 真实 git / 网络；真实 `.ai/tasks` / `.ai/results` / `PROJECT_STATE` /
  `GPT_REVIEW_LEDGER` / adjudication store 与 worktree 在命令前后逐字节不变。
- **不可变语料计数不再写死（同一批回归）**：
  `tests/integration/test_review_evidence_manifest_regression.py` /
  `test_review_closure_adjudication_regression.py` 里针对 immutable 语料的
  `invalid_count` / `unresolved_contradiction_count` 改为**与逐项分类 / 逐项事实同源复算**。
  理由：历史 result 永久只读、语料只增不减，写死数字在下一份任务 result 落库后必然失真
  （GOLD-046 自身 result 让这两个计数各 +1）；复算后仍**显式**要求「每个 invalid 项都以
  客观终态矛盾为理由」「`GOLD-044` / `GOLD-046` 必须出现在归一化矛盾集合里」
  「除已裁决项外每条已知历史矛盾都必须仍在未裁决集合里」，因此是**收紧**而不是放宽，
  且对后续新落库的不可变 result 不再回退（CI 不会因新增 result 变红）。
- **职责边界**：canary PASS 只是**回归事实**，不签发 verdict、不写 ledger / state、不推进
  `last_reviewed_task`、不解除 `PHASE3_3_DATA`、不改变 Phase / L1~L4 / `LIVE_TRADING=false` /
  `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`；GOLD-047 自身 result 的终态自洽仍由 GPT 核对。
- **回归测试**：`tests/unit/test_result_terminal_canary.py`（18 项：schema / 退出码 /
  reason code 词表与同名常量、假 git 白名单与 commit 证据、源码守卫「绝不 spawn 子进程」
  「绝不 patch 终态链」、平台无关（无 `os.name` / `sys.platform` 分支）、事实投影、正确
  事实零 code、以及缺失 / 不可读 / 非终态 / 退化 fixture / raw 被改写 / GOLD-044 式回归
  全部 fail-closed）+
  `tests/integration/test_result_terminal_canary_regression.py`（6 项：新进程 PASS 且真实
  仓库零改写、两场景语义独立、两次运行稳定、父进程 monkeypatch 免疫、新进程边界被污染
  必 fail-closed、Phase 3.3 / 交易安全不变量不变）。

## 2.21 Supervisor 通信边界与事件驱动 GPT 闭环

- **唯一决策者**：GPT 是唯一 Planner / Reviewer / Architect。只有 GPT 可以判定任务
  `PASS` / `FAIL`、签发 review ledger、创建或排序 `GOLD-*` 任务、修改项目 Phase、解除
  blocker。测试通过、CI 通过、Cline 退出码、Supervisor 路由判断都只是证据，不是 verdict。
- **Supervisor 仅是通信层**：AI_SUPERVISOR 只允许执行 Git fetch/隔离审查、上下文采集、
  调用 GPT、结构/路径校验、原子 control commit/push、状态同步、进程锁、退避与重试。
  Supervisor 不得自行把结果改成 PASS/FAIL，不得自行编写 GOLD 任务，不得自行推进 Phase。
- **Executor 边界**：Cline 负责领取 GPT 已批准的任务并调用 DeepSeek 完成开发/整改，随后
  运行任务声明的验证、写不可变 result 并 commit/push。Cline/DeepSeek 不得签 review、补队列
  或把失败自行降级成通过。
- **Git 是唯一中转载体**：task、immutable result、PROJECT_STATE、GPT_REVIEW_LEDGER、
  adjudication/brain request 与代码提交共同组成 GPT 和本地执行器之间的可审计状态；运行时
  日志或 `.ai/runtime/**` 只能作为诊断副本，不得成为唯一任务/整改来源。
- **事件触发，不等小时轮询**：Supervisor 发现远端/本地新 commit、
  `completed-but-unreviewed` 或队列为空时必须立即唤醒 GPT review；周期审查只作兜底。事件
  只触发 GPT，不得由 Supervisor 直接补任务或改变 verdict。
- **PASS 闭环**：GPT 验收 PASS 后，如已批准队列低于 `planner_lookahead_size`，由 GPT 在同一
  control plan 中写完整 dependency-safe task 文件及兼容的 PROJECT_STATE/队列更新；
  Supervisor 只校验和发布该 GPT 输出。
- **FAIL 闭环**：GPT 验收 FAIL 时必须在 `required_fixes` 中给出明确失败证据和整改要求，
  并写入 runner 可读的整改 task、`.ai/brain/requests/**` 或 PROJECT_STATE。Supervisor 对
  “只有日志通知、没有 Git 整改载体”的 FAIL 必须 fail-closed 拒绝发布；Cline 随后按该
  GPT 整改任务再次调用 DeepSeek。
- **BLOCKED 语义**：仅当缺少外部证据、需要人工门禁或无法安全形成 Executor 整改任务时
  使用 `BLOCKED`。这时允许队列为空，但 Supervisor 仍按受控退避重试 GPT review；不得伪造
  修复任务来清空 blocker。
- **并发与历史安全**：GPT 思考期间远端 HEAD 改变则整份计划作废并重审；禁止 force push，
  禁止改写历史 `.ai/results/**`，禁止静默删除 blocker，禁止越过 L3/L4、
  `LIVE_TRADING=false` 与 `ALLOW_EXTERNAL_ORDER_SUBMISSION=false`。

## 2.22 Fresh-process 门禁证据状态文件（GOLD-051）

`CONTROL_PLANE_FRESH_PROCESS_RESTART` 的证据由自由文本改为**机读、可校验、内容寻址**的
状态文件 `.ai/ci_evidence/fresh_process_restart_evidence.json`（版本化 schema
`fresh-process-restart-evidence-v1`）。

- **schema 必需字段**：`operator_identity`、`restart_commit_sha`、`old_pid_terminated`
  （列表，每项 `pid` / `method` 终止方式 / `terminated_at` 终止时间）、`new_process_pid`、
  `new_process_started_at`、`head_check_cmd_and_output`、`git_fetch_or_pull_cmd_and_output`、
  `ci_run_id`、`ci_run_conclusion_on_restart_commit`、`human_attestation`
  （自由文本 `statement` + `attested_at` + `operator_identity`）。
- **只读校验器** `orchestrator/fresh_process_evidence.py::verify_evidence(candidate, repo)`
  稳定判定三态：
  - `cleared`：最小事实集合齐全且全部 gate 条件满足；
  - `not_cleared`：证据合法（指向正确 restart commit），但 gate 条件未满足；
  - `invalid`：证据不可用（文件缺失 / 不可读 / schema 不匹配 / 缺字段 / 摘要不一致 /
    `restart_commit_sha` ≠ 本地 HEAD 或 ≠ `origin/cline-agent` HEAD / 早于最小快照）。
- **稳定 reason codes**：`HEAD_MISMATCH`、`RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT`、
  `CI_NOT_GREEN`、`MISSING_OPERATOR_IDENTITY`、`MISSING_HUMAN_ATTESTATION`、
  `DIGEST_MISMATCH`。
- **clear 判定最小事实集合**：`restart_commit_sha` 必须同时等于本地 `HEAD` 与
  `origin/cline-agent` HEAD，且 `git merge-base --is-ancestor` 判定其不早于最小快照
  `490c1decf1ba4b316019c7a272726d04601b8fab`；`ci_run_conclusion_on_restart_commit`
  必须为 `success`；`operator_identity` 与 `human_attestation.statement` 非空。
- **内容寻址摘要**：`compute_content_digest(data)` 对「除时间戳与自引用 `content_digest`
  外的规范 JSON」求 sha256；`content_digest` 为可选字段，存在时校验器校验一致
  （不一致 ⇒ `invalid` / `DIGEST_MISMATCH`）。
- **唯一写入口** `scripts/record_fresh_process_evidence.py`：**仅非 CI 环境**（
  `GITHUB_ACTIONS` / `CI` 环境变量未设置）且交互 TTY 或显式 `--input` 时允许写文件；
  在 CI 环境中运行必须 fail-closed（非零退出码、不落盘）。CLI 不调用网络 / GitHub API，
  仅校验本地文件与 git 元数据；CI run 信息以显式入参为准。
- **GPT 判定规则**：GPT 只以 `verify_evidence` 的 `verdict` 与 `reason_codes` 为唯一判定
  依据；`cleared` 才可据此解除该 gate，`not_cleared` / `invalid` 一律不清除。该状态文件
  不得由 CI 或任何自动化流水线自动生成；未显式调用 CLI 时文件不存在视作 gate 未清除。
  本证据不解除 `PHASE3_3_DATA`，不改变 Phase 3.3 blocker、L3/L4 与交易安全开关。





