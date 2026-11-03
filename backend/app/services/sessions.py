from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError

from app.core.security import generate_csrf_token, generate_session_token
from app.core.settings_service import SettingsService


class SessionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionData:
    token: str
    user_id: uuid.UUID
    csrf: str
    created_at: str


class SessionService:
    def __init__(self, redis_client: Any, settings_service: SettingsService) -> None:
        self._redis = redis_client
        self._settings = settings_service

    @staticmethod
    def _session_key(token: str) -> str:
        return f"qsa:session:{token}"

    @staticmethod
    def _user_key(user_id: uuid.UUID | str) -> str:
        return f"qsa:user_sessions:{user_id}"

    def ttl_seconds(self) -> int:
        return self._settings.snapshot.get_int("security.session_ttl_minutes") * 60

    def _ttl_seconds(self) -> int:
        return self.ttl_seconds()

    def create(self, user_id: uuid.UUID) -> SessionData:
        token = generate_session_token()
        csrf = generate_csrf_token()
        created_at = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {
                "user_id": str(user_id),
                "csrf": csrf,
                "created_at": created_at,
            },
            separators=(",", ":"),
        )
        ttl = self._ttl_seconds()
        key = self._session_key(token)
        user_key = self._user_key(user_id)
        try:
            pipe = self._redis.pipeline()
            pipe.set(key, payload, ex=ttl)
            pipe.sadd(user_key, token)
            pipe.expire(user_key, ttl)
            pipe.execute()
        except RedisError as exc:
            raise SessionUnavailableError("会话存储不可用") from exc
        return SessionData(
            token=token,
            user_id=user_id,
            csrf=csrf,
            created_at=created_at,
        )

    def get(self, token: str) -> SessionData | None:
        try:
            raw = self._redis.get(self._session_key(token))
        except RedisError as exc:
            raise SessionUnavailableError("会话存储不可用") from exc
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return SessionData(
                token=token,
                user_id=uuid.UUID(payload["user_id"]),
                csrf=payload["csrf"],
                created_at=payload.get("created_at", ""),
            )
        except (ValueError, KeyError, TypeError):
            return None

    def delete(self, token: str, user_id: uuid.UUID) -> None:
        try:
            pipe = self._redis.pipeline()
            pipe.delete(self._session_key(token))
            pipe.srem(self._user_key(user_id), token)
            pipe.execute()
        except RedisError as exc:
            raise SessionUnavailableError("会话存储不可用") from exc

    def revoke_user(self, user_id: uuid.UUID) -> None:
        user_key = self._user_key(user_id)
        try:
            tokens = self._redis.smembers(user_key)
            pipe = self._redis.pipeline()
            for token in tokens:
                pipe.delete(self._session_key(token))
            pipe.delete(user_key)
            pipe.execute()
        except RedisError as exc:
            raise SessionUnavailableError("会话存储不可用") from exc

    def revoke_others(self, user_id: uuid.UUID, keep_token: str) -> None:
        user_key = self._user_key(user_id)
        try:
            tokens = self._redis.smembers(user_key)
            pipe = self._redis.pipeline()
            for token in tokens:
                if token != keep_token:
                    pipe.delete(self._session_key(token))
            pipe.delete(user_key)
            pipe.sadd(user_key, keep_token)
            pipe.expire(user_key, self._ttl_seconds())
            pipe.expire(self._session_key(keep_token), self._ttl_seconds())
            pipe.execute()
        except RedisError as exc:
            raise SessionUnavailableError("会话存储不可用") from exc
