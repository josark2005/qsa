from __future__ import annotations

from app.core.db import Base, make_engine, make_session_factory
from app.models.sync_request import SyncRequest
from app.worker import RequestWorker


def _make_worker(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    worker = RequestWorker(factory, engine)
    return worker, factory, engine


def _create_request(factory, **overrides) -> int:
    with factory() as db:
        entry = SyncRequest(
            kind=overrides.pop("kind", "full_backfill"),
            payload=overrides.pop("payload", {}),
            status=overrides.pop("status", "running"),
            progress=overrides.pop(
                "progress",
                {"phase": "baostock_stock", "message": "BaoStock 全字段与多源核对"},
            ),
            **overrides,
        )
        db.add(entry)
        db.commit()
        return entry.id


def test_full_backfill_step_errors_mark_request_failed(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path)
    try:
        request_id = _create_request(factory)
        worker._complete_request(
            request_id,
            {
                "source": "full",
                "errors": {
                    "baostock_stock": (
                        "RuntimeError: baostock login failed: 网络接收错误。"
                    )
                },
            },
        )
        with factory() as db:
            entry = db.get(SyncRequest, request_id)
            assert entry is not None
            assert entry.status == "failed"
            assert entry.error is not None
            assert "baostock_stock" in entry.error
            assert entry.progress["phase"] == "failed"
            assert entry.progress["message"] == "1 个步骤异常"
    finally:
        engine.dispose()


def test_full_backfill_without_errors_marks_success(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path)
    try:
        request_id = _create_request(factory)
        worker._complete_request(
            request_id, {"source": "full", "errors": {}, "steps": {}}
        )
        with factory() as db:
            entry = db.get(SyncRequest, request_id)
            assert entry is not None
            assert entry.status == "success"
            assert entry.error is None
            assert entry.progress["message"] == "完成"
    finally:
        engine.dispose()


def test_non_full_result_ignores_errors_key(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path)
    try:
        request_id = _create_request(factory, kind="backfill")
        worker._complete_request(request_id, {"source": "tencent", "errors": ["X"]})
        with factory() as db:
            entry = db.get(SyncRequest, request_id)
            assert entry is not None
            assert entry.status == "success"
    finally:
        engine.dispose()
