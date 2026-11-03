from __future__ import annotations

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
        yield AppFixture(client=client)
