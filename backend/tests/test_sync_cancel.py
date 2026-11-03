from __future__ import annotations

import pytest

from app.core.db import Base, make_engine, make_session_factory
from app.models.sync_request import SyncRequest
from app.sync.cancellation import SyncCancelled
from app.worker import RequestWorker


def test_cancel_queued_sync_request(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    created = app_fixture.client.post(
        "/api/admin/sync/requests",
        headers=csrf,
        json={
            "kind": "backfill",
            "source": "tencent",
            "asset_type": "all",
            "start": "2026-01-01",
            "end": "2026-01-02",
        },
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]

    cancelled = app_fixture.client.post(
        f"/api/admin/sync/requests/{request_id}/cancel",
        headers=csrf,
    )
    assert cancelled.status_code == 200, cancelled.text
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["error"] == "用户停止任务"
    assert payload["finished_at"] is not None

    again = app_fixture.client.post(
        f"/api/admin/sync/requests/{request_id}/cancel",
        headers=csrf,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "sync_request_finished"


def test_cancel_queued_history_import(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    created = app_fixture.client.post(
        "/api/admin/sync/requests",
        headers=csrf,
        json={"kind": "history_import", "force": True},
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["kind"] == "history_import"
    assert payload["payload"]["force"] is True

    cancelled = app_fixture.client.post(
        f"/api/admin/sync/requests/{payload['id']}/cancel",
        headers=csrf,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


def test_worker_stops_when_request_is_cancelling(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        entry = SyncRequest(
            kind="backfill",
            payload={},
            status="cancelling",
        )
        db.add(entry)
        db.commit()
        request_id = entry.id

    worker = RequestWorker(factory, engine)
    with pytest.raises(SyncCancelled):
        worker._update_progress(request_id, {"phase": "stock", "percent": 10})

    worker._finish(request_id, status="cancelled", error="用户停止任务")
    with factory() as db:
        entry = db.get(SyncRequest, request_id)
        assert entry is not None
        assert entry.status == "cancelled"
        assert entry.progress == {"phase": "cancelled", "message": "已停止"}

    engine.dispose()
