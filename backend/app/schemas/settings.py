from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SettingMeta(BaseModel):
    key: str
    label: str
    group: str
    group_label: str
    type: str
    description: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    dynamic: bool
    requires_restart: bool
    public: bool
    sensitive: bool = False
    multiline: bool = False
    order: int
    tags: list[str]
    value: Any
    version: int
    is_default: bool
    updated_by: str | None = None
    updated_by_username: str | None = None
    updated_at: datetime | None = None


class SettingsResponse(BaseModel):
    items: list[SettingMeta]
    loaded_at: datetime | None = None


class SettingUpdateItem(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: Any
    version: int = Field(default=0, ge=0)


class SettingsPatchRequest(BaseModel):
    items: list[SettingUpdateItem] = Field(min_length=1)


class PublicSettingsResponse(BaseModel):
    values: dict[str, Any]
    loaded_at: datetime | None = None
