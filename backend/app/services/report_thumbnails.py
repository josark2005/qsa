from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def _front_factor(bar: dict[str, Any]) -> float | None:
    """前复权系数：close_adj / close；任一无效时返回 None 表示保留原始值。"""
    close = bar.get("close")
    close_adj = bar.get("close_adj")
    if close is None or close_adj is None:
        return None
    close_value = float(close)
    if close_value == 0:
        return None
    return float(close_adj) / close_value


def _ohlc_row(bar: dict[str, Any]) -> list[float | None]:
    factor = _front_factor(bar)
    multiplier = factor if factor is not None else 1.0
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
                DailyBar.open,
                DailyBar.high,
                DailyBar.low,
                DailyBar.close,
                DailyBar.close_adj,
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

    for symbol in requested:
        instrument_id = instruments.get(symbol)
        if instrument_id is None:
            continue
        bars = _bars_for_instrument(db, instrument_id, as_of)
        if not bars:
            continue
        items.append({"symbol": symbol, "ohlc": [_ohlc_row(bar) for bar in bars]})

    return {"as_of": as_of.isoformat(), "items": items}
