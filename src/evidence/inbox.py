"""授权 Evidence 本地 Inbox 发现与预检（GOLD-011，**纯本地只读发现** / 零网络 / 零数据库）。

GOLD-005~010 已提供 Evidence Gateway（契约 + 逐行机械校验 + 隔离）、Operator（单入口工作流）、
Readiness（资格观测 / 通知 / 单次 tick runner）。但 operator 仍缺一个**入口**：
"把候选 evidence 放到哪里、系统怎么确定性地发现并预检它"。

本模块补齐该入口，且**只做发现与预检**：

- **显式目录、只读发现**：只扫描 CLI 显式给出的 ``--inbox-dir``；候选包 = inbox 目录的
  每个**直接子目录**（目录内必须有 ``manifest.json``）；**绝不**移动 / 重命名 / 删除 /
  改写 inbox 内的任何原始 evidence 文件，也不联网、不写数据库、不新增 migration/schema；
- **manifest 显式关联**：候选 evidence 必须由 ``manifest.json`` 声明
  （``evidence_type`` / ``source``（或别名 ``provider``）/ ``authorization_reference`` /
  ``time_semantics`` / ``availability_semantics`` / ``historical_oos_applicable`` /
  ``files`` 内每个文件的 ``path`` + ``sha256``）；缺失 / 格式错误一律 **fail-closed**
  并给出稳定 :class:`InboxReasonCode`；
- **复用既有口径，不复制算法**：引用校验复用
  :func:`src.evidence.validation.valid_reference`，逐行机械校验复用
  :func:`src.evidence.validation.assess_row`（同一个 ``evidence-intake-v1`` 契约与
  :class:`~src.evidence.contracts.ReasonCode`），隔离 / 资格 / 授权 / 时间 / OOS 判定
  **全部沿用 Evidence Gateway**，本模块只增加 **manifest / 文件层面**"够不够、对不对"；
- **内容级 SHA-256 与确定性 fingerprint**：每个文件算 SHA-256 并与 manifest 声明的摘要
  核对；候选包 fingerprint 只由**文件内容摘要与结构标记**（排序、确定性）派生，
  **不含** mtime / 扫描时间 / 绝对路径，也**不参与**资格判定（只用于内容身份与幂等去重）；
- **幂等**：pending 清单是**当前 inbox 内容的镜像**（以内容指纹为键）：同一内容重复扫描
  → 既有条目**不重复生成**，仅更新 ``last_seen_at`` / ``scan_count``；内容变化 → 新指纹 →
  新条目；同一指纹但状态变化（例如审计时点推进使"未来时间"变合法）→ 记入
  ``status_changed``（**有意义变化，绝不静默**）；
- **只生成脱敏 pending / preflight artifact**：四类结论显式区分
  （``discovered`` / ``preflight_pass`` / ``quarantined`` / ``requires_human_action``），
  全部经 :func:`src.common.redaction.safe_text` / :func:`~src.common.redaction.safe_url`
  脱敏（凭据擦除、URL 去查询串），**不含** evidence 正文；``--state`` 只读、
  ``--out`` 才**原子**落盘 pending 状态（单实例锁防并发重扫）；
- **绝不自动 intake**：本模块没有 import / commit 路径，也不写任何数据库；
  ``preflight_pass`` 只表示"可以进入**人工确认 / 显式 intake** 队列"，
  真正的落库仍须 operator 显式执行 ``scripts/evidence_operator.py workflow --no-dry-run``；
- **诚实**：所有 artifact 恒为 ``blocker_active=true`` / ``human_gate_required=true`` /
  ``data_qualification_passed=false`` / ``phase_transition_allowed=false`` /
  ``requires_human_action=true``（**硬编码**，不被 manifest 或被篡改的 state 透传影响）；
  模板 / 示例 / Mock（显式标记或文件名命中示例词表）一律判
  ``SYNTHETIC_EVIDENCE`` 并**整包隔离**，永不计入真实资格。

入口：``scripts/evidence_inbox.py``（``--inbox-dir`` 必填；``--state`` 只读、``--out`` 才写）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    ReasonCode,
    synthetic_marker_fields,
)
from src.evidence.intake import read_input_file
from src.evidence.readiness_watch import MAX_CODE_CHARS, atomic_write_text
from src.evidence.validation import assess_row, valid_reference
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_LOCK_CONFLICT",
    "EXIT_NO_CANDIDATES",
    "EXIT_OK",
    "EXIT_STATE_INVALID",
    "EXIT_UNUSABLE",
    "FILE_ENTRY_REQUIRED_FIELDS",
    "INBOX_NOTE",
    "INBOX_REPORT_KIND",
    "INBOX_REPORT_NAME",
    "INBOX_SCHEMA_VERSION",
    "MANIFEST_FILE_NAME",
    "MANIFEST_REQUIRED_FIELDS",
    "MAX_DECLARED_REFERENCE_CHARS",
    "MAX_PATH_CHARS",
    "PENDING_FILE_NAME",
    "PENDING_KIND",
    "PENDING_LOCK_SUFFIX",
    "SUPPORTED_FILE_FORMATS",
    "CandidatePackage",
    "InboxArtifactWriteError",
    "InboxDirError",
    "InboxError",
    "InboxPreflightReport",
    "InboxReasonCode",
    "InboxStatus",
    "ManifestFileRef",
    "PendingEntry",
    "PendingRegister",
    "PendingStateError",
    "ensure_outside_inbox",
    "exit_code_for",
    "load_pending_register",
    "render_inbox_summary",
    "run_inbox_scan",
    "scan_inbox",
    "write_pending_register",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
INBOX_SCHEMA_VERSION: Final[int] = 1
#: 预检报告标识 / 文档标识（稳定，供上游与定时器日志解析）
INBOX_REPORT_NAME: Final[str] = "evidence_inbox_preflight"
INBOX_REPORT_KIND: Final[str] = "evidence_inbox_preflight"
#: 待处理清单（pending state）文档标识
PENDING_KIND: Final[str] = "evidence_inbox_pending"
#: 候选包内 manifest 的**固定**文件名（不猜、不做多命名约定）
MANIFEST_FILE_NAME: Final[str] = "manifest.json"
#: pending state 的默认文件名（operator 放在显式工作目录里）
PENDING_FILE_NAME: Final[str] = "inbox_pending.json"
#: pending state 的单实例锁后缀（与 ``--out`` 同级）
PENDING_LOCK_SUFFIX: Final[str] = ".lock"

#: manifest 必填声明（缺任一项 → ``MANIFEST_FIELD_MISSING``，fail-closed）
MANIFEST_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "evidence_type",
    "source",
    "authorization_reference",
    "time_semantics",
    "availability_semantics",
    "historical_oos_applicable",
    "files",
)
#: ``source`` 的等价别名（"source / provider"：两者其一非空即可）
SOURCE_ALIAS_FIELD: Final[str] = "provider"
#: ``files`` 内每个文件的必填项
FILE_ENTRY_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("path", "sha256")
#: manifest 顶层**允许**的键（契约字段 + 明确元数据）。
#: 其余"凭据类"键名（``api_key`` / ``token`` …）一律命中凭据防护；
#: 注意 ``authorization_reference`` 是**契约字段本身**（与 intake 的列白名单同口径），
#: 不能因为名字里含 authorization 就被误判为凭据列。
ALLOWED_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        *MANIFEST_REQUIRED_FIELDS,
        SOURCE_ALIAS_FIELD,
        "schema_version",
        "contract_version",
        "notes",
    }
)
#: ``files`` 条目**允许**的键
ALLOWED_FILE_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {*FILE_ENTRY_REQUIRED_FIELDS, "format"}
)
#: 支持的逐行输入格式（与 Evidence Gateway 的 ``SUPPORTED_FORMATS`` 同口径 + ``auto``）
SUPPORTED_FILE_FORMATS: Final[tuple[str, ...]] = ("auto", "csv", "jsonl")
#: 引用 / 路径类字段的展示上限
MAX_PATH_CHARS: Final[int] = 200
#: 授权引用入参的内存上限（防止异常长串）
MAX_DECLARED_REFERENCE_CHARS: Final[int] = 1_000

# ---- 退出码（稳定；复用 GOLD-010 runner 的既有取值，不另造一套语义）--------
#: 扫描完成（**不代表**资格通过；是否有人工待确认项见 ``counts.preflight_pass``）
EXIT_OK: Final[int] = runner.EXIT_OK
#: 参数 / 配置错误（例如未显式给出 ``--inbox-dir``）
EXIT_CONFIG_ERROR: Final[int] = runner.EXIT_CONFIG_ERROR
#: inbox 目录或输出 artifact 不可用（fail-closed，零写入）
EXIT_UNUSABLE: Final[int] = runner.EXIT_WORKDIR_UNUSABLE
#: 既有 pending state 损坏 / 被篡改（fail-closed，保留旧文件，零写入）
EXIT_STATE_INVALID: Final[int] = runner.EXIT_STATE_INVALID
#: 扫描完成但**没有任何**可进入人工确认队列的候选（预期 BLOCKED，不是故障；
#: 与 runner 的 ``EXIT_BLOCKED`` 同值同义）
EXIT_NO_CANDIDATES: Final[int] = runner.EXIT_BLOCKED
#: 锁冲突：另一个重扫正持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = runner.EXIT_LOCK_CONFLICT

#: 固定说明：inbox 只做发现与预检，不解除 blocker、不切 Phase、不自动 intake
INBOX_NOTE: Final[str] = (
    "本工具只对显式 `--inbox-dir` 做**纯本地只读发现与预检**：不联网、不写数据库、"
    "不移动 / 不删除 / 不改写任何原始 evidence 文件，也不调用任何 intake / commit 路径；"
    "`preflight_pass` 只表示“可以进入**人工确认 / 显式 intake** 队列”，**不表示**授权已通过"
    "人工核验、也不表示数据资格通过；`blocker_active` / `human_gate_required` 恒为 true，"
    "`data_qualification_passed` / `phase_transition_allowed` 恒为 false，"
    "`PHASE3_3_DATA` **保持 BLOCKED**。"
)

#: 目录快照中的结构标记（非内容摘要）
_LAYOUT_SYMLINK: Final[str] = "symlink"
_LAYOUT_DIR: Final[str] = "dir"
_LAYOUT_OTHER: Final[str] = "other"
_LAYOUT_UNREADABLE: Final[str] = "unreadable"

_HEX64: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
_NAME_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
#: 逐行隔离类原因码（值形态；与 Evidence Gateway 的 ``QUARANTINE_REASON_CODES`` 同源）
_QUARANTINE_VALUES: Final[frozenset[str]] = frozenset(
    code.value for code in QUARANTINE_REASON_CODES
)


class InboxStatus(StrEnum):
    """候选包预检结论（``PREFLIGHT_PASS`` 也只进入**人工**队列）。"""

    PREFLIGHT_PASS = "PREFLIGHT_PASS"
    QUARANTINED = "QUARANTINED"


class InboxReasonCode(StrEnum):
    """inbox 预检的稳定原因码（文本永远脱敏）。

    与既有 :class:`~src.evidence.contracts.ReasonCode` 重合的部分**直接复用其字符串值**
    （下游解析口径一致，不另造词汇表）；其余只描述 **manifest / 目录 / 文件层面**的
    缺失、不可核验或安全拒绝，**不替代**逐行业务判定（逐行判定仍由
    :func:`src.evidence.validation.assess_row` 给出）。
    """

    # ---- 复用既有 ReasonCode 口径（值完全一致）---------------------------
    AUTHORIZATION_MISSING = ReasonCode.AUTHORIZATION_MISSING.value
    AUTHORIZATION_REFERENCE_INVALID = ReasonCode.AUTHORIZATION_REFERENCE_INVALID.value
    SCOPE_MISMATCH = ReasonCode.SCOPE_MISMATCH.value
    SENSITIVE_VALUE_DETECTED = ReasonCode.SENSITIVE_VALUE_DETECTED.value
    SYNTHETIC_EVIDENCE = ReasonCode.SYNTHETIC_EVIDENCE.value
    ROW_UNREADABLE = ReasonCode.ROW_UNREADABLE.value
    AVAILABILITY_UNPROVEN = ReasonCode.AVAILABILITY_UNPROVEN.value
    # ---- inbox 专有：manifest / 目录 / 文件层面 ---------------------------
    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_UNREADABLE = "MANIFEST_UNREADABLE"
    MANIFEST_SCHEMA_UNSUPPORTED = "MANIFEST_SCHEMA_UNSUPPORTED"
    MANIFEST_FIELD_MISSING = "MANIFEST_FIELD_MISSING"
    EVIDENCE_TYPE_UNSUPPORTED = "EVIDENCE_TYPE_UNSUPPORTED"
    PACKAGE_SYMLINK_REJECTED = "PACKAGE_SYMLINK_REJECTED"
    PACKAGE_LAYOUT_INVALID = "PACKAGE_LAYOUT_INVALID"
    EVIDENCE_FILE_ENTRY_INVALID = "EVIDENCE_FILE_ENTRY_INVALID"
    EVIDENCE_FILE_PATH_UNSAFE = "EVIDENCE_FILE_PATH_UNSAFE"
    EVIDENCE_FILE_MISSING = "EVIDENCE_FILE_MISSING"
    EVIDENCE_FILE_UNREADABLE = "EVIDENCE_FILE_UNREADABLE"
    EVIDENCE_FILE_UNDECLARED = "EVIDENCE_FILE_UNDECLARED"
    EVIDENCE_DIGEST_INVALID = "EVIDENCE_DIGEST_INVALID"
    EVIDENCE_DIGEST_MISMATCH = "EVIDENCE_DIGEST_MISMATCH"
    TIME_SEMANTICS_MISSING = "TIME_SEMANTICS_MISSING"
    AVAILABILITY_SEMANTICS_MISSING = "AVAILABILITY_SEMANTICS_MISSING"
    NO_ACCEPTABLE_ROWS = "NO_ACCEPTABLE_ROWS"
    CONTAINS_QUARANTINED_ROWS = "CONTAINS_QUARANTINED_ROWS"


class InboxError(RuntimeError):
    """inbox 预检失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class InboxDirError(InboxError):
    """inbox 目录不可用，或输出 artifact 与 inbox 目录冲突。"""


