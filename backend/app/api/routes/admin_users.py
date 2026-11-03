from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    api_error,
    get_client_ip,
    get_container,
    get_db,
    require_admin,
    verify_csrf,
)
from app.core.container import Container
from app.core.security import PasswordPolicyError
from app.models.user import User
from app.schemas.users import (
    UserCreateRequest,
    UserCreateResponse,
    UserListResponse,
    UserRead,
    UserResetPasswordRequest,
    UserResetPasswordResponse,
    UserUpdateRequest,
)
from app.services.audit import add_audit
from app.services.sessions import SessionUnavailableError
from app.services.users import (
    LastAdminError,
    UserConflictError,
    create_user,
    get_user,
    reset_password,
    update_user,
    user_audit_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_admin), Depends(verify_csrf)],
)


@router.get("", response_model=UserListResponse)
def list_users(
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> UserListResponse:
    max_limit = container.settings_service.snapshot.get_int(
        "screening.max_page_size"
    )
    limit = min(limit, max_limit)

    filters = []
    if q:
        filters.append(User.username.ilike(f"%{q.strip().lower()}%"))

    total_statement = select(func.count()).select_from(User)
    items_statement = select(User)
    for condition in filters:
        total_statement = total_statement.where(condition)
        items_statement = items_statement.where(condition)
    total = int(db.scalar(total_statement) or 0)
    users = db.scalars(
        items_statement.order_by(User.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return UserListResponse(
        items=[UserRead.model_validate(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
def create_user_endpoint(
    payload: UserCreateRequest,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> UserCreateResponse:
    min_length = container.settings_service.snapshot.get_int(
        "security.password_min_length"
    )
    try:
        user, generated = create_user(
            db,
            username=payload.username,
            role=payload.role,
            password=payload.password,
            created_by=actor.id,
            min_password_length=min_length,
        )
        add_audit(
            db,
            actor_id=actor.id,
            action="user.create",
            target_type="user",
            target_id=str(user.id),
            after=user_audit_payload(user),
            ip=get_client_ip(request),
        )
        db.commit()
    except UserConflictError as exc:
        db.rollback()
        raise api_error(409, "user_conflict", str(exc)) from None
    except PasswordPolicyError as exc:
        db.rollback()
        raise api_error(422, "password_policy", str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise api_error(409, "user_conflict", "该用户名已存在") from None

    return UserCreateResponse(
        user=UserRead.model_validate(user),
        temporary_password=generated,
    )


@router.patch("/{user_id}", response_model=UserRead)
def update_user_endpoint(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> UserRead:
    user = get_user(db, user_id)
    if user is None:
        raise api_error(404, "user_not_found", "用户不存在")

    before = user_audit_payload(user)
    quota_overrides = (
        payload.quota_overrides.model_dump(exclude_none=True)
        if payload.quota_overrides is not None
        else None
    )
    try:
        update_user(
            db,
            user=user,
            role=payload.role,
            is_active=payload.is_active,
            quota_overrides=quota_overrides,
        )
        add_audit(
            db,
            actor_id=actor.id,
            action="user.update",
            target_type="user",
            target_id=str(user.id),
            before=before,
            after=user_audit_payload(user),
            ip=get_client_ip(request),
        )
        db.commit()
    except LastAdminError as exc:
        db.rollback()
        raise api_error(409, "last_admin", str(exc)) from None
    except UserConflictError as exc:
        db.rollback()
        raise api_error(409, "user_conflict", str(exc)) from None

    if not user.is_active or before["role"] != user.role:
        try:
            container.session_service.revoke_user(user.id)
        except SessionUnavailableError:
            logger.warning("failed to revoke sessions for user %s", user.id)

    return UserRead.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=UserResetPasswordResponse)
def reset_password_endpoint(
    user_id: uuid.UUID,
    payload: UserResetPasswordRequest,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> UserResetPasswordResponse:
    user = get_user(db, user_id)
    if user is None:
        raise api_error(404, "user_not_found", "用户不存在")
    min_length = container.settings_service.snapshot.get_int(
        "security.password_min_length"
    )
    try:
        user, generated = reset_password(
            db,
            user=user,
            password=payload.password,
            min_password_length=min_length,
        )
        add_audit(
            db,
            actor_id=actor.id,
            action="user.reset_password",
            target_type="user",
            target_id=str(user.id),
            after={"must_change_password": True},
            ip=get_client_ip(request),
        )
        db.commit()
    except PasswordPolicyError as exc:
        db.rollback()
        raise api_error(422, "password_policy", str(exc)) from None

    try:
        container.session_service.revoke_user(user.id)
    except SessionUnavailableError:
        logger.warning("failed to revoke sessions for user %s", user.id)

    return UserResetPasswordResponse(
        user=UserRead.model_validate(user),
        temporary_password=generated,
    )
