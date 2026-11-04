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


class CompareRequest(BaseModel):
    """检查单个标的是否满足一组筛选条件（用于调整条件）。"""

    symbol: str = Field(min_length=1, max_length=32)
    as_of: date | None = None
    logic: Literal["and", "or"] = "and"
    conditions: list[ConditionIn] = Field(min_length=1, max_length=30)


class CompareConditionResult(BaseModel):
    field: str
    label: str
    category: str
    category_label: str
    op: str
    target: Any
    params: dict[str, Any]
    actual: Any
    verdict: Literal["pass", "fail", "unknown"]
    gap: float | None
    reason: str
    detail: dict[str, Any] | None = None


class CompareResponse(BaseModel):
    symbol: str
    code: str
    name: str
    board: str | None
    industry: str | None
    as_of: date
    logic: Literal["and", "or"]
    matches: bool
    passed: int
    failed: int
    unknown: int
    conditions: list[CompareConditionResult]


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
