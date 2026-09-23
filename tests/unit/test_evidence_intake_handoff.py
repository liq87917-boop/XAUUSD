"""GOLD-027 人工证据 Intake Handoff 契约与只读预检单元测试（纯本地 / 零数据库 / 零网络）。

覆盖：

- 版本化 handoff schema 只**引用** ``evidence-intake-v1`` 契约字段（不新增列、不新增阈值）；
- 五种状态（``MISSING`` / ``PRESENT_UNVERIFIED`` / ``HUMAN_VERIFICATION_REQUIRED`` /
  ``NON_QUALIFYING`` / ``GATE_BLOCKED``）的确定性判定；
- **结构完整 ≠ 资格通过**：``preflight_pass`` 与安全布尔恒为独立事实；
- 缺失授权证明 / 缺失独立时间语义 / 缺独立可用证据（OOS）一律 fail-closed；
- Mock / 模板 / 示例 **永远** ``NON_QUALIFYING``；
- 绝不伪造时间：报告中除 ``as_of`` 外没有任何时间戳，候选文件 mtime 绝不出现；
- 只读：不修改候选包内任何文件（内容与 mtime 全部不变）；
- 源码级守卫：无网络 / 无写库 / 无 ``open`` / 无 ``os.stat`` / 无 ``getmtime`` / 无 ``utime``。
"""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope, field_by_name
from src.evidence.handoff import thresholds
from src.evidence.inbox import MANIFEST_FILE_NAME, scan_inbox
from src.evidence.intake_handoff import (
    INTAKE_HANDOFF_CONTRACT_VERSION,
    INTAKE_HANDOFF_KIND,
    INTAKE_HANDOFF_SCHEMA_VERSION,
    INTAKE_STATUS_ORDER,
    MATERIAL_SPECS,
    PREFLIGHT_PASS_SEMANTICS,
    STATUS_MEANINGS,
    TIME_SEMANTICS_REQUIREMENTS,
    IntakeStatus,
    build_intake_handoff,
    intake_handoff_schema,
    load_intake_handoff,
    render_intake_handoff_markdown,
    unknown_contract_fields,
)
from src.monitoring.evidence_gap_diagnostic import (
    GAP_CATEGORIES,
    build_manifest_diagnostic,
)
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "evidence" / "intake_handoff.py"
CLI_PATH = REPO_ROOT / "scripts" / "evidence_intake_handoff.py"

#: 证据文件 mtime 被刻意设成远古时间：任何"用 mtime 当业务时间"的实现都会立刻暴露
FROZEN_MTIME = datetime(2019, 1, 2, 3, 4, 5, tzinfo=UTC)
_MTIME_STAMP = "2019-01-02"
#: ``as_of`` 之外的 ISO 时间戳（用于"绝不伪造时间"断言）
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")

#: handoff 模块允许的导入（单一事实源：既有 evidence / monitoring / 标准库）
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "pathlib",
        "typing",
        "src.common.redaction",
        "src.evidence.contracts",
        "src.evidence.handoff",
        "src.evidence.inbox",
        "src.monitoring.evidence_gap_diagnostic",
        "src.monitoring.phase33_qualification",
    }
)
#: 模块 / CLI 源码中**禁止**出现的调用名（网络 / 采集 / 写库 / 文件时间 / 直接写文件）。
#: 属性调用统一加 ``.`` 前缀（例如 ``.run``），因此 ``set.add`` 这类无害调用不会误报；
#: 数据库写入（``commit`` / ``add`` / ``delete``）由"不得 import database / sqlalchemy"的门禁覆盖。
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
        "write_text",
        "request",
    }
)
#: 属性形式同样禁止的调用名
FORBIDDEN_ATTR_CALLS = frozenset(
    {
        "getmtime",
        "utime",
        "system",
        "popen",
        "Popen",
        "urlopen",
        "connect",
        "intake_evidence",
        "write_text",
        "request",
    }
)
#: CLI 允许的导入（多出 CLI 专用入口；仍**不含**数据库 / 网络 / 采集）
ALLOWED_CLI_IMPORTS = frozenset(
    {
        *ALLOWED_IMPORTS,
        "argparse",
        "json",
        "sys",
        "scripts._console",
        "scripts.evidence_readiness",
        "src.evidence.intake_handoff",
        "src.evidence.readiness_watch",
    }
)


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


