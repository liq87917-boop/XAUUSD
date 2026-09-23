"""Phase 3.3 材料级人工核验凭证 CLI（**GOLD-028**，纯本地 / 默认零写入）。

把 GOLD-027 的 intake handoff **结构完整预检**进一步转换为**可审计的材料级人工核验凭证**：
对每个必核验材料记录受控决策（``VERIFIED`` / ``REJECTED`` / ``NEEDS_CHANGES``）、稳定 reason
code、显式 reviewer、带时区的 ``reviewed_at`` 与独立 evidence reference，并**硬绑定**当前候选
package fingerprint + GOLD-027 handoff / manifest 内容身份 + scope。

用法::

    # ① 打印版本化凭证契约（零写入、零网络）
    python -m scripts.evidence_human_verification_attestation --schema

    # ② 只读预检：按人工显式给出的核验输入生成凭证（默认只打印 stdout）
    python -m scripts.evidence_human_verification_attestation ^
        --inbox-dir logs/evidence/inbox --verification logs/evidence/human_verification.json --json

    # ③ 唯一写开关：显式 --out 原子落盘凭证（仍不写库、不 intake、不解除 blocker）
    python -m scripts.evidence_human_verification_attestation ^
        --inbox-dir logs/evidence/inbox --verification logs/evidence/human_verification.json ^
        --json --out logs/evidence/phase33_human_verification_attestation.json

    # ④ 覆盖既有凭证必须**显式**声明 revision + supersedes（绝不静默改写历史）
    python -m scripts.evidence_human_verification_attestation ... --revision 2 --supersedes <id>

    # ⑤ 防伪核验（纯只读）：重新绑定当前 package 并检测 stale / tampered / missing reference
    python -m scripts.evidence_human_verification_attestation ^
        --inbox-dir logs/evidence/inbox ^
        --verify-attestation logs/evidence/phase33_human_verification_attestation.json --json

安全与边界：

- **只读 / 零网络 / 零数据库**：只扫描显式 ``--inbox-dir``（复用 GOLD-027 只读 handoff），
  只读取显式 ``--verification`` 本地 JSON；不抓取站点、不绕过 robots / 条款 / 证书、
  不移动 / 删除 / 改写任何原始证据、不写库、不调用任何 intake / commit 路径；
- **默认零写入**：唯一写开关是显式 ``--out``（原子写），且拒绝写进候选目录或覆盖核验输入；
- **复制不解除 blocker**：``evidence_qualified`` / ``data_qualification_passed`` /
  ``phase_transition_allowed`` / ``l3_l4_auto_advance_allowed`` / ``advance_allowed`` 恒为 false；
  ``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` 恒为 true（**硬编码**）；
- **``all_required_verified`` 只代表材料级人工核验完成**：绝不等于数据资格通过，也不推进 Phase；
- **Mock / 模板 / 示例 / 合成、preflight 未通过、材料不可核验**一律 fail-closed（拒绝 VERIFIED）。
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
from src.evidence.human_verification_attestation import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_WORKDIR_UNUSABLE,
    AttestationError,
    AttestationVerification,
    HumanVerificationAttestation,
    attestation_schema,
    exit_code_for,
    main_verification_exit_code,
    render_attestation_summary,
    run_attestation,
    verify_attestation,
)
from src.evidence.readiness_watch import atomic_write_text  # noqa: E402

#: 别名：与模块退出码同一口径（参数错误由 argparse 以 ``2`` 退出）
EXIT_UNUSABLE: int = EXIT_WORKDIR_UNUSABLE
EXIT_INVALID: int = EXIT_STATE_INVALID
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（无任何 intake / 写库 / qualify / approve / advance 参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_human_verification_attestation",
        description=(
            "Phase 3.3 材料级人工核验凭证：只做纯本地只读预检（默认只打印 stdout、零网络、"
            "零数据库、零写入）；`all_required_verified` **只**代表材料级人工核验完成，"
            "不代表 evidence qualified，也不会解除 PHASE3_3_DATA，更不会推进 L3 / L4。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        default=None,
        help="显式本地候选目录（候选包 = 其直接子目录或单文件；除 --schema 外必填）",
    )
    parser.add_argument(
        "--package",
        default=None,
        help="候选包选择器（目录名 / 目录路径 / fingerprint；只有一个候选包时可省略）",
    )
    parser.add_argument(
        "--verification",
        type=Path,
        default=None,
        help="**必填**（除 --schema / --verify-attestation 外）：人工核验输入 JSON 文件",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        dest="schema_output",
        help="打印版本化凭证契约（JSON；不需要 --inbox-dir）",
    )
    parser.add_argument(
        "--verify-attestation",
        type=Path,
        default=None,
        help="纯只读防伪核验模式：重新绑定当前 package 并检测漂移 / 篡改 / 缺引用",
    )
    parser.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
    )
    parser.add_argument(
        "--revision", type=int, default=1, help="凭证修订号（覆盖既有凭证时必须 >= 2）"
    )
    parser.add_argument(
        "--supersedes", default=None, help="被取代凭证的 attestation_id（覆盖时必须显式给出）"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="唯一写开关：显式给出才把凭证原子落盘（缺省只打印 stdout）",
    )
    return parser


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON。"""
    if json_output:
        print(safe_text(message), file=sys.stderr)
    else:
        safe_print(message)


