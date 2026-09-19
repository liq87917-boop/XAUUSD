from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.alpha.source_authorization import validate_source_authorizations


def _row() -> dict[str, str]:
    return {
        "source": "licensed-source",
        "external_account_id": "analyst-1",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://example.invalid/license",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
        "reviewed_by": "owner",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }


def test_approves_current_three_permission_authorization() -> None:
    result = validate_source_authorizations(
        [_row()], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    assert result.ready
    assert result.approved_accounts == frozenset({("licensed-source", "analyst-1")})
    assert result.approved_windows == (
        (
            ("licensed-source", "analyst-1"),
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ),
    )
    assert not result.errors


def test_rejects_missing_permission_and_expired_authorization() -> None:
    row = _row()
    row["permits_research_use"] = "false"
    row["expires_at"] = "2026-01-15T00:00:00Z"
    result = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    assert not result.ready
    assert any("未同时许可" in item for item in result.errors)
    assert any("授权已过期" in item for item in result.errors)


def test_rejects_unverifiable_authorization_reference() -> None:
    row = _row()
    row["authorization_reference"] = "trust-me"
    result = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    assert not result.ready
    assert any("docs/legal" in item for item in result.errors)


def test_rejects_reference_path_traversal_or_malformed_https() -> None:
    for reference in (
        "docs/legal/../outside.txt",
        "docs/legal/../../secret.txt",
        "https://[malformed",
        "https://user:password@example.org/license",
        "http://example.org/license",
    ):
        row = _row()
        row["authorization_reference"] = reference
        result = validate_source_authorizations(
            [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
        )
        assert not result.ready, reference
        assert any("authorization_reference" in item for item in result.errors)


def test_rejects_missing_local_evidence_but_accepts_present_file(tmp_path: Path) -> None:
    row = _row()
    row["authorization_reference"] = "docs/legal/vendor-license.pdf"
    missing = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=tmp_path
    )
    assert not missing.ready
    evidence = tmp_path / "docs" / "legal" / "vendor-license.pdf"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"fixture only")
    present = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=tmp_path
    )
    assert present.ready


def test_pending_and_empty_authorizations_remain_blocked() -> None:
    row = _row()
    row["authorization_status"] = "PENDING"
    pending = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    empty = validate_source_authorizations(
        [], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    assert not pending.ready
    assert pending.warnings
    assert not empty.ready
    assert empty.errors == ("授权表没有数据行",)


def test_rejects_near_future_review_and_naive_audit_clock() -> None:
    row = _row()
    row["reviewed_at"] = "2026-02-01T00:01:00Z"
    result = validate_source_authorizations(
        [row], now=datetime(2026, 2, 1, tzinfo=UTC), evidence_root=Path(".")
    )
    assert any("reviewed_at 是未来时间" in error for error in result.errors)
    with pytest.raises(ValueError, match="now 必须包含时区"):
        validate_source_authorizations([row], now=datetime(2026, 2, 1), evidence_root=Path("."))
