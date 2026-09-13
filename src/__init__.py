"""GOLD-AI 业务源码包。

说明：本文件使 ``src`` 成为显式包（而不是隐式命名空间包），
从而让 mypy 能唯一确定模块名（``src.common.time`` 而不是 ``common.time``）。
当前内容（Phase 1）：``src/common`` 通用能力；后续阶段将加入
``src/collectors``、``src/processors``、``src/features``、``src/authors`` …（01 §18）。
"""

__all__: list[str] = []
