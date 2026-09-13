"""数据库类型统一映射（03_数据库完整设计 第 14 节：金融数值类型）。

生产 / 研究环境的唯一目标数据库是 PostgreSQL：
- 主键      → UUID（由 UUID v7 生成，时间有序）
- 时间      → TIMESTAMPTZ（timezone-aware，系统内部统一 UTC）
- 结构化    → JSONB
- 价格/金额 → NUMERIC(20,8)；收益率 NUMERIC(18,10)；概率 NUMERIC(12,10)

本地单测与开发允许使用 SQLite（``sqlite+pysqlite:///...``），因此对
PostgreSQL 专属类型提供 SQLite 变体（JSONB → JSON，UUID → CHAR(32)）。
这**只影响测试环境 DDL**，不会改变 PostgreSQL 上的实际类型。

⚠️ 强制约束：**生产 / 研究环境必须使用 PostgreSQL**。SQLite 仅用于本地开发与
   自动化测试 —— 它不保存时区偏移（``TIMESTAMPTZ`` 语义丢失）、没有 JSONB 索引与
   原生 UUID、也缺少并发写入与分区能力，绝不可承载采集数据与研究事实数据的长期存储。
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

__all__ = [
    "AMOUNT",
    "JSONB",
    "MACRO_VALUE",
    "PRICE",
    "PROBABILITY",
    "PROBABILITY_ANY",
    "QUANTITY",
    "RETURN_RATE",
    "TIMESTAMP",
    "UUID",
    "VOLUME",
    "enum_type",
]

#: PostgreSQL JSONB；SQLite 退化为 JSON（仅测试/本地开发）
JSONB = _PG_JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")

#: 主键 / 外键统一类型（as_uuid=True：Python 侧使用 uuid.UUID）
UUID = sa.Uuid(as_uuid=True)

#: TIMESTAMPTZ：所有时间字段必须 timezone-aware（06_Cline开发规则 第 7 条）
TIMESTAMP = sa.DateTime(timezone=True)

#: 价格 NUMERIC(20,8)
PRICE = sa.Numeric(20, 8)
#: 金额 / 数量 NUMERIC(20,8)
AMOUNT = sa.Numeric(20, 8)
QUANTITY = sa.Numeric(20, 8)
#: 成交量 NUMERIC(28,8)
VOLUME = sa.Numeric(28, 8)
#: 宏观指标值 NUMERIC(28,10)
MACRO_VALUE = sa.Numeric(28, 10)
#: 收益率 NUMERIC(18,10)
RETURN_RATE = sa.Numeric(18, 10)
#: 概率 / 分数 NUMERIC(12,10)，取值范围 0~1
PROBABILITY = sa.Numeric(12, 10)
#: 可为负的分数（如 sentiment -1~1）
PROBABILITY_ANY = sa.Numeric(12, 10)

# 说明：SQLAlchemy 的 TypeEngine 实例不可变，可安全跨列复用；
# 但 sa.Enum 会携带元素集合/约束名，必须每列新建，故封装成 enum_type()。


def enum_type[E: Enum](enum_cls: type[E], *, name: str, length: int) -> sa.Enum:
    """构造 ``VARCHAR(length) + CHECK`` 形式的枚举列类型。

    Args:
        enum_cls: Python 枚举类（本项目统一使用 ``StrEnum``）。
        name: 约束名片段，最终约束名为 ``ck_<table>_<name>``（命名约定生成）。
        length: VARCHAR 长度，严格对齐 04_数据表结构及字段定义。

    Returns:
        ``sa.Enum`` 实例（``native_enum=False``，不创建 PostgreSQL ENUM 类型）。

    设计说明：
        不使用 PostgreSQL native enum，原因是研究系统会持续新增取值
        （Regime、alpha_type、状态机等），native enum 的 ALTER TYPE
        在历史 migration 上的维护成本过高；VARCHAR + CHECK 既有约束强度，
        又可通过普通 migration 演进。
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
