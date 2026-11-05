from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from time import sleep
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.core.db import utcnow
from app.core.settings_service import SettingsService
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.personal_qq_group import PersonalQQGroup
from app.models.qq_group_subscription import QQGroupSubscription
from app.qq_bot.client import ChatTarget, QQBotClient
from app.qq_bot.onebot.client import OneBotClient
from app.schemas.screening import ConditionIn
from app.services.personal_qq import (
    build_onebot_client,
    personal_targets as list_personal_targets,
)
from app.services.report_pdf import build_report_pdf
from app.services.screen_reports import create_report_snapshot
from app.services.watch_notify import notify_watch_announcements
from app.sync.bars import (
    MARKET_CLOSE_TIME,
    MARKET_DATA_READY_TIME,
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
from app.sync.runs import StepRun
from app.sync.universe import sync_universe

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
# 等待行情落定时按小片 sleep，保证停止请求能在一秒级内生效。
WAIT_SLICE_SECONDS = 5.0

# 每日流水线分两段独立调度：先拉取数据，再按默认条件筛选并汇报。
DATA_PIPELINE = "daily_data"
REPORT_PIPELINE = "daily_report"
DAILY_PIPELINES = (DATA_PIPELINE, REPORT_PIPELINE)


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
    client: QQBotClient | None,
    targets: list[ChatTarget],
    text: str,
) -> None:
    if not _client_ready(client):
        return
    for target in targets:
        try:
            client.send_text(target, text)
        except Exception:
            logger.exception("daily report QQ notification failed for %s", target.openid)


def _notify_personal(
    client: OneBotClient | None,
    targets: list[ChatTarget],
    text: str,
) -> bool:
    """向个人号 OneBot 群推送纯文本；任何失败都只记录日志，不影响官方流程。"""

    if not _client_ready(client) or not targets:
        return False
    sent = False
    for target in targets:
        try:
            client.send_text(target, text)
            sent = True
        except Exception:
            logger.exception(
                "daily report personal QQ notification failed for %s",
                target.openid,
            )
    return sent


def _client_ready(client: Any) -> bool:
    """机器人未启用或未配置时不要尝试登录/发送。"""

    return client is not None and bool(getattr(client, "configured", False))


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


def _baostock_has_bars(
    target_date: date,
    symbols: list[str],
    asset_type: str,
) -> bool:
    """用少量标的探测 BaoStock 是否已有目标交易日日线。

    BaoStock 的日线通常在收盘后较晚才更新；15:00 档的日报如果在 BaoStock 上
    探测不到当日数据，就跳过全市场 BaoStock 空查询，直接走 Tencent 兜底。
    """

    if not symbols:
        return False
    try:
        from app.datasources.baostock import BaoStockClient

        with BaoStockClient() as client:
            for symbol in symbols[:5]:
                frame = client.fetch_history(
                    symbol,
                    target_date.isoformat(),
                    target_date.isoformat(),
                    asset_type=asset_type,
                )
                if not frame.empty:
                    return True
    except Exception:
        logger.warning(
            "探测 BaoStock 当日数据失败，改用 Tencent 兜底补齐",
            exc_info=True,
        )
    return False


def market_data_wait_seconds(target_date: date, now: datetime) -> float:
    """当日刚收盘时返回需要等待行情落定的秒数，其它情况返回 0。"""

    if target_date != now.date() or now.time() < MARKET_CLOSE_TIME:
        return 0.0
    ready_at = datetime.combine(
        target_date, MARKET_DATA_READY_TIME, tzinfo=SHANGHAI
    )
    return max(0.0, (ready_at - now).total_seconds())


def _wait_for_market_data(
    target_date: date,
    *,
    cancel_check=None,
    progress_callback: Callable[[dict], None] | None = None,
) -> None:
    """先等当日行情落定再拉取数据，避免日报用上未落定的行情。"""

    remaining = market_data_wait_seconds(target_date, datetime.now(SHANGHAI))
    if remaining <= 0:
        return
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "wait_market_data",
                "message": (
                    "等待当日行情落定"
                    f"（{MARKET_DATA_READY_TIME.strftime('%H:%M')}）后拉取数据"
                ),
                "percent": 0,
            }
        )
    while remaining > 0:
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        sleep(min(WAIT_SLICE_SECONDS, remaining))
        remaining -= WAIT_SLICE_SECONDS
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "wait_market_data",
                "message": "当日行情已落定，开始拉取数据",
                "percent": 100,
            }
        )


