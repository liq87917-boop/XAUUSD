"""Scheduler → Collector → Processor 可选接线集成测试（GOLD-003）。

★ 全程不联网 ★（SQLite 文件库 + `tests/conftest.py` 的双层网络守卫；采集器只产出内存构造的
``RawItemPayload``，域名一律 ``.invalid``）。

验证 GOLD-003 acceptance：
1. 默认（不带 ``--with-processor``）→ 注入 ``post_processor=None``、零 ``processed_items``
   （与 GOLD-001-R2 行为一致）；
2. 显式开启 → 经真实 ``default_collector_factory`` / Scheduler / runner 路径，
   每条原始记录落库后立即加工（``raw_items → processed_items`` 闭环）；
3. 同一 30 分钟槽重复触发只执行一次；重复加工同一原始事实不产生重复行；
4. 单源 Processor 故障被隔离：其它源照常采集与加工、原始数据不丢、状态诚实；
5. Processor 故障信息中的 ``api_key=...`` 不进入日志、``job_runs.output_json``
   与 ``processed_items.structured_json``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import JobRun, ProcessedItem, RawItem, Source
from database.models.enums import JobStatus, ProcessStatus, RawItemType, SourceType
from database.session import session_scope
from scripts import run_collector_scheduler
from scripts.run_collector_scheduler import main
from src.collectors.base import BaseCollector
from src.collectors.registry import register_collector, unregister_collector
from src.collectors.types import CollectWindow, FetchPage, RawItemPayload
from src.processors.collection.contracts import ProcessorInput
from src.processors.collection.pipeline import CollectionProcessor

pytestmark = pytest.mark.integration

#: 调度时钟刻意取**历史时刻**：``CollectionProcessor`` 的"未来时间戳"容差判定用的是
#: 真实 ``utc_now()``，若把假槽放在未来（例如今天 12:00Z 而真实时间为上午），
#: 探针产出的 ``published_at`` 会被判为坏数据（REJECTED），测不到正常加工路径。
MOMENT = datetime(2026, 9, 1, 12, 7, 30, tzinfo=UTC)
SECRET_VALUE = "SECRETVALUE-9f1c"
PROBE_COLLECTOR = "scheduler_probe"


class _Clock:
    """固定时钟（槽边界可断言，不依赖真实时间）。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class _ProbeCollector(BaseCollector):
    """确定性产出 1 条 NEWS 原始记录的最小采集器（不联网）。

    ``executions`` 为**类级**调用记录：生产工厂内部会新建采集器实例，
    只有类级计数才能观察到"同一槽第二次触发是否真的没有再执行采集"。
    """

    collector_name = PROBE_COLLECTOR
    source_type: ClassVar[SourceType] = SourceType.NEWS
    executions: ClassVar[list[str]] = []

    async def _do_fetch(self, cursor: dict | None, window: CollectWindow) -> FetchPage:
        type(self).executions.append(self.source.name)
        record_id = f"{self.source.name}-rec-1"
        payload = RawItemPayload(
            source_record_id=record_id,
            item_type=RawItemType.NEWS,
            title="黄金 3400 上方继续看多",
            content_text="3400 上方减仓后继续持有。",
            source_url=f"https://feed.invalid/{self.source.name}.xml?api_key={SECRET_VALUE}",
            published_at=window.end_utc - timedelta(minutes=5),
            raw_json={
                "id": record_id,
                "feed_url": f"https://feed.invalid/{self.source.name}.xml?api_key={SECRET_VALUE}",
            },
        )
        return FetchPage(payloads=(payload,), next_cursor=None)


class _BrokenProcessor:
    """单源 Processor 故障替身：异常信息里带 ``api_key``（验证脱敏）。"""

    processor_name = "broken_processor"
    processor_version = "broken-v1"

    def process_persisted(self, session: Any, raw_item: Any) -> Any:
        raise RuntimeError(f"processor-boom api_key={SECRET_VALUE}")


@pytest.fixture(autouse=True)
def _reset_probe_executions() -> Iterator[None]:
    _ProbeCollector.executions.clear()
    try:
        yield
    finally:
        _ProbeCollector.executions.clear()


