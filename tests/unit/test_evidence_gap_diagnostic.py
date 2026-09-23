"""GOLD-026 ``PHASE3_3_DATA`` 缺口只读诊断单元测试（纯函数 / 零数据库 / 零网络）。

覆盖：

- 空库、部分达标、量化门槛全达标三种情形下的四态分类与 fail-closed 结论；
- 顶层结论恒为 ``GATE_BLOCKED``，安全布尔恒为假（工具永不解除 / 永不推进 L3-L4）；
- 缺失字段只报告事实：报告中除 ``as_of`` 外不出现任何时间戳，且绝不使用文件 mtime；
- Mock / 模板 / 示例始终 non-qualifying；
- 本地 manifest 事实（授权引用 / 时间语义声明 / 合成标记）与缺目录 fail-closed；
- 源码级守卫：无网络 / 无写库 / 无 ``os.stat`` / ``getmtime`` / ``utime``。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
)
from src.common import hashing
from src.evidence.contracts import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceScope,
    field_by_name,
    required_field_names,
)
from src.evidence.inbox import (
    MANIFEST_FILE_NAME,
    MANIFEST_REQUIRED_FIELDS,
    InboxDirError,
    scan_inbox,
)
from src.evidence.ledger import ledger_from_raw_json
from src.monitoring.evidence_gap_diagnostic import (
    AUTHORIZATION_CONTRACT_FIELDS,
    AVAILABILITY_CONTRACT_FIELDS,
    DIAGNOSTIC_KIND,
    DIAGNOSTIC_SCHEMA_VERSION,
    GAP_CATEGORIES,
    MANDATORY_HUMAN_STEPS,
    TIME_CONTRACT_FIELDS,
    ManifestDiagnostic,
    ReadinessClass,
    StepVerification,
    build_gap_diagnostic,
    build_manifest_diagnostic,
    load_gap_diagnostic,
    render_gap_diagnostic_markdown,
)
from src.monitoring.evidence_readiness import build_readiness_report

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "monitoring" / "evidence_gap_diagnostic.py"
CLI_PATH = REPO_ROOT / "scripts" / "evidence_gap_diagnostic.py"
WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)

#: ``account`` 之外的 ISO 时间戳正则（用于"绝不伪造时间"的断言）
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")

#: 允许的诊断模块导入（单一事实源：既有 evidence / monitoring / 阈值模块）
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "pathlib",
        "typing",
        "sqlalchemy",
        "sqlalchemy.orm",
        "src.alpha.evidence_gate",
        "src.common.redaction",
        "src.evidence.contracts",
        "src.evidence.handoff",
        "src.evidence.inbox",
        "src.evidence.ledger",
        "src.monitoring.evidence_readiness",
        "src.monitoring.phase33_qualification",
    }
)
#: 诊断 / CLI 源码中**禁止**出现的调用名（网络 / 采集 / 写库 / 文件时间）
FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "getmtime",
        "utime",
        "stat",
        "system",
        "popen",
        "run",
        "Popen",
        "urlopen",
        "connect",
        "intake_evidence",
        "commit",
        "add",
        "delete",
        "write_text",
    }
)


def evidence_block(
    scope: str,
    *,
    record_id: str,
    oos: bool = True,
    available_at: str | None = None,
    source: str = "evidence-source-a",
) -> dict[str, Any]:
    """构造一条 ``evidence-intake-v1`` 证据块（纯内存，零数据库）。"""
    return {
        "evidence": {
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "scope": scope,
            "oos_eligible": oos,
            "available_at": available_at,
            "source": source,
            "source_record_id": record_id,
        }
    }


def diagnostic_for(
    entries: list[dict[str, Any]],
    *,
    manifest: ManifestDiagnostic | None = None,
) -> Any:
    """由内存条目构造诊断（复用既有 readiness / handoff 口径，零数据库）。"""
    readiness = build_readiness_report(
        ledger_from_raw_json((entry, None) for entry in entries), as_of=MOMENT
    )
    return build_gap_diagnostic(readiness, manifest=manifest)


def compliant_author_entries(count: int = MIN_AUTHOR_SAMPLES) -> list[dict[str, Any]]:
    """达到 Author 量化门槛的合规条目（含独立 ``available_at``）。"""
    return [
        evidence_block(
            EvidenceScope.AUTHOR.value,
            record_id=f"a-{index:04d}",
            available_at=(WINDOW_START + timedelta(days=index)).isoformat(),
        )
        for index in range(count)
    ]


def compliant_news_entries(
    count: int = MIN_NEWS_EVENTS, *, days: int = MIN_NEWS_HISTORY_DAYS
) -> list[dict[str, Any]]:
    """达到 News 量化门槛的合规条目（覆盖 ≥ 门槛天数、单源占比 ≤ 40%）。"""
    return [
        evidence_block(
            EvidenceScope.NEWS.value,
            record_id=f"n-{index:04d}",
            available_at=(WINDOW_START + timedelta(days=index % (days + 1))).isoformat(),
            source=f"evidence-source-{index % 3}",
        )
        for index in range(count)
    ]


class _EmptyResult:
    """只读假结果集（永远为空）。"""

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    """只读假会话：不连数据库、不写任何东西。"""

    def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()


def news_row(record_id: str, *, synthetic: bool = False) -> dict[str, Any]:
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
    root: Path,
    *,
    name: str = "pkg-news-01",
    synthetic: bool = False,
    authorization_reference: str = "https://vendor.example/terms",
    time_semantics: str = "provider_export_iso8601_with_tz",
    availability_semantics: str = "provider_archive_export_daily_snapshot",
    historical_oos_applicable: bool | None = True,
) -> Path:
    """在 ``root`` 下建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    evidence = package / "news.jsonl"
    evidence.write_text(
        json.dumps(news_row("n-0001", synthetic=synthetic), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": "news",
        "source": "evidence-source-a",
        "authorization_reference": authorization_reference,
        "time_semantics": time_semantics,
        "availability_semantics": availability_semantics,
        "historical_oos_applicable": historical_oos_applicable,
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


def manifest_section(root: Path) -> ManifestDiagnostic:
    """对候选根目录做**只读**预检并映射为诊断段（零数据库、零网络）。"""
    return build_manifest_diagnostic(scan_inbox(root, moment=MOMENT))


def imported_modules(path: Path) -> set[str]:
    """收集源码中 ``import`` / ``from ... import`` 的模块名（源码级守卫用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def called_names(path: Path) -> set[str]:
    """收集源码中被调用的**简单名**（``f(...)`` / ``x.f(...)`` 都算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_empty_ledger_is_gate_blocked_and_never_qualified() -> None:
    diagnostic = diagnostic_for([])

    assert diagnostic.schema_version == DIAGNOSTIC_SCHEMA_VERSION
    assert diagnostic.kind == DIAGNOSTIC_KIND
    assert diagnostic.readiness is ReadinessClass.GATE_BLOCKED
    assert diagnostic.blocker_active is True
    assert diagnostic.human_gate_required is True
    assert diagnostic.gate_blocked is True
    assert diagnostic.data_qualification_passed is False
    assert diagnostic.phase_transition_allowed is False
    assert diagnostic.advance_allowed is False
    assert diagnostic.l3_l4_auto_advance_allowed is False
    assert diagnostic.as_of == MOMENT
    assert diagnostic.open_evidence_gap_count > 0
    assert diagnostic.count(ReadinessClass.CODE_READY) > 0
    assert diagnostic.count(ReadinessClass.GATE_BLOCKED) > 0


def test_empty_ledger_has_no_human_verification_items() -> None:
    """无任何证据时不得出现"证据已在、只需人工核验"的项（防把缺失说成已具备）。"""
    diagnostic = diagnostic_for([])

    assert diagnostic.items_for(ReadinessClass.HUMAN_VERIFICATION_REQUIRED) == ()
    missing_items = diagnostic.items_for(ReadinessClass.EVIDENCE_MISSING)
    assert missing_items
    # 每个实质缺口都必须明确指出"缺什么"（缺字段或缺多少条），不得只写结论
    assert all(item.missing_fields or item.missing_count is not None for item in missing_items)


def test_empty_ledger_item_keys_are_stable_and_ordered() -> None:
    diagnostic = diagnostic_for([])

    expected = [
        "contract.intake_fields",
        "contract.manifest_fields",
        "contract.read_only",
        "contract.reason_codes",
        "gate.l3_l4",
        "gate.model_authority",
        "gate.phase3_3_data",
        "author.authorization",
        "author.source",
        "author.published_at",
        "author.collected_at",
        "author.effective_at",
        "author.availability_oos",
        "author.time_semantics",
        "author.sample_size",
        "news.authorization",
        "news.source",
        "news.source_share",
        "news.published_at",
        "news.collected_at",
        "news.effective_at",
        "news.availability_oos",
        "news.time_semantics",
        "news.sample_size",
        "news.coverage",
    ]

    assert [item.key for item in diagnostic.items] == expected
    assert len({item.key for item in diagnostic.items}) == len(expected)
    assert all(item.category in GAP_CATEGORIES for item in diagnostic.items)
    assert all(item.scope in {"both", "author", "news"} for item in diagnostic.items)
    payload = diagnostic.to_dict()
    assert payload["report"] == DIAGNOSTIC_KIND
    assert all(item["mock_or_template_qualifies"] is False for item in payload["items"])


def test_report_never_contains_fabricated_timestamps() -> None:
    """除审计时点外，报告中不得出现任何时间戳（防用当前时间 / mtime 填补缺失值）。"""
    diagnostic = diagnostic_for([])

    payload = json.dumps(diagnostic.to_dict(), ensure_ascii=False, sort_keys=True)

    assert set(_TIMESTAMP_RE.findall(payload)) == {diagnostic.as_of.isoformat()}


def test_human_steps_are_blocking_and_ordered() -> None:
    diagnostic = diagnostic_for([])

    assert [step.step for step in diagnostic.human_steps] == [
        spec.step for spec in MANDATORY_HUMAN_STEPS
    ]
    assert all(step.blocking is True for step in diagnostic.human_steps)
    assert all(step["blocking"] is True for step in diagnostic.to_dict()["human_steps"])

    verifications = {step.step: step.verification for step in diagnostic.human_steps}
    assert verifications["provide_authorized_evidence"] is StepVerification.MACHINE_UNSATISFIED
    assert verifications["verify_authorization_validity"] is StepVerification.HUMAN_ONLY
    assert verifications["verify_independent_availability"] is StepVerification.MACHINE_UNSATISFIED
    assert verifications["reach_quantified_thresholds"] is StepVerification.MACHINE_UNSATISFIED
    assert verifications["exclude_synthetic_evidence"] is StepVerification.HUMAN_ONLY
    assert verifications["l3_human_gate_decision"] is StepVerification.HUMAN_ONLY

    thresholds_step = diagnostic.step("reach_quantified_thresholds")
    assert str(MIN_AUTHOR_SAMPLES) in thresholds_step.requirement
    assert str(MIN_NEWS_EVENTS) in thresholds_step.requirement
    assert str(MIN_NEWS_HISTORY_DAYS) in thresholds_step.requirement
    assert str(int(MAX_NEWS_SOURCE_SHARE * 100)) in thresholds_step.requirement


def test_non_qualifying_evidence_is_always_false() -> None:
    diagnostic = diagnostic_for([])

    kinds = {item.kind for item in diagnostic.non_qualifying}
    assert {"template_or_example", "mock_or_synthetic_corpus"} <= kinds
    assert all(item.counts_toward_eligibility is False for item in diagnostic.non_qualifying)
    assert all(
        item["counts_toward_eligibility"] is False
        for item in diagnostic.to_dict()["non_qualifying_evidence"]
    )


def test_naive_as_of_is_rejected() -> None:
    readiness = build_readiness_report(ledger_from_raw_json([]), as_of=MOMENT)

    with pytest.raises(ValueError, match="必须包含时区"):
        build_gap_diagnostic(dataclasses.replace(readiness, as_of=datetime(2026, 9, 22, 12, 0)))


def test_thresholds_and_contract_references_reuse_existing_sources() -> None:
    diagnostic = diagnostic_for([])

    assert dict(diagnostic.thresholds) == {
        "author_min_eligible_records": MIN_AUTHOR_SAMPLES,
        "news_min_eligible_records": MIN_NEWS_EVENTS,
        "news_min_history_days": MIN_NEWS_HISTORY_DAYS,
        "news_max_source_share": MAX_NEWS_SOURCE_SHARE,
    }
    for name in (*AUTHORIZATION_CONTRACT_FIELDS, *AVAILABILITY_CONTRACT_FIELDS):
        assert field_by_name(name) is not None, name
    assert field_by_name("published_at") is not None
    assert field_by_name("collected_at") is not None
    assert field_by_name("effective_at") is None  # 派生字段：不是新增输入列
    assert set(TIME_CONTRACT_FIELDS) == {"published_at", "collected_at", "effective_at"}
    assert set(required_field_names(EvidenceScope.AUTHOR)) >= {
        "author_name",
        "external_account_id",
    }


def test_repeated_build_is_deterministic() -> None:
    first = diagnostic_for(compliant_author_entries(3))
    second = diagnostic_for(compliant_author_entries(3))

    assert first == second
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_thresholds_met_still_gate_blocked_and_human_only() -> None:
    """量化门槛全达标：仍必须 BLOCKED，且只剩人工核验（绝不自动 qualified）。"""
    entries = [*compliant_author_entries(), *compliant_news_entries()]
    diagnostic = diagnostic_for(entries)

    assert diagnostic.readiness is ReadinessClass.GATE_BLOCKED
    assert diagnostic.gate_blocked is True
    assert diagnostic.data_qualification_passed is False
    assert diagnostic.phase_transition_allowed is False
    assert diagnostic.advance_allowed is False
    assert diagnostic.l3_l4_auto_advance_allowed is False
    assert diagnostic.open_evidence_gap_count == 0
    for key in ("author.sample_size", "news.sample_size", "news.coverage", "news.source_share"):
        item = diagnostic.item(key)
        assert item.readiness is ReadinessClass.HUMAN_VERIFICATION_REQUIRED
        assert item.missing_fields == ()
    assert diagnostic.step("provide_authorized_evidence").verification is (
        StepVerification.MACHINE_CONFIRMED
    )
    assert diagnostic.step("reach_quantified_thresholds").verification is (
        StepVerification.MACHINE_CONFIRMED
    )
    assert diagnostic.step("verify_authorization_validity").verification is (
        StepVerification.HUMAN_ONLY
    )
    assert diagnostic.step("l3_human_gate_decision").verification is StepVerification.HUMAN_ONLY


def test_not_oos_eligible_records_are_evidence_missing() -> None:
    entries = [
        evidence_block(EvidenceScope.AUTHOR.value, record_id="a-0001", oos=False, available_at=None)
    ]
    diagnostic = diagnostic_for(entries)

    availability = diagnostic.item("author.availability_oos")
    assert availability.readiness is ReadinessClass.EVIDENCE_MISSING
    assert availability.missing_fields == AVAILABILITY_CONTRACT_FIELDS
    assert availability.missing_count == 1
    assert "AVAILABILITY_UNPROVEN" in availability.machine_fact
    assert diagnostic.item("author.sample_size").missing_count == MIN_AUTHOR_SAMPLES
    assert diagnostic.count(ReadinessClass.HUMAN_VERIFICATION_REQUIRED) > 0


def test_load_gap_diagnostic_rejects_naive_as_of() -> None:
    with pytest.raises(ValueError, match="必须包含时区"):
        load_gap_diagnostic(_EmptySession(), as_of=datetime(2026, 9, 22, 12, 0))


def test_missing_inbox_dir_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InboxDirError):
        load_gap_diagnostic(_EmptySession(), as_of=MOMENT, inbox_dir=tmp_path / "absent")


def test_load_gap_diagnostic_is_read_only_over_fake_session(tmp_path: Path) -> None:
    build_package(tmp_path)

    diagnostic = load_gap_diagnostic(_EmptySession(), as_of=MOMENT, inbox_dir=tmp_path)

    assert diagnostic.manifest.scanned is True
    assert diagnostic.manifest.preflight_pass_count == 1
    assert diagnostic.open_evidence_gap_count > 0
    assert diagnostic.readiness is ReadinessClass.GATE_BLOCKED
    assert diagnostic.data_qualification_passed is False


def test_manifest_facts_report_declared_and_missing_fields(tmp_path: Path) -> None:
    build_package(tmp_path)

    section = manifest_section(tmp_path)

    assert section.scanned is True
    assert len(section.facts) == 1
    fact = section.facts[0]
    assert fact.scope == "news"
    assert fact.status == "PREFLIGHT_PASS"
    assert fact.synthetic is False
    assert set(fact.declared_fields) == set(MANIFEST_REQUIRED_FIELDS)
    assert fact.missing_fields == ()
    assert fact.to_dict()["qualifies"] is False  # 候选 ≠ 授权已核验 ≠ 资格通过


def test_missing_declaration_is_reported_not_filled(tmp_path: Path) -> None:
    build_package(tmp_path, availability_semantics="", historical_oos_applicable=None)

    fact = manifest_section(tmp_path).facts[0]

    assert "availability_semantics" in fact.missing_fields
    assert "historical_oos_applicable" in fact.missing_fields
    assert "availability_semantics" not in fact.declared_fields
    assert fact.availability_semantics == ""
    assert fact.historical_oos_applicable is None


def test_synthetic_candidate_never_qualifies(tmp_path: Path) -> None:
    build_package(tmp_path, synthetic=True)

    section = manifest_section(tmp_path)
    diagnostic = diagnostic_for([], manifest=section)

    assert section.synthetic_count == 1
    assert section.facts[0].synthetic is True
    assert diagnostic.manifest.synthetic_count == 1
    by_kind = {item.kind: item for item in diagnostic.non_qualifying}
    assert by_kind["local_manifest_synthetic"].detected_count == 1
    assert by_kind["local_manifest_synthetic"].counts_toward_eligibility is False
    assert diagnostic.step("exclude_synthetic_evidence").verification is (
        StepVerification.MACHINE_UNSATISFIED
    )
    assert diagnostic.item("news.sample_size").readiness is ReadinessClass.EVIDENCE_MISSING
    assert diagnostic.readiness is ReadinessClass.GATE_BLOCKED
    assert diagnostic.data_qualification_passed is False


def test_manifest_authorization_declaration_requires_human_verification(tmp_path: Path) -> None:
    build_package(tmp_path)

    diagnostic = diagnostic_for([], manifest=manifest_section(tmp_path))

    authorization = diagnostic.item("news.authorization")
    assert authorization.readiness is ReadinessClass.HUMAN_VERIFICATION_REQUIRED
    assert authorization.missing_fields == ()
    assert diagnostic.readiness is ReadinessClass.GATE_BLOCKED
    assert diagnostic.data_qualification_passed is False


def test_manifest_declared_time_semantics_is_quoted_but_not_evidence(tmp_path: Path) -> None:
    build_package(tmp_path, time_semantics="provider_export_iso8601_with_tz")

    item = diagnostic_for([], manifest=manifest_section(tmp_path)).item("news.time_semantics")

    assert item.readiness is ReadinessClass.EVIDENCE_MISSING
    assert item.missing_fields == ()
    assert "provider_export_iso8601_with_tz" in item.machine_fact


def test_manifest_mtime_is_never_used_as_evidence_time(tmp_path: Path) -> None:
    package = build_package(tmp_path)
    stamp = datetime(2019, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    for path in package.rglob("*"):
        os.utime(path, (stamp, stamp))

    diagnostic = diagnostic_for([], manifest=manifest_section(tmp_path))
    payload = json.dumps(diagnostic.to_dict(), ensure_ascii=False, sort_keys=True)

    assert "2019-01-02" not in payload
    assert str(int(stamp)) not in payload
    assert set(_TIMESTAMP_RE.findall(payload)) == {diagnostic.as_of.isoformat()}


def test_unknown_evidence_type_is_not_attributed_to_a_scope(tmp_path: Path) -> None:
    package = build_package(tmp_path)
    manifest_path = package / MANIFEST_FILE_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["evidence_type"] = "blog"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    section = manifest_section(tmp_path)

    assert section.facts[0].scope == "unknown"
    assert "evidence_type" not in section.facts[0].missing_fields
    assert section.for_scope(EvidenceScope.NEWS) == ()


def test_module_source_has_no_network_or_write_paths() -> None:
    """源码级守卫：只允许既有模块导入，且无网络 / 写库 / 文件时间调用。"""
    assert imported_modules(MODULE_PATH) <= ALLOWED_IMPORTS
    assert called_names(MODULE_PATH) & FORBIDDEN_CALLS == set()


def test_cli_source_has_no_network_or_write_paths() -> None:
    allowed = ALLOWED_IMPORTS | {
        "argparse",
        "json",
        "sys",
        "collections.abc",
        "database.session",
        "scripts._console",
        "scripts.evidence_readiness",
        "src.evidence.readiness_watch",
        "src.monitoring.evidence_gap_diagnostic",
    }

    assert imported_modules(CLI_PATH) <= allowed
    assert called_names(CLI_PATH) & FORBIDDEN_CALLS == set()


def test_render_markdown_keeps_blocker_and_no_fabricated_time() -> None:
    diagnostic = diagnostic_for([])

    text = render_gap_diagnostic_markdown(diagnostic)

    assert "# PHASE3_3_DATA 证据缺口诊断" in text
    assert "GATE_BLOCKED" in text
    assert "EVIDENCE_MISSING" in text
    assert "不解除" in text
    assert "advance_allowed=false" in text
    assert "l3_l4_auto_advance_allowed=false" in text
    assert str(diagnostic.open_evidence_gap_count) in text
    assert set(_TIMESTAMP_RE.findall(text)) == {diagnostic.as_of.isoformat()}


def test_render_markdown_includes_manifest_and_hides_content(tmp_path: Path) -> None:
    build_package(tmp_path)

    text = render_gap_diagnostic_markdown(
        diagnostic_for([], manifest=manifest_section(tmp_path))
    )

    assert "本地候选 manifest 事实" in text
    assert "未提供候选目录" not in text
    assert "亚洲盘金价" not in text  # 绝不回显证据正文



