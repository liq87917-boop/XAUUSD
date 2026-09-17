"""Phase 3.0 W0-1 的文档状态守卫。

这些断言防止后续模块把期货代理、小时缺口或探索性 IC 探针误写成现货 Alpha 验收结论。
不访问网络或数据库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase3_roadmap_keeps_w0_1_restrictions_visible() -> None:
    """W0-1 只能作为受限底座，不能绕过 W0-2 直接宣称 Alpha 验证。"""
    roadmap = (REPO_ROOT / "docs" / "05_分阶段开发路线图.md").read_text(encoding="utf-8")

    assert "W0-1 行情数据底座" in roadmap
    assert "不得" in roadmap and "Alpha 已验证" in roadmap
    assert "W0-2 的宏观发布时间语义治理" in roadmap
    assert "XAUUSD → GC=F" in roadmap
    assert "禁止插值或缩短窗口" in roadmap


def test_phase3_plan_requires_proxy_and_gap_provenance() -> None:
    """后续标签/特征必须留存代理标的身份并丢弃不完整 horizon。"""
    plan = (REPO_ROOT / "docs" / "14_Phase3_执行规划.md").read_text(encoding="utf-8")

    assert "instrument_proxy=COMEX_CONTINUOUS_FUTURES" in plan
    assert "horizon 内数据缺失 → **丢弃并计数**，不插值、不缩短窗口" in plan
    assert "W0-2 已按 Initial Release Only 口径验收通过" in plan
    assert "W0-3 新闻回填已受限验收通过" in plan
    assert "W0-4 作者观点链也已受限验收通过" in plan
