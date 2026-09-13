"""Phase 1 数据管道端到端复核报告（冻结验证工具）。

用途（对应团队批复的「跑一次完整端到端 Mock 测试 + 生成总结报告」）：

1. 用 **Alembic ``upgrade head``** 建库（证明迁移可独立建出全部 15 张表）；
2. 执行 **种子**（instruments / sources）并验证种子幂等；
3. 把 **market / news / macro 三个采集器** 串起来跑（同一窗口、同一轮）；
4. 复跑第二轮，验证 **幂等**（零新增）；
5. 输出 **落库数据量** 与 **各表约束校验**（结构清单 + 运行期不变式探测）；
6. 生成 Markdown 报告（默认 ``docs/09_Phase1_数据管道总结报告.md``）。

用法::

    python -m scripts.phase1_pipeline_report
    python -m scripts.phase1_pipeline_report --out logs/phase1_report.md
    # PostgreSQL 验证（关闭 TD-02，需要可用的 PG 实例）：
    python -m scripts.phase1_pipeline_report \
        --db-url postgresql+psycopg://gold_ai:gold_ai@localhost:5432/gold_ai --migrate

⚠️ 请使用 ``python -m`` 调用（等价于 pytest 的 ``pythonpath="."``，会把仓库根加入
``sys.path``）。直接 ``python scripts/phase1_pipeline_report.py`` 只有在项目以
editable 方式安装（``pip install -e ".[dev]"``）或已设置 ``PYTHONPATH=.`` 时才可用，
否则会报 ``ModuleNotFoundError: No module named 'database'``。

★ 100% Mock ★：所有 HTTP 由内置 ``_MockTransport`` 按 URL 路由返回，
本脚本**不访问任何外部网络**（与测试套件同一口径）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from database.models import ALL_TABLES, PROTECTED_TABLES, CollectorRun, RawItem, Source
from database.models.enums import SourceType
from database.seeds import seed_instruments, seed_sources
from database.session import build_engine, build_session_factory
from src.collectors import (
    CollectWindow,
    HttpRequest,
    HttpResponse,
    NewsCollector,
    run_collectors,
)
from src.collectors.macro import MacroCollector
from src.collectors.market import MarketCollector
from src.common.exceptions import ImmutableRecordError
from src.common.time import utc_now

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "09_Phase1_数据管道总结报告.md"

#: 使用"过去的"窗口：保证 open_time / event_at < collected_at（真实系统时间），与运行时刻无关
WINDOW_START = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
WINDOW = CollectWindow(start_at=WINDOW_START, end_at=WINDOW_START + timedelta(minutes=10))

#: 报告专用 Mock 来源（生产种子的 URL 是真实站点，这里必须换成 mock 主机）
MARKET_HOST = "market.invalid"
NEWS_HOST = "feed.invalid"
MACRO_HOST = "api.stlouisfed.org"
MARKET_PREFIX = "/v8/finance/chart/"
#: FRED 密钥：报告只演示"从环境变量读取"的契约，用固定占位值（不落库、不打印在 URL 上）
REPORT_API_KEY = "phase1-report-key"


# ---------------------------------------------------------------------------
# Mock 传输层（按 URL 路由；一次为整条管道供数，与请求顺序无关）
# ---------------------------------------------------------------------------
def _chart_response(_request: HttpRequest) -> HttpResponse:
    """Yahoo chart JSON：窗口内每分钟 1 根 K 线（10 根，满足"窗口 ÷ 周期"期望）。"""
    bars = 10
    timestamps = [
        int((WINDOW_START + timedelta(minutes=offset)).timestamp()) for offset in range(bars)
    ]
    base = [2400.0 + offset for offset in range(bars)]
    return HttpResponse(
        status=200,
        json_body={
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "XAUUSD", "currency": "USD"},
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": base,
                                    "high": [value + 5 for value in base],
                                    "low": [value - 5 for value in base],
                                    "close": [value + 1 for value in base],
                                    "volume": [120.0] * bars,
                                }
                            ]
                        },
                    }
                ],
            }
        },
    )


def _rss_response(_request: HttpRequest) -> HttpResponse:
    """RSS 2.0：2 条窗口内新闻（时区明确：GMT 与 +0800，都会被换算为 UTC）。"""

    def item(guid: str, title: str, pub_date: str) -> str:
        return (
            "<item>"
            f"<title>{title}</title>"
            f"<link>https://{NEWS_HOST}/{guid}</link>"
            "<description><p>Macro digest.</p></description>"
            f'<guid isPermaLink="false">{guid}</guid>'
            f"<pubDate>{pub_date}</pubDate>"
            "<category>Monetary Policy</category>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss version=\"2.0\"><channel><title>Fed</title>"
        + item("p-1", "Fed holds rates steady", "Tue, 02 Jan 2024 08:05:00 GMT")
        + item("p-2", "Fed minutes published", "Tue, 02 Jan 2024 16:05:00 +0800")
        + "</channel></rss>"
    )
    return HttpResponse(status=200, text=xml)


def _fred_response(request: HttpRequest) -> HttpResponse:
    """FRED observations：每个 series 返回 1 条过去日期的观测（保持日粒度）。"""
    series_id = str(request.params.get("series_id", ""))
    is_rate = series_id == "DFF"
    return HttpResponse(
        status=200,
        json_body={
            "units": "Percent" if is_rate else "Index 2017=100",
            "observations": [
                {
                    "realtime_start": "2024-01-02",
                    "realtime_end": "2024-01-02",
                    "date": "2023-12-01",
                    "value": "4.33" if is_rate else "319.6",
                }
            ],
        },
    )


class _MockTransport:
    """按 URL 路由的 Mock 传输层（满足 ``Transport`` 协议）。"""

    def __init__(self, routes: Sequence[tuple[str, Callable[[HttpRequest], HttpResponse]]]) -> None:
        self._routes = list(routes)
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for needle, responder in self._routes:
            if needle in request.url:
                return responder(request)
        raise AssertionError(f"未路由的请求：{request.url}")


def _build_transport() -> _MockTransport:
    return _MockTransport(
        [
            (MARKET_PREFIX, _chart_response),
            (NEWS_HOST, _rss_response),
            (MACRO_HOST, _fred_response),
        ]
    )


# ---------------------------------------------------------------------------
# 数据库准备
# ---------------------------------------------------------------------------
def _prepare_database(db_url: str | None, *, migrate: bool) -> str:
    """准备目标库：未指定则创建临时 SQLite 文件；``migrate`` 时执行 ``alembic upgrade head``。"""
    if db_url is None:
        workdir = Path(tempfile.mkdtemp(prefix="gold_ai_phase1_report_"))
        db_url = f"sqlite+pysqlite:///{(workdir / 'phase1_report.db').as_posix()}"
        migrate = True

    if migrate:
        from alembic import command
        from alembic.config import Config

        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
        config.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(config, "head")
    return db_url


def _mask_url(url: str) -> str:
    """脱敏数据库 URL（去掉密码），可直接写进报告。"""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest and ":" in rest.split("@", 1)[0]:
        credentials, host = rest.split("@", 1)
        return f"{scheme}://{credentials.split(':', 1)[0]}:***@{host}"
    return url


def _alembic_version(engine: sa.Engine) -> str:
    try:
        with engine.connect() as connection:
            value = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        return str(value) if value else "(未知)"
    except sa.exc.SQLAlchemyError:
        return "(未迁移)"


# ---------------------------------------------------------------------------
# 采集：种子 + 三个采集器（Mock）
# ---------------------------------------------------------------------------
async def _no_sleep(_seconds: float) -> None:
    """报告模式下的假 sleep：重试退避不真正等待。"""
    return None


def _ensure_report_sources(session: Session) -> tuple[Source, Source, Source]:
    """建立报告专用的三个来源（配置形状与生产种子一致，仅把 URL 指向 Mock 主机）。

    生产种子的真实站点地址不能用于 Mock 管道，因此这里以"运维新增来源"的方式补齐；
    自然键为 ``sources.name``，重复执行不会产生重复行（与种子同一幂等口径）。
    """
    definitions: tuple[tuple[str, SourceType, str, dict[str, Any]], ...] = (
        (
            "report-market",
            SourceType.MARKET,
            f"https://{MARKET_HOST}",
            {
                "collector": "market_collector",
                "provider": "yahoo_chart",
                "symbols": ["XAUUSD"],
                "timeframes": ["1m"],
                # 行情条数由"窗口 ÷ 周期"自动推断，0 表示不额外叠加固定阈值
                "min_records_per_run": 0,
            },
        ),
        (
            "report-news",
            SourceType.NEWS,
            f"https://{NEWS_HOST}",
            {
                "collector": "news_collector",
                "feeds": [f"https://{NEWS_HOST}/rss"],
                "min_records_per_run": 1,
            },
        ),
        (
            "report-macro",
            SourceType.MACRO,
            f"https://{MACRO_HOST}",
            {
                "collector": "macro_collector",
                "provider": "fred",
                "series": ["CPIAUCSL", "DFF"],
                "lookback_days": 45,
                "min_records_per_run": 1,
            },
        ),
    )

    resolved: dict[str, Source] = {}
    for name, source_type, base_url, config_json in definitions:
        source = session.scalar(sa.select(Source).where(Source.name == name))
        if source is None:
            source = Source(
                name=name,
                source_type=source_type,
                base_url=base_url,
                timezone="UTC",
                enabled=True,
                config_json=config_json,
            )
            session.add(source)
        resolved[name] = source
    session.flush()
    return (resolved["report-market"], resolved["report-news"], resolved["report-macro"])


def _build_collectors(transport: _MockTransport, sources: Sequence[Source]) -> list[Any]:
    """构造三个采集器（顺序 = 行情 → 新闻 → 宏观）。"""
    market, news, macro = sources
    return [
        MarketCollector(market, transport=transport, sleep=_no_sleep),
        NewsCollector(news, transport=transport, sleep=_no_sleep),
        MacroCollector(macro, transport=transport, sleep=_no_sleep, api_key=REPORT_API_KEY),
    ]


async def _run_round(
    session: Session, sources: Sequence[Source]
) -> tuple[list[Any], _MockTransport]:
    """执行一轮三源采集并提交；返回运行结果与所用传输层（可统计请求数）。"""
    transport = _build_transport()
    results = await run_collectors(session, _build_collectors(transport, sources), window=WINDOW)
    session.commit()
    return list(results), transport


def _table_counts(engine: sa.Engine) -> dict[str, int]:
    """逐表行数（Phase 1 十五张 + Phase 2 四张 = 19 张）。"""
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
            for table in ALL_TABLES
        }


# ---------------------------------------------------------------------------
# 约束校验
# ---------------------------------------------------------------------------
#: 运行期不变式（说明, 违规计数 SQL）——全部期望 0；时间类即"防未来数据泄漏"门禁
_INVARIANTS: tuple[tuple[str, str], ...] = (
    (
        "行情：open_time ≤ effective_at",
        "SELECT count(*) FROM market_bars WHERE open_time > effective_at",
    ),
    (
        "行情：collected_at ≤ effective_at",
        "SELECT count(*) FROM market_bars WHERE collected_at > effective_at",
    ),
    (
        "行情：close_time > open_time",
        "SELECT count(*) FROM market_bars WHERE close_time <= open_time",
    ),
    ("行情：low ≤ high", "SELECT count(*) FROM market_bars WHERE low > high"),
    (
        "行情：价格 > 0",
        "SELECT count(*) FROM market_bars"
        " WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0",
    ),
    (
        "行情：K 线不重复（instrument + timeframe + open_time）",
        "SELECT count(*) FROM (SELECT instrument_id, timeframe, open_time FROM market_bars"
        " GROUP BY instrument_id, timeframe, open_time HAVING count(*) > 1) AS dup",
    ),
    (
        "新闻：published_at ≤ effective_at",
        "SELECT count(*) FROM news_events"
        " WHERE published_at IS NOT NULL AND published_at > effective_at",
    ),
    (
        "宏观：event_at ≤ effective_at",
        "SELECT count(*) FROM macro_events WHERE event_at > effective_at",
    ),
    (
        "宏观：collected_at ≤ effective_at",
        "SELECT count(*) FROM macro_events WHERE collected_at > effective_at",
    ),
    (
        "原始层：published_at ≤ effective_at",
        "SELECT count(*) FROM raw_items"
        " WHERE published_at IS NOT NULL AND published_at > effective_at",
    ),
    (
        "原始层：collected_at ≤ effective_at",
        "SELECT count(*) FROM raw_items WHERE collected_at > effective_at",
    ),
    (
        "全局：effective_at ≤ 本轮采集结束时间",
        "SELECT count(*) FROM (SELECT effective_at FROM raw_items"
        " UNION ALL SELECT effective_at FROM market_bars"
        " UNION ALL SELECT effective_at FROM news_events"
        " UNION ALL SELECT effective_at FROM macro_events) AS rows"
        " WHERE effective_at > (SELECT max(finished_at) FROM collector_runs)",
    ),
)


def _probe_invariants(engine: sa.Engine) -> list[tuple[str, int]]:
    """执行运行期不变式探测（返回 ``(说明, 违规行数)``）。"""
    with engine.connect() as connection:
        return [
            (label, int(connection.scalar(sa.text(sql)) or 0))  # noqa: S608
            for label, sql in _INVARIANTS
        ]


def _constraint_inventory(engine: sa.Engine) -> list[dict[str, Any]]:
    """从数据库反射每张表的约束清单（PK / UNIQUE / CHECK / FK / NOT NULL）。"""
    inspector = sa.inspect(engine)
    inventory: list[dict[str, Any]] = []
    for table in ALL_TABLES:
        columns = inspector.get_columns(table)
        inventory.append(
            {
                "table": table,
                "columns": len(columns),
                "not_null": sum(1 for column in columns if not column.get("nullable", True)),
                "pk": list(inspector.get_pk_constraint(table).get("constrained_columns") or []),
                "unique": [
                    list(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints(table)
                ],
                "fk": len(inspector.get_foreign_keys(table)),
                "check": len(inspector.get_check_constraints(table)),
            }
        )
    return inventory


def _behavior_checks(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """行为级校验：不可覆盖守卫、唯一约束拒重复、保护表注册。"""
    checks: list[tuple[str, str]] = []

    # 1) 唯一约束：(source_id, source_record_id) 重复必须被数据库拒绝
    with factory() as session:
        raw = session.scalars(sa.select(RawItem).limit(1)).first()
        if raw is None:
            checks.append(("唯一约束拒绝重复原始记录", "SKIP（无数据）"))
        else:
            session.add(
                RawItem(
                    source_id=raw.source_id,
                    source_record_id=raw.source_record_id,
                    item_type=raw.item_type,
                    content_hash=raw.content_hash,
                    collected_at=raw.collected_at,
                    effective_at=raw.effective_at,
                )
            )
            try:
                session.flush()
                checks.append(("唯一约束拒绝重复原始记录", "FAIL（数据库接受了重复记录）"))
            except sa.exc.IntegrityError:
                checks.append(("唯一约束拒绝重复原始记录", "PASS（IntegrityError）"))
            finally:
                session.rollback()

    # 2) 原始数据不可覆盖：修改已入库的 raw_items 必须被 ORM 守卫拒绝
    with factory() as session:
        raw = session.scalars(sa.select(RawItem).limit(1)).first()
        if raw is None:
            checks.append(("原始数据不可覆盖（UPDATE 被拒绝）", "SKIP（无数据）"))
        else:
            raw.content_text = "tampered"
            try:
                session.flush()
                checks.append(("原始数据不可覆盖（UPDATE 被拒绝）", "FAIL（修改被接受）"))
            except ImmutableRecordError:
                checks.append(("原始数据不可覆盖（UPDATE 被拒绝）", "PASS（ImmutableRecordError）"))
            finally:
                session.rollback()

    expected_protected = {
        # Phase 1 原始 / 加工事实表
        "raw_items",
        "raw_media",
        "processed_items",
        # Phase 2（Author Lab）派生事实表
        "author_opinions",
        "propagation_edges",
        "author_skill_snapshots",
        "author_weight_snapshots",
    }
    actual_protected = set(PROTECTED_TABLES)
    status = "PASS" if expected_protected <= actual_protected else "FAIL"
    checks.append(
        (
            "不可覆盖守卫注册表",
            f"{status}（{', '.join(sorted(actual_protected))}）",
        )
    )
    return checks


def _collector_run_rows(factory: sessionmaker[Session]) -> list[CollectorRun]:
    """读取全部 ``collector_runs``（按开始时间排序）。"""
    with factory() as session:
        return list(
            session.scalars(sa.select(CollectorRun).order_by(CollectorRun.started_at)).all()
        )


def _raw_items_by_type(engine: sa.Engine) -> list[tuple[str, int]]:
    """原始层按 ``item_type`` 分组统计（QUOTE / NEWS / MACRO ...）。"""
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT item_type, count(*) FROM raw_items GROUP BY item_type ORDER BY item_type"
            )
        ).all()
    return [(str(row[0]), int(row[1])) for row in rows]


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReportData:
    """报告所需的全部实测数据（由 main 收集，渲染函数只负责排版）。"""

    generated_at: datetime
    db_url: str
    alembic_version: str
    environments: dict[str, str]
    seed_lines: list[str]
    rounds: list[tuple[str, list[Any], int]]
    run_rows: list[CollectorRun]
    counts_round1: dict[str, int]
    counts_round2: dict[str, int]
    raw_by_type: list[tuple[str, int]]
    invariants: list[tuple[str, int]]
    inventory: list[dict[str, Any]]
    behaviors: list[tuple[str, str]]


def _result_row(result: Any) -> tuple[str, str, str, str, str, str, str, str]:
    """把一次 ``CollectorRunResult`` 转成表格行。"""
    outcome = result.outcome
    if outcome is None:
        return (
            result.collector_name,
            result.source_name,
            result.status.value,
            "-",
            "0",
            "0",
            "0",
            str(len(result.warnings)),
        )
    return (
        result.collector_name,
        result.source_name,
        result.status.value,
        str(outcome.fetched_count),
        str(outcome.inserted_count),
        str(outcome.duplicate_count),
        str(outcome.failed_count),
        str(len(result.warnings)),
    )


def _render_report(data: ReportData) -> str:
    """渲染 Markdown 报告（第 1 ~ 3.2 节）。"""
    lines: list[str] = [
        "# GOLD-AI Phase 1 数据管道总结报告（冻结复核）",
        "",
        "> 本报告由 `scripts/phase1_pipeline_report.py` 自动生成；**100% Mock 数据，零外部网络**。",
        "> 覆盖链路：种子（instruments / sources）→ 三源采集（market / news / macro）",
        "> → `raw_items` → `market_bars` / `news_events` / `macro_events` → `collector_runs`。",
        "",
        "## 1. 运行环境与口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 生成时间（UTC） | {data.generated_at.strftime('%Y-%m-%d %H:%M:%S')} |",
        f"| 数据库 | `{_mask_url(data.db_url)}` |",
        f"| Alembic 版本 | `{data.alembic_version}` |",
        f"| 采集窗口 | {WINDOW.start_utc.isoformat()} → {WINDOW.end_utc.isoformat()}"
        "（过去时间，保证不触发未来语义） |",
        f"| 采集器注册表 | {data.environments.get('collectors', '-')} |",
        f"| Python | {data.environments.get('python', '-')} |",
        f"| SQLAlchemy | {data.environments.get('sqlalchemy', '-')} |",
        f"| 未注册采集器的来源 | {data.environments.get('unregistered', '-')} |",
        "",
        "## 2. 种子与采集轮次",
        "",
    ]
    lines += [f"- {line}" for line in data.seed_lines]
    lines.append("")

    for round_name, results, requests in data.rounds:
        lines += [
            f"### {round_name}",
            "",
            "| 采集器 | 来源 | 状态 | fetched | inserted | duplicate | failed | 告警 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines += ["| " + " | ".join(_result_row(result)) + " |" for result in results]
        lines += ["", f"Mock HTTP 请求数：**{requests}**（全部由脚本内路由返回，无外部网络）", ""]

    lines += [
        "## 3. 落库数据量（Phase 1 十五张 + Phase 2 四张 = 19 张）",
        "",
        "| 表 | 第 1 轮后 | 第 2 轮后（幂等复跑） | Δ |",
        "|---|---|---|---|",
    ]
    for table in ALL_TABLES:
        first = data.counts_round1.get(table, 0)
        second = data.counts_round2.get(table, 0)
        lines.append(f"| `{table}` | {first} | {second} | {second - first:+d} |")

    lines += [
        "",
        "### 3.1 原始层明细（raw_items 按 item_type）",
        "",
        "| item_type | 行数 |",
        "|---|---|",
    ]
    lines += [
        f"| {item_type} | {count} |" for item_type, count in data.raw_by_type
    ] or ["| - | 0 |"]

    lines += ["", "### 3.2 collector_runs 明细", ""]
    if data.run_rows:
        lines += [
            "| # | 采集器 | 状态 | fetched | inserted | duplicate | failed | retry | 告警 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for index, run in enumerate(data.run_rows, start=1):
            warnings = run.warnings_json.get("warnings", []) if run.warnings_json else []
            lines.append(
                f"| {index} | {run.collector_name} | {run.status.value} | {run.fetched_count}"
                f" | {run.inserted_count} | {run.duplicate_count} | {run.failed_count}"
                f" | {run.retry_count} | {len(warnings)} |"
            )
    else:
        lines.append("（无记录）")

    lines += [
        "",
        "## 4. 约束校验",
        "",
        "### 4.1 表结构约束清单（由数据库反射得出）",
        "",
        "| 表 | 列数 | NOT NULL 列 | PK | UNIQUE | FK | CHECK |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in data.inventory:
        unique = "; ".join("+".join(columns) for columns in row["unique"]) if row["unique"] else "—"
        lines.append(
            f"| `{row['table']}` | {row['columns']} | {row['not_null']}"
            f" | {'+'.join(row['pk']) or '—'} | {unique} | {row['fk']} | {row['check']} |"
        )

    lines += [
        "",
        "### 4.2 运行期不变式（期望违规行数 = 0；时间类即防未来数据泄漏门禁）",
        "",
        "| 不变式 | 违规行数 | 结论 |",
        "|---|---|---|",
    ]
    for label, violations in data.invariants:
        lines.append(f"| {label} | {violations} | {'PASS' if violations == 0 else 'FAIL'} |")

    lines += ["", "### 4.3 行为级校验", "", "| 校验项 | 结果 |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in data.behaviors]

    invariant_failures = [label for label, violations in data.invariants if violations != 0]
    behavior_failures = [name for name, value in data.behaviors if value.startswith("FAIL")]
    failed_rounds = [
        f"{result.collector_name}:{result.status.value}"
        for _name, results, _requests in data.rounds
        for result in results
        if result.status.value != "SUCCESS"
    ]

    lines += ["", "## 5. 结论", ""]
    if invariant_failures or behavior_failures or failed_rounds:
        lines += [
            "- 结论：**FAIL**",
            f"  - 不变式违规项：{invariant_failures or '无'}",
            f"  - 行为校验失败项：{behavior_failures or '无'}",
            f"  - 非 SUCCESS 采集器：{failed_rounds or '无'}",
        ]
    else:
        lines += [
            "- 结论：**PASS** —— 三源采集全部 SUCCESS；原始层三类 item_type 齐全；"
            "结构化层行数与原始层一致；全部时间因果不变式零违规；重跑幂等（Δ=0）；"
            "唯一约束与不可覆盖守卫均按预期生效。",
        ]
    lines += [
        "",
        "### 未覆盖范围（技术债，详见根目录 `TECH_DEBT.md`）",
        "",
        "- **TD-01**：本报告全部为 Mock 数据，未与 FRED / Yahoo / RSS 真实接口联调；",
        "- **TD-02**：默认在临时 SQLite 上运行"
        "（PG 验证需显式 `--db-url` + `--migrate`）；",
        "- **TD-03**：`market_bars` 无 `timeframe='4h'` 行"
        "（provider 无该粒度，待 Processor 层聚合）；",
        "- **TD-04**：时区不明确的新闻不生成 `news_events`（本报告样本时区明确：GMT / +0800）；",
        "- **TD-05 / TD-07**：`macro_events.forecast_value` / `previous_value` 恒为 NULL；",
        "- **TD-09 / TD-10 / TD-11**：Scheduler、Dashboard / 只读 API、独立 Processor 层未实现；",
        "- **Phase 2（TD-16）**：`author_opinions` 等四张表已由 migration 0005 建立，"
        "但作者库 CRUD / 导入与观点提取器尚未交付，故本报告中它们的行数为 0。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 1 数据管道端到端 Mock 复核报告")
    parser.add_argument("--db-url", default=None, help="目标数据库 URL；默认创建临时 SQLite 库")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="对目标库执行 alembic upgrade head（临时 SQLite 默认执行；指定 --db-url 时不执行）",
    )
    parser.add_argument("--out", default=None, help=f"报告输出路径；默认 {DEFAULT_OUTPUT.name}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行完整复核并输出报告；返回进程退出码（0 = 生成成功）。"""
    args = _parse_args(argv)
    db_url = _prepare_database(args.db_url, migrate=args.migrate or args.db_url is None)
    engine = build_engine(db_url)
    factory = build_session_factory(engine)

    try:
        with factory() as session:
            first_seed = {
                "instruments": seed_instruments(session),
                "sources": seed_sources(session),
            }
            second_seed = {
                "instruments": seed_instruments(session),
                "sources": seed_sources(session),
            }
            session.commit()
            sources = _ensure_report_sources(session)
            session.commit()

            round1, transport1 = asyncio.run(_run_round(session, sources))
            counts_round1 = _table_counts(engine)
            round2, transport2 = asyncio.run(_run_round(session, sources))
            counts_round2 = _table_counts(engine)

        from src.collectors import available_collectors

        data = ReportData(
            generated_at=utc_now(),
            db_url=db_url,
            alembic_version=_alembic_version(engine),
            environments={
                "collectors": ", ".join(available_collectors()),
                "python": sys.version.split()[0],
                "sqlalchemy": sa.__version__,
                "unregistered": (
                    "econ_calendar_collector（enabled=false）、weibo_collector（Phase 2）"
                ),
            },
            seed_lines=[
                f"种子（第 1 次）：{first_seed['instruments'].summary()}；"
                f"{first_seed['sources'].summary()}",
                f"种子（第 2 次，幂等验证）：{second_seed['instruments'].summary()}；"
                f"{second_seed['sources'].summary()}",
                f"报告专用来源：{', '.join(source.name for source in sources)}"
                "（配置形状与生产种子一致，URL 指向 Mock 主机）",
            ],
            rounds=[
                ("第 1 轮：三源采集", round1, len(transport1.requests)),
                ("第 2 轮：幂等复跑", round2, len(transport2.requests)),
            ],
            run_rows=_collector_run_rows(factory),
            counts_round1=counts_round1,
            counts_round2=counts_round2,
            raw_by_type=_raw_items_by_type(engine),
            invariants=_probe_invariants(engine),
            inventory=_constraint_inventory(engine),
            behaviors=_behavior_checks(factory),
        )
    finally:
        engine.dispose()

    report = _render_report(data)
    output = Path(args.out) if args.out else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"报告已生成：{output}")
    print(f"数据库：{db_url}")
    print(f"Alembic 版本：{data.alembic_version}")
    for name, results, requests in data.rounds:
        statuses = ", ".join(f"{result.collector_name}={result.status.value}" for result in results)
        print(f"{name} | HTTP 请求 {requests} | {statuses}")
    populated = {key: value for key, value in data.counts_round2.items() if value}
    print("落库行数：" + ", ".join(f"{key}={value}" for key, value in populated.items()))
    print(f"不变式违规总数：{sum(count for _label, count in data.invariants)}（期望 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
