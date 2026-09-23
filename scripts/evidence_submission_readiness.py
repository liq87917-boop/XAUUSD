"""Phase 3.3 人工证据提交就绪包 CLI（**GOLD-033**，默认只读 / 零网络 / 默认零写入）。

把 GOLD-026 ~ GOLD-030 已有只读结论（缺口诊断 / handoff / intake handoff 契约 /
材料级人工核验 / 端到端链审计）汇总成**单一、确定、可操作**的"业务方还缺什么"清单：

```text
# ① 只读汇总（默认只打印 stdout；零写入、零网络、零数据库写入）
python -m scripts.evidence_submission_readiness --json

# ② 纳入**本地**候选目录（GOLD-027 只读预检）与**当前**凭证核验（GOLD-028）
python -m scripts.evidence_submission_readiness ^
    --inbox-dir logs/evidence/inbox ^
    --attestation logs/evidence/phase33_human_verification_attestation.json --json

# ③ 纳入 GOLD-030 链审计结论（工程链是否一致）
python -m scripts.evidence_submission_readiness ^
    --chain-audit logs/evidence/phase33_evidence_chain_audit.json --json

# ④ 单一 rehearsal：先验证工具链健康（纯本地 fixture；**不**代表真实证据）
python -m scripts.evidence_submission_readiness --rehearsal ^
    --work-dir logs/submission_rehearsal --json

# ⑤ 唯一写开关：显式 --out 原子落盘**本包本身**（仍不写库、不 intake、不解除 blocker）
python -m scripts.evidence_submission_readiness --json ^
    --out logs/evidence/phase33_human_evidence_submission_pack.json

# ⑥ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
python -m scripts.evidence_submission_readiness --as-of 2026-09-23T00:00:00+00:00 --json
```

安全与边界：

- **只读 / 零网络 / 零数据库写入 / 零交易**：只读取显式路径的本地 artifact 与库内只读台账；
  不采集、不写库、不执行 intake，**绝不**移动 / 删除 / 改写 inbox 内原始 evidence、
  历史 evidence artifact、``Review Ledger``、``PROJECT_STATE`` 或 ``.ai/tasks`` / ``.ai/results``；
- **五个独立结论**：``engineering_ready`` / ``submission_materials_complete`` /
  ``human_verification_complete`` / ``data_qualification_passed`` / ``phase_transition_allowed``；
  后两者**恒为 false**，``l3_gate_pending`` / ``blocker_active`` / ``human_gate_required``
  **恒为 true**（**硬编码**），``PHASE3_3_DATA`` 保持 BLOCKED；
- **缺材料时只给事实**：输出稳定 missing reason codes 与人工动作清单（**只引用**既有契约字段 /
  既有阈值 / 既有命令），**绝不生成或推断** ``published_at`` / ``collected_at`` /
  ``effective_at`` / ``available_at`` / OOS 值；
- **Mock / fixture / 模板永不计数**：``--rehearsal`` 只接受 fixture / Mock 输入
  （不得与 ``--inbox-dir`` / ``--attestation`` / ``--chain-audit`` 同时给出），
  且 ``submission_materials_complete`` / ``human_verification_complete`` 恒为 false；
- 退出码：``0`` 无待办缺口（**仍不是**资格通过）/ ``2`` 参数或输入错误（含 inbox 目录缺失）/
  ``3`` 路径不可用（链审计报告 / 凭证 / 输出不可用、把输出写进 inbox、演练工作目录落在仓库
  非 ``logs/`` 之处）/ ``4`` 输入 artifact 损坏 / 被篡改 / 漂移（fail-closed，零写入）/
  ``5`` 仍有待办缺口（**当前预期**：真实材料 / 人工核验 / 工程链至少缺一项）/ ``6`` 锁冲突；
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

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence.chain_audit import (  # noqa: E402
    ChainAuditArgumentError,
    ChainAuditError,
    ChainAuditPathError,
    run_chain_audit,
)
from src.evidence.human_verification_attestation import (  # noqa: E402
    AttestationArgumentError,
    AttestationError,
    AttestationPathError,
)
from src.evidence.inbox import InboxError  # noqa: E402
from src.evidence.rehearsal import (  # noqa: E402
    REHEARSAL_SCENARIOS,
    RehearsalArgumentError,
    RehearsalError,
    RehearsalPathError,
    build_rehearsal_chain,
    ensure_rehearsal_work_dir,
)
from src.evidence.submission_readiness import (  # noqa: E402
    EXIT_CONFIG_ERROR,
    EXIT_UNUSABLE,
    SubmissionPackArgumentError,
    SubmissionPackError,
    SubmissionReadinessPack,
    chain_binding_from_audit,
    load_chain_binding,
    main_pack_exit_code,
    render_submission_pack_markdown,
    run_submission_pack,
    submission_pack_exit_code_for,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项；无任何 intake / 写库 / 放行参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_submission_readiness",
        description=(
            "Phase 3.3 人工证据提交就绪包（默认只读 / 零网络 / 默认零写入）：汇总当前真实材料缺口、"
            "人工核验要求与 L3 待办；任何结果都不会解除 PHASE3_3_DATA、也不会推进 L3 / L4。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        default=None,
        help="可选的本地候选目录（GOLD-027 只读预检；不联网、不 ingest）",
    )
    parser.add_argument(
        "--attestation",
        type=Path,
        default=None,
        help="可选的 GOLD-028 材料级人工核验凭证（**必须**与 --inbox-dir 同时给出才可比对）",
    )
    parser.add_argument(
        "--chain-audit",
        type=Path,
        default=None,
        help="可选的 GOLD-030 链审计报告 JSON（只读取事实，不重算）",
    )
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="纯本地演练：在 --work-dir 内生成 fixture 链并核验工具链健康（**不是**真实证据）",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="演练工作目录（只在 --rehearsal 下有意义；仓库内仅允许 logs/ 之下）",
    )
    parser.add_argument(
        "--scenario",
        default="ready",
        choices=list(REHEARSAL_SCENARIOS),
        help="演练场景（只在 --rehearsal 下有意义）",
    )
    parser.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="稳定 JSON")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="唯一写开关：显式给出才原子落盘本包（缺省只打印 stdout）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="可选的输出锁文件路径（缺省与 --out 同级；仅在 --out 时使用）",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """参数互斥校验（fail-closed：演练模式绝不与真实输入混算）。"""
    real_inputs = {
        "--inbox-dir": args.inbox_dir,
        "--attestation": args.attestation,
        "--chain-audit": args.chain_audit,
    }
    given = [flag for flag, value in real_inputs.items() if value]
    if args.rehearsal:
        if args.work_dir is None:
            parser.error("--rehearsal 必须显式给出 --work-dir（只在该目录内写 fixture）")
        if given:
            parser.error(
                "--rehearsal 是纯本地演练模式：不得与真实输入参数同时给出"
                f"（实际给出：{'、'.join(given)}）"
            )
        return
    if args.work_dir is not None or args.scenario != "ready":
        parser.error("--work-dir / --scenario 只在 --rehearsal 模式下有意义")
    if args.attestation is not None and args.inbox_dir is None:
        parser.error("--attestation 必须与 --inbox-dir 同时给出（凭证必须与**当前**候选目录比对）")


def _render(pack: SubmissionReadinessPack, *, json_output: bool) -> str:
    """渲染输出（稳定 JSON 或人类可读 Markdown）。"""
    if json_output:
        return json.dumps(pack.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_submission_pack_markdown(pack)


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息统一走 stderr（``--json`` 时 stdout 必须是纯 JSON）。"""
    if json_output:
        print(message, file=sys.stderr)


