"""控制面不变量单一事实源（GOLD-049）。

``PROJECT_STATE`` 的 ``queue_status`` 是控制面状态。回归测试必须从唯一权威来源
（``PROJECT_STATE`` 结构本身 + ``queue_status`` 唯一允许的取值域 + 与 ``status`` /
``blockers`` 的语义一致性）读取事实，而不是把某个具体快照值（例如 ``ACTIVE``）硬编码
进断言。控制面的合理状态迁移（例如 GPT 判 BLOCKED 后队列进入 ``HOLD`` / ``BLOCKED``）
不应被当作断言失败。

本模块**只读**：只做纯函数校验并输出稳定 reason code，绝不写文件、绝不改变
``PHASE3_3_DATA`` blocker，绝不弱化交易安全不变量（``LIVE_TRADING=false`` /
``ALLOW_EXTERNAL_ORDER_SUBMISSION=false``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: ``queue_status`` 唯一允许的取值域。
QUEUE_STATUS_DOMAIN: frozenset[str] = frozenset({"ACTIVE", "HOLD", "BLOCKED"})

#: 稳定 reason code（fail-closed，绝不本地化 / 绝不改写历史语义）。
QUEUE_STATUS_MISSING = "QUEUE_STATUS_MISSING"
QUEUE_STATUS_NOT_A_STRING = "QUEUE_STATUS_NOT_A_STRING"
QUEUE_STATUS_OUT_OF_DOMAIN = "QUEUE_STATUS_OUT_OF_DOMAIN"
QUEUE_STATUS_ACTIVE_WHILE_BLOCKED = "QUEUE_STATUS_ACTIVE_WHILE_BLOCKED"
QUEUE_STATUS_BLOCKED_WHILE_ACTIVE = "QUEUE_STATUS_BLOCKED_WHILE_ACTIVE"
QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS = "QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS"


def active_blocker_codes(state: Mapping[str, Any]) -> frozenset[str]:
    """返回当前未解除（``blockers``）blocker 的 code 集合，只读。"""
    blockers = state.get("blockers") or []
    if not isinstance(blockers, list):
        return frozenset()
    return frozenset(
        blocker["code"]
        for blocker in blockers
        if isinstance(blocker, dict) and isinstance(blocker.get("code"), str)
    )


def queue_status_consistency_violations(state: Mapping[str, Any]) -> list[str]:
    """校验 ``queue_status`` 与 ``status``/``blockers`` 的语义一致性。

    返回稳定 reason code 列表；空列表表示语义自洽。合法判定规则（fail-closed，
    只放行显式允许的组合）：

    * ``queue_status`` 必须在 ``{ACTIVE, HOLD, BLOCKED}`` 内；
    * ``status == "BLOCKED"`` 时 ``queue_status`` 只能是 ``{BLOCKED, HOLD}``
      （阻断项目不得有 ACTIVE 队列）；
    * ``status == "ACTIVE"`` 时 ``queue_status`` 只能是 ``{ACTIVE, HOLD}``
      （活动项目不得有 BLOCKED 队列）；
    * 存在未解除 blocker 时 ``queue_status`` 不能是 ``ACTIVE``（blocker 阻断队列运行）。
    """
    violations: list[str] = []

    queue_status = state.get("queue_status")
    if queue_status is None:
        return [QUEUE_STATUS_MISSING]
    if not isinstance(queue_status, str):
        return [QUEUE_STATUS_NOT_A_STRING]
    if queue_status not in QUEUE_STATUS_DOMAIN:
        violations.append(QUEUE_STATUS_OUT_OF_DOMAIN)

    status = state.get("status")
    has_active_blocker = bool(active_blocker_codes(state))

    if status == "BLOCKED" and queue_status == "ACTIVE":
        violations.append(QUEUE_STATUS_ACTIVE_WHILE_BLOCKED)
    if status == "ACTIVE" and queue_status == "BLOCKED":
        violations.append(QUEUE_STATUS_BLOCKED_WHILE_ACTIVE)
    if has_active_blocker and queue_status == "ACTIVE":
        violations.append(QUEUE_STATUS_ACTIVE_WITH_ACTIVE_BLOCKERS)

    return violations
