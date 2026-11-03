from __future__ import annotations

from typing import Any

import redis

from app.core.config import AppConfig


def make_redis(config: AppConfig) -> Any:
    if config.use_fake_redis:
        import fakeredis

        return fakeredis.FakeRedis(decode_responses=True)
    return redis.Redis.from_url(config.redis_url, decode_responses=True)
