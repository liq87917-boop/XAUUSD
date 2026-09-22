"""单入口授权 Evidence Operator 工作流（**GOLD-007**）。

把 GOLD-005/006 的能力串成一个默认安全、可重复的 operator workflow：

```text
template → preflight → quarantine → intake → recheck
（+ 可选 gateway-only author-chain）
```

用法::

    # 单入口（推荐）：默认 dry-run，串联 preflight / 隔离摘要 / recheck（零写入、零网络）
    python -m scripts.evidence_operator workflow --scope news --input logs/evidence/news.csv --json

    # 唯一显式写入路径：先跑完整 dry-run 写入门禁，再 append-only 落库
    python -m scripts.evidence_operator workflow --scope news --input logs/evidence/news.csv \
        --no-dry-run --manifest logs/evidence/news_manifest.json

    # 单步命令（与 workflow 同一套校验逻辑）
    python -m scripts.evidence_operator template --scope author
    python -m scripts.evidence_operator preflight --scope author --input author.jsonl
    python -m scripts.evidence_operator quarantine --scope author --input author.jsonl
    python -m scripts.evidence_operator intake --scope author --input author.jsonl
    python -m scripts.evidence_operator recheck --json
    python -m scripts.evidence_operator author-chain --json    # gateway-only 作者归属（dry-run）

安全与边界：

- **默认 dry-run / 零网络 / 零写入**：不抓取站点、不绕过 robots / 条款 / 证书；
  `template` 只做本地字符串拼接；`preflight` / `quarantine` 只做 dry-run；
  `recheck` 直接复用 ``scripts.evidence_readiness``（只读台账 + 现有资格报告）；
- **写入前二次验证**：`intake` / `workflow --no-dry-run` 会先跑一次完整 dry-run
  Evidence Gateway 校验（:func:`src.evidence.workflow.evaluate_write_gate`），
  再 append-only 写入；未授权 / 合成示例 / 时间非法 / 身份冲突 / 凭据 / 坏行一律隔离，
  绝不进入 qualification ledger，也绝不覆盖历史事实；
- **gateway-only 作者链**：`author-chain` 只消费 `raw_json.evidence` 里带
  `evidence-intake-v1` + `scope=author` + `oos_eligible=true` 的记录；
  普通 CSV（`import_real_posts.py`）/ 历史样本 / Mock 没有证据块，无法绕过 gateway；
- **不解除 blocker**：`blocker_active` / `human_gate_required` 恒为 true；
  报告 PASS 只说明量化门槛，不等于人工授权核验；无真实合格证据时 `PHASE3_3_DATA` 保持 BLOCKED；
- **脱敏**：只输出计数 / 稳定原因码 / 已脱敏来源名 / 指纹前缀 / 时间，
  绝不输出正文、token / API key / Authorization 或完整 source config；
- 退出码：``0`` 报告成功（BLOCKED 也是正常结果）/ ``2`` 参数或输入错误 /
  ``3`` 输入没有数据行 / ``4`` `intake` 存在被隔离的行（数据已隔离，未计入可信证据）。
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
from scripts.evidence_readiness import main as readiness_main  # noqa: E402
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from scripts.intake_evidence import validate_output_targets  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    OPERATOR_STEPS,
    OPERATOR_WORKFLOW_SCHEMA_VERSION,
    TEMPLATE_FORMATS,
    TEMPLATE_ROOT,
    AuthorChainReport,
    EvidenceIntakeReport,
    EvidenceScope,
    InputFile,
    InputRow,
    QuarantineSummary,
    WriteGateDecision,
    attribute_gateway_author_evidence,
    build_quarantine_summary,
    evaluate_write_gate,
    intake_evidence,
    load_evidence_ledger,
    quarantine_payload,
    read_input_file,
    render_quarantine_summary,
    render_template,
    render_write_gate,
    template_output_name,
    write_template,
)
from src.monitoring import (  # noqa: E402
    PHASE3_3_BLOCKER_CODE,
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
EXIT_QUARANTINED = 4

#: 报告标识（稳定，供上游解析）
WORKFLOW_REPORT_NAME = "evidence_operator_workflow"


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """添加通用参数（全部为非敏感项；默认只读、默认零网络）。"""
    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in EvidenceScope],
        default=None,
        help="证据类别：author（作者帖子类）或 news（新闻/快讯类）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="候选输入文件（.jsonl / .ndjson / .json 或 .csv；本地文件，不联网）",
    )
    parser.add_argument(
        "--format",
        default="auto",
        choices=["auto", "jsonl", "csv"],
        help="输入格式（默认 auto：按后缀识别）",
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


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_operator",
        description=(
            "单入口授权 Evidence Operator 工作流：template → preflight → quarantine → "
            "intake → recheck（默认 dry-run、默认零网络、默认零写入）；"
            "任何结果都不会解除 PHASE3_3_DATA。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    workflow = sub.add_parser(
        "workflow",
        help="单入口：一次完成 preflight / 隔离摘要 /（可选）显式 intake / recheck",
    )
    _add_common_args(workflow)
    workflow.add_argument(
        "--no-dry-run",
        action="store_true",
        help="显式提交：append-only 写入证据与（--author-chain 时的）作者归属",
    )
    workflow.add_argument(
        "--author-chain",
        action="store_true",
        help="同时运行 gateway-only 作者归属链（默认 dry-run）",
    )
    workflow.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSON manifest 落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )
    workflow.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help="隔离清单 JSONL 落盘路径（仅在同时给出 --no-dry-run 且存在隔离行时写入）",
    )
    workflow.add_argument(
        "--report",
        type=Path,
        default=None,
        help="报告落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )

    template = sub.add_parser(
        "template",
        help="prepare template：输出可填写的 Author / News 证据模板（默认打印到 stdout）",
    )
    template.add_argument(
        "--scope",
        choices=[scope.value for scope in EvidenceScope],
        required=True,
        help="模板类别：author 或 news",
    )
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
        help="落盘路径（缺省 = 只打印到 stdout，不写文件）",
    )
    template.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的模板文件（默认拒绝，防止覆盖 operator 已填写内容）",
    )

    preflight = sub.add_parser(
        "preflight",
        help="dry-run / preflight：只读台账 + 候选文件量化 + 写入门禁（零写入）",
    )
    _add_common_args(preflight)

    quarantine = sub.add_parser(
        "quarantine",
        help="quarantine summary：对候选文件 dry-run 并输出可人工复核的隔离摘要（零写入）",
    )
    _add_common_args(quarantine)

    intake = sub.add_parser(
        "intake",
        help="explicit intake：默认 dry-run；--no-dry-run 才 append-only 落库",
    )
    _add_common_args(intake)
    intake.add_argument(
        "--no-dry-run",
        action="store_true",
        help="显式提交：append-only 写入 raw_items + processed_items",
    )
    intake.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSON manifest 落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )
    intake.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help="隔离清单 JSONL 落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )

    recheck = sub.add_parser(
        "recheck",
        help="qualification recheck：复用 evidence_readiness（只读台账 + 现有资格报告）",
    )
    _add_common_args(recheck)
    recheck.add_argument(
        "--no-dry-run",
        action="store_true",
        help="允许把 --report 写到磁盘（默认只打印）",
    )
    recheck.add_argument(
        "--report",
        type=Path,
        default=None,
        help="报告落盘路径（仅在同时给出 --no-dry-run 时写入）",
    )

    author_chain = sub.add_parser(
        "author-chain",
        help="gateway-only 作者归属链：只消费已通过 Evidence Gateway 的 Author 证据",
    )
    author_chain.add_argument(
        "--as-of",
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间）",
    )
    author_chain.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    author_chain.add_argument(
        "--no-dry-run",
        action="store_true",
        help="显式提交：append-only 写入 authors / author_accounts / author_posts",
    )
    return parser


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON（机器可读稳定）。"""
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
        safe_print(render_template(scope, fmt))
        default_target = TEMPLATE_ROOT / template_output_name(scope, fmt)
        print(
            "[evidence] 模板未落盘（仅 stdout）；写入默认位置请加 "
            f"--out {default_target.as_posix()}",
            file=sys.stderr,
        )
        return EXIT_OK
    try:
        written = write_template(scope, fmt, target, overwrite=bool(args.overwrite))
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    safe_print(
        f"[evidence] 模板已写入：{written}（示例行带 record_kind=example / is_mock=true；"
        "导入时判 SYNTHETIC_EVIDENCE 隔离，不进入 qualification ledger）"
    )
    return EXIT_OK


