from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"
    __table_args__ = (
        Index("ix_trading_calendar_open_date", "is_open", "cal_date"),
    )

    exchange: Mapped[str] = mapped_column(String(4), primary_key=True)
    cal_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="baostock")
