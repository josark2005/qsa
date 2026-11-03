from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType, utcnow


class FinancialReport(Base):
    __tablename__ = "financial_reports"
    __table_args__ = (
        Index("ix_financial_reports_period_announce", "report_period", "announce_date"),
        Index("ix_financial_reports_instrument_announce", "instrument_id", "announce_date"),
    )

    instrument_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    report_period: Mapped[date] = mapped_column(Date, primary_key=True)
    announce_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(8), nullable=False)

    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    revenue_yoy: Mapped[Decimal | None] = mapped_column(Numeric(20, 4, asdecimal=False))
    net_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    net_profit_yoy: Mapped[Decimal | None] = mapped_column(Numeric(20, 4, asdecimal=False))
    eps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6, asdecimal=False))
    bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6, asdecimal=False))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 4, asdecimal=False))
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 4, asdecimal=False))
    ocf_per_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 6, asdecimal=False))
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    debt_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 4, asdecimal=False))
    operating_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2, asdecimal=False))
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="akshare")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
