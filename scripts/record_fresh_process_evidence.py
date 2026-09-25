"""GOLD-051：CONTROL_PLANE_FRESH_PROCESS_RESTART 机读证据采集 CLI。

仅在**非 CI 环境**下允许写证据文件；CI 环境（GITHUB_ACTIONS / CI 环境变量已设置）
必须 fail-closed 非零退出且不写文件。证据文件落盘
``.ai/ci_evidence/fresh_process_restart_evidence.json``（版本化 schema
``fresh-process-restart-evidence-v1``）。

用法::

    # ① 打印 schema 契约（零写入）
    python -m scripts.record_fresh_process_evidence --schema

    # ② 交互式采集（仅非 CI + 交互 TTY；人类操作）
    python -m scripts.record_fresh_process_evidence

    # ③ 非交互传参（测试/脚本用；仍拒绝 CI 环境）
    python -m scripts.record_fresh_process_evidence --input evidence.json

安全与边界：本工具只写**一个**证据文件，不写库、不解除 gate、不产生 verdict、不调用网络。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import fresh_process_evidence as fpe  # noqa: E402

EVIDENCE_PATH = REPO_ROOT / ".ai" / "ci_evidence" / "fresh_process_restart_evidence.json"

EXIT_OK = 0
EXIT_CI_FORBIDDEN = 2
EXIT_INVALID = 3


def _in_ci() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _git_rev_parse(ref: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record_fresh_process_evidence",
        description="采集 CONTROL_PLANE_FRESH_PROCESS_RESTART 机读证据（仅非 CI 环境可写）",
    )
    parser.add_argument(
        "--schema", action="store_true",
        help="打印版本化 schema 契约并退出（零写入）",
    )
    parser.add_argument(
        "--input",
        help="从 JSON 文件路径读取字段（非交互，测试/脚本用；仍拒绝 CI 环境）",
    )
    return parser


def _load_input(value: str) -> dict[str, Any]:
    text = value
    if not value.strip().startswith("{"):
        text = Path(value).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--input 必须是 JSON 对象")
    return data


def _interactive_collect() -> dict[str, Any]:
    """交互式采集（仅非 CI + 交互 TTY）。restart_commit_sha / 时间戳自动从 git 元数据取。"""
    print("=== 采集 fresh-process restart 证据（仅非 CI 环境）===")

    operator_identity = input("operator_identity（操作者身份，如姓名/邮箱）: ").strip()
    restart_commit_sha = _git_rev_parse("HEAD") or ""
    print(f"restart_commit_sha（自动取本地 HEAD）: {restart_commit_sha}")

    old_pid_raw = input("old_pid_terminated（逗号分隔的旧 PID 列表，留空表示无）: ").strip()
    old_pids = [int(x) for x in old_pid_raw.split(",") if x.strip().isdigit()]
    termination_method = input(
        "termination_method（旧进程终止方式，如 taskkill /F 或 SIGTERM）: "
    ).strip()

    new_process_pid = int(input("new_process_pid（新进程 PID）: ").strip() or "0")
    new_process_started_at = _now_iso()

    head_check_cmd = "git rev-parse HEAD"
    head_check_output = f"HEAD={restart_commit_sha}"

    git_pull_cmd = input(
        "git_fetch_or_pull_cmd_and_output（粘贴你执行的 fetch/pull 命令与输出）: "
    ).strip()

    ci_run_id = input("ci_run_id（restart commit 的全绿 CI run id）: ").strip()
    ci_run_conclusion = input("ci_run_conclusion_on_restart_commit（success）: ").strip()

    statement = input("human_attestation.statement（人工声明文本）: ").strip()

    return {
        "operator_identity": operator_identity,
        "restart_commit_sha": restart_commit_sha,
        "old_pid_terminated": [
            {"pid": pid, "method": termination_method, "terminated_at": _now_iso()}
            for pid in old_pids
        ],
        "new_process_pid": new_process_pid,
        "new_process_started_at": new_process_started_at,
        "head_check_cmd_and_output": f"{head_check_cmd}\n{head_check_output}",
        "git_fetch_or_pull_cmd_and_output": git_pull_cmd,
        "ci_run_id": ci_run_id,
        "ci_run_conclusion_on_restart_commit": ci_run_conclusion,
        "human_attestation": {
            "statement": statement,
            "attested_at": _now_iso(),
            "operator_identity": operator_identity,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.schema:
        print(json.dumps(fpe.schema(), ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK

    if _in_ci():
        print(
            "[evidence] CI 环境禁止生成 fresh-process 证据（fail-closed，未写入任何文件）",
            file=sys.stderr,
        )
        return EXIT_CI_FORBIDDEN

    if args.input:
        try:
            data = _load_input(args.input)
        except (OSError, ValueError) as exc:
            print(f"[evidence] 无法读取 --input：{exc}", file=sys.stderr)
            return EXIT_INVALID
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                "[evidence] 非交互 TTY 下必须显式提供 --input（fail-closed，未写入）",
                file=sys.stderr,
            )
            return EXIT_INVALID
        data = _interactive_collect()

    data["schema"] = fpe.SCHEMA_VERSION
    data["schema_version"] = 1
    data["content_digest"] = fpe.compute_content_digest(data)

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(EVIDENCE_PATH, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))

    result = fpe.verify_evidence(EVIDENCE_PATH, REPO_ROOT)
    print(
        f"[evidence] 已写入 {EVIDENCE_PATH}；"
        f"自校验 verdict={result['verdict']} reason_codes={result['reason_codes']}"
    )
    return EXIT_OK if result["verdict"] == fpe.VERDICT_CLEARED else EXIT_INVALID


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
