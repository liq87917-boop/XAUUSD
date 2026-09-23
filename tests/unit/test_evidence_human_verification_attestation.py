"""GOLD-028 材料级人工核验凭证单元测试（纯本地 / 零数据库 / 零网络 / 零证据改写）。

覆盖：

- 版本化凭证契约只**引用** GOLD-027 材料契约与既有阈值（不新增第二套材料 / 门槛）；
- 正向：结构完整的真实候选包 + 全部材料 ``VERIFIED`` → ``all_required_verified=true``，
  但 ``evidence_qualified`` / ``data_qualification_passed`` / ``phase_transition_allowed`` /
  ``l3_l4_auto_advance_allowed`` / ``advance_allowed`` **恒为 false**、
  ``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` **恒为 true**；
- fail-closed：缺授权证明 / 时间语义未核验 / availability-OOS 未核验 / Mock-模板 / 缺材料决策 /
  REJECTED / NEEDS_CHANGES **一律**不能让凭证达标；
- 漂移：package fingerprint / handoff 内容身份 / scope 不一致 → 拒绝；写盘后内容漂移 → verify 失效；
- 篡改 / 修订：凭证被改写（决策、计数、安全字段）→ ``verify`` fail-closed；同 id 幂等、
  否则必须显式 ``revision`` + ``supersedes``；
- 敏感值 / 时间语义：疑似凭据一律拒绝；``reviewed_at`` naive / 未来一律拒绝；
- 只读：候选证据内容与 mtime 零变化；除 ``attested_at`` / ``generated_at`` / 人工 ``reviewed_at``
  外没有任何时间戳，文件 mtime 绝不出现；
- 源码级守卫：无网络 / 无写库 / 无 ``open`` / 无 ``os.stat`` / 无 ``getmtime`` / 无 ``utime``。
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.human_verification_attestation import (
    ALL_REQUIRED_VERIFIED_SEMANTICS,
    ATTESTATION_CONTRACT_VERSION,
    ATTESTATION_KIND,
    ATTESTATION_SCHEMA_VERSION,
    REQUIRED_VERIFICATION_CATEGORIES,
    VERIFICATION_DECISIONS,
    AttestationNotAttestableError,
    AttestationStateError,
    HumanVerificationAttestation,
    attestable_material_keys,
    attestation_schema,
    build_attestation,
    handoff_content_sha256,
    load_attestation_document,
    load_verification_input,
    run_attestation,
    verify_attestation,
)
from src.evidence.inbox import MANIFEST_FILE_NAME
from src.evidence.intake_handoff import MATERIAL_SPECS, load_intake_handoff
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
MOMENT_ISO = "2026-09-23T00:00:00+00:00"
REVIEWED_AT = "2026-09-22T00:00:00+00:00"
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "evidence" / "human_verification_attestation.py"
CLI_PATH = REPO_ROOT / "scripts" / "evidence_human_verification_attestation.py"

#: 证据文件 mtime 被刻意设成远古时间：任何"用 mtime 当业务时间"的实现都会立刻暴露
FROZEN_MTIME = datetime(2019, 1, 2, 3, 4, 5, tzinfo=UTC)
_MTIME_STAMP = "2019-01-02"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")
#: 允许出现在凭证里的时间戳（人工核验动作时间 + 审计时点；**绝无**证据时间）
ALLOWED_TIMESTAMPS = frozenset(
    {MOMENT_ISO, "2026-09-23T00:00:00+00:00", REVIEWED_AT, "2026-09-22T00:00:00+00:00"}
)

#: 模块允许的导入（单一事实源：既有 evidence / monitoring / 标准库）
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "pathlib",
        "re",
        "typing",
        "src.common.hashing",
        "src.common.redaction",
        "src.evidence.contracts",
        "src.evidence.inbox",
        "src.evidence.intake_handoff",
        "src.evidence.readiness_runner",
        "src.evidence.readiness_watch",
        "src.monitoring.phase33_qualification",
    }
)
#: 模块 / CLI 源码中**禁止**出现的调用名（网络 / 采集 / 写库 / 文件时间 / 直接写文件）
FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "getmtime",
        "utime",
        "stat",
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
        "sys",
        "scripts._console",
        "scripts.evidence_readiness",
        "src.evidence.human_verification_attestation",
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
    (package / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package



def handoff_of(root: Path) -> Any:
    """只读预检 ``root``（GOLD-027；零数据库、零网络）。"""
    return load_intake_handoff(root, as_of=MOMENT)


def material_keys_of(package: Any) -> tuple[str, ...]:
    """候选包中**可人工核验**的材料 key（Gate 材料不参与材料级核验）。"""
    return tuple(item.key for item in package.materials if item.category != "gate")


def verification_doc(
    package: Any,
    *,
    handoff: Any | None = None,
    drop: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一份**人工核验输入**（默认全部材料 ``VERIFIED``）。"""
    keys = material_keys_of(package)
    materials = [
        {
            "material": key,
            "decision": "VERIFIED",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": "operator-li",
            "reviewed_at": REVIEWED_AT,
            "evidence_reference": "https://vendor.example/terms",
            "note": "人工核验完成",
        }
        for key in keys
        if key != drop
    ]
    doc: dict[str, Any] = {
        "schema_version": 1,
        "package_fingerprint": package.fingerprint,
        "scope": package.scope,
        "reviewer": "operator-li",
        "materials": materials,
    }
    if handoff is not None:
        doc["handoff_content_sha256"] = handoff_content_sha256(handoff)
    if overrides:
        doc.update(overrides)
    return doc


