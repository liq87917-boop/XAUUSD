from datetime import UTC, datetime, timedelta

from scripts.check_phase3_3_readiness import render
from src.alpha.evidence_gate import AuthorReadiness, Phase33Readiness, assess_news_counts


def test_quantitative_pass_does_not_claim_alpha_clearance_without_license() -> None:
    first = datetime(2025, 1, 1, tzinfo=UTC)
    news = assess_news_counts(
        {"source_a": 100, "source_b": 100, "source_c": 100},
        first,
        first + timedelta(days=120),
    )
    assert news.ready
    result = Phase33Readiness(
        authors=(AuthorReadiness("author-1", "测试作者", 30, 30, True),),
        label_status_counts=(("LABELED", 30),),
        news=news,
        hf_weak_supervision_rows=0,
        author_ready=True,
        news_ready=True,
    )
    report = render(result)
    assert "Author Alpha：BLOCKED（本报告未核验来源授权）" in report
    assert "News Alpha：BLOCKED（本报告未核验来源授权及历史可用证据）" in report
    assert "Author 库内门槛：PASS；News 库内门槛：PASS" in report
    assert "- Author Alpha：PASS" not in report
    assert "- News Alpha：PASS" not in report
