"""Phase 3 最小验证探针：技术面特征 → 黄金未来收益的 **OOS IC**（零迁移、纯离线）。

**目的**：回答一个问题——"技术面信号对黄金未来收益有没有**独立、可复现**的预测力？"
用最小成本（无迁移、无新依赖、不碰数据库 schema）给用户一个**可信的方向性判断**，
再决定是否投入完整的 Phase 3.0 数据底座 + 3.1 Regime。

**口径（严格遵循 `docs/08 §6` 与 `docs/14 §3.3–3.4`）**：

1. **时间序切分 + walk-forward**：先留出训练段，再按窗口滚动；**禁止 shuffle**；
2. **embargo**：训练段末尾回退 `horizon` 根 bar，避免训练标签与测试窗口重叠（标签泄漏）；
3. **标签**：`entry = open[t+1]`、`exit = close[t+h]`、`label = log(exit/entry)`
   —— 绝不使用"包含信号时刻、尚未走完"的 bar；
4. **禁止全样本统计**：特征只用滚动/回溯窗口；模型标准化只在**训练段**拟合（逐窗口重拟合）；
5. **基准对照**：random / always_long / momentum / MA（`docs/08 §6` 要求）；
6. **指标**：IC（Spearman）、ICIR、t 统计量、BH-FDR 校正后的 q 值；命中率给 **Wilson 95% CI**、
   Brier Score；
7. **`scoring=NOT_EVALUATED`**（`docs/10 §7.1`）：样本不足 / 信号恒定的系列**不评分**，
   只记"未评估"并写明原因——**绝不当成"IC=0（无预测力）"**，且不进多重检验的分母。

**范围**：只做技术面 + 4 个基准；**不使用任何宏观数据**（`macro_events` 目前只有观测期、
缺 `released_at`，存在前视风险 R3 —— 该修复由探针阶段的独立工具先行交付，本脚本不消费宏观输入）。
不写迁移、不动数据库 schema、不引入新依赖（只用 numpy / pandas / scikit-learn / httpx）。

用法::

    python scripts/probe_ic.py --dry-run                  # 默认：只打印统计，不写任何文件
    python scripts/probe_ic.py --no-dry-run               # 显式落盘（bars 快照 + 长表 + 报告）
    python scripts/probe_ic.py --source fixture --bars-fixture logs/probe_ic/xauusd_1d.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.evaluate_extractor import NOT_EVALUATED_MARKER  # noqa: E402
from scripts.report_hf_benchmark import format_pct, wilson_interval  # noqa: E402

DEFAULT_SYMBOL: Final[str] = "XAUUSD"
#: 项目标的 → provider（Yahoo）ticker **候选链**：按顺序尝试，第一个返回 K 线的即采用。
#: **实测（2026-09-14）**：`XAUUSD=X` 返回 **HTTP 404**（Yahoo 已无该代码），
#: `GC=F`（COMEX 黄金连续合约）返回 **HTTP 200 / 252 根 1d**；故把可用者前置，
#: 保留候选链是为了"换源/换代码"时不必改口径（实际使用哪个 ticker 会写进报告附录 B）。
PROVIDER_SYMBOLS: Final[dict[str, tuple[str, ...]]] = {
    "XAUUSD": ("GC=F", "XAUUSD=X", "XAUUSD"),
}
#: 与 `database/seeds/sources.py` 的 `market_yahoo.base_url` 保持一致（不写死第三方站点）
YAHOO_BASE_URL: Final[str] = "https://query1.finance.yahoo.com"
YAHOO_CHART_PATH: Final[str] = "/v8/finance/chart/{symbol}"
#: 请求头：**必须带 User-Agent**（实测无 UA → HTTP 429）。
#: 用项目自己的 UA（与 `src/collectors/transport.py` 默认值一致），不伪装浏览器。
YAHOO_USER_AGENT: Final[str] = "gold-ai-collector/0.2"

DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "logs" / "probe_ic"
DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "experiments" / "Phase3_探针_技术面IC报告.md"

#: 首期 8 个技术特征（`docs/05 Phase 3 Technical Alpha`）
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "ret_1",
    "momentum_20",
    "ma_gap_20",
    "rsi_14",
    "macd_hist",
    "atr_14_pct",
    "realized_vol_20",
    "breakout_20",
)
#: 4 个基准（`docs/08 §6`：random / always long / momentum / MA）
BASELINE_NAMES: Final[tuple[str, ...]] = ("random", "always_long", "momentum", "ma")
MODEL_NAMES: Final[tuple[str, ...]] = ("model_lr", "model_hgb")
#: 特征的方向判定阈值（> 阈值 → 看多）：用于"命中率/Brier"这一层，与 IC 互补。
#: 只列**有方向含义**的特征；波动类特征给 `None` → 不做方向预测（记 `NOT_EVALUATED`）。
FEATURE_DIRECTIONAL_THRESHOLD: Final[dict[str, float]] = {
    "ret_1": 0.0,
    "momentum_20": 0.0,
    "ma_gap_20": 0.0,
    "rsi_14": 50.0,  # RSI 以 50 为多空分界
    "macd_hist": 0.0,
    "breakout_20": 0.0,
}
#: 无方向含义的特征（波动类）：只评估 IC，命中率显式记「未评估」
NON_DIRECTIONAL_FEATURES: Final[tuple[str, ...]] = ("atr_14_pct", "realized_vol_20")
FEATURE_WINDOW: Final[int] = 20
#: 多重检验的假阳性率（Benjamini-Hochberg）
FDR_ALPHA: Final[float] = 0.05
#: 一个窗口至少多少个 OOS 行才计入（否则该窗口记为未评估）
MIN_WINDOW_ROWS: Final[int] = 10
#: 训练段最少多少行才拟合模型（否则该窗口跳过模型，仅算单特征 IC）
MIN_TRAIN_ROWS: Final[int] = 120
#: 至少要几个窗口才能算 ICIR（否则 ICIR 记未评估）
MIN_WINDOWS_FOR_ICIR: Final[int] = 3
#: 报告里最多列多少条未评估记录（完整清单始终在 `ic_long_table.csv`）
MAX_UNEVALUATED_ROWS: Final[int] = 24


# ---------------------------------------------------------------------------
# 配置与数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """一组探针配置：数据周期 × provider 回溯区间 × 预测 horizon × OOS 窗口长度。"""

    name: str
    interval: str  # "1d" / "1h"
    lookback: str  # Yahoo range 参数："1y" / "2y"
    horizon_bars: int  # 持有多少根 bar（label = log(close[t+h]/open[t+1])）
    window_bars: int  # OOS 滚动窗口长度（ICIR 需要多个窗口）
    description: str = ""


#: 默认探针矩阵：主探针 = 用户要求的「1 年日线 → 未来 1 日」；另两条复用 2 年小时线覆盖 4h / 1d。
PROBE_CONFIGS: Final[tuple[ProbeConfig, ...]] = (
    ProbeConfig("1d_1y_h1", "1d", "1y", 1, 21, "日线 → 未来 1 日（主探针）"),
    ProbeConfig("1h_2y_h4", "1h", "2y", 4, 120, "小时线 → 未来 4 小时"),
    ProbeConfig("1h_2y_h24", "1h", "2y", 24, 120, "小时线 → 未来 1 日（24 根 1h）"),
)


@dataclass(slots=True)
class FetchResult:
    """一次取数结果（含 provider ticker 与跳过条数，便于审计与复现）。"""

    provider_symbol: str
    interval: str
    lookback: str
    frame: pd.DataFrame
    skipped_bars: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def rows(self) -> int:
        return len(self.frame)

    def digest(self) -> str:
        """数据指纹（内容哈希前 16 位）：报告留痕，保证"同一份数据可复现"。"""
        payload = self.frame.reset_index().to_csv(index=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(slots=True)
class SeriesStat:
    """长表的一行：`(config, kind, series_id, metric)` 的一个统计值。

    `scoring = NOT_EVALUATED` 表示**该指标无法评估**（样本不足 / 信号恒定 / 窗口不足）：
    此时 `value` 恒为 `None`，且该行**不进多重检验的分母**（`docs/10 §7.1` 同一纪律）。
    """

    config: str
    kind: str  # feature / baseline / model
    series_id: str
    metric: str
    value: float | None
    n_obs: int = 0
    n_windows: int = 0
    ci_low: float | None = None
    ci_high: float | None = None
    p_value: float | None = None
    p_bh: float | None = None
    scoring: str = ""
    note: str = ""

    def to_row(self) -> dict[str, str]:
        def _num(value: float | None) -> str:
            return "" if value is None else f"{value:.6f}"

        return {
            "config": self.config,
            "kind": self.kind,
            "series_id": self.series_id,
            "metric": self.metric,
            "value": _num(self.value),
            "n_obs": str(self.n_obs),
            "n_windows": str(self.n_windows),
            "ci_low": _num(self.ci_low),
            "ci_high": _num(self.ci_high),
            "p_value": _num(self.p_value),
            "p_bh": _num(self.p_bh),
            "scoring": self.scoring,
            "note": self.note,
        }


#: 长表列（落盘 CSV 用；`scoring` 列与 `docs/10 §7.1` 同构）
LONG_TABLE_COLUMNS: Final[tuple[str, ...]] = (
    "config",
    "kind",
    "series_id",
    "metric",
    "value",
    "n_obs",
    "n_windows",
    "ci_low",
    "ci_high",
    "p_value",
    "p_bh",
    "scoring",
    "note",
)


def _safe_print(text: str) -> None:
    """兼容旧名：真正的实现在 `scripts/_console.safe_print`（两个脚本共用）。"""
    safe_print(text)


# ---------------------------------------------------------------------------
# 取数（真实联网的只有 `fetch_yahoo_bars`；测试一律走 fixture）
# ---------------------------------------------------------------------------
def _value_at(values: Sequence[Any] | None, index: int) -> Any:
    if not values or index >= len(values):
        return None
    return values[index]


def parse_yahoo_chart(payload: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    """Yahoo chart JSON → ``(有效 bar 行, 跳过条数)``（纯函数，便于单测）。

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

    rows: list[dict[str, Any]] = []
    skipped = 0
    for index, raw_timestamp in enumerate(timestamps):
        try:
            epoch = int(raw_timestamp)
        except (TypeError, ValueError):
            skipped += 1
            continue
        raw = {key: _value_at(quote.get(key), index) for key in ("open", "high", "low", "close")}
        if any(value is None for value in raw.values()):
            skipped += 1  # provider 对无成交 bar 返回 null：这类 bar 不得进入研究数据
            continue
        volume = _value_at(quote.get("volume"), index)
        rows.append(
            {
                "open_time": pd.Timestamp(epoch, unit="s", tz="UTC"),
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float("nan") if volume is None else float(volume),
            }
        )
    return rows, skipped


