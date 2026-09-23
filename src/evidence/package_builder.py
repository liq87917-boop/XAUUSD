"""授权 Evidence 本地 Package / Manifest Builder（GOLD-017，纯本地）。

默认 **dry-run** / 显式人工输入：把**人工显式提供**的授权 / 时间 / availability / OOS 元数据与
**人工指定的现有 evidence 文件**整理成 GOLD-011 inbox 可直接消费的 ``manifest.json``。

GOLD-011 已定义候选包契约（候选包 = inbox 直接子目录 + 固定 ``manifest.json``；manifest 必填
``evidence_type`` / ``source`` / ``authorization_reference`` / ``time_semantics`` /
``availability_semantics`` / ``historical_oos_applicable`` / ``files[].path+sha256``，并逐文件
核验内容摘要、逐行复用 ``evidence-intake-v1`` 的 ``assess_row``）。但业务方首次交付真实数据仍要
**手工**写 manifest、手工算 SHA-256，容易造成格式 / 摘要 / 路径错误。

本模块补齐这一“摩擦点”的**纯本地**入口，且**只做机械整理**：

- **显式人工输入，绝不推断**：``evidence_type`` / ``source`` / ``authorization_reference`` /
  ``time_semantics`` / ``availability_semantics`` / ``historical_oos_applicable`` 与要纳入的
  文件清单**必须**由人工显式给出；工具**绝不**从文件名、正文、URL、mtime、当前时间或其他上下文
  推断 / 补造授权、发布时间、采集时间、availability 或 OOS 语义；
- **复用而不复制契约**：manifest 字段 / 允许键 / 必填项 / 格式词表**直接复用** GOLD-011 的
  ``MANIFEST_REQUIRED_FIELDS`` / ``ALLOWED_MANIFEST_KEYS`` / ``FILE_ENTRY_REQUIRED_FIELDS`` /
  ``ALLOWED_FILE_ENTRY_KEYS`` / ``SUPPORTED_FILE_FORMATS`` / ``INBOX_SCHEMA_VERSION`` 与
  ``evidence-intake-v1`` 契约；引用校验复用
  :func:`src.evidence.validation.valid_reference`，逐行预检复用
  :func:`src.evidence.validation.assess_row`，**不复制、不降低**任何资格规则与阈值；
- **默认 dry-run**：只生成**确定性、脱敏**的 manifest 预览与文件摘要，**零写入**；只有显式
  ``--out`` 才允许写 manifest，且 ``--out`` **必须**等于目标 package 目录内的固定
  ``manifest.json``（写到其它任意路径一律 fail-closed）；
- **只读既有 evidence**：只读取 operator 明确指定的现有文件；拒绝绝对路径 / ``..`` 路径穿越 /
  符号链接 / 目录 / ``manifest.json`` 自引用 / 未支持扩展或格式 / 重复文件 / 大小写冲突路径 /
  包目录之外的路径 / 包内未声明文件；**绝不**移动、删除、改名或改写原始 evidence；
- **内容级 SHA-256 且确定性排序**：每个文件按**原始字节**计算 SHA-256；``manifest.files`` 按规范化
  相对路径**确定性排序**；同一输入 + 同一显式元数据 → **byte-stable** 的 manifest；
  文件 mtime / ctime / 生成时间**绝不**进入内容身份或证据语义（manifest 里根本没有时间字段）；
- **写入前同源预检**：写 manifest 前先对所有指定文件执行与 GOLD-005 / GOLD-011 **同源**的本地逐行
  预检（``read_input_file`` + ``assess_row``），输出 ``accepted`` / ``quarantined`` /
  ``not_oos_eligible`` 与稳定原因码；预检结果**不写入** evidence 原始行，也**绝不**把预检通过
  宣称为“授权已人工确认”或“数据资格通过”；
- **原子写 + 单实例锁 + 不静默覆盖**：``--out`` 时先取 GOLD-010 单实例锁（锁文件**刻意放在 package
  目录之外**，避免成为包内未声明文件），再核验既有 manifest：不存在则创建；逐字节一致→**幂等**；
  内容不同一律 :class:`EvidencePackageConflictError`（``MANIFEST_CONFLICT``）fail-closed，
  **没有** ``--force`` / ``--overwrite``；
- **凭据与敏感值**：``authorization_reference`` / ``source`` / ``notes`` 走既有
  :mod:`src.common.redaction` 与 :func:`valid_reference`；敏感键名 / 疑似凭据 blob / 可被脱敏规则
  命中的取值**一律拒绝**（宁拒绝不静默改写）；CLI 错误输出统一脱敏，``--json`` 时 stdout 纯 JSON；
- **持续硬编码 blocker**：所有 artifact 恒为 ``blocker_active=true`` /
  ``human_gate_required=true`` / ``requires_human_action=true`` /
  ``data_qualification_passed=false`` / ``phase_transition_allowed=false`` /
  ``auto_intake_allowed=false`` / ``writes_database=false``；**生成 manifest ≠ 授权已核验 ≠ 资格通过
  ≠ 可提交 L3**，``PHASE3_3_DATA`` **保持 BLOCKED**。

入口：``scripts/evidence_package.py``（默认 dry-run；``--out`` 是**唯一**写开关，且只允许写
``<package-dir>/manifest.json``；**没有**任何 intake / 数据库参数）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final

from src.common import hashing
from src.common.redaction import is_sensitive_key, redact_secrets, safe_text, safe_url
from src.evidence import readiness_runner as runner
from src.evidence.contracts import (
    EVIDENCE_CONTRACT_VERSION,
    EXAMPLE_MARKER_VALUES,
    QUARANTINE_REASON_CODES,
    EvidenceScope,
)
from src.evidence.inbox import (
    ALLOWED_FILE_ENTRY_KEYS,
    ALLOWED_MANIFEST_KEYS,
    FILE_ENTRY_REQUIRED_FIELDS,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    MANIFEST_REQUIRED_FIELDS,
    MAX_PATH_CHARS,
    SUPPORTED_FILE_FORMATS,
    InboxReasonCode,
)
from src.evidence.intake import read_input_file, resolve_format
from src.evidence.readiness_watch import MAX_CODE_CHARS, atomic_write_text
from src.evidence.validation import assess_row, valid_reference
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_INPUT_INVALID",
    "EXIT_LOCK_CONFLICT",
    "EXIT_MANIFEST_CONFLICT",
    "EXIT_OK",
    "EXIT_UNUSABLE",
    "EVIDENCE_TYPES",
    "MANIFEST_LOCK_SUFFIX",
    "MAX_FILE_COUNT",
    "MAX_NOTES_CHARS",
    "MAX_NOTES_INPUT_CHARS",
    "MAX_REFERENCE_CHARS",
    "MAX_SEMANTICS_CHARS",
    "MAX_SOURCE_CHARS",
    "PACKAGE_BUILDER_EXECUTION_MODE",
    "PACKAGE_BUILDER_KIND",
    "PACKAGE_BUILDER_NOTE",
    "PACKAGE_BUILDER_REPORT_NAME",
    "PACKAGE_BUILDER_SCHEMA_VERSION",
    "PREFLIGHT_NOTE",
    "EvidencePackageArgumentError",
    "EvidencePackageCode",
    "EvidencePackageConflictError",
    "EvidencePackageError",
    "EvidencePackageInputError",
    "EvidencePackageInternalError",
    "EvidencePackagePathError",
    "EvidencePackagePreview",
    "EvidencePackageWriteError",
    "ManifestFileEntry",
    "ManifestWriteStatus",
    "PreflightOutcome",
    "RowPreflight",
    "build_manifest_preview",
    "exit_code_for",
    "manifest_bytes",
    "manifest_digest",
    "preflight_rows",
    "render_package_summary",
    "run_package_builder",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
PACKAGE_BUILDER_SCHEMA_VERSION: Final[int] = 1
#: 预览文档标识 / 报告标识（稳定，供脚本与人工解析）
PACKAGE_BUILDER_KIND: Final[str] = "evidence_package_manifest_preview"
PACKAGE_BUILDER_REPORT_NAME: Final[str] = PACKAGE_BUILDER_KIND
#: 执行模式（**纯本地 + 显式人工输入 + 默认 dry-run**；硬编码）
PACKAGE_BUILDER_EXECUTION_MODE: Final[str] = "LOCAL_EVIDENCE_PACKAGE_MANIFEST_BUILDER"
#: 单实例锁后缀（与 GOLD-010 / 011 / 016 同一约定）
MANIFEST_LOCK_SUFFIX: Final[str] = ".lock"

#: 允许的证据类别（与 ``EvidenceScope`` **同源**，不另造词表）
EVIDENCE_TYPES: Final[tuple[str, ...]] = tuple(scope.value for scope in EvidenceScope)
_SCOPES_BY_TYPE: Final[dict[str, EvidenceScope]] = {scope.value: scope for scope in EvidenceScope}

#: ``source`` 声明长度上限
MAX_SOURCE_CHARS: Final[int] = 100
#: ``time_semantics`` / ``availability_semantics`` 声明长度上限
MAX_SEMANTICS_CHARS: Final[int] = 200
#: ``authorization_reference`` 长度上限（与 GOLD-011 的入参上限同口径）
MAX_REFERENCE_CHARS: Final[int] = 1_000
#: ``notes`` 写入 manifest 的长度上限
MAX_NOTES_CHARS: Final[int] = 400
#: ``notes`` 输入长度上限（超出 → fail-closed，绝不静默截断成假事实）
MAX_NOTES_INPUT_CHARS: Final[int] = 4_000
#: 单个 package 的文件数量上限（防止异常输入撑爆 manifest）
MAX_FILE_COUNT: Final[int] = 64
#: 文件名展示上限（与 GOLD-011 的路径展示上限同口径）
MAX_ENTRY_NAME_CHARS: Final[int] = MAX_PATH_CHARS

_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
#: 文件名分词（与 GOLD-011 的示例 / 合成词表判定同口径）
_NAME_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
#: 疑似凭据 blob（长 base64 / hex 串；宁拒绝不静默改写）
_BLOB_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9+/=_-]{40,}\Z")
#: 控制字符（含 NUL / 换行）：声明文本里一律拒绝
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
#: 逐行隔离类原因码（值形态；与 Evidence Gateway 的 ``QUARANTINE_REASON_CODES`` 同源）
_QUARANTINE_VALUES: Final[frozenset[str]] = frozenset(
    code.value for code in QUARANTINE_REASON_CODES
)

#: 固定说明：本工具的边界（**硬编码**，不随任何输入变化）
PACKAGE_BUILDER_NOTE: Final[str] = (
    "本工具只把**人工显式提供**的授权 / 时间 / availability / OOS 元数据与**人工指定的现有 "
    "evidence 文件**整理成 GOLD-011 inbox 可直接消费的 `manifest.json`：不推断授权、不伪造时间、"
    "不联网、不写数据库、不移动 / 删除 / 改写任何原始 evidence、不自动 intake；"
    "`blocker_active` / `human_gate_required` / `requires_human_action` 恒为 true，"
    "`data_qualification_passed` / `phase_transition_allowed` 恒为 false；"
    "`PHASE3_3_DATA` **保持 BLOCKED**。"
)
#: 固定说明：预检只表示“可以进入人工确认队列”，**不是**授权确认、**不是**资格通过
PREFLIGHT_NOTE: Final[str] = (
    "写入前预检只复用 Evidence Gateway 的逐行机械校验（`assess_row`，`evidence-intake-v1`）："
    "输出 `accepted` / `quarantined` / `not_oos_eligible` 与稳定原因码；它**不表示**授权已核验、"
    "**不表示**数据资格通过，也**不代表**可以提交 L3；真实资格仍须业务方授权 + 人工核验。"
)
#: 固定说明：manifest 里没有时间字段（byte-stable 与“不用操作时间冒充证据时间”）
MANIFEST_TIME_FREE_NOTE: Final[str] = (
    "manifest **不含**任何时间字段（没有 generated_at / 没有文件 mtime / ctime）："
    "同一输入与同一显式元数据必须得到 byte-stable 的 manifest；报告里的 `generated_at` 只是"
    "**审计操作时间**，绝不是证据时间，也不会被用来推导任何证据时间。"
)


class ManifestWriteStatus(StrEnum):
    """manifest 写入结论（``DRY_RUN`` 表示**零写入**）。"""

    DRY_RUN = "DRY_RUN_NOT_WRITTEN"
    WRITTEN = "WRITTEN"
    IDEMPOTENT_UNCHANGED = "IDEMPOTENT_UNCHANGED"


class PreflightOutcome(StrEnum):
    """逐行预检的**描述性**结论（**不是**资格判定，也**不是**授权确认）。"""

    NO_ROWS = "NO_ROWS"
    HAS_QUARANTINED_ROWS = "HAS_QUARANTINED_ROWS"
    NO_ACCEPTABLE_ROWS = "NO_ACCEPTABLE_ROWS"
    ACCEPTED_WITHOUT_OOS = "ACCEPTED_WITHOUT_OOS"
    ACCEPTED_WITH_OOS = "ACCEPTED_WITH_OOS"


class EvidencePackageCode(StrEnum):
    """manifest builder 的稳定原因码（任何一条都意味着 **fail-closed**，绝不写出 manifest）。

    note:
        与 GOLD-011 inbox / ``evidence-intake-v1`` 同义的原因码**直接沿用**其字符串
        （``MANIFEST_FIELD_MISSING`` / ``EVIDENCE_FILE_PATH_UNSAFE`` /
        ``SENSITIVE_VALUE_DETECTED`` …），不另造第二套词表。
    """

    # ---- 复用 GOLD-011 inbox 的稳定原因码（值完全一致）--------------------
    MANIFEST_FIELD_MISSING = InboxReasonCode.MANIFEST_FIELD_MISSING.value
    MANIFEST_UNREADABLE = InboxReasonCode.MANIFEST_UNREADABLE.value
    MANIFEST_SCHEMA_UNSUPPORTED = InboxReasonCode.MANIFEST_SCHEMA_UNSUPPORTED.value
    EVIDENCE_TYPE_UNSUPPORTED = InboxReasonCode.EVIDENCE_TYPE_UNSUPPORTED.value
    PACKAGE_LAYOUT_INVALID = InboxReasonCode.PACKAGE_LAYOUT_INVALID.value
    EVIDENCE_FILE_ENTRY_INVALID = InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value
    EVIDENCE_FILE_PATH_UNSAFE = InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value
    EVIDENCE_FILE_MISSING = InboxReasonCode.EVIDENCE_FILE_MISSING.value
    EVIDENCE_FILE_UNREADABLE = InboxReasonCode.EVIDENCE_FILE_UNREADABLE.value
    EVIDENCE_FILE_UNDECLARED = InboxReasonCode.EVIDENCE_FILE_UNDECLARED.value
    SENSITIVE_VALUE_DETECTED = InboxReasonCode.SENSITIVE_VALUE_DETECTED.value
    SYNTHETIC_EVIDENCE = InboxReasonCode.SYNTHETIC_EVIDENCE.value
    AUTHORIZATION_REFERENCE_INVALID = InboxReasonCode.AUTHORIZATION_REFERENCE_INVALID.value
    TIME_SEMANTICS_MISSING = InboxReasonCode.TIME_SEMANTICS_MISSING.value
    AVAILABILITY_SEMANTICS_MISSING = InboxReasonCode.AVAILABILITY_SEMANTICS_MISSING.value
    AVAILABILITY_UNPROVEN = InboxReasonCode.AVAILABILITY_UNPROVEN.value
    CONTAINS_QUARANTINED_ROWS = InboxReasonCode.CONTAINS_QUARANTINED_ROWS.value
    NO_ACCEPTABLE_ROWS = InboxReasonCode.NO_ACCEPTABLE_ROWS.value
    ROW_UNREADABLE = InboxReasonCode.ROW_UNREADABLE.value
    # ---- builder 专有：输入 / 路径 / 写入门禁 -----------------------------
    DUPLICATE_FILE_PATH = "DUPLICATE_FILE_PATH"
    FILE_NAME_CASE_CONFLICT = "FILE_NAME_CASE_CONFLICT"
    MANIFEST_SELF_REFERENCE = "MANIFEST_SELF_REFERENCE"
    UNSUPPORTED_FILE_FORMAT = "UNSUPPORTED_FILE_FORMAT"
    TOO_MANY_FILES = "TOO_MANY_FILES"
    METADATA_VALUE_INVALID = "METADATA_VALUE_INVALID"
    NOTES_TOO_LONG = "NOTES_TOO_LONG"
    NO_INPUT_FILES = "NO_INPUT_FILES"
    PACKAGE_DIR_UNUSABLE = "PACKAGE_DIR_UNUSABLE"
    MANIFEST_OUT_NOT_TARGET = "MANIFEST_OUT_NOT_TARGET"
    MANIFEST_CONFLICT = "MANIFEST_CONFLICT"
    MANIFEST_WRITE_FAILED = "MANIFEST_WRITE_FAILED"
    MANIFEST_VERIFY_FAILED = "MANIFEST_VERIFY_FAILED"
    PACKAGE_STALE = "PACKAGE_STALE"


class EvidencePackageError(RuntimeError):
    """manifest builder 失败（**fail-closed**：调用方按退出码处理，不得假设已写出任何文件）。"""


class EvidencePackageArgumentError(EvidencePackageError):
    """参数错误（缺必填元数据 / 文件清单为空 / 时点缺时区 / 非法取值）。"""


class EvidencePackagePathError(EvidencePackageError):
    """package 目录或输出路径不可用（含 ``--out`` 不是 ``<package-dir>/manifest.json``）。"""


class EvidencePackageInputError(EvidencePackageError):
    """输入未通过 fail-closed 校验（路径安全 / 存在性 / 重复 / 未声明 / 敏感值 …）。

    Attributes:
        codes: 稳定原因码（去重排序），便于脚本与人工稳定解析。
        details: 已脱敏的逐项说明（**不含** evidence 正文与凭据值）。
    """

    def __init__(self, codes: Sequence[str], details: Sequence[str] = ()) -> None:
        self.codes: tuple[str, ...] = tuple(sorted({str(code) for code in codes}))
        self.details: tuple[str, ...] = tuple(str(item) for item in details)
        summary = "、".join(self.codes[:8]) if self.codes else "UNKNOWN"
        super().__init__(
            "manifest 未生成（fail-closed；零写入、绝不推断授权 / 时间 / availability / OOS）："
            f"{len(self.codes)} 类输入问题；原因码：{summary}"
        )


class EvidencePackageConflictError(EvidencePackageError):
    """既有 ``manifest.json`` 与本次规范化内容**不同**（``MANIFEST_CONFLICT``，绝静默覆盖）。"""


class EvidencePackageWriteError(EvidencePackageError):
    """manifest 原子写失败或写后自检失败（**fail-closed**）。"""


class EvidencePackageInternalError(EvidencePackageError):
    """内部自检失败（manifest 不满足 GOLD-011 契约，说明代码与契约失配）。"""


# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）----------
#: dry-run 预览已生成，或 manifest 已写入 / 幂等未变（**仍不是**资格通过）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数 / 配置错误（缺必填元数据、文件清单为空、时点缺时区等）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: package 目录 / 输出路径不可用（含 ``--out`` 不是固定 ``manifest.json``、原子写失败）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: 输入未通过 fail-closed 校验（路径不安全 / 缺失 / 重复 / 未声明 / 敏感值 / 格式不支持）
EXIT_INPUT_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 既有 manifest 与本次内容不同（拒绝静默覆盖；需人工新建 package 目录 / 新内容身份）
EXIT_MANIFEST_CONFLICT: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一处 manifest 写入正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_PACKAGE_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (EvidencePackageConflictError, EXIT_MANIFEST_CONFLICT),
    (EvidencePackageArgumentError, EXIT_CONFIG_ERROR),
    (EvidencePackagePathError, EXIT_UNUSABLE),
    (EvidencePackageWriteError, EXIT_UNUSABLE),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (EvidencePackageInputError, EXIT_INPUT_INVALID),
    (EvidencePackageInternalError, EXIT_INPUT_INVALID),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为“输入非法”）。"""
    for kind, code in _PACKAGE_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_INPUT_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（原因文本 / 来源名 / 文件名共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = MAX_PATH_CHARS) -> str:
    """路径类展示：脱敏 + 截断（绝不回显凭据）。"""
    return _safe(value, max_chars=max_chars)


