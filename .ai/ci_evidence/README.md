# XAUUSD 失败 CI — job 级日志与失败断言汇总

来源：GitHub Actions 仓库 `liq87917-boop/XAUUSD`，工作流 `CI`，分支 `cline-agent`。
抓取时间：2026-09-25（本地 `gh` CLI）。
对应 DeepSeek 审查（`runtime/plans/XAUUSD/20260925-180906-096098c2.json`）required_fixes 中列出的 6 次失败 run。

## 结论总览

- 工作流 `CI` 有 3 个 job：`lint + type check + tests (py3.12)`、`lint + type check + tests (py3.13)`、`PostgreSQL native schema + seeds`。
- 6 次 run 中，**py3.12 与 py3.13 两个 job 全部 failure**，`PostgreSQL native schema + seeds` 全部 success。
- 失败集中在 `pytest` 步骤；`Ruff lint` 与 `Mypy static type check` 步骤全部 success。

## 6 次失败 run

| # | run id | headSha | 提交标题 | 时间(UTC) | py3.12 | py3.13 | PostgreSQL |
|---|--------|---------|----------|-----------|--------|--------|------------|
| 1 | 36026387906 | 3ea96d4 | ai: complete GOLD-047 | 2026-09-24T16:18 | failure (2 failed) | failure (2 failed) | success |
| 2 | 36027627116 | cc9213b | control: block queue after GOLD-047 recurrence | 2026-09-24T16:28 | failure (6 failed) | failure (6 failed) | success |
| 3 | 36034679803 | 490c1de | control: align GOLD-047 recovery gate with current head | 2026-09-24T17:29 | failure (6 failed) | failure (6 failed) | success |
| 4 | 36048709353 | 271f66c | control: refresh GOLD-047 recovery gate | 2026-09-24T19:31 | failure (6 failed) | failure (6 failed) | success |
| 5 | 36055545594 | 6d7c7ec | control: refresh GOLD-047 current-head CI gate | 2026-09-24T20:32 | failure (6 failed) | failure (6 failed) | success |
| 6 | 36083054452 | 096098c | control: enforce supervisor GPT communication boundary | 2026-09-25T01:40 | failure (6 failed) | failure (6 failed) | success |

## 失败断言（稳定、跨 6 次 run 复现）

### 类别 A：候选状态指针漂移 `CANDIDATE_STATE_POINTER_DRIFT`（自 run 1 起即存在）

```
tests/integration/test_review_closure_precondition_regression.py::test_real_repo_matching_candidate_is_ready_and_zero_write
    >       assert payload["reason_codes"] == []
    E       AssertionError: assert ['CANDIDATE_S...OINTER_DRIFT'] == []
    E         Left contains one more item: 'CANDIDATE_STATE_POINTER_DRIFT'
    E         +     'CANDIDATE_STATE_POINTER_DRIFT',

tests/integration/test_review_closure_precondition_regression.py::test_real_repo_cli_temp_file_candidate_zero_write
    >       assert exit_code == precondition.EXIT_OK
    E       assert 2 == 0
```

含义：review 收口前置校验（`review_closure_precondition`）对真实仓库比对候选写集时，检测到
`reason_codes` 非空，包含 `CANDIDATE_STATE_POINTER_DRIFT`（候选 PROJECT_STATE 的
`last_reviewed_task` 等指针相对已提交 store/HEAD 发生漂移），因此退出码为 2（非 `EXIT_OK` 0）。

### 类别 B：`queue_status` 期望 ACTIVE 但为 BLOCKED（自 run 2 起引入）

以下 4 个回归文件中的同名用例 `test_phase33_blocker_and_trading_invariants_unchanged` 全部失败：

```
tests/integration/test_result_terminal_canary_regression.py::test_phase33_blocker_and_trading_invariants_unchanged
tests/integration/test_result_terminal_consistency_regression.py::test_phase33_blocker_and_trading_invariants_unchanged
tests/integration/test_review_binding_regression.py::test_phase33_blocker_and_trading_invariants_unchanged
tests/integration/test_review_ledger_integrity_regression.py::test_phase33_blocker_and_trading_invariants_unchanged

    >       assert state["queue_status"] == "ACTIVE"
    E       AssertionError: assert 'BLOCKED' == 'ACTIVE'
```

含义：这些回归测试读取仓库内已提交的 `.ai/PROJECT_STATE.json`，断言其 `queue_status == "ACTIVE"`；
但当前 HEAD 的 PROJECT_STATE 已被 GPT 判为 `status=BLOCKED / queue_status=BLOCKED`（队列被 block），
导致断言失败。该失败自 `cc9213b`（"control: block queue after GOLD-047 recurrence"）这一提交起出现，
与“阻断队列”的控制面变更直接对应。

## 失败计数演化

- run 1（3ea96d4）：`2 failed, 3837 passed, 3 skipped`（仅类别 A 的 2 个用例）。
- run 2–6（cc9213b 起）：`6 failed, 3833 passed, 3 skipped`（类别 A 的 2 个 + 类别 B 的 4 个）。

## 原始日志文件

- 每个 run 的 job 列表（含步骤级 conclusion）：`<run_id>.jobs.json`
- 每个 job 的完整日志：`<run_id>.job<job_id>.log`（`gh run view --job <job_id> --log`）

job id 对应关系（示例，其余见各 `.jobs.json`）：

| run id | py3.12 job id | py3.13 job id | PostgreSQL job id |
|--------|---------------|---------------|-------------------|
| 36026387906 | 107723881578 | 107723881835 | 107723881935 |
| 36027627116 | 107728071710 | 107728071456 | 107728071019 |
| 36034679803 | 107751697328 | 107751697156 | 107751697453 |
| 36048709353 | 107798570489 | 107798570235 | 107798570556 |
| 36055545594 | 107821418297 | 107821417923 | 107821418357 |
| 36083054452 | 107908942274 | 107908942100 | 107908942347 |

## 根因提示（供 GPT 复审定位，非裁决）

1. 类别 A 是数据/指针一致性漂移：`review_closure_precondition` 在真实仓库上发现候选状态指针
   （`last_reviewed_task` 相对 committed store/HEAD）漂移，`CANDIDATE_STATE_POINTER_DRIFT`。
2. 类别 B 是控制面状态与回归测试期望的冲突：GPT 判 BLOCKED 后提交的 `.ai/PROJECT_STATE.json`
   使 `queue_status=BLOCKED`，而 4 个回归测试硬编码期望 `ACTIVE`。

以上仅为 job 级失败事实与断言文本，不构成 PASS/FAIL 裁决。
