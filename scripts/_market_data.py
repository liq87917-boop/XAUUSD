"""W0-1 行情回填 / 缺口审计的**共享工具**（provider 取数 + 会话日历 + 缺口分类 + 4h 聚合）。

为什么单独一个 `_` 前缀模块：与 `scripts/_console.py` 同一套路 —— W0-1 的两个脚本
（`scripts/backfill_market_bars.py` 回填、`scripts/audit_market_gaps.py` 审计）必须共用
**同一套**解析与分类口径；禁止各写一份（口径漂移会让审计结论不可信）。

数据源：Yahoo Finance chart JSON（`database/seeds/sources.py` 的 `market_yahoo`，无需 API Key）。

**实测结论（2026-09-15，写进代码注释以免反复踩）**：

1. **必须带 `User-Agent`**，否则 provider 直接 `HTTP 429`；
2. 可用 ticker：`XAUUSD → GC=F`、`DXY → DX-Y.NYB`、`USDCNY → CNY=X`、`US10Y → ^TNX`；
3. 不可用：`XAUUSD=X`(404)、`DX=F`(404)、`USDCNY`(404)；
   **裸 `DXY` / `TNX` 返回 HTTP 200 但 0 根 bar → 必须当作失败**（候选链会自动落到下一个）；
4. **`US10Y_REAL`（实际利率）Yahoo 没有**（`DFII10` → 404）→ 由 Phase 3.0 **W0-2** 的
   FRED 序列 `DFII10` 提供（带 `released_at`，见 R3）；本模块**显式声明不支持**，
   绝不静默返回空数据（W0-1 报告里会记为"交给 W0-2"）。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from src.collectors.errors import TransportError
from src.collectors.transport import HttpRequest, HttpResponse

# ---------------------------------------------------------------------------
# provider 常量（与 database/seeds/sources.py 的 market_yahoo 保持一致）
# ---------------------------------------------------------------------------
YAHOO_BASE_URL: Final[str] = "https://query1.finance.yahoo.com"
YAHOO_CHART_PATH: Final[str] = "/v8/finance/chart/{symbol}"
#: 与 `src/collectors/transport.py` 的默认 UA 一致（不伪装浏览器）
YAHOO_USER_AGENT: Final[str] = "gold-ai-collector/0.2"

#: 项目标的 → provider ticker **候选链**（按顺序尝试，第一个返回 **>0 根有效 bar** 的采用）
PROVIDER_SYMBOLS: Final[dict[str, tuple[str, ...]]] = {
    "XAUUSD": ("GC=F", "XAUUSD=X"),
    "DXY": ("DX-Y.NYB", "DXY"),
    "USDCNY": ("CNY=X", "USDCNY=X"),
    "US10Y": ("^TNX", "TNX"),
}
#: 行情源**无法提供**的标的 → 原因（脚本必须在报告里显式说明，不允许静默跳过）
UNSUPPORTED_SYMBOLS: Final[dict[str, str]] = {
    "US10Y_REAL": (
        "Yahoo 无实际利率序列（DFII10 → 404）；由 W0-2 的 FRED DFII10 提供（含 released_at）"
    ),
}

#: 周期 → 分钟数（4h 由 1h 聚合，provider 无此粒度 → TD-03）
TIMEFRAME_MINUTES: Final[dict[str, int]] = {"1h": 60, "4h": 240, "1d": 1440}
#: 默认 provider 回溯窗口（W0-1：1d 取 10 年、1h 取 2 年）
DEFAULT_LOOKBACKS: Final[dict[str, str]] = {"1d": "10y", "1h": "2y"}
#: **采集窗口的回溯天数**（provider 硬限制）：1h 只允许最近 **730 天**，卡在边界上会被拒
#: （实测 `period1/period2` 正好 730 天 → **HTTP 422**），故留 10 天边际用 720 天。
BACKFILL_LOOKBACK_DAYS: Final[dict[str, int]] = {"1d": 3650, "1h": 720}
#: 4h 聚合的分钟步长
FOUR_HOURS_MINUTES: Final[int] = 240
#: 会话日历的**交易所本地时区**：CME 黄金期货（GC=F）会话按**美东时间**定义，
#: 用 UTC 分桶会把 DST 造成的"UTC 小时漂移"误判成数据缺失/空洞（实测踩过：1d 假阳性 954 条）。
DEFAULT_SESSION_TZ: Final[str] = "America/New_York"


@dataclass(frozen=True, slots=True)
class HttpxTransport:
    """`src.collectors.transport.Transport` 协议的 **httpx** 实现。

    **为什么需要它（实测 2026-09-15）**：Yahoo 边界对 **aiohttp** 客户端一律返回
    `HTTP 403`（HTML 错误页，换 UA/Accept 均无用），而**同 URL、同参数、同 UA** 用
    httpx 则 `HTTP 200`。项目既有的 `AiohttpTransport` 对其他站点（RSS 等）工作正常，
    因此这里提供一个**协议兼容的替身**，采集器的解析/落库/幂等逻辑**完全不改**。

    用 `asyncio.to_thread` 调 httpx 同步 API，保持 `send()` 的异步签名。
    """

    default_timeout_seconds: float = 30.0
    user_agent: str = YAHOO_USER_AGENT
    accept: str = "application/json"

    async def send(self, request: HttpRequest) -> HttpResponse:
        """发出请求并映射为项目统一的 `HttpResponse`。

        Raises:
            TransportError: 网络/客户端异常（由采集器的重试策略统一处理）。
        """
        import asyncio

        import httpx

        headers = {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            **dict(request.headers),
        }
        timeout = request.timeout_seconds or self.default_timeout_seconds
        try:
            response = await asyncio.to_thread(
                httpx.request,
                request.method,
                request.url,
                params=dict(request.params),
                headers=headers,
                json=request.json_body,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 统一映射为领域异常，交给重试策略
            raise TransportError(
                f"httpx 请求失败：{request.url}（{type(exc).__name__}: {exc}）",
                details={"url": request.url},
            ) from exc

        payload: Any = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = response.json()
            except ValueError:
                payload = None
        return HttpResponse(
            status=response.status_code,
            json_body=payload,
            text=response.text,
            headers=dict(response.headers),
        )


def local_bucket(moment: datetime, session_tz: str = DEFAULT_SESSION_TZ) -> tuple[int, int]:
    """把 UTC 时刻换算到会话本地时区后取 `(本地星期, 本地小时)` 桶。"""
    local = moment.astimezone(ZoneInfo(session_tz))
    return (local.weekday(), local.hour)


@dataclass(frozen=True, slots=True)
class ChartSlot:
    """provider 返回的**一个时间槽**（`is_null=True` 表示 OHLC 缺失 —— 审计对象）。"""

    open_time: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None

    @property
    def is_null(self) -> bool:
        return self.open is None

    @property
    def bucket(self) -> tuple[int, int]:
        """会话日历分桶键 = `(UTC 星期, UTC 小时)`（0=周一）。"""
        return (self.open_time.weekday(), self.open_time.hour)


@dataclass(slots=True)
class ChartFetch:
    """一次 provider 取数（**保留空槽**，供缺口审计使用）。"""

    symbol: str
    interval: str
    lookback: str
    provider_symbol: str
    slots: tuple[ChartSlot, ...] = ()
    bad_timestamps: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total(self) -> int:
        return len(self.slots)

    @property
    def valid_slots(self) -> tuple[ChartSlot, ...]:
        return tuple(slot for slot in self.slots if not slot.is_null)

    @property
    def null_slots(self) -> tuple[ChartSlot, ...]:
        return tuple(slot for slot in self.slots if slot.is_null)

    def digest(self) -> str:
        """内容指纹（前 16 位）：报告留痕，保证"同一份数据可复现"。"""
        payload = "\n".join(
            f"{slot.open_time.isoformat()},{slot.open},{slot.high},{slot.low},{slot.close}"
            for slot in self.slots
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 解析与取数
# ---------------------------------------------------------------------------
def _value_at(values: Sequence[Any] | None, index: int) -> Any:
    if not values or index >= len(values):
        return None
    return values[index]


def parse_chart_slots(payload: Mapping[str, Any] | None) -> tuple[list[ChartSlot], int]:
    """Yahoo chart JSON → ``(全部时间槽, 坏时间戳数)``。

    与 `scripts/probe_ic.py` 的关键差异：**这里保留 OHLC 为 null 的空槽**
    （空槽正是缺口审计的对象），只有**时间戳本身损坏**才计入 `bad_timestamps`。

    Raises:
        ValueError: 响应结构异常或 provider 报错（**不静默产出空数据**）。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("行情响应不是 JSON 对象")
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise ValueError("行情响应缺少 chart 字段")
    if chart.get("error"):
        raise ValueError(f"行情接口返回错误：{chart.get('error')!r}")
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        raise ValueError("行情响应缺少 result")

    result = results[0] if isinstance(results[0], Mapping) else {}
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    quote = quotes[0] if quotes and isinstance(quotes[0], Mapping) else {}

    def _number(key: str, index: int) -> float | None:
        raw = _value_at(quote.get(key), index)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    slots: list[ChartSlot] = []
    bad = 0
    for index, raw_timestamp in enumerate(timestamps):
        try:
            epoch = int(raw_timestamp)
        except (TypeError, ValueError):
            bad += 1
            continue
        slots.append(
            ChartSlot(
                open_time=datetime.fromtimestamp(epoch, tz=UTC),
                open=_number("open", index),
                high=_number("high", index),
                low=_number("low", index),
                close=_number("close", index),
                volume=_number("volume", index),
            )
        )
    return slots, bad


