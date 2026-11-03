from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.db import utcnow
from app.models.sync_run import SyncRun

logger = logging.getLogger(__name__)


class StepRun:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        pipeline: str,
        step: str,
        run_id: uuid.UUID | None = None,
        target_date: date | None = None,
        trigger: str = "manual",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.pipeline = pipeline
        self.step = step
        self.run_id = run_id or uuid.uuid4()
        self.target_date = target_date
        self.trigger = trigger
        self.meta = meta
        self.id: int | None = None

    def __enter__(self) -> StepRun:
        with self.session_factory() as db:
            entry = SyncRun(
                run_id=self.run_id,
                pipeline=self.pipeline,
                step=self.step,
                status="running",
                trigger=self.trigger,
                target_date=self.target_date,
                meta=self.meta,
            )
            db.add(entry)
            db.commit()
            self.id = entry.id
        return self

    def finish(self, *, rows: int | None = None, meta: dict[str, Any] | None = None) -> None:
        if self.id is None:
            return
        with self.session_factory() as db:
            entry = db.get(SyncRun, self.id)
            if entry is None:
                return
            entry.status = "success"
            entry.finished_at = utcnow()
            entry.rows_affected = rows
            if meta is not None:
                entry.meta = {**(entry.meta or {}), **meta}
            db.commit()

    def skip(self, reason: str) -> None:
        if self.id is None:
            return
        with self.session_factory() as db:
            entry = db.get(SyncRun, self.id)
            if entry is None:
                return
            entry.status = "skipped"
            entry.finished_at = utcnow()
            entry.error = reason
            db.commit()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is not None and self.id is not None:
            with self.session_factory() as db:
                entry = db.get(SyncRun, self.id)
                if entry is not None:
                    entry.status = "failed"
                    entry.finished_at = utcnow()
                    entry.error = f"{type(exc).__name__}: {exc}"[:4000]
                    db.commit()
            logger.exception("sync step failed: %s/%s", self.pipeline, self.step)
        return False


@contextmanager
def advisory_lock(engine: Engine, name: str) -> Iterator[bool]:
    if engine.dialect.name != "postgresql":
        yield True
        return
    connection = engine.connect()
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"),
                {"name": f"qsa:sync:{name}"},
            ).scalar()
        )
        if not acquired:
            logger.warning("sync lock is already held: %s", name)
        yield acquired
    finally:
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"),
                {"name": f"qsa:sync:{name}"},
            )
        except Exception:
            logger.exception("failed to release sync lock: %s", name)
        connection.close()
