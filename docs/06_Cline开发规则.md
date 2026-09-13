# 06_Cline开发规则.md

# GOLD-AI Cline 开发规则 V1.0

> 本文件用于约束 Cline 在本项目中的开发行为。每次执行开发任务前，必须先读取本文件。

---

## 1. 总原则

Cline 的任务不是“尽快写代码”，而是：

1. 不破坏现有结构。
2. 按阶段推进。
3. 不擅自扩大范围。
4. 任何数据库修改可迁移。
5. 任何核心逻辑可测试。
6. 任何金融研究逻辑不得引入未来数据。
7. 默认不允许实盘交易。

---

## 2. 开始任务前必须执行

每次任务首先：

1. 阅读：
   - 01_系统总体架构设计
   - 02_系统详细设计
   - 03_数据库完整设计
   - 04_数据表结构及字段定义
   - 05_分阶段开发路线图
   - 06_Cline开发规则
2. 阅读当前阶段 Prompt。
3. 检查当前代码目录。
4. 检查已有 migration。
5. 检查已有 tests。
6. 输出“本次计划修改清单”。

在没有明确要求时，不重构与当前任务无关代码。

---

## 3. 修改范围规则

只允许修改当前阶段要求的内容。

禁止：

- 因“顺便优化”重写整个项目
- 擅自更换技术栈
- 擅自删除已有接口
- 擅自修改字段语义
- 擅自删除历史 migration
- 擅自开启 LIVE_TRADING

---

## 4. 文件规则

新建代码应进入既定目录。

禁止：

- 把所有逻辑写进 `main.py`
- 建立 `utils.py` 大杂烩
- Controller/API 层写模型训练逻辑
- ORM 模型里塞复杂业务逻辑

推荐：

```text
api → service → domain → repository
```

---

## 5. 命名规则

Python：

- 文件：snake_case
- 函数：snake_case
- 类：PascalCase
- 常量：UPPER_CASE

数据库：

- 表：snake_case 复数
- 字段：snake_case
- FK：`xxx_id`

---

## 6. 类型规则

所有新增 Python 代码尽可能提供 type hints。

禁止核心函数大量使用 `Any`。

金额、价格、收益相关必须优先 Decimal。

---

## 7. 时间规则

系统内部统一 UTC。

数据库用 `TIMESTAMPTZ`。

严禁 naive datetime。

必须明确：

- published_at
- collected_at
- effective_at
- event_at

---

## 8. 未来数据泄漏规则

这是项目最高等级规则之一。

任何特征：

```text
feature.effective_at <= prediction_at
```

任何训练标签：

- label 可以来自未来价格
- feature 不可以来自 prediction_at 之后

如果无法证明不存在 leakage，则任务不能标记完成。

---

## 9. 原始数据规则

原始数据不可覆盖。

修正：

- 新版本
- superseded_by
- provenance

禁止直接修改历史 raw 内容。

---

## 10. 数据库规则

所有表结构修改：

1. 修改 ORM
2. 生成 migration
3. 检查 migration
4. 执行 migration
5. 运行测试

禁止手改 DB 后不生成 migration。

---

## 11. API 规则

统一：

- `/api/v1`
- Pydantic request/response schema
- 明确 HTTP status
- 错误返回结构统一

建议错误格式：

```json
{
  "code": "VALIDATION_ERROR",
  "message": "...",
  "details": {}
}
```

---

## 12. Collector 规则

Collector 必须：

- 可独立运行
- 可 health check
- 可重试
- 可断点
- 幂等
- 去重
- 记录 run

不得因为一个 source 失败导致其他 source 不运行。

---

## 13. Processor 规则

任何处理结果必须记录：

- processor_name
- processor_version
- input
- output
- status

LLM 输出必须保留原始 response 或结构化 trace。

---

## 14. 模型规则

每次训练记录：

- dataset version
- feature version
- code version
- model parameters
- random seed
- evaluation window

禁止仅保存 `.pkl` 而没有元数据。

---

## 15. 回测规则

必须考虑：

- transaction cost
- spread
- slippage
- fee

禁止使用未来 K 线 close 决定同一根 K 线 open 的成交。

---

## 16. 策略版本规则

Strategy Version 永不覆盖。

修改任何：

- entry
- exit
- stop loss
- take profit
- confirmation
- position rule

都必须生成新版本。

---

## 17. 风险规则

Risk Engine 独立。

禁止策略代码绕过 risk evaluation。

Risk 拒绝后不得下模拟订单。

---

## 18. 实盘安全规则

全项目默认：

```env
LIVE_TRADING=false
ALLOW_EXTERNAL_ORDER_SUBMISSION=false
```

Cline 不得：

- 新增真实券商自动下单
- 默认打开实盘
- 移除风险门禁
- 使用模拟接口冒充实盘测试

---

## 19. 测试规则

新增功能必须配套测试。

测试分类：

- unit
- integration
- regression
- data_quality
- leakage

不能用“代码能启动”作为完成标准。

---

## 20. 完成任务前必须执行

至少运行：

```bash
pytest
```

如配置存在，再运行：

```bash
ruff check .
black --check .
mypy src
```

前端：

```bash
npm run lint
npm run build
```

---

## 21. 修改现有代码时

先搜索调用链。

修改一个 public function 前必须确认：

- 谁调用它
- 是否会破坏 API
- 是否会破坏 DB
- 是否会破坏测试

---

## 22. Bug 修复规则

Bug 修复必须：

1. 复现
2. 找根因
3. 添加回归测试
4. 修复
5. 测试通过

禁止只根据报错文本“猜修复”。

---

## 23. Cline 每轮输出格式

完成后必须输出：

```text
本次完成：
1. ...
2. ...

修改文件：
- ...

数据库迁移：
- ...

新增测试：
- ...

测试结果：
- ...

未完成/风险：
- ...

下一步建议：
- ...
```

---

## 24. 禁止伪完成

以下不算完成：

- TODO 占位
- fake/mock 永久替代真实接口
- UI 有按钮但无后端
- API 返回固定假数据
- test 被 skip
- 捕获 Exception 后静默忽略

Mock 只允许用于测试或明确的开发 Stub。

---

## 25. 阶段锁定规则

当前 Phase 未达到验收标准，不进入下一 Phase 主开发。

允许提前预留接口，但不要提前堆实现。

