"""Phase 3.3 人工证据 Intake Handoff 预检 CLI（**GOLD-027**，纯本地只读 / 默认零写入）。

把"人工如何按**单一契约**提交真实授权 Author / News 材料"变成可执行的 handoff：
``--schema`` 打印版本化提交契约；``--inbox-dir`` 只对**显式本地目录**做只读预检，
逐材料给出 ``MISSING`` / ``PRESENT_UNVERIFIED`` / ``HUMAN_VERIFICATION_REQUIRED`` /
``NON_QUALIFYING`` / ``GATE_BLOCKED``，并明确"**结构完整 ≠ 资格通过**"。

用法::

    # ① 打印版本化 handoff 契约（人工 / 业务方据此准备材料；零网络、零写入）
    python -m scripts.evidence_intake_handoff --schema

    # ② 只读预检显式本地候选目录（默认只打印 stdout；零写入、零数据库）
    python -m scripts.evidence_intake_handoff --inbox-dir logs/evidence/inbox --json

    # ③ 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
    python -m scripts.evidence_intake_handoff --inbox-dir logs/evidence/inbox ^
        --as-of 2026-09-23T00:00:00+00:00

    # ④ 唯一写开关：显式 --out 原子落盘预检本身（仍不写数据库、不 intake）
    python -m scripts.evidence_intake_handoff --inbox-dir logs/evidence/inbox --json ^
        --out logs/evidence/intake_handoff.json

安全与边界：

- **只读 / 零网络 / 零数据库**：只扫描显式 ``--inbox-dir``（复用 GOLD-011 ``scan_inbox``），
  不抓取站点、不绕过 robots / 条款 / 证书、不移动 / 删除 / 改写任何原始证据、不写库、
  不调用任何 intake / commit 路径；默认只打印 stdout，唯一写开关是显式 ``--out``（原子写）；
- **复用而不复制**：材料契约 / 字段名 / 原因码完全复用 ``evidence-intake-v1`` 与 GOLD-011
  / GOLD-026 的只读能力，**不新增、不降低**任何资格阈值；
- **不解除 blocker**：``evidence_qualified`` / ``data_qualification_passed`` /
  ``phase_transition_allowed`` / ``advance_allowed`` / ``l3_l4_auto_advance_allowed``
  恒为 false；``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` 恒为 true；
- **不伪造时间事实**：``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at``
  缺失时只报告事实与人工下一步（绝不用当前时间 / 文件 mtime / 抓取时间填补）；
- **Mock 永不 qualify**：模板 / Mock / 示例一律 ``NON_QUALIFYING``；
- 退出码：``0`` 至少一个候选包**结构完整**（仍**不是**资格通过）/
  ``2`` 参数或输入错误 / ``4`` ``--out`` 不可写 /
  ``5`` 没有任何结构完整的候选包（**预期**：``PHASE3_3_DATA`` 保持 BLOCKED）。
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
from src.evidence.inbox import InboxError  # noqa: E402
from src.evidence.intake_handoff import (  # noqa: E402
    IntakeHandoffDocument,
    intake_handoff_schema,
    load_intake_handoff,
    render_intake_handoff_markdown,
)
from src.evidence.readiness_watch import atomic_write_text  # noqa: E402

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_OUTPUT_UNUSABLE = 4
#: 没有任何**结构完整**的候选包：JSON 与退出状态都诚实保持 BLOCKED
EXIT_INCOMPLETE = 5


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（无任何 intake / 写库 / qualify / approve / advance 参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_intake_handoff",
        description=(
            "Phase 3.3 人工证据 Intake Handoff 预检：只做纯本地只读预检（默认只打印 stdout、"
            "零网络、零数据库）；`preflight_pass` 只表示 package 结构完整，"
            "不代表 evidence qualified，也不会解除 PHASE3_3_DATA，更不会推进 L3 / L4。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        default=None,
        help="**必填**（除 --schema 外）：本地候选目录（候选包 = 其直接子目录或单文件）",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        dest="schema_output",
        help="打印版本化 handoff 提交契约（JSON；不需要 --inbox-dir）",
    )
    parser.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
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
        help="唯一写开关：显式给出才把预检结果原子落盘（缺省只打印 stdout）",
    )
    return parser


def _write(target: Path | None, text: str) -> int:
    """唯一写开关：显式 ``--out`` 才原子落盘（返回 ``EXIT_OK`` 或 ``EXIT_OUTPUT_UNUSABLE``）。"""
    if target is None:
        return EXIT_OK
    try:
        atomic_write_text(target, text + "\n")
    except OSError as exc:
        print(
            f"[evidence] handoff 预检写盘失败：{type(exc).__name__}: {safe_text(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_OUTPUT_UNUSABLE
    print(f"[evidence] handoff 预检已写入：{target}", file=sys.stderr)
    return EXIT_OK


def _render(document: IntakeHandoffDocument, *, json_output: bool) -> str:
    """渲染稳定 JSON 或人类可读 Markdown。"""
    if json_output:
        return json.dumps(document.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_intake_handoff_markdown(document)


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
    out_path: Path | None = args.out

    if args.schema_output:
        text = json.dumps(intake_handoff_schema(), ensure_ascii=False, indent=2, sort_keys=True)
        safe_print(text)
        return _write(out_path, text)

    if args.inbox_dir is None:
        parser.error(
            "必须显式提供 --inbox-dir（本地候选目录），或使用 --schema 打印 handoff 契约"
        )

    try:
        document = load_intake_handoff(args.inbox_dir, as_of=moment)
    except (OSError, ValueError, InboxError) as exc:
        print(
            f"[evidence] handoff 预检不可用（fail-closed）：{type(exc).__name__}: "
            f"{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR

    text = _render(document, json_output=json_output)
    safe_print(text)
    write_code = _write(out_path, text)
    if write_code != EXIT_OK:
        return write_code
    # 结构完整 ≠ 资格通过：即使 preflight_pass 也仍需人工核验与 L3 人工 Gate
    return EXIT_OK if document.preflight_pass_count > 0 else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())

