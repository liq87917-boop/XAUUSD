"""GOLD-033 提交就绪包 CLI 集成测试（临时 SQLite + 本地候选目录；零网络 / 零交易）。

覆盖：

- CLI **默认只读**：空库 → 诚实 BLOCKED（退出码 ``5``）、零数据库写入、零文件写入；
- ``--inbox-dir`` 只做 GOLD-027 只读预检（候选 **不** ingest、inbox 逐字节不变）；
- 五个结论的独立性：材料结构完整 / 人工核验完成 / 工程链全绿**都不**解除 ``PHASE3_3_DATA``；
- ``--rehearsal --work-dir`` 只验证工具链健康（fixture **不是**真实证据）；
- ``--chain-audit`` / ``--attestation`` 只读取既有 artifact 事实；
- 唯一写开关 ``--out``（原子写、无 ``.tmp`` 残留、写进 inbox 被拒）；
- CLI **没有**任何 qualify / advance / intake 参数（未知参数一律 ``SystemExit 2``）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import RawItem
from scripts.evidence_submission_readiness import (
    EXIT_CONFIG_ERROR,
    EXIT_UNUSABLE,
    build_parser,
    main,
)
from src.common import hashing
from src.evidence.chain_audit import (
    EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
    run_chain_audit,
)
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.human_verification_attestation import (
    handoff_content_sha256,
    run_attestation,
)
from src.evidence.inbox import MANIFEST_FILE_NAME
from src.evidence.intake_handoff import load_intake_handoff
from src.evidence.rehearsal import build_rehearsal_chain
from src.evidence.submission_readiness import EXIT_BLOCKED, SUBMISSION_PACK_KIND
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, tzinfo=UTC)
FORBIDDEN_OPTIONS = ("--qualify", "--approve", "--advance", "--no-dry-run", "--write-db")


def raw_item_count(session_factory: sessionmaker[Session]) -> int:
    """库内原始条目数（证明 CLI **没有** ingest）。"""
    with session_factory() as session:
        return int(
            session.execute(sa.select(sa.func.count()).select_from(RawItem)).scalar_one()
        )


def author_row(record_id: str = "a-0001") -> dict[str, Any]:
    """一条**完全合规**的本地 Author 证据行（零网络）。"""
    return {
        "source": "evidence-source-author",
        "source_record_id": record_id,
        "author_name": "作者甲",
        "external_account_id": "acct-0001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350。",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "url": "https://vendor.example/posts/a-0001",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }


def news_row(record_id: str = "n-0001") -> dict[str, Any]:
    """一条**完全合规**的本地 News 证据行（零网络）。"""
    return {
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


def build_package(
    root: Path, name: str, *, evidence_type: str, rows: list[dict[str, Any]]
) -> Path:
    """在 ``root`` 下建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / "evidence.jsonl"
    evidence.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": evidence_type,
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
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return package


def compliant_inbox(root: Path) -> Path:
    """Author + News 都**结构完整**的本地候选目录（零网络）。"""
    inbox = root / "inbox"
    build_package(inbox, "pkg-author-0001", evidence_type="author", rows=[author_row()])
    build_package(inbox, "pkg-news-0001", evidence_type="news", rows=[news_row()])
    return inbox


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内全部文件的相对路径 → 字节摘要（忽略 ``*.lock``）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def cli_json(
    session_factory: sessionmaker[Session], capsys: Any, *args: str
) -> tuple[int, dict[str, Any]]:
    """以 ``--json`` 运行 CLI 并返回（退出码, JSON 载荷）。"""
    code = main(["--json", *args], session_factory=session_factory, moment=MOMENT)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


# ---------------------------------------------------------------------------
# 参数契约：只读、无 qualify / advance / intake 开关
# ---------------------------------------------------------------------------
def test_help_exposes_only_readonly_options(capsys: Any) -> None:
    with pytest.raises(SystemExit) as failure:
        main(["--help"])
    help_text = capsys.readouterr().out

    assert failure.value.code == 0
    for option in ("--inbox-dir", "--attestation", "--chain-audit", "--rehearsal", "--out"):
        assert option in help_text
    for option in FORBIDDEN_OPTIONS:
        assert option not in help_text
    assert set(build_parser()._option_string_actions) == {
        "-h",
        "--help",
        "--inbox-dir",
        "--attestation",
        "--chain-audit",
        "--rehearsal",
        "--work-dir",
        "--scenario",
        "--as-of",
        "--json",
        "--out",
        "--lock",
    }


