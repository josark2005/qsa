from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.analytics.indicators import (
    _add_indicators,
    build_technical_payload,
    front_adjust_ratio,
)
from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument


def test_indicators_are_computed_for_all_periods() -> None:
    close = [10 + index * 0.1 for index in range(60)]
    frame = pd.DataFrame(
        {
            "open": close,
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": [1000] * len(close),
        },
        index=pd.date_range("2026-01-01", periods=len(close), freq="D"),
    )
    result = _add_indicators(frame)

    expected = {
        "ma5",
        "ma10",
        "ma20",
        "expma20",
        "expma50",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "rsi6",
        "rsi12",
        "rsi24",
    }
    assert expected <= set(result.columns)
    assert result["ma5"].iloc[-1] == 15.7
    assert result["macd_hist"].iloc[-1] > 0
    assert 0 <= result["rsi6"].iloc[-1] <= 100
    assert 0 <= result["kdj_k"].iloc[-1] <= 100


def _seeded_payload(tmp_path, *, factors: list[float]):
    engine = make_engine(f"sqlite:///{(tmp_path / 'technical.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        instrument = Instrument(
            symbol="600299.SH",
            code="600299",
            exchange="SH",
            name="安迪苏",
            asset_type="stock",
            board="main",
            status="active",
            is_st=False,
            source="test",
        )
        db.add(instrument)
        db.flush()
        start = date(2026, 9, 20)
        closes = [8.09] * (len(factors) - 1) + [8.49]
        for index, factor in enumerate(factors):
            close = closes[index]
            db.add(
                DailyBar(
                    instrument_id=instrument.id,
                    trade_date=start + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    preclose=8.09,
                    close_adj=close * factor,
                    volume=1000,
                    adj_factor=factor,
                    pct_chg=4.94 if index == len(factors) - 1 else 0.0,
                    is_suspended=False,
                    is_st=False,
                    source="test",
                )
            )
        db.commit()
        payload = build_technical_payload(db, instrument, date(2026, 11, 3))
    engine.dispose()
    return payload


def test_front_adjust_falls_back_when_factors_regress(tmp_path) -> None:
    # 历史归档（2.42）与增量数据（1.0）混写：最新因子回退时不能放大旧数据。
    payload = _seeded_payload(tmp_path, factors=[2.42] * 29 + [1.0])
    daily = payload["periods"]["daily"]
    closes = [row[1] for row in daily["ohlc"]]
    assert max(closes) < 9.0, closes
    assert daily["latest"]["close"] == 8.49
    assert daily["latest"]["pct_chg"] == 4.94
    assert daily["bar_count"] == 30


def test_front_adjust_still_applies_for_consistent_factors() -> None:
    frame = pd.DataFrame(
        {
            "close": [10.0, 10.0, 10.0],
            "adj_factor": [2.0, 2.0, 2.42],
        }
    )
    ratio = front_adjust_ratio(frame, symbol="600299.SH")
    assert round(float(ratio.iloc[0]), 6) == round(2.0 / 2.42, 6)
    assert float(ratio.iloc[-1]) == 1.0
