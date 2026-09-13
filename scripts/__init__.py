"""运维 / 研究脚本包（人工触发的 CLI，不放业务逻辑）。

为什么必须有这个文件：`scripts` 必须是**显式包**，否则
`mypy`（`pyproject.toml` 里 `files = ["config", "database", "src", "scripts"]`）
会把 `scripts/foo.py` 同时解析为顶层模块 `foo` 与包内模块 `scripts.foo`，
报 "Source file found twice under different module names"。

（本包内的脚本之间互相导入时用 `from scripts import xxx`，与测试的导入路径保持一致。）
"""

__all__: list[str] = []
