from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.indicators import front_adjust_ratio
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport

THUMBNAIL_BAR_LIMIT = 30
THUMBNAIL_MAX_SYMBOLS = 60

_OHLC_KEYS = ("open", "high", "low", "close")


def report_row_symbols(report: ScreenReport) -> list[str]:
    """按报表行顺序返回其中的标的代码（已去重）。"""
    symbols: list[str] = []
    seen: set[str] = set()
    for row in report.rows or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not symbol:
            continue
        text = str(symbol)
        if text in seen:
            continue
        seen.add(text)
        symbols.append(text)
    return symbols


def _factor_ratios(
    db: Session,
    instruments: dict[str, int],
    as_of: date,
) -> dict[int, pd.Series]:
    """按详情图相同的口径计算前复权系数。

    详情图（build_technical_payload）用整段 adj_factor 历史归一化，并在检测到
    因子回退（历史归档与增量数据混写）时退回原始价格；走势缩略图必须复用同一
    规则，否则缩略图会按每根 K 线自己的后复权因子缩放，出现详情图里没有的假跳空。
    """

    if not instruments:
        return {}
    symbols = {instrument_id: symbol for symbol, instrument_id in instruments.items()}
    factors: dict[int, list[Any]] = {instrument_id: [] for instrument_id in symbols}
    dates: dict[int, list[date]] = {instrument_id: [] for instrument_id in symbols}
    rows = db.execute(
        select(
            DailyBar.instrument_id,
            DailyBar.trade_date,
            DailyBar.adj_factor,
        )
        .where(
            DailyBar.instrument_id.in_(symbols),
            DailyBar.trade_date <= as_of,
            DailyBar.adj_factor.is_not(None),
        )
        .order_by(DailyBar.instrument_id, DailyBar.trade_date)
    ).all()
    for instrument_id, trade_date, adj_factor in rows:
        factors[instrument_id].append(adj_factor)
        dates[instrument_id].append(trade_date)

    ratios: dict[int, pd.Series] = {}
    for instrument_id, values in factors.items():
        if not values:
            continue
        frame = pd.DataFrame(
            {"adj_factor": values},
            index=pd.Index(dates[instrument_id], name="trade_date"),
        )
        ratios[instrument_id] = front_adjust_ratio(
            frame, symbol=symbols[instrument_id]
        )
    return ratios


def _ohlc_row(bar: dict[str, Any], multiplier: float) -> list[float | None]:
    values: list[float | None] = []
    for key in _OHLC_KEYS:
        value = bar.get(key)
        if value is None:
            values.append(None)
        else:
            values.append(round(float(value) * multiplier, 4))
    return values


def _bars_for_instrument(
    db: Session, instrument_id: int, as_of: date
) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            select(
                DailyBar.trade_date,
                DailyBar.open,
                DailyBar.high,
                DailyBar.low,
                DailyBar.close,
            )
            .where(
                DailyBar.instrument_id == instrument_id,
                DailyBar.trade_date <= as_of,
            )
            .order_by(DailyBar.trade_date.desc())
            .limit(THUMBNAIL_BAR_LIMIT)
        )
        .mappings()
        .all()
    )
    # 查询按日期倒序取最近 THUMBNAIL_BAR_LIMIT 根，返回时按时间正序（最旧在前）。
    return [dict(row) for row in reversed(rows)]


def build_thumbnail_payload(
    db: Session,
    report: ScreenReport,
    symbols: list[str],
) -> dict[str, Any]:
    """构造报表页 K 线缩略图数据。

    只在报表行内筛选标的，不在报表中的代码静默跳过；没有日线数据的标的
    同样跳过，因此没有可返回数据时 items 为空数组。
    """

    as_of = report.data_as_of or report.screen_as_of
    allowed = set(report_row_symbols(report))
    requested: list[str] = []
    for symbol in symbols:
        if symbol in allowed and symbol not in requested:
            requested.append(symbol)

    items: list[dict[str, Any]] = []
    if not requested:
        return {"as_of": as_of.isoformat(), "items": items}

    instruments = {
        str(symbol): int(instrument_id)
        for instrument_id, symbol in db.execute(
            select(Instrument.id, Instrument.symbol).where(
                Instrument.symbol.in_(requested)
            )
        ).all()
    }
    ratios = _factor_ratios(db, instruments, as_of)

    for symbol in requested:
        instrument_id = instruments.get(symbol)
        if instrument_id is None:
            continue
        bars = _bars_for_instrument(db, instrument_id, as_of)
        if not bars:
            continue
        ratio = ratios.get(instrument_id)
        ohlc: list[list[float | None]] = []
        for bar in bars:
            multiplier = 1.0
            if ratio is not None:
                value = ratio.get(bar["trade_date"])
                if value is not None and not pd.isna(value):
                    multiplier = float(value)
            ohlc.append(_ohlc_row(bar, multiplier))
        items.append({"symbol": symbol, "ohlc": ohlc})

    return {"as_of": as_of.isoformat(), "items": items}
