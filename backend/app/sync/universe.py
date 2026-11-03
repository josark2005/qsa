from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.parsing import parse_date
from app.datasources.akshare import AkShareClient
from app.datasources.baostock import BaoStockClient
from app.datasources.symbols import (
    board_for_code,
    canonical_symbol,
    from_baostock_symbol,
    normalize_code,
)
from app.models.instrument import Instrument
from app.sync.db_utils import bulk_upsert

logger = logging.getLogger(__name__)


def _is_st(name: str) -> bool:
    return "ST" in name.upper()


def _infer_etf_fund_type(name: str) -> str | None:
    if "货币" in name:
        return "money"
    if "债" in name:
        return "bond"
    if any(
        token in name
        for token in ("黄金", "白银", "商品", "原油", "豆粕", "有色", "能源化工")
    ):
        return "commodity"
    if any(
        token in name
        for token in (
            "恒生",
            "港股",
            "纳斯达克",
            "标普",
            "日经",
            "德国",
            "法国",
            "QDII",
            "海外",
            "中概",
        )
    ):
        return "cross_border"
    return "equity"


def sync_universe(
    db: Session,
    *,
    ak_client: AkShareClient | None = None,
) -> dict[str, Any]:
    basic = pd.DataFrame()
    industry_raw = pd.DataFrame()
    try:
        with BaoStockClient() as client:
            basic = client.fetch_all_basic()
            industry_raw = client.fetch_stock_industry()
    except Exception:
        logger.warning(
            "BaoStock universe unavailable; falling back to AkShare",
            exc_info=True,
        )

    industry_map: dict[str, str] = {}
    if not industry_raw.empty:
        for _, row in industry_raw.iterrows():
            raw_code = str(row.get("code", ""))
            if "." not in raw_code:
                continue
            industry = row.get("industry")
            if industry:
                industry_map[from_baostock_symbol(raw_code)] = str(industry)

    ak = ak_client or AkShareClient()
    current_a = ak.fetch_stock_list()

    rows: dict[str, dict[str, Any]] = {}
    for _, row in basic.iterrows():
        raw_code = str(row.get("code", ""))
        if "." not in raw_code:
            continue
        canonical = from_baostock_symbol(raw_code)
        code, exchange = canonical.split(".", 1)
        basic_type = str(row.get("type", ""))
        if basic_type == "1":
            asset_type = "stock"
            board = board_for_code(code)
            fund_type = None
        elif basic_type == "5":
            asset_type = "etf"
            board = None
            fund_type = _infer_etf_fund_type(str(row.get("code_name", "")))
        else:
            continue
        name = str(row.get("code_name", "")).strip()
        status = "active" if str(row.get("status", "1")) == "1" else "delisted"
        rows[canonical] = {
            "symbol": canonical,
            "code": code,
            "exchange": exchange,
            "name": name,
            "asset_type": asset_type,
            "board": board,
            "status": status,
            "list_date": parse_date(row.get("ipoDate")),
            "delist_date": parse_date(row.get("outDate")),
            "is_st": _is_st(name),
            "industry": industry_map.get(canonical),
            "fund_type": fund_type,
            "source": "baostock",
        }

    for _, row in current_a.iterrows():
        code = normalize_code(row["code"])
        canonical = canonical_symbol(code)
        name = str(row.get("name", "")).strip()
        if canonical in rows:
            if rows[canonical]["status"] == "active":
                rows[canonical]["name"] = name
                rows[canonical]["is_st"] = _is_st(name)
            continue
        _, exchange = canonical.split(".", 1)
        rows[canonical] = {
            "symbol": canonical,
            "code": code,
            "exchange": exchange,
            "name": name,
            "asset_type": "stock",
            "board": "bse" if exchange == "BJ" else board_for_code(code),
            "status": "active",
            "list_date": None,
            "delist_date": None,
            "is_st": _is_st(name),
            "industry": None,
            "fund_type": None,
            "source": "akshare",
        }

    values = list(rows.values())
    count = bulk_upsert(
        db,
        Instrument,
        values,
        index_elements=["symbol"],
        update_columns=[
            "name",
            "asset_type",
            "board",
            "status",
            "list_date",
            "delist_date",
            "is_st",
            "industry",
            "fund_type",
            "source",
        ],
        chunk_size=1000,
    )
    db.commit()
    counts: dict[str, int] = {}
    for row in values:
        key = f"{row['asset_type']}:{row['status']}"
        counts[key] = counts.get(key, 0) + 1
    return {"rows": count, "counts": counts}


def load_instrument_map(db: Session) -> dict[str, Instrument]:
    instruments = db.scalars(select(Instrument)).all()
    return {instrument.code: instrument for instrument in instruments}


def list_instruments(
    db: Session,
    *,
    asset_type: str | None = None,
    include_delisted: bool = False,
    exchange: str | None = None,
    limit: int | None = None,
) -> list[Instrument]:
    statement = select(Instrument)
    if asset_type:
        statement = statement.where(Instrument.asset_type == asset_type)
    if not include_delisted:
        statement = statement.where(Instrument.status == "active")
    if exchange:
        statement = statement.where(Instrument.exchange == exchange)
    statement = statement.order_by(Instrument.symbol)
    if limit:
        statement = statement.limit(limit)
    return list(db.scalars(statement).all())
