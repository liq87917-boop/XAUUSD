"""Phase 1 数据源种子（严格遵循 04_数据表结构及字段定义 第 2 节 sources）。

字段映射（04 §2）：
    name / source_type / base_url / timezone / enabled / config_json
    （id、created_at、updated_at 由 ORM 自动生成）

设计说明：
- ``name`` 是自然键（唯一约束），保证重复执行种子不会产生重复来源。
- ``source_type`` 只能取 WEIBO / NEWS / MARKET / MACRO（04 §2）。
- ``timezone`` 记录**来源自身的原始时区**，采集器负责把它转换成 UTC 后入库；
  时间字段一律以 TIMESTAMPTZ(UTC) 存储（06_Cline开发规则 第 7 条）。
- ``config_json`` 保存采集配置（采集器名称、周期、范围）。其中引用的采集器将在
  Phase 1 第 2 步 Collector Framework 中实现；在此之前该字段只描述"计划配置"，
  不会让系统产生任何真实网络请求（06_Cline开发规则 第 24 条：禁止伪完成）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from database.models.enums import SourceType
from src.collectors.macro import DEFAULT_W0_2_SERIES

__all__ = ["SOURCE_SEEDS", "SourceSeed"]


@dataclass(frozen=True, slots=True)
class SourceSeed:
    """sources 表的一行定义。"""

    name: str
    source_type: SourceType
    base_url: str
    timezone: str
    enabled: bool = True
    config_json: dict[str, Any] = field(default_factory=dict)


#: 种子定义（幂等：以 name 为自然键，已存在则跳过）
SOURCE_SEEDS: Final[tuple[SourceSeed, ...]] = (
    SourceSeed(
        name="weibo_main",
        source_type=SourceType.WEIBO,
        base_url="https://m.weibo.cn",
        timezone="Asia/Shanghai",
        config_json={
            "collector": "weibo_collector",
            "scope": "author_accounts",
            "interval_minutes": 30,
            "max_authors_per_run": 100,
            "min_records_per_run": 1,
            "note": "Phase 1 首期 50~100 位财经作者；采集器属 Phase 2，且严禁未授权抓取",
        },
    ),
    SourceSeed(
        name="fed_press_releases",
        source_type=SourceType.NEWS,
        base_url="https://www.federalreserve.gov",
        timezone="UTC",
        config_json={
            "collector": "news_collector",
            "feeds": ["https://www.federalreserve.gov/feeds/press_all.xml"],
            "interval_minutes": 30,
            # 官方源发布频率低（每周数次）：30 分钟窗口内经常没有新稿，
            # 因此不设阈值（0=不检查），避免把"正常安静"误报成告警；可用性由 health_check 兜底
            "min_records_per_run": 0,
            "note": "美联储官方新闻稿 RSS（公开、稳定）；宏观政策类新闻的权威来源",
        },
    ),
    SourceSeed(
        name="fred_blog",
        source_type=SourceType.NEWS,
        base_url="https://fredblog.stlouisfed.org",
        timezone="UTC",
        config_json={
            "collector": "rss_collector",
            "sources_path": "config/rss_sources.json",
            "source_ids": ["fred_blog"],
            "interval_minutes": 1440,
            "min_records_per_run": 0,
            "note": "FRED Blog 官方公开 RSS；W0-3 新闻回填与宏观背景源",
        },
    ),
    SourceSeed(
        name="manual-汇通网",
        source_type=SourceType.NEWS,
        base_url="https://gold.fx678.com",
        timezone="Asia/Shanghai",
        enabled=False,
        config_json={
            "collector": "manual_import",
            "min_records_per_run": 0,
            "note": "人工整理的历史中文黄金快讯；只允许本地显式导入，不自动抓取",
        },
    ),
    SourceSeed(
        name="manual-华尔街见闻",
        source_type=SourceType.NEWS,
        base_url="https://wallstreetcn.com",
        timezone="Asia/Shanghai",
        enabled=False,
        config_json={
            "collector": "manual_import",
            "min_records_per_run": 0,
            "note": "人工整理的历史中文黄金快讯；只允许本地显式导入，不自动抓取",
        },
    ),
    SourceSeed(
        name="jin10_flash",
        source_type=SourceType.NEWS,
        base_url="https://www.jin10.com",
        timezone="Asia/Shanghai",
        config_json={
            "collector": "news_collector",
            "kind": "flash",
            "feeds": [],
            "interval_minutes": 30,
            "min_records_per_run": 5,
            # 快讯为高频源：30 分钟少于 5 条通常意味着 feed 异常；
            # 请填写经授权的公开 RSS/Atom 地址
            "note": "快讯为高频源（阈值 5/30min）；需填写经授权的公开 RSS/Atom 地址",
        },
    ),
    SourceSeed(
        name="sina_finance_gold",
        source_type=SourceType.NEWS,
        base_url="https://finance.sina.com.cn",
        timezone="Asia/Shanghai",
        config_json={
            "collector": "news_collector",
            "section": "gold",
            "feeds": [],
            "interval_minutes": 30,
            "min_records_per_run": 1,
            "note": "请填写经授权的公开 RSS/Atom 地址；留空时该来源不会运行（运行期给出明确错误）",
        },
    ),
    SourceSeed(
        name="investing_news",
        source_type=SourceType.NEWS,
        base_url="https://www.investing.com/news",
        timezone="UTC",
        config_json={
            "collector": "news_collector",
            "commodities_only": True,
            "feeds": [],
            "interval_minutes": 30,
            "min_records_per_run": 1,
            "note": "请填写经授权的公开 RSS/Atom 地址；留空时该来源不会运行（运行期给出明确错误）",
        },
    ),
    SourceSeed(
        name="fred_macro",
        source_type=SourceType.MACRO,
        base_url="https://api.stlouisfed.org",
        timezone="UTC",
        config_json={
            "collector": "macro_collector",
            "provider": "fred",
            # 密钥只从环境变量读取：绝不写入源码、配置或数据库（团队批复）
            "api_key_env": "FRED_API_KEY",
            "series": [dict(series) for series in DEFAULT_W0_2_SERIES],
            # 宏观观测按日/月/季发布，30 分钟窗口内通常没有新观测 → 用回看窗口复采（幂等）
            "lookback_days": 45,
            "min_records_per_run": 1,
            "frequency_note": "保持 provider 原始粒度（日/月/季），不重采样、不拉平",
            "interval_minutes": 30,
            "note": "FRED 官方公开 API（免费注册后配置环境变量 FRED_API_KEY）；严禁收费站点爬取",
        },
    ),
    SourceSeed(
        name="econ_calendar_investing",
        source_type=SourceType.MACRO,
        base_url="https://www.investing.com/economic-calendar",
        timezone="UTC",
        enabled=False,
        config_json={
            "collector": "econ_calendar_collector",
            "event_codes": ["CPI", "PCE", "NFP", "FOMC"],
            "interval_minutes": 30,
            "min_records_per_run": 0,
            "note": "经济日历采集器待实现（Step 3 后续）；启用前会因未注册而报错，故暂置 false",
        },
    ),
    SourceSeed(
        name="market_yahoo",
        source_type=SourceType.MARKET,
        base_url="https://query1.finance.yahoo.com",
        timezone="UTC",
        config_json={
            "collector": "market_collector",
            "provider": "yahoo_chart",
            "symbols": ["XAUUSD", "DXY", "US10Y", "USDCNY"],
            # 项目标的 → provider ticker（**实测校准，2026-09-15**）：
            #   XAUUSD → GC=F（COMEX 黄金连续合约；Yahoo 无 XAUUSD）
            #   DXY    → DX-Y.NYB（ICE 美元指数；DX=F 已失效，裸 DXY 返回 0 根 bar）
            #   USDCNY → CNY=X
            #   US10Y  → ^TNX（CBOE 10 年期收益率指数）
            # 注意：`US10Y_REAL`（实际利率）Yahoo **没有**序列 → 由 FRED `DFII10` 提供（W0-2）。
            "provider_symbols": {
                "XAUUSD": "GC=F",
                "DXY": "DX-Y.NYB",
                "USDCNY": "CNY=X",
                "US10Y": "^TNX",
            },
            "timeframes": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
            "interval_minutes": 30,
            # 行情条数由"窗口 ÷ 周期"自动推断（MarketCollector 覆盖 _expected_min_records），
            # 这里的 0 表示不再额外叠加固定阈值
            "min_records_per_run": 0,
            "note": "4h 需客户端聚合：market_collector 会跳过并打印告警（provider 无 4h）",
        },
    ),
    SourceSeed(
        name="market_stooq_backup",
        source_type=SourceType.MARKET,
        base_url="https://stooq.com",
        timezone="UTC",
        enabled=False,
        config_json={
            "collector": "market_collector",
            "role": "backup",
            "min_records_per_run": 0,
            "note": "备用行情源：主源不可用时由运维显式启用（enabled=False 不代表功能缺失）",
        },
    ),
    SourceSeed(
        name="market_dukascopy_xauusd",
        source_type=SourceType.MARKET,
        base_url="https://datafeed.dukascopy.com/datafeed",
        timezone="UTC",
        enabled=False,
        config_json={
            "collector": "dukascopy_xauusd_backfill",
            "role": "historical_backfill",
            "provider": "dukascopy_bi5",
            "provider_symbol": "XAUUSD",
            "instrument_symbol": "XAUUSD_DUKASCOPY",
            "raw_timeframe": "1m_bid",
            "output_timeframe": "1h",
            "price_scale": 1000,
            "min_records_per_run": 0,
            "note": "独立现货研究序列；不得与 Yahoo GC=F 期货代理逐根拼接",
        },
    ),
)
