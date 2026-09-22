"""Evidence Intake 的逐行机械校验（纯函数：零 I/O、零网络、确定性）。

校验顺序（对齐 ``docs/10`` 的"宁可不判也不猜"原则）：

```text
① 列映射（规范列优先，别名补空缺；未知列只留列名）
   → ② 必填齐全性（来源身份 / 记录 ID / 内容 / 时间 / 出处 / 授权声明）
   → ③ 授权声明（显式 APPROVED + 白名单依据 + 可核验引用 + 三项许可 + 人工签认）
   → ④ 时间语义（必须带时区；不得未来；collected_at 必须晚于 published_at）
   → ⑤ 历史可用证据（available_at + 证据形式 + 引用；缺失/矛盾 → NOT_OOS_ELIGIBLE）
   → ⑥ 凭据防护（凭据类列/值一律隔离，绝不写入报告、manifest 或数据库）
```

关键立场：

- **授权不可推断**：``status`` 不是显式 ``APPROVED``（缺失 / ``UNKNOWN`` / ``DENIED``）
  一律 ``AUTHORIZATION_MISSING``；程序不因为"网页可公开访问"而放行；
- **时间不可补**：任何时间缺失 / naive / 未来 / 因果颠倒都直接隔离，
  绝不用"当前时间"补齐（``effective_at`` 只按 ``max(published_at, collected_at)`` 派生）；
- **历史可用不可冒充**：``published_at`` 早于 ``collected_at`` 只说明"当时已发布"，
  不证明"当时系统可用"；没有独立 ``available_at`` 证据时标记
  ``AVAILABILITY_UNPROVEN`` → ``NOT_OOS_ELIGIBLE``（仍是有效原始事实，但不计入 OOS 证据）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.parse import urlsplit

from src.common import hashing
from src.common.redaction import is_sensitive_key, redact_secrets, safe_text, safe_url
from src.common.time import resolve_effective_at
from src.evidence.contracts import (
    ALLOWED_AUTHORIZATION_BASES,
    EVIDENCE_CONTRACT_VERSION,
    PERMISSION_FIELDS,
    QUARANTINE_REASON_CODES,
    AuthorizationDeclaration,
    EvidenceScope,
    ReasonCode,
    RowStatus,
    alias_map,
    canonical_field_names,
    required_field_names,
)

__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_REFERENCE_CHARS",
    "EvidenceRecord",
    "NormalizedRow",
    "RowAssessment",
    "assess_row",
    "normalize_input_row",
]

#: 正文入库存留上限（与 ``src/processors/collection/contracts.py`` 的默认值一致）
MAX_CONTENT_CHARS: Final[int] = 200_000
#: 引用类字段的存留上限（足够容纳 URL / 归档路径，同时防止异常长串撑爆 JSON）
MAX_REFERENCE_CHARS: Final[int] = 1_000

#: 必填字段 → 隔离原因码（``content`` 由"正文或内容引用至少一项"单独判定）
_MISSING_REASON: Final[dict[str, ReasonCode]] = {
    "source": ReasonCode.SOURCE_IDENTITY_MISSING,
    "source_record_id": ReasonCode.SOURCE_RECORD_ID_MISSING,
    "published_at": ReasonCode.PUBLISHED_AT_INVALID,
    "collected_at": ReasonCode.COLLECTED_AT_INVALID,
    "provenance_reference": ReasonCode.PROVENANCE_MISSING,
    "author_name": ReasonCode.SOURCE_IDENTITY_MISSING,
    "external_account_id": ReasonCode.SOURCE_IDENTITY_MISSING,
}
_AUTHORIZATION_REASON = ReasonCode.AUTHORIZATION_MISSING

#: 非敏感的中性附加列（仅记录"哪些列被忽略"，不参与判定）
_KNOWN_EXTRA_COLUMNS: Final[frozenset[str]] = frozenset(
    {"is_mock", "category", "has_media", "note", "notes", "collector", "batch"}
)


def _text(value: Any) -> str:
    """把任意输入值规范化为去空白的字符串（``None`` → 空串）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _truth(value: str) -> bool:
    """布尔列解析（``true`` / ``1`` / ``yes`` / ``y`` / ``on``；其余为 False）。"""
    return value.strip().lower() in {"true", "1", "yes", "y", "on"}


