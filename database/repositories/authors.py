"""作者库仓储层（04 §3 authors / §4 author_accounts）。

对应文档：
- 04 §3/§4：字段定义；``authors.canonical_name`` 与
  ``author_accounts(source_id, external_account_id)`` 是幂等自然键
- 02 §5.6：作者状态 ACTIVE / EXPLORATION / PROBATION / DISABLED（低权重作者进 PROBATION，
  不永久删除）
- 03 §16：管理操作必须写 ``audit_logs``（本模块每次**管理性**修改都会留痕）

分层约定（06_Cline开发规则 第 4 条）：
    仓储层只做"受约束的数据访问"，不包含业务判断；上层（CLI / service）负责编排与事务提交。
    本模块**不 commit**（与 ``raw_items.py`` 口径一致），由调用方决定事务边界，
    以保证"业务修改 + 审计"原子可见。

审计范围说明（重要取舍）：
    - **管理性修改**（作者创建 / 改名 / 改状态 / 软删除；账号创建 / 改名 / 启停）→ 必写审计；
    - **采集性写入**（``author_accounts.last_collected_at`` 心跳）→ 不写审计：
      它由每轮采集触发（进度已在 ``collector_runs`` 留痕），逐次审计只会淹没审计表。
      该取舍在此显式记录，便于审计口径复核。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import Author, AuthorAccount, Source
from database.models.enums import AuthorStatus
from database.repositories.audit import record_audit, snapshot_model
from src.common.time import utc_now

__all__ = ["AuthorAccountRepository", "AuthorRepository"]

_AUTHOR_ENTITY = "authors"
_ACCOUNT_ENTITY = "author_accounts"


class AuthorRepository:
    """``authors`` 的查询与安全修改（每次管理性修改都写 ``audit_logs``）。"""

    def __init__(
        self,
        session: Session,
        *,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._ip_address = ip_address

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, author_id: uuid.UUID) -> Author | None:
        return self._session.get(Author, author_id)

    def get_by_canonical_name(self, canonical_name: str) -> Author | None:
        """按自然键查询（``canonical_name`` 唯一）。"""
        return self._session.scalar(
            sa.select(Author).where(Author.canonical_name == canonical_name.strip())
        )

    def find_by_display_name(self, display_name: str) -> Author | None:
        return self._session.scalar(
            sa.select(Author).where(Author.display_name == display_name.strip())
        )

    def list_authors(
        self,
        *,
        status: AuthorStatus | None = None,
        include_deleted: bool = False,
    ) -> list[Author]:
        statement = sa.select(Author).order_by(Author.display_name)
        if not include_deleted:
            statement = statement.where(Author.is_deleted.is_(False))
        if status is not None:
            statement = statement.where(Author.status == status)
        return list(self._session.scalars(statement).all())

    def count(self, *, include_deleted: bool = False) -> int:
        statement = sa.select(sa.func.count()).select_from(Author)
        if not include_deleted:
            statement = statement.where(Author.is_deleted.is_(False))
        return int(self._session.scalar(statement) or 0)

    # ------------------------------------------------------------------
    # 修改（管理性操作 → 写审计）
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        display_name: str,
        canonical_name: str | None = None,
        status: AuthorStatus = AuthorStatus.ACTIVE,
        metadata_json: dict[str, object] | None = None,
        action: str = "author.create",
    ) -> Author:
        """创建作者（自然键冲突时抛 ``ValueError``，避免脏数据进入事实表）。"""
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("authors.display_name 不能为空（04 §3）")

        clean_canonical = canonical_name.strip() if canonical_name else None
        if clean_canonical is not None:
            existing = self.get_by_canonical_name(clean_canonical)
            if existing is not None:
                raise ValueError(
                    f"authors.canonical_name={clean_canonical!r} 已存在（id={existing.id}）："
                    "请改用 ensure() 或在导入前做去重"
                )

        author = Author(
            display_name=clean_name,
            canonical_name=clean_canonical,
            status=status,
            metadata_json=dict(metadata_json) if metadata_json else None,
        )
        self._session.add(author)
        self._session.flush()
        record_audit(
            self._session,
            action=action,
            entity_type=_AUTHOR_ENTITY,
            entity_id=author.id,
            before=None,
            after=snapshot_model(author),
            user_id=self._user_id,
            ip_address=self._ip_address,
        )
        return author

    def ensure(
        self,
        *,
        display_name: str,
        canonical_name: str | None = None,
        status: AuthorStatus = AuthorStatus.ACTIVE,
        metadata_json: dict[str, object] | None = None,
        action: str = "author.ensure_create",
    ) -> tuple[Author, bool]:
        """幂等写入：已存在（按 ``canonical_name`` → ``display_name`` 匹配）则原样返回。

        Returns:
            ``(author, created)``；``created=False`` 表示命中既有作者且**未做任何修改**
            （与种子"只补齐、不覆盖运维配置"的口径一致）。
        """
        clean_canonical = canonical_name.strip() if canonical_name else None
        existing = (
            self.get_by_canonical_name(clean_canonical)
            if clean_canonical is not None
            else self.find_by_display_name(display_name)
        )
        if existing is not None:
            return existing, False
        return (
            self.create(
                display_name=display_name,
                canonical_name=clean_canonical,
                status=status,
                metadata_json=metadata_json,
                action=action,
            ),
            True,
        )

    def update_status(
        self, author: Author, status: AuthorStatus, *, action: str = "author.status_change"
    ) -> Author:
        """变更作者状态（含 PROBATION / DISABLED；状态未变则不写审计）。"""
        if author.status == status:
            return author
        before = snapshot_model(author)
        author.status = status
        self._session.flush()
        record_audit(
            self._session,
            action=action,
            entity_type=_AUTHOR_ENTITY,
            entity_id=author.id,
            before=before,
            after=snapshot_model(author),
            user_id=self._user_id,
            ip_address=self._ip_address,
        )
        return author

    def update_profile(
        self,
        author: Author,
        *,
        display_name: str | None = None,
        metadata_json: dict[str, object] | None = None,
        action: str = "author.update",
    ) -> Author:
        """更新展示名 / 扩展信息（无实际变化时不写审计，避免噪声）。"""
        before = snapshot_model(author)
        if display_name is not None:
            clean = display_name.strip()
            if not clean:
                raise ValueError("authors.display_name 不能为空（04 §3）")
            author.display_name = clean
        if metadata_json is not None:
            author.metadata_json = dict(metadata_json)
        self._session.flush()
        after = snapshot_model(author)
        if before != after:
            record_audit(
                self._session,
                action=action,
                entity_type=_AUTHOR_ENTITY,
                entity_id=author.id,
                before=before,
                after=after,
                user_id=self._user_id,
                ip_address=self._ip_address,
            )
        return author

    def soft_delete(self, author: Author, *, action: str = "author.soft_delete") -> Author:
        """软删除（03 §15）：保留历史，不物理删除。"""
        if author.is_deleted:
            return author
        before = snapshot_model(author)
        author.is_deleted = True
        author.deleted_at = utc_now()
        self._session.flush()
        record_audit(
            self._session,
            action=action,
            entity_type=_AUTHOR_ENTITY,
            entity_id=author.id,
            before=before,
            after=snapshot_model(author),
            user_id=self._user_id,
            ip_address=self._ip_address,
        )
        return author


class AuthorAccountRepository:
    """``author_accounts`` 的查询与安全修改（平台账号，04 §4）。

    自然键：``(source_id, external_account_id)``（唯一约束兜底）。
    """

    def __init__(
        self,
        session: Session,
        *,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._ip_address = ip_address

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, account_id: uuid.UUID) -> AuthorAccount | None:
        return self._session.get(AuthorAccount, account_id)

    def get_by_external_id(
        self, *, source_id: uuid.UUID, external_account_id: str
    ) -> AuthorAccount | None:
        return self._session.scalar(
            sa.select(AuthorAccount).where(
                AuthorAccount.source_id == source_id,
                AuthorAccount.external_account_id == external_account_id.strip(),
            )
        )

    def list_for_author(
        self, author_id: uuid.UUID, *, enabled_only: bool = False
    ) -> list[AuthorAccount]:
        statement = (
            sa.select(AuthorAccount)
            .where(AuthorAccount.author_id == author_id)
            .order_by(AuthorAccount.external_account_id)
        )
        if enabled_only:
            statement = statement.where(AuthorAccount.enabled.is_(True))
        return list(self._session.scalars(statement).all())

    def count(self) -> int:
        return int(self._session.scalar(sa.select(sa.func.count()).select_from(AuthorAccount)) or 0)

    def resolve_source(self, source_name: str) -> Source | None:
        """按名称解析来源（导入 CSV 时需要把 `source_name` 映射为 `source_id`）。"""
        return self._session.scalar(sa.select(Source).where(Source.name == source_name.strip()))

    # ------------------------------------------------------------------
    # 修改（管理性操作 → 写审计）
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        author: Author,
        source: Source,
        external_account_id: str,
        account_name: str | None = None,
        profile_url: str | None = None,
        follower_count: int | None = None,
        verified: bool = False,
        enabled: bool = True,
        action: str = "author_account.create",
    ) -> AuthorAccount:
        """新增平台账号（``(source_id, external_account_id)`` 冲突时抛 ``ValueError``）。"""
        clean_external = external_account_id.strip()
        if not clean_external:
            raise ValueError("author_accounts.external_account_id 不能为空（04 §4）")

        existing = self.get_by_external_id(
            source_id=source.id, external_account_id=clean_external
        )
        if existing is not None:
            raise ValueError(
                f"账号 ({source.name}, {clean_external!r}) 已存在（id={existing.id}）："
                "请改用 upsert()"
            )

        account = AuthorAccount(
            author_id=author.id,
            source_id=source.id,
            external_account_id=clean_external,
            account_name=account_name.strip() if account_name else None,
            profile_url=profile_url.strip() if profile_url else None,
            follower_count=follower_count,
            verified=verified,
            enabled=enabled,
        )
        self._session.add(account)
        self._session.flush()
        record_audit(
            self._session,
            action=action,
            entity_type=_ACCOUNT_ENTITY,
            entity_id=account.id,
            before=None,
            after=snapshot_model(account),
            user_id=self._user_id,
            ip_address=self._ip_address,
        )
        return account

    def upsert(
        self,
        *,
        author: Author,
        source: Source,
        external_account_id: str,
        account_name: str | None = None,
        profile_url: str | None = None,
        follower_count: int | None = None,
        verified: bool = False,
        enabled: bool = True,
        update_existing: bool = False,
        action: str = "author_account.upsert_create",
    ) -> tuple[AuthorAccount, bool]:
        """幂等写入账号。

        Args:
            update_existing: 命中既有账号时是否刷新可变字段。默认 ``False``
                （"只补齐、不覆盖运维配置"，与种子口径一致）。

        Returns:
            ``(account, created)``。
        """
        clean_external = external_account_id.strip()
        existing = self.get_by_external_id(
            source_id=source.id, external_account_id=clean_external
        )
        if existing is None:
            return (
                self.create(
                    author=author,
                    source=source,
                    external_account_id=clean_external,
                    account_name=account_name,
                    profile_url=profile_url,
                    follower_count=follower_count,
                    verified=verified,
                    enabled=enabled,
                    action=action,
                ),
                True,
            )

        if update_existing:
            self.update_account(
                existing,
                author_id=author.id,
                account_name=account_name,
                profile_url=profile_url,
                follower_count=follower_count,
                verified=verified,
                enabled=enabled,
                action="author_account.update",
            )
        return existing, False

    def update_account(
        self,
        account: AuthorAccount,
        *,
        author_id: uuid.UUID | None = None,
        account_name: str | None = None,
        profile_url: str | None = None,
        follower_count: int | None = None,
        verified: bool | None = None,
        enabled: bool | None = None,
        action: str = "author_account.update",
    ) -> AuthorAccount:
        """更新账号信息（无变化则不写审计，避免噪声）。"""
        before = snapshot_model(account)
        if author_id is not None:
            account.author_id = author_id
        if account_name is not None:
            account.account_name = account_name.strip()
        if profile_url is not None:
            account.profile_url = profile_url.strip()
        if follower_count is not None:
            account.follower_count = follower_count
        if verified is not None:
            account.verified = verified
        if enabled is not None:
            account.enabled = enabled
        self._session.flush()
        after = snapshot_model(account)
        if before != after:
            record_audit(
                self._session,
                action=action,
                entity_type=_ACCOUNT_ENTITY,
                entity_id=account.id,
                before=before,
                after=after,
                user_id=self._user_id,
                ip_address=self._ip_address,
            )
        return account

    def set_enabled(
        self, account: AuthorAccount, enabled: bool, *, action: str = "author_account.enable_change"
    ) -> AuthorAccount:
        """启用 / 停用账号（停用后不应再被采集）。"""
        if account.enabled == enabled:
            return account
        return self.update_account(account, enabled=enabled, action=action)

    def touch_last_collected_at(
        self, account: AuthorAccount, *, collected_at: datetime
    ) -> AuthorAccount:
        """记录最近采集时间（**采集性写入，不写审计**，见模块说明）。"""
        account.last_collected_at = collected_at
        self._session.flush()
        return account