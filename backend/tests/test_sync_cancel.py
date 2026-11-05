from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.db import Base, make_engine, make_session_factory, utcnow
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


def _create_cancelling_request(factory, *, age_seconds: int) -> int:
    """把 updated_at 往回拨来模拟「点了停止但后台迟迟不收敛」，测试里不做真实等待。

    用 with 关闭 session：TestClient 在别的线程里跑应用，SQLite 上留着的写事务
    会让接口请求一直等锁。
    """
    stale = utcnow() - timedelta(seconds=age_seconds)
    with factory() as db:
        entry = SyncRequest(
            kind="full_backfill",
            payload={},
            status="cancelling",
            started_at=stale,
            progress={"phase": "baostock_stock", "percent": 30},
            updated_at=stale,
        )
        db.add(entry)
        db.commit()
        return entry.id


def test_cancel_stuck_request_force_cancelled_after_grace(app_fixture, container) -> None:
    """超过 sync.cancel_grace_seconds 仍是 cancelling：接口强制置为 cancelled。"""
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    request_id = _create_cancelling_request(
        container.session_factory, age_seconds=600
    )

    response = app_fixture.client.post(
        f"/api/admin/sync/requests/{request_id}/cancel",
        headers=csrf,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "cancelled"
    assert payload["finished_at"] is not None
    assert "强制停止" in (payload["error"] or "")
    assert payload["progress"]["cancel_forced"] is True
    assert payload["progress"]["message"] == "已停止（强制）"


def test_cancel_within_grace_keeps_cancelling(app_fixture, container) -> None:
    """宽限期内不强制停止，界面继续显示「停止中」。"""
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    request_id = _create_cancelling_request(
        container.session_factory, age_seconds=1
    )

    response = app_fixture.client.post(
        f"/api/admin/sync/requests/{request_id}/cancel",
        headers=csrf,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "cancelling"
    assert payload["finished_at"] is None
    assert not (payload["progress"] or {}).get("cancel_forced")


def test_request_list_also_converges_forced_cancel(app_fixture, container) -> None:
    """列表轮询时同样会顺手收敛卡住的停止请求，用户不必再点一次。"""
    login = app_fixture.login_admin()
    request_id = _create_cancelling_request(
        container.session_factory, age_seconds=600
    )

    listed = app_fixture.client.get("/api/admin/sync/requests")
    assert listed.status_code == 200, listed.text
    entry = next(
        item for item in listed.json()["items"] if item["id"] == request_id
    )
    assert entry["status"] == "cancelled"
    assert entry["progress"]["cancel_forced"] is True


def test_full_backfill_resume_skips_finished_steps(monkeypatch, tmp_path) -> None:
    """自动续跑：resume_from_step 之前的步骤直接跳过，不重跑几小时的前置步骤。"""
    from app.core.db import Base, make_engine, make_session_factory
    from app.sync import full_backfill

    engine = make_engine(f"sqlite:///{(tmp_path / 'resume.db').as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    calls: list[str] = []

    def _record(name: str, result: dict | None = None):
        def fake(*args, **kwargs):
            calls.append(name)
            return result if result is not None else {"rows": 1}

        return fake

    monkeypatch.setattr(full_backfill, "sync_trading_calendar", _record("calendar", 10))
    monkeypatch.setattr(full_backfill, "sync_universe", _record("universe", {"rows": 1}))
    monkeypatch.setattr(full_backfill, "backfill_tencent_bars", _record("tencent"))
    monkeypatch.setattr(full_backfill, "backfill_baostock_bars", _record("baostock"))
    monkeypatch.setattr(full_backfill, "backfill_bse_bars", _record("bse"))
    monkeypatch.setattr(full_backfill, "recompute_limit_events", _record("limits", 5))
    monkeypatch.setattr(
        full_backfill, "sync_financial_reports", _record("financials", {"rows": 1})
    )
    monkeypatch.setattr(
        full_backfill, "sync_shareholder_counts", _record("shareholders", {"rows": 1})
    )
    monkeypatch.setattr(
        full_backfill, "check_daily_bar_quality", _record("quality", {"rows": 1})
    )

    try:
        with session_factory() as db:
            result = full_backfill.run_full_backfill(
                db,
                session_factory=session_factory,
                engine=engine,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 3, 31),
                asset_type="stock",
                resume_from_step="baostock_stock",
            )
        # baostock_stock 之前的步骤跳过，之后的照跑。
        assert "tencent" not in calls
        assert "bse" in calls
        assert result["resumed"] is True
        assert result["resume_from_step"] == "baostock_stock"
        assert "calendar" in result["skipped_steps"]
        assert "tencent_stock" in result["skipped_steps"]
        assert "baostock_stock" in result["finished_steps"]
        assert result["stall_timeouts"] == 0
    finally:
        engine.dispose()
