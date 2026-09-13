"""作者库种子与 CSV 导入（04 §3 authors / §4 author_accounts）。

团队裁决（2026-09）：Phase 2 的作者来源先用「机构 / 分析师 RSS 账号」或「本地 CSV 模拟导入」，
**不采集微博**。因此本模块提供两条路径：

1. ``AUTHOR_SEEDS``：内置少量作者定义（本地演练与测试用），账号挂在 **NEWS 来源**
   （如 ``jin10_flash`` / ``sina_finance_gold``）上，不涉及任何微博抓取；
2. ``import_authors_from_csv``：从本地 CSV 批量导入首期 50~100 位作者（团队裁决的过渡方案）。

幂等口径（与 instruments / sources 种子一致）：
    自然键 = ``authors.canonical_name``（缺失时回退 ``display_name``）
             + ``author_accounts(source_id, external_account_id)``；
    **已存在记录不覆盖**（避免冲掉运维人工调整），除非显式 ``update_existing=True``。

审计：所有管理性写入都会写 ``audit_logs``（口径见 ``database/repositories/authors.py``）；
一次导入/种子额外写一条 ``authors.import`` 汇总审计（记录来源、条数与问题清单）。

本模块**不 commit**：事务边界由调用方（CLI / service）决定。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from database.models.enums import AuthorStatus
from database.repositories.audit import record_audit
from database.repositories.authors import AuthorAccountRepository, AuthorRepository

__all__ = [
    "AUTHOR_SEEDS",
    "CSV_COLUMNS",
    "AuthorImportReport",
    "AuthorSeed",
    "import_author_seeds",
    "import_authors_from_csv",
    "load_author_seeds_from_csv",
]

#: CSV 列（多余列忽略；**缺失必填列 → 该行记 problem，不静默跳过**）
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "display_name",
    "canonical_name",
    "status",
    "source_name",
    "external_account_id",
    "account_name",
    "enabled",
    "verified",
    "follower_count",
    "profile_url",
    "metadata_json",
)

#: CSV 必填列
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("display_name", "source_name", "external_account_id")

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on"})


@dataclass(frozen=True, slots=True)
class AuthorSeed:
    """一位作者 + 一个平台账号的定义（04 §3 / §4）。"""

    display_name: str
    source_name: str
    external_account_id: str
    canonical_name: str | None = None
    status: AuthorStatus = AuthorStatus.ACTIVE
    account_name: str | None = None
    enabled: bool = True
    verified: bool = False
    follower_count: int | None = None
    profile_url: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)


#: 内置作者定义（**不涉及微博**；账号挂在 NEWS 来源上，见团队裁决）
AUTHOR_SEEDS: Final[tuple[AuthorSeed, ...]] = (
    AuthorSeed(
        display_name="金十数据快讯",
        canonical_name="jin10-news",
        source_name="jin10_flash",
        external_account_id="jin10-rss",
        account_name="金十数据",
        verified=True,
        metadata_json={
            "kind": "institution",
            "note": "机构快讯账号（RSS）；团队裁决：不采集微博",
        },
    ),
    AuthorSeed(
        display_name="新浪财经黄金频道",
        canonical_name="sina-gold",
        source_name="sina_finance_gold",
        external_account_id="sina-gold-rss",
        account_name="新浪财经-黄金",
        verified=True,
        metadata_json={"kind": "media", "note": "媒体频道账号（RSS）"},
    ),
    AuthorSeed(
        display_name="示例分析师（待人工确认）",
        canonical_name="analyst-demo",
        source_name="jin10_flash",
        external_account_id="analyst-demo-rss",
        status=AuthorStatus.EXPLORATION,
        account_name="示例分析师",
        enabled=False,
        metadata_json={
            "kind": "analyst",
            "note": "CSV 导入示例；账户默认停用，等待人工确认后再启用",
        },
    ),
)


@dataclass(slots=True)
class AuthorImportReport:
    """一次作者导入/种子的结果（可打印、可测试、可写审计）。"""

    source_label: str
    created_authors: list[str] = field(default_factory=list)
    existing_authors: list[str] = field(default_factory=list)
    created_accounts: list[str] = field(default_factory=list)
    existing_accounts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def created_author_count(self) -> int:
        return len(self.created_authors)

    @property
    def existing_author_count(self) -> int:
        return len(self.existing_authors)

    @property
    def created_account_count(self) -> int:
        return len(self.created_accounts)

    @property
    def existing_account_count(self) -> int:
        return len(self.existing_accounts)

    def summary(self) -> str:
        mode = "（DRY-RUN，未落库）" if self.dry_run else ""
        return (
            f"{self.source_label}{mode}：作者新增 {self.created_author_count}、已存在 "
            f"{self.existing_author_count}；账号新增 {self.created_account_count}、已存在 "
            f"{self.existing_account_count}；问题 {len(self.problems)}"
        )


# ---------------------------------------------------------------------------
# CSV 解析（纯函数：可脱离数据库做单元测试）
# ---------------------------------------------------------------------------
def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def _parse_follower_count(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    return int(raw.strip())


def _parse_metadata_json(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not raw.strip():
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("metadata_json 必须是 JSON 对象")
    return parsed


def load_author_seeds_from_csv(path: Path) -> tuple[list[AuthorSeed], list[str]]:
    """解析作者 CSV → ``AuthorSeed`` 列表 + 问题清单。

    Returns:
        ``(seeds, problems)``。缺必填列直接抛 ``ValueError``（配置类错误立刻暴露）；
        行级问题（空值 / 非法枚举 / 非法 JSON / 非法整数）记入 ``problems`` 并跳过该行。
    """
    if not path.exists():
        raise FileNotFoundError(f"作者 CSV 不存在：{path}")

    seeds: list[AuthorSeed] = []
    problems: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = [column for column in REQUIRED_COLUMNS if column not in fields]
        if missing:
            raise ValueError(f"作者 CSV 缺少必填列 {missing}；必需列：{list(REQUIRED_COLUMNS)}")

        for row_number, row in enumerate(reader, start=2):
            prefix = f"第 {row_number} 行"
            display_name = (row.get("display_name") or "").strip()
            source_name = (row.get("source_name") or "").strip()
            external_id = (row.get("external_account_id") or "").strip()
            if not display_name or not source_name or not external_id:
                problems.append(
                    f"{prefix}：display_name / source_name / external_account_id 均不能为空，已跳过"
                )
                continue

            status_raw = (row.get("status") or "").strip().upper()
            try:
                status = AuthorStatus(status_raw) if status_raw else AuthorStatus.ACTIVE
            except ValueError:
                allowed = [member.value for member in AuthorStatus]
                problems.append(f"{prefix}：status={status_raw!r} 非法（允许 {allowed}），已跳过")
                continue

            try:
                follower_count = _parse_follower_count(row.get("follower_count"))
                metadata_json = _parse_metadata_json(row.get("metadata_json"))
            except (ValueError, TypeError) as exc:
                problems.append(f"{prefix}：字段解析失败（{exc}），已跳过")
                continue

            seeds.append(
                AuthorSeed(
                    display_name=display_name,
                    canonical_name=(row.get("canonical_name") or "").strip() or None,
                    status=status,
                    source_name=source_name,
                    external_account_id=external_id,
                    account_name=(row.get("account_name") or "").strip() or None,
                    enabled=_parse_bool(row.get("enabled"), default=True),
                    verified=_parse_bool(row.get("verified"), default=False),
                    follower_count=follower_count,
                    profile_url=(row.get("profile_url") or "").strip() or None,
                    metadata_json=metadata_json or {},
                )
            )
    return seeds, problems


# ---------------------------------------------------------------------------
# 写入（幂等 + 审计）
# ---------------------------------------------------------------------------
def import_author_seeds(
    session: Session,
    seeds: Sequence[AuthorSeed],
    *,
    source_label: str,
    update_existing: bool = False,
    dry_run: bool = False,
) -> AuthorImportReport:
    """把作者定义幂等写入库（作者 + 平台账号），并写审计。

    Args:
        session: 当前事务的 Session（本函数**不 commit**）。
        seeds: 作者定义（内置种子或 CSV 解析结果）。
        source_label: 审计与报告中的来源标识（如 ``csv:authors.csv`` / ``builtin-seeds``）。
        update_existing: 命中既有账号时是否刷新可变字段（默认 ``False``：不覆盖运维配置）。
        dry_run: ``True`` 时用 savepoint 执行后回滚（演练：不落库、不留审计）。

    Returns:
        :class:`AuthorImportReport`（含 ``problems``，绝不静默跳过问题行）。
    """
    author_repo = AuthorRepository(session)
    account_repo = AuthorAccountRepository(session)
    report = AuthorImportReport(source_label=source_label, dry_run=dry_run)
    savepoint = session.begin_nested() if dry_run else None

    try:
        for definition in seeds:
            label = definition.canonical_name or definition.display_name

            # 先解析来源：作者 + 平台账号是一个导入单元，来源缺失时**整行跳过**，
            # 避免留下"没有账号的半个作者"（05 Phase 2 的作者库是作者+账号成对维护）。
            source = account_repo.resolve_source(definition.source_name)
            if source is None:
                report.problems.append(
                    f"{label}：来源 {definition.source_name!r} 不存在于 sources 表"
                    "（请先执行 python -m database.seeds --scope sources）"
                )
                continue

            try:
                author, created = author_repo.ensure(
                    display_name=definition.display_name,
                    canonical_name=definition.canonical_name,
                    status=definition.status,
                    metadata_json=definition.metadata_json or None,
                    action="author.import_create",
                )
            except ValueError as exc:
                report.problems.append(f"{label}：{exc}")
                continue
            (report.created_authors if created else report.existing_authors).append(label)

            _account, account_created = account_repo.upsert(
                author=author,
                source=source,
                external_account_id=definition.external_account_id,
                account_name=definition.account_name,
                profile_url=definition.profile_url,
                follower_count=definition.follower_count,
                verified=definition.verified,
                enabled=definition.enabled,
                update_existing=update_existing,
                action="author_account.import_create",
            )
            account_key = f"{definition.source_name}:{definition.external_account_id}"
            (report.created_accounts if account_created else report.existing_accounts).append(
                account_key
            )

        record_audit(
            session,
            action="authors.import",
            entity_type="authors",
            before=None,
            after={
                "source": source_label,
                "created_authors": report.created_authors,
                "existing_authors": report.existing_authors,
                "created_accounts": report.created_accounts,
                "existing_accounts": report.existing_accounts,
                "problems": report.problems,
                "update_existing": update_existing,
                "dry_run": dry_run,
            },
        )
    finally:
        if savepoint is not None:
            savepoint.rollback()
    return report


def import_authors_from_csv(
    session: Session,
    path: Path,
    *,
    update_existing: bool = False,
    dry_run: bool = False,
) -> AuthorImportReport:
    """从 CSV 导入作者库（解析问题与写入问题合并上报）。"""
    seeds, parse_problems = load_author_seeds_from_csv(path)
    report = import_author_seeds(
        session,
        seeds,
        source_label=f"csv:{path}",
        update_existing=update_existing,
        dry_run=dry_run,
    )
    report.problems = [*parse_problems, *report.problems]
    return report