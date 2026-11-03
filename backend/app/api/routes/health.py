from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import get_container
from app.core.container import Container

router = APIRouter(tags=["health"])


def _check_db(container: Container) -> bool:
    try:
        with container.session_factory() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _check_redis(container: Container) -> bool:
    try:
        return bool(container.redis.ping())
    except Exception:
        return False


@router.get("/health")
def health(container: Container = Depends(get_container)) -> JSONResponse:
    db_ok = _check_db(container)
    redis_ok = _check_redis(container)
    status = "ok" if db_ok and redis_ok else "degraded"
    return JSONResponse(
        {
            "status": status,
            "database": db_ok,
            "redis": redis_ok,
            "environment": container.config.app_env,
        }
    )


@router.get("/health/ready")
def ready(container: Container = Depends(get_container)) -> JSONResponse:
    db_ok = _check_db(container)
    redis_ok = _check_redis(container)
    ok = db_ok and redis_ok
    return JSONResponse(
        {
            "status": "ready" if ok else "not_ready",
            "database": db_ok,
            "redis": redis_ok,
        },
        status_code=200 if ok else 503,
    )
