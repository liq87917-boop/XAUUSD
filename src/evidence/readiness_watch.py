"""Phase 3.3 Evidence Readiness 状态变更通知（GOLD-009，只读 / 默认 dry-run）。

GOLD-008 的人工交接包解决了"把当前缺口交给 operator"的问题，但仍遗留一个缺口：
**状态发生变化时没有确定性、可幂等的通知**，operator 只能反复盯盘 / 轮询。

本模块补齐该缺口，但**只做本地、脱敏、可幂等投递的状态变更事件**：

- **复用唯一资格口径**：只消费
  :class:`src.evidence.handoff.EvidenceHandoffReport`（其阈值同源于
  ``src.alpha.evidence_gate``），**绝不新造第二套阈值算法**，也不修改任何阈值；
- **确定性指纹 / 快照**：只基于**脱敏白名单字段**（``status`` /
  ``ready_for_human_review`` / 各 scope 的 ``eligible`` / ``required`` / ``remaining`` /
  ``coverage`` 缺口 / ``source_share_evaluable`` / 稳定原因码）计算 SHA-256 指纹，
  输出全部确定性排序（scope 名与原因码均排序），**不含时间戳**（时间戳变化不算状态变化）；
- **有意义变化检测**：首次快照、BLOCKED 缺口变化、原因码集合变化、
  ``ready_for_human_review`` false→true / true→false 才产生事件；
  **完全相同状态重复运行产生 0 个事件**（幂等）；
- **只写本地文件型安全出口**：不写数据库、不新增 migration/schema、不联网、
  不接邮件 / 短信 / Webhook / 第三方推送；快照与事件都走**原子写**
  （同目录临时文件 + ``os.replace``），避免半写状态；
- **诚实**：``blocker_active`` / ``human_gate_required`` 恒为 ``True``，
  ``data_qualification_passed`` / ``phase_transition_allowed`` 恒为 ``False``；
  ``ready_for_human_review=true`` 只是"可以进入人工复核"，
  Phase 切换仍须 ``.ai/DEVELOPMENT_PROTOCOL.md`` 的 **L3 人工确认**；
- **脱敏**：所有字符串字段（含原因码 / 事件消息）统一经
  :func:`src.common.redaction.safe_text`；绝不输出正文、token / API key /
  Authorization 或完整 source config。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from src.common.redaction import safe_text
from src.evidence.handoff import EvidenceHandoffReport, ScopeGap
from src.monitoring.phase33_qualification import CheckStatus

__all__ = [
    "EVENT_ORDER",
    "SCOPE_STATE_FIELDS",
    "SNAPSHOT_KIND",
    "SNAPSHOT_SCHEMA_VERSION",
    "WATCH_EVENT_KIND",
    "WATCH_NOTE",
    "WATCH_REPORT_NAME",
    "WATCH_SCHEMA_VERSION",
    "ReadinessSnapshot",
    "ScopeChange",
    "ScopeSnapshot",
    "SnapshotStateError",
    "WatchEvent",
    "WatchEventType",
    "build_snapshot",
    "detect_changes",
    "load_snapshot_state",
    "render_watch_summary",
    "write_events",
    "write_snapshot_state",
]

#: 快照文档标识（稳定，供上游解析；与事件文档区分）
SNAPSHOT_KIND: Final[str] = "evidence_readiness_snapshot"
#: 事件文档标识
WATCH_EVENT_KIND: Final[str] = "evidence_readiness_watch_events"
#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
WATCH_SCHEMA_VERSION: Final[int] = 1
#: 与快照共用的 schema 版本（两者同步演进）
SNAPSHOT_SCHEMA_VERSION: Final[int] = WATCH_SCHEMA_VERSION
#: 报告标识（稳定，供上游解析）
WATCH_REPORT_NAME: Final[str] = "evidence_readiness_watch"
#: 单个原因码 / 字符串字段的最大长度（脱敏后截断，防止异常内容撑爆事件）
MAX_CODE_CHARS: Final[int] = 120
#: 事件消息最大长度（比原因码宽松，保证人类可读整句不被截断）
MAX_MESSAGE_CHARS: Final[int] = 400
#: 参与指纹与差分的 scope 白名单字段（顺序即差分顺序，稳定）
SCOPE_STATE_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "eligible",
    "required",
    "remaining",
    "remaining_checks",
    "coverage_applicable",
    "coverage_days",
    "coverage_required",
    "coverage_remaining",
    "source_share_applicable",
    "source_share_evaluable",
    "source_share_remaining",
)
#: 固定说明：通知层不解除 blocker、不切 Phase
WATCH_NOTE: Final[str] = (
    "本工具只把 Evidence readiness 状态变化转换为**本地、脱敏、幂等**的通知事件，"
    "用于减少 operator 盯盘 / 轮询：不联网、不写数据库、不接第三方推送，"
    "**不解除** `PHASE3_3_DATA`。即使 `ready_for_human_review=true`，"
    "Phase 切换仍须 `.ai/DEVELOPMENT_PROTOCOL.md` 的 L3 人工确认。"
)
class SnapshotStateError(RuntimeError):
    """快照 state 文件不可用（缺失之外的任何异常都**安全失败**，绝不静默当作首次快照）。"""


class WatchEventType(StrEnum):
    """有意义变化的固定事件类型（顺序即输出顺序）。"""

    FIRST_SNAPSHOT = "FIRST_SNAPSHOT"
    READY_FOR_HUMAN_REVIEW_ENABLED = "READY_FOR_HUMAN_REVIEW_ENABLED"
    READY_FOR_HUMAN_REVIEW_REVOKED = "READY_FOR_HUMAN_REVIEW_REVOKED"
    BLOCKER_GAP_CHANGED = "BLOCKER_GAP_CHANGED"
    REASON_CODES_CHANGED = "REASON_CODES_CHANGED"


#: 事件输出顺序（稳定；测试与 CLI 共同引用）
EVENT_ORDER: Final[tuple[WatchEventType, ...]] = (
    WatchEventType.FIRST_SNAPSHOT,
    WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED,
    WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED,
    WatchEventType.BLOCKER_GAP_CHANGED,
    WatchEventType.REASON_CODES_CHANGED,
)


def _safe(value: object) -> str:
    """字符串字段统一脱敏 + 截断（原因码 / 状态 / 消息共用）。"""
    return safe_text(str(value), max_chars=MAX_CODE_CHARS)


def _canonical_json(payload: Any) -> str:
    """确定性 JSON 文本（排序键 + 紧凑分隔符），指纹计算共用。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint_of(state: dict[str, Any]) -> str:
    """对**状态**（不含时间戳）计算 SHA-256 指纹。"""
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()
@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    """单个 scope 的脱敏白名单状态（可量化缺口视图，无正文 / 无来源配置）。"""

    scope: str
    status: str
    eligible: int
    required: int
    remaining: int
    remaining_checks: int
    coverage_applicable: bool
    coverage_days: int
    coverage_required: int
    coverage_remaining: int
    source_share_applicable: bool
    source_share_evaluable: bool
    source_share_remaining: float

    def state(self) -> dict[str, Any]:
        """可序列化状态（参与指纹；字段顺序由 :data:`SCOPE_STATE_FIELDS` 决定）。"""
        return {
            "scope": self.scope,
            "status": self.status,
            "eligible": self.eligible,
            "required": self.required,
            "remaining": self.remaining,
            "remaining_checks": self.remaining_checks,
            "coverage_applicable": self.coverage_applicable,
            "coverage_days": self.coverage_days,
            "coverage_required": self.coverage_required,
            "coverage_remaining": self.coverage_remaining,
            "source_share_applicable": self.source_share_applicable,
            "source_share_evaluable": self.source_share_evaluable,
            "source_share_remaining": self.source_share_remaining,
        }
