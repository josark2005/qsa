from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(min_interval, 0.0)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last
            remaining = self._min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last = time.monotonic()


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "request",
) -> T:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if type(exc).__name__ == "Push2RateLimitError":
                logger.error("%s hit Eastmoney rate limit; stop retrying", label)
                raise
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "%s failed (%s/%s): %s; retrying in %.1fs",
                label,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error