def _notify_settings(settings: SettingsService, redis_client: Any):
    """按设置构造通知用的 QQ 客户端；未启用的通道一律返回空。

    汇报推送只依赖通道配置、订阅记录与数据校验，不读取 LLM 聊天模式、
    做梦/休眠等会话状态：机器人在群里休眠时，定时选股汇报仍照常发送。
    """

    config = get_config()
    snapshot = settings.snapshot
    official_enabled = snapshot.get_bool("qq_bot.enabled")
    personal_enabled = snapshot.get_bool("personal_qq.enabled")
    client: QQBotClient | None = None
    official_ready = False
    targets: list[ChatTarget] = []
    if official_enabled:
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
        official_ready = client.configured
    personal_client: OneBotClient | None = None
    if personal_enabled:
        personal_client = build_onebot_client(snapshot, redis_client, config)
    return {
        "config": config,
        "snapshot": snapshot,
        "official_enabled": official_enabled,
        "official_ready": official_ready,
        "client": client,
        "personal_enabled": personal_enabled,
        "personal_client": personal_client,
    }


def _step_runner(
    db: Session,
    session_factory: sessionmaker,
    *,
    pipeline: str,
    target_date: date,
    trigger: str,
    cancel_check,
    progress_callback: Callable[[dict], None] | None,
):
    """返回 (results, errors, run_step)：统一记录 StepRun 与错误。"""

    run_id = uuid.uuid4()
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def run_step(step: str, function):
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        if progress_callback is not None:
            progress_callback(
                {"phase": step, "message": f"执行 {step}", "percent": 0}
            )
        try:
            with StepRun(
                session_factory,
                pipeline=pipeline,
                step=step,
                run_id=run_id,
                target_date=target_date,
                trigger=trigger,
            ) as run:
                value = function()
                rows = value.get("rows") if isinstance(value, dict) else None
                run.finish(rows=rows, meta=value if isinstance(value, dict) else None)
            results[step] = value
            if progress_callback is not None:
                progress_callback(
                    {"phase": step, "message": f"{step} 完成", "percent": 100}
                )
            return value
        except SyncCancelled:
            raise
        except Exception as exc:
            db.rollback()
            errors[step] = f"{type(exc).__name__}: {exc}"
            logger.exception("%s step failed: %s", pipeline, step)
            return None

    return results, errors, run_step


def _bar_progress(
    phase: str,
    *,
    cancel_check,
    progress_callback: Callable[[dict], None] | None,
):
    def callback(update: dict) -> None:
        if cancel_check is not None and cancel_check():
            raise SyncCancelled("用户停止任务")
        if progress_callback is not None:
            progress_callback(
                {
                    **update,
                    "phase": phase,
                    "message": f"{phase} 同步中",
                }
            )

    return callback


