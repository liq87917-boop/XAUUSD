"""Evidence 本地 Inbox 发现与预检 CLI（**GOLD-011**，纯本地只读发现 / 零网络 / 零数据库）。

把 GOLD-005~010 的 Evidence Gateway / Operator / Readiness 能力接到一个**显式目录**上：
operator 只需把候选 Author / News evidence 与 ``manifest.json`` 放进 inbox 目录的子目录，
本命令即可**确定性**发现、预检并生成**脱敏**的 pending / preflight artifact。

用法::

    # 只读扫描（stdout 打印；零写入、零网络；不会移动 / 删除任何原始 evidence）
    python -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox --json

    # 与既有 pending 清单比较（--state 只读；损坏 → 退出码 4，零写入）
    python -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox ^
        --state logs/evidence/inbox_pending.json --json

    # 唯一写开关：--out 原子落盘更新后的 pending 清单（自带单实例锁，防并发重扫）
    python -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox ^
        --state logs/evidence/inbox_pending.json ^
        --out logs/evidence/inbox_pending.json --json

    # 固定审计时点（ISO8601 **必须带时区**；用于人工复现 / 培训，不改变任何口径）
    python -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox ^
        --as-of 2026-09-23T00:00:00+00:00

安全与边界：

- **只读发现**：只扫描显式 ``--inbox-dir`` 的**直接子目录**（候选包）；
  **绝不**移动 / 重命名 / 删除 / 改写 inbox 内的任何文件；不联网、不写数据库、
  不新增 migration/schema、不接第三方推送、**不自动 intake**；
- **manifest 显式关联**：候选包必须提供 ``manifest.json``（``evidence_type`` /
  ``source`` / ``authorization_reference`` / ``time_semantics`` /
  ``availability_semantics`` / ``historical_oos_applicable`` / ``files`` 的
  ``path`` + ``sha256``）；缺失 / 不一致一律 **fail-closed** 并给出稳定原因码；
- **内容摘要与指纹**：每个文件算 SHA-256 并与声明核对；候选包指纹只由内容摘要与结构标记
  派生（不含 mtime / 扫描时间 / 绝对路径），同内容重复扫描**不重复生成**待处理项；
- **复用同一口径**：逐行预检复用 Evidence Gateway 的 ``assess_row``（``evidence-intake-v1``），
  授权 / 时间 / availability / 隔离原因码与 ``intake`` **完全同源**；不复制算法、不降阈值；
- **只生成脱敏 artifact**：``discovered`` / ``preflight_pass`` / ``quarantined`` /
  ``requires_human_action`` 显式区分；凭据类字段只记录键名、URL 去掉查询串；
- **不解除 blocker**：四个安全字段恒为 ``blocker_active=true`` /
  ``human_gate_required=true`` / ``data_qualification_passed=false`` /
  ``phase_transition_allowed=false``；``preflight_pass`` 仍须**人工确认 + 显式 intake**
  （``scripts/evidence_operator.py workflow --no-dry-run``）与 L3 人工 Gate；
- 退出码：``0`` 扫描完成（可能存在人工待确认项）/ ``2`` 参数错误 /
  ``3`` inbox 目录或输出不可用（含输出写在 inbox 内）/ ``4`` 既有 pending state 损坏
  （安全失败，零写入）/ ``5`` 扫描完成但**没有任何**可进入人工队列的候选（预期 BLOCKED）/
  ``6`` 锁冲突（另一个重扫正在运行）。
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

from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    EXIT_NO_CANDIDATES,
    EXIT_OK,
    InboxError,
    LockConflictError,
    LockUnavailableError,
    inbox_exit_code_for,
    render_inbox_summary,
    run_inbox_scan,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--inbox-dir`` 必填：只扫描**显式**目录）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_inbox",
        description=(
            "Evidence 本地 Inbox 发现与预检：只读扫描显式目录、manifest 显式关联、"
            "内容 SHA-256 + 幂等指纹、fail-closed、脱敏；不联网、不写数据库、"
            "不移动 / 删除原始 evidence、不自动 intake、不解除 PHASE3_3_DATA。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        required=True,
        help="**必填**：候选 evidence 的 inbox 目录（候选包 = 其直接子目录，内含 manifest.json）",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="既有 pending 清单（**只读**；用于幂等去重；文件不存在 = 首次扫描）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="pending 清单落盘路径（**唯一写开关**；原子写；必须位于 inbox 目录之外）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发重扫）",
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
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：执行**一次**只读扫描并返回进程退出码（零网络、零数据库写入）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    scan_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    try:
        report = run_inbox_scan(
            args.inbox_dir,
            moment=scan_moment,
            pending_path=args.state,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (InboxError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] inbox 预检未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return inbox_exit_code_for(exc)

    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_inbox_summary(report)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] pending 清单已原子写入：{args.out}"
            "（需人工确认后显式执行 evidence_operator workflow --no-dry-run）",
            json_output=json_output,
        )
    return EXIT_OK if report.has_candidates else EXIT_NO_CANDIDATES


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
