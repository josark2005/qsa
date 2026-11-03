from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class WorkerLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    level: str
    logger: str
    message: str
    request_id: int | None

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # SQLite 读回来是 naive 的 UTC 时间，统一补上时区，便于前端按 ISO 解析。
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class WorkerLogListResponse(BaseModel):
    items: list[WorkerLogRead]
    has_more: bool
