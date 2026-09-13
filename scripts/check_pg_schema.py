"""PostgreSQL 结构校验脚本（CI / 手工验收）。

为什么需要它：
    "生产 / 研究环境必须使用 PostgreSQL" 是本项目的强制约束
    （见 database/types.py 与 database/session.py 顶部说明）。SQLite 测试无法证明
    JSONB / 原生 UUID / TIMESTAMPTZ 真的建成了原生类型，因此本脚本在真实
    PostgreSQL 上做三项断言：
      1. 迁移是否已执行（Phase 1 的 15 张表齐全）；
      2. 关键列为 PG 原生类型（uuid / jsonb / timestamp with time zone）；
      3. 基础数据种子是否可重复执行（幂等）。

用法：
    set DATABASE_URL=postgresql+psycopg://...
    python scripts/check_pg_schema.py

退出码：0 = 全部通过；1 = 存在失败项（直接在 CI 中作为门禁）。
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

from config.settings import get_settings
from database.seeds import seed_database
from database.seeds.runner import SeedResult

__all__ = ["main"]

#: 期望的 PG 原生类型（information_schema.columns.data_type）
EXPECTED_PG_TYPES: dict[tuple[str, str], str] = {
    ("raw_items", "id"): "uuid",
    ("raw_items", "raw_json"): "jsonb",
    ("raw_items", "published_at"): "timestamp with time zone",
    ("raw_items", "effective_at"): "timestamp with time zone",
    ("market_bars", "open"): "numeric",
    ("market_bars", "timeframe"): "character varying",
    ("instruments", "id"): "uuid",
    ("sources", "config_json"): "jsonb",
    ("job_runs", "input_json"): "jsonb",
    ("audit_logs", "before_json"): "jsonb",
}


def _database_url() -> str:
    override = os.environ.get("GOLD_AI_DATABASE_URL")
    return override or get_settings().database_url


def check_tables(engine: sa.Engine, expected_tables: tuple[str, ...]) -> list[str]:
    """返回缺失的表名列表。"""
    inspector = sa.inspect(engine)
    return [table for table in expected_tables if not inspector.has_table(table)]


def check_native_types(engine: sa.Engine) -> list[str]:
    """返回不符合预期的 (表, 列, 实际类型) 列表。"""
    query = sa.text(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = :table_name
        """
    )
    problems: list[str] = []
    with engine.connect() as connection:
        for (table, column), expected in EXPECTED_PG_TYPES.items():
            rows: dict[str, str] = {
                str(row[0]): str(row[1])
                for row in connection.execute(query, {"table_name": table}).all()
            }
            actual = rows.get(column)
            if actual != expected:
                problems.append(f"{table}.{column} 期望 {expected}，实际 {actual}")
    return problems


def check_seed_idempotency(database_url: str) -> list[str]:
    """种子第二次执行不得新增任何记录。"""
    results: dict[str, SeedResult] = seed_database(database_url, scope="all")
    repeated: dict[str, SeedResult] = seed_database(database_url, scope="all")

    problems: list[str] = [
        f"{entity}: 首次未写入任何记录（期望 >0）"
        for entity, result in results.items()
        if result.created_count == 0
    ]
    problems.extend(
        f"{entity}: 重复执行新增了 {result.created_count} 条（必须为 0）"
        for entity, result in repeated.items()
        if result.created_count
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    """执行全部 PostgreSQL 校验，返回进程退出码。"""
    from database.models import PHASE1_TABLES

    database_url = _database_url()
    masked = sa.engine.make_url(database_url).render_as_string(hide_password=True)
    print(f"[pg-check] 目标库：{masked}")

    if database_url.startswith("sqlite"):
        print("[pg-check] 该脚本必须在 PostgreSQL 上运行（当前为 SQLite）", file=sys.stderr)
        return 1

    engine = sa.create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        problems = [
            *[f"缺少表：{table}" for table in check_tables(engine, PHASE1_TABLES)],
            *check_native_types(engine),
        ]
    finally:
        engine.dispose()

    problems.extend(check_seed_idempotency(database_url))

    if problems:
        for problem in problems:
            print(f"[pg-check] FAIL: {problem}", file=sys.stderr)
        return 1

    print(f"[pg-check] PASS：{len(PHASE1_TABLES)} 张表、原生类型、种子幂等全部通过")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CI / 手工执行
    raise SystemExit(main())
