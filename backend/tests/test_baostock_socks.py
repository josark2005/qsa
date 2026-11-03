from __future__ import annotations

import threading
import time

import pytest

from app.datasources import baostock as baostock_module
from app.datasources.baostock import BaoStockClient


class FakeResult:
    def __init__(
        self,
        error_code: str = "0",
        error_msg: str = "success",
        rows: list[list[str]] | None = None,
        fields: list[str] | None = None,
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self._rows = list(rows or [])
        self.fields = list(fields or [])
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


class FakeLogin:
    def __init__(self, error_code: str = "0", error_msg: str = "success") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


@pytest.fixture(autouse=True)
def reset_baostock_state(monkeypatch):
    import baostock.util.socketutil as socketutil

    original_connect = socketutil.SocketUtil.connect
    monkeypatch.setattr(baostock_module, "_socks_configured", False)
    monkeypatch.setattr(baostock_module, "_socks_error", None)
    monkeypatch.setattr(baostock_module, "_socks_description", "direct TCP")
    monkeypatch.setattr(baostock_module, "_LOGIN_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.delenv("BAOSTOCK_SOCKS_PROXY", raising=False)
    yield
    socketutil.SocketUtil.connect = original_connect


def test_baostock_direct_transport_by_default(monkeypatch) -> None:
    import baostock.util.socketutil as socketutil

    original_connect = socketutil.SocketUtil.connect
    assert baostock_module.baostock_transport_description() == "direct TCP"
    assert socketutil.SocketUtil.connect is original_connect


def test_baostock_socks_proxy_is_configured(monkeypatch) -> None:
    import baostock.util.socketutil as socketutil

    original_connect = socketutil.SocketUtil.connect
    monkeypatch.setenv("BAOSTOCK_SOCKS_PROXY", "socks5h://127.0.0.1:1080")
    assert baostock_module.baostock_transport_description() == "SOCKS5 127.0.0.1:1080"
    assert socketutil.SocketUtil.connect is not original_connect


def test_baostock_socks_proxy_without_scheme_and_default_port(monkeypatch) -> None:
    monkeypatch.setenv("BAOSTOCK_SOCKS_PROXY", "proxy.local")
    assert baostock_module.baostock_transport_description() == "SOCKS5 proxy.local:1080"


def test_baostock_socks_invalid_scheme_raises(monkeypatch) -> None:
    monkeypatch.setenv("BAOSTOCK_SOCKS_PROXY", "http://127.0.0.1:7890")
    with pytest.raises(RuntimeError, match="BAOSTOCK_SOCKS_PROXY 配置无效"):
        baostock_module.baostock_transport_description()


def test_baostock_login_failure_reports_transport(monkeypatch) -> None:
    monkeypatch.setattr(baostock_module, "bs", _FakeBs(login=lambda: FakeLogin("10002007", "网络接收错误。")))
    with pytest.raises(RuntimeError) as excinfo:
        with BaoStockClient():
            pass
    message = str(excinfo.value)
    assert "direct TCP" in message
    assert "10002007" in message
    assert "BAOSTOCK_SOCKS_PROXY" in message

    # 登录失败也必须释放进程锁，后续客户端不能被永久阻塞。
    monkeypatch.setattr(baostock_module, "bs", _FakeBs(login=lambda: FakeLogin()))
    with BaoStockClient():
        pass


def test_baostock_login_retries_once_after_timeout(monkeypatch) -> None:
    login_calls = 0

    def flaky_login() -> FakeLogin:
        nonlocal login_calls
        login_calls += 1
        if login_calls == 1:
            return FakeLogin("10002007", "网络接收错误。")
        return FakeLogin()

    monkeypatch.setattr(baostock_module, "bs", _FakeBs(login=flaky_login))
    with BaoStockClient():
        pass
    assert login_calls == 2


def test_baostock_relogin_recovers_when_session_is_stolen(monkeypatch) -> None:
    login_calls = 0

    def fake_login() -> FakeLogin:
        nonlocal login_calls
        login_calls += 1
        return FakeLogin()

    query_calls = 0

    def fake_query(start_date: str, end_date: str) -> FakeResult:
        nonlocal query_calls
        query_calls += 1
        if query_calls == 1:
            return FakeResult("10001001", "用户未登录")
        return FakeResult(
            rows=[["2026-01-05", "1"]],
            fields=["calendar_date", "is_trading_day"],
        )

    fake_bs = _FakeBs(login=fake_login, query_trade_dates=fake_query)
    monkeypatch.setattr(baostock_module, "bs", fake_bs)

    with BaoStockClient() as client:
        frame = client.fetch_trade_dates("2026-01-01", "2026-01-10")

    assert list(frame.columns) == ["calendar_date", "is_trading_day"]
    assert len(frame) == 1
    assert login_calls == 2
    assert query_calls == 2


def test_baostock_query_failure_mentions_session_conflict(monkeypatch) -> None:
    monkeypatch.setattr(baostock_module, "retry_call", lambda call, label: call())
    monkeypatch.setattr(
        baostock_module,
        "bs",
        _FakeBs(
            login=lambda: FakeLogin(),
            query_trade_dates=lambda start, end: FakeResult("10001001", "用户未登录"),
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        with BaoStockClient() as client:
            client.fetch_trade_dates("2026-01-01", "2026-01-10")
    assert "会话已失效" in str(excinfo.value)
    assert "10001001" in str(excinfo.value)


def test_baostock_clients_are_serialized_within_process(monkeypatch) -> None:
    monkeypatch.setattr(baostock_module, "bs", _FakeBs(login=lambda: FakeLogin()))

    active = 0
    peak = 0
    guard = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()

    def run(wait_for_release: bool) -> None:
        nonlocal active, peak
        with BaoStockClient():
            with guard:
                active += 1
                peak = max(peak, active)
            if wait_for_release:
                first_entered.set()
                release_first.wait(5)
            with guard:
                active -= 1

    first = threading.Thread(target=run, args=(True,))
    first.start()
    assert first_entered.wait(2)

    second = threading.Thread(target=run, args=(False,))
    second.start()
    # 第二个客户端必须被进程锁挡住，不会并发登录。
    time.sleep(0.2)
    assert peak == 1

    release_first.set()
    first.join(5)
    second.join(5)
    assert peak == 1


class _FakeBs:
    """Minimal stand-in for the baostock module used by BaoStockClient."""

    def __init__(self, login=None, logout=None, query_trade_dates=None, **extra) -> None:
        self.login = login or (lambda *args, **kwargs: FakeLogin())
        self.logout = logout or (lambda *args, **kwargs: None)
        if query_trade_dates is not None:
            self.query_trade_dates = query_trade_dates
        for name, value in extra.items():
            setattr(self, name, value)
