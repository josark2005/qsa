from __future__ import annotations

import secrets
from collections.abc import Generator
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.container import Container
from app.models.user import User
from app.services.sessions import SessionData, SessionUnavailableError
from app.services.users import get_user


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise api_error(503, "service_unavailable", "服务尚未初始化")
    return container


def get_db(container: Container = Depends(get_container)) -> Generator[Session, None, None]:
    db = container.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def get_session_data(
    request: Request,
    container: Container = Depends(get_container),
) -> SessionData:
    token = request.cookies.get(container.config.session_cookie_name)
    if not token:
        raise api_error(401, "not_authenticated", "请先登录")
    try:
        session = container.session_service.get(token)
    except SessionUnavailableError:
        raise api_error(503, "session_unavailable", "会话服务暂不可用") from None
    if session is None:
        raise api_error(401, "session_expired", "登录已过期，请重新登录")
    return session


def get_current_user(
    db: Session = Depends(get_db),
    session: SessionData = Depends(get_session_data),
) -> User:
    user = get_user(db, session.user_id)
    if user is None:
        raise api_error(401, "user_not_found", "用户不存在")
    if not user.is_active:
        raise api_error(403, "user_disabled", "账号已被禁用")
    return user


def require_ready_user(user: User = Depends(get_current_user)) -> User:
    if user.must_change_password:
        raise api_error(
            403,
            "password_change_required",
            "首次登录或密码重置后需要先修改密码",
        )
    return user


def require_admin(user: User = Depends(require_ready_user)) -> User:
    if user.role != "admin":
        raise api_error(403, "admin_required", "需要管理员权限")
    return user


def verify_csrf(
    request: Request,
    session: SessionData = Depends(get_session_data),
) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    token = request.headers.get("X-CSRF-Token")
    if not token or not secrets.compare_digest(token, session.csrf):
        raise api_error(403, "csrf_failed", "CSRF 校验失败，请刷新页面后重试")


def get_settings_service(container: Container = Depends(get_container)) -> Any:
    return container.settings_service
