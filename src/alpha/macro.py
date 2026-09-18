"""Phase 3.2 Macro Alpha 的 initial-release-only 独立 OOS 门禁。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from src.alpha.technical import Metric, _calibration_bins, _metric


@dataclass(frozen=True, slots=True)
class MacroGateResult:
    metrics: tuple[Metric, ...]
    calibration_bins: tuple[dict[str, float | int], ...]
    raw_brier: float
    calibrated_brier: float
    rows_usable: int
    train_rows: int
    validation_rows: int
    test_rows: int
    feature_names: tuple[str, ...]
    max_release_violation_count: int
    passed: bool


def build_macro_frame(
    gold: pd.DataFrame, macro_events: pd.DataFrame, dxy: pd.DataFrame
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """按 release time 向后 as-of 对齐宏观值，绝不按 observation date 偷看。"""
    gold_data = gold.sort_index().copy()
    gold_data.index.name = "open_time"
    signal_at = pd.to_datetime(gold_data["close_time"], utc=True)
    base = gold_data.assign(_signal_at=signal_at).reset_index().sort_values("_signal_at")

    events = macro_events.copy()
    events["released_at"] = pd.to_datetime(events["released_at"], utc=True)
    events = events.sort_values("released_at")
    feature_names: list[str] = []
    for code in sorted(events["event_code"].unique()):
        series = events.loc[events["event_code"] == code, ["released_at", "actual_value"]].copy()
        series["actual_value"] = pd.to_numeric(series["actual_value"])
        series[f"macro_{code}_level"] = series["actual_value"]
        series[f"macro_{code}_change"] = series["actual_value"].diff()
        series[f"macro_{code}_released_at"] = series["released_at"]
        series = series.drop(columns="actual_value")
        base = pd.merge_asof(base, series, left_on="_signal_at", right_on="released_at")
        base = base.drop(columns="released_at")
        feature_names.extend([f"macro_{code}_level", f"macro_{code}_change"])

    dxy_data = dxy.sort_index().copy()
    dxy_data.index.name = "open_time"
    dxy_close = dxy_data["close"].astype(float)
    dxy_data["dxy_ret_1"] = np.log(dxy_close).diff()
    dxy_data["dxy_momentum_20"] = np.log(dxy_close).diff().rolling(20).sum()
    dxy_data["dxy_effective_at"] = pd.to_datetime(dxy_data["close_time"], utc=True)
    dxy_features = dxy_data.reset_index()[
        ["dxy_effective_at", "dxy_ret_1", "dxy_momentum_20"]
    ].sort_values("dxy_effective_at")
    base = pd.merge_asof(base, dxy_features, left_on="_signal_at", right_on="dxy_effective_at")
    feature_names.extend(["dxy_ret_1", "dxy_momentum_20"])

    gold_close = base["close"].astype(float)
    base["gold_momentum_20"] = np.log(gold_close).diff().rolling(20).sum()
    base["gold_ma_gap_20"] = gold_close / gold_close.rolling(20).mean() - 1

    opens = pd.DatetimeIndex(pd.to_datetime(base["open_time"], utc=True))
    entry_positions = opens.searchsorted(pd.DatetimeIndex(base["_signal_at"]), side="right")
    returns = np.full(len(base), np.nan)
    for row, position in enumerate(entry_positions):
        if position >= len(base):
            continue
        returns[row] = np.log(
            float(base.iloc[position]["close"]) / float(base.iloc[position]["open"])
        )
    base["_future_ret"] = returns
    return base.set_index("open_time"), tuple(feature_names)


def evaluate_macro_gate(
    gold: pd.DataFrame, macro_events: pd.DataFrame, dxy: pd.DataFrame, seed: int = 42
) -> MacroGateResult:
    data, feature_names = build_macro_frame(gold, macro_events, dxy)
    usable = data.dropna(
        subset=[*feature_names, "_future_ret", "gold_momentum_20", "gold_ma_gap_20"]
    )
    if len(usable) < 500:
        raise ValueError("Macro Alpha 可用样本不足 500 行")
    train_end = int(len(usable) * 0.60)
    validation_end = int(len(usable) * 0.80)
    train = usable.iloc[:train_end]
    validation = usable.iloc[train_end + 1 : validation_end]
    test = usable.iloc[validation_end + 1 :]
    scaler = StandardScaler().fit(train.loc[:, feature_names].to_numpy(float))
    y_train = (train["_future_ret"].to_numpy(float) > 0).astype(int)
    y_validation = (validation["_future_ret"].to_numpy(float) > 0).astype(int)
    model = LogisticRegression(max_iter=1000, random_state=seed).fit(
        scaler.transform(train.loc[:, feature_names].to_numpy(float)), y_train
    )
    validation_logit = model.decision_function(
        scaler.transform(validation.loc[:, feature_names].to_numpy(float))
    ).reshape(-1, 1)
    calibrator = LogisticRegression(max_iter=1000, random_state=seed).fit(
        validation_logit, y_validation
    )
    test_x = scaler.transform(test.loc[:, feature_names].to_numpy(float))
    raw = model.predict_proba(test_x)[:, 1]
    calibrated = calibrator.predict_proba(model.decision_function(test_x).reshape(-1, 1))[:, 1]
    returns = test["_future_ret"].to_numpy(float)
    rng = np.random.default_rng(seed)
    random_probability = rng.random(len(test))
    momentum = test["gold_momentum_20"].to_numpy(float)
    ma_gap = test["gold_ma_gap_20"].to_numpy(float)
    metrics = (
        _metric("macro_lr_platt", calibrated, returns, calibrated),
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
    release_columns = [name for name in usable if name.endswith("_released_at")]
    violations = sum(
        int((pd.to_datetime(usable[name], utc=True) > usable["_signal_at"]).sum())
        for name in release_columns
    )
    outcome = (returns > 0).astype(float)
    return MacroGateResult(
        metrics=metrics,
        calibration_bins=_calibration_bins(calibrated, outcome),
        raw_brier=float(np.mean((raw - outcome) ** 2)),
        calibrated_brier=float(np.mean((calibrated - outcome) ** 2)),
        rows_usable=len(usable),
        train_rows=len(train),
        validation_rows=len(validation),
        test_rows=len(test),
        feature_names=feature_names,
        max_release_violation_count=violations,
        passed=passed,
    )
