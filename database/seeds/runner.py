"""幂等种子执行器（instruments / sources）。

幂等策略（Phase 1 最高优先级之一：重复执行不得产生重复数据）：
- ``instruments`` 的自然键 = ``symbol``（数据库唯一约束兜底）
- ``sources`` 的自然键 = ``name``（数据库唯一约束兜底）
- 已存在的记录**一律不修改**：种子的职责是"补齐必备基础数据"，而不是覆盖运维配置
  （``enabled`` / ``config_json`` 可能已被人工调整）。若确需刷新定义，必须通过
  显式的新脚本或 Alembic migration，禁止静默覆盖（06_Cline开发规则 第 9 条）。

⚠️ 环境说明：本地开发 / 单元测试可使用 SQLite（``sqlite+pysqlite:///...``，
   仅用于降低本地依赖），但 **生产与研究环境必须使用 PostgreSQL**：
   JSONB / UUID / TIMESTAMPTZ 原生类型、并发写入与后续分区能力都依赖 PostgreSQL。

前置条件：目标库结构必须已由 Alembic 迁移创建（``alembic upgrade head``），
        本模块只写数据、不建表（03_数据库完整设计 第 19 节）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import Instrument, Source
from database.seeds.authors import (
    AUTHOR_SEEDS,
    AuthorSeed,
    import_author_seeds,
    import_authors_from_csv,
)
from database.seeds.instruments import INSTRUMENT_SEEDS, InstrumentSeed
from database.seeds.sources import SOURCE_SEEDS, SourceSeed
from database.session import build_engine, build_session_factory
from src.common.exceptions import SeedError

__all__ = [
    "SeedResult",
    "seed_all",
    "seed_authors",
    "seed_database",
    "seed_instruments",
    "seed_sources",
]

_REQUIRED_TABLES = ("instruments", "sources", "authors")


@dataclass(frozen=True, slots=True)
class SeedResult:
    """一次种子的执行结果（可打印、可测试、可写审计）。"""

    entity: str
    created: tuple[str, ...]
    existing: tuple[str, ...]

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def existing_count(self) -> int:
        return len(self.existing)

    def summary(self) -> str:
        return f"{self.entity}: 新增 {self.created_count} 条，已存在 {self.existing_count} 条"


def seed_instruments(
    session: Session, seeds: Sequence[InstrumentSeed] = INSTRUMENT_SEEDS
) -> SeedResult:
    """写入标的白名单（insert-if-absent，字段严格对齐 04 §12）。"""
    existing_symbols = set(session.scalars(sa.select(Instrument.symbol)).all())
    created: list[str] = []

    for definition in seeds:
        if definition.symbol in existing_symbols:
            continue
        session.add(
            Instrument(
                symbol=definition.symbol,
                name=definition.name,
                asset_class=definition.asset_class,
                quote_currency=definition.quote_currency,
                timezone=definition.timezone,
                enabled=definition.enabled,
            )
        )
        created.append(definition.symbol)

    session.flush()
    defined_order = [definition.symbol for definition in seeds]
    return SeedResult(
        entity="instruments",
        created=tuple(created),
        existing=tuple(s for s in defined_order if s in existing_symbols),
    )


def seed_sources(session: Session, seeds: Sequence[SourceSeed] = SOURCE_SEEDS) -> SeedResult:
    """写入基础来源配置（insert-if-absent，字段严格对齐 04 §2）。"""
    existing_names = set(session.scalars(sa.select(Source.name)).all())
    created: list[str] = []

    for definition in seeds:
        if definition.name in existing_names:
            continue
        session.add(
            Source(
                name=definition.name,
                source_type=definition.source_type,
                base_url=definition.base_url,
                timezone=definition.timezone,
                enabled=definition.enabled,
                config_json=dict(definition.config_json),
            )
        )
        created.append(definition.name)

    session.flush()
    defined_order = [definition.name for definition in seeds]
    return SeedResult(
        entity="sources",
        created=tuple(created),
        existing=tuple(s for s in defined_order if s in existing_names),
    )


def seed_authors(session: Session, seeds: Sequence[AuthorSeed] = AUTHOR_SEEDS) -> SeedResult:
    """写入作者库（作者 + 平台账号；自然键幂等，已存在不覆盖）。

    与 instruments / sources 种子的区别：作者种子会**连带写入平台账号**，
    因此单独返回 ``SeedResult``（authors 维度），账号结果通过审计与报告查看。

    Raises:
        SeedError: 种子定义本身有问题（来源不存在等）——种子是受控定义，必须立刻失败，
            而不是把问题行悄悄跳过。
    """
    report = import_author_seeds(session, seeds, source_label="builtin-seeds")
    if report.problems:
        raise SeedError("作者种子存在问题：" + "；".join(report.problems))
    return SeedResult(
        entity="authors",
        created=tuple(report.created_authors),
        existing=tuple(report.existing_authors),
    )


def seed_all(session: Session) -> dict[str, SeedResult]:
    """一次性写入全部 Phase 1 基础数据（标的 / 来源 / 作者）。"""
    return {
        "instruments": seed_instruments(session),
        "sources": seed_sources(session),
        "authors": seed_authors(session),
    }


def seed_database(
    database_url: str | None = None,
    *,
    scope: str = "all",
    dry_run: bool = False,
    authors_csv: str | Path | None = None,
) -> dict[str, SeedResult]:
    """按数据库 URL 执行种子（自带事务与前置校验）。

    Args:
        database_url: 目标库 URL；``None`` 表示使用配置 ``DATABASE_URL``。
        scope: ``all`` / ``instruments`` / ``sources`` / ``authors``。
        dry_run: ``True`` 时执行后回滚（演练用，不落库）。
        authors_csv: 指定后，作者部分改为**从该 CSV 导入**（团队裁决：先用本地 CSV 过渡）。

    Returns:
        ``entity -> SeedResult``。

    Raises:
        SeedError: 目标库缺少表结构（需先执行 ``alembic upgrade head``）；
            CSV 导入存在问题行（此时**整批回滚**，修好 CSV 再来）。
        ValueError: ``scope`` 取值非法。
    """
    if scope not in {"all", "instruments", "sources", "authors"}:
        raise ValueError(
            f"scope 只能是 all / instruments / sources / authors，收到：{scope!r}"
        )

    engine = build_engine(database_url)
    try:
        _assert_schema_ready(engine)
        factory = build_session_factory(engine)
        session = factory()
        try:
            results: dict[str, SeedResult] = {}
            if scope in {"all", "instruments"}:
                results["instruments"] = seed_instruments(session)
            if scope in {"all", "sources"}:
                results["sources"] = seed_sources(session)
            if scope in {"all", "authors"}:
                if authors_csv is not None:
                    csv_report = import_authors_from_csv(
                        session, Path(authors_csv), dry_run=dry_run
                    )
                    if csv_report.problems:
                        raise SeedError(
                            f"作者 CSV 导入存在 {len(csv_report.problems)} 个问题"
                            "（整批回滚，未落库）：" + "；".join(csv_report.problems)
                        )
                    results["authors"] = SeedResult(
                        entity="authors",
                        created=tuple(csv_report.created_authors),
                        existing=tuple(csv_report.existing_authors),
                    )
                else:
                    results["authors"] = seed_authors(session)

            if dry_run:
                session.rollback()
            else:
                session.commit()
            return results
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    finally:
        engine.dispose()


def _assert_schema_ready(engine: sa.Engine) -> None:
    """前置校验：表不存在时给出可执行的修复指引，而不是抛裸 SQL 错误。"""
    inspector = sa.inspect(engine)
    missing = [table for table in _REQUIRED_TABLES if not inspector.has_table(table)]
    if missing:
        raise SeedError(
            f"目标数据库缺少 Phase 1 表：{missing}。请先执行结构迁移："
            "python -m alembic upgrade head（03_数据库完整设计 第 19 节）"
        )
