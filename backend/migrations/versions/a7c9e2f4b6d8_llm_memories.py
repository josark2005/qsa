"""llm memories

Revision ID: a7c9e2f4b6d8
Revises: d4b7c2e9f1a3
Create Date: 2026-11-04 12:40:00.000000

新增 llm_memories 表：每个 LLM 会话保存一份长期记忆摘要。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a7c9e2f4b6d8"
down_revision: Union[str, Sequence[str], None] = "d4b7c2e9f1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_memories",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_summarized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_memories_session_key"),
        "llm_memories",
        ["session_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_llm_memories_session_key"), table_name="llm_memories"
    )
    op.drop_table("llm_memories")
