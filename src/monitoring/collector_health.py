"""只读的采集运行健康度聚合层（GOLD-004）。

职责边界（**重要**）：
- 本模块**只读**：只执行 SELECT，不写库、不修改任何历史事实、不解除 Phase 3.3 阻塞；
- 复用 Scheduler 已确立的口径（``JOB_TYPE`` / ``collector_name_for`` / ``default_stale_after``），
  **禁止另造互相冲突的阈值**；
- 事实来源：``collector_runs``（source 级运行事实）、``raw_items``（实际采集事实）、
  ``processed_items``（加工事实，经 ``raw_items.source_id`` 归因）、``job_runs``（调度槽事实）、
  ``sources``（启用状态与采集器配置的**派生**值）；
- 未知 / 空库 / 陈旧 / 从未成功 / 只有在途运行 / Processor 未观测 一律**不得**报成 HEALTHY；
- 输出只允许白名单字段（来源名、类型、计数、时间、状态、脱敏后的错误摘要），
  **绝不输出** ``sources.config_json`` 原文或任何凭据（擦除见 ``src.common.redaction``）。

``processed_items.status`` → 计数口径（唯一解释，禁止在别处重新定义）：
``SUCCESS`` → ``success``；``SKIPPED`` → ``rejected``（``DUPLICATE`` 与 ``REJECTED``
在落库时都映射为 ``SKIPPED``——两者都不产出新事实，见
``src/processors/collection/contracts.py``）；``FAILED`` → ``failed``；
``PENDING`` / ``PROCESSING`` / ``RETRYING`` → ``in_flight``。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import CollectorRun, JobRun, ProcessedItem, RawItem, Source
from database.models.enums import CollectorRunStatus, JobStatus, ProcessStatus
from src.common.redaction import safe_text, safe_url
from src.common.time import to_utc, utc_now
from src.scheduler.core import JOB_TYPE, collector_name_for, default_stale_after
from src.scheduler.slots import DEFAULT_INTERVAL_MINUTES

__all__ = [
    "DEFAULT_STALE_AFTER",
    "DEFAULT_WINDOW_HOURS",
    "FAILED_STREAK_THRESHOLD",
    "HEALTH_SCHEMA_VERSION",
    "HealthReport",
    "HealthState",
    "ProcessorCounts",
    "ProcessorState",
    "ProcessorSummary",
    "SchedulerSummary",
    "SourceHealth",
    "build_health_report",
    "build_source_health",
    "classify_processor_state",
    "classify_source_state",
    "consecutive_failures",
    "load_health_report",
    "render_health_report",
]

#: 默认观测窗口：最近 24 小时（= 48 个 30 分钟调度槽）
DEFAULT_WINDOW_HOURS: Final[int] = 24
#: 连续硬失败达到该次数即判 FAILED（连败口径，不是"窗口内失败总数"）
FAILED_STREAK_THRESHOLD: Final[int] = 3
#: 陈旧阈值：复用 Scheduler 的 3 倍调度间隔（30 分钟 → 90 分钟），禁止另造阈值
DEFAULT_STALE_AFTER: Final[timedelta] = default_stale_after(DEFAULT_INTERVAL_MINUTES)
#: 健康度报告 schema 版本（字段增删必须同步升版本 + 更新测试）
HEALTH_SCHEMA_VERSION: Final[int] = 1

#: 硬失败（完全失败）状态：只有这些计入"连败"
_HARD_FAILURE_STATUSES: Final[frozenset[CollectorRunStatus]] = frozenset(
    {CollectorRunStatus.FAILED}
)
#: 部分失败 / 降级：不健康但不算硬失败（打断连败）
_PARTIAL_STATUSES: Final[frozenset[CollectorRunStatus]] = frozenset(
    {CollectorRunStatus.PARTIAL_FAILED, CollectorRunStatus.DEGRADED}
)
#: 成功终态
_SUCCESS_STATUSES: Final[frozenset[CollectorRunStatus]] = frozenset(
    {CollectorRunStatus.SUCCESS}
)
#: 在途（无终态）
_IN_FLIGHT_STATUSES: Final[frozenset[CollectorRunStatus]] = frozenset(
    {CollectorRunStatus.PENDING, CollectorRunStatus.RUNNING}
)

#: ``processed_items.status`` → 聚合桶（唯一映射，见模块 docstring）
_PROCESSED_BUCKETS: Final[dict[ProcessStatus, str]] = {
    ProcessStatus.SUCCESS: "success",
    ProcessStatus.SKIPPED: "rejected",
    ProcessStatus.FAILED: "failed",
    ProcessStatus.PENDING: "in_flight",
    ProcessStatus.PROCESSING: "in_flight",
    ProcessStatus.RETRYING: "in_flight",
}


class HealthState(StrEnum):
    """来源 / 整体健康状态（每个取值都必须"诚实"，不得用 UNKNOWN 冒充 HEALTHY）。"""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STALE = "STALE"
    NEVER_RUN = "NEVER_RUN"
    NEVER_SUCCEEDED = "NEVER_SUCCEEDED"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"
    NO_SOURCES = "NO_SOURCES"


class ProcessorState(StrEnum):
    """采集后处理（Processor）的观测状态。"""

    OBSERVED = "OBSERVED"
    NOT_OBSERVED = "NOT_OBSERVED"
    NO_INPUT = "NO_INPUT"


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    """把入参时间归一为 UTC；naive 时间直接拒绝（禁止隐式时区）。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def _from_db(value: datetime | None, *, field_name: str) -> datetime | None:
    """把库中时间归一为 UTC。

    SQLite（本地 / 测试库）不保存时区，读取时**按 UTC 解释**；生产 PostgreSQL 为
    ``TIMESTAMPTZ``，天然 timezone-aware，本函数为幂等归一（与 Scheduler 同一口径）。
    """
    if value is None:
        return None
    return to_utc(value, assume_tz=UTC, field_name=field_name)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class ProcessorCounts:
    """加工结果计数（口径见模块 docstring；``rejected`` 含复用与拒绝）。"""

    success: int = 0
    rejected: int = 0
    failed: int = 0
    in_flight: int = 0

    @property
    def total(self) -> int:
        return self.success + self.rejected + self.failed + self.in_flight

    def to_dict(self) -> dict[str, int]:
        return {
            "success": self.success,
            "rejected": self.rejected,
            "failed": self.failed,
            "in_flight": self.in_flight,
        }


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """单个 source 的只读健康事实（字段全部为白名单，不含 ``config_json``）。"""

    source_name: str
    source_id: str
    source_type: str
    enabled: bool
    collector: str | None
    base_url: str | None
    state: HealthState
    reason: str
    runs: int
    succeeded: int
    partial: int
    failed: int
    in_flight: int
    failure_streak: int
    stale: bool
    inserted_reported: int
    duplicate_reported: int
    raw_items_observed: int
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    processed: ProcessorCounts
    processor_state: ProcessorState

    @property
    def healthy(self) -> bool:
        """只有 ``HEALTHY`` 才是健康；DISABLED / UNKNOWN 都不是。"""
        return self.state is HealthState.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "enabled": self.enabled,
            "collector": self.collector,
            "base_url": self.base_url,
            "state": self.state.value,
            "healthy": self.healthy,
            "reason": self.reason,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "partial": self.partial,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "failure_streak": self.failure_streak,
            "stale": self.stale,
            "inserted_reported": self.inserted_reported,
            "duplicate_reported": self.duplicate_reported,
            "raw_items_observed": self.raw_items_observed,
            "last_run_at": _iso(self.last_run_at),
            "last_success_at": _iso(self.last_success_at),
            "last_error": self.last_error,
            "processed": self.processed.to_dict(),
            "processor_state": self.processor_state.value,
        }


