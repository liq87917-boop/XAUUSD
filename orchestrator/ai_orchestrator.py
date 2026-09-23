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
    if not pull_latest():

        return False

    return push_pending_commits()


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

def get_task_dependencies(
    task
):

    value = task.get(
        "depends_on",
        []
    )

    if value in (
        None,
        ""
    ):
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
        raise ValueError(
            "depends_on 必须是字符串或字符串数组"
        )

    task_id = str(
        task.get(
            "task_id",
            ""
        )
    ).strip()

    dependencies = []
    seen = set()

    for item in value:

        dependency = str(
            item
        ).strip()

        if not dependency:
            raise ValueError(
                f"{task_id}: depends_on 包含空任务 ID"
            )

        if dependency == task_id:
            raise ValueError(
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
    task
):

    gate = task.get(
        "human_gate"
    )

    if isinstance(
        gate,
        dict
    ):
        gate = gate.get(
            "level"
        )

    if gate in (
        None,
        ""
    ):
        return None

    return str(
        gate
    ).strip().upper()


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


def evaluate_task_readiness(
    task
):

    task_id = str(
        task.get(
            "task_id",
            ""
        )
    ).strip()

    auto_start = task.get(
        "auto_start",
        True
    )

    if auto_start is not True:

        return (
            False,
            f"{task_id}: auto_start=false"
        )

    if task.get(
        "requires_human_approval",
        False
    ) is True:

        return (
            False,
            f"{task_id}: requires_human_approval=true"
        )

    gate = get_task_human_gate(
        task
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

    for dependency in get_task_dependencies(
        task
    ):

        dependency_task = (
            TASK_DIR
            /
            f"{dependency}.json"
        )

        if not dependency_task.exists():

            return (
                False,
                f"{task_id}: dependency missing: {dependency}"
            )

        dependency_status = (
            task_result_status(
                dependency
            )
        )

        if (
            dependency_status
            ==
            "completed"
        ):
            continue

        if (
            dependency_status
            ==
            "blocked"
        ):

            return (
                False,
                f"{task_id}: dependency blocked: {dependency}"
            )

        return (
            False,
            (
                f"{task_id}: dependency not completed: "
                f"{dependency}={dependency_status}"
            )
        )

    return (
        True,
        "ready"
    )


def queue_snapshot():

    pending = []

    for task_file in sorted(
        TASK_DIR.glob(
            "*.json"
        )
    ):

        try:

            task = load_task(
                task_file
            )

        except Exception:
            continue

        task_id = task[
            "task_id"
        ]

        status = task_result_status(
            task_id
        )

        if status in {
            "completed",
            "blocked"
        }:
            continue

        pending.append(
            task_id
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

def find_next_task_with_reason():

    tasks = sorted(
        TASK_DIR.glob(
            "*.json"
        )
    )

    for task_file in tasks:

        try:

            task = load_task(
                task_file
            )

        except Exception as exc:

            _logger.error(
                "跳过无效任务 %s: %s",
                task_file.name,
                exc
            )

            continue

        task_id = (
            task["task_id"]
        )

        status = task_result_status(
            task_id
        )

        if status in {
            "completed",
            "blocked"
        }:

            continue

        ready, reason = (
            evaluate_task_readiness(
                task
            )
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

    "forbidden":
        "PROVIDER_FORBIDDEN",

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

    if cline_result.get(
        "timed_out",
        False
    ):

        return (
            "retryable_external",
            "CLINE_TIMEOUT"
        )

    combined = (
        (
            cline_result.get(
                "stderr",
                ""
            )
            or
            ""
        )
        +
        "\n"
        +
        (
            cline_result.get(
                "stdout",
                ""
            )
            or
            ""
        )
    ).lower()

    for pattern, code in (
        NON_RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in combined:

            return (
                "non_retryable_external",
                code
            )

    for pattern, code in (
        RETRYABLE_EXTERNAL_PATTERNS
        .items()
    ):

        if pattern in combined:

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
    recovery_path=None
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

        "finish_reason":
            summary.get(
                "finish_reason"
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
    note=None
):

    task_id = (
        task["task_id"]
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

        # ----------------------------------------------------
        # Attempt 记录
        # ----------------------------------------------------

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
                    failure_code
            )
        )

        attempts.append(
            attempt_record
        )

        # ====================================================
        # 判断成功
        # ====================================================

        success = (

            cline_result[
                "returncode"
            ]
            ==
            0

            and

            validations_passed(
                validations
            )
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
                    diff_stat
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
                    "Task %s: completed",
                    task_id
                )

            else:

                _logger.warning(

                    "Task %s: "
                    "completed locally, "
                    "push pending",

                    task_id
                )

            return (
                "completed"
                if pushed
                else "push_pending"
            )

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

                # ============================================
                # Git Sync
                # ============================================

                if not sync_repository():

                    sleep_interruptibly(
                        POLL_SECONDS
                    )

                    continue

                # ============================================
                # Task
                # ============================================

                (
                    task_file,
                    wait_reason
                ) = find_next_task_with_reason()

                if task_file:

                    outcome = process_task(
                        task_file
                    )

                    last_idle_log_at = None

                    if (
                        outcome
                        ==
                        "completed"
                    ):

                        _logger.info(
                            "Rolling queue: "
                            "checking next task immediately."
                        )

                        continue

                    if (
                        outcome
                        ==
                        "waiting_external"
                    ):

                        _logger.error(
                            "Orchestrator 因外部 Provider "
                            "不可重试错误停止。"
                            "修复余额/认证/配额后重新运行 "
                            "start_agent.bat 即可从同一 Task 重试。"
                        )

                        return 3

                else:

                    now = time.monotonic()

                    if should_log_idle(
                        last_idle_log_at,
                        current_time=now
                    ):

                        snapshot = (
                            queue_snapshot()
                        )

                        pending_preview = (
                            ", ".join(
                                snapshot[
                                    "pending"
                                ][:
                                    QUEUE_TARGET_SIZE
                                ]
                            )
                            or
                            "-"
                        )

                        _logger.info(
                            "No runnable task. "
                            "pending=%s/%s [%s] | %s",
                            snapshot[
                                "count"
                            ],
                            snapshot[
                                "target"
                            ],
                            pending_preview,
                            wait_reason
                        )

                        last_idle_log_at = now

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