"""系统基础表（04 §40 job_runs / 03 §11 data_versions / 04 §41 audit_logs）。

Phase 1 作用：
- ``job_runs``：30 分钟调度框架的幂等与重试记录。``idempotency_key`` 唯一，
  保证"同一调度窗口重复触发只执行一次"（08_测试与验收标准 12）。
- ``data_versions``：任何训练 / 回测必须绑定 ``data_version_id``（03 §11）。
- ``audit_logs``：管理操作（数据源 / 作者 CRUD）的修改前后快照，永久可查。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from database.base import (
    Base,
    CreatedAtMixin,
    UpdatedAtMixin,
    UUIDPrimaryKeyMixin,
)
from database.models.enums import JobStatus
from database.types import JSONB, TIMESTAMP, UUID, enum_type

__all__ = ["AuditLog", "DataVersion", "JobRun"]


class JobRun(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """调度任务执行记录（幂等 + 重试 + 可观测）。"""

    __tablename__ = "job_runs"
    __table_args__ = (
        sa.Index("ix_job_runs_status_scheduled_at", "status", "scheduled_at"),
        sa.CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
    )

    job_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # 幂等键：同一调度窗口重复触发只允许执行一次（唯一约束兜底）
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    scheduled_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus, name="job_status", length=30),
        nullable=False,
        default=JobStatus.PENDING,
    )
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class DataVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """数据集版本（03 §11）。任何训练 / 回测必须绑定一个 data_version_id。"""

    __tablename__ = "data_versions"
    __table_args__ = (
        sa.UniqueConstraint("dataset_name", "version", name="uq_data_versions_dataset_version"),
        sa.CheckConstraint(
            "start_at IS NULL OR end_at IS NULL OR end_at >= start_at",
            name="period_order",
        ),
        sa.Index("ix_data_versions_generated_at", "generated_at"),
    )

    dataset_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    # 数据哈希：用于验证"同一版本可复现"（03 §12 Experiment 追溯）
    data_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    source_scope_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    filter_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    row_count: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """审计日志（管理操作永久留存，03 §16）。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_logs_created_at", "created_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
