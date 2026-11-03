from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PipelineName = Literal[
    "market",
    "etf",
    "financial",
    "financial_morning",
    "weekly",
    "daily_report",
]
BackfillSource = Literal["tencent", "baostock", "full"]


class SyncRequestCreate(BaseModel):
    kind: Literal["pipeline", "backfill", "full_backfill", "history_import"]
    pipeline: PipelineName | None = None
    target_date: date | None = None
    start: date | None = None
    end: date | None = None
    asset_type: Literal["stock", "etf", "all"] = "all"
    source: BackfillSource = "tencent"
    limit: int | None = Field(default=None, ge=1, le=10000)
    include_delisted: bool = False
    rebuild_limits: bool = True
    full_refresh: bool = False
    force: bool = False
    symbols: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_request(self) -> SyncRequestCreate:
        if self.kind == "pipeline" and self.pipeline is None:
            raise ValueError("pipeline 请求必须指定 pipeline")
        if self.kind == "full_backfill" and self.source != "full":
            raise ValueError("full_backfill 请求必须使用 full 数据源")
        if self.source == "full" and self.kind != "full_backfill":
            raise ValueError("full 数据源必须使用 full_backfill 请求")
        if self.kind in {"backfill", "full_backfill"} and self.start and self.end:
            if self.start > self.end:
                raise ValueError("start 不能晚于 end")
        return self


class SyncRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    pipeline: str | None
    payload: dict[str, Any]
    status: str
    requested_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: dict[str, Any] | None
    progress: dict[str, Any] | None
    updated_at: datetime | None


class SyncRequestListResponse(BaseModel):
    items: list[SyncRequestRead]
    total: int
    limit: int
    offset: int
