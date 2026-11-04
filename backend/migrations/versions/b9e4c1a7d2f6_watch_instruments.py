"""watch instruments

Revision ID: b9e4c1a7d2f6
Revises: a7c9e2f4b6d8
Create Date: 2026-11-04 13:05:00.000000

新增 watch_instruments 表：用户关注的标的列表，instrument_id 唯一。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b9e4c1a7d2f6"
down_revision: Union[str, Sequence[str], None] = "a7c9e2f4b6d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watch_instruments",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_watch_instruments_instrument_id"),
        "watch_instruments",
        ["instrument_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_watch_instruments_created_at"),
        "watch_instruments",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_watch_instruments_created_at"),
        table_name="watch_instruments",
    )
    op.drop_index(
        op.f("ix_watch_instruments_instrument_id"),
        table_name="watch_instruments",
    )
    op.drop_table("watch_instruments")
