from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan", "NaN"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    parsed = to_float(value)
    if parsed is None:
        return None
    return int(parsed)


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text in {"", "-", "--", "None", "nan", "NaT"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def date_to_yymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
