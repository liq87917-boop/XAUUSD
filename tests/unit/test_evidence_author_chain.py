"""GOLD-007 gateway-only 作者归属链的边界单元测试（零数据库、零网络）。

覆盖：
- 普通 CSV（`import_real_posts.py` 口径）/ 历史样本 / Mock 没有证据块 → **恒非候选**；
- 只有 `evidence-intake-v1` + `scope=author` + `oos_eligible=true` + 作者身份齐全才算候选；
- 证据字段脱敏（来源名里的凭据不进入候选）；
- 报告 JSON 结构稳定且问题清单 / 说明都脱敏。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from src.evidence import EVIDENCE_CONTRACT_VERSION
from src.evidence.author_chain import (
    AUTHOR_CHAIN_SCHEMA_VERSION,
    AuthorChainReport,
    gateway_author_evidence,
)

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"

REPORT_KEYS = {
    "schema_version",
    "scope",
    "contract_version",
    "as_of",
    "dry_run",
    "candidates",
    "attributed",
    "duplicate",
    "skipped",
    "identity_conflicts",
    "authors_created",
    "accounts_created",
    "problems",
    "notes",
}


def _envelope(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "scope": "author",
        "source": "manual-evidence-author",
        "source_record_id": "post-0001",
        "fingerprint": "f" * 64,
        "author_name": "作者甲",
        "external_account_id": "acct-001",
        "available_at": "2026-09-18T09:05:00+00:00",
        "oos_eligible": True,
    }
    payload.update(overrides)
    return payload


def test_plain_csv_import_cannot_enter_author_chain() -> None:
    """`import_real_posts.py` 的普通 CSV 载荷没有证据块 → 无法绕过 gateway。"""
    raw = {
        "import_kind": "manual_real_corpus",
        "author_name": "作者甲",
        "language": "zh",
        "collected_at_provenance": "input",
    }
    assert gateway_author_evidence(raw) is None
    assert gateway_author_evidence({}) is None
    assert gateway_author_evidence(None) is None
    assert gateway_author_evidence({"evidence": None}) is None
    assert gateway_author_evidence({"evidence": "not-a-mapping"}) is None


def test_only_contract_author_oos_evidence_is_a_candidate() -> None:
    assert gateway_author_evidence({"evidence": _envelope()}) is not None
    assert (
        gateway_author_evidence({"evidence": _envelope(contract_version="evidence-intake-v0")})
        is None
    )
    assert gateway_author_evidence({"evidence": _envelope(scope="news")}) is None
    assert gateway_author_evidence({"evidence": _envelope(oos_eligible=False)}) is None
    assert gateway_author_evidence({"evidence": _envelope(oos_eligible=None)}) is None
    assert gateway_author_evidence({"evidence": _envelope(external_account_id="")}) is None
    assert gateway_author_evidence({"evidence": _envelope(author_name="")}) is None
    assert gateway_author_evidence({"evidence": _envelope(source="")}) is None
    assert gateway_author_evidence({"evidence": _envelope(source_record_id="")}) is None


def test_candidate_fields_are_redacted() -> None:
    parsed = gateway_author_evidence(
        {"evidence": _envelope(source=f"manual token={SECRET}")}
    )
    assert parsed is not None
    assert SECRET not in parsed.source
    assert "token=***" in parsed.source
    assert parsed.author_name == "作者甲"
    assert parsed.external_account_id == "acct-001"
    assert parsed.available_at == "2026-09-18T09:05:00+00:00"


def test_author_chain_report_json_is_stable_and_sanitized() -> None:
    report = AuthorChainReport(
        as_of=MOMENT,
        dry_run=True,
        candidates=1,
        attributed=1,
        duplicate=0,
        skipped=0,
        identity_conflicts=0,
        authors_created=1,
        accounts_created=1,
        problems=(f"账号 {SECRET} 已归属其他作者：身份冲突",),
        notes=("默认 dry-run",),
    )
    payload = report.to_dict()
    assert set(payload) == REPORT_KEYS
    assert payload["schema_version"] == AUTHOR_CHAIN_SCHEMA_VERSION
    assert payload["scope"] == "author"
    assert payload["contract_version"] == EVIDENCE_CONTRACT_VERSION
    assert payload["dry_run"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    text = report.render()
    assert SECRET not in serialized
    assert SECRET not in text
    assert "gateway-only" in text
    assert "dry-run" in text
