"""把冻结的 SQLite 研究库原样迁入一个空 PostgreSQL 数据库。

默认只审计；只有 ``--no-dry-run`` 才会在单一事务中写入。迁移后逐表核对行数与
规范化内容 SHA-256，任一不一致都会回滚。SQLite 读回的 naive datetime 按项目既有
约定解释为 UTC，不按 Windows 本地时区转换。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Engine, Table

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings  # noqa: E402
from database.models import PHASE1_TABLES, PHASE2_TABLES, Base  # noqa: E402
from database.session import build_engine  # noqa: E402

DEFAULT_SOURCE = REPO_ROOT / "database" / "backups" / "gold_ai_w0_preflight_closed_20260917.db"
DEFAULT_REPORT = REPO_ROOT / "logs" / "sqlite_to_postgres_migration_report.json"
CHUNK_SIZE: Final[int] = 1_000


def mask_db_url(url: str) -> str:
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest and ":" in rest.split("@", 1)[0]:
        credentials, host = rest.split("@", 1)
        return f"{scheme}://{credentials.split(':', 1)[0]}:***@{host}"
    return url


@dataclass(frozen=True, slots=True)
class TableAudit:
    table: str
    source_rows: int
    target_rows: int
    source_hash: str
    target_hash: str

    @property
    def passed(self) -> bool:
        return self.source_rows == self.target_rows and self.source_hash == self.target_hash


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalise_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_value(item) for item in value]
    return value


def _prepare_row(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    prepared = dict(row)
    for column in table.columns:
        value = prepared.get(column.name)
        if value is not None and isinstance(column.type, sa.DateTime):
            prepared[column.name] = _utc(value)
    return prepared


def _read_rows(connection: sa.Connection, table: Table) -> list[dict[str, Any]]:
    primary_key = list(table.primary_key.columns)
    statement = sa.select(table)
    if primary_key:
        statement = statement.order_by(*primary_key)
    return [_prepare_row(table, dict(row)) for row in connection.execute(statement).mappings()]


def _rows_hash(table: Table, rows: Iterable[Mapping[str, Any]]) -> str:
    column_names = [column.name for column in table.columns]
    canonical = [{name: _normalise_value(row.get(name)) for name in column_names} for row in rows]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterable[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), CHUNK_SIZE):
        yield rows[start : start + CHUNK_SIZE]


def _tables() -> list[Table]:
    # W0 冻结源库停留在 Phase 2；W0-5 新表应由 Alembic 在目标库创建为空表，
    # 不能反向要求历史 SQLite 快照包含尚未存在的 Phase 3 结构。
    return [Base.metadata.tables[name] for name in PHASE1_TABLES + PHASE2_TABLES]


def audit_database(engine: Engine) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    with engine.connect() as connection:
        for table in _tables():
            rows = _read_rows(connection, table)
            result[table.name] = (len(rows), _rows_hash(table, rows))
    return result


def _assert_empty_target(connection: sa.Connection) -> None:
    nonempty = {
        table.name: int(connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0)
        for table in _tables()
    }
    nonempty = {name: count for name, count in nonempty.items() if count}
    if nonempty:
        details = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise ValueError(f"目标数据库必须为空；当前存在数据：{details}")


def migrate_database(source_engine: Engine, target_engine: Engine) -> list[TableAudit]:
    """在单一目标事务内迁移并校验；失败自动回滚。"""
    source_audit = audit_database(source_engine)
    with source_engine.connect() as source, target_engine.begin() as target:
        _assert_empty_target(target)
        for table in _tables():
            rows = _read_rows(source, table)
            deferred_superseded: list[tuple[Any, Any]] = []
            if table.name == "raw_items":
                for row in rows:
                    if row.get("superseded_by_id") is not None:
                        deferred_superseded.append((row["id"], row["superseded_by_id"]))
                        row["superseded_by_id"] = None
            for chunk in _chunks(rows):
                if chunk:
                    target.execute(table.insert(), list(chunk))
            if deferred_superseded:
                for row_id, superseded_by_id in deferred_superseded:
                    target.execute(
                        table.update()
                        .where(table.c.id == row_id)
                        .values(superseded_by_id=superseded_by_id)
                    )

        audits: list[TableAudit] = []
        for table in _tables():
            target_rows = _read_rows(target, table)
            source_count, source_hash = source_audit[table.name]
            audit = TableAudit(
                table=table.name,
                source_rows=source_count,
                target_rows=len(target_rows),
                source_hash=source_hash,
                target_hash=_rows_hash(table, target_rows),
            )
            audits.append(audit)
            if not audit.passed:
                raise RuntimeError(f"迁移校验失败：{asdict(audit)}")
    return audits


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="冻结 SQLite → 空 PostgreSQL 研究库迁移")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE))
    parser.add_argument("--target-url", default=None, help="默认读取 DATABASE_URL")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    source_path = Path(args.source_db).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite 快照不存在：{source_path}")
    target_url = args.target_url or get_settings().database_url
    if not target_url.startswith("postgresql+psycopg://"):
        raise ValueError("目标必须是 postgresql+psycopg:// 数据库")

    source_engine = build_engine(f"sqlite+pysqlite:///{source_path.as_posix()}")
    target_engine = build_engine(target_url)
    try:
        source_audit = audit_database(source_engine)
        target_audit = audit_database(target_engine)
        print(f"[migrate] source={source_path}")
        print(f"[migrate] target={mask_db_url(target_url)}")
        print(f"[migrate] source_rows={sum(count for count, _hash in source_audit.values())}")
        if not args.no_dry_run:
            nonempty = {name: count for name, (count, _hash) in target_audit.items() if count}
            print(f"[migrate] DRY-RUN target_nonempty={nonempty}")
            return 0

        audits = migrate_database(source_engine, target_engine)
        report = {
            "source": str(source_path),
            "target": mask_db_url(target_url),
            "passed": all(item.passed for item in audits),
            "tables": [asdict(item) | {"passed": item.passed} for item in audits],
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[migrate] PASS tables={len(audits)} rows={sum(x.source_rows for x in audits)}")
        print(f"[migrate] report={report_path}")
        return 0
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