def _write(target: Path | None, text: str) -> int:
    """唯一写开关：显式 ``--out`` 才原子落盘（返回 ``EXIT_OK`` 或 ``EXIT_UNUSABLE``）。"""
    if target is None:
        return EXIT_OK
    try:
        atomic_write_text(target, text + "\n")
    except OSError as exc:
        print(
            f"[evidence] 凭证写盘失败：{type(exc).__name__}: {safe_text(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE
    print(f"[evidence] 材料级人工核验凭证已写入：{target}", file=sys.stderr)
    return EXIT_OK


def _render_attestation(attestation: HumanVerificationAttestation, *, json_output: bool) -> str:
    """渲染稳定 JSON 或人类可读 Markdown。"""
    if json_output:
        return json.dumps(attestation.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_attestation_summary(attestation)


def _render_verification(verification: AttestationVerification, *, json_output: bool) -> str:
    """渲染防伪核验结论（稳定 JSON 或人类可读 Markdown）。"""
    if json_output:
        return json.dumps(verification.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        "# 材料级人工核验凭证 · 防伪核验（纯只读）",
        "",
        f"- attestation_id（声明）：`{verification.attestation_id}`",
        f"- attestation_id（重算）：`{verification.recomputed_attestation_id}`；"
        f"一致={str(verification.attestation_id_matches).lower()}",
        f"- package fingerprint 一致={str(verification.package_fingerprint_matches).lower()}；"
        f"package 内容一致={str(verification.package_content_matches).lower()}；"
        f"handoff 内容一致={str(verification.handoff_content_matches).lower()}；"
        f"preflight 一致={str(verification.preflight_pass_matches).lower()}；"
        f"scope 一致={str(verification.scope_matches).lower()}",
        f"- all_required_verified（凭证声明）={str(verification.all_required_verified).lower()}；"
        "**这是材料级人工核验结论，不是资格判定**",
        f"- 核验通过：**{str(verification.verified).lower()}**；"
        "`data_qualification_passed` / `phase_transition_allowed` 恒为 false",
    ]
    if verification.codes:
        lines += ["", "## 稳定原因码（fail-closed）", ""]
        lines.extend(f"- `{code}`" for code in verification.codes)
    if verification.details:
        lines += ["", "不一致明细", ""]
        lines.extend(f"- {item}" for item in verification.details)
    lines.append("")
    return "\n".join(lines)



def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口（只读 / 零网络 / 零数据库 / 默认零写入；返回进程退出码）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)
    json_output = bool(args.json_output)

    if args.schema_output:
        text = json.dumps(attestation_schema(), ensure_ascii=False, indent=2, sort_keys=True)
        safe_print(text)
        return _write(args.out, text)

    if args.verify_attestation is not None:
        if (
            args.out is not None
            or args.verification is not None
            or args.revision != 1
            or args.supersedes is not None
        ):
            parser.error(
                "--verify-attestation 是纯只读重新绑定核验模式：不得与 --verification / "
                "--revision / --supersedes / --out 同时给出"
            )
        if args.inbox_dir is None:
            parser.error("--verify-attestation 必须同时给出 --inbox-dir（重新绑定的候选目录）")
        try:
            verification = verify_attestation(
                args.verify_attestation, args.inbox_dir, moment=moment
            )
        except AttestationError as exc:
            print(
                "[evidence] 凭证防伪核验未完成（fail-closed，未写入任何 artifact）："
                f"{safe_text(str(exc))}",
                file=sys.stderr,
            )
            return exit_code_for(exc)
        safe_print(_render_verification(verification, json_output=json_output))
        return main_verification_exit_code(verification.verified)

    if args.inbox_dir is None or args.verification is None:
        parser.error(
            "必须显式提供 --inbox-dir（本地候选目录）与 --verification（人工核验输入 JSON），"
            "或使用 --schema 打印契约 / --verify-attestation 做防伪核验"
        )

    try:
        attestation = run_attestation(
            args.inbox_dir,
            verification_path=args.verification,
            moment=moment,
            package=args.package,
            revision=args.revision,
            supersedes=args.supersedes,
            out_path=args.out,
        )
    except AttestationError as exc:
        # fail-closed：任何失败都**不写** artifact；错误信息统一脱敏
        print(
            "[evidence] 材料级人工核验凭证未生成（fail-closed，未写入任何 artifact）："
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return exit_code_for(exc)

    safe_print(_render_attestation(attestation, json_output=json_output))
    if args.out is not None:
        _notify(
            "[evidence] 材料级人工核验凭证已原子写入："
            f"{args.out}（**不是**数据资格通过；PHASE3_3_DATA 仍 BLOCKED）",
            json_output=json_output,
        )
    return EXIT_OK if attestation.all_required_verified else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())

