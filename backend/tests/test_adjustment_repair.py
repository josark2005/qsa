from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import Base, make_engine, make_session_factory
from app.models.daily_bar import DailyBar
from app.models.instrument import Instrument
from app.models.sync_request import SyncRequest
from app.services.adjustment_repair import repair_adjustment_factors
from app.sync.cancellation import SyncCancelled
from app.worker import RequestWorker


def _seed(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'repair.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    start = date(2026, 9, 25)
    with factory() as db:
        mixed = Instrument(
            symbol="600299.SH",
            code="600299",
            exchange="SH",
            name="安迪苏",
            asset_type="stock",
            board="main",
            status="active",
            is_st=False,
            source="test",
        )
        consistent = Instrument(
            symbol="600000.SH",
            code="600000",
            exchange="SH",
            name="浦发银行",
            asset_type="stock",
            board="main",
            status="active",
            is_st=False,
            source="test",
        )
        db.add_all([mixed, consistent])
        db.flush()

        factors = [2.42] * 29 + [1.0]
        closes = [8.09] * 29 + [8.49]
        for index, (factor, close) in enumerate(zip(factors, closes)):
            db.add(
                DailyBar(
                    instrument_id=mixed.id,
                    trade_date=start + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    close_adj=close * factor,
                    volume=1000,
                    adj_factor=factor,
                    is_suspended=False,
                    is_st=False,
                    source="test",
                )
            )
        for index in range(10):
            db.add(
                DailyBar(
                    instrument_id=consistent.id,
                    trade_date=start + timedelta(days=index),
                    open=8.4,
                    high=8.4,
                    low=8.4,
                    close=8.4,
                    close_adj=8.4,
                    volume=1000,
                    adj_factor=1.0,
                    is_suspended=False,
                    is_st=False,
                    source="test",
                )
            )
        db.commit()
    return engine, factory


def _factors(factory, symbol: str) -> list[float]:
    with factory() as db:
        instrument = db.scalar(select(Instrument).where(Instrument.symbol == symbol))
        rows = list(
            db.scalars(
                select(DailyBar)
                .where(DailyBar.instrument_id == instrument.id)
                .order_by(DailyBar.trade_date.asc())
            )
        )
        return [float(row.adj_factor) for row in rows]


def test_repair_dry_run_does_not_write(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            report = repair_adjustment_factors(db, apply=False)
        assert report["mode"] == "dry-run"
        assert report["instruments_scanned"] == 2
        assert report["instruments_fixed"] == 1
        assert report["rows_updated"] == 1
        assert report["samples"][0]["symbol"] == "600299.SH"
        assert report["samples"][0]["broken_factor"] == 1.0
        assert report["samples"][0]["repaired_factor"] == 2.42
        assert _factors(factory, "600299.SH")[-1] == 1.0
    finally:
        engine.dispose()


def test_repair_apply_normalizes_factors_and_close_adj(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            report = repair_adjustment_factors(db, apply=True)
        assert report["mode"] == "applied"
        assert report["instruments_fixed"] == 1
        assert report["rows_updated"] == 1

        factors = _factors(factory, "600299.SH")
        assert factors == sorted(factors), factors
        assert factors[-1] == 2.42

        with factory() as db:
            instrument = db.scalar(
                select(Instrument).where(Instrument.symbol == "600299.SH")
            )
            last = db.scalar(
                select(DailyBar)
                .where(DailyBar.instrument_id == instrument.id)
                .order_by(DailyBar.trade_date.desc())
            )
            assert float(last.adj_factor) == 2.42
            assert round(float(last.close_adj), 4) == round(8.49 * 2.42, 4)
    finally:
        engine.dispose()


def test_repair_reports_progress_and_honours_cancel(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        updates: list[dict] = []
        with factory() as db:
            repair_adjustment_factors(
                db, apply=False, progress_callback=updates.append
            )
        assert updates
        assert updates[-1]["percent"] == 100
        assert updates[-1]["processed"] == 2

        with pytest.raises(SyncCancelled):
            with factory() as db:
                repair_adjustment_factors(
                    db, apply=False, cancel_check=lambda: True
                )
    finally:
        engine.dispose()


def test_repair_request_can_be_submitted(app_fixture) -> None:
    login = app_fixture.login_admin()
    csrf = app_fixture.csrf_headers(login["csrf_token"])

    created = app_fixture.client.post(
        "/api/admin/sync/requests",
        headers=csrf,
        json={"kind": "repair_adj_factors", "asset_type": "all"},
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["kind"] == "repair_adj_factors"
    assert payload["status"] == "queued"
    assert payload["payload"]["asset_type"] == "all"

    again = app_fixture.client.post(
        "/api/admin/sync/requests",
        headers=csrf,
        json={"kind": "repair_adj_factors", "asset_type": "all"},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "repair_adj_factors_active"


def test_worker_executes_repair_request(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            entry = SyncRequest(
                kind="repair_adj_factors",
                payload={"asset_type": "all"},
                status="running",
            )
            db.add(entry)
            db.commit()
            request_id = entry.id

        worker = RequestWorker(factory, engine)
        result = worker._execute(
            {
                "id": request_id,
                "kind": "repair_adj_factors",
                "pipeline": None,
                "payload": {"asset_type": "all"},
            }
        )
        assert result["mode"] == "applied"
        assert result["instruments_fixed"] == 1
        assert _factors(factory, "600299.SH")[-1] == 2.42
    finally:
        engine.dispose()


def test_repair_symbol_filter_skips_other_instruments(tmp_path) -> None:
    engine, factory = _seed(tmp_path)
    try:
        with factory() as db:
            report = repair_adjustment_factors(
                db, symbols=["600000.SH"], apply=True
            )
        assert report["instruments_scanned"] == 1
        assert report["instruments_fixed"] == 0
        assert report["rows_updated"] == 0
        assert _factors(factory, "600299.SH")[-1] == 1.0
    finally:
        engine.dispose()