def _aware_time(value: str) -> datetime | None:
    """严格解析 ISO8601 → UTC；naive / 不可解析一律 ``None``（绝不假设时区）。"""
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _valid_reference(value: str) -> bool:
    """引用是否可核验：``https`` URL（无凭据）或 ``docs/legal/`` 内的相对路径。

    语义与 ``src/alpha/source_authorization.py`` 的引用格式一致，
    但本层**不检查文件是否真实存在**（存在性由人工 Gate / 只读体检复核），
    因此本函数是纯函数、可离线重放。
    """
    clean = value.strip().replace("\\", "/")
    if not clean:
        return False
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() == "https":
        return (
            bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and port != 0
        )
    if parsed.scheme or parsed.netloc or "?" in clean or "#" in clean:
        return False
    path = PurePosixPath(clean)
    return (
        not path.is_absolute()
        and path.parts[:2] == ("docs", "legal")
        and len(path.parts) > 2
        and ".." not in path.parts
    )


@dataclass(frozen=True, slots=True)
class NormalizedRow:
    """列映射结果（规范名 → 原始字符串值）与未知/凭据类列名单（**只留列名，不留值**）。"""

    values: Mapping[str, str]
    ignored_columns: tuple[str, ...]
    sensitive_columns: tuple[str, ...]

    def get(self, name: str) -> str:
        return self.values.get(name, "")


def normalize_input_row(row: Mapping[str, Any]) -> NormalizedRow:
    """按契约把一行输入映射为规范列（规范列优先，别名补空缺；与既有语料脚本同口径）。"""
    canonical_names = set(canonical_field_names())
    aliases = alias_map()
    values: dict[str, str] = {}
    ignored: list[str] = []
    sensitive: list[str] = []
    for key, value in row.items():
        if key is None:
            continue
        name = str(key).strip().lower()
        if name in canonical_names:
            values.setdefault(name, _text(value))
    for key, value in row.items():
        if key is None:
            continue
        name = str(key).strip().lower()
        if name in canonical_names:
            continue
        target = aliases.get(name)
        if target is None:
            if name not in _KNOWN_EXTRA_COLUMNS:
                ignored.append(name)
            if is_sensitive_key(name):
                sensitive.append(name)
            continue
        if target not in values:
            values[target] = _text(value)
    return NormalizedRow(
        values=values,
        ignored_columns=tuple(sorted(set(ignored))),
        sensitive_columns=tuple(sorted(set(sensitive))),
    )



@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """一条通过契约校验的证据（所有字符串均已脱敏；时间均已 UTC）。"""

    scope: EvidenceScope
    fingerprint: str
    source: str
    source_record_id: str
    content: str | None
    content_reference: str | None
    content_hash: str
    title: str | None
    language: str | None
    published_at: datetime
    collected_at: datetime
    effective_at: datetime
    available_at: datetime | None
    availability_provenance: str | None
    availability_reference: str | None
    authorization: AuthorizationDeclaration
    provenance_reference: str
    url: str | None
    author_name: str | None
    external_account_id: str | None
    oos_eligible: bool
    not_oos_eligible_reason: ReasonCode | None
    ingested_at: datetime
    ignored_columns: tuple[str, ...]

    def to_evidence_json(self) -> dict[str, Any]:
        """写入 ``raw_items.raw_json["evidence"]`` 的完整证据块（白名单字段）。"""
        return {
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "scope": self.scope.value,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "author_name": self.author_name,
            "external_account_id": self.external_account_id,
            "title": self.title,
            "language": self.language,
            "content_reference": self.content_reference,
            "content_hash": self.content_hash,
            "published_at": self.published_at.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "effective_at": self.effective_at.isoformat(),
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "availability_provenance": self.availability_provenance,
            "availability_reference": self.availability_reference,
            "authorization": self.authorization.to_dict(),
            "provenance_reference": self.provenance_reference,
            "url": self.url,
            "ingested_at": self.ingested_at.isoformat(),
            "oos_eligible": self.oos_eligible,
            "not_oos_eligible_reason": (
                self.not_oos_eligible_reason.value if self.not_oos_eligible_reason else None
            ),
            "ignored_columns": list(self.ignored_columns),
        }


