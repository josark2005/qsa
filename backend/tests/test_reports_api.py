from __future__ import annotations


def test_public_report_requires_only_token(app_fixture) -> None:
    response = app_fixture.client.get("/api/public/reports/not-a-real-token")
    assert response.status_code == 404
    assert response.json()["code"] == "report_not_found"


def test_report_creation_requires_auth(app_fixture) -> None:
    response = app_fixture.client.post(
        "/api/reports",
        json={
            "logic": "and",
            "conditions": [
                {
                    "field": "holder_change_pct",
                    "op": "lte",
                    "value": -5,
                    "params": {},
                }
            ],
            "sort": [],
        },
    )
    assert response.status_code == 401


def test_report_history_requires_auth_and_starts_empty(app_fixture) -> None:
    anonymous = app_fixture.client.get("/api/reports")
    assert anonymous.status_code == 401

    app_fixture.login_admin()
    response = app_fixture.client.get("/api/reports")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["limit"] == 20
    assert payload["offset"] == 0
