from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import api_error, get_db, require_admin
from app.schemas.logs import WorkerLogListResponse, WorkerLogRead
from app.services.worker_logs import list_worker_logs

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

router = APIRouter(
    prefix="/admin/logs",
    tags=["admin-logs"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=WorkerLogListResponse)
def get_worker_logs(
    level: str | None = Query(default=None, max_length=10),
    request_id: int | None = Query(default=None),
    q: str | None = Query(default=None, max_length=120),
    after_id: int | None = Query(default=None),
    before_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> WorkerLogListResponse:
    normalized_level = level.strip().upper() if level else None
    if normalized_level is not None and normalized_level not in LOG_LEVELS:
        raise api_error(422, "invalid_log_level", "日志级别不合法")
    if after_id is not None and before_id is not None:
        raise api_error(422, "invalid_log_cursor", "after_id 与 before_id 不能同时使用")
    keyword = q.strip() if q else None
    items, has_more = list_worker_logs(
        db,
        level=normalized_level,
        request_id=request_id,
        q=keyword or None,
        after_id=after_id,
        before_id=before_id,
        limit=limit,
    )
    return WorkerLogListResponse(
        items=[WorkerLogRead.model_validate(item) for item in items],
        has_more=has_more,
    )