def _cli_exit_code(error: BaseException) -> int:
    """把各层 fail-closed 异常映射为**稳定**退出码（参数 / 输入非法 → ``2``）。"""
    if isinstance(
        error,
        (
            InboxError,
            ValueError,
            SubmissionPackArgumentError,
            RehearsalArgumentError,
            ChainAuditArgumentError,
            AttestationArgumentError,
        ),
    ):
        return EXIT_CONFIG_ERROR
    if isinstance(error, (RehearsalPathError, ChainAuditPathError, AttestationPathError)):
        return EXIT_UNUSABLE
    return submission_pack_exit_code_for(error)


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：生成**一次**提交就绪包（零网络、零数据库写入、默认零写入）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
        _validate_args(parser, args)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = (
        moment if moment is not None else (as_of if as_of is not None else datetime.now(UTC))
    )
    json_output = bool(args.json_output)
    factory = session_factory or build_session_factory(build_engine())
    try:
        if args.rehearsal:
            chain = build_rehearsal_chain(
                args.work_dir, moment=run_moment, scenario=args.scenario
            )
            audit = run_chain_audit(
                chain.to_chain_inputs(),
                moment=run_moment,
                evidence_source="rehearsal_fixture",
                scenario=chain.scenario,
            )
            out = ensure_rehearsal_work_dir(args.out) if args.out is not None else None
            with factory() as session:
                try:
                    pack = run_submission_pack(
                        session,
                        moment=run_moment,
                        chain=chain_binding_from_audit(audit),
                        evidence_source="rehearsal_fixture",
                        out_path=out,
                        lock_path=args.lock,
                    )
                finally:
                    # 只读台账：显式回滚，杜绝任何隐式写入
                    session.rollback()
            if out is not None:  # pragma: no cover - 仅在显式 --out 时执行
                _notify(
                    f"[evidence] 提交就绪包（演练）已原子写入：{out}"
                    "（演练**不是**真实证据；PHASE3_3_DATA 仍 BLOCKED）",
                    json_output=json_output,
                )
        else:
            binding = None if args.chain_audit is None else load_chain_binding(args.chain_audit)
            with factory() as session:
                try:
                    pack = run_submission_pack(
                        session,
                        moment=run_moment,
                        inbox_dir=args.inbox_dir,
                        attestation_path=args.attestation,
                        chain=binding,
                        out_path=args.out,
                        lock_path=args.lock,
                    )
                finally:
                    session.rollback()
            if args.out is not None:  # pragma: no cover - 仅在显式 --out 时执行
                _notify(
                    f"[evidence] 提交就绪包已原子写入：{args.out}"
                    "（只读汇总；**不是**资格通过，Phase 切换仍须 L3 人工 Gate）",
                    json_output=json_output,
                )
    except (
        SubmissionPackError,
        RehearsalError,
        ChainAuditError,
        AttestationError,
        InboxError,
        OSError,
        ValueError,
    ) as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] 提交就绪包未完成（fail-closed，未写入任何 artifact）："
            f"{type(exc).__name__}: {safe_text(str(exc))}",
            file=sys.stderr,
        )
        return _cli_exit_code(exc)
    safe_print(_render(pack, json_output=json_output))
    return main_pack_exit_code(pack)


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
