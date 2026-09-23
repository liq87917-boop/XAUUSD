"""受控的 planner snapshot 输出写入器（GOLD-024）。

为什么单独一个模块
------------------
``orchestrator/planner_snapshot.py`` 是**纯只读**事实快照（源码内不存在任何写入路径，
由源码守卫测试锁定）。GOLD-024 允许 CLI 把同一份快照写到用户显式指定的
**runtime / 临时路径**，因此唯一的一处写操作必须被隔离、被守卫、被测试：

- 本模块只做一件事：把**已经渲染好的** snapshot JSON 写到一个已通过守卫的路径；
- 守卫是 fail-closed 的：路径不合法就返回 ``(None, reason)``，调用方**绝不写任何文件**
  并以退出码 ``EXIT_OUTPUT_REJECTED`` 收口。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- 绝不写 ``.ai/tasks/**``、``.ai/results/**``、``.ai/PROJECT_STATE.json``、``.git/**``、
  ``src/**``、``database/**``、``config/**``、``data/**``、``logs/**``、
  ``scripts/**``、``tests/**``、``docs/**`` 或任何仓库级文件（业务数据零改写）；
- 只允许写 ``<root>/.ai/runtime/**`` 与系统临时目录，二者之外一律拒绝；
- 路径先解析再判定：``..`` 逃逸（例如 ``.ai/runtime/../tasks/x.json``）会被拒；
- 父目录不存在时**不创建目录**，直接拒绝（零隐式副作用）；
- 不执行任何 Git / 网络 / 子进程操作，不规划任务、不改写任何状态。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

# 写路径被拒时报告给人类的稳定 code（stderr 只有人类通道）。
ISSUE_OUTPUT_PATH_REJECTED = "OUTPUT_PATH_REJECTED"

# ``--output`` 目标被拒时的退出码（与 0/2/3 的漂移语义互不干扰）。
EXIT_OUTPUT_REJECTED = 4

# 相对仓库根的**唯一**允许写入位置（runtime 路径）。
RUNTIME_SUBPATH = ".ai/runtime"

# 相对仓库根的**禁止**写入位置。
# 先于「允许根」判定，给 GPT / 人工更明确的原因；覆盖项目状态、安全配置与业务数据。
FORBIDDEN_OUTPUT_SUBPATHS = (
    ".ai/tasks",
    ".ai/results",
    ".ai/PROJECT_STATE.json",
    ".ai/logs",
    ".git",
    "src",
    "database",
    "config",
    "data",
    "logs",
    "scripts",
    "tests",
    "docs",
    "templates",
    "examples",
    ".clinerules",
    "pyproject.toml",
    "alembic.ini",
    "README.md",
    "TECH_DEBT.md",
    "PROGRESS_LOG.md",
)


def temp_directory() -> Path:
    """系统临时目录（runtime / 临时路径里的「临时」一侧）。"""

    return Path(tempfile.gettempdir())


def allowed_output_roots(root: Path) -> tuple[Path, ...]:
    """允许写入的根目录：``<root>/.ai/runtime`` 与系统临时目录。"""

    return (Path(root).resolve() / RUNTIME_SUBPATH, temp_directory().resolve())


def forbidden_output_paths(root: Path) -> tuple[Path, ...]:
    """相对仓库根的禁止写入位置（已解析为绝对路径，便于前缀判定）。"""

    resolved_root = Path(root).resolve()

    return tuple((resolved_root / item).resolve() for item in FORBIDDEN_OUTPUT_SUBPATHS)


def is_within(path: Path, root: Path) -> bool:
    """``path`` 是否等于 ``root`` 或位于 ``root`` 之内（纯路径判定，不触碰文件系统）。"""

    return path == root or root in path.parents


def resolve_output_target(root: Path, output: str | Path) -> tuple[Path | None, str | None]:
    """把 ``--output`` 解析为**已验证**的写入目标；不合法时返回 ``(None, reason)``。

    调用方必须把 ``None`` 视为 fail-closed：不写任何文件、以 ``EXIT_OUTPUT_REJECTED`` 收口。
    相对路径按仓库根解析；``..`` 会被 ``resolve()`` 归一化后再判定，无法逃逸出允许根。
    """

    candidate = Path(output)

    if not candidate.is_absolute():
        candidate = Path(root) / candidate

    resolved = candidate.resolve()

    for forbidden in forbidden_output_paths(root):
        if is_within(resolved, forbidden):
            return None, f"禁止写入项目状态 / 业务路径: {resolved}"

    allowed_roots = allowed_output_roots(root)

    if not any(is_within(resolved, allowed) for allowed in allowed_roots):
        allowed_text = ", ".join(str(item) for item in allowed_roots)

        return None, f"只允许写入 runtime / 临时路径（{allowed_text}）: {resolved}"

    if not resolved.parent.is_dir():
        return None, f"父目录不存在（本模块不创建目录）: {resolved.parent}"

    return resolved, None


def guard_summary() -> dict[str, Any]:
    """守卫契约的机器可读声明（供 planner snapshot 的 ``--output`` 契约测试断言）。"""

    return {
        "schema": "gold-ai/planner-snapshot-output/v1",
        "read_only_by_default": True,
        "runtime_subpath": RUNTIME_SUBPATH,
        "forbidden_subpaths": list(FORBIDDEN_OUTPUT_SUBPATHS),
        "rejection_code": ISSUE_OUTPUT_PATH_REJECTED,
        "rejection_exit_code": EXIT_OUTPUT_REJECTED,
        "creates_directories": False,
    }


def write_snapshot_output(target: str | Path, rendered: str) -> None:
    """写入已通过守卫的目标文件（本模块**唯一**的写操作）。

    调用方必须先取得 :func:`resolve_output_target` 的 ``(target, None)`` 结果；
    本函数不做任何路径判定以外的决策，也不创建目录。
    """

    Path(target).write_text(rendered, encoding="utf-8")
