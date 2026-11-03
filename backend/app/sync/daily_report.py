from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.core.db import utcnow
from app.core.settings_service import SettingsService
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.qq_group_subscription import QQGroupSubscription
from app.qq_bot.client import ChatTarget, QQBotClient
from app.schemas.screening import ConditionIn
from app.services.report_pdf import build_report_pdf
from app.services.screen_reports import create_report_snapshot
from app.sync.bars import (
    backfill_baostock_bars,
    backfill_bse_bars,
    recompute_limit_events,
)
from app.sync.calendar import sync_trading_calendar
from app.sync.financials import (
    quarter_ends,
    sync_financial_reports,
    sync_shareholder_counts,
)
from app.sync.quality import check_daily_bar_quality
from app.sync.runs import StepRun
from app.sync.universe import sync_universe

logger = logging.getLogger(__name__)


def _missing_symbols(
    db: Session,
    *,
    target_date: date,
    asset_type: str,
    exchanges: tuple[str, ...],
) -> list[str]:
    instruments = set(
        db.scalars(
            select(Instrument.symbol).where(
                Instrument.asset_type == asset_type,
                Instrument.status == "active",
                Instrument.exchange.in_(exchanges),
            )
        ).all()
    )
    existing = set(
        db.scalars(
            select(Instrument.symbol)
            .join(DailyBar, DailyBar.instrument_id == Instrument.id)
            .where(
                DailyBar.trade_date == target_date,
                Instrument.asset_type == asset_type,
                Instrument.exchange.in_(exchanges),
            )
        ).all()
    )
    return sorted(instruments - existing)


def _default_conditions(settings: SettingsService) -> list[ConditionIn]:
    try:
        raw = json.loads(settings.snapshot.get_str("screening.default_conditions"))
        conditions = [ConditionIn.model_validate(item) for item in raw]
        if conditions:
            return conditions
    except Exception:
        logger.warning("invalid default conditions; using built-in defaults", exc_info=True)
    return [
        ConditionIn(field="holder_change_pct", op="lte", value=-5, params={}),
        ConditionIn(field="revenue_yoy", op="gte", value=20, params={}),
    ]


def _subscribed_targets(db: Session) -> list[ChatTarget]:
    groups = db.scalars(
        select(QQGroupSubscription.group_openid).where(
            QQGroupSubscription.active.is_(True)
        )
    ).all()
    return [
        ChatTarget(kind="group", openid=str(group), msg_id="", actor_openid=str(group))
        for group in groups
    ]


def _notify(
    client: QQBotClient,
    targets: list[ChatTarget],
    text: str,
) -> None:
    if not client.configured:
        return
    for target in targets:
        try:
            client.send_text(target, text)
        except Exception:
            logger.exception("daily report QQ notification failed for %s", target.openid)


def _coverage(
    db: Session,
    *,
    target_date: date,
    asset_type: str,
) -> tuple[int, int, float]:
    expected = int(
        db.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(
                Instrument.asset_type == asset_type,
                Instrument.status == "active",
            )
        )
        or 0
    )
    actual = int(
        db.scalar(
            select(func.count(func.distinct(Instrument.id)))
            .select_from(Instrument)
            .join(DailyBar, DailyBar.instrument_id == Instrument.id)
            .where(
                DailyBar.trade_date == target_date,
                Instrument.asset_type == asset_type,
                Instrument.status == "active",
            )
        )
        or 0
    )
    ratio = actual / expected if expected else 1.0
    return expected, actual, ratio


def _data_ready(
    db: Session,
    target_date: date,
) -> tuple[bool, str]:
    latest = db.scalar(
        select(func.max(DailyBar.trade_date))
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(Instrument.asset_type == "stock")
    )
    stock_expected, stock_actual, stock_ratio = _coverage(
        db, target_date=target_date, asset_type="stock"
    )
    etf_expected, etf_actual, etf_ratio = _coverage(
        db, target_date=target_date, asset_type="etf"
    )
    reason = (
        f"数据日期 {latest or '—'}，目标日 {target_date.isoformat()}；"
        f"行情覆盖 {stock_actual + etf_actual}/{stock_expected + etf_expected}；"
        f"日线 {stock_actual}/{stock_expected}（{stock_ratio:.1%}），"
        f"ETF {etf_actual}/{etf_expected}（{etf_ratio:.1%}）"
    )
    ready = (
        latest is not None
        and latest >= target_date
        and stock_ratio >= 0.98
        and etf_ratio >= 0.98
    )
    return ready, reason


