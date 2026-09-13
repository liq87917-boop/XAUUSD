"""命令行入口：``python -m database.seeds``。

示例：
    python -m database.seeds --scope all
    python -m database.seeds --db-url "sqlite+pysqlite:///./local.db" --dry-run

说明：
- 种子只写数据、不建表；执行前必须先完成结构迁移：``python -m alembic upgrade head``。
- 输出中的数据库 URL 会隐藏密码，避免把连接串泄漏到日志/终端历史。
"""

from __future__ import annotations

import argparse
import sys

import sqlalchemy as sa

from config.settings import get_settings
from database.seeds.runner import SeedResult, seed_database
from src.common.exceptions import GoldAIError

__all__ = ["main"]


def _masked_url(url: str) -> str:
    """隐藏密码后的连接串（仅用于输出）。"""
    try:
        return sa.engine.make_url(url).render_as_string(hide_password=True)
    except sa.exc.ArgumentError:  # pragma: no cover - 非法 URL 由后续连接阶段报错
        return "<无法解析的 DATABASE_URL>"


def _format_items(items: tuple[str, ...], *, limit: int = 10) -> str:
    """列表输出（超过 limit 时截断，避免 100 位作者刷屏）。"""
    if not items:
        return ""
    if len(items) <= limit:
        return ", ".join(items)
    shown = ", ".join(items[:limit])
    return f"{shown} …（共 {len(items)} 条）"


def main(argv: list[str] | None = None) -> int:
    """CLI 入口，返回进程退出码（0 成功 / 1 失败）。"""
    parser = argparse.ArgumentParser(
        prog="python -m database.seeds",
        description=(
            "GOLD-AI Phase 1/2 基础数据种子"
            "（instruments / sources / authors，全部幂等）"
        ),
    )
    parser.add_argument("--db-url", default=None, help="目标数据库 URL（默认取 DATABASE_URL）")
    parser.add_argument(
        "--scope",
        choices=("all", "instruments", "sources", "authors"),
        default="all",
        help="种子范围（默认 all；authors 会连带写入平台账号）",
    )
    parser.add_argument("--dry-run", action="store_true", help="演练：执行后回滚，不落库")
    parser.add_argument(
        "--authors-csv",
        default=None,
        help=(
            "从本地 CSV 导入作者（团队裁决：先用机构/分析师 RSS + 本地 CSV 过渡）；"
            "需配合 --scope authors 或 --scope all"
        ),
    )
    args = parser.parse_args(argv)

    if args.authors_csv and args.scope not in {"authors", "all"}:
        print(
            "[seeds] 参数错误：--authors-csv 只能与 --scope authors 或 --scope all 一起使用",
            file=sys.stderr,
        )
        return 1

    database_url = args.db_url or get_settings().database_url
    mode = "DRY-RUN（不落库）" if args.dry_run else "正式写入"
    print(f"[seeds] 目标库：{_masked_url(database_url)}")
    print(f"[seeds] 范围：{args.scope}｜模式：{mode}")
    if args.authors_csv:
        print(f"[seeds] 作者来源：本地 CSV {args.authors_csv}")

    try:
        results: dict[str, SeedResult] = seed_database(
            database_url,
            scope=args.scope,
            dry_run=args.dry_run,
            authors_csv=args.authors_csv,
        )
    except GoldAIError as exc:
        print(f"[seeds] 失败：{exc}", file=sys.stderr)
        return 1
    except sa.exc.SQLAlchemyError as exc:
        # 数据库层错误（连接失败 / 权限 / 迁移缺失）：给出可读信息并返回非 0，
        # 但不吞掉异常语义（错误内容完整打印，便于定位）。
        print(f"[seeds] 数据库错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for result in results.values():
        print(f"[seeds] {result.summary()}")
        if result.created:
            print(f"[seeds]   + {_format_items(result.created)}")
        if result.existing:
            print(f"[seeds]   = {_format_items(result.existing)}")
    if "authors" in results:
        print("[seeds] 提示：作者平台账号明细见 audit_logs（action=authors.import）")
    print("[seeds] 完成")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
