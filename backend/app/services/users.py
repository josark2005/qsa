from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import (
    generate_password,
    hash_password,
    normalize_username,
    validate_password,
)
from app.models.user import User

USER_ROLES = {"admin", "user"}
QUOTA_OVERRIDE_KEYS = {
    "screen_per_minute",
    "screen_per_day",
    "export_per_day",
}


class UserConflictError(ValueError):
    pass


class LastAdminError(ValueError):
    pass


def get_user(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(
        select(User).where(User.username == normalize_username(username))
    )


def normalize_new_username(username: str) -> str:
    value = normalize_username(username)
    if not 2 <= len(value) <= 32:
        raise UserConflictError("用户名长度需为 2-32 位")
    if any(character.isspace() for character in value) or "@" in value:
        raise UserConflictError("用户名不能包含空格或 @")
    return value


def count_active_admins(db: Session, exclude_id: uuid.UUID | None = None) -> int:
    statement = (
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True))
    )
    if exclude_id is not None:
        statement = statement.where(User.id != exclude_id)
    return int(db.scalar(statement) or 0)


def ensure_role(role: str) -> str:
    if role not in USER_ROLES:
        raise UserConflictError("角色必须是 admin 或 user")
    return role


def ensure_admin_remains(
    db: Session,
    user: User,
    new_role: str,
    new_active: bool,
) -> None:
    losing_admin = (
        user.role == "admin"
        and user.is_active
        and (new_role != "admin" or not new_active)
    )
    if losing_admin and count_active_admins(db, exclude_id=user.id) == 0:
        raise LastAdminError("不能删除、禁用或降级最后一个有效管理员")


def normalize_quota_overrides(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - QUOTA_OVERRIDE_KEYS)
    if unknown:
        raise UserConflictError(f"未知配额覆盖项: {', '.join(unknown)}")
    normalized: dict[str, int] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise UserConflictError(f"{key} 必须是非负整数")
        normalized[key] = raw
    return normalized or None


def create_user(
    db: Session,
    *,
    username: str,
    role: str,
    password: str | None,
    created_by: uuid.UUID | None,
    min_password_length: int,
) -> tuple[User, str | None]:
    ensure_role(role)
    username = normalize_new_username(username)
    if get_user_by_username(db, username) is not None:
        raise UserConflictError("该用户名已存在")
    generated: str | None = None
    if password is None:
        generated = generate_password(16)
        password = generated
    validate_password(password, min_password_length)
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        must_change_password=True,
        created_by=created_by,
    )
    db.add(user)
    db.flush()
    return user, generated


def update_user(
    db: Session,
    *,
    user: User,
    role: str,
    is_active: bool,
    quota_overrides: dict[str, Any] | None,
) -> User:
    ensure_role(role)
    ensure_admin_remains(db, user, role, is_active)
    user.role = role
    user.is_active = is_active
    user.quota_overrides = normalize_quota_overrides(quota_overrides)
    db.flush()
    return user


def reset_password(
    db: Session,
    *,
    user: User,
    password: str | None,
    min_password_length: int,
) -> tuple[User, str | None]:
    generated: str | None = None
    if password is None:
        generated = generate_password(16)
        password = generated
    validate_password(password, min_password_length)
    user.password_hash = hash_password(password)
    user.must_change_password = True
    db.flush()
    return user, generated


def user_audit_payload(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "quota_overrides": user.quota_overrides,
    }
