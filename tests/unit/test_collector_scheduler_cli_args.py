"""Scheduler CLI 参数解析单元测试（纯函数；零数据库、零网络）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scripts.run_collector_scheduler import build_parser, resolve_stale_after
from src.scheduler.slots import DEFAULT_INTERVAL_MINUTES

pytestmark = pytest.mark.unit


def test_default_arguments() -> None:
    args = build_parser().parse_args([])
    assert args.once is False
    assert args.interval_minutes == DEFAULT_INTERVAL_MINUTES == 30
    assert args.stale_after_minutes is None


def test_once_and_interval_flags() -> None:
    args = build_parser().parse_args(["--once", "--interval-minutes", "15"])
    assert args.once is True
    assert args.interval_minutes == 15


def test_resolve_stale_after_default_and_override() -> None:
    assert resolve_stale_after(30, None) == timedelta(minutes=90)
    assert resolve_stale_after(15, 20) == timedelta(minutes=20)
