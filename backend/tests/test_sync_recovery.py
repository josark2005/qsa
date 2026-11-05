from __future__ import annotations

from datetime import timedelta

from app.core.db import Base, make_engine, make_session_factory, utcnow
from app.models.sync_request import SyncRequest
from app.worker import MAX_AUTO_RECOVERIES, RequestWorker

# 参考 test_worker_retry.py：直接调 worker 的私有方法，不启动真实线程。


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get_int(self, key: str) -> int:
        return int(self._values[key])

    def get_str(self, key: str) -> str:
        return str(self._values[key])


class _FakeSettings:
    def __init__(
        self,
        *,
        stall_timeout: int = 300,
        stall_guard: int = 0,
        heartbeat: int = 10,
    ) -> None:
        self.snapshot = _FakeSnapshot(
            {
                "sync.stall_timeout_seconds": stall_timeout,
                "sync.stall_guard_seconds": stall_guard,
                "sync.heartbeat_seconds": heartbeat,
            }
        )


def _make_worker(tmp_path, name: str = "recover.db", settings_service=None):
    engine = make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    worker = RequestWorker(factory, engine, settings_service=settings_service)
    return worker, factory, engine


def _create_request(
    factory,
    *,
    kind: str = "full_backfill",
    status: str = "running",
    progress: dict | None = None,
    payload: dict | None = None,
    age_seconds: int = 0,
) -> int:
    """心跳过期靠「把 updated_at / started_at 往回拨」构造，测试里不做真实等待。"""
    stale = utcnow() - timedelta(seconds=age_seconds)
    with factory() as db:
        entry = SyncRequest(
            kind=kind,
            payload=payload if payload is not None else {},
            status=status,
            progress=progress,
            started_at=stale if status == "running" else None,
            updated_at=stale,
        )
        db.add(entry)
        db.commit()
        return entry.id


def _get_request(factory, request_id: int) -> SyncRequest:
    with factory() as db:
        entry = db.get(SyncRequest, request_id)
        assert entry is not None
        return entry


def test_recover_stalled_request_requeues_and_records_resume_point(tmp_path) -> None:
    # 默认阈值 = max(stall_guard(0), 2*300+60) = 660 秒；心跳停在 1000 秒前。
    worker, factory, engine = _make_worker(tmp_path, settings_service=_FakeSettings())
    try:
        request_id = _create_request(
            factory,
            progress={"phase": "baostock_stock", "percent": 40},
            age_seconds=1000,
        )
        worker._recover_stalled_requests()

        entry = _get_request(factory, request_id)
        assert entry.status == "queued"
        assert entry.started_at is None
        assert entry.payload["recovered"] is True
        assert entry.payload["recover_count"] == 1
        # 全量回补按步骤续跑：续跑起点就是卡住的那一步。
        assert entry.payload["resume_from_step"] == "baostock_stock"
        assert entry.progress["phase"] == "recovering"
    finally:
        engine.dispose()


def test_recover_skips_request_with_fresh_heartbeat(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path, settings_service=_FakeSettings())
    try:
        request_id = _create_request(
            factory,
            progress={"phase": "baostock_stock"},
            age_seconds=5,
        )
        worker._recover_stalled_requests()

        entry = _get_request(factory, request_id)
        assert entry.status == "running"
        assert "recovered" not in (entry.payload or {})
    finally:
        engine.dispose()


def test_recover_skips_cancelling_and_finished_requests(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path, settings_service=_FakeSettings())
    try:
        cancelling_id = _create_request(
            factory,
            status="cancelling",
            progress={"phase": "baostock_stock"},
            age_seconds=100000,
        )
        completed_id = _create_request(
            factory,
            progress={"phase": "completed", "percent": 100},
            age_seconds=100000,
        )
        worker._recover_stalled_requests()

        # 停止是操作者意图，交给强制停止宽限期收敛；已完成的当然不动。
        assert _get_request(factory, cancelling_id).status == "cancelling"
        assert _get_request(factory, completed_id).status == "running"
        assert _get_request(factory, completed_id).payload == {}
    finally:
        engine.dispose()


def test_recover_gives_up_after_max_auto_recoveries(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path, settings_service=_FakeSettings())
    try:
        request_id = _create_request(
            factory,
            progress={"phase": "baostock_stock"},
            payload={"recover_count": MAX_AUTO_RECOVERIES},
            age_seconds=1000,
        )
        worker._recover_stalled_requests()

        entry = _get_request(factory, request_id)
        assert entry.status == "failed"
        assert entry.error is not None
        assert "自动续跑" in entry.error
    finally:
        engine.dispose()


def test_recover_is_disabled_when_threshold_is_zero(tmp_path) -> None:
    # 两个设置都为 0 时阈值为 0，直接不处理（不会因为 0 秒阈值把所有任务都重置）。
    worker, factory, engine = _make_worker(
        tmp_path, settings_service=_FakeSettings(stall_timeout=0, stall_guard=0)
    )
    try:
        request_id = _create_request(
            factory,
            progress={"phase": "baostock_stock"},
            age_seconds=1000,
        )
        worker._recover_stalled_requests()
        assert _get_request(factory, request_id).status == "running"
    finally:
        engine.dispose()


def test_claim_skips_kind_that_is_already_running(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path)
    try:
        running_id = _create_request(factory, kind="full_backfill", status="running")
        queued_id = _create_request(factory, kind="full_backfill", status="queued")

        claimed = worker._claim()

        # 同一 kind 已经在跑：不领第二条（过去两个 worker 线程会双开全量回补）。
        assert claimed is None
        assert _get_request(factory, queued_id).status == "queued"
        assert _get_request(factory, running_id).status == "running"
    finally:
        engine.dispose()


def test_claim_still_takes_request_of_other_kind(tmp_path) -> None:
    worker, factory, engine = _make_worker(tmp_path)
    try:
        _create_request(factory, kind="full_backfill", status="running")
        queued_id = _create_request(factory, kind="backfill", status="queued")

        claimed = worker._claim()

        assert claimed is not None
        assert claimed["id"] == queued_id
        assert claimed["kind"] == "backfill"
        assert _get_request(factory, queued_id).status == "running"
        # 领完，队列空了。
        assert worker._claim() is None
    finally:
        engine.dispose()
