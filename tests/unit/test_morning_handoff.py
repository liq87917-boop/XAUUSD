from scripts.build_morning_handoff import HandoffSnapshot, render


def test_handoff_contains_results_boundaries_and_user_actions() -> None:
    snapshot = HandoffSnapshot(
        commits=(("abc1234", "test commit"),),
        database_counts=(("market_regimes", 11872), ("author_weight_snapshots", 0)),
        revision="0007_phase3_feature_tables",
        working_tree_clean_before_report=True,
    )
    report = render(snapshot)
    assert "Technical：FAIL" in report
    assert "Macro：FAIL" in report
    assert "Author / News：BLOCKED" in report
    assert "3234 passed / 1 skipped" in report
    assert "author_posts_template.csv" in report
    assert "每个 source + account 账号至少 30 条" in report
    assert "唯一可解析 URL" in report
    assert "暂时不要自行填写普通历史新闻 CSV" in report
    assert "不能在当前 test 上继续调参" in report
    assert "`abc1234` test commit" in report
    assert "生成本报告前工作区：干净" in report
