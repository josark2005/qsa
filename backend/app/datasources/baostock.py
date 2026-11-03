from __future__ import annotations

import logging
import os
import socket
import threading
import time
from urllib.parse import unquote, urlparse

import baostock as bs
import baostock.common.context as bs_context
import pandas as pd

from app.datasources.base import RateLimiter, retry_call
from app.datasources.symbols import to_baostock_symbol

logger = logging.getLogger(__name__)
DEFAULT_SOCKET_TIMEOUT = 30
# 匿名登录握手实测要 10-40 秒（容器/限流时更长），30 秒会误判为网络错误，
# 因此登录单独用更长的超时，并允许一次重试。
LOGIN_SOCKET_TIMEOUT = 90
_LOGIN_ATTEMPTS = 2
_LOGIN_RETRY_DELAY_SECONDS = 5.0
BSERR_NO_LOGIN = "10001001"
# BaoStock 的网络类错误码（1000200x），出现时基本可以确定是出站连接/代理问题。
_SOCKET_ERROR_CODES = {
    "10002001",
    "10002002",
    "10002003",
    "10002004",
    "10002005",
    "10002006",
    "10002007",
    "10002008",
}

# baostock 的会话是进程级全局状态（baostock.common.context.default_socket），
# 同一进程内并发使用两个 BaoStockClient 会互相覆盖 socket，甚至让服务端会话失效，
# 因此用进程级锁把登录-查询-登出整段串行化。
_session_lock = threading.RLock()
_socks_configured = False
_socks_description = "direct TCP"
_socks_error: str | None = None


def _configure_baostock_socks() -> None:
    """Route BaoStock's raw TCP socket through SOCKS5 when configured."""
    global _socks_configured, _socks_description, _socks_error
    if _socks_configured:
        if _socks_error is not None:
            raise RuntimeError(_socks_error)
        return
    raw = os.getenv("BAOSTOCK_SOCKS_PROXY", "").strip()
    if not raw:
        _socks_configured = True
        _socks_description = "direct TCP"
        return
    if "://" not in raw:
        raw = f"socks5h://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname:
        _socks_configured = True
        _socks_error = (
            f"BAOSTOCK_SOCKS_PROXY 配置无效: {raw!r}，需要 socks5://host:port 或 "
            "socks5h://host:port（socks5h 由代理解析域名）"
        )
        raise RuntimeError(_socks_error)

    host = parsed.hostname
    port = parsed.port or 1080
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    try:
        import socks
        import baostock.common.contants as constants
        import baostock.util.socketutil as socketutil
    except ImportError as exc:  # pragma: no cover - PySocks 是必需依赖
        _socks_configured = True
        _socks_error = f"BAOSTOCK_SOCKS_PROXY 需要 PySocks：{exc}"
        raise RuntimeError(_socks_error) from exc

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
            rdns=parsed.scheme == "socks5h",
            username=username,
            password=password,
        )
        sock.settimeout(LOGIN_SOCKET_TIMEOUT)
        try:
            sock.connect((target, constants.BAOSTOCK_SERVER_PORT))
        except Exception as exc:
            raise RuntimeError(
                f"BaoStock SOCKS5 代理 {host}:{port} 连接失败: "
                f"{type(exc).__name__}: {exc}；请检查 BAOSTOCK_SOCKS_PROXY。"
                "容器内不要写 127.0.0.1（那是容器自身），可使用 "
                "host.docker.internal（compose 需声明 host-gateway）或宿主机 LAN IP"
            ) from exc
        setattr(bs_context, "default_socket", sock)

    socketutil.SocketUtil.connect = connect
    _socks_configured = True
    _socks_description = f"SOCKS5 {host}:{port}"
    logger.info("BaoStock SOCKS5 proxy configured: %s:%s", host, port)


def baostock_transport_description() -> str:
    """Return the effective BaoStock transport, raising on invalid proxy config."""
    _configure_baostock_socks()
    return _socks_description


