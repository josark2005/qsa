from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.daily_bar import DailyBar
from app.sync.bars import (
    backfill_baostock_bars,
    backfill_bse_bars,
    backfill_tencent_bars,
    recompute_limit_events,
)
from app.sync.calendar import sync_trading_calendar
from app.sync.cancellation import SyncCancelled
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
MARKET_OPEN_DATE = date(1990, 12, 19)
ProgressCallback = Callable[[dict], None]

FULL_STEPS = (
    "calendar",
    "universe",
    "tencent_stock",
    "tencent_etf",
    "baostock_stock",
    "baostock_etf",
    "bse",
    "limits",
    "financials",
    "shareholders",
    "quality_stock",
    "quality_etf",
)


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def _merge_verification(results: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "missing": 0,
        "samples": [],
    }
    for key in ("baostock_stock", "baostock_etf"):
        verification = (results.get(key) or {}).get("verification")
        if not verification:
            continue
        for field in ("checked", "matched", "mismatched", "missing"):
            merged[field] += int(verification.get(field) or 0)
        for sample in verification.get("samples") or []:
            if len(merged["samples"]) >= 20:
                break
            merged["samples"].append(sample)
    return merged


def run_full_backfill(
    db: Session,
    *,
    session_factory: sessionmaker,
    engine: Engine,
    start_date: date,
    end_date: date,
    limit: int | None = None,
    include_delisted: bool = False,
    asset_type: str = "all",
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    stall_timeout_seconds: Callable[[], float] | float | None = None,
    resume_from_step: str | None = None,
) -> dict[str, Any]:
    run_id = uuid.uuid4()
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    finished_steps: list[str] = []
    last_error: str | None = None
    skipped_steps: list[str] = []
    # 只有真正从中间步骤续跑时才跳过前置步骤；resume_from_step 指向第一步
    # （或不是合法步骤名）时等价于从头跑完每一步。
    resume_from = (
        resume_from_step
        if resume_from_step in FULL_STEPS and resume_from_step != FULL_STEPS[0]
        else None
    )
    resume_index = FULL_STEPS.index(resume_from) if resume_from else 0

    def stall_timeout() -> float:
        if stall_timeout_seconds is None:
            return 0.0
        if callable(stall_timeout_seconds):
            return float(stall_timeout_seconds())
        return float(stall_timeout_seconds)

    def emit(phase: str, message: str, **extra: Any) -> None:
        if progress_callback is not None:
            progress_callback(
                {"phase": phase, "message": message, **extra}
            )

    def step_progress(prefix: str, label: str):
        def callback(update: dict) -> None:
            payload = {key: value for key, value in update.items() if key != "phase"}
            emit(prefix, label, **payload)

        return callback

    def run_step(name: str, label: str, function):
        nonlocal last_error
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        if (
            resume_from is not None
            and name in FULL_STEPS
            and FULL_STEPS.index(name) < resume_index
        ):
            # 上次任务已经跑完这一步（见 sync_runs 的 full_backfill 步骤记录），
            # 自动续跑时直接跳过，避免几小时的前置步骤重跑。
            skipped_steps.append(name)
            emit(name, f"{label}（上次已完成，跳过）", percent=100)
            return None
        emit(name, label, percent=0)
        try:
            with StepRun(
                session_factory,
                pipeline="full_backfill",
                step=name,
                run_id=run_id,
                target_date=end_date,
                trigger="manual",
            ) as run:
                value = function()
                rows = value.get("rows") if isinstance(value, dict) else None
                run.finish(rows=rows, meta=value if isinstance(value, dict) else None)
            finished_steps.append(name)
            results[name] = value
            emit(name, f"{label}完成", percent=100)
            return value
        except SyncCancelled:
            raise
        except Exception as exc:
            db.rollback()
            last_error = f"{name}: {type(exc).__name__}: {exc}"
            errors[name] = f"{type(exc).__name__}: {exc}"
            logger.exception("full backfill step failed: %s", name)
            emit(name, f"{label}失败：{exc}", percent=100)
            return None

    today = _today()
    with advisory_lock(engine, "full_backfill") as acquired:
        if not acquired:
            return {"skipped": True, "reason": "lock_held"}

        run_step(
            "calendar",
            "更新交易日历",
            lambda: {
                "rows": sync_trading_calendar(
                    db,
                    start_date=date(today.year - 2, 1, 1),
                    end_date=date(today.year + 1, 12, 31),
                )
            },
        )
        run_step("universe", "更新证券列表", lambda: sync_universe(db))

        if asset_type in {"stock", "all"}:
            run_step(
                "tencent_stock",
                "Tencent 全量价格",
                lambda: backfill_tencent_bars(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    asset_type="stock",
                    include_delisted=include_delisted,
                    limit=limit,
                    full_refresh=True,
                    progress_callback=step_progress(
                        "tencent_stock", "Tencent 全量价格"
                    ),
                    cancel_check=cancel_check,
                    stall_timeout_seconds=stall_timeout(),
                ),
            )
        if asset_type in {"etf", "all"}:
            run_step(
                "tencent_etf",
                "Tencent ETF 全量价格",
                lambda: backfill_tencent_bars(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    asset_type="etf",
                    include_delisted=include_delisted,
                    limit=limit,
                    full_refresh=True,
                    progress_callback=step_progress(
                        "tencent_etf", "Tencent ETF 全量价格"
                    ),
                    cancel_check=cancel_check,
                    stall_timeout_seconds=stall_timeout(),
                ),
            )
        if asset_type in {"stock", "all"}:
            run_step(
                "baostock_stock",
                "BaoStock 全字段与多源核对",
                lambda: backfill_baostock_bars(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    asset_type="stock",
                    include_delisted=include_delisted,
                    limit=limit,
                    full_refresh=True,
                    verify_existing=True,
                    progress_callback=step_progress(
                        "baostock_stock", "BaoStock 全字段与多源核对"
                    ),
                    cancel_check=cancel_check,
                    stall_timeout_seconds=stall_timeout(),
                ),
            )
            run_step(
                "bse",
                "北交所行情",
                lambda: backfill_bse_bars(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    include_delisted=include_delisted,
                    limit=limit,
                    full_refresh=True,
                    progress_callback=step_progress("bse", "北交所行情"),
                    cancel_check=cancel_check,
                    stall_timeout_seconds=stall_timeout(),
                ),
            )
        if asset_type in {"etf", "all"}:
            run_step(
                "baostock_etf",
                "BaoStock ETF 全字段与多源核对",
                lambda: backfill_baostock_bars(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    asset_type="etf",
                    include_delisted=include_delisted,
                    limit=limit,
                    full_refresh=True,
                    verify_existing=True,
                    progress_callback=step_progress(
                        "baostock_etf", "BaoStock ETF 全字段与多源核对"
                    ),
                    cancel_check=cancel_check,
                    stall_timeout_seconds=stall_timeout(),
                ),
            )
        if asset_type in {"stock", "all"}:
            run_step(
                "limits",
                "重算涨跌停事件",
                lambda: {"rows": recompute_limit_events(db, start_date, end_date)},
            )

            periods = [
                value.strftime("%Y%m%d")
                for value in quarter_ends(end_date, 8)
            ]
            run_step(
                "financials",
                "回补财报",
                lambda: sync_financial_reports(db, periods=periods),
            )
            run_step(
                "shareholders",
                "回补股东户数",
                lambda: sync_shareholder_counts(
                    db, periods=["最新", *periods]
                ),
            )

        latest_bar_date = db.scalar(
            select(func.max(DailyBar.trade_date))
        )
        if latest_bar_date is not None:
            if asset_type in {"stock", "all"}:
                run_step(
                    "quality_stock",
                    "行情质量校验",
                    lambda: check_daily_bar_quality(
                        db, target_date=latest_bar_date, asset_type="stock"
                    ),
                )
            if asset_type in {"etf", "all"}:
                run_step(
                    "quality_etf",
                    "ETF 行情质量校验",
                    lambda: check_daily_bar_quality(
                        db, target_date=latest_bar_date, asset_type="etf"
                    ),
                )

        verification = _merge_verification(results)
        if verification["checked"] or verification["missing"]:
            emit(
                "verification",
                (
                    f"核对 {verification['checked']} 条，差异 "
                    f"{verification['mismatched']} 条，缺失 {verification['missing']} 条"
                ),
                percent=100,
                rows=verification["checked"],
                errors=0,
                current=f"差异 {verification['mismatched']}",
            )

    stall_timeouts = sum(
        int((value or {}).get("stall_timeouts") or 0)
        for value in results.values()
        if isinstance(value, dict)
    )
    if errors:
        emit("completed", f"{len(errors)} 个步骤异常", percent=100)
    return {
        "source": "full",
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "steps": {name: name in results for name in FULL_STEPS},
        "finished_steps": finished_steps,
        "skipped_steps": skipped_steps,
        "resumed": resume_from is not None,
        "resume_from_step": resume_from,
        "stall_timeouts": stall_timeouts,
        "last_error": last_error,
        "verification": verification,
        "errors": errors,
    }