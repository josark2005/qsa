from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    and_,
    case,
    exists,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Session, aliased

from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.limit_event import LimitEvent
from app.models.shareholder_count import ShareholderCount
from app.models.trading_calendar import TradingCalendar
from app.screening.registry import (
    FactorError,
    FactorSpec,
    get_factor,
    normalize_params,
    normalize_value,
)
from app.schemas.screening import ConditionIn, ScreenRequest, SortIn

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ScreenError(ValueError):
    pass


def _today() -> date:
    return datetime.now(SHANGHAI).date()


def _resolve_as_of(db: Session, requested: date | None) -> date:
    target = requested or _today()
    value = db.scalar(
        select(func.max(TradingCalendar.cal_date)).where(
            TradingCalendar.exchange == "CN",
            TradingCalendar.is_open.is_(True),
            TradingCalendar.cal_date <= target,
        )
    )
    if value is None:
        raise ScreenError("没有可用交易日历，请先同步交易日历")
    return value


def _window_start(db: Session, as_of: date, days: int) -> date:
    value = db.scalar(
        select(TradingCalendar.cal_date)
        .where(
            TradingCalendar.exchange == "CN",
            TradingCalendar.is_open.is_(True),
            TradingCalendar.cal_date <= as_of,
        )
        .order_by(TradingCalendar.cal_date.desc())
        .offset(max(days - 1, 0))
        .limit(1)
    )
    if value is None:
        value = db.scalar(
            select(func.min(TradingCalendar.cal_date)).where(
                TradingCalendar.exchange == "CN",
                TradingCalendar.is_open.is_(True),
                TradingCalendar.cal_date <= as_of,
            )
        )
    return value or as_of


def _limit_mode_condition(mode: str, model=LimitEvent):
    if mode == "touch":
        return model.is_touched.is_(True)
    if mode == "one_word":
        return model.is_one_word.is_(True)
    return model.is_close_limit.is_(True)


def _years_start(as_of: date, years: int) -> date:
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:
        # Feb 29 -> Feb 28
        return as_of.replace(year=as_of.year - years, day=28)


def _window_expression(
    db: Session,
    field: str,
    params: dict[str, Any],
    as_of: date,
    latest_bar,
):
    if field == "recent_price_amplitude_ratio":
        years = int(params["years"])
        start = _years_start(as_of, years)
        min_low = func.min(DailyBar.low)
        max_high = func.max(DailyBar.high)
        return (
            select(max_high / func.nullif(min_low, 0))
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date >= start,
                DailyBar.trade_date <= as_of,
            )
            .scalar_subquery()
        )

    days = int(params["days"])
    start = _window_start(db, as_of, days)
    if field in {"first_limit_up_volume_ratio", "limit_up_volume_ratio"}:
        event = aliased(LimitEvent)
        previous_event = aliased(LimitEvent)
        previous_open = (
            select(func.max(TradingCalendar.cal_date))
            .where(
                TradingCalendar.exchange == "CN",
                TradingCalendar.is_open.is_(True),
                TradingCalendar.cal_date < event.trade_date,
            )
            .correlate(event)
            .scalar_subquery()
        )
        filters = [
            event.instrument_id == Instrument.id,
            event.direction == "up",
            event.trade_date >= start,
            event.trade_date <= as_of,
            _limit_mode_condition(str(params.get("mode", "close")), event),
        ]
        if field == "first_limit_up_volume_ratio":
            filters.append(
                ~exists(
                    select(1)
                    .select_from(previous_event)
                    .where(
                        previous_event.instrument_id == Instrument.id,
                        previous_event.direction == "up",
                        previous_event.trade_date == previous_open,
                        _limit_mode_condition(
                            str(params.get("mode", "close")), previous_event
                        ),
                    )
                    .correlate(Instrument)
                )
            )
        event_date = (
            select(event.trade_date)
            .where(*filters)
            .order_by(event.trade_date.desc())
            .limit(1)
            .correlate(Instrument)
            .scalar_subquery()
        )
        event_volume = (
            select(DailyBar.volume)
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date == event_date,
            )
            .correlate(Instrument)
            .scalar_subquery()
        )
        previous_volume = (
            select(DailyBar.volume)
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date < event_date,
            )
            .order_by(DailyBar.trade_date.desc())
            .limit(1)
            .correlate(Instrument)
            .scalar_subquery()
        )
        return (
            select(event_volume / func.nullif(previous_volume, 0))
            .where(event_date.isnot(None))
            .scalar_subquery()
        )
    if field == "window_price_range_pct":
        min_low = func.min(DailyBar.low)
        max_high = func.max(DailyBar.high)
        return (
            select(
                (max_high - min_low) / func.nullif(min_low, 0) * 100
            )
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date >= start,
                DailyBar.trade_date <= as_of,
            )
            .scalar_subquery()
        )
    if field in {"limit_up_count", "limit_down_count"}:
        direction = "up" if field == "limit_up_count" else "down"
        mode = str(params.get("mode", "close"))
        return (
            select(func.count())
            .select_from(LimitEvent)
            .where(
                LimitEvent.instrument_id == Instrument.id,
                LimitEvent.direction == direction,
                LimitEvent.trade_date >= start,
                LimitEvent.trade_date <= as_of,
                _limit_mode_condition(mode),
            )
            .scalar_subquery()
        )
    if field == "avg_amount":
        return (
            select(func.avg(DailyBar.amount))
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date >= start,
                DailyBar.trade_date <= as_of,
                DailyBar.amount.isnot(None),
            )
            .scalar_subquery()
        )
    if field in {"close_pct_rank", "price_from_low_pct"}:
        min_obs_ratio = float(params.get("min_obs_ratio", 0.8))
        min_rows = max(1, math.ceil(days * min_obs_ratio))
        count_all = func.count().filter(DailyBar.close_adj.isnot(None))
        base_filter = [
            DailyBar.instrument_id == Instrument.id,
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= as_of,
        ]
        if field == "close_pct_rank":
            count_le = func.count().filter(
                DailyBar.close_adj <= latest_bar.c.close_adj
            )
            rank_expr = case(
                (latest_bar.c.close_adj.is_(None), None),
                (
                    count_all < min_rows,
                    None,
                ),
                else_=count_le * 1.0 / func.nullif(count_all, 0),
            )
            return (
                select(rank_expr)
                .where(*base_filter)
                .scalar_subquery()
            )
        count_min = func.min(DailyBar.close_adj)
        ratio_expr = case(
            (latest_bar.c.close_adj.is_(None), None),
            (count_all < min_rows, None),
            else_=latest_bar.c.close_adj / func.nullif(count_min, 0) - 1,
        )
        return (
            select(ratio_expr)
            .where(*base_filter)
            .scalar_subquery()
        )
    raise FactorError(f"unsupported window field: {field}")


