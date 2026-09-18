from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.alpha.technical import add_strict_labels, compute_features, time_split


def _bars(rows: int = 800) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    close = 2000 + np.arange(rows) * 0.1 + np.sin(np.arange(rows) / 9)
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "close_time": index + pd.Timedelta(hours=1),
        },
        index=index,
    )


def test_features_are_backward_looking() -> None:
    bars = _bars()
    original = compute_features(bars).iloc[300].copy()
    changed = bars.copy()
    changed.iloc[301:, changed.columns.get_loc("close")] *= 10
    replay = compute_features(changed).iloc[300]
    for name in original.index:
        if name in {"open", "high", "low", "close", "close_time"}:
            continue
        assert replay[name] == pytest.approx(original[name], nan_ok=True)


def test_strict_label_skips_bar_opening_at_signal_time() -> None:
    bars = _bars(40)
    labelled = add_strict_labels(bars, horizon=2)
    # signal_at = 01:00；01:00 open 与其相等，必须跳过；entry 应为 02:00。
    assert labelled.iloc[0]["_entry_at"] == bars.index[2]
    assert labelled.iloc[0]["_exit_at"] == bars.iloc[3]["close_time"]


def test_label_rejects_gap_inside_horizon() -> None:
    full = _bars(40)
    bars = full.drop(full.index[3:10])
    labelled = add_strict_labels(bars, horizon=3)
    assert pd.isna(labelled.iloc[0]["_future_ret"])


def test_time_split_has_two_embargoes() -> None:
    data = _bars(1000)
    split = time_split(data, embargo=24)
    assert split.train.index[-1] < split.validation.index[0]
    assert split.validation.index[-1] < split.test.index[0]
    assert data.index.get_loc(split.validation.index[0]) - len(split.train) == 24
    assert data.index.get_loc(split.test.index[0]) - 800 == 24
