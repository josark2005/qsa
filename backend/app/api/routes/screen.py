from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.api.deps import (
    api_error,
    get_container,
    get_db,
    require_ready_user,
    verify_csrf,
)
from app.core.container import Container
from app.models.user import User
from app.screening.query_builder import (
    ScreenError,
    build_screen,
    compare_instrument,
)
from app.screening.registry import (
    CATEGORY_LABELS,
    FactorError,
    factor_metadata,
)
from app.schemas.screening import (
    CompareRequest,
    CompareResponse,
    FactorListResponse,
    ScreenRequest,
    ScreenResponse,
)
from app.services.screen_quota import QuotaUnavailableError, ScreenQuota

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

router = APIRouter(tags=["screening"])


@router.get("/meta/factors", response_model=FactorListResponse)
def list_factors(
    user: User = Depends(require_ready_user),
) -> FactorListResponse:
    return FactorListResponse(
        items=factor_metadata(),
        categories=[
            {"key": key, "label": label}
            for key, label in CATEGORY_LABELS.items()
        ],
    )


def _cache_key(request: ScreenRequest) -> str:
    payload = request.model_dump(mode="json")
    payload["_today"] = datetime.now(SHANGHAI).date().isoformat()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"qsa:screen:{digest}"


@router.post("/screen", response_model=ScreenResponse)
def run_screen(
    payload: ScreenRequest,
    user: User = Depends(require_ready_user),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
    x_cache_control: str | None = Header(default=None, alias="X-Cache-Control"),
) -> ScreenResponse | JSONResponse:
    snapshot = container.settings_service.snapshot
    if snapshot.get_bool("maintenance.enabled") and user.role != "admin":
        raise api_error(503, "maintenance", "系统维护中，请稍后再试")

    max_page_size = snapshot.get_int("screening.max_page_size")
    if payload.page_size > max_page_size:
        raise api_error(
            422,
            "page_size_too_large",
            f"page_size 不能大于 {max_page_size}",
        )
    max_window_days = snapshot.get_int("screening.max_window_days")
    timeout_ms = snapshot.get_int("screening.statement_timeout_ms")
    cache_ttl = snapshot.get_int("screening.cache_ttl_seconds")

    quota = ScreenQuota(container.redis, container.settings_service)
    try:
        allowed, retry_after, reason = quota.check_and_consume(user)
    except QuotaUnavailableError:
        raise api_error(503, "quota_unavailable", "配额服务暂不可用") from None
    if not allowed:
        detail = "筛选频率超限" if reason == "minute" else "今日筛选次数已用完"
        raise HTTPException(
            status_code=429,
            detail={"code": "quota_exceeded", "message": detail},
            headers={"Retry-After": str(retry_after)},
        )

    cache_key = _cache_key(payload)
    use_cache = cache_ttl > 0 and x_cache_control != "no-cache"
    if use_cache:
        try:
            cached = container.redis.get(cache_key)
            if cached:
                return ScreenResponse.model_validate(json.loads(cached))
        except RedisError:
            logger.warning("screen cache read failed", exc_info=True)

    try:
        result = build_screen(
            db,
            payload,
            max_window_days=max_window_days,
            statement_timeout_ms=timeout_ms,
        )
    except (FactorError, ScreenError) as exc:
        raise api_error(422, "screen_invalid", str(exc)) from None

    response = ScreenResponse.model_validate(result)
    if use_cache:
        try:
            container.redis.set(
                cache_key,
                json.dumps(jsonable_encoder(response), ensure_ascii=False),
                ex=cache_ttl,
            )
        except RedisError:
            logger.warning("screen cache write failed", exc_info=True)
    return response


@router.post("/screen/compare", response_model=CompareResponse)
def compare_screen(
    payload: CompareRequest,
    user: User = Depends(require_ready_user),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> CompareResponse:
    """检查单个标的是否满足当前条件，并列出不满足的条件（不占筛选配额）。"""

    snapshot = container.settings_service.snapshot
    if snapshot.get_bool("maintenance.enabled") and user.role != "admin":
        raise api_error(503, "maintenance", "系统维护中，请稍后再试")

    request = ScreenRequest(
        as_of=payload.as_of,
        logic=payload.logic,
        conditions=payload.conditions,
        sort=[],
        page=1,
        page_size=1,
    )
    try:
        result = compare_instrument(
            db,
            symbol=payload.symbol.strip(),
            request=request,
            max_window_days=snapshot.get_int("screening.max_window_days"),
            statement_timeout_ms=snapshot.get_int("screening.statement_timeout_ms"),
        )
    except (FactorError, ScreenError) as exc:
        raise api_error(422, "compare_invalid", str(exc)) from None
    return CompareResponse.model_validate(result)
