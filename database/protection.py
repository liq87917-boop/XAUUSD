"""原始数据不可覆盖守卫（最高级别红线之一）。

对应文档：
- 03_数据库完整设计 第 10 节：禁止 UPDATE 覆盖主要研究事实表。
- 06_Cline开发规则 第 9 条：原始数据不可覆盖；修正必须"新版本 + superseded_by + provenance"。
- .clinerules 第二条：严禁覆盖原始数据。

实现方式：
    通过 SQLAlchemy ORM 事件（``before_update`` / ``before_delete``）在 flush 阶段拦截，
    比"口头约定"更可靠，且对 PostgreSQL / SQLite 行为一致，可被单元测试覆盖。

唯一合法例外：
    ``raw_items.superseded_by_id``：数据修正时指向新版本记录（04 §5）。因此
    该表只允许修改这一列，其余任何列变更都会被拒绝。
"""

from __future__ import annotations

from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import event

from src.common.exceptions import ImmutableRecordError

__all__ = ["ALLOWED_UPDATE_COLUMNS", "enable_immutability_protection"]

#: 表名 → 允许被 UPDATE 的列（未列出的列一律禁止修改）
ALLOWED_UPDATE_COLUMNS: Final[dict[str, frozenset[str]]] = {
    # 修正路径：仅允许写入指向新版本的 superseded_by_id
    "raw_items": frozenset({"superseded_by_id"}),
    # 媒体二进制落地后才写入对象存储地址
    "raw_media": frozenset({"storage_uri"}),
    # 加工结果完全 append-only：任何修改都必须产生新的 processor_version 记录
    "processed_items": frozenset(),
    # 数据版本是训练/回测的不可变快照标识；数据变化必须追加新版本。
    "data_versions": frozenset(),
    # 宏观 vintage 是时间因果事实：修订必须追加新 released_at 行，严禁覆盖旧值。
    "macro_events": frozenset(),
    # ------------------------------------------------------------------
    # Phase 2（Author Lab）派生事实表：同样完全 append-only
    # （03 §10 明确列出 author_opinions / author_skill_snapshots /
    #   author_weight_snapshots；propagation_edges 是派生事实，一并保护）
    # 修正方式：新的 parser_version / model_version 产生新行，历史行永不改写。
    # ------------------------------------------------------------------
    "author_opinions": frozenset(),
    "propagation_edges": frozenset(),
    "author_skill_snapshots": frozenset(),
    "author_weight_snapshots": frozenset(),
}


def _changed_column_keys(instance: Any) -> set[str]:
    """返回实例上已被修改的列名集合。"""
    state = sa.inspect(instance)
    changed: set[str] = set()
    for attribute in state.mapper.column_attrs:
        if state.attrs[attribute.key].history.has_changes():
            changed.add(attribute.key)
    return changed


def enable_immutability_protection(base: type[Any] | None = None) -> list[str]:
    """为已映射的事实表模型注册不可覆盖守卫。

    Args:
        base: ORM 基类，默认使用 :class:`database.base.Base`。

    Returns:
        实际注册守卫的表名列表（便于测试与启动日志核对）。
    """
    if base is None:  # pragma: no cover - 仅为避免顶部循环导入
        from database.base import Base as _Base

        base = _Base

    protected: list[str] = []
    for mapper in base.registry.mappers:
        mapped_class = mapper.class_
        local_table = mapper.local_table
        # 只处理真实表（继承/单表映射等场景可能不是 Table）
        if not isinstance(local_table, sa.Table):
            continue
        table_name = local_table.name
        allowed = ALLOWED_UPDATE_COLUMNS.get(table_name)
        if allowed is None:
            continue

        _attach_guards(mapped_class, table_name=table_name, allowed=allowed)
        protected.append(table_name)
    return sorted(protected)


def _attach_guards(mapped_class: type[Any], *, table_name: str, allowed: frozenset[str]) -> None:
    """给单个 ORM 类挂上 UPDATE / DELETE 守卫。"""

    @event.listens_for(mapped_class, "before_update")
    def _block_illegal_update(
        _mapper: Any,
        _connection: Any,
        target: Any,
        _allowed: frozenset[str] = allowed,
        _table: str = table_name,
    ) -> None:
        changed = _changed_column_keys(target)
        illegal = sorted(changed - _allowed)
        if illegal:
            raise ImmutableRecordError(
                f"表 {_table} 为研究事实数据，禁止覆盖历史内容；"
                f"本次尝试修改字段 {illegal}，允许修改的字段为 {sorted(_allowed)}。"
                "修正方式：新增版本记录并把旧记录的 superseded_by_id 指向新版本"
                "（03_数据库完整设计 第 10 节）。",
                details={"table": _table, "illegal_columns": illegal},
            )

    @event.listens_for(mapped_class, "before_delete")
    def _block_delete(
        _mapper: Any,
        _connection: Any,
        target: Any,
        _table: str = table_name,
    ) -> None:
        raise ImmutableRecordError(
            f"表 {_table} 为研究事实数据，禁止物理删除；"
            "如需修正请创建新版本并标记 superseded_by_id（03_数据库完整设计 第 10 节）。",
            details={"table": _table},
        )
