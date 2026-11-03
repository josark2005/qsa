from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.screening import ConditionIn, SortIn


class ReportCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    as_of: date | None = None
    logic: Literal["and", "or"] = "and"
    conditions: list[ConditionIn] = Field(min_length=1, max_length=30)
    sort: list[SortIn] = Field(default_factory=list, max_length=5)


class ReportCreateResponse(BaseModel):
    token: str
    title: str
    generated_at: str
    report_date: date
    screen_as_of: date
    data_as_of: date | None
    total: int
    included: int
    truncated: bool
    rows: list[dict[str, Any]]
    condition_views: list[dict[str, Any]]
    sort_views: list[dict[str, Any]]
    extra_fields: list[dict[str, Any]]
    criteria: dict[str, Any]
    stale: bool = False
    stale_reason: str | None = None
    latest_trade_date: date | None = None


class ReportHistoryItem(BaseModel):
    token: str
    title: str
    generated_at: datetime
    report_date: date
    screen_as_of: date
    data_as_of: date | None
    total: int
    included: int
    truncated: bool
    condition_views: list[dict[str, Any]]
    stale: bool
    stale_reason: str | None
    owner_username: str | None = None


class ReportHistoryResponse(BaseModel):
    items: list[ReportHistoryItem]
    total: int
    limit: int
    offset: int
    latest_trade_date: date | None = None
