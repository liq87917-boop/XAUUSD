"""GOLD-030 Phase 3.3 证据链端到端审计 / 演练 CLI 集成测试（零网络 / 零数据库 / 零交易）。

覆盖：

- CLI **只读**审计模式：既有 artifact 逐字节不变、stdout 为纯 JSON、退出码语义稳定；
- CLI **演练**模式：只在显式 ``--work-dir`` 内写 fixture，仓库 / 其它目录零副作用；
- 缺 artifact / 收据被篡改 → 退出码 ``5``（**预期 BLOCKED**）+ 最早失败阶段；
- ``--out`` 唯一写开关（原子写、无 ``.tmp`` 残留）与锁冲突零写入（退出码 ``6``）;
- 演练工作目录**不得**污染仓库（退出码 ``4``）；
- **fresh subprocess** 冒烟：真实进程、干净 cwd、纯 JSON stdout、相对路径零写入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.evidence_chain_audit import main as chain_audit_main
from src.common import hashing
from src.evidence.chain_audit import (
    CHAIN_AUDIT_KIND,
    EVIDENCE_SOURCE_OPERATOR_PROVIDED,
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    EXIT_BLOCKED,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_UNUSABLE,
)
from src.evidence.readiness_runner import SingleInstanceLock
from src.evidence.rehearsal import RehearsalChain, build_rehearsal_chain

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "scripts.evidence_chain_audit"


def build_chain(tmp_path: Path, *, scenario: str = "ready") -> RehearsalChain:
    """在 ``tmp_path`` 内搭起演练链 fixture（零网络 / 零数据库）。"""
    return build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT, scenario=scenario)


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内全部文件的相对路径 → 字节摘要。

    note:
        默认**跳过** ``*.lock``：单实例锁是并发审计留痕（且被持有期间不可读），
        不应被算作"资产被改写"。
    """
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def audit_argv(chain: RehearsalChain, *extra: str) -> list[str]:
    """构造"只读审计既有 artifact"的 CLI 参数。"""
    return [
        "--inbox-dir",
        str(chain.inbox_dir),
        "--handoff",
        str(chain.handoff_report_path),
        "--attestation",
        str(chain.attestation_path),
        "--ledger",
        str(chain.ledger_path),
        "--approved-list",
        str(chain.approved_list_path),
        "--plan",
        str(chain.plan_path),
        "--operator-result",
        str(chain.operator_result_path),
        "--recheck",
        str(chain.recheck_path),
        "--readiness",
        str(chain.readiness_path),
        "--receipt",
        str(chain.receipt_path),
        "--packet",
        str(chain.packet_path),
        "--record",
        str(chain.record_path),
        *extra,
    ]