def fetch_chart(
    symbol: str,
    *,
    interval: str,
    lookback: str,
    timeout: float = 60.0,
    provider_symbols: Sequence[str] | None = None,
) -> ChartFetch:
    """按候选链取数（**保留空槽**；真实联网的只有这一个函数）。

    Raises:
        RuntimeError: 全部候选 ticker 都失败（原因逐条列出，不静默返回空数据）。
    """
    import httpx  # 惰性导入：避免测试在导入期触碰网络栈

    if symbol in UNSUPPORTED_SYMBOLS:
        raise RuntimeError(f"{symbol} 无可用行情源：{UNSUPPORTED_SYMBOLS[symbol]}")
    candidates = tuple(provider_symbols or PROVIDER_SYMBOLS.get(symbol, (symbol,)))
    if not candidates:
        raise RuntimeError(f"{symbol} 未配置 provider ticker 候选链（见 scripts/_market_data.py）")

    errors: list[str] = []
    for candidate in candidates:
        url = f"{YAHOO_BASE_URL}{YAHOO_CHART_PATH.format(symbol=candidate)}"
        try:
            response = httpx.get(
                url,
                params={
                    "interval": interval,
                    "range": lookback,
                    "includePrePost": "false",
                    "events": "div,splits",
                },
                # 必须带 UA：无 UA 时 provider 直接 429（实测）
                headers={"User-Agent": YAHOO_USER_AGENT, "Accept": "application/json"},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常逐条收集，最后统一报错
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        if response.status_code != 200:
            errors.append(f"{candidate}: HTTP {response.status_code}")
            continue
        try:
            slots, bad = parse_chart_slots(response.json())
        except ValueError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        # HTTP 200 但 0 根**有效** bar（例如裸 DXY/TNX）→ 视为失败，继续下一个候选
        if not any(not slot.is_null for slot in slots):
            errors.append(f"{candidate}: 返回 0 根有效 K 线（共 {len(slots)} 个槽）")
            continue
        return ChartFetch(
            symbol=symbol,
            interval=interval,
            lookback=lookback,
            provider_symbol=candidate,
            slots=tuple(slots),
            bad_timestamps=bad,
        )
    raise RuntimeError("所有候选 ticker 均取数失败：" + "；".join(errors))


# ---------------------------------------------------------------------------
# 会话日历（**数据驱动**：不硬编码 DST / 假期表）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionProfile:
    """按 `(UTC 星期, UTC 小时)` 桶标定的交易时段分布（阈值来自**同一份数据**）。"""

    presence: Mapping[tuple[int, int], float]
    open_buckets: frozenset[tuple[int, int]]
    closed_buckets: frozenset[tuple[int, int]]
    threshold: float

    def is_closed(self, bucket: tuple[int, int]) -> bool:
        return bucket in self.closed_buckets

    def is_open(self, bucket: tuple[int, int]) -> bool:
        return bucket in self.open_buckets


def build_session_profile(
    slots: Sequence[ChartSlot],
    *,
    closed_threshold: float = 0.02,
    session_tz: str = DEFAULT_SESSION_TZ,
) -> SessionProfile:
    """用同一份数据标定「常规开市/休市」桶（**不硬编码** DST 与交易所假期）。

    做法：把每个时刻换算到**会话本地时区**（CME = 美东）后，对每个
    `(本地星期, 本地小时)` 桶统计「有有效 bar 的比例」；比例 < `closed_threshold`
    的桶 = 常规休市（周末、每日结算间隙等）；其余 = 常规开市。
    未在数据里出现过的桶**既不判开市也不判休市**（保守）。
    """
    buckets = [local_bucket(slot.open_time, session_tz) for slot in slots]
    totals: Counter[tuple[int, int]] = Counter(buckets)
    valid: Counter[tuple[int, int]] = Counter(
        bucket for bucket, slot in zip(buckets, slots, strict=True) if not slot.is_null
    )
    presence = {
        bucket: (valid.get(bucket, 0) / count) if count else 0.0 for bucket, count in totals.items()
    }
    closed = frozenset(bucket for bucket, ratio in presence.items() if ratio < closed_threshold)
    opened = frozenset(bucket for bucket, ratio in presence.items() if ratio >= closed_threshold)
    return SessionProfile(
        presence=presence,
        open_buckets=opened,
        closed_buckets=closed,
        threshold=closed_threshold,
    )


# ---------------------------------------------------------------------------
# 缺口审计（区分"休市/流动性空档"与"数据缺失"）
# ---------------------------------------------------------------------------
#: 常规休市（周末 / 每日结算间隙）→ **预期内**，不算数据质量问题
CATEGORY_SESSION_BREAK: Final[str] = "session_break"
#: 疑似交易所假期（工作日整段无 bar）→ 预期内但需人工确认
CATEGORY_HOLIDAY_SUSPECT: Final[str] = "holiday_suspect"
#: **数据缺失（异常）**：常规开市时段内的空 bar
CATEGORY_DATA_GAP: Final[str] = "data_gap"
#: provider 连时间戳都没返回（按会话日历推算应有）
CATEGORY_MISSING_TIMESTAMP: Final[str] = "missing_timestamp"
CATEGORY_LABELS: Final[dict[str, str]] = {
    CATEGORY_SESSION_BREAK: "常规休市（预期内）",
    CATEGORY_HOLIDAY_SUSPECT: "疑似假期（需人工确认）",
    CATEGORY_DATA_GAP: "**数据缺失（异常）**",
    CATEGORY_MISSING_TIMESTAMP: "**时间戳缺失（异常）**",
}


@dataclass(frozen=True, slots=True)
class SlotFinding:
    """一条审计发现（异常槽或缺失槽）。"""

    symbol: str
    interval: str
    open_time: datetime
    category: str
    note: str = ""


@dataclass(slots=True)
class GapAudit:
    """一次缺口审计的完整结果。"""

    symbol: str
    interval: str
    lookback: str
    provider_symbol: str
    provider_digest: str
    total_slots: int = 0
    valid_slots: int = 0
    null_slots: int = 0
    bad_timestamps: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    missing_timestamps: int = 0
    longest_run: dict[str, int] = field(default_factory=dict)
    findings: list[SlotFinding] = field(default_factory=list)
    low_coverage_days: list[tuple[str, int, int]] = field(default_factory=list)
    first_slot_at: datetime | None = None
    last_slot_at: datetime | None = None

    @property
    def anomaly_count(self) -> int:
        """**真正的异常**数量（= 数据缺失 + 时间戳缺失；其余都是预期内）。"""
        return self.counts.get(CATEGORY_DATA_GAP, 0) + self.counts.get(
            CATEGORY_MISSING_TIMESTAMP, 0
        )

    @property
    def null_ratio(self) -> float:
        return self.null_slots / self.total_slots if self.total_slots else 0.0

    def summary_lines(self) -> list[str]:
        """控制台/报告用的摘要行（口径写清，不给人误读空间）。"""
        parts = [f"{self.symbol} {self.interval}：槽 {self.total_slots}（有效 {self.valid_slots}）"]
        for category, label in CATEGORY_LABELS.items():
            count = self.counts.get(category, 0)
            if count:
                parts.append(f"{label} {count}")
        parts.append(f"ticker={self.provider_symbol}")
        parts.append(f"指纹={self.provider_digest}")
        return ["｜".join(parts)]


#: 覆盖不足的日子在报告里最多列几条
MAX_LOW_COVERAGE_DAYS: Final[int] = 10


def _longest_run(categories: Sequence[str], target: str) -> int:
    best = current = 0
    for category in categories:
        current = current + 1 if category == target else 0
        best = max(best, current)
    return best


def audit_gaps(
    fetch: ChartFetch,
    *,
    closed_threshold: float = 0.02,
    session_tz: str = DEFAULT_SESSION_TZ,
    max_findings: int = 500,
) -> GapAudit:
    """审计一份 provider 取数：把空槽分成「常规休市 / 疑似假期 / **数据缺失**」并统计时间戳缺失。

    **会话按交易所本地时区（默认美东）核账**：CME 会话由美东时间定义，用 UTC 小时分桶会把
    DST 造成的「UTC 小时漂移」误判为数据缺失（实测：1d 假阳性 954 条）——本实现按
    `(本地星期, 本地小时)` 分桶、按**本地日期**核算，因此对 DST 免疫。

    规则（全部可复算、无人工干预，**不需要假期表**）：

    1. **常规休市**（预期内）：空槽落在「开市比例 < `closed_threshold`」的**本地时段桶**
       —— 周末与每日结算间隙由数据本身标定；
    2. **疑似假期**（需人工确认）：**工作日整日无有效 bar**（当日观测槽数达到该本地星期几的
       常态值，但有效槽为 0）；
    3. **数据缺失（异常）**：常规开市时段内的其余空槽；
    4. **时间戳缺失（异常）**：当日**实收槽数 < 该本地星期几常态槽数**，差额即缺失条数。
    """
    audit = GapAudit(
        symbol=fetch.symbol,
        interval=fetch.interval,
        lookback=fetch.lookback,
        provider_symbol=fetch.provider_symbol,
        provider_digest=fetch.digest(),
        total_slots=fetch.total,
        valid_slots=len(fetch.valid_slots),
        null_slots=len(fetch.null_slots),
        bad_timestamps=fetch.bad_timestamps,
    )
    if not fetch.slots:
        return audit
    audit.first_slot_at = fetch.slots[0].open_time
    audit.last_slot_at = fetch.slots[-1].open_time
    tz = ZoneInfo(session_tz)
    profile = build_session_profile(
        fetch.slots, closed_threshold=closed_threshold, session_tz=session_tz
    )

    # 逐**本地日期**聚合（会话按交易所本地时间定义 → 对 DST 免疫）
    observed_by_date: Counter[str] = Counter()
    valid_by_date: Counter[str] = Counter()
    weekday_by_date: dict[str, int] = {}
    anomaly_slots: dict[str, list[ChartSlot]] = {}
    for slot in fetch.slots:
        local = slot.open_time.astimezone(tz)
        date = local.date().isoformat()
        weekday_by_date[date] = local.weekday()
        observed_by_date[date] += 1
        if not slot.is_null:
            valid_by_date[date] += 1
            continue
        if not profile.is_closed(local_bucket(slot.open_time, session_tz)):
            anomaly_slots.setdefault(date, []).append(slot)

    # 每个**本地星期几**的"常态槽数" = 该星期几观测槽数的众数（自校准，不需要假期表）
    counts_by_weekday: dict[int, list[int]] = {}
    for date, observed in observed_by_date.items():
        counts_by_weekday.setdefault(weekday_by_date[date], []).append(observed)
    expected_by_weekday = {
        weekday: Counter(values).most_common(1)[0][0]
        for weekday, values in counts_by_weekday.items()
        if values
    }

    # ① 先判"疑似假期"（工作日整日无有效 bar）与"时间戳缺失"（实收 < 常态）
    holiday_dates: set[str] = set()
    missing_by_date: Counter[str] = Counter()
    for date, observed in sorted(observed_by_date.items()):
        weekday = weekday_by_date[date]
        expected = expected_by_weekday.get(weekday, 0)
        valid = valid_by_date.get(date, 0)
        if weekday < 5 and expected and valid == 0:
            holiday_dates.add(date)
            if len(audit.findings) < max_findings:
                audit.findings.append(
                    SlotFinding(
                        fetch.symbol,
                        fetch.interval,
                        fetch.slots[0].open_time,
                        CATEGORY_HOLIDAY_SUSPECT,
                        f"{date} 整日无有效 bar（应有 {expected} 个槽）"
                        "→ 形态似交易所假期，待人工确认",
                    )
                )
        elif expected and observed < expected:
            missing_by_date[date] = expected - observed
            if len(audit.findings) < max_findings:
                audit.findings.append(
                    SlotFinding(
                        fetch.symbol,
                        fetch.interval,
                        fetch.slots[0].open_time,
                        CATEGORY_MISSING_TIMESTAMP,
                        f"{date} 实收 {observed} 个槽 < 常态 {expected} 个"
                        f" → 缺 {expected - observed} 个时间戳",
                    )
                )

    # ② 再给每个空槽分类（假期日 → 疑似假期；否则开市时段内 → 数据缺失）
    categories: list[str] = []
    anomaly_category: dict[datetime, str] = {}
    for date, slots in sorted(anomaly_slots.items()):
        on_holiday = date in holiday_dates
        category = CATEGORY_HOLIDAY_SUSPECT if on_holiday else CATEGORY_DATA_GAP
        note = (
            f"{date} 疑似交易所假期（整日无有效 bar）"
            if on_holiday
            else f"{date} 常规开市时段内空 bar（当日有效 {valid_by_date.get(date, 0)}）"
        )
        for slot in slots:
            anomaly_category[slot.open_time] = category
            if len(audit.findings) < max_findings:
                audit.findings.append(
                    SlotFinding(fetch.symbol, fetch.interval, slot.open_time, category, note)
                )

    for slot in fetch.slots:
        if not slot.is_null:
            categories.append("")
        elif profile.is_closed(local_bucket(slot.open_time, session_tz)):
            categories.append(CATEGORY_SESSION_BREAK)
        else:
            categories.append(anomaly_category.get(slot.open_time, CATEGORY_DATA_GAP))

    counts: Counter[str] = Counter(categories)
    del counts[""]
    counts[CATEGORY_MISSING_TIMESTAMP] = sum(missing_by_date.values())
    audit.counts = dict(counts)
    audit.missing_timestamps = sum(missing_by_date.values())
    audit.longest_run = {
        CATEGORY_SESSION_BREAK: _longest_run(categories, CATEGORY_SESSION_BREAK),
        CATEGORY_HOLIDAY_SUSPECT: _longest_run(categories, CATEGORY_HOLIDAY_SUSPECT),
        CATEGORY_DATA_GAP: _longest_run(categories, CATEGORY_DATA_GAP),
        CATEGORY_MISSING_TIMESTAMP: max(missing_by_date.values(), default=0),
    }
    audit.low_coverage_days = sorted(
        (
            (date, valid_by_date.get(date, 0), expected_by_weekday.get(weekday_by_date[date], 0))
            for date in observed_by_date
            if expected_by_weekday.get(weekday_by_date[date], 0)
            and valid_by_date.get(date, 0) < expected_by_weekday.get(weekday_by_date[date], 0)
        ),
        key=lambda item: item[2] - item[1],
        reverse=True,
    )[:MAX_LOW_COVERAGE_DAYS]
    return audit


# ---------------------------------------------------------------------------
# TD-03：4h 聚合（provider 无 4h 粒度，必须由 Processor 层聚合）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceBar:
    """待聚合的输入 bar（小时级）。"""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True, slots=True)
