from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType, utcnow


class ScreenReport(Base):
    __tablename__ = "screen_reports"
    __table_args__ = (
        Index("ix_screen_reports_generated_at", "generated_at"),
        Index("ix_screen_reports_owner", "owner_id", "generated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)

    criteria: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    condition_views: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False
    )
    sort_views: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    extra_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)

    total: Mapped[int] = mapped_column(Integer, nullable=False)
    included: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    screen_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    data_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
