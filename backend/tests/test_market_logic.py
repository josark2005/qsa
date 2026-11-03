from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.core.db import Base, make_engine, make_session_factory
from app.datasources.symbols import (
    board_for_code,
    canonical_symbol,
    to_baostock_symbol,
    to_tencent_symbol,
)
from app.datasources.tencent import _parse_line
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.limit_event import LimitEvent
from app.models.trading_calendar import TradingCalendar
from app.sync.bars import (
    backfill_limit_prices,
    compute_limit_prices,
    recompute_limit_events,
)
from app.sync.financials import quarter_ends


def test_symbol_mapping() -> None:
    assert canonical_symbol("600000") == "600000.SH"
    assert canonical_symbol("300750") == "300750.SZ"
    assert canonical_symbol("920000") == "920000.BJ"
    assert to_baostock_symbol("600000.SH") == "sh.600000"
    assert to_tencent_symbol("920000.BJ") == "bj920000"
    assert board_for_code("688981") == "star"
    assert board_for_code("300750") == "gem"
    assert board_for_code("920000") == "bse"


def test_limit_prices_main_and_star() -> None:
    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="SH",
        board="main",
        is_st=False,
        preclose=10.0,
        trading_day_number=100,
    )
    assert up == 11.0
    assert down == 9.0

    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="SZ",
        board="gem",
        is_st=False,
        preclose=10.0,
        trading_day_number=100,
    )
    assert up == 12.0
    assert down == 8.0


def test_limit_prices_new_stock_and_st() -> None:
    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="SH",
        board="main",
        is_st=False,
        preclose=10.0,
        trading_day_number=1,
    )
    assert up == 14.4
    assert down == 6.4

    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="SZ",
        board="main",
        is_st=True,
        preclose=10.0,
        trading_day_number=100,
    )
    assert up == 10.5
    assert down == 9.5

    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="SZ",
        board="gem",
        is_st=False,
        preclose=10.0,
        trading_day_number=3,
    )
    assert up is None and down is None


def test_limit_prices_bse_floor() -> None:
    up, down = compute_limit_prices(
        asset_type="stock",
        exchange="BJ",
        board="bse",
        is_st=False,
        preclose=10.76,
        trading_day_number=100,
    )
    assert up == 13.98
    assert down == 7.53


def test_limit_prices_etf_none() -> None:
    up, down = compute_limit_prices(
        asset_type="etf",
        exchange="SH",
        board=None,
        is_st=False,
        preclose=4.0,
        trading_day_number=100,
    )
    assert up is None and down is None


def test_parse_tencent_line() -> None:
    line = (
        'v_sh600000="1~浦发银行~600000~10.08~10.02~9.98~141912~79839~61861~'
        '10.07~209~10.06~720~10.05~417~10.04~638~10.03~1369~10.08~34~'
        '10.09~3873~10.10~3624~10.11~1080~10.12~2050~~20261102093925~'
        '0.06~0.60~10.09~9.93~10.08/141912/142205912~141912~14221~0.04~'
        '6.48~~10.09~9.93~1.60~3357.23~3357.23~0.44~11.02~9.02~4.66~'
        '-7308~10.02~6.21~6.71~~~-0.01~14220.5912~0.0000~0~   A~GP-A~";'
    )
    row = _parse_line(line)
    assert row is not None
    assert row["symbol"] == "600000.SH"
    assert row["preclose"] == "10.02"
    assert row["up_limit"] == "11.02"
    assert row["down_limit"] == "9.02"


def test_quarter_ends_january() -> None:
    ends = quarter_ends(date(2026, 1, 15), 3)
    assert ends == [
        date(2025, 12, 31),
        date(2025, 9, 30),
        date(2025, 6, 30),
    ]


def test_backfill_limit_prices_and_events(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'bars.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    trade_date = date(2026, 10, 30)
    with factory() as db:
        instrument = Instrument(
            symbol="603233.SH",
            code="603233",
            exchange="SH",
            name="大参林",
            asset_type="stock",
            board="main",
            list_date=date(2020, 1, 2),
            source="test",
        )
        db.add(instrument)
        db.add(
            TradingCalendar(
                exchange="CN",
                cal_date=date(2026, 10, 29),
                is_open=True,
                source="test",
            )
        )
        db.add(
            TradingCalendar(
                exchange="CN",
                cal_date=trade_date,
                is_open=True,
                source="test",
            )
        )
        db.commit()
        db.add(
            DailyBar(
                instrument_id=instrument.id,
                trade_date=trade_date,
                preclose=18.42,
                open=18.54,
                high=20.26,
                low=18.35,
                close=20.26,
                volume=23_947_044,
                source="sina_verified",
            )
        )
        db.commit()

        assert backfill_limit_prices(db, trade_date, trade_date) == 1
        bar = db.get(DailyBar, (instrument.id, trade_date))
        assert float(bar.up_limit) == 20.26
        assert float(bar.down_limit) == 16.58

        assert recompute_limit_events(db, trade_date, trade_date) == 1
        event = db.scalar(
            select(LimitEvent).where(
                LimitEvent.instrument_id == instrument.id,
                LimitEvent.trade_date == trade_date,
            )
        )
        assert event is not None
        assert event.direction == "up"
        assert event.is_close_limit is True
        assert event.is_touched is True
    engine.dispose()
