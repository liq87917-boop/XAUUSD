"""Evidence Intake 逐行校验单元测试（GOLD-005；纯函数，不触库、不联网）。

覆盖：授权缺失/未知/拒绝、时间伪造防护、历史 CSV 不具 OOS 资格、
坏行隔离、敏感字段处置、别名列、系统赋值字段忽略。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.evidence.contracts import (
    QUARANTINE_REASON_CODES,
    EvidenceScope,
    ReasonCode,
    RowStatus,
)
from src.evidence.validation import assess_row, normalize_input_row

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def author_row(**overrides: Any) -> dict[str, Any]:
    """一条完全合规的 Author 证据行（含独立历史可用证据）。"""
    row: dict[str, Any] = {
        "source": "manual-example-author",
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


def news_row(**overrides: Any) -> dict[str, Any]:
    """一条完全合规的 News 证据行（无作者身份列，但含可核验内容引用）。"""
    row: dict[str, Any] = {
        "source": "manual-example-news",
        "source_record_id": "news-2026-09-18-001",
        "title": "金价短线回落",
        "content_ref": "https://example.com/archive/news-2026-09-18-001",
        "published_at": "2026-09-18T08:00:00+00:00",
        "collected_at": "2026-09-18T09:00:00+00:00",
        "available_at": "2026-09-18T08:30:00+00:00",
        "availability_provenance": "historical_snapshot",
        "availability_reference": "docs/legal/news-archives/provider-export-2026-09.md",
        "provenance_reference": "https://example.com/news/news-2026-09-18-001",
        "authorization_status": "APPROVED",
        "authorization_basis": "license_agreement",
        "authorization_reference": "https://example.com/terms/news-license",
        "authorization_reviewed_by": "合规复核人-李四",
        "authorization_reviewed_at": "2026-09-18T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def assess(row: dict[str, Any], *, scope: EvidenceScope = EvidenceScope.AUTHOR):
    return assess_row(row, scope=scope, index=0, row_number=1, moment=MOMENT)


def codes(row: dict[str, Any], *, scope: EvidenceScope = EvidenceScope.AUTHOR) -> set[ReasonCode]:
    return set(assess(row, scope=scope).reason_codes)


def test_valid_author_row_is_accepted_and_oos_eligible() -> None:
    result = assess(author_row())
    assert result.status is RowStatus.ACCEPTED
    assert result.quarantined is False
    assert result.reason_codes == ()
    assert result.oos_eligible is True
    assert result.not_oos_eligible_reason is None
    record = result.record
    assert record is not None
    assert record.scope is EvidenceScope.AUTHOR
    assert len(record.fingerprint) == 64
    # effective_at 只按既有语义派生（max(published_at, collected_at)），不引入新信息
    assert record.effective_at == record.collected_at
    assert record.published_at == datetime(2026, 9, 18, 0, 30, tzinfo=UTC)
    assert record.authorization.status == "APPROVED"
    assert record.ignored_columns == ()


def test_missing_authorization_is_never_inferred() -> None:
    for value in ("", "UNKNOWN", "PENDING", "REJECTED"):
        assert ReasonCode.AUTHORIZATION_MISSING in codes(author_row(authorization_status=value))
    assert ReasonCode.AUTHORIZATION_MISSING in codes(author_row(authorization_basis="guess"))
    assert ReasonCode.AUTHORIZATION_MISSING in codes(author_row(permits_research_use="false"))
    assert ReasonCode.AUTHORIZATION_MISSING in codes(author_row(authorization_reviewed_by=""))
    # 引用格式不可核验 → 独立原因码
    assert ReasonCode.AUTHORIZATION_REFERENCE_INVALID in codes(
        author_row(authorization_reference="http://example.com/terms")
    )
    assert ReasonCode.AUTHORIZATION_REFERENCE_INVALID in codes(
        author_row(authorization_reference="docs/other/permit.md")
    )


def test_authorization_window_must_cover_collected_at() -> None:
    # 事后授权不能追认既有采集
    assert ReasonCode.AUTHORIZATION_MISSING in codes(
        author_row(authorization_valid_from="2026-09-20T00:00:00+08:00")
    )
    # 已过期
    assert ReasonCode.AUTHORIZATION_MISSING in codes(
        author_row(authorization_expires_at="2026-09-17T00:00:00+08:00")
    )


def test_provenance_is_required() -> None:
    assert ReasonCode.PROVENANCE_MISSING in codes(author_row(provenance_reference=""))
    assert ReasonCode.PROVENANCE_MISSING in codes(author_row(provenance_reference="some free text"))


def test_published_at_requires_timezone_and_cannot_be_future() -> None:
    assert ReasonCode.PUBLISHED_AT_INVALID in codes(author_row(published_at="2026-09-18T08:30:00"))
    assert ReasonCode.PUBLISHED_AT_INVALID in codes(author_row(published_at="2026-09-18"))
    assert ReasonCode.PUBLISHED_AT_INVALID in codes(author_row(published_at="not-a-time"))
    assert ReasonCode.PUBLISHED_AT_INVALID in codes(
        author_row(published_at="2026-09-23T00:00:00+00:00")
    )


def test_collected_at_must_be_later_than_published_at() -> None:
    assert ReasonCode.COLLECTED_AT_INVALID in codes(
        author_row(collected_at="2026-09-18T08:30:00+08:00")
    )
    assert ReasonCode.COLLECTED_AT_INVALID in codes(
        author_row(collected_at="2026-09-18T07:00:00+08:00")
    )
    assert ReasonCode.COLLECTED_AT_INVALID in codes(author_row(collected_at="2026-09-18T09:10:00"))
    assert ReasonCode.COLLECTED_AT_INVALID in codes(
        author_row(collected_at="2026-09-23T00:00:00+00:00")
    )


def test_missing_content_and_reference_is_quarantined() -> None:
    assert ReasonCode.CONTENT_EMPTY in codes(author_row(content="", content_ref=""))
    # 只给可核验的内容引用也算有效内容
    only_ref = assess(author_row(content="", content_ref="https://example.com/a/1"))
    assert only_ref.status is RowStatus.ACCEPTED
    assert only_ref.record is not None
    assert only_ref.record.content is None
    assert only_ref.record.content_reference == "https://example.com/a/1"


def test_missing_identity_fields_are_quarantined() -> None:
    assert ReasonCode.SOURCE_IDENTITY_MISSING in codes(author_row(source=""))
    assert ReasonCode.SOURCE_RECORD_ID_MISSING in codes(author_row(source_record_id=""))
    assert ReasonCode.SOURCE_IDENTITY_MISSING in codes(author_row(author_name=""))
    assert ReasonCode.SOURCE_IDENTITY_MISSING in codes(author_row(external_account_id=""))


def test_historical_row_without_availability_evidence_is_not_oos_eligible() -> None:
    """普通历史 CSV：发布时间早于采集时间，但没有独立可用时间证据。"""
    result = assess(
        author_row(available_at="", availability_provenance="", availability_reference="")
    )
    assert result.status is RowStatus.ACCEPTED
    assert result.oos_eligible is False
    assert result.not_oos_eligible_reason is ReasonCode.AVAILABILITY_UNPROVEN
    assert ReasonCode.AVAILABILITY_UNPROVEN in result.reason_codes
    assert ReasonCode.AVAILABILITY_UNPROVEN not in QUARANTINE_REASON_CODES


def test_inconsistent_availability_evidence_is_unproven_not_oos_eligible() -> None:
    for overrides in (
        {"availability_provenance": ""},
        {"availability_reference": "not a reference"},
        {"available_at": "2026-09-18T11:00:00+08:00"},  # 晚于 collected_at
        {"available_at": "2026-09-18T07:00:00+08:00"},  # 早于 published_at
        {"available_at": "2026-09-23T00:00:00+00:00"},  # 未来
    ):
        result = assess(author_row(**overrides))
        assert result.status is RowStatus.ACCEPTED
        assert result.oos_eligible is False
        assert result.not_oos_eligible_reason is ReasonCode.AVAILABILITY_UNPROVEN


def test_scope_mismatch_is_quarantined() -> None:
    assert ReasonCode.SCOPE_MISMATCH in codes(author_row(scope="news"))
    assert ReasonCode.SCOPE_MISMATCH not in codes(author_row(scope="author"))


def test_normalize_maps_alias_columns_with_canonical_priority() -> None:
    normalized = normalize_input_row(
        {
            "source_name": "manual-x",
            "post_id": "post-9",
            "author": "作者乙",
            "account_id": "acct-9",
            "text_content": "正文",
            "content": "规范列优先",
            "published": "2026-09-18T08:30:00+08:00",
            "unrelated": "ignored",
        }
    )
    assert normalized.get("source") == "manual-x"
    assert normalized.get("source_record_id") == "post-9"
    assert normalized.get("author_name") == "作者乙"
    assert normalized.get("external_account_id") == "acct-9"
    assert normalized.get("content") == "规范列优先"
    assert normalized.get("published_at") == "2026-09-18T08:30:00+08:00"
    assert normalized.ignored_columns == ("unrelated",)
    assert normalized.sensitive_columns == ()


def test_credentials_in_input_are_quarantined_without_echoing_values() -> None:
    secret = "sk-abcdef0123456789abcdef"
    result = assess(author_row(api_key=secret))
    assert result.status is RowStatus.QUARANTINED
    assert ReasonCode.SENSITIVE_VALUE_DETECTED in result.reason_codes
    joined = " ".join(result.reasons)
    assert secret not in joined
    assert "api_key" in joined  # 只记录列名，不记录值
    # 引用里带凭据同样隔离，且值不出现在原因文本里
    result = assess(author_row(provenance_reference="https://example.com/a?token=VERYSECRET123"))
    assert ReasonCode.SENSITIVE_VALUE_DETECTED in result.reason_codes
    assert "VERYSECRET123" not in " ".join(result.reasons)


def test_system_assigned_ingested_at_from_input_is_ignored() -> None:
    row = author_row()
    row["ingested_at"] = "1999-01-01T00:00:00+00:00"
    result = assess(row)
    assert result.record is not None
    assert result.record.ingested_at == MOMENT  # 系统赋值，输入不得伪造


def test_news_row_without_author_columns_is_accepted() -> None:
    result = assess(news_row(), scope=EvidenceScope.NEWS)
    assert result.status is RowStatus.ACCEPTED
    assert result.oos_eligible is True
    assert result.record is not None
    assert result.record.author_name is None
    assert result.record.content is None
    assert result.record.content_reference is not None


def test_same_row_is_invalid_in_author_scope_when_author_identity_missing() -> None:
    assert ReasonCode.SOURCE_IDENTITY_MISSING in codes(news_row(), scope=EvidenceScope.AUTHOR)


def test_fingerprint_is_deterministic_and_content_sensitive() -> None:
    first = assess(author_row()).fingerprint
    second = assess(author_row()).fingerprint
    assert first == second
    assert first is not None
    changed = assess(author_row(content="完全不同的正文内容")).fingerprint
    assert changed is not None
    assert changed != first
    assert assess(author_row(source="")).fingerprint is None


def test_moment_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="moment"):
        assess_row(
            author_row(),
            scope=EvidenceScope.AUTHOR,
            index=0,
            row_number=1,
            moment=datetime(2026, 9, 22, 12, 0),
        )


def test_identity_fields_are_truncated_to_column_limits() -> None:
    """`sources.name` VARCHAR(100) / `raw_items.source_record_id` VARCHAR(300)。"""
    result = assess(author_row(source="s" * 200, source_record_id="r" * 400))
    assert result.record is not None
    assert len(result.record.source) == 100
    assert len(result.record.source_record_id) == 300
    # 指纹由未截断的原始身份计算，不因列长限制而漂移
    assert result.fingerprint is not None
    assert len(result.fingerprint) == 64