def _template_step() -> dict[str, Any]:
    """template 步骤的只读事实（模板文件是否存在；不写任何文件）。"""
    files: list[dict[str, Any]] = []
    for scope in EvidenceScope:
        path = TEMPLATE_ROOT / template_output_name(scope, "csv")
        files.append({"scope": scope.value, "path": path.as_posix(), "exists": path.exists()})
    return {
        "step": OPERATOR_STEPS[0],
        "template_root": TEMPLATE_ROOT.as_posix(),
        "files": files,
        "command": "python -m scripts.evidence_operator template --scope author|news",
        "note": (
            "模板示例行显式标记 record_kind=example / is_mock=true，导入时判 "
            "SYNTHETIC_EVIDENCE 隔离，永不计入 qualification ledger。"
        ),
    }


def _workflow_payload(
    *,
    as_of: datetime,
    dry_run: bool,
    template: dict[str, Any],
    readiness: EvidenceReadinessReport,
    batch: BatchQuantification | None,
    gate: WriteGateDecision | None,
    quarantine: QuarantineSummary | None,
    intake_report: EvidenceIntakeReport | None,
    author_report: AuthorChainReport | None,
    qualification: QualificationReport | None,
) -> dict[str, Any]:
    """组装单入口 workflow 的稳定 JSON 载荷（只含白名单标量；不解除 blocker）。"""
    ready = readiness.ready and (qualification.ready if qualification is not None else True)
    return {
        "schema_version": OPERATOR_WORKFLOW_SCHEMA_VERSION,
        "report": WORKFLOW_REPORT_NAME,
        "contract_version": readiness.contract_version,
        "as_of": as_of.isoformat(),
        "dry_run": dry_run,
        "steps": list(OPERATOR_STEPS),
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "ready": ready,
        "template": template,
        "preflight": {
            "readiness": readiness.to_dict(),
            "batch": batch.to_dict() if batch is not None else None,
        },
        "write_gate": gate.to_dict() if gate is not None else None,
        "quarantine": quarantine.to_dict() if quarantine is not None else None,
        "intake": intake_report.to_dict() if intake_report is not None else None,
        "author_chain": author_report.to_dict() if author_report is not None else None,
        "qualification": qualification.to_dict() if qualification is not None else None,
        "notes": [
            "单入口默认 dry-run / 零网络 / 零写入；只有 --no-dry-run 才 append-only 落库。",
            "写入门禁来自一次完整 dry-run Evidence Gateway 校验；隔离行不落库、不计入 ledger。",
            "作者归属链为 gateway-only：普通 CSV / 历史样本没有证据块，无法绕过 gateway。",
            f"blocker_active / human_gate_required 恒为 true；本工具不解除 "
            f"{PHASE3_3_BLOCKER_CODE}。",
        ],
    }


