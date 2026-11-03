from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from redis.exceptions import RedisError

from app.core.settings_service import SettingsService
from app.models.user import User


class QuotaUnavailableError(RuntimeError):
    pass


class ScreenQuota:
    def __init__(self, redis_client: Any, settings_service: SettingsService) -> None:
        self._redis = redis_client
        self._settings = settings_service

    def check_and_consume(
        self, user: User
    ) -> tuple[bool, int, str | None]:
        overrides = user.quota_overrides or {}
        minute_limit = overrides.get(
            "screen_per_minute",
            self._settings.snapshot.get_int("quota.screen_per_minute"),
        )
        day_limit = overrides.get(
            "screen_per_day",
            self._settings.snapshot.get_int("quota.screen_per_day"),
        )
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        minute_key = (
            f"qsa:quota:screen:minute:{user.id}:{int(now.timestamp() // 60)}"
        )
        day_key = f"qsa:quota:screen:day:{user.id}:{now.date().isoformat()}"
        try:
            pipe = self._redis.pipeline()
            pipe.get(minute_key)
            pipe.get(day_key)
            minute_raw, day_raw = pipe.execute()
            minute_count = int(minute_raw or 0)
            day_count = int(day_raw or 0)
            if minute_limit and minute_limit > 0 and minute_count >= int(minute_limit):
                return False, 60 - now.second, "minute"
            if day_limit and day_limit > 0 and day_count >= int(day_limit):
                tomorrow = datetime.combine(
                    now.date() + timedelta(days=1),
                    time.min,
                    tzinfo=now.tzinfo,
                )
                return False, max(1, int((tomorrow - now).total_seconds())), "day"
            pipe = self._redis.pipeline()
            pipe.incr(minute_key)
            pipe.expire(minute_key, 60)
            pipe.incr(day_key)
            pipe.expire(day_key, 86400)
            pipe.execute()
            return True, 0, None
        except RedisError as exc:
            raise QuotaUnavailableError("筛选配额服务暂不可用") from exc
