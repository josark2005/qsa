from __future__ import annotations

import logging

import baostock as bs
import pandas as pd

from app.datasources.base import RateLimiter, retry_call
from app.datasources.symbols import to_baostock_symbol

logger = logging.getLogger(__name__)

STOCK_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST,peTTM,pbMRQ"
)
ETF_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg"
)


class BaoStockClient:
    def __init__(self, delay_seconds: float = 0.02) -> None:
        self._limiter = RateLimiter(delay_seconds)
        self._logged_in = False

    def __enter__(self) -> BaoStockClient:
        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(f"baostock login failed: {result.error_msg}")
        self._logged_in = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._logged_in:
            bs.logout()
            self._logged_in = False

    def _fetch(self, rs) -> pd.DataFrame:
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"baostock query failed: {rs.error_code} {rs.error_msg}")
        return pd.DataFrame(rows, columns=rs.fields)

    def fetch_all_basic(self) -> pd.DataFrame:
        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._fetch(bs.query_stock_basic())

        return retry_call(call, label="baostock_all_basic")

    def fetch_trade_dates(self, start_date: str, end_date: str) -> pd.DataFrame:
        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._fetch(bs.query_trade_dates(start_date, end_date))

        return retry_call(call, label="baostock_trade_dates")

    def fetch_stock_industry(self) -> pd.DataFrame:
        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._fetch(bs.query_stock_industry())

        return retry_call(call, label="baostock_industry")

    def fetch_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        asset_type: str = "stock",
        adjustflag: str = "3",
    ) -> pd.DataFrame:
        code = to_baostock_symbol(symbol)
        fields = STOCK_FIELDS if asset_type == "stock" else ETF_FIELDS

        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._fetch(
                bs.query_history_k_data_plus(
                    code,
                    fields,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag=adjustflag,
                )
            )

        return retry_call(call, label=f"baostock_history:{symbol}")

    def fetch_adjust_factors(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        code = to_baostock_symbol(symbol)

        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._fetch(
                bs.query_adjust_factor(
                    code=code, start_date=start_date, end_date=end_date
                )
            )

        return retry_call(call, label=f"baostock_adjust:{symbol}")
