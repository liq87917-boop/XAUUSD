"""Phase 3.3 作者来源授权的机械门禁。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

APPROVED: Final[str] = "APPROVED"
ALLOWED_BASES: Final[frozenset[str]] = frozenset(
    {"official_api", "license_agreement", "written_permission", "user_owned"}
)
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "source",
    "external_account_id",
    "authorization_status",
    "authorization_basis",
    "authorization_reference",
    "permits_automated_collection",
    "permits_local_storage",
    "permits_research_use",
    "reviewed_by",
    "reviewed_at",
    "valid_from",
    "expires_at",
)
REQUIRED_VALUES: Final[tuple[str, ...]] = tuple(
    column for column in REQUIRED_COLUMNS if column != "expires_at"
)


@dataclass(frozen=True, slots=True)
class SourceAuthorizationAudit:
    rows: int
    approved_accounts: frozenset[tuple[str, str]]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    ready: bool


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _true(value: str) -> bool:
    return value.strip().lower() == "true"


def _valid_reference(value: str, *, evidence_root: Path) -> bool:
    clean = value.strip().replace("\\", "/")
    try:
        parsed = urlsplit(clean)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() == "https":
        return bool(hostname) and parsed.username is None and parsed.password is None and port != 0
    if parsed.scheme or parsed.netloc or "?" in clean or "#" in clean:
        return False
    path = PurePosixPath(clean)
    structurally_valid = (
        not path.is_absolute()
        and path.parts[:2] == ("docs", "legal")
        and len(path.parts) > 2
        and ".." not in path.parts
    )
    if not structurally_valid:
        return False
    root = evidence_root.resolve()
    candidate = (root / clean).resolve()
    return candidate.is_relative_to(root / "docs" / "legal") and candidate.is_file()


def validate_source_authorizations(
    rows: Sequence[Mapping[str, str]], *, now: datetime, evidence_root: Path
) -> SourceAuthorizationAudit:
    """仅批准有证据、三项用途均许可且当前有效的稳定账号键。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须包含时区")
    moment = now.astimezone(UTC)
    errors: list[str] = []
    warnings: list[str] = []
    approved: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for number, row in enumerate(rows, start=1):
        missing = [name for name in REQUIRED_VALUES if not str(row.get(name, "")).strip()]
        if missing:
            errors.append(f"第 {number} 行缺少必填值：{missing}")
            continue
        account = (
            str(row["source"]).strip(),
            str(row["external_account_id"]).strip(),
        )
        if account in seen:
            errors.append(f"第 {number} 行 source+external_account_id 重复：{account}")
        seen.add(account)

        status = str(row["authorization_status"]).strip().upper()
        if status != APPROVED:
            warnings.append(f"账号 {account!r} 状态为 {status!r}，不予放行")
            continue
        basis = str(row["authorization_basis"]).strip().lower()
        if basis not in ALLOWED_BASES:
            errors.append(f"第 {number} 行 authorization_basis 不受支持：{basis!r}")
        if not _valid_reference(str(row["authorization_reference"]), evidence_root=evidence_root):
            errors.append(
                f"第 {number} 行 authorization_reference 必须是 https URL 或 docs/legal/ 内现存文件"
            )
        permissions = (
            "permits_automated_collection",
            "permits_local_storage",
            "permits_research_use",
        )
        denied = [name for name in permissions if not _true(str(row[name]))]
        if denied:
            errors.append(f"第 {number} 行未同时许可采集、存储与研究：{denied}")

        reviewed = _time(str(row["reviewed_at"]))
        valid_from = _time(str(row["valid_from"]))
        expires_raw = str(row.get("expires_at", "")).strip()
        expires = _time(expires_raw) if expires_raw else None
        if reviewed is None or valid_from is None or (expires_raw and expires is None):
            errors.append(f"第 {number} 行授权时间不可解析或缺少时区")
        else:
            if reviewed > moment:
                errors.append(f"第 {number} 行 reviewed_at 是未来时间")
            if valid_from > moment:
                errors.append(f"第 {number} 行授权尚未生效")
            if expires is not None and expires <= moment:
                errors.append(f"第 {number} 行授权已过期")
            if expires is not None and expires <= valid_from:
                errors.append(f"第 {number} 行 expires_at 不晚于 valid_from")

        row_has_error = any(item.startswith(f"第 {number} 行") for item in errors)
        if not row_has_error:
            approved.add(account)

    if not rows:
        errors.append("授权表没有数据行")
    ready = bool(rows) and not errors and bool(approved)
    return SourceAuthorizationAudit(
        rows=len(rows),
        approved_accounts=frozenset(approved),
        errors=tuple(errors),
        warnings=tuple(warnings),
        ready=ready,
    )
