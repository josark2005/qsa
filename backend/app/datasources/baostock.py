from __future__ import annotations

import logging
import os
import socket
from urllib.parse import unquote, urlparse

import baostock as bs
import baostock.common.context as bs_context
import pandas as pd

from app.datasources.base import RateLimiter, retry_call
from app.datasources.symbols import to_baostock_symbol

logger = logging.getLogger(__name__)
DEFAULT_SOCKET_TIMEOUT = 30
_socks_configured = False


def _configure_baostock_socks() -> None:
    """Route BaoStock's raw TCP socket through SOCKS5 when configured."""
    global _socks_configured
    if _socks_configured:
        return
    raw = os.getenv("BAOSTOCK_SOCKS_PROXY", "").strip()
    if not raw:
        return
    if "://" not in raw:
        raw = f"socks5h://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname:
        logger.warning("invalid BAOSTOCK_SOCKS_PROXY: %s", raw)
        return

    import socks
    import baostock.common.contants as constants
    import baostock.util.socketutil as socketutil

    host = parsed.hostname
    port = parsed.port or 1080
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    def connect(self, api_key: str) -> None:
        target = (
            constants.BAOSTOCK_VIP_SERVER_IP
            if str(api_key).startswith("bs-")
            else constants.BAOSTOCK_SERVER_IP
        )
        sock = socks.socksocket()
        sock.set_proxy(
            socks.SOCKS5,
            host,
            port,
            rdns=True,
            username=username,
            password=password,
        )
        sock.settimeout(DEFAULT_SOCKET_TIMEOUT)
        sock.connect((target, constants.BAOSTOCK_SERVER_PORT))
        setattr(bs_context, "default_socket", sock)

    socketutil.SocketUtil.connect = connect
    _socks_configured = True
    logger.info("BaoStock SOCKS5 proxy configured: %s:%s", host, port)

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
        _configure_baostock_socks()
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(DEFAULT_SOCKET_TIMEOUT)
        try:
            result = bs.login()
        finally:
            socket.setdefaulttimeout(previous_timeout)
        if result.error_code != "0":
            raise RuntimeError(f"baostock login failed: {result.error_msg}")
        sock = getattr(bs_context, "default_socket", None)
        if sock is not None:
            sock.settimeout(DEFAULT_SOCKET_TIMEOUT)
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