def frame_from_rows(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """行列表 → 以 `open_time`(UTC) 为索引、时间升序的 OHLCV DataFrame（缺失量列补 NaN）。"""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows).set_index("open_time").sort_index()
    if "volume" not in frame.columns:
        frame["volume"] = float("nan")
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_yahoo_bars(
    symbol: str,
    *,
    interval: str,
    lookback: str,
    timeout: float = 30.0,
    provider_symbols: Sequence[str] | None = None,
) -> FetchResult:
    """按候选 ticker 链抓取 K 线（**真实联网**；仅 `--source yahoo|auto` 会走到这里）。

    Raises:
        RuntimeError: 全部候选 ticker 都失败（原因逐条列出，不静默返回空数据）。
    """
    import httpx  # 惰性导入：避免测试在导入期触碰网络栈

    candidates = tuple(provider_symbols or PROVIDER_SYMBOLS.get(symbol, (symbol,)))
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
            rows, skipped = parse_yahoo_chart(response.json())
        except ValueError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        if not rows:
            errors.append(f"{candidate}: 返回 0 根有效 K 线")
            continue
        return FetchResult(candidate, interval, lookback, frame_from_rows(rows), skipped)
    raise RuntimeError("所有候选 ticker 均取数失败：" + "；".join(errors))


