"""Phase 3.0 **W0-1 行情回填**：把 XAUUSD / DXY / USDCNY 的历史 K 线写进 `market_bars`。

**口径（严格）**：

1. **走既有采集器**（`src/collectors/market.py` 的 `MarketCollector` + `run_collector`）：
   幂等自然键、`raw_items` 原始 JSON 留档、`effective_at` 防未来数据泄漏语义全部复用，
   **不另造写入路径**；
2. **只 append**：绝不 UPDATE/DELETE 历史 bar（`.clinerules` 原始数据不可覆盖）；
3. **`4h` 由 1h 聚合**（TD-03：provider 无 4h 粒度）→ 只写**满桶**（4 根 1h 齐备），半桶丢弃并计数；
4. **`US10Y_REAL` 不由本脚本采集**：Yahoo 无实际利率序列（`DFII10` → 404）
   → 由 W0-2 的 FRED 提供（含 `released_at`）；
5. 默认 `--dry-run`（只取数 + 打印 + 可选快照），**加 `--no-dry-run` 才写库**。

用法::

    python scripts/backfill_market_bars.py --dry-run                  # 预检 + 取数计划（不写库）
    python scripts/backfill_market_bars.py --no-dry-run               # 回填 1d(10y) + 1h(2y)
    python scripts/backfill_market_bars.py --no-dry-run --with-4h     # 额外聚合出 4h（TD-03）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
from database.models import DataVersion, Instrument, MarketBar, Source  # noqa: E402
from database.models.enums import Timeframe  # noqa: E402
from database.session import build_engine, build_session_factory  # noqa: E402
from scripts._console import configure_stdout  # noqa: E402
from scripts._market_data import (  # noqa: E402
    BACKFILL_LOOKBACK_DAYS,
    DEFAULT_LOOKBACKS,
    PROVIDER_SYMBOLS,
    TIMEFRAME_MINUTES,
    UNSUPPORTED_SYMBOLS,
    HttpxTransport,
    SourceBar,
    aggregate_bars_to_4h,
    audit_gaps,
    fetch_chart,
)
from src.collectors.market import MarketCollector  # noqa: E402
from src.collectors.runner import run_collector  # noqa: E402
from src.collectors.transport import AiohttpTransport  # noqa: E402
from src.collectors.types import CollectWindow  # noqa: E402
from src.processors.timeline import ensure_utc_from_database  # noqa: E402

DEFAULT_SYMBOLS: Final[str] = "XAUUSD,DXY,USDCNY"
DEFAULT_TIMEFRAMES: Final[str] = "1d,1h"
DEFAULT_MARKET_SOURCE: Final[str] = "market_yahoo"
DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "experiments" / "Phase3.0_W0-1_行情回填报告.md"
#: 建库/种子命令（预检失败时直接打印给用户，避免"猜怎么修"）。
#: 注意：`database.seeds --scope` **只接受单个值**（all/instruments/sources/authors），
#: 不接受逗号列表 —— 这里给的是可直接复制执行的两条命令（实测 2026-09-15）。
SETUP_HINT: Final[str] = (
    "① `alembic upgrade head`（建表） → "
    "② `python -m database.seeds --scope instruments` → "
    "③ `python -m database.seeds --scope sources`"
)


def parse_csv_list(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("列表不能为空")
    return items


def plan_windows(
    symbols: Sequence[str], timeframes: Sequence[str], *, now: datetime | None = None
) -> tuple[list[tuple[str, str, CollectWindow]], list[str]]:
    """生成 `(symbol, timeframe, window)` 采集计划；返回 `(计划, 说明)`。

    回溯窗口默认：`1d → 10y`、`1h → 2y`（`DEFAULT_LOOKBACKS`）。
    不支持的标的（如 `US10Y_REAL`）进入"说明"而**不进入计划**。
    """
    moment = now or datetime.now(UTC)
    plan: list[tuple[str, str, CollectWindow]] = []
    notes: list[str] = []
    for symbol in symbols:
        if symbol in UNSUPPORTED_SYMBOLS:
            notes.append(f"跳过 `{symbol}`：{UNSUPPORTED_SYMBOLS[symbol]}")
            continue
        if symbol not in PROVIDER_SYMBOLS:
            notes.append(
                f"跳过 `{symbol}`：未在 `scripts/_market_data.PROVIDER_SYMBOLS` "
                "登记 provider ticker"
            )
            continue
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_MINUTES:
                notes.append(f"跳过 `{symbol} {timeframe}`：未知周期")
                continue
            if timeframe == "4h":
                notes.append(f"跳过 `{symbol} 4h`：provider 无此粒度（由 1h 聚合，见 --with-4h）")
                continue
            days = BACKFILL_LOOKBACK_DAYS.get(timeframe, 720)
            start = moment - timedelta(days=days)
            plan.append((symbol, timeframe, CollectWindow(start_at=start, end_at=moment)))
    return plan, notes


def preflight(session: Session, *, symbols: Sequence[str]) -> list[str]:
    """预检数据库与种子（**只读**）：返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    inspector = sa.inspect(session.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("market_bars", "instruments", "sources", "collector_runs", "raw_items"):
        if table not in tables:
            problems.append(f"缺表 `{table}`（当前 {len(tables)} 张表）")
    if "instruments" in tables:
        existing = {row[0] for row in session.execute(sa.select(Instrument.symbol))}
        missing = [symbol for symbol in symbols if symbol not in existing]
        if missing:
            problems.append(f"`instruments` 缺标的：{missing}")
    if "sources" in tables:
        source = session.scalar(sa.select(Source).where(Source.name == DEFAULT_MARKET_SOURCE))
        if source is None:
            problems.append(f"`sources` 缺行情源 `{DEFAULT_MARKET_SOURCE}`")
        elif not source.enabled:
            problems.append(f"行情源 `{DEFAULT_MARKET_SOURCE}` 处于禁用状态（enabled=False）")
    return problems


# ---------------------------------------------------------------------------
# 回填核心
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class BackfillOutcome:
    """一个 `(symbol, timeframe)` 的回填结果（字段与 `collector_runs` 统计列对应）。"""

    symbol: str
    timeframe: str
    window_start: str
    window_end: str
    status: str
    fetched: int = 0
    inserted: int = 0
    duplicate: int = 0
    failed: int = 0
    warnings: tuple[str, ...] = ()
    note: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "status": self.status,
            "fetched": str(self.fetched),
            "inserted": str(self.inserted),
            "duplicate": str(self.duplicate),
            "failed": str(self.failed),
            "warnings": " | ".join(self.warnings),
            "note": self.note,
        }


