"""审计留痕（04 §41 ``audit_logs``）：管理操作必须记录修改前后快照。

对应文档：
- 04 §41：``user_id`` / ``action`` / ``entity_type`` / ``entity_id`` / ``before_json`` /
  ``after_json`` / ``ip_address``
- 03 §16：审计信息**永久留存**（普通日志仅 90~180 天）
- 06_Cline开发规则 第 13 条：管理操作必须可追溯；第 9 条：原始数据不可覆盖

设计要点：
1. 审计写入与业务修改在**同一事务**：业务不提交，审计也不可见（不会出现"改了却没记"）。
2. 快照只包含**列字段**且全部转成 JSON 可序列化形式
   （UUID → str、datetime → ISO8601、Enum → value、Decimal → str），便于 PG JSONB 查询与人工比对。
3. 审计表本身 append-only（不在 `PROTECTED_TABLES` 中，但 repository 不提供更新/删除入口）。
4. **不记录密钥等敏感信息**：只快照业务字段（本模块不做任何脱敏之外的额外写入）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuditLog

__all__ = ["record_audit", "snapshot_model"]


def _jsonable(value: Any) -> Any:
    """把列值转成 JSON 可序列化形式（保持人类可读，便于审计比对）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def snapshot_model(instance: Any) -> dict[str, Any]:
    """把 ORM 实例的列字段快照成 JSON 可序列化字典（不含关系字段）。"""
    state = sa.inspect(instance)
    return {
        attribute.key: _jsonable(getattr(instance, attribute.key))
        for attribute in state.mapper.column_attrs
    }


def record_audit(
    session: Session,
    *,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """写入一条审计记录并 flush（提交由调用方决定，保证与业务修改同事务）。"""
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=_jsonable(before) if before is not None else None,
        after_json=_jsonable(after) if after is not None else None,
        user_id=user_id,
        ip_address=ip_address,
    )
    session.add(entry)
    session.flush()
    return entry