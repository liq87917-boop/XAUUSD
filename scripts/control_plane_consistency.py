"""控制面一致性检查 CLI（GOLD-057）。

默认 dry-run 只写 stdout；仅显式 ``--output`` 才可写 ``<root>/.ai/runtime/**`` 或系统临时
目录（复用 ``orchestrator/planner_snapshot_output.py`` 的白名单守卫）。对
``.ai/PROJECT_STATE.json``、``.ai/GPT_REVIEW_LEDGER.json``、``.ai/results/**``、
``.ai/adjudications/**`` 零写入。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator import control_plane_status as cps
from orchestrator import planner_snapshot_output as pso

EXIT_OK = 0
EXIT_INCONSISTENT = 3  # 与 0/2/4 的漂移/拒绝语义互不干扰


def load_state(root: Path) -> dict:
    """读取 ``.ai/PROJECT_STATE.json``（只读）。"""
    path = root / ".ai" / "PROJECT_STATE.json"
    return json.loads(path.read_text(encoding="utf-8"))


def render_report(state: dict) -> str:
    """渲染机器可读的一致性报告（只读，不落盘）。"""
    violations = cps.status_violations(state) + cps.phase33_violations(state)
    report = {
        "schema": "gold-ai/control-plane-consistency/v1",
        "project": state.get("project"),
        "status": state.get("status"),
        "queue_status": state.get("queue_status"),
        "derived_queue_status": cps.derive_queue_status(state),
        "phase33_frozen": cps.phase33_frozen(state),
        "phase33_active": cps.phase33_active(state),
        "violations": violations,
        "consistent": not violations,
    }
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="控制面一致性检查（默认 dry-run 只写 stdout）",
    )
    parser.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    parser.add_argument(
        "--output",
        default=None,
        help="只允许写 <root>/.ai/runtime/** 或系统临时目录",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    state = load_state(root)
    rendered = render_report(state)
    violations = cps.status_violations(state) + cps.phase33_violations(state)

    if args.output:
        target, reason = pso.resolve_output_target(root, args.output)
        if target is None:
            print(reason, file=sys.stderr)
            return pso.EXIT_OUTPUT_REJECTED
        pso.write_snapshot_output(target, rendered)
    else:
        sys.stdout.write(rendered)

    return EXIT_OK if not violations else EXIT_INCONSISTENT


if __name__ == "__main__":
    raise SystemExit(main())
