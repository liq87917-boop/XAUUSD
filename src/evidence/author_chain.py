"""Evidence Gateway → 作者归属链（GOLD-007）。

**唯一合法来源**：本模块只消费 `raw_json` 里带 ``evidence-intake-v1`` 证据块、
``scope=author``、``oos_eligible=true`` 且作者身份两列齐全的 ``raw_items``
（即已经过 Evidence Gateway 机械校验并有独立历史可用证据的记录）。

为什么必须这样限定（对应任务要求"不得从普通 CSV 或历史样本绕过 gateway"）：

- ``scripts/import_real_posts.py`` 读取的是**普通 CSV**，其 ``raw_json`` 只写
  ``import_kind=manual_real_corpus``，**没有**证据块。因此历史 CSV / 普通导入 /
  Mock 样本在此恒被判为非候选（:func:`gateway_author_evidence` 返回 ``None``），
  **无法**进入作者归属链，也就无法影响 Phase 3.3 资格；
- 本模块**不做**任何抓取、不做 robots 探测、不联网：只读库内既有 ``raw_items``；
- 本模块**不启用采集**：新建的 ``author_accounts`` 一律 ``enabled=false``
  （只建立作者归属，不注册采集器）；账号启用仍需人工 Gate；
- 身份冲突（同一 ``(source, external_account_id)`` 已归属**其他**作者）一律**跳过并上报**，
  绝不静默覆盖历史归属。

调用约定：默认 ``dry_run=True``（嵌套 savepoint + 回滚，零写入）；
显式 ``dry_run=False`` 才 append-only 落 ``authors`` / ``author_accounts`` / ``author_posts``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorPost, RawItem
from database.models.enums import RawItemType
from database.repositories.authors import AuthorAccountRepository, AuthorRepository
from src.common.hashing import content_hash
from src.common.redaction import safe_text
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.processors.timeline import ensure_utc_from_database

__all__ = [
    "AUTHOR_CHAIN_NOTE",
    "AUTHOR_CHAIN_SCHEMA_VERSION",
    "AuthorChainReport",
    "GatewayAuthorEvidence",
    "attribute_gateway_author_evidence",
    "gateway_author_evidence",
    "load_gateway_author_evidence",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
AUTHOR_CHAIN_SCHEMA_VERSION: Final[int] = 1
#: 固定说明（每次输出都带上）
AUTHOR_CHAIN_NOTE: Final[str] = (
    "作者归属链只消费**已通过 Evidence Gateway** 的 Author 证据（`evidence-intake-v1` + "
    "`scope=author` + `oos_eligible=true`）；普通 CSV / 历史样本 / Mock 没有证据块，"
    "恒不是候选，无法绕过 gateway。本链只建立归属，不启用采集、不训练 Alpha，"
    "也不解除 PHASE3_3_DATA。"
)


@dataclass(frozen=True, slots=True)
class GatewayAuthorEvidence:
    """从证据块解析出的**作者归属必要信息**（全部经脱敏）。"""

    source: str
    source_record_id: str
    fingerprint: str | None
    author_name: str
    external_account_id: str
    available_at: str | None


def gateway_author_evidence(raw_json: Mapping[str, Any] | None) -> GatewayAuthorEvidence | None:
    """纯函数：``raw_json`` 是否为**可进入作者链**的 gateway Author 证据。

    只有同时满足以下条件才返回信息，否则返回 ``None``（不猜、不放宽）：

    1. 存在 ``evidence`` 映射，且 ``contract_version == evidence-intake-v1``；
    2. ``scope == author``；
    3. ``oos_eligible is True``（具备独立历史可用证据）；
    4. ``source`` / ``source_record_id`` / ``author_name`` / ``external_account_id`` 均非空。
    """
    if not isinstance(raw_json, Mapping):
        return None
    evidence = raw_json.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    if str(evidence.get("contract_version") or "").strip() != EVIDENCE_CONTRACT_VERSION:
        return None
    if str(evidence.get("scope") or "").strip().lower() != EvidenceScope.AUTHOR.value:
        return None
    if evidence.get("oos_eligible") is not True:
        return None
    source = safe_text(str(evidence.get("source") or "").strip(), max_chars=100)
    record_id = safe_text(str(evidence.get("source_record_id") or "").strip(), max_chars=300)
    author_name = safe_text(str(evidence.get("author_name") or "").strip(), max_chars=200)
    account_id = safe_text(str(evidence.get("external_account_id") or "").strip(), max_chars=200)
    if not (source and record_id and author_name and account_id):
        return None
    available = evidence.get("available_at")
    fingerprint = str(evidence.get("fingerprint") or "").strip()
    return GatewayAuthorEvidence(
        source=source,
        source_record_id=record_id,
        fingerprint=fingerprint or None,
        author_name=author_name,
        external_account_id=account_id,
        available_at=str(available) if available else None,
    )


def _canonical_name(source: str, external_account_id: str) -> str:
    """作者幂等自然键（``authors.canonical_name`` 上限 200；超长时退化为稳定哈希）。"""
    label = f"gateway:{source}:{external_account_id}"
    if len(label) <= 200:
        return label
    digest = content_hash(source, external_account_id, namespace="evidence.author_chain")
    return f"gateway:{digest[:32]}"


@dataclass(frozen=True, slots=True)
class AuthorChainReport:
    """一次作者归属（gateway-only）的可审计结论（默认 dry-run）。"""

    as_of: datetime
    dry_run: bool
    candidates: int
    attributed: int
    duplicate: int
    skipped: int
    identity_conflicts: int
    authors_created: int
    accounts_created: int
    problems: tuple[str, ...]
    notes: tuple[str, ...]
    scope: str = EvidenceScope.AUTHOR.value
    schema_version: int = AUTHOR_CHAIN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "as_of": self.as_of.isoformat(),
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "attributed": self.attributed,
            "duplicate": self.duplicate,
            "skipped": self.skipped,
            "identity_conflicts": self.identity_conflicts,
            "authors_created": self.authors_created,
            "accounts_created": self.accounts_created,
            "problems": [safe_text(problem, max_chars=300) for problem in self.problems],
            "notes": [safe_text(note, max_chars=300) for note in self.notes],
        }

    def render(self) -> str:
        """渲染人类可读 Markdown（脱敏；不解除 blocker）。"""
        mode = "dry-run（零写入）" if self.dry_run else "提交（append-only 写入作者链）"
        lines = [
            "### 作者归属链（gateway-only）",
            "",
            f"- 模式：{mode}",
            f"- 审计时点（UTC）：{self.as_of.isoformat()}",
            f"- 候选（gateway Author 证据）：{self.candidates}；"
            f"新归属：{self.attributed}；已归属（幂等）：{self.duplicate}；"
            f"跳过：{self.skipped}；身份冲突：{self.identity_conflicts}",
            f"- 新建作者：{self.authors_created}；新建账号：{self.accounts_created}",
        ]
        if self.problems:
            lines.append("")
            lines.append("问题清单（已脱敏）：")
            lines += [f"- {safe_text(problem, max_chars=300)}" for problem in self.problems]
        lines += ["", *[f"- {safe_text(note, max_chars=300)}" for note in self.notes], ""]
        return "\n".join(lines)


def load_gateway_author_evidence(session: Session) -> tuple[RawItem, ...]:
    """只读：挑出库内**已经过 Evidence Gateway 且有 OOS 资格**的 Author 记录。

    **不做**任何抓取；普通导入 / 历史 CSV / Mock 的 ``raw_json`` 没有证据块，
    因此在 :func:`gateway_author_evidence` 处被过滤掉（恒非候选）。
    """
    rows = session.scalars(
        sa.select(RawItem)
        .where(RawItem.item_type == RawItemType.POST)
        .order_by(RawItem.effective_at, RawItem.source_record_id)
    ).all()
    return tuple(raw for raw in rows if gateway_author_evidence(raw.raw_json) is not None)


def _as_utc(value: datetime, field_name: str) -> datetime:
    """数据库读回时间统一为 UTC-aware（SQLite 不保存偏移；语义已知为 UTC）。"""
    return ensure_utc_from_database(value, field_name=field_name)


def attribute_gateway_author_evidence(
    session: Session, *, as_of: datetime, dry_run: bool = True
) -> AuthorChainReport:
    """把 gateway Author 证据归属到 ``authors`` / ``author_accounts`` / ``author_posts``。

    Args:
        session: 数据库会话。
        as_of: 审计时点（必须带时区，保证可复现）。
        dry_run: ``True``（默认）只演练并回滚；``False`` 才 append-only 落库。

    Raises:
        ValueError: ``as_of`` 未带时区。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    moment = as_of.astimezone(UTC)
    candidates = load_gateway_author_evidence(session)
    author_repo = AuthorRepository(session)
    account_repo = AuthorAccountRepository(session)
    savepoint = session.begin_nested() if dry_run else None
    attributed = duplicate = skipped = conflicts = authors_created = accounts_created = 0
    problems: list[str] = []
    try:
        for raw in candidates:
            parsed = gateway_author_evidence(raw.raw_json)
            if parsed is None:  # pragma: no cover - load 已过滤；防御性分支
                skipped += 1
                continue
            already = session.scalar(
                sa.select(AuthorPost.id).where(AuthorPost.raw_item_id == raw.id).limit(1)
            )
            if already is not None:
                duplicate += 1
                continue
            source = account_repo.resolve_source(parsed.source)
            if source is None:
                problems.append(
                    f"来源 {parsed.source} 不在 sources 表（应由证据入口创建）；已跳过"
                )
                skipped += 1
                continue
            account = account_repo.get_by_external_id(
                source_id=source.id, external_account_id=parsed.external_account_id
            )
            if account is not None:
                existing_author = author_repo.get(account.author_id)
                if existing_author is None or existing_author.display_name != parsed.author_name:
                    problems.append(
                        f"账号 {parsed.external_account_id} 已归属其他作者：身份冲突，"
                        "已跳过且不覆盖历史归属"
                    )
                    conflicts += 1
                    skipped += 1
                    continue
                author = existing_author
            else:
                author, author_created = author_repo.ensure(
                    display_name=parsed.author_name,
                    canonical_name=_canonical_name(parsed.source, parsed.external_account_id),
                    metadata_json={
                        "kind": "gateway_author_evidence",
                        "contract_version": EVIDENCE_CONTRACT_VERSION,
                    },
                    action="author.ensure_gateway_evidence",
                )
                authors_created += int(author_created)
                account, account_created = account_repo.upsert(
                    author=author,
                    source=source,
                    external_account_id=parsed.external_account_id,
                    account_name=parsed.author_name,
                    verified=False,
                    enabled=False,  # 只建立归属，不启用采集
                    action="author_account.upsert_gateway_evidence",
                )
                accounts_created += int(account_created)
            session.add(
                AuthorPost(
                    author_id=author.id,
                    author_account_id=account.id,
                    raw_item_id=raw.id,
                    published_at=(
                        _as_utc(raw.published_at, "published_at") if raw.published_at else None
                    ),
                    collected_at=_as_utc(raw.collected_at, "collected_at"),
                    effective_at=_as_utc(raw.effective_at, "effective_at"),
                    text_content=raw.content_text,
                    has_media=False,
                )
            )
            session.flush()
            attributed += 1
    finally:
        if savepoint is not None:
            savepoint.rollback()
    notes = (
        AUTHOR_CHAIN_NOTE,
        "默认 dry-run：不写 authors / author_accounts / author_posts；"
        "只有显式 --no-dry-run 才 append-only 落库。",
        "新建 author_accounts 一律 enabled=false（不注册采集器）；账号启用仍需人工 Gate。",
        "同一 raw_item 只归属一次（author_posts.raw_item_id 唯一）；重复运行为幂等。",
    )
    return AuthorChainReport(
        as_of=moment,
        dry_run=dry_run,
        candidates=len(candidates),
        attributed=attributed,
        duplicate=duplicate,
        skipped=skipped,
        identity_conflicts=conflicts,
        authors_created=authors_created,
        accounts_created=accounts_created,
        problems=tuple(problems),
        notes=notes,
    )