def author_row(record_id: str = "a-0001") -> dict[str, Any]:
    """一条**完全合规**的本地 Author 证据行（含作者身份字段）。"""
    row = news_row(record_id)
    row.update(
        {
            "source": "evidence-source-author",
            "author_name": "作者甲",
            "external_account_id": "acct-0001",
        }
    )
    return row


def write_package(
    root: Path,
    *,
    name: str = "pkg-news-01",
    scope: str = EvidenceScope.NEWS.value,
    rows: list[dict[str, Any]] | None = None,
    manifest_drop: str | None = None,
    manifest_extra: dict[str, Any] | None = None,
    write_manifest: bool = True,
) -> Path:
    """在 ``root`` 下建一个**本地**候选包（manifest.json + 单级证据文件；零网络）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    payload = rows if rows is not None else [news_row()]
    evidence = package / "evidence.jsonl"
    evidence.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in payload) + "\n",
        encoding="utf-8",
    )
    os.utime(evidence, (FROZEN_MTIME.timestamp(), FROZEN_MTIME.timestamp()))
    if not write_manifest:
        return package
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": scope,
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
    if manifest_drop is not None:
        manifest.pop(manifest_drop, None)
    if manifest_extra is not None:
        manifest.update(manifest_extra)
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def load(root: Path) -> Any:
    """只读预检 ``root``（零数据库、零网络）。"""
    return load_intake_handoff(root, as_of=MOMENT)


def payload_of(root: Path) -> dict[str, Any]:
    """只读预检并返回稳定 JSON 载荷。"""
    return json.loads(json.dumps(load(root).to_dict(), ensure_ascii=False, sort_keys=True))



# ---- 1. 版本化 handoff 契约（只引用既有证据契约与阈值）----------------------


def test_schema_is_versioned_and_reuses_existing_contract() -> None:
    schema = intake_handoff_schema()

    assert schema["kind"] == INTAKE_HANDOFF_KIND
    assert schema["schema_version"] == INTAKE_HANDOFF_SCHEMA_VERSION == 1
    assert schema["contract_version"] == INTAKE_HANDOFF_CONTRACT_VERSION
    assert schema["contract_version"] == EVIDENCE_CONTRACT_VERSION
    assert schema["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert schema["package_layout"]["manifest_file_name"] == MANIFEST_FILE_NAME


def test_schema_materials_only_reference_contract_fields() -> None:
    schema = intake_handoff_schema()

    assert unknown_contract_fields() == ()
    assert [spec.key for spec in MATERIAL_SPECS] == [item["key"] for item in schema["materials"]]
    for spec in MATERIAL_SPECS:
        assert spec.category in GAP_CATEGORIES
        assert spec.scope in {"both", EvidenceScope.AUTHOR.value}
        for name in spec.contract_fields:
            assert field_by_name(name) is not None, name
        for name in spec.reference_fields:
            assert name in spec.contract_fields


def test_schema_declares_author_and_news_requirements() -> None:
    schema = intake_handoff_schema()
    material_keys = {item["key"] for item in schema["materials"]}

    assert {
        "source_identity",
        "authorization_declaration",
        "time_semantics",
        "published_at",
        "collected_at",
        "effective_at",
        "availability_oos",
        "author_identity",
        "phase33_data_gate",
    } <= material_keys
    author = schema["scope_contract_fields"][EvidenceScope.AUTHOR.value]
    news = schema["scope_contract_fields"][EvidenceScope.NEWS.value]
    assert "author_name" in author["required_contract_fields"]
    assert author["author_only_contract_fields"] == ["author_name", "external_account_id"]
    assert "author_name" not in news["required_contract_fields"]
    assert news["author_only_contract_fields"] == []


def test_schema_statuses_and_time_semantics_are_declared() -> None:
    schema = intake_handoff_schema()

    assert schema["statuses"] == [status.value for status in INTAKE_STATUS_ORDER]
    assert set(schema["status_meanings"]) == set(schema["statuses"])
    assert schema["preflight_pass_semantics"] == PREFLIGHT_PASS_SEMANTICS
    assert "结构完整" in schema["preflight_pass_semantics"]
    assert "evidence_qualified" in schema["preflight_pass_does_not_imply"]
    assert any(
        PHASE3_3_BLOCKER_CODE in item for item in schema["preflight_pass_does_not_imply"]
    )
    assert schema["time_semantics_requirements"] == list(TIME_SEMANTICS_REQUIREMENTS)
    joined = " ".join(schema["time_semantics_requirements"])
    for token in ("published_at", "collected_at", "available_at", "mtime", "NON_QUALIFYING"):
        assert token in joined
    assert schema["gap_categories"] == list(GAP_CATEGORIES)


def test_schema_thresholds_only_reference_existing_gate() -> None:
    schema = intake_handoff_schema()
    existing = thresholds()

    assert schema["thresholds"] == {name: value for name, value in sorted(existing.items())}
    assert schema["thresholds"] == dict(existing)
    assert "thresholds" in json.dumps(schema, ensure_ascii=False)


def test_status_meanings_cover_every_status() -> None:
    assert set(STATUS_MEANINGS) == {status.value for status in INTAKE_STATUS_ORDER}
    assert all(STATUS_MEANINGS[status.value] for status in INTAKE_STATUS_ORDER)
    assert "不计入真实资格" in STATUS_MEANINGS[IntakeStatus.NON_QUALIFYING.value]
    assert "人工" in STATUS_MEANINGS[IntakeStatus.GATE_BLOCKED.value]


def test_unknown_contract_fields_is_empty_and_detects_drift() -> None:
    """机器可检查断言：handoff 只引用既有契约字段（健康树为空）。"""
    assert unknown_contract_fields() == ()
    known = {name for spec in MATERIAL_SPECS for name in spec.contract_fields}
    assert known
    assert all(field_by_name(name) is not None for name in known)



# ---- 2. 状态判定与 fail-closed ----------------------------------------------


def test_missing_scan_reports_no_packages() -> None:
    document = build_intake_handoff(build_manifest_diagnostic(None), as_of=MOMENT)

    assert document.scanned is False
    assert document.package_count == 0
    assert document.preflight_pass_count == 0
    assert document.incomplete_count == 0
    assert document.open_material_gap_count == 0
    assert document.evidence_qualified is False
    assert set(dict(document.status_counts)) == {status.value for status in INTAKE_STATUS_ORDER}


def test_empty_inbox_reports_no_packages(tmp_path: Path) -> None:
    document = load(tmp_path)

    assert document.scanned is True
    assert document.package_count == 0
    assert document.preflight_pass_count == 0
    assert document.evidence_qualified is False
    assert document.blocker_active is True and document.gate_blocked is True


def test_clean_package_is_structurally_complete_but_not_qualified(tmp_path: Path) -> None:
    write_package(tmp_path)

    document = load(tmp_path)
    package = document.packages[0]

    assert package.scope == EvidenceScope.NEWS.value
    assert [item.key for item in package.materials] == [
        spec.key for spec in MATERIAL_SPECS if spec.scope == "both"
    ]
    statuses = {item.key: item.status for item in package.materials}
    assert statuses["phase33_data_gate"] is IntakeStatus.GATE_BLOCKED
    assert {
        key: status for key, status in statuses.items() if key != "phase33_data_gate"
    } == {
        key: IntakeStatus.HUMAN_VERIFICATION_REQUIRED
        for key in statuses
        if key != "phase33_data_gate"
    }
    assert package.preflight_pass is True
    assert package.evidence_qualified is False
    assert package.gate_blocked is True
    assert document.preflight_pass_count == 1
    assert document.incomplete_count == 0
    assert document.synthetic_count == 0
    # 文档级安全布尔：全部**硬编码**，结构完整也绝不改变
    assert document.blocker_code == PHASE3_3_BLOCKER_CODE
    assert document.blocker_active is True
    assert document.human_gate_required is True
    assert document.gate_blocked is True
    assert document.evidence_qualified is False
    assert document.data_qualification_passed is False
    assert document.phase_transition_allowed is False
    assert document.advance_allowed is False
    assert document.l3_l4_auto_advance_allowed is False


def test_preflight_pass_explicitly_excludes_qualification(tmp_path: Path) -> None:
    write_package(tmp_path)

    payload = payload_of(tmp_path)
    package = payload["packages"][0]

    assert package["preflight_pass"] is True
    assert package["evidence_qualified"] is False
    assert package["gate_blocked"] is True
    assert package["preflight_pass_meaning"] == PREFLIGHT_PASS_SEMANTICS
    assert "结构完整" in package["preflight_pass_meaning"]
    assert "evidence_qualified" in payload["schema"]["preflight_pass_does_not_imply"]
    assert all(
        item["counts_toward_eligibility"] is False for item in package["materials"]
    )
    assert package["human_verification_materials"]


def test_author_package_evaluates_author_identity(tmp_path: Path) -> None:
    write_package(
        tmp_path,
        name="pkg-author",
        scope=EvidenceScope.AUTHOR.value,
        rows=[author_row()],
    )

    package = load(tmp_path).packages[0]

    assert package.scope == EvidenceScope.AUTHOR.value
    assert [item.key for item in package.materials] == [spec.key for spec in MATERIAL_SPECS]
    assert package.status_of("author_identity") is IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    assert package.material("evidence_records").present_fields == ("files",)
    assert package.preflight_pass is True


def test_news_package_has_no_author_only_material(tmp_path: Path) -> None:
    write_package(tmp_path)

    package = load(tmp_path).packages[0]

    with pytest.raises(KeyError):
        package.material("author_identity")
    with pytest.raises(KeyError):
        package.material("not_a_material")


def test_missing_authorization_proof_fails_closed(tmp_path: Path) -> None:
    write_package(tmp_path, manifest_drop="authorization_reference")

    document = load(tmp_path)
    package = document.packages[0]
    material = package.material("authorization_declaration")

    assert material.status is IntakeStatus.MISSING
    assert "authorization_reference" in material.missing_fields
    assert package.preflight_pass is False
    assert package.evidence_qualified is False
    assert document.preflight_pass_count == 0
    assert document.incomplete_count == 1
    assert any("inbox 预检未通过" in note for note in package.notes)
    assert package.status_of("phase33_data_gate") is IntakeStatus.GATE_BLOCKED


def test_rows_without_authorization_evidence_are_present_unverified(tmp_path: Path) -> None:
    row = news_row()
    del row["authorization_status"]
    write_package(tmp_path, rows=[row])

    package = load(tmp_path).packages[0]
    material = package.material("authorization_declaration")

    # 授权引用已声明（present），但行级机械校验未通过 → 不能进入人工核验队列
    assert material.status is IntakeStatus.PRESENT_UNVERIFIED
    assert "authorization_status" in material.missing_fields
    assert package.preflight_pass is False



def test_missing_independent_time_semantics_fails_closed(tmp_path: Path) -> None:
    write_package(tmp_path, manifest_drop="time_semantics")

    package = load(tmp_path).packages[0]

    assert package.status_of("time_semantics") is IntakeStatus.MISSING
    assert package.material("time_semantics").missing_fields == ("time_semantics",)
    # 数据行本身完好：绝不因为缺声明就伪造时间，也不把行字段判成缺失
    assert package.status_of("published_at") is IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    assert package.material("published_at").machine_fact
    assert package.preflight_pass is False


def test_rows_without_independent_availability_are_present_unverified(tmp_path: Path) -> None:
    row = news_row()
    del row["available_at"]
    write_package(tmp_path, rows=[row])

    document = load(tmp_path)
    package = document.packages[0]
    material = package.material("availability_oos")

    assert material.status is IntakeStatus.PRESENT_UNVERIFIED
    assert material.missing_fields == ("available_at",)
    assert package.preflight_pass is False
    assert document.count(IntakeStatus.PRESENT_UNVERIFIED) == 1
    assert "缺独立可用时间的行 1 条" in material.machine_fact


def test_missing_availability_declaration_fails_closed(tmp_path: Path) -> None:
    write_package(tmp_path, manifest_drop="availability_semantics")

    package = load(tmp_path).packages[0]
    material = package.material("availability_oos")

    assert material.status is IntakeStatus.MISSING
    assert set(material.missing_fields) == {"availability_semantics"}
    assert package.preflight_pass is False


def test_oos_not_declared_is_present_unverified(tmp_path: Path) -> None:
    write_package(tmp_path, manifest_extra={"historical_oos_applicable": False})

    package = load(tmp_path).packages[0]
    material = package.material("availability_oos")

    assert material.status is IntakeStatus.PRESENT_UNVERIFIED
    assert "historical_oos_applicable" in material.missing_fields
    assert package.preflight_pass is False
    assert package.evidence_qualified is False


def test_mock_or_template_rows_are_always_non_qualifying(tmp_path: Path) -> None:
    write_package(tmp_path, rows=[news_row(synthetic=True)])

    document = load(tmp_path)
    package = document.packages[0]

    assert package.synthetic is True
    assert package.preflight_pass is False
    assert package.evidence_qualified is False
    expected_non_qualifying = {
        spec.key
        for spec in MATERIAL_SPECS
        if spec.scope == "both" and spec.key != "phase33_data_gate"
    }
    assert set(package.non_qualifying_materials) == expected_non_qualifying
    assert package.status_of("phase33_data_gate") is IntakeStatus.GATE_BLOCKED
    assert all(item.missing_fields == () for item in package.materials)
    assert all(item.human_next_step for item in package.materials)
    assert document.synthetic_count == 1
    assert document.evidence_qualified is False


def test_manifest_level_mock_marker_is_non_qualifying(tmp_path: Path) -> None:
    write_package(tmp_path, manifest_extra={"is_mock": True})

    package = load(tmp_path).packages[0]

    assert package.synthetic is True
    assert package.preflight_pass is False
    assert package.non_qualifying_materials


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    write_package(tmp_path, write_manifest=False)

    document = load(tmp_path)
    package = document.packages[0]

    assert package.inbox_status == "QUARANTINED"
    assert package.preflight_pass is False
    assert set(package.missing_materials) >= {
        "evidence_records",
        "source_identity",
        "authorization_declaration",
        "time_semantics",
    }
    assert package.status_of("phase33_data_gate") is IntakeStatus.GATE_BLOCKED
    assert document.incomplete_count == 1
    assert document.status_counts



# ---- 3. 计数 / 确定性 / 只读 / 脱敏 ------------------------------------------


def test_status_counts_and_lookup_helpers(tmp_path: Path) -> None:
    write_package(tmp_path, name="pkg-clean")
    write_package(tmp_path, name="pkg-mock", rows=[news_row("n-0002", synthetic=True)])
    write_package(tmp_path, name="pkg-no-auth", manifest_drop="authorization_reference")

    document = load(tmp_path)

    assert document.package_count == 3
    assert document.preflight_pass_count == 1
    assert document.incomplete_count == 2
    counts = dict(document.status_counts)
    assert set(counts) == {status.value for status in INTAKE_STATUS_ORDER}
    assert counts["GATE_BLOCKED"] == 3
    assert document.count(IntakeStatus.MISSING) == 1
    assert document.count(IntakeStatus.NON_QUALIFYING) == 8
    assert document.open_material_gap_count == 1
    total = sum(count for _name, count in document.status_counts)
    assert total == sum(len(package.materials) for package in document.packages)

    fingerprints = sorted(package.fingerprint for package in document.packages)
    assert len(set(fingerprints)) == 3
    assert document.package(fingerprints[0]).fingerprint == fingerprints[0]
    with pytest.raises(KeyError):
        document.package("0" * 64)


def test_never_fabricates_timestamps_or_leaks_mtime(tmp_path: Path) -> None:
    row = news_row()
    del row["available_at"]
    write_package(tmp_path, rows=[row])

    payload = json.dumps(payload_of(tmp_path), ensure_ascii=False, sort_keys=True)

    assert set(_TIMESTAMP_RE.findall(payload)) == {MOMENT.isoformat()}
    assert _MTIME_STAMP not in payload
    assert FROZEN_MTIME.isoformat() not in payload


def test_as_of_must_be_timezone_aware(tmp_path: Path) -> None:
    write_package(tmp_path)
    naive = datetime(2026, 9, 23, 0, 0)

    with pytest.raises(ValueError, match="必须包含时区"):
        load_intake_handoff(tmp_path, as_of=naive)
    manifest = build_manifest_diagnostic(scan_inbox(tmp_path, moment=MOMENT))
    with pytest.raises(ValueError, match="必须包含时区"):
        build_intake_handoff(manifest, as_of=naive)


def test_repeated_load_is_byte_stable(tmp_path: Path) -> None:
    write_package(tmp_path)

    first = json.dumps(load(tmp_path).to_dict(), ensure_ascii=False, sort_keys=True)
    second = json.dumps(load(tmp_path).to_dict(), ensure_ascii=False, sort_keys=True)

    assert first == second


def test_load_is_read_only_for_evidence_files(tmp_path: Path) -> None:
    package = write_package(tmp_path)
    evidence = package / "evidence.jsonl"
    manifest = package / MANIFEST_FILE_NAME
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (evidence, manifest)
    }
    listing = sorted(path.name for path in package.iterdir())

    load(tmp_path)

    for path in (evidence, manifest):
        assert path.read_bytes() == before[path.name][0]
        assert path.stat().st_mtime_ns == before[path.name][1]
    assert sorted(path.name for path in package.iterdir()) == listing


def test_markdown_keeps_blocker_without_echoing_evidence(tmp_path: Path) -> None:
    write_package(tmp_path)

    document = load(tmp_path)
    text = render_intake_handoff_markdown(document)

    assert "PHASE3_3_DATA" in text
    assert "GATE_BLOCKED" in text
    assert "结构完整" in text
    assert "evidence_qualified=false" in text
    assert PREFLIGHT_PASS_SEMANTICS in text
    for spec in MATERIAL_SPECS:
        assert spec.key in text
    # 绝不回显候选证据正文 / mtime
    assert "亚洲盘金价自 2400 回落至 2385" not in text
    assert _MTIME_STAMP not in text


def test_markdown_for_empty_inbox_stays_honest(tmp_path: Path) -> None:
    text = render_intake_handoff_markdown(load(tmp_path))

    assert "没有候选包" in text
    assert "evidence_qualified=false" in text
    assert "l3_l4_auto_advance_allowed=false" in text



# ---- 4. 源码级守卫（无网络 / 无写库 / 无文件时间 / 无直接写文件）-------------


def _called_names(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """源码中出现过的调用名：(``Name(...)``, ``Attribute(...)``)，后者不带点。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plain: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            plain.add(func.id)
        elif isinstance(func, ast.Attribute):
            attributes.add(func.attr)
    return frozenset(plain), frozenset(attributes)


