"""证据入口台账（**只读**）：统计写入原始层、经 ``evidence-intake-v1`` 认证的记录。

为什么单独一个模块：

- GOLD-004 的资格观测层需要**机器可读**地知道"库内到底有多少条真正经证据入口认证、
  且具备独立历史可用证据的记录"，而不是只看总条数；
- 台账**只读**：只 SELECT ``raw_items``，不写库、不改写任何历史事实；
- 台账**不解除** blocker：它只报告事实；``PHASE3_3_DATA`` 的解除条件仍是
  真实授权数据实际达标 + 人工 Gate（见 ``src/monitoring/phase33_qualification.py``）；
- 只有带 ``raw_json["evidence"]["contract_version"] == evidence-intake-v1`` 的记录才计入，
  普通导入 / 历史 CSV / Mock 数据不会被误算成可信证据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import RawItem
from database.models.enums import RawItemType
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope

__all__ = [
    "EvidenceLedger",
    "ScopeLedger",
    "ledger_from_raw_json",
    "load_evidence_ledger",
]


def _parse_time(value: Any) -> datetime | None:
    """解析证据块里的 ISO8601 字符串（不合法一律 ``None``，绝不猜）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ScopeLedger:
    """单个 scope 的台账计数（``certified`` ⊇ ``oos_eligible``）。"""

    scope: str
    certified_records: int
    oos_eligible_records: int
    not_oos_eligible_records: int
    evidence_start: datetime | None = None
    evidence_end: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "certified_records": self.certified_records,
            "oos_eligible_records": self.oos_eligible_records,
            "not_oos_eligible_records": self.not_oos_eligible_records,
            "evidence_start": self.evidence_start.isoformat() if self.evidence_start else None,
            "evidence_end": self.evidence_end.isoformat() if self.evidence_end else None,
        }


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    """Author / News 两个 scope 的台账（两个 scope **始终存在**，缺失即 0）。"""

    contract_version: str
    scopes: tuple[ScopeLedger, ...]

    def scope(self, scope: EvidenceScope | str) -> ScopeLedger:
        name = scope.value if isinstance(scope, EvidenceScope) else str(scope)
        for item in self.scopes:
            if item.scope == name:
                return item
        return ScopeLedger(name, 0, 0, 0)

    @classmethod
    def empty(cls, *, contract_version: str = EVIDENCE_CONTRACT_VERSION) -> EvidenceLedger:
        return cls(
            contract_version=contract_version,
            scopes=tuple(
                ScopeLedger(scope.value, 0, 0, 0) for scope in EvidenceScope
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "scopes": [item.to_dict() for item in self.scopes],
        }


def ledger_from_raw_json(entries: Iterable[tuple[Any, Any]]) -> EvidenceLedger:
    """从 ``(raw_json, effective_at)`` 序列汇总台账（纯函数，可离线重放）。"""
    certified: dict[str, int] = {scope.value: 0 for scope in EvidenceScope}
    eligible: dict[str, int] = {scope.value: 0 for scope in EvidenceScope}
    unproven: dict[str, int] = {scope.value: 0 for scope in EvidenceScope}
    windows: dict[str, list[datetime]] = {scope.value: [] for scope in EvidenceScope}
    for raw_json, _effective_at in entries:
        if not isinstance(raw_json, Mapping):
            continue
        evidence = raw_json.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        if str(evidence.get("contract_version") or "").strip() != EVIDENCE_CONTRACT_VERSION:
            continue
        scope_name = str(evidence.get("scope") or "").strip().lower()
        if scope_name not in certified:
            continue
        certified[scope_name] += 1
        if evidence.get("oos_eligible") is True:
            eligible[scope_name] += 1
            available = _parse_time(evidence.get("available_at"))
            if available is not None:
                windows[scope_name].append(available)
        else:
            unproven[scope_name] += 1
    scopes: list[ScopeLedger] = []
    for scope in EvidenceScope:
        values = sorted(windows[scope.value])
        scopes.append(
            ScopeLedger(
                scope=scope.value,
                certified_records=certified[scope.value],
                oos_eligible_records=eligible[scope.value],
                not_oos_eligible_records=unproven[scope.value],
                evidence_start=values[0] if values else None,
                evidence_end=values[-1] if values else None,
            )
        )
    return EvidenceLedger(
        contract_version=EVIDENCE_CONTRACT_VERSION,
        scopes=tuple(scopes),
    )


def load_evidence_ledger(session: Session) -> EvidenceLedger:
    """读取库内证据台账（**只读**；只扫描 ``POST`` / ``NEWS`` 类型的原始记录）。"""
    entries = session.execute(
        sa.select(RawItem.raw_json, RawItem.effective_at).where(
            RawItem.item_type.in_([RawItemType.POST, RawItemType.NEWS])
        )
    ).all()
    return ledger_from_raw_json((raw_json, effective_at) for raw_json, effective_at in entries)
