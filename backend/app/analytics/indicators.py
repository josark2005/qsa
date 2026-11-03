from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument

logger = logging.getLogger(__name__)

INDICATOR_PARAMS = {
    "ma": [5, 10, 20],
    "macd": [12, 26, 9],
    "kdj": [9, 3, 3],
    "expma": [20, 50],
    "rsi": [6, 12, 24],
}

PERIOD_WINDOWS = {"daily": 180, "weekly": 120, "monthly": 72}
PERIOD_FREQ = {"weekly": "W-FRI", "monthly": "M"}

DAILY_SQL_COLUMNS = (
    DailyBar.trade_date,
    DailyBar.open,
    DailyBar.high,
    DailyBar.low,
    DailyBar.close,
    DailyBar.adj_factor,
    DailyBar.volume,
)


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)

    for window in INDICATOR_PARAMS["ma"]:
        frame[f"ma{window}"] = close.rolling(window, min_periods=1).mean()

    fast, slow, signal = INDICATOR_PARAMS["macd"]
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    frame["macd_dif"] = dif
    frame["macd_dea"] = dea
    frame["macd_hist"] = (dif - dea) * 2

    for window in INDICATOR_PARAMS["expma"]:
        frame[f"expma{window}"] = _ema(close, window)

    kdj_n, kdj_k, kdj_d = INDICATOR_PARAMS["kdj"]
    lowest = low.rolling(kdj_n, min_periods=1).min()
    highest = high.rolling(kdj_n, min_periods=1).max()
    denominator = (highest - lowest).replace(0, np.nan)
    rsv = ((close - lowest) / denominator * 100).fillna(50.0)
    k_value = _sma(rsv, kdj_k)
    d_value = _sma(k_value, kdj_d)
    frame["kdj_k"] = k_value
    frame["kdj_d"] = d_value
    frame["kdj_j"] = 3 * k_value - 2 * d_value

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    for window in INDICATOR_PARAMS["rsi"]:
        avg_gain = _sma(gain, window)
        avg_loss = _sma(loss, window)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
        frame[f"rsi{window}"] = rsi

    return frame


def _aggregate(frame: pd.DataFrame, freq: str) -> pd.DataFrame:
    periods = frame.index.to_period(freq)
    groups = frame.groupby(periods, sort=True)
    aggregated = groups.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    aggregated = aggregated.dropna(subset=["close"])
    last_dates = frame.index.to_series().groupby(periods).last()
    aggregated["trade_date"] = last_dates.reindex(aggregated.index)
    aggregated = aggregated.set_index("trade_date").sort_index()
    aggregated.index = pd.to_datetime(aggregated.index)
    return aggregated