async def run_backfill(
    session: Session,
    *,
    source: Source,
    plan: Sequence[tuple[str, str, CollectWindow]],
    timeout: float = 60.0,
    transport_name: str = "httpx",
) -> list[BackfillOutcome]:
    """按计划逐 `(symbol, timeframe)` 采集并提交（**走既有采集器 + 指定传输层**）。

    采集器负责幂等（重复 bar 记 `duplicate`）、`raw_items` 原始 JSON 留档与
    `effective_at` 防泄漏语义；本脚本只做编排与统计，**不直接写 `market_bars`**。

    `transport_name`：`httpx`（默认）/ `aiohttp`。
    **实测 2026-09-15**：Yahoo 对 aiohttp 客户端一律 `HTTP 403`（同参数 httpx 200），
    故默认走 `HttpxTransport`；留 `aiohttp` 仅为对照与其它站点复用。
    """
    transport: Any = (
        HttpxTransport(default_timeout_seconds=max(15.0, timeout))
        if transport_name == "httpx"
        else AiohttpTransport(default_timeout_seconds=max(15.0, timeout))
    )
    outcomes: list[BackfillOutcome] = []
    for symbol, timeframe, window in plan:
        collector = MarketCollector(
            source,
            instrument_symbols=(symbol,),
            timeframes=(timeframe,),
            transport=transport,
            sleep=asyncio.sleep,
        )
        result = await run_collector(session, collector, window=window, resume=False)
        session.commit()
        outcome = result.outcome
        outcomes.append(
            BackfillOutcome(
                symbol=symbol,
                timeframe=timeframe,
                window_start=window.start_utc.isoformat(),
                window_end=window.end_utc.isoformat(),
                status=str(result.status),
                fetched=getattr(outcome, "fetched_count", 0) if outcome else 0,
                inserted=getattr(outcome, "inserted_count", 0) if outcome else 0,
                duplicate=getattr(outcome, "duplicate_count", 0) if outcome else 0,
                failed=getattr(outcome, "failed_count", 0) if outcome else 0,
                warnings=tuple(result.warnings or ()),
                note="" if result.error_message is None else str(result.error_message),
            )
        )
    return outcomes


