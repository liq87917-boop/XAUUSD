"""GOLD-012 Evidence Inbox 人工复核 CLI 端到端测试（临时文件 / 真实子进程 / 零网络 / 零数据库）。

覆盖：

- 只读列出：空 ledger 退出码 ``5``（预期 BLOCKED），stdout 为纯 JSON，inbox 内零写入；
- 记录 approve：退出码 ``0``、ledger + 批准清单原子落盘、重复提交幂等（不新增记录）；
- 内容变化：旧批准失效（``invalidated``）且对旧指纹 approve 退出码 ``4``（fail-closed，零写入）；
- 冲突：未显式 revision / override 退出码 ``4``；显式 revision + override 退出码 ``0`` 且历史保留；
- 参数 / 词表错误退出码 ``2``；inbox 缺失 / 输出写进 inbox 退出码 ``3``；
- 损坏 ledger 退出码 ``4``（旧文件原样保留）；锁冲突退出码 ``6``（零写入）；
- 真实子进程冒烟：``python -m scripts.evidence_review`` 端到端可跑（无网络、无数据库）。
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

from scripts.evidence_review import main
from src.common import hashing
from src.evidence import (
    EXIT_LOCK_CONFLICT,
    EXIT_NO_DECISION,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    REASON_CODES_BY_DECISION,
    ReviewDecision,
    ReviewReasonCode,
    SingleInstanceLock,
    scan_inbox,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
APPROVE_CODE = ReviewReasonCode.APPROVED_FOR_EXPLICIT_INTAKE.value
REJECT_CODE = ReviewReasonCode.REJECTED_AUTHORIZATION_INSUFFICIENT.value


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


def fingerprint_of(inbox: Path) -> str:
    """取 inbox 中唯一候选包的内容指纹（GOLD-011 口径）。"""
    report = scan_inbox(inbox, moment=MOMENT)
    assert len(report.packages) == 1, report.to_dict()
    return report.packages[0].fingerprint


def decide_argv(
    inbox: Path,
    ledger: Path,
    *,
    decision: str = "approve",
    fingerprint: str,
    reason_code: str = APPROVE_CODE,
    reviewer: str = "operator-li",
    extra: list[str] | None = None,
) -> list[str]:
    """构造一次决策的 CLI 参数（测试辅助）。"""
    argv = [
        "--inbox-dir",
        str(inbox),
        "--ledger",
        str(ledger),
        "--out",
        str(ledger),
        "--decision",
        decision,
        "--fingerprint",
        fingerprint,
        "--reviewer",
        reviewer,
        "--reason-code",
        reason_code,
        "--as-of",
        MOMENT.isoformat(),
    ]
    return argv + list(extra or [])


def json_stdout(capsys: Any) -> dict[str, Any]:
    """读取 stdout 的纯 JSON（stdout 必须无杂音）。"""
    return json.loads(capsys.readouterr().out)

def test_cli_list_only_empty_inbox_exits_5_without_writing(
    tmp_path: Path, capsys: Any
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    code = main(["--inbox-dir", str(inbox), "--json", "--as-of", MOMENT.isoformat()])
    assert code == EXIT_NO_DECISION
    payload = json_stdout(capsys)
    assert payload["action"] == "LIST_ONLY"
    assert payload["counts"]["ledger_decisions"] == 0
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["approved_list"]["approved_count"] == 0
    assert list(inbox.iterdir()) == []  # 只读运行绝不写入 inbox


def test_cli_approve_writes_ledger_and_approved_list_idempotently(
    tmp_path: Path, capsys: Any
) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    evidence = package_dir / "author.jsonl"
    evidence_bytes = evidence.read_bytes()
    ledger = tmp_path / "state" / "review_ledger.json"
    approved = tmp_path / "state" / "approved_for_intake.json"
    fingerprint = fingerprint_of(inbox)
    argv = decide_argv(inbox, ledger, fingerprint=fingerprint) + [
        "--approved-out",
        str(approved),
        "--json",
    ]

    code = main(argv)
    assert code == EXIT_OK
    payload = json_stdout(capsys)
    assert payload["action"] == "DECISION_RECORDED"
    assert payload["decision"]["decision"] == "APPROVE"
    assert payload["approved_list"]["approved_count"] == 1
    document = json.loads(ledger.read_text(encoding="utf-8"))
    assert document["decision_count"] == 1
    assert document["blocker_active"] is True
    assert document["data_qualification_passed"] is False
    assert json.loads(approved.read_text(encoding="utf-8"))["approved_count"] == 1
    assert [item.name for item in ledger.parent.iterdir() if item.name.endswith(".tmp")] == []

    # 幂等：完全相同的决策重复提交不新增记录（也不改写历史）
    original = ledger.read_bytes()
    code = main(argv)
    assert code == EXIT_OK
    second = json_stdout(capsys)
    assert second["action"] == "DECISION_IDEMPOTENT"
    assert second["counts"]["ledger_decisions"] == 1
    assert ledger.read_bytes() == original
    # 原始 evidence 字节级未被改写，包内也没有新增文件
    assert evidence.read_bytes() == evidence_bytes
    assert sorted(item.name for item in package_dir.iterdir()) == [
        "author.jsonl",
        MANIFEST_FILE_NAME,
    ]



def test_cli_content_change_invalidates_approval(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox)
    ledger = tmp_path / "review_ledger.json"
    old_fingerprint = fingerprint_of(inbox)
    code = main(decide_argv(inbox, ledger, fingerprint=old_fingerprint) + ["--json"])
    assert code == EXIT_OK
    capsys.readouterr()

    # 内容变化：追加一行并同步更新 manifest 摘要
    evidence = package_dir / "author.jsonl"
    rows = (author_row("a-0001"), author_row("a-0002"))
    evidence.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    manifest_path = package_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(evidence.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # ① 只读列出：旧批准失效，且**不静默放行**
    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--ledger",
            str(ledger),
            "--json",
            "--as-of",
            MOMENT.isoformat(),
        ]
    )
    assert code == EXIT_NO_DECISION
    payload = json_stdout(capsys)
    assert payload["approved_list"]["approved_count"] == 0
    invalidated = payload["approved_list"]["invalidated"][0]
    assert invalidated["fingerprint"] == old_fingerprint
    assert invalidated["reason_code"] == "CANDIDATE_MISSING"
    assert payload["counts"]["fingerprints_reviewed"] == 1  # 历史仍保留

    # ② 对旧指纹继续 approve → fail-closed（退出码 4，零写入）
    before = ledger.read_bytes()
    code = main(decide_argv(inbox, ledger, fingerprint=old_fingerprint) + ["--json"])
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fail-closed" in captured.err
    assert ledger.read_bytes() == before


def test_cli_conflict_requires_explicit_revision_and_override(
    tmp_path: Path, capsys: Any
) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger = tmp_path / "review_ledger.json"
    fingerprint = fingerprint_of(inbox)
    assert main(decide_argv(inbox, ledger, fingerprint=fingerprint) + ["--json"]) == EXIT_OK
    capsys.readouterr()
    before = ledger.read_bytes()

    conflict = decide_argv(
        inbox, ledger, decision="reject", fingerprint=fingerprint, reason_code=REJECT_CODE
    ) + ["--json"]
    code = main(conflict)
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err
    assert ledger.read_bytes() == before  # 冲突绝不静默覆盖

    override = decide_argv(
        inbox,
        ledger,
        decision="reject",
        fingerprint=fingerprint,
        reason_code=REJECT_CODE,
        extra=["--revision", "2", "--override"],
    ) + ["--json"]
    code = main(override)
    assert code == EXIT_OK
    payload = json_stdout(capsys)
    assert payload["decision"]["revision"] == 2
    assert payload["decision"]["override"] is True
    document = json.loads(ledger.read_text(encoding="utf-8"))
    assert document["decision_count"] == 2  # 历史全部保留
    assert [item["decision"] for item in document["decisions"]] == ["APPROVE", "REJECT"]
    assert payload["approved_list"]["approved_count"] == 0



def test_cli_argument_and_reason_code_errors_exit_2(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert ReviewDecision.APPROVE.value in {"APPROVE", "REJECT", "NEEDS_CHANGES"}
    assert APPROVE_CODE in REASON_CODES_BY_DECISION[ReviewDecision.APPROVE]
    with pytest.raises(SystemExit) as missing_inbox_dir:
        main(["--json"])
    assert missing_inbox_dir.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive_as_of:
        main(["--inbox-dir", str(inbox), "--as-of", "2026-09-23T00:00:00"])
    assert naive_as_of.value.code == 2  # 禁止隐式时区
    capsys.readouterr()
    with pytest.raises(SystemExit) as missing_decision_args:
        main(["--inbox-dir", str(inbox), "--decision", "approve"])
    assert missing_decision_args.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as wrong_reason:
        main(
            [
                "--inbox-dir",
                str(inbox),
                "--decision",
                "approve",
                "--fingerprint",
                "a" * 64,
                "--reviewer",
                "operator-li",
                "--reason-code",
                REJECT_CODE,
            ]
        )
    assert wrong_reason.value.code == 2  # 原因码必须与决策匹配
    capsys.readouterr()
    with pytest.raises(SystemExit) as override_without_revision:
        main(
            [
                "--inbox-dir",
                str(inbox),
                "--decision",
                "reject",
                "--fingerprint",
                "a" * 64,
                "--reviewer",
                "operator-li",
                "--reason-code",
                REJECT_CODE,
                "--override",
            ]
        )
    assert override_without_revision.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as revision_without_decision:
        main(["--inbox-dir", str(inbox), "--revision", "2"])
    assert revision_without_decision.value.code == 2
    capsys.readouterr()



def test_cli_unusable_paths_exit_3(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    fingerprint = fingerprint_of(inbox)
    code = main(["--inbox-dir", str(tmp_path / "nope"), "--json", "--as-of", MOMENT.isoformat()])
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err

    code = main(
        decide_argv(inbox, inbox / "review_ledger.json", fingerprint=fingerprint) + ["--json"]
    )
    assert code == EXIT_UNUSABLE  # 输出写在 inbox 内 → 拒绝（零写入）
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (inbox / "review_ledger.json").exists()
    assert not (inbox / "review_ledger.json.lock").exists()


def test_cli_corrupt_ledger_exits_4_and_preserves_state(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger = tmp_path / "review_ledger.json"
    out = tmp_path / "out_ledger.json"
    ledger.write_text("{ not json", encoding="utf-8")
    code = main(decide_argv(inbox, ledger, fingerprint=fingerprint_of(inbox)) + ["--json"])
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""  # fail-closed：stdout 为空
    assert "fail-closed" in captured.err
    assert ledger.read_text(encoding="utf-8") == "{ not json"  # 旧 ledger 原样保留
    assert not out.exists()


def test_cli_lock_conflict_exits_6_without_writes(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger = tmp_path / "review_ledger.json"
    lock_path = ledger.with_name(ledger.name + ".lock")
    fingerprint = fingerprint_of(inbox)
    with SingleInstanceLock(lock_path, owner="holder") as info:
        assert info.owner == "holder"
        code = main(decide_argv(inbox, ledger, fingerprint=fingerprint) + ["--json"])
        assert code == EXIT_LOCK_CONFLICT
        captured = capsys.readouterr()
        assert captured.out == "" and "fail-closed" in captured.err
        assert not ledger.exists()  # 零写入
        assert lock_path.exists()  # 活动锁不被删除 / 改写


def test_cli_markdown_summary_keeps_blocker_visible(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    ledger = tmp_path / "review_ledger.json"
    code = main(decide_argv(inbox, ledger, fingerprint=fingerprint_of(inbox)))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "PHASE3_3_DATA" in out
    assert "human_gate_required=true" in out
    assert "不自动 intake" in out
    assert "pkg-author-01" in out
    assert "APPROVED_FOR_EXPLICIT_INTAKE" in out
    assert "evidence_operator workflow --no-dry-run" in out



def test_cli_real_process_smoke(tmp_path: Path) -> None:
    """真实子进程：``python -m scripts.evidence_review`` 端到端（零网络 / 零数据库）。"""
    inbox = tmp_path / "inbox"
    ledger = tmp_path / "state" / "review_ledger.json"
    approved = tmp_path / "state" / "approved_for_intake.json"
    build_package(inbox)
    fingerprint = fingerprint_of(inbox)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_review",
            "--inbox-dir",
            str(inbox),
            "--ledger",
            str(ledger),
            "--out",
            str(ledger),
            "--approved-out",
            str(approved),
            "--decision",
            "approve",
            "--fingerprint",
            fingerprint,
            "--reviewer",
            "operator-li",
            "--reason-code",
            APPROVE_CODE,
            "--json",
            "--as-of",
            MOMENT.isoformat(),
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
    assert payload["action"] == "DECISION_RECORDED"
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["approved_list"]["approved_count"] == 1
    assert ledger.exists() and approved.exists()
    assert json.loads(ledger.read_text(encoding="utf-8"))["decision_count"] == 1

