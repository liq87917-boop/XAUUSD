# GOLD-053 attempt 1 失败用例清单（抢救记录）

> 来源：`.ai/runtime/recovery/GOLD-053-attempt-1-20260926T103401+0800/untracked/pytest_gold053.out`
> 方法：应用 recovery 的 `changes.patch` + 恢复 untracked 文件后，按 pytest 收集顺序
> （`pytest --collect-only -q`，共 4399 个测试）逐字符定位进度条中的 `F`。

## 结论（重要更正）

attempt 1 的实质失败是 **9 个（非 7 个）pre-existing 断言失败**，根因统一为：

> `PHASE3_3_DATA` 已被 human operator override 冻结到 `history_blockers`
> （`PROJECT_STATE.blockers == []`），但 8 个回归测试文件仍断言
> `PHASE3_3_DATA in blockers`。

**这些失败与 GOLD-053 的代码改动无关**：GOLD-053 引入的 6 个文件
（4 个回归测试 + `control_plane_status.py` + `control_plane_consistency.py` + 2 个新测试文件）
定向跑全部通过（58 passed）。

Windows pytest tmpdir `PermissionError` 发生在 `sessionfinish` 收尾阶段，是**次要环境噪音**，
只是让 FAILURES 摘要没打印、退出码非 0，从而被 Orchestrator 误判为 `terminal_not_completed`。

## 失败用例清单（按 pytest 收集顺序）

| # | 用例 | 失败断言（从源码推断） |
|---|------|------------------------|
| 1 | tests/integration/test_planner_autopilot_contract_regression.py::test_real_queue_only_marks_admissible_work_executable_under_blocker | `report["phase_blockers"] == ["PHASE3_3_DATA"]` |
| 2 | tests/integration/test_planner_autopilot_contract_regression.py::test_real_project_state_activates_phase_blocker_gate | `"PHASE3_3_DATA" in blocker_codes(state)` |
| 3 | tests/integration/test_planner_autopilot_contract_regression.py::test_real_plan_is_compliant_while_phase34_counterexample_is_rejected | 依赖 `blocker_codes(state)` 含 `PHASE3_3_DATA` |
| 4 | tests/integration/test_planner_mutation_precondition_regression.py::test_phase33_blocker_and_trading_invariants_unchanged | `"PHASE3_3_DATA" in [b["code"] for b in state["blockers"]]` |
| 5 | tests/integration/test_planner_refill_request_regression.py::test_phase33_blocker_and_trading_invariants_unchanged | `"PHASE3_3_DATA" in {b["code"] for b in payload["blockers"]}` |
| 6 | tests/integration/test_review_backlog_regression.py::test_phase33_blocker_and_trading_invariants_unchanged | `"PHASE3_3_DATA" in [b["code"] for b in state["blockers"]]` |
| 7 | tests/integration/test_review_closure_adjudication_regression.py::test_valid_adjudication_does_not_touch_ledger_or_state | `"PHASE3_3_DATA" in [b["code"] for b in state["blockers"]]` |
| 8 | tests/integration/test_review_closure_precondition_regression.py::test_real_repo_phase3_3_blocker_removal_fails_closed | `consistent_state()` 期望 `PHASE3_3_DATA` 在 `blockers` |
| 9 | tests/integration/test_review_evidence_manifest_regression.py::test_safety_invariants_and_pointers_unchanged | `"PHASE3_3_DATA" in {b["code"] for b in state["blockers"]}` |

## 根因说明

`override_audit` 中 `freeze_phase33_data_blocker`（2026-09-26T00:00:00Z，evidence_commit `a86eeb0c`）
把 `PHASE3_3_DATA` 从 `blockers` 移入 `history_blockers` 并冻结。此后：

- `PROJECT_STATE.blockers == []`
- `PROJECT_STATE.history_blockers` 含 `PHASE3_3_DATA`（frozen，`retryable=false`）

GOLD-049 只修正了 4 个回归文件（`result_terminal_canary` / `result_terminal_consistency` /
`review_binding` / `review_ledger_integrity`）的同名断言为
`PHASE3_3_DATA in (blockers | history_blockers)` 兼容写法，但**遗漏了另外 8 个文件**
（上表 1-9）。这 8 个文件仍硬编码 `PHASE3_3_DATA in blockers`，因此在冻结后失败。

## 分类

全部 9 个失败均属 **(c) fixture/断言过期** —— 测试读取真实 `.ai/PROJECT_STATE.json`，
期望 `PHASE3_3_DATA` 在 `blockers`，但实际已冻结到 `history_blockers`。

- 不是 (a) 实现错误：GOLD-053 的实现正确（6 个文件全绿）；
- 不是 (b) GOLD-053 语义统一后的断言更新：这是 GOLD-049 的**遗漏**，GOLD-053 没有触碰这 8 个文件。

## 对替代任务的建议

GPT 提替代任务前，必须先：

1. 建立 pytest 全绿基线：修正上表 8 个文件的 `PHASE3_3_DATA` 断言为
   `PHASE3_3_DATA in (blockers | history_blockers)` 兼容写法（不削弱校验，仅把
   「当前活跃 blocker」语义放宽为「活跃或冻结 blocker」）；
2. 明确授权改测试断言，并说明「旧语义 → 新语义」的变更理由。
