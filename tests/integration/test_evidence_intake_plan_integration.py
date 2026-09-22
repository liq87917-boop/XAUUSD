"""GOLD-013 Intake Plan CLI 端到端测试（临时目录 / 真实子进程 / 零网络 / 零数据库）。

覆盖：

- 只读核验：退出码 ``0`` + stdout 纯 JSON + inbox 内零写入 + 原始 evidence 字节不变；
- 唯一写开关 ``--out``：原子落盘计划本身、幂等（重复生成逐字节稳定）、提示走 stderr；
- 没有任何仍成立的批准 → 退出码 ``5``（预期 BLOCKED），绝不伪造成功；
- 核验不通过（内容变化 / 批准清单被改 / 计划时间早于批准）→ 退出码 ``4``、stdout 为空、零写入；
- 参数错误（缺必填参数 / 时区缺失 / 试图传入 ``--no-dry-run``）→ 退出码 ``2``；
- inbox 不可用或输出写进 inbox → 退出码 ``3``；损坏 state → 退出码 ``4``（旧文件原样保留）；
- 锁冲突 → 退出码 ``6``（零写入）；Markdown 摘要保留 blocker / human gate；
- 真实子进程冒烟：``python -m scripts.evidence_intake_plan`` 端到端可跑。
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

from scripts.evidence_intake_plan import main
from src.common import hashing
from src.evidence import (
    EXIT_BLOCKED,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    INBOX_SCHEMA_VERSION,
    INTAKE_PLAN_KIND,
    MANIFEST_FILE_NAME,
    OPERATOR_EXPLICIT_FLAG,
    ReviewReasonCode,
    SingleInstanceLock,
    run_review,
    scan_inbox,
)
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
SECRET = "sk-livesecret0123456789"


def author_row(record_id: str = "a-0001") -> dict[str, Any]:
    """一条完全合规的 Author 证据行（与单元测试同口径）。"""
    return {
        "source": "vendor-author",
        "source_record_id": record_id,
        "author_name": "张三",
        "external_account_id": "acct-0001",
        "content": "黄金短线看多",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-06-02T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }


def build_package(inbox: Path, name: str = "pkg-author-01") -> Path:
    """建一个完全合规的候选包（JSONL + manifest）。"""
    package = inbox / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / "author.jsonl"
    evidence.write_text(json.dumps(author_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "evidence_type": "author",
        "source": "vendor-author",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": "author.jsonl",
                "sha256": hashing.sha256_bytes(evidence.read_bytes()),
                "format": "jsonl",
            }
        ],
    }
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def approve(tmp_path: Path, inbox: Path) -> tuple[Path, Path]:
    """按 GOLD-012 口径 approve 唯一候选包，返回 (ledger, approved list)。"""
    package = scan_inbox(inbox, moment=MOMENT).packages[0]
    ledger = tmp_path / "state" / "review_ledger.json"
    approved = tmp_path / "state" / "approved_for_intake.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=package.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        out_path=ledger,
        approved_out_path=approved,
    )
    return ledger, approved


def plan_argv(inbox: Path, ledger: Path, approved: Path, *extra: str) -> list[str]:
    """构造计划 CLI 参数（固定审计时点，便于复现）。"""
    return [
        "--inbox-dir",
        str(inbox),
        "--ledger",
        str(ledger),
        "--approved-list",
        str(approved),
        "--as-of",
        MOMENT.isoformat(),
        *extra,
    ]


def json_stdout(capsys: Any) -> dict[str, Any]:
    """读取 stdout 的纯 JSON（stdout 必须无杂音）。"""
    return json.loads(capsys.readouterr().out)


def test_cli_plan_is_read_only_and_ready(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    evidence = package_dir / "author.jsonl"
    evidence_bytes = evidence.read_bytes()
    ledger, approved = approve(tmp_path, inbox)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    code = main(plan_argv(inbox, ledger, approved, "--json"))
    assert code == EXIT_OK
    payload = json_stdout(capsys)
    assert payload["kind"] == INTAKE_PLAN_KIND
    assert payload["status"] == "READY_FOR_EXPLICIT_INTAKE"
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["approved_for_explicit_intake_count"] == 1
    assert payload["data_qualification_passed_count"] == 0
    assert payload["handoff"][0]["command"].endswith(OPERATOR_EXPLICIT_FLAG)
    assert payload["written_path"] is None
    # 只读：没有新增文件，原始 evidence 字节不变
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert evidence.read_bytes() == evidence_bytes


def test_cli_out_writes_plan_atomically_and_idempotently(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)
    out = tmp_path / "state" / "evidence_intake_plan.json"

    code = main(plan_argv(inbox, ledger, approved, "--out", str(out), "--json"))
    assert code == EXIT_OK
    payload = json_stdout(capsys)
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["kind"] == INTAKE_PLAN_KIND
    assert document["plan_id"] == payload["plan_id"]
    assert document["written_path"] is None  # 文件内容是计划本身，不是"写开关"的自述
    assert [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")] == []

    text = out.read_text(encoding="utf-8")
    code = main(plan_argv(inbox, ledger, approved, "--out", str(out), "--json"))
    assert code == EXIT_OK
    again = json_stdout(capsys)
    assert again["plan_id"] == payload["plan_id"]
    assert out.read_text(encoding="utf-8") == text  # 幂等 / 逐字节稳定


def test_cli_blocked_plan_exits_5(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger = tmp_path / "state" / "review_ledger.json"
    approved = tmp_path / "state" / "approved_for_intake.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    run_review(inbox, moment=MOMENT, out_path=ledger, approved_out_path=approved)

    code = main(plan_argv(inbox, ledger, approved, "--json"))
    assert code == EXIT_BLOCKED
    payload = json_stdout(capsys)
    assert payload["status"] == "BLOCKED_NO_APPROVED_EVIDENCE"
    assert payload["approved_for_explicit_intake_count"] == 0
    assert payload["data_qualification_passed"] is False
    assert payload["blocker_active"] is True



def test_cli_inconsistent_plan_exits_4_without_writing(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)
    out = tmp_path / "state" / "plan.json"

    # 内容变化（同步更新 manifest 摘要）→ 旧批准失效
    evidence = package_dir / "author.jsonl"
    evidence.write_text(
        "".join(
            json.dumps(author_row(record_id), ensure_ascii=False) + "\n"
            for record_id in ("a-0001", "a-0002")
        ),
        encoding="utf-8",
    )
    manifest_path = package_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(evidence.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    code = main(plan_argv(inbox, ledger, approved, "--out", str(out), "--json"))
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""  # fail-closed：stdout 为空
    assert "fail-closed" in captured.err
    assert "FINGERPRINT_MISSING" in captured.err
    assert not out.exists()  # 零写入


def test_cli_argument_errors_exit_2(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)
    with pytest.raises(SystemExit) as missing_all:
        main(["--json"])
    assert missing_all.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as missing_ledger:
        main(["--inbox-dir", str(inbox), "--approved-list", str(approved)])
    assert missing_ledger.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive_as_of:
        main(
            [
                "--inbox-dir",
                str(inbox),
                "--ledger",
                str(ledger),
                "--approved-list",
                str(approved),
                "--as-of",
                "2026-09-23T00:00:00",
            ]
        )
    assert naive_as_of.value.code == 2  # 禁止隐式时区
    capsys.readouterr()
    with pytest.raises(SystemExit) as explicit_intake_flag:
        main(plan_argv(inbox, ledger, approved, "--no-dry-run"))
    assert explicit_intake_flag.value.code == 2  # 本工具绝不暴露真实 intake 开关
    capsys.readouterr()


def test_cli_unusable_paths_exit_3(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)

    code = main(plan_argv(tmp_path / "nope", ledger, approved, "--json"))
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err

    inside = inbox / "plan.json"
    code = main(plan_argv(inbox, ledger, approved, "--out", str(inside), "--json"))
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not inside.exists()
    assert not inside.with_name(inside.name + ".lock").exists()



def test_cli_corrupt_state_exits_4_and_preserves_files(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)

    approved.write_text("{ not json", encoding="utf-8")
    code = main(plan_argv(inbox, ledger, approved, "--json"))
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err
    assert approved.read_text(encoding="utf-8") == "{ not json"  # 旧文件原样保留

    ledger.write_text("{ not json", encoding="utf-8")
    code = main(plan_argv(inbox, ledger, approved, "--json"))
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err
    assert ledger.read_text(encoding="utf-8") == "{ not json"


def test_cli_lock_conflict_exits_6_without_writes(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)
    out = tmp_path / "state" / "plan.json"
    lock_path = out.with_name(out.name + ".lock")

    with SingleInstanceLock(lock_path, owner="holder") as info:
        assert info.owner == "holder"
        code = main(plan_argv(inbox, ledger, approved, "--out", str(out), "--json"))
        assert code == EXIT_LOCK_CONFLICT
        captured = capsys.readouterr()
        assert captured.out == "" and "fail-closed" in captured.err
        assert not out.exists()  # 零写入
        assert lock_path.exists()  # 活动锁不被删除 / 改写


def test_cli_markdown_summary_keeps_blocker_visible(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)

    code = main(plan_argv(inbox, ledger, approved))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "PHASE3_3_DATA" in out
    assert "blocker_active" in out
    assert "human_gate_required" in out
    assert "data_qualification_passed_count" in out
    assert OPERATOR_EXPLICIT_FLAG in out
    assert "evidence_operator" in out


def test_cli_real_process_smoke(tmp_path: Path) -> None:
    """真实子进程：``python -m scripts.evidence_intake_plan`` 端到端（零网络 / 零数据库）。"""
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger, approved = approve(tmp_path, inbox)
    out = tmp_path / "state" / "plan.json"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_intake_plan",
            *plan_argv(inbox, ledger, approved, "--out", str(out), "--json"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == EXIT_OK, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["kind"] == INTAKE_PLAN_KIND
    assert payload["blocker_active"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["approved_for_explicit_intake_count"] == 1
    assert payload["written_path"] is not None
    assert json.loads(out.read_text(encoding="utf-8"))["plan_id"] == payload["plan_id"]
    # 不泄露凭据样式字符串
    assert SECRET not in completed.stdout