@dataclass(frozen=True, slots=True)
class RowAssessment:
    """一行输入的校验结论（隔离时 ``record`` 为 ``None``，只有脱敏后的原因与列名）。"""

    index: int
    row_number: int
    scope: EvidenceScope
    source: str
    source_record_id: str
    fingerprint: str | None
    reason_codes: tuple[ReasonCode, ...]
    reasons: tuple[str, ...]
    ignored_columns: tuple[str, ...]
    record: EvidenceRecord | None

    @property
    def quarantined(self) -> bool:
        return self.record is None

    @property
    def status(self) -> RowStatus:
        return RowStatus.QUARANTINED if self.quarantined else RowStatus.ACCEPTED

    @property
    def oos_eligible(self) -> bool:
        return bool(self.record is not None and self.record.oos_eligible)

    @property
    def not_oos_eligible_reason(self) -> ReasonCode | None:
        return self.record.not_oos_eligible_reason if self.record is not None else None


def _safe(value: str, *, max_chars: int = 200) -> str:
    """短字段脱敏（擦除凭据 + 截断）。"""
    return safe_text(value, max_chars=max_chars)


def _safe_reference(value: str) -> str:
    """引用字段脱敏：URL 去掉 userinfo / query / fragment，其余按普通文本处理。"""
    return safe_text(safe_url(value, max_chars=MAX_REFERENCE_CHARS), max_chars=MAX_REFERENCE_CHARS)


def _dedupe_reasons(
    reasons: list[tuple[ReasonCode, str]],
) -> tuple[tuple[ReasonCode, ...], tuple[str, ...]]:
    """原因（码, 文本）去重：同一原因码只保留首条文本，顺序稳定。"""
    codes: list[ReasonCode] = []
    texts: list[str] = []
    seen: set[ReasonCode] = set()
    for code, text in reasons:
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
        texts.append(text)
    return tuple(codes), tuple(texts)


