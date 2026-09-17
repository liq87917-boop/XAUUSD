"""`scripts/probe_ic.py` 的单元测试（**全离线**：只用合成 K 线 fixture，绝不联网）。

覆盖重点：
1. 取数解析：Yahoo chart JSON → 行/跳过计数、坏结构必须报错；
2. **防泄漏（最高优先级）**：特征只用 `≤ t` 的信息（扰动未来 bar 不得改变过去特征值）；
   标签 = `log(close[t+h]/open[t+1])`（不使用未走完的 bar）；
3. **OOS 纪律**：时间序 walk-forward、`train_end = oos_start - horizon`（embargo）、禁止 shuffle；
4. 统计层：Spearman IC（常量/样本不足 → 不可评估）、Wilson CI 复用、BH-FDR、p 值；
5. `scoring=NOT_EVALUATED`（`docs/10 §7.1`）：样本不足/常量信号**不评分**、不进 BH 分母；
6. CLI：默认 `--dry-run` 不写盘、`--no-dry-run` 写四类产物、参数错误退 2、fixture 模式不联网。
"""

from __future__ import annotations

import csv
import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.evaluate_extractor import NOT_EVALUATED_MARKER
from scripts.probe_ic import (
    FEATURE_NAMES,
    LONG_TABLE_COLUMNS,
    PROBE_CONFIGS,
    FetchResult,
    ProbeConfig,
    _assemble_stats,
    add_labels,
    bh_fdr,
    build_repro_command,
    compute_features,
    evaluate_config,
    frame_from_rows,
    load_bars_from_csv,
    main,
    parse_yahoo_chart,
    render_report,
    select_configs,
    spearman_ic,
    two_sided_p_from_t,
    walk_forward_windows,
    write_outputs,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 合成数据（确定性，绝不触网）
# ---------------------------------------------------------------------------
def _bars(
    rows: int = 900, *, seed: int = 7, drift: float = 0.0002, start: str = "2024-01-01"
) -> pd.DataFrame:
    """几何随机游走 OHLCV（小时频）；确定性且无未来信息。"""
    rng = np.random.default_rng(seed)
    close = 1800.0 * np.exp(np.cumsum(rng.normal(drift, 0.003, rows)))
    open_ = np.concatenate([[1800.0], close[:-1]])
    frame = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "volume": 100.0,
        },
        index=pd.date_range(start, periods=rows, freq="h", tz="UTC"),
    )
    frame.index.name = "open_time"
    return frame


def _write_bars_csv(path: Path, frame: pd.DataFrame) -> Path:
    frame.reset_index().to_csv(path, index=False, encoding="utf-8")
    return path


def _tiny_config(**overrides: object) -> ProbeConfig:
    base: dict[str, object] = {
        "name": "test_1h_h1",
        "interval": "1h",
        "lookback": "2y",
        "horizon_bars": 1,
        "window_bars": 100,
        "description": "测试配置",
    }
    base.update(overrides)
    return ProbeConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1) 取数解析
# ---------------------------------------------------------------------------
def _chart_payload(timestamps: list[int], closes: list[float | None]) -> dict[str, object]:
    opens = [None if value is None else value - 1.0 for value in closes]
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": [None if v is None else v + 1 for v in closes],
                                "low": [None if v is None else v - 1 for v in closes],
                                "close": closes,
                                "volume": [10 for _ in closes],
                            }
                        ]
                    },
                }
            ],
        }
    }


