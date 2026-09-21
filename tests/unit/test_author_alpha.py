from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.alpha.author_alpha import (
    MIN_AUTHOR_SAMPLES,
    SkillSample,
    beta_posterior_mean,
    compute_author_skill,
    compute_calibration_score,
    compute_direction_skill,
    compute_timing_skill,
    filter_by_time,
)


def _sample(
    opinion_id: str = "o1",
    *,
    effective_at: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    stance_direction: int = 1,
    direction_hit: bool = True,
    log_return: float = 0.001,
    confidence: float | None = 0.7,
) -> SkillSample:
    return SkillSample(
        opinion_id=opinion_id,
        effective_at=effective_at,
        stance_direction=stance_direction,
        direction_hit=direction_hit,
        log_return=log_return,
        confidence=confidence,
    )


def test_filter_by_time_excludes_future() -> None:
    as_of = datetime(2025, 1, 2, tzinfo=UTC)
    past = _sample("past", effective_at=as_of - timedelta(days=1))
    at_boundary = _sample("at", effective_at=as_of)
    future = _sample("future", effective_at=as_of + timedelta(days=1))
    result = filter_by_time([past, at_boundary, future], as_of)
    assert [item.opinion_id for item in result] == ["past", "at"]


def test_beta_posterior_mean_shrinks_to_prior_without_samples() -> None:
    assert beta_posterior_mean(0, 0, 1.0, 1.0) == pytest.approx(0.5)


def test_beta_posterior_mean_approaches_raw_with_more_samples() -> None:
    assert beta_posterior_mean(1, 1, 1.0, 1.0) == pytest.approx(2 / 3)
    assert beta_posterior_mean(10, 10, 1.0, 1.0) == pytest.approx(11 / 12)


def test_beta_posterior_mean_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError):
        beta_posterior_mean(-1, 1, 1.0, 1.0)
    with pytest.raises(ValueError):
        beta_posterior_mean(2, 1, 1.0, 1.0)


def test_direction_skill_shrinks_small_sample() -> None:
    samples = [_sample()]
    hits, trials, raw, shrunken = compute_direction_skill(samples)
    assert (hits, trials) == (1, 1)
    assert raw == pytest.approx(1.0)
    assert shrunken == pytest.approx(2 / 3)


def test_direction_skill_excludes_flat_and_unknown_stance() -> None:
    samples = [
        _sample("long", stance_direction=1, direction_hit=True),
        _sample("short", stance_direction=-1, direction_hit=False),
        _sample("flat", stance_direction=0, direction_hit=True),
    ]
    hits, trials, raw, shrunken = compute_direction_skill(samples)
    assert trials == 2
    assert hits == 1
    assert raw == pytest.approx(0.5)


def test_direction_skill_returns_none_without_directional_samples() -> None:
    samples = [_sample("flat", stance_direction=0, direction_hit=True)]
    hits, trials, raw, shrunken = compute_direction_skill(samples)
    assert (hits, trials, raw, shrunken) == (0, 0, None, None)


def test_timing_skill_all_positive_returns_is_one() -> None:
    samples = [
        _sample("long", direction_hit=True, log_return=0.001),
        _sample("short", stance_direction=-1, direction_hit=True, log_return=-0.002),
    ]
    raw_mean, skill = compute_timing_skill(samples)
    # 两条均为"看对且赚"：有向收益 stance_direction × log_return 均 > 0
    assert skill == pytest.approx(1.0)
    # 有向收益均值：(1 × 0.001 + (-1) × (-0.002)) / 2 = 0.0015
    assert raw_mean == pytest.approx(0.0015)


def test_timing_skill_all_negative_returns_is_zero() -> None:
    samples = [
        _sample("long", direction_hit=False, log_return=-0.001),
        _sample("short", stance_direction=-1, direction_hit=False, log_return=0.002),
    ]
    raw_mean, skill = compute_timing_skill(samples)
    # 两条均为"看错且亏"：有向收益均 < 0
    assert skill == pytest.approx(0.0)
    assert raw_mean == pytest.approx(-0.0015)


def test_timing_skill_mixed_returns_proportion() -> None:
    samples = [
        _sample("win1", direction_hit=True, log_return=0.001),
        _sample("lose1", direction_hit=False, log_return=-0.001),
        _sample("win2", direction_hit=True, log_return=0.003),
    ]
    raw_mean, skill = compute_timing_skill(samples)
    # 3 条中 2 条有向收益 > 0
    assert skill == pytest.approx(2 / 3)
    assert raw_mean == pytest.approx((0.001 - 0.001 + 0.003) / 3)


def test_timing_skill_none_without_directional_samples() -> None:
    samples = [
        _sample("flat", stance_direction=0, direction_hit=True, log_return=0.001),
        _sample("unknown", direction_hit=None, log_return=None),
    ]
    assert compute_timing_skill(samples) == (None, None)


def test_calibration_score_perfect_calibration_is_one() -> None:
    samples = [_sample(f"o{i}", confidence=0.5, direction_hit=(i % 2 == 0)) for i in range(10)]
    assert compute_calibration_score(samples) == pytest.approx(1.0)


def test_calibration_score_none_with_insufficient_samples() -> None:
    samples = [_sample(f"o{i}", confidence=0.5, direction_hit=True) for i in range(9)]
    assert compute_calibration_score(samples) is None


def test_calibration_score_penalizes_miscalibration() -> None:
    samples = [_sample(f"o{i}", confidence=0.9, direction_hit=False) for i in range(20)]
    score = compute_calibration_score(samples)
    assert score is not None and score < 0.5


def test_compute_author_skill_blocks_below_threshold() -> None:
    as_of = datetime(2025, 2, 1, tzinfo=UTC)
    samples = [_sample(f"o{i}", effective_at=as_of - timedelta(days=1)) for i in range(5)]
    result = compute_author_skill(samples, author_id="a1", as_of=as_of)
    assert result.sample_size == 5
    assert result.ready is False


def test_compute_author_skill_ready_at_threshold() -> None:
    as_of = datetime(2025, 2, 1, tzinfo=UTC)
    samples = [
        _sample(f"o{i}", effective_at=as_of - timedelta(days=1)) for i in range(MIN_AUTHOR_SAMPLES)
    ]
    result = compute_author_skill(samples, author_id="a1", as_of=as_of)
    assert result.sample_size == MIN_AUTHOR_SAMPLES
    assert result.ready is True


def test_compute_author_skill_excludes_future_samples() -> None:
    """核心防泄漏：as_of 之后的观点不得计入历史技能。"""
    as_of = datetime(2025, 2, 1, tzinfo=UTC)
    past = [_sample(f"past{i}", effective_at=as_of - timedelta(days=1)) for i in range(3)]
    future = [_sample(f"future{i}", effective_at=as_of + timedelta(days=1)) for i in range(3)]
    result = compute_author_skill(past + future, author_id="a1", as_of=as_of)
    assert result.sample_size == 3


def test_compute_author_skill_rejects_naive_as_of() -> None:
    with pytest.raises(ValueError, match="时区"):
        compute_author_skill([_sample()], author_id="a1", as_of=datetime(2025, 1, 1))


def test_entry_exit_independence_are_none_in_v1() -> None:
    as_of = datetime(2025, 2, 1, tzinfo=UTC)
    result = compute_author_skill(
        [_sample(f"o{i}", effective_at=as_of) for i in range(MIN_AUTHOR_SAMPLES)],
        author_id="a1",
        as_of=as_of,
    )
    assert result.entry_skill is None
    assert result.exit_skill is None
    assert result.independence_score is None