def run_daily_report_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    settings: SettingsService,
    redis_client: Any,
    trigger: str = "schedule",
) -> dict[str, Any]:
    config = get_config()
    run_id = uuid.uuid4()
    snapshot = settings.snapshot
    app_id = (snapshot.get_str("qq_bot.app_id") or config.qq_bot_app_id).strip()
    app_secret = (
        snapshot.get_str("qq_bot.app_secret") or config.qq_bot_app_secret
    ).strip()
    client = QQBotClient(
        app_id=app_id,
        app_secret=app_secret,
        redis_client=redis_client,
        api_base=config.qq_bot_api_base,
    )
    targets = _subscribed_targets(db)
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    _notify(
        client,
        targets,
        f"QSA 每日数据拉取开始\n目标交易日：{target_date.isoformat()}",
    )

    def run_step(step: str, function):
        try:
            with StepRun(
                session_factory,
                pipeline="daily_report",
                step=step,
                run_id=run_id,
                target_date=target_date,
                trigger=trigger,
            ) as run:
                value = function()
                rows = value.get("rows") if isinstance(value, dict) else None
                run.finish(rows=rows, meta=value if isinstance(value, dict) else None)
            results[step] = value
            return value
        except Exception as exc:
            db.rollback()
            errors[step] = f"{type(exc).__name__}: {exc}"
            logger.exception("daily report step failed: %s", step)
            return None

    run_step(
        "calendar",
        lambda: {
            "rows": sync_trading_calendar(
                db,
                start_date=date(target_date.year - 1, 1, 1),
                end_date=date(target_date.year + 1, 12, 31),
            )
        },
    )
    run_step("universe", lambda: sync_universe(db))

    missing_stock = _missing_symbols(
        db, target_date=target_date, asset_type="stock", exchanges=("SH", "SZ")
    )
    missing_etf = _missing_symbols(
        db, target_date=target_date, asset_type="etf", exchanges=("SH", "SZ")
    )
    missing_bse = _missing_symbols(
        db, target_date=target_date, asset_type="stock", exchanges=("BJ",)
    )
    if missing_stock:
        run_step(
            "bars_stock",
            lambda: backfill_baostock_bars(
                db,
                start_date=target_date,
                end_date=target_date,
                asset_type="stock",
                symbols=missing_stock,
            ),
        )
    if missing_etf:
        run_step(
            "bars_etf",
            lambda: backfill_baostock_bars(
                db,
                start_date=target_date,
                end_date=target_date,
                asset_type="etf",
                symbols=missing_etf,
            ),
        )
    if missing_bse:
        run_step(
            "bars_bse",
            lambda: backfill_bse_bars(
                db,
                start_date=target_date,
                end_date=target_date,
                symbols=missing_bse,
            ),
        )

    run_step(
        "limits",
        lambda: {"rows": recompute_limit_events(db, target_date, target_date)},
    )

    periods = [value.strftime("%Y%m%d") for value in quarter_ends(target_date, 2)]
    run_step(
        "financials",
        lambda: sync_financial_reports(db, periods=periods),
    )
    run_step(
        "shareholders",
        lambda: sync_shareholder_counts(db, periods=["最新", *periods]),
    )
    run_step(
        "quality_stock",
        lambda: check_daily_bar_quality(
            db, target_date=target_date, asset_type="stock"
        ),
    )
    run_step(
        "quality_etf",
        lambda: check_daily_bar_quality(
            db, target_date=target_date, asset_type="etf"
        ),
    )

    ready, readiness = _data_ready(db, target_date)
    if not ready:
        _notify(
            client,
            targets,
            f"QSA 每日选股未推送：数据校验未通过。\n{readiness}",
        )
        return {
            "target_date": target_date.isoformat(),
            "data_ready": False,
            "readiness": readiness,
            "subscribed_groups": len(targets),
            "report_token": None,
            "notified": not not targets and client.configured,
            "errors": errors,
        }

    report = None
    try:
        report = create_report_snapshot(
            db,
            logic="and",
            conditions=_default_conditions(settings),
            sorts=[],
            owner_id=None,
            title=f"每日选股 {target_date.isoformat()}",
            as_of=target_date,
            max_window_days=snapshot.get_int("screening.max_window_days"),
            statement_timeout_ms=snapshot.get_int(
                "screening.statement_timeout_ms"
            ),
        )
        db.commit()
        base_url = (
            snapshot.get_str("general.public_base_url")
            or config.qq_bot_public_base_url
        )
        link = f"{base_url.rstrip('/')}/reports/{report.token}"
        file_name = (
            f"QSA_Daily_{target_date.isoformat()}_{report.total}.pdf"
        )
        detail = (
            "PDF 仅含汇总表（入选超过 50 只）。"
            if report.total > 50
            else "PDF 含每只入选标的的详情页。"
        )
        _notify(
            client,
            targets,
            f"QSA 每日选股完成\n交易日：{target_date.isoformat()}\n"
            f"入选：{report.total} 只\n报表：{link}\n{detail}",
        )
        try:
            pdf_path = build_report_pdf(
                db, report, public_base_url=base_url
            )
            if client.configured:
                for target in targets:
                    try:
                        file_info = client.upload_file(
                            target, pdf_path, file_name=file_name
                        )
                        client.send_file(target, file_info)
                    except Exception:
                        logger.exception(
                            "daily report PDF delivery failed for %s",
                            target.openid,
                        )
                        _notify(
                            client,
                            [target],
                            "PDF 发送失败，请使用上方报表链接查看。",
                        )
        except Exception:
            logger.exception("daily report PDF build failed")
            _notify(client, targets, "PDF 生成失败，请使用上方报表链接查看。")
        for subscription in db.scalars(
            select(QQGroupSubscription).where(
                QQGroupSubscription.active.is_(True)
            )
        ).all():
            subscription.last_pushed_at = utcnow()
        db.commit()
    except Exception as exc:
        logger.exception("daily report generation failed")
        errors["report"] = f"{type(exc).__name__}: {exc}"
        _notify(client, targets, f"QSA 每日选股失败：{exc}")

    if errors:
        _notify(
            client,
            targets,
            f"每日拉取完成，但有 {len(errors)} 个步骤异常："
            + "、".join(errors.keys()),
        )

    return {
        "target_date": target_date.isoformat(),
        "data_ready": True,
        "readiness": readiness,
        "missing": {
            "stock": len(missing_stock),
            "etf": len(missing_etf),
            "bse": len(missing_bse),
        },
        "report_token": report.token if report is not None else None,
        "subscribed_groups": len(targets),
        "notified": bool(targets) and client.configured,
        "errors": errors,
    }