def _render_workflow_text(
    *,
    template: dict[str, Any],
    readiness: EvidenceReadinessReport,
    batch: BatchQuantification | None,
    gate: WriteGateDecision | None,
    quarantine: QuarantineSummary | None,
    author_report: AuthorChainReport | None,
    qualification: QualificationReport | None,
) -> str:
    """渲染单入口 workflow 的人类可读 Markdown（脱敏；不解除 blocker）。"""
    lines = [
        "# 授权 Evidence Operator 工作流（单入口；默认 dry-run / 零网络 / 零写入）",
        "",
        f"> blocker `{PHASE3_3_BLOCKER_CODE}`：active=true；human_gate_required=true；"
        "就绪度 PASS 不等于 Alpha 放行，本工具不解除 blocker。",
        "",
        f"## 1. {OPERATOR_STEPS[0]}（prepare template）",
        "",
    ]
    for item in template["files"]:
        lines.append(
            f"- `{item['scope']}`：`{item['path']}`（存在：{str(item['exists']).lower()}）"
        )
    lines += ["", "## 2. preflight（只读台账 + 候选文件量化）", ""]
    if batch is not None:
        lines.append(
            f"- 候选批次：{batch.scope} / {batch.input_path}：行数 {batch.rows}、"
            f"accepted {batch.accepted}、quarantined {batch.quarantined}、"
            f"duplicate {batch.duplicate}、conflict {batch.conflict}、"
            f"not_oos_eligible {batch.not_oos_eligible}"
        )
    lines += ["", render_readiness_report(readiness)]
    if gate is not None:
        lines += ["", "## 3. intake（写入门禁）", "", render_write_gate(gate)]
    if quarantine is not None:
        lines += ["", render_quarantine_summary(quarantine)]
    if author_report is not None:
        lines += ["", "## 4. author-chain（gateway-only）", "", author_report.render()]
    if qualification is not None:
        lines += [
            "",
            "## 5. recheck（现有 Phase 3.3 资格报告）",
            "",
            render_qualification_report(qualification),
        ]
    lines.append("")
    return "\n".join(lines)


