"""GOLD-027 Intake Handoff 预检 CLI 端到端测试（纯本地目录 / 零数据库 / 零网络）。

覆盖：

- ``--help`` 暴露的只有只读开关；任何 ``qualify`` / ``approve`` / ``advance`` /
  ``--no-dry-run`` / 写库参数一律不存在（未知参数退出码 2）；
- ``--schema`` 打印版本化 handoff 契约（不需要 ``--inbox-dir``，默认零写入）；
- 默认**零写入**：只有显式 ``--out`` 才原子落盘，且候选证据文件（内容 + mtime）零变化；
- 结构完整 → 退出码 0，但 ``evidence_qualified`` / ``data_qualification_passed`` /
  ``phase_transition_allowed`` / ``advance_allowed`` / ``l3_l4_auto_advance_allowed`` 恒为 false；
- Mock / 模板 / 空目录 → 退出码 5（预期 BLOCKED）；
- 目录缺失 / naive ``--as-of`` fail-closed（退出码 2、stdout 为空）；
- fresh subprocess 端到端：cwd 零文件写入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.evidence_intake_handoff import (
    EXIT_INCOMPLETE,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_OUTPUT_UNUSABLE,
    build_parser,
    main,
)
from src.common import hashing
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.inbox import MANIFEST_FILE_NAME
from src.evidence.intake_handoff import INTAKE_HANDOFF_KIND
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT_ISO = "2026-09-23T00:00:00+00:00"
FORBIDDEN_OPTIONS = ("--qualify", "--approve", "--advance", "--no-dry-run", "--write-db")


def news_row(record_id: str = "n-0001", *, synthetic: bool = False) -> dict[str, Any]:
    """一条**完全合规**的本地 News 证据行（可切换为示例 / Mock 行）。"""
    row: dict[str, Any] = {
        "source": "evidence-source-a",
        "source_record_id": record_id,
        "title": "金价短线回落",
        "content": "亚洲盘金价自 2400 回落至 2385。",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    if synthetic:
        row["record_kind"] = "example"
        row["is_mock"] = "true"
    return row


def build_package(
    root: Path, *, name: str = "pkg-news", synthetic: bool = False
) -> Path:
    """在 ``root`` 下建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / "evidence.jsonl"
    evidence.write_text(
        json.dumps(news_row(synthetic=synthetic), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": "news",
        "source": "evidence-source-a",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": evidence.name,
                "sha256": hashing.sha256_bytes(evidence.read_bytes()),
                "format": "jsonl",
            }
        ],
    }
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    """目录内每个文件的（内容, mtime_ns）快照（用于证明"历史证据未被改写"）。"""
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def cli_json(capsys: Any, *args: str) -> tuple[int, dict[str, Any]]:
    """以 ``--json`` 运行 CLI 并返回（退出码, JSON 载荷）。"""
    code = main(["--json", *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_help_exposes_only_readonly_options(capsys: Any) -> None:
    with pytest.raises(SystemExit) as failure:
        main(["--help"])
    help_text = capsys.readouterr().out

    assert failure.value.code == 0
    assert "--inbox-dir" in help_text
    assert "--schema" in help_text
    assert "--out" in help_text
    for option in FORBIDDEN_OPTIONS:
        assert option not in help_text
    assert set(build_parser()._option_string_actions) == {
        "-h",
        "--help",
        "--inbox-dir",
        "--schema",
        "--as-of",
        "--json",
        "--out",
    }


def test_forbidden_options_are_rejected(capsys: Any) -> None:
    for option in FORBIDDEN_OPTIONS:
        with pytest.raises(SystemExit) as failure:
            main([option])
        assert failure.value.code == 2
        capsys.readouterr()


def test_no_inbox_dir_requires_schema(capsys: Any) -> None:
    with pytest.raises(SystemExit) as failure:
        main(["--json"])
    captured = capsys.readouterr()

    assert failure.value.code == 2
    assert captured.out == ""


def test_missing_inbox_dir_fails_closed(capsys: Any, tmp_path: Path) -> None:
    code = main(["--json", "--as-of", MOMENT_ISO, "--inbox-dir", str(tmp_path / "absent")])
    captured = capsys.readouterr()

    assert code == EXIT_INPUT_ERROR
    assert captured.out == ""
    assert "InboxDirError" in captured.err


def test_naive_as_of_is_rejected(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)

    with pytest.raises(SystemExit) as failure:
        main(["--json", "--as-of", "2026-09-23T00:00:00", "--inbox-dir", str(inbox)])
    captured = capsys.readouterr()

    assert failure.value.code == 2
    assert captured.out == ""


def test_schema_is_printed_without_inbox_dir_or_writes(capsys: Any, tmp_path: Path) -> None:
    code = main(["--schema"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert payload["kind"] == INTAKE_HANDOFF_KIND
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert "结构完整" in payload["preflight_pass_semantics"]
    assert payload["materials"]
    assert list(tmp_path.iterdir()) == []


def test_schema_out_is_the_only_write_switch(capsys: Any, tmp_path: Path) -> None:
    target = tmp_path / "reports" / "handoff_schema.json"

    assert list(tmp_path.iterdir()) == []
    code = main(["--schema", "--out", str(target)])
    capsys.readouterr()

    assert code == EXIT_OK
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["kind"] == INTAKE_HANDOFF_KIND
    assert sorted(item.name for item in tmp_path.iterdir()) == ["reports"]


def test_structural_complete_package_keeps_everything_blocked(
    capsys: Any, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    build_package(inbox)

    code, payload = cli_json(capsys, "--as-of", MOMENT_ISO, "--inbox-dir", str(inbox))

    assert code == EXIT_OK
    assert payload["preflight_pass_count"] == 1
    assert payload["incomplete_count"] == 0
    assert payload["evidence_qualified"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["advance_allowed"] is False
    assert payload["l3_l4_auto_advance_allowed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["gate_blocked"] is True
    package = payload["packages"][0]
    assert package["preflight_pass"] is True
    assert package["evidence_qualified"] is False
    assert list(workdir.iterdir()) == []  # 默认零写入


def test_empty_and_mock_inbox_exit_blocked(capsys: Any, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    mock_inbox = tmp_path / "mock-inbox"
    build_package(mock_inbox, name="pkg-mock", synthetic=True)

    empty_code, empty_payload = cli_json(
        capsys, "--as-of", MOMENT_ISO, "--inbox-dir", str(empty)
    )
    mock_code, mock_payload = cli_json(
        capsys, "--as-of", MOMENT_ISO, "--inbox-dir", str(mock_inbox)
    )

    assert empty_code == EXIT_INCOMPLETE
    assert empty_payload["package_count"] == 0
    assert empty_payload["evidence_qualified"] is False

    assert mock_code == EXIT_INCOMPLETE
    assert mock_payload["synthetic_count"] == 1
    assert mock_payload["preflight_pass_count"] == 0
    assert mock_payload["evidence_qualified"] is False
    assert mock_payload["packages"][0]["non_qualifying_materials"]



def test_out_writes_only_the_artifact_and_keeps_evidence_untouched(
    capsys: Any, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    package = build_package(inbox)
    before = snapshot(package)
    listing = sorted(item.name for item in inbox.iterdir())
    target = tmp_path / "reports" / "handoff.json"

    code = main(
        ["--json", "--as-of", MOMENT_ISO, "--inbox-dir", str(inbox), "--out", str(target)]
    )
    capsys.readouterr()

    assert code == EXIT_OK
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["evidence_qualified"] is False
    assert written["packages"][0]["preflight_pass"] is True
    # 历史 evidence / result 绝不改写：内容与 mtime 一模一样，目录项不变
    assert snapshot(package) == before
    assert sorted(item.name for item in inbox.iterdir()) == listing


def test_unusable_out_path_returns_error(capsys: Any, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    code = main(["--schema", "--out", str(blocker / "handoff.json")])
    capsys.readouterr()

    assert code == EXIT_OUTPUT_UNUSABLE


def test_markdown_output_keeps_blocker(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)

    code = main(["--as-of", MOMENT_ISO, "--inbox-dir", str(inbox)])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Phase 3.3 人工证据 Intake Handoff 预检" in out
    assert "GATE_BLOCKED" in out
    assert "结构完整" in out
    assert "evidence_qualified=false" in out
    assert "l3_l4_auto_advance_allowed=false" in out
    assert "亚洲盘金价自 2400 回落至 2385" not in out


def test_fresh_subprocess_runs_readonly(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    build_package(inbox)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_intake_handoff",
            "--inbox-dir",
            str(inbox),
            "--as-of",
            MOMENT_ISO,
            "--json",
        ],
        cwd=str(workdir),
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == EXIT_OK, completed.stderr
    assert payload["evidence_qualified"] is False
    assert payload["blocker_active"] is True
    assert payload["packages"][0]["preflight_pass"] is True
    assert list(workdir.iterdir()) == []
    assert "Traceback" not in completed.stderr

