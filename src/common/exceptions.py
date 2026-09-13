"""GOLD-AI 统一异常定义。

对应文档：
- 06_Cline开发规则 第 11 条（统一错误返回结构）
- 第 24 条（禁止伪完成：不允许静默吞异常）

所有业务异常继承 :class:`GoldAIError`，并携带稳定的 ``code``，
便于 API 层统一转换成 ``{"code": ..., "message": ..., "details": {}}``。
"""

from __future__ import annotations

from typing import Any


class GoldAIError(Exception):
    """GOLD-AI 业务异常基类。"""

    code: str = "GOLD_AI_ERROR"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 统一错误结构。"""
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        return self.message


class ConfigSecurityError(GoldAIError):
    """配置违反实盘/安全门禁。"""

    code = "CONFIG_SECURITY_VIOLATION"
    http_status = 500


class TimeSemanticsError(GoldAIError):
    """时间语义错误：naive datetime、时间倒置、未来时间戳等。"""

    code = "TIME_SEMANTICS_VIOLATION"
    http_status = 422


class ImmutableRecordError(GoldAIError):
    """原始/事实数据被尝试覆盖（03_数据库完整设计 第 10 节）。"""

    code = "IMMUTABLE_RECORD_VIOLATION"
    http_status = 409


class SeedError(GoldAIError):
    """基础数据种子的前置条件不满足（例如尚未执行 ``alembic upgrade head``）。"""

    code = "SEED_PRECONDITION_FAILED"
    http_status = 500