def log_baostock_transport() -> str:
    """Log the effective BaoStock transport once at startup."""
    try:
        description = baostock_transport_description()
    except RuntimeError as exc:
        logger.error("BaoStock 出站连接配置无效：%s", exc)
        return "invalid"
    logger.info(
        "BaoStock 出站连接：%s（TCP public-api.baostock.com:10030）", description
    )
    return description


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
        self._lock_held = False

    def __enter__(self) -> BaoStockClient:
        self._acquire_session()
        try:
            self._login()
        except BaseException:
            self._release_session()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._logged_in:
                try:
                    bs.logout()
                except Exception:
                    logger.warning("BaoStock 登出失败，忽略", exc_info=True)
                self._logged_in = False
        finally:
            self._release_session()

    def _acquire_session(self) -> None:
        if not _session_lock.acquire(blocking=False):
            logger.warning(
                "进程内已有 BaoStock 会话，等待其结束（同一账号并发登录会互相顶掉）"
            )
            _session_lock.acquire()
        self._lock_held = True

    def _release_session(self) -> None:
        if self._lock_held:
            self._lock_held = False
            _session_lock.release()

    def _drop_socket(self) -> None:
        sock = getattr(bs_context, "default_socket", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        setattr(bs_context, "default_socket", None)

    def _login(self) -> None:
        _configure_baostock_socks()
        last_error: RuntimeError | None = None
        for attempt in range(1, _LOGIN_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                self._login_once()
            except RuntimeError as exc:
                last_error = exc
                if attempt >= _LOGIN_ATTEMPTS:
                    break
                logger.warning(
                    "BaoStock 登录失败（第 %s/%s 次，耗时 %.1fs）：%s；%.0fs 后重试",
                    attempt,
                    _LOGIN_ATTEMPTS,
                    time.monotonic() - started,
                    exc,
                    _LOGIN_RETRY_DELAY_SECONDS,
                )
                self._logged_in = False
                self._drop_socket()
                time.sleep(_LOGIN_RETRY_DELAY_SECONDS)
                continue
            logger.info(
                "BaoStock 登录成功（%s，耗时 %.1fs）",
                _socks_description,
                time.monotonic() - started,
            )
            return
        assert last_error is not None
        raise last_error

    def _login_once(self) -> None:
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(LOGIN_SOCKET_TIMEOUT)
        try:
            result = bs.login()
        except Exception as exc:
            raise RuntimeError(
                f"baostock login failed ({_socks_description}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            socket.setdefaulttimeout(previous_timeout)
        if result.error_code != "0":
            raise RuntimeError(self._login_error_message(result))
        sock = getattr(bs_context, "default_socket", None)
        if sock is not None:
            sock.settimeout(DEFAULT_SOCKET_TIMEOUT)
        self._logged_in = True

    @staticmethod
    def _login_error_message(result) -> str:
        message = (
            f"baostock login failed ({_socks_description}): "
            f"{result.error_code} {result.error_msg}"
        )
        if result.error_code in _SOCKET_ERROR_CODES:
            message += (
                "；BaoStock 走原始 TCP（public-api.baostock.com:10030），"
                "HTTP(S) 代理对它无效；出站受限时请设置 "
                "BAOSTOCK_SOCKS_PROXY=socks5h://host:port（容器内不要用 127.0.0.1）"
            )
        return message

    @staticmethod
    def _query_error_message(rs) -> str:
        message = f"baostock query failed: {rs.error_code} {rs.error_msg}"
        if rs.error_code == BSERR_NO_LOGIN:
            message += (
                "；当前会话已失效，可能被其它 BaoStock 会话顶掉"
                "（worker 定时任务、手动 CLI 回补或另一部署共用同一匿名账号），"
                "请避免并发使用 BaoStock"
            )
        return message

    def _relogin(self) -> None:
        logger.warning(
            "BaoStock 会话失效（10001001 用户未登录），重新登录后重试"
        )
        self._logged_in = False
        self._drop_socket()
        self._login()

    def _query(self, factory):
        try:
            result = factory()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"baostock query failed ({_socks_description}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if getattr(result, "error_code", None) == BSERR_NO_LOGIN and self._logged_in:
            # 另一个会话（同进程不会发生，因为持有进程锁）顶掉了当前会话时，
            # 重新登录一次通常可以恢复；仍失败则交给 retry_call/上层报错。
            self._relogin()
            result = factory()
        return self._fetch(result)

    def _run(self, factory, *, label: str) -> pd.DataFrame:
        def call() -> pd.DataFrame:
            self._limiter.wait()
            return self._query(factory)

        return retry_call(call, label=label)

    def _fetch(self, rs) -> pd.DataFrame:
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(self._query_error_message(rs))
        return pd.DataFrame(rows, columns=rs.fields)

    def fetch_all_basic(self) -> pd.DataFrame:
        return self._run(bs.query_stock_basic, label="baostock_all_basic")

    def fetch_trade_dates(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self._run(
            lambda: bs.query_trade_dates(start_date, end_date),
            label="baostock_trade_dates",
        )

    def fetch_stock_industry(self) -> pd.DataFrame:
        return self._run(bs.query_stock_industry, label="baostock_industry")

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
        return self._run(
            lambda: bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag,
            ),
            label=f"baostock_history:{symbol}",
        )

    def fetch_adjust_factors(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        code = to_baostock_symbol(symbol)
        return self._run(
            lambda: bs.query_adjust_factor(
                code=code, start_date=start_date, end_date=end_date
            ),
            label=f"baostock_adjust:{symbol}",
        )
