"""Evidence Readiness 状态变更通知 CLI（**GOLD-009**，只读 / 默认 dry-run）。

把当前真实就绪度状态转换为**确定性、脱敏、可幂等投递**的本地通知事件，
让 operator 只在 blocker 状态、缺口或 ``ready_for_human_review`` 发生**有意义变化**时收到提示。

用法::

    # 默认：只读台账 + 打印本次快照与事件（零写入、零网络）
    python -m scripts.evidence_readiness_watch --json

    # 与上次快照比较（需显式给出 state 路径；仍零写入）
    python -m scripts.evidence_readiness_watch --state logs/evidence/readiness_state.json --json

    # 显式持久化：原子写新快照 / 事件文件（唯一写开关；其余参数默认零写入）
    python -m scripts.evidence_readiness_watch `
        --state logs/evidence/readiness_state.json `
        --out logs/evidence/readiness_state.json `
        --events logs/evidence/readiness_events.json --json

    # 追加候选文件 dry-run（零写入），让隔离原因码变化可被检测
    python -m scripts.evidence_readiness_watch --scope news --input logs/evidence/news.csv --json

安全与边界：

- **默认只读 / 零网络 / 零写入**：不抓取站点、不写数据库、不联网；``--state`` 只读，
  只有显式 ``--out`` 才写快照、只有显式 ``--events`` 才写事件，两者均为**原子写**；
- **不接第三方推送**：不发送邮件 / 短信 / Webhook，事件只落在本地文件或 stdout；
- **不自动解除 blocker**：``blocker_active`` / ``human_gate_required`` 恒为 true；
  ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 false；
  ``ready_for_human_review=true`` 仍须 ``DEVELOPMENT_PROTOCOL`` 的 L3 人工 Gate；
- **脱敏**：只输出计数 / 阈值 / 缺口 / 稳定原因码 / 时间，不输出正文、token / API key /
  Authorization 或完整 source config；
- 退出码：``0`` 量化门槛达标（**仍需人工 Gate**）/ ``2`` 参数或输入错误 / ``3`` 输入没有数据行 /
  ``4`` state 文件损坏（**安全失败**，且不写任何输出）/ ``5`` 仍未达标（诚实保持 BLOCKED）。
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
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    WATCH_NOTE,
    WATCH_REPORT_NAME,
    WATCH_SCHEMA_VERSION,
    EvidenceScope,
    InputFile,
    InputRow,
    ReadinessSnapshot,
    SnapshotStateError,
    WatchEvent,
    build_handoff_report,
    build_snapshot,
    detect_changes,
    intake_evidence,
    load_evidence_ledger,
    load_snapshot_state,
    read_input_file,
    render_watch_summary,
    write_events,
    write_snapshot_state,
)
from src.monitoring import build_readiness_report, summarize_batch  # noqa: E402

#: 退出码（机器可读；与模块 docstring 一致）
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_NO_ROWS = 3
#: state 文件损坏：安全失败，**不写任何输出**
EXIT_STATE_INVALID = 4
#: 仍未达标：JSON 与退出状态都诚实保持 BLOCKED（不得把工具完成当作数据资格通过）
EXIT_BLOCKED = 5


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（全部参数均为非敏感项；默认只读、零网络、零写入）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_readiness_watch",
        description=(
            "Evidence Readiness 状态变更通知：默认只读、默认零网络、默认零写入；"
            "只在状态发生有意义变化时产生脱敏事件；任何结果都不会解除 PHASE3_3_DATA。"
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
        "--state",
        type=Path,
        default=None,
        help="上次快照 state 路径（**只读**；文件不存在 = 首次运行；损坏则安全失败退出 4）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="新快照落盘路径（**必须显式指定**；原子写；缺省零写入）",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=None,
        help="事件落盘路径（**必须显式指定**；原子写；缺省零写入）",
    )
    return parser


def _same_file(left: Path, right: Path) -> bool:
    """判断两个路径是否指向同一文件（含硬链接别名；不存在的路径按解析结果比较）。"""
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
    except OSError:  # pragma: no cover - 平台相关的 stat 失败：退回纯路径比较
        pass
    return left.resolve() == right.resolve()


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON。"""
    if json_output:
        print(safe_text(message), file=sys.stderr)
    else:
        safe_print(message)


