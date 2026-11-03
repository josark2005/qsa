from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.core.config import get_config
from app.core.db import make_engine, make_session_factory, utcnow
from app.core.redis_client import make_redis
from app.core.settings_service import SettingsService
from app.core.settings_watcher import SettingsWatcher
from app.models.sync_request import SyncRequest
from app.services.history_archive import DEFAULT_HISTORY_DIR, import_history
from app.sync.bars import (
    backfill_baostock_bars,
    backfill_tencent_bars,
    recompute_limit_events,
)
from app.sync.cancellation import SyncCancelled
from app.sync.full_backfill import MARKET_OPEN_DATE, run_full_backfill
from app.sync.pipelines import (
    PIPELINE_NAMES,
    run_pipeline,
    today_shanghai,
)

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class SchedulerManager:
    def __init__(
        self,
        scheduler,
        session_factory,
        engine,
        settings_service,
        redis_client,
    ) -> None:
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.engine = engine
        self.settings_service = settings_service
        self.redis_client = redis_client
        self._signature: tuple | None = None

    def apply(self) -> None:
        snapshot = self.settings_service.snapshot
        signature = tuple(
            (
                name,
                snapshot.get_bool(f"sync.pipeline.{name}.enabled"),
                snapshot.get_str(f"sync.pipeline.{name}.cron"),
            )
            for name in PIPELINE_NAMES
        )
        if signature == self._signature:
            return

        misfire = snapshot.get_int("sync.misfire_grace_seconds")
        for name in PIPELINE_NAMES:
            job_id = f"pipeline:{name}"
            enabled = snapshot.get_bool(f"sync.pipeline.{name}.enabled")
            existing = self.scheduler.get_job(job_id)
            if not enabled:
                if existing is not None:
                    self.scheduler.remove_job(job_id)
                    logger.info("pipeline %s disabled", name)
                continue
            cron = snapshot.get_str(f"sync.pipeline.{name}.cron")
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=SHANGHAI)
            except Exception:
                logger.exception("invalid cron for pipeline %s: %s", name, cron)
                continue
            self.scheduler.add_job(
                self._run_job,
                trigger=trigger,
                args=[name],
                id=job_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=misfire,
                name=f"pipeline:{name}",
            )
            logger.info("pipeline %s scheduled with cron %s", name, cron)
        self._signature = signature

    def _run_job(self, name: str) -> None:
        try:
            result = run_pipeline(
                name,
                session_factory=self.session_factory,
                engine=self.engine,
                trigger="schedule",
                settings_service=self.settings_service,
                redis_client=self.redis_client,
            )
            logger.info("pipeline %s finished: %s", name, result)
        except Exception:
            logger.exception("pipeline %s failed", name)