def _safe_reference(value: str, *, max_chars: int = MAX_PATH_CHARS) -> str:
    """引用类展示：URL 去掉 userinfo / query / fragment，再脱敏 + 截断。"""
    return safe_text(safe_url(value, max_chars=max_chars), max_chars=max_chars)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """manifest 的**唯一**内容摘要口径：对将要落盘的字节计算 SHA-256（可逐字节复核）。"""
    return hashing.sha256_bytes(manifest_bytes(manifest))


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """manifest 的**唯一**落盘字节形态（缩进 2 + 排序键 + 结尾换行 + UTF-8）。"""
    text = json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def _same_path(left: Path, right: Path) -> bool:
    """两个路径是否指向同一处（Windows 大小写不敏感；不做任何真实 IO 探测）。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - 极端路径解析失败时退化为字符串比较
        return str(left) == str(right)


def _is_hex64(value: object) -> bool:
    """是否是 64 位小写十六进制摘要（内容寻址字段的**唯一**合法形态）。"""
    return isinstance(value, str) and bool(_HEX64.match(value))


def _require_aware(moment: object, *, field_name: str) -> datetime:
    """要求带时区的 ``datetime``（naive → 参数错误，绝不默认 UTC 猜测）。"""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise EvidencePackageArgumentError(f"{field_name} 必须是带时区的 datetime")
    return moment


def _require_bool(raw: object, *, field_name: str) -> bool:
    """严格布尔（缺字段 / 非布尔 → fail-closed，绝不把字符串当布尔）。"""
    if not isinstance(raw, bool):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.MANIFEST_FIELD_MISSING.value}：{field_name} 必须显式给出布尔值"
            "（本工具绝不推断授权 / 时间 / availability / OOS 语义）"
        )
    return raw


def _dedupe_reasons(
    reasons: Sequence[tuple[str, str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把 ``(原因码, 说明)`` 去重并**确定性排序**（同一码只保留最短说明）。"""
    codes: set[str] = set()
    texts: dict[str, str] = {}
    for code, text in reasons:
        key = str(code)
        codes.add(key)
        previous = texts.get(key)
        if previous is None or len(text) < len(previous):
            texts[key] = str(text)
    ordered = tuple(sorted(codes))
    return ordered, tuple(texts[code] for code in ordered)


