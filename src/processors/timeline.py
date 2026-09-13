"""观点时间因果契约（团队裁决 2：**不新增数据库列**，改为应用层 + Leakage 测试强制）。

背景与结论：
- ``author_opinions.effective_at`` = "该观点最早已可用时间"，必须 **不早于** 上游
  ``author_posts.effective_at`` —— 否则等于用未来信息解释过去，构成未来数据泄漏
  （`docs/10 §3`、`.clinerules` 第 1 条红线）；
- 该约束是**跨表**的，单表 CHECK 无法表达。团队裁决（2026-09）：
  **不**给 `author_opinions` 增加 `source_effective_at` 列，改为：
  1. 管道写入前调用 :func:`resolve_opinion_effective_at` 计算该字段；
  2. 写入前调用 :func:`assert_opinion_available_after_post` 做不变量校验（违规即抛错）；
  3. `tests/leakage/test_opinion_timeline.py` 覆盖违规场景（必须报错，不得静默）。
- 单表可强制部分已由 migration 0005 的 CHECK 承担（`created_at >= effective_at`）。
"""

from __future__ import annotations

from datetime import datetime

from src.common.exceptions import TimeSemanticsError
from src.common.time import UTC_TZ, to_utc

__all__ = [
    "assert_opinion_available_after_post",
    "ensure_utc_from_database",
    "resolve_opinion_effective_at",
]


def ensure_utc_from_database(value: datetime, *, field_name: str) -> datetime:
    """把**数据库读回**的时间统一为 UTC-aware（管道内的必要一步）。

    为什么需要它：SQLite（本地/测试库）不保存时区偏移，读回的是 naive datetime，
    而 PostgreSQL 的 TIMESTAMPTZ 读回是 aware。若直接把 naive 值交给
    :func:`resolve_opinion_effective_at`，严格守卫会（正确地）拒绝隐式转换——
    这正是它在生产环境防止"时区猜测"的设计。

    因此调用约定是：
    - **外部数据**（Feed / API / 手工 CSV）→ 必须显式声明时区（`to_utc(..., assume_tz=...)`）；
    - **数据库读回** → 用本函数（语义已知为 UTC，因为 Phase 1 起所有时间列都以 UTC 写入）。
    """
    return to_utc(value, assume_tz=UTC_TZ, field_name=field_name)


def resolve_opinion_effective_at(*, post_effective_at: datetime) -> datetime:
    """观点可用时间 = 上游帖子的可用时间。

    为什么不是"抽取完成时间"：观点只是对**已经公开**的信息的解释，不产生新的可用性；
    若改用抽取完成时间，同一批历史数据重放会得到不同结果（不可复现，违背
    `docs/01` "研究可复现" 原则）。

    Raises:
        TimeSemanticsError: 传入 naive datetime（`docs/06` 第 7 条：禁止 naive 时间）。
    """
    return to_utc(post_effective_at, assume_tz=None, field_name="post_effective_at")


def assert_opinion_available_after_post(
    *, opinion_effective_at: datetime, post_effective_at: datetime
) -> None:
    """不变量：``opinion_effective_at >= post_effective_at``（违规抛错，绝不静默修正）。

    Raises:
        TimeSemanticsError: 观点可用时间早于上游帖子可用时间（未来数据泄漏）。
    """
    opinion = to_utc(opinion_effective_at, assume_tz=None, field_name="opinion_effective_at")
    post = to_utc(post_effective_at, assume_tz=None, field_name="post_effective_at")
    if opinion < post:
        raise TimeSemanticsError(
            f"观点可用时间({opinion.isoformat()}) 早于上游帖子可用时间({post.isoformat()})："
            "构成未来数据泄漏（docs/10 §3、docs/04 §44 决策 6）",
            details={
                "opinion_effective_at": opinion.isoformat(),
                "post_effective_at": post.isoformat(),
            },
        )