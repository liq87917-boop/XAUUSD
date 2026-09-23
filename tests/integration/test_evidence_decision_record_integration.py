"""GOLD-016 L3 人工决策记录集成测试（真实 CLI + 真实审计产物 + 临时 SQLite / 零网络）。

端到端链路（全部临时目录、临时 SQLite、零网络、零真实授权数据）：

    inbox 摆放候选包 → GOLD-011 预检 → GOLD-012 人工复核 / 批准清单 → GOLD-013 只读 plan
    → **人工显式** Evidence Operator 真落库（``--no-dry-run --manifest``）
    → 真实 qualification recheck → **真实** GOLD-008 handoff 报告
    → GOLD-015 决策包（``scripts.evidence_decision_packet``）
    → **GOLD-016 人工决策记录**（``scripts.evidence_decision_record``）+ 防伪核验

覆盖：真实链路（证据未达标）→ 决策包诚实 BLOCKED（退出 5）→ ``approve`` 被拒（退出 5，
零写入）而 ``reject`` / ``needs_changes`` 可记录；库内达标（Mock 证据块，**仅控制流**）→
``approve`` 可记录（退出 0，但 ``data_qualification_passed`` 恒 false）；packet 被改写 →
``approve`` / 防伪核验均 fail-closed；覆盖既有记录必须显式 revision + supersedes；
并发锁冲突（退出 6，零写入）；**绝不**写数据库 / 原始 evidence；真实子进程 CLI 冒烟。
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
from scripts.evidence_decision_packet import main as packet_main
from scripts.evidence_decision_record import main as record_main
from scripts.evidence_handoff import main as handoff_main
from scripts.evidence_operator import main as operator_main
from scripts.evidence_readiness import main as readiness_main
from src.common import hashing
from src.common.hashing import content_hash
from src.evidence import (
    DECISION_RECORD_KIND,
    EVIDENCE_CONTRACT_VERSION,
    EXIT_BLOCKED,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_STATE_INVALID,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    QUALIFICATION_RECHECK_REPORT,
    DecisionRecordCode,
    SingleInstanceLock,
    load_handoff_document,
    run_intake_plan,
    run_intake_receipt,
    run_review,
    scan_inbox,
    write_snapshot_state,
)
from src.evidence.human_verification_attestation import run_attestation
from src.evidence.intake_handoff import load_intake_handoff
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
INTAKE_AT = MOMENT + timedelta(hours=1)
RECHECK_AT = MOMENT + timedelta(hours=2)
HANDOFF_AT = MOMENT + timedelta(hours=3)
PACKET_AT = MOMENT + timedelta(hours=4)
DECISION_AT = MOMENT + timedelta(hours=5)
REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_NAME = "author.jsonl"
APPROVE_CODE = "APPROVED_FOR_EXPLICIT_INTAKE"
REVIEWER = "operator-l3-li"
REASON = "APPROVED_AFTER_L3_REVIEW"
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
    """就地篡改一个 JSON 文档（模拟 packet 被改写）。"""
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
    """``(raw_items, sources)`` 计数（断言本工具**绝不**写数据库）。"""
    with session_factory() as session:
        raw = session.scalar(sa.select(sa.func.count()).select_from(RawItem))
        sources = session.scalar(sa.select(sa.func.count()).select_from(Source))
    return int(raw or 0), int(sources or 0)


def file_snapshot(root: Path, *, include_locks: bool = False) -> dict[str, str]:
    """目录内所有文件的相对路径与内容摘要（"什么都没被改写" 的断言用）。

    note:
        默认**跳过** ``*.lock``：单实例锁文件是并发审计留痕（且持锁时不可读），
        不属于被断言"零变化"的产物。
    """
    return {
        str(path.relative_to(root)): hashing.sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and (include_locks or not path.name.endswith(".lock"))
    }


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


def record_argv(packet: Path, decision: str, *extra: str) -> list[str]:
    """构造 GOLD-016 CLI 参数（``--decision`` / ``--reviewer`` 显式给出）。"""
    return [
        "--packet",
        str(packet),
        "--decision",
        decision,
        "--reviewer",
        REVIEWER,
        *extra,
    ]


def make_packet(
    tmp_path: Path,
    files: dict[str, Path],
    *extra: str,
) -> tuple[int, Path]:
    """用**真实** GOLD-015 CLI 落盘一个决策包，返回（退出码, packet 路径）。"""
    packet = tmp_path / "evidence_decision_packet.json"
    code = packet_main(packet_argv(files, *extra, "--out", str(packet)), moment=PACKET_AT)
    return code, packet


# ---------------------------------------------------------------------------
# 真实链路（证据未达标）：approve 被拒，reject 可记录，数据库 / 文件零变化
# ---------------------------------------------------------------------------
def test_blocked_chain_records_reject_and_refuses_approve(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    packet_code, packet = make_packet(tmp_path, files)
    assert packet_code == EXIT_BLOCKED, "证据未达标时必须诚实 BLOCKED"
    capsys.readouterr()
    rows_before = row_counts(session_factory)
    before = file_snapshot(tmp_path)

    # ① approve：packet 不可提交 → fail-closed（退出 5、零写入）
    out = tmp_path / "l3_record.json"
    approve_code = record_main(
        record_argv(packet, "approve", "--out", str(out), "--json"), moment=DECISION_AT
    )
    captured = capsys.readouterr()
    assert approve_code == EXIT_BLOCKED
    assert captured.out == ""
    assert DecisionRecordCode.PACKET_NOT_SUBMITTABLE.value in captured.err
    assert not out.exists()
    assert file_snapshot(tmp_path) == before
    assert row_counts(session_factory) == rows_before

    # ② needs_changes：可记录，且**不改变**任何资格状态
    code = record_main(
        record_argv(
            packet,
            "needs_changes",
            "--note",
            "证据不足，退回补充真实授权与独立可用性证据",
            "--out",
            str(out),
            "--json",
        ),
        moment=DECISION_AT,
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DECISION_RECORD_KIND
    assert payload["human_decision"] == "needs_changes"
    assert payload["packet_verified"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["phase_transition_executed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["packet"]["packet_id"]
    assert payload["packet"]["submit_to_l3_human_gate"] is False
    assert out.exists()

    # ③ 防伪核验（只读）通过；数据库与其余文件仍零变化
    rows_after = row_counts(session_factory)
    assert record_main(
        ["--packet", str(packet), "--verify-record", str(out), "--json"], moment=DECISION_AT
    ) == EXIT_OK
    verification = json.loads(capsys.readouterr().out)
    assert verification["verified"] is True
    assert verification["approve_still_valid"] is None
    assert row_counts(session_factory) == rows_after == rows_before


# ---------------------------------------------------------------------------
# 库内达标（Mock 证据块，仅控制流）：approve 可记录；packet 被改写 → fail-closed
# ---------------------------------------------------------------------------
def test_qualified_chain_approve_then_detect_packet_change(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    seed_ready_ledger(session_factory)  # Mock 证据块（仅用于控制流，不代表真实资格）
    files = prepare_chain(
        tmp_path, session_factory, synthetic_ready_recheck=True, with_receipt=True
    )
    packet_code, packet = make_packet(tmp_path, files)
    assert packet_code == EXIT_OK, "库内达标（Mock）时应可提交 L3 人工 Gate"
    capsys.readouterr()
    out = tmp_path / "l3_record.json"

    assert (
        record_main(
            record_argv(
                packet, "approve", "--reason-code", REASON, "--out", str(out), "--json"
            ),
            moment=DECISION_AT,
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_decision"] == "approve"
    assert payload["approve_gate"]["satisfied"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    record_id = payload["record_id"]

    # 只读防伪核验：与当前 packet 完全一致
    assert (
        record_main(
            ["--packet", str(packet), "--verify-record", str(out), "--json"],
            moment=DECISION_AT,
        )
        == EXIT_OK
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True

    # 覆盖既有记录必须显式 revision + supersedes（绝不静默改写历史）
    conflict_code = record_main(
        record_argv(packet, "reject", "--out", str(out), "--json"), moment=DECISION_AT
    )
    captured = capsys.readouterr()
    assert conflict_code == EXIT_STATE_INVALID
    assert DecisionRecordCode.RECORD_CONFLICT.value in captured.err
    assert json.loads(out.read_text(encoding="utf-8"))["record_id"] == record_id

    supersede_code = record_main(
        record_argv(
            packet,
            "reject",
            "--revision",
            "2",
            "--supersedes",
            record_id,
            "--out",
            str(out),
            "--json",
        ),
        moment=DECISION_AT,
    )
    assert supersede_code == EXIT_OK
    superseded = json.loads(capsys.readouterr().out)
    assert superseded["revision"] == 2
    assert superseded["supersedes"] == record_id
    assert superseded["record_id"] != record_id
    assert superseded["data_qualification_passed"] is False
    assert json.loads(out.read_text(encoding="utf-8"))["revision"] == 2
    assert (
        record_main(
            ["--packet", str(packet), "--verify-record", str(out), "--json"],
            moment=DECISION_AT,
        )
        == EXIT_OK
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True

    # packet 被改写（例如把 data_qualification_passed 偷偷置 true）→ 一切 fail-closed
    tamper(packet, lambda doc: doc.__setitem__("data_qualification_passed", True))
    before = file_snapshot(tmp_path)
    assert (
        record_main(
            record_argv(packet, "approve", "--out", str(tmp_path / "other.json"), "--json"),
            moment=DECISION_AT,
        )
        == EXIT_STATE_INVALID
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert DecisionRecordCode.PACKET_TAMPERED.value in captured.err
    assert not (tmp_path / "other.json").exists()
    assert file_snapshot(tmp_path) == before

    # 防伪核验：当前 packet 自身不合法 → 也 fail-closed（绝不宣称有效）
    assert (
        record_main(
            ["--packet", str(packet), "--verify-record", str(out), "--json"],
            moment=DECISION_AT,
        )
        == EXIT_STATE_INVALID
    )
    assert capsys.readouterr().out == ""




# ---------------------------------------------------------------------------
# 并发锁冲突（fail-closed、零写入）与真实子进程 CLI 冒烟
# ---------------------------------------------------------------------------
def test_lock_conflict_exits_6_without_writes(
    tmp_path: Path, session_factory: sessionmaker[Session], capsys: Any
) -> None:
    files = prepare_chain(tmp_path, session_factory)
    _, packet = make_packet(tmp_path, files)
    capsys.readouterr()
    out = tmp_path / "l3_record.json"
    held = SingleInstanceLock(out.with_name(out.name + ".lock"), owner="held-by-test")
    with held:
        # 锁文件本身是并发审计留痕；在持锁后取快照，证明**其余文件零变化**
        before = file_snapshot(tmp_path)
        code = record_main(
            record_argv(packet, "reject", "--out", str(out), "--json"), moment=DECISION_AT
        )
        assert file_snapshot(tmp_path) == before
    captured = capsys.readouterr()
    assert code == EXIT_LOCK_CONFLICT
    assert captured.out == ""
    assert not out.exists()


def test_real_subprocess_cli_smoke(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """真实子进程冒烟：`python -m scripts.evidence_decision_record`（stdout 为纯 JSON）。"""
    files = prepare_chain(tmp_path, session_factory)
    _, packet = make_packet(tmp_path, files)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = tmp_path / "l3_record.json"

    blocked = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_decision_record",
            *record_argv(packet, "approve", "--json", "--as-of", DECISION_AT.isoformat()),
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
    assert blocked.returncode == EXIT_BLOCKED, blocked.stderr
    assert blocked.stdout == ""

    recorded = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_decision_record",
            *record_argv(
                packet,
                "reject",
                "--json",
                "--out",
                str(out),
                "--as-of",
                DECISION_AT.isoformat(),
            ),
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
    assert recorded.returncode == EXIT_OK, recorded.stderr
    payload = json.loads(recorded.stdout)
    assert payload["kind"] == DECISION_RECORD_KIND
    assert payload["human_decision"] == "reject"
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["blocker_active"] is True
    assert payload["human_gate_level"] == "L3"
    assert payload["packet"]["status"] == "BLOCKED_PENDING_EVIDENCE"
    assert payload["packet_verification"]["violations"] == []
    assert out.exists()

    verified = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evidence_decision_record",
            "--packet",
            str(packet),
            "--verify-record",
            str(out),
            "--json",
            "--as-of",
            DECISION_AT.isoformat(),
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
    assert verified.returncode == EXIT_OK, verified.stderr
    assert json.loads(verified.stdout)["verified"] is True
