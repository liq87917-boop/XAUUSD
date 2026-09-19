import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.check_phase3_3_author_input import main, render, validate_report_target
from src.alpha.author_input import AuthorInputAudit
from src.alpha.source_authorization import SourceAuthorizationAudit


def _render(*, author_ready: bool, authorization_ready: bool) -> str:
    author = AuthorInputAudit(
        rows=30,
        author_counts=(("分析师 [source/account]", 30),),
        errors=(),
        warnings=(),
        ready=author_ready,
    )
    authorization = SourceAuthorizationAudit(
        rows=2,
        approved_accounts=frozenset({("source", "account")}),
        approved_windows=((("source", "account"), datetime(2026, 1, 1, tzinfo=UTC), None),),
        errors=() if authorization_ready else ("第 2 行授权已过期",),
        warnings=(),
        ready=authorization_ready,
    )
    return render(
        author,
        Path("posts.csv"),
        "posts-sha256",
        authorization,
        Path("authorizations.csv"),
        "authorizations-sha256",
    )


def test_report_blocks_when_authorization_audit_fails_even_if_posts_pass() -> None:
    report = _render(author_ready=True, authorization_ready=False)
    assert "结论：BLOCKED" in report
    assert "第 2 行授权已过期" in report
    assert "逐行授权检查通过账号：1" in report
    assert "结论：PASS" not in report


def test_report_requires_both_author_and_authorization_audits() -> None:
    assert "结论：BLOCKED" in _render(author_ready=False, authorization_ready=True)
    assert "结论：PASS" in _render(author_ready=True, authorization_ready=True)


@pytest.mark.parametrize("protected", ["posts", "authorizations"])
def test_report_cannot_overwrite_user_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: str
) -> None:
    posts = tmp_path / "posts.csv"
    authorizations = tmp_path / "authorizations.csv"
    posts.write_text("original posts", encoding="utf-8")
    authorizations.write_text("original authorizations", encoding="utf-8")
    report = posts if protected == "posts" else authorizations
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_phase3_3_author_input.py",
            "--input",
            str(posts),
            "--authorizations",
            str(authorizations),
            "--report",
            str(report),
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        main()
    assert posts.read_text(encoding="utf-8") == "original posts"
    assert authorizations.read_text(encoding="utf-8") == "original authorizations"


def test_report_target_allows_separate_path(tmp_path: Path) -> None:
    validate_report_target(
        tmp_path / "report.md",
        tmp_path / "posts.csv",
        tmp_path / "authorizations.csv",
    )
