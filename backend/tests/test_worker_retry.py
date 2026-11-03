from __future__ import annotations

from app.core.db import Base, make_engine, make_session_factory
from app.models.sync_request import SyncRequest
from app.worker import RequestWorker


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get_int(self, key: str) -> int:
        return int(self._values[key])

    def get_str(self, key: str) -> str:
        return str(self._values[key])


class _FakeSettings:
    def __init__(self, *, retry_count: int = 3, delay_seconds: int = 300) -> None:
        self.snapshot = _FakeSnapshot(
            {
                "sync.retry_count": retry_count,
                "sync.retry_delay_seconds": delay_seconds,
            }
        )


def _make_worker(tmp_path, settings_service=None):
    engine = make_engine(f"sqlite:///{(tmp_path / 'retry.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    worker = RequestWorker(factory, engine, settings_service=settings_service)
    return worker, factory, engine


def _create_request(factory, *, kind: str = "backfill", progress: dict | None = None) -> int:
    with factory() as db:
        entry = SyncRequest(
            kind=kind,
            payload={},
            status="running",
            progress=progress,
        )
        db.add(entry)
        db.commit()
        return entry.id


def _get_request(factory, request_id: int) -> SyncRequest:
    with factory() as db:
        entry = db.get(SyncRequest, request_id)
        assert entry is not None
        return entry


def _run_one_request(worker, request_id: int, *, kind: str = "backfill") -> None:
    """只跑一轮 _run 主循环，不启动后台线程。"""
    pending = [
        {"id": request_id, "kind": kind, "pipeline": None, "payload": {}}
    ]

    def claim():
        if pending:
            return pending.pop(0)
        worker._stop.set()
        return None

    worker._claim = claim
    worker.poll_seconds = 0
    worker._run()


def test_transient_failure_retries_then_succeeds(tmp_path) -> None:
    worker, factory, engine = _make_worker(
        tmp_path, settings_service=_FakeSettings(retry_count=1, delay_seconds=2)
    )
    try:
        request_id = _create_request(
            factory, progress={"phase": "bars", "percent": 42}
        )
        calls: list[int] = []
        sleeps: list[float] = []
        progress_updates: list[dict] = []

        def fake_execute(request):
            calls.append(request["id"])
            if len(calls) == 1:
                raise RuntimeError("网络抖动")
            return {"rows": 3}

        real_update = worker._update_progress

        def capture_update(rid: int, progress: dict) -> None:
            progress_updates.append(dict(progress))
            real_update(rid, progress)

        worker._execute = fake_execute
        worker._sleep = lambda seconds: sleeps.append(seconds)
        worker._update_progress = capture_update
        _run_one_request(worker, request_id)

        entry = _get_request(factory, request_id)
        assert calls == [request_id, request_id]
        assert entry.status == "success"
        assert entry.result == {"rows": 3}
        # delay=2 秒被拆成不超过 1 秒的切片，且测试中不发生真实 sleep。
        assert sleeps == [1.0, 1.0]

        retry_updates = [u for u in progress_updates if u.get("phase") == "retry"]
        assert len(retry_updates) == 1
        retry_update = retry_updates[0]
        assert retry_update["indicator"] == "retry"
        assert retry_update["attempt"] == 1
        assert retry_update["total_attempts"] == 2
        assert "网络抖动" in retry_update["message"]
        # 重试进度保留进入重试前的 percent。
        assert retry_update["percent"] == 42
    finally:
        engine.dispose()


def test_retry_count_zero_fails_without_retrying(tmp_path) -> None:
    worker, factory, engine = _make_worker(
        tmp_path, settings_service=_FakeSettings(retry_count=0, delay_seconds=2)
    )
    try:
        request_id = _create_request(factory)
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_execute(request):
            calls.append(request["id"])
            raise ValueError("数据源不可用")

        worker._execute = fake_execute
        worker._sleep = lambda seconds: sleeps.append(seconds)
        _run_one_request(worker, request_id)

        entry = _get_request(factory, request_id)
        assert len(calls) == 1
        assert sleeps == []
        assert entry.status == "failed"
        assert entry.error is not None
        assert "ValueError: 数据源不可用" in entry.error
    finally:
        engine.dispose()


def test_cancellation_during_retry_wait_marks_request_cancelled(tmp_path) -> None:
    worker, factory, engine = _make_worker(
        tmp_path, settings_service=_FakeSettings(retry_count=3, delay_seconds=5)
    )
    try:
        request_id = _create_request(factory)
        calls: list[int] = []

        def fake_execute(request):
            calls.append(request["id"])
            raise RuntimeError("数据源不可用")

        def fake_sleep(_seconds: float) -> None:
            with factory() as db:
                entry = db.get(SyncRequest, request_id)
                assert entry is not None
                entry.status = "cancelling"
                db.commit()

        worker._execute = fake_execute
        worker._sleep = fake_sleep
        _run_one_request(worker, request_id)

        entry = _get_request(factory, request_id)
        assert len(calls) == 1
        assert entry.status == "cancelled"
        assert entry.error == "用户停止任务"
    finally:
        engine.dispose()


def test_pipeline_requests_are_not_retried(tmp_path) -> None:
    worker, factory, engine = _make_worker(
        tmp_path, settings_service=_FakeSettings(retry_count=3, delay_seconds=2)
    )
    try:
        request_id = _create_request(factory, kind="pipeline")
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_execute(request):
            calls.append(request["id"])
            raise RuntimeError("pipeline 失败")

        worker._execute = fake_execute
        worker._sleep = lambda seconds: sleeps.append(seconds)
        _run_one_request(worker, request_id, kind="pipeline")

        entry = _get_request(factory, request_id)
        assert len(calls) == 1
        assert sleeps == []
        assert entry.status == "failed"
    finally:
        engine.dispose()


def test_missing_settings_service_disables_retry(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path, settings_service=None)
    try:
        request_id = _create_request(factory)
        calls: list[int] = []

        def fake_execute(request):
            calls.append(request["id"])
            raise RuntimeError("失败")

        worker._execute = fake_execute
        _run_one_request(worker, request_id)

        entry = _get_request(factory, request_id)
        assert len(calls) == 1
        assert entry.status == "failed"
    finally:
        engine.dispose()
