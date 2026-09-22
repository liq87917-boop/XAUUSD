"""合规授权数据的 Evidence Intake 契约（GOLD-005，``evidence-intake-v1``）。

本模块是**版本化输入契约**的唯一来源：Author / News 两类证据文件允许哪些列、哪些列必填、
字段名如何映射到既有模型语义（``raw_items`` / ``author_posts`` / ``news_events``），
全部在此声明，禁止在 CLI 或导入实现里另写一套。

职责边界（对齐 `.clinerules` 与 `.ai/DEVELOPMENT_PROTOCOL.md`）：

1. **不联网**：本层不抓取微博 / Kitco / 任何站点，不做 robots 探测，不申请证书豁免；
   只接收业务方**已提供**的授权数据文件（JSONL / CSV）；
2. **只做机械校验**：验证授权声明、来源身份、时间语义与内容完整性是否**齐全**；
   ``APPROVED`` / ``permits_* = true`` / 许可引用都是**人工签认声明**，
   程序不证明其法律效力、许可范围或签认人身份；
3. **不臆断授权**：缺失 / ``UNKNOWN`` / ``DENIED`` 一律**不得**被推断为 authorized；
4. **不伪造时间**：``published_at`` / ``collected_at`` / ``available_at`` 只来自输入；
   ``effective_at = max(published_at, collected_at)``（沿用既有 raw 契约）；
   ``ingested_at`` 由**系统**赋值（输入提供的值一律忽略，防止伪造审计时间）；
5. **不覆盖历史事实**：只写 append-only 的 ``raw_items`` / ``processed_items``，
   同一 ``(source, source_record_id)`` 若内容不同判 ``IDENTITY_CONFLICT`` 并隔离，绝不 UPDATE；
6. **历史 OOS 资格独立**：``published_at`` 早于 ``collected_at`` **本身不等于历史可用**；
   必须有独立的 ``available_at`` + ``availability_provenance`` + ``availability_reference``
   才有 OOS 资格，否则明确标记 ``NOT_OOS_ELIGIBLE``（``AVAILABILITY_UNPROVEN``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ALLOWED_AUTHORIZATION_BASES",
    "AUTHOR_ONLY_FIELDS",
    "COMMON_REQUIRED_FIELDS",
    "ELIGIBILITY_REASON_CODES",
    "EVIDENCE_CONTRACT_VERSION",
    "EVIDENCE_FIELDS",
    "EVIDENCE_SCHEMA_VERSION",
    "PERMISSION_FIELDS",
    "QUARANTINE_REASON_CODES",
    "SYSTEM_ASSIGNED_FIELDS",
    "AuthorizationDeclaration",
    "EvidenceField",
    "EvidenceScope",
    "ReasonCode",
    "RowStatus",
    "alias_map",
    "canonical_field_names",
    "field_by_name",
    "required_field_names",
]

#: 契约版本：字段增删必须升版本，并同步更新 README / 测试与既有落库元数据解释
EVIDENCE_CONTRACT_VERSION: Final[str] = "evidence-intake-v1"
#: 报告 / manifest 的机器可读 schema 版本
EVIDENCE_SCHEMA_VERSION: Final[int] = 1

#: 授权依据白名单（与 ``src/alpha/source_authorization.py`` **同一词汇表**，不另造）
ALLOWED_AUTHORIZATION_BASES: Final[frozenset[str]] = frozenset(
    {"official_api", "license_agreement", "written_permission", "user_owned"}
)
#: 三项用途许可（与 ``source_authorization`` 对齐；缺任一项即视为授权声明不完整）
PERMISSION_FIELDS: Final[tuple[str, ...]] = (
    "permits_automated_collection",
    "permits_local_storage",
    "permits_research_use",
)
#: 系统赋值字段：输入提供时**忽略**（不得由外部伪造审计时间）
SYSTEM_ASSIGNED_FIELDS: Final[tuple[str, ...]] = ("ingested_at", "fingerprint")


class EvidenceScope(StrEnum):
    """证据类别（Author = 作者帖子类证据；News = 新闻/快讯类证据）。"""

    AUTHOR = "author"
    NEWS = "news"

    @property
    def item_type(self) -> str:
        """映射到 ``raw_items.item_type``（Author → POST；News → NEWS）。"""
        return "POST" if self is EvidenceScope.AUTHOR else "NEWS"

    @property
    def label(self) -> str:
        return "作者" if self is EvidenceScope.AUTHOR else "新闻"


class RowStatus(StrEnum):
    """逐行导入结论（``DUPLICATE`` 是幂等重放，不是失败）。"""

    ACCEPTED = "ACCEPTED"
    QUARANTINED = "QUARANTINED"
    DUPLICATE = "DUPLICATE"


class ReasonCode(StrEnum):
    """机器可读原因码（稳定；文本永远脱敏）。

    隔离类（``QUARANTINE_REASON_CODES``）表示该行**不得**进入可信证据；
    资格类（``ELIGIBILITY_REASON_CODES``）表示该行仍是有效原始事实，
    但**不具备**历史 OOS 资格（``NOT_OOS_ELIGIBLE``）。
    """

    # ---- 隔离类 ------------------------------------------------------
    SOURCE_IDENTITY_MISSING = "SOURCE_IDENTITY_MISSING"
    SOURCE_RECORD_ID_MISSING = "SOURCE_RECORD_ID_MISSING"
    CONTENT_EMPTY = "CONTENT_EMPTY"
    PUBLISHED_AT_INVALID = "PUBLISHED_AT_INVALID"
    COLLECTED_AT_INVALID = "COLLECTED_AT_INVALID"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"
    AUTHORIZATION_MISSING = "AUTHORIZATION_MISSING"
    AUTHORIZATION_REFERENCE_INVALID = "AUTHORIZATION_REFERENCE_INVALID"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    SENSITIVE_VALUE_DETECTED = "SENSITIVE_VALUE_DETECTED"
    ROW_UNREADABLE = "ROW_UNREADABLE"
    # ---- 幂等类 ------------------------------------------------------
    DUPLICATE = "DUPLICATE"
    # ---- 资格类（非隔离）--------------------------------------------
    AVAILABILITY_UNPROVEN = "AVAILABILITY_UNPROVEN"


#: 导致隔离的原因码（这些行不落库、不计入可信证据）
QUARANTINE_REASON_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.SOURCE_IDENTITY_MISSING,
        ReasonCode.SOURCE_RECORD_ID_MISSING,
        ReasonCode.CONTENT_EMPTY,
        ReasonCode.PUBLISHED_AT_INVALID,
        ReasonCode.COLLECTED_AT_INVALID,
        ReasonCode.PROVENANCE_MISSING,
        ReasonCode.AUTHORIZATION_MISSING,
        ReasonCode.AUTHORIZATION_REFERENCE_INVALID,
        ReasonCode.SCOPE_MISMATCH,
        ReasonCode.IDENTITY_CONFLICT,
        ReasonCode.SENSITIVE_VALUE_DETECTED,
        ReasonCode.ROW_UNREADABLE,
    }
)
#: 只影响 OOS 资格、不导致隔离的原因码
ELIGIBILITY_REASON_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.AVAILABILITY_UNPROVEN}
)


@dataclass(frozen=True, slots=True)
class EvidenceField:
    """一个契约字段（名称 / 别名 / 适用范围 / 说明）。"""

    name: str
    aliases: tuple[str, ...]
    required_for: frozenset[EvidenceScope]
    description: str

    def is_required_for(self, scope: EvidenceScope) -> bool:
        return scope in self.required_for


def _required(name: str, *, aliases: tuple[str, ...] = (), description: str = "") -> EvidenceField:
    """两类证据都必填的字段。"""
    return EvidenceField(name, aliases, frozenset(EvidenceScope), description)


def _optional(name: str, *, aliases: tuple[str, ...] = (), description: str = "") -> EvidenceField:
    """可选字段（Author / News 通用）。"""
    return EvidenceField(name, aliases, frozenset(), description)


def _author_only(name: str, *, aliases: tuple[str, ...] = (), description: str) -> EvidenceField:
    """仅 Author 证据必填的字段。"""
    return EvidenceField(name, aliases, frozenset({EvidenceScope.AUTHOR}), description)


def _system(name: str, *, description: str) -> EvidenceField:
    """系统赋值字段（不得由输入提供）。"""
    return EvidenceField(name, (), frozenset(), description)


#: 字段注册表（顺序即文档与报错呈现顺序）
EVIDENCE_FIELDS: Final[tuple[EvidenceField, ...]] = (
    _required(
        "source",
        aliases=("source_name",),
        description="来源身份（平台/供应商名称，写入 sources.name；不得用模型名冒充）",
    ),
    _required(
        "source_record_id",
        aliases=("id", "post_id", "record_id"),
        description="来源内稳定且唯一的记录 ID（幂等键的一部分）",
    ),
    _optional(
        "content",
        aliases=("text_content", "content_text", "text"),
        description="原文（不改写、不总结）；与 content_ref 至少一项非空",
    ),
    _optional(
        "content_ref",
        aliases=("content_reference", "content_url"),
        description="可核验的内容引用（归档快照 / 官方导出指针）；与 content 至少一项非空",
    ),
    _optional("title", aliases=("headline",), description="标题/导语（可空）"),
    _required(
        "published_at",
        aliases=("published", "publish_time"),
        description="外部首次公开时间，必须带时区（naive 一律拒绝，不用当前时间补）",
    ),
    _required(
        "collected_at",
        aliases=("collected", "collect_time"),
        description="实际采集/导出该记录的时间，必须带时区且**晚于** published_at",
    ),
    _optional(
        "available_at",
        aliases=("availability_at",),
        description="独立的历史可用时间证据（研究系统最早可据此决策的时刻）",
    ),
    _optional(
        "availability_provenance",
        description="available_at 的证据形式（如 provider_archive_export / historical_snapshot）",
    ),
    _optional(
        "availability_reference",
        aliases=("availability_evidence",),
        description="available_at 的可核验引用（https URL 或 docs/legal/ 内路径）",
    ),
    _required(
        "provenance_reference",
        aliases=("provenance", "evidence_reference"),
        description="来源出处/证据引用（原始页面、导出文件或授权档案的可核验指针）",
    ),
    _optional("url", aliases=("source_url", "link"), description="原始页面地址"),
    _optional("language", description="语言代码（如 zh / en）"),
    _author_only(
        "author_name",
        aliases=("author", "display_name"),
        description="作者显示名（Author 证据必填）",
    ),
    _author_only(
        "external_account_id",
        aliases=("account_id", "account", "author_account_id"),
        description="平台账号稳定 ID（Author 证据必填；不得填昵称副本）",
    ),
    _required(
        "authorization_status",
        aliases=("auth_status",),
        description="授权状态；只有显式 APPROVED 才放行（缺失/UNKNOWN/DENIED 一律隔离）",
    ),
    _required(
        "authorization_basis",
        aliases=("auth_basis",),
        description="授权依据（取值见 ALLOWED_AUTHORIZATION_BASES），如 written_permission",
    ),
    _required(
        "authorization_reference",
        aliases=("auth_reference", "license_reference", "license_url"),
        description="可复核的 https 条款 URL 或 docs/legal/ 内现存许可文件路径",
    ),
    _required(
        "authorization_reviewed_by",
        aliases=("reviewed_by",),
        description="实际核验授权的人（不得填模型名冒充人工）",
    ),
    _required(
        "authorization_reviewed_at",
        aliases=("reviewed_at",),
        description="核验时间，必须带时区且不得为未来时间",
    ),
    _optional(
        "authorization_valid_from",
        aliases=("valid_from",),
        description="授权生效时间（带时区）；collected_at 必须不早于它",
    ),
    _optional(
        "authorization_expires_at",
        aliases=("expires_at",),
        description="授权到期时间（带时区）；collected_at 必须早于它",
    ),
    _optional("permits_automated_collection", description="是否明确允许自动采集"),
    _optional("permits_local_storage", description="是否明确允许本地保存"),
    _optional("permits_research_use", description="是否明确允许研究/模型处理"),
    _optional(
        "scope",
        description="可选的显式 scope；与 CLI 的 --scope 不一致时隔离（SCOPE_MISMATCH）",
    ),
    _system(
        "ingested_at",
        description="系统赋值（输入提供的值一律忽略，防止伪造审计时间）",
    ),
)

#: Author 证据额外必填的字段
AUTHOR_ONLY_FIELDS: Final[tuple[str, ...]] = ("author_name", "external_account_id")
#: 两类证据都必填的字段（含三项用途许可，与 ``source_authorization`` 对齐）
COMMON_REQUIRED_FIELDS: Final[tuple[str, ...]] = tuple(
    field.name
    for field in EVIDENCE_FIELDS
    if field.required_for == frozenset(EvidenceScope)
) + PERMISSION_FIELDS

_FIELDS_BY_NAME: Final[dict[str, EvidenceField]] = {field.name: field for field in EVIDENCE_FIELDS}


def field_by_name(name: str) -> EvidenceField | None:
    """按规范名取字段定义（不存在返回 ``None``）。"""
    return _FIELDS_BY_NAME.get(str(name).strip().lower())


def canonical_field_names() -> tuple[str, ...]:
    """全部规范字段名（含系统赋值字段）。"""
    return tuple(field.name for field in EVIDENCE_FIELDS)


def required_field_names(scope: EvidenceScope) -> tuple[str, ...]:
    """指定 scope 的必填字段（Author 额外要求作者身份两列）。"""
    names = list(COMMON_REQUIRED_FIELDS)
    if scope is EvidenceScope.AUTHOR:
        names.extend(AUTHOR_ONLY_FIELDS)
    return tuple(names)


def alias_map() -> dict[str, str]:
    """别名 → 规范名（规范名不在此表中，按\"规范列优先\"两遍扫描）。"""
    mapping: dict[str, str] = {}
    for field in EVIDENCE_FIELDS:
        for alias in field.aliases:
            mapping[alias] = field.name
    return mapping


@dataclass(frozen=True, slots=True)
class AuthorizationDeclaration:
    """授权声明的**逐字段事实**（程序只校验齐全性，不判断法律效力）。"""

    status: str
    basis: str
    reference: str
    reviewed_by: str
    reviewed_at: str
    valid_from: str | None
    expires_at: str | None
    permits_automated_collection: bool
    permits_local_storage: bool
    permits_research_use: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "basis": self.basis,
            "reference": self.reference,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "permits_automated_collection": self.permits_automated_collection,
            "permits_local_storage": self.permits_local_storage,
            "permits_research_use": self.permits_research_use,
        }


