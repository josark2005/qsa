from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
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
from app.core.settings_registry import SETTING_SPECS
from app.core.settings_service import (
    SettingUpdate,
    SettingsConflictError,
)
from app.core.settings_registry import SettingsValidationError
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.schemas.settings import (
    PublicSettingsResponse,
    SettingsPatchRequest,
    SettingsResponse,
)
from app.services.sessions import SessionUnavailableError

logger = logging.getLogger(__name__)

GROUP_ORDER = {
    "quota": 10,
    "security": 20,
    "screening": 30,
    "sync": 40,
    "qq_bot": 50,
    "personal_qq": 60,
    "maintenance": 70,
}

router = APIRouter(
    prefix="/admin/settings",
    tags=["admin-settings"],
    dependencies=[Depends(require_admin)],
)
public_router = APIRouter(prefix="/settings", tags=["settings"])


def build_admin_settings(db: Session, container: Container) -> SettingsResponse:
    rows = db.execute(
        select(SystemSetting, User.username).outerjoin(
            User, SystemSetting.updated_by == User.id
        )
    ).all()
    row_map = {row.key: (row, username) for row, username in rows}
    snapshot = container.settings_service.snapshot

    items = []
    for spec in sorted(
        SETTING_SPECS,
        key=lambda item: (GROUP_ORDER.get(item.group, 999), item.order),
    ):
        meta = spec.to_meta()
        row, username = row_map.get(spec.key, (None, None))
        meta.update(
            {
                "value": snapshot.values.get(spec.key, spec.default),
                "version": row.version if row is not None else 0,
                "is_default": row is None,
                "updated_by": str(row.updated_by) if row and row.updated_by else None,
                "updated_by_username": username,
                "updated_at": row.updated_at if row is not None else None,
            }
        )
        items.append(meta)
    return SettingsResponse(items=items, loaded_at=snapshot.loaded_at)


@router.get("", response_model=SettingsResponse)
def list_settings(
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> SettingsResponse:
    return build_admin_settings(db, container)


@router.patch("", response_model=SettingsResponse)
def patch_settings(
    payload: SettingsPatchRequest,
    request: Request,
    actor: User = Depends(require_admin),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> SettingsResponse:
    updates = [
        SettingUpdate(key=item.key, value=item.value, version=item.version)
        for item in payload.items
    ]
    actor_id = actor.id
    try:
        container.settings_service.update(
            updates,
            actor_id=actor_id,
            ip=get_client_ip(request),
        )
    except SettingsConflictError as exc:
        raise api_error(409, "settings_conflict", str(exc)) from None
    except SettingsValidationError as exc:
        raise api_error(422, "settings_invalid", str(exc)) from None

    db.rollback()
    return build_admin_settings(db, container)


@public_router.get("/public", response_model=PublicSettingsResponse)
def public_settings(
    container: Container = Depends(get_container),
) -> PublicSettingsResponse:
    snapshot = container.settings_service.snapshot
    values = {
        spec.key: snapshot.values.get(spec.key, spec.default)
        for spec in SETTING_SPECS
        if spec.public
    }
    return PublicSettingsResponse(values=values, loaded_at=snapshot.loaded_at)