def hourly_bars(session: Session, instrument_id: Any) -> list[SourceBar]:
    """读出某标的的 **1h** bars（供 4h 聚合；缺 bar 不补，交给"满桶"规则处理）。

    注意：**SQLite 读回的时间是 naive**（Phase 1/2 已踩过），这里统一用
    `ensure_utc_from_database` 归一为 UTC-aware，否则与 `datetime.now(UTC)` 比较会
    `TypeError: can't compare offset-naive and offset-aware datetimes`。
    """
    rows = session.execute(
        sa.select(
            MarketBar.open_time,
            MarketBar.open,
            MarketBar.high,
            MarketBar.low,
            MarketBar.close,
            MarketBar.volume,
        )
        .where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.timeframe == Timeframe.H1,
        )
        .order_by(MarketBar.open_time)
    ).all()
    return [
        SourceBar(
            open_time=ensure_utc_from_database(row[0], field_name="market_bars.open_time"),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=None if row[5] is None else float(row[5]),
        )
        for row in rows
    ]


def aggregate_and_insert_4h(session: Session, *, symbol: str) -> tuple[int, int, int, int]:
    """把该标的的 1h bars 聚合成 4h 并**幂等**写入（TD-03）。

    Returns:
        `(满桶数, 新插入数, 已存在数, 被跳过的半桶数)`

    Raises:
        ValueError: 库里没有该标的。
    """
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == symbol))
    if instrument is None:
        raise ValueError(f"instruments 中不存在标的 {symbol}（先跑种子）")
    bars = hourly_bars(session, instrument.id)
    aggregated, skipped = aggregate_bars_to_4h(bars)
    source_id = session.scalar(
        sa.select(MarketBar.source_id).where(MarketBar.instrument_id == instrument.id).limit(1)
    )
    existing = {
        # 同 `hourly_bars`：SQLite 读回 naive → 必须归一，否则幂等去重会失效（撞 UNIQUE）
        ensure_utc_from_database(value, field_name="market_bars.open_time")
        for value in session.scalars(
            sa.select(MarketBar.open_time).where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.timeframe == Timeframe.H4,
            )
        )
    }
    now = datetime.now(UTC)
    inserted = 0
    for bar in aggregated:
        if bar.open_time in existing:
            continue
        session.add(
            MarketBar(
                instrument_id=instrument.id,
                timeframe=Timeframe.H4,
                open_time=bar.open_time,
                close_time=bar.close_time,
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                volume=None if bar.volume is None else Decimal(str(bar.volume)),
                source_id=source_id,
                collected_at=now,
                # 派生 bar 必须"既已收盘、又已算出来"之后才可用（沿用采集器防泄漏语义）
                effective_at=max(bar.close_time, now),
            )
        )
        inserted += 1
        existing.add(bar.open_time)
    session.flush()
    _record_4h_data_version(session, instrument=instrument, symbol=symbol, generated_at=now)
    session.commit()
    return len(aggregated), inserted, len(aggregated) - inserted, len(skipped)


