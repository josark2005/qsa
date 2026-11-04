from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.trading_calendar import TradingCalendar
from app.sync.bars import (
    MARKET_CLOSE_TIME,
    MARKET_DATA_READY_TIME,
    backfill_baostock_bars,
    backfill_bse_bars,
    recompute_limit_events,
    sync_incremental_bars,
)
from app.sync.calendar import (
    is_trading_day,
    latest_trading_day,
    sync_trading_calendar,
)
from app.sync.cancellation import SyncCancelled
from app.sync.daily_report import (
    DATA_PIPELINE,
    REPORT_PIPELINE,
    run_daily_data_pipeline,
    run_daily_report_pipeline,
)
from app.sync.financials import (
    quarter_ends,
    sync_financial_reports,
    sync_shareholder_counts,
)
from app.sync.quality import check_daily_bar_quality
from app.sync.runs import StepRun, advisory_lock
from app.sync.universe import sync_universe

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

PIPELINE_NAMES = (
    "market",
    "etf",
    "financial",
    "financial_morning",
    "weekly",
    DATA_PIPELINE,
    REPORT_PIPELINE,
)
# 每日流水线分两段：先拉数据（daily_data），再筛选汇报（daily_report）。
DAILY_PIPELINES = (DATA_PIPELINE, REPORT_PIPELINE)


def today_shanghai() -> date:
    return datetime.now(SHANGHAI).date()


def daily_report_cutoff(today: date, now: datetime) -> date:
    """日报目标日上界：15:00 收盘后取当天，收盘前只取到前一天。"""

    return today if now.time() >= MARKET_CLOSE_TIME else today - timedelta(days=1)


def _ensure_not_cancelled(cancel_check) -> None:
    if cancel_check is not None and cancel_check():
        raise SyncCancelled("用户停止任务")


def ensure_calendar(db: Session) -> None:
    today = today_shanghai()
    count = int(
        db.scalar(
            select(func.count())
            .select_from(TradingCalendar)
            .where(
                TradingCalendar.cal_date >= date(today.year - 2, 1, 1),
                TradingCalendar.cal_date <= date(today.year + 1, 12, 31),
            )
        )
        or 0
    )
    if count == 0:
        sync_trading_calendar(
            db,
            start_date=date(2010, 1, 1),
            end_date=date(today.year + 1, 12, 31),
        )


def _sync_calendar_step(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
) -> dict:
    today = today_shanghai()
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step="calendar",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        rows = sync_trading_calendar(
            db,
            start_date=date(today.year - 2, 1, 1),
            end_date=date(today.year + 1, 12, 31),
        )
        run.finish(rows=rows)
        return {"calendar_rows": rows}


def _sync_universe_step(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
) -> dict:
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step="universe",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        result = sync_universe(db)
        run.finish(rows=result["rows"], meta=result)
        return result


def _sync_bars_for_day(
    db: Session,
    *,
    target_date: date,
    asset_type: str,
    limit: int | None = None,
) -> dict:
    today = today_shanghai()
    if target_date == today:
        now = datetime.now(SHANGHAI)
        if now.time() < MARKET_DATA_READY_TIME:
            raise RuntimeError("市场尚未收盘，不能同步当日行情")
        result = sync_incremental_bars(
            db,
            target_date=target_date,
            asset_type=asset_type,
            limit=limit,
        )
        if result.get("rows", 0) > 0:
            return {**result, "mode": "incremental"}
        logger.warning("incremental returned no rows; falling back to backfill")

    if asset_type == "etf":
        result = backfill_baostock_bars(
            db,
            start_date=target_date,
            end_date=target_date,
            asset_type="etf",
            limit=limit,
        )
        return {**result, "mode": "backfill"}
    if asset_type == "stock":
        stock = backfill_baostock_bars(
            db,
            start_date=target_date,
            end_date=target_date,
            asset_type="stock",
            limit=limit,
        )
        bse = backfill_bse_bars(
            db,
            start_date=target_date,
            end_date=target_date,
            limit=limit,
        )
        return {
            "mode": "backfill",
            "rows": stock["rows"] + bse["rows"],
            "stock": stock,
            "bse": bse,
            "errors": stock["errors"] + bse["errors"],
        }
    raise ValueError(f"unsupported asset_type: {asset_type}")


