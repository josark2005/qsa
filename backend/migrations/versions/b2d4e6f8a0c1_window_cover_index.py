"""add covering index for window price queries

Revision ID: b2d4e6f8a0c1
Revises: a1c3d5e7f9b2
Create Date: 2026-11-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "b2d4e6f8a0c1"
down_revision: Union[str, Sequence[str], None] = "a1c3d5e7f9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_daily_bars_window_cover "
        "ON daily_bars (instrument_id, trade_date) INCLUDE (close_adj)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_daily_bars_window_cover")
