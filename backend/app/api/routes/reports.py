from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.indicators import INDICATOR_PARAMS, build_technical_payload
from app.api.deps import (
    api_error,
    get_client_ip,
    get_container,
    get_db,
    require_ready_user,
    verify_csrf,
)
from app.core.container import Container
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport
from app.models.user import User
from app.schemas.reports import (
    ReportCreateRequest,
    ReportCreateResponse,
    ReportHistoryItem,
    ReportHistoryResponse,
)
from app.screening.registry import FactorError
from app.screening.query_builder import ScreenError
from app.services.audit import add_audit
from app.services.report_pdf import build_report_pdf
from app.services.report_thumbnails import (
    THUMBNAIL_MAX_SYMBOLS,
    build_thumbnail_payload,
)
from app.services.screen_quota import QuotaUnavailableError, ScreenQuota
from app.services.screen_reports import (
    create_report_snapshot,
    latest_stock_bar_date,
    report_response,
    stale_reasons,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])
public_router = APIRouter(prefix="/public/reports", tags=["reports"])


def _get_report(db: Session, token: str) -> ScreenReport:
    report = db.scalar(select(ScreenReport).where(ScreenReport.token == token))
    if report is None:
        raise api_error(404, "report_not_found", "报表不存在或链接已失效")
    return report


@router.get("", response_model=ReportHistoryResponse)
def list_reports(
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_ready_user),
    db: Session = Depends(get_db),
) -> ReportHistoryResponse:
    filters = []
    if user.role != "admin":
        filters.append(ScreenReport.owner_id == user.id)
    if q:
        filters.append(ScreenReport.title.ilike(f"%{q.strip()}%"))

    total_statement = select(func.count()).select_from(ScreenReport)
    items_statement = select(ScreenReport, User.username).outerjoin(
        User, ScreenReport.owner_id == User.id
    )
    for condition in filters:
        total_statement = total_statement.where(condition)
        items_statement = items_statement.where(condition)
    total = int(db.scalar(total_statement) or 0)
    rows = db.execute(
        items_statement.order_by(
            ScreenReport.generated_at.desc(), ScreenReport.id.desc()
        )
        .offset(offset)
        .limit(limit)
    ).all()
    latest_trade_date = latest_stock_bar_date(db)
    items = []
    for report, owner_username in rows:
        reasons = stale_reasons(report, latest_trade_date)
        items.append(
            ReportHistoryItem(
                token=report.token,
                title=report.title,
                generated_at=report.generated_at,
                report_date=report.report_date,
                screen_as_of=report.screen_as_of,
                data_as_of=report.data_as_of,
                total=report.total,
                included=report.included,
                truncated=report.truncated,
                condition_views=report.condition_views,
                stale=bool(reasons),
                stale_reason="；".join(reasons) if reasons else None,
                owner_username=owner_username,
            )
        )
    return ReportHistoryResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        latest_trade_date=latest_trade_date,
    )


@router.post("", response_model=ReportCreateResponse)
def create_report(
    payload: ReportCreateRequest,
    request: Request,
    user: User = Depends(require_ready_user),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> ReportCreateResponse:
    snapshot = container.settings_service.snapshot
    if snapshot.get_bool("maintenance.enabled") and user.role != "admin":
        raise api_error(503, "maintenance", "系统维护中，请稍后再试")

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

    try:
        report = create_report_snapshot(
            db,
            logic=payload.logic,
            conditions=payload.conditions,
            sorts=payload.sort,
            owner_id=user.id,
            title=payload.title,
            as_of=payload.as_of,
            max_window_days=snapshot.get_int("screening.max_window_days"),
            statement_timeout_ms=snapshot.get_int(
                "screening.statement_timeout_ms"
            ),
        )
    except (FactorError, ScreenError) as exc:
        raise api_error(422, "screen_invalid", str(exc)) from None

    add_audit(
        db,
        actor_id=user.id,
        action="report.create",
        target_type="screen_report",
        target_id=report.token,
        after={
            "total": report.total,
            "included": report.included,
            "truncated": report.truncated,
            "screen_as_of": report.screen_as_of.isoformat(),
            "data_as_of": report.data_as_of.isoformat() if report.data_as_of else None,
        },
        ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(report)
    return report_response(db, report, latest_trade_date=report.data_as_of)


@public_router.get("/{token}", response_model=ReportCreateResponse)
def get_public_report(
    token: str,
    response: Response,
    db: Session = Depends(get_db),
) -> ReportCreateResponse:
    report = _get_report(db, token)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return report_response(db, report)


@public_router.get("/{token}/technical")
def get_report_technical(
    token: str,
    response: Response,
    symbol: str = Query(min_length=1, max_length=16),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    report = _get_report(db, token)
    symbols = {
        str(row.get("symbol"))
        for row in report.rows
        if isinstance(row, dict) and row.get("symbol")
    }
    if symbol not in symbols:
        raise api_error(404, "symbol_not_in_report", "该标的不在报表中")
    instrument = db.scalar(select(Instrument).where(Instrument.symbol == symbol))
    if instrument is None:
        raise api_error(404, "instrument_not_found", "证券不存在")

    as_of = report.data_as_of or report.screen_as_of
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "public, max-age=3600"
    payload = build_technical_payload(db, instrument, as_of)
    payload["indicator_params"] = INDICATOR_PARAMS
    return payload


@public_router.get("/{token}/thumbnails")
def get_report_thumbnails(
    token: str,
    response: Response,
    symbols: str = Query(min_length=1, max_length=2000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    requested: list[str] = []
    for part in symbols.split(","):
        symbol = part.strip()
        if symbol and symbol not in requested:
            requested.append(symbol)
    if not requested:
        raise api_error(422, "invalid_symbols", "请至少提供一个标的代码")
    if len(requested) > THUMBNAIL_MAX_SYMBOLS:
        raise api_error(
            422,
            "too_many_symbols",
            f"一次最多查询 {THUMBNAIL_MAX_SYMBOLS} 个标的",
        )

    report = _get_report(db, token)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return build_thumbnail_payload(db, report, requested)


@public_router.get("/{token}/pdf")
def get_report_pdf(
    token: str,
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> FileResponse:
    report = _get_report(db, token)
    base_url = (
        container.settings_service.snapshot.get_str("general.public_base_url")
        or container.config.qq_bot_public_base_url
    )
    path = build_report_pdf(db, report, public_base_url=base_url)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"qsa-report-{token[:8]}.pdf",
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "public, max-age=3600",
        },
    )
