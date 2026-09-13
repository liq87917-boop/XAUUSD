"""GOLD-AI 数据库包（ORM 模型、迁移、数据版本化）。

内部模块：
- ``database.base``：声明式基类、命名约定、通用字段 Mixin
- ``database.types``：PostgreSQL 类型映射（UUID / TIMESTAMPTZ / JSONB / NUMERIC）
- ``database.models``：Phase 1 表结构（sources / authors / raw_items / market_bars ...）
- ``database.protection``：研究事实表"不可覆盖"守卫
- ``database.session``：引擎与 Session 工厂
- ``database.migrations``：Alembic 迁移（唯一结构变更入口）

注意：本文件故意不导入子模块，避免包初始化时的循环导入；
      业务代码请显式导入，例如 ``from database.base import Base``。
"""

__all__: list[str] = []
