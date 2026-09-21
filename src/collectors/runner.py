"""采集运行编排：游标续采 + ``collector_runs`` 落库 + 单源失败隔离。

对应文档：
- 06_Cline开发规则 第 12 条：Collector 必须可独立运行、可 health check、可重试、
  可断点、幂等、去重、记录 run；**不得因为一个 source 失败导致其他 source 不运行**。
- 04 §7 ``collector_runs``：统计字段（fetched / inserted / duplicate / failed）
  + ``cursor_json``（断点）+ ``error_message``。
- 08 §12 Scheduler 验收：中间某一步失败、重启 worker、断点恢复，
  不得重复生成不可幂等数据。

⚠️ 生产 / 研究环境必须使用 PostgreSQL；SQLite 仅用于本地开发与自动化测试。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import CollectorRun
from database.models.enums import CollectorRunStatus
from src.collectors.base import BaseCollector
from src.collectors.types import CollectOutcome, CollectWindow
from src.common.time import utc_now

__all__ = [
    "CollectorRunResult",
    "load_resume_cursor",
    "render_run_summary",
    "run_collector",
    "run_collectors",
]

_log = get_logger("collectors.runner")

#: 可作为续采起点的状态（成功 / 部分成功都保留游标）
_RESUMABLE_STATUSES: tuple[CollectorRunStatus, ...] = (
    CollectorRunStatus.SUCCESS,
    CollectorRunStatus.PARTIAL_FAILED,
)


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    """一次采集器运行的可观测结果（供 Scheduler / Dashboard / 测试使用）。"""

    collector_name: str
    source_name: str
    status: CollectorRunStatus
    run_id: uuid.UUID | None
    outcome: CollectOutcome | None
    error_message: str | None
    resumed_from_cursor: bool = False
    #: 数据质量告警（例如"低于预期条数"）：
    #: 同时写入 ``collector_runs.warnings_json``（非 NULL 即表示该轮为 WARNING 级运行）
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is CollectorRunStatus.SUCCESS

    @property
    def retry_count(self) -> int:
        return self.outcome.retry_count if self.outcome is not None else 0

    def summary(self) -> str:
        base = f"{self.collector_name}@{self.source_name}: {self.status.value}"
        if self.outcome is None:
            return f"{base} | {self.error_message}"
        suffix = f" | warnings={len(self.warnings)}" if self.warnings else ""
        return f"{base} | {self.outcome.summary()}{suffix}"


def load_resume_cursor(
    session: Session,
    *,
    collector_name: str,
    source_id: uuid.UUID | None,
    exclude_run_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """读取最近一次成功 / 部分成功运行的 ``cursor_json``（断点续采的来源）。

    只认这两个状态：FAILED 的游标可能对应"未推进"的位置，重跑即可，无需续采。
    """
    stmt: sa.Select[Any] = (
        sa.select(CollectorRun.cursor_json)
        .where(CollectorRun.collector_name == collector_name)
        .where(CollectorRun.status.in_(_RESUMABLE_STATUSES))
        .order_by(CollectorRun.started_at.desc())
        .limit(1)
    )
    if source_id is None:
        stmt = stmt.where(CollectorRun.source_id.is_(None))
    else:
        stmt = stmt.where(CollectorRun.source_id == source_id)
    if exclude_run_id is not None:
        stmt = stmt.where(CollectorRun.id != exclude_run_id)

    cursor = session.scalar(stmt)
    return dict(cursor) if cursor else None


async def run_collector(
    session: Session,
    collector: BaseCollector,
    *,
    window: CollectWindow,
    resume: bool = True,
    strict: bool = False,
) -> CollectorRunResult:
    """执行一个采集器的一轮采集，并把结果写入 ``collector_runs``。

    Args:
        session: 事务 Session（提交由调用方决定；Scheduler 每轮提交一次）。
        collector: 采集器实例。
        window: 采集时间窗口。
        resume: 是否从上一次成功 / 部分成功的运行续采（断点续传）。
        strict: True 时非预期异常原样抛出（调试用）；默认 False 以保证隔离与可观测。

    Returns:
        :class:`CollectorRunResult`（业务失败不抛出，除非 ``strict=True``）。
    """
    try:
        return await _run_once(session, collector, window=window, resume=resume, strict=strict)
    except Exception as exc:
        # 编排层自身异常（例如写 collector_runs 失败）：仍返回失败结果，避免中断整轮调度
        message = f"采集编排失败：{type(exc).__name__}: {exc}"
        _log.error("%s | %s", collector.collector_name, message)
        if strict:
            raise
        return CollectorRunResult(
            collector_name=collector.collector_name,
            source_name=collector.source.name,
            status=CollectorRunStatus.FAILED,
            run_id=None,
            outcome=None,
            error_message=message,
        )


async def _run_once(
    session: Session,
    collector: BaseCollector,
    *,
    window: CollectWindow,
    resume: bool,
    strict: bool,
) -> CollectorRunResult:
    """``run_collector`` 的实际实现：run 行与采集执行绑定在同一个事务里。"""
    run = CollectorRun(
        collector_name=collector.collector_name,
        source_id=collector.source.id,
        started_at=utc_now(),
        status=CollectorRunStatus.RUNNING,
    )
    session.add(run)
    session.flush()

    resume_cursor = (
        load_resume_cursor(
            session,
            collector_name=collector.collector_name,
            source_id=collector.source.id,
            exclude_run_id=run.id,
        )
        if resume
        else None
    )
    _log.info(
        "开始采集 %s@%s（续采=%s）",
        collector.collector_name,
        collector.source.name,
        resume_cursor is not None,
    )

    try:
        outcome = await collector.collect(session=session, window=window, cursor=resume_cursor)
    except Exception as exc:
        run.status = CollectorRunStatus.FAILED
        run.finished_at = utc_now()
        # 失败路径同样持久化重试次数（超时 / 限流重试是数据质量分析的关键信号）
        run.retry_count = collector.retry_count
        run.error_message = f"{type(exc).__name__}: {exc}"
        session.flush()
        _log.warning("%s 采集失败：%s", collector.collector_name, run.error_message)
        if strict:
            raise
        return CollectorRunResult(
            collector_name=collector.collector_name,
            source_name=collector.source.name,
            status=CollectorRunStatus.FAILED,
            run_id=run.id,
            outcome=None,
            error_message=run.error_message,
            resumed_from_cursor=resume_cursor is not None,
            warnings=collector.last_warnings,
        )

    run.status = outcome.status
    run.finished_at = outcome.finished_at
    run.fetched_count = outcome.fetched_count
    run.inserted_count = outcome.inserted_count
    run.duplicate_count = outcome.duplicate_count
    run.failed_count = outcome.failed_count
    run.retry_count = outcome.retry_count
    run.cursor_json = outcome.cursor
    run.error_message = outcome.error_message
    # 数据质量告警持久化：非 NULL 即表示本轮为 WARNING 级（阈值告警等）
    warning_messages = list(collector.last_warnings)
    run.warnings_json = {"warnings": warning_messages} if warning_messages else None
    session.flush()

    for warning in warning_messages:
        _log.warning("%s 数据质量告警：%s", collector.collector_name, warning)

    _log.info("完成采集 %s | %s", collector.collector_name, outcome.summary())
    return CollectorRunResult(
        collector_name=collector.collector_name,
        source_name=collector.source.name,
        status=outcome.status,
        run_id=run.id,
        outcome=outcome,
        error_message=outcome.error_message,
        resumed_from_cursor=outcome.resumed_from_cursor,
        warnings=collector.last_warnings,
    )


async def run_collectors(
    session: Session,
    collectors: Sequence[BaseCollector],
    *,
    window: CollectWindow,
    resume: bool = True,
) -> tuple[CollectorRunResult, ...]:
    """顺序执行多个采集器：**单个采集器失败不影响其他采集器**。

    对应 06_Cline开发规则 第 12 条与 08 §12：某个来源被封禁 / 超时，
    其他来源必须照常采集并各自留下 ``collector_runs`` 记录。
    """
    results: list[CollectorRunResult] = []
    for collector in collectors:
        results.append(await run_collector(session, collector, window=window, resume=resume))
    return tuple(results)


def render_run_summary(results: Sequence[CollectorRunResult]) -> str:
    """把一轮多采集器结果渲染成树状健康摘要（供日志 / 未来监控通知使用）。"""
    lines = ["Collector Run"]
    totals = {"fetched": 0, "inserted": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    for result in results:
        lines.append(f"├── {result.collector_name:<20} {result.status.value}")
        outcome = result.outcome
        if outcome is None:
            lines.append(f"│   └── {result.error_message or 'no outcome'}")
            continue
        if outcome.transport == "rest":
            lines.append("│   └── REST fallback used")
        totals["fetched"] += outcome.fetched_count
        totals["inserted"] += outcome.inserted_count
        totals["duplicate"] += outcome.duplicate_count
        totals["skipped"] += outcome.skipped_count
        totals["failed"] += outcome.failed_count
    lines += [
        "",
        "Total:",
        f"fetched    {totals['fetched']}",
        f"inserted   {totals['inserted']}",
        f"duplicate  {totals['duplicate']}",
        f"skipped    {totals['skipped']}",
        f"failed     {totals['failed']}",
    ]
    return "\n".join(lines)

