"""W0-2 宏观回填 CLI 单元测试（零网络）。"""

# ruff: noqa: I001 -- ruff 0.16.7 在该测试模块反复建议无变化的导入排序。

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.backfill_macro_vintages import build_chunks, main, parse_date, parse_series
from src.collectors.macro import DEFAULT_W0_2_SERIES, _normalize_series


pytestmark = pytest.mark.unit


def test_build_chunks_are_contiguous_and_bounded() -> None:
    chunks = build_chunks(date(2024, 1, 1), date(2025, 1, 5), chunk_days=365)

    assert chunks == (
        (date(2024, 1, 1), date(2024, 12, 30)),
        (date(2024, 12, 31), date(2025, 1, 5)),
    )


def test_build_chunks_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        build_chunks(date(2025, 1, 2), date(2025, 1, 1), chunk_days=365)
    with pytest.raises(ValueError):
        build_chunks(date(2025, 1, 1), date(2025, 1, 2), chunk_days=366)
    with pytest.raises(ValueError):
        parse_date("bad", name="--start")


def test_default_series_are_the_approved_eight() -> None:
    specs = parse_series("", _normalize_series(DEFAULT_W0_2_SERIES))

    assert [spec.series_id for spec in specs] == [
        "CPIAUCSL",
        "PCEPI",
        "PAYEMS",
        "DFF",
        "DFII10",
        "DGS10",
        "DTWEXBGS",
        "FEDFUNDS",
    ]


def test_cli_default_dry_run_never_creates_output(
    tmp_path: Path, sqlite_url: str, session, make_source, capsys
) -> None:
    make_source(
        name="fred_macro",
        source_type="MACRO",
        base_url="https://api.stlouisfed.org",
        config_json={"provider": "fred", "series": ["DFF"]},
    )
    session.commit()
    output = tmp_path / "meta.json"

    code = main(
        [
            "--series",
            "DFF",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--db-url",
            sqlite_url,
            "--out",
            str(output),
        ]
    )

    assert code == 0
    assert not output.exists()
    assert "未联网、未写库" in capsys.readouterr().out


def test_cli_rejects_conflicting_flags() -> None:
    assert main(["--dry-run", "--no-dry-run"]) == 2