def _number(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _series(values: pd.Series) -> list[float | None]:
    return [_number(value) for value in values.tolist()]


def _latest(frame: pd.DataFrame) -> dict[str, Any]:
    row = frame.iloc[-1]
    previous_close = frame["close"].iloc[-2] if len(frame) > 1 else None
    pct_chg = None
    if previous_close not in (None, 0) and not pd.isna(previous_close):
        pct_chg = (float(row["close"]) / float(previous_close) - 1) * 100
    return {
        "trade_date": frame.index[-1].date().isoformat(),
        "close": _number(row["close"]),
        "pct_chg": _number(pct_chg, 2),
        "ma5": _number(row.get("ma5")),
        "ma10": _number(row.get("ma10")),
        "ma20": _number(row.get("ma20")),
        "expma20": _number(row.get("expma20")),
        "expma50": _number(row.get("expma50")),
        "macd": {
            "dif": _number(row.get("macd_dif")),
            "dea": _number(row.get("macd_dea")),
            "hist": _number(row.get("macd_hist")),
        },
        "kdj": {
            "k": _number(row.get("kdj_k"), 2),
            "d": _number(row.get("kdj_d"), 2),
            "j": _number(row.get("kdj_j"), 2),
        },
        "rsi": {
            "rsi6": _number(row.get("rsi6"), 2),
            "rsi12": _number(row.get("rsi12"), 2),
            "rsi24": _number(row.get("rsi24"), 2),
        },
    }


def _period_payload(frame: pd.DataFrame, period: str) -> dict[str, Any]:
    if frame.empty:
        return {
            "dates": [],
            "ohlc": [],
            "volume": [],
            "ma": {"ma5": [], "ma10": [], "ma20": []},
            "expma": {"expma20": [], "expma50": []},
            "macd": {"dif": [], "dea": [], "hist": []},
            "kdj": {"k": [], "d": [], "j": []},
            "rsi": {"rsi6": [], "rsi12": [], "rsi24": []},
            "latest": None,
            "bar_count": 0,
            "limited_history": True,
        }

    window = PERIOD_WINDOWS[period]
    sliced = frame.tail(window)
    return {
        "dates": [value.date().isoformat() for value in sliced.index],
        "ohlc": [
            [
                _number(row["open"]),
                _number(row["close"]),
                _number(row["low"]),
                _number(row["high"]),
            ]
            for _, row in sliced.iterrows()
        ],
        "volume": [_number(value, 0) or 0 for value in sliced["volume"].tolist()],
        "ma": {
            "ma5": _series(sliced["ma5"]),
            "ma10": _series(sliced["ma10"]),
            "ma20": _series(sliced["ma20"]),
        },
        "expma": {
            "expma20": _series(sliced["expma20"]),
            "expma50": _series(sliced["expma50"]),
        },
        "macd": {
            "dif": _series(sliced["macd_dif"]),
            "dea": _series(sliced["macd_dea"]),
            "hist": _series(sliced["macd_hist"]),
        },
        "kdj": {
            "k": _series(sliced["kdj_k"]),
            "d": _series(sliced["kdj_d"]),
            "j": _series(sliced["kdj_j"]),
        },
        "rsi": {
            "rsi6": _series(sliced["rsi6"]),
            "rsi12": _series(sliced["rsi12"]),
            "rsi24": _series(sliced["rsi24"]),
        },
        "latest": _latest(sliced),
        "bar_count": int(len(frame)),
        "limited_history": bool(len(frame) < 60 if period == "monthly" else len(frame) < 30),
    }


def _empty_payload(
    *,
    symbol: str,
    name: str,
    as_of: date,
    message: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "as_of": as_of.isoformat(),
        "periods": {
            period: _period_payload(pd.DataFrame(), period)
            for period in PERIOD_WINDOWS
        },
        "params": INDICATOR_PARAMS,
        "message": message,
    }


def _front_adjust_ratio(frame: pd.DataFrame, *, symbol: str) -> pd.Series:
    """返回前复权系数（每行 * 原始价格 = 展示价格），最后一行系数固定为 1。

    QSA 的 adj_factor 来自 BaoStock backAdjustFactor（后复权因子，随分红送转
    单调不减）。历史归档与腾讯/AkShare 增量数据混写时，可能出现最新一根 K 线
    的 adj_factor 回退（例如旧数据 2.42、新数据 1.0）；若直接按“最新因子”
    归一化，旧数据会被整体放大，制造出单日 -58% 的假跌。检测到因子序列回退
    时放弃复权（系数恒为 1），用原始价格展示，并记录一条 warning 便于排查。
    """
    factors = pd.to_numeric(frame["adj_factor"], errors="coerce")
    latest = factors.dropna()
    latest_value = float(latest.iloc[-1]) if not latest.empty else 1.0
    if latest_value <= 0:
        latest_value = 1.0
    normalized = factors.fillna(latest_value).replace(0, latest_value)
    diffs = normalized.diff().dropna()
    tolerance = max(abs(float(normalized.max())) * 1e-6, 1e-9)
    if (diffs < -tolerance).any():
        logger.warning(
            "adjustment factor series is inconsistent for %s "
            "(latest=%.4f, min=%.4f); using raw prices",
            symbol,
            latest_value,
            float(normalized.min()),
        )
        return pd.Series(1.0, index=frame.index)
    return normalized / latest_value


def build_technical_payload(
    db: Session,
    instrument: Instrument,
    as_of: date,
) -> dict[str, Any]:
    rows = db.execute(
        select(*DAILY_SQL_COLUMNS)
        .where(
            DailyBar.instrument_id == instrument.id,
            DailyBar.trade_date <= as_of,
        )
        .order_by(DailyBar.trade_date)
    ).mappings().all()
    if not rows:
        return _empty_payload(
            symbol=instrument.symbol,
            name=instrument.name,
            as_of=as_of,
            message="暂无日线数据",
        )

    frame = pd.DataFrame(rows)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"])
    for column in ("open", "high", "low", "close", "adj_factor", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["close"])
    frame = frame.drop_duplicates(subset=["trade_date"], keep="last")
    frame = frame.sort_values("trade_date")
    if frame.empty:
        return _empty_payload(
            symbol=instrument.symbol,
            name=instrument.name,
            as_of=as_of,
            message="暂无有效日线数据",
        )

    ratio = _front_adjust_ratio(frame, symbol=instrument.symbol)
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column] * ratio
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame["volume"] = frame["volume"].fillna(0)
    frame = frame.set_index("trade_date")

    daily = _add_indicators(frame.copy())
    weekly = _add_indicators(_aggregate(frame, PERIOD_FREQ["weekly"]))
    monthly = _add_indicators(_aggregate(frame, PERIOD_FREQ["monthly"]))

    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "as_of": frame.index[-1].date().isoformat(),
        "periods": {
            "daily": _period_payload(daily, "daily"),
            "weekly": _period_payload(weekly, "weekly"),
            "monthly": _period_payload(monthly, "monthly"),
        },
        "params": INDICATOR_PARAMS,
        "message": None,
    }
