# Evidence 输入模板（GOLD-006）

本目录是 Author / News 证据的 **operator-ready 模板**，与
`src/evidence/templates.py` 生成器保持一致（模板版本 `TEMPLATE_SCHEMA_VERSION = 1`）。

| 文件 | 用途 |
|---|---|
| `author_evidence_template.csv` | 作者帖子类证据（需 `author_name` / `external_account_id`） |
| `news_evidence_template.csv` | 新闻 / 快讯类证据 |

## 1. 为什么示例行不会被当成真实证据

模板里两行数据是**示例**，不是可用证据：

- 每行都带 `record_kind=example`、`is_mock=true`；
- 其余字段是占位值，其中时间不是合法 ISO8601、`authorization_status=PENDING`、
  三项 `permits_* = false`。

导入入口（`scripts/intake_evidence.py`）命中任一标记列即判
`SYNTHETIC_EVIDENCE` 并**整行隔离**：不写库、不计入 qualification ledger。
即使有人手工删掉标记列，示例行仍会因授权缺失 / 时间非法被隔离。

## 2. 使用步骤（默认只读、零网络）

```powershell
# ① 生成 / 刷新模板（默认打印到 stdout；--out 默认拒绝覆盖已存在文件）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness template --scope author
.\.venv\Scripts\python.exe -m scripts.evidence_readiness template --scope news `
  --out examples/evidence/news_evidence_template.csv

# ② 复制模板 → 删掉示例行 → 按契约逐行填写真实（已授权）数据
#    - published_at / collected_at / available_at 必须带时区，且不得未来
#    - collected_at 必须晚于 published_at；不得用当前时间补齐
#    - available_at 必须有独立 availability_provenance + availability_reference
#    - 授权只有显式 APPROVED + 白名单依据 + 可核验引用 + 三项 true + 人工签认才放行

# ③ dry-run / preflight：只读量化（不写库、不联网）
.\.venv\Scripts\python.exe -m scripts.evidence_readiness preflight --scope news `
  --input <填写后的文件> --json

# ④ 查看隔离行原因（原因码稳定、文本脱敏），修正原始来源后重复 ③
# ⑤ 显式提交（append-only；需要人工核验授权后才可执行）
.\.venv\Scripts\python.exe -m scripts.intake_evidence --scope news --input <填写后的文件> `
  --no-dry-run --manifest logs/evidence/news_manifest.json --quarantine logs/evidence/news_quarantine.jsonl

# ⑥ 一键资格复核：串联只读台账与现有 Phase 3.3 qualification report
.\.venv\Scripts\python.exe -m scripts.evidence_readiness recheck --json
```

## 3. 硬门槛（只报告，不放宽）

| 范围 | 阈值（`src/alpha/evidence_gate.py`） |
|---|---|
| Author | 每个来源账号 ≥ 30 条具备独立历史可用证据的可信帖子 |
| News | ≥ 200 条 eligible、覆盖 ≥ 90 天、单一来源 ≤ 40% |

`blocker_active` 与 `human_gate_required` 恒为 `true`：量化达标也不解除
`PHASE3_3_DATA`，授权法律效力与历史可用时间证据必须由人工 Gate 核验。
