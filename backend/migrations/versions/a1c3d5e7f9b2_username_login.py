"""switch user login from email to username

Revision ID: a1c3d5e7f9b2
Revises: f7d1a2b3c4e5
Create Date: 2026-11-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3d5e7f9b2"
down_revision: Union[str, Sequence[str], None] = "f7d1a2b3c4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))
    op.execute(
        """
        WITH base AS (
            SELECT id,
                   created_at,
                   lower(split_part(email, '@', 1)) AS name
            FROM users
        ),
        numbered AS (
            SELECT id,
                   name,
                   row_number() OVER (
                       PARTITION BY name ORDER BY created_at, id
                   ) AS rn
            FROM base
        )
        UPDATE users AS u
        SET username = CASE
            WHEN numbered.rn = 1 AND length(numbered.name) >= 2
                THEN numbered.name
            ELSE left(
                coalesce(nullif(numbered.name, ''), 'user'), 25
            ) || '_' || numbered.rn
        END
        FROM numbered
        WHERE u.id = numbered.id
        """
    )
    op.alter_column("users", "username", nullable=False)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "email")


def downgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.execute(
        """
        UPDATE users
        SET email = username || '@local'
        """
    )
    op.alter_column("users", "email", nullable=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "username")
