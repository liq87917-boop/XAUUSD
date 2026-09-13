"""SQLAlchemy 2.0 声明式基类、命名约定与通用字段 Mixin。

对应文档：
- 03_数据库完整设计 第 14 节（金融数值类型）、第 15 节（外键策略）、第 19 节（迁移原则）
- 04_数据表结构及字段定义 第 1 节（通用字段规范）
- 06_Cline开发规则 第 5 条（命名规则）、第 7 条（时间规则）

命名约定说明：
    Alembic 在执行 ``op.create_table`` 时会自动继承 ``target_metadata.naming_convention``
    （见 alembic/operations/schemaobj.py），因此 ORM 元数据与 migration 生成的
    约束名完全一致，跨 PostgreSQL / SQLite 也是确定性的。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from database.types import JSONB, TIMESTAMP, UUID
from src.common.time import utc_now
from src.common.uuid7 import uuid7

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "CreatedAtMixin",
    "SoftDeleteMixin",
    "UUIDPrimaryKeyMixin",
    "UpdatedAtMixin",
]

#: 约束命名约定（表/字段全部 snake_case，见 06_Cline开发规则 第 5 条）
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        datetime: TIMESTAMP,
        uuid.UUID: UUID,
        dict[str, Any]: JSONB,
    }


class UUIDPrimaryKeyMixin:
    """UUID v7 主键 Mixin（时间有序，写入局部性优于 UUID v4）。"""

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid7)


class CreatedAtMixin:
    """入库创建时间（UTC，timezone-aware）。"""

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=utc_now)


class UpdatedAtMixin:
    """更新时间。

    仅"可变实体"（sources / authors / author_accounts / instruments / job_runs）使用；
    研究事实表（raw_items / processed_items 等）为 append-only，禁止 UPDATE，
    因此不得继承本 Mixin（见 03_数据库完整设计 第 10 节）。
    """

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=utc_now, onupdate=utc_now
    )


class SoftDeleteMixin:
    """软删除标记（03_数据库完整设计 第 15 节：删除使用软删除）。"""

    is_deleted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True, default=None)