def assess_row(
    row: Mapping[str, Any],
    *,
    scope: EvidenceScope,
    index: int,
    row_number: int,
    moment: datetime,
    ingested_at: datetime | None = None,
) -> RowAssessment:
    """校验一行输入；返回校验结论（含脱敏原因与可选证据记录）。

    Args:
        row: 原始输入行（JSONL object 或 CSV dict）。
        scope: 命令声明的证据类别。
        index: 批次内序号（0 起）。
        row_number: 显示用行号（CSV 含表头从 2 起；JSONL 从 1 起）。
        moment: 审计时钟（必须带时区；用于"未来时间"判定，保证可复现）。
        ingested_at: 系统入账时间；缺省 = ``moment``。

    Raises:
        ValueError: ``moment`` 未带时区（禁止隐式时区）。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment 必须包含时区")
    moment = moment.astimezone(UTC)
    ingested = (ingested_at or moment).astimezone(UTC)
    normalized = normalize_input_row(row)
    reasons: list[tuple[ReasonCode, str]] = []

    source = normalized.get("source")
    record_id = normalized.get("source_record_id")
    if not source:
        reasons.append((ReasonCode.SOURCE_IDENTITY_MISSING, "缺少必填字段 source（来源身份）"))
    if not record_id:
        reasons.append(
            (ReasonCode.SOURCE_RECORD_ID_MISSING, "缺少必填字段 source_record_id（幂等键）")
        )
    for name in required_field_names(scope):
        if name in {"source", "source_record_id"}:
            continue
        if not normalized.get(name):
            reasons.append(
                (_MISSING_REASON.get(name, _AUTHORIZATION_REASON), f"缺少必填字段 {name}")
            )

    content = normalized.get("content")
    content_ref = normalized.get("content_ref")
    if not content and not content_ref:
        reasons.append(
            (ReasonCode.CONTENT_EMPTY, "content 与 content_ref 同时为空（无内容也无内容引用）")
        )
    declared_scope = normalized.get("scope").strip().lower()
    if declared_scope and declared_scope != scope.value:
        reasons.append(
            (
                ReasonCode.SCOPE_MISMATCH,
                f"行内 scope={_safe(declared_scope, max_chars=40)} 与命令 scope="
                f"{scope.value} 不一致",
            )
        )

    status = normalized.get("authorization_status").strip().upper()
    basis = normalized.get("authorization_basis").strip().lower()
    reference = normalized.get("authorization_reference")
    if status != "APPROVED":
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_MISSING,
                f"authorization_status={_safe(status, max_chars=40) or 'MISSING'} 不是显式 "
                "APPROVED（缺失 / UNKNOWN / DENIED 一律不予放行，不得推断授权）",
            )
        )
    if basis not in ALLOWED_AUTHORIZATION_BASES:
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_MISSING,
                f"authorization_basis={_safe(basis, max_chars=40) or 'MISSING'} 不在白名单 "
                f"{sorted(ALLOWED_AUTHORIZATION_BASES)}",
            )
        )
    if reference and not _valid_reference(reference):
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_REFERENCE_INVALID,
                "authorization_reference 必须是 https URL 或 docs/legal/ 内相对路径",
            )
        )
    denied_permissions = [name for name in PERMISSION_FIELDS if not _truth(normalized.get(name))]
    if denied_permissions:
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_MISSING,
                f"未同时许可采集 / 存储 / 研究：{denied_permissions}",
            )
        )
    reviewed_by = normalized.get("authorization_reviewed_by")
    if not reviewed_by:
        reasons.append(
            (ReasonCode.AUTHORIZATION_MISSING, "缺少人工核验人 authorization_reviewed_by")
        )
    reviewed_at = _aware_time(normalized.get("authorization_reviewed_at"))
    if reviewed_at is None:
        reasons.append(
            (ReasonCode.AUTHORIZATION_MISSING, "authorization_reviewed_at 不可解析或缺少时区")
        )
    elif reviewed_at > moment:
        reasons.append((ReasonCode.AUTHORIZATION_MISSING, "authorization_reviewed_at 是未来时间"))

    published = _aware_time(normalized.get("published_at"))
    if published is None:
        reasons.append(
            (
                ReasonCode.PUBLISHED_AT_INVALID,
                "published_at 不可解析或缺少时区（禁止用当前时间补齐）",
            )
        )
    elif published > moment:
        reasons.append((ReasonCode.PUBLISHED_AT_INVALID, "published_at 是未来时间"))
    collected = _aware_time(normalized.get("collected_at"))
    if collected is None:
        reasons.append(
            (
                ReasonCode.COLLECTED_AT_INVALID,
                "collected_at 不可解析或缺少时区（禁止复制发布时间充数）",
            )
        )
    else:
        if collected > moment:
            reasons.append((ReasonCode.COLLECTED_AT_INVALID, "collected_at 是未来时间"))
        if published is not None and collected <= published:
            reasons.append(
                (
                    ReasonCode.COLLECTED_AT_INVALID,
                    "collected_at 必须晚于 published_at，不得复制发布时间",
                )
            )

    valid_from_raw = normalized.get("authorization_valid_from")
    valid_from = _aware_time(valid_from_raw) if valid_from_raw else None
    if valid_from_raw and valid_from is None:
        reasons.append(
            (ReasonCode.AUTHORIZATION_MISSING, "authorization_valid_from 不可解析或缺少时区")
        )
    elif valid_from is not None and valid_from > moment:
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_MISSING,
                "authorization_valid_from 是未来时间（授权尚未生效）",
            )
        )
    expires_raw = normalized.get("authorization_expires_at")
    expires = _aware_time(expires_raw) if expires_raw else None
    if expires_raw and expires is None:
        reasons.append(
            (ReasonCode.AUTHORIZATION_MISSING, "authorization_expires_at 不可解析或缺少时区")
        )
    elif expires is not None and valid_from is not None and expires <= valid_from:
        reasons.append(
            (
                ReasonCode.AUTHORIZATION_MISSING,
                "authorization_expires_at 不晚于 authorization_valid_from",
            )
        )
    if collected is not None:
        if valid_from is not None and collected < valid_from:
            reasons.append(
                (
                    ReasonCode.AUTHORIZATION_MISSING,
                    "collected_at 早于授权生效时间（不得用事后授权追认既有采集）",
                )
            )
        if expires is not None and collected >= expires:
            reasons.append((ReasonCode.AUTHORIZATION_MISSING, "collected_at 已超出授权有效期"))

    provenance = normalized.get("provenance_reference")
    if provenance and not _valid_reference(provenance):
        reasons.append(
            (
                ReasonCode.PROVENANCE_MISSING,
                "provenance_reference 必须是 https URL 或 docs/legal/ 内相对路径",
            )
        )

    available_raw = normalized.get("available_at")
    availability_provenance = normalized.get("availability_provenance")
    availability_reference = normalized.get("availability_reference")
    available_at = _aware_time(available_raw) if available_raw else None
    availability_problem: str | None = None
    if not available_raw:
        availability_problem = (
            "缺少独立 available_at：published_at 早于 collected_at 不等于历史可用"
        )
    elif available_at is None:
        availability_problem = "available_at 不可解析或缺少时区（禁止用当前时间补齐）"
    elif available_at > moment:
        availability_problem = "available_at 是未来时间"
    elif not availability_provenance:
        availability_problem = "缺少 availability_provenance（无法审计证据形式）"
    elif not _valid_reference(availability_reference):
        availability_problem = "availability_reference 必须是 https URL 或 docs/legal/ 内相对路径"
    elif (
        published is not None
        and collected is not None
        and not (published <= available_at <= collected)
    ):
        availability_problem = "available_at 不在 [published_at, collected_at] 区间（证据自相矛盾）"

    if availability_problem is not None:
        # 非隔离原因：记录仍可作为有效原始事实，但不具备历史 OOS 资格
        reasons.append((ReasonCode.AVAILABILITY_UNPROVEN, availability_problem))

    if normalized.sensitive_columns:
        reasons.append(
            (
                ReasonCode.SENSITIVE_VALUE_DETECTED,
                f"输入行含凭据类列 {list(normalized.sensitive_columns)}（只记录列名，值丢弃）",
            )
        )
    credential_fields = sorted(
        name
        for name, value in normalized.values.items()
        if value and redact_secrets(value) != value
    )
    if credential_fields:
        reasons.append(
            (
                ReasonCode.SENSITIVE_VALUE_DETECTED,
                f"字段疑似包含凭据 {credential_fields}（值不记录、不入库）",
            )
        )

    fingerprint: str | None = None
    if source and record_id:
        fingerprint = hashing.content_hash(
            EVIDENCE_CONTRACT_VERSION,
            scope.value,
            source,
            record_id,
            hashing.content_hash(content, content_ref),
            namespace="evidence.intake.fingerprint",
        )

    reason_codes, reason_texts = _dedupe_reasons(reasons)
    quarantined = any(code in QUARANTINE_REASON_CODES for code in reason_codes)
    record: EvidenceRecord | None = None
    if (
        not quarantined
        and fingerprint is not None
        and published is not None
        and collected is not None
    ):
        record = EvidenceRecord(
            scope=scope,
            fingerprint=fingerprint,
            source=_safe(source, max_chars=100),  # sources.name VARCHAR(100)
            source_record_id=_safe(record_id, max_chars=300),  # raw_items.source_record_id
            content=safe_text(content, max_chars=MAX_CONTENT_CHARS) if content else None,
            content_reference=_safe_reference(content_ref) if content_ref else None,
            content_hash=hashing.content_hash(
                normalized.get("title") or None, content or content_ref
            ),
            title=_safe(normalized.get("title")) or None,
            language=_safe(normalized.get("language"), max_chars=40) or None,
            published_at=published,
            collected_at=collected,
            effective_at=resolve_effective_at(published_at=published, collected_at=collected),
            available_at=available_at,
            availability_provenance=(
                _safe(availability_provenance) if availability_provenance else None
            ),
            availability_reference=(
                _safe_reference(availability_reference) if availability_reference else None
            ),
            authorization=AuthorizationDeclaration(
                status=status,
                basis=basis,
                reference=_safe_reference(reference) if reference else "",
                reviewed_by=_safe(reviewed_by),
                reviewed_at=reviewed_at.isoformat() if reviewed_at else "",
                valid_from=valid_from.isoformat() if valid_from else None,
                expires_at=expires.isoformat() if expires else None,
                permits_automated_collection=_truth(
                    normalized.get("permits_automated_collection")
                ),
                permits_local_storage=_truth(normalized.get("permits_local_storage")),
                permits_research_use=_truth(normalized.get("permits_research_use")),
            ),
            provenance_reference=_safe_reference(provenance),
            url=_safe_reference(normalized.get("url")) or None,
            author_name=_safe(normalized.get("author_name")) or None,
            external_account_id=_safe(normalized.get("external_account_id")) or None,
            oos_eligible=availability_problem is None,
            not_oos_eligible_reason=(
                None if availability_problem is None else ReasonCode.AVAILABILITY_UNPROVEN
            ),
            ingested_at=ingested,
            ignored_columns=normalized.ignored_columns,
        )
    return RowAssessment(
        index=index,
        row_number=row_number,
        scope=scope,
        source=_safe(source),
        source_record_id=_safe(record_id),
        fingerprint=fingerprint,
        reason_codes=reason_codes,
        reasons=reason_texts,
        ignored_columns=normalized.ignored_columns,
        record=record,
    )





