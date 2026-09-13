"""脚本控制台输出工具（把 Windows 中文环境的编码坑集中到一处）。

为什么需要它：Git Bash / PowerShell / cmd 的默认代码页常常是 **GBK**。
- 脚本若**无条件**把 stdout 切成 UTF-8，中文在 GBK 控制台上会变成乱码；
- 脚本若打印 GBK 表示不了的字符（例如 "⚠"），在 GBK 控制台上会直接
  ``UnicodeEncodeError`` 让整个脚本崩掉（本项目真实踩过）；
- 而管道 / CI 里标准输出常是 ASCII，这时又**必须**切 UTF-8，否则同样崩。

因此统一入口：先探测当前编码能否表示"我们确实要打印的字符"，不能才切换。

注意：**新增打印内容前先确认它能被 GBK 编码**（若确实需要特殊符号，同步更新 ``_PROBE``）。
"""

from __future__ import annotations

import sys
from typing import Final

__all__ = ["configure_stdout", "stdout_needs_utf8"]

#: 探测字符：取自脚本实际会打印的中文与常用符号
_PROBE: Final[str] = "黄金：（）【】→％"


def stdout_needs_utf8(encoding: str | None) -> bool:
    """当前标准输出编码能否表示脚本要打印的字符？（GBK 可以；ASCII / cp1252 不行）"""
    if not encoding:
        return True
    try:
        _PROBE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def configure_stdout() -> None:
    """只在必要时把 stdout 切到 UTF-8（GBK 控制台下强切 UTF-8 反而会乱码）。"""
    stream = sys.stdout
    if stdout_needs_utf8(getattr(stream, "encoding", None)) and hasattr(
        stream, "reconfigure"
    ):
        stream.reconfigure(encoding="utf-8", errors="replace")
