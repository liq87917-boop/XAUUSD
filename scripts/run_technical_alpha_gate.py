"""运行 Phase 3.2 Technical Alpha 独立 OOS 门禁；默认只打印，不落盘。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

import pandas as pd
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from database.session import build_engine  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.alpha.technical import TechnicalGateResult, evaluate_technical_gate  # noqa: E402

DEFAULT_REPORT: Final[Path] = REPO_ROOT / "docs" / "experiments" / "Phase3_2_Technical_Alpha报告.md"


def load_bars(symbol: str) -> pd.DataFrame:
    query = sa.text(
        """SELECT mb.open_time, mb.close_time, mb.open, mb.high, mb.low, mb.close, mb.volume
        FROM market_bars mb JOIN instruments i ON i.id = mb.instrument_id
        WHERE i.symbol = :symbol AND mb.timeframe = '1h'
        ORDER BY mb.open_time"""
    )
    with build_engine().connect() as connection:
        frame = pd.read_sql(query, connection, params={"symbol": symbol})
    if frame.empty:
        raise ValueError(f"{symbol} 没有 1h 行情")
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    return frame.set_index("open_time")


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def render_report(result: TechnicalGateResult, symbol: str) -> str:
    rows = []
    for metric in result.metrics:
        rows.append(
            f"| {metric.name} | {_fmt(metric.ic)} | {_fmt(metric.icir)} | "
            f"{metric.hit_rate:.2%} | [{metric.wilson_low:.2%}, {metric.wilson_high:.2%}] | "
            f"{metric.hit_p_value:.4f} | {metric.brier:.4f} | {metric.observations} |"
        )
    calibration = [
        f"| {item['bin']} | {item['count']} | {item['mean_probability']:.4f} | "
        f"{item['observed_rate']:.4f} |"
        for item in result.calibration_bins
    ]
    verdict = "PASS" if result.passed else "FAIL（停止写入 Alpha 事实表）"
    return "\n".join(
        [
            "# Phase 3.2 Technical Alpha 独立 OOS 门禁报告",
            "",
            f"> **结论：{verdict}**。本报告只评价信号质量，不是交易策略或收益回测。",
            "",
            "## 1. 冻结口径",
            "",
            f"- 数据：`{symbol}` 独立 Dukascopy 现货 1h 序列，不与 `GC=F` 混合。",
            "- 特征：return / momentum / MA / RSI / MACD / ATR / realized vol / breakout。",
            "- 标签：特征 bar 完成后，严格选择 `open_time > signal_at` 的下一根；"
            "持有 24 根可交易 1h bar；允许日常计划性停盘，周末/异常长缺口丢弃。",
            "- 切分：60% train / 20% validation / 20% untouched test；"
            "边界各 embargo 24 根；禁止 shuffle。",
            "- 模型：StandardScaler + L2 Logistic Regression；Platt 只在 validation 拟合。",
            "- 正式统计：test 每 24 根抽 1 条非重叠标签；随机种子 42。",
            "- PASS：IC >= 0.03 且 ICIR >= 0.3，或单侧精确二项检验 p < 0.05。",
            "",
            "## 2. 数据与切分",
            "",
            f"- 原始 {result.rows_total:,}；可用 {result.rows_usable:,}。",
            f"- train {result.train_rows:,}；validation {result.validation_rows:,}；"
            f"test {result.test_rows:,}。",
            f"- 正式非重叠 test 样本 {result.non_overlapping_test_rows:,}。",
            f"- 信号区间：{result.first_signal_at.isoformat()} "
            f"至 {result.last_signal_at.isoformat()}。",
            "",
            "## 3. OOS 结果",
            "",
            "| 信号 | IC | ICIR | 命中率 | Wilson 95% CI | p(>50%) | Brier | N |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "## 4. 概率校准",
            "",
            f"- 未校准 test Brier：{result.raw_brier:.4f}",
            f"- Platt 校准 test Brier：{result.calibrated_brier:.4f}",
            "- 下表是完整 test 的可靠性分箱；未校准概率不得进入下游信号。",
            "",
            "| 分箱 | N | 平均预测概率 | 实际上涨率 |",
            "|---:|---:|---:|---:|",
            *calibration,
            "",
            "## 5. 门禁动作",
            "",
            (
                "门槛通过；下一小步才允许设计并迁移 Alpha 事实表。"
                if result.passed
                else "门槛未通过：不建立/不写入 `alpha_models`、`alpha_signals` 或 `predictions`；"
                "保留负面结果，随后独立验证 Macro Alpha，不做技术+宏观混合。"
            ),
            "",
            "## 6. 边界",
            "",
            "本结果不能证明可交易性；未计手续费、滑点、仓位、止损或风险预算。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XAUUSD_DUKASCOPY")
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    configure_stdout()
    result = evaluate_technical_gate(load_bars(args.symbol))
    report = render_report(result, args.symbol)
    safe_print(report)
    if args.no_dry_run:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
