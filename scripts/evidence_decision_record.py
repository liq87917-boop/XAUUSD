"""L3 人工决策记录 CLI（**GOLD-016**，纯本地 / 显式人工输入 / 默认只读预检）。

把**人工显式**给出的 L3 Gate 决策（``approve`` / ``reject`` / ``needs_changes``）绑定到**具体**的
GOLD-015 决策包（``packet_id`` + 内容摘要 + 产物摘要），生成**确定性、脱敏、内容寻址**
（``record_id``）的人工决策记录；**不做**任何 intake，**不写数据库**，**不修改** ``PROJECT_STATE``，
**不解除** ``PHASE3_3_DATA``，**不切换** Phase。

用法::

    # ① 只读预检：打印"将要写入"的决策记录（零写入）
    python -m scripts.evidence_decision_record \\
        --packet logs/evidence/evidence_decision_packet.json \\
        --decision approve --reviewer operator-l3-li \\
        --reason-code APPROVED_AFTER_L3_REVIEW --json

    # ② 唯一写开关：显式 --out 原子落盘记录（仍**不**写数据库、**不**改 PROJECT_STATE）
    python -m scripts.evidence_decision_record ... --out logs/evidence/l3_human_decision_record.json

    # ③ 覆盖既有记录必须**显式**声明 revision + supersedes（绝不静默改写历史）
    python -m scripts.evidence_decision_record ... --revision 2 --supersedes <record_id> --out ...

    # ④ 防伪核验（纯只读）：把既有记录与**当前** packet 逐项比对
    python -m scripts.evidence_decision_record \\
        --packet logs/evidence/evidence_decision_packet.json \\
        --verify-record logs/evidence/l3_human_decision_record.json --json

    # ⑤ 固定审计时点（ISO8601 必须带时区；用于人工复现）
    python -m scripts.evidence_decision_record ... --as-of 2026-09-23T04:00:00+00:00 --json

安全与边界：

- **纯本地 / 显式人工输入**：``--decision`` 与 ``--reviewer`` **必须**由人工显式给出，工具
  **绝不**自行生成批准、**绝不**推断人工意图；**不采集**任何凭据（reviewer 只接受非敏感 label）；
- **approve 必须落在可提交的 packet 上**：packet 本身 ``submit_to_l3_human_gate=false`` 时
  ``approve`` 一律被拒（退出码 ``5``，零写入）；``reject`` / ``needs_changes`` 可被记录，但
  **绝不**改变任何资格状态；
- **fail-closed**：packet 缺失 / 损坏 / 被篡改 / 内容或 ``packet_id`` 不一致 / 出现证据时间键 /
  出现未来时间 / 与既有记录冲突 / 并发锁冲突 → 稳定原因码 + **零写入**（退出码 ``3`` / ``4`` /
  ``6``）；
- **默认零写入**：只有显式 ``--out`` 才（先取单实例锁）**原子**落盘记录本身；本工具**没有**任何
  intake / ``--no-dry-run`` / 数据库参数，**绝不**修改 ``PROJECT_STATE``、**绝不**解除 blocker；
- **区分语义**：``human_decision_recorded`` / ``human_decision`` / ``packet_verified`` 三个独立
  事实；``data_qualification_passed`` / ``phase_transition_allowed`` /
  ``phase_transition_executed`` **恒为** false、``blocker_active`` / ``human_gate_required``
  **恒为** true、``human_gate_level`` 恒为 ``L3``（**硬编码**）；``PHASE3_3_DATA`` 保持 BLOCKED；
- 退出码：``0`` 记录已生成（人工决策已记录；仍**不是**资格通过）/ ``2`` 参数错误（缺必填参数 /
  互斥参数冲突 / 时区缺失）/ ``3`` 路径或输出不可用（含把记录写到 packet 文件上的拒绝）/
  ``4`` 任一输入缺失 / 损坏 / 篡改 / stale / ``record_id`` 不一致或防伪核验不通过（fail-closed，
  零写入）/ ``5`` ``approve`` 被拒（packet 不可提交，**预期 BLOCKED**）/ ``6`` 锁冲突；
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
    DECISION_RECORD_ACTIONS,
    DECISION_RECORD_EXECUTION_MODE,
    EXIT_OK,
    MAX_RECORD_NOTE_CHARS,
    DecisionRecordVerification,
    LockConflictError,
    LockUnavailableError,
    decision_record_exit_code_for,
    main_verification_exit_code,
    render_decision_record_summary,
    run_decision_record,
    verify_decision_record,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（**没有**任何 intake / 写库 / 修改 PROJECT_STATE 的参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_decision_record",
        description=(
            "L3 人工决策记录：把**人工显式**给出的 approve / reject / needs_changes 绑定到"
            "**具体**的 GOLD-015 packet（packet_id + 内容摘要 + 产物摘要）；默认只读预检，"
            "只有显式 --out 才原子落盘记录本身。"
        ),
    )
    parser.add_argument(
        "--packet",
        type=Path,
        required=True,
        help="GOLD-015 决策包（`evidence_decision_packet --out` 产物；本工具只读并逐项核验）",
    )
    parser.add_argument(
        "--decision",
        choices=DECISION_RECORD_ACTIONS,
        default=None,
        help="**人工显式**决策（approve / reject / needs_changes）；工具绝不自行生成",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="**人工显式**、非敏感的 operator / reviewer label（不采集任何凭据）",
    )
    parser.add_argument(
        "--note",
        default=None,
        help=f"可选人工说明（脱敏 + 截断至 {MAX_RECORD_NOTE_CHARS} 字符；过长输入 fail-closed）",
    )
    parser.add_argument(
        "--reason-code",
        dest="reason_code",
        default=None,
        help="可选稳定原因码（大写形态，例如 APPROVED_AFTER_L3_REVIEW）",
    )
    parser.add_argument(
        "--revision",
        type=int,
        default=1,
        help="记录 revision（默认 1；覆盖既有记录必须显式 > 既有 revision 并给出 --supersedes）",
    )
    parser.add_argument(
        "--supersedes",
        default=None,
        help="被取代记录的 record_id（覆盖既有记录时**必须**显式给出）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="**唯一**记录写开关（原子写 + 单实例锁；不得指向 packet 文件本身；缺省 = 只读）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = ``--out`` + ``.lock``；防并发覆盖记录）",
    )
    parser.add_argument(
        "--verify-record",
        dest="verify_record",
        type=Path,
        default=None,
        help="纯只读防伪核验模式：把该记录与 ``--packet`` 逐项比对（不得与写参数同用）",
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


def _render_verification_summary(verification: DecisionRecordVerification) -> str:
    """渲染人类可读的防伪核验摘要（脱敏；**不代表**资格通过）。"""
    lines: list[str] = [
        "# L3 人工决策记录防伪核验（只读；**不是**资格判定）",
        "",
        f"- 记录：`{verification.record_path}`；packet：`{verification.packet_path}`",
        f"- record_id：`{verification.record_id}`；重新推导："
        f"`{verification.recomputed_record_id}`（一致："
        f"{str(verification.record_id_matches).lower()}）",
        f"- 决策：`{verification.decision}`；reviewer：`{verification.reviewer}`；"
        f"revision：{verification.revision}；supersedes：`{verification.supersedes or '—'}`",
        f"- packet_id 一致：{str(verification.packet_id_matches).lower()}；内容摘要一致："
        f"{str(verification.content_sha256_matches).lower()}",
        f"- 当初的 approve 是否仍成立：{verification.approve_still_valid}",
        f"- 结论：verified = {str(verification.verified).lower()}；"
        f"原因码：{list(verification.codes) or '—'}",
        "",
        f"- 执行模式：`{DECISION_RECORD_EXECUTION_MODE}`；blocker 仍为 active（BLOCKED 未解除）；"
        "`data_qualification_passed` / `phase_transition_allowed` 恒为 false。",
        "",
    ]
    if verification.details:
        lines += ["## 不一致明细", "", *[f"- {item}" for item in verification.details], ""]
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：执行**一次**决策记录或防伪核验，返回进程退出码（零网络、零数据库）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    if args.verify_record is not None:
        if any(
            item is not None
            for item in (
                args.out,
                args.decision,
                args.reviewer,
                args.note,
                args.reason_code,
                args.supersedes,
            )
        ):
            parser.error(
                "--verify-record 是纯只读防伪核验模式：不得与 --decision / --reviewer / --note / "
                "--reason-code / --supersedes / --out 同时给出"
            )
        try:
            verification = verify_decision_record(
                args.verify_record, args.packet, moment=run_moment
            )
        except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
            print(
                "[evidence] decision record 防伪核验未完成（fail-closed，未写入任何 artifact）："
                f"{safe_text(str(exc))}",
                file=sys.stderr,
            )
            return decision_record_exit_code_for(exc)
        text = (
            json.dumps(verification.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            if json_output
            else _render_verification_summary(verification)
        )
        safe_print(text)
        return main_verification_exit_code(verification.verified)
    if args.decision is None or args.reviewer is None:
        parser.error(
            "记录人工决策必须**显式**给出 --decision（approve / reject / needs_changes）"
            "与 --reviewer（非敏感 label）；工具绝不自行生成批准"
        )
    try:
        record = run_decision_record(
            args.packet,
            decision=args.decision,
            reviewer=args.reviewer,
            moment=run_moment,
            note=args.note,
            reason_code=args.reason_code,
            revision=args.revision,
            supersedes=args.supersedes,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] decision record 未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return decision_record_exit_code_for(exc)
    text = (
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_decision_record_summary(record)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] L3 人工决策记录已原子写入：{args.out}"
            "（人工决策审计产物；**不是**数据资格通过，Phase 切换仍须 L3 人工 Gate）",
            json_output=json_output,
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
