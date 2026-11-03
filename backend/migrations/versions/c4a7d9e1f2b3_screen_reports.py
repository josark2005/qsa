"""screen reports

Revision ID: c4a7d9e1f2b3
Revises: 8d10354749f8
Create Date: 2026-11-02 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4a7d9e1f2b3'
down_revision: Union[str, Sequence[str], None] = '8d10354749f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "screen_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("criteria", JSON_TYPE, nullable=False),
        sa.Column("condition_views", JSON_TYPE, nullable=False),
        sa.Column("sort_views", JSON_TYPE, nullable=False),
        sa.Column("extra_fields", JSON_TYPE, nullable=False),
        sa.Column("rows", JSON_TYPE, nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("included", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("screen_as_of", sa.Date(), nullable=False),
        sa.Column("data_as_of", sa.Date(), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_screen_reports_token"), "screen_reports", ["token"], unique=True
    )
    op.create_index(
        "ix_screen_reports_generated_at",
        "screen_reports",
        ["generated_at"],
        unique=False,
    )
    op.create_index(
        "ix_screen_reports_owner",
        "screen_reports",
        ["owner_id", "generated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_screen_reports_owner", table_name="screen_reports")
    op.drop_index("ix_screen_reports_generated_at", table_name="screen_reports")
    op.drop_index(op.f("ix_screen_reports_token"), table_name="screen_reports")
    op.drop_table("screen_reports")
