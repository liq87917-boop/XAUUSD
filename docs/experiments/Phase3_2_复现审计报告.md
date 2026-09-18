# Phase 3.2 复现审计报告

> **结论：PASS**。同一真实输入连续运行两次，结果逐字段一致，数据库事实表行数不变。

- Technical 数据 SHA-256：`d0e0b39e1220b24428aa1cf39d7f68b9b5dc16ef8d25762d319eda77f7234d0d`
- Macro 组合 SHA-256：`aa6d1026c9aec1161b897c1569867fa6a1f22b0220a7ceba879c3ec1dc7c5bc6`
- 比较纪律：逐字段精确相等，不使用数值容差。

| 事实表 | 审计后行数 |
|---|---:|
| `feature_snapshots` | 11873 |
| `market_regimes` | 11872 |
| `author_skill_snapshots` | 0 |
| `author_weight_snapshots` | 0 |

该审计只证明确定性与零写库，不改变两个 Alpha 的 FAIL 结论。