def tamper(path: Path, mutate: Any) -> None:
    """本地改写一个 JSON 文档（仅用于篡改场景）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 只读审计模式
# ---------------------------------------------------------------------------
def test_cli_audit_mode_read_only_and_pure_json(tmp_path: Path, capsys: Any) -> None:
    """只读审计既有 artifact：退出码 0、stdout 纯 JSON、artifact 逐字节不变。"""
    chain = build_chain(tmp_path)
    before = file_snapshot(chain.work_dir)

    code = chain_audit_main(audit_argv(chain, "--json"), moment=MOMENT)

    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    assert payload["kind"] == CHAIN_AUDIT_KIND
    assert payload["evidence_source"] == EVIDENCE_SOURCE_OPERATOR_PROVIDED
    assert payload["engineering_chain_ready"] is True
    assert payload["earliest_failure_stage"] is None
    assert payload["scenario"] is None
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_active"] is True
    assert payload["l3_human_gate_pending"] is True
    assert file_snapshot(chain.work_dir) == before


def test_cli_missing_artifacts_exit_five_with_earliest_stage(tmp_path: Path, capsys: Any) -> None:
    """只给出 inbox 目录 → 退出码 5（BLOCKED）+ 最早失败阶段 = attestation。"""
    chain = build_chain(tmp_path)
    capsys.readouterr()

    code = chain_audit_main(["--inbox-dir", str(chain.inbox_dir), "--json"], moment=MOMENT)

    captured = capsys.readouterr()
    assert code == EXIT_BLOCKED
    payload = json.loads(captured.out)
    assert payload["engineering_chain_ready"] is False
    assert payload["earliest_failure_stage"] == "attestation"
    assert payload["data_qualification_passed"] is False
    assert payload["blocker_active"] is True


def test_cli_tampered_receipt_exit_five(tmp_path: Path, capsys: Any) -> None:
    """收据被改写 → 退出码 5，最早失败阶段 = intake_receipt，稳定原因码可见。"""
    chain = build_chain(tmp_path)
    tamper(chain.receipt_path, lambda payload: payload.update({"receipt_id": "0" * 64}))
    capsys.readouterr()

    code = chain_audit_main(audit_argv(chain, "--json"), moment=MOMENT)

    captured = capsys.readouterr()
    assert code == EXIT_BLOCKED
    payload = json.loads(captured.out)
    assert payload["earliest_failure_stage"] == "intake_receipt"
    stages = {item["stage"]: item for item in payload["stages"]}
    assert "RECEIPT_ID_MISMATCH" in stages["intake_receipt"]["reason_codes"]
    assert stages["decision_packet"]["status"] == "NOT_EVALUATED"


def test_cli_out_is_the_only_write_switch(tmp_path: Path, capsys: Any) -> None:
    """``--out``：原子落盘报告本身，历史 artifact 不变，无 ``.tmp`` 残留。"""
    chain = build_chain(tmp_path)
    before = file_snapshot(chain.work_dir)
    report = chain.work_dir / "audit.json"
    capsys.readouterr()

    code = chain_audit_main(
        audit_argv(chain, "--json", "--out", str(report)), moment=MOMENT
    )

    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["chain_id"] == json.loads(captured.out)["chain_id"]
    after = file_snapshot(chain.work_dir)
    assert set(after) - set(before) == {"audit.json"}
    for relative, digest in before.items():
        assert after[relative] == digest
    assert not list(chain.work_dir.glob("*.tmp"))
    assert "已原子写入" in captured.err


def test_cli_lock_conflict_zero_write(tmp_path: Path, capsys: Any) -> None:
    """锁冲突：退出码 6、零写入、stdout 为空。"""
    chain = build_chain(tmp_path)
    report = chain.work_dir / "audit.json"
    capsys.readouterr()
    held = SingleInstanceLock(report.with_name(report.name + ".lock"), owner="held-by-test")
    with held:
        before = file_snapshot(chain.work_dir)
        code = chain_audit_main(
            audit_argv(chain, "--json", "--out", str(report)), moment=MOMENT
        )
        assert file_snapshot(chain.work_dir) == before
    captured = capsys.readouterr()
    assert code == EXIT_LOCK_CONFLICT
    assert captured.out == ""
    assert not report.exists()


def test_cli_rejects_rehearsal_with_artifact_flags(tmp_path: Path) -> None:
    """演练模式与既有 artifact 参数互斥（fail-closed，绝不混用）。"""
    with pytest.raises(SystemExit) as exc:
        chain_audit_main(
            [
                "--rehearsal",
                "--work-dir",
                str(tmp_path / "rehearsal"),
                "--inbox-dir",
                str(tmp_path),
            ]
        )
    assert exc.value.code == 2


def test_cli_requires_inbox_dir_or_rehearsal(tmp_path: Path) -> None:
    """既不给 ``--inbox-dir`` 也不给 ``--rehearsal`` → 参数错误（绝不猜路径）。"""
    with pytest.raises(SystemExit) as exc:
        chain_audit_main(["--json"])
    assert exc.value.code == 2


def test_cli_rehearsal_refuses_repo_work_dir(tmp_path: Path, capsys: Any) -> None:
    """演练工作目录写进仓库非 ``logs/`` 路径 → 退出码 3（路径不可用），**不**创建任何目录。"""
    target = REPO_ROOT / "tests" / "_chain_audit_cli_should_never_exist"
    capsys.readouterr()

    code = chain_audit_main(["--rehearsal", "--work-dir", str(target), "--json"], moment=MOMENT)

    captured = capsys.readouterr()
    assert code == EXIT_UNUSABLE
    assert captured.out == ""
    assert not target.exists()



# ---------------------------------------------------------------------------
# 演练模式（单一 rehearsal 命令）
# ---------------------------------------------------------------------------
def test_cli_rehearsal_ready_mode(tmp_path: Path, capsys: Any) -> None:
    """单一 rehearsal 命令：只在显式工作目录内写 fixture，工程链全绿但仍不放行。"""
    work_dir = tmp_path / "rehearsal"
    capsys.readouterr()

    code = chain_audit_main(
        ["--rehearsal", "--work-dir", str(work_dir), "--json"], moment=MOMENT
    )

    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    assert payload["evidence_source"] == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert payload["scenario"] == "ready"
    assert payload["rehearsal"] is True
    assert payload["engineering_chain_ready"] is True
    assert payload["earliest_failure_stage"] is None
    assert payload["real_evidence_missing"] is True
    assert payload["human_verification_missing"] is True
    assert payload["l3_human_gate_pending"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_active"] is True
    # 只写显式工作目录：`tmp_path` 下只多出 rehearsal 一个目录
    assert {path.name for path in tmp_path.iterdir()} == {"rehearsal"}


def test_cli_rehearsal_blocked_mode(tmp_path: Path, capsys: Any) -> None:
    """blocked 场景：packet 诚实 BLOCKED，记录 reject，工程链仍逐段一致。"""
    capsys.readouterr()

    code = chain_audit_main(
        [
            "--rehearsal",
            "--work-dir",
            str(tmp_path / "rehearsal"),
            "--scenario",
            "blocked",
            "--json",
        ],
        moment=MOMENT,
    )

    captured = capsys.readouterr()
    assert code == EXIT_OK
    payload = json.loads(captured.out)
    stages = {item["stage"]: item for item in payload["stages"]}
    assert stages["decision_packet"]["facts"]["packet_status"] == "BLOCKED_PENDING_EVIDENCE"
    assert stages["decision_packet"]["facts"]["submit_to_l3_human_gate"] == "false"
    assert stages["decision_record"]["facts"]["decision"] == "reject"
    assert payload["engineering_chain_ready"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["blocker_active"] is True


def test_cli_rehearsal_with_out_writes_report(tmp_path: Path, capsys: Any) -> None:
    """演练 + ``--out``：报告原子落盘，提示走 stderr（stdout 仍是纯 JSON）。"""
    work_dir = tmp_path / "rehearsal"
    report = work_dir / "audit.json"
    capsys.readouterr()

    code = chain_audit_main(
        ["--rehearsal", "--work-dir", str(work_dir), "--json", "--out", str(report)],
        moment=MOMENT,
    )

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert json.loads(captured.out)["chain_id"] == json.loads(
        report.read_text(encoding="utf-8")
    )["chain_id"]
    assert "演练" in captured.err


def test_cli_exposes_no_intake_or_write_flags() -> None:
    """``--help`` 绝不暴露任何 intake / 写库 / qualify / advance 开关。"""
    from scripts.evidence_chain_audit import build_parser

    help_text = build_parser().format_help()
    for token in ("--no-dry-run", "--qualify", "--advance", "--commit", "--session", "--live"):
        assert token not in help_text



def _run_subprocess(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在**干净 cwd** 里以真实子进程运行 CLI（项目通过 ``PYTHONPATH`` 注入）。"""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def test_subprocess_rehearsal_zero_side_effects(tmp_path: Path) -> None:
    """fresh subprocess 演练：退出码 0、stdout 纯 JSON、cwd 内只有显式工作目录。"""
    completed = _run_subprocess(
        tmp_path,
        "--rehearsal",
        "--work-dir",
        str(tmp_path / "rehearsal"),
        "--json",
        "--as-of",
        MOMENT.isoformat(),
    )

    assert completed.returncode == EXIT_OK, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["engineering_chain_ready"] is True
    assert payload["evidence_source"] == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert {path.name for path in tmp_path.iterdir()} == {"rehearsal"}


