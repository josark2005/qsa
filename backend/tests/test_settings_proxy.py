from __future__ import annotations

import os

from app.core.settings_service import _PROXY_KEYS, _apply_proxy_url


def test_proxy_environment_apply_and_restore(monkeypatch) -> None:
    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)

    _apply_proxy_url("http://127.0.0.1:7890")
    for key in _PROXY_KEYS:
        assert os.environ[key] == "http://127.0.0.1:7890"

    _apply_proxy_url("")
    for key in _PROXY_KEYS:
        assert key not in os.environ