def _sync_bars_step(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
    asset_type: str,
    limit: int | None = None,
) -> dict:
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step=f"bars_{asset_type}",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        result = _sync_bars_for_day(
            db, target_date=target_date, asset_type=asset_type, limit=limit
        )
        run.finish(rows=int(result.get("rows", 0)), meta=result)
        return result


def _sync_limits_step(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
    start_date: date | None = None,
) -> dict:
    start = start_date or target_date
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step="limit_events",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        rows = recompute_limit_events(db, start, target_date)
        run.finish(rows=rows)
        return {"limit_events": rows, "start_date": start.isoformat()}


def _quality_step(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
    asset_type: str,
) -> dict:
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step=f"quality_{asset_type}",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        result = check_daily_bar_quality(
            db, target_date=target_date, asset_type=asset_type
        )
        run.finish(rows=result["actual"], meta=result)
        return result


def _run_financial_steps(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    run_id: uuid.UUID,
    target_date: date,
    trigger: str,
    period_count: int,
    include_historical_holders: bool,
) -> dict:
    periods = [value.strftime("%Y%m%d") for value in quarter_ends(target_date, period_count)]
    results: dict[str, Any] = {}
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step="financial_reports",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        results["financials"] = sync_financial_reports(db, periods=periods)
        run.finish(
            rows=results["financials"]["rows"],
            meta=results["financials"],
        )

    holder_periods = ["最新"]
    if include_historical_holders:
        holder_periods.extend(periods)
    with StepRun(
        session_factory,
        pipeline=pipeline,
        step="shareholders",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        results["shareholders"] = sync_shareholder_counts(db, periods=holder_periods)
        run.finish(
            rows=results["shareholders"]["rows"],
            meta=results["shareholders"],
        )
    return results


def run_market_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    trigger: str,
    limit: int | None = None,
) -> dict:
    run_id = uuid.uuid4()
    results: dict[str, Any] = {}
    results.update(
        _sync_calendar_step(
            db,
            session_factory,
            pipeline="market",
            run_id=run_id,
            target_date=target_date,
            trigger=trigger,
        )
    )
    results["universe"] = _sync_universe_step(
        db,
        session_factory,
        pipeline="market",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    )
    results["bars"] = _sync_bars_step(
        db,
        session_factory,
        pipeline="market",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="stock",
        limit=limit,
    )
    results["limits"] = _sync_limits_step(
        db,
        session_factory,
        pipeline="market",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    )
    results["quality"] = _quality_step(
        db,
        session_factory,
        pipeline="market",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="stock",
    )
    return results


def run_etf_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    trigger: str,
    limit: int | None = None,
) -> dict:
    run_id = uuid.uuid4()
    results: dict[str, Any] = {}
    results.update(
        _sync_calendar_step(
            db,
            session_factory,
            pipeline="etf",
            run_id=run_id,
            target_date=target_date,
            trigger=trigger,
        )
    )
    results["universe"] = _sync_universe_step(
        db,
        session_factory,
        pipeline="etf",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    )
    results["bars"] = _sync_bars_step(
        db,
        session_factory,
        pipeline="etf",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="etf",
        limit=limit,
    )
    results["quality"] = _quality_step(
        db,
        session_factory,
        pipeline="etf",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="etf",
    )
    return results


def run_financial_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    trigger: str,
    period_count: int = 2,
    pipeline: str = "financial",
    include_historical_holders: bool = False,
) -> dict:
    run_id = uuid.uuid4()
    return _run_financial_steps(
        db,
        session_factory,
        pipeline=pipeline,
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        period_count=period_count,
        include_historical_holders=include_historical_holders,
    )


def run_weekly_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    trigger: str,
) -> dict:
    run_id = uuid.uuid4()
    results: dict[str, Any] = {}
    results.update(
        _sync_calendar_step(
            db,
            session_factory,
            pipeline="weekly",
            run_id=run_id,
            target_date=target_date,
            trigger=trigger,
        )
    )
    results["universe"] = _sync_universe_step(
        db,
        session_factory,
        pipeline="weekly",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    )
    start = target_date - timedelta(days=14)
    with StepRun(
        session_factory,
        pipeline="weekly",
        step="bars_recent",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
    ) as run:
        stock = backfill_baostock_bars(
            db, start_date=start, end_date=target_date, asset_type="stock"
        )
        etf = backfill_baostock_bars(
            db, start_date=start, end_date=target_date, asset_type="etf"
        )
        bse = backfill_bse_bars(db, start_date=start, end_date=target_date)
        recent = {
            "rows": stock["rows"] + etf["rows"] + bse["rows"],
            "stock": stock,
            "etf": etf,
            "bse": bse,
        }
        results["bars_recent"] = recent
        run.finish(rows=recent["rows"], meta=recent)
    results["limits"] = _sync_limits_step(
        db,
        session_factory,
        pipeline="weekly",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        start_date=target_date - timedelta(days=30),
    )
    results["financials"] = _run_financial_steps(
        db,
        session_factory,
        pipeline="weekly",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        period_count=4,
        include_historical_holders=True,
    )
    results["quality_stock"] = _quality_step(
        db,
        session_factory,
        pipeline="weekly",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="stock",
    )
    results["quality_etf"] = _quality_step(
        db,
        session_factory,
        pipeline="weekly",
        run_id=run_id,
        target_date=target_date,
        trigger=trigger,
        asset_type="etf",
    )
    return results


