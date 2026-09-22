"""调度核心纯单元测试（敏感字段擦除 / 状态聚合 / 摘要序列化）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database.models.enums import JobStatus
from src.scheduler.core import (
    JOB_TYPE,
    SchedulerRunResult,
    SourceRunSummary,
    aggregate_status,
    default_stale_after,
    redact_secrets,
)
from src.scheduler.slots import resolve_slot

pytestmark = pytest.mark.unit

_MOMENT = datetime(2026, 9, 22, 12, 7, 30, tzinfo=UTC)


def _summary(
    status: str,
    *,
    failed: int = 0,
    error: str | None = None,
    warnings: tuple[str, ...] = (),
) -> SourceRunSummary:
    return SourceRunSummary(
        source_name=f"src-{status.lower()}",
        source_id="00000000-0000-0000-0000-000000000000",
        collector_name="collector",
        status=status,
        failed=failed,
        error=error,
        warnings=warnings,
    )


def test_job_type_constant() -> None:
    assert JOB_TYPE == "collector_scheduler"


def test_default_stale_after_is_three_intervals() -> None:
    assert default_stale_after(30) == timedelta(minutes=90)
    assert default_stale_after(60) == timedelta(minutes=180)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer sk-abcdef123456", "abcdef123456"),
        ("api_key=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ("token: SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ('{"api_key": "SUPERSECRETVALUE"}', "SUPERSECRETVALUE"),
        ("refresh_token=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
        ("secret=SUPERSECRETVALUE", "SUPERSECRETVALUE"),
    ],
)
def test_redact_secrets_removes_credentials(text: str, secret: str) -> None:
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert "***" in redacted


def test_redact_secrets_keeps_plain_text() -> None:
    plain = "connect timeout after 3 attempts"
    assert redact_secrets(plain) == plain


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], JobStatus.SUCCESS),
        (["SUCCESS", "SUCCESS"], JobStatus.SUCCESS),
        (["FAILED"], JobStatus.FAILED),
        (["FAILED", "CONSTRUCTION_FAILED"], JobStatus.FAILED),
        (["SUCCESS", "FAILED"], JobStatus.PARTIAL_FAILED),
        (["SUCCESS", "CONSTRUCTION_FAILED"], JobStatus.PARTIAL_FAILED),
        (["SUCCESS", "PARTIAL_FAILED"], JobStatus.PARTIAL_FAILED),
    ],
)
def test_aggregate_status(statuses: list[str], expected: JobStatus) -> None:
    assert aggregate_status([_summary(status) for status in statuses]) is expected


def test_source_summary_to_dict_uses_whitelist_keys() -> None:
    payload = _summary("SUCCESS", warnings=("低于预期下限",)).to_dict()
    assert set(payload) == {
        "source",
        "source_id",
        "collector",
        "status",
        "run_id",
        "fetched",
        "inserted",
        "duplicate",
        "failed",
        "skipped",
        "retry_count",
        "warnings",
    }
    assert payload["warnings"] == ["低于预期下限"]


def test_source_summary_to_dict_includes_error_only_when_present() -> None:
    assert "error" not in _summary("SUCCESS").to_dict()
    assert _summary("FAILED", error="boom").to_dict()["error"] == "boom"


def test_scheduler_run_result_to_dict_and_render() -> None:
    slot = resolve_slot(_MOMENT, 30)
    result = SchedulerRunResult(
        slot=slot,
        status=JobStatus.SUCCESS,
        executed=True,
        job_run_id=None,
        retry_count=0,
        reason="executed",
        sources=(_summary("SUCCESS"),),
    )
    payload = result.to_dict()
    assert payload["status"] == "SUCCESS"
    assert payload["slot"]["idempotency_key"] == slot.idempotency_key
    assert payload["sources"][0]["status"] == "SUCCESS"
    assert payload["job_run_id"] is None
    assert result.is_failure is False
    assert "executed=True" in result.render()
    assert "status=SUCCESS" in result.render()
