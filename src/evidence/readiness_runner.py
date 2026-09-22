"""Phase 3.3 Evidence Readiness 单次本地 tick runner（GOLD-010，纯本地 / 零网络 / 零数据库写入）。

GOLD-009 已给出**确定性、脱敏、幂等**的 readiness 状态变化事件，但 operator 仍须反复
手工执行 CLI 才能"盯"到变化。本模块补齐**纯本地周期运行工程能力**：把一次 tick 做成
可由外部定时器（Windows Task Scheduler / 现有本地 orchestrator / 其它外部定时器）
安全调用的**单次**命令。

设计要点（与 `.clinerules` / `.ai/DEVELOPMENT_PROTOCOL.md` 一致）：

- **不自带常驻循环**：本模块只执行**一次** tick，调度完全交给外部定时器
  （不新增任何第三方 scheduler 依赖，也不自动修改用户的系统计划任务）；
- **单实例锁**：同一工作目录内的 tick 用 OS 级独占文件锁（``msvcrt`` / ``fcntl``，
  零第三方依赖）串行化；锁文件内写入**可审计的 owner / pid / 获取时间**且不含任何
  敏感信息；正常退出释放锁（文件保留为审计留痕），锁冲突一律 **fail-closed**，
  **绝不删除 / 绝不改写**仍活动的锁，避免并发写 state；
- **只写显式配置的本地工作目录**：三类 artifact = snapshot state
  （``readiness_state.json``）/ 事件日志（``readiness_events.jsonl``）/ 简洁 status
  （``readiness_status.json``），全部使用既有**原子写**原语
  （同目录临时文件 + ``fsync`` + ``os.replace``）；**不写数据库、不联网**；
- **口径完全复用 GOLD-009**：指纹、事件类型、缺口与阈值口径全部来自
  :mod:`src.evidence.readiness_watch` / :mod:`src.evidence.handoff`
  （阈值同源于 ``src.alpha.evidence_gate``），**不复制、不降低任何阈值算法**；
- **幂等**：无 readiness 变化时**不追加**重复事件；有变化时只持久化 GOLD-009 定义的
  脱敏事件；事件日志按**有界保留**（保留最新 N 条，确定性）滚动，
  且**本次 tick 的新事件永不被丢弃**；
- **写入顺序事件优先**：先追加事件日志 → 再原子替换 state → 最后写 status。
  这样即使进程在写入途中被强杀，最坏只会"多一条待确认事件"，
  而不会出现"状态已更新、事件永久丢失"（那会让 operator 永远看不到变化）；
- **fail-closed**：state / 事件日志损坏、锁冲突、资格计算失败、artifact 写入失败时
  返回**稳定**的非零退出码（见 :func:`exit_code_for`），**保留旧的有效 state**，
  绝不把异常当成"首次快照"，也绝不自动重置资格状态；
- **诚实**：status 恒为 ``blocker_active=true`` / ``human_gate_required=true`` /
  ``data_qualification_passed=false`` / ``phase_transition_allowed=false``
  （**硬编码**，不被上游或被篡改的 state 透传影响）；即使
  ``ready_for_human_review=true``，Phase 切换仍须 ``.ai/DEVELOPMENT_PROTOCOL.md``
  的 **L3 人工确认**。

入口：``scripts/evidence_readiness_runner.py``（单次 tick；唯一写入口是显式 ``--work-dir``）。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from sqlalchemy.orm import Session, sessionmaker

from src.common.redaction import safe_text
from src.evidence.contracts import EvidenceScope
from src.evidence.handoff import build_handoff_report
from src.evidence.ledger import load_evidence_ledger
from src.evidence.readiness_watch import (
    MAX_CODE_CHARS,
    WATCH_EVENT_KIND,
    WATCH_SCHEMA_VERSION,
    ReadinessSnapshot,
    SnapshotStateError,
    WatchEvent,
    atomic_write_text,
    build_snapshot,
    detect_changes,
    load_snapshot_state,
    render_watch_summary,
    write_snapshot_state,
)
from src.monitoring.evidence_readiness import build_readiness_report

__all__ = [
    "DEFAULT_JOURNAL_LIMIT",
    "EVENTS_FILE_NAME",
    "EXIT_BLOCKED",
    "EXIT_CONFIG_ERROR",
    "EXIT_LOCK_CONFLICT",
    "EXIT_OK",
    "EXIT_QUALIFICATION_FAILED",
    "EXIT_STATE_INVALID",
    "EXIT_WORKDIR_UNUSABLE",
    "LOCK_FILE_NAME",
    "LOCK_KIND",
    "RUNNER_NOTE",
    "RUNNER_REPORT_NAME",
    "RUNNER_SCHEMA_VERSION",
    "STATE_FILE_NAME",
    "STATUS_FILE_NAME",
    "STATUS_KIND",
    "ArtifactWriteError",
    "LockConflictError",
    "LockInfo",
    "LockUnavailableError",
    "QualificationError",
    "RunnerError",
    "SingleInstanceLock",
    "SnapshotBuilder",
    "TickPaths",
    "TickReport",
    "TickStateError",
    "WorkDirError",
    "build_snapshot_from_session",
    "exit_code_for",
    "load_event_journal",
    "render_tick_summary",
    "run_tick",
    "write_event_journal",
    "write_status",
]

#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试与 README
RUNNER_SCHEMA_VERSION: Final[int] = 1
#: 报告标识（稳定，供上游 / 定时器日志解析）
RUNNER_REPORT_NAME: Final[str] = "evidence_readiness_tick"
#: status 文档标识
STATUS_KIND: Final[str] = "evidence_readiness_tick_status"
#: 单实例锁文件标识
LOCK_KIND: Final[str] = "evidence_readiness_tick_lock"
#: 工作目录内的固定文件布局（不猜路径、不散落到目录之外）
STATE_FILE_NAME: Final[str] = "readiness_state.json"
EVENTS_FILE_NAME: Final[str] = "readiness_events.jsonl"
STATUS_FILE_NAME: Final[str] = "readiness_status.json"
LOCK_FILE_NAME: Final[str] = "readiness_tick.lock"
#: 事件日志有界保留上限（保留**最新** N 条；确定性滚动）
DEFAULT_JOURNAL_LIMIT: Final[int] = 200
#: 读取锁文件 owner 记录的最大字符数（防止异常内容撑爆内存）
LOCK_READ_LIMIT: Final[int] = 4096

#: 退出码（机器可读；稳定，供定时器判断"预期 BLOCKED"还是"真实故障"）
EXIT_OK: Final[int] = 0
#: 参数 / 配置错误（例如未显式给出 --work-dir）
EXIT_CONFIG_ERROR: Final[int] = 2
#: 工作目录不可用 / artifact 写入失败（fail-closed）
EXIT_WORKDIR_UNUSABLE: Final[int] = 3
#: state 或事件日志损坏（fail-closed，保留旧 state，零写入）
EXIT_STATE_INVALID: Final[int] = 4
#: 仍未达标：**预期状态**（不是定时器故障），诚实保持 BLOCKED
EXIT_BLOCKED: Final[int] = 5
#: 锁冲突：另一个 tick 正在持有活动锁（fail-closed，零写入）
EXIT_LOCK_CONFLICT: Final[int] = 6
#: 资格计算失败（fail-closed，保留旧 state，零写入）
EXIT_QUALIFICATION_FAILED: Final[int] = 7

#: 固定说明：runner 只做单次本地 tick，不解除 blocker、不切 Phase
RUNNER_NOTE: Final[str] = (
    "本 runner 只执行**单次**本地 tick（调度交给 Windows Task Scheduler 或现有本地 "
    "orchestrator 等外部定时器）：不自带常驻循环、不联网、不写数据库、不接邮件 / 短信 / "
    "Webhook / 第三方推送，也不自动修改 OS 计划任务。它**不解除** `PHASE3_3_DATA`："
    "`blocker_active` / `human_gate_required` 恒为 true，`data_qualification_passed` / "
    "`phase_transition_allowed` 恒为 false；即使 `ready_for_human_review=true`，"
    "Phase 切换仍须 `.ai/DEVELOPMENT_PROTOCOL.md` 的 L3 人工确认。"
)

#: 事件日志中**必须恒定**的安全字段（加载时校验：被篡改即 fail-closed）
_JOURNAL_SAFETY_FIELDS: Final[tuple[tuple[str, bool], ...]] = (
    ("blocker_active", True),
    ("human_gate_required", True),
    ("data_qualification_passed", False),
    ("phase_transition_allowed", False),
)


class RunnerError(RuntimeError):
    """tick runner 失败（**fail-closed**：调用方按退出码处理，且不得假设已写入 artifact）。"""


class WorkDirError(RunnerError):
    """工作目录不可用 / 不可写（无法安全维护 state、事件日志与 status）。"""


class LockUnavailableError(RunnerError):
    """锁文件无法创建或加锁（权限 / IO 异常）。"""


class LockConflictError(RunnerError):
    """锁冲突：另一个 tick 正持有**活动**锁（不删除、不改写该锁，零写入）。"""


class TickStateError(RunnerError):
    """本地 state 或事件日志损坏 / 被篡改（**fail-closed**，保留旧 state，零写入）。"""


class QualificationError(RunnerError):
    """资格计算失败（**fail-closed**，保留旧 state，零写入）。"""


class ArtifactWriteError(RunnerError):
    """artifact（state / 事件日志 / status）原子写失败（**fail-closed**）。"""


#: 退出码映射表（顺序即优先级）→ 稳定退出码
_EXIT_CODES: Final[tuple[tuple[type[RunnerError], int], ...]] = (
    (TickStateError, EXIT_STATE_INVALID),
    (LockConflictError, EXIT_LOCK_CONFLICT),
    (WorkDirError, EXIT_WORKDIR_UNUSABLE),
    (ArtifactWriteError, EXIT_WORKDIR_UNUSABLE),
    (LockUnavailableError, EXIT_WORKDIR_UNUSABLE),
    (QualificationError, EXIT_QUALIFICATION_FAILED),
)


def exit_code_for(error: RunnerError) -> int:
    """把 :class:`RunnerError` 映射为**稳定**的进程退出码（未知类型按 fail-closed 处理）。"""
    for kind, code in _EXIT_CODES:
        if isinstance(error, kind):
            return code
    return EXIT_STATE_INVALID


@dataclass(frozen=True, slots=True)
class TickPaths:
    """一次 tick 的本地文件布局（全部位于**显式配置**的工作目录内）。"""

    root: Path
    state: Path
    events: Path
    status: Path
    lock: Path

    @classmethod
    def in_work_dir(cls, work_dir: Path) -> TickPaths:
        """由工作目录派生固定文件名（不猜测、不散落到目录之外）。"""
        root = Path(work_dir)
        return cls(
            root=root,
            state=root / STATE_FILE_NAME,
            events=root / EVENTS_FILE_NAME,
            status=root / STATUS_FILE_NAME,
            lock=root / LOCK_FILE_NAME,
        )


@dataclass(frozen=True, slots=True)
class LockInfo:
    """单实例锁的**可审计** owner 信息（只有 owner / pid / 时间，绝无敏感数据）。"""

    owner: str
    pid: int
    acquired_at: datetime
    recovered_stale: bool = False
    previous_owner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """锁文件内容（稳定 JSON；``previous_owner`` 为接管陈旧锁的审计留痕）。"""
        return {
            "kind": LOCK_KIND,
            "schema_version": RUNNER_SCHEMA_VERSION,
            "runner": RUNNER_REPORT_NAME,
            "owner": _safe(self.owner),
            "pid": self.pid,
            "acquired_at": self.acquired_at.isoformat(),
            "recovered_stale": self.recovered_stale,
            "previous_owner": None if self.previous_owner is None else _safe(self.previous_owner),
        }


def _safe(value: object) -> str:
    """统一脱敏 + 截断（锁 owner / 路径 / 事件日志文本共用）。"""
    return safe_text(str(value), max_chars=MAX_CODE_CHARS)


def _utc_now() -> datetime:
    """当前 UTC 时刻（tz-aware；锁获取时间默认时钟）。"""
    return datetime.now(UTC)


def _owner_token() -> str:
    """生成锁 owner 标识（随机 token；不含用户名 / 机器名等可识别信息）。"""
    return f"tick-{os.urandom(8).hex()}"


def _lock_module() -> Any:
    """按平台返回 OS 文件锁模块（Windows ``msvcrt`` / POSIX ``fcntl``；运行时导入）。

    说明：两个模块**互不存在于对方平台**，因此按运行时平台动态导入并显式标注为 ``Any``，
    既避免"在非本平台导入失败"，也避免类型检查器只在单一平台上通过。
    """
    return importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")


def _try_exclusive(handle: TextIO) -> bool:
    """尝试取 OS 级**非阻塞**独占锁；失败即冲突（另一个 tick 正在持有）。"""
    module = _lock_module()
    handle.seek(0)
    try:
        if os.name == "nt":  # pragma: no cover - 平台分支（本机 CI 为 Windows 分支）
            module.locking(handle.fileno(), module.LK_NBLCK, 1)
        else:  # pragma: no cover - POSIX 分支（语义等价：独占 + 非阻塞）
            module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
    except OSError:
        return False
    return True


def _release_exclusive(handle: TextIO) -> None:
    """释放 OS 级独占锁（best-effort；句柄关闭时 OS 亦会释放）。"""
    module = _lock_module()
    handle.seek(0)
    if os.name == "nt":  # pragma: no cover - 平台分支
        module.locking(handle.fileno(), module.LK_UNLCK, 1)
        return
    module.flock(handle.fileno(), module.LOCK_UN)


def _read_lock_record(handle: TextIO) -> str | None:
    """读取锁文件内的 owner 记录（**必须持有锁**才能读：Windows 锁区间禁止他进程读）。

    Returns:
        ``None``（文件为空）或已脱敏的可审计描述；内容不可解析时返回脱敏原文，
        **绝不因此崩溃**（损坏的锁文件按陈旧锁安全接管）。
    """
    handle.seek(0)
    raw = handle.read(LOCK_READ_LIMIT)
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _safe(f"<不可解析> {raw.strip()}")
    if not isinstance(payload, dict):
        return _safe(f"<不可解析> {raw.strip()}")
    parts = [f"{key}={payload[key]}" for key in ("owner", "pid", "acquired_at") if key in payload]
    return _safe(";".join(parts)) if parts else None


def _write_lock_record(handle: TextIO, info: LockInfo) -> None:
    """在**已持有**的锁文件内写入 owner 记录（truncate + 写入 + ``fsync``）。"""
    handle.seek(0)
    handle.truncate(0)
    handle.write(json.dumps(info.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


class SingleInstanceLock:
    """基于 OS 级独占文件锁的单实例门（防止 tick 并发重入写同一工作目录）。

    语义（与任务要求一一对应）：

    - **可审计**：锁文件内记录 ``owner`` / ``pid`` / ``acquired_at``；接管陈旧锁时额外
      记录 ``recovered_stale=true`` 与 ``previous_owner``，**不含任何敏感数据**；
    - **绝不删除**：任何路径都不会 unlink 锁文件 —— OS 锁才是"是否活动"的唯一权威，
      因此**不可能**因为"强删活动锁"而造成并发写 state；文件保留为审计留痕；
    - **陈旧锁安全恢复**：崩溃进程留下的锁文件（无活动 OS 锁）可被安全接管；
      内容不可解析同样按陈旧锁接管（只留痕，不崩溃）；
    - **活动锁冲突**：另一个仍持有 OS 锁的 tick → :class:`LockConflictError`，
      调用方必须 fail-closed（零写入）。
    """

    def __init__(
        self,
        path: Path,
        *,
        owner: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._owner = owner if owner else _owner_token()
        self._clock = clock if clock is not None else _utc_now
        self._handle: TextIO | None = None
        self._info: LockInfo | None = None

    @property
    def path(self) -> Path:
        """锁文件路径。"""
        return self._path

    @property
    def info(self) -> LockInfo | None:
        """已持有的锁信息；未持有为 ``None``。"""
        return self._info

    @property
    def held(self) -> bool:
        """当前是否持有锁。"""
        return self._handle is not None

    def acquire(self) -> LockInfo:
        """获取锁（幂等：已持有则直接返回）。

        Raises:
            LockUnavailableError: 锁文件无法创建 / 打开。
            LockConflictError: 另一个 tick 正持有活动锁（**不删除、不改写**）。
        """
        if self._handle is not None and self._info is not None:
            return self._info
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LockUnavailableError(f"锁目录不可创建：{_safe(self._path.parent)}") from exc
        try:
            # 句柄必须跨方法存活（锁需在整个 tick 期间持有），故在此显式管理生命周期
            handle = open(self._path, "a+", encoding="utf-8", newline="\n")  # noqa: SIM115
        except OSError as exc:
            raise LockUnavailableError(f"锁文件不可打开：{_safe(self._path)}") from exc
        if not _try_exclusive(handle):
            with contextlib.suppress(OSError):
                handle.close()
            raise LockConflictError(
                f"锁冲突：另一个 tick 正持有活动锁（未删除、未改动任何文件）：{_safe(self._path)}"
            )
        try:
            previous = _read_lock_record(handle)
            info = LockInfo(
                owner=self._owner,
                pid=os.getpid(),
                acquired_at=self._clock(),
                recovered_stale=previous is not None,
                previous_owner=previous,
            )
            _write_lock_record(handle, info)
        except BaseException:
            with contextlib.suppress(OSError):
                _release_exclusive(handle)
            with contextlib.suppress(OSError):
                handle.close()
            raise
        self._handle = handle
        self._info = info
        return info

    def owner_record(self) -> str | None:
        """读取锁文件当前的 owner 记录（**需持有锁**；未持有时返回 ``None``）。"""
        if self._handle is None:
            return None
        return _read_lock_record(self._handle)

    def release(self) -> None:
        """释放锁（幂等；best-effort，**绝不删除**锁文件，异常不外抛）。"""
        handle, self._handle, self._info = self._handle, None, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            _release_exclusive(handle)
        with contextlib.suppress(OSError):
            handle.close()

    def __enter__(self) -> LockInfo:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def load_event_journal(path: Path) -> tuple[dict[str, Any], ...]:
    """读取事件日志（JSONL；文件不存在 = 空日志）。

    Raises:
        TickStateError: 不可读 / 某行不是 JSON 对象 / 某行安全字段被篡改
            （**fail-closed**：绝不静默丢弃或重写既有事件留痕）。
    """
    if not path.exists():
        return ()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TickStateError(f"事件日志不可读：{_safe(path)}") from exc
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TickStateError(f"事件日志第 {number} 行不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise TickStateError(f"事件日志第 {number} 行必须是 JSON 对象")
        for name, expected in _JOURNAL_SAFETY_FIELDS:
            if payload.get(name) is not expected:
                raise TickStateError(
                    f"事件日志第 {number} 行安全字段 {name} 值非法：拒绝加载（fail-closed）"
                )
        entries.append(payload)
    return tuple(entries)


def write_event_journal(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    """**原子**写事件日志（每行一个 GOLD-009 脱敏事件；``sort_keys`` 保证确定性）。"""
    text = "".join(
        json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n" for entry in entries
    )
    atomic_write_text(path, text)


def write_status(path: Path, report: TickReport) -> None:
    """**原子**写 status（简洁、机器可读；诚实字段硬编码）。"""
    text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


@dataclass(frozen=True, slots=True)
class TickReport:
    """一次 tick 的**稳定**结果（status 文档与人类可读摘要的唯一事实来源）。"""

    generated_at: datetime
    work_dir: Path
    lock: LockInfo
    snapshot: ReadinessSnapshot
    events: tuple[WatchEvent, ...]
    journal_entries: int
    journal_added: int
    journal_dropped: int
    journal_limit: int
    previous: ReadinessSnapshot | None = None

    @property
    def changed(self) -> bool:
        """本次 tick 是否产生了有意义变化（0 事件 = 幂等无重复告警）。"""
        return bool(self.events)

    @property
    def ready_for_human_review(self) -> bool:
        """量化门槛是否达标（**仍须** L3 人工 Gate）。"""
        return self.snapshot.ready_for_human_review

    @property
    def previous_fingerprint(self) -> str | None:
        """前次快照指纹（首次运行为 ``None``）。"""
        return None if self.previous is None else self.previous.fingerprint

    def to_dict(self) -> dict[str, Any]:
        """稳定 status 文档（诚实字段**硬编码**：绝不被上游或被篡改的 state 透传影响）。"""
        return {
            "kind": STATUS_KIND,
            "report": RUNNER_REPORT_NAME,
            "schema_version": RUNNER_SCHEMA_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "work_dir": _safe(self.work_dir),
            "blocker_code": self.snapshot.blocker_code,
            "blocker_active": True,
            "human_gate_required": True,
            "data_qualification_passed": False,
            "phase_transition_allowed": False,
            "ready_for_human_review": self.snapshot.ready_for_human_review,
            "status": self.snapshot.status,
            "fingerprint": self.snapshot.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
            "changed": self.changed,
            "event_count": len(self.events),
            "events": [event.to_dict() for event in self.events],
            "gaps": {
                item.scope: {
                    "eligible": item.eligible,
                    "required": item.required,
                    "remaining": item.remaining,
                }
                for item in self.snapshot.scopes
            },
            "lock": self.lock.to_dict(),
            "journal": {
                "entries": self.journal_entries,
                "added": self.journal_added,
                "dropped": self.journal_dropped,
                "limit": self.journal_limit,
                "file": EVENTS_FILE_NAME,
            },
            "tick_completed": True,
            "notes": [RUNNER_NOTE],
        }


#: 快照构建器：给定审计时刻 → readiness 快照（真实 tick 用 :func:`build_snapshot_from_session`）
SnapshotBuilder = Callable[[datetime], ReadinessSnapshot]


def _ensure_work_dir(root: Path) -> None:
    """确保工作目录存在且是目录（否则 fail-closed）。"""
    if root.exists() and not root.is_dir():
        raise WorkDirError(f"工作目录不是目录：{_safe(root)}")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkDirError(f"工作目录不可创建：{_safe(root)}") from exc


def _load_previous_state(path: Path) -> ReadinessSnapshot | None:
    """读取上次快照（文件不存在 = 首次运行）；损坏即 fail-closed（**绝不**当作首次快照）。"""
    try:
        return load_snapshot_state(path)
    except SnapshotStateError as exc:
        raise TickStateError(
            f"snapshot state 不可用（fail-closed，未写入任何 artifact）：{_safe(exc)}"
        ) from exc


def _call_builder(builder: SnapshotBuilder, moment: datetime) -> ReadinessSnapshot:
    """调用快照构建器（异常 → fail-closed；**异常正文不落盘 / 不打印**，避免泄露连接串）。"""
    try:
        snapshot = builder(moment)
    except Exception as exc:  # 资格计算失败必须 fail-closed 且脱敏
        raise QualificationError(
            f"资格计算失败（{type(exc).__name__}）：fail-closed，未写入任何 artifact；"
            "异常正文不落盘 / 不打印，避免泄露数据库连接串或凭据"
        ) from exc
    if not isinstance(snapshot, ReadinessSnapshot):
        raise QualificationError("资格计算返回类型非法：fail-closed，未写入任何 artifact")
    return snapshot


def run_tick(
    work_dir: Path,
    *,
    moment: datetime,
    snapshot_builder: SnapshotBuilder,
    journal_limit: int = DEFAULT_JOURNAL_LIMIT,
    lock_owner: str | None = None,
) -> TickReport:
    """执行**一次** tick（纯本地；不联网、不写数据库、不自带循环）。

    写入顺序：**先追加事件日志 → 再原子替换 snapshot state → 最后写 status**。
    即使进程在写入途中被强杀，最坏只会"多一条待确认事件"，
    而不会出现"状态已更新、事件永久丢失"（后者会让 operator 永远看不到变化）。

    Args:
        work_dir: **显式配置**的本地工作目录（唯一写入口；内部 artifact 使用固定文件名）。
        moment: 本次审计时刻（tz-aware；同时作为快照 ``generated_at`` 与事件 ``detected_at``）。
        snapshot_builder: 快照构建器（真实运行用 :func:`build_snapshot_from_session`）。
        journal_limit: 事件日志有界保留上限（保留**最新** N 条；本次新事件永不被丢弃）。
        lock_owner: 可选的锁 owner 标识（仅审计用途；默认随机 token）。

    Returns:
        :class:`TickReport`（含 status 文档、本次事件与锁信息）。

    Raises:
        WorkDirError: 工作目录不可用；或 artifact 写入失败。
        LockUnavailableError: 锁文件无法创建 / 加锁。
        LockConflictError: 另一个 tick 正持有活动锁（**零写入**）。
        TickStateError: state 或事件日志损坏（**保留旧 state**，零写入）。
        QualificationError: 资格计算失败（**保留旧 state**，零写入）。
    """
    paths = TickPaths.in_work_dir(work_dir)
    _ensure_work_dir(paths.root)
    limit = max(1, int(journal_limit))
    with SingleInstanceLock(paths.lock, owner=lock_owner) as lock_info:
        previous = _load_previous_state(paths.state)
        entries = load_event_journal(paths.events)
        current = _call_builder(snapshot_builder, moment)
        events = detect_changes(previous, current, detected_at=moment)
        new_entries: tuple[dict[str, Any], ...] = tuple(
            {"kind": WATCH_EVENT_KIND, "schema_version": WATCH_SCHEMA_VERSION, **event.to_dict()}
            for event in events
        )
        # 无有意义变化且日志已存在 → 不追加重复事件（幂等），也不重写既有日志
        write_journal = bool(new_entries) or not paths.events.exists()
        effective_limit = max(limit, len(new_entries)) if write_journal else limit
        retained = (*entries, *new_entries)[-effective_limit:] if write_journal else entries
        dropped = len(entries) + len(new_entries) - len(retained) if write_journal else 0
        report = TickReport(
            generated_at=moment,
            work_dir=paths.root,
            lock=lock_info,
            snapshot=current,
            events=events,
            journal_entries=len(retained),
            journal_added=len(new_entries),
            journal_dropped=dropped,
            journal_limit=effective_limit,
            previous=previous,
        )
        try:
            if write_journal:
                write_event_journal(paths.events, retained)
            write_snapshot_state(paths.state, current)
            write_status(paths.status, report)
        except OSError as exc:
            raise ArtifactWriteError(
                f"artifact 写入失败（{type(exc).__name__}）：fail-closed；事件日志优先写入、"
                f"旧 state 保留；请检查工作目录可写性与磁盘空间：{_safe(paths.root)}"
            ) from exc
    return report


def build_snapshot_from_session(
    factory: sessionmaker[Session],
    *,
    moment: datetime,
    scopes: Sequence[EvidenceScope] = (EvidenceScope.AUTHOR, EvidenceScope.NEWS),
) -> ReadinessSnapshot:
    """从数据库**只读**计算一次 readiness 快照（真实 tick 的默认 builder）。

    链路与既有 CLI **同源**：``load_evidence_ledger`` → ``build_readiness_report`` →
    ``build_handoff_report`` → ``build_snapshot``；阈值 / 指纹 / 事件口径全部复用，
    **绝不复制第二套算法**。会话显式 ``rollback`` 并由上下文管理器关闭，**零数据库写入**。
    """
    with factory() as session:
        try:
            ledger = load_evidence_ledger(session)
        finally:
            session.rollback()
    readiness = build_readiness_report(ledger, as_of=moment, scopes=tuple(scopes))
    handoff = build_handoff_report(readiness)
    return build_snapshot(handoff, generated_at=moment)


def render_tick_summary(report: TickReport) -> str:
    """渲染人类可读的 tick 摘要（脱敏；**不解除** blocker、不切换 Phase）。"""
    info = report.lock
    return "\n".join(
        [
            render_watch_summary(report.snapshot, report.events, previous=report.previous),
            "",
            "## 6. Tick 元信息（runner）",
            "",
            f"- 工作目录：`{_safe(report.work_dir)}`（唯一写入口；state / 事件日志 / "
            "status 均写在此目录）",
            f"- 单实例锁：owner=`{_safe(info.owner)}`；pid={info.pid}；"
            f"acquired_at={info.acquired_at.isoformat()}；"
            f"接管陈旧锁={str(info.recovered_stale).lower()}",
            f"- 事件日志：本次新增 {report.journal_added} 条；保留 {report.journal_entries} 条"
            f"（上限 {report.journal_limit}）；滚动丢弃 {report.journal_dropped} 条",
            f"- 本次 tick：changed={str(report.changed).lower()}；"
            f"event_count={len(report.events)}；data_qualification_passed=false；"
            "phase_transition_allowed=false",
            "",
            f"- {RUNNER_NOTE}",
            "",
        ]
    )
