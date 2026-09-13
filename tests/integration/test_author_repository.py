"""作者库仓储集成测试（04 §3/§4 + 审计留痕 + 幂等 + 软删除）。

覆盖团队要求：「实现作者/账号的 Repository（增加/更新/查询）」与「所有修改记录 audit_logs」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuditLog, Author, AuthorAccount
from database.models.enums import AuthorStatus
from database.repositories.authors import AuthorAccountRepository, AuthorRepository

pytestmark = pytest.mark.integration


def _audits(session: Session, *, action: str | None = None) -> list[AuditLog]:
    statement = sa.select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    return list(session.scalars(statement).all())


# ---------------------------------------------------------------------------
# 1) 作者 CRUD + 审计
# ---------------------------------------------------------------------------
def test_create_author_persists_and_writes_audit(session: Session) -> None:
    repo = AuthorRepository(session, ip_address="127.0.0.1")

    author = repo.create(display_name="某机构分析师", canonical_name="analyst-a")
    session.flush()

    assert repo.get(author.id) is author
    assert repo.get_by_canonical_name("analyst-a") is author
    assert author.status is AuthorStatus.ACTIVE
    assert author.is_deleted is False

    audits = _audits(session, action="author.create")
    assert len(audits) == 1
    entry = audits[0]
    assert entry.entity_type == "authors"
    assert entry.entity_id == author.id
    assert entry.before_json is None
    assert entry.after_json is not None
    assert entry.after_json["display_name"] == "某机构分析师"
    assert entry.after_json["canonical_name"] == "analyst-a"
    assert entry.ip_address == "127.0.0.1"


def test_create_author_rejects_duplicate_canonical_name(session: Session) -> None:
    repo = AuthorRepository(session)
    repo.create(display_name="A", canonical_name="dup")

    with pytest.raises(ValueError, match="已存在"):
        repo.create(display_name="B", canonical_name="dup")


@pytest.mark.parametrize("display_name", ["", "   "])
def test_create_author_rejects_blank_display_name(session: Session, display_name: str) -> None:
    repo = AuthorRepository(session)

    with pytest.raises(ValueError, match="display_name"):
        repo.create(display_name=display_name)


def test_ensure_is_idempotent_and_does_not_overwrite(session: Session) -> None:
    """幂等：第二次调用返回既有作者且**不修改任何字段**（种子口径）。"""
    repo = AuthorRepository(session)
    first, created_first = repo.ensure(display_name="原始名", canonical_name="same-key")
    second, created_second = repo.ensure(display_name="新名字", canonical_name="same-key")

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.display_name == "原始名"  # 不覆盖
    assert len(_audits(session, action="author.ensure_create")) == 1


def test_ensure_without_canonical_name_matches_by_display_name(session: Session) -> None:
    repo = AuthorRepository(session)
    author, _ = repo.ensure(display_name="无标准名作者")
    again, created = repo.ensure(display_name="无标准名作者")

    assert created is False
    assert again.id == author.id


def test_list_and_count_respect_soft_delete(session: Session) -> None:
    repo = AuthorRepository(session)
    keeper = repo.create(display_name="保留", canonical_name="keep")
    removed = repo.create(display_name="删除", canonical_name="gone")

    assert repo.count() == 2
    assert {item.id for item in repo.list_authors()} == {keeper.id, removed.id}

    repo.soft_delete(removed)
    session.flush()

    assert repo.count() == 1
    assert repo.count(include_deleted=True) == 2
    assert [item.id for item in repo.list_authors()] == [keeper.id]
    assert {item.id for item in repo.list_authors(include_deleted=True)} == {keeper.id, removed.id}


def test_update_status_writes_audit_and_skips_unchanged(session: Session) -> None:
    repo = AuthorRepository(session)
    author = repo.create(display_name="状态作者", canonical_name="status-author")

    repo.update_status(author, AuthorStatus.PROBATION)
    repo.update_status(author, AuthorStatus.PROBATION)  # 无变化 → 不写审计

    assert author.status is AuthorStatus.PROBATION
    audits = _audits(session, action="author.status_change")
    assert len(audits) == 1
    assert audits[0].before_json is not None
    assert audits[0].after_json is not None
    assert audits[0].before_json["status"] == AuthorStatus.ACTIVE.value
    assert audits[0].after_json["status"] == AuthorStatus.PROBATION.value


def test_update_profile_writes_audit_only_on_change(session: Session) -> None:
    repo = AuthorRepository(session)
    author = repo.create(display_name="旧的", canonical_name="profile-author")

    repo.update_profile(author, display_name="新的")
    repo.update_profile(author, display_name="新的")  # 无变化

    assert author.display_name == "新的"
    audits = _audits(session, action="author.update")
    assert len(audits) == 1
    assert audits[0].before_json is not None
    assert audits[0].before_json["display_name"] == "旧的"


def test_soft_delete_is_idempotent_and_audited(session: Session) -> None:
    repo = AuthorRepository(session)
    author = repo.create(display_name="待删除", canonical_name="soft-delete")

    repo.soft_delete(author)
    repo.soft_delete(author)

    assert author.is_deleted is True
    assert author.deleted_at is not None
    assert len(_audits(session, action="author.soft_delete")) == 1


def test_repository_threads_actor_into_audit(session: Session) -> None:
    user_id = uuid.uuid4()
    repo = AuthorRepository(session, user_id=user_id, ip_address="10.0.0.9")

    repo.create(display_name="带操作者", canonical_name="actor-author")

    entry = _audits(session, action="author.create")[0]
    assert entry.user_id == user_id
    assert entry.ip_address == "10.0.0.9"


# ---------------------------------------------------------------------------
# 2) 平台账号 CRUD + 审计
# ---------------------------------------------------------------------------
def test_account_create_persists_and_audits(session: Session, make_source) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="账号作者", canonical_name="account-author")
    source = make_source(name="news-feed")

    account = accounts.create(
        author=author,
        source=source,
        external_account_id="rss-001",
        account_name="机构账号",
        follower_count=1500,
        verified=True,
    )
    session.flush()

    assert accounts.get(account.id) is account
    assert (
        accounts.get_by_external_id(source_id=source.id, external_account_id="rss-001") is account
    )
    assert account.enabled is True

    entry = _audits(session, action="author_account.create")[0]
    assert entry.entity_type == "author_accounts"
    assert entry.entity_id == account.id
    assert entry.after_json is not None
    assert entry.after_json["external_account_id"] == "rss-001"
    assert entry.after_json["author_id"] == str(author.id)


def test_account_create_rejects_duplicate(session: Session, make_source) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="重复账号", canonical_name="dup-account")
    source = make_source(name="news-feed")

    accounts.create(author=author, source=source, external_account_id="rss-dup")

    with pytest.raises(ValueError, match="已存在"):
        accounts.create(author=author, source=source, external_account_id="rss-dup")


@pytest.mark.parametrize("external_id", ["", "   "])
def test_account_create_rejects_blank_external_id(
    session: Session, make_source, external_id: str
) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="空账号", canonical_name="blank-account")

    with pytest.raises(ValueError, match="external_account_id"):
        accounts.create(
            author=author, source=make_source(name="news-feed"), external_account_id=external_id
        )


def test_account_upsert_is_idempotent_without_overwriting(session: Session, make_source) -> None:
    """默认不覆盖：第二次 upsert 不得改写既有账号字段（运维配置优先）。"""
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="幂等作者", canonical_name="upsert-author")
    source = make_source(name="news-feed")

    account, created_first = accounts.upsert(
        author=author, source=source, external_account_id="rss-upsert", account_name="原始名"
    )
    same, created_second = accounts.upsert(
        author=author, source=source, external_account_id="rss-upsert", account_name="新名字"
    )

    assert created_first is True
    assert created_second is False
    assert same.id == account.id
    assert same.account_name == "原始名"
    assert same.enabled is True
    assert len(_audits(session, action="author_account.upsert_create")) == 1


def test_account_upsert_with_update_existing_refreshes_and_audits(
    session: Session, make_source
) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="刷新作者", canonical_name="refresh-author")
    source = make_source(name="news-feed")

    accounts.upsert(author=author, source=source, external_account_id="rss-refresh")
    refreshed, created = accounts.upsert(
        author=author,
        source=source,
        external_account_id="rss-refresh",
        account_name="渠道改名",
        follower_count=260,
        update_existing=True,
    )

    assert created is False
    assert refreshed.account_name == "渠道改名"
    assert refreshed.follower_count == 260
    entry = _audits(session, action="author_account.update")[0]
    assert entry.before_json is not None
    assert entry.before_json["account_name"] is None


def test_account_set_enabled_audits_and_skips_unchanged(session: Session, make_source) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="启停作者", canonical_name="enable-author")
    account = accounts.create(
        author=author, source=make_source(name="news-feed"), external_account_id="rss-toggle"
    )

    accounts.set_enabled(account, False)
    accounts.set_enabled(account, False)  # 无变化 → 不写审计

    assert account.enabled is False
    audits = _audits(session, action="author_account.enable_change")
    assert len(audits) == 1
    assert audits[0].after_json is not None
    assert audits[0].after_json["enabled"] is False


def test_touch_last_collected_at_does_not_write_audit(session: Session, make_source) -> None:
    """采集性心跳不写审计（见仓储模块口径说明），但字段必须被更新。"""
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="心跳作者", canonical_name="heartbeat-author")
    account = accounts.create(
        author=author, source=make_source(name="news-feed"), external_account_id="rss-beat"
    )
    before_audits = len(_audits(session))

    collected_at = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
    accounts.touch_last_collected_at(account, collected_at=collected_at)
    session.flush()

    assert account.last_collected_at is not None
    assert len(_audits(session)) == before_audits


def test_list_for_author_filters_enabled(session: Session, make_source) -> None:
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="多账号作者", canonical_name="multi-account")
    source = make_source(name="news-feed")

    enabled_account = accounts.create(
        author=author, source=source, external_account_id="rss-enabled"
    )
    disabled_account = accounts.create(
        author=author, source=source, external_account_id="rss-disabled", enabled=False
    )

    assert {item.id for item in accounts.list_for_author(author.id)} == {
        enabled_account.id,
        disabled_account.id,
    }
    assert [item.id for item in accounts.list_for_author(author.id, enabled_only=True)] == [
        enabled_account.id
    ]
    assert accounts.count() == 2


def test_resolve_source_returns_none_for_unknown(session: Session, make_source) -> None:
    accounts = AuthorAccountRepository(session)
    source = make_source(name="known-source")

    assert accounts.resolve_source("known-source") is source
    assert accounts.resolve_source("no-such-source") is None


def test_author_and_account_are_mutable_entities(session: Session, make_source) -> None:
    """作者/账号是可变实体（允许 UPDATE），与 append-only 事实表明确区分。"""
    authors = AuthorRepository(session)
    accounts = AuthorAccountRepository(session)
    author = authors.create(display_name="可变实体", canonical_name="mutable-entity")
    account = accounts.create(
        author=author, source=make_source(name="news-feed"), external_account_id="rss-mutable"
    )

    author.display_name = "改名后"
    account.account_name = "渠道改名"
    session.flush()

    assert session.get(Author, author.id).display_name == "改名后"
    assert session.get(AuthorAccount, account.id).account_name == "渠道改名"