def _write_artifacts(
    args: argparse.Namespace,
    *,
    text: str,
    dry_run: bool,
    report: EvidenceIntakeReport | None,
    json_output: bool,
) -> None:
    """落盘报告 / manifest / quarantine（**仅在显式 --no-dry-run**；默认零写入）。"""
    report_target: Path | None = getattr(args, "report", None)
    manifest_target: Path | None = getattr(args, "manifest", None)
    quarantine_target: Path | None = getattr(args, "quarantine", None)

    if report_target is not None:
        if dry_run:
            _notify(
                f"（dry-run：未写入 {report_target}；如需写入请加 --no-dry-run）",
                json_output=json_output,
            )
        else:
            report_target.parent.mkdir(parents=True, exist_ok=True)
            report_target.write_text(text + "\n", encoding="utf-8")
            _notify(f"报告已写入：{report_target}", json_output=json_output)

    if manifest_target is not None and report is not None:
        if dry_run:
            _notify(
                f"（dry-run：未写入 {manifest_target}；如需写入请加 --no-dry-run）",
                json_output=json_output,
            )
        else:
            manifest_target.parent.mkdir(parents=True, exist_ok=True)
            manifest_target.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _notify(f"manifest 已写入：{manifest_target}", json_output=json_output)

    if quarantine_target is None:
        return
    if dry_run:
        _notify(
            f"（dry-run：未写入 {quarantine_target}；如需写入请加 --no-dry-run）",
            json_output=json_output,
        )
        return
    if report is None:
        _notify(f"没有候选批次，未创建：{quarantine_target}", json_output=json_output)
        return
    entries = quarantine_payload(report)
    if not entries:
        _notify(f"没有需要隔离的行，未创建：{quarantine_target}", json_output=json_output)
        return
    quarantine_target.parent.mkdir(parents=True, exist_ok=True)
    payload_lines = [
        json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries
    ]
    quarantine_target.write_text("\n".join(payload_lines) + "\n", encoding="utf-8")
    _notify(f"隔离清单已写入：{quarantine_target}（{len(entries)} 行）", json_output=json_output)