def _document(
    snapshot: ReadinessSnapshot,
    events: tuple[WatchEvent, ...],
    previous: ReadinessSnapshot | None,
) -> dict[str, Any]:
    """构造 stdout 的稳定 JSON 文档（诚实字段硬编码，绝不透传可被篡改的布尔值）。"""
    snapshot_dict = snapshot.to_dict()
    return {
        "report": WATCH_REPORT_NAME,
        "schema_version": WATCH_SCHEMA_VERSION,
        "generated_at": snapshot_dict["generated_at"],
        "fingerprint": snapshot_dict["fingerprint"],
        "previous_fingerprint": previous.fingerprint if previous is not None else None,
        "blocker_code": snapshot_dict["blocker_code"],
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "ready_for_human_review": snapshot_dict["ready_for_human_review"],
        "changed": bool(events),
        "event_count": len(events),
        "events": [event.to_dict() for event in events],
        "snapshot": snapshot_dict,
        "notes": [WATCH_NOTE],
    }


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
    state_path: Path | None = args.state
    out_path: Path | None = args.out
    events_path: Path | None = args.events
    if scope_name is not None and input_path is None:
        parser.error("--scope 只在提供候选文件（--input）时有意义，请与 --input 一起使用")
    if input_path is not None and scope_name is None:
        parser.error("--input 必须与 --scope 一起使用（否则无法判定证据类别）")
    if out_path is not None and events_path is not None and _same_file(out_path, events_path):
        parser.error("--out 与 --events 必须指向不同文件（快照与事件是两种文档）")
    for label, target in (("--state", state_path), ("--input", input_path)):
        if target is not None and events_path is not None and _same_file(target, events_path):
            parser.error(f"--events 不能与 {label} 指向同一文件")
    if input_path is not None and out_path is not None and _same_file(input_path, out_path):
        parser.error("--out 不能与 --input 指向同一文件")

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

    # 先读上次快照：损坏即**安全失败**，绝不写任何输出（避免把坏状态当首次快照）
    previous: ReadinessSnapshot | None = None
    if state_path is not None:
        try:
            previous = load_snapshot_state(state_path)
        except SnapshotStateError as exc:
            print(
                f"[evidence] state 不可用（安全失败，未写入任何输出）：{safe_text(str(exc))}",
                file=sys.stderr,
            )
            return EXIT_STATE_INVALID

    factory = session_factory or build_session_factory(build_engine())
    batch = None
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
                batch = summarize_batch(batch_report)
            ledger = load_evidence_ledger(session)
        finally:
            # 只读 / dry-run：显式回滚，杜绝任何隐式写入
            session.rollback()

    readiness = build_readiness_report(
        ledger,
        as_of=moment,
        batches=(batch,) if batch is not None else (),
        scopes=(EvidenceScope.AUTHOR, EvidenceScope.NEWS),
    )
    handoff = build_handoff_report(readiness, batch=batch)
    snapshot = build_snapshot(handoff, generated_at=moment)
    events = detect_changes(previous, snapshot, detected_at=moment)

    json_output = bool(args.json_output)
    text = (
        json.dumps(
            _document(snapshot, events, previous), ensure_ascii=False, indent=2, sort_keys=True
        )
        if json_output
        else render_watch_summary(snapshot, events, previous=previous)
    )
    safe_print(text)

    if out_path is not None:
        write_snapshot_state(out_path, snapshot)
        _notify(f"[evidence] readiness 快照已原子写入：{out_path}", json_output=json_output)
    if events_path is not None:
        write_events(events_path, events, snapshot=snapshot)
        _notify(f"[evidence] 事件已原子写入：{events_path}", json_output=json_output)
    return EXIT_OK if handoff.ready_for_human_review else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
