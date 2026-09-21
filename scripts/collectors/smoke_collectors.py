"""三个采集器冒烟测试（真实调用，**不落库**，仅验证连通性与字段）。

用法：python scripts/collectors/smoke_collectors.py
敏感值（Token / API Key）只从 .env / 环境变量读取，绝不硬编码。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collectors.akshare_gold import parse_sge_bars  # noqa: E402
from src.collectors.dbnomics_macro import parse_dbnomics_series  # noqa: E402
from src.collectors.opennews import BASE_URL, OPENNEWS_TOKEN_ENV, SEARCH_PATH  # noqa: E402


def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def _rows(frame):
    return frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)


def smoke_akshare() -> None:
    print("=" * 60)
    print("1) akshare_gold  |  ak.spot_hist_sge('Au99.99')")
    print("=" * 60)
    try:
        import akshare as ak

        df = ak.spot_hist_sge(symbol="Au99.99")
        print(f"总条数: {len(df)}  列名: {list(df.columns)}")
        points = parse_sge_bars(_rows(df.tail(5)))
        print(f"解析成功: {len(points)} 条")
        for point in points:
            print(f"  {point.open_time.date()} close={point.close} volume={point.volume}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] akshare_gold: {type(exc).__name__}: {exc}")


def smoke_dbnomics() -> None:
    print("=" * 60)
    print("2) dbnomics_macro  |  dbnomics.fetch_series('IMF', 'CPI', REF_AREA=US)")
    print("=" * 60)
    try:
        import dbnomics

        df = dbnomics.fetch_series("IMF", "CPI", dimensions={"REF_AREA": "US"})
        print(f"总条数: {len(df)}  列名: {list(df.columns)}")
        records = _rows(df.tail(5))
        print("最近 5 条原始记录:")
        for record in records:
            print(f"  {record}")
        normalized = []
        for record in records:
            row = {}
            for key, value in record.items():
                if key == "period":
                    row["period"] = value
                elif key == "value":
                    row["value"] = value
                elif key == "series_code":
                    row["series_code"] = value
            normalized.append(row)
        observations = parse_dbnomics_series(normalized, series_id="IMF/CPI")
        print(f"解析成功: {len(observations)} 条")
        for obs in observations:
            print(f"  {obs.event_at.date()} value={obs.value} code={obs.series_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] dbnomics_macro: {type(exc).__name__}: {exc}")


def smoke_opennews() -> None:
    print("=" * 60)
    print("3) opennews  |  POST /open/news_search（Bearer Token）")
    print("=" * 60)
    token = os.environ.get(OPENNEWS_TOKEN_ENV, "").strip()
    if not token:
        print(f"[FAIL] 缺少 {OPENNEWS_TOKEN_ENV}（.env 未配置）")
        return
    try:
        response = httpx.post(
            f"{BASE_URL}{SEARCH_PATH}",
            json={"keyword": "gold"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        print(f"HTTP 状态码: {response.status_code}")
        out = ROOT / "logs" / "_opennews_response.json"
        out.write_text(response.text, encoding="utf-8")
        print(f"原始响应已写入 {out}（UTF-8），响应长度 {len(response.text)} 字符")
        if response.status_code == 200:
            from src.collectors.opennews import parse_news_search

            items = parse_news_search(response.json())
            print(f"解析成功: {len(items)} 条 news（已过滤非 engineType=news）")
            for item in items[:5]:
                print(
                    f"  headline={item.title[:60]!r} published_at={item.published_at} "
                    f"source={item.source_name} url={item.url}"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] opennews: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    _load_env()
    smoke_akshare()
    print()
    smoke_dbnomics()
    print()
    smoke_opennews()