def test_parse_yahoo_chart_counts_valid_and_skipped_rows() -> None:
    """空 bar（provider 返回 null）与坏时间戳都要计入 skipped，**不得静默**。"""
    payload = _chart_payload(
        [1700000000, 1700003600, 1700007200, "bad"], [1800.0, None, 1805.0, None]
    )

    rows, skipped = parse_yahoo_chart(payload)

    assert [row["close"] for row in rows] == [1800.0, 1805.0]
    assert skipped == 2
    assert str(rows[0]["open_time"].tzinfo) == "UTC"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"chart": {"error": {"code": "Not Found"}, "result": None}}, "行情接口返回错误"),
        ({"nope": 1}, "缺少 chart 字段"),
        ({"chart": {"error": None, "result": []}}, "缺少 result"),
        ("not a mapping", "不是 JSON 对象"),
    ],
)
def test_parse_yahoo_chart_rejects_broken_payload(payload: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_yahoo_chart(payload)  # type: ignore[arg-type]


def test_frame_from_rows_is_sorted_and_typed() -> None:
    rows = [
        {
            "open_time": pd.Timestamp("2024-01-02", tz="UTC"),
            "open": 2,
            "high": 3,
            "low": 1,
            "close": 2.5,
        },
        {
            "open_time": pd.Timestamp("2024-01-01", tz="UTC"),
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
        },
    ]

    frame = frame_from_rows(rows)

    assert list(frame.index) == sorted(frame.index)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].dtype == float


def test_fetch_yahoo_bars_sends_user_agent_and_falls_through_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取数必须带 UA（无 UA → 429）；候选链中途失败要自动落到下一个 ticker。"""
    from scripts.probe_ic import fetch_yahoo_bars

    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, status: int, payload: dict[str, object] | None = None) -> None:
            self.status_code = status
            self._payload = payload or {}

        def json(self) -> dict[str, object]:
            return self._payload

    def _fake_get(
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        calls.append({"url": url, "headers": dict(headers or {})})
        if "GC=F" in url:
            return _Response(404)  # 模拟"首个候选不可用"
        return _Response(200, _chart_payload([1700000000, 1700003600], [1800.0, 1805.0]))

    monkeypatch.setattr("httpx.get", _fake_get)

    fetch = fetch_yahoo_bars("XAUUSD", interval="1d", lookback="1y")

    assert fetch.provider_symbol == "XAUUSD=X"
    assert fetch.rows == 2
    assert len(calls) == 2  # 第一个 404 → 自动落到下一个候选
    assert all(str(call["headers"].get("User-Agent", "")) for call in calls)


def test_fetch_yahoo_bars_reports_all_candidate_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部候选失败 → RuntimeError 且**逐条列出原因**（不静默返回空数据）。"""
    from scripts.probe_ic import fetch_yahoo_bars

    class _Response:
        status_code = 429

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="所有候选 ticker 均取数失败"):
        fetch_yahoo_bars("XAUUSD", interval="1d", lookback="1y")


