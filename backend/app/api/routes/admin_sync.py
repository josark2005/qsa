from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
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
from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.shareholder_count import ShareholderCount
from app.models.sync_request import SyncRequest
from app.models.sync_run import SyncRun
from app.models.trading_calendar import TradingCalendar
from app.models.user import User
from app.schemas.sync import (
    SyncRequestCreate,
    SyncRequestListResponse,
    SyncRequestRead,
)
from app.services.audit import add_audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/sync",
    tags=["admin-sync"],
    dependencies=[Depends(require_admin)],
)


def _run_payload(run: SyncRun) -> dict:
    return {
        "id": run.id,
        "run_id": str(run.run_id),
        "pipeline": run.pipeline,
        "step": run.step,
        "status": run.status,
        "trigger": run.trigger,
        "target_date": run.target_date,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "rows_affected": run.rows_affected,
        "error": run.error,
        "meta": run.meta,
    }


@router.get("/status")
def sync_status(
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> dict:
    counts = db.execute(
        select(
            Instrument.asset_type,
            Instrument.status,
            func.count(),
        ).group_by(Instrument.asset_type, Instrument.status)
    ).all()
    instrument_counts = [
        {"asset_type": asset_type, "status": status, "count": count}
        for asset_type, status, count in counts
    ]

    latest_stock_bar = db.scalar(
        select(func.max(DailyBar.trade_date))
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(Instrument.asset_type == "stock")
    )
    latest_etf_bar = db.scalar(
        select(func.max(DailyBar.trade_date))
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(Instrument.asset_type == "etf")
    )
    stock_bar_count = (
        int(
            db.scalar(
                select(func.count())
                .select_from(DailyBar)
                .join(Instrument, Instrument.id == DailyBar.instrument_id)
                .where(
                    DailyBar.trade_date == latest_stock_bar,
                    Instrument.asset_type == "stock",
                )
            )
            or 0
        )
        if latest_stock_bar
        else 0
    )
    etf_bar_count = (
        int(
            db.scalar(
                select(func.count())
                .select_from(DailyBar)
                .join(Instrument, Instrument.id == DailyBar.instrument_id)
                .where(
                    DailyBar.trade_date == latest_etf_bar,
                    Instrument.asset_type == "etf",
                )
            )
            or 0
        )
        if latest_etf_bar
        else 0
    )

    latest_financial = db.execute(
        select(FinancialReport.report_period, FinancialReport.announce_date)
        .order_by(
            FinancialReport.report_period.desc(),
            FinancialReport.announce_date.desc(),
        )
        .limit(1)
    ).first()
    financial_count = (
        int(
            db.scalar(
                select(func.count())
                .select_from(FinancialReport)
                .where(FinancialReport.report_period == latest_financial[0])
            )
            or 0
        )
        if latest_financial
        else 0
    )

    holder_periods = db.execute(
        select(ShareholderCount.period_end, func.count())
        .group_by(ShareholderCount.period_end)
        .having(func.count() >= 1000)
        .order_by(ShareholderCount.period_end.desc())
        .limit(6)
    ).all()
    holder_choice = holder_periods[0] if holder_periods else None
    if holder_choice is None:
        fallback = db.execute(
            select(ShareholderCount.period_end, func.count())
            .group_by(ShareholderCount.period_end)
            .order_by(ShareholderCount.period_end.desc())
            .limit(1)
        ).first()
        if fallback is not None:
            holder_choice = fallback
            holder_periods = [fallback]
    holder_count = int(holder_choice[1]) if holder_choice else 0
    holder_announce = (
        db.scalar(
            select(func.max(ShareholderCount.announce_date)).where(
                ShareholderCount.period_end == holder_choice[0]
            )
        )
        if holder_choice
        else None
    )

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    latest_calendar = db.scalar(
        select(func.max(TradingCalendar.cal_date)).where(
            TradingCalendar.is_open.is_(True)
        )
    )
    latest_past_calendar = db.scalar(
        select(func.max(TradingCalendar.cal_date)).where(
            TradingCalendar.is_open.is_(True),
            TradingCalendar.cal_date <= today,
        )
    )
    recent_runs = db.scalars(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(15)
    ).all()

    return {
        "instruments": instrument_counts,
        "calendar_latest_open_date": latest_calendar,
        "calendar_latest_past_open_date": latest_past_calendar,
        "daily_bars": {
            "stock": {"latest_date": latest_stock_bar, "rows": stock_bar_count},
            "etf": {"latest_date": latest_etf_bar, "rows": etf_bar_count},
        },
        "financial_reports": {
            "latest_period": latest_financial[0] if latest_financial else None,
            "latest_announce_date": latest_financial[1] if latest_financial else None,
            "rows": financial_count,
        },
        "shareholder_counts": {
            "latest_period": holder_choice[0] if holder_choice else None,
            "latest_announce_date": holder_announce,
            "rows": holder_count,
            "periods": [
                {"period_end": period_end, "rows": int(count)}
                for period_end, count in holder_periods
            ],
        },
        "recent_runs": [_run_payload(run) for run in recent_runs],
        "worker_full_backfill_ready": (
            container.redis.get("qsa:worker:capabilities")
            == "full_backfill_v1"
        ),
        "loaded_at": container.settings_service.snapshot.loaded_at,
    }


@router.post("/requests", response_model=SyncRequestRead)
def create_sync_request(
    payload: SyncRequestCreate,
    request: Request,
    actor: User = Depends(require_admin),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> SyncRequestRead:
    if payload.kind == "full_backfill":
        if (
            container.redis.get("qsa:worker:capabilities")
            != "full_backfill_v1"
        ):
            raise api_error(
                409,
                "worker_outdated",
                "worker 尚未加载多源回补能力，请等待 worker 重启完成",
            )
        active = db.scalars(
            select(SyncRequest).where(
                SyncRequest.kind.in_(["backfill", "full_backfill"]),
                SyncRequest.status.in_(["queued", "running"]),
            )
        ).all()
        if active:
            raise api_error(
                409,
                "backfill_active",
                "已有回补任务在队列或执行中，请等待完成后再执行一键全量回补",
            )
    data = payload.model_dump(mode="json", exclude={"kind", "pipeline"})
    entry = SyncRequest(
        kind=payload.kind,
        pipeline=payload.pipeline,
        payload=data,
        status="queued",
        requested_by=actor.id,
    )
    db.add(entry)
    db.flush()
    add_audit(
        db,
        actor_id=actor.id,
        action="sync.request",
        target_type="sync_request",
        target_id=str(entry.id),
        after={
            "kind": payload.kind,
            "pipeline": payload.pipeline,
            "source": payload.source,
        },
        ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(entry)
    try:
        container.redis.publish("qsa:sync:requested", str(entry.id))
    except Exception:
        logger.warning("failed to publish sync request notification", exc_info=True)
    return SyncRequestRead.model_validate(entry)


@router.get("/requests", response_model=SyncRequestListResponse)
def list_sync_requests(
    kind: str | None = Query(default=None, max_length=16),
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> SyncRequestListResponse:
    max_limit = container.settings_service.snapshot.get_int(
        "screening.max_page_size"
    )
    limit = min(limit, max_limit)
    filters = []
    if kind:
        filters.append(SyncRequest.kind == kind)
    if status:
        filters.append(SyncRequest.status == status)
    total_statement = select(func.count()).select_from(SyncRequest)
    items_statement = select(SyncRequest)
    for condition in filters:
        total_statement = total_statement.where(condition)
        items_statement = items_statement.where(condition)
    total = int(db.scalar(total_statement) or 0)
    items = db.scalars(
        items_statement.order_by(SyncRequest.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return SyncRequestListResponse(
        items=[SyncRequestRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/runs")
def sync_runs(
    pipeline: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> dict:
    max_limit = container.settings_service.snapshot.get_int(
        "screening.max_page_size"
    )
    limit = min(limit, max_limit)
    filters = []
    if pipeline:
        filters.append(SyncRun.pipeline == pipeline)
    if status:
        filters.append(SyncRun.status == status)

    total_statement = select(func.count()).select_from(SyncRun)
    items_statement = select(SyncRun)
    for condition in filters:
        total_statement = total_statement.where(condition)
        items_statement = items_statement.where(condition)
    total = int(db.scalar(total_statement) or 0)
    runs = db.scalars(
        items_statement.order_by(SyncRun.started_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "items": [_run_payload(run) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
