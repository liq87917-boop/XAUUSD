"""GOLD-011 Evidence Inbox CLI 端到端测试（临时文件 / 真实子进程 / 零网络 / 零数据库）。

覆盖：

- 空 inbox：退出码 ``5``（预期 BLOCKED），stdout 为纯 JSON，inbox 内零写入；
- 合法候选：退出码 ``0``、pending 清单原子落盘、重复扫描幂等（discovered=0）；
- 隔离候选：退出码 ``5`` 且给出稳定原因码；
- 损坏 state：退出码 ``4``、旧文件原样保留、零写入；
- 锁冲突：退出码 ``6``、零写入、活动锁不被删除；
- 参数 / 目录错误：退出码 ``2`` / ``3``（含把 ``--out`` 写进 inbox 的拒绝）；
- 真实子进程冒烟：``python -m scripts.evidence_inbox`` 端到端可跑（无网络、无数据库）。
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

from scripts.evidence_inbox import main
from src.common import hashing
from src.evidence import (
    EXIT_LOCK_CONFLICT,
    EXIT_NO_CANDIDATES,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    InboxReasonCode,
    SingleInstanceLock,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)


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


def json_stdout(capsys: Any) -> dict[str, Any]:
    """读取 stdout 的纯 JSON（stdout 必须无杂音）。"""
    return json.loads(capsys.readouterr().out)


def test_cli_scan_empty_inbox_exits_5_without_writing(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    code = main(["--inbox-dir", str(inbox), "--json", "--as-of", MOMENT.isoformat()])
    assert code == EXIT_NO_CANDIDATES
    payload = json_stdout(capsys)
    assert payload["counts"]["packages"] == 0
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert list(inbox.iterdir()) == []  # 扫描绝不写入 inbox


def test_cli_scan_valid_package_is_idempotent_and_atomic(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "state" / "inbox_pending.json"
    package_dir = build_package(inbox)
    evidence = package_dir / "author.jsonl"
    evidence_bytes = evidence.read_bytes()
    before = sorted(item.name for item in package_dir.iterdir())
    argv = [
        "--inbox-dir",
        str(inbox),
        "--state",
        str(out),
        "--out",
        str(out),
        "--json",
        "--as-of",
        MOMENT.isoformat(),
    ]
    code = main(argv)
    assert code == EXIT_OK
    payload = json_stdout(capsys)
    assert payload["counts"]["preflight_pass"] == 1
    assert payload["counts"]["discovered"] == 1
    assert payload["preflight_pass"][0]["requires_human_action"] is True
    assert payload["data_qualification_passed"] is False
    assert out.exists()
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["entry_count"] == 1
    assert document["entries"][0]["status"] == "PREFLIGHT_PASS"
    assert [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")] == []
    first_seen = document["entries"][0]["first_seen_at"]

    # 重复扫描（同内容）：幂等，不重复生成待处理项
    code = main(argv)
    assert code == EXIT_OK
    second = json_stdout(capsys)
    assert second["counts"]["discovered"] == 0
    assert second["counts"]["already_pending"] == 1
    assert second["counts"]["pending_entries"] == 1
    updated = json.loads(out.read_text(encoding="utf-8"))
    assert updated["entry_count"] == 1
    assert updated["entries"][0]["first_seen_at"] == first_seen
    assert updated["entries"][0]["scan_count"] == 2
    assert not (inbox / "inbox_pending.json").exists()  # 写入永不落在 inbox 内
    # 原始 evidence 从未被移动 / 重命名 / 删除 / 改写（字符级一致），包内也没有新增文件
    assert evidence.read_bytes() == evidence_bytes
    assert sorted(item.name for item in package_dir.iterdir()) == before


def test_cli_quarantined_candidate_exits_5_with_reason_codes(
    tmp_path: Path, capsys: Any
) -> None:
    inbox = tmp_path / "inbox"
    broken = inbox / "evidence-no-manifest"
    broken.mkdir(parents=True)
    (broken / "author.jsonl").write_text("{}\n", encoding="utf-8")
    code = main(["--inbox-dir", str(inbox), "--json", "--as-of", MOMENT.isoformat()])
    assert code == EXIT_NO_CANDIDATES
    payload = json_stdout(capsys)
    assert payload["counts"]["quarantined"] == 1
    assert payload["quarantined"][0]["reason_codes"] == [InboxReasonCode.MANIFEST_MISSING.value]
    assert payload["preflight_pass"] == []


def test_cli_markdown_summary_keeps_blocker_visible(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    build_package(inbox, name="evidence-broken")
    (inbox / "evidence-broken" / MANIFEST_FILE_NAME).unlink()
    code = main(["--inbox-dir", str(inbox), "--as-of", MOMENT.isoformat()])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "PHASE3_3_DATA" in out
    assert "human_gate_required=true" in out
    assert "不自动 intake" in out
    assert "pkg-author-01" in out and "evidence-broken" in out
    assert InboxReasonCode.MANIFEST_MISSING.value in out



def test_cli_corrupt_state_exits_4_and_preserves_state(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    state = tmp_path / "inbox_pending.json"
    out = tmp_path / "out_pending.json"
    build_package(inbox)
    state.write_text("{ not json", encoding="utf-8")
    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--state",
            str(state),
            "--out",
            str(out),
            "--json",
            "--as-of",
            MOMENT.isoformat(),
        ]
    )
    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""  # fail-closed：stdout 为空
    assert "fail-closed" in captured.err
    assert state.read_text(encoding="utf-8") == "{ not json"  # 旧 state 原样保留
    assert not out.exists()  # 零写入


def test_cli_lock_conflict_exits_6_without_writes(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    build_package(inbox)
    lock_path = out.with_name(out.name + ".lock")
    with SingleInstanceLock(lock_path, owner="holder") as info:
        assert info.owner == "holder"
        code = main(
            [
                "--inbox-dir",
                str(inbox),
                "--out",
                str(out),
                "--json",
                "--as-of",
                MOMENT.isoformat(),
            ]
        )
    assert code == EXIT_LOCK_CONFLICT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fail-closed" in captured.err
    assert not out.exists()  # 零写入
    assert lock_path.exists()  # 活动锁不被删除 / 改写


def test_cli_output_inside_inbox_exits_3(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    build_package(inbox)
    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--out",
            str(inbox / "pending.json"),
            "--json",
            "--as-of",
            MOMENT.isoformat(),
        ]
    )
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (inbox / "pending.json").exists()
    assert not (inbox / "pending.json.lock").exists()
    assert list(inbox.glob("*.json")) == []  # inbox 内不新增任何 artifact


def test_cli_missing_inbox_dir_exits_3(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "--inbox-dir",
            str(tmp_path / "nope"),
            "--json",
            "--as-of",
            MOMENT.isoformat(),
        ]
    )
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fail-closed" in captured.err


def test_cli_argument_errors(tmp_path: Path, capsys: Any) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    with pytest.raises(SystemExit) as missing_inbox_dir:
        main(["--json"])
    assert missing_inbox_dir.value.code == 2  # --inbox-dir 必填：只扫描显式目录
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive_as_of:
        main(["--inbox-dir", str(inbox), "--as-of", "2026-09-23T00:00:00"])
    assert naive_as_of.value.code == 2  # 禁止隐式时区
    capsys.readouterr()


def test_cli_real_process_smoke(tmp_path: Path) -> None:
    """真实子进程：``python -m scripts.evidence_inbox`` 端到端（零网络 / 零数据库）。"""
    inbox = tmp_path / "inbox"
    out = tmp_path / "state" / "inbox_pending.json"
    build_package(inbox)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_inbox",
            "--inbox-dir",
            str(inbox),
            "--state",
            str(out),
            "--out",
            str(out),
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
    assert payload["counts"]["preflight_pass"] == 1
    assert payload["blocker_active"] is True
    assert payload["data_qualification_passed"] is False
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["entry_count"] == 1
    assert [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")] == []