@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    """Evidence readiness 的**确定性状态快照**（脱敏白名单；时间戳不参与指纹）。"""

    kind: str
    schema_version: int
    contract_version: str
    generated_at: datetime
    blocker_code: str
    blocker_active: bool
    human_gate_required: bool
    status: str
    ready_for_human_review: bool
    total_remaining_gap_count: int
    scopes: tuple[ScopeSnapshot, ...]
    reason_codes: tuple[str, ...]
    fingerprint: str

    def scope(self, name: str) -> ScopeSnapshot:
        """按 scope 名取快照；缺失即显式失败（防止口径漂移被静默忽略）。"""
        for item in self.scopes:
            if item.scope == name:
                return item
        raise KeyError(name)

    def state(self) -> dict[str, Any]:
        """状态视图（**不含** ``generated_at`` / ``fingerprint``）：指纹与差分的事实来源。"""
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "human_gate_required": self.human_gate_required,
            "status": self.status,
            "ready_for_human_review": self.ready_for_human_review,
            "total_remaining_gap_count": self.total_remaining_gap_count,
            "scopes": {item.scope: item.state() for item in self.scopes},
            "reason_codes": list(self.reason_codes),
        }

    def to_dict(self) -> dict[str, Any]:
        """可落盘字典（状态 + 审计用时间戳与指纹）。"""
        payload = self.state()
        payload["generated_at"] = self.generated_at.isoformat()
        payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReadinessSnapshot:
        """从落盘字典恢复快照（严格校验；任何异常都**安全失败**）。

        Raises:
            SnapshotStateError: 文档类型 / schema 版本 / 结构 / 指纹校验任一失败，
                或 state 声称 blocker 已解除（拒绝加载被篡改的状态）。
        """
        if payload.get("kind") != SNAPSHOT_KIND:
            raise SnapshotStateError("state 文件不是 evidence readiness 快照")
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotStateError(
                f"state schema_version 不受支持：{payload.get('schema_version')!r}"
            )
        if payload.get("blocker_active") is not True:
            raise SnapshotStateError(
                "state 声称 blocker 已解除：拒绝加载（blocker 只能由人工 Gate 解除）"
            )
        if payload.get("human_gate_required") is not True:
            raise SnapshotStateError("state 缺少 human_gate_required=true：拒绝加载")
        raw_scopes = payload.get("scopes")
        if not isinstance(raw_scopes, dict) or not raw_scopes:
            raise SnapshotStateError("state 缺少 scopes")
        scopes = tuple(_scope_from_state(raw_scopes[name], name) for name in sorted(raw_scopes))
        raw_codes = payload.get("reason_codes") or []
        if not isinstance(raw_codes, list):
            raise SnapshotStateError("state reason_codes 必须是数组")
        try:
            generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        except (KeyError, ValueError) as exc:
            raise SnapshotStateError("state generated_at 无法解析") from exc
        try:
            total_remaining = int(payload.get("total_remaining_gap_count") or 0)
        except (TypeError, ValueError) as exc:
            raise SnapshotStateError("state total_remaining_gap_count 非法") from exc
        snapshot = cls(
            kind=SNAPSHOT_KIND,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            contract_version=_safe(payload.get("contract_version") or ""),
            generated_at=generated_at,
            blocker_code=_safe(payload.get("blocker_code") or ""),
            blocker_active=True,
            human_gate_required=True,
            status=_safe(payload.get("status") or ""),
            ready_for_human_review=bool(payload.get("ready_for_human_review")),
            total_remaining_gap_count=total_remaining,
            scopes=scopes,
            reason_codes=tuple(sorted({_safe(code) for code in raw_codes})),
            fingerprint=_safe(payload.get("fingerprint") or ""),
        )
        if snapshot.fingerprint != _fingerprint_of(snapshot.state()):
            raise SnapshotStateError("state 指纹校验失败：文件可能被损坏或篡改")
        return snapshot