class RequestWorker:
    def __init__(
        self,
        session_factory,
        engine,
        settings_service=None,
        redis_client=None,
        poll_seconds: float = 3.0,
        max_workers: int = 1,
    ) -> None:
        self.session_factory = session_factory
        self.engine = engine
        self.settings_service = settings_service
        self.redis_client = redis_client
        self.poll_seconds = poll_seconds
        self.worker_count = max(1, int(max_workers))
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"sync-request-worker-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count)
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def _claim(self) -> dict | None:
        with self.session_factory() as db:
            entry = db.scalar(
                select(SyncRequest)
                .where(SyncRequest.status == "queued")
                .order_by(SyncRequest.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if entry is None:
                return None
            entry.status = "running"
            entry.started_at = utcnow()
            db.commit()
            return {
                "id": entry.id,
                "kind": entry.kind,
                "pipeline": entry.pipeline,
                "payload": entry.payload or {},
            }

    def _update_progress(self, request_id: int, progress: dict) -> None:
        with self.session_factory() as db:
            entry = db.get(SyncRequest, request_id)
            if entry is None:
                return
            if entry.status in {"cancelling", "cancelled"}:
                raise SyncCancelled("用户停止任务")
            entry.progress = jsonable_encoder(progress)
            entry.updated_at = utcnow()
            db.commit()

    def _is_cancelling(self, request_id: int) -> bool:
        with self.session_factory() as db:
            status = db.scalar(
                select(SyncRequest.status).where(SyncRequest.id == request_id)
            )
            return status in {"cancelling", "cancelled"}

    def _finish(
        self,
        request_id: int,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.session_factory() as db:
            entry = db.get(SyncRequest, request_id)
            if entry is None:
                return
            entry.status = status
            entry.finished_at = utcnow()
            entry.result = result
            entry.error = error[:4000] if error else None
            progress = dict(entry.progress or {})
            if status == "success":
                progress.update(
                    {"phase": "completed", "percent": 100, "message": "完成"}
                )
            elif status == "cancelled":
                progress.update({"phase": "cancelled", "message": "已停止"})
            else:
                progress.update({"phase": "failed", "message": "执行失败"})
            entry.progress = progress
            entry.updated_at = utcnow()
            db.commit()

    def _execute(self, request: dict) -> dict:
        payload = request["payload"]
        request_id = request["id"]
        if self._is_cancelling(request_id):
            raise SyncCancelled("用户停止任务")
        if request["kind"] == "history_import":
            config = get_config()
            directory = (
                Path(config.history_dir)
                if config.history_dir
                else DEFAULT_HISTORY_DIR
            )
            with self.session_factory() as db:
                return import_history(
                    db,
                    source_dir=directory,
                    force=bool(payload.get("force", False)),
                    progress_callback=lambda update: self._update_progress(
                        request_id, update
                    ),
                    cancel_check=lambda: self._is_cancelling(request_id),
                )
        if request["kind"] == "pipeline":
            if not request["pipeline"]:
                raise ValueError("pipeline request missing pipeline")
            target = (
                date.fromisoformat(payload["target_date"])
                if payload.get("target_date")
                else None
            )
            self._update_progress(
                request_id,
                {
                    "phase": "pipeline",
                    "message": f"运行 {request['pipeline']}",
                    "percent": 0,
                },
            )
            return run_pipeline(
                request["pipeline"],
                session_factory=self.session_factory,
                engine=self.engine,
                target_date=target,
                trigger="manual",
                settings_service=self.settings_service,
                redis_client=self.redis_client,
                cancel_check=lambda: self._is_cancelling(request_id),
            )

        if request["kind"] == "full_backfill":
            start = (
                date.fromisoformat(payload["start"])
                if payload.get("start")
                else MARKET_OPEN_DATE
            )
            end = (
                date.fromisoformat(payload["end"])
                if payload.get("end")
                else today_shanghai()
            )
            asset_type = payload.get("asset_type", "all")
            limit = payload.get("limit")
            include_delisted = bool(payload.get("include_delisted", False))

            def full_progress(update: dict) -> None:
                self._update_progress(
                    request_id,
                    {**update, "source": "full", "asset_type": asset_type},
                )

            with self.session_factory() as db:
                return run_full_backfill(
                    db,
                    session_factory=self.session_factory,
                    engine=self.engine,
                    start_date=start,
                    end_date=end,
                    limit=limit,
                    include_delisted=include_delisted,
                    asset_type=asset_type,
                    progress_callback=full_progress,
                    cancel_check=lambda: self._is_cancelling(request_id),
                )

        if request["kind"] != "backfill":
            raise ValueError(f"unknown request kind: {request['kind']}")

        start = (
            date.fromisoformat(payload["start"])
            if payload.get("start")
            else date(2023, 1, 1)
        )
        end = (
            date.fromisoformat(payload["end"])
            if payload.get("end")
            else today_shanghai()
        )
        source = payload.get("source", "tencent")
        asset_type = payload.get("asset_type", "all")
        limit = payload.get("limit")
        include_delisted = bool(payload.get("include_delisted", False))
        rebuild_limits = bool(payload.get("rebuild_limits", True))
        full_refresh = bool(payload.get("full_refresh", False))
        symbols = payload.get("symbols") or None
        types = ["stock", "etf"] if asset_type == "all" else [asset_type]
        results: dict = {}
        with self.session_factory() as db:
            if source == "full":
                def full_progress(update: dict) -> None:
                    self._update_progress(
                        request_id,
                        {**update, "source": "full", "asset_type": asset_type},
                    )

                return run_full_backfill(
                    db,
                    session_factory=self.session_factory,
                    engine=self.engine,
                    start_date=start,
                    end_date=end,
                    limit=limit,
                    include_delisted=include_delisted,
                    asset_type=asset_type,
                    progress_callback=full_progress,
                    cancel_check=lambda: self._is_cancelling(request_id),
                )
            for item in types:
                if self._is_cancelling(request_id):
                    raise SyncCancelled("用户停止任务")
                def progress(update: dict, item: str = item) -> None:
                    self._update_progress(
                        request_id,
                        {
                            **update,
                            "source": source,
                            "asset_type": item,
                        },
                    )

                if source == "tencent":
                    payload_result = backfill_tencent_bars(
                        db,
                        start_date=start,
                        end_date=end,
                        asset_type=item,
                        include_delisted=include_delisted,
                        limit=limit,
                        symbols=symbols,
                        progress_callback=progress,
                        progress_every=5,
                        full_refresh=full_refresh,
                    )
                else:
                    payload_result = backfill_baostock_bars(
                        db,
                        start_date=start,
                        end_date=end,
                        asset_type=item,
                        include_delisted=include_delisted,
                        limit=limit,
                        symbols=symbols,
                        progress_callback=progress,
                        progress_every=1,
                        full_refresh=full_refresh,
                    )
                results[item] = payload_result
            if self._is_cancelling(request_id):
                raise SyncCancelled("用户停止任务")
            if rebuild_limits:
                self._update_progress(
                    request_id,
                    {
                        "phase": "limits",
                        "message": "重算涨跌停事件",
                        "percent": 0,
                        "source": source,
                    },
                )
                results["limit_events"] = recompute_limit_events(db, start, end)
                self._update_progress(
                    request_id,
                    {
                        "phase": "limits",
                        "message": "涨跌停事件完成",
                        "percent": 100,
                        "source": source,
                        "rows": results["limit_events"],
                    },
                )
        results["source"] = source
        results["start"] = start.isoformat()
        results["end"] = end.isoformat()
        return results

    def _run(self) -> None:
        logger.info("sync request worker started")
        while not self._stop.is_set():
            request = self._claim()
            if request is None:
                self._stop.wait(self.poll_seconds)
                continue
            request_id = request["id"]
            logger.info("processing sync request %s: %s", request_id, request["kind"])
            try:
                result = self._execute(request)
                if self._is_cancelling(request_id):
                    self._finish(
                        request_id,
                        status="cancelled",
                        error="用户停止任务",
                    )
                    logger.info("sync request %s cancelled", request_id)
                else:
                    self._finish(
                        request_id,
                        status="success",
                        result=jsonable_encoder(result),
                    )
                    logger.info("sync request %s completed", request_id)
            except SyncCancelled:
                self._finish(
                    request_id,
                    status="cancelled",
                    error="用户停止任务",
                )
                logger.info("sync request %s cancelled", request_id)
            except Exception as exc:
                if self._is_cancelling(request_id):
                    self._finish(
                        request_id,
                        status="cancelled",
                        error="用户停止任务",
                    )
                    logger.info("sync request %s cancelled", request_id)
                else:
                    logger.exception("sync request %s failed", request_id)
                    self._finish(
                        request_id,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
        logger.info("sync request worker stopped")


def main() -> int:
    config = get_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine = make_engine(config.database_url)
    session_factory = make_session_factory(engine)
    redis_client = make_redis(config)
    scheduler = BlockingScheduler(
        timezone=SHANGHAI,
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={"coalesce": True, "max_instances": 1},
    )

    def publish() -> None:
        redis_client.publish(config.settings_channel, "changed")

    settings_service = SettingsService(session_factory, publisher=publish)
    settings_service.load()
    manager = SchedulerManager(
        scheduler,
        session_factory,
        engine,
        settings_service,
        redis_client,
    )
    watcher = SettingsWatcher(
        settings_service,
        redis_client,
        config.settings_channel,
        config.settings_poll_seconds,
        on_reload=manager.apply,
    )
    watcher.start()
    request_worker = RequestWorker(
        session_factory,
        engine,
        settings_service=settings_service,
        redis_client=redis_client,
        max_workers=settings_service.snapshot.get_int("sync.max_concurrent_jobs"),
    )
    request_worker.start()
    capabilities_stop = threading.Event()

    def publish_capabilities() -> None:
        try:
            redis_client.set("qsa:worker:capabilities", "full_backfill_v1", ex=90)
        except Exception:
            logger.warning("failed to publish worker capabilities", exc_info=True)

    def capability_heartbeat() -> None:
        publish_capabilities()
        while not capabilities_stop.wait(30):
            publish_capabilities()

    threading.Thread(
        target=capability_heartbeat,
        name="worker-capabilities",
        daemon=True,
    ).start()
    manager.apply()
    logger.info("sync worker started with pipelines: %s", ", ".join(PIPELINE_NAMES))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("sync worker stopping")
    finally:
        capabilities_stop.set()
        request_worker.stop()
        watcher.stop()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            redis_client.close()
        except Exception:
            pass
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
