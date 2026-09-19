"""Phase 3.3 真实作者帖子输入的只读资格校验。"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

MIN_AUTHOR_SAMPLES: Final[int] = 30
MIN_CONTENT_CHARS: Final[int] = 90
FUTURE_TOLERANCE: Final[timedelta] = timedelta(hours=1)
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "source",
    "author_name",
    "external_account_id",
    "content",
    "published_at",
    "collected_at",
    "effective_at",
    "url",
    "has_media",
    "source_type",
    "collection_time_provenance",
)


@dataclass(frozen=True, slots=True)
class AuthorInputAudit:
    rows: int
    author_counts: tuple[tuple[str, int], ...]
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


def _content_key(value: str) -> str:
    normalized = " ".join(value.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _url_key(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    authority = parsed.hostname.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}{parsed.path}?{parsed.query}"


def validate_author_input(
    rows: Sequence[Mapping[str, str]],
    *,
    now: datetime,
    authorized_accounts: Collection[tuple[str, str]],
) -> AuthorInputAudit:
    """验证时间因果、身份、重复与每作者样本门槛；不修改输入。"""
    moment = now.astimezone(UTC)
    errors: list[str] = []
    warnings: list[str] = []
    counts: Counter[tuple[str, str]] = Counter()
    account_names: dict[tuple[str, str], str] = {}
    seen_ids: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    seen_urls: set[str] = set()
    for number, row in enumerate(rows, start=1):
        missing = [name for name in REQUIRED_COLUMNS if not str(row.get(name, "")).strip()]
        if missing:
            errors.append(f"第 {number} 行缺少必填值：{missing}")
            continue
        source = str(row["source"]).strip()
        record_id = str(row["id"]).strip()
        external_account_id = str(row["external_account_id"]).strip()
        author_name = str(row["author_name"]).strip()
        account = (source, external_account_id)
        previous_name = account_names.setdefault(account, author_name)
        if previous_name != author_name:
            errors.append(
                f"第 {number} 行账号 {source}/{external_account_id} 的作者名不一致："
                f"{previous_name!r} != {author_name!r}"
            )
        identity = (source, record_id)
        if identity in seen_ids:
            errors.append(f"第 {number} 行 source+id 重复：{identity}")
        seen_ids.add(identity)

        content = str(row["content"]).strip()
        content_key = _content_key(content)
        if content_key in seen_content:
            errors.append(f"第 {number} 行正文与前文重复，不能增加独立样本")
        seen_content.add(content_key)
        if len(content) < MIN_CONTENT_CHARS:
            errors.append(f"第 {number} 行正文仅 {len(content)} 字符 < {MIN_CONTENT_CHARS}")

        url = str(row["url"]).strip()
        url_key = _url_key(url)
        if url_key is None:
            errors.append(f"第 {number} 行 url 必须是可复核的 http/https 地址")
        elif url_key in seen_urls:
            errors.append(f"第 {number} 行 url 与前文重复，不能增加独立样本")
        else:
            seen_urls.add(url_key)

        published = _time(str(row["published_at"]))
        collected = _time(str(row["collected_at"]))
        effective = _time(str(row["effective_at"]))
        if published is None or collected is None or effective is None:
            errors.append(f"第 {number} 行时间不可解析或缺少时区")
        else:
            if collected <= published:
                errors.append(
                    f"第 {number} 行 collected_at 必须晚于 published_at，不能复制发布时间"
                )
            if effective != max(published, collected):
                errors.append(f"第 {number} 行 effective_at 不等于发布时间和采集时间的较晚者")
            if published > moment + FUTURE_TOLERANCE or collected > moment + FUTURE_TOLERANCE:
                errors.append(f"第 {number} 行包含未来时间")

        if str(row["collection_time_provenance"]).strip() != "independent_observation":
            errors.append(f"第 {number} 行采集时间来源不是 independent_observation")
        if str(row["source_type"]).strip().upper() != "NEWS":
            errors.append(f"第 {number} 行 source_type 必须为 NEWS")
        if str(row["has_media"]).strip().lower() not in {"true", "false"}:
            errors.append(f"第 {number} 行 has_media 必须为 true/false")
        counts[account] += 1

    if not rows:
        errors.append("输入没有数据行")
    labelled_counts = tuple(
        (
            f"{account_names[account]} [{account[0]}/{account[1]}]",
            count,
        )
        for account, count in sorted(counts.items())
    )
    for author, count in labelled_counts:
        if count < MIN_AUTHOR_SAMPLES:
            warnings.append(f"作者 {author!r} 只有 {count} 条 < {MIN_AUTHOR_SAMPLES}")
    for account in sorted(counts):
        if account not in authorized_accounts:
            errors.append(f"账号 {account!r} 没有当前有效的采集、存储与研究授权")
    ready = (
        bool(rows)
        and not errors
        and bool(counts)
        and all(count >= MIN_AUTHOR_SAMPLES for count in counts.values())
    )
    return AuthorInputAudit(
        rows=len(rows),
        author_counts=labelled_counts,
        errors=tuple(errors),
        warnings=tuple(warnings),
        ready=ready,
    )
