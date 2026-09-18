from __future__ import annotations

import numpy as np
import pandas as pd

from src.alpha.macro import build_macro_frame


def _daily_bars(rows: int = 80) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="D", tz="UTC")
    close = np.linspace(2000, 2100, rows)
    return pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "close_time": index + pd.Timedelta(days=1),
        },
        index=index,
    )


def test_macro_value_is_invisible_before_release() -> None:
    gold = _daily_bars()
    dxy = _daily_bars().assign(close=np.linspace(100, 101, 80))
    events = pd.DataFrame(
        {
            "event_code": ["TEST", "TEST"],
            "released_at": ["2025-01-10T00:00:00Z", "2025-02-10T00:00:00Z"],
            "actual_value": [1.0, 9.0],
        }
    )
    frame, _ = build_macro_frame(gold, events, dxy)
    before = frame.loc[pd.Timestamp("2025-02-08", tz="UTC")]
    after = frame.loc[pd.Timestamp("2025-02-10", tz="UTC")]
    assert before["macro_TEST_level"] == 1.0
    assert after["macro_TEST_level"] == 9.0
    assert pd.Timestamp(after["macro_TEST_released_at"]) <= after["_signal_at"]


def test_macro_label_entry_is_strictly_after_signal() -> None:
    gold = _daily_bars()
    dxy = _daily_bars().assign(close=np.linspace(100, 101, 80))
    events = pd.DataFrame(
        {
            "event_code": ["TEST", "TEST"],
            "released_at": ["2025-01-02T00:00:00Z", "2025-01-03T00:00:00Z"],
            "actual_value": [1.0, 2.0],
        }
    )
    frame, _ = build_macro_frame(gold, events, dxy)
    expected = np.log(float(gold.iloc[2]["close"]) / float(gold.iloc[2]["open"]))
    assert frame.iloc[0]["_future_ret"] == expected
