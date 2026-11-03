from __future__ import annotations


def test_health(app_fixture) -> None:
    response = app_fixture.client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] is True
    assert payload["redis"] is True


def test_login_me_and_no_registration(app_fixture) -> None:
    app_fixture.login_admin()
    me = app_fixture.client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"
    assert me.json()["user"]["username"] == "admin"
    assert "email" not in me.json()["user"]
    assert me.json()["csrf_token"]

    register = app_fixture.client.post(
        "/api/auth/register",
        json={"username": "x", "password": "whatever-password"},
    )
    assert register.status_code == 404


def test_settings_update_conflict_and_audit(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    response = app_fixture.client.get("/api/admin/settings")
    assert response.status_code == 200
    items = {item["key"]: item for item in response.json()["items"]}
    assert "sync.pipeline.market.cron" in items
    assert items["sync.pipeline.market.cron"]["value"] == "30 17 * * 1-5"

    patch = app_fixture.client.patch(
        "/api/admin/settings",
        headers=csrf,
        json={
            "items": [
                {
                    "key": "quota.screen_per_minute",
                    "value": 42,
                    "version": 0,
                }
            ]
        },
    )
    assert patch.status_code == 200, patch.text
    updated = {item["key"]: item for item in patch.json()["items"]}
    assert updated["quota.screen_per_minute"]["value"] == 42
    assert updated["quota.screen_per_minute"]["version"] == 1

    conflict = app_fixture.client.patch(
        "/api/admin/settings",
        headers=csrf,
        json={
            "items": [
                {
                    "key": "quota.screen_per_minute",
                    "value": 50,
                    "version": 0,
                }
            ]
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "settings_conflict"

    missing_csrf = app_fixture.client.patch(
        "/api/admin/settings",
        json={"items": [{"key": "quota.screen_per_minute", "value": 50, "version": 1}]},
    )
    assert missing_csrf.status_code == 403

    invalid_cron = app_fixture.client.patch(
        "/api/admin/settings",
        headers=csrf,
        json={
            "items": [
                {
                    "key": "sync.pipeline.market.cron",
                    "value": "* * * * *",
                    "version": 0,
                }
            ]
        },
    )
    assert invalid_cron.status_code == 422
    assert invalid_cron.json()["code"] == "settings_invalid"

    audit = app_fixture.client.get("/api/admin/audit-logs?action=settings.update")
    assert audit.status_code == 200
    assert audit.json()["total"] >= 1

    public = app_fixture.client.get("/api/settings/public")
    assert public.status_code == 200
    assert "screening.max_page_size" in public.json()["values"]


def test_admin_provisions_user_and_password_change(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    created = app_fixture.client.post(
        "/api/admin/users",
        headers=csrf,
        json={
            "username": "trader",
            "role": "user",
            "password": "InitialPassword123",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["user"]["must_change_password"] is True
    assert body["temporary_password"] is None
    user_id = body["user"]["id"]

    user_login = app_fixture.login("trader", "InitialPassword123")
    assert user_login["user"]["must_change_password"] is True
    user_csrf = app_fixture.csrf_headers(user_login["csrf_token"])

    blocked = app_fixture.client.get("/api/admin/users")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"

    changed = app_fixture.client.post(
        "/api/auth/change-password",
        headers=user_csrf,
        json={
            "current_password": "InitialPassword123",
            "new_password": "UpdatedPassword123",
        },
    )
    assert changed.status_code == 200, changed.text

    me = app_fixture.client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["must_change_password"] is False

    still_not_admin = app_fixture.client.get("/api/admin/users")
    assert still_not_admin.status_code == 403
    assert still_not_admin.json()["code"] == "admin_required"

    refreshed_admin_login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(refreshed_admin_login["csrf_token"])
    disable = app_fixture.client.patch(
        f"/api/admin/users/{user_id}",
        headers=csrf,
        json={"role": "user", "is_active": False, "quota_overrides": None},
    )
    assert disable.status_code == 200, disable.text

    disabled_login = app_fixture.client.post(
        "/api/auth/login",
        json={"username": "trader", "password": "UpdatedPassword123"},
    )
    assert disabled_login.status_code == 403
    assert disabled_login.json()["code"] == "user_disabled"


def test_last_admin_protection(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])
    admin_id = login["user"]["id"]

    response = app_fixture.client.patch(
        f"/api/admin/users/{admin_id}",
        headers=csrf,
        json={"role": "user", "is_active": True, "quota_overrides": None},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "last_admin"
