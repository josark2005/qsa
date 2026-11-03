"""history imports

Revision ID: f7d1a2b3c4e5
Revises: e6c9f4a7b2d1
Create Date: 2026-11-02 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7d1a2b3c4e5'
down_revision: Union[str, Sequence[str], None] = 'e6c9f4a7b2d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "history_imports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_history_imports_filename"),
        "history_imports",
        ["filename"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_history_imports_filename"), table_name="history_imports"
    )
    op.drop_table("history_imports")
