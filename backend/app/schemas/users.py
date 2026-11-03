from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

UserRole = Literal["admin", "user"]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    quota_overrides: dict[str, Any] | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class QuotaOverrides(BaseModel):
    screen_per_minute: int | None = Field(default=None, ge=0)
    screen_per_day: int | None = Field(default=None, ge=0)
    export_per_day: int | None = Field(default=None, ge=0)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[^\s@]+$")
    role: UserRole = "user"
    password: str | None = Field(default=None, max_length=256)


class UserCreateResponse(BaseModel):
    user: UserRead
    temporary_password: str | None = None


class UserUpdateRequest(BaseModel):
    role: UserRole
    is_active: bool
    quota_overrides: QuotaOverrides | None = None


class UserResetPasswordRequest(BaseModel):
    password: str | None = Field(default=None, max_length=256)


class UserResetPasswordResponse(BaseModel):
    user: UserRead
    temporary_password: str | None = None


class UserListResponse(BaseModel):
    items: list[UserRead]
    total: int
    limit: int
    offset: int
