from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.shareholder_count import ShareholderCount
from app.schemas.screening import ConditionIn, ScreenRequest
from app.screening.query_builder import ScreenError, build_screen
from app.screening.registry import FactorError, FACTORS, factor_metadata
from app.services.screen_reports import latest_stock_bar_date
from app.services.web_search import WebSearchError
from app.services.web_search import web_search as _web_search

logger = logging.getLogger(__name__)

# 单次工具结果的最大字符数，超出时裁剪条目并标记 truncated。
MAX_TOOL_CHARS = 4000
# 工具参数上限。
MAX_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_HISTORY_DAYS = 60
MIN_HISTORY_DAYS = 5
MAX_HISTORY_DAYS = 250
# 多候选时最多列出的标的数量。
MAX_CANDIDATES = 5
# 筛选工具不做逐条明细时的分页规模（取足够多再本地截断）。
SCREEN_FETCH_PAGESIZE = 100

DATA_NOTE = "数据来自 QSA 本地库，可能不是最新交易日。"

WEB_SEARCH_DISABLED = "网页搜索未启用"


class ToolArgumentError(ValueError):
    """工具参数不合法，消息可直接展示给模型。"""


# ---------------------------------------------------------------------------
# 数值与 JSON 辅助
# ---------------------------------------------------------------------------


