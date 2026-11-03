"""qq subscription group name

Revision ID: e6c9f4a7b2d1
Revises: d5b8e2f3a4c6
Create Date: 2026-11-02 15:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e6c9f4a7b2d1'
down_revision: Union[str, Sequence[str], None] = 'd5b8e2f3a4c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "qq_group_subscriptions",
        sa.Column("group_name", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qq_group_subscriptions", "group_name")
