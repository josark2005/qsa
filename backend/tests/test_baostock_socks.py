from __future__ import annotations

from app.datasources import baostock as baostock_module


def test_baostock_socks_proxy_is_configured(monkeypatch) -> None:
    import baostock.util.socketutil as socketutil

    original_connect = socketutil.SocketUtil.connect
    monkeypatch.setenv("BAOSTOCK_SOCKS_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(baostock_module, "_socks_configured", False)
    try:
        baostock_module._configure_baostock_socks()
        assert socketutil.SocketUtil.connect is not original_connect
    finally:
        socketutil.SocketUtil.connect = original_connect
