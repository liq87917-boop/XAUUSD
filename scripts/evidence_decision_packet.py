"""L3 人工决策包 CLI（**GOLD-015**，纯本地只读聚合；默认零写入）。

把最新 readiness / handoff（GOLD-008/010）、批准清单与 GOLD-013 intake plan、GOLD-014
verified receipt 与 qualification recheck 聚合为**确定性、脱敏、内容寻址**（``packet_id``）的
**L3 人工 Gate 决策包**；**不做**任何 intake，**不写数据库**，**不解除** ``PHASE3_3_DATA``，
**不切换 Phase**。

用法::

    # ① 只读聚合并打印决策包（零写入）
    python -m scripts.evidence_decision_packet \\
        --handoff logs/evidence/handoff.json \\
        --readiness logs/evidence/readiness_state.json \\
        --inbox-dir logs/evidence/inbox \\
        --ledger logs/evidence/inbox_review_ledger.json \\
        --approved-list logs/evidence/approved_for_intake.json \\
        --plan logs/evidence/evidence_intake_plan.json \\
        --operator-result logs/evidence/author_manifest.json \\
        --recheck logs/evidence/phase33_recheck.json \\
        --receipt logs/evidence/evidence_intake_receipt.json --json

    # ② 唯一写开关：显式 --out 原子落盘 packet（仍**不**写数据库、**不**自动 intake）
    python -m scripts.evidence_decision_packet ... --out logs/evidence/decision_packet.json

    # ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
    python -m scripts.evidence_decision_packet ... --as-of 2026-09-23T04:00:00+00:00 --json

安全与边界：

- **纯本地 / 只读**：所有输入只读；**默认零写入**，只有显式 ``--out`` 才**原子**落盘 packet；
  **绝不**移动 / 重命名 / 删除 / 改写 inbox 内任何原始 evidence；**绝不**写数据库、**不新增**
  migration / schema、**不联网**；**绝不**修改 ``.ai/PROJECT_STATE.json`` 或解除 blocker；
- **聚合（fail-closed）**：handoff 必须结构自洽（内部算术 / ``thresholds`` / 安全字段 /
  无证据时间键）；给出 ``--readiness`` 时快照必须与 handoff **同源**（含**指纹**）；plan /
  批准清单 / 人工显式执行结果 / recheck 的核验**沿用 GOLD-014 的稳定原因码**；handoff 不得
  早于最近一次显式落库；显式给出 GOLD-014 收据文件时其 ``receipt_id`` 必须内容寻址自洽且与
  **当前**重新绑定结果一致 —— 任一不一致 → 退出码 ``4`` 且**零写入**；
- **没有**任何 intake / ``--no-dry-run`` 参数：本工具**绝不**执行 intake；
- **区分五个布尔，绝不越权**：``evidence_ready_for_human_review`` / ``receipt_verified`` /
  ``qualification_recheck_ready`` 是三个**独立**事实；``data_qualification_passed`` /
  ``phase_transition_allowed`` **恒为** false、``blocker_active`` / ``human_gate_required``
  **恒为** true、``human_gate_level`` 恒为 ``L3``；本工具只能给出
  ``submit_to_l3_human_gate`` 与缺口 / 稳定原因码，``PHASE3_3_DATA`` **保持 BLOCKED**；
- 退出码：``0`` ``submit_to_l3_human_gate=true``（可提交 L3 人工 Gate；仍**不是**资格通过）/
  ``2`` 参数错误（缺必填参数 / 时区缺失）/ ``3`` inbox 或输出不可用（含把 ``--out`` 写进
  inbox 的拒绝）/ ``4`` 任一输入缺失 / 损坏 / stale / 篡改或核验不通过（fail-closed，零写入）/
  ``5`` packet 已生成但**不可提交**（**预期 BLOCKED**）/ ``6`` 锁冲突；
  失败路径 stdout 为空、stderr 已脱敏。
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
    EXIT_BLOCKED,
    EXIT_OK,
    LockConflictError,
    LockUnavailableError,
    decision_packet_exit_code_for,
    render_decision_packet_summary,
    run_decision_packet,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（五个输入必填；**没有**任何 intake / 写库参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_decision_packet",
        description=(
            "L3 人工决策包：把 readiness / handoff、GOLD-013 plan、GOLD-014 收据与 "
            "qualification recheck 聚合为确定性脱敏的只读决策包（内容级 packet_id）；默认零写入、"
            "不联网、不写数据库、绝不执行 intake、不解除 PHASE3_3_DATA、不切换 Phase。"
        ),
    )
    parser.add_argument(
        "--handoff",
        type=Path,
        required=True,
        help=(
            "**必填**：GOLD-008 evidence handoff 报告（**只读**；"
            "内部算术 / thresholds / 安全字段不自洽 → 退出码 4）"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        dest="inbox_dir",
        type=Path,
        required=True,
        help="**必填**：候选 evidence 的 inbox 目录（GOLD-011 口径；只读扫描其直接子目录）",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="**必填**：GOLD-012 review ledger（**只读**；批准来源必须可核验）",
    )
    parser.add_argument(
        "--approved-list",
        dest="approved_list",
        type=Path,
        required=True,
        help="**必填**：GOLD-012 脱敏批准清单（**只读**；批准范围必须可核验）",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="**必填**：GOLD-013 intake plan（**只读**；plan 过期 / 被改写 → 退出码 4）",
    )
    parser.add_argument(
        "--readiness",
        type=Path,
        default=None,
        help=(
            "可选：GOLD-010 tick 落盘的 readiness state（**只读**；必须与 handoff **同源**，"
            "含指纹，否则退出码 4）"
        ),
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help=(
            "可选：GOLD-014 intake receipt（**只读**；receipt_id 必须内容寻址自洽且与**当前**"
            "重新绑定结果一致，否则退出码 4）"
        ),
    )
    parser.add_argument(
        "--operator-result",
        dest="operator_results",
        type=Path,
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "人工**显式** Evidence Operator 执行结果（`--no-dry-run --manifest` 产物；可重复给出，"
            "每个被批准的证据文件都必须被覆盖）"
        ),
    )
    parser.add_argument(
        "--recheck",
        type=Path,
        default=None,
        help="qualification recheck 结果（`phase33_qualification_recheck`；必须不早于显式执行，"
        "且 readiness 结论必须与 handoff 一致）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="**唯一** packet 写开关（原子写 + 单实例锁；必须位于 inbox 之外；缺省 = 只读）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发覆盖 packet）",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间；用于人工复现）",
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
    """CLI 主入口：执行**一次**聚合并返回进程退出码（零网络、零数据库读写）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    try:
        packet = run_decision_packet(
            args.inbox_dir,
            moment=run_moment,
            handoff_path=args.handoff,
            plan_path=args.plan,
            ledger_path=args.ledger,
            approved_list_path=args.approved_list,
            readiness_path=args.readiness,
            receipt_path=args.receipt,
            operator_result_paths=tuple(args.operator_results or ()),
            recheck_path=args.recheck,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] decision packet 未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return decision_packet_exit_code_for(exc)

    text = (
        json.dumps(packet.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_decision_packet_summary(packet)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] decision packet 已原子写入：{args.out}"
            "（只读审计产物；仍**不是**资格通过，Phase 切换仍须 L3 人工 Gate）",
            json_output=json_output,
        )
    return EXIT_OK if packet.submit_to_l3_human_gate else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())