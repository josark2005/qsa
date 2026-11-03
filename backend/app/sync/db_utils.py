from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.db import utcnow


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: clean_value(value) for key, value in row.items()}


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def bulk_upsert(
    db: Session,
    model: type,
    rows: list[dict[str, Any]],
    *,
    index_elements: list[str],
    update_columns: list[str],
    chunk_size: int = 1000,
) -> int:
    if not rows:
        return 0
    table = inspect(model).local_table
    columns = {column.name for column in table.columns}
    total = 0
    for chunk in chunked(rows, chunk_size):
        cleaned = [clean_row(row) for row in chunk]
        stmt = pg_insert(model).values(cleaned)
        update_values = {
            column: getattr(stmt.excluded, column)
            for column in update_columns
            if column in columns
        }
        if "updated_at" in columns:
            update_values["updated_at"] = utcnow()
        if update_values:
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_=update_values,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=index_elements)
        db.execute(stmt)
        total += len(cleaned)
    return total
