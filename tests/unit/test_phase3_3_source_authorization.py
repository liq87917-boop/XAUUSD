from datetime import UTC, datetime

from src.alpha.source_authorization import validate_source_authorizations


def _row() -> dict[str, str]:
    return {
        "source": "licensed-source",
        "external_account_id": "analyst-1",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "docs/legal/vendor-license.pdf",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
        "reviewed_by": "owner",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }


def test_approves_current_three_permission_authorization() -> None:
    result = validate_source_authorizations([_row()], now=datetime(2026, 2, 1, tzinfo=UTC))
    assert result.ready
    assert result.approved_accounts == frozenset({("licensed-source", "analyst-1")})
    assert not result.errors


def test_rejects_missing_permission_and_expired_authorization() -> None:
    row = _row()
    row["permits_research_use"] = "false"
    row["expires_at"] = "2026-01-15T00:00:00Z"
    result = validate_source_authorizations([row], now=datetime(2026, 2, 1, tzinfo=UTC))
    assert not result.ready
    assert any("未同时许可" in item for item in result.errors)
    assert any("授权已过期" in item for item in result.errors)


def test_rejects_unverifiable_authorization_reference() -> None:
    row = _row()
    row["authorization_reference"] = "trust-me"
    result = validate_source_authorizations([row], now=datetime(2026, 2, 1, tzinfo=UTC))
    assert not result.ready
    assert any("docs/legal" in item for item in result.errors)


def test_pending_and_empty_authorizations_remain_blocked() -> None:
    row = _row()
    row["authorization_status"] = "PENDING"
    pending = validate_source_authorizations([row], now=datetime(2026, 2, 1, tzinfo=UTC))
    empty = validate_source_authorizations([], now=datetime(2026, 2, 1, tzinfo=UTC))
    assert not pending.ready
    assert pending.warnings
    assert not empty.ready
    assert empty.errors == ("授权表没有数据行",)
