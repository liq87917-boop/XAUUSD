import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

TASK_DIR = ROOT / ".ai" / "tasks"
RESULT_DIR = ROOT / ".ai" / "results"
RUNTIME_DIR = ROOT / ".ai" / "runtime"

POLL_SECONDS = 20

REQUIRED_BRANCH = "cline-agent"


def run_command(command, shell=True):
    result = subprocess.run(
        command,
        cwd=ROOT,
        shell=shell,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


def git(command):
    return run_command(f"git {command}")


def current_branch():
    result = git("branch --show-current")

    if result["returncode"] != 0:
        return None

    return result["stdout"].strip()


def git_is_dirty():
    result = git("status --porcelain")

    return bool(result["stdout"].strip())


def pull_latest():
    print("Pulling latest tasks...")

    result = git("pull --rebase origin cline-agent")

    if result["returncode"] != 0:
        print(result["stderr"])

    return result["returncode"] == 0


def find_next_task():

    tasks = sorted(TASK_DIR.glob("*.json"))

    for task_file in tasks:

        result_file = RESULT_DIR / task_file.name

        if not result_file.exists():
            return task_file

    return None


def load_task(task_file):

    with open(task_file, "r", encoding="utf-8") as f:
        return json.load(f)


def run_cline(task_file):

    # Windows npm 全局命令优先使用 .cmd
    if os.name == "nt":
        cline_exe = shutil.which("cline.cmd")
    else:
        cline_exe = shutil.which("cline")

    if not cline_exe:
        cline_exe = shutil.which("cline")

    if not cline_exe:
        raise RuntimeError(
            "找不到 Cline CLI，请先执行 npm install -g cline"
        )

    relative_task = task_file.relative_to(ROOT)

    # 不再把整个 task 内容塞进命令行。
    # Cline 自己读取 task 文件。
    prompt = (
        f"读取任务文件 {relative_task.as_posix()}，"
        f"严格按照其中任务执行，同时遵守项目根目录 .clinerules。"
        f"完成所有 requirements 和必要测试后输出结果并退出。"
    )

    args = [
        cline_exe,
        "--json",
        "--yolo",
        "--timeout",
        "3600",
        prompt
    ]

    print("Starting Cline...")
    print(f"Cline executable: {cline_exe}")
    print(f"Task file: {relative_task}")

    if os.name == "nt":

        # npm 在 Windows 下安装的是 cline.cmd。
        # 显式通过 cmd shell 执行，避免 subprocess 直接调用
        # .cmd 时出现参数丢失/TTY 模式误判。
        command_line = subprocess.list2cmdline(args)

        result = subprocess.run(
            command_line,
            cwd=ROOT,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

    else:

        result = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


def run_validations(task):

    commands = task.get("validation_commands", [])

    results = []

    for command in commands:

        print(f"Validation: {command}")

        result = run_command(command)

        results.append({
            "command": command,
            "returncode": result["returncode"],
            "stdout": result["stdout"][-5000:],
            "stderr": result["stderr"][-5000:]
        })

    return results


def get_changed_files():

    result = git("status --short")

    return [
        line
        for line in result["stdout"].splitlines()
        if line.strip()
    ]


def get_diff_stat():

    result = git("diff --stat")

    return result["stdout"]


def save_result(task, cline_result, validations):

    task_id = task["task_id"]

    validation_success = all(
        r["returncode"] == 0
        for r in validations
    )

    success = (
        cline_result["returncode"] == 0
        and validation_success
    )

    result = {

        "task_id": task_id,

        "status": (
            "completed"
            if success
            else "failed"
        ),

        "finished_at": datetime.now().isoformat(),

        "cline_exit_code":
            cline_result["returncode"],

        "changed_files":
            get_changed_files(),

        "git_diff_stat":
            get_diff_stat(),

        "validations":
            validations,

        "cline_output_tail":
            cline_result["stdout"][-8000:],

        "cline_error_tail":
            cline_result["stderr"][-5000:]
    }

    result_file = RESULT_DIR / f"{task_id}.json"

    with open(
        result_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=2
        )

    return result


def commit_result(task, result):

    task_id = task["task_id"]

    status = result["status"]

    git("add -A")

    if status == "completed":

        message = f"ai: complete {task_id}"

    else:

        message = f"ai: failed {task_id}"

    commit = git(
        f'commit -m "{message}"'
    )

    if commit["returncode"] != 0:

        print(commit["stdout"])
        print(commit["stderr"])

        return False

    # 先同步远端，避免 ChatGPT 刚创建新 task 时冲突
    pull = git(
        "pull --rebase origin cline-agent"
    )

    if pull["returncode"] != 0:

        print(pull["stderr"])

        return False

    push = git(
        "push origin cline-agent"
    )

    if push["returncode"] != 0:

        print(push["stderr"])

        return False

    return True


def process_task(task_file):

    task = load_task(task_file)

    task_id = task["task_id"]

    print("")
    print("=" * 60)
    print(f"Task: {task_id}")
    print(task.get("title", ""))
    print("=" * 60)

    # 防止把你自己还没提交的代码混进 AI commit
    if git_is_dirty():

        print(
            "工作区存在未提交修改，"
            "为了避免覆盖人工代码，本轮停止。"
        )

        return

    cline_result = run_cline(task_file)

    validations = run_validations(task)

    result = save_result(
        task,
        cline_result,
        validations
    )

    commit_result(
        task,
        result
    )

    print("")
    print(
        f"Task {task_id}: "
        f"{result['status']}"
    )


def main():

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

    branch = current_branch()

    if branch != REQUIRED_BRANCH:

        print(
            f"当前分支是 {branch}"
        )

        print(
            f"必须切换到 {REQUIRED_BRANCH}"
        )

        return

    print("")
    print("AI Orchestrator started")
    print(f"Project: {ROOT}")
    print(f"Branch : {branch}")
    print("")

    while True:

        try:

            pull_latest()

            task_file = find_next_task()

            if task_file:

                process_task(task_file)

            else:

                print(
                    "No new task..."
                )

        except KeyboardInterrupt:

            print("Stopping...")
            break

        except Exception as e:

            print(
                f"ERROR: {e}"
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()