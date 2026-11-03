"""qq group subscriptions

Revision ID: d5b8e2f3a4c6
Revises: c4a7d9e1f2b3
Create Date: 2026-11-02 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5b8e2f3a4c6'
down_revision: Union[str, Sequence[str], None] = 'c4a7d9e1f2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qq_group_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_qq_group_subscriptions_group_openid"),
        "qq_group_subscriptions",
        ["group_openid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_qq_group_subscriptions_group_openid"),
        table_name="qq_group_subscriptions",
    )
    op.drop_table("qq_group_subscriptions")