def _looks_like_credential(text: str) -> bool:
    """取值是否会被既有脱敏规则命中 / 是否为疑似凭据 blob（**宁拒绝不静默改写**）。"""
    return redact_secrets(text) != text or bool(_BLOB_LIKE_PATTERN.match(text))


def _clean_evidence_type(raw: object) -> tuple[str, EvidenceScope]:
    """``evidence_type`` **必须**是显式的 ``author`` / ``news``（大小写敏感，不做静默归一）。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.MANIFEST_FIELD_MISSING.value}：必须显式给出 evidence_type "
            f"{list(EVIDENCE_TYPES)}（本工具绝不从文件名 / 正文推断证据类别）"
        )
    if text not in _SCOPES_BY_TYPE:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.EVIDENCE_TYPE_UNSUPPORTED.value}：evidence_type 必须是 "
            f"{list(EVIDENCE_TYPES)} 之一（大小写敏感）；实际：{_safe(raw, max_chars=40)}"
        )
    return text, _SCOPES_BY_TYPE[text]


def _clean_declared_text(
    raw: object, *, field_name: str, code: EvidencePackageCode, max_chars: int
) -> str:
    """声明类文本：非空 / 无控制字符 / 限长 / 不含疑似凭据（任一不满足即 fail-closed）。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise EvidencePackageArgumentError(
            f"{code.value}：必须显式给出 {field_name}"
            "（人工显式提供；本工具绝不从上下文推断 / 补造）"
        )
    if _CONTROL_CHARS.search(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：{field_name} 不得包含控制字符"
        )
    if len(text) > max_chars:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：{field_name} 超过 "
            f"{max_chars} 字符（fail-closed，绝不静默截断成假事实）"
        )
    if _looks_like_credential(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value}：{field_name} 疑似凭据 / "
            "敏感值（本工具拒绝写入 manifest，绝不静默改写）；请去掉凭据后重新声明"
        )
    return text


