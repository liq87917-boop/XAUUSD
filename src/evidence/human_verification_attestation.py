"""Phase 3.3 **材料级人工核验凭证**（Human Verification Attestation，GOLD-028）。

本模块把 GOLD-027 的 intake handoff **结构完整预检**（``preflight_pass``）进一步转换为一份
**可审计的材料级人工核验凭证**：对每个必核验材料，明确记录

- **决策**（受控词表 ``VERIFIED`` / ``REJECTED`` / ``NEEDS_CHANGES``）、
- **稳定 reason code**、
- **显式 reviewer label**（非敏感 operator 标签，**不采集任何凭据**）、
- **带时区的 ``reviewed_at``**（人工核验动作时间，**不是**证据时间，**不**由文件 mtime /
  当前时间推导）、
- **独立 evidence reference**（``https://`` 出处或 ``docs/legal/`` 路径）。

职责边界（**重要**，与 ``.clinerules`` 一致）：

- **只读 / 零网络 / 零数据库 / 零写入（默认）**：不抓取站点、不绕过 robots / 条款 / 证书、
  不写数据库、不训练模型、不触碰交易；只消费**显式给出的本地候选目录**（``inbox_dir``）与
  人工显式给出的本地核验输入文件（``verification_path``）；
- **绑定而不可漂移**：凭证硬绑定**当前**候选 package fingerprint、GOLD-027 handoff / manifest
  的**内容身份**（``handoff_content_sha256``）与 scope；任一 package / manifest / content
  fingerprint 漂移都必须使旧凭证**失效**（verify 模式给出稳定原因码），**绝不静默继承**；
- **``all_required_verified`` 只代表材料级人工核验完成**：它**绝不**等于
  ``evidence_qualified``、**不解除** ``PHASE3_3_DATA``、也**不**代表 L3 / L4 通过；
  产物里 ``data_qualification_passed`` / ``phase_transition_allowed`` /
  ``l3_l4_auto_advance_allowed`` / ``evidence_qualified`` / ``advance_allowed`` **恒为 false**、
  ``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` **恒为 true**（**硬编码**，
  不被输入或上游报告透传）；
- **fail-closed**：Mock / 模板 / 示例 / 合成包、``preflight_pass=false`` 的包、以及**结构上
  不属于 ``HUMAN_VERIFICATION_REQUIRED``** 的材料，一律**不允许**被声明为 ``VERIFIED``
  （``MATERIAL_NOT_VERIFIABLE``）；缺失材料决策 → ``MATERIAL_DECISION_MISSING``；
  任一必然导致 ``all_required_verified=false``；
- **不伪造时间事实**：``published_at`` / ``collected_at`` / ``effective_at`` / ``available_at``
  / OOS 证据绝不出现在凭证里（只作为字段名出现在要求文本中），也绝不用当前时间 / 文件 mtime /
  抓取时间或任何推断值填补；
- **脱敏**：只输出白名单标量（字段名 / 计数 / 稳定原因码 / 已脱敏的引用与标签 / 时间），
  **绝不**输出候选文件正文、token / API key / Authorization 或完整 source config。

入口：``scripts/evidence_human_verification_attestation.py``（``--schema`` 打印版本化凭证契约；
默认只打印 stdout、零写入；唯一写开关是显式 ``--out``；``--verify-attestation`` 为纯只读
重新绑定核验模式）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common.hashing import sha256_text
from src.common.redaction import safe_text
from src.evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceScope
from src.evidence.inbox import InboxError, ensure_outside_inbox
from src.evidence.intake_handoff import (
    INTAKE_HANDOFF_SCHEMA_VERSION,
    MATERIAL_SPECS,
    PREFLIGHT_PASS_SEMANTICS,
    HandoffMaterial,
    IntakeHandoffDocument,
    IntakeStatus,
    MaterialSpec,
    PackageHandoff,
    load_intake_handoff,
)
from src.evidence.readiness_runner import (
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_WORKDIR_UNUSABLE,
)
from src.evidence.readiness_watch import atomic_write_text
from src.monitoring.phase33_qualification import PHASE3_3_BLOCKER_CODE

__all__ = [
    "ALL_REQUIRED_VERIFIED_SEMANTICS",
    "ATTESTATION_CONTRACT_VERSION",
    "ATTESTATION_EXECUTION_MODE",
    "ATTESTATION_FILE_NAME",
    "ATTESTATION_KIND",
    "ATTESTATION_NOTE",
    "ATTESTATION_REPORT_NAME",
    "ATTESTATION_SCHEMA_VERSION",
    "ATTESTATION_SCOPE",
    "ATTESTATION_TIME_SEMANTICS_NOTE",
    "ATTESTATION_VERIFICATION_KIND",
    "EXIT_BLOCKED",
    "EXIT_CONFIG_ERROR",
    "EXIT_OK",
    "EXIT_STATE_INVALID",
    "EXIT_WORKDIR_UNUSABLE",
    "FORBIDDEN_EVIDENCE_KEYS",
    "MAX_EVIDENCE_REFERENCE_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_NOTE_INPUT_CHARS",
    "MAX_REASON_CODE_CHARS",
    "MAX_REVIEWER_CHARS",
    "MAX_REVISION",
    "REQUIRED_VERIFICATION_CATEGORIES",
    "REVIEWER_KIND",
    "VERIFICATION_DECISIONS",
    "VERIFICATION_INPUT_SCHEMA_VERSION",
    "AttestationArgumentError",
    "AttestationCode",
    "AttestationError",
    "AttestationNotAttestableError",
    "AttestationPathError",
    "AttestationStateError",
    "AttestationVerification",
    "AttestationWriteError",
    "HumanVerificationAttestation",
    "MaterialAttestation",
    "MaterialDecision",
    "VerificationDecision",
    "attestable_material_keys",
    "attestation_schema",
    "build_attestation",
    "compute_attestation_id",
    "exit_code_for",
    "handoff_content_sha256",
    "load_attestation_document",
    "load_verification_input",
    "main_verification_exit_code",
    "package_content_sha256",
    "render_attestation_summary",
    "run_attestation",
    "verify_attestation",
]

# ---------------------------------------------------------------------------
# 版本与固定说明（字段增删必须同步升版本 + 更新 README / 测试）
# ---------------------------------------------------------------------------
#: 机器可读 schema 版本
ATTESTATION_SCHEMA_VERSION: Final[int] = 1
#: 凭证文档标识 / 报告标识（稳定，供人工与上游日志解析）
ATTESTATION_KIND: Final[str] = "phase33_human_verification_attestation"
ATTESTATION_REPORT_NAME: Final[str] = ATTESTATION_KIND
#: verify 模式的产物标识
ATTESTATION_VERIFICATION_KIND: Final[str] = f"{ATTESTATION_KIND}_verification"
#: 凭证文件名建议（只有显式 ``--out`` 才写盘）
ATTESTATION_FILE_NAME: Final[str] = "phase33_human_verification_attestation.json"
#: 人工核验契约版本（输入字段名仍只来自既有 ``evidence-intake-v1`` 契约）
ATTESTATION_CONTRACT_VERSION: Final[str] = "human-verification-attestation-v1"
#: 核验输入文件 schema 版本
VERIFICATION_INPUT_SCHEMA_VERSION: Final[int] = 1
#: 执行模式（**只读 + 显式人工输入**；硬编码）
ATTESTATION_EXECUTION_MODE: Final[str] = "LOCAL_HUMAN_VERIFICATION_ATTESTATION"
#: 凭证作用域（防止把"材料级人工核验"读成"数据资格通过"）
ATTESTATION_SCOPE: Final[str] = "material_level_human_verification_only_not_data_qualification"
#: reviewer 口径（**只**接受显式 label；不采集凭据）
REVIEWER_KIND: Final[str] = "explicit_operator_label_no_credentials"
#: 受控人工决策词表（**必须**由人工显式给出）
VERIFICATION_DECISIONS: Final[tuple[str, ...]] = ("VERIFIED", "REJECTED", "NEEDS_CHANGES")
#: reviewer label 长度上限
MAX_REVIEWER_CHARS: Final[int] = 64
#: reason code 长度上限
MAX_REASON_CODE_CHARS: Final[int] = 64
#: 独立 evidence reference 长度上限
MAX_EVIDENCE_REFERENCE_CHARS: Final[int] = 300
#: note 落盘长度上限（脱敏后）
MAX_NOTE_CHARS: Final[int] = 400
#: note 输入长度上限（超出 → fail-closed，绝不静默截断成假事实）
MAX_NOTE_INPUT_CHARS: Final[int] = 4000
#: revision 上限（防止异常输入撑爆凭证）
MAX_REVISION: Final[int] = 1_000_000

#: 固定说明：凭证**不是**资格判定，也不推进任何 Gate
ATTESTATION_NOTE: Final[str] = (
    "本凭证只记录**人工材料级核验**的结果（谁、何时、依据哪条独立引用核验了哪个材料），"
    "它**不是** `evidence_qualified`，**不解除** "
    f"`{PHASE3_3_BLOCKER_CODE}`，也**不**代表 L3 / L4 通过："
    "`data_qualification_passed` / `phase_transition_allowed` / `l3_l4_auto_advance_allowed` / "
    "`evidence_qualified` / `advance_allowed` **恒为 false**、`blocker_active` / "
    "`human_gate_required` / `gate_blocked` **恒为 true**（**硬编码**）。"
)
#: ``all_required_verified`` 的诚实语义（文档与机器字段都必须明确这一点）
ALL_REQUIRED_VERIFIED_SEMANTICS: Final[str] = (
    "`all_required_verified=true` **只**表示「当前候选包非 Mock / 模板 / 示例 / 合成、"
    "GOLD-027 `preflight_pass=true`、且每个必核验材料都已有带独立引用的受控人工决策 "
    "`VERIFIED`」；它**绝不**等于 `evidence_qualified=true`、**不解除** "
    f"`{PHASE3_3_BLOCKER_CODE}`、也**不**代表 L3 / L4 通过。"
)
#: 时间语义说明（人工核验动作时间 vs 证据时间）
ATTESTATION_TIME_SEMANTICS_NOTE: Final[str] = (
    "`reviewed_at` / `generated_at` / `attested_at` **都是审计操作时间**，**不是**证据时间；"
    "它们绝不用于冒充 `published_at` / `collected_at` / `effective_at` / `available_at`，"
    "文件 mtime 与当前时间同样**不被采信**、**不参与**任何判定。"
)

#: 必核验材料**至少**要覆盖的类别（缺一即契约漂移，fail-closed）
REQUIRED_VERIFICATION_CATEGORIES: Final[tuple[str, ...]] = (
    "authorization",
    "source",
    "time_semantics",
    "published_at",
    "collected_at",
    "effective_at",
    "availability_oos",
)
#: Gate 材料类别（gate 材料**不**可被人工核验为 VERIFIED）
_GATE_CATEGORY: Final[str] = "gate"

#: reviewer label：只允许可审计、非敏感的显式标签（不含空格 / 引号 / 路径分隔符）
_REVIEWER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")
#: reason code：稳定大写原因码形态（与 GOLD-012/013/014/016 同一口径）
_REASON_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: 独立 evidence reference：``https://`` 出处或 ``docs/legal/`` 路径（与 GOLD-027 同口径）
_HTTPS_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^https://[^\s/]+/\S*$")
_LEGAL_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^docs/legal/[A-Za-z0-9][A-Za-z0-9._/-]*$"
)
#: "看起来像凭据"的取值（reviewer / reference / reason 一律拒绝）
_SECRET_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(passwo?r?d|pwd|secret|token|api[_-]?key|apikey|bearer|credential|"
    r"authorization|private[_-]?key|session[_-]?id)"
)
#: 长十六进制 / base64 形态的疑似密钥串（reviewer / reference 一律拒绝）
_BLOB_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")
#: 凭证中**禁止**出现的证据时间键（递归检查键名；凭证只记录审计操作时间）
FORBIDDEN_EVIDENCE_KEYS: Final[tuple[str, ...]] = (
    "published_at",
    "collected_at",
    "effective_at",
    "available_at",
)

#: 凭证 id 绑定用的 package 字段（**不含**任何审计时间）
_PACKAGE_BINDING_KEYS: Final[tuple[str, ...]] = (
    "scope",
    "package_dir",
    "fingerprint",
    "package_content_sha256",
    "handoff_content_sha256",
    "handoff_contract_version",
    "handoff_schema_version",
    "inbox_status",
    "synthetic",
    "preflight_pass",
)
#: 凭证 id 绑定用的材料字段（**不含**派生文本与 requirement）
_MATERIAL_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "material",
    "intake_status",
    "decision",
    "reason_code",
    "reviewer",
    "reviewed_at",
    "evidence_reference",
    "note",
    "counts_as_verified",
)
#: 核验输入文件允许的顶层键（未知键一律拒绝：防止夹带凭据 / 正文）
_VERIFICATION_INPUT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "package_fingerprint",
        "handoff_content_sha256",
        "scope",
        "reviewer",
        "materials",
    }
)
#: 核验输入文件允许的材料项键
_MATERIAL_DECISION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "material",
        "decision",
        "reason_code",
        "reviewer",
        "reviewed_at",
        "evidence_reference",
        "note",
    }
)
_HEX_DIGITS: Final[str] = "0123456789abcdef"


class VerificationDecision(StrEnum):
    """受控人工核验决策（**必须**由人工显式给出；工具绝不自行生成）。"""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    NEEDS_CHANGES = "NEEDS_CHANGES"


class AttestationCode(StrEnum):
    """材料级人工核验的稳定原因码（任何一条都意味着 fail-closed：不得视为资格通过）。"""

    # ---- 材料 / 包级结论 ------------------------------------------------
    MATERIAL_DECISION_MISSING = "MATERIAL_DECISION_MISSING"
    MATERIAL_NOT_VERIFIABLE = "MATERIAL_NOT_VERIFIABLE"
    MATERIAL_REJECTED = "MATERIAL_REJECTED"
    MATERIAL_NEEDS_CHANGES = "MATERIAL_NEEDS_CHANGES"
    MATERIAL_REFERENCE_MISSING = "MATERIAL_REFERENCE_MISSING"
    NON_QUALIFYING_PACKAGE = "NON_QUALIFYING_PACKAGE"
    PREFLIGHT_NOT_PASSED = "PREFLIGHT_NOT_PASSED"
    PACKAGE_SELECTION_REQUIRED = "PACKAGE_SELECTION_REQUIRED"
    PACKAGE_SELECTION_AMBIGUOUS = "PACKAGE_SELECTION_AMBIGUOUS"
    PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
    # ---- 绑定 / 漂移 ----------------------------------------------------
    PACKAGE_FINGERPRINT_MISMATCH = "PACKAGE_FINGERPRINT_MISMATCH"
    PACKAGE_FINGERPRINT_DRIFT = "PACKAGE_FINGERPRINT_DRIFT"
    PACKAGE_CONTENT_DRIFT = "PACKAGE_CONTENT_DRIFT"
    HANDOFF_CONTENT_MISMATCH = "HANDOFF_CONTENT_MISMATCH"
    HANDOFF_CONTENT_DRIFT = "HANDOFF_CONTENT_DRIFT"
    PREFLIGHT_DRIFT = "PREFLIGHT_DRIFT"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    # ---- 人工输入 ------------------------------------------------------
    VERIFICATION_INVALID = "VERIFICATION_INVALID"
    VERIFICATION_NOT_FOUND = "VERIFICATION_NOT_FOUND"
    VERIFICATION_UNREADABLE = "VERIFICATION_UNREADABLE"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"
    DUPLICATE_MATERIAL = "DUPLICATE_MATERIAL"
    DECISION_INVALID = "DECISION_INVALID"
    REVIEWER_INVALID = "REVIEWER_INVALID"
    REASON_CODE_INVALID = "REASON_CODE_INVALID"
    EVIDENCE_REFERENCE_INVALID = "EVIDENCE_REFERENCE_INVALID"
    EVIDENCE_REFERENCE_UNTRUSTED = "EVIDENCE_REFERENCE_UNTRUSTED"
    SENSITIVE_VALUE_REJECTED = "SENSITIVE_VALUE_REJECTED"
    REVIEWED_AT_INVALID = "REVIEWED_AT_INVALID"
    REVIEWED_AT_FUTURE = "REVIEWED_AT_FUTURE"
    NOTE_TOO_LONG = "NOTE_TOO_LONG"
    # ---- 修订 / 冲突 ----------------------------------------------------
    REVISION_INVALID = "REVISION_INVALID"
    SUPERSEDES_MISMATCH = "SUPERSEDES_MISMATCH"
    ATTESTATION_CONFLICT = "ATTESTATION_CONFLICT"
    ATTESTATION_OVERWRITE_REFUSED = "ATTESTATION_OVERWRITE_REFUSED"
    # ---- 凭证文档 -------------------------------------------------------
    ATTESTATION_NOT_FOUND = "ATTESTATION_NOT_FOUND"
    ATTESTATION_UNREADABLE = "ATTESTATION_UNREADABLE"
    ATTESTATION_TAMPERED = "ATTESTATION_TAMPERED"
    ATTESTATION_ID_MISMATCH = "ATTESTATION_ID_MISMATCH"
    EVIDENCE_TIME_SUBSTITUTION = "EVIDENCE_TIME_SUBSTITUTION"


class AttestationError(RuntimeError):
    """凭证层失败（**fail-closed**：调用方按退出码处理，不得假设已落盘任何 artifact）。"""


class AttestationArgumentError(AttestationError):
    """参数错误（缺必填参数、候选选择歧义、时点缺时区等）。"""


class AttestationPathError(AttestationError):
    """输入 / 输出路径不可用（含把凭证写进 inbox 或核验输入文件上的拒绝）。"""


class AttestationStateError(AttestationError):
    """状态不可用（漂移 / 篡改 / 冲突 / 非法人工输入；**零写入**）。"""


class AttestationNotAttestableError(AttestationStateError):
    """包**不可**被核验（Mock / 模板 / 合成 / preflight 未通过 / 材料不可核验）。"""


class AttestationWriteError(AttestationError):
    """凭证原子写失败（**fail-closed**）。"""


#: 异常 -> 稳定退出码（未知异常一律 ``4``：fail-closed、不得假设已写入）
_EXIT_CODES: Final[tuple[tuple[type[BaseException], int], ...]] = (
    (AttestationNotAttestableError, EXIT_BLOCKED),
    (AttestationArgumentError, EXIT_CONFIG_ERROR),
    (AttestationPathError, EXIT_WORKDIR_UNUSABLE),
    (AttestationWriteError, EXIT_WORKDIR_UNUSABLE),
    (AttestationStateError, EXIT_STATE_INVALID),
)


def exit_code_for(error: BaseException) -> int:
    """把异常映射为**稳定**退出码（未知异常一律 ``4``：fail-closed、不得假设已写入）。"""
    for error_type, code in _EXIT_CODES:
        if isinstance(error, error_type):
            return code
    return EXIT_STATE_INVALID


def main_verification_exit_code(verified: bool) -> int:
    """verify 模式结论 → 退出码（通过 ``0``，不通过 ``4``）。"""
    return EXIT_OK if verified else EXIT_STATE_INVALID



# ---------------------------------------------------------------------------
# 通用脱敏 / 解析助手（与 GOLD-015/016 同一口径，不复刻业务规则）
# ---------------------------------------------------------------------------
def _safe(value: object, *, max_chars: int = MAX_REASON_CODE_CHARS) -> str:
    """统一脱敏 + 截断（写盘 / 打印前的**唯一**入口）。"""
    return safe_text(str(value), max_chars=max_chars)


def _safe_path(value: object, *, max_chars: int = 200) -> str:
    """路径的脱敏 + 截断表示（审计用；不做任何真实 IO）。"""
    return safe_text(str(value), max_chars=max_chars)


def _is_hex64(value: object) -> bool:
    """是否是 64 位小写十六进制摘要（内容寻址字段的**唯一**合法形态）。"""
    return isinstance(value, str) and len(value) == 64 and all(ch in _HEX_DIGITS for ch in value)


def _canonical_json(payload: object) -> str:
    """确定性 JSON（排序键 + 紧凑分隔符 + 保留中文），内容摘要的**唯一**口径。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_aware(moment: object, *, field_name: str) -> datetime:
    """要求带时区的 ``datetime``（naive → 参数错误，绝不默认 UTC 猜测）。"""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise AttestationArgumentError(f"{field_name} 必须是带时区的 datetime")
    return moment


