"""组装层单元测试（无数据库）：label_to_sample / compute_all_author_skills / metadata。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.alpha.author_alpha import AuthorSkillResult, SkillSample
from src.alpha.author_skill_assembly import (
    AuthorSample,
    build_skill_metadata,
    compute_all_author_skills,
    label_to_sample,
)
from src.processors.opinion_labels import OpinionLabel


def _label(
    opinion_id: str = "o1",
    *,
    status: str = "LABELED",
    stance_direction: int | None = 1,
    direction_hit: bool | None = True,
    log_return: float | None = 0.001,
    effective_at: str = "2025-01-01T00:00:00+00:00",
    horizon: str | None = "H1",
) -> OpinionLabel:
    return OpinionLabel(
        opinion_id=opinion_id,
        effective_at=effective_at,
        stance="LONG",
        horizon=horizon,
        horizon_source="declared",
        status=status,
        stance_direction=stance_direction,
        direction_hit=direction_hit,
        log_return=log_return,
    )


def test_label_to_sample_maps_labeled() -> None:
    sample = label_to_sample(
        _label(log_return=0.01, stance_direction=-1, direction_hit=True),
        author_id="a1",
        confidence=0.7,
        information_type="TECHNICAL",
    )
    assert sample is not None
    assert sample.opinion_id == "o1"
    assert sample.stance_direction == -1
    assert sample.direction_hit is True
    assert sample.log_return == pytest.approx(0.01)
    assert sample.confidence == pytest.approx(0.7)
    assert sample.information_type == "TECHNICAL"
    assert sample.horizon == "H1"
    assert sample.effective_at == datetime(2025, 1, 1, tzinfo=UTC)


def test_label_to_sample_skips_non_labeled() -> None:
    label = _label(
        status="NO_ENTRY_BAR", stance_direction=None, direction_hit=None, log_return=None
    )
    assert label_to_sample(label, author_id="a1", confidence=None, information_type=None) is None


def test_label_to_sample_skips_labeled_without_direction() -> None:
    label = _label(stance_direction=None, direction_hit=None, log_return=None)
    assert label_to_sample(label, author_id="a1", confidence=None, information_type=None) is None


def test_compute_all_author_skills_groups_and_filters_by_as_of() -> None:
    as_of = datetime(2025, 2, 1, tzinfo=UTC)

    def _sample(opinion_id: str, effective_at: datetime, hit: bool) -> SkillSample:
        return SkillSample(
            opinion_id=opinion_id,
            effective_at=effective_at,
            stance_direction=1,
            direction_hit=hit,
            log_return=0.001,
        )

    items = [
        AuthorSample("a1", _sample("o1", as_of - timedelta(days=1), True)),
        AuthorSample("a1", _sample("o2", as_of - timedelta(days=2), False)),
        # a1 的未来样本必须被排除（防泄漏）
        AuthorSample("a1", _sample("o3", as_of + timedelta(days=1), True)),
        AuthorSample("a2", _sample("o4", as_of - timedelta(days=1), True)),
    ]
    results = compute_all_author_skills(items, as_of=as_of)
    assert [r.author_id for r in results] == ["a1", "a2"]
    by_id = {r.author_id: r for r in results}
    assert by_id["a1"].sample_size == 2
    assert by_id["a2"].sample_size == 1


def test_build_skill_metadata_contains_raw_columns() -> None:
    result = AuthorSkillResult(
        author_id="a1",
        as_of=datetime(2025, 2, 1, tzinfo=UTC),
        sample_size=10,
        direction_hits=7,
        direction_raw=0.7,
        direction_skill=0.666666,
        timing_raw=0.001,
        timing_skill=0.6,
        calibration_score=None,
        entry_skill=None,
        exit_skill=None,
        independence_score=None,
        ready=False,
    )
    meta = build_skill_metadata(result)
    assert meta["direction_skill_raw"] == pytest.approx(0.7)
    assert meta["direction_hits"] == 7
    assert meta["timing_skill_raw"] == pytest.approx(0.001)
    assert meta["ready"] is False
