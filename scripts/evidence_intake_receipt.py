"""Evidence 显式 Intake **Receipt** CLI（**GOLD-014**，纯本地只读核验）。

把 GOLD-013 的 intake plan、**当前** inbox / review 状态、人工**显式** Evidence Operator 执行结果
（``--no-dry-run --manifest`` 产物）与随后的 qualification recheck 绑定成**确定性、脱敏、内容寻址**
的 receipt；**不做**任何 intake，**不写数据库**，**不解除** ``PHASE3_3_DATA``。

用法::

    # ① 只读核验并打印收据（零写入）
    python -m scripts.evidence_intake_receipt \\
        --inbox-dir logs/evidence/inbox \\
        --ledger logs/evidence/inbox_review_ledger.json \\
        --approved-list logs/evidence/approved_for_intake.json \\
        --plan logs/evidence/evidence_intake_plan.json \\
        --operator-result logs/evidence/author_manifest.json \\
        --recheck logs/evidence/phase33_recheck.json --json

    # ② 唯一写开关：显式 --out 原子落盘 receipt（仍**不**写数据库、**不**自动 intake）
    python -m scripts.evidence_intake_receipt ... --out logs/evidence/evidence_intake_receipt.json

    # ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
    python -m scripts.evidence_intake_receipt ... --as-of 2026-09-23T00:00:00+00:00 --json

安全与边界：

- **纯本地 / 只读**：所有输入只读；**默认零写入**，只有显式 ``--out`` 才**原子**落盘 receipt；
  **绝不**移动 / 重命名 / 删除 / 改写 inbox 内任何原始 evidence；**绝不**写数据库、**不新增**
  migration / schema、**不联网**；**绝不**修改 ``.ai/PROJECT_STATE.json`` 或解除 blocker；
- **重新绑定（fail-closed）**：plan 必须**当前仍然成立**（用**当前** inbox + ledger + 批准清单
  重新算出的 ``plan_id`` 与条目摘要完全一致），执行结果必须按**内容 SHA-256** 覆盖每条批准且
  ``dry_run=false`` / ``persisted>=1``，recheck 必须存在且不早于显式执行；任一不一致 → 退出码 ``4``
  且**零写入**；
- **没有**任何 intake / ``--no-dry-run`` 参数：本工具**绝不**执行 intake；
- **收据 ≠ 资格**：``intake_executed`` / ``receipt_verified`` 与 ``data_qualification_passed`` /
  ``phase_transition_allowed`` 是**四个独立**字段，后两者**恒为** false；``blocker_active`` /
  ``human_gate_required`` 恒为 true；``PHASE3_3_DATA`` **保持 BLOCKED**；
- 退出码：``0`` 收据生成且执行与复核绑定成功 / ``2`` 参数错误（缺必填参数 / 时区缺失）/
  ``3`` inbox 或输出不可用（含把 ``--out`` 写进 inbox 的拒绝）/ ``4`` plan、执行结果、复核结果或
  ledger 损坏、被篡改或核验不通过（fail-closed，零写入）/ ``5`` 收据生成但**没有任何**仍成立的
  批准（**预期 BLOCKED**）/ ``6`` 锁冲突；失败路径 stdout 为空、stderr 已脱敏。
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
    intake_receipt_exit_code_for,
    render_intake_receipt_summary,
    run_intake_receipt,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（四个输入必填；**没有**任何 intake / 写库参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_intake_receipt",
        description=(
            "Evidence 显式 Intake Receipt：把 GOLD-013 plan、**当前** inbox / review 状态、"
            "人工**显式** Evidence Operator 执行结果与 qualification recheck 绑定成确定性脱敏的"
            "只读收据（内容级 receipt_id）；默认零写入、不联网、不写数据库、绝不执行 intake、"
            "不解除 PHASE3_3_DATA。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        required=True,
        help="**必填**：候选 evidence 的 inbox 目录（GOLD-011 口径；只读扫描其直接子目录）",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="**必填**：GOLD-012 review ledger（**只读**；损坏 / 被篡改 → 退出码 4）",
    )
    parser.add_argument(
        "--approved-list",
        dest="approved_list",
        type=Path,
        required=True,
        help="**必填**：GOLD-012 脱敏批准清单（approved-for-explicit-intake；只读）",
    )
    parser.add_argument(
        "--plan",
        dest="plan",
        type=Path,
        required=True,
        help="**必填**：GOLD-013 intake plan（**只读**；plan 过期 / 被改写 → 退出码 4）",
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
        dest="recheck",
        type=Path,
        default=None,
        help="qualification recheck 结果（`phase33_qualification_recheck`；必须不早于显式执行）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="**唯一**收据写开关（原子写 + 单实例锁；必须位于 inbox 之外；缺省 = 只读）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发覆盖收据）",
    )
    parser.add_argument(
        "--as-of",
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
    """CLI 主入口：执行**一次**收据核验并返回进程退出码（零网络、零数据库写入）。"""
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
        receipt = run_intake_receipt(
            args.inbox_dir,
            moment=run_moment,
            plan_path=args.plan,
            ledger_path=args.ledger,
            approved_list_path=args.approved_list,
            operator_result_paths=tuple(args.operator_results or ()),
            recheck_path=args.recheck,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] intake receipt 未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return intake_receipt_exit_code_for(exc)

    text = (
        json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_intake_receipt_summary(receipt)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] intake receipt 已原子写入：{args.out}"
            "（只读审计产物；仍**不是**资格通过，Phase 切换仍须 L3 人工 Gate）",
            json_output=json_output,
        )
    return EXIT_OK if receipt.receipt_verified else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
