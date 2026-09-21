"""News Alpha 纯函数单元测试（无数据库）：时间因果标签 + 时间切分 + OOS 门禁。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.alpha.news_alpha import (
    HORIZON_BARS,
    MIN_ROWS_FOR_SPLIT,
    NewsObservation,
    add_forward_return_labels,
    evaluate_news_gate,
    time_split,
)


def _bars(n: int = 600) -> pd.DataFrame:
    """构造连续 1h bar 的行情（index=open_time，列 open/close/close_time）。"""
    start = pd.Timestamp("2026-01-05", tz="UTC")
    index = pd.date_range(start, periods=n, freq="h")
    close = np.linspace(1900.0, 2100.0, n)
    open_ = close - 1.0
    return pd.DataFrame(
        {"open": open_, "close": close, "close_time": index + pd.Timedelta(hours=1)},
        index=index,
    )


def _obs(
    event_id: str,
    effective_at: datetime,
    *,
    sentiment: float = 0.5,
    importance: float = 0.5,
) -> NewsObservation:
    return NewsObservation(
        event_id=event_id,
        published_at=effective_at - timedelta(minutes=5),
        effective_at=effective_at,
        sentiment=sentiment,
        importance=importance,
    )


def test_add_forward_return_labels_excludes_equal_time_bar() -> None:
    """时间因果：入场 bar 的 open_time 严格晚于 effective_at（等时 bar 被排除）。"""
    bars = _bars(100)
    base = datetime(2026, 1, 5, tzinfo=UTC)
    # effective_at 恰好等于第 5 根 bar 的 open_time → 入场 bar 必须是第 6 根
    obs = _obs("e1", base + timedelta(hours=5))
    frame = add_forward_return_labels([obs], bars, horizon=24)
    assert len(frame) == 1
    expected = np.log(
        float(bars.iloc[29]["close"]) / float(bars.iloc[6]["open"])
    )  # entry=bar6, exit=bar6+23=bar29
    assert frame.iloc[0]["forward_return"] == pytest.approx(expected)


def test_add_forward_return_labels_uses_first_bar_after_effective_at() -> None:
    bars = _bars(100)
    base = datetime(2026, 1, 5, tzinfo=UTC)
    # effective_at = bar5.open_time - 30min → 入场 bar 是 bar5
    obs = _obs("e1", base + timedelta(hours=5, minutes=-30))
    frame = add_forward_return_labels([obs], bars, horizon=24)
    expected = np.log(float(bars.iloc[28]["close"]) / float(bars.iloc[5]["open"]))
    assert frame.iloc[0]["forward_return"] == pytest.approx(expected)


def test_add_forward_return_labels_rejects_naive_effective_at() -> None:
    bars = _bars(100)
    naive = datetime(2026, 1, 5, 5, 0)  # 无时区
    obs = NewsObservation(
        event_id="e1", published_at=naive, effective_at=naive, sentiment=0.5, importance=0.5
    )
    with pytest.raises(ValueError, match="时区"):
        add_forward_return_labels([obs], bars)


def test_add_forward_return_labels_rejects_missing_bars_columns() -> None:
    bars = pd.DataFrame({"open": [1.0, 2.0], "close": [2.0, 3.0]})
    obs = _obs("e1", datetime(2026, 1, 5, tzinfo=UTC))
    with pytest.raises(ValueError, match="close_time"):
        add_forward_return_labels([obs], bars)


def test_add_forward_return_labels_drops_misaligned_observation() -> None:
    bars = _bars(100)
    base = datetime(2026, 1, 5, tzinfo=UTC)
    # effective_at 太晚 → exit 越界，被丢弃
    obs = _obs("e1", base + timedelta(hours=99))
    frame = add_forward_return_labels([obs], bars, horizon=24)
    assert frame.empty


def test_time_split_splits_60_20_20_with_embargo() -> None:
    n = 1000
    frame = pd.DataFrame(
        {"sentiment": np.arange(n, dtype=float), "importance": np.arange(n, dtype=float)}
    )
    split = time_split(frame)
    assert len(split.train) == 600
    assert len(split.validation) == 200 - HORIZON_BARS
    assert len(split.test) == 200 - HORIZON_BARS


def test_time_split_rejects_insufficient_samples() -> None:
    frame = pd.DataFrame({"sentiment": np.arange(10, dtype=float)})
    with pytest.raises(ValueError, match="样本不足"):
        time_split(frame)


def test_evaluate_news_gate_rejects_insufficient_samples() -> None:
    bars = _bars(100)
    base = datetime(2026, 1, 5, tzinfo=UTC)
    obs_list = [
        _obs(f"e{i}", base + timedelta(hours=i)) for i in range(50)
    ]
    frame = add_forward_return_labels(obs_list, bars, horizon=24)
    with pytest.raises(ValueError, match="样本不足"):
        evaluate_news_gate(frame)


def test_evaluate_news_gate_runs_end_to_end() -> None:
    # 随机游走 close，保证 forward_return 有正有负（否则 LR 只有单一类别）
    n = 600
    rng = np.random.default_rng(42)
    start = pd.Timestamp("2026-01-05", tz="UTC")
    index = pd.date_range(start, periods=n, freq="h")
    close = 2000.0 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    open_ = close * np.exp(rng.normal(0, 0.0005, n))
    bars = pd.DataFrame(
        {"open": open_, "close": close, "close_time": index + pd.Timedelta(hours=1)},
        index=index,
    )
    base = datetime(2026, 1, 5, tzinfo=UTC)
    obs_list = [
        _obs(
            f"e{i}",
            base + timedelta(hours=i),
            sentiment=float(rng.uniform(-1, 1)),
            importance=float(rng.uniform(0, 1)),
        )
        for i in range(300)
    ]
    frame = add_forward_return_labels(obs_list, bars, horizon=24)
    assert len(frame) >= MIN_ROWS_FOR_SPLIT
    result = evaluate_news_gate(frame)
    assert result.rows_usable >= MIN_ROWS_FOR_SPLIT
    assert len(result.metrics) == 4
    assert result.metrics[0].name == "model_lr_platt"
    assert isinstance(result.passed, bool)
    assert result.feature_set_version == "news-2-v1"
