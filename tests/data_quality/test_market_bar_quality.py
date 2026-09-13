"""行情数据质量测试（08 §4 Market Bar 验收项 / 03 §17 数据质量规则）。

底线：OHLC 关系、价格 > 0、K 线时间不重复、时间语义正确 —— 全部在数据库层强制，
任何写入路径（ORM / 原生 SQL / 采集器）都无法绕过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from database.models import AssetClass, Instrument, MarketBar, Timeframe
from database.models.enums import SourceType
from src.common.time import utc_now

pytestmark = pytest.mark.data_quality

OPEN_TIME = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
CLOSE_TIME = OPEN_TIME + timedelta(hours=1)


@pytest.fixture()
def instrument(session) -> Instrument:
    obj = Instrument(
        symbol="XAUUSD",
        name="Gold Spot",
        asset_class=AssetClass.METAL,
        quote_currency="USD",
        timezone="UTC",
    )
    session.add(obj)
    session.flush()
    return obj


@pytest.fixture()
def make_bar(session, instrument: Instrument):
    def _make_bar(**overrides: object) -> MarketBar:
        collected_at = overrides.pop("collected_at", utc_now())
        assert isinstance(collected_at, datetime)
        defaults: dict[str, object] = {
            "instrument": instrument,
            "timeframe": Timeframe.H1,
            "open_time": OPEN_TIME,
            "close_time": CLOSE_TIME,
            "open": Decimal("2400.00000000"),
            "high": Decimal("2410.00000000"),
            "low": Decimal("2395.00000000"),
            "close": Decimal("2405.00000000"),
            "volume": Decimal("1234.50000000"),
            "collected_at": collected_at,
            "effective_at": collected_at,
        }
        defaults.update(overrides)
        bar = MarketBar(**defaults)  # type: ignore[arg-type]
        session.add(bar)
        session.flush()
        return bar

    return _make_bar


def test_valid_bar_is_accepted_and_keeps_decimal_precision(session, make_bar) -> None:
    bar = make_bar()
    session.expunge(bar)
    loaded = session.get(MarketBar, bar.id)
    assert loaded is not None
    assert loaded.high == Decimal("2410.00000000")
    # SQLite 不保存时区偏移（本地测试库的固有限制）；生产/研究库为 PostgreSQL
    # TIMESTAMPTZ，会返回 timezone-aware 时间。这里断言"存储的 UTC 时刻一致"，
    # 而"列必须是 timezone-aware"由 tests/integration/test_phase1_schema.py 基于
    # ORM 元数据强制校验。
    assert loaded.open_time.replace(tzinfo=UTC) == OPEN_TIME
    assert loaded.timeframe == Timeframe.H1


def test_high_below_close_is_rejected(session, make_bar) -> None:
    with pytest.raises(IntegrityError):
        make_bar(high=Decimal("2401"), close=Decimal("2405"))
    session.rollback()


def test_low_above_open_is_rejected(session, make_bar) -> None:
    with pytest.raises(IntegrityError):
        make_bar(low=Decimal("2401"), open=Decimal("2400"))
    session.rollback()


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_non_positive_price_is_rejected(session, make_bar, field: str) -> None:
    with pytest.raises(IntegrityError):
        make_bar(**{field: Decimal("0")})
    session.rollback()


def test_close_time_must_be_after_open_time(session, make_bar) -> None:
    with pytest.raises(IntegrityError):
        make_bar(close_time=OPEN_TIME)
    session.rollback()


def test_negative_volume_is_rejected(session, make_bar) -> None:
    with pytest.raises(IntegrityError):
        make_bar(volume=Decimal("-1"))
    session.rollback()


def test_effective_at_cannot_precede_collected_at(session, make_bar) -> None:
    collected_at = utc_now()
    with pytest.raises(IntegrityError):
        make_bar(collected_at=collected_at, effective_at=collected_at - timedelta(seconds=1))
    session.rollback()


def test_duplicate_bar_is_rejected(session, instrument, make_bar) -> None:
    """同一 instrument + timeframe + open_time 不得重复（K 线时间不重复）。"""
    make_bar()
    with pytest.raises(IntegrityError):
        session.add(
            MarketBar(
                instrument=instrument,
                timeframe=Timeframe.H1,
                open_time=OPEN_TIME,
                close_time=CLOSE_TIME,
                open=Decimal("2400"),
                high=Decimal("2410"),
                low=Decimal("2395"),
                close=Decimal("2405"),
                collected_at=utc_now(),
                effective_at=utc_now(),
            )
        )
        session.flush()
    session.rollback()


def test_unknown_timeframe_is_rejected_by_database_check(session, instrument) -> None:
    """绕过 ORM 直接写库时，数据库 CHECK 仍必须拦截非法 timeframe。"""
    now = utc_now()
    session.add(
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            open_time=OPEN_TIME,
            close_time=CLOSE_TIME,
            open=Decimal("2400"),
            high=Decimal("2410"),
            low=Decimal("2395"),
            close=Decimal("2405"),
            collected_at=now,
            effective_at=now,
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE market_bars SET timeframe = '2h'"))
    session.rollback()


def test_instrument_symbol_is_unique(session, instrument: Instrument) -> None:
    session.add(
        Instrument(
            symbol=instrument.symbol,
            asset_class=AssetClass.METAL,
            quote_currency="USD",
            timezone="UTC",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_bar_timeframe_values_follow_documented_set() -> None:
    assert {tf.value for tf in Timeframe} == {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def test_instrument_symbol_length_limit() -> None:
    """symbol 长度受 VARCHAR(50) 限制（防止误存超长串导致隐性截断）。"""
    assert Instrument.__table__.c.symbol.type.length == 50


# ---------------------------------------------------------------------------
# 唯一性保证（团队批复：绝对无法插入重复行情）
# ---------------------------------------------------------------------------
def test_market_bar_uniqueness_indexes_are_declared() -> None:
    """ORM 元数据必须同时包含：按来源的唯一索引 + 更严格的跨来源唯一约束。"""
    indexes = {index.name: bool(index.unique) for index in MarketBar.__table__.indexes}
    assert indexes.get("uq_market_bars_source_instrument_timeframe_open") is True

    unique_constraints = {
        constraint.name
        for constraint in MarketBar.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_market_bars_instrument_timeframe_open" in unique_constraints


def test_duplicate_bar_from_another_source_is_still_rejected(
    session, instrument: Instrument, make_bar, make_source
) -> None:
    """★ 绝对无法插入重复行情：即使换一个来源，同一根 K 线也必须被拒绝。"""
    make_bar()
    second_source = make_source(name="second-market-source", source_type=SourceType.MARKET)

    with pytest.raises(IntegrityError):
        make_bar(source_id=second_source.id)
    session.rollback()