def test_subprocess_audit_mode_is_read_only(tmp_path: Path) -> None:
    """fresh subprocess 只读审计：退出码 0、stdout 纯 JSON、artifact 零改写。"""
    chain = build_chain(tmp_path)
    before = file_snapshot(chain.work_dir)

    completed = _run_subprocess(
        tmp_path, *audit_argv(chain, "--json"), "--as-of", MOMENT.isoformat()
    )

    assert completed.returncode == EXIT_OK, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["evidence_source"] == EVIDENCE_SOURCE_OPERATOR_PROVIDED
    assert payload["engineering_chain_ready"] is True
    assert file_snapshot(chain.work_dir) == before


def test_subprocess_tampered_chain_exit_five(tmp_path: Path) -> None:
    """fresh subprocess：篡改后可稳定解析最早失败阶段（退出码 5）。"""
    chain = build_chain(tmp_path)
    tamper(chain.record_path, lambda payload: payload.update({"reviewer": "someone-else"}))

    completed = _run_subprocess(
        tmp_path, *audit_argv(chain, "--json"), "--as-of", MOMENT.isoformat()
    )

    assert completed.returncode == EXIT_BLOCKED, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["earliest_failure_stage"] == "decision_record"
    stages = {item["stage"]: item for item in payload["stages"]}
    assert "RECORD_ID_MISMATCH" in stages["decision_record"]["reason_codes"]
    assert payload["data_qualification_passed"] is False


def test_subprocess_help_smoke(tmp_path: Path) -> None:
    """``--help`` 在 fresh subprocess 可用（零写入、无 Traceback）。"""
    completed = _run_subprocess(tmp_path, "--help")

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
    assert "Traceback" not in completed.stderr
    assert list(tmp_path.iterdir()) == []

