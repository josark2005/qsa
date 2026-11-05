from __future__ import annotations

import logging
import threading
import time
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
from app.datasources.baostock import log_baostock_transport
from app.models.sync_request import SyncRequest
from app.services.adjustment_repair import repair_adjustment_factors
from app.services.history_archive import DEFAULT_HISTORY_DIR, import_history
from app.services.worker_logs import (
    CURRENT_SYNC_REQUEST,
    install_worker_log_handler,
)
from app.sync.bars import (
    backfill_baostock_bars,
    backfill_tencent_bars,
    recompute_limit_events,
)
from app.sync.cancellation import SyncCancelled
from app.sync.full_backfill import FULL_STEPS, MARKET_OPEN_DATE, run_full_backfill
from app.sync.pipelines import (
    PIPELINE_NAMES,
    run_pipeline,
    today_shanghai,
)
from app.sync.watchdog import SyncStalled, watch_stall

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 只有真实的数据回补请求会按 sync.retry_count / sync.retry_delay_seconds 重试。
# 定时 pipeline 有外部副作用（例如每日 QQ 群汇报），重试会造成重复推送，故排除。
RETRYABLE_REQUEST_KINDS = frozenset({"backfill", "full_backfill", "history_import"})
# 重试等待按不超过 1 秒的切片 sleep，保证停止请求能在一秒内生效。
RETRY_WAIT_SLICE_SECONDS = 1.0
# 同一次任务因为心跳过期被自动续跑的最大次数；超过就停下留给人工判断，
# 避免在数据库锁或数据源长期不可用时无限重排队。
MAX_AUTO_RECOVERIES = 2
# full_backfill 的步骤名（用于判断卡死后应从哪一步继续）。
FULL_STEP_NAMES = frozenset(FULL_STEPS)


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
            running_kinds = set(
                db.scalars(
                    select(SyncRequest.kind).where(
                        SyncRequest.status.in_(["running", "cancelling"])
                    )
                ).all()
            )
            statement = (
                select(SyncRequest)
                .where(SyncRequest.status == "queued")
                .order_by(SyncRequest.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(10)
            )
            if running_kinds:
                # 同一个 kind 同时只允许一条在跑：过去队列里排着两条全量回补时，
                # 第二个 worker 线程会再领一条，两个回补互相覆盖写库、BaoStock
                # 会话互相顶掉，表现就是「卡住 + 停不下来」。
                statement = statement.where(SyncRequest.kind.notin_(running_kinds))
            entry = db.scalar(statement)
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

    def _setting_int(self, key: str, default: int) -> int:
        if self.settings_service is None:
            return default
        try:
            return int(self.settings_service.snapshot.get_int(key))
        except (KeyError, TypeError, ValueError):
            return default

    def _stall_timeout(self, request_id: int) -> float:
        """单次数据源请求允许的最长无响应时间（秒）；同步等待设置的改动。"""
        return float(self._setting_int("sync.stall_timeout_seconds", 300))

    @property
    def heartbeat_seconds(self) -> float:
        return float(max(1, self._setting_int("sync.heartbeat_seconds", 10)))

    def _heartbeat(
        self,
        request_id: int,
        seconds: float,
        *,
        stall_timeout: float | None = None,
    ) -> None:
        """写一次进度心跳；期间发现停止请求会立刻抛 SyncCancelled。

        `stall_seconds` 表示「已经等了这么久还没有任何响应」，界面据此提示
        用户「正在等待数据源，超时后会跳过并稍后续传」，而不是看起来又卡住了。
        """
        with self.session_factory() as db:
            entry = db.get(SyncRequest, request_id)
            if entry is None:
                return
            if entry.status in {"cancelling", "cancelled"}:
                raise SyncCancelled("用户停止任务")
            progress = dict(entry.progress or {})
            progress["phase"] = progress.get("phase") or "running"
            progress["heartbeat_at"] = utcnow().isoformat()
            progress["stall_seconds"] = round(max(0.0, float(seconds)), 1)
            if stall_timeout is not None:
                progress["stall_timeout_seconds"] = int(stall_timeout)
            progress.setdefault("stall_timeouts", 0)
            entry.progress = progress
            entry.updated_at = utcnow()
            db.commit()

    def _watch(self, request_id: int, stall_timeout: float | None = None):
        """任务级看门狗：按心跳间隔轮询停止请求，同时刷新进度心跳。"""

        def on_tick(seconds: float) -> None:
            self._heartbeat(request_id, seconds, stall_timeout=stall_timeout)

        interval = self.heartbeat_seconds
        return watch_stall(0, on_tick=on_tick, interval=interval)

    def _sleep(self, seconds: float) -> None:
        """可注入的等待钩子；测试替换它即可避免真实 sleep。"""
        time.sleep(seconds)

    def _retry_policy(self, kind: str) -> tuple[int, int]:
        """返回 (重试次数, 重试间隔秒数)。

        仅回补类请求重试；定时 pipeline 有外部副作用（如 QQ 群汇报），重试可能
        重复推送，因此不纳入。未注入 settings_service 时不重试。
        """
        if self.settings_service is None or kind not in RETRYABLE_REQUEST_KINDS:
            return 0, 0
        snapshot = self.settings_service.snapshot
        retry_count = max(0, snapshot.get_int("sync.retry_count"))
        delay_seconds = max(0, snapshot.get_int("sync.retry_delay_seconds"))
        return retry_count, delay_seconds

    def _retry_progress(
        self,
        request_id: int,
        *,
        attempt: int,
        total_attempts: int,
        delay_seconds: int,
        error: Exception,
    ) -> None:
        """失败后记录重试进度，保留原有 percent。"""
        summary = f"{type(error).__name__}: {error}"
        if len(summary) > 300:
            summary = f"{summary[:300]}…"
        with self.session_factory() as db:
            entry = db.get(SyncRequest, request_id)
            progress = dict(entry.progress or {}) if entry is not None else {}
        progress.update(
            {
                "phase": "retry",
                "indicator": "retry",
                "message": (
                    f"第 {attempt}/{total_attempts} 次执行失败：{summary}；"
                    f"{delay_seconds} 秒后重试"
                ),
                "attempt": attempt,
                "total_attempts": total_attempts,
            }
        )
        self._update_progress(request_id, progress)

    def _wait_for_retry(self, request_id: int, delay_seconds: int) -> None:
        """在重试间隔内按不超过 1 秒的切片等待，并持续检查取消。"""
        remaining = float(delay_seconds)
        while remaining > 0:
            if self._is_cancelling(request_id):
                raise SyncCancelled("用户停止任务")
            slice_seconds = min(RETRY_WAIT_SLICE_SECONDS, remaining)
            self._sleep(slice_seconds)
            remaining -= slice_seconds
        if self._is_cancelling(request_id):
            raise SyncCancelled("用户停止任务")

    def _execute_with_retries(self, request: dict) -> dict:
        """执行请求；回补类请求按设置重试，pipeline 等只执行一次。"""
        request_id = request["id"]
        retry_count, delay_seconds = self._retry_policy(request["kind"])
        total_attempts = retry_count + 1
        for attempt in range(1, total_attempts + 1):
            if self._is_cancelling(request_id):
                raise SyncCancelled("用户停止任务")
            try:
                return self._execute(request)
            except SyncCancelled:
                raise
            except Exception as exc:
                request = self._refresh_payload(request)
                if attempt >= total_attempts:
                    raise
                logger.warning(
                    "sync request %s attempt %s/%s failed, retry in %ss: %s: %s",
                    request_id,
                    attempt,
                    total_attempts,
                    delay_seconds,
                    type(exc).__name__,
                    exc,
                )
                self._retry_progress(
                    request_id,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    delay_seconds=delay_seconds,
                    error=exc,
                )
                self._wait_for_retry(request_id, delay_seconds)
        raise RuntimeError("retry loop exhausted unexpectedly")  # pragma: no cover

    def _finish(
        self,
        request_id: int,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        message: str | None = None,
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
                progress.update({"phase": "failed", "message": message or "执行失败"})
            entry.progress = progress
            entry.updated_at = utcnow()
            db.commit()

    def _refresh_payload(self, request: dict) -> dict:
        """重试前重新读一次 payload，拿到心跳里记下的断点续传位置。"""
        try:
            with self.session_factory() as db:
                entry = db.get(SyncRequest, request["id"])
                if entry is None:
                    return request
                progress = dict(entry.progress or {})
                resume = progress.get("resume")
                payload = dict(entry.payload or {})
                if resume and request["kind"] == "backfill":
                    payload["resume"] = resume
                return {**request, "payload": payload}
        except Exception:
            logger.warning(
                "failed to refresh payload for request %s", request["id"], exc_info=True
            )
            return request

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
            with self._watch(request_id):
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
        if request["kind"] == "repair_adj_factors":
            asset_type = payload.get("asset_type") or "all"
            if asset_type not in {"stock", "etf", "all"}:
                raise ValueError(f"unsupported asset_type: {asset_type}")
            symbols = payload.get("symbols") or None
            with self._watch(request_id):
                with self.session_factory() as db:
                    return repair_adjustment_factors(
                        db,
                        symbols=list(symbols) if symbols else None,
                        asset_type=asset_type,
                        apply=True,
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
                progress_callback=lambda update: self._update_progress(
                    request_id, update
                ),
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

            with self._watch(request_id):
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
                        stall_timeout_seconds=self._stall_timeout(request_id),
                        resume_from_step=(
                            payload.get("resume_from_step")
                            or (payload.get("resume") or {}).get("step")
                        ),
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
        # 断点续传：payload["resume"] = {"item": "stock", "after": "600123.SH"}
        # 表示上一个 worker（或本次重试）在这个步骤的这只标的上中断，从这里继续。
        resume = payload.get("resume") or {}
        stall_timeout = self._stall_timeout(request_id)
        types = ["stock", "etf"] if asset_type == "all" else [asset_type]
        results: dict = {}
        with self._watch(request_id):
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
                        stall_timeout_seconds=stall_timeout,
                    )
                for item in types:
                    if self._is_cancelling(request_id):
                        raise SyncCancelled("用户停止任务")
                    resume_after = (
                        resume.get("after") if resume.get("item") == item else None
                    )

                    def progress(
                        update: dict, item: str = item, resume_after=resume_after
                    ) -> None:
                        payload_update = {
                            **update,
                            "source": source,
                            "asset_type": item,
                        }
                        if resume_after:
                            payload_update["resume_after"] = resume_after
                        self._update_progress(request_id, payload_update)

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
                            cancel_check=lambda: self._is_cancelling(request_id),
                            stall_timeout_seconds=stall_timeout,
                            resume_after_symbol=resume_after,
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
                            cancel_check=lambda: self._is_cancelling(request_id),
                            stall_timeout_seconds=stall_timeout,
                            resume_after_symbol=resume_after,
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

    def _complete_request(self, request_id: int, result) -> None:
        """Mark a request finished, surfacing partial failures.

        全量回补按步骤捕获异常并写进 result["errors"]，过去这里一律标记成功，
        用户看到「完成」但 BaoStock 等步骤其实失败了。非空 errors 视为失败。
        """
        errors = (
            result.get("errors")
            if isinstance(result, dict) and result.get("source") == "full"
            else None
        )
        if errors:
            summary = "; ".join(f"{name}: {value}" for name, value in errors.items())
            logger.warning(
                "sync request %s finished with %s failed steps: %s",
                request_id,
                len(errors),
                summary,
            )
            self._finish(
                request_id,
                status="failed",
                result=jsonable_encoder(result),
                error=summary,
                message=f"{len(errors)} 个步骤异常",
            )
            return
        self._finish(
            request_id,
            status="success",
            result=jsonable_encoder(result),
        )

    def _recover_stalled_requests(self) -> None:
        """把心跳过期的 running 请求重新排队，实现「卡死后自动续跑」。

        只处理真正「运行中」的请求：cancelling/cancelled 不动（停止是操作者
        意图，交给强制停止宽限期收敛）；同一任务最多自动续跑
        MAX_AUTO_RECOVERIES 次，超过后标记失败留给人工判断。
        """
        stall_timeout = float(self._setting_int("sync.stall_timeout_seconds", 300))
        guard_seconds = float(self._setting_int("sync.stall_guard_seconds", 0))
        threshold = max(guard_seconds, 2 * stall_timeout + 60 if stall_timeout else 0)
        if threshold <= 0:
            return
        now = utcnow()
        with self.session_factory() as db:
            entries = list(
                db.scalars(
                    select(SyncRequest).where(SyncRequest.status == "running")
                ).all()
            )
            for entry in entries:
                progress = dict(entry.progress or {})
                phase = str(progress.get("phase") or "")
                if phase in {"", "completed", "failed", "cancelled", "queued"}:
                    continue
                heartbeat = progress.get("heartbeat_at") or entry.updated_at
                if heartbeat is None:
                    heartbeat = entry.started_at
                if heartbeat is None:
                    continue
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=now.tzinfo)
                stalled_for = (now - heartbeat).total_seconds()
                if stalled_for < threshold:
                    continue
                payload = dict(entry.payload or {})
                recover_count = int(payload.get("recover_count") or 0)
                if recover_count >= MAX_AUTO_RECOVERIES:
                    entry.status = "failed"
                    entry.finished_at = now
                    entry.error = (
                        f"任务在 {phase} 阶段连续 {recover_count + 1} 次超时无心跳，"
                        "已停止自动续跑，请检查数据源与运行日志"
                    )
                    progress.update(
                        {
                            "phase": "failed",
                            "message": "多次超时，已停止自动续跑",
                        }
                    )
                    entry.progress = progress
                    logger.error(
                        "sync request %s stalled %s times, giving up",
                        entry.id,
                        recover_count + 1,
                    )
                    continue
                payload["recover_count"] = recover_count + 1
                payload["recovered"] = True
                if phase in FULL_STEP_NAMES:
                    if not payload.get("resume_from_step"):
                        payload["resume_from_step"] = phase
                elif entry.kind == "backfill" and not payload.get("resume"):
                    # 单源回补按标的断点续传：从卡住的那只后面继续。
                    payload["resume"] = {
                        "item": progress.get("asset_type") or "stock",
                        "after": progress.get("current"),
                    }
                progress.update(
                    {
                        "phase": "recovering",
                        "message": (
                            f"任务超过 {int(stalled_for)} 秒没有心跳，"
                            f"自动重新排队续跑（第 {recover_count + 1} 次）"
                        ),
                    }
                )
                entry.payload = payload
                entry.progress = progress
                entry.status = "queued"
                entry.started_at = None
                entry.updated_at = now
                logger.warning(
                    "sync request %s stalled for %ss at step %s, requeued (%s)",
                    entry.id,
                    int(stalled_for),
                    phase,
                    recover_count + 1,
                )
            db.commit()

    def _run(self) -> None:
        logger.info("sync request worker started")
        while not self._stop.is_set():
            try:
                self._recover_stalled_requests()
            except Exception:
                logger.exception("failed to recover stalled sync requests")
            request = self._claim()
            if request is None:
                self._stop.wait(self.poll_seconds)
                continue
            request_id = request["id"]
            logger.info("processing sync request %s: %s", request_id, request["kind"])
            request_context = CURRENT_SYNC_REQUEST.set(request_id)
            try:
                result = self._execute_with_retries(request)
                if self._is_cancelling(request_id):
                    self._finish(
                        request_id,
                        status="cancelled",
                        error="用户停止任务",
                    )
                    logger.info("sync request %s cancelled", request_id)
                else:
                    self._complete_request(request_id, result)
                    logger.info("sync request %s handled", request_id)
            except SyncCancelled:
                self._finish(
                    request_id,
                    status="cancelled",
                    error="用户停止任务",
                )
                logger.info("sync request %s cancelled", request_id)
            except SyncStalled as exc:
                # 任务级看门狗在外部超时：本次执行已经没有推进，直接按失败收尾，
                # 由 _execute_with_retries 的重试或自动续跑接着断点续传。
                logger.warning("sync request %s stalled: %s", request_id, exc)
                self._finish(
                    request_id,
                    status="failed",
                    error=f"任务长时间无响应：{exc}",
                    message="任务长时间无响应，稍后可重新提交续跑",
                )
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
            finally:
                CURRENT_SYNC_REQUEST.reset(request_context)
        logger.info("sync request worker stopped")


def main() -> int:
    config = get_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log_baostock_transport()
    engine = make_engine(config.database_url)
    session_factory = make_session_factory(engine)
    log_handler = install_worker_log_handler(session_factory)
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
        log_handler.stop()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())