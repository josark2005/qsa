from __future__ import annotations

from datetime import date, timedelta

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.trading_calendar import TradingCalendar
from app.schemas.screening import ConditionIn, ScreenRequest
from app.screening.query_builder import build_screen

TODAY = date(2026, 11, 4)


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'low.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _seed(db) -> None:
    for offset in range(0, 70):
        day = TODAY - timedelta(days=offset)
        if day.weekday() < 5:
            db.add(
                TradingCalendar(
                    exchange="CN", cal_date=day, is_open=True, source="test"
                )
            )
    short = Instrument(
        symbol="301999.SZ",
        code="301999",
        exchange="SZ",
        name="次新测试",
        asset_type="stock",
        board="gem",
        status="active",
        list_date=TODAY - timedelta(days=30),
        source="test",
    )
    mature = Instrument(
        symbol="600000.SH",
        code="600000",
        exchange="SH",
        name="老股测试",
        asset_type="stock",
        board="main",
        status="active",
        list_date=date(2010, 1, 1),
        source="test",
    )
    db.add_all([short, mature])
    db.flush()
    # 次新股只有 10 根日线：最新收盘比窗口最低点高 5%（不在低点）。
    for offset in range(10):
        close = 10.0 if offset > 0 else 10.5
        db.add(
            DailyBar(
                instrument_id=short.id,
                trade_date=TODAY - timedelta(days=offset),
                open=close,
                high=close,
                low=close,
                close=close,
                close_adj=close,
                volume=100,
                source="test",
            )
        )
    # 老股 60 根日线：前 59 根 10.0，最新收盘 9.8 正好是窗口最低点。
    for offset in range(60):
        close = 9.8 if offset == 0 else 10.0
        db.add(
            DailyBar(
                instrument_id=mature.id,
                trade_date=TODAY - timedelta(days=offset),
                open=close,
                high=close,
                low=close,
                close=close,
                close_adj=close,
                volume=100,
                source="test",
            )
        )
    db.commit()


def _symbols(tmp_path, condition: ConditionIn) -> set[str]:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            _seed(db)
            result = build_screen(
                db,
                ScreenRequest(
                    as_of=TODAY, logic="and", conditions=[condition], sort=[]
                ),
                max_window_days=2500,
                statement_timeout_ms=5000,
            )
            return {row["symbol"] for row in result["rows"]}
    finally:
        engine.dispose()


def test_price_from_low_uses_available_history_for_new_listings(tmp_path) -> None:
    """历史不足窗口天数时，按上市以来的全部日线判断，不应一律判空。"""

    matched = _symbols(
        tmp_path,
        ConditionIn(
            field="price_from_low_pct",
            op="lte",
            value=0.3,
            params={"days": 750, "min_obs_ratio": 0.8},
        ),
    )
    # 次新股历史不足 750 日时按上市以来数据判断，不再一律判空。
    assert "301999.SZ" in matched


def test_price_from_low_uses_short_window_within_calendar(tmp_path) -> None:
    """窗口只有 40 个日历日时，老股仍按现有数据（38 根）判断，不受原来 60 行门槛影响。"""

    matched = _symbols(
        tmp_path,
        ConditionIn(
            field="price_from_low_pct",
            op="lte",
            value=0.3,
            params={"days": 60, "min_obs_ratio": 0.8},
        ),
    )
    assert "600000.SH" in matched
    assert "301999.SZ" in matched


def test_price_from_low_still_filters_high_above_low(tmp_path) -> None:
    matched = _symbols(
        tmp_path,
        ConditionIn(
            field="price_from_low_pct",
            op="lte",
            value=0.01,
            params={"days": 750, "min_obs_ratio": 0.8},
        ),
    )
    # 次新股距离低点还有 5%，不应命中。
    assert "301999.SZ" not in matched
