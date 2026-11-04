from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class PersonalQQGroup(Base):
    """个人号（OneBot v11）每日选股推送的群订阅。"""

    __tablename__ = "personal_qq_groups"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    group_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    last_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