def _clean_source(raw: object) -> str:
    """``source``：非敏感来源名（拒绝凭据类键名与路径分隔符，**不采集任何凭据**）。"""
    text = _clean_declared_text(
        raw,
        field_name="source",
        code=EvidencePackageCode.METADATA_VALUE_INVALID,
        max_chars=MAX_SOURCE_CHARS,
    )
    if is_sensitive_key(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value}：source 看起来是凭据类键名"
            "（api_key / token / secret 等），本工具**绝不**采集凭据"
        )
    if any(separator in text for separator in ("/", "\\", ":")):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：source 必须是来源名"
            "（不得包含路径分隔符或盘符）"
        )
    return text


def _clean_reference(raw: object) -> str:
    """``authorization_reference``：走既有 :func:`valid_reference` + 敏感值拒绝。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.MANIFEST_FIELD_MISSING.value}：必须显式给出 "
            "authorization_reference（本工具绝不推断授权）"
        )
    if _CONTROL_CHARS.search(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：authorization_reference "
            "不得包含控制字符"
        )
    if len(text) > MAX_REFERENCE_CHARS:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：authorization_reference 超过 "
            f"{MAX_REFERENCE_CHARS} 字符"
        )
    if is_sensitive_key(text) or _looks_like_credential(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value}：authorization_reference 疑似"
            "凭据 / 敏感值（拒绝写入 manifest，绝不静默改写）"
        )
    if not valid_reference(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.AUTHORIZATION_REFERENCE_INVALID.value}："
            "authorization_reference 必须是 https URL 或 docs/legal/ 内现存许可文件路径"
        )
    return text


def _clean_notes(raw: object) -> str | None:
    """可选 ``notes``：**脱敏 + 限长**；超长输入 fail-closed（不静默截断成假事实）。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.NOTES_TOO_LONG.value}：notes 必须是字符串（或缺省）"
        )
    text = raw.strip()
    if not text:
        return None
    if "\x00" in text:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.METADATA_VALUE_INVALID.value}：notes 不得包含 NUL"
        )
    if len(text) > MAX_NOTES_INPUT_CHARS:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.NOTES_TOO_LONG.value}：notes 超过 {MAX_NOTES_INPUT_CHARS} 字符"
            "（fail-closed，绝不静默截断）"
        )
    if _looks_like_credential(text):
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value}：notes 疑似包含凭据"
            "（本工具拒绝写入 manifest，绝不静默改写）"
        )
    return safe_text(text, max_chars=MAX_NOTES_CHARS)


def _clean_file_list(raw_files: Sequence[str]) -> tuple[str, ...]:
    """要纳入 package 的相对文件清单：**必须**由 operator 显式给出且非空。"""
    items: list[object] = [raw_files] if isinstance(raw_files, str) else list(raw_files)
    if not items:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.NO_INPUT_FILES.value}：必须显式给出至少一个证据文件"
            "（--file <相对文件名>；本工具绝不自动扫描目录、绝不猜测该纳入哪些文件）"
        )
    if len(items) > MAX_FILE_COUNT:
        raise EvidencePackageArgumentError(
            f"{EvidencePackageCode.TOO_MANY_FILES.value}：文件数量不得超过 {MAX_FILE_COUNT}"
        )
    return tuple(str(item) for item in items)


def _safe_relative_name(raw: str) -> str | None:
    """把声明的文件名规范化为 package 目录内的**单级文件名**；不安全返回 ``None``。

    与 GOLD-011 的 ``_safe_relative_name`` **同口径**：拒绝绝对路径（POSIX / Windows / UNC）、
    盘符、``..``、多级路径、含 NUL 的名字。只允许 package 目录下的**直接子文件**
    （不猜、不放宽，故不存在路径穿越与越界读取）。
    """
    clean = raw.strip().replace("\\", "/")
    if not clean or clean in {".", ".."} or "\x00" in clean:
        return None
    if PurePosixPath(clean).is_absolute() or PureWindowsPath(clean).is_absolute():
        return None
    if PureWindowsPath(clean).drive:
        return None
    parts = PurePosixPath(clean).parts
    if len(parts) != 1 or parts[0] in {".", ".."}:
        return None
    return parts[0]