def _num(value: Any, digits: int = 4) -> float | None:
    """把 Decimal / int / float 统一转成 float 并控制小数位；None 保持 None。"""

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return round(result, digits)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _dumps(payload: dict[str, Any]) -> str:
    """序列化为 JSON 字符串；超长时裁剪列表条目并标记 truncated。"""

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= MAX_TOOL_CHARS:
        return text

    # 依次尝试裁剪最大的列表字段
    for key in ("items", "results", "candidates", "factors"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            trimmed = list(value)
            while trimmed and len(text) > MAX_TOOL_CHARS:
                trimmed.pop()
                marker = {"truncated": True}
                if "total" in payload:
                    marker["total"] = payload["total"]
                payload = {**payload, key: trimmed, **marker}
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= MAX_TOOL_CHARS:
                return text

    # 仍然超长：退化为截短文本，但仍保证是合法 JSON 且不超过上限
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    limit = MAX_TOOL_CHARS - 60
    while True:
        fallback = {"truncated": True, "message": raw[: max(limit, 0)]}
        text = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= MAX_TOOL_CHARS or limit <= 0:
            return text
        limit -= 100


def _error(message: str) -> str:
    """工具错误统一成中文 JSON 字符串。"""

    return json.dumps({"error": message}, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def _as_int_arg(
    arguments: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = arguments.get(key, default)
    if raw is None or raw == "":
        raw = default
    if isinstance(raw, bool):
        raise ToolArgumentError(f"参数 {key} 必须是整数")
    if isinstance(raw, str):
        text = raw.strip()
        try:
            raw = int(text)
        except ValueError:
            raise ToolArgumentError(f"参数 {key} 必须是整数") from None
    elif isinstance(raw, float):
        if raw != int(raw):
            raise ToolArgumentError(f"参数 {key} 必须是整数")
        raw = int(raw)
    elif not isinstance(raw, int):
        raise ToolArgumentError(f"参数 {key} 必须是整数")
    return max(minimum, min(maximum, int(raw)))


def _as_str_arg(arguments: dict[str, Any], key: str, *, required: bool) -> str:
    raw = arguments.get(key)
    if raw is None:
        if required:
            raise ToolArgumentError(f"缺少参数 {key}")
        return ""
    if not isinstance(raw, str):
        raise ToolArgumentError(f"参数 {key} 必须是字符串")
    text = raw.strip()
    if required and not text:
        raise ToolArgumentError(f"参数 {key} 不能为空")
    return text


def _as_dict_arg(arguments: dict[str, Any], key: str) -> dict[str, Any]:
    raw = arguments.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolArgumentError(f"参数 {key} 必须是对象")
    return raw


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


def _function(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def tool_definitions(*, include_web_search: bool = True) -> list[dict[str, Any]]:
    """OpenAI tools 数组，元素形如 {"type": "function", "function": {"name":..., "description":..., "parameters": {...JSON Schema...}}}"""

    tools: list[dict[str, Any]] = [
        _function(
            "search_instruments",
            "按名称、代码或证券代码模糊搜索标的，返回代码、名称、板块、行业与最新收盘。"
            f"{DATA_NOTE}",
            {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，可以是公司名称、证券代码或带后缀的代码",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限，1-20，默认 10",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
            },
            ["query"],
        ),
        _function(
            "get_instrument_profile",
            "查询单个标的的概况：板块行业、最新日线行情、最近一期财报与股东户数。"
            f"{DATA_NOTE}",
            {
                "symbol": {
                    "type": "string",
                    "description": "标的代码或名称，例如 600519、600519.SH、贵州茅台",
                },
            },
            ["symbol"],
        ),
        _function(
            "get_price_history",
            "统计单个标的最近 N 个交易日的区间表现：起止日期、涨跌幅、最高最低、平均成交额与换手率。"
            f"{DATA_NOTE}",
            {
                "symbol": {
                    "type": "string",
                    "description": "标的代码或名称，例如 600519、600519.SH、贵州茅台",
                },
                "days": {
                    "type": "integer",
                    "description": "回溯的交易日数量，5-250，默认 60",
                    "minimum": MIN_HISTORY_DAYS,
                    "maximum": MAX_HISTORY_DAYS,
                },
            },
            ["symbol"],
        ),
        _function(
            "screen_instruments",
            "按条件批量筛选标的，条件之间为“且”关系。字段必须来自 list_screening_factors。"
            f"{DATA_NOTE}",
            {
                "conditions": {
                    "type": "array",
                    "description": (
                        '筛选条件数组，例如 [{"field": "pe_ttm", "op": "lte", '
                        '"value": 30, "params": {}}]'
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "description": "筛选字段 key"},
                            "op": {
                                "type": "string",
                                "description": "操作符：eq/ne/gt/gte/lt/lte/between/in/contains",
                            },
                            "value": {
                                "description": "比较值，between 需要两个数字的数组，in 需要字符串数组",
                            },
                            "params": {
                                "type": "object",
                                "description": "字段参数，例如窗口天数 days",
                            },
                        },
                        "required": ["field", "op", "value"],
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "返回明细条数上限，1-20，默认 10",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
            },
            ["conditions"],
        ),
        _function(
            "list_screening_factors",
            "列出全部可用筛选字段及其操作符、参数默认值，用于构造 screen_instruments 的条件。"
            f"{DATA_NOTE}",
            {},
            [],
        ),
    ]
    if include_web_search:
        tools.append(
            _function(
                "web_search",
                "联网搜索最新资讯、公告或新闻，返回标题、链接与摘要。",
                {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限，1-20，默认 5",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                    },
                },
                ["query"],
            )
        )
    return tools


# ---------------------------------------------------------------------------
# 标的解析
# ---------------------------------------------------------------------------


def _latest_bars(db: Session, instrument_ids: list[int]) -> dict[int, Any]:
    """批量取每个标的最近一根日线（一条 SQL，不做 N+1）。

    先按 instrument_id 求最大 trade_date，再回表取行情字段；
    这种写法在 SQLite 与 PostgreSQL 上都可用。
    """

    if not instrument_ids:
        return {}
    latest_dates = (
        select(
            DailyBar.instrument_id.label("instrument_id"),
            func.max(DailyBar.trade_date).label("trade_date"),
        )
        .where(DailyBar.instrument_id.in_(instrument_ids))
        .group_by(DailyBar.instrument_id)
        .subquery("latest_dates")
    )
    rows = db.execute(
        select(
            DailyBar.instrument_id,
            DailyBar.trade_date,
            DailyBar.close,
            DailyBar.pct_chg,
        )
        .select_from(DailyBar)
        .join(
            latest_dates,
            and_(
                DailyBar.instrument_id == latest_dates.c.instrument_id,
                DailyBar.trade_date == latest_dates.c.trade_date,
            ),
        )
        .where(DailyBar.instrument_id.in_(instrument_ids))
    ).mappings().all()
    return {row["instrument_id"]: row for row in rows}


def _resolve_instrument(db: Session, raw: str) -> Instrument:
    """把 600519 / 600519.SH / 名称 解析成唯一的标的。

    顺序：精确 symbol → 精确 code → 名称精确 → 名称模糊唯一。
    多候选时抛 ToolArgumentError 并列出候选。
    """

    text = (raw or "").strip()
    if not text:
        raise ToolArgumentError("缺少标的代码或名称")

    base = (
        select(Instrument)
        .where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
        )
    )
    candidates: list[Instrument] = []

    direct = db.scalar(base.where(Instrument.symbol == text.upper()).limit(1))
    if direct is not None:
        return direct

    candidates = list(
        db.scalars(base.where(Instrument.code == text).limit(MAX_CANDIDATES + 1))
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _raise_ambiguous(text, candidates)

    candidates = list(
        db.scalars(base.where(Instrument.name == text).limit(MAX_CANDIDATES + 1))
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        _raise_ambiguous(text, candidates)

    candidates = list(
        db.scalars(
            base.where(
                or_(
                    Instrument.name.ilike(f"%{text}%"),
                    Instrument.symbol.ilike(f"%{text}%"),
                    Instrument.code.ilike(f"%{text}%"),
                )
            )
            .order_by(Instrument.symbol)
            .limit(MAX_CANDIDATES + 1)
        )
    )
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        _raise_ambiguous(text, candidates)

    raise ToolArgumentError(
        f"未找到标的「{text}」，可先用 search_instruments 搜索代码或名称"
    )


def _raise_ambiguous(text: str, candidates: list[Instrument]) -> None:
    preview = "、".join(
        f"{item.symbol}({item.name})" for item in candidates[:MAX_CANDIDATES]
    )
    raise ToolArgumentError(
        f"「{text}」匹配到多个标的：{preview}；请改用更精确的证券代码"
    )


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _search_instruments(
    db: Session, arguments: dict[str, Any]
) -> dict[str, Any]:
    query = _as_str_arg(arguments, "query", required=True)
    limit = _as_int_arg(
        arguments, "limit", DEFAULT_SEARCH_LIMIT, minimum=1, maximum=MAX_LIMIT
    )
    pattern = f"%{query}%"
    rows = db.execute(
        select(
            Instrument.id,
            Instrument.symbol,
            Instrument.code,
            Instrument.name,
            Instrument.board,
            Instrument.industry,
            Instrument.is_st,
        )
        .where(
            Instrument.asset_type == "stock",
            Instrument.status == "active",
            or_(
                Instrument.symbol.ilike(pattern),
                Instrument.code.ilike(pattern),
                Instrument.name.ilike(pattern),
            ),
        )
        # 精确匹配优先：symbol / code / name 完全一致排在模糊命中之前
        .order_by(
            (Instrument.symbol == query.upper()).desc(),
            (Instrument.code == query).desc(),
            (Instrument.name == query).desc(),
            Instrument.symbol,
        )
        .limit(limit)
    ).mappings().all()
    if not rows:
        return {"query": query, "total": 0, "items": []}

    # 第二条批量查询取这些标的各自的最新日线，避免 N+1
    latest = _latest_bars(db, [row["id"] for row in rows])
    items = []
    for row in rows:
        bar = latest.get(row["id"])
        items.append(
            {
                "symbol": row["symbol"],
                "code": row["code"],
                "name": row["name"],
                "board": row["board"],
                "industry": row["industry"],
                "is_st": bool(row["is_st"]),
                "close": _num(bar["close"]) if bar is not None else None,
                "pct_chg": _num(bar["pct_chg"]) if bar is not None else None,
            }
        )
    return {"query": query, "total": len(items), "items": items}


def _get_instrument_profile(
    db: Session, arguments: dict[str, Any]
) -> dict[str, Any]:
    symbol = _as_str_arg(arguments, "symbol", required=True)
    instrument = _resolve_instrument(db, symbol)

    bar = db.execute(
        select(
            DailyBar.trade_date,
            DailyBar.close,
            DailyBar.pct_chg,
            DailyBar.pe_ttm,
            DailyBar.pb,
            DailyBar.turnover_rate,
            DailyBar.amount,
        )
        .where(DailyBar.instrument_id == instrument.id)
        .order_by(DailyBar.trade_date.desc())
        .limit(1)
    ).mappings().first()

    fin = db.execute(
        select(
            FinancialReport.report_period,
            FinancialReport.revenue_yoy,
            FinancialReport.net_profit_yoy,
            FinancialReport.roe,
            FinancialReport.gross_margin,
            FinancialReport.debt_ratio,
            FinancialReport.eps,
            FinancialReport.bps,
        )
        .where(FinancialReport.instrument_id == instrument.id)
        .order_by(
            FinancialReport.report_period.desc(),
            FinancialReport.announce_date.desc(),
        )
        .limit(1)
    ).mappings().first()

    holder = db.execute(
        select(
            ShareholderCount.period_end,
            ShareholderCount.holder_num,
            ShareholderCount.holder_change_pct,
        )
        .where(ShareholderCount.instrument_id == instrument.id)
        .order_by(
            ShareholderCount.period_end.desc(),
            ShareholderCount.announce_date.desc(),
        )
        .limit(1)
    ).mappings().first()

    return {
        "symbol": instrument.symbol,
        "code": instrument.code,
        "name": instrument.name,
        "board": instrument.board,
        "industry": instrument.industry,
        "is_st": bool(instrument.is_st),
        "latest_bar": None
        if bar is None
        else {
            "trade_date": _date_str(bar["trade_date"]),
            "close": _num(bar["close"]),
            "pct_chg": _num(bar["pct_chg"]),
            "pe_ttm": _num(bar["pe_ttm"]),
            "pb": _num(bar["pb"]),
            "turnover_rate": _num(bar["turnover_rate"]),
            "amount": _num(bar["amount"], 2),
        },
        "latest_report": None
        if fin is None
        else {
            "report_period": _date_str(fin["report_period"]),
            "revenue_yoy": _num(fin["revenue_yoy"]),
            "net_profit_yoy": _num(fin["net_profit_yoy"]),
            "roe": _num(fin["roe"]),
            "gross_margin": _num(fin["gross_margin"]),
            "debt_ratio": _num(fin["debt_ratio"]),
            "eps": _num(fin["eps"]),
            "bps": _num(fin["bps"]),
        },
        "latest_holder": None
        if holder is None
        else {
            "period_end": _date_str(holder["period_end"]),
            "holder_num": _int(holder["holder_num"]),
            "holder_change_pct": _num(holder["holder_change_pct"]),
        },
        "note": DATA_NOTE,
    }


def _get_price_history(
    db: Session, arguments: dict[str, Any]
) -> dict[str, Any]:
    symbol = _as_str_arg(arguments, "symbol", required=True)
    days = _as_int_arg(
        arguments,
        "days",
        DEFAULT_HISTORY_DAYS,
        minimum=MIN_HISTORY_DAYS,
        maximum=MAX_HISTORY_DAYS,
    )
    instrument = _resolve_instrument(db, symbol)

    rows = db.execute(
        select(
            DailyBar.trade_date,
            DailyBar.close,
            DailyBar.close_adj,
            DailyBar.high,
            DailyBar.low,
            DailyBar.amount,
            DailyBar.turnover_rate,
        )
        .where(DailyBar.instrument_id == instrument.id)
        .order_by(DailyBar.trade_date.desc())
        .limit(days)
    ).mappings().all()

    if not rows:
        return {
            "symbol": instrument.symbol,
            "code": instrument.code,
            "name": instrument.name,
            "days": days,
            "bars": 0,
            "message": "该标的没有可用的日线数据",
            "note": DATA_NOTE,
        }

    ordered = list(reversed(rows))  # 按日期升序
    first, last = ordered[0], ordered[-1]
    # 涨跌幅优先用前复权收盘，缺失回退普通收盘
    start_close = first["close_adj"] if first["close_adj"] is not None else first["close"]
    end_close = last["close_adj"] if last["close_adj"] is not None else last["close"]
    change_pct: float | None = None
    if start_close is not None and end_close is not None:
        start_value = float(start_close)
        if start_value != 0:
            change_pct = _num((float(end_close) / start_value - 1) * 100)

    highs = [float(row["high"]) for row in ordered if row["high"] is not None]
    lows = [float(row["low"]) for row in ordered if row["low"] is not None]
    amounts = [float(row["amount"]) for row in ordered if row["amount"] is not None]
    turnovers = [
        float(row["turnover_rate"])
        for row in ordered
        if row["turnover_rate"] is not None
    ]

    latest_close = last["close"] if last["close"] is not None else end_close

    return {
        "symbol": instrument.symbol,
        "code": instrument.code,
        "name": instrument.name,
        "days": days,
        "bars": len(ordered),
        "start_date": _date_str(first["trade_date"]),
        "end_date": _date_str(last["trade_date"]),
        "latest_close": _num(latest_close),
        "change_pct": change_pct,
        "high": _num(max(highs)) if highs else None,
        "low": _num(min(lows)) if lows else None,
        "avg_amount": _num(sum(amounts) / len(amounts), 2) if amounts else None,
        "avg_turnover_rate": _num(sum(turnovers) / len(turnovers)) if turnovers else None,
        "note": DATA_NOTE,
    }


@contextmanager
def _sqlite_statement_timeout_compat(db: Session):
    """SQLite 不支持 `SET LOCAL statement_timeout`，在测试/本地库上跳过它。

    build_screen 会执行 PostgreSQL 专有的 SET LOCAL；这里只在该会话上临时
    忽略这一条语句，生产 PostgreSQL 行为完全不变。
    """

    bind = db.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        yield
        return
    original = db.execute

    def _execute(statement, *args, **kwargs):
        if str(statement).strip().upper().startswith("SET LOCAL"):
            return None
        return original(statement, *args, **kwargs)

    db.execute = _execute  # type: ignore[method-assign]
    try:
        yield
    finally:
        db.execute = original  # type: ignore[method-assign]


def _screen_instruments(
    db: Session, arguments: dict[str, Any]
) -> dict[str, Any]:
    raw_conditions = arguments.get("conditions")
    if raw_conditions is None:
        raise ToolArgumentError(
            "缺少参数 conditions，可先调用 list_screening_factors 了解可用字段"
        )
    if not isinstance(raw_conditions, list):
        raise ToolArgumentError("参数 conditions 必须是数组")
    if not raw_conditions:
        raise ToolArgumentError("参数 conditions 不能为空")
    limit = _as_int_arg(
        arguments, "limit", DEFAULT_SEARCH_LIMIT, minimum=1, maximum=MAX_LIMIT
    )

    conditions: list[ConditionIn] = []
    known_keys = {factor.key: factor for factor in FACTORS}
    for index, raw in enumerate(raw_conditions):
        if not isinstance(raw, dict):
            raise ToolArgumentError(f"第 {index + 1} 个条件必须是对象")
        field = raw.get("field")
        if not isinstance(field, str) or not field.strip():
            raise ToolArgumentError(f"第 {index + 1} 个条件缺少字段名 field")
        field = field.strip()
        if field not in known_keys:
            raise ToolArgumentError(
                f"未知筛选字段：{field}；可先调用 list_screening_factors 查看可用字段"
            )
        op = raw.get("op")
        if not isinstance(op, str) or not op.strip():
            raise ToolArgumentError(f"第 {index + 1} 个条件缺少操作符 op")
        if "value" not in raw:
            raise ToolArgumentError(f"第 {index + 1} 个条件缺少 value")
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise ToolArgumentError(f"第 {index + 1} 个条件的 params 必须是对象")
        conditions.append(
            ConditionIn(field=field, op=op.strip(), value=raw["value"], params=params)
        )

    request = ScreenRequest(
        logic="and",
        conditions=conditions,
        page=1,
        page_size=SCREEN_FETCH_PAGESIZE,
    )
    try:
        with _sqlite_statement_timeout_compat(db):
            result = build_screen(
                db,
                request,
                max_window_days=MAX_HISTORY_DAYS,
                statement_timeout_ms=30000,
            )
    except FactorError as exc:
        raise ToolArgumentError(
            f"筛选条件不合法：{exc}；可先调用 list_screening_factors 查看可用字段"
        ) from exc
    except ScreenError as exc:
        raise ToolArgumentError(f"筛选失败：{exc}") from exc

    # 数据日期：优先 build_screen 解析出的交易日，缺失时用本地最新日线兜底
    as_of = result.get("as_of")
    if as_of is None:
        as_of = latest_stock_bar_date(db)

    rows = result.get("rows") or []
    items = [
        {
            "symbol": row.get("symbol"),
            "code": str(row.get("symbol") or "").split(".")[0],
            "name": row.get("name"),
            "close": _num(row.get("close")),
            "pct_chg": _num(row.get("pct_chg")),
        }
        for row in rows[:limit]
    ]
    return {
        "as_of": _date_str(as_of),
        "total": int(result.get("total") or 0),
        "returned": len(items),
        "items": items,
        "note": DATA_NOTE,
    }


def _list_screening_factors(
    db: Session, arguments: dict[str, Any]
) -> dict[str, Any]:
    del arguments  # 无参数
    # 字段说明与单位不进 JSON（体积会超过单次结果上限），保持结构精简
    factors = [
        {
            "key": item["key"],
            "label": item["label"],
            "category": item["category"],
            "value_type": item["value_type"],
            "ops": list(item["ops"]),
            "params": [
                {"name": param["name"], "default": param["default"]}
                for param in item["params"]
            ],
        }
        for item in factor_metadata()
    ]
    return {"factors": factors, "total": len(factors)}


def _web_search_tool(
    arguments: dict[str, Any], *, snapshot: Any
) -> dict[str, Any]:
    if not _web_search_enabled(snapshot):
        raise ToolArgumentError(WEB_SEARCH_DISABLED)
    query = _as_str_arg(arguments, "query", required=True)
    limit = _as_int_arg(arguments, "limit", 5, minimum=1, maximum=MAX_LIMIT)
    hits = _web_search(query, snapshot=snapshot, limit=limit)
    results = [
        {
            "title": hit.get("title"),
            "url": hit.get("url"),
            "snippet": hit.get("snippet"),
        }
        for hit in hits or []
    ]
    return {"query": query, "results": results}


def _web_search_enabled(snapshot: Any) -> bool:
    try:
        return bool(snapshot.get_bool("llm.web_search_enabled"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "search_instruments": _search_instruments,
    "get_instrument_profile": _get_instrument_profile,
    "get_price_history": _get_price_history,
    "screen_instruments": _screen_instruments,
    "list_screening_factors": _list_screening_factors,
}
_DB_TOOLS = frozenset(_TOOLS)


def run_tool(
    name: str, arguments: dict[str, Any], *, session_factory, snapshot
) -> str:
    """执行工具，返回 JSON 字符串（ensure_ascii=False）。任何错误都返回中文错误字符串，绝不抛异常。"""

    try:
        if not isinstance(name, str) or not name.strip():
            return _error("缺少工具名称")
        name = name.strip()
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(f"工具 {name} 的参数必须是 JSON 对象")

        if name == "web_search":
            payload = _web_search_tool(arguments, snapshot=snapshot)
            return _dumps(payload)

        handler = _TOOLS.get(name)
        if handler is None:
            return _error(f"未知工具：{name}")

        with session_factory() as db:
            payload = handler(db, arguments)
        return _dumps(payload)
    except ToolArgumentError as exc:
        return _error(str(exc))
    except WebSearchError as exc:
        logger.warning("网页搜索失败：%s", exc)
        return _error(f"网页搜索失败：{exc}")
    except Exception as exc:  # 兜底：任何异常都转成中文错误文本
        logger.warning("工具 %s 执行异常：%s: %s", name, type(exc).__name__, exc)
        return _error(f"工具 {name} 执行失败：{type(exc).__name__}: {exc}")