@pytest.fixture()
def registered_probe() -> Iterator[str]:
    """把探针采集器注册进生产注册表（测试结束必须注销，避免污染其它测试）。"""
    register_collector(_ProbeCollector)
    try:
        yield PROBE_COLLECTOR
    finally:
        unregister_collector(PROBE_COLLECTOR)


@contextmanager
def _capture_collector_logs() -> Iterator[list[str]]:
    """捕获 ``gold_ai.collectors.base`` 的日志消息。

    特意挂在**具体子 logger** 上：项目 logging 配置对 ``gold_ai`` 设了
    ``propagate: false``，只依赖 root/caplog 会捕不到记录。
    """
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("gold_ai.collectors.base")
    handler = _Capture(level=logging.DEBUG)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _seed_source(
    factory: sessionmaker[Session], name: str, *, collector: str = PROBE_COLLECTOR
) -> None:
    with session_scope(factory) as session:
        session.add(
            Source(
                name=name,
                source_type=SourceType.NEWS,
                enabled=True,
                config_json={"collector": collector, "min_records_per_run": 0},
            )
        )


def _rows(factory: sessionmaker[Session], model: Any) -> list[Any]:
    with factory() as session:
        return list(session.scalars(sa.select(model)))


def _job_runs(factory: sessionmaker[Session]) -> list[JobRun]:
    with factory() as session:
        return list(session.scalars(sa.select(JobRun).order_by(JobRun.scheduled_at)))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def test_default_scheduler_does_not_wire_processor(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认未开启 → 注入 ``None``、零 ``processed_items``（零行为差异）。"""
    _seed_source(session_factory, "wiring-default")
    injected: list[object] = []

    def spy(**kwargs: Any):
        injected.append(kwargs.get("post_processor"))
        return lambda source: _ProbeCollector(source)

    monkeypatch.setattr(run_collector_scheduler, "default_collector_factory", spy)

    code = main(["--once"], session_factory=session_factory, clock=_Clock(MOMENT))

    assert code == 0
    assert injected == [None]  # 未显式开启 ⇒ post_processor=None
    assert len(_rows(session_factory, RawItem)) == 1
    assert _rows(session_factory, ProcessedItem) == []


def test_with_processor_flag_injects_collection_processor(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--with-processor`` → CLI 把真实 ``CollectionProcessor`` 交给 bootstrap 工厂。"""
    _seed_source(session_factory, "wiring-flag")
    injected: list[object] = []

    def spy(**kwargs: Any):
        processor = kwargs.get("post_processor")
        injected.append(processor)
        return lambda source: _ProbeCollector(source, post_processor=processor)

    monkeypatch.setattr(run_collector_scheduler, "default_collector_factory", spy)

    code = main(
        ["--once", "--with-processor"], session_factory=session_factory, clock=_Clock(MOMENT)
    )

    assert code == 0
    assert len(injected) == 1
    processor = injected[0]
    assert isinstance(processor, CollectionProcessor)
    assert processor.processor_name == "collection_normalizer"
    rows = _rows(session_factory, ProcessedItem)
    assert len(rows) == 1
    assert rows[0].status is ProcessStatus.SUCCESS


def test_production_bootstrap_closes_raw_to_processed_loop(
    session_factory: sessionmaker[Session], registered_probe: str
) -> None:
    """生产路径（真实 bootstrap + Scheduler + runner + Processor）闭环。

    这是本任务的核心证据：``--with-processor`` 开启后 30 分钟采集能完成
    ``raw_items → processed_items``，且审计摘要与 ``job_runs.output_json`` 不含凭据。
    """
    _seed_source(session_factory, "wiring-loop", collector=registered_probe)

    code = main(
        ["--once", "--with-processor"], session_factory=session_factory, clock=_Clock(MOMENT)
    )

    assert code == 0
    assert _ProbeCollector.executions == ["wiring-loop"]

    raws = _rows(session_factory, RawItem)
    assert len(raws) == 1
    rows = _rows(session_factory, ProcessedItem)
    assert len(rows) == 1

    processed = rows[0]
    assert processed.raw_item_id == raws[0].id
    assert processed.processor_name == "collection_normalizer"
    assert processed.processor_version == "collection-normalizer-v1"
    assert processed.status is ProcessStatus.SUCCESS
    # effective_at 不得早于上游事实（防未来数据泄漏下界）
    assert _as_utc(processed.effective_at) >= _as_utc(raws[0].effective_at)

    structured = json.dumps(processed.structured_json, ensure_ascii=False)
    assert processed.structured_json["outcome"] == "SUCCESS"
    assert "feed.invalid/wiring-loop.xml" in structured  # URL 保留 host + path
    assert SECRET_VALUE not in structured  # query（含 api_key）必须被剥离

    jobs = _job_runs(session_factory)
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.SUCCESS
    output = json.dumps(jobs[0].output_json, ensure_ascii=False)
    assert SECRET_VALUE not in output
    assert jobs[0].output_json["sources"][0]["inserted"] == 1


def test_repeated_slot_and_reprocessing_are_idempotent(
    session_factory: sessionmaker[Session], registered_probe: str
) -> None:
    """同一槽重复触发只执行一次；重复加工同一原始事实不产生重复行。"""
    _seed_source(session_factory, "wiring-idem", collector=registered_probe)
    clock = _Clock(MOMENT)

    first = main(["--once", "--with-processor"], session_factory=session_factory, clock=clock)
    second = main(["--once", "--with-processor"], session_factory=session_factory, clock=clock)

    assert (first, second) == (0, 0)
    assert _ProbeCollector.executions == ["wiring-idem"]  # 命中幂等 ⇒ 不再执行采集
    assert len(_job_runs(session_factory)) == 1  # 同槽不产生第二条 JobRun
    raws = _rows(session_factory, RawItem)
    assert len(raws) == 1
    assert len(_rows(session_factory, ProcessedItem)) == 1

    with session_scope(session_factory) as session:
        raw = session.scalars(sa.select(RawItem)).one()
        processor = CollectionProcessor()
        replay = processor.process_one(ProcessorInput.from_raw_item(raw))
        assert processor.persist_records(session, (replay,)) == 0  # 重复加工：新增 0 行

    assert len(_rows(session_factory, ProcessedItem)) == 1


def test_single_source_processor_failure_is_isolated_and_redacted(
    session_factory: sessionmaker[Session],
) -> None:
    """单源 Processor 故障隔离：原始数据不丢、其它源照常加工、告警脱敏。"""
    _seed_source(session_factory, "iso-bad")
    _seed_source(session_factory, "iso-good")

    def factory(source: Source) -> BaseCollector:
        processor = _BrokenProcessor() if source.name == "iso-bad" else CollectionProcessor()
        return _ProbeCollector(source, post_processor=processor)

    with _capture_collector_logs() as messages:
        code = main(
            ["--once"],
            session_factory=session_factory,
            clock=_Clock(MOMENT),
            collector_factory=factory,
        )

    assert code == 0  # 后处理异常不改变采集状态，也不让整轮失败
    assert _ProbeCollector.executions == ["iso-bad", "iso-good"]  # 两个源都执行了

    raws = _rows(session_factory, RawItem)
    assert len(raws) == 2  # ★ 原始数据一条都不许丢
    processed = _rows(session_factory, ProcessedItem)
    assert len(processed) == 1  # 只有正常源产出加工结果
    assert processed[0].status is ProcessStatus.SUCCESS

    jobs = _job_runs(session_factory)
    assert len(jobs) == 1
    output = jobs[0].output_json
    warnings = " | ".join(output["warnings"])
    assert "Processor 后处理异常" in warnings
    assert "processor-boom" in warnings
    assert SECRET_VALUE not in json.dumps(output, ensure_ascii=False)
    assert "***" in warnings  # 凭据被擦除而不是透传

    joined_logs = " | ".join(messages)
    assert "Processor 后处理异常" in joined_logs
    assert SECRET_VALUE not in joined_logs  # ★ 日志里不得出现 api_key 明文
    assert "***" in joined_logs
