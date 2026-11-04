"""personal qq groups

Revision ID: c3f8a1d6b2e4
Revises: b8e2f4c6a9d1
Create Date: 2026-11-04 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3f8a1d6b2e4'
down_revision: Union[str, Sequence[str], None] = 'b8e2f4c6a9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personal_qq_groups",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("group_name", sa.String(length=128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_personal_qq_groups_group_id"),
        "personal_qq_groups",
        ["group_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_personal_qq_groups_group_id"),
        table_name="personal_qq_groups",
    )
    op.drop_table("personal_qq_groups")