def load_bars_from_csv(path: Path) -> pd.DataFrame:
    """读离线 fixture K 线（列：`open_time,open,high,low,close[,volume]`）。

    Raises:
        ValueError: 缺列或时间无法按 UTC 解析（探针宁可报错，也不用错数据）。
    """
    frame = pd.read_csv(path)
    missing = {"open_time", "open", "high", "low", "close"} - set(frame.columns)
    if missing:
        raise ValueError(f"fixture {path.name} 缺列：{sorted(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = float("nan")
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True, errors="raise")
    return (
        frame.set_index("open_time")
        .sort_index()
        .loc[:, ["open", "high", "low", "close", "volume"]]
        .astype(float)
    )


# ---------------------------------------------------------------------------
# 特征与标签（特征只用 ≤ t 的信息；标签用 t+1 之后的价格）
# ---------------------------------------------------------------------------
def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI（0~100）；分母为 0 的边界按惯例处理（全涨=100，全跌=0，无变化=50）。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss > 0, 100.0)
    rsi = rsi.where(avg_gain > 0, 0.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi.where(avg_gain.notna(), np.nan)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """ATR（Wilder 平滑）：真实波幅的真均值。"""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute_features(frame: pd.DataFrame, *, window: int = FEATURE_WINDOW) -> pd.DataFrame:
    """计算 8 个首期技术特征（**只用回溯窗口；严禁全样本 mean/std**）。

    特征清单（`docs/05 Phase 3`）：`ret_1` / `momentum_20` / `ma_gap_20` / `rsi_14` /
    `macd_hist` / `atr_14_pct` / `realized_vol_20` / `breakout_20`。

    Raises:
        ValueError: 缺 OHLC 列。
    """
    missing = {"open", "high", "low", "close"} - set(frame.columns)
    if missing:
        raise ValueError(f"K 线缺少列：{sorted(missing)}")
    data = frame.copy()
    close = data["close"]
    log_ret = np.log(close).diff()

    data["ret_1"] = log_ret
    data["momentum_20"] = log_ret.rolling(window).sum()
    data["ma_gap_20"] = close / close.rolling(window).mean() - 1.0
    data["rsi_14"] = _rsi(close, 14)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    data["macd_hist"] = (macd - signal) / close  # 除以价格 → 量纲无关，便于跨时段比较
    data["atr_14_pct"] = _atr(data["high"], data["low"], close, 14) / close
    data["realized_vol_20"] = log_ret.rolling(window).std(ddof=1)
    # 突破：收盘价相对"**前** window 根的最高价"（shift(1) 保证不含当根 → 无同 bar 前视）
    prior_high = data["high"].rolling(window).max().shift(1)
    data["breakout_20"] = close / prior_high - 1.0
    return data


def add_labels(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """加前瞻收益标签：`entry = open[t+1]`、`exit = close[t+h]`（**不使用未走完的 bar**）。

    Raises:
        ValueError: `horizon < 1`。
    """
    if horizon < 1:
        raise ValueError(f"horizon 必须 ≥ 1，收到 {horizon}")
    data = frame.copy()
    entry = data["open"].shift(-1)
    exit_price = data["close"].shift(-horizon)
    data["_entry"] = entry
    data["_exit"] = exit_price
    data["_future_ret"] = np.log(exit_price / entry)
    return data


def walk_forward_windows(
    n_rows: int,
    *,
    train_ratio: float,
    window_bars: int,
    horizon: int,
    min_train: int = MIN_TRAIN_ROWS,
) -> tuple[list[tuple[int, int, int]], int]:
    """时间序 walk-forward 窗口 → ``([(train_end, oos_start, oos_end)], 被跳过的窗口数)``。

    **禁止 shuffle**；`train_end = oos_start - horizon`（embargo），
    避免训练标签（用到 `t+1..t+h` 的价格）与测试窗口重叠。

    Raises:
        ValueError: 参数非法。
    """
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio 必须在 (0,1) 内，收到 {train_ratio}")
    if window_bars < 1 or horizon < 1:
        raise ValueError(f"window_bars/horizon 必须 ≥ 1：{window_bars}/{horizon}")

    start = int(n_rows * train_ratio)
    windows: list[tuple[int, int, int]] = []
    skipped = 0
    while start < n_rows:
        end = min(start + window_bars, n_rows)
        train_end = max(0, start - horizon)
        if train_end >= min_train and (end - start) >= MIN_WINDOW_ROWS:
            windows.append((train_end, start, end))
        else:
            skipped += 1
        start = end
    return windows, skipped


# ---------------------------------------------------------------------------
# 统计工具（不引入 scipy：IC 用秩相关，p 值用 math.erfc 的正态近似）
# ---------------------------------------------------------------------------
def spearman_ic(x: pd.Series, y: pd.Series) -> float | None:
    """Spearman 秩相关（IC）；样本不足或任一侧方差为 0 → `None`（**不可评估**，不是 0）。"""
    pair = pd.concat([x, y], axis=1).dropna()
    if len(pair) < MIN_WINDOW_ROWS:
        return None
    left, right = pair.iloc[:, 0], pair.iloc[:, 1]
    if left.nunique() < 2 or right.nunique() < 2:
        return None
    value = left.corr(right, method="spearman")
    return None if pd.isna(value) else float(value)


def bh_fdr(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg FDR 校正 → 与输入**同序**的 q 值（多重检验纪律）。"""
    count = len(p_values)
    if count == 0:
        return []
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [1.0] * count
    running_min = 1.0
    for position in range(count - 1, -1, -1):
        index = order[position]
        rank = position + 1
        running_min = min(running_min, p_values[index] * count / rank)
        adjusted[index] = running_min
    return adjusted


def two_sided_p_from_t(t_stat: float) -> float:
    """双侧 p 值（正态近似）：``p = erfc(|t|/√2)`` —— 不引入 scipy。

    ⚠️ 局限：窗口重叠会让该 p 值偏乐观；因此结论以 **BH 校正后的 q 值 + IC 符号一致性** 为准。
    """
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


# ---------------------------------------------------------------------------
# 评估引擎（walk-forward OOS）
# ---------------------------------------------------------------------------
def _empty_stats(config_name: str, reason: str) -> list[SeriesStat]:
    """整条配置不可评估时，**逐系列**写 `NOT_EVALUATED`（绝不写成 "IC=0"）。"""
    stats: list[SeriesStat] = []
    for kind, names in (
        ("feature", FEATURE_NAMES),
        ("baseline", BASELINE_NAMES),
        ("model", MODEL_NAMES),
    ):
        for series_id in names:
            for metric in ("ic_mean", "hit_rate"):
                stats.append(
                    SeriesStat(
                        config=config_name,
                        kind=kind,
                        series_id=series_id,
                        metric=metric,
                        value=None,
                        scoring=NOT_EVALUATED_MARKER,
                        note=reason,
                    )
                )
    return stats


@dataclass(slots=True)
class ConfigResult:
    """一条探针配置的完整结果（统计长表 + 审计元数据）。"""

    config: ProbeConfig
    stats: list[SeriesStat] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def evaluate_config(
    frame: pd.DataFrame,
    config: ProbeConfig,
    *,
    train_ratio: float = 0.5,
    seed: int = 42,
    min_train: int = MIN_TRAIN_ROWS,
) -> ConfigResult:
    """跑一条配置：特征/标签 → walk-forward OOS → 逐系列统计。

    每条序列同时算：**逐窗口 IC**（→ `ic_mean` / `ic_std` / `icir`）与
    **逐窗口命中**（→ `hit_rate`（Wilson 95% CI）+ `brier`）。
    无法评估的指标写 `scoring=NOT_EVALUATED` 并说明原因（**不进 BH 分母**）。
    """
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    data = add_labels(compute_features(frame), config.horizon_bars)
    usable = data.dropna(subset=[*FEATURE_NAMES, "_future_ret"])
    meta: dict[str, Any] = {
        "config": config.name,
        "description": config.description,
        "interval": config.interval,
        "lookback": config.lookback,
        "horizon_bars": config.horizon_bars,
        "window_bars": config.window_bars,
        "rows_total": int(len(frame)),
        "rows_usable": int(len(usable)),
        "rows_dropped_warmup_or_tail": int(len(frame) - len(usable)),
        "train_ratio": train_ratio,
        "seed": seed,
    }
    if not usable.empty:
        meta["first_signal_at"] = usable.index[0].isoformat()
        meta["last_signal_at"] = usable.index[-1].isoformat()

    windows, skipped_windows = walk_forward_windows(
        len(usable),
        train_ratio=train_ratio,
        window_bars=config.window_bars,
        horizon=config.horizon_bars,
        min_train=min_train,
    )
    meta["windows"] = len(windows)
    meta["windows_skipped"] = skipped_windows
    meta["oos_rows"] = int(sum(end - start for _train, start, end in windows))
    if not windows:
        meta["note"] = "没有可用的 OOS 窗口（样本不足）"
        return ConfigResult(config, _empty_stats(config.name, "无可用 OOS 窗口（样本不足）"), meta)

    all_series: tuple[str, ...] = (*FEATURE_NAMES, *BASELINE_NAMES, *MODEL_NAMES)
    ic_windows: dict[str, list[float]] = {name: [] for name in all_series}
    ic_undefined: dict[str, int] = dict.fromkeys(all_series, 0)
    hit_total: dict[str, int] = dict.fromkeys(all_series, 0)
    obs_total: dict[str, int] = dict.fromkeys(all_series, 0)
    brier_total: dict[str, float] = dict.fromkeys(all_series, 0.0)
    model_windows_skipped = 0
    rng = np.random.default_rng(seed)

    for train_end, start, end in windows:
        train = usable.iloc[:train_end]  # embargo 已由 walk_forward_windows 应用
        test = usable.iloc[start:end]
        outcome = (test["_future_ret"] > 0).astype(int)
        future = test["_future_ret"]
        size = len(test)
        truth = outcome.to_numpy(dtype=bool)

        def _record_ic(name: str, ic: float | None) -> None:
            if ic is None:
                ic_undefined[name] += 1
            else:
                ic_windows[name].append(ic)

        def _record_flags(
            name: str,
            predicted_up: pd.Series,
            *,
            proba: pd.Series | None = None,
            # 默认参数在**定义时**绑定当前迭代的 truth/size（ruff B023：闭包不得引用循环变量）
            _truth: np.ndarray = truth,
            _size: int = size,
        ) -> None:
            guess = predicted_up.to_numpy(dtype=bool)
            hit_total[name] += int(np.sum(guess == _truth))
            obs_total[name] += _size
            probabilities = (
                np.where(guess, 1.0, 0.0) if proba is None else proba.to_numpy(dtype=float)
            )
            brier_total[name] += float(np.sum((probabilities - _truth.astype(float)) ** 2))

        # 1) 单特征：IC（纯 OOS 秩相关）+ 方向命中率（有方向含义的特征才做）
        for name in FEATURE_NAMES:
            _record_ic(name, spearman_ic(test[name], future))
            threshold = FEATURE_DIRECTIONAL_THRESHOLD.get(name)
            if threshold is None:
                continue  # 波动类特征：只评估 IC（命中率在统计层记未评估）
            scores = test[name].to_numpy(dtype=float) - threshold
            _record_flags(name, pd.Series(scores > 0, index=test.index))

        # 2) 基准（docs/08 §6：random / always long / momentum / MA）
        random_scores = pd.Series(rng.random(size), index=test.index)
        _record_ic("random", spearman_ic(random_scores, future))
        _record_flags("random", random_scores >= 0.5, proba=random_scores)

        always_up = pd.Series(True, index=test.index)
        _record_ic("always_long", None)  # 常量信号 → IC 无定义（记未评估，不记 0）
        _record_flags("always_long", always_up, proba=pd.Series(1.0, index=test.index))

        momentum_scores = pd.Series(
            np.sign(test["momentum_20"].to_numpy(dtype=float)), index=test.index
        )
        _record_ic("momentum", spearman_ic(momentum_scores, future))
        _record_flags("momentum", momentum_scores > 0)

        ma_scores = pd.Series(np.sign(test["ma_gap_20"].to_numpy(dtype=float)), index=test.index)
        _record_ic("ma", spearman_ic(ma_scores, future))
        _record_flags("ma", ma_scores > 0)

        # 3) 模型（标准化**只在训练段拟合**；训练段不足/单一类别 → 本窗口模型未评估）
        x_train = train.loc[:, list(FEATURE_NAMES)].to_numpy(dtype=float)
        y_train = (train["_future_ret"] > 0).to_numpy(dtype=int)
        x_test = test.loc[:, list(FEATURE_NAMES)].to_numpy(dtype=float)
        if len(train) >= min_train and len(np.unique(y_train)) == 2:
            scaler = StandardScaler().fit(x_train)
            for model_name, model in (
                ("model_lr", LogisticRegression(max_iter=1000, random_state=seed)),
                ("model_hgb", HistGradientBoostingClassifier(random_state=seed)),
            ):
                try:
                    model.fit(scaler.transform(x_train), y_train)
                except Exception:  # noqa: BLE001 - 单窗口拟合失败不得中断整轮
                    model_windows_skipped += 1
                    continue
                proba = pd.Series(
                    model.predict_proba(scaler.transform(x_test))[:, 1], index=test.index
                )
                _record_ic(model_name, spearman_ic(proba, future))
                _record_flags(model_name, proba >= 0.5, proba=proba)
        else:
            model_windows_skipped += 2  # 两个模型都未评估

    meta["model_windows_skipped"] = model_windows_skipped
    stats = _assemble_stats(
        config.name,
        all_series=all_series,
        ic_windows=ic_windows,
        ic_undefined=ic_undefined,
        hit_total=hit_total,
        obs_total=obs_total,
        brier_total=brier_total,
    )
    return ConfigResult(config, stats, meta)


def _kind_of(series_id: str) -> str:
    if series_id in FEATURE_NAMES:
        return "feature"
    if series_id in BASELINE_NAMES:
        return "baseline"
    return "model"


def _series_stats(
    *,
    config_name: str,
    series_id: str,
    values: Sequence[float],
    undefined_windows: int,
    hits: int,
    obs: int,
    brier_sum: float,
    hit_reason: str = "无 OOS 样本（该序列未在任何窗口产生预测）",
) -> list[SeriesStat]:
    """单个序列的全部统计行（IC 家族 + 命中率 + Brier）；不可评估的一律标 `NOT_EVALUATED`。"""
    kind = _kind_of(series_id)
    windows_used = len(values)
    rows: list[SeriesStat] = []

    def _row(
        metric: str,
        value: float | None,
        *,
        note: str = "",
        scoring: str = "",
        p_value: float | None = None,
        ci_low: float | None = None,
        ci_high: float | None = None,
    ) -> SeriesStat:
        return SeriesStat(
            config=config_name,
            kind=kind,
            series_id=series_id,
            metric=metric,
            value=value,
            n_obs=obs,
            n_windows=windows_used,
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
            scoring=scoring,
            note=note,
        )

    # --- IC 家族（ic_mean / ic_std / icir / ic_positive_ratio）---
    if windows_used >= MIN_WINDOWS_FOR_ICIR:
        mean = float(np.mean(values))
        std = float(np.std(values, ddof=1))
        icir = (mean / std) if std > 0 else None
        t_stat = (icir * math.sqrt(windows_used)) if icir is not None else None
        positive = sum(1 for value in values if value > 0)
        note = f"IC>0 窗口 {positive}/{windows_used}"
        if undefined_windows:
            note += f"；另 {undefined_windows} 个窗口 IC 无定义"
        rows.append(
            _row(
                "ic_mean",
                mean,
                p_value=(two_sided_p_from_t(t_stat) if t_stat is not None else None),
                note=note,
            )
        )
        rows.append(_row("ic_std", std, note="逐窗口 IC 标准差（ddof=1）"))
        rows.append(
            _row(
                "icir",
                icir,
                note="IC 均值 / 标准差" if icir is not None else "IC 标准差为 0 → ICIR 无定义",
            )
        )
        rows.append(_row("ic_positive_ratio", positive / windows_used, note="IC>0 的窗口占比"))
    else:
        reason = f"有效窗口 {windows_used} < {MIN_WINDOWS_FOR_ICIR}" + (
            f"（另 {undefined_windows} 个窗口 IC 无定义：常量信号或样本不足）"
            if undefined_windows
            else ""
        )
        for metric in ("ic_mean", "ic_std", "icir", "ic_positive_ratio"):
            rows.append(_row(metric, None, scoring=NOT_EVALUATED_MARKER, note=reason))

    # --- 命中率（Wilson 95% CI）与 Brier ---
    if obs > 0:
        hit_rate = hits / obs
        low, high = wilson_interval(hits, obs)
        rows.append(_row("hit_rate", hit_rate, ci_low=low, ci_high=high, note=f"{hits}/{obs}"))
        rows.append(_row("brier", brier_sum / obs, note="越低越好"))
    else:
        for metric in ("hit_rate", "brier"):
            rows.append(
                _row(
                    metric,
                    None,
                    scoring=NOT_EVALUATED_MARKER,
                    note=hit_reason,
                )
            )
    return rows


def _assemble_stats(
    config_name: str,
    *,
    all_series: Sequence[str],
    ic_windows: Mapping[str, Sequence[float]],
    ic_undefined: Mapping[str, int],
    hit_total: Mapping[str, int],
    obs_total: Mapping[str, int],
    brier_total: Mapping[str, float],
) -> list[SeriesStat]:
    """把逐窗口累计量整理成统计长表：IC 家族 + 命中率（Wilson CI）+ Brier + BH-FDR。"""
    stats: list[SeriesStat] = []
    for series_id in all_series:
        hit_reason = (
            "该特征无方向含义（波动类），只评估 IC，不做方向预测"
            if series_id in NON_DIRECTIONAL_FEATURES
            else "无 OOS 样本（该序列未在任何窗口产生预测）"
        )
        stats.extend(
            _series_stats(
                config_name=config_name,
                series_id=series_id,
                values=list(ic_windows.get(series_id, ())),
                undefined_windows=int(ic_undefined.get(series_id, 0)),
                hits=int(hit_total.get(series_id, 0)),
                obs=int(obs_total.get(series_id, 0)),
                brier_sum=float(brier_total.get(series_id, 0.0)),
                hit_reason=hit_reason,
            )
        )
    ic_rows = [row for row in stats if row.metric == "ic_mean" and row.p_value is not None]

    # --- 多重检验：BH-FDR 只作用于**可评估**的 ic_mean（未评估不进分母）---
    evaluated = list(ic_rows)
    if evaluated:
        adjusted = bh_fdr([row.p_value for row in evaluated if row.p_value is not None])
        for row, q_value in zip(evaluated, adjusted, strict=True):
            row.p_bh = q_value
            threshold = "✅ 显著" if q_value <= FDR_ALPHA else "不显著"
            row.note = f"{row.note}；BH-q={q_value:.4f} {threshold}" if row.note else threshold
    return stats


# ---------------------------------------------------------------------------
# 报告与落盘
# ---------------------------------------------------------------------------
def _fmt(value: float | None, digits: int = 4) -> str:
    return "—（未评估）" if value is None else f"{value:.{digits}f}"


def _pct_or_dash(value: float | None) -> str:
    return "—（未评估）" if value is None else format_pct(value)


def _lookup(stats: Sequence[SeriesStat], series_id: str, metric: str) -> SeriesStat | None:
    for row in stats:
        if row.series_id == series_id and row.metric == metric:
            return row
    return None


def _stat_row(stats: Sequence[SeriesStat], series_id: str) -> str:
    """报告表格的一行（缺失/未评估一律显示"未评估"，不显示 0）。"""
    ic = _lookup(stats, series_id, "ic_mean")
    icir = _lookup(stats, series_id, "icir")
    hit = _lookup(stats, series_id, "hit_rate")
    brier = _lookup(stats, series_id, "brier")
    q_value = ic.p_bh if ic is not None else None
    q_text = (
        "—（未评估）"
        if q_value is None
        else f"{q_value:.4f}" + (" ✅" if q_value <= FDR_ALPHA else "")
    )
    if (
        hit is not None
        and hit.value is not None
        and hit.ci_low is not None
        and hit.ci_high is not None
    ):
        hit_text = (
            f"{format_pct(hit.value)}（95% CI {format_pct(hit.ci_low)}~{format_pct(hit.ci_high)}）"
        )
    else:
        hit_text = "—（未评估）"
    windows = ic.n_windows if ic is not None else 0
    return (
        f"| `{series_id}` | {_fmt(ic.value if ic is not None else None)} "
        f"| {_fmt(icir.value if icir is not None else None)} | {q_text} | {hit_text} "
        f"| {_fmt(brier.value if brier is not None else None)} | {windows} |"
    )


def render_report(
    results: Sequence[ConfigResult],
    *,
    symbol: str,
    data_notes: Sequence[str],
    generated_at: datetime,
    repro_command: str,
) -> str:
    """生成探针报告（Markdown，纯函数；**只陈述测量结果与局限**）。"""
    lines: list[str] = []
    add = lines.append
    primary = results[0] if results else None

    add("# Phase 3 最小验证探针报告 · 技术面对黄金收益的 OOS IC")
    add("")
    add(
        "> **性质**：**只读探针**（零迁移、不写数据库、不引入新依赖）。目的是判断"
        "「技术面信号对黄金未来收益有没有独立、可复现的预测力」，**不是** Phase 3 验收结论。"
    )
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(f"- 标的：`{symbol}`；配置数：{len(results)}")
    add(
        "- **OOS 纪律**：时间序 walk-forward（禁止 shuffle）；训练段末尾回退 `horizon` 根"
        "（embargo）；标准化只在训练段拟合；标签 = `log(close[t+h]/open[t+1])`。"
    )
    add(
        "- **不使用任何宏观数据**（`macro_events` 缺 `released_at`，存在前视风险 R3 → 由探针阶段的"
        "独立工具先行修复；本脚本不消费宏观输入）。"
    )
    for note in data_notes:
        add(f"- {note}")
    add("")

    if primary is not None:
        meta = primary.meta
        add(f"## 0. 结论摘要（主探针 `{primary.config.name}`：{primary.config.description}）")
        add("")
        add(
            f"- 数据：{meta.get('rows_total', 0)} 根 bar → 可用样本 {meta.get('rows_usable', 0)} 根"
            f"（预热/尾部丢弃 {meta.get('rows_dropped_warmup_or_tail', 0)} 根）；"
            f"OOS 窗口 {meta.get('windows', 0)} 个、OOS 行 {meta.get('oos_rows', 0)}"
        )
        significant = [
            row
            for row in primary.stats
            if row.metric == "ic_mean"
            and row.value is not None
            and row.p_bh is not None
            and row.p_bh <= FDR_ALPHA
        ]
        if significant:
            add("- **BH-FDR 校正后显著（q ≤ 0.05）的序列**：")
            for row in significant:
                direction = "正" if (row.value or 0.0) > 0 else "负"
                base_note = (row.note or "").split("；BH-q=")[0]  # 去掉重复的 BH 后缀
                add(
                    f"  - `{row.series_id}`（{row.kind}）：IC 均值 {_fmt(row.value)}"
                    f"（{direction}向）、BH-q {_fmt(row.p_bh)}、{base_note}"
                )
        else:
            add(
                "- **没有任何序列通过 BH-FDR 校正（q ≤ 0.05）** → 该窗口内看不到稳定的"
                "技术面预测力。"
            )
        add(
            "- 与 4 个基准的对照见 §3：**只有明显超过 `always_long` / `momentum` / `ma` 的 IC "
            "才算增量信息**（`always_long` 的命中率就是样本的上涨占比）。"
        )
        add("")

    for index, result in enumerate(results, start=1):
        meta = result.meta
        add(f"## {index}. 配置 `{result.config.name}`：{result.config.description}")
        add("")
        add(
            f"- 周期 `{meta.get('interval')}` / 回溯 `{meta.get('lookback')}` / "
            f"horizon {meta.get('horizon_bars')} 根 / OOS 窗口 {meta.get('window_bars')} 根"
        )
        add(
            f"- 可用样本 {meta.get('rows_usable')} 根、OOS 窗口 {meta.get('windows')} 个"
            f"（跳过 {meta.get('windows_skipped')} 个）、OOS 行 {meta.get('oos_rows')}"
            f"、模型窗口跳过 {meta.get('model_windows_skipped', 0)} 次"
        )
        add("")
        add("| 序列 | IC 均值 | ICIR | BH-q | 命中率（Wilson 95% CI） | Brier | 有效窗口 |")
        add("|---|---|---|---|---|---|---|")
        for name in (*FEATURE_NAMES, *BASELINE_NAMES, *MODEL_NAMES):
            add(_stat_row(result.stats, name))
        add("")

    add("## 4. 未评估台账（`scoring=NOT_EVALUATED`，**不计入任何分母**）")
    add("")
    add("| 配置 | 序列 | 指标 | 原因 |")
    add("|---|---|---|---|")
    unevaluated = 0
    for result in results:
        for row in result.stats:
            if row.scoring == NOT_EVALUATED_MARKER:
                unevaluated += 1
                if unevaluated <= MAX_UNEVALUATED_ROWS:
                    add(f"| `{row.config}` | `{row.series_id}` | {row.metric} | {row.note} |")
    if unevaluated == 0:
        add("| — | — | — | （无未评估项） |")
    add("")
    add(f"共 **{unevaluated}** 条未评估记录（完整清单见 `ic_long_table.csv` 的 `scoring` 列）。")
    add("")
    add("## 5. 局限与下一步")
    add("")
    add(
        "1. **样本量**：探针刻意只取 1 年日线 / 2 年小时线，OOS 窗口数有限 → 任何 IC 都必须带 "
        "CI 与 q 值来读，**不得**用点估计下结论；"
    )
    add(
        "2. **多重检验**：8 特征 × 2 模型 × 多配置 → 已做 BH-FDR；**q > 0.05 一律视为「未证明」**；"
    )
    add("3. **单标的**：只有 XAUUSD，不能外推成「技术面在黄金上有普遍预测力」的结论；")
    add("4. **无交易成本**：本探针只测「信号质量」，不测可交易性（成本与滑点属 Phase 4 回测）；")
    add(
        "5. **不进 Phase 3 结论**：探针只用于决定「是否投入完整数据底座 + Regime」；"
        "若 IC 不显著，应优先考虑「换特征 / 扩样本」而不是上更复杂的模型（`.clinerules` 禁止 DL）。"
    )
    add(
        "6. **Brier 只在同类之间可比**：`random` 输出连续概率，而特征/`momentum`/`ma` 输出 0/1 判定"
        " → 跨类比较 Brier 会误导（本条与上条同属口径限制，报告读者必读）。"
    )
    add("")
    add("## 附录 A. 复现命令")
    add("")
    add("```powershell")
    add(repro_command)
    add("```")
    add("")
    add("## 附录 B. 数据指纹与审计")
    add("")
    for result in results:
        meta = result.meta
        add(
            f"- `{meta.get('config')}`：ticker `{meta.get('provider_symbol')}`、"
            f"内容指纹 `{meta.get('digest')}`、bar 数 {meta.get('rows_total')}、"
            f"区间 `{meta.get('first_bar_at')}` ~ `{meta.get('last_bar_at')}`、"
            f"provider 跳过 {meta.get('skipped_bars')} 根"
        )
    add("")
    return "\n".join(lines) + "\n"


def write_outputs(
    *,
    out_dir: Path,
    report_path: Path,
    symbol: str,
    fetches: Mapping[str, FetchResult],
    results: Sequence[ConfigResult],
    report_text: str,
    meta_payload: Mapping[str, Any],
) -> list[Path]:
    """落盘：bar 快照 + 统计长表 + 元数据 + 报告（**只在 `--no-dry-run` 下调用**）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fetch in fetches.values():
        path = out_dir / f"{symbol}_{fetch.interval}_{fetch.lookback}.csv"
        fetch.frame.reset_index().to_csv(path, index=False, encoding="utf-8")
        written.append(path)
    long_table = out_dir / "ic_long_table.csv"
    with long_table.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LONG_TABLE_COLUMNS))
        writer.writeheader()
        for result in results:
            for row in result.stats:
                writer.writerow(row.to_row())
    written.append(long_table)
    meta_path = out_dir / "probe_ic_meta.json"
    meta_path.write_text(
        json.dumps(meta_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append(meta_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    written.append(report_path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def select_configs(names: str) -> tuple[ProbeConfig, ...]:
    """按逗号分隔的名字选配置（空 = 全部）。

    Raises:
        ValueError: 出现未知配置名（**不静默忽略**）。
    """
    if not names.strip():
        return PROBE_CONFIGS
    wanted = [item.strip() for item in names.split(",") if item.strip()]
    known = {config.name: config for config in PROBE_CONFIGS}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(f"未知配置 {unknown}；可选：{sorted(known)}")
    return tuple(known[name] for name in wanted)


def build_repro_command(args: argparse.Namespace) -> str:
    """报告附录里的复现命令（与本次实际参数一致）。"""
    parts = ["python scripts/probe_ic.py", f"--symbol {args.symbol}"]
    if args.configs.strip():
        parts.append(f"--configs {args.configs}")
    if args.source == "fixture":
        parts.append(f"--source fixture --bars-fixture {args.bars_fixture}")
    parts.append(f"--train-ratio {args.train_ratio} --seed {args.seed}")
    parts.append(f"--out-dir {args.out_dir}")
    parts.append("--dry-run")
    return " ".join(parts) + "   # 默认 dry-run；落盘需显式 --no-dry-run"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/probe_ic.py",
        description="Phase 3 最小验证探针：技术面特征 → 黄金未来收益的 OOS IC（只读、零迁移）",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"标的（默认 {DEFAULT_SYMBOL}）")
    parser.add_argument(
        "--configs",
        default="",
        help=f"逗号分隔的配置名（默认全部：{','.join(c.name for c in PROBE_CONFIGS)}）",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "yahoo", "fixture"),
        default="auto",
        help="取数方式：auto/yahoo=Yahoo chart 真实联网；fixture=离线 CSV（测试与离线复算）",
    )
    parser.add_argument(
        "--bars-fixture",
        default="",
        help="fixture CSV（列：open_time,open,high,low,close[,volume]）",
    )
    parser.add_argument("--train-ratio", type=float, default=0.5, help="训练段占比（时间序切分）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（random 基准与模型）")
    parser.add_argument("--timeout", type=float, default=30.0, help="取数超时（秒）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="落盘目录")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径（Markdown）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印，不写任何文件（**默认行为**）"
    )
    parser.add_argument("--no-dry-run", action="store_true", help="显式允许落盘")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=参数或取数失败。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[probe] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run

    try:
        configs = select_configs(args.configs)
    except ValueError as exc:
        print(f"[probe] {exc}", file=sys.stderr)
        return 2
    if args.source == "fixture":
        if not args.bars_fixture:
            print("[probe] --source fixture 必须同时提供 --bars-fixture <csv>", file=sys.stderr)
            return 2
        if len(configs) != 1:
            print("[probe] fixture 模式一次只允许一条配置（--configs 只能给一个）", file=sys.stderr)
            return 2

    print(f"[probe] 标的={args.symbol}｜配置={[c.name for c in configs]}｜source={args.source}")
    print(
        f"[probe] OOS 纪律：时间序 walk-forward、embargo=horizon、train_ratio={args.train_ratio}、"
        f"seed={args.seed}｜**不使用任何宏观数据**（R3 未修）"
    )
    print(
        "[probe] 模式=dry-run（不写任何文件）"
        if dry_run
        else f"[probe] 模式=落盘：bars → {args.out_dir}｜报告 → {args.report}"
    )

    fetches: dict[str, FetchResult] = {}
    try:
        for config in configs:
            key = f"{config.interval}|{config.lookback}"
            if key in fetches:
                continue
            if args.source == "fixture":
                fixture = Path(args.bars_fixture)
                frame = load_bars_from_csv(fixture)
                fetches[key] = FetchResult(
                    f"fixture:{fixture.name}", config.interval, config.lookback, frame
                )
                print(
                    f"[probe] fixture：{fixture.name} → {len(frame)} 根 bar"
                    f"（{config.interval}/{config.lookback}）"
                )
            else:
                print(
                    f"[probe] 取数：{args.symbol} interval={config.interval} "
                    f"range={config.lookback}"
                )
                fetch = fetch_yahoo_bars(
                    args.symbol,
                    interval=config.interval,
                    lookback=config.lookback,
                    timeout=args.timeout,
                )
                fetches[key] = fetch
                print(
                    f"[probe]   → ticker={fetch.provider_symbol}｜bars={fetch.rows}"
                    f"｜provider 跳过 {fetch.skipped_bars}"
                )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[probe] 取数失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    results: list[ConfigResult] = []
    for config in configs:
        fetch = fetches[f"{config.interval}|{config.lookback}"]
        result = evaluate_config(fetch.frame, config, train_ratio=args.train_ratio, seed=args.seed)
        result.meta.update(
            {
                "provider_symbol": fetch.provider_symbol,
                "digest": fetch.digest(),
                "skipped_bars": fetch.skipped_bars,
                "first_bar_at": fetch.frame.index[0].isoformat() if fetch.rows else "",
                "last_bar_at": fetch.frame.index[-1].isoformat() if fetch.rows else "",
            }
        )
        results.append(result)
        ic_rows = [row for row in result.stats if row.metric == "ic_mean"]
        best = max(ic_rows, key=lambda row: abs(row.value or 0.0), default=None)
        unevaluated = sum(1 for row in result.stats if row.scoring == NOT_EVALUATED_MARKER)
        print(
            f"[probe] {config.name}：OOS 窗口 {result.meta.get('windows')} 个｜"
            f"最强 |IC| 序列 `{best.series_id if best is not None else '—'}`"
            f"={_fmt(best.value if best is not None else None)}｜未评估 {unevaluated} 条"
        )

    report_text = render_report(
        results,
        symbol=args.symbol,
        data_notes=[
            f"取数方式：`{args.source}`（provider ticker 见附录 B）",
            f"探针矩阵：{[config.name for config in configs]}",
            *(
                [
                    "⚠️ **本次取数来自离线 fixture（可能是合成数据）**：结果只能用于验证管道与"
                    "产物形态，**不得**解读为任何市场结论。"
                ]
                if args.source == "fixture"
                else []
            ),
        ],
        generated_at=datetime.now(UTC),
        repro_command=build_repro_command(args),
    )
    if dry_run:
        _safe_print(report_text)
        print("[probe] --dry-run：未写任何文件（加 --no-dry-run 才落盘）")
        return 0

    meta_payload: dict[str, Any] = {
        "symbol": args.symbol,
        "source": args.source,
        "train_ratio": args.train_ratio,
        "seed": args.seed,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "configs": [result.meta for result in results],
        "fetches": {
            key: {
                "provider_symbol": fetch.provider_symbol,
                "interval": fetch.interval,
                "lookback": fetch.lookback,
                "rows": fetch.rows,
                "skipped_bars": fetch.skipped_bars,
                "digest": fetch.digest(),
            }
            for key, fetch in fetches.items()
        },
        "caveats": [
            "只读探针：未写数据库、未新增迁移、未引入新依赖",
            "不使用任何宏观数据（macro_events 缺 released_at，R3 未修）",
            "**不是** Phase 3 验收结论；q > 0.05 一律视为未证明",
            "无交易成本口径（可交易性属 Phase 4 回测）",
        ],
    }
    written = write_outputs(
        out_dir=Path(args.out_dir),
        report_path=Path(args.report),
        symbol=args.symbol,
        fetches=fetches,
        results=results,
        report_text=report_text,
        meta_payload=meta_payload,
    )
    print("[probe] 已写出：")
    for path in written:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
