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
    CATEGORY_LABELS,
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


# 涨停放量判定阈值：「涨停且放量」= 涨停日成交量 >= 一贯均量 * 该倍数。
VOLUME_EXPAND_RATIO = 1.0


def _limit_up_exists_expression(as_of_start: date, as_of: date, mode: str):
    return exists(
        select(1)
        .select_from(LimitEvent)
        .where(
            LimitEvent.instrument_id == Instrument.id,
            LimitEvent.direction == "up",
            LimitEvent.trade_date >= as_of_start,
            LimitEvent.trade_date <= as_of,
            _limit_mode_condition(mode),
        )
        .correlate(Instrument)
    )


def _limit_up_volume_ratio_expression(
    as_of: date,
    start: date,
    mode: str,
    base_days: int,
):
    """最近窗口内最近一次涨停的成交量 / 涨停前一贯均量（前 base_days 个交易日均量）。"""

    event = aliased(LimitEvent)
    event_date = (
        select(event.trade_date)
        .where(
            event.instrument_id == Instrument.id,
            event.direction == "up",
            event.trade_date >= start,
            event.trade_date <= as_of,
            _limit_mode_condition(mode, event),
        )
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
    base_volume = (
        select(func.avg(DailyBar.volume))
        .where(
            DailyBar.instrument_id == Instrument.id,
            DailyBar.trade_date < event_date,
            DailyBar.volume > 0,
        )
        .order_by(DailyBar.trade_date.desc())
        .limit(base_days)
        .correlate(Instrument)
        .scalar_subquery()
    )
    return (
        select(event_volume / func.nullif(base_volume, 0))
        .where(event_date.isnot(None))
        .scalar_subquery()
    )


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
    if field in {"limit_up_volume_ratio_min", "limit_up_is_volume_expand"}:
        base_days = int(params.get("base_days", 5))
        ratio = _limit_up_volume_ratio_expression(
            as_of, start, str(params.get("mode", "close")), base_days
        )
        if field == "limit_up_volume_ratio_min":
            return ratio
        # 布尔口径：最近窗口内既有涨停、且该涨停是放量（比值 >= VOLUME_EXPAND_RATIO）。
        # 用 is_(true) 复用 COUNT 子查询，避免相关子查询被重复展开。
        return and_(
            _limit_up_exists_expression(start, as_of, str(params.get("mode", "close"))).is_(
                True
            ),
            ratio >= VOLUME_EXPAND_RATIO,
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
        # SQLite 没有 DISTINCT ON，latest_* 不去重会把同一标的展开成多行：
        # 窗口统计必须以「本次窗口」为范围，否则会连同窗口外的历史一起统计。
        min_obs_ratio = float(params.get("min_obs_ratio", 0.8))
        min_rows = max(1, math.ceil(days * min_obs_ratio))
        count_all = func.count().filter(
            DailyBar.close_adj.isnot(None),
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= as_of,
        )
        base_filter = [
            DailyBar.instrument_id == Instrument.id,
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= as_of,
        ]
        if field == "close_pct_rank":
            # latest_bar 是外层 JOIN 的别名，不能出现在标量子查询的 FROM 里
            # （SQLite 下会变成笛卡尔积）。这里用相关子查询重新取最新收盘价。
            latest_close_adj = (
                select(DailyBar.close_adj)
                .where(
                    DailyBar.instrument_id == Instrument.id,
                    DailyBar.trade_date <= as_of,
                )
                .order_by(DailyBar.trade_date.desc())
                .limit(1)
                .correlate(Instrument)
                .scalar_subquery()
            )
            count_le = func.count().filter(DailyBar.close_adj <= latest_close_adj)
            rank_expr = case(
                (latest_close_adj.is_(None), None),
                (count_all < min_rows, None),
                else_=count_le * 1.0 / func.nullif(count_all, 0),
            )
            return (
                select(rank_expr)
                .where(*base_filter)
                .correlate(Instrument)
                .scalar_subquery()
            )
        count_min = func.min(DailyBar.close_adj)
        # 窗口内样本不足（新股、历史数据未回补完整）时，只要求「现有数据」够判断
        # 当前价是否贴近低点：取 min(窗口天数, 上市至今交易日数) 作为应有样本。
        listed_rows = (
            select(func.count())
            .select_from(DailyBar)
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date <= as_of,
                DailyBar.close_adj.isnot(None),
            )
            .correlate(Instrument)
            .scalar_subquery()
        )
        available_rows = case(
            (listed_rows < days, listed_rows), else_=days
        )
        required_rows = case(
            (available_rows < min_rows, available_rows),
            else_=min_rows,
        )
        # 同上：用相关子查询取最新收盘价，避免把外层 latest_bar 别名带进 FROM。
        latest_close_adj = (
            select(DailyBar.close_adj)
            .where(
                DailyBar.instrument_id == Instrument.id,
                DailyBar.trade_date <= as_of,
            )
            .order_by(DailyBar.trade_date.desc())
            .limit(1)
            .correlate(Instrument)
            .scalar_subquery()
        )
        ratio_expr = case(
            (latest_close_adj.is_(None), None),
            (count_all < required_rows, None),
            else_=latest_close_adj / func.nullif(count_min, 0) - 1,
        )
        return (
            select(ratio_expr)
            .where(*base_filter)
            .correlate(Instrument)
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

    # 开盘/日内形态：都基于最近一个交易日的开高低收与前收盘。
    open_price = latest_bar.c.open
    close_price = latest_bar.c.close
    preclose = latest_bar.c.preclose
    if key == "open_gap_pct":
        return (
            (open_price / func.nullif(preclose, 0) - 1) * 100
        )
    if key == "open_gap_up":
        return open_price > preclose
    if key == "open_gap_down":
        return open_price < preclose
    if key == "intraday_change_pct":
        return (
            (close_price / func.nullif(open_price, 0) - 1) * 100
        )
    if key == "open_gap_low_close":
        return and_(open_price > preclose, close_price < open_price)
    if key == "open_gap_high_close":
        return and_(open_price < preclose, close_price > open_price)
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


def _connection_as_of(db: Session, requested: date | None) -> date:
    """仅供对比接口使用：拿不到交易日历时退化为请求日期或今天。"""

    try:
        return _resolve_as_of(db, requested)
    except ScreenError:
        return requested or _today()


def _apply_statement_timeout(db: Session, statement_timeout_ms: int) -> None:
    # SET LOCAL 只有 PostgreSQL 支持；SQLite（测试库）跳过。
    if db.get_bind().dialect.name != "postgresql":
        return
    timeout = max(1000, int(statement_timeout_ms))
    db.execute(text(f"SET LOCAL statement_timeout = {timeout}"))


def _latest_ctes(as_of: date):
    latest_bar = (
        select(
            DailyBar.instrument_id.label("instrument_id"),
            DailyBar.trade_date.label("trade_date"),
            DailyBar.close.label("close"),
            DailyBar.close_adj.label("close_adj"),
            DailyBar.pct_chg.label("pct_chg"),
            DailyBar.open.label("open"),
            DailyBar.high.label("high"),
            DailyBar.low.label("low"),
            DailyBar.preclose.label("preclose"),
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
            ShareholderCount.period_end.label("period_end"),
            ShareholderCount.announce_date.label("holder_announce_date"),
            ShareholderCount.announce_date.label("announce_date"),
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
    return latest_bar, latest_holder, latest_fin


def build_screen(
    db: Session,
    request: ScreenRequest,
    *,
    max_window_days: int,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    as_of = _resolve_as_of(db, request.as_of)
    latest_bar, latest_holder, latest_fin = _latest_ctes(as_of)

    predicates = []
    extra_columns: dict[str, Any] = {}
    extra_fields: list[dict[str, Any]] = []

    # SQLite 忽略 DISTINCT ON，latest_* 不去重；价格分位类条件的求值维度
    # 是「每个标的一行」，因此外层以标的为主表、CTE 只作为相关子查询取值。
    low_rank_used = any(
        condition.field in {"close_pct_rank", "price_from_low_pct"}
        for condition in request.conditions
    )

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
    def base_select(model=None, columns=()):
        """按主表构造查询；排名类条件不 JOIN latest_bar，避免 SQLite 多行展开。"""

        statement = select(*columns).select_from(Instrument)
        statement = statement.outerjoin(
            latest_holder, latest_holder.c.instrument_id == Instrument.id
        ).outerjoin(latest_fin, latest_fin.c.instrument_id == Instrument.id)
        if not low_rank_used:
            statement = statement.outerjoin(
                latest_bar, latest_bar.c.instrument_id == Instrument.id
            )
        return statement.where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
            where_clause,
        )

    base_from = base_select(columns=(Instrument.id,))
    count_statement = select(func.count()).select_from(base_from.subquery())

    if low_rank_used:
        # SQLite 忽略 DISTINCT ON：latest_bar 不能出现在 FROM，改为按标的相关取值。
        latest_bar_columns = [
            (
                select(bar_column)
                .where(
                    DailyBar.instrument_id == Instrument.id,
                    DailyBar.trade_date <= as_of,
                )
                .order_by(DailyBar.trade_date.desc())
                .limit(1)
                .correlate(Instrument)
                .scalar_subquery()
                .label(label)
            )
            for bar_column, label in (
                (DailyBar.trade_date, "trade_date"),
                (DailyBar.close, "close"),
                (DailyBar.pct_chg, "pct_chg"),
                (DailyBar.pe_ttm, "pe_ttm"),
                (DailyBar.pb, "pb"),
                (DailyBar.turnover_rate, "turnover_rate"),
                (DailyBar.amount, "amount"),
            )
        ]
    else:
        latest_bar_columns = [
            latest_bar.c[key].label(key)
            for key in (
                "trade_date",
                "close",
                "pct_chg",
                "pe_ttm",
                "pb",
                "turnover_rate",
                "amount",
            )
        ]

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
        *latest_bar_columns,
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
    statement = base_select(
        columns=(*columns, *extra_columns.values())
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

    _apply_statement_timeout(db, statement_timeout_ms)
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

VERDICT_UNKNOWN = "unknown"


def _coerce_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_condition(
    factor: FactorSpec,
    op: str,
    target: Any,
    actual: Any,
) -> tuple[str, float | None, str]:
    """判断单条条件是否满足，并给出与目标的差距。

    返回 (verdict, gap, reason)：verdict 为 pass / fail / unknown；gap 表示
    实际值离满足条件还差多少（已满足或无法判断时为 None）；reason 是给用户看
    的简短说明，例如「低于下限 3.20」或「缺少该项数据，无法判断」。
    """

    if actual is None:
        return VERDICT_UNKNOWN, None, "缺少该项数据，无法判断"

    if factor.value_type == "bool":
        if op == "eq":
            satisfied = bool(actual) is bool(target)
        elif op == "ne":
            satisfied = bool(actual) is not bool(target)
        else:  # pragma: no cover - registry 已限制操作符
            raise FactorError(f"unsupported operator: {op}")
        label = "是" if bool(actual) else "否"
        return ("pass" if satisfied else "fail"), None, f"当前为「{label}」"

    if factor.value_type == "string":
        text = str(actual)
        expected = target if isinstance(target, list) else [target]
        expected_texts = [str(item) for item in expected]
        if op == "eq":
            satisfied = text == expected_texts[0]
        elif op == "ne":
            satisfied = text != expected_texts[0]
        elif op == "in":
            satisfied = text in expected_texts
        elif op == "contains":
            satisfied = expected_texts[0].lower() in text.lower()
        else:  # pragma: no cover - registry 已限制操作符
            raise FactorError(f"unsupported operator: {op}")
        return ("pass" if satisfied else "fail"), None, f"当前为「{text}」"

    number = _coerce_number(actual)
    if number is None:
        numeric_target = (
            float(target)
            if isinstance(target, (int, float)) and not isinstance(target, bool)
            else None
        )
        if numeric_target is not None:
            if op == "gte" and numeric_target <= 0:
                # 计数类条件（例如「涨停次数 ≥ 0」）没有记录本身就是 0 次。
                return "pass", 0.0, "没有记录（按 0 计）"
            if op == "lte" and numeric_target >= 0:
                # 缺少数据时不知道是否真的低于上限，不能判定为满足。
                return VERDICT_UNKNOWN, None, "缺少该项数据，无法判断是否低于上限"
        return VERDICT_UNKNOWN, None, "缺少该项数据，无法判断"

    def gapped(value: float, text: str) -> tuple[str, float, str]:
        # 数值差距去掉浮点噪声（例如 2.5000000000000004 -> 2.5）。
        gap = float(f"{abs(value):.4g}")
        return "fail", gap, f"{text} {gap:g}"

    if op == "between":
        low, high = float(target[0]), float(target[1])
        if low <= number <= high:
            return "pass", 0.0, "在目标区间内"
        if number < low:
            return gapped(low - number, "低于下限")
        return gapped(number - high, "高于上限")
    if op == "in" and factor.value_type == "number":
        allowed = {float(item) for item in target}
        if number in allowed:
            return "pass", 0.0, "在目标取值内"
        return "fail", None, "不在目标取值内"
    limit = float(target)
    if op == "gt":
        satisfied, gap = number > limit, limit - number
    elif op == "gte":
        satisfied, gap = number >= limit, limit - number
    elif op == "lt":
        satisfied, gap = number < limit, number - limit
    elif op == "lte":
        satisfied, gap = number <= limit, number - limit
    elif op == "eq":
        satisfied, gap = number == limit, abs(number - limit)
    elif op == "ne":
        satisfied, gap = number != limit, 1.0
    else:  # pragma: no cover - registry 已限制操作符
        raise FactorError(f"unsupported operator: {op}")
    if satisfied:
        return "pass", 0.0, "已满足"
    return "fail", abs(gap), "已不满足"


def _limit_counts(
    db: Session, instrument_id: int, as_of: date, days: int
) -> dict[str, int]:
    start = _window_start(db, as_of, days)
    up = down = 0
    for direction in db.scalars(
        select(LimitEvent.direction).where(
            LimitEvent.instrument_id == instrument_id,
            LimitEvent.trade_date >= start,
            LimitEvent.trade_date <= as_of,
        )
    ):
        if direction == "up":
            up += 1
        elif direction == "down":
            down += 1
    return {"up": up, "down": down}


def compare_instrument(
    db: Session,
    *,
    symbol: str,
    request: ScreenRequest,
    max_window_days: int,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    """检查单个标的是否满足全部筛选条件，并列出不满足的条件。"""

    instrument = db.scalar(select(Instrument).where(Instrument.symbol == symbol))
    if instrument is None:
        raise ScreenError("证券不存在")
    if instrument.asset_type != "stock" or instrument.status != "active":
        raise ScreenError("该标的不是可筛选的在市标的")

    as_of = _connection_as_of(db, request.as_of)
    latest_bar, latest_holder, latest_fin = _latest_ctes(as_of)

    evaluated: list[tuple[ConditionIn, FactorSpec, dict[str, Any], Any]] = []
    extra_columns: list[Any] = []
    for index, condition in enumerate(request.conditions):
        factor, params, value = _normalize_condition(
            condition, max_window_days=max_window_days
        )
        expression = _factor_expression(
            db, factor, params, as_of, latest_bar, latest_holder, latest_fin
        )
        alias = f"c{index}"
        extra_columns.append(expression.label(alias))
        evaluated.append((condition, factor, params, value))

    statement = (
        select(*extra_columns)
        .select_from(Instrument)
        .outerjoin(latest_bar, latest_bar.c.instrument_id == Instrument.id)
        .outerjoin(latest_holder, latest_holder.c.instrument_id == Instrument.id)
        .outerjoin(latest_fin, latest_fin.c.instrument_id == Instrument.id)
        .where(Instrument.id == instrument.id)
    )
    _apply_statement_timeout(db, statement_timeout_ms)
    raw = db.execute(statement).mappings().first()

    limit_windows = {
        int(params["days"])
        for _, factor, params, _ in evaluated
        if factor.key in {"limit_up_count", "limit_down_count"}
    }
    limit_counts = {
        days: _limit_counts(db, instrument.id, as_of, days)
        for days in sorted(limit_windows)
    }

    low_obs_counts: dict[tuple[str, int], tuple[int, int]] = {}
    for _, factor, params, _ in evaluated:
        if factor.key not in {"close_pct_rank", "price_from_low_pct"}:
            continue
        days = int(params["days"])
        key = (factor.key, days)
        if key in low_obs_counts:
            continue
        start = _window_start(db, as_of, days)
        rows_in_window = int(
            db.scalar(
                select(func.count())
                .select_from(DailyBar)
                .where(
                    DailyBar.instrument_id == instrument.id,
                    DailyBar.trade_date >= start,
                    DailyBar.trade_date <= as_of,
                    DailyBar.close_adj.isnot(None),
                )
            )
            or 0
        )
        total_rows = int(
            db.scalar(
                select(func.count())
                .select_from(DailyBar)
                .where(
                    DailyBar.instrument_id == instrument.id,
                    DailyBar.trade_date <= as_of,
                    DailyBar.close_adj.isnot(None),
                )
            )
            or 0
        )
        min_rows = max(1, math.ceil(days * float(params.get("min_obs_ratio", 0.8))))
        low_obs_counts[key] = (rows_in_window, min(total_rows, min_rows))

    results: list[dict[str, Any]] = []
    passed = failed = unknown = 0
    for index, (condition, factor, params, value) in enumerate(evaluated):
        actual = raw.get(f"c{index}") if raw is not None else None
        verdict, gap, reason = evaluate_condition(
            factor, condition.op, value, actual
        )
        if verdict == "pass":
            passed += 1
        elif verdict == "fail":
            failed += 1
        else:
            unknown += 1
        item: dict[str, Any] = {
            "field": factor.key,
            "label": factor.label,
            "category": factor.category,
            "category_label": CATEGORY_LABELS.get(
                factor.category, factor.category
            ),
            "op": condition.op,
            "target": value,
            "params": params,
            "actual": actual,
            "verdict": verdict,
            "gap": gap,
            "reason": reason,
        }
        if factor.key in {"close_pct_rank", "price_from_low_pct"}:
            rows_in_window, required = low_obs_counts.get(
                (factor.key, int(params["days"])), (0, 0)
            )
            if actual is None:
                item["reason"] = (
                    f"窗口内只有 {rows_in_window} 个交易日数据，"
                    f"至少需要 {required} 个交易日才能判断"
                )
            item["detail"] = {
                "windows": rows_in_window,
                "required": required,
                "days": int(params["days"]),
            }
        if factor.key in {"limit_up_count", "limit_down_count"}:
            counts = limit_counts.get(int(params["days"]))
            if counts:
                item["detail"] = {
                    "days": int(params["days"]),
                    "up": counts["up"],
                    "down": counts["down"],
                    "mode": str(params.get("mode", "close")),
                }
        results.append(item)

    # 与筛选口径一致：全部满足（and）或任一满足（or）；缺数据的条件不算满足。
    matches = (
        passed == len(results)
        if request.logic == "and"
        else passed > 0
    )
    return {
        "symbol": instrument.symbol,
        "code": instrument.code,
        "name": instrument.name,
        "board": instrument.board,
        "industry": instrument.industry,
        "as_of": as_of.isoformat(),
        "logic": request.logic,
        "matches": matches,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "conditions": results,
    }

