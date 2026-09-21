"""把 ``logs/posts.csv`` 的 Mock 合成博文幂等导入研究库（raw_items → author_posts）。

标记（用户裁决硬性要求）：
- 每条 ``raw_json`` 写 ``data_source=SYNTHETIC`` + ``is_mock=true`` + ``mock_batch``；
- Mock 作者的 ``canonical_name`` 用 ``mock:<source>`` 前缀、``display_name`` 加 ``[MOCK]`` 后缀、
  ``metadata_json.data_source=SYNTHETIC``，与真实语料严格区分，防止未来误当作真实数据。

导入后需要另跑 ``scripts/run_opinion_pipeline.py --no-dry-run`` 抽取观点，
再由 ``scripts/build_opinion_labels.py --no-dry-run`` 生成前瞻收益标签。

默认 dry-run；只有显式 ``--no-dry-run`` 才提交。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
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
from scripts.generate_mock_posts import MOCK_ACCOUNTS, MOCK_BATCH  # noqa: E402
from src.common.hashing import content_hash  # noqa: E402
from src.common.time import parse_iso8601  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "logs" / "posts.csv"
#: 合成数据统一标记（用户裁决：防止未来误当作真实数据）
DATA_SOURCE_SYNTHETIC = "SYNTHETIC"


@dataclass(slots=True)
class ImportReport:
    rows: int = 0
    inserted: int = 0
    duplicate: int = 0
    failed: int = 0
    authors_created: int = 0
    accounts_created: int = 0


def _mock_author_seeds() -> list[AuthorSeed]:
    """把 4 个 Mock 账号（display_name, source_name）转成可区分的作者种子。"""
    seeds: list[AuthorSeed] = []
    for display_name, source_name in MOCK_ACCOUNTS:
        seeds.append(
            AuthorSeed(
                display_name=f"{display_name}[MOCK]",
                canonical_name=f"mock:{source_name}",
                source_name=source_name,
                external_account_id=f"mock-{source_name}-rss",
                account_name=f"{display_name}(Mock)",
                verified=False,
                metadata_json={
                    "kind": "synthetic",
                    "data_source": DATA_SOURCE_SYNTHETIC,
                    "mock_batch": MOCK_BATCH,
                },
            )
        )
    return seeds


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [{key: value or "" for key, value in row.items()} for row in csv.DictReader(handle)]
    required = {"post_id", "text_content", "source_name", "effective_at"}
    if not rows:
        raise ValueError("输入 CSV 没有数据行")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"输入 CSV 缺少必需列：{sorted(missing)}")
    return rows


def import_rows(session: Session, rows: Sequence[Mapping[str, str]]) -> ImportReport:
    report = ImportReport(rows=len(rows))
    author_report = import_author_seeds(
        session, _mock_author_seeds(), source_label="phase3-w0-4-mock-posts"
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
            source_name = (row.get("source_name") or "").strip()
            source = sources[source_name]
            account = accounts[(source_name, f"mock-{source_name}-rss")]
            post_id = (row.get("post_id") or "").strip()
            if not post_id:
                raise ValueError("post_id 为空")
            existing = session.scalar(
                sa.select(RawItem).where(
                    RawItem.source_id == source.id, RawItem.source_record_id == post_id
                )
            )
            if existing is not None:
                report.duplicate += 1
                continue
            content = (row.get("text_content") or "").strip()
            if not content:
                raise ValueError(f"第 {row_number} 行 text_content 为空")
            published_raw = (row.get("published_at") or "").strip()
            published_at = (
                parse_iso8601(published_raw, field_name="published_at") if published_raw else None
            )
            collected_at = parse_iso8601(
                (row.get("collected_at") or "").strip(), field_name="collected_at"
            )
            effective_at = parse_iso8601(
                (row.get("effective_at") or "").strip(), field_name="effective_at"
            )
            raw = RawItem(
                source_id=source.id,
                source_record_id=post_id,
                item_type=RawItemType.POST,
                title=None,
                content_text=content,
                raw_json={
                    "import_kind": "mock_synthetic",
                    "data_source": DATA_SOURCE_SYNTHETIC,
                    "is_mock": True,
                    "mock_batch": MOCK_BATCH,
                    "author_name": (row.get("author_name") or "").strip(),
                    "edge_case": (row.get("edge_case") or "").strip(),
                    "repost_of": (row.get("repost_of") or "").strip(),
                    # Mock 有独立采集时间（collected_at = published_at + 1min），非回填
                    "collected_at_provenance": "independent_observation",
                },
                source_url=(row.get("url") or "").strip() or None,
                content_hash=(row.get("content_hash") or "").strip() or content_hash(content),
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
            raise ValueError(f"Mock 帖子导入失败：{exc}") from exc
    return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="W0-4：Mock 合成博文入库（data_source=SYNTHETIC）")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[mock-posts] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    try:
        rows = load_rows(Path(args.input))
        with session_scope() as session:
            nested = session.begin_nested() if dry_run else None
            report = import_rows(session, rows)
            if nested is not None:
                nested.rollback()
        print(f"[mock-posts] {'DRY-RUN ' if dry_run else ''}{asdict(report)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[mock-posts] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

