from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    api_error,
    get_client_ip,
    get_container,
    get_current_user,
    get_db,
    get_session_data,
    verify_csrf,
)
from app.core.container import Container
from app.core.db import utcnow
from app.core.security import (
    PasswordPolicyError,
    hash_password,
    normalize_username,
    validate_password,
    verify_dummy_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageResponse,
)
from app.schemas.users import UserRead
from app.services.sessions import SessionData, SessionUnavailableError
from app.services.users import get_user_by_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def set_session_cookie(response: Response, container: Container, token: str) -> None:
    response.set_cookie(
        key=container.config.session_cookie_name,
        value=token,
        max_age=container.session_service.ttl_seconds(),
        httponly=True,
        secure=container.config.session_cookie_secure,
        samesite=container.config.session_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, container: Container) -> None:
    response.delete_cookie(
        key=container.config.session_cookie_name,
        path="/",
        secure=container.config.session_cookie_secure,
        samesite=container.config.session_cookie_samesite,
    )


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> LoginResponse:
    ip = get_client_ip(request)
    username = normalize_username(payload.username)

    try:
        allowed, retry_after = container.login_limiter.check(ip, username)
    except SessionUnavailableError:
        raise api_error(503, "login_unavailable", "登录服务暂不可用") from None
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "login_locked",
                "message": "登录失败次数过多，请稍后再试",
            },
            headers={"Retry-After": str(retry_after)},
        )

    user = get_user_by_username(db, username)
    if user is None:
        verify_dummy_password(payload.password)
        try:
            container.login_limiter.record_failure(ip, username)
        except SessionUnavailableError:
            logger.warning("failed to record login failure: session store unavailable")
        raise api_error(401, "invalid_credentials", "用户名或密码错误")

    if not verify_password(payload.password, user.password_hash):
        try:
            container.login_limiter.record_failure(ip, username)
        except SessionUnavailableError:
            logger.warning("failed to record login failure: session store unavailable")
        raise api_error(401, "invalid_credentials", "用户名或密码错误")

    if not user.is_active:
        raise api_error(403, "user_disabled", "账号已被禁用")

    try:
        container.login_limiter.reset(ip, username)
    except SessionUnavailableError:
        logger.warning("failed to reset login failure counter")

    user.last_login_at = utcnow()
    db.commit()

    try:
        session = container.session_service.create(user.id)
    except SessionUnavailableError:
        raise api_error(503, "session_unavailable", "会话服务暂不可用") from None

    set_session_cookie(response, container, session.token)
    return LoginResponse(
        user=UserRead.model_validate(user),
        csrf_token=session.csrf,
    )


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    session: SessionData = Depends(get_session_data),
    _: None = Depends(verify_csrf),
    container: Container = Depends(get_container),
) -> MessageResponse:
    try:
        container.session_service.delete(session.token, session.user_id)
    except SessionUnavailableError:
        logger.warning("failed to delete session during logout")
    clear_session_cookie(response, container)
    return MessageResponse(message="已退出登录")


@router.get("/me", response_model=MeResponse)
def me(
    user: User = Depends(get_current_user),
    session: SessionData = Depends(get_session_data),
) -> MeResponse:
    return MeResponse(
        user=UserRead.model_validate(user),
        csrf_token=session.csrf,
    )


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: SessionData = Depends(get_session_data),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> MessageResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise api_error(400, "invalid_current_password", "当前密码不正确")
    min_length = container.settings_service.snapshot.get_int(
        "security.password_min_length"
    )
    try:
        validate_password(payload.new_password, min_length)
    except PasswordPolicyError as exc:
        raise api_error(422, "password_policy", str(exc)) from None
    if payload.current_password == payload.new_password:
        raise api_error(422, "password_unchanged", "新密码不能与当前密码相同")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    db.commit()

    try:
        container.session_service.revoke_others(user.id, keep_token=session.token)
    except SessionUnavailableError:
        logger.warning("failed to revoke other sessions after password change")

    return MessageResponse(message="密码已修改")