def _parse_aware_moment(raw: object, *, field_name: str) -> datetime:
    """解析 ISO8601 且**必须带时区**（缺时区 / 非法 → 稳定原因码 fail-closed）。"""
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.utcoffset() is None:
            raise AttestationStateError(
                f"{AttestationCode.REVIEWED_AT_INVALID.value}：{field_name} 必须带时区"
            )
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise AttestationStateError(
            f"{AttestationCode.REVIEWED_AT_INVALID.value}：{field_name} 缺失或不是字符串"
        )
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AttestationStateError(
            f"{AttestationCode.REVIEWED_AT_INVALID.value}：{field_name} 不是合法 ISO8601："
            f"{_safe(raw, max_chars=40)}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttestationStateError(
            f"{AttestationCode.REVIEWED_AT_INVALID.value}：{field_name} 必须显式带时区"
            "（拒绝 naive 时间：绝不隐式当 UTC）"
        )
    return parsed


def _as_moment(raw: object) -> datetime | None:
    """宽松解析（核验路径用）：不合法时返回 ``None``，由调用方记一条原因码。"""
    try:
        return _parse_aware_moment(raw, field_name="timestamp")
    except AttestationError:
        return None


def _add(found: list[tuple[str, str]], code: AttestationCode, detail: str) -> None:
    """记一条（稳定原因码, 已脱敏说明）。"""
    found.append((code.value, _safe(detail, max_chars=200)))


def _format_violations(violations: Sequence[tuple[str, str]], *, what: str) -> str:
    """把（稳定原因码, 说明）列表汇总成**可稳定解析**的一句话（原因码在最前）。"""
    codes = sorted({code for code, _ in violations})
    details = "；".join(detail for _, detail in violations[:4])
    return (
        f"{'、'.join(codes)}：{what}未通过（{len(violations)} 项，fail-closed、零写入）：{details}"
    )


