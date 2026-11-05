from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
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
from app.core.config import get_config
from app.core.container import Container
from app.core.db import utcnow
from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.history_import import HistoryImport
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
from app.services.history_archive import DEFAULT_HISTORY_DIR

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
    financial_totals = db.execute(
        select(
            func.min(FinancialReport.report_period),
            func.max(FinancialReport.report_period),
            func.count(),
        ).select_from(FinancialReport)
    ).one()
    bar_totals = db.execute(
        select(
            func.min(DailyBar.trade_date),
            func.max(DailyBar.trade_date),
            func.count(),
        )
        .select_from(DailyBar)
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(Instrument.asset_type.in_(("stock", "etf")))
    ).one()

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
    holder_totals = db.execute(
        select(
            func.min(ShareholderCount.period_end),
            func.max(ShareholderCount.period_end),
            func.count(),
        ).select_from(ShareholderCount)
    ).one()

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    latest_calendar = db.scalar(
        select(func.max(TradingCalendar.cal_date)).where(
            TradingCalendar.is_open.is_(True)
        )
    )
    earliest_calendar = db.scalar(
        select(func.min(TradingCalendar.cal_date)).where(
            TradingCalendar.is_open.is_(True)
        )
    )
    calendar_open_days = int(
        db.scalar(
            select(func.count())
            .select_from(TradingCalendar)
            .where(TradingCalendar.is_open.is_(True))
        )
        or 0
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

    imported_files, imported_rows = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(HistoryImport.rows), 0),
        ).select_from(HistoryImport)
    ).one()
    config = get_config()
    history_dir = (
        Path(config.history_dir) if config.history_dir else DEFAULT_HISTORY_DIR
    )
    archive_paths = []
    if history_dir.exists():
        archive_paths = [
            path
            for path in history_dir.glob("*.db")
            if path.name == "qsa-reference.db"
            or (
                len(path.stem) == 7
                and path.stem[4] == "-"
                and path.stem[:4].isdigit()
                and path.stem[5:].isdigit()
            )
        ]
    archive_files = len(archive_paths)
    archive_bytes = sum(path.stat().st_size for path in archive_paths)
    bar_rows = int(bar_totals[2] or 0)
    marker_count = int(imported_files or 0)
    marker_rows = int(imported_rows or 0)
    archive_expected_rows = 0
    manifest_path = history_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            archive_expected_rows = int(manifest.get("daily_bar_rows") or 0)
        except (OSError, ValueError, TypeError):
            logger.warning("failed to read history manifest %s", manifest_path)
    if archive_expected_rows == 0:
        archive_expected_rows = marker_rows
    needs_force_import = (
        archive_files > 0
        and archive_expected_rows > 0
        and bar_rows < int(archive_expected_rows * 0.9)
    )
    if (
        not needs_force_import
        and archive_files > 0
        and archive_expected_rows == 0
        and bar_rows < 100_000
    ):
        needs_force_import = True

    return {
        "instruments": instrument_counts,
        "calendar_latest_open_date": latest_calendar,
        "calendar_latest_past_open_date": latest_past_calendar,
        "calendar_min_date": earliest_calendar,
        "calendar_max_date": latest_calendar,
        "calendar_open_days": calendar_open_days,
        "daily_bars": {
            "stock": {"latest_date": latest_stock_bar, "rows": stock_bar_count},
            "etf": {"latest_date": latest_etf_bar, "rows": etf_bar_count},
            "total_rows": int(bar_totals[2] or 0),
            "min_date": bar_totals[0],
            "max_date": bar_totals[1],
        },
        "financial_reports": {
            "latest_period": latest_financial[0] if latest_financial else None,
            "latest_announce_date": latest_financial[1] if latest_financial else None,
            "rows": financial_count,
            "total_rows": int(financial_totals[2] or 0),
            "period_min": financial_totals[0],
            "period_max": financial_totals[1],
        },
        "shareholder_counts": {
            "latest_period": holder_choice[0] if holder_choice else None,
            "latest_announce_date": holder_announce,
            "rows": holder_count,
            "total_rows": int(holder_totals[2] or 0),
            "period_min": holder_totals[0],
            "period_max": holder_totals[1],
            "periods": [
                {"period_end": period_end, "rows": int(count)}
                for period_end, count in holder_periods
            ],
        },
        "history": {
            "directory": str(history_dir),
            "archive_files": archive_files,
            "archive_bytes": archive_bytes,
            "imported_files": marker_count,
            "imported_rows": marker_rows,
            "archive_expected_rows": archive_expected_rows,
            "pending_files": max(archive_files - marker_count, 0),
            "daily_bar_rows": bar_rows,
            "needs_import": archive_files > 0
            and (marker_count == 0 or archive_files > marker_count),
            "needs_force_import": needs_force_import,
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
    if payload.kind == "history_import":
        active = db.scalars(
            select(SyncRequest).where(
                SyncRequest.kind == "history_import",
                SyncRequest.status.in_(["queued", "running", "cancelling"]),
            )
        ).all()
        if active:
            raise api_error(
                409,
                "history_import_active",
                "已有历史数据导入任务在队列或执行中",
            )
    if payload.kind == "repair_adj_factors":
        active = db.scalars(
            select(SyncRequest).where(
                SyncRequest.kind == "repair_adj_factors",
                SyncRequest.status.in_(["queued", "running", "cancelling"]),
            )
        ).all()
        if active:
            raise api_error(
                409,
                "repair_adj_factors_active",
                "已有复权因子修复任务在队列或执行中",
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


def _force_cancel_stuck(
    db: Session,
    entry: SyncRequest,
    container: Container,
) -> bool:
    """停止请求迟迟不生效时强制收敛（看门狗兜底）。

    worker 只有在两次网络请求之间才会读到 cancelling，数据源卡死时可能几十分钟
    都读不到。管理员点了停止、宽限期内后台仍未收尾（状态没变成 cancelled）时，
    这里直接把任务标记为已停止，界面不再无限停在「停止中」。后台线程可能还在
    收尾，所以日志里会留一条警告，运行日志可以继续观察。
    """
    if entry.status != "cancelling":
        return False
    grace = container.settings_service.snapshot.get_int(
        "sync.cancel_grace_seconds"
    )
    reference = entry.updated_at or entry.started_at or entry.created_at
    if reference is None:
        return False
    now = utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=now.tzinfo)
    if (now - reference).total_seconds() < grace:
        return False
    entry.status = "cancelled"
    entry.finished_at = now
    entry.error = f"用户停止任务（超过 {grace} 秒未收敛，已强制停止）"
    progress = dict(entry.progress or {})
    progress.update(
        {
            "phase": "cancelled",
            "message": "已停止（强制）",
            "cancel_forced": True,
        }
    )
    entry.progress = progress
    entry.updated_at = now
    db.commit()
    logger.warning(
        "sync request %s force-cancelled after %ss of cancelling",
        entry.id,
        grace,
    )
    return True


@router.post("/requests/{request_id}/cancel", response_model=SyncRequestRead)
def cancel_sync_request(
    request_id: int,
    request: Request,
    actor: User = Depends(require_admin),
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> SyncRequestRead:
    entry = db.get(SyncRequest, request_id)
    if entry is None:
        raise api_error(404, "sync_request_not_found", "同步请求不存在")
    if entry.status in {"success", "failed", "cancelled"}:
        raise api_error(409, "sync_request_finished", "任务已经结束，无法停止")
    if entry.status == "cancelling":
        if _force_cancel_stuck(db, entry, container):
            add_audit(
                db,
                actor_id=actor.id,
                action="sync.cancel_forced",
                target_type="sync_request",
                target_id=str(entry.id),
                before={"status": "cancelling"},
                after={"status": "cancelled", "forced": True},
                ip=get_client_ip(request),
            )
            db.commit()
        return SyncRequestRead.model_validate(entry)

    previous_status = entry.status
    if entry.status == "queued":
        entry.status = "cancelled"
        entry.finished_at = utcnow()
        entry.error = "用户停止任务"
        entry.progress = {"phase": "cancelled", "message": "已停止"}
    elif entry.status == "running":
        entry.status = "cancelling"
    else:
        raise api_error(409, "sync_request_not_running", "任务当前不可停止")
    entry.updated_at = utcnow()

    add_audit(
        db,
        actor_id=actor.id,
        action="sync.cancel",
        target_type="sync_request",
        target_id=str(entry.id),
        before={"status": previous_status},
        after={"status": entry.status},
        ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(entry)
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
    # 轮询列表时顺便收敛「停止中」超时的任务，用户不用再手动点一次停止。
    if any(item.status == "cancelling" for item in items):
        forced = [
            item for item in items if _force_cancel_stuck(db, item, container)
        ]
        if forced:
            logger.warning(
                "force-cancelled %s stuck sync requests: %s",
                len(forced),
                ", ".join(str(item.id) for item in forced),
            )
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