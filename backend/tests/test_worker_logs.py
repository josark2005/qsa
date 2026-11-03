from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import select

import app.models  # noqa: F401  # 注册全部模型，供 Base.metadata.create_all 使用
from app.core.db import Base, make_engine, make_session_factory, utcnow
from app.models.sync_request import SyncRequest
from app.models.worker_log import WorkerLog
from app.services.users import create_user
from app.services.worker_logs import (
    CURRENT_SYNC_REQUEST,
    WORKER_LOG_MAX_MESSAGE,
    WorkerLogHandler,
    install_worker_log_handler,
    list_worker_logs,
    trim_worker_logs,
)


def _make_store(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'worker_logs.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _create_request(factory) -> int:
    with factory() as db:
        entry = SyncRequest(kind="backfill", payload={}, status="running")
        db.add(entry)
        db.commit()
        return entry.id


def _add_log(factory, **overrides) -> int:
    values = {
        "created_at": overrides.pop("created_at", utcnow()),
        "level": overrides.pop("level", "INFO"),
        "logger": overrides.pop("logger", "app.worker"),
        "message": overrides.pop("message", "hello"),
        "request_id": overrides.pop("request_id", None),
    }
    values.update(overrides)
    with factory() as db:
        row = WorkerLog(**values)
        db.add(row)
        db.commit()
        return row.id


def test_importing_worker_does_not_start_log_thread() -> None:
    import app.worker  # noqa: F401

    names = {thread.name for thread in threading.enumerate()}
    assert "worker-log-flusher" not in names


def test_handler_persists_matching_logs_with_request_id(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        request_id = _create_request(factory)
        handler = WorkerLogHandler(factory)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        app_logger = logging.getLogger("app.services.test_worker_logs")
        app_logger.setLevel(logging.DEBUG)
        handler.start()
        external = logging.getLogger("external.library")
        external.setLevel(logging.DEBUG)
        token = CURRENT_SYNC_REQUEST.set(request_id)
        try:
            app_logger.info("sync %s started", request_id)
            app_logger.debug("debug not captured")
            external.info("external info not captured")
            logging.getLogger("app.services.worker_logs").warning("self log skipped")
        finally:
            CURRENT_SYNC_REQUEST.reset(token)
        external.warning("external warning captured")
        handler.stop()
        root_logger.removeHandler(handler)

        with factory() as db:
            rows = db.scalars(select(WorkerLog).order_by(WorkerLog.id)).all()
        assert [row.message for row in rows] == [
            f"sync {request_id} started",
            "external warning captured",
        ]
        info_row, warning_row = rows
        assert info_row.level == "INFO"
        assert info_row.logger == "app.services.test_worker_logs"
        assert info_row.request_id == request_id
        assert info_row.created_at is not None
        assert warning_row.level == "WARNING"
        assert warning_row.request_id is None
    finally:
        engine.dispose()


def test_handler_truncates_message_and_appends_traceback(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        handler = WorkerLogHandler(factory)
        logger = logging.getLogger("app.services.test_worker_logs")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        handler.start()
        logger.info("x" * (WORKER_LOG_MAX_MESSAGE + 500))
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("request crashed")
        handler.stop()
        logger.removeHandler(handler)

        with factory() as db:
            rows = db.scalars(select(WorkerLog).order_by(WorkerLog.id)).all()
        assert len(rows) == 2
        assert len(rows[0].message) == WORKER_LOG_MAX_MESSAGE
        assert "Traceback (most recent call last)" in rows[1].message
        assert "ValueError: boom" in rows[1].message
    finally:
        engine.dispose()


def test_handler_drops_records_when_queue_is_full(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        handler = WorkerLogHandler(factory, queue_size=1)
        logger = logging.getLogger("app.services.test_worker_logs")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.info("first")
        logger.info("second")
        handler.stop()
        logger.removeHandler(handler)

        with factory() as db:
            rows = db.scalars(select(WorkerLog)).all()
        assert len(rows) == 1
        assert rows[0].message == "first"
        assert handler.dropped == 1
    finally:
        engine.dispose()


def test_install_worker_log_handler_writes_and_stops(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        handler = install_worker_log_handler(factory)
        assert handler._thread is not None
        assert handler._thread.is_alive()
        logger = logging.getLogger("app.services.test_worker_logs")
        logger.setLevel(logging.INFO)
        logger.info("installed handler works")
        handler.stop()
        assert handler._thread is None
        with factory() as db:
            rows = db.scalars(select(WorkerLog)).all()
        assert any(row.message == "installed handler works" for row in rows)
    finally:
        engine.dispose()


def test_worker_run_sets_current_sync_request_context(tmp_path) -> None:
    from app.worker import RequestWorker

    engine, factory = _make_store(tmp_path)
    try:
        request_id = _create_request(factory)
        worker = RequestWorker(factory, engine)
        captured: list[int | None] = []

        def fake_execute(request):
            captured.append(CURRENT_SYNC_REQUEST.get())
            return {"rows": 1}

        pending = [
            {"id": request_id, "kind": "backfill", "pipeline": None, "payload": {}}
        ]

        def claim():
            if pending:
                return pending.pop(0)
            worker._stop.set()
            return None

        worker._claim = claim
        worker._execute = fake_execute
        worker._run()

        assert captured == [request_id]
        assert CURRENT_SYNC_REQUEST.get() is None
    finally:
        engine.dispose()


def test_list_worker_logs_filters_and_pagination(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        request_id = _create_request(factory)
        first = _add_log(
            factory,
            level="INFO",
            logger="app.sync.bars",
            message="alpha start",
            request_id=request_id,
        )
        second = _add_log(
            factory,
            level="WARNING",
            logger="app.sync.bars",
            message="alpha slow",
            request_id=request_id,
        )
        third = _add_log(
            factory, level="ERROR", logger="app.worker", message="beta boom"
        )

        with factory() as db:
            rows, has_more = list_worker_logs(db)
            assert [row.id for row in rows] == [first, second, third]
            assert has_more is False

            rows, has_more = list_worker_logs(db, limit=2)
            assert [row.id for row in rows] == [second, third]
            assert has_more is True

            rows, _ = list_worker_logs(db, level="warning")
            assert [row.id for row in rows] == [second]

            rows, _ = list_worker_logs(db, request_id=request_id)
            assert [row.id for row in rows] == [first, second]

            rows, _ = list_worker_logs(db, q="boom")
            assert [row.id for row in rows] == [third]

            rows, _ = list_worker_logs(db, q="sync.bars")
            assert [row.id for row in rows] == [first, second]

            rows, has_more = list_worker_logs(db, after_id=first, limit=1)
            assert [row.id for row in rows] == [second]
            assert has_more is True

            rows, has_more = list_worker_logs(db, after_id=second, limit=1)
            assert [row.id for row in rows] == [third]
            assert has_more is False

            rows, has_more = list_worker_logs(db, before_id=third, limit=1)
            assert [row.id for row in rows] == [second]
            assert has_more is True

            rows, has_more = list_worker_logs(db, before_id=second, limit=1)
            assert [row.id for row in rows] == [first]
            assert has_more is False
    finally:
        engine.dispose()


def test_trim_worker_logs_removes_old_and_excess_rows(tmp_path) -> None:
    engine, factory = _make_store(tmp_path)
    try:
        old_time = utcnow() - timedelta(days=30)
        old_ids = [
            _add_log(factory, message="old", created_at=old_time) for _ in range(2)
        ]
        recent_ids = [
            _add_log(factory, message=f"recent {index}") for index in range(5)
        ]

        with factory() as db:
            removed = trim_worker_logs(db, keep_days=7, keep_rows=3)
            remaining = [
                row.id
                for row in db.scalars(
                    select(WorkerLog).order_by(WorkerLog.id)
                ).all()
            ]

        assert removed == 4
        assert remaining == recent_ids[-3:]
        assert not set(old_ids) & set(remaining)
    finally:
        engine.dispose()


def test_admin_logs_api_auth_and_payload(app_fixture) -> None:
    session_factory = app_fixture.client.app.state.container.session_factory
    with session_factory() as db:
        db.add(WorkerLog(level="INFO", logger="app.worker", message="worker started"))
        db.add(
            WorkerLog(
                level="ERROR", logger="app.sync.bars", message="pull failed"
            )
        )
        db.commit()

    app_fixture.login_admin()
    response = app_fixture.client.get("/api/admin/logs")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["items"]) == 2
    assert payload["has_more"] is False
    assert payload["items"][0]["id"] < payload["items"][1]["id"]
    item = payload["items"][0]
    assert item["level"] == "INFO"
    assert item["logger"] == "app.worker"
    assert item["request_id"] is None
    assert item["created_at"].endswith(("Z", "+00:00"))

    filtered = app_fixture.client.get("/api/admin/logs?level=error")
    assert filtered.status_code == 200
    assert [entry["message"] for entry in filtered.json()["items"]] == [
        "pull failed"
    ]

    with session_factory() as db:
        user, _ = create_user(
            db,
            username="logviewer",
            role="user",
            password="ViewerPassword123",
            created_by=None,
            min_password_length=12,
        )
        user.must_change_password = False
        db.commit()
    app_fixture.login("logviewer", "ViewerPassword123")
    blocked = app_fixture.client.get("/api/admin/logs")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "admin_required"


def test_admin_logs_api_pagination(app_fixture) -> None:
    session_factory = app_fixture.client.app.state.container.session_factory
    with session_factory() as db:
        for index in range(3):
            db.add(
                WorkerLog(
                    level="INFO",
                    logger="app.worker",
                    message=f"row {index}",
                )
            )
        db.commit()

    app_fixture.login_admin()
    first_page = app_fixture.client.get("/api/admin/logs?limit=2")
    assert first_page.status_code == 200, first_page.text
    first_payload = first_page.json()
    assert len(first_payload["items"]) == 2
    assert first_payload["has_more"] is True
    assert [entry["message"] for entry in first_payload["items"]] == [
        "row 1",
        "row 2",
    ]

    after_id = first_payload["items"][-1]["id"]
    with session_factory() as db:
        db.add(WorkerLog(level="INFO", logger="app.worker", message="row 3"))
        db.commit()

    polled = app_fixture.client.get(f"/api/admin/logs?after_id={after_id}")
    assert polled.status_code == 200, polled.text
    polled_payload = polled.json()
    assert [entry["message"] for entry in polled_payload["items"]] == ["row 3"]
    assert polled_payload["has_more"] is False


def test_admin_logs_api_validation(app_fixture) -> None:
    app_fixture.login_admin()

    bad_level = app_fixture.client.get("/api/admin/logs?level=TRACE")
    assert bad_level.status_code == 422
    assert bad_level.json()["code"] == "invalid_log_level"

    both_cursors = app_fixture.client.get(
        "/api/admin/logs?after_id=1&before_id=2"
    )
    assert both_cursors.status_code == 422
    assert both_cursors.json()["code"] == "invalid_log_cursor"

    long_query = app_fixture.client.get("/api/admin/logs?q=" + "x" * 121)
    assert long_query.status_code == 422

    too_large_limit = app_fixture.client.get("/api/admin/logs?limit=501")
    assert too_large_limit.status_code == 422