class PendingStateError(InboxError):
    """既有 pending state 损坏 / 被篡改（**fail-closed**，保留旧文件，零写入）。"""


class InboxArtifactWriteError(InboxError):
    """pending state 原子写失败（**fail-closed**）。"""


#: 退出码映射表（顺序即优先级）→ 稳定退出码（未知类型按 fail-closed 处理）
_INBOX_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (PendingStateError, EXIT_STATE_INVALID),
    (runner.LockConflictError, EXIT_LOCK_CONFLICT),
    (InboxDirError, EXIT_UNUSABLE),
    (InboxArtifactWriteError, EXIT_UNUSABLE),
    (runner.LockUnavailableError, EXIT_UNUSABLE),
)


def exit_code_for(error: BaseException) -> int:
    """把失败映射为**稳定**退出码（未知类型按 fail-closed 处理为“state 非法”）。"""
    for kind, code in _INBOX_EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


def _safe(value: object, *, max_chars: int = MAX_CODE_CHARS) -> str:
    """统一脱敏 + 截断（原因文本 / 来源名 / 文件名共用）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = MAX_PATH_CHARS) -> str:
    """路径类展示：脱敏 + 截断（绝不回显凭据）。"""
    return _safe(value, max_chars=max_chars)


def _safe_reference(value: str, *, max_chars: int = MAX_PATH_CHARS) -> str:
    """引用类展示：URL 去掉 userinfo / query / fragment，再脱敏 + 截断。"""
    return safe_text(safe_url(value, max_chars=max_chars), max_chars=max_chars)


def _dedupe_reasons(
    reasons: Sequence[tuple[str, str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """原因（码, 文本）去重：同一原因码只保留**排序后第一条**文本，顺序完全确定。"""
    codes: list[str] = []
    texts: list[str] = []
    for code, text in sorted(reasons):
        if code in codes:
            continue
        codes.append(code)
        texts.append(_safe(text, max_chars=300))
    return tuple(codes), tuple(texts)


# ---------------------------------------------------------------------------
# 目录快照 / 内容指纹 / 静态安全检测
# ---------------------------------------------------------------------------
def _layout(root: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """对候选包做**内容级**目录快照：相对名 → ``sha256:<hex>`` 或结构标记。

    安全与确定性：

    - 只读文件内容（不读 mtime，也不把绝对路径写进快照）；
    - **不跟随符号链接**（标记为 ``symlink`` 并给出稳定原因码）；
    - 子目录 / 其它非常规条目一律标记并给出原因码（fail-closed）。
    """
    layout: dict[str, str] = {}
    reasons: list[tuple[str, str]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        name = entry.name
        if entry.is_symlink():
            layout[name] = _LAYOUT_SYMLINK
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"符号链接不被跟随（fail-closed）：{_safe(name)}",
                )
            )
            continue
        if entry.is_dir():
            layout[name] = _LAYOUT_DIR
            reasons.append(
                (
                    InboxReasonCode.PACKAGE_LAYOUT_INVALID.value,
                    f"候选包内不允许子目录：{_safe(name)}",
                )
            )
            continue
        if not entry.is_file():
            layout[name] = _LAYOUT_OTHER
            reasons.append(
                (
                    InboxReasonCode.PACKAGE_LAYOUT_INVALID.value,
                    f"候选包内含非常规条目（既非文件也非目录）：{_safe(name)}",
                )
            )
            continue
        try:
            digest = hashing.sha256_bytes(entry.read_bytes())
        except OSError as exc:
            layout[name] = _LAYOUT_UNREADABLE
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_UNREADABLE.value,
                    f"文件不可读（{type(exc).__name__}）：{_safe(name)}",
                )
            )
            continue
        layout[name] = f"sha256:{digest}"
    return layout, reasons


def _package_fingerprint(layout: Mapping[str, str]) -> str:
    """候选包**内容级**指纹：只由文件内容摘要与结构标记派生（排序、确定性）。

    ⚠️ 刻意**不含**文件名、mtime、绝对路径与扫描时间：这些都不得成为"资格证据"，
    也不应因为"改个名字 / 拷到别的机器 / 换个时刻扫描"就变成新的待处理项。
    指纹**只用于内容身份与幂等去重**，不参与任何资格判定。
    """
    payload = json.dumps(sorted(layout.values()), ensure_ascii=False, separators=(",", ":"))
    return hashing.sha256_text(payload)


def _synthetic_by_name(package_dir_name: str, file_names: Sequence[str]) -> bool:
    """候选包名 / 文件名是否命中示例 / 合成词表（**仅用于排除**，绝不用于放行）。

    文件名只在这里出现，且只可能让候选**更不可能**通过（模板 / 示例 / Mock 永不计入资格）。
    """
    for name in (package_dir_name, *file_names):
        tokens = {token for token in _NAME_TOKEN_SPLIT.split(name.casefold()) if token}
        if tokens & EXAMPLE_MARKER_VALUES:
            return True
    return False


def _sensitive_findings(
    mapping: Mapping[str, Any], *, allowed: frozenset[str]
) -> tuple[list[str], list[str]]:
    """返回 (凭据类键名, 疑似含凭据的字段名)；**只返回键名，绝不返回值**。

    ``allowed`` 为契约允许的键（manifest / files 条目），它们**不是**"凭据类键"，
    否则 ``authorization_reference`` 这类必填契约字段会被自己的名字误伤。
    """
    sensitive_keys: list[str] = []
    credential_keys: list[str] = []
    for raw_key, value in mapping.items():
        key = str(raw_key)
        if key.lower() not in allowed and is_sensitive_key(key):
            sensitive_keys.append(key)
        if isinstance(value, str) and value.strip() and redact_secrets(value) != value:
            credential_keys.append(key)
    return sorted(set(sensitive_keys)), sorted(set(credential_keys))


def _sensitive_reasons(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    """manifest（含 ``files`` 条目）的凭据防护：命中即隔离，**只记录键名**。"""
    reasons: list[tuple[str, str]] = []
    findings: list[tuple[str, tuple[list[str], list[str]]]] = [
        ("manifest", _sensitive_findings(manifest, allowed=ALLOWED_MANIFEST_KEYS))
    ]
    raw_files = manifest.get("files")
    if isinstance(raw_files, list):
        findings.extend(
            (f"files[{index}]", _sensitive_findings(item, allowed=ALLOWED_FILE_ENTRY_KEYS))
            for index, item in enumerate(raw_files, start=1)
            if isinstance(item, Mapping)
        )
    for where, (sensitive_keys, credential_keys) in findings:
        if sensitive_keys:
            reasons.append(
                (
                    InboxReasonCode.SENSITIVE_VALUE_DETECTED.value,
                    f"{where} 含凭据类键 {sensitive_keys}（只记录键名，值丢弃）",
                )
            )
        if credential_keys:
            reasons.append(
                (
                    InboxReasonCode.SENSITIVE_VALUE_DETECTED.value,
                    f"{where} 字段疑似包含凭据 {credential_keys}（值不记录、不入库）",
                )
            )
    return reasons


def _read_manifest(package_dir: Path) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
    """读取候选包内固定名 manifest（只读；缺失 / 损坏 / 版本不符一律 fail-closed）。"""
    path = package_dir / MANIFEST_FILE_NAME
    if path.is_symlink():
        return None, [
            (
                InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                f"{MANIFEST_FILE_NAME} 不得是符号链接（拒绝跟随）",
            )
        ]
    if not path.is_file():
        return None, [
            (
                InboxReasonCode.MANIFEST_MISSING.value,
                f"候选包缺少 {MANIFEST_FILE_NAME}（候选 evidence 必须由 manifest 显式关联）",
            )
        ]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [
            (
                InboxReasonCode.MANIFEST_UNREADABLE.value,
                f"{MANIFEST_FILE_NAME} 不可读（{type(exc).__name__}）",
            )
        ]
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [
            (
                InboxReasonCode.MANIFEST_UNREADABLE.value,
                f"{MANIFEST_FILE_NAME} 不是合法 UTF-8 JSON（{type(exc).__name__}）",
            )
        ]
    if not isinstance(payload, dict):
        return None, [
            (
                InboxReasonCode.MANIFEST_UNREADABLE.value,
                f"{MANIFEST_FILE_NAME} 顶层必须是 JSON 对象",
            )
        ]
    reasons: list[tuple[str, str]] = []
    schema = payload.get("schema_version")
    if schema is not None and schema != INBOX_SCHEMA_VERSION:
        reasons.append(
            (
                InboxReasonCode.MANIFEST_SCHEMA_UNSUPPORTED.value,
                f"manifest schema_version={_safe(schema, max_chars=20)} 不受支持"
                f"（当前 {INBOX_SCHEMA_VERSION}）",
            )
        )
    contract = payload.get("contract_version")
    if contract is not None and str(contract).strip() != EVIDENCE_CONTRACT_VERSION:
        reasons.append(
            (
                InboxReasonCode.MANIFEST_SCHEMA_UNSUPPORTED.value,
                f"manifest contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致",
            )
        )
    return payload, reasons


@dataclass(frozen=True, slots=True)
class _Declaration:
    """manifest 解析出的声明事实（**只是声明**：程序不据此判定资格，只校验齐全性）。"""

    evidence_type: str
    scope: EvidenceScope | None
    source: str
    authorization_reference: str
    time_semantics: str
    availability_semantics: str
    historical_oos_applicable: bool | None
    file_entries: tuple[tuple[str, str, str], ...] = ()  # (path, 声明 sha256, format)

    def file_paths(self) -> tuple[str, ...]:
        """声明的文件路径（仅用于示例 / 合成词表排除判定）。"""
        return tuple(path for path, _sha, _fmt in self.file_entries)


_EMPTY_DECLARATION: Final[_Declaration] = _Declaration(
    evidence_type="",
    scope=None,
    source="",
    authorization_reference="",
    time_semantics="",
    availability_semantics="",
    historical_oos_applicable=None,
)


def _declared_text(
    manifest: Mapping[str, Any], field: str, *, alias: str | None = None
) -> tuple[str, bool]:
    """读取字符串声明（字段缺失时尝试 ``alias``）；返回 (值, 是否出现过)。"""
    value = manifest.get(field)
    if (value is None or not str(value).strip()) and alias is not None:
        value = manifest.get(alias)
    if value is None:
        return "", False
    text = str(value).strip()
    return text[:MAX_DECLARED_REFERENCE_CHARS], bool(text)


def _parse_file_entries(
    manifest: Mapping[str, Any],
) -> tuple[tuple[tuple[str, str, str], ...], list[tuple[str, str]]]:
    """解析 ``files`` 声明（每条 = path + sha256 + 可选 format）；只做格式检查。"""
    reasons: list[tuple[str, str]] = []
    raw = manifest.get("files")
    if not isinstance(raw, list) or not raw:
        return (), [
            (
                InboxReasonCode.MANIFEST_FIELD_MISSING.value,
                "manifest 的 files 必须是非空数组（每个文件声明 path + sha256）",
            )
        ]
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            reasons.append(
                (InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value, f"files[{index}] 必须是对象")
            )
            continue
        path = item.get("path")
        digest = item.get("sha256")
        fmt = item.get("format", "auto")
        if not isinstance(path, str) or not path.strip():
            reasons.append(
                (InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value, f"files[{index}] 缺少 path")
            )
            continue
        name = path.strip()
        if name in seen:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value,
                    f"files 中重复声明同一路径：{_safe(name)}",
                )
            )
            continue
        seen.add(name)
        if not isinstance(digest, str) or not digest.strip():
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_DIGEST_INVALID.value,
                    f"files[{index}]（{_safe(name)}）缺少 sha256 文件摘要",
                )
            )
            continue
        fmt_text = fmt.strip().lower() if isinstance(fmt, str) else ""
        if fmt_text not in SUPPORTED_FILE_FORMATS:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value,
                    f"files[{index}]（{_safe(name)}）format 必须是 {SUPPORTED_FILE_FORMATS}",
                )
            )
            fmt_text = "auto"
        entries.append((name, digest.strip().lower(), fmt_text))
    return tuple(entries), reasons


def _parse_declarations(
    manifest: Mapping[str, Any],
) -> tuple[_Declaration, list[tuple[str, str]]]:
    """校验 manifest 的必填声明（**只校验齐全性与格式，不据声明放行任何资格**）。"""
    reasons: list[tuple[str, str]] = []
    raw_type, _present = _declared_text(manifest, "evidence_type")
    evidence_type = raw_type.lower()
    scope: EvidenceScope | None = None
    if not evidence_type:
        reasons.append(
            (
                InboxReasonCode.MANIFEST_FIELD_MISSING.value,
                "manifest 缺少必填声明 evidence_type（author|news）",
            )
        )
    else:
        try:
            scope = EvidenceScope(evidence_type)
        except ValueError:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_TYPE_UNSUPPORTED.value,
                    f"evidence_type={_safe(raw_type, max_chars=40)} 不是 author|news",
                )
            )

    source, _present = _declared_text(manifest, "source", alias=SOURCE_ALIAS_FIELD)
    if not source:
        reasons.append(
            (
                InboxReasonCode.MANIFEST_FIELD_MISSING.value,
                f"manifest 缺少必填声明 source（或别名 {SOURCE_ALIAS_FIELD}）",
            )
        )

    reference, _present = _declared_text(manifest, "authorization_reference")
    if not reference:
        reasons.append(
            (
                InboxReasonCode.AUTHORIZATION_MISSING.value,
                "manifest 缺少必填声明 authorization_reference（授权引用）",
            )
        )
    elif not valid_reference(reference):
        reasons.append(
            (
                InboxReasonCode.AUTHORIZATION_REFERENCE_INVALID.value,
                "authorization_reference 必须是 https URL 或 docs/legal/ 内相对路径",
            )
        )

    time_semantics, _present = _declared_text(manifest, "time_semantics")
    if not time_semantics:
        reasons.append(
            (
                InboxReasonCode.TIME_SEMANTICS_MISSING.value,
                "manifest 缺少必填声明 time_semantics（published_at / collected_at 的时区口径）",
            )
        )

    availability_semantics, _present = _declared_text(manifest, "availability_semantics")
    if not availability_semantics:
        reasons.append(
            (
                InboxReasonCode.AVAILABILITY_SEMANTICS_MISSING.value,
                "manifest 缺少必填声明 availability_semantics（独立可用时间的证据形式）",
            )
        )

    oos_raw = manifest.get("historical_oos_applicable")
    oos: bool | None = None
    if "historical_oos_applicable" not in manifest or oos_raw is None:
        reasons.append(
            (
                InboxReasonCode.MANIFEST_FIELD_MISSING.value,
                "manifest 缺少必填声明 historical_oos_applicable（布尔值）",
            )
        )
    elif not isinstance(oos_raw, bool):
        reasons.append(
            (
                InboxReasonCode.MANIFEST_FIELD_MISSING.value,
                "historical_oos_applicable 必须是布尔值（true / false）",
            )
        )
    else:
        oos = oos_raw
        if not oos:
            reasons.append(
                (
                    InboxReasonCode.AVAILABILITY_UNPROVEN.value,
                    "manifest 声明该候选不适用于历史 OOS：缺少独立可用性证据 → "
                    "只能隔离（Phase 3.3 资格必须有 OOS 证据）",
                )
            )

    entries, entry_reasons = _parse_file_entries(manifest)
    reasons.extend(entry_reasons)
    return (
        _Declaration(
            evidence_type=evidence_type,
            scope=scope,
            source=source,
            authorization_reference=reference,
            time_semantics=time_semantics,
            availability_semantics=availability_semantics,
            historical_oos_applicable=oos,
            file_entries=entries,
        ),
        reasons,
    )


@dataclass(frozen=True, slots=True)
class ManifestFileRef:
    """manifest 声明的证据文件（含**实测**内容 SHA-256 与摘要核对结论）。"""

    path: str
    sha256: str
    declared_sha256: str
    digest_verified: bool
    format: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": _safe_path(self.path),
            "format": self.format,
            "sha256": self.sha256,
            "declared_sha256": self.declared_sha256,
            "digest_verified": self.digest_verified,
        }


def _safe_relative_name(raw: str) -> str | None:
    """把 manifest 声明的 ``path`` 规范化为**候选包内的单级文件名**；不安全返回 ``None``。

    拒绝：绝对路径（POSIX / Windows / UNC）、盘符、``..``、多级路径、含 NUL 的名字。
    只允许候选包目录下的**直接子文件**（不猜、不放宽，故不存在路径穿越与越界读取）。
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


