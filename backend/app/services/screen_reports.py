from __future__ import annotations

import json
import secrets
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.screen_report import ScreenReport
from app.schemas.reports import ReportCreateResponse
from app.schemas.screening import ConditionIn, ScreenRequest, SortIn
from app.screening.query_builder import build_screen
from app.screening.registry import FactorSpec, get_factor

SHANGHAI = ZoneInfo("Asia/Shanghai")
REPORT_MAX_ROWS = 1000

OP_LABELS = {
    "eq": "等于",
    "ne": "不等于",
    "gt": "大于",
    "gte": "≥",
    "lt": "小于",
    "lte": "≤",
    "between": "介于",
    "contains": "包含",
    "in": "属于",
}
MODE_LABELS = {"close": "收盘", "touch": "触板", "one_word": "一字"}


def format_factor_value(factor: FactorSpec, value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return " ~ ".join(format_factor_value(factor, item) for item in value)
    if factor.choices and isinstance(value, str):
        return factor.choice_labels.get(value, value)
    if (
        factor.key in {"price_from_low_pct", "close_pct_rank"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return f"{text}{factor.unit}"
    return str(value) if value is not None else "—"


def format_factor_params(factor: FactorSpec, params: dict[str, Any]) -> str:
    parts: list[str] = []
    for param in factor.params:
        value = params.get(param.name, param.default)
        if param.name == "mode":
            parts.append(MODE_LABELS.get(str(value), str(value)))
        elif param.name == "days":
            parts.append(f"{value}日")
        elif param.name == "min_obs_ratio":
            parts.append(f"数据占比 {value}")
        else:
            parts.append(f"{param.label} {value}")
    return " · ".join(parts)


def condition_views(conditions: list[ConditionIn]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for condition in conditions:
        factor = get_factor(condition.field)
        views.append(
            {
                "field": factor.key,
                "label": factor.label,
                "op": condition.op,
                "op_label": OP_LABELS.get(condition.op, condition.op),
                "value": jsonable_encoder(condition.value),
                "value_text": format_factor_value(factor, condition.value),
                "params": jsonable_encoder(condition.params),
                "params_text": format_factor_params(factor, condition.params),
            }
        )
    return views


def sort_views(sorts: list[SortIn]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for item in sorts:
        factor = get_factor(item.field)
        views.append(
            {
                "field": factor.key,
                "label": factor.label,
                "order": item.order,
                "order_label": "降序" if item.order == "desc" else "升序",
                "params": jsonable_encoder(item.params),
                "params_text": format_factor_params(factor, item.params),
            }
        )
    return views


def latest_stock_bar_date(db: Session, cutoff: date | None = None) -> date | None:
    statement = (
        select(func.max(DailyBar.trade_date))
        .join(Instrument, Instrument.id == DailyBar.instrument_id)
        .where(Instrument.asset_type == "stock")
    )
    if cutoff is not None:
        statement = statement.where(DailyBar.trade_date <= cutoff)
    return db.scalar(statement)


def stale_reasons(report: ScreenReport, latest_trade_date: date | None) -> list[str]:
    reasons: list[str] = []
    today = datetime.now(SHANGHAI).date()
    if report.report_date < today:
        reasons.append(f"报表生成于 {report.report_date.isoformat()}，已非当日报表")
    if report.data_as_of is None:
        reasons.append("报表没有可用的日线数据")
    elif latest_trade_date is not None and report.data_as_of < latest_trade_date:
        reasons.append(
            "报表行情数据截至 "
            f"{report.data_as_of.isoformat()}，当前最新交易日为 "
            f"{latest_trade_date.isoformat()}"
        )
    return reasons


def report_response(
    db: Session,
    report: ScreenReport,
    *,
    latest_trade_date: date | None = None,
) -> ReportCreateResponse:
    if latest_trade_date is None:
        latest_trade_date = latest_stock_bar_date(db)
    reasons = stale_reasons(report, latest_trade_date)
    return ReportCreateResponse(
        token=report.token,
        title=report.title,
        generated_at=report.generated_at.isoformat(),
        report_date=report.report_date,
        screen_as_of=report.screen_as_of,
        data_as_of=report.data_as_of,
        total=report.total,
        included=report.included,
        truncated=report.truncated,
        rows=report.rows,
        condition_views=report.condition_views,
        sort_views=report.sort_views,
        extra_fields=report.extra_fields,
        criteria=report.criteria,
        stale=bool(reasons),
        stale_reason="；".join(reasons) if reasons else None,
        latest_trade_date=latest_trade_date,
    )


def unique_report_token(db: Session) -> str:
    for _ in range(5):
        token = secrets.token_urlsafe(32)
        if db.scalar(select(ScreenReport.id).where(ScreenReport.token == token)) is None:
            return token
    raise RuntimeError("无法生成唯一报表链接")


def create_report_snapshot(
    db: Session,
    *,
    logic: str,
    conditions: list[ConditionIn],
    sorts: list[SortIn],
    owner_id: uuid.UUID | None,
    title: str | None,
    as_of: date | None,
    max_window_days: int,
    statement_timeout_ms: int,
) -> ScreenReport:
    screen_request = ScreenRequest(
        as_of=as_of,
        logic=logic,  # type: ignore[arg-type]
        conditions=conditions,
        sort=sorts,
        page=1,
        page_size=REPORT_MAX_ROWS,
    )
    result = build_screen(
        db,
        screen_request,
        max_window_days=max_window_days,
        statement_timeout_ms=statement_timeout_ms,
    )
    rows = jsonable_encoder(result["rows"])
    total = int(result["total"])
    included = len(rows)
    screen_as_of = result["as_of"]
    data_as_of = latest_stock_bar_date(db, cutoff=screen_as_of)
    now = datetime.now(SHANGHAI)
    criteria = {
        "logic": logic,
        "conditions": [condition.model_dump(mode="json") for condition in conditions],
        "sort": [item.model_dump(mode="json") for item in sorts],
    }
    report = ScreenReport(
        token=unique_report_token(db),
        owner_id=owner_id,
        title=title or f"选股报表 {now:%Y-%m-%d %H:%M}",
        criteria=criteria,
        condition_views=condition_views(conditions),
        sort_views=sort_views(sorts),
        extra_fields=jsonable_encoder(result["extra_fields"]),
        rows=rows,
        total=total,
        included=included,
        truncated=total > included,
        screen_as_of=screen_as_of,
        data_as_of=data_as_of,
        report_date=now.date(),
        generated_at=datetime.now(SHANGHAI).astimezone(ZoneInfo("UTC")),
    )
    db.add(report)
    db.flush()
    return report


def estimate_condition_total(
    db: Session,
    *,
    logic: str,
    conditions: list[ConditionIn],
    sorts: list[SortIn],
    as_of: date | None,
    max_window_days: int,
    statement_timeout_ms: int,
) -> tuple[int, date]:
    screen_request = ScreenRequest(
        as_of=as_of,
        logic=logic,  # type: ignore[arg-type]
        conditions=conditions,
        sort=sorts,
        page=1,
        page_size=1,
    )
    result = build_screen(
        db,
        screen_request,
        max_window_days=max_window_days,
        statement_timeout_ms=statement_timeout_ms,
    )
    screen_as_of = result["as_of"]
    data_as_of = latest_stock_bar_date(db, cutoff=screen_as_of) or screen_as_of
    return int(result["total"]), data_as_of