def _imported_modules(path: Path) -> set[str]:
    """源码中出现过的导入模块名（``import`` / ``from ... import`` 两类）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_module_and_cli_avoid_network_write_and_file_time_calls() -> None:
    for path in (MODULE_PATH, CLI_PATH):
        plain, attributes = _called_names(path)
        assert not (plain & FORBIDDEN_CALLS), path.name
        assert not (attributes & FORBIDDEN_ATTR_CALLS), path.name


def test_module_and_cli_only_import_existing_readonly_capabilities() -> None:
    assert _imported_modules(MODULE_PATH) <= ALLOWED_IMPORTS
    assert _imported_modules(CLI_PATH) <= ALLOWED_CLI_IMPORTS
    for path in (MODULE_PATH, CLI_PATH):
        modules = _imported_modules(path)
        assert not any(module.startswith("database") for module in modules)
        assert not any(module.startswith("sqlalchemy") for module in modules)
        assert not any(module.startswith("requests") for module in modules)


def test_safety_booleans_are_hardcoded_in_source() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    for token in (
        "data_qualification_passed=False",
        "phase_transition_allowed=False",
        "advance_allowed=False",
        "l3_l4_auto_advance_allowed=False",
    ):
        assert token in source
    assert source.count("evidence_qualified=False,") >= 2
    assert source.count("gate_blocked=True,") >= 2
    assert '"counts_toward_eligibility": False,' in source
    assert "intake_evidence" not in source

