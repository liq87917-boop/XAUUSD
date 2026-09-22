"""采集运行健康度与 Phase 3.3 数据资格观测 CLI（**只读**，GOLD-004）。

用法::

    # 人类可读摘要（默认；只读，不写库）
    python -m scripts.report_collector_health

    # 稳定 JSON（机器可读）
    python -m scripts.report_collector_health --json

    # 指定窗口 / 审计时点（可复现）
    python -m scripts.report_collector_health --window-hours 48 --as-of 2026-09-22T12:00:00+00:00

    # 落盘 Markdown 报告（必须显式 --no-dry-run，与项目其它脚本一致）
    python -m scripts.report_collector_health --no-dry-run \
        --report docs/experiments/collector_health.md

安全与边界：
- 只读：只 SELECT；不写库、不改写任何历史事实、**不解除** ``PHASE3_3_DATA`` blocker；
- 不联网；不输出凭据或完整 ``sources.config_json``（脱敏见 ``src/monitoring``）；
- 默认即 dry-run：不传 ``--no-dry-run`` 时绝不会写任何文件；
- 退出码：读取成功一律 0（本命令是观测工具，不用于 CI 门禁判失败）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.monitoring import DEFAULT_WINDOW_HOURS, load_monitoring_report  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项）。"""
    parser = argparse.ArgumentParser(
        prog="report_collector_health",
        description="采集运行健康度与 Phase 3.3 数据资格只读观测报告",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"观测窗口（小时，默认 {DEFAULT_WINDOW_HOURS}；必须为正）",
    )
    parser.add_argument(
        "--stale-after-minutes",
        type=int,
        default=None,
        help="陈旧阈值（分钟，默认 = 3 倍 30 分钟调度间隔 = 90 分钟）",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间）",
    )
    parser.add_argument(
        "--hf-weak-supervision-rows",
        type=int,
        default=0,
        help="HF 标题弱监督行数（仅记录，不参与资格判定）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown 报告落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="允许写入 --report（默认只打印，不落盘）",
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


def resolve_stale_after(minutes: int | None) -> timedelta | None:
    """把 ``--stale-after-minutes`` 解析为 ``timedelta``（缺省交给监控层默认值）。"""
    return None if minutes is None else timedelta(minutes=minutes)


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """CLI 主入口（只读；返回进程退出码）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.window_hours <= 0:
        parser.error("--window-hours 必须为正数")
    if args.stale_after_minutes is not None and args.stale_after_minutes <= 0:
        parser.error("--stale-after-minutes 必须为正数")
    if args.hf_weak_supervision_rows < 0:
        parser.error("--hf-weak-supervision-rows 不能为负数")
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))

    factory = session_factory or build_session_factory(build_engine())
    with factory() as session:
        report = load_monitoring_report(
            session,
            window_hours=args.window_hours,
            as_of=as_of,
            stale_after=resolve_stale_after(args.stale_after_minutes),
            hf_weak_supervision_rows=args.hf_weak_supervision_rows,
        )

    if args.json:
        safe_print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        safe_print(report.render())

    if args.report is not None:
        if not args.no_dry_run:
            safe_print(f"（dry-run：未写入 {args.report}；如需写入请加 --no-dry-run）")
        else:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report.render(), encoding="utf-8")
            safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
