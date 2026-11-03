from __future__ import annotations

import pandas as pd

from app.analytics.indicators import _add_indicators


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
