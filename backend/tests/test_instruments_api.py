from __future__ import annotations


def test_instrument_search_requires_auth(app_fixture) -> None:
    response = app_fixture.client.get("/api/instruments/search?q=600")
    assert response.status_code == 401


def test_instrument_search_and_missing_technical(app_fixture) -> None:
    app_fixture.login_admin()

    search = app_fixture.client.get("/api/instruments/search?q=600")
    assert search.status_code == 200
    assert search.json()["items"] == []

    technical = app_fixture.client.get("/api/instruments/600000.SH/technical")
    assert technical.status_code == 404
    assert technical.json()["code"] == "instrument_not_found"