def _check_files(
    layout: Mapping[str, str], declaration: _Declaration
) -> tuple[tuple[ManifestFileRef, ...], list[tuple[str, str]]]:
    """逐文件核对：路径安全性 → 存在性 → 常规文件 → 64 位摘要 → 内容 SHA-256 一致。

    ⚠️ 只依据 :func:`_layout`（已在候选包目录内读取内容摘要）判定，
    **绝不**按 manifest 声明的路径去目录外读文件（从根上排除路径穿越）。
    """
    reasons: list[tuple[str, str]] = []
    refs: list[ManifestFileRef] = []
    resolved_names: set[str] = set()
    for raw_path, declared_sha, fmt in declaration.file_entries:
        name = _safe_relative_name(raw_path)
        if name is None:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"files 中的 path 不得是绝对路径 / 上级目录 / 多级路径：{_safe(raw_path)}",
                )
            )
            continue
        resolved_names.add(name)
        kind = layout.get(name)
        if kind is None:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_MISSING.value,
                    f"声明的证据文件不存在于候选包内：{_safe(name)}",
                )
            )
            continue
        if not kind.startswith("sha256:"):
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value,
                    f"声明的证据文件不是常规文件（符号链接 / 子目录一律拒绝）：{_safe(name)}",
                )
            )
            continue
        actual = kind.split(":", 1)[1]
        if not _HEX64.match(declared_sha):
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_DIGEST_INVALID.value,
                    f"声明的 sha256 不是 64 位小写十六进制：{_safe(name)}",
                )
            )
            continue
        verified = actual == declared_sha
        if not verified:
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_DIGEST_MISMATCH.value,
                    f"内容 SHA-256 与 manifest 声明不一致：{_safe(name)}",
                )
            )
        refs.append(
            ManifestFileRef(
                path=name,
                sha256=actual,
                declared_sha256=declared_sha,
                digest_verified=verified,
                format=fmt,
            )
        )
    for name in sorted(layout):
        if name == MANIFEST_FILE_NAME or name in resolved_names:
            continue
        reasons.append(
            (
                InboxReasonCode.EVIDENCE_FILE_UNDECLARED.value,
                f"候选包内存在未声明的文件（fail-closed）：{_safe(name)}",
            )
        )
    return tuple(refs), reasons