def _synthetic_by_name(package_dir_name: str, file_names: Sequence[str]) -> bool:
    """package 目录名 / 文件名是否命中示例 / 合成词表（**仅用于排除**，绝不用于放行）。"""
    for name in (package_dir_name, *file_names):
        tokens = {token for token in _NAME_TOKEN_SPLIT.split(name.casefold()) if token}
        if tokens & EXAMPLE_MARKER_VALUES:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ManifestFileEntry:
    """一个将要写入 manifest 的证据文件（摘要来自**真实字节**，不含任何时间字段）。"""

    path: str
    format: str
    sha256: str
    size_bytes: int

    def to_manifest_entry(self) -> dict[str, str]:
        """GOLD-011 manifest ``files`` 条目（只含契约允许的键）。"""
        return {"path": self.path, "sha256": self.sha256, "format": self.format}

    def to_dict(self) -> dict[str, Any]:
        """报告用结构（额外带**内容派生**的字节数；不是证据语义、不含 mtime）。"""
        return {
            "path": self.path,
            "format": self.format,
            "sha256": self.sha256,
            "bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class RowPreflight:
    """逐行预检汇总（**只计数与稳定原因码**；不含正文与原始值）。"""

    rows: int = 0
    accepted: int = 0
    quarantined: int = 0
    not_oos_eligible: int = 0
    synthetic: bool = False
    code_counts: tuple[tuple[str, int], ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()
    #: 逐文件计数：(path, rows, accepted, quarantined, not_oos_eligible)
    per_file: tuple[tuple[str, int, int, int, int], ...] = ()

    @property
    def outcome(self) -> PreflightOutcome:
        """描述性结论（**不是**资格判定、**不是**授权确认）。"""
        if self.rows == 0:
            return PreflightOutcome.NO_ROWS
        if self.quarantined:
            return PreflightOutcome.HAS_QUARANTINED_ROWS
        if self.accepted == 0:
            return PreflightOutcome.NO_ACCEPTABLE_ROWS
        if self.not_oos_eligible:
            return PreflightOutcome.ACCEPTED_WITHOUT_OOS
        return PreflightOutcome.ACCEPTED_WITH_OOS

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "not_oos_eligible": self.not_oos_eligible,
            "outcome": self.outcome.value,
            "synthetic": self.synthetic,
            "reason_code_counts": {code: count for code, count in self.code_counts},
            "per_file": [
                {
                    "path": path,
                    "rows": rows,
                    "accepted": accepted,
                    "quarantined": quarantined,
                    "not_oos_eligible": not_oos,
                }
                for path, rows, accepted, quarantined, not_oos in self.per_file
            ],
            "global_reasons": [text for _code, text in self.reasons],
        }


def _resolve_files(
    root: Path, names: Sequence[str]
) -> tuple[tuple[ManifestFileEntry, ...], list[tuple[str, str]]]:
    """逐文件解析（**只读**）：路径安全 → 存在性 → 常规文件 → 未支持格式 → 内容 SHA-256。

    处理顺序与 ``names`` 的**排序**一致（确定性）；``manifest.files`` 最终按规范化相对路径排序。
    绝不跟随符号链接、绝不越界读取、绝不写入 / 移动 / 删除任何文件，也绝不读取 mtime。
    """
    reasons: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    entries: list[ManifestFileEntry] = []
    for raw in sorted(names):
        name = _safe_relative_name(raw)
        if name is None:
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"文件不得是绝对路径 / 上级目录 / 多级路径：{_safe(raw)}",
                )
            )
            continue
        if name == MANIFEST_FILE_NAME:
            reasons.append(
                (
                    EvidencePackageCode.MANIFEST_SELF_REFERENCE.value,
                    f"{MANIFEST_FILE_NAME} 是 manifest 自身，不得作为证据文件声明",
                )
            )
            continue
        if len(name) > MAX_ENTRY_NAME_CHARS:
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_ENTRY_INVALID.value,
                    f"文件名超过 {MAX_ENTRY_NAME_CHARS} 字符：{_safe(name)}",
                )
            )
            continue
        folded = name.casefold()
        previous = seen.get(folded)
        if previous is not None:
            code = (
                EvidencePackageCode.DUPLICATE_FILE_PATH
                if previous == name
                else EvidencePackageCode.FILE_NAME_CASE_CONFLICT
            )
            reasons.append(
                (
                    code.value,
                    f"重复 / 大小写冲突的文件声明：{_safe(name)}（已声明 {_safe(previous)}）",
                )
            )
            continue
        seen[folded] = name
        candidate = root / name
        if candidate.is_symlink():
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"符号链接不被跟随（fail-closed）：{_safe(name)}",
                )
            )
            continue
        if candidate.is_dir():
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"必须是常规文件、不得是目录：{_safe(name)}",
                )
            )
            continue
        if not candidate.is_file():
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_MISSING.value,
                    f"指定的证据文件不存在于 package 目录内：{_safe(name)}",
                )
            )
            continue
        if not _same_path(candidate.parent, root):
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"文件必须位于 package 目录内（拒绝包目录之外）：{_safe(name)}",
                )
            )
            continue
        try:
            fmt = resolve_format(candidate, "auto")
        except ValueError as exc:
            reasons.append(
                (
                    EvidencePackageCode.UNSUPPORTED_FILE_FORMAT.value,
                    f"{_safe(name)}：{_safe(exc, max_chars=120)}",
                )
            )
            continue
        if fmt not in SUPPORTED_FILE_FORMATS:
            reasons.append(
                (
                    EvidencePackageCode.UNSUPPORTED_FILE_FORMAT.value,
                    f"{_safe(name)}：format 必须是 {list(SUPPORTED_FILE_FORMATS)}",
                )
            )
            continue
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_UNREADABLE.value,
                    f"文件不可读（{type(exc).__name__}）：{_safe(name)}",
                )
            )
            continue
        entries.append(
            ManifestFileEntry(
                path=name,
                format=fmt,
                sha256=hashing.sha256_bytes(payload),
                size_bytes=len(payload),
            )
        )
    entries.sort(key=lambda item: item.path)
    return tuple(entries), reasons


def _undeclared_reasons(root: Path, declared: Sequence[str]) -> list[tuple[str, str]]:
    """package 目录内的**未声明**条目一律 fail-closed（与 GOLD-011 scanner 同口径）。"""
    reasons: list[tuple[str, str]] = []
    declared_names = set(declared)
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [
            (
                EvidencePackageCode.PACKAGE_DIR_UNUSABLE.value,
                f"package 目录不可读（{type(exc).__name__}）：{_safe_path(root)}",
            )
        ]
    for child in children:
        name = child.name
        if name == MANIFEST_FILE_NAME or name in declared_names:
            continue
        if child.is_symlink():
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"package 内不允许符号链接（fail-closed）：{_safe(name)}",
                )
            )
        elif child.is_dir():
            reasons.append(
                (
                    EvidencePackageCode.PACKAGE_LAYOUT_INVALID.value,
                    f"package 内不允许子目录：{_safe(name)}",
                )
            )
        else:
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_UNDECLARED.value,
                    f"package 内存在未声明的文件（fail-closed）：{_safe(name)}",
                )
            )
    return reasons


def preflight_rows(
    root: Path,
    entries: Sequence[ManifestFileEntry],
    *,
    scope: EvidenceScope,
    moment: datetime,
) -> RowPreflight:
    """用 **Evidence Gateway 的逐行机械校验**做只读预检（不落库、不联网、不写文件）。

    复用 :func:`src.evidence.validation.assess_row`（``evidence-intake-v1`` 契约），因此隔离 /
    授权 / 时间 / availability 口径与 ``intake`` **完全同源**；``DUPLICATE`` / ``IDENTITY_CONFLICT``
    （需要读库）留给**显式 intake** 处理。预检结果**绝不写回** evidence 原始行，也**绝不**被宣称
    为授权确认或资格通过。
    """
    rows = accepted = quarantined = not_oos = 0
    synthetic = False
    counts: Counter[str] = Counter()
    reasons: list[tuple[str, str]] = []
    per_file: list[tuple[str, int, int, int, int]] = []
    for entry in entries:
        file_rows = file_accepted = file_quarantined = file_not_oos = 0
        try:
            _input_file, input_rows = read_input_file(
                root / entry.path, requested_format=entry.format
            )
            for index, row in enumerate(input_rows):
                rows += 1
                file_rows += 1
                if row.data is None:
                    quarantined += 1
                    file_quarantined += 1
                    counts[EvidencePackageCode.ROW_UNREADABLE.value] += 1
                    continue
                assessment = assess_row(
                    row.data, scope=scope, index=index, row_number=row.row_number, moment=moment
                )
                codes = [code.value for code in assessment.reason_codes]
                for code in codes:
                    counts[code] += 1
                if EvidencePackageCode.SYNTHETIC_EVIDENCE.value in codes:
                    synthetic = True
                if any(code in _QUARANTINE_VALUES for code in codes):
                    quarantined += 1
                    file_quarantined += 1
                    continue
                accepted += 1
                file_accepted += 1
                if not assessment.oos_eligible:
                    not_oos += 1
                    file_not_oos += 1
        except Exception as exc:  # 任何单文件异常都必须 fail-closed，且异常正文不入 artifact
            reasons.append(
                (
                    EvidencePackageCode.EVIDENCE_FILE_UNREADABLE.value,
                    f"证据文件预检失败（{type(exc).__name__}）：{_safe(entry.path)}",
                )
            )
        per_file.append((entry.path, file_rows, file_accepted, file_quarantined, file_not_oos))
    return RowPreflight(
        rows=rows,
        accepted=accepted,
        quarantined=quarantined,
        not_oos_eligible=not_oos,
        synthetic=synthetic,
        code_counts=tuple(sorted(counts.items())),
        reasons=tuple(reasons),
        per_file=tuple(per_file),
    )


