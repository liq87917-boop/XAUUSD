"""宏观 vintage 的时间因果查询（Phase 3.0 W0-2）。"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from database.models import MacroEvent
from src.common.time import to_utc


def macro_events_as_of(
    session: Session,
    *,
    as_of: datetime,
    event_code: str | None = None,
    country: str | None = None,
) -> list[MacroEvent]:
    """返回 ``as_of`` 时真实可见的每个观测期最新 vintage。"""
    moment = to_utc(as_of, assume_tz=None, field_name="as_of")
    newer = aliased(MacroEvent)
    statement = sa.select(MacroEvent).where(
        MacroEvent.released_at <= moment,
        MacroEvent.effective_at <= moment,
        sa.or_(MacroEvent.vintage_end_at.is_(None), moment < MacroEvent.vintage_end_at),
        ~sa.exists().where(
            newer.source_id == MacroEvent.source_id,
            newer.event_code == MacroEvent.event_code,
            newer.country == MacroEvent.country,
            newer.event_at == MacroEvent.event_at,
            newer.released_at > MacroEvent.released_at,
            newer.released_at <= moment,
            newer.effective_at <= moment,
        ),
    )
    if event_code is not None:
        statement = statement.where(MacroEvent.event_code == event_code)
    if country is not None:
        statement = statement.where(MacroEvent.country == country)
    return list(
        session.scalars(statement.order_by(MacroEvent.event_at, MacroEvent.released_at)).all()
    )