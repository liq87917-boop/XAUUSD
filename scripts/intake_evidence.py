"""Evidence Intake CLI：接收业务方已授权的 Author / News 证据（**GOLD-005**）。

用法::

    # 默认 dry-run：只校验并打印报告（不写库、不落文件、不联网）
    python -m scripts.intake_evidence --scope author --input logs/evidence/authors.jsonl

    # 机器可读 JSON
    python -m scripts.intake_evidence --scope news --input logs/evidence/news.csv --json

    # 可复现审计时点（必须带时区；用于"未来时间"判定）
    python -m scripts.intake_evidence --scope news --input logs/evidence/news.csv \
        --as-of 2026-09-22T12:00:00+00:00

    # 显式提交：append-only 写入 raw_items + processed_items，并落 manifest / quarantine
    python -m scripts.intake_evidence --scope news --input logs/evidence/news.csv \
        --no-dry-run --manifest logs/evidence/news_manifest.json \
        --quarantine logs/evidence/news_quarantine.jsonl

安全与边界：

- **默认 dry-run / 零写入**：不传 ``--no-dry-run`` 时绝不写数据库，也不落 manifest /
  quarantine 文件；
- **默认零网络**：只读本地输入文件；不抓取任何站点，不绕过 robots / 条款 / 证书限制；
- **不覆盖历史事实**：同一 ``(source, source_record_id)`` 内容不同判 ``IDENTITY_CONFLICT``
  并隔离，绝不 UPDATE；
- **不夸大资格**：没有独立 ``available_at`` 证据的记录标记 ``NOT_OOS_ELIGIBLE``；
- **不解除 blocker**：本命令不会解除 ``PHASE3_3_DATA``（授权法律效力与历史可用性
  仍需人工核验）；
- 退出码：``0`` 全部通过 / ``2`` 参数或输入文件错误 / ``3`` 输入没有数据行 /
  ``4`` 存在被隔离的行（数据已隔离，未计入可信证据）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    EVIDENCE_CONTRACT_VERSION,
    EvidenceScope,
    intake_evidence,
    quarantine_payload,
    read_input_file,
)

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_NO_ROWS = 3
EXIT_QUARANTINED = 4


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项）。"""
    parser = argparse.ArgumentParser(
        prog="intake_evidence",
        description=(
            f"授权证据接收入口（契约 {EVIDENCE_CONTRACT_VERSION}）："
            "默认 dry-run、默认零网络、默认零写入"
        ),
    )
    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in EvidenceScope],
        required=True,
        help="证据类别：author（作者帖子类）或 news（新闻/快讯类）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="输入文件（.jsonl / .ndjson / .json 或 .csv；本地文件，不联网）",
    )
    parser.add_argument(
        "--format",
        default="auto",
        choices=["auto", "jsonl", "csv"],
        help="输入格式（默认 auto：按后缀识别）",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSON manifest 落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )
    parser.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help="隔离清单 JSONL 落盘路径（仅在同时给出 --no-dry-run 且存在隔离行时写入）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验（默认行为；显式写出便于审计）",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="显式提交：append-only 写入 raw_items + processed_items",
    )
    return parser


def parse_as_of(raw: str | None) -> datetime | None:
    """解析 ``--as-of``（必须带时区，否则拒绝：禁止隐式时区）。"""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--as-of 无法解析为 ISO8601：{raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of 必须包含时区（例如 2026-09-22T12:00:00+00:00）")
    return parsed.astimezone(UTC)


def validate_output_targets(input_path: Path, targets: Sequence[Path]) -> None:
    """拒绝把 manifest / quarantine 写到输入文件上（包括硬链接别名）。"""
    resolved_input = input_path.resolve()
    seen: list[Path] = []
    for target in targets:
        resolved = target.resolve()
        if resolved == resolved_input or (
            resolved.exists() and input_path.exists() and resolved.samefile(input_path)
        ):
            raise ValueError("--manifest / --quarantine 不能与 --input 指向同一文件")
        if resolved in seen:
            raise ValueError("--manifest 与 --quarantine 不能指向同一文件")
        seen.append(resolved)


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """CLI 主入口（默认 dry-run；返回进程退出码）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.no_dry_run:
        parser.error("--dry-run 与 --no-dry-run 不能同时使用")
    dry_run = not args.no_dry_run
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    targets = [target for target in (args.manifest, args.quarantine) if target is not None]
    if targets:
        try:
            validate_output_targets(args.input, targets)
        except ValueError as exc:
            parser.error(str(exc))

    input_path: Path = args.input
    try:
        input_file, rows = read_input_file(input_path, requested_format=args.format)
    except (OSError, ValueError) as exc:
        print(
            f"[evidence] 输入不可用：{type(exc).__name__}: {safe_text(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
    if not rows:
        print("[evidence] 输入没有数据行", file=sys.stderr)
        return EXIT_NO_ROWS

    moment = as_of if as_of is not None else datetime.now(UTC)
    scope = EvidenceScope(args.scope)
    factory = session_factory or build_session_factory(build_engine())
    with factory() as session:
        try:
            report = intake_evidence(
                session,
                scope=scope,
                input_file=input_file,
                rows=rows,
                moment=moment,
                dry_run=dry_run,
            )
        except Exception:  # noqa: BLE001 - 明确失败并整体回滚，不静默吞掉
            session.rollback()
            raise
        if dry_run:
            session.rollback()
        else:
            session.commit()

    if args.json:
        safe_print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        safe_print(report.render())

    _write_artifacts(args, report, dry_run=dry_run, json_mode=args.json)
    return EXIT_QUARANTINED if report.counts.quarantined else EXIT_OK


def _write_artifacts(
    args: argparse.Namespace,
    report: object,
    *,
    dry_run: bool,
    json_mode: bool = False,
) -> None:
    """落盘 manifest / quarantine（**仅在非 dry-run 时**；默认零写入）。

    ``--json`` 模式下提示信息走 stderr，保证 stdout 是**纯 JSON**（机器可读稳定）。
    """
    from src.evidence import EvidenceIntakeReport

    assert isinstance(report, EvidenceIntakeReport)

    def notify(message: str) -> None:
        if json_mode:
            print(safe_text(message), file=sys.stderr)
        else:
            safe_print(message)

    if args.manifest is not None:
        if dry_run:
            notify(f"（dry-run：未写入 {args.manifest}；如需写入请加 --no-dry-run）")
        else:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            notify(f"manifest 已写入：{args.manifest}")
    if args.quarantine is not None:
        if dry_run:
            notify(f"（dry-run：未写入 {args.quarantine}；如需写入请加 --no-dry-run）")
        else:
            entries = quarantine_payload(report)
            if not entries:
                notify(f"没有需要隔离的行，未创建：{args.quarantine}")
                return
            args.quarantine.parent.mkdir(parents=True, exist_ok=True)
            args.quarantine.write_text(
                "\n".join(
                    json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries
                )
                + "\n",
                encoding="utf-8",
            )
            notify(f"隔离清单已写入：{args.quarantine}（{len(entries)} 行）")


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())

