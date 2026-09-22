"""Evidence Inbox 人工复核决策与审计 CLI（**GOLD-012**，纯本地 / 零网络 / 零数据库）。

给 GOLD-011 的只读 inbox 预检补上**人工决策层**：operator 对**显式指纹**（GOLD-011 的内容级
候选包指纹）记录 ``approve`` / ``reject`` / ``needs_changes`` 及最小审计元数据，
并生成**脱敏**的 approved-for-explicit-intake 清单。

用法::

    # ① 只读：列出 ledger 最新决策与当前仍成立的批准（零写入）
    python -m scripts.evidence_review --inbox-dir logs/evidence/inbox \
        --ledger logs/evidence/inbox_review_ledger.json --json

    # ② 记录决策（dry-run：只预览，不落盘；想落盘必须给 --out）
    python -m scripts.evidence_review --inbox-dir logs/evidence/inbox \
        --decision approve --fingerprint <64 位指纹> --reviewer operator-li \
        --reason-code APPROVED_FOR_EXPLICIT_INTAKE --out logs/evidence/inbox_review_ledger.json \
        --approved-out logs/evidence/approved_for_intake.json --json

    # ③ 推翻既有决策：必须**显式**给出新 revision + --override（历史全部保留）
    python -m scripts.evidence_review --inbox-dir logs/evidence/inbox \
        --decision reject --fingerprint <同一指纹> --reviewer operator-li \
        --reason-code REJECTED_AUTHORIZATION_INSUFFICIENT \
        --revision 2 --override --out logs/evidence/inbox_review_ledger.json

安全与边界：

- **纯本地**：只扫描显式 ``--inbox-dir``（只读）；**绝不**移动 / 重命名 / 删除 / 改写 inbox 内
  任何原始 evidence，不联网、不写数据库、不新增 migration/schema、**不自动 intake**；
- **approve 门禁**：必须“该指纹当前仍在扫描结果中 + 当前预检 ``PREFLIGHT_PASS`` +
  非模板 / 示例 / Mock”；内容变化 → 新指纹（**旧批准绝不继承**）；候选消失 / 预检不通过 /
  state 损坏 / 复核元数据与当前证据不一致一律 **fail-closed**；
- **追加式 ledger**：只 append、**绝不静默覆盖**；同一指纹 + 完全相同的决策内容重复提交**幂等**；
  任何差异都必须显式 ``--revision`` + ``--override``（保留全部历史，``supersedes`` 指向前一条）；
- **写开关只有一个**：``--out`` 才写 ledger（``--approved-out`` 才写批准清单），两者**原子写**
  并自带单实例锁；``--ledger`` 只读，损坏 → 退出码 ``4`` 且零写入；
- **批准 ≠ 资格**：``approved-for-explicit-intake`` 只是人工预审清单；落库仍须 operator **显式**
  执行 ``scripts/evidence_operator.py workflow --no-dry-run``，Phase 切换仍须 L3 人工 Gate；
- **复核元数据不是证据**：``--reviewer`` / ``--note`` / 文件名 / mtime / 复核时间都不构成
  ``published_at`` / ``collected_at`` / ``effective_at`` / ``availability`` 证据；
  凭据类内容一律**拒绝记录**；
- 退出码：``0`` 决策已记录（或幂等重复）/ ``2`` 参数或词表错误 / ``3`` inbox 目录或输出不可用
  （含把输出写进 inbox 的拒绝）/ ``4`` ledger 损坏或决策冲突或目标不满足门禁（fail-closed）/
  ``5`` 只读运行且**没有任何**仍成立的批准（预期 BLOCKED）/ ``6`` 锁冲突。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    EXIT_NO_DECISION,
    EXIT_OK,
    REASON_CODES_BY_DECISION,
    LockConflictError,
    LockUnavailableError,
    ReviewDecision,
    ReviewError,
    render_review_summary,
    review_exit_code_for,
    run_review,
)

#: CLI 决策名 → 内部决策（受控词表；三个选项，与任务口径一一对应）
DECISION_CHOICES: Final[tuple[str, ...]] = ("approve", "needs_changes", "reject")
#: 全部允许的原因码（跨决策使用仍会被核心层拒绝）
ALL_REASON_CODES: Final[tuple[str, ...]] = tuple(
    sorted({code for group in REASON_CODES_BY_DECISION.values() for code in group})
)
#: CLI 决策名 → :class:`ReviewDecision`
_DECISION_MAP: Final[dict[str, ReviewDecision]] = {
    "approve": ReviewDecision.APPROVE,
    "reject": ReviewDecision.REJECT,
    "needs_changes": ReviewDecision.NEEDS_CHANGES,
}


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--inbox-dir`` 必填：只扫描**显式**目录；无敏感参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_review",
        description=(
            "Evidence Inbox 人工复核决策与审计：只对显式指纹记录 "
            "approve / reject / needs_changes，追加式 ledger（幂等 / 冲突必须显式 revision）、"
            "脱敏 approved-for-explicit-intake 清单；不联网、不写数据库、不自动 intake、"
            "不移动 / 删除原始 evidence、不解除 PHASE3_3_DATA。"
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
        default=None,
        help="既有 review ledger（**只读**；文件不存在 = 首次运行；损坏 → 退出码 4）",
    )
    parser.add_argument(
        "--decision",
        choices=DECISION_CHOICES,
        default=None,
        help="人工复核决策（省略 = 只读列出 ledger 与批准清单，零写入）",
    )
    parser.add_argument(
        "--fingerprint",
        default=None,
        help="GOLD-011 的 64 位**内容级**候选包指纹（给出 --decision 时必填）",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="复核人**非敏感**标识（给出 --decision 时必填；不得包含凭据）",
    )
    parser.add_argument(
        "--reason-code",
        dest="reason_code",
        choices=ALL_REASON_CODES,
        default=None,
        help="受控原因码（给出 --decision 时必填，且必须与该决策匹配）",
    )
    parser.add_argument(
        "--note",
        default="",
        help="可选**非敏感**备注（<= 300 字符；不得包含凭据，否则拒绝记录）",
    )
    parser.add_argument(
        "--reviewed-at",
        default=None,
        help="复核时点（ISO8601，必须带时区；缺省 = 审计时点；不得晚于审计时点）",
    )
    parser.add_argument(
        "--revision",
        type=int,
        default=None,
        help="显式新 revision（推翻既有决策时必须 = 既有 revision + 1，且需 --override）",
    )
    parser.add_argument(
        "--override",
        action="store_true",
        help="显式承认这是一次推翻（必须与 --revision 同时给出；历史全部保留）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="**唯一** ledger 写开关（原子写 + 单实例锁；必须位于 inbox 目录之外）",
    )
    parser.add_argument(
        "--approved-out",
        type=Path,
        default=None,
        help="批准清单（approved-for-explicit-intake）落盘路径（可选；原子写；必须在 inbox 之外）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发写 ledger）",
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


def _validate_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[ReviewDecision | None, datetime | None]:
    """校验参数组合（任一不合法 → ``parser.error``，退出码 ``2``；零写入）。"""
    decision: ReviewDecision | None = None
    if args.decision is not None:
        decision = _DECISION_MAP[str(args.decision)]
        missing = [
            flag
            for flag, value in (
                ("--fingerprint", args.fingerprint),
                ("--reviewer", args.reviewer),
                ("--reason-code", args.reason_code),
            )
            if not value
        ]
        if missing:
            parser.error("给出 --decision 时必须同时给出 " + "、".join(missing))
        allowed = sorted(REASON_CODES_BY_DECISION[decision])
        if str(args.reason_code).upper() not in allowed:
            parser.error(
                f"--reason-code 必须与决策 {decision.value} 匹配；允许值：{allowed}"
            )
    elif args.revision is not None or args.override:
        parser.error("--revision / --override 只在给出 --decision 时有意义")
    if args.override and args.revision is None:
        parser.error("--override 必须与显式 --revision 同时给出（绝不静默覆盖历史）")
    if args.revision is not None and args.revision < 1:
        parser.error("--revision 必须是 >= 1 的整数")
    reviewed_at = parse_as_of(args.reviewed_at)
    return decision, reviewed_at


def main(
    argv: Sequence[str] | None = None,
    *,
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：执行**一次**复核运行并返回进程退出码（零网络、零数据库写入）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
        decision, reviewed_at = _validate_args(parser, args)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    try:
        report = run_review(
            args.inbox_dir,
            moment=run_moment,
            decision=decision,
            fingerprint=args.fingerprint,
            reviewer=args.reviewer,
            reason_code=args.reason_code,
            note=args.note,
            reviewed_at=reviewed_at,
            revision=args.revision,
            override=bool(args.override),
            ledger_path=args.ledger,
            out_path=args.out,
            approved_out_path=args.approved_out,
            lock_path=args.lock,
        )
    except (ReviewError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] review 未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return review_exit_code_for(exc)

    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_review_summary(report)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] review ledger 已原子写入：{args.out}"
            "（人工预审通过**不等于**资格通过；落库仍须显式执行 "
            "evidence_operator workflow --no-dry-run）",
            json_output=json_output,
        )
    if args.approved_out is not None:
        _notify(
            f"[evidence] approved-for-explicit-intake 清单已原子写入：{args.approved_out}",
            json_output=json_output,
        )
    settled = report.has_decision or report.approved_list.has_approved
    return EXIT_OK if settled else EXIT_NO_DECISION


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
