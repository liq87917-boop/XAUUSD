"""OpenNewsCollector 集成测试（SQLite，100% Mock，不访问外部网络）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import NewsEvent, Source
from database.models.enums import SourceType
from src.collectors.base import PERSIST_INSERTED
from src.collectors.errors import CollectorError
from src.collectors.opennews import OPENNEWS_TOKEN_ENV, OpenNewsCollector
from src.collectors.transport import HttpResponse
from src.collectors.types import CollectWindow

pytestmark = pytest.mark.integration

FIXED_CLOCK = datetime(2026, 9, 20, tzinfo=UTC)


def _response() -> HttpResponse:
    return HttpResponse(
        status=200,
        json_body={
            "data": [
                {
                    "text": "Gold rallies on Fed outlook",
                    "description": "Gold prices rose...",
                    "ts": "2026-09-15T10:00:00Z",
                    "source": "test-feed",
                    "link": "https://example.invalid/gold",
                    "engineType": "news",
                }
            ]
        },
    )


class _Transport:
    def __init__(self, response: HttpResponse) -> None:
        self._response = response
        self.requests: list = []

    async def send(self, request):
        self.requests.append(request)
        return self._response


@pytest.fixture()
def opennews_source(session: Session, make_source) -> Source:
    return make_source(name="opennews", source_type=SourceType.NEWS)


def _window() -> CollectWindow:
    return CollectWindow(
        start_at=datetime(2026, 9, 1, tzinfo=UTC), end_at=datetime(2026, 9, 20, tzinfo=UTC)
    )


def test_do_fetch_and_persist_writes_news_event(
    session: Session, opennews_source, monkeypatch
) -> None:
    monkeypatch.setenv(OPENNEWS_TOKEN_ENV, "secret-token")
    collector = OpenNewsCollector(
        opennews_source, transport=_Transport(_response()), clock=lambda: FIXED_CLOCK
    )
    page = asyncio.run(collector._do_fetch(None, _window()))
    assert len(page.payloads) == 1
    payload = page.payloads[0]

    assert collector._persist_payload(session, payload) == PERSIST_INSERTED
    session.flush()
    event = session.scalar(sa.select(NewsEvent))
    assert event is not None
    assert event.headline == "Gold rallies on Fed outlook"
    assert event.published_at == datetime(2026, 9, 15, 10, 0)  # SQLite 读回 naive
    # 采集时刻 >= 发布时间 → effective_at = collected_at
    assert event.effective_at == FIXED_CLOCK.replace(tzinfo=None)


def test_build_request_raises_without_token(
    session: Session, opennews_source, monkeypatch
) -> None:
    monkeypatch.delenv(OPENNEWS_TOKEN_ENV, raising=False)
    collector = OpenNewsCollector(opennews_source, transport=_Transport(_response()))
    with pytest.raises(CollectorError, match="6551.io/mcp"):
        collector._build_request("gold")


def test_build_request_adds_bearer_token(session: Session, opennews_source, monkeypatch) -> None:
    monkeypatch.setenv(OPENNEWS_TOKEN_ENV, "secret-token")
    collector = OpenNewsCollector(opennews_source, transport=_Transport(_response()))
    request = collector._build_request("gold")
    assert request.headers["Authorization"] == "Bearer secret-token"
