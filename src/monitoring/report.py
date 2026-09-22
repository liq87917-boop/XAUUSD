"""采集健康度 + Phase 3.3 数据资格的**只读**组合报告（GOLD-004）。

- 组合层只做编排与渲染：不新增业务阈值、不写库、不联网、不生成交易信号；
- 默认人类可读（Markdown）；``to_dict()`` 提供稳定机器可读结构（供 CLI ``--json``）；
- 脱敏在事实层完成（``safe_text`` / ``safe_url``），且 ``sources.config_json``
  **整体不进入输出**，因此不会泄露凭据或完整来源配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.orm import Session

from src.monitoring.collector_health import (
    DEFAULT_WINDOW_HOURS,
    HealthReport,
    load_health_report,
    render_health_report,
)
from src.monitoring.phase33_qualification import (
    PHASE3_3_BLOCKER_CODE,
    QualificationReport,
    load_qualification_report,
    render_qualification_report,
)

__all__ = [
    "MONITORING_SCHEMA_VERSION",
    "REPORT_TITLE",
    "MonitoringReport",
    "load_monitoring_report",
    "render_monitoring_report",
]

#: 组合报告 schema 版本（字段增删必须同步升版本 + 更新测试）
MONITORING_SCHEMA_VERSION: Final[int] = 1
#: 报告标题（CLI 与落盘 Markdown 共用）
REPORT_TITLE: Final[str] = "采集运行健康度与 Phase 3.3 数据资格观测报告"


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    """一次只读观测的完整结果（健康度 + 资格缺口）。"""

    schema_version: int
    generated_at: datetime
    window_hours: int
    health: HealthReport
    qualification: QualificationReport

    @property
    def healthy(self) -> bool:
        return self.health.healthy

    @property
    def blocker_active(self) -> bool:
        return self.qualification.blocker_active

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report": REPORT_TITLE,
            "generated_at": self.generated_at.isoformat(),
            "window_hours": self.window_hours,
            "healthy": self.healthy,
            "blocker_code": PHASE3_3_BLOCKER_CODE,
            "blocker_active": self.blocker_active,
            "health": self.health.to_dict(),
            "qualification": self.qualification.to_dict(),
        }

    def render(self) -> str:
        return render_monitoring_report(self)


def render_monitoring_report(report: MonitoringReport) -> str:
    """渲染人类可读 Markdown（控制台 / 落盘共用；不含凭据）。"""
    header = [
        f"# {REPORT_TITLE}",
        "",
        "> 只读报告：不写库、不改写历史事实、**不解除 Phase 3.3 blocker**；"
        "不输出凭据与完整来源配置。",
        "",
        f"- 生成时点（UTC）：{report.generated_at.isoformat()}",
        f"- 总体健康：{str(report.healthy).lower()}（{report.health.state.value}）",
        f"- Phase 3.3 blocker：{PHASE3_3_BLOCKER_CODE}"
        f"（active={str(report.blocker_active).lower()}）",
        "",
    ]
    return "\n".join(
        [
            *header,
            render_health_report(report.health),
            "",
            render_qualification_report(report.qualification),
        ]
    )


def load_monitoring_report(
    session: Session,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    as_of: datetime | None = None,
    stale_after: timedelta | None = None,
    hf_weak_supervision_rows: int = 0,
) -> MonitoringReport:
    """读取只读组合报告（健康度 + 资格缺口，共用同一审计时钟）。"""
    health = load_health_report(
        session, window_hours=window_hours, as_of=as_of, stale_after=stale_after
    )
    qualification = load_qualification_report(
        session, as_of=health.as_of, hf_weak_supervision_rows=hf_weak_supervision_rows
    )
    return MonitoringReport(
        schema_version=MONITORING_SCHEMA_VERSION,
        generated_at=health.as_of,
        window_hours=window_hours,
        health=health,
        qualification=qualification,
    )