def _notify_watch_safely(
    db: Session,
    settings_service: Any,
    redis_client: Any,
    target_date: date,
) -> None:
    """财报/股东户数同步后推送关注标的公告；任何失败只记日志。"""

    if settings_service is None or redis_client is None:
        return
    try:
        from app.services.watch_notify import notify_watch_announcements

        result = notify_watch_announcements(
            db,
            settings=settings_service,
            redis_client=redis_client,
            target_date=target_date,
        )
        if result.get("notified"):
            logger.info(
                "watch announcements notified: %s items -> %s groups",
                result.get("items"),
                result.get("groups"),
            )
    except Exception:
        logger.exception("watch announcement notification failed")


def run_pipeline(
    name: str,
    *,
    session_factory: sessionmaker,
    engine: Engine,
    target_date: date | None = None,
    trigger: str = "manual",
    limit: int | None = None,
    settings_service: Any | None = None,
    redis_client: Any | None = None,
    cancel_check=None,
    progress_callback=None,
) -> dict:
    if name not in PIPELINE_NAMES:
        raise ValueError(f"unknown pipeline: {name}")

    _ensure_not_cancelled(cancel_check)
    with session_factory() as db:
        ensure_calendar(db)
        today = today_shanghai()
        if name in DAILY_PIPELINES:
            cutoff = daily_report_cutoff(today, datetime.now(SHANGHAI))
            current_target = target_date or latest_trading_day(db, cutoff) or cutoff
        else:
            current_target = target_date or latest_trading_day(db, today) or today
        if name in {"market", "etf", "weekly"} and not is_trading_day(
            db, current_target
        ):
            logger.warning("skip %s: %s is not a trading day", name, current_target)
            return {"skipped": True, "reason": "not_trading_day"}

    with advisory_lock(engine, f"pipeline:{name}") as acquired:
        if not acquired:
            return {"skipped": True, "reason": "lock_held"}
        with session_factory() as db:
            if name in DAILY_PIPELINES:
                if settings_service is None or redis_client is None:
                    raise ValueError(f"{name} requires settings and redis")
                runner = (
                    run_daily_data_pipeline
                    if name == DATA_PIPELINE
                    else run_daily_report_pipeline
                )
                return runner(
                    db,
                    session_factory,
                    target_date=current_target,
                    settings=settings_service,
                    redis_client=redis_client,
                    trigger=trigger,
                    cancel_check=cancel_check,
                    progress_callback=progress_callback,
                )
            if name == "market":
                return run_market_pipeline(
                    db,
                    session_factory,
                    target_date=current_target,
                    trigger=trigger,
                    limit=limit,
                )
            if name == "etf":
                return run_etf_pipeline(
                    db,
                    session_factory,
                    target_date=current_target,
                    trigger=trigger,
                    limit=limit,
                )
            if name == "financial":
                result = run_financial_pipeline(
                    db,
                    session_factory,
                    target_date=current_target,
                    trigger=trigger,
                    period_count=2,
                    pipeline="financial",
                )
                _notify_watch_safely(
                    db, settings_service, redis_client, current_target
                )
                return result
            if name == "financial_morning":
                result = run_financial_pipeline(
                    db,
                    session_factory,
                    target_date=current_target,
                    trigger=trigger,
                    period_count=2,
                    pipeline="financial_morning",
                )
                _notify_watch_safely(
                    db, settings_service, redis_client, current_target
                )
                return result
            return run_weekly_pipeline(
                db, session_factory, target_date=current_target, trigger=trigger
            )
