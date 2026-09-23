"""GOLD-014 Intake Receipt 集成测试（真实 CLI + 真实审计产物 + 临时 SQLite / 零网络）。

端到端链路（全部临时目录、临时 SQLite、零网络、零真实授权数据）：

    inbox 摆放候选包 → GOLD-011 预检 → GOLD-012 人工复核/批准清单 → GOLD-013 只读 plan
    → **人工显式** Evidence Operator 真落库（``--no-dry-run --manifest``）
    → 真实 qualification recheck（``phase33_qualification_recheck``）
    → GOLD-014 收据核验（``scripts.evidence_intake_receipt``）

覆盖：真实执行结果与真实复核结果被绑定 → 退出 0 且只读；``--out`` 原子写 + 幂等；
plan/执行结果被改写 → 退出 4 且零写入；无批准 → 退出 5；参数 / 路径 / 锁 / 损坏 state；
Markdown 摘要保留 blocker；真实子进程 CLI 冒烟。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from scripts.evidence_intake_receipt import main
from scripts.evidence_operator import main as operator_main
from scripts.evidence_readiness import main as readiness_main
from src.common import hashing
from src.evidence import (
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    INBOX_SCHEMA_VERSION,
    INTAKE_RECEIPT_KIND,
    MANIFEST_FILE_NAME,
    OPERATOR_MANIFEST_REPORT,
    QUALIFICATION_RECHECK_REPORT,
    SingleInstanceLock,
    run_intake_plan,
    run_review,
    scan_inbox,
)
from src.evidence.human_verification_attestation import run_attestation
from src.evidence.intake_handoff import load_intake_handoff
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
INTAKE_AT = MOMENT + timedelta(hours=1)
RECHECK_AT = MOMENT + timedelta(hours=2)
RECEIPT_AT = MOMENT + timedelta(hours=3)
REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = "APPROVED_FOR_EXPLICIT_INTAKE"


def author_row(**overrides: Any) -> dict[str, Any]:
    """完全合规的 Author 证据行（含独立历史可用证据；与 GOLD-007 集成测试同口径）。"""
    row: dict[str, Any] = {
        "source": "manual-evidence-author",
        "source_record_id": "post-0001",
        "author_name": "作者甲",
        "external_account_id": "acct-001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350，本周维持逢高做空思路。",
        "published_at": "2026-09-18T08:30:00+08:00",
        "collected_at": "2026-09-18T09:10:00+08:00",
        "available_at": "2026-09-18T09:05:00+08:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://example.com/archive/2026-09-18",
        "provenance_reference": "https://example.com/posts/post-0001",
        "url": "https://example.com/posts/post-0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "authorization_reviewed_by": "合规复核人-张三",
        "authorization_reviewed_at": "2026-09-17T10:00:00+08:00",
        "authorization_valid_from": "2026-09-16T00:00:00+08:00",
        "authorization_expires_at": "2027-09-16T00:00:00+08:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def build_inbox_package(inbox: Path, name: str = "pkg-author-01") -> Path:
    """在 inbox 下建一个合规候选包（manifest 摘要取自**实际内容**）。"""
    package = inbox / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = write_jsonl(package / EVIDENCE_NAME, [author_row()])
    manifest = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "evidence_type": "author",
        "source": "manual-evidence-author",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": EVIDENCE_NAME,
                "sha256": hashing.sha256_bytes(evidence.read_bytes()),
                "format": "jsonl",
            }
        ],
    }
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def write_attestation(inbox: Path, *, moment: datetime = MOMENT, suffix: str = "") -> Path:
    """为 inbox 内唯一候选包生成合法 GOLD-028 材料级人工核验凭证（零网络）。"""
    handoff = load_intake_handoff(inbox, as_of=moment)
    package = handoff.packages[0]
    materials = [
        {
            "material": item.key,
            "decision": "VERIFIED",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": "operator-li",
            "reviewed_at": "2026-09-22T00:00:00+00:00",
            "evidence_reference": "https://vendor.example/terms",
        }
        for item in package.materials
        if item.category != "gate"
    ]
    document = {
        "schema_version": 1,
        "package_fingerprint": package.fingerprint,
        "scope": package.scope,
        "reviewer": "operator-li",
        "materials": materials,
    }
    root = Path(inbox).parent
    verification = root / f"verification{suffix}.json"
    verification.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    out = root / f"phase33_human_verification_attestation{suffix}.json"
    run_attestation(
        inbox,
        verification_path=verification,
        moment=moment,
        package=package.fingerprint,
        out_path=out,
    )
    return out


def prepare_ready_inputs(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> dict[str, Path]:
    """跑完整条真实链路：预检 → 复核 → plan → **显式** operator 落库 → 真实 recheck。

    准备阶段的 operator / recheck 也会打印报告：这里用 ``redirect_stdout`` 吞掉，
    保证被测 CLI 的 stdout 是**纯净** JSON。
    """
    inbox = tmp_path / "inbox"
    package = build_inbox_package(inbox)
    evidence = package / EVIDENCE_NAME
    report = scan_inbox(inbox, moment=MOMENT)
    target = next(item for item in report.packages if item.package_dir == package.name)
    ledger = tmp_path / "inbox_review_ledger.json"
    approved = tmp_path / "approved_for_intake.json"
    run_review(
        inbox,
        moment=MOMENT,
        decision="approve",
        fingerprint=target.fingerprint,
        reviewer="operator-li",
        reason_code=APPROVE_CODE,
        attestation_path=write_attestation(inbox, suffix=f"-{target.fingerprint[:8]}"),
        out_path=ledger,
        approved_out_path=approved,
    )
    plan = tmp_path / "evidence_intake_plan.json"
    run_intake_plan(
        inbox, moment=MOMENT, ledger_path=ledger, approved_list_path=approved, out_path=plan
    )
    manifest = tmp_path / "author_manifest.json"
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            operator_main(
                [
                    "intake",
                    "--scope",
                    "author",
                    "--input",
                    str(evidence),
                    "--json",
                    "--no-dry-run",
                    "--manifest",
                    str(manifest),
                    "--as-of",
                    INTAKE_AT.isoformat(),
                ],
                session_factory=session_factory,
            )
            == EXIT_OK
        )
    recheck = tmp_path / "phase33_recheck.json"
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            readiness_main(
                [
                    "recheck",
                    "--json",
                    "--no-dry-run",
                    "--report",
                    str(recheck),
                    "--as-of",
                    RECHECK_AT.isoformat(),
                ],
                session_factory=session_factory,
            )
            == EXIT_OK
        )
    return {
        "inbox": inbox,
        "package": package,
        "evidence": evidence,
        "ledger": ledger,
        "approved": approved,
        "plan": plan,
        "manifest": manifest,
        "recheck": recheck,
    }


def prepare_blocked_inputs(tmp_path: Path) -> dict[str, Path]:
    """没有任何批准的路径（预期 BLOCKED）：inbox 有候选但 ledger/清单为空。"""
    inbox = tmp_path / "inbox"
    build_inbox_package(inbox)
    ledger = tmp_path / "inbox_review_ledger.json"
    approved = tmp_path / "approved_for_intake.json"
    run_review(inbox, moment=MOMENT, out_path=ledger, approved_out_path=approved)
    plan = tmp_path / "evidence_intake_plan.json"
    run_intake_plan(
        inbox, moment=MOMENT, ledger_path=ledger, approved_list_path=approved, out_path=plan
    )
    return {"inbox": inbox, "ledger": ledger, "approved": approved, "plan": plan}


def receipt_argv(files: dict[str, Path], *extra: str) -> list[str]:
    """构造 GOLD-014 CLI 参数（默认带上显式执行结果与 recheck）。"""
    argv = [
        "--inbox-dir",
        str(files["inbox"]),
        "--ledger",
        str(files["ledger"]),
        "--approved-list",
        str(files["approved"]),
        "--plan",
        str(files["plan"]),
    ]
    if "manifest" in files:
        argv += ["--operator-result", str(files["manifest"])]
    if "recheck" in files:
        argv += ["--recheck", str(files["recheck"])]
    argv += list(extra)
    return argv


def json_stdout(capsys: Any) -> dict[str, Any]:
    """读取 stdout 的稳定 JSON（失败路径应为空）。"""
    return json.loads(capsys.readouterr().out)


def snapshot(root: Path) -> list[str]:
    """目录内全部文件的相对路径 + 字节（用于断言"零写入 / 零改动"）。"""
    items: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            items.append(f"{path.relative_to(root)}:{hashing.sha256_bytes(path.read_bytes())}")
    return items


# ---------------------------------------------------------------------------
# 真实链路：绑定成功（退出 0，只读）
# ---------------------------------------------------------------------------
def test_cli_receipt_is_read_only_and_verified(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    before = snapshot(tmp_path)
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))

    code = main(receipt_argv(files, "--json"), moment=RECEIPT_AT)

    assert code == EXIT_OK
    payload = json_stdout(capsys)
    assert payload["kind"] == INTAKE_RECEIPT_KIND
    assert payload["status"] == "VERIFIED_EXECUTION_RECORDED"
    assert len(payload["receipt_id"]) == 64
    assert payload["intake_executed"] is True
    assert payload["receipt_verified"] is True
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["requires_explicit_operator_action"] is True
    assert payload["executed_entry_count"] == 1
    assert payload["landed_rows"] == 1
    assert payload["written_path"] is None
    assert payload["verification"]["violations"] == []
    assert payload["plan"]["plan_id"] == json.loads(
        files["plan"].read_text(encoding="utf-8")
    )["plan_id"]
    # 绑定的执行结果就是真实 manifest（含 ``report`` 与内容摘要）
    result = payload["operator_results"][0]
    assert result["report"] == OPERATOR_MANIFEST_REPORT
    assert result["input_sha256"] == manifest["input"]["sha256"]
    assert result["artifact_sha256"] == hashing.sha256_bytes(files["manifest"].read_bytes())
    assert result["dry_run"] is False
    assert payload["qualification_recheck"]["report"] == QUALIFICATION_RECHECK_REPORT
    assert payload["qualification_recheck"]["is_qualification_decision"] is False
    assert snapshot(tmp_path) == before  # 只读：什么都没写


def test_cli_out_writes_receipt_atomically_and_idempotently(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    out = tmp_path / "evidence_intake_receipt.json"

    assert main(receipt_argv(files, "--json", "--out", str(out)), moment=RECEIPT_AT) == EXIT_OK
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["kind"] == INTAKE_RECEIPT_KIND
    assert document["receipt_id"] == json_stdout(capsys)["receipt_id"]
    assert document["written_path"] is None

    assert main(receipt_argv(files, "--json", "--out", str(out)), moment=RECEIPT_AT) == EXIT_OK
    assert out.read_text(encoding="utf-8") == text  # 幂等：逐字节稳定
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


def test_cli_blocked_receipt_exits_5(tmp_path: Path, capsys: Any) -> None:
    files = prepare_blocked_inputs(tmp_path)
    before = snapshot(tmp_path)

    code = main(receipt_argv(files, "--json"), moment=RECEIPT_AT)

    assert code == EXIT_BLOCKED
    payload = json_stdout(capsys)
    assert payload["status"] == "BLOCKED_NO_APPROVED_EVIDENCE"
    assert payload["intake_executed"] is False
    assert payload["receipt_verified"] is False
    assert payload["blocker_active"] is True and payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# 篡改 / 过期 / 参数 / 路径 / 锁
# ---------------------------------------------------------------------------
def _tamper_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def test_cli_dry_run_execution_result_exits_4_without_writing(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    _tamper_json(files["manifest"], lambda payload: payload.__setitem__("dry_run", True))
    before = snapshot(tmp_path)

    code = main(receipt_argv(files, "--json"), moment=RECEIPT_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""  # 失败路径 stdout 为空
    assert "OPERATOR_NOT_EXECUTED" in captured.err
    assert snapshot(tmp_path) == before


def test_cli_stale_plan_exits_4_without_writing(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    # 真实内容变化 → 新指纹 → 旧 plan 不再成立
    write_jsonl(files["evidence"], [author_row(source_record_id="post-0002")])
    before = snapshot(tmp_path)

    code = main(receipt_argv(files, "--json"), moment=RECEIPT_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PLAN_STALE" in captured.err or "PLAN_ENTRY_MISMATCH" in captured.err
    assert snapshot(tmp_path) == before


def test_cli_corrupt_artifacts_exit_4_and_preserve_files(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    files["recheck"].write_text("{broken", encoding="utf-8")

    code = main(receipt_argv(files, "--json"), moment=RECEIPT_AT)

    assert code == EXIT_STATE_INVALID
    assert capsys.readouterr().out == ""
    assert files["recheck"].read_text(encoding="utf-8") == "{broken"


def test_cli_argument_errors_exit_2(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    base = receipt_argv(files, "--json")
    without_plan = [
        part
        for index, part in enumerate(base)
        if part != "--plan" and not (index > 0 and base[index - 1] == "--plan")
    ]
    with pytest.raises(SystemExit) as missing_plan:
        main(without_plan, moment=RECEIPT_AT)
    assert missing_plan.value.code == EXIT_CONFIG_ERROR
    with pytest.raises(SystemExit) as naive_as_of:
        main(receipt_argv(files, "--json", "--as-of", "2026-09-23T03:00:00"), moment=RECEIPT_AT)
    assert naive_as_of.value.code == EXIT_CONFIG_ERROR


def test_cli_unusable_paths_exit_3(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)

    assert main(receipt_argv(files, "--json", "--out", str(files["inbox"] / "r.json")),
                moment=RECEIPT_AT) == EXIT_UNUSABLE
    assert capsys.readouterr().out == ""
    missing = tmp_path / "absent-inbox"
    argv = [part for part in receipt_argv(files, "--json")]
    argv[argv.index("--inbox-dir") + 1] = str(missing)
    assert main(argv, moment=RECEIPT_AT) == EXIT_UNUSABLE
    assert capsys.readouterr().out == ""


def test_cli_lock_conflict_exits_6_without_writes(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)
    out = tmp_path / "receipt.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")

    with held:
        code = main(receipt_argv(files, "--json", "--out", str(out)), moment=RECEIPT_AT)

    assert code == EXIT_LOCK_CONFLICT
    assert capsys.readouterr().out == ""
    assert not out.exists()


def test_cli_markdown_summary_keeps_blocker_visible(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_ready_inputs(tmp_path, session_factory)

    assert main(receipt_argv(files), moment=RECEIPT_AT) == EXIT_OK

    text = capsys.readouterr().out
    assert "blocker_active" in text
    assert "human_gate_required" in text
    assert "data_qualification_passed" in text
    assert "phase_transition_allowed" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "L3" in text


def test_cli_real_process_smoke(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """真实子进程冒烟：`python -m scripts.evidence_intake_receipt`（stdout 为纯 JSON）。"""
    files = prepare_ready_inputs(tmp_path, session_factory)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_intake_receipt",
            *receipt_argv(files, "--json", "--as-of", RECEIPT_AT.isoformat()),
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

    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["kind"] == INTAKE_RECEIPT_KIND
    assert payload["receipt_verified"] is True
    assert payload["intake_executed"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_active"] is True
