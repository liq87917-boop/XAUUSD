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
- **只对已批准的 runtime 路径**（``<root>/.ai/runtime/**``）做确定性目录准备：
  runtime 不进 Git（见 ``.gitignore``），干净 CI checkout 里父目录本就不存在，
  默认镜像 / 输出路径不能因此被判「路径非法」；该准备绝不扩大到系统临时目录、
  ``.ai/tasks``、``.ai/results``、``.ai/PROJECT_STATE.json``、``src``、``database``；
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


def runtime_output_root(root: Path) -> Path:
    """**唯一**允许做目录准备的根目录：``<root>/.ai/runtime``。"""

    return (Path(root).resolve() / RUNTIME_SUBPATH)


def prepare_output_parent(resolved: Path, root: Path) -> str | None:
    """为**已批准 runtime 路径**确定性准备父目录；其它位置只判定、不创建。

    返回 ``None`` 表示目标父目录已存在，或已被安全创建；返回字符串表示拒绝原因
    （调用方必须 fail-closed，且绝不写任何文件）。

    为什么只对 runtime：``.ai/runtime`` 永不进 Git（``.gitignore``），干净 CI
    checkout 里天然不存在；默认镜像 / 输出路径若因此被拒，控制面就会在 CI 上
    假失败。同时又必须守住写入白名单，所以这里：
    - 只接受 ``<root>/.ai/runtime/**`` 之内的目标（``..`` 已在 :func:`resolve_output_target`
      解析过，无法逃逸）；
    - 只创建**缺失的父目录链**（``exist_ok=True``），已存在目录零副作用；
    - 系统临时目录 / ``.ai/tasks`` / ``.ai/results`` / ``.ai/PROJECT_STATE.json`` /
      ``src`` / ``database`` 等一律不创建。
    """

    parent = resolved.parent

    if parent.is_dir():
        return None

    if not is_within(resolved, runtime_output_root(root)):
        return f"父目录不存在（只为 <root>/.ai/runtime/** 准备目录）: {parent}"

    try:
        parent.mkdir(parents=True, exist_ok=True)

    except OSError as exc:
        return f"runtime 父目录准备失败（fail-closed）: {parent} ({exc})"

    if not parent.is_dir():
        return f"runtime 父目录准备后仍不可用（fail-closed）: {parent}"

    return None


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

    # 父目录准备**只**对已批准的 runtime 路径生效（见 prepare_output_parent）。
    preparation_error = prepare_output_parent(resolved, root)

    if preparation_error is not None:
        return None, preparation_error

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
        "creates_directories": True,
        "creates_directories_scope": RUNTIME_SUBPATH,
        "directory_preparation": "parents-only, inside <root>/.ai/runtime/**, exist_ok=True",
    }


def write_snapshot_output(target: str | Path, rendered: str) -> None:
    """写入已通过守卫的目标文件（本模块**唯一**的写操作）。

    调用方必须先取得 :func:`resolve_output_target` 的 ``(target, None)`` 结果；
    父目录已由 :func:`prepare_output_parent` 在守卫范围内准备好（只对
    ``<root>/.ai/runtime/**`` 生效），因此本函数只写文件、不做任何其它决策。
    """

    Path(target).write_text(rendered, encoding="utf-8")
