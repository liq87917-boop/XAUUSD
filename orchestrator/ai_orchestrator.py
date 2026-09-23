import atexit
import contextlib
import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ============================================================
# 基础目录
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

TASK_DIR = ROOT / ".ai" / "tasks"
RESULT_DIR = ROOT / ".ai" / "results"
RUNTIME_DIR = ROOT / ".ai" / "runtime"
LOG_DIR = ROOT / ".ai" / "logs"

# GPT 唯一拥有写权限的项目状态；本模块只**只读**呈现，绝不修改（GOLD-035）。
PROJECT_STATE_PATH = ROOT / ".ai" / "PROJECT_STATE.json"

TASK_STATE_DIR = RUNTIME_DIR / "tasks"
RECOVERY_DIR = RUNTIME_DIR / "recovery"

# 恢复状态机的 runtime 审计文件（只落 runtime，绝不进 Git、绝不写 result）。
RECOVERY_STATE_FILE = RECOVERY_DIR / "recovery_state.json"

# 陈旧 lock 原文的归档目录（审计留痕，绝不进 Git）。
RECOVERY_LOCK_ARCHIVE_DIR = RECOVERY_DIR / "locks"

LOCK_FILE = RUNTIME_DIR / "orchestrator.lock"
LOG_FILE = LOG_DIR / "orchestrator.log"

# ============================================================
# 启动前 bootstrap sync（GOLD-021）
# ============================================================
#
# launcher（start_agent.bat）必须在启动本进程之前先运行
# `python -m orchestrator.bootstrap_sync`，并把结果写到该文件。
# Orchestrator 启动时读取它，把「启动前同步结果 + 本进程看到的 HEAD」
# 一起打进日志，用于确认当前进程实际加载的版本。
BOOTSTRAP_STATE_FILE = RUNTIME_DIR / "bootstrap_state.json"


# ============================================================
# 配置
# ============================================================

POLL_SECONDS = int(
    os.getenv(
        "AI_POLL_SECONDS",
        "20"
    )
)

QUEUE_TARGET_SIZE = max(
    1,
    int(
        os.getenv(
            "AI_QUEUE_TARGET_SIZE",
            "3"
        )
    )
)

IDLE_LOG_SECONDS = max(
    POLL_SECONDS,
    int(
        os.getenv(
            "AI_IDLE_LOG_SECONDS",
            "1800"
        )
    )
)

BLOCKING_HUMAN_GATES = {
    "L3",
    "L4"
}

# Task 元数据允许出现的 human_gate 词表。
# 词表以外的任何值（含拼写错误 / 未知档位）一律 fail-closed 停线，
# 绝不静默降级为「无 Gate」。
SUPPORTED_HUMAN_GATES = {
    "L1",
    "L2",
    "L3",
    "L4"
}

# result 终态：可以安全跳过，且绝不重写历史 result。
TERMINAL_RESULT_STATUSES = {
    "completed",
    "blocked"
}

# 非终态 result：任务尚未收口，依赖它的任务必须继续等待。
UNRESOLVED_RESULT_STATUSES = {
    "pending",
    "running"
}

# 已知 result 状态全集。
# 集合以外的状态（例如损坏 result / 手写错误状态）视为 unknown，
# 一律 fail-closed 停线。
KNOWN_RESULT_STATUSES = (
    TERMINAL_RESULT_STATUSES
    | UNRESOLVED_RESULT_STATUSES
)

# ============================================================
# Orchestrator 判定语义（GOLD-020）
# ============================================================
#
# Cline 的 raw finish reason（例如 "aborted"）只代表 CLI 自己的收尾原因，
# 不能代表 Orchestrator 的最终判定。
# 所以新 result 必须把两者分开保存：
#   - cline_finish_reason_raw：CLI 原始值（审计用，可能是 aborted）；
#   - execution_outcome / normalized_finish_reason：Orchestrator 稳定判定。
# 绝不让「唯一 finish_reason」在一个已完成任务上显示 aborted。

EXECUTION_OUTCOME_COMPLETED = "completed"

EXECUTION_OUTCOME_BLOCKED = "blocked"

EXECUTION_OUTCOME_FAILED = "failed"

EXECUTION_OUTCOME_WAITING_EXTERNAL = "waiting_external"

NORMALIZED_FINISH_REASONS = {
    EXECUTION_OUTCOME_COMPLETED,
    EXECUTION_OUTCOME_BLOCKED,
    EXECUTION_OUTCOME_FAILED,
    EXECUTION_OUTCOME_WAITING_EXTERNAL
}

# ============================================================
# 单轮循环动作（GOLD-020）
# ============================================================

ITERATION_CONTINUE = "continue"

ITERATION_SLEEP = "sleep"

ITERATION_STOP = "stop"

DEFAULT_MAX_ATTEMPTS = int(
    os.getenv(
        "AI_MAX_ATTEMPTS",
        "3"
    )
)

CLINE_TIMEOUT_SECONDS = int(
    os.getenv(
        "AI_CLINE_TIMEOUT",
        "3600"
    )
)

CLINE_PROVIDER = (
    os.getenv(
        "AI_CLINE_PROVIDER",
        "deepseek"
    )
    .strip()
)

CLINE_MODEL = (
    os.getenv(
        "AI_CLINE_MODEL",
        ""
    )
    .strip()
)

REQUIRED_BRANCH = os.getenv(
    "AI_BRANCH",
    "cline-agent"
)

REMOTE_NAME = os.getenv(
    "AI_REMOTE",
    "origin"
)


# ============================================================
# 异常中断恢复状态机（GOLD-022）
# ============================================================
#
# 背景（GOLD-018 真实事故）：
#   进程被中断/重启后，工作区遗留一批**没有进入 Git**的修改。
#   旧实现对此只有两种反应：要么沉默 defer，要么直接 reset/clean。
#   前者会永久卡线，后者可能抹掉人工修改（也抹掉唯一的现场证据）。
#
# 新的确定性状态机（绝不交给 Cline / LLM 决定）：
#   1) 只有能**证明**「属于上一次同一 task 的中断现场」时才允许 snapshot + 清理；
#   2) 自动清理的前置条件是 snapshot 完整且**可恢复**（校验通过）；
#   3) 证据不足（人工改动 / 来源不明 / 多重证据）一律 fail-closed 停线；
#   4) 恢复动作只写 runtime 审计，绝不写 result、绝不把恢复当任务完成。

# attempt 启动时写入 runtime 的现场基线证据 schema。
ATTEMPT_EVIDENCE_SCHEMA = "gold-ai/interrupted-attempt/v1"

# recovery snapshot / 恢复审计文件 schema。
RECOVERY_SNAPSHOT_SCHEMA = "gold-ai/recovery-snapshot/v1"

RECOVERY_STATE_SCHEMA = "gold-ai/recovery-state/v1"

# 单实例 lock 的 schema（旧版 lock 无此字段，仍按其他证据判定）。
LOCK_SCHEMA = "gold-ai/orchestrator-lock/v1"

# 恢复审计保留的事件条数上限（有界，避免 runtime 无限增长）。
RECOVERY_EVENT_HISTORY_LIMIT = 20

# 工作区状态分类词表。
WORKTREE_CLEAN = "clean"

WORKTREE_INTERRUPTED_TASK = "interrupted_task"

WORKTREE_UNKNOWN = "unknown_dirty_worktree"

# 中断现场使用的 failure_class / failure_code
# （沿用 GOLD-018 事故手工恢复时写入的稳定词表，便于审计对齐）。
INTERRUPTED_FAILURE_CLASS = "interrupted_previous_process"

INTERRUPTED_FAILURE_CODE = "OLD_PROCESS_INTERRUPTED"

# 恢复动作结果词表。
RECOVERY_OUTCOME_CLEAN = "no_recovery_needed"

RECOVERY_OUTCOME_RECOVERED = "recovered"

RECOVERY_OUTCOME_MANUAL = "manual_intervention_required"

RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED = "snapshot_unverified"

RECOVERY_OUTCOME_SNAPSHOT_FAILED = "snapshot_failed"

RECOVERY_OUTCOME_CLEANUP_FAILED = "cleanup_failed"

# 恢复审计事件类型。
RECOVERY_EVENT_WORKTREE = "worktree_recovery"

RECOVERY_EVENT_LOCK = "stale_lock_recovery"

RECOVERY_EVENT_BLOCKED = "manual_intervention"

# lock 分类词表。
LOCK_ACTIVE = "active"

LOCK_STALE = "stale"

LOCK_UNKNOWN = "unknown"


# ============================================================
# 全局状态
# ============================================================

_logger = logging.getLogger(
    "ai_orchestrator"
)

_lock_owned = False

# 人工介入告警限流时间戳（只影响日志噪声，绝不降低检测频率）。
_last_worktree_block_log_at: float | None = None


# ============================================================
# 时间
# ============================================================

def now_iso():

    return (
        datetime
        .now()
        .astimezone()
        .isoformat(
            timespec="seconds"
        )
    )


# ============================================================
# 目录初始化
# ============================================================

def ensure_directories():

    TASK_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    RESULT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    RUNTIME_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    TASK_STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    RECOVERY_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    RECOVERY_LOCK_ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


# ============================================================
# 日志
# ============================================================

def setup_logging():

    if _logger.handlers:
        return

    _logger.setLevel(
        logging.INFO
    )

    _logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(
        sys.stdout
    )

    console.setFormatter(
        formatter
    )

    _logger.addHandler(
        console
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )

    file_handler.setFormatter(
        formatter
    )

    _logger.addHandler(
        file_handler
    )


# ============================================================
# JSON
# ============================================================

def atomic_write_json(
    path,
    data
):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_path,
        path
    )


