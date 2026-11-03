from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.core.parsing import parse_date, to_float, to_int
from app.datasources.akshare import AkShareClient, FinancialBundle
from app.models.financial_report import FinancialReport
from app.models.instrument import Instrument
from app.models.shareholder_count import ShareholderCount
from app.sync.db_utils import bulk_upsert, clean_row
from app.sync.universe import load_instrument_map

logger = logging.getLogger(__name__)

REPORT_UPDATE_COLUMNS = [
    "report_type",
    "revenue",
    "revenue_yoy",
    "net_profit",
    "net_profit_yoy",
    "eps",
    "bps",
    "roe",
    "gross_margin",
    "ocf_per_share",
    "total_assets",
    "total_liabilities",
    "debt_ratio",
    "operating_cash_flow",
    "industry",
    "raw",
    "source",
]

HOLDER_UPDATE_COLUMNS = [
    "holder_num",
    "holder_num_prev",
    "holder_change",
    "holder_change_pct",
    "avg_holding_value",
    "avg_holding_shares",
    "total_mv",
    "total_shares",
    "source",
]


def quarter_ends(today: date, count: int) -> list[date]:
    ends: list[date] = []
    year = today.year
    quarter_ends_in_year = [
        date(year, 3, 31),
        date(year, 6, 30),
        date(year, 9, 30),
        date(year, 12, 31),
    ]
    available = [end for end in quarter_ends_in_year if end <= today]
    candidate = max(available) if available else date(year - 1, 12, 31)
    while len(ends) < count:
        ends.append(candidate)
        month = candidate.month
        if month == 3:
            candidate = date(candidate.year - 1, 12, 31)
        elif month == 6:
            candidate = date(candidate.year, 3, 31)
        elif month == 9:
            candidate = date(candidate.year, 6, 30)
        else:
            candidate = date(candidate.year, 9, 30)
    return ends


def report_type_for(period: date) -> str:
    if period.month == 3:
        return "Q1"
    if period.month == 6:
        return "H1"
    if period.month == 9:
        return "Q3"
    return "FY"


def _index_frame(df: pd.DataFrame, code_column: str = "股票代码") -> dict[str, pd.Series]:
    if df is None or df.empty or code_column not in df.columns:
        return {}
    frame = df.copy()
    frame[code_column] = frame[code_column].astype(str).str.zfill(6)
    frame = frame.drop_duplicates(code_column, keep="last")
    return {str(row[code_column]): row for _, row in frame.iterrows()}


def _raw_json(row: pd.Series) -> dict[str, Any]:
    return clean_row({str(key): value for key, value in row.items()})


def normalize_financial_bundle(
    bundle: FinancialBundle,
    instrument_map: dict[str, Instrument],
) -> list[dict]:
    yjbb = _index_frame(bundle.yjbb)
    zcfz = _index_frame(bundle.zcfz)
    lrb = _index_frame(bundle.lrb)
    xjll = _index_frame(bundle.xjll)
    period = date(
        int(bundle.period[0:4]), int(bundle.period[4:6]), int(bundle.period[6:8])
    )
    report_type = report_type_for(period)

    rows: list[dict] = []
    for code, base in yjbb.items():
        instrument = instrument_map.get(code)
        if instrument is None or instrument.asset_type != "stock":
            continue
        zc = zcfz.get(code)
        lr = lrb.get(code)
        xj = xjll.get(code)
        announce_date = (
            parse_date(base.get("最新公告日期"))
            or (parse_date(zc.get("公告日期")) if zc is not None else None)
            or (parse_date(lr.get("公告日期")) if lr is not None else None)
            or (parse_date(xj.get("公告日期")) if xj is not None else None)
        )
        if announce_date is None:
            continue
        revenue = to_float(base.get("营业总收入-营业总收入"))
        revenue_yoy = to_float(base.get("营业总收入-同比增长"))
        net_profit = to_float(base.get("净利润-净利润"))
        net_profit_yoy = to_float(base.get("净利润-同比增长"))
        if lr is not None:
            revenue = revenue if revenue is not None else to_float(lr.get("营业总收入"))
            revenue_yoy = (
                revenue_yoy
                if revenue_yoy is not None
                else to_float(lr.get("营业总收入同比"))
            )
            net_profit = (
                net_profit if net_profit is not None else to_float(lr.get("净利润"))
            )
            net_profit_yoy = (
                net_profit_yoy
                if net_profit_yoy is not None
                else to_float(lr.get("净利润同比"))
            )
        industry_value = base.get("所处行业")
        industry = (
            None
            if industry_value is None or pd.isna(industry_value)
            else str(industry_value)
        )
        rows.append(
            {
                "instrument_id": instrument.id,
                "report_period": period,
                "announce_date": announce_date,
                "report_type": report_type,
                "revenue": revenue,
                "revenue_yoy": revenue_yoy,
                "net_profit": net_profit,
                "net_profit_yoy": net_profit_yoy,
                "eps": to_float(base.get("每股收益")),
                "bps": to_float(base.get("每股净资产")),
                "roe": to_float(base.get("净资产收益率")),
                "gross_margin": to_float(base.get("销售毛利率")),
                "ocf_per_share": to_float(base.get("每股经营现金流量")),
                "total_assets": to_float(zc.get("资产-总资产")) if zc is not None else None,
                "total_liabilities": (
                    to_float(zc.get("负债-总负债")) if zc is not None else None
                ),
                "debt_ratio": to_float(zc.get("资产负债率")) if zc is not None else None,
                "operating_cash_flow": (
                    to_float(xj.get("经营性现金流-现金流量净额"))
                    if xj is not None
                    else None
                ),
                "industry": industry,
                "raw": _raw_json(base),
                "source": "akshare",
            }
        )
    return rows


