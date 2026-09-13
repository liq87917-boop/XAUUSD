"""raw_items 仓储层：数据修正路径 ①（先插新记录，再回填取代指针）。

对应文档：
- 03_数据库完整设计 第 10 节：禁止 UPDATE 覆盖；修正 = 新版本 + ``superseded_by_id`` + provenance。
- .clinerules 第二条：严禁覆盖原始数据。
- ``database/models/raw.py``：两条合法修正路径（本文件实现路径 ①）。

为什么必须"先插新记录、再回填指针"：
    ``raw_items.superseded_by_id`` 是指向 ``raw_items.id`` 的外键，且
    ``(source_id, source_record_id)`` 是严格唯一键（03 §7）。因此正确顺序是：
      ① 插入新记录（来源平台给出的新 ``source_record_id``）→ flush；
      ② 把旧记录的 ``superseded_by_id`` 回填为新记录主键 → flush。
    若先改指针，会因外键指向不存在的行而失败；若把同一自然键当"新版本"插入，
    会违反唯一键（这正是 raw.py 中记录的约束条件）。

幂等性：重复调用同一 (original, replacement) 组合为无操作（不写库、不报错）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import RawItem
from src.common.exceptions import ImmutableRecordError

__all__ = ["supersede_raw_item"]


def supersede_raw_item(session: Session, *, original: RawItem, replacement: RawItem) -> RawItem:
    """把 ``original`` 标记为"已被 ``replacement`` 取代"，返回 ``original``。

    Args:
        session: 当前事务的 Session（提交由调用方决定，便于批量修正使用同一事务）。
        original: 被取代的旧记录（必须已持久化）。
        replacement: 取代它的新记录（**必须已入库 / 已 flush**，即修正路径 ① 的第二步）。

    Returns:
        已回填 ``superseded_by_id`` 的 ``original``。

    Raises:
        ValueError: 参数非法：新旧同一条记录、新记录尚未入库、新记录自身已被取代、
            或新旧记录不属于同一 ``source_id``。
        ImmutableRecordError: 旧记录已被**其它**记录取代；禁止改写取代链
            （保留修正历史的可追溯性）。
    """
    if original is replacement or original.id == replacement.id:
        raise ValueError("replacement 必须是与 original 不同的 raw_items 记录")

    replacement_state = sa.inspect(replacement)
    if not replacement_state.persistent:
        raise ValueError(
            "replacement 必须先入库（session.add + flush）再回填取代指针："
            "raw_items.superseded_by_id 是指向 raw_items 的外键（修正路径 ①）"
        )
    if replacement.superseded_by_id is not None:
        raise ValueError("replacement 自身已被取代，不能作为新的活动版本")
    if replacement.source_id != original.source_id:
        raise ValueError("修正路径 ① 要求新旧记录属于同一 source_id（跨来源取代请走显式迁移）")

    # 幂等：已经指向同一条新记录时不重复写入
    if original.superseded_by_id == replacement.id:
        return original

    if original.superseded_by_id is not None:
        raise ImmutableRecordError(
            f"raw_items {original.id} 已被 {original.superseded_by_id} 取代，禁止改写取代链；"
            "如需再次修正，请基于当前活动版本追加新记录",
            details={
                "raw_item_id": str(original.id),
                "current_superseded_by": str(original.superseded_by_id),
            },
        )

    # 唯一被允许的 UPDATE 列（由 database/protection.py 强制）
    original.superseded_by_id = replacement.id
    session.flush()
    return original
