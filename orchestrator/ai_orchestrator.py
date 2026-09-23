import atexit
import contextlib
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

TASK_STATE_DIR = RUNTIME_DIR / "tasks"
RECOVERY_DIR = RUNTIME_DIR / "recovery"

LOCK_FILE = RUNTIME_DIR / "orchestrator.lock"
LOG_FILE = LOG_DIR / "orchestrator.log"


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
# 全局状态
# ============================================================

_logger = logging.getLogger(
    "ai_orchestrator"
)

_lock_owned = False


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

        "changed_files":
            get_changed_files(),

        "untracked_files":
            untracked_files,

        "patch_file":
            "changes.patch"
    }

    atomic_write_json(
        snapshot_dir
        /
        "recovery.json",
        metadata
    )

    return snapshot_dir


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
                started_at
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

def is_pid_running(
    pid
):

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

            errors="replace"
        )

        output = (
            result.stdout
            .lower()
        )

        return (
            str(pid)
            in
            output
        )

    # ========================================================
    # Linux / macOS
    # ========================================================

    try:

        os.kill(
            pid,
            0
        )

        return True

    except OSError:

        return False


def acquire_lock():

    global _lock_owned

    ensure_directories()

    # ========================================================
    # 已存在 Lock
    # ========================================================

    if LOCK_FILE.exists():

        existing = (
            read_json(
                LOCK_FILE,
                default={}
            )
            or
            {}
        )

        pid = existing.get(
            "pid"
        )

        try:

            pid = int(
                pid
            )

        except (
            TypeError,
            ValueError
        ):

            pid = None

        # ----------------------------------------------------
        # 另一个 Orchestrator 正在运行
        # ----------------------------------------------------

        if (
            pid
            and
            is_pid_running(
                pid
            )
        ):

            raise RuntimeError(

                "已有 Orchestrator "
                "正在运行，"

                f"PID={pid}。"
            )

        # ----------------------------------------------------
        # 上一次异常结束留下的 lock
        # ----------------------------------------------------

        _logger.warning(
            "发现陈旧 lock 文件，"
            "自动清理。"
        )

        try:

            LOCK_FILE.unlink()

        except OSError as exc:

            raise RuntimeError(

                "无法清理陈旧 "
                f"lock 文件: {exc}"
            ) from exc

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
    last_idle_log_at
):
    """执行一轮 Orchestrator 循环，返回本轮动作。

    顺序与旧 main loop 完全一致，并显式固化 push recovery 契约：
    1) 先 Git sync（pull --rebase + retry push pending commits）；
    2) 同步失败 => fail-closed：本轮不执行任何任务
       （completed locally + push pending 时绝不重跑 Cline，
        本地 commit/result 一律保留）；
    3) 只有 remote synced 之后，rolling queue 才允许检查/执行下一个 task。
    """

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
                last_idle_log_at
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

    return {

        "action":
            ITERATION_SLEEP,

        "outcome":
            "idle",

        "last_idle_log_at":
            last_idle_log_at
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
        ""
    )

    last_idle_log_at = None

    # ========================================================
    # Main Loop
    # ========================================================

    try:

        while True:

            try:

                step = run_iteration(
                    last_idle_log_at
                )

                last_idle_log_at = step[
                    "last_idle_log_at"
                ]

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