@dataclass(frozen=True, slots=True)
class _RowPreflight:
    """逐行预检汇总（**只计数与稳定原因码**；不含正文与原始值）。"""

    rows: int = 0
    acceptable: int = 0
    quarantined: int = 0
    not_oos_eligible: int = 0
    synthetic: bool = False
    code_counts: tuple[tuple[str, int], ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()


def _preflight_rows(
    package_dir: Path,
    file_refs: tuple[ManifestFileRef, ...],
    *,
    scope: EvidenceScope,
    moment: datetime,
) -> _RowPreflight:
    """用 **Evidence Gateway 的逐行机械校验**做只读预检（不落库、不联网、不写文件）。

    复用 :func:`src.evidence.validation.assess_row`（``evidence-intake-v1`` 契约），
    因此隔离 / 授权 / 时间 / availability 口径与 ``intake`` **完全同源**；
    ``DUPLICATE`` / ``IDENTITY_CONFLICT``（需要读库）留给**显式 intake** 处理。
    """
    rows = acceptable = quarantined = not_oos = 0
    synthetic = False
    counts: Counter[str] = Counter()
    reasons: list[tuple[str, str]] = []
    for ref in file_refs:
        try:
            _input_file, input_rows = read_input_file(
                package_dir / ref.path, requested_format=ref.format
            )
            for index, row in enumerate(input_rows):
                rows += 1
                if row.data is None:
                    quarantined += 1
                    counts[InboxReasonCode.ROW_UNREADABLE.value] += 1
                    continue
                assessment = assess_row(
                    row.data, scope=scope, index=index, row_number=row.row_number, moment=moment
                )
                codes = [code.value for code in assessment.reason_codes]
                for code in codes:
                    counts[code] += 1
                if InboxReasonCode.SYNTHETIC_EVIDENCE.value in codes:
                    synthetic = True
                if any(code in _QUARANTINE_VALUES for code in codes):
                    quarantined += 1
                    continue
                acceptable += 1
                record = assessment.record
                if record is None or not record.oos_eligible:
                    not_oos += 1
        except Exception as exc:  # 任何单文件异常都必须 fail-closed，且异常正文不入 artifact
            reasons.append(
                (
                    InboxReasonCode.EVIDENCE_FILE_UNREADABLE.value,
                    f"证据文件预检失败（{type(exc).__name__}）：{_safe(ref.path)}",
                )
            )
    return _RowPreflight(
        rows=rows,
        acceptable=acceptable,
        quarantined=quarantined,
        not_oos_eligible=not_oos,
        synthetic=synthetic,
        code_counts=tuple(sorted(counts.items())),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class CandidatePackage:
    """一个候选包的预检结论（脱敏；``reason_codes`` 为空才算 ``PREFLIGHT_PASS``）。"""

    package_dir: str
    fingerprint: str
    evidence_type: str
    source: str
    authorization_reference: str
    time_semantics: str
    availability_semantics: str
    historical_oos_applicable: bool | None
    files: tuple[ManifestFileRef, ...]
    rows: int
    acceptable_rows: int
    quarantined_rows: int
    not_oos_eligible_rows: int
    synthetic: bool
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    code_counts: tuple[tuple[str, int], ...] = ()

    @property
    def status(self) -> InboxStatus:
        """``PREFLIGHT_PASS`` 仅当**没有任何** fail-closed 原因码（仍只进人工队列）。"""
        return InboxStatus.QUARANTINED if self.reason_codes else InboxStatus.PREFLIGHT_PASS

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**：不含正文、不含原始行值）。"""
        return {
            "package_dir": _safe_path(self.package_dir),
            "fingerprint": self.fingerprint,
            "status": self.status.value,
            "evidence_type": _safe(self.evidence_type, max_chars=40),
            "source": _safe(self.source, max_chars=100),
            "authorization_reference": _safe_reference(self.authorization_reference),
            "time_semantics": _safe(self.time_semantics, max_chars=200),
            "availability_semantics": _safe(self.availability_semantics, max_chars=200),
            "historical_oos_applicable": self.historical_oos_applicable,
            "files": [item.to_dict() for item in self.files],
            "rows": self.rows,
            "acceptable_rows": self.acceptable_rows,
            "quarantined_rows": self.quarantined_rows,
            "not_oos_eligible_rows": self.not_oos_eligible_rows,
            "synthetic": self.synthetic,
            "reason_codes": list(self.reason_codes),
            "reasons": [_safe(text, max_chars=300) for text in self.reasons],
            "reason_code_counts": {code: count for code, count in self.code_counts},
            "requires_human_action": self.status is InboxStatus.PREFLIGHT_PASS,
        }


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """待处理清单中的**一条**候选包（以 fingerprint 为键：同内容不重复生成）。"""

    fingerprint: str
    status: str
    evidence_type: str
    source: str
    reason_codes: tuple[str, ...]
    file_count: int
    first_seen_at: str
    last_seen_at: str
    scan_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "status": self.status,
            "evidence_type": _safe(self.evidence_type, max_chars=40),
            "source": _safe(self.source, max_chars=100),
            "reason_codes": list(self.reason_codes),
            "file_count": self.file_count,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "scan_count": self.scan_count,
        }


@dataclass(frozen=True, slots=True)
class PendingRegister:
    """脱敏的**待处理清单**（pending state；``--out`` 才原子落盘，``--state`` 只读）。"""

    generated_at: datetime
    entries: tuple[PendingEntry, ...]

    @property
    def kind(self) -> str:
        """文档标识（稳定）。"""
        return PENDING_KIND

    def entry_for(self, fingerprint: str) -> PendingEntry | None:
        """按指纹取条目（不存在返回 ``None``）。"""
        for entry in self.entries:
            if entry.fingerprint == fingerprint:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；含审计时间但审计时间不参与任何判定）。"""
        return {
            "kind": PENDING_KIND,
            "schema_version": INBOX_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
            "notes": [INBOX_NOTE],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PendingRegister:
        """从落盘字典恢复（**严格校验**；任何异常都 fail-closed）。

        Raises:
            PendingStateError: 文档标识 / schema / 契约版本 / 结构 / 条目不合法。
        """
        if payload.get("kind") != PENDING_KIND:
            raise PendingStateError("state 文件不是 evidence inbox pending 清单")
        if payload.get("schema_version") != INBOX_SCHEMA_VERSION:
            raise PendingStateError(
                f"state schema_version 不受支持：{payload.get('schema_version')!r}"
            )
        contract = payload.get("contract_version")
        if contract is not None and str(contract) != EVIDENCE_CONTRACT_VERSION:
            raise PendingStateError(
                f"state contract_version 与 {EVIDENCE_CONTRACT_VERSION} 不一致"
            )
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise PendingStateError("state entries 必须是数组")
        entries: list[PendingEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_entries, start=1):
            if not isinstance(item, Mapping):
                raise PendingStateError(f"state entries[{index}] 必须是对象")
            entry = _pending_entry_from_state(item, index)
            if entry.fingerprint in seen:
                raise PendingStateError(f"state entries[{index}] 指纹重复：拒绝加载")
            seen.add(entry.fingerprint)
            entries.append(entry)
        try:
            generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        except (KeyError, ValueError) as exc:
            raise PendingStateError("state generated_at 无法解析") from exc
        return cls(generated_at=generated_at, entries=tuple(entries))


def _pending_entry_from_state(item: Mapping[str, Any], index: int) -> PendingEntry:
    """从 state 条目恢复单条记录（逐字段严格校验；绝不猜测缺失字段）。"""
    fingerprint = str(item.get("fingerprint") or "")
    if not _HEX64.match(fingerprint):
        raise PendingStateError(f"state entries[{index}] fingerprint 非法")
    status = str(item.get("status") or "")
    if status not in {member.value for member in InboxStatus}:
        raise PendingStateError(f"state entries[{index}] status 非法：{status!r}")
    raw_codes = item.get("reason_codes") or []
    if not isinstance(raw_codes, list):
        raise PendingStateError(f"state entries[{index}] reason_codes 必须是数组")
    try:
        file_count = int(item.get("file_count") or 0)
        scan_count = int(item.get("scan_count") or 0)
    except (TypeError, ValueError) as exc:
        raise PendingStateError(f"state entries[{index}] 计数非法") from exc
    return PendingEntry(
        fingerprint=fingerprint,
        status=status,
        evidence_type=_safe(item.get("evidence_type") or "", max_chars=40),
        source=_safe(item.get("source") or "", max_chars=100),
        reason_codes=tuple(sorted({_safe(code, max_chars=80) for code in raw_codes})),
        file_count=file_count,
        first_seen_at=_safe(item.get("first_seen_at") or "", max_chars=40),
        last_seen_at=_safe(item.get("last_seen_at") or "", max_chars=40),
        scan_count=scan_count,
    )


@dataclass(frozen=True, slots=True)
class InboxPreflightReport:
    """一次 inbox 扫描的**稳定**结果（预检 artifact 与人类可读摘要的唯一事实来源）。"""

    generated_at: datetime
    inbox_dir: Path
    packages: tuple[CandidatePackage, ...]
    skipped: tuple[tuple[str, str], ...]
    discovered: tuple[str, ...]
    already_pending: tuple[str, ...]
    register: PendingRegister
    status_changed: tuple[str, ...] = ()
    notes: tuple[str, ...] = (INBOX_NOTE,)

    @property
    def preflight_pass(self) -> tuple[CandidatePackage, ...]:
        """通过预检的候选（**仍需人工确认 + 显式 intake**，不等于资格通过）。"""
        return tuple(item for item in self.packages if item.status is InboxStatus.PREFLIGHT_PASS)

    @property
    def quarantined(self) -> tuple[CandidatePackage, ...]:
        """被隔离 / 拒绝的候选（含缺失与不一致；每条都有稳定原因码）。"""
        return tuple(item for item in self.packages if item.status is InboxStatus.QUARANTINED)

    @property
    def requires_human_action(self) -> bool:
        """恒为 ``True``：inbox 只把候选交给**人工**，绝不自动 intake / 解除 blocker。"""
        return True

    @property
    def has_candidates(self) -> bool:
        """是否存在**可以进入人工确认 / 显式 intake 队列**的候选。"""
        return bool(self.preflight_pass)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；四个安全字段**硬编码**）。"""
        return {
            "kind": INBOX_REPORT_KIND,
            "report": INBOX_REPORT_NAME,
            "schema_version": INBOX_SCHEMA_VERSION,
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "inbox_dir": _safe_path(self.inbox_dir),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "requires_human_action": True,
            "counts": {
                "packages": len(self.packages),
                "discovered": len(self.discovered),
                "already_pending": len(self.already_pending),
                "status_changed": len(self.status_changed),
                "preflight_pass": len(self.preflight_pass),
                "quarantined": len(self.quarantined),
                "skipped": len(self.skipped),
                "pending_entries": len(self.register.entries),
            },
            "discovered": list(self.discovered),
            "already_pending": list(self.already_pending),
            "status_changed": list(self.status_changed),
            "preflight_pass": [item.to_dict() for item in self.preflight_pass],
            "quarantined": [item.to_dict() for item in self.quarantined],
            "skipped": [
                {"path": _safe_path(path), "reason_code": code} for path, code in self.skipped
            ],
            "pending": self.register.to_dict(),
            "notes": list(self.notes),
        }


def _scan_package(package_dir: Path, *, moment: datetime) -> CandidatePackage:
    """扫描一个候选包目录（只读；任何缺失 / 不一致都落为**稳定**隔离原因码）。"""
    layout, layout_reasons = _layout(package_dir)
    fingerprint = _package_fingerprint(layout)
    reasons: list[tuple[str, str]] = list(layout_reasons)

    manifest, manifest_reasons = _read_manifest(package_dir)
    reasons.extend(manifest_reasons)

    declaration = _EMPTY_DECLARATION
    synthetic = False
    if manifest is not None:
        declaration, declared_reasons = _parse_declarations(manifest)
        reasons.extend(declared_reasons)
        reasons.extend(_sensitive_reasons(manifest))
        synthetic = bool(synthetic_marker_fields(manifest)) or _synthetic_by_name(
            package_dir.name, declaration.file_paths()
        )
        if synthetic:
            reasons.append(
                (
                    InboxReasonCode.SYNTHETIC_EVIDENCE.value,
                    "候选包带显式示例 / 合成 / Mock 标记（或名称命中示例词表）："
                    "模板与示例永不计入真实资格",
                )
            )

    file_refs: tuple[ManifestFileRef, ...] = ()
    row_result = _RowPreflight()
    if declaration.scope is not None and declaration.file_entries:
        file_refs, file_reasons = _check_files(layout, declaration)
        reasons.extend(file_reasons)
        if not file_reasons:
            # 摘要未核实前**不解析**文件内容（fail-closed 短路：不把未声明 / 已变更的内容带进判定）
            row_result = _preflight_rows(
                package_dir, file_refs, scope=declaration.scope, moment=moment
            )
            reasons.extend(row_result.reasons)
            synthetic = synthetic or row_result.synthetic
            if row_result.quarantined:
                reasons.append(
                    (
                        InboxReasonCode.CONTAINS_QUARANTINED_ROWS.value,
                        f"候选包内 {row_result.quarantined} 行被隔离（整包 fail-closed）："
                        "请修正后重新放入",
                    )
                )
            if row_result.acceptable == 0:
                reasons.append(
                    (
                        InboxReasonCode.NO_ACCEPTABLE_ROWS.value,
                        "候选包没有可接收的数据行（空文件 / 全部被隔离）："
                        "不满足进入人工确认队列的最小前提",
                    )
                )
            if row_result.not_oos_eligible:
                reasons.append(
                    (
                        InboxReasonCode.AVAILABILITY_UNPROVEN.value,
                        f"候选包内 {row_result.not_oos_eligible} 行缺少独立 available_at / "
                        "availability 证据（不满足 OOS 证据要求）",
                    )
                )

    codes, texts = _dedupe_reasons(reasons)
    return CandidatePackage(
        package_dir=package_dir.name,
        fingerprint=fingerprint,
        evidence_type=declaration.evidence_type,
        source=declaration.source,
        authorization_reference=declaration.authorization_reference,
        time_semantics=declaration.time_semantics,
        availability_semantics=declaration.availability_semantics,
        historical_oos_applicable=declaration.historical_oos_applicable,
        files=file_refs,
        rows=row_result.rows,
        acceptable_rows=row_result.acceptable,
        quarantined_rows=row_result.quarantined,
        not_oos_eligible_rows=row_result.not_oos_eligible,
        synthetic=synthetic,
        reason_codes=codes,
        reasons=texts,
        code_counts=row_result.code_counts,
    )


def _unscannable_package(name: str, error: OSError) -> CandidatePackage:
    """候选包目录本身不可读：fail-closed 隔离（内容指纹只有目录名，绝不猜测内容）。"""
    codes, texts = _dedupe_reasons(
        [
            (
                InboxReasonCode.PACKAGE_LAYOUT_INVALID.value,
                f"候选包目录不可读（{type(error).__name__}）：{_safe(name)}",
            )
        ]
    )
    return CandidatePackage(
        package_dir=name,
        fingerprint=hashing.sha256_text(f"unreadable:{name}"),
        evidence_type="",
        source="",
        authorization_reference="",
        time_semantics="",
        availability_semantics="",
        historical_oos_applicable=None,
        files=(),
        rows=0,
        acceptable_rows=0,
        quarantined_rows=0,
        not_oos_eligible_rows=0,
        synthetic=False,
        reason_codes=codes,
        reasons=texts,
    )


def _scan_root_file(path: Path) -> CandidatePackage:
    """inbox 根目录下的散落文件：**不是**候选包（缺 manifest），fail-closed 隔离并说明。"""
    layout: dict[str, str] = {}
    reasons: list[tuple[str, str]] = [
        (
            InboxReasonCode.MANIFEST_MISSING.value,
            "inbox 根目录下的散落文件不是候选包：请放入子目录并提供 "
            f"{MANIFEST_FILE_NAME}（本文件未被移动 / 删除）",
        )
    ]
    try:
        layout[path.name] = f"sha256:{hashing.sha256_bytes(path.read_bytes())}"
    except OSError as exc:
        layout[path.name] = _LAYOUT_UNREADABLE
        reasons.append(
            (
                InboxReasonCode.EVIDENCE_FILE_UNREADABLE.value,
                f"根目录条目不可读（{type(exc).__name__}）：{_safe(path.name)}",
            )
        )
    codes, texts = _dedupe_reasons(reasons)
    return CandidatePackage(
        package_dir=path.name,
        fingerprint=_package_fingerprint(layout),
        evidence_type="",
        source="",
        authorization_reference="",
        time_semantics="",
        availability_semantics="",
        historical_oos_applicable=None,
        files=(),
        rows=0,
        acceptable_rows=0,
        quarantined_rows=0,
        not_oos_eligible_rows=0,
        synthetic=False,
        reason_codes=codes,
        reasons=texts,
    )






# ---------------------------------------------------------------------------
# 扫描 / 落盘 / 渲染
# ---------------------------------------------------------------------------
def scan_inbox(
    inbox_dir: Path, *, moment: datetime, previous: PendingRegister | None = None
) -> InboxPreflightReport:
    """**只读**扫描显式 inbox 目录，返回脱敏预检报告（不写 artifact、不碰原始文件）。

    Args:
        inbox_dir: 显式给出的 inbox 目录（候选包 = 其**直接子目录**）。
        moment: 审计时点（tz-aware；用于行级"未来时间"判定与审计时间戳）。
        previous: 既有 pending 清单（``--state``），用于**幂等去重**（同内容不重复生成）。

    Raises:
        ValueError: ``moment`` 未带时区（禁止隐式时区）。
        InboxDirError: inbox 目录不存在 / 不是目录 / 不可读。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment 必须包含时区")
    moment = moment.astimezone(UTC)
    root = Path(inbox_dir)
    if not root.is_dir():
        raise InboxDirError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise InboxDirError(
            f"inbox 目录不可读（{type(exc).__name__}）：{_safe_path(root)}"
        ) from exc

    packages: list[CandidatePackage] = []
    skipped: list[tuple[str, str]] = []
    for entry in children:
        if entry.is_symlink():
            # 符号链接一律不跟随（既可能是路径穿越，也可能是"双份数据"的假象）
            skipped.append((entry.name, InboxReasonCode.PACKAGE_SYMLINK_REJECTED.value))
            continue
        if entry.is_dir():
            try:
                packages.append(_scan_package(entry, moment=moment))
            except OSError as exc:
                packages.append(_unscannable_package(entry.name, exc))
            continue
        if entry.is_file():
            packages.append(_scan_root_file(entry))
            continue
        skipped.append((entry.name, InboxReasonCode.PACKAGE_LAYOUT_INVALID.value))

    ordered = tuple(sorted(packages, key=lambda item: (item.fingerprint, item.package_dir)))
    entries: list[PendingEntry] = []
    discovered: list[str] = []
    already_pending: list[str] = []
    status_changed: list[str] = []
    seen: set[str] = set()
    for package in ordered:
        if package.fingerprint in seen:
            continue  # 同一内容只保留**一条**待处理项（幂等）
        seen.add(package.fingerprint)
        old = previous.entry_for(package.fingerprint) if previous is not None else None
        if old is None:
            discovered.append(package.fingerprint)
        else:
            already_pending.append(package.fingerprint)
            if old.status != package.status.value:
                status_changed.append(package.fingerprint)
        entries.append(
            PendingEntry(
                fingerprint=package.fingerprint,
                status=package.status.value,
                evidence_type=package.evidence_type,
                source=package.source,
                reason_codes=package.reason_codes,
                file_count=len(package.files),
                first_seen_at=old.first_seen_at if old is not None else moment.isoformat(),
                last_seen_at=moment.isoformat(),
                scan_count=(old.scan_count + 1) if old is not None else 1,
            )
        )
    return InboxPreflightReport(
        generated_at=moment,
        inbox_dir=root,
        packages=ordered,
        skipped=tuple(sorted(skipped)),
        discovered=tuple(discovered),
        already_pending=tuple(already_pending),
        register=PendingRegister(generated_at=moment, entries=tuple(entries)),
        status_changed=tuple(status_changed),
    )




def load_pending_register(path: Path) -> PendingRegister | None:
    """读取既有 pending 清单（文件不存在 = 首次扫描；损坏 / 被篡改 → fail-closed）。

    Raises:
        PendingStateError: 文件不可读 / 非 JSON 对象 / 严格校验失败
            （**绝不**当作首次扫描，也绝不覆盖既有文件）。
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PendingStateError(f"pending state 不可读：{_safe_path(target)}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PendingStateError("pending state 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise PendingStateError("pending state 顶层必须是 JSON 对象")
    return PendingRegister.from_dict(payload)


def write_pending_register(path: Path, register: PendingRegister) -> None:
    """**原子**落盘 pending 清单（同目录临时文件 + ``fsync`` + ``os.replace``）。

    Raises:
        InboxArtifactWriteError: 写入失败（fail-closed；调用方不得假设已落盘）。
    """
    text = json.dumps(register.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(Path(path), text)
    except OSError as exc:
        raise InboxArtifactWriteError(
            f"pending state 写入失败（{type(exc).__name__}）：fail-closed；"
            f"请检查输出目录可写性与磁盘空间：{_safe_path(path)}"
        ) from exc


def ensure_outside_inbox(root: Path, target: Path) -> None:
    """输出 / 状态文件必须位于 inbox 目录**之外**（否则下一次扫描会判为未声明文件）。

    公开导出：GOLD-012 的人工复核决策层（``src.evidence.review``）复用**同一**安全口径，
    避免各处各写一套（与 ``valid_reference`` / ``atomic_write_text`` 的公开方式一致）。
    """
    try:
        resolved_root = root.resolve()
        resolved_target = target.resolve()
    except OSError as exc:
        raise InboxDirError(f"路径无法解析（{type(exc).__name__}）") from exc
    if resolved_target == resolved_root or resolved_root in resolved_target.parents:
        raise InboxDirError(
            "pending state / 输出必须放在 inbox 目录**之外**"
            f"（否则会被下一次扫描判为未声明文件）：{_safe_path(target)}"
        )



def run_inbox_scan(
    inbox_dir: Path,
    *,
    moment: datetime,
    pending_path: Path | None = None,
    out_path: Path | None = None,
    lock_path: Path | None = None,
    lock_owner: str | None = None,
) -> InboxPreflightReport:
    """扫描 inbox，并在**显式给出** ``out_path`` 时原子落盘 pending 清单。

    安全语义：

    - ``pending_path``（``--state``）**只读**；损坏 → :class:`PendingStateError`（零写入）；
    - 只有 ``out_path``（``--out``）才写文件，且先取**单实例锁**再读 state（避免并发重扫
      带来的读改写竞态）；锁冲突 →
      :class:`~src.evidence.readiness_runner.LockConflictError` 且**零写入**；
    - inbox 内的原始 evidence 文件**永不**被移动 / 重命名 / 删除 / 改写；
    - 零网络、零数据库写入、不调用任何 intake / commit 路径。

    Raises:
        ValueError: ``moment`` 未带时区。
        InboxDirError: inbox 目录不可用，或输出与 inbox 目录冲突。
        PendingStateError: 既有 pending state 损坏 / 被篡改（fail-closed）。
        LockConflictError: 另一个重扫正持有活动锁（fail-closed，零写入）。
        InboxArtifactWriteError: pending 清单写入失败（fail-closed）。
    """
    root = Path(inbox_dir)
    if not root.is_dir():
        raise InboxDirError(f"inbox 目录不存在或不是目录：{_safe_path(root)}")
    pending = Path(pending_path) if pending_path is not None else None
    out = Path(out_path) if out_path is not None else None
    for target in (pending, out):
        if target is not None:
            ensure_outside_inbox(root, target)
    if out is None:
        previous = load_pending_register(pending) if pending is not None else None
        return scan_inbox(root, moment=moment, previous=previous)
    lock_file = (
        Path(lock_path)
        if lock_path is not None
        else out.with_name(out.name + PENDING_LOCK_SUFFIX)
    )
    with runner.SingleInstanceLock(lock_file, owner=lock_owner):
        previous = load_pending_register(pending) if pending is not None else None
        report = scan_inbox(root, moment=moment, previous=previous)
        write_pending_register(out, report.register)
    return report


def render_inbox_summary(report: InboxPreflightReport) -> str:
    """渲染人类可读的 inbox 预检摘要（脱敏；**不解除** blocker、不切换 Phase）。"""
    counts = report.to_dict()["counts"]
    lines: list[str] = [
        "# 授权 Evidence 本地 Inbox 预检（Evidence Inbox Preflight）",
        "",
        f"> blocker `{PHASE3_3_BLOCKER_CODE}`：active=true；human_gate_required=true；"
        "data_qualification_passed=false；phase_transition_allowed=false；"
        "本工具只做**发现与预检**，不自动 intake、不解除 blocker。",
        "",
        "## 1. 概览",
        "",
        f"- inbox 目录：`{_safe_path(report.inbox_dir)}`（只读发现；原始文件未被移动 / 删除）",
        f"- 候选包：{counts['packages']}；本轮新发现：{counts['discovered']}；"
        f"既有 pending（幂等跳过重复生成）：{counts['already_pending']}；"
        f"状态变化：{counts['status_changed']}",
        f"- preflight_pass（**仍需人工确认 + 显式 intake**）：{counts['preflight_pass']}；"
        f"quarantined / rejected：{counts['quarantined']}；skipped（符号链接等）："
        f"{counts['skipped']}；pending 条目：{counts['pending_entries']}",
        f"- 审计时点（UTC）：{report.generated_at.isoformat()}",
        "",
        "## 2. preflight_pass（进入人工确认 / 显式 intake 队列）",
        "",
        "| 候选包 | evidence_type | source | 文件数 | 行数 / 可接收 | 指纹（前 16 位） |",
        "|---|---|---|---:|---:|---|",
    ]
    if not report.preflight_pass:
        lines.append("| — | — | — | 0 | — | — |")
    for item in report.preflight_pass:
        lines.append(
            f"| {_safe_path(item.package_dir)} | {_safe(item.evidence_type, max_chars=40)} | "
            f"{_safe(item.source, max_chars=100)} | {len(item.files)} | "
            f"{item.rows} / {item.acceptable_rows} | {item.fingerprint[:16]} |"
        )
    lines += ["", "## 3. quarantined / rejected（fail-closed）", ""]
    if not report.quarantined:
        lines.append("- —（本轮没有被隔离的候选包）")
    for item in report.quarantined:
        lines.append(
            f"- `{_safe_path(item.package_dir)}`（指纹前 16 位 `{item.fingerprint[:16]}`）"
            f"：{len(item.files)} 个声明文件；行数 {item.rows}（可接收 {item.acceptable_rows}、"
            f"隔离 {item.quarantined_rows}、缺 OOS 证据 {item.not_oos_eligible_rows}）"
        )
        for code, text in zip(item.reason_codes, item.reasons, strict=False):
            lines.append(f"  - `{code}`：{text}")
    if report.skipped:
        lines += ["", "## 4. skipped（不跟随 / 不识别）", ""]
        for path, code in report.skipped:
            lines.append(f"- `{_safe_path(path)}`：`{code}`")
    lines += ["", "## 5. 口径与边界", "", f"- {INBOX_NOTE}", ""]
    return "\n".join(lines)