def sync_financial_reports(
    db: Session,
    *,
    periods: list[str],
    client: AkShareClient | None = None,
) -> dict:
    ak = client or AkShareClient()
    instrument_map = load_instrument_map(db)
    total = 0
    per_period: dict[str, int] = {}
    for period in periods:
        bundle = ak.fetch_financial_bundle(period)
        rows = normalize_financial_bundle(bundle, instrument_map)
        written = bulk_upsert(
            db,
            FinancialReport,
            rows,
            index_elements=["instrument_id", "report_period", "announce_date"],
            update_columns=REPORT_UPDATE_COLUMNS,
            chunk_size=1000,
        )
        db.commit()
        per_period[period] = written
        total += written
        logger.info("financial period %s rows=%s", period, written)
    return {"rows": total, "periods": per_period}


def sync_shareholder_counts(
    db: Session,
    *,
    periods: list[str],
    client: AkShareClient | None = None,
) -> dict:
    ak = client or AkShareClient()
    instrument_map = load_instrument_map(db)
    total = 0
    per_period: dict[str, int] = {}
    for period in periods:
        raw = ak.fetch_shareholders(period)
        rows: list[dict] = []
        if not raw.empty:
            for _, record in raw.iterrows():
                code = str(record["code"]).zfill(6)
                instrument = instrument_map.get(code)
                if instrument is None or instrument.asset_type != "stock":
                    continue
                period_end = parse_date(record.get("period_end"))
                announce_date = parse_date(record.get("announce_date"))
                if period_end is None or announce_date is None:
                    continue
                rows.append(
                    {
                        "instrument_id": instrument.id,
                        "period_end": period_end,
                        "announce_date": announce_date,
                        "holder_num": to_int(record.get("holder_num")),
                        "holder_num_prev": to_int(record.get("holder_num_prev")),
                        "holder_change": to_int(record.get("holder_change")),
                        "holder_change_pct": to_float(
                            record.get("holder_change_pct")
                        ),
                        "avg_holding_value": to_float(
                            record.get("avg_holding_value")
                        ),
                        "avg_holding_shares": to_float(
                            record.get("avg_holding_shares")
                        ),
                        "total_mv": to_float(record.get("total_mv")),
                        "total_shares": to_float(record.get("total_shares")),
                        "source": "akshare",
                    }
                )
        written = bulk_upsert(
            db,
            ShareholderCount,
            rows,
            index_elements=["instrument_id", "period_end", "announce_date"],
            update_columns=HOLDER_UPDATE_COLUMNS,
            chunk_size=1000,
        )
        db.commit()
        per_period[period] = written
        total += written
        logger.info("shareholder period %s rows=%s", period, written)
    return {"rows": total, "periods": per_period}
