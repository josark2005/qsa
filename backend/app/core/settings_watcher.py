from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.core.settings_service import SettingsService

logger = logging.getLogger(__name__)


class SettingsWatcher:
    def __init__(
        self,
        service: SettingsService,
        redis_client: Any | None,
        channel: str,
        poll_seconds: int,
        on_reload: Callable[[], None] | None = None,
    ) -> None:
        self._service = service
        self._redis = redis_client
        self._channel = channel
        self._poll_seconds = max(poll_seconds, 5)
        self._on_reload = on_reload
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="settings-watcher", daemon=True
        )

    def _reload(self) -> None:
        self._service.load()
        if self._on_reload is not None:
            try:
                self._on_reload()
            except Exception:
                logger.exception("settings reload callback failed")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        pubsub = None
        last_poll = 0.0
        while not self._stop.is_set():
            try:
                if self._redis is not None:
                    if pubsub is None:
                        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                        pubsub.subscribe(self._channel)
                    message = pubsub.get_message(timeout=1.0)
                    if message is not None:
                        self._reload()
                        last_poll = time.monotonic()
                else:
                    time.sleep(1.0)

                now = time.monotonic()
                if now - last_poll >= self._poll_seconds:
                    self._reload()
                    last_poll = now
            except Exception:
                logger.exception("settings watcher failed; retrying in 5 seconds")
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                    pubsub = None
                time.sleep(5)
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass
