"""GOLD-015 L3 人工决策包集成测试（真实 CLI + 真实审计产物 + 临时 SQLite / 零网络）。

端到端链路（全部临时目录、临时 SQLite、零网络、零真实授权数据）：

    inbox 摆放候选包 → GOLD-011 预检 → GOLD-012 人工复核 / 批准清单 → GOLD-013 只读 plan
    → **人工显式** Evidence Operator 真落库（``--no-dry-run --manifest``）
    → 真实 qualification recheck（``phase33_qualification_recheck``）
    → **真实** GOLD-008 handoff 报告（``scripts.evidence_handoff``）
    → GOLD-015 决策包（``scripts.evidence_decision_packet``）

覆盖：真实链路（证据未达标）→ 决策包诚实 BLOCKED（退出 5）且收据可核验；
库内达标（Mock 证据块）→ 可提交 L3 人工 Gate（退出 0，但 data_qualification_passed 恒 false）；
``--out`` 原子写 + 幂等；handoff / readiness state 被改写 → 退出 4 且零写入；
参数 / 路径 / 锁 / 损坏 state；Markdown 摘要保留 blocker；决策包**绝不**写数据库 / 原始 evidence；
真实子进程 CLI 冒烟。
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
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import RawItem, Source
from database.models.enums import RawItemType, SourceType
from scripts.evidence_decision_packet import main
from scripts.evidence_handoff import main as handoff_main
from scripts.evidence_operator import main as operator_main
from scripts.evidence_readiness import main as readiness_main
from src.common import hashing
from src.common.hashing import content_hash
from src.evidence import (
    DECISION_PACKET_KIND,
    EVIDENCE_CONTRACT_VERSION,
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    HANDOFF_REPORT_NAME,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    QUALIFICATION_RECHECK_REPORT,
    DecisionPacketStatus,
    PacketVerificationCode,
    SingleInstanceLock,
    load_handoff_document,
    run_intake_plan,
    run_intake_receipt,
    run_review,
    scan_inbox,
    write_snapshot_state,
)
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
INTAKE_AT = MOMENT + timedelta(hours=1)
RECHECK_AT = MOMENT + timedelta(hours=2)
HANDOFF_AT = MOMENT + timedelta(hours=3)
PACKET_AT = MOMENT + timedelta(hours=4)
REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = "APPROVED_FOR_EXPLICIT_INTAKE"
SECRET = "sk-livesecret0123456789"
#: 合成证据的独立历史可用时间窗口（必须早于任何审计时点）
EVIDENCE_START = datetime(2026, 5, 1, tzinfo=UTC)


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
    """把若干行写成 JSONL（UTF-8）。"""
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    """把字典写成 JSON 文件（UTF-8、稳定排序）并返回路径。"""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def tamper(path: Path, mutate: Any) -> None:
    """就地篡改一个 JSON 文档（模拟 handoff / readiness state 被改写）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json(path, payload)


def build_inbox_package(inbox: Path, name: str = "pkg-author-01") -> Path:
    """在 inbox 下建一个合规候选包（manifest 摘要取自**实际内容**）。"""
    package = inbox / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = write_jsonl(package / EVIDENCE_NAME, [author_row()])
    manifest = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
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


def synthetic_recheck(*, ready: bool, as_of: datetime = RECHECK_AT) -> dict[str, Any]:
    """**合法格式**的 qualification recheck 产物（仅用于测试控制流；不代表任何数据资格）。"""
    return {
        "schema_version": 1,
        "report": QUALIFICATION_RECHECK_REPORT,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "as_of": as_of.isoformat(),
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "ready": ready,
        "gate": {
            "qualification_ready": ready,
            "qualification_pass_count": 7 if ready else 0,
            "qualification_blocked_count": 0 if ready else 2,
            "readiness_ready": ready,
            "readiness_blocked_scope_count": 0 if ready else 2,
        },
        "readiness": {"scopes": []},
        "qualification": {"checks": []},
        "notes": ["测试夹具：仅用于控制流，不代表任何真实数据资格。"],
    }


def ensure_source(session: Session, name: str) -> Source:
    """按名字取源（不存在则建一个**禁用**的 News 源）。"""
    existing = session.scalar(sa.select(Source).where(Source.name == name))
    if existing is not None:
        return existing
    source = Source(name=name, source_type=SourceType.NEWS, timezone="UTC", enabled=False)
    session.add(source)
    session.flush()
    return source


def seed_evidence(
    session_factory: sessionmaker[Session],
    *,
    scope: str,
    source_name: str,
    count: int,
    span_days: int,
) -> None:
    """直接写 ``raw_items``（Mock 证据块，仅用于把 readiness 推到达标；不代表真实资格）。"""
    with session_factory() as session:
        source = ensure_source(session, source_name)
        item_type = RawItemType.POST if scope == "author" else RawItemType.NEWS
        for index in range(count):
            available = EVIDENCE_START + timedelta(days=span_days * index / max(count - 1, 1))
            session.add(
                RawItem(
                    source_id=source.id,
                    source_record_id=f"{source_name}-{index:04d}",
                    item_type=item_type,
                    title="gold evidence",
                    content_text="黄金证据",
                    raw_json={
                        "import_kind": "authorized_evidence_intake",
                        "evidence": {
                            "contract_version": EVIDENCE_CONTRACT_VERSION,
                            "scope": scope,
                            "source": source_name,
                            "oos_eligible": True,
                            "available_at": available.isoformat(),
                        },
                    },
                    source_url="https://example.com/evidence",
                    content_hash=content_hash(f"{source_name}-{index}", "黄金证据"),
                    published_at=available,
                    collected_at=available,
                    effective_at=available,
                )
            )
        session.commit()


def seed_ready_ledger(session_factory: sessionmaker[Session]) -> None:
    """把库内 readiness 推到达标（Author 30 条 / News 210 条 / 覆盖 200 天 / 单源 ~33%）。"""
    from src.alpha.evidence_gate import (
        MIN_AUTHOR_SAMPLES,
        MIN_NEWS_EVENTS,
        MIN_NEWS_HISTORY_DAYS,
    )

    seed_evidence(
        session_factory,
        scope="author",
        source_name="seed-author",
        count=MIN_AUTHOR_SAMPLES,
        span_days=40,
    )
    for name in ("seed-news-a", "seed-news-b", "seed-news-c"):
        seed_evidence(
            session_factory,
            scope="news",
            source_name=name,
            count=(MIN_NEWS_EVENTS + 2) // 3,
            span_days=MIN_NEWS_HISTORY_DAYS + 90,
        )


def row_counts(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    """``(raw_items, sources)`` 计数（断言决策包**绝不**写数据库）。"""
    with session_factory() as session:
        raw = session.scalar(sa.select(sa.func.count()).select_from(RawItem))
        sources = session.scalar(sa.select(sa.func.count()).select_from(Source))
    return int(raw or 0), int(sources or 0)


def file_snapshot(root: Path) -> dict[str, str]:
    """目录内所有文件的相对路径与内容摘要（"什么都没被改写" 的断言用）。"""
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# 真实链路夹具
# ---------------------------------------------------------------------------
def prepare_chain(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    *,
    handoff_at: datetime = HANDOFF_AT,
    synthetic_ready_recheck: bool = False,
    with_receipt: bool = False,
    with_state: bool = True,
) -> dict[str, Path]:
    """跑完整条真实链路：预检 → 复核 → plan → **显式**落库 → recheck → **真实** handoff。

    准备阶段的 operator / recheck / handoff 也会打印报告：这里用 ``redirect_stdout`` 吞掉，
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
    if synthetic_ready_recheck:
        # 测试库无法让真实 qualification gate 达标 → 用**合法格式**产物控制控制流
        write_json(recheck, synthetic_recheck(ready=True))
    else:
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
    handoff = tmp_path / "handoff.json"
    with contextlib.redirect_stdout(io.StringIO()):
        code = handoff_main(
            ["--json", "--out", str(handoff), "--as-of", handoff_at.isoformat()],
            session_factory=session_factory,
        )
    assert code in {EXIT_OK, EXIT_BLOCKED}, "handoff 生成失败（预期 0 或 5）"
    files: dict[str, Path] = {
        "inbox": inbox,
        "package": package,
        "evidence": evidence,
        "ledger": ledger,
        "approved": approved,
        "plan": plan,
        "manifest": manifest,
        "recheck": recheck,
        "handoff": handoff,
    }
    if with_state:
        state = tmp_path / "readiness_state.json"
        write_snapshot_state(state, load_handoff_document(handoff).source)
        files["state"] = state
    if with_receipt:
        receipt = tmp_path / "evidence_intake_receipt.json"
        run_intake_receipt(
            inbox,
            moment=PACKET_AT,
            plan_path=plan,
            ledger_path=ledger,
            approved_list_path=approved,
            operator_result_paths=(manifest,),
            recheck_path=recheck,
            out_path=receipt,
        )
        files["receipt"] = receipt
    return files


def packet_argv(files: dict[str, Path], *extra: str) -> list[str]:
    """构造 GOLD-015 CLI 参数（默认带上可用的 handoff / state / 执行结果 / recheck / 收据）。"""
    argv = [
        "--handoff",
        str(files["handoff"]),
        "--inbox-dir",
        str(files["inbox"]),
        "--ledger",
        str(files["ledger"]),
        "--approved-list",
        str(files["approved"]),
        "--plan",
        str(files["plan"]),
    ]
    for key, flag in (
        ("state", "--readiness"),
        ("receipt", "--receipt"),
        ("manifest", "--operator-result"),
        ("recheck", "--recheck"),
    ):
        target = files.get(key)
        if target is not None:
            argv += [flag, str(target)]
    argv += list(extra)
    return argv


# ---------------------------------------------------------------------------
# 真实链路：证据未达标 → 诚实 BLOCKED（退出 5）
# ---------------------------------------------------------------------------
def test_real_chain_is_honestly_blocked(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    before = file_snapshot(tmp_path)
    rows_before = row_counts(session_factory)

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_BLOCKED
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DECISION_PACKET_KIND
    assert payload["status"] == DecisionPacketStatus.BLOCKED_PENDING_EVIDENCE.value
    assert payload["submit_to_l3_human_gate"] is False
    assert payload["evidence_ready_for_human_review"] is False
    assert payload["receipt_verified"] is True  # 真实显式落库 + 真实 recheck 已绑定
    assert payload["qualification_recheck_ready"] is False
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["readiness_binding"] == "STATE_FILE_MATCHED"
    assert payload["approved_entry_count"] == 1
    assert payload["landed_rows"] >= 1
    assert payload["gaps"]["author"]["remaining"] > 0
    assert payload["verification"]["violations"] == []
    # 只读：文件与数据库都没变
    assert file_snapshot(tmp_path) == before
    assert row_counts(session_factory) == rows_before


# ---------------------------------------------------------------------------
# 真实链路 + 库内达标（Mock 证据块）→ 可提交 L3 人工 Gate（退出 0）
# ---------------------------------------------------------------------------
def test_qualified_chain_can_submit_to_l3_human_gate(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    seed_ready_ledger(session_factory)  # Mock 证据块（仅用于控制流，不代表真实资格）
    files = prepare_chain(
        tmp_path, session_factory, synthetic_ready_recheck=True, with_receipt=True
    )
    before = file_snapshot(tmp_path)
    rows_before = row_counts(session_factory)

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == DecisionPacketStatus.READY_FOR_L3_HUMAN_GATE.value
    assert payload["submit_to_l3_human_gate"] is True
    assert payload["evidence_ready_for_human_review"] is True
    assert payload["receipt_verified"] is True
    assert payload["qualification_recheck_ready"] is True
    assert payload["data_qualification_passed"] is False  # 仍**不是**资格通过
    assert payload["phase_transition_allowed"] is False
    assert payload["data_qualification_passed_count"] == 0
    assert payload["receipt"]["receipt_file_provided"] is True
    assert payload["receipt"]["receipt_file_matched"] is True
    assert payload["gaps"]["author"]["status"] == "PASS"
    assert payload["gaps"]["news"]["status"] == "PASS"
    assert file_snapshot(tmp_path) == before
    assert row_counts(session_factory) == rows_before


def test_cli_out_writes_packet_atomically_and_idempotently(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    seed_ready_ledger(session_factory)
    files = prepare_chain(tmp_path, session_factory, synthetic_ready_recheck=True)
    out = tmp_path / "decision_packet.json"

    assert main(packet_argv(files, "--json", "--out", str(out)), moment=PACKET_AT) == EXIT_OK
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["kind"] == DECISION_PACKET_KIND
    assert document["packet_id"] == json.loads(capsys.readouterr().out)["packet_id"]
    assert document["written_path"] is None

    assert main(packet_argv(files, "--json", "--out", str(out)), moment=PACKET_AT) == EXIT_OK
    assert out.read_text(encoding="utf-8") == text  # 幂等：逐字节稳定
    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# 篡改 / 参数 / 路径 / 锁
# ---------------------------------------------------------------------------
def test_tampered_handoff_exits_4_without_writing(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    tamper(files["handoff"], lambda payload: payload.__setitem__("blocker_active", False))
    before = file_snapshot(tmp_path)

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""  # 失败路径 stdout 为空
    assert PacketVerificationCode.HANDOFF_TAMPERED.value in captured.err
    assert file_snapshot(tmp_path) == before


def test_tampered_readiness_state_exits_4(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    tamper(files["state"], lambda payload: payload.__setitem__("ready_for_human_review", True))

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    # 指纹校验失败 → readiness state 被视为损坏 / 被篡改
    assert PacketVerificationCode.READINESS_TAMPERED.value in captured.err


def test_content_change_exits_4(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    write_jsonl(files["evidence"], [author_row(source_record_id="post-0002")])

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PLAN_STALE" in captured.err or "PLAN_ENTRY_MISMATCH" in captured.err


def test_stale_handoff_exits_4(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    """handoff 早于最近一次显式落库 → stale（必须重新生成 handoff 才能提交）。"""
    files = prepare_chain(tmp_path, session_factory, handoff_at=MOMENT, with_state=False)

    code = main(packet_argv(files, "--json"), moment=PACKET_AT)

    assert code == EXIT_STATE_INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert PacketVerificationCode.HANDOFF_STALE.value in captured.err


def test_argument_errors_exit_2(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    files = prepare_chain(tmp_path, session_factory)
    base = packet_argv(files, "--json")
    without_handoff = [
        part
        for index, part in enumerate(base)
        if part != "--handoff" and not (index > 0 and base[index - 1] == "--handoff")
    ]

    with pytest.raises(SystemExit) as missing_handoff:
        main(without_handoff, moment=PACKET_AT)
    assert missing_handoff.value.code == EXIT_CONFIG_ERROR

    with pytest.raises(SystemExit) as naive_as_of:
        main(packet_argv(files, "--json", "--as-of", "2026-09-23T04:00:00"), moment=PACKET_AT)
    assert naive_as_of.value.code == EXIT_CONFIG_ERROR


def test_unusable_paths_exit_3(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)

    inside_inbox = packet_argv(files, "--json", "--out", str(files["inbox"] / "packet.json"))
    assert main(inside_inbox, moment=PACKET_AT) == EXIT_UNUSABLE
    assert capsys.readouterr().out == ""

    argv = packet_argv(files, "--json")
    argv[argv.index("--inbox-dir") + 1] = str(tmp_path / "absent-inbox")
    assert main(argv, moment=PACKET_AT) == EXIT_UNUSABLE
    assert capsys.readouterr().out == ""


def test_lock_conflict_exits_6_without_writes(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    out = tmp_path / "decision_packet.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")

    with held:
        code = main(packet_argv(files, "--json", "--out", str(out)), moment=PACKET_AT)

    assert code == EXIT_LOCK_CONFLICT
    assert capsys.readouterr().out == ""
    assert not out.exists()


def test_cli_markdown_summary_keeps_blocker_visible(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)

    assert main(packet_argv(files), moment=PACKET_AT) == EXIT_BLOCKED

    text = capsys.readouterr().out
    assert "blocker_active" in text
    assert "human_gate_required" in text
    assert "data_qualification_passed" in text
    assert "phase_transition_allowed" in text
    assert PHASE3_3_BLOCKER_CODE in text
    assert "L3" in text
    assert "submit_to_l3_human_gate" in text


def test_cli_real_process_smoke(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """真实子进程冒烟：`python -m scripts.evidence_decision_packet`（stdout 为纯 JSON）。"""
    files = prepare_chain(tmp_path, session_factory)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 让子进程 stdout 稳定为 UTF-8
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_decision_packet",
            *packet_argv(files, "--json", "--as-of", PACKET_AT.isoformat()),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        check=False,
    )

    assert result.returncode == EXIT_BLOCKED, result.stderr
    payload = json.loads(result.stdout)
    assert payload["kind"] == DECISION_PACKET_KIND
    assert payload["submit_to_l3_human_gate"] is False
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["handoff"]["report"] == HANDOFF_REPORT_NAME