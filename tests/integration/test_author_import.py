"""作者库种子 / CSV 导入 / CLI 集成测试（05 Phase 2 Step 1 与团队裁决的落地验证）。

覆盖：
- `seed_authors`（内置种子）幂等、连带写入平台账号、来源缺失时立刻失败；
- `import_authors_from_csv`（团队裁决的过渡方案）创建 / 幂等 / 问题上报 / dry-run；
- `seed_database(scope="authors", authors_csv=...)` 与 CLI 退出码。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuditLog, Author, AuthorAccount
from database.models.enums import AuthorStatus, SourceType
from database.seeds import AuthorSeed, seed_authors
from database.seeds.authors import AUTHOR_SEEDS, CSV_COLUMNS, import_authors_from_csv
from database.seeds.runner import seed_database, seed_sources
from database.seeds.sources import SOURCE_SEEDS
from database.session import build_engine
from src.common.exceptions import SeedError

pytestmark = pytest.mark.integration


def _count(session: Session, model: type) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


def _audit_actions(session: Session) -> list[str]:
    return list(session.scalars(sa.select(AuditLog.action).order_by(AuditLog.created_at)).all())


def _write_authors_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _author_row(**overrides: str) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    row.update(
        {
            "display_name": "机构A",
            "canonical_name": "inst-a",
            "status": "ACTIVE",
            "source_name": "jin10_flash",
            "external_account_id": "inst-a-rss",
            "account_name": "机构A-快讯",
            "enabled": "true",
            "verified": "true",
            "follower_count": "50000",
            "metadata_json": '{"kind": "institution"}',
        }
    )
    row.update(overrides)
    return row


def _migrated_url(tmp_path: Path, alembic_config_factory, name: str = "authors.db") -> str:
    from alembic import command

    database_url = f"sqlite+pysqlite:///{(tmp_path / name).as_posix()}"
    command.upgrade(alembic_config_factory(database_url), "head")
    return database_url


# ---------------------------------------------------------------------------
# 1) 内置种子
# ---------------------------------------------------------------------------
def test_seed_authors_creates_authors_accounts_and_audit(session: Session) -> None:
    seed_sources(session)

    result = seed_authors(session)

    assert result.entity == "authors"
    assert result.created_count == 3
    assert result.existing_count == 0
    assert _count(session, Author) == 3
    assert _count(session, AuthorAccount) == 3

    actions = _audit_actions(session)
    assert actions.count("author.import_create") == 3
    assert actions.count("author_account.import_create") == 3
    assert actions.count("authors.import") == 1  # 本次导入汇总

    demo = session.scalar(
        sa.select(AuthorAccount).where(AuthorAccount.external_account_id == "analyst-demo-rss")
    )
    assert demo is not None
    assert demo.enabled is False  # 示例分析师账号默认停用
    assert demo.author.status is AuthorStatus.EXPLORATION


def test_seed_authors_is_idempotent(session: Session) -> None:
    seed_sources(session)
    seed_authors(session)
    audit_before = _audit_actions(session)

    again = seed_authors(session)

    assert again.created_count == 0
    assert again.existing_count == 3
    assert _count(session, Author) == 3
    assert _count(session, AuthorAccount) == 3
    # 二次执行只新增"导入汇总"一条审计（没有任何实体被修改）
    assert _audit_actions(session)[len(audit_before) :] == ["authors.import"]


def test_seed_authors_never_uses_weibo_source() -> None:
    """团队裁决：不采集微博 —— 内置作者不得挂在 WEIBO 来源上。"""
    weibo_sources = {seed.name for seed in SOURCE_SEEDS if seed.source_type is SourceType.WEIBO}
    assert {seed.source_name for seed in AUTHOR_SEEDS}.isdisjoint(weibo_sources)


def test_seed_authors_fails_when_source_is_missing(session: Session) -> None:
    """种子定义受控：来源不存在必须立刻失败（而不是把问题行悄悄跳过）。"""
    seeds = (
        AuthorSeed(
            display_name="孤儿作者",
            canonical_name="orphan",
            source_name="no-such-source",
            external_account_id="orphan-rss",
        ),
    )

    with pytest.raises(SeedError, match="no-such-source"):
        seed_authors(session, seeds)

    assert _count(session, Author) == 0  # 未落库（调用方回滚）


# ---------------------------------------------------------------------------
# 2) CSV 导入
# ---------------------------------------------------------------------------
def test_import_authors_from_csv_creates_authors_and_accounts(
    session: Session, tmp_path: Path
) -> None:
    seed_sources(session)
    path = _write_authors_csv(
        tmp_path / "authors.csv",
        [
            _author_row(),
            _author_row(
                display_name="分析师B",
                canonical_name="analyst-b",
                external_account_id="analyst-b-rss",
                status="PROBATION",
                source_name="sina_finance_gold",
                enabled="false",
            ),
        ],
    )

    report = import_authors_from_csv(session, path)
    session.flush()

    assert report.created_author_count == 2
    assert report.created_account_count == 2
    assert report.problems == []

    analyst_b = session.scalar(sa.select(Author).where(Author.canonical_name == "analyst-b"))
    assert analyst_b is not None
    assert analyst_b.status is AuthorStatus.PROBATION
    account = session.scalar(
        sa.select(AuthorAccount).where(AuthorAccount.external_account_id == "analyst-b-rss")
    )
    assert account is not None
    assert account.author_id == analyst_b.id
    assert account.enabled is False


def test_import_authors_from_csv_is_idempotent(session: Session, tmp_path: Path) -> None:
    seed_sources(session)
    path = _write_authors_csv(tmp_path / "authors.csv", [_author_row()])

    first = import_authors_from_csv(session, path)
    second = import_authors_from_csv(session, path)

    assert first.created_author_count == 1
    assert second.created_author_count == 0
    assert second.existing_author_count == 1
    assert second.existing_account_count == 1
    assert _count(session, Author) == 1
    assert _count(session, AuthorAccount) == 1


def test_import_authors_from_csv_reports_problems_and_keeps_valid_rows(
    session: Session, tmp_path: Path
) -> None:
    seed_sources(session)
    path = _write_authors_csv(
        tmp_path / "authors.csv",
        [
            _author_row(),
            _author_row(
                display_name="孤儿来源作者",
                canonical_name="orphan-source",
                source_name="no-such-source",
                external_account_id="orphan-rss",
            ),
            _author_row(status="VIP", external_account_id="bad-status"),
        ],
    )

    report = import_authors_from_csv(session, path)

    assert report.created_author_count == 1
    assert len(report.problems) == 2
    assert any("no-such-source" in item for item in report.problems)
    assert any("status" in item for item in report.problems)
    # 合法行照常落库：问题行不阻断其它行（但必须上报）
    assert _count(session, Author) == 1


def test_import_authors_from_csv_dry_run_writes_nothing(session: Session, tmp_path: Path) -> None:
    seed_sources(session)
    path = _write_authors_csv(tmp_path / "authors.csv", [_author_row()])

    report = import_authors_from_csv(session, path, dry_run=True)

    assert report.dry_run is True
    assert report.created_author_count == 1  # 报告仍给出"将会创建"的数量
    assert _count(session, Author) == 0
    assert _count(session, AuthorAccount) == 0
    assert "authors.import" not in _audit_actions(session)  # 演练不留审计


# ---------------------------------------------------------------------------
# 3) seed_database / CLI
# ---------------------------------------------------------------------------
def test_seed_database_scope_authors_requires_sources_first(
    tmp_path: Path, alembic_config_factory
) -> None:
    """`--scope authors` 依赖 sources：缺来源时给出可执行指引（不静默失败）。"""
    database_url = _migrated_url(tmp_path, alembic_config_factory, "authors-only.db")

    with pytest.raises(SeedError, match="scope sources"):
        seed_database(database_url, scope="authors")


def test_seed_database_scope_authors_after_sources(tmp_path: Path, alembic_config_factory) -> None:
    database_url = _migrated_url(tmp_path, alembic_config_factory, "authors-e2e.db")
    seed_database(database_url, scope="sources")

    results = seed_database(database_url, scope="authors")

    assert results["authors"].created_count == 3
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            authors = connection.execute(sa.text("SELECT COUNT(*) FROM authors")).scalar_one()
            accounts = connection.execute(
                sa.text("SELECT COUNT(*) FROM author_accounts")
            ).scalar_one()
            audits = connection.execute(sa.text("SELECT COUNT(*) FROM audit_logs")).scalar_one()
        assert (authors, accounts) == (3, 3)
        assert audits >= 7  # 3 作者 + 3 账号 + 1 汇总
    finally:
        engine.dispose()


def test_seed_database_with_authors_csv(tmp_path: Path, alembic_config_factory) -> None:
    database_url = _migrated_url(tmp_path, alembic_config_factory, "authors-csv.db")
    seed_database(database_url, scope="sources")
    path = _write_authors_csv(tmp_path / "authors.csv", [_author_row()])

    results = seed_database(database_url, scope="authors", authors_csv=str(path))

    assert results["authors"].created_count == 1
    assert results["authors"].created == ("inst-a",)


def test_seed_database_with_problematic_csv_rolls_back_everything(
    tmp_path: Path, alembic_config_factory
) -> None:
    """CSV 有问题 → 整批回滚（不落半成品），并给出问题清单。"""
    database_url = _migrated_url(tmp_path, alembic_config_factory, "authors-bad-csv.db")
    seed_database(database_url, scope="sources")
    path = _write_authors_csv(
        tmp_path / "authors.csv",
        [_author_row(), _author_row(status="VIP", external_account_id="bad-1")],
    )

    with pytest.raises(SeedError, match="问题"):
        seed_database(database_url, scope="authors", authors_csv=str(path))

    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            authors = connection.execute(sa.text("SELECT COUNT(*) FROM authors")).scalar_one()
        assert authors == 0
    finally:
        engine.dispose()


def test_cli_scope_authors_after_sources(
    tmp_path: Path, capsys, alembic_config_factory
) -> None:
    from database.seeds.__main__ import main

    database_url = _migrated_url(tmp_path, alembic_config_factory, "cli-authors.db")
    seed_database(database_url, scope="sources")

    exit_code = main(["--db-url", database_url, "--scope", "authors"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "authors: 新增 3" in captured.out
    assert "audit_logs" in captured.out


def test_cli_authors_csv_import(tmp_path: Path, capsys, alembic_config_factory) -> None:
    from database.seeds.__main__ import main

    database_url = _migrated_url(tmp_path, alembic_config_factory, "cli-authors-csv.db")
    seed_database(database_url, scope="sources")
    path = _write_authors_csv(tmp_path / "authors.csv", [_author_row()])

    exit_code = main(
        ["--db-url", database_url, "--scope", "authors", "--authors-csv", str(path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "作者来源：本地 CSV" in captured.out
    assert "authors: 新增 1" in captured.out


def test_cli_authors_csv_requires_author_scope(tmp_path: Path, capsys) -> None:
    from database.seeds.__main__ import main

    exit_code = main(["--scope", "instruments", "--authors-csv", "whatever.csv"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "参数错误" in captured.err


def test_cli_authors_csv_with_problems_returns_failure(
    tmp_path: Path, capsys, alembic_config_factory
) -> None:
    from database.seeds.__main__ import main

    database_url = _migrated_url(tmp_path, alembic_config_factory, "cli-bad-csv.db")
    seed_database(database_url, scope="sources")
    path = _write_authors_csv(
        tmp_path / "authors.csv", [_author_row(status="VIP", external_account_id="bad-cli")]
    )

    exit_code = main(
        ["--db-url", database_url, "--scope", "authors", "--authors-csv", str(path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "[seeds] 失败" in captured.err
    assert "status" in captured.err