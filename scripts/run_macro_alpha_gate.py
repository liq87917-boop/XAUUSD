"""运行 Phase 3.2 Macro Alpha 独立 OOS 门禁；默认不落盘。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.session import build_engine  # noqa: E402
from scripts._console import configure_stdout, safe_print  # noqa: E402
from src.alpha.macro import MacroGateResult, evaluate_macro_gate  # noqa: E402

REPORT = ROOT / "docs" / "experiments" / "Phase3_2_Macro_Alpha报告.md"


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    engine = build_engine()
    bar_query = sa.text(
        """SELECT mb.open_time, mb.close_time, mb.open, mb.high, mb.low, mb.close
        FROM market_bars mb JOIN instruments i ON i.id=mb.instrument_id
        WHERE i.symbol=:symbol AND mb.timeframe='1d' ORDER BY mb.open_time"""
    )
    macro_query = sa.text(
        """SELECT event_code, released_at, actual_value FROM macro_events
        WHERE actual_value IS NOT NULL ORDER BY released_at"""
    )
    with engine.connect() as connection:
        gold = pd.read_sql(bar_query, connection, params={"symbol": "XAUUSD"})
        dxy = pd.read_sql(bar_query, connection, params={"symbol": "DXY"})
        macro = pd.read_sql(macro_query, connection)
    for frame in (gold, dxy):
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
        frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
        frame.set_index("open_time", inplace=True)
    return gold, macro, dxy


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def render(result: MacroGateResult) -> str:
    table = [
        f"| {m.name} | {_fmt(m.ic)} | {_fmt(m.icir)} | {m.hit_rate:.2%} | "
        f"[{m.wilson_low:.2%}, {m.wilson_high:.2%}] | {m.hit_p_value:.4f} | "
        f"{m.brier:.4f} | {m.observations} |"
        for m in result.metrics
    ]
    calibration = [
        f"| {item['bin']} | {item['count']} | {item['mean_probability']:.4f} | "
        f"{item['observed_rate']:.4f} |"
        for item in result.calibration_bins
    ]
    verdict = "PASS" if result.passed else "FAIL（停止写入 Alpha 事实表）"
    return "\n".join(
        [
            "# Phase 3.2 Macro Alpha 独立 OOS 门禁报告",
            "",
            f"> **结论：{verdict}**。本报告只评价信号质量，不是交易策略或收益回测。",
            "",
            "## 1. 冻结口径",
            "",
            "- 标签资产：数据库 `XAUUSD` 的 Yahoo `GC=F` COMEX 连续期货代理；不得表述成现货验证。",
            "- 宏观输入：8 条 FRED/ALFRED initial-release-only 序列，严格按 `released_at` as-of；"
            "另含 DXY 日收益与 20 日动量。",
            "- 切分：60/20/20 时间序；边界各 embargo 1 个日线标签；禁止 shuffle。",
            "- 模型：StandardScaler + L2 Logistic Regression；Platt 只在 validation 拟合。",
            "- PASS：IC >= 0.03 且 ICIR >= 0.3，或命中率单侧精确二项 p < 0.05。",
            "",
            "## 2. 数据与因果审计",
            "",
            f"- 可用 {result.rows_usable:,}；train {result.train_rows:,}；"
            f"validation {result.validation_rows:,}；test {result.test_rows:,}。",
            f"- 特征数：{len(result.feature_names)}；发布时刻晚于信号时刻违规："
            f"{result.max_release_violation_count}。",
            f"- 数据 SHA-256：`{result.data_hash}`",
            f"- 特征集：`{result.feature_set_version}`；模型：`{result.model_version}`；"
            f"seed={result.seed}；代码提交：`{_commit()}`。",
            "",
            "## 3. OOS 结果",
            "",
            "| 信号 | IC | ICIR | 命中率 | Wilson 95% CI | p(>50%) | Brier | N |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            *table,
            "",
            "## 4. 校准",
            "",
            f"- 未校准 test Brier：{result.raw_brier:.4f}",
            f"- Platt 校准 test Brier：{result.calibrated_brier:.4f}",
            "- 只有校准概率参与正式比较；未校准概率不得进入下游。",
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
                else "门槛未通过：不建表、不写信号、不与 Technical Alpha 混合。"
            ),
            "",
            "## 6. 边界",
            "",
            "结果仅适用于 `GC=F` 代理及当前数据版本；未计交易成本、仓位与风险。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    configure_stdout()
    result = evaluate_macro_gate(*load_inputs())
    report = render(result)
    safe_print(report)
    if args.no_dry_run:
        args.report.write_text(report, encoding="utf-8")
        safe_print(f"报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
