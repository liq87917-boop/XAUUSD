"""Phase 1 标的白名单种子（严格遵循 04_数据表结构及字段定义 第 12 节 instruments）。

字段映射（04 §12）：
    symbol / name / asset_class / quote_currency / timezone / enabled
    （id、created_at、updated_at 由 ORM 自动生成）

范围依据 05_分阶段开发路线图 Phase 1.5：
    首期至少 XAUUSD、DXY、美债收益率、USD/CNY；
    可追加 COMEX Gold、Silver、Oil、BTC —— 全部通过字典表扩展，结构不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from database.models.enums import AssetClass

__all__ = ["INSTRUMENT_SEEDS", "REQUIRED_PHASE1_SYMBOLS", "InstrumentSeed"]


@dataclass(frozen=True, slots=True)
class InstrumentSeed:
    """instruments 表的一行定义。"""

    symbol: str
    name: str
    asset_class: AssetClass
    quote_currency: str
    timezone: str
    enabled: bool = True


#: Phase 1 首期必须具备的标的（05 §5 明确列出的四项）
REQUIRED_PHASE1_SYMBOLS: Final[tuple[str, ...]] = ("XAUUSD", "DXY", "US10Y", "USDCNY")

#: 种子定义（幂等：以 symbol 为自然键，已存在则跳过）
INSTRUMENT_SEEDS: Final[tuple[InstrumentSeed, ...]] = (
    InstrumentSeed(
        symbol="XAUUSD",
        name="黄金/美元 现货",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="XAUUSD_DUKASCOPY",
        name="黄金/美元现货（Dukascopy 独立研究序列）",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="XAGUSD",
        name="白银/美元 现货",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="COMEX_GC",
        name="COMEX 黄金期货主力合约",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="SGE_AU9999",
        name="上海黄金交易所 Au99.99",
        asset_class=AssetClass.METAL,
        quote_currency="CNY",
        timezone="Asia/Shanghai",
    ),
    InstrumentSeed(
        symbol="DXY",
        name="美元指数 DXY",
        asset_class=AssetClass.INDEX,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="US10Y",
        name="美国10年期国债收益率",
        asset_class=AssetClass.BOND,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="US02Y",
        name="美国2年期国债收益率",
        asset_class=AssetClass.BOND,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="US10Y_REAL",
        name="美国10年期实际利率（TIPS）",
        asset_class=AssetClass.BOND,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="USDCNY",
        name="美元/人民币",
        asset_class=AssetClass.FX,
        quote_currency="CNY",
        timezone="Asia/Shanghai",
    ),
    InstrumentSeed(
        symbol="WTI",
        name="WTI 原油",
        asset_class=AssetClass.ENERGY,
        quote_currency="USD",
        timezone="UTC",
    ),
    InstrumentSeed(
        symbol="BTCUSD",
        name="BTC/美元",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        timezone="UTC",
    ),
)