class AggregatedBar:
    """聚合后的 4h bar（UTC 对齐到 4 小时桶）。"""

    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    bars: int


def bucket_start(moment: datetime, *, bucket_minutes: int = FOUR_HOURS_MINUTES) -> datetime:
    """把时刻向下取整到 UTC 的 `bucket_minutes` 桶起点（默认 4h → 00/04/08/12/16/20 UTC）。

    Raises:
        ValueError: `bucket_minutes` 不是 1440 的因子（保证桶不会跨日）。
    """
    if bucket_minutes <= 0 or 1440 % bucket_minutes:
        raise ValueError(f"bucket_minutes 必须是 1440 的因子，收到 {bucket_minutes}")
    minutes = moment.hour * 60 + moment.minute
    floored = (minutes // bucket_minutes) * bucket_minutes
    return moment.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def aggregate_bars_to_4h(
    bars: Sequence[SourceBar],
    *,
    bucket_minutes: int = FOUR_HOURS_MINUTES,
    required_bars: int = 4,
) -> tuple[list[AggregatedBar], list[datetime]]:
    """小时级 bar → 4h bar（TD-03）。**只输出满桶**，返回 `(bars, 被跳过的桶起点)`。

    规则：按 `bucket_start` 分桶，桶内 bar 数必须等于 `required_bars` 才输出
    —— **半桶不得当完整 4h bar**（半桶的 close 不是该周期收盘价，会污染后续特征）；
    OHLC 取 first-open / max-high / min-low / last-close，成交量求和（全空则为 None）。
    """
    grouped: dict[datetime, list[SourceBar]] = {}
    for bar in bars:
        grouped.setdefault(bucket_start(bar.open_time, bucket_minutes=bucket_minutes), []).append(
            bar
        )

    result: list[AggregatedBar] = []
    skipped: list[datetime] = []
    for start, members in sorted(grouped.items()):
        ordered = sorted(members, key=lambda item: item.open_time)
        if len(ordered) != required_bars:
            skipped.append(start)
            continue
        volumes = [item.volume for item in ordered if item.volume is not None]
        result.append(
            AggregatedBar(
                open_time=start,
                close_time=start + timedelta(minutes=bucket_minutes),
                open=ordered[0].open,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=ordered[-1].close,
                volume=(sum(volumes) if volumes else None),
                bars=len(ordered),
            )
        )
    return result, skipped
