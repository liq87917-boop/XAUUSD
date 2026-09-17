from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorPost, RawItem
from database.models.enums import SourceType
from scripts.import_real_posts import import_rows


def test_real_post_import_is_traceable_and_idempotent(session: Session, make_source) -> None:
    make_source(name="manual-test", source_type=SourceType.NEWS, enabled=False)
    rows = [
        {
            "id": "",
            "content": "黄金突破关键阻力，未来一小时继续看多，目标价上移。" * 4,
            "title": "黄金突破",
            "published_at": "2026-09-01T08:00:00+00:00",
            "effective_at": "2026-09-01T08:00:00+00:00",
            "source": "manual-test",
            "author_name": "测试作者",
            "url": "https://example.invalid/post-1",
            "has_media": "false",
            "language": "zh",
            "category": "gold",
            "collected_at": "",
        }
    ]

    first = import_rows(session, rows)
    second = import_rows(session, rows)

    assert first.inserted == 1 and first.used_effective_as_collected == 1
    assert second.inserted == 0 and second.duplicate == 1
    raw = session.scalar(sa.select(RawItem))
    post = session.scalar(sa.select(AuthorPost))
    assert raw is not None and post is not None
    assert raw.raw_json["collected_at_provenance"] == "input_effective_at_fallback"
    assert raw.effective_at.replace(tzinfo=UTC) == datetime(2026, 9, 1, 8, tzinfo=UTC)
    assert post.raw_item_id == raw.id
