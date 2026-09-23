"""GOLD-026 ``PHASE3_3_DATA`` 缺口诊断 CLI 端到端测试（临时 SQLite + 本地 manifest）。

覆盖：

- 空库 → 诚实 BLOCKED（退出码 5、``advance_allowed=false``），且零数据库写入；
- CLI 没有任何"资格通过 / 推进 Phase"的参数；未知参数一律拒绝；
- 唯一写开关是显式 ``--out``；``--inbox-dir`` 只做本地只读预检；
- 本地候选目录缺失 → fail-closed（退出码 2）；
- 通过真实 ``scripts.intake_evidence`` 写入合规证据块（仅控制流）后：量化门槛达标仍
  ``GATE_BLOCKED``、仍需 L3 人工决策（**绝不**自动 qualified）。

全链路使用临时 SQLite + 临时目录，零网络。
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
from scripts.evidence_gap_diagnostic import (
    EXIT_EVIDENCE_GAPS,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_OUTPUT_UNUSABLE,
    build_parser,
    main,
)
from scripts.intake_evidence import main as intake_main
from src.alpha.evidence_gate import MIN_AUTHOR_SAMPLES, MIN_NEWS_EVENTS
from src.common import hashing
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION
from src.evidence.inbox import MANIFEST_FILE_NAME
from src.monitoring.evidence_gap_diagnostic import (
    MANDATORY_HUMAN_STEPS,
    ReadinessClass,
    StepVerification,
)

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
FORBIDDEN_OPTIONS = ("--qualify", "--approve", "--advance", "--no-dry-run", "--write-db")


def author_row(index: int) -> dict[str, Any]:
    """一条**完全合规**的 Author 证据行（时间自洽、带独立可用时间）。"""
    stamp = WINDOW_START + timedelta(days=index % 5)
    return {
        "source": "manual-evidence-author",
        "source_record_id": f"post-{index:04d}",
        "author_name": "作者甲",
        "external_account_id": "acct-0001",
        "content": "黄金在 2400 附近承压，若跌破 2380 看向 2350。",
        "published_at": stamp.isoformat(),
        "collected_at": (stamp + timedelta(hours=1)).isoformat(),
        "available_at": (stamp + timedelta(minutes=30)).isoformat(),
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "url": f"https://vendor.example/posts/post-{index:04d}",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "docs/legal/author-permits/example-author.md",
        "authorization_reviewed_by": "合规复核人-张三",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "authorization_valid_from": "2026-04-01T00:00:00+00:00",
        "authorization_expires_at": "2027-04-01T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }


def news_row(index: int) -> dict[str, Any]:
    """一条**完全合规**的 News 证据行（覆盖 91 天窗口、单源占比 ≤ 40%）。"""
    stamp = WINDOW_START + timedelta(days=index % 91)
    return {
        "source": f"manual-evidence-news-{index % 3}",
        "source_record_id": f"news-{index:04d}",
        "title": "金价短线回落",
        "content": "亚洲盘金价自 2400 回落至 2385。",
        "published_at": stamp.isoformat(),
        "collected_at": (stamp + timedelta(hours=1)).isoformat(),
        "available_at": (stamp + timedelta(minutes=30)).isoformat(),
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "url": f"https://vendor.example/news/news-{index:04d}",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "合规复核人-张三",
        "authorization_reviewed_at": "2026-05-30T00:00:00+00:00",
        "authorization_valid_from": "2026-04-01T00:00:00+00:00",
        "authorization_expires_at": "2027-04-01T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    """把证据行写成 JSONL（UTF-8；零网络）。"""
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def ingest(path: Path, scope: str, session_factory: sessionmaker[Session]) -> None:
    """通过真实 intake 入口写入证据块（仅用于**控制流**；不代表授权或资格通过）。"""
    code = intake_main(
        ["--scope", scope, "--input", str(path), "--as-of", MOMENT.isoformat(), "--no-dry-run"],
        session_factory=session_factory,
    )
    assert code == 0


def raw_item_count(session_factory: sessionmaker[Session]) -> int:
    """库内原始记录数（用于断言诊断本身零写入）。"""
    with session_factory() as session:
        return int(session.scalar(sa.select(sa.func.count()).select_from(RawItem)) or 0)


def build_local_package(root: Path) -> Path:
    """建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / "pkg-news-01"
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / "news.jsonl"
    write_jsonl(evidence, [news_row(0)])
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": "news",
        "source": "manual-evidence-news-0",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": "news.jsonl",
                "sha256": hashing.sha256_bytes(evidence.read_bytes()),
                "format": "jsonl",
            }
        ],
    }
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def cli_json(
    session_factory: sessionmaker[Session], capsys: Any, *args: str
) -> tuple[int, dict[str, Any]]:
    """运行 CLI 并解析 stdout 的纯 JSON（返回退出码与载荷）。"""
    capsys.readouterr()  # 先清空缓冲区，保证只解析本次 CLI 的 stdout
    code = main([*args, "--json", "--as-of", MOMENT.isoformat()], session_factory=session_factory)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_empty_database_is_gate_blocked_and_zero_write(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    before = raw_item_count(session_factory)

    code, payload = cli_json(session_factory, capsys)

    assert code == EXIT_EVIDENCE_GAPS
    assert payload["blocker_code"] == "PHASE3_3_DATA"
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["gate_blocked"] is True
    assert payload["readiness"] == ReadinessClass.GATE_BLOCKED.value
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["advance_allowed"] is False
    assert payload["l3_l4_auto_advance_allowed"] is False
    assert payload["open_evidence_gap_count"] > 0
    assert payload["manifest"]["scanned"] is False
    assert raw_item_count(session_factory) == before


def test_cli_has_no_route_to_qualify_or_advance(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    help_text = build_parser().format_help()

    assert "--out" in help_text
    for option in FORBIDDEN_OPTIONS:
        assert option not in help_text
        with pytest.raises(SystemExit) as failure:
            main([option], session_factory=session_factory)
        assert failure.value.code == 2
        capsys.readouterr()


def test_out_is_the_only_write_switch(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    workdir = tmp_path / "cli-workdir"
    workdir.mkdir()
    target = workdir / "reports" / "gap.json"

    code, _payload = cli_json(session_factory, capsys)

    assert code == EXIT_EVIDENCE_GAPS
    assert list(workdir.iterdir()) == []  # 默认零写入
    assert not target.exists()

    code = main(
        ["--json", "--as-of", MOMENT.isoformat(), "--out", str(target)],
        session_factory=session_factory,
    )
    capsys.readouterr()
    assert code == EXIT_EVIDENCE_GAPS
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["blocker_active"] is True
    assert written["advance_allowed"] is False
    assert written["data_qualification_passed"] is False


def test_local_inbox_dir_is_preflight_only(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    build_local_package(tmp_path / "inbox")

    code, payload = cli_json(session_factory, capsys, "--inbox-dir", str(tmp_path / "inbox"))

    manifest = payload["manifest"]
    assert code == EXIT_EVIDENCE_GAPS
    assert manifest["scanned"] is True
    assert manifest["package_count"] == 1
    assert manifest["preflight_pass_count"] == 1
    assert manifest["packages"][0]["qualifies"] is False
    assert manifest["packages"][0]["declared_fields"]
    assert raw_item_count(session_factory) == 0  # 只读预检：不 ingest


def test_missing_inbox_dir_fails_closed(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    code = main(
        ["--json", "--as-of", MOMENT.isoformat(), "--inbox-dir", str(tmp_path / "absent")],
        session_factory=session_factory,
    )
    captured = capsys.readouterr()

    assert code == EXIT_INPUT_ERROR
    assert captured.out == ""
    assert "InboxDirError" in captured.err


def test_unusable_out_path_returns_error(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    code = main(
        ["--json", "--as-of", MOMENT.isoformat(), "--out", str(blocker / "gap.json")],
        session_factory=session_factory,
    )
    capsys.readouterr()

    assert code == EXIT_OUTPUT_UNUSABLE


def test_markdown_output_keeps_blocker(
    session_factory: sessionmaker[Session], capsys: Any
) -> None:
    code = main(["--as-of", MOMENT.isoformat()], session_factory=session_factory)
    out = capsys.readouterr().out

    assert code == EXIT_EVIDENCE_GAPS
    assert "PHASE3_3_DATA 证据缺口诊断" in out
    assert "GATE_BLOCKED" in out
    assert "不解除" in out
    assert "advance_allowed=false" in out


def test_ingested_evidence_keeps_gate_blocked_and_human_only(
    session_factory: sessionmaker[Session], tmp_path: Path, capsys: Any
) -> None:
    authors = write_jsonl(
        tmp_path / "authors.jsonl", [author_row(index) for index in range(MIN_AUTHOR_SAMPLES)]
    )
    news = write_jsonl(
        tmp_path / "news.jsonl", [news_row(index) for index in range(MIN_NEWS_EVENTS)]
    )
    ingest(authors, "author", session_factory)
    ingest(news, "news", session_factory)
    assert raw_item_count(session_factory) > 0

    code, payload = cli_json(session_factory, capsys)

    assert code == EXIT_OK  # 无实质证据缺口
    assert payload["readiness"] == ReadinessClass.GATE_BLOCKED.value
    assert payload["class_counts"][ReadinessClass.EVIDENCE_MISSING.value] == 0
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["advance_allowed"] is False
    assert payload["l3_l4_auto_advance_allowed"] is False

    steps = {step["step"]: step for step in payload["human_steps"]}
    assert [step["step"] for step in payload["human_steps"]] == [
        spec.step for spec in MANDATORY_HUMAN_STEPS
    ]
    assert all(step["blocking"] is True for step in payload["human_steps"])
    assert steps["provide_authorized_evidence"]["verification"] == (
        StepVerification.MACHINE_CONFIRMED.value
    )
    assert steps["l3_human_gate_decision"]["verification"] == StepVerification.HUMAN_ONLY.value
    assert payload["manifest"]["scanned"] is False


