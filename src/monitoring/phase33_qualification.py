"""Phase 3.3 Author / News 数据资格缺口的机器可读视图（GOLD-004）。

职责边界（**重要**）：
- 只读：复用 ``src.alpha.evidence_gate`` 的**既有口径与阈值**（``MIN_AUTHOR_SAMPLES`` /
  ``MIN_NEWS_EVENTS`` / ``MIN_NEWS_HISTORY_DAYS`` / ``MAX_NEWS_SOURCE_SHARE``），
  禁止在本模块另造阈值，也**不修改** ``src/alpha/**``；
- 诚实：库内数量门槛 PASS **不等于** Alpha 放行。来源授权与"历史可用时间证据"必须由
  人工 Gate 完成，本模块一律输出 ``BLOCKED``，**不得**用 Mock 或缺失数据判 PASS；
- ``PHASE3_3_DATA`` blocker 由本模块持续显式输出（``blocker_code`` / ``blocker_active``），
  观测层不具备解除 blocker 的能力；
- 输出结构包含：当前值、要求值、比较方式、PASS/BLOCKED、原因、证据时间范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import NewsEvent, RawItem
from database.models.enums import RawItemType
from src.alpha.evidence_gate import (
    MAX_NEWS_SOURCE_SHARE,
    MIN_AUTHOR_SAMPLES,
    MIN_NEWS_EVENTS,
    MIN_NEWS_HISTORY_DAYS,
    AuthorReadiness,
    Phase33Readiness,
    load_phase33_readiness,
)
from src.processors.timeline import ensure_utc_from_database

__all__ = [
    "HUMAN_GATE_COMPARATOR",
    "PHASE3_3_BLOCKER_CODE",
    "QUALIFICATION_SCHEMA_VERSION",
    "AuthorGap",
    "CheckStatus",
    "QualificationGap",
    "QualificationReport",
    "build_qualification_report",
    "load_qualification_report",
    "render_qualification_report",
]

#: 保持诚实的 blocker 代码（与 ``.ai/PROJECT_STATE.json`` 一致）
PHASE3_3_BLOCKER_CODE: Final[str] = "PHASE3_3_DATA"
#: 机器可读 schema 版本：字段增删必须同步升版本 + 更新测试
QUALIFICATION_SCHEMA_VERSION: Final[int] = 1
#: 需要人工 Gate 的比较方式（观测层无法自动判定）
HUMAN_GATE_COMPARATOR: Final[str] = "requires_human_gate"


class CheckStatus(StrEnum):
    """单项资格检查的结论（只有 PASS 才算达标）。"""

    PASS = "PASS"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class QualificationGap:
    """一条机器可读的资格缺口（当前值 / 要求值 / 结论 / 原因 / 证据时间范围）。"""

    key: str
    scope: str
    metric: str
    current: int | float | str | None
    required: int | float | str | None
    comparator: str
    status: CheckStatus
    reason: str
    evidence_start: str | None = None
    evidence_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "scope": self.scope,
            "metric": self.metric,
            "current": self.current,
            "required": self.required,
            "comparator": self.comparator,
            "status": self.status.value,
            "reason": self.reason,
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
        }


@dataclass(frozen=True, slots=True)
class AuthorGap:
    """单个作者的可信样本明细（机器可读；凭据不参与）。"""

    author_id: str
    display_name: str
    opinions: int
    trusted_posts: int
    min_account_samples: int
    identity_consistent: bool
    status: CheckStatus
    accounts: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "author_id": self.author_id,
            "display_name": self.display_name,
            "opinions": self.opinions,
            "trusted_posts": self.trusted_posts,
            "min_account_samples": self.min_account_samples,
            "identity_consistent": self.identity_consistent,
            "status": self.status.value,
            "accounts": [
                {"account": account, "trusted_posts": count} for account, count in self.accounts
            ],
        }


@dataclass(frozen=True, slots=True)
class QualificationReport:
    """Phase 3.3 数据资格缺口报告（只读；blocker 保持显式）。"""

    schema_version: int
    blocker_code: str
    blocker_active: bool
    ready: bool
    as_of: datetime | None
    author_evidence: tuple[str | None, str | None]
    news_evidence: tuple[str | None, str | None]
    authors: tuple[AuthorGap, ...]
    checks: tuple[QualificationGap, ...]
    pass_count: int
    blocked_count: int
    hf_weak_supervision_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "blocker_code": self.blocker_code,
            "blocker_active": self.blocker_active,
            "ready": self.ready,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "evidence_windows": {
                "author": {
                    "start_at": self.author_evidence[0],
                    "end_at": self.author_evidence[1],
                },
                "news": {"start_at": self.news_evidence[0], "end_at": self.news_evidence[1]},
            },
            "checks": [item.to_dict() for item in self.checks],
            "authors": [item.to_dict() for item in self.authors],
            "pass_count": self.pass_count,
            "blocked_count": self.blocked_count,
            "hf_weak_supervision_rows": self.hf_weak_supervision_rows,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _author_gap(readiness: AuthorReadiness) -> AuthorGap:
    """把 evidence_gate 的作者资格转成机器可读缺口（阈值口径完全复用）。"""
    samples = [count for _account, count in readiness.account_counts]
    min_samples = min(samples) if samples else 0
    return AuthorGap(
        author_id=readiness.author_id,
        display_name=readiness.display_name,
        opinions=readiness.opinions,
        trusted_posts=readiness.trusted_posts,
        min_account_samples=min_samples,
        identity_consistent=readiness.identity_consistent,
        status=CheckStatus.PASS if readiness.ready else CheckStatus.BLOCKED,
        accounts=readiness.account_counts,
    )


def _author_checks(
    readiness: Phase33Readiness,
    authors: tuple[AuthorGap, ...],
    evidence: tuple[str | None, str | None],
) -> list[QualificationGap]:
    """Author 三项检查（数量门槛 + 归属一致 + 来源授权人工 Gate）。"""
    ready_authors = sum(1 for item in authors if item.status is CheckStatus.PASS)
    min_samples = min((item.min_account_samples for item in authors), default=0)
    identity_ok = bool(authors) and all(item.identity_consistent for item in authors)
    author_start, author_end = evidence
    return [
        QualificationGap(
            key="author.trusted_posts_per_account",
            scope="author",
            metric="每个来源账号的可信独立帖子数（取作者最小值）",
            current=min_samples,
            required=MIN_AUTHOR_SAMPLES,
            comparator=">=",
            status=CheckStatus.PASS if readiness.author_ready else CheckStatus.BLOCKED,
            reason=(
                f"{len(authors)} 位作者中 {ready_authors} 位达标；"
                "口径见 docs/14 Phase 3.3 验收标准 1"
                if authors
                else "库内没有带观点的作者：Author Alpha 资格无证据"
            ),
            evidence_start=author_start,
            evidence_end=author_end,
        ),
        QualificationGap(
            key="author.identity_consistency",
            scope="author",
            metric="作者与来源账号归属一致性",
            current=f"{sum(1 for item in authors if item.identity_consistent)}/{len(authors)}",
            required="全部一致",
            comparator="==",
            status=CheckStatus.PASS if identity_ok else CheckStatus.BLOCKED,
            reason="作者 / 账号 / 帖子归属必须一致，否则不得参与技能计算",
            evidence_start=author_start,
            evidence_end=author_end,
        ),
        QualificationGap(
            key="author.source_authorization",
            scope="author",
            metric="来源授权（采集 / 存储 / 研究用途许可）",
            current="unverified",
            required="人工 Gate 通过",
            comparator=HUMAN_GATE_COMPARATOR,
            status=CheckStatus.BLOCKED,
            reason=(
                "本报告只读库内数量门槛，不核验来源许可；授权审查必须由人工 Gate 完成"
                "（见 docs/operations/Phase3_3_用户数据交接说明.md）"
            ),
        ),
    ]


def _news_checks(
    readiness: Phase33Readiness, evidence: tuple[str | None, str | None]
) -> list[QualificationGap]:
    """News 四项检查（事件数 / 时间跨度 / 单源占比 + 历史可用时间证据人工 Gate）。"""
    news = readiness.news
    news_start, news_end = evidence
    if news.events == 0:
        news_reason = "库内没有可用新闻事件：不得用缺失数据判 PASS"
    elif news.ready:
        news_reason = "库内数量/跨度/集中度均达标；仍不构成来源授权结论"
    else:
        news_reason = (
            f"事件 {news.events}、跨度 {news.history_days} 天、"
            f"单一来源占比 {news.largest_source_share:.2%} 未同时达标"
        )
    return [
        QualificationGap(
            key="news.events",
            scope="news",
            metric="可用新闻事件数",
            current=news.events,
            required=MIN_NEWS_EVENTS,
            comparator=">=",
            status=(
                CheckStatus.PASS if news.events >= MIN_NEWS_EVENTS else CheckStatus.BLOCKED
            ),
            reason=news_reason,
            evidence_start=news_start,
            evidence_end=news_end,
        ),
        QualificationGap(
            key="news.history_days",
            scope="news",
            metric="可用时间跨度（天）",
            current=news.history_days,
            required=MIN_NEWS_HISTORY_DAYS,
            comparator=">=",
            status=(
                CheckStatus.PASS
                if news.history_days >= MIN_NEWS_HISTORY_DAYS
                else CheckStatus.BLOCKED
            ),
            reason=(
                "跨度按逐条 max(news_events.effective_at, raw_items.effective_at) 计算；"
                "不得用今天采集的旧标题回填历史"
            ),
            evidence_start=news_start,
            evidence_end=news_end,
        ),
        QualificationGap(
            key="news.max_source_share",
            scope="news",
            metric="单一来源事件占比",
            current=news.largest_source_share,
            required=MAX_NEWS_SOURCE_SHARE,
            comparator="<=",
            status=(
                CheckStatus.PASS
                if news.events > 0 and news.largest_source_share <= MAX_NEWS_SOURCE_SHARE
                else CheckStatus.BLOCKED
            ),
            reason=(
                "无事件证据时不得判 PASS"
                if news.events == 0
                else f"最大来源占比 {news.largest_source_share:.2%}"
            ),
            evidence_start=news_start,
            evidence_end=news_end,
        ),
        QualificationGap(
            key="news.available_history_evidence",
            scope="news",
            metric="可审计的历史可用时间证据（effective_at >= collected_at）",
            current="unverified",
            required="合规来源的历史可用证据",
            comparator=HUMAN_GATE_COMPARATOR,
            status=CheckStatus.BLOCKED,
            reason="本报告不核验来源授权与历史可用时间证据；不得用 Mock / 缺失数据判 PASS",
        ),
    ]


def build_qualification_report(
    readiness: Phase33Readiness,
    *,
    author_evidence: tuple[datetime | None, datetime | None] = (None, None),
    news_evidence: tuple[datetime | None, datetime | None] = (None, None),
    hf_weak_supervision_rows: int = 0,
) -> QualificationReport:
    """纯函数：把 evidence_gate 结果 + 证据时间范围转成机器可读缺口报告。"""
    authors = tuple(_author_gap(item) for item in readiness.authors)
    author_window = (_iso(author_evidence[0]), _iso(author_evidence[1]))
    news_window = (_iso(news_evidence[0]), _iso(news_evidence[1]))
    checks = [
        *_author_checks(readiness, authors, author_window),
        *_news_checks(readiness, news_window),
    ]
    blocked = sum(1 for item in checks if item.status is CheckStatus.BLOCKED)
    return QualificationReport(
        schema_version=QUALIFICATION_SCHEMA_VERSION,
        blocker_code=PHASE3_3_BLOCKER_CODE,
        blocker_active=blocked > 0,
        ready=blocked == 0,
        as_of=readiness.as_of,
        author_evidence=author_window,
        news_evidence=news_window,
        authors=authors,
        checks=tuple(checks),
        pass_count=len(checks) - blocked,
        blocked_count=blocked,
        hf_weak_supervision_rows=hf_weak_supervision_rows,
    )


def _news_evidence_window(
    session: Session, moment: datetime
) -> tuple[datetime | None, datetime | None]:
    """新闻证据的时间范围。

    口径与 ``src.alpha.evidence_gate`` **完全一致**：逐条取
    ``max(news_events.effective_at, raw_items.effective_at)``、排除晚于审计时点的行、同一
    ``raw_item`` 只保留较晚可用时刻（保守防前视）。本函数只补齐门禁**未导出**的时间范围，
    不改变任何阈值判定。
    """
    rows = session.execute(
        sa.select(RawItem.id, NewsEvent.effective_at, RawItem.effective_at)
        .join(NewsEvent, NewsEvent.raw_item_id == RawItem.id)
        .where(RawItem.item_type == RawItemType.NEWS)
    ).all()
    available: dict[str, datetime] = {}
    for raw_id, event_at, raw_at in rows:
        available_at = max(
            ensure_utc_from_database(event_at, field_name="news_event_at"),
            ensure_utc_from_database(raw_at, field_name="raw_item_at"),
        )
        if available_at > moment:
            continue
        key = str(raw_id)
        prior = available.get(key)
        available[key] = max(prior, available_at) if prior is not None else available_at
    if not available:
        return (None, None)
    values = list(available.values())
    return (min(values), max(values))


def _author_evidence_window(session: Session) -> tuple[datetime | None, datetime | None]:
    """作者证据时间范围：原始 ``POST`` 记录的 ``effective_at`` 区间（不猜时间）。"""
    earliest = session.scalars(
        sa.select(RawItem.effective_at)
        .where(RawItem.item_type == RawItemType.POST)
        .order_by(RawItem.effective_at.asc())
        .limit(1)
    ).first()
    latest = session.scalars(
        sa.select(RawItem.effective_at)
        .where(RawItem.item_type == RawItemType.POST)
        .order_by(RawItem.effective_at.desc())
        .limit(1)
    ).first()
    return (
        ensure_utc_from_database(earliest, field_name="raw_item.effective_at")
        if earliest is not None
        else None,
        ensure_utc_from_database(latest, field_name="raw_item.effective_at")
        if latest is not None
        else None,
    )


def load_qualification_report(
    session: Session,
    *,
    as_of: datetime | None = None,
    hf_weak_supervision_rows: int = 0,
) -> QualificationReport:
    """读取 Phase 3.3 数据资格缺口（**严格只读**；复用 evidence_gate + 证据时间范围）。

    Args:
        session: 数据库会话（只读使用）。
        as_of: 审计时点（必须带时区）；缺省 = ``evidence_gate`` 使用当前 UTC 时间。
        hf_weak_supervision_rows: HF 标题弱监督行数（仅记录，**不参与**资格判定）。

    Raises:
        ValueError: ``as_of`` 未带时区。
    """
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise ValueError("as_of 必须包含时区")
    readiness = load_phase33_readiness(
        session, hf_weak_supervision_rows=hf_weak_supervision_rows, as_of=as_of
    )
    moment = readiness.as_of if readiness.as_of is not None else datetime.now(UTC)
    return build_qualification_report(
        readiness,
        author_evidence=_author_evidence_window(session),
        news_evidence=_news_evidence_window(session, moment),
        hf_weak_supervision_rows=hf_weak_supervision_rows,
    )


def _render_evidence(window: tuple[str | None, str | None]) -> str:
    start, end = window
    if start is None and end is None:
        return "无可用证据"
    return f"{start or '—'} → {end or '—'}"


def render_qualification_report(report: QualificationReport) -> str:
    """把资格缺口渲染为人类可读 Markdown（**不解除** blocker）。"""
    lines: list[str] = [
        "## 2. Phase 3.3 数据资格缺口（只读）",
        "",
        f"> blocker `{report.blocker_code}`：active={str(report.blocker_active).lower()}；"
        "库内数量门槛 PASS **不等于** Alpha 放行，本报告不解除 blocker。",
        "",
        f"- 审计时点：{report.as_of.isoformat() if report.as_of is not None else '—'}",
        f"- 证据时间范围（Author）：{_render_evidence(report.author_evidence)}",
        f"- 证据时间范围（News）：{_render_evidence(report.news_evidence)}",
        f"- 检查汇总：PASS={report.pass_count}、BLOCKED={report.blocked_count}",
        f"- HF 标题弱监督行数（仅记录，不参与判定）：{report.hf_weak_supervision_rows}",
        "",
        "| 检查 | 范围 | 指标 | 当前 | 要求 | 比较 | 状态 | 证据范围 | 原因 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for check in report.checks:
        lines.append(
            f"| {check.key} | {check.scope} | {check.metric} | {check.current} | "
            f"{check.required} | {check.comparator} | {check.status.value} | "
            f"{check.evidence_start or '—'} → {check.evidence_end or '—'} | {check.reason} |"
        )
    lines += [
        "",
        "### 作者可信样本明细",
        "",
        "| 作者 | 观点数 | 可信帖子 | 账号最小样本 | 归属一致 | 状态 |",
        "|---|---:|---:|---:|---|---|",
    ]
    if not report.authors:
        lines.append("| — | 0 | 0 | 0 | — | BLOCKED |")
    for author in report.authors:
        lines.append(
            f"| {author.display_name} | {author.opinions} | {author.trusted_posts} | "
            f"{author.min_account_samples} | {str(author.identity_consistent).lower()} | "
            f"{author.status.value} |"
        )
    lines += [
        "",
        "### 口径与边界",
        "",
        "- 阈值全部复用 `src/alpha/evidence_gate.py`"
        "（30 条可信帖子 / 200 事件 / 90 天 / 单源 40%），本报告不另造阈值；",
        "- 来源授权与历史可用时间证据属人工 Gate，观测层只能报 BLOCKED"
        "（不得用 Mock 判 PASS）；",
        "- 本报告不写库、不生成技能/权重/Alpha 事实，也不进入 Phase 3.4。",
        "",
    ]
    return "\n".join(lines)
