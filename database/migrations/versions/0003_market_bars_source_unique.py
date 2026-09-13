"""market_bars 增加按来源的唯一索引（团队批复）

Revision ID: 0003_market_bars_source_unique
Revises: 0002_collector_run_retry_count
Create Date: 2026-09-12

背景与目的：
    团队批复要求在表结构上增加 ``(source_id, symbol, timeframe, open_time)`` 的唯一索引，
    以确保"数据库层面绝对无法插入重复行情"。

    实现说明（两点必须读）：
    1. ``market_bars`` **没有 symbol 列**：04 §13 定义的是 ``instrument_id`` 外键，
       而 ``instruments.symbol`` 自带唯一约束 → symbol 与 instrument_id 是一一对应的，
       因此本迁移用 ``instrument_id`` 表达批复中的 symbol。
    2. 已存在的 ``uq_market_bars_instrument_timeframe_open``（instrument_id, timeframe,
       open_time）**更严格**，它本身就保证跨来源也不会出现重复行情；本迁移新增的
       ``uq_market_bars_source_instrument_timeframe_open`` 与之并不冲突，作用是：
       - 显式表达"同一来源内同一根 K 线绝不重复"的业务语义；
       - 为未来支持"多来源行情共存"预留保护（届时应另起迁移放宽严格约束）。
    注意：``source_id`` 可为 NULL（04 §13 允许），而 SQL 唯一索引不约束 NULL 值，
    因此手工写入的无来源 K 线仍需依赖严格约束兜底。

不可回滚说明：无（downgrade 直接删除该索引）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_market_bars_source_unique"
down_revision: str | None = "0002_collector_run_retry_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_market_bars_source_instrument_timeframe_open"
_COLUMNS = ["source_id", "instrument_id", "timeframe", "open_time"]


def upgrade() -> None:
    op.create_index(INDEX_NAME, "market_bars", _COLUMNS, unique=True)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="market_bars")
