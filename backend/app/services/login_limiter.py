from __future__ import annotations

import hashlib
from typing import Any

from redis.exceptions import RedisError

from app.core.settings_service import SettingsService
from app.services.sessions import SessionUnavailableError


class LoginLimiter:
    def __init__(self, redis_client: Any, settings_service: SettingsService) -> None:
        self._redis = redis_client
        self._settings = settings_service

    @staticmethod
    def _key(ip: str, username: str) -> str:
        digest = hashlib.sha256(f"{ip}|{username}".encode("utf-8")).hexdigest()[:40]
        return f"qsa:login:fail:{digest}"

    def check(self, ip: str, username: str) -> tuple[bool, int]:
        max_attempts = self._settings.snapshot.get_int(
            "security.login_max_attempts"
        )
        lock_seconds = (
            self._settings.snapshot.get_int("security.login_lock_minutes") * 60
        )
        key = self._key(ip, username)
        try:
            raw = self._redis.get(key)
            if raw is None:
                return True, 0
            count = int(raw)
            if count < max_attempts:
                return True, 0
            ttl = self._redis.ttl(key)
            return False, max(int(ttl), lock_seconds)
        except RedisError as exc:
            raise SessionUnavailableError("登录限流存储不可用") from exc

    def record_failure(self, ip: str, username: str) -> None:
        lock_seconds = (
            self._settings.snapshot.get_int("security.login_lock_minutes") * 60
        )
        key = self._key(ip, username)
        try:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = pipe.execute()
            if int(ttl) < 0:
                self._redis.expire(key, lock_seconds)
        except RedisError as exc:
            raise SessionUnavailableError("登录限流存储不可用") from exc

    def reset(self, ip: str, username: str) -> None:
        try:
            self._redis.delete(self._key(ip, username))
        except RedisError as exc:
            raise SessionUnavailableError("登录限流存储不可用") from exc
