"""Scheduler CLI 参数解析单元测试（纯函数；零数据库、零网络）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scripts.run_collector_scheduler import (
    build_parser,
    build_post_processor,
    resolve_stale_after,
)
from src.scheduler.slots import DEFAULT_INTERVAL_MINUTES

pytestmark = pytest.mark.unit


def test_default_arguments() -> None:
    args = build_parser().parse_args([])
    assert args.once is False
    assert args.interval_minutes == DEFAULT_INTERVAL_MINUTES == 30
    assert args.stale_after_minutes is None
    # 默认**不**接线 Processor：行为与 GOLD-001-R2 一致（GOLD-003 acceptance）
    assert args.with_processor is False


def test_with_processor_flag_is_explicit_opt_in() -> None:
    assert build_parser().parse_args(["--with-processor"]).with_processor is True
    assert build_parser().parse_args(["--once", "--with-processor"]).with_processor is True


def test_build_post_processor_is_disabled_by_default() -> None:
    """``--with-processor`` 关闭时必须是 ``None``（零行为差异）。"""
    assert build_post_processor(False) is None


def test_build_post_processor_returns_collection_processor_when_enabled() -> None:
    processor = build_post_processor(True)

    assert processor is not None
    assert processor.processor_name == "collection_normalizer"
    assert processor.processor_version == "collection-normalizer-v1"


def test_once_and_interval_flags() -> None:
    args = build_parser().parse_args(["--once", "--interval-minutes", "15"])
    assert args.once is True
    assert args.interval_minutes == 15


def test_resolve_stale_after_default_and_override() -> None:
    assert resolve_stale_after(30, None) == timedelta(minutes=90)
    assert resolve_stale_after(15, 20) == timedelta(minutes=20)