def _read_json_object(
    path: Path, *, missing_code: str, unreadable_code: str, what: str
) -> dict[str, Any]:
    """读取一个 JSON 对象（不存在 / 不可读 / 非法 / 非对象 → 稳定原因码 fail-closed）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AttestationPathError(f"{missing_code}：{what}不存在：{_safe_path(path)}") from exc
    except OSError as exc:
        raise AttestationStateError(
            f"{unreadable_code}：{what}不可读（{type(exc).__name__}）：{_safe_path(path)}"
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise AttestationStateError(
            f"{unreadable_code}：{what}不是合法 JSON：{_safe_path(path)}"
        ) from exc
    if not isinstance(payload, dict):
        raise AttestationStateError(
            f"{unreadable_code}：{what}的 JSON 顶层必须是对象：{_safe_path(path)}"
        )
    return payload



# ---------------------------------------------------------------------------
# 材料契约（**只引用** GOLD-027 ``MATERIAL_SPECS``，不新增第二套材料 / 阈值）
# ---------------------------------------------------------------------------
_SPEC_BY_KEY: Final[dict[str, MaterialSpec]] = {spec.key: spec for spec in MATERIAL_SPECS}


def attestable_material_keys() -> tuple[str, ...]:
    """可由人工核验的材料 key（= GOLD-027 全部非 Gate 材料；顺序即输出顺序）。

    Raises:
        ValueError: Gate 材料契约漂移（不是恰好一个）—— 绝不静默按"不可核验"处理。
    """
    gates = tuple(spec.key for spec in MATERIAL_SPECS if spec.category == _GATE_CATEGORY)
    if len(gates) != 1:
        raise ValueError(f"handoff Gate 材料契约漂移（应为恰好 1 个）：{list(gates)}")
    return tuple(spec.key for spec in MATERIAL_SPECS if spec.category != _GATE_CATEGORY)


def _check_material_coverage() -> None:
    """契约漂移守卫：必核验材料**至少**要覆盖授权 / 来源 / 时间语义 / 可用性（OOS）。

    Raises:
        ValueError: GOLD-027 材料契约漂移导致覆盖不足（绝不静默放过）。
    """
    keys = set(attestable_material_keys())
    categories = {spec.category for spec in MATERIAL_SPECS if spec.key in keys}
    missing = sorted(set(REQUIRED_VERIFICATION_CATEGORIES) - categories)
    if missing:
        raise ValueError(f"必核验材料覆盖漂移（缺少类别）：{missing}")
    if "time_semantics" not in keys:
        raise ValueError("必核验材料契约漂移：缺少独立时间语义材料 `time_semantics`")


def _spec_for(key: str) -> MaterialSpec:
    """按 key 取材料契约；未知 key 显式失败（绝不静默）。"""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise AttestationStateError(
            f"{AttestationCode.UNKNOWN_MATERIAL.value}：`{_safe(key, max_chars=64)}` 不是当前"
            " handoff 材料契约里的材料"
        )
    return spec


# ---------------------------------------------------------------------------
# 内容身份（package / handoff）—— **不含任何审计时间**，用于漂移检测
# ---------------------------------------------------------------------------
def _material_fact_payload(material: HandoffMaterial) -> dict[str, Any]:
    """单个材料的**结构事实**（不含自由文本）：结构漂移必须改变内容身份。"""
    return {
        "key": material.key,
        "category": material.category,
        "scope": material.scope,
        "status": material.status.value,
        "present_fields": list(material.present_fields),
        "missing_fields": list(material.missing_fields),
    }


def package_content_sha256(package: PackageHandoff) -> str:
    """**当前**候选包 manifest / 材料结构的内容身份（漂移检测用；零 I/O）。"""
    payload = {
        "package_dir": package.package_dir,
        "fingerprint": package.fingerprint,
        "scope": package.scope,
        "inbox_status": package.inbox_status,
        "synthetic": package.synthetic,
        "preflight_pass": package.preflight_pass,
        "materials": [_material_fact_payload(item) for item in package.materials],
    }
    return sha256_text(_canonical_json(payload))


def handoff_content_sha256(document: IntakeHandoffDocument) -> str:
    """GOLD-027 intake handoff / manifest 的**内容身份**（不含 ``as_of``，可复现）。"""
    payload = {
        "kind": document.kind,
        "schema_version": document.schema_version,
        "contract_version": document.contract_version,
        "blocker_code": document.blocker_code,
        "inbox_dir": document.inbox_dir,
        "scanned": document.scanned,
        "packages": [
            {
                "package_dir": package.package_dir,
                "fingerprint": package.fingerprint,
                "scope": package.scope,
                "inbox_status": package.inbox_status,
                "synthetic": package.synthetic,
                "preflight_pass": package.preflight_pass,
                "package_content_sha256": package_content_sha256(package),
            }
            for package in document.packages
        ],
    }
    return sha256_text(_canonical_json(payload))


#: 凭证 id 的**策略块**（安全字段硬编码，与任何输入无关；绝不透传上游）
def _policy_block() -> dict[str, Any]:
    """返回硬编码安全策略块（任何输入都无法把它改成"通过"）。"""
    return {
        "attestation_scope": ATTESTATION_SCOPE,
        "attestation_execution_mode": ATTESTATION_EXECUTION_MODE,
        "reviewer_kind": REVIEWER_KIND,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "l3_l4_auto_advance_allowed": False,
        "evidence_qualified": False,
        "advance_allowed": False,
        "gate_blocked": True,
        "writes_database": False,
        "writes_project_state": False,
        "collects_evidence": False,
        "all_required_verified_is_qualification": False,
    }



# ---------------------------------------------------------------------------
# 版本化凭证契约（人工据此准备材料；机器可读、可 diff）
# ---------------------------------------------------------------------------
def attestation_schema() -> dict[str, Any]:
    """返回**版本化** Human Verification Attestation 契约。

    这是"材料级人工核验"的机器可读来源：必核验材料清单（**只引用** GOLD-027
    ``MATERIAL_SPECS``，不新增 / 不降低任何资格阈值）、受控决策词表、稳定原因码、
    核验输入文件格式、reviewer / 独立引用约束，以及 ``all_required_verified`` 的诚实语义。
    """
    _check_material_coverage()
    keys = attestable_material_keys()
    return {
        "kind": ATTESTATION_KIND,
        "report": ATTESTATION_REPORT_NAME,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "contract_version": ATTESTATION_CONTRACT_VERSION,
        "verification_input_schema_version": VERIFICATION_INPUT_SCHEMA_VERSION,
        "execution_mode": ATTESTATION_EXECUTION_MODE,
        "attestation_scope": ATTESTATION_SCOPE,
        "blocker_code": PHASE3_3_BLOCKER_CODE,
        "bound_to": {
            "package_fingerprint": "当前候选包的 GOLD-011 / GOLD-027 内容级 fingerprint",
            "package_content_sha256": "当前候选包材料结构的内容身份（本模块派生）",
            "handoff_content_sha256": "GOLD-027 intake handoff / manifest 的内容身份（不含 as_of）",
            "handoff_schema_version": INTAKE_HANDOFF_SCHEMA_VERSION,
            "scope": "候选包 scope（author / news；决定 Author 专有材料是否纳入）",
            "contract_version": EVIDENCE_CONTRACT_VERSION,
        },
        "package_layout": "沿用 GOLD-011 inbox 布局：显式 --inbox-dir 下的直接子目录（或单文件）",
        "materials": [
            {
                "key": _spec_for(key).key,
                "category": _spec_for(key).category,
                "scope": _spec_for(key).scope,
                "requirement": _spec_for(key).requirement,
                "contract_fields": list(_spec_for(key).contract_fields),
            }
            for key in keys
        ],
        "required_categories": list(REQUIRED_VERIFICATION_CATEGORIES),
        "decisions": list(VERIFICATION_DECISIONS),
        "verification_input": {
            "format": "JSON 对象；未知顶层 / 材料项键一律拒绝（防止夹带凭据 / 正文）",
            "required_keys": ["schema_version", "package_fingerprint", "materials"],
            "optional_keys": ["handoff_content_sha256", "scope", "reviewer"],
            "material_decision_keys": sorted(_MATERIAL_DECISION_KEYS),
            "reviewed_at": "ISO8601 **必须带时区**；naive 或晚于审计时点一律 fail-closed",
            "evidence_reference": (
                "独立引用：`https://<host>/...` 或 `docs/legal/...`；"
                "不接受裸文件名、`http://`、`file://` 或疑似凭据串"
            ),
        },
        "reason_codes": [code.value for code in AttestationCode],
        "all_required_verified_semantics": ALL_REQUIRED_VERIFIED_SEMANTICS,
        "all_required_verified_does_not_imply": [
            "evidence_qualified",
            "data_qualification_passed",
            f"{PHASE3_3_BLOCKER_CODE}_unblocked",
            "l3_human_gate_passed",
            "l4_review_issued",
            "phase_transition_allowed",
        ],
        "preflight_pass_semantics": PREFLIGHT_PASS_SEMANTICS,
        "notes": [
            ATTESTATION_NOTE,
            ALL_REQUIRED_VERIFIED_SEMANTICS,
            ATTESTATION_TIME_SEMANTICS_NOTE,
        ],
    }



# ---------------------------------------------------------------------------
# 人工核验输入（**必须**由人工显式给出；只接受本地 JSON 文件）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MaterialDecision:
    """一条**人工显式**给出的材料核验决策（脱敏、限长、无凭据）。"""

    material: str
    decision: str
    reason_code: str
    reviewer: str
    reviewed_at: datetime
    evidence_reference: str
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at.astimezone(UTC).isoformat(),
            "evidence_reference": self.evidence_reference,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class VerificationInput:
    """一份核验输入文件（**声明**它绑定的是哪个候选包，绝不隐式套用到别的包）。"""

    path: str
    schema_version: int
    package_fingerprint: str
    handoff_content_sha256: str | None
    scope: str | None
    reviewer: str | None
    decisions: tuple[MaterialDecision, ...]

    def for_material(self, key: str) -> MaterialDecision | None:
        """按材料 key 取决策（缺省返回 ``None``；重复已在加载期拒绝）。"""
        for item in self.decisions:
            if item.material == key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": _safe_path(self.path),
            "schema_version": self.schema_version,
            "package_fingerprint": self.package_fingerprint,
            "handoff_content_sha256": self.handoff_content_sha256,
            "scope": self.scope,
            "reviewer": self.reviewer,
            "material_count": len(self.decisions),
            "materials": sorted({item.material for item in self.decisions}),
        }


def _clean_reviewer(value: object) -> str:
    """reviewer **必须**是显式、非敏感的 operator label（不采集任何凭据）。"""
    if not isinstance(value, str) or not value.strip():
        raise AttestationStateError(
            f"{AttestationCode.REVIEWER_INVALID.value}：reviewer 必须显式给出"
            "（非敏感 operator / reviewer label；本工具不采集任何凭据）"
        )
    text = value.strip()
    if len(text) > MAX_REVIEWER_CHARS:
        raise AttestationStateError(
            f"{AttestationCode.REVIEWER_INVALID.value}：reviewer 超过 {MAX_REVIEWER_CHARS} 字符"
        )
    if not _REVIEWER_PATTERN.match(text):
        raise AttestationStateError(
            f"{AttestationCode.REVIEWER_INVALID.value}：reviewer 只允许字母 / 数字 / `._@+-`，"
            "且必须以字母或数字开头（不接受空格、引号、路径分隔符）"
        )
    if _SECRET_LIKE_PATTERN.search(text) or _BLOB_LIKE_PATTERN.match(text):
        raise AttestationStateError(
            f"{AttestationCode.SENSITIVE_VALUE_REJECTED.value}：reviewer 看起来像凭据 / 密钥串，"
            "本工具**绝不**采集任何凭据"
        )
    return text


def _clean_reason_code(value: object) -> str:
    """稳定大写原因码（脱敏 + 限长；不做静默大小写转换）。"""
    if not isinstance(value, str) or not value.strip():
        raise AttestationStateError(
            f"{AttestationCode.REASON_CODE_INVALID.value}：reason_code 必须是非空字符串"
        )
    text = value.strip()
    if len(text) > MAX_REASON_CODE_CHARS or not _REASON_CODE_PATTERN.match(text):
        raise AttestationStateError(
            f"{AttestationCode.REASON_CODE_INVALID.value}：reason_code 必须是大写稳定原因码形态"
            f"（^[A-Z][A-Z0-9_]*$，<= {MAX_REASON_CODE_CHARS} 字符）"
        )
    return safe_text(text, max_chars=MAX_REASON_CODE_CHARS)


def _clean_evidence_reference(value: object) -> str:
    """独立 evidence reference：``https://`` 出处或 ``docs/legal/`` 路径（拒绝凭据 / 裸名）。"""
    if not isinstance(value, str) or not value.strip():
        raise AttestationStateError(
            f"{AttestationCode.EVIDENCE_REFERENCE_INVALID.value}：evidence_reference 必须显式给出"
            "（独立可复核引用；不接受裸文件名 / 内联正文）"
        )
    text = value.strip()
    if len(text) > MAX_EVIDENCE_REFERENCE_CHARS:
        raise AttestationStateError(
            f"{AttestationCode.EVIDENCE_REFERENCE_INVALID.value}：evidence_reference 超过 "
            f"{MAX_EVIDENCE_REFERENCE_CHARS} 字符"
        )
    if _SECRET_LIKE_PATTERN.search(text) or _BLOB_LIKE_PATTERN.match(text):
        raise AttestationStateError(
            f"{AttestationCode.SENSITIVE_VALUE_REJECTED.value}：evidence_reference 看起来像凭据 / "
            "密钥串，本工具**绝不**采集任何凭据"
        )
    if _HTTPS_REFERENCE_PATTERN.match(text) or _LEGAL_PATH_PATTERN.match(text):
        return safe_text(text, max_chars=MAX_EVIDENCE_REFERENCE_CHARS)
    raise AttestationStateError(
        f"{AttestationCode.EVIDENCE_REFERENCE_UNTRUSTED.value}：evidence_reference 必须是 "
        "`https://<host>/...` 出处或 `docs/legal/...` 路径（拒绝 `http://` / `file://` / 裸文件名）"
    )