def test_safe_print_survives_gbk_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归测试（真实缺陷）**：GBK 控制台下打印含 `✅` 的报告不得崩溃，只降级为 `?`。"""
    from scripts.probe_ic import _safe_print

    class _GbkStream:
        encoding = "gbk"

        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, text: str) -> int:
            text.encode("gbk")  # 真实 GBK 流：不可表示的字符 → UnicodeEncodeError
            self.written.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    stream = _GbkStream()
    monkeypatch.setattr("sys.stdout", stream)

    _safe_print("BH-q=0.0079 ✅ 显著 / 未评估 ∅")

    assert stream.written, "降级后的文本必须被写出"
    assert "?" in stream.written[-1]
    assert "显著" in stream.written[-1]  # 中文不受影响


def test_load_bars_from_csv_roundtrip_and_missing_column(tmp_path: Path) -> None:
    path = _write_bars_csv(tmp_path / "bars.csv", _bars(rows=30))
    frame = load_bars_from_csv(path)

    assert len(frame) == 30
    assert frame.index.tz is not None

    bad = tmp_path / "bad.csv"
    bad.write_text("open_time,open,close\n2024-01-01T00:00:00Z,1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺列"):
        load_bars_from_csv(bad)


# ---------------------------------------------------------------------------
# 2) 防泄漏：特征只用 ≤ t 的信息；标签用 t+1 之后的价格
# ---------------------------------------------------------------------------
def test_compute_features_produces_all_eight_features() -> None:
    frame = compute_features(_bars(rows=120))

    assert set(FEATURE_NAMES) <= set(frame.columns)
    assert frame[list(FEATURE_NAMES)].iloc[-1].notna().all()


def test_compute_features_ignores_future_bars() -> None:
    """**核心防泄漏测试**：把 `t` 之后的价格全部 ×10，`t` 之前的特征值必须逐位不变。"""
    base = _bars(rows=200)
    cutoff = 150
    perturbed = base.copy()
    perturbed.loc[perturbed.index[cutoff + 1 :], ["open", "high", "low", "close"]] *= 10.0

    left = compute_features(base).iloc[: cutoff + 1]
    right = compute_features(perturbed).iloc[: cutoff + 1]

    pd.testing.assert_frame_equal(left, right)


def test_compute_features_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="缺少列"):
        compute_features(pd.DataFrame({"close": [1.0, 2.0]}))


def test_add_labels_uses_next_open_and_horizon_close() -> None:
    frame = pd.DataFrame(
        {
            "open": [10.0, 20.0, 30.0, 40.0],
            "high": [11.0, 21.0, 31.0, 41.0],
            "low": [9.0, 19.0, 29.0, 39.0],
            "close": [15.0, 25.0, 35.0, 45.0],
        },
        index=pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC"),
    )

    labelled = add_labels(frame, 2)

    # t=0：entry = open[1] = 20，exit = close[2] = 35
    assert labelled["_future_ret"].iloc[0] == pytest.approx(float(np.log(35.0 / 20.0)))
    # 尾部 h 行没有标签（不存在未来 bar，不得编造）
    assert labelled["_future_ret"].iloc[-2:].isna().all()


def test_add_labels_rejects_bad_horizon() -> None:
    with pytest.raises(ValueError, match="horizon"):
        add_labels(_bars(rows=10), 0)


# ---------------------------------------------------------------------------
# 3) OOS 纪律：embargo + 连续窗口（不 shuffle）
# ---------------------------------------------------------------------------
def test_walk_forward_windows_are_ordered_and_embargoed() -> None:
    windows, skipped = walk_forward_windows(
        1000, train_ratio=0.5, window_bars=100, horizon=4, min_train=100
    )

    assert skipped == 0 and len(windows) == 5
    previous_end: int | None = None
    for train_end, start, end in windows:
        assert train_end == start - 4  # embargo = horizon（防训练标签与测试窗口重叠）
        assert start < end
        if previous_end is not None:
            assert start == previous_end  # 连续、不重叠、顺序固定
        previous_end = end
    assert windows[0][1] == 500 and windows[-1][2] == 1000


def test_walk_forward_windows_counts_skipped_windows() -> None:
    windows, skipped = walk_forward_windows(
        300, train_ratio=0.05, window_bars=100, horizon=1, min_train=120
    )

    assert len(windows) == 1 and skipped == 2  # 前两个窗口训练段不足 → 记跳过


@pytest.mark.parametrize(
    ("ratio", "window", "horizon"), [(0.0, 10, 1), (1.0, 10, 1), (0.5, 0, 1), (0.5, 10, 0)]
)
def test_walk_forward_windows_rejects_bad_params(ratio: float, window: int, horizon: int) -> None:
    with pytest.raises(ValueError):
        walk_forward_windows(100, train_ratio=ratio, window_bars=window, horizon=horizon)


# ---------------------------------------------------------------------------
# 4) 统计工具
# ---------------------------------------------------------------------------
def test_spearman_ic_known_and_degenerate_cases() -> None:
    ascending = pd.Series(np.arange(1.0, 13.0))  # 12 个样本 ≥ 窗口最少点数
    target = ascending * 2.0

    assert spearman_ic(ascending, target) == pytest.approx(1.0)
    assert spearman_ic(ascending.iloc[::-1].reset_index(drop=True), target) == pytest.approx(-1.0)
    # 常量信号 → **不可评估**（None），不是 0
    assert spearman_ic(pd.Series([1.0] * 12), target) is None
    # 样本不足（< 窗口最少点数）→ 不可评估
    assert spearman_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0])) is None


def test_bh_fdr_matches_hand_computed_values() -> None:
    assert bh_fdr([0.01, 0.02, 0.03, 0.04, 0.05]) == pytest.approx([0.05] * 5)
    assert bh_fdr([0.001, 0.5]) == pytest.approx([0.002, 0.5])
    assert bh_fdr([]) == []


def test_two_sided_p_from_t_known_values() -> None:
    assert two_sided_p_from_t(0.0) == pytest.approx(1.0)
    assert two_sided_p_from_t(1.959963984540054) == pytest.approx(0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# 5) `scoring=NOT_EVALUATED`（docs/10 §7.1）：不评分、不进分母
# ---------------------------------------------------------------------------
def test_marker_is_shared_with_evaluate_extractor() -> None:
    """标记字符串跨脚本唯一（两处漂移会被这个断言抓住）。"""
    from scripts import probe_ic

    assert probe_ic.NOT_EVALUATED_MARKER == NOT_EVALUATED_MARKER == "NOT_EVALUATED"


def test_assemble_stats_wires_ic_icir_wilson_and_bh() -> None:
    stats = _assemble_stats(
        "cfg",
        all_series=("ret_1", "always_long"),
        ic_windows={"ret_1": [0.1, 0.2, 0.3], "always_long": []},
        ic_undefined={"ret_1": 0, "always_long": 5},
        hit_total={"ret_1": 0, "always_long": 60},
        obs_total={"ret_1": 0, "always_long": 100},
        brier_total={"ret_1": 0.0, "always_long": 40.0},
    )

    ret = {row.metric: row for row in stats if row.series_id == "ret_1"}
    assert ret["ic_mean"].value == pytest.approx(0.2)
    assert ret["ic_std"].value == pytest.approx(0.1)
    assert ret["icir"].value == pytest.approx(2.0)
    assert ret["ic_positive_ratio"].value == pytest.approx(1.0)
    assert ret["ic_mean"].p_value is not None and ret["ic_mean"].p_bh is not None

    always = {row.metric: row for row in stats if row.series_id == "always_long"}
    assert always["ic_mean"].scoring == NOT_EVALUATED_MARKER
    assert always["ic_mean"].value is None  # 常量信号 → 不评分（**不是 0**）
    assert always["hit_rate"].value == pytest.approx(0.6)
    assert always["hit_rate"].ci_low is not None and always["hit_rate"].ci_high is not None
    assert always["hit_rate"].ci_low < 0.6 < always["hit_rate"].ci_high


def test_evaluate_config_marks_not_evaluated_when_too_few_rows() -> None:
    result = evaluate_config(_bars(rows=80), _tiny_config())

    assert result.meta["windows"] == 0
    assert result.stats  # 逐系列仍留记录（而不是静默空表）
    assert all(row.scoring == NOT_EVALUATED_MARKER for row in result.stats)
    assert all(row.value is None for row in result.stats)


# ---------------------------------------------------------------------------
# 6) 端到端（合成数据）：跑通 + 可复现
# ---------------------------------------------------------------------------
def test_evaluate_config_end_to_end_on_synthetic_bars() -> None:
    result = evaluate_config(_bars(rows=900), _tiny_config(), train_ratio=0.5, seed=1)

    assert result.meta["windows"] >= 3
    assert result.meta["oos_rows"] > 0
    names = {row.series_id for row in result.stats}
    assert set(FEATURE_NAMES) <= names
    assert {"random", "always_long", "momentum", "ma"} <= names
    assert {"model_lr", "model_hgb"} <= names

    rows = {(row.series_id, row.metric): row for row in result.stats}
    for series_id in ("random", "momentum", "model_lr"):
        hit = rows[(series_id, "hit_rate")]
        assert hit.value is not None and 0.0 <= hit.value <= 1.0
        assert hit.ci_low is not None and hit.ci_high is not None
        assert hit.ci_low <= hit.value <= hit.ci_high
        brier = rows[(series_id, "brier")]
        assert brier.value is not None and 0.0 <= brier.value <= 1.0
    # 常量基准的 IC 必须记「未评估」
    assert rows[("always_long", "ic_mean")].scoring == NOT_EVALUATED_MARKER


def test_evaluate_config_directional_vs_volatility_features() -> None:
    """有方向含义的特征给命中率；波动类特征只给 IC，命中率显式记「未评估」。"""
    result = evaluate_config(_bars(rows=900), _tiny_config(), seed=2)
    rows = {(row.series_id, row.metric): row for row in result.stats}

    directional = rows[("momentum_20", "hit_rate")]
    assert directional.value is not None and 0.0 <= directional.value <= 1.0
    assert directional.scoring == ""

    volatility = rows[("realized_vol_20", "hit_rate")]
    assert volatility.value is None
    assert volatility.scoring == NOT_EVALUATED_MARKER
    assert "无方向含义" in volatility.note


def test_evaluate_config_is_reproducible_with_same_seed() -> None:
    first = evaluate_config(_bars(rows=600), _tiny_config(), seed=5)
    second = evaluate_config(_bars(rows=600), _tiny_config(), seed=5)

    assert [row.value for row in first.stats] == [row.value for row in second.stats]
    assert [row.scoring for row in first.stats] == [row.scoring for row in second.stats]


def test_render_report_contains_required_sections() -> None:
    result = evaluate_config(_bars(rows=600), _tiny_config(), seed=3)
    text = render_report(
        [result],
        symbol="XAUUSD",
        data_notes=["取数方式：`fixture`"],
        generated_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        repro_command="python scripts/probe_ic.py --dry-run",
    )

    for section in (
        "# Phase 3 最小验证探针报告",
        "## 0. 结论摘要",
        "## 1. 配置 `test_1h_h1`",
        "## 4. 未评估台账",
        "## 5. 局限与下一步",
        "## 附录 A. 复现命令",
        "## 附录 B. 数据指纹与审计",
    ):
        assert section in text
    assert "禁止 shuffle" in text
    assert "不使用任何宏观数据" in text


def test_write_outputs_writes_four_kinds_of_artifacts(tmp_path: Path) -> None:
    frame = _bars(rows=300)
    config = _tiny_config()
    result = evaluate_config(frame, config)
    fetch = FetchResult("TEST", config.interval, config.lookback, frame)
    long_table = LONG_TABLE_COLUMNS

    written = write_outputs(
        out_dir=tmp_path / "out",
        report_path=tmp_path / "reports" / "probe.md",
        symbol="XAUUSD",
        fetches={"1h|2y": fetch},
        results=[result],
        report_text="# report\n",
        meta_payload={"caveats": ["只读探针"]},
    )

    names = {path.name for path in written}
    assert names == {"XAUUSD_1h_2y.csv", "ic_long_table.csv", "probe_ic_meta.json", "probe.md"}
    assert (tmp_path / "reports" / "probe.md").read_text(encoding="utf-8") == "# report\n"
    bars_csv = tmp_path / "out" / "XAUUSD_1h_2y.csv"
    assert len(bars_csv.read_text(encoding="utf-8").splitlines()) == len(frame) + 1
    with (tmp_path / "out" / "ic_long_table.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle).fieldnames or []) == list(long_table)
    meta = json.loads((tmp_path / "out" / "probe_ic_meta.json").read_text(encoding="utf-8"))
    assert meta["caveats"] == ["只读探针"]


# ---------------------------------------------------------------------------
# 7) CLI：默认 dry-run、fixture 模式绝不联网、错误退 2
# ---------------------------------------------------------------------------
def _cli_args(tmp_path: Path, frame: pd.DataFrame, *extra: str) -> list[str]:
    fixture = _write_bars_csv(tmp_path / "fixture.csv", frame)
    return [
        "--source",
        "fixture",
        "--bars-fixture",
        str(fixture),
        "--configs",
        "1d_1y_h1",
        "--out-dir",
        str(tmp_path / "out"),
        "--report",
        str(tmp_path / "report.md"),
        *extra,
    ]


def test_main_dry_run_prints_report_and_writes_nothing(tmp_path: Path, capsys) -> None:
    code = main(_cli_args(tmp_path, _bars(rows=600)))

    out = capsys.readouterr().out
    assert code == 0
    assert "# Phase 3 最小验证探针报告" in out
    assert "未写任何文件" in out
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "report.md").exists()


def test_main_no_dry_run_writes_outputs(tmp_path: Path, capsys) -> None:
    code = main(_cli_args(tmp_path, _bars(rows=900), "--no-dry-run"))

    assert code == 0
    assert (tmp_path / "out" / "XAUUSD_1d_1y.csv").exists()
    long_table = tmp_path / "out" / "ic_long_table.csv"
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## 0. 结论摘要" in report
    assert "未评估台账" in report
    with long_table.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and "scoring" in rows[0]
    meta = json.loads((tmp_path / "out" / "probe_ic_meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "fixture"
    assert any("未引入新依赖" in caveat for caveat in meta["caveats"])
    assert "已写出" in capsys.readouterr().out


def test_main_fixture_mode_never_touches_network(tmp_path: Path, monkeypatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("fixture 模式不得调用联网取数")

    monkeypatch.setattr("scripts.probe_ic.fetch_yahoo_bars", _boom)

    assert main(_cli_args(tmp_path, _bars(rows=600))) == 0


def test_main_rejects_conflicting_flags(tmp_path: Path) -> None:
    assert main(_cli_args(tmp_path, _bars(rows=300), "--dry-run", "--no-dry-run")) == 2


def test_main_requires_fixture_path(tmp_path: Path, capsys) -> None:
    code = main(["--source", "fixture", "--configs", "1d_1y_h1", "--dry-run"])

    assert code == 2
    assert "bars-fixture" in capsys.readouterr().err


def test_main_fixture_mode_allows_single_config_only(tmp_path: Path, capsys) -> None:
    fixture = _write_bars_csv(tmp_path / "f.csv", _bars(rows=300))

    code = main(
        [
            "--source",
            "fixture",
            "--bars-fixture",
            str(fixture),
            "--configs",
            "1d_1y_h1,1h_2y_h4",
            "--out-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    assert code == 2
    assert "一次只允许一条配置" in capsys.readouterr().err


def test_main_unknown_config_returns_2(tmp_path: Path, capsys) -> None:
    code = main(["--configs", "nope", "--dry-run"])

    assert code == 2
    assert "未知配置" in capsys.readouterr().err


def test_main_reports_fetch_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("所有候选 ticker 均取数失败：HTTP 404")

    monkeypatch.setattr("scripts.probe_ic.fetch_yahoo_bars", _boom)

    code = main(["--configs", "1d_1y_h1", "--out-dir", str(tmp_path / "out"), "--dry-run"])

    assert code == 2
    assert "取数失败" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_select_configs_and_repro_command() -> None:
    assert select_configs("") == PROBE_CONFIGS
    assert [config.name for config in select_configs(" 1d_1y_h1 ")] == ["1d_1y_h1"]
    with pytest.raises(ValueError, match="未知配置"):
        select_configs("nope")

    args = Namespace(
        symbol="XAUUSD",
        configs="1d_1y_h1",
        source="fixture",
        bars_fixture="logs/probe_ic/x.csv",
        train_ratio=0.5,
        seed=42,
        out_dir="logs/probe_ic",
    )

    command = build_repro_command(args)

    assert "--configs 1d_1y_h1" in command
    assert "--source fixture --bars-fixture logs/probe_ic/x.csv" in command
    assert "--train-ratio 0.5 --seed 42" in command
    assert command.rstrip().endswith("--no-dry-run")
