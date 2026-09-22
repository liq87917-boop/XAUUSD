"""证据就绪度 / preflight 与一键 Phase 3.3 资格复核入口（**GOLD-006**）。

Operator 工作流（全部默认只读、默认零网络）：

```text
① prepare template   ：scripts.evidence_readiness template --scope author|news
② dry-run / preflight：scripts.evidence_readiness preflight [--scope S --input F] [--json]
③ inspect quarantine ：preflight 报告里的批次量化 + 原因码（或 intake 的 --quarantine 文件）
④ explicit intake    ：scripts.intake_evidence --scope S --input F --no-dry-run ...
⑤ qualification recheck：scripts.evidence_readiness recheck [--json]
```

用法::

    # ① 打印可填写模板（stdout；零写库、零联网）
    python -m scripts.evidence_readiness template --scope author

    # ① 写入模板文件（拒绝覆盖已存在文件，除非 --overwrite）
    python -m scripts.evidence_readiness template --scope news \
        --out examples/evidence/news_evidence_template.csv

    # ② 只读就绪度（库内台账）+ 候选文件 dry-run 量化（零写入）
    python -m scripts.evidence_readiness preflight --scope news \
        --input logs/evidence/news.csv --json

    # ⑤ 一键资格复核：串联只读台账与现有 Phase 3.3 qualification report
    python -m scripts.evidence_readiness recheck --json

安全与边界：

- **默认只读 / 零网络**：不抓取站点、不绕过 robots / 条款 / 证书；`template` 只做本地字符串拼接，
  `preflight` 对候选文件只做 dry-run（零写入），`recheck` 只读库内台账与资格报告；
- **不解除 blocker**：`blocker_active` 恒为 true、`human_gate_required` 恒为 true；
  报告 PASS 只说明"量化门槛达标"，不等于人工授权核验通过；
- **脱敏**：报告只含计数、阈值、缺口、稳定原因码、已脱敏来源名与时间，
  绝不回显 token / API key / Authorization / 完整 source config，也不输出正文；
- **模板不计入真实证据**：模板行带 `record_kind=example` / `is_mock=true`，
  导入时判 `SYNTHETIC_EVIDENCE` 隔离，永不进入 qualification ledger；
- 退出码：``0`` 报告生成成功（BLOCKED 也是正常结果）/ ``2`` 参数或输入错误 /
  ``3`` 输入没有数据行。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    TEMPLATE_FORMATS,
    TEMPLATE_ROOT,
    EvidenceScope,
    InputFile,
    InputRow,
    intake_evidence,
    load_evidence_ledger,
    read_input_file,
    render_template,
    template_output_name,
    write_template,
)
from src.monitoring import (  # noqa: E402
    BatchQuantification,
    EvidenceReadinessReport,
    QualificationReport,
    build_readiness_report,
    load_qualification_report,
    render_qualification_report,
    render_readiness_report,
    summarize_batch,
)

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_NO_ROWS = 3

def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_readiness",
        description=(
            "证据就绪度 / preflight 与一键资格复核：默认只读、默认零网络、默认零写入；"
            "任何结果都不会解除 PHASE3_3_DATA。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scopes = [scope.value for scope in EvidenceScope]

    template = sub.add_parser(
        "template",
        help="prepare template：输出可填写的 Author / News 证据模板（默认打印到 stdout）",
    )
    template.add_argument("--scope", choices=scopes, required=True, help="证据类别")
    template.add_argument(
        "--format",
        default="csv",
        choices=list(TEMPLATE_FORMATS),
        help="模板格式（默认 csv）",
    )
    template.add_argument(
        "--out",
        type=Path,
        default=None,
        help="写入路径（缺省只打印到 stdout；已存在时拒绝覆盖，除非 --overwrite）",
    )
    template.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的模板文件（默认拒绝，防止覆盖已填写内容）",
    )

    preflight = sub.add_parser(
        "preflight",
        help="dry-run / preflight：只读台账 + 候选文件 dry-run 量化（零写入、零网络）",
    )
    preflight.add_argument("--scope", choices=scopes, default=None, help="只输出该 scope")
    preflight.add_argument(
        "--input", type=Path, default=None, help="候选证据文件（JSONL / CSV；仅本地，零网络）"
    )
    preflight.add_argument(
        "--format",
        default="auto",
        choices=["auto", "jsonl", "csv"],
        help="候选文件格式（默认 auto：按后缀识别）",
    )
    preflight.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
    )
    preflight.add_argument("--json", action="store_true", dest="json_output", help="稳定 JSON")
    preflight.add_argument(
        "--report", type=Path, default=None, help="落盘路径（仅在同时给出 --no-dry-run 时写入）"
    )
    preflight.add_argument(
        "--no-dry-run", action="store_true", help="允许把 --report 写到磁盘（默认只打印）"
    )

    recheck = sub.add_parser(
        "recheck",
        help="qualification recheck：串联只读台账与现有 Phase 3.3 qualification report",
    )
    recheck.add_argument(
        "--as-of", default=None, help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC）"
    )
    recheck.add_argument("--json", action="store_true", dest="json_output", help="稳定 JSON")
    recheck.add_argument(
        "--report", type=Path, default=None, help="落盘路径（仅在同时给出 --no-dry-run 时写入）"
    )
    recheck.add_argument(
        "--no-dry-run", action="store_true", help="允许把 --report 写到磁盘（默认只打印）"
    )
    return parser


def parse_as_of(raw: str | None) -> datetime | None:
    """解析 ``--as-of``（必须带时区，否则拒绝：禁止隐式时区）。"""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--as-of 无法解析为 ISO8601：{raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of 必须包含时区（例如 2026-09-22T12:00:00+00:00）")
    return parsed.astimezone(UTC)

def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON。"""
    if json_output:
        print(safe_text(message), file=sys.stderr)
    else:
        safe_print(message)


