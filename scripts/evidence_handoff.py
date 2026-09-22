"""Evidence 人工交接包 CLI（**GOLD-008**，只读 / 默认 dry-run）。

把当前真实数据库的资格状态整理成可交给业务方 / operator 的**确定性人工交接包**：
机器可读 JSON + 人类可读 Markdown，量化 Author / News 缺口、coverage / source-share 状态、
主要隔离原因码计数，并给出明确的人工证据 checklist（模板 / Mock / 示例醒目标记为不计资格）。

用法::

    # 默认：只读台账 + 只打印 stdout（零写入、零网络）
    python -m scripts.evidence_handoff --json
    python -m scripts.evidence_handoff --as-of 2026-09-22T12:00:00+00:00

    # 追加候选文件 dry-run 隔离原因计数（仍零写入）
    python -m scripts.evidence_handoff --scope news --input logs/evidence/news.csv --json

    # 显式落盘（唯一写文件路径；必须给出 --out）
    python -m scripts.evidence_handoff --json --out logs/evidence/handoff.json

安全与边界：

- **只读 / 零网络 / 零写入**：不抓取站点、不绕过 robots / 条款 / 证书；`--input` 只做
  dry-run（显式回滚，绝不落库）；只有显式给出 ``--out`` 才会写文件，默认只打印 stdout；
- **不自动 intake**：本命令不提供 ``--no-dry-run``，不 append 任何记录，不建立来源，
  不启用采集，也不进入 Phase 3.4；
- **复用唯一口径**：缺口与阈值完全复用 ``scripts.evidence_readiness`` /
  ``src.monitoring.evidence_readiness``（阈值来自 ``src.alpha.evidence_gate``），
  本命令不复制第二套阈值算法；
- **不解除 blocker**：``blocker_active`` / ``human_gate_required`` 恒为 true，
  ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 false；无真实合格
  授权证据时保持 BLOCKED；
- **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 已脱敏来源名 / 时间，
  绝不输出正文、token / API key / Authorization 或完整 source config；
- 退出码：``0`` 量化门槛达标（**仍需人工 Gate**，不代表数据资格通过）/
  ``2`` 参数或输入错误 / ``3`` 输入没有数据行 / ``5`` 仍未达标（诚实保持 BLOCKED）。
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
from src.evidence import (  # noqa: E402
    EvidenceScope,
    InputFile,
    InputRow,
    build_handoff_report,
    intake_evidence,
    load_evidence_ledger,
    read_input_file,
    render_handoff_markdown,
)
from src.monitoring import build_readiness_report, summarize_batch  # noqa: E402

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_NO_ROWS = 3
#: 仍未达标：JSON 与退出状态都诚实保持 BLOCKED（不得把工具完成当作数据资格通过）
EXIT_BLOCKED = 5


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项；默认只读、零网络、零写入）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_handoff",
        description=(
            "Evidence 人工交接包：只读生成授权证据缺口报告（默认 dry-run、默认零网络、"
            "默认零写入）；任何结果都不会解除 PHASE3_3_DATA。"
        ),
    )
    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in EvidenceScope],
        default=None,
        help="候选文件类别（仅与 --input 一起使用；只做 dry-run，不落库）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="可选候选输入文件（.jsonl / .ndjson / .json 或 .csv；本地文件，不联网）",
    )
    parser.add_argument(
        "--format",
        default="auto",
        choices=["auto", "jsonl", "csv"],
        help="候选输入格式（默认 auto：按后缀识别）",
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
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="报告落盘路径（**必须显式指定**；缺省只打印 stdout，零写入）",
    )
    return parser


def _reject_same_file(input_path: Path, target: Path) -> None:
    """拒绝把报告写到候选输入文件上（含硬链接别名）。"""
    resolved_input = input_path.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_input or (
        resolved_target.exists() and input_path.exists() and resolved_target.samefile(input_path)
    ):
        raise ValueError("--out 不能与 --input 指向同一文件")


def _write_output(target: Path | None, text: str) -> None:
    """落盘报告（**仅在显式给出 --out 时**；默认零写入）。"""
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"[evidence] handoff 报告已写入：{target}", file=sys.stderr)


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

    scope_name: str | None = args.scope
    input_path: Path | None = args.input
    out_path: Path | None = args.out
    if input_path is None and scope_name is not None:
        parser.error("--scope 只在提供候选文件（--input）时有意义，请与 --input 一起使用")
    if input_path is not None and scope_name is None:
        parser.error("--input 必须与 --scope 一起使用（否则无法判定证据类别）")
    if out_path is not None and input_path is not None:
        try:
            _reject_same_file(input_path, out_path)
        except ValueError as exc:
            parser.error(str(exc))

    input_file: InputFile | None = None
    rows: list[InputRow] = []
    if input_path is not None:
        try:
            input_file, rows = read_input_file(input_path, requested_format=str(args.format))
        except (OSError, ValueError) as exc:
            print(
                f"[evidence] 输入不可用：{type(exc).__name__}: {safe_text(str(exc))}",
                file=sys.stderr,
            )
            return EXIT_INPUT_ERROR
        if not rows:
            print("[evidence] 输入没有数据行", file=sys.stderr)
            return EXIT_NO_ROWS

    factory = session_factory or build_session_factory(build_engine())
    batch_report = None
    with factory() as session:
        try:
            if input_file is not None and scope_name is not None:
                # 只做 dry-run（零写入）；不自动 intake、不改库
                batch_report = intake_evidence(
                    session,
                    scope=EvidenceScope(scope_name),
                    input_file=input_file,
                    rows=rows,
                    moment=moment,
                    dry_run=True,
                )
            ledger = load_evidence_ledger(session)
        finally:
            # 只读 / dry-run：显式回滚，杜绝任何隐式写入
            session.rollback()

    batch = summarize_batch(batch_report) if batch_report is not None else None
    readiness = build_readiness_report(
        ledger,
        as_of=moment,
        batches=(batch,) if batch is not None else (),
        scopes=(EvidenceScope.AUTHOR, EvidenceScope.NEWS),
    )
    report = build_handoff_report(readiness, batch=batch)
    json_output = bool(args.json_output)
    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_handoff_markdown(report)
    )
    safe_print(text)
    _write_output(out_path, text)
    return EXIT_OK if report.ready_for_human_review else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
