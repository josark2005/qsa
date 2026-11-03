from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import AppConfig
from app.core.settings_service import SettingsService
from app.core.settings_watcher import SettingsWatcher
from app.services.login_limiter import LoginLimiter
from app.services.sessions import SessionService


@dataclass
class Container:
    config: AppConfig
    engine: Engine
    session_factory: sessionmaker
    redis: Any
    settings_service: SettingsService
    session_service: SessionService
    login_limiter: LoginLimiter
    settings_watcher: SettingsWatcher
