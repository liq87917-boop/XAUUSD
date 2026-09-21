"""作者技能 CLI 单元测试（无数据库）：resolve_as_of / render_report / Mock 作者标记。"""

from __future__ import annotations

from datetime import UTC, datetime

from scripts.build_author_skill import _format_score, render_report, resolve_as_of
from scripts.import_mock_posts import DATA_SOURCE_SYNTHETIC, _mock_author_seeds
from src.alpha.author_alpha import AuthorSkillResult
from src.processors.opinion_labels import OpinionLabel


class _FakeAuthor:
    def __init__(self, display_name: str, data_source: str | None) -> None:
        self.display_name = display_name
        self.metadata_json = {"data_source": data_source} if data_source else None


def _label(opinion_id: str, *, status: str = "LABELED", exit_at: str | None = None) -> OpinionLabel:
    return OpinionLabel(
        opinion_id=opinion_id,
        effective_at="2026-09-01T00:00:00+00:00",
        stance="LONG",
        horizon="H1",
        horizon_source="declared",
        status=status,
        exit_at=exit_at,
    )


def _result(author_id: str, *, sample_size: int = 30) -> AuthorSkillResult:
    return AuthorSkillResult(
        author_id=author_id,
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
        sample_size=sample_size,
        direction_hits=15,
        direction_raw=0.5,
        direction_skill=0.5,
        timing_raw=0.001,
        timing_skill=0.5,
        calibration_score=0.6,
        entry_skill=None,
        exit_skill=None,
        independence_score=None,
        ready=True,
    )


def test_mock_author_seeds_are_marked_synthetic() -> None:
    seeds = _mock_author_seeds()
    assert len(seeds) == 4
    for seed in seeds:
        assert seed.canonical_name is not None and seed.canonical_name.startswith("mock:")
        assert seed.metadata_json["data_source"] == DATA_SOURCE_SYNTHETIC
        assert seed.metadata_json["mock_batch"]
        assert seed.display_name.endswith("[MOCK]")
        assert seed.verified is False


def test_resolve_as_of_uses_max_exit_at() -> None:
    labels = [
        _label("o1", exit_at="2026-09-01T01:00:00+00:00"),
        _label("o2", exit_at="2026-09-05T04:00:00+00:00"),
        _label("o3", status="NO_ENTRY_BAR", exit_at=None),  # 非 LABELED 不参与
    ]
    assert resolve_as_of(labels) == datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


def test_resolve_as_of_falls_back_to_now_without_labeled() -> None:
    labels = [_label("o1", status="UNTRUSTED_COLLECTION_TIME", exit_at=None)]
    as_of = resolve_as_of(labels)
    assert as_of.tzinfo is not None
    assert abs((as_of - datetime.now(UTC)).total_seconds()) < 60


def test_format_score_renders_not_evaluated_and_value() -> None:
    assert _format_score(None) == "NOT_EVALUATED"
    assert _format_score(0.5) == "0.5000"
    assert _format_score(0.002817, 6) == "0.002817"


def test_render_report_layers_mock_and_real() -> None:
    results = [_result("a1", sample_size=97)]
    authors = {
        "a1": _FakeAuthor("金十数据快讯[MOCK]", DATA_SOURCE_SYNTHETIC),
        "a2": _FakeAuthor("华尔街见闻", None),
    }
    opinion_author = {"o1": "a1", "o2": "a2"}
    labels = [_label("o1", status="LABELED"), _label("o2", status="UNTRUSTED_COLLECTION_TIME")]

    report = render_report(
        results,
        authors=authors,
        opinion_author=opinion_author,
        labels=labels,
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
        written=1,
        total_snapshots=4,
        dry_run=False,
    )
    # 顶部一句话结论
    assert "本次仅验证管线正确性，不作任何真实作者技能结论" in report
    # Mock 层五列表头齐全
    assert "direction_skill_raw" in report
    assert "direction_skill" in report
    assert "timing_skill_raw" in report
    assert "timing_skill" in report
    assert "calibration_score" in report
    # Mock 作者出现在 Mock 层，真实作者出现在真实层
    assert "金十数据快讯[MOCK]" in report
    assert "华尔街见闻" in report
    # 真实层 0 可用标签
    assert "华尔街见闻 | 1 | 0 |" in report