def _clean_note(value: object) -> str | None:
    """可选 note：**脱敏 + 限长**；超长输入 fail-closed（不静默截断成假事实）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise AttestationStateError(f"{AttestationCode.NOTE_TOO_LONG.value}：note 必须是字符串")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_NOTE_INPUT_CHARS:
        raise AttestationStateError(
            f"{AttestationCode.NOTE_TOO_LONG.value}：note 超过 {MAX_NOTE_INPUT_CHARS} 字符"
            "（fail-closed，绝不静默截断）"
        )
    return safe_text(text, max_chars=MAX_NOTE_CHARS)


def _reject_unknown_keys(
    payload: Mapping[str, Any], allowed: frozenset[str], *, where: str
) -> None:
    """未知键一律拒绝（防止把凭据 / 正文塞进核验输入）。"""
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AttestationStateError(
            f"{AttestationCode.UNKNOWN_FIELD.value}：{where} 出现未允许字段：{unknown}"
        )


def _clean_decision_value(value: object) -> VerificationDecision:
    """受控决策词表（**必须**由人工显式给出；不做任何静默转换）。"""
    if isinstance(value, VerificationDecision):
        return value
    text = value if isinstance(value, str) else ""
    if text not in VERIFICATION_DECISIONS:
        raise AttestationStateError(
            f"{AttestationCode.DECISION_INVALID.value}：decision 必须是 "
            f"{' / '.join(VERIFICATION_DECISIONS)} 之一；实际：{_safe(value, max_chars=40)!r}"
        )
    return VerificationDecision(text)



def _material_decision_from_payload(
    raw: Mapping[str, Any], *, index: int, default_reviewer: object, moment: datetime
) -> MaterialDecision:
    """把一条材料决策解析为受控对象（逐项严格校验；任一不合法即 fail-closed）。"""
    _reject_unknown_keys(raw, _MATERIAL_DECISION_KEYS, where=f"materials[{index}]")
    material = raw.get("material")
    if not isinstance(material, str) or not material.strip():
        raise AttestationStateError(
            f"{AttestationCode.UNKNOWN_MATERIAL.value}：materials[{index}].material 必须显式给出"
        )
    key = material.strip()
    _spec_for(key)  # 未知材料立即 fail-closed
    if key not in attestable_material_keys():
        raise AttestationStateError(
            f"{AttestationCode.MATERIAL_NOT_VERIFIABLE.value}：`{key}` 不是可人工核验的材料"
            "（Gate 材料不可被核验为 VERIFIED）"
        )
    decision = _clean_decision_value(raw.get("decision"))
    reason_code = _clean_reason_code(raw.get("reason_code"))
    reference = _clean_evidence_reference(raw.get("evidence_reference"))
    reviewer = _clean_reviewer(raw.get("reviewer", default_reviewer))
    reviewed_at = _parse_aware_moment(
        raw.get("reviewed_at"), field_name=f"materials[{index}].reviewed_at"
    )
    if reviewed_at.astimezone(UTC) > moment:
        raise AttestationStateError(
            f"{AttestationCode.REVIEWED_AT_FUTURE.value}：materials[{index}].reviewed_at"
            f"（{reviewed_at.astimezone(UTC).isoformat()}）晚于审计时点，fail-closed"
        )
    note = _clean_note(raw.get("note"))
    return MaterialDecision(
        material=key,
        decision=decision.value,
        reason_code=reason_code,
        reviewer=reviewer,
        reviewed_at=reviewed_at.astimezone(UTC),
        evidence_reference=reference,
        note=note,
    )


def load_verification_input(path: Path | str, *, moment: datetime) -> VerificationInput:
    """读取**人工显式**给出的核验输入文件（**只读**；零网络 / 零数据库 / 零写入）。

    Raises:
        AttestationPathError: 文件不存在。
        AttestationStateError: 非法 JSON / 结构 / 未知字段 / 非法决策 / 敏感值 / 未来时间。
    """
    moment = _require_aware(moment, field_name="moment")
    target = Path(path)
    payload = _read_json_object(
        target,
        missing_code=AttestationCode.VERIFICATION_NOT_FOUND.value,
        unreadable_code=AttestationCode.VERIFICATION_UNREADABLE.value,
        what="人工核验输入",
    )
    _reject_unknown_keys(payload, _VERIFICATION_INPUT_KEYS, where="核验输入顶层")
    schema_version = payload.get("schema_version")
    if schema_version != VERIFICATION_INPUT_SCHEMA_VERSION:
        raise AttestationStateError(
            f"{AttestationCode.VERIFICATION_INVALID.value}：schema_version 必须是 "
            f"{VERIFICATION_INPUT_SCHEMA_VERSION}（实际 {_safe(schema_version, max_chars=20)}）"
        )
    fingerprint = payload.get("package_fingerprint")
    if not _is_hex64(fingerprint):
        raise AttestationStateError(
            f"{AttestationCode.VERIFICATION_INVALID.value}：package_fingerprint 必须是 64 位"
            "小写十六进制（**必须**显式绑定当前候选包，绝不隐式套用）"
        )
    handoff_hash = payload.get("handoff_content_sha256")
    if handoff_hash is not None and not _is_hex64(handoff_hash):
        raise AttestationStateError(
            f"{AttestationCode.VERIFICATION_INVALID.value}：handoff_content_sha256 必须是 64 位"
            "小写十六进制或省略"
        )
    scope_raw = payload.get("scope")
    scope: str | None = None
    if scope_raw is not None:
        known_scopes = {item.value for item in EvidenceScope}
        if not isinstance(scope_raw, str) or scope_raw not in known_scopes:
            raise AttestationStateError(
                f"{AttestationCode.VERIFICATION_INVALID.value}：scope 必须是 "
                f"{sorted(known_scopes)} 之一或省略"
            )
        scope = scope_raw
    reviewer_raw = payload.get("reviewer")
    reviewer: str | None = None
    if reviewer_raw is not None:
        reviewer = _clean_reviewer(reviewer_raw)
    materials_raw = payload.get("materials")
    if not isinstance(materials_raw, list):
        raise AttestationStateError(
            f"{AttestationCode.VERIFICATION_INVALID.value}：materials 必须是数组"
        )
    decisions: list[MaterialDecision] = []
    seen: set[str] = set()
    for index, item in enumerate(materials_raw):
        if not isinstance(item, Mapping):
            raise AttestationStateError(
                f"{AttestationCode.VERIFICATION_INVALID.value}：materials[{index}] 必须是对象"
            )
        decision = _material_decision_from_payload(
            item, index=index, default_reviewer=reviewer, moment=moment
        )
        if decision.material in seen:
            raise AttestationStateError(
                f"{AttestationCode.DUPLICATE_MATERIAL.value}：材料 `{decision.material}` 出现多次"
                "（同一次核验每个材料只能有一个受控决策）"
            )
        seen.add(decision.material)
        decisions.append(decision)
    return VerificationInput(
        path=_safe_path(target),
        schema_version=VERIFICATION_INPUT_SCHEMA_VERSION,
        package_fingerprint=str(fingerprint),
        handoff_content_sha256=str(handoff_hash) if handoff_hash is not None else None,
        scope=scope,
        reviewer=reviewer,
        decisions=tuple(decisions),
    )



# ---------------------------------------------------------------------------
# 结论对象：材料核验 / 凭证 / 防伪核验（同一事实来源，全部脱敏）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MaterialAttestation:
    """一条材料的**人工核验结论**（``counts_as_verified`` 是唯一的计入口径）。"""

    material: str
    category: str
    scope: str
    intake_status: str
    requirement: str
    decision: str | None
    decision_recorded: bool
    counts_as_verified: bool
    reason_code: str | None
    reviewer: str | None
    reviewed_at: datetime | None
    evidence_reference: str | None
    note: str | None
    codes: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """凭证 id 绑定用的最小载荷（**不含**派生文本，保证重算一致）。"""
        return {
            "material": self.material,
            "intake_status": self.intake_status,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reviewer": self.reviewer,
            "reviewed_at": (
                self.reviewed_at.astimezone(UTC).isoformat() if self.reviewed_at else None
            ),
            "evidence_reference": self.evidence_reference,
            "note": self.note,
            "counts_as_verified": self.counts_as_verified,
        }

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**：只有拒绝 / 计数 / 稳定原因码与显式引用）。"""
        return {
            **self.payload(),
            "category": self.category,
            "scope": self.scope,
            "requirement": self.requirement,
            "decision_recorded": self.decision_recorded,
            "codes": list(self.codes),
        }


