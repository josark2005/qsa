from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppConfig
from app.core.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.services.users import create_user

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPassword123"


@dataclass
class AppFixture:
    client: TestClient
    admin_username: str = ADMIN_USERNAME
    admin_password: str = ADMIN_PASSWORD

    def login(self, username: str, password: str) -> dict:
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def login_admin(self) -> dict:
        return self.login(self.admin_username, self.admin_password)

    @staticmethod
    def csrf_headers(csrf_token: str) -> dict[str, str]:
        return {"X-CSRF-Token": csrf_token}


@pytest.fixture()
def app_fixture(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as db:
        admin, _ = create_user(
            db,
            username=ADMIN_USERNAME,
            role="admin",
            password=ADMIN_PASSWORD,
            created_by=None,
            min_password_length=12,
        )
        admin.must_change_password = False
        db.commit()
    engine.dispose()

    config = AppConfig(
        app_env="test",
        database_url=db_url,
        use_fake_redis=True,
        session_cookie_secure=False,
        log_level="WARNING",
        settings_poll_seconds=30,
    )
    app = create_app(config)
    with TestClient(app) as client:
        # 停掉后台 SettingsWatcher：它每 30s 从数据库重新 load 配置，会覆盖
        # 测试里设置的 SettingsSnapshot（CI 上曾因此偶发 notified/transport 断言失败）。
        client.app.state.container.settings_watcher.stop()
        yield AppFixture(client=client)


_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_host(host: str) -> bool:
    if host in _LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    """CI 中禁止测试发起真实外网连接（本地开发不强制）。

    所有测试都应使用 fakeredis / TestClient / monkeypatch，不应依赖网络；
    万一有测试尝试连接外网，会在这里立刻失败并指出目标地址。
    """
    if not os.environ.get("CI"):
        return
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        host_text = str(host)
        if not _is_local_host(host_text):
            raise RuntimeError(f"测试不允许访问外部网络：{host_text}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
