"""30 分钟 Collector Scheduler CLI（研究数据采集服务，**不是**实盘服务）。

用法::

    # 单轮：执行"当前 UTC 30 分钟槽"的采集（幂等，重复执行不会重复采集）
    python -m scripts.run_collector_scheduler --once

    # 常驻：每个 30 分钟槽执行一次（Ctrl+C 正常退出）
    python -m scripts.run_collector_scheduler --interval-minutes 30

    # 采集后立即加工（raw_items → processed_items）：显式开启 Processor
    python -m scripts.run_collector_scheduler --interval-minutes 30 --with-processor

安全与边界（与 .clinerules 一致）：
- 只读取 ``sources``（``enabled=true`` 且配置 ``config_json.collector``），
  不打印、不落库任何密钥 / Token（``JobRun.output_json`` 只写白名单摘要）；
- 采集器构造走 :func:`src.collectors.bootstrap.default_collector_factory`（生产注册表），
  授权 / robots / 证书等门禁仍由各采集器自身强制，本 CLI 不做任何绕过；
- ``--with-processor`` **默认关闭**：不传该开关时注入 ``post_processor=None``，
  行为与 GOLD-001-R2 完全一致；开启后由 CLI 显式构造
  :class:`~src.processors.collection.pipeline.CollectionProcessor` 并交给 bootstrap 注入，
  Processor 的业务逻辑既不在本 CLI、也不在 ``src/scheduler/core.py``；
- 调度只使用标准库 ``asyncio`` / ``time``，未引入 APScheduler / Celery。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from config.logging import get_logger  # noqa: E402
from database.session import build_engine, build_session_factory, session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.collectors.bootstrap import default_collector_factory  # noqa: E402
from src.common.time import utc_now  # noqa: E402
from src.processors.collection.contracts import PersistedItemProcessor  # noqa: E402
from src.processors.collection.pipeline import CollectionProcessor  # noqa: E402
from src.scheduler.core import (  # noqa: E402
    CollectorFactory,
    SchedulerRunResult,
    default_stale_after,
    run_scheduled_collection_once,
)
from src.scheduler.slots import (  # noqa: E402
    DEFAULT_INTERVAL_MINUTES,
    seconds_until_next_slot,
)

_log = get_logger("scripts.run_collector_scheduler")

ClockFn = Callable[[], datetime]
SleeperFn = Callable[[float], None]


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--once`` / ``--interval-minutes`` / ``--stale-after-minutes``）。"""
    parser = argparse.ArgumentParser(
        prog="run_collector_scheduler",
        description="30 分钟采集调度（研究数据采集服务；不是实盘服务）",
    )
    parser.add_argument("--once", action="store_true", help="只执行当前调度槽一次后退出")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"调度间隔（分钟，默认 {DEFAULT_INTERVAL_MINUTES}；必须整除 24 小时）",
    )
    parser.add_argument(
        "--stale-after-minutes",
        type=int,
        default=None,
        help="在途 RUNNING/RETRYING 多久后可接管（默认 3 倍调度间隔）",
    )
    parser.add_argument(
        "--with-processor",
        action="store_true",
        help="采集成功后立即执行采集后处理（raw_items → processed_items）；默认关闭",
    )
    return parser


def build_post_processor(enabled: bool) -> PersistedItemProcessor | None:
    """按显式开关构造采集后处理 Processor（默认关闭 → ``None``）。

    Args:
        enabled: ``--with-processor`` 是否显式开启。

    Returns:
        ``None``（默认，行为与 GOLD-001-R2 一致、零 ``processed_items``）或
        :class:`~src.processors.collection.pipeline.CollectionProcessor` 实例。

    备注：这里只做"显式可选接线"——Processor 的业务逻辑不在本 CLI，也不在
    ``src/scheduler/core.py``；实例最终由
    :func:`src.collectors.bootstrap.default_collector_factory` 注入采集器。
    """
    return CollectionProcessor() if enabled else None


def resolve_stale_after(interval_minutes: int, override_minutes: int | None) -> timedelta:
    """把 ``--stale-after-minutes`` 解析为 ``timedelta``（缺省 = 3 倍调度间隔）。"""
    if override_minutes is None:
        return default_stale_after(interval_minutes)
    return timedelta(minutes=override_minutes)


def run_once(
    session: Session,
    *,
    interval_minutes: int,
    stale_after: timedelta,
    clock: ClockFn = utc_now,
    collector_factory: CollectorFactory | None = None,
) -> SchedulerRunResult:
    """同步执行一轮调度（内部 ``asyncio.run``；供 CLI 与测试复用）。"""
    return asyncio.run(
        run_scheduled_collection_once(
            session,
            interval_minutes=interval_minutes,
            clock=clock,
            stale_after=stale_after,
            collector_factory=collector_factory,
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    clock: ClockFn = utc_now,
    sleeper: SleeperFn = time.sleep,
    collector_factory: CollectorFactory | None = None,
) -> int:
    """CLI 主入口。

    Returns:
        进程退出码：``--once`` 下 JobRun 为 FAILED 时返回 1，其余 0；Ctrl+C 安全退出返回 0。
    """
    configure_stdout()
    args = build_parser().parse_args(argv)
    stale_after = resolve_stale_after(args.interval_minutes, args.stale_after_minutes)
    processor = build_post_processor(args.with_processor)
    if processor is not None:
        _log.info(
            "已显式启用采集后处理：processor=%s@%s（raw_items → processed_items）",
            processor.processor_name,
            processor.processor_version,
        )
    factory = (
        collector_factory
        if collector_factory is not None
        else default_collector_factory(post_processor=processor)
    )
    factory_session = session_factory or build_session_factory(build_engine())

    if args.once:
        with session_scope(factory_session) as session:
            result = run_once(
                session,
                interval_minutes=args.interval_minutes,
                stale_after=stale_after,
                clock=clock,
                collector_factory=factory,
            )
        print(result.render())
        return 1 if result.is_failure else 0

    _log.info("启动常驻调度：interval=%d 分钟", args.interval_minutes)
    try:
        while True:
            with session_scope(factory_session) as session:
                result = run_once(
                    session,
                    interval_minutes=args.interval_minutes,
                    stale_after=stale_after,
                    clock=clock,
                    collector_factory=factory,
                )
            print(result.render())
            delay = seconds_until_next_slot(clock(), args.interval_minutes)
            _log.info("下一个调度槽在 %.1f 秒后", delay)
            sleeper(delay)
    except KeyboardInterrupt:
        print("收到 Ctrl+C，调度循环已安全退出")
        return 0


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
