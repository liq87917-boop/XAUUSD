from src.alpha.boundaries import EXPECTED_REVISION, assess_phase3_boundaries


def test_current_checkpoint_passes_without_downstream_facts() -> None:
    result = assess_phase3_boundaries(
        revision=EXPECTED_REVISION,
        table_names={"market_bars", "feature_snapshots", "market_regimes"},
        author_skill_rows=0,
        author_weight_rows=0,
        live_trading=False,
        external_order_submission=False,
    )
    assert result.passed


def test_rejects_any_bypassed_gate() -> None:
    assert not assess_phase3_boundaries(
        revision=EXPECTED_REVISION,
        table_names={"alpha_signals"},
        author_skill_rows=0,
        author_weight_rows=0,
        live_trading=False,
        external_order_submission=False,
    ).passed
    assert not assess_phase3_boundaries(
        revision=EXPECTED_REVISION,
        table_names=set(),
        author_skill_rows=1,
        author_weight_rows=0,
        live_trading=False,
        external_order_submission=False,
    ).passed
    assert not assess_phase3_boundaries(
        revision=EXPECTED_REVISION,
        table_names=set(),
        author_skill_rows=0,
        author_weight_rows=0,
        live_trading=True,
        external_order_submission=False,
    ).passed
