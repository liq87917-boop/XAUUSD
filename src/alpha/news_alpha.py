"""Phase 3.3 News Alpha 的离线信号框架（纯函数：不读 DB、不写权重、不含仓位/风控）。

输入（由调用方组装，本模块不读数据库）：
- ``news_events``：结构化新闻（headline / sentiment / importance / event_type /
  published_at / effective_at）；
- ``raw_items(NEWS)``：原始新闻（补齐 headline 文本）；
- HF 标题情感弱监督（150 条）：只能作特征 / 弱标签，**不得回灌为观点金标准**
  （Phase 2 裁决，``docs/14 §3.3`` 验收标准 6）。

输出：事件/标题特征 → 未来收益的预测信号（概率，未经校准不得进 ``predictions``）。

时间因果（硬性要求）：新闻 ``published_at <= 信号 effective_at <= 标签起算点``。
标签起算点 = ``effective_at`` 之后第一根完整 bar 的 ``open``，入场 bar 的
``open_time`` 严格晚于 ``effective_at``（等时 bar 被排除，防未来数据泄漏）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from src.alpha.reproducibility import frame_digest

#: 预注册特征（第一版：情感分 + 重要性，均来自 news_events 结构化字段）
FEATURE_NAMES: Final[tuple[str, ...]] = ("sentiment", "importance")
#: 标签 horizon（可交易的 1h bar 数）；与 Phase 3.2 Technical 的 1d horizon 对齐
HORIZON_BARS: Final[int] = 24
#: 时间切分隔离带（= 1 个完整标签长度）
EMBARGO_BARS: Final[int] = HORIZON_BARS
SEED: Final[int] = 42
NEWS_MODEL_VERSION: Final[str] = "news-lr-platt-v1"
NEWS_FEATURE_SET_VERSION: Final[str] = "news-2-v1"
#: 三段式时间切分的最小可用样本数（低于此值不评估，报"样本不足"）
MIN_ROWS_FOR_SPLIT: Final[int] = 200


@dataclass(frozen=True, slots=True)
class NewsObservation:
    """一条可用于建模的新闻观测（事件 + 归属 + 特征字段）。"""

    event_id: str
    published_at: datetime
    effective_at: datetime
    sentiment: float | None
    importance: float | None
    headline: str | None = None
    event_type: str | None = None


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
class NewsGateResult:
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
    first_signal_at: datetime
    last_signal_at: datetime
    data_hash: str
    feature_set_version: str
    model_version: str
    seed: int
    passed: bool


def add_forward_return_labels(
    observations: list[NewsObservation],
    bars: pd.DataFrame,
    horizon: int = HORIZON_BARS,
) -> pd.DataFrame:
    """把新闻观测对齐到行情，计算未来收益标签（时间因果）。

    标签起算点 = ``effective_at`` 之后第一根完整 bar 的 ``open``（``open_time``
    严格晚于 ``effective_at``，等时 bar 被排除）。允许日内的计划性停盘（<=6h），
    周末/异常空洞不插值、不缩窗。

    Args:
        observations: 新闻观测（``effective_at`` 必须 timezone-aware）。
        bars: 按 ``open_time`` 升序的行情 DataFrame，index=open_time，
            列含 ``open`` / ``close`` / ``close_time``。
        horizon: 可交易 bar 数（默认 24 = 1d）。

    Returns:
        每行一条新闻观测 + ``forward_return`` 列的 DataFrame（对齐失败的观测被丢弃）。
    """
    if horizon < 1:
        raise ValueError("horizon 必须 >= 1")
    required = {"open", "close", "close_time"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars 缺少列：{sorted(missing)}")
    data = bars.sort_index().copy()
    opens = pd.DatetimeIndex(data.index)
    rows: list[dict[str, object]] = []
    for obs in observations:
        if obs.effective_at.tzinfo is None or obs.effective_at.utcoffset() is None:
            raise ValueError(f"新闻 {obs.event_id} 的 effective_at 必须带时区")
        effective = pd.Timestamp(obs.effective_at)
        entry_pos = int(opens.searchsorted(effective, side="right"))
        exit_pos = entry_pos + horizon - 1
        if entry_pos >= len(data) or exit_pos >= len(data):
            continue
        window = opens[entry_pos : exit_pos + 1]
        if len(window) != horizon or (window[1:] - window[:-1] > pd.Timedelta(hours=6)).any():
            continue
        entry_open = float(data.iloc[entry_pos]["open"])
        exit_close = float(data.iloc[exit_pos]["close"])
        rows.append(
            {
                "event_id": obs.event_id,
                "published_at": obs.published_at,
                "effective_at": obs.effective_at,
                "sentiment": obs.sentiment,
                "importance": obs.importance,
                "headline": obs.headline,
                "event_type": obs.event_type,
                "forward_return": math.log(exit_close / entry_open),
            }
        )
    return pd.DataFrame(rows)


def time_split(data: pd.DataFrame, embargo: int = EMBARGO_BARS) -> Split:
    """60/20/20 时间切分；两个边界各留一个完整标签长度的隔离带（禁止 shuffle）。"""
    if len(data) < MIN_ROWS_FOR_SPLIT:
        raise ValueError(f"可用样本不足 {MIN_ROWS_FOR_SPLIT} 行（实际 {len(data)}）")
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
    for start in range(0, len(score), HORIZON_BARS):
        value = _spearman(
            score[start : start + HORIZON_BARS], truth_return[start : start + HORIZON_BARS]
        )
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



def evaluate_news_gate(frame: pd.DataFrame, seed: int = SEED) -> NewsGateResult:
    """执行一次冻结的 News Alpha OOS 门禁（三段式，不写数据库）。

    ``frame`` 需含 ``FEATURE_NAMES`` 与 ``forward_return`` 列（由
    :func:`add_forward_return_labels` 产出）；按时间切分后 train -> validation
    Platt calibration -> untouched test。样本不足时抛 ``ValueError``（调用方
    据此报"样本不足"，不写信号）。
    """
    usable = (
        frame.dropna(subset=[*FEATURE_NAMES, "forward_return"])
        .sort_values("effective_at")
        .copy()
    )
    split = time_split(usable)
    x_train = split.train.loc[:, FEATURE_NAMES].to_numpy(float)
    x_validation = split.validation.loc[:, FEATURE_NAMES].to_numpy(float)
    x_test = split.test.loc[:, FEATURE_NAMES].to_numpy(float)
    y_train = (split.train["forward_return"].to_numpy(float) > 0).astype(int)
    y_validation = (split.validation["forward_return"].to_numpy(float) > 0).astype(int)
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
    returns = test["forward_return"].to_numpy(float)
    calibrated = calibrated_probability[positions]
    rng = np.random.default_rng(seed)
    random_probability = rng.random(len(test))
    sentiment = test["sentiment"].to_numpy(float)
    metrics = (
        _metric("model_lr_platt", calibrated, returns, calibrated),
        _metric("random", random_probability, returns, random_probability),
        _metric("always_long", np.ones(len(test)), returns, np.ones(len(test))),
        _metric("sentiment", sentiment, returns, (sentiment > 0).astype(float)),
    )
    main = metrics[0]
    passed = bool(
        (main.ic is not None and main.ic >= 0.03 and main.icir is not None and main.icir >= 0.3)
        or (main.hit_rate > 0.5 and main.hit_p_value < 0.05)
    )
    outcome = (split.test["forward_return"].to_numpy(float) > 0).astype(float)
    return NewsGateResult(
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
        first_signal_at=usable["effective_at"].iloc[0],
        last_signal_at=usable["effective_at"].iloc[-1],
        data_hash=frame_digest(frame, (*FEATURE_NAMES, "forward_return", "effective_at")),
        feature_set_version=NEWS_FEATURE_SET_VERSION,
        model_version=NEWS_MODEL_VERSION,
        seed=seed,
        passed=passed,
    )

