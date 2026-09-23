"""PHASE3_3_DATA 证据缺口只读诊断 CLI（**GOLD-026**，默认只读 / 零网络 / 默认零写入）。

把当前真实库内证据状态与（可选）**本地**候选 manifest 整理成**单一、确定、可操作**的
缺口诊断，供 GPT 与人工判断"还缺什么、必须由谁完成"：

```text
# ① 只读诊断（默认只打印 stdout；零写入、零网络）
python -m scripts.evidence_gap_diagnostic --json

# ② 固定审计时点（ISO8601 必须带时区；用于人工复现，不改变任何口径）
python -m scripts.evidence_gap_diagnostic --as-of 2026-09-22T12:00:00+00:00

# ③ 纳入本地候选 manifest 事实（只读预检，不 ingest）
python -m scripts.evidence_gap_diagnostic --inbox-dir logs/evidence/inbox --json

# ④ 唯一写开关：显式 --out 原子落盘诊断本身（仍不写数据库、不 intake）
python -m scripts.evidence_gap_diagnostic --json --out logs/evidence/gap.json
```

安全与边界：

- **只读 / 零网络 / 零写入**：不抓取站点、不绕过 robots / 条款 / 证书；`--inbox-dir` 只做既有
  GOLD-011 的**只读**预检（`src.evidence.inbox.scan_inbox`）；不 append 任何记录、不建 source、
  不启用采集、不写 `PROJECT_STATE`；默认只打印 stdout，唯一写开关是显式 ``--out``；
- **复用唯一口径**：缺口 / 阈值 / 清单完全复用 ``src.monitoring.evidence_readiness`` +
  ``src.evidence.handoff`` + ``src.evidence.inbox``（阈值来源 ``src.alpha.evidence_gate``），
  本命令**不新增、不降低**任何资格阈值；
- **不解除 blocker**：``blocker_active`` / ``human_gate_required`` 恒为 true，
  ``data_qualification_passed`` / ``phase_transition_allowed`` / ``advance_allowed`` /
  ``l3_l4_auto_advance_allowed`` 恒为 false；L3 / L4 只能人工推进；
- **不伪造缺失字段**：``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at`` /
  OOS 证据缺失时只报告事实与人工下一步（绝不用当前时间 / 文件 mtime / 抓取时间填补）；
- **Mock 永不 qualify**：模板 / Mock / 示例一律显式标记 ``counts_toward_eligibility=false``；
- **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏文本，绝不输出正文或凭据；
- 退出码：``0`` 诊断生成且**无实质证据缺口**（gate 仍 BLOCKED）/ ``2`` 参数或输入错误 /
  ``4`` ``--out`` 不可写 / ``5`` 仍有实质证据缺口（**预期**：``PHASE3_3_DATA`` 保持 BLOCKED）。
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
from src.evidence.inbox import InboxError  # noqa: E402
from src.evidence.readiness_watch import atomic_write_text  # noqa: E402
from src.monitoring.evidence_gap_diagnostic import (  # noqa: E402
    EvidenceGapDiagnostic,
    load_gap_diagnostic,
    render_gap_diagnostic_markdown,
)

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_OUTPUT_UNUSABLE = 4
#: 仍有实质证据缺口：JSON 与退出状态都诚实保持 BLOCKED（工具完成 ≠ 资格通过）
EXIT_EVIDENCE_GAPS = 5


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项；无任何 intake / 写库 / 放行参数）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_gap_diagnostic",
        description=(
            "PHASE3_3_DATA 证据缺口只读诊断（默认只打印 stdout、零网络、零写入）；"
            "任何结果都不会解除 PHASE3_3_DATA，也不会推进 L3 / L4。"
        ),
    )
    parser.add_argument(
        "--inbox-dir",
        type=Path,
        default=None,
        help="可选的本地候选目录（只读预检 manifest 事实；不联网、不 ingest）",
    )
    parser.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="稳定 JSON")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="唯一写开关：显式给出才把诊断原子落盘（缺省只打印 stdout）",
    )
    return parser


def _render(diagnostic: EvidenceGapDiagnostic, *, json_output: bool) -> str:
    """渲染稳定 JSON 或人类可读 Markdown。"""
    if json_output:
        return json.dumps(diagnostic.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_gap_diagnostic_markdown(diagnostic)


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """CLI 主入口（只读 / 零网络 / 默认零写入；返回进程退出码）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)

    factory = session_factory or build_session_factory(build_engine())
    try:
        with factory() as session:
            try:
                diagnostic = load_gap_diagnostic(
                    session, as_of=moment, inbox_dir=args.inbox_dir
                )
            finally:
                # 只读：显式回滚，杜绝任何隐式写入
                session.rollback()
    except (OSError, ValueError, InboxError) as exc:
        print(
            f"[evidence] 诊断不可用：{type(exc).__name__}: {safe_text(str(exc))}",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR

    json_output = bool(args.json_output)
    text = _render(diagnostic, json_output=json_output)
    safe_print(text)
    out_path: Path | None = args.out
    if out_path is not None:
        try:
            atomic_write_text(out_path, text + "\n")
        except OSError as exc:
            print(
                f"[evidence] 诊断写盘失败：{type(exc).__name__}: {safe_text(str(exc))}",
                file=sys.stderr,
            )
            return EXIT_OUTPUT_UNUSABLE
        print(f"[evidence] 诊断已写入：{out_path}", file=sys.stderr)
    return EXIT_EVIDENCE_GAPS if diagnostic.open_evidence_gap_count > 0 else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
