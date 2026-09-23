"""GOLD-028 材料级人工核验凭证 CLI 端到端测试（纯本地目录 / 零数据库 / 零网络）。

覆盖：

- ``--help`` 暴露的只有只读开关；任何 ``qualify`` / ``approve`` / ``advance`` /
  ``--no-dry-run`` / 写库参数一律不存在（未知参数退出码 2）；
- ``--schema`` 打印版本化凭证契约（不需要 ``--inbox-dir``，默认零写入）；
- 默认**零写入**：只有显式 ``--out`` 才原子落盘，且候选证据文件（内容 + mtime）零变化；
- ``all_required_verified=true`` → 退出码 0，但 ``evidence_qualified`` /
  ``data_qualification_passed`` / ``phase_transition_allowed`` /
  ``l3_l4_auto_advance_allowed`` / ``advance_allowed`` 恒为 false、
  ``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` 恒为 true；
- 未全部核验 / Mock / 空候选 → 退出码 5（预期 BLOCKED）；
- 漂移 / 篡改 / naive 时点 fail-closed（退出码 4 / 2、失败路径 stdout 为空）；
- fresh subprocess 端到端：cwd 零文件写入。
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

from scripts.evidence_human_verification_attestation import (
    EXIT_BLOCKED,
    EXIT_INVALID,
    EXIT_OK,
    EXIT_UNUSABLE,
    build_parser,
    main,
)
from src.common import hashing
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.human_verification_attestation import ATTESTATION_KIND
from src.evidence.inbox import MANIFEST_FILE_NAME
from src.evidence.intake_handoff import load_intake_handoff
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
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


def build_package(root: Path, *, name: str = "pkg-news", synthetic: bool = False) -> Path:
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


def verification_file(root: Path, *, all_verified: bool = True) -> Path:
    """按当前候选包写一份人工核验输入（默认全部材料 ``VERIFIED``）。"""
    handoff = load_intake_handoff(root, as_of=MOMENT)
    package = handoff.packages[0]
    materials = [
        {
            "material": item.key,
            "decision": "VERIFIED" if all_verified else "NEEDS_CHANGES",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": "operator-li",
            "reviewed_at": "2026-09-22T00:00:00+00:00",
            "evidence_reference": "https://vendor.example/terms",
        }
        for item in package.materials
        if item.category != "gate"
    ]
    doc = {
        "schema_version": 1,
        "package_fingerprint": package.fingerprint,
        "scope": package.scope,
        "reviewer": "operator-li",
        "materials": materials,
    }
    target = root.parent / "verification.json"
    target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """候选包内所有文件的（内容 + mtime）快照：证明**零证据改写**。"""
    return {
        item.name: (hashing.sha256_bytes(item.read_bytes()), item.stat().st_mtime_ns)
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def cli_json(capsys: Any, *args: str) -> tuple[int, dict[str, Any]]:
    """运行 CLI 并解析 stdout JSON（返回 ``(exit_code, payload)``）。"""
    code = main([*args, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_help_exposes_only_readonly_options() -> None:
    help_text = build_parser().format_help()

    assert "--inbox-dir" in help_text
    assert "--verification" in help_text
    assert "--verify-attestation" in help_text
    assert "--schema" in help_text
    for option in FORBIDDEN_OPTIONS:
        assert option not in help_text


def test_unknown_options_are_rejected(capsys: Any, tmp_path: Path) -> None:
    for option in FORBIDDEN_OPTIONS:
        with pytest.raises(SystemExit) as excinfo:
            main(["--inbox-dir", str(tmp_path), option])
        assert excinfo.value.code == 2
    capsys.readouterr()


def test_schema_output_is_versioned_and_writes_nothing(capsys: Any, tmp_path: Path) -> None:
    code, payload = cli_json(capsys, "--schema")

    assert code == EXIT_OK
    assert payload["kind"] == ATTESTATION_KIND
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert "evidence_qualified" in payload["all_required_verified_does_not_imply"]
    assert list(tmp_path.iterdir()) == []


def test_schema_without_inbox_is_allowed_and_out_writes_only_it(
    capsys: Any, tmp_path: Path
) -> None:
    target = tmp_path / "reports" / "schema.json"

    code = main(["--schema", "--out", str(target)])
    capsys.readouterr()

    assert code == EXIT_OK
    assert json.loads(target.read_text(encoding="utf-8"))["kind"] == ATTESTATION_KIND


def test_cli_requires_inbox_and_verification(capsys: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--json"])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_naive_as_of_is_rejected_with_empty_stdout(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--inbox-dir",
                str(inbox),
                "--verification",
                str(verification),
                "--as-of",
                "2026-09-23T00:00:00",
                "--json",
            ]
        )
    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""



def test_all_verified_returns_ok_but_never_qualifies(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package = build_package(inbox)
    before = snapshot(package)
    listing = sorted(item.name for item in inbox.iterdir())
    verification = verification_file(inbox)

    code, payload = cli_json(
        capsys,
        "--inbox-dir",
        str(inbox),
        "--verification",
        str(verification),
        "--as-of",
        MOMENT_ISO,
    )

    assert code == EXIT_OK
    assert payload["all_required_verified"] is True
    assert payload["evidence_qualified"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_l4_auto_advance_allowed"] is False
    assert payload["advance_allowed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["gate_blocked"] is True
    assert payload["package_fingerprint"]
    assert payload["handoff_content_sha256"]
    # 默认**零写入**：候选证据内容与 mtime 零变化，目录项不增不减
    assert snapshot(package) == before
    assert sorted(item.name for item in inbox.iterdir()) == listing


def test_out_writes_only_the_attestation(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package = build_package(inbox)
    before = snapshot(package)
    verification = verification_file(inbox)
    target = tmp_path / "reports" / "attestation.json"

    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
            "--out",
            str(target),
        ]
    )
    capsys.readouterr()

    assert code == EXIT_OK
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["kind"] == ATTESTATION_KIND
    assert written["all_required_verified"] is True
    assert written["data_qualification_passed"] is False
    assert snapshot(package) == before
    assert sorted(item.name for item in inbox.iterdir()) == ["pkg-news"]


def test_unusable_out_path_returns_unusable(capsys: Any, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    code = main(["--schema", "--out", str(blocker / "attestation.json")])
    capsys.readouterr()

    assert code == EXIT_UNUSABLE


def test_incomplete_attestation_returns_blocked(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox, all_verified=False)

    code, payload = cli_json(
        capsys,
        "--inbox-dir",
        str(inbox),
        "--verification",
        str(verification),
        "--as-of",
        MOMENT_ISO,
    )

    assert code == EXIT_BLOCKED
    assert payload["all_required_verified"] is False
    assert payload["evidence_qualified"] is False
    assert "MATERIAL_NEEDS_CHANGES" in payload["codes"]


def test_mock_package_returns_blocked_and_never_qualifies(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, synthetic=True)
    handoff = load_intake_handoff(inbox, as_of=MOMENT)
    doc = {
        "schema_version": 1,
        "package_fingerprint": handoff.packages[0].fingerprint,
        "materials": [],
    }
    verification = tmp_path / "mock-verification.json"
    verification.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    code, payload = cli_json(
        capsys,
        "--inbox-dir",
        str(inbox),
        "--verification",
        str(verification),
        "--as-of",
        MOMENT_ISO,
    )

    assert code == EXIT_BLOCKED
    assert payload["synthetic"] is True
    assert payload["all_required_verified"] is False
    assert "NON_QUALIFYING_PACKAGE" in payload["codes"]
    assert payload["verified_material_count"] == 0


def test_mock_package_with_verified_claim_fails_closed(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, synthetic=True)
    verification = verification_file(inbox)

    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_BLOCKED
    assert captured.out == ""
    assert "NON_QUALIFYING_PACKAGE" in captured.err


def test_empty_inbox_reports_no_candidate_package(capsys: Any, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    verification = tmp_path / "verification.json"
    verification.write_text(
        json.dumps({"schema_version": 1, "package_fingerprint": "a" * 64, "materials": []}),
        encoding="utf-8",
    )

    code = main(
        [
            "--inbox-dir",
            str(empty),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_BLOCKED
    assert captured.out == ""
    assert "PACKAGE_NOT_FOUND" in captured.err



def test_verify_attestation_passes_when_binding_matches(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox)
    target = tmp_path / "reports" / "attestation.json"
    main(
        [
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
            "--out",
            str(target),
        ]
    )
    capsys.readouterr()

    code, payload = cli_json(
        capsys,
        "--inbox-dir",
        str(inbox),
        "--as-of",
        MOMENT_ISO,
        "--verify-attestation",
        str(target),
    )

    assert code == EXIT_OK
    assert payload["verified"] is True
    assert payload["codes"] == []
    assert payload["attestation_id_matches"] is True
    assert payload["package_fingerprint_matches"] is True
    assert payload["handoff_content_matches"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False


def test_verify_attestation_detects_drift(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox)
    target = tmp_path / "reports" / "attestation.json"
    main(
        [
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
            "--out",
            str(target),
        ]
    )
    capsys.readouterr()

    # 候选证据被改写 → package / handoff 内容身份漂移：旧凭证必须失效
    (inbox / "pkg-news" / "evidence.jsonl").write_text(
        json.dumps(news_row("n-0009"), ensure_ascii=False) + "\n", encoding="utf-8"
    )

    code, payload = cli_json(
        capsys,
        "--inbox-dir",
        str(inbox),
        "--as-of",
        MOMENT_ISO,
        "--verify-attestation",
        str(target),
    )

    assert code == EXIT_INVALID
    assert payload["verified"] is False
    assert "PACKAGE_FINGERPRINT_DRIFT" in payload["codes"]
    assert "HANDOFF_CONTENT_DRIFT" in payload["codes"]


def test_verify_attestation_detects_tampering(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox)
    target = tmp_path / "reports" / "attestation.json"
    main(
        [
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
            "--as-of",
            MOMENT_ISO,
            "--json",
            "--out",
            str(target),
        ]
    )
    capsys.readouterr()
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["data_qualification_passed"] = True
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    code = main(
        [
            "--inbox-dir",
            str(inbox),
            "--as-of",
            MOMENT_ISO,
            "--json",
            "--verify-attestation",
            str(target),
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_INVALID
    assert captured.out == ""
    assert "ATTESTATION_TAMPERED" in captured.err


def test_verify_mode_rejects_other_write_flags(capsys: Any, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox)
    verification = verification_file(inbox)
    target = tmp_path / "reports" / "attestation.json"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--inbox-dir",
                str(inbox),
                "--as-of",
                MOMENT_ISO,
                "--verify-attestation",
                str(target),
                "--verification",
                str(verification),
            ]
        )
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_fresh_subprocess_runs_readonly(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    build_package(inbox)
    verification = verification_file(inbox)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_human_verification_attestation",
            "--inbox-dir",
            str(inbox),
            "--verification",
            str(verification),
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
    assert payload["all_required_verified"] is True
    assert payload["evidence_qualified"] is False
    assert payload["blocker_active"] is True
    assert list(workdir.iterdir()) == []
    assert "Traceback" not in completed.stderr

