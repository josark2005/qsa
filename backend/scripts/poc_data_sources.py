from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import akshare as ak
import baostock as bs
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "poc" / datetime.now().strftime("%Y%m%d_%H%M%S")

LAST_TRADING_DAY = "20261030"
REPORT_PERIOD = "20260930"
FALLBACK_REPORT_PERIOD = "20260630"
ETF_CODE = "510300"


def _fetch_baostock_rows(rs) -> pd.DataFrame:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"baostock error {rs.error_code}: {rs.error_msg}")
    return pd.DataFrame(rows, columns=rs.fields)


def _baostock_history(
    code: str,
    fields: str,
    start_date: str = "2026-09-01",
    end_date: str = "2026-10-30",
    adjustflag: str = "3",
) -> pd.DataFrame:
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=adjustflag,
        )
        return _fetch_baostock_rows(rs)
    finally:
        bs.logout()


def test_stock_spot() -> pd.DataFrame:
    return ak.stock_zh_a_spot_em()


def test_etf_spot() -> pd.DataFrame:
    return ak.fund_etf_spot_em()


def test_etf_hist() -> pd.DataFrame:
    return ak.fund_etf_hist_em(
        symbol=ETF_CODE,
        period="daily",
        start_date="20250101",
        end_date=LAST_TRADING_DAY,
        adjust="qfq",
    )


def test_trade_calendar() -> pd.DataFrame:
    return ak.tool_trade_date_hist_sina()


def test_gdhs_latest() -> pd.DataFrame:
    return ak.stock_zh_a_gdhs(symbol="最新")


def test_gdhs_period() -> pd.DataFrame:
    return ak.stock_zh_a_gdhs(symbol="20260630")


def test_zt_pool_recent() -> pd.DataFrame:
    return ak.stock_zt_pool_em(date=LAST_TRADING_DAY)


def test_zt_pool_2024() -> pd.DataFrame:
    return ak.stock_zt_pool_em(date="20240102")


def test_zt_pool_2020() -> pd.DataFrame:
    return ak.stock_zt_pool_em(date="20200102")


def test_dt_pool_recent() -> pd.DataFrame:
    return ak.stock_zt_pool_dtgc_em(date=LAST_TRADING_DAY)


def test_stock_info() -> pd.DataFrame:
    return ak.stock_info_a_code_name()


def test_delisted_sh() -> pd.DataFrame:
    return ak.stock_info_sh_delist()


def test_delisted_sz() -> pd.DataFrame:
    return ak.stock_info_sz_delist()


def _financial_bulk(fetch) -> pd.DataFrame:
    for period in (REPORT_PERIOD, FALLBACK_REPORT_PERIOD):
        df = fetch(period)
        if df is not None and not df.empty:
            df.attrs["report_period"] = period
            return df
    return pd.DataFrame()


def test_yjbb() -> pd.DataFrame:
    return _financial_bulk(ak.stock_yjbb_em)


def test_zcfz() -> pd.DataFrame:
    return _financial_bulk(ak.stock_zcfz_em)


def test_lrb() -> pd.DataFrame:
    return _financial_bulk(ak.stock_lrb_em)


def test_xjll() -> pd.DataFrame:
    return _financial_bulk(ak.stock_xjll_em)


def test_bs_daily_stock() -> pd.DataFrame:
    fields = (
        "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
        "turn,tradestatus,pctChg,isST,peTTM,pbMRQ"
    )
    return _baostock_history("sh.600000", fields)


def test_bs_daily_etf() -> pd.DataFrame:
    fields = (
        "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
        "turn,tradestatus,pctChg"
    )
    return _baostock_history("sh.510300", fields)


def test_bs_adjust() -> pd.DataFrame:
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        rs = bs.query_adjust_factor(
            code="sh.600000", start_date="2025-01-01", end_date="2026-10-30"
        )
        return _fetch_baostock_rows(rs)
    finally:
        bs.logout()