def compute_attestation_id(
    *,
    revision: int,
    supersedes: str | None,
    package_block: Mapping[str, Any],
    material_payloads: Sequence[Mapping[str, Any]],
) -> str:
    """由（策略块 + 修订 + package 绑定 + 材料核验）派生内容级 ``attestation_id``。"""
    payload = {
        "policy": _policy_block(),
        "revision": revision,
        "supersedes": supersedes,
        "package": {key: package_block.get(key) for key in _PACKAGE_BINDING_KEYS},
        "materials": [
            {key: item.get(key) for key in _MATERIAL_PAYLOAD_KEYS} for item in material_payloads
        ],
    }
    return sha256_text(_canonical_json(payload))


def _forked_verification_view(package: PackageHandoff) -> tuple[HandoffMaterial, ...]:
    """取当前候选包中**可人工核验**的材料（Gate 材料不参与材料级核验）。"""
    return tuple(item for item in package.materials if item.category != _GATE_CATEGORY)


def _material_attestation(
    material: HandoffMaterial,
    decision: MaterialDecision | None,
    *,
    package_attestable: bool,
) -> MaterialAttestation:
    """把 1 条材料 + 人工决策判定为核验结论（**不**自行生成任何批准）。"""
    verifiable = material.status is IntakeStatus.HUMAN_VERIFICATION_REQUIRED
    codes: list[str] = []
    counts_as_verified = False
    if decision is None:
        codes.append(AttestationCode.MATERIAL_DECISION_MISSING.value)
    elif decision.decision == VerificationDecision.VERIFIED.value:
        if verifiable and package_attestable:
            counts_as_verified = True
        else:
            codes.append(AttestationCode.MATERIAL_NOT_VERIFIABLE.value)
    elif decision.decision == VerificationDecision.REJECTED.value:
        codes.append(AttestationCode.MATERIAL_REJECTED.value)
    else:
        codes.append(AttestationCode.MATERIAL_NEEDS_CHANGES.value)
    return MaterialAttestation(
        material=material.key,
        category=material.category,
        scope=material.scope,
        intake_status=material.status.value,
        requirement=material.requirement,
        decision=None if decision is None else decision.decision,
        decision_recorded=decision is not None,
        counts_as_verified=counts_as_verified,
        reason_code=None if decision is None else decision.reason_code,
        reviewer=None if decision is None else decision.reviewer,
        reviewed_at=None if decision is None else decision.reviewed_at,
        evidence_reference=None if decision is None else decision.evidence_reference,
        note=None if decision is None else decision.note,
        codes=tuple(codes),
    )



