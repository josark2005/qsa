from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_container, get_db, require_admin
from app.core.container import Container
from app.models.audit_log import AuditLog
from app.schemas.audit import AuditLogListResponse, AuditLogRead

router = APIRouter(
    prefix="/admin/audit-logs",
    tags=["admin-audit"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=AuditLogListResponse)
def list_audit_logs(
    action: str | None = Query(default=None, max_length=64),
    actor_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> AuditLogListResponse:
    max_limit = container.settings_service.snapshot.get_int(
        "screening.max_page_size"
    )
    limit = min(limit, max_limit)

    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if actor_id:
        filters.append(AuditLog.actor_id == actor_id)

    total_statement = select(func.count()).select_from(AuditLog)
    items_statement = select(AuditLog)
    for condition in filters:
        total_statement = total_statement.where(condition)
        items_statement = items_statement.where(condition)

    total = int(db.scalar(total_statement) or 0)
    items = db.scalars(
        items_statement.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return AuditLogListResponse(
        items=[AuditLogRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