def write_json(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    """把核验输入 / 篡改版凭证写到临时目录（测试用；零网络）。"""
    target = tmp_path / name
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def clean_case(tmp_path: Path, *, scope: str = EvidenceScope.NEWS.value) -> tuple[Path, Any, Path]:
    """建一个**结构完整**的候选包并返回 ``(inbox, package, verification_path)``。"""
    inbox = tmp_path / "inbox"
    rows = [author_row()] if scope == EvidenceScope.AUTHOR.value else [news_row()]
    write_package(inbox, scope=scope, rows=rows)
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    path = write_json(
        tmp_path, "verification.json", verification_doc(package, handoff=handoff)
    )
    return inbox, package, path


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """候选包内所有文件的（内容 + mtime）快照：证明**零证据改写**。"""
    return {
        item.name: (hashing.sha256_bytes(item.read_bytes()), item.stat().st_mtime_ns)
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def payload_of(attestation: HumanVerificationAttestation) -> dict[str, Any]:
    """凭证的稳定 JSON 载荷（往返序列化，模拟真实落盘）。"""
    return json.loads(json.dumps(attestation.to_dict(), ensure_ascii=False, sort_keys=True))



# ---- 1. 版本化凭证契约（只引用既有 GOLD-027 材料契约与阈值）--------------------


def test_schema_is_versioned_and_reuses_gold_027_materials() -> None:
    schema = attestation_schema()
    assert schema["kind"] == ATTESTATION_KIND
    assert schema["schema_version"] == ATTESTATION_SCHEMA_VERSION
    assert schema["contract_version"] == ATTESTATION_CONTRACT_VERSION
    assert schema["blocker_code"] == PHASE3_3_BLOCKER_CODE
    keys = [item["key"] for item in schema["materials"]]
    assert keys == list(attestable_material_keys())
    # 只**引用** GOLD-027 材料契约：Gate 材料不在可核验清单里，其余一一对应
    assert "phase33_data_gate" not in keys
    assert keys == [spec.key for spec in MATERIAL_SPECS if spec.category != "gate"]


def test_schema_declares_required_categories_and_decisions() -> None:
    schema = attestation_schema()
    categories = {item["category"] for item in schema["materials"]}
    for required in REQUIRED_VERIFICATION_CATEGORIES:
        assert required in categories, required
    assert schema["required_categories"] == list(REQUIRED_VERIFICATION_CATEGORIES)
    assert schema["decisions"] == list(VERIFICATION_DECISIONS)
    assert set(schema["decisions"]) == {"VERIFIED", "REJECTED", "NEEDS_CHANGES"}


def test_schema_states_all_required_verified_is_not_qualification() -> None:
    schema = attestation_schema()
    assert schema["all_required_verified_semantics"] == ALL_REQUIRED_VERIFIED_SEMANTICS
    assert "evidence_qualified" in schema["all_required_verified_does_not_imply"]
    assert "data_qualification_passed" in schema["all_required_verified_does_not_imply"]
    assert f"{PHASE3_3_BLOCKER_CODE}_unblocked" in schema["all_required_verified_does_not_imply"]
    assert "phase_transition_allowed" in schema["all_required_verified_does_not_imply"]
    assert schema["all_required_verified_semantics"].startswith("`all_required_verified=true`")
    assert schema["bound_to"]["package_fingerprint"]


def test_schema_binds_current_package_handoff_and_scope() -> None:
    schema = attestation_schema()
    bound = schema["bound_to"]
    for key in (
        "package_fingerprint",
        "package_content_sha256",
        "handoff_content_sha256",
        "handoff_schema_version",
        "scope",
    ):
        assert bound.get(key), key


# ---- 2. 正向：结构完整的真实包 + 全部材料 VERIFIED ---------------------------


def test_clean_package_all_required_verified_but_never_qualified(tmp_path: Path) -> None:
    inbox, package, verification = clean_case(tmp_path)

    attestation = run_attestation(inbox, verification_path=verification, moment=MOMENT)

    assert attestation.all_required_verified is True
    # 材料级人工核验 ≠ 资格 / Gate：硬编码安全字段恒为诚实取值
    assert attestation.evidence_qualified is False
    assert attestation.data_qualification_passed is False
    assert attestation.phase_transition_allowed is False
    assert attestation.l3_l4_auto_advance_allowed is False
    assert attestation.advance_allowed is False
    assert attestation.blocker_active is True
    assert attestation.human_gate_required is True
    assert attestation.gate_blocked is True
    assert attestation.blocker_code == PHASE3_3_BLOCKER_CODE
    assert attestation.required_material_count == len(material_keys_of(package))
    assert attestation.verified_material_count == attestation.required_material_count
    assert attestation.codes == ()
    assert attestation.package_fingerprint == package.fingerprint
    assert attestation.preflight_pass is True
    assert attestation.synthetic is False


def test_author_scope_includes_author_identity_material(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path, scope=EvidenceScope.AUTHOR.value)

    attestation = run_attestation(inbox, verification_path=verification, moment=MOMENT)

    assert "author_identity" in {item.material for item in attestation.materials}
    assert attestation.all_required_verified is True


def test_attestation_id_is_content_addressed_and_stable(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    first = run_attestation(inbox, verification_path=verification, moment=MOMENT)
    second = run_attestation(inbox, verification_path=verification, moment=MOMENT)

    assert first.attestation_id == second.attestation_id
    assert len(first.attestation_id) == 64
    assert first.attestation_id == first.attestation_id.lower()
    # 换一个审计时点**不**改变内容级凭证 id（id 只由绑定 + 人工核验派生）
    other = run_attestation(
        inbox,
        verification_path=verification,
        moment=datetime(2026, 9, 23, 6, 0, tzinfo=UTC),
    )
    assert other.attestation_id == first.attestation_id



# ---- 3. fail-closed：关键材料缺失 / 未核验一律不能达标 ------------------------


def test_missing_authorization_proof_cannot_be_verified(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    write_package(inbox, manifest_drop="authorization_reference")
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    authorization = package.material("authorization_declaration")
    assert authorization.status.value != "HUMAN_VERIFICATION_REQUIRED"

    # ① 谎报 VERIFIED → 直接 fail-closed（绝不产生凭证）
    lying = write_json(
        tmp_path, "lying.json", verification_doc(package, handoff=handoff, drop="time_semantics")
    )
    with pytest.raises(AttestationNotAttestableError) as excinfo:
        run_attestation(inbox, verification_path=lying, moment=MOMENT)
    assert "MATERIAL_NOT_VERIFIABLE" in str(excinfo.value)

    # ② 不做任何核验声明 → 凭证明确不达标 + 稳定原因码（绝不把结构完整当资格）
    no_claims = verification_doc(package, handoff=handoff, drop="authorization_declaration")
    no_claims["materials"] = []
    attestation = run_attestation(
        inbox, verification_path=write_json(tmp_path, "no-claims.json", no_claims), moment=MOMENT
    )
    assert attestation.all_required_verified is False
    assert "MATERIAL_DECISION_MISSING" in attestation.codes
    assert "PREFLIGHT_NOT_PASSED" in attestation.codes
    assert attestation.material("authorization_declaration").counts_as_verified is False
    assert attestation.material("authorization_declaration").intake_status == "MISSING"


def test_missing_independent_time_semantics_cannot_be_verified(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    write_package(inbox, manifest_drop="time_semantics")
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    assert package.material("time_semantics").status.value != "HUMAN_VERIFICATION_REQUIRED"

    lying = write_json(tmp_path, "lying.json", verification_doc(package, handoff=handoff))
    with pytest.raises(AttestationNotAttestableError) as excinfo:
        run_attestation(inbox, verification_path=lying, moment=MOMENT)
    assert "MATERIAL_NOT_VERIFIABLE" in str(excinfo.value)

    # ② 不做任何核验声明 → 明确不达标 + 稳定原因码
    no_claims = verification_doc(package, handoff=handoff)
    no_claims["materials"] = []
    attestation = run_attestation(
        inbox, verification_path=write_json(tmp_path, "no-time.json", no_claims), moment=MOMENT
    )
    assert attestation.all_required_verified is False
    assert "MATERIAL_DECISION_MISSING" in attestation.codes
    assert attestation.material("time_semantics").counts_as_verified is False
    assert attestation.material("time_semantics").intake_status == "MISSING"


def test_rows_without_independent_availability_cannot_be_verified(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    row = news_row()
    row.pop("available_at")
    write_package(inbox, rows=[row])
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    assert package.material("availability_oos").status.value == "PRESENT_UNVERIFIED"

    lying = write_json(tmp_path, "lying.json", verification_doc(package, handoff=handoff))
    with pytest.raises(AttestationNotAttestableError) as excinfo:
        run_attestation(inbox, verification_path=lying, moment=MOMENT)
    assert "MATERIAL_NOT_VERIFIABLE" in str(excinfo.value)


def test_missing_availability_declaration_cannot_be_verified(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    write_package(inbox, manifest_drop="availability_semantics")
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    assert package.material("availability_oos").status.value != "HUMAN_VERIFICATION_REQUIRED"

    with pytest.raises(AttestationNotAttestableError):
        run_attestation(
            inbox,
            verification_path=write_json(
                tmp_path, "lying.json", verification_doc(package, handoff=handoff)
            ),
            moment=MOMENT,
        )


def test_mock_or_template_rows_are_always_non_qualifying(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    write_package(inbox, rows=[news_row(synthetic=True)])
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    assert package.synthetic is True

    with pytest.raises(AttestationNotAttestableError) as excinfo:
        run_attestation(
            inbox,
            verification_path=write_json(
                tmp_path, "lying.json", verification_doc(package, handoff=handoff)
            ),
            moment=MOMENT,
        )
    assert "NON_QUALIFYING_PACKAGE" in str(excinfo.value)

    # 不谎报也不能达标：所有材料恒不计入核验，并给出非资格原因码
    empty = {"schema_version": 1, "package_fingerprint": package.fingerprint, "materials": []}
    attestation = run_attestation(
        inbox, verification_path=write_json(tmp_path, "mock.json", empty), moment=MOMENT
    )
    assert attestation.all_required_verified is False
    assert "NON_QUALIFYING_PACKAGE" in attestation.codes
    assert attestation.synthetic is True
    assert attestation.verified_material_count == 0


def test_reject_and_needs_changes_never_count_as_verified(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    for decision in ("REJECTED", "NEEDS_CHANGES"):
        doc = verification_doc(package, handoff=handoff)
        doc["materials"][0]["decision"] = decision
        attestation = run_attestation(
            inbox,
            verification_path=write_json(tmp_path, f"{decision}.json", doc),
            moment=MOMENT,
        )
        assert attestation.all_required_verified is False
        assert attestation.material(doc["materials"][0]["material"]).decision == decision
        assert attestation.material(doc["materials"][0]["material"]).counts_as_verified is False
        assert f"MATERIAL_{'REJECTED' if decision == 'REJECTED' else 'NEEDS_CHANGES'}" in (
            attestation.codes
        )



# ---- 4. 漂移：package / handoff / scope 不一致一律 fail-closed ----------------


def test_package_fingerprint_drift_in_input_is_rejected(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    doc = verification_doc(package, handoff=handoff, overrides={"package_fingerprint": "a" * 64})

    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "drift-fp.json", doc), moment=MOMENT
        )
    assert "PACKAGE_FINGERPRINT_MISMATCH" in str(excinfo.value)


def test_handoff_content_drift_in_input_is_rejected(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    doc = verification_doc(package, handoff=handoff, overrides={"handoff_content_sha256": "b" * 64})

    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "drift-hh.json", doc), moment=MOMENT
        )
    assert "HANDOFF_CONTENT_MISMATCH" in str(excinfo.value)


def test_scope_mismatch_is_rejected(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    doc = verification_doc(package, handoff=handoff, overrides={"scope": "author"})

    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "drift-scope.json", doc), moment=MOMENT
        )
    assert "SCOPE_MISMATCH" in str(excinfo.value)


def test_written_attestation_detects_package_content_drift(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    out = tmp_path / "reports" / "attestation.json"
    written = run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    assert written.written_path is not None

    before = verify_attestation(out, inbox, moment=MOMENT)
    assert before.verified is True
    assert before.codes == ()

    # 候选证据内容被替换 → package fingerprint 漂移（旧凭证必须失效，绝不静默继承）
    (inbox / "pkg-news-01" / "evidence.jsonl").write_text(
        json.dumps(news_row("n-0009"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    after = verify_attestation(out, inbox, moment=MOMENT)

    assert after.verified is False
    assert "PACKAGE_FINGERPRINT_DRIFT" in after.codes
    assert "HANDOFF_CONTENT_DRIFT" in after.codes
    assert after.package_fingerprint_matches is False
    assert after.handoff_content_matches is False
    assert after.all_required_verified is True  # 凭证**声明**的内容不改写
    assert after.to_dict()["data_qualification_passed"] is False
    assert after.to_dict()["phase_transition_allowed"] is False



# ---- 5. 篡改 / 重复 / 修订语义 ----------------------------------------------


def test_verify_detects_tampered_decisions(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    out = tmp_path / "reports" / "attestation.json"
    run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["materials"][0]["decision"] = "REJECTED"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AttestationStateError) as excinfo:
        verify_attestation(out, inbox, moment=MOMENT)
    assert "ATTESTATION_TAMPERED" in str(excinfo.value)


def test_verify_detects_weakened_safety_fields(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    out = tmp_path / "reports" / "attestation.json"
    run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["blocker_active"] = False
    payload["data_qualification_passed"] = True
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AttestationStateError) as excinfo:
        verify_attestation(out, inbox, moment=MOMENT)
    assert "ATTESTATION_TAMPERED" in str(excinfo.value)


def test_verify_detects_fabricated_all_required_verified(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    write_package(inbox, manifest_drop="authorization_reference")
    handoff = handoff_of(inbox)
    package = handoff.packages[0]
    doc = verification_doc(package, handoff=handoff)
    doc["materials"] = []
    verification = write_json(tmp_path, "no-claims.json", doc)
    out = tmp_path / "reports" / "attestation.json"
    written = run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    assert written.all_required_verified is False

    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["all_required_verified"] = True
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AttestationStateError) as excinfo:
        verify_attestation(out, inbox, moment=MOMENT)
    assert "ATTESTATION_TAMPERED" in str(excinfo.value)


def test_verify_detects_missing_reference(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    out = tmp_path / "reports" / "attestation.json"
    run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["materials"][0]["evidence_reference"] = ""
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AttestationStateError) as excinfo:
        verify_attestation(out, inbox, moment=MOMENT)
    assert "MATERIAL_REFERENCE_MISSING" in str(excinfo.value)


def test_duplicate_and_revision_semantics(tmp_path: Path) -> None:
    inbox, package, verification = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    out = tmp_path / "reports" / "attestation.json"
    first = run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)

    # 同 id 幂等：允许原地重写（内容同一）
    again = run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    assert again.attestation_id == first.attestation_id

    # 内容不同的凭证绝不静默覆盖
    changed = verification_doc(package, handoff=handoff)
    changed["materials"][0]["decision"] = "REJECTED"
    changed_path = write_json(tmp_path, "changed.json", changed)
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(inbox, verification_path=changed_path, moment=MOMENT, out_path=out)
    assert "ATTESTATION_CONFLICT" in str(excinfo.value)

    # 显式 revision + supersedes 才允许改写（且 supersedes 必须指向当前 id）
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox,
            verification_path=changed_path,
            moment=MOMENT,
            out_path=out,
            revision=2,
            supersedes="c" * 64,
        )
    assert "SUPERSEDES_MISMATCH" in str(excinfo.value)

    second = run_attestation(
        inbox,
        verification_path=changed_path,
        moment=MOMENT,
        out_path=out,
        revision=2,
        supersedes=first.attestation_id,
    )
    assert second.revision == 2
    assert second.supersedes == first.attestation_id
    assert second.all_required_verified is False
    stored = load_attestation_document(out)
    assert stored["revision"] == 2
    assert stored["supersedes"] == first.attestation_id



# ---- 6. 敏感值 / 时间语义 / 输入结构 ----------------------------------------


def test_sensitive_values_are_rejected(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)

    doc = verification_doc(
        package,
        handoff=handoff,
        overrides={"reviewer": "sk_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
    )
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "secret.json", doc), moment=MOMENT
        )
    assert "SENSITIVE_VALUE_REJECTED" in str(excinfo.value)

    doc = verification_doc(package, handoff=handoff)
    doc["materials"][0]["reviewer"] = "bearer_abcdefghijklmnopqrstuvwxyz0123456789"
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "secret2.json", doc), moment=MOMENT
        )
    assert "SENSITIVE_VALUE_REJECTED" in str(excinfo.value)


def test_evidence_reference_must_be_independent(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    for bad in ("evidence.jsonl", "http://vendor.example/terms", "file:///c:/terms.pdf"):
        doc = verification_doc(package, handoff=handoff)
        doc["materials"][0]["evidence_reference"] = bad
        with pytest.raises(AttestationStateError) as excinfo:
            run_attestation(
                inbox, verification_path=write_json(tmp_path, "ref.json", doc), moment=MOMENT
            )
        assert "EVIDENCE_REFERENCE_UNTRUSTED" in str(excinfo.value)

    # docs/legal/... 路径是可接受的独立引用
    ok = verification_doc(package, handoff=handoff)
    ok["materials"][0]["evidence_reference"] = "docs/legal/vendor-license-2026.md"
    attestation = run_attestation(
        inbox, verification_path=write_json(tmp_path, "ref-ok.json", ok), moment=MOMENT
    )
    assert attestation.all_required_verified is True


def test_naive_and_future_reviewed_at_fail_closed(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)

    naive = verification_doc(package, handoff=handoff)
    naive["materials"][0]["reviewed_at"] = "2026-09-22T00:00:00"
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "naive.json", naive), moment=MOMENT
        )
    assert "REVIEWED_AT_INVALID" in str(excinfo.value)

    future = verification_doc(package, handoff=handoff)
    future["materials"][0]["reviewed_at"] = "2026-09-24T00:00:00+00:00"
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "future.json", future), moment=MOMENT
        )
    assert "REVIEWED_AT_FUTURE" in str(excinfo.value)



def test_unknown_fields_and_duplicate_materials_are_rejected(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)

    unknown = verification_doc(package, handoff=handoff, overrides={"api_key": "x"})
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "unknown.json", unknown), moment=MOMENT
        )
    assert "UNKNOWN_FIELD" in str(excinfo.value)

    dup = verification_doc(package, handoff=handoff)
    dup["materials"].append(copy.deepcopy(dup["materials"][0]))
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "dup.json", dup), moment=MOMENT
        )
    assert "DUPLICATE_MATERIAL" in str(excinfo.value)

    gate = verification_doc(package, handoff=handoff)
    gate["materials"].append(
        {
            "material": "phase33_data_gate",
            "decision": "VERIFIED",
            "reason_code": "HUMAN_REVIEWED",
            "reviewer": "operator-li",
            "reviewed_at": REVIEWED_AT,
            "evidence_reference": "https://vendor.example/terms",
        }
    )
    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "gate.json", gate), moment=MOMENT
        )
    assert "MATERIAL_NOT_VERIFIABLE" in str(excinfo.value)


