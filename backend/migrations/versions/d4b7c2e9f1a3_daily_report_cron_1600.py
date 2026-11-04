"""daily report cron at 16:00

Revision ID: d4b7c2e9f1a3
Revises: c3f8a1d6b2e4
Create Date: 2026-11-04 08:50:00.000000

把历史上保存过的“每日 03:31”默认值迁移为 16:00；只更新仍等于旧默认值的
记录，管理员自定义过的 cron 保持不变。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4b7c2e9f1a3"
down_revision: Union[str, Sequence[str], None] = "c3f8a1d6b2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "sync.pipeline.daily_report.cron"
OLD_VALUE = "31 3 * * *"
NEW_VALUE = "0 16 * * *"


def _settings_table(bind) -> sa.Table:
    return sa.Table(
        "system_settings",
        sa.MetaData(),
        autoload_with=bind,
    )


def _replace_value(bind, *, expected: str, replacement: str) -> None:
    table = _settings_table(bind)
    row = bind.execute(
        sa.select(table.c.value, table.c.version).where(
            table.c.key == KEY
        )
    ).first()
    if row is None or row[0] != expected:
        return
    bind.execute(
        sa.update(table)
        .where(table.c.key == KEY)
        .values(value=replacement, version=row[1] + 1)
    )


def upgrade() -> None:
    _replace_value(op.get_bind(), expected=OLD_VALUE, replacement=NEW_VALUE)


def downgrade() -> None:
    _replace_value(op.get_bind(), expected=NEW_VALUE, replacement=OLD_VALUE)
