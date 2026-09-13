"""采集运行记录（04 §7 collector_runs）。

对应文档：
- 06_Cline开发规则 第 12 条：Collector 必须可 health check、可重试、可断点、
  幂等、去重、记录 run；单个 source 失败不得影响其他 source。
- 08_测试与验收标准 4：collector_run 必须有统计数据。

为什么统计字段必须在库里而不是日志里：
    "无大规模重复"（Phase 1 验收门槛）需要可查询的 duplicate/fetched 比例，
    日志无法做长期趋势与告警。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from database.models.enums import CollectorRunStatus
from database.types import JSONB, TIMESTAMP, UUID, enum_type

__all__ = ["CollectorRun"]


class CollectorRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """一次采集器运行的完整记录（可重试、可断点、可审计）。

    字段说明（04 §7 + 团队批复）：
    - 04 §7 定义的统计列：fetched / inserted / duplicate / failed + cursor_json + error_message；
    - ``retry_count`` 为 Phase 1 第 2 步经团队批复新增（migration 0002），
      持久化"3 次重试机制"实际触发的重试次数，便于数据质量分析与 Dashboard 展示。
    """

    __tablename__ = "collector_runs"
    __table_args__ = (
        sa.Index("ix_collector_runs_name_started_at", "collector_name", "started_at"),
        sa.Index("ix_collector_runs_status", "status"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
        sa.CheckConstraint("fetched_count >= 0", name="fetched_count_non_negative"),
        sa.CheckConstraint("inserted_count >= 0", name="inserted_count_non_negative"),
        sa.CheckConstraint("duplicate_count >= 0", name="duplicate_count_non_negative"),
        sa.CheckConstraint("failed_count >= 0", name="failed_count_non_negative"),
        sa.CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
    )

    collector_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # 可为空：部分采集器（如宏观日历）一次覆盖多个来源
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    status: Mapped[CollectorRunStatus] = mapped_column(
        enum_type(CollectorRunStatus, name="collector_run_status", length=30),
        nullable=False,
        default=CollectorRunStatus.PENDING,
    )
    fetched_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    inserted_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # 团队批复新增（migration 0002）：持久化超时重试次数，供数据质量分析与 Dashboard 使用。
    # server_default 与 migration 保持一致，保证"ORM 元数据 == 迁移结果"（漂移测试强制）。
    retry_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    # 团队批复新增（migration 0004）：数据质量告警
    # （例如低于 sources.config_json['min_records_per_run']）。
    # 结构：{"warnings": ["..."]}；**非 NULL 即表示本轮为 WARNING 级运行**（同时也会打 WARNING
    # 日志），这样 Dashboard / 数据质量巡检无需依赖日志文件即可查询告警。
    warnings_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    # 断点：下一次增量采集的起点（如微博 since_id、行情 API 最新 open_time）
    cursor_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