@dataclass(frozen=True, slots=True)
class HumanVerificationAttestation:
    """**材料级人工核验凭证**（确定性脱敏；默认零写入，``written_path`` 才落盘）。

    Attributes:
        generated_at / attested_at: 本次核验的**审计操作时间**（**不是**证据时间）。
        attestation_id: 内容级摘要（策略块 + 修订 + package 绑定 + 材料核验）。
        all_required_verified: **只**代表材料级人工核验完成；**绝不**代表资格通过。
    """

    schema_version: int
    kind: str
    report: str
    contract_version: str
    execution_mode: str
    attestation_scope: str
    scope: str
    generated_at: datetime
    attested_at: datetime
    attestation_id: str
    revision: int
    supersedes: str | None
    package_dir: str
    package_fingerprint: str
    package_content_sha256: str
    handoff_content_sha256: str
    handoff_contract_version: str
    handoff_schema_version: int
    inbox_dir: str | None
    inbox_status: str
    synthetic: bool
    preflight_pass: bool
    all_required_verified: bool
    required_material_count: int
    verified_material_count: int
    rejected_material_count: int
    needs_changes_material_count: int
    missing_decision_material_count: int
    materials: tuple[MaterialAttestation, ...]
    codes: tuple[str, ...]
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    data_qualification_passed: bool
    phase_transition_allowed: bool
    l3_l4_auto_advance_allowed: bool
    evidence_qualified: bool
    advance_allowed: bool
    gate_blocked: bool
    notes: tuple[str, ...]
    written_path: str | None = None

    @property
    def package_attestable(self) -> bool:
        """当前候选包是否**结构上**可进入材料级核验（非合成且 GOLD-027 预检通过）。"""
        return (not self.synthetic) and self.preflight_pass

    @property
    def attested_material_count(self) -> int:
        """已记录受控决策的材料数量。"""
        return sum(1 for item in self.materials if item.decision_recorded)

    def material(self, key: str) -> MaterialAttestation:
        """按 key 取材料结论；缺失即显式失败（防止口径漂移被静默忽略）。"""
        for item in self.materials:
            if item.material == key:
                return item
        raise KeyError(key)

    def package_block(self) -> dict[str, Any]:
        """凭证 id 绑定用的 package 块（**不含**任何审计时间）。"""
        return {
            "scope": self.scope,
            "package_dir": self.package_dir,
            "fingerprint": self.package_fingerprint,
            "package_content_sha256": self.package_content_sha256,
            "handoff_content_sha256": self.handoff_content_sha256,
            "handoff_contract_version": self.handoff_contract_version,
            "handoff_schema_version": self.handoff_schema_version,
            "inbox_status": self.inbox_status,
            "synthetic": self.synthetic,
            "preflight_pass": self.preflight_pass,
        }

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全布尔恒为诚实取值）。"""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "report": self.report,
            "contract_version": self.contract_version,
            "execution_mode": self.execution_mode,
            "attestation_scope": self.attestation_scope,
            "scope": self.scope,
            "generated_at": self.generated_at.isoformat(),
            "attested_at": self.attested_at.isoformat(),
            "attestation_id": self.attestation_id,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "package": self.package_block(),
            "package_dir": self.package_dir,
            "package_fingerprint": self.package_fingerprint,
            "package_content_sha256": self.package_content_sha256,
            "handoff_content_sha256": self.handoff_content_sha256,
            "handoff_contract_version": self.handoff_contract_version,
            "handoff_schema_version": self.handoff_schema_version,
            "inbox_dir": self.inbox_dir,
            "inbox_status": self.inbox_status,
            "synthetic": self.synthetic,
            "preflight_pass": self.preflight_pass,
            "all_required_verified": self.all_required_verified,
            "all_required_verified_meaning": ALL_REQUIRED_VERIFIED_SEMANTICS,
            "all_required_verified_is_qualification": False,
            "required_material_count": self.required_material_count,
            "verified_material_count": self.verified_material_count,
            "rejected_material_count": self.rejected_material_count,
            "needs_changes_material_count": self.needs_changes_material_count,
            "missing_decision_material_count": self.missing_decision_material_count,
            "materials": [item.to_dict() for item in self.materials],
            "codes": list(self.codes),
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "data_qualification_passed": self.data_qualification_passed,
            "phase_transition_allowed": self.phase_transition_allowed,
            "l3_l4_auto_advance_allowed": self.l3_l4_auto_advance_allowed,
            "evidence_qualified": self.evidence_qualified,
            "advance_allowed": self.advance_allowed,
            "gate_blocked": self.gate_blocked,
            "schema": attestation_schema(),
            "notes": list(self.notes),
        }



def _select_package(document: IntakeHandoffDocument, selector: str | None) -> PackageHandoff:
    """选择**当前**候选包（按 fingerprint / 目录名 / 目录路径）；歧义 / 缺失 → fail-closed。"""
    if selector is None:
        if not document.packages:
            raise AttestationNotAttestableError(
                f"{AttestationCode.PACKAGE_NOT_FOUND.value}：inbox 内没有任何候选包"
                "（结构缺口 → fail-closed）"
            )
        if len(document.packages) == 1:
            return document.packages[0]
        raise AttestationArgumentError(
            f"{AttestationCode.PACKAGE_SELECTION_REQUIRED.value}：候选包不止一个（"
            f"{len(document.packages)} 个），必须显式指定 --package（目录名或 fingerprint）"
        )
    wanted = selector.strip()
    matches = [
        package
        for package in document.packages
        if package.fingerprint == wanted
        or Path(package.package_dir).name == wanted
        or package.package_dir == wanted
    ]
    if not matches:
        raise AttestationNotAttestableError(
            f"{AttestationCode.PACKAGE_NOT_FOUND.value}：inbox 内没有匹配 "
            f"`{_safe(wanted, max_chars=60)}` 的候选包（结构缺口 → fail-closed）"
        )
    if len(matches) > 1:
        raise AttestationArgumentError(
            f"{AttestationCode.PACKAGE_SELECTION_AMBIGUOUS.value}：`{_safe(wanted, max_chars=60)}`"
            " 命中多个候选包，请改用 fingerprint 显式指定"
        )
    return matches[0]


def _clean_revision(revision: object, supersedes: object) -> tuple[int, str | None]:
    """``revision`` / ``supersedes``：冲突必须**显式**声明，且 revision 必须严格递增。"""
    supersedes_id: str | None = None
    if supersedes is not None:
        if not isinstance(supersedes, str) or not _is_hex64(supersedes):
            raise AttestationArgumentError(
                f"{AttestationCode.REVISION_INVALID.value}：supersedes 必须是 64 位小写十六进制的 "
                "attestation_id"
            )
        supersedes_id = supersedes.lower()
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise AttestationArgumentError(
            f"{AttestationCode.REVISION_INVALID.value}：revision 必须是整数"
        )
    if revision < 1 or revision > MAX_REVISION:
        raise AttestationArgumentError(
            f"{AttestationCode.REVISION_INVALID.value}：revision 必须在 1 ~ {MAX_REVISION} 之间"
        )
    if supersedes_id is None and revision != 1:
        raise AttestationArgumentError(
            f"{AttestationCode.REVISION_INVALID.value}：声明 revision>1 必须同时给出 supersedes"
            "（不得静默改写历史）"
        )
    if supersedes_id is not None and revision <= 1:
        raise AttestationArgumentError(
            f"{AttestationCode.REVISION_INVALID.value}：supersedes 必须使用 revision >= 2"
        )
    return revision, supersedes_id



def build_attestation(
    package: PackageHandoff,
    handoff: IntakeHandoffDocument,
    verification: VerificationInput,
    *,
    moment: datetime,
    revision: int = 1,
    supersedes: str | None = None,
) -> HumanVerificationAttestation:
    """由**只读** handoff 事实 + **人工显式**核验输入构造材料级核验凭证（纯函数）。

    Raises:
        AttestationStateError: 绑定漂移 / 非法人工输入 / 对不可核验材料声明 VERIFIED。
    """
    moment = _require_aware(moment, field_name="moment").astimezone(UTC)
    _check_material_coverage()
    if verification.package_fingerprint != package.fingerprint:
        raise AttestationStateError(
            f"{AttestationCode.PACKAGE_FINGERPRINT_MISMATCH.value}：核验输入绑定的是 "
            f"`{verification.package_fingerprint[:16]}…`，但当前候选包 fingerprint 是 "
            f"`{package.fingerprint[:16]}…`（package 漂移 → fail-closed，绝不静默继承）"
        )
    current_package_hash = package_content_sha256(package)
    current_handoff_hash = handoff_content_sha256(handoff)
    if (
        verification.handoff_content_sha256 is not None
        and verification.handoff_content_sha256 != current_handoff_hash
    ):
        raise AttestationStateError(
            f"{AttestationCode.HANDOFF_CONTENT_MISMATCH.value}：核验输入绑定的 handoff 内容身份"
            "与**当前** intake handoff / manifest 不一致（content 漂移 → fail-closed）"
        )
    if verification.scope is not None and verification.scope != package.scope:
        raise AttestationStateError(
            f"{AttestationCode.SCOPE_MISMATCH.value}：核验输入 scope=`{verification.scope}`，"
            f"当前候选包 scope=`{package.scope}`（fail-closed）"
        )
    attestable = (not package.synthetic) and package.preflight_pass
    subjects = _forked_verification_view(package)
    claimed_verified: list[HandoffMaterial] = []
    for material in subjects:
        decision = verification.for_material(material.key)
        if decision is not None and decision.decision == VerificationDecision.VERIFIED.value:
            claimed_verified.append(material)
    if claimed_verified and package.synthetic:
        raise AttestationNotAttestableError(
            f"{AttestationCode.NON_QUALIFYING_PACKAGE.value}：当前候选包带 Mock / 模板 / "
            "示例 / 合成标记，**任何**材料都不得被核验为 VERIFIED（fail-closed）"
        )
    for material in claimed_verified:
        if material.status is not IntakeStatus.HUMAN_VERIFICATION_REQUIRED:
            raise AttestationNotAttestableError(
                f"{AttestationCode.MATERIAL_NOT_VERIFIABLE.value}：材料 `{material.key}` 当前状态是"
                f"`{material.status.value}`（不是 HUMAN_VERIFICATION_REQUIRED），**不允许**被声明"
                "为 VERIFIED（Mock / 模板 / 缺独立证据一律 fail-closed）"
            )
    if claimed_verified and not package.preflight_pass:
        raise AttestationNotAttestableError(
            f"{AttestationCode.PREFLIGHT_NOT_PASSED.value}：GOLD-027 预检未通过"
            f"（inbox_status=`{package.inbox_status}`）；结构不完整的包不得被核验为 VERIFIED"
            "（fail-closed）"
        )


    materials = tuple(
        _material_attestation(
            material, verification.for_material(material.key), package_attestable=attestable
        )
        for material in subjects
    )
    codes: list[str] = []
    if package.synthetic:
        codes.append(AttestationCode.NON_QUALIFYING_PACKAGE.value)
    elif not package.preflight_pass:
        codes.append(AttestationCode.PREFLIGHT_NOT_PASSED.value)
    for item in materials:
        codes.extend(item.codes)
    required = len(materials)
    verified = sum(1 for item in materials if item.counts_as_verified)
    rejected = sum(1 for item in materials if item.decision == VerificationDecision.REJECTED.value)
    needs_changes = sum(
        1 for item in materials if item.decision == VerificationDecision.NEEDS_CHANGES.value
    )
    missing = sum(1 for item in materials if not item.decision_recorded)
    all_required_verified = attestable and required > 0 and verified == required
    package_block: dict[str, Any] = {
        "scope": package.scope,
        "package_dir": package.package_dir,
        "fingerprint": package.fingerprint,
        "package_content_sha256": current_package_hash,
        "handoff_content_sha256": current_handoff_hash,
        "handoff_contract_version": handoff.contract_version,
        "handoff_schema_version": handoff.schema_version,
        "inbox_status": package.inbox_status,
        "synthetic": package.synthetic,
        "preflight_pass": package.preflight_pass,
    }
    attestation_id = compute_attestation_id(
        revision=revision,
        supersedes=supersedes,
        package_block=package_block,
        material_payloads=[item.payload() for item in materials],
    )
    return HumanVerificationAttestation(
        schema_version=ATTESTATION_SCHEMA_VERSION,
        kind=ATTESTATION_KIND,
        report=ATTESTATION_REPORT_NAME,
        contract_version=ATTESTATION_CONTRACT_VERSION,
        execution_mode=ATTESTATION_EXECUTION_MODE,
        attestation_scope=ATTESTATION_SCOPE,
        scope=package.scope,
        generated_at=moment,
        attested_at=moment,
        attestation_id=attestation_id,
        revision=revision,
        supersedes=supersedes,
        package_dir=package.package_dir,
        package_fingerprint=package.fingerprint,
        package_content_sha256=current_package_hash,
        handoff_content_sha256=current_handoff_hash,
        handoff_contract_version=handoff.contract_version,
        handoff_schema_version=handoff.schema_version,
        inbox_dir=handoff.inbox_dir,
        inbox_status=package.inbox_status,
        synthetic=package.synthetic,
        preflight_pass=package.preflight_pass,
        all_required_verified=all_required_verified,
        required_material_count=required,
        verified_material_count=verified,
        rejected_material_count=rejected,
        needs_changes_material_count=needs_changes,
        missing_decision_material_count=missing,
        materials=materials,
        codes=tuple(dict.fromkeys(codes)),
        # 以下安全字段全部**硬编码**：任何输入 / 上游报告都无法把它们改成"通过"
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=True,
        human_gate_required=True,
        data_qualification_passed=False,
        phase_transition_allowed=False,
        l3_l4_auto_advance_allowed=False,
        evidence_qualified=False,
        advance_allowed=False,
        gate_blocked=True,
        notes=(
            ATTESTATION_NOTE,
            ALL_REQUIRED_VERIFIED_SEMANTICS,
            ATTESTATION_TIME_SEMANTICS_NOTE,
            PREFLIGHT_PASS_SEMANTICS,
        ),
    )



# ---------------------------------------------------------------------------
# 凭证文档的**严格只读**校验（被改写 → ATTESTATION_TAMPERED，fail-closed）
# ---------------------------------------------------------------------------
def _forbidden_key_paths(payload: object, *, prefix: str = "") -> list[str]:
    """递归找出所有**禁止的证据时间键**路径（凭证绝不允许出现证据时间字段）。"""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key)
            location = f"{prefix}.{name}" if prefix else name
            if name in FORBIDDEN_EVIDENCE_KEYS:
                found.append(location)
            found.extend(_forbidden_key_paths(value, prefix=location))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(_forbidden_key_paths(item, prefix=f"{prefix}[{index}]"))
    return found


def _verify_materials_block(block: object, *, found: list[tuple[str, str]]) -> int:
    """凭证材料块：键齐备 / 决策受控 / 计数一致 / 独立引用非空（被改写 → tamper）。"""
    if not isinstance(block, list) or not block:
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "凭证 materials 必须是非空数组")
        return 0
    verified = 0
    seen: set[str] = set()
    for index, item in enumerate(block):
        if not isinstance(item, Mapping):
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"materials[{index}] 必须是对象")
            continue
        missing = [key for key in _MATERIAL_PAYLOAD_KEYS if key not in item]
        if missing:
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"materials[{index}] 缺字段：" + "、".join(sorted(missing)),
            )
        key = item.get("material")
        if not isinstance(key, str) or not key:
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"materials[{index}].material 非法")
        elif key in seen:
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"materials[{index}] 材料重复：{key}")
        else:
            seen.add(key)
        decision = item.get("decision")
        if decision is not None and decision not in VERIFICATION_DECISIONS:
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"materials[{index}].decision 非法")
        flags = item.get("counts_as_verified")
        if not isinstance(flags, bool):
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"materials[{index}].counts_as_verified 必须是布尔",
            )
        elif flags:
            verified += 1
            if decision != VerificationDecision.VERIFIED.value:
                _add(
                    found,
                    AttestationCode.ATTESTATION_TAMPERED,
                    f"materials[{index}] 计为已核验但 decision 不是 VERIFIED",
                )
        reviewer = item.get("reviewer")
        if reviewer is not None and (not isinstance(reviewer, str) or not reviewer):
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"materials[{index}].reviewer 非法")
        if decision is not None and not reviewer:
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"materials[{index}] 有决策但缺 reviewer（必须可追溯）",
            )
        reviewed_at = item.get("reviewed_at")
        if decision is not None and _as_moment(reviewed_at) is None:
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"materials[{index}].reviewed_at 必须是带时区的 ISO8601",
            )
        reference = item.get("evidence_reference")
        if not isinstance(reference, str) or not reference.strip():
            _add(
                found,
                AttestationCode.MATERIAL_REFERENCE_MISSING,
                f"materials[{index}] 缺独立 evidence reference",
            )
    return verified


def _verify_package_block(block: object, *, found: list[tuple[str, str]]) -> tuple[bool, bool, int]:
    """凭证 package 绑定块（被改写 → tamper）；返回 ``(synthetic, preflight, schema_version)``。"""
    if not isinstance(block, Mapping):
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "凭证缺 package 绑定块")
        return False, False, -1
    missing = [key for key in _PACKAGE_BINDING_KEYS if key not in block]
    if missing:
        _add(
            found,
            AttestationCode.ATTESTATION_TAMPERED,
            "凭证 package 绑定块缺字段：" + "、".join(sorted(missing)),
        )
    for key in ("fingerprint", "package_content_sha256", "handoff_content_sha256"):
        if not _is_hex64(block.get(key)):
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"凭证 package.{key} 必须是摘要")
    for key in ("scope", "package_dir", "inbox_status", "handoff_contract_version"):
        if not isinstance(block.get(key), str) or not block.get(key):
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"凭证 package.{key} 必须是非空字符串",
            )
    for key in ("synthetic", "preflight_pass"):
        if not isinstance(block.get(key), bool):
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"凭证 package.{key} 必须是布尔")
    handoff_schema = block.get("handoff_schema_version")
    if not isinstance(handoff_schema, int) or isinstance(handoff_schema, bool):
        _add(
            found,
            AttestationCode.ATTESTATION_TAMPERED,
            "凭证 package.handoff_schema_version 必须是整数",
        )
        handoff_schema = -1
    scope = block.get("scope")
    if isinstance(scope, str) and scope not in {item.value for item in EvidenceScope}:
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "凭证 package.scope 不在受控取值内")
    return (
        block.get("synthetic") is True,
        block.get("preflight_pass") is True,
        int(handoff_schema),
    )



def _attestation_document_problems(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """既有凭证文档的结构 / 安全字段 / 算术自洽核验（任一被改写 → tamper，fail-closed）。"""
    found: list[tuple[str, str]] = []
    for field, expected in (
        ("kind", ATTESTATION_KIND),
        ("report", ATTESTATION_REPORT_NAME),
        ("schema_version", ATTESTATION_SCHEMA_VERSION),
        ("contract_version", ATTESTATION_CONTRACT_VERSION),
        ("execution_mode", ATTESTATION_EXECUTION_MODE),
        ("attestation_scope", ATTESTATION_SCOPE),
        ("blocker_code", PHASE3_3_BLOCKER_CODE),
    ):
        actual = payload.get(field)
        if actual != expected or type(actual) is not type(expected):
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"凭证字段 {field} 不是 {expected!r}")
    for field, expected_bool in (
        ("blocker_active", True),
        ("human_gate_required", True),
        ("data_qualification_passed", False),
        ("phase_transition_allowed", False),
        ("l3_l4_auto_advance_allowed", False),
        ("evidence_qualified", False),
        ("advance_allowed", False),
        ("gate_blocked", True),
        ("all_required_verified_is_qualification", False),
    ):
        actual = payload.get(field)
        if actual is not expected_bool or not isinstance(actual, bool):
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"安全字段 {field} 必须恒为 {expected_bool}（硬编码，不允许被改写）",
            )
    if not _is_hex64(payload.get("attestation_id")):
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "attestation_id 必须是 64 位小写十六进制")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "revision 必须是 >= 1 的整数")
    supersedes = payload.get("supersedes")
    if supersedes is not None and not _is_hex64(supersedes):
        _add(found, AttestationCode.ATTESTATION_TAMPERED, "supersedes 必须是摘要或 null")
    for field in ("generated_at", "attested_at"):
        if _as_moment(payload.get(field)) is None:
            _add(found, AttestationCode.ATTESTATION_TAMPERED, f"{field} 必须是带时区的 ISO8601")
    synthetic, preflight_pass, handoff_schema = _verify_package_block(
        payload.get("package"), found=found
    )
    if handoff_schema > INTAKE_HANDOFF_SCHEMA_VERSION:
        _add(
            found,
            AttestationCode.ATTESTATION_TAMPERED,
            "凭证绑定的 handoff schema 版本高于本工具支持版本",
        )
    verified_count = _verify_materials_block(payload.get("materials"), found=found)
    block = payload.get("materials")
    materials = block if isinstance(block, list) else []
    required_count = len(materials)
    rejected = sum(
        1
        for item in materials
        if isinstance(item, Mapping) and item.get("decision") == VerificationDecision.REJECTED.value
    )
    needs_changes = sum(
        1
        for item in materials
        if isinstance(item, Mapping)
        and item.get("decision") == VerificationDecision.NEEDS_CHANGES.value
    )
    missing = sum(
        1 for item in materials if isinstance(item, Mapping) and item.get("decision") is None
    )
    for field, expected_count in (
        ("required_material_count", required_count),
        ("verified_material_count", verified_count),
        ("rejected_material_count", rejected),
        ("needs_changes_material_count", needs_changes),
        ("missing_decision_material_count", missing),
    ):
        actual = payload.get(field)
        if actual != expected_count or isinstance(actual, bool):
            _add(
                found,
                AttestationCode.ATTESTATION_TAMPERED,
                f"计数字段 {field} 与材料块不一致（应为 {expected_count}）",
            )
    expected_all = (
        (not synthetic)
        and preflight_pass
        and required_count > 0
        and verified_count == required_count
    )
    declared_all = payload.get("all_required_verified")
    if declared_all is not expected_all or not isinstance(declared_all, bool):
        _add(
            found,
            AttestationCode.ATTESTATION_TAMPERED,
            "all_required_verified 与材料块 / package 绑定不一致（绝不静默继承旧结论）",
        )
    forbidden = _forbidden_key_paths(payload)
    if forbidden:
        _add(
            found,
            AttestationCode.EVIDENCE_TIME_SUBSTITUTION,
            "凭证不允许出现证据时间字段：" + "、".join(forbidden[:8]),
        )
    return found


def load_attestation_document(path: Path | str) -> dict[str, Any]:
    """读取既有凭证并做**逐项**结构核验（被改写 → ``ATTESTATION_TAMPERED``，fail-closed）。"""
    target = Path(path)
    payload = _read_json_object(
        target,
        missing_code=AttestationCode.ATTESTATION_NOT_FOUND.value,
        unreadable_code=AttestationCode.ATTESTATION_UNREADABLE.value,
        what="材料级人工核验凭证",
    )
    problems = _attestation_document_problems(payload)
    if problems:
        raise AttestationStateError(
            _format_violations(problems, what="凭证文档完整性核验")
        )
    return payload


def _attestation_id_from_document(payload: Mapping[str, Any]) -> str:
    """从凭证文档**重新推导** ``attestation_id``（防伪核验：与声明值必须一致）。"""
    revision = payload.get("revision")
    package_block = payload.get("package")
    materials = payload.get("materials")
    supersedes = payload.get("supersedes")
    return compute_attestation_id(
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else -1,
        supersedes=supersedes if isinstance(supersedes, str) else None,
        package_block=package_block if isinstance(package_block, Mapping) else {},
        material_payloads=(
            [item for item in materials if isinstance(item, Mapping)]
            if isinstance(materials, list)
            else []
        ),
    )



# ---------------------------------------------------------------------------
# 只读加载 / 执行 / 原子写（默认零写入；唯一写开关是显式 out_path）
# ---------------------------------------------------------------------------
def _load_handoff(inbox_dir: str | Path, *, moment: datetime) -> IntakeHandoffDocument:
    """**只读**预检显式本地候选目录（零网络 / 零数据库 / 零写入；缺目录 fail-closed）。"""
    try:
        return load_intake_handoff(inbox_dir, as_of=moment)
    except (OSError, ValueError, InboxError) as exc:
        raise AttestationPathError(
            f"候选目录不可用（fail-closed）：{type(exc).__name__}: {_safe(str(exc))}"
        ) from exc


def _same_path(left: Path, right: Path) -> bool:
    """两个路径是否指向同一处（Windows 大小写不敏感；不做任何真实 IO 探测）。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - 极端路径解析失败时退化为字符串比较
        return str(left) == str(right)


