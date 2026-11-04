from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.personal_qq_group import PersonalQQGroup
from app.models.qq_group_subscription import QQGroupSubscription
from app.models.shareholder_count import ShareholderCount
from app.models.watch_instrument import WatchInstrument
from app.qq_bot.client import ChatTarget
from app.services.personal_qq import build_onebot_client, personal_targets

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 已提醒过的公告用 redis 去重，避免同一条财报/股东户数在多次同步里重复推送。
NOTIFIED_PREFIX = "qsa:watch:notified:"
NOTIFIED_TTL_SECONDS = 30 * 86400
DEFAULT_LOOKBACK_DAYS = 2


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.2f}%"


def _number(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def _period_label(period: date | None) -> str:
    if period is None:
        return "未知报告期"
    quarter = {3: "一季报", 6: "半年报", 9: "三季报", 12: "年报"}.get(period.month)
    return f"{period.year}年{quarter}" if quarter else period.isoformat()


def collect_watch_announcements(
    db: Session, *, since: date, until: date
) -> list[dict[str, Any]]:
    """收集关注标的在 [since, until] 内公告的财报与股东户数，按标的聚合。"""

    grouped: dict[int, dict[str, Any]] = {}
    report_rows = db.execute(
        select(
            WatchInstrument.id,
            Instrument.symbol,
            Instrument.code,
            Instrument.name,
            FinancialReport.report_period,
            FinancialReport.announce_date,
            FinancialReport.revenue_yoy,
            FinancialReport.net_profit_yoy,
            FinancialReport.roe,
        )
        .join(Instrument, Instrument.id == WatchInstrument.instrument_id)
        .join(FinancialReport, FinancialReport.instrument_id == Instrument.id)
        .where(
            WatchInstrument.active.is_(True),
            FinancialReport.announce_date >= since,
            FinancialReport.announce_date <= until,
        )
        .order_by(FinancialReport.announce_date, FinancialReport.report_period)
    ).all()
    for row in report_rows:
        entry = grouped.setdefault(
            row.id,
            {
                "watch_id": row.id,
                "symbol": row.symbol,
                "code": row.code,
                "name": row.name,
                "reports": [],
                "holders": [],
            },
        )
        entry["reports"].append(
            {
                "period": row.report_period,
                "announce_date": row.announce_date,
                "revenue_yoy": row.revenue_yoy,
                "net_profit_yoy": row.net_profit_yoy,
                "roe": row.roe,
            }
        )

    holder_rows = db.execute(
        select(
            WatchInstrument.id,
            Instrument.symbol,
            Instrument.code,
            Instrument.name,
            ShareholderCount.period_end,
            ShareholderCount.announce_date,
            ShareholderCount.holder_num,
            ShareholderCount.holder_change_pct,
        )
        .join(Instrument, Instrument.id == WatchInstrument.instrument_id)
        .join(ShareholderCount, ShareholderCount.instrument_id == Instrument.id)
        .where(
            WatchInstrument.active.is_(True),
            ShareholderCount.announce_date >= since,
            ShareholderCount.announce_date <= until,
        )
        .order_by(ShareholderCount.announce_date, ShareholderCount.period_end)
    ).all()
    for row in holder_rows:
        entry = grouped.setdefault(
            row.id,
            {
                "watch_id": row.id,
                "symbol": row.symbol,
                "code": row.code,
                "name": row.name,
                "reports": [],
                "holders": [],
            },
        )
        entry["holders"].append(
            {
                "period_end": row.period_end,
                "announce_date": row.announce_date,
                "holder_num": row.holder_num,
                "holder_change_pct": row.holder_change_pct,
            }
        )
    return list(grouped.values())


def _dedup_key(
    item: dict[str, Any], kind: str, period: date | None, announce: date | None
) -> str:
    return (
        f"{NOTIFIED_PREFIX}{item['watch_id']}:{kind}:"
        f"{period.isoformat() if period else '-'}:"
        f"{announce.isoformat() if announce else '-'}"
    )


def _filter_new(
    redis_client: Any, entry: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """返回本次需要推送的文案行；同一条公告只推一次（redis 去重，未知标题不过滤）。"""

    lines: list[str] = []
    keys: list[str] = []
    for report in entry["reports"]:
        key = _dedup_key(entry, "report", report["period"], report["announce_date"])
        if redis_client is not None and not _claim(redis_client, key):
            continue
        detail = (
            f"{_period_label(report['period'])}（公告 {report['announce_date']}）："
            f"营收同比 {_pct(report['revenue_yoy'])}，"
            f"净利同比 {_pct(report['net_profit_yoy'])}，"
            f"ROE {_pct(report['roe'])}"
        )
        lines.append(f"· 财报 {detail}")
        keys.append(key)
    for holder in entry["holders"]:
        key = _dedup_key(entry, "holder", holder["period_end"], holder["announce_date"])
        if redis_client is not None and not _claim(redis_client, key):
            continue
        detail = (
            f"截至 {holder['period_end']}（公告 {holder['announce_date']}）："
            f"{_number(holder['holder_num'])} 户，"
            f"环比 {_pct(holder['holder_change_pct'])}"
        )
        lines.append(f"· 股东户数 {detail}")
        keys.append(key)
    return lines, keys


def _claim(redis_client: Any, key: str) -> bool:
    try:
        return bool(redis_client.set(key, "1", nx=True, ex=NOTIFIED_TTL_SECONDS))
    except Exception:
        logger.warning("关注标的公告去重失败，按未提醒处理", exc_info=True)
        return True


def _release(redis_client: Any, keys: list[str]) -> None:
    if redis_client is None or not keys:
        return
    try:
        redis_client.delete(*keys)
    except Exception:
        pass


def _build_official_client(settings: Any, redis_client: Any, config: Any) -> Any:
    """按系统设置构造官方 QQ 客户端；机器人未启用或设置异常时返回 None。"""

    try:
        from app.core.config import get_config
        from app.qq_bot.client import QQBotClient

        cfg = config or get_config()
        snapshot = settings.snapshot
        # 官方机器人关闭时不要构造客户端，也不要做任何推送尝试。
        if not snapshot.get_bool("qq_bot.enabled"):
            return None
        client = QQBotClient(
            app_id=(
                snapshot.get_str("qq_bot.app_id") or cfg.qq_bot_app_id
            ).strip(),
            app_secret=(
                snapshot.get_str("qq_bot.app_secret") or cfg.qq_bot_app_secret
            ).strip(),
            redis_client=redis_client,
            api_base=cfg.qq_bot_api_base,
        )
        return client if client.configured else None
    except Exception:
        logger.warning("构造官方 QQ 客户端失败，跳过官方群提醒", exc_info=True)
        return None


def _build_personal_client(settings: Any, redis_client: Any, config: Any) -> Any:
    try:
        from app.core.config import get_config

        snapshot = settings.snapshot
        # 个人号通道关闭时不要构造客户端，也不要做任何推送尝试。
        if not snapshot.get_bool("personal_qq.enabled"):
            return None
        return build_onebot_client(snapshot, redis_client, config or get_config())
    except Exception:
        logger.warning("构造个人号客户端失败，跳过个人号群提醒", exc_info=True)
        return None


def _official_targets(db: Session) -> list[ChatTarget]:
    groups = db.scalars(
        select(QQGroupSubscription.group_openid).where(
            QQGroupSubscription.active.is_(True)
        )
    ).all()
    return [
        ChatTarget(kind="group", openid=str(group), msg_id="", actor_openid=str(group))
        for group in groups
    ]


def _personal_targets(db: Session) -> list[ChatTarget]:
    return personal_targets(db)


def notify_watch_announcements(
    db: Session,
    *,
    session_factory: sessionmaker | None = None,
    settings: Any,
    redis_client: Any,
    config: Any = None,
    target_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    official_client: Any = None,
    official_targets: list[ChatTarget] | None = None,
    personal_client: Any = None,
    personal_targets: list[ChatTarget] | None = None,
) -> dict[str, Any]:
    """把关注标的最近公告推送到已订阅的 QQ 群（官方与个人号通道）。"""

    moment = target_date or datetime.now(SHANGHAI).date()
    since = moment - timedelta(days=max(0, int(lookback_days) - 1))
    entries = collect_watch_announcements(db, since=since, until=moment)
    result = {
        "instruments": len(entries),
        "items": 0,
        "groups": 0,
        "notified": False,
    }
    if not entries:
        return result

    if official_client is None:
        official_client = _build_official_client(settings, redis_client, config)
    if personal_client is None and personal_targets is None:
        personal_client = _build_personal_client(settings, redis_client, config)
    if official_targets is None:
        official_targets = _official_targets(db)
    if personal_targets is None:
        try:
            if settings is not None and settings.snapshot.get_bool("personal_qq.enabled"):
                personal_targets = _personal_targets(db)
            else:
                personal_targets = []
        except Exception:
            personal_targets = []
    if not official_targets and not personal_targets:
        logger.info("关注标的公告提醒：没有已订阅的群，跳过推送")
        return result

    messages: list[str] = []
    claimed: list[str] = []
    for entry in entries:
        lines, keys = _filter_new(redis_client, entry)
        if not lines:
            continue
        claimed.extend(keys)
        header = (
            f"【关注标的提醒】{entry['name']}({entry['symbol']})"
        )
        messages.append(header + "\n" + "\n".join(lines))
    if not messages:
        return result

    text = (
        f"关注标的公告（{moment.isoformat()}）\n\n"
        + "\n\n".join(messages)
        + "\n\n数据来自 QSA 本地库，可在平台「关注」页或对话中管理关注标的。"
    )

    sent_groups = 0
    if official_client is not None and getattr(official_client, "configured", True):
        for target in official_targets or []:
            try:
                official_client.send_text(target, text)
                sent_groups += 1
            except Exception:
                logger.exception("关注标的公告提醒推送到官方群失败：%s", target.openid)
    if personal_client is not None and getattr(personal_client, "configured", True):
        for target in personal_targets or []:
            try:
                personal_client.send_text(target, text)
                sent_groups += 1
            except Exception:
                logger.exception("关注标的公告提醒推送到个人号群失败：%s", target.openid)

    if sent_groups == 0:
        # 没有成功推送就不占用去重标记，下次同步还能再提醒。
        _release(redis_client, claimed)
        return result

    result["items"] = len(claimed)
    result["groups"] = sent_groups
    result["notified"] = True
    logger.info(
        "关注标的公告提醒：%s 条公告，推送到 %s 个群", len(claimed), sent_groups
    )
    return result