def _template_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """prepare template：打印或写入模板（零写库、零网络）。"""
    scope = EvidenceScope(str(args.scope))
    fmt = str(args.format)
    target: Path | None = args.out
    if target is None:
        default_target = TEMPLATE_ROOT / template_output_name(scope, fmt)
        safe_print(render_template(scope, fmt))
        print(
            f"[evidence] 模板未落盘（仅 stdout）；写入默认位置请加 "
            f"--out {default_target.as_posix()}",
            file=sys.stderr,
        )
        return EXIT_OK
    try:
        written = write_template(scope, fmt, target, overwrite=bool(args.overwrite))
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    safe_print(
        f"[evidence] 模板已写入：{written}（示例行带 record_kind=example / is_mock=true，"
        "导入时判 SYNTHETIC_EVIDENCE 隔离，不进入 qualification ledger）"
    )
    return EXIT_OK


def _recheck_payload(
    readiness: EvidenceReadinessReport, qualification: QualificationReport
) -> dict[str, Any]:
    """一键 qualification recheck 的稳定 JSON 载荷（只读）。"""
    return {
        "schema_version": readiness.schema_version,
        "report": "phase33_qualification_recheck",
        "contract_version": readiness.contract_version,
        "as_of": readiness.as_of.isoformat(),
        "blocker_code": readiness.blocker_code,
        "blocker_active": readiness.blocker_active,
        "human_gate_required": readiness.human_gate_required,
        "ready": readiness.ready and qualification.ready,
        "gate": {
            "qualification_ready": qualification.ready,
            "qualification_pass_count": qualification.pass_count,
            "qualification_blocked_count": qualification.blocked_count,
            "readiness_ready": readiness.ready,
            "readiness_blocked_scope_count": sum(
                1 for item in readiness.scopes if not item.ready
            ),
        },
        "readiness": readiness.to_dict(),
        "qualification": qualification.to_dict(),
        "notes": [
            "recheck 只读：不写库、不联网、不修改任何历史事实；落盘需显式 --no-dry-run --report。",
            "blocker_active 由人工 Gate 与真实证据决定；本工具不具备解除 blocker 的能力。",
            *readiness.notes,
        ],
    }


def _render(
    command: str,
    readiness: EvidenceReadinessReport,
    qualification: QualificationReport | None,
    *,
    json_output: bool,
) -> str:
    """渲染稳定 JSON 或人类可读 Markdown（recheck 会串联现有 qualification 报告）。"""
    if command == "recheck":
        assert qualification is not None  # main 已在 recheck 分支构造
        if json_output:
            return json.dumps(
                _recheck_payload(readiness, qualification),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        return (
            render_readiness_report(readiness)
            + "\n"
            + render_qualification_report(qualification)
        )
    if json_output:
        return json.dumps(readiness.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_readiness_report(readiness)


def _write_report(args: argparse.Namespace, text: str, *, json_output: bool) -> None:
    """落盘报告（**仅在显式 --no-dry-run**；默认零写入）。"""
    target: Path | None = getattr(args, "report", None)
    if target is None:
        return
    if not bool(getattr(args, "no_dry_run", False)):
        _notify(f"（dry-run：未写入 {target}；如需写入请加 --no-dry-run）", json_output=json_output)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    _notify(f"报告已写入：{target}", json_output=json_output)

def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """CLI 主入口（默认只读 / 零网络 / 零写入；返回进程退出码）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = str(args.command)
    if command == "template":
        return _template_command(args, parser)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)

    input_path: Path | None = getattr(args, "input", None)
    scope_name: str | None = getattr(args, "scope", None)
    if input_path is not None and scope_name is None:
        parser.error("--input 必须与 --scope 一起使用（否则无法判定证据类别）")

    input_file: InputFile | None = None
    rows: list[InputRow] = []
    if input_path is not None:
        try:
            input_file, rows = read_input_file(
                input_path, requested_format=str(getattr(args, "format", "auto"))
            )
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
    batches: tuple[BatchQuantification, ...] = ()
    qualification: QualificationReport | None = None
    with factory() as session:
        try:
            if input_file is not None and scope_name is not None:
                batch_report = intake_evidence(
                    session,
                    scope=EvidenceScope(scope_name),
                    input_file=input_file,
                    rows=rows,
                    moment=moment,
                    dry_run=True,
                )
                batches = (summarize_batch(batch_report),)
            ledger = load_evidence_ledger(session)
            if command == "recheck":
                qualification = load_qualification_report(session, as_of=moment)
        finally:
            # 只读 / dry-run：显式回滚，杜绝任何隐式写入
            session.rollback()

    scopes = (
        (EvidenceScope(scope_name),)
        if scope_name is not None
        else (EvidenceScope.AUTHOR, EvidenceScope.NEWS)
    )
    readiness = build_readiness_report(ledger, as_of=moment, batches=batches, scopes=scopes)
    json_output = bool(getattr(args, "json_output", False))
    text = _render(command, readiness, qualification, json_output=json_output)
    safe_print(text)
    _write_report(args, text, json_output=json_output)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
