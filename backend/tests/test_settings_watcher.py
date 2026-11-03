from __future__ import annotations

import time

import fakeredis

from app.core.db import Base, make_engine, make_session_factory
from app.core.settings_service import SettingUpdate, SettingsService
from app.core.settings_watcher import SettingsWatcher


def test_settings_watcher_reloads_other_instance(tmp_path) -> None:
    db_url = f"sqlite:///{(tmp_path / 'watcher.db').as_posix()}"
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    channel = "qsa:test:settings"

    service_a = SettingsService(
        session_factory,
        publisher=lambda: redis_client.publish(channel, "changed"),
    )
    service_b = SettingsService(session_factory)
    service_a.load()
    service_b.load()

    watcher = SettingsWatcher(service_b, redis_client, channel, poll_seconds=60)
    watcher.start()
    try:
        service_a.update(
            [SettingUpdate(key="quota.screen_per_minute", value=77, version=0)],
            actor_id=None,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if service_b.snapshot.get_int("quota.screen_per_minute") == 77:
                break
            time.sleep(0.05)
        assert service_b.snapshot.get_int("quota.screen_per_minute") == 77
    finally:
        watcher.stop()
        redis_client.close()
        engine.dispose()