def _factor_expression(
    db: Session,
    factor: FactorSpec,
    params: dict[str, Any],
    as_of: date,
    latest_bar,
    latest_holder,
    latest_fin,
):
    key = factor.key
    basic = {
        "is_st": Instrument.is_st,
        "board": Instrument.board,
        "industry": Instrument.industry,
    }
    if key in basic:
        return basic[key]
    if key == "list_days":
        return func.coalesce(literal(as_of) - Instrument.list_date, 999999)
    bar_fields = {
        "close": latest_bar.c.close,
        "pct_chg": latest_bar.c.pct_chg,
        "pe_ttm": latest_bar.c.pe_ttm,
        "pb": latest_bar.c.pb,
        "turnover_rate": latest_bar.c.turnover_rate,
    }
    if key in bar_fields:
        return bar_fields[key]
    holder_fields = {
        "holder_num": latest_holder.c.holder_num,
        "holder_change_pct": latest_holder.c.holder_change_pct,
        "holder_change": latest_holder.c.holder_change,
    }
    if key in holder_fields:
        return holder_fields[key]
    financial_fields = {
        "revenue_yoy": latest_fin.c.revenue_yoy,
        "net_profit_yoy": latest_fin.c.net_profit_yoy,
        "roe": latest_fin.c.roe,
        "gross_margin": latest_fin.c.gross_margin,
        "debt_ratio": latest_fin.c.debt_ratio,
        "operating_cash_flow": latest_fin.c.operating_cash_flow,
        "eps": latest_fin.c.eps,
        "bps": latest_fin.c.bps,
    }
    if key in financial_fields:
        return financial_fields[key]
    return _window_expression(db, key, params, as_of, latest_bar)


def _extra_alias(factor: FactorSpec, params: dict[str, Any]) -> str:
    if factor.key in {"limit_up_count", "limit_down_count"}:
        return f"{factor.key}_{params['days']}_{params.get('mode', 'close')}"
    if factor.key in {"first_limit_up_volume_ratio", "limit_up_volume_ratio"}:
        return f"{factor.key}_{params['days']}_{params.get('mode', 'close')}"
    if factor.key in {
        "avg_amount",
        "window_price_range_pct",
        "close_pct_rank",
        "price_from_low_pct",
    }:
        return f"{factor.key}_{params['days']}"
    if factor.key == "recent_price_amplitude_ratio":
        return f"{factor.key}_{params['years']}"
    return factor.key


