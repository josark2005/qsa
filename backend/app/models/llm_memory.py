from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow


class LLMMemory(Base):
    """LLM 会话的长期记忆摘要（每个会话一份）。

    对话历史仍放在 redis（短期上下文），这里保存由较早对话压缩出的摘要，
    用于在后续对话里注入 system 消息，避免上下文无限增长。
    """

    __tablename__ = "llm_memories"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    session_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 累计整理进摘要的对话轮数（一轮 = 一条用户消息 + 一条助手回复）。
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_summarized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
