"""GOLD-017 Evidence Package Builder 与 GOLD-011 Inbox Scanner 端到端测试。

（临时文件 / 真实子进程 / 零网络 / 零数据库）

覆盖：

- **真链路**：CLI 预览（dry-run，零写入）→ CLI 写 manifest（原子写）→ **真实** GOLD-011 inbox
  scanner 只读预检 → ``PREFLIGHT_PASS`` 且逐文件 ``digest_verified``；
- **坏包 / 被篡改包仍 fail-closed**：证据被改写后 scanner 判 ``EVIDENCE_DIGEST_MISMATCH`` 并隔离，
  builder 再次写入 → ``MANIFEST_CONFLICT``（旧 manifest 原样保留）；
- **CLI 退出码** 0 / 2 / 3 / 4 / 5 / 6 与 stdout / stderr 纯净性、``--out`` 越界拒绝、锁冲突零写入；
- **真实子进程冒烟**：``python -m scripts.evidence_package``（零网络、零数据库）。
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

from scripts.evidence_inbox import main as inbox_main
from scripts.evidence_package import main as package_main
from src.common import hashing
from src.evidence import (
    EXIT_INPUT_INVALID,
    EXIT_LOCK_CONFLICT,
    EXIT_MANIFEST_CONFLICT,
    EXIT_NO_CANDIDATES,
    EXIT_OK,
    EXIT_UNUSABLE,
    MANIFEST_FILE_NAME,
    SingleInstanceLock,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
EVIDENCE_NAME = "author-2026-06.jsonl"


def author_row(record_id: str = "a-0001", **overrides: Any) -> dict[str, Any]:
    """一条完全合规的 Author 证据行（与单元测试同口径）。"""
    row: dict[str, Any] = {
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
    row.update(overrides)
    return row


def make_package(tmp_path: Path) -> Path:
    """建 ``inbox/pkg-author-01``（只有 evidence 文件；manifest 由 CLI 生成）。"""
    package = tmp_path / "inbox" / "pkg-author-01"
    package.mkdir(parents=True, exist_ok=True)
    (package / EVIDENCE_NAME).write_text(
        json.dumps(author_row(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return package


def cli_args(package: Path, *, work: Path, out: Path | None = None, **overrides: Any) -> list[str]:
    """构造 builder CLI 参数（元数据一律**显式**给出；锁默认放在 inbox 之外的 work 目录）。"""
    meta: dict[str, str] = {
        "evidence-type": "author",
        "source": "vendor-author",
        "authorization-reference": "https://vendor.example/terms",
        "time-semantics": "provider_export_iso8601_with_tz",
        "availability-semantics": "provider_archive_export_daily_snapshot",
        "historical-oos-applicable": "true",
    }
    for key in list(meta):
        if key in overrides:
            meta[key] = str(overrides.pop(key))
    args = ["--package-dir", str(package), "--file", EVIDENCE_NAME, "--json"]
    for key, value in meta.items():
        args += [f"--{key}", value]
    args += ["--lock", str(work / "package.lock")]
    if out is not None:
        args += ["--out", str(out)]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    return args


def json_stdout(capsys: Any) -> dict[str, Any]:
    """读取 stdout 的纯 JSON（stdout 必须无杂音）。"""
    return json.loads(capsys.readouterr().out)


def test_cli_build_then_real_inbox_scanner_accepts_package(
    tmp_path: Path, capsys: Any
) -> None:
    """端到端：dry-run → 写 manifest → **真实** GOLD-011 scanner 预检通过。"""
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    manifest_file = package / MANIFEST_FILE_NAME
    before = (package / EVIDENCE_NAME).read_bytes()
    # ① dry-run：零写入
    assert package_main(cli_args(package, work=work), moment=MOMENT) == EXIT_OK
    preview = json_stdout(capsys)
    assert preview["write_status"] == "DRY_RUN_NOT_WRITTEN"
    assert preview["blocker_active"] is True
    assert preview["data_qualification_passed"] is False
    assert not manifest_file.exists()
    # ② 唯一写开关：显式 --out 指向固定 manifest.json
    assert (
        package_main(cli_args(package, work=work, out=manifest_file), moment=MOMENT) == EXIT_OK
    )
    written = json_stdout(capsys)
    assert written["write_status"] == "WRITTEN"
    assert manifest_file.exists()
    assert (package / EVIDENCE_NAME).read_bytes() == before  # 原始 evidence 零改写
    assert json.loads(manifest_file.read_text(encoding="utf-8")) == written["manifest"]
    # ③ 真实 GOLD-011 inbox scanner：只读预检 → PREFLIGHT_PASS
    inbox = package.parent
    assert inbox_main(["--inbox-dir", str(inbox), "--json"], moment=MOMENT) == EXIT_OK
    report = json_stdout(capsys)
    assert report["counts"] == {
        "packages": 1,
        "discovered": 1,
        "already_pending": 0,
        "status_changed": 0,
        "preflight_pass": 1,
        "quarantined": 0,
        "skipped": 0,
        "pending_entries": 1,
    }
    scanned = report["preflight_pass"][0]
    assert scanned["status"] == "PREFLIGHT_PASS"
    assert scanned["files"][0]["digest_verified"] is True
    assert scanned["files"][0]["sha256"] == hashing.sha256_bytes(before)
    assert scanned["files"][0]["declared_sha256"] == scanned["files"][0]["sha256"]
    assert scanned["acceptable_rows"] == 1 and scanned["quarantined_rows"] == 0
    assert report["blocker_active"] is True and report["data_qualification_passed"] is False


def test_scanner_quarantines_tampered_package_and_builder_refuses_rewrite(
    tmp_path: Path, capsys: Any
) -> None:
    """被篡改的包仍由 scanner fail-closed；builder **绝不**静默覆盖既有 manifest。"""
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    manifest_file = package / MANIFEST_FILE_NAME
    assert (
        package_main(cli_args(package, work=work, out=manifest_file), moment=MOMENT) == EXIT_OK
    )
    original_manifest = manifest_file.read_bytes()
    capsys.readouterr()
    # 篡改证据（追加一行 → 内容摘要变化）
    evidence = package / EVIDENCE_NAME
    evidence.write_text(
        evidence.read_text(encoding="utf-8")
        + json.dumps(author_row("a-9999"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    inbox = package.parent
    assert inbox_main(["--inbox-dir", str(inbox), "--json"], moment=MOMENT) == EXIT_NO_CANDIDATES
    report = json_stdout(capsys)
    assert report["counts"]["preflight_pass"] == 0 and report["counts"]["quarantined"] == 1
    quarantined = report["quarantined"][0]
    assert "EVIDENCE_DIGEST_MISMATCH" in quarantined["reason_codes"]
    # builder 再次写入 → MANIFEST_CONFLICT（退出码 5、零写入）
    assert (
        package_main(cli_args(package, work=work, out=manifest_file), moment=MOMENT)
        == EXIT_MANIFEST_CONFLICT
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "MANIFEST_CONFLICT" in captured.err
    assert manifest_file.read_bytes() == original_manifest  # 旧 manifest 原样保留


def test_scanner_rejects_bad_package_without_manifest(tmp_path: Path, capsys: Any) -> None:
    """没有 manifest 的“裸”包：scanner 仍 fail-closed（证明 builder 产物不是绕过手段）。"""
    package = make_package(tmp_path)
    assert inbox_main(["--inbox-dir", str(package.parent), "--json"], moment=MOMENT) == (
        EXIT_NO_CANDIDATES
    )
    report = json_stdout(capsys)
    assert report["counts"]["preflight_pass"] == 0
    assert "MANIFEST_MISSING" in report["quarantined"][0]["reason_codes"]


def test_cli_argument_errors_exit_2(tmp_path: Path, capsys: Any) -> None:
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(SystemExit) as missing:
        package_main(["--package-dir", str(package), "--json"])
    assert missing.value.code == 2  # 缺必填元数据 / --file
    capsys.readouterr()
    with pytest.raises(SystemExit) as naive:
        package_main([*cli_args(package, work=work), "--as-of", "2026-09-23T00:00:00"])
    assert naive.value.code == 2  # 禁止隐式时区
    capsys.readouterr()
    with pytest.raises(SystemExit) as bad_oos:
        package_main([*cli_args(package, work=work), "--historical-oos-applicable", "maybe"])
    assert bad_oos.value.code == 2
    capsys.readouterr()
    assert not (package / MANIFEST_FILE_NAME).exists()


def test_cli_unusable_paths_exit_3(tmp_path: Path, capsys: Any) -> None:
    work = tmp_path / "work"
    work.mkdir()
    assert package_main(cli_args(tmp_path / "nope", work=work), moment=MOMENT) == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err
    package = make_package(tmp_path)
    elsewhere = tmp_path / "elsewhere.json"
    code = package_main(cli_args(package, work=work, out=elsewhere), moment=MOMENT)
    assert code == EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == "" and "MANIFEST_OUT_NOT_TARGET" in captured.err
    assert not elsewhere.exists()
    assert not (package / MANIFEST_FILE_NAME).exists()


def test_cli_input_invalid_exit_4(tmp_path: Path, capsys: Any) -> None:
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    args = cli_args(package, work=work)
    args[args.index("--file") + 1] = "missing.jsonl"
    assert package_main(args, moment=MOMENT) == EXIT_INPUT_INVALID
    captured = capsys.readouterr()
    assert captured.out == "" and "EVIDENCE_FILE_MISSING" in captured.err
    assert not (package / MANIFEST_FILE_NAME).exists()


def test_cli_lock_conflict_exit_6_without_writing(tmp_path: Path, capsys: Any) -> None:
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    with SingleInstanceLock(work / "package.lock", owner="holder"):
        code = package_main(
            cli_args(package, work=work, out=package / MANIFEST_FILE_NAME), moment=MOMENT
        )
        assert code == EXIT_LOCK_CONFLICT
        assert not (package / MANIFEST_FILE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == "" and "fail-closed" in captured.err


def test_cli_real_process_smoke(tmp_path: Path) -> None:
    """真实子进程：``python -m scripts.evidence_package`` → manifest → 真实 scanner。"""
    package = make_package(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    manifest_file = package / MANIFEST_FILE_NAME
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    builder_args = [
        *cli_args(package, work=work, out=manifest_file),
        "--as-of",
        MOMENT.isoformat(),
    ]

    def run(module: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )

    completed = run("scripts.evidence_package", builder_args)
    assert completed.returncode == EXIT_OK, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["write_status"] == "WRITTEN"
    assert payload["blocker_active"] is True
    assert payload["data_qualification_passed"] is False
    assert manifest_file.exists()

    repeated = run("scripts.evidence_package", builder_args)  # 幂等：同内容不重写
    assert repeated.returncode == EXIT_OK, repeated.stderr
    assert json.loads(repeated.stdout)["write_status"] == "IDEMPOTENT_UNCHANGED"

    scanned = run(
        "scripts.evidence_inbox",
        ["--inbox-dir", str(package.parent), "--json", "--as-of", MOMENT.isoformat()],
    )
    assert scanned.returncode == EXIT_OK, scanned.stderr
    report = json.loads(scanned.stdout)
    assert report["counts"]["preflight_pass"] == 1
    assert report["counts"]["quarantined"] == 0
    assert report["preflight_pass"][0]["files"][0]["digest_verified"] is True