@pytest.mark.parametrize("option", FORBIDDEN_OPTIONS)
def test_forbidden_options_are_rejected(option: str) -> None:
    with pytest.raises(SystemExit) as failure:
        main([option])

    assert failure.value.code == 2


def test_rehearsal_requires_work_dir_and_rejects_real_inputs(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(SystemExit) as missing:
        main(["--rehearsal"], session_factory=session_factory, moment=MOMENT)
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as mixed:
        main(
            ["--rehearsal", "--inbox-dir", "somewhere"],
            session_factory=session_factory,
            moment=MOMENT,
        )
    assert mixed.value.code == 2

    with pytest.raises(SystemExit) as no_inbox_for_attestation:
        main(
            ["--attestation", "somewhere.json"],
            session_factory=session_factory,
            moment=MOMENT,
        )
    assert no_inbox_for_attestation.value.code == 2


# ---------------------------------------------------------------------------
# 只读模式：空库 / 本地候选目录 / 人工核验 / 链审计
# ---------------------------------------------------------------------------
def test_cli_empty_db_reports_blocker_and_writes_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    code, payload = cli_json(session_factory, capsys)

    assert code == EXIT_BLOCKED
    assert payload["kind"] == SUBMISSION_PACK_KIND
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["blocker_active"] is True
    assert payload["generates_evidence_times"] is False
    assert payload["mock_or_fixture_counts_as_real_material"] is False
    assert list(cwd.iterdir()) == []  # 默认零写入
    assert raw_item_count(session_factory) == 0


def test_cli_local_inbox_is_read_only_preflight(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    inbox = compliant_inbox(tmp_path)
    before = file_snapshot(inbox)

    code, payload = cli_json(session_factory, capsys, "--inbox-dir", str(inbox))

    assert code == EXIT_BLOCKED
    assert payload["submission_materials_complete"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["human_verification_complete"] is False
    assert "SUBMISSION_HUMAN_ATTESTATION_MISSING" in payload["missing_reason_codes"]
    assert "SUBMISSION_ENGINEERING_CHAIN_NOT_AUDITED" in payload["missing_reason_codes"]
    assert file_snapshot(inbox) == before  # 只读预检：inbox 逐字节不变
    assert raw_item_count(session_factory) == 0  # 不 ingest


def test_cli_missing_inbox_dir_fails_closed(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    code = main(
        ["--json", "--inbox-dir", str(tmp_path / "absent")],
        session_factory=session_factory,
        moment=MOMENT,
    )
    captured = capsys.readouterr()

    assert code == EXIT_CONFIG_ERROR
    assert captured.out == ""
    assert "InboxDirError" in captured.err


# ---------------------------------------------------------------------------
# rehearsal / 链审计：只写显式目录、**绝不**解除 blocker
# ---------------------------------------------------------------------------
def test_cli_rehearsal_reports_toolchain_health_only(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    work_dir = cwd / "rehearsal"

    code, payload = cli_json(
        session_factory, capsys, "--rehearsal", "--work-dir", str(work_dir)
    )

    assert code == EXIT_BLOCKED
    assert payload["evidence_source"] == EVIDENCE_SOURCE_REHEARSAL_FIXTURE
    assert payload["rehearsal"] is True
    assert payload["engineering_ready"] is True
    assert payload["submission_materials_complete"] is False
    assert payload["human_verification_complete"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["blocker_active"] is True
    assert "SUBMISSION_REHEARSAL_INPUT_NON_QUALIFYING" in payload["missing_reason_codes"]
    # 只写显式 work-dir：其它目录零副作用（fixture **不是**真实证据）
    assert {path.name for path in cwd.iterdir()} == {"rehearsal"}
    assert raw_item_count(session_factory) == 0


def test_cli_chain_audit_binding_marks_engineering_ready_only(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    chain = build_rehearsal_chain(tmp_path / "rehearsal", moment=MOMENT)
    report = tmp_path / "phase33_evidence_chain_audit.json"
    run_chain_audit(
        chain.to_chain_inputs(),
        moment=MOMENT,
        evidence_source=EVIDENCE_SOURCE_REHEARSAL_FIXTURE,
        scenario=chain.scenario,
        out_path=report,
    )

    code, payload = cli_json(session_factory, capsys, "--chain-audit", str(report))

    assert code == EXIT_BLOCKED
    assert payload["engineering_ready"] is True
    assert payload["chain"]["engineering_chain_ready"] is True
    assert payload["chain"]["chain_id"]
    assert payload["submission_materials_complete"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert "SUBMISSION_ENGINEERING_CHAIN_NOT_AUDITED" not in payload["missing_reason_codes"]
    assert raw_item_count(session_factory) == 0


def test_cli_attestation_binding_completes_human_verification_only(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, "pkg-author-0001", evidence_type="author", rows=[author_row()])
    document = load_intake_handoff(inbox, as_of=MOMENT)
    package = document.packages[0]
    verification = tmp_path / "verification.json"
    verification.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_fingerprint": package.fingerprint,
                "scope": package.scope,
                "reviewer": "operator-li",
                "handoff_content_sha256": handoff_content_sha256(document),
                "materials": [
                    {
                        "material": item.key,
                        "decision": "VERIFIED",
                        "reason_code": "HUMAN_REVIEWED",
                        "reviewer": "operator-li",
                        "reviewed_at": (MOMENT - timedelta(minutes=30)).isoformat(),
                        "evidence_reference": "https://vendor.example/terms",
                    }
                    for item in package.materials
                    if item.category != "gate"
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    attestation = tmp_path / "phase33_human_verification_attestation.json"
    run_attestation(
        inbox,
        verification_path=verification,
        moment=MOMENT,
        package=package.fingerprint,
        out_path=attestation,
    )

    code, payload = cli_json(
        session_factory,
        capsys,
        "--inbox-dir",
        str(inbox),
        "--attestation",
        str(attestation),
    )

    assert code == EXIT_BLOCKED
    assert payload["human_verification_complete"] is True
    assert payload["submission_materials_complete"] is False  # 只有 Author
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["l3_gate_pending"] is True
    assert payload["attestation"]["verified"] is True
    assert raw_item_count(session_factory) == 0


# ---------------------------------------------------------------------------
# 唯一写开关：--out
# ---------------------------------------------------------------------------
def test_cli_out_writes_only_the_pack(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    inbox = compliant_inbox(tmp_path)
    before = file_snapshot(inbox)
    out = tmp_path / "reports" / "phase33_human_evidence_submission_pack.json"

    code = main(
        ["--json", "--inbox-dir", str(inbox), "--out", str(out)],
        session_factory=session_factory,
        moment=MOMENT,
    )
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    written = json.loads(out.read_text(encoding="utf-8"))

    assert code == EXIT_BLOCKED
    assert printed["kind"] == SUBMISSION_PACK_KIND
    assert printed["written_path"] is not None
    assert written["kind"] == SUBMISSION_PACK_KIND
    assert written["data_qualification_passed"] is False
    assert written["phase_transition_allowed"] is False
    assert written["l3_gate_pending"] is True
    assert sorted(path.name for path in out.parent.iterdir()) == sorted(
        [out.name, out.name + ".lock"]
    )
    assert not list(tmp_path.rglob("*.tmp"))
    assert file_snapshot(inbox) == before


def test_cli_refuses_out_inside_inbox(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    inbox = compliant_inbox(tmp_path)
    before = file_snapshot(inbox)

    code = main(
        ["--json", "--inbox-dir", str(inbox), "--out", str(inbox / "pack.json")],
        session_factory=session_factory,
        moment=MOMENT,
    )
    captured = capsys.readouterr()

    assert code == EXIT_UNUSABLE
    assert captured.out == ""
    assert file_snapshot(inbox) == before
    assert not (inbox / "pack.json").exists()
