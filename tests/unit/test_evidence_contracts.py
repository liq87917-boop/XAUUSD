"""Evidence Intake 版本化契约单元测试（GOLD-005；纯函数，不触库、不联网）。"""

from __future__ import annotations

from src.evidence.contracts import (
    ALLOWED_AUTHORIZATION_BASES,
    AUTHOR_ONLY_FIELDS,
    COMMON_REQUIRED_FIELDS,
    ELIGIBILITY_REASON_CODES,
    EVIDENCE_CONTRACT_VERSION,
    EVIDENCE_FIELDS,
    EVIDENCE_SCHEMA_VERSION,
    PERMISSION_FIELDS,
    QUARANTINE_REASON_CODES,
    SYSTEM_ASSIGNED_FIELDS,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    alias_map,
    canonical_field_names,
    field_by_name,
    required_field_names,
)


def test_contract_version_and_scopes_are_stable() -> None:
    assert EVIDENCE_CONTRACT_VERSION == "evidence-intake-v1"
    assert EVIDENCE_SCHEMA_VERSION == 1
    assert [scope.value for scope in EvidenceScope] == ["author", "news"]
    assert EvidenceScope.AUTHOR.item_type == "POST"
    assert EvidenceScope.NEWS.item_type == "NEWS"
    assert EvidenceScope.AUTHOR.label == "作者"
    assert EvidenceScope.NEWS.label == "新闻"


def test_required_fields_cover_identity_time_provenance_and_authorization() -> None:
    common = required_field_names(EvidenceScope.NEWS)
    author = required_field_names(EvidenceScope.AUTHOR)
    assert set(author) - set(common) == set(AUTHOR_ONLY_FIELDS)
    for name in (
        "source",
        "source_record_id",
        "published_at",
        "collected_at",
        "provenance_reference",
        "authorization_status",
        "authorization_basis",
        "authorization_reference",
        "authorization_reviewed_by",
        "authorization_reviewed_at",
    ):
        assert name in common
    for name in PERMISSION_FIELDS:
        assert name in common
    # available_at 不是必填：缺它只影响 OOS 资格（NOT_OOS_ELIGIBLE），不隔离
    assert "available_at" not in common
    assert set(COMMON_REQUIRED_FIELDS) <= set(common)


def test_ingested_at_is_system_assigned_not_required() -> None:
    assert SYSTEM_ASSIGNED_FIELDS == ("ingested_at", "fingerprint")
    assert "ingested_at" in canonical_field_names()
    field = field_by_name("ingested_at")
    assert field is not None
    assert field.required_for == frozenset()
    assert "系统赋值" in field.description


def test_reason_codes_split_between_quarantine_and_eligibility() -> None:
    assert not (QUARANTINE_REASON_CODES & ELIGIBILITY_REASON_CODES)
    for code in (
        ReasonCode.AUTHORIZATION_MISSING,
        ReasonCode.PROVENANCE_MISSING,
        ReasonCode.PUBLISHED_AT_INVALID,
        ReasonCode.COLLECTED_AT_INVALID,
        ReasonCode.CONTENT_EMPTY,
        ReasonCode.IDENTITY_CONFLICT,
        ReasonCode.SENSITIVE_VALUE_DETECTED,
        ReasonCode.ROW_UNREADABLE,
    ):
        assert code in QUARANTINE_REASON_CODES
    assert ReasonCode.DUPLICATE not in QUARANTINE_REASON_CODES
    assert ReasonCode.AVAILABILITY_UNPROVEN in ELIGIBILITY_REASON_CODES
    statuses = [status.value for status in RowStatus]
    assert statuses == ["ACCEPTED", "QUARANTINED", "DUPLICATE"]


def test_alias_map_matches_existing_corpus_column_names() -> None:
    aliases = alias_map()
    assert aliases["text_content"] == "content"
    assert aliases["post_id"] == "source_record_id"
    assert aliases["source_name"] == "source"
    assert aliases["author"] == "author_name"
    assert aliases["account_id"] == "external_account_id"
    assert aliases["published"] == "published_at"
    assert aliases["source_url"] == "url"
    # 别名不得覆盖规范名（规范列优先由 normalize_input_row 保证）
    assert "content" not in aliases
    assert set(aliases.values()) <= set(canonical_field_names())


def test_authorization_bases_reuse_alpha_vocabulary() -> None:
    expected = {"official_api", "license_agreement", "written_permission", "user_owned"}
    assert set(ALLOWED_AUTHORIZATION_BASES) == expected
    assert all(field.description for field in EVIDENCE_FIELDS)
