"""把已验收的真实观点 CSV 幂等导入 raw_items → author_posts。

默认 dry-run；只有显式 ``--no-dry-run`` 才提交。输入时间必须是带时区 ISO8601，
缺少 collected_at 时沿用文件中已审计的 effective_at，并在 raw_json 留下该回退来源。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from database.models import AuthorAccount, AuthorPost, RawItem, Source  # noqa: E402
from database.models.enums import RawItemType  # noqa: E402
from database.seeds.authors import AuthorSeed, import_author_seeds  # noqa: E402
from database.session import session_scope  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from src.common.hashing import content_hash  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "logs" / "real_posts_opinion_2026_09_14.csv"


@dataclass(slots=True)
class ImportReport:
    rows: int = 0
    inserted: int = 0
    duplicate: int = 0
    failed: int = 0
    authors_created: int = 0
    accounts_created: int = 0
    used_effective_as_collected: int = 0


def parse_aware(value: str, *, field: str, row_number: int) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"第 {row_number} 行 {field} 不是合法 ISO8601：{text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"第 {row_number} 行 {field} 缺少时区：{text!r}")
    return parsed.astimezone(UTC)


def stable_record_id(row: Mapping[str, str]) -> str:
    explicit = (row.get("id") or "").strip()
    if explicit:
        return explicit
    url = (row.get("url") or "").strip()
    if url:
        return url
    return content_hash(
        row.get("source"), row.get("title"), row.get("content"), row.get("published_at")
    )


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [{key: value or "" for key, value in row.items()} for row in csv.DictReader(handle)]
    required = {"content", "published_at", "effective_at", "source", "author_name"}
    if not rows:
        raise ValueError("输入 CSV 没有数据行")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"输入 CSV 缺少必需列：{sorted(missing)}")
    return rows


def _author_seeds(rows: Sequence[Mapping[str, str]]) -> list[AuthorSeed]:
    pairs = sorted(
        {
            ((row.get("source") or "").strip(), (row.get("author_name") or "").strip())
            for row in rows
        }
    )
    if any(not source or not author for source, author in pairs):
        raise ValueError("source / author_name 不能为空")
    return [
        AuthorSeed(
            display_name=author,
            canonical_name=f"manual:{source}:{author}",
            source_name=source,
            external_account_id=source,
            account_name=author,
            verified=True,
            metadata_json={"kind": "manual_real_corpus", "source_file": DEFAULT_INPUT.name},
        )
        for source, author in pairs
    ]


def import_rows(session: Session, rows: Sequence[Mapping[str, str]]) -> ImportReport:
    report = ImportReport(rows=len(rows))
    author_report = import_author_seeds(
        session, _author_seeds(rows), source_label="phase3-w0-4-real-posts"
    )
    if author_report.problems:
        raise ValueError("；".join(author_report.problems))
    report.authors_created = author_report.created_author_count
    report.accounts_created = author_report.created_account_count

    sources = {source.name: source for source in session.scalars(sa.select(Source)).all()}
    accounts = {
        (source_name, account.external_account_id): account
        for account, source_name in session.execute(
            sa.select(AuthorAccount, Source.name).join(Source, Source.id == AuthorAccount.source_id)
        ).all()
    }
    for row_number, row in enumerate(rows, start=2):
        try:
            source_name = (row.get("source") or "").strip()
            source = sources[source_name]
            account = accounts[(source_name, source_name)]
            published_at = parse_aware(
                row.get("published_at") or "", field="published_at", row_number=row_number
            )
            effective_at = parse_aware(
                row.get("effective_at") or "", field="effective_at", row_number=row_number
            )
            collected_raw = (row.get("collected_at") or "").strip()
            collected_at = (
                parse_aware(collected_raw, field="collected_at", row_number=row_number)
                if collected_raw
                else effective_at
            )
            if not collected_raw:
                report.used_effective_as_collected += 1
            if effective_at < max(published_at, collected_at):
                raise ValueError(
                    f"第 {row_number} 行 effective_at 早于 published_at/collected_at"
                )
            record_id = stable_record_id(row)
            existing = session.scalar(
                sa.select(RawItem).where(
                    RawItem.source_id == source.id, RawItem.source_record_id == record_id
                )
            )
            if existing is not None:
                report.duplicate += 1
                continue
            content = (row.get("content") or "").strip()
            if not content:
                raise ValueError(f"第 {row_number} 行 content 为空")
            raw = RawItem(
                source_id=source.id,
                source_record_id=record_id,
                item_type=RawItemType.POST,
                title=(row.get("title") or "").strip() or None,
                content_text=content,
                raw_json={
                    "import_kind": "manual_real_corpus",
                    "author_name": (row.get("author_name") or "").strip(),
                    "language": (row.get("language") or "").strip(),
                    "category": (row.get("category") or "").strip(),
                    "has_media": (row.get("has_media") or "").strip().lower() == "true",
                    "collected_at_provenance": (
                        "input" if collected_raw else "input_effective_at_fallback"
                    ),
                },
                source_url=(row.get("url") or "").strip() or None,
                content_hash=content_hash(row.get("title"), content),
                published_at=published_at,
                collected_at=collected_at,
                effective_at=effective_at,
            )
            session.add(raw)
            session.flush()
            session.add(
                AuthorPost(
                    author_id=account.author_id,
                    author_account_id=account.id,
                    raw_item_id=raw.id,
                    published_at=published_at,
                    collected_at=collected_at,
                    effective_at=effective_at,
                    text_content=content,
                    has_media=(row.get("has_media") or "").strip().lower() == "true",
                )
            )
            session.flush()
            report.inserted += 1
        except (KeyError, ValueError) as exc:
            report.failed += 1
            raise ValueError(f"真实帖子导入失败：{exc}") from exc
    return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="W0-4：真实观点语料正式入库")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[real-posts] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    try:
        rows = load_rows(Path(args.input))
        with session_scope() as session:
            nested = session.begin_nested() if dry_run else None
            report = import_rows(session, rows)
            if nested is not None:
                nested.rollback()
        print(f"[real-posts] {'DRY-RUN ' if dry_run else ''}{asdict(report)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[real-posts] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