def _apply_operator(expression, op: str, value: Any):
    if op == "between":
        return expression.between(value[0], value[1])
    if op == "in":
        return expression.in_(value)
    if op == "contains":
        return expression.ilike(f"%{value}%")
    if op == "eq":
        if isinstance(value, bool):
            return expression.is_(value)
        return expression == value
    if op == "ne":
        if isinstance(value, bool):
            return expression.is_not(value)
        return expression != value
    if op == "gt":
        return expression > value
    if op == "gte":
        return expression >= value
    if op == "lt":
        return expression < value
    if op == "lte":
        return expression <= value
    raise FactorError(f"unsupported operator: {op}")


def _normalize_condition(
    condition: ConditionIn,
    *,
    max_window_days: int,
) -> tuple[FactorSpec, dict[str, Any], Any]:
    factor = get_factor(condition.field)
    params = normalize_params(factor, condition.params)
    if "days" in params and factor.category == "low":
        if int(params["days"]) > max_window_days:
            raise FactorError(
                f"{factor.key}.days 不能大于系统上限 {max_window_days}"
            )
    value = normalize_value(factor, condition.op, condition.value)
    return factor, params, value


def build_screen(
    db: Session,
    request: ScreenRequest,
    *,
    max_window_days: int,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    as_of = _resolve_as_of(db, request.as_of)
    latest_bar = (
        select(
            DailyBar.instrument_id.label("instrument_id"),
            DailyBar.trade_date.label("trade_date"),
            DailyBar.close.label("close"),
            DailyBar.close_adj.label("close_adj"),
            DailyBar.pct_chg.label("pct_chg"),
            DailyBar.pe_ttm.label("pe_ttm"),
            DailyBar.pb.label("pb"),
            DailyBar.turnover_rate.label("turnover_rate"),
            DailyBar.amount.label("amount"),
        )
        .where(DailyBar.trade_date <= as_of)
        .distinct(DailyBar.instrument_id)
        .order_by(DailyBar.instrument_id, DailyBar.trade_date.desc())
        .cte("latest_bar")
    )
    latest_holder = (
        select(
            ShareholderCount.instrument_id.label("instrument_id"),
            ShareholderCount.holder_num.label("holder_num"),
            ShareholderCount.holder_change_pct.label("holder_change_pct"),
            ShareholderCount.holder_change.label("holder_change"),
            ShareholderCount.period_end.label("holder_period_end"),
            ShareholderCount.announce_date.label("holder_announce_date"),
        )
        .where(ShareholderCount.announce_date <= as_of)
        .distinct(ShareholderCount.instrument_id)
        .order_by(
            ShareholderCount.instrument_id,
            ShareholderCount.period_end.desc(),
            ShareholderCount.announce_date.desc(),
        )
        .cte("latest_holder")
    )
    latest_fin = (
        select(
            FinancialReport.instrument_id.label("instrument_id"),
            FinancialReport.report_period.label("report_period"),
            FinancialReport.announce_date.label("financial_announce_date"),
            FinancialReport.revenue_yoy.label("revenue_yoy"),
            FinancialReport.net_profit_yoy.label("net_profit_yoy"),
            FinancialReport.roe.label("roe"),
            FinancialReport.gross_margin.label("gross_margin"),
            FinancialReport.debt_ratio.label("debt_ratio"),
            FinancialReport.operating_cash_flow.label("operating_cash_flow"),
            FinancialReport.eps.label("eps"),
            FinancialReport.bps.label("bps"),
        )
        .where(FinancialReport.announce_date <= as_of)
        .distinct(FinancialReport.instrument_id)
        .order_by(
            FinancialReport.instrument_id,
            FinancialReport.report_period.desc(),
            FinancialReport.announce_date.desc(),
        )
        .cte("latest_fin")
    )

    predicates = []
    extra_columns: dict[str, Any] = {}
    extra_fields: list[dict[str, Any]] = []

    def expression_for(field: str, params: dict[str, Any]):
        factor = get_factor(field)
        return _factor_expression(
            db, factor, params, as_of, latest_bar, latest_holder, latest_fin
        )

    for condition in request.conditions:
        factor, params, value = _normalize_condition(
            condition, max_window_days=max_window_days
        )
        expression = _factor_expression(
            db, factor, params, as_of, latest_bar, latest_holder, latest_fin
        )
        predicates.append(_apply_operator(expression, condition.op, value))
        if factor.category in {"limit", "low"} or factor.key == "avg_amount":
            alias = _extra_alias(factor, params)
            if alias not in extra_columns:
                extra_columns[alias] = expression.label(alias)
                extra_fields.append(
                    {
                        "alias": alias,
                        "label": factor.label,
                        "field": factor.key,
                        "params": params,
                    }
                )

    sort_expressions = []
    for sort in request.sort:
        if sort.field == "symbol":
            sort_expressions.append(
                (Instrument.symbol, sort.order)
            )
            continue
        factor = get_factor(sort.field)
        params = normalize_params(factor, sort.params)
        if "days" in params and factor.category == "low":
            if int(params["days"]) > max_window_days:
                raise FactorError(
                    f"{factor.key}.days 不能大于系统上限 {max_window_days}"
                )
        expression = _factor_expression(
            db, factor, params, as_of, latest_bar, latest_holder, latest_fin
        )
        if factor.category in {"limit", "low"} or factor.key == "avg_amount":
            alias = _extra_alias(factor, params)
            if alias not in extra_columns:
                extra_columns[alias] = expression.label(alias)
                extra_fields.append(
                    {
                        "alias": alias,
                        "label": factor.label,
                        "field": factor.key,
                        "params": params,
                    }
                )
        sort_expressions.append((expression, sort.order))

    where_clause = (
        and_(*predicates) if request.logic == "and" else or_(*predicates)
    )
    base_from = (
        select(Instrument.id)
        .select_from(Instrument)
        .outerjoin(latest_bar, latest_bar.c.instrument_id == Instrument.id)
        .outerjoin(latest_holder, latest_holder.c.instrument_id == Instrument.id)
        .outerjoin(latest_fin, latest_fin.c.instrument_id == Instrument.id)
        .where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
            where_clause,
        )
    )
    count_statement = select(func.count()).select_from(base_from.subquery())

    columns = [
        Instrument.symbol.label("symbol"),
        Instrument.name.label("name"),
        Instrument.board.label("board"),
        Instrument.industry.label("industry"),
        Instrument.is_st.label("is_st"),
        Instrument.list_date.label("list_date"),
        func.coalesce(literal(as_of) - Instrument.list_date, 999999).label(
            "list_days"
        ),
        latest_bar.c.trade_date.label("trade_date"),
        latest_bar.c.close.label("close"),
        latest_bar.c.pct_chg.label("pct_chg"),
        latest_bar.c.pe_ttm.label("pe_ttm"),
        latest_bar.c.pb.label("pb"),
        latest_bar.c.turnover_rate.label("turnover_rate"),
        latest_bar.c.amount.label("amount"),
        latest_holder.c.holder_num.label("holder_num"),
        latest_holder.c.holder_change_pct.label("holder_change_pct"),
        latest_holder.c.holder_period_end.label("holder_period_end"),
        latest_fin.c.report_period.label("report_period"),
        latest_fin.c.revenue_yoy.label("revenue_yoy"),
        latest_fin.c.net_profit_yoy.label("net_profit_yoy"),
        latest_fin.c.roe.label("roe"),
        latest_fin.c.gross_margin.label("gross_margin"),
        latest_fin.c.debt_ratio.label("debt_ratio"),
        latest_fin.c.operating_cash_flow.label("operating_cash_flow"),
        latest_fin.c.eps.label("eps"),
    ]
    statement = (
        select(*columns, *extra_columns.values())
        .select_from(Instrument)
        .outerjoin(latest_bar, latest_bar.c.instrument_id == Instrument.id)
        .outerjoin(latest_holder, latest_holder.c.instrument_id == Instrument.id)
        .outerjoin(latest_fin, latest_fin.c.instrument_id == Instrument.id)
        .where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
            where_clause,
        )
    )
    if sort_expressions:
        for expression, order in sort_expressions:
            statement = statement.order_by(
                expression.desc().nulls_last()
                if order == "desc"
                else expression.asc().nulls_last()
            )
    else:
        statement = statement.order_by(Instrument.symbol)
    statement = statement.order_by(Instrument.symbol)
    offset = (request.page - 1) * request.page_size
    statement = statement.offset(offset).limit(request.page_size)

    timeout = max(1000, int(statement_timeout_ms))
    db.execute(text(f"SET LOCAL statement_timeout = {timeout}"))
    total = int(db.scalar(count_statement) or 0)
    rows = [
        {key: value for key, value in row.items()}
        for row in db.execute(statement).mappings().all()
    ]
    return {
        "as_of": as_of,
        "total": total,
        "page": request.page,
        "page_size": request.page_size,
        "rows": rows,
        "extra_fields": extra_fields,
    }
