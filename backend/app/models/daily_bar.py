from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class DailyBar(Base):
    __tablename__ = "daily_bars"
    __table_args__ = (
        Index("ix_daily_bars_trade_date", "trade_date"),
        Index("ix_daily_bars_trade_date_instrument", "trade_date", "instrument_id"),
    )

    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)

    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    preclose: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    close_adj: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))

    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4, asdecimal=False))
    pct_chg: Mapped[Decimal | None] = mapped_column(Numeric(12, 4, asdecimal=False))
    adj_factor: Mapped[Decimal | None] = mapped_column(Numeric(20, 8, asdecimal=False))

    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6, asdecimal=False))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(18, 6, asdecimal=False))
    up_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))
    down_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 4, asdecimal=False))

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