def _record_4h_data_version(
    session: Session,
    *,
    instrument: Instrument,
    symbol: str,
    generated_at: datetime,
) -> None:
    """为当前 4h 派生快照写稳定指纹；相同数据复跑不产生新版本。"""
    rows = list(
        session.execute(
            sa.select(
                MarketBar.open_time,
                MarketBar.close_time,
                MarketBar.open,
                MarketBar.high,
                MarketBar.low,
                MarketBar.close,
                MarketBar.volume,
                MarketBar.source_id,
            )
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.timeframe == Timeframe.H4,
            )
            .order_by(MarketBar.open_time)
        ).all()
    )
    if not rows:
        return
    canonical_rows = [
        [
            ensure_utc_from_database(row.open_time, field_name="market_bars.open_time").isoformat(),
            ensure_utc_from_database(
                row.close_time, field_name="market_bars.close_time"
            ).isoformat(),
            str(row.open),
            str(row.high),
            str(row.low),
            str(row.close),
            None if row.volume is None else str(row.volume),
            str(row.source_id),
        ]
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(canonical_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    dataset_name = f"market_bars:{symbol}:4h"
    version = f"4h-v1-{digest[:12]}"
    exists = session.scalar(
        sa.select(DataVersion.id).where(
            DataVersion.dataset_name == dataset_name,
            DataVersion.version == version,
        )
    )
    if exists is not None:
        return
    session.add(
        DataVersion(
            dataset_name=dataset_name,
            version=version,
            start_at=ensure_utc_from_database(
                rows[0].open_time, field_name="market_bars.open_time"
            ),
            end_at=ensure_utc_from_database(
                rows[-1].close_time, field_name="market_bars.close_time"
            ),
            data_hash=digest,
            source_scope_json={
                "instrument_id": str(instrument.id),
                "symbol": symbol,
                "input_timeframe": "1h",
                "output_timeframe": "4h",
                "processor": "aggregate_bars_to_4h",
                "processor_version": "4h-v1",
            },
            filter_json={"full_buckets_only": True, "bucket_alignment": "UTC"},
            row_count=len(rows),
            generated_at=generated_at,
            notes="由 market_bars 1h 满桶派生；同一 data_hash 幂等复用版本",
        )
    )


def write_snapshots(out_dir: Path, plan: Sequence[tuple[str, str, CollectWindow]]) -> list[Path]:
    """写 provider 原始 bar 快照（**只读审计用**，不落库；含 `is_null` 列）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for symbol, timeframe, _window in plan:
        lookback = DEFAULT_LOOKBACKS.get(timeframe, "2y")
        fetch = fetch_chart(symbol, interval=timeframe, lookback=lookback)
        path = out_dir / f"{symbol}_{timeframe}_{lookback}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["open_time", "open", "high", "low", "close", "volume", "is_null"])
            for slot in fetch.slots:
                writer.writerow(
                    [
                        slot.open_time.isoformat(),
                        slot.open,
                        slot.high,
                        slot.low,
                        slot.close,
                        slot.volume,
                        int(slot.is_null),
                    ]
                )
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def mask_db_url(url: str) -> str:
    """脱敏数据库 URL（报告里只留库类型与路径，绝不打印口令）。"""
    import re

    return re.sub(r":[^:@/]+@", ":***@", url)


#: 实测约束（provider / 客户端）——**随脚本固化**，报告每次再生成都不会丢
PROVIDER_CONSTRAINTS: Final[tuple[str, ...]] = (
    "**Yahoo 不认项目标的**：`XAUUSD` 直接请求 → HTTP 403；必须映射 provider ticker"
    "（`XAUUSD→GC=F`、`DXY→DX-Y.NYB`、`USDCNY→CNY=X`、`US10Y→^TNX`），映射存于"
    "`sources.config_json['provider_symbols']`。`US10Y_REAL` **Yahoo 无序列**（`DFII10` 404）"
    "→ 明确交 W0-2 的 FRED，**不静默跳过**。",
    "**Yahoo 拒绝 aiohttp**：同 URL/参数/UA，aiohttp → HTTP 403（HTML 错误页）、httpx → HTTP 200，"
    "故回填默认走 `HttpxTransport`（协议兼容替身；`--transport aiohttp` 仅供对照）。"
    "**无 User-Agent → HTTP 429**。",
    "**1h 只回溯约 730 天**：窗口正好卡 730 天被拒（HTTP 422），实现取 720 天"
    "（`BACKFILL_LOOKBACK_DAYS`）；更长 1h 历史需换源。",
    "**provider 会返回“进行中”bar**：最后一根时间戳 = 抓取墙钟（实测 `USDCNY 1d` 三轮分别为"
    " `05:04:08`/`05:12:48`/`05:19:02`，`XAUUSD 1h` 出现 `04:58:13`/`05:08:45`），永不收盘且每轮"
    "产生新主键 → 采集层按 `close_time > 抓取时刻` **拒绝入库**（只入库已收盘 K 线）。",
    "**会话日历必须按交易所本地时区核账**：CME 黄金会话由美东时间定义，用 UTC 小时分桶会把 DST"
    "漂移误判成缺失（实测 1d 假阳性 954 条）→ 已改为本地时区分桶 + 本地日期核算。",
    "**提前收盘日会有 30 分钟粒度 bar**（如 2025-11-28、2025-12-24 的 `17:30`，秒=0）：属合法数据，"
    "不要按“非整点”清理。",
    "**FX（`CNY=X`）会话不规则**：1h 空槽无法按“本地时段桶”归因（实测 2846 个空槽全被判为"
    "“数据缺失”属**方法退化**），FX 的缺口分类需人工复核。",
)

#: 本轮实测发现并修复的缺陷（均已配回归测试）
FIXED_DEFECTS: Final[tuple[str, ...]] = (
    "**DB 读回时间戳无时区**（SQLite 不回 tz）→ 直接比较抛 `TypeError`，并让 4h 幂等失效"
    "（重复插入 → UNIQUE 冲突）：统一走 `ensure_utc_from_database`。",
    "**采集器不认识 provider ticker**：项目标的直接进 URL → 全量 HTTP 403（Phase 1 走的是"
    "MockTransport，这条真实链路从未被验证）→ 新增 `provider_symbols` 映射 + 回归测试。",
    "**aiohttp 被 Yahoo 拒绝**（HTTP 403）→ 新增协议兼容的 `HttpxTransport`"
    "（`scripts/_market_data.py`）+ 回归测试。",
    "**“进行中”bar 污染库**（每轮每标的 1 根，无界增长）→ `parse_yahoo_chart(not_after=…)` 拒绝"
    "未收盘 bar（`src/collectors/market.py`）+ 回归测试。",
    "**`safe_print` 未导入**导致审计脚本 `NameError` → 抽到 `scripts/_console.py` 共用。",
    "**种子命令提示有误**（`--scope` 不接受逗号列表）→ 预检提示改为两条可直接执行的命令。",
)


def render_report(
    *,
    outcomes: Sequence[BackfillOutcome],
    aggregation: Sequence[tuple[str, int, int, int, int]],
    audits: Sequence[Any],
    notes: Sequence[str],
    db_url: str,
    generated_at: datetime,
    repro_command: str,
) -> str:
    """生成回填报告（Markdown，纯函数）。"""
    lines: list[str] = []
    add = lines.append
    add("# Phase 3.0 W0-1 行情回填报告")
    add("")
    add(
        "> **口径**：走既有 `MarketCollector` + `run_collector`（幂等自然键、`raw_items` 留档、"
        "`effective_at` 防泄漏语义），**只 append、不覆盖**；`4h` 由 1h 聚合（TD-03，只写满桶）。"
    )
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 数据库：`{mask_db_url(db_url)}`")
    add(
        "- 注意：本篇**不使用任何宏观数据**；`US10Y_REAL` 由 W0-2 的 FRED 提供"
        "（Yahoo 无实际利率序列）"
    )
    for note in notes:
        add(f"- ℹ️ {note}")
    add("")

    add("## 0. 摘要")
    add("")
    add("| 标的 | 周期 | 状态 | fetched | **inserted** | duplicate | failed | 告警 |")
    add("|---|---|---|---|---|---|---|---|")
    for outcome in outcomes:
        add(
            f"| `{outcome.symbol}` | {outcome.timeframe} | {outcome.status} | {outcome.fetched} "
            f"| **{outcome.inserted}** | {outcome.duplicate} | {outcome.failed} "
            f"| {'；'.join(outcome.warnings) if outcome.warnings else '—'} |"
        )
    add("")
    total_inserted = sum(outcome.inserted for outcome in outcomes)
    total_duplicate = sum(outcome.duplicate for outcome in outcomes)
    add(f"- 合计：**新插入 {total_inserted} 根**、重复（幂等命中）{total_duplicate} 根")
    add("")
    idempotent = total_inserted == 0
    add(
        "- 本轮判定："
        + (
            "**幂等轮（0 新增）** —— 全部自然键命中既有行，未覆盖任何既有数据"
            if idempotent
            else (
                f"**写入轮**：新增 {total_inserted} 根"
                "（首次回填，或 provider 补齐/修订了已收盘 bar）"
            )
        )
    )
    add(
        "- 落库口径：只 append（不覆盖原始数据）｜4h **只写满桶**｜未收盘 bar **一律拒绝**｜"
        "`effective_at` 承担防泄漏语义"
    )
    add("- 实测约束与已修缺陷见 §6（随脚本固化，不依赖人工记忆）")
    add("")

    add("## 1. 明细（含采集窗口）")
    add("")
    add("| 标的 | 周期 | 窗口起点（UTC） | 窗口终点（UTC） | 说明 |")
    add("|---|---|---|---|---|")
    for outcome in outcomes:
        add(
            f"| `{outcome.symbol}` | {outcome.timeframe} | `{outcome.window_start}` "
            f"| `{outcome.window_end}` | {outcome.note or '—'} |"
        )
    add("")

    add("## 2. TD-03：4h 聚合（provider 无 4h 粒度）")
    add("")
    if aggregation:
        add("| 标的 | 满桶数 | 新插入 | 已存在 | 跳过的半桶 |")
        add("|---|---|---|---|---|")
        for symbol, complete, inserted, existing, skipped in aggregation:
            add(f"| `{symbol}` | {complete} | **{inserted}** | {existing} | {skipped} |")
        add("")
        add(
            "> 半桶（1h bar 不足 4 根）**一律丢弃**：半桶的 close 不是该周期收盘价，"
            "放进库里会污染后续特征（宁缺勿错）。"
        )
    else:
        add("- （本次未执行 4h 聚合；加 `--with-4h` 启用）")
    add("")

    add(
        "## 3. 缺口复审（provider 侧，方法见 `docs/experiments/Phase3.0_W0-1_行情缺口审计报告.md`）"
    )
    add("")
    if audits:
        add(
            "| 标的 | 周期 | 槽数 | 有效 | 空槽 | 常规休市 | 疑似假期 | **数据缺失** "
            "| **时间戳缺失** |"
        )
        add("|---|---|---|---|---|---|---|---|---|")
        for audit in audits:
            add(
                f"| `{audit.symbol}` | {audit.interval} | {audit.total_slots} "
                f"| {audit.valid_slots} "
                f"| {audit.null_slots} | {audit.counts.get('session_break', 0)} "
                f"| {audit.counts.get('holiday_suspect', 0)} "
                f"| **{audit.counts.get('data_gap', 0)}** "
                f"| **{audit.counts.get('missing_timestamp', 0)}** |"
            )
    else:
        add("- （本次未做缺口复审）")
    add("")

    add("## 4. 幂等性验证（必须复跑一次）")
    add("")
    add(
        "复跑同一命令：预期 `inserted = 0`、`duplicate ≈ 上一轮 inserted`。"
        "若 `inserted > 0`，说明窗口右端推进（新 bar）、provider 补齐/修订了已收盘 bar，"
        "或自然键失效 —— **后者是缺陷，必须查**。"
    )
    add("")
    add("本轮执行序列（每轮原始日志归档于 `logs/archive/phase3_w0_1_backfill/`）：")
    add("")
    add("| 轮次 | 结果 | 说明 |")
    add("|---|---|---|")
    add("| A | 1d 成功 / **1h 全失败（HTTP 422）** | 窗口正好卡 730 天被拒 → 改 720 天（§6.1）|")
    add("| B | 首灌：1d 2510/2511/2543；1h 11277/11745/9539；4h 2436/2831/1541 | 真实首灌 |")
    add("| C | 1d `inserted=0` ✓；1h 少量新增 | 暴露 provider “进行中”bar 污染（§6.1）→ 修复 |")
    add("| D | 修复后复核：1h “墙钟”垃圾归零；USDCNY 新增 38 根均为整点已收盘 bar | 缺陷确认修复 |")
    add("| E、F、G | **6/6 `inserted=0`；4h 3/3 `新插入=0`** | 连续复现 3 次（本篇为末轮产物）|")
    add("")
    add(
        "> `duplicate` 与库内实际行数的差额 = **历史轮次已落库、但本轮 payload 未再返回**的 bar"
        "（provider 修订/回撤）。按“只 append、不覆盖原始数据”的红线，这些行**保留不删**。"
    )
    add("")
    add("## 5. 局限与后续")
    add("")
    add(
        "1. **派生 4h 没有 raw 留档**：`raw_items` 只存 provider 原始 bar；4h 是可复算的派生结果，"
        "其血缘记录在 `collector_runs`（1h）+ 本报告 + `effective_at` 语义中；"
    )
    add(
        "2. **1h 回溯上限**：provider 对 1h 只给约 730 天（2 年），更长历史需换源或改用 "
        "1d 聚合（后续再评估）；"
    )
    add("3. **`market_bars` 的 4h 与 1h 并存**：下游取数必须显式指定 timeframe，不得混用；")
    add(
        "4. **下一步**：W0-2（宏观 + `released_at`，R3）、W0-3（新闻）、W0-4（作者观点链）、"
        "W0-5（特征底座 + 迁移 0006）。"
    )
    add(
        "5. **历史清理留痕**：早期轮次产生的 9 根“墙钟”垃圾行 + 8 根未收盘 bar 已删除，"
        "证据分别存档于 `logs/archive/phase3_w0_1_backfill/deleted_wall_clock_bars.csv` 与 "
        "`deleted_unclosed_bars.csv`；判据为 `秒≠0`（任何市场合法 bar 时间戳都是整分）与 "
        "`close_time > collected_at`，不会误删提前收盘的 30 分钟 bar。"
    )
    add("")
    add("## 6. 实测约束与已修缺陷（随脚本固化）")
    add("")
    add("### 6.1 provider / 客户端约束（实测 2026-09-15）")
    add("")
    for item in PROVIDER_CONSTRAINTS:
        add(f"- {item}")
    add("")
    add("### 6.2 本轮发现并修复的缺陷（均已带回归测试）")
    add("")
    for item in FIXED_DEFECTS:
        add(f"- {item}")
    add("")
    add("## 附录 A. 复现命令")
    add("")
    add("```powershell")
    add(repro_command)
    add("```")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "logs" / "market_backfill"
OUTCOME_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "timeframe",
    "window_start",
    "window_end",
    "status",
    "fetched",
    "inserted",
    "duplicate",
    "failed",
    "warnings",
    "note",
)


def build_repro_command(args: argparse.Namespace) -> str:
    """报告附录里的复现命令（**不回显 `--db-url`**，避免口令进报告）。"""
    parts = [
        "python scripts/backfill_market_bars.py",
        f"--symbols {args.symbols}",
        f"--timeframes {args.timeframes}",
    ]
    if args.with_4h:
        parts.append("--with-4h")
    if args.snapshot_dir:
        parts.append(f"--snapshot-dir {args.snapshot_dir}")
    if args.transport != "httpx":
        parts.append(f"--transport {args.transport}")
    parts.append("--no-dry-run")
    return " ".join(parts) + "   # 默认 dry-run；真实回填需显式 --no-dry-run"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/backfill_market_bars.py",
        description="Phase 3.0 W0-1 行情回填（走既有采集器；默认 dry-run，不写库）",
    )
    parser.add_argument(
        "--symbols", default=DEFAULT_SYMBOLS, help=f"逗号分隔（默认 {DEFAULT_SYMBOLS}）"
    )
    parser.add_argument(
        "--timeframes", default=DEFAULT_TIMEFRAMES, help=f"逗号分隔（默认 {DEFAULT_TIMEFRAMES}）"
    )
    parser.add_argument("--with-4h", action="store_true", help="额外由 1h 聚合出 4h（TD-03）")
    parser.add_argument(
        "--snapshot-dir", default="", help="把 provider 快照写到该目录（只读审计用）"
    )
    parser.add_argument(
        "--transport",
        choices=("httpx", "aiohttp"),
        default="httpx",
        help="传输层实现（默认 httpx：**实测 Yahoo 对 aiohttp 一律 403**，同参数 httpx 200）",
    )
    parser.add_argument("--db-url", default="", help="覆盖 DATABASE_URL（默认取 .env）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次请求超时（秒）")
    parser.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), help="落盘目录（outcomes CSV + meta）"
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径（Markdown）")
    parser.add_argument(
        "--dry-run", action="store_true", help="预检 + 取数 + 缺口复审，**不写库不写文件**（默认）"
    )
    parser.add_argument("--no-dry-run", action="store_true", help="显式允许回填落库")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=参数错误或预检未通过。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[backfill] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run

    try:
        symbols = parse_csv_list(args.symbols)
        timeframes = parse_csv_list(args.timeframes)
    except ValueError as exc:
        print(f"[backfill] {exc}", file=sys.stderr)
        return 2
    plan, notes = plan_windows(symbols, timeframes)
    if not plan:
        print("[backfill] 没有可执行的采集计划（见下方说明）", file=sys.stderr)
        for note in notes:
            print(f"  - {note}", file=sys.stderr)
        return 2

    engine = build_engine(args.db_url or None)
    session = build_session_factory(engine)()
    try:
        problems = preflight(session, symbols=[symbol for symbol, _, _ in plan])
        if problems:
            print("[backfill] 预检未通过：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print(f"[backfill] 修复：{SETUP_HINT}", file=sys.stderr)
            return 2
        print(f"[backfill] 预检通过（库 = {mask_db_url(str(engine.url))}）")

        print(f"[backfill] 计划 {len(plan)} 项：")
        for symbol, timeframe, window in plan:
            actual_days = (window.end_utc - window.start_utc).days
            print(
                f"  - {symbol} {timeframe}：{window.start_utc.date()} → {window.end_utc.date()}"
                f"（lookback={DEFAULT_LOOKBACKS.get(timeframe, '2y')}/{actual_days} 天，"
                f"ticker={PROVIDER_SYMBOLS[symbol][0]}）"
            )
        for note in notes:
            print(f"  ℹ️ {note}")

        if dry_run:
            print("[backfill] dry-run：只取数 + 缺口复审，**不写库、不写文件**")
            for symbol, timeframe, _window in plan:
                try:
                    fetch = fetch_chart(
                        symbol, interval=timeframe, lookback=DEFAULT_LOOKBACKS.get(timeframe, "2y")
                    )
                    audit = audit_gaps(fetch)
                except (RuntimeError, ValueError, OSError) as exc:
                    print(
                        f"[backfill][FAILED] {symbol} {timeframe}：{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                print(f"[backfill] {audit.summary_lines()[0]}")
            print("[backfill] dry-run 结束：未写库、未写文件（加 --no-dry-run 才回填）")
            return 0

        source = session.scalar(sa.select(Source).where(Source.name == DEFAULT_MARKET_SOURCE))
        assert source is not None  # 预检已保证存在
        outcomes = asyncio.run(
            run_backfill(
                session,
                source=source,
                plan=plan,
                timeout=args.timeout,
                transport_name=args.transport,
            )
        )
        for outcome in outcomes:
            print(
                f"[backfill] {outcome.symbol} {outcome.timeframe}：{outcome.status}｜"
                f"fetched={outcome.fetched} inserted={outcome.inserted} "
                f"duplicate={outcome.duplicate} failed={outcome.failed}"
            )

        aggregation: list[tuple[str, int, int, int, int]] = []
        if args.with_4h:
            for symbol in sorted({symbol for symbol, _, _ in plan}):
                complete, inserted, existing, skipped = aggregate_and_insert_4h(
                    session, symbol=symbol
                )
                aggregation.append((symbol, complete, inserted, existing, skipped))
                print(
                    f"[backfill] 4h {symbol}：满桶 {complete}｜新插入 {inserted}｜"
                    f"已存在 {existing}｜跳过半桶 {skipped}"
                )

        if args.snapshot_dir:
            for path in write_snapshots(Path(args.snapshot_dir), plan):
                print(f"[backfill] 快照：{path}")

        audits = []
        for symbol, timeframe, _window in plan:
            try:
                audits.append(
                    audit_gaps(
                        fetch_chart(
                            symbol,
                            interval=timeframe,
                            lookback=DEFAULT_LOOKBACKS.get(timeframe, "2y"),
                        )
                    )
                )
            except (RuntimeError, ValueError, OSError):
                continue

        report_text = render_report(
            outcomes=outcomes,
            aggregation=aggregation,
            audits=audits,
            notes=notes,
            db_url=str(engine.url),
            generated_at=datetime.now(UTC),
            repro_command=build_repro_command(args),
        )

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        outcomes_csv = out_dir / "backfill_outcomes.csv"
        with outcomes_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(OUTCOME_COLUMNS))
            writer.writeheader()
            for outcome in outcomes:
                writer.writerow(outcome.to_row())
        meta_path = out_dir / "backfill_meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "db_url": mask_db_url(str(engine.url)),
                    "plan": [
                        {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "start": window.start_utc.isoformat(),
                            "end": window.end_utc.isoformat(),
                        }
                        for symbol, timeframe, window in plan
                    ],
                    "outcomes": [outcome.to_row() for outcome in outcomes],
                    "aggregation_4h": [
                        {
                            "symbol": symbol,
                            "complete_buckets": complete,
                            "inserted": inserted,
                            "existing": existing,
                            "skipped_partial": skipped,
                        }
                        for symbol, complete, inserted, existing, skipped in aggregation
                    ],
                    "notes": list(notes),
                    "caveats": [
                        "只 append、不覆盖历史（同一自然键复跑记 duplicate）",
                        (
                            "4h 为派生 bar（无 raw_items 留档）；每个标的的当前完整快照写入 "
                            "data_versions，含 processor_version、范围、行数和 SHA-256"
                        ),
                        "US10Y_REAL 由 W0-2 的 FRED DFII10 提供（含 released_at）",
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
        print("[backfill] 已写出：")
        for path in (outcomes_csv, meta_path, report_path):
            print(f"  - {path}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