def _author_chain_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    session_factory: sessionmaker[Session] | None,
) -> int:
    """gateway-only 作者归属链（默认 dry-run、零网络、零写入）。"""
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)
    dry_run = not bool(args.no_dry_run)
    json_output = bool(args.json_output)
    factory = session_factory or build_session_factory(build_engine())
    with factory() as session:
        try:
            report = attribute_gateway_author_evidence(session, as_of=moment, dry_run=dry_run)
            if dry_run:
                session.rollback()
            else:
                session.commit()
        except Exception:  # noqa: BLE001 - 明确失败并整体回滚，不静默吞掉
            session.rollback()
            raise
        finally:
            session.rollback()
    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else report.render()
    )
    safe_print(text)
    return EXIT_OK


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
    if command == "author-chain":
        return _author_chain_command(args, parser, session_factory)
    if command == "recheck":
        return _forward_recheck(args, session_factory)

    try:
        as_of = parse_as_of(getattr(args, "as_of", None))
    except ValueError as exc:
        parser.error(str(exc))
    moment = as_of if as_of is not None else datetime.now(UTC)

    json_output = bool(getattr(args, "json_output", False))
    dry_run = not bool(getattr(args, "no_dry_run", False))
    scope_name: str | None = getattr(args, "scope", None)
    input_path: Path | None = getattr(args, "input", None)
    if input_path is not None and scope_name is None:
        parser.error("--input 必须与 --scope 一起使用（否则无法判定证据类别）")
    if command in {"quarantine", "intake"} and (input_path is None or scope_name is None):
        parser.error(f"{command} 必须同时提供 --scope 与 --input")
    targets = [
        target
        for target in (getattr(args, "manifest", None), getattr(args, "quarantine", None))
        if target is not None
    ]
    if targets and input_path is not None:
        try:
            validate_output_targets(input_path, targets)
        except ValueError as exc:
            parser.error(str(exc))

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

    scope = EvidenceScope(scope_name) if scope_name is not None else None
    should_commit = command in {"intake", "workflow"} and not dry_run
    factory = session_factory or build_session_factory(build_engine())
    preflight_report: EvidenceIntakeReport | None = None
    committed_report: EvidenceIntakeReport | None = None
    author_report: AuthorChainReport | None = None
    qualification: QualificationReport | None = None
    with factory() as session:
        try:
            if input_file is not None and scope is not None:
                # ① 写入前二次验证：完整 dry-run Evidence Gateway 校验（零写入）
                preflight_report = intake_evidence(
                    session,
                    scope=scope,
                    input_file=input_file,
                    rows=rows,
                    moment=moment,
                    dry_run=True,
                )
                session.rollback()
                # ② 仅在显式 --no-dry-run 时 append-only 落库（会再次完整校验）
                if should_commit:
                    committed_report = intake_evidence(
                        session,
                        scope=scope,
                        input_file=input_file,
                        rows=rows,
                        moment=moment,
                        dry_run=False,
                    )
                    session.commit()
            if command == "workflow" and bool(getattr(args, "author_chain", False)):
                author_report = attribute_gateway_author_evidence(
                    session, as_of=moment, dry_run=dry_run
                )
                if dry_run:
                    session.rollback()
                else:
                    session.commit()
            # ③ 只读台账 + 现有 Phase 3.3 资格报告
            ledger = load_evidence_ledger(session)
            if command == "workflow":
                qualification = load_qualification_report(session, as_of=moment)
        except Exception:  # noqa: BLE001 - 明确失败并整体回滚，不静默吞掉
            session.rollback()
            raise
        finally:
            session.rollback()

    scopes = (scope,) if scope is not None else (EvidenceScope.AUTHOR, EvidenceScope.NEWS)
    batch = summarize_batch(preflight_report) if preflight_report is not None else None
    readiness = build_readiness_report(
        ledger,
        as_of=moment,
        batches=(batch,) if batch is not None else (),
        scopes=scopes,
    )
    gate = evaluate_write_gate(preflight_report) if preflight_report is not None else None
    quarantine = (
        build_quarantine_summary(preflight_report) if preflight_report is not None else None
    )
    template = _template_step()
    outcome_report = committed_report if committed_report is not None else preflight_report

    if command == "workflow":
        payload = _workflow_payload(
            as_of=moment,
            dry_run=dry_run,
            template=template,
            readiness=readiness,
            batch=batch,
            gate=gate,
            quarantine=quarantine,
            intake_report=committed_report,
            author_report=author_report,
            qualification=qualification,
        )
        text = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if json_output
            else _render_workflow_text(
                template=template,
                readiness=readiness,
                batch=batch,
                gate=gate,
                quarantine=quarantine,
                author_report=author_report,
                qualification=qualification,
            )
        )
    elif command == "quarantine":
        assert quarantine is not None  # command 校验已保证 --input/--scope
        text = (
            json.dumps(quarantine.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            if json_output
            else render_quarantine_summary(quarantine)
        )
    elif command == "preflight":
        payload = {
            "schema_version": OPERATOR_WORKFLOW_SCHEMA_VERSION,
            "report": "evidence_operator_preflight",
            "as_of": moment.isoformat(),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "readiness": readiness.to_dict(),
            "write_gate": gate.to_dict() if gate is not None else None,
            "quarantine": quarantine.to_dict() if quarantine is not None else None,
        }
        text = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if json_output
            else "\n".join(
                [
                    render_readiness_report(readiness),
                    render_write_gate(gate) if gate is not None else "",
                    render_quarantine_summary(quarantine) if quarantine is not None else "",
                ]
            )
        )
    else:  # intake
        assert outcome_report is not None  # command 校验已保证 --input/--scope
        payload = {
            "schema_version": OPERATOR_WORKFLOW_SCHEMA_VERSION,
            "report": "evidence_operator_intake",
            "as_of": moment.isoformat(),
            "dry_run": dry_run,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "write_gate": gate.to_dict() if gate is not None else None,
            "quarantine": quarantine.to_dict() if quarantine is not None else None,
            "intake": outcome_report.to_dict(),
        }
        text = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if json_output
            else "\n".join(
                [
                    render_write_gate(gate) if gate is not None else "",
                    render_quarantine_summary(quarantine) if quarantine is not None else "",
                    outcome_report.render(),
                ]
            )
        )

    safe_print(text)
    _write_artifacts(
        args, text=text, dry_run=dry_run, report=committed_report, json_output=json_output
    )
    if command == "intake" and outcome_report is not None and outcome_report.counts.quarantined:
        return EXIT_QUARANTINED
    return EXIT_OK


def _forward_recheck(
    args: argparse.Namespace, session_factory: sessionmaker[Session] | None
) -> int:
    """复用 ``scripts.evidence_readiness.py`` 的一键资格复核（只读；不另造第二套口径）。"""
    forwarded: list[str] = ["recheck"]
    if getattr(args, "scope", None):
        forwarded += ["--scope", str(args.scope)]
    if getattr(args, "input", None) is not None:
        forwarded += [
            "--input",
            str(args.input),
            "--format",
            str(getattr(args, "format", "auto")),
        ]
    if getattr(args, "as_of", None):
        forwarded += ["--as-of", str(args.as_of)]
    if getattr(args, "json_output", False):
        forwarded.append("--json")
    if getattr(args, "report", None) is not None:
        forwarded += ["--report", str(args.report)]
    if getattr(args, "no_dry_run", False):
        forwarded.append("--no-dry-run")
    return readiness_main(forwarded, session_factory=session_factory)


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
