from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConditionIn(BaseModel):
    field: str = Field(min_length=1, max_length=64)
    op: str = Field(min_length=1, max_length=16)
    value: Any
    params: dict[str, Any] = Field(default_factory=dict)


class SortIn(BaseModel):
    field: str = Field(min_length=1, max_length=64)
    order: Literal["asc", "desc"] = "desc"
    params: dict[str, Any] = Field(default_factory=dict)


class ScreenRequest(BaseModel):
    as_of: date | None = None
    logic: Literal["and", "or"] = "and"
    conditions: list[ConditionIn] = Field(min_length=1, max_length=30)
    sort: list[SortIn] = Field(default_factory=list, max_length=5)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=1000)


class ExtraField(BaseModel):
    alias: str
    label: str
    field: str
    params: dict[str, Any]


class ScreenResponse(BaseModel):
    as_of: date
    total: int
    page: int
    page_size: int
    rows: list[dict[str, Any]]
    extra_fields: list[ExtraField]


class FactorListResponse(BaseModel):
    items: list[dict[str, Any]]
    categories: list[dict[str, str]]
