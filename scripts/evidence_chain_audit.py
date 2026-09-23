"""Phase 3.3 证据链端到端只读审计 / 纯本地演练 CLI（**GOLD-030**，默认只读）。

把 ``GOLD-027 handoff → GOLD-028 材料级人工核验凭证 → GOLD-012/029 review 批准 →
GOLD-013 plan → 人工显式执行结果 → GOLD-014 receipt/recheck → GOLD-015 packet →
GOLD-016 L3 决策记录`` 串成**一条**可重复的端到端验收链：逐段核验 artifact identity /
schema / content binding，任一漂移**fail-closed** 并给出**最早失败阶段**与稳定原因码。

用法::

    # ① 只读审计既有 artifact（零写入、零网络、零数据库；stdout 为纯 JSON）
    python -m scripts.evidence_chain_audit --inbox-dir logs/evidence/inbox \\
        --handoff logs/evidence/handoff.json \\
        --attestation logs/evidence/phase33_human_verification_attestation.json \\
        --ledger logs/evidence/inbox_review_ledger.json \\
        --approved-list logs/evidence/approved_for_intake.json \\
        --plan logs/evidence/evidence_intake_plan.json \\
        --operator-result logs/evidence/author_manifest.json \\
        --recheck logs/evidence/phase33_recheck.json \\
        --readiness logs/evidence/readiness_state.json \\
        --receipt logs/evidence/evidence_intake_receipt.json \\
        --packet logs/evidence/evidence_decision_packet.json \\
        --record logs/evidence/l3_human_decision_record.json --json

    # ② 单一 rehearsal 命令（业务方在提交真实证据前先验证工具链健康）：
    #    在显式工作目录内生成 fixture 链并审计；仓库内仅允许 logs/ 之下
    python -m scripts.evidence_chain_audit --rehearsal --work-dir logs/chain_rehearsal --json

    # ③ 唯一写开关：显式 --out 原子落盘审计报告本身（仍不写库、不 intake、不解除 blocker）
    python -m scripts.evidence_chain_audit --rehearsal --work-dir logs/chain_rehearsal \\
        --json --out logs/evidence/phase33_evidence_chain_audit.json

    # ④ 固定审计时点（ISO8601 必须带时区；用于人工复现 / 培训，不改变任何口径）
    python -m scripts.evidence_chain_audit --rehearsal --work-dir logs/chain_rehearsal \\
        --as-of 2026-09-23T00:00:00+00:00 --json

安全与边界：

- **只读 / 零网络 / 零数据库 / 零交易**：只读取显式路径的本地 artifact，不采集、不写库、
  不调用任何 intake / commit / 交易代码，**绝不**移动 / 删除 / 改写任何原始 evidence
  或历史 artifact；
- **演练只写显式 ``--work-dir``**：仓库内仅允许 ``logs/`` 之下（其它仓库路径一律拒绝）；
  演练的"人工显式执行结果"是**显式标注的 Mock manifest**，**绝不**真实落库；
- **fail-closed + 最早失败阶段**：任一段缺失 / 损坏 / 被篡改 / 漂移 → 该段 ``FAIL``（或
  ``MISSING``）+ 稳定原因码，后续阶段 ``NOT_EVALUATED``；
- **工程链全绿也不放行**：``engineering_chain_ready=true`` **只**表示工具链契约一致；
  ``data_qualification_passed`` / ``phase_transition_allowed`` **恒为 false**、
  ``blocker_active`` / ``human_gate_required`` 恒为 true（**硬编码**），
  ``PHASE3_3_DATA`` 保持 BLOCKED，L3 / L4 只能人工决策；
- 退出码：``0`` 工程链全部 PASS（**不是**资格通过）/ ``2`` 参数或时区错误 /
  ``3`` 路径不可用（inbox 目录或输出不可用、把输出写进 inbox、演练工作目录落在仓库
  非 ``logs/`` 之处）/ ``4`` artifact 损坏 / 被篡改 / 漂移或校验失败（fail-closed，
  零写入）/ ``5`` 证据链未走通（**预期 BLOCKED**，含 stage ``FAIL`` / ``MISSING``）/
  ``6`` 锁冲突；失败路径 stdout 为空、stderr 已脱敏。
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
from src.evidence.chain_audit import (  # noqa: E402
    ChainAuditError,
    ChainAuditInputs,
    EvidenceChainAudit,
    chain_audit_exit_code_for,
    main_audit_exit_code,
    render_chain_audit_summary,
    run_chain_audit,
)
from src.evidence.rehearsal import (  # noqa: E402
    REHEARSAL_SCENARIOS,
    RehearsalError,
    build_rehearsal_chain,
    ensure_rehearsal_work_dir,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（**没有**任何 intake / 写库 / qualify / advance / 交易参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_chain_audit",
        description=(
            "Phase 3.3 证据链端到端**只读**审计 / 纯本地演练：逐段核验 GOLD-027 → GOLD-028 → "
            "GOLD-012/029 → GOLD-013 → GOLD-014 → GOLD-015 → GOLD-016 的 artifact identity / "
            "schema / content binding；默认只打印 stdout（零写入、零网络、零数据库），"
            "`--out` 是唯一写开关（只写审计报告本身）；演练 `--rehearsal` 只在显式 `--work-dir` "
            "内写 fixture。**绝不**解除 PHASE3_3_DATA、**绝不**自动推进 L3/L4、"
            "**绝不**把 fixture / Mock 当作真实资格证据。"
        ),
    )
    _add_artifact_arguments(parser)
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help=(
            "纯本地演练：在显式 --work-dir 内生成完整 fixture 证据链（含**Mock** 执行结果）"
            "并审计；零网络 / 零数据库 / 不真实落库"
        ),
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="演练工作目录（--rehearsal 必填；仓库内仅允许 logs/ 之下）",
    )
    parser.add_argument(
        "--scenario",
        choices=list(REHEARSAL_SCENARIOS),
        default="ready",
        help=(
            "演练场景：ready = 量化达标控制流（记录 approve）/ blocked = 诚实 BLOCKED"
            "（packet 不可提交，记录 reject）"
        ),
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出稳定 JSON")
    parser.add_argument("--as-of", default=None, help="固定审计时点（ISO8601 必须带时区）")
    parser.add_argument("--out", default=None, help="唯一写开关：原子落盘审计报告本身")
    parser.add_argument("--lock", default=None, help="单实例锁路径（默认与 --out 同级）")
    return parser


def _add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    """登记**显式** artifact 参数（缺省 = 该段未提供 → 对应阶段 MISSING）。"""
    parser.add_argument("--inbox-dir", default=None, help="显式本地候选目录（只读）")
    parser.add_argument("--handoff", dest="handoff_report", default=None, help="GOLD-008 handoff")
    parser.add_argument("--attestation", default=None, help="GOLD-028 材料级人工核验凭证")
    parser.add_argument("--ledger", default=None, help="GOLD-012 review ledger")
    parser.add_argument("--approved-list", default=None, help="GOLD-012 批准清单")
    parser.add_argument("--plan", default=None, help="GOLD-013 intake plan")
    parser.add_argument(
        "--operator-result",
        action="append",
        default=None,
        help="人工**显式**执行结果 manifest（可重复；演练时为 Mock）",
    )
    parser.add_argument("--recheck", default=None, help="qualification recheck 结果")
    parser.add_argument("--readiness", default=None, help="GOLD-010 readiness state 快照")
    parser.add_argument("--receipt", default=None, help="GOLD-014 intake receipt")
    parser.add_argument("--packet", default=None, help="GOLD-015 L3 决策包")
    parser.add_argument("--record", default=None, help="GOLD-016 L3 人工决策记录")


def _inputs_from_args(args: argparse.Namespace) -> ChainAuditInputs:
    """把 CLI 参数转换为审计输入（**只**使用显式给出的路径）。"""
    return ChainAuditInputs(
        inbox_dir=str(args.inbox_dir),
        handoff_report=args.handoff_report,
        attestation=args.attestation,
        ledger=args.ledger,
        approved_list=args.approved_list,
        plan=args.plan,
        operator_results=tuple(args.operator_result or ()),
        recheck=args.recheck,
        readiness=args.readiness,
        receipt=args.receipt,
        packet=args.packet,
        record=args.record,
    )


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """参数互斥 / 必填校验（fail-closed：宁可报错也不猜路径）。"""
    artifact_flags = {
        "--inbox-dir": args.inbox_dir,
        "--handoff": args.handoff_report,
        "--attestation": args.attestation,
        "--ledger": args.ledger,
        "--approved-list": args.approved_list,
        "--plan": args.plan,
        "--operator-result": args.operator_result,
        "--recheck": args.recheck,
        "--readiness": args.readiness,
        "--receipt": args.receipt,
        "--packet": args.packet,
        "--record": args.record,
    }
    given = [flag for flag, value in artifact_flags.items() if value]
    if args.rehearsal:
        if args.work_dir is None:
            parser.error("--rehearsal 必须显式给出 --work-dir（只在该目录内写 fixture）")
        if given:
            parser.error(
                "--rehearsal 是纯本地演练模式：不得与任何 artifact 参数同时给出"
                f"（实际给出：{'、'.join(given)}）"
            )
        return
    if args.inbox_dir is None:
        parser.error("必须显式给出 --inbox-dir（本地候选目录），或使用 --rehearsal --work-dir")
    if args.work_dir is not None or args.scenario != "ready":
        parser.error("--work-dir / --scenario 只在 --rehearsal 模式下有意义")


def _render(audit: EvidenceChainAudit, *, json_output: bool) -> str:
    """渲染审计输出（JSON 或人类可读摘要）。"""
    if json_output:
        return json.dumps(audit.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_chain_audit_summary(audit)


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息统一走 stderr（``--json`` 时 stdout 必须是纯 JSON）。"""
    if json_output:
        print(message, file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：执行**一次**证据链审计（零网络、零数据库、默认零写入）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
        _validate_args(parser, args)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    try:
        if args.rehearsal:
            chain = build_rehearsal_chain(
                args.work_dir, moment=run_moment, scenario=args.scenario
            )
            out = ensure_rehearsal_work_dir(args.out) if args.out is not None else None
            audit = run_chain_audit(
                chain.to_chain_inputs(),
                moment=run_moment,
                evidence_source="rehearsal_fixture",
                scenario=chain.scenario,
                out_path=out,
                lock_path=args.lock,
            )
            if out is not None:  # pragma: no cover - 仅在显式 --out 时执行
                _notify(
                    f"[evidence] 证据链审计报告（演练）已原子写入：{out}"
                    "（演练**不是**真实资格证据；PHASE3_3_DATA 仍 BLOCKED）",
                    json_output=json_output,
                )
        else:
            audit = run_chain_audit(
                _inputs_from_args(args),
                moment=run_moment,
                out_path=args.out,
                lock_path=args.lock,
            )
            if args.out is not None:  # pragma: no cover - 仅在显式 --out 时执行
                _notify(
                    f"[evidence] 证据链审计报告已原子写入：{args.out}"
                    "（只读审计结论；**不是**资格通过，Phase 切换仍须 L3 人工 Gate）",
                    json_output=json_output,
                )
    except (ChainAuditError, RehearsalError, RuntimeError) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] 证据链审计未完成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return chain_audit_exit_code_for(exc)
    safe_print(_render(audit, json_output=json_output))
    return main_audit_exit_code(audit)


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())

