"""只读运行健康度与数据资格观测层（GOLD-004）。

本包**只读**：
- :mod:`src.monitoring.collector_health`：从 ``collector_runs`` / ``raw_items`` /
  ``processed_items`` / ``job_runs`` / ``sources`` 汇总 source 级运行健康度；
- :mod:`src.monitoring.phase33_qualification`：复用 ``src.alpha.evidence_gate`` 的既有阈值，
  输出机器可读的 Phase 3.3 Author / News 资格缺口（blocker 保持 BLOCKED）；
- :mod:`src.monitoring.evidence_readiness`：证据就绪度 / preflight（GOLD-006），
  量化证据入口台账与阈值之间的 remaining gap，并支持一键资格复核（blocker 恒为 active）；
- :mod:`src.monitoring.report`：组合报告（Markdown / 稳定 JSON）。

调用入口见 ``scripts/report_collector_health.py`` 与 ``scripts/evidence_readiness.py``。
"""

# src.monitoring 的公开导出改用 PEP 562 **惰性导出**（GOLD-018）。
#
# 为什么必须惰性：本包在 ``__init__`` 里 eager import 整个依赖图时，``import src.monitoring``
# 会**立即**加载 ``evidence_readiness`` / ``phase33_qualification``，进而 import
# ``src.evidence.contracts`` 并触发 ``src.evidence.__init__`` 的 eager 依赖图
# （``decision_packet`` → ``readiness_runner`` → ``handoff``），而 ``handoff`` 又反向 import
# **尚未初始化完**的 ``src.monitoring.evidence_readiness``（只执行到它第 38 行），于是
# ``import src.monitoring`` 之后再进入 ``src.evidence`` 链会抛：``ImportError: cannot import
# name 'BatchQuantification' from partially initialized module
# 'src.monitoring.evidence_readiness'``（反向顺序却正常，说明是包边界缺陷）。
#
# 现在包初始化阶段**不加载任何子模块**：只有**真正被访问**的名字才 import 其定义
# 子模块，因此 monitoring-first / evidence-first / 直接子模块 first 等任意导入顺序都稳定。
#
# 下表**只登记名字 -> 定义所在子模块**，**不复制**任何业务类 / 阈值 / 枚举 / 常量：
# 单一事实源仍是各子模块；``from <pkg> import X`` / ``from <pkg> import <子模块>`` 的
# 既有用法**完全不变**（含 ``import <pkg>`` 后再取属性）。

from __future__ import annotations

import importlib
from typing import Any, Final

#: 公开名 -> 定义所在子模块（src.monitoring 的子模块级导出索引）。
_LAZY_EXPORTS_BY_MODULE: Final[dict[str, tuple[str, ...]]] = {
    "collector_health": (
        "DEFAULT_STALE_AFTER", "DEFAULT_WINDOW_HOURS", "FAILED_STREAK_THRESHOLD",
        "HealthReport", "HealthState", "ProcessorCounts", "ProcessorState",
        "ProcessorSummary", "SchedulerSummary", "SourceHealth", "classify_source_state",
        "load_health_report", "render_health_report",
    ),
    "evidence_readiness": (
        "BatchQuantification", "EvidenceReadinessReport", "READINESS_NOTE",
        "READINESS_REPORT_TITLE", "READINESS_SCHEMA_VERSION", "ReadinessCheck",
        "ScopeReadiness", "build_readiness_report", "render_readiness_report",
        "summarize_batch",
    ),
    "phase33_qualification": (
        "AuthorGap", "CheckStatus", "EVIDENCE_INTAKE_NOTE",
        "EVIDENCE_INTAKE_SCHEMA_VERSION", "EvidenceIntakeSection", "PHASE3_3_BLOCKER_CODE",
        "QualificationGap", "QualificationReport", "build_evidence_intake_section",
        "load_qualification_report", "render_qualification_report",
    ),
    "report": (
        "MONITORING_SCHEMA_VERSION", "MonitoringReport", "REPORT_TITLE",
        "load_monitoring_report", "render_monitoring_report",
    ),
}

#: 公开名与定义处属性名不同的别名：``公开名 -> (子模块, 属性名)``。
_LAZY_EXPORT_ALIASES: Final[dict[str, tuple[str, str]]] = {
}

#: 惰性导出总表：``公开名 -> (子模块, 属性名)``。
_LAZY_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    **{
        name: (module, name)
        for module, names in _LAZY_EXPORTS_BY_MODULE.items()
        for name in names
    },
    **_LAZY_EXPORT_ALIASES,
}


def __getattr__(name: str) -> Any:
    """PEP 562 惰性导出：语义与旧版 eager ``from ... import ...`` **完全一致**。

    只有**真正被访问**的名字才会 import 其定义子模块，因此包初始化阶段**不会**加载
    整个依赖图，也就不会与其它包形成初始化期循环导入（GOLD-018）。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attribute = target
        value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute)
        globals()[name] = value  # 缓存：后续访问不再进入 __getattr__
        return value
    if name in _LAZY_EXPORTS_BY_MODULE:
        # 子模块名同样惰性可见（``from src.monitoring import <子模块>`` 等既有用法保持可用）。
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """``dir()`` = 模块字典 ∪ 公开导出（与 eager 版一致）。"""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "DEFAULT_STALE_AFTER",
    "DEFAULT_WINDOW_HOURS",
    "EVIDENCE_INTAKE_NOTE",
    "EVIDENCE_INTAKE_SCHEMA_VERSION",
    "FAILED_STREAK_THRESHOLD",
    "MONITORING_SCHEMA_VERSION",
    "PHASE3_3_BLOCKER_CODE",
    "READINESS_NOTE",
    "READINESS_REPORT_TITLE",
    "READINESS_SCHEMA_VERSION",
    "REPORT_TITLE",
    "AuthorGap",
    "BatchQuantification",
    "CheckStatus",
    "EvidenceIntakeSection",
    "EvidenceReadinessReport",
    "HealthReport",
    "HealthState",
    "MonitoringReport",
    "ProcessorCounts",
    "ProcessorState",
    "ProcessorSummary",
    "QualificationGap",
    "QualificationReport",
    "ReadinessCheck",
    "SchedulerSummary",
    "ScopeReadiness",
    "SourceHealth",
    "build_evidence_intake_section",
    "build_readiness_report",
    "classify_source_state",
    "load_health_report",
    "load_monitoring_report",
    "load_qualification_report",
    "render_health_report",
    "render_monitoring_report",
    "render_qualification_report",
    "render_readiness_report",
    "summarize_batch",
]
