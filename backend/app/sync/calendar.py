from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import akshare as ak
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.parsing import parse_date
from app.datasources.baostock import BaoStockClient
from app.datasources.symbols import board_for_code
from app.models.trading_calendar import TradingCalendar
from app.sync.db_utils import bulk_upsert

CALENDAR_EXCHANGE = "CN"
logger = logging.getLogger(__name__)


def sync_trading_calendar(
    db: Session,
    *,
    start_date: date,
    end_date: date,
) -> int:
    try:
        with BaoStockClient() as client:
            raw = client.fetch_trade_dates(
                start_date.isoformat(), end_date.isoformat()
            )
        rows = []
        for _, row in raw.iterrows():
            value = parse_date(row.get("calendar_date"))
            if value is None:
                continue
            rows.append(
                {
                    "exchange": CALENDAR_EXCHANGE,
                    "cal_date": value,
                    "is_open": str(row.get("is_trading_day")) == "1",
                    "source": "baostock",
                }
            )
    except Exception:
        logger.warning(
            "BaoStock calendar unavailable; falling back to AkShare",
            exc_info=True,
        )
        raw = ak.tool_trade_date_hist_sina()
        rows = [
            {
                "exchange": CALENDAR_EXCHANGE,
                "cal_date": parse_date(row.get("trade_date")),
                "is_open": True,
                "source": "akshare",
            }
            for _, row in raw.iterrows()
            if start_date <= parse_date(row.get("trade_date")) <= end_date
        ]
    count = bulk_upsert(
        db,
        TradingCalendar,
        rows,
        index_elements=["exchange", "cal_date"],
        update_columns=["is_open", "source"],
    )
    db.commit()
    return count


def is_trading_day(db: Session, target: date) -> bool:
    result = db.scalar(
        select(TradingCalendar.is_open).where(
            TradingCalendar.exchange == CALENDAR_EXCHANGE,
            TradingCalendar.cal_date == target,
        )
    )
    return bool(result)


def latest_trading_day(db: Session, on_or_before: date) -> date | None:
    return db.scalar(
        select(func.max(TradingCalendar.cal_date)).where(
            TradingCalendar.exchange == CALENDAR_EXCHANGE,
            TradingCalendar.is_open.is_(True),
            TradingCalendar.cal_date <= on_or_before,
        )
    )


def next_trading_day(db: Session, after: date) -> date | None:
    return db.scalar(
        select(func.min(TradingCalendar.cal_date)).where(
            TradingCalendar.exchange == CALENDAR_EXCHANGE,
            TradingCalendar.is_open.is_(True),
            TradingCalendar.cal_date > after,
        )
    )


@dataclass
class CalendarIndex:
    dates: list[date]
    ordinals: dict[date, int]

    @classmethod
    def load(cls, db: Session, start: date, end: date) -> CalendarIndex:
        values = db.scalars(
            select(TradingCalendar.cal_date)
            .where(
                TradingCalendar.exchange == CALENDAR_EXCHANGE,
                TradingCalendar.is_open.is_(True),
                TradingCalendar.cal_date >= start,
                TradingCalendar.cal_date <= end,
            )
            .order_by(TradingCalendar.cal_date)
        ).all()
        dates = list(values)
        return cls(dates=dates, ordinals={value: index + 1 for index, value in enumerate(dates)})

    def first_on_or_after(self, value: date) -> date | None:
        for candidate in self.dates:
            if candidate >= value:
                return candidate
        return None

    def trading_day_number(self, list_date: date | None, trade_date: date) -> int | None:
        if list_date is None:
            return None
        first = self.first_on_or_after(list_date)
        if first is None:
            return None
        start = self.ordinals.get(first)
        current = self.ordinals.get(trade_date)
        if start is None or current is None or current < start:
            return None
        return current - start + 1


def default_calendar_window(today: date) -> tuple[date, date]:
    return date(today.year - 2, 1, 1), date(today.year + 1, 12, 31)


def board_for_instrument(exchange: str, code: str, asset_type: str) -> str | None:
    if asset_type == "etf":
        return None
    if exchange == "BJ":
        return "bse"
    return board_for_code(code)
