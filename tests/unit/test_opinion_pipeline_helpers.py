"""Post → Opinion 管道的纯函数部分（不依赖数据库）。

覆盖：标的解析（白名单缺失不得自动造标的）、draft 标的去重、报告计数与摘要。
"""

from __future__ import annotations

import uuid

import pytest

from database.models.enums import OpinionStance
from src.processors.opinion_extractor import DiagnosticCode
from src.processors.opinion_pipeline import (
    PipelineReport,
    iter_draft_instruments,
    resolve_instrument_id,
)
from src.processors.schemas import AuthorOpinionDraft

pytestmark = pytest.mark.unit

XAUUSD_ID = uuid.uuid4()


def _draft(instrument: str | None, *, stance: str = "LONG") -> AuthorOpinionDraft:
    return AuthorOpinionDraft(  # type: ignore[arg-type]
        stance=stance, instrument=instrument, parser_version="unit-test-v1"
    )


# ---------------------------------------------------------------------------
# 1) 标的解析
# ---------------------------------------------------------------------------
def test_resolve_instrument_id_returns_none_for_missing_symbol() -> None:
    """观点未指向标的是合法情形（docs/10 §4.7），不得报错也不得默认 XAUUSD。"""
    assert resolve_instrument_id(None, {"XAUUSD": XAUUSD_ID}) is None


def test_resolve_instrument_id_is_case_insensitive() -> None:
    assert resolve_instrument_id("xauusd", {"XAUUSD": XAUUSD_ID}) == XAUUSD_ID
    assert resolve_instrument_id(" XAUUSD ", {"XAUUSD": XAUUSD_ID}) == XAUUSD_ID


def test_resolve_instrument_id_returns_none_for_unknown_symbol() -> None:
    """白名单外标的 → None（调用方必须计数并记录诊断，禁止自动造标的）。"""
    assert resolve_instrument_id("XPTUSD", {"XAUUSD": XAUUSD_ID}) is None
    assert resolve_instrument_id("", {"XAUUSD": XAUUSD_ID}) is None


# ---------------------------------------------------------------------------
# 2) drafts 标的去重
# ---------------------------------------------------------------------------
def test_iter_draft_instruments_deduplicates_keeping_order() -> None:
    drafts = [
        _draft("XAUUSD"),
        _draft("XAGUSD", stance="SHORT"),
        _draft("XAUUSD"),
        _draft(None, stance="UNKNOWN"),
    ]

    assert iter_draft_instruments(drafts) == ["XAUUSD", "XAGUSD"]


def test_iter_draft_instruments_handles_empty_input() -> None:
    assert iter_draft_instruments([]) == []


# ---------------------------------------------------------------------------
# 3) 运行报告
# ---------------------------------------------------------------------------
def test_report_counts_diagnostics_by_code() -> None:
    report = PipelineReport()

    report.count_diagnostic(DiagnosticCode.CONDITIONAL)
    report.count_diagnostic(DiagnosticCode.CONDITIONAL)
    report.count_diagnostic(DiagnosticCode.NO_STANCE_KEYWORD)

    assert report.diagnostics == {"conditional": 2, "no_stance_keyword": 1}


def test_report_summary_contains_key_metrics() -> None:
    report = PipelineReport()
    report.posts_scanned = 5
    report.posts_processed = 4
    report.opinions_created = 6
    report.posts_failed = 1

    summary = report.summary()

    assert "scanned=5" in summary
    assert "processed=4" in summary
    assert "opinions=6" in summary
    assert "failed=1" in summary


def test_report_defaults_are_zero() -> None:
    report = PipelineReport()

    assert report.posts_scanned == 0
    assert report.opinions_created == 0
    assert report.diagnostics == {}


def test_draft_stance_enum_is_used_by_report_helpers() -> None:
    """护栏：draft 的 stance 必须是枚举（防止有人把裸字符串塞进管道）。"""
    assert _draft("XAUUSD").stance is OpinionStance.LONG