def read_json(
    path,
    default=None
):

    if not path.exists():
        return default

    try:

        with open(
            path,
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as exc:

        _logger.error(
            "读取 JSON 失败: %s | %s",
            path,
            exc
        )

        return default


def sha256_file(
    path: Path
) -> str | None:
    """计算文件的 SHA-256（用于 snapshot 完整性/可恢复性校验）。

    文件不存在或不可读时返回 None（调用方必须 fail-closed）。
    """

    digest = hashlib.sha256()

    try:

        with open(
            path,
            "rb"
        ) as handle:

            for chunk in iter(
                lambda: handle.read(
                    1024 * 1024
                ),
                b""
            ):

                digest.update(
                    chunk
                )

    except OSError:

        return None

    return digest.hexdigest()


# ============================================================
# 通用命令
# ============================================================

def run_command(
    command,
    shell=True,
    timeout=None
):

    try:

        result = subprocess.run(
            command,
            cwd=ROOT,
            shell=shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )

        return {
            "returncode":
                result.returncode,

            "stdout":
                result.stdout,

            "stderr":
                result.stderr,

            "timed_out":
                False
        }

    except subprocess.TimeoutExpired as exc:

        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(
            stdout,
            bytes
        ):

            stdout = stdout.decode(
                "utf-8",
                errors="replace"
            )

        if isinstance(
            stderr,
            bytes
        ):

            stderr = stderr.decode(
                "utf-8",
                errors="replace"
            )

        return {

            "returncode":
                124,

            "stdout":
                stdout,

            "stderr":
                stderr
                + "\nCommand timed out.",

            "timed_out":
                True
        }


# ============================================================
# Git
# ============================================================

def git(
    command,
    timeout=120
):

    return run_command(
        f"git {command}",
        shell=True,
        timeout=timeout
    )


def current_branch():

    result = git(
        "branch --show-current"
    )

    if (
        result["returncode"]
        != 0
    ):

        return None

    return (
        result["stdout"]
        .strip()
    )


def get_git_status_lines():

    result = git(
        "status --porcelain"
    )

    if (
        result["returncode"]
        != 0
    ):

        raise RuntimeError(

            "git status 执行失败: "

            + (
                result["stderr"].strip()
                or
                result["stdout"].strip()
            )
        )

    return [

        line

        for line
        in result["stdout"].splitlines()

        if line.strip()
    ]


def git_is_dirty():

    return bool(
        get_git_status_lines()
    )


def head_full_sha() -> str | None:
    """当前 HEAD 的完整 SHA（用于中断现场的 HEAD 基线比对）。"""

    result = git(
        "rev-parse HEAD"
    )

    if (
        result["returncode"]
        != 0
    ):

        return None

    value = (
        result["stdout"]
        .strip()
    )

    return value or None


def parse_status_line(
    line: str
) -> tuple[str, str] | None:
    """解析 `git status --porcelain` 单行 → (XY 状态码, 路径)。

    无法解析时返回 None（调用方只把它当同类信息，不做任何清理决策）。
    """

    if len(line) < 4:

        return None

    code = line[:2]

    raw_path = line[3:].strip()

    if not raw_path:

        return None

    # rename / copy 形式：`XY OLD -> NEW`，只保留目标路径。
    if " -> " in raw_path:

        raw_path = (
            raw_path
            .split(" -> ")[-1]
            .strip()
        )

    if not raw_path:

        return None

    return code, raw_path


def split_dirty_paths(
    lines: list[str]
) -> tuple[list[str], list[str]]:
    """把 dirty 状态行拆成 (tracked 修改文件, untracked 文件)。"""

    tracked: list[str] = []
    untracked: list[str] = []

    for line in lines:

        parsed = parse_status_line(
            line
        )

        if parsed is None:

            continue

        code, path = parsed

        if code == "??":

            untracked.append(
                path
            )

        else:

            tracked.append(
                path
            )

    return tracked, untracked


# ============================================================
# Git Pull
# ============================================================

def pull_latest():

    _logger.info(
        "Pulling latest tasks..."
    )

    result = git(

        f"pull --rebase "
        f"{REMOTE_NAME} "
        f"{REQUIRED_BRANCH}",

        timeout=180
    )

    if (
        result["returncode"]
        != 0
    ):

        message = (
            result["stderr"].strip()
            or
            result["stdout"].strip()
        )

        _logger.error(

            "Git pull 失败，"
            "本轮不执行任务: %s",

            message
        )

        return False

    return True


# ============================================================
# Git Push
# ============================================================

def local_ahead_count():

    result = git(

        f"rev-list --count "
        f"{REMOTE_NAME}/"
        f"{REQUIRED_BRANCH}"
        f"..HEAD"
    )

    if (
        result["returncode"]
        != 0
    ):

        return None

    try:

        return int(
            result["stdout"]
            .strip()
            or
            "0"
        )

    except ValueError:

        return None


def push_pending_commits():

    ahead = local_ahead_count()

    if ahead == 0:

        return True

    if ahead is None:

        _logger.warning(
            "无法判断本地是否领先远端，"
            "将尝试 push。"
        )

    else:

        _logger.info(
            "检测到 %s 个待推送 commit。",
            ahead
        )

    result = git(

        f"push "
        f"{REMOTE_NAME} "
        f"{REQUIRED_BRANCH}",

        timeout=180
    )

    if (
        result["returncode"]
        != 0
    ):

        message = (
            result["stderr"].strip()
            or
            result["stdout"].strip()
        )

        _logger.error(

            "Git push 失败。"
            "代码和 commit 已保留在本地，"
            "下轮继续重试: %s",

            message
        )

        return False

    _logger.info(
        "Git push 成功。"
    )

    return True


# ============================================================
# Git 同步
# ============================================================

def sync_repository():

    if git_is_dirty():

        _logger.warning(
            "工作区存在未提交修改，"
            "跳过 Git 同步和任务执行。"
        )

        for line in get_git_status_lines():

            _logger.warning(
                "  %s",
                line
            )

        return False

    # 先 pull。
    #
    # 如果之前任务已经 commit，
    # 但 push 因网络失败，
    # pull --rebase 可以安全同步远端，
    # 然后下面继续 retry push。
    #
    # 注意：pull --rebase 绝不 force push，也绝不 reset 已完成 commit；
    # 任何失败都 fail-closed：本轮不执行任何任务，保留本地 commit/result。
    if not pull_latest():

        _logger.error(
            "Git sync 未完成（pull/rebase 失败）："
            "本轮不执行任务，"
            "本地 commit/result 保留，"
            "下一轮继续恢复。"
        )

        return False

    if not push_pending_commits():

        _logger.error(
            "Git sync 未完成（push pending）："
            "本轮不执行任务，"
            "本地 commit/result 保留，"
            "下一轮继续恢复。"
        )

        return False

    cleared = clear_push_pending_states()

    if cleared:

        _logger.info(
            "remote synced: push 成功，"
            "已清理 push_pending 状态: %s",
            ", ".join(cleared)
        )

    else:

        _logger.info(
            "remote synced: 远端与本地一致，无待推送 commit。"
        )

    return True


# ============================================================
# 版本可见性（GOLD-021）
# ============================================================
#
# 解决「磁盘代码已更新，但运行中的进程仍是旧代码」这类无法确认版本的问题：
#   - 启动时打印本进程实际看到的 HEAD 短 SHA；
#   - 打印 launcher 的 bootstrap sync 结果（branch / HEAD / provider / model）；
#   - 运行中若磁盘 HEAD 变化，明确告警「当前进程仍是启动时加载的代码」。

def head_short_sha():

    result = git(
        "rev-parse --short HEAD"
    )

    if (
        result["returncode"]
        != 0
    ):

        return None

    value = (
        result["stdout"]
        .strip()
    )

    return value or None


def abbrev_sha(value):

    if not isinstance(value, str):

        return "<unknown>"

    text = value.strip()

    return text[:8] or "<unknown>"


def commit_prefix_matches(
    expected,
    observed
):
    """比较完整 SHA 与短 SHA（任一为空 / 非字符串时返回 False）。"""

    if (
        not isinstance(expected, str)
        or
        not isinstance(observed, str)
    ):

        return False

    left = expected.strip().lower()

    right = observed.strip().lower()

    if not left or not right:

        return False

    length = min(
        len(left),
        len(right)
    )

    return (
        left[:length]
        ==
        right[:length]
    )


def read_bootstrap_sync_state():
    """读取 launcher 在启动本进程之前落盘的 bootstrap sync 结果。"""

    state = read_json(
        BOOTSTRAP_STATE_FILE
    )

    if isinstance(state, dict):

        return state

    return None


def describe_bootstrap_sync(state):
    """把 bootstrap sync 结果压缩成一行可审计日志（绝不含任何凭据）。"""

    if not isinstance(state, dict):

        return (
            "not recorded：launcher 未执行 / 未落盘 bootstrap sync，"
            "无法确认本进程启动前是否已同步"
        )

    status = (
        "OK"
        if state.get("ok") is True
        else "FAILED"
    )

    return (
        f"{status}"
        f" result={state.get('result') or 'unknown'}"
        f" branch={state.get('branch') or '<unknown>'}"
        f" head_before={abbrev_sha(state.get('head_before'))}"
        f" head_after={abbrev_sha(state.get('head_after'))}"
        f" provider={state.get('provider') or '<unknown>'}"
        f" model={state.get('model') or '<provider default>'}"
        f" checked_at={state.get('checked_at') or '<unknown>'}"
    )


def bootstrap_head_mismatch(
    state,
    head_sha
):
    """同步之后磁盘 HEAD 又变了 → 返回告警文本；一致 / 无数据返回 None。"""

    if not isinstance(state, dict) or not head_sha:

        return None

    synced = state.get("head_after")

    if (
        not isinstance(synced, str)
        or
        not synced.strip()
    ):

        return None

    if commit_prefix_matches(synced, head_sha):

        return None

    return (
        "bootstrap sync 记录的 HEAD（"
        + abbrev_sha(synced)
        + "）与本进程看到的 HEAD（"
        + abbrev_sha(head_sha)
        + "）不一致：磁盘代码在启动前同步之后又被改动，"
        "请确认当前进程加载的版本，必要时重启 start_agent.bat。"
    )


# ============================================================
# Task 路径
# ============================================================

def task_state_path(
    task_id
):

    return (
        TASK_STATE_DIR
        /
        f"{task_id}.json"
    )


def result_path(
    task_id
):

    return (
        RESULT_DIR
        /
        f"{task_id}.json"
    )


# ============================================================
# Runtime Task State
# ============================================================

def write_task_state(
    task_id,
    status,
    **extra
):

    data = {

        "task_id":
            task_id,

        "status":
            status,

        "updated_at":
            now_iso(),

        **extra
    }

    atomic_write_json(
        task_state_path(task_id),
        data
    )


def clear_task_state(
    task_id
):

    path = task_state_path(
        task_id
    )

    if path.exists():

        with contextlib.suppress(OSError):

            path.unlink()


def push_pending_state_paths():
    """列出 runtime 中所有 `push_pending` 任务状态文件。

    仅用于审计与「远端已同步」后的清理，不读取/改写任何 result。
    """

    if not TASK_STATE_DIR.exists():

        return []

    paths = []

    for path in sorted(
        TASK_STATE_DIR.glob("*.json")
    ):

        state = read_json(
            path,
            default=None
        )

        if (
            isinstance(state, dict)
            and
            state.get("status") == "push_pending"
        ):

            paths.append(path)

    return paths


def clear_push_pending_states():
    """远端同步成功后清理 `push_pending` 运行时状态。

    返回被清理的 task_id 列表（仅审计用）。
    本地 commit 与 result 绝不删除；这里只删除 runtime 状态文件。
    """

    cleared = []

    for path in push_pending_state_paths():

        state = read_json(
            path,
            default=None
        )

        task_id = None

        if isinstance(state, dict):

            task_id = state.get("task_id")

        cleared.append(
            task_id
            or
            path.stem
        )

        with contextlib.suppress(OSError):

            path.unlink()

    return cleared


# ============================================================
# Task 配置
# ============================================================

def get_task_max_attempts(
    task
):

    value = task.get(
        "max_attempts",
        DEFAULT_MAX_ATTEMPTS
    )

    try:

        value = int(
            value
        )

    except (
        TypeError,
        ValueError
    ):

        value = (
            DEFAULT_MAX_ATTEMPTS
        )

    # 最少 1 次
    # 最多 10 次
    return max(
        1,
        min(
            value,
            10
        )
    )


# ============================================================
# Rolling Queue
# ============================================================

class TaskMetadataError(
    ValueError
):
    """Task 元数据非法。

    属于安全语义错误：调用方必须 fail-closed 停线，
    禁止静默跳过、禁止继续扫描后续任务。
    """


def qualify_reason(
    task_id: str,
    reason: str
) -> str:
    """确保停线原因始终以当前 task_id 开头，且不重复前缀。"""

    prefix = f"{task_id}: "

    if reason.startswith(prefix):
        return reason

    return f"{prefix}{reason}"


def get_task_bool_flag(
    task: dict[str, Any],
    key: str,
    default: bool
) -> bool:
    """严格读取布尔型 task 元数据。

    只接受真正的 bool（`true` / `false`）。
    字符串 `"true"`、数字 `1`、`null` 等一律 fail-closed。
    """

    value = task.get(key, default)

    if not isinstance(value, bool):

        raise TaskMetadataError(
            f"{key} 必须是 bool 类型"
        )

    return value


def get_task_dependencies(
    task: dict[str, Any]
) -> list[str]:
    """严格读取并校验 depends_on。

    只接受非空 task_id 字符串，或由非空 task_id 字符串组成的数组；
    重复项去重且保持顺序。
    类型错误 / 空 ID / 依赖自身一律 fail-closed 抛 TaskMetadataError。
    """

    value = task.get(
        "depends_on",
        []
    )

    # 未声明依赖（缺字段 / null）等价于无依赖。
    if value is None:
        return []

    if isinstance(
        value,
        str
    ):
        value = [
            value
        ]

    if not isinstance(
        value,
        list
    ):

        raise TaskMetadataError(
            "depends_on 必须是字符串或字符串数组"
        )

    task_id = str(
        task.get(
            "task_id",
            ""
        )
    ).strip()

    dependencies: list[str] = []
    seen: set[str] = set()

    for item in value:

        if not isinstance(
            item,
            str
        ):

            raise TaskMetadataError(
                "depends_on 必须是字符串或字符串数组"
            )

        dependency = item.strip()

        if not dependency:

            raise TaskMetadataError(
                f"{task_id}: depends_on 包含空任务 ID"
            )

        if dependency == task_id:

            raise TaskMetadataError(
                f"{task_id}: depends_on 不允许依赖自身"
            )

        if dependency in seen:
            continue

        seen.add(
            dependency
        )

        dependencies.append(
            dependency
        )

    return dependencies


def get_task_human_gate(
    task: dict[str, Any]
) -> str | None:
    """严格读取并校验 human_gate。

    仅接受 `SUPPORTED_HUMAN_GATES` 词表内的档位（大小写不敏感）；
    未知档位 / 非法类型一律 fail-closed 抛 TaskMetadataError。
    """

    gate = task.get(
        "human_gate"
    )

    if isinstance(
        gate,
        dict
    ):

        if "level" not in gate:

            raise TaskMetadataError(
                "human_gate 对象必须包含 level"
            )

        gate = gate["level"]

    # 未声明 Gate（缺字段 / null / 空字符串）等价于无 Gate。
    if gate is None:
        return None

    if not isinstance(
        gate,
        str
    ):

        raise TaskMetadataError(
            "human_gate 必须是字符串"
        )

    normalized = gate.strip().upper()

    if not normalized:
        return None

    if normalized not in SUPPORTED_HUMAN_GATES:

        raise TaskMetadataError(
            f"human_gate 未知档位: {normalized}"
        )

    return normalized


def task_result_status(
    task_id
):

    existing_result = read_json(
        result_path(
            task_id
        ),
        default=None
    )

    if not existing_result:
        return "pending"

    status = str(
        existing_result.get(
            "status",
            ""
        )
    ).strip().lower()

    return (
        status
        or
        "unknown"
    )


def build_dependency_graph(
    task: dict[str, Any] | None = None
) -> tuple[dict[str, list[str]], list[str]]:
    """构建待处理任务的依赖图。

    - `task=None`：覆盖队列里全部非终态任务（整体校验 / 诊断）。
    - `task=<任务>`：只覆盖该任务及其依赖闭包（单任务 readiness 校验）。

    返回 `(graph, errors)`：

    - `graph`：`{task_id: [仍未完成的依赖 task_id, ...]}`。
      已 completed / blocked 的依赖属于终态，不进入图，也不参与环检测。
    - `errors`：稳定可读的结构性错误
      （task 文件损坏 / 元数据非法 / 缺失依赖），带 owner task_id 前缀。
    """

    graph: dict[str, list[str]] = {}
    errors: list[str] = []
    pending: list[tuple[str, dict[str, Any] | None]] = []
    queued: set[str] = set()

    def enqueue(
        task_id: str,
        payload: dict[str, Any] | None
    ) -> None:

        if task_id in queued or task_id in graph:
            return

        queued.add(
            task_id
        )

        pending.append(
            (task_id, payload)
        )

    if task is None:

        for task_file in sorted(
            TASK_DIR.glob(
                "*.json"
            )
        ):

            task_id = task_file.stem

            if task_result_status(
                task_id
            ) in TERMINAL_RESULT_STATUSES:
                continue

            enqueue(
                task_id,
                None
            )

    else:

        enqueue(
            str(
                task.get(
                    "task_id",
                    ""
                )
            ).strip(),
            task
        )

    while pending:

        task_id, payload = pending.pop(0)

        if payload is None:

            try:

                payload = load_task(
                    TASK_DIR / f"{task_id}.json"
                )

            except Exception as exc:

                errors.append(
                    f"{task_id}: task file invalid: {exc}"
                )

                continue

        try:

            # 依赖 task 自身的元数据也必须合法，
            # 否则它永远无法收口，依赖它的任务只能停线。
            get_task_bool_flag(
                payload,
                "auto_start",
                True
            )

            get_task_bool_flag(
                payload,
                "requires_human_approval",
                False
            )

            get_task_human_gate(
                payload
            )

            dependencies = get_task_dependencies(
                payload
            )

        except TaskMetadataError as exc:

            errors.append(
                f"{task_id}: metadata invalid: {exc}"
            )

            continue

        unresolved: list[str] = []

        for dependency in dependencies:

            dependency_file = (
                TASK_DIR
                /
                f"{dependency}.json"
            )

            if not dependency_file.exists():

                errors.append(
                    f"{task_id}: dependency missing: {dependency}"
                )

                continue

            if task_result_status(
                dependency
            ) in TERMINAL_RESULT_STATUSES:
                continue

            unresolved.append(
                dependency
            )

            enqueue(
                dependency,
                None
            )

        graph[task_id] = unresolved

    return graph, errors


def detect_dependency_cycles(
    graph: dict[str, list[str]]
) -> list[str]:
    """DFS 三色标记检测直接 / 间接依赖环。

    返回形如 `dependency cycle: GOLD-A -> GOLD-B -> GOLD-A`
    的稳定可读原因；无环时返回空列表。
    """

    cycles: list[str] = []
    seen: set[tuple[str, ...]] = set()
    state: dict[str, str] = {}
    stack: list[str] = []

    def visit(
        node: str
    ) -> None:

        state[node] = "visiting"
        stack.append(
            node
        )

        for dependency in graph.get(
            node,
            []
        ):

            if dependency not in graph:
                continue

            dependency_state = state.get(
                dependency
            )

            if dependency_state is None:

                visit(
                    dependency
                )

            elif dependency_state == "visiting":

                cycle = (
                    stack[stack.index(dependency):]
                    +
                    [dependency]
                )

                key = tuple(
                    cycle
                )

                if key in seen:
                    continue

                seen.add(
                    key
                )

                cycles.append(
                    "dependency cycle: "
                    + " -> ".join(
                        cycle
                    )
                )

        stack.pop()
        state[node] = "done"

    for node in sorted(
        graph
    ):

        if node not in state:
            visit(
                node
            )

    return cycles


def dependency_graph_errors(
    task: dict[str, Any] | None = None
) -> list[str]:
    """返回依赖图错误（损坏 task / 元数据非法 / 缺失依赖 / 直接与间接环）。

    结果非空即代表必须 fail-closed 停线。
    """

    graph, errors = build_dependency_graph(
        task
    )

    return errors + detect_dependency_cycles(
        graph
    )


def validate_dependency_graph() -> list[str]:
    """校验队列中全部非终态任务的依赖图（整体诊断 / 测试）。"""

    return dependency_graph_errors(
        None
    )


def evaluate_dependency(
    task_id: str,
    seen: set[str]
) -> tuple[bool, str, str]:
    """递归判定单个依赖（含其自身依赖）是否已经满足。

    返回 `(ok, state, reason)`：

    - `ok=True`：依赖已 completed。
    - `state="pending"`：依赖只是还没跑完，reason 指向最靠前的未完成依赖。
    - `state="blocked"` / `"missing"` / `"unknown"`：
      属于必须停线的阻塞原因，reason 指向最具体的阻塞点。

    依赖环由 `dependency_graph_errors` 单独报告，
    这里用 `seen` 截断递归，避免无限循环。
    """

    if task_id in seen:
        return True, "completed", "ready"

    seen.add(
        task_id
    )

    dependency_file = (
        TASK_DIR
        /
        f"{task_id}.json"
    )

    if not dependency_file.exists():

        return (
            False,
            "missing",
            f"dependency missing: {task_id}"
        )

    status = task_result_status(
        task_id
    )

    if status == "completed":
        return True, "completed", "ready"

    if status == "blocked":

        return (
            False,
            "blocked",
            f"dependency blocked: {task_id}"
        )

    if status not in UNRESOLVED_RESULT_STATUSES:

        return (
            False,
            "unknown",
            f"dependency status unknown: {task_id}={status}"
        )

    # 依赖本身尚未完成：继续向下看它自己的依赖，
    # 以便报告更具体的 blocked / missing / unknown 阻塞点。
    try:

        nested = get_task_dependencies(
            load_task(
                dependency_file
            )
        )

    except Exception:

        return (
            False,
            "pending",
            f"dependency not completed: {task_id}={status}"
        )

    for nested_task_id in nested:

        ok, nested_state, nested_reason = evaluate_dependency(
            nested_task_id,
            seen
        )

        if not ok and nested_state != "pending":
            return False, nested_state, nested_reason

    return (
        False,
        "pending",
        f"dependency not completed: {task_id}={status}"
    )


def evaluate_task_readiness(
    task: dict[str, Any]
) -> tuple[bool, str]:
    """判定任务能否被 rolling queue 自动执行。

    以下情况一律 fail-closed，返回 `(False, reason)`，
    由调用方停线，绝不跳过当前任务：

    - task 元数据非法（depends_on / auto_start / requires_human_approval / human_gate）
    - 自身 result 状态未知
    - 依赖图异常（缺失依赖 / 依赖环 / 依赖 task 损坏）
    - 等待 Human Gate（auto_start=false / requires_human_approval=true / L3、L4）
    - 依赖尚未完成、blocked 或状态未知
    """

    task_id = str(
        task.get(
            "task_id",
            ""
        )
    ).strip()

    try:

        auto_start = get_task_bool_flag(
            task,
            "auto_start",
            True
        )

        requires_human_approval = get_task_bool_flag(
            task,
            "requires_human_approval",
            False
        )

        gate = get_task_human_gate(
            task
        )

        dependencies = get_task_dependencies(
            task
        )

    except TaskMetadataError as exc:

        return (
            False,
            f"{task_id}: metadata invalid: {exc}"
        )

    own_status = task_result_status(
        task_id
    )

    if own_status not in KNOWN_RESULT_STATUSES:

        return (
            False,
            f"{task_id}: result status unknown: {own_status}"
        )

    if not auto_start:

        return (
            False,
            f"{task_id}: auto_start=false"
        )

    if requires_human_approval:

        return (
            False,
            f"{task_id}: requires_human_approval=true"
        )

    if (
        gate
        in
        BLOCKING_HUMAN_GATES
    ):

        return (
            False,
            f"{task_id}: human_gate={gate}"
        )

    graph_errors = dependency_graph_errors(
        task
    )

    if graph_errors:

        return (
            False,
            qualify_reason(
                task_id,
                graph_errors[0]
            )
        )

    for dependency in dependencies:

        ok, _, reason = evaluate_dependency(
            dependency,
            set()
        )

        if not ok:

            return (
                False,
                qualify_reason(
                    task_id,
                    reason
                )
            )

    return (
        True,
        "ready"
    )


def queue_snapshot() -> dict[str, Any]:
    """统计队列里仍未收口的任务。

    终态（completed / blocked）任务一律排除，因此不会重跑历史任务。
    文件名即 task_id：即使 task JSON 损坏，也仍然计入 pending，
    保证停线原因与队列诊断不会掩盖损坏的任务。
    """

    pending: list[str] = []

    for task_file in sorted(
        TASK_DIR.glob(
            "*.json"
        )
    ):

        if task_result_status(
            task_file.stem
        ) in TERMINAL_RESULT_STATUSES:
            continue

        pending.append(
            task_file.stem
        )

    return {
        "pending":
            pending,

        "count":
            len(
                pending
            ),

        "target":
            QUEUE_TARGET_SIZE
    }


def queue_diagnostic(
    wait_reason: str
) -> str:
    """构建 rolling queue 停线诊断文本。

    包含 pending count/target、首个停线 task 与停线原因。
    由 main loop 在 idle throttle 保护下打印，
    避免每 20 秒刷同一条日志，但不降低检测频率。
    """

    snapshot = queue_snapshot()

    pending_tasks: list[str] = snapshot["pending"]
    target: int = snapshot["target"]

    first_stop = (
        pending_tasks[0]
        if pending_tasks
        else
        "-"
    )

    preview = (
        ", ".join(
            pending_tasks[:target]
        )
        or
        "-"
    )

    return (
        f"No runnable task. "
        f"pending={snapshot['count']}/{target} "
        f"[{preview}] | "
        f"first_stop={first_stop} | "
        f"reason={wait_reason}"
    )


def should_log_idle(
    last_logged_at,
    current_time=None
):

    now = (
        time.monotonic()
        if current_time is None
        else current_time
    )

    return (
        last_logged_at
        is None
        or
        (
            now
            -
            last_logged_at
        )
        >=
        IDLE_LOG_SECONDS
    )


# ============================================================
# GPT Planner refill 低水位提示（GOLD-035）
# ============================================================
#
# 背景（.ai/DEVELOPMENT_PROTOCOL.md §2.10）：
#   除「当前正在执行 / 即将执行的 queue head」之外，GPT Planner 默认维持
#   3 个已批准 follow-on task。低水位必须在**队列耗尽之前**被明确看到，
#   而不是等到 `No runnable task` 才发现断粮。
#
# 职责边界（与 GOLD-034 只读事实包完全一致）：
#   1) 只读：复用 orchestrator.planner_refill_request 的确定性事实，
#      这里绝不复制第二套 readiness / 依赖 / 终态 / 漂移判断；
#   2) 只报告：绝不生成任务、绝不补队列、绝不改 PROJECT_STATE /
#      GPT_REVIEW_LEDGER / result，也绝不跳过 blocked / human-gated 的 queue head；
#   3) 节流：同一事实状态在 REFILL_HINT_SECONDS 内只提示一次；事实一变立即重新提示；
#   4) 唯一写操作：把同一份只读事实镜像到受控 runtime 路径（复用
#      planner_snapshot_output 的 fail-closed 守卫），供人工诊断。

# 低水位提示的稳定标记（供人类 / GPT grep 与测试断言；只报告，不含任何决定）。
PLANNER_REFILL_REQUIRED_CODE = "GPT_PLANNER_REFILL_REQUIRED"

# 队列足量 / 只剩合法 human-gated tail 时的标记：同一通道的状态翻转。
PLANNER_REFILL_SATISFIED_CODE = "GPT_PLANNER_REFILL_SATISFIED"

# 提示上下文（机器可读）。
REFILL_HINT_CONTEXT_POST_PUSH = "post_successful_commit_push"

REFILL_HINT_CONTEXT_IDLE = "idle_no_runnable_task"

# 只读事实镜像的文件名（落在 RUNTIME_DIR 下；runtime 不进 Git）。
REFILL_REQUEST_RUNTIME_NAME = "planner_refill_request.json"

# 低水位提示节流：同一事实状态在此秒数内只提示一次（避免每 20 秒刷屏）。
REFILL_HINT_SECONDS = max(
    POLL_SECONDS,
    int(
        os.getenv(
            "AI_REFILL_HINT_SECONDS",
            "1800"
        )
    )
)

# 按需加载过的 orchestrator.* 只读模块（只导入一次，绝不重复执行模块级代码）。
_REPO_SCOPED_MODULES: dict[str, Any] = {}


def repo_scoped_import(
    module_name: str
) -> Any | None:
    """按需导入 ``orchestrator.*`` 只读模块（必要时把仓库根临时放进 ``sys.path``）。

    为什么需要它：本模块在生产环境以 ``py -u orchestrator/ai_orchestrator.py``
    方式启动，此时 ``sys.path[0]`` 是 ``orchestrator/`` 而不是仓库根，
    ``import orchestrator.X`` 只有在仓库根位于 ``sys.path`` 上时才成立。
    这里只在必要时临时补上仓库根，导入结束后立即还原，**只读**、不留副作用；
    导入失败一律返回 ``None``（只读提示降级，绝不阻塞 rolling queue）。
    """

    cached = _REPO_SCOPED_MODULES.get(
        module_name
    )

    if cached is not None:

        return cached

    root = str(
        ROOT
    )

    inserted = (
        root
        not in
        sys.path
    )

    if inserted:

        sys.path.insert(
            0,
            root
        )

    try:

        module = importlib.import_module(
            module_name
        )

    except Exception as exc:

        _logger.warning(
            "导入只读模块 %s 失败（只读提示降级）: %s",
            module_name,
            exc
        )

        return None

    finally:

        if inserted:

            with contextlib.suppress(
                ValueError
            ):

                sys.path.remove(
                    root
                )

    _REPO_SCOPED_MODULES[
        module_name
    ] = module

    return module


def planner_refill_facts() -> dict[str, Any] | None:
    """只读构建 GOLD-034 refill 事实包；任何失败都降级为 ``None``。

    降级即「本轮不出提示」，绝不影响 rolling queue 的推进；事实包本身只读，
    绝不写 ``.ai/tasks`` / ``.ai/results`` / ``PROJECT_STATE`` / review ledger。
    """

    refill = repo_scoped_import(
        "orchestrator.planner_refill_request"
    )

    if refill is None:

        return None

    try:

        return refill.build_planner_refill_request(
            root=ROOT,
            state_path=PROJECT_STATE_PATH,
            tasks_dir=TASK_DIR,
            results_dir=RESULT_DIR,
        )

    except Exception as exc:

        _logger.warning(
            "Planner refill 事实构建失败（只读提示降级）: %s",
            exc
        )

        return None


def planner_refill_hint_state(
    state: dict[str, Any] | None
) -> tuple[str | None, float | None]:
    """从跨轮状态里读出 ``(signature, emitted_at)``；缺失 / 非法一律视为空。"""

    if not isinstance(state, dict):

        return None, None

    signature = state.get(
        "signature"
    )

    emitted_at = state.get(
        "emitted_at"
    )

    valid_emitted_at = (
        emitted_at
        if (
            isinstance(emitted_at, (int, float))
            and
            not isinstance(emitted_at, bool)
        )
        else
        None
    )

    return (
        signature if isinstance(signature, str) else None,
        valid_emitted_at
    )


def should_emit_refill_hint(
    signature: str,
    last_signature: str | None,
    last_emitted_at: float | None,
    current_time: float | None = None
) -> bool:
    """低水位提示节流：状态变化立即提示；同一状态只在 ``REFILL_HINT_SECONDS`` 后重复。

    与 :func:`should_log_idle` 同构：节流只影响日志噪声，
    绝不降低检测频率，也绝不改变任何队列决策。
    """

    if signature != last_signature:

        return True

    if last_emitted_at is None:

        return True

    now = (
        time.monotonic()
        if current_time is None
        else current_time
    )

    return (
        now
        -
        last_emitted_at
    ) >= REFILL_HINT_SECONDS


def planner_refill_hint_signature(
    payload: dict[str, Any]
) -> str:
    """低水位提示的稳定状态签名（只由确定性事实派生，wall-clock 不参与）。"""

    codes = payload.get(
        "reason_codes"
    )

    reasons = (
        ",".join(
            str(item)
            for item in codes
        )
        if isinstance(codes, list)
        else
        ""
    )

    return (
        f"digest={payload.get('facts_digest')}"
        f"|head={payload.get('queue_head')}"
        f"|follow_on={payload.get('follow_on_count')}"
        f"|target={payload.get('lookahead_target')}"
        f"|deficit={payload.get('deficit')}"
        f"|required={payload.get('refill_required')}"
        f"|codes={reasons}"
    )


def planner_refill_hint_line(
    payload: dict[str, Any],
    *,
    context: str
) -> str:
    """构建结构化 refill 提示行。

    稳定字段名：`head` / `follow_on_count` / `target` / `deficit` / `reason_codes`。
    提示里**只有事实**：缺几个、卡在哪个 queue head、为什么，以及
    `executor_can_refill=false`（规划权仍只属于 GPT）。不含任何后续任务内容。
    """

    code = (
        PLANNER_REFILL_REQUIRED_CODE
        if payload.get("refill_required")
        else PLANNER_REFILL_SATISFIED_CODE
    )

    codes = payload.get(
        "reason_codes"
    )

    reasons = (
        ",".join(
            str(item)
            for item in codes
        )
        if isinstance(codes, list)
        else
        ""
    )

    return (
        f"{code}"
        f" context={context}"
        f" head={payload.get('queue_head') or '-'}"
        f" follow_on_count={payload.get('follow_on_count')}"
        f" target={payload.get('lookahead_target')}"
        f" deficit={payload.get('deficit')}"
        f" hard_gate_tail_allowed={payload.get('hard_gate_tail_allowed')}"
        f" reason_codes=[{reasons}]"
        f" facts_digest={payload.get('facts_digest')}"
        f" executor_can_refill=false"
    )


def planner_refill_mirror_allowed() -> bool:
    """runtime 镜像的前置条件：只读事实必须来自**本仓库自己的** tasks / results。

    这不是权限判断，而是「镜像内容可信」判断：只有 Orchestrator 正在观察自己的
    ``<root>/.ai/tasks`` / ``.ai/results`` 时，写进 ``.ai/runtime/**`` 的 refill
    事实才对人工诊断有意义；路径被重定向时只输出日志、不写镜像文件。
    """

    try:

        root = Path(
            ROOT
        ).resolve()

        directories = (
            Path(TASK_DIR),
            Path(RESULT_DIR)
        )

        return all(

            root == directory.resolve()

            or

            root in directory.resolve().parents

            for directory in directories
        )

    except Exception:

        return False


def mirror_planner_refill_request(
    payload: dict[str, Any],
    *,
    root: Path | None = None,
    target: Path | None = None
) -> Path | None:
    """把只读 refill 事实镜像写入受控 runtime 路径。

    写入**唯一**复用 :mod:`orchestrator.planner_snapshot_output` 的 fail-closed
    守卫：只允许 ``<root>/.ai/runtime/**`` 或系统临时目录，其余位置一律拒绝且
    不写任何文件（因此绝不写 ``.ai/tasks`` / ``.ai/results`` /
    ``.ai/PROJECT_STATE.json`` / ``.ai/GPT_REVIEW_LEDGER.json``）。
    """

    snapshot_output = repo_scoped_import(
        "orchestrator.planner_snapshot_output"
    )

    refill = repo_scoped_import(
        "orchestrator.planner_refill_request"
    )

    if snapshot_output is None or refill is None:

        return None

    resolved_root = (
        Path(root)
        if root is not None
        else ROOT
    )

    resolved_target = (
        Path(target)
        if target is not None
        else RUNTIME_DIR / REFILL_REQUEST_RUNTIME_NAME
    )

    output_target, reason = snapshot_output.resolve_output_target(
        resolved_root,
        resolved_target
    )

    if output_target is None:

        _logger.warning(
            "Planner refill 事实未镜像（fail-closed）: %s",
            reason
        )

        return None

    try:

        snapshot_output.write_snapshot_output(
            output_target,
            refill.render_planner_refill_request(
                payload
            )
        )

    except Exception as exc:

        # 镜像只是「人工诊断副本」：任何 I/O 失败都必须降级为日志，
        # 绝不允许影响 rolling queue 的推进。
        _logger.warning(
            "Planner refill 事实镜像写入失败（忽略，不影响队列）: %s",
            exc
        )

        return None

    return output_target


def planner_refill_report(
    *,
    context: str,
    only_when_required: bool = False,
    throttle: bool = True,
    last_signature: str | None = None,
    last_emitted_at: float | None = None,
    current_time: float | None = None
) -> dict[str, Any]:
    """只读计算 refill 事实并按需输出结构化提示（+ runtime 镜像）。

    返回值永远是**新的提示状态**，因此调用方即使本轮没输出提示也能正确推进
    节流窗口：

    - ``payload``：只读事实包；事实不可读时为 ``None``（只读提示降级）；
    - ``signature``：当前事实的稳定签名；
    - ``emitted``：本轮是否真的输出了提示；
    - ``line``：本轮的提示行（未输出时为 ``None``）；
    - ``emitted_at``：最近一次输出提示的 monotonic 时间。

    ``only_when_required=True`` 用于「成功 commit+push 之后」路径：只有
    ``deficit > 0`` 才提示（队列足量时保持安静）；idle 路径则两种状态都提示，
    但受 :func:`should_emit_refill_hint` 节流保护。
    """

    payload = planner_refill_facts()

    if payload is None:

        return {
            "payload": None,
            "signature": last_signature,
            "emitted": False,
            "line": None,
            "emitted_at": last_emitted_at,
        }

    signature = planner_refill_hint_signature(
        payload
    )

    now = (
        time.monotonic()
        if current_time is None
        else current_time
    )

    required = bool(
        payload.get(
            "refill_required"
        )
    )

    suppressed_by_requirement = only_when_required and not required

    suppressed_by_throttle = throttle and not should_emit_refill_hint(
        signature,
        last_signature,
        last_emitted_at,
        current_time=now
    )

    emit = not (
        suppressed_by_requirement
        or
        suppressed_by_throttle
    )

    line = None

    if emit:

        line = planner_refill_hint_line(
            payload,
            context=context
        )

        _logger.info(
            "%s",
            line
        )

        if planner_refill_mirror_allowed():

            mirror_planner_refill_request(
                payload
            )

        else:

            _logger.info(
                "Planner refill 事实未镜像："
                "只读事实不来自本仓库 queue（只报告，不写文件）"
            )

    return {
        "payload": payload,
        "signature": signature,
        "emitted": emit,
        "line": line,
        "emitted_at": (
            now
            if emit
            else last_emitted_at
        ),
    }


def planner_refill_idle_hint(
    state: dict[str, Any] | None = None,
    *,
    current_time: float | None = None
) -> dict[str, Any]:
    """idle / no-runnable 路径的**节流**低水位提示，返回可跨轮传递的新状态。

    - 队列足量时也会提示一次 ``GPT_PLANNER_REFILL_SATISFIED``（状态翻转可见），
      之后同样按 ``REFILL_HINT_SECONDS`` 节流；
    - 事实一变（例如刚完成 / 阻塞一个 task）签名就变，立即重新提示，
      因此「队列耗尽之前」始终能看到 ``GPT_PLANNER_REFILL_REQUIRED``；
    - 只报告：绝不生成任务、绝不改状态，也绝不改变本轮停线判定。
    """

    last_signature, last_emitted_at = planner_refill_hint_state(
        state
    )

    report = planner_refill_report(
        context=REFILL_HINT_CONTEXT_IDLE,
        throttle=True,
        last_signature=last_signature,
        last_emitted_at=last_emitted_at,
        current_time=current_time,
    )

    return {
        "signature": report["signature"],
        "emitted_at": report["emitted_at"],
    }


# ============================================================
# Task 读取
# ============================================================

def load_task(
    task_file
):

    with open(
        task_file,
        encoding="utf-8"
    ) as f:

        task = json.load(f)

    task_id = str(
        task.get(
            "task_id",
            ""
        )
    ).strip()

    if not task_id:

        raise ValueError(
            f"任务缺少 task_id: "
            f"{task_file}"
        )

    # 防止：
    #
    # 文件名：
    # GOLD-001.json
    #
    # 里面却写：
    # GOLD-002
    #
    if (
        task_id
        !=
        task_file.stem
    ):

        raise ValueError(

            "task_id 与文件名不一致: "

            f"task_id={task_id}, "

            f"file={task_file.name}"
        )

    return task


# ============================================================
# 查找下一个任务
# ============================================================

def find_next_task_with_reason() -> tuple[Path | None, str]:
    """查找队列中第一个可执行的 task。

    fail-closed 语义：

    - 终态（completed / blocked）任务仍可跳过，且绝不重写历史 result；
    - 第一个非终态任务一旦 JSON 损坏 / 元数据非法 / 依赖异常
      （缺失依赖、依赖环、依赖 task 损坏）/ 等待 Human Gate /
      依赖 blocked、unknown、pending，立即停线并返回稳定可读原因；
    - 绝不 `continue` 到后续 task。
    """

    for task_file in sorted(
        TASK_DIR.glob(
            "*.json"
        )
    ):

        task_id = task_file.stem

        if task_result_status(
            task_id
        ) in TERMINAL_RESULT_STATUSES:
            continue

        try:

            task = load_task(
                task_file
            )

        except Exception as exc:

            _logger.error(
                "任务 %s 损坏，rolling queue 停线: %s",
                task_file.name,
                exc
            )

            return (
                None,
                f"{task_id}: task file invalid: {exc}"
            )

        ready, reason = evaluate_task_readiness(
            task
        )

        if not ready:

            return (
                None,
                reason
            )

        return (
            task_file,
            "ready"
        )

    return (
        None,
        "queue empty"
    )


def find_next_task():

    task_file, _ = (
        find_next_task_with_reason()
    )

    return task_file


# ============================================================
# Cline Prompt
# ============================================================

def build_cline_prompt(
    task_file
):

    relative_task = (
        task_file
        .relative_to(ROOT)
        .as_posix()
    )

    return (

        f"读取任务文件 "
        f"{relative_task}，"

        f"严格按照其中任务执行，"

        f"同时遵守项目根目录 "
        f".clinerules。"

        f"先分析现有代码和相关文档，"
        f"再进行最小范围修改。"

        f"完成 task 中的 "
        f"requirements、acceptance "
        f"和必要测试。"

        f"不要修改 .ai/tasks，"

        f"不要修改 .ai/results。"

        f"完成后总结："

        f"完成内容、修改文件、"
        f"测试结果、遗留问题、"
        f"是否满足 acceptance，"

        f"然后退出。"
    )


# ============================================================
# Cline Failure Classification / Recovery
# ============================================================

NON_RETRYABLE_EXTERNAL_PATTERNS = {
    "insufficient balance":
        "INSUFFICIENT_BALANCE",

    "credits balance is $0.00":
        "INSUFFICIENT_BALANCE",

    "insufficient_credits":
        "INSUFFICIENT_BALANCE",

    "payment required":
        "PAYMENT_REQUIRED",

    "quota exceeded":
        "QUOTA_EXHAUSTED",

    "quota exhausted":
        "QUOTA_EXHAUSTED",

    "invalid api key":
        "INVALID_API_KEY",

    "api key is invalid":
        "INVALID_API_KEY",

    "authentication failed":
        "AUTHENTICATION_FAILED",

    "unauthorized":
        "AUTHENTICATION_FAILED",

    "model not found":
        "MODEL_UNAVAILABLE",

    "model is not available":
        "MODEL_UNAVAILABLE"
}

RETRYABLE_EXTERNAL_PATTERNS = {
    "rate limit":
        "RATE_LIMITED",

    "too many requests":
        "RATE_LIMITED",

    "temporarily unavailable":
        "PROVIDER_TEMPORARY_UNAVAILABLE",

    "service unavailable":
        "PROVIDER_TEMPORARY_UNAVAILABLE",

    "bad gateway":
        "PROVIDER_BAD_GATEWAY",

    "gateway timeout":
        "PROVIDER_GATEWAY_TIMEOUT",

    "connection reset":
        "NETWORK_ERROR",

    "connection aborted":
        "NETWORK_ERROR",

    "connection refused":
        "NETWORK_ERROR",

    "network error":
        "NETWORK_ERROR",

    "socket hang up":
        "NETWORK_ERROR",

    "econnreset":
        "NETWORK_ERROR",

    "etimedout":
        "NETWORK_ERROR"
}


def classify_cline_failure(
    cline_result
):

    stderr_text = (
        cline_result.get(
            "stderr",
            ""
        )
        or
        ""
    ).lower()

    # Provider fatal errors are emitted on stderr by Cline.
    # Check them even when the process happens to return 0,
    # so validation cannot turn a billing/auth failure into a false success.
    for pattern, code in (
        NON_RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in stderr_text:

            return (
                "non_retryable_external",
                code
            )

    for pattern, code in (
        RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in stderr_text:

            return (
                "retryable_external",
                code
            )

    if cline_result.get(
        "timed_out",
        False
    ):

        return (
            "retryable_external",
            "CLINE_TIMEOUT"
        )

    if (
        cline_result.get(
            "returncode"
        )
        ==
        0
    ):

        return (
            "none",
            None
        )

    # Non-zero CLI exits may encode the fatal error as JSON on stdout.
    stdout_text = (
        cline_result.get(
            "stdout",
            ""
        )
        or
        ""
    ).lower()

    for pattern, code in (
        NON_RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in stdout_text:

            return (
                "non_retryable_external",
                code
            )

    for pattern, code in (
        RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in stdout_text:

            return (
                "retryable_external",
                code
            )

    return (
        "execution_failure",
        "CLINE_EXECUTION_FAILED"
    )

def build_cline_args(
    cline_exe,
    prompt
):

    args = [
        cline_exe,
        "--json",
        "--yolo",
        "--provider",
        CLINE_PROVIDER,
    ]

    if CLINE_MODEL:

        args.extend([
            "--model",
            CLINE_MODEL
        ])

    args.extend([
        "--timeout",
        str(
            CLINE_TIMEOUT_SECONDS
        ),
        prompt
    ])

    return args


def recovery_snapshot_dir(
    task_id,
    attempt
):

    stamp = (
        datetime
        .now()
        .astimezone()
        .strftime(
            "%Y%m%dT%H%M%S%z"
        )
    )

    return (
        RECOVERY_DIR
        /
        f"{task_id}-attempt-{attempt}-{stamp}"
    )


def save_recovery_snapshot(
    task_id,
    attempt,
    failure_class,
    failure_code
):

    snapshot_dir = (
        recovery_snapshot_dir(
            task_id,
            attempt
        )
    )

    snapshot_dir.mkdir(
        parents=True,
        exist_ok=False
    )

    patch_result = git(
        "diff --binary HEAD"
    )

    patch_text = (
        patch_result.get(
            "stdout",
            ""
        )
        if (
            patch_result.get(
                "returncode"
            )
            ==
            0
        )
        else
        ""
    )

    patch_path = (
        snapshot_dir
        /
        "changes.patch"
    )

    patch_path.write_text(
        patch_text,
        encoding="utf-8"
    )

    untracked_result = git(
        "ls-files --others --exclude-standard"
    )

    untracked_files = []

    untracked_hashes: dict[str, str] = {}

    if (
        untracked_result.get(
            "returncode"
        )
        ==
        0
    ):

        for raw_path in (
            untracked_result
            .get(
                "stdout",
                ""
            )
            .splitlines()
        ):

            relative = (
                raw_path
                .strip()
            )

            if not relative:

                continue

            source = (
                ROOT
                /
                relative
            )

            try:

                resolved = source.resolve()

                resolved.relative_to(
                    ROOT.resolve()
                )

            except (
                OSError,
                ValueError
            ):

                continue

            if (
                not source.exists()
                or
                not source.is_file()
                or
                source.is_symlink()
            ):

                continue

            destination = (
                snapshot_dir
                /
                "untracked"
                /
                relative
            )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            shutil.copy2(
                source,
                destination
            )

            untracked_files.append(
                relative
            )

            copied_hash = sha256_file(
                destination
            )

            if copied_hash is None:

                raise RuntimeError(

                    "恢复快照写入后无法校验: "
                    f"{relative}"
                )

            untracked_hashes[
                relative
            ] = copied_hash

    tracked_files, _ = split_dirty_paths(
        get_git_status_lines()
    )

    patch_bytes = (
        patch_path.stat().st_size
    )

    metadata = {
        "task_id":
            task_id,

        "attempt":
            attempt,

        "created_at":
            now_iso(),

        "failure_class":
            failure_class,

        "failure_code":
            failure_code,

        # 可恢复性证据（GOLD-022）：schema + 逐文件哈希，
        # 让「清理前必须先证明 snapshot 可恢复」有确定依据。
        "schema":
            RECOVERY_SNAPSHOT_SCHEMA,

        "head":
            head_full_sha(),

        "branch":
            current_branch(),

        "pid":
            os.getpid(),

        "changed_files":
            get_changed_files(),

        "tracked_files":
            tracked_files,

        "untracked_files":
            untracked_files,

        "untracked_sha256":
            untracked_hashes,

        "patch_file":
            "changes.patch",

        "patch_bytes":
            patch_bytes,

        "patch_sha256":
            sha256_file(patch_path)
    }

    atomic_write_json(
        snapshot_dir
        /
        "recovery.json",
        metadata
    )

    return snapshot_dir


# ============================================================
# 中断现场判定（GOLD-022）
# ============================================================
#
# 「可证明属于上一次同一 task 的中断现场」的完整证据链：
#   1) runtime 恰好存在 1 条 `status=running` 的 attempt 记录；
#   2) 该记录的 task_id 与 rolling queue 当前真正会执行的 task 完全一致；
#   3) 记录带 `ATTEMPT_EVIDENCE_SCHEMA`（即由本版本在 clean 工作区上写入的现场基线）；
#   4) 记录里的 owner PID 已不存在（能证明 owner 进程已消失）；
#   5) 记录里的 HEAD / branch 与当前完全一致（中断期间没有任何新 commit / 分支切换）；
#   6) 该 task 至今没有终态 result（否则现场不可能是「未完成的中断现场」）。
# 任何一条不成立 ⇒ WORKTREE_UNKNOWN ⇒ 严格停线，绝不 snapshot、绝不清理。


def current_attempt_baseline() -> dict[str, Any]:
    """attempt 启动时的现场基线（中断证据的唯一合法来源）。

    调用时机固定在「工作区已确认 clean」之后，
    因此这份基线可以证明：dirty 变化发生在该 attempt 期间。
    """

    return {
        "evidence_schema":
            ATTEMPT_EVIDENCE_SCHEMA,

        "pid":
            os.getpid(),

        "branch":
            current_branch(),

        "head":
            head_full_sha(),

        "worktree_clean":
            True,

        "baseline_at":
            now_iso()
    }


def running_attempt_states() -> list[dict[str, Any]]:
    """列出 runtime 中所有 `status=running` 的 attempt 记录。

    只做只读枚举；损坏文件 / 非 running 状态一律不计入。
    """

    states: list[dict[str, Any]] = []

    if not TASK_STATE_DIR.exists():

        return states

    for path in sorted(
        TASK_STATE_DIR.glob(
            "*.json"
        )
    ):

        state = read_json(
            path,
            default=None
        )

        if not isinstance(state, dict):

            continue

        if state.get("status") != "running":

            continue

        states.append(
            state
        )

    return states


def interrupted_scene_assessment(
    ready_task_id: str | None
) -> tuple[str, str, dict[str, Any] | None]:
    """判定 dirty worktree 是否**可证明**属于上一次同一 task 的中断现场。

    返回 `(classification, reason, state)`；
    `classification == WORKTREE_INTERRUPTED_TASK` 时 `state` 为那份证据记录。
    """

    states = running_attempt_states()

    if not states:

        return (
            WORKTREE_UNKNOWN,
            "no running attempt record in runtime: "
            "dirty worktree cannot be attributed",
            None
        )

    if len(states) > 1:

        return (
            WORKTREE_UNKNOWN,
            f"{len(states)} running attempt records in runtime "
            "(expected exactly 1)",
            None
        )

    state = states[0]

    recorded_task = str(
        state.get(
            "task_id",
            ""
        )
    ).strip()

    if not recorded_task:

        return (
            WORKTREE_UNKNOWN,
            "running attempt record has no task_id",
            None
        )

    if ready_task_id is None:

        return (
            WORKTREE_UNKNOWN,

            f"running attempt record is {recorded_task} "
            "but rolling queue has no runnable task",

            None
        )

    if recorded_task != ready_task_id:

        return (
            WORKTREE_UNKNOWN,

            f"running attempt record is {recorded_task} "
            f"but rolling queue would run {ready_task_id}",

            None
        )

    if (
        state.get("evidence_schema")
        !=
        ATTEMPT_EVIDENCE_SCHEMA
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has no compatible evidence schema",
            None
        )

    attempt = state.get(
        "attempt"
    )

    if (
        isinstance(attempt, bool)
        or
        not isinstance(attempt, int)
        or
        attempt < 1
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has an invalid attempt number",
            None
        )

    started_at = state.get(
        "started_at"
    )

    if (
        not isinstance(started_at, str)
        or
        not started_at.strip()
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has no started_at timestamp",
            None
        )

    pid = state.get(
        "pid"
    )

    if (
        isinstance(pid, bool)
        or
        not isinstance(pid, int)
        or
        pid <= 0
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has no usable owner pid",
            None
        )

    if pid == os.getpid():

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "is owned by the current process",
            None
        )

    if is_pid_running(
        pid
    ):

        return (
            WORKTREE_UNKNOWN,
            f"attempt owner pid {pid} is still running",
            None
        )

    recorded_head = state.get(
        "head"
    )

    if (
        not isinstance(recorded_head, str)
        or
        not recorded_head.strip()
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has no HEAD baseline",
            None
        )

    current_head = head_full_sha()

    if (
        not current_head
        or
        recorded_head.strip().lower()
        !=
        current_head.strip().lower()
    ):

        return (
            WORKTREE_UNKNOWN,

            "HEAD changed since attempt start "
            f"(recorded={abbrev_sha(recorded_head)}, "
            f"current={abbrev_sha(current_head)})",

            None
        )

    recorded_branch = state.get(
        "branch"
    )

    if (
        not isinstance(recorded_branch, str)
        or
        not recorded_branch.strip()
    ):

        return (
            WORKTREE_UNKNOWN,
            f"running attempt record for {recorded_task} "
            "has no branch baseline",
            None
        )

    current_branch_name = current_branch()

    if (
        not current_branch_name
        or
        recorded_branch.strip()
        !=
        current_branch_name.strip()
    ):

        return (
            WORKTREE_UNKNOWN,

            "branch changed since attempt start "
            f"(recorded={recorded_branch}, "
            f"current={current_branch_name or '<unknown>'})",

            None
        )

    if (
        task_result_status(
            recorded_task
        )
        in
        TERMINAL_RESULT_STATUSES
    ):

        return (
            WORKTREE_UNKNOWN,
            f"{recorded_task} already has a terminal result: "
            "dirty worktree cannot be an interrupted scene",
            None
        )

    describe = (
        f"interrupted attempt {recorded_task}#{attempt} "
        f"(pid={pid}, started_at={started_at}, "
        f"head={abbrev_sha(recorded_head)}): "
        "owner process gone, HEAD/branch unchanged, no terminal result"
    )

    return (
        WORKTREE_INTERRUPTED_TASK,
        describe,
        state
    )


# ============================================================
# Snapshot 可恢复性校验与安全清理（GOLD-022）
# ============================================================


def snapshot_relative_path(
    path: Path
) -> str:
    """把 snapshot 路径转成相对仓库根的稳定可读形式。"""

    try:

        return str(
            path.relative_to(
                ROOT
            )
        )

    except ValueError:

        return str(path)


def verify_recovery_snapshot(
    snapshot_dir: Path,
    require_worktree_match: bool = False
) -> tuple[bool, str]:
    """校验 recovery snapshot 是否完整且**可恢复**。

    这是任何自动清理之前的**唯一前置条件**（fail-closed）：
    - metadata 必须是本版本 schema，且 SHA-256 / 字节数与磁盘文件逐项一致；
    - untracked 副本必须存在且哈希与记录一致；
    - `require_worktree_match=True` 时还要求 patch 与**当前**工作区互为逆操作、
      且 untracked 副本与工作区现存文件内容一致（证明快照确实记录了现场）。
    """

    if not snapshot_dir.is_dir():

        return False, f"snapshot directory missing: {snapshot_dir}"

    metadata = read_json(
        snapshot_dir
        /
        "recovery.json",
        default=None
    )

    if not isinstance(metadata, dict):

        return False, "snapshot metadata unreadable"

    if (
        metadata.get("schema")
        !=
        RECOVERY_SNAPSHOT_SCHEMA
    ):

        return False, "snapshot metadata has no compatible schema"

    task_id = metadata.get("task_id")

    if (
        not isinstance(task_id, str)
        or
        not task_id.strip()
    ):

        return False, "snapshot metadata has no task_id"

    attempt = metadata.get("attempt")

    if (
        isinstance(attempt, bool)
        or
        not isinstance(attempt, int)
        or
        attempt < 0
    ):

        return False, "snapshot metadata has an invalid attempt number"

    created_at = metadata.get("created_at")

    if (
        not isinstance(created_at, str)
        or
        not created_at.strip()
    ):

        return False, "snapshot metadata has no created_at"

    tracked_files = metadata.get("tracked_files")

    if (
        not isinstance(tracked_files, list)
        or
        not all(
            isinstance(item, str)
            for item in tracked_files
        )
    ):

        return False, "snapshot metadata has an invalid tracked_files list"

    untracked_hashes = metadata.get("untracked_sha256")

    if (
        not isinstance(untracked_hashes, dict)
        or
        not all(
            isinstance(key, str)
            and
            isinstance(value, str)
            for key, value in untracked_hashes.items()
        )
    ):

        return False, "snapshot metadata has an invalid untracked_sha256 map"

    patch_path = (
        snapshot_dir
        /
        str(
            metadata.get(
                "patch_file",
                "changes.patch"
            )
        )
    )

    if not patch_path.is_file():

        return False, f"snapshot patch missing: {patch_path.name}"

    patch_size = patch_path.stat().st_size

    patch_hash = sha256_file(
        patch_path
    )

    if patch_hash is None:

        return False, f"snapshot patch unreadable: {patch_path.name}"

    if patch_hash != metadata.get("patch_sha256"):

        return False, "snapshot patch sha256 mismatch"

    if patch_size != metadata.get("patch_bytes"):

        return False, "snapshot patch size mismatch"

    if bool(tracked_files) != (patch_size > 0):

        return False, "snapshot patch does not match recorded tracked files"

    for relative, expected in sorted(
        untracked_hashes.items()
    ):

        copied = sha256_file(
            snapshot_dir
            /
            "untracked"
            /
            relative
        )

        if copied is None:

            return False, (
                "snapshot copy missing for untracked file: "
                f"{relative}"
            )

        if copied != expected:

            return False, (
                "snapshot copy sha256 mismatch for untracked file: "
                f"{relative}"
            )

    if not tracked_files and not untracked_hashes:

        return False, "snapshot records no recoverable change"

    if require_worktree_match:

        if tracked_files:

            check = git(
                f'apply --check --reverse --binary "{patch_path}"'
            )

            if check["returncode"] != 0:

                message = (
                    check["stderr"].strip()
                    or
                    check["stdout"].strip()
                )

                return False, (
                    "snapshot patch does not match the current worktree: "
                    f"{message}"
                )

        for relative, expected in sorted(
            untracked_hashes.items()
        ):

            workspace_hash = sha256_file(
                ROOT
                /
                relative
            )

            if workspace_hash is None:

                return False, (
                    "workspace file missing for untracked snapshot: "
                    f"{relative}"
                )

            if workspace_hash != expected:

                return False, (
                    "workspace file differs from untracked snapshot: "
                    f"{relative}"
                )

    return True, "verified"


def restore_recovery_snapshot(
    snapshot_dir: Path
) -> list[str]:
    """把已验证的 recovery snapshot 恢复到工作区（人工 / 测试用，fail-closed）。

    - 未通过 `verify_recovery_snapshot()` 一律拒绝恢复；
    - 已存在且内容不同的人工文件**绝不覆盖**（抛错，交由人工判断）；
    - 只写工作区，绝不写 result、绝不 commit。
    """

    verified, reason = verify_recovery_snapshot(
        snapshot_dir
    )

    if not verified:

        raise RuntimeError(
            f"recovery snapshot 不可恢复，拒绝 restore: {reason}"
        )

    metadata = read_json(
        snapshot_dir
        /
        "recovery.json",
        default={}
    )

    if not isinstance(metadata, dict):

        raise RuntimeError(
            "recovery snapshot metadata 损坏，拒绝 restore"
        )

    restored: list[str] = []

    tracked_files = metadata.get(
        "tracked_files",
        []
    )

    if tracked_files:

        patch_path = (
            snapshot_dir
            /
            str(
                metadata.get(
                    "patch_file",
                    "changes.patch"
                )
            )
        )

        applied = git(
            f'apply --binary "{patch_path}"'
        )

        if applied["returncode"] != 0:

            message = (
                applied["stderr"].strip()
                or
                applied["stdout"].strip()
            )

            raise RuntimeError(
                "恢复 tracked 修改失败: " + message
            )

        restored.extend(
            tracked_files
        )

    untracked_hashes = metadata.get(
        "untracked_sha256",
        {}
    )

    if not isinstance(untracked_hashes, dict):

        raise RuntimeError(
            "recovery snapshot untracked 记录损坏，拒绝 restore"
        )

    for relative in sorted(
        untracked_hashes
    ):

        source = (
            snapshot_dir
            /
            "untracked"
            /
            relative
        )

        destination = (
            ROOT
            /
            relative
        )

        if destination.exists():

            if (
                sha256_file(destination)
                !=
                sha256_file(source)
            ):

                raise RuntimeError(
                    f"拒绝覆盖内容不同的已存在文件: {relative}"
                )

            continue

        destination.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        shutil.copy2(
            source,
            destination
        )

        restored.append(
            relative
        )

    return restored


def record_recovery_event(
    event: dict[str, Any]
) -> dict[str, Any]:
    """把一次恢复决策写入 runtime 审计文件（有界、原子写）。

    只写 `.ai/runtime/recovery/recovery_state.json`：
    绝不写 `.ai/results/**`，绝不 commit，绝不把恢复当成任务完成。
    """

    state = read_json(
        RECOVERY_STATE_FILE,
        default=None
    )

    events: list[Any] = []

    if (
        isinstance(state, dict)
        and
        isinstance(state.get("events"), list)
    ):

        events = [
            item
            for item in state["events"]
            if isinstance(item, dict)
        ]

    record = {
        "recorded_at": now_iso(),
        **event
    }

    events.append(
        record
    )

    atomic_write_json(

        RECOVERY_STATE_FILE,

        {
            "schema":
                RECOVERY_STATE_SCHEMA,

            "updated_at":
                now_iso(),

            "events":
                events[-RECOVERY_EVENT_HISTORY_LIMIT:]
        }
    )

    return record


def should_log_worktree_block() -> bool:
    """人工介入告警限流（默认 IDLE_LOG_SECONDS），检测频率不变。"""

    global _last_worktree_block_log_at

    now = time.monotonic()

    if (
        _last_worktree_block_log_at
        is not None
        and
        (now - _last_worktree_block_log_at)
        <
        IDLE_LOG_SECONDS
    ):

        return False

    _last_worktree_block_log_at = now

    return True


def cleanup_interrupted_scene(
    task_id: str,
    state: dict[str, Any],
    dirty_lines: list[str],
    assessment: str
) -> dict[str, Any]:
    """保存 → 校验 → 清理「可证明属于本 task 的中断现场」。

    顺序固定：先 snapshot，再校验可恢复，**只有校验通过**才清理并允许重试同一 task。
    校验失败 / 清理失败都停线，绝不在证据不足时动工作区。
    """

    attempt = state.get(
        "attempt"
    )

    tracked_files, untracked_files = split_dirty_paths(
        dirty_lines
    )

    snapshot_dir: Path

    try:

        snapshot_dir = save_recovery_snapshot(

            task_id,

            attempt,

            INTERRUPTED_FAILURE_CLASS,

            INTERRUPTED_FAILURE_CODE
        )

    except (
        OSError,
        RuntimeError
    ) as exc:

        _logger.error(
            "中断现场 snapshot 写入失败（%s）：严格停线，"
            "绝不清理工作区，请人工处理现场。",
            exc
        )

        record_recovery_event(

            {
                "kind":
                    RECOVERY_EVENT_WORKTREE,

                "decision":
                    "auto_recover",

                "outcome":
                    RECOVERY_OUTCOME_SNAPSHOT_FAILED,

                "task_id":
                    task_id,

                "attempt":
                    attempt,

                "assessment":
                    assessment,

                "dirty_files":
                    list(dirty_lines),

                "reason":
                    str(exc),

                "snapshot":
                    None,

                "snapshot_verified":
                    False,

                "cleaned":
                    False
            }
        )

        return {

            "status":
                RECOVERY_OUTCOME_SNAPSHOT_FAILED,

            "action":
                ITERATION_STOP,

            "outcome":
                RECOVERY_OUTCOME_SNAPSHOT_FAILED,

            "reason":
                str(exc),

            "snapshot":
                None
        }

    snapshot_path = snapshot_relative_path(
        snapshot_dir
    )

    verified, verification = verify_recovery_snapshot(

        snapshot_dir,

        require_worktree_match=True
    )

    _logger.warning(
        "中断现场 snapshot: dir=%s verified=%s detail=%s",
        snapshot_path,
        verified,
        verification
    )

    event: dict[str, Any] = {

        "kind":
            RECOVERY_EVENT_WORKTREE,

        "decision":
            "auto_recover",

        "task_id":
            task_id,

        "attempt":
            attempt,

        "assessment":
            assessment,

        "dirty_files":
            list(dirty_lines),

        "tracked_files":
            tracked_files,

        "untracked_files":
            untracked_files,

        "snapshot":
            snapshot_path,

        "snapshot_verified":
            verified,

        "verification":
            verification,

        "cleaned":
            False,

        "failure_class":
            INTERRUPTED_FAILURE_CLASS,

        "failure_code":
            INTERRUPTED_FAILURE_CODE,

        "evidence": {
            key: state.get(key)
            for key in (
                "pid",
                "branch",
                "head",
                "started_at",
                "evidence_schema"
            )
        }
    }

    if not verified:

        record_recovery_event(

            {
                **event,

                "outcome":
                    RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED
            }
        )

        _logger.error(
            "中断现场 snapshot 校验失败（%s）："
            "严格停线，绝不清理工作区；snapshot=%s，请人工判断后再启动。",
            verification,
            snapshot_path
        )

        return {

            "status":
                RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED,

            "action":
                ITERATION_STOP,

            "outcome":
                RECOVERY_OUTCOME_SNAPSHOT_UNVERIFIED,

            "reason":
                verification,

            "snapshot":
                snapshot_path
        }

    try:

        reset_task_changes()

    except RuntimeError as exc:

        record_recovery_event(

            {
                **event,

                "outcome":
                    RECOVERY_OUTCOME_CLEANUP_FAILED,

                "reason":
                    str(exc)
            }
        )

        _logger.error(
            "中断现场清理失败（%s）：停线；snapshot 已保留在 %s。",
            exc,
            snapshot_path
        )

        return {

            "status":
                RECOVERY_OUTCOME_CLEANUP_FAILED,

            "action":
                ITERATION_STOP,

            "outcome":
                RECOVERY_OUTCOME_CLEANUP_FAILED,

            "reason":
                str(exc),

            "snapshot":
                snapshot_path
        }

    record_recovery_event(

        {
            **event,

            "outcome":
                RECOVERY_OUTCOME_RECOVERED,

            "cleaned":
                True,

            "cleaned_at":
                now_iso()
        }
    )

    # 证据一次性消费：中断现场已被 snapshot 记录并清理，
    # 必须让这份 `running` 证据失效，否则之后**同一条**证据会被
    # 用来清理另一次（可能来自人工）的 dirty 现场。
    write_task_state(

        task_id,

        "recovered",

        attempt=
            attempt,

        recovery_snapshot=
            snapshot_path,

        failure_class=
            INTERRUPTED_FAILURE_CLASS,

        failure_code=
            INTERRUPTED_FAILURE_CODE,

        recovered_at=
            now_iso()
    )

    _logger.warning(
        "中断任务现场已安全恢复: task=%s attempt=%s snapshot=%s "
        "verified=%s files=%s；同一 task 将重新从 attempt 1 开始"
        "（本轮不算完成任务，也不写 result）。",
        task_id,
        attempt,
        snapshot_path,
        verified,
        len(tracked_files) + len(untracked_files)
    )

    return {

        "status":
            RECOVERY_OUTCOME_RECOVERED,

        "action":
            ITERATION_CONTINUE,

        "outcome":
            RECOVERY_OUTCOME_RECOVERED,

        "reason":
            assessment,

        "snapshot":
            snapshot_path
    }


def recover_interrupted_worktree() -> dict[str, Any]:
    """单轮恢复状态机入口（Git sync 之前执行）。

    决策完全由确定性证据决定：
    - 干净 ⇒ 无需恢复；
    - 可证明属于上一次同一 task 的中断现场 ⇒ snapshot + 校验 + 清理 + 重试同一 task；
    - 其它 dirty ⇒ 严格停线（绝不自动 reset / clean），并留下 runtime 审计。

    这里**绝不调用 Cline、绝不询问 LLM、绝不规划下一任务**。
    """

    if not git_is_dirty():

        return {

            "status":
                RECOVERY_OUTCOME_CLEAN,

            "action":
                ITERATION_CONTINUE,

            "outcome":
                RECOVERY_OUTCOME_CLEAN,

            "reason":
                "worktree_clean"
        }

    dirty_lines = get_git_status_lines()

    ready_task_file, wait_reason = find_next_task_with_reason()

    ready_task_id = (
        ready_task_file.stem
        if ready_task_file is not None
        else None
    )

    (
        assessment,
        reason,
        state
    ) = interrupted_scene_assessment(
        ready_task_id
    )

    if (
        assessment
        ==
        WORKTREE_INTERRUPTED_TASK
        and
        state is not None
    ):

        _logger.warning(
            "检测到可证明的中断任务现场：%s",
            reason
        )

        return cleanup_interrupted_scene(

            ready_task_id
            or
            "",

            state,

            dirty_lines,

            reason
        )

    if should_log_worktree_block():

        _logger.error(
            "工作区存在无法证明属于「上一次同一 task 中断现场」的未提交修改："
            "严格停线，绝不自动 reset/clean，也绝不写入任何 result。reason=%s",
            reason
        )

        for line in dirty_lines:

            _logger.error(
                "  %s",
                line
            )

        _logger.error(
            "等待人工判断：若确认是上一次中断任务的现场，"
            "Orchestrator 会在证据齐全时自动 snapshot 恢复；"
            "其它情况请人工处理（手工提交 / 手工备份后再清理）。"
        )

        record_recovery_event(

            {
                "kind":
                    RECOVERY_EVENT_BLOCKED,

                "decision":
                    "manual_intervention",

                "outcome":
                    RECOVERY_OUTCOME_MANUAL,

                "task_id":
                    ready_task_id,

                "queue_wait_reason":
                    wait_reason,

                "reason":
                    reason,

                "dirty_files":
                    list(dirty_lines),

                "snapshot":
                    None,

                "snapshot_verified":
                    False,

                "cleaned":
                    False
            }
        )

    return {

        "status":
            RECOVERY_OUTCOME_MANUAL,

        "action":
            ITERATION_STOP,

        "outcome":
            RECOVERY_OUTCOME_MANUAL,

        "reason":
            reason
    }


# ============================================================
# Cline CLI
# ============================================================

def find_cline_executable():

    # Windows npm 全局 CLI
    #
    # 优先寻找：
    #
    # cline.cmd
    #
    if os.name == "nt":

        cline_exe = shutil.which(
            "cline.cmd"
        )

        if cline_exe:

            return cline_exe

    return shutil.which(
        "cline"
    )


# ============================================================
# Cline JSON 解析
# ============================================================

def parse_cline_json_output(
    stdout
):

    # 不保存 reasoning。
    #
    # 只保存 run_result / done 的最终信息。
    #
    summary = {

        "finish_reason":
            None,

        "final_text":
            "",

        "iterations":
            None,

        "duration_ms":
            None,

        "usage":
            None,

        "model":
            None
    }

    for raw_line in stdout.splitlines():

        line = (
            raw_line
            .strip()
        )

        if not line.startswith(
            "{"
        ):

            continue

        try:

            event = json.loads(
                line
            )

        except json.JSONDecodeError:

            continue

        event_type = event.get(
            "type"
        )

        # ----------------------------------------------------
        # run_result
        # ----------------------------------------------------

        if event_type == "run_result":

            summary[
                "finish_reason"
            ] = event.get(
                "finishReason"
            )

            summary[
                "final_text"
            ] = (
                event.get(
                    "text"
                )
                or
                ""
            )

            summary[
                "iterations"
            ] = event.get(
                "iterations"
            )

            summary[
                "duration_ms"
            ] = event.get(
                "durationMs"
            )

            summary[
                "usage"
            ] = (
                event.get(
                    "aggregateUsage"
                )
                or
                event.get(
                    "usage"
                )
            )

            model = event.get(
                "model"
            )

            if isinstance(
                model,
                dict
            ):

                summary[
                    "model"
                ] = model.get(
                    "id"
                )

            elif isinstance(
                model,
                str
            ):

                summary[
                    "model"
                ] = model

        # ----------------------------------------------------
        # done
        # ----------------------------------------------------

        elif (
            event_type
            ==
            "agent_event"
        ):

            inner = event.get(
                "event"
            )

            if (
                isinstance(
                    inner,
                    dict
                )
                and
                inner.get("type")
                ==
                "done"
            ):

                if not summary[
                    "final_text"
                ]:

                    summary[
                        "final_text"
                    ] = (
                        inner.get(
                            "text"
                        )
                        or
                        ""
                    )

                if (
                    summary[
                        "finish_reason"
                    ]
                    is None
                ):

                    summary[
                        "finish_reason"
                    ] = inner.get(
                        "reason"
                    )

                if (
                    summary[
                        "iterations"
                    ]
                    is None
                ):

                    summary[
                        "iterations"
                    ] = inner.get(
                        "iterations"
                    )

                if (
                    summary[
                        "usage"
                    ]
                    is None
                ):

                    summary[
                        "usage"
                    ] = inner.get(
                        "usage"
                    )

    return summary


# ============================================================
# 执行 Cline
# ============================================================

def run_cline(
    task_file
):

    cline_exe = (
        find_cline_executable()
    )

    if not cline_exe:

        raise RuntimeError(

            "找不到 Cline CLI，"
            "请先执行 "
            "npm install -g cline"
        )

    prompt = build_cline_prompt(
        task_file
    )

    args = build_cline_args(
        cline_exe,
        prompt
    )

    _logger.info(
        "Starting Cline..."
    )

    _logger.info(
        "Cline executable: %s",
        cline_exe
    )

    _logger.info(
        "Cline provider: %s",
        CLINE_PROVIDER
    )

    _logger.info(
        "Cline model: %s",
        (
            CLINE_MODEL
            or
            "<provider default>"
        )
    )

    _logger.info(
        "Task file: %s",
        task_file.relative_to(
            ROOT
        )
    )

    try:

        # ====================================================
        # Windows
        # ====================================================
        #
        # 这里保留我们已经实际验证成功的方案：
        #
        # Python
        #   ↓
        # Windows Shell
        #   ↓
        # cline.cmd
        #   ↓
        # Cline headless
        #
        # 不使用 stdin pipe。
        #
        if os.name == "nt":

            command_line = (
                subprocess
                .list2cmdline(
                    args
                )
            )

            result = subprocess.run(

                command_line,

                cwd=ROOT,

                shell=True,

                capture_output=True,

                text=True,

                encoding="utf-8",

                errors="replace",

                timeout=(
                    CLINE_TIMEOUT_SECONDS
                    +
                    60
                )
            )

        # ====================================================
        # Linux / macOS
        # ====================================================
        else:

            result = subprocess.run(

                args,

                cwd=ROOT,

                capture_output=True,

                text=True,

                encoding="utf-8",

                errors="replace",

                timeout=(
                    CLINE_TIMEOUT_SECONDS
                    +
                    60
                )
            )

        parsed = (
            parse_cline_json_output(
                result.stdout
            )
        )

        return {

            "returncode":
                result.returncode,

            "stdout":
                result.stdout,

            "stderr":
                result.stderr,

            "timed_out":
                False,

            "summary":
                parsed
        }

    except subprocess.TimeoutExpired as exc:

        stdout = (
            exc.stdout
            or
            ""
        )

        stderr = (
            exc.stderr
            or
            ""
        )

        if isinstance(
            stdout,
            bytes
        ):

            stdout = stdout.decode(
                "utf-8",
                errors="replace"
            )

        if isinstance(
            stderr,
            bytes
        ):

            stderr = stderr.decode(
                "utf-8",
                errors="replace"
            )

        return {

            "returncode":
                124,

            "stdout":
                stdout,

            "stderr":
                (
                    stderr
                    +
                    "\nCline process timed out."
                ),

            "timed_out":
                True,

            "summary":
                parse_cline_json_output(
                    stdout
                )
        }


# ============================================================
# Validation
# ============================================================

def run_validations(
    task
):

    commands = task.get(
        "validation_commands",
        []
    )

    results = []

    for command in commands:

        _logger.info(
            "Validation: %s",
            command
        )

        result = run_command(

            command,

            shell=True,

            timeout=(
                CLINE_TIMEOUT_SECONDS
            )
        )

        results.append({

            "command":
                command,

            "returncode":
                result[
                    "returncode"
                ],

            "timed_out":
                result[
                    "timed_out"
                ],

            "stdout_tail":
                result[
                    "stdout"
                ][-5000:],

            "stderr_tail":
                result[
                    "stderr"
                ][-5000:]
        })

    return results


def validations_passed(
    validations
):

    return all(

        item[
            "returncode"
        ]
        == 0

        for item
        in validations
    )


# ============================================================
# Git Diff
# ============================================================

def get_changed_files():

    result = git(
        "status --short"
    )

    if (
        result["returncode"]
        != 0
    ):

        return []

    return [

        line

        for line
        in result["stdout"].splitlines()

        if line.strip()
    ]


def get_diff_stat():

    result = git(
        "diff --stat"
    )

    if (
        result["returncode"]
        == 0
    ):

        return result[
            "stdout"
        ]

    return ""


# ============================================================
# 失败回滚
# ============================================================

def reset_task_changes():

    _logger.warning(
        "回滚本次失败尝试产生的代码修改..."
    )

    reset = git(
        "reset --hard HEAD"
    )

    if (
        reset["returncode"]
        != 0
    ):

        raise RuntimeError(

            "git reset --hard HEAD 失败: "

            + (
                reset["stderr"].strip()
                or
                reset["stdout"].strip()
            )
        )

    # 删除本轮 Cline 新建、
    # 但尚未进入 Git 的文件。
    #
    # .gitignore 中的 runtime/logs
    # 不会被删除。
    clean = git(
        "clean -fd"
    )

    if (
        clean["returncode"]
        != 0
    ):

        raise RuntimeError(

            "git clean -fd 失败: "

            + (
                clean["stderr"].strip()
                or
                clean["stdout"].strip()
            )
        )


# ============================================================
# Attempt Result
# ============================================================

def normalize_finish_reason(
    execution_outcome
):
    """把 Orchestrator 判定归一化成稳定的 finish reason。

    只输出 Orchestrator 词表内的值，绝不复制 Cline raw finish reason。
    """

    if execution_outcome in NORMALIZED_FINISH_REASONS:

        return execution_outcome

    return EXECUTION_OUTCOME_FAILED


def cline_finish_reason_raw(
    cline_result
):
    """Cline CLI 自报的原始 finish reason（审计用，可能是 aborted）。"""

    summary = (
        cline_result.get(
            "summary"
        )
        or
        {}
    )

    return summary.get(
        "finish_reason"
    )


def attempt_outcome(
    cline_result,
    validations
):
    """attempt 级 Orchestrator 判定，与最终 success 判定同源。

    成功条件与旧实现完全一致：
    Cline returncode == 0 且全部 validation returncode == 0。
    """

    if (
        cline_result.get(
            "returncode"
        )
        !=
        0
    ):

        return EXECUTION_OUTCOME_FAILED

    if validations_passed(
        validations
    ):

        return EXECUTION_OUTCOME_COMPLETED

    return EXECUTION_OUTCOME_FAILED


def build_attempt_record(
    attempt,
    started_at,
    finished_at,
    cline_result,
    validations,
    changed_files,
    diff_stat,
    failure_class="none",
    failure_code=None,
    recovery_path=None,
    execution_outcome=EXECUTION_OUTCOME_FAILED
):

    summary = (
        cline_result.get(
            "summary"
        )
        or
        {}
    )

    return {

        "attempt":
            attempt,

        "started_at":
            started_at,

        "finished_at":
            finished_at,

        "cline_exit_code":
            cline_result[
                "returncode"
            ],

        "cline_timed_out":
            cline_result.get(
                "timed_out",
                False
            ),

        "failure_class":
            failure_class,

        "failure_code":
            failure_code,

        "recovery_path":
            recovery_path,

        # Orchestrator 判定（新语义，GOLD-020）。
        "execution_outcome":
            execution_outcome,

        "normalized_finish_reason":
            normalize_finish_reason(
                execution_outcome
            ),

        # 兼容旧键：新结果写入归一化判定值，
        # 保证成功任务不会再出现「唯一 finish_reason = aborted」的歧义。
        "finish_reason":
            normalize_finish_reason(
                execution_outcome
            ),

        # Cline raw finish reason 原文（只读审计用，可能是 aborted）。
        "cline_finish_reason_raw":
            cline_finish_reason_raw(
                cline_result
            ),

        "model":
            summary.get(
                "model"
            ),

        "iterations":
            summary.get(
                "iterations"
            ),

        "duration_ms":
            summary.get(
                "duration_ms"
            ),

        "usage":
            summary.get(
                "usage"
            ),

        "cline_final_text":
            summary.get(
                "final_text",
                ""
            ),

        "cline_error_tail":
            cline_result.get(
                "stderr",
                ""
            )[-5000:],

        "changed_files":
            changed_files,

        "git_diff_stat":
            diff_stat,

        "validations":
            validations
    }


# ============================================================
# 最终结果
# ============================================================

def write_final_result(
    task,
    status,
    attempts,
    changed_files=None,
    diff_stat="",
    note=None,
    execution_outcome=None
):

    task_id = (
        task["task_id"]
    )

    outcome = (
        execution_outcome
        or
        status
    )

    result = {

        "task_id":
            task_id,

        "title":
            task.get(
                "title",
                ""
            ),

        "status":
            status,

        # Orchestrator 判定（新语义，GOLD-020）：
        # execution_outcome / normalized_finish_reason 必须与 status 一致；
        # Cline raw finish reason 只保留在 attempt.cline_finish_reason_raw。
        "execution_outcome":
            outcome,

        "normalized_finish_reason":
            normalize_finish_reason(
                outcome
            ),

        "finished_at":
            now_iso(),

        "attempt_count":
            len(
                attempts
            ),

        "max_attempts":
            get_task_max_attempts(
                task
            ),

        "changed_files":
            (
                changed_files
                or
                []
            ),

        "git_diff_stat":
            diff_stat,

        "attempts":
            attempts
    }

    if note:

        result[
            "note"
        ] = note

    atomic_write_json(
        result_path(task_id),
        result
    )

    return result


# ============================================================
# Commit + Push
# ============================================================

def commit_task_result(
    task,
    status
):

    task_id = (
        task["task_id"]
    )

    # 任务开始前工作区一定是 clean。
    #
    # 所以这里的修改理论上全部来自：
    #
    # Cline
    # +
    # result.json
    #
    git(
        "add -A"
    )

    if status == "completed":

        commit_message = (
            f"ai: complete "
            f"{task_id}"
        )

    else:

        commit_message = (
            f"ai: blocked "
            f"{task_id}"
        )

    commit = git(
        f'commit -m "{commit_message}"'
    )

    if (
        commit["returncode"]
        != 0
    ):

        message = (
            commit["stderr"].strip()
            or
            commit["stdout"].strip()
        )

        _logger.error(
            "Git commit 失败: %s",
            message
        )

        return False

    _logger.info(
        "Git commit 完成: %s",
        commit_message
    )

    # --------------------------------------------------------
    # push 失败时：
    #
    # 不回滚 commit。
    #
    # 下一轮：
    #
    # sync_repository()
    #
    # 会继续 retry push。
    #
    # 因为 result 已经处于 terminal 状态，
    # 所以不会重新执行 Cline。
    # --------------------------------------------------------

    if not push_pending_commits():

        _logger.warning(

            "任务已经完成并提交到本地 Git，"
            "但尚未推送成功。"

            "下轮会优先重试 push，"
            "不会重新执行任务。"
        )

        return False

    return True


# ============================================================
# 执行 Task
# ============================================================

def process_task(
    task_file
):

    task = load_task(
        task_file
    )

    task_id = (
        task["task_id"]
    )

    max_attempts = (
        get_task_max_attempts(
            task
        )
    )

    existing_result = read_json(
        result_path(task_id),
        default=None
    )

    prior_attempts = []

    # ========================================================
    # V2 result
    # ========================================================

    if (
        existing_result
        and
        isinstance(
            existing_result.get(
                "attempts"
            ),
            list
        )
    ):

        prior_attempts = (
            existing_result[
                "attempts"
            ]
        )

    # ========================================================
    # 兼容 V1 failed result
    # ========================================================

    elif (
        existing_result
        and
        existing_result.get(
            "status"
        )
        ==
        "failed"
    ):

        prior_attempts = [

            {
                "attempt":
                    1,

                "finished_at":
                    existing_result.get(
                        "finished_at"
                    ),

                "cline_exit_code":
                    existing_result.get(
                        "cline_exit_code"
                    ),

                "cline_error_tail":
                    existing_result.get(
                        "cline_error_tail",
                        ""
                    ),

                "migrated_from_v1":
                    True
            }
        ]

    start_attempt = (
        len(
            prior_attempts
        )
        +
        1
    )

    # ========================================================
    # 已达到重试上限
    # ========================================================

    if (
        start_attempt
        >
        max_attempts
    ):

        _logger.error(

            "Task %s 已达到"
            "最大尝试次数 %s，"
            "标记为 blocked。",

            task_id,
            max_attempts
        )

        if git_is_dirty():

            # 只有能证明「属于本 task 中断现场」时才允许清理；
            # 证据不足一律 fail-closed 停线，绝不 reset/clean 未知修改。
            (
                dirty_assessment,
                dirty_reason,
                dirty_state
            ) = interrupted_scene_assessment(
                task_id
            )

            if (
                dirty_assessment
                !=
                WORKTREE_INTERRUPTED_TASK
                or
                dirty_state is None
            ):

                _logger.error(
                    "Task %s 达到最大尝试次数，但工作区存在无法证明"
                    "属于本任务中断现场的修改（%s）："
                    "严格停线，绝不自动 reset/clean，也不写 result。",
                    task_id,
                    dirty_reason
                )

                record_recovery_event(

                    {
                        "kind":
                            RECOVERY_EVENT_BLOCKED,

                        "decision":
                            "manual_intervention",

                        "outcome":
                            RECOVERY_OUTCOME_MANUAL,

                        "context":
                            "max_attempts_reached",

                        "task_id":
                            task_id,

                        "reason":
                            dirty_reason,

                        "dirty_files":
                            get_git_status_lines(),

                        "cleaned":
                            False
                    }
                )

                return "deferred"

            reset_task_changes()

        write_final_result(

            task,

            "blocked",

            prior_attempts,

            note=(
                "Maximum retry count reached."
            )
        )

        commit_task_result(
            task,
            "blocked"
        )

        clear_task_state(
            task_id
        )

        return "blocked"

    # ========================================================
    # 输出任务信息
    # ========================================================

    _logger.info(
        "=" * 60
    )

    _logger.info(
        "Task: %s",
        task_id
    )

    _logger.info(
        "%s",
        task.get(
            "title",
            ""
        )
    )

    _logger.info(
        "Max attempts: %s",
        max_attempts
    )

    _logger.info(
        "=" * 60
    )

    # ========================================================
    # 最后安全检查
    # ========================================================

    if git_is_dirty():

        _logger.warning(

            "工作区存在未提交修改，"
            "为了避免覆盖人工代码，"
            "本轮停止。"
        )

        for line in get_git_status_lines():

            _logger.warning(
                "  %s",
                line
            )

        return "deferred"

    attempts = list(
        prior_attempts
    )

    # ========================================================
    # Retry Loop
    # ========================================================

    for attempt in range(
        start_attempt,
        max_attempts + 1
    ):

        started_at = now_iso()

        write_task_state(

            task_id,

            "running",

            attempt=
                attempt,

            max_attempts=
                max_attempts,

            started_at=
                started_at,

            # 现场基线证据（GOLD-022）：只有能证明「dirty 变化发生在
            # 这次 attempt 期间」时才允许中断后自动 snapshot + 清理。
            **current_attempt_baseline()
        )

        _logger.info(

            "执行 Task %s，"
            "attempt %s/%s",

            task_id,
            attempt,
            max_attempts
        )

        # ----------------------------------------------------
        # Cline
        # ----------------------------------------------------

        cline_result = run_cline(
            task_file
        )

        (
            failure_class,
            failure_code
        ) = classify_cline_failure(
            cline_result
        )

        # ----------------------------------------------------
        # Provider / Billing / Auth fail-fast
        # ----------------------------------------------------

        if (
            failure_class
            ==
            "non_retryable_external"
        ):

            changed_files = (
                get_changed_files()
            )

            diff_stat = (
                get_diff_stat()
            )

            recovery_path = None

            if git_is_dirty():

                snapshot_dir = (
                    save_recovery_snapshot(
                        task_id,
                        attempt,
                        failure_class,
                        failure_code
                    )
                )

                recovery_path = str(
                    snapshot_dir
                    .relative_to(
                        ROOT
                    )
                )

                _logger.error(
                    "Cline 外部不可重试错误 %s；"
                    "已保存恢复现场: %s",
                    failure_code,
                    recovery_path
                )

                reset_task_changes()

            else:

                _logger.error(
                    "Cline 外部不可重试错误 %s；"
                    "工作区无任务修改。",
                    failure_code
                )

            finished_at = now_iso()

            write_task_state(
                task_id,
                "waiting_external",
                attempt=
                    attempt,
                max_attempts=
                    max_attempts,
                finished_at=
                    finished_at,
                failure_class=
                    failure_class,
                failure_code=
                    failure_code,
                recovery_path=
                    recovery_path
            )

            _logger.error(
                "Task %s 暂停："
                "不运行 validation、"
                "不消耗后续 retry、"
                "rolling queue 停线。",
                task_id
            )

            return "waiting_external"

        # ----------------------------------------------------
        # Validation
        # ----------------------------------------------------

        # Cline 进程本身失败时成功条件已经不可能成立，
        # 不再浪费数分钟执行全量测试。
        if (
            cline_result[
                "returncode"
            ]
            ==
            0
        ):

            validations = run_validations(
                task
            )

        else:

            validations = []

            _logger.warning(
                "Cline 执行失败 (%s/%s)，"
                "跳过 validation。",
                failure_class,
                failure_code
            )

        # ----------------------------------------------------
        # Git diff
        # ----------------------------------------------------

        changed_files = (
            get_changed_files()
        )

        diff_stat = (
            get_diff_stat()
        )

        finished_at = now_iso()

        # ====================================================
        # 判断成功（唯一判定源，GOLD-020）
        # ====================================================

        outcome = attempt_outcome(

            cline_result,

            validations
        )

        success = (
            outcome
            ==
            EXECUTION_OUTCOME_COMPLETED
        )

        # ----------------------------------------------------
        # Attempt 记录
        # ----------------------------------------------------
        #
        # execution_outcome / normalized_finish_reason 与上面的 success
        # 完全同源；Cline raw finish reason 只单独保存在
        # cline_finish_reason_raw，不再冒充 Orchestrator 判定。

        attempt_record = (
            build_attempt_record(

                attempt=
                    attempt,

                started_at=
                    started_at,

                finished_at=
                    finished_at,

                cline_result=
                    cline_result,

                validations=
                    validations,

                changed_files=
                    changed_files,

                diff_stat=
                    diff_stat,

                failure_class=
                    failure_class,

                failure_code=
                    failure_code,

                execution_outcome=
                    outcome
            )
        )

        attempts.append(
            attempt_record
        )

        # ====================================================
        # SUCCESS
        # ====================================================

        if success:

            write_task_state(

                task_id,

                "completed",

                attempt=
                    attempt,

                max_attempts=
                    max_attempts,

                finished_at=
                    finished_at
            )

            write_final_result(

                task,

                "completed",

                attempts,

                changed_files=
                    changed_files,

                diff_stat=
                    diff_stat,

                execution_outcome=
                    EXECUTION_OUTCOME_COMPLETED
            )

            _logger.info(
                "Task %s: completed locally, "
                "result 已写入本地终态。",
                task_id
            )

            pushed = (
                commit_task_result(

                    task,

                    "completed"
                )
            )

            clear_task_state(
                task_id
            )

            if pushed:

                _logger.info(
                    "Task %s: remote synced (push ok); "
                    "rolling queue continue.",
                    task_id
                )

                # ================================================
                # GPT Planner 低水位提示（GOLD-035）
                # ================================================
                #
                # 「任务完成 + commit + push 全部成功（远端可见）」之后，
                # 立即用同一份只读事实包重算「当前任务之外还剩几个已批准
                # follow-on」；不足 lookahead_target 时明确请求 GPT 补队列，
                # 而不是等下一次断粮才发现。
                #
                # 只报告：绝不生成任务、绝不改状态、绝不跨越 Gate，
                # 也绝不阻塞下面 rolling queue 的推进。

                planner_refill_report(
                    context=REFILL_HINT_CONTEXT_POST_PUSH,
                    only_when_required=True,
                    throttle=False
                )

                return "completed"

            # ====================================================
            # push 失败：completed locally + push pending
            # ====================================================
            #
            # 这是 Git 同步问题，不是 validation 失败：
            # 严禁重跑 Cline，严禁 force push / reset，
            # 本地 commit 与 result 一律保留。
            write_task_state(

                task_id,

                "push_pending",

                attempt=
                    attempt,

                max_attempts=
                    max_attempts,

                finished_at=
                    finished_at,

                execution_outcome=
                    EXECUTION_OUTCOME_COMPLETED,

                local_result=
                    "completed",

                push_status=
                    "pending",

                remote=
                    REMOTE_NAME,

                branch=
                    REQUIRED_BRANCH
            )

            _logger.warning(

                "Task %s: completed locally, "
                "push pending. "

                "本地 commit/result 保留，"
                "不会重新执行 Cline；"

                "下一轮先 Git sync/rebase + retry push，"
                "只有同步成功后才允许 rolling queue 继续。",

                task_id
            )

            return "push_pending"

        # ====================================================
        # FAILED
        # ====================================================

        _logger.warning(

            "Task %s "
            "attempt %s/%s 失败。",

            task_id,
            attempt,
            max_attempts
        )

        stderr = (
            cline_result
            .get(
                "stderr",
                ""
            )
            .strip()
        )

        if stderr:

            _logger.warning(

                "Cline stderr: %s",

                stderr[
                    -1500:
                ]
            )

        failed_validations = [

            item[
                "command"
            ]

            for item
            in validations

            if (
                item[
                    "returncode"
                ]
                !=
                0
            )
        ]

        if failed_validations:

            _logger.warning(

                "失败的 validation: %s",

                ", ".join(
                    failed_validations
                )
            )

        recovery_path = None

        if git_is_dirty():

            snapshot_dir = (
                save_recovery_snapshot(
                    task_id,
                    attempt,
                    failure_class,
                    failure_code
                )
            )

            recovery_path = str(
                snapshot_dir
                .relative_to(
                    ROOT
                )
            )

            _logger.info(
                "失败现场已保存: %s",
                recovery_path
            )

        write_task_state(

            task_id,

            "failed",

            attempt=
                attempt,

            max_attempts=
                max_attempts,

            finished_at=
                finished_at,

            failure_class=
                failure_class,

            failure_code=
                failure_code,

            recovery_path=
                recovery_path
        )

        # ----------------------------------------------------
        # 失败 attempt 不能污染下一次 retry
        # ----------------------------------------------------

        if git_is_dirty():

            reset_task_changes()

        # ====================================================
        # RETRY
        # ====================================================

        if (
            attempt
            <
            max_attempts
        ):

            wait_seconds = min(
                POLL_SECONDS,
                10
            )

            _logger.info(

                "将在 %s 秒后"
                "重试 Task %s。",

                wait_seconds,
                task_id
            )

            time.sleep(
                wait_seconds
            )

            continue

        # ====================================================
        # BLOCKED
        # ====================================================

        write_task_state(

            task_id,

            "blocked",

            attempt=
                attempt,

            max_attempts=
                max_attempts,

            finished_at=
                finished_at
        )

        write_final_result(

            task,

            "blocked",

            attempts,

            note=(
                "Task failed after "
                "maximum retry count."
            )
        )

        commit_task_result(
            task,
            "blocked"
        )

        clear_task_state(
            task_id
        )

        _logger.error(

            "Task %s: blocked "
            "after %s attempts",

            task_id,
            max_attempts
        )

        return "blocked"


# ============================================================
# 单实例锁
# ============================================================

def running_process_image(
    pid: int
) -> str | None:
    """返回 PID 对应进程的映像名（仅 Windows 可查）。

    - 探测成功且命中 ⇒ 返回映像名；
    - 探测成功但没有该 PID ⇒ 返回 None（**确实不存在**）；
    - 探测本身失败（tasklist 缺失 / 超时 / 非 0 退出）⇒ 抛 `OSError`，
      调用方必须 fail-closed，绝不把它当成「进程不存在」。
    """

    if os.name != "nt":

        return None

    try:

        result = subprocess.run(

            [
                "tasklist",

                "/FI",

                f"PID eq {pid}",

                "/NH"
            ],

            capture_output=True,

            text=True,

            encoding="utf-8",

            errors="replace",

            timeout=30
        )

    except (
        OSError,
        subprocess.SubprocessError
    ) as exc:

        raise OSError(f"tasklist 探测失败: {exc}") from exc

    if result.returncode != 0:

        raise OSError(
            f"tasklist 返回 {result.returncode}: {result.stderr.strip()}"
        )

    for line in result.stdout.splitlines():

        parts = line.split()

        # 形态：`python.exe  12345  Console  1  123,456 K`
        # 只有 PID 列**精确相等**才算命中（避免子串误判）。
        if (
            len(parts) >= 2
            and
            parts[1].isdigit()
            and
            int(parts[1]) == pid
        ):

            return parts[0]

    return None


def is_pid_running(
    pid
) -> bool:
    """判断 PID 是否仍在运行（fail-closed：无法判断时按「存活」处理）。"""

    if (
        not pid
        or
        pid <= 0
    ):

        return False

    if (
        pid
        ==
        os.getpid()
    ):

        return True

    # ========================================================
    # Windows
    # ========================================================

    if os.name == "nt":

        try:

            if (
                running_process_image(
                    pid
                )
                is not None
            ):

                return True

        except OSError as exc:

            # 无法证明 owner 不存在 ⇒ 必须按存活处理（绝不放行清理）。
            _logger.warning(
                "无法确认 PID %s 是否存活（%s）：按存活处理。",
                pid,
                exc
            )

            return True

        return False

    # ========================================================
    # Linux / macOS
    # ========================================================

    try:

        os.kill(
            pid,
            0
        )

    except ProcessLookupError:

        return False

    except OSError:

        # 权限不足 / 其它异常同样无法证明进程不存在 ⇒ 按存活处理。
        return True

    return True


def read_lock_evidence() -> tuple[dict[str, Any] | None, str]:
    """读取 lock 文件并校验**来源**（fail-closed）。

    返回 `(payload, reason)`：

    - `payload` 非 None ⇒ 来源可证明属于本项目（project 一致 + pid 可用 + started_at 齐全）；
    - `payload` 为 None ⇒ 来源不明（损坏 / 缺字段 / 非本项目 / schema 不认识），
      调用方必须停线，**绝不自动删除**。
    """

    if not LOCK_FILE.exists():

        return None, "lock file missing"

    payload = read_json(
        LOCK_FILE,
        default=None
    )

    if not isinstance(payload, dict):

        return None, "lock payload is not a readable JSON object"

    schema = payload.get("schema")

    # 旧版 lock 没有 schema 字段：其余证据齐全时仍可判定；
    # 一旦出现**不认识**的 schema，来源不明，必须停线。
    if (
        schema is not None
        and
        schema != LOCK_SCHEMA
    ):

        return None, f"lock schema unknown: {schema!r}"

    project = payload.get("project")

    if (
        not isinstance(project, str)
        or
        not project.strip()
    ):

        return None, "lock has no project owner"

    try:

        owned = (
            Path(project).resolve()
            ==
            ROOT.resolve()
        )

    except OSError:

        return None, f"lock project path is unusable: {project}"

    if not owned:

        return None, f"lock belongs to another project: {project}"

    started_at = payload.get("started_at")

    if (
        not isinstance(started_at, str)
        or
        not started_at.strip()
    ):

        return None, "lock has no started_at timestamp"

    raw_pid = payload.get("pid")

    if isinstance(raw_pid, bool):

        return None, f"lock has no usable pid: {raw_pid!r}"

    try:

        pid = int(raw_pid)

    except (
        TypeError,
        ValueError
    ):

        return None, f"lock has no usable pid: {raw_pid!r}"

    if pid <= 0:

        return None, f"lock has no usable pid: {raw_pid!r}"

    evidence = dict(payload)

    evidence["pid"] = pid

    return evidence, "lock provenance verified"


def classify_lock_state() -> tuple[str, str, dict[str, Any] | None]:
    """把 lock 分类成 active / stale / unknown（fail-closed）。

    - `active`：owner PID 仍在运行 ⇒ 停线，绝不删除；
    - `unknown`：来源不明 ⇒ 停线，绝不删除，要求人工确认；
    - `stale`：来源可证明且 owner PID 已不存在 ⇒ 才允许归档 + 清理。
    """

    payload, reason = read_lock_evidence()

    if payload is None:

        return LOCK_UNKNOWN, reason, None

    pid = payload["pid"]

    if pid == os.getpid():

        return (
            LOCK_ACTIVE,
            f"lock is held by the current process (pid={pid})",
            payload
        )

    if is_pid_running(pid):

        try:

            image = (
                running_process_image(pid)
                or
                "unknown process"
            )

        except OSError as exc:

            image = f"unknown process ({exc})"

        return (
            LOCK_ACTIVE,
            f"lock owner pid={pid} is still running ({image})",
            payload
        )

    return (
        LOCK_STALE,
        f"lock owner pid={pid} is not running "
        "(provably stale lock, provenance verified)",
        payload
    )


def archive_stale_lock(
    payload: dict[str, Any]
) -> str | None:
    """把陈旧 lock 原文归档到 runtime（审计留痕，绝不进 Git）。

    归档失败只告警，不阻止清理——「可清理」的证据来自 PID 判活，
    与归档成败无关。
    """

    try:

        RECOVERY_LOCK_ARCHIVE_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        stamp = (
            datetime
            .now()
            .astimezone()
            .strftime("%Y%m%dT%H%M%S%z")
        )

        destination = (
            RECOVERY_LOCK_ARCHIVE_DIR
            /
            f"orchestrator.lock.stale-{stamp}.json"
        )

        atomic_write_json(
            destination,
            payload
        )

    except (
        OSError,
        TypeError,
        ValueError
    ) as exc:

        _logger.error(
            "陈旧 lock 归档失败（仍继续清理，审计留痕缺失）: %s",
            exc
        )

        return None

    return snapshot_relative_path(
        destination
    )


def acquire_lock():

    global _lock_owned

    ensure_directories()

    # ========================================================
    # 已存在 Lock
    # ========================================================

    if LOCK_FILE.exists():

        state, reason, payload = classify_lock_state()

        # ----------------------------------------------------
        # 来源不明：停线（绝不猜测、绝不自动删除）
        # ----------------------------------------------------

        if state == LOCK_UNKNOWN:

            message = (

                "发现来源不明的 lock 文件，严格停线，绝不自动删除："

                f"{LOCK_FILE}（{reason}）。"

                "请人工确认没有其它 Orchestrator 正在运行后，"

                "手动删除该 lock 并重启 start_agent.bat。"
            )

            _logger.error(
                "%s",
                message
            )

            record_recovery_event(

                {
                    "kind":
                        RECOVERY_EVENT_LOCK,

                    "decision":
                        "manual_intervention",

                    "outcome":
                        RECOVERY_OUTCOME_MANUAL,

                    "lock":
                        str(LOCK_FILE),

                    "reason":
                        reason,

                    "cleaned":
                        False
                }
            )

            raise RuntimeError(
                message
            )

        # ----------------------------------------------------
        # 另一个 Orchestrator 正在运行：停线
        # ----------------------------------------------------

        if state == LOCK_ACTIVE:

            raise RuntimeError(
                f"已有 Orchestrator 正在运行（{reason}）。"
            )

        # ----------------------------------------------------
        # 陈旧 lock：能证明无活动 owner ⇒ 归档留痕后清理
        # ----------------------------------------------------

        archived = archive_stale_lock(
            payload
            or
            {}
        )

        try:

            LOCK_FILE.unlink()

        except OSError as exc:

            raise RuntimeError(

                "无法清理陈旧 "
                f"lock 文件: {exc}"
            ) from exc

        record_recovery_event(

            {
                "kind":
                    RECOVERY_EVENT_LOCK,

                "decision":
                    "auto_recover",

                "outcome":
                    RECOVERY_OUTCOME_RECOVERED,

                "lock":
                    str(LOCK_FILE),

                "reason":
                    reason,

                "archive":
                    archived,

                "evidence":
                    payload,

                "cleaned":
                    True,

                "cleaned_at":
                    now_iso()
            }
        )

        _logger.warning(
            "陈旧 lock 已清理（已证明 owner 进程不存在）：%s；归档=%s",
            reason,
            archived
            or
            "未归档"
        )

    # ========================================================
    # 创建 Lock
    # ========================================================

    fd = os.open(

        LOCK_FILE,

        os.O_CREAT
        |
        os.O_EXCL
        |
        os.O_WRONLY
    )

    try:

        data = json.dumps(

            {
                "schema":
                    LOCK_SCHEMA,

                "pid":
                    os.getpid(),

                "started_at":
                    now_iso(),

                "project":
                    str(ROOT)
            },

            ensure_ascii=False,

            indent=2
        )

        os.write(
            fd,
            data.encode(
                "utf-8"
            )
        )

    finally:

        os.close(
            fd
        )

    _lock_owned = True


def release_lock():

    global _lock_owned

    if not _lock_owned:

        return

    try:

        if LOCK_FILE.exists():

            LOCK_FILE.unlink()

    except OSError:

        pass

    _lock_owned = False


# ============================================================
# 可中断 Sleep
# ============================================================

def sleep_interruptibly(
    seconds
):

    end = (
        time.monotonic()
        +
        seconds
    )

    while True:

        remaining = (
            end
            -
            time.monotonic()
        )

        if (
            remaining
            <=
            0
        ):

            return

        time.sleep(

            min(
                remaining,
                1
            )
        )


# ============================================================
# 单轮循环（GOLD-020）
# ============================================================

def run_iteration(
    last_idle_log_at,
    refill_hint_state=None
):
    """执行一轮 Orchestrator 循环，返回本轮动作。

    顺序：
    0) 异常中断现场恢复（GOLD-022）：dirty worktree 只有在能被证明是
       「上一次同一 task 的中断现场」时才 snapshot + 校验 + 清理；
       否则严格停线；
    1) 先 Git sync（pull --rebase + retry push pending commits）；
    2) 同步失败 => fail-closed：本轮不执行任何任务
       （completed locally + push pending 时绝不重跑 Cline，
        本地 commit/result 一律保留）；
    3) 只有 remote synced 之后，rolling queue 才允许检查/执行下一个 task。

    另外（GOLD-035）：idle / no-runnable 路径会用**只读** refill 事实包提示
    `GPT_PLANNER_REFILL_REQUIRED`（队列足量时是 `..._SATISFIED`），提示受
    `REFILL_HINT_SECONDS` 节流保护，跨轮状态通过返回值的
    ``refill_hint_state`` 传递。提示只报告，绝不改变任何停线 / 执行判定。
    """

    # ========================================================
    # 异常中断现场恢复（GOLD-022）
    # ========================================================
    #
    # 必须在 Git sync 之前判定 dirty worktree 的来源，
    # 否则会永久卡在「dirty ⇒ 不同步 ⇒ 不恢复」的死循环里。
    # 该判定是纯确定性代码：绝不调用 Cline，也绝不让 LLM 决定丢弃现场。

    recovery = recover_interrupted_worktree()

    if recovery["action"] == ITERATION_STOP:

        return {

            "action":
                ITERATION_SLEEP,

            "outcome":
                recovery["outcome"],

            "last_idle_log_at":
                last_idle_log_at,

            "refill_hint_state":
                refill_hint_state
        }

    # ========================================================
    # Git Sync
    # ========================================================

    if not sync_repository():

        _logger.warning(

            "Git sync 未完成（push pending / pull-rebase 失败）："

            "本轮不执行任何任务，"

            "本地 commit/result 保留，"

            "下一轮先恢复远端同步。"
        )

        return {

            "action":
                ITERATION_SLEEP,

            "outcome":
                "sync_pending",

            "last_idle_log_at":
                last_idle_log_at,

            "refill_hint_state":
                refill_hint_state
        }

    # ========================================================
    # Task
    # ========================================================

    (
        task_file,
        wait_reason
    ) = find_next_task_with_reason()

    if task_file:

        outcome = process_task(
            task_file
        )

        if outcome == "completed":

            _logger.info(
                "Rolling queue: remote synced, "
                "checking next task immediately."
            )

            return {

                "action":
                    ITERATION_CONTINUE,

                "outcome":
                    outcome,

                "last_idle_log_at":
                    None,

                # 刚完成一个 task：事实已变，下一轮 idle 时允许立即重新提示。
                "refill_hint_state":
                    None
            }

        if outcome == "waiting_external":

            _logger.error(
                "Orchestrator 因外部 Provider "
                "不可重试错误停止。"
                "修复余额/认证/配额后重新运行 "
                "start_agent.bat 即可从同一 Task 重试。"
            )

            return {

                "action":
                    ITERATION_STOP,

                "outcome":
                    outcome,

                "last_idle_log_at":
                    None,

                "refill_hint_state":
                    None
            }

        # push_pending / failed / blocked / deferred：
        # 一律 sleeping 后重来，下一轮第一步仍是 Git sync。
        return {

            "action":
                ITERATION_SLEEP,

            "outcome":
                outcome,

            "last_idle_log_at":
                None,

            # 任务 outcome 会改变队列事实：允许下一轮 idle 立即重新提示。
            "refill_hint_state":
                None
        }


    now = time.monotonic()

    if should_log_idle(
        last_idle_log_at,
        current_time=now
    ):

        _logger.info(
            queue_diagnostic(
                wait_reason
            )
        )

        last_idle_log_at = now

    # ========================================================
    # GPT Planner 低水位提示（GOLD-035）
    # ========================================================
    #
    # idle / no-runnable 同样必须在队列**耗尽之前**看到「需要补队列」，
    # 而不是等 `No runnable task` 反复出现才发现断粮。
    #
    # 只报告：绝不生成任务、绝不补队列、绝不改状态，也绝不绕过
    # blocked / human-gated 的 queue head（上面的停线判定完全不变）。
    # 提示本身去重 / 节流，避免每 20 秒刷屏；事实一变立即重新提示。

    refill_hint_state = planner_refill_idle_hint(
        refill_hint_state,
        current_time=now
    )

    return {

        "action":
            ITERATION_SLEEP,

        "outcome":
            "idle",

        "last_idle_log_at":
            last_idle_log_at,

        "refill_hint_state":
            refill_hint_state
    }


# ============================================================
# Main
# ============================================================

def main():

    ensure_directories()

    setup_logging()

    # ========================================================
    # Lock
    # ========================================================

    try:

        acquire_lock()

    except Exception as exc:

        _logger.error(
            "%s",
            exc
        )

        return 2

    atexit.register(
        release_lock
    )

    # ========================================================
    # Branch
    # ========================================================

    branch = current_branch()

    if (
        branch
        !=
        REQUIRED_BRANCH
    ):

        _logger.error(
            "当前分支是 %s",
            branch
        )

        _logger.error(
            "必须切换到 %s",
            REQUIRED_BRANCH
        )

        release_lock()

        return 2

    # ========================================================
    # 版本可见性（GOLD-021）
    # ========================================================
    #
    # 启动顺序由 launcher 保证：bootstrap sync → Python/Orchestrator load。
    # 这里把「本进程实际看到的 HEAD」与「启动前 bootstrap sync 结果」
    # 都打印出来，便于确认运行中的进程到底加载的是哪个版本。

    startup_head = head_short_sha()

    bootstrap_state = read_bootstrap_sync_state()

    # ========================================================
    # 启动信息
    # ========================================================

    _logger.info(
        ""
    )

    _logger.info(
        "AI Orchestrator V2 started"
    )

    _logger.info(
        "Project: %s",
        ROOT
    )

    _logger.info(
        "Branch : %s",
        branch
    )

    _logger.info(
        "HEAD   : %s",
        (
            startup_head
            or
            "<unknown>"
        )
    )

    _logger.info(
        "Bootstrap sync: %s",
        describe_bootstrap_sync(
            bootstrap_state
        )
    )

    consistency_warning = bootstrap_head_mismatch(
        bootstrap_state,
        startup_head
    )

    if consistency_warning:

        _logger.warning(
            "%s",
            consistency_warning
        )

    _logger.info(
        "Poll   : %ss",
        POLL_SECONDS
    )

    _logger.info(
        "Retries: %s",
        DEFAULT_MAX_ATTEMPTS
    )

    _logger.info(
        "Cline timeout: %ss",
        CLINE_TIMEOUT_SECONDS
    )

    _logger.info(
        "Cline provider: %s",
        CLINE_PROVIDER
    )

    _logger.info(
        "Cline model: %s",
        (
            CLINE_MODEL
            or
            "<provider default>"
        )
    )

    _logger.info(
        "Queue target: %s task(s)",
        QUEUE_TARGET_SIZE
    )

    _logger.info(
        "Idle log: %ss",
        IDLE_LOG_SECONDS
    )

    _logger.info(
        "Refill hint: %ss (GPT_PLANNER_REFILL_REQUIRED throttle)",
        REFILL_HINT_SECONDS
    )

    _logger.info(
        ""
    )

    last_idle_log_at = None

    # GOLD-035：低水位提示的跨轮节流状态（只在 idle 路径推进）。
    refill_hint_state = None

    # ========================================================
    # Main Loop
    # ========================================================

    try:

        while True:

            try:

                step = run_iteration(
                    last_idle_log_at,
                    refill_hint_state
                )

                last_idle_log_at = step[
                    "last_idle_log_at"
                ]

                # GOLD-035：跨轮保留低水位提示的节流状态，
                # 避免 idle / no-runnable 时每 20 秒重复刷屏。
                refill_hint_state = step.get(
                    "refill_hint_state"
                )

                # ============================================
                # 版本漂移可见性（GOLD-021）
                # ============================================
                #
                # 本轮可能刚刚 pull 了新 commit：磁盘代码已变，
                # 但本进程仍运行启动时加载的代码，必须明确告警，
                # 避免再次出现「磁盘已更新却以为是新代码」的误判。

                observed_head = head_short_sha()

                if (
                    observed_head
                    and
                    observed_head
                    != startup_head
                ):

                    _logger.warning(

                        "检测到磁盘代码已更新"

                        "（%s -> %s）："

                        "当前进程仍运行"

                        "启动时加载的代码，"

                        "请重启 start_agent.bat"

                        "以加载新版本。",

                        (
                            startup_head
                            or
                            "<unknown>"
                        ),

                        observed_head
                    )

                    startup_head = observed_head

                if step["action"] == ITERATION_CONTINUE:

                    continue

                if step["action"] == ITERATION_STOP:

                    return 3

                # ============================================
                # Sleep
                # ============================================

                sleep_interruptibly(
                    POLL_SECONDS
                )

            except KeyboardInterrupt:

                raise

            except Exception:

                _logger.exception(
                    "本轮执行出现"
                    "未处理异常。"
                )

                sleep_interruptibly(
                    POLL_SECONDS
                )

    # ========================================================
    # Ctrl + C
    # ========================================================

    except KeyboardInterrupt:

        _logger.info(
            "Stopping..."
        )

    # ========================================================
    # Cleanup
    # ========================================================

    finally:

        release_lock()

    return 0


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )