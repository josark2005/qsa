from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LimitEvent(Base):
    __tablename__ = "limit_events"
    __table_args__ = (
        Index("ix_limit_events_trade_date_direction", "trade_date", "direction"),
    )

    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    direction: Mapped[str] = mapped_column(String(4), primary_key=True)

    is_close_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_touched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_one_word: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="computed")
