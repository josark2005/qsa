from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.limit_event import LimitEvent
from app.models.trading_calendar import TradingCalendar
from app.schemas.screening import ConditionIn, ScreenRequest
from app.screening.query_builder import build_screen
from app.screening.registry import get_factor, normalize_params

TODAY = date(2026, 11, 4)


_DB_COUNTER = itertools.count()


def _factory(tmp_path):
    # 每个用例用独立数据库文件，避免同一条用例内多次调用时重复建表数据。
    path = tmp_path / f"shape-{next(_DB_COUNTER)}.db"
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _seed(db) -> None:
    for offset in range(0, 40):
        day = TODAY - timedelta(days=offset)
        if day.weekday() < 5:
            db.add(
                TradingCalendar(
                    exchange="CN", cal_date=day, is_open=True, source="test"
                )
            )
    high_open_low_close = Instrument(
        symbol="600000.SH",
        code="600000",
        exchange="SH",
        name="高开低走",
        asset_type="stock",
        board="main",
        status="active",
        source="test",
    )
    low_open_high_close = Instrument(
        symbol="000001.SZ",
        code="000001",
        exchange="SZ",
        name="低开高走",
        asset_type="stock",
        board="main",
        status="active",
        source="test",
    )
    db.add_all([high_open_low_close, low_open_high_close])
    db.flush()

    # 前 6 个交易日：成交量 100，平开平收。
    for instrument in (high_open_low_close, low_open_high_close):
        for offset in range(1, 7):
            db.add(
                DailyBar(
                    instrument_id=instrument.id,
                    trade_date=TODAY - timedelta(days=offset),
                    open=10.0,
                    high=10.2,
                    low=9.8,
                    close=9.9,
                    preclose=10.0,
                    close_adj=9.9,
                    volume=100,
                    source="test",
                )
            )
    # 当日：600000 高开低走 + 涨停放量（10 倍），000001 低开高走 + 涨停缩量。
    db.add(
        DailyBar(
            instrument_id=high_open_low_close.id,
            trade_date=TODAY,
            open=11.5,
            high=11.6,
            low=10.9,
            close=11.0,
            preclose=11.0,
            close_adj=11.0,
            volume=1000,
            source="test",
        )
    )
    db.add(
        DailyBar(
            instrument_id=low_open_high_close.id,
            trade_date=TODAY,
            open=9.0,
            high=11.2,
            low=8.9,
            close=11.0,
            preclose=10.0,
            close_adj=11.0,
            volume=60,
            source="test",
        )
    )
    for instrument in (high_open_low_close, low_open_high_close):
        db.add(
            LimitEvent(
                instrument_id=instrument.id,
                trade_date=TODAY,
                direction="up",
                is_close_limit=True,
                is_touched=True,
                is_one_word=False,
                source="test",
            )
        )
    db.commit()


def _match(tmp_path, *conditions: ConditionIn) -> set[str]:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            _seed(db)
            result = build_screen(
                db,
                ScreenRequest(as_of=TODAY, logic="and", conditions=list(conditions), sort=[]),
                max_window_days=2500,
                statement_timeout_ms=5000,
            )
            return {row["symbol"] for row in result["rows"]}
    finally:
        engine.dispose()


def test_open_gap_patterns_split_high_and_low_open(tmp_path) -> None:
    assert _match(
        tmp_path,
        ConditionIn(field="open_gap_low_close", op="eq", value=True, params={}),
    ) == {"600000.SH"}
    assert _match(
        tmp_path,
        ConditionIn(field="open_gap_high_close", op="eq", value=True, params={}),
    ) == {"000001.SZ"}
    assert _match(
        tmp_path,
        ConditionIn(field="open_gap_up", op="eq", value=True, params={}),
    ) == {"600000.SH"}
    assert _match(
        tmp_path,
        ConditionIn(field="open_gap_down", op="eq", value=True, params={}),
    ) == {"000001.SZ"}


def test_open_gap_ratios(tmp_path) -> None:
    high_open = _match(
        tmp_path,
        ConditionIn(field="open_gap_pct", op="gte", value=4, params={}),
    )
    assert high_open == {"600000.SH"}
    intraday_down = _match(
        tmp_path,
        ConditionIn(field="intraday_change_pct", op="lte", value=-4, params={}),
    )
    assert intraday_down == {"600000.SH"}


def test_limit_up_volume_expand_requires_volume_growth(tmp_path) -> None:
    base = {"days": 20, "mode": "close", "base_days": 5}
    assert _match(
        tmp_path,
        ConditionIn(field="limit_up_is_volume_expand", op="eq", value=True, params=base),
    ) == {"600000.SH"}
    assert _match(
        tmp_path,
        ConditionIn(field="limit_up_is_volume_expand", op="ne", value=True, params=base),
    ) == {"000001.SZ"}
    assert _match(
        tmp_path,
        ConditionIn(field="limit_up_volume_ratio_min", op="gte", value=5, params=base),
    ) == {"600000.SH"}


def test_volume_factor_params_are_validated() -> None:
    factor = get_factor("limit_up_is_volume_expand")
    params = normalize_params(factor, {"days": 20, "mode": "close", "base_days": 5})
    assert params["base_days"] == 5
    with pytest.raises(Exception):
        normalize_params(factor, {"days": 20, "mode": "close", "base_days": 1})