def run_daily_data_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    settings: SettingsService,
    redis_client: Any,
    trigger: str = "schedule",
    cancel_check=None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """只负责拉取数据：行情、涨跌停、财报/股东与关注标的公告，不做筛选推送。"""

    context = _notify_settings(settings, redis_client)
    config = context["config"]
    snapshot = context["snapshot"]
    client: QQBotClient | None = context["client"]
    official_ready = bool(context["official_ready"])
    personal_client: OneBotClient | None = context["personal_client"]
    targets = _subscribed_targets(db) if official_ready else []
    personal_groups = (
        list_personal_targets(db)
        if context["personal_enabled"]
        else []
    )

    results, errors, run_step = _step_runner(
        db,
        session_factory,
        pipeline=DATA_PIPELINE,
        target_date=target_date,
        trigger=trigger,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )

    _notify(
        client,
        targets,
        f"QSA 每日数据拉取开始\n目标交易日：{target_date.isoformat()}",
    )

    def bar_progress(phase: str):
        return _bar_progress(
            phase,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )

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

    # 15:00 收盘后立即触发时，先等当日行情落定（15:05）再拉取，
    # 保证后续筛选和推送用的是当天已落定的数据，而不是昨天的旧数据。
    _wait_for_market_data(
        target_date,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )

    missing_stock = _missing_symbols(
        db, target_date=target_date, asset_type="stock", exchanges=("SH", "SZ")
    )
    missing_etf = _missing_symbols(
        db, target_date=target_date, asset_type="etf", exchanges=("SH", "SZ")
    )
    # BaoStock 日线要到晚间才更新；先探测一次，没有目标日数据就直接走 Tencent，
    # 避免对全市场做一轮必然为空的 BaoStock 查询。
    probe_symbols = missing_stock or missing_etf
    baostock_ready = (
        _baostock_has_bars(
            target_date,
            probe_symbols,
            "stock" if missing_stock else "etf",
        )
        if probe_symbols
        else False
    )
    if probe_symbols and not baostock_ready and progress_callback is not None:
        progress_callback(
            {
                "phase": "bars_stock",
                "message": "BaoStock 尚无目标交易日数据，改用 Tencent 补齐",
                "percent": 0,
            }
        )

    def fill_missing_bars(
        asset_type: str,
        *,
        exchanges: tuple[str, ...],
        phase: str,
    ) -> list[str]:
        """补齐目标日缺失日线：BaoStock 可用时优先，否则/仍缺时用 Tencent 兜底。

        BaoStock 能同时返回正确的后复权因子；Tencent 兜底会继承上一根已知因子，
        不会把 adj_factor 写成 1.0（避免因子回退假跌）。
        """

        remaining = _missing_symbols(
            db,
            target_date=target_date,
            asset_type=asset_type,
            exchanges=exchanges,
        )
        if not remaining:
            return []
        if baostock_ready:
            run_step(
                f"{phase}_baostock",
                lambda: backfill_baostock_bars(
                    db,
                    start_date=target_date,
                    end_date=target_date,
                    asset_type=asset_type,
                    symbols=remaining,
                    progress_callback=bar_progress(f"{phase}_baostock"),
                    progress_every=5,
                ),
            )
            remaining = _missing_symbols(
                db,
                target_date=target_date,
                asset_type=asset_type,
                exchanges=exchanges,
            )
        if remaining:
            # BaoStock 登录失败或没有当日数据时用 Tencent 兜底。
            run_step(
                f"{phase}_tencent",
                lambda: backfill_tencent_bars(
                    db,
                    start_date=target_date,
                    end_date=target_date,
                    asset_type=asset_type,
                    symbols=remaining,
                    include_adjust_factors=False,
                    progress_callback=bar_progress(f"{phase}_tencent"),
                    progress_every=2,
                ),
            )
            remaining = _missing_symbols(
                db,
                target_date=target_date,
                asset_type=asset_type,
                exchanges=exchanges,
            )
        return remaining

    missing_stock = fill_missing_bars(
        "stock", exchanges=("SH", "SZ"), phase="bars_stock"
    )
    missing_etf = fill_missing_bars(
        "etf", exchanges=("SH", "SZ"), phase="bars_etf"
    )
    missing_bse = _missing_symbols(
        db, target_date=target_date, asset_type="stock", exchanges=("BJ",)
    )
    if missing_bse:
        run_step(
            "bars_bse",
            lambda: backfill_bse_bars(
                db,
                start_date=target_date,
                end_date=target_date,
                symbols=missing_bse,
                progress_callback=bar_progress("bars_bse"),
                progress_every=2,
            ),
        )
        missing_bse = _missing_symbols(
            db, target_date=target_date, asset_type="stock", exchanges=("BJ",)
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
    # 关注标的的财报/股东户数公告：数据同步完成后推送到已订阅的群（失败不影响主流程）。
    run_step(
        "watch_notify",
        lambda: notify_watch_announcements(
            db,
            session_factory=session_factory,
            settings=settings,
            redis_client=redis_client,
            config=config,
            target_date=target_date,
            official_client=client,
            official_targets=targets,
            personal_client=personal_client,
            personal_targets=personal_groups,
        ),
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
            f"QSA 每日数据拉取完成，但数据校验未通过，稍后的群汇报会跳过。\n{readiness}",
        )
        _notify_personal(
            personal_client,
            personal_groups,
            f"QSA 每日数据拉取完成，但数据校验未通过，稍后的群汇报会跳过。\n{readiness}",
        )
    elif errors:
        _notify(
            client,
            targets,
            f"每日数据拉取完成，但有 {len(errors)} 个步骤异常："
            + "、".join(errors.keys()),
        )
        _notify_personal(
            personal_client,
            personal_groups,
            f"每日数据拉取完成，但有 {len(errors)} 个步骤异常："
            + "、".join(errors.keys()),
        )

    return {
        "target_date": target_date.isoformat(),
        "data_ready": ready,
        "readiness": readiness,
        "missing": {
            "stock": len(missing_stock),
            "etf": len(missing_etf),
            "bse": len(missing_bse),
        },
        "errors": errors,
    }


def run_daily_report_pipeline(
    db: Session,
    session_factory: sessionmaker,
    *,
    target_date: date,
    settings: SettingsService,
    redis_client: Any,
    trigger: str = "schedule",
    cancel_check=None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """只负责汇报：按默认条件筛选、生成报表并推送到已启用的群。"""

    context = _notify_settings(settings, redis_client)
    config = context["config"]
    snapshot = context["snapshot"]
    official_enabled = bool(context["official_enabled"])
    official_ready = bool(context["official_ready"])
    personal_enabled = bool(context["personal_enabled"])
    client: QQBotClient | None = context["client"]
    personal_client: OneBotClient | None = context["personal_client"]
    targets = _subscribed_targets(db) if official_ready else []
    personal_groups = (
        list_personal_targets(db) if personal_enabled else []
    )

    results, errors, run_step = _step_runner(
        db,
        session_factory,
        pipeline=REPORT_PIPELINE,
        target_date=target_date,
        trigger=trigger,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    personal_notified = False

    ready, readiness = _data_ready(db, target_date)
    if not ready:
        _notify(
            client,
            targets,
            f"QSA 每日汇报未推送：数据校验未通过。\n{readiness}",
        )
        personal_notified = _notify_personal(
            personal_client,
            personal_groups,
            f"QSA 每日汇报未推送：数据校验未通过。\n{readiness}",
        )
        return {
            "target_date": target_date.isoformat(),
            "data_ready": False,
            "readiness": readiness,
            "subscribed_groups": len(targets),
            "personal_groups": len(personal_groups),
            "personal_notified": personal_notified,
            "report_token": None,
            "official_enabled": official_enabled,
            "official_ready": official_ready,
            "personal_enabled": personal_enabled,
            "notified": bool(targets) and official_ready,
            "errors": errors,
        }

    report = None
    if progress_callback is not None:
        progress_callback(
            {"phase": "screening", "message": "按默认条件筛选", "percent": 0}
        )
    try:
        report = create_report_snapshot(
            db,
            logic="and",
            conditions=_default_conditions(settings),
            sorts=[],
            owner_id=None,
            title=f"每日汇报 {target_date.isoformat()}",
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
            f"QSA 每日汇报完成\n交易日：{target_date.isoformat()}\n"
            f"入选：{report.total} 只\n报表：{link}\n{detail}",
        )
        personal_notified = _notify_personal(
            personal_client,
            personal_groups,
            f"QSA 每日汇报完成\n交易日：{target_date.isoformat()}\n"
            f"入选：{report.total} 只\n报表：{link}\n"
            "（个人号通道，PDF 请打开链接查看）",
        )
        # 官方机器人未启用/未配置或没有订阅群时不再构建 PDF，避免无意义的耗时构建。
        if official_ready and targets:
            try:
                pdf_path = build_report_pdf(
                    db, report, public_base_url=base_url
                )
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
        if personal_notified:
            for group in db.scalars(
                select(PersonalQQGroup).where(
                    PersonalQQGroup.active.is_(True)
                )
            ).all():
                group.last_pushed_at = utcnow()
        db.commit()
    except Exception as exc:
        logger.exception("daily report generation failed")
        errors["report"] = f"{type(exc).__name__}: {exc}"
        _notify(client, targets, f"QSA 每日汇报失败：{exc}")
        personal_notified = _notify_personal(
            personal_client,
            personal_groups,
            f"QSA 每日汇报失败：{exc}",
        )

    return {
        "target_date": target_date.isoformat(),
        "data_ready": True,
        "readiness": readiness,
        "report_token": report.token if report is not None else None,
        "subscribed_groups": len(targets),
        "personal_groups": len(personal_groups),
        "personal_notified": personal_notified,
        "official_enabled": official_enabled,
        "official_ready": official_ready,
        "personal_enabled": personal_enabled,
        "notified": bool(targets) and official_ready,
        "errors": errors,
    }