def _recheck_existing_attestation(out: Path, attestation: HumanVerificationAttestation) -> None:
    """既有凭证**绝不**被静默覆盖：同 id 幂等；否则必须显式 revision + supersedes。"""
    if not out.exists():
        return
    existing = load_attestation_document(out)
    existing_id = str(existing["attestation_id"]).lower()
    if _attestation_id_from_document(existing) != existing_id:
        raise AttestationStateError(
            f"{AttestationCode.ATTESTATION_TAMPERED.value}：既有凭证 {_safe_path(out)} 的 "
            "attestation_id 无法重新推导（文档被改写），fail-closed、零写入"
        )
    if existing_id == attestation.attestation_id:
        # 同一 package 绑定 + 同一人工核验：幂等重复凭证（内容同一），允许原地重写
        return
    if attestation.supersedes is None:
        raise AttestationStateError(
            f"{AttestationCode.ATTESTATION_CONFLICT.value}：既有凭证（{existing_id[:16]}…）仍在，"
            "覆盖必须**显式**给出 --supersedes <attestation_id> 与更大的 --revision"
            "（绝不静默改写历史）"
        )
    if attestation.supersedes != existing_id:
        raise AttestationStateError(
            f"{AttestationCode.SUPERSEDES_MISMATCH.value}：--supersedes"
            f"（{attestation.supersedes[:16]}…）不是当前凭证的 attestation_id"
            f"（{existing_id[:16]}…）"
        )
    existing_revision = existing["revision"]
    if (
        isinstance(existing_revision, int)
        and not isinstance(existing_revision, bool)
        and attestation.revision <= existing_revision
    ):
        raise AttestationStateError(
            f"{AttestationCode.REVISION_INVALID.value}：revision（{attestation.revision}）必须"
            f"**大于**既有凭证的 revision（{existing_revision}）"
        )


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """原子落盘 JSON（同目录临时文件 + ``fsync`` + ``os.replace``；无残留 ``.tmp``）。"""
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise AttestationWriteError(
            f"{what} 写入失败（{type(exc).__name__}）：fail-closed；请检查输出目录可写性与"
            f"磁盘空间：{_safe_path(path)}"
        ) from exc


def run_attestation(
    inbox_dir: str | Path,
    *,
    verification_path: Path | str,
    moment: datetime,
    package: str | None = None,
    revision: object = 1,
    supersedes: object = None,
    out_path: Path | str | None = None,
) -> HumanVerificationAttestation:
    """执行**一次**材料级人工核验凭证（默认只读；只有显式 ``out_path`` 才写凭证文件）。

    安全语义：

    - 候选目录只读（复用 GOLD-027 ``load_intake_handoff``），人工核验输入只读；
    - 凭证**硬绑定**当前 package fingerprint + handoff / manifest 内容身份 + scope，
      漂移 / 篡改 / 非法人工输入一律 :class:`AttestationStateError`（**零写入**）；
    - 只有 ``out_path`` 才写**凭证本身**：先做既有凭证冲突检查（同 id 幂等；否则必须显式
      ``supersedes`` + 更大 ``revision``），再**原子**落盘；拒绝把凭证写进 ``inbox_dir``
      （否则会改变候选集）或覆盖核验输入文件；
    - 本函数**没有**任何 intake / commit / 数据库 / 网络调用，**绝不**修改 ``PROJECT_STATE``、
      **绝不**解除 blocker、**绝不**切换 Phase。

    Raises:
        AttestationArgumentError: 参数不合法（含时点缺时区、revision / supersedes 非法）。
        AttestationPathError: 候选目录 / 输出路径不可用（含写进 inbox 或核验输入的拒绝）。
        AttestationNotAttestableError: 对不可核验包 / 材料声明 VERIFIED（**预期 BLOCKED**）。
        AttestationStateError: 漂移 / 篡改 / 冲突 / 非法人工输入（fail-closed）。
        AttestationWriteError: 凭证原子写失败（fail-closed）。
    """
    moment = _require_aware(moment, field_name="moment").astimezone(UTC)
    cleaned_revision, cleaned_supersedes = _clean_revision(revision, supersedes)
    handoff = _load_handoff(inbox_dir, moment=moment)
    verification = load_verification_input(verification_path, moment=moment)
    selected = _select_package(handoff, package)
    attestation = build_attestation(
        selected,
        handoff,
        verification,
        moment=moment,
        revision=cleaned_revision,
        supersedes=cleaned_supersedes,
    )
    if out_path is None:
        return attestation
    out = Path(out_path)
    if _same_path(out, Path(verification_path)):
        raise AttestationPathError(
            f"{AttestationCode.ATTESTATION_OVERWRITE_REFUSED.value}：``--out`` 不得指向核验输入"
            f"文件本身（会破坏人工核验证据）：{_safe_path(out)}"
        )
    try:
        ensure_outside_inbox(Path(inbox_dir), out)
    except InboxError as exc:
        raise AttestationPathError(
            f"{AttestationCode.ATTESTATION_OVERWRITE_REFUSED.value}：``--out`` 不得写进候选目录"
            f"（会改变候选集并造成自我漂移）：{_safe(str(exc))}"
        ) from exc
    if out.is_dir():
        raise AttestationPathError(f"输出路径是目录、不是凭证文件：{_safe_path(out)}")
    _recheck_existing_attestation(out, attestation)
    _atomic_write_json(out, attestation.to_dict(), what="材料级人工核验凭证")
    return replace(attestation, written_path=_safe_path(out))



