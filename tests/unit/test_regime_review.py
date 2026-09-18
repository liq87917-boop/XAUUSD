from __future__ import annotations

import csv
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from database.models import Regime
from scripts.build_regimes import REVIEW_LABEL_ORDER, _sample_points, _write_review_files
from scripts.evaluate_regime_review import evaluate
from src.alpha.regime import RegimePoint

pytestmark = pytest.mark.unit


def _point(index: int, label: Regime) -> RegimePoint:
    moment = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return RegimePoint(
        bar_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        start_at=moment,
        end_at=moment + timedelta(hours=1),
        as_of=moment + timedelta(hours=1),
        max_effective_at=moment + timedelta(hours=1),
        input_start_at=moment - timedelta(hours=60),
        input_count=61,
        input_hash="a" * 64,
        values={
            "close": 2600.0,
            "ema_20": 2590.0,
            "ema_60": 2580.0,
            "ema_20_slope_4": 0.002,
            "ema_60_slope_4": 0.001,
            "adx_14": 27.0,
            "atr_14_pct": 0.003,
            "atr_pct_rank_60": 0.7,
            "news_count_4h": 0,
            "macro_count_4h": 0,
        },
        raw_regime=label,
        statistical_regime=label,
        regime=Regime.RANGE if label is Regime.TREND_UP else label,
        confidence=0.8,
    )


def test_v2_review_is_stratified_and_contains_required_features(tmp_path) -> None:
    points = tuple(
        _point(index * len(REVIEW_LABEL_ORDER) + offset, label)
        for index in range(10)
        for offset, label in enumerate(REVIEW_LABEL_ORDER)
    )
    selected = _sample_points(points)
    assert len(selected) == 50
    assert {point.raw_regime for point in selected} == set(REVIEW_LABEL_ORDER)

    review = tmp_path / "review.csv"
    key = tmp_path / "key.csv"
    _write_review_files(points, review_path=review, key_path=key)
    with review.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with key.open(encoding="utf-8-sig", newline="") as handle:
        answers = list(csv.DictReader(handle))
    assert len(rows) == len(answers) == 50
    assert "ema_20_slope_4_pct" in rows[0]
    assert "ema_60_slope_4_pct" in rows[0]
    assert "start_at_beijing" in rows[0]
    assert "review_target_label" not in rows[0]
    assert {row["review_target_label"] for row in answers} == {
        label.value for label in REVIEW_LABEL_ORDER
    }


def test_evaluator_scores_target_labels_and_rejects_incomplete(tmp_path) -> None:
    points = tuple(
        _point(index * len(REVIEW_LABEL_ORDER) + offset, label)
        for index in range(10)
        for offset, label in enumerate(REVIEW_LABEL_ORDER)
    )
    review = tmp_path / "review.csv"
    key = tmp_path / "key.csv"
    _write_review_files(points, review_path=review, key_path=key)
    with review.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        columns = list(rows[0])
    with key.open(encoding="utf-8-sig", newline="") as handle:
        answers = {row["review_id"]: row for row in csv.DictReader(handle)}
    for row in rows:
        row["human_label"] = answers[row["review_id"]]["review_target_label"]
        row["human_note"] = "blind review"
    with review.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    result = evaluate(review, key)
    assert result["passed"] is True
    assert result["matched"] == 50

    rows[0]["human_label"] = ""
    with review.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    invalid = evaluate(review, key)
    assert invalid["passed"] is False
    assert invalid["errors"]
