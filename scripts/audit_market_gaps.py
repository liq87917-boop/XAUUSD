"""W0-1 缺口审计：把 provider 的空 bar 拆成「常规休市 / 疑似假期 / **数据缺失** / **时间戳缺失**」。

**要回答的问题**（用户 2026-09-15 决策）：探针在 1h/2y 上看到 **3059 个空 bar（≈21%）**——
它们到底是「休市/流动性空档」（预期内），还是「数据缺失」（必须修）？

**方法（数据驱动，不硬编码 DST / 假期表）**：

1. 用同一份数据标定 `(UTC 星期, UTC 小时)` 桶的「有 bar 比例」；
2. 比例 < `--closed-threshold`（默认 2%）的桶 = **常规休市**（周末、每日结算间隙）；
3. 工作日里连续 ≥ 6 个空槽且当日几乎无 bar → **疑似假期**（形态特征，需人工确认）；
4. 常规开市时段内的其余空槽 → **数据缺失（异常）**；
5. 按会话日历推算「应有」、但 provider **连时间戳都没返回**的槽 → **时间戳缺失（异常）**。

**为什么这样分类**：只有 3/4/5 是质量问题，1/2 是市场本身的作息；把它们混成一个「缺口率」
会得出错误结论（例如把周末算成数据缺失）。

用法::

    python scripts/audit_market_gaps.py --dry-run                    # 默认：只打印，不写文件
    python scripts/audit_market_gaps.py --no-dry-run                 # 落盘 CSV + Markdown 报告
    python scripts/audit_market_gaps.py --symbols XAUUSD,DXY --timeframes 1h --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts._market_data import (  # noqa: E402
    CATEGORY_DATA_GAP,
    CATEGORY_HOLIDAY_SUSPECT,
    CATEGORY_LABELS,
    CATEGORY_MISSING_TIMESTAMP,
    CATEGORY_SESSION_BREAK,
    DEFAULT_LOOKBACKS,
    DEFAULT_SESSION_TZ,
    TIMEFRAME_MINUTES,
    GapAudit,
    audit_gaps,
    fetch_chart,
)

DEFAULT_SYMBOLS: Final[str] = "XAUUSD"
DEFAULT_TIMEFRAMES: Final[str] = "1h,1d"
DEFAULT_CLOSED_THRESHOLD: Final[float] = 0.02
DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "logs" / "market_audit"
DEFAULT_REPORT: Final[Path] = (
    REPO_ROOT / "docs" / "experiments" / "Phase3.0_W0-1_行情缺口审计报告.md"
)
AUDIT_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "interval",
    "lookback",
    "provider_symbol",
    "total_slots",
    "valid_slots",
    "null_slots",
    "bad_timestamps",
    "session_break",
    "holiday_suspect",
    "data_gap",
    "missing_timestamp",
    "null_ratio",
    "anomaly_ratio",
    "longest_data_gap_run",
    "provider_digest",
)
FINDING_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "interval",
    "open_time",
    "category",
    "note",
)
#: 报告里最多列多少条异常明细
MAX_REPORT_FINDINGS: Final[int] = 30


def select_items(symbols: str, timeframes: str) -> tuple[tuple[str, str], ...]:
    """解析 `--symbols/--timeframes` → `((symbol, interval), ...)`。

    Raises:
        ValueError: 空列表、未知周期（含 provider 不支持的 4h）。
    """
    symbol_list = tuple(item.strip() for item in symbols.split(",") if item.strip())
    interval_list = tuple(item.strip() for item in timeframes.split(",") if item.strip())
    if not symbol_list or not interval_list:
        raise ValueError("--symbols 与 --timeframes 都不能为空")
    for interval in interval_list:
        if interval == "4h":
            raise ValueError("provider 无 4h 粒度（TD-03：4h 由 1h 聚合）→ 请审计 1h/1d")
        if interval not in TIMEFRAME_MINUTES:
            raise ValueError(f"未知周期 {interval!r}；可选：{sorted(TIMEFRAME_MINUTES)}")
    return tuple((symbol, interval) for symbol in symbol_list for interval in interval_list)


def audit_one(
    symbol: str,
    interval: str,
    *,
    lookback: str | None = None,
    closed_threshold: float = DEFAULT_CLOSED_THRESHOLD,
    session_tz: str = DEFAULT_SESSION_TZ,
    timeout: float = 60.0,
) -> GapAudit:
    """取数 + 审计一个 `(symbol, interval)`（真实联网；会带 UA 并走候选链）。

    Raises:
        RuntimeError: 该标的无可用行情源或取数失败（调用方记为 FAILED，不让整轮崩溃）。
    """
    window = lookback or DEFAULT_LOOKBACKS.get(interval, "2y")
    fetch = fetch_chart(symbol, interval=interval, lookback=window, timeout=timeout)
    return audit_gaps(fetch, closed_threshold=closed_threshold, session_tz=session_tz)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def render_report(
    audits: Sequence[GapAudit],
    *,
    failures: Sequence[str],
    closed_threshold: float,
    session_tz: str = DEFAULT_SESSION_TZ,
    generated_at: datetime,
    repro_command: str,
) -> str:
    """生成审计报告（Markdown，纯函数：输入=审计结果，输出=文本）。"""
    lines: list[str] = []
    add = lines.append
    add("# Phase 3.0 W0-1 行情缺口审计报告")
    add("")
    add(
        "> **目的**：回答「探针在 1h/2y 上看到的 ≈21% 空 bar 是**休市**还是**数据缺失**」。"
        "只有 `data_gap` / `missing_timestamp` 才是质量问题；`session_break`（周末、每日结算间隙）"
        "与 `holiday_suspect`（疑似假期）是市场作息。"
    )
    add("")
    add(f"- 生成时间（UTC）：`{generated_at.isoformat(timespec='seconds')}`")
    add(
        f"- 会话标定：按**交易所本地时区 `{session_tz}`** 分桶（`(本地星期, 本地小时)`）"
        f"，桶内有效 bar 比例 < **{closed_threshold:.0%}** → 判为常规休市"
    )
    add("- 数据源：Yahoo Finance chart JSON（`market_yahoo`，**必须带 User-Agent**，否则 429）")
    add("- **不使用任何宏观数据**；不做 4h（provider 无此粒度，见 TD-03）")
    for issue in failures:
        add(f"- ⚠️ {issue}")
    add("")

    add("## 0. 结论摘要")
    add("")
    total_anomalies = sum(audit.anomaly_count for audit in audits)
    total_slots = sum(audit.total_slots for audit in audits)
    if audits:
        if total_anomalies == 0:
            add(
                f"- **{total_slots} 个时间槽中真正的数据质量问题 = 0**：所有空 bar 都落在"
                "常规休市/疑似假期（市场作息，**不是**数据缺失）。"
            )
        else:
            ratio = total_anomalies / total_slots if total_slots else 0.0
            add(
                f"- **真正的数据质量问题 = {total_anomalies} 个槽（占 {ratio:.2%}）**："
                "`data_gap` + `missing_timestamp` 明细见 §2；其余空槽是休市/假期。"
            )
    add("")
    add(
        "| 标的 | 周期 | 槽数 | 有效 | 空槽 | 常规休市 | 疑似假期 | **数据缺失** | **时间戳缺失** "
        "| 空槽占比 | 异常占比 |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for audit in audits:
        anomaly_ratio = audit.anomaly_count / audit.total_slots if audit.total_slots else 0.0
        add(
            f"| `{audit.symbol}` | {audit.interval} | {audit.total_slots} | {audit.valid_slots} "
            f"| {audit.null_slots} | {audit.counts.get(CATEGORY_SESSION_BREAK, 0)} "
            f"| {audit.counts.get(CATEGORY_HOLIDAY_SUSPECT, 0)} "
            f"| **{audit.counts.get(CATEGORY_DATA_GAP, 0)}** "
            f"| **{audit.counts.get(CATEGORY_MISSING_TIMESTAMP, 0)}** "
            f"| {audit.null_ratio:.2%} | {anomaly_ratio:.2%} |"
        )
    add("")

    add("## 1. 逐项明细（含数据指纹）")
    add("")
    for index, audit in enumerate(audits, start=1):
        add(f"### 1.{index} `{audit.symbol}` / {audit.interval}（回溯 `{audit.lookback}`）")
        add("")
        add(
            f"- provider ticker：`{audit.provider_symbol}`；内容指纹：`{audit.provider_digest}`；"
            f"坏时间戳：{audit.bad_timestamps}"
        )
        add(
            f"- 区间：`{audit.first_slot_at}` ~ `{audit.last_slot_at}`；最长连续 `data_gap`："
            f"{audit.longest_run.get(CATEGORY_DATA_GAP, 0)} 个槽；最长连续休市："
            f"{audit.longest_run.get(CATEGORY_SESSION_BREAK, 0)} 个槽"
        )
        add("")
        add("| 类别 | 数量 | 说明 |")
        add("|---|---|---|")
        for category, label in CATEGORY_LABELS.items():
            add(f"| `{category}` | {audit.counts.get(category, 0)} | {label} |")
        add("")

    anomalies = [
        finding
        for audit in audits
        for finding in audit.findings
        if finding.category in {CATEGORY_DATA_GAP, CATEGORY_MISSING_TIMESTAMP}
    ]
    add(f"## 2. 异常明细（`data_gap` / `missing_timestamp`，最多 {MAX_REPORT_FINDINGS} 条）")
    add("")
    if anomalies:
        add("| 标的 | 周期 | 时间槽（UTC） | 类别 | 说明 |")
        add("|---|---|---|---|---|")
        for finding in anomalies[:MAX_REPORT_FINDINGS]:
            add(
                f"| `{finding.symbol}` | {finding.interval} | `{finding.open_time.isoformat()}` "
                f"| `{finding.category}` | {finding.note} |"
            )
    else:
        add("- **无**：没有任何槽落在常规开市时段却缺数据。")
    add("")

    holidays = sorted(
        {
            finding.open_time.date().isoformat()
            for audit in audits
            for finding in audit.findings
            if finding.category == CATEGORY_HOLIDAY_SUSPECT
        }
    )
    add("## 3. 疑似假期清单（需人工确认，**不是**数据缺失）")
    add("")
    add(f"- 命中日期：{'、'.join(holidays) if holidays else '（无）'}")
    add(
        "- 判据：工作日整段无 bar（形态与交易所假期一致）。脚本**刻意不硬编码假期表**，"
        "避免维护错误；如需完全确认请对照 CME 假日表人工核对。"
    )
    add("")

    add("## 4. 覆盖不足的日子（top 10）")
    add("")
    if any(audit.low_coverage_days for audit in audits):
        add("| 标的 | 周期 | UTC 日期 | 有效 bar | 应有槽 | 缺口 |")
        add("|---|---|---|---|---|---|")
        for audit in audits:
            for date, present, expected in audit.low_coverage_days:
                add(
                    f"| `{audit.symbol}` | {audit.interval} | {date} | {present} | {expected} "
                    f"| {expected - present} |"
                )
    else:
        add("- 无（每个交易日的 bar 数都与会话日历一致）")
    add("")

    add("## 5. 方法学与局限")
    add("")
    add(
        "1. **会话日历来自数据本身**，且按**交易所本地时区**核账（CME 黄金 = 美东）："
        "自动适配 DST、不维护假期表；代价是「长期流动性极差的时段」也可能被归入休市；"
    )
    add(
        "2. **阈值敏感性**：`--closed-threshold` 默认 2%；调高会把更多稀疏时段判成休市、"
        "调低会把休市判成缺失 —— 引用数字时必须连同阈值一起引用；"
    )
    add(
        "3. **只审计 provider 侧**：本报告只看「provider 给了什么」，不检查落库是否完整"
        "（落库完整性由 W0-1 回填的 `collector_runs` 与入库行数核对）；"
    )
    add(
        "4. **`US10Y_REAL` 不在本审计范围**：Yahoo 无实际利率序列（`DFII10` → 404）"
        "→ 由 W0-2 的 FRED 提供。"
    )
    add("")
    add("## 附录 A. 复现命令")
    add("")
    add("```powershell")
    add(repro_command)
    add("```")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def audit_rows(audits: Sequence[GapAudit]) -> list[dict[str, str]]:
    """审计汇总行（每 `(symbol, interval)` 一行）。"""
    rows: list[dict[str, str]] = []
    for audit in audits:
        ratio = audit.anomaly_count / audit.total_slots if audit.total_slots else 0.0
        rows.append(
            {
                "symbol": audit.symbol,
                "interval": audit.interval,
                "lookback": audit.lookback,
                "provider_symbol": audit.provider_symbol,
                "total_slots": str(audit.total_slots),
                "valid_slots": str(audit.valid_slots),
                "null_slots": str(audit.null_slots),
                "bad_timestamps": str(audit.bad_timestamps),
                "session_break": str(audit.counts.get(CATEGORY_SESSION_BREAK, 0)),
                "holiday_suspect": str(audit.counts.get(CATEGORY_HOLIDAY_SUSPECT, 0)),
                "data_gap": str(audit.counts.get(CATEGORY_DATA_GAP, 0)),
                "missing_timestamp": str(audit.counts.get(CATEGORY_MISSING_TIMESTAMP, 0)),
                "null_ratio": f"{audit.null_ratio:.6f}",
                "anomaly_ratio": f"{ratio:.6f}",
                "longest_data_gap_run": str(audit.longest_run.get(CATEGORY_DATA_GAP, 0)),
                "provider_digest": audit.provider_digest,
            }
        )
    return rows


def finding_rows(audits: Sequence[GapAudit]) -> list[dict[str, str]]:
    """逐槽发现（含全部四类；`session_break` 不写入以避免文件过大）。"""
    rows: list[dict[str, str]] = []
    for audit in audits:
        for finding in audit.findings:
            if finding.category == CATEGORY_SESSION_BREAK:
                continue
            rows.append(
                {
                    "symbol": finding.symbol,
                    "interval": finding.interval,
                    "open_time": finding.open_time.isoformat(),
                    "category": finding.category,
                    "note": finding.note,
                }
            )
    return rows


def write_outputs(
    *,
    out_dir: Path,
    report_path: Path,
    audits: Sequence[GapAudit],
    report_text: str,
    meta_payload: dict[str, Any],
) -> list[Path]:
    """落盘：审计汇总 CSV + 逐槽发现 CSV + 元数据 + 报告（**只在 `--no-dry-run` 下调用**）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    summary = out_dir / "gap_audit_summary.csv"
    with summary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(AUDIT_COLUMNS))
        writer.writeheader()
        writer.writerows(audit_rows(audits))
    written.append(summary)
    findings = out_dir / "gap_findings.csv"
    with findings.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FINDING_COLUMNS))
        writer.writeheader()
        writer.writerows(finding_rows(audits))
    written.append(findings)
    meta_path = out_dir / "gap_audit_meta.json"
    meta_path.write_text(
        json.dumps(meta_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append(meta_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    written.append(report_path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_repro_command(args: argparse.Namespace) -> str:
    """报告附录里的复现命令（与本次实际参数一致）。"""
    parts = [
        "python scripts/audit_market_gaps.py",
        f"--symbols {args.symbols}",
        f"--timeframes {args.timeframes}",
        f"--session-tz {args.session_tz}",
        f"--closed-threshold {args.closed_threshold}",
    ]
    if args.lookback:
        parts.append(f"--lookback {args.lookback}")
    parts.append("--dry-run")
    return " ".join(parts) + "   # 默认 dry-run；落盘需显式 --no-dry-run"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/audit_market_gaps.py",
        description="W0-1 行情缺口审计：区分「休市/流动性空档」与「数据缺失」（只读 provider）",
    )
    parser.add_argument(
        "--symbols", default=DEFAULT_SYMBOLS, help=f"逗号分隔（默认 {DEFAULT_SYMBOLS}）"
    )
    parser.add_argument(
        "--timeframes",
        default=DEFAULT_TIMEFRAMES,
        help=f"逗号分隔（默认 {DEFAULT_TIMEFRAMES}；**不支持 4h** —— provider 无此粒度）",
    )
    parser.add_argument(
        "--lookback", default="", help="覆盖回溯窗口（如 2y）；空 = 用默认（1d→10y、1h→2y）"
    )
    parser.add_argument(
        "--closed-threshold",
        type=float,
        default=DEFAULT_CLOSED_THRESHOLD,
        help="判「常规休市」的桶比例阈值（默认 0.02）",
    )
    parser.add_argument(
        "--session-tz",
        default=DEFAULT_SESSION_TZ,
        help=f"会话本地时区（默认 {DEFAULT_SESSION_TZ}；CME 黄金会话按美东定义 → DST 免疫）",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="取数超时（秒）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="落盘目录")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径（Markdown）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印，不写任何文件（**默认行为**）"
    )
    parser.add_argument("--no-dry-run", action="store_true", help="显式允许落盘")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """入口；退出码 0=成功（含 dry-run）/ 2=参数错误或全部取数失败。"""
    configure_stdout()
    args = _parse_args(argv)
    if args.dry_run and args.no_dry_run:
        print("[audit] --dry-run 与 --no-dry-run 不能同时使用", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run

    try:
        items = select_items(args.symbols, args.timeframes)
    except ValueError as exc:
        print(f"[audit] {exc}", file=sys.stderr)
        return 2

    items_label = [f"{symbol}/{interval}" for symbol, interval in items]
    print(f"[audit] 审计项：{items_label}｜closed_threshold={args.closed_threshold}")
    print(
        "[audit] 口径：空槽分四类（常规休市 / 疑似假期 / **数据缺失** / **时间戳缺失**）；"
        "只有后两类是质量问题"
    )

    audits: list[GapAudit] = []
    failures: list[str] = []
    for symbol, interval in items:
        lookback = args.lookback or DEFAULT_LOOKBACKS.get(interval, "2y")
        try:
            print(f"[audit] 取数 + 审计：{symbol} {interval}（lookback={lookback}）")
            audit = audit_one(
                symbol,
                interval,
                lookback=args.lookback or None,
                closed_threshold=args.closed_threshold,
                session_tz=args.session_tz,
                timeout=args.timeout,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            failures.append(f"{symbol} {interval}：{type(exc).__name__}: {exc}")
            print(
                f"[audit][FAILED] {symbol} {interval}：{type(exc).__name__}: {exc}", file=sys.stderr
            )
            continue
        audits.append(audit)
        print(f"[audit] {audit.summary_lines()[0]}")

    if not audits:
        print("[audit] 没有任何成功审计项（见上方 FAILED 原因）", file=sys.stderr)
        return 2

    report_text = render_report(
        audits,
        failures=failures,
        closed_threshold=args.closed_threshold,
        session_tz=args.session_tz,
        generated_at=datetime.now(UTC),
        repro_command=build_repro_command(args),
    )
    if dry_run:
        safe_print(report_text)
        print("[audit] --dry-run：未写任何文件（加 --no-dry-run 才落盘）")
        return 0

    written = write_outputs(
        out_dir=Path(args.out_dir),
        report_path=Path(args.report),
        audits=audits,
        report_text=report_text,
        meta_payload={
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "closed_threshold": args.closed_threshold,
            "session_tz": args.session_tz,
            "items": [f"{symbol}/{interval}" for symbol, interval in items],
            "failures": failures,
            "audits": audit_rows(audits),
            "caveats": [
                "会话日历由数据本身标定，且按交易所本地时区核账（CME 黄金 = 美东 → DST 免疫）",
                "只审计 provider 侧；落库完整性由 W0-1 回填核对",
                "US10Y_REAL 由 W0-2 的 FRED DFII10 提供（Yahoo 无实际利率序列）",
            ],
        },
    )
    print("[audit] 已写出：")
    for path in written:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())
