from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models.worker_log import WorkerLog

logger = logging.getLogger(__name__)

# worker 日志最多保留 20000 行 / 7 天；超长消息截断，避免单条日志把表撑爆。
WORKER_LOG_MAX_ROWS = 20000
WORKER_LOG_KEEP_DAYS = 7
WORKER_LOG_MAX_MESSAGE = 4000
WORKER_LOG_QUEUE_SIZE = 2000
WORKER_LOG_FLUSH_INTERVAL = 1.0
WORKER_LOG_FLUSH_BATCH = 100
WORKER_LOG_TRIM_INTERVAL = 300.0

# 当前 worker 线程正在处理的 sync_requests.id；_run 执行请求时设置，
# 自动化日志落库时带上它，管理端即可按任务筛选日志。
CURRENT_SYNC_REQUEST: ContextVar[int | None] = ContextVar(
    "current_sync_request", default=None
)


class WorkerLogHandler(logging.Handler):
    """把 worker 日志异步落库到 worker_logs 表。

    设计约束：
    - 绝不阻塞业务线程：emit() 只做入队，队列满则丢弃；
    - 绝不自递归：跳过本模块 logger，写库失败也只记 console 不回流；
    - 只有显式 install_worker_log_handler() 才会启动后台线程，
      因此导入 app.services.worker_logs / app.worker 不会启动任何线程。
    """

    def __init__(
        self,
        session_factory,
        *,
        max_rows: int = WORKER_LOG_MAX_ROWS,
        keep_days: int = WORKER_LOG_KEEP_DAYS,
        flush_interval: float = WORKER_LOG_FLUSH_INTERVAL,
        batch_size: int = WORKER_LOG_FLUSH_BATCH,
        queue_size: int = WORKER_LOG_QUEUE_SIZE,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._max_rows = max(1, int(max_rows))
        self._keep_days = max(1, int(keep_days))
        self._flush_interval = max(0.05, float(flush_interval))
        self._batch_size = max(1, int(batch_size))
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_trim = 0.0
        self.dropped = 0

    def start(self) -> WorkerLogHandler:
        """启动后台 flush 线程（幂等）。"""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name="worker-log-flusher",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """停止后台线程并同步写完队列里剩余记录。"""
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._flush()
        logging.getLogger().removeHandler(self)
        self._thread = None

    @staticmethod
    def _should_capture(record: logging.LogRecord) -> bool:
        if record.name.startswith("app.services.worker_logs"):
            return False
        if record.levelno < logging.INFO:
            return False
        return record.name.startswith("app") or record.levelno >= logging.WARNING

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._should_capture(record):
                return
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                traceback_text = "".join(
                    traceback.format_exception(*record.exc_info)
                ).rstrip()
                message = f"{message}\n{traceback_text}"
            self._queue.put_nowait(
                {
                    "created_at": utcnow(),
                    "level": record.levelname,
                    "logger": record.name[:128],
                    "message": message[:WORKER_LOG_MAX_MESSAGE],
                    "request_id": CURRENT_SYNC_REQUEST.get(),
                }
            )
            if self._queue.qsize() >= self._batch_size:
                self._wake.set()
        except queue.Full:
            self.dropped += 1
        except Exception:  # pragma: no cover - 日志落库绝不应打断业务
            self.dropped += 1

    def _run(self) -> None:
        try:
            self._trim()
        except Exception:
            logger.warning("failed to trim worker logs on start", exc_info=True)
        while not self._stop_event.is_set():
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            if self._stop_event.is_set():
                break
            self._flush()
            self._maybe_trim()
        self._flush()

    def _take_batch(self) -> list[dict]:
        rows: list[dict] = []
        while len(rows) < self._batch_size:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def _flush(self) -> None:
        while True:
            rows = self._take_batch()
            if not rows:
                return
            try:
                with self._session_factory() as db:
                    db.execute(insert(WorkerLog), rows)
                    db.commit()
            except Exception:
                self.dropped += len(rows)
                logger.warning(
                    "failed to persist %s worker log rows",
                    len(rows),
                    exc_info=True,
                )

    def _maybe_trim(self) -> None:
        now = time.monotonic()
        if now - self._last_trim < WORKER_LOG_TRIM_INTERVAL:
            return
        self._last_trim = now
        self._trim()

    def _trim(self) -> None:
        self._last_trim = time.monotonic()
        with self._session_factory() as db:
            trim_worker_logs(
                db, keep_days=self._keep_days, keep_rows=self._max_rows
            )


def install_worker_log_handler(session_factory) -> WorkerLogHandler:
    """安装并启动 worker 日志处理器；仅 worker main() 调用（pytest 保持安静）。"""
    handler = WorkerLogHandler(session_factory)
    logging.getLogger().addHandler(handler)
    handler.start()
    return handler


def trim_worker_logs(
    session: Session,
    keep_days: int = WORKER_LOG_KEEP_DAYS,
    keep_rows: int = WORKER_LOG_MAX_ROWS,
) -> int:
    """删除超期日志和超出保留行数的旧日志，返回删除行数。"""
    removed = 0
    if keep_days > 0:
        cutoff = utcnow() - timedelta(days=keep_days)
        result = session.execute(
            delete(WorkerLog).where(WorkerLog.created_at < cutoff)
        )
        removed += int(result.rowcount or 0)
    if keep_rows > 0:
        max_id = session.scalar(select(func.max(WorkerLog.id)))
        if max_id is not None:
            result = session.execute(
                delete(WorkerLog).where(
                    WorkerLog.id <= int(max_id) - int(keep_rows)
                )
            )
            removed += int(result.rowcount or 0)
    session.commit()
    return removed


def list_worker_logs(
    db: Session,
    level: str | None = None,
    request_id: int | None = None,
    q: str | None = None,
    after_id: int | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> tuple[list[WorkerLog], bool]:
    """按条件查询日志，始终按 id 升序返回，(rows, has_more)。

    after_id 用于增量轮询（取更新的日志）；before_id 用于向前翻页；
    两者都不传时返回最新 limit 条（内部倒序查询再反转）。
    """
    limit = max(1, int(limit))
    filters = []
    if level:
        filters.append(WorkerLog.level == level.strip().upper())
    if request_id is not None:
        filters.append(WorkerLog.request_id == int(request_id))
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                WorkerLog.message.ilike(pattern),
                WorkerLog.logger.ilike(pattern),
            )
        )

    statement = select(WorkerLog)
    for condition in filters:
        statement = statement.where(condition)

    if after_id is not None:
        rows = db.scalars(
            statement.where(WorkerLog.id > int(after_id))
            .order_by(WorkerLog.id.asc())
            .limit(limit + 1)
        ).all()
        return list(rows[:limit]), len(rows) > limit

    if before_id is not None:
        rows = db.scalars(
            statement.where(WorkerLog.id < int(before_id))
            .order_by(WorkerLog.id.desc())
            .limit(limit + 1)
        ).all()
        return list(reversed(rows[:limit])), len(rows) > limit

    rows = db.scalars(
        statement.order_by(WorkerLog.id.desc()).limit(limit + 1)
    ).all()
    return list(reversed(rows[:limit])), len(rows) > limit
