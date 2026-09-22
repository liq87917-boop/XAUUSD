"""三个采集器 dry-run 全链路测试（真实调用，**不落库**）。

对每个采集器：构造临时 Source + Collector，跑 ``collect()`` 完整流程
（fetch → parse → 幂等判重），用 savepoint 回滚所有写入。单个 provider 失败
不阻塞其他采集器；记录 provider / 状态 / 条数 / 异常原因。

用法：python scripts/collectors/dry_run_collectors.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from database.models import Source
from database.models.enums import SourceType
from database.seeds import seed_instruments
from database.session import session_scope
from src.collectors.akshare_gold import AkshareGoldCollector
from src.collectors.base import BaseCollector
from src.collectors.dbnomics_macro import DbnomicsMacroCollector
from src.collectors.opennews import OpenNewsCollector
from src.collectors.transport import AiohttpTransport
from src.collectors.types import CollectWindow

WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 21, tzinfo=UTC)
)


def _plan() -> list[tuple[str, Source, BaseCollector]]:
    akshare = Source(
        name="smoke_akshare", source_type=SourceType.MARKET, base_url="",
        config_json={"collector": "akshare_gold"},
    )
    dbnomics = Source(
        name="smoke_dbnomics", source_type=SourceType.MACRO, base_url="",
        config_json={"collector": "dbnomics_macro", "provider": "IMF", "series": "CPI",
                     "dimensions": {"REF_AREA": "US"}},
    )
    opennews = Source(
        name="smoke_opennews", source_type=SourceType.NEWS, base_url="https://ai.6551.io",
        config_json={"collector": "opennews", "keywords": ["gold"]},
    )
    return [
        ("akshare_gold", akshare, AkshareGoldCollector(akshare)),
        (
            "dbnomics_macro",
            dbnomics,
            DbnomicsMacroCollector(dbnomics, transport=AiohttpTransport()),
        ),
        ("opennews", opennews, OpenNewsCollector(opennews, transport=AiohttpTransport())),
    ]


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def run_all() -> None:
    _load_env()
    with session_scope() as session:
        seed_instruments(session)
        session.flush()
        for name, source, collector in _plan():
            nested = session.begin_nested()
            try:
                session.add(source)
                session.flush()
                outcome = asyncio.run(collector.collect(session=session, window=WINDOW))
                nested.rollback()
                print(
                    f"[{name}] status={outcome.status.value} "
                    f"fetched={outcome.fetched_count} inserted={outcome.inserted_count} "
                    f"duplicate={outcome.duplicate_count} failed={outcome.failed_count}"
                )
                if outcome.error_message:
                    print(f"    error: {outcome.error_message}")
            except Exception as exc:  # noqa: BLE001
                nested.rollback()
                print(f"[{name}] EXCEPTION: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    run_all()