# ---------------------------------------------------------------------------
# 防伪核验（凭证文档 ↔ **当前**候选目录；纯只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AttestationVerification:
    """一份凭证与**当前**候选包的一致性核验结论（只读审计，**零写入**）。"""

    attestation_path: str
    inbox_dir: str
    attestation_id: str
    recomputed_attestation_id: str
    attestation_id_matches: bool
    package_fingerprint: str
    current_package_fingerprint: str | None
    package_fingerprint_matches: bool
    package_content_sha256: str
    current_package_content_sha256: str | None
    package_content_matches: bool
    handoff_content_sha256: str
    current_handoff_content_sha256: str
    handoff_content_matches: bool
    preflight_pass_matches: bool
    scope_matches: bool
    all_required_verified: bool
    codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """是否**全部**一致（任一原因码都表示 fail-closed：不得当作有效凭证）。"""
        return not self.codes

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（**全部脱敏**；安全字段恒为诚实取值）。"""
        return {
            "kind": ATTESTATION_VERIFICATION_KIND,
            "report": ATTESTATION_REPORT_NAME,
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "attestation_path": _safe_path(self.attestation_path),
            "inbox_dir": _safe_path(self.inbox_dir),
            "attestation_id": self.attestation_id,
            "recomputed_attestation_id": self.recomputed_attestation_id,
            "attestation_id_matches": self.attestation_id_matches,
            "package_fingerprint": self.package_fingerprint,
            "current_package_fingerprint": self.current_package_fingerprint,
            "package_fingerprint_matches": self.package_fingerprint_matches,
            "package_content_sha256": self.package_content_sha256,
            "current_package_content_sha256": self.current_package_content_sha256,
            "package_content_matches": self.package_content_matches,
            "handoff_content_sha256": self.handoff_content_sha256,
            "current_handoff_content_sha256": self.current_handoff_content_sha256,
            "handoff_content_matches": self.handoff_content_matches,
            "preflight_pass_matches": self.preflight_pass_matches,
            "scope_matches": self.scope_matches,
            "all_required_verified": self.all_required_verified,
            "all_required_verified_meaning": ALL_REQUIRED_VERIFIED_SEMANTICS,
            "verified": self.verified,
            "codes": list(self.codes),
            "details": list(self.details),
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "l3_l4_auto_advance_allowed": False,
            "evidence_qualified": False,
            "advance_allowed": False,
            "gate_blocked": True,
        }



def verify_attestation(
    attestation_path: Path | str, inbox_dir: str | Path, *, moment: datetime
) -> AttestationVerification:
    """把一份凭证与**当前**候选目录逐项比对（**纯只读**；漂移 → 稳定原因码）。

    核验内容：①凭证文档自身未被改写（重新推导 ``attestation_id`` 与声明值一致、安全字段未被
    削弱、计数 / 合取自洽、无证据时间键）；②凭证绑定的 package fingerprint / package 内容身份 /
    handoff 内容身份仍与**当前**候选目录一致；③``preflight_pass`` / ``scope`` 未变；
    ④每个已计为核验的材料仍带独立引用且当前仍 ``HUMAN_VERIFICATION_REQUIRED``。

    Raises:
        AttestationPathError: 凭证不存在，或候选目录不可用。
        AttestationStateError: 凭证被改写 / 完整性核验不通过（fail-closed，零写入）。
    """
    moment = _require_aware(moment, field_name="moment").astimezone(UTC)
    payload = load_attestation_document(attestation_path)
    current_handoff = _load_handoff(inbox_dir, moment=moment)
    declared_id = str(payload["attestation_id"]).lower()
    recomputed_id = _attestation_id_from_document(payload)
    package_block = payload["package"]
    declared_fingerprint = str(package_block["fingerprint"]).lower()
    declared_package_hash = str(package_block["package_content_sha256"])
    declared_handoff_hash = str(package_block["handoff_content_sha256"])
    current_handoff_hash = handoff_content_sha256(current_handoff)
    matches = [
        item for item in current_handoff.packages if item.fingerprint == declared_fingerprint
    ]
    codes: list[str] = []
    details: list[str] = []
    if declared_id != recomputed_id:
        codes.append(AttestationCode.ATTESTATION_ID_MISMATCH.value)
        details.append("凭证文档被改写：重新推导的 attestation_id 与声明值不一致")
    if not matches:
        codes.append(AttestationCode.PACKAGE_FINGERPRINT_DRIFT.value)
        details.append("当前候选目录内已不存在该 fingerprint 的候选包（package 漂移：旧凭证失效）")
    current_package_hash: str | None = None
    current_scope: str | None = None
    current_preflight: bool | None = None
    if matches:
        package = matches[0]
        current_package_hash = package_content_sha256(package)
        current_scope = package.scope
        current_preflight = package.preflight_pass
        if current_package_hash != declared_package_hash:
            codes.append(AttestationCode.PACKAGE_CONTENT_DRIFT.value)
            details.append("候选包材料结构已变化（manifest / 材料事实内容漂移：旧凭证失效）")
        if current_scope != str(package_block["scope"]):
            codes.append(AttestationCode.SCOPE_MISMATCH.value)
            details.append("候选包 scope 与凭证绑定不一致")
        if current_preflight is not bool(package_block["preflight_pass"]):
            codes.append(AttestationCode.PREFLIGHT_DRIFT.value)
            details.append("候选包 GOLD-027 预检结论与凭证绑定不一致")
        verified_keys = {
            str(item.get("material"))
            for item in payload["materials"]
            if item.get("counts_as_verified") is True
        }
        for item in package.materials:
            if (
                item.key in verified_keys
                and item.status is not IntakeStatus.HUMAN_VERIFICATION_REQUIRED
            ):
                codes.append(AttestationCode.MATERIAL_NOT_VERIFIABLE.value)
                details.append(f"材料 `{item.key}` 当前已不是 HUMAN_VERIFICATION_REQUIRED")
    if current_handoff_hash != declared_handoff_hash:
        codes.append(AttestationCode.HANDOFF_CONTENT_DRIFT.value)
        details.append("intake handoff / manifest 内容身份已变化（content 漂移：旧凭证失效）")
    for item in payload["materials"]:
        reference = item.get("evidence_reference")
        if item.get("decision") is not None and not str(reference or "").strip():
            codes.append(AttestationCode.MATERIAL_REFERENCE_MISSING.value)
            details.append(f"材料 `{item.get('material')}` 缺独立 evidence reference")
    return AttestationVerification(
        attestation_path=_safe_path(attestation_path),
        inbox_dir=_safe_path(inbox_dir),
        attestation_id=declared_id,
        recomputed_attestation_id=recomputed_id,
        attestation_id_matches=declared_id == recomputed_id,
        package_fingerprint=declared_fingerprint,
        current_package_fingerprint=(matches[0].fingerprint if matches else None),
        package_fingerprint_matches=bool(matches),
        package_content_sha256=declared_package_hash,
        current_package_content_sha256=current_package_hash,
        package_content_matches=(
            current_package_hash is not None and current_package_hash == declared_package_hash
        ),
        handoff_content_sha256=declared_handoff_hash,
        current_handoff_content_sha256=current_handoff_hash,
        handoff_content_matches=current_handoff_hash == declared_handoff_hash,
        preflight_pass_matches=(
            bool(matches) and (current_preflight is bool(package_block["preflight_pass"]))
        ),
        scope_matches=current_scope == str(package_block["scope"]),
        all_required_verified=bool(payload["all_required_verified"]),
        codes=tuple(dict.fromkeys(codes)),
        details=tuple(details),
    )



def render_attestation_summary(attestation: HumanVerificationAttestation) -> str:
    """渲染人类可读的凭证摘要（脱敏；**不解除** blocker、**不推进** Phase）。"""
    lines: list[str] = [
        "# Phase 3.3 材料级人工核验凭证（Human Verification Attestation）",
        "",
        f"> {ATTESTATION_NOTE}",
        "",
        f"- attestation_id（内容级）：`{attestation.attestation_id}`；"
        f"revision：{attestation.revision}；supersedes：`{attestation.supersedes or '—'}`",
        f"- 审计时点（**不是**证据时间）：{attestation.attested_at.isoformat()}；"
        f"执行模式：`{attestation.execution_mode}`；作用域：`{attestation.attestation_scope}`",
        f"- 候选包：`{attestation.package_dir}`（scope={attestation.scope}；"
        f"inbox_status={attestation.inbox_status}）",
        f"- 绑定摘要：fingerprint=`{attestation.package_fingerprint[:16]}…`；"
        f"package_content=`{attestation.package_content_sha256[:16]}…`；"
        f"handoff_content=`{attestation.handoff_content_sha256[:16]}…`",
        f"- 包可核验：synthetic={str(attestation.synthetic).lower()}；"
        f"preflight_pass={str(attestation.preflight_pass).lower()}",
        f"- **all_required_verified={str(attestation.all_required_verified).lower()}**"
        "（**只**代表材料级人工核验完成）",
        f"- 材料数：必需 {attestation.required_material_count}；已核验 "
        f"{attestation.verified_material_count}；REJECTED {attestation.rejected_material_count}；"
        f"NEEDS_CHANGES {attestation.needs_changes_material_count}；缺决策 "
        f"{attestation.missing_decision_material_count}",
        f"- evidence_qualified={str(attestation.evidence_qualified).lower()}；"
        f"data_qualification_passed={str(attestation.data_qualification_passed).lower()}；"
        f"phase_transition_allowed={str(attestation.phase_transition_allowed).lower()}；"
        f"l3_l4_auto_advance_allowed={str(attestation.l3_l4_auto_advance_allowed).lower()}；"
        f"advance_allowed={str(attestation.advance_allowed).lower()}",
        f"- blocker `{attestation.blocker_code}`：blocker_active="
        f"{str(attestation.blocker_active).lower()}；human_gate_required="
        f"{str(attestation.human_gate_required).lower()}；gate_blocked="
        f"{str(attestation.gate_blocked).lower()}",
        f"- {ALL_REQUIRED_VERIFIED_SEMANTICS}",
        "",
        "## 1. 材料级人工核验明细",
        "",
        "| 材料 | handoff 状态 | 决策 | 计入核验 | reason_code | reviewer | reviewed_at | "
        "独立引用 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in attestation.materials:
        lines.append(
            f"| {item.material} | {item.intake_status} | {item.decision or '—'} | "
            f"{str(item.counts_as_verified).lower()} | {item.reason_code or '—'} | "
            f"{item.reviewer or '—'} | "
            f"{item.reviewed_at.isoformat() if item.reviewed_at else '—'} | "
            f"{item.evidence_reference or '—'} | {item.note or '—'} |"
        )
    if attestation.codes:
        lines += ["", "## 2. 稳定原因码（任何一条都表示 fail-closed）", ""]
        lines.extend(f"- `{code}`" for code in attestation.codes)
    lines += ["", "## 3. 口径与边界", ""]
    lines.extend(f"- {note}" for note in attestation.notes)
    lines.append("")
    return "\n".join(lines)

