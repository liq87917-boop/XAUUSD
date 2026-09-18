from __future__ import annotations

import numpy as np
import pandas as pd

from src.alpha.technical import FEATURE_NAMES, add_strict_labels, compute_features


def test_future_price_injection_does_not_change_prior_features() -> None:
    index = pd.date_range("2025-01-01", periods=200, freq="h", tz="UTC")
    close = 2000 + np.sin(np.arange(200) / 7)
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "close_time": index + pd.Timedelta(hours=1),
        },
        index=index,
    )
    before = compute_features(bars).loc[index[100], FEATURE_NAMES]
    bars.loc[index[101] :, ["open", "high", "low", "close"]] *= 5
    after = compute_features(bars).loc[index[100], FEATURE_NAMES]
    pd.testing.assert_series_equal(before, after)


def test_every_label_entry_is_strictly_after_signal_at() -> None:
    index = pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")
    close = np.linspace(2000, 2010, 100)
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "close_time": index + pd.Timedelta(hours=1),
        },
        index=index,
    )
    labelled = add_strict_labels(compute_features(bars), horizon=4).dropna(subset=["_future_ret"])
    assert (pd.to_datetime(labelled["_entry_at"], utc=True) > labelled["close_time"]).all()
