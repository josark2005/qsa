from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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


class ShareholderCount(Base):
    __tablename__ = "shareholder_counts"
    __table_args__ = (
        Index("ix_shareholder_counts_period_announce", "period_end", "announce_date"),
        Index("ix_shareholder_counts_instrument_announce", "instrument_id", "announce_date"),
    )

    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    announce_date: Mapped[date] = mapped_column(Date, primary_key=True)

    holder_num: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_num_prev: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_change: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_change_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4, asdecimal=False)
    )
    avg_holding_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 2, asdecimal=False)
    )
    avg_holding_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4, asdecimal=False)
    )
    total_mv: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    total_shares: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="akshare")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
