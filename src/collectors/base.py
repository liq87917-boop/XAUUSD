"""采集器基类：站点无关的高层编排 + 站点相关的抽象钩子。

本文件的核心约束：
1. ``collect()`` 的抽象层次必须高 —— 只做"与站点无关"的编排：
   读游标 → 翻页（``_do_fetch``）→ ``content_hash`` 幂等落库 → 去重统计 →
   生成新游标 / 状态 → 返回 :class:`CollectOutcome`；
2. **本文件不得出现任何具体站点的 URL、接口路径或解析规则**：微博、新闻、行情、
   宏观采集器只需继承本类并实现 ``_do_fetch()``（Step 3 落地）；
3. 子类在 ``_do_fetch()`` 内通过 ``self._request(HttpRequest(...))`` 发请求，
   即自动获得 3 次重试、指数退避、超时与重试计数（``self.retry_count``）。

红线：
- 原始数据只追加不覆盖（``database/protection.py`` 在 flush 期强制）；
- 幂等：同一 ``source_record_id`` 或同一 ``content_hash`` 在**同一数据源内**只落库一次；
- 时间语义：``effective_at = max(published_at, collected_at)``，禁止 naive datetime。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import RawItem, RawMedia, Source
from database.models.enums import CollectorRunStatus, SourceType
from src.collectors.errors import CollectorError, FetchAttempt
from src.collectors.transport import (
    HttpRequest,
    HttpResponse,
    RetryPolicy,
    SleepFn,
    Transport,
    send_with_retry,
)
from src.collectors.types import (
    CollectorHealth,
    CollectOutcome,
    CollectWindow,
    FetchPage,
    RawItemPayload,
)
from src.common.redaction import safe_text
from src.common.time import utc_now
from src.processors.collection.contracts import (
    PersistedItemProcessor,
    ProcessedRecord,
    RecordOutcome,
)

__all__ = ["PERSIST_DUPLICATE", "PERSIST_INSERTED", "BaseCollector"]

_log = get_logger("collectors.base")

ClockFn = Callable[[], datetime]

#: 落库结果标识：供子类覆盖 ``_persist_payload`` 时复用（避免魔法字符串散落各处）
PERSIST_INSERTED = "inserted"
PERSIST_DUPLICATE = "duplicate"

_INSERTED = PERSIST_INSERTED
_DUPLICATE = PERSIST_DUPLICATE

#: 采集器侧保留的 Processor 告警上限（防止异常刷屏把 collector_runs.warnings_json 撑爆）
_MAX_PROCESSING_WARNINGS = 20


class BaseCollector(ABC):
    """所有采集器的基类。

    子类必须声明 ``collector_name``（与 ``sources.config_json["collector"]`` 一致）
    与 ``source_type``，并实现 ``_do_fetch()``。
    """

    #: 采集器名称（注册表键；必须与数据源配置一致）
    collector_name: ClassVar[str] = ""
    #: 该采集器适用的来源类型
    source_type: ClassVar[SourceType]
    #: 单轮采集最多翻页数（防止无限翻页；达到上限按 PARTIAL_FAILED 处理并保留游标）
    max_pages_per_run: ClassVar[int] = 50
    #: 单轮最少记录数（0 = 不检查）。低于该值时打印 WARNING 级数据质量告警。
    min_records_per_run: ClassVar[int] = 0
    #: 默认重试策略（3 次尝试 + 指数退避）
    default_retry_policy: ClassVar[RetryPolicy] = RetryPolicy()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls):
            return
        if not getattr(cls, "collector_name", ""):
            raise TypeError(f"{cls.__name__} 必须声明 collector_name")
        if not hasattr(cls, "source_type"):
            raise TypeError(f"{cls.__name__} 必须声明 source_type")
        if getattr(cls, "max_pages_per_run", 0) < 1:
            raise TypeError(f"{cls.__name__}.max_pages_per_run 必须 >= 1")

    def __init__(
        self,
        source: Source,
        *,
        transport: Transport | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: ClockFn = utc_now,
        sleep: SleepFn | None = None,
        post_processor: PersistedItemProcessor | None = None,
    ) -> None:
        """初始化采集器。

        Args:
            source: 对应的 ``sources`` 记录（采集器只从它读取配置，不硬编码站点信息）。
            transport: 传输实现。**生产**传 :class:`AiohttpTransport`；
                **测试**传 MockTransport（保证测试不访问外部网络）。
            retry_policy: 重试策略，默认 3 次尝试 + 指数退避。
            clock: 时间源（测试可注入固定时钟，保证断言稳定）。
            sleep: 退避休眠函数（测试注入假实现，避免真正等待）。
            post_processor: 可选**采集后处理钩子**（TD-11）：每条原始记录落库后调用一次，
                只接收 ``(session, raw_item)``，由 Processor 负责 normalize / dedup /
                effective_at 并写 ``processed_items``。**默认 None（零行为变化）**；
                采集器自身不实现 Processor 业务逻辑，分层边界不被破坏。
        """
        self.source = source
        self.retry_policy = retry_policy or self.default_retry_policy
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._attempts: list[FetchAttempt] = []
        self._warnings: tuple[str, ...] = ()
        self._post_processor = post_processor
        self._processing_counts: dict[str, int] = {}
        self._processing_warnings: list[str] = []
        #: 本轮实际使用的传输通道（library / rest / cache），供统一结果对象与监控使用
        self.transport_used: str | None = None
        #: 本轮跳过的记录数（数据质量校验拒绝，未入库）
        self.skipped_count: int = 0

        # 每轮最少记录数：来自 sources.config_json['min_records_per_run']（团队批复）
        # 类属性 min_records_per_run 作为缺省值；0 表示"不检查"。
        declared_min = (source.config_json or {}).get("min_records_per_run")
        self._min_records: int = (
            self.min_records_per_run if declared_min is None else int(declared_min)
        )


    # ------------------------------------------------------------------
    # 站点相关：由具体采集器实现
    # ------------------------------------------------------------------
    @abstractmethod
    async def _do_fetch(
        self, cursor: dict[str, Any] | None, window: CollectWindow
    ) -> FetchPage:
        """抓取"下一页"数据（**站点相关，子类实现**）。

        Args:
            cursor: 上次中断时保存的游标（``None`` 表示从头开始）；实现方需自行
                从游标中恢复分页位置，保证断点续采。
            window: 采集时间窗口。

        Returns:
            :class:`FetchPage`；``next_cursor=None`` 表示已到末尾（本轮完成）。
        """

    async def _do_health_check(self) -> CollectorHealth:
        """默认健康检查：只校验配置完整性，不发网络请求。

        具体采集器可覆盖为轻量探测请求（例如 GET 一次首页判断是否可用）。
        """
        base_url = (self.source.base_url or "").strip()
        healthy = bool(base_url)
        return CollectorHealth(
            collector_name=self.collector_name,
            healthy=healthy,
            checked_at=self._clock(),
            message=None if healthy else "数据源未配置 base_url（sources.base_url 为空）",
            details={
                "base_url": base_url,
                "enabled": self.source.enabled,
                "source_name": self.source.name,
            },
        )

    # ------------------------------------------------------------------
    # 通用能力：请求（含重试）、健康检查、重试计数
    # ------------------------------------------------------------------
    async def _request(self, request: HttpRequest) -> HttpResponse:
        """发送请求：自动应用重试策略并累计重试计数。

        Raises:
            CollectorFetchError: 重试耗尽；
            CollectorError: 未注入 Transport。
        """
        return await send_with_retry(
            self._transport_required(),
            request,
            self.retry_policy,
            sleep=self._sleep,
            on_attempt=self._attempts.append,
        )

    def _transport_required(self) -> Transport:
        if self._transport is None:
            raise CollectorError(
                "未注入 Transport：生产环境请使用 AiohttpTransport，"
                "测试请注入 MockTransport（采集器测试不得访问外部网络）"
            )
        return self._transport

    @property
    def retry_count(self) -> int:
        """本轮已触发的重试次数（失败尝试次数）。"""
        return sum(1 for attempt in self._attempts if not attempt.succeeded)

    @property
    def attempts(self) -> tuple[FetchAttempt, ...]:
        """本轮所有尝试记录（排障用）。"""
        return tuple(self._attempts)

    @property
    def last_warnings(self) -> tuple[str, ...]:
        """最近一轮采集产生的数据质量告警（不落库，供调用方 / 测试 / Dashboard 观察）。"""
        return self._warnings

    def _read_local_text(self, path: str | Path, *, kind: str) -> str:
        """读取本地文件（CSV 兜底模式共用）：缺失/不可读时给出可执行错误。

        Args:
            path: 文件路径。
            kind: 文件用途描述（用于错误信息，如 "新闻 CSV" / "宏观 CSV"）。
        """
        target = Path(path)
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CollectorError(
                f"{kind} 文件不存在：{target}", details={"path": str(target)}
            ) from exc
        except OSError as exc:
            raise CollectorError(
                f"{kind} 文件读取失败：{target}（{exc}）", details={"path": str(target)}
            ) from exc

    def _expected_min_records(self, window: CollectWindow) -> int:
        """本轮期望的最少记录数。

        默认取 ``sources.config_json['min_records_per_run']``（缺省用类属性
        ``min_records_per_run``，0 = 不检查）；子类可按业务规则覆盖
        （例如行情采集器：窗口 60 分钟 ÷ 1m 周期 → 期望 60 根）。
        """
        return self._min_records

    def _reset_run_state(self) -> None:
        """每轮采集开始前清空运行期状态（子类可覆盖以清理自身缓存与告警）。"""
        self._attempts.clear()
        self._warnings = ()
        self.transport_used = None
        self.skipped_count = 0
        self._processing_counts.clear()
        self._processing_warnings.clear()

    def _evaluate_run_warnings(
        self, window: CollectWindow, outcome: CollectOutcome
    ) -> tuple[str, ...]:
        """数据质量告警（在 ``collector_runs`` 状态之外补充的可观测信息）。

        通用规则：本轮实际获得记录数（inserted + duplicate）低于期望下限时不静默——
        既打印 WARNING 日志，也通过 :attr:`last_warnings` 暴露给调用方。
        另外附带 Processor 后处理告警（若注入了 ``post_processor``）。
        """
        expected = self._expected_min_records(window)
        obtained = outcome.inserted_count + outcome.duplicate_count
        if expected > 0 and obtained < expected:
            return (
                f"本轮仅获得 {obtained} 条记录，低于预期下限 {expected} 条"
                f"（窗口 {window.start_utc.isoformat()} ~ {window.end_utc.isoformat()}）",
                *self._processing_warnings,
            )
        return tuple(self._processing_warnings)

    async def health_check(self) -> CollectorHealth:
        """对外健康检查：任何异常都转成"不健康"结果，不向调用方抛出。"""
        try:
            return await self._do_health_check()
        except Exception as exc:
            return CollectorHealth(
                collector_name=self.collector_name,
                healthy=False,
                checked_at=self._clock(),
                message=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # 扩展点（默认空实现；具体采集器可覆盖）
    # ------------------------------------------------------------------
    def _after_persist(self, session: Session, raw_item: RawItem, payload: RawItemPayload) -> None:  # noqa: B027
        """单条原始记录落库后的钩子（例如微博采集器写入 ``author_posts``）。

        这是**可选**扩展点：默认什么都不做，子类按需覆盖。
        """

    def _after_run(self, session: Session, outcome: CollectOutcome) -> None:  # noqa: B027
        """一轮采集结束后的钩子（例如更新 ``author_accounts.last_collected_at``）。

        这是**可选**扩展点：默认什么都不做，子类按需覆盖。
        """

    # ------------------------------------------------------------------
    # 采集后处理（TD-11：Processor 接线点）
    # ------------------------------------------------------------------
    @property
    def post_processor(self) -> PersistedItemProcessor | None:
        """注入的采集后处理钩子（``None`` 表示未接线，行为与本能力引入前一致）。"""
        return self._post_processor

    def processing_summary(self) -> dict[str, Any] | None:
        """Processor 后处理的可审计摘要（未接线时返回 ``None``）。

        只含白名单计数与告警（已脱敏），**不含** token / API key / 完整 source config，
        可直接写入 ``job_runs.output_json`` / CLI 统计。
        """
        if self._post_processor is None:
            return None
        counts = self._processing_counts
        return {
            "processor": self._post_processor.processor_name,
            "processor_version": self._post_processor.processor_version,
            "processed": counts.get(RecordOutcome.SUCCESS.value, 0),
            "duplicate": counts.get(RecordOutcome.DUPLICATE.value, 0),
            "rejected": counts.get(RecordOutcome.REJECTED.value, 0),
            "failed": counts.get(RecordOutcome.FAILED.value, 0),
            "warnings": list(self._processing_warnings),
        }

    def _post_process(self, session: Session, raw_item: RawItem) -> None:
        """把刚落库的原始记录交给 Processor（可选钩子；失败不得拖垮采集）。"""
        processor = self._post_processor
        if processor is None:
            return
        try:
            record: ProcessedRecord = processor.process_persisted(session, raw_item)
        except Exception as exc:  # noqa: BLE001 - 后处理异常必须可观测，但不得阻断采集
            warning = f"Processor 后处理异常：{type(exc).__name__}: {exc}"
            self._record_processing_warning(warning)
            _log.warning("%s | %s", self.collector_name, warning)
            return

        outcome = record.outcome.value
        self._processing_counts[outcome] = self._processing_counts.get(outcome, 0) + 1
        for warning in record.warnings:
            self._record_processing_warning(warning)

    def _record_processing_warning(self, text: str) -> None:
        """记录 Processor 告警（脱敏 + 截断 + 上限，绝不写入凭据）。"""
        if len(self._processing_warnings) >= _MAX_PROCESSING_WARNINGS:
            return
        redacted = safe_text(str(text), max_chars=300)
        if redacted:
            self._processing_warnings.append(redacted)


    # ------------------------------------------------------------------
    # 高层编排：与站点完全无关
    # ------------------------------------------------------------------
    async def collect(
        self,
        *,
        session: Session,
        window: CollectWindow,
        cursor: Mapping[str, Any] | None = None,
    ) -> CollectOutcome:
        """执行一轮采集（站点无关的高层编排；子类不应覆盖）。

        流程：``_do_fetch`` 翻页 → 逐条幂等落库 → 统计 → 保存新游标 → 判定状态。

        Args:
            session: 写入 ``raw_items`` / ``raw_media`` 的事务 Session（提交由调用方决定）。
            window: 采集时间窗口。
            cursor: 续采游标（来自上一次 ``collector_runs.cursor_json``）。

        Returns:
            :class:`CollectOutcome`，字段可直接写入 ``collector_runs``（04 §7）。
        """
        started_at = self._clock()
        self._reset_run_state()

        resume_point: dict[str, Any] | None = dict(cursor) if cursor else None
        state: dict[str, Any] | None = resume_point

        fetched = inserted = duplicate = failed = pages = 0
        error_message: str | None = None
        error_type: str | None = None
        finished_naturally = False

        while pages < self.max_pages_per_run:
            try:
                page = await self._do_fetch(state, window)
            except CollectorError as exc:
                error_message = str(exc)
                error_type = type(exc).__name__
                break
            except Exception as exc:
                # 明确记录后按失败处理，绝不静默吞掉（06_Cline开发规则 第 24 条）
                error_message = f"采集器内部错误：{type(exc).__name__}: {exc}"
                error_type = type(exc).__name__
                break

            pages += 1
            fetched += page.fetched

            for payload in page.payloads:
                try:
                    persisted = self._persist_payload(session, payload)
                except Exception as exc:
                    failed += 1
                    error_message = (
                        f"记录 {payload.source_record_id} 入库失败：{type(exc).__name__}: {exc}"
                    )
                    continue
                if persisted == _INSERTED:
                    inserted += 1
                elif persisted == _DUPLICATE:
                    duplicate += 1

            state = page.next_cursor
            if state is None:
                finished_naturally = True
                break

        if not finished_naturally and error_message is None:
            error_message = (
                f"达到单轮最大页数限制（max_pages_per_run={self.max_pages_per_run}），"
                "已保存游标供下轮继续"
            )

        outcome = CollectOutcome(
            collector_name=self.collector_name,
            source_id=self.source.id,
            status=self._resolve_status(
                error_message=error_message,
                processed=inserted + duplicate + failed,
                pages=pages,
            ),
            started_at=started_at,
            finished_at=self._clock(),
            fetched_count=fetched,
            inserted_count=inserted,
            duplicate_count=duplicate,
            failed_count=failed,
            pages_fetched=pages,
            retry_count=self.retry_count,
            cursor=state,
            resumed_from_cursor=resume_point is not None,
            error_message=error_message,
            skipped_count=self.skipped_count,
            transport=self.transport_used,
            error_type=error_type,
        )

        # 数据质量告警：低于预期条数（例如行情缺了 1 分钟）必须留下 WARNING 日志
        self._warnings = self._evaluate_run_warnings(window, outcome)
        for warning in self._warnings:
            _log.warning("%s | %s", self.collector_name, warning)

        self._after_run(session, outcome)
        return outcome

    def _resolve_status(
        self, *, error_message: str | None, processed: int, pages: int
    ) -> CollectorRunStatus:
        """状态判定：SUCCESS / DEGRADED / PARTIAL_FAILED / FAILED。"""
        if error_message is None:
            # fallback（如 REST）成功 → 降级而非完全成功
            if self.transport_used == "rest":
                return CollectorRunStatus.DEGRADED
            return CollectorRunStatus.SUCCESS
        if pages > 0 or processed > 0:
            return CollectorRunStatus.PARTIAL_FAILED
        return CollectorRunStatus.FAILED


    # ------------------------------------------------------------------
    # 幂等落库（原始数据只追加）
    # ------------------------------------------------------------------
    def _persist_payload(self, session: Session, payload: RawItemPayload) -> str:
        """写入一条原始记录，返回 ``"inserted"`` 或 ``"duplicate"``。

        幂等规则（Phase 1 验收门槛：无大规模重复）：
        1. 同数据源内 ``source_record_id`` 相同 → 重复（数据库唯一约束兜底）；
        2. 同数据源内 ``content_hash`` 相同 → 重复（应对同一内容被平台重新分配 ID、
           或采集窗口重叠导致的重复抓取）。
        去重只在**同一数据源**内进行：不同来源出现相同文本是两条独立记录。
        """
        digest = payload.resolved_content_hash()

        exists = session.scalar(
            sa.select(RawItem.id)
            .where(RawItem.source_id == self.source.id)
            .where(
                sa.or_(
                    RawItem.source_record_id == payload.source_record_id,
                    RawItem.content_hash == digest,
                )
            )
            .limit(1)
        )
        if exists is not None:
            return _DUPLICATE

        collected_at = self._clock()
        raw_item = RawItem(
            source_id=self.source.id,
            source_record_id=payload.source_record_id,
            item_type=payload.item_type,
            title=payload.title,
            content_text=payload.content_text,
            raw_json=dict(payload.raw_json),
            source_url=payload.source_url,
            content_hash=digest,
            published_at=payload.published_at,
            collected_at=collected_at,
            effective_at=payload.resolve_effective_at(collected_at),
        )
        session.add(raw_item)
        session.flush()  # 先拿到主键，才能写 raw_media

        self._persist_media(session, raw_item, payload)
        self._after_persist(session, raw_item, payload)
        # 采集后处理（可选）：Processor 负责 normalize / dedup / effective_at → processed_items。
        # 必须在 raw_item 落库之后（Processor 需要 raw_item.id 与数据库里的时间字段）。
        self._post_process(session, raw_item)
        return _INSERTED

    def _persist_media(self, session: Session, raw_item: RawItem, payload: RawItemPayload) -> None:
        """写入媒体元数据（同一 raw_item 内按 sha256 去重，04 §6）。

        去重必须覆盖"同一批次内的重复"：两条 payload 携带相同 sha256 时，
        数据库唯一约束会拒绝第二条，因此这里用 seen 集合在内存中先行拦截。
        """
        if not payload.media:
            return

        seen: set[str] = {
            digest
            for digest in session.scalars(
                sa.select(RawMedia.sha256).where(RawMedia.raw_item_id == raw_item.id)
            ).all()
            if digest is not None
        }
        for media in payload.media:
            if media.sha256 is not None:
                if media.sha256 in seen:
                    continue
                seen.add(media.sha256)
            session.add(
                RawMedia(
                    raw_item_id=raw_item.id,
                    media_type=media.media_type,
                    original_url=media.original_url,
                    storage_uri=media.storage_uri,
                    sha256=media.sha256,
                    width=media.width,
                    height=media.height,
                    mime_type=media.mime_type,
                    collected_at=self._clock(),
                )
            )
        session.flush()



