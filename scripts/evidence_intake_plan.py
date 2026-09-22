"""Evidence Approved-for-Explicit-Intake **Intake Plan** CLI（**GOLD-013**，纯本地只读）。

把 GOLD-012 的批准清单与**当前** inbox / review ledger 重新绑定核验，输出**确定性、脱敏、只读**的
intake plan（内容级 ``plan_id``），供人工在**显式**执行 Evidence Operator intake 之前复核。

用法::

    # ① 只读核验并打印计划（零写入）
    python -m scripts.evidence_intake_plan \
        --inbox-dir logs/evidence/inbox \
        --ledger logs/evidence/inbox_review_ledger.json \
        --approved-list logs/evidence/approved_for_intake.json --json

    # ② 唯一写开关：显式 --out 原子落盘计划本身（仍**不**写数据库、**不**自动 intake）
    python -m scripts.evidence_intake_plan \
        --inbox-dir logs/evidence/inbox \
        --ledger logs/evidence/inbox_review_ledger.json \
        --approved-list logs/evidence/approved_for_intake.json \
        --out logs/evidence/evidence_intake_plan.json --json

    # ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
    python -m scripts.evidence_intake_plan --inbox-dir <inbox> --ledger <ledger> \
        --approved-list <approved> --as-of 2026-09-23T00:00:00+00:00 --json

安全与边界：

- **纯本地 / 只读**：只**读**显式 inbox / ledger / approved list；**默认零写入**，只有显式
  ``--out`` 才**原子**落盘计划（先取单实例锁）；**绝不**移动 / 重命名 / 删除 / 改写 inbox 内任何
  原始 evidence；**绝不**写数据库、**不新增** migration / schema、**不联网**；
- **最终写入前重新核验（fail-closed）**：批准清单结构自洽 + 每条批准必须是 ledger 上该指纹的
  **最新有效**决策（``revision`` / ``decision_id`` 完全一致，``review override`` 后旧批准失效）+
  该指纹**当前仍在** inbox 且仍 ``PREFLIGHT_PASS``、非模板 / 示例 / Mock、摘要与复核时一致 +
  清单与"用当前 inbox + ledger 重新算出的批准集合"完全一致；任一不一致 → 退出码 ``4`` 且**零写入**；
- **没有**任何 intake / ``--no-dry-run`` 参数：本工具**绝不**自动 intake；计划里的 ``handoff``
  只是**字符串**命令模板（必带 ``--no-dry-run`` 与显式 ``--input``），真实落库仍须人工**显式**执行
  ``scripts/evidence_operator.py workflow --no-dry-run``；
- **计划 ≠ 资格**：``approved_for_explicit_intake`` 与 ``data_qualification_passed`` 是两个字段，
  后者**恒为** ``false``（``data_qualification_passed_count`` 恒为 ``0``）；``blocker_active`` /
  ``human_gate_required`` 恒为 true，``phase_transition_allowed`` 恒为 false，``PHASE3_3_DATA``
  **保持 BLOCKED**；
- 退出码：``0`` 计划生成成功且**至少一条**批准仍成立 / ``2`` 参数错误（缺必填参数 / 时区缺失）/
  ``3`` inbox 或输出不可用（含把 ``--out`` 写进 inbox 的拒绝）/ ``4`` 批准清单 / ledger 损坏、
  被篡改或核验不通过（fail-closed，零写入）/ ``5`` 计划生成成功但**没有任何**仍成立的批准
  （**预期 BLOCKED**）/ ``6`` 锁冲突；失败路径 stdout 为空、stderr 已脱敏。
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
    intake_plan_exit_code_for,
    render_intake_plan_summary,
    run_intake_plan,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（三个输入全部必填；**没有**任何 intake / 写库参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_intake_plan",
        description=(
            "Evidence approved-for-explicit-intake Intake Plan：把 GOLD-012 批准清单与**当前** "
            "inbox / review ledger 重新绑定核验，输出确定性脱敏的只读计划（内容级 plan_id）；"
            "默认零写入、不联网、不写数据库、绝不自动 intake、不解除 PHASE3_3_DATA。"
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
        "--out",
        type=Path,
        default=None,
        help="**唯一**计划写开关（原子写 + 单实例锁；必须位于 inbox 之外；缺省 = 只读）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发覆盖计划）",
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
    """CLI 主入口：执行**一次**计划生成并返回进程退出码（零网络、零数据库写入）。"""
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
        plan = run_intake_plan(
            args.inbox_dir,
            moment=run_moment,
            ledger_path=args.ledger,
            approved_list_path=args.approved_list,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] intake plan 未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return intake_plan_exit_code_for(exc)

    text = (
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_intake_plan_summary(plan)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] intake plan 已原子写入：{args.out}"
            "（只读核验产物；真实落库仍须人工**显式**执行 "
            "evidence_operator workflow --no-dry-run）",
            json_output=json_output,
        )
    return EXIT_OK if plan.has_approved else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())

