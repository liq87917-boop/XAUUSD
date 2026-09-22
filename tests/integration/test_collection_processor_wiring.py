"""采集 → Processor 端到端接线测试（TD-11，参考实现 = RSS 采集路径）。

★ 全程不联网 ★：HTTP 由 ``conftest.MockTransport`` 脚本化提供，RSS 源均为 ``.invalid`` 域。

验证：
1. 注入 ``post_processor`` 后，``raw_items`` 与 ``processed_items`` 同时落库；
2. 不注入时行为与本能力引入前完全一致（零 ``processed_items``）——无行为回归；
3. Processor 抛异常不得中断采集（告警可观测、状态诚实）；
4. 加工结果不含凭据 / 完整 source config（URL query 被剥离）；
5. CLI ``--to-db`` 路径启用 Processor，统计含 processing 摘要。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import ProcessedItem, RawItem
from database.models.enums import CollectorRunStatus, ProcessStatus, RawItemType, SourceType
from scripts import collect_rss
from src.collectors import CollectWindow, HttpResponse, run_collector
from src.collectors.rss_collector import RssCollector, RssSourceSpec
from src.processors.collection.pipeline import CollectionProcessor

pytestmark = pytest.mark.integration

SECRET_VALUE = "SECRETVALUE-9f1c"
FEED_A = f"https://feed-a.invalid/rss.xml?api_key={SECRET_VALUE}"
WINDOW = CollectWindow(
    start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 30, tzinfo=UTC)
)


def _spec(source_id: str, url: str) -> RssSourceSpec:
    return RssSourceSpec(
        id=source_id,
        name=source_id,
        url=url,
        source_name=source_id,
        author_name="金十数据",
        enabled=True,
        verified=True,
    )


def _rss(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>Gold</title>" + "".join(items) + "</channel></rss>"
    )


def _item(guid: str, *, title: str = "黄金 3400 上方继续看多") -> str:
    return (
        "<item>"
        f"<title><![CDATA[{title}]]></title>"
        f"<link>https://feed.invalid/{guid}</link>"
        "<description><![CDATA[<p>3400 上方减仓后继续持有。</p>]]></description>"
        f'<guid isPermaLink="false">{guid}</guid>'
        "<pubDate>Mon, 14 Sep 2026 08:30:00 GMT</pubDate>"
        "</item>"
    )


def _feed(*items: str) -> HttpResponse:
    return HttpResponse(status=200, text=_rss(*items))


def _raws(session: Session) -> list[RawItem]:
    return list(
        session.scalars(sa.select(RawItem).where(RawItem.item_type == RawItemType.NEWS)).all()
    )


def _processed(session: Session) -> list[ProcessedItem]:
    return list(session.scalars(sa.select(ProcessedItem)).all())


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.fixture()
def rss_source(make_source):
    return make_source(
        name="rss-source",
        source_type=SourceType.NEWS,
        base_url=FEED_A,
        enabled=True,
        config_json={"min_records_per_run": 0},
    )


class _BrokenProcessor:
    """故意在加工时抛异常（验证采集层兜底隔离）。"""

    processor_name = "broken_processor"
    processor_version = "broken-v1"

    def process_persisted(self, session: Session, raw_item: Any) -> Any:
        raise RuntimeError("processor-boom")


async def test_rss_collection_writes_raw_and_processed_items(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport(
            [HttpResponse(404), _feed(_item("g-1"), _item("g-2", title="Gold nears 3450"))]
        ),
        cache_dir=tmp_path,
        post_processor=CollectionProcessor(),
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert result.outcome is not None and result.outcome.inserted_count == 2

    raws = {item.source_record_id: item for item in _raws(session)}
    rows = _processed(session)
    assert set(raws) == {"g-1", "g-2"}
    assert len(rows) == 2
    assert {str(row.raw_item_id) for row in rows} == {str(item.id) for item in raws.values()}

    raw_by_id = {item.id: item for item in raws.values()}
    for row in rows:
        raw = raw_by_id[row.raw_item_id]
        assert row.status is ProcessStatus.SUCCESS
        assert row.processor_name == "collection_normalizer"
        assert row.normalized_text == "3400 上方减仓后继续持有。"
        assert _as_utc(row.effective_at) >= _as_utc(raw.effective_at)

    summary = collector.processing_summary()
    assert summary is not None
    assert summary["processed"] == 2
    assert summary["failed"] == 0
    assert summary["processor"] == "collection_normalizer"

    # 凭据不得进入加工结果：URL 的 query（含 api_key）必须被剥离
    serialized = "".join(str(row.structured_json) for row in rows)
    assert SECRET_VALUE not in serialized
    assert "feed-a.invalid/rss.xml" in serialized


async def test_rss_collection_without_processor_does_not_write_processed_items(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport([HttpResponse(404), _feed(_item("g-1"))]),
        cache_dir=tmp_path,
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS
    assert len(_raws(session)) == 1
    assert _processed(session) == []  # 未接线 → 行为与引入前一致
    assert collector.post_processor is None
    assert collector.processing_summary() is None


async def test_processor_failure_does_not_break_collection(
    session: Session, rss_source, mock_transport, tmp_path: Path
) -> None:
    collector = RssCollector(
        rss_source,
        specs=[_spec("feed_a", FEED_A)],
        transport=mock_transport([HttpResponse(404), _feed(_item("g-1"))]),
        cache_dir=tmp_path,
        post_processor=_BrokenProcessor(),
    )

    result = await run_collector(session, collector, window=WINDOW)

    assert result.status is CollectorRunStatus.SUCCESS  # 采集本身成功
    assert len(_raws(session)) == 1  # 原始数据必须保住
    assert _processed(session) == []
    assert any("Processor 后处理异常" in warning for warning in result.warnings)
    summary = collector.processing_summary()
    assert summary is not None
    assert summary["processed"] == 0
    assert any("processor-boom" in warning for warning in summary["warnings"])


async def test_cli_to_db_runs_processor_and_reports_summary(
    session: Session,
    engine: sa.Engine,
    make_source,
    mock_transport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``collect_rss --to-db`` 是 Processor 的**参考接线路径**（生产 CLI 级验证）。"""
    make_source(
        name="feed_a",
        source_type=SourceType.NEWS,
        base_url=FEED_A,
        enabled=True,
        config_json={"min_records_per_run": 0},
    )
    session.commit()  # _drive_to_db 使用独立事务，需要先让来源可见
    transport = mock_transport([HttpResponse(404), _feed(_item("cli-p1"))])
    monkeypatch.setattr(collect_rss, "AiohttpTransport", lambda: transport)
    monkeypatch.setattr("database.session.build_engine", lambda: engine)

    args = argparse.Namespace(
        lookback_days=90,
        min_interval=1.0,
        cache_dir=str(tmp_path / "cache"),
        cache_mode="auto",
        per_feed_limit=None,
        json_out=str(tmp_path / "stats.json"),
    )
    code = await collect_rss._drive_to_db(args, [_spec("feed_a", FEED_A)])  # noqa: SLF001

    assert code == 0
    session.expire_all()
    assert len(_raws(session)) == 1
    rows = _processed(session)
    assert len(rows) == 1
    assert rows[0].status is ProcessStatus.SUCCESS
    assert SECRET_VALUE not in str(rows[0].structured_json)

    stats = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    processing = stats["sources"][0]["processing"]
    assert processing["processor"] == "collection_normalizer"
    assert processing["processed"] == 1
    assert processing["failed"] == 0