@dataclass(frozen=True, slots=True)
class SchedulerSummary:
    """调度槽（``job_runs``）层面的只读汇总：用于区分"没调度"与"调度失败"。"""

    slots: int
    success: int
    partial_failed: int
    failed: int
    in_flight: int
    other: int
    retries: int
    per_source_entries: int
    slots_without_output: int
    last_slot_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slots": self.slots,
            "success": self.success,
            "partial_failed": self.partial_failed,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "other": self.other,
            "retries": self.retries,
            "per_source_entries": self.per_source_entries,
            "slots_without_output": self.slots_without_output,
            "last_slot_at": _iso(self.last_slot_at),
        }


@dataclass(frozen=True, slots=True)
class ProcessorSummary:
    """全局加工观测（含"未观测"这一**显式**不健康状态）。"""

    state: ProcessorState
    counts: ProcessorCounts
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "counts": self.counts.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次只读健康度观测的完整结果（``to_dict()`` 为稳定机器可读 schema）。"""

    schema_version: int
    as_of: datetime
    window_start: datetime
    window_hours: int
    stale_after_minutes: int
    state: HealthState
    healthy: bool
    reason: str
    sources: tuple[SourceHealth, ...]
    scheduler: SchedulerSummary
    processor: ProcessorSummary
    totals: dict[str, int]
    unattributed_runs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat(),
            "window": {
                "hours": self.window_hours,
                "start_at": self.window_start.isoformat(),
                "end_at": self.as_of.isoformat(),
                "stale_after_minutes": self.stale_after_minutes,
            },
            "state": self.state.value,
            "healthy": self.healthy,
            "reason": self.reason,
            "sources": [item.to_dict() for item in self.sources],
            "scheduler": self.scheduler.to_dict(),
            "processor": self.processor.to_dict(),
            "totals": dict(self.totals),
            "unattributed_runs": self.unattributed_runs,
        }


def consecutive_failures(statuses: Sequence[CollectorRunStatus]) -> int:
    """统计"从新到旧"开头的连续硬失败次数（成功 / 部分失败会打断连败）。

    Args:
        statuses: 运行状态序列，**必须按 ``started_at`` 由新到旧**排列。
    """
    streak = 0
    for status in statuses:
        if status not in _HARD_FAILURE_STATUSES:
            break
        streak += 1
    return streak


def classify_source_state(
    *,
    enabled: bool,
    runs: int,
    succeeded: int,
    partial: int,
    failed: int,
    in_flight: int,
    failure_streak: int,
    last_success_at: datetime | None,
    as_of: datetime,
    stale_after: timedelta,
) -> tuple[HealthState, str]:
    """把 source 级事实映射为**唯一确定**的状态与原因（判定顺序即优先级）。

    判定顺序（先匹配先生效）：
    1. 来源已禁用 → ``DISABLED``；
    2. 窗口内无运行且库内从未成功 → ``NEVER_RUN``；
    3. 连续硬失败 >= :data:`FAILED_STREAK_THRESHOLD` → ``FAILED``；
    4. 窗口内有硬失败且无成功终态 → ``FAILED``；
    5. 库内从未成功（例如只有部分失败 / 只有在途） → ``NEVER_SUCCEEDED``；
    6. 窗口内有失败 / 部分失败 → ``DEGRADED``；
    7. 最近成功超过陈旧阈值 → ``STALE``；
    8. 窗口内无运行记录 → ``UNKNOWN``（不得当作健康）；
    9. 窗口内有成功运行 → ``HEALTHY``；
    10. 兜底（只有在途运行等） → ``UNKNOWN``。
    """
    if not enabled:
        return HealthState.DISABLED, "来源已禁用：不产生采集健康证据"

    if runs == 0 and last_success_at is None:
        return HealthState.NEVER_RUN, "窗口内无运行记录，且库内从未成功"

    if failure_streak >= FAILED_STREAK_THRESHOLD:
        return (
            HealthState.FAILED,
            f"连续硬失败 {failure_streak} 次（阈值 {FAILED_STREAK_THRESHOLD}）",
        )

    if failed > 0 and succeeded == 0:
        return HealthState.FAILED, f"窗口内 {failed} 次运行硬失败且无成功终态"

    if last_success_at is None:
        return HealthState.NEVER_SUCCEEDED, "窗口内无成功终态，且库内从未成功"

    stale = (as_of - last_success_at) > stale_after
    if failed > 0 or partial > 0:
        detail = f"窗口内失败 {failed} 次 / 部分失败 {partial} 次"
        if stale:
            detail += "；最近成功已超过陈旧阈值"
        return HealthState.DEGRADED, detail

    if stale:
        minutes = int(stale_after.total_seconds() // 60)
        return HealthState.STALE, f"最近成功距今超过 {minutes} 分钟（陈旧阈值）"

    if runs == 0:
        return (
            HealthState.UNKNOWN,
            "窗口内无运行记录（最近成功发生在窗口之前），无法判定当前健康度",
        )

    if succeeded > 0:
        return HealthState.HEALTHY, "窗口内存在成功运行且未陈旧"

    return HealthState.UNKNOWN, "窗口内只有在途运行（PENDING/RUNNING），尚无终态证据"


def classify_processor_state(
    *, raw_items_observed: int, counts: ProcessorCounts
) -> tuple[ProcessorState, str]:
    """判定加工观测状态：**没有加工证据时绝不报 OBSERVED**。"""
    if counts.total > 0:
        return ProcessorState.OBSERVED, f"窗口内加工结果 {counts.total} 条"
    if raw_items_observed > 0:
        return (
            ProcessorState.NOT_OBSERVED,
            "窗口内有原始数据但没有任何加工结果（Processor 未启用或未运行）",
        )
    return ProcessorState.NO_INPUT, "窗口内没有原始数据，无加工输入"


def build_source_health(
    *,
    source_name: str,
    source_id: str,
    source_type: str,
    enabled: bool,
    collector: str | None,
    base_url: str | None,
    statuses: Sequence[CollectorRunStatus],
    last_success_at: datetime | None,
    inserted_reported: int,
    duplicate_reported: int,
    raw_items_observed: int,
    processed: ProcessorCounts,
    last_run_at: datetime | None,
    last_error: str | None,
    as_of: datetime,
    stale_after: timedelta,
) -> SourceHealth:
    """纯函数：由事实构造 :class:`SourceHealth`（在此完成**唯一一次**脱敏）。

    Args:
        statuses: 窗口内该 source 的运行状态，**按 ``started_at`` 升序**传入。
        last_error: 最近一次错误的原始文本（内部过 ``safe_text``：擦除凭据并截断）。
        base_url: 来源基址（内部过 ``safe_url``：去掉 userinfo / query / fragment）。
    """
    counters = Counter(statuses)
    succeeded = sum(counters[status] for status in _SUCCESS_STATUSES)
    partial = sum(counters[status] for status in _PARTIAL_STATUSES)
    failed = sum(counters[status] for status in _HARD_FAILURE_STATUSES)
    in_flight = sum(counters[status] for status in _IN_FLIGHT_STATUSES)
    streak = consecutive_failures(list(reversed(statuses)))

    state, reason = classify_source_state(
        enabled=enabled,
        runs=len(statuses),
        succeeded=succeeded,
        partial=partial,
        failed=failed,
        in_flight=in_flight,
        failure_streak=streak,
        last_success_at=last_success_at,
        as_of=as_of,
        stale_after=stale_after,
    )
    processor_state, _processor_reason = classify_processor_state(
        raw_items_observed=raw_items_observed, counts=processed
    )
    return SourceHealth(
        source_name=source_name,
        source_id=source_id,
        source_type=source_type,
        enabled=enabled,
        collector=collector,
        base_url=safe_url(base_url) if base_url else None,
        state=state,
        reason=reason,
        runs=len(statuses),
        succeeded=succeeded,
        partial=partial,
        failed=failed,
        in_flight=in_flight,
        failure_streak=streak,
        stale=last_success_at is not None and (as_of - last_success_at) > stale_after,
        inserted_reported=inserted_reported,
        duplicate_reported=duplicate_reported,
        raw_items_observed=raw_items_observed,
        last_run_at=last_run_at,
        last_success_at=last_success_at,
        last_error=safe_text(last_error) if last_error else None,
        processed=processed,
        processor_state=processor_state,
    )


def _classify_overall(
    sources: Sequence[SourceHealth], processor: ProcessorSummary
) -> tuple[HealthState, str]:
    """整体状态：**任一启用来源非健康、或加工未观测，整体就不是 HEALTHY**。"""
    if not sources:
        return HealthState.NO_SOURCES, "库内没有可观测来源（sources 为空或全部软删除）"
    enabled = [item for item in sources if item.enabled]
    if not enabled:
        return HealthState.DISABLED, "全部来源已禁用：没有采集健康证据"

    failed = [item for item in enabled if item.state is HealthState.FAILED]
    if failed:
        names = "、".join(item.source_name for item in failed[:5])
        return HealthState.FAILED, f"{len(failed)} 个启用来源硬失败：{names}"

    non_healthy = [item for item in enabled if not item.healthy]
    reasons: list[str] = []
    if non_healthy:
        summary = "、".join(f"{item.source_name}={item.state.value}" for item in non_healthy[:5])
        reasons.append(f"{len(non_healthy)} 个启用来源非健康：{summary}")
    not_observed = [
        item for item in enabled if item.processor_state is ProcessorState.NOT_OBSERVED
    ]
    if processor.state is ProcessorState.NOT_OBSERVED:
        reasons.append(processor.reason)
    elif not_observed:
        names = "、".join(item.source_name for item in not_observed[:5])
        reasons.append(f"{len(not_observed)} 个来源有原始数据但无加工结果：{names}")
    if processor.counts.failed > 0:
        reasons.append(f"窗口内加工失败 {processor.counts.failed} 条")
    if reasons:
        return HealthState.DEGRADED, "；".join(reasons)
    return HealthState.HEALTHY, "全部启用来源窗口内成功且未陈旧，加工结果可见"


def _summarize_scheduler(job_rows: Sequence[JobRun]) -> SchedulerSummary:
    """汇总窗口内的调度槽事实（``output_json`` 不可解析时显式计数，不猜健康度）。"""
    counters: Counter[JobStatus] = Counter()
    retries = 0
    entries = 0
    slots_without_output = 0
    for job in job_rows:
        counters[job.status] += 1
        retries += int(job.retry_count or 0)
        output = job.output_json if isinstance(job.output_json, dict) else None
        reported = output.get("sources") if output is not None else None
        if isinstance(reported, list):
            entries += len(reported)
        else:
            slots_without_output += 1
    last_slot = max((job.scheduled_at for job in job_rows), default=None)
    return SchedulerSummary(
        slots=len(job_rows),
        success=counters[JobStatus.SUCCESS],
        partial_failed=counters[JobStatus.PARTIAL_FAILED],
        failed=counters[JobStatus.FAILED],
        in_flight=(
            counters[JobStatus.PENDING]
            + counters[JobStatus.RUNNING]
            + counters[JobStatus.RETRYING]
        ),
        other=counters[JobStatus.CANCELLED],
        retries=retries,
        per_source_entries=entries,
        slots_without_output=slots_without_output,
        last_slot_at=_from_db(last_slot, field_name="job_runs.scheduled_at"),
    )


def build_health_report(
    *,
    as_of: datetime,
    window_hours: int,
    stale_after: timedelta,
    sources: Sequence[SourceHealth],
    scheduler: SchedulerSummary,
    processor: ProcessorSummary,
    unattributed_runs: int,
) -> HealthReport:
    """纯函数：把来源级 / 调度级 / 加工级事实组合成 :class:`HealthReport`。"""
    state, reason = _classify_overall(sources, processor)
    enabled = [item for item in sources if item.enabled]
    processed = processor.counts
    totals = {
        "sources": len(sources),
        "sources_enabled": len(enabled),
        "sources_healthy": sum(1 for item in enabled if item.healthy),
        "sources_non_healthy": sum(1 for item in enabled if not item.healthy),
        "runs": sum(item.runs for item in sources),
        "succeeded": sum(item.succeeded for item in sources),
        "partial": sum(item.partial for item in sources),
        "failed": sum(item.failed for item in sources),
        "in_flight": sum(item.in_flight for item in sources),
        "inserted_reported": sum(item.inserted_reported for item in sources),
        "duplicate_reported": sum(item.duplicate_reported for item in sources),
        "raw_items_observed": sum(item.raw_items_observed for item in sources),
        "processed_success": processed.success,
        "processed_rejected": processed.rejected,
        "processed_failed": processed.failed,
        "processed_in_flight": processed.in_flight,
        "unattributed_runs": unattributed_runs,
    }
    return HealthReport(
        schema_version=HEALTH_SCHEMA_VERSION,
        as_of=as_of,
        window_start=as_of - timedelta(hours=window_hours),
        window_hours=window_hours,
        stale_after_minutes=int(stale_after.total_seconds() // 60),
        state=state,
        healthy=state is HealthState.HEALTHY,
        reason=reason,
        sources=tuple(sources),
        scheduler=scheduler,
        processor=processor,
        totals=totals,
        unattributed_runs=unattributed_runs,
    )


def _add_bucket(counts: ProcessorCounts, bucket: str, value: int) -> ProcessorCounts:
    """把一条 ``(status, count)`` 累加到对应桶（显式分支，保持类型安全）。"""
    success = counts.success
    rejected = counts.rejected
    failed = counts.failed
    in_flight = counts.in_flight
    if bucket == "success":
        success += value
    elif bucket == "rejected":
        rejected += value
    elif bucket == "failed":
        failed += value
    else:
        in_flight += value
    return ProcessorCounts(success=success, rejected=rejected, failed=failed, in_flight=in_flight)


def _latest_success_at(session: Session, source_id: uuid.UUID) -> datetime | None:
    """该 source 在库内**最近一次成功**的时间（不限窗口；用 ORM 类型化列取时间）。"""
    run = session.scalars(
        sa.select(CollectorRun)
        .where(
            CollectorRun.source_id == source_id,
            CollectorRun.status == CollectorRunStatus.SUCCESS,
        )
        .order_by(CollectorRun.started_at.desc())
        .limit(1)
    ).first()
    if run is None:
        return None
    return _from_db(run.finished_at or run.started_at, field_name="collector_runs.finished_at")


def load_health_report(
    session: Session,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    as_of: datetime | None = None,
    stale_after: timedelta | None = None,
) -> HealthReport:
    """从持久化事实读取窗口内的采集健康度（**严格只读**，只执行 SELECT）。

    Args:
        session: 数据库会话（只读使用；本函数不调用 ``flush`` / ``commit``）。
        window_hours: 观测窗口（小时，必须为正）。
        as_of: 审计时点（必须带时区）；缺省 = 当前 UTC 时间。
        stale_after: 陈旧阈值；缺省 = :data:`DEFAULT_STALE_AFTER`（复用 Scheduler 口径）。

    Raises:
        ValueError: ``window_hours <= 0`` / ``stale_after <= 0`` / ``as_of`` 无时区。
    """
    if window_hours <= 0:
        raise ValueError(f"window_hours 必须为正数，得到 {window_hours}")
    threshold = stale_after if stale_after is not None else DEFAULT_STALE_AFTER
    if threshold <= timedelta(0):
        raise ValueError(f"stale_after 必须为正数，得到 {threshold}")
    moment = _require_aware(as_of, field_name="as_of") if as_of is not None else utc_now()
    window_start = moment - timedelta(hours=window_hours)

    sources = session.scalars(
        sa.select(Source).where(Source.deleted_at.is_(None)).order_by(Source.name)
    ).all()
    runs = session.scalars(
        sa.select(CollectorRun)
        .where(CollectorRun.started_at >= window_start, CollectorRun.started_at <= moment)
        .order_by(CollectorRun.started_at)
    ).all()
    raw_counts: dict[uuid.UUID, int] = {
        source_id: int(count)
        for source_id, count in session.execute(
            sa.select(RawItem.source_id, sa.func.count())
            .where(RawItem.collected_at >= window_start, RawItem.collected_at <= moment)
            .group_by(RawItem.source_id)
        ).all()
    }
    processed_rows = session.execute(
        sa.select(RawItem.source_id, ProcessedItem.status, sa.func.count())
        .join(RawItem, ProcessedItem.raw_item_id == RawItem.id)
        .where(ProcessedItem.created_at >= window_start, ProcessedItem.created_at <= moment)
        .group_by(RawItem.source_id, ProcessedItem.status)
    ).all()
    job_rows = session.scalars(
        sa.select(JobRun)
        .where(
            JobRun.job_type == JOB_TYPE,
            JobRun.scheduled_at >= window_start,
            JobRun.scheduled_at <= moment,
        )
        .order_by(JobRun.scheduled_at)
    ).all()

    runs_by_source: dict[uuid.UUID | None, list[CollectorRun]] = {}
    for run in runs:
        runs_by_source.setdefault(run.source_id, []).append(run)

    processed_by_source: dict[uuid.UUID, ProcessorCounts] = {}
    bucket_totals: Counter[str] = Counter()
    for source_id, status, count in processed_rows:
        bucket = _PROCESSED_BUCKETS.get(status, "in_flight")
        processed_by_source[source_id] = _add_bucket(
            processed_by_source.get(source_id, ProcessorCounts()), bucket, int(count)
        )
        bucket_totals[bucket] += int(count)

    source_rows: list[SourceHealth] = []
    for source in sources:
        source_runs = runs_by_source.get(source.id, [])
        source_rows.append(
            build_source_health(
                source_name=source.name,
                source_id=str(source.id),
                source_type=str(source.source_type),
                enabled=bool(source.enabled),
                collector=collector_name_for(source),
                base_url=source.base_url,
                statuses=[run.status for run in source_runs],
                last_success_at=_latest_success_at(session, source.id),
                inserted_reported=sum(int(run.inserted_count) for run in source_runs),
                duplicate_reported=sum(int(run.duplicate_count) for run in source_runs),
                raw_items_observed=raw_counts.get(source.id, 0),
                processed=processed_by_source.get(source.id, ProcessorCounts()),
                last_run_at=(
                    _from_db(source_runs[-1].started_at, field_name="collector_runs.started_at")
                    if source_runs
                    else None
                ),
                last_error=next(
                    (run.error_message for run in reversed(source_runs) if run.error_message),
                    None,
                ),
                as_of=moment,
                stale_after=threshold,
            )
        )

    processor_counts = ProcessorCounts(
        success=bucket_totals["success"],
        rejected=bucket_totals["rejected"],
        failed=bucket_totals["failed"],
        in_flight=bucket_totals["in_flight"],
    )
    processor_state, processor_reason = classify_processor_state(
        raw_items_observed=sum(raw_counts.values()), counts=processor_counts
    )
    return build_health_report(
        as_of=moment,
        window_hours=window_hours,
        stale_after=threshold,
        sources=source_rows,
        scheduler=_summarize_scheduler(job_rows),
        processor=ProcessorSummary(
            state=processor_state, counts=processor_counts, reason=processor_reason
        ),
        unattributed_runs=len(runs_by_source.get(None, [])),
    )


def _format_time(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "—"


def render_health_report(report: HealthReport) -> str:
    """把健康度报告渲染为人类可读 Markdown（**只读副本**，不含凭据）。"""
    empty_row = (
        "| — | — | NO_SOURCES | 库内没有可观测来源 | 0 | 0 | 0 | 0 | 0 | 0 | — | — | — | — |"
    )
    lines: list[str] = [
        "## 1. 采集运行健康度（只读）",
        "",
        "> 事实来源：`collector_runs`（运行）/ `raw_items`（原始）/ `processed_items`（加工）"
        "/ `job_runs`（调度槽）/ `sources`（启用状态派生值）。本报告不写库、不改历史事实。",
        "",
        f"- 审计时点（UTC）：{report.as_of.isoformat()}",
        f"- 观测窗口：{report.window_hours} 小时（起点 {report.window_start.isoformat()}）",
        f"- 陈旧阈值：{report.stale_after_minutes} 分钟（= 3 倍 30 分钟调度间隔）",
        f"- **总体状态：{report.state.value}（healthy={str(report.healthy).lower()}）**"
        f"：{report.reason}",
        "",
        "| 计数 | 值 |",
        "|---|---:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in report.totals.items()]
    lines += [
        "",
        f"未归因采集运行（`collector_runs.source_id` 为空）：{report.unattributed_runs} 次。",
        "",
        "### 来源健康度",
        "",
        "| 来源 | 启用 | 状态 | 原因 | 运行 | 成功 | 部分 | 失败 | 在途 | 连败 | 最近成功 | "
        "原始(新/去重) | 加工(成功/拒绝/失败) | 加工状态 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    if not report.sources:
        lines.append(empty_row)
    for item in report.sources:
        lines.append(
            f"| {item.source_name} | {str(item.enabled).lower()} | {item.state.value} | "
            f"{item.reason} | {item.runs} | {item.succeeded} | {item.partial} | {item.failed} | "
            f"{item.in_flight} | {item.failure_streak} | {_format_time(item.last_success_at)} | "
            f"{item.inserted_reported}/{item.duplicate_reported} | "
            f"{item.processed.success}/{item.processed.rejected}/{item.processed.failed} | "
            f"{item.processor_state.value} |"
        )
    lines += [
        "",
        "### 调度槽",
        "",
        f"- 窗口内槽数：{report.scheduler.slots}"
        f"（SUCCESS={report.scheduler.success}、PARTIAL_FAILED={report.scheduler.partial_failed}、"
        f"FAILED={report.scheduler.failed}、在途={report.scheduler.in_flight}、"
        f"其它={report.scheduler.other}）",
        f"- 重试累计：{report.scheduler.retries}；逐来源摘要条目："
        f"{report.scheduler.per_source_entries}",
        f"- `output_json` 缺失或不可解析的槽：{report.scheduler.slots_without_output}"
        "（这些槽**不提供**来源级证据）",
        f"- 最近槽时间：{_format_time(report.scheduler.last_slot_at)}",
        "",
        "### 采集后处理（Processor）",
        "",
        f"- 状态：{report.processor.state.value}（{report.processor.reason}）",
        f"- 加工计数：成功 {report.processor.counts.success}、拒绝/复用 "
        f"{report.processor.counts.rejected}、失败 {report.processor.counts.failed}、"
        f"在途 {report.processor.counts.in_flight}",
        "- 口径：`processed_items.status` 中 `SKIPPED` 同时涵盖 `DUPLICATE` 与 "
        "`REJECTED`（都不产出新事实）；`NOT_OBSERVED` 明确表示“有原始数据但没有加工结果”，"
        "**不是**健康。",
        "",
    ]
    return "\n".join(lines)