def test_bs_all_stock() -> pd.DataFrame:
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        rs = bs.query_all_stock(day="2026-10-30")
        return _fetch_baostock_rows(rs)
    finally:
        bs.logout()


def test_bs_trade_dates() -> pd.DataFrame:
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        rs = bs.query_trade_dates(start_date="2026-01-01", end_date="2026-10-30")
        return _fetch_baostock_rows(rs)
    finally:
        bs.logout()


def test_bs_stock_basic() -> pd.DataFrame:
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        rs = bs.query_stock_basic(code="sh.600000")
        return _fetch_baostock_rows(rs)
    finally:
        bs.logout()


TESTS = {
    "trade_calendar": test_trade_calendar,
    "stock_spot": test_stock_spot,
    "stock_info": test_stock_info,
    "gdhs_latest": test_gdhs_latest,
    "gdhs_period": test_gdhs_period,
    "zt_pool_recent": test_zt_pool_recent,
    "zt_pool_2024": test_zt_pool_2024,
    "zt_pool_2020": test_zt_pool_2020,
    "dt_pool_recent": test_dt_pool_recent,
    "etf_spot": test_etf_spot,
    "etf_hist": test_etf_hist,
    "yjbb": test_yjbb,
    "zcfz": test_zcfz,
    "lrb": test_lrb,
    "xjll": test_xjll,
    "delisted_sh": test_delisted_sh,
    "delisted_sz": test_delisted_sz,
    "bs_daily_stock": test_bs_daily_stock,
    "bs_daily_etf": test_bs_daily_etf,
    "bs_adjust": test_bs_adjust,
    "bs_all_stock": test_bs_all_stock,
    "bs_trade_dates": test_bs_trade_dates,
    "bs_stock_basic": test_bs_stock_basic,
}


def _save_df(name: str, df: pd.DataFrame) -> str | None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = OUT_DIR / f"{name}.parquet"
    try:
        df.to_parquet(parquet_path, index=False)
        return str(parquet_path.relative_to(ROOT))
    except Exception:
        csv_path = OUT_DIR / f"{name}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        return str(csv_path.relative_to(ROOT))


def run_test(name: str, fn) -> dict:
    print(f"\n=== {name} ===")
    started = time.perf_counter()
    result: dict = {"name": name}
    try:
        df = fn()
        elapsed = round(time.perf_counter() - started, 3)
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
        result.update(
            {
                "status": "ok",
                "elapsed_seconds": elapsed,
                "rows": len(df),
                "columns": [str(c) for c in df.columns],
                "report_period": df.attrs.get("report_period"),
            }
        )
        if not df.empty:
            result["sample"] = json.loads(
                df.head(3).to_json(orient="records", force_ascii=False)
            )
            result["saved_to"] = _save_df(name, df)
        print(f"rows={len(df)} elapsed={elapsed}s")
        print("columns:", list(df.columns))
        if not df.empty:
            print(df.head(3).to_string())
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        result.update(
            {
                "status": "error",
                "elapsed_seconds": elapsed,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        print(f"ERROR after {elapsed}s: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated test names; default runs all",
    )
    args = parser.parse_args()
    selected = [s.strip() for s in args.only.split(",") if s.strip()] or list(TESTS)
    unknown = [name for name in selected if name not in TESTS]
    if unknown:
        raise SystemExit(f"unknown tests: {unknown}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "akshare_version": ak.__version__,
        "pandas_version": pd.__version__,
        "baostock_version": getattr(bs, "__version__", "unknown"),
        "tests": [],
    }
    for name in selected:
        summary["tests"].append(run_test(name, TESTS[name]))
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nsummary: {summary_path}")
    ok = sum(1 for item in summary["tests"] if item["status"] == "ok")
    print(f"passed {ok}/{len(summary['tests'])}")


if __name__ == "__main__":
    main()