def test_verification_input_must_pin_current_package(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    doc = verification_doc(package, handoff=handoff)
    doc.pop("package_fingerprint")

    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "no-fp.json", doc), moment=MOMENT
        )
    assert "VERIFICATION_INVALID" in str(excinfo.value)


def test_reason_code_must_be_stable_shape(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    doc = verification_doc(package, handoff=handoff)
    doc["materials"][0]["reason_code"] = "lower-case"

    with pytest.raises(AttestationStateError) as excinfo:
        run_attestation(
            inbox, verification_path=write_json(tmp_path, "reason.json", doc), moment=MOMENT
        )
    assert "REASON_CODE_INVALID" in str(excinfo.value)


def test_missing_verification_file_and_inbox_fail_closed(tmp_path: Path) -> None:
    inbox, _, _ = clean_case(tmp_path)

    with pytest.raises(Exception) as excinfo:
        run_attestation(inbox, verification_path=tmp_path / "nope.json", moment=MOMENT)
    assert "VERIFICATION_NOT_FOUND" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        run_attestation(
            tmp_path / "no-inbox", verification_path=tmp_path / "nope.json", moment=MOMENT
        )
    assert "fail-closed" in str(excinfo.value)


def test_naive_moment_fails_closed(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    with pytest.raises(Exception) as excinfo:
        run_attestation(
            inbox,
            verification_path=verification,
            moment=datetime(2026, 9, 23),  # naive：绝不隐式当 UTC
        )
    assert "时区" in str(excinfo.value)


def test_out_must_stay_outside_inbox(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    with pytest.raises(Exception) as excinfo:
        run_attestation(
            inbox,
            verification_path=verification,
            moment=MOMENT,
            out_path=inbox / "attestation.json",
        )
    assert "ATTESTATION_OVERWRITE_REFUSED" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        run_attestation(
            inbox, verification_path=verification, moment=MOMENT, out_path=verification
        )
    assert "ATTESTATION_OVERWRITE_REFUSED" in str(excinfo.value)



# ---- 7. 只读 / 零证据改写 / 不伪造时间 ---------------------------------------


def test_load_is_read_only_for_evidence_files(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    before = snapshot(inbox)
    listing = sorted(item.name for item in inbox.iterdir())

    attestation = run_attestation(inbox, verification_path=verification, moment=MOMENT)
    out = tmp_path / "reports" / "attestation.json"
    run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)
    verify_attestation(out, inbox, moment=MOMENT)

    assert attestation.all_required_verified is True
    # 历史 evidence 绝不改写：内容与 mtime 一模一样，目录项不变
    assert snapshot(inbox) == before
    assert sorted(item.name for item in inbox.iterdir()) == listing


def test_repeated_load_is_byte_stable(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    first = payload_of(run_attestation(inbox, verification_path=verification, moment=MOMENT))
    second = payload_of(run_attestation(inbox, verification_path=verification, moment=MOMENT))

    assert first == second
    assert first["attestation_id"] == second["attestation_id"]


def _all_keys(payload: object) -> set[str]:
    """递归收集全部键名（用于断言凭证里绝无证据时间字段）。"""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            found |= _all_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _all_keys(item)
    return found


def test_never_fabricates_timestamps_or_leaks_mtime(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    payload = payload_of(run_attestation(inbox, verification_path=verification, moment=MOMENT))
    text = json.dumps(payload, ensure_ascii=False)

    # 候选文件 mtime（2019-01-02）**绝不**出现
    assert _MTIME_STAMP not in text
    # 除审计时点与人工 reviewed_at 外没有任何时间戳
    found = set(_TIMESTAMP_RE.findall(text))
    assert found <= ALLOWED_TIMESTAMPS, found - ALLOWED_TIMESTAMPS
    # 凭证里**没有**任何证据时间字段（只作为材料 key / 要求文本出现，绝不作为时间键）
    keys = _all_keys(payload)
    assert not (keys & {"published_at", "collected_at", "effective_at", "available_at"}), (
        keys & {"published_at", "collected_at", "effective_at", "available_at"}
    )
    assert "reviewed_at" in keys
    assert "attested_at" in keys


def test_evidence_content_is_never_echoed(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)

    payload = payload_of(run_attestation(inbox, verification_path=verification, moment=MOMENT))
    text = json.dumps(payload, ensure_ascii=False)

    assert "亚洲盘金价自 2400 回落至 2385" not in text
    assert "金价短线回落" not in text
    assert "evidence.jsonl" not in text


def test_markdown_summary_keeps_blocker_and_hides_content(tmp_path: Path) -> None:
    inbox, _, verification = clean_case(tmp_path)
    out = tmp_path / "reports" / "attestation.json"
    run_attestation(inbox, verification_path=verification, moment=MOMENT, out_path=out)

    from src.evidence.human_verification_attestation import render_attestation_summary

    attestation = run_attestation(inbox, verification_path=verification, moment=MOMENT)
    text = render_attestation_summary(attestation)

    assert "材料级人工核验凭证" in text
    assert "all_required_verified=true" in text
    assert "evidence_qualified=false" in text
    assert str(PHASE3_3_BLOCKER_CODE) in text
    assert "亚洲盘金价自 2400 回落至 2385" not in text



# ---- 8. 源码级守卫（无网络 / 无写库 / 无文件时间 / 安全布尔硬编码）----------


def _called_names(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """收集源码里被调用的名字（裸名 + 属性名），用于禁止网络 / 写库 / 文件时间调用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bare: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                bare.add(func.id)
            elif isinstance(func, ast.Attribute):
                attrs.add(func.attr)
    return frozenset(bare), frozenset(attrs)


def _imported_modules(path: Path) -> set[str]:
    """收集源码里导入的模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_module_and_cli_avoid_network_write_and_file_time_calls() -> None:
    for path in (MODULE_PATH, CLI_PATH):
        bare, attrs = _called_names(path)
        assert not (bare & FORBIDDEN_CALLS), (path.name, bare & FORBIDDEN_CALLS)
        assert not (attrs & FORBIDDEN_ATTR_CALLS), (path.name, attrs & FORBIDDEN_ATTR_CALLS)
        # 绝不直接建 HTTP / DB 连接对象
        assert not (bare & {"urlopen", "connect", "system", "popen"}), path.name


def test_module_and_cli_only_import_existing_readonly_capabilities() -> None:
    for path in (MODULE_PATH,):
        assert _imported_modules(path) <= ALLOWED_IMPORTS, _imported_modules(path) - ALLOWED_IMPORTS
    assert _imported_modules(CLI_PATH) <= ALLOWED_CLI_IMPORTS, (
        _imported_modules(CLI_PATH) - ALLOWED_CLI_IMPORTS
    )
    # 绝不导入数据库 / 采集器 / 外部模型
    for path in (MODULE_PATH, CLI_PATH):
        modules = _imported_modules(path)
        assert not [name for name in modules if "database" in name or "sqlalchemy" in name]
        assert not [name for name in modules if "collectors" in name]


def test_safety_booleans_are_hardcoded_in_source() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    for fragment in (
        "data_qualification_passed=False",
        "phase_transition_allowed=False",
        "l3_l4_auto_advance_allowed=False",
        "evidence_qualified=False",
        "advance_allowed=False",
        "blocker_active=True",
        "human_gate_required=True",
        "gate_blocked=True",
        '"all_required_verified_is_qualification": False',
    ):
        assert fragment in source, fragment
    # 安全字段绝不由输入 / 上游报告透传
    assert "getattr(package, \"data_qualification_passed\"" not in source
    assert "document.data_qualification_passed" not in source


def test_all_required_verified_never_implies_qualification() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "`all_required_verified=true`" in source
    assert "它**绝不**等于 `evidence_qualified=true`" in source
    schema = attestation_schema()
    assert schema["all_required_verified_semantics"] == ALL_REQUIRED_VERIFIED_SEMANTICS
    assert "evidence_qualified" in schema["all_required_verified_does_not_imply"]
    assert f"{PHASE3_3_BLOCKER_CODE}_unblocked" in schema["all_required_verified_does_not_imply"]
    assert schema["notes"]


def test_build_attestation_is_deterministic_and_pure(tmp_path: Path) -> None:
    inbox, package, _ = clean_case(tmp_path)
    handoff = handoff_of(inbox)
    verification = load_verification_input(
        write_json(tmp_path, "v.json", verification_doc(package, handoff=handoff)), moment=MOMENT
    )

    first = build_attestation(package, handoff, verification, moment=MOMENT)
    second = build_attestation(package, handoff, verification, moment=MOMENT)

    assert first == second
    assert isinstance(first, HumanVerificationAttestation)

