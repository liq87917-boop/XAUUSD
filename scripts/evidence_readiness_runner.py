"""Evidence Readiness 单次本地 tick runner CLI（**GOLD-010**，纯本地 / 零网络 / 零数据库写入）。

把 GOLD-009 的 readiness 状态变更能力做成**可由外部定时器调用的一次性命令**：
Windows Task Scheduler / 现有本地 orchestrator / 其它外部定时器负责调度，
本命令**只跑一个 tick**，自带**单实例锁**防止并发重入，**绝不常驻循环**。

用法::

    # 单次 tick（唯一写入口：显式 --work-dir；state / 事件日志 / status 都写在这里）
    python -m scripts.evidence_readiness_runner --work-dir logs/evidence/runner --json

    # 固定审计时点（ISO8601 **必须带时区**；用于人工复现 / 培训，不改变口径）
    python -m scripts.evidence_readiness_runner --work-dir logs/evidence/runner `
        --as-of 2026-09-23T00:00:00+00:00

安全与边界：

- **零网络 / 零数据库写入**：readiness 只读台账并显式 ``rollback``，不抓取任何站点；
- **单实例锁**：锁文件写在 ``--work-dir`` 内，记录可审计 ``owner`` / ``pid`` / 获取时间
  （不含敏感数据）；锁冲突 → 退出码 ``6`` 且**零写入**（绝不删除仍活动的锁）；
- **只写显式工作目录**：``readiness_state.json`` / ``readiness_events.jsonl`` /
  ``readiness_status.json`` 三个 artifact 全部**原子写**；
- **fail-closed**：state / 事件日志损坏 → 退出码 ``4``（保留旧 state）；
  资格计算失败 → 退出码 ``7``（保留旧 state）；artifact 写入失败 → 退出码 ``3``；
- **不解除 blocker**：status 恒为 ``blocker_active=true`` / ``human_gate_required=true`` /
  ``data_qualification_passed=false`` / ``phase_transition_allowed=false``；
  即使 ``ready_for_human_review=true``，Phase 切换仍须 ``DEVELOPMENT_PROTOCOL`` 的 L3 人工 Gate；
- **不调度自己**：本命令不安装 / 不修改任何 OS 计划任务（调度方式仅写在 README，由人工配置）；
- 退出码：``0`` 量化门槛达标（**仍需 L3 人工 Gate**）/ ``2`` 参数错误 /
  ``3`` 工作目录或 artifact 不可写 / ``4`` state 或事件日志损坏（安全失败）/
  ``5`` 仍未达标（**预期 BLOCKED**，不是定时器故障）/ ``6`` 锁冲突（另一个 tick 正在运行）/
  ``7`` 资格计算失败。
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
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    ReadinessSnapshot,
    RunnerError,
    build_snapshot_from_session,
    exit_code_for,
    render_tick_summary,
    run_tick,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--work-dir`` 必填：runner 的唯一写入口必须显式给出）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_readiness_runner",
        description=(
            "Evidence Readiness 单次本地 tick：由外部定时器调用；自带单实例锁、原子写、"
            "fail-closed；不联网、不写数据库、不解除 PHASE3_3_DATA、不常驻循环。"
        ),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help=(
            "**必填**：本地工作目录（state / 事件日志 / status / 锁文件都写在此目录内，唯一写入口）"
        ),
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间）",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    return parser


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON。"""
    if json_output:
        print(safe_text(message), file=sys.stderr)
    else:
        safe_print(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """CLI 主入口：执行**一次** tick 并返回进程退出码（零网络、零数据库写入、无常驻循环）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)
    work_dir: Path = args.work_dir
    factory = session_factory or build_session_factory(build_engine())

    def snapshot_builder(at: datetime) -> ReadinessSnapshot:
        """真实 builder：只读台账计算快照（与既有 CLI 同源口径）。"""
        return build_snapshot_from_session(factory, moment=at)

    try:
        report = run_tick(work_dir, moment=moment, snapshot_builder=snapshot_builder)
    except RunnerError as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            f"[evidence] tick 未完成（fail-closed，未写入任何 artifact）：{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return exit_code_for(exc)

    json_output = bool(args.json_output)
    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_tick_summary(report)
    )
    safe_print(text)
    _notify(f"[evidence] tick status 已原子写入：{work_dir}", json_output=json_output)
    return EXIT_OK if report.ready_for_human_review else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动 / 定时器运行入口
    raise SystemExit(main())
