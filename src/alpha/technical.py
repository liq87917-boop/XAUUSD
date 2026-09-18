"""Phase 3.2 Technical Alpha 的独立、时间因果 OOS 门禁。

本模块只评估信号质量，不包含仓位、交易、手续费或风控。概率模型采用
train -> validation Platt calibration -> untouched test 的三段式流程。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

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
HORIZON_BARS: Final[int] = 24
EMBARGO_BARS: Final[int] = HORIZON_BARS
SEED: Final[int] = 42


@dataclass(frozen=True, slots=True)
class Split:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    ic: float | None
    icir: float | None
    hit_rate: float
    wilson_low: float
    wilson_high: float
    hit_p_value: float
    brier: float
    observations: int
    windows: int


@dataclass(frozen=True, slots=True)
class TechnicalGateResult:
    metrics: tuple[Metric, ...]
    calibration_bins: tuple[dict[str, float | int], ...]
    raw_brier: float
    calibrated_brier: float
    rows_total: int
    rows_usable: int
    train_rows: int
    validation_rows: int
    test_rows: int
    non_overlapping_test_rows: int
    first_signal_at: pd.Timestamp
    last_signal_at: pd.Timestamp
    passed: bool


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    result = 100 - 100 / (1 + rs)
    result = result.where(avg_loss > 0, 100.0)
    result = result.where(avg_gain > 0, 0.0)
    return result.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)


def compute_features(frame: pd.DataFrame) -> pd.DataFrame:
    """仅用当前及历史已完成 K 线计算预注册的 8 个技术特征。"""
    required = {"open", "high", "low", "close", "close_time"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"K 线缺少列：{sorted(missing)}")
    data = frame.sort_index().copy()
    close = data["close"].astype(float)
    log_ret = np.log(close).diff()
    data["ret_1"] = log_ret
    data["momentum_20"] = log_ret.rolling(20).sum()
    data["ma_gap_20"] = close / close.rolling(20).mean() - 1
    data["rsi_14"] = _rsi(close)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    data["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / close
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            data["high"].astype(float) - data["low"].astype(float),
            (data["high"].astype(float) - previous_close).abs(),
            (data["low"].astype(float) - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr_14_pct"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / close
    data["realized_vol_20"] = log_ret.rolling(20).std(ddof=1)
    data["breakout_20"] = close / data["high"].astype(float).rolling(20).max().shift(1) - 1
    return data


def add_strict_labels(frame: pd.DataFrame, horizon: int = HORIZON_BARS) -> pd.DataFrame:
    """生成严格标签：入场 bar 的 open_time 必须晚于特征 bar 的 close_time。

    horizon 按可交易的 1h bar 计数。允许 Dukascopy 的日常计划性停盘（<=6h），
    但周末或异常长空洞不会被插值，也不会缩短窗口。
    """
    if horizon < 1:
        raise ValueError("horizon 必须 >= 1")
    data = frame.sort_index().copy()
    opens = pd.DatetimeIndex(data.index)
    closes = pd.to_datetime(data["close_time"], utc=True)
    entry_positions = opens.searchsorted(closes, side="right")
    future = np.full(len(data), np.nan, dtype=float)
    entry_at: list[pd.Timestamp | pd.NaT] = [pd.NaT] * len(data)
    exit_at: list[pd.Timestamp | pd.NaT] = [pd.NaT] * len(data)
    for row, entry_pos in enumerate(entry_positions):
        exit_pos = int(entry_pos) + horizon - 1
        if exit_pos >= len(data):
            continue
        window = opens[int(entry_pos) : exit_pos + 1]
        if len(window) != horizon or (window[1:] - window[:-1] > pd.Timedelta(hours=6)).any():
            continue
        entry_price = float(data.iloc[int(entry_pos)]["open"])
        exit_price = float(data.iloc[exit_pos]["close"])
        future[row] = math.log(exit_price / entry_price)
        entry_at[row] = opens[int(entry_pos)]
        exit_at[row] = pd.Timestamp(data.iloc[exit_pos]["close_time"])
    data["_future_ret"] = future
    data["_entry_at"] = entry_at
    data["_exit_at"] = exit_at
    return data


def time_split(data: pd.DataFrame, embargo: int = EMBARGO_BARS) -> Split:
    """60/20/20 时间切分；两个边界各留一个完整标签长度的隔离带。"""
    if len(data) < 500:
        raise ValueError("可用样本不足 500 行")
    train_end = int(len(data) * 0.60)
    validation_end = int(len(data) * 0.80)
    train = data.iloc[:train_end]
    validation = data.iloc[train_end + embargo : validation_end]
    test = data.iloc[validation_end + embargo :]
    if min(map(len, (train, validation, test))) == 0:
        raise ValueError("时间切分后存在空区间")
    return Split(train, validation, test)


def _spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 10 or np.unique(left).size < 2 or np.unique(right).size < 2:
        return None
    value = pd.Series(left).corr(pd.Series(right), method="spearman")
    return None if pd.isna(value) else float(value)


def _wilson(hits: int, total: int) -> tuple[float, float]:
    z = 1.959963984540054
    p = hits / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return center - margin, center + margin


def _binomial_greater_p(hits: int, total: int) -> float:
    """精确单侧二项检验 P(X >= hits), X~Binom(total, 0.5)。"""
    if hits <= total / 2:
        return 1.0
    logs = [
        math.lgamma(total + 1)
        - math.lgamma(k + 1)
        - math.lgamma(total - k + 1)
        - total * math.log(2)
        for k in range(hits, total + 1)
    ]
    peak = max(logs)
    return min(1.0, math.exp(peak) * sum(math.exp(item - peak) for item in logs))


def _metric(
    name: str, score: np.ndarray, truth_return: np.ndarray, probability: np.ndarray
) -> Metric:
    outcome = truth_return > 0
    prediction = probability >= 0.5
    hits = int(np.sum(prediction == outcome))
    low, high = _wilson(hits, len(outcome))
    window_ics: list[float] = []
    for start in range(0, len(score), 24):
        value = _spearman(score[start : start + 24], truth_return[start : start + 24])
        if value is not None:
            window_ics.append(value)
    ic = _spearman(score, truth_return)
    std = float(np.std(window_ics, ddof=1)) if len(window_ics) >= 2 else 0.0
    icir = float(np.mean(window_ics) / std) if std > 0 else None
    return Metric(
        name=name,
        ic=ic,
        icir=icir,
        hit_rate=hits / len(outcome),
        wilson_low=low,
        wilson_high=high,
        hit_p_value=_binomial_greater_p(hits, len(outcome)),
        brier=float(np.mean((probability - outcome.astype(float)) ** 2)),
        observations=len(outcome),
        windows=len(window_ics),
    )


def _calibration_bins(
    probability: np.ndarray, outcome: np.ndarray
) -> tuple[dict[str, float | int], ...]:
    bins: list[dict[str, float | int]] = []
    bucket = np.minimum((probability * 10).astype(int), 9)
    for index in range(10):
        mask = bucket == index
        if mask.any():
            bins.append(
                {
                    "bin": index,
                    "count": int(mask.sum()),
                    "mean_probability": float(probability[mask].mean()),
                    "observed_rate": float(outcome[mask].mean()),
                }
            )
    return tuple(bins)


def evaluate_technical_gate(frame: pd.DataFrame, seed: int = SEED) -> TechnicalGateResult:
    """执行一次冻结的 Technical Alpha OOS 门禁，不写数据库。"""
    labelled = add_strict_labels(compute_features(frame))
    usable = labelled.dropna(subset=[*FEATURE_NAMES, "_future_ret"]).copy()
    split = time_split(usable)
    x_train = split.train.loc[:, FEATURE_NAMES].to_numpy(float)
    x_validation = split.validation.loc[:, FEATURE_NAMES].to_numpy(float)
    x_test = split.test.loc[:, FEATURE_NAMES].to_numpy(float)
    y_train = (split.train["_future_ret"].to_numpy(float) > 0).astype(int)
    y_validation = (split.validation["_future_ret"].to_numpy(float) > 0).astype(int)
    scaler = StandardScaler().fit(x_train)
    model = LogisticRegression(max_iter=1000, random_state=seed).fit(
        scaler.transform(x_train), y_train
    )
    validation_logit = model.decision_function(scaler.transform(x_validation)).reshape(-1, 1)
    calibrator = LogisticRegression(max_iter=1000, random_state=seed).fit(
        validation_logit, y_validation
    )
    test_logit = model.decision_function(scaler.transform(x_test)).reshape(-1, 1)
    raw_probability = model.predict_proba(scaler.transform(x_test))[:, 1]
    calibrated_probability = calibrator.predict_proba(test_logit)[:, 1]

    # 显著性与正式门槛使用非重叠样本，避免 24h 标签重叠夸大有效样本数。
    positions = np.arange(0, len(split.test), HORIZON_BARS)
    test = split.test.iloc[positions]
    returns = test["_future_ret"].to_numpy(float)
    calibrated = calibrated_probability[positions]
    rng = np.random.default_rng(seed)
    random_probability = rng.random(len(test))
    momentum = test["momentum_20"].to_numpy(float)
    ma_gap = test["ma_gap_20"].to_numpy(float)
    metrics = (
        _metric("model_lr_platt", calibrated, returns, calibrated),
        _metric("random", random_probability, returns, random_probability),
        _metric("always_long", np.ones(len(test)), returns, np.ones(len(test))),
        _metric("momentum", momentum, returns, (momentum > 0).astype(float)),
        _metric("ma", ma_gap, returns, (ma_gap > 0).astype(float)),
    )
    main = metrics[0]
    passed = bool(
        (main.ic is not None and main.ic >= 0.03 and main.icir is not None and main.icir >= 0.3)
        or (main.hit_rate > 0.5 and main.hit_p_value < 0.05)
    )
    outcome = (split.test["_future_ret"].to_numpy(float) > 0).astype(float)
    return TechnicalGateResult(
        metrics=metrics,
        calibration_bins=_calibration_bins(calibrated_probability, outcome),
        raw_brier=float(np.mean((raw_probability - outcome) ** 2)),
        calibrated_brier=float(np.mean((calibrated_probability - outcome) ** 2)),
        rows_total=len(frame),
        rows_usable=len(usable),
        train_rows=len(split.train),
        validation_rows=len(split.validation),
        test_rows=len(split.test),
        non_overlapping_test_rows=len(test),
        first_signal_at=pd.Timestamp(usable.index[0]),
        last_signal_at=pd.Timestamp(usable.index[-1]),
        passed=passed,
    )