def _scope_from_state(item: Any, name: str) -> ScopeSnapshot:
    """从 state 字典恢复单个 scope 快照（字段级严格校验）。"""
    if not isinstance(item, dict):
        raise SnapshotStateError(f"state scope 结构非法：{name}")
    try:
        return ScopeSnapshot(
            scope=_safe(item["scope"]),
            status=_safe(item["status"]),
            eligible=int(item["eligible"]),
            required=int(item["required"]),
            remaining=int(item["remaining"]),
            remaining_checks=int(item["remaining_checks"]),
            coverage_applicable=bool(item["coverage_applicable"]),
            coverage_days=int(item["coverage_days"]),
            coverage_required=int(item["coverage_required"]),
            coverage_remaining=int(item["coverage_remaining"]),
            source_share_applicable=bool(item["source_share_applicable"]),
            source_share_evaluable=bool(item["source_share_evaluable"]),
            source_share_remaining=round(float(item["source_share_remaining"]), 6),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotStateError(f"state scope 字段非法：{name}") from exc


def _scope_snapshot(gap: ScopeGap) -> ScopeSnapshot:
    """把交接包的 scope 缺口重排为脱敏白名单快照（纯函数，零 I/O）。"""
    return ScopeSnapshot(
        scope=_safe(gap.scope),
        status=_safe(gap.status),
        eligible=int(gap.eligible),
        required=int(gap.required),
        remaining=int(gap.remaining),
        remaining_checks=int(gap.remaining_checks),
        coverage_applicable=bool(gap.coverage_applicable),
        coverage_days=int(gap.coverage_days),
        coverage_required=int(gap.coverage_required),
        coverage_remaining=int(gap.coverage_remaining),
        source_share_applicable=bool(gap.source_share_applicable),
        source_share_evaluable=bool(gap.source_share_evaluable),
        source_share_remaining=round(float(gap.source_share_remaining), 6),
    )


def _reason_codes(handoff: EvidenceHandoffReport) -> tuple[str, ...]:
    """稳定原因码集合 = 仍未达标的检查键 ∪ 候选批次隔离原因码（排序去重）。

    只取**代码型标识**（检查键 / 既有 ``ReasonCode`` 值），不取自由文本 reason，
    因此集合变化等价于"哪个资格项仍未达标"发生了变化。
    """
    codes: set[str] = set()
    for gap in (handoff.author, handoff.news):
        for check in gap.checks:
            if check.status is not CheckStatus.PASS:
                codes.add(_safe(check.key))
    for code, _count in handoff.quarantine_reason_counts:
        codes.add(_safe(code))
    return tuple(sorted(codes))


def build_snapshot(
    handoff: EvidenceHandoffReport,
    *,
    generated_at: datetime,
) -> ReadinessSnapshot:
    """把人工交接包转换为确定性状态快照（纯函数；不触库、不联网、不写文件）。

    Args:
        handoff: :func:`src.evidence.handoff.build_handoff_report` 的结果
            （阈值与缺口口径已由既有 Evidence Readiness 决定）。
        generated_at: 本次运行时刻（**不参与指纹**，仅用于审计）。

    Returns:
        :class:`ReadinessSnapshot`；``blocker_active`` / ``human_gate_required`` 恒为
        ``True``（即使上游被篡改也不会弱化）。
    """
    scopes = tuple(
        sorted(
            (_scope_snapshot(handoff.author), _scope_snapshot(handoff.news)),
            key=lambda item: item.scope,
        )
    )
    provisional = ReadinessSnapshot(
        kind=SNAPSHOT_KIND,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        contract_version=_safe(handoff.contract_version),
        generated_at=generated_at,
        blocker_code=_safe(handoff.blocker_code),
        blocker_active=True,
        human_gate_required=True,
        status=_safe(handoff.status),
        ready_for_human_review=bool(handoff.ready_for_human_review),
        total_remaining_gap_count=int(handoff.total_remaining_gap_count),
        scopes=scopes,
        reason_codes=_reason_codes(handoff),
        fingerprint="",
    )
    return ReadinessSnapshot(
        kind=provisional.kind,
        schema_version=provisional.schema_version,
        contract_version=provisional.contract_version,
        generated_at=provisional.generated_at,
        blocker_code=provisional.blocker_code,
        blocker_active=True,
        human_gate_required=True,
        status=provisional.status,
        ready_for_human_review=provisional.ready_for_human_review,
        total_remaining_gap_count=provisional.total_remaining_gap_count,
        scopes=provisional.scopes,
        reason_codes=provisional.reason_codes,
        fingerprint=_fingerprint_of(provisional.state()),
    )
@dataclass(frozen=True, slots=True)
class ScopeChange:
    """单个 scope 白名单字段的变化（前一值 → 当前值）。"""

    scope: str
    field: str
    previous: int | float | bool | str
    current: int | float | bool | str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "field": self.field,
            "previous": self.previous,
            "current": self.current,
        }

    def describe(self) -> str:
        """人类可读描述（确定性、无敏感内容）。"""
        return _safe(f"{self.scope}.{self.field}: {self.previous} → {self.current}")


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """一条**语义明确**的 readiness 状态变更通知（脱敏；不解除 blocker）。"""

    event: WatchEventType
    detected_at: datetime
    fingerprint: str
    previous_fingerprint: str | None
    blocker_code: str
    status: str
    previous_status: str | None
    ready_for_human_review: bool
    previous_ready_for_human_review: bool | None
    total_remaining_gap_count: int
    reason_codes: tuple[str, ...]
    added_reason_codes: tuple[str, ...]
    removed_reason_codes: tuple[str, ...]
    scope_changes: tuple[ScopeChange, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        """稳定 JSON（关键诚实字段**硬编码**：绝不从快照透传可被篡改的布尔值）。"""
        return {
            "report": WATCH_REPORT_NAME,
            "event": self.event.value,
            "detected_at": self.detected_at.isoformat(),
            "fingerprint": self.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
            "blocker_code": self.blocker_code,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "status": self.status,
            "previous_status": self.previous_status,
            "ready_for_human_review": self.ready_for_human_review,
            "previous_ready_for_human_review": self.previous_ready_for_human_review,
            "total_remaining_gap_count": self.total_remaining_gap_count,
            "reason_codes": list(self.reason_codes),
            "added_reason_codes": list(self.added_reason_codes),
            "removed_reason_codes": list(self.removed_reason_codes),
            "scope_changes": [change.to_dict() for change in self.scope_changes],
            "message": self.message,
        }
def _scope_changes(
    previous: ReadinessSnapshot, current: ReadinessSnapshot
) -> tuple[ScopeChange, ...]:
    """逐 scope 比较白名单字段（顺序稳定：scope 名 → 字段顺序）。"""
    changes: list[ScopeChange] = []
    names = sorted(
        {item.scope for item in previous.scopes} | {item.scope for item in current.scopes}
    )
    for name in names:
        try:
            before_state = previous.scope(name).state()
            after_state = current.scope(name).state()
        except KeyError:
            continue
        for field_name in SCOPE_STATE_FIELDS:
            if before_state[field_name] != after_state[field_name]:
                changes.append(
                    ScopeChange(
                        scope=name,
                        field=field_name,
                        previous=before_state[field_name],
                        current=after_state[field_name],
                    )
                )
    return tuple(changes)


def _code_delta(
    previous: ReadinessSnapshot, current: ReadinessSnapshot
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """原因码集合变化（新增 / 移除，均排序）。"""
    before = set(previous.reason_codes)
    after = set(current.reason_codes)
    return tuple(sorted(after - before)), tuple(sorted(before - after))


def _event_message(
    event: WatchEventType,
    *,
    current: ReadinessSnapshot,
    added: tuple[str, ...],
    removed: tuple[str, ...],
    scope_changes: tuple[ScopeChange, ...],
) -> str:
    """构造确定性、脱敏的事件消息（不含时间戳，便于测试断言稳定）。"""
    if event is WatchEventType.FIRST_SNAPSHOT:
        text = (
            f"首次 Evidence readiness 快照：status={current.status}；"
            f"ready_for_human_review={str(current.ready_for_human_review).lower()}；"
            f"仍未达标检查数={current.total_remaining_gap_count}；"
            f"原因码={list(current.reason_codes)}。blocker 仍 active，需人工 Gate。"
        )
    elif event is WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED:
        text = (
            "Evidence readiness 变化：ready_for_human_review false→true"
            "（量化门槛达标，可进入人工复核）。human_gate_required=true、"
            "phase_transition_allowed=false：Phase 切换仍须 L3 人工确认。"
        )
    elif event is WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED:
        text = (
            "Evidence readiness 变化：ready_for_human_review true→false"
            "（资格回落），blocker 继续生效，需人工复核原因。"
        )
    elif event is WatchEventType.BLOCKER_GAP_CHANGED:
        detail = "；".join(change.describe() for change in scope_changes)
        text = (
            f"Evidence readiness 缺口变化（status={current.status}）："
            f"仍未达标检查数={current.total_remaining_gap_count}；{detail}"
        )
    else:
        text = (
            "Evidence readiness 原因码集合变化："
            f"新增={list(added)}；移除={list(removed)}；"
            f"仍未达标检查数={current.total_remaining_gap_count}"
        )
    return safe_text(text, max_chars=MAX_MESSAGE_CHARS)
def detect_changes(
    previous: ReadinessSnapshot | None,
    current: ReadinessSnapshot,
    *,
    detected_at: datetime | None = None,
) -> tuple[WatchEvent, ...]:
    """检测**有意义**的 readiness 状态变化（纯函数；零 I/O）。

    触发条件（其余情况一律 0 事件，保证幂等）：

    - ``previous is None`` → 一个 :attr:`WatchEventType.FIRST_SNAPSHOT`；
    - 指纹相同（状态完全一致）→ **空元组**（重复运行不重复告警）；
    - BLOCKED 缺口变化 → :attr:`WatchEventType.BLOCKER_GAP_CHANGED`；
    - 原因码集合变化 → :attr:`WatchEventType.REASON_CODES_CHANGED`；
    - ``ready_for_human_review`` false→true / true→false →
      :attr:`WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED` /
      :attr:`WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED`。

    Returns:
        按 :data:`EVENT_ORDER` 排序的事件元组；每条事件都携带
        ``human_gate_required=true`` / ``phase_transition_allowed=false``。
    """
    moment = detected_at if detected_at is not None else current.generated_at
    if previous is None:
        return (
            _event(
                WatchEventType.FIRST_SNAPSHOT,
                current=current,
                previous=None,
                moment=moment,
                added=current.reason_codes,
                removed=(),
                scope_changes=(),
            ),
        )
    if previous.fingerprint == current.fingerprint:
        return ()
    scope_changes = _scope_changes(previous, current)
    added, removed = _code_delta(previous, current)
    events: list[WatchEvent] = []
    emit_ready_up = not previous.ready_for_human_review and current.ready_for_human_review
    emit_ready_down = previous.ready_for_human_review and not current.ready_for_human_review
    for event_type in EVENT_ORDER:
        should_emit = (
            (event_type is WatchEventType.READY_FOR_HUMAN_REVIEW_ENABLED and emit_ready_up)
            or (event_type is WatchEventType.READY_FOR_HUMAN_REVIEW_REVOKED and emit_ready_down)
            or (event_type is WatchEventType.BLOCKER_GAP_CHANGED and bool(scope_changes))
            or (event_type is WatchEventType.REASON_CODES_CHANGED and bool(added or removed))
        )
        if not should_emit:
            continue
        events.append(
            _event(
                event_type,
                current=current,
                previous=previous,
                moment=moment,
                added=added,
                removed=removed,
                scope_changes=scope_changes,
            )
        )
    return tuple(events)


def _event(
    event: WatchEventType,
    *,
    current: ReadinessSnapshot,
    previous: ReadinessSnapshot | None,
    moment: datetime,
    added: tuple[str, ...],
    removed: tuple[str, ...],
    scope_changes: tuple[ScopeChange, ...],
) -> WatchEvent:
    """构造一条事件（诚实字段硬编码；消息统一脱敏）。"""
    return WatchEvent(
        event=event,
        detected_at=moment,
        fingerprint=current.fingerprint,
        previous_fingerprint=previous.fingerprint if previous is not None else None,
        blocker_code=current.blocker_code,
        status=current.status,
        previous_status=previous.status if previous is not None else None,
        ready_for_human_review=current.ready_for_human_review,
        previous_ready_for_human_review=(
            previous.ready_for_human_review if previous is not None else None
        ),
        total_remaining_gap_count=current.total_remaining_gap_count,
        reason_codes=current.reason_codes,
        added_reason_codes=added,
        removed_reason_codes=removed,
        scope_changes=scope_changes,
        message=_event_message(
            event,
            current=current,
            added=added,
            removed=removed,
            scope_changes=scope_changes,
        ),
    )
def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本（同目录临时文件 + ``fsync`` + ``os.replace``），避免半写状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        assert tmp_name is not None  # noqa: S101 - 仅供 mypy 收窄类型
        os.replace(tmp_name, path)
    except BaseException:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        raise


def load_snapshot_state(path: Path) -> ReadinessSnapshot | None:
    """读取快照 state：文件不存在返回 ``None``（首次运行），其余异常**安全失败**。

    Raises:
        SnapshotStateError: 文件为空 / JSON 损坏 / 结构非法 / 指纹校验失败
            （绝不把损坏状态静默当作"首次快照"，否则会掩盖真实问题）。
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotStateError(f"state 文件不可读：{safe_text(str(exc))}") from exc
    if not raw.strip():
        raise SnapshotStateError("state 文件为空")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotStateError(f"state 文件不是合法 JSON：{safe_text(exc.msg)}") from exc
    if not isinstance(payload, dict):
        raise SnapshotStateError("state 文件顶层必须是 JSON 对象")
    return ReadinessSnapshot.from_dict(payload)


def write_snapshot_state(path: Path, snapshot: ReadinessSnapshot) -> None:
    """原子落盘快照 state（**仅在 CLI 显式给出 ``--out`` 时调用**）。"""
    text = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def write_events(
    path: Path, events: tuple[WatchEvent, ...], *, snapshot: ReadinessSnapshot
) -> None:
    """原子落盘事件文档（**仅在 CLI 显式给出 ``--events`` 时调用**；零事件也写空列表）。"""
    payload: dict[str, Any] = {
        "kind": WATCH_EVENT_KIND,
        "report": WATCH_REPORT_NAME,
        "schema_version": WATCH_SCHEMA_VERSION,
        "generated_at": snapshot.generated_at.isoformat(),
        "fingerprint": snapshot.fingerprint,
        "blocker_code": snapshot.blocker_code,
        "blocker_active": True,
        "human_gate_required": True,
        "data_qualification_passed": False,
        "phase_transition_allowed": False,
        "event_count": len(events),
        "events": [event.to_dict() for event in events],
        "notes": [WATCH_NOTE],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)
def render_watch_summary(
    snapshot: ReadinessSnapshot,
    events: tuple[WatchEvent, ...],
    *,
    previous: ReadinessSnapshot | None = None,
) -> str:
    """渲染人类可读的变更通知摘要（脱敏；**不解除** blocker、不切换 Phase）。"""
    previous_line = (
        f"前次指纹：`{previous.fingerprint}`"
        if previous is not None
        else "前次指纹：—（首次运行 / 未提供 --state）"
    )
    lines: list[str] = [
        "# Evidence Readiness 变更通知（Evidence Readiness Watch）",
        "",
        f"> blocker `{snapshot.blocker_code}`：active=true；human_gate_required=true；"
        "本通知层只减少盯盘 / 轮询，**不解除** blocker，也不切换 Phase。",
        "",
        "## 1. 状态",
        "",
        f"- status：`{snapshot.status}`",
        f"- ready_for_human_review：{str(snapshot.ready_for_human_review).lower()}",
        "- data_qualification_passed：false；phase_transition_allowed：false",
        f"- 仍未达标检查数：{snapshot.total_remaining_gap_count}",
        f"- 快照指纹：`{snapshot.fingerprint}`；{previous_line}",
        f"- 审计时点（UTC）：{snapshot.generated_at.isoformat()}",
        "",
        "## 2. 缺口快照（脱敏白名单）",
        "",
        "| scope | status | eligible | required | remaining | 未达标检查 | coverage 缺口 | "
        "source-share 可评估 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in snapshot.scopes:
        coverage = f"{item.coverage_remaining}" if item.coverage_applicable else "—"
        share = str(item.source_share_evaluable).lower() if item.source_share_applicable else "—"
        lines.append(
            f"| {item.scope} | {item.status} | {item.eligible} | {item.required} | "
            f"{item.remaining} | {item.remaining_checks} | {coverage} | {share} |"
        )
    lines += ["", "## 3. 原因码（稳定集合，排序）", ""]
    if snapshot.reason_codes:
        lines.extend(f"- `{code}`" for code in snapshot.reason_codes)
    else:
        lines.append("- —（当前无仍未达标检查键 / 隔离原因码）")
    lines += [
        "",
        "## 4. 变更事件",
        "",
        f"- 事件数：{len(events)}（0 表示状态与上次快照完全一致，幂等无重复告警）",
        "",
        "| 事件 | 状态 | ready_for_human_review | 说明 |",
        "|---|---|---|---|",
    ]
    if not events:
        lines.append("| — | — | — | 无有意义变化 |")
    for event in events:
        lines.append(
            f"| {event.event.value} | {event.status} | "
            f"{str(event.ready_for_human_review).lower()} | {event.message} |"
        )
    lines += ["", "## 5. 口径与边界", "", f"- {WATCH_NOTE}", ""]
    return "\n".join(lines)
