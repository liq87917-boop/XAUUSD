"""30 分钟采集调度核心（TD-09）。

职责边界（**重要**）：
- 本模块只做"调度 + 幂等 + 故障隔离 + 摘要落库"，**不得**出现任何 provider URL、
  解析逻辑或业务特例；采集器注册与构造交给 :mod:`src.collectors.bootstrap`。
- 复用现有 ``JobRun``（``idempotency_key`` 唯一约束）与 ``run_collectors``（单源故障隔离），
  **不新增 migration、不修改任何 schema**。

验收要点（08 §12）：
1. 同一 UTC 30 分钟槽重复触发只执行一次，且不产生第二条 ``job_runs``；
2. stale 的 RUNNING/RETRYING 可安全接管并 ``retry_count += 1``；未过期一律跳过；
3. 单 source 构造失败或执行失败不阻断其他 source，最终状态诚实为 PARTIAL_FAILED/FAILED；
4. ``output_json`` 只记录白名单摘要字段，绝不写入密钥或完整 source 配置。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import JobRun, Source
from database.models.enums import JobStatus
from src.collectors.base import BaseCollector
from src.collectors.registry import collector_for_source
from src.collectors.runner import CollectorRunResult, run_collectors
from src.collectors.types import CollectWindow
from src.common.time import to_utc, utc_now
from src.scheduler.slots import DEFAULT_INTERVAL_MINUTES, ScheduleSlot, resolve_slot

__all__ = [
    "JOB_TYPE",
    "CollectorFactory",
    "SchedulerRunResult",
    "SourceRunSummary",
    "aggregate_status",
    "collector_name_for",
    "default_stale_after",
    "enabled_collector_sources",
    "redact_secrets",
    "run_scheduled_collection_once",
]

_log = get_logger("scheduler.core")

#: ``job_runs.job_type`` 取值：便于运维按类型过滤调度记录
JOB_TYPE = "collector_scheduler"

#: 视为"仍在执行中"的状态（只有这些才允许按 stale 阈值接管）
_IN_FLIGHT_STATUSES: tuple[JobStatus, ...] = (JobStatus.RUNNING, JobStatus.RETRYING)

#: 单条错误 / 告警文本的最大长度（防止异常信息把 output_json 撑爆）
_MAX_ERROR_CHARS = 500

#: ``job_runs.error_message`` 的最大长度
_MAX_JOB_ERROR_CHARS = 2000

ClockFn = Callable[[], datetime]
CollectorFactory = Callable[[Source], BaseCollector]

#: 凭据擦除规则（宁多擦不可漏：Authorization / Bearer / sk- / api_key / token 等）
#: 注意 ``["']?`` —— 摘要里可能是 JSON（``{"api_key": "..."}``），键名后的引号必须容忍。
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization)\b[\"']?\s*[:=]\s*[^\s,;]+"), r"\1=***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]+"), r"\1 ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-***"),
    (re.compile(r"(?i)\b(api[_-]?key)\b[\"']?\s*[:=]\s*[^\s,;]+"), r"\1=***"),
    (
        re.compile(
            r"(?i)\b(access[_-]?token|refresh[_-]?token|secret|token)\b[\"']?\s*[:=]\s*[^\s,;]+"
        ),
        r"\1=***",
    ),
)


def redact_secrets(text: str) -> str:
    """把文本里的凭据擦成 ``***``（用于错误 / 告警摘要，避免进 ``output_json``）。"""
    redacted = text
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


@dataclass(frozen=True, slots=True)
class SourceRunSummary:
    """单个 source 的可审计摘要（只含白名单字段，**绝不包含 ``config_json``**）。"""

    source_name: str
    source_id: str
    collector_name: str | None
    status: str
    run_id: str | None = None
    fetched: int = 0
    inserted: int = 0
    duplicate: int = 0
    failed: int = 0
    skipped: int = 0
    retry_count: int = 0
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_hard_failure(self) -> bool:
        """硬失败：构造失败或执行失败（不是 PARTIAL_FAILED / DEGRADED）。"""
        return self.status in {"FAILED", "CONSTRUCTION_FAILED"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source_name,
            "source_id": self.source_id,
            "collector": self.collector_name,
            "status": self.status,
            "run_id": self.run_id,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "duplicate": self.duplicate,
            "failed": self.failed,
            "skipped": self.skipped,
            "retry_count": self.retry_count,
            "warnings": list(self.warnings),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class SchedulerRunResult:
    """一次调度触发的结果（``executed=False`` 表示命中幂等 / 在途，不重复执行）。"""

    slot: ScheduleSlot
    status: JobStatus
    executed: bool
    job_run_id: uuid.UUID | None
    retry_count: int
    reason: str
    sources: tuple[SourceRunSummary, ...] = ()

    @property
    def is_failure(self) -> bool:
        return self.status is JobStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_run_id": str(self.job_run_id) if self.job_run_id else None,
            "status": self.status.value,
            "executed": self.executed,
            "reason": self.reason,
            "retry_count": self.retry_count,
            "slot": self.slot.to_dict(),
            "sources": [summary.to_dict() for summary in self.sources],
        }

    def render(self) -> str:
        """人读单行摘要（CLI 打印用）。"""
        totals = _totals(self.sources)
        return (
            f"[scheduler] slot={self.slot.start_at.isoformat()} status={self.status.value} "
            f"executed={self.executed} reason={self.reason} retry_count={self.retry_count} "
            f"sources={totals['sources']} fetched={totals['fetched']} "
            f"inserted={totals['inserted']} duplicate={totals['duplicate']} "
            f"failed={totals['failed_records']}"
        )


def default_stale_after(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> timedelta:
    """默认 stale 阈值 = 3 个调度间隔（30 分钟 → 90 分钟）。"""
    return timedelta(minutes=interval_minutes * 3)


def collector_name_for(source: Source) -> str | None:
    """从 ``sources.config_json['collector']`` 解析采集器名称（缺失返回 ``None``）。"""
    config = source.config_json or {}
    name = config.get("collector")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def enabled_collector_sources(session: Session) -> tuple[Source, ...]:
    """读取"已启用且配置了采集器"的数据源（按 ``name`` 稳定排序，便于复现）。"""
    stmt = (
        sa.select(Source)
        .where(Source.enabled.is_(True))
        .where(Source.deleted_at.is_(None))
        .order_by(Source.name)
    )
    sources = session.scalars(stmt).all()
    return tuple(source for source in sources if collector_name_for(source) is not None)


def aggregate_status(summaries: Sequence[SourceRunSummary]) -> JobStatus:
    """按"诚实统计"聚合 ``JobRun`` 状态（绝不静默 SUCCESS）。"""
    if not summaries:
        return JobStatus.SUCCESS
    hard_failures = [summary for summary in summaries if summary.is_hard_failure]
    if not hard_failures and all(summary.status == "SUCCESS" for summary in summaries):
        return JobStatus.SUCCESS
    if len(hard_failures) == len(summaries):
        return JobStatus.FAILED
    return JobStatus.PARTIAL_FAILED


def _stored_utc(value: datetime | None) -> datetime | None:
    """把库中时间归一为 UTC。

    SQLite 不保存时区（本地 / CI 测试库），读取时**按 UTC 解释**；生产 PostgreSQL 为
    ``TIMESTAMPTZ``，天然 timezone-aware，此函数为幂等归一。
    """
    if value is None:
        return None
    return to_utc(value, field_name="job_runs.timestamp")


def _format_error(exc: BaseException) -> str:
    return redact_secrets(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR_CHARS]


def _format_message(text: str | None) -> str | None:
    if not text:
        return None
    return redact_secrets(text)[:_MAX_ERROR_CHARS]


def _find_job_run(session: Session, idempotency_key: str) -> JobRun | None:
    return session.scalar(sa.select(JobRun).where(JobRun.idempotency_key == idempotency_key))


def _resume_or_skip(
    session: Session,
    job_run: JobRun,
    *,
    now: datetime,
    stale_after: timedelta,
) -> tuple[JobRun, bool, str]:
    """在途（RUNNING/RETRYING）记录的处理：过期接管，未过期跳过。"""
    if job_run.status not in _IN_FLIGHT_STATUSES:
        return job_run, False, f"already_{job_run.status.value.lower()}"

    started_at = _stored_utc(job_run.started_at) or _stored_utc(job_run.scheduled_at)
    if started_at is None:  # pragma: no cover - ``scheduled_at`` 非空，防御性分支
        return job_run, False, "in_flight_unknown_start"
    if now - started_at < stale_after:
        # 未过 stale 阈值：不得并发接管（否则两个 worker 会同时采集同一槽）
        return job_run, False, "in_flight"

    job_run.retry_count = job_run.retry_count + 1
    job_run.status = JobStatus.RUNNING
    job_run.started_at = now
    job_run.finished_at = None
    job_run.error_message = None
    session.flush()
    return job_run, True, "stale_recovered"


def _acquire_job_run(
    session: Session,
    *,
    slot: ScheduleSlot,
    now: datetime,
    stale_after: timedelta,
) -> tuple[JobRun, bool, str]:
    """获取该槽的 ``JobRun``：命中幂等返回 ``(job_run, False, reason)``。

    首次创建用 savepoint 包裹，令唯一约束冲突（并发 worker）可恢复而非拖垮事务。
    """
    existing = _find_job_run(session, slot.idempotency_key)
    if existing is not None:
        return _resume_or_skip(session, existing, now=now, stale_after=stale_after)

    job_run = JobRun(
        job_type=JOB_TYPE,
        idempotency_key=slot.idempotency_key,
        scheduled_at=slot.start_at,
        started_at=now,
        status=JobStatus.RUNNING,
        retry_count=0,
        input_json={"slot": slot.to_dict(), "started_at": now.isoformat()},
    )
    try:
        with session.begin_nested():
            session.add(job_run)
            session.flush()
    except IntegrityError:
        # 并发场景：另一个 worker 已抢到同槽（唯一约束兜底），本进程不重复执行
        concurrent = _find_job_run(session, slot.idempotency_key)
        if concurrent is None:  # pragma: no cover - 唯一约束冲突后必然能查到
            raise
        return concurrent, False, "in_flight_concurrent"
    return job_run, True, "executed"


def _summary_for_construction_failure(
    source: Source, collector_name: str | None, error: str
) -> SourceRunSummary:
    return SourceRunSummary(
        source_name=source.name,
        source_id=str(source.id),
        collector_name=collector_name,
        status="CONSTRUCTION_FAILED",
        error=error,
    )


def _summary_from_result(
    source: Source,
    collector: BaseCollector,
    result: CollectorRunResult | None,
    *,
    run_error: str | None,
) -> SourceRunSummary:
    if result is None:
        return SourceRunSummary(
            source_name=source.name,
            source_id=str(source.id),
            collector_name=collector.collector_name,
            status="FAILED",
            error=run_error or "采集器未返回结果",
        )
    outcome = result.outcome
    return SourceRunSummary(
        source_name=source.name,
        source_id=str(source.id),
        collector_name=result.collector_name,
        status=result.status.value,
        run_id=str(result.run_id) if result.run_id else None,
        fetched=outcome.fetched_count if outcome else 0,
        inserted=outcome.inserted_count if outcome else 0,
        duplicate=outcome.duplicate_count if outcome else 0,
        failed=outcome.failed_count if outcome else 0,
        skipped=outcome.skipped_count if outcome else 0,
        retry_count=result.retry_count,
        warnings=tuple(redact_secrets(warning) for warning in result.warnings),
        error=_format_message(result.error_message),
    )


def _totals(summaries: Sequence[SourceRunSummary]) -> dict[str, int]:
    return {
        "sources": len(summaries),
        "succeeded": sum(1 for s in summaries if s.status == "SUCCESS"),
        "failed_sources": sum(1 for s in summaries if s.is_hard_failure),
        "fetched": sum(s.fetched for s in summaries),
        "inserted": sum(s.inserted for s in summaries),
        "duplicate": sum(s.duplicate for s in summaries),
        "skipped": sum(s.skipped for s in summaries),
        "failed_records": sum(s.failed for s in summaries),
    }


def _join_errors(summaries: Sequence[SourceRunSummary]) -> str | None:
    messages = [f"{s.source_name}: {s.error}" for s in summaries if s.error]
    if not messages:
        return None
    return redact_secrets("; ".join(messages))[:_MAX_JOB_ERROR_CHARS]


def _build_output_json(
    *,
    slot: ScheduleSlot,
    summaries: Sequence[SourceRunSummary],
    retry_count: int,
    reason: str,
    started_at: datetime,
    finished_at: datetime,
    window: CollectWindow,
) -> dict[str, Any]:
    warnings = [warning for summary in summaries for warning in summary.warnings]
    if not summaries:
        warnings.append("本轮没有已启用且配置了 config_json['collector'] 的数据源")
    return {
        "job_type": JOB_TYPE,
        "scheduler": {
            "interval_minutes": slot.interval_minutes,
            "slot": slot.to_dict(),
            "window": {
                "start_at": window.start_utc.isoformat(),
                "end_at": window.end_utc.isoformat(),
            },
            "retry_count": retry_count,
            "reason": reason,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": (finished_at - started_at).total_seconds(),
        },
        "totals": _totals(summaries),
        "sources": [summary.to_dict() for summary in summaries],
        "warnings": warnings,
    }


async def run_scheduled_collection_once(
    session: Session,
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    clock: ClockFn = utc_now,
    stale_after: timedelta | None = None,
    collector_factory: CollectorFactory | None = None,
) -> SchedulerRunResult:
    """执行"当前 UTC 调度槽"的一次采集（幂等；重复触发直接返回既有状态）。

    Args:
        session: 数据库会话（事务边界由调用方 ``session_scope`` 管理）。
        interval_minutes: 调度间隔（默认 30 分钟；必须整除 24 小时）。
        clock: 时间源（测试注入固定时钟，保证槽边界可断言）。
        stale_after: 在途记录多久后可接管；默认 :func:`default_stale_after`。
        collector_factory: ``Source → BaseCollector``；默认使用生产注册表
            （生产 CLI 应传 :func:`src.collectors.bootstrap.default_collector_factory`）。

    Returns:
        :class:`SchedulerRunResult`；``executed=False`` 表示命中幂等或在途未过期。
    """
    threshold = stale_after if stale_after is not None else default_stale_after(interval_minutes)
    if threshold <= timedelta(0):
        raise ValueError(f"stale_after 必须为正数，得到 {threshold}")

    now = to_utc(clock(), assume_tz=None, field_name="now")
    slot = resolve_slot(now, interval_minutes)

    job_run, acquired, reason = _acquire_job_run(
        session, slot=slot, now=now, stale_after=threshold
    )
    if not acquired:
        _log.info("调度槽 %s 命中幂等（%s），跳过执行", slot.start_at.isoformat(), reason)
        return SchedulerRunResult(
            slot=slot,
            status=job_run.status,
            executed=False,
            job_run_id=job_run.id,
            retry_count=job_run.retry_count,
            reason=reason,
        )

    factory = collector_factory or collector_for_source
    sources = enabled_collector_sources(session)
    window = CollectWindow(start_at=slot.start_at, end_at=slot.end_at)
    _log.info(
        "开始调度槽 %s：%d 个已启用来源（interval=%d 分钟，retry_count=%d）",
        slot.start_at.isoformat(),
        len(sources),
        slot.interval_minutes,
        job_run.retry_count,
    )

    plan: list[tuple[Source, BaseCollector | None, SourceRunSummary | None]] = []
    for source in sources:
        name = collector_name_for(source)
        try:
            collector = factory(source)
        except Exception as exc:  # noqa: BLE001 - 单源构造失败必须隔离，不得阻断其他源
            error = _format_error(exc)
            _log.warning("构造采集器失败：source=%s error=%s", source.name, error)
            plan.append((source, None, _summary_for_construction_failure(source, name, error)))
        else:
            plan.append((source, collector, None))

    constructed = [collector for _, collector, _ in plan if collector is not None]
    results: dict[tuple[str, str], CollectorRunResult] = {}
    run_error: str | None = None
    if constructed:
        try:
            outcome = await run_collectors(session, constructed, window=window, resume=True)
        except Exception as exc:  # noqa: BLE001 - 编排层兜底：异常也要落库，不能静默
            run_error = _format_error(exc)
            _log.error("采集编排失败：%s", run_error)
        else:
            results = {(item.collector_name, item.source_name): item for item in outcome}

    summaries: list[SourceRunSummary] = []
    for source, planned_collector, construction_failure in plan:
        if construction_failure is not None:
            summaries.append(construction_failure)
            continue
        if planned_collector is None:  # pragma: no cover - 与构造失败互斥
            continue
        summaries.append(
            _summary_from_result(
                source,
                planned_collector,
                results.get((planned_collector.collector_name, source.name)),
                run_error=run_error,
            )
        )

    started_at = _stored_utc(job_run.started_at) or now
    finished_at = to_utc(clock(), assume_tz=None, field_name="finished_at")
    status = aggregate_status(summaries)
    job_run.status = status
    job_run.finished_at = finished_at
    job_run.error_message = _join_errors(summaries)
    job_run.output_json = _build_output_json(
        slot=slot,
        summaries=summaries,
        retry_count=job_run.retry_count,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
        window=window,
    )
    session.flush()
    _log.info("调度槽 %s 完成：status=%s", slot.start_at.isoformat(), status.value)

    return SchedulerRunResult(
        slot=slot,
        status=status,
        executed=True,
        job_run_id=job_run.id,
        retry_count=job_run.retry_count,
        reason=reason,
        sources=tuple(summaries),
    )




