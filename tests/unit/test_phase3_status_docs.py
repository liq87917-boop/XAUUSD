from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_exposes_current_phase_and_negative_results() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "# GOLD-AI · Phase 3 Alpha Lab" in readme
    assert "Phase 3.3 数据资格阻塞" in readme
    assert "Phase 3.2 Technical / Macro 独立 OOS 基线均未达到" in readme
    assert "当前仅 Mock 实现" not in readme
    assert "Phase 3 及以后的表" not in readme


def test_roadmap_does_not_claim_alpha_passed() -> None:
    roadmap = (ROOT / "docs" / "05_分阶段开发路线图.md").read_text(encoding="utf-8")
    assert "Phase 3.2 Technical / Macro 均完成独立 OOS 门禁" in roadmap
    assert "结论为负面" in roadmap
    assert "Phase 3.3 Author / News" in roadmap
    assert "当前不得进入融合、策略或回测" in roadmap
