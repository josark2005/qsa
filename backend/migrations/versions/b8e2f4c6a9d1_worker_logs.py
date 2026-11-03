"""worker logs

Revision ID: b8e2f4c6a9d1
Revises: b2d4e6f8a0c1
Create Date: 2026-11-03 21:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8e2f4c6a9d1'
down_revision: Union[str, Sequence[str], None] = 'b2d4e6f8a0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker_logs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("logger", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["sync_requests.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_worker_logs_created_at"), "worker_logs", ["created_at"]
    )
    op.create_index(
        op.f("ix_worker_logs_request_id"), "worker_logs", ["request_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_worker_logs_request_id"), table_name="worker_logs")
    op.drop_index(op.f("ix_worker_logs_created_at"), table_name="worker_logs")
    op.drop_table("worker_logs")