@dataclass(frozen=True, slots=True)
class EvidencePackagePreview:
    """一次 manifest 预览 / 写入的**稳定**结果（报告与人类可读摘要的唯一事实来源）。

    Attributes:
        generated_at: 本次构建的**审计操作时间**（**不是**证据时间；manifest 里没有时间字段）。
        manifest: 将要 / 已经写入的 manifest（键只含 GOLD-011 允许的契约字段）。
        manifest_sha256: 对 ``manifest`` 的**落盘字节**计算的 SHA-256（可逐字节复核）。
        write_status: ``DRY_RUN_NOT_WRITTEN`` / ``WRITTEN`` / ``IDEMPOTENT_UNCHANGED``。
    """

    generated_at: datetime
    package_dir: str
    package_name: str
    evidence_type: str
    source: str
    authorization_reference: str
    time_semantics: str
    availability_semantics: str
    historical_oos_applicable: bool
    manifest: dict[str, Any]
    manifest_sha256: str
    files: tuple[ManifestFileEntry, ...]
    preflight: RowPreflight
    warnings: tuple[str, ...] = ()
    write_status: ManifestWriteStatus = ManifestWriteStatus.DRY_RUN
    written_path: str | None = None
    notes: tuple[str, ...] = (PACKAGE_BUILDER_NOTE, PREFLIGHT_NOTE, MANIFEST_TIME_FREE_NOTE)

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return PACKAGE_BUILDER_KIND

    @property
    def blocker_active(self) -> bool:
        """恒为 ``True``：真实数据资格仍未解除（**硬编码**）。"""
        return True

    @property
    def human_gate_required(self) -> bool:
        """恒为 ``True``：Phase 切换仍须 **L3 人工 Gate**。"""
        return True

    @property
    def requires_human_action(self) -> bool:
        """恒为 ``True``：预检通过只表示进入**人工确认**队列。"""
        return True

    @property
    def data_qualification_passed(self) -> bool:
        """恒为 ``False``：生成 manifest **不是**资格通过。"""
        return False

    @property
    def phase_transition_allowed(self) -> bool:
        """恒为 ``False``：本工具**不**授权 Phase 切换。"""
        return False

    @property
    def auto_intake_allowed(self) -> bool:
        """恒为 ``False``：本工具**不**自动 intake、**不**写数据库。"""
        return False

    @property
    def writes_database(self) -> bool:
        """恒为 ``False``：本工具零数据库写入。"""
        return False

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段显式且恒定）。"""
        return {
            "kind": PACKAGE_BUILDER_KIND,
            "report": PACKAGE_BUILDER_REPORT_NAME,
            "schema_version": PACKAGE_BUILDER_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "execution_mode": PACKAGE_BUILDER_EXECUTION_MODE,
            "generated_at": self.generated_at.isoformat(),
            "package_dir": self.package_dir,
            "package_name": self.package_name,
            "manifest_file_name": MANIFEST_FILE_NAME,
            "manifest_sha256": self.manifest_sha256,
            "manifest": self.manifest,
            "metadata": {
                "evidence_type": self.evidence_type,
                "source": self.source,
                "authorization_reference": _safe_reference(self.authorization_reference),
                "time_semantics": self.time_semantics,
                "availability_semantics": self.availability_semantics,
                "historical_oos_applicable": self.historical_oos_applicable,
            },
            "files": [entry.to_dict() for entry in self.files],
            "preflight": self.preflight.to_dict(),
            "warnings": list(self.warnings),
            "write_status": self.write_status.value,
            "written_path": self.written_path,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "human_gate_level": "L3",
            "requires_human_action": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "auto_intake_allowed": False,
            "writes_database": False,
            "writes_project_state": False,
            "time_semantics_note": MANIFEST_TIME_FREE_NOTE,
            "notes": list(self.notes),
        }


def _package_warnings(
    preflight: RowPreflight, *, historical_oos_applicable: bool
) -> tuple[str, ...]:
    """人读告警（**只描述**预检事实与已知 scanner 口径，不含资格结论）。"""
    warnings: list[str] = []
    if not historical_oos_applicable:
        warnings.append(
            f"{EvidencePackageCode.AVAILABILITY_UNPROVEN.value}：manifest 声明 "
            "historical_oos_applicable=false → GOLD-011 scanner 会整包隔离"
            "（Phase 3.3 资格必须有 OOS 证据）"
        )
    if preflight.synthetic:
        warnings.append(
            f"{EvidencePackageCode.SYNTHETIC_EVIDENCE.value}：预检发现显式示例 / 合成标记的行"
            "（模板与示例永不计入真实资格）"
        )
    if preflight.quarantined:
        warnings.append(
            f"{EvidencePackageCode.CONTAINS_QUARANTINED_ROWS.value}：预检发现 "
            f"{preflight.quarantined} 行被隔离 → GOLD-011 scanner 会整包隔离"
        )
    if preflight.rows and preflight.accepted == 0:
        warnings.append(
            f"{EvidencePackageCode.NO_ACCEPTABLE_ROWS.value}：没有任何可接收的数据行"
            "（不满足进入人工确认队列的最小前提）"
        )
    if preflight.not_oos_eligible:
        warnings.append(
            f"{EvidencePackageCode.AVAILABILITY_UNPROVEN.value}：{preflight.not_oos_eligible} 行"
            "缺少独立 available_at / availability 证据（不满足 OOS 证据要求）"
        )
    return tuple(warnings)


def _require_package_dir(root: Path) -> None:
    """package 目录必须**已存在且为目录**（本工具绝不创建目录、绝不移动原始 evidence）。"""
    if root.is_symlink():
        raise EvidencePackagePathError(
            f"{EvidencePackageCode.PACKAGE_DIR_UNUSABLE.value}：package 目录不得是符号链接："
            f"{_safe_path(root)}"
        )
    if not root.is_dir():
        raise EvidencePackagePathError(
            f"{EvidencePackageCode.PACKAGE_DIR_UNUSABLE.value}：package 目录必须**已存在且为目录**"
            f"（本工具绝不创建目录 / 绝不移动原始 evidence）：{_safe_path(root)}"
        )


def _check_manifest_contract(manifest: Mapping[str, Any]) -> None:
    """内部自检：生成的 manifest 必须**正好**满足 GOLD-011 契约（防御性硬校验）。"""
    problems: list[str] = []
    unknown = sorted(set(manifest) - set(ALLOWED_MANIFEST_KEYS))
    if unknown:
        problems.append(f"出现契约外的键：{unknown}")
    missing = [
        name
        for name in MANIFEST_REQUIRED_FIELDS
        if manifest.get(name) is None or manifest.get(name) == ""
    ]
    if missing:
        problems.append(f"缺少必填声明：{missing}")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        problems.append("files 必须是非空数组")
    else:
        for index, item in enumerate(raw_files, start=1):
            if not isinstance(item, Mapping):
                problems.append(f"files[{index}] 必须是对象")
                continue
            extra = sorted(set(item) - set(ALLOWED_FILE_ENTRY_KEYS))
            if extra:
                problems.append(f"files[{index}] 出现契约外的键：{extra}")
            absent = [name for name in FILE_ENTRY_REQUIRED_FIELDS if not item.get(name)]
            if absent:
                problems.append(f"files[{index}] 缺少 {absent}")
            if item.get("format") not in SUPPORTED_FILE_FORMATS:
                problems.append(
                    f"files[{index}] format 必须是 {list(SUPPORTED_FILE_FORMATS)}"
                )
            if not _is_hex64(item.get("sha256")):
                problems.append(f"files[{index}] sha256 必须是 64 位小写十六进制")
    if problems:
        raise EvidencePackageInternalError(
            "manifest 自检失败（与 GOLD-011 / evidence-intake-v1 契约失配）：" + "；".join(problems)
        )


def _assemble_manifest(
    *,
    evidence_type: str,
    source: str,
    authorization_reference: str,
    time_semantics: str,
    availability_semantics: str,
    historical_oos_applicable: bool,
    entries: Sequence[ManifestFileEntry],
    notes: str | None,
) -> dict[str, Any]:
    """按 GOLD-011 契约组装 manifest（**不含**任何时间字段；同输入 → 同字节）。"""
    manifest: dict[str, Any] = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_type": evidence_type,
        "source": source,
        "authorization_reference": authorization_reference,
        "time_semantics": time_semantics,
        "availability_semantics": availability_semantics,
        "historical_oos_applicable": historical_oos_applicable,
        "files": [entry.to_manifest_entry() for entry in entries],
    }
    if notes is not None:
        manifest["notes"] = notes
    return manifest


def build_manifest_preview(
    package_dir: Path | str,
    *,
    files: Sequence[str],
    evidence_type: object,
    source: object,
    authorization_reference: object,
    time_semantics: object,
    availability_semantics: object,
    historical_oos_applicable: object,
    moment: datetime,
    notes: object = None,
) -> EvidencePackagePreview:
    """构建 manifest **预览**（**零写入**）：显式元数据 + 显式文件清单 → 确定性 manifest。

    只读取 ``files`` 明确指定的、**已存在于** ``package_dir`` 内的单级常规文件；绝不推断授权 /
    发布时间 / 采集时间 / availability / OOS 语义，绝不使用 mtime 或当前时间充当证据时间，
    绝不移动 / 删除 / 改写任何文件，也绝不写数据库或联网。

    Raises:
        EvidencePackageArgumentError: 元数据缺失 / 非法（含 ``evidence_type`` 非 author|news、
            ``historical_oos_applicable`` 非布尔、文件清单为空、时点缺时区）。
        EvidencePackagePathError: ``package_dir`` 不存在 / 不是目录 / 是符号链接。
        EvidencePackageInputError: 文件路径不安全、缺失、目录、符号链接、重复 / 大小写冲突、
            未支持格式、manifest 自引用、包内未声明文件、示例 / 合成名称或敏感值（fail-closed）。
        EvidencePackageInternalError: 生成的 manifest 不满足 GOLD-011 契约（代码与契约失配）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(package_dir)
    _require_package_dir(root)
    cleaned_type, scope = _clean_evidence_type(evidence_type)
    cleaned_source = _clean_source(source)
    cleaned_reference = _clean_reference(authorization_reference)
    cleaned_time = _clean_declared_text(
        time_semantics,
        field_name="time_semantics",
        code=EvidencePackageCode.TIME_SEMANTICS_MISSING,
        max_chars=MAX_SEMANTICS_CHARS,
    )
    cleaned_availability = _clean_declared_text(
        availability_semantics,
        field_name="availability_semantics",
        code=EvidencePackageCode.AVAILABILITY_SEMANTICS_MISSING,
        max_chars=MAX_SEMANTICS_CHARS,
    )
    oos = _require_bool(historical_oos_applicable, field_name="historical_oos_applicable")
    cleaned_notes = _clean_notes(notes)
    declared = _clean_file_list(files)
    entries, reasons = _resolve_files(root, declared)
    if not reasons:
        reasons.extend(_undeclared_reasons(root, [entry.path for entry in entries]))
    if not reasons and _synthetic_by_name(root.name, [entry.path for entry in entries]):
        reasons.append(
            (
                EvidencePackageCode.SYNTHETIC_EVIDENCE.value,
                "package 目录名 / 文件名命中示例 / 合成 / 模板词表：模板与示例永不计入真实资格"
                "（请改用业务包名 / 文件名后重试）",
            )
        )
    if reasons:
        codes, texts = _dedupe_reasons(reasons)
        raise EvidencePackageInputError(codes, texts)
    if not entries:
        raise EvidencePackageInputError(
            [EvidencePackageCode.NO_INPUT_FILES.value],
            ["没有解析出任何可纳入 package 的证据文件"],
        )
    manifest = _assemble_manifest(
        evidence_type=cleaned_type,
        source=cleaned_source,
        authorization_reference=cleaned_reference,
        time_semantics=cleaned_time,
        availability_semantics=cleaned_availability,
        historical_oos_applicable=oos,
        entries=entries,
        notes=cleaned_notes,
    )
    _check_manifest_contract(manifest)
    preflight = preflight_rows(root, entries, scope=scope, moment=moment)
    return EvidencePackagePreview(
        generated_at=moment,
        package_dir=_safe_path(root),
        package_name=_safe(root.name, max_chars=MAX_ENTRY_NAME_CHARS),
        evidence_type=cleaned_type,
        source=cleaned_source,
        authorization_reference=cleaned_reference,
        time_semantics=cleaned_time,
        availability_semantics=cleaned_availability,
        historical_oos_applicable=oos,
        manifest=manifest,
        manifest_sha256=manifest_digest(manifest),
        files=entries,
        preflight=preflight,
        warnings=_package_warnings(preflight, historical_oos_applicable=oos),
    )


def _default_lock_path(root: Path) -> Path:
    """默认锁文件路径：**刻意放在 package 目录之外**（避免锁文件成为包内未声明文件）。"""
    return root.parent / f"{root.name}.manifest{MANIFEST_LOCK_SUFFIX}"


def _write_manifest(out: Path, manifest: Mapping[str, Any]) -> tuple[ManifestWriteStatus, str]:
    """把 manifest 原子写入**固定**目标：已存在且逐字节一致 → 幂等；内容不同 → fail-closed。"""
    payload = manifest_bytes(manifest)
    if out.is_symlink():
        raise EvidencePackageInputError(
            [EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value],
            [f"{MANIFEST_FILE_NAME} 不得是符号链接（拒绝跟随）：{_safe_path(out)}"],
        )
    if out.exists():
        try:
            existing = out.read_bytes()
        except OSError as exc:
            raise EvidencePackageInputError(
                [EvidencePackageCode.MANIFEST_UNREADABLE.value],
                [f"既有 {MANIFEST_FILE_NAME} 不可读（{type(exc).__name__}）：{_safe_path(out)}"],
            ) from exc
        if existing == payload:
            return ManifestWriteStatus.IDEMPOTENT_UNCHANGED, _safe_path(out)
        raise EvidencePackageConflictError(
            f"{EvidencePackageCode.MANIFEST_CONFLICT.value}：既有 {MANIFEST_FILE_NAME}"
            f"（{_safe_path(out)}）与本次规范化内容**不同**，拒绝静默覆盖（fail-closed）；"
            "请新建 package 目录 / 使用新的内容身份（本工具**没有** --force / --overwrite）"
        )
    try:
        atomic_write_text(out, payload.decode("utf-8"))
    except OSError as exc:
        raise EvidencePackageWriteError(
            f"{EvidencePackageCode.MANIFEST_WRITE_FAILED.value}：manifest 原子写失败"
            f"（{type(exc).__name__}）：fail-closed；请检查目录可写性与磁盘空间：{_safe_path(out)}"
        ) from exc
    try:
        written = out.read_bytes()
    except OSError as exc:
        raise EvidencePackageWriteError(
            f"{EvidencePackageCode.MANIFEST_VERIFY_FAILED.value}：manifest 写后不可读"
            f"（{type(exc).__name__}）：{_safe_path(out)}"
        ) from exc
    if written != payload:
        raise EvidencePackageWriteError(
            f"{EvidencePackageCode.MANIFEST_VERIFY_FAILED.value}：manifest 写后自检失败"
            f"（字节不一致）：{_safe_path(out)}"
        )
    return ManifestWriteStatus.WRITTEN, _safe_path(out)


