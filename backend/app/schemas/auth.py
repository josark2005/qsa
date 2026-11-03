from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.users import UserRead


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    user: UserRead
    csrf_token: str


class MeResponse(BaseModel):
    user: UserRead
    csrf_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class MessageResponse(BaseModel):
    message: str