def run_package_builder(
    package_dir: Path | str,
    *,
    files: Sequence[str],
    evidence_type: object,
    source: object,
    authorization_reference: object,
    time_semantics: object,
    availability_semantics: object,
    historical_oos_applicable: object,
    moment: datetime,
    notes: object = None,
    out_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    lock_owner: str | None = None,
) -> EvidencePackagePreview:
    """执行**一次** manifest 构建（默认 dry-run；只有显式 ``out_path`` 才写 manifest）。

    安全语义：

    - 默认**零写入**：``out_path=None`` 时只返回预览（不取锁、不写任何文件）；
    - 只有显式 ``out_path`` 才写 manifest，且**必须正好**是 ``<package-dir>/manifest.json``
      （写到其它任意路径 → :class:`EvidencePackagePathError`，零写入）；
    - 写盘前先取 GOLD-010 单实例锁（默认锁文件在 package 目录**之外**），锁内**重新**读取文件并
      重新计算内容 SHA-256 与预检（防 preflight 与写入之间的 TOCTOU），再做既有 manifest 冲突检查，
      最后**原子**落盘并**写后复读自检**；
    - 既有 manifest 内容不同 → :class:`EvidencePackageConflictError`（**绝不**静默覆盖）；
    - 本函数**没有**任何 intake / commit / 数据库 / 网络调用，**绝不**修改 ``PROJECT_STATE``、
      **绝不**解除 blocker、**绝不**切换 Phase。

    Raises:
        EvidencePackageArgumentError: 参数不合法（含时点缺时区、元数据缺失 / 非法）。
        EvidencePackagePathError: package 目录不可用，或 ``out_path`` 不是固定 ``manifest.json``。
        EvidencePackageInputError: 文件 / 元数据未通过 fail-closed 校验（零写入）。
        EvidencePackageConflictError: 既有 manifest 与本次内容不同（零写入）。
        EvidencePackageWriteError: 原子写失败或写后自检失败。
        LockConflictError: 另一处 manifest 写入正持有活动锁（**零写入**）。
        LockUnavailableError: 锁文件不可创建（**零写入**）。
    """
    moment = _require_aware(moment, field_name="moment")
    root = Path(package_dir)
    _require_package_dir(root)
    target = root / MANIFEST_FILE_NAME
    out: Path | None = None
    if out_path is not None:
        candidate = Path(out_path)
        if candidate.is_dir():
            raise EvidencePackagePathError(
                f"{EvidencePackageCode.MANIFEST_OUT_NOT_TARGET.value}：--out 是目录、不是 manifest "
                f"文件：{_safe_path(candidate)}"
            )
        if not _same_path(candidate, target):
            raise EvidencePackagePathError(
                f"{EvidencePackageCode.MANIFEST_OUT_NOT_TARGET.value}：--out 必须**正好**是"
                f" {_safe_path(target)}（禁止写到其它任意路径）；实际：{_safe_path(candidate)}"
            )
        out = candidate
    if out is None:
        return build_manifest_preview(
            root,
            files=files,
            evidence_type=evidence_type,
            source=source,
            authorization_reference=authorization_reference,
            time_semantics=time_semantics,
            availability_semantics=availability_semantics,
            historical_oos_applicable=historical_oos_applicable,
            moment=moment,
            notes=notes,
        )
    lock_file = Path(lock_path) if lock_path is not None else _default_lock_path(root)
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        preview = build_manifest_preview(
            root,
            files=files,
            evidence_type=evidence_type,
            source=source,
            authorization_reference=authorization_reference,
            time_semantics=time_semantics,
            availability_semantics=availability_semantics,
            historical_oos_applicable=historical_oos_applicable,
            moment=moment,
            notes=notes,
        )
        status, written = _write_manifest(out, preview.manifest)
        return replace(preview, write_status=status, written_path=written)


def render_package_summary(preview: EvidencePackagePreview) -> str:
    """渲染人类可读的 manifest 预览摘要（脱敏；**不解除** blocker、**不代表**资格通过）。"""
    payload = preview.to_dict()
    metadata = payload["metadata"]
    preflight = payload["preflight"]
    written = payload["written_path"]
    lines: list[str] = [
        "# Evidence Package Manifest 预览（**本地机械整理**，不是资格判定器）",
        "",
        f"- 文档：`{PACKAGE_BUILDER_KIND}` / schema `{PACKAGE_BUILDER_SCHEMA_VERSION}` / "
        f"契约 `{EVIDENCE_CONTRACT_VERSION}` / 模式 `{PACKAGE_BUILDER_EXECUTION_MODE}`",
        f"- package：`{payload['package_dir']}`（manifest 固定名 `{MANIFEST_FILE_NAME}`）",
        f"- 证据类别：`{metadata['evidence_type']}`；来源：`{metadata['source']}`",
        "- 授权引用（人工声明；程序**不证明**其法律效力）："
        f"`{metadata['authorization_reference']}`",
        f"- time_semantics：`{metadata['time_semantics']}`",
        f"- availability_semantics：`{metadata['availability_semantics']}`",
        f"- historical_oos_applicable：`{metadata['historical_oos_applicable']}`",
        f"- 文件数量：{len(preview.files)}（内容 SHA-256 来自真实字节，确定性排序）",
        f"- manifest 摘要（落盘字节）：`{payload['manifest_sha256']}`",
        f"- 写入状态：`{payload['write_status']}`"
        + (f" → `{written}`" if written else "（dry-run，零写入）"),
        "",
        "## 文件（相对 package 目录，单级）",
        "",
    ]
    for item in payload["files"]:
        lines.append(
            f"  - `{item['path']}`：format=`{item['format']}` bytes={item['bytes']}"
            f" sha256=`{item['sha256']}`"
        )
    lines += [
        "",
        "## 写入前本地预检（与 `evidence-intake-v1` 同源，**不是**资格结论）",
        "",
        f"- 行数 {preflight['rows']}；accepted {preflight['accepted']}；"
        f"quarantined {preflight['quarantined']}；"
        f"not_oos_eligible {preflight['not_oos_eligible']}；"
        f"结论 `{preflight['outcome']}`",
    ]
    for item in preflight["per_file"]:
        lines.append(
            f"  - `{item['path']}`：rows={item['rows']} accepted={item['accepted']} "
            f"quarantined={item['quarantined']} not_oos_eligible={item['not_oos_eligible']}"
        )
    counts = preflight["reason_code_counts"]
    if counts:
        rendered = "、".join(f"{code}×{count}" for code, count in sorted(counts.items()))
        lines += ["", f"- 稳定原因码计数：{rendered}"]
    if preview.warnings:
        lines += ["", "## 告警（**必须人工确认**）", ""]
        lines += [f"- {item}" for item in preview.warnings]
    lines += [
        "",
        "## 边界（硬编码，不随任何输入变化）",
        "",
        f"- `blocker_code`：`{payload['blocker_code']}`；`blocker_active=true`；"
        "`human_gate_required=true`；`requires_human_action=true`；"
        "`data_qualification_passed=false`；`phase_transition_allowed=false`；"
        "`auto_intake_allowed=false`；`writes_database=false`",
        f"- {PACKAGE_BUILDER_NOTE}",
        f"- {PREFLIGHT_NOTE}",
        f"- {MANIFEST_TIME_FREE_NOTE}",
        "",
    ]
    return "\n".join(lines)


